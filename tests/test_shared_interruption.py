"""One interruption lifetime for setup, provider, tool and durable settlement."""
import asyncio

import pytest

from agilecode.adapters.model import Turn
from agilecode.adapters.store import ProjectStore, decode, encode
from agilecode.application.cancellation import OperationInterrupted, current_cancellation
from agilecode.core.types import Events, ModelProfile, Settings, now
from agilecode.runtime.conversation import ConversationConfig, ConversationEngine
from agilecode.runtime import conversation_records as records
from agilecode.runtime.tools import Tool, ToolManager


@pytest.fixture
async def store(tmp_path):
    value = ProjectStore(tmp_path / "interruption.db")
    await value.open()
    yield value
    await value.close()


class Client:
    def __init__(self, calls=()):
        self.tool_calls = calls
        self.requests = []

    async def context_window(self, profile):
        return 32768

    async def stream(self, profile, messages, tools, delta):
        self.requests.append(messages)
        body = {"role": "assistant", "content": "Current direction acknowledged"}
        if len(self.requests) == 1 and self.tool_calls:
            body.update(content=None, tool_calls=[
                {"id": name, "function": {"name": name, "arguments": "{}"}}
                for name in self.tool_calls])
        return Turn(body, "test", None, "tool_calls" if body.get("tool_calls") else "stop")


class SettlingTool(Tool):
    name = "settling_effect"
    uncertain_on_interrupt = True

    def __init__(self, *, known=True, successful=False):
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.known, self.successful = known, successful
        self.cancellations = 0
        self.token = None

    async def execute(self, args, identity):
        self.token = current_cancellation()
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancellations += 1
            self.cancelled.set()
            await self.release.wait()
            if self.successful:
                return {"physical_effect": "completed before interruption"}
            if self.known:
                raise OperationInterrupted("Provisioning settled; execution was not launched.",
                                           result={"provisioned": True, "launched": False})
            raise


class Unstarted(Tool):
    name = "later_effect"
    uncertain_on_interrupt = True
    def __init__(self):
        self.calls = 0
    async def execute(self, args, identity):
        self.calls += 1
        return {"changed": True}


def make_config(store, tools=(), emit=None, session="session", tag=None):
    manager = ToolManager(list(tools), [t.name for t in tools], store=store,
        events=Events(), settings=lambda: Settings(), session_id=session)
    return ConversationConfig(session, profile=lambda: ModelProfile(model="test"),
        tools=lambda: manager, emit=emit, tag=tag)


@pytest.mark.asyncio
@pytest.mark.parametrize("successful", [False, True])
async def test_input_acceptance_publishes_before_cleanup_and_continues_known_effect(store, successful):
    effect, later = SettlingTool(successful=successful), Unstarted()
    client = Client([effect.name, later.name])
    observed = []
    config = make_config(store, [effect, later], lambda kind, data: observed.append((kind, data)))
    engine = ConversationEngine(store, client)
    first = await engine.accept_input(config, "Old direction", "first")
    task = asyncio.create_task(engine.run_turn(config, first))
    try:
        await asyncio.wait_for(effect.entered.wait(), 2)
        correction = await engine.accept_input(config, "Do not launch execution", "correction")
        await asyncio.wait_for(effect.cancelled.wait(), 2)
        assert effect.token.cancelled and not task.done()
        assert any(data.get("input", {}).get("id") == correction["id"] for _, data in observed)
        visible = await store.messages("session", include_partial=True)
        row = next(row for row in visible if row["id"] == correction["message_id"])
        assert row["request_id"] == "correction" and row["partial"]
        assert len(client.requests) == 1 and not later.calls
        effect.release.set()
        outcome = await asyncio.wait_for(task, 2)
        assert outcome["status"] == "completed"
        assert len(client.requests) == 2 and not later.calls
        assert await records.receipt_outcome(store, correction) == outcome
        result = decode((await store.one("SELECT result FROM tools WHERE name=?", (effect.name,)))["result"])
        if successful:
            assert result["status"] == "ok" and result["data"]["physical_effect"]
        else:
            assert result["outcome_known"] and result["data"]["launched"] is False
        second = decode((await store.one("SELECT result FROM tools WHERE name=?", (later.name,)))["result"])
        assert second["error"]["code"] == "not_executed"
        messages = client.requests[-1]
        assert [m["role"] for m in messages] == ["user", "assistant", "tool", "tool", "user"]
        assert messages[-1]["content"] == "Do not launch execution"
    finally:
        effect.release.set()
        await engine.cancel("session")
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_unknown_mutating_effect_stays_blocked_and_keeps_correction(store):
    effect = SettlingTool(known=False)
    client = Client([effect.name])
    config = make_config(store, [effect])
    engine = ConversationEngine(store, client)
    first = await engine.accept_input(config, "Old direction")
    task = asyncio.create_task(engine.run_turn(config, first))
    await effect.entered.wait()
    correction = await engine.accept_input(config, "Correct the direction")
    await effect.cancelled.wait()
    effect.release.set()
    result = await asyncio.wait_for(task, 2)
    assert result["status"] == "blocked" and len(client.requests) == 1
    assert await records.receipt_outcome(store, correction) is None
    assert (await store.one("SELECT status FROM tools"))["status"] == "unknown"
    assert any(row["id"] == correction["message_id"] for row in await store.messages("session", include_partial=True))


@pytest.mark.asyncio
async def test_stop_wins_correction_cleanup_and_does_not_poison_next_turn(store):
    effect, later = SettlingTool(), Unstarted()
    client = Client([effect.name, later.name])
    config = make_config(store, [effect, later], tag="run")
    one, two = ConversationEngine(store, client), ConversationEngine(store, client)
    first = await one.accept_input(config, "Original")
    task = asyncio.create_task(one.run_turn(config, first))
    await effect.entered.wait()
    receipts = [await two.accept_input(config, f"Correction {index}", str(index)) for index in range(3)]
    await effect.cancelled.wait()
    assert two.interrupt_tag("run", reason="stop") == "session"
    assert two.stop("session")
    effect.release.set()
    assert (await asyncio.wait_for(task, 2))["status"] == "stopped"
    assert effect.cancellations == 1 and len(client.requests) == 1 and not later.calls
    assert not two.stop("session")
    assert not one.active_turns and not one.stop_requested
    outcome = await two.run_turn(config, receipts[-1])
    assert outcome["status"] == "completed"
    assert all([await records.receipt_outcome(store, item) == outcome for item in receipts])


@pytest.mark.asyncio
@pytest.mark.parametrize("one_shot", [False, True])
async def test_shared_stop_covers_provider_setup_and_leaves_other_session_alone(store, one_shot):
    client = Client()
    first, second = ConversationEngine(store, client), ConversationEngine(store, client)
    config = make_config(store)
    entered = asyncio.Event()
    async def profile():
        entered.set()
        await asyncio.Event().wait()
    config.profile = profile
    if one_shot:
        task = asyncio.create_task(first.one_shot(config, [{"role": "user", "content": "Draft"}], [], purpose="draft"))
    else:
        task = asyncio.create_task(first.run_turn(config))
    await entered.wait()
    assert not client.requests and second.stop("session")
    assert (await asyncio.wait_for(task, 2))["status"] == "stopped"
    other = make_config(store, session="independent")
    assert (await second.run_turn(other))["status"] == "completed"
    assert not first.active_turns and not first.controller.admitted


@pytest.mark.asyncio
async def test_stop_cancels_queued_receipt_waiter_without_discarding_input(store):
    effect = SettlingTool()
    client = Client([effect.name])
    config = make_config(store, [effect])
    engine = ConversationEngine(store, client)
    first = await engine.accept_input(config, "Original")
    running = asyncio.create_task(engine.run_turn(config, first))
    await effect.entered.wait()
    correction = await engine.accept_input(config, "Correction")
    queued = asyncio.create_task(engine.run_turn(config, correction))
    await asyncio.sleep(0)
    stop = asyncio.create_task(engine.cancel("session"))
    await effect.cancelled.wait()
    effect.release.set()
    assert await asyncio.wait_for(stop, 2)
    result = await asyncio.gather(running, queued, return_exceptions=True)
    assert result[0]["status"] == "stopped" and isinstance(result[1], asyncio.CancelledError)
    assert await records.receipt_outcome(store, correction) is None
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_shared_activity_covers_tools_and_cleanup_with_ordered_revisions(store):
    effect = SettlingTool()
    client = Client([effect.name])
    events = []
    config = make_config(store, [effect], lambda kind, data: events.append((kind, data)))
    engine = ConversationEngine(store, client)
    task = asyncio.create_task(engine.run_turn(config, await engine.accept_input(config, "Original")))
    await effect.entered.wait()
    activity = engine.activity("session")
    assert activity["turn_active"] and activity["activity_revision"] == 1
    assert engine.activity_snapshot()["active_conversations"] == {"session": activity["turn_id"]}
    engine.stop("session")
    await effect.cancelled.wait()
    assert engine.activity("session") == activity
    effect.release.set()
    await task
    assert engine.activity("session") == {"turn_active": False, "turn_id": None, "activity_revision": 2}
    lifecycle = [(kind, data) for kind, data in events if kind.startswith("turn_")]
    assert [kind for kind, _ in lifecycle] == ["turn_started", "turn_finished"]
    assert lifecycle[0][1] == activity
    assert lifecycle[1][1] == {**activity, "turn_active": False, "activity_revision": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("name,kind", [("start_ticket", "start_ticket"), ("resume_ticket", "resume_run"),
                                      ("move_ticket", "move_ticket"), ("delete_ticket", "delete_ticket"),
                                      ("delete_attempt", "attempt_rewind")])
@pytest.mark.parametrize("status", ["completed", "interrupted"])
async def test_application_receipt_recovers_known_outcome_without_replaying(store, name, kind, status):
    identity = "execution"
    original = {"ticket_id": "ticket", "state": "In Progress"}
    result = {"execution": "interrupted" if status == "interrupted" else "started", "reason": "Settled before next phase."}
    await store.insert("operations", {"id": identity, "kind": kind, "owner_id": "ticket",
        "data": encode({"original": original}), "status": status,
        "result": encode(result), "created": now(), "updated": now()})
    tool = {"id": identity, "name": name, "args": encode({"ticket": "ticket", "state": "In Progress"}),
            "result": None, "category": "mutation"}
    payload = await records.recover_execution_payload(store, tool)
    assert payload["outcome_known"]
    if status == "interrupted":
        assert payload["error"]["code"] == "interrupted" and payload["data"] == result
    else:
        assert payload["status"] == "ok" and payload["data"]["result"] == result
    tool["args"] = encode({"ticket": "ticket", "state": "Backlog"})
    unknown = await records.recover_execution_payload(store, tool)
    assert unknown["error"]["code"] == "interrupted" and not unknown.get("outcome_known")


@pytest.mark.asyncio
async def test_caller_cancellation_during_correction_cleanup_never_restarts(store):
    effect = SettlingTool()
    client = Client([effect.name])
    config = make_config(store, [effect])
    engine = ConversationEngine(store, client)
    task = asyncio.create_task(engine.run_turn(config, await engine.accept_input(config, "Original")))
    await effect.entered.wait()
    receipt = await engine.accept_input(config, "Correction")
    await effect.cancelled.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert not task.done()
    effect.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert len(client.requests) == 1
    assert await records.receipt_outcome(store, receipt) is None
    assert not engine.activity("session")["turn_active"]


@pytest.mark.asyncio
async def test_legacy_discussion_stop_owns_pre_turn_request_alias(store, tmp_path):
    from types import SimpleNamespace
    from agilecode.core.discussion import ProjectDiscussion
    app = SimpleNamespace(store=store)
    discussion = ProjectDiscussion(app)
    identity = "legacy"
    await store.insert("discussions", {"id": identity, "title": "Legacy discussion",
        "session_id": "project-discussion", "archived": 0, "created": now(), "updated": now()})
    entered = asyncio.Event()
    async def accepted_then_wait(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    app.client = Client()
    discussion.engine.accept_input = accepted_then_wait
    task = asyncio.create_task(discussion.ask(identity, "Direction"))
    await entered.wait()
    assert discussion.engine.is_busy()
    result = await discussion.stop(identity)
    assert result["stopped"]
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not discussion.engine.is_busy()
    assert (await discussion.get(identity))["session_id"] == "project-discussion"


async def seed_feedback_ticket(store):
    await store.insert("tickets", {"id": "ticket", "key": "T-1", "title": "Feedback", "description": "",
        "state": "In Progress", "team_ref": "team", "process_id": "process", "created": now(), "updated": now()})
    await store.insert("runs", {"id": "run", "ticket_id": "ticket", "snapshot": "{}", "state": "running",
        "workspace": "/fixture", "branch": "ticket", "base_branch": "main", "base_oid": "base",
        "created": now(), "updated": now()})
    for index in range(2):
        await store.insert("attempts", {"id": f"attempt-{index}", "run_id": "run", "step_id": "work",
            "number": index + 1, "session_id": f"session-{index}", "member": "{}", "model": "{}",
            "status": "running", "context": "{}", "created": now()})


@pytest.mark.asyncio
async def test_feedback_identity_is_once_across_concurrent_attempt_routing(store):
    from agilecode.core.types import Conflict
    await seed_feedback_ticket(store)
    first, second = await asyncio.gather(*[
        records.accept_ticket_feedback(store, "ticket", "run", f"session-{index}", "Exact correction", "stable",
                                       with_created=True)
        for index in range(2)])
    assert first[0]["id"] == second[0]["id"]
    assert sorted([first[1], second[1]]) == [False, True]
    assert len(await store.query("SELECT * FROM operations WHERE kind='conversation_input'")) == 1
    assert len(await store.query("SELECT * FROM activity")) == 1
    with pytest.raises(Conflict):
        await records.accept_ticket_feedback(store, "ticket", "run", "session-1", "Different correction", "stable")


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
async def test_feedback_retry_recovers_receipt_before_mutable_run_checks(store, terminal):
    from agilecode.application.execution import ExecutionOperations
    from types import SimpleNamespace
    await seed_feedback_ticket(store)
    receipt = await records.accept_ticket_feedback(store, "ticket", "run", "session-0", "Exact correction", "stable")
    if terminal:
        await store.update("runs", "run", state="completed")
        await store.execute("UPDATE tickets SET state='Done',archived=1 WHERE id='ticket'")
        await store.transaction(lambda c: records.settle_terminal_feedback_in(c, "run", "ticket"))
    async def find(ticket):
        return await store.get("tickets", ticket)
    async def changed_run(*args):
        raise AssertionError("An accepted input must recover before reading a new attempt/run")
    app = SimpleNamespace(store=store, find_ticket=find, active_run=changed_run)
    result = await ExecutionOperations.submit_feedback.__wrapped__(app, "ticket", "Exact correction",
                                                                 interrupt=True, input_id="stable")
    assert result["receipt_id"] == receipt["id"] and result["session_id"] == "session-0"
    assert result["interrupted"] is False
    assert result["needs_followup"] is terminal
    assert len(await store.query("SELECT * FROM operations WHERE kind='conversation_input'")) == 1


@pytest.mark.asyncio
async def test_cancelled_mcp_discovery_closes_prior_handles_before_retry(tmp_path, monkeypatch):
    from agilecode.adapters import mcp
    from agilecode.core.types import McpServerConfig
    opened = []
    entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Connection:
        def __init__(self, config, workspace):
            self.config = config
            self.closed = False
            opened.append(self)
        async def start(self):
            if self.config.id == "second" and len(opened) == 2:
                entered.set()
                await asyncio.Event().wait()
        async def list_items(self, method, key):
            return [{"name": "tool", "inputSchema": {"type": "object"}}] if key == "tools" else []
        async def close(self):
            if len(opened) == 2 and self.config.id == "first":
                closing.set()
                await release.wait()
            self.closed = True
    monkeypatch.setattr(mcp, "McpConnection", Connection)
    service = mcp.McpService([McpServerConfig(id=name, command="fixture", enabled=True)
                             for name in ("first", "second")], tmp_path)
    task = asyncio.create_task(service.discover())
    await entered.wait()
    task.cancel()
    await closing.wait()
    task.cancel()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(connection.closed for connection in opened)
    assert not service.loaded and not service.connections and not service.tools
    await service.discover()
    assert service.loaded and not service.errors
    assert set(service.connections) == {"first", "second"} and len(service.tools) == 2
    await service.close()
    assert all(connection.closed for connection in opened)
