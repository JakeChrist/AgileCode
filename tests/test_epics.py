"""Lightweight epic metadata, naming, persistence, and PM integration."""
import sqlite3

import pytest

from agilecode.adapters.store import ProjectStore
from agilecode.core.application import Application
from agilecode.core.types import AgileCodeError, Conflict
from conftest import ScriptClient


@pytest.mark.asyncio
async def test_epic_ticket_naming_association_and_metadata(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        epic = await app.create_epic(
            "AgentGUIInt", "Agent GUI Interaction",
            "Isolate graphical application sessions.",
            "Capture images and limit input to the application.",
        )
        assert epic["epic"]["status"] == "Active"
        assert epic["ticket_count"] == 0

        standalone = await app.create_ticket("Standalone task", "No epic required")
        first = await app.create_ticket("Session isolation", "Isolate application", epic_id="AgentGUIInt")
        second = await app.create_ticket("Screenshot capture", "Capture pixels", epic_id="AgentGUIInt")
        assert standalone["key"].startswith("T-")
        assert standalone["epic_id"] is None
        assert (first["key"], second["key"]) == ("AgentGUIInt-01", "AgentGUIInt-02")
        assert all(t["epic_id"] == "AgentGUIInt" for t in await app.board() if t["id"] in (first["id"], second["id"]))

        info = await app.get_epic("AgentGUIInt")
        assert [t["key"] for t in info["tickets"]] == ["AgentGUIInt-01", "AgentGUIInt-02"]
        assert info["completed_count"] == 0

        updated = await app.update_epic(
            "AgentGUIInt", expected_revision=info["epic"]["revision"],
            completion_criteria="Only the target window is controlled.",
        )
        assert updated["epic"]["completion_criteria"] == "Only the target window is controlled."
        with pytest.raises(Conflict):
            await app.update_epic("AgentGUIInt", expected_revision=info["epic"]["revision"], title="Stale edit")

        # Progress does not automatically change the epic's organizational status.
        await app.store.update("tickets", first["id"], state="Done")
        listed = await app.list_epics()
        assert listed[0]["ticket_count"] == 2
        assert listed[0]["completed_count"] == 1
        assert listed[0]["status"] == "Active"

        with pytest.raises(AgileCodeError, match="Unknown epic"):
            await app.create_ticket("Invalid", "", epic_id="DoesNotExist")
        with pytest.raises(AgileCodeError, match="already in use"):
            await app.create_epic("AgentGUIInt", "Duplicate")
    finally:
        await app.close()

    reopened = Application(repository, tmp_path / "global", client=ScriptClient())
    await reopened.open()
    try:
        epic = await reopened.get_epic("AgentGUIInt")
        assert epic["epic"]["completion_criteria"] == "Only the target window is controlled."
        assert [t["key"] for t in epic["tickets"]] == ["AgentGUIInt-01", "AgentGUIInt-02"]
        third = await reopened.create_ticket("Mouse control", "Control pointer", epic_id="AgentGUIInt")
        assert third["key"] == "AgentGUIInt-03"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_ticket_reassignment_keeps_keys_unique(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        await app.create_epic("UI", "User interface")
        ticket = await app.create_ticket("Initial standalone", "")
        changed = await app.edit_ticket(
            ticket["id"], expected_revision=ticket["revision"], epic_id="UI"
        )
        assert changed["key"] == "UI-01"
        assert changed["epic_id"] == "UI"
        detached = await app.edit_ticket(
            ticket["id"], expected_revision=changed["revision"], epic_id=""
        )
        assert detached["epic_id"] is None
        assert detached["key"].startswith("T-")
        assert detached["key"] != ticket["key"]
        next_ticket = await app.create_ticket("New UI task", "", epic_id="UI")
        assert next_ticket["key"] == "UI-02"
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_project_manager_exposes_organizational_epic_tools(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        pm = app.project_manager
        names = {item["function"]["name"] for item in pm._tool_manager().schemas()}
        assert {"create_epic", "get_epic", "list_epics", "update_epic"} <= names
        created = await pm._create_epic({
            "epic_id": "NewFeature", "title": "New feature",
            "description": "Split into small tickets.", "completion_criteria": "Feature works.",
        }, "call-create")
        assert created["epic"]["id"] == "NewFeature"
        ticket = await pm._create_ticket({
            "title": "First increment", "description": "Do one bounded piece",
            "epic_id": "NewFeature", "priority": 0,
        }, "call-ticket")
        assert ticket["key"] == "NewFeature-01"
        assert (await pm._get_ticket({"ticket": ticket["key"]}, "call-get"))["ticket"]["epic_id"] == "NewFeature"
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_existing_database_migrates_without_rekeying_tickets(tmp_path):
    path = tmp_path / "existing.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE tickets (id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, "
        "title TEXT NOT NULL, description TEXT NOT NULL, state TEXT NOT NULL, "
        "team_ref TEXT NOT NULL, process_id TEXT NOT NULL, provider_id TEXT)"
    )
    connection.execute(
        "INSERT INTO tickets VALUES(?,?,?,?,?,?,?,?)",
        ("old", "T-001", "Prior ticket", "Preserve this", "Backlog", "team", "process", None),
    )
    connection.execute("PRAGMA user_version=2")
    connection.commit()
    connection.close()
    store = ProjectStore(path)
    await store.open()
    try:
        original = await store.get("tickets", "old")
        assert original["key"] == "T-001"
        assert original["epic_id"] is None
        assert await store.query("SELECT * FROM epics") == []
        assert (await store.one("PRAGMA user_version"))["user_version"] == 3
    finally:
        await store.close()
