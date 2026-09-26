"""Validated, UI-independent definitions. Runtime state lives in the project store."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
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
    summary: str = Field(min_length=1, max_length=4000)
    output: str = Field(min_length=1, max_length=200_000)


class ModelProfile(DataModel):
    base_url: str = "http://127.0.0.1:1234/v1"
    model: str | None = None
    api_key_env: str | None = None
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


NATIVE_TOOL_NAMES = [
    "list_files", "read_file", "code_search", "write_file", "edit_file", "move_path",
    "delete_path", "run_command", "read_job_output", "stop_job", "read_artifact",
    "git_status", "git_diff", "git_log", "load_skill", "update_progress",
    "record_check", "ask_user", "finish_step", "final_commit", "mcp_read_resource",
]
LEGACY_TOOL_GROUPS = {
    "read": ["list_files", "read_file", "read_job_output", "read_artifact"],
    "search": ["code_search"],
    "write": ["write_file", "edit_file"],
    "destructive": ["move_path", "delete_path"],
    "execute": ["run_command", "stop_job"],
    "git": ["git_status", "git_diff", "git_log"],
    "skills": ["load_skill"],
    "control": ["update_progress", "ask_user", "finish_step"],
    "evidence": ["record_check"],
    "commit": ["final_commit"],
    "mcp": ["mcp_read_resource"],
}


def _expand_legacy_tool_names(values: list[str]) -> list[str]:
    import fnmatch
    expanded: list[str] = []
    for value in values:
        if value == "*":
            matches = NATIVE_TOOL_NAMES
        elif value in LEGACY_TOOL_GROUPS:
            matches = LEGACY_TOOL_GROUPS[value]
        elif any(ch in value for ch in "*?["):
            matches = [name for name in NATIVE_TOOL_NAMES if fnmatch.fnmatch(name, value)]
        else:
            matches = [value]
        for name in matches:
            if name not in expanded:
                expanded.append(name)
    return expanded


class TeamMember(DataModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=160)
    system_prompt: str = Field(min_length=1, max_length=30000)
    allowed_tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    model_override: str | None = None

    @field_validator("allowed_tools", mode="before")
    @classmethod
    def explicit_tool_names(cls, value):
        return _expand_legacy_tool_names(list(value or []))


StepKind = Literal["understand", "requirements", "architecture", "plan", "implement", "verify", "review", "commit", "analysis"]


class Step(DataModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=160)
    team_member_id: str
    prompt: str = Field(min_length=1, max_length=30000)
    kind: StepKind = "analysis"
    correction_target: str | None = None

    async def run(self, context: StepContext) -> StepResult:
        """Execute through the injected headless session runner; no shared step state."""
        return await context.execute(self, context)


STANDARD_KINDS = ["understand", "requirements", "architecture", "plan", "implement", "verify", "review", "commit"]


class Process(DataModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=160)
    steps: list[Step] = Field(min_length=1, max_length=64)
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
            raise ValueError("The standard process must contain the exact eight ordered stages.")
        return self


class Team(DataModel):
    format_version: Literal[1] = 1
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=160)
    members: list[TeamMember] = Field(min_length=1, max_length=64)
    processes: list[Process] = Field(min_length=1, max_length=64)
    default_process_id: str
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
    pattern: str = Field(min_length=1, max_length=1000)
    action: Literal["allow", "ask", "deny"]


def default_command_rules() -> list[CommandRule]:
    return [
        CommandRule(pattern="sudo *", action="ask"),
        CommandRule(pattern="apt install *", action="ask"),
        CommandRule(pattern="apt-get install *", action="ask"),
        CommandRule(pattern="pip install *", action="ask"),
        CommandRule(pattern="pip3 install *", action="ask"),
        CommandRule(pattern="python -m pip install *", action="ask"),
        CommandRule(pattern="python3 -m pip install *", action="ask"),
        CommandRule(pattern="npm install *", action="ask"),
        CommandRule(pattern="npm i *", action="ask"),
        CommandRule(pattern="pnpm add *", action="ask"),
        CommandRule(pattern="yarn add *", action="ask"),
        CommandRule(pattern="cargo install *", action="ask"),
        CommandRule(pattern="gem install *", action="ask"),
        CommandRule(pattern="flatpak install *", action="ask"),
    ]


class Settings(DataModel):
    format_version: Literal[1] = 1
    active_profile: str = "local"
    profiles: dict[str, ModelProfile] = Field(default_factory=lambda: {"local": ModelProfile()})
    default_team: str = "builtin:standard"
    default_process: str = "development"
    autonomy: Literal["full", "restricted"] = "restricted"
    tool_rules: dict[str, Literal["allow", "ask", "deny"]] = Field(default_factory=dict)
    command_rules: list[CommandRule] = Field(default_factory=default_command_rules)
    merge_on_completion: bool = False
    remove_worktree_after_merge: bool = True
    remove_checkpoints_after_merge: bool = True
    delete_branch_after_merge: bool = False
    max_corrections: int = Field(default=10, ge=1)
    shell_command_timeout_seconds: float | None = Field(default=None, gt=0)
    verification_commands: list[str] = Field(default_factory=list)
    setup_commands: list[str] = Field(default_factory=list)
    mcp_servers: list[McpServerConfig] = Field(default_factory=list)
    theme: Literal["dark", "light"] = "dark"
    open_reasoning_by_default: bool = False
    @field_validator("tool_rules", mode="before")
    @classmethod
    def explicit_tool_rule_names(cls, value):
        rules = dict(value or {})
        expanded: dict[str, str] = {}
        for key, action in rules.items():
            names = _expand_legacy_tool_names([key])
            for name in names:
                expanded.setdefault(name, action)
        return expanded

    @model_validator(mode="after")
    def valid(self):
        if self.active_profile not in self.profiles:
            raise ValueError("active_profile must identify an existing profile.")
        if len({s.id for s in self.mcp_servers}) != len(self.mcp_servers):
            raise ValueError("MCP server IDs must be unique.")
        return self


@dataclass
class StepContext:
    ticket: dict[str, Any]
    run: dict[str, Any]
    attempt: dict[str, Any]
    team: Team
    process: Process
    workspace: Path
    outputs: dict[str, StepResult]
    previous_output: str | None
    correction_report: str | None
    execute: Callable[[Step, StepContext], Awaitable[StepResult]]
    phase: str = "development"
    extra: dict[str, Any] = field(default_factory=dict)


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
