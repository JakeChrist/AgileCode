"""Tool objects plus one manager for model registration, dispatch and durable call records."""
from __future__ import annotations

import asyncio
import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

from agilecode.core.context import requirement_ids
from agilecode.core.types import (
    AccessDenied,
    AgileCodeError,
    Conflict,
    DataModel,
    Event,
    Events,
    NeedsReview,
    Settings,
    StepContext,
    StepResult,
    now,
    uid,
)
from agilecode.adapters.git import GitRepository
from agilecode.adapters.mcp import McpService
from agilecode.adapters.search import SearchService
from agilecode.adapters.shell import ShellService
from agilecode.adapters.skills import SkillRegistry
from agilecode.adapters.store import ProjectStore, decode, encode
from agilecode.adapters.workspace import Workspace, digest


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


class CreateTicketArgs(DataModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=100_000)


class CommandPolicy:
    """Decide command approval without performing containment checks."""

    def __init__(self, settings: Callable[[], Settings]):
        self.settings = settings

    def decide(self, command: str, settings: Settings | None = None) -> str:
        settings = settings if settings is not None else self.settings()
        # An explicit tool-level deny disables command execution entirely.
        tool_rule = settings.tool_rules.get("run_command")
        if tool_rule == "deny":
            return "deny"

        command = command.strip()
        for rule in settings.command_rules:
            if fnmatch.fnmatchcase(command, rule.pattern):
                return rule.action

        if tool_rule in {"allow", "ask"}:
            return tool_rule
        return "allow" if settings.autonomy == "full" else "ask"

    def binding(self, command: str, cwd: str, settings: Settings | None = None) -> str:
        settings = settings if settings is not None else self.settings()
        return encode({
            "tool": "run_command",
            "command": command,
            "cwd": cwd,
            "tool_rule": settings.tool_rules.get("run_command"),
            "command_rules": [rule.model_dump(mode="json") for rule in settings.command_rules],
            "autonomy": settings.autonomy,
        })


class Tool:
    """One model-callable capability.

    Subclasses own their operation. ToolManager only exposes named tools, validates
    and records calls, applies the tool's permission decision, and dispatches execute().
    """

    name = ""
    description = ""
    arguments: type[BaseModel] | dict = EmptyArgs
    requires_approval = False
    uncertain_on_interrupt = False
    interruption_message: str | None = None
    blocks_with_live_jobs = False

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

    def decision(self, settings: Settings, args: dict) -> str:
        explicit = settings.tool_rules.get(self.name)
        if explicit:
            return explicit
        if not self.requires_approval:
            return "allow"
        return "allow" if settings.autonomy == "full" else "ask"

    def binding(self, settings: Settings, args: dict) -> str:
        return encode({
            "tool": self.name,
            "args": args,
            "tool_rule": settings.tool_rules.get(self.name),
            "autonomy": settings.autonomy,
        })

    async def authorize(self, args: dict, execution_id: str, settings: Callable[[], Settings], wait) -> None:
        """Apply this tool's approval policy before starting an operation."""
        current = settings()
        decision = self.decision(current, args)
        if decision == "deny":
            raise AccessDenied("This operation is disabled by the user.")
        binding = self.binding(current, args)
        if decision == "ask":
            if wait is None:
                raise AccessDenied("This operation requires approval, but this session has no approval channel.")
            answer = await wait(execution_id, "approval", f"Allow {self.name}?\n{encode(args)}", binding)
            if answer != "approve":
                raise AccessDenied("The user denied this operation.")
            current = settings()
            if self.binding(current, args) != binding or self.decision(current, args) == "deny":
                raise Conflict("The approval is stale because the operation or policy changed.")

    async def execute(self, args: dict, execution_id: str) -> Any:
        raise NotImplementedError


class CreateTicketTool(Tool):
    name = "create_ticket"
    description = "Create a new AgileCode ticket in Backlog."
    arguments = CreateTicketArgs

    def __init__(self, create_ticket: Callable[..., Any], source_discussion_id: str | None = None):
        self.create_ticket = create_ticket
        self.source_discussion_id = source_discussion_id

    async def execute(self, args, execution_id):
        return await self.create_ticket(
            args["title"],
            args["description"],
            source_discussion_id=self.source_discussion_id,
            request_id=execution_id,
        )


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

    def __init__(self, workspace: Workspace, git: GitRepository):
        self.search = SearchService(workspace, git)

    async def execute(self, args, execution_id):
        return await self.search.search(**args)


class WorkspaceWriteTool(Tool):
    requires_approval = True
    uncertain_on_interrupt = True
    blocks_with_live_jobs = True

    def __init__(self, workspace: Workspace, write_lock: asyncio.Lock):
        self.workspace = workspace
        self.write_lock = write_lock


class WriteFileTool(WorkspaceWriteTool):
    name = "write_file"
    description = "Create an absent file (create=true) or replace using its expected_hash. Worktree only."
    arguments = WriteArgs

    async def execute(self, args, execution_id):
        async with self.write_lock:
            return await asyncio.to_thread(self.workspace.write, **args)


class EditFileTool(WorkspaceWriteTool):
    name = "edit_file"
    description = "Apply exact replacements to the expected file hash. No fuzzy matching or partial writes."
    arguments = EditArgs

    async def execute(self, args, execution_id):
        async with self.write_lock:
            return await asyncio.to_thread(self.workspace.edit, **args)


class MovePathTool(WorkspaceWriteTool):
    name = "move_path"
    description = "Move a regular file to an absent destination, using its expected hash."
    arguments = MoveArgs

    async def execute(self, args, execution_id):
        async with self.write_lock:
            return await asyncio.to_thread(self.workspace.move, **args)


class DeletePathTool(WorkspaceWriteTool):
    name = "delete_path"
    description = "Delete one regular file at its expected hash. Does not recursively delete directories."
    arguments = DeleteArgs

    async def execute(self, args, execution_id):
        async with self.write_lock:
            return await asyncio.to_thread(self.workspace.delete, **args)


class RunCommandTool(Tool):
    name = "run_command"
    description = "Run a command in this ticket's persistent Bash shell. background=true creates a managed job."
    arguments = CommandArgs
    requires_approval = True
    uncertain_on_interrupt = True
    interruption_message = "Command stopped. Partial effects may remain; inspect them before deciding what to run next."

    def __init__(self, shell: ShellService, settings: Callable[[], Settings], attempt_id: str,
                 write_lock: asyncio.Lock):
        self.shell = shell
        self.settings = settings
        self.policy = CommandPolicy(settings)
        self.attempt_id = attempt_id
        self.write_lock = write_lock

    def decision(self, settings: Settings, args: dict) -> str:
        return self.policy.decide(args["command"], settings)

    def binding(self, settings: Settings, args: dict) -> str:
        return encode({
            "policy": self.policy.binding(args["command"], str(self.shell.shell.cwd), settings),
            "args": args,
        })

    async def authorize(self, args, execution_id, settings, wait):
        await super().authorize(args, execution_id, settings, wait)
        self._authorized_binding = self.binding(settings(), args)

    async def execute(self, args, execution_id):
        async with self.write_lock:
            if getattr(self, "_authorized_binding", None) != self.binding(self.settings(), args):
                raise Conflict("Command context or policy changed; request a new tool call.")
            self._authorized_binding = None
            containment = self.shell.shell.guard.validate(args["command"], self.shell.shell.cwd)
            if containment.decision == "block":
                raise AccessDenied(containment.reason)
            return await self.shell.run(
                args["command"],
                self.attempt_id,
                timeout=self.settings().shell_command_timeout_seconds,
                background=args["background"],
            )


async def _read_artifact(store: ProjectStore, ticket_id: str, args: dict) -> dict:
    artifact = await store.get("artifacts", args["artifact_id"])
    if artifact["ticket_id"] != ticket_id:
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


class ReadArtifactTool(Tool):
    name = "read_artifact"
    description = "Read a bounded part of an artifact owned by this ticket."
    arguments = ArtifactArgs

    def __init__(self, store: ProjectStore, ticket_id: str):
        self.store = store
        self.ticket_id = ticket_id

    async def execute(self, args, execution_id):
        return await _read_artifact(self.store, self.ticket_id, args)


class ReadJobOutputTool(Tool):
    name = "read_job_output"
    description = "Read an owned job's real status, exit code and bounded output."
    arguments = JobArgs

    def __init__(self, store: ProjectStore, ticket_id: str):
        self.store = store
        self.ticket_id = ticket_id

    async def execute(self, args, execution_id):
        job = await self.store.get("jobs", args["job_id"])
        if job["ticket_id"] != self.ticket_id:
            raise AccessDenied("Job belongs to another ticket.")
        artifact = await _read_artifact(
            self.store,
            self.ticket_id,
            {"artifact_id": job["artifact_id"], "offset": args["offset"], "limit": args["limit"]},
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

    async def execute(self, args, execution_id):
        await self.shell.stop(args["job_id"])
        return {"stopped": args["job_id"]}


class GitStatusTool(Tool):
    name = "git_status"
    description = "Inspect the ticket's Git status."
    arguments = EmptyArgs

    def __init__(self, git: GitRepository, workspace: Workspace):
        self.git = git
        self.workspace = workspace

    async def execute(self, args, execution_id):
        return {"status": await self.git.status(self.workspace.root)}


class GitDiffTool(Tool):
    name = "git_diff"
    description = "Inspect the COMPLETE eligible ticket change against the recorded starting commit."
    arguments = EmptyArgs

    def __init__(self, git: GitRepository, workspace: Workspace, base_oid: str, store: ProjectStore,
                 shell: ShellService, ticket_id: str):
        self.git = git
        self.workspace = workspace
        self.base_oid = base_oid
        self.store = store
        self.shell = shell
        self.ticket_id = ticket_id

    async def execute(self, args, execution_id):
        text = await self.git.diff(self.workspace.root, self.base_oid)
        if len(text.encode()) > 100000:
            identity = uid()
            path = self.shell.artifact_root / f"{identity}.diff"
            await asyncio.to_thread(path.write_text, text, encoding="utf-8")
            await self.store.insert("artifacts", {
                "id": identity,
                "ticket_id": self.ticket_id,
                "name": "Complete diff",
                "path": str(path),
                "size": len(text.encode()),
                "hash": digest(text.encode()),
                "complete": 1,
                "created": now(),
            })
            return {"diff": text[:100000], "partial": True, "artifact_id": identity}
        return {"diff": text, "partial": False}


class GitLogTool(Tool):
    name = "git_log"
    description = "Inspect recent commits without changing refs."
    arguments = EmptyArgs

    def __init__(self, git: GitRepository, workspace: Workspace):
        self.git = git
        self.workspace = workspace

    async def execute(self, args, execution_id):
        return {"log": (await self.git.run("log", "-20", "--oneline", cwd=self.workspace.root)).text}


class LoadSkillTool(Tool):
    name = "load_skill"
    description = "Load a skill explicitly enabled for this member; loading a skill never executes it."
    arguments = SkillArgs

    def __init__(self, skills: SkillRegistry, allowed_skills: list[str]):
        self.skills = skills
        self.allowed_skills = allowed_skills

    async def execute(self, args, execution_id):
        return await asyncio.to_thread(self.skills.load, args["name"], self.allowed_skills)


class UpdateProgressTool(Tool):
    name = "update_progress"
    description = "Record plan progress without modifying the original plan or process state."
    arguments = ProgressArgs

    def __init__(self, store: ProjectStore, ticket_id: str, attempt_id: str):
        self.store = store
        self.ticket_id = ticket_id
        self.attempt_id = attempt_id

    async def execute(self, args, execution_id):
        await self.store.activity("ProgressUpdated", {"attempt_id": self.attempt_id, **args}, self.ticket_id)
        return args


class AskUserTool(Tool):
    name = "ask_user"
    description = "Pause this SAME session for essential clarification. Must be the only call in the response."
    arguments = QuestionArgs

    def __init__(self, wait: Callable[..., Any]):
        self.wait = wait

    async def execute(self, args, execution_id):
        answer = await self.wait(execution_id, "question", args["question"], encode(args))
        return {"answer": answer}


class FinishStepTool(Tool):
    name = "finish_step"
    description = "Return status, summary and the completed step artifact. Must be the only call."
    arguments = StepResult

    def __init__(self, gate: Callable[[StepResult], Any]):
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
    blocks_with_live_jobs = True

    def __init__(self, commit: Callable[[str], Any], write_lock: asyncio.Lock):
        self.commit = commit
        self.write_lock = write_lock

    async def execute(self, args, execution_id):
        async with self.write_lock:
            return await self.commit(args["message"])


class RecordCheckTool(Tool):
    name = "record_check"
    description = "Record a requirement check backed by a successful source tool call from this verification session."
    arguments = CheckArgs

    def __init__(self, context: StepContext, git: GitRepository, workspace: Workspace, store: ProjectStore):
        self.context = context
        self.git = git
        self.workspace = workspace
        self.store = store

    async def execute(self, args, execution_id):
        requirements = []
        for key, output in self.context.outputs.items():
            if "requirements" in key or any(
                step.id == key and step.kind == "requirements"
                for step in self.context.process.steps
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
                    raise AgileCodeError(
                        "A passing check requires an observed completed command with exit code zero."
                    )
        elif args["status"] == "PASS":
            raise AgileCodeError("PASS requires a real source execution ID.")

        fingerprint = await self.git.snapshot_tree(self.workspace.root)
        identity = uid()
        await self.store.insert("evidence", {
            "id": identity,
            "attempt_id": self.context.attempt["id"],
            "requirement": args["requirement"],
            "status": args["status"],
            "method": args["method"],
            "source_tool": args["source_execution_id"],
            "fingerprint": fingerprint,
            "created": now(),
        })
        return {"evidence_id": identity, **args, "fingerprint": fingerprint}


class MCPTool(Tool):
    """A discovered MCP capability presented through the same Tool interface as native tools."""

    requires_approval = True
    uncertain_on_interrupt = True

    def __init__(self, mcp: McpService, alias: str, remote: dict):
        self.mcp = mcp
        self.alias = alias
        self.name = alias
        self.description = remote["description"]
        self.arguments = remote["schema"]

    def decision(self, settings: Settings, args: dict) -> str:
        specific = settings.tool_rules.get(self.name)
        legacy = settings.tool_rules.get("mcp")
        if "deny" in {specific, legacy}:
            return "deny"
        if specific:
            return specific
        if legacy:
            return legacy
        return "allow" if settings.autonomy == "full" else "ask"

    def binding(self, settings: Settings, args: dict) -> str:
        return encode({"tool": super().binding(settings, args), "legacy_mcp_rule": settings.tool_rules.get("mcp")})

    async def execute(self, args, execution_id):
        result = await self.mcp.call(self.alias, args)
        if result.get("isError"):
            raise AgileCodeError("MCP tool returned an error: " + encode(result)[:8000])
        return result


class MCPReadResourceTool(Tool):
    name = "mcp_read_resource"
    description = "Read a resource from an explicitly configured MCP server."
    arguments = ResourceArgs
    requires_approval = True
    uncertain_on_interrupt = True

    def __init__(self, mcp: McpService):
        self.mcp = mcp

    async def execute(self, args, execution_id):
        return await self.mcp.read_resource(**args)


@dataclass
class ExecutionResult:
    payload: dict
    step_result: StepResult | None = None


class ToolManager:
    """Expose already-created Tool objects to a model and execute correlated tool calls."""

    def __init__(self, tools: list[Tool], allowed_tools: list[str], *,
                 store: ProjectStore, events: Events, settings: Callable[[], Settings],
                 session_id: str, ticket_id: str | None = None, run_id: str | None = None,
                 attempt_id: str | None = None, wait: Callable[..., Any] | None = None,
                 shell: ShellService | None = None, check: Callable[..., Any] | None = None):
        allowed = set(allowed_tools)
        names = [tool.name for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError("Tool names must be unique.")
        self.tools = {tool.name: tool for tool in tools if tool.name in allowed}
        self.store = store
        self.events = events
        self.settings = settings
        self.session_id = session_id
        self.ticket_id = ticket_id
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.wait = wait
        self.shell = shell
        self.check = check

    def schemas(self) -> list[dict]:
        return [tool.schema() for tool in self.tools.values()]

    async def execute(self, call: dict, request_id: str) -> ExecutionResult:
        name = call["function"]["name"]
        raw = call["function"].get("arguments", "{}")
        existing = await self.store.one(
            "SELECT * FROM tools WHERE request_id=? AND call_id=?",
            (request_id, call["id"]),
        )
        if existing and (existing["session_id"] != self.session_id or existing["name"] != name or existing["args"] != raw):
            raise Conflict("Tool call identity was reused with different arguments or session.")
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
            await self.store.insert("tools", {
                "id": identity,
                "session_id": self.session_id,
                "request_id": request_id,
                "call_id": call["id"],
                "name": name,
                "args": raw,
                "status": "pending",
                # Historical column retained only for restart recovery, never permissions.
                "category": "mutation" if tool and tool.uncertain_on_interrupt else "read",
                "created": now(),
            })

        result = None
        executing = False
        try:
            if not tool:
                raise AccessDenied(f"Tool {name!r} is not exposed to this model.")
            args = tool.validate(raw)
            await tool.authorize(args, identity, self.settings, self.wait)

            if tool.blocks_with_live_jobs and self.shell and self.shell.has_live_jobs():
                raise AgileCodeError("Stop owned background jobs before changing or committing source.")

            await self.store.update("tools", identity, status="running")
            self.events.emit(Event(
                "ToolStateChanged",
                self.ticket_id,
                self.run_id,
                self.attempt_id,
                self.session_id,
                {"tool_id": identity, "name": name, "status": "running"},
            ))
            if self.check:
                await self.check()
            executing = True
            data = await tool.execute(args, identity)
            result = data if isinstance(data, StepResult) else None
            data = data.model_dump(mode="json") if result else data
            payload = {"status": "ok", "_execution_id": identity, "data": data}
        except asyncio.CancelledError:
            current = await self.store.get("tools", identity)
            if executing and tool and tool.interruption_message and current["status"] == "running":
                # A tool may report a known interruption while still requiring
                # inspection of partial effects. Never replay the closed call.
                payload = {"status": "error", "_execution_id": identity,
                           "error": {"code": "interrupted", "message":
                               tool.interruption_message}}
                await self.store.update("tools", identity, status="completed", result=encode(payload), finished=now())
            elif current["status"] != "waiting":
                uncertain = bool(executing and current["status"] == "running" and tool and tool.uncertain_on_interrupt)
                await self.store.update("tools", identity, status="unknown" if uncertain else "pending")
            raise
        except NeedsReview:
            raise
        except Exception as exc:
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
        self.events.emit(Event(
            "ToolStateChanged",
            self.ticket_id,
            self.run_id,
            self.attempt_id,
            self.session_id,
            {"tool_id": identity, "name": name, "status": payload["status"]},
        ))
        return ExecutionResult(payload, result)
