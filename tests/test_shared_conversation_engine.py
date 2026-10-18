"""Real durable state and controlled interleavings for the one conversation engine."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from agilecode.adapters.model import ContextWindowChanged, Turn
from agilecode.adapters.store import ProjectStore, decode, encode
from agilecode.core.tools import Tool, ToolManager
from agilecode.core.types import Conflict, Events, ModelProfile, Settings, now
from agilecode.runtime.conversation import ConversationConfig, ConversationEngine
from agilecode.runtime import conversation_records as records


@pytest.fixture
async def store(tmp_path):
    store = ProjectStore(tmp_path / "conversation.db")
    await store.open()
    yield store
    await store.close()


class Client:
    def __init__(self):
        self.calls = []
        self.callback = None
        self.entered = asyncio.Event()
        self.release = None
        self.fail_context = False

    async def resolve(self, profile):
        return profile.model_copy(update={"model": "fake"})

    async def close(self):
        pass

    async def context_window(self, profile):
        return 32768

    async def stream(self, profile, messages, tools, on_delta=None):
        self.calls.append(messages)
        self.callback = on_delta
        self.entered.set()
        if self.release:
            await self.release.wait()
        if self.fail_context:
            self.fail_context = False
            raise ContextWindowChanged("context changed")
        return Turn({"role": "assistant", "content": "Saved answer"}, "fake", {"total_tokens": 10}, "stop")


class Mutation(Tool):
    name = "mutate"
    uncertain_on_interrupt = True
    def __init__(self):
        self.calls = 0
    async def execute(self, args, execution_id):
        self.calls += 1
        return {"changed": True}


def config(store, client, *, session="test", tools=(), emit=None):
    manager = ToolManager(list(tools), [tool.name for tool in tools], store=store,
        events=Events(), settings=lambda: Settings(), session_id=session)
    async def initialize():
        if not await store.messages(session):
            await store.message(session, {"role": "system", "content": "Required controls"})
    return ConversationConfig(session, profile=lambda: ModelProfile(model="fake"), tools=lambda: manager,
                              initialize=initialize, emit=emit), manager


async def pending_call(store, session="test", *, name="mutate", version=0, revision=0):
    request_id = "request"
    call = {"id": "call", "function": {"name": name, "arguments": "{}"}}
    await store.insert("requests", {"id": request_id, "session_id": session, "model": "fake",
        "input": encode({"input_version": version, "control_revision": revision}), "status": "completed", "created": now()})
    await store.message(session, {"role": "assistant", "content": None, "tool_calls": [call]}, request_id)
    return call


@pytest.mark.asyncio
async def test_input_receipt_is_visible_and_idempotent_before_turn(store):
    receipt = await records.accept_input(store, "test", "Correct this", "input-1")
    assert await records.accept_input(store, "test", "Correct this", "input-1") == receipt
    assert not await store.messages("test")
    visible = await store.messages("test", include_partial=True)
    assert visible[0]["body"]["content"] == "Correct this"
    with pytest.raises(Conflict):
        await records.accept_input(store, "test", "Different", "input-1")
    assert (await records.state(store, "test"))["input_version"] == 1


@pytest.mark.asyncio
async def test_two_inputs_answered_together_do_not_generate_twice(store):
    client = Client()
    first, second = ConversationEngine(store, client), ConversationEngine(store, client)
    cfg, _ = config(store, client)
    one = await first.accept_input(cfg, "First question", "one")
    two = await second.accept_input(cfg, "Second question", "two")
    outcomes = await asyncio.gather(first.run_turn(cfg, one), second.run_turn(cfg, two))
    assert len(client.calls) == 1
    assert outcomes[0] == outcomes[1]
    assert [m["content"] for m in client.calls[0] if m["role"] == "user"] == ["First question", "Second question"]
    assert await first.run_turn(cfg, one) == outcomes[0]
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_correction_winning_dispatch_does_not_execute_pending_mutation(store):
    client, mutation = Client(), Mutation()
    engine = ConversationEngine(store, client)
    cfg, _ = config(store, client, tools=[mutation])
    await store.message("test", {"role": "system", "content": "Controls"})
    await store.message("test", {"role": "user", "content": "Old direction"})
    await pending_call(store)
    receipt = await engine.accept_input(cfg, "Do not mutate", "correction")
    result = await engine.run_turn(cfg, receipt)
    assert result["status"] == "completed" and mutation.calls == 0
    history = await store.messages("test")
    roles = [row["body"]["role"] for row in history]
    assert roles == ["system", "user", "assistant", "tool", "user", "assistant"]
    assert decode(history[3]["body"]["content"])["error"]["code"] == "not_executed"


@pytest.mark.asyncio
async def test_correction_after_approval_wait_is_rechecked(store):
    entered, release = asyncio.Event(), asyncio.Event()
    class ApprovedMutation(Mutation):
        requires_approval = True
    mutation, client = ApprovedMutation(), Client()
    cfg, manager = config(store, client, tools=[mutation])
    async def wait(*args):
        entered.set()
        await release.wait()
        return "approve"
    manager.wait = wait
    call = await pending_call(store)
    engine = ConversationEngine(store, client)
    task = asyncio.create_task(engine.close_outstanding_calls(cfg, manager))
    await entered.wait()
    await engine.accept_input(cfg, "Changed my mind", "new")
    release.set()
    await task
    assert mutation.calls == 0
    row = await store.one("SELECT * FROM tools")
    assert "not executed" in decode(row["result"])["error"]["message"]


@pytest.mark.asyncio
async def test_one_result_writer_rejects_conflicting_replay(store):
    call = await pending_call(store)
    tool = Mutation()
    _, manager = config(store, Client(), tools=[tool])
    result = await manager.execute(call, "request")
    assert (await manager.execute(call, "request")).payload == result.payload
    assert tool.calls == 1
    rows = [r for r in await store.messages("test") if r["body"]["role"] == "tool"]
    assert len(rows) == 1
    with pytest.raises(Conflict):
        await records.complete_execution_exchange_once(store, result.payload["_execution_id"], {"status": "other"})


@pytest.mark.asyncio
async def test_late_delta_does_not_change_finished_or_stopped_turn(store):
    events = []
    client = Client()
    client.release = asyncio.Event()
    cfg, _ = config(store, client, emit=lambda kind, data: events.append((kind, data)))
    engine = ConversationEngine(store, client)
    receipt = await engine.accept_input(cfg, "Question")
    task = asyncio.create_task(engine.run_turn(cfg, receipt))
    await client.entered.wait()
    callback = client.callback
    callback("text", "partial")
    engine.stop("test")
    result = await task
    assert result["status"] == "stopped" and result["content"] == "partial"
    count = len(events)
    callback("text", "late")
    assert len(events) == count
    assert (await store.one("SELECT status FROM requests"))["status"] == "interrupted"
    assert await records.receipt_outcome(store, receipt) is None


@pytest.mark.asyncio
async def test_input_change_during_generation_retries_current_input(store):
    client = Client()
    client.release = asyncio.Event()
    cfg, _ = config(store, client)
    engine = ConversationEngine(store, client)
    one = await engine.accept_input(cfg, "First", "one")
    task = asyncio.create_task(engine.run_turn(cfg, one))
    await client.entered.wait()
    two = await engine.accept_input(cfg, "Correction", "two")
    client.release.set()
    result = await task
    assert len(client.calls) == 2
    assert client.calls[-1][-1]["content"] == "Correction"
    assert await records.receipt_outcome(store, one) == result == await records.receipt_outcome(store, two)


@pytest.mark.asyncio
async def test_context_metadata_refresh_uses_shared_retry(store):
    client = Client()
    client.fail_context = True
    cfg, _ = config(store, client)
    engine = ConversationEngine(store, client)
    result = await engine.run_turn(cfg, await engine.accept_input(cfg, "Question"))
    assert result["status"] == "completed" and len(client.calls) == 2
    assert [r["status"] for r in await store.query("SELECT * FROM requests ORDER BY created")] == ["interrupted", "completed"]


@pytest.mark.asyncio
async def test_shared_session_lock_covers_different_engine_instances(store):
    client = Client()
    client.release = asyncio.Event()
    one, two = ConversationEngine(store, client), ConversationEngine(store, client)
    cfg, _ = config(store, client)
    receipt = await one.accept_input(cfg, "Question", "same")
    first = asyncio.create_task(one.run_turn(cfg, receipt))
    await client.entered.wait()
    second = asyncio.create_task(two.run_turn(cfg, receipt))
    await asyncio.sleep(0)
    assert len(client.calls) == 1
    client.release.set()
    assert await first == await second
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_identical_session_names_in_different_projects_are_independent(store, tmp_path):
    other = ProjectStore(tmp_path / "other.db")
    await other.open()
    try:
        async with records.session_lock(store, "project-manager"):
            async with asyncio.timeout(1):
                async with records.session_lock(other, "project-manager"):
                    pass
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_claimed_creation_recovers_identity_without_recreating(store):
    # Tool execution committed but its result projection was interrupted.
    await pending_call(store, name="create_ticket")
    await store.insert("tickets", {"id": "original", "key": "T-1", "title": "Original", "description": "",
        "state": "Backlog", "team_ref": "missing-now", "process_id": "missing-now", "created": now(), "updated": now()})
    await store.insert("tools", {"id": "execution", "session_id": "test", "request_id": "request", "call_id": "call",
        "name": "create_ticket", "args": "{}", "status": "running", "category": "mutation", "created": now()})
    await store.insert("operations", {"id": "execution", "kind": "create_ticket", "owner_id": "original", "data": "{}",
        "status": "completed", "result": encode({"ticket_id": "original"}), "created": now(), "updated": now()})
    _, manager = config(store, Client())
    call = {"id": "call", "function": {"name": "create_ticket", "arguments": "{}"}}
    result = await manager.execute(call, "request")
    assert result.payload["data"]["id"] == "original"
    assert len(await store.query("SELECT * FROM tickets")) == 1


@pytest.mark.asyncio
async def test_cancelled_unentered_tool_remains_unstarted(store):
    entered = asyncio.Event()
    async def check():
        entered.set()
        await asyncio.Event().wait()
    mutation = Mutation()
    _, manager = config(store, Client(), tools=[mutation])
    manager.check = check
    call = await pending_call(store)
    task = asyncio.create_task(manager.execute(call, "request"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mutation.calls == 0
    assert (await store.one("SELECT status FROM tools"))["status"] == "pending"


async def pm_fixture(store, tmp_path, client):
    from agilecode.core.project_manager import ProjectManager
    async def resolve(profile):
        return profile
    client.resolve = resolve
    app = SimpleNamespace(store=store, storage=tmp_path / "pm", global_root=tmp_path / "global",
                          root=tmp_path, client=client, events=Events(), all_paused=False,
                          config=SimpleNamespace(settings=Settings(), get_provider=lambda *args: SimpleNamespace(profile=ModelProfile(model="fake"))))
    pm = ProjectManager(app)
    pm._system_prompt = lambda: "Persistent PM controls"
    _, manager = config(store, client, session="project-manager")
    pm._tool_manager = lambda: manager
    pm.state = pm._default_state()
    await pm._change_state({"status": "running"}, control=True)
    await store.message("project-manager", {"role": "system", "content": "Persistent PM controls"})
    return pm


@pytest.mark.asyncio
async def test_pm_answer_intent_settles_before_next_turn_after_repeated_cancellation(store, tmp_path):
    import threading
    class QuestionClient(Client):
        async def stream(self, *args):
            answer = await super().stream(*args)
            answer.message["content"] = "NEEDS_USER: Choose direction"
            return answer
    client = QuestionClient()
    pm = await pm_fixture(store, tmp_path, client)
    pm.settings.uncertainty = "ask"
    entered, release = threading.Event(), threading.Event()
    original_write = pm._write_json
    def delayed_write(path, value):
        if value.get("current_activity") == "Waiting for user direction":
            entered.set()
            assert release.wait(3)
        original_write(path, value)
    pm._write_json = delayed_write
    cfg = pm._conversation_config(True)
    receipt = await pm.engine.accept_input(cfg, "First", "first")
    first = asyncio.create_task(pm.engine.run_turn(cfg, receipt))
    await asyncio.to_thread(entered.wait)
    pending = await store.query("SELECT * FROM operations WHERE kind='conversation_state' AND status='pending'")
    assert len(pending) == 1
    assert await records.receipt_outcome(store, receipt) is not None
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    second = asyncio.create_task(pm.engine.run_turn(pm._conversation_config(True), receipt))
    await asyncio.sleep(0.01)
    assert not second.done() and len(client.calls) == 1
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    result = await second
    assert result["content"].startswith("NEEDS_USER")
    assert pm.state["status"] == "paused"
    assert json.loads(pm.state_path.read_text())["status"] == "paused"
    assert not await store.query("SELECT * FROM operations WHERE kind='conversation_state' AND status='pending'")
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_pm_failed_file_settlement_blocks_admission_and_receipt_retry(store, tmp_path):
    client = Client()
    pm = await pm_fixture(store, tmp_path, client)
    original_write = pm._write_json
    def failed_write(path, value):
        if value.get("current_activity") == "Monitoring project":
            raise OSError("disk temporarily unavailable")
        original_write(path, value)
    pm._write_json = failed_write
    cfg = pm._conversation_config(True)
    receipt = await pm.engine.accept_input(cfg, "Question", "first")
    with pytest.raises(OSError):
        await pm.engine.run_turn(cfg, receipt)
    assert await store.query("SELECT * FROM operations WHERE kind='conversation_state' AND status='pending'")
    with pytest.raises(OSError):
        await pm.engine.run_turn(pm._conversation_config(True), receipt)
    assert len(client.calls) == 1
    pm._write_json = original_write
    answer = await pm.engine.run_turn(pm._conversation_config(True), receipt)
    assert answer["content"] == "Saved answer" and len(client.calls) == 1
    assert pm.state["current_activity"] == "Monitoring project"


@pytest.mark.asyncio
async def test_newer_pm_control_supersedes_pending_outcome(store, tmp_path):
    pm = await pm_fixture(store, tmp_path, Client())
    old_revision = pm.state["_control_revision"]
    await records.finish_turn(store, "project-manager", "old", {"status": "completed"},
        intent={"expected_revision": old_revision, "changes": {"status": "paused", "current_activity": "Stale pause"}})
    await pm._change_state({"status": "off", "current_activity": "Off"}, control=True)
    await pm._reconcile_state_intents()
    assert pm.state["status"] == "off" and pm.state["current_activity"] == "Off"
    assert (await store.get("operations", "conversation-state:old"))["status"] == "superseded"


@pytest.mark.asyncio
async def test_pm_state_file_reservation_outlives_cancelled_writer(store, tmp_path):
    import threading
    pm = await pm_fixture(store, tmp_path, Client())
    entered, release = threading.Event(), threading.Event()
    original_write = pm._write_json
    def delayed_write(path, value):
        if value.get("current_activity") == "Old write":
            entered.set()
            assert release.wait(3)
        original_write(path, value)
    pm._write_json = delayed_write
    first = asyncio.create_task(pm._change_state({"current_activity": "Old write"}, control=True))
    await asyncio.to_thread(entered.wait)
    first.cancel()
    first.cancel()
    second = asyncio.create_task(pm._change_state({"status": "off", "current_activity": "New write"}, control=True))
    await asyncio.sleep(0.01)
    assert not second.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert json.loads(pm.state_path.read_text())["current_activity"] == "New write"
    assert pm.state["status"] == "off"


@pytest.mark.asyncio
async def test_finish_acceptance_is_ordered_against_correction(store):
    from agilecode.core.tools import FinishStepTool
    started, release = asyncio.Event(), asyncio.Event()
    async def gate(result):
        started.set()
        await release.wait()
    finish = FinishStepTool(gate)
    cfg, manager = config(store, Client(), tools=[finish])
    call = await pending_call(store, name="finish_step")
    args = encode({"status": "PASS", "summary": "Old finish", "output": "Old work"})
    call["function"]["arguments"] = args
    await store.execute("UPDATE messages SET body=? WHERE request_id=?", (encode({"role": "assistant", "tool_calls": [call]}), "request"))
    manager.input_version = 0
    task = asyncio.create_task(manager.execute(call, "request"))
    await started.wait()
    await records.accept_input(store, "test", "Correction wins", "correction")
    release.set()
    result = await task
    assert result.step_result is None
    assert result.payload["error"]["code"] == "input_changed"


@pytest.mark.asyncio
async def test_known_finished_result_survives_later_correction(store):
    from agilecode.core.tools import FinishStepTool
    async def gate(result):
        pass
    cfg, manager = config(store, Client(), tools=[FinishStepTool(gate)])
    call = await pending_call(store, name="finish_step")
    call["function"]["arguments"] = encode({"status": "PASS", "summary": "Finished", "output": "Known work"})
    await store.execute("UPDATE messages SET body=? WHERE request_id=?", (encode({"role": "assistant", "tool_calls": [call]}), "request"))
    manager.input_version = 0
    accepted = await manager.execute(call, "request")
    assert accepted.step_result is not None
    receipt = await records.accept_input(store, "test", "Later correction", "later")
    outcome = await ConversationEngine(store, Client()).run_turn(cfg)
    assert outcome["status"] == "finished"
    assert await records.receipt_outcome(store, receipt) is None


@pytest.mark.asyncio
async def test_discussion_retry_recovers_answer_after_archive(repository, tmp_path):
    from agilecode.core.application import Application
    client = Client()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        discussion = await app.discussion.create()
        first = await app.discussion.ask(discussion["id"], "Question", input_id="stable")
        await app.discussion.archive(discussion["id"])
        retry = await app.discussion.ask(discussion["id"], "Question", input_id="stable")
        assert retry["content"] == first["content"] and retry["discussion"]["archived"]
        assert len(client.calls) == 1
        with pytest.raises(Conflict):
            await app.discussion.ask(discussion["id"], "Different", input_id="stable")
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_discussion_archive_wins_admission_retains_pending_input(repository, tmp_path):
    from agilecode.core.application import Application
    from agilecode.core.types import AgileCodeError
    client = Client()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        discussion = await app.discussion.create()
        cfg = app.discussion._config(discussion)
        receipt = await app.discussion.engine.accept_input(cfg, "Accepted question", "stable")
        await app.discussion.archive(discussion["id"])
        with pytest.raises(AgileCodeError, match="Restore"):
            await app.discussion.engine.run_turn(cfg, receipt)
        assert len(client.calls) == 0 and await records.receipt_outcome(app.store, receipt) is None
        visible = await app.discussion.history(discussion["id"])
        assert any(row["body"].get("content") == "Accepted question" for row in visible["messages"])
        await app.discussion.archive(discussion["id"], False)
        answer = await app.discussion.ask(discussion["id"], "Accepted question", "stable")
        assert answer["content"] == "Saved answer" and len(client.calls) == 1
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_discussion_uses_current_metadata_after_wait(repository, tmp_path):
    from agilecode.core.application import Application
    client = Client()
    client.release = asyncio.Event()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        discussion = await app.discussion.create()
        task = asyncio.create_task(app.discussion.ask(discussion["id"], "Question"))
        await client.entered.wait()
        await app.discussion.rename(discussion["id"], "Updated during response")
        client.release.set()
        assert (await task)["discussion"]["title"] == "Updated during response"
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_discussion_draft_is_tracked_without_tool_dispatch(repository, tmp_path):
    from agilecode.core.application import Application
    class DraftClient(Client):
        async def stream(self, profile, messages, tools, on_delta=None):
            self.calls.append(messages)
            return Turn({"role": "assistant", "content": None, "tool_calls": [{"id": "draft", "function": {
                "name": "draft_ticket", "arguments": encode({"title": "Investigate issue", "description": "## Scope\nInvestigate only."})}}]}, "fake", None, "tool_calls")
    client = DraftClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        discussion = await app.discussion.create()
        await app.store.message(discussion["session_id"], {"role": "user", "content": "Investigate the issue"})
        draft = await app.discussion.draft_ticket(discussion["id"])
        assert draft["title"] == "Investigate issue"
        assert not await app.store.query("SELECT * FROM tools")
        assert not await app.store.query("SELECT * FROM tickets")
        row = await app.store.one("SELECT * FROM requests")
        assert row["status"] == "completed" and decode(row["input"])["purpose"] == "ticket_draft"
        assert len(await app.store.messages(discussion["session_id"])) == 1
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_explicit_stop_precedes_simultaneous_input_change(store):
    client = Client()
    client.release = asyncio.Event()
    cfg, _ = config(store, client)
    engine = ConversationEngine(store, client)
    first = await engine.accept_input(cfg, "First", "first")
    task = asyncio.create_task(engine.run_turn(cfg, first))
    await client.entered.wait()
    engine.stop("test")
    second = await engine.accept_input(cfg, "Correction", "second")
    result = await task
    assert result["status"] == "stopped" and len(client.calls) == 1
    assert await records.receipt_outcome(store, second) is None


@pytest.mark.asyncio
async def test_summary_uses_recorded_cancellable_one_shot_without_answer_transcript(store):
    client = Client()
    cfg, _ = config(store, client)
    engine = ConversationEngine(store, client)
    async def compact(messages, window, summarize, **kwargs):
        assert kwargs["retry_attempts"] == 3
        assert await summarize("Previous data") == "Saved answer"
        return messages, {}
    engine.context.prepare = compact
    await engine.run_turn(cfg, await engine.accept_input(cfg, "Question"))
    requests = await store.query("SELECT * FROM requests ORDER BY created")
    assert [decode(row["input"])["purpose"] for row in requests] == ["context_preparation", "conversation"]
    history = await store.messages("test")
    assert [row["body"]["role"] for row in history] == ["system", "user", "assistant"]


@pytest.mark.asyncio
async def test_optional_binding_revoked_during_claim_never_enters(store):
    mutation = Mutation()
    _, manager = config(store, Client(), tools=[mutation])
    exposed = True
    manager.current_exposure = lambda name: exposed
    async def guard(identity):
        nonlocal exposed
        exposed = False
        return True
    manager.dispatch_guard = guard
    call = await pending_call(store)
    outcome = await manager.execute(call, "request")
    assert mutation.calls == 0 and outcome.payload["error"]["code"] == "access_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement,code", [("deny", "access_denied"), ("ask", "stale_state")])
async def test_policy_change_before_final_claim_revalidates_current_settings(store, replacement, code):
    from agilecode.application.policy import policy_lock
    client, mutation = Client(), Mutation()
    cfg, manager = config(store, client, tools=[mutation])
    current = Settings(tool_rules={"mutate": "allow"})
    manager.settings = lambda: current
    entered, release = asyncio.Event(), asyncio.Event()
    async def check():
        entered.set()
        await release.wait()
    manager.check = check
    await pending_call(store)
    task = asyncio.create_task(ConversationEngine(store, client).run_turn(cfg))
    await entered.wait()
    async with policy_lock(store):
        # Replace the object, as Configuration does; do not merely mutate the
        # object that the tool happened to authorize against earlier.
        current = Settings(tool_rules={"mutate": replacement})
    release.set()
    await task
    tool = await store.one("SELECT * FROM tools WHERE name='mutate'")
    assert mutation.calls == 0
    assert decode(tool["result"])["error"]["code"] == code
    assert not await store.one("SELECT id FROM operations WHERE id=?", ("conversation-dispatch:" + tool["id"],))


@pytest.mark.asyncio
async def test_policy_writer_cannot_overtake_check_to_claim_gap(store):
    from agilecode.application.policy import policy_lock
    mutation = Mutation()
    _, manager = config(store, Client(), tools=[mutation])
    current = Settings(tool_rules={"mutate": "allow"})
    manager.settings = lambda: current
    entered, release = asyncio.Event(), asyncio.Event()
    async def guard(identity):
        entered.set()
        await release.wait()
        return await records.claim_dispatch(store, "test", identity, 0, 0, kind="tool")
    manager.dispatch_guard = guard
    call = await pending_call(store)
    task = asyncio.create_task(manager.execute(call, "request"))
    await entered.wait()
    async def revoke():
        nonlocal current
        async with policy_lock(store):
            current = Settings(tool_rules={"mutate": "deny"})
    writer = asyncio.create_task(revoke())
    await asyncio.sleep(0)
    assert not writer.done()
    release.set()
    outcome, _ = await asyncio.gather(task, writer)
    assert mutation.calls == 1 and outcome.payload["status"] == "ok"
    assert current.tool_rules["mutate"] == "deny"


@pytest.mark.asyncio
async def test_restricted_pm_resume_delivers_input_accepted_while_paused(store, tmp_path):
    client = Client()
    pm = await pm_fixture(store, tmp_path, client)
    pm.app._require_execution_enabled = lambda: None
    async def snapshot():
        return {"state": dict(pm.state)}
    pm.snapshot = snapshot
    await pm.message("First question", "first")
    await pm.pause()
    await pm.message("Saved while paused", "pending")
    assert len(client.calls) == 1
    await pm.resume()
    assert pm.turn_task is not None
    await pm.turn_task
    assert len(client.calls) == 2
    assert any(message.get("content") == "Saved while paused" for message in client.calls[-1])
    pending = await store.one("SELECT * FROM operations WHERE kind='conversation_input' AND result IS NULL")
    assert pending is None
    await pm.resume()
    await asyncio.sleep(0)
    assert len(client.calls) == 2


def test_role_adapters_have_no_model_or_exchange_loops():
    import ast
    from pathlib import Path
    import agilecode
    root = Path(agilecode.__file__).parent
    for relative, owner, method in [("core/runner.py", "AgentRunner", "run"),
                                     ("core/project_manager.py", "ProjectManager", "_execute_turn"),
                                     ("core/discussion.py", "ProjectDiscussion", "ask")]:
        tree = ast.parse((root / relative).read_text())
        role = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == owner)
        entry = next(node for node in role.body if isinstance(node, ast.AsyncFunctionDef) and node.name == method)
        assert not any(isinstance(node, (ast.For, ast.While, ast.AsyncFor)) for node in ast.walk(entry))
        assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "stream"
                       for node in ast.walk(tree))
        assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "run_turn"
                   for node in ast.walk(entry))


@pytest.mark.asyncio
async def test_stop_from_another_engine_instance_uses_shared_controller(store):
    client = Client()
    client.release = asyncio.Event()
    cfg, _ = config(store, client)
    first, second = ConversationEngine(store, client), ConversationEngine(store, client)
    receipt = await first.accept_input(cfg, "Question")
    task = asyncio.create_task(first.run_turn(cfg, receipt))
    await client.entered.wait()
    assert second.stop("test")
    assert (await task)["status"] == "stopped"


@pytest.mark.asyncio
async def test_large_result_keeps_one_stable_preview_and_full_outcome(store, tmp_path):
    from agilecode.application.services.artifacts import ArtifactService
    class Large(Mutation):
        async def execute(self, args, identity):
            self.calls += 1
            return {"text": "data" * 1000}
    tool = Large()
    await store.insert("tickets", {"id": "ticket", "key": "T-1", "title": "Title", "description": "",
        "state": "Backlog", "team_ref": "team", "process_id": "process", "created": now(), "updated": now()})
    cfg, manager = config(store, Client(), tools=[tool])
    manager.ticket_id = "ticket"
    manager.settings = lambda: Settings(tool_result_inline_bytes=100, tool_result_preview_chars=30)
    manager.shell = SimpleNamespace(artifact_root=tmp_path / "artifacts", artifacts=ArtifactService(store, tmp_path / "artifacts"))
    call = await pending_call(store)
    first = await manager.execute(call, "request")
    preview = next(row["body"]["content"] for row in await store.messages("test") if row["body"]["role"] == "tool")
    second = await manager.execute(call, "request")
    assert first.payload == second.payload and len(first.payload["data"]["text"]) == 4000
    assert decode(preview)["partial"] is True
    assert len(await store.query("SELECT * FROM artifacts")) == 1 and tool.calls == 1
    assert preview == next(row["body"]["content"] for row in await store.messages("test") if row["body"]["role"] == "tool")
    # Legacy preview has no projection-journal row; recover its verified exact bytes.
    await store.execute("DELETE FROM operations WHERE kind='conversation_result'")
    await records.complete_execution_exchange_once(store, first.payload["_execution_id"], first.payload)
    assert len([row for row in await store.messages("test") if row["body"]["role"] == "tool"]) == 1


@pytest.mark.asyncio
async def test_one_shot_cancellation_during_dispatch_setup_closes_request(store, monkeypatch):
    client = Client()
    cfg, _ = config(store, client)
    engine = ConversationEngine(store, client)
    entered = asyncio.Event()
    original_claim = records.claim_dispatch
    async def delayed_claim(*args, **kwargs):
        result = await original_claim(*args, **kwargs)
        entered.set()
        await asyncio.Event().wait()
        return result
    monkeypatch.setattr(records, "claim_dispatch", delayed_claim)
    task = asyncio.create_task(engine.one_shot(cfg, [{"role": "user", "content": "Draft"}], [], purpose="ticket_draft"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not client.calls
    request = await store.one("SELECT * FROM requests")
    assert request["status"] == "interrupted" and request["finished"]
    assert not await store.messages("test")


@pytest.mark.asyncio
async def test_one_shot_cancellation_waits_for_provider_started_inside_callback(store):
    exited, release_cleanup = asyncio.Event(), asyncio.Event()
    class SlowCleanup(Client):
        async def stream(self, *args):
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release_cleanup.wait()
                exited.set()
                raise
    client = SlowCleanup()
    callback_entered = asyncio.Event()
    async def emit(kind, data):
        if kind == "started":
            callback_entered.set()
            await asyncio.Event().wait()
    cfg, _ = config(store, client, emit=emit)
    engine = ConversationEngine(store, client)
    task = asyncio.create_task(engine.one_shot(cfg, [{"role": "user", "content": "Draft"}], [], purpose="ticket_draft"))
    await callback_entered.wait()
    await client.entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and engine.lock("test").locked()
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exited.is_set() and not engine.active_generations
    assert (await store.one("SELECT * FROM requests"))["status"] == "interrupted"


@pytest.mark.asyncio
async def test_pm_own_tool_child_event_does_not_start_an_autonomous_turn(store, tmp_path):
    from agilecode.core.types import Event
    class ToolClient(Client):
        async def stream(self, *args):
            turn = await super().stream(*args)
            if len(self.calls) == 1:
                turn.message = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "own-call", "function": {"name": "mutate", "arguments": "{}"}}]}
            return turn
    client = ToolClient()
    pm = await pm_fixture(store, tmp_path, client)
    pm.settings.mode = "fully_automated"
    queued = []
    pm._queue_update = queued.append
    class OwnMutation(Mutation):
        async def execute(self, args, identity):
            pm.app.events.emit(Event("TicketChanged", ticket_id="ticket"))
            return await super().execute(args, identity)
    mutation = OwnMutation()
    _, manager = config(store, client, session="project-manager", tools=[mutation])
    pm._tool_manager = lambda: manager
    pm.app.events.subscribe(pm._on_project_event)
    cfg = pm._conversation_config(True)
    result = await pm.engine.run_turn(cfg, await pm.engine.accept_input(cfg, "Manage this"))
    assert result["status"] == "completed" and len(client.calls) == 2
    assert mutation.calls == 1 and queued == []

@pytest.mark.asyncio
async def test_model_worker_failure_retries_generation_without_replaying_completed_tool(store):
    from agilecode.adapters.model import ModelUnavailable

    class Flaky(Client):
        def __init__(self):
            super().__init__()
            self.fail = True

        async def stream(self, profile, messages, schemas, on_delta=None):
            if self.fail:
                self.fail = False
                self.calls.append(messages)
                if on_delta:
                    on_delta("reasoning", "Partial reasoning before model crash")
                raise ModelUnavailable("The model has crashed without additional information.")
            return await super().stream(profile, messages, schemas, on_delta)

    client = Flaky()
    client.operational_settings = lambda: Settings(
        model_transport_retry_attempts=2,
        model_transport_retry_delay_cap_seconds=0,
    )
    mutation = Mutation()
    config_, manager = config(store, client, tools=[mutation])
    await pending_call(store)
    outcome = await ConversationEngine(store, client).run_turn(config_)
    assert outcome["status"] == "completed"
    assert mutation.calls == 1
    assert len(client.calls) == 2
    request_rows = await store.query("SELECT * FROM requests ORDER BY created,rowid")
    failed = [r for r in request_rows if r["status"] == "interrupted"]
    assert len(failed) == 1 and "model has crashed" in failed[0]["error"]
    assert len([r for r in request_rows if r["status"] == "completed" and r["id"] != "request"]) == 1
    tool_rows = await store.query("SELECT * FROM tools")
    assert len(tool_rows) == 1 and tool_rows[0]["status"] == "completed"
    # Interrupted stream fragments are retained only as partial history.
    assert all(m["body"]["role"] != "tool" or m["body"]["tool_call_id"] == "call"
               for m in await store.messages("test"))


@pytest.mark.asyncio
async def test_recovery_exhaustion_is_bounded_and_model_generation_resets_counter(store):
    from agilecode.adapters.model import ModelError, ModelUnavailable

    class Sequence(Client):
        def __init__(self):
            super().__init__()
            self.behaviors = ["fail", "ok", "fail", "ok"]

        async def stream(self, profile, messages, tools, on_delta=None):
            behavior = self.behaviors.pop(0)
            if behavior == "fail":
                self.calls.append(messages)
                raise ModelUnavailable("Temporary worker crash")
            return await super().stream(profile, messages, tools, on_delta)

    client = Sequence()
    client.operational_settings = lambda: Settings(
        model_transport_retry_attempts=2, model_transport_retry_delay_cap_seconds=0)
    cfg, _ = config(store, client)
    engine = ConversationEngine(store, client)
    assert (await engine.run_turn(cfg, await engine.accept_input(cfg, "First")))["status"] == "completed"
    assert (await engine.run_turn(cfg, await engine.accept_input(cfg, "Second")))["status"] == "completed"
    assert len(client.calls) == 4

    class AlwaysFail(Client):
        async def stream(self, profile, messages, tools, on_delta=None):
            self.calls.append(messages)
            raise ModelUnavailable("Repeated inference-worker failure")

    failed_client = AlwaysFail()
    failed_client.operational_settings = client.operational_settings
    failed_config, _ = config(store, failed_client, session="another")
    with pytest.raises(ModelError, match=r"failed after 2 attempt"):
        await ConversationEngine(store, failed_client).run_turn(
            failed_config, await ConversationEngine(store, failed_client).accept_input(
                failed_config, "One more request"))
    assert len(failed_client.calls) == 2


@pytest.mark.asyncio
async def test_context_budget_includes_exposed_tool_definitions(store):
    client = Client()
    cfg, manager = config(store, client)
    manager.schemas = lambda: [{"type": "function", "function": {
        "name": "tool_fixture", "description": "Large declaration " * 100,
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
    result = await ConversationEngine(store, client).run_turn(
        cfg, await ConversationEngine(store, client).accept_input(cfg, "Check request sizing"))
    assert result["status"] == "completed"
    request = (await store.query("SELECT * FROM requests WHERE status='completed' ORDER BY created,rowid"))[-1]
    payload = decode(request["input"])
    assert payload["budget"]["tool_schema_estimate"] > 0
    assert payload["budget"]["estimated_complete_request_tokens"] == (
        payload["budget"]["estimated_input_tokens"] + payload["budget"]["tool_schema_estimate"]
    )



