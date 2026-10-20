"""Durable conversation receipts, dispatch claims and the one tool-result writer.

Records use the existing project operation journal. Transactions are deliberately
short: no provider, permission wait or filesystem operation runs inside SQLite.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from agilecode.adapters.store import decode, encode
from agilecode.application.policy import policy_lock
from agilecode.application.cancellation import conversation_actor as active_session
from agilecode.core.types import AgileCodeError, Conflict, now, uid


_locks: dict[tuple, asyncio.Lock] = {}


@dataclass
class ConversationController:
    generations: dict = field(default_factory=dict)
    stop_requested: set = field(default_factory=set)
    turns: dict = field(default_factory=dict)
    admitted: dict = field(default_factory=dict)
    requests: dict = field(default_factory=dict)
    activity_revisions: dict = field(default_factory=dict)
    aliases: dict = field(default_factory=dict)
    retired_scopes: dict = field(default_factory=dict)


_controllers: dict[str, ConversationController] = {}

def controller(store):
    key = str(store.path.resolve())
    return _controllers.setdefault(key, ConversationController())


def session_identity(store, session):
    return str(store.path.resolve()), session



class SessionReservation:
    """A lazily bound reservation, safe to compose before an event loop starts."""
    def __init__(self, project, session):
        self.project, self.session = project, session

    def _lock(self):
        key = (asyncio.get_running_loop(), self.project, self.session)
        return _locks.setdefault(key, asyncio.Lock())

    def locked(self):
        return self._lock().locked()

    async def __aenter__(self):
        await self._lock().acquire()
        return self

    async def __aexit__(self, *args):
        self._lock().release()


def session_lock(store, session_id: str):
    # Application holds the exclusive project-process lock. A runtime is hosted
    # on one event loop; independent instances share the same reservation here.
    return SessionReservation(str(store.path.resolve()), session_id)


async def owned_finalization(awaitable):
    """Keep ownership until actual work ends, despite repeated waiter cancellation."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            return result, cancelled
        except asyncio.CancelledError:
            if task.done():
                return task.result(), True
            cancelled = True


def _key(session: str) -> str:
    return "conversation:" + session


def state_in(c, session: str) -> dict:
    row = c.execute("SELECT value FROM meta WHERE key=?", (_key(session),)).fetchone()
    return decode(row[0], {}) if row else {"input_version": 0, "control_revision": 0}


def save_state_in(c, session: str, state: dict):
    c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (_key(session), encode(state)))


async def state(store, session: str) -> dict:
    return await store.transaction(lambda c: state_in(c, session))


async def retained_context(store, session: str) -> dict | None:
    """Read the derived request view independently of the canonical transcript."""
    return (await state(store, session)).get("context_checkpoint")


async def retain_context(store, session: str, checkpoint: dict | None):
    """Commit a completed compaction without changing input/control versions.

    Keeping this with conversation metadata makes it durable across application
    restarts and backups, and subject to the existing attempt-erasure lifecycle.
    No in-flight or failed summary replaces a previously completed checkpoint.
    """
    def retain(c):
        current = state_in(c, session)
        if checkpoint is None:
            current.pop("context_checkpoint", None)
        else:
            current["context_checkpoint"] = checkpoint
        save_state_in(c, session, current)
    await store.transaction(retain)


async def bump_control(store, session: str) -> int:
    def bump(c):
        current = state_in(c, session)
        current["control_revision"] = current.get("control_revision", 0) + 1
        save_state_in(c, session, current)
        return current["control_revision"]
    return await store.transaction(bump)


def operation_in(c, identity: str, kind: str, owner: str, data: dict, status="pending", result=None):
    c.execute("INSERT INTO operations(id,kind,owner_id,data,status,result,created,updated) VALUES(?,?,?,?,?,?,?,?)",
              (identity, kind, owner, encode(data), status, encode(result) if result is not None else None, now(), now()))


def append_message_in(c, session: str, body: dict, request_id=None, *, partial=False, identity=None):
    identity = identity or uid()
    seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE session_id=?", (session,)).fetchone()[0]
    c.execute("INSERT INTO messages(id,session_id,seq,body,request_id,partial,created) VALUES(?,?,?,?,?,?,?)",
              (identity, session, seq, encode(body), request_id, int(partial), now()))
    return identity


def accept_input_in(c, session: str, text: str, input_id: str,
                    validate_in: Callable | None = None, *, interrupt: bool = True) -> dict:
    identity = "conversation-input:" + hashlib.sha256(encode([session, input_id]).encode()).hexdigest()
    existing = c.execute("SELECT * FROM operations WHERE id=?", (identity,)).fetchone()
    if existing:
        data = decode(existing["data"])
        if (data["session_id"] != session or data["text"] != text or data["input_id"] != input_id
                or data.get("interrupt", True) != interrupt):
            raise Conflict("Input identity was reused with different content or session.")
        return {**data, "id": identity, "outcome": decode(existing["result"])}
    if validate_in:
        validate_in(c)
    current = state_in(c, session)
    # Informational updates do not invalidate in-flight generations or tool claims.
    # They become model history at the next safe generation boundary.
    if interrupt:
        current["input_version"] = current.get("input_version", 0) + 1
        save_state_in(c, session, current)
    # Accepted input is immediately inspectable, but is not provider history
    # until preceding assistant exchanges have been closed.
    message_id = append_message_in(c, session, {"role": "user", "content": text}, input_id, partial=True)
    data = {"session_id": session, "input_id": input_id, "text": text,
            "version": current.get("input_version", 0), "interrupt": interrupt,
            "included": False, "message_id": message_id}
    operation_in(c, identity, "conversation_input", session, data)
    return {**data, "id": identity, "outcome": None}


async def accept_input(store, session: str, text: str, input_id: str | None = None,
                       validate_in: Callable | None = None, *, interrupt: bool = True) -> dict:
    input_id = input_id or uid()
    return await store.transaction(lambda c: accept_input_in(
        c, session, text, input_id, validate_in, interrupt=interrupt))


def _feedback_disposition(run_id, session):
    return {"status": "needs_followup", "run_id": run_id, "session_id": session,
            "reason": "This execution has finished. Its existing result was retained; review this later feedback before deciding on further work."}


def settle_terminal_feedback_in(c, run_id: str, ticket_id: str) -> list[dict]:
    """Retain later input with a visible disposition; never reopen completed work."""
    dispositions = []
    for row in c.execute("SELECT * FROM activity WHERE ticket_id=? AND kind='PendingFeedback' ORDER BY id", (ticket_id,)).fetchall():
        data = decode(row["data"])
        if data.get("run_id") != run_id:
            continue
        receipt = c.execute("SELECT * FROM operations WHERE id=? AND kind='conversation_input'", (data.get("receipt_id"),)).fetchone()
        if receipt is None or receipt["result"] is not None:
            continue
        outcome = _feedback_disposition(run_id, data["session_id"])
        c.execute("UPDATE operations SET status='needs_followup',result=?,updated=? WHERE id=?",
                  (encode(outcome), now(), receipt["id"]))
        data["disposition"] = outcome
        c.execute("UPDATE activity SET kind='FeedbackNeedsFollowup',data=? WHERE id=?", (encode(data), row["id"]))
        dispositions.append(data)
    return dispositions


def ticket_feedback_receipt_in(c, ticket_id: str, text: str, input_id: str | None):
    if not input_id:
        return None
    row = c.execute("SELECT o.* FROM operations o JOIN attempts a ON a.session_id=o.owner_id "
                    "JOIN runs r ON r.id=a.run_id WHERE r.ticket_id=? AND o.kind='conversation_input' "
                    "AND json_extract(o.data,'$.input_id')=? ORDER BY o.created,o.rowid LIMIT 1",
                    (ticket_id, input_id)).fetchone()
    if row is None:
        return None
    data = decode(row["data"])
    if data.get("text") != "USER FEEDBACK: " + text:
        raise Conflict("Input identity was reused with different feedback content.")
    return {**data, "id": row["id"], "outcome": decode(row["result"])}


async def ticket_feedback_receipt(store, ticket_id: str, text: str, input_id: str | None):
    """Recover acknowledgement before routing against mutable execution state."""
    return await store.transaction(lambda c: ticket_feedback_receipt_in(c, ticket_id, text, input_id))


async def accept_ticket_feedback(store, ticket_id: str, run_id: str, session: str,
                                  text: str, input_id: str | None = None, *, with_created=False):
    """Accept once across attempt changes, atomically with terminal routing."""
    input_id = input_id or uid()
    def accept(c):
        prior = ticket_feedback_receipt_in(c, ticket_id, text, input_id)
        if prior is not None:
            return prior, False
        run = c.execute("SELECT r.* FROM runs r JOIN attempts a ON a.run_id=r.id "
                        "WHERE r.id=? AND r.ticket_id=? AND a.session_id=?", (run_id, ticket_id, session)).fetchone()
        if run is None:
            raise Conflict("Feedback no longer identifies the original ticket execution.")
        receipt = accept_input_in(c, session, "USER FEEDBACK: " + text, input_id)
        data = {"run_id": run_id, "session_id": session, "text": text,
                "receipt_id": receipt["id"], "input_id": input_id}
        c.execute("INSERT INTO activity(ticket_id,kind,data,created) VALUES(?,?,?,?)",
                  (ticket_id, "PendingFeedback", encode(data), now()))
        if run["state"] in {"completed", "integrated", "cancelled", "failed"}:
            settle_terminal_feedback_in(c, run_id, ticket_id)
            receipt["outcome"] = _feedback_disposition(run_id, session)
        return receipt, True
    receipt, created = await store.transaction(accept)
    return (receipt, created) if with_created else receipt


async def receipt_outcome(store, receipt: dict | None):
    if not receipt:
        return None
    row = await store.one("SELECT result FROM operations WHERE id=? AND kind='conversation_input'", (receipt["id"],))
    return decode(row["result"]) if row else None


async def include_inputs(store, session: str) -> int:
    def include(c):
        rows = c.execute("SELECT * FROM operations WHERE kind='conversation_input' AND owner_id=? ORDER BY created,id", (session,)).fetchall()
        for row in sorted(rows, key=lambda r: decode(r["data"])["version"]):
            data = decode(row["data"])
            if data["included"]:
                continue
            seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE session_id=?", (session,)).fetchone()[0]
            c.execute("UPDATE messages SET partial=0,seq=? WHERE id=?", (seq, data["message_id"]))
            data["included"] = True
            c.execute("UPDATE operations SET data=?,updated=? WHERE id=?", (encode(data), now(), row["id"]))
        return state_in(c, session).get("input_version", 0)
    return await store.transaction(include)


async def claim_dispatch(store, session: str, identity: str, version: int, control_revision: int,
                         *, kind: str, validate_in: Callable | None = None) -> bool:
    from agilecode.application.cancellation import current_cancellation
    cancellation = current_cancellation()
    def claim(c):
        if cancellation is not None and cancellation.cancelled:
            return False
        current = state_in(c, session)
        if (current.get("input_version", 0) != version or
                current.get("control_revision", 0) != control_revision):
            return False
        if validate_in:
            validate_in(c)
        opid = "conversation-dispatch:" + identity
        existing = c.execute("SELECT * FROM operations WHERE id=?", (opid,)).fetchone()
        if existing:
            # Only a positively retired question wait may resume against the
            # same accepted inputs. Mutating claimed work is never replayed.
            if existing["status"] == "waiting" and kind == "tool":
                c.execute("UPDATE operations SET status='claimed',updated=? WHERE id=?", (now(), opid))
                return True
            return False
        operation_in(c, opid, "conversation_dispatch", session,
                     {"version": version, "control_revision": control_revision, "kind": kind}, "claimed")
        return True
    return await store.transaction(claim)


async def request_version(store, request_id: str) -> tuple[int, int]:
    row = await store.one("SELECT input FROM requests WHERE id=?", (request_id,))
    data = decode(row["input"], {}) if row else {}
    return data.get("input_version", 0), data.get("control_revision", 0)


def error_code(payload: dict):
    error = payload.get("error")
    return error.get("code") if isinstance(error, dict) else None


def interrupted_tool_result(identity: str, message: str | None = None) -> dict:
    return {"status": "error", "_execution_id": identity, "error": {"code": "interrupted", "message": message or
        "Tool execution ended before its result was recorded. Partial effects may remain; inspect the worktree before retrying."}}


async def recover_application_operation(store, tool: dict) -> dict | None:
    """Project a verified application receipt without replaying its operation.

    These application callbacks may return a richer current record. Recovery
    deliberately reports the durable operation result rather than inventing the
    exact response that an interrupted callback would have returned.
    """
    kinds = {"start_ticket": "start_ticket", "resume_ticket": "resume_run",
             "move_ticket": "move_ticket", "delete_ticket": "delete_ticket",
             "delete_attempt": "attempt_rewind", "delete_epic": "delete_epic"}
    kind = kinds.get(tool["name"])
    if kind is None:
        return None
    operation = await store.one("SELECT * FROM operations WHERE id=? AND kind=? "
                                "AND status IN ('completed','interrupted')", (tool["id"], kind))
    if not operation or operation["result"] is None:
        return None
    original = decode(operation["data"], {}).get("original")
    arguments = decode(tool["args"], {})
    if not isinstance(original, dict) or not isinstance(arguments, dict):
        return None
    for key in ("state", "position", "provider_id", "attempt_id", "epic_id"):
        if key in arguments and arguments[key] != original.get(key):
            return None
    reference = arguments.get("ticket", arguments.get("ticket_id"))
    if reference is not None and reference != original.get("ticket_id"):
        target = await store.one("SELECT id FROM tickets WHERE id=? OR key=?", (reference, reference))
        if target and target["id"] != original.get("ticket_id"):
            return None
        if not target and kind != "delete_ticket":
            return None
        # A completed deletion has removed the lookup record. Its exact opaque
        # execution ID and operation kind still bind the preserved receipt.
    result = decode(operation["result"])
    if operation["status"] == "interrupted":
        payload = interrupted_tool_result(tool["id"],
            result.get("reason") if isinstance(result, dict) else None)
        payload.update(outcome_known=True, data=result,
                       operation={"kind": kind, "status": "interrupted", "recovered": True})
        return payload
    return {"status": "ok", "_execution_id": tool["id"], "outcome_known": True,
            "data": {"operation": kind, "result": result, "recovered": True}}


async def recover_execution_payload(store, tool: dict) -> dict:
    """Reconcile local commit evidence before falling back to honest uncertainty."""
    if tool.get("result") is not None:
        return decode(tool["result"])
    from agilecode.application.process_operations import recover_process_operation
    from agilecode.application.services.agent_workspace import recover_workspace_operation
    for recover in (recover_process_operation, recover_workspace_operation, recover_application_operation):
        local = await recover(store, tool)
        if local is not None:
            return local
    name = tool["name"]
    if name in {"create_ticket", "create_epic"}:
        operation = await store.one("SELECT * FROM operations WHERE id=? AND kind=? AND status='completed'", (tool["id"], name))
        if operation:
            key = "ticket_id" if name == "create_ticket" else "epic_id"
            data = await store.get("tickets" if name == "create_ticket" else "epics", decode(operation["result"])[key])
            return {"status": "ok", "_execution_id": tool["id"], "data": data}
    payload = interrupted_tool_result(tool["id"])
    if tool["category"] != "mutation":
        payload["outcome_known"] = True
    return payload



def _reconciliation_id(tool_id: str) -> str:
    return "conversation-reconciliation:" + tool_id


def _reconciled_payload_in(c, tool_id: str) -> dict | None:
    row = c.execute("SELECT * FROM operations WHERE id=? AND kind='conversation_reconciliation' AND status='completed'",
                    (_reconciliation_id(tool_id),)).fetchone()
    if row is None:
        return None
    tool = c.execute("SELECT * FROM tools WHERE id=?", (tool_id,)).fetchone()
    data, payload = decode(row["data"]), decode(row["result"])
    if (tool is None or tool["name"] != "final_commit" or row["owner_id"] != tool["session_id"] or
            data.get("execution_id") != tool_id or data.get("original_result_hash") !=
            hashlib.sha256(encode(decode(tool["result"])).encode()).hexdigest() or
            not isinstance(payload, dict) or payload.get("_execution_id") != tool_id):
        raise Conflict("Verified reconciliation no longer matches its original execution.")
    return payload


async def reconciled_execution_payload(store, tool_id: str) -> dict | None:
    """Read a separately verified outcome without rewriting the original result."""
    return await store.transaction(lambda c: _reconciled_payload_in(c, tool_id))


def _project_reconciliation_notices_in(c, session: str):
    # A context notice must not split an assistant's batch from its tool results.
    # Startup can encounter later unstarted calls with no tools row yet, so wait
    # until every saved exchange in the session has its matching results.
    messages = c.execute("SELECT * FROM messages WHERE session_id=? AND partial=0 ORDER BY seq", (session,)).fetchall()
    results = {(row["request_id"], decode(row["body"]).get("tool_call_id"))
               for row in messages if decode(row["body"]).get("role") == "tool"}
    for row in messages:
        for call in decode(row["body"]).get("tool_calls") or []:
            if (row["request_id"], call.get("id")) not in results:
                return
    for row in c.execute("SELECT * FROM operations WHERE kind='conversation_reconciliation' AND owner_id=? AND status='completed' ORDER BY created,id", (session,)).fetchall():
        data = decode(row["data"])
        payload = _reconciled_payload_in(c, data["execution_id"])
        identity = row["id"] + ":notice"
        if c.execute("SELECT id FROM messages WHERE id=?", (identity,)).fetchone():
            continue
        append_message_in(c, session, {"role": "user", "content":
            "VERIFIED TOOL OUTCOME RECONCILIATION — recorded task data. "
            "The original interrupted final_commit result is retained in history. "
            "The operation journal and Git now verify that the requested commit completed. "
            "Continue from this known outcome without repeating that commit operation.\n" + encode(payload)},
            identity=identity)


async def project_reconciliation_notices(store, session: str):
    await store.transaction(lambda c: _project_reconciliation_notices_in(c, session))


async def reconcile_final_commit_execution(store, tool_id: str, *, git=None) -> dict | None:
    """Resolve only a journal-backed final commit; never replay a mutating tool.

    The original interrupted result and its sole tool message remain immutable.
    A new journal record plus ordinary context notice records later verified
    evidence. Call after the normal exchange writer has closed the interrupted
    execution. A mismatch leaves uncertainty unchanged.
    """
    known = await reconciled_execution_payload(store, tool_id)
    if known is not None:
        tool = await store.get("tools", tool_id)
        await project_reconciliation_notices(store, tool["session_id"])
        return known
    tool = await store.one("SELECT * FROM tools WHERE id=?", (tool_id,))
    if (not tool or tool["name"] != "final_commit" or tool["category"] != "mutation" or
            tool["status"] != "unknown"):
        return None
    original = decode(tool["result"], {})
    if error_code(original) != "interrupted" or original.get("outcome_known"):
        return None
    run = await store.one("SELECT runs.* FROM runs JOIN attempts ON runs.id=attempts.run_id WHERE attempts.session_id=?",
                          (tool["session_id"],))
    if run is None:
        return None
    operation_id = "final-commit:" + run["id"]
    operation = await store.one("SELECT * FROM operations WHERE id=? AND kind='final_commit' AND owner_id=? AND status IN ('prepared','completed')",
                                (operation_id, run["id"]))
    if operation is None:
        return None
    data, args = decode(operation["data"], {}), decode(tool["args"], {})
    if (not all(isinstance(data.get(key), str) and data[key] for key in ("base_oid", "expected_tree", "branch", "message")) or
            data["base_oid"] != run["base_oid"] or data["branch"] != run["branch"] or
            args.get("message") != data["message"]):
        return None
    try:
        if git is None:
            from agilecode.adapters.git import GitRepository
            identity_row = await store.one("SELECT value FROM meta WHERE key='project'")
            identity = decode(identity_row["value"], {}) if identity_row else {}
            if not identity.get("root") or not identity.get("common_git_dir"):
                return None
            git = GitRepository(Path(identity["root"]))
            if (await git.identify())["common_git_dir"] != identity["common_git_dir"]:
                return None
        workspace = Path(run["workspace"])
        await git.verify_worktree(workspace, data["branch"])
        oid = await git.head(workspace)
        tree = (await git.run("rev-parse", "HEAD^{tree}", cwd=workspace)).text
        parents = (await git.run("rev-list", "--parents", "-n", "1", oid, cwd=workspace)).text.split()[1:]
        message = (await git.run("show", "-s", "--format=%B", oid, cwd=workspace)).text
        if (oid == data["base_oid"] or tree != data["expected_tree"] or parents != [data["base_oid"]] or
                message != data["message"].strip() or await git.snapshot_tree(workspace) != tree):
            return None
        if operation["status"] == "completed":
            if (decode(operation["result"], {}).get("oid") != oid or
                    run["final_oid"] != oid or run["final_tree"] != tree):
                return None
        elif (operation["result"] is not None or run["final_oid"] not in (None, oid) or
              run["final_tree"] not in (None, tree)):
            return None
        # Recheck the moving references after observing the complete source tree.
        await git.verify_worktree(workspace, data["branch"])
        if await git.head(workspace) != oid:
            return None
    except (AgileCodeError, OSError):
        return None
    payload = {"status": "ok", "_execution_id": tool_id, "outcome_known": True,
               "data": {"commit": oid, "tree": tree, "branch": data["branch"],
                        "message": data["message"], "recovered": True}}
    evidence = {"operation_id": operation_id, "run_id": run["id"], "workspace": run["workspace"],
                "branch": data["branch"], "base_oid": data["base_oid"], "oid": oid, "tree": tree,
                "parents": parents}
    def persist(c):
        prior = _reconciled_payload_in(c, tool_id)
        if prior is not None:
            return prior
        current_tool = c.execute("SELECT * FROM tools WHERE id=?", (tool_id,)).fetchone()
        current_run = c.execute("SELECT * FROM runs WHERE id=?", (run["id"],)).fetchone()
        current_operation = c.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if (current_tool is None or current_run is None or current_operation is None or
                any(current_tool[key] != tool[key] for key in ("session_id", "request_id", "call_id", "name", "args", "status", "result")) or
                any(current_run[key] != run[key] for key in ("workspace", "branch", "base_oid", "final_oid", "final_tree")) or
                any(current_operation[key] != operation[key] for key in ("kind", "owner_id", "data", "status", "result"))):
            return None
        c.execute("UPDATE runs SET final_oid=?,final_tree=?,updated=? WHERE id=?", (oid, tree, now(), run["id"]))
        c.execute("UPDATE operations SET status='completed',result=?,updated=? WHERE id=?",
                  (encode({"oid": oid}), now(), operation_id))
        operation_in(c, _reconciliation_id(tool_id), "conversation_reconciliation", tool["session_id"],
                     {"execution_id": tool_id, "original_result_hash": hashlib.sha256(encode(original).encode()).hexdigest(),
                      "evidence": evidence}, "completed", payload)
        _project_reconciliation_notices_in(c, tool["session_id"])
        return payload
    return await store.transaction(persist)


def append_tool_result_once(c, session: str, request_id: str, call_id: str, content: str):
    """Project one result for this assistant exchange; conflicting replay fails."""
    rows = c.execute("SELECT * FROM messages WHERE session_id=? AND request_id=? ORDER BY seq", (session, request_id)).fetchall()
    assistant = [r for r in rows if decode(r["body"]).get("role") == "assistant"
                 and any(x.get("id") == call_id for x in decode(r["body"]).get("tool_calls", []))]
    if not assistant:
        return None  # A directly invoked executor can have an audit request only.
    if len(assistant) != 1:
        raise Conflict("Tool call has more than one assistant exchange.")
    results = [r for r in rows if decode(r["body"]).get("role") == "tool"
               and decode(r["body"]).get("tool_call_id") == call_id]
    if results:
        if len(results) != 1 or decode(results[0]["body"]).get("content") != content:
            raise Conflict("Conflicting result for the same assistant tool exchange.")
        return results[0]["id"]
    return append_message_in(c, session, {"role": "tool", "tool_call_id": call_id, "content": content}, request_id)


async def complete_execution_exchange_once(store, tool_id: str, payload: dict,
                                            *, content: str | None = None,
                                            expected_version: int | None = None) -> dict:
    """The sole execution-result/transcript writer, shared with startup repair."""
    def complete(c):
        row = c.execute("SELECT * FROM tools WHERE id=?", (tool_id,)).fetchone()
        if row is None:
            raise Conflict("Unknown tool execution.")
        saved = decode(row["result"])
        actual = payload
        if saved is not None and saved != payload:
            raise Conflict("Tool execution already has a different durable result.")
        if saved is None and expected_version is not None and state_in(c, row["session_id"]).get("input_version", 0) != expected_version:
            actual = {"status": "error", "_execution_id": tool_id, "error": {
                "code": "input_changed", "message": "New user input arrived before completion was accepted. Reassess the step."}}
        representation = content if content is not None and actual == payload else encode(actual)
        opid = "conversation-result:" + tool_id
        prior = c.execute("SELECT * FROM operations WHERE id=?", (opid,)).fetchone()
        if prior:
            recorded = decode(prior["data"])
            if recorded.get("result_hash") is not None and recorded["result_hash"] != hashlib.sha256(encode(actual).encode()).hexdigest():
                raise Conflict("Tool result projection conflicts with its recorded outcome.")
            representation = recorded["content"]
        else:
            # Migration of a saved result whose legacy preview already appears in
            # the transcript. Preserve only a representation proven equivalent.
            existing_messages = c.execute("SELECT body FROM messages WHERE session_id=? AND request_id=?", (row["session_id"], row["request_id"])).fetchall()
            for message_row in existing_messages:
                body = decode(message_row["body"])
                if body.get("role") != "tool" or body.get("tool_call_id") != row["call_id"]:
                    continue
                candidate = body.get("content", "")
                try:
                    old = json.loads(candidate)
                except (TypeError, ValueError):
                    raise Conflict("Existing legacy tool result cannot be verified.") from None
                if old == actual:
                    representation = candidate
                elif (saved is not None and old.get("partial") is True and
                      old.get("_execution_id") == tool_id and old.get("status") == actual.get("status") and
                      isinstance(old.get("preview"), str) and encode(actual).startswith(old["preview"])):
                    artifact = c.execute("SELECT * FROM artifacts WHERE id=?", (old.get("artifact_id"),)).fetchone()
                    if artifact is None:
                        raise Conflict("Legacy tool-result preview refers to a missing artifact.")
                    representation = candidate
                else:
                    raise Conflict("Legacy tool transcript conflicts with its saved execution result.")
            operation_in(c, opid, "conversation_result", row["session_id"], {"content": representation, "result_hash": hashlib.sha256(encode(actual).encode()).hexdigest()}, "completed")
        uncertain = (error_code(actual) == "interrupted" and row["category"] == "mutation" and not actual.get("outcome_known"))
        if row["name"] == "create_ticket" and isinstance(actual.get("error"), str) and "Read interrupted" in actual["error"]:
            uncertain = True  # Historical discussions misclassified creation as a read.
        status = "unknown" if uncertain else "completed"
        c.execute("UPDATE tools SET status=?,result=?,finished=? WHERE id=?", (status, encode(actual), now(), tool_id))
        append_tool_result_once(c, row["session_id"], row["request_id"], row["call_id"], representation)
        c.execute("UPDATE operations SET status='completed',updated=? WHERE id=?", (now(), "conversation-dispatch:" + tool_id))
        return actual
    return await store.transaction(complete)


async def finish_turn(store, session: str, turn_id: str, outcome: dict, *, version: int | None = None,
                      intent: dict | None = None, require_current=True) -> bool:
    def finish(c):
        if require_current and version is not None:
            if state_in(c, session).get("input_version", 0) != version:
                return False
            # An informational update can arrive during the final generation.
            # Continue until every accepted message was actually supplied to
            # the model; the check and settlement share one DB transaction.
            for row in c.execute(
                "SELECT data FROM operations WHERE kind='conversation_input' "
                "AND owner_id=? AND result IS NULL", (session,)
            ):
                if not decode(row["data"])["included"]:
                    return False
        identity = "conversation-turn:" + turn_id
        existing = c.execute("SELECT id FROM operations WHERE id=?", (identity,)).fetchone()
        if not existing:
            operation_in(c, identity, "conversation_turn", session, {"version": version}, "completed", outcome)
        if intent:
            opid = "conversation-state:" + turn_id
            if not c.execute("SELECT id FROM operations WHERE id=?", (opid,)).fetchone():
                operation_in(c, opid, "conversation_state", session, intent)
        if version is not None:
            for row in c.execute("SELECT * FROM operations WHERE kind='conversation_input' AND owner_id=? AND result IS NULL", (session,)).fetchall():
                data = decode(row["data"])
                if data["included"] and data["version"] <= version:
                    c.execute("UPDATE operations SET result=?,status='completed',updated=? WHERE id=?", (encode(outcome), now(), row["id"]))
        return True
    return await store.transaction(finish)
