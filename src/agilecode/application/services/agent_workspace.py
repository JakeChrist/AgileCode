"""One coordinated owner of exact, worktree-scoped filesystem mutations.

Lifecycle admission belongs to the caller: model writes use their runtime lock;
manual edits retain their paused-run eligibility and output invalidation. Both
use these same Workspace checks and physical write mechanics.
"""
from __future__ import annotations

import asyncio

from agilecode.adapters.store import decode, encode
from agilecode.application.coordination import settle
from agilecode.application.cancellation import cancellation_checkpoint, current_cancellation, OperationInterrupted
from agilecode.core.types import Conflict, NeedsReview, now


WORKSPACE_OPERATIONS = {"write_file": "write", "edit_file": "edit",
                        "move_path": "move", "delete_path": "delete"}


def _arguments(operation, args):
    args = dict(args)
    if operation == "write_file":
        args.setdefault("expected_hash", None)
        args.setdefault("create", False)
    elif operation == "edit_file":
        args["replacements"] = [{"expected_count": 1, **item} for item in args["replacements"]]
    return args


def _receipt(row, operation, original):
    if row["kind"] != operation or decode(row["data"]) != original:
        raise Conflict("This execution ID is already bound to different workspace input.")
    if row["status"] == "cancelled":
        raise OperationInterrupted("Workspace operation was interrupted before modifying files.", result=decode(row["result"]))
    if row["status"] != "completed" or row["result"] is None:
        raise NeedsReview("The earlier workspace operation has unresolved effects. Inspect the worktree before retrying.")
    return decode(row["result"])


class AgentWorkspaceOperations:
    def __init__(self, workspace, write_lock: asyncio.Lock, store=None):
        self.workspace = workspace
        self.write_lock = write_lock
        self.store = store

    async def write(self, *, execution_id=None, **args):
        return await self.execute("write_file", args, execution_id)

    async def edit(self, *, execution_id=None, **args):
        return await self.execute("edit_file", args, execution_id)

    async def move(self, *, execution_id=None, **args):
        return await self.execute("move_path", args, execution_id)

    async def delete(self, *, execution_id=None, **args):
        return await self.execute("delete_path", args, execution_id)

    async def execute(self, operation: str, args: dict, execution_id: str | None = None):
        method = WORKSPACE_OPERATIONS[operation]
        args = _arguments(operation, args)
        cancellation_checkpoint()
        try:
            await self.write_lock.acquire()
        except asyncio.CancelledError:
            cancellation_checkpoint()
            raise
        try:
            cancellation_checkpoint()
            # Hold the same lock until the worker AND its receipt settle, even
            # when the awaiting coroutine is cancelled repeatedly.
            return await settle(self._execute_owned(operation, method, args, execution_id),
                                preserve_result=current_cancellation() is not None)
        finally:
            self.write_lock.release()

    async def _execute_owned(self, operation, method, args, execution_id):
        original = {"workspace": str(self.workspace.root), "args": args}
        journaled = self.store is not None and execution_id is not None
        if journaled:
            def begin(c):
                prior = c.execute("SELECT * FROM operations WHERE id=?", (execution_id,)).fetchone()
                if prior:
                    return _receipt(prior, operation, original)
                timestamp = now()
                c.execute("INSERT INTO operations(id,kind,owner_id,data,status,created,updated) VALUES(?,?,?,?,?,?,?)",
                          (execution_id, operation, str(self.workspace.root), encode(original),
                           "started", timestamp, timestamp))
                return None
            prior = await self.store.transaction(begin)
            if prior is not None:
                return prior
        def mutate():
            # to_thread copies this operation's token, including a stop arriving
            # while the worker was queued. Once entered, an atomic edit settles.
            cancellation_checkpoint()
            return getattr(self.workspace, method)(**args)
        try:
            cancellation_checkpoint()
            result = await asyncio.to_thread(mutate)
        except OperationInterrupted:
            if journaled:
                await self.store.update("operations", execution_id, status="cancelled",
                    result=encode({"effect":"not_applied"}), updated=now())
            raise
        if journaled:
            await self.store.update("operations", execution_id, status="completed",
                                    result=encode(result), updated=now())
        return result


async def recover_workspace_operation(store, execution: dict) -> dict | None:
    """Recover only a durable settled receipt, never guess from current files."""
    if execution["name"] not in WORKSPACE_OPERATIONS:
        return None
    row = await store.one("SELECT * FROM operations WHERE id=?", (execution["id"],))
    if row is None or row["status"] not in {"completed", "cancelled"} or row["result"] is None:
        return None
    original = decode(row["data"], {})
    if original.get("args") != _arguments(execution["name"], decode(execution["args"], {})):
        raise Conflict("The saved workspace receipt does not match this execution's arguments.")
    if row["status"] == "cancelled":
        from agilecode.runtime.conversation_records import interrupted_tool_result
        payload = interrupted_tool_result(execution["id"], "Workspace operation stopped before modifying files.")
        payload.update(outcome_known=True, data=decode(row["result"]))
        return payload
    result = _receipt(row, execution["name"], original)
    return {"status": "ok", "_execution_id": execution["id"], "data": result}
