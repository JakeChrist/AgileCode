import asyncio
from pathlib import Path

import pytest

from agilecode.adapters.model import Turn
from agilecode.core.application import Application
from agilecode.core.types import Event
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
        history = [r["body"] for r in await app.store.messages("project-manager")]
        assert [m["role"] for m in history] == [
            "system", "user", "assistant", "tool", "user"
        ]
        assert history[-2]["tool_call_id"] == "pending-1"
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
        app.project_manager.state["status"] = "running"
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
        app.project_manager.state["status"] = "running"
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

        client.release_first.set()

        async def wait_for_second():
            while len(client.prompts) < 2:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_second(), 3)
        assert len(client.prompts) == 2
        notice = client.prompts[1][-1]
        assert notice["role"] == "user"
        assert notice["content"].count("TicketChanged: ticket ticket-a") == 1
        assert "StepAttemptCompleted: ticket ticket-b" in notice["content"]
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

    root = repository / ".agile_code" / "project_manager"
    assert (root / "settings.json").is_file()
    assert (root / "state.json").is_file()
    exclude = Path((repository / ".git" / "info" / "exclude"))
    assert ".agile_code/" in exclude.read_text()

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
async def test_pm_state_update_does_not_stop_monitoring(repository, tmp_path):
    client = ProjectManagerClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        await app.project_manager.save_settings({"mode": "fully_automated"})
        await app.project_manager.resume()
        await asyncio.wait_for(client.pm_started.wait(), 2)
        async def await_idle():
            while app.project_manager.turn_task and not app.project_manager.turn_task.done():
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
