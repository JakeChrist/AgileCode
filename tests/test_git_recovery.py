import asyncio
import json
import os
import stat
import zipfile
from pathlib import Path
import pytest
from agilecode.core.application import Application
from agilecode.core.backup import restore_backup
from agilecode.core.types import Settings,Conflict,AgileCodeError
from agilecode.adapters.store import encode
from conftest import ScriptClient,git

async def setup_run(root,home,client=None,auto=False):
    app=Application(root,home,client=client or ScriptClient())
    await app.open(initialize=True)
    await app.config.save(Settings(autonomy='full',merge_on_completion=auto))
    ticket=await app.create_ticket('Answer two','Make answer return two and update the test.')
    await app.move_ticket(ticket['id'],'Ready')
    run=await app.start_ticket(ticket['id'])
    return app,ticket,run

@pytest.mark.asyncio
async def test_checkpoint_bytes_modes_links_and_stale_preview(repository,tmp_path):
    app,ticket,run=await setup_run(repository,tmp_path/'global',ScriptClient(question=True))
    try:
        for _ in range(200):
            if await app.store.one("SELECT id FROM waits WHERE status='pending'"):break
            await asyncio.sleep(.01)
        work=Path(run['workspace'])
        (work/'raw.bin').write_bytes(b'\x00\xff\x81raw\r\n')
        (work/'newline.txt').write_bytes(b'one\r\ntwo\r\n')
        (work/'executable.sh').write_text('#!/bin/sh\necho ok\n');(work/'executable.sh').chmod(0o755)
        (work/'link').symlink_to('newline.txt')
        head=git(work,'rev-parse','HEAD');index=git(work,'write-tree')
        cp=await app.checkpoints.capture(run,'Known state')
        assert git(work,'rev-parse','HEAD')==head
        assert git(work,'write-tree')==index
        assert git(repository,'show-ref',cp['ref'])
        (work/'raw.bin').unlink();(work/'newline.txt').write_text('changed')
        (work/'executable.sh').chmod(0o644);(work/'link').unlink();(work/'extra.txt').write_text('new')
        preview=await app.checkpoints.preview(run,cp['id'])
        (work/'extra.txt').write_text('user changed after preview')
        with pytest.raises(Conflict):await app.checkpoints.restore(run,cp['id'],preview['expected_tree'])
        assert (work/'extra.txt').read_text()=='user changed after preview'
        preview=await app.checkpoints.preview(run,cp['id'])
        await app.checkpoints.restore(run,cp['id'],preview['expected_tree'])
        assert (work/'raw.bin').read_bytes()==b'\x00\xff\x81raw\r\n'
        assert (work/'newline.txt').read_bytes()==b'one\r\ntwo\r\n'
        assert (work/'executable.sh').stat().st_mode&stat.S_IXUSR
        assert (work/'link').is_symlink() and os.readlink(work/'link')=='newline.txt'
        assert not (work/'extra.txt').exists()
        assert git(work,'rev-parse','HEAD')==head and git(work,'write-tree')==index
        await app.store.update('checkpoints',cp['id'],status='preparing')
        await app.checkpoints.recover()
        assert (await app.store.get('checkpoints',cp['id']))['status']=='available'
    finally:await app.close()

@pytest.mark.asyncio
async def test_question_survives_engine_restart_without_new_attempt(repository,tmp_path):
    app,ticket,run=await setup_run(repository,tmp_path/'global',ScriptClient(question=True))
    for _ in range(200):
        wait=await app.store.one("SELECT * FROM waits WHERE status='pending'")
        if wait:break
        await asyncio.sleep(.01)
    assert wait
    session=wait['session_id']; await app.close()
    reopened=Application(repository,tmp_path/'global',client=ScriptClient(question=True));await reopened.open()
    try:
        pending=await reopened.store.get('waits',wait['id'])
        assert pending['status']=='pending'
        await reopened.answer(pending['id'],'Yes, make it two.')
        result=await asyncio.wait_for(reopened.wait_for_run(run['id']),30)
        assert result['state']=='completed',result
        attempts=await reopened.store.query("SELECT * FROM attempts WHERE run_id=? AND step_id='understand'",(run['id'],))
        assert len(attempts)==1 and attempts[0]['session_id']==session
    finally:await reopened.close()

@pytest.mark.asyncio
async def test_stale_approval_rejected_then_combined_tree_revalidated(repository,tmp_path):
    app,ticket,run=await setup_run(repository,tmp_path/'global')
    try:
        completed=await app.wait_for_run(run['id']);assert completed['state']=='completed',completed
        candidate=await app.prepare_integration(ticket['id'])
        (repository/'other.txt').write_text('another ticket change\n')
        git(repository,'add','other.txt');git(repository,'commit','-m','Concurrent unrelated change')
        original=git(repository,'rev-parse','HEAD')
        with pytest.raises(Conflict):await app.approve_integration(candidate['id'],candidate['candidate_oid'],candidate['target_oid'])
        assert git(repository,'rev-parse','HEAD')==original
        rebuilt=await app.prepare_integration(ticket['id'],rebuild=True)
        assert rebuilt['state']=='awaiting_review',rebuilt
        report=json.loads(rebuilt['report'])
        assert report['strategy']=='merge' and len(report['validation_attempts'])==2
        await app.approve_integration(rebuilt['id'],rebuilt['candidate_oid'],rebuilt['target_oid'])
        assert (repository/'other.txt').read_text()=='another ticket change\n'
        assert (repository/'app.py').read_text().endswith('return 2\n')
        assert len(git(repository,'rev-list','--parents','-n','1','HEAD').split())==3
    finally:await app.close()

@pytest.mark.asyncio
async def test_cleanup_preserves_unknown_ignored_files(repository,tmp_path):
    app,ticket,run=await setup_run(repository,tmp_path/'global')
    try:
        result=await app.wait_for_run(run['id']);assert result['state']=='completed',result
        # This content is ignored through the main Git exclude file, not an edit to
        # the ticket tree. It must not be removed merely because Git reports clean.
        (repository/'.git/info/exclude').write_text('important-local/\n')
        work=Path(run['workspace']);(work/'important-local').mkdir();(work/'important-local/data').write_text('keep')
        candidate=await app.prepare_integration(ticket['id'])
        merged=await app.approve_integration(candidate['id'],candidate['candidate_oid'],candidate['target_oid'])
        assert merged['state']=='integrated'
        assert (work/'important-local/data').read_text()=='keep'
        assert json.loads(merged['report'])['cleanup']['problems']
    finally:await app.close()

@pytest.mark.asyncio
async def test_two_tickets_execute_without_model_serialization(repository,tmp_path):
    client=ScriptClient(question=True,latency=.08);app,ticket,run=await setup_run(repository,tmp_path/'global',client)
    try:
        second=await app.create_ticket('Second independent ticket','Run independently.')
        await app.move_ticket(second['id'],'Ready');two=await app.start_ticket(second['id'])
        for _ in range(300):
            waits=await app.store.query("SELECT * FROM waits WHERE status='pending'")
            if len(waits)==2:break
            await asyncio.sleep(.01)
        assert len(waits)==2 and run['workspace']!=two['workspace']
        await asyncio.gather(*(app.answer(w['id'],'Yes, two.') for w in waits))
        results=await asyncio.gather(app.wait_for_run(run['id']),app.wait_for_run(two['id']))
        assert all(r['state']=='completed' for r in results),results
        assert client.max_active>1
        assert (repository/'app.py').read_text().endswith('return 1\n')
        assert run['branch']!=two['branch']
        assert git(repository,'rev-parse',run['branch'])==results[0]['final_oid']
        assert git(repository,'rev-parse',two['branch'])==results[1]['final_oid']
    finally:await app.close()

@pytest.mark.asyncio
async def test_backup_restore_uncommitted_ticket_and_reject_overwrite(repository,tmp_path):
    app,ticket,run=await setup_run(repository,tmp_path/'global',ScriptClient(question=True))
    for _ in range(200):
        if await app.store.one("SELECT id FROM waits WHERE status='pending'"):break
        await asyncio.sleep(.01)
    work=Path(run['workspace']);(work/'uncommitted.txt').write_text('Durable data before any final commit.\n')
    await app.close()
    app=Application(repository,tmp_path/'global',client=ScriptClient());await app.open()
    try:
        await app.project_manager.save_settings({"project_instructions": "Back up Project Manager configuration"})
        app.project_manager.state["goals"] = ["Retain PM goals"]
        await app.project_manager._persist_state()
        backup=tmp_path/'recovery.zip';result=await app.backup(backup)
        assert backup.exists() and result['recovery_checkpoints']
    finally:await app.close()
    destination=tmp_path/'recovered'
    restored=await restore_backup(backup,destination,tmp_path/'another-global')
    assert restored['project']==str(destination)
    assert (destination/'.agilecode'/'project_manager'/'settings.json').is_file()
    assert (destination/'.agilecode'/'project_manager'/'state.json').is_file()
    assert not (destination/'.agile_code').exists()
    assert (destination/'app.py').read_text().endswith('return 1\n')
    reopened=Application(destination,tmp_path/'another-global',client=ScriptClient());await reopened.open()
    try:
        assert reopened.project_manager.settings.project_instructions == "Back up Project Manager configuration"
        assert reopened.project_manager.state["goals"] == ["Retain PM goals"]
        loaded=await reopened.latest_run(ticket['id'])
        assert Path(loaded['workspace']).is_relative_to(destination)
        assert (Path(loaded['workspace'])/'uncommitted.txt').read_text()=='Durable data before any final commit.\n'
        assert git(Path(loaded['workspace']),'rev-parse','HEAD')==run['base_oid']
    finally:await reopened.close()
    with pytest.raises(AgileCodeError):await restore_backup(backup,destination)
    tampered=tmp_path/'tampered.zip'
    with zipfile.ZipFile(backup) as source,zipfile.ZipFile(tampered,'w') as out:
        for name in source.namelist():
            data=source.read(name)
            if name=='agilecode.db':data+=b'tamper'
            out.writestr(name,data)
    with pytest.raises(AgileCodeError):await restore_backup(tampered,tmp_path/'bad')
    assert not (tmp_path/'bad').exists()
