"""Short, project-scoped ordering for policy publication and dispatch admission."""
from __future__ import annotations

import asyncio


_locks: dict[tuple, asyncio.Lock] = {}


def permits_automatic_integration(ticket, settings):
    """Resolve the existing ticket override without expanding permission scope."""
    override = ticket['merge_auto']
    return bool(settings.merge_on_completion if override is None else override)


class PolicyReservation:
    def __init__(self, project):
        self.project = project

    def _lock(self):
        key = (asyncio.get_running_loop(), self.project)
        return _locks.setdefault(key, asyncio.Lock())

    async def __aenter__(self):
        await self._lock().acquire()
        return self

    async def __aexit__(self, *args):
        self._lock().release()


def policy_lock(store):
    """Never hold this reservation while awaiting an approval or entered tool."""
    return PolicyReservation(str(store.path.resolve()))
