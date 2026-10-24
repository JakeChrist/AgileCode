import asyncio
import json
from pathlib import Path

import pytest

from agilecode.adapters.model import Turn
from agilecode.core.application import Application
from agilecode.core.types import Event, AgileCodeError
from conftest import ScriptClient


class ProjectManagerClient(ScriptClient):
    def __init__(self):
        super().__init__()
        self.pm_calls = 0
        self.pm_started = asyncio.Event()

    async def stream(self, profile, messages, tools, on_delta=None):
        system = messages[0].get("content", "") if messages else ""
        if "persistent Project Manager" in system:
            self.pm_calls += 1
            self.pm_started.set()
            return Turn(
                {"role": "assistant", "content": "Management turn complete."},
                "fixture-model",
                None,
                "stop",
            )
        return await super().stream(profile, messages, tools, on_delta)



@pytest.mark.asyncio
async def test_pm_archive_tool_can_restore_tickets_under_existing_permission(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ProjectManagerClient())
    await app.open(initialize=True)
    try:
        pm = app.project_manager
        ticket = await app.create_ticket("Retained work", "Preserve this ticket and its board state.")
        assert "archive_ticket" not in pm._tool_manager().tools

        await pm.save_settings({"allow_archive": True})
        tool = pm._tool_manager().tools["archive_ticket"]
        archive_args = tool.validate(json.dumps({"ticket": ticket["key"]}))
        assert archive_args["archived"] is True  # Existing model calls still archive.
        assert await tool.execute(archive_args, "archive-call") == {"archived": ticket["key"]}
        assert (await app.find_ticket(ticket["id"]))["archived"]
        assert not any(row["id"] == ticket["id"] for row in await app.board())

        unarchive_args = tool.validate(json.dumps({"ticket": ticket["key"], "archived": False}))
        assert await tool.execute(unarchive_args, "unarchive-call") == {"unarchived": ticket["key"]}
        restored = await app.find_ticket(ticket["id"])
        assert not restored["archived"]
        assert restored["state"] == ticket["state"]
        assert any(row["id"] == ticket["id"] for row in await app.board())

        await pm.save_settings({"allow_archive": False})
        assert "archive_ticket" not in pm._tool_manager().tools
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_pm_state_snapshot_excludes_conversation_history(repository, tmp_path):
    # PM state refreshes must not fetch and return the entire transcript.
    # Conversation history uses the exact same paginated API as ticket steps.
    app = Application(repository, tmp_path / "global", client=ProjectManagerClient())
    await app.open(initialize=True)
    try:
        for number in range(235):
            await app.store.message("project-manager", {
                "role": "user", "content": f"Recorded message {number}",
            })
        snapshot = await app.project_manager.snapshot()
        assert snapshot["session"] == "project-manager"
        assert "messages" not in snapshot

        recent = await app.conversation("project-manager")
        assert len(recent) == 200
        assert recent[-1]["body"]["content"] == "Recorded message 234"
        older = await app.conversation("project-manager", before=recent[0]["seq"])
        assert len(older) == 36  # Initial system message and 35 older user messages.
        assert older[0]["body"]["role"] == "system"
        assert older[-1]["seq"] + 1 == recent[0]["seq"]
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_restricted_pm_waits_for_user_but_can_complete_one_operation(repository, tmp_path):
    client = ProjectManagerClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        await app.project_manager.save_settings({
            "mode": "restricted",
            "max_concurrent_tickets": 4,
            "wait_for_current_tickets": False,
            "answer_ticket_questions": True,
            "uncertainty": "continue",
            "allow_archive": False,
            "allow_delete": False,
        })
        await app.project_manager.resume()
        await asyncio.sleep(0.05)
        assert client.pm_calls == 0

        result = await app.project_manager.message("Inspect the project and report back.")
        assert client.pm_calls == 1
        assert result["state"]["status"] == "running"
        messages = [
            row["body"] for row in await app.store.messages("project-manager")
            if row["body"].get("role") != "system"
        ]
        assert [message["role"] for message in messages] == ["user", "assistant"]
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_fully_automated_pm_self_starts_and_pause_does_not_pause_tickets(repository, tmp_path):
    client = ProjectManagerClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        await app.project_manager.save_settings({
            "mode": "fully_automated",
            "max_concurrent_tickets": 3,
            "wait_for_current_tickets": False,
            "answer_ticket_questions": True,
            "uncertainty": "continue",
            "allow_archive": False,
            "allow_delete": False,
        })
        await app.project_manager.resume()
        await asyncio.wait_for(client.pm_started.wait(), 2)
        assert client.pm_calls >= 1

        ticket = await app.create_ticket("Independent ticket", "Do not let PM lifecycle mutate this ticket.")
        before = dict(ticket)
        snapshot = await app.project_manager.pause()
        after = await app.find_ticket(ticket["id"])

        assert snapshot["state"]["status"] == "paused"
        assert after["state"] == before["state"]
        assert after["revision"] == before["revision"]
    finally:
        await app.close()



@pytest.mark.asyncio
async def test_pm_resume_after_pause_all_does_not_resume_tickets(repository, tmp_path):
    client = ProjectManagerClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        await app.project_manager.save_settings({"mode": "fully_automated"})
        await app.pause_all()
        assert app.all_paused
        assert app.project_manager.state["status"] == "paused"

        # PM Resume is independent of the globally paused ticket executions.
        resumed = await app.project_manager.resume()
        assert resumed["state"]["status"] == "running"
        assert app.all_paused
        await asyncio.wait_for(client.pm_started.wait(), 3)

        await app.project_manager.pause()
        assert app.all_paused
        result = await app.resume_all()
        assert not app.all_paused
        assert result["project_manager_status"] == "running"
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_pm_receives_technical_recovery_notices_as_distinct_from_ticket_failures(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ProjectManagerClient())
    await app.open(initialize=True)
    try:
        await app.project_manager.save_settings({"mode": "fully_automated"})
        prompt = app.project_manager._system_prompt()
        assert "Repeated technical errors alone never justify" in prompt
        await app.project_manager.resume()
        app.events.emit(Event("ExecutionTechnicalFailure", "ticket-technical", data={
            "issue": "model_stream", "message": "Temporary stream interruption",
        }))
        app.events.emit(Event("ExecutionTechnicalRecovered", "ticket-technical", data={
            "issue": "model_stream", "message": "Generation recovered",
        }))
        async with asyncio.timeout(3):
            while True:
                messages = [item["body"].get("content", "")
                            for item in await app.store.messages("project-manager")]
                if (any("TECHNICAL EXECUTION UPDATE (retrying automatically)" in text for text in messages)
                        and any("TECHNICAL EXECUTION UPDATE (recovered)" in text for text in messages)):
                    break
                await asyncio.sleep(0.01)
        assert any("not an engineering failure" in text for text in messages)
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_pausing_busy_pm_does_not_pause_executing_ticket_or_lose_context(repository, tmp_path):
    from test_git_recovery import setup_run

    class BlockingClient(ProjectManagerClient):
        def __init__(self):
            super().__init__()
            self.ticket_started = asyncio.Event()

        async def stream(self, profile, messages, tools, on_delta=None):
            system = messages[0].get("content", "") if messages else ""
            if "persistent Project Manager" in system:
                self.pm_calls += 1
                self.pm_started.set()
            else:
                self.ticket_started.set()
            await asyncio.Event().wait()

    client = BlockingClient()
    app, ticket, run = await setup_run(repository, tmp_path / "global", client)
    try:
        await asyncio.wait_for(client.ticket_started.wait(), 5)
        await app.project_manager.save_settings({"mode": "fully_automated"})
        await app.project_manager.resume()
        await asyncio.wait_for(client.pm_started.wait(), 5)
        saved_messages = {row["id"] for row in await app.store.messages("project-manager")}

        paused = await asyncio.wait_for(app.project_manager.pause(), 5)
        assert paused["state"]["status"] == "paused"
        assert not app.all_paused
        assert (await app.store.get("runs", run["id"]))["state"] == "running"
        assert not app.tasks[run["id"]].done()
        assert saved_messages <= {row["id"] for row in await app.store.messages("project-manager")}

        resumed = await app.project_manager.resume()
        assert resumed["state"]["status"] == "running"
        async with asyncio.timeout(5):
            while client.pm_calls < 2:
                await asyncio.sleep(0.01)
        assert not app.all_paused
        assert (await app.store.get("runs", run["id"]))["state"] == "running"
        assert not app.tasks[run["id"]].done()
    finally:
        await app.close()


class FailingAutonomousClient(ProjectManagerClient):
    def __init__(self):
        super().__init__()
        self.prompts = []
        self.resumed = asyncio.Event()

    async def stream(self, profile, messages, tools, on_delta=None):
        if messages and "persistent Project Manager" in (messages[0].get("content") or ""):
            self.prompts.append(messages)
            if len(self.prompts) == 1:
                return Turn(
                    {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "pm-call-1", "type": "function",
                         "function": {"name": "list_tickets", "arguments": "{}"}}
                    ]},
                    "fixture-model", None, "tool_calls",
                )
            if len(self.prompts) == 2:
                raise RuntimeError("Simulated Project Manager model failure")
            self.resumed.set()
            return Turn(
                {"role": "assistant", "content": "Management resumed."},
                "fixture-model", None, "stop",
            )
        return await super().stream(profile, messages, tools, on_delta)


@pytest.mark.asyncio
async def test_autonomous_tool_continuation_failure_and_resume(repository, tmp_path):
    client = FailingAutonomousClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        await app.project_manager.save_settings({"mode": "fully_automated"})
        await app.project_manager.resume()

        async def wait_for_failure():
            while app.project_manager.state["status"] != "paused":
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_failure(), 3)
        assert len(client.prompts) == 2
        assert all(any(m["role"] == "user" for m in prompt) for prompt in client.prompts)
        assert any(m["role"] == "tool" for m in client.prompts[1])

        history = [row["body"] for row in await app.store.messages("project-manager")]
        assert [m["role"] for m in history] == ["system", "user", "assistant", "tool", "error"]
        assert "Simulated Project Manager model failure" in history[-1]["content"]
        requests = await app.store.query(
            "SELECT status FROM requests WHERE session_id='project-manager' ORDER BY created"
        )
        assert [row["status"] for row in requests] == ["completed", "interrupted"]

        await app.project_manager.resume()
        await asyncio.wait_for(client.resumed.wait(), 3)
        assert client.prompts[2][-1]["role"] == "user"
        assert "Project Manager resumed" in client.prompts[2][-1]["content"]
        assert not any(m["role"] == "error" for m in client.prompts[2])
        assert sum(m["role"] == "user" for m in (
            row["body"] for row in await app.store.messages("project-manager")
        )) == 2
    finally:
        await app.close()


class TicketStylePMClient(ProjectManagerClient):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def stream(self, profile, messages, tools, on_delta=None):
        if "persistent Project Manager" in (messages[0].get("content") or ""):
            self.sent.append(messages)
            if len(self.sent) == 1:
                return Turn({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "pm-list-1",
                        "type": "function",
                        "function": {"name": "list_tickets", "arguments": "{}"},
                    }],
                }, "fixture-model", None, "tool_calls")
            return Turn({"role": "assistant", "content": "Inspected project."},
                        "fixture-model", None, "stop")
        return await super().stream(profile, messages, tools, on_delta)


@pytest.mark.asyncio
async def test_pm_continues_tool_calls_but_only_restarts_on_project_events(repository, tmp_path):
    client = TicketStylePMClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        await app.project_manager.save_settings({"mode": "fully_automated"})
        await app.project_manager.resume()

        async def await_turns(count):
            while len(client.sent) < count:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(await_turns(2), 3)
        assert [m["role"] for m in client.sent[0]] == ["system", "user"]
        assert client.sent[1][-1]["role"] == "tool"
        # Ending a completed response does not prompt another model request.
        await asyncio.sleep(0.1)
        assert len(client.sent) == 2
        original = [r["body"] for r in await app.store.messages("project-manager")]
        assert [m["role"] for m in original] == [
            "system", "user", "assistant", "tool", "assistant",
        ]

        # Only an actual external ticket update starts a new response.
        app.events.emit(Event("TicketChanged", "ticket-updated"))
        await asyncio.wait_for(await_turns(3), 3)
        assert client.sent[2][-1]["role"] == "user"
        assert "TicketChanged: ticket ticket-updated" in client.sent[2][-1]["content"]

        async def await_saved_response():
            while len(await app.store.messages("project-manager")) < 7:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(await_saved_response(), 3)
        history = [r["body"] for r in await app.store.messages("project-manager")]
        assert [m["role"] for m in history] == [
            "system", "user", "assistant", "tool", "assistant", "user", "assistant",
        ]
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_pm_finishes_tool_results_before_user_correction(repository, tmp_path):
    from agilecode.core.types import now, uid
    from agilecode.adapters.store import encode

    app = Application(repository, tmp_path / "global", client=ProjectManagerClient())
    await app.open(initialize=True)
    try:
        await app.store.message("project-manager", {"role": "user", "content": "Inspect the project"})
        request_id = uid()
        await app.store.insert("requests", {
            "id": request_id, "session_id": "project-manager", "model": "fixture-model",
            "input": encode({"messages": []}), "status": "completed", "created": now(),
        })
        await app.store.message("project-manager", {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "pending-1", "type": "function",
                "function": {"name": "list_tickets", "arguments": "{}"},
            }],
        }, request_id)
        await app.project_manager.message("Change direction.")
        # Paused input is accepted and visible immediately, but accepting it
        # cannot execute or repair an old pending tool exchange as a side effect.
        history = [r["body"] for r in await app.store.messages("project-manager", include_partial=True)]
        assert [m["role"] for m in history] == ["system", "user", "assistant", "user"]
        assert not await app.store.query("SELECT * FROM tools")
        assert history[-1]["content"] == "Change direction."
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_pm_rejects_missing_user_after_context_compaction(repository, tmp_path, monkeypatch):
    # LM Studio must never receive the malformed history that previously
    # triggered Qwen's "No user query found in messages" Jinja error.
    client = ProjectManagerClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        async def invalid_compaction(self, messages, context_window, summarize):
            return [messages[0]], {}

        monkeypatch.setattr(
            "agilecode.core.project_manager.ContextManager.prepare",
            invalid_compaction,
        )
        await app.project_manager._change_state({"status": "running"}, control=True)
        await app.project_manager._run_turn(user_initiated=False)

        assert client.pm_calls == 0
        assert app.project_manager.state["status"] == "paused"
        history = [r["body"] for r in await app.store.messages("project-manager")]
        assert history[-1]["role"] == "error"
        assert "no user instruction" in history[-1]["content"]
        assert not await app.store.query(
            "SELECT id FROM requests WHERE session_id='project-manager'"
        )
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_pm_legacy_orphan_history_does_not_execute_tools_or_reach_model(repository, tmp_path):
    class CapturingClient(ProjectManagerClient):
        def __init__(self):
            super().__init__()
            self.sent = []

        async def stream(self, profile, messages, tools, on_delta=None):
            if "persistent Project Manager" in (messages[0].get("content") or ""):
                self.sent.append(messages)
            return await super().stream(profile, messages, tools, on_delta)

    client = CapturingClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        # Older PM code could save a tool call without the user task that
        # caused it. Preserve that historical record, but do not replay it.
        await app.store.message("project-manager", {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "orphaned-old-tool",
                "type": "function",
                "function": {"name": "list_tickets", "arguments": "{}"},
            }],
        })
        await app.project_manager._change_state({"status": "running"}, control=True)
        await app.project_manager._run_turn(user_initiated=False)

        assert len(client.sent) == 1
        assert [m["role"] for m in client.sent[0]] == ["system", "user"]
        assert not await app.store.query(
            "SELECT id FROM tools WHERE session_id='project-manager'"
        )
        history = [r["body"] for r in await app.store.messages("project-manager")]
        assert history[1]["tool_calls"][0]["id"] == "orphaned-old-tool"
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_pm_only_wakes_for_real_ticket_updates(repository, tmp_path):
    client = ProjectManagerClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        await app.project_manager.save_settings({"mode": "fully_automated"})
        await app.project_manager.resume()
        await asyncio.wait_for(client.pm_started.wait(), 2)
        await asyncio.sleep(0.1)
        assert client.pm_calls == 1

        # UI and token events are not management triggers.
        app.events.emit(Event("ProjectManagerChanged", session_id="project-manager"))
        app.events.emit(Event("ModelReasoningDelta", "ticket-1"))
        await asyncio.sleep(0.05)
        assert client.pm_calls == 1

        # Ticket updates, including completion, do wake the PM.
        app.events.emit(Event("ExecutionCompleted", "ticket-1"))
        async def await_second():
            while client.pm_calls < 2:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(await_second(), 2)
        await asyncio.sleep(0.05)
        assert client.pm_calls == 2

        await app.project_manager.pause()
        app.events.emit(Event("TicketChanged", "ticket-2"))
        await asyncio.sleep(0.05)
        assert client.pm_calls == 2
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_pm_batches_ticket_updates_while_current_response_is_active(repository, tmp_path):
    class SlowPMClient(ProjectManagerClient):
        def __init__(self):
            super().__init__()
            self.release_first = asyncio.Event()
            self.prompts = []

        async def stream(self, profile, messages, tools, on_delta=None):
            if messages and "persistent Project Manager" in (messages[0].get("content") or ""):
                self.prompts.append(messages)
                if len(self.prompts) == 1:
                    self.pm_started.set()
                    await self.release_first.wait()
                return Turn({"role": "assistant", "content": "Noted."},
                            "fixture-model", None, "stop")
            return await super().stream(profile, messages, tools, on_delta)

    client = SlowPMClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        await app.project_manager.save_settings({"mode": "fully_automated"})
        await app.project_manager.resume()
        await asyncio.wait_for(client.pm_started.wait(), 2)

        app.events.emit(Event("TicketChanged", "ticket-a"))
        app.events.emit(Event("TicketChanged", "ticket-a"))
        app.events.emit(Event("StepAttemptCompleted", "ticket-b"))
        assert len(client.prompts) == 1

        # Each event becomes a durable conversation message. Wait until all
        # are accepted before releasing the held provider response.
        async with asyncio.timeout(3):
            while True:
                visible = await app.store.messages("project-manager", include_partial=True)
                updates = [r["body"]["content"] for r in visible
                           if (r["body"].get("content") or "").startswith(
                               "PROJECT STATUS UPDATE (informational):")
                           and "ticket-" in r["body"]["content"]]
                if len(updates) == 3:
                    break
                await asyncio.sleep(0.01)
        client.release_first.set()

        async def wait_for_second():
            while len(client.prompts) < 2:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_second(), 3)
        assert len(client.prompts) == 2
        notices = [m["content"] for m in client.prompts[1]
                   if m["role"] == "user" and "ticket-" in m["content"]]
        assert sum("TicketChanged: ticket ticket-a" in msg for msg in notices) == 2
        assert sum("StepAttemptCompleted: ticket ticket-b" in msg for msg in notices) == 1
        await asyncio.sleep(0.1)
        assert len(client.prompts) == 2
    finally:
        client.release_first.set()
        await app.close()


@pytest.mark.asyncio
async def test_project_manager_state_persists_and_restart_is_paused(repository, tmp_path):
    global_root = tmp_path / "global"
    client = ProjectManagerClient()
    app = Application(repository, global_root, client=client)
    await app.open(initialize=True)
    try:
        await app.project_manager.save_settings({
            "mode": "fully_automated",
            "max_concurrent_tickets": 2,
            "wait_for_current_tickets": True,
            "answer_ticket_questions": False,
            "uncertainty": "ask",
            "allow_archive": True,
            "allow_delete": False,
        })
        app.project_manager.state["goals"] = ["Ship the requested product"]
        app.project_manager.state["plan"] = ["Build the next independent ticket batch"]
        app.project_manager.state["current_activity"] = "Planning"
        await app.project_manager._persist_state()
        await app.project_manager.resume()
        await asyncio.wait_for(client.pm_started.wait(), 2)
    finally:
        await app.close()

    root = repository / ".agilecode" / "project_manager"
    assert (root / "settings.json").is_file()
    assert (root / "state.json").is_file()
    assert not (repository / ".agile_code").exists()

    reopened = Application(repository, global_root, client=ProjectManagerClient())
    await reopened.open()
    try:
        snapshot = await reopened.project_manager.snapshot()
        assert snapshot["state"]["status"] == "paused"
        assert snapshot["state"]["goals"] == ["Ship the requested product"]
        assert snapshot["state"]["plan"] == ["Build the next independent ticket batch"]
        assert snapshot["settings"]["mode"] == "fully_automated"
        assert snapshot["settings"]["max_concurrent_tickets"] == 2
        assert snapshot["settings"]["wait_for_current_tickets"] is True
    finally:
        await reopened.close()



@pytest.mark.asyncio
async def test_pm_migrates_legacy_storage_preserving_settings_and_state(repository, tmp_path):
    global_root = tmp_path / "global"
    first = Application(repository, global_root, client=ProjectManagerClient())
    await first.open(initialize=True)
    try:
        await first.project_manager.save_settings({
            "project_instructions": "Preserve configuration",
            "max_concurrent_tickets": 3,
        })
        first.project_manager.state["goals"] = ["Preserve goals"]
        await first.project_manager._persist_state()
    finally:
        await first.close()

    current = repository / ".agilecode" / "project_manager"
    legacy = repository / ".agile_code" / "project_manager"
    legacy.parent.mkdir(parents=True)
    current.rename(legacy)
    (legacy / "custom-data.txt").write_text("Must not be discarded")

    reopened = Application(repository, global_root, client=ProjectManagerClient())
    await reopened.open()
    try:
        assert reopened.project_manager.root == current
        assert (current / "custom-data.txt").read_text() == "Must not be discarded"
        assert not legacy.parent.exists()
        snapshot = await reopened.project_manager.snapshot()
        assert snapshot["settings"]["project_instructions"] == "Preserve configuration"
        assert snapshot["settings"]["max_concurrent_tickets"] == 3
        assert snapshot["state"]["goals"] == ["Preserve goals"]
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_pm_migration_keeps_other_legacy_files_git_ignored(repository, tmp_path):
    legacy = repository / ".agile_code" / "project_manager"
    legacy.mkdir(parents=True)
    (legacy / "settings.json").write_text(json.dumps({"project_instructions": "Existing project"}))
    (legacy.parent / "notes.txt").write_text("Keep this file")

    app = Application(repository, tmp_path / "global", client=ProjectManagerClient())
    await app.open(initialize=True)
    try:
        assert app.project_manager.settings.project_instructions == "Existing project"
        assert (repository / ".agilecode" / "project_manager" / "settings.json").exists()
        assert not legacy.exists()
        assert (legacy.parent / "notes.txt").read_text() == "Keep this file"
        exclude = repository / ".git" / "info" / "exclude"
        assert ".agile_code/" in exclude.read_text()
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_pm_migration_refuses_existing_destination_without_overwriting(repository, tmp_path):
    legacy = repository / ".agile_code" / "project_manager"
    current = repository / ".agilecode" / "project_manager"
    legacy.mkdir(parents=True)
    current.mkdir(parents=True)
    (legacy / "state.json").write_text('{"source": "old"}')
    (current / "state.json").write_text('{"source": "new"}')

    app = Application(repository, tmp_path / "global", client=ProjectManagerClient())
    with pytest.raises(AgileCodeError, match="exists in both"):
        await app.open(initialize=True)
    assert (legacy / "state.json").read_text() == '{"source": "old"}'
    assert (current / "state.json").read_text() == '{"source": "new"}'


@pytest.mark.asyncio
async def test_pm_migration_refuses_symlinked_legacy_directory(repository, tmp_path):
    external = tmp_path / "external"
    (external / "project_manager").mkdir(parents=True)
    (external / "project_manager" / "state.json").write_text("external state")
    (repository / ".agile_code").symlink_to(external, target_is_directory=True)

    app = Application(repository, tmp_path / "global", client=ProjectManagerClient())
    with pytest.raises(AgileCodeError, match="symlink"):
        await app.open(initialize=True)
    assert (external / "project_manager" / "state.json").read_text() == "external state"


@pytest.mark.asyncio
async def test_pm_state_update_does_not_stop_monitoring(repository, tmp_path):
    client = ProjectManagerClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        await app.project_manager.save_settings({"mode": "fully_automated"})
        await app.project_manager.resume()
        await asyncio.wait_for(client.pm_started.wait(), 2)
        async def await_idle():
            while app.project_manager.engine.is_active("project-manager"):
                await asyncio.sleep(0.01)
        await asyncio.wait_for(await_idle(), 2)
        await app.project_manager._update_state({
            "goals": [],
            "plan": [],
            "current_activity": "No immediate work identified.",
        }, "test")
        snapshot = await app.project_manager.snapshot()
        assert snapshot["state"]["status"] == "running"
        assert snapshot["state"]["current_activity"] == "No immediate work identified."
    finally:
        await app.close()


def test_project_manager_ui_has_separate_runtime_and_settings_tabs():
    qml = (
        Path(__file__).parents[1]
        / "src"
        / "agilecode"
        / "desktop"
        / "qml"
        / "Main.qml"
    ).read_text()
    assert 'text: "Project Manager"' in qml
    assert 'TabButton { text: "Runtime" }' in qml
    assert 'TabButton { text: "Settings" }' in qml
    assert 'objectName: "projectManagerConversation"' in qml


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_id", [None, "lead-systems-engineer"])
async def test_builtin_pm_prompt_guides_requirements_first_with_existing_profiles(
    repository, tmp_path, profile_id
):
    app = Application(repository, tmp_path / "global", client=ProjectManagerClient())
    await app.open(initialize=True)
    try:
        if profile_id:
            # Existing customized profiles need not be refreshed to receive the
            # built-in starting rule, and their instructions remain intact.
            profile = app.project_manager.profiles.get(profile_id)
            customized = profile.model_dump(mode="json")
            customized["instructions"] = "Keep this existing custom role guidance."
            app.project_manager.save_profile(customized)

        await app.project_manager.save_settings({
            "profile_id": profile_id,
            "project_instructions": "Prioritize the current project objective.",
        })
        prompt = app.project_manager._system_prompt()
        builtin = prompt.split("\n\n", 1)[0]
        for instruction in (
            "When beginning a new project or feature, first establish what the system should accomplish.",
            "Examine the user's request and any existing requirements.",
            "If intended behavior, scope, constraints, or acceptance criteria are missing or unclear, "
            "normally make requirements definition the first delegated task.",
            "Requirements Engineering team (team_ref='builtin:requirements-engineering')",
            "Define Requirements process (process_id='requirements-definition')",
            "Do not assume requirements are settled merely because the implementation seems obvious.",
            "Reuse adequate existing requirements and address only the gaps.",
            "Create a bounded requirements ticket stating its purpose, known constraints, questions "
            "to resolve, a committed requirements document deliverable, and completion criteria.",
            "Have the team develop the requirements and flag unresolved decisions.",
            "Read their deliverable before deciding what next work is justified; "
            "do not populate a full speculative implementation backlog.",
            "There is no mandatory engineering-phase sequence or readiness gate",
            "a well-understood change may proceed directly to implementation, "
            "and several activities may proceed in parallel.",
            "RESTRICTED MODE: Respond to user instructions only.",
        ):
            assert instruction in builtin
        assert "Prioritize the current project objective." in prompt
        if profile_id:
            assert "Keep this existing custom role guidance." in prompt
            assert app.project_manager.profiles.get(profile_id).instructions == customized["instructions"]

        # Inspect the actual persisted system message, without scripting a
        # model response that pretends to choose a requirements ticket.
        messages = await app.store.messages("project-manager")
        system = next(row["body"] for row in messages if row["body"].get("role") == "system")
        assert system["content"] == prompt
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_reusable_pm_profiles_and_project_overrides(repository, tmp_path):
    global_root = tmp_path / "shared-global"
    app = Application(repository, global_root, client=ProjectManagerClient())
    await app.open(initialize=True)
    try:
        global_skills = [s for s in app.skills.catalog(["*"])
                         if s["scope"] == "global" and "error" not in s]
        assert global_skills
        skill = global_skills[0]["id"]
        created = app.project_manager.save_profile({
            "name": "Research Director",
            "instructions": "Coordinate independent research directions.",
            "skills": [skill],
        })
        assert created["id"] in [p["id"] for p in app.project_manager.list_profiles()]
        assert (global_root / "project_manager" / "profiles" / (created["id"] + ".json")).exists()

        settings = app.project_manager.settings.model_dump(mode="json")
        settings.update(profile_id=created["id"],
                        project_instructions="Prioritize this project's mathematical models.",
                        project_skills=[skill])
        await app.project_manager.save_settings(settings)
        prompt = app.project_manager._system_prompt()
        assert "Coordinate independent research directions." in prompt
        assert "Prioritize this project's mathematical models." in prompt
        assert skill in prompt
        assert len(app.project_manager._effective_skills()) == 1
        assert "load_skill" in app.project_manager._tool_manager().tools
        assert app.project_manager.settings.mode == "restricted"
        assert not app.project_manager.settings.allow_delete

        edited = app.project_manager.save_profile({
            "id": created["id"],
            "name": "Research Director",
            "instructions": "Explore multiple competing hypotheses.",
            "skills": [skill],
        })
        assert edited["id"] == created["id"]
        assert "Explore multiple competing hypotheses." in app.project_manager._system_prompt()
        assert "Coordinate independent research directions." not in app.project_manager._system_prompt()
    finally:
        await app.close()

    reopened = Application(repository, global_root, client=ProjectManagerClient())
    await reopened.open()
    try:
        assert reopened.project_manager.settings.profile_id == created["id"]
        assert reopened.project_manager.profiles.get(created["id"]).skills == [skill]
        deleted = await reopened.project_manager.delete_profile(created["id"])
        assert deleted["deleted"] == created["id"]
        assert reopened.project_manager.settings.profile_id is None
        assert reopened.project_manager.settings.mode == "restricted"
        assert "Explore multiple competing hypotheses." not in reopened.project_manager._system_prompt()
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_bundled_lead_systems_engineer_profile(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ProjectManagerClient())
    await app.open(initialize=True)
    try:
        profile = app.project_manager.profiles.get("lead-systems-engineer")
        assert profile.name == "Lead Systems Engineer"
        assert profile.skills == [
            "systems-engineering-lifecycle",
            "technical-planning-delegation",
            "system-integration-oversight",
            "system-level-assessment",
            "systems-engineering-technical-reviews",
        ]
        assert app.project_manager.settings.profile_id is None
        for name in profile.skills:
            skill = app.skills.load(name, profile.skills)
            assert skill["scope"] == "global"
            assert skill["instructions"]

        settings = app.project_manager.settings.model_dump(mode="json")
        settings["profile_id"] = profile.id
        await app.project_manager.save_settings(settings)
        prompt = app.project_manager._system_prompt()
        assert "Lead Systems Engineer" in prompt
        assert "Requirements Engineering team" in prompt
        assert all(name in prompt for name in profile.skills)

        # Restarting should not replace an existing customized bundled profile.
        data = profile.model_dump(mode="json")
        data["instructions"] = "Customized systems engineering oversight."
        app.project_manager.save_profile(data)
        app.project_manager.profiles.install_bundled()
        assert app.project_manager.profiles.get(profile.id).instructions == data["instructions"]
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_lead_engineer_review_skill_upgrades_existing_default_profile_once(repository, tmp_path):
    global_root = tmp_path / "global"
    root = global_root / "project_manager" / "profiles"
    root.mkdir(parents=True)
    target = root / "lead-systems-engineer.json"
    packaged = (
        Path(__file__).parents[1] / "src" / "agilecode" / "resources"
        / "pm_profiles" / "lead-systems-engineer.json"
    )
    old = json.loads(packaged.read_text(encoding="utf-8"))
    old["skills"].remove("systems-engineering-technical-reviews")
    old["instructions"] = "User-customized engineering guidance."
    target.write_text(json.dumps(old), encoding="utf-8")

    app = Application(repository, global_root, client=ProjectManagerClient())
    await app.open(initialize=True)
    try:
        profile = app.project_manager.profiles.get("lead-systems-engineer")
        assert profile.instructions == old["instructions"]
        assert profile.skills == [*old["skills"], "systems-engineering-technical-reviews"]
        assert (root / ".lead-systems-engineer-reviews-v1").exists()

        # Removing the newly added skill later is a user choice, not something
        # a future startup should silently reverse.
        saved = profile.model_dump(mode="json")
        saved["skills"] = old["skills"]
        app.project_manager.save_profile(saved)
    finally:
        await app.close()

    reopened = Application(repository, global_root, client=ProjectManagerClient())
    await reopened.open()
    try:
        profile = reopened.project_manager.profiles.get("lead-systems-engineer")
        assert profile.instructions == old["instructions"]
        assert profile.skills == old["skills"]
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_lead_engineer_review_skill_respects_existing_custom_skill_set(repository, tmp_path):
    global_root = tmp_path / "global"
    root = global_root / "project_manager" / "profiles"
    root.mkdir(parents=True)
    target = root / "lead-systems-engineer.json"
    packaged = (
        Path(__file__).parents[1] / "src" / "agilecode" / "resources"
        / "pm_profiles" / "lead-systems-engineer.json"
    )
    customized = json.loads(packaged.read_text(encoding="utf-8"))
    customized["skills"] = ["systems-engineering-lifecycle", "system-level-assessment"]
    target.write_text(json.dumps(customized), encoding="utf-8")

    app = Application(repository, global_root, client=ProjectManagerClient())
    await app.open(initialize=True)
    try:
        assert app.project_manager.profiles.get("lead-systems-engineer").skills == customized["skills"]
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_pm_profile_rejects_unknown_global_skill(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ProjectManagerClient())
    await app.open(initialize=True)
    try:
        initial_profiles = app.project_manager.list_profiles()
        with pytest.raises(Exception, match="Unknown global PM skills"):
            app.project_manager.save_profile({
                "name": "Invalid",
                "instructions": "Draft articles.",
                "skills": ["missing-skill"],
            })
        assert app.project_manager.list_profiles() == initial_profiles
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_direct_pm_message_interrupts_autonomous_generation_without_project_event(repository, tmp_path):
    class HeldPMClient(ProjectManagerClient):
        def __init__(self):
            super().__init__()
            self.prompts = []
            self.first_cancelled = asyncio.Event()

        async def stream(self, profile, messages, tools, on_delta=None):
            if "persistent Project Manager" in (messages[0].get("content") or ""):
                self.prompts.append(messages)
                if len(self.prompts) == 1:
                    self.pm_started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        self.first_cancelled.set()
                        raise
                return Turn({"role": "assistant", "content": "User instruction handled."},
                            "fixture-model", None, "stop")
            return await super().stream(profile, messages, tools, on_delta)

    client = HeldPMClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        await app.project_manager.save_settings({"mode": "fully_automated"})
        await app.project_manager.resume()
        await asyncio.wait_for(client.pm_started.wait(), 3)
        result = await asyncio.wait_for(
            app.project_manager.message("Immediately inspect this problem", "direct"), 5)
        assert client.first_cancelled.is_set()
        assert result["state"]["status"] == "running"
        assert len(client.prompts) == 2
        assert any(message.get("content") == "Immediately inspect this problem"
                   for message in client.prompts[-1])
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_ticket_event_does_not_interrupt_active_pm_user_conversation(repository, tmp_path):
    class HeldUserClient(ProjectManagerClient):
        def __init__(self):
            super().__init__()
            self.prompts = []
            self.user_started = asyncio.Event()
            self.release_user = asyncio.Event()
            self.user_cancelled = False

        async def stream(self, profile, messages, tools, on_delta=None):
            if "persistent Project Manager" in (messages[0].get("content") or ""):
                self.prompts.append(messages)
                if len(self.prompts) == 1:
                    self.pm_started.set()
                if len(self.prompts) == 2:
                    self.user_started.set()
                    try:
                        await self.release_user.wait()
                    except asyncio.CancelledError:
                        self.user_cancelled = True
                        raise
                return Turn({"role": "assistant", "content": "Update considered."},
                            "fixture-model", None, "stop")
            return await super().stream(profile, messages, tools, on_delta)

    client = HeldUserClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    sending = None
    try:
        await app.project_manager.save_settings({"mode": "fully_automated"})
        await app.project_manager.resume()
        await asyncio.wait_for(client.pm_started.wait(), 3)
        async with asyncio.timeout(3):
            while app.project_manager.engine.is_active("project-manager"):
                await asyncio.sleep(0.01)
        sending = asyncio.create_task(app.project_manager.message("Examine architecture"))
        await asyncio.wait_for(client.user_started.wait(), 3)
        app.events.emit(Event("ExecutionCompleted", "ticket-completed"))
        async with asyncio.timeout(3):
            while not any("ExecutionCompleted: ticket ticket-completed"
                          in (r["body"].get("content") or "")
                          for r in await app.store.messages("project-manager", include_partial=True)):
                await asyncio.sleep(0.01)
        assert not client.user_cancelled
        assert len(client.prompts) == 2
        client.release_user.set()
        await asyncio.wait_for(sending, 5)
        assert not client.user_cancelled
        assert len(client.prompts) == 3
        assert any("ExecutionCompleted: ticket ticket-completed" in (m.get("content") or "")
                   for m in client.prompts[-1])
    finally:
        client.release_user.set()
        await app.close()
        if sending is not None:
            await asyncio.gather(sending, return_exceptions=True)


@pytest.mark.asyncio
async def test_pm_user_reply_to_question_does_not_require_resume(repository, tmp_path):
    class QuestionClient(ProjectManagerClient):
        def __init__(self):
            super().__init__()
            self.prompts = []

        async def stream(self, profile, messages, tools, on_delta=None):
            if "persistent Project Manager" in (messages[0].get("content") or ""):
                self.prompts.append(messages)
                return Turn({"role": "assistant", "content":
                    "NEEDS_USER: Choose direction" if len(self.prompts) == 1
                    else "Following the requested direction."},
                    "fixture-model", None, "stop")
            return await super().stream(profile, messages, tools, on_delta)

    client = QuestionClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        await app.project_manager.save_settings({"mode": "restricted", "uncertainty": "ask"})
        await app.project_manager.resume()
        await app.project_manager.message("What should we build first?")
        assert app.project_manager.state["status"] == "running"
        assert app.project_manager.state["current_activity"] == "Waiting for user direction"

        await asyncio.wait_for(app.project_manager.message("Build the requirements first"), 3)
        assert len(client.prompts) == 2
        assert any(m.get("content") == "Build the requirements first" for m in client.prompts[-1])
        assert app.project_manager.state["status"] == "running"
    finally:
        await app.close()
