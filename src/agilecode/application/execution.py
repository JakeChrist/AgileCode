"""Shared application operations, extracted from the public compatibility facade."""
from __future__ import annotations
import asyncio
import json
import re
import shutil
import sqlite3
import traceback
from pathlib import Path
from agilecode.core.types import AgileCodeError, Conflict, NeedsReview, GenerationStopped, Team, Event, uid, now
from agilecode.adapters.store import encode, decode
from agilecode.adapters.workspace import Workspace, digest, inside

from .coordination import owned_operation, settle, project_mutation
from .cancellation import cancellation_checkpoint, current_cancellation, detached_cancellation, OperationInterrupted
from .process_operations import ensure_commit_stage
from .queries import read_only_process


from .eligibility import execution_eligibility


INTERRUPTED_DELETE_BLOCKER = ('Deletion stopped after removing some ticket-owned resources. Inspect retained files and explicitly retry deletion; execution cannot resume.')


def erase_attempt_receipts_in(c, attempts):
    """Erase journal/transcript metadata with the histories they describe."""
    for attempt in attempts:
        session = attempt['session_id']
        tools = c.execute('SELECT id FROM tools WHERE session_id=?', (session,)).fetchall()
        for tool in tools:
            identities = [tool['id']] + [prefix + tool['id'] for prefix in
                ('conversation-result:', 'conversation-dispatch:', 'conversation-reconciliation:')]
            c.executemany('DELETE FROM operations WHERE id=?', ((identity,) for identity in identities))
        c.execute('DELETE FROM operations WHERE owner_id IN (?,?)', (session, attempt['id']))
        c.execute('DELETE FROM meta WHERE key=?', ('conversation:' + session,))


class ExecutionOperations:
    def __init__(self, *, root, global_root, storage, store, git, config, events, coordinator,
                 queries, tickets, checkpoints, processor=None, skills=None, client=None,
                 discussion=None, project_manager=None, execution_enabled=True, jobs=None):
        self.root, self.global_root, self.storage = Path(root), Path(global_root), Path(storage)
        self.store, self.git, self.config, self.events = store, git, config, events
        self.coordinator, self.queries, self.tickets = coordinator, queries, tickets
        self.checkpoints, self.processor, self.skills, self.client = checkpoints, processor, skills, client
        self.discussion, self.project_manager = discussion, project_manager
        self.execution_enabled = execution_enabled
        self.runtimes, self.tasks, self.integration_tasks = {}, queries.tasks, {}
        self.ticket_locks = {}
        self.pending_answers = {}
        self._all_execution_lock = asyncio.Lock()
        self._deferred_integrations = set()
        self.merges = None
        if jobs is None:
            from .services.jobs import JobService
            jobs = JobService(store, runtime_supplier=lambda: list(self.runtimes.values()) +
                              (list(self.merges.runtimes.values()) if self.merges else []))
        self.jobs = jobs

    @property
    def all_paused(self):
        return self.coordinator.paused

    @all_paused.setter
    def all_paused(self, value):
        self.coordinator.paused = bool(value)

    def _ticket_lock(self, ticket_id):
        return self.ticket_locks.setdefault(ticket_id, asyncio.Lock())

    async def find_ticket(self, ticket_id):
        return await self.queries.find_ticket(ticket_id)

    async def selected_run(self, ticket_id):
        # This private host's callers operate on selected work, not history.
        return await self.queries.selected_run(ticket_id)

    async def active_run(self, ticket_id):
        return await self.queries.active_run(ticket_id)

    async def move_ticket(self, *args, **kwargs):
        return await self.tickets.move_ticket(*args, **kwargs)

    async def require_eligible(self, ticket_id, intent='run', *, expected_transition=None):
        facts = await self.queries.ticket_execution(ticket_id)
        reason = execution_eligibility(facts['ticket'], facts, self.execution_controls(), intent)
        if not self.execution_enabled:
            reason = 'This host does not run execution.'
        if expected_transition is not None and facts['transition_id'] != expected_transition:
            reason = 'Board transition changed before execution could start.'
        if reason:
            raise Conflict(reason)
        return facts

    async def stop_ticket(self, ticket_id):
        # Caller holds a reservation, not a lock needed by completion/cleanup.
        if not self.execution_enabled:
            unfinished = await self.store.one("SELECT 1 FROM runs WHERE ticket_id=? AND state IN ('running','waiting','pause_requested','provisioning')", (ticket_id,))
            uncertain = await self.store.one("SELECT 1 FROM integrations WHERE ticket_id=? AND state IN ('preparing','applying','uncertain')", (ticket_id,))
            if unfinished or uncertain:
                raise Conflict('Open an execution-capable host to reconcile interrupted work before moving this ticket.')
        before = await self.active_run(ticket_id)
        cancellation_checkpoint()
        was_active = bool(before and before['state'] in {'running','waiting','pause_requested','provisioning'})
        if before and self.processor:
            await self.processor.agent.stop_run(before['id'])
        await self.coordinator.stop(ticket_id)
        if self.merges:
            await self.merges.pause_ticket(ticket_id)
        # Cancellation may already have stored 'paused'. Preserve the user's
        # explicit pause sentinel so a subsequently supplied answer cannot restart it.
        return await self._pause_run(ticket_id, active_only=not was_active)

    async def continue_ticket(self, ticket_id, *, allow_dirty_base=False, expected_transition=None, request_id=None):
        cancellation_checkpoint()
        if self.all_paused:
            return {'execution':'paused', 'reason':'Everything is paused.'}
        facts = await self.require_eligible(ticket_id, expected_transition=expected_transition)
        run = facts['run']
        if not run:
            await self._start_new(ticket_id, allow_dirty_base=allow_dirty_base, expected_transition=expected_transition, request_id=request_id)
            return {'execution':'started'}
        if facts['classification'] == 'awaiting_integration':
            await self.prepare_integration(ticket_id, wait=False, resume=True)
            return {'execution':'integration_started'}
        if run['state'] in {'paused','failed','cancelled','running','waiting','provisioning'}:
            if run['state'] in {'running','waiting','provisioning'} and self.tasks.get(run['id']) and not self.tasks[run['id']].done():
                return {'execution':'running'}
            await self.resume_run(ticket_id, expected_transition=expected_transition)
            return {'execution':'resumed'}
        return {'execution':'finished'}

    @owned_operation
    async def delete_ticket(self,ticket_id,*,request_id=None):
        identity = request_id or 'delete:' + ticket_id
        original = {'ticket_id':ticket_id}
        prior = await self.tickets._prior(identity,'delete_ticket',original)
        if prior and prior['status'] == 'completed':
            return decode(prior['result'])
        if prior and prior['status'] == 'interrupted':
            if request_id:
                raise OperationInterrupted('This deletion request was stopped; a new explicit request is required.', result=decode(prior['result']))
            # A fresh manual invocation can deliberately finish a stopped
            # deletion. Startup only replays deleting_resources, never this state.
            await self.store.update('operations',identity,status='prepared',result=None,updated=now())
        ticket = await self.find_ticket(ticket_id)
        if not prior:
            await self.store.insert('operations',dict(id=identity,kind='delete_ticket',owner_id=identity,
                data=encode({'original':original,'ticket_id':ticket['id']}),status='prepared',created=now(),updated=now()))
        try:
            result = await self._delete_ticket(ticket['id'],identity)
        except OperationInterrupted as exc:
            journal = await self.store.get('operations',identity)
            changed = journal['status'] == 'deleting_resources'
            result = {'ticket':ticket['id'],'deleted':False,'resources_may_have_changed':changed,'reason':str(exc)}
            await self.store.update('operations',identity,status='interrupted',result=encode(result),updated=now())
            if changed:
                self.coordinator.blocked_tickets[ticket['id']] = INTERRUPTED_DELETE_BLOCKER
            exc.result = result
            raise
        await self.store.update('operations',identity,status='completed',result=encode(result),updated=now())
        return result

    async def _delete_ticket(self, ticket_id, operation_id):
        """Permanently erase an unintegrated ticket and every resource it owns."""
        ticket = await self.find_ticket(ticket_id)
        async with self.coordinator.reserve(ticket['id'],operation_id,'delete',resolving_blocker=INTERRUPTED_DELETE_BLOCKER):
            await self.stop_ticket(ticket['id'])
            cancellation_checkpoint()
            runs = await self.store.query(
                "SELECT * FROM runs WHERE ticket_id=? ORDER BY created,id",
                (ticket["id"],),
            )
            integrations = await self.store.query(
                "SELECT * FROM integrations WHERE ticket_id=? ORDER BY created,id",
                (ticket["id"],),
            )

            if ticket["state"] == "Done" or any(run["state"] == "integrated" for run in runs) or any(
                record["state"] == "integrated" for record in integrations
            ):
                raise AgileCodeError(
                    "Integrated/completed tickets cannot be deleted. Their changes are already part of the project history."
                )

            inbound = await self.store.query(
                "SELECT t.key FROM dependencies d JOIN tickets t ON t.id=d.ticket_id "
                "WHERE d.depends_on=? ORDER BY t.key",
                (ticket["id"],),
            )
            children = await self.store.query(
                "SELECT key FROM tickets WHERE parent_id=? ORDER BY key",
                (ticket["id"],),
            )
            if inbound or children:
                references = [row["key"] for row in inbound] + [row["key"] for row in children]
                raise Conflict(
                    "Delete blocked because other tickets reference this ticket: "
                    + ", ".join(references)
                    + ". Remove those references explicitly first; deletion never edits another ticket."
                )

            # Refuse deletion if Git shows that ticket code already reached its target
            # branch even if the database did not finish recording integration.
            for run in runs:
                if run["final_oid"]:
                    merged = await self.git.run(
                        "merge-base", "--is-ancestor", run["final_oid"],
                        f"refs/heads/{run['base_branch']}", check=False,
                    )
                    if merged.code == 0:
                        raise AgileCodeError(
                            "Ticket commit is already reachable from its target branch; deletion would not remove it from project history."
                        )
            for record in integrations:
                if record["candidate_oid"]:
                    merged = await self.git.run(
                        "merge-base", "--is-ancestor", record["candidate_oid"],
                        f"refs/heads/{record['target_branch']}", check=False,
                    )
                    if merged.code == 0:
                        raise AgileCodeError(
                            "An integration candidate is already reachable from its target branch; this ticket cannot be erased safely."
                        )

            run_root = (self.storage / "worktrees").resolve()
            integration_root = (self.storage / "integration").resolve()

            def owned_path(value: str, root: Path, label: str) -> Path:
                path = Path(value).resolve()
                if path == root or not inside(path, root):
                    raise Conflict(f"Recorded {label} path is outside AgileCode-owned storage: {path}")
                return path

            run_paths = {
                run["id"]: owned_path(run["workspace"], run_root, "ticket worktree")
                for run in runs
            }
            integration_paths = {
                record["id"]: owned_path(record["workspace"], integration_root, "integration worktree")
                for record in integrations
            }

            # Validate branch/worktree ownership before destroying anything.
            worktrees = await self.git.worktrees()
            for run in runs:
                path = run_paths[run["id"]]
                ref = f"refs/heads/{run['branch']}"
                registered = worktrees.get(str(path))
                if registered and registered.get("branch") != ref:
                    raise Conflict("Recorded ticket worktree no longer owns its ticket branch.")
                other_users = [
                    worktree for worktree, data in worktrees.items()
                    if data.get("branch") == ref and Path(worktree).resolve() != path
                ]
                if other_users:
                    raise Conflict(
                        "Ticket branch is checked out outside its recorded ticket worktree: "
                        + ", ".join(other_users[:3])
                    )

            # Stop every live activity owned by the ticket before removing files.
            cancellation_checkpoint()
            for run in runs:
                runtime = self.runtimes.get(run["id"])
                if runtime:
                    runtime.control.cancelled = True
                    runtime.control.permit.set()
            tasks = [
                self.tasks[run["id"]]
                for run in runs
                if run["id"] in self.tasks and not self.tasks[run["id"]].done()
            ]
            integration_task = self.integration_tasks.get(ticket["id"])
            if integration_task and not integration_task.done():
                tasks.append(integration_task)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            # Integration preparation can race with a deletion request. Re-read its
            # durable state after cancellation and re-check target reachability
            # before any ticket-owned Git state is destroyed.
            runs = await self.store.query(
                "SELECT * FROM runs WHERE ticket_id=? ORDER BY created,id",
                (ticket["id"],),
            )
            integrations = await self.store.query(
                "SELECT * FROM integrations WHERE ticket_id=? ORDER BY created,id",
                (ticket["id"],),
            )
            if any(run["state"] == "integrated" for run in runs) or any(
                record["state"] == "integrated" for record in integrations
            ):
                raise AgileCodeError(
                    "Ticket became integrated while deletion was being prepared; no ticket-owned Git state was deleted."
                )
            for run in runs:
                if run["final_oid"]:
                    merged = await self.git.run(
                        "merge-base", "--is-ancestor", run["final_oid"],
                        f"refs/heads/{run['base_branch']}", check=False,
                    )
                    if merged.code == 0:
                        raise AgileCodeError(
                            "Ticket commit reached its target branch while deletion was being prepared; deletion was stopped."
                        )
            for record in integrations:
                if record["candidate_oid"]:
                    merged = await self.git.run(
                        "merge-base", "--is-ancestor", record["candidate_oid"],
                        f"refs/heads/{record['target_branch']}", check=False,
                    )
                    if merged.code == 0:
                        raise AgileCodeError(
                            "Integration candidate reached its target branch while deletion was being prepared; deletion was stopped."
                        )
            run_paths = {
                run["id"]: owned_path(run["workspace"], run_root, "ticket worktree")
                for run in runs
            }
            integration_paths = {
                record["id"]: owned_path(record["workspace"], integration_root, "integration worktree")
                for record in integrations
            }

            # A destructive delete may only consume resources uniquely owned by
            # this ticket. Inconsistent shared ownership is a hard stop rather
            # than permission to mutate another ticket.
            worktrees = await self.git.worktrees()
            for run in runs:
                other = await self.store.one(
                    "SELECT t.key FROM runs r JOIN tickets t ON t.id=r.ticket_id "
                    "WHERE r.ticket_id<>? AND (r.workspace=? OR r.branch=?) LIMIT 1",
                    (ticket["id"], run["workspace"], run["branch"]),
                )
                if other:
                    raise Conflict(f"Ticket Git resources are also recorded by {other['key']}; deletion stopped.")
                path = run_paths[run["id"]]
                ref = f"refs/heads/{run['branch']}"
                registered = worktrees.get(str(path))
                if registered and registered.get("branch") != ref:
                    raise Conflict("Recorded ticket worktree no longer owns its ticket branch.")
                other_users = [
                    worktree for worktree, data in worktrees.items()
                    if data.get("branch") == ref and Path(worktree).resolve() != path
                ]
                if other_users:
                    raise Conflict(
                        "Ticket branch is checked out outside its recorded ticket worktree: "
                        + ", ".join(other_users[:3])
                    )
            for record in integrations:
                other = await self.store.one(
                    "SELECT t.key FROM integrations i JOIN tickets t ON t.id=i.ticket_id "
                    "WHERE i.ticket_id<>? AND i.workspace=? LIMIT 1",
                    (ticket["id"], record["workspace"]),
                )
                if other:
                    raise Conflict(f"Integration worktree is also recorded by {other['key']}; deletion stopped.")

            for run in runs:
                runtime = self.runtimes.pop(run["id"], None)
                if runtime:
                    await runtime.shell.close()
                    await runtime.mcp.close()
                self.tasks.pop(run["id"], None)
            self.integration_tasks.pop(ticket["id"], None)
            for record in integrations:
                runtime = self.merges.runtimes.pop(record["id"], None) if self.merges else None
                if runtime:
                    await runtime.shell.close()
                    await runtime.mcp.close()

            async def discard_unregistered(path: Path):
                cancellation_checkpoint()
                if path.is_symlink():
                    await asyncio.to_thread(path.unlink)
                elif path.exists():
                    await asyncio.to_thread(shutil.rmtree, path)

            cancellation_checkpoint()
            await self.store.update('operations',operation_id,status='deleting_resources',updated=now())
            # Integration worktrees/refs are ticket-owned scratch state.
            for record in integrations:
                path = integration_paths[record["id"]]
                if str(path) in await self.git.worktrees():
                    cancellation_checkpoint()
                    await self.git.delete_owned_worktree(path)
                else:
                    await discard_unregistered(path)
                ref = f"refs/agilecode/integrations/{record['id']}"
                actual = await self.git.run("rev-parse", "--verify", ref, check=False)
                if actual.code == 0:
                    cancellation_checkpoint()
                    await self.git.run("update-ref", "-d", ref, actual.text)

            checkpoint_prefix = f"refs/agilecode/checkpoints/{ticket['id']}/"
            checkpoint_refs = (
                await self.git.run("for-each-ref", "--format=%(refname)", checkpoint_prefix)
            ).text.splitlines()
            for ref in checkpoint_refs:
                actual = await self.git.run("rev-parse", "--verify", ref, check=False)
                if actual.code == 0:
                    cancellation_checkpoint()
                    await self.git.run("update-ref", "-d", ref, actual.text)

            # Ticket worktrees are deliberately removed even when dirty. Once all
            # worktrees are gone, their dedicated branches are forcibly deleted.
            for run in runs:
                path = run_paths[run["id"]]
                if str(path) in await self.git.worktrees():
                    cancellation_checkpoint()
                    await self.git.delete_owned_worktree(path, run["branch"])
                else:
                    await discard_unregistered(path)
            for run in runs:
                cancellation_checkpoint()
                await self.git.delete_owned_branch(run["branch"])

            artifact_root = (self.storage / "artifacts" / ticket["id"]).resolve()
            artifacts_parent = (self.storage / "artifacts").resolve()
            if artifact_root != artifacts_parent and inside(artifact_root, artifacts_parent):
                await discard_unregistered(artifact_root)

            owned_tool_ids = {
                row["id"] for row in await self.store.query(
                    "SELECT tools.id FROM tools JOIN attempts ON attempts.session_id=tools.session_id "
                    "JOIN runs ON runs.id=attempts.run_id WHERE runs.ticket_id=?",
                    (ticket["id"],),
                )
            }
            owned_job_ids = {
                row["id"] for row in await self.store.query(
                    "SELECT id FROM jobs WHERE ticket_id=?",
                    (ticket["id"],),
                )
            }
            run_ids = {run["id"] for run in runs}
            orphan_activity_ids = []
            for row in await self.store.query("SELECT * FROM activity WHERE ticket_id IS NULL"):
                data = decode(row["data"], {})
                if (
                    data.get("id") in owned_tool_ids
                    or data.get("id") in owned_job_ids
                    or data.get("run_id") in run_ids
                ):
                    orphan_activity_ids.append(row["id"])

            def erase(c):
                erase_attempt_receipts_in(c, c.execute(
                    'SELECT a.id,a.session_id FROM attempts a JOIN runs r ON r.id=a.run_id WHERE r.ticket_id=?',
                    (ticket['id'],)).fetchall())
                c.execute(
                    "DELETE FROM operations WHERE owner_id=? "
                    "OR owner_id IN (SELECT id FROM runs WHERE ticket_id=?) "
                    "OR owner_id IN (SELECT id FROM integrations WHERE ticket_id=?)",
                    (ticket["id"], ticket["id"], ticket["id"]),
                )
                c.execute(
                    "DELETE FROM evidence WHERE attempt_id IN "
                    "(SELECT a.id FROM attempts a JOIN runs r ON r.id=a.run_id WHERE r.ticket_id=?)",
                    (ticket["id"],),
                )
                c.execute("DELETE FROM waits WHERE ticket_id=?", (ticket["id"],))
                c.execute("DELETE FROM jobs WHERE ticket_id=?", (ticket["id"],))
                c.execute(
                    "DELETE FROM tools WHERE session_id IN "
                    "(SELECT a.session_id FROM attempts a JOIN runs r ON r.id=a.run_id WHERE r.ticket_id=?)",
                    (ticket["id"],),
                )
                c.execute(
                    "DELETE FROM requests WHERE session_id IN "
                    "(SELECT a.session_id FROM attempts a JOIN runs r ON r.id=a.run_id WHERE r.ticket_id=?)",
                    (ticket["id"],),
                )
                c.execute(
                    "DELETE FROM messages WHERE session_id IN "
                    "(SELECT a.session_id FROM attempts a JOIN runs r ON r.id=a.run_id WHERE r.ticket_id=?)",
                    (ticket["id"],),
                )
                c.execute(
                    "DELETE FROM attempts WHERE run_id IN (SELECT id FROM runs WHERE ticket_id=?)",
                    (ticket["id"],),
                )
                c.execute("DELETE FROM checkpoints WHERE ticket_id=?", (ticket["id"],))
                c.execute("DELETE FROM integrations WHERE ticket_id=?", (ticket["id"],))
                c.execute("DELETE FROM artifacts WHERE ticket_id=?", (ticket["id"],))
                c.execute("DELETE FROM runs WHERE ticket_id=?", (ticket["id"],))
                c.execute("DELETE FROM discussion_tickets WHERE ticket_id=?", (ticket["id"],))
                c.execute("DELETE FROM dependencies WHERE ticket_id=?", (ticket["id"],))
                c.execute("DELETE FROM activity WHERE ticket_id=?", (ticket["id"],))
                if orphan_activity_ids:
                    marks = ",".join("?" for _ in orphan_activity_ids)
                    c.execute(f"DELETE FROM activity WHERE id IN ({marks})", tuple(orphan_activity_ids))
                c.execute("DELETE FROM tickets WHERE id=?", (ticket["id"],))
                c.execute("UPDATE operations SET status='completed',result=?,updated=? WHERE id=?",
                          (encode({'ticket':ticket['id'],'key':ticket['key'],'deleted':True}),now(),operation_id))
            cancellation_checkpoint()
            await self.store.transaction(erase)

            result = {"ticket": ticket["id"], "key": ticket["key"], "deleted": True}
            self.events.emit(Event("TicketDeleted", ticket["id"], data={"key": ticket["key"]}))

        if self.coordinator.blocked_tickets.get(ticket["id"]) == INTERRUPTED_DELETE_BLOCKER:
            self.coordinator.blocked_tickets.pop(ticket["id"], None)
        self.ticket_locks.pop(ticket["id"], None)
        return result


    @owned_operation
    async def start_ticket(self, ticket_id, *, allow_dirty_base=False, expected_revision=None,
                           request_id=None, caller='manual', provider_id=None):
        original = dict(ticket_id=ticket_id,allow_dirty_base=bool(allow_dirty_base),expected_revision=expected_revision,caller=caller,provider_id=provider_id)
        if request_id:
            prior = await self.tickets._prior(request_id,'start_ticket',original)
            if prior and prior['status'] == 'interrupted':
                raise OperationInterrupted('This start request was interrupted; inspect its retained state before issuing a new request.', result=decode(prior['result']))
            if prior and prior['result']:
                return await self.store.get('runs',decode(prior['result'])['run_id'])
            if not prior:
                await self.store.insert('operations',dict(id=request_id,kind='start_ticket',owner_id=ticket_id,data=encode({'original':original}),status='prepared',created=now(),updated=now()))
        try:
            result = await self._start_ticket(ticket_id,allow_dirty_base=allow_dirty_base,expected_revision=expected_revision,request_id=request_id,caller=caller,provider_id=provider_id)
        except OperationInterrupted as exc:
            if request_id:
                await self.store.update('operations',request_id,status='interrupted',
                    result=encode(exc.result or {'execution':'interrupted','reason':str(exc)}),updated=now())
            raise
        if request_id:
            await self.store.update('operations',request_id,status='completed',result=encode({'run_id':result['id']}),updated=now())
        return result

    async def _start_ticket(self, ticket_id, *, allow_dirty_base=False, expected_revision=None,
                           request_id=None, caller='manual', provider_id=None):
        self._require_execution_enabled()
        ticket = await self.find_ticket(ticket_id)
        if expected_revision is not None and ticket['revision'] != expected_revision:
            raise Conflict('Ticket moved before execution could start.')
        if ticket['state'] not in {'Ready','In Progress'} or ticket['archived']:
            raise AgileCodeError('Move the ticket to Ready or In Progress before starting execution.')
        if isinstance(caller, dict) and caller.get('scope') == 'project_manager':
            changes = {'merge_auto':True}
            if provider_id is not None:
                changes['provider_id'] = provider_id
            ticket = await self.tickets.edit_ticket(ticket['id'], expected_revision=ticket['revision'], **changes)
        if ticket['state'] != 'In Progress':
            ticket = await self.move_ticket(ticket['id'], 'In Progress', expected_revision=ticket['revision'],
                request_id=(request_id + ':move') if request_id else None, caller=caller, activate=False)
        facts = await self.queries.ticket_execution(ticket['id'])
        run = facts['run']
        if run and run['state'] not in {'failed','cancelled','integrated'} and not (run['state'] == 'completed' and not run['final_oid'] and read_only_process(run)):
            return run
        return await self._start_new(ticket['id'], allow_dirty_base=allow_dirty_base,
                                     expected_transition=ticket['board_transition_id'], request_id=request_id)

    async def _start_new(self, ticket_id, *, allow_dirty_base=False, expected_transition=None, request_id=None):
        owner = request_id or uid()
        async with self.coordinator.reserve(ticket_id, owner, 'start'):
            facts = await self.queries.ticket_execution(ticket_id)
            ticket, existing = facts['ticket'], facts['run']
            # Explicit Start keeps the established replacement policy for stopped/integrated work.
            legacy = bool(existing and existing['state'] == 'completed' and not existing['final_oid'] and read_only_process(existing))
            eligibility = dict(facts, finished=False) if existing and (existing['state'] in {'failed','cancelled','integrated'} or legacy) else facts
            reason = execution_eligibility(ticket, eligibility, self.execution_controls())
            if reason:
                raise Conflict(reason)
            if expected_transition and ticket['board_transition_id'] != expected_transition:
                raise Conflict('Board transition changed before execution could start.')
            if existing and existing['state'] not in {'failed','cancelled','integrated'} and not legacy:
                return existing
            unmet = await self.store.query("SELECT t.key FROM tickets t JOIN dependencies d ON t.id=d.depends_on WHERE d.ticket_id=? AND t.state<>'Done'", (ticket_id,))
            if unmet:
                raise AgileCodeError('Incomplete dependencies: ' + ', '.join(t['key'] for t in unmet))
            branch, base = await self.git.branch(), await self.git.head()
            from .prerequisites import prerequisite_inputs
            await prerequisite_inputs(self, ticket_id, branch)
            if await self.git.dirty() and not allow_dirty_base:
                raise AgileCodeError('The original checkout has uncommitted changes. Commit them, or explicitly start from committed HEAD; uncommitted changes are never copied into a ticket.')
            team = ensure_commit_stage(await self.config.get_team(ticket['team_ref']), ticket['process_id'])
            identity = uid()
            ticket_branch = f"agilecode/{ticket['key']}/{identity[:8]}"
            path = self.storage/'worktrees'/f"{ticket['key']}-{identity[:8]}"
            ticket_snapshot = dict(ticket)
            source_context = await self.discussion.context_for_ticket(ticket_id) if self.discussion else None
            if source_context:
                ticket_snapshot['source_discussion_context'] = source_context
            provider = self.config.get_provider(ticket['provider_id'])
            snapshot = {'ticket':ticket_snapshot, 'team':team.model_dump(mode='json'), 'process_id':ticket['process_id'],
                        'ticket_provider_id':ticket['provider_id'], 'provider_id':provider.id, 'dirty_base_acknowledged':allow_dirty_base}
            run = dict(id=identity,ticket_id=ticket_id,snapshot=encode(snapshot),state='provisioning',step_index=0,outputs='{}',
                       corrections=0,correction=None,workspace=str(path),branch=ticket_branch,base_branch=branch,base_oid=base,
                       final_oid=None,final_tree=None,error=None,created=now(),updated=now(),board_transition_id=ticket['board_transition_id'])
            def claim(c):
                current = c.execute('SELECT * FROM tickets WHERE id=?', (ticket_id,)).fetchone()
                if current['state'] != 'In Progress' or current['board_transition_id'] != ticket['board_transition_id'] or current['revision'] != ticket['revision']:
                    raise Conflict('Ticket moved before provisioning was admitted.')
                c.execute('INSERT INTO runs('+','.join(run)+') VALUES('+','.join('?' for _ in run)+')', tuple(run.values()))
                c.execute('UPDATE tickets SET selected_run_id=? WHERE id=?', (identity,ticket_id))
                c.execute('INSERT INTO operations(id,kind,owner_id,data,status,created,updated) VALUES(?,?,?,?,?,?,?)',
                          ('provision:'+identity,'provision',identity,encode({'transition_id':ticket['board_transition_id']}),'prepared',now(),now()))
            cancellation_checkpoint()
            await self.store.transaction(claim)
            provisioned = False
            try:
                cancellation_checkpoint()
                # Reservation stays live until add_worktree actually settles, including cancellation.
                await settle(self.git.add_worktree(path,ticket_branch,base))
                provisioned = True
                await self.store.update('runs',identity,state='paused',updated=now())
                await self.store.update('operations','provision:'+identity,status='completed',result=encode({'run_id':identity}),updated=now())
                try:
                    cancellation_checkpoint()
                except OperationInterrupted as exc:
                    exc.result = {'run_id':identity, 'ticket_id':ticket_id, 'execution':'paused',
                                  'reason':'Workspace provisioning settled; ticket execution was not launched.'}
                    raise
                if not self.all_paused:
                    await self._launch(await self.store.get('runs',identity), owner)
            except BaseException as exc:
                await self.store.update('runs',identity,state='paused' if path.exists() else 'failed',error=str(exc),updated=now())
                if isinstance(exc, OperationInterrupted) and not provisioned:
                    await self.store.update('operations','provision:'+identity,status='cancelled',
                        result=encode({'run_id':identity,'effect':'not_applied'}),updated=now())
                raise
            self.events.emit(Event('TicketChanged',ticket_id,identity))
            return await self.store.get('runs',identity)

    async def _launch(self, run, owner):
        cancellation_checkpoint()
        task = self.tasks.get(run['id'])
        if task and not task.done():
            return
        await self.require_eligible(run['ticket_id'])
        from .prerequisites import prepare_launch
        run = await prepare_launch(self, run, owner)
        runtime = self.runtimes.get(run['id'])
        if runtime is None:
            runtime = self._runtime(run)
            self.runtimes[run['id']] = runtime
        runtime.control.cancelled = False
        runtime.control.permit.set()
        runtime.operation_owner = owner
        ticket = await self.find_ticket(run['ticket_id'])
        cancellation_checkpoint()
        await self.store.update('runs',run['id'],state='running',error=None,board_transition_id=ticket['board_transition_id'],updated=now())
        try:
            self.tasks[run['id']] = await self.coordinator.spawn(run['ticket_id'],owner,'execution',self._run(runtime),
                check=lambda: self.require_eligible(run['ticket_id']), after=lambda: self._continue_completed(run['id']), name='ticket-'+ticket['key'])
        except BaseException:
            await self.store.update('runs',run['id'],state='paused',error='Execution admission stopped before work began.',updated=now())
            raise

    async def _continue_completed(self, run_id):
        run = await self.store.get('runs',run_id)
        if run['state'] != 'completed':
            return
        facts = await self.queries.ticket_execution(run['ticket_id'])
        if facts['selected_run_id'] != run_id or facts['transition_id'] != run['board_transition_id'] or facts['ticket']['state'] != 'In Progress':
            await self.store.execute("UPDATE operations SET status='superseded',updated=? WHERE id=?",(now(),'continuation:'+run_id))
            return
        try:
            if run['final_oid']:
                await self.prepare_integration(run['ticket_id'],wait=False)
                await self.store.execute("UPDATE operations SET status='dispatched',updated=? WHERE id=?",(now(),'continuation:'+run_id))
        except (AgileCodeError, Conflict) as exc:
            await self.store.activity('ContinuationDeferred', {'run_id':run_id, 'message':str(exc)},run['ticket_id'])



    def _runtime(self, run: dict, *, workspace: Path | None = None, phase="development") -> Runtime:
        from agilecode.core.runner import Runtime
        from agilecode.adapters.shell import ShellService
        from agilecode.adapters.mcp import McpService
        workspace = workspace or Path(run["workspace"])
        settings = self.config.settings
        scope = Workspace(workspace,self.root,self.global_root,read_limit=settings.file_read_page_bytes)
        shell = ShellService(scope,self.store,self.events,run["ticket_id"],run["id"],
                             self.storage/"artifacts"/run["ticket_id"],event_phase=phase,
                             log_limit_bytes=settings.shell_log_max_bytes,
                             preview_bytes=settings.command_preview_bytes)
        runtime = Runtime(run["id"],scope,shell,McpService(self.config.settings.mcp_servers,workspace))
        return runtime


    async def _boundary(self, runtime: Runtime):
        await runtime.control.check()


    async def _run(self, runtime: Runtime):
        from agilecode.adapters.model import ModelUnavailable
        run_id = runtime.run_id
        generation_stopped = False
        try:
            run = await self.store.get("runs",run_id)
            if (runtime.shell.shell.process is None or runtime.shell.shell.process.returncode is not None
                    or runtime.prepared_shell_generation != runtime.shell.shell.generation):
                await runtime.control.check()
                from agilecode.adapters.environment import WorktreeEnvironment
                runtime.environment = await WorktreeEnvironment(runtime.shell, self.config.settings).prepare()
                runtime.prepared_shell_generation = runtime.shell.shell.generation
                await self.store.activity("ShellCreated", {"run_id": run_id, "environment": runtime.environment}, run["ticket_id"])
            # Provider/network loss is not a ticket failure. Resume the same durable
            # attempt after reconnection; previously completed tool calls are read
            # from storage and are never sent for execution a second time.
            reconnect_attempt = 0
            while True:
                await runtime.control.check()
                try:
                    await self.processor.run(runtime)
                    break
                except ModelUnavailable as exc:
                    reconnect_attempt += 1
                    run = await self.store.get("runs", run_id)
                    if run["state"] != "running":
                        raise
                    delay = min(2 ** min(reconnect_attempt, 5), 30)
                    message = f"Provider temporarily unavailable; retrying automatically in {delay}s: {exc}"
                    await self.store.update("runs", run_id, error=message, updated=now())
                    if reconnect_attempt == 1:
                        await self.store.activity("ModelConnectionInterrupted",
                                                  {"run_id": run_id, "message": str(exc)}, run["ticket_id"])
                        attempt = await self.store.one(
                            "SELECT session_id FROM attempts WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
                            (run_id,),
                        )
                        if attempt:
                            await self.store.message(attempt["session_id"], {
                                "role": "error", "content": message,
                            })
                            self.events.emit(Event("ConversationChanged", run["ticket_id"], run_id,
                                                   session_id=attempt["session_id"]))
                    self.events.emit(Event("RunChanged", run["ticket_id"], run_id,
                                           data={"message": message, "retrying": True}))
                    await asyncio.sleep(delay)
            run = await self.store.get('runs', run_id)
            if run['state'] == 'completed':
                await self.store.execute("INSERT INTO operations(id,kind,owner_id,data,status,created,updated) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                    ('continuation:'+run_id,'execution_continuation',run_id,encode({'transition_id':run['board_transition_id']}),'pending',now(),now()))
        except GenerationStopped as exc:
            generation_stopped = True
            run = await self.store.get("runs",run_id)
            if run["state"] != "cancelled":
                await self.store.update("runs",run_id,state="paused",error=str(exc),updated=now())
            await self.store.execute("UPDATE attempts SET status='interrupted' WHERE run_id=? AND status='running'",(run_id,))
            self.events.emit(Event("RunChanged",run["ticket_id"],run_id,data={"message":str(exc)}))
        except asyncio.CancelledError:
            run = await self.store.get("runs",run_id)
            if run["state"] in {"running", "waiting"}:
                await self.store.update("runs",run_id,state="paused",error="Execution interrupted; see the current step conversation for incomplete tool calls.",updated=now())
            await self.store.execute("UPDATE attempts SET status='interrupted' WHERE run_id=? AND status IN ('running','waiting')",(run_id,))
        except Exception as exc:
            run = await self.store.get("runs",run_id)
            if run["state"] not in {"cancelled", "pause_requested"}:
                message = f"{type(exc).__name__}: {exc}"
                await self.store.update("runs",run_id,state="paused",error=message,updated=now())
                attempt = await self.store.one(
                    "SELECT session_id FROM attempts WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
                    (run_id,),
                )
                if attempt:
                    await self.store.message(attempt["session_id"], {
                        "role": "error", "content": message,
                    })
                    self.events.emit(Event("ConversationChanged", run["ticket_id"], run_id,
                                           session_id=attempt["session_id"]))
                await self.store.execute("UPDATE attempts SET status='interrupted' WHERE run_id=? AND status='running'",(run_id,))
                await self.store.activity("ExecutionInterrupted", {
                    "run_id": run_id, "exception": type(exc).__name__,
                    "message": str(exc), "traceback": traceback.format_exc(),
                }, run["ticket_id"])
                self.events.emit(Event("ExecutionInterrupted",run["ticket_id"],run_id,data={"message":message}))
        finally:
            retained_background = False
            if generation_stopped and runtime.shell.has_live_jobs():
                # Stop Generation leaves already-entered background work alone.
                # Transfer its ownership before releasing the model/process task.
                async def watch_background():
                    try:
                        tasks = [task for _,task in runtime.shell.background.values() if not task.done()]
                        if tasks:
                            await asyncio.gather(*tasks,return_exceptions=True)
                    except asyncio.CancelledError:
                        await settle(runtime.shell.close())
                        await settle(runtime.mcp.close())
                        raise
                try:
                    current = await self.store.get('runs',run_id)
                    await self.coordinator.spawn(current['ticket_id'],runtime.operation_owner,'background_jobs',watch_background())
                    retained_background = True
                except Conflict:
                    pass  # A concurrent Pause/Move already owns stopping all work.
            if not retained_background:
                await settle(runtime.shell.close())
                await settle(runtime.mcp.close())
            run = await self.store.get("runs",run_id)
            self.events.emit(Event("TicketChanged",run["ticket_id"],run_id))


    async def wait_for_run(self, run_id: str):
        if run_id in self.tasks:
            await self.tasks[run_id]
        return await self.store.get("runs",run_id)


    async def stop_generation(self, ticket_id: str) -> dict:
        ticket = await self.find_ticket(ticket_id)
        run = await self.active_run(ticket["id"])
        if not run:
            raise AgileCodeError("Ticket has no active execution.")
        session_id = await self.processor.agent.stop_run(run["id"])
        return {"stopped": bool(session_id), "session_id": session_id, "run_id": run["id"]}


    def execution_controls(self) -> dict:
        return {"all_paused": self.all_paused}


    def _require_execution_enabled(self):
        if not self.execution_enabled:
            raise AgileCodeError('This host does not run execution.')
        if self.all_paused:
            raise AgileCodeError("Everything is paused. Use Resume All to continue.")


    async def _persist_execution_controls(self):
        await self.store.execute(
            "INSERT INTO meta(key,value) VALUES('execution_controls',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (encode({**self.execution_controls(), "deferred_integrations": sorted(self._deferred_integrations)}),),
        )
        self.events.emit(Event("ExecutionControlsChanged", data=self.execution_controls()))


    async def _defer_integration(self, ticket_id: str) -> dict:
        self._deferred_integrations.add(ticket_id)
        await self._persist_execution_controls()
        return await self.store.one(
            "SELECT * FROM integrations WHERE ticket_id=? ORDER BY created DESC LIMIT 1", (ticket_id,),
        ) or {"ticket_id": ticket_id, "state": "paused"}


    @owned_operation
    async def pause_all(self):
        async with self._all_execution_lock:
            await self.coordinator.set_paused(True)
            await self._persist_execution_controls()
            errors = []
            manager = self.project_manager
            if manager and manager.state.get('status') != 'off':
                try:
                    await manager.pause()
                except Exception as exc:
                    errors.append(f'Project Manager: {exc}')
            tickets = await self.store.query('SELECT id,key FROM tickets')
            paused = 0
            for ticket in tickets:
                try:
                    # Existing bounded provisioning retains its reservation. Wait
                    # for its actual task before taking the pause reservation.
                    while ticket['id'] in self.coordinator.reservations:
                        await asyncio.sleep(0)
                    async with self.coordinator.reserve(ticket['id'],uid(),'pause'):
                        paused += bool(await self.stop_ticket(ticket['id']))
                except Exception as exc:
                    errors.append(f"{ticket['key']}: {exc}")
            if self.merges:
                self._deferred_integrations.update(await self.merges.pause_all())
            await self._persist_execution_controls()
            return {**self.execution_controls(),'paused_tickets':paused,'resumed_tickets':0,'tickets':paused,
                    'project_manager_status':manager.state.get('status') if manager else 'off',
                    'errors':errors,'project_manager':await manager.snapshot() if manager else {}}



    @owned_operation
    async def resume_all(self):
        async with self._all_execution_lock:
            await self.coordinator.set_paused(False)
            await self._persist_execution_controls()
            errors, resumed = [], 0
            tickets = await self.store.query("SELECT id,key FROM tickets WHERE state='In Progress' AND archived=0")
            for ticket in tickets:
                try:
                    facts = await self.queries.ticket_execution(ticket['id'])
                    if facts['finished']:
                        continue
                    outcome = await self.continue_ticket(ticket['id'])
                    resumed += outcome['execution'] in {'started','resumed','integration_started'}
                    self._deferred_integrations.discard(ticket['id'])
                except Exception as exc:
                    errors.append(f"{ticket['key']}: {exc}")
            await self._persist_execution_controls()
            manager = self.project_manager
            if manager and manager.state.get('status') == 'paused':
                try:
                    await manager.resume()
                except Exception as exc:
                    errors.append(f'Project Manager: {exc}')
            return {**self.execution_controls(),'paused_tickets':0,'resumed_tickets':resumed,'tickets':resumed,
                    'project_manager_status':manager.state.get('status') if manager else 'off',
                    'errors':errors,'project_manager':await manager.snapshot() if manager else {}}



    @owned_operation
    async def pause_run(self, ticket_id):
        ticket = await self.find_ticket(ticket_id)
        async with self.coordinator.reserve(ticket['id'],uid(),'pause'):
            active = await self.active_run(ticket['id'])
            integration = self.integration_tasks.get(ticket['id'])
            if not active and not self.coordinator.activities.get(ticket['id']) and not (integration and not integration.done()):
                raise AgileCodeError('Ticket has no active execution.')
            await self.stop_ticket(ticket['id'])
            if active:
                current = await self.store.get('runs',active['id'])
                if current['state'] == 'paused':
                    await self._pause_run(ticket['id'],active_only=False)

    async def _pause_run(self, ticket_id: str, *, active_only=False):
        ticket = await self.find_ticket(ticket_id); run = await self.active_run(ticket["id"])
        if not run:
            if active_only:
                return False
            raise AgileCodeError("Ticket has no active execution.")
        runtime = self.runtimes.get(run["id"])
        if active_only and run["state"] not in {"running", "waiting", "pause_requested"}:
            # Stopping only generation can leave owned background jobs running.
            if run["state"] != "paused" or not runtime or not runtime.shell.has_live_jobs():
                return False
        if self.processor:
            await self.processor.agent.stop_run(run['id'])
        if runtime:
            runtime.control.permit.clear()
        await self.store.execute(
            "UPDATE runs SET state='pause_requested',updated=? WHERE id=? "
            "AND state NOT IN ('completed','integrated','cancelled','failed')",
            (now(), run["id"]),
        )
        self.events.emit(Event("RunChanged",ticket["id"],run["id"]))
        task = self.tasks.get(run["id"])
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if runtime:
            await runtime.shell.close()
            await runtime.mcp.close()
        # Only the shell owner can attest that its process actually terminated.
        # A lost handle is uncertainty, never evidence that Pause killed a job.
        self.coordinator.blocked_tickets.update(await self.jobs.recover_unowned(run_id=run['id']))
        residual = await self.store.one("SELECT id FROM jobs WHERE run_id=? AND status IN ('starting','running','unknown') LIMIT 1", (run['id'],))
        if residual:
            reason = self.coordinator.blocked_tickets.get(ticket['id']) or 'Job termination has not settled; inspect the retained execution before resuming.'
            self.coordinator.blocked_tickets.setdefault(ticket['id'], reason)
            await self.store.update('runs',run['id'],error=reason,updated=now())
            self.events.emit(Event('RunChanged',ticket['id'],run['id'],data={'message':reason}))
            raise Conflict(reason)
        current = await self.store.get("runs", run["id"])
        if current["state"] in {"completed", "integrated", "cancelled", "failed"}:
            if self.all_paused and current["state"] == "completed" and current["final_oid"]:
                await self._defer_integration(ticket["id"])
            return False
        await self.store.update("runs",run["id"],state="paused",error="Paused by user.",updated=now())
        self.events.emit(Event("ExecutionPaused",ticket["id"],run["id"]))
        self.events.emit(Event("RunChanged",ticket["id"],run["id"]))
        return True


    @owned_operation
    async def resume_run(self, ticket_id, *, expected_transition=None, caller='manual', request_id=None):
        original = dict(ticket_id=ticket_id,expected_transition=expected_transition,caller=caller)
        if request_id:
            prior = await self.tickets._prior(request_id,'resume_run',original)
            if prior and prior['status'] == 'completed':
                return
            if prior and prior['status'] == 'interrupted':
                raise OperationInterrupted('This resume request was interrupted before launch.', result=decode(prior['result']))
            if not prior:
                await self.store.insert('operations',dict(id=request_id,kind='resume_run',owner_id=ticket_id,
                    data=encode({'original':original}),status='prepared',created=now(),updated=now()))
        try:
            await self._resume_selected(ticket_id,expected_transition=expected_transition,caller=caller,request_id=request_id)
        except OperationInterrupted as exc:
            if request_id:
                await self.store.update('operations',request_id,status='interrupted',
                    result=encode(exc.result or {'execution':'interrupted','reason':str(exc)}),updated=now())
            raise
        if request_id:
            run = await self.queries.selected_run(ticket_id)
            await self.store.update('operations',request_id,status='completed',result=encode({'run_id':run['id']}),updated=now())

    async def _resume_selected(self,ticket_id,*,expected_transition=None,caller='manual',request_id=None):
        self._require_execution_enabled()
        ticket = await self.find_ticket(ticket_id)
        selected = await self.queries.selected_run(ticket['id'])
        runtime = self.runtimes.get(selected['id']) if selected else None
        parent_owner = getattr(runtime,'operation_owner',None) if runtime else None
        async with self.coordinator.reserve(ticket['id'],request_id or uid(),'resume',parent_owner=parent_owner) as lease:
            facts = await self.require_eligible(ticket['id'], expected_transition=expected_transition)
            run = await self.active_run(ticket['id'])
            if not run:
                raise AgileCodeError('This ticket has no active execution to resume.')
            if run['state'] in {'failed','cancelled'}:
                runtime = self.runtimes.pop(run['id'],None)
                if runtime:
                    await runtime.shell.close()
                    await runtime.mcp.close()
            await self._launch(run,lease.owner)



    async def _resume_run(self, ticket_id):
        return await self.resume_run(ticket_id)



    @owned_operation
    async def cancel_run(self, ticket_id):
        ticket = await self.find_ticket(ticket_id)
        async with self.coordinator.reserve(ticket['id'],uid(),'cancel'):
            run = await self.queries.selected_run(ticket['id'])
            if not run:
                return
            if run['state'] == 'integrated':
                raise AgileCodeError('Integrated history cannot be cancelled.')
            await self.stop_ticket(ticket['id'])
            cancellation_checkpoint()
            await self.store.update('runs',run['id'],state='cancelled',updated=now())
            await self.store.execute('INSERT OR REPLACE INTO operations(id,kind,owner_id,data,status,result,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                ('cancel:'+run['id'],'cancel_run',run['id'],encode({'deliberate':True}),'completed',encode({'state':'cancelled'}),now(),now()))
            self.events.emit(Event('RunChanged',ticket['id'],run['id']))



    async def _wait(self, context: StepContext, runtime: Runtime, tool_id: str, kind: str, prompt: str, binding: str) -> str:
        existing = await self.store.one("SELECT * FROM waits WHERE tool_id=? ORDER BY created DESC LIMIT 1",(tool_id,))
        if existing and existing["binding"] != binding:
            raise Conflict("Pending operation changed after it was presented.")
        if existing and existing["status"] == "resolved":
            return existing["answer"] or ""
        identity = existing["id"] if existing else uid()
        if not existing:
            await self.store.insert("waits",{"id":identity,"tool_id":tool_id,"ticket_id":context.ticket["id"],"session_id":context.attempt["session_id"],
                "kind":kind,"prompt":prompt,"binding":binding,"status":"pending","answer":None,"created":now()})
        await self.store.update("tools",tool_id,status="waiting")
        await self.store.update("attempts",context.attempt["id"],status="waiting")
        if context.phase == "development":
            await self.store.update("runs",context.run["id"],state="waiting",updated=now())
        future = asyncio.get_running_loop().create_future(); self.pending_answers[identity] = future
        self.events.emit(Event("QuestionRequested" if kind == "question" else "ApprovalRequested",context.ticket["id"],context.run["id"],context.attempt["id"],context.attempt["session_id"],{"id":identity,"prompt":prompt,"kind":kind}))
        try:
            # An answer might have been durably delivered just before the future
            # was installed; re-read rather than relying on a transient signal.
            saved = await self.store.get("waits",identity)
            answer = saved["answer"] if saved["status"] == "resolved" else await future
            await runtime.control.check()
            await self.store.update("attempts",context.attempt["id"],status="running")
            if context.phase == "development":
                await self.store.update("runs",context.run["id"],state="running",updated=now())
            return answer
        finally:
            self.pending_answers.pop(identity,None)


    @project_mutation
    async def answer(self, wait_id: str, answer: str):
        record = await self.store.get("waits",wait_id)
        if record["kind"] == "approval" and answer not in {"approve","deny"}:
            raise AgileCodeError("Approval answer must be approve or deny.")
        if not answer.strip():
            raise AgileCodeError("Provide a nonempty answer up to 100,000 characters.")
        if record["status"] == "resolved":
            if record["answer"] == answer:
                return
            raise Conflict("This question/approval already has a different recorded answer.")
        await self.store.update("waits",wait_id,status="resolved",answer=answer,resolved=now())
        future = self.pending_answers.get(wait_id)
        if future and not future.done():
            future.set_result(answer)
        else:
            run = await self.active_run(record["ticket_id"])
            # Recording an answer is not permission to restart an explicitly paused ticket.
            if run and run["state"] != "pause_requested" and run["error"] != "Paused by user.":
                await self.resume_run(record["ticket_id"])
        self.events.emit(Event("WaitResolved",record["ticket_id"],data={"id":wait_id}))


    @project_mutation
    async def submit_feedback(self,ticket_id: str,text: str,*,interrupt=False,input_id=None):
        text = text.strip()
        if not text:
            raise AgileCodeError("Enter a correction before sending it.")
        from agilecode.runtime.conversation_records import accept_ticket_feedback, ticket_feedback_receipt
        def acknowledgement(receipt, interrupted=False):
            outcome = receipt.get("outcome") or {}
            return {"queued": not bool(outcome), "needs_followup": outcome.get("status") == "needs_followup",
                    "interrupted": interrupted, "session_id": receipt["session_id"], "receipt_id": receipt["id"],
                    "disposition": outcome or None, "input": receipt}
        ticket = await self.find_ticket(ticket_id)
        receipt = await ticket_feedback_receipt(self.store, ticket["id"], text, input_id)
        if receipt is not None:
            return acknowledgement(receipt)
        run = await self.active_run(ticket["id"])
        if not run:
            raise AgileCodeError("There is no active ticket execution for feedback.")
        attempt=await self.store.one(
            "SELECT * FROM attempts WHERE run_id=? AND phase='development' ORDER BY created DESC,number DESC LIMIT 1",
            (run["id"],),
        )
        if not attempt:
            raise AgileCodeError("No step has started yet.")
        receipt, created = await accept_ticket_feedback(self.store,ticket['id'],run['id'],attempt['session_id'],text,input_id,
                                                       with_created=True)
        if not created:
            return acknowledgement(receipt)
        interrupted = self.processor.agent.engine.input_accepted(receipt["session_id"], receipt, interrupt=interrupt)
        self.events.emit(Event("FeedbackQueued",ticket["id"],run["id"],attempt["id"],receipt["session_id"], {"input": receipt}))
        return acknowledgement(receipt, interrupted)


    async def prepare_integration(self,ticket_id,*,wait=True,rebuild=False,resume=False):
        cancellation_checkpoint()
        ticket = await self.find_ticket(ticket_id)
        if self.all_paused:
            return await self._defer_integration(ticket['id'])
        await self.require_eligible(ticket['id'], 'integration')
        task = self.integration_tasks.get(ticket['id'])
        if not task or task.done():
            cancellation_checkpoint()
            # This admitted ticket continuation is independent of the calling
            # conversation, just like an execution launched by coordinator.spawn.
            with detached_cancellation():
                task = asyncio.create_task(self.merges.prepare(ticket['id'],rebuild=rebuild,resume=resume),name='integrate-'+ticket['key'])
            self.integration_tasks[ticket['id']] = task
            def report(completed):
                if not completed.cancelled() and completed.exception():
                    self.events.emit(Event('IntegrationBlocked',ticket['id'],data={'message':str(completed.exception())}))
            task.add_done_callback(report)
        return await task if wait else {'status':'preparing'}



    async def approve_integration(self,integration_id,expected_candidate,expected_target):
        return await self.merges.apply(integration_id,expected_candidate,expected_target)



    async def retry_cleanup(self,ticket_id: str):
        ticket=await self.find_ticket(ticket_id)
        integration=await self.store.one("SELECT * FROM integrations WHERE ticket_id=? AND state='integrated' ORDER BY created DESC LIMIT 1",(ticket["id"],))
        if not integration:
            raise AgileCodeError("No successful integration is available for cleanup.")
        return await self.merges.cleanup(integration["id"])


    @owned_operation
    async def delete_attempt(self,ticket_id,attempt_id,*,request_id=None):
        identity = request_id or 'rewind:'+ticket_id+':'+attempt_id
        original = dict(ticket_id=ticket_id,attempt_id=attempt_id)
        prior = await self.tickets._prior(identity,'attempt_rewind',original)
        if prior and prior['status'] == 'completed':
            return decode(prior['result'])
        if prior and prior['status'] == 'interrupted':
            if request_id:
                raise OperationInterrupted('This rewind request was stopped before restoring files.', result=decode(prior['result']))
            await self.store.update('operations',identity,status='prepared',result=None,updated=now())
        if prior and prior['status'] == 'database_committed':
            return await self._finish_rewind_cleanup(identity)
        if prior and prior['status'] in {'restoring','blocked'}:
            data = decode(prior['data'])
            run = await self.store.get('runs',data['run_id'])
            observed = await self.git.snapshot_tree(Path(run['workspace']))
            if observed not in {data['expected_tree'],data['target_tree']}:
                raise Conflict('Interrupted rewind has an unknown file outcome. Inspect the retained worktree before resuming.')
        if not prior:
            await self.store.insert('operations',dict(id=identity,kind='attempt_rewind',owner_id=ticket_id,
                data=encode({'original':original}),status='prepared',created=now(),updated=now()))
        try:
            return await self._delete_attempt(ticket_id,attempt_id,identity)
        except OperationInterrupted as exc:
            result = {'ticket':ticket_id,'effect':'not_applied','reason':str(exc)}
            await self.store.update('operations',identity,status='interrupted',result=encode(result),updated=now())
            exc.result = result
            raise

    async def _delete_attempt(self, ticket_id, attempt_id, operation_id):
        """Discard this development attempt and later attempts, restoring its starting tree."""
        ticket = await self.find_ticket(ticket_id)
        async with self.coordinator.reserve(ticket['id'],operation_id,'rewind'):
            run = await self.selected_run(ticket["id"])
            if not run or run["final_oid"] or ticket["state"] == "Done":
                raise AgileCodeError("Only uncommitted, unintegrated ticket attempts can be deleted.")
            if run["state"] in {"integrated", "completed", "provisioning"}:
                raise AgileCodeError("This run can no longer be rewound.")
            if await self.store.one("SELECT 1 FROM integrations WHERE run_id=? LIMIT 1", (run["id"],)):
                raise AgileCodeError("Remove the integration candidate before rewinding this ticket.")
            integration_task = self.integration_tasks.get(ticket["id"])
            if integration_task and not integration_task.done():
                raise Conflict("Integration is running; do not rewind while it is changing the ticket.")

            history = await self.store.query(
                "SELECT rowid AS position, * FROM attempts WHERE run_id=? ORDER BY rowid",
                (run["id"],),
            )
            selected = next((row for row in history if row["id"] == attempt_id and row["phase"] == "development"), None)
            if not selected:
                raise AgileCodeError("This attempt is no longer part of the ticket.")
            discarded = [row for row in history if row["position"] >= selected["position"]]
            if any(row["phase"] != "development" for row in discarded):
                raise Conflict("Integration attempts cannot be removed through ticket rewind.")
            retained = [row for row in history if row["position"] < selected["position"]]
            saved = decode(selected["context"], {})
            current_inputs = decode(run["snapshot"], {}).get("ticket", {}).get("prerequisites", [])
            if (saved.get("base_oid", run["base_oid"]) != run["base_oid"]
                    or saved.get("ticket", {}).get("prerequisites", []) != current_inputs):
                raise Conflict("This attempt belongs to earlier prerequisite inputs. Rewinding would discard the synchronized baseline; retained history remains available for inspection.")
            target_tree = saved.get("start_tree")
            if not target_tree:
                raise AgileCodeError("The selected attempt has no saved starting tree.")
            snapshot = decode(run["snapshot"])
            process = Team.model_validate(snapshot["team"]).process(snapshot["process_id"])
            step_index = next((i for i, step in enumerate(process.steps) if step.id == selected["step_id"]), None)
            if step_index is None:
                raise Conflict("The attempt's process step no longer exists.")

            # Modern attempts carry exact identifiers. Legacy attempts have their
            # previous StepResult values, so recover the latest matching earlier ID.
            outputs = saved.get("output_attempts")
            if outputs is None:
                outputs = {}
                for step_id, value in saved.get("outputs", {}).items():
                    candidate = next((
                        old for old in reversed(retained)
                        if old["step_id"] == step_id and old["result"]
                        and decode(old["result"]) == value
                    ), None)
                    if candidate is None:
                        raise Conflict("Cannot reconstruct earlier handoff outputs safely.")
                    outputs[step_id] = candidate["id"]
            kept_ids = {row["id"] for row in retained}
            if any(value not in kept_ids for value in outputs.values()):
                raise Conflict("Saved handoff refers to an attempt that would be deleted.")

            corrections = saved.get("corrections")
            if corrections is None:
                discarded_corrections = sum(
                    bool(row["result"]
                         and decode(row["result"], {}).get("status") in {"FAIL", "CHANGES_REQUIRED"}
                         and next((step.correction_target for step in process.steps if step.id == row["step_id"]), None))
                    for row in discarded
                )
                corrections = max(0, run["corrections"] - discarded_corrections)

            await self.git.verify_worktree(Path(run["workspace"]), run["branch"])
            if await self.git.head(Path(run["workspace"])) != run["base_oid"]:
                raise Conflict("Ticket branch contains a commit; rewind cannot discard committed history.")
            await self.git.run("cat-file", "-e", f"{target_tree}^{{tree}}")

            cancellation_checkpoint()
            # A running attempt may still have a shell command or model response.
            # Pause and wait for owned execution to end before touching its files.
            task = self.tasks.get(run["id"])
            if run["state"] in {"running", "waiting", "pause_requested"} or (task and not task.done()):
                await self._pause_run(ticket["id"])
            runtime = self.runtimes.pop(run["id"], None)
            self.tasks.pop(run["id"], None)
            if runtime:
                await runtime.shell.close()
                await runtime.mcp.close()
            run = await self.store.get("runs", run["id"])
            if run["final_oid"] or await self.git.head(Path(run["workspace"])) != run["base_oid"]:
                raise Conflict("The ticket was committed during pause; no files were discarded.")

            current_tree = await self.git.snapshot_tree(Path(run["workspace"]))
            journal = await self.store.get('operations',operation_id)
            journal_data = decode(journal['data'])
            journal_data.update(run_id=run['id'],expected_tree=current_tree,target_tree=target_tree)
            cancellation_checkpoint()
            await self.store.update('operations',operation_id,status='restoring',data=encode(journal_data),updated=now())
            cancellation_checkpoint()
            await settle(self.checkpoints.restore_tree(run, target_tree, current_tree))
            # Stage state is not part of the recorded tree; discard staged changes
            # while leaving the just-restored file contents intact.
            await self.git.run("reset", "--mixed", "HEAD", cwd=Path(run["workspace"]))
            if await self.git.snapshot_tree(Path(run["workspace"])) != target_tree:
                raise Conflict("Restored file state changed while resetting the index.")

            removed_ids = [row["id"] for row in discarded]
            removed_sessions = [row["session_id"] for row in discarded]
            placeholders = ",".join("?" for _ in removed_ids)
            session_marks = ",".join("?" for _ in removed_sessions)
            checkpoint_rows = await self.store.query(
                "SELECT * FROM checkpoints WHERE run_id=? AND created>=?",
                (run["id"], selected["created"]),
            )
            artifact_rows = await self.store.query(
                "SELECT * FROM artifacts WHERE ticket_id=? AND created>=?",
                (ticket["id"], selected["created"]),
            )
            tool_ids = {
                row["id"] for row in await self.store.query(
                    f"SELECT id FROM tools WHERE session_id IN ({session_marks})",
                    tuple(removed_sessions),
                )
            }
            job_ids = {
                row["id"] for row in await self.store.query(
                    f"SELECT id FROM jobs WHERE attempt_id IN ({placeholders})",
                    tuple(removed_ids),
                )
            }
            orphan_activities = []
            for row in await self.store.query("SELECT * FROM activity WHERE ticket_id=?", (ticket["id"],)):
                data = decode(row["data"], {})
                if not isinstance(data, dict):
                    continue
                if (data.get("attempt_id") in removed_ids
                    or data.get("session_id") in removed_sessions
                    or data.get("id") in tool_ids or data.get("id") in job_ids):
                    orphan_activities.append(row["id"])

            result = {'ticket':ticket['id'],'run':run['id'],'deleted':len(discarded),
                      'step_id':selected['step_id'],'step_index':step_index,'cleanup_warnings':[]}
            journal_data.update(checkpoints=checkpoint_rows,artifacts=artifact_rows)
            def erase(c):
                erase_attempt_receipts_in(c, discarded)
                c.execute(f"DELETE FROM evidence WHERE attempt_id IN ({placeholders})", removed_ids)
                c.execute(f"DELETE FROM waits WHERE session_id IN ({session_marks})", removed_sessions)
                c.execute(f"DELETE FROM jobs WHERE attempt_id IN ({placeholders})", removed_ids)
                c.execute(f"DELETE FROM tools WHERE session_id IN ({session_marks})", removed_sessions)
                c.execute(f"DELETE FROM requests WHERE session_id IN ({session_marks})", removed_sessions)
                c.execute(f"DELETE FROM messages WHERE session_id IN ({session_marks})", removed_sessions)
                c.execute(f"DELETE FROM attempts WHERE id IN ({placeholders})", removed_ids)
                for row in checkpoint_rows:
                    c.execute("DELETE FROM checkpoints WHERE id=?", (row["id"],))
                for row in artifact_rows:
                    c.execute("DELETE FROM artifacts WHERE id=?", (row["id"],))
                for activity_id in orphan_activities:
                    c.execute("DELETE FROM activity WHERE id=?", (activity_id,))
                c.execute(
                    "UPDATE runs SET outputs=?,step_index=?,corrections=?,correction=?,"
                    "state='paused',error=?,updated=? WHERE id=?",
                    (encode(outputs), step_index, corrections, saved.get("correction_report"),
                     "Attempt deleted; ready to restart this step.", now(), run["id"]),
                )
                c.execute("UPDATE operations SET status='database_committed',data=?,result=?,updated=? WHERE id=?",
                          (encode(journal_data),encode(result),now(),operation_id))
            await self.store.transaction(erase)
            token = current_cancellation()
            if token and token.cancelled:
                result['cleanup_warnings'] = ['Cleanup deferred after interruption; retained checkpoint refs and artifacts.']
                await self.store.update('operations',operation_id,status='completed',result=encode(result),updated=now())
                self.events.emit(Event('TicketChanged',ticket['id'],run['id']))
                return result
            return await self._finish_rewind_cleanup(operation_id)

    async def _finish_rewind_cleanup(self,operation_id):
        journal = await self.store.get('operations',operation_id)
        result = decode(journal['result'])
        async with self.coordinator.reserve(result['ticket'],operation_id,'rewind'):
            async with self.coordinator.activity(result['ticket'],operation_id,'rewind_cleanup',cancellable=False):
                return await settle(self._rewind_cleanup_effect(operation_id))

    async def _rewind_cleanup_effect(self,operation_id):
            journal = await self.store.get('operations',operation_id)
            data,result = decode(journal['data']),decode(journal['result'])
            checkpoint_rows,artifact_rows = data.get('checkpoints',[]),data.get('artifacts',[])
            cleanup_warnings = []
            for checkpoint in checkpoint_rows:
                if checkpoint["oid"]:
                    response = await self.git.run("update-ref", "-d", checkpoint["ref"], checkpoint["oid"], check=False)
                    if response.code:
                        cleanup_warnings.append("Could not remove checkpoint ref: " + checkpoint["ref"])
            artifact_root = (self.storage / "artifacts" / result['ticket']).resolve()
            for artifact in artifact_rows:
                path = Path(artifact["path"]).resolve()
                if inside(path, artifact_root) and path.is_file():
                    try:
                        path.unlink()
                    except OSError:
                        cleanup_warnings.append("Could not remove artifact: " + artifact["id"])
            result['cleanup_warnings'] = cleanup_warnings
            await self.store.update('operations',operation_id,status='completed',result=encode(result),updated=now())
            self.events.emit(Event('TicketChanged',result['ticket'],result['run']))
            return result



    async def recover_pending_operations(self):
        """Reconcile durable local intent, never repeat an uncertain external effect."""
        for operation in await self.store.query("SELECT owner_id,result FROM operations WHERE kind='move_ticket' AND status='blocked'"):
            self.coordinator.blocked_tickets.setdefault(operation['owner_id'],
                'Movement has an unresolved stop outcome: ' + decode(operation['result'], {}).get('reason', 'inspect retained activity'))
        for op in await self.store.query("SELECT result FROM operations WHERE kind='delete_ticket' AND status='interrupted'"):
            result = decode(op['result'],{})
            if result.get('resources_may_have_changed') and await self.store.one('SELECT 1 FROM tickets WHERE id=?',(result['ticket'],)):
                self.coordinator.blocked_tickets[result['ticket']] = INTERRUPTED_DELETE_BLOCKER
        # Only an explicit recovery boundary repairs legacy failure labels.
        # A cancelled execution is never silently promoted by a read.
        interrupted = await self.store.query("SELECT r.* FROM runs r JOIN tickets t ON t.selected_run_id=r.id WHERE t.state='In Progress' AND r.state='failed' AND r.final_oid IS NULL AND EXISTS (SELECT 1 FROM attempts a WHERE a.run_id=r.id AND a.phase='development' AND a.status='interrupted')")
        for run in interrupted:
            if run['ticket_id'] in self.coordinator.blocked_tickets:
                continue
            await self.store.update('runs',run['id'],state='paused',error='Recovered interrupted execution; resume when ready.',updated=now())
            await self.store.execute('INSERT OR REPLACE INTO operations(id,kind,owner_id,data,status,result,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                ('recover-interrupted:'+run['id'],'legacy_run_recovery',run['id'],encode({'previous_error':run['error'],'evidence':'selected failed run with interrupted development attempt'}),'completed',encode({'state':'paused'}),now(),now()))
        for op in await self.store.query("SELECT * FROM operations WHERE kind='move_ticket' AND status IN ('reserved','board_committed') ORDER BY created,id"):
            if op['owner_id'] in self.coordinator.blocked_tickets:
                continue
            data = decode(op['data'],{})
            original = data.get('original',{})
            if op['status'] == 'board_committed':
                result = decode(op['result'],{})
                result.update(execution='paused',reason='Board change recovered. Resume explicitly when ready.')
                await self.store.update('operations',op['id'],status='completed',result=encode(result),updated=now())
                continue
            try:
                await self.move_ticket(request_id=op['id'],**original)
            except (Conflict, AgileCodeError) as exc:
                await self.store.update('operations',op['id'],status='conflict',result=encode({'board_committed':False,'execution':'blocked','reason':str(exc)}),updated=now())
        for op in await self.store.query("SELECT * FROM operations WHERE kind='delete_ticket' AND status='deleting_resources' ORDER BY created,id"):
            # The same validated deletion implementation rechecks ownership and
            # reachable target history before consuming any remaining resources.
            original = decode(op['data'])['original']
            if original['ticket_id'] in self.coordinator.blocked_tickets:
                continue
            try:
                await self.delete_ticket(original['ticket_id'],request_id=op['id'])
            except (Conflict, AgileCodeError) as exc:
                await self.store.update('operations',op['id'],status='blocked',result=encode({'reason':str(exc)}),updated=now())

        for op in await self.store.query("SELECT * FROM operations WHERE kind='attempt_rewind' AND status IN ('restoring','database_committed')"):
            if op['owner_id'] in self.coordinator.blocked_tickets:
                continue
            original = decode(op['data'])['original']
            try:
                await self.delete_attempt(**original,request_id=op['id'])
            except (Conflict,AgileCodeError) as exc:
                await self.store.update('operations',op['id'],status='blocked',updated=now())
                self.coordinator.blocked_tickets[op['owner_id']] = str(exc)
        for op in await self.store.query("SELECT * FROM operations WHERE kind='execution_continuation' AND status='pending'"):
            run = await self.store.get('runs', op['owner_id'])
            if run['ticket_id'] in self.coordinator.blocked_tickets:
                continue
            if self.execution_enabled and not self.all_paused:
                await self._continue_completed(op['owner_id'])

    async def load_interrupted_blockers(self):
        """Read-only startup for a domain host: absence of live handles is not evidence."""
        blocked = {}
        for run in await self.store.query("SELECT ticket_id FROM runs WHERE state IN ('running','waiting','pause_requested','provisioning')"):
            blocked[run['ticket_id']] = 'Interrupted execution needs an execution-capable host before this ticket can change.'
        for record in await self.store.query("SELECT ticket_id FROM integrations WHERE state IN ('preparing','applying','uncertain')"):
            blocked[record['ticket_id']] = 'Interrupted integration must be reconciled before this ticket can change.'
        for job in await self.store.query("SELECT ticket_id FROM jobs WHERE status IN ('starting','running','unknown')"):
            blocked[job['ticket_id']] = 'Owned job outcome is unresolved; open an execution-capable host.'
        for op in await self.store.query("SELECT * FROM operations WHERE kind='delete_ticket' AND status='deleting_resources'"):
            data = decode(op['data'],{})
            if data.get('ticket_id'):
                blocked[data['ticket_id']] = 'Interrupted deletion must settle before this ticket can change.'
        for operation in await self.store.query("SELECT owner_id,result FROM operations WHERE kind='move_ticket' AND status='blocked'"):
            blocked[operation['owner_id']] = 'Movement has an unresolved stop outcome: ' + decode(operation['result'], {}).get('reason', 'inspect retained activity')
        for op in await self.store.query("SELECT result FROM operations WHERE kind='delete_ticket' AND status='interrupted'"):
            result = decode(op['result'],{})
            if result.get('resources_may_have_changed') and await self.store.one('SELECT 1 FROM tickets WHERE id=?',(result['ticket'],)):
                blocked[result['ticket']] = INTERRUPTED_DELETE_BLOCKER
        self.coordinator.blocked_tickets.update(blocked)
        return blocked
