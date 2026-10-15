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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from agilecode.core.types import AccessDenied, AgileCodeError, Interrupted, uid, Event, Events
from .guard import CommandGuard
from .workspace import Workspace, inside
from .store import ProjectStore
from agilecode.application.services.artifacts import ArtifactService
from agilecode.application.services.jobs import JobService
from agilecode.application.cancellation import current_cancellation, cancellation_checkpoint
from agilecode.application.coordination import settle
from .processes import stop_process_group, live_group_members, spawn_owned_process

MAX_PREVIEW_BYTES = 64 * 1024


def shell_environment(extra: dict[str, str] | None = None, *, isolate_python=False) -> dict[str, str]:
    # Preserve ordinary runtime environment while avoiding inheritance of obvious
    # endpoint credentials and Git redirection. This is hygiene, not isolation.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and not any(s in k.upper() for s in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))}
    env.pop("BASH_ENV", None); env.pop("ENV", None)
    if isolate_python:
        # The host application's venv is not a ticket's Python environment.
        inherited_venv = env.pop("VIRTUAL_ENV", None)
        excluded_bins = {str(Path(inherited_venv) / "bin")} if inherited_venv else set()
        if sys.prefix != sys.base_prefix:
            excluded_bins.add(str(Path(sys.prefix) / "bin"))
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", os.defpath).split(os.pathsep)
                                      if p.rstrip("/") not in excluded_bins)
        for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV_PROMPT", "PIP_TARGET", "PIP_PREFIX", "PIP_USER"):
            env.pop(key, None)
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
    def __init__(self, workspace: Workspace, environment: dict[str, str] | None = None, *, preview_bytes=MAX_PREVIEW_BYTES):
        self.workspace = workspace
        self.guard = CommandGuard(workspace)
        self.preview_bytes = preview_bytes
        self.cwd = workspace.root
        self.environment = shell_environment(environment, isolate_python=True)
        self.lock = asyncio.Lock()
        self.process: asyncio.subprocess.Process | None = None
        self.current: _Command | None = None
        self.tasks: list[asyncio.Task] = []
        self.input_write: int | None = None
        self.input_read: int | None = None
        self.control_write: int | None = None
        self.control_transport = None
        self.generation = 0
        self.close_lock = asyncio.Lock()

    async def start(self):
        try:
            await self._start()
        except BaseException:
            await self.close()
            raise

    async def _start(self):
        if self.process and self.process.returncode is None:
            return
        control_read, control_write = os.pipe()
        input_read, input_write = os.pipe()
        self.input_write, self.input_read, self.control_write = input_write, input_read, control_write
        try:
            self.process = await spawn_owned_process(
                "bash", "--noprofile", "--norc", "-s", cwd=self.workspace.root,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                pass_fds=(control_write, input_read), env=self.environment, start_new_session=True)
        except BaseException:
            for fd in (control_read, control_write, input_read, input_write):
                os.close(fd)
            self.input_write = self.input_read = self.control_write = None
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
        left = self.preview_bytes - len(command.data)
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

    async def execute(self, command: str, *, timeout=None,
                      callback: Callable[[bytes], None] | None = None, internal=False) -> ShellResult:
        async with self.lock:
            if not internal:
                decision = self.guard.validate(command, self.cwd)
                if decision.decision == "block":
                    raise AccessDenied(decision.reason)
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
        # The human-readable preview cannot limit a managed job's environment.
        chunks: list[bytes] = []
        await self.execute("env -0", internal=True, callback=chunks.append)
        data = b"".join(chunks)
        return {os.fsdecode(item.split(b"=",1)[0]): os.fsdecode(item.split(b"=",1)[1])
                for item in data.split(b"\0") if b"=" in item}

    async def send_input(self, text: str):
        if self.input_write is None or not self.current:
            raise AgileCodeError("No running foreground command is accepting input.")
        await asyncio.to_thread(os.write, self.input_write, text.encode())

    async def close(self):
        async with self.close_lock:
            await settle(self._close())

    async def _close(self):
        process = self.process
        if process:
            await stop_process_group(process)
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
                 artifact_root: Path, *, event_phase="development", log_limit_bytes=None, preview_bytes=MAX_PREVIEW_BYTES):
        self.workspace, self.store, self.events = workspace, store, events
        self.ticket_id, self.run_id = ticket_id, run_id
        self.artifact_root = artifact_root
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.artifacts = ArtifactService(store, self.artifact_root)
        self.jobs = JobService(store, self.artifacts, shell_supplier=lambda: [self])
        self.shell = PersistentShell(workspace, preview_bytes=preview_bytes)
        self.log_limit_bytes = log_limit_bytes
        self.background: dict[str, tuple[asyncio.subprocess.Process, asyncio.Task]] = {}
        self.foreground_task: asyncio.Task | None = None
        self.foreground_id: str | None = None
        self.event_phase = event_phase

    async def run(self, command: str, attempt_id: str | None, *, timeout=None, background=False) -> dict:
        cancellation_checkpoint()
        decision = self.shell.guard.validate(command, self.shell.cwd)
        if decision.decision == "block":
            raise AccessDenied(decision.reason)
        job, artifact = await self.jobs.register(self, command, attempt_id, background=background)
        job_id = job["id"]
        artifact_id, path = artifact["id"], Path(artifact["path"])
        scope = current_cancellation()
        launch_finished = asyncio.Event()
        owned_process = None
        release_scope = lambda: None
        if scope is not None:
            async def stop_owned_job():
                await launch_finished.wait()
                if job_id in self.background:
                    await self.stop(job_id)
                elif self.shell.process is not None and (self.foreground_id == job_id or self.shell.process is owned_process):
                    # Raw children of a foreground command share the persistent
                    # shell group even after that command returned.
                    await self.shell.close()
            release_scope = scope.own(stop_owned_job)
        if background:
            try:
                cancellation_checkpoint()
                env = await self.shell.exported_environment()
                cancellation_checkpoint()
                proc = await settle(spawn_owned_process("bash", "--noprofile", "--norc", "-c", command,
                    cwd=self.shell.cwd, env=env, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True), preserve_result=True)
                task = asyncio.create_task(self._background_job(job_id, artifact_id, path, proc, attempt_id))
                self.background[job_id] = proc, task
                task.add_done_callback(lambda _: release_scope())
                await self.jobs.started(job_id, self, environment=env)
                cancellation_checkpoint()
                return {"job_id": job_id, "status": "running", "artifact_id": artifact_id, "background": True}
            finally:
                launch_finished.set()
        launch_finished.set()
        cancellation_checkpoint()
        self.foreground_task = asyncio.current_task()
        self.foreground_id = job_id
        await self.jobs.started(job_id, self)
        total, truncated = 0, False
        handle = path.open("wb")
        def output(data: bytes):
            nonlocal total, truncated
            amount = len(data) if self.log_limit_bytes is None else max(0, min(len(data), self.log_limit_bytes-total))
            handle.write(data[:amount]); total += amount
            truncated |= amount < len(data)
            if scope is None or not scope.cancelled:
                self.events.emit(Event("ShellOutput", self.ticket_id, self.run_id, attempt_id, data={"job_id": job_id, "text": data.decode(errors="replace")}))
        try:
            result = await self.shell.execute(command, timeout=timeout, callback=output, internal=True)
            owned_process = self.shell.process
            if owned_process is None or not (live_group_members(owned_process.pid) - {owned_process.pid}):
                release_scope()
            await self.jobs.finished(job_id, self, "completed", exit_code=result.exit_code, cwd=result.cwd)
            return {"job_id": job_id, "artifact_id": artifact_id, "exit_code": result.exit_code, "cwd": result.cwd,
                    "output": result.output, "partial": truncated or result.truncated, "status": "completed"}
        except asyncio.CancelledError:
            await self.jobs.finished(job_id, self, "cancelled")
            raise
        except BaseException:
            await self.jobs.finished(job_id, self, "interrupted")
            raise
        finally:
            handle.close()
            try:
                await self._artifact_finished(artifact_id, path, capture_truncated=truncated)
            finally:
                self.foreground_task = None; self.foreground_id = None
                self.events.emit(Event("JobChanged", self.ticket_id, self.run_id, attempt_id, data={"job_id": job_id}))

    async def _artifact_finished(self, artifact_id: str, path: Path, *, capture_truncated=False):
        from agilecode.application.coordination import settle
        await settle(self.artifacts.finalize(artifact_id, capture_truncated=capture_truncated))

    async def _background_job(self, identity, artifact_id, path, proc, attempt_id):
        total = 0
        truncated = False
        try:
            with path.open("wb") as handle:
                async def pump(reader):
                    nonlocal total, truncated
                    while data := await reader.read(8192):
                        amount = len(data) if self.log_limit_bytes is None else max(0, min(len(data), self.log_limit_bytes-total))
                        handle.write(data[:amount]); total += amount
                        truncated |= amount < len(data)
                        scope = current_cancellation()
                        if scope is None or not scope.cancelled:
                            self.events.emit(Event("ShellOutput", self.ticket_id, self.run_id, attempt_id,
                                                   data={"job_id": identity, "text": data.decode(errors="replace")}))
                try:
                    await asyncio.gather(pump(proc.stdout), pump(proc.stderr))
                    code = await proc.wait()
                    await self.jobs.finished(identity, self, "completed", exit_code=code)
                except asyncio.CancelledError:
                    await self._kill(proc)
                    await self.jobs.finished(identity, self, "cancelled")
                    raise
                finally:
                    handle.flush()
        finally:
            await self._artifact_finished(artifact_id, path, capture_truncated=truncated)
            if truncated:
                await self.store.activity("JobOutputTruncated", {"job_id": identity, "limit_bytes": self.log_limit_bytes}, self.ticket_id)
            self.events.emit(Event("JobChanged", self.ticket_id, self.run_id, attempt_id, data={"job_id": identity}))

    def owns_job(self, job_id: str) -> bool:
        if job_id == self.foreground_id:
            return self.foreground_task is not None and not self.foreground_task.done()
        if job_id in self.background:
            proc, task = self.background[job_id]
            return proc.returncode is None or not task.done()
        return False

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
        await stop_process_group(proc)

    async def stop(self, job_id: str):
        if job_id == self.foreground_id and self.foreground_task:
            self.foreground_task.cancel()
            await self.shell.close()
        elif job_id in self.background:
            proc, task = self.background[job_id]
            if task.done() and proc.returncode is not None:
                return
            task.cancel()
            await self._kill(proc)
            await asyncio.gather(task, return_exceptions=True)
        else:
            raise AccessDenied("This job is not owned by the current ticket.")

    def has_live_jobs(self) -> bool:
        return self.foreground_id is not None or any(not task.done() for _, task in self.background.values())

    async def close(self):
        await self.shell.close()
        for proc, task in self.background.values():
            if task.done() and proc.returncode is not None:
                continue
            task.cancel()
            await self._kill(proc)
            await asyncio.gather(task, return_exceptions=True)
        self.background.clear()
