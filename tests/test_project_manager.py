import asyncio
from pathlib import Path

import pytest

from agilecode.adapters.model import Turn
from agilecode.core.application import Application
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
        assert any(m["role"] == "user" for m in client.prompts[2])
        assert not any(m["role"] == "error" for m in client.prompts[2])
        assert sum(m["role"] == "user" for m in (
            row["body"] for row in await app.store.messages("project-manager")
        )) == 1
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
async def test_pm_matches_ticket_tool_continuation_and_next_instruction(repository, tmp_path):
    client = TicketStylePMClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    try:
        await app.project_manager.save_settings({"mode": "fully_automated"})
        # Drive two management turns without scheduling delays. The first
        # response calls a tool and finishes; the second must receive a newly
        # persisted user instruction, like the working ticket runner.
        app.project_manager.state["status"] = "running"
        first = await app.project_manager._run_turn(user_initiated=False)
        assert first["state"]["status"] == "running"
        assert len(client.sent) == 2
        assert client.sent[0][-1]["role"] == "user"
        assert client.sent[1][-1]["role"] == "tool"
        assert any(m["role"] == "user" for m in client.sent[1])

        second = await app.project_manager._run_turn(user_initiated=False)
        assert second["state"]["status"] == "running"
        assert len(client.sent) == 3
        assert client.sent[2][-1]["role"] == "user"
        assert "Continue managing the project" in client.sent[2][-1]["content"]
        history = [r["body"] for r in await app.store.messages("project-manager")]
        assert [m["role"] for m in history] == [
            "system", "user", "assistant", "tool", "assistant", "user", "assistant"
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
async def test_fully_automated_pm_can_mark_project_complete(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ProjectManagerClient())
    await app.open(initialize=True)
    try:
        await app.project_manager.save_settings({
            "mode": "fully_automated",
            "max_concurrent_tickets": 2,
            "wait_for_current_tickets": False,
            "answer_ticket_questions": True,
            "uncertainty": "continue",
            "allow_archive": False,
            "allow_delete": False,
        })
        await app.project_manager.resume()
        await app.project_manager._update_state({
            "goals": [],
            "plan": [],
            "current_activity": "All requested project work is complete.",
            "project_complete": True,
        }, "test")
        snapshot = await app.project_manager.snapshot()
        assert snapshot["state"]["status"] == "paused"
        assert snapshot["state"]["current_activity"] == "All requested project work is complete."
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
async def test_pm_profile_rejects_unknown_global_skill(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ProjectManagerClient())
    await app.open(initialize=True)
    try:
        with pytest.raises(Exception, match="Unknown global PM skills"):
            app.project_manager.save_profile({
                "name": "Invalid",
                "instructions": "Draft articles.",
                "skills": ["missing-skill"],
            })
        assert app.project_manager.list_profiles() == []
    finally:
        await app.close()
