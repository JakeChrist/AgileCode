"""Ordinary ticket commits and correction resets."""
from pathlib import Path

import pytest

from agilecode.adapters.git import GitRepository


@pytest.mark.asyncio
async def test_step_commits_are_preserved_without_squashing(repository):
    git = GitRepository(repository)
    branch = await git.branch(repository)
    base = await git.head(repository)

    (repository / "requirements.md").write_text("REQ-1: Expected behavior\n")
    requirements = await git.commit_step(repository, branch, "Define Requirements", attempt_id="req")
    (repository / "architecture.md").write_text("Initial architecture\n")
    old_architecture = await git.commit_step(repository, branch, "Define Technical Changes", attempt_id="arch1")
    (repository / "plan.md").write_text("Initial plan\n")
    await git.commit_step(repository, branch, "Plan Implementation", attempt_id="plan1")

    await git.reset_to_commit(repository, branch, requirements)
    assert await git.head(repository) == requirements
    assert not (repository / "architecture.md").exists()
    assert not (repository / "plan.md").exists()

    (repository / "architecture.md").write_text("Corrected architecture\n")
    corrected = await git.commit_step(repository, branch, "Define Technical Changes", attempt_id="arch2")
    assert corrected != old_architecture
    (repository / "plan.md").write_text("Corrected plan\n")
    planned = await git.commit_step(repository, branch, "Plan Implementation", attempt_id="plan2")
    final = await git.final_commit(
        repository, branch, planned, await git.head_tree(repository),
        "Commit Work", attempt_id="final",
    )
    history = (await git.run("rev-list", "--reverse", f"{base}..{final}")).text.splitlines()
    assert history == [requirements, corrected, planned, final]
    assert (await git.run("merge-base", "--is-ancestor", old_architecture, final,
                          check=False)).code != 0


@pytest.mark.asyncio
async def test_git_reset_handles_file_directory_transition(repository):
    git = GitRepository(repository)
    branch = await git.branch(repository)
    (repository / "engine").write_text("old file\n")
    baseline = await git.commit_step(repository, branch, "Old architecture")

    (repository / "engine").unlink()
    (repository / "engine").mkdir()
    (repository / "engine" / "main.py").write_text("print('new')\n")
    await git.commit_step(repository, branch, "New architecture")

    await git.reset_to_commit(repository, branch, baseline)
    assert (repository / "engine").is_file()
    assert (repository / "engine").read_text() == "old file\n"


@pytest.mark.asyncio
async def test_reset_to_previous_step_after_diverging_corrections(repository):
    git = GitRepository(repository)
    branch = await git.branch(repository)

    (repository / "requirements.md").write_text("Requirements\n")
    requirements = await git.commit_step(repository, branch, "Requirements", attempt_id="req")
    (repository / "implementation.md").write_text("First version\n")
    first_implementation = await git.commit_step(repository, branch, "Implement", attempt_id="impl1")
    (repository / "review.md").write_text("First review\n")
    await git.commit_step(repository, branch, "Review", attempt_id="review1")

    # A correction discards the first implementation from the active history.
    await git.reset_to_commit(repository, branch, requirements)
    (repository / "implementation.md").write_text("Revised version\n")
    await git.commit_step(repository, branch, "Implement", attempt_id="impl2")
    (repository / "review.md").write_text("Revised review\n")
    await git.commit_step(repository, branch, "Review", attempt_id="review2")
    assert (await git.run("merge-base", "--is-ancestor", first_implementation, "HEAD",
                          cwd=repository, check=False)).code != 0

    # Deleting the old review must restore the previous implementation commit,
    # even though that commit is not an ancestor of the current branch.
    (repository / "uncommitted.txt").write_text("Current step work\n")
    assert await git.reset_to_commit(repository, branch, first_implementation) == first_implementation
    assert await git.head(repository) == first_implementation
    assert (await git.run("rev-parse", branch)).text == first_implementation
    assert (repository / "implementation.md").read_text() == "First version\n"
    assert not (repository / "review.md").exists()
    assert not (repository / "uncommitted.txt").exists()
    assert not await git.dirty(repository)


@pytest.mark.asyncio
async def test_delete_review_attempt_after_correction_restores_previous_step(repository, tmp_path):
    import asyncio
    import json

    from agilecode.core.application import Application
    from conftest import ScriptClient

    class HoldFinalCommit(ScriptClient):
        def __init__(self):
            super().__init__(review_correction=True)
            self.commit_started = asyncio.Event()
            self.allow_commit = asyncio.Event()

        async def stream(self, profile, messages, tools, on_delta):
            if "CURRENT STEP: Commit Work (commit)" in messages[0]["content"]:
                self.commit_started.set()
                await self.allow_commit.wait()
            return await super().stream(profile, messages, tools, on_delta)

    client = HoldFinalCommit()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        ticket = await app.create_ticket("Rewind after correction", "Make answer return two.")
        await app.move_ticket(ticket["id"], "Ready")
        run = await app.start_ticket(ticket["id"])
        await asyncio.wait_for(client.commit_started.wait(), 30)
        await app.pause_run(ticket["id"])

        attempts = await app.store.query(
            "SELECT * FROM attempts WHERE run_id=? AND phase='development' ORDER BY rowid",
            (run["id"],),
        )
        rejected_review = next(
            row for row in attempts if row["step_id"] == "review" and row["result"]
            and json.loads(row["result"])["status"] == "CHANGES_REQUIRED"
        )
        saved = json.loads(rejected_review["context"])
        previous_commit = saved["start_oid"]
        worktree = Path(run["workspace"])
        assert (await app.git.run("merge-base", "--is-ancestor", previous_commit, "HEAD",
                                  cwd=worktree, check=False)).code != 0

        result = await app.delete_attempt(ticket["id"], rejected_review["id"])
        assert result["deleted"] > 1
        assert await app.git.head(worktree) == previous_commit
        assert (await app.git.run("rev-parse", run["branch"])).text == previous_commit
        assert not await app.git.dirty(worktree)
        restored = await app.selected_run(ticket["id"])
        assert restored["state"] == "paused"
        assert json.loads(restored["outputs"]) == saved["output_attempts"]
        assert not await app.store.one(
            "SELECT id FROM attempts WHERE id=?", (rejected_review["id"],)
        )

        # Execution can advance again from the restored preceding step.
        client.allow_commit.set()
        await app.resume_run(ticket["id"])
        completed = await asyncio.wait_for(app.wait_for_run(run["id"]), 30)
        assert completed["state"] == "completed"
        assert (await app.git.run("merge-base", "--is-ancestor", previous_commit, "HEAD",
                                  cwd=worktree, check=False)).code == 0
    finally:
        client.allow_commit.set()
        await app.close()
