"""Persistent project-level autonomous Project Manager."""
from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .context import ContextManager
from .tools import Tool, ToolManager, ListFilesTool, ReadFileTool, CodeSearchTool
from .types import AgileCodeError, AccessDenied, DataModel, Event, now, uid
from agilecode.adapters.store import encode
from agilecode.adapters.workspace import Workspace


PM_SESSION_ID = "project-manager"


class ProjectManagerSettings(DataModel):
    mode: Literal["restricted", "fully_automated"] = "restricted"
    max_concurrent_tickets: int = Field(default=4, ge=1, le=32)
    wait_for_current_tickets: bool = False
    answer_ticket_questions: bool = True
    uncertainty: Literal["continue", "ask"] = "continue"
    allow_archive: bool = False
    allow_delete: bool = False


class EmptyArgs(DataModel):
    pass


class TicketArgs(DataModel):
    ticket: str = Field(min_length=1)


class ListTicketsArgs(DataModel):
    query: str = ""
    include_archived: bool = False


class CreateTicketArgs(DataModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=100_000)
    team_ref: str | None = None
    process_id: str | None = None
    parent_id: str | None = None
    priority: int = 0


class EditTicketArgs(DataModel):
    ticket: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=100_000)
    team_ref: str | None = None
    process_id: str | None = None
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
    answer: str = Field(min_length=1, max_length=100_000)


class PMStateArgs(DataModel):
    goals: list[str] | None = None
    plan: list[str] | None = None
    current_activity: str | None = Field(default=None, max_length=4000)


class GitArgs(DataModel):
    command: str = Field(min_length=1, max_length=20_000)


class CallbackTool(Tool):
    def __init__(self, name: str, description: str, arguments: type[DataModel], callback):
        self.name = name
        self.description = description
        self.arguments = arguments
        self.callback = callback

    async def execute(self, args, execution_id):
        return await self.callback(args, execution_id)


class ProjectGitCommandTool(Tool):
    name = "git_command"
    description = "Run a read-only Git command against the project repository. The command must start with git."
    arguments = GitArgs

    READ_ONLY = {
        "status", "log", "show", "diff", "branch", "rev-parse", "ls-files", "grep",
        "remote", "tag", "describe", "blame", "shortlog", "for-each-ref", "merge-base",
        "name-rev", "cat-file", "ls-tree",
    }

    def __init__(self, git):
        self.git = git

    async def execute(self, args, execution_id):
        try:
            argv = shlex.split(args["command"])
        except ValueError as exc:
            raise AgileCodeError(f"Invalid Git command: {exc}") from exc
        if len(argv) < 2 or argv[0] != "git":
            raise AgileCodeError("git_command requires a Git command beginning with git.")
        if argv[1] not in self.READ_ONLY:
            raise AccessDenied("Project Manager git_command is read-only.")
        result = await self.git.run(*argv[1:], check=False)
        return {
            "status": "completed",
            "exit_code": result.code,
            "stdout": result.stdout.decode(errors="replace")[:100_000],
            "stderr": result.stderr.decode(errors="replace")[:20_000],
        }


class ProjectManager:
    """One persistent project-level manager, independent from ticket runtimes."""

    def __init__(self, app):
        self.app = app
        self.root = app.root / ".agile_code" / "project_manager"
        self.settings_path = self.root / "settings.json"
        self.state_path = self.root / "state.json"
        self.settings = ProjectManagerSettings()
        self.state: dict[str, Any] = {}
        self.loop_task: asyncio.Task | None = None
        self.turn_task: asyncio.Task | None = None
        self.active_generation: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "status": "paused",
            "goals": [],
            "plan": [],
            "current_activity": "Idle",
            "updated": now(),
        }

    @staticmethod
    def _load_json(path: Path, default):
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp.replace(path)

    async def open(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        raw_settings = await asyncio.to_thread(self._load_json, self.settings_path, {})
        self.settings = ProjectManagerSettings.model_validate(raw_settings or {})
        raw_state = await asyncio.to_thread(self._load_json, self.state_path, self._default_state())
        state = {**self._default_state(), **(raw_state or {})}
        # A host exit/crash always requires explicit human intervention before PM work resumes.
        if state.get("status") == "running":
            state["status"] = "paused"
            state["current_activity"] = "Paused after AgileCode restart"
        self.state = state
        await self._persist_settings()
        await self._persist_state()
        if not await self.app.store.messages(PM_SESSION_ID, include_partial=True):
            await self.app.store.message(PM_SESSION_ID, {
                "role": "system",
                "content": self._system_prompt(),
            })

    async def shutdown(self) -> None:
        await self._cancel_current()
        if self.loop_task and not self.loop_task.done():
            self.loop_task.cancel()
            await asyncio.gather(self.loop_task, return_exceptions=True)
        self.loop_task = None
        if self.state.get("status") == "running":
            self.state["status"] = "paused"
            self.state["current_activity"] = "Paused because AgileCode closed"
            await self._persist_state()

    async def _persist_settings(self) -> None:
        await asyncio.to_thread(
            self._write_json, self.settings_path, self.settings.model_dump(mode="json")
        )

    async def _persist_state(self) -> None:
        self.state["updated"] = now()
        await asyncio.to_thread(self._write_json, self.state_path, self.state)
        self.app.events.emit(Event(
            "ProjectManagerChanged",
            session_id=PM_SESSION_ID,
            data={"state": self.state, "settings": self.settings.model_dump(mode="json")},
        ))

    def _system_prompt(self) -> str:
        mode = self.settings.mode
        mode_text = (
            "FULLY AUTOMATED MODE: You are the active project manager. Independently inspect the "
            "project, establish and revise goals, maintain your own plan, create/manage/start tickets, "
            "react to results, and continue until the project is complete. You may create new goals "
            "whenever the project needs them. Do not wait for a user instruction to continue."
            if mode == "fully_automated"
            else
            "RESTRICTED MODE: Act only in response to the user's current instruction. You may perform "
            "a complete multi-step management operation, but do not independently establish new project "
            "goals or continue managing after the requested operation is complete."
        )
        uncertainty = (
            "When uncertain, make the best supported decision and continue."
            if self.settings.uncertainty == "continue"
            else
            "When a material decision genuinely requires the user, end your response with a line beginning "
            "exactly 'NEEDS_USER:' followed by the question. Do not use this for routine engineering judgment."
        )
        return (
            "You are AgileCode's persistent Project Manager. Tickets, teams, and processes are separate "
            "object classes: tickets describe/manage work; teams contain engineering members; processes "
            "define workflows. Coordinate them without treating them as a hierarchy. You may directly inspect "
            "the repository, but do not directly implement source changes; engineering changes happen through "
            "ticket execution. Normal ticket management is your responsibility. Keep your structured goals, "
            "plan, and current activity current using update_project_manager_state. "
            + mode_text + " " + uncertainty
        )

    async def snapshot(self) -> dict:
        messages = await self.app.store.messages(PM_SESSION_ID, include_partial=True)
        return {
            "session": PM_SESSION_ID,
            "state": self.state,
            "settings": self.settings.model_dump(mode="json"),
            "messages": messages,
        }

    async def save_settings(self, raw: dict) -> dict:
        self.settings = ProjectManagerSettings.model_validate(raw)
        await self._persist_settings()
        # Refresh the durable system instruction for subsequent turns.
        messages = await self.app.store.messages(PM_SESSION_ID, include_partial=True)
        system = next((m for m in messages if m["body"].get("role") == "system"), None)
        if system:
            await self.app.store.execute(
                "UPDATE messages SET body=? WHERE id=?",
                (encode({"role": "system", "content": self._system_prompt()}), system["id"]),
            )
        if self.settings.mode == "restricted" and self.loop_task and not self.loop_task.done():
            self.loop_task.cancel()
            await asyncio.gather(self.loop_task, return_exceptions=True)
            self.loop_task = None
        elif self.settings.mode == "fully_automated" and self.state.get("status") == "running":
            self._ensure_loop()
        await self._persist_state()
        return self.settings.model_dump(mode="json")

    async def resume(self) -> dict:
        self.state["status"] = "running"
        self.state["current_activity"] = "Resuming project management"
        await self._persist_state()
        if self.settings.mode == "fully_automated":
            self._ensure_loop()
        return await self.snapshot()

    async def pause(self) -> dict:
        self.state["status"] = "paused"
        self.state["current_activity"] = "Paused"
        await self._persist_state()
        await self._cancel_current()
        if self.loop_task and not self.loop_task.done():
            self.loop_task.cancel()
            await asyncio.gather(self.loop_task, return_exceptions=True)
        self.loop_task = None
        return await self.snapshot()

    async def turn_off(self) -> dict:
        self.state["status"] = "off"
        self.state["current_activity"] = "Off"
        await self._persist_state()
        await self._cancel_current()
        if self.loop_task and not self.loop_task.done():
            self.loop_task.cancel()
            await asyncio.gather(self.loop_task, return_exceptions=True)
        self.loop_task = None
        return await self.snapshot()

    async def stop_response(self) -> dict:
        stopped = await self._cancel_current()
        if stopped and self.state.get("status") == "running":
            self.state["current_activity"] = "Current response stopped; reconsidering"
            await self._persist_state()
            if self.settings.mode == "fully_automated":
                self._ensure_loop()
        return {"stopped": stopped}

    async def _cancel_current(self) -> bool:
        stopped = False
        for task in (self.active_generation, self.turn_task):
            if task and not task.done():
                task.cancel()
                stopped = True
        if stopped:
            await asyncio.sleep(0)
        return stopped

    async def message(self, text: str) -> dict:
        text = text.strip()
        if not text:
            raise AgileCodeError("Enter a Project Manager message.")
        # User input has priority: stop the current PM response/action loop first.
        await self._cancel_current()
        await self.app.store.message(PM_SESSION_ID, {"role": "user", "content": text})
        self.app.events.emit(Event("ProjectManagerChanged", session_id=PM_SESSION_ID))
        if self.state.get("status") != "running":
            return await self.snapshot()
        if self.settings.mode == "restricted":
            self.turn_task = asyncio.create_task(self._run_turn(user_initiated=True))
            try:
                await self.turn_task
            finally:
                self.turn_task = None
        else:
            self._ensure_loop()
        return await self.snapshot()

    def _ensure_loop(self) -> None:
        if not self.loop_task or self.loop_task.done():
            self.loop_task = asyncio.create_task(
                self._autonomous_loop(), name="project-manager"
            )

    async def _active_ticket_count(self) -> int:
        row = await self.app.store.one(
            "SELECT COUNT(*) AS n FROM runs WHERE state IN "
            "('provisioning','running','waiting','pause_requested')"
        )
        return int(row["n"] if row else 0)

    async def _wait_for_all_current_tickets(self) -> None:
        while self.state.get("status") == "running":
            if await self._active_ticket_count() == 0:
                return
            await asyncio.sleep(1.0)

    async def _autonomous_loop(self) -> None:
        try:
            while (
                self.state.get("status") == "running"
                and self.settings.mode == "fully_automated"
            ):
                self.turn_task = asyncio.create_task(self._run_turn(user_initiated=False))
                try:
                    await self.turn_task
                except asyncio.CancelledError:
                    if self.state.get("status") != "running":
                        raise
                finally:
                    self.turn_task = None
                if self.state.get("status") != "running":
                    break
                if self.settings.wait_for_current_tickets and await self._active_ticket_count():
                    self.state["current_activity"] = "Waiting for current tickets to finish"
                    await self._persist_state()
                    await self._wait_for_all_current_tickets()
                else:
                    # Autonomous, but paced so an idle PM does not hammer the model endpoint.
                    await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            pass

    async def _update_state(self, args, execution_id):
        for key in ("goals", "plan", "current_activity"):
            if args.get(key) is not None:
                self.state[key] = args[key]
        await self._persist_state()
        return self.state

    async def _list_tickets(self, args, execution_id):
        return await self.app.board(args["query"], include_archived=args["include_archived"])

    async def _get_ticket(self, args, execution_id):
        return await self.app.ticket_detail(args["ticket"])

    async def _list_teams(self, args, execution_id):
        return await self.app.config.list_teams()

    async def _create_ticket(self, args, execution_id):
        ticket = await self.app.create_ticket(
            args["title"],
            args["description"],
            team_ref=args.get("team_ref"),
            process_id=args.get("process_id"),
            parent_id=args.get("parent_id"),
            merge_auto=True,
            request_id=execution_id,
        )
        if args.get("priority"):
            ticket = await self.app.edit_ticket(
                ticket["id"], expected_revision=ticket["revision"], priority=args["priority"]
            )
        return ticket

    async def _edit_ticket(self, args, execution_id):
        ticket = await self.app.find_ticket(args["ticket"])
        changes = {
            key: value for key, value in args.items()
            if key != "ticket" and value is not None
        }
        return await self.app.edit_ticket(
            ticket["id"], expected_revision=ticket["revision"], **changes
        )

    async def _move_ticket(self, args, execution_id):
        ticket = await self.app.find_ticket(args["ticket"])
        return await self.app.move_ticket(
            ticket["id"], args["state"], expected_revision=ticket["revision"],
            position=args.get("position"),
        )

    async def _add_dependency(self, args, execution_id):
        ticket = await self.app.find_ticket(args["ticket"])
        dependency = await self.app.find_ticket(args["depends_on"])
        await self.app.add_dependency(ticket["id"], dependency["id"])
        return {"ticket": ticket["key"], "depends_on": dependency["key"]}

    async def _remove_dependency(self, args, execution_id):
        ticket = await self.app.find_ticket(args["ticket"])
        dependency = await self.app.find_ticket(args["depends_on"])
        await self.app.remove_dependency(ticket["id"], dependency["id"])
        return {"ticket": ticket["key"], "removed_dependency": dependency["key"]}

    async def _start_ticket(self, args, execution_id):
        if await self._active_ticket_count() >= self.settings.max_concurrent_tickets:
            raise AgileCodeError(
                f"Maximum concurrent tickets ({self.settings.max_concurrent_tickets}) is already reached."
            )
        ticket = await self.app.find_ticket(args["ticket"])
        if not ticket["merge_auto"]:
            ticket = await self.app.edit_ticket(
                ticket["id"], expected_revision=ticket["revision"], merge_auto=True
            )
        return await self.app.start_ticket(ticket["id"])

    async def _pause_ticket(self, args, execution_id):
        ticket = await self.app.find_ticket(args["ticket"])
        await self.app.pause_run(ticket["id"])
        return {"paused": ticket["key"]}

    async def _resume_ticket(self, args, execution_id):
        if await self._active_ticket_count() >= self.settings.max_concurrent_tickets:
            raise AgileCodeError(
                f"Maximum concurrent tickets ({self.settings.max_concurrent_tickets}) is already reached."
            )
        ticket = await self.app.find_ticket(args["ticket"])
        await self.app.resume_run(ticket["id"])
        return {"resumed": ticket["key"]}

    async def _cancel_ticket(self, args, execution_id):
        ticket = await self.app.find_ticket(args["ticket"])
        await self.app.cancel_run(ticket["id"])
        return {"cancelled": ticket["key"]}

    async def _archive_ticket(self, args, execution_id):
        ticket = await self.app.find_ticket(args["ticket"])
        await self.app.archive_ticket(ticket["id"], True)
        return {"archived": ticket["key"]}

    async def _delete_ticket(self, args, execution_id):
        ticket = await self.app.find_ticket(args["ticket"])
        return await self.app.delete_ticket(ticket["id"])

    async def _pending_questions(self, args, execution_id):
        return await self.app.store.query(
            "SELECT waits.*,tickets.key AS ticket_key,tickets.title AS ticket_title "
            "FROM waits JOIN tickets ON tickets.id=waits.ticket_id "
            "WHERE waits.status='pending' ORDER BY waits.created"
        )

    async def _answer_ticket(self, args, execution_id):
        await self.app.answer(args["wait_id"], args["answer"])
        return {"answered": args["wait_id"]}

    def _tool_manager(self) -> ToolManager:
        scope = Workspace(self.app.root, self.app.root, self.app.global_root)
        tools: list[Tool] = [
            ListFilesTool(scope),
            ReadFileTool(scope),
            CodeSearchTool(scope, self.app.git),
            ProjectGitCommandTool(self.app.git),
            CallbackTool("list_tickets", "List project tickets and their current board state.", ListTicketsArgs, self._list_tickets),
            CallbackTool("get_ticket", "Get full details for one ticket, including execution and dependencies.", TicketArgs, self._get_ticket),
            CallbackTool("list_teams", "List configured teams and processes available for ticket assignment.", EmptyArgs, self._list_teams),
            CallbackTool("create_ticket", "Create a backlog ticket. PM-created tickets use automatic integration.", CreateTicketArgs, self._create_ticket),
            CallbackTool("edit_ticket", "Edit normal ticket fields such as title, description, team, process, priority, or merge policy.", EditTicketArgs, self._edit_ticket),
            CallbackTool("move_ticket", "Move or reorder a ticket on the board.", MoveTicketArgs, self._move_ticket),
            CallbackTool("add_dependency", "Make one ticket depend on another.", DependencyArgs, self._add_dependency),
            CallbackTool("remove_dependency", "Remove an existing ticket dependency.", DependencyArgs, self._remove_dependency),
            CallbackTool("start_ticket", "Start a Ready ticket, subject to the PM concurrency limit.", TicketArgs, self._start_ticket),
            CallbackTool("pause_ticket", "Pause one ticket execution. This does not pause the Project Manager.", TicketArgs, self._pause_ticket),
            CallbackTool("resume_ticket", "Resume one paused ticket execution, subject to the PM concurrency limit.", TicketArgs, self._resume_ticket),
            CallbackTool("cancel_ticket", "Cancel one ticket execution. This does not stop the Project Manager.", TicketArgs, self._cancel_ticket),
            CallbackTool("update_project_manager_state", "Update the PM's persistent goals, plan, or current activity.", PMStateArgs, self._update_state),
        ]
        allowed = [tool.name for tool in tools]
        if self.settings.answer_ticket_questions:
            tools += [
                CallbackTool("list_pending_ticket_questions", "List unresolved questions/approvals from ticket executions.", EmptyArgs, self._pending_questions),
                CallbackTool("answer_ticket_question", "Answer a pending ticket question/approval on behalf of the project manager.", AnswerTicketArgs, self._answer_ticket),
            ]
            allowed += ["list_pending_ticket_questions", "answer_ticket_question"]
        if self.settings.allow_archive:
            tools.append(CallbackTool("archive_ticket", "Archive a ticket.", TicketArgs, self._archive_ticket))
            allowed.append("archive_ticket")
        if self.settings.allow_delete:
            tools.append(CallbackTool("delete_ticket", "Permanently delete an eligible ticket and its owned resources.", TicketArgs, self._delete_ticket))
            allowed.append("delete_ticket")
        return ToolManager(
            tools,
            allowed,
            store=self.app.store,
            events=self.app.events,
            settings=lambda: self.app.config.settings,
            session_id=PM_SESSION_ID,
        )

    async def _summarize_context(self, profile, source: str) -> str:
        turn = await self.app.client.stream(
            profile,
            [
                {"role": "system", "content": (
                    "Compress this Project Manager history faithfully. Preserve user directives, goals, plans, "
                    "decisions, ticket identifiers, tool outcomes, blockers, and current progress. Do not invent facts."
                )},
                {"role": "user", "content": source},
            ],
            [],
            None,
        )
        return turn.message.get("content") or turn.message.get("reasoning_content") or source

    async def _run_turn(self, *, user_initiated: bool) -> dict:
        async with self.lock:
            if self.state.get("status") != "running":
                return await self.snapshot()
            manager = self._tool_manager()
            profile = await self.app.client.resolve(
                self.app.config.settings.profiles[self.app.config.settings.active_profile]
            )
            self.state["current_activity"] = (
                "Responding to user direction" if user_initiated else "Managing project autonomously"
            )
            await self._persist_state()
            while self.state.get("status") == "running":
                messages = [row["body"] for row in await self.app.store.messages(PM_SESSION_ID)]
                if not user_initiated:
                    messages.append({
                        "role": "user",
                        "content": (
                            "AUTONOMOUS MANAGEMENT CYCLE. Inspect current reality as needed, update your goals/plan, "
                            "and take the next useful permitted project-management actions. Do not merely narrate what "
                            "you could do. Respect the concurrency and waiting settings enforced by your tools."
                        ),
                    })
                context_window = await self.app.client.context_window(profile)
                messages, _ = await ContextManager().prepare(
                    messages, context_window,
                    lambda source: self._summarize_context(profile, source),
                )
                request_id = uid()
                await self.app.store.insert("requests", {
                    "id": request_id,
                    "session_id": PM_SESSION_ID,
                    "model": profile.model,
                    "input": encode({"messages": messages, "tools": manager.schemas()}),
                    "status": "streaming",
                    "created": now(),
                })
                partial_text: list[str] = []
                partial_reasoning: list[str] = []

                def on_delta(kind, value):
                    if kind == "text":
                        partial_text.append(value)
                    elif kind == "reasoning":
                        partial_reasoning.append(value)
                    self.app.events.emit(Event(
                        "ProjectManagerDelta",
                        session_id=PM_SESSION_ID,
                        data={"request_id": request_id, "kind": kind, "value": value},
                    ))

                self.app.events.emit(Event(
                    "ProjectManagerGenerationStarted",
                    session_id=PM_SESSION_ID,
                    data={"request_id": request_id},
                ))
                self.active_generation = asyncio.create_task(
                    self.app.client.stream(profile, messages, manager.schemas(), on_delta)
                )
                try:
                    turn = await self.active_generation
                    await self.app.store.message(
                        PM_SESSION_ID, turn.message, request_id,
                        partial=turn.finish_reason == "length",
                    )
                    await self.app.store.update(
                        "requests", request_id, status="completed",
                        usage=encode(turn.usage) if turn.usage else None,
                        finished=now(),
                    )
                except asyncio.CancelledError:
                    body = {"role": "assistant", "content": "".join(partial_text) or None}
                    if partial_reasoning:
                        body["reasoning_content"] = "".join(partial_reasoning)
                    if body.get("content") or body.get("reasoning_content"):
                        await self.app.store.message(PM_SESSION_ID, body, request_id, partial=True)
                    await self.app.store.update(
                        "requests", request_id, status="interrupted",
                        error="Project Manager response interrupted", finished=now(),
                    )
                    raise
                finally:
                    self.active_generation = None
                    self.app.events.emit(Event(
                        "ProjectManagerGenerationFinished",
                        session_id=PM_SESSION_ID,
                        data={"request_id": request_id},
                    ))

                calls = turn.message.get("tool_calls") or []
                if not calls:
                    content = turn.message.get("content") or ""
                    if self.settings.uncertainty == "ask" and "NEEDS_USER:" in content:
                        self.state["status"] = "paused"
                        self.state["current_activity"] = "Waiting for user direction"
                    else:
                        self.state["current_activity"] = "Monitoring project"
                    await self._persist_state()
                    self.app.events.emit(Event("ProjectManagerChanged", session_id=PM_SESSION_ID))
                    return await self.snapshot()

                for call in calls:
                    outcome = await manager.execute(call, request_id)
                    await self.app.store.message(
                        PM_SESSION_ID,
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": encode(outcome.payload),
                        },
                        request_id,
                    )
                self.app.events.emit(Event("ProjectManagerChanged", session_id=PM_SESSION_ID))
        return await self.snapshot()
