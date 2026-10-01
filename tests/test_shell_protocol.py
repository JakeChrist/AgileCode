import asyncio
import json
from pathlib import Path
import pytest
import httpx
from agilecode.adapters.workspace import Workspace
from agilecode.adapters.shell import PersistentShell, shell_environment
from agilecode.adapters.model import OpenAICompatibleClient,StreamAssembler,ModelError
from agilecode.core.types import ModelProfile,AccessDenied


@pytest.mark.asyncio
async def test_persistent_shell_cwd_environment_function_stdin_and_exit(tmp_path):
    work=tmp_path/"work";work.mkdir();(work/"src").mkdir()
    shell=PersistentShell(Workspace(work,work,tmp_path/"global"))
    try:
        await shell.execute("export PERSISTENT_VALUE=hello")
        await shell.execute("cd src")
        assert (await shell.execute("printf '%s:%s' \"$PERSISTENT_VALUE\" \"$PWD\"")).output==f"hello:{work}/src"
        await shell.execute("greet() { printf 'persisted'; }")
        assert (await shell.execute("greet")).output=="persisted"
        assert (await shell.execute("false")).exit_code==1
        command=asyncio.create_task(shell.execute("read -r value; printf '%s' \"$value\""))
        for _ in range(100):
            if shell.current:break
            await asyncio.sleep(.005)
        await shell.send_input("interactive answer\n")
        assert (await asyncio.wait_for(command,3)).output=="interactive answer"
        with pytest.raises(AccessDenied):await shell.execute("cd ../..")
    finally:await shell.close()


@pytest.mark.asyncio
async def test_shell_cancellation_resets_state_and_stops_owned_group(tmp_path):
    shell=PersistentShell(Workspace(tmp_path,tmp_path,tmp_path))
    try:
        await shell.execute("export TEMP_VALUE=before")
        task=asyncio.create_task(shell.execute("sleep 60"))
        await asyncio.sleep(.1);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert shell.process is None or shell.process.returncode is not None
        assert (await shell.execute("printf '%s' \"$TEMP_VALUE\"")).output==""
    finally:await shell.close()


def test_command_guard_is_containment_only(tmp_path):
    from agilecode.adapters.guard import CommandGuard

    work=tmp_path/"work";work.mkdir()
    guard=CommandGuard(Workspace(work,work,tmp_path/"global"))

    assert guard.validate("python -c \"print('ok')\"", work).decision=="ok"
    assert guard.validate("for x in a b; do echo $x; done", work).decision=="ok"
    assert guard.validate("cd ..", work).decision=="block"
    assert guard.validate("printf x > ../outside.txt", work).decision=="block"


def test_shell_does_not_inherit_endpoint_credentials(monkeypatch):
    monkeypatch.setenv("LOCAL_API_KEY","sensitive");monkeypatch.setenv("GIT_WORK_TREE","/wrong")
    env=shell_environment();assert "LOCAL_API_KEY" not in env and "GIT_WORK_TREE" not in env


def test_stream_assembler_interleaved_calls_and_reasoning():
    stream=StreamAssembler()
    chunks=[
        {"choices":[{"delta":{"reasoning_content":"Visible returned reasoning","tool_calls":[{"index":0,"id":"a","function":{"name":"read_","arguments":"{\"path\":"}},{"index":1,"id":"b","function":{"name":"git_command","arguments":"{"}}]}}]},
        {"choices":[{"delta":{"tool_calls":[{"index":1,"function":{"arguments":"}"}},{"index":0,"function":{"name":"file","arguments":"\"app.py\"}"}}]}}]},
        {"model":"local","choices":[{"delta":{},"finish_reason":"tool_calls"}]},
        {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":8}}
    ]
    for chunk in chunks:stream.feed(chunk)
    result=stream.finish()
    assert result.message["tool_calls"][0]["function"]["name"]=="read_file"
    assert json.loads(result.message["tool_calls"][0]["function"]["arguments"])["path"]=="app.py"
    assert result.message["reasoning_content"]=="Visible returned reasoning"
    assert result.usage["prompt_tokens"]==5


@pytest.mark.parametrize("finish",[None,"length","content_filter"])
def test_incomplete_or_filtered_stream_never_yields_executable_turn(finish):
    assembler=StreamAssembler();assembler.feed({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call","function":{"name":"write_file","arguments":"{}"}}]},"finish_reason":finish}]})
    with pytest.raises(ModelError):assembler.finish()


@pytest.mark.asyncio
async def test_openai_compatible_sse_preserves_base_prefix_and_unknown_usage():
    requests=[]
    async def handle(request):
        requests.append(request)
        if request.method=="GET":return httpx.Response(200,json={"data":[{"id":"loaded-model"}]})
        data={"choices":[{"delta":{"content":"Hello"},"finish_reason":"stop"}],"model":"served"}
        return httpx.Response(200,headers={"Content-Type":"text/event-stream"},text="data: "+json.dumps(data)+"\n\ndata: [DONE]\n\n")
    client=OpenAICompatibleClient(httpx.MockTransport(handle))
    try:
        profile=await client.resolve(ModelProfile(base_url="http://localhost:9999/prefix/v1"))
        assert profile.model=="loaded-model"
        events=[];turn=await client.stream(profile,[{"role":"user","content":"hi"}],[],lambda *event:events.append(event))
        assert turn.message["content"]=="Hello" and turn.usage is None
        assert requests[-1].url.path=="/prefix/v1/chat/completions"
        assert events==[("text","Hello")]
    finally:await client.close()


@pytest.mark.asyncio
async def test_multiple_models_require_explicit_selection():
    client=OpenAICompatibleClient(httpx.MockTransport(lambda request:httpx.Response(200,json={"data":[{"id":"a"},{"id":"b"}]})))
    try:
        with pytest.raises(ModelError):await client.resolve(ModelProfile())
        assert (await client.resolve(ModelProfile(model="b"))).model=="b"
    finally:await client.close()


@pytest.mark.asyncio
async def test_model_client_does_not_serialize_requests():
    active=0;maximum=0
    async def handle(request):
        nonlocal active,maximum
        active+=1;maximum=max(maximum,active);await asyncio.sleep(.05);active-=1
        return httpx.Response(200,json={"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]})
    client=OpenAICompatibleClient(httpx.MockTransport(handle));profile=ModelProfile(model="local")
    try:
        await asyncio.gather(*(client.stream(profile,[{"role":"user","content":"hi"}],[]) for _ in range(4)))
        assert maximum==4
    finally:await client.close()


@pytest.mark.asyncio
async def test_model_error_redacts_configured_key(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY","secret_value")
    client=OpenAICompatibleClient(httpx.MockTransport(lambda r:httpx.Response(400,text="bad secret_value")))
    try:
        with pytest.raises(ModelError) as exc:await client.stream(ModelProfile(model="m",api_key_env="TEST_MODEL_KEY"),[],[])
        assert "secret_value" not in str(exc.value) and "[REDACTED]" in str(exc.value)
    finally:await client.close()
