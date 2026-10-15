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
from dataclasses import dataclass


class OperationInterrupted(asyncio.CancelledError):
    """A known stop at an operation boundary, with no unsettled physical effect."""

    outcome_known = True

    def __init__(self, message='Interrupted before the next operation phase; already-started work has settled.', *, result=None):
        super().__init__(message)
        self.result = result


@dataclass
class CancellationToken:
    cancelled: bool = False

    def cancel(self):
        self.cancelled = True


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
