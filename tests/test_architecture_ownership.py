"""Executable ownership contracts, including mutation-tested source guards.

These guards complement behavioral tests: a second implementation that happens
to produce the same answer must still fail.  They inspect the installed source
tree and exercise the real GUI, CLI, and registered PM adapters.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from agilecode.application.tickets import TicketOperations
from agilecode.core.application import Application
from agilecode.core.types import AgileCodeError, ModelProfile, StepResult
from agilecode.runtime.conversation import ConversationConfig, ConversationEngine
from conftest import ScriptClient, git


SOURCE = Path(inspect.getfile(Application)).resolve().parents[1]
OWNERS = {
    **{name: ("application/tickets.py", "tickets") for name in (
        "create_ticket", "edit_ticket", "move_ticket", "archive_ticket",
        "add_dependency", "remove_dependency", "delete_epic",
    )},
    **{name: ("application/execution.py", "execution_ops") for name in (
        "delete_ticket", "start_ticket", "pause_run", "resume_run", "cancel_run",
    )},
    **{name: ("application/services/workspace.py", "workspace_ops") for name in (
        "save_source", "restore_checkpoint",
    )},
}


def _sources():
    return {str(path.relative_to(SOURCE)): path.read_text(encoding="utf-8")
            for path in SOURCE.rglob("*.py")}


def _attribute(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _attribute(node.value) + "." + node.attr
    return ""


def _forwarder(node, target):
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body.pop(0)  # A compatibility docstring does not introduce behavior.
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return False
    awaited = body[0].value
    if not isinstance(awaited, ast.Await) or not isinstance(awaited.value, ast.Call):
        return False
    call = awaited.value
    return (_attribute(call.func) == target
            and len(call.args) == 1 and isinstance(call.args[0], ast.Starred)
            and isinstance(call.args[0].value, ast.Name) and call.args[0].value.id == "args"
            and len(call.keywords) == 1 and call.keywords[0].arg is None
            and isinstance(call.keywords[0].value, ast.Name)
            and call.keywords[0].value.id == "kwargs")


def _operation_violations(sources):
    errors, found = [], {name: [] for name in OWNERS}
    for path, source in sources.items():
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in OWNERS:
                continue
            owner, facade_target = OWNERS[node.name]
            label = f"{path}:{node.lineno} {node.name}"
            if path == owner:
                found[node.name].append(label)
            elif path == "core/application.py":
                if not _forwarder(node, f"self.{facade_target}.{node.name}"):
                    errors.append(label + " contains behavior in the compatibility facade")
            elif path == "application/execution.py" and node.name == "move_ticket":
                if not _forwarder(node, "self.tickets.move_ticket"):
                    errors.append(label + " duplicates ticket movement")
            else:
                errors.append(label + " is a second operation implementation")
    for name, definitions in found.items():
        if len(definitions) != 1:
            errors.append(f"{name} needs one canonical definition, found {definitions}")
    return errors


def _client_violations(sources):
    # Derive PM entry points from the registered tools, so adding a new tool
    # also subjects its callback to the boundary rather than silently escaping.
    factory = ast.parse(sources["runtime/tool_factories.py"])
    callbacks = {
        call.args[-1].attr for call in ast.walk(factory)
        if isinstance(call, ast.Call) and _attribute(call.func) == "CallbackTool"
        and call.args and isinstance(call.args[-1], ast.Attribute)
        and _attribute(call.args[-1].value) == "pm"
    }
    errors = []
    for path, source in sources.items():
        if path == "core/project_manager.py":
            regions = [node for node in ast.walk(ast.parse(source))
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and node.name in callbacks]
        elif path == "cli.py" or path.startswith("desktop/") or path == "runtime/pm_tools.py":
            regions = [ast.parse(source)]
        else:
            continue
        for region in regions:
            aliases = {}
            # Track ordinary local aliases, including an alias of an alias.
            def resolved(node):
                value = _attribute(node)
                first, dot, rest = value.partition(".")
                return aliases.get(first, first) + (dot + rest if dot else "")
            for node in sorted(ast.walk(region), key=lambda item: getattr(item, "lineno", 0)):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            aliases[target.id] = resolved(node.value)
                if not isinstance(node, ast.Call):
                    continue
                name = resolved(node.func)
                parts = name.split(".")
                storage = {"store", "conn", "connection", "db"}.intersection(parts[:-1])
                if storage and parts[-1] in {"insert", "update", "execute", "executemany", "transaction"}:
                    errors.append(f"{path}:{node.lineno} raw storage mutation {name}")
                if "git" in parts[:-1] and parts[-1] in {
                    "run", "commit", "add_worktree", "remove_worktree", "restore_tree", "reset",
                }:
                    errors.append(f"{path}:{node.lineno} raw Git operation {name}")
    return errors


def _state_and_conversation_violations(sources):
    errors, streams, engines = [], [], []
    for path, source in sources.items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "ConversationEngine":
                engines.append(path)
            if not isinstance(node, ast.Call):
                continue
            name = _attribute(node.func)
            if name.endswith(".stream") and path not in {"adapters/model.py", "adapters/mcp.py"}:
                streams.append(path)
                if path != "runtime/conversation.py":
                    errors.append(f"{path}:{node.lineno} provider dispatch outside the conversation engine")
            if path == "application/tickets.py":
                continue
            if (name.endswith(".update") and node.args
                    and isinstance(node.args[0], ast.Constant) and node.args[0].value == "tickets"
                    and any(keyword.arg == "state" for keyword in node.keywords)):
                errors.append(f"{path}:{node.lineno} generic ticket state update")
            if name.endswith((".execute", ".executemany")) and node.args:
                # Constants inside concatenated SQL catch both ordinary SQL and
                # an injected literal prefix before bound parameters.
                literals = [item.value for item in ast.walk(node.args[0])
                            if isinstance(item, ast.Constant) and isinstance(item.value, str)]
                if any(re.search(r"\bUPDATE\s+[\"`\[]?tickets[\"`\]]?\s+SET\s+.*\bstate\s*=", value, re.I | re.S)
                       for value in literals):
                    errors.append(f"{path}:{node.lineno} raw ticket state SQL")
    if streams != ["runtime/conversation.py"]:
        errors.append(f"Expected one provider dispatch implementation, found {streams}")
    if engines != ["runtime/conversation.py"]:
        errors.append(f"Expected one ConversationEngine definition, found {engines}")
    return errors


def test_source_has_one_operation_owner_and_only_forwarding_facades():
    assert _operation_violations(_sources()) == []


def test_gui_cli_and_registered_pm_tools_cannot_write_store_or_git_directly():
    assert _client_violations(_sources()) == []


def test_ticket_state_and_provider_dispatch_have_single_owners():
    assert _state_and_conversation_violations(_sources()) == []


@pytest.mark.parametrize("path,injection,guard,reason", [
    ("desktop/bridge.py", "async def move_ticket(store, ticket):\n    return ticket\n",
     _operation_violations, "second operation implementation"),
    ("cli.py", "async def bypass(app):\n    db = app.store\n    await db.update('tickets', 't', title='bypass')\n",
     _client_violations, "raw storage mutation"),
    ("runtime/pm_tools.py", "async def bypass(app):\n    repo = app.git\n    await repo.run('reset', '--hard')\n",
     _client_violations, "raw Git operation"),
    ("core/runner.py", "async def duplicate(client, messages):\n    while True:\n        await client.stream(None, messages, [], None)\n",
     _state_and_conversation_violations, "provider dispatch outside"),
    ("application/execution.py", "async def bypass(store):\n    await store.execute(\"UPDATE tickets SET state='Done' WHERE id=?\", ('t',))\n",
     _state_and_conversation_violations, "raw ticket state SQL"),
    ("application/execution.py", "async def bypass(store):\n    await store.update('tickets', 't', state='Done')\n",
     _state_and_conversation_violations, "generic ticket state update"),
])
def test_source_guards_reject_deliberate_duplicate_or_bypass(path, injection, guard, reason):
    sources = _sources()
    assert guard(sources) == []
    sources[path] += "\n" + injection  # Disposable strings; production files stay untouched.
    assert any(reason in error for error in guard(sources))


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["gui", "cli", "pm"])
async def test_real_move_adapters_reach_canonical_operation(repository, tmp_path, monkeypatch, caller):
    app = Application(repository, tmp_path / "global", execution=False)
    await app.open(initialize=True)
    ticket = await app.create_ticket("Ownership", "Move through the actual interface")
    original, calls, writes = TicketOperations.move_ticket, [], []

    def trace(sql):
        if re.search(r"\bUPDATE\s+tickets\s+SET\s+.*\bstate\s*=", sql, re.I):
            frame = inspect.currentframe().f_back
            writes.append((sql, frame.f_globals["__name__"], frame.f_code.co_qualname))

    async def observed(owner, *args, **kwargs):
        calls.append((owner, args, kwargs))
        # SQLite runs on the store's worker. Inspect the *actual SQL writer*
        # there, rather than inferring persistence ownership from a call name.
        await owner.store.call(lambda: owner.store.conn.set_trace_callback(trace))
        try:
            return await original(owner, *args, **kwargs)
        finally:
            await owner.store.call(lambda: owner.store.conn.set_trace_callback(None))

    monkeypatch.setattr(TicketOperations, "move_ticket", observed)
    request_id = "ownership-" + caller
    try:
        if caller == "gui":
            from agilecode.desktop.bridge import Bridge
            bridge = SimpleNamespace(worker=SimpleNamespace(app=app))
            result = await Bridge._execute(bridge, "move", {
                "ticket": ticket["id"], "state": "Ready", "revision": ticket["revision"],
                "request_id": request_id,
            })
        elif caller == "pm":
            from agilecode.core.project_manager import ProjectManager
            pm = ProjectManager(app)
            tool = pm._tool_manager().tools["move_ticket"]
            result = await tool.execute({"ticket": ticket["id"], "state": "Ready"}, request_id)
        else:
            from agilecode.cli import execute, parser
            await app.close()  # The real CLI owns and closes its own host.
            result = await execute(parser().parse_args([
                "-p", str(repository), "--global-dir", str(tmp_path / "global"),
                "move", ticket["id"], "Ready", "--request-id", request_id,
            ]))
        assert len(calls) == 1
        owner, args, kwargs = calls[0]
        assert type(owner) is TicketOperations
        assert args == (ticket["id"], "Ready") and kwargs["request_id"] == request_id
        assert result["state"] == "Ready" and result["movement"]["board_committed"]
        assert result["movement"]["operation_id"] == request_id
        assert len(writes) == 1
        assert writes[0][1:] == ("agilecode.application.tickets", "TicketOperations.move_ticket.<locals>.commit")
        assert "state='Ready'" in writes[0][0] and ticket["id"] in writes[0][0]
        if caller == "pm":
            assert kwargs["caller"]["scope"] == "project_manager"
        else:
            assert kwargs.get("caller", "manual") == "manual"
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_generic_updates_cannot_change_ticket_state_even_with_other_fields(repository, tmp_path):
    app = Application(repository, tmp_path / "global", execution=False)
    await app.open(initialize=True)
    try:
        ticket = await app.create_ticket("Keep this title", "Original")
        for state in ("Backlog", "Ready", "In Progress", "Done"):
            with pytest.raises(AgileCodeError, match="move_ticket"):
                await app.store.update("tickets", ticket["id"], state=state, title="Bypass")
            assert await app.find_ticket(ticket["id"]) == ticket
        with pytest.raises(AgileCodeError, match="Unsupported ticket field"):
            await app.edit_ticket(ticket["id"], expected_revision=ticket["revision"], state="Done")
        moved = await app.move_ticket(ticket["id"], "Ready", request_id="authorized-owner")
        assert moved["state"] == "Ready" and moved["title"] == "Keep this title"
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_all_three_role_adapters_call_the_same_conversation_method(repository, tmp_path, monkeypatch):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    calls = []

    async def observed(engine, config, trigger=None):
        assert type(engine) is ConversationEngine and isinstance(config, ConversationConfig)
        calls.append((config.session_id, trigger, inspect.currentframe().f_back.f_globals["__name__"]))
        if config.session_id == "ownership-step":
            return {"status": "finished", "step_result": {
                "status": "PASS", "summary": "One engine", "output": "Shared result",
            }}
        return {"status": "completed", "content": "Shared result"}

    monkeypatch.setattr(ConversationEngine, "run_turn", observed)
    try:
        discussion = await app.discussion.create("Ownership discussion")
        assert (await app.discussion.ask(discussion["id"], "Check this", input_id="discussion-input"))["content"] == "Shared result"
        # _execute_turn is the actual PM adapter, without scheduling an unrelated
        # autonomous turn. Its config and return presentation remain real.
        app.project_manager.state["status"] = "running"
        await app.project_manager._execute_turn(user_initiated=True, trigger="pm-input")
        context = SimpleNamespace(attempt={"id": "attempt", "session_id": "ownership-step"},
                                  ticket={"id": "ticket"}, run={"id": "run"}, phase="development")
        result = await app.processor.agent.run(context, object(), ModelProfile(),
                                               SimpleNamespace(check=lambda: None), "Ticket prompt")
        assert isinstance(result, StepResult) and result.output == "Shared result"
        assert [(session, module) for session, _, module in calls] == [
            (discussion["session_id"], "agilecode.core.discussion"),
            ("project-manager", "agilecode.core.project_manager"),
            ("ownership-step", "agilecode.core.runner"),
        ]
        assert calls[0][1]["id"] and calls[1][1] == "pm-input" and calls[2][1] == "attempt"
        assert app.client.calls == 0  # The interception is at the common engine boundary.
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_auto_complete_edits_only_permission_and_never_request_work(repository, tmp_path, monkeypatch):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        tickets = []
        for state in ("Backlog", "Ready", "In Progress", "Done"):
            ticket = await app.create_ticket(state, "Permission has no completion side effect")
            await app.move_ticket(ticket["id"], state, activate=False)
            tickets.append(await app.find_ticket(ticket["id"]))
        before_operations = await app.store.query("SELECT * FROM operations ORDER BY id")
        before_head = git(repository, "rev-parse", "HEAD")

        async def unexpected(*args, **kwargs):
            pytest.fail("Changing Auto Complete requested movement or execution")

        monkeypatch.setattr(app.tickets, "move_ticket", unexpected)
        for name in ("start_ticket", "continue_ticket", "prepare_integration"):
            monkeypatch.setattr(app.execution_ops, name, unexpected)
        monkeypatch.setattr(app.merges, "prepare", unexpected)
        monkeypatch.setattr(app.merges, "apply", unexpected)
        for permission in (True, False, True):
            await app.config.save(app.config.settings.model_copy(update={"merge_on_completion": permission}))
            assert app.config.settings.merge_on_completion is permission
        for ticket in tickets:
            for permission in (True, False, None):
                current = await app.find_ticket(ticket["id"])
                changed = await app.edit_ticket(ticket["id"], expected_revision=current["revision"], merge_auto=permission)
                assert changed["merge_auto"] == permission
                ignored = {"merge_auto", "revision", "updated"}
                assert {k: v for k, v in changed.items() if k not in ignored} == {
                    k: v for k, v in ticket.items() if k not in ignored}
        assert await app.store.query("SELECT * FROM operations ORDER BY id") == before_operations
        assert not await app.store.query("SELECT * FROM runs")
        assert not await app.store.query("SELECT * FROM integrations")
        assert not app.tasks and not app.integration_tasks and app.client.calls == 0
        assert git(repository, "rev-parse", "HEAD") == before_head
    finally:
        await app.close()
