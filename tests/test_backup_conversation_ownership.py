import asyncio
import pytest
from agilecode.core.application import Application
from agilecode.core.backup import create_backup
from agilecode.domain.types import AgileCodeError
from conftest import ScriptClient

@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['before_session_admission','provider_setup'])
async def test_backup_uses_shared_request_lifetime_before_model_generation(repository,tmp_path,monkeypatch,phase):
    app=Application(repository,tmp_path/'global',client=ScriptClient());await app.open(initialize=True)
    entered=asyncio.Event();release=asyncio.Event();sending=None
    try:
        discussion=await app.discussion.create('Shared backup lifetime')
        if phase=='before_session_admission':
            original=app.discussion.get
            async def get(identity):
                entered.set();await release.wait();return await original(identity)
            monkeypatch.setattr(app.discussion,'get',get)
        else:
            original=app.client.resolve
            async def resolve(profile):
                entered.set();await release.wait();return await original(profile)
            monkeypatch.setattr(app.client,'resolve',resolve)
        sending=asyncio.create_task(app.discussion.ask(discussion['id'],'Keep this request','backup-request'))
        await asyncio.wait_for(entered.wait(),3)
        assert not app.discussion.engine.active_generations
        with pytest.raises(AgileCodeError,match='idle engine'):
            await create_backup(app,tmp_path/'must-not-exist.zip')
        assert not (tmp_path/'must-not-exist.zip').exists()
    finally:
        await app.discussion.shutdown()
        release.set()
        if sending:await asyncio.gather(sending,return_exceptions=True)
        await app.close()
