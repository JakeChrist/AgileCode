"""Lightweight epic metadata, naming, persistence, and PM integration."""
import asyncio
import sqlite3

import pytest

from agilecode.adapters.store import ProjectStore, SCHEMA_VERSION, encode
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
        await app.move_ticket(first["id"], "Done")
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
async def test_epic_association_of_started_ticket_never_renames_git_key(repository, tmp_path):
    app = Application(repository, tmp_path / "global", client=ScriptClient())
    await app.open(initialize=True)
    try:
        await app.create_epic("LegacyGroup", "Group existing work")
        ticket = await app.create_ticket("Already executed", "")
        original_key = ticket["key"]
        await app.store.execute(
            "INSERT INTO runs(id,ticket_id,snapshot,state,workspace,branch,base_branch,base_oid,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("historical-run", ticket["id"], "{}", "integrated", "/old-worktree",
             "agilecode/" + original_key + "/historical", "master", "old-sha", "2026-01-01", "2026-01-01"),
        )
        attached = await app.edit_ticket(
            ticket["id"], expected_revision=ticket["revision"], epic_id="LegacyGroup"
        )
        assert attached["epic_id"] == "LegacyGroup"
        assert attached["key"] == original_key
        assert (await app.get_epic("LegacyGroup"))["tickets"][0]["key"] == original_key
        detached = await app.edit_ticket(
            ticket["id"], expected_revision=attached["revision"], epic_id=""
        )
        assert detached["key"] == original_key
        assert detached["epic_id"] is None
    finally:
        await app.close()



@pytest.mark.asyncio
async def test_project_manager_exposes_organizational_epic_tools(repository, tmp_path):
    from agilecode.core.project_manager import ProjectManager

    app = Application(repository, tmp_path / "global", execution=False)
    await app.open(initialize=True)
    try:
        pm = ProjectManager(app)
        names = {item["function"]["name"] for item in pm._tool_manager().schemas()}
        assert {"create_epic", "get_epic", "list_epics", "update_epic", "delete_epic"} <= names
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
        delete_tool = pm._tool_manager().tools["delete_epic"]
        deleted = await delete_tool.execute({"epic_id": "NewFeature"}, "call-delete")
        assert deleted["detached_ticket_ids"] == [ticket["id"]]
        assert await delete_tool.execute({"epic_id": "NewFeature"}, "call-delete") == deleted
        preserved = await app.find_ticket(ticket["key"])
        assert preserved["epic_id"] is None and preserved["key"] == "NewFeature-01"
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
        assert (await store.one("PRAGMA user_version"))["user_version"] == SCHEMA_VERSION
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_delete_epic_preserves_running_and_archived_tickets_and_all_owned_work(repository, tmp_path):
    app = Application(repository, tmp_path / "global", execution=False)
    await app.open(initialize=True)
    try:
        epic = await app.create_epic("Preserve", "Preserve ticket work")
        running = await app.create_ticket("Document epic ownership", "Write docs/epic-ownership.md describing grouping-only deletion and verify retained ticket identifiers.", epic_id="Preserve")
        archived = await app.create_ticket("Review retention cases", "Record preservation cases for archived tickets in docs/retention-cases.md.", epic_id="Preserve")
        standalone = await app.create_ticket("Review dependent work", "Review docs/epic-ownership.md after the dependency completes and record findings in docs/review.md.")
        await app.archive_ticket(archived["id"])
        await app.add_dependency(standalone["id"], running["id"])
        await app.move_ticket(running["id"], "In Progress")
        # Mechanical persistence fixtures, with real Git refs and files. No
        # scripted model runs or claims about agent-produced work are involved.
        worktree = repository / ".agilecode" / "worktrees" / running["key"] / "retained"
        base = await app.git.head()
        branch = "agilecode/" + running["key"] + "/retained"
        await app.git.add_worktree(worktree, branch, base)
        await app.store.insert("runs", {
            "id": "retained-run", "ticket_id": running["id"], "snapshot": "{}", "state": "running",
            "workspace": str(worktree), "branch": branch, "base_branch": "master", "base_oid": base,
            "created": running["created"], "updated": running["updated"],
        })
        await app.store.insert("attempts", {
            "id": "retained-attempt", "run_id": "retained-run", "step_id": "understand", "number": 1,
            "session_id": "retained-session", "member": "{}", "model": "not-invoked", "status": "running",
            "context": "{}", "created": running["created"],
        })
        await app.store.message("retained-session", {"role": "user", "content": running["description"]})
        await app.store.insert("requests", {
            "id": "retained-request", "session_id": "retained-session", "model": "not-invoked",
            "input": "{}", "status": "pending", "created": running["created"],
        })
        (worktree / "scratch.txt").write_text("Keep uncommitted work\n")
        checkpoint = {"ref": "refs/agilecode/checkpoints/" + running["id"] + "/retained"}
        await app.git.run("update-ref", checkpoint["ref"], base)
        await app.store.insert("checkpoints", {
            "id": "retained-checkpoint", "ticket_id": running["id"], "run_id": "retained-run",
            "ref": checkpoint["ref"], "oid": base, "label": "Retained history fixture",
            "status": "ready", "step_index": 0, "outputs": "{}", "created": running["created"],
        })
        artifact = repository / ".agilecode" / "artifacts" / running["id"] / "keep.log"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("Keep artifact\n")
        await app.store.insert("artifacts", {
            "id": "preserve-artifact", "ticket_id": running["id"], "name": "Keep log",
            "path": str(artifact), "size": artifact.stat().st_size,
            "complete": 1, "created": running["created"],
        })
        tables = ("dependencies", "runs", "attempts", "messages", "requests", "tools", "waits",
                  "jobs", "artifacts", "evidence", "checkpoints", "integrations", "discussion_tickets", "activity")
        before = {table: await app.store.query(f"SELECT * FROM {table}") for table in tables}
        assert before["attempts"] and before["messages"] and before["requests"]
        tickets_before = {ticket["id"]: ticket for ticket in await app.store.query("SELECT * FROM tickets")}
        operations_before = await app.store.query("SELECT * FROM operations")
        worktrees_before = await app.git.worktrees()
        refs_before = (await app.git.run("show-ref")).text
        events = []
        unsubscribe = app.events.subscribe(events.append)

        result = await app.delete_epic("Preserve", expected_revision=epic["epic"]["revision"], request_id="delete-preserve")

        assert result["deleted"] and result["detached_ticket_count"] == 2
        assert set(result["detached_ticket_ids"]) == {running["id"], archived["id"]}
        assert await app.list_epics() == []
        assert {table: await app.store.query(f"SELECT * FROM {table}") for table in tables} == before
        assert await app.store.query("SELECT * FROM operations WHERE id<>?", ("delete-preserve",)) == operations_before
        for identity, old in tickets_before.items():
            current = await app.find_ticket(identity)
            if identity == standalone["id"]:
                assert current == old
            else:
                assert current["epic_id"] is None
                assert current["revision"] == old["revision"] + 1
                assert {key: value for key, value in current.items() if key not in {"epic_id", "revision", "updated"}} == {
                    key: value for key, value in old.items() if key not in {"epic_id", "revision", "updated"}}
        assert await app.git.worktrees() == worktrees_before
        assert (await app.git.run("show-ref")).text == refs_before
        assert (worktree / "scratch.txt").read_text() == "Keep uncommitted work\n"
        assert artifact.read_text() == "Keep artifact\n"
        assert (await app.git.run("rev-parse", "--verify", checkpoint["ref"])).code == 0
        assert [(event.kind, event.ticket_id) for event in events] == [
            ("EpicChanged", None), *[("TicketChanged", identity) for identity in result["detached_ticket_ids"]]]
        unsubscribe()
        with pytest.raises(Conflict):
            await app.edit_ticket(archived["id"], expected_revision=tickets_before[archived["id"]]["revision"], title="Stale")
    finally:
        await app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["Backlog", "Ready", "In Progress", "Done"])
async def test_epic_delete_never_moves_or_rekeys_tickets(repository, tmp_path, state):
    app = Application(repository, tmp_path / "global", execution=False)
    await app.open(initialize=True)
    try:
        await app.create_epic("Grouping", "Keep board state independent of organization")
        ticket = await app.create_ticket("Record retention requirements", "Write docs/retention.md covering ticket-state preservation when a grouping is removed.", epic_id="Grouping")
        if state != "Backlog":
            await app.move_ticket(ticket["id"], state)
        before = await app.find_ticket(ticket["id"])
        await app.delete_epic("Grouping")
        after = await app.find_ticket(ticket["id"])
        assert after["state"] == state
        assert after["key"] == before["key"] == "Grouping-01"
        assert after["epic_id"] is None
        assert after["selected_run_id"] == before["selected_run_id"]
        assert after["board_transition_id"] == before["board_transition_id"]
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_delete_epic_is_atomic_revision_checked_and_retriable(repository, tmp_path):
    app = Application(repository, tmp_path / "global", execution=False)
    await app.open(initialize=True)
    try:
        await app.create_epic("Atomic", "Atomic grouping")
        ticket = await app.create_ticket("Document transaction guarantees", "Write docs/transaction-guarantees.md and verify rollback leaves ticket metadata unchanged.", epic_id="Atomic")
        epic = await app.update_epic("Atomic", title="Updated grouping")
        events = []
        app.events.subscribe(events.append)
        with pytest.raises(Conflict, match="Refresh before deleting"):
            await app.delete_epic("Atomic", expected_revision=1, request_id="stale-delete")
        await app.store.execute("CREATE TRIGGER fail_epic_delete BEFORE DELETE ON epics BEGIN SELECT RAISE(ABORT, 'injected deletion failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="injected deletion failure"):
            await app.delete_epic("Atomic", expected_revision=epic["epic"]["revision"], request_id="atomic-delete")
        assert await app.find_ticket(ticket["id"]) == ticket
        assert (await app.get_epic("Atomic"))["epic"] == epic["epic"]
        assert not await app.store.one("SELECT 1 FROM meta WHERE key='deleted_epic:Atomic'")
        assert not await app.store.one("SELECT 1 FROM operations WHERE id IN ('stale-delete','atomic-delete')")
        assert not events
        await app.store.execute("DROP TRIGGER fail_epic_delete")
        result = await app.delete_epic("Atomic", expected_revision=epic["epic"]["revision"], request_id="atomic-delete")
        emitted = len(events)
        assert await app.delete_epic("Atomic", expected_revision=epic["epic"]["revision"], request_id="atomic-delete") == result
        assert len(events) == emitted
        with pytest.raises(Conflict, match="different original input"):
            await app.delete_epic("Other", request_id="atomic-delete")
        with pytest.raises(AgileCodeError, match="Unknown epic"):
            await app.delete_epic("Missing")
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_concurrent_epic_delete_retries_commit_and_notify_once(repository, tmp_path, monkeypatch):
    app = Application(repository, tmp_path / "global", execution=False)
    await app.open(initialize=True)
    try:
        await app.create_epic("Repeated", "One grouping deletion")
        ticket = await app.create_ticket("Review retry identity", "Write docs/retry-identity.md describing duplicate command handling.", epic_id="Repeated")
        # Force both callers past the preliminary lookup before either commits.
        prior = app.tickets._prior
        ready = asyncio.Event()
        arrivals = 0

        async def synchronized_prior(*args):
            nonlocal arrivals
            found = await prior(*args)
            arrivals += 1
            if arrivals == 2:
                ready.set()
            await ready.wait()
            return found

        monkeypatch.setattr(app.tickets, "_prior", synchronized_prior)
        events = []
        app.events.subscribe(events.append)
        results = await asyncio.gather(*(app.delete_epic("Repeated", request_id="duplicate-delete") for _ in range(2)))
        assert results[0] == results[1]
        assert (await app.find_ticket(ticket["id"]))["revision"] == ticket["revision"] + 1
        assert [event.kind for event in events] == ["EpicChanged", "TicketChanged"]
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_deleted_epic_ids_remain_reserved_after_reopen_even_without_tickets(repository, tmp_path):
    app = Application(repository, tmp_path / "global", execution=False)
    await app.open(initialize=True)
    try:
        await app.create_epic("Empty", "No tickets")
        assert (await app.list_epics())[0]["ticket_count"] == 0
        assert (await app.delete_epic("Empty"))["detached_ticket_count"] == 0
        await app.create_epic("Old", "Old grouping")
        ticket = await app.create_ticket("Document stable identifiers", "Write docs/stable-identifiers.md describing retained ticket keys after grouping deletion.", epic_id="Old")
        await app.delete_epic("Old")
        assert (await app.find_ticket(ticket["id"]))["key"] == "Old-01"
    finally:
        await app.close()
    reopened = Application(repository, tmp_path / "global", execution=False)
    await reopened.open()
    try:
        for identity in ("Empty", "Old"):
            with pytest.raises(AgileCodeError, match="reserved"):
                await reopened.create_epic(identity, "Reuse would collide with retained keys")
        await reopened.create_epic("New", "New grouping")
        next_ticket = await reopened.create_ticket("Review fresh identifiers", "Review new epic ticket numbering and record findings in docs/identifier-review.md.", epic_id="New")
        assert next_ticket["key"] == "New-01"
        assert (await reopened.find_ticket("Old-01"))["epic_id"] is None
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_pm_epic_deletion_recovers_the_committed_result(repository, tmp_path):
    from agilecode.runtime.conversation_records import recover_execution_payload

    app = Application(repository, tmp_path / "global", execution=False)
    await app.open(initialize=True)
    try:
        await app.create_epic("Recovered", "Recover deleted grouping")
        result = await app.delete_epic("Recovered", request_id="delete-tool")
        tool = {"id": "delete-tool", "name": "delete_epic", "args": encode({"epic_id": "Recovered"}),
                "result": None, "category": "mutation"}
        recovered = await recover_execution_payload(app.store, tool)
        assert recovered["status"] == "ok" and recovered["outcome_known"]
        assert recovered["data"]["result"] == result
        tool["args"] = encode({"epic_id": "Other"})
        unknown = await recover_execution_payload(app.store, tool)
        assert unknown["status"] == "error" and not unknown.get("outcome_known")
    finally:
        await app.close()
