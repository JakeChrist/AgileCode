"""Manual restore journals and shutdown own real filesystem effects."""
import asyncio
import os
from pathlib import Path
import signal
import sys
import threading

import pytest

from agilecode.adapters.store import decode, encode
from agilecode.adapters.workspace import Workspace
from agilecode.application.coordination import CHECKPOINT_RESTORE_BLOCKER
from agilecode.core.application import Application
from agilecode.core.types import AgileCodeError, Conflict, Team
from conftest import ScriptClient


class BlockingClient(ScriptClient):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def stream(self, *args, **kwargs):
        self.started.set()
        await asyncio.Event().wait()


async def restore_fixture(repository, tmp_path):
    client = BlockingClient()
    app = Application(repository, tmp_path / 'global', client=client)
    await app.open(initialize=True)
    ticket = await app.create_ticket('Restore safely', 'Retain interrupted restore evidence.')
    await app.move_ticket(ticket['id'], 'In Progress')
    await client.started.wait()
    await app.pause_run(ticket['id'])
    run = await app.selected_run(ticket['id'])
    work = Path(run['workspace'])
    (work / 'second.txt').write_text('checkpoint second\n')
    checkpoint = await app.checkpoint(ticket['id'], 'Restore target')
    (work / 'app.py').write_text('before first\n')
    (work / 'second.txt').write_text('before second\n')
    snapshot = decode(run['snapshot'])
    process = Team.model_validate(snapshot['team']).process(snapshot['process_id'])
    outputs = {step.id: 'stale-output-' + step.id for step in process.steps}
    await app.store.update('runs', run['id'], outputs=encode(outputs), step_index=len(process.steps)-1)
    preview = await app.preview_checkpoint(ticket['id'], checkpoint['id'])
    return app, ticket, run, checkpoint, preview


async def assert_outputs_invalidated(app, run):
    current = await app.store.get('runs', run['id'])
    snapshot = decode(current['snapshot'])
    process = Team.model_validate(snapshot['team']).process(snapshot['process_id'])
    index = next((i for i, step in enumerate(process.steps) if step.kind == 'implement'), 0)
    assert current['step_index'] == index
    assert not set(decode(current['outputs'])) & {step.id for step in process.steps[index:]}


@pytest.mark.asyncio
async def test_restore_finished_files_recovers_database_without_replay(repository, tmp_path, monkeypatch):
    app, ticket, run, checkpoint, preview = await restore_fixture(repository, tmp_path)
    original = app.checkpoints.restore_tree
    async def fail_after_files(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError('lost after source files reached target')
    monkeypatch.setattr(app.checkpoints, 'restore_tree', fail_after_files)
    try:
        with pytest.raises(RuntimeError, match='lost after'):
            await app.restore_checkpoint(ticket['id'], checkpoint['id'], preview['expected_tree'])
        assert (await app.ticket_execution(ticket['id']))['blocking_reason'] == CHECKPOINT_RESTORE_BLOCKER
        await assert_outputs_invalidated(app, run)
        # No restore_tree call is needed to reconcile exact target evidence.
        await app.workspace_ops.recover_pending_operations()
        await app.workspace_ops.recover_pending_operations()
        assert (await app.ticket_execution(ticket['id']))['blocking_reason'] is None
        operation = await app.store.one("SELECT * FROM operations WHERE kind='checkpoint_restore'")
        assert operation['status'] == 'completed'
        assert (Path(run['workspace']) / 'app.py').read_text().endswith('return 1\n')
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_partial_restore_blocks_resume_but_fresh_preview_can_resolve(repository, tmp_path, monkeypatch):
    app, ticket, run, checkpoint, preview = await restore_fixture(repository, tmp_path)
    original = app.git.run
    count = 0
    async def fail_second_blob(*args, **kwargs):
        nonlocal count
        if args[:2] == ('cat-file', 'blob'):
            count += 1
            if count == 2:
                raise RuntimeError('lost between file replacements')
        return await original(*args, **kwargs)
    monkeypatch.setattr(app.git, 'run', fail_second_blob)
    try:
        with pytest.raises(RuntimeError, match='between file'):
            await app.restore_checkpoint(ticket['id'], checkpoint['id'], preview['expected_tree'])
        monkeypatch.setattr(app.git, 'run', original)
        await app.workspace_ops.recover_pending_operations()
        await app.workspace_ops.recover_pending_operations()
        operation = await app.store.one("SELECT * FROM operations WHERE kind='checkpoint_restore'")
        assert operation['status'] == 'blocked'
        assert (await app.ticket_execution(ticket['id']))['blocking_reason'] == CHECKPOINT_RESTORE_BLOCKER
        await assert_outputs_invalidated(app, run)
        with pytest.raises(Conflict, match='Checkpoint restore'):
            await app.resume_run(ticket['id'])
        with pytest.raises(Conflict):
            await app.backup(tmp_path / 'unsafe.zip')
        fresh = await app.preview_checkpoint(ticket['id'], checkpoint['id'])
        work = Path(run['workspace'])
        (work / 'later-user-edit.txt').write_text('Preserve until explicitly previewed.\n')
        with pytest.raises(Conflict, match='stale'):
            await app.restore_checkpoint(ticket['id'], checkpoint['id'], fresh['expected_tree'])
        assert (work / 'later-user-edit.txt').exists()
        fresh = await app.preview_checkpoint(ticket['id'], checkpoint['id'])
        result = await app.restore_checkpoint(ticket['id'], checkpoint['id'], fresh['expected_tree'])
        assert result['tree'] == checkpoint['tree']
        assert (await app.store.get('operations', operation['id']))['status'] == 'superseded'
        assert (await app.ticket_execution(ticket['id']))['blocking_reason'] is None
        before = await app.store.get('checkpoints', result['before_checkpoint'])
        assert before['tree'] == fresh['expected_tree']
    finally:
        await app.close()


CRASH_RESTORE = r'''
import asyncio, os, signal, sys
from pathlib import Path
from agilecode.core.application import Application
async def main():
    repository, home, ticket, checkpoint, expected, boundary, marker = sys.argv[1:]
    app = Application(repository, home, execution=False)
    await app.open()
    def crash():
        Path(marker).write_text(boundary)
        os.kill(os.getpid(), signal.SIGKILL)
    if boundary in {'before', 'target'}:
        original = app.checkpoints.restore_tree
        async def restore(*args, **kwargs):
            if boundary == 'before': crash()
            result = await original(*args, **kwargs)
            crash()
        app.checkpoints.restore_tree = restore
    else:
        original = app.git.run
        count = 0
        async def run(*args, **kwargs):
            nonlocal count
            if args[:2] == ('cat-file', 'blob'):
                count += 1
                if count == 2: crash()
            return await original(*args, **kwargs)
        app.git.run = run
    await app.restore_checkpoint(ticket, checkpoint, expected)
asyncio.run(main())
'''


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['before', 'partial', 'target'])
async def test_sigkill_restore_reconciles_twice_without_replaying_files(repository, tmp_path, boundary):
    app, ticket, run, checkpoint, preview = await restore_fixture(repository, tmp_path)
    await app.close()
    marker = tmp_path / ('restore-crashed-' + boundary)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / 'src'))
    child = await asyncio.create_subprocess_exec(sys.executable, '-c', CRASH_RESTORE,
        str(repository), str(tmp_path / 'global'), ticket['id'], checkpoint['id'], preview['expected_tree'],
        boundary, str(marker), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    stdout, stderr = await asyncio.wait_for(child.communicate(), 15)
    assert child.returncode == -signal.SIGKILL, (stdout, stderr)
    assert marker.read_text() == boundary
    observed = None
    for _ in range(2):
        reopened = Application(repository, tmp_path / 'global', execution=False)
        await reopened.open()
        try:
            # Also exercise the recovery owner directly: repeats are inert.
            await reopened.workspace_ops.recover_pending_operations()
            operation = await reopened.store.one("SELECT * FROM operations WHERE kind='checkpoint_restore'")
            assert operation['status'] == {'before':'cancelled', 'partial':'blocked', 'target':'completed'}[boundary]
            tree = await reopened.git.snapshot_tree(Path(run['workspace']))
            if observed is not None:
                assert tree == observed
            observed = tree
            await assert_outputs_invalidated(reopened, run)
            facts = await reopened.ticket_execution(ticket['id'])
            assert bool(facts['blocking_reason']) is (boundary == 'partial')
            if boundary == 'partial':
                fresh = await reopened.preview_checkpoint(ticket['id'], checkpoint['id'])
                assert fresh['expected_tree'] == tree
        finally:
            await reopened.close()


@pytest.mark.asyncio
async def test_close_admission_drains_source_writer_and_rejects_new_operations(repository, tmp_path, monkeypatch):
    app, ticket, run, checkpoint, preview = await restore_fixture(repository, tmp_path)
    release, entered = threading.Event(), threading.Event()
    original = Workspace.write
    def blocked_write(self, *args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Workspace, 'write', blocked_write)
    source = await app.read_source(ticket['id'], 'app.py')
    saving = asyncio.create_task(app.save_source(ticket['id'], 'app.py', 'changed = 4\n', source['hash']))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await app.coordinator.close_admission()
        draining = asyncio.create_task(app.coordinator.drain_owned_work())
        await asyncio.sleep(0)
        assert not draining.done()
        with pytest.raises(Conflict, match='closing'):
            await app.move_ticket(ticket['id'], 'Ready')
        release.set()
        await saving
        await asyncio.wait_for(draining, 5)
        assert not app.store.closed
        assert not app.coordinator.owned_tasks
        assert not app.coordinator.reservations
        await assert_outputs_invalidated(app, run)
    finally:
        release.set()
        await app.close()


CRASH_SOURCE_SAVE = r'''
import asyncio, os, signal, sys
from pathlib import Path
from agilecode.core.application import Application
from agilecode.adapters.workspace import Workspace
async def main():
    repository, home, ticket, expected, boundary, marker = sys.argv[1:]
    app = Application(repository, home, execution=False)
    await app.open()
    original = Workspace.write
    def crash():
        Path(marker).write_text(boundary)
        os.kill(os.getpid(), signal.SIGKILL)
    def write(self, *args, **kwargs):
        if boundary == 'before': crash()
        result = original(self, *args, **kwargs)
        crash()
    Workspace.write = write
    await app.save_source(ticket, 'app.py', 'saved = True\n', expected)
asyncio.run(main())
'''


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['before', 'after'])
async def test_sigkill_source_save_never_keeps_old_verification(repository, tmp_path, boundary):
    app, ticket, run, checkpoint, preview = await restore_fixture(repository, tmp_path)
    source = await app.read_source(ticket['id'], 'app.py')
    await app.close()
    marker = tmp_path / ('source-crashed-' + boundary)
    child = await asyncio.create_subprocess_exec(sys.executable, '-c', CRASH_SOURCE_SAVE,
        str(repository), str(tmp_path / 'global'), ticket['id'], source['hash'], boundary, str(marker),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / 'src')))
    stdout, stderr = await asyncio.wait_for(child.communicate(), 15)
    assert child.returncode == -signal.SIGKILL, (stdout, stderr)
    assert marker.read_text() == boundary
    expected_content = 'before first\n' if boundary == 'before' else 'saved = True\n'
    for _ in range(2):
        reopened = Application(repository, tmp_path / 'global', execution=False)
        await reopened.open()
        try:
            await assert_outputs_invalidated(reopened, run)
            assert (await reopened.read_source(ticket['id'], 'app.py'))['content'] == expected_content
            receipt = await reopened.store.one("SELECT * FROM operations WHERE kind='source_save'")
            assert receipt['status'] == 'interrupted'
            assert decode(receipt['result'])['effect'] == ('not_applied' if boundary == 'before' else 'unconfirmed')
        finally:
            await reopened.close()


@pytest.mark.asyncio
async def test_source_save_keeps_final_hash_guard_after_intent(repository, tmp_path, monkeypatch):
    app, ticket, run, checkpoint, preview = await restore_fixture(repository, tmp_path)
    source = await app.read_source(ticket['id'], 'app.py')
    original = Workspace.write
    def race_external_edit(self, *args, **kwargs):
        (self.root / 'app.py').write_text('external = 8\n')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Workspace, 'write', race_external_edit)
    try:
        with pytest.raises(Conflict):
            await app.save_source(ticket['id'], 'app.py', 'overwrite = 1\n', source['hash'])
        assert (Path(run['workspace'])/'app.py').read_text() == 'external = 8\n'
        await assert_outputs_invalidated(app, run)
        assert (await app.store.one("SELECT * FROM operations WHERE kind='source_save'"))['status'] == 'interrupted'
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_close_waits_for_after_continuation_database_work(repository, tmp_path):
    app = Application(repository, tmp_path/'global', execution=False)
    await app.open(initialize=True)
    entered, release = asyncio.Event(), asyncio.Event()
    async def work():
        return None
    async def after():
        entered.set()
        await release.wait()
        await app.store.activity('AfterContinuation', {'settled': True})
    task = await app.coordinator.spawn('synthetic-ticket', 'owned-test', 'execution', work(), after=after)
    await entered.wait()
    assert not app.coordinator.activities
    closing = asyncio.create_task(app.close())
    await asyncio.sleep(0)
    try:
        assert not closing.done()
        assert not app.store.closed
        release.set()
        await asyncio.wait_for(closing, 5)
        assert task.done()
        import sqlite3
        with sqlite3.connect(app.store.path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM activity WHERE kind='AfterContinuation'").fetchone()[0] == 1
    finally:
        release.set()
        await app.close()
