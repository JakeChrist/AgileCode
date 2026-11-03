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
from agilecode.application.services.agent_workspace import AgentWorkspaceOperations

from agilecode.application.coordination import owned_operation, settle, CHECKPOINT_RESTORE_BLOCKER as RESTORE_BLOCKER
from agilecode.application.cancellation import cancellation_checkpoint, OperationInterrupted


class WorkspaceOperations:
    def __init__(self, *, store, git, config, root, global_root, coordinator, queries,
                 runtimes, tasks, checkpoints, events):
        self.store, self.git, self.config = store, git, config
        self.root, self.global_root = Path(root), Path(global_root)
        self.coordinator, self.queries = coordinator, queries
        self.runtimes, self.tasks, self.checkpoints, self.events = runtimes, tasks, checkpoints, events

    async def find_ticket(self, identity):
        return await self.queries.find_ticket(identity)

    async def selected_run(self, identity):
        return await self.queries.selected_run(identity)

    @owned_operation
    async def save_source(self, ticket_id, path, content, expected_hash):
        ticket = await self.find_ticket(ticket_id)
        async with self.coordinator.reserve(ticket['id'],uid(),'source_save'):
            return await self._save_source(ticket['id'],path,content,expected_hash)

    @owned_operation
    async def restore_checkpoint(self,ticket_id,checkpoint_id,expected_tree):
        ticket = await self.find_ticket(ticket_id)
        async with self.coordinator.reserve(ticket['id'],uid(),'source_restore',
                                            resolving_blocker=RESTORE_BLOCKER):
            return await self._restore_checkpoint(ticket['id'],checkpoint_id,expected_tree)

    @owned_operation
    async def checkpoint(self,ticket_id,label='User checkpoint',*,owner=None):
        ticket = await self.find_ticket(ticket_id)
        run = await self.selected_run(ticket['id'])
        if not run:
            raise AgileCodeError('No ticket worktree exists yet.')
        runtime = self.runtimes.get(run['id'])
        if runtime and runtime.shell.has_live_jobs():
            raise NeedsReview('Stop running jobs before taking a checkpoint.')
        parent_owner = owner or (getattr(runtime,'operation_owner',None) if runtime else None)
        async with self.coordinator.reserve(ticket['id'],uid(),'checkpoint',parent_owner=parent_owner) as lease:
            if runtime:
                async with runtime.write_lock:
                    current = await self.queries.ticket_execution(ticket['id'])
                    if current['selected_run_id'] != run['id'] or current['blocking_reason']:
                        raise Conflict(current['blocking_reason'] or 'Selected execution changed before checkpoint capture.')
                    if runtime.shell.has_live_jobs():
                        raise NeedsReview('Stop running jobs before taking a checkpoint.')
                    cancellation_checkpoint()
                    return await settle(self.checkpoints.capture(current['run'],label,owner=lease.owner))
            current = await self.queries.ticket_execution(ticket['id'])
            if current['selected_run_id'] != run['id'] or current['blocking_reason']:
                raise Conflict(current['blocking_reason'] or 'Selected execution changed before checkpoint capture.')
            cancellation_checkpoint()
            return await settle(self.checkpoints.capture(current['run'],label,owner=lease.owner))

    async def _restore_checkpoint(self, ticket_id: str, checkpoint_id: str, expected_tree: str):
        ticket = await self.find_ticket(ticket_id)
        run = await self.selected_run(ticket['id'])
        if not run or run['state'] not in {'paused', 'cancelled'} or run['final_oid']:
            raise AgileCodeError('Restore requires a paused, uncommitted development run.')
        task = self.tasks.get(run['id'])
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        runtime = self.runtimes.get(run['id'])
        if runtime:
            await settle(runtime.shell.close())
            await settle(runtime.mcp.close())
            self.runtimes.pop(run['id'], None)
        identity = 'checkpoint-restore:' + uid()
        recorded = False

        async def record_intent(preview, before):
            nonlocal recorded
            cancellation_checkpoint()
            target = await self.store.get('checkpoints', checkpoint_id)
            data = {'run_id': run['id'], 'checkpoint_id': checkpoint_id,
                    'expected_tree': expected_tree, 'target_tree': preview['target_tree'],
                    'expected_oid': preview['expected_oid'], 'target_oid': preview['target_oid'],
                    'before_checkpoint': None, 'step_index': target['step_index'],
                    'outputs': decode(target['outputs'], {}), 'prior_state': run['state']}
            timestamp = now()

            def begin(c):
                current = c.execute('SELECT selected_run_id FROM tickets WHERE id=?', (ticket['id'],)).fetchone()
                if not current or current['selected_run_id'] != run['id']:
                    raise Conflict('Selected execution changed before checkpoint restoration.')
                c.execute("INSERT INTO operations(id,kind,owner_id,data,status,created,updated) VALUES(?,?,?,?,?,?,?)",
                          (identity, 'checkpoint_restore', ticket['id'], encode(data), 'restoring', timestamp, timestamp))
                # An explicit new preview authorizes replacing a blocked partial
                # restore, and its before-checkpoint preserves that observed tree.
                c.execute("UPDATE operations SET status='superseded',updated=? WHERE kind='checkpoint_restore' AND owner_id=? AND id<>? AND status IN ('restoring','blocked')",
                          (timestamp, ticket['id'], identity))
                self._invalidate_restore_outputs(c, run['id'], data, RESTORE_BLOCKER)
            await self.store.transaction(begin)
            recorded = True
            cancellation_checkpoint()

        cancellation_checkpoint()
        try:
            result = await settle(self.checkpoints.restore(run, checkpoint_id, expected_tree,
                                                          before_mutation=record_intent))
            await self._finish_restore(identity)
        except BaseException as exc:
            if recorded:
                if isinstance(exc, OperationInterrupted):
                    # The callback stopped before restore_tree was dispatched.
                    await settle(self._finish_restore(identity, unchanged=True))
                else:
                    await settle(self._block_restore(identity))
            raise
        self.events.emit(Event('CheckpointRestored', ticket['id'], run['id'], data=result))
        return result

    @staticmethod
    def _invalidate_restore_outputs(c, run_id, data, error):
        c.execute("UPDATE attempts SET status='superseded' WHERE run_id=? AND status IN ('running','waiting','paused','interrupted')", (run_id,))
        c.execute('UPDATE runs SET step_index=?,outputs=?,correction=?,error=?,updated=? WHERE id=?',
                  (data['step_index'], encode(data['outputs']),
                   data.get('correction', 'User restored a checkpoint. Reassess current files against the plan; rerun verification and review.'),
                   error, now(), run_id))

    async def _finish_restore(self, identity, *, unchanged=False):
        operation = await self.store.get('operations', identity)
        data = decode(operation['data'])
        result = {'tree': data['expected_tree'] if unchanged else data['target_tree'],
                  'before_checkpoint': data['before_checkpoint'], 'checkpoint_id': data['checkpoint_id']}
        if unchanged:
            result['restored'] = False
        def finish(c):
            current = c.execute('SELECT selected_run_id FROM tickets WHERE id=?', (operation['owner_id'],)).fetchone()
            if not current or current['selected_run_id'] != data['run_id']:
                raise Conflict('Interrupted checkpoint restore belongs to a different selected execution.')
            self._invalidate_restore_outputs(c, data['run_id'], data, None)
            # An aborted restore is not permission to revive a cancelled run.
            c.execute('UPDATE runs SET state=?,updated=? WHERE id=?',
                      (data['prior_state'] if unchanged else 'paused', now(), data['run_id']))
            c.execute('UPDATE operations SET status=?,result=?,updated=? WHERE id=?',
                      ('cancelled' if unchanged else 'completed', encode(result), now(), identity))
        await self.store.transaction(finish)
        async with self.coordinator.lock:
            if self.coordinator.blocked_tickets.get(operation['owner_id']) == RESTORE_BLOCKER:
                self.coordinator.blocked_tickets.pop(operation['owner_id'], None)
        return result

    async def _block_restore(self, identity):
        operation = await self.store.get('operations', identity)
        await self.store.update('operations', identity, status='blocked', updated=now())
        async with self.coordinator.lock:
            self.coordinator.blocked_tickets.setdefault(operation['owner_id'], RESTORE_BLOCKER)

    async def recover_pending_operations(self):
        """Reconcile file evidence before admitting work; never replay a partial restore."""
        for operation in await self.store.query("SELECT * FROM operations WHERE kind='source_save' AND status='writing' ORDER BY created,id"):
            data = decode(operation['data'])
            observed_hash = None
            try:
                run = await self.store.get('runs', data['run_id'])
                scope = Workspace(Path(run['workspace']), self.root, self.global_root,
                                  read_limit=self.config.settings.file_read_page_bytes)
                observed_hash = (await asyncio.to_thread(scope.read, data['path']))['hash']
            except (AgileCodeError, OSError):
                pass
            # Source replacement is atomic and verification was invalidated in
            # the intent transaction. Do not replay an unconfirmed user's edit.
            result = {'recovered': True, 'observed_hash': observed_hash,
                      'effect': 'not_applied' if observed_hash == data['expected_hash'] else 'unconfirmed',
                      'note': 'Inspect retained source; the interrupted save was not replayed. Verification must run again.'}
            await self.store.update('operations', operation['id'], status='interrupted', result=encode(result), updated=now())
        operations = await self.store.query("SELECT * FROM operations WHERE kind='checkpoint_restore' AND status IN ('restoring','blocked') ORDER BY created,id")
        for operation in operations:
            blocker = self.coordinator.blocked_tickets.get(operation['owner_id'])
            if blocker and blocker != RESTORE_BLOCKER:
                continue  # Unknown physical writers make tree evidence unsafe.
            data = decode(operation['data'])
            try:
                run = await self.store.get('runs', data['run_id'])
                await self.git.verify_worktree(Path(run['workspace']), run['branch'])
                observed = await self.git.head(Path(run['workspace']))
                if observed == data['target_oid']:
                    await self._finish_restore(operation['id'])
                elif observed == data['expected_oid']:
                    await self._finish_restore(operation['id'], unchanged=True)
                else:
                    await self._block_restore(operation['id'])
            except (AgileCodeError, OSError):
                await self._block_restore(operation['id'])


    async def read_source(self,ticket_id: str,path: str):
        ticket=await self.find_ticket(ticket_id)
        facts=await self.queries.ticket_execution(ticket['id'])
        if facts['blocking_reason']:
            raise Conflict(facts['blocking_reason'])
        run=facts['run']
        if run and run["final_oid"] and not Path(run["workspace"]).exists():
            from agilecode.adapters.workspace import decode_text, digest
            if not self.git.eligible(path, tracked=True):
                raise AgileCodeError("Invalid or protected committed source path.")
            rel = str(Path(path))
            size = await self.git.run("cat-file","-s",run["final_oid"]+":"+rel,check=False)
            if size.code:
                raise AgileCodeError("Committed source is missing or exceeds the text-file limit.")
            raw = (await self.git.run("show",run["final_oid"]+":"+rel)).stdout
            text, encoding = decode_text(raw)
            clipped = text.encode("utf-8")[:self.config.settings.file_read_page_bytes].decode("utf-8",errors="ignore")
            return {"path":rel,"scope":"workspace","hash":digest(raw),"size":len(raw),
                "encoding":encoding,"newline":"CRLF" if b"\r\n" in raw else "LF",
                "total_lines":len(text.splitlines()),"start_line":1,"end_line":len(clipped.splitlines()),
                "partial":clipped!=text,"content":clipped,"commit":run["final_oid"]}
        root=Path(run["workspace"]) if run and Path(run["workspace"]).exists() else self.root
        return await asyncio.to_thread(Workspace(root,self.root,self.global_root,readonly=root==self.root,
                                               read_limit=self.config.settings.file_read_page_bytes).read,path)


    async def _save_source(self, ticket_id: str, path: str, content: str, expected_hash: str):
        ticket = await self.find_ticket(ticket_id)
        run = await self.selected_run(ticket['id'])
        if not run or run['state'] != 'paused' or run['final_oid']:
            raise AgileCodeError('Manual edits require a paused, uncommitted development run.')
        facts = await self.queries.ticket_execution(ticket['id'])
        if facts['blocking_reason']:
            raise Conflict(facts['blocking_reason'])
        runtime = self.runtimes.get(run['id'])
        if runtime and runtime.shell.has_live_jobs():
            raise NeedsReview('Stop jobs before a manual edit.')
        task = self.tasks.get(run['id'])
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        scope = Workspace(Path(run['workspace']), self.root, self.global_root,
                          read_limit=self.config.settings.file_read_page_bytes)
        before = await asyncio.to_thread(scope.read, path)
        if before['hash'] != expected_hash:
            raise Conflict('Stale edit: file bytes no longer match the read hash.')
        snapshot = decode(run['snapshot'])
        process = Team.model_validate(snapshot['team']).process(snapshot['process_id'])
        index = next((i for i, step in enumerate(process.steps) if step.kind == 'implement'), 0)
        outputs = decode(run['outputs'], {})
        for step in process.steps[index:]:
            outputs.pop(step.id, None)
        identity = 'source-save:' + uid()
        data = {'run_id': run['id'], 'path': before['path'], 'expected_hash': expected_hash,
                'step_index': index, 'outputs': outputs,
                'correction': 'User manually edited source; reconcile with the plan and rerun verification/review.'}
        def prepare(c):
            current = c.execute('SELECT selected_run_id FROM tickets WHERE id=?', (ticket['id'],)).fetchone()
            if not current or current['selected_run_id'] != run['id']:
                raise Conflict('Selected execution changed before source saving.')
            c.execute('INSERT INTO operations(id,kind,owner_id,data,status,created,updated) VALUES(?,?,?,?,?,?,?)',
                      (identity, 'source_save', ticket['id'], encode(data), 'writing', now(), now()))
            # Invalidate before the atomic replacement. A crash can preserve
            # either file version, but can never retain old verification as valid.
            self._invalidate_restore_outputs(c, run['id'], data, run['error'])
        cancellation_checkpoint()
        await self.store.transaction(prepare)
        try:
            # Keep the writer's own hash recheck, encoding/newline preservation
            # and atomic replace; preflight does not replace its final guard.
            writer = AgentWorkspaceOperations(scope, runtime.write_lock if runtime else asyncio.Lock(), self.store)
            result = await writer.write(path=path, content=content, expected_hash=expected_hash, create=False)
        except BaseException as exc:
            await self.store.update('operations', identity, status='interrupted',
                result=encode({'effect': 'not_applied' if isinstance(exc, OperationInterrupted) else 'unconfirmed',
                               'reason': str(exc)}), updated=now())
            raise
        await self.store.update('operations', identity, status='completed', result=encode(result), updated=now())
        self.events.emit(Event('SourceChanged', ticket['id'], run['id'], data=result))
        return result



    async def preview_checkpoint(self,ticket_id,checkpoint_id):
        facts = await self.queries.ticket_execution(ticket_id)
        if facts['blocking_reason'] and facts['blocking_reason'] != RESTORE_BLOCKER:
            raise Conflict(facts['blocking_reason'])
        if not facts['run']:
            raise AgileCodeError('No ticket worktree exists yet.')
        return await self.checkpoints.preview(facts['run'],checkpoint_id)
