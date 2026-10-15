import asyncio
import threading
from pathlib import Path
import pytest
from agilecode.core.application import Application
from agilecode.adapters.workspace import Workspace
from test_analysis_board_completion import BlockingClient

@pytest.mark.asyncio
async def test_close_retains_database_owner_through_cancelled_source_writer(repository,tmp_path,monkeypatch):
    client=BlockingClient();app=Application(repository,tmp_path/'global',client=client)
    await app.open(initialize=True)
    ticket=await app.create_ticket('Close barrier','Preserve the real source write.')
    await app.move_ticket(ticket['id'],'In Progress')
    await asyncio.wait_for(client.started.wait(),5)
    await app.pause_run(ticket['id'])
    source=await app.read_source(ticket['id'],'app.py')
    entered=threading.Event();release=threading.Event()
    original=Workspace.write
    def delayed(*args,**kwargs):
        entered.set()
        if not release.wait(10):raise AssertionError('test did not release writer')
        return original(*args,**kwargs)
    monkeypatch.setattr(Workspace,'write',delayed)
    save=asyncio.create_task(app.save_source(ticket['id'],'app.py','saved before close\n',source['hash']))
    await asyncio.wait_for(asyncio.to_thread(entered.wait),5)
    closing=asyncio.create_task(app.close())
    try:
        await asyncio.sleep(.03)
        assert not closing.done() and not app.store.closed and not app.closed
        closing.cancel();closing.cancel()
        await asyncio.sleep(.03)
        assert not app.store.closed and not app.closed
        release.set()
        result=await asyncio.wait_for(save,10)
        assert result
        await asyncio.wait_for(asyncio.gather(closing,return_exceptions=True),10)
        assert app.closed and app.store.closed
        await app.close()
    finally:
        release.set()
        await asyncio.gather(save,closing,return_exceptions=True)
        if not app.closed:await app.close()
    reopened=Application(repository,tmp_path/'global',execution=False);await reopened.open()
    try:
        assert (await reopened.read_source(ticket['id'],'app.py'))['content']=='saved before close\n'
        selected=await reopened.selected_run(ticket['id'])
        assert selected['state']=='paused'
        assert 'Manually edited' in (selected.get('correction') or '') or selected.get('correction')
    finally:await reopened.close()

@pytest.mark.parametrize('during_startup', [False, True])
def test_desktop_stop_waits_for_host_close(monkeypatch, tmp_path, during_startup):
    import queue
    from agilecode.desktop import bridge
    opened=threading.Event(); allow_open=threading.Event()
    closing=threading.Event(); allow_close=threading.Event()
    class Events:
        def subscribe(self, callback): pass
    class Host:
        def __init__(self,*args): self.events=Events(); self.closed=False
        async def open(self,**kwargs):
            opened.set()
            await asyncio.to_thread(allow_open.wait)
        async def close(self):
            if self.closed:return
            closing.set()
            await asyncio.to_thread(allow_close.wait)
            self.closed=True
    monkeypatch.setattr(bridge,'Application',Host)
    worker=bridge.EngineThread(tmp_path,None,False,queue.Queue())
    stopper=None
    try:
        worker.start(); assert opened.wait(3)
        if not during_startup:
            allow_open.set(); assert worker.ready.wait(3)
        stopper=threading.Thread(target=worker.stop);stopper.start()
        if during_startup:
            assert not closing.wait(.05)
            assert stopper.is_alive() and worker.is_alive()
            allow_open.set()
        assert closing.wait(3)
        assert stopper.is_alive() and worker.loop.is_running()
        allow_close.set();stopper.join(3)
        assert not stopper.is_alive() and not worker.is_alive()
        assert worker.app.closed and worker.loop.is_closed()
    finally:
        allow_open.set();allow_close.set()
        if stopper:stopper.join(5)
        elif worker.is_alive():worker.stop()
