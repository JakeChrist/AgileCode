"""Process progress and requirement evidence, with durable execution receipts.

Adapters supply validated model arguments. This owner validates the process facts
and commits each local effect together with its result, without holding SQLite
open while inspecting Git. Execution IDs identify operations, not provider IDs.
"""
from __future__ import annotations

from agilecode.adapters.store import decode, encode
from agilecode.application.coordination import settle
from agilecode.core.context import requirement_ids
from agilecode.core.types import AgileCodeError, Conflict, now


PROCESS_OPERATIONS = frozenset({"update_progress", "record_check"})


def _arguments(kind: str, args: dict) -> dict:
    if kind == "update_progress":
        return {"completed": args.get("completed", []),
                "remaining": args.get("remaining", []), "notes": args.get("notes", "")}
    return {"requirement": args["requirement"], "status": args["status"],
            "method": args["method"], "source_execution_id": args.get("source_execution_id")}


def _receipt(row, kind: str, original: dict):
    if row is None:
        return None
    if row["kind"] != kind or decode(row["data"]) != original:
        raise Conflict("This execution ID is already bound to different process input.")
    if row["status"] != "completed" or row["result"] is None:
        raise Conflict("This process operation has no settled local result.")
    return decode(row["result"])


def _prior_in(c, execution_id: str, kind: str, original: dict):
    return _receipt(c.execute("SELECT * FROM operations WHERE id=?", (execution_id,)).fetchone(),
                    kind, original)


def _complete_in(c, execution_id: str, kind: str, original: dict, result: dict):
    timestamp = now()
    c.execute("INSERT INTO operations(id,kind,owner_id,data,status,result,created,updated) "
              "VALUES(?,?,?,?,?,?,?,?)",
              (execution_id, kind, original["attempt_id"], encode(original), "completed",
               encode(result), timestamp, timestamp))


async def update_progress(store, ticket_id: str, attempt_id: str, args: dict,
                          execution_id: str) -> dict:
    """Record progress exactly once, atomically with its replayable receipt."""
    args = _arguments("update_progress", args)
    original = {"ticket_id": ticket_id, "attempt_id": attempt_id, "args": args}

    def record(c):
        prior = _prior_in(c, execution_id, "update_progress", original)
        if prior is not None:
            return prior
        c.execute("INSERT INTO activity(ticket_id,kind,data,created) VALUES(?,?,?,?)",
                  (ticket_id, "ProgressUpdated", encode({"attempt_id": attempt_id, **args}), now()))
        _complete_in(c, execution_id, "update_progress", original, args)
        return args

    return await settle(store.transaction(record))


def _validate_source(c, session_id: str, args: dict):
    source_id = args["source_execution_id"]
    if not source_id:
        if args["status"] == "PASS":
            raise AgileCodeError("PASS requires a real source execution ID.")
        return
    source = c.execute("SELECT * FROM tools WHERE id=?", (source_id,)).fetchone()
    if source is None:
        raise AgileCodeError(f"Unknown tools ID: {source_id}")
    if source["session_id"] != session_id or source["status"] != "completed":
        raise AgileCodeError("Evidence must cite a completed tool from this verification session.")
    result = decode(source["result"], {})
    if not isinstance(result, dict) or result.get("status") != "ok" or source["name"] not in {
        "read_file", "code_search", "git_command", "run_command", "read_job_output"
    }:
        raise AgileCodeError("The source is not a successful inspection or command result.")
    if args["status"] == "PASS" and source["name"] in {"run_command", "git_command", "read_job_output"}:
        data = result.get("data", {})
        if not isinstance(data, dict) or data.get("exit_code") != 0 or data.get("status") != "completed":
            raise AgileCodeError("A passing check requires an observed completed command with exit code zero.")


async def record_check(context, git, workspace, store, args: dict, execution_id: str) -> dict:
    """Validate real verification evidence and persist it once for this call."""
    args = _arguments("record_check", args)
    original = {"ticket_id": context.ticket["id"], "attempt_id": context.attempt["id"],
                "session_id": context.attempt["session_id"], "args": args}
    prior = _receipt(await store.one("SELECT * FROM operations WHERE id=?", (execution_id,)),
                     "record_check", original)
    if prior is not None:
        return prior

    requirements = []
    for key, output in context.outputs.items():
        if "requirements" in key or any(step.id == key and step.kind == "requirements"
                                        for step in context.process.steps):
            requirements += requirement_ids(output.output)
    if args["requirement"] not in requirements:
        raise AgileCodeError("That requirement is not in the recorded requirements artifact.")

    await store.transaction(lambda c: _validate_source(c, context.attempt["session_id"], args))
    fingerprint = await git.snapshot_tree(workspace.root)

    def record(c):
        prior = _prior_in(c, execution_id, "record_check", original)
        if prior is not None:
            return prior
        # Revalidate database-owned evidence at the same boundary as insertion.
        _validate_source(c, context.attempt["session_id"], args)
        identity = execution_id
        c.execute("INSERT INTO evidence(id,attempt_id,requirement,status,method,source_tool,fingerprint,created) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (identity, context.attempt["id"], args["requirement"], args["status"],
                   args["method"], args["source_execution_id"], fingerprint, now()))
        result = {"evidence_id": identity, **args, "fingerprint": fingerprint}
        _complete_in(c, execution_id, "record_check", original, result)
        return result

    return await settle(store.transaction(record))


async def recover_process_operation(store, execution: dict) -> dict | None:
    """Return a proven local outcome; never replay a mutation or infer success.

    Called by the conversation recovery owner before it records an interrupted
    outcome. The caller remains responsible for current access and call identity.
    """
    if execution["name"] not in PROCESS_OPERATIONS:
        return None
    row = await store.one("SELECT * FROM operations WHERE id=?", (execution["id"],))
    if row is None:
        return None
    original = decode(row["data"], {})
    args = _arguments(execution["name"], decode(execution["args"], {}))
    if original.get("args") != args:
        raise Conflict("The saved process receipt does not match this execution's arguments.")
    attempt = await store.get("attempts", original["attempt_id"])
    if attempt["session_id"] != execution["session_id"]:
        raise Conflict("The saved process receipt belongs to a different verification session.")
    result = _receipt(row, execution["name"], original)
    return {"status": "ok", "_execution_id": execution["id"], "data": result}
