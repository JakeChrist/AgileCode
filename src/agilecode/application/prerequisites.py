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
    """Called under the start/resume reservation, before any agent is launched."""
    run = await _upgrade_process(app, run)
    target, inputs = await prerequisite_inputs(app, run['ticket_id'], run['base_branch'])
    snapshot = decode(run['snapshot'])
    previous = snapshot['ticket'].get('prerequisites', [])
    if not inputs and not previous:
        return run
    cwd = Path(run['workspace'])
    await app.git.verify_worktree(cwd, run['branch'])
    if run['final_oid'] or await app.git.head(cwd) != run['base_oid']:
        raise Conflict('Prerequisite synchronization requires the retained uncommitted ticket branch; its HEAD changed.')
    # Previously admitted inputs may legitimately have been edited by this
    # ticket. Do not reset that work merely because it is being resumed.
    if inputs == previous:
        contained = True
        for item in inputs:
            contained = contained and (await app.git.run('merge-base', '--is-ancestor', item['integration_commit'], run['base_oid'], check=False)).code == 0
        if contained:
            return run
    if (await app.git.run('merge-base', '--is-ancestor', run['base_oid'], target, check=False)).code:
        raise Conflict('The prerequisite target diverged from this worktree baseline. Resolve the branch history before resuming; retained work was not changed.')
    runtime = app.runtimes.get(run['id'])
    if runtime and runtime.shell.has_live_jobs():
        raise Conflict('Stop running jobs before synchronizing prerequisite inputs; retained work was not changed.')
    if (await app.git.run('diff', '--cached', '--quiet', run['base_oid'], '--', cwd=cwd, check=False)).code:
        raise Conflict('Prerequisite synchronization would replace staged changes. Preserve or unstage them explicitly before resuming; the index and working files were not changed.')
    before = await app.checkpoints.capture(run, 'Before prerequisite synchronization', owner=owner)
    merged_tree = before['tree']
    if target != run['base_oid']:
        merged_tree = await app.git.merge_tree(before['oid'], target)
    entries = await app.git.tree_entries(merged_tree)
    before_entries = await app.git.tree_entries(before['tree'])
    for path in entries:
        file = cwd / path
        for parent in Path(path).parents:
            if parent == Path('.'):
                continue
            physical = cwd / parent
            if physical.is_symlink() or (physical.exists() and not physical.is_dir() and str(parent) not in before_entries):
                raise Conflict('Prerequisite synchronization has an unsafe or unrecorded path parent; inspect it before resuming: ' + str(parent))
        if path not in before_entries and (file.exists() or file.is_symlink()):
            raise Conflict('Prerequisite synchronization would overwrite an ignored/unrecorded local file; preserve or move it first: ' + path)
    # The shared restore operation intentionally preserves unknown directories.
    # Reject directory-to-file replacements before it can remove any files.
    for path in entries:
        if (cwd / path).is_dir() and not (cwd / path).is_symlink():
            raise Conflict('Prerequisite synchronization would replace a directory with a file; inspect and resolve it first: ' + path)
    incoming = [path for path in entries if path not in before_entries]
    if incoming:
        ignored = await app.git.run('check-ignore', '--no-index', '-z', '--stdin', cwd=cwd,
            input=b'\0'.join(path.encode('utf-8', errors='surrogateescape') for path in incoming) + b'\0', check=False)
        if ignored.code not in {0, 1}:
            raise Conflict('Could not validate local ignore rules before prerequisite synchronization.')
        if ignored.stdout:
            names = ignored.stdout.decode('utf-8', errors='replace').rstrip('\0').replace('\0', ', ')
            raise Conflict('Local ignore rules hide incoming prerequisite files. Adjust those rules before resuming; no files were changed: ' + names)
    snapshot['ticket']['prerequisites'] = inputs
    identity = 'prerequisites:' + uid()
    data = {'run_id': run['id'], 'before_checkpoint': before['id'], 'before_tree': before['tree'],
            'tree': merged_tree, 'base_oid': run['base_oid'], 'target_oid': target, 'snapshot': snapshot}
    cancellation_checkpoint()
    await app.store.insert('operations', dict(id=identity, kind='prerequisite_sync', owner_id=run['ticket_id'],
        data=encode(data), status='applying', created=now(), updated=now()))
    async def apply():
        if (await app.git.run('diff', '--cached', '--quiet', run['base_oid'], '--', cwd=cwd, check=False)).code:
            raise Conflict('The index changed during prerequisite preparation; staged contents were preserved.')
        await app.checkpoints.restore_tree(run, merged_tree, before['tree'])
        if (await app.git.run('diff', '--cached', '--quiet', run['base_oid'], '--', cwd=cwd, check=False)).code:
            raise Conflict('The index changed during prerequisite synchronization; staged contents were preserved for review.')
        await app.git.run('update-ref', f"refs/heads/{run['branch']}", target, run['base_oid'])
        await app.git.run('read-tree', target, cwd=cwd)
        await _finish_preparation(app, identity, data)
    try:
        await settle(apply())
    except BaseException:
        await app.store.update('runs', run['id'], state='paused', error=SYNC_BLOCKER, updated=now())
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
                  (encode({'base_oid': data['target_oid'], 'tree': data['tree'], 'prerequisites': data['snapshot']['ticket']['prerequisites']}), now(), identity))
    await app.store.transaction(finish)


async def recover_preparation(app):
    for operation in await app.store.query("SELECT * FROM operations WHERE kind='prerequisite_sync' AND status IN ('applying','blocked')"):
        data = decode(operation['data'])
        run = await app.store.get('runs', data['run_id'])
        try:
            cwd = Path(run['workspace'])
            await app.git.verify_worktree(cwd, run['branch'])
            head, tree = await app.git.head(cwd), await app.git.snapshot_tree(cwd)
            index = await app.git.run('write-tree', cwd=cwd, check=False)
            old_index = (await app.git.run('rev-parse', f"{data['base_oid']}^{{tree}}")).text
            new_index = (await app.git.run('rev-parse', f"{data['target_oid']}^{{tree}}")).text
            if index.code or index.text not in {old_index, new_index}:
                raise Conflict('The index contains staged changes outside the recorded synchronization. Preserve or unstage them explicitly before restarting; no staged contents were discarded.')
            if tree == data['tree'] and head in {data['base_oid'], data['target_oid']}:
                if head != data['target_oid']:
                    await app.git.run('update-ref', f"refs/heads/{run['branch']}", data['target_oid'], head)
                await app.git.run('read-tree', data['target_oid'], cwd=cwd)
                await _finish_preparation(app, operation['id'], data)
            elif head in {data['base_oid'], data['target_oid']} and tree == data['before_tree']:
                # A user may have explicitly restored the before-sync checkpoint
                # after an interrupted file application. Only this recorded
                # private branch transition is undone; the project is untouched.
                if head != data['base_oid']:
                    await app.git.run('update-ref', f"refs/heads/{run['branch']}", data['base_oid'], head)
                await app.git.run('read-tree', data['base_oid'], cwd=cwd)
                await app.store.update('operations', operation['id'], status='not_applied', updated=now())
            else:
                raise Conflict('The physical outcome differs from both recorded trees; inspect retained work and restore the before-sync checkpoint before restarting.')
        except Exception as exc:
            await app.store.update('operations', operation['id'], status='blocked', result=encode({'reason': str(exc)}), updated=now())
            await app.store.update('runs', run['id'], state='paused', error=SYNC_BLOCKER + ' ' + str(exc), updated=now())
