"""Typed tools, one authorization/execution path, and minimal structured outcomes."""
from __future__ import annotations

import asyncio
import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field, ValidationError
from agilecode.core.types import (DataModel, StepResult, StepContext, Step, Settings, ResultStatus,
                                  AgileCodeError, AccessDenied, Conflict, NeedsReview, Event, Events, uid, now)
from agilecode.core.context import requirement_ids
from agilecode.adapters.store import ProjectStore, encode, decode
from agilecode.adapters.workspace import Workspace, digest
from agilecode.adapters.git import GitRepository
from agilecode.adapters.search import SearchService
from agilecode.adapters.shell import ShellService
from agilecode.adapters.skills import SkillRegistry
from agilecode.adapters.mcp import McpService


class EmptyArgs(DataModel):
    pass
class PathArgs(DataModel):
    path: str
    scope: Literal["workspace", "project", "global"] = "workspace"
class ReadArgs(PathArgs):
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)
class ListArgs(DataModel):
    path: str = "."
    scope: Literal["workspace", "project", "global"] = "workspace"
    recursive: bool = False
    limit: int = Field(default=300, ge=1)
class SearchArgs(DataModel):
    query: str = Field(min_length=1)
    kind: Literal["text", "regex", "path", "symbol", "references"] = "text"
    scope: Literal["workspace", "project", "global"] = "workspace"
    path: str = "."
    limit: int = Field(default=100, ge=1)
    case_sensitive: bool = False
class WriteArgs(DataModel):
    path: str
    content: str
    expected_hash: str | None = None
    create: bool = False
class Replacement(DataModel):
    old: str = Field(min_length=1)
    new: str
    expected_count: int = Field(default=1, ge=1)
class EditArgs(DataModel):
    path: str
    expected_hash: str = Field(min_length=64, max_length=64)
    replacements: list[Replacement] = Field(min_length=1)
class DeleteArgs(DataModel):
    path: str
    expected_hash: str
class MoveArgs(DeleteArgs):
    destination: str
class CommandArgs(DataModel):
    command: str = Field(min_length=1)
    timeout_seconds: float | None = Field(default=None, ge=0)
    background: bool = False
class JobArgs(DataModel):
    job_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20000, ge=1)
class StopArgs(DataModel):
    job_id: str
class ArtifactArgs(DataModel):
    artifact_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20000, ge=1, le=100000)
class QuestionArgs(DataModel):
    question: str = Field(min_length=1)
class SkillArgs(DataModel):
    name: str

class CommitArgs(DataModel):
    message: str = Field(min_length=1)
class CheckArgs(DataModel):
    requirement: str = Field(pattern=r"^REQ-\d+$")
    status: Literal["PASS", "FAIL", "NOT_RUN", "NOT_APPLICABLE"]
    method: str = Field(min_length=1)
    source_execution_id: str | None = None
class ProgressArgs(DataModel):
    completed: list[str] = Field(default_factory=list)
    remaining: list[str] = Field(default_factory=list)
    notes: str = ""
class ResourceArgs(DataModel):
    server: str
    uri: str


@dataclass
class ToolSpec:
    name: str
    description: str
    category: str
    arguments: type[BaseModel] | dict
    handler: Callable[[dict, str], Awaitable[Any]]
    def schema(self):
        parameters = self.arguments if isinstance(self.arguments, dict) else self.arguments.model_json_schema()
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": parameters}}
    def validate(self, raw: str) -> dict:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise AgileCodeError("Tool arguments must be an object.")
        if isinstance(self.arguments, dict):
            Draft202012Validator(self.arguments).validate(data)
            return data
        return self.arguments.model_validate(data).model_dump(mode="json")


class ToolRegistry:
    def __init__(self):
        self.tools: dict[str, ToolSpec] = {}
    def register(self, spec: ToolSpec):
        if spec.name in self.tools:
            raise ValueError(f"Duplicate tool: {spec.name}")
        self.tools[spec.name] = spec
    def exposed(self) -> list[dict]:
        return [tool.schema() for tool in self.tools.values()]


@dataclass
class ExecutionResult:
    payload: dict
    step_result: StepResult | None = None


class ToolExecutor:
    def __init__(self, context: StepContext, step: Step, workspace: Workspace, git: GitRepository,
                 store: ProjectStore, shell: ShellService, skills: SkillRegistry, mcp: McpService,
                 settings: Callable[[], Settings], events: Events,
                 wait: Callable[..., Awaitable[str]], gate: Callable[[StepResult], Awaitable[None]],
                 commit: Callable[[str], Awaitable[dict]], write_lock: asyncio.Lock):
        self.context, self.step, self.workspace = context, step, workspace
        self.git, self.store, self.shell = git, store, shell
        self.skills, self.mcp, self.settings, self.events = skills, mcp, settings, events
        self.wait, self.gate, self.commit, self.write_lock = wait, gate, commit, write_lock
        self.search = SearchService(workspace, git, settings)
        self.registry = ToolRegistry()
        self.member = context.team.member(step.team_member_id)
        self._build_registry()

    def _permitted(self, name: str, category: str) -> bool:
        allowed = self.member.allowed_tools
        role_allowed = any(x in {"*", name, category} or fnmatch.fnmatch(name, x) for x in allowed)
        if category == "control":
            role_allowed = True
        if not role_allowed:
            return False
        if category in {"write", "destructive"} and self.step.kind != "implement":
            return False
        if category == "execute" and self.step.kind not in {"implement", "verify"}:
            return False
        if name == "record_check" and self.step.kind != "verify":
            return False
        if name == "final_commit" and (self.step.kind != "commit" or self.context.phase != "development"):
            return False
        return True

    def _add(self, name, description, category, arguments, handler):
        if self._permitted(name, category):
            self.registry.register(ToolSpec(name, description, category, arguments, handler))

    def _build_registry(self):
        self._add("list_files", "List files in an approved scope. Defaults to the ticket worktree.", "read", ListArgs, self._list)
        self._add("read_file", "Read UTF-8 source with its exact byte hash. Line ranges are one-based and inclusive.", "read", ReadArgs, self._read)
        self._add("code_search", "Search literal text, regex, filename/path, Python AST symbols, or textual reference candidates.", "search", SearchArgs, self._search)
        self._add("write_file", "Create an absent file (create=true) or replace using its expected_hash. Worktree only.", "write", WriteArgs, self._write)
        self._add("edit_file", "Apply exact replacements to the expected file hash. No fuzzy matching or partial writes.", "write", EditArgs, self._edit)
        self._add("move_path", "Move a regular file to an absent destination, using its expected hash.", "destructive", MoveArgs, self._move)
        self._add("delete_path", "Delete one regular file at its expected hash. Does not recursively delete directories.", "destructive", DeleteArgs, self._delete)
        self._add("run_command", "Run a guarded command in this ticket's REAL persistent Bash shell. background=true creates a managed job. No Git lifecycle mutations.", "execute", CommandArgs, self._command)
        self._add("read_job_output", "Read an owned job's real status, exit code and bounded output.", "read", JobArgs, self._job)
        self._add("stop_job", "Stop only a job belonging to this ticket.", "execute", StopArgs, self._stop)
        self._add("read_artifact", "Read a bounded part of an artifact owned by this ticket.", "read", ArtifactArgs, self._artifact)
        self._add("git_status", "Inspect the ticket's Git status.", "git", EmptyArgs, self._status)
        self._add("git_diff", "Inspect the COMPLETE eligible ticket change against the recorded starting commit.", "git", EmptyArgs, self._diff)
        self._add("git_log", "Inspect recent commits without changing refs.", "git", EmptyArgs, self._log)
        self._add("load_skill", "Load a skill explicitly enabled for this member; loading does not execute scripts.", "skills", SkillArgs, self._skill)
        self._add("update_progress", "Record plan progress without modifying the original plan or process state.", "control", ProgressArgs, self._progress)
        self._add("record_check", "Record a requirement check backed by a real tool result from THIS verification session. Sources are read_file, code_search, git_diff, run_command or read_job_output.", "evidence", CheckArgs, self._check)
        self._add("ask_user", "Pause this SAME session for essential clarification. Must be the only call in the response.", "control", QuestionArgs, self._question)
        self._add("finish_step", "Return status, summary and the completed step artifact. Must be the only call. Application gates decide whether to advance.", "control", StepResult, self._finish)
        self._add("final_commit", "Request the engine-owned final descriptive ticket commit after passed verification and review.", "commit", CommitArgs, self._commit)
        for alias, remote in self.mcp.tools.items():
            async def handler(args, execution_id, name=alias):
                result = await self.mcp.call(name, args)
                if result.get("isError"):
                    raise AgileCodeError("MCP tool returned an error: " + encode(result)[:8000])
                return result
            self._add(alias, remote["description"], "mcp", remote["schema"], handler)
        if self.mcp.connections:
            self._add("mcp_read_resource", "Read a resource from an explicitly configured MCP server.", "mcp", ResourceArgs, self._resource)

    def decision(self, name: str, category: str) -> str:
        settings = self.settings()
        rules = settings.tool_rules
        # A deny at either granularity wins; a name may override an ask default.
        if "deny" in {rules.get(name), rules.get(category)}:
            return "deny"
        if name in rules:
            return rules[name]
        if category in rules:
            return rules[category]
        if category in {"read", "search", "git", "skills", "control", "evidence"}:
            return "allow"
        return "allow" if settings.autonomy == "full" else "ask"

    async def execute(self, call: dict, request_id: str) -> ExecutionResult:
        name, raw = call["function"]["name"], call["function"].get("arguments", "{}")
        existing = await self.store.one("SELECT * FROM tools WHERE request_id=? AND call_id=?", (request_id, call["id"]))
        if existing and existing["result"]:
            payload = decode(existing["result"])
            result = StepResult.model_validate(payload["data"]) if name == "finish_step" and payload.get("status") == "ok" else None
            return ExecutionResult(payload, result)
        if existing and existing["status"] in {"running", "unknown"}:
            raise NeedsReview("A previous execution has an uncertain outcome. Resolve it in Recovery before resuming; it will not be replayed automatically.")
        spec = self.registry.tools.get(name)
        identity = existing["id"] if existing else uid()
        if not existing:
            await self.store.insert("tools", {"id": identity, "session_id": self.context.attempt["session_id"], "request_id": request_id,
                "call_id": call["id"], "name": name, "args": raw, "status": "pending", "category": spec.category if spec else "unknown", "created": now()})
        try:
            if not spec:
                raise AccessDenied(f"Tool {name!r} is not exposed to this member and step.")
            args = spec.validate(raw)
            decision = self.decision(name, spec.category)
            if decision == "deny":
                raise AccessDenied("This tool is disabled by the user.")
            guard_review = False
            if name == "run_command":
                decision_ = self.shell.shell.guard.validate(args["command"], self.shell.shell.cwd)
                if decision_.decision == "deny":
                    raise AccessDenied(decision_.reason)
                guard_review = decision_.decision == "ask"
            # Binding records requested hashes and exact cwd; after a wait the tool
            # validates those preconditions again before any effect.
            binding = encode({"name": name, "args": args, "cwd": str(self.shell.shell.cwd),
                              "policy": self.settings().model_dump(mode="json").get("tool_rules"),
                              "autonomy": self.settings().autonomy})
            if decision == "ask" or guard_review:
                answer = await self.wait(identity, "approval", f"Allow {name}?\n{encode(args)}", binding)
                if answer != "approve":
                    raise AccessDenied("The user denied this operation.")
                current_binding = encode({"name": name, "args": args, "cwd": str(self.shell.shell.cwd),
                              "policy": self.settings().model_dump(mode="json").get("tool_rules"),
                              "autonomy": self.settings().autonomy})
                if current_binding != binding or self.decision(name, spec.category) == "deny":
                    raise Conflict("The approval is stale because cwd or policy changed.")
            await self.store.update("tools", identity, status="running")
            self.events.emit(Event("ToolStateChanged", self.context.ticket["id"], self.context.run["id"], self.context.attempt["id"],
                                   self.context.attempt["session_id"], {"tool_id": identity, "name": name, "status": "running"}))
            if spec.category in {"write", "destructive", "commit"} and self.shell.has_live_jobs():
                raise AgileCodeError("Stop owned background jobs before changing or committing source.")
            data = await spec.handler(args, identity)
            result = data if isinstance(data, StepResult) else None
            data = data.model_dump(mode="json") if result else data
            payload = {"status": "ok", "_execution_id": identity, "data": data}
        except asyncio.CancelledError:
            current = await self.store.get("tools", identity)
            # Approval/question waits have not executed the proposed effect.
            if current["status"] != "waiting":
                uncertain = current["category"] in {"write", "destructive", "execute", "mcp", "commit"}
                await self.store.update("tools", identity, status="unknown" if uncertain else "pending")
            raise
        except NeedsReview:
            raise
        except Exception as exc:
            result = None
            payload = {"status": "error", "_execution_id": identity,
                       "error": {"code": getattr(exc, "code", "invalid_tool"), "message": str(exc)[:6000]}}
        await self.store.update("tools", identity, status="completed", result=encode(payload), finished=now())
        self.events.emit(Event("ToolStateChanged", self.context.ticket["id"], self.context.run["id"], self.context.attempt["id"],
                               self.context.attempt["session_id"], {"tool_id": identity, "name": name, "status": payload["status"]}))
        return ExecutionResult(payload, result)

    async def _list(self, a, _): return await asyncio.to_thread(self.workspace.list_files, **a)
    async def _read(self, a, _): return await asyncio.to_thread(self.workspace.read, **a)
    async def _search(self, a, _): return await self.search.search(**a)
    async def _write(self, a, _):
        async with self.write_lock: return await asyncio.to_thread(self.workspace.write, **a)
    async def _edit(self, a, _):
        async with self.write_lock: return await asyncio.to_thread(self.workspace.edit, **a)
    async def _delete(self, a, _):
        async with self.write_lock: return await asyncio.to_thread(self.workspace.delete, **a)
    async def _move(self, a, _):
        async with self.write_lock: return await asyncio.to_thread(self.workspace.move, **a)
    async def _command(self, a, _):
        async with self.write_lock:
            timeout = a["timeout_seconds"]
            if timeout is None:
                timeout = self.settings().shell_command_timeout_seconds
            return await self.shell.run(a["command"], self.context.attempt["id"], timeout=timeout, background=a["background"], approved_unknown=True)
    async def _stop(self, a, _):
        await self.shell.stop(a["job_id"]); return {"stopped": a["job_id"]}
    async def _job(self, a, _):
        job = await self.store.get("jobs", a["job_id"])
        if job["ticket_id"] != self.context.ticket["id"]:
            raise AccessDenied("Job belongs to another ticket.")
        artifact = await self._artifact({"artifact_id": job["artifact_id"], "offset": a["offset"], "limit": a["limit"]}, "")
        return {**job, "output": artifact["content"], "partial": artifact["partial"]}
    async def _artifact(self, a, _):
        artifact = await self.store.get("artifacts", a["artifact_id"])
        if artifact["ticket_id"] != self.context.ticket["id"]:
            raise AccessDenied("Artifact belongs to another ticket.")
        def read():
            with Path(artifact["path"]).open("rb") as f:
                f.seek(a["offset"]); data = f.read(a["limit"])
            return data
        data = await asyncio.to_thread(read)
        size = Path(artifact["path"]).stat().st_size
        return {"content": data.decode(errors="replace"), "offset": a["offset"], "bytes": len(data), "total_bytes": size,
                "partial": a["offset"]+len(data)<size or a["offset"]>0, "complete": bool(artifact["complete"])}
    async def _status(self, a, _): return {"status": await self.git.status(self.workspace.root)}
    async def _diff(self, a, _):
        text = await self.git.diff(self.workspace.root, self.context.extra.get("diff_base", self.context.run["base_oid"]))
        # Full diff is retained as an artifact when it exceeds the user's configured inline limit.
        inline_chars = self.settings().diff_inline_chars
        if inline_chars is not None and len(text) > inline_chars:
            identity = uid(); path = self.shell.artifact_root / f"{identity}.diff"
            await asyncio.to_thread(path.write_text, text, encoding="utf-8")
            await self.store.insert("artifacts", {"id": identity, "ticket_id": self.context.ticket["id"], "name": "Complete diff",
                "path": str(path), "size": len(text.encode()), "hash": digest(text.encode()), "complete": 1, "created": now()})
            return {"diff": text[:inline_chars], "partial": True, "artifact_id": identity}
        return {"diff": text, "partial": False}
    async def _log(self, a, _):
        return {"log": (await self.git.run("log", "-20", "--oneline", cwd=self.workspace.root)).text}
    async def _skill(self, a, _): return await asyncio.to_thread(self.skills.load, a["name"], self.member.skills)
    async def _progress(self, a, _):
        await self.store.activity("ProgressUpdated", {"attempt_id": self.context.attempt["id"], **a}, self.context.ticket["id"])
        return a
    async def _question(self, a, identity):
        answer = await self.wait(identity, "question", a["question"], encode(a))
        return {"answer": answer}
    async def _finish(self, a, _):
        result = StepResult.model_validate(a)
        await self.gate(result)
        return result
    async def _commit(self, a, _):
        async with self.write_lock: return await self.commit(a["message"])
    async def _resource(self, a, _): return await self.mcp.read_resource(**a)

    async def _check(self, a, _):
        requirements = []
        for key, output in self.context.outputs.items():
            if "requirements" in key or any(s.id == key and s.kind == "requirements" for s in self.context.process.steps):
                requirements += requirement_ids(output.output)
        if a["requirement"] not in requirements:
            raise AgileCodeError("That requirement is not in the recorded requirements artifact.")
        source = None
        if a["source_execution_id"]:
            source = await self.store.get("tools", a["source_execution_id"])
            if source["session_id"] != self.context.attempt["session_id"] or source["status"] != "completed":
                raise AgileCodeError("Evidence must cite a completed tool from this verification session.")
            result = decode(source["result"], {})
            if result.get("status") != "ok" or source["name"] not in {"read_file", "code_search", "git_diff", "run_command", "read_job_output"}:
                raise AgileCodeError("The source is not a successful inspection or command result.")
            if a["status"] == "PASS" and source["name"] in {"run_command", "read_job_output"}:
                data = result.get("data", {})
                if data.get("exit_code") != 0 or data.get("status") != "completed":
                    raise AgileCodeError("A passing check requires an observed completed command with exit code zero.")
        elif a["status"] == "PASS":
            raise AgileCodeError("PASS requires a real source execution ID.")
        fingerprint = await self.git.snapshot_tree(self.workspace.root)
        identity = uid()
        await self.store.insert("evidence", {"id": identity, "attempt_id": self.context.attempt["id"], "requirement": a["requirement"],
            "status": a["status"], "method": a["method"], "source_tool": a["source_execution_id"], "fingerprint": fingerprint, "created": now()})
        return {"evidence_id": identity, **a, "fingerprint": fingerprint}
