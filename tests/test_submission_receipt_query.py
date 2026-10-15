import pytest
from agilecode.adapters.store import ProjectStore
from agilecode.application.query_views import ProjectQueries
from agilecode.runtime.conversation_records import accept_input, include_inputs
from agilecode.domain.types import AgileCodeError

@pytest.mark.asyncio
async def test_submission_query_reports_acceptance_without_replaying_or_mutating(tmp_path):
    store=ProjectStore(tmp_path/'project.db');await store.open()
    queries=ProjectQueries(store,lambda:200)
    try:
        assert await queries.submission_receipt('missing') is None
        accepted=await accept_input(store,'pm','Keep this instruction','send-id')
        assert await queries.submission_receipt('send-id') == accepted
        before=await store.query('SELECT * FROM messages ORDER BY seq')
        for _ in range(2):
            assert (await queries.submission_receipt('send-id',session_id='pm'))['message_id']==accepted['message_id']
        assert await store.query('SELECT * FROM messages ORDER BY seq')==before
        await include_inputs(store,'pm')
        assert (await queries.submission_receipt('send-id'))['included'] is True
        await accept_input(store,'other','Different session','send-id')
        with pytest.raises(AgileCodeError,match='ambiguous'):
            await queries.submission_receipt('send-id')
        assert (await queries.submission_receipt('send-id',session_id='other'))['text']=='Different session'
        assert await queries.submission_receipt('send-id',session_id='unrelated') is None
    finally:await store.close()

@pytest.mark.asyncio
async def test_activity_query_reads_shared_turn_during_setup_and_after_stop(repository,tmp_path):
    import asyncio
    from agilecode.core.application import Application
    from agilecode.runtime.conversation import ConversationConfig
    from agilecode.runtime.tools import ToolManager
    from conftest import ScriptClient
    app=Application(repository,tmp_path/'global',client=ScriptClient());await app.open(initialize=True)
    engine=app.processor.agent.engine
    entered=asyncio.Event();release=asyncio.Event()
    async def profile():
        entered.set();await release.wait()
        return app.config.get_provider().profile
    manager=ToolManager([],[],store=app.store,events=app.events,settings=lambda:app.config.settings,session_id='activity-query')
    config=ConversationConfig('activity-query',profile=profile,tools=lambda:manager)
    task=None
    try:
        receipt=await engine.accept_input(config,'A request','activity-request')
        task=asyncio.create_task(engine.run_turn(config,receipt))
        await asyncio.wait_for(entered.wait(),3)
        snapshot=await app.conversation_activity()
        first_revision=snapshot['conversation_activity_revisions']['activity-query']
        assert snapshot['active_conversations']['activity-query']==engine.activity('activity-query')['turn_id']
        assert first_revision>0
        await engine.cancel('activity-query')
        await asyncio.wait_for(task,3)
        ended=await app.conversation_activity()
        assert 'activity-query' not in ended['active_conversations']
        assert ended['conversation_activity_revisions']['activity-query']>first_revision
        assert not engine.is_active('activity-query')
    finally:
        release.set()
        if task and not task.done():task.cancel()
        if task:await asyncio.gather(task,return_exceptions=True)
        await app.close()
