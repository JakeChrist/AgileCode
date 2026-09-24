import asyncio
import json
import re
import sys
from pathlib import Path
import httpx
import pytest
from agilecode.adapters.mcp import McpService,McpConnection
from agilecode.adapters.model import StreamAssembler,ModelError,OpenAICompatibleClient
from agilecode.core.types import McpServerConfig,Settings,AgileCodeError,ModelProfile
from agilecode.core.application import Application
from conftest import ScriptClient,git
from test_git_recovery import setup_run

@pytest.mark.asyncio
async def test_mcp_stdio_handshake_roots_pagination_tools_resources(tmp_path):
    server=Path(__file__).parents[1]/'examples/mcp_echo_server.py'
    config=McpServerConfig(id='echo',transport='stdio',command=sys.executable,args=[str(server)],enabled=True,scope='ticket')
    service=McpService([config],tmp_path)
    try:
        await service.discover()
        assert not service.errors,service.errors
        assert len(service.tools)==1
        alias=next(iter(service.tools));out=await service.call(alias,{'text':'hello'})
        assert out['content'][0]['text']=='hello'
        assert out['content'][1]['text']=='cwd='+str(tmp_path)
        assert service.resources[0]['uri']=='memory://example'
        content=await service.read_resource('echo','memory://example')
        assert content['contents'][0]['text'].startswith('This is a read-only')
    finally:await service.close()
    assert not service.connections

@pytest.mark.asyncio
async def test_mcp_http_sse_and_session_headers(tmp_path,monkeypatch):
    original=httpx.AsyncClient;requests=[]
    async def handle(request):
        requests.append(request)
        if request.method=='DELETE':return httpx.Response(200,json={})
        payload=json.loads(request.content)
        if 'id' not in payload:return httpx.Response(202)
        if payload['method']=='initialize':
            return httpx.Response(200,headers={'Mcp-Session-Id':'test-session'},json={'jsonrpc':'2.0','id':payload['id'],'result':{'protocolVersion':'2025-03-26','capabilities':{},'serverInfo':{'name':'fixture'}}})
        assert request.headers['Mcp-Session-Id']=='test-session'
        event={'jsonrpc':'2.0','id':payload['id'],'result':{'tools':[]}}
        return httpx.Response(200,headers={'Content-Type':'text/event-stream'},text='data: '+json.dumps(event)+'\n\n')
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    connection=McpConnection(McpServerConfig(id='remote',transport='http',url='http://localhost/mcp',enabled=True),tmp_path)
    try:
        await connection.start()
        assert await connection.list_items('tools/list','tools')==[]
    finally:await connection.close()
    assert requests[-1].method=='DELETE'

@pytest.mark.asyncio
async def test_mcp_disabled_server_is_not_launched(tmp_path):
    service=McpService([McpServerConfig(id='off',transport='stdio',command='does-not-exist',enabled=False)],tmp_path)
    await service.discover();assert not service.errors and not service.connections

@pytest.mark.asyncio
async def test_real_http_protocol_demo(tmp_path):
    sys.path.insert(0,str(Path(__file__).parents[1]/'examples'))
    from demo import exercise
    result=await exercise(tmp_path/'demo',tmp_path/'global')
    assert result['state']=='Done' and len(result['steps'])==8
    assert result['http_requests']>=16

class ConflictClient(ScriptClient):
    def action(self,messages):
        if 'CURRENT STEP: Resolve merge conflicts' in messages[0]['content']:
            results=[]
            for message in messages:
                if message['role']=='tool':results.append(json.loads(message['content']))
            if not results:return 'read_file',{'path':'app.py'}
            if len(results)==1:
                return 'write_file',{'path':'app.py','content':'def answer():\n    return 2  # target annotation\n','expected_hash':results[0]['data']['hash']}
            return 'finish_step',{'status':'PASS','summary':'Combined ticket behavior and target annotation','output':'Resolved the conflict by retaining the target annotation and returning 2.'}
        return super().action(messages)

@pytest.mark.asyncio
async def test_conflicting_merge_runs_resolver_verify_and_review(repository,tmp_path):
    app,ticket,run=await setup_run(repository,tmp_path/'global',ConflictClient())
    try:
        done=await app.wait_for_run(run['id']);assert done['state']=='completed',done
        (repository/'app.py').write_text('def answer():\n    return 1  # target annotation\n')
        git(repository,'add','app.py');git(repository,'commit','-m','Add target annotation')
        old=git(repository,'rev-parse','HEAD')
        candidate=await app.prepare_integration(ticket['id'])
        assert candidate['state']=='awaiting_review',candidate
        assert git(repository,'rev-parse','HEAD')==old
        report=json.loads(candidate['report']);assert 'app.py' in report['conflicts']
        assert len(report['validation_attempts'])==3
        await app.approve_integration(candidate['id'],candidate['candidate_oid'],candidate['target_oid'])
        assert (repository/'app.py').read_text()=='def answer():\n    return 2  # target annotation\n'
    finally:await app.close()

class FalsePass(ScriptClient):
    def action(self,messages):
        if re.search(r'CURRENT STEP: .*?\(verify\)',messages[0]['content']):
            return 'finish_step',{'status':'PASS','summary':'Pretend tests passed','output':'No actual evidence supplied.'}
        return super().action(messages)

@pytest.mark.asyncio
async def test_engine_rejects_verification_without_evidence(repository,tmp_path):
    app,ticket,run=await setup_run(repository,tmp_path/'global',FalsePass())
    try:
        done=await app.wait_for_run(run['id'])
        assert done['state']=='paused',done
        assert done['final_oid'] is None
        assert git(Path(run['workspace']),'rev-parse','HEAD')==run['base_oid']
        assert (await app.find_ticket(ticket['id']))['state']=='In Progress'
    finally:await app.close()

@pytest.mark.asyncio
async def test_automatic_merge_policy_independent_of_full_tool_autonomy(repository,tmp_path):
    app,ticket,run=await setup_run(repository,tmp_path/'global',auto=True)
    try:
        done=await app.wait_for_run(run['id'])
        if ticket['id'] in app.integration_tasks:await app.integration_tasks[ticket['id']]
        assert (await app.find_ticket(ticket['id']))['state']=='Done'
        assert (repository/'app.py').read_text().endswith('return 2\n')
    finally:await app.close()


def test_length_finish_is_partial_text_not_protocol_failure():
    assembler=StreamAssembler()
    assembler.feed({"choices":[{"index":0,"delta":{"reasoning_content":"Thinking. ","content":"Partial answer"},"finish_reason":"length"}]})
    turn=assembler.finish()
    assert turn.finish_reason=="length"
    assert turn.message["content"]=="Partial answer"
    assert turn.message["reasoning_content"]=="Thinking. "


def test_length_finish_never_exposes_truncated_tool_call():
    assembler=StreamAssembler()
    assembler.feed({"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"name":"read_file","arguments":"{\\\"path\\\":"}}]},"finish_reason":"length"}]})
    with pytest.raises(ModelError,match="output-token limit"):
        assembler.finish()


@pytest.mark.asyncio
async def test_model_request_does_not_impose_max_tokens():
    captured={}
    async def handle(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200,headers={"Content-Type":"application/json"},json={
            "id":"response-1",
            "model":"fixture",
            "choices":[{"index":0,"message":{"content":"ok"},"finish_reason":"stop"}]
        })
    client=OpenAICompatibleClient(transport=httpx.MockTransport(handle))
    try:
        profile=ModelProfile(model="fixture")
        turn=await client.stream(profile,[{"role":"user","content":"hello"}],[])
        assert turn.message["content"]=="ok"
        assert "max_tokens" not in captured
        assert "max_completion_tokens" not in captured
    finally:
        await client.close()
