import asyncio
import json
import sqlite3
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
import pytest
from agilecode.core.application import Application
from agilecode.adapters.store import ProjectStore, SCHEMA_VERSION
from agilecode.domain.types import AgileCodeError
from agilecode.cli import parser, execute

@pytest.mark.asyncio
async def test_definition_only_host_never_constructs_runtime(repository,tmp_path,monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('Runtime construction in definition-only host')
    monkeypatch.setattr('agilecode.core.application.Application._initialize_runtime',forbidden)
    app=Application(repository,tmp_path/'global',execution=False)
    await app.open(initialize=True)
    try:
        assert app.client is app.processor is app.project_manager is app.merges is None
        assert app.discussion._engine is None
        ticket=await app.create_ticket('Organize','No model required')
        moved=await app.move_ticket(ticket['id'],'In Progress',request_id='one-shot')
        assert moved['state']=='In Progress'
        assert moved['movement']['execution'] in {'blocked','unavailable'}
        assert not app.tasks and not app.runtimes
        assert await app.selected_run(ticket['id']) is None
        assert (await app.move_ticket(ticket['id'],'In Progress',request_id='one-shot'))==moved
        assert (await app.ticket_detail(ticket['id']))['ticket']['id']==ticket['id']
    finally:await app.close()

@pytest.mark.asyncio
async def test_cli_explicit_zero_cursor_and_shared_move(repository,tmp_path):
    args=parser().parse_args(['-p',str(repository),'--global-dir',str(tmp_path/'global'),'init'])
    await execute(args)
    app=Application(repository,tmp_path/'global',execution=False);await app.open()
    try:
        ticket=await app.create_ticket('CLI','Shared operations')
        for word in ('first','second','third'):
            await app.store.message('session',{'role':'user','content':word})
    finally:await app.close()
    base=['-p',str(repository),'--global-dir',str(tmp_path/'global')]
    rows=await execute(parser().parse_args(base+['conversation','session','--after','0']))
    assert [r['body']['content'] for r in rows]==['first','second','third']
    assert parser().parse_args(base+['conversation','session']).after is None
    moved=await execute(parser().parse_args(base+['move',ticket['id'],'In Progress','--request-id','cli-move']))
    assert moved['state']=='In Progress' and moved['movement']['board_committed']
    assert moved['movement']['execution'] in {'blocked','unavailable'}

@pytest.mark.asyncio
async def test_schema_upgrade_rolls_back_and_preserves_verified_backup(tmp_path):
    path=tmp_path/'old.db'
    c=sqlite3.connect(path);c.execute('CREATE TABLE marker(value TEXT)');c.execute("INSERT INTO marker VALUES('preserve')");c.execute('PRAGMA user_version=3');c.commit();c.close()
    store=ProjectStore(path);await store.open(migrate=False)
    try:
        def fail(c):
            c.execute("UPDATE marker SET value='wrong'")
            raise RuntimeError('injected migration failure')
        with pytest.raises(RuntimeError,match='injected'):
            await store.upgrade_schema(data_migrations=(fail,))
        assert (await store.one('PRAGMA user_version'))['user_version']==3
        assert (await store.one('SELECT * FROM marker'))['value']=='preserve'
        assert await store.one("SELECT 1 FROM sqlite_master WHERE name='runs'") is None
        backups=list((tmp_path/'migration-backups').glob('*.db'));assert backups
        c=sqlite3.connect(backups[0]);assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok';assert c.execute('SELECT value FROM marker').fetchone()[0]=='preserve';c.close()
        await store.upgrade_schema()
        assert (await store.one('PRAGMA user_version'))['user_version']==SCHEMA_VERSION
        assert await store.upgrade_schema() is None
        assert (await store.one('PRAGMA foreign_keys'))['foreign_keys']==1
    finally:await store.close()

@pytest.mark.asyncio
async def test_future_configuration_refuses_before_database_upgrade(repository,tmp_path):
    storage=repository/'.agilecode';storage.mkdir()
    db=storage/'agilecode.db';c=sqlite3.connect(db);c.execute('CREATE TABLE marker(value TEXT)');c.execute('PRAGMA user_version=3');c.commit();c.close()
    global_root=tmp_path/'global';global_root.mkdir();(global_root/'settings.json').write_text(json.dumps({'format_version':999}))
    app=Application(repository,global_root,execution=False)
    try:
        with pytest.raises((AgileCodeError,ValueError)):
            await app.open()
    finally:await app.close()
    c=sqlite3.connect(db);assert c.execute('PRAGMA user_version').fetchone()[0]==3;assert c.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone() is None;c.close()

@pytest.mark.parametrize('action,payload',[('resume',{'ticket':'t'}),('resume_execution',{'ticket':'t'}),('move',{'ticket':'t','state':'In Progress'})])
def test_real_bridge_unsaved_guard_covers_alias_and_activation(action,payload):
    from PySide6.QtCore import QObject
    from agilecode.desktop.bridge import Bridge
    bridge=Bridge.__new__(Bridge);QObject.__init__(bridge)
    bridge.windows=[SimpleNamespace(dirty=lambda:True)];bridge.outbox=Queue()
    identity=bridge.dispatch(action,json.dumps(payload))
    kind,(_,returned_action,result)=bridge.outbox.get_nowait()
    assert identity and kind=='response' and returned_action==action
    assert result['ok'] is False and 'Save or discard' in result['error']
