"""Validated, UI-independent definitions. Runtime state lives in the project store."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Literal
from uuid import uuid4
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def uid() -> str:
    return uuid4().hex


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AgileCodeError(Exception):
    """An actionable error which may be displayed without a traceback."""
    code = "application_error"


class Conflict(AgileCodeError):
    code = "stale_state"


class AccessDenied(AgileCodeError):
    code = "access_denied"


class NeedsReview(AgileCodeError):
    code = "review_required"


class Interrupted(AgileCodeError):
    code = "interrupted"


class GenerationStopped(AgileCodeError):
    """The user deliberately stopped the current model response."""
    code = "generation_stopped"


class BoardState(StrEnum):
    BACKLOG = "Backlog"
    READY = "Ready"
    IN_PROGRESS = "In Progress"
    DONE = "Done"


class ResultStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    CHANGES_REQUIRED = "CHANGES_REQUIRED"


class DataModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class StepResult(DataModel):
    status: ResultStatus
    summary: str = Field(min_length=1)
    output: str = Field(min_length=1)


class ModelProfile(DataModel):
    base_url: str = "http://127.0.0.1:1234/v1"
    model: str | None = None
    api_key_env: str | None = None
    # Connection/write/pool and metadata-read timeout; generation reads are unlimited.
    # Keep saved values compatible, including profiles frozen into retained attempts.
    timeout_seconds: float | None = Field(default=None, gt=0)
    request_options: dict[str, Any] = Field(default_factory=dict)
    round_trip_reasoning: bool = False
    @model_validator(mode="after")
    def valid(self):
        from urllib.parse import urlsplit
        u = urlsplit(self.base_url)
        if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("Use an HTTP(S) base URL without credentials, query, or fragment.")
        if {"messages", "tools", "model", "stream", "n"} & self.request_options.keys():
            raise ValueError("request_options cannot replace model, stream, n, messages, or tools.")
        return self


class ProviderConfig(DataModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=160)
    profile: ModelProfile = Field(default_factory=ModelProfile)


class ProviderPool(DataModel):
    format_version: Literal[1] = 1
    default_provider_id: str | None = None
    providers: list[ProviderConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid(self):
        ids = [provider.id for provider in self.providers]
        if len(ids) != len(set(ids)):
            raise ValueError("Provider IDs must be unique.")
        if self.providers and self.default_provider_id not in ids:
            raise ValueError("A non-empty provider pool must identify exactly one default provider.")
        if not self.providers and self.default_provider_id is not None:
            raise ValueError("An empty provider pool cannot identify a default provider.")
        return self


NATIVE_TOOL_NAMES = (
    "list_files", "read_file", "code_search",
    "write_file", "edit_file", "move_path", "delete_path",
    "run_command", "read_job_output", "stop_job", "read_artifact",
    "git_command", "load_skill", "create_ticket",
    "update_progress", "record_check", "ask_user", "finish_step",
    "final_commit", "mcp_read_resource",
)

# Tools shown in the team-member permission UI but not granted implicitly.
DEFAULT_MEMBER_TOOL_NAMES = tuple(name for name in NATIVE_TOOL_NAMES if name != "create_ticket")

LEGACY_ALLOWED_TOOL_GROUPS = {
    "read": ("list_files", "read_file", "read_job_output", "read_artifact"),
    "search": ("code_search",),
    "write": ("write_file", "edit_file"),
    "destructive": ("move_path", "delete_path"),
    "execute": ("run_command", "stop_job"),
    "git": ("git_command",),
    "skills": ("load_skill",),
    "control": ("update_progress", "ask_user", "finish_step"),
    "evidence": ("record_check",),
    "commit": ("final_commit",),
    "mcp": ("mcp_read_resource",),
}


class TeamMember(DataModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=160)
    system_prompt: str = Field(min_length=1)
    allowed_tools: list[str] = Field(default_factory=lambda: list(DEFAULT_MEMBER_TOOL_NAMES))
    skills: list[str] = Field(default_factory=list)
    model_override: str | None = None

    @field_validator("allowed_tools")
    @classmethod
    def explicit_tool_names(cls, value):
        for name in value:
            if not name or name.strip() != name or name in LEGACY_ALLOWED_TOOL_GROUPS or any(c in name for c in "*?[]"):
                raise ValueError("allowed_tools requires explicit tool names, not categories or wildcard patterns.")
        return list(dict.fromkeys(value))


StepKind = Literal["understand", "requirements", "architecture", "plan", "implement", "verify_design", "verify", "review", "commit", "analysis"]


class Step(DataModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=160)
    team_member_id: str
    prompt: str = Field(min_length=1)
    kind: StepKind = "analysis"
    correction_target: str | None = None


STANDARD_KINDS = ["understand", "requirements", "architecture", "plan", "implement", "verify_design", "verify", "review", "commit"]


class Process(DataModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=160)
    steps: list[Step] = Field(min_length=1)
    standard: bool = False
    @model_validator(mode="after")
    def valid(self):
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("Step IDs must be unique inside their process.")
        for i, s in enumerate(self.steps):
            if s.correction_target and s.correction_target not in ids[:i]:
                raise ValueError("A correction target must reference an earlier step.")
        if self.standard and [s.kind for s in self.steps] != STANDARD_KINDS:
            raise ValueError("The standard process must contain the exact nine ordered engineering stages.")
        return self


class Team(DataModel):
    format_version: Literal[2] = 2
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=160)
    members: list[TeamMember] = Field(min_length=1)
    processes: list[Process] = Field(min_length=1)
    default_process_id: str

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_tools(cls, value):
        if not isinstance(value, dict):
            return value
        value = dict(value)
        version = value.get("format_version", 1)
        members = []
        for member in value.get("members", []):
            if not isinstance(member, dict):
                members.append(member)
                continue
            member = dict(member)
            legacy = member.get("allowed_tools")
            if version == 1:
                names = []
                for name in legacy if legacy is not None else ["*"]:
                    if any(c in name for c in "*?["):
                        from fnmatch import fnmatchcase
                        # Dynamic MCP capabilities are selected explicitly after discovery.
                        names.extend(tool for tool in DEFAULT_MEMBER_TOOL_NAMES if fnmatchcase(tool, name))
                    else:
                        names.extend(LEGACY_ALLOWED_TOOL_GROUPS.get(name, (name,)))
                # These were unconditionally exposed in version 1. Preserve them
                # once during migration; version 2 never adds implicit tools.
                names.extend(LEGACY_ALLOWED_TOOL_GROUPS["control"])
                member["allowed_tools"] = list(dict.fromkeys(names))
            elif isinstance(legacy, list):
                # Consolidate the former one-tool-per-Git-command permissions.
                names = [
                    "git_command" if name in {"git_status", "git_diff", "git_log"} else name
                    for name in legacy
                ]
                member["allowed_tools"] = list(dict.fromkeys(names))
            members.append(member)
        value.update(members=members)
        if version == 1:
            value["format_version"] = 2
        return value
    @model_validator(mode="after")
    def valid(self):
        members = [m.id for m in self.members]
        processes = [p.id for p in self.processes]
        if len(members) != len(set(members)) or len(processes) != len(set(processes)):
            raise ValueError("Duplicate member/process IDs.")
        if self.default_process_id not in processes:
            raise ValueError("The default process must belong to this team.")
        for p in self.processes:
            for s in p.steps:
                if s.team_member_id not in members:
                    raise ValueError(f"Step {s.id} references a member outside this team.")
        return self
    def process(self, process_id: str) -> Process:
        return next(p for p in self.processes if p.id == process_id)
    def member(self, member_id: str) -> TeamMember:
        return next(m for m in self.members if m.id == member_id)


class McpServerConfig(DataModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,60}$")
    transport: Literal["stdio", "http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = False
    scope: Literal["external", "ticket"] = "external"
    api_key_env: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    max_message_bytes: int | None = Field(default=None, ge=1)
    @model_validator(mode="after")
    def valid(self):
        if self.transport == "stdio" and not self.command:
            raise ValueError("A stdio server requires a command.")
        if self.transport == "http":
            from urllib.parse import urlsplit
            parsed = urlsplit(self.url or "")
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("An HTTP server requires an HTTP(S) URL without embedded credentials.")
        return self


class CommandRule(DataModel):
    pattern: str = Field(min_length=1)
    action: Literal["allow", "ask", "deny"]


class Settings(DataModel):
    format_version: Literal[1] = 1
    default_team: str = "builtin:standard"
    default_process: str = "development"
    autonomy: Literal["full", "restricted"] = "restricted"
    tool_rules: dict[str, Literal["allow", "ask", "deny"]] = Field(default_factory=dict)
    command_rules: list[CommandRule] = Field(default_factory=list)
    merge_on_completion: bool = False
    remove_worktree_after_merge: bool = True
    remove_checkpoints_after_merge: bool = True
    delete_branch_after_merge: bool = False
    max_corrections: int | None = Field(default=10, ge=1)
    shell_command_timeout_seconds: float | None = Field(default=None, gt=0)
    git_operation_timeout_seconds: float | None = Field(default=None, gt=0)
    model_transport_retry_attempts: int = Field(default=3, ge=1)
    model_transport_retry_delay_cap_seconds: float = Field(default=30, ge=0)
    shell_log_max_bytes: int | None = Field(default=None, ge=1)
    command_preview_bytes: int = Field(default=65536, ge=1)
    file_read_page_bytes: int = Field(default=262144, ge=1)
    tool_result_inline_bytes: int = Field(default=300000, ge=1)
    tool_result_preview_chars: int = Field(default=40000, ge=0)
    search_file_budget: int | None = Field(default=None, ge=1)
    search_time_budget_seconds: float | None = Field(default=None, gt=0)
    conversation_page_size: int = Field(default=200, ge=1)
    verification_commands: list[str] = Field(default_factory=list)
    setup_commands: list[str] = Field(default_factory=list)
    python_environment: Literal["auto", "venv", "disabled"] = "auto"
    python_interpreter: str = Field(default="python3", min_length=1)
    mcp_servers: list[McpServerConfig] = Field(default_factory=list)
    theme: Literal["dark", "light"] = "dark"
    open_reasoning_by_default: bool = False
    open_tools_by_default: bool = False
    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_provider_settings(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            value.pop("active_profile", None)
            value.pop("profiles", None)
        return value

    @model_validator(mode="after")
    def valid(self):
        if len({s.id for s in self.mcp_servers}) != len(self.mcp_servers):
            raise ValueError("MCP server IDs must be unique.")
        return self



@dataclass(frozen=True)
class Event:
    kind: str
    ticket_id: str | None = None
    run_id: str | None = None
    attempt_id: str | None = None
    session_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=now)
    id: str = field(default_factory=uid)


class Events:
    """Small synchronous notification boundary. Listeners must not block."""
    def __init__(self):
        self._listeners: list[Callable[[Event], None]] = []
    def subscribe(self, callback: Callable[[Event], None]) -> Callable[[], None]:
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None
    def emit(self, event: Event) -> None:
        import logging
        for callback in tuple(self._listeners):
            try:
                callback(event)
            except Exception:
                logging.getLogger(__name__).exception("Event subscriber failed")


__all__ = [
    "uid",
    "now",
    "AgileCodeError",
    "Conflict",
    "AccessDenied",
    "NeedsReview",
    "Interrupted",
    "GenerationStopped",
    "BoardState",
    "ResultStatus",
    "DataModel",
    "StepResult",
    "ModelProfile",
    "ProviderConfig",
    "ProviderPool",
    "NATIVE_TOOL_NAMES",
    "DEFAULT_MEMBER_TOOL_NAMES",
    "LEGACY_ALLOWED_TOOL_GROUPS",
    "TeamMember",
    "StepKind",
    "Step",
    "STANDARD_KINDS",
    "Process",
    "Team",
    "McpServerConfig",
    "CommandRule",
    "Settings",
    "Event",
    "Events",
]
