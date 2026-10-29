"""Shared application operations, extracted from the public compatibility facade."""
from __future__ import annotations
import asyncio
import json
import re
import shutil
import sqlite3
import traceback
from pathlib import Path
from agilecode.core.types import AgileCodeError, Conflict, NeedsReview, BoardState, Team, Event, uid, now
from agilecode.adapters.store import encode, decode
from agilecode.adapters.workspace import Workspace, digest, inside

from .coordination import owned_operation, project_mutation
from .cancellation import cancellation_checkpoint, OperationInterrupted
from .policy import policy_lock


def validate_title(title, *, subject='title'):
    if not isinstance(title, str) or not title.strip():
        raise AgileCodeError(f'Provide a nonempty {subject}.')
    return title.strip()


def operation_input(original):
    return json.dumps(original, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


class TicketOperations:
    def __init__(self, store, config, events, coordinator, queries, *, execution=None, discussion=None):
        self.store, self.config, self.events = store, config, events
        self.coordinator, self.queries = coordinator, queries
        self.execution, self.discussion = execution, discussion

    def _check_identity(self, prior, kind, original):
        if prior['kind'] != kind or operation_input(decode(prior['data'], {}).get('original')) != operation_input(original):
            raise Conflict('This request ID is already bound to different original input.')

    async def _prior(self, request_id, kind, original):
        if not request_id:
            return None
        prior = await self.store.one('SELECT * FROM operations WHERE id=?', (request_id,))
        if prior:
            self._check_identity(prior, kind, original)
        return prior

    @staticmethod
    def _epic_id(value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", value):
            raise AgileCodeError("Epic ID must start with a letter and contain up to 40 letters, digits, hyphens or underscores.")
        if value == "T":
            raise AgileCodeError("Epic ID 'T' is reserved for standalone ticket keys.")
        return value


    @project_mutation
    async def create_epic(self, epic_id: str, title: str, description: str = "",
                          completion_criteria: str = "", request_id: str | None = None) -> dict:
        original = dict(epic_id=epic_id, title=title, description=description, completion_criteria=completion_criteria)
        prior = await self._prior(request_id, 'create_epic', original)
        if prior:
            return await self.get_epic(decode(prior['result'])['epic_id'])
        epic_id = self._epic_id(epic_id)
        if not epic_id:
            raise AgileCodeError("An epic requires an ID.")
        title = validate_title(title,subject='epic title')
        if not isinstance(description, str):
            raise AgileCodeError("Epic description cannot exceed 100,000 characters.")
        if not isinstance(completion_criteria, str):
            raise AgileCodeError("Epic completion criteria cannot exceed 100,000 characters.")
        timestamp = now()

        def create(c):
            if request_id:
                prior = c.execute('SELECT * FROM operations WHERE id=?', (request_id,)).fetchone()
                if prior:
                    self._check_identity(prior, 'create_epic', original)
                    return
            # Ticket keys (and the work they identify) survive epic deletion.
            # Keep the namespace reserved even if its last ticket is removed.
            if c.execute("SELECT 1 FROM meta WHERE key=?", ("deleted_epic:" + epic_id,)).fetchone():
                raise AgileCodeError(f"Epic ID {epic_id!r} was deleted and is reserved. Choose a new ID.")
            c.execute(
                "INSERT INTO epics(id,title,description,completion_criteria,status,created,updated) "
                "VALUES(?,?,?,?,?,?,?)",
                (epic_id, title.strip(), description, completion_criteria, "Active", timestamp, timestamp),
            )
            if request_id:
                c.execute(
                    "INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)",
                    (request_id, "create_epic", epic_id, encode({"original": original}), "completed",
                     encode({"epic_id": epic_id}), timestamp, timestamp),
                )

        try:
            cancellation_checkpoint()
            await self.store.transaction(create)
        except sqlite3.IntegrityError as exc:
            raise AgileCodeError(f"Epic ID {epic_id!r} is already in use.") from exc
        self.events.emit(Event("EpicChanged", data={"epic_id": epic_id}))
        return await self.get_epic(epic_id)


    async def list_epics(self) -> list[dict]:
        return await self.store.query(
            "SELECT e.id,e.title,SUBSTR(e.description,1,500) AS description,e.status,"
            "e.revision,e.created,e.updated,COUNT(t.id) AS ticket_count,"
            "SUM(CASE WHEN t.state='Done' THEN 1 ELSE 0 END) AS completed_count "
            "FROM epics e LEFT JOIN tickets t ON t.epic_id=e.id "
            "GROUP BY e.id ORDER BY e.created,e.id"
        )


    async def get_epic(self, epic_id: str) -> dict:
        epic = await self.store.get("epics", epic_id)
        tickets = await self.store.query(
            "SELECT id,key,title,state,archived FROM tickets WHERE epic_id=? "
            "ORDER BY CASE WHEN SUBSTR(key,1,LENGTH(epic_id)+1)=epic_id||'-' "
            "THEN 0 ELSE 1 END,CAST(SUBSTR(key,LENGTH(epic_id)+2) AS INTEGER),key",
            (epic_id,),
        )
        return {
            "epic": {key: value for key, value in epic.items() if key != "next_ticket_number"},
            "tickets": tickets, "ticket_count": len(tickets),
            "completed_count": sum(ticket["state"] == "Done" for ticket in tickets),
        }


    @project_mutation
    async def update_epic(self, epic_id: str, *, expected_revision: int | None = None,
                          **changes) -> dict:
        if not changes.keys() <= {"title", "description", "completion_criteria", "status"}:
            raise AgileCodeError("Unsupported epic field.")
        epic = await self.store.get("epics", epic_id)
        if "title" in changes:
            changes['title'] = validate_title(changes['title'],subject='epic title')
        for field in ("description", "completion_criteria"):
            if field in changes and (not isinstance(changes[field], str) ):
                raise AgileCodeError(f"Epic {field.replace('_', ' ')} cannot exceed 100,000 characters.")
        if "status" in changes and changes["status"] not in {"Active", "Completed", "Archived"}:
            raise AgileCodeError("Epic status must be Active, Completed, or Archived.")
        if not changes:
            return await self.get_epic(epic_id)
        revision = epic["revision"] if expected_revision is None else expected_revision
        sql = ("UPDATE epics SET " + ",".join(f"{field}=?" for field in changes) +
               ",revision=revision+1,updated=? WHERE id=? AND revision=?")
        cancellation_checkpoint()
        count = await self.store.execute(sql, (*changes.values(), now(), epic_id, revision))
        if count != 1:
            raise Conflict("Epic changed since it was opened. Refresh before saving.")
        self.events.emit(Event("EpicChanged", data={"epic_id": epic_id}))
        return await self.get_epic(epic_id)


    @project_mutation
    async def delete_epic(self, epic_id: str, *, expected_revision: int | None = None,
                          request_id: str | None = None) -> dict:
        """Delete only the grouping; detach tickets without changing their keys."""
        original = dict(epic_id=epic_id, expected_revision=expected_revision)
        prior = await self._prior(request_id, "delete_epic", original)
        if prior:
            return decode(prior["result"])
        identity, timestamp = request_id or uid(), now()

        def delete(c):
            prior = c.execute("SELECT * FROM operations WHERE id=?", (identity,)).fetchone()
            if prior:
                self._check_identity(prior, "delete_epic", original)
                return decode(prior["result"]), False
            epic = c.execute("SELECT revision FROM epics WHERE id=?", (epic_id,)).fetchone()
            if epic is None:
                raise AgileCodeError(f"Unknown epic: {epic_id}")
            if expected_revision is not None and expected_revision != epic["revision"]:
                raise Conflict("Epic changed since it was opened. Refresh before deleting.")
            ticket_ids = [row["id"] for row in c.execute(
                "SELECT id FROM tickets WHERE epic_id=? ORDER BY id", (epic_id,)
            )]
            # Do not use edit_ticket here: detaching an unstarted ticket there
            # intentionally assigns a new key. Deleting a grouping never does.
            c.execute("UPDATE tickets SET epic_id=NULL,revision=revision+1,updated=? WHERE epic_id=?",
                      (timestamp, epic_id))
            c.execute("DELETE FROM epics WHERE id=?", (epic_id,))
            c.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("deleted_epic:" + epic_id, timestamp))
            result = {"epic_id": epic_id, "deleted": True,
                      "detached_ticket_ids": ticket_ids, "detached_ticket_count": len(ticket_ids)}
            c.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)",
                      (identity, "delete_epic", epic_id, encode({"original": original}), "completed",
                       encode(result), timestamp, timestamp))
            return result, True

        cancellation_checkpoint()
        result, changed = await self.store.transaction(delete)
        if changed:
            self.events.emit(Event("EpicChanged", data={"epic_id": epic_id, "deleted": True}))
            for ticket_id in result["detached_ticket_ids"]:
                self.events.emit(Event("TicketChanged", ticket_id))
        return result


    @project_mutation
    async def create_ticket(self, title: str, description: str, *, team_ref: str | None = None,
                            process_id: str | None = None, parent_id: str | None = None,
                            provider_id: str | None = None, epic_id: str | None = None,
                            merge_auto: bool | None = None, source_discussion_id: str | None = None,
                            request_id: str | None = None) -> dict:
        original = dict(title=title, description=description, team_ref=team_ref, process_id=process_id,
                        parent_id=parent_id, provider_id=provider_id, epic_id=epic_id, merge_auto=merge_auto,
                        source_discussion_id=source_discussion_id)
        prior = await self._prior(request_id, 'create_ticket', original)
        if prior:
            return await self.store.get('tickets', decode(prior['result'])['ticket_id'])
        title = validate_title(title)
        if not isinstance(description, str):
            raise AgileCodeError('Description must be text.')
        epic_id = self._epic_id(epic_id)
        settings = self.config.settings
        reference = team_ref or settings.default_team
        team = await self.config.get_team(reference)
        process_id = process_id or (settings.default_process if reference == settings.default_team else team.default_process_id)
        if process_id not in {p.id for p in team.processes}:
            raise AgileCodeError("Choose a process owned by the selected team.")
        if parent_id:
            await self.store.get("tickets", parent_id)
        if provider_id is not None:
            self.config.get_provider(provider_id)
        source_discussion = await self.discussion.get(source_discussion_id) if source_discussion_id and self.discussion else None
        if source_discussion_id and not source_discussion:
            source_discussion = await self.store.get('discussions', source_discussion_id)
        identity, timestamp = uid(), now()
        def create(c):
            if request_id:
                prior = c.execute('SELECT * FROM operations WHERE id=?', (request_id,)).fetchone()
                if prior:
                    self._check_identity(prior, 'create_ticket', original)
                    return decode(prior['result'])['ticket_id']
            row = c.execute("SELECT value FROM meta WHERE key='ticket_sequence'").fetchone()
            number = int(row[0])+1 if row else 1
            c.execute("INSERT INTO meta VALUES('ticket_sequence',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(number),))
            key = f"T-{number:03d}"
            if epic_id:
                epic = c.execute("SELECT next_ticket_number FROM epics WHERE id=?", (epic_id,)).fetchone()
                if epic is None:
                    raise AgileCodeError(f"Unknown epic: {epic_id}")
                key = f"{epic_id}-{epic['next_ticket_number']:02d}"
                c.execute("UPDATE epics SET next_ticket_number=next_ticket_number+1 WHERE id=?", (epic_id,))
            c.execute("INSERT INTO tickets(id,key,title,description,state,team_ref,process_id,provider_id,epic_id,parent_id,merge_auto,position,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identity, key, title.strip(), description, "Backlog", reference, process_id, provider_id, epic_id, parent_id,
                 int(merge_auto) if merge_auto is not None else None, number*1024, timestamp, timestamp))
            if source_discussion:
                c.execute("INSERT INTO discussion_tickets(discussion_id,ticket_id,created) VALUES(?,?,?)",
                          (source_discussion["id"], identity, timestamp))
            if request_id:
                c.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)", (request_id,"create_ticket",identity,encode({'original': original}),"completed",encode({"ticket_id":identity}),timestamp,timestamp))
            return identity
        if parent_id:
            async with self.coordinator.reserve(parent_id,uid(),'dependency'):
                cancellation_checkpoint()
                identity = await self.store.transaction(create)
        else:
            cancellation_checkpoint()
            identity = await self.store.transaction(create)
        self.events.emit(Event("TicketChanged", identity))
        return await self.store.get("tickets", identity)


    async def find_ticket(self,identity):
        return await self.queries.find_ticket(identity)

    @project_mutation
    async def add_ticket_note(self, ticket_id: str, text: str, *, author: str = "user",
                              request_id: str | None = None) -> dict:
        ticket = await self.find_ticket(ticket_id)
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise AgileCodeError("Ticket note must contain 1 to 20,000 characters.")
        if author not in {"user", "project_manager"}:
            raise AgileCodeError("Unknown ticket note author.")
        text = text.strip()

        def add(c):
            if not c.execute("SELECT 1 FROM tickets WHERE id=?", (ticket["id"],)).fetchone():
                raise AgileCodeError("Ticket no longer exists.")
            if request_id:
                previous = c.execute(
                    "SELECT id,data,created FROM activity WHERE ticket_id=? AND kind='TicketNote' "
                    "AND json_extract(data,'$.request_id')=? LIMIT 1",
                    (ticket["id"], request_id),
                ).fetchone()
                if previous:
                    saved = decode(previous["data"], {})
                    if saved.get("text") != text or saved.get("author") != author:
                        raise Conflict("Ticket note request ID belongs to a different note.")
                    return {"id": previous["id"], "ticket_id": ticket["id"],
                            "author": author, "text": text, "created": previous["created"]}
            timestamp = now()
            record = {"author": author, "text": text, "request_id": request_id}
            cursor = c.execute(
                "INSERT INTO activity(ticket_id,kind,data,created) VALUES(?,?,?,?)",
                (ticket["id"], "TicketNote", encode(record), timestamp),
            )
            return {"id": cursor.lastrowid, "ticket_id": ticket["id"],
                    "author": author, "text": text, "created": timestamp}

        note = await self.store.transaction(add)
        self.events.emit(Event("TicketChanged", ticket["id"],
                               data={"redundant_pm_notice": author == "project_manager"}))
        return note

    @project_mutation
    async def edit_ticket(self, ticket_id: str, *, expected_revision: int, **changes) -> dict:
        allowed = {"title", "description", "team_ref", "process_id", "provider_id", "priority", "merge_auto", "epic_id"}
        if not changes.keys() <= allowed:
            raise AgileCodeError("Unsupported ticket field.")
        ticket = await self.find_ticket(ticket_id)
        if 'title' in changes:
            changes['title'] = validate_title(changes['title'])
        if 'description' in changes and not isinstance(changes['description'], str):
            raise AgileCodeError('Description must be text.')
        if ticket["state"] == "In Progress" and changes.keys() & {"description", "team_ref", "process_id", "provider_id"}:
            raise AgileCodeError("Ticket/process/provider inputs are frozen during execution. Cancel and start a revised run to change them.")
        team = await self.config.get_team(changes.get("team_ref",ticket["team_ref"]))
        if "provider_id" in changes and changes["provider_id"] is not None:
            self.config.get_provider(changes["provider_id"])
        if changes.get("process_id",ticket["process_id"]) not in {p.id for p in team.processes}:
            raise AgileCodeError("The selected process does not belong to this team.")
        if "epic_id" in changes:
            changes["epic_id"] = self._epic_id(changes["epic_id"])
            if changes["epic_id"] and not await self.store.one(
                "SELECT 1 FROM epics WHERE id=?", (changes["epic_id"],)
            ):
                raise AgileCodeError(f"Unknown epic: {changes['epic_id']}")
        if not changes:
            return ticket

        def edit(c):
            current = c.execute('SELECT * FROM tickets WHERE id=?', (ticket['id'],)).fetchone()
            if current['state'] == 'In Progress' and changes.keys() & {'description', 'team_ref', 'process_id', 'provider_id'}:
                raise AgileCodeError('Ticket/process/provider inputs are frozen during execution. Cancel and start a revised run to change them.')
            fields = dict(changes)
            # Existing runs use the ticket key in persistent Git branch/worktree
            # paths. Epic membership remains editable, but only never-run tickets
            # can be rekeyed to the EpicID-NN naming convention.
            if ("epic_id" in fields and fields["epic_id"] != ticket["epic_id"]
                    and not c.execute("SELECT 1 FROM runs WHERE ticket_id=? LIMIT 1",
                                      (ticket["id"],)).fetchone()):
                if fields["epic_id"]:
                    epic = c.execute("SELECT next_ticket_number FROM epics WHERE id=?", (fields["epic_id"],)).fetchone()
                    if epic is None:
                        raise AgileCodeError(f"Unknown epic: {fields['epic_id']}")
                    fields["key"] = f"{fields['epic_id']}-{epic['next_ticket_number']:02d}"
                    c.execute("UPDATE epics SET next_ticket_number=next_ticket_number+1 WHERE id=?", (fields["epic_id"],))
                else:
                    row = c.execute("SELECT value FROM meta WHERE key='ticket_sequence'").fetchone()
                    number = int(row[0]) + 1 if row else 1
                    c.execute("INSERT INTO meta VALUES('ticket_sequence',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(number),))
                    fields["key"] = f"T-{number:03d}"
            sql = ("UPDATE tickets SET " + ",".join(f"{field}=?" for field in fields) +
                   ",revision=revision+1,updated=? WHERE id=? AND revision=?")
            count = c.execute(sql, (*fields.values(), now(), ticket["id"], expected_revision)).rowcount
            if count != 1:
                raise Conflict("Ticket changed since it was opened. Refresh before saving.")

        async with policy_lock(self.store):
            cancellation_checkpoint()
            await self.store.transaction(edit)
        self.events.emit(Event("TicketChanged", ticket["id"]))
        return await self.store.get("tickets", ticket["id"])


    @project_mutation
    async def add_dependency(self, ticket_id: str, depends_on: str):
        ticket = await self.find_ticket(ticket_id)
        dependency = await self.find_ticket(depends_on)
        def insert(c):
            if ticket['id'] == dependency['id']:
                raise AgileCodeError('A ticket cannot depend on itself.')
            cycle = c.execute("WITH RECURSIVE chain(id) AS (SELECT depends_on FROM dependencies WHERE ticket_id=? UNION SELECT d.depends_on FROM dependencies d JOIN chain c ON d.ticket_id=c.id) SELECT id FROM chain WHERE id=?", (dependency['id'],ticket['id'])).fetchone()
            if cycle:
                raise AgileCodeError('This dependency would create a cycle.')
            c.execute('INSERT OR IGNORE INTO dependencies VALUES(?,?)', (ticket['id'],dependency['id']))
        from contextlib import AsyncExitStack
        async with AsyncExitStack() as stack:
            for identity in sorted({ticket['id'],dependency['id']}):
                await stack.enter_async_context(self.coordinator.reserve(identity,uid(),'dependency'))
            cancellation_checkpoint()
            await self.store.transaction(insert)
        self.events.emit(Event('TicketChanged', ticket['id']))



    @project_mutation
    async def remove_dependency(self, ticket_id: str, depends_on: str):
        ticket = await self.find_ticket(ticket_id); dependency = await self.find_ticket(depends_on)
        cancellation_checkpoint()
        await self.store.execute("DELETE FROM dependencies WHERE ticket_id=? AND depends_on=?", (ticket["id"],dependency["id"]))
        self.events.emit(Event("TicketChanged",ticket["id"]))


    @staticmethod
    def _raise_move_rejection(operation):
        if operation and operation['status'] == 'interrupted':
            raise OperationInterrupted('This movement was interrupted before its board commit.', result=decode(operation['result']))
        if operation and operation['status'] in {'rejected', 'blocked', 'conflict'}:
            result = decode(operation['result'], {})
            exception = Conflict if result.get('error_type') == 'Conflict' or operation['status'] != 'rejected' else AgileCodeError
            raise exception(result.get('reason') or 'This movement request was rejected.')

    async def _record_move_failure(self, identity, ticket_id, original, exc, *, owns_reservation, uncertain):
        """Bind a settled rejection; never terminalize a competing request's work."""
        result = dict(board_committed=False, changed=False, execution='blocked', reason=str(exc),
                      error_type=type(exc).__name__, operation_id=identity)
        status = 'interrupted' if isinstance(exc, OperationInterrupted) else ('blocked' if uncertain else 'rejected')
        def record(c):
            prior = c.execute('SELECT * FROM operations WHERE id=?', (identity,)).fetchone()
            if prior:
                self._check_identity(prior, 'move_ticket', original)
                if not owns_reservation or prior['status'] != 'reserved':
                    return False
                c.execute('UPDATE operations SET status=?,result=?,updated=? WHERE id=?',
                          (status, encode(result), now(), identity))
            else:
                c.execute('INSERT INTO operations(id,kind,owner_id,data,status,result,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                          (identity, 'move_ticket', ticket_id, encode({'original': original}), status, encode(result), now(), now()))
            return True
        recorded = await self.store.transaction(record)
        if recorded and uncertain:
            async with self.coordinator.lock:
                self.coordinator.blocked_tickets.setdefault(ticket_id,
                    'Movement could not establish that ticket work stopped. Inspect the retained activity before retrying: ' + str(exc))

    @owned_operation
    async def move_ticket(self, ticket_id: str, state: str, *, expected_revision=None, position=None,
                          allow_dirty_base=False, request_id=None, caller='manual', completion_origin=None,
                          activate=True) -> dict:
        import math
        target = BoardState(state).value
        if position is not None and not math.isfinite(position):
            raise AgileCodeError('Card position must be finite.')
        ticket = await self.find_ticket(ticket_id)
        identity = request_id or uid()
        original = dict(ticket_id=ticket['id'], state=target, expected_revision=expected_revision,
                        position=position, allow_dirty_base=bool(allow_dirty_base), caller=caller,
                        completion_origin=completion_origin, activate=bool(activate))
        prior = await self._prior(identity, 'move_ticket', original)
        self._raise_move_rejection(prior)
        if prior and prior['status'] == 'completed':
            return {**await self.find_ticket(ticket['id']), 'movement': decode(prior['result'])}
        owns_reservation = stopping = False
        try:
            async with self.coordinator.reserve(ticket['id'], identity, 'move'):
                owns_reservation = True
                try:
                    ticket = await self.find_ticket(ticket['id'])
                    prior = await self._prior(identity, 'move_ticket', original)
                    self._raise_move_rejection(prior)
                    if prior and prior['status'] == 'completed':
                        return {**ticket, 'movement': decode(prior['result'])}
                    if prior and prior['status'] == 'board_committed':
                        result = decode(prior['result'])
                    else:
                        revision = ticket['revision'] if expected_revision is None else expected_revision
                        if not prior:
                            await self.store.insert('operations', dict(id=identity, kind='move_ticket', owner_id=ticket['id'],
                                data=encode({'original': original, 'revision': revision}), status='reserved', created=now(), updated=now()))
                        else:
                            revision = decode(prior['data'])['revision']
                        if completion_origin and (ticket.get('selected_run_id') != completion_origin['run_id'] or
                                                  ticket.get('board_transition_id') != completion_origin['transition_id'] or
                                                  ticket['state'] != 'In Progress'):
                            result = dict(board_committed=False, changed=False, execution='stale_completion',
                                          reason='Completion belongs to an earlier execution or board transition.', operation_id=identity)
                            await self.store.update('operations', identity, status='completed', result=encode(result), updated=now())
                            return {**ticket, 'movement': result}
                        if revision != ticket['revision']:
                            raise Conflict('The board card changed; refresh before moving it.')
                        if completion_origin and self.coordinator.activities.get(ticket['id']):
                            raise Conflict('Completion cannot move the board until owned activity and cleanup settle.')
                        changed = target != ticket['state']
                        # This wait is outside all short locks. Reservation prevents new work.
                        if changed and ticket['state'] == 'In Progress' and self.execution and not completion_origin:
                            stopping = True
                            await self.execution.stop_ticket(ticket['id'])
                            stopping = False
                        def commit(c):
                            current = c.execute('SELECT * FROM tickets WHERE id=?', (ticket['id'],)).fetchone()
                            if current['revision'] != revision:
                                raise Conflict('Ticket changed while execution was settling; refresh before moving it.')
                            if completion_origin and (current['selected_run_id'] != completion_origin['run_id'] or current['board_transition_id'] != completion_origin['transition_id']):
                                raise Conflict('Completion became stale while execution was settling.')
                            if changed and target == 'In Progress' and isinstance(caller, dict) and caller.get('scope') == 'project_manager':
                                limit = caller.get('admission_limit')
                                count = c.execute("SELECT COUNT(*) FROM tickets WHERE state='In Progress' AND archived=0 AND id<>?", (ticket['id'],)).fetchone()[0]
                                if limit is not None and count >= limit:
                                    raise AgileCodeError(f'Maximum concurrent tickets ({limit}) is already reached.')
                            if changed or position is not None:
                                fields = {'revision': revision+1, 'updated': now()}
                                if changed:
                                    fields.update(state=target, board_transition_id=identity)
                                if position is not None:
                                    fields['position'] = float(position)
                                c.execute('UPDATE tickets SET '+','.join(k+'=?' for k in fields)+' WHERE id=? AND revision=?', (*fields.values(), ticket['id'], revision))
                            result = dict(board_committed=True, changed=changed, execution='unchanged' if not changed else 'stopped', reason=None, operation_id=identity)
                            c.execute("UPDATE operations SET status='board_committed',result=?,updated=? WHERE id=?", (encode(result), now(), identity))
                            return result
                        cancellation_checkpoint()
                        result = await self.store.transaction(commit)
                        if changed or position is not None:
                            self.events.emit(Event('TicketChanged', ticket['id']))
                except BaseException as exc:
                    # The durable rejection/blocker is visible before releasing
                    # admission, including when stopping physical work failed.
                    await self._record_move_failure(identity, ticket['id'], original, exc,
                                                    owns_reservation=True, uncertain=stopping)
                    raise
        except BaseException as exc:
            if not owns_reservation:
                await self._record_move_failure(identity, ticket['id'], original, exc,
                                                owns_reservation=False, uncertain=False)
            raise
        # Durable board commit precedes continuation and reservation is released.
        if result['changed'] and target == 'In Progress' and activate:
            if not self.execution or not self.execution.execution_enabled:
                result.update(execution='unavailable', reason='Board saved. This host does not run execution.')
            else:
                try:
                    cancellation_checkpoint()
                    outcome = await self.execution.continue_ticket(ticket['id'], allow_dirty_base=allow_dirty_base,
                        expected_transition=identity, request_id=identity+':continue')
                    result.update(execution=outcome['execution'], reason=outcome.get('reason'))
                except OperationInterrupted as exc:
                    # The board movement is a settled fact. Interruption stops
                    # activation without undoing that user's board transition.
                    result.update(execution='interrupted', reason=str(exc))
                except Exception as exc:
                    result.update(execution='blocked', reason=str(exc))
        await self.store.update('operations', identity, status='completed', result=encode(result), updated=now())
        return {**await self.find_ticket(ticket['id']), 'movement': result}



    @owned_operation
    async def archive_ticket(self, ticket_id: str, archived=True):
        ticket = await self.find_ticket(ticket_id)
        async with self.coordinator.reserve(ticket['id'], uid(), 'archive'):
            facts = await self.queries.ticket_execution(ticket['id'])
            run = facts['run']
            if facts['blocking_reason']:
                raise Conflict(facts['blocking_reason'])
            if archived and run and not facts['finished'] and run['state'] not in {'cancelled','failed'}:
                raise AgileCodeError('Finish integration or cancel the run before archiving. Unmerged files/history will be retained.')
            cancellation_checkpoint()
            await self.store.execute('UPDATE tickets SET archived=?,revision=revision+1,updated=? WHERE id=?', (int(archived),now(),ticket['id']))
        self.events.emit(Event('TicketChanged',ticket['id']))



