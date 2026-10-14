"""Board movement and analysis completion are independent of Git integration."""
import asyncio
import subprocess
from pathlib import Path

import pytest

from agilecode.core.application import Application
from agilecode.core.types import Settings
from conftest import ScriptClient, git


class BlockingClient(ScriptClient):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def stream(self, profile, messages, tools, on_delta):
        self.started.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "team_ref,process_id",
    [
        ("builtin:requirements-engineering", "requirements-definition"),
        ("builtin:systems-architecture", "architecture-development"),
    ],
)
async def test_read_only_process_finishes_done_without_commit(
    repository, tmp_path, team_ref, process_id
):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        await app.config.save(Settings(autonomy="full"))
        ticket = await app.create_ticket(
            "Analyze the project", "Produce the requested engineering findings.",
            team_ref=team_ref, process_id=process_id,
        )
        await app.move_ticket(ticket["id"], "Ready")
        started = await app.start_ticket(ticket["id"])
        finished = await asyncio.wait_for(app.wait_for_run(started["id"]), 30)

        assert finished["state"] == "completed"
        assert finished["final_oid"] is None
        assert (await app.find_ticket(ticket["id"]))["state"] == "Done"
        assert not Path(started["workspace"]).exists()
        assert subprocess.run(
            ["git", "-C", str(repository), "show-ref", "--verify", "--quiet",
             "refs/heads/" + started["branch"]],
            check=False,
        ).returncode != 0
        assert not await app.store.query(
            "SELECT id FROM integrations WHERE ticket_id=?", (ticket["id"],)
        )
        attempt_rows = await app.store.query(
            "SELECT result FROM attempts WHERE run_id=? AND phase='development'",
            (started["id"],),
        )
        assert attempt_rows
        assert all('"status":"PASS"' in row["result"] for row in attempt_rows)
        details = await app.project_manager._get_ticket({"ticket": ticket["id"]}, "test")
        assert details["final_output"]["output"]
        assert git(repository, "rev-parse", "HEAD") == started["base_oid"]
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


@pytest.mark.asyncio
async def test_recovery_finishes_previously_stranded_analysis_run(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        ticket = await app.create_ticket(
            "Requirements analysis", "Document the needs.",
            team_ref="builtin:requirements-engineering", process_id="requirements-definition",
        )
        await app.move_ticket(ticket["id"], "Ready")
        original = app._complete_read_only_run

        async def simulate_interrupted_finalization(run):
            return True

        app._complete_read_only_run = simulate_interrupted_finalization
        started = await app.start_ticket(ticket["id"])
        finished = await asyncio.wait_for(app.wait_for_run(started["id"]), 30)
        assert finished["state"] == "completed"
        assert (await app.find_ticket(ticket["id"]))["state"] == "In Progress"
        assert Path(started["workspace"]).exists()
        app._complete_read_only_run = original
    finally:
        await app.close()

    reopened = Application(repository, tmp_path / "global", client=ScriptClient())
    await reopened.open()
    try:
        assert (await reopened.find_ticket(ticket["id"]))["state"] == "Done"
        assert not Path(started["workspace"]).exists()
        assert (await reopened.latest_run(ticket["id"]))["state"] == "completed"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_analysis_source_changes_prevent_automatic_done(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        ticket = await app.create_ticket(
            "Investigate", "Read the code.",
            team_ref="builtin:requirements-engineering", process_id="requirements-definition",
        )
        await app.move_ticket(ticket["id"], "Ready")
        original = app._complete_read_only_run

        async def defer(run):
            return True

        app._complete_read_only_run = defer
        started = await app.start_ticket(ticket["id"])
        finished = await asyncio.wait_for(app.wait_for_run(started["id"]), 30)
        app._complete_read_only_run = original
        (Path(started["workspace"]) / "app.py").write_text("def answer():\n    return 999\n")

        assert await app._complete_read_only_run(finished) is True
        assert (await app.find_ticket(ticket["id"]))["state"] == "In Progress"
        assert (await app.latest_run(ticket["id"]))["state"] == "paused"
        assert Path(started["workspace"]).exists()
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_reopened_analysis_keeps_completed_run_after_restart(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        ticket = await app.create_ticket(
            "Requirements", "Analyze project requirements.",
            team_ref="builtin:requirements-engineering", process_id="requirements-definition",
        )
        await app.move_ticket(ticket["id"], "Ready")
        run = await app.start_ticket(ticket["id"])
        await asyncio.wait_for(app.wait_for_run(run["id"]), 30)
        assert (await app.find_ticket(ticket["id"]))["state"] == "Done"
        assert not Path(run["workspace"]).exists()
        await app.move_ticket(ticket["id"], "Backlog")
    finally:
        await app.close()

    reopened = Application(repository, tmp_path / "global", client=ScriptClient())
    await reopened.open()
    try:
        assert (await reopened.find_ticket(ticket["id"]))["state"] == "Backlog"
        assert (await reopened.latest_run(ticket["id"]))["state"] == "completed"
    finally:
        await reopened.close()
