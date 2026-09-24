"""A single OpenAI-compatible Chat Completions client for local servers.

No provider registry, model loading, or request-concurrency scheduler is present.
"""
from __future__ import annotations
import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable
import httpx
from agilecode.core.types import AgileCodeError, ModelProfile


class ModelError(AgileCodeError):
    code = "model_error"


@dataclass
class Turn:
    message: dict[str, Any]
    model: str | None = None
    usage: dict | None = None
    finish_reason: str | None = None
    response_id: str | None = None


class StreamAssembler:
    """One instance per request. A complete terminal response is required to act."""
    def __init__(self):
        self.text = ""
        self.reasoning = ""
        self.calls: dict[int, dict] = {}
        self.finish_reason: str | None = None
        self.usage = None
        self.model = None
        self.response_id = None
        self.bytes = 0

    def feed(self, chunk: dict) -> list[tuple[str, Any]]:
        if "error" in chunk:
            raise ModelError(str(chunk["error"]))
        self.model = chunk.get("model") or self.model
        self.response_id = chunk.get("id") or self.response_id
        if chunk.get("usage") is not None:
            self.usage = chunk["usage"]
        events = []
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta", choice.get("message", {})) or {}
            content = delta.get("content")
            if content:
                if not isinstance(content, str):
                    raise ModelError("This release expects textual assistant content.")
                self.text += content; events.append(("text", content))
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                self.reasoning += reasoning; events.append(("reasoning", reasoning))
            for fragment in delta.get("tool_calls") or []:
                index = fragment.get("index", len(self.calls))
                if not isinstance(index, int) or index < 0 or index > 128:
                    raise ModelError("Invalid tool-call stream index.")
                call = self.calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if fragment.get("id"):
                    if call["id"] and call["id"] != fragment["id"]:
                        raise ModelError("Tool call changed identity during streaming.")
                    call["id"] = fragment["id"]
                function = fragment.get("function") or {}
                if function.get("name"):
                    name = function["name"]
                    if call["function"]["name"] != name:
                        call["function"]["name"] += name
                if function.get("arguments"):
                    args = function["arguments"]
                    call["function"]["arguments"] += args if isinstance(args, str) else json.dumps(args)
                events.append(("tool_delta", {"index": index, **fragment}))
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
        self.bytes += len(json.dumps(chunk).encode())
        if self.bytes > 8 * 1024 * 1024:
            raise ModelError("Model response exceeds the 8 MiB stream limit.")
        return events

    def finish(self) -> Turn:
        calls = [self.calls[i] for i in sorted(self.calls)]
        if self.finish_reason == "length":
            # Hitting the configured output-token limit is a valid terminal
            # condition for ordinary text/reasoning. A truncated tool call is
            # different: its arguments may be incomplete, so never expose it
            # for execution.
            if calls:
                raise ModelError("Response reached the output-token limit while forming a tool call. The incomplete tool call was discarded and no tools were executed.")
            message: dict[str, Any] = {"role": "assistant", "content": self.text or None}
            if self.reasoning:
                message["reasoning_content"] = self.reasoning
            return Turn(message, self.model, self.usage, self.finish_reason, self.response_id)
        if self.finish_reason not in {"stop", "tool_calls", "function_call"}:
            raise ModelError(f"Response did not finish normally ({self.finish_reason or 'stream ended early'}). No tools executed.")
        ids = [c["id"] for c in calls]
        if len(ids) != len(set(ids)) or any(not c["id"] or not c["function"]["name"] for c in calls):
            raise ModelError("Missing or duplicate tool-call identity.")
        # Argument JSON is validated by ToolExecutor, which can return a correlated
        # error to the model instead of converting malformed prose into an action.
        message: dict[str, Any] = {"role": "assistant", "content": self.text or None}
        if calls:
            message["tool_calls"] = calls
        if self.reasoning:
            message["reasoning_content"] = self.reasoning
        return Turn(message, self.model, self.usage, self.finish_reason, self.response_id)


class OpenAICompatibleClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.http = httpx.AsyncClient(transport=transport, follow_redirects=False,
                                      limits=httpx.Limits(max_connections=None, max_keepalive_connections=20))

    @staticmethod
    def headers(profile: ModelProfile) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if profile.api_key_env:
            value = os.getenv(profile.api_key_env)
            if not value:
                raise ModelError(f"Credential environment variable {profile.api_key_env!r} is not set.")
            headers["Authorization"] = f"Bearer {value}"
        return headers

    async def models(self, profile: ModelProfile) -> list[str]:
        try:
            response = await self.http.get(profile.base_url.rstrip("/") + "/models", headers=self.headers(profile), timeout=15)
            response.raise_for_status()
            return [item["id"] for item in response.json().get("data", []) if isinstance(item.get("id"), str)]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise ModelError(f"Cannot list models at {profile.base_url}. Check that the local server is running. {type(exc).__name__}") from exc

    async def resolve(self, profile: ModelProfile) -> ModelProfile:
        if profile.model:
            return profile.model_copy(deep=True)
        available = await self.models(profile)
        if len(available) != 1:
            raise ModelError("Select a model in Settings; the endpoint listed " + str(len(available)) + " models. AgileCode does not load or guess between models.")
        return profile.model_copy(update={"model": available[0]}, deep=True)

    async def stream(self, profile: ModelProfile, messages: list[dict], tools: list[dict], on_delta: Callable[[str, Any], None] | None = None) -> Turn:
        # Remove locally recorded reasoning unless this particular endpoint needs it
        # round-tripped. This is a user-selected protocol option, not an inferred model flag.
        wire = []
        for message in messages:
            item = dict(message)
            if not profile.round_trip_reasoning:
                item.pop("reasoning_content", None)
            wire.append(item)
        payload = {"model": profile.model, "messages": wire, "stream": True, "max_tokens": profile.max_tokens, **profile.request_options}
        if tools:
            payload["tools"] = tools
        url = profile.base_url.rstrip("/") + "/chat/completions"
        for attempt in range(3):
            assembler = StreamAssembler()
            received = False
            try:
                async with self.http.stream("POST", url, headers=self.headers(profile), json=payload,
                                            timeout=httpx.Timeout(profile.timeout_seconds, connect=15)) as response:
                    if response.status_code in {429, 502, 503, 504} and attempt < 2:
                        retry = response.headers.get("Retry-After", "")
                        delay = min(float(retry), 30) if retry.replace('.', '', 1).isdigit() else (2 ** attempt)
                        await response.aread()
                        await asyncio.sleep(delay)
                        continue
                    if response.status_code >= 400:
                        raw = (await response.aread()).decode(errors="replace")[:2000]
                        if profile.api_key_env and os.getenv(profile.api_key_env):
                            raw = raw.replace(os.environ[profile.api_key_env], "[REDACTED]")
                        raise ModelError(f"HTTP {response.status_code}: {raw}")
                    content_type = response.headers.get("Content-Type", "")
                    if "application/json" in content_type:
                        raw = await response.aread()
                        if len(raw) > 8 * 1024 * 1024:
                            raise ModelError("Oversized JSON model response.")
                        received = True
                        chunks = assembler.feed(json.loads(raw))
                        for kind, value in chunks:
                            if on_delta:
                                on_delta(kind, value)
                        return assembler.finish()
                    data_lines = []
                    async for line in response.aiter_lines():
                        if len(line) > 2 * 1024 * 1024:
                            raise ModelError("Oversized SSE line.")
                        if line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                            continue
                        if line == "" and data_lines:
                            event = "\n".join(data_lines); data_lines = []
                            if event == "[DONE]":
                                break
                            received = True
                            for kind, value in assembler.feed(json.loads(event)):
                                if on_delta:
                                    on_delta(kind, value)
                    if data_lines and "\n".join(data_lines) != "[DONE]":
                        received = True
                        for kind, value in assembler.feed(json.loads("\n".join(data_lines))):
                            if on_delta:
                                on_delta(kind, value)
                    return assembler.finish()
            except (httpx.TransportError, asyncio.TimeoutError) as exc:
                if received or attempt == 2:
                    raise ModelError(f"Model connection interrupted ({type(exc).__name__}). Partial output did not execute any tool.") from exc
                await asyncio.sleep(2 ** attempt)
            except (ValueError, KeyError, TypeError) as exc:
                raise ModelError(f"Invalid compatible response: {exc}") from exc
        raise ModelError("The endpoint did not accept the request after bounded retries.")

    async def close(self):
        await self.http.aclose()
