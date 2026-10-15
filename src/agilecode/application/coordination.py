"""Project-wide admission and ticket ownership; never wait for workers under a lock.

ProjectLock excludes other writable hosts. These short boundaries order admission
inside that host, while the existing operations journal records durable effects.
"""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import wraps
from agilecode.core.types import Conflict
from .cancellation import cancellation_checkpoint, current_cancellation, detached_cancellation


CHECKPOINT_RESTORE_BLOCKER = ('Checkpoint restore is incomplete. Preview a checkpoint and restore again '
                              'to reconcile the retained files before resuming.')

async def settle(awaitable, *, preserve_result=False):
    """Keep ownership until an already-started physical operation has settled."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            token = current_cancellation()
            if token is not None:
                token.cancel()
    result = task.result()
    if cancelled and not preserve_result:
        raise asyncio.CancelledError
    return result


def owned_operation(fn):
    @wraps(fn)
    async def wrapper(*args, **kwargs):
        coordinator = getattr(args[0], 'coordinator', None) if args else None
        cancellation_checkpoint()
        if coordinator is None:
            return await settle(fn(*args, **kwargs), preserve_result=current_cancellation() is not None)
        return await coordinator.own(fn(*args, **kwargs), owner=kwargs.get("owner"))
    return wrapper


@dataclass(frozen=True)
class Reservation:
    ticket_id: str
    owner: str
    kind: str
    task: asyncio.Task


@dataclass
class Activity:
    ticket_id: str
    owner: str
    kind: str
    task: asyncio.Task
    cancellable: bool = True


class ProjectCoordinator:
    CONTROL_KINDS = {'move', 'pause', 'cancel', 'delete', 'rewind', 'dependency'}

    def __init__(self):
        self.lock = asyncio.Lock()
        self.reservations: dict[str, Reservation] = {}
        self.activities: dict[str, list[Activity]] = {}
        self.maintenance_owner: str | None = None
        self.paused = False
        self.closing = False
        self.owned_tasks: set[asyncio.Task] = set()
        self.maintenance_task: asyncio.Task | None = None
        self.blocked_tickets: dict[str, str] = {}

    @asynccontextmanager
    async def _admission(self, coroutine):
        """Dispose a supplied coroutine if admission is cancelled before entry."""
        try:
            async with self.lock:
                yield
        except BaseException:
            coroutine.close()
            cancellation_checkpoint()
            raise

    async def own(self, coroutine, *, owner=None):
        """Track public operations through post-reservation persistence and cleanup."""
        async with self._admission(coroutine):
            try:
                cancellation_checkpoint()
            except BaseException:
                coroutine.close()
                raise
            if self.closing:
                coroutine.close()
                raise Conflict('The project is closing; new work is blocked.')
            if self.maintenance_owner and self.maintenance_owner != owner:
                coroutine.close()
                raise Conflict('Project maintenance blocks new work.')
            task = asyncio.create_task(coroutine)
            self.owned_tasks.add(task)
        try:
            return await settle(task, preserve_result=current_cancellation() is not None)
        finally:
            # Completion callbacks do not perform external work. Keeping this
            # registry until the underlying operation settles makes close safe.
            self.owned_tasks.discard(task)

    async def close_admission(self):
        """Close is permanent for this host; no continuation can reopen it."""
        async with self.lock:
            self.closing = True

    async def drain_owned_work(self):
        """Settle admitted operations before database/owner release.

        A shielded file writer or manual Git application is never mistaken for a
        cancelled waiter. Only cancellable activities receive cancellation.
        """
        await self.close_admission()
        current = asyncio.current_task()
        cancelled = set()
        while True:
            async with self.lock:
                activities = [entry for entries in self.activities.values() for entry in entries]
                tasks = set(self.owned_tasks)
                tasks.update(lease.task for lease in self.reservations.values())
                tasks.update(entry.task for entry in activities)
                if self.maintenance_task:
                    tasks.add(self.maintenance_task)
                tasks = {task for task in tasks if task is not current and not task.done()}
                cancellable = {entry.task for entry in activities
                               if entry.task in tasks and all(other.cancellable for other in activities
                                                              if other.task is entry.task)}
            if not tasks:
                return
            for task in cancellable - cancelled:
                task.cancel()
                cancelled.add(task)
            await settle(asyncio.gather(*tasks, return_exceptions=True))

    @asynccontextmanager
    async def reserve(self, ticket_id, operation_id, kind, parent_owner=None, *, resolving_blocker=None):
        owner = parent_owner or operation_id
        nested = False
        async with self.lock:
            cancellation_checkpoint()
            if self.closing:
                raise Conflict('The project is closing; new work is blocked.')
            if ticket_id in self.blocked_tickets and self.blocked_tickets[ticket_id] != resolving_blocker:
                raise Conflict(self.blocked_tickets[ticket_id])
            if self.maintenance_owner and self.maintenance_owner != owner:
                raise Conflict('Project maintenance is in progress. Retry after it finishes.')
            prior = self.reservations.get(ticket_id)
            if prior:
                if prior.owner != owner or prior.task is not asyncio.current_task():
                    raise Conflict(f'Ticket is busy with {prior.kind}; retry after it settles.')
                nested = True
                lease = prior
            else:
                foreign = [a for a in self.activities.get(ticket_id, []) if a.owner != owner]
                if foreign and kind not in self.CONTROL_KINDS:
                    raise Conflict(f'Ticket is busy with {foreign[0].kind}; retry after it settles.')
                lease = Reservation(ticket_id, owner, kind, asyncio.current_task())
                self.reservations[ticket_id] = lease
        try:
            yield lease
        finally:
            if not nested:
                async with self.lock:
                    if self.reservations.get(ticket_id) is lease:
                        self.reservations.pop(ticket_id, None)

    @asynccontextmanager
    async def activity(self, ticket_id, owner, kind, parent_owner=None, *, check=None, cancellable=True):
        owner = parent_owner or owner
        async with self.lock:
            cancellation_checkpoint()
            if self.closing:
                raise Conflict('The project is closing; new work is blocked.')
            if ticket_id in self.blocked_tickets:
                raise Conflict(self.blocked_tickets[ticket_id])
            if self.maintenance_owner and self.maintenance_owner != owner:
                raise Conflict('Project maintenance blocks new work.')
            prior = self.reservations.get(ticket_id)
            if prior and prior.owner != owner:
                raise Conflict(f'Ticket is reserved for {prior.kind}.')
            foreign = [a for a in self.activities.get(ticket_id, []) if a.owner != owner]
            if foreign:
                raise Conflict(f'Ticket already has active {foreign[0].kind}.')
            if check:
                await check()
            cancellation_checkpoint()
            entry = Activity(ticket_id, owner, kind, asyncio.current_task(), cancellable)
            self.activities.setdefault(ticket_id, []).append(entry)
        try:
            yield entry
        finally:
            await settle(self._release_activity(entry))

    async def _release_activity(self,entry):
        async with self.lock:
            entries = self.activities.get(entry.ticket_id, [])
            if entry in entries:
                entries.remove(entry)
            if not entries:
                self.activities.pop(entry.ticket_id,None)

    async def stop(self, ticket_id):
        """Reservation must already block admission; physical cleanup owns activity."""
        async with self.lock:
            entries = list(self.activities.get(ticket_id, []))
        tasks = {a.task for a in entries if a.task is not asyncio.current_task() and not a.task.done()}
        for task in tasks:
            if all(a.cancellable for a in entries if a.task is task):
                task.cancel()
        if tasks:
            await settle(asyncio.gather(*tasks, return_exceptions=True))
            for entry in entries:
                if entry.task.done():
                    await self._release_activity(entry)

    @asynccontextmanager
    async def maintenance(self, owner):
        async with self.lock:
            cancellation_checkpoint()
            if self.closing:
                raise Conflict('The project is closing; new work is blocked.')
            if self.maintenance_owner:
                raise Conflict('Project maintenance is already in progress.')
            self.maintenance_owner = owner
            if self.reservations or self.activities or self.blocked_tickets or any(not task.done() for task in self.owned_tasks):
                self.maintenance_owner = None
                raise Conflict('Project is busy; pause active work before backup.')
            self.maintenance_task = asyncio.current_task()
        try:
            yield owner
        finally:
            async with self.lock:
                if self.maintenance_owner == owner:
                    self.maintenance_owner = None
                    self.maintenance_task = None

    async def set_paused(self, value):
        async with self.lock:
            cancellation_checkpoint()
            self.paused = bool(value)

    def busy(self, ticket_id=None):
        if ticket_id is None:
            return bool(self.activities or self.reservations or self.blocked_tickets or any(not task.done() for task in self.owned_tasks))
        return bool(self.activities.get(ticket_id) or self.reservations.get(ticket_id) or ticket_id in self.blocked_tickets)

    async def spawn(self, ticket_id, owner, kind, coroutine, *, check=None, after=None, name=None):
        """Register before scheduling; removing the activity happens after cleanup."""
        async with self._admission(coroutine):
            try:
                cancellation_checkpoint()
            except BaseException:
                coroutine.close()
                raise
            if self.closing:
                coroutine.close()
                raise Conflict('The project is closing; new work is blocked.')
            if ticket_id in self.blocked_tickets:
                coroutine.close()
                raise Conflict(self.blocked_tickets[ticket_id])
            if self.maintenance_owner and self.maintenance_owner != owner:
                coroutine.close()
                raise Conflict('Project maintenance blocks new work.')
            prior = self.reservations.get(ticket_id)
            if prior and prior.owner != owner:
                coroutine.close()
                raise Conflict(f'Ticket is reserved for {prior.kind}.')
            if any(a.owner != owner for a in self.activities.get(ticket_id, [])):
                coroutine.close()
                raise Conflict('Ticket already has active work.')
            if check:
                try:
                    await check()
                    cancellation_checkpoint()
                except BaseException:
                    coroutine.close()
                    raise
            entry = Activity(ticket_id, owner, kind, None)
            entered = False
            async def run():
                nonlocal entered
                entered = True
                try:
                    return await coroutine
                finally:
                    await settle(self._release_activity(entry))
                    if after:
                        # Continuation gets fresh admission after ownership settles.
                        await after()
            # The turn may stop after admission, but this ticket now owns its
            # execution independently. A PM correction is not Pause All.
            with detached_cancellation():
                entry.task = asyncio.create_task(run(), name=name)
            self.activities.setdefault(ticket_id, []).append(entry)
            # Activity admission ends before a continuation can take a fresh
            # lease, while full task ownership lasts through its final DB write.
            self.owned_tasks.add(entry.task)
            entry.task.add_done_callback(self.owned_tasks.discard)
            def ensure_settled(task):
                if entered:
                    return
                coroutine.close()
                async def release_unstarted():
                    async with self.lock:
                        entries = self.activities.get(ticket_id, [])
                        if entry in entries:
                            entries.remove(entry)
                        if not entries:
                            self.activities.pop(ticket_id,None)
                asyncio.create_task(release_unstarted())
            entry.task.add_done_callback(ensure_settled)
            return entry.task


def project_mutation(fn):
    """Participate in project maintenance without serializing unrelated tickets."""
    @wraps(fn)
    @owned_operation
    async def wrapped(self, *args, **kwargs):
        from agilecode.core.types import uid
        owner = uid()
        async with self.coordinator.reserve('@write:' + owner,owner,fn.__name__):
            return await fn(self,*args,**kwargs)
    return wrapped
