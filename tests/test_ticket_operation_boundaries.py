"""Real-store barriers for the shared ticket owner and physical activity lifetime."""
import asyncio
import threading
from pathlib import Path
import pytest
from agilecode.core.application import Application
from agilecode.core.types import AgileCodeError, Conflict, Settings
from agilecode.adapters.store import decode, encode
from agilecode.adapters.workspace import Workspace
from conftest import AnalysisClient, ScriptClient


class BlockingClient(ScriptClient):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
    async def stream(self, *args, **kwargs):
        self.started.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_create_retry_binds_original_before_mutable_defaults(repository,tmp_path):
    app = Application(repository,tmp_path/'global',execution=False)
    await app.open(initialize=True)
    try:
        ticket = await app.create_ticket('  Original  ','Description',request_id='create-1')
        async def unavailable(_):
            raise AgileCodeError('Catalog changed')
        app.config.get_team = unavailable
        assert (await app.create_ticket('  Original  ','Description',request_id='create-1'))['id'] == ticket['id']
        with pytest.raises(Conflict):
            await app.create_ticket('Different','Description',request_id='create-1')
        with pytest.raises(AgileCodeError):
            await app.edit_ticket(ticket['id'],expected_revision=ticket['revision'],title=' \t\n')
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_reciprocal_dependencies_are_one_transaction(repository,tmp_path):
    app = Application(repository,tmp_path/'global',execution=False)
    await app.open(initialize=True)
    try:
        a = await app.create_ticket('A','A'); b = await app.create_ticket('B','B')
        results = await asyncio.gather(app.add_dependency(a['id'],b['id']),app.add_dependency(b['id'],a['id']),return_exceptions=True)
        assert sum(isinstance(r,AgileCodeError) for r in results) == 1
        assert len(await app.store.query('SELECT * FROM dependencies')) == 1
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_move_final_revision_cas_does_not_erase_concurrent_edit(repository,tmp_path):
    app = Application(repository,tmp_path/'global',execution=False)
    await app.open(initialize=True)
    try:
        ticket = await app.create_ticket('Before','Description')
        ticket = await app.move_ticket(ticket['id'],'In Progress',activate=False)
        entered, release = asyncio.Event(), asyncio.Event()
        async def stop(_):
            entered.set(); await release.wait()
        app.execution_ops.stop_ticket = stop
        moving = asyncio.create_task(app.move_ticket(ticket['id'],'Ready',expected_revision=ticket['revision'],request_id='move-race'))
        await entered.wait()
        edited = await app.edit_ticket(ticket['id'],expected_revision=ticket['revision'],title='After')
        release.set()
        with pytest.raises(Conflict):
            await moving
        current = await app.find_ticket(ticket['id'])
        assert (current['state'],current['title'],current['revision']) == ('In Progress','After',edited['revision'])
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_board_commit_survives_dirty_base_execution_block(repository,tmp_path):
    app = Application(repository,tmp_path/'global',client=BlockingClient())
    await app.open(initialize=True)
    try:
        ticket = await app.create_ticket('Dirty base','Description')
        (repository/'app.py').write_text('dirty = True\n')
        result = await app.move_ticket(ticket['id'],'In Progress',request_id='dirty-move')
        assert result['state'] == 'In Progress'
        assert result['movement']['board_committed']
        assert result['movement']['execution'] == 'blocked'
        assert 'uncommitted' in result['movement']['reason']
        again = await app.move_ticket(ticket['id'],'In Progress',request_id='dirty-move')
        assert again['movement'] == result['movement']
        assert not await app.latest_run(ticket['id'])
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_stale_completion_after_away_back_and_harmless_reorder(repository,tmp_path):
    app = Application(repository,tmp_path/'global',client=BlockingClient())
    await app.open(initialize=True)
    try:
        ticket = await app.create_ticket('Retained work','Description')
        ticket = await app.move_ticket(ticket['id'],'In Progress')
        run = await app.selected_run(ticket['id'])
        old = {'run_id':run['id'],'transition_id':run['board_transition_id']}
        reordered = await app.move_ticket(ticket['id'],'In Progress',position=33)
        assert reordered['board_transition_id'] == old['transition_id']
        assert (await app.selected_run(ticket['id']))['id'] == run['id']
        await app.move_ticket(ticket['id'],'Ready')
        await app.move_ticket(ticket['id'],'In Progress')
        result = await app.move_ticket(ticket['id'],'Done',completion_origin=old,request_id='stale-complete',activate=False)
        assert result['state'] == 'In Progress'
        assert result['movement']['execution'] == 'stale_completion'
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_pm_admission_is_atomic_and_manual_has_no_pm_limit(repository,tmp_path):
    app = Application(repository,tmp_path/'global',execution=False)
    await app.open(initialize=True)
    try:
        tickets = [await app.create_ticket(str(n),'Description') for n in range(3)]
        scope = {'scope':'project_manager','admission_limit':1}
        results = await asyncio.gather(*(app.move_ticket(t['id'],'In Progress',caller=scope,activate=False) for t in tickets[:2]),return_exceptions=True)
        assert sum(isinstance(r,AgileCodeError) for r in results) == 1
        assert await app.ticket_queries.active_ticket_count() == 1
        await app.move_ticket(tickets[2]['id'],'In Progress',activate=False)
        assert await app.ticket_queries.active_ticket_count() == 2
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_source_cancellation_retains_reservation_until_thread_settles(repository,tmp_path,monkeypatch):
    client = BlockingClient()
    app = Application(repository,tmp_path/'global',client=client)
    await app.open(initialize=True)
    release = threading.Event()
    try:
        ticket = await app.create_ticket('Edit source','Description')
        await app.move_ticket(ticket['id'],'In Progress')
        await client.started.wait()
        await app.pause_run(ticket['id'])
        source = await app.read_source(ticket['id'],'app.py')
        entered = threading.Event()
        write = Workspace.write
        def blocked_write(self,*args,**kwargs):
            entered.set(); release.wait(10)
            return write(self,*args,**kwargs)
        monkeypatch.setattr(Workspace,'write',blocked_write)
        saving = asyncio.create_task(app.save_source(ticket['id'],'app.py','edited = True\n',source['hash']))
        await asyncio.to_thread(entered.wait,5)
        assert entered.is_set()
        saving.cancel()
        await asyncio.sleep(0)
        with pytest.raises(Conflict):
            await app.resume_run(ticket['id'])
        assert ticket['id'] in app.coordinator.reservations
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await saving
        assert ticket['id'] not in app.coordinator.reservations
        assert (await app.read_source(ticket['id'],'app.py'))['content'] == 'edited = True\n'
    finally:
        release.set()
        await app.close()


@pytest.mark.asyncio
async def test_resume_all_starts_never_started_active_column(repository,tmp_path):
    client = BlockingClient()
    app = Application(repository,tmp_path/'global',client=client)
    await app.open(initialize=True)
    try:
        await app.pause_all()
        ticket = await app.create_ticket('Queued while paused','Description')
        await app.move_ticket(ticket['id'],'In Progress')
        assert await app.selected_run(ticket['id']) is None
        result = await app.resume_all()
        assert result['resumed_tickets'] == 1
        await asyncio.wait_for(client.started.wait(),10)
        assert (await app.selected_run(ticket['id']))['state'] == 'running'
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_analysis_completion_evidence_allows_archive_after_cleanup(repository,tmp_path):
    app = Application(repository,tmp_path/'global',client=AnalysisClient())
    await app.open(initialize=True)
    try:
        await app.config.save(Settings(autonomy='full', merge_on_completion=True))
        ticket = await app.create_ticket('Analyze','Description',team_ref='builtin:requirements-engineering',process_id='requirements-definition')
        await app.move_ticket(ticket['id'],'Ready')
        run = await app.start_ticket(ticket['id'])
        await app.wait_for_run(run['id'])
        await app.prepare_integration(ticket['id'])
        facts = await app.ticket_execution(ticket['id'])
        assert facts['finished'] and facts['classification'] == 'integrated'
        await app.archive_ticket(ticket['id'])
        assert (await app.find_ticket(ticket['id']))['archived']
        assert not Path(run['workspace']).exists()
    finally:
        await app.close()


async def completed_development(app):
    await app.config.save(Settings(autonomy='full',merge_on_completion=False))
    ticket = await app.create_ticket('Change answer','Implement and verify it.')
    await app.move_ticket(ticket['id'],'Ready')
    run = await app.start_ticket(ticket['id'])
    await app.wait_for_run(run['id'])
    task = app.integration_tasks.get(ticket['id'])
    if task:
        await task
    record = await app.store.one('SELECT * FROM integrations WHERE run_id=? ORDER BY created DESC LIMIT 1',(run['id'],))
    assert record['state'] == 'awaiting_review'
    return ticket,run,record


@pytest.mark.asyncio
async def test_manual_integration_and_cleanup_are_owned_before_move(repository,tmp_path,monkeypatch):
    app = Application(repository,tmp_path/'global',client=ScriptClient())
    await app.open(initialize=True)
    try:
        ticket,run,record = await completed_development(app)
        entered,release = asyncio.Event(),asyncio.Event()
        original = app.git.run
        async def hold_target(*args,**kwargs):
            if args[:2] == ('merge','--ff-only'):
                entered.set(); await release.wait()
            return await original(*args,**kwargs)
        monkeypatch.setattr(app.git,'run',hold_target)
        applying = asyncio.create_task(app.approve_integration(record['id'],record['candidate_oid'],record['target_oid']))
        await asyncio.wait_for(entered.wait(),10)
        moving = asyncio.create_task(app.move_ticket(ticket['id'],'Ready'))
        await asyncio.sleep(0.03)
        assert not moving.done()
        assert app.coordinator.activities.get(ticket['id'])
        release.set()
        assert (await applying)['state'] == 'integrated'
        assert (await moving)['state'] == 'Ready'
        assert not app.coordinator.activities.get(ticket['id'])
        assert (await app.selected_run(ticket['id']))['state'] == 'integrated'
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_integrated_outcome_recovers_board_continuation_twice(repository,tmp_path,monkeypatch):
    app = Application(repository,tmp_path/'global',client=ScriptClient())
    await app.open(initialize=True)
    ticket,run,record = await completed_development(app)
    async def crash_after_effect(_):
        raise RuntimeError('simulated loss after Git and cleanup before board continuation')
    monkeypatch.setattr(app.merges,'_complete_board',crash_after_effect)
    with pytest.raises(RuntimeError):
        await app.approve_integration(record['id'],record['candidate_oid'],record['target_oid'])
    assert (await app.store.get('integrations',record['id']))['state'] == 'integrated'
    assert (await app.find_ticket(ticket['id']))['state'] == 'In Progress'
    await app.close()
    revision = None
    for _ in range(2):
        reopened = Application(repository,tmp_path/'global',client=ScriptClient())
        await reopened.open()
        try:
            current = await reopened.find_ticket(ticket['id'])
            assert current['state'] == 'Done'
            if revision is not None:
                assert current['revision'] == revision
            revision = current['revision']
            assert (await reopened.store.get('operations','integration-board:'+record['id']))['status'] == 'completed'
        finally:
            await reopened.close()


@pytest.mark.asyncio
async def test_deletion_recovers_remaining_resources_and_receipt_twice(repository,tmp_path,monkeypatch):
    app = Application(repository,tmp_path/'global',client=BlockingClient())
    await app.open(initialize=True)
    ticket = await app.create_ticket('Delete me','Description')
    await app.move_ticket(ticket['id'],'In Progress')
    await app.pause_run(ticket['id'])
    await app.move_ticket(ticket['id'],'Ready')
    original = app.git.delete_owned_branch
    calls = 0
    async def fail_after_branch(branch):
        nonlocal calls
        await original(branch)
        calls += 1
        if calls == 1:
            raise RuntimeError('simulated crash after branch removal')
    monkeypatch.setattr(app.git,'delete_owned_branch',fail_after_branch)
    with pytest.raises(RuntimeError):
        await app.delete_ticket(ticket['id'],request_id='delete-recover')
    assert (await app.store.get('operations','delete-recover'))['status'] == 'deleting_resources'
    await app.close()
    for _ in range(2):
        reopened = Application(repository,tmp_path/'global',client=ScriptClient())
        await reopened.open()
        try:
            assert not await reopened.store.one('SELECT 1 FROM tickets WHERE id=?',(ticket['id'],))
            result = await reopened.delete_ticket(ticket['id'],request_id='delete-recover')
            assert result['deleted']
        finally:
            await reopened.close()


@pytest.mark.asyncio
async def test_rewind_recovers_file_effect_before_database_commit_twice(repository,tmp_path,monkeypatch):
    client = BlockingClient()
    app = Application(repository,tmp_path/'global',client=client)
    await app.open(initialize=True)
    ticket = await app.create_ticket('Rewind me','Description')
    await app.move_ticket(ticket['id'],'In Progress')
    await client.started.wait()
    await app.pause_run(ticket['id'])
    run = await app.selected_run(ticket['id'])
    attempt = await app.store.one('SELECT * FROM attempts WHERE run_id=? ORDER BY rowid',(run['id'],))
    (Path(run['workspace'])/'app.py').write_text('changed = True\n')
    original = app.checkpoints.restore_tree
    async def crash_after_restore(*args,**kwargs):
        result = await original(*args,**kwargs)
        raise RuntimeError('simulated loss after physical restoration')
    monkeypatch.setattr(app.checkpoints,'restore_tree',crash_after_restore)
    with pytest.raises(RuntimeError):
        await app.delete_attempt(ticket['id'],attempt['id'],request_id='rewind-recover')
    assert (await app.store.get('operations','rewind-recover'))['status'] == 'restoring'
    await app.close()
    for _ in range(2):
        reopened = Application(repository,tmp_path/'global',client=ScriptClient())
        await reopened.open()
        try:
            assert not await reopened.store.one('SELECT 1 FROM attempts WHERE id=?',(attempt['id'],))
            result = await reopened.delete_attempt(ticket['id'],attempt['id'],request_id='rewind-recover')
            assert result['deleted'] == 1
            assert (Path(run['workspace'])/'app.py').read_text() == 'def answer():\n    return 1\n'
        finally:
            await reopened.close()


@pytest.mark.asyncio
async def test_selection_migration_preserves_older_paused_and_blocks_ambiguity(repository,tmp_path):
    from agilecode.application.queries import migrate_run_selection
    app = Application(repository,tmp_path/'global',client=BlockingClient())
    await app.open(initialize=True)
    try:
        ticket = await app.create_ticket('Selection evidence','Description')
        await app.move_ticket(ticket['id'],'In Progress')
        await app.pause_run(ticket['id'])
        run = await app.selected_run(ticket['id'])
        newer = dict(run,id='newer-cancelled',state='cancelled',created='2099-01-01T00:00:00',workspace=str(tmp_path/'missing'),branch='missing')
        await app.store.insert('runs',newer)
        await app.store.execute("DELETE FROM meta WHERE key='selected_run_migration_v1'")
        await app.store.transaction(migrate_run_selection)
        assert (await app.selected_run(ticket['id']))['id'] == run['id']
        # Newer successful history and older unfinished evidence conflict; no
        # query is permitted to guess a current run by timestamp afterward.
        await app.store.update('runs',newer['id'],state='integrated')
        await app.store.execute("DELETE FROM meta WHERE key='selected_run_migration_v1'")
        await app.store.transaction(migrate_run_selection)
        assert await app.selected_run(ticket['id']) is None
        facts = await app.ticket_execution(ticket['id'])
        assert 'ambiguous' in facts['blocking_reason']
        with pytest.raises(Conflict):
            await app.read_source(ticket['id'],'app.py')
    finally:
        await app.close()
