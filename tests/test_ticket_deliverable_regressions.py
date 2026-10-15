"""Independent regression contracts for committed cross-ticket deliverables."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agilecode.core.application import Application
from agilecode.core.types import AgileCodeError, Settings
from conftest import ScriptClient, git


class HoldConsumerClient(ScriptClient):
    """Run the producer normally and inspect consumer admission before it acts."""

    def __init__(self):
        super().__init__()
        self.consumer_started = asyncio.Event()
        self.consumer_requests = []

    async def stream(self, profile, messages, tools, on_delta):
        if any("__hold_consumer__" in str(message.get("content", "")) for message in messages):
            self.consumer_requests.append([dict(message) for message in messages])
            self.consumer_started.set()
            await asyncio.Event().wait()
        return await super().stream(profile, messages, tools, on_delta)


async def open_app(repository, tmp_path):
    client = HoldConsumerClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    await app.config.save(Settings(autonomy="full"))
    return app, client


async def consumer(app, client):
    ticket = await app.create_ticket("Consume predecessor deliverables", "__hold_consumer__")
    await app.move_ticket(ticket["id"], "Ready")
    run = await app.start_ticket(ticket["id"])
    await asyncio.wait_for(client.consumer_started.wait(), 5)
    await app.pause_run(ticket["id"])
    client.consumer_started.clear()
    return ticket, run


async def producer(app, *, integrate=True):
    ticket = await app.create_ticket("Produce prerequisite", "Change answer to two with verified evidence.")
    await app.move_ticket(ticket["id"], "Ready")
    run = await app.start_ticket(ticket["id"])
    finished = await asyncio.wait_for(app.wait_for_run(run["id"]), 30)
    assert finished["state"] == "completed", finished
    assert finished["final_oid"]
    if integrate:
        candidate = await app.prepare_integration(ticket["id"])
        assert candidate["state"] == "awaiting_review", candidate
        integrated = await app.approve_integration(
            candidate["id"], candidate["candidate_oid"], candidate["target_oid"]
        )
        assert integrated["state"] == "integrated", integrated
    return ticket, finished


@pytest.mark.asyncio
async def test_manual_done_without_any_output_does_not_satisfy_prerequisite(repository, tmp_path):
    app, client = await open_app(repository, tmp_path)
    try:
        source = await app.create_ticket("Unperformed prerequisite", "Produce a necessary output.")
        await app.move_ticket(source["id"], "Done")
        target = await app.create_ticket("Dependent", "__hold_consumer__")
        await app.add_dependency(target["id"], source["id"])
        await app.move_ticket(target["id"], "Ready")
        with pytest.raises(AgileCodeError):
            await app.start_ticket(target["id"])
        assert not client.consumer_requests
        assert await app.selected_run(target["id"]) is None
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_manual_done_commit_without_integration_does_not_satisfy_prerequisite(repository, tmp_path):
    app, client = await open_app(repository, tmp_path)
    try:
        source, source_run = await producer(app, integrate=False)
        await app.move_ticket(source["id"], "Done")
        target = await app.create_ticket("Dependent", "__hold_consumer__")
        await app.add_dependency(target["id"], source["id"])
        await app.move_ticket(target["id"], "Ready")
        with pytest.raises(AgileCodeError):
            await app.start_ticket(target["id"])
        assert not client.consumer_requests
        assert git(repository, "rev-parse", "HEAD") == source_run["base_oid"]
        assert (repository / "app.py").read_text().endswith("return 1\n")
    finally:
        await app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("keep_local_file", [False, True])
async def test_existing_consumer_workspace_receives_integrated_prerequisite_before_resume(
    repository, tmp_path, keep_local_file
):
    app, client = await open_app(repository, tmp_path)
    try:
        target, target_run = await consumer(app, client)
        work = Path(target_run["workspace"])
        if keep_local_file:
            (work / "consumer-notes.txt").write_text("Preserve my independent work.\n")
        original_requests = len(client.consumer_requests)
        source, source_run = await producer(app)
        assert (work / "app.py").read_text().endswith("return 1\n")
        await app.add_dependency(target["id"], source["id"])
        await app.resume_run(target["id"])
        await asyncio.wait_for(client.consumer_started.wait(), 5)
        assert len(client.consumer_requests) == original_requests + 1
        assert (work / "app.py").read_text().endswith("return 2\n")
        if keep_local_file:
            assert (work / "consumer-notes.txt").read_text() == "Preserve my independent work.\n"
        assert (await app.selected_run(target["id"]))["id"] == target_run["id"]
        context = "\n".join(str(message.get("content", "")) for message in client.consumer_requests[-1])
        assert source["key"] in context
        assert source_run["final_oid"] in context
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_prerequisite_conflict_preserves_consumer_bytes_and_prevents_generation(repository, tmp_path):
    app, client = await open_app(repository, tmp_path)
    try:
        target, target_run = await consumer(app, client)
        work = Path(target_run["workspace"])
        local = "def answer():\n    return 999\n"
        (work / "app.py").write_text(local)
        before_head = git(work, "rev-parse", "HEAD")
        before_index = git(work, "write-tree")
        before_requests = len(client.consumer_requests)
        source, _ = await producer(app)
        await app.add_dependency(target["id"], source["id"])
        with pytest.raises(AgileCodeError):
            await app.resume_run(target["id"])
        assert len(client.consumer_requests) == before_requests
        assert (work / "app.py").read_text() == local
        assert git(work, "rev-parse", "HEAD") == before_head
        assert git(work, "write-tree") == before_index
        assert not git(work, "ls-files", "-u")
        assert (await app.selected_run(target["id"]))["state"] == "paused"
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_dependency_added_to_paused_consumer_is_checked_again_before_resume(repository, tmp_path):
    app, client = await open_app(repository, tmp_path)
    try:
        target, target_run = await consumer(app, client)
        before_requests = len(client.consumer_requests)
        source = await app.create_ticket("Not ready", "Produce the missing prerequisite.")
        await app.add_dependency(target["id"], source["id"])
        with pytest.raises(AgileCodeError):
            await app.resume_run(target["id"])
        assert len(client.consumer_requests) == before_requests
        assert (await app.selected_run(target["id"]))["id"] == target_run["id"]
        assert (await app.selected_run(target["id"]))["state"] == "paused"
    finally:
        await app.close()


class ReportClient(ScriptClient):
    def __init__(self):
        super().__init__()
        self.analysis_started = asyncio.Event()
        self.release_analysis = asyncio.Event()

    async def stream(self, profile, messages, tools, on_delta):
        import json
        from agilecode.adapters.model import Turn
        if "CURRENT STEP: Write report (analysis)" in messages[0]["content"]:
            self.analysis_started.set()
            await self.release_analysis.wait()
            outcomes = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
            assert all(outcome["status"] == "ok" for outcome in outcomes), outcomes
            if not outcomes:
                name, args = "write_file", {"path": "report.md", "content": "# Findings\nEvidence-backed conclusions.\n", "create": True}
            else:
                name, args = "finish_step", {"status": "PASS", "summary": "Report recorded", "output": "The deliverable is report.md."}
            return Turn({"role": "assistant", "content": None, "tool_calls": [{
                "id": "report", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}
            }]}, "fixture-model", None, "tool_calls")
        return await super().stream(profile, messages, tools, on_delta)


async def start_report(app, client, *, automatic, premature=False):
    from agilecode.domain.types import Process, Step, Team, TeamMember
    team = Team(
        id="regression-analysis", name="Regression analysis",
        members=[TeamMember(id="analyst", name="Analyst", system_prompt="Produce the requested report.")],
        processes=[Process(id="report", name="Report", steps=[
            Step(id="report", name="Write report", team_member_id="analyst", kind="analysis", prompt="Write report.md."),
            *([Step(id="premature", name="Premature commit", team_member_id="analyst", kind="analysis", prompt="Inspect the report without committing.")] if premature else []),
            Step(id="commit", name="Commit report", team_member_id="analyst", kind="commit", prompt="Commit the deliverable."),
        ])], default_process_id="report",
    )
    await app.config.save_team(team, "project")
    ticket = await app.create_ticket("Produce analysis report", "Deliver report.md.",
                                    team_ref="project:regression-analysis", process_id="report", merge_auto=automatic)
    await app.move_ticket(ticket["id"], "Ready")
    run = await app.start_ticket(ticket["id"])
    await asyncio.wait_for(client.analysis_started.wait(), 5)
    return ticket, run


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic", [False, True])
async def test_analysis_deliverable_clean_divergent_integration_uses_existing_permission(
    repository, tmp_path, automatic
):
    import json
    client = ReportClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        await app.config.save(Settings(autonomy="full"))
        ticket, run = await start_report(app, client, automatic=automatic)
        (repository / "parallel.txt").write_text("Another completed ticket.\n")
        git(repository, "add", "parallel.txt")
        git(repository, "commit", "-m", "Concurrent independent work")
        target_oid = git(repository, "rev-parse", "HEAD")
        client.release_analysis.set()
        finished = await asyncio.wait_for(app.wait_for_run(run["id"]), 20)
        assert finished["state"] == "completed", finished
        assert finished["final_oid"]
        candidate = await app.prepare_integration(ticket["id"])
        assert candidate["state"] == ("integrated" if automatic else "awaiting_review"), candidate
        report = json.loads(candidate["report"])
        assert report["strategy"] == "merge"
        assert report["validation_attempts"] == []
        assert report.get("validation")
        if not automatic:
            assert git(repository, "rev-parse", "HEAD") == target_oid
            candidate = await app.approve_integration(
                candidate["id"], candidate["candidate_oid"], candidate["target_oid"]
            )
            assert candidate["state"] == "integrated"
        assert (repository / "report.md").read_text() == "# Findings\nEvidence-backed conclusions.\n"
        assert (repository / "parallel.txt").read_text() == "Another completed ticket.\n"
        assert (await app.find_ticket(ticket["id"]))["state"] == "Done"
        attempts = await app.store.query("SELECT step_id FROM attempts WHERE run_id=? ORDER BY rowid", (run["id"],))
        assert [attempt["step_id"] for attempt in attempts] == ["report", "commit"]
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_analysis_conflict_without_validation_retains_candidate_and_blocks(repository, tmp_path):
    client = ReportClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        await app.config.save(Settings(autonomy="full"))
        ticket, run = await start_report(app, client, automatic=True)
        (repository / "report.md").write_text("A conflicting report from other work.\n")
        git(repository, "add", "report.md")
        git(repository, "commit", "-m", "Concurrent conflicting report")
        target_oid = git(repository, "rev-parse", "HEAD")
        client.release_analysis.set()
        finished = await asyncio.wait_for(app.wait_for_run(run["id"]), 20)
        assert finished["state"] == "completed", finished
        candidate = await app.prepare_integration(ticket["id"])
        assert candidate["state"] == "blocked", candidate
        assert "conflict" in candidate["error"].lower()
        assert git(repository, "rev-parse", "HEAD") == target_oid
        assert (repository / "report.md").read_text() == "A conflicting report from other work.\n"
        assert (Path(run["workspace"]) / "report.md").read_text() == "# Findings\nEvidence-backed conclusions.\n"
        assert Path(candidate["workspace"]).exists()
        attempts = await app.store.query("SELECT step_id FROM attempts WHERE run_id=? ORDER BY rowid", (run["id"],))
        assert [attempt["step_id"] for attempt in attempts] == ["report", "commit"]
    finally:
        await app.close()


class EarlyCommitReportClient(ReportClient):
    def __init__(self):
        super().__init__()
        self.early_outcome = None

    async def stream(self, profile, messages, tools, on_delta):
        import json
        from agilecode.adapters.model import Turn
        if "CURRENT STEP: Premature commit (analysis)" in messages[0]["content"]:
            outcomes = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
            if not outcomes:
                name, args = "final_commit", {"message": "Must not commit before the commit stage"}
            else:
                self.early_outcome = outcomes[0]
                name, args = "finish_step", {"status": "PASS", "summary": "Inspection complete", "output": "The report is ready for the configured commit stage."}
            return Turn({"role": "assistant", "content": None, "tool_calls": [{
                "id": "premature", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}
            }]}, "fixture-model", None, "tool_calls")
        return await super().stream(profile, messages, tools, on_delta)


@pytest.mark.asyncio
async def test_analysis_cannot_use_final_commit_before_configured_commit_stage(repository, tmp_path):
    client = EarlyCommitReportClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        await app.config.save(Settings(autonomy="full"))
        _, run = await start_report(app, client, automatic=False, premature=True)
        client.release_analysis.set()
        finished = await asyncio.wait_for(app.wait_for_run(run["id"]), 20)
        assert finished["state"] == "completed", finished
        assert client.early_outcome and client.early_outcome["status"] == "error"
        assert "commit" in str(client.early_outcome).lower()
        assert git(Path(run["workspace"]), "log", "-1", "--format=%s") != "Must not commit before the commit stage"
        assert git(repository, "rev-list", "--count", f"{run['base_oid']}..{finished['final_oid']}") == "1"
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_restoring_pre_sync_checkpoint_cannot_silently_drop_admitted_prerequisites(repository, tmp_path):
    app, client = await open_app(repository, tmp_path)
    try:
        target, run = await consumer(app, client)
        work = Path(run["workspace"])
        source, _ = await producer(app)
        await app.add_dependency(target["id"], source["id"])
        await app.resume_run(target["id"])
        await asyncio.wait_for(client.consumer_started.wait(), 5)
        await app.pause_run(target["id"])
        client.consumer_started.clear()
        before_requests = len(client.consumer_requests)
        checkpoint = await app.store.one(
            "SELECT * FROM checkpoints WHERE run_id=? AND label='Before prerequisite synchronization' ORDER BY created LIMIT 1",
            (run["id"],),
        )
        assert checkpoint and checkpoint["status"] == "available"
        current = await app.selected_run(target["id"])
        try:
            preview = await app.checkpoints.preview(current, checkpoint["id"])
            await app.restore_checkpoint(target["id"], checkpoint["id"], preview["expected_tree"])
        except AgileCodeError:
            # Refusing a cross-baseline restore is safe if the current source is intact.
            assert (work / "app.py").read_text().endswith("return 2\n")
            return
        try:
            await app.resume_run(target["id"])
        except AgileCodeError:
            assert len(client.consumer_requests) == before_requests
            return
        await asyncio.wait_for(client.consumer_started.wait(), 5)
        assert (work / "app.py").read_text().endswith("return 2\n")
    finally:
        await app.close()


class ExtraFileProducerClient(HoldConsumerClient):
    def action(self, messages):
        import json
        name, args = super().action(messages)
        if name == "finish_step" and "(implement)" in messages[0]["content"].split("CURRENT STEP:", 1)[1].split("\n", 1)[0]:
            written = any(
                call["function"]["name"] == "write_file"
                and json.loads(call["function"]["arguments"]).get("path") == "prerequisite-report.md"
                for message in messages for call in message.get("tool_calls", [])
            )
            if not written:
                return "write_file", {"path": "prerequisite-report.md", "content": "The committed prerequisite report.\n", "create": True}
        return name, args


@pytest.mark.asyncio
@pytest.mark.parametrize("local_kind", ["regular", "fifo"])
async def test_sync_never_overwrites_ignored_consumer_file_at_new_prerequisite_path(repository, tmp_path, local_kind):
    client = ExtraFileProducerClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        await app.config.save(Settings(autonomy="full"))
        target, run = await consumer(app, client)
        work = Path(run["workspace"])
        with (work / ".gitignore").open("a") as ignore:
            ignore.write("prerequisite-report.md\n")
        local = "User's ignored local report must survive.\n"
        local_path = work / "prerequisite-report.md"
        if local_kind == "fifo":
            import os
            os.mkfifo(local_path)
        else:
            local_path.write_text(local)
        before_inode = local_path.stat().st_ino
        assert git(work, "check-ignore", "prerequisite-report.md")
        source, _ = await producer(app)
        await app.add_dependency(target["id"], source["id"])
        before_requests = len(client.consumer_requests)
        with pytest.raises(AgileCodeError):
            await app.resume_run(target["id"])
        assert len(client.consumer_requests) == before_requests
        assert local_path.stat().st_ino == before_inode
        if local_kind == "fifo":
            import stat
            assert stat.S_ISFIFO(local_path.stat().st_mode)
        else:
            assert local_path.read_text() == local
    finally:
        await app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_boundary", ["before_branch_update", "after_branch_update"])
async def test_interrupted_sync_blocks_generation_and_reconciles_exact_recorded_tree(
    repository, tmp_path, monkeypatch, failure_boundary
):
    app, client = await open_app(repository, tmp_path)
    try:
        target, run = await consumer(app, client)
        work = Path(run["workspace"])
        (work / "consumer-notes.txt").write_text("Retain consumer work across recovery.\n")
        source, source_run = await producer(app)
        await app.add_dependency(target["id"], source["id"])
        before_requests = len(client.consumer_requests)
        original_git = app.git.run
        injected = False

        async def fail_once(*args, **kwargs):
            nonlocal injected
            branch_update = args[:2] == ("update-ref", "refs/heads/" + run["branch"])
            index_update = args[:2] == ("read-tree", source_run["final_oid"]) and kwargs.get("cwd") == work
            if not injected and ((failure_boundary == "before_branch_update" and branch_update)
                                 or (failure_boundary == "after_branch_update" and index_update)):
                injected = True
                raise AgileCodeError("Injected synchronization interruption")
            return await original_git(*args, **kwargs)

        monkeypatch.setattr(app.git, "run", fail_once)
        with pytest.raises(AgileCodeError, match="Injected"):
            await app.resume_run(target["id"])
        assert injected
        assert len(client.consumer_requests) == before_requests
        assert (work / "consumer-notes.txt").read_text() == "Retain consumer work across recovery.\n"
        facts = await app.ticket_queries.ticket_execution(target["id"])
        assert facts["blocking_reason"]
        with pytest.raises(AgileCodeError):
            await app.resume_run(target["id"])
        monkeypatch.setattr(app.git, "run", original_git)
        await app.close()
        client = HoldConsumerClient()
        app = Application(repository, tmp_path / "global", client=client)
        await app.open()
        facts = await app.ticket_queries.ticket_execution(target["id"])
        assert not facts["blocking_reason"], facts
        assert facts["run"]["state"] == "paused"
        assert not client.consumer_requests
        assert (work / "app.py").read_text().endswith("return 2\n")
        assert (work / "consumer-notes.txt").read_text() == "Retain consumer work across recovery.\n"
        assert git(work, "rev-parse", "HEAD") == facts["run"]["base_oid"] == source_run["final_oid"]
        assert git(work, "write-tree") == git(work, "rev-parse", "HEAD^{tree}")
        await app.resume_run(target["id"])
        await asyncio.wait_for(client.consumer_started.wait(), 5)
        assert len(client.consumer_requests) == 1
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_rewinding_old_attempt_cannot_discard_synchronized_prerequisites(repository, tmp_path):
    app, client = await open_app(repository, tmp_path)
    try:
        target, run = await consumer(app, client)
        work = Path(run["workspace"])
        old_attempt = await app.store.one("SELECT id FROM attempts WHERE run_id=? ORDER BY rowid LIMIT 1", (run["id"],))
        source, _ = await producer(app)
        await app.add_dependency(target["id"], source["id"])
        await app.resume_run(target["id"])
        await asyncio.wait_for(client.consumer_started.wait(), 5)
        await app.pause_run(target["id"])
        with pytest.raises(AgileCodeError):
            await app.delete_attempt(target["id"], old_attempt["id"])
        assert (work / "app.py").read_text().endswith("return 2\n")
        assert await app.store.get("attempts", old_attempt["id"])
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_historical_touched_path_removal_does_not_invent_permanent_artifact_requirement(repository, tmp_path):
    app, client = await open_app(repository, tmp_path)
    try:
        source, source_run = await producer(app)
        git(repository, "rm", "tests/test_app.py")
        git(repository, "commit", "-m", "Retire obsolete verification helper")
        target = await app.create_ticket("Consume current prerequisite", "__hold_consumer__")
        await app.add_dependency(target["id"], source["id"])
        await app.move_ticket(target["id"], "Ready")
        run = await app.start_ticket(target["id"])
        await asyncio.wait_for(client.consumer_started.wait(), 5)
        assert not (Path(run["workspace"]) / "tests/test_app.py").exists()
        context = "\n".join(str(message.get("content", "")) for message in client.consumer_requests[-1])
        assert "tests/test_app.py" in context and source_run["final_oid"] in context
        assert "removed" in context.lower() or "missing" in context.lower()
        assert "self.assertEqual(answer(), 2)" in git(repository, "show", source_run["final_oid"] + ":tests/test_app.py")
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_legacy_process_upgrade_does_not_replace_frozen_member_permissions(repository, tmp_path):
    from agilecode.adapters.store import decode, encode
    app, client = await open_app(repository, tmp_path)
    try:
        ticket = await app.create_ticket(
            "Legacy analyst", "__hold_consumer__", team_ref="builtin:requirements-engineering",
            process_id="requirements-definition",
        )
        await app.move_ticket(ticket["id"], "Ready")
        run = await app.start_ticket(ticket["id"])
        await asyncio.wait_for(client.consumer_started.wait(), 5)
        await app.pause_run(ticket["id"])
        client.consumer_started.clear()
        snapshot = decode(run["snapshot"])
        process = next(item for item in snapshot["team"]["processes"] if item["id"] == snapshot["process_id"])
        process["steps"] = [step for step in process["steps"] if step["kind"] != "commit"]
        member = next(item for item in snapshot["team"]["members"] if item["id"] == process["steps"][0]["team_member_id"])
        saved_permissions = {"read_file", "finish_step", "custom_existing_tool"}
        member["allowed_tools"] = sorted(saved_permissions)
        await app.store.update("runs", run["id"], snapshot=encode(snapshot))
        await app.resume_run(ticket["id"])
        await asyncio.wait_for(client.consumer_started.wait(), 5)
        upgraded = decode((await app.selected_run(ticket["id"]))["snapshot"])
        upgraded_member = next(item for item in upgraded["team"]["members"] if item["id"] == member["id"])
        actual = set(upgraded_member["allowed_tools"])
        assert saved_permissions <= actual
        assert actual - saved_permissions <= {"write_file", "edit_file", "move_path", "delete_path"}
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_sync_preserves_distinct_staged_and_unstaged_consumer_versions(repository, tmp_path):
    app, client = await open_app(repository, tmp_path)
    try:
        target, run = await consumer(app, client)
        work = Path(run["workspace"])
        (work / "consumer-notes.txt").write_text("The separately staged version.\n")
        git(work, "add", "consumer-notes.txt")
        (work / "consumer-notes.txt").write_text("Later unstaged changes.\n")
        before_index = git(work, "write-tree")
        source, _ = await producer(app)
        await app.add_dependency(target["id"], source["id"])
        before_requests = len(client.consumer_requests)
        try:
            await app.resume_run(target["id"])
        except AgileCodeError:
            assert len(client.consumer_requests) == before_requests
        else:
            await asyncio.wait_for(client.consumer_started.wait(), 5)
        assert (work / "consumer-notes.txt").read_text() == "Later unstaged changes.\n"
        assert git(work, "show", ":consumer-notes.txt") == "The separately staged version."
        if not client.consumer_started.is_set():
            assert git(work, "write-tree") == before_index
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_sync_recovery_does_not_erase_staged_only_changes_made_after_interruption(repository, tmp_path, monkeypatch):
    app, client = await open_app(repository, tmp_path)
    try:
        target, run = await consumer(app, client)
        work = Path(run["workspace"])
        working = "The expected consumer working version.\n"
        (work / "consumer-notes.txt").write_text(working)
        source, source_run = await producer(app)
        await app.add_dependency(target["id"], source["id"])
        original_git = app.git.run
        injected = False

        async def fail_index_update(*args, **kwargs):
            nonlocal injected
            if not injected and args[:2] == ("read-tree", source_run["final_oid"]) and kwargs.get("cwd") == work:
                injected = True
                raise AgileCodeError("Injected interruption before index reset")
            return await original_git(*args, **kwargs)

        monkeypatch.setattr(app.git, "run", fail_index_update)
        with pytest.raises(AgileCodeError, match="Injected"):
            await app.resume_run(target["id"])
        monkeypatch.setattr(app.git, "run", original_git)
        # Keep the expected working tree but stage different new user content.
        (work / "consumer-notes.txt").write_text("User's new staged-only version.\n")
        git(work, "add", "consumer-notes.txt")
        (work / "consumer-notes.txt").write_text(working)
        index = git(work, "write-tree")
        await app.close()
        client = HoldConsumerClient()
        app = Application(repository, tmp_path / "global", client=client)
        await app.open()
        assert git(work, "write-tree") == index
        assert git(work, "show", ":consumer-notes.txt") == "User's new staged-only version."
        assert (work / "consumer-notes.txt").read_text() == working
        assert not client.consumer_requests
        facts = await app.ticket_queries.ticket_execution(target["id"])
        assert facts["blocking_reason"]
    finally:
        await app.close()
