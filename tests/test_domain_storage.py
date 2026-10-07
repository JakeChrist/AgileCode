import asyncio
import json
import sys
import os
import sqlite3
from pathlib import Path
import pytest
from pydantic import ValidationError
from agilecode.core.defaults import requirements_engineering_team, standard_team
from agilecode.core.types import Team,TeamMember,Settings,ModelProfile,ProviderConfig,Process,Step,StepResult,ResultStatus,Conflict,AgileCodeError
from agilecode.core.application import Application
from agilecode.adapters.store import ProjectStore
from agilecode.adapters.model import Turn
from conftest import ScriptClient


def test_team_owns_process_members():
    raw=standard_team().model_dump(mode="json")
    raw["processes"][0]["steps"][0]["team_member_id"]="not-in-team"
    with pytest.raises(ValidationError):Team.model_validate(raw)


def test_legacy_member_tool_groups_normalize_to_explicit_names():
    raw = standard_team().model_dump(mode="json")
    raw["format_version"] = 1
    raw["members"][0]["allowed_tools"] = ["read", "execute", "git"]
    member = Team.model_validate(raw).members[0]
    assert member.allowed_tools == [
        "list_files", "read_file", "read_job_output", "read_artifact",
        "run_command", "stop_job", "git_command",
        "update_progress", "ask_user", "finish_step",
    ]
    assert Team.model_validate(raw).format_version == 2


def test_standard_stages_cannot_skip():
    raw=standard_team().model_dump(mode="json");raw["processes"][0]["steps"].pop(2)
    with pytest.raises(ValidationError):Team.model_validate(raw)


def test_standard_team_uses_specialized_engineering_roles():
    team = standard_team()
    process = team.process("development")
    assert [(step.id, step.team_member_id) for step in process.steps] == [
        ("understand", "requirements-engineer"),
        ("requirements", "requirements-engineer"),
        ("architecture", "architect"),
        ("plan", "implementation-planner"),
        ("implement", "code"),
        ("verify_design", "verification-validation-engineer"),
        ("verify", "verification-validation-engineer"),
        ("review", "code-reviewer"),
        ("commit", "git-committer"),
    ]
    assert [step.name for step in process.steps] == [
        "Read Ticket",
        "Define Requirements",
        "Define Technical Changes",
        "Plan Implementation",
        "Implement Changes",
        "Design Verification & Validation",
        "Run Verification & Validation",
        "Review Code",
        "Commit Work",
    ]

    code = team.member("code")
    vv = team.member("verification-validation-engineer")
    reviewer = team.member("code-reviewer")
    committer = team.member("git-committer")
    assert "final_commit" not in code.allowed_tools
    assert "production-test-design" in vv.skills
    assert {"write_file", "edit_file", "run_command", "record_check"} <= set(vv.allowed_tools)
    assert "write_file" not in reviewer.allowed_tools
    assert "final_commit" in committer.allowed_tools
    assert "write_file" not in committer.allowed_tools


def test_requirements_engineering_team_has_expected_members_and_procedures():
    team = requirements_engineering_team()

    assert team.id == "requirements-engineering"
    assert team.default_process_id == "requirements-definition"
    assert [(member.id, member.name) for member in team.members] == [
        ("requirements-engineer", "Requirements Engineer"),
        ("requirements-manager", "Requirements Manager"),
        ("verification-validation-engineer", "Verification & Validation Engineer"),
    ]
    assert [(process.id, process.name) for process in team.processes] == [
        ("stakeholder-needs-definition", "Define Stakeholder Needs"),
        ("requirements-definition", "Define Requirements"),
        ("requirements-validation", "Validate Requirements"),
        ("requirements-change-management", "Manage Requirements Change"),
        ("product-verification", "Verify Product Against Requirements"),
        ("product-validation", "Validate Product Against Stakeholder Needs"),
    ]
    assert all(not process.standard for process in team.processes)
    assert all(step.kind == "analysis" for process in team.processes for step in process.steps)


def test_requirements_engineering_procedure_step_ownership():
    team = requirements_engineering_team()
    assert [(step.id, step.team_member_id) for step in team.process("stakeholder-needs-definition").steps] == [
        ("analyze-proposal", "requirements-engineer"),
        ("develop-stakeholder-needs", "requirements-engineer"),
        ("review-stakeholder-needs", "verification-validation-engineer"),
        ("revise-stakeholder-needs", "requirements-engineer"),
        ("establish-needs-traceability", "requirements-manager"),
    ]
    assert [(step.id, step.team_member_id) for step in team.process("requirements-definition").steps] == [
        ("derive-requirements", "requirements-engineer"),
        ("analyze-requirements", "requirements-engineer"),
        ("define-verification-criteria", "verification-validation-engineer"),
        ("review-requirements", "verification-validation-engineer"),
        ("revise-requirements", "requirements-engineer"),
        ("baseline-requirements", "requirements-manager"),
    ]
    assert [(step.id, step.team_member_id) for step in team.process("requirements-validation").steps] == [
        ("trace-requirements-to-needs", "requirements-manager"),
        ("evaluate-requirements-coverage", "verification-validation-engineer"),
        ("evaluate-requirements-quality", "verification-validation-engineer"),
        ("produce-validation-findings", "verification-validation-engineer"),
        ("resolve-validation-findings", "requirements-engineer"),
        ("update-validated-baseline", "requirements-manager"),
    ]
    assert [(step.id, step.team_member_id) for step in team.process("requirements-change-management").steps] == [
        ("analyze-change-request", "requirements-engineer"),
        ("perform-impact-analysis", "requirements-manager"),
        ("develop-requirement-changes", "requirements-engineer"),
        ("review-requirement-changes", "verification-validation-engineer"),
        ("update-change-traceability", "requirements-manager"),
        ("establish-new-baseline", "requirements-manager"),
    ]
    assert [(step.id, step.team_member_id) for step in team.process("product-verification").steps] == [
        ("identify-applicable-requirements", "requirements-manager"),
        ("select-verification-methods", "verification-validation-engineer"),
        ("collect-verification-evidence", "verification-validation-engineer"),
        ("evaluate-requirement-compliance", "verification-validation-engineer"),
        ("review-verification-results", "requirements-engineer"),
        ("record-verification-status", "requirements-manager"),
    ]
    assert [(step.id, step.team_member_id) for step in team.process("product-validation").steps] == [
        ("identify-applicable-stakeholder-needs", "requirements-manager"),
        ("examine-intended-use", "verification-validation-engineer"),
        ("evaluate-need-satisfaction", "verification-validation-engineer"),
        ("identify-requirements-deficiencies", "requirements-engineer"),
        ("record-validation-results", "requirements-manager"),
    ]



def test_custom_process_can_change_sequence_but_not_create_forward_correction():
    team=standard_team().model_dump(mode="json");process=team["processes"][0];process["standard"]=False;process["steps"].pop(2)
    assert len(Team.model_validate(team).processes[0].steps)==8
    process["steps"][0]["correction_target"]="plan"
    with pytest.raises(ValidationError):Team.model_validate(team)


@pytest.mark.parametrize("change",[
    {"base_url":"file:///tmp/local"},{"base_url":"http://key:password@localhost/v1"},
    {"request_options":{"messages":[]}},{"context_window":1},
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
        builtins = {item["ref"]: item["team"]["name"] for item in await app.config.list_teams() if item["scope"] == "builtin"}
        assert builtins == {
            "builtin:standard": "Standard Development Team",
            "builtin:requirements-engineering": "Requirements Engineering Team",
        }
        requirements_team = await app.config.get_team("builtin:requirements-engineering")
        assert requirements_team.default_process_id == "requirements-definition"

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


@pytest.mark.asyncio
async def test_project_discussions_are_independent_persistent_and_linkable(repository,tmp_path):
    app=Application(repository,tmp_path/"global",client=ScriptClient());await app.open(initialize=True)
    try:
        first=await app.discussion.create()
        second=await app.discussion.create("Architecture ideas")
        assert first["session_id"]!=second["session_id"]
        await app.store.message(first["session_id"],{"role":"user","content":"Should checks be separated?"})
        await app.store.message(first["session_id"],{"role":"assistant","content":"Keep them together for now."})
        await app.store.message(second["session_id"],{"role":"user","content":"How should the modules be organized?"})
        assert len((await app.discussion.history(first["id"]))["messages"])==2
        assert len((await app.discussion.history(second["id"]))["messages"])==1

        renamed=await app.discussion.rename(first["id"],"Checks and captures")
        assert renamed["title"]=="Checks and captures"
        ticket=await app.create_ticket("Refine combat rules","Implement the agreed cleanup.",source_discussion_id=first["id"])
        source=await app.discussion.source_for_ticket(ticket["id"])
        assert source and source["id"]==first["id"]
        context=await app.discussion.context_for_ticket(ticket["id"])
        assert "Should checks be separated?" in context["transcript"]
        assert "Keep them together for now." in context["transcript"]

        archived=await app.discussion.archive(second["id"],True)
        assert archived["archived"]==1
        assert second["id"] not in {item["id"] for item in await app.discussion.list()}
        assert second["id"] in {item["id"] for item in await app.discussion.list(include_archived=True)}
    finally:await app.close()


class DiscussionCreateTicketClient(ScriptClient):
    async def stream(self, profile, messages, tools, on_delta=None):
        names = [tool["function"]["name"] for tool in tools]
        if "create_ticket" in names:
            tool_results = [message for message in messages if message.get("role") == "tool"]
            if tool_results:
                payload = json.loads(tool_results[-1]["content"])
                return Turn(
                    {"role": "assistant", "content": f"Created {payload['data']['key']}."},
                    "fixture-model",
                    None,
                    "stop",
                )
            return Turn(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "create-ticket-1",
                        "type": "function",
                        "function": {
                            "name": "create_ticket",
                            "arguments": json.dumps({
                                "title": "Capture discussion decision",
                                "description": "Implement the project decision recorded in this discussion.",
                            }),
                        },
                    }],
                },
                "fixture-model",
                None,
                "tool_calls",
            )
        return await super().stream(profile, messages, tools, on_delta)


@pytest.mark.asyncio
async def test_project_discussion_can_create_linked_backlog_ticket(repository,tmp_path):
    app=Application(repository,tmp_path/"global",client=DiscussionCreateTicketClient());await app.open(initialize=True)
    try:
        discussion=await app.discussion.create("Ticket creation")
        result=await app.discussion.ask(discussion["id"],"Create a ticket for this decision.")
        tickets=await app.board()

        assert result["content"]=="Created T-001."
        assert len(tickets)==1
        ticket=tickets[0]
        assert ticket["title"]=="Capture discussion decision"
        assert ticket["state"]=="Backlog"
        source=await app.discussion.source_for_ticket(ticket["id"])
        assert source and source["id"]==discussion["id"]

        calls=await app.store.query(
            "SELECT name,status FROM tools WHERE session_id=? ORDER BY created",
            (discussion["session_id"],),
        )
        assert calls==[{"name":"create_ticket","status":"completed"}]
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_legacy_singleton_project_discussion_is_preserved_on_reopen(repository,tmp_path):
    global_root=tmp_path/"global"
    app=Application(repository,global_root,client=ScriptClient());await app.open(initialize=True)
    await app.store.message("project-discussion",{"role":"user","content":"Legacy design question"})
    await app.store.message("project-discussion",{"role":"assistant","content":"Legacy answer"})
    await app.close()

    reopened=Application(repository,global_root,client=ScriptClient());await reopened.open()
    try:
        discussions=await reopened.discussion.list()
        legacy=next(item for item in discussions if item["session_id"]=="project-discussion")
        history=await reopened.discussion.history(legacy["id"])
        assert [m["body"]["content"] for m in history["messages"]]==["Legacy design question","Legacy answer"]
        assert "Legacy design question" in legacy["title"]
    finally:await reopened.close()


@pytest.mark.asyncio
async def test_legacy_request_limit_is_discarded_and_new_defaults_apply(repository,tmp_path):
    global_root=tmp_path/"global"
    global_root.mkdir()
    (global_root/"settings.json").write_text(json.dumps({
        "format_version":1,
        "active_profile":"local",
        "profiles":{"local":{
            "base_url":"http://127.0.0.1:1234/v1",
            "model":None,
            "api_key_env":None,
            "context_window":32768,
            "timeout_seconds":None,
            "request_options":{},
            "round_trip_reasoning":False
        }},
        "max_requests_per_attempt":100
    }))
    app=Application(repository,global_root,client=ScriptClient())
    await app.open(initialize=True)
    try:
        dumped=app.config.settings.model_dump()
        assert "max_requests_per_attempt" not in dumped
        assert app.config.settings.max_corrections==10
        assert app.config.settings.shell_command_timeout_seconds is None
        profile=app.config.get_provider("local").profile
        assert profile.timeout_seconds is None
        assert "context_window" not in profile.model_dump()
        assert app.config.provider_pool.default_provider_id=="local"
        assert (global_root/"providers.json").is_file()
        migrated=json.loads((global_root/"settings.json").read_text())
        assert "profiles" not in migrated and "active_profile" not in migrated
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_provider_pool_is_global_and_ticket_can_reference_saved_provider(repository,tmp_path):
    global_root=tmp_path/"global"
    app=Application(repository,global_root,client=ScriptClient())
    await app.open(initialize=True)
    try:
        app.config.save_provider(
            ProviderConfig(
                id="secondary",
                name="Secondary local model",
                profile=ModelProfile(base_url="http://127.0.0.1:8080/v1",model="secondary-model"),
            ),
            make_default=True,
        )
        ticket=await app.create_ticket("Provider assignment","Use the explicitly selected provider.",provider_id="secondary")
        assert ticket["provider_id"]=="secondary"
        assert app.config.get_provider().id=="secondary"
        assert app.config.get_provider("secondary").profile.model=="secondary-model"
    finally:
        await app.close()

    reopened=Application(repository,global_root,client=ScriptClient())
    await reopened.open()
    try:
        assert reopened.config.get_provider().id=="secondary"
        assert (await reopened.find_ticket(ticket["id"]))["provider_id"]=="secondary"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_legacy_category_tool_rules_migrate_to_exact_tool_names(repository,tmp_path):
    global_root=tmp_path/"global"
    global_root.mkdir()
    settings=Settings().model_dump(mode="json")
    settings["tool_rules"]={"execute":"allow","write":"ask","git":"deny"}
    (global_root/"settings.json").write_text(json.dumps(settings))

    app=Application(repository,global_root,client=ScriptClient())
    await app.open(initialize=True)
    try:
        rules=app.config.settings.tool_rules
        assert "execute" not in rules
        assert "write" not in rules
        assert "git" not in rules
        assert rules["run_command"]=="allow"
        assert rules["stop_job"]=="allow"
        assert rules["write_file"]=="ask"
        assert rules["edit_file"]=="ask"
        assert rules["git_command"]=="deny"
        assert "git_status" not in rules
        assert "git_diff" not in rules
        assert "git_log" not in rules
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_old_exact_git_tool_rules_collapse_to_git_command(repository,tmp_path):
    global_root=tmp_path/"global"
    global_root.mkdir()
    settings=Settings().model_dump(mode="json")
    settings["tool_rules"]={"git_status":"allow","git_diff":"ask","git_log":"allow"}
    (global_root/"settings.json").write_text(json.dumps(settings))

    app=Application(repository,global_root,client=ScriptClient())
    await app.open(initialize=True)
    try:
        rules=app.config.settings.tool_rules
        assert rules["git_command"]=="ask"
        assert not {"git_status","git_diff","git_log"} & rules.keys()
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_legacy_saved_max_tokens_is_discarded(repository,tmp_path):
    global_root=tmp_path/"global"
    global_root.mkdir()
    (global_root/"settings.json").write_text(json.dumps({
        "format_version":1,
        "active_profile":"local",
        "profiles":{"local":{
            "base_url":"http://127.0.0.1:1234/v1",
            "model":None,
            "api_key_env":None,
            "context_window":32768,
            "max_tokens":4096,
            "timeout_seconds":180,
            "request_options":{},
            "round_trip_reasoning":False
        }}
    }))
    app=Application(repository,global_root,client=ScriptClient())
    await app.open(initialize=True)
    try:
        profile=app.config.get_provider("local").profile
        assert "max_tokens" not in profile.model_dump()
        assert "context_window" not in profile.model_dump()
    finally:
        await app.close()


class TicketDraftClient(ScriptClient):
    def __init__(self):
        super().__init__()
        self.draft_messages=None

    async def stream(self,profile,messages,tools,on_delta=None):
        names=[tool["function"]["name"] for tool in tools]
        if names==["draft_ticket"]:
            self.draft_messages=messages
            return Turn({
                "role":"assistant",
                "content":None,
                "tool_calls":[{
                    "id":"draft-1",
                    "type":"function",
                    "function":{
                        "name":"draft_ticket",
                        "arguments":json.dumps({
                            "title":"Add configurable directory restrictions",
                            "description":"Add an editable allowlist of directories AgileCode may modify. Keep the project worktree writable while allowing explicitly configured additional paths. Reads remain available for project/global context, but writes outside the allowlist must be rejected."
                        })
                    }
                }]
            },"fixture-model",None,"tool_calls")
        return await super().stream(profile,messages,tools,on_delta)


@pytest.mark.asyncio
async def test_discussion_create_ticket_generates_editable_draft_without_creating_ticket(repository,tmp_path):
    client=TicketDraftClient()
    app=Application(repository,tmp_path/"global",client=client);await app.open(initialize=True)
    try:
        discussion=await app.discussion.create("Directory restrictions")
        await app.store.message(discussion["session_id"],{"role":"user","content":"I want an editable whitelist of directories the tool can touch."})
        await app.store.message(discussion["session_id"],{"role":"assistant","content":"The ticket worktree should remain writable, with explicitly allowed extra paths."})
        draft=await app.discussion.draft_ticket(discussion["id"])

        assert draft["discussion"]["id"]==discussion["id"]
        assert draft["title"]=="Add configurable directory restrictions"
        assert "editable allowlist" in draft["description"]
        assert not await app.board()

        supplied="\n".join(
            message.get("content","") for message in client.draft_messages
            if isinstance(message.get("content"),str)
        )
        assert "editable whitelist of directories" in supplied
        assert "ticket worktree should remain writable" in supplied
        assert "Return exactly one draft_ticket tool call" in client.draft_messages[0]["content"]
    finally:
        await app.close()
