"""Cooperative stop boundaries shared by conversations and owned operations.

Task cancellation stops an awaiting conversation, but must not abandon a file
writer or Git command. A shared token reaches its shielded children so they can
settle that physical effect and decline the next phase. Independently admitted
background work has its own lifetime and must detach from the launching turn.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Awaitable, Callable


class OperationInterrupted(asyncio.CancelledError):
    """A known stop at an operation boundary, with no unsettled physical effect."""

    outcome_known = True

    def __init__(self, message='Interrupted before the next operation phase; already-started work has settled.', *, result=None):
        super().__init__(message)
        self.result = result


@dataclass
class CancellationToken:
    cancelled: bool = False
    reason: str | None = None
    _children: dict[object, Callable[[], Awaitable]] = field(default_factory=dict, repr=False)
    _cleanup: asyncio.Task | None = field(default=None, repr=False)
    termination_errors: list[str] = field(default_factory=list)
    on_empty: Callable | None = field(default=None, repr=False)

    def cancel(self, reason=None):
        self.cancelled = True
        if reason is not None and self.reason != "stop":
            self.reason = reason

    def own(self, stop_and_wait: Callable[[], Awaitable]):
        """Register physical cleanup before starting response-owned background work."""
        if self.cancelled:
            raise OperationInterrupted()
        key = object()
        self._children[key] = stop_and_wait
        def release():
            self._children.pop(key, None)
            self._notify_empty()
        return release

    def _notify_empty(self):
        if (not self._children and not self.termination_errors and self.on_empty and
                (self._cleanup is None or self._cleanup.done())):
            self.on_empty()

    @property
    def has_children(self):
        return bool(self._children)

    def begin_stop(self):
        """Signal every registered child immediately, without waiting on a sibling."""
        self.cancel()
        if self._cleanup is None:
            async def cleanup():
                results = await asyncio.gather(*(stop() for stop in tuple(self._children.values())),
                                               return_exceptions=True)
                self._children.clear()
                self.termination_errors.extend(str(error) or type(error).__name__
                    for error in results if isinstance(error, BaseException))
            self._cleanup = asyncio.create_task(cleanup())
            self._cleanup.add_done_callback(lambda _: self._notify_empty())
        return self._cleanup

    async def stop_and_wait(self):
        """One uncancellable cleanup lifetime, shared by repeated stop requests."""
        self.begin_stop()
        while not self._cleanup.done():
            try:
                await asyncio.shield(self._cleanup)
            except asyncio.CancelledError:
                continue
        self._cleanup.result()
        return self.termination_errors


_current: ContextVar[CancellationToken | None] = ContextVar('operation_cancellation', default=None)
# The actor and stop token share the same lifetime boundary. Runtime records
# re-export this object; application ownership never imports the runtime layer.
conversation_actor: ContextVar[tuple[str, str] | None] = ContextVar('agilecode_conversation_session', default=None)


def current_cancellation() -> CancellationToken | None:
    return _current.get()


def cancellation_checkpoint():
    token = current_cancellation()
    if token is not None and token.cancelled:
        raise OperationInterrupted()


@contextmanager
def bind_cancellation(token: CancellationToken | None):
    binding = _current.set(token)
    try:
        yield token
    finally:
        _current.reset(binding)


@contextmanager
def detached_cancellation():
    """Detach independent work from its launching turn and conversation actor."""
    with bind_cancellation(None):
        actor = conversation_actor.set(None)
        try:
            yield
        finally:
            conversation_actor.reset(actor)


async def owned_to_thread(fn, /, *args, **kwargs):
    """Do not confuse cancellation of a thread waiter with termination of its work.

    Reads can cooperatively check the scope token. Once a non-cooperative library
    call has started, wait for it to settle before allowing replacement work.
    """
    cancellation_checkpoint()
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        token = current_cancellation()
        if token is not None:
            token.cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        # Retrieve the worker exception even when cancellation takes precedence.
        if not task.cancelled():
            task.exception()
        raise
