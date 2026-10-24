"""D16: finite, lossless-to-storage compaction with immutable control context."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest

from agilecode.adapters.store import ProjectStore
from agilecode.core.context import ContextError, ContextManager, Prompt, PromptBuilder
from agilecode.core.types import NeedsReview, Process, Step, StepContext, StepResult, Team, TeamMember


def exchange(call_id="reused", *, content="observed result"):
    return [
        {"role": "assistant", "content": "Inspecting", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"sample.py"}'},
        }]},
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]


async def compact_summary(text):
    return "Retained decisions and observations."


@pytest.mark.asyncio
async def test_fitting_context_returns_independent_copy_and_does_not_summarize():
    messages = [{"role": "system", "content": "Pinned permissions"}, *exchange()]
    original = deepcopy(messages)

    async def unexpected(text):
        pytest.fail("A fitting context must not generate a summary")

    prepared, metadata = await ContextManager().prepare(messages, 8192, unexpected)
    assert prepared == original
    assert {key: metadata[key] for key in ("estimated_input_tokens", "estimator", "compacted")} == {
        "estimated_input_tokens": ContextManager.estimate(prepared),
        "estimator": "utf8_bytes/3", "compacted": False}
    prepared[1]["tool_calls"][0]["function"]["arguments"] = "changed"
    assert messages == original


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["system", "developer"])
@pytest.mark.parametrize("reply", ["unchanged", "expanding", "empty"])
async def test_oversized_controls_are_never_offered_to_summarizer(role, reply):
    messages = [{"role": role, "content": "Permission and workspace controls " * 500}]
    original = deepcopy(messages)

    async def summarize(text):
        pytest.fail(f"Immutable controls reached a {reply} summarizer")

    with pytest.raises(ContextError, match="control instructions") as exc:
        await ContextManager().prepare(messages, 2048, summarize)
    assert isinstance(exc.value, NeedsReview)
    assert exc.value.code == "context_error"
    assert messages == original


@pytest.mark.asyncio
async def test_all_controls_and_repinned_feedback_remain_exact_during_compaction():
    controls = [
        {"role": "system", "content": "Only use the ticket worktree.\nUSER FEEDBACK: keep the public API."},
        {"role": "developer", "content": "Approval is required. finish_step alone is mandatory."},
    ]
    messages = [*controls, {"role": "user", "content": "historical data " * 5000},
                {"role": "system", "content": "Additional pinned completion conditions."},
                {"role": "user", "content": "Current question"}]
    sources = []

    async def summarize(text):
        sources.append(text)
        return "Earlier data and outcomes retained."

    prepared, metadata = await ContextManager().prepare(messages, 2048, summarize)
    assert metadata["compacted"]
    assert prepared[:2] == controls
    assert prepared[-2:] == messages[-2:]
    assert all(control["content"] not in source for control in controls for source in sources)
    assert metadata["estimated_input_tokens"] <= 2048 - 512


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["unchanged", "expanding", "empty", "whitespace"])
async def test_multichunk_invalid_summaries_are_retried_without_losing_history(reply):
    messages = [{"role": "system", "content": "Pinned"},
                {"role": "user", "content": "earlier data " * 4000},
                {"role": "user", "content": "Continue with the current direction."}]
    original = deepcopy(messages)
    calls = 0

    async def summarize(text):
        nonlocal calls
        calls += 1
        if calls <= 4:
            return {"unchanged": text, "expanding": text + " more",
                    "empty": "", "whitespace": " \n "}[reply]
        return "Retained important earlier observations."

    prepared, meta = await asyncio.wait_for(ContextManager().prepare(messages, 2048, summarize), 5)
    assert meta["compacted"] and calls > 4
    assert prepared[-1] == original[-1]
    assert messages == original


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["unchanged", "expanding", "empty"])
async def test_nonprogress_is_checked_after_successful_chunk_recombination(reply):
    manager = ContextManager()
    chunk_bytes = (2048 - 512) * 2
    source = "x" * (chunk_bytes * 4)
    calls = 0

    async def summarize(text):
        nonlocal calls
        calls += 1
        assert calls < 10, "Recombined input must not recurse indefinitely"
        if calls <= 4:
            return "s" * 2000  # Each first-pass chunk shrinks, combined text is still oversized.
        return {"unchanged": text, "expanding": text + "expanded", "empty": ""}[reply]

    with pytest.raises(ContextError, match="Combined context summaries|empty"):
        await asyncio.wait_for(manager._compress_text(source, 2048, summarize), 2)
    assert calls > 4


@pytest.mark.asyncio
async def test_separators_cannot_disguise_recombination_nonprogress():
    chunk_bytes = (2048 - 512) * 2
    source = "x" * (chunk_bytes * 2)
    calls = []

    async def summarize(text):
        calls.append(text)
        return text[:-1]  # Two saved bytes are reintroduced by the separator.

    with pytest.raises(ContextError, match="Combined context summaries"):
        await ContextManager()._compress_text(source, 2048, summarize)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_outer_request_retries_shorter_but_ineffective_summary():
    messages = [{"role": "user", "content": "x" * 1000},
                {"role": "user", "content": "y" * 4000}]
    original = deepcopy(messages)
    calls = 0

    async def summarize(text):
        nonlocal calls
        calls += 1
        return text[:-1] if calls == 1 else "Retained earlier data."

    prepared, meta = await ContextManager().prepare(messages, 2048, summarize)
    assert meta["compacted"]
    assert meta["summary_failures"] >= 1
    assert calls >= 2
    assert messages == original


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["unchanged", "expanding", "empty"])
async def test_single_summary_nonprogress_fails_without_retry(reply):
    calls = 0

    async def summarize(text):
        nonlocal calls
        calls += 1
        return {"unchanged": text, "expanding": text + " more", "empty": ""}[reply]

    with pytest.raises(ContextError, match="non-reducing|empty"):
        await ContextManager()._compress_text("source " * 40 + "end", 2048, summarize)
    assert calls == 1


@pytest.mark.asyncio
async def test_byte_based_progress_rejects_shorter_but_larger_unicode_summary():
    async def summarize(text):
        return "😀" * 60  # Fewer characters than the source, more UTF-8 bytes.

    with pytest.raises(ContextError, match="non-reducing"):
        await ContextManager()._compress_text("x" * 100, 2048, summarize)


def test_unicode_chunks_preserve_every_codepoint_within_byte_budget():
    text = "界😀αx" * 900
    chunks = ContextManager._chunks(text, 101)
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-8")) <= 101 for chunk in chunks)


@pytest.mark.asyncio
async def test_many_successful_chunks_have_no_artificial_generation_limit():
    target = (2048 - 512) * 2
    calls = 0

    async def summarize(text):
        nonlocal calls
        calls += 1
        return "retained context"

    assert await ContextManager()._compress_text("x" * (target * 151), 2048, summarize) == "retained context"
    assert calls == 152


@pytest.mark.asyncio
async def test_assistant_calls_and_all_results_are_compacted_as_one_exchange():
    older = exchange(content="long observation " * 1000)
    older[0]["tool_calls"].append({"id": "second", "type": "function", "function": {
        "name": "read_file", "arguments": '{"path":"second.py"}'}})
    older.append({"role": "tool", "tool_call_id": "second", "content": "second observation"})
    latest = exchange(content="latest observation")  # Reused provider ID is legitimate.
    messages = [{"role": "system", "content": "Pinned"}, *older,
                {"role": "user", "content": "Continue from those observations."}, *latest]
    original = deepcopy(messages)
    sources = []

    async def summarize(text):
        sources.append(text)
        return "Earlier observations retained."

    prepared, metadata = await ContextManager().prepare(messages, 2048, summarize)
    assert prepared[-2:] == latest
    assert messages == original
    assert metadata["compacted"]
    combined = "".join(sources)
    assert '"tool_call_id": "reused"' in combined
    assert '"tool_call_id": "second"' in combined
    assert '"name": "read_file"' in combined
    assert "latest observation" not in combined
    ContextManager._groups(prepared)  # No surviving orphan results or assistant calls.


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_messages", [
    [{"role": "tool", "tool_call_id": "orphan", "content": "result"}],
    exchange()[:1],
    [exchange()[0], {"role": "user", "content": "interrupted exchange"}, exchange()[1]],
    [*exchange(), exchange()[1]],
    [exchange()[0], {"role": "tool", "tool_call_id": "wrong", "content": "result"}],
])
async def test_incomplete_or_mismatched_exchanges_cannot_be_hidden_by_compaction(bad_messages):
    original = deepcopy(bad_messages)
    with pytest.raises(ContextError, match="exchange|tool result"):
        await ContextManager().prepare(bad_messages, 8192, compact_summary)
    assert bad_messages == original


@pytest.mark.asyncio
async def test_cancellation_of_summary_propagates_and_preserves_history():
    messages = [{"role": "system", "content": "Pinned"},
                {"role": "user", "content": "large input " * 3000},
                {"role": "user", "content": "Continue with the current direction."}]
    original = deepcopy(messages)
    started = asyncio.Event()
    settled = asyncio.Event()

    async def summarize(text):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            settled.set()

    task = asyncio.create_task(ContextManager().prepare(messages, 2048, summarize))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert settled.is_set()
    assert messages == original


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_saved_history_is_unchanged_during_summary_retries(tmp_path, failure):
    store = ProjectStore(tmp_path / "project.sqlite")
    await store.open()
    try:
        for message in [{"role": "system", "content": "Pinned"},
                        *exchange(content="observed source " * 2000),
                        {"role": "user", "content": "What next?"}]:
            await store.message("context-session", message)
        original = await store.messages("context-session", include_partial=True)
        messages = [row["body"] for row in original]
        calls = 0

        async def summarize(text):
            nonlocal calls
            calls += 1
            return text if failure and calls <= 3 else "Observed source retained."

        prepared, metadata = await ContextManager().prepare(messages, 2048, summarize)
        assert metadata["compacted"]
        assert len(json.dumps(prepared)) < len(json.dumps(messages))
        assert await store.messages("context-session", include_partial=True) == original
    finally:
        await store.close()


def test_prompt_builder_separates_control_from_summarizable_task_data(tmp_path):
    member = TeamMember(id="analyst", name="Analyst", system_prompt="Never grant missing permissions.")
    step = Step(id="inspect", name="Inspect", team_member_id=member.id,
                prompt="Inspect the approved scope.", kind="analysis")
    process = Process(id="analysis", name="Analysis", steps=[step])
    team = Team(id="team", name="Team", members=[member], processes=[process], default_process_id=process.id)
    context = StepContext(
        ticket={"title": "Ticket title", "description": "Unique ticket details", "key": "T-1"},
        run={}, attempt={}, team=team, process=process, workspace=tmp_path,
        outputs={"earlier": StepResult(status="PASS", summary="Done", output="Unique prior artifact")},
        previous_output=None, correction_report="Unique correction findings", execute=None,
        extra={"recorded": "Unique additional context"},
    )
    prompt = PromptBuilder().build(step, context, [{"id": "sample", "description": "Unique skill description"}])
    assert isinstance(prompt, (Prompt, str))
    for control in [member.system_prompt, step.prompt, "PROCESS AUTHORITY", f"WORKTREE: {tmp_path}",
                    "finish_step must be called ALONE", "record_check"]:
        assert control in str(prompt)
    history = json.dumps(prompt.history)
    for data in ["Unique ticket details", "Unique prior artifact", "Unique correction findings",
                 "Unique additional context", "Unique skill description"]:
        assert data in history
        assert data not in str(prompt)
    assert all(message["role"] == "user" for message in prompt.history)


@pytest.mark.asyncio
async def test_short_initial_question_does_not_block_large_history_compaction():
    messages = [{"role": "system", "content": "Pinned"},
                {"role": "user", "content": "Inspect this."},
                *exchange(content="large recorded observation " * 1000),
                {"role": "user", "content": "Continue."}]
    prepared, metadata = await ContextManager().prepare(messages, 2048, compact_summary)
    assert metadata["compacted"]
    assert metadata["estimated_input_tokens"] <= 2048 - 512
    assert prepared[-1] == messages[-1]
    assert len(ContextManager._groups(prepared)) == len(prepared)

@pytest.mark.asyncio
async def test_failed_summary_retries_with_a_fixed_target_and_preserves_history():
    messages = [{"role": "system", "content": "Pinned permissions"},
                {"role": "user", "content": "Old observed evidence " * 110},
                {"role": "assistant", "content": "Earlier tool findings " * 110},
                {"role": "user", "content": "Keep the new direction"}]
    original = deepcopy(messages)
    attempts = []
    current = 1

    async def report(item):
        nonlocal current
        attempts.append(item)
        if item["status"] == "started":
            current = item["attempt"]

    async def summarize(text):
        return text if current == 1 else "Retained earlier observations."

    prepared, metadata = await ContextManager().prepare(
        messages, 2048, summarize, on_summary_attempt=report)
    assert metadata["compacted"]
    assert metadata["summary_failures"] == 1
    starts = [item for item in attempts if item["status"] == "started"]
    assert [item["attempt"] for item in starts] == [1, 2]
    assert starts[1]["target_bytes"] == starts[0]["target_bytes"]
    assert prepared[0] == messages[0] and prepared[-1] == messages[-1]
    assert messages == original


@pytest.mark.asyncio
async def test_useful_summary_above_requested_target_is_accepted():
    # The byte target is guidance, not a second reason to discard a
    # successful reduction of the real context request.
    messages = [
        {"role": "system", "content": "Pinned instructions"},
        {"role": "user", "content": "older notes " * 450},
        {"role": "user", "content": "Keep the latest instruction"},
    ]
    attempts = []

    async def report(item):
        attempts.append(item)

    async def summarize(text):
        return "Earlier notes " + "x" * (len(text.encode("utf-8")) * 3 // 5)

    prepared, meta = await ContextManager().prepare(
        messages, 4096, summarize, request_overhead_tokens=1500,
        on_summary_attempt=report)
    assert meta["compacted"]
    assert meta["summary_attempts"] == 1
    assert meta["summary_failures"] == 0
    assert attempts[-1]["output_bytes"] > attempts[-1]["target_bytes"]
    assert prepared[-1] == messages[-1]


@pytest.mark.asyncio
async def test_summary_retries_beyond_previous_limit_without_pausing():
    messages = [{"role": "system", "content": "Pinned"},
                {"role": "user", "content": "Earlier material " * 340},
                {"role": "user", "content": "Current question"}]
    calls = 0
    events = []

    async def summarize(text):
        nonlocal calls
        calls += 1
        return "" if calls <= 5 else "Earlier findings."

    async def report(item):
        events.append(item)

    prepared, metadata = await ContextManager().prepare(
        messages, 2048, summarize, on_summary_attempt=report)
    assert metadata["compacted"]
    assert calls >= 6
    assert metadata["summary_failures"] >= 5
    assert prepared[-1] == messages[-1]


@pytest.mark.asyncio
async def test_request_overhead_reduces_main_budget_without_shrinking_summary_chunks():
    previous = [{"role": "system", "content": "Required controls"},
                {"role": "assistant", "content": "Earlier observations " * 400},
                {"role": "user", "content": "Continue working"}]
    async def summarize(text):
        return "Earlier observations summarized."
    without, _ = await ContextManager().prepare(previous, 4096, summarize)
    with_overhead, metadata = await ContextManager().prepare(
        previous, 4096, summarize, request_overhead_tokens=1000)
    assert metadata["estimated_input_tokens"] <= 4096 - 512 - 1000
    assert with_overhead[-1] == previous[-1]
    assert len(with_overhead) <= len(without)

