"""Shared process/workspace bindings and actual durable local operation recovery."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agilecode.adapters.git import GitRepository
from agilecode.adapters.store import ProjectStore, decode, encode
from agilecode.adapters.workspace import Workspace, digest
from agilecode.application import process_operations
from agilecode.application.process_operations import recover_process_operation
from agilecode.application.services.agent_workspace import AgentWorkspaceOperations, recover_workspace_operation
from agilecode.core.tools import (DeletePathTool, EditFileTool, MovePathTool, RecordCheckTool,
                                  ToolManager, UpdateProgressTool, WriteFileTool)
from agilecode.core.types import AgileCodeError, Conflict, Events, NeedsReview, Settings, StepResult, now


@pytest.fixture
async def operation_store(tmp_path):
    store = ProjectStore(tmp_path / "operations.db")
    await store.open()
    timestamp = now()
    await store.insert("tickets", {"id": "ticket", "key": "T-1", "title": "Test", "description": "Test",
                                  "state": "In Progress", "team_ref": "test", "process_id": "test",
                                  "created": timestamp, "updated": timestamp})
    await store.insert("runs", {"id": "run", "ticket_id": "ticket", "snapshot": "{}", "state": "running",
                               "workspace": str(tmp_path), "branch": "test", "base_branch": "main",
                               "base_oid": "base", "created": timestamp, "updated": timestamp})
    await store.insert("attempts", {"id": "attempt", "run_id": "run", "step_id": "verify", "number": 1,
                                   "session_id": "session", "member": "{}", "model": "{}", "status": "running",
                                   "context": "{}", "created": timestamp})
    await store.insert("requests", {"id": "request", "session_id": "session", "model": "test",
                                   "input": "{}", "status": "completed", "created": timestamp})
    yield store
    await store.close()


def context():
    return SimpleNamespace(ticket={"id": "ticket"}, attempt={"id": "attempt", "session_id": "session"},
                           process=SimpleNamespace(steps=[]),
                           outputs={"requirements": StepResult(status="PASS", summary="Requirements", output="REQ-1: works")})


def manager(store, tools):
    return ToolManager(tools, [tool.name for tool in tools], store=store, events=Events(),
                       settings=lambda: Settings(autonomy="full"), session_id="session", ticket_id="ticket")


def call(name, args, identity="call"):
    return {"id": identity, "function": {"name": name, "arguments": encode(args)}}


async def source(store, *, session="session", name="run_command", status="completed", result=None):
    await store.insert("tools", {"id": "source", "session_id": session, "request_id": "request",
                                 "call_id": "source-call", "name": name, "args": "{}", "status": status,
                                 "category": "read", "created": now(), "result": encode(result or {
                                     "status": "ok", "data": {"status": "completed", "exit_code": 0}})})


@pytest.mark.asyncio
async def test_progress_executor_calls_shared_operation_and_replays_one_receipt(operation_store, monkeypatch):
    store = operation_store
    observed = []
    original = process_operations.update_progress

    async def tracked(*args):
        observed.append(args[-1])
        return await original(*args)

    monkeypatch.setattr(process_operations, "update_progress", tracked)
    tool = UpdateProgressTool(store, "ticket", "attempt")
    result = await manager(store, [tool]).execute(call("update_progress", {"completed": ["One"]}), "request")
    assert result.payload["status"] == "ok"
    execution_id = result.payload["_execution_id"]
    assert observed == [execution_id]
    assert (await store.get("tools", execution_id))["category"] == "mutation"
    replay = await original(store, "ticket", "attempt", {"completed": ["One"]}, execution_id)
    assert replay == result.payload["data"]
    assert len(await store.query("SELECT * FROM activity WHERE kind='ProgressUpdated'")) == 1
    with pytest.raises(Conflict, match="different process input"):
        await original(store, "ticket", "attempt", {"completed": ["Different"]}, execution_id)
    execution = await store.get("tools", execution_id)
    assert await recover_process_operation(store, execution) == result.payload


@pytest.mark.asyncio
async def test_record_check_executor_uses_shared_real_backend_once(operation_store, repository, monkeypatch):
    store = operation_store
    await source(store)
    git = GitRepository(repository)
    workspace = Workspace(repository, repository, repository.parent / "global")
    ctx = context()
    observed = []
    original = process_operations.record_check

    async def tracked(*args):
        observed.append(args[-1])
        return await original(*args)

    monkeypatch.setattr(process_operations, "record_check", tracked)
    tool = RecordCheckTool(ctx, git, workspace, store)
    args = {"requirement": "REQ-1", "status": "PASS", "method": "Observed test", "source_execution_id": "source"}
    result = await manager(store, [tool]).execute(call("record_check", args), "request")
    assert result.payload["status"] == "ok"
    identity = result.payload["_execution_id"]
    assert observed == [identity]
    assert result.payload["data"]["fingerprint"] == await git.snapshot_tree(repository)
    assert (await store.get("tools", identity))["category"] == "mutation"
    # A replay is a receipt lookup even if mutable artifact/source facts changed.
    ctx.outputs.clear()
    await store.update("tools", "source", status="unknown")
    assert await original(ctx, git, workspace, store, args, identity) == result.payload["data"]
    assert len(await store.query("SELECT * FROM evidence")) == 1
    assert await recover_process_operation(store, await store.get("tools", identity)) == result.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("source_options,args_patch,error", [
    ({"session": "other"}, {}, "verification session"),
    ({"status": "running"}, {}, "completed tool"),
    ({"result": {"status": "error"}}, {}, "successful inspection"),
    ({"name": "write_file"}, {}, "successful inspection"),
    ({"result": {"status": "ok", "data": {"status": "running", "exit_code": 0}}}, {}, "completed command"),
    ({"result": {"status": "ok", "data": {"status": "completed", "exit_code": 1}}}, {}, "exit code zero"),
    ({}, {"source_execution_id": None}, "real source execution"),
    ({}, {"requirement": "REQ-9"}, "recorded requirements"),
])
async def test_record_check_preserves_evidence_gate(operation_store, tmp_path, source_options, args_patch, error):
    store = operation_store
    await source(store, **source_options)

    async def snapshot(_):
        pytest.fail("Invalid evidence must fail before Git inspection")

    args = {"requirement": "REQ-1", "status": "PASS", "method": "Test", "source_execution_id": "source", **args_patch}
    with pytest.raises(AgileCodeError, match=error):
        await process_operations.record_check(context(), SimpleNamespace(snapshot_tree=snapshot),
                                              SimpleNamespace(root=tmp_path), store, args, "check")
    assert not await store.query("SELECT * FROM evidence")


@pytest.mark.asyncio
async def test_nonpassing_check_can_omit_source_and_commit_revalidates_source(operation_store, tmp_path):
    store = operation_store

    async def snapshot(_):
        return "tree"

    args = {"requirement": "REQ-1", "status": "NOT_RUN", "method": "Not run"}
    result = await process_operations.record_check(context(), SimpleNamespace(snapshot_tree=snapshot),
                                                 SimpleNamespace(root=tmp_path), store, args, "not-run")
    assert result["source_execution_id"] is None
    await source(store)

    async def stale_snapshot(_):
        await store.update("tools", "source", status="unknown")
        return "tree"

    args.update(status="PASS", source_execution_id="source")
    with pytest.raises(AgileCodeError, match="completed tool"):
        await process_operations.record_check(context(), SimpleNamespace(snapshot_tree=stale_snapshot),
                                              SimpleNamespace(root=tmp_path), store, args, "stale")
    assert len(await store.query("SELECT * FROM evidence")) == 1


@pytest.mark.asyncio
async def test_cancelled_progress_commits_receipt_before_cancellation_returns(operation_store, monkeypatch):
    store = operation_store
    entered, release = threading.Event(), threading.Event()
    original = store.transaction

    async def blocked(callback):
        def transaction(c):
            result = callback(c)
            entered.set()
            assert release.wait(5)
            return result
        return await original(transaction)

    monkeypatch.setattr(store, "transaction", blocked)
    task = asyncio.create_task(process_operations.update_progress(store, "ticket", "attempt", {}, "progress"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    monkeypatch.setattr(store, "transaction", original)
    execution = {"id": "progress", "name": "update_progress", "session_id": "session", "args": "{}"}
    assert (await recover_process_operation(store, execution))["status"] == "ok"
    assert len(await store.query("SELECT * FROM activity")) == 1


@pytest.mark.asyncio
async def test_workspace_adapters_bind_one_real_service_and_preserve_checks(operation_store, tmp_path, monkeypatch):
    store = operation_store
    root = tmp_path / "work"; root.mkdir()
    workspace = Workspace(root, root, tmp_path / "global")
    lock = asyncio.Lock()
    called = []
    original = AgentWorkspaceOperations.execute

    async def tracked(self, operation, args, execution_id=None):
        called.append((operation, execution_id))
        return await original(self, operation, args, execution_id)

    monkeypatch.setattr(AgentWorkspaceOperations, "execute", tracked)
    tools = [cls(workspace, lock, store) for cls in [WriteFileTool, EditFileTool, MovePathTool, DeletePathTool]]
    executor = manager(store, tools)
    written = await executor.execute(call("write_file", {"path": "one", "content": "old", "create": True}, "write"), "request")
    edited = await executor.execute(call("edit_file", {"path": "one", "expected_hash": digest(b"old"),
                                      "replacements": [{"old": "old", "new": "new"}]}, "edit"), "request")
    moved = await executor.execute(call("move_path", {"path": "one", "destination": "two", "expected_hash": digest(b"new")}, "move"), "request")
    deleted = await executor.execute(call("delete_path", {"path": "two", "expected_hash": digest(b"new")}, "delete"), "request")
    for payload in [written.payload, edited.payload, moved.payload, deleted.payload]:
        assert payload["status"] == "ok"
        execution = await store.get("tools", payload["_execution_id"])
        assert await recover_workspace_operation(store, execution) == payload
    assert [x[0] for x in called] == ["write_file", "edit_file", "move_path", "delete_path"]
    assert list(root.iterdir()) == []
    # The file is now gone. Replaying its already committed write cannot recreate it.
    assert await tools[0].execute({"path": "one", "content": "old", "create": True}, written.payload["_execution_id"]) == written.payload["data"]
    assert not (root / "one").exists()
    with pytest.raises(Conflict):
        await tools[0].execute({"path": "one", "content": "different", "create": True}, written.payload["_execution_id"])
    # Same low-level service is available to manual lifecycle operations.
    manual = AgentWorkspaceOperations(workspace, lock)
    await manual.write(path="manual", content="v1", create=True)
    with pytest.raises(Conflict):
        await manual.write(path="manual", content="v2", expected_hash=digest(b"wrong"))
    assert (root / "manual").read_text() == "v1"


@pytest.mark.asyncio
async def test_cancelled_workspace_worker_retains_lock_and_durable_result(operation_store, tmp_path, monkeypatch):
    root = tmp_path / "work"; root.mkdir()
    workspace = Workspace(root, root, tmp_path / "global")
    lock = asyncio.Lock()
    service = AgentWorkspaceOperations(workspace, lock, operation_store)
    entered, release = threading.Event(), threading.Event()
    original = workspace.write

    def slow_write(**args):
        entered.set()
        assert release.wait(5)
        return original(**args)

    monkeypatch.setattr(workspace, "write", slow_write)
    args = {"path": "one", "content": "value", "create": True}
    task = asyncio.create_task(service.write(execution_id="write", **args))
    acquired = asyncio.Event()

    async def next_writer():
        async with lock:
            acquired.set()

    waiter = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        waiter = asyncio.create_task(next_writer())
        await asyncio.sleep(0.01)
        assert lock.locked() and not acquired.is_set() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await waiter
    finally:
        release.set()
        await asyncio.gather(task, *([waiter] if waiter else []), return_exceptions=True)
    assert (root / "one").read_text() == "value"
    execution = {"id": "write", "name": "write_file", "args": encode(args)}
    recovered = await recover_workspace_operation(operation_store, execution)
    assert recovered["status"] == "ok"
    assert recovered["data"]["hash"] == digest(b"value")
    assert await service.write(execution_id="write", **args) == recovered["data"]


@pytest.mark.asyncio
async def test_unsettled_workspace_receipt_never_replays_mutation(operation_store, tmp_path):
    workspace = Workspace(tmp_path, tmp_path, tmp_path / "global")
    args = {"path": "new", "content": "value", "create": True, "expected_hash": None}
    await operation_store.insert("operations", {"id": "uncertain", "kind": "write_file", "owner_id": str(tmp_path),
        "data": encode({"workspace": str(tmp_path), "args": args}), "status": "started", "created": now(), "updated": now()})
    execution = {"id": "uncertain", "name": "write_file", "args": encode(args)}
    assert await recover_workspace_operation(operation_store, execution) is None
    with pytest.raises(NeedsReview, match="unresolved effects"):
        await AgentWorkspaceOperations(workspace, asyncio.Lock(), operation_store).write(execution_id="uncertain", **args)
    assert not (tmp_path / "new").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["update_progress", "record_check", "write_file"])
async def test_restart_recovers_effect_before_transcript_without_reexecution(operation_store, repository, name):
    store = operation_store
    git = GitRepository(repository)
    workspace = Workspace(repository, repository, repository.parent / "global")
    ctx = context()
    args = {"update_progress": {"notes": "Saved"},
            "record_check": {"requirement": "REQ-1", "status": "PASS", "method": "Observed", "source_execution_id": "source"},
            "write_file": {"path": "created.txt", "content": "saved", "create": True}}[name]
    if name == "record_check":
        await source(store)
    provider_call = call(name, args, "provider-call")
    await store.message("session", {"role": "assistant", "content": None, "tool_calls": [provider_call]}, "request")
    await store.insert("tools", {"id": "lost-result", "session_id": "session", "request_id": "request",
        "call_id": "provider-call", "name": name, "args": encode(args), "status": "running", "category": "mutation", "created": now()})

    def binding(db):
        if name == "update_progress":
            return UpdateProgressTool(db, "ticket", "attempt")
        if name == "record_check":
            return RecordCheckTool(ctx, git, workspace, db)
        return WriteFileTool(workspace, asyncio.Lock(), db)

    expected = await binding(store).execute(args, "lost-result")
    assert not [m for m in await store.messages("session") if m["body"]["role"] == "tool"]
    await store.close()
    restarted = ProjectStore(store.path)
    await restarted.open()
    try:
        recovered_binding = binding(restarted)

        async def forbidden(*args):
            pytest.fail("A committed local receipt must not reexecute its operation")

        recovered_binding.execute = forbidden
        executor = manager(restarted, [recovered_binding])
        first = await executor.execute(provider_call, "request")
        assert first.payload == {"status": "ok", "_execution_id": "lost-result", "data": expected}
        assert (await executor.execute(provider_call, "request")).payload == first.payload
        results = [m for m in await restarted.messages("session") if m["body"]["role"] == "tool"]
        assert len(results) == 1
        assert decode(results[0]["body"]["content"]) == first.payload
        if name == "update_progress":
            assert len(await restarted.query("SELECT * FROM activity")) == 1
        elif name == "record_check":
            assert len(await restarted.query("SELECT * FROM evidence")) == 1
        else:
            assert (repository / "created.txt").read_text() == "saved"
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_cancelled_real_executor_recovers_actual_workspace_outcome(operation_store, tmp_path, monkeypatch):
    store = operation_store
    root = tmp_path / "work"; root.mkdir()
    workspace = Workspace(root, root, tmp_path / "global")
    lock = asyncio.Lock()
    entered, release = threading.Event(), threading.Event()
    original = workspace.write

    def delayed_write(**args):
        entered.set()
        assert release.wait(5)
        return original(**args)

    monkeypatch.setattr(workspace, "write", delayed_write)
    executor = manager(store, [WriteFileTool(workspace, lock, store)])
    provider_call = call("write_file", {"path": "created", "content": "done", "create": True})
    await store.message("session", {"role": "assistant", "content": None, "tool_calls": [provider_call]}, "request")
    task = asyncio.create_task(executor.execute(provider_call, "request"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert lock.locked() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    execution = await store.one("SELECT * FROM tools WHERE name='write_file'")
    assert decode(execution["result"])["status"] == "ok"
    assert execution["status"] == "completed"
    assert (root / "created").read_text() == "done"
    assert (await executor.execute(provider_call, "request")).payload == decode(execution["result"])
    assert len([m for m in await store.messages("session") if m["body"]["role"] == "tool"]) == 1


@pytest.mark.asyncio
async def test_manual_save_and_model_write_reach_same_real_workspace_operation(repository, tmp_path, monkeypatch):
    from agilecode.core.application import Application
    from test_workspace_restore_recovery import BlockingClient

    observed = []
    original = AgentWorkspaceOperations.write

    async def tracked(self, **args):
        observed.append((self.workspace.root, args.get("execution_id")))
        return await original(self, **args)

    monkeypatch.setattr(AgentWorkspaceOperations, "write", tracked)
    client = BlockingClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        ticket = await app.create_ticket("Edit safely", "Share write/check mechanics")
        await app.move_ticket(ticket["id"], "In Progress")
        await asyncio.wait_for(client.started.wait(), 5)
        await app.pause_run(ticket["id"])
        run = await app.selected_run(ticket["id"])
        source_read = await app.read_source(ticket["id"], "app.py")
        saved = await app.save_source(ticket["id"], "app.py", "manual = True\n", source_read["hash"])
        assert observed == [(Path(run["workspace"]).resolve(), None)]
        assert saved["hash"] == digest(b"manual = True\n")
        current = await app.store.get("runs", run["id"])
        assert current["state"] == "paused" and "rerun verification/review" in current["correction"]
        runtime = app.runtimes[run["id"]]
        tool = WriteFileTool(runtime.workspace, runtime.write_lock, app.store)
        await tool.execute({"path": "app.py", "content": "model = True\n", "expected_hash": saved["hash"]}, "model-write")
        assert observed[-1] == (runtime.workspace.root, "model-write")
        assert (runtime.workspace.root / "app.py").read_text() == "model = True\n"
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_same_process_execution_race_commits_one_local_effect(operation_store, tmp_path):
    store = operation_store
    progress = await asyncio.gather(*[
        process_operations.update_progress(store, "ticket", "attempt", {"notes": "Once"}, "progress-race")
        for _ in range(2)
    ])
    assert progress[0] == progress[1]
    assert len(await store.query("SELECT * FROM activity")) == 1
    both_started = asyncio.Event()
    readers = 0

    async def snapshot(_):
        nonlocal readers
        readers += 1
        if readers == 2:
            both_started.set()
        await both_started.wait()
        return "tree"

    args = {"requirement": "REQ-1", "status": "NOT_RUN", "method": "Not run"}
    checks = await asyncio.wait_for(asyncio.gather(*[
        process_operations.record_check(context(), SimpleNamespace(snapshot_tree=snapshot),
                                        SimpleNamespace(root=tmp_path), store, args, "check-race")
        for _ in range(2)
    ]), 5)
    assert checks[0] == checks[1]
    assert len(await store.query("SELECT * FROM evidence")) == 1
