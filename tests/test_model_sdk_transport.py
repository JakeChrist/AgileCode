"""Real SDK + HTTPX TCP regressions against a controlled loopback HTTP server.

These verify transport/protocol behavior, not LM Studio or model inference.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import gc
import json
from types import SimpleNamespace
import traceback

import httpx
import pytest

from agilecode.adapters.model import ModelError, ModelUnavailable, OpenAICompatibleClient
from agilecode.core.types import ModelProfile


def _event(delta=None, finish=None, *, error=None):
    payload = {"error": error} if error else {"id": "fixture-response", "model": "fixture", "choices": [
        {"index": 0, "delta": delta or {}, "finish_reason": finish},
    ]}
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


async def _headers(writer, status=200):
    writer.write(f"HTTP/1.1 {status} Fixture\r\nContent-Type: text/event-stream\r\n"
                 "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()


async def _chunk(writer, body):
    writer.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
    await writer.drain()


async def _finish(writer):
    await _chunk(writer, b"data: [DONE]\n\n")
    writer.write(b"0\r\n\r\n")
    await writer.drain()


async def _json(writer, body, status=200):
    data = json.dumps(body).encode()
    writer.write(f"HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\n"
                 f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
    await writer.drain()


class _Server:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        self.tasks = set()
        self.errors = []

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/v1"
        return self

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode().split("\r\n")
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.lower()] = value.strip()
            body = await reader.readexactly(int(headers.get("content-length", 0)))
            request = {"line": lines[0], "headers": headers, "body": json.loads(body) if body else None}
            self.requests.append(request)
            await self.handler(request, reader, writer)
        except (asyncio.CancelledError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:
            self.errors.append(exc)
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self.tasks.discard(task)

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert not self.errors, self.errors


def _client():
    # A real network transport restricted by the fixture URL to loopback. Do not
    # send the fixture through an ambient developer/CI proxy.
    client = OpenAICompatibleClient(transport=httpx.AsyncHTTPTransport(trust_env=False))
    client.operational_settings = lambda: SimpleNamespace(
        model_transport_retry_attempts=3, model_transport_retry_delay_cap_seconds=0,
    )
    return client


def _partial_tool():
    return {"tool_calls": [{"index": 0, "id": "partial-call", "type": "function",
                            "function": {"name": "write_file", "arguments": '{"path":'}}]}


@pytest.mark.parametrize("phase", ["headers", "reasoning", "tools", "error_body"])
async def test_saved_numeric_timeout_never_deadlines_generation_and_cancel_closes_socket(phase):
    ready = asyncio.Event()
    closed = asyncio.Event()

    async def handle(request, reader, writer):
        if phase != "headers":
            await _headers(writer, 503 if phase == "error_body" else 200)
            if phase == "reasoning":
                await _chunk(writer, _event({"reasoning_content": "Still thinking"}))
            elif phase == "tools":
                await _chunk(writer, _event(_partial_tool()))
            else:
                await _chunk(writer, b'{"error":')
        ready.set()
        if await reader.read() == b"":
            closed.set()

    async with _Server(handle) as server:
        client = _client()
        # Simulate a serialized legacy/resumed profile rather than relying on a
        # new profile's default None timeout.
        profile = ModelProfile.model_validate({"base_url": server.url, "model": "fixture", "timeout_seconds": 0.05})
        events = []
        task = asyncio.create_task(client.stream(profile, [], [], lambda *event: events.append(event)))
        try:
            await asyncio.wait_for(ready.wait(), 2)
            await asyncio.sleep(0.2)
            assert not task.done(), "A saved numeric timeout still interrupted generation"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            # EOF must precede client.close(); otherwise cancellation leaked an
            # in-flight connection and the assertion could be a cleanup artifact.
            await asyncio.wait_for(closed.wait(), 1)
            assert len(server.requests) == 1
            assert [kind for kind, _ in events] == {
                "headers": [], "reasoning": ["reasoning"], "tools": ["tool_delta"], "error_body": [],
            }[phase]
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.close()


@pytest.mark.parametrize("pause", ["headers", "between_chunks"])
async def test_generation_resumes_normally_after_silence_exceeds_saved_timeout(pause):
    async def handle(request, reader, writer):
        if pause == "headers":
            await asyncio.sleep(0.2)
        await _headers(writer)
        await _chunk(writer, _event({"reasoning": "Thinking"}))
        if pause == "between_chunks":
            await asyncio.sleep(0.2)
        await _chunk(writer, _event({"content": "Completed"}, "stop"))
        await _finish(writer)

    async with _Server(handle) as server:
        client = _client()
        try:
            profile = ModelProfile(base_url=server.url, model="fixture", timeout_seconds=0.05)
            turn = await asyncio.wait_for(client.stream(profile, [], []), 2)
            assert turn.message == {"role": "assistant", "content": "Completed", "reasoning_content": "Thinking"}
            assert len(server.requests) == 1
        finally:
            await client.close()


@pytest.mark.parametrize("output", [None, "text", "reasoning", "tools"])
async def test_real_socket_drop_is_reported_for_shared_retry(output):
    async def handle(request, reader, writer):
        if output is not None:
            await _headers(writer)
            delta = {"text": {"content": "Partial"}, "reasoning": {"reasoning_content": "Thinking"},
                     "tools": _partial_tool()}[output]
            await _chunk(writer, _event(delta))
        writer.transport.abort()

    async with _Server(handle) as server:
        client = _client()
        events = []
        try:
            with pytest.raises(ModelUnavailable, match="RemoteProtocolError"):
                await asyncio.wait_for(client.stream(ModelProfile(base_url=server.url, model="fixture"), [], [],
                                                     lambda *event: events.append(event)), 2)
            assert len(server.requests) == 1
            assert bool(events) is (output is not None)
        finally:
            await client.close()


@pytest.mark.parametrize("failure", ["http", "transient", "sse", "models"])
async def test_provider_key_is_redacted_from_real_sdk_exception_traceback(monkeypatch, failure):
    secret = "fixture-secret-for-error-redaction"
    monkeypatch.setenv("FIXTURE_KEY", secret)

    async def handle(request, reader, writer):
        error = {"message": f"Provider echoed {secret}"}
        if failure == "sse":
            await _headers(writer)
            await _chunk(writer, _event(error=error))
            await _finish(writer)
        else:
            await _json(writer, {"error": error}, 503 if failure == "transient" else 400)

    async with _Server(handle) as server:
        client = _client()
        profile = ModelProfile(base_url=server.url, model="fixture", api_key_env="FIXTURE_KEY")
        try:
            try:
                if failure == "models":
                    await client.models(profile)
                else:
                    await client.stream(profile, [], [])
            except ModelError as exc:
                assert secret not in str(exc)
                # ExecutionInterrupted stores formatted tracebacks, not only the
                # final exception message. SDK causes may contain provider body.
                assert secret not in traceback.format_exc()
            else:
                pytest.fail("Expected an actionable model error")
        finally:
            await client.close()


async def test_shared_http_pool_survives_request_wrapper_gc_and_keeps_auth_isolated(monkeypatch):
    for key, value in {
        "OPENAI_API_KEY": "ambient-key", "OPENAI_ADMIN_KEY": "ambient-admin",
        "OPENAI_ORG_ID": "ambient-org", "OPENAI_PROJECT_ID": "ambient-project",
        "OPENAI_BASE_URL": "http://127.0.0.1:1/not-selected",
        "OPENAI_CUSTOM_HEADERS": "aUtHoRiZaTiOn: Bearer ambient-header\nopenai-project: ambient-project-header\n"
                                 "OPENAI-ORGANIZATION: ambient-org-header\nx-api-key: ambient-extra",
        "FIXTURE_A": "selected-a", "FIXTURE_B": "selected-b",
    }.items():
        monkeypatch.setenv(key, value)
    entered = 0
    all_entered = asyncio.Event()

    async def handle(request, reader, writer):
        nonlocal entered
        if request["body"] is None:
            await _json(writer, {"data": [{"id": "fixture", "context_length": 4096}]})
            return
        entered += 1
        if entered == 3:
            all_entered.set()
        await asyncio.wait_for(all_entered.wait(), 2)
        await _headers(writer)
        await _chunk(writer, _event({"content": "ok"}, "stop"))
        await _finish(writer)

    async with _Server(handle) as server:
        client = _client()
        profiles = [ModelProfile(base_url=server.url, model=model, api_key_env=key)
                    for model, key in [("a", "FIXTURE_A"), ("b", "FIXTURE_B"), ("local", None)]]
        try:
            turns = await asyncio.gather(*(client.stream(profile, [], []) for profile in profiles))
            assert all(turn.message["content"] == "ok" for turn in turns)
            gc.collect()
            await asyncio.sleep(0)
            assert not client.http.is_closed
            for profile in profiles:
                assert await client.models(profile) == ["fixture"]
            expected = {"a": "Bearer selected-a", "b": "Bearer selected-b", "local": None}
            for request in server.requests[:3]:
                assert request["headers"].get("authorization") == expected[request["body"]["model"]]
            assert [request["headers"].get("authorization") for request in server.requests[3:]] == list(expected.values())
            for request in server.requests:
                headers = request["headers"]
                assert "openai-project" not in headers and "openai-organization" not in headers
                assert "x-api-key" not in headers
                assert "ambient" not in str(headers) and "unused-local-key" not in str(headers)
        finally:
            await client.close()
