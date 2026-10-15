"""A stopped turn settles admitted effects without launching subsequent work."""
import asyncio
import threading
from pathlib import Path

import pytest

from agilecode.adapters.store import decode
from agilecode.adapters.workspace import Workspace
from agilecode.application.cancellation import (CancellationToken, OperationInterrupted,
    bind_cancellation, cancellation_checkpoint)
from agilecode.application.coordination import ProjectCoordinator, owned_operation
from agilecode.application.services.agent_workspace import AgentWorkspaceOperations
from agilecode.core.application import Application
from agilecode.core.types import Settings
from conftest import ScriptClient


def in_turn(token, coroutine):
    with bind_cancellation(token):
        return asyncio.create_task(coroutine)


@pytest.mark.asyncio
async def test_owned_success_is_a_known_result_after_repeated_cancel():
    entered, release = asyncio.Event(), asyncio.Event()
    class Service:
        coordinator = ProjectCoordinator()
        @owned_operation
        async def effect(self):
            entered.set()
            await release.wait()
            return {'effect':'completed'}
    token = CancellationToken()
    operation = in_turn(token, Service().effect())
    await entered.wait()
    token.cancel()
    operation.cancel()
    await asyncio.sleep(0)
    operation.cancel()
    await asyncio.sleep(0)
    assert not operation.done()
    release.set()
    assert await operation == {'effect':'completed'}
    assert not Service.coordinator.owned_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['reserve','activity','spawn','own'])
async def test_coordinator_rejects_stopped_turn_at_admission(boundary):
    coordinator, token = ProjectCoordinator(), CancellationToken()
    token.cancel()
    entered = False
    async def work():
        nonlocal entered
        entered = True
    with bind_cancellation(token), pytest.raises(OperationInterrupted):
        if boundary == 'reserve':
            async with coordinator.reserve('ticket','request','start'):
                pass
        elif boundary == 'activity':
            async with coordinator.activity('ticket','request','execution'):
                pass
        elif boundary == 'spawn':
            await coordinator.spawn('ticket','request','execution',work())
        else:
            await coordinator.own(work())
    assert not entered
    assert not coordinator.busy()


@pytest.mark.asyncio
async def test_admitted_ticket_is_independent_from_launching_turn():
    coordinator, token = ProjectCoordinator(), CancellationToken()
    release, started = asyncio.Event(), asyncio.Event()
    async def work():
        started.set()
        await release.wait()
        cancellation_checkpoint()
        return 'finished'
    with bind_cancellation(token):
        task = await coordinator.spawn('ticket','request','execution',work())
    await started.wait()
    token.cancel()
    release.set()
    assert await task == 'finished'


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['start','move'])
async def test_interrupted_provisioning_never_launches_or_replays_after_open(repository,tmp_path,monkeypatch,operation):
    app = Application(repository,tmp_path/'global',client=ScriptClient())
    await app.open(initialize=True)
    entered, release, launched = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = app.git.add_worktree
    async def provision(*args,**kwargs):
        result = await original(*args,**kwargs)
        entered.set()
        await release.wait()
        return result
    async def runtime(_):
        launched.set()
        await asyncio.Future()
    monkeypatch.setattr(app.git,'add_worktree',provision)
    monkeypatch.setattr(app.execution_ops,'_run',runtime)
    ticket = await app.create_ticket('Stop before launch','Preserve the admitted worktree.')
    await app.move_ticket(ticket['id'],'Ready')
    token = CancellationToken()
    call = app.start_ticket(ticket['id'],request_id='stopped-start') if operation == 'start' else app.move_ticket(ticket['id'],'In Progress',request_id='stopped-move')
    task = in_turn(token,call)
    try:
        await asyncio.wait_for(entered.wait(),5)
        token.cancel()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert app.coordinator.reservations.get(ticket['id'])
        release.set()
        if operation == 'start':
            with pytest.raises(OperationInterrupted):
                await task
        else:
            assert (await task)['movement']['execution'] == 'interrupted'
        run = await app.selected_run(ticket['id'])
        assert run['state'] == 'paused'
        assert Path(run['workspace']).exists()
        assert not launched.is_set()
        assert run['id'] not in app.tasks
        provision_op = await app.store.get('operations','provision:'+run['id'])
        assert provision_op['status'] == 'completed'
        assert (await app.find_ticket(ticket['id']))['state'] == 'In Progress'
    finally:
        release.set()
        await app.close()
    for _ in range(2):
        reopened = Application(repository,tmp_path/'global',client=ScriptClient())
        await reopened.open()
        try:
            assert (await reopened.selected_run(ticket['id']))['state'] == 'paused'
            assert not reopened.tasks
            assert (await reopened.find_ticket(ticket['id']))['state'] == 'In Progress'
        finally:
            await reopened.close()


@pytest.mark.asyncio
async def test_resume_stops_after_eligibility_before_runtime_dispatch(repository,tmp_path,monkeypatch):
    app = Application(repository,tmp_path/'global',client=ScriptClient())
    await app.open(initialize=True)
    started, release, entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    launches = []
    async def runtime(run):
        launches.append(run.run_id)
        started.set()
        await asyncio.Future()
    monkeypatch.setattr(app.execution_ops,'_run',runtime)
    try:
        ticket = await app.create_ticket('Resume boundary','No launch after stop.')
        await app.move_ticket(ticket['id'],'Ready')
        run = await app.start_ticket(ticket['id'])
        await started.wait()
        await app.pause_run(ticket['id'])
        original = app.execution_ops.require_eligible
        async def eligible(*args,**kwargs):
            value = await original(*args,**kwargs)
            entered.set()
            await release.wait()
            return value
        monkeypatch.setattr(app.execution_ops,'require_eligible',eligible)
        token = CancellationToken()
        task = in_turn(token,app.resume_run(ticket['id'],request_id='stop-resume'))
        await entered.wait()
        token.cancel()
        task.cancel()
        release.set()
        with pytest.raises(OperationInterrupted):
            await task
        assert launches == [run['id']]
        assert (await app.selected_run(ticket['id']))['state'] == 'paused'
        assert (await app.store.get('operations','stop-resume'))['status'] == 'interrupted'
    finally:
        release.set()
        await app.close()


@pytest.mark.asyncio
async def test_started_workspace_writer_settles_and_returns_its_receipt(tmp_path,monkeypatch):
    root = tmp_path/'work'; root.mkdir()
    (root/'file').write_text('before')
    workspace = Workspace(root,root,tmp_path/'global')
    expected = workspace.read('file')['hash']
    entered, release = threading.Event(), threading.Event()
    original = workspace.write
    def write(**args):
        entered.set()
        assert release.wait(5)
        return original(**args)
    monkeypatch.setattr(workspace,'write',write)
    token = CancellationToken()
    service = AgentWorkspaceOperations(workspace,asyncio.Lock())
    task = in_turn(token,service.write(path='file',content='after',expected_hash=expected))
    try:
        assert await asyncio.to_thread(entered.wait,5)
        token.cancel()
        task.cancel()
        await asyncio.sleep(0)
        assert service.write_lock.locked()
        assert not task.done()
        release.set()
        result = await task
        assert result['hash'] == workspace.read('file')['hash']
        assert (root/'file').read_text() == 'after'
        assert not service.write_lock.locked()
    finally:
        release.set()


async def integration_fixture(repository,tmp_path):
    app = Application(repository,tmp_path/'global',client=ScriptClient())
    await app.open(initialize=True)
    await app.config.save(Settings(autonomy='full',merge_on_completion=False))
    ticket = await app.create_ticket('Integration boundary','Return two and verify it.')
    await app.move_ticket(ticket['id'],'Ready')
    run = await app.start_ticket(ticket['id'])
    await app.wait_for_run(run['id'])
    await app.integration_tasks[ticket['id']]
    record = await app.store.one('SELECT * FROM integrations WHERE run_id=?',(run['id'],))
    assert record['state'] == 'awaiting_review'
    return app,ticket,run,record


@pytest.mark.asyncio
@pytest.mark.parametrize('stage',['validation','merge'])
async def test_integration_final_dispatch_gate_and_started_git_settlement(repository,tmp_path,monkeypatch,stage):
    app,ticket,run,record = await integration_fixture(repository,tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    merges, cleanup = [], []
    original_run, original_snapshot = app.git.run, app.git.snapshot_tree
    async def git_run(*args,**kwargs):
        if args[:2] == ('merge','--ff-only'):
            merges.append(args)
            if stage == 'merge':
                entered.set()
                await release.wait()
        return await original_run(*args,**kwargs)
    async def snapshot(path,*args,**kwargs):
        result = await original_snapshot(path,*args,**kwargs)
        if stage == 'validation' and Path(path) == Path(record['workspace']):
            entered.set()
            await release.wait()
        return result
    original_cleanup = app.merges.cleanup
    async def tracked_cleanup(*args,**kwargs):
        cleanup.append(args)
        return await original_cleanup(*args,**kwargs)
    monkeypatch.setattr(app.git,'run',git_run)
    monkeypatch.setattr(app.git,'snapshot_tree',snapshot)
    monkeypatch.setattr(app.merges,'cleanup',tracked_cleanup)
    token = CancellationToken()
    task = in_turn(token,app.approve_integration(record['id'],record['candidate_oid'],record['target_oid']))
    try:
        await asyncio.wait_for(entered.wait(),5)
        token.cancel()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        if stage == 'validation':
            with pytest.raises(OperationInterrupted):
                await task
            assert not merges
            assert await app.git.head() == record['target_oid']
            assert (await app.store.get('integrations',record['id']))['state'] == 'awaiting_review'
        else:
            assert (await task)['state'] == 'integrated'
            assert len(merges) == 1
            assert await app.git.head() == record['candidate_oid']
            assert (await app.store.get('operations','integrate:'+record['id']))['status'] == 'completed'
            assert not cleanup
            assert Path(record['workspace']).exists()
            await app.merges.recover()
            assert (await app.find_ticket(ticket['id']))['state'] == 'Done'
    finally:
        release.set()
        await app.close()


@pytest.mark.asyncio
async def test_partial_delete_stops_later_destruction_and_is_not_replayed(repository,tmp_path,monkeypatch):
    app = Application(repository,tmp_path/'global',client=ScriptClient())
    await app.open(initialize=True)
    started, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def runtime(_):
        started.set()
        await asyncio.Future()
    monkeypatch.setattr(app.execution_ops,'_run',runtime)
    ticket = await app.create_ticket('Retain stopped deletion','Do not erase the remaining branch or history.')
    await app.move_ticket(ticket['id'],'Ready')
    run = await app.start_ticket(ticket['id'])
    await started.wait()
    await app.pause_run(ticket['id'])
    original = app.git.delete_owned_worktree
    async def remove(*args,**kwargs):
        result = await original(*args,**kwargs)
        entered.set()
        await release.wait()
        return result
    monkeypatch.setattr(app.git,'delete_owned_worktree',remove)
    token = CancellationToken()
    receipt_id = 'delete:' + ticket['id']
    task = in_turn(token,app.delete_ticket(ticket['id']))
    try:
        await asyncio.wait_for(entered.wait(),5)
        token.cancel()
        task.cancel()
        release.set()
        with pytest.raises(OperationInterrupted):
            await task
        assert not Path(run['workspace']).exists()
        assert (await app.git.run('rev-parse','--verify','refs/heads/'+run['branch'],check=False)).code == 0
        assert await app.find_ticket(ticket['id'])
        receipt = await app.store.get('operations',receipt_id)
        assert receipt['status'] == 'interrupted'
        assert decode(receipt['result'])['resources_may_have_changed']
    finally:
        release.set()
        await app.close()
    for _ in range(2):
        reopened = Application(repository,tmp_path/'global',client=ScriptClient())
        await reopened.open()
        try:
            assert await reopened.find_ticket(ticket['id'])
            assert ticket['id'] in reopened.coordinator.blocked_tickets
            assert (await reopened.git.run('rev-parse','--verify','refs/heads/'+run['branch'],check=False)).code == 0
            assert (await reopened.store.get('operations',receipt_id))['status'] == 'interrupted'
            if _ == 1:
                assert (await reopened.delete_ticket(ticket['id']))['deleted']
                assert ticket['id'] not in reopened.coordinator.blocked_tickets
        finally:
            await reopened.close()


@pytest.mark.asyncio
async def test_checkpoint_restore_correction_at_exact_physical_boundary_preserves_bytes(repository,tmp_path,monkeypatch):
    from test_workspace_restore_recovery import restore_fixture
    app,ticket,run,checkpoint,preview = await restore_fixture(repository,tmp_path)
    work = Path(run['workspace'])
    before = {path.name:path.read_bytes() for path in [work/'app.py',work/'second.txt']}
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.git.tree_entries
    async def tree_entries(tree):
        result = await original(tree)
        if tree == preview['target_tree']:
            entered.set()
            await release.wait()
        return result
    monkeypatch.setattr(app.git,'tree_entries',tree_entries)
    token = CancellationToken()
    task = in_turn(token,app.restore_checkpoint(ticket['id'],checkpoint['id'],preview['expected_tree']))
    try:
        await asyncio.wait_for(entered.wait(),5)
        token.cancel()
        task.cancel()
        release.set()
        with pytest.raises(OperationInterrupted):
            await task
        assert {name:(work/name).read_bytes() for name in before} == before
        receipt = await app.store.one("SELECT * FROM operations WHERE kind='checkpoint_restore'")
        assert receipt['status'] == 'cancelled'
        assert decode(receipt['result'])['restored'] is False
        assert ticket['id'] not in app.coordinator.blocked_tickets
    finally:
        release.set()
        await app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage',['before_restore','after_restore'])
async def test_rewind_stops_before_restore_or_settles_started_tree_without_optional_cleanup(repository,tmp_path,monkeypatch,stage):
    from test_workspace_restore_recovery import restore_fixture
    app,ticket,run,checkpoint,preview = await restore_fixture(repository,tmp_path)
    attempt = await app.store.one('SELECT * FROM attempts WHERE run_id=? ORDER BY rowid LIMIT 1',(run['id'],))
    work = Path(run['workspace'])
    before = (work/'app.py').read_bytes()
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.checkpoints.restore_tree
    async def restore(*args,**kwargs):
        if stage == 'before_restore':
            entered.set()
            await release.wait()
        result = await original(*args,**kwargs)
        if stage == 'after_restore':
            entered.set()
            await release.wait()
        return result
    monkeypatch.setattr(app.checkpoints,'restore_tree',restore)
    token = CancellationToken()
    task = in_turn(token,app.delete_attempt(ticket['id'],attempt['id'],request_id='stopped-rewind'))
    try:
        await asyncio.wait_for(entered.wait(),5)
        token.cancel()
        task.cancel()
        release.set()
        if stage == 'before_restore':
            with pytest.raises(OperationInterrupted):
                await task
            assert (work/'app.py').read_bytes() == before
            assert await app.store.get('attempts',attempt['id'])
            assert (await app.store.get('operations','stopped-rewind'))['status'] == 'interrupted'
        else:
            result = await task
            assert result['deleted'] >= 1
            assert result['cleanup_warnings']
            assert not await app.store.one('SELECT * FROM attempts WHERE id=?',(attempt['id'],))
            assert (await app.store.get('operations','stopped-rewind'))['status'] == 'completed'
            assert (await app.git.run('rev-parse','--verify',checkpoint['ref'],check=False)).code == 0
        await app.execution_ops.recover_pending_operations()
        assert (await app.store.get('operations','stopped-rewind'))['status'] == ('interrupted' if stage == 'before_restore' else 'completed')
    finally:
        release.set()
        await app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',['own','spawn'])
async def test_cancelled_admission_wait_closes_unentered_coroutine(boundary):
    coordinator, token = ProjectCoordinator(), CancellationToken()
    await coordinator.lock.acquire()
    async def work():
        pytest.fail('Unadmitted coroutine ran')
    coroutine = work()
    call = coordinator.own(coroutine) if boundary == 'own' else coordinator.spawn('ticket','owner','execution',coroutine)
    task = in_turn(token,call)
    await asyncio.sleep(0)
    token.cancel()
    task.cancel()
    try:
        with pytest.raises(OperationInterrupted):
            await task
        assert coroutine.cr_frame is None
        assert not coordinator.busy()
    finally:
        coordinator.lock.release()


@pytest.mark.asyncio
async def test_workspace_stopped_before_dispatch_recovers_known_nonmutation(tmp_path,monkeypatch):
    from agilecode.adapters.store import ProjectStore, encode, now
    from agilecode.application.services.agent_workspace import recover_workspace_operation
    store = ProjectStore(tmp_path/'project.db')
    await store.open()
    try:
        args = {'path':'file','content':'never written','expected_hash':None,'create':True}
        await store.insert('operations',dict(id='stopped-write',kind='write_file',owner_id='work',
            data=encode({'workspace':'work','args':args}),status='cancelled',
            result=encode({'effect':'not_applied'}),created=now(),updated=now()))
        recovered = await recover_workspace_operation(store,{'id':'stopped-write','name':'write_file','args':encode(args)})
        assert recovered['status'] == 'error'
        assert recovered['error']['code'] == 'interrupted'
        assert recovered['outcome_known']
        assert recovered['data']['effect'] == 'not_applied'
    finally:
        await store.close()
