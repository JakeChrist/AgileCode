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
        discussion=await app.discussion.create()
        answer=await app.discussion.ask(discussion['id'],'What does app.py contain?')
        assert 'answer()' in answer['content']
        assert not await app.board()
        assert (repository/'app.py').read_text().endswith('return 1\n')
        await app.discussion.ask(discussion['id'],'Again?')
    finally:await app.close()


class BlockingDiscussionClient:
    def __init__(self):
        self.started=asyncio.Event()

    async def close(self):pass
    async def resolve(self,p):return p.model_copy(update={'model':'blocking-fixture'})
    async def stream(self,profile,messages,tools,on_delta):
        on_delta('reasoning','Inspecting the project. ')
        on_delta('text','Partial answer')
        self.started.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_project_discussion_generation_can_be_stopped_and_partial_output_is_retained(repository,tmp_path):
    client=BlockingDiscussionClient()
    app=Application(repository,tmp_path/'global',client=client);await app.open(initialize=True)
    try:
        discussion=await app.discussion.create()
        task=asyncio.create_task(app.discussion.ask(discussion['id'],'Explain this project.'))
        await asyncio.wait_for(client.started.wait(),2)
        stopped=await app.discussion.stop(discussion['id'])
        assert stopped['stopped']
        result=await asyncio.wait_for(task,2)
        assert result['stopped']

        history=(await app.discussion.history(discussion['id']))['messages']
        partial=[m for m in history if m['body']['role']=='assistant' and m['partial']]
        assert len(partial)==1
        assert partial[0]['body']['content']=='Partial answer'
        assert partial[0]['body']['reasoning_content']=='Inspecting the project. '
        request=(await app.store.query("SELECT * FROM requests WHERE session_id=?",(discussion['session_id'],)))[0]
        assert request['status']=='interrupted'
        assert request['error']=='Stopped by user'
    finally:await app.close()


class BlockingTicketClient(ScriptClient):
    def __init__(self):
        super().__init__()
        self.started=asyncio.Event()
        self.block_once=True

    async def stream(self,profile,messages,tools,on_delta):
        if self.block_once:
            self.block_once=False
            on_delta('reasoning','Planning. ')
            on_delta('text','Partial ticket response')
            self.started.set()
            await asyncio.Event().wait()
        return await super().stream(profile,messages,tools,on_delta)


@pytest.mark.asyncio
async def test_ticket_generation_stop_pauses_run_without_cancelling_ticket(repository,tmp_path):
    client=BlockingTicketClient()
    app,ticket,run=await setup_run(repository,tmp_path/'global',client)
    try:
        await asyncio.wait_for(client.started.wait(),2)
        stopped=await app.stop_generation(ticket['id'])
        assert stopped['stopped']
        result=await asyncio.wait_for(app.wait_for_run(run['id']),2)
        assert result['state']=='paused'
        assert result['error']=='Model generation stopped by user.'
        assert (await app.find_ticket(ticket['id']))['state']=='In Progress'

        messages=await app.conversation(stopped['session_id'])
        partial=[m for m in messages if m['body']['role']=='assistant' and m['partial']]
        assert len(partial)==1
        assert partial[0]['body']['content']=='Partial ticket response'
        assert partial[0]['body']['reasoning_content']=='Planning. '
    finally:await app.close()


class SlowResolveDiscussionClient(BlockingDiscussionClient):
    def __init__(self):
        super().__init__()
        self.resolving=asyncio.Event()
        self.release=asyncio.Event()

    async def resolve(self,p):
        self.resolving.set()
        await self.release.wait()
        return p.model_copy(update={'model':'blocking-fixture'})


@pytest.mark.asyncio
async def test_discussion_stop_works_even_before_model_stream_starts(repository,tmp_path):
    client=SlowResolveDiscussionClient()
    app=Application(repository,tmp_path/'global',client=client);await app.open(initialize=True)
    try:
        discussion=await app.discussion.create()
        task=asyncio.create_task(app.discussion.ask(discussion['id'],'Stop this immediately.'))
        await asyncio.wait_for(client.resolving.wait(),2)
        stopped=await app.discussion.stop(discussion['id'])
        assert stopped['stopped'] and stopped['pending']
        client.release.set()
        result=await asyncio.wait_for(task,2)
        assert result['stopped']
    finally:await app.close()


class LengthDiscussionClient:
    async def close(self):pass
    async def resolve(self,p):return p.model_copy(update={'model':'length-fixture'})
    async def stream(self,profile,messages,tools,on_delta):
        on_delta('reasoning','Reasoning reached the limit. ')
        on_delta('text','Visible partial answer.')
        return Turn({'role':'assistant','content':'Visible partial answer.','reasoning_content':'Reasoning reached the limit. '},
                    'length-fixture',None,'length')


@pytest.mark.asyncio
async def test_project_discussion_length_response_is_retained_without_error(repository,tmp_path):
    app=Application(repository,tmp_path/'global',client=LengthDiscussionClient());await app.open(initialize=True)
    try:
        discussion=await app.discussion.create()
        result=await app.discussion.ask(discussion['id'],'Give me a long answer.')
        assert result['truncated']
        history=(await app.discussion.history(discussion['id']))['messages']
        assistant=[m for m in history if m['body']['role']=='assistant'][-1]
        assert assistant['partial']
        assert assistant['body']['content']=='Visible partial answer.'
    finally:await app.close()


class LengthThenTicketClient(ScriptClient):
    def __init__(self):
        super().__init__()
        self.truncated=False

    async def stream(self,profile,messages,tools,on_delta):
        if not self.truncated:
            self.truncated=True
            on_delta('text','Partial step analysis.')
            return Turn({'role':'assistant','content':'Partial step analysis.'},
                        'fixture-model',None,'length')
        return await super().stream(profile,messages,tools,on_delta)


@pytest.mark.asyncio
async def test_ticket_agent_continues_after_text_length_finish(repository,tmp_path):
    client=LengthThenTicketClient()
    app,ticket,run=await setup_run(repository,tmp_path/'global',client)
    try:
        result=await asyncio.wait_for(app.wait_for_run(run['id']),30)
        assert result['state']=='completed',result
        messages=await app.store.query(
            "SELECT * FROM messages WHERE partial=1 ORDER BY created"
        )
        assert any('Partial step analysis.' in row['body'] for row in messages)
    finally:await app.close()


@pytest.mark.asyncio
async def test_ticket_resume_uses_active_execution_even_when_newer_terminal_run_exists(repository,tmp_path):
    client=BlockingTicketClient()
    app,ticket,run=await setup_run(repository,tmp_path/'global',client)
    try:
        await asyncio.wait_for(client.started.wait(),2)
        stopped=await app.stop_generation(ticket['id'])
        assert stopped['stopped']
        paused=await asyncio.wait_for(app.wait_for_run(run['id']),2)
        assert paused['state']=='paused'

        newer=dict(paused)
        newer.update({
            'id':'newer-failed-run',
            'state':'failed',
            'error':'synthetic failed history record',
            'created':'9999-01-01T00:00:00.000+00:00',
            'updated':'9999-01-01T00:00:00.000+00:00',
        })
        await app.store.insert('runs',newer)
        assert (await app.latest_run(ticket['id']))['id']=='newer-failed-run'

        detail=await app.ticket_detail(ticket['id'])
        assert detail['run']['id']==run['id']
        assert detail['execution']['run_id']==run['id']
        assert detail['execution']['can_resume']

        await app.resume_run(ticket['id'])
        finished=await asyncio.wait_for(app.wait_for_run(run['id']),30)
        assert finished['state']=='completed',finished
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_ticket_resume_repairs_failed_run_with_real_interrupted_attempt(repository,tmp_path):
    client=BlockingTicketClient()
    app,ticket,run=await setup_run(repository,tmp_path/'global',client)
    try:
        await asyncio.wait_for(client.started.wait(),2)
        stopped=await app.stop_generation(ticket['id'])
        assert stopped['stopped']
        paused=await asyncio.wait_for(app.wait_for_run(run['id']),2)
        assert paused['state']=='paused'

        interrupted=await app.store.one(
            "SELECT * FROM attempts WHERE run_id=? AND phase='development' AND status='interrupted' "
            "ORDER BY number DESC LIMIT 1",
            (run['id'],)
        )
        assert interrupted is not None

        # Reproduce the inconsistent persisted state older builds could leave behind:
        # the ticket is still In Progress and has a real interrupted session, but
        # the run row itself says failed.
        await app.store.update('runs',run['id'],state='failed',error='stale failed state')
        active=await app.active_run(ticket['id'])
        assert active and active['id']==run['id']

        detail=await app.ticket_detail(ticket['id'])
        assert detail['execution']['can_resume']

        await app.resume_run(ticket['id'])
        finished=await asyncio.wait_for(app.wait_for_run(run['id']),30)
        assert finished['state']=='completed',finished
    finally:
        await app.close()
