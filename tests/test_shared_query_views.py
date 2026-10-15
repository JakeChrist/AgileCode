import pytest
from agilecode.adapters.store import ProjectStore
from agilecode.application.query_views import ProjectQueries
from agilecode.domain.types import AgileCodeError

@pytest.mark.asyncio
async def test_cursor_direction_latest_default_and_metadata(tmp_path):
    store=ProjectStore(tmp_path/'db');await store.open()
    try:
        for text in ('a','b','c','d','e'):
            await store.message('s',{'role':'user','content':text})
        query=ProjectQueries(store,lambda:2)
        latest=await query.conversation_page('s')
        assert [r['body']['content'] for r in latest['messages']]==['d','e']
        assert latest['has_older'] and not latest['has_newer']
        first=await query.conversation_page('s',after=0)
        assert [r['body']['content'] for r in first['messages']]==['a','b']
        assert not first['has_older'] and first['has_newer']
        middle=await query.conversation('s',after=first['messages'][-1]['seq'])
        assert [r['body']['content'] for r in middle]==['c','d']
        previous=await query.conversation('s',before=latest['messages'][0]['seq'])
        assert [r['body']['content'] for r in previous]==['b','c']
        assert await query.conversation('missing')==[]
        for kwargs in ({'before':1,'after':0},{'after':-1},{'limit':0},{'before':True}):
            with pytest.raises(AgileCodeError):await query.conversation('s',**kwargs)
    finally:await store.close()
