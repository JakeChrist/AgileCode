"""Discarded task histories must not survive in operation receipts."""
import asyncio
import pytest

from agilecode.adapters.store import encode
from agilecode.core.application import Application
from agilecode.core.types import now
from test_workspace_restore_recovery import BlockingClient


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['delete', 'rewind'])
async def test_discard_removes_owned_receipts_and_preserves_unrelated(repository, tmp_path, action):
    client = BlockingClient()
    app = Application(repository, tmp_path/'global', client=client)
    await app.open(initialize=True)
    try:
        ticket = await app.create_ticket('Discard receipts', 'Erase only owned histories.')
        await app.move_ticket(ticket['id'], 'In Progress')
        await asyncio.wait_for(client.started.wait(), 5)
        await app.pause_run(ticket['id'])
        run = await app.selected_run(ticket['id'])
        attempt = await app.store.one('SELECT * FROM attempts WHERE run_id=? ORDER BY rowid', (run['id'],))
        session = attempt['session_id']
        request = await app.store.one('SELECT * FROM requests WHERE session_id=?', (session,))
        receipts = []
        for index, kind in enumerate(['write_file', 'update_progress', 'record_check']):
            tool_id = 'discard-tool-' + str(index)
            await app.store.insert('tools', dict(id=tool_id, session_id=session, request_id=request['id'],
                call_id=tool_id, name=kind, args='{}', status='completed', result='{}',
                category='mutation', created=now(), finished=now()))
            owner = run['workspace'] if kind == 'write_file' else attempt['id']
            receipts.append((tool_id, kind, owner))
            receipts.extend((prefix + tool_id, prefix[:-1].replace('-', '_'), session) for prefix in
                ['conversation-result:', 'conversation-dispatch:', 'conversation-reconciliation:'])
        for kind in ['input', 'turn', 'state', 'generation']:
            receipts.append(('hashed-' + kind, 'conversation_' + kind, session))
        receipts.append(('unrelated', 'conversation_input', 'unrelated-session'))
        for identity, kind, owner in receipts:
            await app.store.insert('operations', dict(id=identity, kind=kind, owner_id=owner,
                data=encode({'text': 'private task text'}), result='{}', status='completed', created=now(), updated=now()))
        await app.store.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', ('conversation:' + session, '{}'))
        await app.store.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', ('conversation:unrelated-session', '{}'))
        if action == 'delete':
            result = await app.delete_ticket(ticket['id'], request_id='discard-request')
        else:
            result = await app.delete_attempt(ticket['id'], attempt['id'], request_id='discard-request')
        assert result['deleted']
        for identity, _, _ in receipts:
            exists = await app.store.one('SELECT 1 FROM operations WHERE id=?', (identity,))
            assert bool(exists) == (identity == 'unrelated')
        assert not await app.store.one('SELECT 1 FROM operations WHERE owner_id IN (?,?)', (session, attempt['id']))
        assert not await app.store.one('SELECT 1 FROM meta WHERE key=?', ('conversation:' + session,))
        assert await app.store.one('SELECT 1 FROM meta WHERE key=?', ('conversation:unrelated-session',))
        assert (await app.store.get('operations', 'discard-request'))['status'] == 'completed'
    finally:
        await app.close()
