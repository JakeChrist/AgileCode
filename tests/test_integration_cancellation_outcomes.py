"""Preparation cancellation cannot erase already-applied or uncertain Git facts."""
import asyncio
import pytest
from agilecode.core.application import Application
from agilecode.core.types import Settings
from conftest import ScriptClient


async def start_automatic(app):
    await app.config.save(Settings(autonomy='full', merge_on_completion=True))
    ticket = await app.create_ticket('Apply atomically', 'Return two and verify it.')
    await app.move_ticket(ticket['id'], 'Ready')
    run = await app.start_ticket(ticket['id'])
    await app.wait_for_run(run['id'])
    return ticket, run


@pytest.mark.asyncio
async def test_cancelled_prepare_waiter_preserves_integrated_git_outcome(repository, tmp_path, monkeypatch):
    app = Application(repository, tmp_path/'global', client=ScriptClient())
    await app.open(initialize=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.git.run
    async def held_merge(*args, **kwargs):
        if args[:2] == ('merge', '--ff-only'):
            entered.set()
            await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(app.git, 'run', held_merge)
    try:
        ticket, run = await start_automatic(app)
        await asyncio.wait_for(entered.wait(), 10)
        task = app.integration_tasks[ticket['id']]
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        record = await app.store.one('SELECT * FROM integrations WHERE run_id=?', (run['id'],))
        assert record['state'] == 'integrated'
        assert (await app.store.get('runs', run['id']))['state'] == 'integrated'
        assert await app.git.head() == record['candidate_oid']
        await app.merges.recover()
        assert (await app.find_ticket(ticket['id']))['state'] == 'Done'
    finally:
        release.set()
        await app.close()


@pytest.mark.asyncio
async def test_prepare_error_preserves_uncertain_application_for_recovery(repository, tmp_path, monkeypatch):
    app = Application(repository, tmp_path/'global', client=ScriptClient())
    await app.open(initialize=True)
    original = app.merges._mark_integrated
    async def fail_receipt(*args, **kwargs):
        raise RuntimeError('lost receipt after target changed')
    monkeypatch.setattr(app.merges, '_mark_integrated', fail_receipt)
    try:
        ticket, run = await start_automatic(app)
        await app.integration_tasks[ticket['id']]
        record = await app.store.one('SELECT * FROM integrations WHERE run_id=?', (run['id'],))
        assert record['state'] == 'uncertain'
        assert await app.git.head() == record['candidate_oid']
        monkeypatch.setattr(app.merges, '_mark_integrated', original)
        await app.merges.recover()
        await app.merges.recover()
        assert (await app.store.get('integrations', record['id']))['state'] == 'integrated'
        assert (await app.find_ticket(ticket['id']))['state'] == 'Done'
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_close_waits_protected_automatic_apply_and_preserves_recovery(repository, tmp_path, monkeypatch):
    app = Application(repository, tmp_path/'global', client=ScriptClient())
    await app.open(initialize=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.git.run
    async def held_merge(*args, **kwargs):
        if args[:2] == ('merge', '--ff-only'):
            entered.set()
            await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(app.git, 'run', held_merge)
    ticket, run = await start_automatic(app)
    await asyncio.wait_for(entered.wait(), 10)
    closing = asyncio.create_task(app.close())
    try:
        await asyncio.sleep(0)
        assert not closing.done()
        assert not app.store.closed
        release.set()
        await asyncio.wait_for(closing, 10)
    finally:
        release.set()
        await app.close()
    for _ in range(2):
        reopened = Application(repository, tmp_path/'global', client=ScriptClient())
        await reopened.open()
        try:
            record = await reopened.store.one('SELECT * FROM integrations WHERE run_id=?', (run['id'],))
            assert record['state'] == 'integrated'
            assert (await reopened.store.get('runs', run['id']))['state'] == 'integrated'
            assert (await reopened.find_ticket(ticket['id']))['state'] == 'Done'
            assert await reopened.git.head() == record['candidate_oid']
        finally:
            await reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('publication', ['ticket', 'settings'])
async def test_auto_permission_revoked_during_git_validation_prevents_dispatch(repository, tmp_path, monkeypatch, publication):
    from pathlib import Path
    app = Application(repository, tmp_path/'global', client=ScriptClient())
    await app.open(initialize=True)
    release = asyncio.Event()
    try:
        await app.config.save(Settings(autonomy='full', merge_on_completion=False))
        ticket = await app.create_ticket('Recheck permission', 'Await approval after revocation.')
        await app.move_ticket(ticket['id'], 'Ready')
        run = await app.start_ticket(ticket['id'])
        await app.wait_for_run(run['id'])
        await app.integration_tasks[ticket['id']]
        record = await app.store.one('SELECT * FROM integrations WHERE run_id=?', (run['id'],))
        assert record['state'] == 'awaiting_review'
        await app.config.save(Settings(autonomy='full', merge_on_completion=True))
        target = await app.git.head()
        entered = asyncio.Event()
        snapshot = app.git.snapshot_tree
        async def held_validation(work, *args, **kwargs):
            result = await snapshot(work, *args, **kwargs)
            if Path(work) == Path(record['workspace']):
                entered.set()
                await release.wait()
            return result
        monkeypatch.setattr(app.git, 'snapshot_tree', held_validation)
        apply = asyncio.create_task(app.merges.apply(record['id'], record['candidate_oid'], record['target_oid'], automatic=True))
        await asyncio.wait_for(entered.wait(), 5)
        if publication == 'ticket':
            latest = await app.find_ticket(ticket['id'])
            await app.edit_ticket(ticket['id'], expected_revision=latest['revision'], merge_auto=False)
        else:
            await app.config.save(Settings(autonomy='full', merge_on_completion=False))
        release.set()
        result = await asyncio.wait_for(apply, 10)
        assert result['state'] == 'awaiting_review'
        assert await app.git.head() == target
        assert not await app.store.one("SELECT 1 FROM operations WHERE id=?", ('integrate:' + record['id'],))
        assert (await app.find_ticket(ticket['id']))['state'] == 'In Progress'
    finally:
        release.set()
        await app.close()
