"""Caller lifecycle boundaries around durable shared conversation outcomes."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from agilecode.adapters.model import Turn
from agilecode.adapters.store import decode
from agilecode.core.application import Application
from agilecode.core.types import AgileCodeError, Settings
from agilecode.core.tools import Tool, ToolManager
from agilecode.runtime import conversation_records as records
from conftest import AnalysisClient, ScriptClient


class CorrectableClient(AnalysisClient):
    def __init__(self):
        super().__init__()
        self.seen_feedback = []

    def action(self, messages):
        self.seen_feedback.extend(message["content"] for message in messages
                                  if message.get("role") == "user" and "USER FEEDBACK:" in (message.get("content") or ""))
        # A correction legitimately invalidates the earlier completion. This
        # deterministic client then assesses the corrected context and finishes.
        rejected = set()
        for index, message in enumerate(messages):
            if (message.get("role") == "tool" and
                    decode(message["content"], {}).get("error", {}).get("code") == "input_changed"):
                rejected.add(index)
                for prior in range(index - 1, -1, -1):
                    if any(call["id"] == message["tool_call_id"] for call in messages[prior].get("tool_calls", [])):
                        rejected.add(prior)
                        break
        messages = [message for index, message in enumerate(messages) if index not in rejected]
        return super().action(messages)


@pytest.mark.parametrize("input_first", [False, True])
async def test_final_step_feedback_has_durable_disposition(repository, tmp_path, monkeypatch, input_first):
    client = CorrectableClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        await app.config.save(Settings(autonomy="full", merge_on_completion=True))
        ticket = await app.create_ticket("Analyze", "Produce the requested findings.",
            team_ref="builtin:requirements-engineering", process_id="requirements-definition")
        original = records.complete_execution_exchange_once
        injected = []
        async def complete(store, identity, payload, **kwargs):
            row = await store.one("SELECT a.step_id,r.snapshot FROM tools t JOIN attempts a ON a.session_id=t.session_id "
                                  "JOIN runs r ON r.id=a.run_id WHERE t.id=? AND t.name='finish_step'", (identity,))
            final = False
            if row and payload.get("status") == "ok" and not injected:
                snapshot = decode(row["snapshot"])
                process = next(item for item in snapshot["team"]["processes"] if item["id"] == snapshot["process_id"])
                final = row["step_id"] == process["steps"][-1]["id"]
            if final and input_first:
                injected.append(await app.submit_feedback(ticket["id"], "Include the additional finding.", input_id="correction"))
            outcome = await original(store, identity, payload, **kwargs)
            if final and not input_first:
                injected.append(await app.submit_feedback(ticket["id"], "Include the additional finding.", input_id="correction"))
            return outcome
        monkeypatch.setattr(records, "complete_execution_exchange_once", complete)
        await app.move_ticket(ticket["id"], "Ready")
        run = await app.start_ticket(ticket["id"])
        finished = await asyncio.wait_for(app.wait_for_run(run["id"]), 15)
        assert finished["state"] == "completed"
        await app.prepare_integration(ticket["id"])
        assert (await app.find_ticket(ticket["id"]))["state"] == "Done"
        receipt = await app.store.get("operations", injected[0]["receipt_id"])
        outcome = decode(receipt["result"])
        activities = await app.store.query("SELECT * FROM activity WHERE ticket_id=? AND kind LIKE '%Feedback%'", (ticket["id"],))
        assert len(activities) == 1
        if input_first:
            assert client.seen_feedback
            assert receipt["status"] == "completed" and outcome["status"] == "finished"
            assert activities[0]["kind"] == "FeedbackDelivered"
        else:
            assert receipt["status"] == "needs_followup" and outcome["status"] == "needs_followup"
            assert outcome["run_id"] == run["id"]
            assert not decode(receipt["data"])["included"]
            assert activities[0]["kind"] == "FeedbackNeedsFollowup"
            assert decode(activities[0]["data"])["text"] == "Include the additional finding."
            visible = await app.store.messages(outcome["session_id"], include_partial=True)
            assert any(row["body"].get("content") == "USER FEEDBACK: Include the additional finding." for row in visible)
    finally:
        await app.close()


async def test_discussion_shutdown_owns_running_tool_and_blocks_new_admission(repository, tmp_path, monkeypatch):
    class ToolClient(ScriptClient):
        async def stream(self, profile, messages, tools, on_delta):
            return Turn({"role": "assistant", "content": None, "tool_calls": [{"id": "create", "type": "function", "function": {
                "name": "create_ticket", "arguments": json.dumps({"title": "Create after cancellation", "description": "Retain the actual outcome"})}}]}, "fake", None, "tool_calls")
    app = Application(repository, tmp_path / "global", client=ToolClient())
    await app.open(initialize=True)
    entered, release = threading.Event(), threading.Event()
    original = app.create_ticket
    async def delayed_create(*args, **kwargs):
        def work():
            entered.set()
            assert release.wait(5)
        await records.owned_finalization(asyncio.to_thread(work))
        return await original(*args, **kwargs)
    monkeypatch.setattr(app, "create_ticket", delayed_create)
    try:
        discussion = await app.discussion.create()
        ask = asyncio.create_task(app.discussion.ask(discussion["id"], "Create the ticket"))
        assert await asyncio.to_thread(entered.wait, 5)
        closing = asyncio.create_task(app.discussion.shutdown())
        await asyncio.sleep(0.01)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        with pytest.raises(AgileCodeError, match="closing"):
            await app.discussion.ask(discussion["id"], "Another question")
        release.set()
        await closing
        assert ask.done() and not app.discussion._turns
        tool = await app.store.one("SELECT * FROM tools WHERE name='create_ticket'")
        result = decode(tool["result"])
        assert result["error"]["code"] == "interrupted" and result["outcome_known"]
        assert not await app.store.query("SELECT * FROM tickets")
        assert len([row for row in await app.store.messages(discussion["session_id"]) if row["body"].get("role") == "tool"]) == 1
    finally:
        release.set()
        await app.close()


async def test_real_configuration_replacement_orders_with_tool_claim(repository, tmp_path):
    from agilecode.core.types import now
    class Mutation(Tool):
        name = "probe_mutation"
        uncertain_on_interrupt = True
        calls = 0
        async def execute(self, args, execution_id):
            self.calls += 1
            return {"changed": True}
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        tool = Mutation()
        await app.config.save(app.config.settings.model_copy(update={"tool_rules": {tool.name: "allow"}}))
        manager = ToolManager([tool], [tool.name], store=app.store, events=app.events,
            settings=lambda: app.config.settings, session_id="policy-test")
        reached, release = asyncio.Event(), asyncio.Event()
        async def check():
            reached.set()
            await release.wait()
        manager.check = check
        manager.dispatch_guard = lambda identity: records.claim_dispatch(app.store, "policy-test", identity, 0, 0, kind="tool")
        await app.store.insert("requests", {"id": "policy-request", "session_id": "policy-test", "model": "fake",
            "input": "{}", "status": "completed", "created": now()})
        call = {"id": "policy-call", "function": {"name": tool.name, "arguments": "{}"}}
        running = asyncio.create_task(manager.execute(call, "policy-request"))
        await reached.wait()
        await app.config.save(app.config.settings.model_copy(update={"tool_rules": {tool.name: "deny"}}))
        release.set()
        outcome = await running
        assert tool.calls == 0 and outcome.payload["error"]["code"] == "access_denied"
    finally:
        await app.close()


async def test_role_factories_bind_only_explicit_tools_and_current_settings(repository, tmp_path):
    from agilecode.adapters.workspace import Workspace
    from agilecode.runtime.tool_factories import discussion_tools, project_manager_tools, ticket_tools
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        await app.config.save(app.config.settings.model_copy(update={"autonomy": "restricted", "tool_rules": {"read_file": "deny"}}))
        discussion = discussion_tools(app, "discussion-id", "session-id")
        assert set(discussion.tools) == {"read_file", "list_files", "code_search", "load_skill", "create_ticket"}
        assert discussion.settings() is app.config.settings
        pm = project_manager_tools(app.project_manager)
        assert pm.settings().autonomy == "full" and pm.settings().tool_rules == {}
        assert not {"run_command", "write_file", "ask_user", "finish_step", "delete_ticket", "archive_ticket"} & pm.tools.keys()
        app.project_manager.settings.allow_archive = True
        assert "archive_ticket" in project_manager_tools(app.project_manager).tools

        async def noop(*args):
            return None
        member = SimpleNamespace(allowed_tools=["read_file"], skills=["*"])
        ctx = SimpleNamespace(team=SimpleNamespace(member=lambda identity: member), attempt={"session_id": "ticket-session", "id": "attempt"},
            ticket={"id": "ticket"}, run={"id": "run"})
        runtime = SimpleNamespace(workspace=Workspace(repository, repository, app.global_root), write_lock=asyncio.Lock(), shell=None,
            mcp=SimpleNamespace(tools={}, connections={}), control=SimpleNamespace(check=noop))
        processor = SimpleNamespace(store=app.store, git=app.git, settings=lambda: app.config.settings,
            skills=app.skills, create_ticket=app.create_ticket, events=app.events)
        manager = ticket_tools(processor, SimpleNamespace(team_member_id="member"), ctx, runtime, wait=noop, gate=noop, commit=noop)
        assert set(manager.tools) == {"read_file"}
        member.allowed_tools = ["ask_user", "finish_step"]
        manager = ticket_tools(processor, SimpleNamespace(team_member_id="member"), ctx, runtime, wait=noop, gate=noop, commit=noop)
        assert set(manager.tools) == {"ask_user", "finish_step"}
        assert manager.tools["finish_step"].gate is noop and manager.tools["ask_user"].wait is noop
    finally:
        await app.close()


def test_roles_have_one_tool_implementation_and_factory_owners():
    import ast
    import inspect
    import textwrap
    from agilecode.core import tools as compatibility
    from agilecode.runtime import tools as canonical
    from agilecode.core.runner import ProcessRunner
    from agilecode.core.project_manager import ProjectManager
    from agilecode.core.discussion import ProjectDiscussion
    assert compatibility is canonical
    assert canonical.ToolManager.__module__ == "agilecode.runtime.tools"
    assert compatibility._read_artifact is canonical._read_artifact
    for method, expected in [(ProcessRunner.execute_step, "ticket_tools"),
                             (ProjectManager._tool_manager, "project_manager_tools"),
                             (ProjectDiscussion._config, "discussion_tools")]:
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        assert any(isinstance(node.func, ast.Name) and node.func.id == expected for node in calls)
        assert not any(isinstance(node.func, ast.Attribute) and node.func.attr.endswith("Tool") for node in calls)
