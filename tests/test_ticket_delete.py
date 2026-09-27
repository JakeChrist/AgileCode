import asyncio
import subprocess
from pathlib import Path

import pytest

from agilecode.core.application import Application
from agilecode.core.types import AgileCodeError, Conflict, Settings
from conftest import ScriptClient, git


def ref_exists(repository: Path, ref: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--verify", ref],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


class BlockingClient(ScriptClient):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def stream(self, profile, messages, tools, on_delta):
        self.started.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_delete_running_ticket_discards_dirty_worktree_branch_refs_artifacts_and_history(repository, tmp_path):
    client = BlockingClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        ticket = await app.create_ticket("Mistaken ticket", "This work should be erased.")
        await app.move_ticket(ticket["id"], "Ready")
        run = await app.start_ticket(ticket["id"])
        await asyncio.wait_for(client.started.wait(), 5)

        worktree = Path(run["workspace"])
        branch = run["branch"]
        assert worktree.exists()
        assert ref_exists(repository, f"refs/heads/{branch}")

        # Explicit deletion must discard source state that normal cleanup preserves.
        (worktree / "app.py").write_text("def answer():\n    return 999\n")
        (worktree / "scratch.txt").write_text("uncommitted\n")
        checkpoint = await app.checkpoint(ticket["id"], "Before deletion")
        assert ref_exists(repository, checkpoint["ref"])
        attempts_before = await app.store.query("SELECT * FROM attempts WHERE run_id=?", (run["id"],))
        assert attempts_before
        sessions = [attempt["session_id"] for attempt in attempts_before]
        assert await app.store.query(
            "SELECT * FROM messages WHERE session_id=?",
            (sessions[0],),
        )
        assert await app.store.query(
            "SELECT * FROM requests WHERE session_id=?",
            (sessions[0],),
        )

        artifact_root = repository / ".agilecode" / "artifacts" / ticket["id"]
        artifact_root.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_root / "manual-test.log"
        artifact_path.write_text("ticket-owned artifact\n")
        await app.store.insert("artifacts", {
            "id": "delete-test-artifact",
            "ticket_id": ticket["id"],
            "name": "Deletion test",
            "path": str(artifact_path),
            "size": artifact_path.stat().st_size,
            "hash": None,
            "complete": 1,
            "created": ticket["created"],
        })

        result = await app.delete_ticket(ticket["id"])
        assert result == {"ticket": ticket["id"], "key": ticket["key"], "deleted": True}

        assert not worktree.exists()
        assert not artifact_root.exists()
        assert not ref_exists(repository, f"refs/heads/{branch}")
        assert not git(repository, "for-each-ref", "--format=%(refname)", f"refs/agilecode/checkpoints/{ticket['id']}/")
        assert not await app.store.query("SELECT * FROM runs WHERE ticket_id=?", (ticket["id"],))
        assert not await app.store.query("SELECT * FROM attempts WHERE run_id=?", (run["id"],))
        assert not await app.store.query("SELECT * FROM messages WHERE session_id=?", (sessions[0],))
        assert not await app.store.query("SELECT * FROM requests WHERE session_id=?", (sessions[0],))
        assert not await app.store.query("SELECT * FROM checkpoints WHERE ticket_id=?", (ticket["id"],))
        assert not await app.store.query("SELECT * FROM artifacts WHERE ticket_id=?", (ticket["id"],))
        assert not await app.store.query("SELECT * FROM activity WHERE ticket_id=?", (ticket["id"],))
        with pytest.raises(AgileCodeError):
            await app.find_ticket(ticket["id"])
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_delete_completed_unintegrated_ticket_removes_integration_candidate(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        await app.config.save(Settings(autonomy="full"))
        ticket = await app.create_ticket("Discard completed work", "Complete it, then erase it before merge.")
        await app.move_ticket(ticket["id"], "Ready")
        run = await app.start_ticket(ticket["id"])
        finished = await asyncio.wait_for(app.wait_for_run(run["id"]), 30)
        assert finished["state"] == "completed"

        integration_task = app.integration_tasks.get(ticket["id"])
        if integration_task:
            await asyncio.wait_for(integration_task, 30)
        integrations = await app.store.query(
            "SELECT * FROM integrations WHERE ticket_id=? ORDER BY created",
            (ticket["id"],),
        )
        assert integrations
        candidate = integrations[-1]
        assert candidate["state"] == "awaiting_review"
        assert Path(candidate["workspace"]).exists()
        assert ref_exists(repository, f"refs/agilecode/integrations/{candidate['id']}")
        assert await app.store.one(
            "SELECT * FROM operations WHERE owner_id=?",
            (run["id"],),
        )

        await app.delete_ticket(ticket["id"])

        assert not Path(run["workspace"]).exists()
        assert not Path(candidate["workspace"]).exists()
        assert not ref_exists(repository, f"refs/heads/{run['branch']}")
        assert not ref_exists(repository, f"refs/agilecode/integrations/{candidate['id']}")
        assert not await app.store.query("SELECT * FROM integrations WHERE ticket_id=?", (ticket["id"],))
        assert not await app.store.query("SELECT * FROM operations WHERE owner_id=?", (run["id"],))
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_delete_refuses_integrated_ticket(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        await app.config.save(Settings(autonomy="full"))
        ticket = await app.create_ticket("Integrated work", "This one reaches the target branch.")
        await app.move_ticket(ticket["id"], "Ready")
        run = await app.start_ticket(ticket["id"])
        await asyncio.wait_for(app.wait_for_run(run["id"]), 30)
        integration_task = app.integration_tasks.get(ticket["id"])
        if integration_task:
            await asyncio.wait_for(integration_task, 30)
        candidate = (await app.store.query(
            "SELECT * FROM integrations WHERE ticket_id=? ORDER BY created",
            (ticket["id"],),
        ))[-1]
        await app.approve_integration(candidate["id"], candidate["candidate_oid"], candidate["target_oid"])

        with pytest.raises(AgileCodeError):
            await app.delete_ticket(ticket["id"])

        assert (await app.find_ticket(ticket["id"]))["state"] == "Done"
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_delete_never_mutates_another_ticket_to_make_deletion_possible(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        doomed = await app.create_ticket("Doomed", "Candidate for deletion.")
        survivor = await app.create_ticket("Survivor", "Must remain unchanged.")
        await app.add_dependency(survivor["id"], doomed["id"])

        with pytest.raises(Conflict):
            await app.delete_ticket(doomed["id"])

        assert (await app.find_ticket(doomed["id"]))["title"] == "Doomed"
        assert (await app.find_ticket(survivor["id"]))["title"] == "Survivor"
        dependency = await app.store.one(
            "SELECT * FROM dependencies WHERE ticket_id=? AND depends_on=?",
            (survivor["id"], doomed["id"]),
        )
        assert dependency is not None
    finally:
        await app.close()
