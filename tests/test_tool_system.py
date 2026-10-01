"""Behavioral coverage of named tools, policy, MCP dispatch and durable recovery."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agilecode.adapters.guard import CommandGuard
from agilecode.adapters.mcp import McpService
from agilecode.adapters.store import ProjectStore, decode
from agilecode.adapters.workspace import Workspace
from agilecode.core.application import Application
from agilecode.core.defaults import standard_team
from agilecode.core.tools import Tool, ToolManager, RunCommandTool, GitCommandTool, MCPTool, CommandPolicy, CreateTicketTool
from agilecode.core.types import Settings, Events, Conflict, NeedsReview, Team, TeamMember, McpServerConfig, NATIVE_TOOL_NAMES, now
from conftest import ScriptClient


def call(name, arguments=None, identity="call-1"):
    return {"id": identity, "function": {"name": name, "arguments": json.dumps(arguments or {})}}


async def request(store, identity="request-1"):
    await store.insert("requests", {"id": identity, "session_id": "session", "model": "fixture",
                                  "input": "{}", "status": "completed", "created": now()})


@pytest.fixture
async def store(tmp_path):
    value = ProjectStore(tmp_path / "test.db")
    await value.open()
    await request(value)
    yield value
    await value.close()


class CounterTool(Tool):
    name = "counter"
    uncertain_on_interrupt = True

    def __init__(self):
        self.calls = 0

    async def execute(self, args, execution_id):
        self.calls += 1
        return {"count": self.calls}


def manager(store, tools, allowed=None, settings=None, wait=None):
    return ToolManager(tools, allowed if allowed is not None else [t.name for t in tools],
                       store=store, events=Events(), settings=lambda: settings or Settings(autonomy="full"),
                       session_id="session", wait=wait)


@pytest.mark.asyncio
async def test_manager_reuses_objects_and_does_not_replay_completed_calls(store):
    tool = CounterTool()
    tools = manager(store, [tool])
    first = await tools.execute(call("counter"), "request-1")
    assert (await tools.execute(call("counter"), "request-1")).payload == first.payload
    await request(store, "request-2")
    await tools.execute(call("counter"), "request-2")
    assert tool.calls == 2
    assert tools.tools["counter"] is tool
    with pytest.raises(Conflict):
        await tools.execute(call("counter", {"changed": True}), "request-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [[], ["*"], ["execute"], ["count*"]])
async def test_unexposed_calls_are_rejected_even_if_model_invents_them(store, allowed):
    tool = CounterTool()
    tools = manager(store, [tool], allowed)
    assert tools.schemas() == []
    result = await tools.execute(call("counter"), "request-1")
    assert result.payload["error"]["code"] == "access_denied"
    assert tool.calls == 0


@pytest.mark.asyncio
async def test_invalid_arguments_record_error_without_running(store):
    tool = CounterTool()
    result = await manager(store, [tool]).execute(call("counter", {"extra": "invalid"}), "request-1")
    assert result.payload["status"] == "error"
    assert tool.calls == 0
    saved = await store.get("tools", result.payload["_execution_id"])
    assert saved["status"] == "completed" and decode(saved["result"]) == result.payload


def test_duplicate_tool_names_are_rejected():
    with pytest.raises(ValueError, match="unique"):
        manager(None, [CounterTool(), CounterTool()])


@pytest.mark.parametrize("name", ["*", "read", "execute", "mcp_*", "git_?", ""])
def test_new_member_config_accepts_only_explicit_names(name):
    with pytest.raises(ValidationError, match="explicit tool names"):
        TeamMember(id="member", name="Member", system_prompt="Role", allowed_tools=[name])


def test_v2_saved_git_tools_collapse_to_git_command():
    raw = standard_team().model_dump(mode="json")
    raw["members"][0]["allowed_tools"] = ["read_file", "git_status", "git_diff", "git_log"]
    member = Team.model_validate(raw).members[0]
    assert member.allowed_tools == ["read_file", "git_command"]


def test_create_ticket_is_selectable_but_not_granted_by_default():
    assert "create_ticket" in NATIVE_TOOL_NAMES
    member = TeamMember(id="member", name="Member", system_prompt="Role")
    assert "create_ticket" not in member.allowed_tools
    assert all("create_ticket" not in member.allowed_tools for member in standard_team().members)


def test_create_ticket_schema_requests_structured_markdown_description():
    tool = CreateTicketTool(lambda *args, **kwargs: None)
    schema = tool.schema()["function"]
    assert "readable Markdown" in schema["description"]
    description = schema["parameters"]["properties"]["description"]["description"]
    assert "readable Markdown" in description
    assert "headings and lists" in description
    assert "dense prose" in description


def test_legacy_team_migration_is_once_and_keeps_completion_tools():
    raw = standard_team().model_dump(mode="json")
    raw["format_version"] = 1
    raw["members"][0]["allowed_tools"] = ["read"]
    team = Team.model_validate(raw)
    assert "finish_step" in team.members[0].allowed_tools
    assert "create_ticket" not in team.members[0].allowed_tools
    team.members[0].allowed_tools = ["read_file"]
    assert Team.model_validate_json(team.model_dump_json()).members[0].allowed_tools == ["read_file"]


@pytest.mark.parametrize("autonomy,expected", [("full", "allow"), ("restricted", "ask")])
def test_autonomy_is_unchanged_without_explicit_command_rules(autonomy, expected):
    policy = CommandPolicy(lambda: Settings(autonomy=autonomy))
    for command in ["pytest", "pip install example", "sudo apt install example", "python -c 'print(1)'"]:
        assert policy.decide(command) == expected


def test_command_rules_are_case_sensitive_ordered_and_match_whole_command():
    settings = Settings(autonomy="restricted", command_rules=[
        {"pattern": "pytest tests/*", "action": "allow"},
        {"pattern": "pytest *", "action": "deny"},
    ])
    policy = CommandPolicy(lambda: settings)
    assert policy.decide("  pytest tests/test_app.py  ") == "allow"
    assert policy.decide("pytest other/") == "deny"
    assert policy.decide("echo ok; pytest tests/test_app.py") == "ask"
    assert policy.decide("PYTEST tests/test_app.py") == "ask"
    settings.tool_rules["run_command"] = "deny"
    assert policy.decide("pytest tests/test_app.py") == "deny"


class FakeShell:
    def __init__(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        scope = Workspace(work, work, tmp_path / "global")
        self.shell = SimpleNamespace(cwd=work, guard=CommandGuard(scope))
        self.commands = []

    async def run(self, command, *args, **kwargs):
        self.commands.append(command)
        return {"status": "completed", "exit_code": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("action,answer,status,count", [
    ("allow", "approve", "ok", 1), ("ask", "approve", "ok", 1),
    ("ask", "deny", "error", 0), ("deny", "approve", "error", 0),
])
async def test_command_allow_ask_deny_through_manager(store, tmp_path, action, answer, status, count):
    settings = Settings(autonomy="full", command_rules=[{"pattern": "pytest", "action": action}])
    shell = FakeShell(tmp_path)
    tool = RunCommandTool(shell, lambda: settings, "step", asyncio.Lock())
    waits = []
    async def wait(*args):
        waits.append(args)
        return answer
    result = await manager(store, [tool], settings=settings, wait=wait).execute(call("run_command", {"command": "pytest"}), "request-1")
    assert result.payload["status"] == status
    assert len(shell.commands) == count
    assert len(waits) == (1 if action == "ask" else 0)


@pytest.mark.asyncio
async def test_git_command_accepts_only_git_executable(store, tmp_path):
    settings = Settings(autonomy="full")
    shell = FakeShell(tmp_path)
    tool = GitCommandTool(shell, lambda: settings, "step", asyncio.Lock())

    ok = await manager(store, [tool], settings=settings).execute(
        call("git_command", {"command": "git status"}),
        "request-1",
    )
    assert ok.payload["status"] == "ok"
    assert shell.commands == ["git status"]

    await request(store, "request-2")
    rejected = await manager(store, [tool], settings=settings).execute(
        call("git_command", {"command": "pytest"}),
        "request-2",
    )
    assert rejected.payload["status"] == "error"
    assert shell.commands == ["git status"]


@pytest.mark.asyncio
async def test_git_command_keeps_shell_operators_inside_git_invocation(store, tmp_path):
    settings = Settings(autonomy="full")
    shell = FakeShell(tmp_path)
    tool = GitCommandTool(shell, lambda: settings, "step", asyncio.Lock())
    result = await manager(store, [tool], settings=settings).execute(
        call("git_command", {"command": "git status && echo not-a-git-command"}),
        "request-1",
    )
    assert result.payload["status"] == "ok"
    assert shell.commands == ["git status '&&' echo not-a-git-command"]


@pytest.mark.asyncio
async def test_stale_command_approval_cannot_execute(store, tmp_path):
    settings = Settings(autonomy="restricted")
    shell = FakeShell(tmp_path)
    tool = RunCommandTool(shell, lambda: settings, "step", asyncio.Lock())
    async def wait(*args):
        settings.tool_rules["run_command"] = "deny"
        return "approve"
    result = await manager(store, [tool], settings=settings, wait=wait).execute(call("run_command", {"command": "pytest"}), "request-1")
    assert result.payload["error"]["code"] == "stale_state"
    assert not shell.commands


@pytest.mark.asyncio
async def test_explicit_allow_never_overrides_containment(store, tmp_path):
    settings = Settings(autonomy="full", command_rules=[{"pattern": "*", "action": "allow"}])
    shell = FakeShell(tmp_path)
    tool = RunCommandTool(shell, lambda: settings, "step", asyncio.Lock())
    result = await manager(store, [tool], settings=settings).execute(call("run_command", {"command": "rm ../outside"}), "request-1")
    assert result.payload["error"]["code"] == "access_denied"
    assert not shell.commands


@pytest.mark.parametrize("command", [
    "rm *.tmp; git reset --hard", "echo x > $OUTPUT > ../outside", "popd; git commit -am bad",
])
def test_uncertain_construct_does_not_hide_later_explicit_violation(tmp_path, command):
    shell = FakeShell(tmp_path)
    assert shell.shell.guard.validate(command, shell.shell.cwd).decision == "block"


@pytest.mark.asyncio
async def test_cancelled_mutation_is_not_replayed(store):
    started = asyncio.Event()
    class SlowTool(CounterTool):
        async def execute(self, args, execution_id):
            self.calls += 1
            started.set()
            await asyncio.Event().wait()
    tool = SlowTool()
    tools = manager(store, [tool])
    task = asyncio.create_task(tools.execute(call("counter"), "request-1"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = await store.one("SELECT * FROM tools")
    assert row["status"] == "unknown"
    with pytest.raises(NeedsReview):
        await tools.execute(call("counter"), "request-1")
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_restart_retains_unknown_mutations(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    await request(app.store)
    await manager(app.store, [CounterTool()]).execute(call("counter"), "request-1")
    await app.store.execute("UPDATE tools SET status='running',result=NULL")
    await app.close()
    reopened = Application(repository, tmp_path / "global", client=ScriptClient())
    await reopened.open()
    try:
        assert (await reopened.store.one("SELECT * FROM tools"))["status"] == "unknown"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_mcp_same_remote_name_on_two_servers_is_distinct_and_validated(store, tmp_path):
    server = str(Path(__file__).parents[1] / "examples/mcp_echo_server.py")
    configs = [McpServerConfig(id=name, command=sys.executable, args=[server], enabled=True) for name in ["one", "two"]]
    mcp = McpService(configs, tmp_path)
    try:
        await mcp.discover()
        assert not mcp.errors
        assert len(mcp.tools) == 2
        aliases = list(mcp.tools)
        tools = manager(store, [MCPTool(mcp, alias, remote) for alias, remote in mcp.tools.items()], [aliases[0]])
        assert [x["function"]["name"] for x in tools.schemas()] == [aliases[0]]
        result = await tools.execute(call(aliases[0], {"text": "hello"}), "request-1")
        assert result.payload["status"] == "ok", result.payload
        invalid = await tools.execute(call(aliases[0], {"text": 123}, "invalid"), "request-1")
        assert invalid.payload["status"] == "error"
        unlisted = await tools.execute(call(aliases[1], {"text": "no"}, "unlisted"), "request-1")
        assert unlisted.payload["error"]["code"] == "access_denied"
        await mcp.close()
        await mcp.discover()
        assert not mcp.errors and list(mcp.tools) == aliases
        resumed = await mcp.call(aliases[0], {"text": "after pause"})
        assert not resumed.get("isError")
    finally:
        await mcp.close()


@pytest.mark.asyncio
async def test_mcp_failure_returns_normal_persisted_error(store):
    class BrokenMcp:
        async def call(self, name, args):
            return {"isError": True, "content": [{"type": "text", "text": "server rejected call"}]}
    tool = MCPTool(BrokenMcp(), "mcp_test_error", {"description": "failure", "schema": {"type": "object"}})
    result = await manager(store, [tool]).execute(call(tool.name), "request-1")
    assert result.payload["status"] == "error"
    assert "server rejected call" in result.payload["error"]["message"]
    assert (await store.get("tools", result.payload["_execution_id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_tool_catalog_returns_selectable_mcp_names(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        server = str(Path(__file__).parents[1] / "examples/mcp_echo_server.py")
        await app.config.save(Settings(mcp_servers=[McpServerConfig(id="docs", command=sys.executable, args=[server], enabled=True)]))
        first = await app.tool_catalog()
        second = await app.tool_catalog()
        assert not first["errors"] and first == second
        assert {"name": "run_command", "label": "run_command"} in first["tools"]
        assert {"name": "git_command", "label": "git_command"} in first["tools"]
        assert not any(t["name"] in {"git_status", "git_diff", "git_log"} for t in first["tools"])
        remote = [t for t in first["tools"] if t["label"].startswith("docs:")]
        assert len(remote) == 1 and remote[0]["name"].startswith("mcp_docs_")
    finally:
        await app.close()


def test_legacy_native_globs_expand_only_once():
    raw = standard_team().model_dump(mode="json")
    raw["format_version"] = 1
    raw["members"][0]["allowed_tools"] = ["git_*", "mcp_*"]
    migrated = Team.model_validate(raw)
    assert migrated.members[0].allowed_tools == ["git_command", "mcp_read_resource", "update_progress", "ask_user", "finish_step"]


@pytest.mark.asyncio
async def test_paused_dispatch_never_enters_tool_or_marks_unstarted_mutation_unknown(store):
    entered = asyncio.Event()
    async def check():
        entered.set()
        await asyncio.Event().wait()
    tool = CounterTool()
    tools = manager(store, [tool])
    tools.check = check
    task = asyncio.create_task(tools.execute(call('counter'), 'request-1'))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tool.calls == 0
    assert (await store.one('SELECT * FROM tools'))['status'] == 'pending'
