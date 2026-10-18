"""Durable compaction with deterministic protocol fixtures, not live model QA."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest

from agilecode.adapters.model import ContextWindowChanged, Turn
from agilecode.adapters.store import ProjectStore, encode
from agilecode.core.context import ContextError, ContextManager
from agilecode.core.tools import Tool, ToolManager
from agilecode.core.types import Events, ModelProfile, Settings
from agilecode.runtime.conversation import ConversationConfig, ConversationEngine
from agilecode.runtime import conversation_records as records


def initial_history():
    return [{"role": "system", "content": "Current controls"},
            {"role": "user", "content": "OLD_SOURCE_DETAIL " * 700},
            {"role": "user", "content": "Keep the current change narrowly scoped."}]


def exchange(content="Observed result"):
    return [{"role": "assistant", "content": None, "tool_calls": [{"id": "call", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":"example.py"}'}}]},
            {"role": "tool", "tool_call_id": "call", "content": content}]


class Client:
    def __init__(self):
        self.summaries = []
        self.requests = []
        self.window = 2048
        self.fail_main = False
        self.refresh_window = False
        self.block_summary = False
        self.summary_entered = asyncio.Event()
        self.summary_instruction = ConversationConfig.__dataclass_fields__["summary_instruction"].default

    async def context_window(self, profile):
        return self.window

    async def stream(self, profile, messages, schemas, on_delta=None):
        if messages[0].get("content") == self.summary_instruction:
            self.summaries.append(deepcopy(messages))
            if self.block_summary:
                self.summary_entered.set()
                await asyncio.Event().wait()
            return Turn({"role": "assistant", "content": "Retained earlier decisions and observations."},
                        "fixture", None, "stop")
        self.requests.append(deepcopy(messages))
        if self.refresh_window:
            self.refresh_window = False
            self.window = 1800
            raise ContextWindowChanged("Provider context was refreshed")
        if self.fail_main:
            self.fail_main = False
            raise RuntimeError("Temporary provider failure")
        return Turn({"role": "assistant", "content": "Recorded response"}, "fixture", None, "stop")


def config(store, *, request_view=None):
    manager = ToolManager([], [], store=store, events=Events(), settings=lambda: Settings(), session_id="test")
    return ConversationConfig("test", profile=lambda: ModelProfile(model="fixture"), tools=lambda: manager,
                              request_view=request_view)


@pytest.fixture
async def store(tmp_path):
    value = ProjectStore(tmp_path / "context.db")
    await value.open()
    for body in initial_history():
        await value.message("test", body)
    yield value
    await value.close()


async def prepared_history():
    history = initial_history()
    prepared, budget = await ContextManager().prepare(history, 2048, summary)
    assert budget["compacted"]
    return history, prepared


async def summary(text):
    return "Earlier task data retained."


@pytest.mark.asyncio
async def test_checkpoint_reuses_summary_and_verbatim_complete_suffix_without_aliases():
    history, prepared = await prepared_history()
    checkpoint = ContextManager.checkpoint(history, prepared)
    suffix = [*exchange(), {"role": "user", "content": "Correction: do not edit the API."}]
    complete = deepcopy(history + suffix)
    resumed = ContextManager.resume(complete, checkpoint)
    assert resumed == prepared + suffix
    ContextManager._groups(resumed)

    async def unexpected(text):
        pytest.fail("Fitting resumed context must not summarize again")

    result, budget = await ContextManager().prepare(resumed, 2048, unexpected)
    assert not budget["compacted"] and result == prepared + suffix
    result[-3]["tool_calls"][0]["function"]["arguments"] = "changed"
    assert complete == history + suffix
    assert checkpoint["messages"] == prepared


@pytest.mark.asyncio
async def test_current_controls_refresh_without_resummarizing_task_data():
    history, prepared = await prepared_history()
    checkpoint = ContextManager.checkpoint(history, prepared)
    updated = deepcopy(history)
    updated[0]["content"] = "Updated controls and newly pinned feedback"
    resumed = ContextManager.resume(updated, checkpoint)
    assert resumed[0] == updated[0]
    assert resumed[1:] == prepared[1:]
    assert checkpoint["messages"][0] == history[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["edit", "delete", "reorder", "rewind", "control_boundary"])
async def test_changed_source_invalidates_checkpoint(change):
    history, prepared = await prepared_history()
    checkpoint = ContextManager.checkpoint(history, prepared)
    changed = deepcopy(history)
    if change == "edit":
        changed[1]["content"] = "Corrected old source"
    elif change == "delete":
        del changed[1]
    elif change == "reorder":
        changed[1:] = reversed(changed[1:])
    elif change == "rewind":
        changed = changed[:1]
    else:
        changed[0]["role"] = "developer"
    assert ContextManager.resume(changed, checkpoint) is None


@pytest.mark.asyncio
async def test_recompaction_rolls_saved_summary_forward_and_preserves_latest_direction():
    history, prepared = await prepared_history()
    latest = {"role": "user", "content": "LATEST CORRECTION: preserve the exact public API."}
    suffix = [{"role": "assistant", "content": "NEW_OBSERVATIONS " * 700}, latest]
    resumed = ContextManager.resume(history + suffix, ContextManager.checkpoint(history, prepared))
    sources = []

    async def compress(text):
        sources.append(text)
        return "Earlier and newer observations retained."

    result, budget = await ContextManager().prepare(resumed, 2048, compress)
    assert budget["compacted"] and result[-1] == latest
    assert sources and all("OLD_SOURCE_DETAIL" not in text for text in sources)
    assert all(latest["content"] not in text for text in sources)


@pytest.mark.asyncio
async def test_large_tool_result_after_current_input_does_not_recompress_tiny_prior_summary():
    history, prepared = await prepared_history()
    suffix = exchange("LARGE_NEW_TOOL_OBSERVATION " * 1000)
    resumed = ContextManager.resume(history + suffix, ContextManager.checkpoint(history, prepared))
    sources = []

    async def compress(text):
        sources.append(text)
        return "Tool observations retained."

    result, budget = await ContextManager().prepare(resumed, 2048, compress)
    assert budget["compacted"]
    assert history[-1] in result
    assert any("LARGE_NEW_TOOL_OBSERVATION" in text for text in sources)
    assert all(history[-1]["content"] not in text for text in sources)
    ContextManager._groups(result)


@pytest.mark.asyncio
async def test_saved_tool_summary_after_human_input_never_displaces_current_direction():
    current = {"role": "user", "content": "PRESERVE THIS HUMAN DIRECTION: do not modify source."}
    history = [{"role": "system", "content": "Required controls"}, current,
               *exchange("LARGE_TOOL_OBSERVATIONS " * 900)]
    first, _ = await ContextManager().prepare(history, 2048, summary)
    assert first[1] == current and first[-1]["content"].startswith(ContextManager.summary_prefix)
    # Round-trip the checkpoint through storage's JSON representation, including
    # generated-summary provenance, before the next oversized context.
    checkpoint = json.loads(encode(ContextManager.checkpoint(history, first)))
    resumed = ContextManager.resume(history + [{"role": "assistant", "content": "MORE_OBSERVATIONS " * 900}], checkpoint)
    sources = []

    async def compress(text):
        sources.append(text)
        return "All prior observations retained."

    result, budget = await ContextManager().prepare(resumed, 2048, compress)
    assert budget["compacted"] and current in result
    assert all(current["content"] not in source for source in sources)
    ContextManager._groups(result)


@pytest.mark.asyncio
async def test_user_text_with_summary_prefix_is_still_current_human_input():
    current = {"role": "user", "content": ContextManager.summary_prefix + "This is my actual correction."}
    messages = initial_history() + [current]
    sources = []

    async def compress(text):
        sources.append(text)
        return "History retained."

    result, _ = await ContextManager().prepare(messages, 2048, compress)
    assert current in result
    assert all(current["content"] not in source for source in sources)


@pytest.mark.asyncio
async def test_oversized_latest_input_reports_size_instead_of_summarizing_it():
    messages = initial_history() + [{"role": "user", "content": "CURRENT DIRECTION " * 1000}]
    original = deepcopy(messages)

    async def unexpected(text):
        pytest.fail("The oversized current direction must not be summarized")

    with pytest.raises(ContextError, match="latest user input"):
        await ContextManager().prepare(messages, 2048, unexpected)
    assert messages == original


@pytest.mark.asyncio
async def test_engine_reuses_durable_context_after_new_input_and_control_revision(store):
    client, cfg = Client(), config(store)
    engine = ConversationEngine(store, client)
    original = await store.messages("test", include_partial=True)
    await engine.run_turn(cfg)
    summary_count = len(client.summaries)
    checkpoint = await records.retained_context(store, "test")
    assert summary_count > 0 and checkpoint is not None
    assert (await store.messages("test", include_partial=True))[:len(original)] == original
    await records.bump_control(store, "test")  # Pause/resume revisions do not change history.
    receipt = await engine.accept_input(cfg, "New correction stays verbatim.", "correction")
    await engine.run_turn(cfg, receipt)
    assert len(client.summaries) == summary_count
    assert client.requests[-1][-2:] == [{"role": "assistant", "content": "Recorded response"},
                                       {"role": "user", "content": "New correction stays verbatim."}]
    assert await records.retained_context(store, "test") == checkpoint


@pytest.mark.asyncio
async def test_request_view_refreshes_live_controls_without_discarding_checkpoint(store):
    controls = ["Current controls"]

    def view(messages):
        return [{"role": "system", "content": controls[0]}, *messages[1:]]

    client, cfg = Client(), config(store, request_view=view)
    engine = ConversationEngine(store, client)
    await engine.run_turn(cfg)
    count = len(client.summaries)
    controls[0] = "Updated required controls and newly pinned feedback"
    await engine.run_turn(cfg, await engine.accept_input(cfg, "Apply the new correction."))
    assert len(client.summaries) == count
    assert client.requests[-1][0] == {"role": "system", "content": controls[0]}
    assert client.requests[-1][-1] == {"role": "user", "content": "Apply the new correction."}


@pytest.mark.asyncio
async def test_restart_and_consistent_database_restore_reuse_checkpoint(store, tmp_path):
    client = Client()
    await ConversationEngine(store, client).run_turn(config(store))
    checkpoint = await records.retained_context(store, "test")
    backup_path = tmp_path / "restored.db"
    await store.backup(backup_path)
    original_path = store.path
    await store.close()
    for path in (original_path, backup_path):
        reopened = ProjectStore(path)
        await reopened.open()
        try:
            resumed_client = Client()
            engine, cfg = ConversationEngine(reopened, resumed_client), config(reopened)
            assert await records.retained_context(reopened, "test") == checkpoint
            await engine.run_turn(cfg, await engine.accept_input(cfg, "Continue after reopening."))
            assert not resumed_client.summaries
            assert resumed_client.requests[-1][-1]["content"] == "Continue after reopening."
        finally:
            await reopened.close()


@pytest.mark.asyncio
async def test_successful_compaction_survives_main_generation_failure(store):
    client = Client()
    client.fail_main = True
    with pytest.raises(RuntimeError, match="Temporary provider"):
        await ConversationEngine(store, client).run_turn(config(store))
    count = len(client.summaries)
    checkpoint = await records.retained_context(store, "test")
    assert checkpoint is not None
    await ConversationEngine(store, client).run_turn(config(store))
    assert len(client.summaries) == count
    assert await records.retained_context(store, "test") == checkpoint


@pytest.mark.asyncio
async def test_context_window_retry_reuses_completed_compaction(store):
    client = Client()
    client.refresh_window = True
    await ConversationEngine(store, client).run_turn(config(store))
    assert len(client.requests) == 2
    # A rejected context window receives more headroom on the second request.
    # The original user direction survives any additional compaction.
    assert client.requests[0][-1] == client.requests[1][-1]
    requests = await store.query("SELECT input FROM requests ORDER BY created,rowid")
    generation_inputs = [json.loads(row["input"]) for row in requests
                         if json.loads(row["input"])["purpose"] == "conversation"]
    assert len(generation_inputs) == 2
    assert generation_inputs[1]["budget"]["context_correction_reserve"] > 0


@pytest.mark.asyncio
async def test_stopped_recompaction_does_not_replace_previous_checkpoint(store):
    client, cfg = Client(), config(store)
    engine = ConversationEngine(store, client)
    await engine.run_turn(cfg)
    checkpoint = await records.retained_context(store, "test")
    await store.message("test", {"role": "assistant", "content": "NEW_LARGE_DATA " * 1000})
    receipt = await engine.accept_input(cfg, "Preserve this newest direction.")
    client.block_summary = True
    task = asyncio.create_task(engine.run_turn(cfg, receipt))
    await asyncio.wait_for(client.summary_entered.wait(), 2)
    engine.stop("test")
    result = await asyncio.wait_for(task, 2)
    assert result["status"] == "stopped"
    assert await records.retained_context(store, "test") == checkpoint
    assert (await store.messages("test"))[-1]["body"]["content"] == "Preserve this newest direction."


@pytest.mark.asyncio
async def test_new_input_during_recompaction_replays_with_correction_verbatim(store):
    client, cfg = Client(), config(store)
    engine = ConversationEngine(store, client)
    await engine.run_turn(cfg)
    await store.message("test", {"role": "assistant", "content": "LARGE_NEW_OBSERVATIONS " * 1000})
    receipt = await engine.accept_input(cfg, "Earlier direction.")
    client.block_summary = True
    task = asyncio.create_task(engine.run_turn(cfg, receipt))
    await asyncio.wait_for(client.summary_entered.wait(), 2)
    client.block_summary = False
    correction = await engine.accept_input(cfg, "LATEST HUMAN CORRECTION: keep all source files unchanged.")
    result = await asyncio.wait_for(task, 2)
    assert result["status"] == "completed"
    assert await records.receipt_outcome(store, correction) is not None
    assert client.requests[-1][-1] == {"role": "user", "content": correction["text"]}
    assert all(correction["text"] not in m["content"] for request in client.summaries for m in request)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["edit", "delete", "clear"])
async def test_engine_invalidates_context_after_transcript_mutation(store, mutation):
    client = Client()
    engine = ConversationEngine(store, client)
    cfg = config(store)
    await engine.run_turn(cfg)
    rows = await store.messages("test")
    if mutation == "edit":
        await store.execute("UPDATE messages SET body=? WHERE id=?",
                            (encode({"role": "user", "content": "Corrected history."}), rows[1]["id"]))
    elif mutation == "delete":
        await store.execute("DELETE FROM messages WHERE id=?", (rows[1]["id"],))
    else:
        await store.execute("DELETE FROM messages WHERE session_id=?", ("test",))
        await store.message("test", {"role": "system", "content": "Current controls"})
        await store.message("test", {"role": "user", "content": "Fresh conversation."})
    await engine.run_turn(cfg)
    assert await records.retained_context(store, "test") is None
    assert all(not str(m.get("content", "")).startswith(ContextManager.summary_prefix)
               for m in client.requests[-1])
    assert all("OLD_SOURCE_DETAIL" not in str(m.get("content", "")) for m in client.requests[-1])


@pytest.mark.asyncio
async def test_one_shot_does_not_replace_conversation_checkpoint(store):
    client = Client()
    engine, cfg = ConversationEngine(store, client), config(store)
    await engine.run_turn(cfg)
    checkpoint = await records.retained_context(store, "test")
    await engine.one_shot(cfg, initial_history(), [], purpose="ticket_draft")
    assert await records.retained_context(store, "test") == checkpoint


@pytest.mark.asyncio
async def test_tool_continuation_reuses_checkpoint_and_keeps_complete_new_exchange(store):
    class ReadFile(Tool):
        name = "read_file"

        async def execute(self, args, execution_id):
            return {"observed": "fixture source"}

    class ToolClient(Client):
        async def stream(self, profile, messages, schemas, on_delta=None):
            result = await super().stream(profile, messages, schemas, on_delta)
            if messages[0].get("content") != self.summary_instruction and len(self.requests) == 1:
                return Turn(exchange()[0], "fixture", None, "tool_calls")
            return result

    client = ToolClient()
    cfg = config(store)
    manager = ToolManager([ReadFile()], ["read_file"], store=store, events=Events(),
                          settings=lambda: Settings(), session_id="test")
    cfg.tools = lambda: manager
    await ConversationEngine(store, client).run_turn(cfg)
    assert len(client.requests) == 2
    assert client.requests[1][:-2] == client.requests[0]
    assert [m["role"] for m in client.requests[1][-2:]] == ["assistant", "tool"]
    ContextManager._groups(client.requests[1])
    requests = await store.query("SELECT input FROM requests ORDER BY created,rowid")
    assert [json.loads(row["input"])["purpose"] for row in requests][-2:] == ["conversation", "conversation"]


@pytest.mark.asyncio
async def test_application_continuation_does_not_displace_current_human_direction():
    current = {"role": "user", "content": "HUMAN CORRECTION: do not edit the API."}
    continuation = "Continue until the assigned step is complete."
    messages = [*initial_history(), current,
                {"role": "assistant", "content": "LARGE_FOLLOWUP_OBSERVATIONS " * 800},
                {"role": "user", "content": continuation}]
    sources = []

    async def compress(text):
        sources.append(text)
        return "Observed context retained."

    prepared, budget = await ContextManager().prepare(messages, 2048, compress, continuation=continuation)
    assert budget["compacted"] and current in prepared
    assert all(current["content"] not in text for text in sources)


@pytest.mark.asyncio
async def test_ordinary_continuation_reuses_checkpoint(store):
    client, cfg = Client(), config(store)
    cfg.continuation = "Continue the assigned step."

    def after_generation():
        if len(client.requests) == 2:
            cfg.continuation = None

    cfg.after_generation = after_generation
    await ConversationEngine(store, client).run_turn(cfg)
    assert len(client.requests) == 2
    assert client.requests[1][:-2] == client.requests[0]
    assert client.requests[1][-1] == {"role": "user", "content": "Continue the assigned step."}
    requests = await store.query("SELECT input FROM requests ORDER BY created,rowid")
    assert [json.loads(row["input"])["purpose"] for row in requests][-2:] == ["conversation", "conversation"]

@pytest.mark.asyncio
async def test_truncated_summary_retries_and_retains_a_valid_checkpoint(store):
    class TruncatedOnce(Client):
        def __init__(self):
            super().__init__()
            self.summary_calls = 0

        async def stream(self, profile, messages, schemas, on_delta=None):
            if messages[0].get("content", "").startswith("Summarize the preceding conversation"):
                self.summary_calls += 1
                if self.summary_calls == 1:
                    return Turn({"role": "assistant", "content": "Partial summary"}, "fixture", None, "length")
                return Turn({"role": "assistant", "content": "Retained earlier observations."},
                            "fixture", None, "stop")
            return await super().stream(profile, messages, schemas, on_delta)

    client = TruncatedOnce()
    cfg = config(store)
    cfg.summary_retry_attempts = lambda: 3
    before = await store.messages("test")
    await ConversationEngine(store, client).run_turn(cfg)
    assert client.summary_calls >= 2
    assert await records.retained_context(store, "test") is not None
    assert (await store.messages("test"))[:len(before)] == before
    requests = await store.query("SELECT * FROM requests ORDER BY created,rowid")
    summaries = [row for row in requests if json.loads(row["input"])["purpose"] == "context_preparation"]
    assert [json.loads(row["input"])["budget"]["summary_attempt"] for row in summaries] == (
        [1] + [2] * (len(summaries) - 1)
    )
    assert "truncated" in summaries[0]["error"]


@pytest.mark.asyncio
async def test_reasoning_only_summary_exhausts_configured_retries_without_losing_history(store):
    class ReasoningOnly(Client):
        def __init__(self):
            super().__init__()
            self.summary_calls = 0

        async def stream(self, profile, messages, schemas, on_delta=None):
            if messages[0].get("content", "").startswith("Summarize the preceding conversation"):
                self.summary_calls += 1
                return Turn({"role": "assistant", "content": None,
                             "reasoning_content": "Internal thinking is not a summary."},
                            "fixture", None, "stop")
            return await super().stream(profile, messages, schemas, on_delta)

    client = ReasoningOnly()
    cfg = config(store)
    cfg.summary_retry_attempts = lambda: 2
    before = await store.messages("test", include_partial=True)
    with pytest.raises(ContextError, match="failed after 2 attempt"):
        await ConversationEngine(store, client).run_turn(cfg)
    assert client.summary_calls == 2
    assert await records.retained_context(store, "test") is None
    assert await store.messages("test", include_partial=True) == before
    requests = await store.query("SELECT * FROM requests ORDER BY created,rowid")
    assert len(requests) == 2
    assert all("empty" in row["error"] for row in requests)

