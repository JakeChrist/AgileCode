"""One operational selection and explicit history, with side-effect-free reads."""
from __future__ import annotations
import asyncio
from pathlib import Path
from agilecode.core.types import AgileCodeError, Team
from agilecode.adapters.store import decode, encode


def read_only_process(run):
    """Recognize legacy transcript-only history; never an execution mode."""
    try:
        snapshot = decode(run['snapshot'])
        process = Team.model_validate(snapshot['team']).process(snapshot['process_id'])
        return (not any(s.kind in {'implement', 'verify_design', 'commit'} for s in process.steps)
                and run['step_index'] >= len(process.steps))
    except (KeyError, TypeError, ValueError):
        return False


def classify_run(run, *, analysis_evidence=False):
    if not run:
        return {'classification': 'never_started', 'finished': False, 'unfinished': False}
    state = run['state']
    if state == 'integrated':
        classification, finished = 'integrated', True
    elif state == 'completed' and not run.get('final_oid') and read_only_process(run):
        classification = 'analysis_completed' if analysis_evidence else 'analysis_unverified'
        finished = bool(analysis_evidence)
    elif state == 'completed' and run.get('final_oid'):
        classification, finished = 'awaiting_integration', False
    elif state in {'failed', 'cancelled'}:
        classification, finished = 'stopped', False
    else:
        classification, finished = 'unfinished', False
    return {'classification': classification, 'finished': finished,
            'unfinished': not finished, 'state': state}


def migrate_run_selection(c):
    """Run once after adding fields; ambiguous legacy evidence stays blocked."""
    marker = 'selected_run_migration_v1'
    if c.execute('SELECT 1 FROM meta WHERE key=?', (marker,)).fetchone():
        return
    for row in c.execute('SELECT * FROM tickets').fetchall():
        ticket = dict(row)
        runs = [dict(r) for r in c.execute('SELECT * FROM runs WHERE ticket_id=? ORDER BY created,id', (ticket['id'],)).fetchall()]
        live = [r for r in runs if r['state'] not in {'completed', 'integrated', 'failed', 'cancelled'}]
        selected = None
        reason = 'no runs'
        if len(live) == 1:
            candidate = live[0]
            newer_finished = [r for r in runs if (r['created'], r['id']) > (candidate['created'], candidate['id']) and r['state'] in {'completed', 'integrated'}]
            if newer_finished:
                reason = 'ambiguous unfinished run and newer completed history'
            else:
                selected, reason = candidate, 'retained unfinished work'
        elif not live and runs:
            selected, reason = runs[-1], 'latest terminal evidence'
        elif live:
            reason = 'ambiguous unfinished runs'
        transition = ticket.get('board_transition_id') or 'migration:' + ticket['id']
        c.execute('UPDATE tickets SET selected_run_id=?,board_transition_id=? WHERE id=?', (selected['id'] if selected else None, transition, ticket['id']))
        if selected:
            c.execute('UPDATE runs SET board_transition_id=? WHERE id=?', (transition, selected['id']))
        c.execute('INSERT OR REPLACE INTO operations(id,kind,owner_id,data,status,result,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                  ('selection:' + ticket['id'], 'run_selection', ticket['id'], encode({'run_ids': [r['id'] for r in runs]}),
                   'completed' if selected or not runs else 'blocked', encode({'selected_run_id': selected['id'] if selected else None, 'reason': reason}), ticket['updated'], ticket['updated']))
    c.execute('INSERT INTO meta(key,value) VALUES(?,?)', (marker, '1'))


class TicketQueries:
    def __init__(self, store, *, tasks=None, source_for_ticket=None, activities=None):
        self.store = store
        self.tasks = tasks if tasks is not None else {}
        self.source_for_ticket = source_for_ticket
        self.activities = activities or (lambda ticket_id: ())

    async def find_ticket(self, identity):
        ticket = await self.store.one('SELECT * FROM tickets WHERE id=? OR key=?', (identity, identity))
        if not ticket:
            raise AgileCodeError(f'Unknown ticket: {identity}')
        return ticket

    async def latest_run(self, ticket_id):
        """Historical newest record; never used for operational selection."""
        return await self.store.one('SELECT * FROM runs WHERE ticket_id=? ORDER BY created DESC,id DESC LIMIT 1', (ticket_id,))

    async def selected_run(self, ticket_id):
        ticket = await self.find_ticket(ticket_id)
        return await self.store.one('SELECT * FROM runs WHERE id=? AND ticket_id=?', (ticket.get('selected_run_id'), ticket['id']))

    async def active_run(self, ticket_id):
        facts = await self.ticket_execution(ticket_id)
        run = facts['run']
        if not run or facts['finished'] or run['state'] == 'completed':
            return None
        # A deliberately stopped run is historical work, never an implicit resume.
        if run['state'] in {'cancelled', 'failed'}:
            return None
        return run

    async def ticket_execution(self, ticket_id):
        ticket = await self.find_ticket(ticket_id)
        run = await self.selected_run(ticket['id'])
        evidence = await self.store.one("SELECT * FROM operations WHERE id=? AND kind='analysis_completion' AND status IN ('validated','completed')", ('analysis:' + run['id'],)) if run else None
        facts = classify_run(run, analysis_evidence=bool(evidence))
        history_count = (await self.store.one('SELECT COUNT(*) AS n FROM runs WHERE ticket_id=?', (ticket['id'],)))['n']
        blocked = 'Selected run is ambiguous; inspect retained execution history.' if history_count and not run else None
        restore = await self.store.one("SELECT data FROM operations WHERE id=? AND kind='resource_restore' AND status='blocked'", ('resource-restore:'+run['id'],)) if run else None
        if restore:
            blocked = decode(restore['data'],{}).get('reason') or 'Restored execution resources require review.'
        rewind = await self.store.one("SELECT id FROM operations WHERE kind='attempt_rewind' AND owner_id=? AND status IN ('restoring','blocked') LIMIT 1",(ticket['id'],))
        if rewind:
            blocked = 'An interrupted attempt rewind must be reconciled before execution resumes.'
        move = await self.store.one("SELECT result FROM operations WHERE kind='move_ticket' AND owner_id=? AND status='blocked' LIMIT 1", (ticket['id'],))
        if move:
            blocked = blocked or ('Movement has an unresolved stop outcome: ' + decode(move['result'], {}).get('reason', 'inspect retained activity'))
        restore = await self.store.one("SELECT id FROM operations WHERE kind='checkpoint_restore' AND owner_id=? AND status IN ('restoring','blocked') LIMIT 1", (ticket['id'],))
        if restore:
            from agilecode.application.coordination import CHECKPOINT_RESTORE_BLOCKER as RESTORE_BLOCKER
            blocked = blocked or RESTORE_BLOCKER
        sync = await self.store.one("SELECT result FROM operations WHERE kind='prerequisite_sync' AND owner_id=? AND status IN ('applying','blocked') LIMIT 1", (ticket['id'],))
        if sync:
            from .prerequisites import SYNC_BLOCKER
            blocked = SYNC_BLOCKER + ' ' + decode(sync['result'], {}).get('reason', '')
        job = await self.store.one("SELECT data FROM operations WHERE kind='job_recovery' AND owner_id=? AND status='blocked' LIMIT 1", (ticket['id'],))
        if job:
            blocked = decode(job['data'], {}).get('reason') or 'An owned job may still be running; its physical outcome must be resolved before changing this ticket.'
        return {**facts, 'ticket': ticket, 'run': run, 'selected_run_id': run['id'] if run else None,
                'transition_id': ticket.get('board_transition_id', ''), 'blocking_reason': blocked,
                'retained_resources': {'workspace_exists': bool(run and Path(run['workspace']).exists()),
                                       'branch': run['branch'] if run else None,
                                       'cleanup_pending': bool(run and facts['finished'] and Path(run['workspace']).exists())},
                'history_count': history_count,
                'deliverable_notice': ('Legacy completion has no committed file deliverable recorded. Retained conversation and files are preserved; reopen and start a new run to produce committed files.' if run and run['state'] == 'completed' and not run['final_oid'] else None)}

    async def board(self, query='', *, include_archived=False):
        return await self.store.query("SELECT t.*,r.id AS run_id,r.state AS execution_state,r.step_index,r.error AS run_error FROM tickets t LEFT JOIN runs r ON r.id=t.selected_run_id WHERE (? OR t.archived=0) AND (t.title LIKE ? OR t.key LIKE ? OR t.description LIKE ?) ORDER BY t.priority DESC,t.position,t.created", (int(include_archived), f'%{query}%', f'%{query}%', f'%{query}%'))

    async def execution_status(self, ticket_id, run=None):
        facts = await self.ticket_execution(ticket_id)
        run = run or facts['run']
        if not run:
            return {'run_id': None, 'state': None, 'can_resume': False, 'can_pause': False, 'blocking_reason': facts['blocking_reason']}
        task = self.tasks.get(run['id'])
        running = bool(task and not task.done())
        activity_kinds = sorted({activity.kind for activity in self.activities(facts['ticket']['id'])
                                 if not activity.task.done()})
        from .eligibility import execution_eligibility
        eligible = execution_eligibility(facts['ticket'], facts) is None
        return {'run_id': run['id'], 'state': run['state'], 'finished': facts['finished'],
                'classification': facts['classification'], 'blocking_reason': facts['blocking_reason'],
                'deliverable_notice': facts['deliverable_notice'],
                'can_restart_legacy': bool(facts['deliverable_notice'] and facts['ticket']['state'] in {'Ready', 'In Progress'} and not facts['blocking_reason']),
                'can_resume': eligible and not facts['finished'] and run['state'] != 'pause_requested' and (run['state'] in {'paused','cancelled'} or (run['state'] == 'running' and not running)),
                'active_activities': activity_kinds,
                'can_pause': not facts['blocking_reason'] and (bool(activity_kinds) or (run['state'] in {'running','waiting','provisioning'} and running))}

    async def ticket_notes(self, ticket_id):
        rows = await self.store.query(
            "SELECT id,data,created FROM activity "
            "WHERE ticket_id=? AND kind='TicketNote' ORDER BY id",
            (ticket_id,),
        )
        return [
            {"id": row["id"], "created": row["created"],
             "author": note.get("author"), "text": note.get("text", "")}
            for row in rows for note in [decode(row["data"], {})]
        ]

    async def ticket_detail(self, ticket_id, *, complete=False):
        facts = await self.ticket_execution(ticket_id)
        ticket, run = facts['ticket'], facts['run']
        attempts = await self.store.query('SELECT * FROM attempts WHERE run_id=? ORDER BY rowid' + ('' if complete else ' LIMIT 500'), (run['id'],)) if run else []
        for attempt in attempts:
            for name in ('result','model','member','context'):
                attempt[name] = decode(attempt[name])
        return {'ticket': ticket, 'run': run, 'attempts': attempts, 'execution': await self.execution_status(ticket['id']),
                'notes': await self.ticket_notes(ticket['id']),
                'source_discussion': await self.source_for_ticket(ticket['id']) if self.source_for_ticket else None,
                'pending_feedback': await self.pending_feedback(ticket['id']),
                'waits': await self.store.query("SELECT * FROM waits WHERE ticket_id=? AND status='pending' ORDER BY created", (ticket['id'],)),
                'jobs': await self.store.query('SELECT * FROM jobs WHERE ticket_id=? ORDER BY created DESC' + ('' if complete else ' LIMIT 100'), (ticket['id'],)),
                'checkpoints': await self.store.query('SELECT * FROM checkpoints WHERE ticket_id=? ORDER BY created DESC' + ('' if complete else ' LIMIT 100'), (ticket['id'],)),
                'integrations': await self.store.query('SELECT * FROM integrations WHERE ticket_id=? ORDER BY created DESC' + ('' if complete else ' LIMIT 20'), (ticket['id'],)),
                'dependencies': await self.store.query('SELECT t.* FROM tickets t JOIN dependencies d ON d.depends_on=t.id WHERE d.ticket_id=?', (ticket['id'],))}

    async def pending_feedback(self, ticket_id):
        """Read saved late-input dispositions without repairing or restarting work."""
        ticket = await self.find_ticket(ticket_id)
        receipts = await self.store.query("SELECT o.* FROM operations o WHERE o.kind='conversation_input' AND o.status='needs_followup' AND o.owner_id IN (SELECT a.session_id FROM attempts a JOIN runs r ON r.id=a.run_id WHERE r.ticket_id=?) ORDER BY o.created,o.id", (ticket['id'],))
        receipt_map = {receipt['id']: receipt for receipt in receipts}
        activities = await self.store.query("SELECT * FROM activity WHERE ticket_id=? AND kind='FeedbackNeedsFollowup' ORDER BY id", (ticket['id'],))
        visible, seen = [], set()
        for activity in activities:
            data = decode(activity['data'], {})
            identity = data.get('receipt_id')
            receipt = receipt_map.get(identity)
            if receipt is None or identity in seen:
                continue
            outcome = decode(receipt['result'], {})
            if outcome.get('status') != 'needs_followup':
                continue
            visible.append({**data, 'disposition': outcome, 'created': activity['created']})
            seen.add(identity)
        # A retained receipt can still explain accepted input if its older
        # activity projection is missing. The read never repairs that projection.
        for receipt in receipts:
            if receipt['id'] in seen:
                continue
            data, outcome = decode(receipt['data'], {}), decode(receipt['result'], {})
            if outcome.get('status') != 'needs_followup':
                continue
            text = data.get('text', '')
            if text.startswith('USER FEEDBACK: '):
                text = text[len('USER FEEDBACK: '):]
            visible.append({'receipt_id': receipt['id'], 'input_id': data.get('input_id'),
                            'run_id': outcome.get('run_id'), 'session_id': data.get('session_id'),
                            'text': text, 'disposition': outcome, 'created': receipt['created']})
        return visible

    async def active_ticket_count(self, *, exclude_ticket_id=None):
        row = await self.store.one("SELECT COUNT(*) AS n FROM tickets WHERE archived=0 AND state='In Progress' AND (? IS NULL OR id<>?)", (exclude_ticket_id,exclude_ticket_id))
        return row['n']
