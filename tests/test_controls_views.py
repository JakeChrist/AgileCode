import asyncio
import json
import re
from pathlib import Path
import pytest
import httpx
from agilecode.core.application import Application
from agilecode.core.diff import file_diff
from agilecode.core.types import Settings,ModelProfile,AgileCodeError,Conflict,CommandRule
from agilecode.core.context import ContextManager
from agilecode.core.defaults import standard_team
from agilecode.core.tools import Tool,ToolManager,RunCommandTool,MCPTool
from agilecode.adapters.model import OpenAICompatibleClient, Turn
from conftest import ScriptClient,git
from test_git_recovery import setup_run

def test_builtin_team_uses_explicit_tool_names_only():
    legacy={"*","read","search","write","destructive","execute","git","skills","control","evidence","mcp"}
    for member in standard_team().members:
        assert member.allowed_tools
        assert not legacy.intersection(member.allowed_tools)


def test_tool_manager_exposes_only_exact_allowed_names():
    class AlphaTool(Tool):
        name="alpha"
        description="alpha"
    class ExecuteNamedTool(Tool):
        name="execute"
        description="literal tool named execute"

    manager=ToolManager(
        [AlphaTool(),ExecuteNamedTool()],
        ["alpha"],
        store=None,
        events=None,
        settings=lambda:Settings(),
        session_id="session",
    )
    assert [schema["function"]["name"] for schema in manager.schemas()]==["alpha"]


def test_command_permission_rules_are_separate_from_containment():
    class DummyShell:
        class Inner:
            cwd=Path(".")
        shell=Inner()

    normal=Settings(autonomy="full")
    command=RunCommandTool(DummyShell(),lambda:normal,"attempt",None)
    assert command.decision(normal,{"command":"pytest","background":False})=="allow"
    assert command.decision(normal,{"command":"pip install example","background":False})=="allow"

    restricted=Settings(autonomy="restricted",command_rules=[])
    assert command.decision(restricted,{"command":"pytest","background":False})=="ask"

    custom=Settings(
        autonomy="full",
        command_rules=[
            CommandRule(pattern="pytest *",action="deny"),
            CommandRule(pattern="pytest tests/unit/*",action="allow"),
        ],
    )
    # Ordered rules: the first matching rule wins.
    assert command.decision(custom,{"command":"pytest tests/unit/test_x.py","background":False})=="deny"


@pytest.mark.asyncio
async def test_mcp_tool_uses_normal_tool_interface():
    class FakeMcp:
        def __init__(self):self.calls=[]
        async def call(self,alias,args):
            self.calls.append((alias,args))
            return {"content":"ok"}

    mcp=FakeMcp()
    tool=MCPTool(
        mcp,
        "mcp_docs_lookup",
        {"description":"Look up docs","schema":{"type":"object","properties":{"q":{"type":"string"}}}},
    )
    assert tool.schema()["function"]["name"]=="mcp_docs_lookup"
    assert await tool.execute({"q":"tool system"},"execution")=={"content":"ok"}
    assert mcp.calls==[("mcp_docs_lookup",{"q":"tool system"})]


def test_diff_preserves_line_mapping_and_final_newline():
    doc=file_diff('a.py','one\ntwo\nthree\n','one\nchanged\nthree')
    changed=[r for r in doc['rows'] if r['kind']=='replace']
    assert changed[0]['old']==2 and changed[0]['new']==2
    assert doc['old_final_newline'] and not doc['new_final_newline']
    doc=file_diff('x.txt','\n'.join(map(str,range(100))),'\n'.join(map(str,range(100))))
    assert any(r['kind']=='fold' for r in doc['rows'])

@pytest.mark.asyncio
async def test_context_compaction_recompresses_prior_summary():
    manager=ContextManager()
    context_window=2048
    messages=[{'role':'system','content':'Keep the current task and constraints.'}]
    for i in range(24):
        messages.append({'role':'user','content':f'turn-{i} ' + ('detail ' * 120)})

    calls=[]
    async def summarize(text):
        calls.append(text)
        return 'summary ' + text[:180]

    prepared,meta=await manager.prepare(messages,context_window,summarize)
    assert meta['compacted']
    assert any('COMPRESSED CONTEXT SUMMARY' in call for call in calls[1:])
    assert ContextManager.estimate(prepared) <= context_window-512


@pytest.mark.asyncio
async def test_context_window_uses_lm_studio_loaded_instance_config():
    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "local-model"}]})
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={
                "models": [{
                    "key": "local-model",
                    "max_context_length": 131072,
                    "loaded_instances": [{
                        "id": "local-model",
                        "config": {"context_length": 12288},
                    }],
                }]
            })
        return httpx.Response(404)

    client=OpenAICompatibleClient(httpx.MockTransport(handler))
    try:
        profile=ModelProfile(base_url="http://127.0.0.1:1234/v1",model="local-model")
        assert await client.context_window(profile)==12288
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_context_window_uses_llamacpp_active_n_ctx():
    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "served-model", "owned_by": "llamacpp"}]})
        if request.url.path == "/props":
            assert request.url.params.get("model") == "served-model"
            return httpx.Response(200, json={
                "default_generation_settings": {"n_ctx": 8192}
            })
        return httpx.Response(404)

    client=OpenAICompatibleClient(httpx.MockTransport(handler))
    try:
        profile=ModelProfile(base_url="http://127.0.0.1:8080/v1",model="served-model")
        assert await client.context_window(profile)==8192
    finally:
        await client.close()



@pytest.mark.asyncio
async def test_conversation_loads_latest_page_then_older_history(repository,tmp_path):
    app=Application(repository,tmp_path/'global',client=ScriptClient())
    await app.open(initialize=True)
    try:
        session='long-session'
        for i in range(405):
            await app.store.message(session,{'role':'assistant','content':f'message-{i+1}'})
        latest=await app.conversation(session)
        assert [row['seq'] for row in latest]==list(range(206,406))
        older=await app.conversation(session,before=latest[0]['seq'])
        assert [row['seq'] for row in older]==list(range(6,206))
        oldest=await app.conversation(session,before=older[0]['seq'])
        assert [row['seq'] for row in oldest]==list(range(1,6))
    finally:
        await app.close()


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
async def test_post_cleanup_source_uses_ticket_commit_not_later_base(repository,tmp_path,monkeypatch):
    from agilecode.core.integration import MergeCoordinator
    entered, cancelled = asyncio.Event(), asyncio.Event()
    original = MergeCoordinator._prepare
    async def held_preparation(self, *args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(MergeCoordinator, '_prepare', held_preparation)
    app,ticket,run=await setup_run(repository,tmp_path/'global')
    try:
        await app.wait_for_run(run['id'])
        await asyncio.wait_for(entered.wait(), 10)
        before = await app.selected_run(ticket['id'])
        # Completed development can still own active integration preparation.
        # Pause must stop that real activity without changing its commit facts.
        await app.pause_run(ticket['id'])
        assert cancelled.is_set()
        current = await app.selected_run(ticket['id'])
        assert current['state'] == before['state'] == 'completed'
        assert (current['final_oid'], current['final_tree']) == (before['final_oid'], before['final_tree'])
        monkeypatch.setattr(MergeCoordinator, '_prepare', original)
        candidate=await app.prepare_integration(ticket['id'])
        await app.approve_integration(candidate['id'],candidate['candidate_oid'],candidate['target_oid'])
        (repository/'app.py').write_text('def answer():\n    return 99\n')
        assert (await app.read_source(ticket['id'],'app.py'))['content'].endswith('return 2\n')
        # Idempotent repeat cleanup is available after the worktree is gone.
        result=await app.retry_cleanup(ticket['id']);assert result is not None
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

class IndeterminateCommandClient(ScriptClient):
    def __init__(self):
        super().__init__()
        self.sent_indeterminate=False

    def action(self,messages):
        system=messages[0]["content"]
        # Match the stable step kind, not a display name. Use the design stage
        # so the normal verification stage still runs and records real tests.
        if re.search(r"CURRENT STEP: .*?\(verify_design\)",system) and not self.sent_indeterminate:
            self.sent_indeterminate=True
            return "run_command",{"command":"python -c \"print('contained')\""}
        return super().action(messages)


@pytest.mark.asyncio
async def test_full_autonomy_runs_indeterminate_command_without_approval(repository,tmp_path):
    client=IndeterminateCommandClient()
    app,ticket,run=await setup_run(repository,tmp_path/'global',client)
    try:
        result=await asyncio.wait_for(app.wait_for_run(run['id']),30)
        assert result['state']=='completed',result
        assert client.sent_indeterminate
        waits=await app.store.query(
            "SELECT * FROM waits WHERE ticket_id=? AND kind='approval'",
            (ticket['id'],),
        )
        assert not waits
        command=await app.store.one(
            "SELECT * FROM tools WHERE name='run_command' AND args LIKE '%python -c%' ORDER BY created LIMIT 1"
        )
        assert command and command['status']=='completed'
        payload=json.loads(command['result'])
        assert payload['status']=='ok'
        assert payload['data']['exit_code']==0
        assert 'contained' in payload['data']['output']
        verification=await app.store.one(
            "SELECT * FROM tools WHERE name='run_command' AND args LIKE '%unittest discover%'"
        )
        assert verification and verification['status']=='completed'
        assert json.loads(verification['result'])['data']['exit_code']==0
    finally:
        await app.close()


class DiscussionClient:
    async def close(self):pass
    async def resolve(self,p):return p.model_copy(update={'model':'read-only-fixture'})
    async def context_window(self,p):return 32768
    async def stream(self,profile,messages,tools,on_delta):
        # Discussion source access is read-only; explicit ticket creation is an
        # established separate capability, unused by this question-only client.
        assert {t['function']['name'] for t in tools}=={
            'read_file','list_files','code_search','load_skill','create_ticket',
        }
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
        assert not await app.board()
        assert not await app.store.one("SELECT id FROM tools WHERE name='create_ticket'")
    finally:await app.close()


class BlockingDiscussionClient:
    def __init__(self):
        self.started=asyncio.Event()

    async def close(self):pass
    async def resolve(self,p):return p.model_copy(update={'model':'blocking-fixture'})
    async def context_window(self,p):return 32768
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
        assert request['error']=='Cancelled by user'
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


@pytest.mark.asyncio
async def test_ticket_correction_interrupts_generation_and_resumes_same_session(repository,tmp_path):
    client=BlockingTicketClient()
    app,ticket,run=await setup_run(repository,tmp_path/'global',client)
    try:
        await asyncio.wait_for(client.started.wait(),2)
        attempt=await app.store.one(
            "SELECT * FROM attempts WHERE run_id=? AND phase='development' ORDER BY created LIMIT 1",
            (run['id'],),
        )
        result=await app.submit_feedback(
            ticket['id'],
            'Keep the change narrowly focused on the requested behavior.',
            interrupt=True,
        )
        assert result['queued'] and result['interrupted']
        assert result['session_id']==attempt['session_id']

        finished=await asyncio.wait_for(app.wait_for_run(run['id']),30)
        assert finished['state']=='completed',finished

        messages=await app.store.messages(attempt['session_id'],include_partial=True)
        feedback=[
            row for row in messages
            if row['body'].get('role')=='user'
            and row['body'].get('content')=='USER FEEDBACK: Keep the change narrowly focused on the requested behavior.'
        ]
        assert len(feedback)==1
        first_step_attempts=await app.store.query(
            "SELECT * FROM attempts WHERE run_id=? AND step_id=? AND phase='development'",
            (run['id'],attempt['step_id']),
        )
        assert len(first_step_attempts)==1
        activity=await app.store.query(
            "SELECT * FROM activity WHERE ticket_id=? AND kind='FeedbackDelivered'",
            (ticket['id'],),
        )
        assert len(activity)==1
    finally:
        await app.close()


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
    async def context_window(self,p):return 32768
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
async def test_ticket_output_length_pauses_without_synthetic_continue(repository,tmp_path):
    client=LengthThenTicketClient()
    app,ticket,run=await setup_run(repository,tmp_path/'global',client)
    try:
        result=await asyncio.wait_for(app.wait_for_run(run['id']),30)
        assert result['state']=='paused',result
        assert 'provider output limit' in result['error']
        messages=await app.store.query(
            "SELECT * FROM messages WHERE partial=1 ORDER BY created"
        )
        assert any('Partial step analysis.' in row['body'] for row in messages)
        synthetic=await app.store.query(
            "SELECT * FROM messages WHERE body LIKE '%Continue the same assigned step%'"
        )
        assert not synthetic
        detail=await app.ticket_detail(ticket['id'])
        assert detail['execution']['can_resume']
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
        assert await app.active_run(ticket['id']) is None
        assert (await app.store.get('runs',run['id']))['state']=='failed'
        assert not (await app.ticket_detail(ticket['id']))['execution']['can_resume']

        # Reads must not repair durable state. Recovery explicitly reconciles
        # the selected run using the interrupted attempt as evidence.
        await app.execution_ops.recover_pending_operations()
        recovery=await app.store.one(
            "SELECT * FROM operations WHERE kind='legacy_run_recovery' AND owner_id=?",
            (run['id'],),
        )
        assert recovery and recovery['status']=='completed'
        assert (await app.store.get('runs',run['id']))['state']=='paused'
        active=await app.active_run(ticket['id'])
        assert active and active['id']==run['id']

        detail=await app.ticket_detail(ticket['id'])
        assert detail['execution']['can_resume']

        await app.resume_run(ticket['id'])
        finished=await asyncio.wait_for(app.wait_for_run(run['id']),30)
        assert finished['state']=='completed',finished
    finally:
        await app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancellation',['legacy_label','explicit_cancel'])
async def test_cancelled_run_requires_explicit_resume_and_reuses_original_execution(repository,tmp_path,cancellation):
    client=BlockingTicketClient()
    app,ticket,run=await setup_run(repository,tmp_path/'global',client)
    try:
        await asyncio.wait_for(client.started.wait(),2)
        stopped=await app.stop_generation(ticket['id'])
        assert stopped['stopped']
        paused=await asyncio.wait_for(app.wait_for_run(run['id']),2)
        assert paused['state']=='paused'
        if cancellation=='explicit_cancel':
            await app.cancel_run(ticket['id'])
        else:
            await app.store.update('runs',run['id'],state='cancelled',error='legacy automatic cancellation')
        cancelled=await app.store.get('runs',run['id'])
        attempts=await app.store.query('SELECT * FROM attempts WHERE run_id=?',(run['id'],))

        assert await app.active_run(ticket['id']) is None
        await app.execution_ops.recover_pending_operations()
        detail=await app.ticket_detail(ticket['id'])
        assert detail['ticket']['state']=='In Progress'
        assert detail['run']['id']==run['id']
        assert detail['execution']['can_resume']

        # Automatic continuation, including Resume All and board activation,
        # must leave deliberate cancellation untouched.
        assert (await app.continue_ticket(ticket['id']))['execution']=='cancelled'
        assert await app.store.get('runs',run['id'])==cancelled
        assert await app.store.query('SELECT * FROM attempts WHERE run_id=?',(run['id'],))==attempts

        # Moving the card out and back also must not restart cancelled work.
        await app.move_ticket(ticket['id'],'Ready')
        await app.move_ticket(ticket['id'],'In Progress')
        assert (await app.selected_run(ticket['id']))['id']==run['id']
        assert (await app.store.get('runs',run['id']))['state']=='cancelled'

        await app.resume_run(ticket['id'])
        finished=await asyncio.wait_for(app.wait_for_run(run['id']),30)
        assert finished['state']=='completed',finished
        assert finished['id']==run['id']
        assert finished['workspace']==cancelled['workspace']
        assert finished['branch']==cancelled['branch']
        assert (await app.selected_run(ticket['id']))['id']==run['id']
        assert len(await app.store.query('SELECT * FROM runs WHERE ticket_id=?',(ticket['id'],)))==1
    finally:
        await app.close()


class ReviewBlockingClient(ScriptClient):
    def __init__(self):
        super().__init__()
        self.review_started=asyncio.Event()
        self.block_review=True

    async def stream(self,profile,messages,tools,on_delta):
        if self.block_review and "CURRENT STEP:" in messages[0]["content"] and "(review)" in messages[0]["content"]:
            self.block_review=False
            self.review_started.set()
            await asyncio.Event().wait()
        return await super().stream(profile,messages,tools,on_delta)


@pytest.mark.asyncio
async def test_nearly_completed_cancelled_run_resumes_after_unarchive_without_repeating_steps(repository,tmp_path):
    client=ReviewBlockingClient()
    app,ticket,run=await setup_run(repository,tmp_path/'global',client)
    try:
        await asyncio.wait_for(client.review_started.wait(),20)
        before=await app.store.get('runs',run['id'])
        assert before['step_index']>0
        assert before['outputs']!='{}'
        accepted=before['outputs']

        await app.cancel_run(ticket['id'])
        cancelled=await app.store.get('runs',run['id'])
        assert cancelled['state']=='cancelled'
        assert cancelled['step_index']==before['step_index']
        assert cancelled['outputs']==accepted

        await app.archive_ticket(ticket['id'],True)
        assert not (await app.ticket_detail(ticket['id']))['execution']['can_resume']
        await app.archive_ticket(ticket['id'],False)
        assert (await app.ticket_detail(ticket['id']))['execution']['can_resume']
        assert (await app.store.get('runs',run['id']))['state']=='cancelled'

        await app.resume_run(ticket['id'])
        finished=await asyncio.wait_for(app.wait_for_run(run['id']),30)
        assert finished['state']=='completed',finished
        assert finished['id']==run['id']
        assert finished['workspace']==cancelled['workspace']
        assert finished['branch']==cancelled['branch']
        for step_id, attempt_id in json.loads(accepted).items():
            assert json.loads(finished['outputs'])[step_id]==attempt_id
        assert len(await app.store.query('SELECT * FROM runs WHERE ticket_id=?',(ticket['id'],)))==1
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_project_manager_resume_ticket_tool_restores_cancelled_run(repository,tmp_path):
    client=BlockingTicketClient()
    app,ticket,run=await setup_run(repository,tmp_path/'global',client)
    try:
        await asyncio.wait_for(client.started.wait(),2)
        await app.cancel_run(ticket['id'])
        cancelled=await app.store.get('runs',run['id'])
        assert cancelled['state']=='cancelled'

        tool=app.project_manager._tool_manager().tools['resume_ticket']
        assert 'cancelled' in tool.description
        args=tool.validate(json.dumps({'ticket':ticket['key']}))
        assert await tool.execute(args,'pm-resume-cancelled')=={'resumed':ticket['key']}

        finished=await asyncio.wait_for(app.wait_for_run(run['id']),30)
        assert finished['state']=='completed',finished
        assert finished['id']==run['id']
        assert finished['workspace']==cancelled['workspace']
        assert len(await app.store.query('SELECT * FROM runs WHERE ticket_id=?',(ticket['id'],)))==1
    finally:
        await app.close()
