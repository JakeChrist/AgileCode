"""Analysis delivers repository files through the common ticket workflow."""
import asyncio
import subprocess
from pathlib import Path

import pytest

from agilecode.core.application import Application
from agilecode.core.types import Settings
from conftest import ScriptClient, AnalysisClient, git


class BlockingClient(ScriptClient):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def stream(self, profile, messages, tools, on_delta):
        self.started.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("team_ref,process_id", [
    ("builtin:requirements-engineering", "stakeholder-needs-definition"),
    ("builtin:requirements-engineering", "requirements-definition"),
    ("builtin:requirements-engineering", "requirements-validation"),
    ("builtin:requirements-engineering", "requirements-change-management"),
    ("builtin:requirements-engineering", "product-verification"),
    ("builtin:requirements-engineering", "product-validation"),
    ("builtin:systems-architecture", "architecture-development"),
    ("builtin:systems-architecture", "interface-management"),
    ("builtin:systems-architecture", "architecture-review"),
    ("builtin:systems-architecture", "architecture-change-management"),
])
async def test_analysis_process_commits_files_then_uses_normal_integration(repository, tmp_path, team_ref, process_id):
    app = Application(repository, tmp_path / "global", client=AnalysisClient())
    await app.open(initialize=True)
    try:
        await app.config.save(Settings(autonomy="full"))
        ticket = await app.create_ticket("Analyze", "Deliver analysis.md with observed findings.", team_ref=team_ref, process_id=process_id)
        await app.move_ticket(ticket["id"], "Ready")
        run = await app.start_ticket(ticket["id"])
        finished = await asyncio.wait_for(app.wait_for_run(run["id"]), 30)
        assert finished["state"] == "completed" and finished["final_oid"]
        assert (await app.find_ticket(ticket["id"]))["state"] == "In Progress"
        assert Path(run["workspace"]).exists()
        assert not (repository / "analysis.md").exists()
        candidate = await app.prepare_integration(ticket["id"])
        assert candidate["state"] == "awaiting_review"
        await app.approve_integration(candidate["id"], candidate["candidate_oid"], candidate["target_oid"])
        assert (await app.find_ticket(ticket["id"]))["state"] == "Done"
        assert "Evidence-backed" in (repository / "analysis.md").read_text()
        details = await app.project_manager._get_ticket({"ticket": ticket["id"]}, "test")
        assert "analysis.md" in details["final_output"]["output"]
        assert await app.store.query("SELECT * FROM checkpoints WHERE run_id=?", (run["id"],))
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_manual_moves_pause_and_resume_without_discarding_work(repository, tmp_path):
    client = BlockingClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        ticket = await app.create_ticket("Investigate", "Human controls board status.")
        assert (await app.move_ticket(ticket["id"], "Done"))["state"] == "Done"
        assert (await app.move_ticket(ticket["id"], "Backlog"))["state"] == "Backlog"

        # Direct Backlog -> In Progress starts the first run.
        assert (await app.move_ticket(ticket["id"], "In Progress"))["state"] == "In Progress"
        await asyncio.wait_for(client.started.wait(), 5)
        run = await app.latest_run(ticket["id"])
        assert run["state"] == "running"
        assert await app.project_manager._active_ticket_count() == 1

        worktree = Path(run["workspace"])
        assert worktree.exists()
        integration = asyncio.create_task(asyncio.Event().wait())
        app.integration_tasks[ticket["id"]] = integration

        # A user move does not require a commit and pauses ticket-owned work.
        assert (await app.move_ticket(ticket["id"], "Done"))["state"] == "Done"
        assert (await app.latest_run(ticket["id"]))["state"] == "paused"
        assert integration.done() and integration.cancelled()
        assert await app.project_manager._active_ticket_count() == 0
        assert worktree.exists()
        assert (await app.latest_run(ticket["id"]))["final_oid"] is None

        await app.move_ticket(ticket["id"], "Ready")
        assert (await app.latest_run(ticket["id"]))["state"] == "paused"
        await app.move_ticket(ticket["id"], "In Progress")
        assert (await app.latest_run(ticket["id"]))["state"] == "running"
        await app.move_ticket(ticket["id"], "Backlog")
        assert (await app.latest_run(ticket["id"]))["state"] == "paused"
        assert worktree.exists()
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_finished_implementation_can_be_moved_manually_without_merging(
    repository, tmp_path
):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        await app.config.save(Settings(autonomy="full"))
        ticket = await app.create_ticket("Change answer", "Implement and verify it.")
        await app.move_ticket(ticket["id"], "Ready")
        started = await app.start_ticket(ticket["id"])
        finished = await asyncio.wait_for(app.wait_for_run(started["id"]), 30)
        assert finished["final_oid"]
        assert (await app.move_ticket(ticket["id"], "Done"))["state"] == "Done"
        assert (await app.latest_run(ticket["id"]))["final_oid"] == finished["final_oid"]
        assert git(repository, "rev-parse", "HEAD") == finished["base_oid"]
        assert (await app.move_ticket(ticket["id"], "Backlog"))["state"] == "Backlog"
        assert Path(started["workspace"]).exists()
    finally:
        await app.close()


async def legacy_analysis(app):
    from agilecode.adapters.store import encode
    from agilecode.core.defaults import requirements_engineering_team
    from agilecode.core.types import now, uid
    ticket = await app.create_ticket("Legacy analysis", "Retain the historic findings.", team_ref="builtin:requirements-engineering", process_id="requirements-definition")
    team = requirements_engineering_team()
    identity = uid()
    run = dict(id=identity, ticket_id=ticket["id"], snapshot=encode({"ticket": ticket, "team": team.model_dump(mode="json"), "process_id": ticket["process_id"]}),
               state="completed", step_index=len(team.process(ticket["process_id"]).steps), outputs="{}", corrections=0,
               workspace=str(app.storage / "worktrees" / identity), branch="legacy/" + identity,
               base_branch="master", base_oid=await app.git.head(), created=now(), updated=now(), board_transition_id="legacy")
    await app.store.insert("runs", run)
    await app.move_ticket(ticket["id"], "Done")
    await app.store.update("tickets", ticket["id"], selected_run_id=identity, board_transition_id="legacy")
    await app.store.insert("operations", dict(id="analysis:" + identity, kind="analysis_completion", owner_id=identity,
        data="{}", status="completed", result="{}", created=now(), updated=now()))
    return ticket, await app.store.get("runs", identity)


@pytest.mark.asyncio
async def test_legacy_analysis_stays_historical_and_cannot_supply_files(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=AnalysisClient())
    await app.open(initialize=True)
    ticket, run = await legacy_analysis(app)
    await app.close()
    reopened = Application(repository, tmp_path / "global", client=AnalysisClient())
    await reopened.open()
    try:
        assert (await reopened.selected_run(ticket["id"]))["snapshot"] == run["snapshot"]
        assert (await reopened.find_ticket(ticket["id"]))["state"] == "Done"
        assert not Path(run["workspace"]).exists()
        assert "no committed" in (await reopened.ticket_execution(ticket["id"]))["deliverable_notice"]
        target = await reopened.create_ticket("Consumer", "Needs the missing report.")
        await reopened.add_dependency(target["id"], ticket["id"])
        await reopened.move_ticket(target["id"], "Ready")
        from agilecode.core.types import Conflict
        with pytest.raises(Conflict, match="no integrated committed deliverable"):
            await reopened.start_ticket(target["id"])
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_explicit_start_reopened_legacy_analysis_keeps_old_history(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=AnalysisClient())
    await app.open(initialize=True)
    try:
        await app.config.save(Settings(autonomy="full"))
        ticket, old = await legacy_analysis(app)
        await app.move_ticket(ticket["id"], "Backlog")
        await app.move_ticket(ticket["id"], "Ready")
        new = await app.start_ticket(ticket["id"])
        assert new["id"] != old["id"]
        finished = await asyncio.wait_for(app.wait_for_run(new["id"]), 30)
        assert finished["state"] == "completed" and finished["final_oid"]
        assert (await app.store.get("runs", old["id"]))["snapshot"] == old["snapshot"]
        assert not (await app.store.get("runs", old["id"]))["final_oid"]
        assert len(await app.store.query("SELECT * FROM runs WHERE ticket_id=?", (ticket["id"],))) == 2
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_no_analysis_file_cannot_complete_with_empty_commit(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        await app.config.save(Settings(autonomy="full"))
        ticket = await app.create_ticket("Analyze", "Deliver findings.", team_ref="builtin:requirements-engineering", process_id="requirements-definition")
        await app.move_ticket(ticket["id"], "Ready")
        run = await app.start_ticket(ticket["id"])
        stopped = await asyncio.wait_for(app.wait_for_run(run["id"]), 30)
        assert stopped["state"] == "paused" and not stopped["final_oid"]
        assert "No empty final commit" in stopped["error"]
        assert (await app.find_ticket(ticket["id"]))["state"] == "In Progress"
        assert Path(run["workspace"]).exists()
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_explicit_resume_upgrades_unfinished_legacy_analysis_without_losing_attempts(repository, tmp_path):
    from agilecode.adapters.store import decode, encode
    from agilecode.core.defaults import requirements_engineering_team

    class PausingAnalysisClient(AnalysisClient):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.hold = True

        async def stream(self, profile, messages, tools, on_delta):
            if self.hold:
                self.started.set()
                await asyncio.Event().wait()
            return await super().stream(profile, messages, tools, on_delta)

    client = PausingAnalysisClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        await app.config.save(Settings(autonomy="full"))
        ticket = await app.create_ticket("Resume legacy analysis", "Deliver analysis.md.",
            team_ref="builtin:requirements-engineering", process_id="requirements-definition")
        await app.move_ticket(ticket["id"], "Ready")
        run = await app.start_ticket(ticket["id"])
        await asyncio.wait_for(client.started.wait(), 5)
        await app.pause_run(ticket["id"])
        before = await app.store.query("SELECT id FROM attempts WHERE run_id=?", (run["id"],))
        snapshot = decode(run["snapshot"])
        old_team = requirements_engineering_team()
        for member in old_team.members:
            member.allowed_tools = [tool for tool in member.allowed_tools if tool not in
                                    {"write_file", "edit_file", "move_path", "delete_path", "run_command"}]
        snapshot["team"] = old_team.model_dump(mode="json")
        await app.store.update("runs", run["id"], snapshot=encode(snapshot))
        client.hold = False
        await app.resume_run(ticket["id"])
        finished = await asyncio.wait_for(app.wait_for_run(run["id"]), 30)
        assert finished["state"] == "completed" and finished["final_oid"], finished
        attempts = await app.store.query("SELECT id,status FROM attempts WHERE run_id=?", (run["id"],))
        assert all(any(row["id"] == old["id"] and row["status"] == "superseded" for row in attempts) for old in before)
        history = await app.store.get("operations", "deliverable-process:" + run["id"])
        assert decode(history["data"])["previous_snapshot"] == snapshot
        assert (Path(run["workspace"]) / "analysis.md").exists()
    finally:
        await app.close()
