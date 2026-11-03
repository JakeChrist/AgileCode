"""Committed prerequisite admission for the canonical ticket launch operation.

Board labels alone do not transfer files. Inputs must have reached the ordinary
integration target; stale consumer worktrees are advanced without discarding
local work. The normal checkpoint/operation owners retain recovery evidence.
"""
from pathlib import Path

from agilecode.adapters.store import decode, encode
from agilecode.core.types import Conflict, Team, now, uid
from .cancellation import cancellation_checkpoint
from .coordination import settle
from .process_operations import ensure_commit_stage


SYNC_BLOCKER = ('Prerequisite synchronization was interrupted. Retained files and the before-sync checkpoint are preserved; '
                'restart to reconcile its recorded outcome before resuming.')


async def prerequisite_inputs(app, ticket_id, branch):
    dependencies = await app.store.query(
        'SELECT t.* FROM tickets t JOIN dependencies d ON d.depends_on=t.id WHERE d.ticket_id=? ORDER BY t.key',
        (ticket_id,))
    target = (await app.git.run('rev-parse', '--verify', f'refs/heads/{branch}')).text
    inputs = []
    for ticket in dependencies:
        label = f"Dependency {ticket['key']}"
        if ticket['state'] != 'Done':
            raise Conflict(f"{label} is incomplete. Complete and integrate it before starting this ticket.")
        run = await app.queries.selected_run(ticket['id'])
        if not run or not run['final_oid'] or run['state'] != 'integrated':
            raise Conflict(f"{label} has no integrated committed deliverable. A Done label or legacy conversation-only result cannot satisfy a prerequisite; complete its normal commit and integration first.")
        integration = await app.store.one(
            "SELECT * FROM integrations WHERE run_id=? AND state='integrated' AND ticket_oid=? ORDER BY updated DESC,id DESC LIMIT 1",
            (run['id'], run['final_oid']))
        if not integration or integration['target_branch'] != branch:
            raise Conflict(f"{label} was not integrated into target branch {branch}.")
        for oid in (run['final_oid'], integration['candidate_oid']):
            if not oid or (await app.git.run('merge-base', '--is-ancestor', oid, target, check=False)).code:
                raise Conflict(f"{label}'s integrated commit is not present on target branch {branch}. Restore the required committed inputs before starting.")
        paths = await app.git.changed_files(run['base_oid'], run['final_oid'])
        if not paths:
            raise Conflict(f"{label} has no committed file deliverables.")
        entries = await app.git.tree_entries(integration['candidate_oid'])
        files = [path for path in paths if path in entries]
        target_entries = await app.git.tree_entries(target)
        missing = [path for path in files if path not in target_entries]
        inputs.append({'id': ticket['id'], 'key': ticket['key'], 'title': ticket['title'],
                       'description': ticket['description'], 'run_id': run['id'],
                       'commit': run['final_oid'], 'integration_commit': integration['candidate_oid'],
                       'files': files, 'current_files': [path for path in files if path in target_entries],
                       'missing_files': missing, 'deleted_files': [path for path in paths if path not in entries],
                       'reference_note': ('Some changed files have since been renamed or removed. Inspect their recorded integration commit for the original output.' if missing else 'Read these committed files in the current worktree; inspect the recorded commits for earlier versions.')})
    return target, inputs


async def _upgrade_process(app, run):
    snapshot = decode(run['snapshot'])
    team = Team.model_validate(snapshot['team'])
    process = team.process(snapshot['process_id'])
    upgraded = ensure_commit_stage(team, process.id)
    if upgraded is team:
        return run
    # Existing configurable ACLs stay authoritative. Built-in analyst roles
    # acquire only the document-edit tools introduced by this workflow version.
    if snapshot['ticket'].get('team_ref') in {'builtin:requirements-engineering', 'builtin:systems-architecture'}:
        current = await app.config.get_team(snapshot['ticket']['team_ref'])
        current_members = {member.id: member for member in current.members}
        for member in upgraded.members:
            if member.id in current_members:
                member.allowed_tools = list(dict.fromkeys(member.allowed_tools +
                    ['write_file', 'edit_file', 'move_path', 'delete_path']))
                member.system_prompt += (' Save the requested analysis deliverables in repository files during your assigned '
                                         'stage and identify their paths. Conversation text alone does not complete a ticket.')
    snapshot['team'] = upgraded.model_dump(mode='json')
    index = min(run['step_index'], len(process.steps) - 1)
    outputs = decode(run['outputs'], {})
    for step in process.steps[index:]:
        outputs.pop(step.id, None)
    def upgrade(c):
        timestamp = now()
        c.execute("INSERT INTO operations(id,kind,owner_id,data,status,created,updated) VALUES(?,?,?,?,?,?,?)",
                  ('deliverable-process:' + run['id'], 'process_upgrade', run['id'],
                   encode({'previous_snapshot': decode(run['snapshot']), 'previous_step_index': run['step_index']}),
                   'completed', timestamp, timestamp))
        c.execute("UPDATE attempts SET status='superseded' WHERE run_id=? AND status IN ('running','waiting','paused','interrupted')", (run['id'],))
        c.execute('UPDATE runs SET snapshot=?,step_index=?,outputs=?,updated=? WHERE id=?',
                  (encode(snapshot), index, encode(outputs), timestamp, run['id']))
    await app.store.transaction(upgrade)
    return await app.store.get('runs', run['id'])


async def prepare_launch(app, run, owner):
    """Bring newly integrated prerequisites into a retained ticket branch."""
    run = await _upgrade_process(app, run)
    target, inputs = await prerequisite_inputs(app, run['ticket_id'], run['base_branch'])
    snapshot = decode(run['snapshot'])
    previous = snapshot['ticket'].get('prerequisites', [])
    if not inputs and not previous:
        return run
    cwd = Path(run['workspace'])
    await app.git.verify_worktree(cwd, run['branch'])
    current = await app.git.head(cwd)
    if inputs == previous:
        contained = True
        for item in inputs:
            if (await app.git.run('merge-base', '--is-ancestor',
                                  item['integration_commit'], current,
                                  cwd=cwd, check=False)).code:
                contained = False
                break
        if contained:
            return run
    if run['final_oid']:
        raise Conflict('Cannot change prerequisites after Commit Work.')
    if (await app.git.run('merge-base', '--is-ancestor', run['base_oid'],
                          target, cwd=cwd, check=False)).code:
        raise Conflict('Prerequisite target diverged from the ticket base.')
    runtime = app.runtimes.get(run['id'])
    if runtime and runtime.shell.has_live_jobs():
        raise Conflict('Stop running jobs before merging prerequisites.')

    snapshot['ticket']['prerequisites'] = inputs
    identity = 'prerequisites:' + uid()
    data = {'run_id': run['id'], 'before_oid': current,
            'target_oid': target, 'snapshot': snapshot}
    cancellation_checkpoint()
    await app.store.insert('operations', dict(
        id=identity, kind='prerequisite_sync', owner_id=run['ticket_id'],
        data=encode(data), status='applying', created=now(), updated=now(),
    ))
    try:
        if (await app.git.run('merge-base', '--is-ancestor', target, current,
                              cwd=cwd, check=False)).code:
            merged = await app.git.run('merge', '--no-ff', '--no-edit',
                                       target, cwd=cwd, check=False)
            if merged.code:
                if (await app.git.run('rev-parse', '-q', '--verify', 'MERGE_HEAD',
                                       cwd=cwd, check=False)).code == 0:
                    await settle(app.git.run('merge', '--abort', cwd=cwd))
                raise Conflict('Prerequisite Git merge failed: ' +
                               merged.stderr.decode(errors='replace').strip())
        await settle(_finish_preparation(app, identity, data))
    except BaseException:
        await app.store.update('runs', run['id'], state='paused',
                               error=SYNC_BLOCKER, updated=now())
        raise
    return await app.store.get('runs', run['id'])


async def _finish_preparation(app, identity, data):
    def finish(c):
        # Reassess changed inputs in fresh sessions, keeping every old attempt
        # and checkpoint as history. Stale requirement/verification pointers
        # cannot attest to a newly synchronized tree.
        c.execute("UPDATE attempts SET status='superseded' WHERE run_id=? AND status IN ('running','waiting','paused','interrupted')", (data['run_id'],))
        c.execute("UPDATE runs SET base_oid=?,snapshot=?,step_index=0,outputs='{}',correction=?,error=NULL,updated=? WHERE id=?",
                  (data['target_oid'], encode(data['snapshot']), 'Prerequisite inputs synchronized. Reassess the retained work against the current ticket and prerequisites.', now(), data['run_id']))
        c.execute("UPDATE operations SET status='completed',result=?,updated=? WHERE id=?",
                  (encode({'base_oid': data['target_oid'], 'prerequisites': data['snapshot']['ticket']['prerequisites']}), now(), identity))
    await app.store.transaction(finish)


async def recover_preparation(app):
    for operation in await app.store.query(
            "SELECT * FROM operations WHERE kind='prerequisite_sync' AND status IN ('applying','blocked')"):
        data = decode(operation['data'])
        run = await app.store.get('runs', data['run_id'])
        try:
            cwd = Path(run['workspace'])
            await app.git.verify_worktree(cwd, run['branch'])
            head = await app.git.head(cwd)
            if head == data['before_oid']:
                await app.store.update('operations', operation['id'], status='not_applied', updated=now())
            elif (await app.git.run('merge-base', '--is-ancestor',
                                    data['target_oid'], head,
                                    cwd=cwd, check=False)).code == 0:
                await _finish_preparation(app, operation['id'], data)
            else:
                raise Conflict('Prerequisite merge has no completed Git commit.')
        except Exception as exc:
            await app.store.update('operations', operation['id'], status='blocked',
                                   result=encode({'reason': str(exc)}), updated=now())
            await app.store.update('runs', run['id'], state='paused',
                                   error=SYNC_BLOCKER + ' ' + str(exc), updated=now())
