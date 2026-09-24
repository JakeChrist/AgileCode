import asyncio
import json
import sys
import os
import sqlite3
from pathlib import Path
import pytest
from pydantic import ValidationError
from agilecode.core.defaults import standard_team
from agilecode.core.types import Team,Settings,ModelProfile,Process,Step,StepResult,ResultStatus,Conflict,AgileCodeError
from agilecode.core.application import Application
from agilecode.adapters.store import ProjectStore
from conftest import ScriptClient


def test_team_owns_process_members():
    raw=standard_team().model_dump(mode="json")
    raw["processes"][0]["steps"][0]["team_member_id"]="not-in-team"
    with pytest.raises(ValidationError):Team.model_validate(raw)


def test_standard_stages_cannot_skip():
    raw=standard_team().model_dump(mode="json");raw["processes"][0]["steps"].pop(2)
    with pytest.raises(ValidationError):Team.model_validate(raw)


def test_custom_process_can_change_sequence_but_not_create_forward_correction():
    team=standard_team().model_dump(mode="json");process=team["processes"][0];process["standard"]=False;process["steps"].pop(2)
    assert len(Team.model_validate(team).processes[0].steps)==7
    process["steps"][0]["correction_target"]="plan"
    with pytest.raises(ValidationError):Team.model_validate(team)


@pytest.mark.parametrize("change",[
    {"base_url":"file:///tmp/local"},{"base_url":"http://key:password@localhost/v1"},
    {"max_tokens":32768},{"request_options":{"messages":[]}},{"context_window":1},
])
def test_invalid_model_configuration(change):
    with pytest.raises(ValidationError):ModelProfile(**change)


def test_structured_step_results_reject_unknown_status():
    with pytest.raises(ValidationError):StepResult(status="probably done",summary="fine",output="ok")


def test_no_qt_in_core_imports():
    import subprocess
    result=subprocess.run([sys.executable,"-c","import agilecode.core.application,sys; assert not any(x.startswith('PySide6') for x in sys.modules)"],capture_output=True,text=True,env={**os.environ,"PYTHONPATH":str(Path(__file__).parents[1]/"src")})
    assert result.returncode==0,result.stderr


@pytest.mark.asyncio
async def test_sqlite_transactions_rollback_and_concurrent_writes(tmp_path):
    store=ProjectStore(tmp_path/"project.db");await store.open()
    try:
        def broken(c):
            c.execute("INSERT INTO meta VALUES('bad','value')")
            raise RuntimeError("rollback")
        with pytest.raises(RuntimeError):await store.transaction(broken)
        assert await store.one("SELECT * FROM meta WHERE key='bad'") is None
        await asyncio.gather(*(store.execute("INSERT INTO meta VALUES(?,?)",(str(i),str(i))) for i in range(50)))
        assert (await store.one("SELECT COUNT(*) n FROM meta"))["n"]==50
        await store.backup(tmp_path/"backup.db")
        c=sqlite3.connect(tmp_path/"backup.db");assert c.execute("PRAGMA integrity_check").fetchone()[0]=="ok";c.close()
    finally:await store.close()


@pytest.mark.asyncio
async def test_newer_database_rejected_without_downgrade(tmp_path):
    path=tmp_path/"project.db";c=sqlite3.connect(path);c.execute("PRAGMA user_version=999");c.close()
    store=ProjectStore(path)
    try:
        with pytest.raises(AgileCodeError):await store.open()
    finally:await store.close()
    c=sqlite3.connect(path);assert c.execute("PRAGMA user_version").fetchone()[0]==999;c.close()


@pytest.mark.asyncio
async def test_scoped_team_save_and_independent_global_default(repository,tmp_path):
    app=Application(repository,tmp_path/"global",client=ScriptClient());await app.open(initialize=True)
    try:
        team=standard_team();team.id="custom";team.name="My reusable team"
        assert await app.config.save_team(team,"global")=="global:custom"
        team.name="Project team";await app.config.save_team(team,"project")
        assert (await app.config.get_team("global:custom")).name=="My reusable team"
        assert (await app.config.get_team("project:custom")).name=="Project team"
        assert len((await app.config.get_team("global:custom")).processes)==1
        await app.config.save(Settings(theme="light"),"project")
        await app.config.save(Settings(theme="dark"),"global")
        await app.config.load()
        assert app.config.settings.theme=="light"
    finally:await app.close()


@pytest.mark.asyncio
async def test_tickets_default_assignment_revisions_dependencies_and_idempotency(repository,tmp_path):
    app=Application(repository,tmp_path/"global",client=ScriptClient());await app.open(initialize=True)
    try:
        a=await app.create_ticket("A","A",request_id="request-id")
        repeat=await app.create_ticket("ignored repeated request","ignored",request_id="request-id")
        assert a["id"]==repeat["id"]
        assert a["team_ref"]=="builtin:standard" and a["process_id"]=="development"
        b=await app.create_ticket("B","B")
        await app.add_dependency(b["id"],a["id"])
        with pytest.raises(AgileCodeError):await app.add_dependency(a["id"],b["id"])
        await app.move_ticket(b["id"],"Ready")
        with pytest.raises(AgileCodeError):await app.start_ticket(b["id"])
        edited=await app.edit_ticket(a["id"],expected_revision=a["revision"],title="A renamed")
        assert edited["revision"]>a["revision"]
        with pytest.raises(Conflict):await app.edit_ticket(a["id"],expected_revision=a["revision"],title="stale")
        with pytest.raises(AgileCodeError):await app.move_ticket(a["id"],"Done")
    finally:await app.close()
