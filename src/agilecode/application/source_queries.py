"""Source read models shared by GUI, CLI and other authorized clients."""
from __future__ import annotations
import asyncio
from pathlib import Path
from agilecode.domain.types import AgileCodeError
from agilecode.adapters.store import decode
from agilecode.adapters.workspace import Workspace
from agilecode.core.diff import file_diff


class SourceQueries:
    def __init__(self, *, store, git, root, global_root, tickets, workspace):
        self.store, self.git = store, git
        self.root, self.global_root = Path(root), Path(global_root)
        self.tickets, self.workspace = tickets, workspace

    async def _facts(self, ticket_id):
        facts = await self.tickets.ticket_execution(ticket_id)
        if facts.get('blocking_reason'):
            raise AgileCodeError(facts['blocking_reason'])
        return facts

    async def code(self, ticket_id, path):
        facts=await self._facts(ticket_id);run=facts['run']
        data=await self.workspace.read_source(facts['ticket']['id'],path)
        return {**data,'ticket':facts['ticket']['id'],
                'run_id':run['id'] if run else None,
                'editable':bool(run and run['state']=='paused' and not run['final_oid']
                                and not data['partial'] and Path(run['workspace']).exists())}

    async def file_list(self, ticket_id):
        facts=await self._facts(ticket_id);ticket=facts['ticket'];run=facts['run']
        if run and run['final_oid'] and not Path(run['workspace']).exists():
            entries=await self.git.tree_entries(run['final_oid'])
            names=sorted(p for p in entries if self.git.eligible(p,tracked=True))
            return {'ticket':ticket['id'],'files':[{'path':p,'directory':False,
                    'symlink':entries[p][0]=='120000'} for p in names],'partial':False}
        path=Path(run['workspace']) if run and Path(run['workspace']).exists() else self.root
        data=await asyncio.to_thread(Workspace(path,self.root,self.global_root).list_files,
                                     recursive=True,limit=None)
        return {**data,'ticket':ticket['id']}

    async def compare_file(self, ticket_id, path):
        facts=await self._facts(ticket_id);run=facts['run']
        if not run:raise AgileCodeError('This ticket has no source change yet.')
        data=await self.workspace.read_source(facts['ticket']['id'],path)
        old=await self.git.run('show',run['base_oid']+':'+data['path'],check=False)
        result=await asyncio.to_thread(file_diff,data['path'],
            old.stdout.decode(errors='replace') if old.code==0 else '',data['content'])
        result['partial']=data['partial']
        return result

    async def integration_diff(self, integration_id):
        record=await self.store.get('integrations',integration_id)
        if not record['candidate_oid']:
            raise AgileCodeError('The candidate does not have a validated commit yet.')
        return (await self.git.run('diff','--no-ext-diff',record['target_oid'],
                                 record['candidate_oid'],'--')).stdout.decode(errors='replace')

    async def diff(self, ticket_id):
        facts=await self._facts(ticket_id);run=facts['run']
        if not run:return ''
        if Path(run['workspace']).exists():
            return await self.git.diff(Path(run['workspace']),run['base_oid'])
        if run['final_oid']:
            return (await self.git.run('diff','--no-ext-diff',run['base_oid'],
                                     run['final_oid'],'--')).stdout.decode(errors='replace')
        return 'Worktree unavailable; no final commit was recorded.'

    async def step_diff(self, ticket_id, attempt_id):
        ticket=await self.tickets.find_ticket(ticket_id)
        attempt=await self.store.one('SELECT a.* FROM attempts a JOIN runs r ON r.id=a.run_id '
            'WHERE a.id=? AND r.ticket_id=?',(attempt_id,ticket['id']))
        if not attempt:raise AgileCodeError('Step attempt does not belong to this ticket.')
        if not attempt['result']:raise AgileCodeError('Step has not completed.')
        start=decode(attempt['context'],{}).get('start_tree');end=attempt['fingerprint']
        if not start or not end:raise AgileCodeError('Saved file states are unavailable for this step.')
        changed=start!=end
        text=(await self.git.run('diff','--no-ext-diff','--no-textconv','--find-renames',start,end,'--')).stdout.decode('utf-8',errors='replace') if changed else ''
        return {'ticket':ticket['id'],'attempt':attempt_id,'changed':changed,'text':text}
