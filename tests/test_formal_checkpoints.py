"""User-enabled, PM-managed engineering checkpoints do not impose phase gates."""
import asyncio

import pytest

from agilecode.core.application import Application
from agilecode.core.types import AgileCodeError
from test_project_manager import ProjectManagerClient
from test_ticket_pause import PausableClient
from test_git_recovery import setup_run


@pytest.mark.asyncio
async def test_pm_checkpoint_review_and_user_decisions(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ProjectManagerClient())
    await app.open(initialize=True)
    try:
        pm = app.project_manager
        assert not pm.settings.formal_checkpoints_enabled
        assert "FORMAL SYSTEMS ENGINEERING CHECKPOINTS ARE ENABLED" not in pm._system_prompt()

        await pm.save_settings({
            "formal_checkpoints_enabled": True,
            "formal_checkpoints": ["PDR", "CDR"],
        })
        assert pm.settings.formal_checkpoints == ["PDR", "CDR"]
        assert "- [ ] PDR" in pm._system_prompt()
        assert "- [ ] CDR" in pm._system_prompt()
        assert "- [ ] SRR" not in pm._system_prompt()

        ticket = await app.create_ticket("Prepare PDR slides", "Prepare technical review slides.")
        presentation = repository / "pdr.md"
        presentation.write_text("# Preliminary design\n\nArchitecture and risks.\n\n---\n\n# Tradeoffs\n\nAlternatives.")
        with pytest.raises(AgileCodeError, match="Done"):
            await pm._begin_checkpoint_review({
                "checkpoint": "PDR", "ticket": ticket["key"], "presentation_path": "pdr.md",
            }, "early-review")

        # Stand in for the ordinary completed-and-integrated presentation ticket.
        await app.store.execute("UPDATE tickets SET state='Done' WHERE id=?", (ticket["id"],))
        await pm.resume()
        assert pm.state["status"] == "running"
        result = await pm._begin_checkpoint_review({
            "checkpoint": "PDR", "ticket": ticket["key"], "presentation_path": "pdr.md",
        }, "pdr-review")
        assert result["review"]["checkpoint"] == "PDR"
        assert pm.state["active_checkpoint"]["ticket"] == ticket["key"]
        assert pm.state["status"] == "running"
        assert "# Tradeoffs" in (await pm.checkpoint_presentation())["markdown"]

        await pm.checkpoint_decision("no_go")
        assert pm.state["active_checkpoint"] is None
        assert not pm.state["completed_checkpoints"]
        assert pm.state["checkpoint_reviews"][-1]["decision"] == "no_go"

        await pm._begin_checkpoint_review({
            "checkpoint": "PDR", "ticket": ticket["key"], "presentation_path": "pdr.md",
        }, "pdr-re-review")
        await pm.checkpoint_decision("go")
        assert pm.state["completed_checkpoints"] == ["PDR"]
        assert "- [x] PDR" in pm._system_prompt()
        assert "- [ ] CDR" in pm._system_prompt()
        assert [review["decision"] for review in pm.state["checkpoint_reviews"]] == ["no_go", "go"]
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_checkpoint_pause_keeps_pm_running_and_stops_ticket(repository, tmp_path):
    client = PausableClient()
    app, ticket, run = await setup_run(repository, tmp_path / "global", client)
    try:
        await app.project_manager.resume()  # Restricted mode; no independent PM model request.
        await asyncio.wait_for(client.started.wait(), 3)
        result = await app.execution_ops.pause_tickets()
        assert result["errors"] == []
        assert result["paused_tickets"] >= 1
        assert (await app.store.get("runs", run["id"]))["state"] == "paused"
        assert app.project_manager.state["status"] == "running"
    finally:
        await app.close()
