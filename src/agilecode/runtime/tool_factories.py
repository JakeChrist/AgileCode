"""Explicit role tool sets. Construction does not drive conversations."""
from __future__ import annotations
from agilecode.adapters.workspace import Workspace
from agilecode.runtime import tools as toolset
from agilecode.runtime.tools import Tool, ToolManager, ListFilesTool, ReadFileTool, CodeSearchTool, LoadSkillTool, CreateTicketTool
from agilecode.runtime.pm_tools import PM_SESSION_ID
from agilecode.runtime.pm_tools import EmptyArgs, TicketArgs, ArchiveTicketArgs, StartTicketArgs, ListTicketsArgs, EpicArgs, CreateEpicArgs, UpdateEpicArgs, CreateTicketArgs, EditTicketArgs, MoveTicketArgs, DependencyArgs, AnswerTicketArgs, PMStateArgs, GitArgs, CallbackTool, ProjectGitCommandTool


def pm_tool_settings(pm):
    # PM authority is deliberately independent of ticket-member tool ACLs.
    return pm.app.config.settings.model_copy(update={"tool_rules": {}, "autonomy": "full"})


def pm_currently_exposed(pm, name):
    if name in {"answer_ticket_question", "list_pending_ticket_questions"}:
        return pm.settings.answer_ticket_questions
    if name == "archive_ticket":
        return pm.settings.allow_archive
    if name == "delete_ticket":
        return pm.settings.allow_delete
    return True



def project_manager_tools(pm):
    scope = Workspace(pm.app.root, pm.app.root, pm.app.global_root,
                      read_limit=pm.app.config.settings.file_read_page_bytes)
    tools: list[Tool] = [
        ListFilesTool(scope),
        ReadFileTool(scope),
        CodeSearchTool(scope, pm.app.git, lambda: pm.app.config.settings),
        ProjectGitCommandTool(pm.app.git),
        LoadSkillTool(pm.app.skills, pm._effective_skills()),
        CallbackTool("list_tickets", "List project tickets and their current board state.", ListTicketsArgs, pm._list_tickets),
        CallbackTool("get_ticket", "Read a ticket's statement of work, dependencies, execution status and results, including final findings or other deliverables, to inform what to do next.", TicketArgs, pm._get_ticket),
        CallbackTool("create_epic", "Organize one bounded engineering outcome with its own completion criteria. Adjust epic boundaries as evidence develops; avoid catch-all epics and speculative full-project work breakdowns. Epics do not execute.", CreateEpicArgs, pm._create_epic),
        CallbackTool("get_epic", "Inspect an epic's purpose, completion criteria and associated ticket progress.", EpicArgs, pm._get_epic),
        CallbackTool("list_epics", "List epics with their status and ticket progress.", EmptyArgs, pm._list_epics),
        CallbackTool("update_epic", "Edit an epic's purpose, completion criteria or status after evaluating progress.", UpdateEpicArgs, pm._update_epic),
        CallbackTool("delete_epic", "Delete only an epic grouping and detach its tickets. All tickets, their keys, work, history, dependencies, branches and worktrees are preserved. The deleted epic ID remains reserved and cannot be reused.", EpicArgs, pm._delete_epic),
        CallbackTool("list_teams", "List configured teams and processes available for ticket assignment.", EmptyArgs, pm._list_teams),
        CallbackTool("list_providers", "List global Provider Pool entries and the current default provider.", EmptyArgs, pm._list_providers),
        CallbackTool("create_ticket", "Create a bounded statement of engineering work in Backlog. Tasks may seek evidence, requirements, architecture, decisions, code, or verification. The description must explicitly specify repository-file deliverables, their expected content, and objective completion criteria. Analysis results must be persisted as repository documents before the ticket's final commit; conversation output alone is insufficient. Choose team_ref and process_id for the actual task (omitted values use project defaults). Create only work justified by present evidence, not an entire speculative implementation plan. epic_id optionally generates an EpicID-NN key; PM-created tickets use automatic integration.", CreateTicketArgs, pm._create_ticket),
        CallbackTool("edit_ticket", "Edit ticket fields including epic_id. Set epic_id to an empty string to detach an unstarted ticket from an epic.", EditTicketArgs, pm._edit_ticket),
        CallbackTool("move_ticket", "Move or reorder a ticket on the board.", MoveTicketArgs, pm._move_ticket),
        CallbackTool("add_dependency", "Make one ticket depend on another.", DependencyArgs, pm._add_dependency),
        CallbackTool("remove_dependency", "Remove an existing ticket dependency.", DependencyArgs, pm._remove_dependency),
        CallbackTool("start_ticket", "Start a Ready ticket, optionally assigning a Provider Pool entry first, subject to the PM concurrency limit.", StartTicketArgs, pm._start_ticket),
        CallbackTool("pause_ticket", "Pause one ticket execution. This does not pause the Project Manager.", TicketArgs, pm._pause_ticket),
        CallbackTool("resume_ticket", "Explicitly resume the selected paused or cancelled ticket execution from its retained progress, subject to the PM concurrency limit.", TicketArgs, pm._resume_ticket),
        CallbackTool("cancel_ticket", "Cancel one ticket execution. This does not stop the Project Manager.", TicketArgs, pm._cancel_ticket),
        CallbackTool("update_project_manager_state", "Record durable goals and an evolving near-term plan. Keep only the next justified activities and revise them after new findings; this is not a fixed end-to-end schedule.", PMStateArgs, pm._update_state),
    ]
    allowed = [tool.name for tool in tools]
    if pm.settings.answer_ticket_questions:
        tools += [
            CallbackTool("list_pending_ticket_questions", "List unresolved questions/approvals from ticket executions.", EmptyArgs, pm._pending_questions),
            CallbackTool("answer_ticket_question", "Answer a pending ticket question/approval on behalf of the project manager.", AnswerTicketArgs, pm._answer_ticket),
        ]
        allowed += ["list_pending_ticket_questions", "answer_ticket_question"]
    if pm.settings.allow_archive:
        tools.append(CallbackTool("archive_ticket", "Archive or unarchive a ticket. Set archived to false to unarchive.", ArchiveTicketArgs, pm._archive_ticket))
        allowed.append("archive_ticket")
    if pm.settings.allow_delete:
        tools.append(CallbackTool("delete_ticket", "Permanently delete an eligible ticket and its owned resources.", TicketArgs, pm._delete_ticket))
        allowed.append("delete_ticket")
    manager = ToolManager(
        tools,
        allowed,
        store=pm.app.store,
        events=pm.app.events,
        settings=lambda: pm_tool_settings(pm),
        session_id=PM_SESSION_ID,
    )

    manager.current_exposure = lambda name: pm_currently_exposed(pm, name)
    return manager



def ticket_tools(processor, step, ctx, runtime, *, wait, gate, commit):
    member = ctx.team.member(step.team_member_id)
    tool_objects = [
        toolset.ListFilesTool(runtime.workspace),
        toolset.ReadFileTool(runtime.workspace),
        toolset.CodeSearchTool(runtime.workspace, processor.git, processor.settings),
        toolset.WriteFileTool(runtime.workspace, runtime.write_lock, store=processor.store),
        toolset.EditFileTool(runtime.workspace, runtime.write_lock, store=processor.store),
        toolset.MovePathTool(runtime.workspace, runtime.write_lock, store=processor.store),
        toolset.DeletePathTool(runtime.workspace, runtime.write_lock, store=processor.store),
        toolset.RunCommandTool(runtime.shell, processor.settings, ctx.attempt["id"], runtime.write_lock),
        toolset.ReadJobOutputTool(processor.store, ctx.ticket["id"]),
        toolset.StopJobTool(runtime.shell),
        toolset.ReadArtifactTool(processor.store, ctx.ticket["id"]),
        toolset.GitCommandTool(
            runtime.shell,
            processor.settings,
            ctx.attempt["id"],
            runtime.write_lock,
        ),
        toolset.LoadSkillTool(processor.skills, member.skills),
        toolset.CreateTicketTool(processor.create_ticket),
        toolset.UpdateProgressTool(processor.store, ctx.ticket["id"], ctx.attempt["id"]),
        toolset.RecordCheckTool(ctx, processor.git, runtime.workspace, processor.store),
        toolset.AskUserTool(wait),
        toolset.FinishStepTool(gate, pass_only=step.kind == "implement"),
        toolset.FinalCommitTool(commit, runtime.write_lock),
    ]
    tool_objects.extend(
        toolset.MCPTool(runtime.mcp, alias, remote)
        for alias, remote in runtime.mcp.tools.items()
    )
    if runtime.mcp.connections:
        tool_objects.append(toolset.MCPReadResourceTool(runtime.mcp))
    manager = toolset.ToolManager(
        tool_objects,
        member.allowed_tools,
        store=processor.store,
        events=processor.events,
        settings=processor.settings,
        session_id=ctx.attempt["session_id"],
        ticket_id=ctx.ticket["id"],
        run_id=ctx.run["id"],
        attempt_id=ctx.attempt["id"],
        wait=wait,
        shell=runtime.shell,
        check=runtime.control.check,
    )
    return manager


def discussion_tools(app, discussion_id, session):
    scope = Workspace(app.root, app.root, app.global_root, read_limit=app.config.settings.file_read_page_bytes)
    return ToolManager([ReadFileTool(scope), ListFilesTool(scope),
        CodeSearchTool(scope, app.git, lambda: app.config.settings), LoadSkillTool(app.skills, ["*"]),
        CreateTicketTool(app.create_ticket, discussion_id)],
        ["read_file", "list_files", "code_search", "load_skill", "create_ticket"],
        store=app.store, events=app.events, settings=lambda: app.config.settings, session_id=session)
