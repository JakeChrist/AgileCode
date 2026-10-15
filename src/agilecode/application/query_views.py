"""Shared transcript, request and wait reads for all interface adapters."""
from __future__ import annotations
from agilecode.domain.types import AgileCodeError
from agilecode.adapters.store import decode


class ProjectQueries:
    def __init__(self, store, page_size, *, activity_snapshot=None):
        self.store, self.page_size = store, page_size
        self.activity_snapshot = activity_snapshot or (lambda: {"active_conversations": {}, "conversation_activity_revisions": {}})

    async def conversation_activity(self):
        return self.activity_snapshot()

    async def conversation(self, session_id, *, before=None, after=None, limit=None):
        if before is not None and after is not None:
            raise AgileCodeError("Use either before or after, not both.")
        for name, value in (("before", before), ("after", after)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise AgileCodeError(f"{name} must be a nonnegative message sequence.")
        limit = self.page_size() if limit is None else limit
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise AgileCodeError("Conversation page size must be a positive integer.")
        clause, params = "", [session_id]
        if before is not None:
            clause = " AND seq<?"; params.append(before)
        elif after is not None:
            clause = " AND seq>?"; params.append(after)
        direction = "ASC" if after is not None else "DESC"
        rows = await self.store.query(
            f"SELECT * FROM messages WHERE session_id=?{clause} ORDER BY seq {direction} LIMIT ?",
            (*params, limit),
        )
        if after is None:
            rows.reverse()
        return [{**row, "body": decode(row["body"])} for row in rows]

    async def conversation_page(self, session_id, *, before=None, after=None, limit=None):
        rows = await self.conversation(session_id, before=before, after=after, limit=limit)
        oldest = rows[0]["seq"] if rows else before
        newest = rows[-1]["seq"] if rows else after
        older = bool(oldest is not None and await self.store.one(
            "SELECT 1 FROM messages WHERE session_id=? AND seq<? LIMIT 1", (session_id, oldest)))
        newer = bool(newest is not None and await self.store.one(
            "SELECT 1 FROM messages WHERE session_id=? AND seq>? LIMIT 1", (session_id, newest)))
        return {"session": session_id, "messages": rows, "has_older": older, "has_newer": newer}

    async def submission_receipt(self, input_id, *, session_id=None):
        """Read durable acceptance for an interface command, without replaying it."""
        if not isinstance(input_id, str) or not input_id:
            raise AgileCodeError("A nonempty input identity is required.")
        clause = " AND owner_id=?" if session_id is not None else ""
        params = (input_id, session_id) if session_id is not None else (input_id,)
        rows = await self.store.query(
            "SELECT id,data,result FROM operations WHERE kind='conversation_input' "
            "AND json_extract(data,'$.input_id')=?" + clause, params)
        if len(rows) > 1:
            raise AgileCodeError("Input identity is ambiguous; specify its conversation session.")
        if not rows:
            return None
        row = rows[0]
        return {**decode(row['data']), 'id': row['id'], 'outcome': decode(row['result'])}

    async def exact_requests(self, session_id):
        rows = await self.store.query("SELECT * FROM requests WHERE session_id=? ORDER BY created,id", (session_id,))
        return [{**row, "input": decode(row["input"])} for row in rows]

    async def pending_waits(self, ticket_id=None):
        if ticket_id is None:
            return await self.store.query("SELECT * FROM waits WHERE status='pending' ORDER BY created,id")
        return await self.store.query("SELECT * FROM waits WHERE status='pending' AND ticket_id=? ORDER BY created,id", (ticket_id,))

    async def get_wait(self, wait_id):
        return await self.store.get("waits", wait_id)

    async def pm_ticket(self, identity):
        """Compact PM-facing projection of the same selected-run facts."""
        from .queries import TicketQueries
        tickets = TicketQueries(self.store)
        # This is model context, not the GUI's full ticket detail or export.
        # Do not send snapshots, step contexts, historical discussions, or job logs.
        ticket = await tickets.find_ticket(identity)
        run = await tickets.selected_run(ticket["id"])
        result = {
            "ticket": {key: ticket[key] for key in (
                "id", "key", "title", "description", "state", "priority",
                "archived", "provider_id", "epic_id",
            )},
            "run": None,
            "steps": [],
            "dependencies": await self.store.query(
                "SELECT t.id,t.key,t.title,t.state FROM tickets t "
                "JOIN dependencies d ON d.depends_on=t.id WHERE d.ticket_id=?",
                (ticket["id"],),
            ),
        }
        if not run:
            return result

        if run["state"] == "completed" and not run["final_oid"]:
            result["deliverable_notice"] = "Legacy completion: conversation history retained, no committed file deliverable recorded."
        result["run"] = {key: run[key] for key in (
            "id", "state", "step_index", "corrections", "error", "final_oid",
        )}
        latest_attempt = await self.store.one(
            "SELECT step_id,status FROM attempts WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
            (run["id"],),
        )
        if latest_attempt:
            result["current_step"] = latest_attempt

        # The run's output pointers identify accepted step results, excluding
        # superseded/correction attempts. Summaries are enough for earlier steps.
        selected = decode(run["outputs"], {})
        if selected:
            rows = await self.store.query(
                "SELECT id,result FROM attempts WHERE run_id=? AND result IS NOT NULL",
                (run["id"],),
            )
            by_id = {row["id"]: decode(row["result"], {}) for row in rows}
            for step_id, attempt_id in selected.items():
                output = by_id.get(attempt_id)
                if not output:
                    continue
                result["steps"].append({
                    "step": step_id,
                    "status": output.get("status"),
                    "summary": str(output.get("summary") or ""),
                })
            if result["steps"]:
                result["latest_step"] = result["steps"][-1]
            if run["state"] in {"completed", "integrated"}:
                for step_id, attempt_id in reversed(list(selected.items())):
                    final = by_id.get(attempt_id)
                    if final:
                        detail = str(final.get("output") or "")
                        result["final_output"] = {
                            "step": step_id,
                            "summary": str(final.get("summary") or ""),
                            "output": detail,
                            "truncated": False,
                        }
                        break
        return result

    async def pm_pending_waits(self):
        """The ordinary pending-wait query with ticket labels for the PM."""
        from .queries import TicketQueries
        tickets = TicketQueries(self.store)
        result = []
        for wait in await self.pending_waits():
            ticket = await tickets.find_ticket(wait["ticket_id"])
            result.append({**wait, "ticket_key": ticket["key"], "ticket_title": ticket["title"]})
        return result
