"""Project Manager summaries and conversation editing stay separate from ticket history."""
import pytest

from agilecode.adapters.store import encode
from agilecode.core.application import Application
from agilecode.core.types import AgileCodeError, now, uid
from conftest import ScriptClient


@pytest.mark.asyncio
async def test_pm_get_ticket_uses_completed_step_outputs_without_raw_history(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        ticket = await app.create_ticket("Summary target", "A short ticket description")
        blank = await app.project_manager._get_ticket({"ticket": ticket["id"]}, "tool")
        assert blank["ticket"]["description"] == "A short ticket description"
        assert blank["run"] is None

        run_id, first_id, last_id = uid(), uid(), uid()
        await app.store.insert("runs", {
            "id": run_id, "ticket_id": ticket["id"],
            "snapshot": encode({"large_internal_context": "X" * 100000}),
            "state": "integrated", "step_index": 2,
            "outputs": encode({"implement": first_id, "commit": last_id}),
            "workspace": "", "branch": "summary-branch",
            "base_branch": "master", "base_oid": "baseline",
            "created": now(), "updated": now(),
        })
        for step_id, attempt_id, detail in [
            ("implement", first_id, "Earlier implementation details"),
            ("commit", last_id, "Final step details " + "Z" * 9000),
        ]:
            await app.store.insert("attempts", {
                "id": attempt_id, "run_id": run_id, "step_id": step_id,
                "number": 1, "session_id": uid(), "member": "{}",
                "model": "{}", "status": "completed",
                "result": encode({"status": "PASS", "summary": step_id + " finished", "output": detail}),
                "context": encode({"historical_transcript": "SECRET" * 10000}),
                "created": now(),
            })
        result = await app.project_manager._get_ticket({"ticket": ticket["id"]}, "tool")
        assert [step["step"] for step in result["steps"]] == ["implement", "commit"]
        assert result["latest_step"]["summary"] == "commit finished"
        assert result["final_output"]["step"] == "commit"
        assert result["final_output"]["truncated"] is True
        assert len(result["final_output"]["output"]) == 8000
        assert "snapshot" not in str(result)
        assert "historical_transcript" not in str(result)
        assert len(str(result)) < 13000
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_pm_delete_assistant_message_cascades_its_tool_result(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        await app.store.message("project-manager", {"role": "user", "content": "Inspect tickets"})
        request_id = uid()
        await app.store.insert("requests", {
            "id": request_id, "session_id": "project-manager",
            "model": "fixture-model", "input": "{}", "status": "completed",
            "created": now(),
        })
        assistant_id = await app.store.message(
            "project-manager",
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call-1", "type": "function",
                             "function": {"name": "get_ticket", "arguments": "{}"}}]},
            request_id,
        )
        await app.store.message(
            "project-manager",
            {"role": "tool", "tool_call_id": "call-1", "content": "OLD RESULT"},
            request_id,
        )
        await app.store.message("project-manager", {"role": "assistant", "content": "More recent note"})
        removed = await app.project_manager.delete_message(assistant_id)
        assert removed == {"deleted": 2}
        history = [row["body"] for row in await app.store.messages("project-manager")]
        assert [row["role"] for row in history] == ["system", "user", "assistant"]
        assert history[-1]["content"] == "More recent note"
        # Audit request records are not coupled to the visible message history.
        assert await app.store.get("requests", request_id)
        with pytest.raises(AgileCodeError):
            await app.project_manager.delete_message(assistant_id)
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_pm_clear_chat_preserves_pm_state_and_tickets_and_requires_pause(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        ticket = await app.create_ticket("Keep this ticket", "Not chat history")
        app.project_manager.state["goals"] = ["Preserve PM project goals"]
        await app.store.message("project-manager", {"role": "user", "content": "Old instruction"})
        await app.store.message("project-manager", {"role": "assistant", "content": "Old response"})

        app.project_manager.state["status"] = "running"
        with pytest.raises(AgileCodeError, match="Pause"):
            await app.project_manager.clear_conversation()
        app.project_manager.state["status"] = "paused"

        cleared = await app.project_manager.clear_conversation()
        assert cleared == {"deleted": 2}
        rows = await app.store.messages("project-manager", include_partial=True)
        assert len(rows) == 1
        assert rows[0]["body"]["role"] == "system"
        assert app.project_manager.state["goals"] == ["Preserve PM project goals"]
        assert (await app.find_ticket(ticket["id"]))["title"] == "Keep this ticket"

        await app.store.message("project-manager", {"role": "user", "content": "Fresh context"})
        assert [m["body"]["role"] for m in await app.store.messages("project-manager")] == ["system", "user"]
    finally:
        await app.close()
