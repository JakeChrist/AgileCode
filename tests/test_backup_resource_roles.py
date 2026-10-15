"""Real Git/SQLite round trips and deterministic maintenance-admission barriers."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import zipfile

import pytest

from agilecode.adapters.checkpoints import CheckpointService
from agilecode.adapters.config_lock import shared_file_lock
from agilecode.adapters.git import GitRepository
from agilecode.adapters.store import ProjectStore, encode
from agilecode.application.coordination import ProjectCoordinator
from agilecode.application.queries import TicketQueries
from agilecode.application.services.backup import BackupService, export_ticket
from agilecode.core.backup import restore_backup
from agilecode.core.defaults import requirements_engineering_team, standard_team
from agilecode.core.types import AgileCodeError, Conflict, now, uid
from conftest import git


@pytest.fixture
async def backup_host(repository, tmp_path):
    storage, global_root = repository / ".agilecode", tmp_path / "global"
    store, repo = ProjectStore(storage / "agilecode.db"), GitRepository(repository)
    await store.open()
    await store.execute("INSERT INTO meta VALUES('project',?)", (encode({**await repo.identify(), "id": "fixture-project"}),))
    pm_root = storage / "project_manager"
    pm_root.mkdir()
    (pm_root / "state.json").write_text(encode({"status": "off", "goals": ["Keep history"]}))
    global_root.mkdir()
    (global_root / "settings.json").write_text('{"private":"settings-secret"}')
    (global_root / "providers.json").write_text('{"private":"provider-secret"}')
    (global_root / "teams").mkdir()
    (global_root / "teams" / "custom.json").write_text('{"name":"Preserve definition"}')
    coordinator = ProjectCoordinator()
    checkpoints, queries = CheckpointService(repo, store), TicketQueries(store)
    service = BackupService(root=repository, storage=storage, global_root=global_root,
                            pm_root=pm_root, store=store, git=repo, checkpoints=checkpoints,
                            coordinator=coordinator, busy_reason=lambda: None,
                            ticket_execution=queries.ticket_execution)
    host = SimpleNamespace(root=repository, storage=storage, global_root=global_root, store=store,
                           repo=repo, coordinator=coordinator, checkpoints=checkpoints, service=service)
    try:
        yield host
    finally:
        await store.close()


async def add_run(host, *, state="paused", analysis=False, resources=True, selected=True):
    ticket_id, run_id, timestamp = uid(), uid(), now()
    team = requirements_engineering_team() if analysis else standard_team()
    process = team.process(team.default_process_id)
    ticket = {"id": ticket_id, "key": "T-" + ticket_id[:8], "title": "Backup fixture",
              "description": "Preserve work and evidence", "state": "Done" if analysis else "In Progress",
              "team_ref": "builtin:" + team.id, "process_id": process.id,
              "merge_auto": 1, "created": timestamp, "updated": timestamp,
              "selected_run_id": run_id if selected else None}
    await host.store.insert("tickets", ticket)
    run = {"id": run_id, "ticket_id": ticket_id, "snapshot": encode({"team": team.model_dump(mode="json"), "process_id": process.id}),
           "state": state, "step_index": len(process.steps) if analysis else 0,
           "workspace": str(host.storage / "worktrees" / run_id), "branch": "ticket/" + run_id,
           "base_branch": "master", "base_oid": await host.repo.head(), "final_oid": None,
           "created": timestamp, "updated": timestamp}
    await host.store.insert("runs", run)
    if resources:
        await host.repo.add_worktree(Path(run["workspace"]), run["branch"], run["base_oid"])
    if analysis and state == "completed":
        await host.store.insert("operations", {"id": "analysis:" + run_id, "kind": "analysis_completion", "owner_id": ticket_id,
                                               "data": "{}", "status": "completed", "result": "{}",
                                               "created": timestamp, "updated": timestamp})
    return await host.store.get("runs", run_id)


def rewrite_manifest(source, destination, change):
    with zipfile.ZipFile(source) as archive, zipfile.ZipFile(destination, "w") as output:
        for name in archive.namelist():
            data = archive.read(name)
            if name == "manifest.json":
                manifest = json.loads(data)
                change(manifest)
                data = json.dumps(manifest).encode()
            output.writestr(name, data)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_completed_analysis_roundtrip_without_branch_or_worktree(backup_host, tmp_path, legacy):
    host = backup_host
    run = await add_run(host, state="completed", analysis=True)
    await host.repo.run("worktree", "remove", run["workspace"])
    await host.repo.run("branch", "-D", run["branch"])
    archive = tmp_path / "analysis.zip"
    await host.service.create(archive)
    with zipfile.ZipFile(archive) as saved:
        manifest = json.loads(saved.read("manifest.json"))
    assert manifest["resources"][run["id"]]["role"] == "history"
    assert not manifest["restore_checkpoints"]
    if legacy:
        old_archive = tmp_path / "legacy.zip"
        rewrite_manifest(archive, old_archive, lambda m: m.pop("resources"))
        archive = old_archive
    destination = tmp_path / "restored"
    result = await restore_backup(archive, destination)
    assert result["restored_runs"] == []
    with sqlite3.connect(destination / ".agilecode/agilecode.db") as connection:
        loaded = connection.execute("SELECT state,workspace FROM runs WHERE id=?", (run["id"],)).fetchone()
        assert loaded[0] == "completed"
        assert not Path(loaded[1]).exists()
    assert (await GitRepository(destination).run("show-ref", "--verify", "refs/heads/" + run["branch"], check=False)).code
    assert len(await GitRepository(destination).worktrees()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["paused", "cancelled"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_retained_work_reconstructs_verified_checkpoint(backup_host, tmp_path, state, legacy):
    host = backup_host
    run = await add_run(host, state=state)
    work = Path(run["workspace"])
    payload = b"uncommitted\r\n\x00\xff"
    (work / "unfinished.bin").write_bytes(payload)
    archive = tmp_path / "retained.zip"
    result = await host.service.create(archive)
    assert result["recovery_checkpoints"][run["id"]]
    if legacy:
        old_archive = tmp_path / "legacy.zip"
        rewrite_manifest(archive, old_archive, lambda m: m.pop("resources"))
        archive = old_archive
    destination = tmp_path / "restored"
    result = await restore_backup(archive, destination)
    assert result["restored_runs"] == [run["id"]]
    restored_path = destination / ".agilecode/worktrees" / run["id"]
    assert (restored_path / "unfinished.bin").read_bytes() == payload
    assert git(restored_path, "rev-parse", "HEAD") == run["base_oid"]
    await GitRepository(destination).verify_worktree(restored_path, run["branch"])
    assert git(destination, "rev-parse", "--git-common-dir") == ".git"


@pytest.mark.asyncio
async def test_failed_provisioning_and_unselected_history_do_not_grow_worktrees(backup_host, tmp_path):
    host = backup_host
    failed = await add_run(host, state="failed", resources=False)
    historical = await add_run(host, state="cancelled", resources=True, selected=False)
    archive = tmp_path / "unknown.zip"
    result = await host.service.create(archive)
    assert failed["id"] in result["unavailable_resources"]
    destination = tmp_path / "restored"
    result = await restore_backup(archive, destination)
    assert result["restored_runs"] == []
    assert result["unavailable_resources"][0]["run_id"] == failed["id"]
    assert not (destination / ".agilecode/worktrees" / failed["id"]).exists()
    assert not (destination / ".agilecode/worktrees" / historical["id"]).exists()
    with sqlite3.connect(destination / ".agilecode/agilecode.db") as connection:
        state, error = connection.execute("SELECT state,error FROM runs WHERE id=?", (failed["id"],)).fetchone()
        assert state == "failed" and "Restore blocked" in error


@pytest.mark.asyncio
async def test_declared_required_ref_mismatch_fails_before_publication(backup_host, tmp_path):
    run = await add_run(backup_host)
    archive = tmp_path / "good.zip"
    await backup_host.service.create(archive)
    bad = tmp_path / "bad.zip"
    def damage(manifest):
        manifest["resources"][run["id"]]["branch_oid"] = "0" * 40
    rewrite_manifest(archive, bad, damage)
    with pytest.raises(AgileCodeError, match="branch is missing or changed"):
        await restore_backup(bad, tmp_path / "unpublished")
    assert not (tmp_path / "unpublished").exists()
    assert not list(tmp_path.glob(".agilecode-restore-*"))


@pytest.mark.asyncio
async def test_missing_original_ref_remains_stopped_and_is_not_fabricated(backup_host, tmp_path):
    host = backup_host
    run = await add_run(host)
    await host.repo.run("update-ref", "-d", "refs/heads/" + run["branch"])
    archive = tmp_path / "missing.zip"
    await host.service.create(archive)
    destination = tmp_path / "restored"
    result = await restore_backup(archive, destination)
    assert result["unavailable_resources"][0]["run_id"] == run["id"]
    assert result["restored_runs"] == []
    assert not (destination / ".agilecode/worktrees" / run["id"]).exists()


@pytest.mark.asyncio
async def test_snapshot_blocks_start_and_all_shared_global_writers(backup_host, tmp_path, monkeypatch):
    host = backup_host
    entered, release = asyncio.Event(), asyncio.Event()
    original = host.store.backup
    async def blocked_backup(destination):
        entered.set()
        await release.wait()
        await original(destination)
    monkeypatch.setattr(host.store, "backup", blocked_backup)
    task = asyncio.create_task(host.service.create(tmp_path / "guarded.zip"))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        with pytest.raises(Conflict, match="maintenance"):
            async with host.coordinator.reserve("ticket", "start", "start"):
                pytest.fail("new start was admitted")
        for relative in ("settings.json", "providers.json", "teams/.catalog", "skills/.catalog", "project_manager/profiles/.catalog"):
            with pytest.raises(Conflict):
                with shared_file_lock(host.global_root / relative):
                    pytest.fail("global writer was admitted")
    finally:
        release.set()
    await task
    assert host.coordinator.maintenance_owner is None
    with shared_file_lock(host.global_root / "settings.json"):
        pass


@pytest.mark.asyncio
async def test_cancelled_backup_holds_admission_until_worker_settles(backup_host, tmp_path, monkeypatch):
    host = backup_host
    entered, release = asyncio.Event(), asyncio.Event()
    original = host.store.backup
    async def blocked_backup(destination):
        entered.set()
        await release.wait()
        await original(destination)
    monkeypatch.setattr(host.store, "backup", blocked_backup)
    task = asyncio.create_task(host.service.create(tmp_path / "cancelled.zip"))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert host.coordinator.maintenance_owner
    assert not task.done()
    with pytest.raises(Conflict):
        async with host.coordinator.reserve("ticket", "resume", "resume"):
            pass
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert host.coordinator.maintenance_owner is None
    assert (tmp_path / "cancelled.zip").exists()


@pytest.mark.asyncio
async def test_busy_check_runs_inside_maintenance_and_existing_work_is_refused(backup_host, tmp_path):
    host = backup_host
    checked = []
    def busy():
        checked.append(host.coordinator.maintenance_owner)
        return "Existing PM work"
    host.service.busy_reason = busy
    with pytest.raises(AgileCodeError, match="Existing PM"):
        await host.service.create(tmp_path / "busy.zip")
    assert checked and checked[0]
    assert host.coordinator.maintenance_owner is None
    checked.clear()
    async with host.coordinator.activity("discussion", "turn", "discussion"):
        with pytest.raises(Conflict, match="busy"):
            await host.service.create(tmp_path / "active.zip")
    assert checked == []


@pytest.mark.asyncio
async def test_restore_safe_defaults_and_portable_global_scope(backup_host, tmp_path):
    host = backup_host
    run = await add_run(host)
    done = await add_run(host, state="completed", analysis=True, resources=False)
    settings = {"merge_on_completion": True, "mcp_servers": [{"id": "external", "enabled": True}]}
    await host.store.execute("INSERT INTO settings VALUES('project',?)", (encode(settings),))
    await host.store.insert("jobs", {"id": uid(), "ticket_id": run["ticket_id"], "run_id": run["id"],
                                     "command": "historical command", "cwd": run["workspace"], "status": "completed", "created": now()})
    archive = tmp_path / "safe.zip"
    await host.service.create(archive)
    with zipfile.ZipFile(archive) as saved:
        assert "global_teams/custom.json" in saved.namelist()
        assert not any("providers.json" in name or name == "settings.json" for name in saved.namelist())
        assert "settings-secret" not in saved.read("manifest.json").decode()
        assert not any(name.endswith(".lock") for name in saved.namelist())
    existing_global = tmp_path / "other-global"
    existing_global.mkdir()
    (existing_global / "settings.json").write_text("leave unchanged")
    destination = tmp_path / "restored"
    await restore_backup(archive, destination, existing_global)
    assert (existing_global / "settings.json").read_text() == "leave unchanged"
    assert (destination / ".agilecode/recovered_global/global_teams/custom.json").exists()
    assert json.loads((destination / ".agilecode/project_manager/state.json").read_text())["status"] == "paused"
    with sqlite3.connect(destination / ".agilecode/agilecode.db") as connection:
        restored = json.loads(connection.execute("SELECT value FROM settings WHERE key='project'").fetchone()[0])
        assert restored["merge_on_completion"] is False
        assert restored["mcp_servers"][0]["enabled"] is False
        assert connection.execute("SELECT merge_auto FROM tickets WHERE id=?", (run["ticket_id"],)).fetchone()[0] == 0
        assert connection.execute("SELECT merge_auto FROM tickets WHERE id=?", (done["ticket_id"],)).fetchone()[0] == 1
        assert connection.execute("SELECT cwd FROM jobs").fetchone()[0] == run["workspace"]
        assert json.loads(connection.execute("SELECT value FROM meta WHERE key='execution_controls'").fetchone()[0])["all_paused"] is True
    with pytest.raises(AgileCodeError, match="must not exist"):
        await restore_backup(archive, destination)


@pytest.mark.asyncio
async def test_ticket_export_keeps_selected_run_scope_and_refuses_overwrite(tmp_path):
    calls = []
    async def detail(ticket_id, *, complete):
        assert ticket_id == "ticket" and complete is True
        return {"ticket": {"id": ticket_id}, "run": {"id": "selected"},
                "attempts": [{"session_id": "selected-session"}], "jobs": [{"id": "ticket-wide-job"}]}
    async def messages(session_id, *, include_partial):
        calls.append((session_id, include_partial))
        return [{"body": {"content": "Retain complete transcript"}}]
    destination = tmp_path / "export.json"
    await export_ticket("ticket", destination, ticket_detail=detail, messages=messages)
    saved = json.loads(destination.read_text())
    assert saved["version"] == 1 and saved["run"]["id"] == "selected"
    assert calls == [("selected-session", True)]
    before = destination.read_bytes()
    with pytest.raises(AgileCodeError, match="destination exists"):
        await export_ticket("ticket", destination, ticket_detail=detail, messages=messages)
    assert destination.read_bytes() == before


@pytest.mark.asyncio
async def test_archive_publication_does_not_overwrite_a_racing_destination(backup_host, tmp_path, monkeypatch):
    from agilecode.application.services import backup
    original = backup.os.link
    destination = tmp_path / "racing.zip"
    def race(source, target):
        Path(target).write_bytes(b"another writer")
        return original(source, target)
    monkeypatch.setattr(backup.os, "link", race)
    with pytest.raises(AgileCodeError, match="destination already exists"):
        await backup_host.service.create(destination)
    assert destination.read_bytes() == b"another writer"
    assert not list(tmp_path.glob("*.partial"))
    assert backup_host.coordinator.maintenance_owner is None


@pytest.mark.asyncio
async def test_missing_checkpoint_identity_is_rejected_before_publication(backup_host, tmp_path):
    run = await add_run(backup_host)
    archive = tmp_path / "good.zip"
    await backup_host.service.create(archive)
    bad = tmp_path / "bad.zip"
    def damage(manifest):
        manifest["resources"][run["id"]]["checkpoint_id"] = "missing-checkpoint"
    rewrite_manifest(archive, bad, damage)
    with pytest.raises(AgileCodeError, match="checkpoint is missing"):
        await restore_backup(bad, tmp_path / "unpublished")
    assert not (tmp_path / "unpublished").exists()


@pytest.mark.asyncio
async def test_restore_uses_archive_evidence_not_current_original_checkout(backup_host, tmp_path):
    host = backup_host
    run = await add_run(host)
    archive = tmp_path / "portable.zip"
    (Path(run["workspace"]) / "retained.txt").write_text("backup contents")
    await host.service.create(archive)
    await host.repo.run("worktree", "remove", "--force", run["workspace"])
    Path(run["workspace"]).symlink_to(tmp_path / "unrelated-directory")
    destination = tmp_path / "restored"
    await restore_backup(archive, destination)
    assert (destination / ".agilecode/worktrees" / run["id"] / "retained.txt").read_text() == "backup contents"


@pytest.mark.asyncio
async def test_missing_resource_blocker_survives_two_reopens(backup_host, tmp_path):
    from agilecode.application.eligibility import execution_eligibility
    run = await add_run(backup_host, resources=False)
    archive = tmp_path / "unknown.zip"
    await backup_host.service.create(archive)
    destination = tmp_path / "restored"
    await restore_backup(archive, destination)
    for _ in range(2):
        store = ProjectStore(destination / ".agilecode/agilecode.db")
        await store.open()
        try:
            facts = await TicketQueries(store).ticket_execution(run["ticket_id"])
            assert facts["blocking_reason"]
            assert execution_eligibility(facts["ticket"], facts, {"all_paused": False}) == facts["blocking_reason"]
            assert not Path(facts["run"]["workspace"]).exists()
        finally:
            await store.close()


@pytest.mark.asyncio
async def test_portable_scope_preserves_skill_payload_lockfiles(backup_host, tmp_path):
    path = backup_host.global_root / "skills/custom/requirements.lock"
    path.parent.mkdir(parents=True)
    path.write_text("a skill's actual payload")
    archive = tmp_path / "payload.zip"
    await backup_host.service.create(archive)
    with zipfile.ZipFile(archive) as saved:
        assert saved.read("global_skills/custom/requirements.lock") == b"a skill's actual payload"
        assert "global_skills/.catalog.lock" not in saved.namelist()


@pytest.mark.asyncio
async def test_application_completed_analysis_backup_reopens_twice_without_branch(repository, tmp_path):
    from agilecode.core.application import Application
    from agilecode.core.types import Settings
    from conftest import AnalysisClient
    app = Application(repository, tmp_path / "global", client=AnalysisClient())
    await app.open(initialize=True)
    archive = tmp_path / "application.zip"
    try:
        await app.config.save(Settings(autonomy="full", merge_on_completion=True, delete_branch_after_merge=True))
        ticket = await app.create_ticket("Analyze requirements", "Retain the completed analysis history.",
                                         team_ref="builtin:requirements-engineering", process_id="requirements-definition")
        await app.move_ticket(ticket["id"], "Ready")
        run = await app.start_ticket(ticket["id"])
        completed = await asyncio.wait_for(app.wait_for_run(run["id"]), 30)
        assert completed["state"] == "completed" and completed["final_oid"]
        await app.prepare_integration(ticket["id"])
        assert not Path(completed["workspace"]).exists()
        assert (await app.git.run("show-ref", "--verify", "refs/heads/" + run["branch"], check=False)).code
        await app.backup(archive)
    finally:
        await app.close()
    destination = tmp_path / "reopened-analysis"
    await restore_backup(archive, destination, tmp_path / "new-global")
    for _ in range(2):
        reopened = Application(destination, tmp_path / "new-global", client=AnalysisClient())
        await reopened.open()
        try:
            facts = await reopened.ticket_execution(ticket["id"])
            assert facts["classification"] == "integrated" and facts["finished"]
            assert facts["ticket"]["state"] == "Done"
            assert not Path(facts["run"]["workspace"]).exists()
            assert (await reopened.git.run("show-ref", "--verify", "refs/heads/" + run["branch"], check=False)).code
        finally:
            await reopened.close()


@pytest.mark.asyncio
async def test_domain_only_backup_retains_unverified_analysis_worktree(repository, tmp_path):
    from agilecode.core.application import Application
    app = Application(repository, tmp_path / "global", execution=False)
    await app.open(initialize=True)
    archive = tmp_path / "unverified-analysis.zip"
    try:
        host = SimpleNamespace(store=app.store, repo=app.git, storage=app.storage)
        run = await add_run(host, state="completed", analysis=True)
        # Legacy completion state lacks the validated-completion receipt. This
        # domain-only host must not invent successful runtime validation.
        await app.store.execute("DELETE FROM operations WHERE id=?", ("analysis:" + run["id"],))
        changed = Path(run["workspace"]) / "unreviewed-analysis.txt"
        changed.write_text("Preserve these uncommitted results.\n")
        assert (await app.ticket_execution(run["ticket_id"]))["classification"] == "analysis_unverified"
        await app.backup(archive)
        with zipfile.ZipFile(archive) as saved:
            manifest = json.loads(saved.read("manifest.json"))
        assert manifest["resources"][run["id"]]["role"] == "retained"
        assert manifest["resources"][run["id"]]["checkpoint_id"]
    finally:
        await app.close()
    destination = tmp_path / "restored-unverified-analysis"
    result = await restore_backup(archive, destination)
    assert result["restored_runs"] == [run["id"]]
    for _ in range(2):
        reopened = Application(destination, tmp_path / "restored-global", execution=False)
        await reopened.open()
        try:
            facts = await reopened.ticket_execution(run["ticket_id"])
            assert facts["classification"] == "analysis_unverified" and not facts["finished"]
            assert (Path(facts["run"]["workspace"]) / "unreviewed-analysis.txt").read_text() == "Preserve these uncommitted results.\n"
        finally:
            await reopened.close()
