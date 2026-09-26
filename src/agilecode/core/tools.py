"""Concrete tool objects and one small manager for model tool calls."""
from __future__ import annotations

import asyncio
import fnmatch
import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

from agilecode.core.types import (
    DataModel, StepResult, StepContext, Step, Settings, AgileCodeError, AccessDenied,
    Conflict, NeedsReview, Event, Events, uid, now,
)
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
    limit: int = Field(default=300, ge=1, le=1000)


class SearchArgs(DataModel):
    query: str = Field(min_length=1, max_length=2000)
    kind: Literal["text", "regex", "path", "symbol", "references"] = "text"
    scope: Literal["workspace", "project", "global"] = "workspace"
    path: str = "."
    limit: int = Field(default=100, ge=1, le=500)
    case_sensitive: bool = False


class WriteArgs(DataModel):
    path: str
    content: str = Field(max_length=4_000_000)
    expected_hash: str | None = None
    create: bool = False


class Replacement(DataModel):
    old: str = Field(min_length=1)
    new: str
    expected_count: int = Field(default=1, ge=1, le=10000)


class EditArgs(DataModel):
    path: str
    expected_hash: str = Field(min_length=64, max_length=64)
    replacements: list[Replacement] = Field(min_length=1, max_length=100)


class DeleteArgs(DataModel):
    path: str
    expected_hash: str


class MoveArgs(DeleteArgs):
    destination: str


class CommandArgs(DataModel):
    command: str = Field(min_length=1, max_length=100000)
    background: bool = False


class JobArgs(DataModel):
    job_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20000, ge=1, le=100000)


class StopArgs(DataModel):
    job_id: str


class ArtifactArgs(DataModel):
    artifact_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20000, ge=1, le=100000)


class QuestionArgs(DataModel):
    question: str = Field(min_length=1, max_length=8000)


class SkillArgs(DataModel):
    name: str


class CommitArgs(DataModel):
    message: str = Field(min_length=10, max_length=12000)


class CheckArgs(DataModel):
    requirement: str = Field(pattern=r"^REQ-\d+$")
    status: Literal["PASS", "FAIL", "NOT_RUN", "NOT_APPLICABLE"]
    method: str = Field(min_length=1, max_length=8000)
    source_execution_id: str | None = None


class ProgressArgs(DataModel):
    completed: list[str] = Field(default_factory=list)
    remaining: list[str] = Field(default_factory=list)
    notes: str = ""


class ResourceArgs(DataModel):
    server: str
    uri: str


class CommandPolicy:
    """Resolve permission for one shell command. Containment is intentionally separate."""

    def __init__(self, settings: Callable[[], Settings]):
        self.settings = settings

    def decide(self, command: str) -> str:
        settings = self.settings()
        tool_rule = settings.tool_rules.get("run_command")
        if tool_rule == "deny":
            return "deny"
        normalized = command.strip()
        for rule in settings.command_rules:
            if fnmatch.fnmatchcase(normalized, rule.pattern):
                return rule.action
        if tool_rule in {"allow", "ask"}:
            return tool_rule
        return "allow" if settings.autonomy == "full" else "ask"

    def snapshot(self) -> dict:
        settings = self.settings()
        return {
            "autonomy": settings.autonomy,
            "run_command": settings.tool_rules.get("run_command"),
            "command_rules": [rule.model_dump(mode="json") for rule in settings.command_rules],
        }


class Tool:
    """One model-callable operation."""

    name = ""
    description = ""
    arguments: type[BaseModel] | dict = EmptyArgs
    requires_approval = False
    uncertain_on_interrupt = False

    def schema(self) -> dict:
        parameters = self.arguments if isinstance(self.arguments, dict) else self.arguments.model_json_schema()
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }

    def validate(self, raw: str) -> dict:
        if len(raw) > 4_000_000:
            raise AgileCodeError("Tool arguments exceed the supported size.")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise AgileCodeError("Tool arguments must be an object.")
        if isinstance(self.arguments, dict):
            Draft202012Validator(self.arguments).validate(data)
            return data
        return self.arguments.model_validate(data).model_dump(mode="json")

    def available(self, step: Step, phase: str) -> bool:
        return True

    def permission(self, settings: Settings, args: dict) -> str:
        explicit = settings.tool_rules.get(self.name)
        if explicit:
            return explicit
        if self.requires_approval and settings.autonomy != "full":
            return "ask"
        return "allow"

    def approval_context(self, settings: Settings, args: dict) -> dict:
        return {
            "autonomy": settings.autonomy,
            "tool_rule": settings.tool_rules.get(self.name),
        }

    @property
    def record_category(self) -> str:
        # Kept only because the existing SQLite schema records this field.
        return "effect" if self.uncertain_on_interrupt else "read"

    async def execute(self, args: dict, execution_id: str) -> Any:
        raise NotImplementedError


def _ensure_no_live_jobs(shell: ShellService) -> None:
    if shell.has_live_jobs():
        raise AgileCodeError("Stop owned background jobs before changing or committing source.")


class ListFilesTool(Tool):
    name = "list_files"
    description = "List files in an approved scope. Defaults to the ticket worktree."
    arguments = ListArgs

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    async def execute(self, args, execution_id):
        return await asyncio.to_thread(self.workspace.list_files, **args)


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read UTF-8 source with its exact byte hash. Line ranges are one-based and inclusive."
    arguments = ReadArgs

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    async def execute(self, args, execution_id):
        return await asyncio.to_thread(self.workspace.read, **args)


class CodeSearchTool(Tool):
    name = "code_search"
    description = "Search literal text, regex, filename/path, Python AST symbols, or textual reference candidates."
    arguments = SearchArgs

    def __init__(self, search: SearchService):
        self.search = search

    async def execute(self, args, execution_id):
        return await self.search.search(**args)


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create an absent file (create=true) or replace using its expected_hash. Worktree only."
    arguments = WriteArgs
    requires_approval = True
    uncertain_on_interrupt = True

    def __init__(self, workspace: Workspace, shell: ShellService, write_lock: asyncio.Lock):
        self.workspace, self.shell, self.write_lock = workspace, shell, write_lock

    def available(self, step, phase):
        return step.kind == "implement"

    async def execute(self, args, execution_id):
        _ensure_no_live_jobs(self.shell)
        async with self.write_lock:
            return await asyncio.to_thread(self.workspace.write, **args)


class EditFileTool(Tool):
    name = "edit_file"
    description = "Apply exact replacements to the expected file hash. No fuzzy matching or partial writes."
    arguments = EditArgs
    requires_approval = True
    uncertain_on_interrupt = True

    def __init__(self, workspace: Workspace, shell: ShellService, write_lock: asyncio.Lock):
        self.workspace, self.shell, self.write_lock = workspace, shell, write_lock

    def available(self, step, phase):
        return step.kind == "implement"

    async def execute(self, args, execution_id):
        _ensure_no_live_jobs(self.shell)
        async with self.write_lock:
            return await asyncio.to_thread(self.workspace.edit, **args)


class MovePathTool(Tool):
    name = "move_path"
    description = "Move a regular file to an absent destination, using its expected hash."
    arguments = MoveArgs
    requires_approval = True
    uncertain_on_interrupt = True

    def __init__(self, workspace: Workspace, shell: ShellService, write_lock: asyncio.Lock):
        self.workspace, self.shell, self.write_lock = workspace, shell, write_lock

    def available(self, step, phase):
        return step.kind == "implement"

    async def execute(self, args, execution_id):
        _ensure_no_live_jobs(self.shell)
        async with self.write_lock:
            return await asyncio.to_thread(self.workspace.move, **args)


class DeletePathTool(Tool):
    name = "delete_path"
    description = "Delete one regular file at its expected hash. Does not recursively delete directories."
    arguments = DeleteArgs
    requires_approval = True
    uncertain_on_interrupt = True

    def __init__(self, workspace: Workspace, shell: ShellService, write_lock: asyncio.Lock):
        self.workspace, self.shell, self.write_lock = workspace, shell, write_lock

    def available(self, step, phase):
        return step.kind == "implement"

    async def execute(self, args, execution_id):
        _ensure_no_live_jobs(self.shell)
        async with self.write_lock:
            return await asyncio.to_thread(self.workspace.delete, **args)


class RunCommandTool(Tool):
    name = "run_command"
    description = "Run a guarded command in this ticket's REAL persistent Bash shell. background=true creates a managed job. No Git lifecycle mutations."
    arguments = CommandArgs
    uncertain_on_interrupt = True

    def __init__(self, shell: ShellService, attempt_id: str, settings: Callable[[], Settings], write_lock: asyncio.Lock):
        self.shell, self.attempt_id, self.settings, self.write_lock = shell, attempt_id, settings, write_lock
        self.policy = CommandPolicy(settings)

    def available(self, step, phase):
        return step.kind in {"implement", "verify"}

    def permission(self, settings, args):
        return self.policy.decide(args["command"])

    def approval_context(self, settings, args):
        return {**self.policy.snapshot(), "cwd": str(self.shell.shell.cwd)}

    async def execute(self, args, execution_id):
        decision = self.shell.shell.guard.validate(args["command"], self.shell.shell.cwd)
        if decision.decision == "deny":
            raise AccessDenied(decision.reason)
        async with self.write_lock:
            return await self.shell.run(
                args["command"],
                self.attempt_id,
                timeout=self.settings().shell_command_timeout_seconds,
                background=args["background"],
            )


class ReadArtifactTool(Tool):
    name = "read_artifact"
    description = "Read a bounded part of an artifact owned by this ticket."
    arguments = ArtifactArgs

    def __init__(self, store: ProjectStore, ticket_id: str):
        self.store, self.ticket_id = store, ticket_id

    async def execute(self, args, execution_id):
        artifact = await self.store.get("artifacts", args["artifact_id"])
        if artifact["ticket_id"] != self.ticket_id:
            raise AccessDenied("Artifact belongs to another ticket.")

        def read():
            with Path(artifact["path"]).open("rb") as handle:
                handle.seek(args["offset"])
                return handle.read(args["limit"])

        data = await asyncio.to_thread(read)
        size = Path(artifact["path"]).stat().st_size
        return {
            "content": data.decode(errors="replace"),
            "offset": args["offset"],
            "bytes": len(data),
            "total_bytes": size,
            "partial": args["offset"] + len(data) < size or args["offset"] > 0,
            "complete": bool(artifact["complete"]),
        }


class ReadJobOutputTool(Tool):
    name = "read_job_output"
    description = "Read an owned job's real status, exit code and bounded output."
    arguments = JobArgs

    def __init__(self, store: ProjectStore, ticket_id: str, artifacts: ReadArtifactTool):
        self.store, self.ticket_id, self.artifacts = store, ticket_id, artifacts

    async def execute(self, args, execution_id):
        job = await self.store.get("jobs", args["job_id"])
        if job["ticket_id"] != self.ticket_id:
            raise AccessDenied("Job belongs to another ticket.")
        artifact = await self.artifacts.execute(
            {"artifact_id": job["artifact_id"], "offset": args["offset"], "limit": args["limit"]},
            execution_id,
        )
        return {**job, "output": artifact["content"], "partial": artifact["partial"]}


class StopJobTool(Tool):
    name = "stop_job"
    description = "Stop only a job belonging to this ticket."
    arguments = StopArgs
    requires_approval = True
    uncertain_on_interrupt = True

    def __init__(self, shell: ShellService):
        self.shell = shell

    def available(self, step, phase):
        return step.kind in {"implement", "verify"}

    async def execute(self, args, execution_id):
        await self.shell.stop(args["job_id"])
        return {"stopped": args["job_id"]}


class GitStatusTool(Tool):
    name = "git_status"
    description = "Inspect the ticket's Git status."
    arguments = EmptyArgs

    def __init__(self, git: GitRepository, workspace: Workspace):
        self.git, self.workspace = git, workspace

    async def execute(self, args, execution_id):
        return {"status": await self.git.status(self.workspace.root)}


class GitDiffTool(Tool):
    name = "git_diff"
    description = "Inspect the COMPLETE eligible ticket change against the recorded starting commit."
    arguments = EmptyArgs

    def __init__(self, git: GitRepository, workspace: Workspace, base_oid: str, artifact_root: Path, store: ProjectStore, ticket_id: str):
        self.git, self.workspace, self.base_oid = git, workspace, base_oid
        self.artifact_root, self.store, self.ticket_id = artifact_root, store, ticket_id

    async def execute(self, args, execution_id):
        text = await self.git.diff(self.workspace.root, self.base_oid)
        if len(text.encode()) > 100000:
            identity = uid()
            path = self.artifact_root / f"{identity}.diff"
            await asyncio.to_thread(path.write_text, text, encoding="utf-8")
            await self.store.insert(
                "artifacts",
                {
                    "id": identity,
                    "ticket_id": self.ticket_id,
                    "name": "Complete diff",
                    "path": str(path),
                    "size": len(text.encode()),
                    "hash": digest(text.encode()),
                    "complete": 1,
                    "created": now(),
                },
            )
            return {"diff": text[:100000], "partial": True, "artifact_id": identity}
        return {"diff": text, "partial": False}


class GitLogTool(Tool):
    name = "git_log"
    description = "Inspect recent commits without changing refs."
    arguments = EmptyArgs

    def __init__(self, git: GitRepository, workspace: Workspace):
        self.git, self.workspace = git, workspace

    async def execute(self, args, execution_id):
        return {"log": (await self.git.run("log", "-20", "--oneline", cwd=self.workspace.root)).text}


class LoadSkillTool(Tool):
    name = "load_skill"
    description = "Load an explicitly enabled skill."
    arguments = SkillArgs

    def __init__(self, skills: SkillRegistry, allowed_skills: list[str]):
        self.skills, self.allowed_skills = skills, allowed_skills

    async def execute(self, args, execution_id):
        return await asyncio.to_thread(self.skills.load, args["name"], self.allowed_skills)


class UpdateProgressTool(Tool):
    name = "update_progress"
    description = "Record plan progress without modifying the original plan or process state."
    arguments = ProgressArgs

    def __init__(self, store: ProjectStore, ticket_id: str, attempt_id: str):
        self.store, self.ticket_id, self.attempt_id = store, ticket_id, attempt_id

    async def execute(self, args, execution_id):
        await self.store.activity("ProgressUpdated", {"attempt_id": self.attempt_id, **args}, self.ticket_id)
        return args


class AskUserTool(Tool):
    name = "ask_user"
    description = "Pause this SAME session for essential clarification. Must be the only call in the response."
    arguments = QuestionArgs

    def __init__(self, wait: Callable[..., Awaitable[str]]):
        self.wait = wait

    async def execute(self, args, execution_id):
        answer = await self.wait(execution_id, "question", args["question"], encode(args))
        return {"answer": answer}


class FinishStepTool(Tool):
    name = "finish_step"
    description = "Return status, summary and the completed step artifact. Must be the only call. Application gates decide whether to advance."
    arguments = StepResult

    def __init__(self, gate: Callable[[StepResult], Awaitable[None]]):
        self.gate = gate

    async def execute(self, args, execution_id):
        result = StepResult.model_validate(args)
        await self.gate(result)
        return result


class FinalCommitTool(Tool):
    name = "final_commit"
    description = "Request the engine-owned final descriptive ticket commit after passed verification and review."
    arguments = CommitArgs
    requires_approval = True
    uncertain_on_interrupt = True

    def __init__(self, commit: Callable[[str], Awaitable[dict]], shell: ShellService, write_lock: asyncio.Lock):
        self.commit, self.shell, self.write_lock = commit, shell, write_lock

    def available(self, step, phase):
        return step.kind == "commit" and phase == "development"

    async def execute(self, args, execution_id):
        _ensure_no_live_jobs(self.shell)
        async with self.write_lock:
            return await self.commit(args["message"])


class RecordCheckTool(Tool):
    name = "record_check"
    description = "Record a requirement check backed by a real source tool result from this verification session."
    arguments = CheckArgs

    def __init__(self, context: StepContext, store: ProjectStore, git: GitRepository, workspace: Workspace):
        self.context, self.store, self.git, self.workspace = context, store, git, workspace

    def available(self, step, phase):
        return step.kind == "verify"

    async def execute(self, args, execution_id):
        requirements = []
        for key, output in self.context.outputs.items():
            if "requirements" in key or any(
                step.id == key and step.kind == "requirements" for step in self.context.process.steps
            ):
                requirements += requirement_ids(output.output)
        if args["requirement"] not in requirements:
            raise AgileCodeError("That requirement is not in the recorded requirements artifact.")

        if args["source_execution_id"]:
            source = await self.store.get("tools", args["source_execution_id"])
            if source["session_id"] != self.context.attempt["session_id"] or source["status"] != "completed":
                raise AgileCodeError("Evidence must cite a completed tool from this verification session.")
            result = decode(source["result"], {})
            if result.get("status") != "ok" or source["name"] not in {
                "read_file", "code_search", "git_diff", "run_command", "read_job_output"
            }:
                raise AgileCodeError("The source is not a successful inspection or command result.")
            if args["status"] == "PASS" and source["name"] in {"run_command", "read_job_output"}:
                data = result.get("data", {})
                if data.get("exit_code") != 0 or data.get("status") != "completed":
                    raise AgileCodeError("A passing check requires an observed completed command with exit code zero.")
        elif args["status"] == "PASS":
            raise AgileCodeError("PASS requires a real source execution ID.")

        fingerprint = await self.git.snapshot_tree(self.workspace.root)
        identity = uid()
        await self.store.insert(
            "evidence",
            {
                "id": identity,
                "attempt_id": self.context.attempt["id"],
                "requirement": args["requirement"],
                "status": args["status"],
                "method": args["method"],
                "source_tool": args["source_execution_id"],
                "fingerprint": fingerprint,
                "created": now(),
            },
        )
        return {"evidence_id": identity, **args, "fingerprint": fingerprint}


class MCPTool(Tool):
    requires_approval = True
    uncertain_on_interrupt = True

    def __init__(self, mcp: McpService, alias: str, definition: dict):
        self.mcp = mcp
        self.name = alias
        self.description = definition["description"]
        self.arguments = definition["schema"]

    async def execute(self, args, execution_id):
        result = await self.mcp.call(self.name, args)
        if result.get("isError"):
            raise AgileCodeError("MCP tool returned an error: " + encode(result)[:8000])
        return result


class MCPResourceTool(Tool):
    name = "mcp_read_resource"
    description = "Read a resource from an explicitly configured MCP server."
    arguments = ResourceArgs
    requires_approval = True
    uncertain_on_interrupt = True

    def __init__(self, mcp: McpService):
        self.mcp = mcp

    async def execute(self, args, execution_id):
        return await self.mcp.read_resource(**args)


class ToolManager:
    """Expose named Tool objects to a model and persist/dispatch its tool calls."""

    def __init__(
        self,
        tools: list[Tool],
        allowed_tools: list[str],
        store: ProjectStore,
        session_id: str,
        settings: Callable[[], Settings],
        *,
        wait: Callable[..., Awaitable[str]] | None = None,
        events: Events | None = None,
        ticket_id: str | None = None,
        run_id: str | None = None,
        attempt_id: str | None = None,
    ):
        duplicates = [tool.name for tool in tools if sum(other.name == tool.name for other in tools) > 1]
        if duplicates:
            raise ValueError("Duplicate tool names: " + ", ".join(sorted(set(duplicates))))
        allowed = set(allowed_tools)
        self.tools = {tool.name: tool for tool in tools if tool.name in allowed}
        self.store, self.session_id, self.settings = store, session_id, settings
        self.wait, self.events = wait, events
        self.ticket_id, self.run_id, self.attempt_id = ticket_id, run_id, attempt_id

    def schemas(self) -> list[dict]:
        return [tool.schema() for tool in self.tools.values()]

    def _binding(self, tool: Tool, args: dict) -> str:
        settings = self.settings()
        return encode({
            "name": tool.name,
            "args": args,
            "policy": tool.approval_context(settings, args),
        })

    async def execute(self, call: dict, request_id: str) -> "ExecutionResult":
        name = call["function"]["name"]
        raw = call["function"].get("arguments", "{}")
        existing = await self.store.one(
            "SELECT * FROM tools WHERE request_id=? AND call_id=?",
            (request_id, call["id"]),
        )
        if existing and existing["result"]:
            payload = decode(existing["result"])
            result = (
                StepResult.model_validate(payload["data"])
                if name == "finish_step" and payload.get("status") == "ok"
                else None
            )
            return ExecutionResult(payload, result)
        if existing and existing["status"] in {"running", "unknown"}:
            raise NeedsReview(
                "A previous execution has an uncertain outcome. Resolve it in Recovery before resuming; "
                "it will not be replayed automatically."
            )

        tool = self.tools.get(name)
        identity = existing["id"] if existing else uid()
        if not existing:
            await self.store.insert(
                "tools",
                {
                    "id": identity,
                    "session_id": self.session_id,
                    "request_id": request_id,
                    "call_id": call["id"],
                    "name": name,
                    "args": raw,
                    "status": "pending",
                    "category": tool.record_category if tool else "read",
                    "created": now(),
                },
            )
        try:
            if not tool:
                raise AccessDenied(f"Tool {name!r} is not exposed to this model.")
            args = tool.validate(raw)
            decision = tool.permission(self.settings(), args)
            if decision == "deny":
                raise AccessDenied("This tool call is disabled by the user.")
            if decision == "ask":
                if self.wait is None:
                    raise AccessDenied("This tool call requires approval, but this surface cannot collect one.")
                binding = self._binding(tool, args)
                answer = await self.wait(identity, "approval", f"Allow {name}?\n{encode(args)}", binding)
                if answer != "approve":
                    raise AccessDenied("The user denied this operation.")
                if self._binding(tool, args) != binding or tool.permission(self.settings(), args) == "deny":
                    raise Conflict("The approval is stale because the command/tool policy changed.")

            await self.store.update("tools", identity, status="running")
            if self.events and self.ticket_id:
                self.events.emit(
                    Event(
                        "ToolStateChanged",
                        self.ticket_id,
                        self.run_id,
                        self.attempt_id,
                        self.session_id,
                        {"tool_id": identity, "name": name, "status": "running"},
                    )
                )
            data = await tool.execute(args, identity)
            result = data if isinstance(data, StepResult) else None
            data = data.model_dump(mode="json") if result else data
            payload = {"status": "ok", "_execution_id": identity, "data": data}
        except asyncio.CancelledError:
            current = await self.store.get("tools", identity)
            if current["status"] != "waiting":
                uncertain = bool(tool and tool.uncertain_on_interrupt)
                await self.store.update("tools", identity, status="unknown" if uncertain else "pending")
            raise
        except NeedsReview:
            raise
        except Exception as exc:
            result = None
            payload = {
                "status": "error",
                "_execution_id": identity,
                "error": {
                    "code": getattr(exc, "code", "invalid_tool"),
                    "message": str(exc)[:6000],
                },
            }

        await self.store.update(
            "tools",
            identity,
            status="completed",
            result=encode(payload),
            finished=now(),
        )
        if self.events and self.ticket_id:
            self.events.emit(
                Event(
                    "ToolStateChanged",
                    self.ticket_id,
                    self.run_id,
                    self.attempt_id,
                    self.session_id,
                    {"tool_id": identity, "name": name, "status": payload["status"]},
                )
            )
        return ExecutionResult(payload, result)


class ExecutionResult:
    def __init__(self, payload: dict, step_result: StepResult | None = None):
        self.payload = payload
        self.step_result = step_result
