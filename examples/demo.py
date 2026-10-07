"""Run an isolated, deterministic full workflow using a real local HTTP/SSE server.

Usage: python examples/demo.py
No model downloads. No changes to existing repositories or global settings.
The protocol fixture is not an AI, and does not measure model quality.
"""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).parent))
from scripted_model import ScriptedModel
from agilecode.core.application import Application
from agilecode.core.types import Settings,ModelProfile,ProviderConfig


class DemoServer:
    def __init__(self):
        self.model=ScriptedModel();self.server=None;self.requests=[]
    async def start(self):
        self.server=await asyncio.start_server(self.handle,'127.0.0.1',0)
        return self.server.sockets[0].getsockname()[1]
    async def handle(self,reader,writer):
        try:
            head=(await reader.readuntil(b'\r\n\r\n')).decode()
            method,path,_=head.split('\r\n',1)[0].split(' ')
            headers=dict(line.split(':',1) for line in head.split('\r\n')[1:] if ':' in line)
            length=int(next((v for k,v in headers.items() if k.lower()=='content-length'),'0'))
            raw=await reader.readexactly(length) if length else b'{}'
            if method=='GET' and path=='/v1/models':
                data=json.dumps({'data':[{'id':'agilecode-diagnostic-fixture','context_length':32768}]}).encode()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n'+data)
            elif method=='POST' and path=='/v1/chat/completions':
                request=json.loads(raw);self.requests.append(request)
                name,args=self.model.action(request['messages'])
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n')
                arguments=json.dumps(args)
                chunks=[{'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'id':'call_1','type':'function','function':{'name':name,'arguments':''}}]},'finish_reason':None}]}]
                for pos in range(0,len(arguments),31):
                    chunks.append({'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'function':{'arguments':arguments[pos:pos+31]}}]},'finish_reason':None}]})
                chunks.append({'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}]})
                for chunk in chunks:
                    chunk['model']='agilecode-diagnostic-fixture'
                    writer.write(('data: '+json.dumps(chunk)+'\n\n').encode());await writer.drain();await asyncio.sleep(0)
                writer.write(b'data: [DONE]\n\n')
            else:
                writer.write(b'HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n')
            await writer.drain()
        except Exception as exc:
            print('Demo server error:',exc,file=sys.stderr)
        finally:
            writer.close();await writer.wait_closed()
    async def close(self):
        if self.server:self.server.close();await self.server.wait_closed()


def initialize_demo(root):
    root.mkdir()
    def git(*args):return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.STDOUT).decode().strip()
    git('init','-b','master');git('config','user.name','AgileCode Demo');git('config','user.email','demo@example.invalid')
    (root/'.gitignore').write_text('.agilecode/\n__pycache__/\n')
    (root/'app.py').write_text('def answer():\n    return 1\n')
    (root/'tests').mkdir()
    (root/'tests/test_app.py').write_text('import unittest\nfrom app import answer\nclass TestApp(unittest.TestCase):\n    def test_answer(self):\n        self.assertEqual(answer(), 1)\n')
    git('add','.');git('commit','-m','Initial demo')


async def exercise(root: Path,global_root: Path) -> dict:
    initialize_demo(root)
    server=DemoServer();port=await server.start()
    app=Application(root,global_root)
    try:
        await app.open(initialize=True)
        await app.config.save(Settings(autonomy='full'))
        app.config.save_provider(
            ProviderConfig(id='local',name='Demo local server',profile=ModelProfile(base_url=f'http://127.0.0.1:{port}/v1')),
            make_default=True,
        )
        ticket=await app.create_ticket('Return two','Change answer() from 1 to 2 and update its regression test.')
        await app.move_ticket(ticket['id'],'Ready');run=await app.start_ticket(ticket['id'])
        done=await app.wait_for_run(run['id'])
        if done['state']!='completed':raise RuntimeError('Demo run failed: '+str(done))
        candidate=await app.prepare_integration(ticket['id'])
        if candidate['state']!='awaiting_review':raise RuntimeError('Demo integration failed: '+str(candidate))
        await app.approve_integration(candidate['id'],candidate['candidate_oid'],candidate['target_oid'])
        attempts=await app.store.query("SELECT step_id,status FROM attempts WHERE run_id=? ORDER BY created",(run['id'],))
        assert (root/'app.py').read_text().endswith('return 2\n')
        assert (await app.find_ticket(ticket['id']))['state']=='Done'
        return {'ticket':ticket['key'],'state':'Done','steps':attempts,'http_requests':len(server.requests),'final_commit':done['final_oid']}
    finally:await app.close();await server.close()


async def main():
    with tempfile.TemporaryDirectory(prefix='agilecode-demo-') as directory:
        print('Diagnostic fixture: real HTTP streaming, Git, SQLite and Bash; no AI model.')
        print(json.dumps(await exercise(Path(directory)/'project',Path(directory)/'global'),indent=2))
        print('Demo passed. Its temporary repository and settings are removed.')

if __name__=='__main__':asyncio.run(main())
