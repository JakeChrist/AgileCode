"""Stop and observe owned Linux process groups, including surviving children.

A waited shell leader is not proof its process group has stopped. Zombies have
already stopped executing and cannot perform work; their parent owns reaping.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal


def _processes():
    result = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            tail = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            result[int(entry.name)] = (tail[0], int(tail[1]), int(tail[2]), tail[19])
        except (OSError, ValueError, IndexError):
            continue
    return result


def live_group_members(pgid):
    return {pid for pid, row in _processes().items() if row[2] == pgid and row[0] not in {'Z', 'X'}}


async def stop_process_group(process, *, grace=1.5):
    """One idempotent stop lifetime; never signal a recycled numeric PID twice."""
    task = getattr(process, "_agilecode_stop_task", None)
    if task is None:
        task = asyncio.create_task(_stop_process_group(process, grace=grace))
        process._agilecode_stop_task = task
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    try:
        return task.result()
    except BaseException as error:
        from agilecode.application.cancellation import current_cancellation
        scope = current_cancellation()
        if scope is not None:
            scope.termination_errors.append("Owned process termination failed: " + (str(error) or type(error).__name__))
        raise


async def _stop_process_group(process, *, grace):
    """Signal the owned group/tree and wait until no observed member can run."""
    known = {}
    original_birth = getattr(process, "_agilecode_birth", None)
    leader = _processes().get(process.pid)
    if original_birth is not None and leader is not None and leader[3] != original_birth:
        # The original process is already reaped and this numeric PID belongs
        # to someone else. Never infer ownership from its new process group.
        await process.wait()
        return

    def observe():
        snapshot = _processes()
        owned = {pid for pid, row in snapshot.items() if row[2] == process.pid}
        owned.add(process.pid)
        while True:
            children = {pid for pid, row in snapshot.items() if row[1] in owned}
            if children <= owned:
                break
            owned |= children
        for pid in owned:
            if pid in snapshot:
                known.setdefault(pid, snapshot[pid][3])
        return {pid for pid, birth in known.items() if pid in snapshot and
                snapshot[pid][3] == birth and snapshot[pid][0] not in {'Z', 'X'}}

    def signal_owned(sig):
        live = observe()
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        for pid in live:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    signal_owned(signal.SIGTERM)
    deadline = asyncio.get_running_loop().time() + grace
    while observe() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(.01)
    signal_owned(signal.SIGKILL)
    await process.wait()
    while observe():
        signal_owned(signal.SIGKILL)
        await asyncio.sleep(.01)


async def spawn_owned_process(*args, **kwargs):
    """A cancellation during process creation must not orphan the new process."""
    task = asyncio.create_task(asyncio.create_subprocess_exec(*args, **kwargs))
    try:
        process = await asyncio.shield(task)
        row = _processes().get(process.pid)
        process._agilecode_birth = row[3] if row else None
        return process
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        process = task.result()
        cleanup = asyncio.create_task(stop_process_group(process))
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()
        raise
