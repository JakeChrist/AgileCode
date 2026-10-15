"""Stable adjacent locks and atomic replacement for shared configuration files.

The lock inode is never replaced or removed. All writers, including installers,
use these locks. Busy locks fail rather than blocking an asyncio event loop.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading

_owned_locks = {}
_lock_guard = threading.Lock()
_file_observers = {}

from agilecode.core.types import Conflict


@contextmanager
def migration_file_observer(owner, callback):
    """Record declared migration file intents before their atomic replacements."""
    with _lock_guard:
        _file_observers[owner] = callback
    try:
        yield
    finally:
        with _lock_guard:
            _file_observers.pop(owner, None)


def atomic_bytes(path: Path, data: bytes, *, mode=0o600) -> None:
    path = Path(path)
    with _lock_guard:
        observers = list(_file_observers.values())
    for callback in observers:
        callback(path, data, mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".save-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def atomic_json(path: Path, value: dict) -> None:
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


@contextmanager
def shared_file_lock(path: Path, *, owner: str | None = None):
    """Lock a stable companion inode, rejecting contention without lost updates."""
    path = Path(path).expanduser().resolve()
    with _lock_guard:
        inherited = owner is not None and _owned_locks.get(str(path), (None,))[0] == owner
    if inherited:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock, flags, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Conflict(f"Configuration is being changed by another operation: {path.name}. Retry after it finishes.") from None
        if owner is not None:
            with _lock_guard:
                _owned_locks[str(path)] = (owner, descriptor)
        yield
    finally:
        if owner is not None:
            with _lock_guard:
                if _owned_locks.get(str(path)) == (owner, descriptor):
                    _owned_locks.pop(str(path), None)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
