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
