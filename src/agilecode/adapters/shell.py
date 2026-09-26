"""A real Bash process with separate command, stdin and completion channels.

This is a command console, not a PTY emulator. Exit/cwd are reported by the shell,
not guessed from prompt strings or a quiet output stream.
"""
from __future__ import annotations
import asyncio
import hashlib
import os
import shlex
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from agilecode.core.types import AccessDenied, AgileCodeError, Interrupted, NeedsReview, uid, now, Event, Events
from .guard import CommandGuard
from .workspace import Workspace, inside, digest
from .store import ProjectStore

MAX_LOG_BYTES = 32 * 1024 * 1024
MAX_PREVIEW_BYTES = 64 * 1024


def shell_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    # Preserve ordinary runtime environment while avoiding inheritance of obvious
    # endpoint credentials and Git redirection. This is hygiene, not isolation.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and not any(s in k.upper() for s in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))}
    env.pop("BASH_ENV", None); env.pop("ENV", None)
    env.update({"TERM": "dumb", "NO_COLOR": "1", "PYTHONUNBUFFERED": "1"})
    if extra:
        env.update(extra)
    return env


@dataclass
class ShellResult:
    exit_code: int
    cwd: str
    output: str
    truncated: bool = False
    raw: bytes = b""


@dataclass
class _Command:
    id: str
    result: asyncio.Future
    out_done: asyncio.Event = field(default_factory=asyncio.Event)
    err_done: asyncio.Event = field(default_factory=asyncio.Event)
    data: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    callback: Callable[[bytes], None] | None = None


class PersistentShell:
    def __init__(self, workspace: Workspace, environment: dict[str, str] | None = None):
        self.workspace = workspace
        self.guard = CommandGuard(workspace)
        self.cwd = workspace.root
        self.environment = shell_environment(environment)
        self.lock = asyncio.Lock()
        self.process: asyncio.subprocess.Process | None = None
        self.current: _Command | None = None
        self.tasks: list[asyncio.Task] = []
        self.input_write: int | None = None
        self.input_read: int | None = None
        self.control_write: int | None = None
        self.control_transport = None
        self.generation = 0

    async def start(self):
        if self.process and self.process.returncode is None:
            return
        control_read, control_write = os.pipe()
        input_read, input_write = os.pipe()
        self.input_write, self.input_read, self.control_write = input_write, input_read, control_write
        try:
            self.process = await asyncio.create_subprocess_exec(
                "bash", "--noprofile", "--norc", "-s", cwd=self.workspace.root,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                pass_fds=(control_write, input_read), env=self.environment, start_new_session=True)
        except BaseException:
            for fd in (control_read, control_write, input_read, input_write):
                os.close(fd)
            raise
        os.close(control_write)
        os.close(input_read)
        reader = asyncio.StreamReader(limit=65536)
        protocol = asyncio.StreamReaderProtocol(reader)
        self.control_transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, os.fdopen(control_read, "rb", buffering=0))
        assert self.process.stdout and self.process.stderr and self.process.stdin
        self.tasks = [asyncio.create_task(self._pump(self.process.stdout, "out")),
                      asyncio.create_task(self._pump(self.process.stderr, "err")),
                      asyncio.create_task(self._control(reader))]
        self.process.stdin.write(b"shopt -s expand_aliases\nset +H\n")
        await self.process.stdin.drain()
        self.cwd = self.workspace.root
        self.generation += 1

    def _output(self, command: _Command | None, data: bytes):
        if command is None or not data:
            return
        if command.callback:
            command.callback(data)
        left = MAX_PREVIEW_BYTES - len(command.data)
        if left > 0:
            command.data.extend(data[:left])
        if len(data) > left:
            command.truncated = True

    async def _pump(self, stream: asyncio.StreamReader, kind: str):
        buffer = b""
        last: _Command | None = None
        try:
            while True:
                data = await stream.read(8192)
                if not data:
                    self._output(last or self.current, buffer)
                    break
                buffer += data
                command = self.current
                last = command
                if command:
                    marker = f"\x1e{command.id}:{kind}\x1f".encode()
                    position = buffer.find(marker)
                    if position >= 0:
                        self._output(command, buffer[:position])
                        buffer = buffer[position+len(marker):]
                        (command.out_done if kind == "out" else command.err_done).set()
                        # Anything after the marker is not credited to the next command.
                        if buffer:
                            self._output(command, buffer); buffer = b""
                    elif len(buffer) > len(marker):
                        self._output(command, buffer[:-len(marker)])
                        buffer = buffer[-len(marker):]
                else:
                    buffer = b""
        except asyncio.CancelledError:
            raise
        finally:
            if self.current:
                (self.current.out_done if kind == "out" else self.current.err_done).set()

    async def _control(self, reader: asyncio.StreamReader):
        try:
            while True:
                identity = (await reader.readuntil(b"\0"))[:-1].decode()
                code = int((await reader.readuntil(b"\0"))[:-1])
                cwd = (await reader.readuntil(b"\0"))[:-1].decode("utf-8", errors="surrogateescape")
                command = self.current
                if command and command.id == identity and not command.result.done():
                    command.result.set_result((code, cwd))
        except (asyncio.IncompleteReadError, ValueError, OSError):
            if self.current and not self.current.result.done():
                self.current.result.set_exception(Interrupted("Shell ended without reporting a command result. Its volatile state is lost; do not blindly replay the command."))

    async def execute(self, command: str, *, timeout=None, approved_unknown=False,
                      callback: Callable[[bytes], None] | None = None, internal=False) -> ShellResult:
        async with self.lock:
            if not internal:
                decision = self.guard.validate(command, self.cwd)
                if decision.decision == "deny":
                    raise AccessDenied(decision.reason)
                if decision.decision == "ask" and not approved_unknown:
                    raise NeedsReview(decision.reason)
            await self.start()
            assert self.process and self.process.stdin
            current = _Command(uid(), asyncio.get_running_loop().create_future(), callback=callback)
            self.current = current
            script = (f"builtin eval -- {shlex.quote(command)} <&{self.input_read}\n"
                      "__agilecode_rc=$?\n"
                      f"builtin printf '\\036{current.id}:out\\037'\n"
                      f"builtin printf '\\036{current.id}:err\\037' >&2\n"
                      f"builtin printf '{current.id}\\0%d\\0%s\\0' \"$__agilecode_rc\" \"$PWD\" >&{self.control_write}\n")
            try:
                self.process.stdin.write(script.encode())
                await self.process.stdin.drain()
                async with asyncio.timeout(timeout):
                    code, cwd = await current.result
                    await current.out_done.wait()
                    await current.err_done.wait()
                self.cwd = Path(cwd).resolve()
                if not inside(self.cwd, self.workspace.root):
                    await self.close()
                    raise AccessDenied("Command left the ticket worktree indirectly. The shell was stopped; previous side effects may already have occurred.")
                return ShellResult(code, cwd, current.data.decode("utf-8", errors="replace"), current.truncated, bytes(current.data))
            except (asyncio.CancelledError, asyncio.TimeoutError):
                await self.close()
                raise
            finally:
                self.current = None

    async def exported_environment(self) -> dict[str, str]:
        result = await self.execute("env -0", internal=True)
        if result.truncated:
            raise AgileCodeError("Exported shell environment is too large for a managed-job snapshot.")
        return {os.fsdecode(item.split(b"=",1)[0]): os.fsdecode(item.split(b"=",1)[1])
                for item in result.raw.split(b"\0") if b"=" in item}

    async def send_input(self, text: str):
        if self.input_write is None or not self.current:
            raise AgileCodeError("No running foreground command is accepting input.")
        await asyncio.to_thread(os.write, self.input_write, text.encode())

    async def close(self):
        process = self.process
        if process and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 1.5)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
        if self.current and not self.current.result.done():
            self.current.result.cancel()
        for task in self.tasks:
            if task is not asyncio.current_task():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*(t for t in self.tasks if t is not asyncio.current_task()), return_exceptions=True)
        self.tasks = []
        if self.control_transport:
            self.control_transport.close(); self.control_transport = None
        if self.input_write is not None:
            try: os.close(self.input_write)
            except OSError: pass
            self.input_write = None
        self.process = None


class ShellService:
    """Ticket-owned persistent shell and separately owned background jobs."""
    def __init__(self, workspace: Workspace, store: ProjectStore, events: Events, ticket_id: str, run_id: str,
                 artifact_root: Path, *, event_phase="development"):
        self.workspace, self.store, self.events = workspace, store, events
        self.ticket_id, self.run_id = ticket_id, run_id
        self.artifact_root = artifact_root
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.shell = PersistentShell(workspace)
        self.background: dict[str, tuple[asyncio.subprocess.Process, asyncio.Task]] = {}
        self.foreground_task: asyncio.Task | None = None
        self.foreground_id: str | None = None
        self.event_phase = event_phase

    async def run(self, command: str, attempt_id: str | None, *, timeout=None, background=False) -> dict:
        job_id, artifact_id = uid(), uid()
        path = self.artifact_root / f"{artifact_id}.log"
        await self.store.insert("artifacts", {"id": artifact_id, "ticket_id": self.ticket_id, "name": "Command output",
                                             "path": str(path), "size": 0, "complete": 0, "created": now()})
        await self.store.insert("jobs", {"id": job_id, "ticket_id": self.ticket_id, "run_id": self.run_id,
                                       "attempt_id": attempt_id, "command": command, "cwd": str(self.shell.cwd),
                                       "status": "starting", "artifact_id": artifact_id, "background": int(background), "created": now()})
        if background:
            decision = self.shell.guard.validate(command, self.shell.cwd)
            if decision.decision == "deny" or (decision.decision == "ask" and not approved_unknown):
                await self.store.update("jobs", job_id, status="denied", finished=now())
                raise AccessDenied(decision.reason)
            env = await self.shell.exported_environment()
            await self.store.update("jobs", job_id, env_hash=digest(str(sorted(env.items())).encode()))
            proc = await asyncio.create_subprocess_exec("bash", "--noprofile", "--norc", "-c", command,
                cwd=self.shell.cwd, env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
            task = asyncio.create_task(self._background_job(job_id, artifact_id, path, proc, attempt_id))
            self.background[job_id] = proc, task
            await self.store.update("jobs", job_id, status="running")
            return {"job_id": job_id, "status": "running", "artifact_id": artifact_id, "background": True}
        self.foreground_task = asyncio.current_task()
        self.foreground_id = job_id
        await self.store.update("jobs", job_id, status="running")
        total, truncated = 0, False
        handle = path.open("wb")
        def output(data: bytes):
            nonlocal total, truncated
            amount = max(0, min(len(data), MAX_LOG_BYTES-total))
            handle.write(data[:amount]); total += amount
            truncated |= amount < len(data)
            self.events.emit(Event("ShellOutput", self.ticket_id, self.run_id, attempt_id, data={"job_id": job_id, "text": data.decode(errors="replace")}))
        try:
            result = await self.shell.execute(command, timeout=timeout, callback=output)
            await self.store.update("jobs", job_id, status="completed", exit_code=result.exit_code, cwd=result.cwd, finished=now())
            return {"job_id": job_id, "artifact_id": artifact_id, "exit_code": result.exit_code, "cwd": result.cwd,
                    "output": result.output, "partial": truncated or result.truncated, "status": "completed"}
        except asyncio.CancelledError:
            await self.store.update("jobs", job_id, status="cancelled", finished=now())
            raise
        except BaseException:
            await self.store.update("jobs", job_id, status="interrupted", finished=now())
            raise
        finally:
            handle.close()
            await self._artifact_finished(artifact_id, path)
            self.foreground_task = None; self.foreground_id = None
            self.events.emit(Event("JobChanged", self.ticket_id, self.run_id, attempt_id, data={"job_id": job_id}))

    async def _artifact_finished(self, artifact_id: str, path: Path):
        data = await asyncio.to_thread(path.read_bytes)
        await self.store.update("artifacts", artifact_id, size=len(data), hash=digest(data), complete=1)

    async def _background_job(self, identity, artifact_id, path, proc, attempt_id):
        total = 0
        with path.open("wb") as handle:
            async def pump(reader):
                nonlocal total
                while data := await reader.read(8192):
                    amount = max(0, min(len(data), MAX_LOG_BYTES-total))
                    handle.write(data[:amount]); total += amount
                    self.events.emit(Event("ShellOutput", self.ticket_id, self.run_id, attempt_id,
                                           data={"job_id": identity, "text": data.decode(errors="replace")}))
            try:
                await asyncio.gather(pump(proc.stdout), pump(proc.stderr))
                code = await proc.wait()
                await self.store.update("jobs", identity, status="completed", exit_code=code, finished=now())
            except asyncio.CancelledError:
                await self._kill(proc)
                await self.store.update("jobs", identity, status="cancelled", finished=now())
                raise
            finally:
                handle.flush()
        await self._artifact_finished(artifact_id, path)
        self.events.emit(Event("JobChanged", self.ticket_id, self.run_id, attempt_id, data={"job_id": identity}))

    async def send_input(self, job_id: str, text: str):
        if job_id == self.foreground_id:
            await self.shell.send_input(text)
        elif job_id in self.background:
            proc, _ = self.background[job_id]
            if proc.stdin and proc.returncode is None:
                proc.stdin.write(text.encode()); await proc.stdin.drain()
            else:
                raise AgileCodeError("The job no longer accepts input.")
        else:
            raise AccessDenied("Job is not owned by this live ticket shell.")

    @staticmethod
    async def _kill(proc):
        if proc.returncode is None:
            try: os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: await asyncio.wait_for(proc.wait(), 1.5)
            except asyncio.TimeoutError:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                await proc.wait()

    async def stop(self, job_id: str):
        if job_id == self.foreground_id and self.foreground_task:
            self.foreground_task.cancel()
            await self.shell.close()
        elif job_id in self.background:
            proc, task = self.background[job_id]
            await self._kill(proc)
            await asyncio.gather(task, return_exceptions=True)
        else:
            raise AccessDenied("This job is not owned by the current ticket.")

    def has_live_jobs(self) -> bool:
        return self.foreground_id is not None or any(proc.returncode is None for proc, _ in self.background.values())

    async def close(self):
        await self.shell.close()
        for proc, task in self.background.values():
            await self._kill(proc)
            await asyncio.gather(task, return_exceptions=True)
        self.background.clear()
