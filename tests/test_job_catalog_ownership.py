"""The concrete producers and compatibility imports reach their canonical owners."""
from __future__ import annotations

import asyncio
import inspect
import multiprocessing
import os
from pathlib import Path
import shlex
import signal
from types import SimpleNamespace

import pytest

from agilecode.adapters.shell import ShellService
from agilecode.adapters.store import ProjectStore
from agilecode.adapters.workspace import Workspace
from agilecode.application.services.jobs import JobService
from agilecode.core.types import AccessDenied, Events, now


@pytest.fixture
async def shell_store(tmp_path):
    store = ProjectStore(tmp_path / "db")
    await store.open()
    for identity in ("a", "b"):
        await store.insert("tickets", {"id": identity, "key": identity, "title": identity,
            "description": "", "state": "Backlog", "team_ref": "builtin:standard",
            "process_id": "development", "created": now(), "updated": now()})
    shell = ShellService(Workspace(tmp_path, tmp_path, tmp_path / "global"), store, Events(),
                         "a", None, tmp_path / "artifacts")
    try:
        yield shell, store
    finally:
        await shell.close()
        await store.close()


def test_catalog_compatibility_paths_export_the_same_policy_objects(tmp_path):
    from agilecode.adapters.skills import SkillRegistry
    from agilecode.core.pm_profiles import PMProfile, PMProfileLibrary
    from agilecode.application.services import catalog_resources
    assert SkillRegistry is catalog_resources.SkillRegistry
    assert PMProfile is catalog_resources.PMProfile
    assert PMProfileLibrary is catalog_resources.PMProfileLibrary
    for cls in (SkillRegistry, PMProfile, PMProfileLibrary):
        assert cls.__module__ == "agilecode.application.services.catalog_resources"
    profiles = PMProfileLibrary(tmp_path)
    profiles.install_bundled()
    assert profiles.list() and profiles.get("lead-systems-engineer").skills


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_real_shell_job_writes_all_come_from_shared_job_owner(shell_store, monkeypatch, background):
    shell, store = shell_store
    calls = []
    insert, update = store.insert, store.update
    async def record_insert(table, values):
        if table == "jobs":
            calls.append(("insert", values["status"], inspect.currentframe().f_back.f_globals["__name__"]))
        return await insert(table, values)
    async def record_update(table, identity, **values):
        if table == "jobs":
            calls.append(("update", values.get("status"), inspect.currentframe().f_back.f_globals["__name__"]))
        return await update(table, identity, **values)
    monkeypatch.setattr(store, "insert", record_insert)
    monkeypatch.setattr(store, "update", record_update)
    result = await shell.run("printf 'canonical job output'", None, background=background)
    if background:
        await asyncio.wait_for(shell.background[result["job_id"]][1], 5)
    job = await store.get("jobs", result["job_id"])
    assert job["status"] == "completed" and job["exit_code"] == 0
    assert [item[1] for item in calls] == ["starting", "running", "completed"]
    assert {item[2] for item in calls} == {"agilecode.application.services.jobs"}
    assert (await shell.jobs.output(job["id"]))["output"] == "canonical job output"


@pytest.mark.asyncio
async def test_job_and_artifact_tools_share_output_rules_and_scope(shell_store):
    from agilecode.core.tools import ReadArtifactTool, ReadJobOutputTool
    shell, store = shell_store
    result = await shell.run("printf 'abcdef'", None)
    page = await shell.jobs.output(result["job_id"], 2, 2)
    tool_page = await ReadJobOutputTool(store, "a").execute({"job_id": result["job_id"], "offset": 2, "limit": 2}, "read-job")
    assert tool_page == page
    artifact_page = await ReadArtifactTool(store, "a").execute({"artifact_id": result["artifact_id"], "offset": 2, "limit": 2}, "read-artifact")
    for key, value in artifact_page.items():
        assert page[key] == value
    assert page["output"] == "cd" and page["bytes"] == 2 and page["next_offset"] == 4
    assert page["producer_complete"] and page["remaining"] and page["partial"]
    with pytest.raises(AccessDenied):
        await ReadJobOutputTool(store, "b").execute({"job_id": result["job_id"]}, "foreign-job")
    with pytest.raises(AccessDenied):
        await ReadArtifactTool(store, "b").execute({"artifact_id": result["artifact_id"]}, "foreign-artifact")


@pytest.mark.asyncio
async def test_job_registration_rejects_run_from_another_ticket_before_artifact_creation(shell_store, tmp_path):
    shell, store = shell_store
    await store.insert("runs", {"id": "foreign-run", "ticket_id": "b", "snapshot": "{}", "state": "paused",
        "workspace": str(tmp_path), "branch": "unused", "base_branch": "master", "base_oid": "unused",
        "created": now(), "updated": now()})
    impostor = SimpleNamespace(ticket_id="a", run_id="foreign-run", shell=shell.shell)
    with pytest.raises(AccessDenied, match="another ticket"):
        await shell.jobs.register(impostor, "true", None)
    assert not await store.query("SELECT * FROM jobs")
    assert not await store.query("SELECT * FROM artifacts")


@pytest.mark.asyncio
async def test_record_transition_requires_the_matching_live_shell(shell_store):
    shell, store = shell_store
    result = await shell.run("sleep 60", None, background=True)
    impostor = SimpleNamespace(ticket_id="a", run_id=None)
    with pytest.raises(AccessDenied, match="live execution shell"):
        await shell.jobs.finished(result["job_id"], impostor, "completed", exit_code=0)
    assert (await store.get("jobs", result["job_id"]))["status"] == "running"
    await shell.jobs.stop(result["job_id"], ticket_id="a")


@pytest.mark.asyncio
async def test_unowned_jobs_recovery_preserves_unknown_effects_and_durable_blocker(shell_store, tmp_path):
    shell, store = shell_store
    await store.insert("jobs", {"id": "orphan", "ticket_id": "a", "run_id": None,
        "command": "potentially still running", "cwd": str(tmp_path), "status": "running", "created": now()})
    await store.insert("jobs", {"id": "settled", "ticket_id": "b", "run_id": None,
        "command": "already finished", "cwd": str(tmp_path), "status": "completed", "created": now()})
    jobs = JobService(store)
    first = await jobs.recover_unowned()
    assert set(first) == {"a"} and "may still be running" in first["a"]
    assert (await store.get("jobs", "orphan"))["status"] == "unknown"
    assert (await store.get("jobs", "orphan"))["finished"] is None
    operation = await store.get("operations", "job_recovery:orphan")
    assert operation["status"] == "blocked"
    assert await JobService(store).recover_unowned() == first
    assert await store.get("operations", "job_recovery:orphan") == operation
    assert len(await store.query("SELECT * FROM operations WHERE kind='job_recovery'")) == 1


@pytest.mark.asyncio
async def test_owned_running_job_is_not_mistaken_for_crash_orphan(shell_store):
    shell, store = shell_store
    result = await shell.run("sleep 60", None, background=True)
    assert await shell.jobs.recover_unowned() == {}
    assert (await store.get("jobs", result["job_id"]))["status"] == "running"
    assert not await store.query("SELECT * FROM operations WHERE kind='job_recovery'")
    await shell.jobs.stop(result["job_id"], ticket_id="a")


def _crash_with_background_job(root, connection):
    async def run():
        root_path = Path(root)
        store = ProjectStore(root_path / "orphan.db")
        await store.open()
        await store.insert("tickets", {"id": "a", "key": "a", "title": "Crash fixture",
            "description": "", "state": "Backlog", "team_ref": "builtin:standard",
            "process_id": "development", "created": now(), "updated": now()})
        shell = ShellService(Workspace(root_path, root_path, root_path / "global"), store,
                             Events(), "a", None, root_path / "artifacts")
        command = ("while [ ! -f " + shlex.quote(str(root_path / "release")) + " ]; do sleep 0.01; done; "
                   "printf survived > " + shlex.quote(str(root_path / "survived")) + "; sleep 60")
        result = await shell.run(command, None, background=True)
        proc, _ = shell.background[result["job_id"]]
        connection.send((proc.pid, result["job_id"]))
        os._exit(71)
    asyncio.run(run())


@pytest.mark.asyncio
async def test_abrupt_host_death_keeps_live_detached_job_blocked_twice(tmp_path):
    if not Path("/proc/self/stat").is_file():
        pytest.skip("Controlled crash fixture requires Linux process identity for safe cleanup")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=_crash_with_background_job, args=(str(tmp_path), sender))
    child.start()
    pid = None
    start_identity = None
    try:
        assert await asyncio.to_thread(receiver.poll, 10)
        pid, job_id = receiver.recv()
        stat = Path(f"/proc/{pid}/stat")
        start_identity = stat.read_text().rsplit(")", 1)[1].split()[19]
        await asyncio.to_thread(child.join, 10)
        assert child.exitcode == 71
        for iteration in range(2):
            store = ProjectStore(tmp_path / "orphan.db")
            await store.open()
            try:
                blockers = await JobService(store).recover_unowned()
                assert set(blockers) == {"a"}
                assert (await store.get("jobs", job_id))["status"] == "unknown"
                assert (await store.get("operations", "job_recovery:" + job_id))["status"] == "blocked"
                if iteration == 0:
                    (tmp_path / "release").touch()
                    async def survived():
                        while not (tmp_path / "survived").exists():
                            await asyncio.sleep(.01)
                    await asyncio.wait_for(survived(), 5)
                    assert (tmp_path / "survived").read_text() == "survived"
            finally:
                await store.close()
    finally:
        if child.is_alive():
            child.terminate()
            await asyncio.to_thread(child.join, 5)
        if pid is not None and start_identity is not None:
            stat = Path(f"/proc/{pid}/stat")
            if stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[19] == start_identity:
                # This PID was received from this test's owned child and is still
                # its exact process. Production recovery never performs this kill.
                os.killpg(pid, signal.SIGKILL)
        receiver.close()
        sender.close()
