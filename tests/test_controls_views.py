import asyncio
import json
from pathlib import Path
import pytest
from agilecode.core.application import Application
from agilecode.core.diff import file_diff
from agilecode.core.types import Settings,AgileCodeError,Conflict
from agilecode.adapters.model import Turn
from conftest import ScriptClient,git
from test_git_recovery import setup_run

def test_diff_preserves_line_mapping_and_final_newline():
    doc=file_diff('a.py','one\ntwo\nthree\n','one\nchanged\nthree')
    changed=[r for r in doc['rows'] if r['kind']=='replace']
    assert changed[0]['old']==2 and changed[0]['new']==2
    assert doc['old_final_newline'] and not doc['new_final_newline']
    doc=file_diff('x.txt','\n'.join(map(str,range(100))),'\n'.join(map(str,range(100))))
    assert any(r['kind']=='fold' for r in doc['rows'])

def test_syntax_spans_respect_utf16_columns():
    pytest.importorskip('pygments')
    from agilecode.desktop.syntax import spans
    data=spans('x = "🌎"; n = 2\n','file.py',True)
    assert max(start+count for start,count,style in data[0])==len('x = "🌎"; n = 2'.encode('utf-16-le'))//2

@pytest.mark.asyncio
async def test_noop_reorder_cannot_restart_running_ticket(repository,tmp_path):
    app,ticket,run=await setup_run(repository,tmp_path/'global',ScriptClient(question=True))
    try:
        row=await app.find_ticket(ticket['id'])
        moved=await app.move_ticket(row['id'],'In Progress',position=-10,expected_revision=row['revision'])
        assert moved['position']==-10
        assert len(await app.store.query('SELECT * FROM runs'))==1
        with pytest.raises(Conflict):await app.move_ticket(row['id'],'In Progress',position=10,expected_revision=row['revision'])
    finally:await app.close()

@pytest.mark.asyncio
async def test_post_cleanup_source_uses_ticket_commit_not_later_base(repository,tmp_path):
    app,ticket,run=await setup_run(repository,tmp_path/'global')
    try:
        await app.wait_for_run(run['id'])
        with pytest.raises(AgileCodeError):
            await app.pause_run(ticket['id'])
        assert (await app.latest_run(ticket['id']))['state']=='completed'
        candidate=await app.prepare_integration(ticket['id'])
        await app.approve_integration(candidate['id'],candidate['candidate_oid'],candidate['target_oid'])
        (repository/'app.py').write_text('def answer():\n    return 99\n')
        assert (await app.read_source(ticket['id'],'app.py'))['content'].endswith('return 2\n')
        # Idempotent repeat cleanup is available after the worktree is gone.
        result=await app.retry_cleanup(ticket['id']);assert result is not None
    finally:await app.close()

class HelperClient(ScriptClient):
    def __init__(self):super().__init__();self.helper_schemas=[]
    async def stream(self,profile,messages,tools,on_delta):
        if 'CURRENT STEP: Helper analysis' in messages[0]['content']:
            self.helper_schemas=[t['function']['name'] for t in tools]
            return Turn({'role':'assistant','content':None,'tool_calls':[{'id':'helper-result','type':'function','function':{'name':'finish_step','arguments':json.dumps({'status':'PASS','summary':'Read-only helper checked scope','output':'The change is confined to answer() and its regression test.'})}}]},'fixture-model',None,'tool_calls')
        return await super().stream(profile,messages,tools,on_delta)
    def action(self,messages):
        if 'CURRENT STEP: Understand ticket' in messages[0]['content'] and not any(m['role']=='tool' for m in messages):
            return 'delegate',{'member_id':'architect','prompt':'Inspect the scope without modifying code.'}
        return super().action(messages)

@pytest.mark.asyncio
async def test_helper_is_attached_readonly_and_does_not_skip_parent(repository,tmp_path):
    client=HelperClient();app,ticket,run=await setup_run(repository,tmp_path/'global',client)
    try:
        result=await app.wait_for_run(run['id']);assert result['state']=='completed',result
        assert 'write_file' not in client.helper_schemas and 'delegate' not in client.helper_schemas
        assert 'final_commit' not in client.helper_schemas
        helpers=await app.store.query("SELECT * FROM attempts WHERE phase LIKE 'helper:%'")
        assert len(helpers)==1 and helpers[0]['run_id']==run['id']
        standard=await app.store.query("SELECT * FROM attempts WHERE phase='development'")
        assert len(standard)==8
    finally:await app.close()

@pytest.mark.asyncio
async def test_write_approval_wait_and_denial_remain_inside_attempt(repository,tmp_path):
    app,ticket,run=await setup_run(repository,tmp_path/'global')
    await app.config.save(Settings(autonomy='restricted',tool_rules={'execute':'allow'}))
    try:
        for _ in range(300):
            waits=await app.store.query("SELECT * FROM waits WHERE status='pending'")
            if waits:break
            await asyncio.sleep(.01)
        assert waits and waits[0]['kind']=='approval'
        work=Path(run['workspace'])
        assert (work/'app.py').read_text().endswith('return 1\n')
        await app.answer(waits[0]['id'],'deny')
        result=await app.wait_for_run(run['id'])
        assert result['state']=='paused',result
        assert (work/'app.py').read_text().endswith('return 1\n')
    finally:await app.close()

class DiscussionClient:
    async def close(self):pass
    async def resolve(self,p):return p.model_copy(update={'model':'read-only-fixture'})
    async def stream(self,profile,messages,tools,on_delta):
        assert all(t['function']['name'] in {'read_file','list_files','code_search','load_skill'} for t in tools)
        if messages[-1]['role']=='tool':
            return Turn({'role':'assistant','content':'app.py defines answer().'},'read-only-fixture',None,'stop')
        return Turn({'role':'assistant','content':None,'tool_calls':[{'id':'read1','type':'function','function':{'name':'read_file','arguments':'{"path":"app.py"}'}}]},'read-only-fixture',None,'tool_calls')

@pytest.mark.asyncio
async def test_read_only_discussion_does_not_create_ticket(repository,tmp_path):
    app=Application(repository,tmp_path/'global',client=DiscussionClient());await app.open(initialize=True)
    try:
        answer=await app.discussion.ask('What does app.py contain?')
        assert 'answer()' in answer
        assert not await app.board()
        assert (repository/'app.py').read_text().endswith('return 1\n')
        await app.discussion.ask('Again?')
    finally:await app.close()
