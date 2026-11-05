"""Persistent event-driven project manager."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .context import ContextManager
from agilecode.runtime.conversation import ConversationConfig, ConversationEngine, conversation_request
from agilecode.runtime import conversation_records
from agilecode.adapters.model import ContextWindowChanged
from .pm_profiles import PMProfile, PMProfileLibrary
from agilecode.runtime.pm_tools import EmptyArgs, TicketArgs, StartTicketArgs, ListTicketsArgs, EpicArgs, CreateEpicArgs, UpdateEpicArgs, CreateTicketArgs, EditTicketArgs, MoveTicketArgs, DependencyArgs, AnswerTicketArgs, PMStateArgs, GitArgs, CallbackTool, ProjectGitCommandTool
from agilecode.runtime.tool_factories import project_manager_tools, pm_tool_settings, pm_currently_exposed
from .types import AgileCodeError, DataModel, Event, now, uid
from agilecode.adapters.store import decode, encode
from agilecode.adapters.workspace import Workspace


from agilecode.runtime.pm_tools import PM_SESSION_ID


class ProjectManagerSettings(DataModel):
    mode: Literal["restricted", "fully_automated"] = "restricted"
    provider_id: str | None = None
    profile_id: str | None = None
    project_instructions: str = Field(default="")
    project_skills: list[str] = Field(default_factory=list)
    max_concurrent_tickets: int = Field(default=4, ge=1)
    wait_for_current_tickets: bool = False
    answer_ticket_questions: bool = True
    uncertainty: Literal["continue", "ask"] = "continue"
    allow_archive: bool = False
    allow_delete: bool = False


class ProjectManager:
    """One persistent project-level manager, independent from ticket runtimes."""

    def __init__(self, app):
        self.app = app
        self.root = app.storage / "project_manager"
        self.settings_path = self.root / "settings.json"
        self.state_path = self.root / "state.json"
        self.settings = ProjectManagerSettings()
        self.profiles = app.profiles if hasattr(app, "profiles") else PMProfileLibrary(app.global_root)
        self.state: dict[str, Any] = {}
        self._event_unsubscribe = None
        self._closing = False
        self.engine = ConversationEngine(app.store, app.client)
        self.lock = self.engine.lock(PM_SESSION_ID)
        self.state_lock = conversation_records.session_lock(app.store, "pm-state")

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "status": "paused",
            "goals": [],
            "plan": [],
            "current_activity": "Idle",
            "updated": now(),
            "_control_revision": 0,
            "_applied_operations": [],
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
        from agilecode.adapters.config_lock import atomic_json
        atomic_json(path, value)

    def _migrate_legacy_storage(self) -> None:
        """Move existing PM data to the canonical project storage on startup.

        The engine holds the project lock during startup. Do not merge or
        overwrite two competing copies of Project Manager state.
        """
        legacy_parent = self.app.root / ".agile_code"
        legacy = legacy_parent / "project_manager"
        if legacy_parent.is_symlink() or legacy.is_symlink() or self.root.is_symlink():
            raise AgileCodeError("Project Manager storage must not be a symlink.")
        if legacy.exists():
            if not legacy.is_dir():
                raise AgileCodeError(f"Legacy Project Manager storage is not a directory: {legacy}")
            if self.root.exists():
                raise AgileCodeError(
                    "Project Manager data exists in both "
                    f"{legacy} and {self.root}. Neither directory was modified; "
                    "reconcile the two directories before starting AgileCode."
                )
            self.root.parent.mkdir(parents=True, exist_ok=True)
            try:
                legacy.rename(self.root)
            except OSError as exc:
                raise AgileCodeError(
                    f"Could not migrate Project Manager data from {legacy} to {self.root}: {exc}"
                ) from exc

        # An empty former parent can go; unrelated legacy files must remain.
        if legacy_parent.is_dir():
            try:
                legacy_parent.rmdir()
            except OSError:
                pass

    async def migrate_storage(self) -> None:
        """Explicit startup migration, suitable for the maintenance reservation."""
        await asyncio.to_thread(self._migrate_legacy_storage)
        await self._ensure_legacy_git_exclude()
        if self.state_path.exists():
            raw = await asyncio.to_thread(self._load_json, self.state_path, {})
            if "_control_revision" not in raw or "_applied_operations" not in raw:
                # The one state writer initializes metadata without changing the
                # pre-upgrade running/paused policy. Ordinary open handles restart.
                await self._change_state({}, control=False)

    async def open(self, *, migrate=True) -> None:
        if migrate:
            await self.migrate_storage()
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
        self._closing = True
        if self._event_unsubscribe:
            self._event_unsubscribe()
            self._event_unsubscribe = None
        await self._cancel_current()
        if self.state.get("status") == "running":
            await self._change_state({"status": "paused", "current_activity": "Paused because AgileCode closed"}, control=True)

    async def _ensure_legacy_git_exclude(self) -> None:
        # Keep any unrelated leftovers ignored; new PM data lives in .agilecode/.
        if not (self.app.root / ".agile_code").is_dir():
            return
        result = await self.app.git.run("rev-parse", "--git-path", "info/exclude")
        path = Path(result.text)
        if not path.is_absolute():
            path = (self.app.root / path).resolve()
        def ensure():
            path.parent.mkdir(parents=True, exist_ok=True)
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            entries = {line.strip() for line in text.splitlines()}
            if ".agile_code/" not in entries:
                from agilecode.adapters.config_lock import atomic_bytes
                mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
                atomic_bytes(path, (text + ("\n" if text and not text.endswith("\n") else "") + ".agile_code/\n").encode(), mode=mode)
        await asyncio.to_thread(ensure)

    async def _persist_settings(self) -> None:
        await asyncio.to_thread(
            self._write_json, self.settings_path, self.settings.model_dump(mode="json")
        )

    async def _change_state(self, changes: dict, *, control=False, operation_id=None, expected_revision=None):
        """The only state.json writer. Its reservation outlives cancelled awaits."""
        async with self.state_lock:
            loaded = await asyncio.to_thread(self._load_json, self.state_path, self.state or self._default_state())
            current = {**self._default_state(), **(loaded or {})}
            applied = list(current.get("_applied_operations", []))
            if operation_id and operation_id in applied:
                self.state = current
                return "applied"
            if expected_revision is not None and current["_control_revision"] != expected_revision:
                self.state = current
                return "superseded"
            if control:
                revision = await conversation_records.bump_control(self.app.store, PM_SESSION_ID)
            else:
                revision = current["_control_revision"]
            # Snapshot mutable values before entering the filesystem worker.
            updated = json.loads(json.dumps({**current, **{k: v for k, v in changes.items() if not k.startswith("_")}}))
            updated["updated"] = now()
            updated["_control_revision"] = revision
            if operation_id:
                applied.append(operation_id)
            updated["_applied_operations"] = applied
            _, cancelled = await conversation_records.owned_finalization(asyncio.to_thread(self._write_json, self.state_path, updated))
            self.state = updated
            self.app.events.emit(Event("ProjectManagerChanged", session_id=PM_SESSION_ID,
                data={"state": json.loads(json.dumps(updated)), "settings": self.settings.model_dump(mode="json")}))
            if cancelled:
                raise asyncio.CancelledError
            return "applied"

    async def _persist_state(self) -> None:
        # Compatibility for initialization and explicit state save callers.
        await self._change_state(dict(self.state), control=True)

    async def _reconcile_state_intents(self):
        pending = await self.app.store.query("SELECT * FROM operations WHERE kind='conversation_state' AND owner_id=? AND status='pending' ORDER BY created,id", (PM_SESSION_ID,))
        for operation in pending:
            data = decode(operation["data"])
            disposition = await self._change_state(data["changes"], operation_id=operation["id"], expected_revision=data["expected_revision"])
            await self.app.store.update("operations", operation["id"], status=disposition, updated=now())
        # Even saved-receipt recovery refreshes the authoritative file snapshot.
        async with self.state_lock:
            self.state = {**self._default_state(), **await asyncio.to_thread(self._load_json, self.state_path, self.state)}
        self.app.events.emit(Event("ProjectManagerChanged", session_id=PM_SESSION_ID))

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
        return self.app.catalog.list_profiles()

    def save_profile(self, raw: dict, **options) -> dict:
        saved = self.app.catalog.save_profile(raw, **options)
        self.app.events.emit(Event("ProjectManagerChanged", session_id=PM_SESSION_ID))
        return saved

    async def delete_profile(self, identity: str, **options) -> dict:
        self.app.catalog.delete_profile(identity, **options)
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
            "When no tickets are running, inspect the codebase and available engineering "
            "findings to identify the next worthwhile task, which need not involve coding."
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
            "You are AgileCode's persistent Project Manager. Coordinate project objectives, engineering "
            "teams, processes, and tickets without directly implementing source changes yourself. "
            "A ticket is a bounded statement of work assigned to an appropriate team and process; "
            "it is not inherently a coding task. Tickets may deliver investigations, evidence, "
            "requirements, decisions, designs, implementations, integration, verification, or "
            "other engineering outcomes. Judge them by their assigned deliverables. "
            "Scale engineering depth to the actual ticket and established project context. "
            "Treat the ticket as a bounded unit of work: do not enlarge its scope merely because "
            "requirements, architecture, planning, or verification roles are involved. "
            "Examine the user's request and any existing requirements before delegating work. "
            "For sufficiently clear work, proceed without an extra requirements-definition ticket. "
            "When important behavior, scope, constraints, or acceptance decisions genuinely remain "
            "unresolved, delegate a focused requirements task to the existing Requirements "
            "Engineering team (team_ref='builtin:requirements-engineering') using the Define "
            "Requirements process (process_id='requirements-definition'). Reuse adequate existing "
            "requirements; address only the gaps rather than inventing additional obligations. "
            "State the needed decision, known constraints, proportionate document deliverable, and "
            "completion conditions. Read that deliverable before deciding further work; do not "
            "populate a speculative implementation backlog. "
            "Manage engineering iteratively: examine current objectives, system evidence, and recent "
            "ticket results; identify the next useful question or capability; delegate a few justified "
            "tasks; examine their results; then update the engineering direction and decide what follows. "
            "Each engineering activity can change which future work is needed. Do not treat an initial "
            "work breakdown or proposed sequence as a contract to execute from start to finish. "
            "Maintain durable goals and a *provisional, near-term* plan using "
            "update_project_manager_state. Look a few steps ahead when supported by existing evidence, "
            "but do not generate a complete future ticket queue or commit to designs not yet investigated. "
            "Reconsider the plan and outstanding assumptions after meaningful findings, including "
            "requirements and architecture results, rather than following old tickets mechanically. "
            "Select teams and processes according to the kind of work; unspecified selections use "
            "project defaults (often development) and may not be appropriate. Consult and load relevant "
            "enabled systems-engineering skills and delegate specialized activities to their teams "
            "when useful. There is no mandatory engineering-phase sequence or readiness gate; a "
            "well-understood change may proceed directly to implementation, and several activities "
            "may proceed in parallel. "
            "Epics are organizational records for bounded, cohesive engineering outcomes with "
            "individually assessable completion criteria. Create or refine epic boundaries as "
            "understanding improves, separating distinct outcomes rather than making one catch-all "
            "epic. Do not populate epics with speculative end-to-end implementation tickets. "
            "Tickets within an epic use its ID and a rough order; numbering is not a dependency. "
            "Standalone tickets need no epic. Ticket completion does not by itself prove epic "
            "completion; assess the objective against the evidence. "
            "TECHNICAL ERRORS: Model-stream interruptions, context summarization, and output-limit "
            "events are conversation execution issues, not evidence that the ticket, assigned team, "
            "or engineering approach is defective. Repeated technical errors alone never justify "
            "cancelling, deleting, or recreating tickets. Preserve existing work and let the "
            "conversation engine recover; distinguish these events from actual engineering findings. "
            "Record the reason for any deliberate ticket cancellation using cancel_ticket. "
            "Use add_ticket_note to preserve other ticket decisions or relevant context. "
            "Use resume_ticket to continue either a paused or a cancelled run from its retained "
            "progress. Do not use start_ticket to replace cancelled work when resumption is intended. "
            "Normal project-management decisions remain yours when engineering evidence warrants them. "
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
        async with conversation_records.policy_lock(self.app.store):
            # Publish only after the durable write succeeds, under the same
            # reservation used for the final tool exposure/dispatch check.
            await conversation_records.owned_finalization(asyncio.to_thread(
                self._write_json, self.settings_path, settings.model_dump(mode="json")))
            self.settings = settings
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
        await self._change_state({}, control=True)
        if self.state.get("status") == "running" and self.settings.mode == "fully_automated":
            self._launch_delivery(self._deliver_update("Project Manager settings changed"))
        return self.settings.model_dump(mode="json")

    async def resume(self) -> dict:
        if self._closing:
            raise AgileCodeError("The Project Manager is closing; new responses are blocked.")
        if not self.app.execution_enabled:
            raise AgileCodeError("This host does not run execution.")
        recovering_error = self.state.get("current_activity", "").startswith("Error: ")
        await self._change_state({"status": "running", "current_activity": "Resuming project management"}, control=True)
        if self.settings.mode == "fully_automated":
            self._launch_delivery(self._deliver_update("Project Manager resumed"))
        else:
            pending_input = await self.app.store.one(
                "SELECT id FROM operations WHERE kind='conversation_input' AND owner_id=? "
                "AND result IS NULL LIMIT 1", (PM_SESSION_ID,))
            history = await self.app.store.messages(PM_SESSION_ID, include_partial=False)
            last = next((row["body"] for row in reversed(history) if row["body"].get("role") != "system"), None)
            if pending_input or recovering_error or (last and last.get("role") == "user"):
                self._launch_delivery(self._run_turn(user_initiated=True))
        return await self.snapshot()

    async def pause(self) -> dict:
        self.engine.stop(PM_SESSION_ID)
        await self._change_state({"status": "paused", "current_activity": "Paused"}, control=True)
        await self._cancel_current()
        return await self.snapshot()

    async def turn_off(self) -> dict:
        self.engine.stop(PM_SESSION_ID)
        await self._change_state({"status": "off", "current_activity": "Off"}, control=True)
        await self._cancel_current()
        return await self.snapshot()

    async def stop_response(self) -> dict:
        stopped = await self._cancel_current()
        if stopped:
            await self._change_state({"current_activity": "Current response stopped"}, control=True)
        return {"stopped": stopped}

    async def _cancel_current(self) -> bool:
        return await self.engine.cancel(PM_SESSION_ID)

    @conversation_request(lambda self, *args, **kwargs: PM_SESSION_ID)
    async def message(self, text: str, input_id: str | None = None) -> dict:
        if self._closing:
            raise AgileCodeError("The Project Manager is closing; new responses are blocked.")
        if not text.strip():
            raise AgileCodeError("Enter a Project Manager message.")
        # Durable acceptance signals Interrupt & Correct through the shared engine.
        # No autonomous event messages are discarded or given a separate queue.
        was_running = self.state.get("status") == "running"
        accepted_revision = self.state.get("_control_revision", 0)
        config = self._conversation_config(True)
        receipt = await self.engine.accept_input(config, text, input_id)
        if receipt.get("outcome") is not None:
            await self.engine.run_turn(config, receipt)
            return {**await self.snapshot(), "input": receipt}
        if was_running:
            await self._change_state({"status": "running"}, control=True,
                                     expected_revision=accepted_revision)
        if self.state.get("status") == "running":
            await self._run_turn(user_initiated=True, trigger=receipt)
        return {**await self.snapshot(), "input": receipt}

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
        "SourceChanged", "FeedbackQueued",
        "ExecutionTechnicalFailure", "ExecutionTechnicalRecovered",
    })

    def _launch_delivery(self, response) -> None:
        # Event callbacks are synchronous; only the conversation engine owns
        # the execution and interruption of the resulting asynchronous work.
        if self._closing:
            response.close()
            return
        task = asyncio.create_task(response)
        def completed(finished):
            if finished.cancelled():
                return
            try:
                finished.result()
            except Exception:
                logging.getLogger(__name__).exception("Project Manager message delivery failed")
        task.add_done_callback(completed)

    def _on_project_event(self, event: Event) -> None:
        if (event.kind not in self._PROJECT_EVENTS or not event.ticket_id
                or self.state.get("status") != "running"
                or self.settings.mode != "fully_automated"):
            return
        if event.kind == "TicketChanged" and (event.data or {}).get("redundant_pm_notice"):
            return
        # PM-owned tool effects are already reflected in the current tool
        # exchange. Independently completed tickets still notify the PM.
        if conversation_records.active_session.get() == conversation_records.session_identity(
                self.app.store, PM_SESSION_ID):
            return
        details = event.data or {}
        if event.kind in {"ExecutionTechnicalFailure", "ExecutionTechnicalRecovered"}:
            stage = "retrying automatically" if event.kind == "ExecutionTechnicalFailure" else "recovered"
            description = (f"TECHNICAL EXECUTION UPDATE ({stage}): ticket {event.ticket_id}; "
                           f"issue={details.get('issue', 'unknown')}; {details.get('message', '')}. "
                           "The ticket's work is preserved; this is not an engineering failure.")
        elif event.kind == "ExecutionInterrupted":
            category = details.get("category", "execution")
            description = (f"ExecutionInterrupted: ticket {event.ticket_id}; "
                           f"category={category}; {details.get('message', '')}")
        else:
            description = f"{event.kind}: ticket {event.ticket_id}"
        self._launch_delivery(self._deliver_update(description))

    @conversation_request(lambda self, *args, **kwargs: PM_SESSION_ID)
    async def _deliver_update(self, description: str) -> None:
        if (self._closing or self.state.get("status") != "running"
                or self.settings.mode != "fully_automated"):
            return
        config = self._conversation_config(False)
        receipt = await self.engine.accept_input(
            config, "PROJECT STATUS UPDATE (informational):\n" + description, interrupt=False)
        await self.engine.run_turn(config, receipt)

    async def _active_ticket_count(self, exclude_ticket_id=None):
        return await self.app.ticket_queries.active_ticket_count(exclude_ticket_id=exclude_ticket_id)

    async def _update_state(self, args, execution_id):
        await self._change_state({key: args[key] for key in ("goals", "plan", "current_activity") if args.get(key) is not None},
                                 operation_id="pm-tool-state:" + execution_id)
        return self.state

    async def _list_tickets(self, args, execution_id):
        return await self.app.board(args["query"], include_archived=args["include_archived"])

    async def _get_ticket(self, args, execution_id):
        return await self.app.project_queries.pm_ticket(args["ticket"])

    async def _create_epic(self, args, execution_id):
        return await self.app.create_epic(
            args["epic_id"], args["title"],
            args["description"], args["completion_criteria"], request_id=execution_id,
        )

    async def _get_epic(self, args, execution_id):
        return await self.app.get_epic(args["epic_id"])

    async def _list_epics(self, args, execution_id):
        return await self.app.list_epics()

    async def _update_epic(self, args, execution_id):
        changes = {
            key: value for key, value in args.items()
            if key != "epic_id" and value is not None
        }
        return await self.app.update_epic(args["epic_id"], **changes)

    async def _delete_epic(self, args, execution_id):
        return await self.app.delete_epic(args["epic_id"], request_id=execution_id)

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
            epic_id=args.get("epic_id"),
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

    async def _add_ticket_note(self, args, execution_id):
        ticket = await self.app.find_ticket(args["ticket"])
        return await self.app.add_ticket_note(
            ticket["id"], args["text"], author="project_manager", request_id=execution_id,
        )

    def _ticket_caller(self):
        return {"scope": "project_manager", "admission_limit": self.settings.max_concurrent_tickets}

    async def _move_ticket(self, args, execution_id):
        ticket = await self.app.find_ticket(args["ticket"])
        return await self.app.move_ticket(ticket["id"], args["state"], expected_revision=ticket["revision"],
            position=args.get("position"), request_id=execution_id, caller=self._ticket_caller())

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
        ticket = await self.app.find_ticket(args["ticket"])
        return await self.app.start_ticket(ticket["id"], request_id=execution_id,
            caller=self._ticket_caller(), provider_id=args.get("provider_id"))

    async def _pause_ticket(self, args, execution_id):
        ticket = await self.app.find_ticket(args["ticket"])
        await self.app.pause_run(ticket["id"])
        return {"paused": ticket["key"]}

    async def _resume_ticket(self, args, execution_id):
        ticket = await self.app.find_ticket(args["ticket"])
        await self.app.resume_run(ticket["id"], caller=self._ticket_caller(), request_id=execution_id)
        return {"resumed": ticket["key"]}

    async def _cancel_ticket(self, args, execution_id):
        ticket = await self.app.find_ticket(args["ticket"])
        await self.app.cancel_run(ticket["id"])
        await self.app.add_ticket_note(
            ticket["id"], "Cancelled run: " + args["reason"].strip(),
            author="project_manager", request_id=execution_id + ":cancellation-note",
        )
        return {"cancelled": ticket["key"]}

    async def _archive_ticket(self, args, execution_id):
        ticket = await self.app.find_ticket(args["ticket"])
        await self.app.archive_ticket(ticket["id"], args["archived"])
        return {"archived" if args["archived"] else "unarchived": ticket["key"]}

    async def _delete_ticket(self, args, execution_id):
        ticket = await self.app.find_ticket(args["ticket"])
        return await self.app.delete_ticket(ticket["id"], request_id=execution_id)

    async def _pending_questions(self, args, execution_id):
        return await self.app.project_queries.pm_pending_waits()

    async def _answer_ticket(self, args, execution_id):
        await self.app.answer(args["wait_id"], args["answer"])
        return {"answered": args["wait_id"]}

    def _pm_tool_settings(self):
        return pm_tool_settings(self)

    def _tool_manager(self):
        return project_manager_tools(self)

    def _currently_exposed(self, name):
        return pm_currently_exposed(self, name)

    def _conversation_config(self, user_initiated):
        expected = {"revision": None}
        async def validate():
            if self._closing or self.state.get("status") != "running":
                raise AgileCodeError("Project Manager is paused or off.")
        async def check():
            async with self.state_lock:
                self.state = {**self._default_state(), **await asyncio.to_thread(self._load_json, self.state_path, self.state)}
            if self.state.get("status") != "running":
                raise asyncio.CancelledError
        async def admit():
            expected["revision"] = self.state.get("_control_revision", 0)
            await self._change_state({"current_activity": "Responding to user direction" if user_initiated else "Evaluating project updates"},
                                     expected_revision=expected["revision"])
        async def initialize():
            history = await self.app.store.messages(PM_SESSION_ID, include_partial=True)
            if not any(self._real_user(row["body"]) for row in history):
                await self.app.store.message(PM_SESSION_ID, {"role": "user", "content":
                    "Begin managing this project. Examine existing work and evidence, update the near-term plan, and identify the next justified engineering tasks."})
        def view(messages):
            system = {"role": "system", "content": self._system_prompt()}
            first_user = next((i for i, message in enumerate(messages) if self._real_user(message)), None)
            return [system] + (messages[first_user:] if first_user is not None else [])
        def emit(kind, data):
            names = {"started": "ProjectManagerGenerationStarted", "finished": "ProjectManagerGenerationFinished",
                     "delta": "ProjectManagerDelta", "changed": "ProjectManagerChanged",
                     "turn_started": "ConversationTurnStarted", "turn_finished": "ConversationTurnFinished",
                      "turn_invalidated": "ConversationTurnInvalidated",
                      "technical_failure": "ProjectManagerTechnicalFailure",
                      "technical_recovered": "ProjectManagerTechnicalRecovered"}
            self.app.events.emit(Event(names[kind], session_id=PM_SESSION_ID,
                                       data={k: v for k, v in data.items() if k != "task"}))
        async def intent(outcome, turn_id):
            if expected["revision"] is None:
                return None
            status = outcome["status"]
            if status == "completed":
                # Asking for input leaves the conversation idle, not paused.
                changes = {"current_activity": "Waiting for user direction"} if (
                    self.settings.uncertainty == "ask" and "NEEDS_USER:" in outcome.get("content", "")) else {"current_activity": "Monitoring project"}
            elif status in {"error", "truncated", "blocked"}:
                error = outcome.get("error") or "Project Manager response was cut off at the model output limit. Partial output was retained."
                await self.app.store.message(PM_SESSION_ID, {"role": "error", "content": error})
                changes = {"status": "paused", "current_activity": "Error: " + error}
            else:
                return None
            return {"expected_revision": expected["revision"], "changes": changes, "turn_id": turn_id}
        def validate_request(messages):
            if not any(self._real_user(message) for message in messages):
                raise AgileCodeError("Project Manager model request has no user instruction.")
        return ConversationConfig(PM_SESSION_ID,
            profile=lambda: self.app.client.resolve(self.app.config.get_provider(self.settings.provider_id).profile),
            tools=self._tool_manager, initialize=initialize, request_view=view, validate=validate, check=check,
            emit=emit, on_admit=admit, reconcile=self._reconcile_state_intents, outcome_intent=intent,
            skip_before_user=True, validate_request=validate_request,
            summary_instruction="Summarize the preceding Project Manager conversation faithfully and concisely, preserving the important information needed to continue. Return only the summary.")

    @staticmethod
    def _real_user(message):
        content = message.get("content") or ""
        return message.get("role") == "user" and isinstance(content, str) and content.strip() and not (
            content.strip().startswith("<tool_response>") and content.strip().endswith("</tool_response>"))

    async def _dispatch_pending(self, manager):
        # Compatibility for callers/tests; all exchange logic is in the engine.
        await self.engine.close_outstanding_calls(self._conversation_config(False), manager,
            dispatch=self.state.get("status") == "running")

    @conversation_request(lambda self, *args, **kwargs: PM_SESSION_ID)
    async def _run_turn(self, *, user_initiated: bool, trigger=None) -> dict:
        try:
            return await self._execute_turn(user_initiated=user_initiated, trigger=trigger)
        except Exception:
            # The shared engine already settled the error before admission unlock.
            return await self.snapshot()

    async def _execute_turn(self, *, user_initiated: bool, trigger=None) -> dict:
        if self.state.get("status") != "running":
            return await self.snapshot()
        await self.engine.run_turn(self._conversation_config(user_initiated), trigger=trigger)
        return await self.snapshot()
