"""Project-manager tool schemas and thin model-facing bindings."""
from __future__ import annotations
from typing import Literal
from pydantic import Field
from agilecode.core.types import DataModel
from agilecode.runtime.tools import Tool
from agilecode.application.services.git_commands import run_git_command

PM_SESSION_ID = "project-manager"


class EmptyArgs(DataModel):
    pass


class TicketArgs(DataModel):
    ticket: str = Field(min_length=1)


class ArchiveTicketArgs(TicketArgs):
    archived: bool = True


class StartTicketArgs(TicketArgs):
    provider_id: str | None = None


class ListTicketsArgs(DataModel):
    query: str = ""
    include_archived: bool = False


class EpicArgs(DataModel):
    epic_id: str = Field(min_length=1)


class CreateEpicArgs(EpicArgs):
    title: str = Field(min_length=1)
    description: str = Field(default="")
    completion_criteria: str = Field(default="")


class UpdateEpicArgs(EpicArgs):
    title: str | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None)
    completion_criteria: str | None = Field(default=None)
    status: Literal["Active", "Completed", "Archived"] | None = None


class CreateTicketArgs(DataModel):
    title: str = Field(
        min_length=1,
        description="Short title for a bounded engineering statement of work, not necessarily an implementation task.",
    )
    description: str = Field(
        default="",
        description=(
            "Describe the bounded task, known context, and constraints in readable Markdown. "
            "Explicitly specify the deliverables and objective completion criteria in this description: "
            "identify the repository files or locations to create or update, their expected content, and "
            "how completion will be verified. Analysis, requirements, design, decisions, and verification "
            "results must be persisted as repository documents before the ticket's final commit; "
            "conversation output alone is insufficient. Do not prescribe unestablished future decisions "
            "or invent production-code changes for an analysis task."
        ),
    )
    team_ref: str | None = Field(
        default=None, description="Team suited to this task; omitted uses the project default.",
    )
    process_id: str | None = Field(
        default=None, description="Process suited to this task; omitted uses the configured default.",
    )
    parent_id: str | None = None
    provider_id: str | None = None
    epic_id: str | None = None
    priority: int = 0


class EditTicketArgs(DataModel):
    ticket: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None)
    team_ref: str | None = None
    process_id: str | None = None
    provider_id: str | None = None
    epic_id: str | None = None
    priority: int | None = None
    merge_auto: bool | None = None


class MoveTicketArgs(DataModel):
    ticket: str = Field(min_length=1)
    state: Literal["Backlog", "Ready", "In Progress", "Done"]
    position: float | None = None


class DependencyArgs(DataModel):
    ticket: str = Field(min_length=1)
    depends_on: str = Field(min_length=1)


class AnswerTicketArgs(DataModel):
    wait_id: str = Field(min_length=1)
    answer: str = Field(min_length=1)


class PMStateArgs(DataModel):
    goals: list[str] | None = Field(
        default=None, description="Durable project objectives, distinct from the current work plan.",
    )
    plan: list[str] | None = Field(
        default=None,
        description=(
            "Provisional near-term direction and the next few engineering activities "
            "supported by current evidence. Revise as work produces new findings; "
            "not an exhaustive roadmap or a fixed implementation contract."
        ),
    )
    current_activity: str | None = Field(default=None)


class GitArgs(DataModel):
    command: str = Field(min_length=1)


class CallbackTool(Tool):
    def __init__(self, name: str, description: str, arguments: type[DataModel], callback):
        self.name = name
        self.description = description
        self.arguments = arguments
        self.callback = callback
        self.uncertain_on_interrupt = not name.startswith(("list_", "get_"))

    async def execute(self, args, execution_id):
        return await self.callback(args, execution_id)


class ProjectGitCommandTool(Tool):
    uncertain_on_interrupt = True
    name = "git_command"
    description = "Run a local Git command against the project repository. Remote operations and remote-tracking references are blocked."
    arguments = GitArgs

    def __init__(self, git):
        self.git = git

    async def execute(self, args, execution_id):
        return await run_git_command(args["command"], git=self.git)


