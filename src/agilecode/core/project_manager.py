"""Persistent event-driven project manager."""
from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .context import ContextManager
from agilecode.adapters.model import ContextWindowChanged
from .pm_profiles import PMProfile, PMProfileLibrary
from .tools import Tool, ToolManager, ListFilesTool, ReadFileTool, CodeSearchTool, LoadSkillTool
from .types import AgileCodeError, AccessDenied, DataModel, Event, now, uid
from agilecode.adapters.store import decode, encode
from agilecode.adapters.workspace import Workspace


PM_SESSION_ID = "project-manager"


class ProjectManagerSettings(DataModel):
    mode: Literal["restricted", "fully_automated"] = "restricted"
    provider_id: str | None = None
    profile_id: str | None = None
    project_instructions: str = Field(default="", max_length=30_000)
    project_skills: list[str] = Field(default_factory=list)
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


class StartTicketArgs(TicketArgs):
    provider_id: str | None = None


class ListTicketsArgs(DataModel):
    query: str = ""
    include_archived: bool = False


class CreateTicketArgs(DataModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=100_000)
    team_ref: str | None = None
    process_id: str | None = None
    parent_id: str | None = None
    provider_id: str | None = None
    priority: int = 0


class EditTicketArgs(DataModel):
    ticket: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=100_000)
    team_ref: str | None = None
    process_id: str | None = None
    provider_id: str | None = None
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
        "status", "log", "show", "diff", "rev-parse", "ls-files", "grep",
        "describe", "blame", "shortlog", "for-each-ref", "merge-base",
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
        self.profiles = PMProfileLibrary(app.global_root)
        self.state: dict[str, Any] = {}
        self.turn_task: asyncio.Task | None = None
        self._event_unsubscribe = None
        self._pending_updates: set[str] = set()
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
        await self._ensure_local_git_exclude()
        raw_settings = await asyncio.to_thread(self._load_json, self.settings_path, {})
        self.settings = ProjectManagerSettings.model_validate(raw_settings or {})
        raw_state = await asyncio.to_thread(self._load_json, self.state_path, self._default_state())
        state = {**self._default_state(), **(raw_state or {})}
        # A host exit/crash always requires explicit human intervention before PM work resumes.
        if state.get("status") == "running":
            state["status"] = "paused"
            state["current_activity"] = "Paused after AgileCode restart"
        self.state = state
        if self.settings.profile_id:
            try:
                self.profiles.get(self.settings.profile_id)
            except AgileCodeError:
                self.settings.profile_id = None
                self.state["current_activity"] = "Missing PM profile; default role selected."
        await self._persist_settings()
        await self._persist_state()
        if not await self.app.store.messages(PM_SESSION_ID, include_partial=True):
            await self.app.store.message(PM_SESSION_ID, {
                "role": "system",
                "content": self._system_prompt(),
            })
        self._event_unsubscribe = self.app.events.subscribe(self._on_project_event)

    async def shutdown(self) -> None:
        if self._event_unsubscribe:
            self._event_unsubscribe()
            self._event_unsubscribe = None
        self._pending_updates.clear()
        await self._cancel_current()
        if self.state.get("status") == "running":
            self.state["status"] = "paused"
            self.state["current_activity"] = "Paused because AgileCode closed"
            await self._persist_state()

    async def _ensure_local_git_exclude(self) -> None:
        result = await self.app.git.run("rev-parse", "--git-path", "info/exclude")
        path = Path(result.text)
        if not path.is_absolute():
            path = (self.app.root / path).resolve()
        def ensure():
            path.parent.mkdir(parents=True, exist_ok=True)
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            entries = {line.strip() for line in text.splitlines()}
            if ".agile_code/" not in entries:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(("\n" if text and not text.endswith("\n") else "") + ".agile_code/\n")
        await asyncio.to_thread(ensure)

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

    def _skill_records(self) -> dict[str, dict]:
        return {
            item["id"]: item
            for item in self.app.skills.catalog(["*"])
            if "error" not in item and not item.get("overridden")
        }

    def _selected_role_profile(self) -> PMProfile | None:
        if not self.settings.profile_id:
            return None
        try:
            return self.profiles.get(self.settings.profile_id)
        except AgileCodeError:
            # A profile may have been deleted from another project while this
            # project remains open. Use the built-in role until reassigned.
            return None

    def _effective_skills(self) -> list[str]:
        profile = self._selected_role_profile()
        names = (profile.skills if profile else []) + self.settings.project_skills
        return list(dict.fromkeys(names))

    def list_profiles(self) -> list[dict]:
        return self.profiles.list()

    def save_profile(self, raw: dict) -> dict:
        available_global = {
            item["id"] for item in self.app.skills.catalog(["*"])
            if item["scope"] == "global" and "error" not in item
        }
        profile = PMProfile.model_validate({**raw, "id": raw.get("id") or "new"})
        unknown = set(profile.skills) - available_global
        if unknown:
            raise AgileCodeError("Unknown global PM skills: " + ", ".join(sorted(unknown)))
        saved = self.profiles.save(raw)
        self.app.events.emit(Event("ProjectManagerChanged", session_id=PM_SESSION_ID))
        return saved

    async def delete_profile(self, identity: str) -> dict:
        self.profiles.delete(identity)
        if self.settings.profile_id == identity:
            self.settings.profile_id = None
            await self._persist_settings()
        self.app.events.emit(Event("ProjectManagerChanged", session_id=PM_SESSION_ID))
        return {"deleted": identity}

    def _system_prompt(self) -> str:
        mode = self.settings.mode
        mode_text = (
            "FULLY AUTOMATED MODE: Evaluate project and ticket updates and use your "
            "judgment to decide what needs attention. You may independently inspect "
            "the repository, manage goals and plans, and create, start, or manage tickets. "
            "When no tickets are running, examine the codebase for useful work."
            if mode == "fully_automated"
            else
            "RESTRICTED MODE: Respond to user instructions only. Do not initiate "
            "independent project work."
        )
        uncertainty = (
            "When uncertain, make the best supported decision and continue."
            if self.settings.uncertainty == "continue"
            else
            "When a material decision genuinely requires the user, end your response with a line beginning "
            "exactly 'NEEDS_USER:' followed by the question. Do not use this for routine engineering judgment."
        )
        scheduling = (
            f"Maximum concurrent tickets: {self.settings.max_concurrent_tickets}. "
            + (
                "Wait for all tickets in the current batch to finish before starting a new batch."
                if self.settings.wait_for_current_tickets
                else
                "You may start eligible tickets as capacity becomes available."
            )
        )
        question_policy = (
            "You may inspect and answer pending ticket questions on behalf of project management."
            if self.settings.answer_ticket_questions
            else
            "Do not answer ticket questions on the user's behalf."
        )
        builtin = (
            "You are AgileCode's persistent Project Manager. Tickets, teams, and processes are separate "
            "object classes: tickets describe/manage work; teams contain engineering members; processes "
            "define workflows. Coordinate them without treating them as a hierarchy. You may directly inspect "
            "the repository, but do not directly implement source changes; engineering changes happen through "
            "ticket execution. Normal ticket management is your responsibility. Keep your structured goals, "
            "plan, and current activity current using update_project_manager_state. "
            + mode_text + " " + scheduling + " " + question_policy + " " + uncertainty
        )
        sections = [builtin]
        profile = self._selected_role_profile()
        if profile and profile.instructions.strip():
            sections.append("PROJECT MANAGER ROLE INSTRUCTIONS (cannot override mode or permissions):\n"
                            + profile.instructions)
        if self.settings.project_instructions.strip():
            sections.append("PROJECT-SPECIFIC INSTRUCTIONS (cannot override mode or permissions):\n"
                            + self.settings.project_instructions)
        selected_skills = self._effective_skills()
        if selected_skills:
            available = self._skill_records()
            entries = [name + ": " + available[name].get("description", "")
                       for name in selected_skills if name in available]
            if entries:
                sections.append("AVAILABLE PM SKILLS:\n" + "\n".join(entries)
                                + "\nLoad a skill using load_skill before following its procedure.")
        return "\n\n".join(sections)

    async def snapshot(self) -> dict:
        # Runtime state is not conversation history. The UI loads conversation
        # pages using the same endpoint as ticket conversations.
        return {
            "session": PM_SESSION_ID,
            "state": self.state,
            "settings": self.settings.model_dump(mode="json"),
            "profiles": self.list_profiles(),
            "skills": await asyncio.to_thread(self.app.skills.catalog, ["*"]),
        }

    async def save_settings(self, raw: dict) -> dict:
        settings = ProjectManagerSettings.model_validate(raw)
        if settings.provider_id is not None:
            self.app.config.get_provider(settings.provider_id)
        if settings.profile_id is not None:
            self.profiles.get(settings.profile_id)
        unknown = set(settings.project_skills) - set(self._skill_records())
        if unknown:
            raise AgileCodeError("Unknown project PM skills: " + ", ".join(sorted(unknown)))
        self.settings = settings
        await self._persist_settings()
        # Refresh the durable system instruction for subsequent turns.
        messages = await self.app.store.messages(PM_SESSION_ID, include_partial=True)
        system = next((m for m in messages if m["body"].get("role") == "system"), None)
        if system:
            await self.app.store.execute(
                "UPDATE messages SET body=? WHERE id=?",
                (encode({"role": "system", "content": self._system_prompt()}), system["id"]),
            )
        if self.state.get("status") == "running":
            await self._cancel_current()
            if self.settings.mode == "fully_automated":
                self._queue_update("Project Manager settings changed")
            else:
                self._pending_updates.clear()
        await self._persist_state()
        return self.settings.model_dump(mode="json")

    async def resume(self) -> dict:
        recovering_error = self.state.get("current_activity", "").startswith("Error: ")
        self.state["status"] = "running"
        self.state["current_activity"] = "Resuming project management"
        await self._persist_state()
        if self.settings.mode == "fully_automated":
            self._queue_update("Project Manager resumed")
        else:
            history = await self.app.store.messages(PM_SESSION_ID, include_partial=False)
            last = next((row["body"] for row in reversed(history) if row["body"].get("role") != "system"), None)
            if (recovering_error or (last and last.get("role") == "user")) and (not self.turn_task or self.turn_task.done()):
                self.turn_task = asyncio.create_task(self._run_turn(user_initiated=True))
        return await self.snapshot()

    async def pause(self) -> dict:
        self.state["status"] = "paused"
        self.state["current_activity"] = "Paused"
        await self._persist_state()
        self._pending_updates.clear()
        await self._cancel_current()
        return await self.snapshot()

    async def turn_off(self) -> dict:
        self.state["status"] = "off"
        self.state["current_activity"] = "Off"
        await self._persist_state()
        self._pending_updates.clear()
        await self._cancel_current()
        return await self.snapshot()

    async def stop_response(self) -> dict:
        self._pending_updates.clear()
        stopped = await self._cancel_current()
        if stopped and self.state.get("status") == "running":
            self.state["current_activity"] = "Current response stopped"
            await self._persist_state()
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
        # A user direction takes precedence over queued event notifications.
        self._pending_updates.clear()
        await self._cancel_current()
        # Like ticket feedback, a new instruction follows completed tool results.
        async with self.lock:
            await self._dispatch_pending(self._tool_manager())
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
            self._start_turn(self._run_turn(user_initiated=True))
        return await self.snapshot()

    def _require_paused_history(self) -> None:
        if self.state.get("status") == "running":
            raise AgileCodeError("Pause the Project Manager before deleting conversation messages.")

    async def delete_message(self, message_id: str) -> dict:
        self._require_paused_history()
        async with self.lock:
            self._require_paused_history()

            def delete(c):
                row = c.execute(
                    "SELECT * FROM messages WHERE session_id=? AND id=?",
                    (PM_SESSION_ID, message_id),
                ).fetchone()
                if row is None:
                    raise AgileCodeError("Project Manager message not found.")
                body = decode(row["body"], {})
                role = body.get("role")
                if role == "system":
                    raise AgileCodeError("The Project Manager system instructions cannot be deleted.")
                if role == "tool":
                    raise AgileCodeError("Delete the assistant message containing this tool result.")

                ids = [row["id"]]
                if role == "assistant":
                    call_ids = {
                        call.get("id") for call in body.get("tool_calls") or []
                        if call.get("id")
                    }
                    if call_ids:
                        following = c.execute(
                            "SELECT id,request_id,body FROM messages "
                            "WHERE session_id=? AND seq>? ORDER BY seq",
                            (PM_SESSION_ID, row["seq"]),
                        ).fetchall()
                        for item in following:
                            tool_body = decode(item["body"], {})
                            if tool_body.get("role") != "tool":
                                break
                            if (item["request_id"] == row["request_id"]
                                    and tool_body.get("tool_call_id") in call_ids):
                                ids.append(item["id"])
                c.executemany("DELETE FROM messages WHERE id=?", [(identity,) for identity in ids])
                return {"deleted": len(ids)}

            result = await self.app.store.transaction(delete)
        # Request/tool execution audit records are retained; only PM conversation
        # messages disappear from both the GUI and subsequent model context.
        self.app.events.emit(Event(
            "ProjectManagerChanged", session_id=PM_SESSION_ID,
            data={"history_reset": True},
        ))
        return result

    async def clear_conversation(self) -> dict:
        self._require_paused_history()
        async with self.lock:
            self._require_paused_history()

            def clear(c):
                count = c.execute(
                    "DELETE FROM messages WHERE session_id=?", (PM_SESSION_ID,)
                ).rowcount
                c.execute(
                    "INSERT INTO messages(id,session_id,seq,body,request_id,partial,created) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (uid(), PM_SESSION_ID, 1,
                     encode({"role": "system", "content": self._system_prompt()}),
                     None, 0, now()),
                )
                return {"deleted": max(0, count - 1)}

            result = await self.app.store.transaction(clear)
        # PM goals, plan, settings, ticket data and tool audit remain untouched.
        self.app.events.emit(Event(
            "ProjectManagerChanged", session_id=PM_SESSION_ID,
            data={"history_reset": True},
        ))
        return result

    _PROJECT_EVENTS = frozenset({
        "TicketChanged", "TicketDeleted", "RunChanged", "StepAttemptStarted",
        "StepAttemptCompleted", "ExecutionCompleted", "ExecutionInterrupted",
        "ExecutionPaused", "QuestionRequested", "ApprovalRequested",
        "WaitResolved", "IntegrationReady", "IntegrationBlocked",
        "TicketIntegrated", "CleanupFinished", "CheckpointRestored",
        "FeedbackQueued",
    })

    def _on_project_event(self, event: Event) -> None:
        if (event.kind not in self._PROJECT_EVENTS or not event.ticket_id
                or self.state.get("status") != "running"
                or self.settings.mode != "fully_automated"):
            return
        # Our own management-tool actions are already in this response and do
        # not constitute another reason to prompt the model.
        if asyncio.current_task() is self.turn_task:
            return
        self._queue_update(f"{event.kind}: ticket {event.ticket_id}")

    def _queue_update(self, description: str) -> None:
        if self.state.get("status") != "running" or self.settings.mode != "fully_automated":
            return
        self._pending_updates.add(description)
        self._schedule_updates()

    def _start_turn(self, response) -> None:
        self.turn_task = asyncio.create_task(response)
        self.turn_task.add_done_callback(self._on_turn_finished)

    def _schedule_updates(self) -> None:
        if (self._pending_updates
                and self.state.get("status") == "running"
                and self.settings.mode == "fully_automated"
                and (not self.turn_task or self.turn_task.done())):
            self._start_turn(self._respond_to_updates())

    def _on_turn_finished(self, task: asyncio.Task) -> None:
        if self.turn_task is not task:
            return
        self.turn_task = None
        # A ticket may have changed during the response. One follow-up
        # receives those updates; no timer or repeated self-prompting.
        self._schedule_updates()

    async def _respond_to_updates(self) -> None:
        try:
            async with self.lock:
                if self.state.get("status") != "running" or not self._pending_updates:
                    return
                updates = sorted(self._pending_updates)
                self._pending_updates.clear()
                await self._dispatch_pending(self._tool_manager())
                history = await self.app.store.messages(PM_SESSION_ID)
                if any(row["body"].get("role") == "user" for row in history):
                    await self.app.store.message(PM_SESSION_ID, {
                        "role": "user",
                        "content": "Project updates:\n" + "\n".join(updates),
                    })
                    self.app.events.emit(Event("ProjectManagerChanged", session_id=PM_SESSION_ID))
                # Empty conversations are initialized by _execute_turn as usual.
            await self._run_turn(user_initiated=False)
        except Exception as exc:
            await self._report_failure(exc)

    async def _active_ticket_count(self, exclude_ticket_id: str | None = None) -> int:
        if exclude_ticket_id:
            row = await self.app.store.one(
                "SELECT COUNT(*) AS n FROM tickets WHERE state='In Progress' AND archived=0 AND id<>?",
                (exclude_ticket_id,),
            )
        else:
            row = await self.app.store.one(
                "SELECT COUNT(*) AS n FROM tickets WHERE state='In Progress' AND archived=0"
            )
        return int(row["n"] if row else 0)

    async def _update_state(self, args, execution_id):
        for key in ("goals", "plan", "current_activity"):
            if args.get(key) is not None:
                self.state[key] = args[key]
        await self._persist_state()
        return self.state

    async def _list_tickets(self, args, execution_id):
        return await self.app.board(args["query"], include_archived=args["include_archived"])

    async def _get_ticket(self, args, execution_id):
        # This is model context, not the GUI's full ticket detail or export.
        # Do not send snapshots, step contexts, historical discussions, or job logs.
        ticket = await self.app.find_ticket(args["ticket"])
        active = await self.app.active_run(ticket["id"])
        run = active or await self.app.latest_run(ticket["id"])
        result = {
            "ticket": {key: ticket[key] for key in (
                "id", "key", "title", "description", "state", "priority",
                "archived", "provider_id",
            )},
            "run": None,
            "steps": [],
            "dependencies": await self.app.store.query(
                "SELECT t.id,t.key,t.title,t.state FROM tickets t "
                "JOIN dependencies d ON d.depends_on=t.id WHERE d.ticket_id=?",
                (ticket["id"],),
            ),
        }
        if not run:
            return result

        result["run"] = {key: run[key] for key in (
            "id", "state", "step_index", "corrections", "error", "final_oid",
        )}
        latest_attempt = await self.app.store.one(
            "SELECT step_id,status FROM attempts WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
            (run["id"],),
        )
        if latest_attempt:
            result["current_step"] = latest_attempt

        # The run's output pointers identify accepted step results, excluding
        # superseded/correction attempts. Summaries are enough for earlier steps.
        selected = decode(run["outputs"], {})
        if selected:
            rows = await self.app.store.query(
                "SELECT id,result FROM attempts WHERE run_id=? AND result IS NOT NULL",
                (run["id"],),
            )
            by_id = {row["id"]: decode(row["result"], {}) for row in rows}
            for step_id, attempt_id in selected.items():
                output = by_id.get(attempt_id)
                if not output:
                    continue
                result["steps"].append({
                    "step": step_id,
                    "status": output.get("status"),
                    "summary": str(output.get("summary") or "")[:1500],
                })
            if result["steps"]:
                result["latest_step"] = result["steps"][-1]
            if run["state"] in {"completed", "integrated"}:
                for step_id, attempt_id in reversed(list(selected.items())):
                    final = by_id.get(attempt_id)
                    if final:
                        detail = str(final.get("output") or "")
                        result["final_output"] = {
                            "step": step_id,
                            "summary": str(final.get("summary") or "")[:1500],
                            "output": detail[:8000],
                            "truncated": len(detail) > 8000,
                        }
                        break
        return result

    async def _list_teams(self, args, execution_id):
        return await self.app.config.list_teams()

    async def _list_providers(self, args, execution_id):
        return self.app.config.providers_snapshot()

    async def _create_ticket(self, args, execution_id):
        ticket = await self.app.create_ticket(
            args["title"],
            args["description"],
            team_ref=args.get("team_ref"),
            process_id=args.get("process_id"),
            parent_id=args.get("parent_id"),
            provider_id=args.get("provider_id"),
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
        if args["state"] == "In Progress":
            raise AgileCodeError("Use start_ticket to begin execution so the Project Manager concurrency limit is enforced.")
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
        changes = {}
        if args.get("provider_id") is not None:
            self.app.config.get_provider(args["provider_id"])
            changes["provider_id"] = args["provider_id"]
        if not ticket["merge_auto"]:
            changes["merge_auto"] = True
        if changes:
            ticket = await self.app.edit_ticket(
                ticket["id"], expected_revision=ticket["revision"], **changes
            )
        return await self.app.start_ticket(ticket["id"])

    async def _pause_ticket(self, args, execution_id):
        ticket = await self.app.find_ticket(args["ticket"])
        await self.app.pause_run(ticket["id"])
        return {"paused": ticket["key"]}

    async def _resume_ticket(self, args, execution_id):
        ticket = await self.app.find_ticket(args["ticket"])
        if await self._active_ticket_count(exclude_ticket_id=ticket["id"]) >= self.settings.max_concurrent_tickets:
            raise AgileCodeError(
                f"Maximum concurrent tickets ({self.settings.max_concurrent_tickets}) is already reached."
            )
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

    def _pm_tool_settings(self):
        # Project Manager authority is configured by PM settings, not team-member tool ACLs.
        return self.app.config.settings.model_copy(update={"tool_rules": {}, "autonomy": "full"})

    def _tool_manager(self) -> ToolManager:
        scope = Workspace(self.app.root, self.app.root, self.app.global_root)
        tools: list[Tool] = [
            ListFilesTool(scope),
            ReadFileTool(scope),
            CodeSearchTool(scope, self.app.git),
            ProjectGitCommandTool(self.app.git),
            LoadSkillTool(self.app.skills, self._effective_skills()),
            CallbackTool("list_tickets", "List project tickets and their current board state.", ListTicketsArgs, self._list_tickets),
            CallbackTool("get_ticket", "Get ticket details, dependencies, run status and concise step summaries, including the final output when complete.", TicketArgs, self._get_ticket),
            CallbackTool("list_teams", "List configured teams and processes available for ticket assignment.", EmptyArgs, self._list_teams),
            CallbackTool("list_providers", "List global Provider Pool entries and the current default provider.", EmptyArgs, self._list_providers),
            CallbackTool("create_ticket", "Create a backlog ticket. Optionally assign a Provider Pool entry; otherwise the default provider is used when execution starts. PM-created tickets use automatic integration.", CreateTicketArgs, self._create_ticket),
            CallbackTool("edit_ticket", "Edit normal ticket fields such as title, description, team, process, priority, or merge policy.", EditTicketArgs, self._edit_ticket),
            CallbackTool("move_ticket", "Move or reorder a ticket on the board.", MoveTicketArgs, self._move_ticket),
            CallbackTool("add_dependency", "Make one ticket depend on another.", DependencyArgs, self._add_dependency),
            CallbackTool("remove_dependency", "Remove an existing ticket dependency.", DependencyArgs, self._remove_dependency),
            CallbackTool("start_ticket", "Start a Ready ticket, optionally assigning a Provider Pool entry first, subject to the PM concurrency limit.", StartTicketArgs, self._start_ticket),
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
            settings=self._pm_tool_settings,
            session_id=PM_SESSION_ID,
        )

    async def _summarize_context(self, profile, source: str) -> str:
        if not source.strip():
            return source
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

    async def _dispatch_pending(self, manager: ToolManager) -> None:
        """Complete outstanding tool calls before another model or user message."""
        history = await self.app.store.messages(PM_SESSION_ID)
        started = False
        for index, row in enumerate(history):
            body = row["body"]
            if (
                body.get("role") == "user"
                and isinstance(body.get("content"), str)
                and body["content"].strip()
                and not (
                    body["content"].strip().startswith("<tool_response>")
                    and body["content"].strip().endswith("</tool_response>")
                )
            ):
                started = True
            # Historical PM responses written without an initial user task
            # are preserved for display, but must not execute stale tools.
            if not started:
                continue
            calls = body.get("tool_calls") or []
            if not calls:
                continue
            # As in ticket execution, call IDs belong to one assistant response.
            result_ids = set()
            for following in history[index + 1:]:
                if following["body"].get("role") != "tool":
                    break
                if following["request_id"] == row["request_id"]:
                    result_ids.add(following["body"].get("tool_call_id"))
            for call in calls:
                if call["id"] in result_ids:
                    continue
                outcome = await manager.execute(call, row["request_id"])
                await self.app.store.message(PM_SESSION_ID, {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": encode(outcome.payload),
                }, row["request_id"])
                self.app.events.emit(Event("ProjectManagerChanged", session_id=PM_SESSION_ID))

    async def _report_failure(self, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"
        await self.app.store.execute(
            "UPDATE requests SET status='interrupted',error=?,finished=? "
            "WHERE session_id=? AND status='streaming'",
            (message, now(), PM_SESSION_ID),
        )
        await self.app.store.message(PM_SESSION_ID, {"role": "error", "content": message})
        if self.state.get("status") == "running":
            self.state["status"] = "paused"
            self.state["current_activity"] = f"Error: {message}"
            await self._persist_state()
        self.app.events.emit(Event("ProjectManagerChanged", session_id=PM_SESSION_ID))

    async def _run_turn(self, *, user_initiated: bool) -> dict:
        try:
            return await self._execute_turn(user_initiated=user_initiated)
        except Exception as exc:
            await self._report_failure(exc)
            return await self.snapshot()

    async def _execute_turn(self, *, user_initiated: bool) -> dict:
        async with self.lock:
            if self.state.get("status") != "running":
                return await self.snapshot()
            provider = self.app.config.get_provider(self.settings.provider_id)
            profile = await self.app.client.resolve(provider.profile)
            self.state["current_activity"] = (
                "Responding to user direction" if user_initiated else "Evaluating project updates"
            )
            await self._persist_state()
            context_refresh_retry = False
            while self.state.get("status") == "running":
                manager = self._tool_manager()
                # This is the ticket runner's sequence: record outstanding tool
                # results, then build the next request from durable history.
                await self._dispatch_pending(manager)
                if self.state.get("status") != "running":
                    return await self.snapshot()
                history = [
                    row["body"] for row in await self.app.store.messages(PM_SESSION_ID)
                    if row["body"].get("role") != "error"
                ]
                if not any(
                    item.get("role") == "user"
                    and isinstance(item.get("content"), str)
                    and item["content"].strip()
                    and not (
                        item["content"].strip().startswith("<tool_response>")
                        and item["content"].strip().endswith("</tool_response>")
                    )
                    for item in history
                ):
                    # The instruction was absent in some older PM conversations.
                    # Save it once, rather than attaching a temporary prompt.
                    await self.app.store.message(PM_SESSION_ID, {
                        "role": "user",
                        "content": "Begin managing this project. Inspect its current state and take the next useful actions.",
                    })
                messages = [
                    row["body"] for row in await self.app.store.messages(PM_SESSION_ID)
                    if row["body"].get("role") != "error"
                ]
                # Resolve the global role anew on each model turn.
                if messages and messages[0].get("role") == "system":
                    messages[0] = {"role": "system", "content": self._system_prompt()}
                else:
                    messages.insert(0, {"role": "system", "content": self._system_prompt()})
                # Preserve malformed legacy history in the transcript, but
                # begin the model request with its first real user instruction.
                # Ticket conversations always have that ordering.
                first_user = next(
                    (i for i, m in enumerate(messages) if
                     m.get("role") == "user"
                     and isinstance(m.get("content"), str)
                     and m["content"].strip()
                     and not (
                         m["content"].strip().startswith("<tool_response>")
                         and m["content"].strip().endswith("</tool_response>")
                     )),
                    None,
                )
                if first_user is not None and first_user > 1:
                    messages = [messages[0]] + messages[first_user:]
                try:
                    context_window = await self.app.client.context_window(profile)
                    messages, budget = await ContextManager().prepare(
                        messages, context_window,
                        lambda source: self._summarize_context(profile, source),
                    )
                except ContextWindowChanged:
                    if context_refresh_retry:
                        raise
                    context_refresh_retry = True
                    continue
                # The request must contain a valid user turn after compaction.
                # Never hand malformed PM history to the model server.
                if not any(
                    m.get("role") == "user"
                    and isinstance(m.get("content"), str)
                    and m["content"].strip()
                    and not (
                        m["content"].strip().startswith("<tool_response>")
                        and m["content"].strip().endswith("</tool_response>")
                    )
                    for m in messages
                ):
                    raise AgileCodeError("Project Manager model request has no user instruction.")
                request_id = uid()
                await self.app.store.insert("requests", {
                    "id": request_id,
                    "session_id": PM_SESSION_ID,
                    "model": profile.model,
                    "input": encode({"messages": messages, "tools": manager.schemas(),
                                     "budget": budget, "profile": profile.model_dump(mode="json")}),
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
                        response_model=turn.model,
                        usage=encode(turn.usage) if turn.usage is not None else None,
                        finished=now(),
                    )
                    context_refresh_retry = False
                except ContextWindowChanged as exc:
                    await self.app.store.update(
                        "requests", request_id, status="interrupted",
                        error=str(exc) or type(exc).__name__, finished=now(),
                    )
                    if context_refresh_retry:
                        raise
                    context_refresh_retry = True
                    continue
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
                except Exception as exc:
                    body = {"role": "assistant", "content": "".join(partial_text) or None}
                    if partial_reasoning:
                        body["reasoning_content"] = "".join(partial_reasoning)
                    if body.get("content") or body.get("reasoning_content"):
                        await self.app.store.message(PM_SESSION_ID, body, request_id, partial=True)
                    await self.app.store.update(
                        "requests", request_id, status="interrupted",
                        error=str(exc) or type(exc).__name__, finished=now(),
                    )
                    raise
                finally:
                    self.active_generation = None
                    self.app.events.emit(Event(
                        "ProjectManagerGenerationFinished",
                        session_id=PM_SESSION_ID,
                        data={"request_id": request_id},
                    ))

                if turn.finish_reason == "length":
                    raise AgileCodeError(
                        "Project Manager response was cut off at the model output limit. "
                        "Partial output was retained."
                    )
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

                # The next iteration records the returned tool calls before
                # the model is asked for another response, just as tickets do.
        return await self.snapshot()
