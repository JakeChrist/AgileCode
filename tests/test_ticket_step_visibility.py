"""Ticket step indicators and Project Manager's read-only execution inspection."""
import asyncio
from pathlib import Path

import pytest

from agilecode.adapters.store import encode
from agilecode.core.application import Application
from agilecode.core.defaults import requirements_engineering_team, standard_team
from agilecode.core.types import AgileCodeError, Event, now, uid
from agilecode.runtime.conversation_records import controller
from conftest import ScriptClient


async def add_saved_run(app, ticket, team, workspace, *, index=0, state="paused"):
    """Install a saved run without launching an unrelated model execution."""
    identity = uid()
    workspace.mkdir(parents=True, exist_ok=True)
    run = {
        "id": identity, "ticket_id": ticket["id"],
        "snapshot": encode({
            "ticket": ticket, "team": team.model_dump(mode="json"),
            "process_id": ticket["process_id"],
        }),
        "state": state, "step_index": index, "outputs": "{}",
        "corrections": 0, "workspace": str(workspace),
        "branch": "test/" + identity, "base_branch": "master",
        "base_oid": await app.git.head(), "board_transition_id": "test",
        "created": now(), "updated": now(),
    }
    await app.store.insert("runs", run)
    await app.store.update("tickets", ticket["id"], selected_run_id=identity)
    return run


async def add_attempt(app, run, step, number, status, result):
    identity, session = uid(), uid()
    await app.store.insert("attempts", {
        "id": identity, "run_id": run["id"], "step_id": step,
        "number": number, "session_id": session,
        "member": "{}", "model": "{}", "status": status,
        "result": encode(result) if result else None,
        "context": "{}", "created": now(), "phase": "development",
    })
    return identity, session


@pytest.mark.asyncio
async def test_board_identifies_actual_process_step_and_completion(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        team = standard_team()
        ticket = await app.create_ticket("Twelve steps", "Inspect step position.")
        run = await add_saved_run(app, ticket, team, tmp_path / "worktree")
        process = team.process(ticket["process_id"])
        assert len(process.steps) == 12

        for index in (0, 7, 8, 10, 11):
            await app.store.update("runs", run["id"], step_index=index)
            card = next(x for x in await app.board() if x["id"] == ticket["id"])
            assert card["step_position"] == index + 1
            assert card["step_total"] == 12
            assert card["step_name"] == process.steps[index].name
            assert not card["step_complete"]
            assert "run_snapshot" not in card

        # A correction returns to the earlier configured step, not a running
        # total of attempted/completed work.
        await app.store.update("runs", run["id"], step_index=5, corrections=1)
        card = next(x for x in await app.board() if x["id"] == ticket["id"])
        assert (card["step_position"], card["step_name"]) == (6, process.steps[5].name)

        await app.store.update("runs", run["id"], step_index=12, state="completed")
        card = next(x for x in await app.board() if x["id"] == ticket["id"])
        assert card["step_complete"]
        assert card["step_position"] == card["step_total"] == 12
        assert not card["step_name"]

        short_team = requirements_engineering_team()
        short = await app.create_ticket(
            "Three steps", "Short process.", team_ref="builtin:requirements-engineering",
            process_id="requirements-definition",
        )
        short_run = await add_saved_run(app, short, short_team, tmp_path / "short",
                                        index=3, state="completed")
        card = next(x for x in await app.board() if x["id"] == short["id"])
        assert card["step_complete"]
        assert card["step_position"] == card["step_total"] == 3
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_pm_can_inspect_rejected_and_passed_attempts_and_conversations(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        ticket = await app.create_ticket("Review work", "Inspect real results.")
        run = await add_saved_run(app, ticket, standard_team(), tmp_path / "working", index=8)
        rejected, session = await add_attempt(
            app, run, "review", 1, "completed",
            {"status": "CHANGES_REQUIRED", "summary": "Review found an issue",
             "output": "Full rejected output with all findings."},
        )
        passed, _ = await add_attempt(
            app, run, "review", 2, "completed",
            {"status": "PASS", "summary": "Corrected", "output": "Full passed output."},
        )
        await app.store.update("runs", run["id"], outputs=encode({"review": passed}))
        await app.store.message(session, {"role": "assistant", "content": "Checking work",
                                         "tool_calls": [{"id": "tool-1", "type": "function",
                                                         "function": {"name": "read_file",
                                                                      "arguments": "{}"}}]})
        await app.store.message(session, {"role": "tool", "tool_call_id": "tool-1",
                                         "content": "Read result"})

        pm = app.project_manager
        tools = pm._tool_manager().tools
        for name in ("get_step_output", "get_step_conversation",
                     "list_ticket_files", "read_ticket_file"):
            assert name in tools

        brief = await pm._get_ticket({"ticket": ticket["key"]}, "test")
        assert {a["id"] for a in brief["attempts"]} == {rejected, passed}
        assert brief["current_step"]["position"] == 9

        result = await pm._get_step_output({"ticket": ticket["id"], "attempt": rejected}, "test")
        assert result["result"]["output"] == "Full rejected output with all findings."
        result = await pm._get_step_output({"ticket": ticket["id"], "attempt": passed}, "test")
        assert result["result"]["output"] == "Full passed output."

        live = controller(app.store).live_streams
        live[session] = {"request_id": "stream", "turn_id": "turn", "purpose": "conversation",
                         "text": ["Part", "ial output"], "reasoning": ["Reasoning"],
                         "tool_deltas": [{"name": "read_file"}]}
        page = await pm._get_step_conversation(
            {"ticket": ticket["id"], "attempt": rejected, "limit": 1}, "test")
        assert page["conversation"]["has_older"]
        assert page["conversation"]["messages"][0]["body"]["role"] == "tool"
        assert page["live"]["text"] == "Partial output"
        assert page["live"]["tool_deltas"] == [{"name": "read_file"}]
        live.pop(session)

        outsider = await app.create_ticket("Different", "Other ticket.")
        with pytest.raises(AgileCodeError):
            await pm._get_step_output({"ticket": outsider["id"], "attempt": rejected}, "test")
        with pytest.raises(AgileCodeError):
            await pm._get_step_conversation(
                {"ticket": outsider["id"], "attempt": rejected}, "test")
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_pm_can_read_only_inspect_ticket_worktree_and_event_summary(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        ticket = await app.create_ticket("Inspect files", "Check worktree evidence.")
        run = await add_saved_run(app, ticket, standard_team(), tmp_path / "ticket-work")
        workspace = Path(run["workspace"])
        (workspace / "report.txt").write_text("Original worktree content\n")
        pm = app.project_manager
        listing = await pm._list_ticket_files({"ticket": ticket["id"], "recursive": True}, "test")
        assert "report.txt" in {item["path"] for item in listing["files"]}
        result = await pm._read_ticket_file(
            {"ticket": ticket["key"], "path": "report.txt", "start_line": 1}, "test")
        assert result["content"] == "Original worktree content\n"
        assert (workspace / "report.txt").read_text() == result["content"]

        with pytest.raises(AgileCodeError):
            await pm._read_ticket_file(
                {"ticket": ticket["id"], "path": "../unrelated"}, "test")

        received = []
        async def capture(description):
            received.append(description)
        pm._deliver_update = capture
        pm._launch_delivery = lambda coro: asyncio.create_task(coro)
        pm.state["status"] = "running"
        pm.settings.mode = "fully_automated"
        pm._on_project_event(Event(
            "StepAttemptCompleted", ticket["id"], run["id"],
            data={"step": "Review Code", "result": {
                "status": "CHANGES_REQUIRED", "summary": "Specific defect",
            }},
        ))
        await asyncio.sleep(0)
        assert received and "Review Code" in received[-1]
        assert "CHANGES_REQUIRED" in received[-1]
        assert "Specific defect" in received[-1]
    finally:
        await app.close()


def test_ticket_card_does_not_cap_step_position():
    source = (Path(__file__).parents[1] /
              "src/agilecode/desktop/qml/TicketCard.qml").read_text()
    assert "Math.min(card.ticket.step_index + 1, 8)" not in source
    assert "card.ticket.step_name" in source
    assert "card.ticket.step_total" in source
