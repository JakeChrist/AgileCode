"""Tool objects plus one manager for model registration, dispatch and durable call records."""
from __future__ import annotations

import asyncio
import fnmatch
import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

from agilecode.runtime import conversation_records as conversation_records
from agilecode.application import process_operations
from agilecode.application.services.agent_workspace import AgentWorkspaceOperations
from agilecode.application.services.artifacts import ArtifactService
from agilecode.application.services.jobs import JobService
from agilecode.application.services.git_commands import validate_git_command, run_git_command
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
from agilecode.adapters.workspace import Workspace


class EmptyArgs(DataModel):
    pass


class PathArgs(DataModel):
    path: str
    scope: Literal["workspace", "project", "global"] = "workspace"


class ReadArgs(PathArgs):
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    byte_offset: int | None = Field(default=None, ge=0)


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
    cursor: str | None = None
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
    background: bool = False


class GitCommandArgs(DataModel):
    command: str = Field(min_length=1)


class JobArgs(DataModel):
    job_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20000, ge=1)


class StopArgs(DataModel):
    job_id: str


class ArtifactArgs(DataModel):
    artifact_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20000, ge=1)


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


class CreateTicketArgs(DataModel):
    title: str = Field(min_length=1)
    epic_id: str | None = None
    description: str = Field(
        description=(
            "Describe a bounded statement of engineering work in readable Markdown with natural headings and lists, rather than dense prose. "
            "State the purpose, known evidence or constraints, expected deliverable "
            "(findings, requirements, decisions, design, code, verification, etc.), and "
            "completion conditions. Do not assume the task is implementation or prescribe "
            "future decisions not supported by current evidence."
        ),
    )


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

    Subclasses describe and bind application operations. ToolManager exposes named
    tools, validates and records calls, applies policy, and dispatches execute().
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

    async def authorize(self, args: dict, execution_id: str, settings: Callable[[], Settings], wait) -> str:
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
        return binding

    def revalidate_authorization(self, args: dict, settings: Settings, binding: str) -> None:
        """Pure final policy check, called inside the policy/dispatch reservation."""
        if self.decision(settings, args) == "deny":
            raise AccessDenied("This operation is disabled by the user.")
        if self.binding(settings, args) != binding:
            raise Conflict("The operation or policy changed before dispatch. Request fresh authorization.")

    async def execute(self, args: dict, execution_id: str) -> Any:
        raise NotImplementedError


class CreateTicketTool(Tool):
    uncertain_on_interrupt = True
    name = "create_ticket"
    description = ("Create a Backlog ticket as a bounded statement of work for investigation, "
                   "requirements, architecture, implementation, verification, or another engineering "
                   "activity. Specify the expected outcome and completion criteria in readable Markdown.")
    arguments = CreateTicketArgs

    def __init__(self, create_ticket: Callable[..., Any], source_discussion_id: str | None = None):
        self.create_ticket = create_ticket
        self.source_discussion_id = source_discussion_id

    async def execute(self, args, execution_id):
        return await self.create_ticket(
            args["title"],
            args["description"],
            epic_id=args.get("epic_id"),
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
    description = "Read UTF-8 files and extract PDF/DOCX text. Returns the original file byte hash; line ranges are one-based and inclusive."
    arguments = ReadArgs

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    async def execute(self, args, execution_id):
        return await asyncio.to_thread(self.workspace.read, **args)


class CodeSearchTool(Tool):
    name = "code_search"
    description = "Search literal text, regex, filename/path, Python AST symbols, or textual reference candidates."
    arguments = SearchArgs

    def __init__(self, workspace: Workspace, git: GitRepository, settings: Callable[[], Settings] | None = None):
        self.search = SearchService(workspace, git, settings)

    async def execute(self, args, execution_id):
        return await self.search.search(**args)


class WorkspaceWriteTool(Tool):
    requires_approval = True
    uncertain_on_interrupt = True
    blocks_with_live_jobs = True

    def __init__(self, workspace: Workspace, write_lock: asyncio.Lock, store: ProjectStore | None = None):
        self.workspace = workspace
        self.write_lock = write_lock
        self.operations = AgentWorkspaceOperations(workspace, write_lock, store)


class WriteFileTool(WorkspaceWriteTool):
    name = "write_file"
    description = "Create an absent file (create=true) or replace using its expected_hash. Worktree only."
    arguments = WriteArgs

    async def execute(self, args, execution_id):
        return await self.operations.write(execution_id=execution_id, **args)


class EditFileTool(WorkspaceWriteTool):
    name = "edit_file"
    description = "Apply exact replacements to the expected file hash. No fuzzy matching or partial writes."
    arguments = EditArgs

    async def execute(self, args, execution_id):
        return await self.operations.edit(execution_id=execution_id, **args)


class MovePathTool(WorkspaceWriteTool):
    name = "move_path"
    description = "Move a regular file to an absent destination, using its expected hash."
    arguments = MoveArgs

    async def execute(self, args, execution_id):
        return await self.operations.move(execution_id=execution_id, **args)


class DeletePathTool(WorkspaceWriteTool):
    name = "delete_path"
    description = "Delete one regular file at its expected hash. Does not recursively delete directories."
    arguments = DeleteArgs

    async def execute(self, args, execution_id):
        return await self.operations.delete(execution_id=execution_id, **args)


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
        self._authorized_binding = await super().authorize(args, execution_id, settings, wait)
        return self._authorized_binding

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
    return await ArtifactService(store).read(**args, ticket_id=ticket_id)


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
        return await JobService(self.store).output(**args, ticket_id=self.ticket_id)


class StopJobTool(Tool):
    name = "stop_job"
    description = "Stop only a job belonging to this ticket."
    arguments = StopArgs
    requires_approval = True
    uncertain_on_interrupt = True

    def __init__(self, shell: ShellService):
        self.shell = shell

    async def execute(self, args, execution_id):
        return await JobService(self.shell.store, shell_supplier=lambda: [self.shell]).stop(
            args["job_id"], ticket_id=self.shell.ticket_id)


class GitCommandTool(Tool):
    name = "git_command"
    description = "Run a local Git command in this ticket's worktree. Remote operations and remote-tracking references are blocked."
    arguments = GitCommandArgs
    uncertain_on_interrupt = True
    interruption_message = "Git command stopped. Partial Git effects may remain; inspect repository state before retrying."

    def __init__(self, shell: ShellService, settings: Callable[[], Settings], attempt_id: str,
                 write_lock: asyncio.Lock):
        self.shell = shell
        self.settings = settings
        self.attempt_id = attempt_id
        self.write_lock = write_lock

    async def execute(self, args, execution_id):
        return await run_git_command(args["command"], shell=self.shell,
            attempt_id=self.attempt_id, settings=self.settings, write_lock=self.write_lock)


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
    uncertain_on_interrupt = True
    name = "update_progress"
    description = "Record plan progress without modifying the original plan or process state."
    arguments = ProgressArgs

    def __init__(self, store: ProjectStore, ticket_id: str, attempt_id: str):
        self.store = store
        self.ticket_id = ticket_id
        self.attempt_id = attempt_id

    async def execute(self, args, execution_id):
        return await process_operations.update_progress(
            self.store, self.ticket_id, self.attempt_id, args, execution_id)


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
    uncertain_on_interrupt = True
    name = "record_check"
    description = "Record a requirement check backed by a successful source tool call from this verification session."
    arguments = CheckArgs

    def __init__(self, context: StepContext, git: GitRepository, workspace: Workspace, store: ProjectStore):
        self.context = context
        self.git = git
        self.workspace = workspace
        self.store = store

    async def execute(self, args, execution_id):
        return await process_operations.record_check(
            self.context, self.git, self.workspace, self.store, args, execution_id)


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


interrupted_tool_result = conversation_records.interrupted_tool_result


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
        self.dispatch_guard = None
        self.current_exposure = None
        self.input_version = None

    def schemas(self) -> list[dict]:
        return [tool.schema() for tool in self.tools.values()]

    async def execute(self, call: dict, request_id: str) -> ExecutionResult:
        async with conversation_records.session_lock(self.store, "tool:" + request_id + ":" + call["id"]):
            return await self._execute(call, request_id)

    async def _complete(self, identity, payload, *, finish=False):
        content = None
        # Preserve the full outcome in tools and one stable preview in the journal.
        if self.shell and self.ticket_id and len(encode(payload).encode()) > self.settings().tool_result_inline_bytes:
            prior = await self.store.one("SELECT data FROM operations WHERE id=?", ("conversation-result:" + identity,))
            if prior:
                content = decode(prior["data"])["content"]
            else:
                text = encode(payload)
                artifact_id = "tool-result-" + identity
                artifacts = getattr(self.shell, "artifacts", ArtifactService(self.store, self.shell.artifact_root))
                await conversation_records.owned_finalization(artifacts.create(self.ticket_id, "Large tool result",
                    suffix=".json", identity=artifact_id, content=text.encode()))
                content = encode({"status": payload["status"], "_execution_id": identity, "partial": True,
                                  "artifact_id": artifact_id, "preview": text[:self.settings().tool_result_preview_chars]})
        return await conversation_records.complete_execution_exchange_once(
            self.store, identity, payload, content=content,
            expected_version=self.input_version if finish else None)

    async def _execute(self, call: dict, request_id: str) -> ExecutionResult:
        name = call["function"]["name"]
        raw = call["function"].get("arguments", "{}")
        existing = await self.store.one("SELECT * FROM tools WHERE request_id=? AND call_id=?", (request_id, call["id"]))
        if existing and (existing["session_id"] != self.session_id or existing["name"] != name or existing["args"] != raw):
            raise Conflict("Tool call identity was reused with different arguments or session.")
        if existing and existing["result"]:
            payload = await self._complete(existing["id"], decode(existing["result"]))
            if name == "final_commit":
                known = await conversation_records.reconcile_final_commit_execution(self.store, existing["id"])
                payload = known or payload
            result = StepResult.model_validate(payload["data"]) if name == "finish_step" and payload.get("status") == "ok" else None
            return ExecutionResult(payload, result)
        if existing and existing["status"] in {"running", "unknown"}:
            payload = await self._complete(existing["id"], await conversation_records.recover_execution_payload(self.store, existing))
            return ExecutionResult(payload)

        tool = self.tools.get(name)
        identity = existing["id"] if existing else uid()
        if existing and existing["status"] == "waiting" and name == "ask_user":
            # Session/tool ownership proves the old question waiter is gone;
            # resuming its durable wait does not replay a mutating operation.
            await self.store.execute("UPDATE operations SET status='waiting',updated=? WHERE id=?", (now(), "conversation-dispatch:" + identity))
        if not existing:
            await self.store.insert("tools", {"id": identity, "session_id": self.session_id, "request_id": request_id,
                "call_id": call["id"], "name": name, "args": raw, "status": "pending",
                "category": "mutation" if tool and tool.uncertain_on_interrupt else "read", "created": now()})
        entered = False
        execution_task = None
        try:
            if not tool:
                raise AccessDenied(f"Tool {name!r} is not exposed to this model.")
            args = tool.validate(raw)
            authorization = await tool.authorize(args, identity, self.settings, self.wait)
            # Check after approval and before claiming/entering an operation.
            if self.check:
                await self.check()
            # Effective settings publishers use this same reservation. The
            # approval/policy check and the durable claim are therefore ordered
            # together; no old allow/approval can slip past a newer deny.
            async with conversation_records.policy_lock(self.store):
                tool.revalidate_authorization(args, self.settings(), authorization)
                if self.current_exposure and not self.current_exposure(name):
                    raise AccessDenied(f"Tool {name!r} is no longer exposed to this model.")
                if tool.blocks_with_live_jobs and self.shell and self.shell.has_live_jobs():
                    raise AgileCodeError("Stop owned background jobs before changing or committing source.")
                if self.dispatch_guard and not await self.dispatch_guard(identity):
                    raise Conflict("New input or control superseded this unstarted tool call; it was not executed.")
                await self.store.update("tools", identity, status="running")
                self.events.emit(Event("ToolStateChanged", self.ticket_id, self.run_id, self.attempt_id, self.session_id,
                                       {"tool_id": identity, "name": name, "status": "running"}))
                if self.current_exposure and not self.current_exposure(name):
                    raise AccessDenied(f"Tool {name!r} is no longer exposed to this model.")
                entered = True
                execution_task = asyncio.create_task(tool.execute(args, identity))
            data = await asyncio.shield(execution_task)
            result = data if isinstance(data, StepResult) else None
            payload = {"status": "ok", "_execution_id": identity,
                       "data": data.model_dump(mode="json") if result else data}
        except asyncio.CancelledError:
            if execution_task is not None:
                execution_task.cancel()
                async def reconcile():
                    try:
                        data, _ = await conversation_records.owned_finalization(execution_task)
                        payload = {"status": "ok", "_execution_id": identity,
                                   "data": data.model_dump(mode="json") if isinstance(data, StepResult) else data}
                    except BaseException:
                        current = await self.store.get("tools", identity)
                        if name == "ask_user" and current["status"] == "waiting":
                            # Waiting for clarification is resumable, not an
                            # interrupted external effect or a completed result.
                            await self.store.execute("UPDATE operations SET status='waiting',updated=? WHERE id=?", (now(), "conversation-dispatch:" + identity))
                            return
                        payload = interrupted_tool_result(identity, tool.interruption_message if tool else None)
                        if not tool or not tool.uncertain_on_interrupt:
                            payload["outcome_known"] = True
                        elif name in {"run_command", "git_command"} and self.shell and not self.shell.foreground_task:
                            # The managed shell finished its cancellation cleanup.
                            # Termination is known; source effects are not undone.
                            payload["outcome_known"] = True
                        recovered = await conversation_records.recover_execution_payload(self.store, await self.store.get("tools", identity))
                        if recovered.get("status") == "ok":
                            payload = recovered
                    await self._complete(identity, payload, finish=name == "finish_step")
                await conversation_records.owned_finalization(reconcile())
            else:
                async def retire_unentered():
                    claim = await self.store.one("SELECT id FROM operations WHERE id=?", ("conversation-dispatch:" + identity,))
                    if claim:
                        await self.store.update("operations", claim["id"], status="retired", updated=now())
                        await self._complete(identity, {"status": "error", "_execution_id": identity, "error": {
                            "code": "not_executed", "message": "Dispatch was retired before physical tool entry. No operation was executed."}})
                await conversation_records.owned_finalization(retire_unentered())
            # Waiting for approval/control has not entered the tool. Leave its
            # pending row for the exchange owner to close against current input.
            raise
        except NeedsReview:
            raise
        except Exception as exc:
            result = None
            payload = {"status": "error", "_execution_id": identity,
                       "error": {"code": getattr(exc, "code", "invalid_tool"), "message": str(exc)[:6000]}}
        payload, cancelled = await conversation_records.owned_finalization(self._complete(identity, payload, finish=name == "finish_step"))
        if cancelled:
            raise asyncio.CancelledError
        result = StepResult.model_validate(payload["data"]) if name == "finish_step" and payload.get("status") == "ok" else None
        self.events.emit(Event("ToolStateChanged", self.ticket_id, self.run_id, self.attempt_id, self.session_id,
                               {"tool_id": identity, "name": name, "status": payload["status"]}))
        return ExecutionResult(payload, result)
