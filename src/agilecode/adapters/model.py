"""One domain adapter over the official OpenAI Chat Completions SDK.

No provider registry, model loading, or request-concurrency scheduler is present.
"""
from __future__ import annotations
import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote
import httpx
from openai import AsyncOpenAI, APIConnectionError, APIError, APIStatusError, Omit
from agilecode.core.types import AgileCodeError, ModelProfile


class ModelError(AgileCodeError):
    code = "model_error"


class ModelUnavailable(ModelError):
    """The provider is temporarily unreachable or interrupted; no tool was dispatched."""

    code = "model_unavailable"


class ContextWindowChanged(ModelError):
    """The provider rejected a request because the active context capacity changed."""

    code = "context_window_changed"


CONTEXT_MARKERS = (
    "context length", "context window", "maximum context", "max context",
    "prompt is too long", "too many tokens", "n_ctx", "exceeds the context",
)
WORKER_FAILURE_MARKERS = (
    "model has crashed", "model crashed", "model worker crashed",
    "worker has crashed", "inference worker crashed", "inference failed",
    "inference process crashed", "worker process exited",
    "model process crashed",
)


def provider_failure(value: Any, prefix: str = "Model stream error") -> ModelError:
    """Classify reported inference failures without treating invalid requests as transient."""
    raw = value if isinstance(value, str) else json.dumps(value, default=str)
    lowered = raw.lower()
    if any(marker in lowered for marker in CONTEXT_MARKERS):
        return ContextWindowChanged(f"{prefix}: {raw}")
    if any(marker in lowered for marker in WORKER_FAILURE_MARKERS):
        return ModelUnavailable(f"{prefix}: {raw}")
    return ModelError(f"{prefix}: {raw}")


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
        self._text_chunks: list[str] = []
        self._reasoning_chunks: list[str] = []
        self.calls: dict[int, dict] = {}
        self.finish_reason: str | None = None
        self.usage = None
        self.model = None
        self.response_id = None

    @property
    def text(self) -> str:
        return "".join(self._text_chunks)

    @property
    def reasoning(self) -> str:
        return "".join(self._reasoning_chunks)

    def feed(self, chunk: dict) -> list[tuple[str, Any]]:
        if "error" in chunk:
            raise provider_failure(chunk["error"])
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
                self._text_chunks.append(content); events.append(("text", content))
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                self._reasoning_chunks.append(reasoning); events.append(("reasoning", reasoning))
            for fragment in delta.get("tool_calls") or []:
                index = fragment.get("index", len(self.calls))
                if not isinstance(index, int) or index < 0:
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
        return events

    def finish(self) -> Turn:
        calls = [self.calls[i] for i in sorted(self.calls)]
        if self.finish_reason == "length":
            # Preserve partial text/reasoning for continuation but discard all
            # incomplete tool-call fragments. No partial call is executable.
            message: dict[str, Any] = {"role": "assistant", "content": self.text or None}
            if self.reasoning:
                message["reasoning_content"] = self.reasoning
            return Turn(message, self.model, self.usage, self.finish_reason, self.response_id)
        if self.finish_reason is None:
            raise ModelUnavailable("Model stream ended before a terminal response. No tools from this response were executed.")
        if self.finish_reason not in {"stop", "tool_calls", "function_call"}:
            raise ModelError(f"Response did not finish normally ({self.finish_reason}). No tools from this response were executed.")
        ids = [c["id"] for c in calls]
        if len(ids) != len(set(ids)) or any(not c["id"] or not c["function"]["name"] for c in calls):
            raise ModelUnavailable("Missing or duplicate tool-call identity.")
        # Argument JSON is validated by ToolManager, which can return a correlated
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
        # Provider-derived runtime metadata. This is deliberately not part of
        # user-editable model settings: AgileCode observes the loaded model
        # configuration and reuses it for the lifetime of this client session.
        self.operational_settings = None
        self._context_windows: dict[tuple[str, str, str | None], int] = {}
        self._context_window_tasks: dict[tuple[str, str, str | None], asyncio.Task[int]] = {}

    @staticmethod
    def headers(profile: ModelProfile) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if profile.api_key_env:
            value = os.getenv(profile.api_key_env)
            if not value:
                raise ModelError(f"Credential environment variable {profile.api_key_env!r} is not set.")
            headers["Authorization"] = f"Bearer {value}"
        return headers

    def _sdk_client(self, profile: ModelProfile) -> AsyncOpenAI:
        # Clients are request-scoped, but all use this adapter's shared, uncapped
        # HTTP pool. The SDK officially supports supplied HTTPX clients. Never
        # mutate shared default auth when two providers are used concurrently.
        selected_headers = self.headers(profile)
        headers = {
            line.split(":", 1)[0].strip(): Omit()
            for line in os.getenv("OPENAI_CUSTOM_HEADERS", "").splitlines()
            if ":" in line and line.split(":", 1)[0].strip()
        }
        headers.update({
            "Authorization": selected_headers.get("Authorization", Omit()),
            "OpenAI-Organization": Omit(),
            "OpenAI-Project": Omit(),
        })
        return AsyncOpenAI(
            # The SDK requires a credential at construction. The placeholder is
            # never sent: an unauthenticated endpoint explicitly omits auth.
            api_key=(selected_headers.get("Authorization", "")[7:] or "unused-local-key"),
            admin_api_key="",
            organization="",
            project="",
            base_url=profile.base_url.rstrip("/") + "/",
            default_headers=headers,
            http_client=self.http,
            timeout=profile.timeout_seconds,
            max_retries=0,
        )

    async def _model_records(self, profile: ModelProfile) -> list[dict]:
        page = await self._sdk_client(profile).models.list(
            extra_headers={"Authorization": Omit()} if not profile.api_key_env else None,
        )
        return [item.model_dump(exclude_unset=True) for item in page.data]

    @staticmethod
    def _failure_name(exc: BaseException) -> str:
        # Preserve the genuine phase/type (ConnectTimeout, RemoteProtocolError,
        # etc.) rather than hiding it behind the SDK's connection wrapper.
        cause = exc.__cause__ if isinstance(exc, APIConnectionError) else None
        return type(cause or exc).__name__

    @staticmethod
    def _redacted_error(profile: ModelProfile, value: Any) -> str:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if profile.api_key_env and (secret := os.getenv(profile.api_key_env)):
            text = text.replace(secret, "[REDACTED]")
        return text[:2000]

    async def models(self, profile: ModelProfile) -> list[str]:
        try:
            return [item["id"] for item in await self._model_records(profile) if isinstance(item.get("id"), str)]
        except APIConnectionError as exc:
            raise ModelUnavailable(f"Cannot contact model server at {profile.base_url} ({self._failure_name(exc)}).") from None
        except APIStatusError as exc:
            if exc.status_code in {429, 500, 502, 503, 504}:
                raise ModelUnavailable(f"Model server temporarily unavailable (HTTP {exc.status_code}).") from None
            raise ModelError(f"Cannot list models at {profile.base_url} (HTTP {exc.status_code}).") from None
        except (APIError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ModelError(f"Cannot list models at {profile.base_url} ({type(exc).__name__}).") from None

    @staticmethod
    def _provider_root(base_url: str) -> str:
        base = base_url.rstrip("/")
        return base[:-3] if base.endswith("/v1") else base

    @staticmethod
    def _context_value(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1024:
            return None
        return value

    async def _get_json(self, url: str, profile: ModelProfile, availability: list[bool] | None = None) -> dict | None:
        try:
            response = await self.http.get(
                url,
                headers=self.headers(profile),
                timeout=profile.timeout_seconds,
            )
            if availability is not None:
                availability.append(True)
            if response.status_code >= 400:
                return None
            data = response.json()
            return data if isinstance(data, dict) else None
        except httpx.TransportError:
            if availability is not None:
                availability.append(False)
            return None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _context_key(profile: ModelProfile) -> tuple[str, str, str | None]:
        if not profile.model:
            raise ModelError("Resolve a model before requesting its context window.")
        return (profile.base_url.rstrip("/"), profile.model, profile.api_key_env)

    def invalidate_context_window(self, profile: ModelProfile) -> None:
        """Forget observed context metadata after evidence that the loaded instance changed."""
        self._context_windows.pop(self._context_key(profile), None)

    async def _discover_context_window(self, profile: ModelProfile) -> int:
        """Ask the provider for the effective context capacity of the selected loaded model."""
        if not profile.model:
            raise ModelError("Resolve a model before requesting its context window.")

        # Some OpenAI-compatible providers include their effective context length
        # directly in the model record. Use only effective fields, never a model's
        # advertised/training maximum.
        availability: list[bool] = []
        try:
            listing = await self._model_records(profile)
            availability.append(True)
        except APIConnectionError:
            listing = []
            availability.append(False)
        except (APIError, ValueError, KeyError, TypeError, AttributeError):
            listing = []
            availability.append(True)
        if listing:
            for item in listing:
                if not isinstance(item, dict) or item.get("id") != profile.model:
                    continue
                for key in ("context_length", "context_window"):
                    value = self._context_value(item.get(key))
                    if value is not None:
                        return value

        root = self._provider_root(profile.base_url)

        # LM Studio's native API exposes the actual load configuration separately
        # from the OpenAI-compatible model list.
        native = await self._get_json(root + "/api/v1/models", profile, availability)
        if native:
            models = native.get("models", [])
            if isinstance(models, list):
                for model in models:
                    if not isinstance(model, dict):
                        continue
                    instances = model.get("loaded_instances") or []
                    if not isinstance(instances, list):
                        continue
                    for instance in instances:
                        if not isinstance(instance, dict) or instance.get("id") != profile.model:
                            continue
                        value = self._context_value((instance.get("config") or {}).get("context_length"))
                        if value is not None:
                            return value
                for model in models:
                    if not isinstance(model, dict) or model.get("key") != profile.model:
                        continue
                    lengths = {
                        value
                        for instance in (model.get("loaded_instances") or [])
                        if isinstance(instance, dict)
                        for value in [self._context_value((instance.get("config") or {}).get("context_length"))]
                        if value is not None
                    }
                    if len(lengths) == 1:
                        return next(iter(lengths))

        # llama.cpp reports the active server context in /props. In router mode,
        # the model query selects the requested loaded model.
        props = await self._get_json(root + "/props?model=" + quote(profile.model, safe=""), profile, availability)
        if props:
            value = self._context_value(
                (props.get("default_generation_settings") or {}).get("n_ctx")
            )
            if value is not None:
                return value

        if availability and not any(availability):
            raise ModelUnavailable(f"Cannot contact model server at {profile.base_url} to read loaded context metadata.")
        raise ModelError(
            f"The provider did not report the loaded context window for model {profile.model!r}. "
            "AgileCode does not configure or guess model context limits."
        )

    async def context_window(self, profile: ModelProfile, *, refresh: bool = False) -> int:
        """Return provider-derived context metadata, discovering it once per resolved model session."""
        key = self._context_key(profile)
        if refresh:
            self._context_windows.pop(key, None)
        elif key in self._context_windows:
            return self._context_windows[key]

        task = self._context_window_tasks.get(key)
        if task is None:
            task = asyncio.create_task(self._discover_context_window(profile))
            self._context_window_tasks[key] = task
        try:
            value = await task
            self._context_windows[key] = value
            return value
        finally:
            if self._context_window_tasks.get(key) is task:
                self._context_window_tasks.pop(key, None)

    async def resolve(self, profile: ModelProfile) -> ModelProfile:
        if profile.model:
            resolved = profile.model_copy(deep=True)
        else:
            available = await self.models(profile)
            if len(available) != 1:
                raise ModelError("Select a model in Settings; the endpoint listed " + str(len(available)) + " models. AgileCode does not load or guess between models.")
            resolved = profile.model_copy(update={"model": available[0]}, deep=True)
        # Resolve provider-owned metadata at the same boundary as model identity.
        # Subsequent turns use the in-memory value rather than polling metadata endpoints.
        await self.context_window(resolved)
        return resolved

    async def stream(self, profile: ModelProfile, messages: list[dict], tools: list[dict], on_delta: Callable[[str, Any], None] | None = None) -> Turn:
        # Remove locally recorded reasoning unless this particular endpoint needs it
        # round-tripped. This is a user-selected protocol option, not an inferred model flag.
        wire = []
        for message in messages:
            item = dict(message)
            if not profile.round_trip_reasoning:
                item.pop("reasoning_content", None)
            wire.append(item)
        sdk = self._sdk_client(profile)
        # One physical model request per call. The shared conversation engine
        # owns bounded retries so its limits cannot multiply with adapter retries.
        timeout = httpx.Timeout(profile.timeout_seconds, read=None)
        assembler = StreamAssembler()
        received = False
        try:
            async with sdk.chat.completions.with_streaming_response.create(
                model=profile.model,
                messages=wire,
                stream=True,
                tools=tools or Omit(),
                extra_body=profile.request_options,
                extra_headers={"Authorization": Omit()} if not profile.api_key_env else None,
                timeout=timeout,
            ) as response:
                if "application/json" in response.headers.get("Content-Type", ""):
                    completion = await response.json()
                    received = True
                    chunks = assembler.feed(completion)
                    for kind, value in chunks:
                        if on_delta:
                            on_delta(kind, value)
                else:
                    stream = await response.parse()
                    async for chunk in stream:
                        received = True
                        for kind, value in assembler.feed(chunk.model_dump(exclude_unset=True)):
                            if on_delta:
                                on_delta(kind, value)
                return assembler.finish()
            # Cancelling a request closes its stream; incomplete model output
            # never becomes an executable tool call.
        except APIStatusError as exc:
            raw = self._redacted_error(
                profile, {"message": exc.message, "body": exc.body}
                if exc.body is not None else exc.message
            )
            if exc.status_code in {408, 429, 500, 502, 503, 504}:
                raise ModelUnavailable(
                    f"Model server temporarily unavailable (HTTP {exc.status_code}): {raw}"
                ) from None
            if exc.status_code == 413:
                self.invalidate_context_window(profile)
                raise ContextWindowChanged(f"Provider rejected oversized request (HTTP 413): {raw}") from None
            error = provider_failure(raw, f"HTTP {exc.status_code}")
            if isinstance(error, ContextWindowChanged):
                self.invalidate_context_window(profile)
            raise error from None
        except (APIConnectionError, httpx.TransportError, asyncio.TimeoutError) as exc:
            phase = "interrupted" if received else "failed before a response"
            raise ModelUnavailable(
                f"Model connection {phase} ({self._failure_name(exc)}). "
                "No tools from this response were executed."
            ) from None
        except ModelError as exc:
            if isinstance(exc, ContextWindowChanged):
                self.invalidate_context_window(profile)
            # Error bodies can echo credentials; never retain unredacted
            # exception chains in durable request/activity records.
            raise type(exc)(self._redacted_error(profile, str(exc))) from None
        except APIError as exc:
            raw = self._redacted_error(
                profile, {"message": exc.message, "body": exc.body}
                if exc.body is not None else exc.message
            )
            error = provider_failure(raw)
            if isinstance(error, ContextWindowChanged):
                self.invalidate_context_window(profile)
            raise error from None
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ModelError(
                f"Invalid compatible response: {self._redacted_error(profile, str(exc))}"
            ) from None

    async def close(self):
        tasks = list(self._context_window_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._context_window_tasks.clear()
        self._context_windows.clear()
        await self.http.aclose()
