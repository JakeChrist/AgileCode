"""SDK request/normalization contracts, not assertions about model intelligence.

The local TCP interruption/timeout tests cover actual network behavior. These
controlled transports isolate wire serialization, credentials, and retry policy
while exercising the real AsyncOpenAI SDK and its SSE decoder.
"""
import asyncio
import json
import traceback
from types import SimpleNamespace

import httpx
import pytest

from agilecode.adapters.model import ContextWindowChanged, ModelError, ModelUnavailable, OpenAICompatibleClient
from agilecode.core.types import ModelProfile


def completion():
    return httpx.Response(200, json={
        "choices": [{"message": {"content": "protocol fixture"}, "finish_reason": "stop"}],
    })


def sse(chunks):
    return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=
        ": SDK comment\r\n\r\n" + "".join("data: " + json.dumps(chunk) + "\r\n\r\n" for chunk in chunks)
        + "data: [DONE]\r\n\r\n")


@pytest.mark.parametrize("round_trip", [False, True])
async def test_sdk_preserves_provider_parameters_reasoning_and_tools(round_trip):
    requests = []

    def handle(request):
        requests.append(request)
        return sse([
            {"id": "response-1", "model": "served-model", "choices": [{"delta": {
                "reasoning_content": "Returned reasoning. ",
                "content": "Visible text. ",
                "tool_calls": [{"index": 0, "id": "call-1", "type": "function", "function": {
                    "name": "read_file", "arguments": '{"path":',
                }}],
            }}]},
            {"choices": [{"delta": {"reasoning": "More reasoning.", "tool_calls": [
                {"index": 0, "function": {"arguments": '"README.md"}'}},
            ]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 8, "vendor_tokens": 4}},
        ])

    client = OpenAICompatibleClient(httpx.MockTransport(handle))
    options = {"temperature": 0.4, "top_k": 20, "chat_template_kwargs": {"enable_thinking": True}}
    profile = ModelProfile(base_url="http://fixture/prefix/v1/", model="selected",
                           round_trip_reasoning=round_trip, request_options=options)
    messages = [{"role": "assistant", "content": "Earlier", "reasoning_content": "Private history"}]
    tool_specs = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]
    events = []
    try:
        turn = await client.stream(profile, messages, tool_specs, lambda *event: events.append(event))
    finally:
        await client.close()
    request = requests[0]
    assert request.url.path == "/prefix/v1/chat/completions"
    assert request.headers["User-Agent"].startswith("AsyncOpenAI/Python ")
    payload = json.loads(request.content)
    assert payload["model"] == "selected" and payload["stream"] is True
    assert payload["tools"] == tool_specs
    assert all(payload[key] == value for key, value in options.items())
    assert ("reasoning_content" in payload["messages"][0]) is round_trip
    assert messages[0]["reasoning_content"] == "Private history"
    assert "max_tokens" not in payload and "max_completion_tokens" not in payload
    assert turn.message["tool_calls"][0]["function"]["arguments"] == '{"path":"README.md"}'
    assert turn.message["reasoning_content"] == "Returned reasoning. More reasoning."
    assert turn.model == "served-model" and turn.response_id == "response-1"
    assert turn.usage["vendor_tokens"] == 4
    assert [kind for kind, _ in events] == ["text", "reasoning", "tool_delta", "reasoning", "tool_delta"]


async def test_sdk_credentials_are_endpoint_specific_without_ambient_headers(monkeypatch):
    for name, value in {
        "OPENAI_API_KEY": "ambient-key",
        "OPENAI_ADMIN_KEY": "ambient-admin",
        "OPENAI_ORG_ID": "ambient-org",
        "OPENAI_PROJECT_ID": "ambient-project",
        "OPENAI_BASE_URL": "https://unrelated.example/v1",
        "OPENAI_CUSTOM_HEADERS": "authorization: Bearer ambient-header\nx-api-key: ambient-extra\nX-Private: private",
        "PROVIDER_A_KEY": "provider-a",
        "PROVIDER_B_KEY": "provider-b",
    }.items():
        monkeypatch.setenv(name, value)
    requests = []
    entered = 0
    all_entered = asyncio.Event()

    async def handle(request):
        nonlocal entered
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "fixture", "context_length": 4096}]})
        entered += 1
        if entered == 3:
            all_entered.set()
        await asyncio.wait_for(all_entered.wait(), 2)
        return completion()

    client = OpenAICompatibleClient(httpx.MockTransport(handle))
    profiles = [ModelProfile(base_url=f"http://{host}/v1", model="fixture", api_key_env=key)
                for host, key in [("a", "PROVIDER_A_KEY"), ("b", "PROVIDER_B_KEY"), ("local", None)]]
    try:
        await asyncio.gather(*(client.stream(profile, [], []) for profile in profiles))
        for profile in profiles:
            assert await client.models(profile) == ["fixture"]
            assert await client.context_window(profile) == 4096
    finally:
        await client.close()
    assert len(requests) == 9
    for request in requests:
        expected = {"a": "Bearer provider-a", "b": "Bearer provider-b", "local": None}[request.url.host]
        assert request.headers.get("Authorization") == expected
        assert "OpenAI-Organization" not in request.headers and "OpenAI-Project" not in request.headers
        assert "x-api-key" not in request.headers and "X-Private" not in request.headers
        assert "ambient" not in str(request.headers) and "unused-local-key" not in str(request.headers)


async def test_missing_selected_credential_never_falls_back_to_ambient(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    monkeypatch.delenv("MISSING_PROVIDER_KEY", raising=False)
    requests = []
    client = OpenAICompatibleClient(httpx.MockTransport(lambda request: requests.append(request)))
    try:
        with pytest.raises(ModelError, match="MISSING_PROVIDER_KEY"):
            await client.stream(ModelProfile(model="fixture", api_key_env="MISSING_PROVIDER_KEY"), [], [])
        assert requests == []
    finally:
        await client.close()


@pytest.mark.parametrize("failure", ["status", "connection"])
async def test_adapter_reports_one_physical_failure_to_shared_retry_owner(failure):
    requests = []

    def handle(request):
        requests.append(request)
        if failure == "connection":
            raise httpx.ConnectError("controlled transport failure", request=request)
        return httpx.Response(503, json={"error": "controlled status"})

    client = OpenAICompatibleClient(httpx.MockTransport(handle))
    try:
        with pytest.raises(ModelUnavailable, match="HTTP 503" if failure == "status" else "ConnectError"):
            await client.stream(ModelProfile(model="fixture"), [], [])
    finally:
        await client.close()
    assert len(requests) == 1


@pytest.mark.parametrize("delivery", ["sse", "json", "status"])
async def test_lm_studio_worker_crash_is_recoverable(delivery):
    message = "The model has crashed without additional information. (Exit code: null)"
    seen = []

    def handle(request):
        seen.append(request)
        if delivery == "sse":
            return sse([{"error": {"message": message}}])
        if delivery == "json":
            return httpx.Response(200, headers={"Content-Type": "application/json"},
                                  json={"error": {"message": message}})
        return httpx.Response(400, json={"error": {"message": message}})

    client = OpenAICompatibleClient(httpx.MockTransport(handle))
    try:
        with pytest.raises(ModelUnavailable, match="model has crashed"):
            await client.stream(ModelProfile(model="fixture"), [], [])
    finally:
        await client.close()
    assert len(seen) == 1


async def test_oversized_request_http_413_requests_context_correction():
    client = OpenAICompatibleClient(httpx.MockTransport(
        lambda request: httpx.Response(413, json={"error": "Prompt exceeded available space"})))
    try:
        with pytest.raises(ContextWindowChanged, match="HTTP 413"):
            await client.stream(ModelProfile(model="fixture"), [], [])
    finally:
        await client.close()


@pytest.mark.parametrize("delivery", ["sse", "json", "status"])
async def test_lm_studio_context_size_exceeded_requests_context_correction(delivery):
    error = {"error": {"message": "Context size has been exceeded."}}

    def handle(request):
        if delivery == "sse":
            return sse([error])
        if delivery == "json":
            return httpx.Response(200, headers={"Content-Type": "application/json"}, json=error)
        return httpx.Response(400, json=error)

    client = OpenAICompatibleClient(httpx.MockTransport(handle))
    try:
        with pytest.raises(ContextWindowChanged, match="Context size has been exceeded"):
            await client.stream(ModelProfile(model="fixture"), [], [])
    finally:
        await client.close()


async def test_sdk_stream_error_preserves_message_and_redacts_selected_key(monkeypatch):
    monkeypatch.setenv("PROVIDER_KEY", "secret-in-error")
    client = OpenAICompatibleClient(httpx.MockTransport(lambda request: sse([
        {"error": {"message": "Provider failed with secret-in-error", "type": "provider_failure"}},
    ])))
    try:
        with pytest.raises(ModelError, match="Provider failed with ") as result:
            await client.stream(ModelProfile(model="fixture", api_key_env="PROVIDER_KEY"), [], [])
        assert "secret-in-error" not in str(result.value)
        assert "[REDACTED]" in str(result.value)
    finally:
        await client.close()


@pytest.mark.parametrize("ending", [None, "length", "content_filter"])
async def test_sdk_rejects_partial_tool_turn_even_with_done(ending):
    requests = []

    def handle(request):
        requests.append(request)
        return sse([{"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "partial-1", "function": {"name": "write_file", "arguments": '{"path":'},
        }]}, "finish_reason": ending}]}])

    client = OpenAICompatibleClient(httpx.MockTransport(handle))
    try:
        if ending == "length":
            turn = await client.stream(ModelProfile(model="fixture"), [], [])
            assert turn.finish_reason == "length"
            assert not turn.message.get("tool_calls")
        else:
            with pytest.raises(ModelError):
                await client.stream(ModelProfile(model="fixture"), [], [])
        assert len(requests) == 1
    finally:
        await client.close()


@pytest.mark.parametrize("kind", ["http", "sse", "json", "transient", "context", "models"])
async def test_provider_error_traceback_cannot_echo_configured_credential(monkeypatch, kind):
    monkeypatch.setenv("PROVIDER_KEY", "secret-in-error")
    def handle(request):
        error = {"error": {"message": "context window secret-in-error" if kind == "context" else "secret-in-error"}}
        if kind == "sse":
            return sse([error])
        return httpx.Response(503 if kind == "transient" else 200 if kind == "json" else 400, json=error)
    client = OpenAICompatibleClient(httpx.MockTransport(handle))
    client.operational_settings = lambda: SimpleNamespace(
        model_transport_retry_attempts=1, model_transport_retry_delay_cap_seconds=0,
    )
    profile = ModelProfile(model="fixture", api_key_env="PROVIDER_KEY")
    try:
        try:
            if kind == "models":
                await client.models(profile)
            else:
                await client.stream(profile, [], [])
        except ModelError:
            formatted = traceback.format_exc()
            assert "secret-in-error" not in formatted
        else:
            pytest.fail("Expected a model protocol error")
    finally:
        await client.close()
