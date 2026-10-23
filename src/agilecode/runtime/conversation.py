"""One durable request/stream/tool/continuation engine for every agent role."""
from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
from typing import Any, Callable

from agilecode.application.cancellation import (CancellationToken, bind_cancellation, cancellation_checkpoint, current_cancellation)
from agilecode.adapters.model import ContextWindowChanged, ModelError, ModelUnavailable
from agilecode.adapters.store import decode, encode
from agilecode.core.context import ContextManager, SummaryFailure
from agilecode.core.types import AgileCodeError, GenerationStopped, NeedsReview, StepResult, now, uid
from . import conversation_records as records


async def invoke(fn, *args):
    if fn is None:
        return None
    value = fn(*args)
    return await value if inspect.isawaitable(value) else value


@dataclass
class ConversationConfig:
    session_id: str
    profile: Callable
    tools: Callable
    initialize: Callable | None = None
    prepare: Callable | None = None
    request_view: Callable | None = None
    validate: Callable | None = None
    check: Callable | None = None
    emit: Callable | None = None
    reconcile: Callable | None = None
    on_admit: Callable | None = None
    outcome_intent: Callable | None = None
    after_generation: Callable | None = None
    continuation: str | None = None
    mixed_control_names: frozenset[str] = frozenset()
    skip_before_user: bool = False
    tag: str | None = None
    validate_in: Callable | None = None
    validate_request: Callable | None = None
    summary_instruction: str = (
        "Summarize the preceding conversation faithfully and concisely, retaining the important "
        "information needed to continue. Summarize verbose tool results rather than reproducing "
        "them in full. Do not invent details. Return only the summary."
    )


class InputChanged(Exception):
    pass


@dataclass
class ActiveTurn:
    task: asyncio.Task
    tag: str | None
    token: CancellationToken = field(default_factory=CancellationToken)
    reason: str | None = None
    input_version: int = -1
    turn_id: str = field(default_factory=uid)
    expected_cancellations: int = 0
    emit: Callable | None = None
    notifications: list[asyncio.Task] = field(default_factory=list)


def conversation_request(session_id):
    """Register a role request before its first lookup or input-acceptance await."""
    def decorate(fn):
        @wraps(fn)
        async def owned(self, *args, **kwargs):
            if self._closing:
                raise AgileCodeError("The conversation is closing; new responses are blocked.")
            with self.engine.request(session_id(self, *args, **kwargs)):
                return await fn(self, *args, **kwargs)
        return owned
    return decorate


class ConversationEngine:
    def __init__(self, store, client, context_manager=None):
        self.store, self.client = store, client
        self.context = context_manager or ContextManager()
        self.controller = records.controller(store)
        self.active_generations = self.controller.generations
        self.stop_requested = self.controller.stop_requested
        self.active_turns = self.controller.turns

    def lock(self, session):
        return records.session_lock(self.store, session)

    def bind_session(self, alias, session):
        """Bind a validated record alias without changing its durable session ID."""
        self.controller.aliases[alias] = session

    def session_for(self, alias):
        return self.controller.aliases.get(alias, alias)

    def is_busy(self):
        return any(not task.done() for groups in (self.controller.requests, self.controller.admitted)
                   for tasks in groups.values() for task in tasks)

    def _session_tasks(self, session):
        session = self.session_for(session)
        return {task for groups in (self.controller.requests, self.controller.admitted)
                for key, tasks in groups.items() if self.session_for(key) == session for task in tasks}

    def is_active(self, session):
        active = self.active_turns.get(session)
        return bool(active and not active.task.done())

    def activity(self, session):
        active = self.active_turns.get(session)
        return {"turn_active": bool(active and not active.task.done()),
                "turn_id": active.turn_id if active else None,
                "activity_revision": self.controller.activity_revisions.get(session, 0)}

    def activity_snapshot(self):
        sessions = set(self.controller.activity_revisions) | set(self.active_turns)
        activity = {session: self.activity(session) for session in sessions}
        return {"active_conversations": {session: state["turn_id"] for session, state in activity.items()
                                          if state["turn_active"]},
                "conversation_activity_revisions": {session: state["activity_revision"]
                                                    for session, state in activity.items()}}

    @contextmanager
    def request(self, session):
        task = asyncio.current_task()
        requests = self.controller.requests.setdefault(session, set())
        requests.add(task)
        try:
            yield
        finally:
            requests.discard(task)
            if not requests:
                self.controller.requests.pop(session, None)

    def request_tasks(self, *, prefix=""):
        return {task for session, tasks in self.controller.requests.items()
                if session.startswith(prefix) for task in tasks}

    async def cancel_requests(self, *, prefix=""):
        cancelled = await self.cancel_tasks(self.request_tasks(prefix=prefix))
        for session in tuple(self.controller.retired_scopes):
            if session.startswith(prefix):
                cancelled = await self.cancel(session) or cancelled
        return cancelled

    async def accept_input(self, config, text, input_id=None, *, interrupt=True):
        receipt = await records.accept_input(
            self.store, config.session_id, text, input_id, config.validate_in, interrupt=interrupt)
        self.input_accepted(config.session_id, receipt, interrupt=interrupt)
        # Publication follows durable acceptance, never physical cleanup.
        await self._emit(config, "changed", input=receipt)
        return receipt

    def input_accepted(self, session, receipt, *, interrupt=True):
        active = self.active_turns.get(session)
        retired = self.controller.retired_scopes.get(session)
        if interrupt and receipt.get("outcome") is None and retired:
            retired.cancel("input")
            retired.begin_stop()
        if (interrupt and receipt.get("outcome") is None and active
                and receipt["version"] > active.input_version):
            return self.interrupt(session, reason="input")
        return False

    def interrupt(self, session, *, reason="input"):
        """Signal the whole turn once; cleanup and correction replay belong here."""
        session = self.session_for(session)
        active = self.active_turns.get(session)
        if not active or active.task.done():
            retired = self.controller.retired_scopes.get(session)
            if retired:
                retired.cancel(reason)
                retired.begin_stop()
                return True
            return False
        if reason == "stop":
            self.stop_requested.add(session)
        if active.reason != "stop":
            active.reason = reason
        already_cancelled = active.token.cancelled
        active.token.cancel(reason)
        active.token.begin_stop()
        if not already_cancelled and active.emit:
            notice = active.emit("turn_invalidated", {"turn_id": active.turn_id, "reason": reason})
            if inspect.isawaitable(notice):
                active.notifications.append(asyncio.create_task(notice))
        if not already_cancelled and active.task is not asyncio.current_task():
            active.task.cancel()
            active.expected_cancellations = active.task.cancelling()
        return True

    def interrupt_tag(self, tag, *, reason="input"):
        sessions = [session for session, active in tuple(self.active_turns.items())
                    if active.tag == tag and not active.task.done()]
        for session in sessions:
            self.stop(session) if reason == "stop" else self.interrupt(session, reason=reason)
        return sessions[0] if sessions else None

    def stop(self, session):
        session = self.session_for(session)
        stopped = self.interrupt(session, reason="stop")
        active = self.active_turns.get(session)
        tasks = self._session_tasks(session)
        for task in tasks:
            if not task.done() and task is not asyncio.current_task() and (not active or task is not active.task):
                task.cancel()
                stopped = True
        return stopped

    async def cancel_tasks(self, tasks):
        """Shared shutdown path, including adapters admitted before session lookup."""
        tasks = {task for task in tasks if task and not task.done() and task is not asyncio.current_task()}
        active_tasks = {active.task: session for session, active in self.active_turns.items()}
        for task in tasks:
            if task in active_tasks:
                self.stop(active_tasks[task])
            else:
                task.cancel()
        if tasks:
            await records.owned_finalization(asyncio.gather(*tasks, return_exceptions=True))
        return bool(tasks)

    async def cancel(self, session, *, pending=()):
        tasks = self._session_tasks(session) | set(pending)
        self.stop(session)
        cancelled = await self.cancel_tasks(tasks)
        retired = self.controller.retired_scopes.get(self.session_for(session))
        if retired:
            errors, _ = await records.owned_finalization(retired.stop_and_wait())
            if errors:
                raise AgileCodeError("Interrupted work has not been confirmed stopped: " + "; ".join(errors))
            self.controller.retired_scopes.pop(self.session_for(session), None)
        return cancelled or retired is not None

    @asynccontextmanager
    async def _ownership(self, config):
        session, task = config.session_id, asyncio.current_task()
        admitted = self.controller.admitted.setdefault(session, set())
        admitted.add(task)
        try:
            async with self.lock(session):
                retired = self.controller.retired_scopes.get(session)
                if retired:
                    errors, _ = await records.owned_finalization(retired.stop_and_wait())
                    if errors:
                        raise AgileCodeError("Earlier response cleanup failed: " + "; ".join(errors))
                    self.controller.retired_scopes.pop(session, None)
                active = ActiveTurn(task, config.tag, expected_cancellations=task.cancelling(), emit=config.emit)
                self.active_turns[session] = active
                binding = records.active_session.set(records.session_identity(self.store, session))
                self.controller.activity_revisions[session] = self.controller.activity_revisions.get(session, 0) + 1
                try:
                    await self._emit(config, "turn_started", **self.activity(session))
                    yield active
                finally:
                    if active.token.cancelled:
                        await records.owned_finalization(active.token.stop_and_wait())
                    if active.notifications:
                        await records.owned_finalization(asyncio.gather(*active.notifications, return_exceptions=True))
                    if active.token.has_children or active.token.termination_errors:
                        retired_token = active.token
                        self.controller.retired_scopes[session] = retired_token
                        def prune():
                            if self.controller.retired_scopes.get(session) is retired_token:
                                self.controller.retired_scopes.pop(session, None)
                        retired_token.on_empty = prune
                    self.active_turns.pop(session, None)
                    self.controller.activity_revisions[session] += 1
                    try:
                        await records.owned_finalization(self._emit(config, "turn_finished",
                            **{**self.activity(session), "turn_id": active.turn_id}))
                    finally:
                        self.stop_requested.discard(session)
                        records.active_session.reset(binding)
        finally:
            admitted.discard(task)
            if not admitted:
                self.controller.admitted.pop(session, None)

    async def _emit(self, config, kind, **data):
        await invoke(config.emit, kind, data)

    async def _check(self, config):
        scope = current_cancellation()
        if scope and scope.termination_errors:
            raise NeedsReview("Interrupted work has not been confirmed stopped: " + "; ".join(scope.termination_errors))
        if config.session_id in self.stop_requested:
            raise GenerationStopped("Model generation stopped by user.")
        cancellation_checkpoint()
        await invoke(config.check)
        cancellation_checkpoint()

    async def _settle(self, config, turn_id, outcome, version=None, *, require_current=True):
        intent = await invoke(config.outcome_intent, outcome, turn_id)
        accepted = await records.finish_turn(self.store, config.session_id, turn_id, outcome,
                                             version=version, intent=intent, require_current=require_current)
        if accepted:
            # SQLite intent committed first; caller-owned file settlement is
            # serialized separately, still inside session admission protection.
            await invoke(config.reconcile)
        return accepted

    async def run_turn(self, config: ConversationConfig, trigger: dict | str | None = None) -> dict:
        async with self._ownership(config) as active:
            while True:
                with bind_cancellation(active.token):
                    outcome = await self._run_owned_turn(config, trigger, active)
                    if active.token.cancelled:
                        errors, _ = await records.owned_finalization(active.token.stop_and_wait())
                        if errors:
                            outcome = {"status": "blocked", "content": "Your input was saved. Interrupted work has not been confirmed stopped.",
                                       "error": "; ".join(errors)}
                if outcome["status"] != "superseded":
                    return outcome
                # Every old exchange and protected physical effect has settled.
                # New durable input now receives a fresh dispatch lifetime.
                if active.reason == "stop":
                    return {"status": "stopped", "content": ""}
                if active.task.cancelling() > active.expected_cancellations:
                    # A caller's cancellation is stronger than our internal
                    # correction signal, even if it arrived during cleanup.
                    raise asyncio.CancelledError
                active.token = CancellationToken()
                active.reason = None
                active.turn_id = uid()
                self.controller.activity_revisions[config.session_id] = self.controller.activity_revisions.get(config.session_id, 0) + 1
                await self._emit(config, "turn_started", **self.activity(config.session_id))
                trigger = None  # A completed old receipt cannot answer later input.

    async def _run_owned_turn(self, config, trigger, active):
        session = config.session_id
        turn_id = uid()
        try:
            cancellation_checkpoint()
            prepared = await invoke(config.prepare)
            if prepared is not None:
                config = prepared
            cancellation_checkpoint()
            await invoke(config.reconcile)
            if isinstance(trigger, dict):
                saved = await records.receipt_outcome(self.store, trigger)
                if saved is not None:
                    return saved
            await self._check(config)
            await invoke(config.validate)
            await invoke(config.on_admit)
            profile = await invoke(config.profile)
            await invoke(config.initialize)
            context_rejections = 0
            unfinished = []
            output_limit_reported = False
            context_failure_reported = False
            while True:
                await self._check(config)
                manager = await invoke(config.tools)
                terminal = await self.close_outstanding_calls(config, manager)
                if terminal is not None:
                    await self._settle(config, turn_id, terminal, terminal.get("answered_input_version"), require_current=False)
                    return terminal
                await self._check(config)
                version = await records.include_inputs(self.store, session)
                active.input_version = version
                history = [dict(r["body"]) for r in await self.store.messages(session)
                           if r["body"].get("role") != "error"]
                if config.request_view:
                    history = await invoke(config.request_view, history)
                if unfinished:
                    history.extend(unfinished)
                    history.append({"role": "user", "content":
                        "Continue the unfinished response without repeating completed text. "
                        "If a tool is needed, issue a new, complete tool call."})
                current = await records.state(self.store, session)
                control_revision = current.get("control_revision", 0)
                schemas = manager.schemas()
                try:
                    prepared, budget = await self.prepare_context(
                        config, profile, history, version, control_revision, retain=not unfinished,
                        tool_schemas=schemas, context_rejections=context_rejections)
                    await self._check(config)
                    await invoke(config.validate_request, prepared)
                    response = await self.generate_and_record(
                        config, profile, prepared, schemas, version, control_revision,
                        budget=budget)
                except InputChanged:
                    unfinished = []
                    context_rejections = 0
                    output_limit_reported = False
                    context_failure_reported = False
                    continue
                except ContextWindowChanged as exc:
                    context_rejections += 1
                    if not context_failure_reported:
                        await self._emit(config, "technical_failure", issue="context_request",
                                         message=str(exc), status="retrying")
                        context_failure_reported = True
                    await asyncio.sleep(1)
                    continue
                if context_failure_reported:
                    await self._emit(config, "technical_recovered", issue="context_request",
                                     message="Context request recovered; continuing the existing work.")
                    context_failure_reported = False
                context_rejections = 0
                if response["status"] == "stopped":
                    await self._settle(config, turn_id, response)
                    return response
                if response["status"] == "input_changed":
                    unfinished = []
                    output_limit_reported = False
                    continue
                if response["status"] == "truncated":
                    if unfinished:
                        unfinished.append({"role": "user", "content":
                            "Continue the unfinished response without repeating completed text. "
                            "If a tool is needed, issue a new, complete tool call."})
                    unfinished.append(response["message"])
                    if not output_limit_reported:
                        await self._emit(config, "technical_failure", issue="output_limit",
                                         message="Generation reached the output limit; continuing automatically.",
                                         status="continuing")
                        output_limit_reported = True
                    continue
                if output_limit_reported:
                    await self._emit(config, "technical_recovered", issue="output_limit",
                                     message="Output-limited generation continued successfully.")
                    unfinished = []
                    output_limit_reported = False
                await invoke(config.after_generation)
                if response["message"].get("tool_calls"):
                    continue
                if config.continuation:
                    await self.store.message(session, {"role": "user", "content": config.continuation})
                    continue
                if await self._settle(config, turn_id, response, version):
                    return response
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                active.token.cancel()
            stopped = isinstance(exc, GenerationStopped) or session in self.stop_requested
            superseded = (isinstance(exc, asyncio.CancelledError) and active.reason == "input" and not stopped
                          and active.task.cancelling() <= active.expected_cancellations)
            outcome = {"status": "stopped" if stopped else "superseded" if superseded else ("cancelled" if isinstance(exc, asyncio.CancelledError) else "error"),
                       "content": "", "error": str(exc) or type(exc).__name__}
            active_turn = active
            async def finalize():
                # Do not dispatch here: a cancellation/stop closes unstarted
                # calls honestly and projects any owned result already known.
                active = self.active_generations.get(session)
                if active and not active[0].done():
                    active[0].cancel()
                    await asyncio.gather(active[0], return_exceptions=True)
                try:
                    errors = await active_turn.token.stop_and_wait()
                    if errors:
                        outcome.update(status="blocked", error="Termination could not be confirmed: " + "; ".join(errors),
                                       content="Your input was saved. Replacement work is blocked until interrupted work is confirmed stopped.")
                    manager = await invoke(config.tools)
                    if manager is not None:
                        await self.close_outstanding_calls(config, manager, dispatch=False)
                finally:
                    await self.store.execute("UPDATE requests SET status='interrupted',error=?,finished=? WHERE session_id=? AND status IN ('pending','streaming')",
                        (outcome["error"], now(), session))
                    await self._settle(config, turn_id, outcome)
            await records.owned_finalization(finalize())
            if stopped or superseded or outcome["status"] == "blocked":
                return outcome
            raise

    async def close_outstanding_calls(self, config, manager, *, dispatch=True):
        history = await self.store.messages(config.session_id)
        started = not config.skip_before_user
        blocked = None
        for row in history:
            body = row["body"]
            if body.get("role") == "user" and (body.get("content") or "").strip() and not (body.get("content") or "").strip().startswith("<tool_response>"):
                started = True
            if not started:
                continue
            calls = body.get("tool_calls") or []
            mixed = len(calls) > 1 and any(c["function"]["name"] in config.mixed_control_names for c in calls)
            version, control_revision = await records.request_version(self.store, row["request_id"]) if calls else (0, 0)
            for call in calls:
                existing = await self.store.one("SELECT * FROM tools WHERE request_id=? AND call_id=?", (row["request_id"], call["id"]))
                supplied = [r for r in history if r["request_id"] == row["request_id"] and r["body"].get("role") == "tool"
                            and r["body"].get("tool_call_id") == call["id"]]
                if supplied:
                    if existing and existing["status"] == "unknown" and not decode(existing["result"], {}).get("outcome_known"):
                        known = await records.reconciled_execution_payload(self.store, existing["id"])
                        if known is None and dispatch and existing["name"] == "final_commit":
                            known = await records.reconcile_final_commit_execution(self.store, existing["id"])
                        if known is None:
                            blocked = {"status": "blocked", "content": "An earlier tool may have had partial effects. Its outcome must be reconciled before continuing.",
                                       "error": "An earlier tool has an uncertain outcome.", "execution_id": existing["id"]}
                            dispatch = False
                    if call["function"]["name"] == "finish_step" and existing:
                        payload = decode(existing["result"], {})
                        if payload.get("status") == "ok" and blocked is None:
                            await records.project_reconciliation_notices(self.store, config.session_id)
                            return {"status": "finished", "step_result": payload["data"], "content": "", "answered_input_version": version}
                    continue
                current = await records.state(self.store, config.session_id)
                obsolete = (current.get("input_version", 0) != version or current.get("control_revision", 0) != control_revision)
                if existing and existing["status"] == "waiting" and not dispatch and not obsolete:
                    continue
                if (mixed or obsolete or not dispatch) and not (existing and (existing["result"] or existing["status"] in {"running", "unknown"})):
                    identity = existing["id"] if existing else uid()
                    if not existing:
                        tool = manager.tools.get(call["function"]["name"])
                        await self.store.insert("tools", {"id": identity, "session_id": config.session_id,
                            "request_id": row["request_id"], "call_id": call["id"], "name": call["function"]["name"],
                            "args": call["function"].get("arguments", "{}"), "status": "pending",
                            "category": "mutation" if tool and tool.uncertain_on_interrupt else "read", "created": now()})
                    scope = current_cancellation()
                    user_cancelled = not mixed and (current.get("input_version", 0) != version or bool(scope and scope.reason in {"input", "stop"}))
                    payload = {"status": "error", "_execution_id": identity, "error": {
                        "code": "mixed_control_batch" if mixed else "not_executed",
                        "message": "ask_user and finish_step must be the only call in a response. No calls in this batch executed." if mixed
                        else "Cancelled by user. This unstarted tool was not executed. Follow the latest user input; do not retry or debug the cancellation." if user_cancelled
                    else "This unstarted tool was not executed because the response ended before dispatch."}, "cancelled_by_user": user_cancelled}
                    await records.complete_execution_exchange_once(self.store, identity, payload)
                    await self.store.execute("UPDATE waits SET status='cancelled' WHERE tool_id=? AND status='pending'", (identity,))
                else:
                    manager.input_version = version
                    async def guard(identity, v=version, r=control_revision):
                        await self._check(config)
                        return await records.claim_dispatch(self.store, config.session_id, identity, v, r,
                                                            kind="tool", validate_in=config.validate_in)
                    manager.dispatch_guard = guard
                    outcome = await manager.execute(call, row["request_id"])
                    if records.error_code(outcome.payload) == "interrupted" and not outcome.payload.get("outcome_known") and manager.tools.get(call["function"]["name"]) and manager.tools[call["function"]["name"]].uncertain_on_interrupt:
                        identity = outcome.payload.get("_execution_id")
                        known = await records.reconciled_execution_payload(self.store, identity)
                        if known is None and dispatch and call["function"]["name"] == "final_commit":
                            known = await records.reconcile_final_commit_execution(self.store, identity)
                        if known is None:
                            blocked = {"status": "blocked", "content": "An earlier tool may have had partial effects. Its outcome must be reconciled before continuing.",
                                       "error": "An earlier tool has an uncertain outcome.", "execution_id": identity}
                            dispatch = False
                    if outcome.step_result is not None and blocked is None:
                        await records.project_reconciliation_notices(self.store, config.session_id)
                        await self._emit(config, "changed")
                        return {"status": "finished", "step_result": outcome.step_result.model_dump(mode="json"), "content": "", "answered_input_version": version}
                await self._emit(config, "changed")
        await records.project_reconciliation_notices(self.store, config.session_id)
        return blocked

    async def prepare_context(self, config, profile, history, version, control_revision, *,
                              retain=False, tool_schemas=(), context_rejections=0):
        # Compact only what will be sent; excluded reasoning stays in storage.
        wire_history = []
        for message in history:
            item = dict(message)
            if not profile.round_trip_reasoning:
                item.pop("reasoning_content", None)
            # A reasoning-only partial has no outgoing assistant content.
            if (item.get("role") == "assistant" and not item.get("content")
                    and not item.get("tool_calls") and not item.get("reasoning_content")):
                continue
            wire_history.append(item)
        working_history = wire_history
        if retain:
            checkpoint = await records.retained_context(self.store, config.session_id)
            resumed = ContextManager.resume(wire_history, checkpoint)
            if resumed is not None:
                working_history = resumed
            elif checkpoint is not None:
                await records.retain_context(self.store, config.session_id, None)
        window = await self.client.context_window(profile)
        # Reserve space for the tool declarations, which are part of the real
        # prompt although they are sent as a separate API field.
        schema_tokens = self.context.estimate([{"tools": tool_schemas}]) if tool_schemas else 0
        # On explicit provider rejection, leave progressively more headroom
        # for protocol/template overhead and tokenizer-estimation mismatch.
        correction_reserve = min(window // 2, context_rejections * max(128, window // 8))
        if window - schema_tokens - correction_reserve <= 512:
            raise ModelError(
                "The provider context cannot fit required tool definitions and control instructions. "
                "Use a provider with a larger context window or reduce the tools exposed to this role."
            )
        current_attempt = 1
        target_bytes = 0
        summary_problem = False
        last_summary_request = None

        async def summarize(text):
            nonlocal last_summary_request
            await self._check(config)
            instructions = (config.summary_instruction
                            + f" Keep the summary under {target_bytes} UTF-8 bytes. "
                              "If an earlier summary was too long or empty, produce a shorter, "
                              "nonempty one. Preserve the important decisions and findings.")
            response = await self.generate_and_record(config, profile, [
                {"role": "system", "content": instructions},
                {"role": "user", "content": text}], [], version, control_revision,
                purpose="context_preparation",
                budget={"summary_attempt": current_attempt, "summary_input_bytes": len(text.encode("utf-8"))})
            last_summary_request = response.get("request_id")
            if response["status"] == "input_changed":
                raise InputChanged
            if response["status"] == "stopped":
                raise GenerationStopped("Context preparation stopped by user.")
            if response["status"] == "truncated":
                raise SummaryFailure("Context summary was truncated by the provider.",
                                     reason="truncated", input_bytes=len(text.encode("utf-8")),
                                     output_bytes=len(response.get("content", "").encode("utf-8")))
            if response["status"] != "completed":
                raise SummaryFailure("Context summary did not complete.",
                                     reason="incomplete", input_bytes=len(text.encode("utf-8")))
            content = response.get("content")
            if not isinstance(content, str) or not content.strip():
                raise SummaryFailure("Context summary returned no final text.",
                                     reason="empty", input_bytes=len(text.encode("utf-8")))
            return content

        async def on_summary_attempt(result):
            nonlocal current_attempt, last_summary_request, target_bytes, summary_problem
            status = result["status"]
            if status == "started":
                current_attempt = result["attempt"]
                target_bytes = result["target_bytes"]
                last_summary_request = None
            else:
                logging.getLogger(__name__).info(
                    "Context summary %s for session %s: attempt=%s, input_bytes=%s, "
                    "output_bytes=%s, reason=%s", status, config.session_id, result["attempt"],
                    result["input_bytes"], result["output_bytes"], result["reason"])
                if status == "failed":
                    if not summary_problem:
                        await self._emit(config, "technical_failure", issue="context_summary",
                                         message="Context summarization needs a shorter result; retrying.",
                                         status="retrying")
                        summary_problem = True
                    if last_summary_request is not None:
                        await self.store.update(
                            "requests", last_summary_request,
                            error=("Summary validation: %s (attempt %s; input %s bytes; output %s bytes)"
                                   % (result["reason"], result["attempt"], result["input_bytes"],
                                      result["output_bytes"])))
                elif status == "succeeded" and summary_problem:
                    await self._emit(config, "technical_recovered", issue="context_summary",
                                     message="Context summarization completed; work continues.")
                    summary_problem = False

        prepared, budget = await self.context.prepare(
            working_history, window, summarize, continuation=config.continuation,
            request_overhead_tokens=schema_tokens + correction_reserve,
            on_summary_attempt=on_summary_attempt)
        budget.update(
            tool_schema_estimate=schema_tokens,
            context_correction_reserve=correction_reserve,
            estimated_complete_request_tokens=budget.get("estimated_input_tokens", self.context.estimate(prepared)) + schema_tokens,
        )
        await self._check(config)
        if retain and budget.get("compacted"):
            await records.retain_context(self.store, config.session_id,
                                         ContextManager.checkpoint(wire_history, prepared))
        return prepared, budget

    async def generate_and_record(self, config, profile, messages, schemas, version, control_revision,
                                  *, purpose="conversation", budget=None):
        # Retry interrupted streams without replaying completed tool executions.
        # Each failed request is recorded; the user can interrupt at any time.
        failures = 0
        while True:
            await self._check(config)
            try:
                result = await self._generate_once(
                    config, profile, messages, schemas, version, control_revision,
                    purpose=purpose, budget=budget)
                if failures:
                    await self._emit(config, "technical_recovered", issue="model_stream",
                                     message="Model stream recovered; continuing the existing work.")
                return result
            except ModelUnavailable as exc:
                failures += 1
                if failures == 1:
                    await self._emit(config, "technical_failure", issue="model_stream",
                                     message=str(exc), status="retrying")
                logging.getLogger(__name__).warning(
                    "Technical model stream interruption for session %s (%s), retry %d: %s",
                    config.session_id, purpose, failures, exc,
                )
                await asyncio.sleep(1)

    async def _generate_once(self, config, profile, messages, schemas, version, control_revision,
                             *, purpose="conversation", budget=None):
        session = config.session_id
        await self._check(config)
        request_id = uid()
        text, reasoning = [], []
        active = self.active_turns.get(session)
        scope = current_cancellation()
        turn_id = active.turn_id if active else None
        accepting = True
        def delta(kind, value):
            if not accepting or session in self.stop_requested or (scope and scope.cancelled):
                return
            if kind == "text":
                text.append(value)
            elif kind == "reasoning":
                reasoning.append(value)
            if config.emit:
                config.emit("delta", {"request_id": request_id, "turn_id": turn_id, "kind": kind, "value": value, "purpose": purpose})
        task = None
        stopped = False
        try:
            await self.store.insert("requests", {"id": request_id, "session_id": session, "model": profile.model or "",
                "input": encode({"messages": messages, "tools": schemas, "budget": budget,
                    "profile": profile.model_dump(mode="json"), "input_version": version,
                    "control_revision": control_revision, "purpose": purpose}), "status": "pending", "created": now()})
            if not await records.claim_dispatch(self.store, session, request_id, version, control_revision,
                                                kind=purpose, validate_in=config.validate_in):
                await self.store.update("requests", request_id, status="interrupted", error="Input or control changed before dispatch", finished=now())
                raise InputChanged
            await self.store.update("requests", request_id, status="streaming")
            cancellation_checkpoint()
            task = asyncio.create_task(self.client.stream(profile, messages, schemas, delta))
            self.active_generations[session] = (task, config.tag)
            await self._emit(config, "started", request_id=request_id, turn_id=turn_id, task=task, purpose=purpose)
            turn = await task
            cancellation_checkpoint()
            accepting = False
            current = await records.state(self.store, session)
            changed = current.get("input_version", 0) != version or current.get("control_revision", 0) != control_revision
            stopped = session in self.stop_requested
            truncated = turn.finish_reason == "length"
            if purpose == "conversation":
                await self.store.message(session, turn.message, request_id,
                    partial=truncated or stopped or (changed and not turn.message.get("tool_calls")))
            else:
                await self.store.transaction(lambda c: records.operation_in(c, "conversation-generation:" + request_id,
                    "conversation_generation", session, {"purpose": purpose}, "completed", turn.message))
            await self.store.update("requests", request_id, status="completed", response_model=turn.model,
                usage=encode(turn.usage) if turn.usage is not None else None, finished=now())
            return {"status": "stopped" if stopped else "input_changed" if changed else "truncated" if truncated else "completed",
                    "content": turn.message.get("content") or "", "message": turn.message, "request_id": request_id}
        except BaseException as exc:
            accepting = False
            stopped = session in self.stop_requested
            body = {"role": "assistant", "content": "".join(text) or None}
            if reasoning:
                body["reasoning_content"] = "".join(reasoning)
            async def record_partial():
                # Setup callbacks can yield before we await the provider. Keep
                # ownership until that provider has actually stopped, too.
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if purpose == "conversation" and (body["content"] or body.get("reasoning_content")):
                    await self.store.message(session, body, request_id, partial=True)
                await self.store.update("requests", request_id, status="interrupted",
                    error="Cancelled by user" if scope and scope.reason in {"input", "stop"} else "Stopped by user" if stopped else str(exc) or type(exc).__name__, finished=now())
            await records.owned_finalization(record_partial())
            if stopped:
                return {"status": "stopped", "content": "".join(text), "message": body, "request_id": request_id}
            raise
        finally:
            accepting = False
            if task is not None and self.active_generations.get(session, (None,))[0] is task:
                self.active_generations.pop(session, None)
            if task is not None:
                await self._emit(config, "finished", request_id=request_id, turn_id=turn_id, stopped=stopped, purpose=purpose)
            await self._emit(config, "changed")

    async def one_shot(self, config, messages, schemas, *, purpose):
        """Tracked summary/draft generation, without tool dispatch or a second loop."""
        async with self._ownership(config) as active:
            with bind_cancellation(active.token):
                try:
                    return await self._one_shot_owned(config, messages, schemas, purpose=purpose)
                finally:
                    if active.token.cancelled:
                        await records.owned_finalization(active.token.stop_and_wait())

    async def _one_shot_owned(self, config, messages, schemas, *, purpose):
        try:
            await self._check(config)
            await invoke(config.validate)
            profile = await invoke(config.profile)
            if callable(messages):
                messages = await invoke(messages)
            current = await records.state(self.store, config.session_id)
            version, revision = current.get("input_version", 0), current.get("control_revision", 0)
            context_rejections = 0
            while True:
                try:
                    prepared, budget = await self.prepare_context(
                        config, profile, messages, version, revision,
                        tool_schemas=schemas, context_rejections=context_rejections)
                    return await self.generate_and_record(
                        config, profile, prepared, schemas, version, revision,
                        purpose=purpose, budget=budget)
                except ContextWindowChanged as exc:
                    context_rejections += 1
                    await asyncio.sleep(1)
        except BaseException as exc:
            if isinstance(exc, GenerationStopped) or config.session_id in self.stop_requested:
                return {"status": "stopped", "content": ""}
            active = self.active_turns.get(config.session_id)
            if isinstance(exc, asyncio.CancelledError) and active and active.reason == "input":
                return {"status": "input_changed", "content": ""}
            raise
