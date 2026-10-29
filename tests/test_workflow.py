import asyncio
import json
from pathlib import Path
import pytest
from agilecode.core.application import Application
from agilecode.core.types import Settings
from conftest import ScriptClient, git


@pytest.mark.asyncio
async def test_twelve_steps_final_commit_merge_cleanup(repository, tmp_path):
    app=Application(repository,tmp_path/"global",client=ScriptClient())
    await app.open(initialize=True)
    try:
        await app.config.save(Settings(autonomy="full",verification_commands=["python -m unittest discover -s tests -q"]))
        ticket=await app.create_ticket("Answer two","Update answer and the regression test.")
        await app.move_ticket(ticket["id"],"Ready")
        run=await app.start_ticket(ticket["id"])
        finished=await asyncio.wait_for(app.wait_for_run(run["id"]),30)
        assert finished["state"]=="completed",finished
        attempts=await app.store.query("SELECT * FROM attempts WHERE run_id=? AND phase='development' ORDER BY created",(run["id"],))
        assert [a["step_id"] for a in attempts]==[
            "understand","requirements","review_requirements",
            "architecture","review_architecture","plan","review_plan",
            "implement","verify_design","verify","review","commit",
        ]
        assert len({a["session_id"] for a in attempts})==12
        assert git(repository,"rev-list","--count",f"{run['base_oid']}..{finished['final_oid']}")=="1"
        assert (repository/"app.py").read_text().endswith("return 1\n")
        candidate=await app.prepare_integration(ticket["id"])
        assert candidate["state"]=="awaiting_review",candidate
        integrated=await app.approve_integration(candidate["id"],candidate["candidate_oid"],candidate["target_oid"])
        assert integrated["state"]=="integrated"
        assert (await app.find_ticket(ticket["id"]))["state"]=="Done"
        assert (repository/"app.py").read_text().endswith("return 2\n")
        assert not Path(run["workspace"]).exists(),json.loads(integrated["report"])
        assert git(repository,"rev-parse",run["branch"])==finished["final_oid"]
        assert not git(repository,"for-each-ref","--format=%(refname)","refs/agilecode/checkpoints")
    finally: await app.close()


@pytest.mark.asyncio
async def test_question_same_session_and_correction_attempt(repository,tmp_path):
    app=Application(repository,tmp_path/"global",client=ScriptClient(question=True,review_correction=True))
    await app.open(initialize=True)
    try:
        await app.config.save(Settings(autonomy="full"))
        ticket=await app.create_ticket("Answer","Return two")
        await app.move_ticket(ticket["id"],"Ready")
        run=await app.start_ticket(ticket["id"])
        for _ in range(200):
            waits=await app.store.query("SELECT * FROM waits WHERE status='pending'")
            if waits: break
            await asyncio.sleep(.01)
        assert waits
        original=waits[0]["session_id"]
        await app.answer(waits[0]["id"],"Yes, return 2.")
        result=await asyncio.wait_for(app.wait_for_run(run["id"]),30)
        assert result["state"]=="completed",result
        attempts=await app.store.query("SELECT * FROM attempts WHERE run_id=? ORDER BY created",(run["id"],))
        assert [a["session_id"] for a in attempts if a["step_id"]=="understand"]==[original]
        assert len([a for a in attempts if a["step_id"]=="implement"])==2
        assert len([a for a in attempts if a["step_id"]=="verify_design"])==2
        assert len([a for a in attempts if a["step_id"]=="verify"])==2
        assert len([a for a in attempts if a["step_id"]=="review"])==2
    finally: await app.close()


@pytest.mark.asyncio
async def test_configured_correction_limit_allows_that_many_correction_attempts(repository,tmp_path):
    app=Application(repository,tmp_path/"global",client=ScriptClient(review_correction=True))
    await app.open(initialize=True)
    try:
        await app.config.save(Settings(autonomy="full",max_corrections=1))
        ticket=await app.create_ticket("Answer","Return two")
        await app.move_ticket(ticket["id"],"Ready")
        run=await app.start_ticket(ticket["id"])
        result=await asyncio.wait_for(app.wait_for_run(run["id"]),30)
        assert result["state"]=="completed",result
        attempts=await app.store.query(
            "SELECT * FROM attempts WHERE run_id=? AND phase='development' ORDER BY created",
            (run["id"],)
        )
        assert len([a for a in attempts if a["step_id"]=="implement"])==2
        assert len([a for a in attempts if a["step_id"]=="review"])==2
    finally:
        await app.close()
