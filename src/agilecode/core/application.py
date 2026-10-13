"""Public headless application API. Desktop and CLI are replaceable clients."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
import traceback
from pathlib import Path
from typing import Any

from .types import (AgileCodeError, AccessDenied, Conflict, NeedsReview, GenerationStopped, BoardState, Settings, Team,
                    StepResult, StepContext, Events, Event, uid, now)
from .runner import Runtime, ProcessRunner, RunControl
from agilecode.adapters.store import ProjectStore, encode, decode
from agilecode.adapters.config import Configuration
from agilecode.adapters.lock import ProjectLock
from agilecode.adapters.git import GitRepository
from agilecode.adapters.workspace import Workspace, digest, inside
from agilecode.adapters.shell import ShellService
from agilecode.adapters.model import OpenAICompatibleClient, ModelUnavailable
from agilecode.adapters.skills import SkillRegistry
from agilecode.adapters.mcp import McpService
from agilecode.adapters.checkpoints import CheckpointService


class Application:
    """One project engine, many isolated concurrent ticket runtimes.

    Construct inside the hosting asyncio loop. No method imports Qt, refers to a
    selected UI ticket, loads a model, or imposes inference-concurrency permits.
    """
    def __init__(self, project: str | Path, global_root: str | Path | None = None, *, client=None):
        self.root = Path(project).expanduser().resolve()
        self.global_root = Path(global_root).expanduser().resolve() if global_root else Path.home()/".agilecode"
        self.storage = self.root/".agilecode"
        self.events = Events()
        self.git = GitRepository(self.root)
        self.store = ProjectStore(self.storage/"agilecode.db")
        self.config = Configuration(self.global_root, self.store)
        self.client = client or OpenAICompatibleClient()
        self.skills = SkillRegistry(self.global_root, self.root)
        self.checkpoints = CheckpointService(self.git, self.store)
        self.runtimes: dict[str, Runtime] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.integration_tasks: dict[str, asyncio.Task] = {}
        self.ticket_locks: dict[str, asyncio.Lock] = {}
        self.all_paused = False
        self._all_execution_lock = asyncio.Lock()
        self._deferred_integrations: set[str] = set()
        self.pending_answers: dict[str, asyncio.Future] = {}
        self.owner: ProjectLock | None = None
        self.opened = False
        self.closed = False
        self.processor = ProcessRunner(self.store, self.git, self.client, self.skills, self.checkpoints,
                                       self.events, lambda:self.config.settings, self.config.get_provider,
                                       self._wait, self._boundary, self.create_ticket)
        from .integration import MergeCoordinator
        self.merges = MergeCoordinator(self)
        from .discussion import ProjectDiscussion
        self.discussion = ProjectDiscussion(self)
        from .project_manager import ProjectManager
        self.project_manager = ProjectManager(self)

    async def open(self, *, initialize=False) -> dict:
        identity = await self.git.identify()
        exists = (self.storage/"agilecode.db").exists()
        if not exists and not initialize:
            raise AgileCodeError("This repository is not initialized. Use 'agilecode init PATH' or Open Project in the desktop app.")
        self.storage.mkdir(parents=True, exist_ok=True)
        self.owner = ProjectLock(self.storage/"engine.lock")
        try:
            await self.store.open()
            prior = await self.store.one("SELECT value FROM meta WHERE key='project'")
            if prior and decode(prior["value"])["common_git_dir"] != identity["common_git_dir"]:
                raise Conflict("Stored Git identity differs. Use the explicit backup relocation workflow rather than reusing this state in an unrelated repository.")
            if not prior:
                identity["id"] = uid()
                await self.store.execute("INSERT INTO meta VALUES('project',?)", (encode(identity),))
            ignored_changed = await self.git.ensure_ignore() if initialize else False
            await self.config.load()
            await self.discussion.ensure_legacy()
            await self._install_builtin_skills()
            await asyncio.to_thread(self.project_manager.profiles.install_bundled)
            await self.project_manager.open()
            await self._recover()
            controls = await self.store.one("SELECT value FROM meta WHERE key='execution_controls'")
            if controls:
                saved = decode(controls["value"], {})
                self.all_paused = bool(saved.get("all_paused", False))
                self._deferred_integrations = set(saved.get("deferred_integrations", []))
            self.opened = True
            return {"project": str(self.root), "initialized": not exists, "gitignore_changed": ignored_changed,
                    "notice": "Commit the .gitignore change before starting a clean-base ticket." if ignored_changed else ""}
        except BaseException:
            await self.store.close()
            self.owner.close()
            raise

    async def _install_builtin_skills(self):
        source = Path(__file__).resolve().parents[1]/"resources"/"skills"
        if not source.exists():
            return
        for folder in sorted(source.iterdir()):
            if not folder.is_dir() or not (folder/"SKILL.md").is_file():
                continue
            target = self.global_root/"skills"/folder.name
            if target.is_symlink():
                raise AgileCodeError(f"Bundled skill directory must not be a symlink: {target}")
            if not target.exists():
                await asyncio.to_thread(shutil.copytree, folder, target)
                continue
            if not target.is_dir():
                raise AgileCodeError(f"Bundled skill path is not a directory: {target}")

            # Refresh shipped skills on application startup after an upgrade.
            # Back up differing installed files (including user edits) before replacing them.
            incoming = [p for p in folder.rglob("*") if p.is_file()]
            changed = False
            replaced = False
            for file in incoming:
                destination = target/file.relative_to(folder)
                if destination.is_symlink() or any(
                    parent.is_symlink() for parent in destination.parents if parent == target or target in parent.parents
                ):
                    raise AgileCodeError(f"Bundled skill destination must not be a symlink: {destination}")
                if not destination.exists():
                    changed = True
                elif not destination.is_file():
                    raise AgileCodeError(f"Bundled skill destination is not a file: {destination}")
                elif destination.read_bytes() != file.read_bytes():
                    changed = replaced = True
            if not changed:
                continue
            if replaced:
                backup = self.global_root/"skill-backups"/f"{folder.name}-{uid()}"
                backup.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.copytree, target, backup)
            await asyncio.to_thread(shutil.copytree, folder, target, dirs_exist_ok=True)

    async def _recover(self):
        # Restore durable state without replaying interrupted tool calls.
        await self.store.execute("UPDATE requests SET status='interrupted',error='Engine stopped during response',finished=? WHERE status='streaming'", (now(),))
        await self.store.execute("UPDATE jobs SET status='interrupted',finished=? WHERE status IN ('running','starting','unknown')", (now(),))
        await self.store.execute("UPDATE attempts SET status='interrupted' WHERE status='running'")
        await self.store.execute("UPDATE runs SET state='paused',error='Engine restarted. Interrupted tool results are recorded in the step conversation.' WHERE state IN ('running','pause_requested')")
        await self.checkpoints.recover()
        for op in await self.store.query("SELECT * FROM operations WHERE kind='final_commit' AND status='prepared'"):
            run = await self.store.get("runs", op["owner_id"])
            data = decode(op["data"])
            cwd = Path(run["workspace"])
            if not cwd.exists():
                continue
            try:
                oid = await self.git.head(cwd)
                tree = (await self.git.run("rev-parse", "HEAD^{tree}", cwd=cwd)).text
                parents = (await self.git.run("rev-list", "--parents", "-n", "1", oid, cwd=cwd)).text.split()[1:]
                if oid != data["base_oid"] and tree == data["expected_tree"] and parents == [data["base_oid"]]:
                    await self.store.update("runs", run["id"], final_oid=oid, final_tree=tree)
                    await self.store.update("operations", op["id"], status="completed", result=encode({"oid":oid}), updated=now())
                    for tool in await self.store.query("SELECT tools.* FROM tools JOIN attempts ON tools.session_id=attempts.session_id WHERE attempts.run_id=? AND tools.name='final_commit' AND tools.status IN ('running','unknown')", (run["id"],)):
                        await self.store.update("tools", tool["id"], status="completed", result=encode({"status":"ok","_execution_id":tool["id"],"data":{"commit":oid,"recovered":True}}), finished=now())
            except AgileCodeError:
                pass
        for run in await self.store.query("SELECT * FROM runs WHERE state='provisioning'"):
            try:
                await self.git.verify_worktree(Path(run["workspace"]), run["branch"])
                if await self.git.head(Path(run["workspace"])) == run["base_oid"]:
                    await self.store.update("runs", run["id"], state="paused", error="Recovered worktree provisioning; resume when ready.")
                    await self.store.execute("UPDATE tickets SET state='In Progress',revision=revision+1 WHERE id=?", (run["ticket_id"],))
            except AgileCodeError:
                await self.store.update("runs", run["id"], state="failed", error="Worktree provisioning was incomplete; original files were not changed.")
        await self.merges.recover()
        # Record lost tool results in the existing step conversation rather
        # than requiring a separate manual reconciliation workflow.
        from .tools import interrupted_tool_result
        for tool in await self.store.query("SELECT * FROM tools WHERE status IN ('running','unknown')"):
            payload = interrupted_tool_result(tool["id"])
            await self.store.update("tools", tool["id"], status="completed",
                                    result=encode(payload), finished=now())
            messages = await self.store.messages(tool["session_id"])
            has_call = any(
                message["request_id"] == tool["request_id"]
                and message["body"].get("role") == "assistant"
                and any(call.get("id") == tool["call_id"]
                        for call in message["body"].get("tool_calls", []))
                for message in messages
            )
            has_result = any(
                message["request_id"] == tool["request_id"]
                and message["body"].get("role") == "tool"
                and message["body"].get("tool_call_id") == tool["call_id"]
                for message in messages
            )
            if has_call and not has_result:
                await self.store.message(tool["session_id"], {
                    "role": "tool", "tool_call_id": tool["call_id"], "content": encode(payload),
                }, tool["request_id"])

    def _ticket_lock(self, ticket_id):
        return self.ticket_locks.setdefault(ticket_id, asyncio.Lock())

    async def tool_catalog(self) -> dict:
        """Discover configured MCP names for explicit member selection."""
        from .types import NATIVE_TOOL_NAMES
        mcp = McpService(self.config.settings.mcp_servers, self.root)
        try:
            await mcp.discover()
            tools = [{"name": name, "label": name} for name in NATIVE_TOOL_NAMES]
            tools.extend({"name": alias, "label": f"{remote['server']}: {remote['remote_name']}"}
                         for alias, remote in mcp.tools.items())
            return {"tools": tools, "errors": mcp.errors}
        finally:
            await mcp.close()


    @staticmethod
    def _epic_id(value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", value):
            raise AgileCodeError("Epic ID must start with a letter and contain up to 40 letters, digits or underscores.")
        return value

    async def create_epic(self, epic_id: str, title: str, description: str = "",
                          completion_criteria: str = "") -> dict:
        epic_id = self._epic_id(epic_id)
        if not epic_id:
            raise AgileCodeError("An epic requires an ID.")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise AgileCodeError("Provide an epic title (1–200 characters).")
        if not isinstance(description, str) or len(description) > 100_000:
            raise AgileCodeError("Epic description cannot exceed 100,000 characters.")
        if not isinstance(completion_criteria, str) or len(completion_criteria) > 100_000:
            raise AgileCodeError("Epic completion criteria cannot exceed 100,000 characters.")
        timestamp = now()
        try:
            await self.store.insert("epics", {
                "id": epic_id, "title": title.strip(), "description": description,
                "completion_criteria": completion_criteria, "status": "Active",
                "created": timestamp, "updated": timestamp,
            })
        except sqlite3.IntegrityError as exc:
            raise AgileCodeError(f"Epic ID {epic_id!r} is already in use.") from exc
        self.events.emit(Event("EpicChanged", data={"epic_id": epic_id}))
        return await self.get_epic(epic_id)

    async def list_epics(self) -> list[dict]:
        return await self.store.query(
            "SELECT e.id,e.title,SUBSTR(e.description,1,500) AS description,e.status,"
            "e.revision,e.created,e.updated,COUNT(t.id) AS ticket_count,"
            "SUM(CASE WHEN t.state='Done' THEN 1 ELSE 0 END) AS completed_count "
            "FROM epics e LEFT JOIN tickets t ON t.epic_id=e.id "
            "GROUP BY e.id ORDER BY e.created,e.id"
        )

    async def get_epic(self, epic_id: str) -> dict:
        epic = await self.store.get("epics", epic_id)
        tickets = await self.store.query(
            "SELECT id,key,title,state,archived FROM tickets WHERE epic_id=? ORDER BY created,key",
            (epic_id,),
        )
        return {
            "epic": {key: value for key, value in epic.items() if key != "next_ticket_number"},
            "tickets": tickets, "ticket_count": len(tickets),
            "completed_count": sum(ticket["state"] == "Done" for ticket in tickets),
        }

    async def update_epic(self, epic_id: str, *, expected_revision: int | None = None,
                          **changes) -> dict:
        if not changes.keys() <= {"title", "description", "completion_criteria", "status"}:
            raise AgileCodeError("Unsupported epic field.")
        epic = await self.store.get("epics", epic_id)
        if "title" in changes:
            title = changes["title"]
            if not isinstance(title, str) or not title.strip() or len(title) > 200:
                raise AgileCodeError("Provide an epic title (1–200 characters).")
            changes["title"] = title.strip()
        for field in ("description", "completion_criteria"):
            if field in changes and (not isinstance(changes[field], str) or len(changes[field]) > 100_000):
                raise AgileCodeError(f"Epic {field.replace('_', ' ')} cannot exceed 100,000 characters.")
        if "status" in changes and changes["status"] not in {"Active", "Completed", "Archived"}:
            raise AgileCodeError("Epic status must be Active, Completed, or Archived.")
        if not changes:
            return await self.get_epic(epic_id)
        revision = epic["revision"] if expected_revision is None else expected_revision
        sql = ("UPDATE epics SET " + ",".join(f"{field}=?" for field in changes) +
               ",revision=revision+1,updated=? WHERE id=? AND revision=?")
        count = await self.store.execute(sql, (*changes.values(), now(), epic_id, revision))
        if count != 1:
            raise Conflict("Epic changed since it was opened. Refresh before saving.")
        self.events.emit(Event("EpicChanged", data={"epic_id": epic_id}))
        return await self.get_epic(epic_id)

    async def create_ticket(self, title: str, description: str, *, team_ref: str | None = None,
                            process_id: str | None = None, parent_id: str | None = None,
                            provider_id: str | None = None, epic_id: str | None = None,
                            merge_auto: bool | None = None, source_discussion_id: str | None = None,
                            request_id: str | None = None) -> dict:
        if not title.strip() or len(title)>200 or len(description)>100000:
            raise AgileCodeError("Provide a title (1–200 characters) and a description up to 100,000 characters.")
        epic_id = self._epic_id(epic_id)
        if request_id:
            prior = await self.store.one("SELECT result FROM operations WHERE id=? AND kind='create_ticket'", (request_id,))
            if prior:
                return await self.store.get("tickets", decode(prior["result"])["ticket_id"])
        settings = self.config.settings
        reference = team_ref or settings.default_team
        team = await self.config.get_team(reference)
        process_id = process_id or (settings.default_process if reference == settings.default_team else team.default_process_id)
        if process_id not in {p.id for p in team.processes}:
            raise AgileCodeError("Choose a process owned by the selected team.")
        if parent_id:
            await self.store.get("tickets", parent_id)
        if provider_id is not None:
            self.config.get_provider(provider_id)
        source_discussion = await self.discussion.get(source_discussion_id) if source_discussion_id else None
        identity, timestamp = uid(), now()
        def create(c):
            row = c.execute("SELECT value FROM meta WHERE key='ticket_sequence'").fetchone()
            number = int(row[0])+1 if row else 1
            c.execute("INSERT INTO meta VALUES('ticket_sequence',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(number),))
            key = f"T-{number:03d}"
            if epic_id:
                epic = c.execute("SELECT next_ticket_number FROM epics WHERE id=?", (epic_id,)).fetchone()
                if epic is None:
                    raise AgileCodeError(f"Unknown epic: {epic_id}")
                key = f"{epic_id}-{epic['next_ticket_number']:02d}"
                c.execute("UPDATE epics SET next_ticket_number=next_ticket_number+1 WHERE id=?", (epic_id,))
            c.execute("INSERT INTO tickets(id,key,title,description,state,team_ref,process_id,provider_id,epic_id,parent_id,merge_auto,position,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identity, key, title.strip(), description, "Backlog", reference, process_id, provider_id, epic_id, parent_id,
                 int(merge_auto) if merge_auto is not None else None, number*1024, timestamp, timestamp))
            if source_discussion:
                c.execute("INSERT INTO discussion_tickets(discussion_id,ticket_id,created) VALUES(?,?,?)",
                          (source_discussion["id"], identity, timestamp))
            if request_id:
                c.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)", (request_id,"create_ticket",identity,"{}","completed",encode({"ticket_id":identity}),timestamp,timestamp))
        await self.store.transaction(create)
        self.events.emit(Event("TicketChanged", identity))
        return await self.store.get("tickets", identity)

    async def find_ticket(self, identity: str) -> dict:
        row = await self.store.one("SELECT * FROM tickets WHERE id=? OR key=?", (identity, identity))
        if not row:
            raise AgileCodeError(f"Unknown ticket: {identity}")
        return row

    async def edit_ticket(self, ticket_id: str, *, expected_revision: int, **changes) -> dict:
        allowed = {"title", "description", "team_ref", "process_id", "provider_id", "priority", "merge_auto", "epic_id"}
        if not changes.keys() <= allowed:
            raise AgileCodeError("Unsupported ticket field.")
        ticket = await self.find_ticket(ticket_id)
        if ticket["state"] == "In Progress" and changes.keys() & {"description", "team_ref", "process_id", "provider_id", "epic_id"}:
            raise AgileCodeError("Ticket/process/provider inputs are frozen during execution. Cancel and start a revised run to change them.")
        team = await self.config.get_team(changes.get("team_ref",ticket["team_ref"]))
        if "provider_id" in changes and changes["provider_id"] is not None:
            self.config.get_provider(changes["provider_id"])
        if changes.get("process_id",ticket["process_id"]) not in {p.id for p in team.processes}:
            raise AgileCodeError("The selected process does not belong to this team.")
        if "epic_id" in changes:
            changes["epic_id"] = self._epic_id(changes["epic_id"])
            if changes["epic_id"] != ticket["epic_id"] and await self.store.one(
                "SELECT 1 FROM runs WHERE ticket_id=? LIMIT 1", (ticket["id"],)
            ):
                raise AgileCodeError("Cannot change a ticket's epic after execution has begun; its Git worktree uses the existing ticket key.")
        if not changes:
            return ticket

        def edit(c):
            fields = dict(changes)
            if "epic_id" in fields and fields["epic_id"] != ticket["epic_id"]:
                if fields["epic_id"]:
                    epic = c.execute("SELECT next_ticket_number FROM epics WHERE id=?", (fields["epic_id"],)).fetchone()
                    if epic is None:
                        raise AgileCodeError(f"Unknown epic: {fields['epic_id']}")
                    fields["key"] = f"{fields['epic_id']}-{epic['next_ticket_number']:02d}"
                    c.execute("UPDATE epics SET next_ticket_number=next_ticket_number+1 WHERE id=?", (fields["epic_id"],))
                else:
                    row = c.execute("SELECT value FROM meta WHERE key='ticket_sequence'").fetchone()
                    number = int(row[0]) + 1 if row else 1
                    c.execute("INSERT INTO meta VALUES('ticket_sequence',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(number),))
                    fields["key"] = f"T-{number:03d}"
            sql = ("UPDATE tickets SET " + ",".join(f"{field}=?" for field in fields) +
                   ",revision=revision+1,updated=? WHERE id=? AND revision=?")
            count = c.execute(sql, (*fields.values(), now(), ticket["id"], expected_revision)).rowcount
            if count != 1:
                raise Conflict("Ticket changed since it was opened. Refresh before saving.")

        await self.store.transaction(edit)
        self.events.emit(Event("TicketChanged", ticket["id"]))
        return await self.store.get("tickets", ticket["id"])

    async def add_dependency(self, ticket_id: str, depends_on: str):
        ticket = await self.find_ticket(ticket_id); dependency = await self.find_ticket(depends_on)
        if ticket["id"] == dependency["id"]:
            raise AgileCodeError("A ticket cannot depend on itself.")
        cycle = await self.store.one("WITH RECURSIVE chain(id) AS (SELECT depends_on FROM dependencies WHERE ticket_id=? UNION SELECT d.depends_on FROM dependencies d JOIN chain c ON d.ticket_id=c.id) SELECT id FROM chain WHERE id=?", (dependency["id"],ticket["id"]))
        if cycle:
            raise AgileCodeError("This dependency would create a cycle.")
        await self.store.execute("INSERT OR IGNORE INTO dependencies VALUES(?,?)", (ticket["id"],dependency["id"]))
        self.events.emit(Event("TicketChanged", ticket["id"]))

    async def remove_dependency(self, ticket_id: str, depends_on: str):
        ticket = await self.find_ticket(ticket_id); dependency = await self.find_ticket(depends_on)
        await self.store.execute("DELETE FROM dependencies WHERE ticket_id=? AND depends_on=?", (ticket["id"],dependency["id"]))
        self.events.emit(Event("TicketChanged",ticket["id"]))

    async def move_ticket(self, ticket_id: str, state: str, *, expected_revision: int | None = None, position: float | None = None, allow_dirty_base=False) -> dict:
        target = BoardState(state)
        ticket = await self.find_ticket(ticket_id)
        if expected_revision is not None and ticket["revision"] != expected_revision:
            raise Conflict("The board card changed; refresh before moving it.")
        if target.value == ticket["state"]:
            if position is not None:
                import math
                if not math.isfinite(position):
                    raise AgileCodeError("Card position must be finite.")
                await self.store.update("tickets",ticket["id"],position=position,revision=ticket["revision"]+1,updated=now())
                self.events.emit(Event("TicketChanged",ticket["id"]))
            return await self.store.get("tickets",ticket["id"])
        if target == BoardState.IN_PROGRESS:
            return await self.start_ticket(ticket["id"], allow_dirty_base=allow_dirty_base)
        if target == BoardState.DONE and ticket["state"] != "Done":
            raise AgileCodeError("Done is set after verified integration, not by bypassing the process with a drag.")
        if ticket["state"] == "In Progress":
            run = await self.latest_run(ticket["id"])
            if run and run["state"] not in {"cancelled", "failed", "integrated"}:
                raise AgileCodeError("Pause/cancel or integrate this run before moving the ticket out of In Progress.")
        if ticket["state"] == "Done" and target != BoardState.BACKLOG:
            raise AgileCodeError("Reopen a completed ticket into Backlog first.")
        fields = {"state": target.value, "updated": now(), "revision":ticket["revision"]+1}
        if position is not None:
            fields["position"] = float(position)
        await self.store.update("tickets", ticket["id"], **fields)
        self.events.emit(Event("TicketChanged", ticket["id"]))
        return await self.store.get("tickets", ticket["id"])

    async def archive_ticket(self, ticket_id: str, archived=True):
        ticket = await self.find_ticket(ticket_id)
        run = await self.latest_run(ticket["id"])
        if archived and run and run["state"] not in {"cancelled", "failed", "integrated"}:
            raise AgileCodeError("Finish integration or cancel the run before archiving. Unmerged files/history will be retained.")
        await self.store.update("tickets", ticket["id"], archived=int(archived), revision=ticket["revision"]+1, updated=now())
        self.events.emit(Event("TicketChanged",ticket["id"]))

    async def delete_ticket(self, ticket_id: str) -> dict:
        """Permanently erase an unintegrated ticket and every resource it owns."""
        ticket = await self.find_ticket(ticket_id)
        lock = self._ticket_lock(ticket["id"])
        async with lock:
            runs = await self.store.query(
                "SELECT * FROM runs WHERE ticket_id=? ORDER BY created,id",
                (ticket["id"],),
            )
            integrations = await self.store.query(
                "SELECT * FROM integrations WHERE ticket_id=? ORDER BY created,id",
                (ticket["id"],),
            )

            if ticket["state"] == "Done" or any(run["state"] == "integrated" for run in runs) or any(
                record["state"] == "integrated" for record in integrations
            ):
                raise AgileCodeError(
                    "Integrated/completed tickets cannot be deleted. Their changes are already part of the project history."
                )

            inbound = await self.store.query(
                "SELECT t.key FROM dependencies d JOIN tickets t ON t.id=d.ticket_id "
                "WHERE d.depends_on=? ORDER BY t.key",
                (ticket["id"],),
            )
            children = await self.store.query(
                "SELECT key FROM tickets WHERE parent_id=? ORDER BY key",
                (ticket["id"],),
            )
            if inbound or children:
                references = [row["key"] for row in inbound] + [row["key"] for row in children]
                raise Conflict(
                    "Delete blocked because other tickets reference this ticket: "
                    + ", ".join(references)
                    + ". Remove those references explicitly first; deletion never edits another ticket."
                )

            # Refuse deletion if Git shows that ticket code already reached its target
            # branch even if the database did not finish recording integration.
            for run in runs:
                if run["final_oid"]:
                    merged = await self.git.run(
                        "merge-base", "--is-ancestor", run["final_oid"],
                        f"refs/heads/{run['base_branch']}", check=False,
                    )
                    if merged.code == 0:
                        raise AgileCodeError(
                            "Ticket commit is already reachable from its target branch; deletion would not remove it from project history."
                        )
            for record in integrations:
                if record["candidate_oid"]:
                    merged = await self.git.run(
                        "merge-base", "--is-ancestor", record["candidate_oid"],
                        f"refs/heads/{record['target_branch']}", check=False,
                    )
                    if merged.code == 0:
                        raise AgileCodeError(
                            "An integration candidate is already reachable from its target branch; this ticket cannot be erased safely."
                        )

            run_root = (self.storage / "worktrees").resolve()
            integration_root = (self.storage / "integration").resolve()

            def owned_path(value: str, root: Path, label: str) -> Path:
                path = Path(value).resolve()
                if path == root or not inside(path, root):
                    raise Conflict(f"Recorded {label} path is outside AgileCode-owned storage: {path}")
                return path

            run_paths = {
                run["id"]: owned_path(run["workspace"], run_root, "ticket worktree")
                for run in runs
            }
            integration_paths = {
                record["id"]: owned_path(record["workspace"], integration_root, "integration worktree")
                for record in integrations
            }

            # Validate branch/worktree ownership before destroying anything.
            worktrees = await self.git.worktrees()
            for run in runs:
                path = run_paths[run["id"]]
                ref = f"refs/heads/{run['branch']}"
                registered = worktrees.get(str(path))
                if registered and registered.get("branch") != ref:
                    raise Conflict("Recorded ticket worktree no longer owns its ticket branch.")
                other_users = [
                    worktree for worktree, data in worktrees.items()
                    if data.get("branch") == ref and Path(worktree).resolve() != path
                ]
                if other_users:
                    raise Conflict(
                        "Ticket branch is checked out outside its recorded ticket worktree: "
                        + ", ".join(other_users[:3])
                    )

            # Stop every live activity owned by the ticket before removing files.
            for run in runs:
                runtime = self.runtimes.get(run["id"])
                if runtime:
                    runtime.control.cancelled = True
                    runtime.control.permit.set()
            tasks = [
                self.tasks[run["id"]]
                for run in runs
                if run["id"] in self.tasks and not self.tasks[run["id"]].done()
            ]
            integration_task = self.integration_tasks.get(ticket["id"])
            if integration_task and not integration_task.done():
                tasks.append(integration_task)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            # Integration preparation can race with a deletion request. Re-read its
            # durable state after cancellation and re-check target reachability
            # before any ticket-owned Git state is destroyed.
            runs = await self.store.query(
                "SELECT * FROM runs WHERE ticket_id=? ORDER BY created,id",
                (ticket["id"],),
            )
            integrations = await self.store.query(
                "SELECT * FROM integrations WHERE ticket_id=? ORDER BY created,id",
                (ticket["id"],),
            )
            if any(run["state"] == "integrated" for run in runs) or any(
                record["state"] == "integrated" for record in integrations
            ):
                raise AgileCodeError(
                    "Ticket became integrated while deletion was being prepared; no ticket-owned Git state was deleted."
                )
            for run in runs:
                if run["final_oid"]:
                    merged = await self.git.run(
                        "merge-base", "--is-ancestor", run["final_oid"],
                        f"refs/heads/{run['base_branch']}", check=False,
                    )
                    if merged.code == 0:
                        raise AgileCodeError(
                            "Ticket commit reached its target branch while deletion was being prepared; deletion was stopped."
                        )
            for record in integrations:
                if record["candidate_oid"]:
                    merged = await self.git.run(
                        "merge-base", "--is-ancestor", record["candidate_oid"],
                        f"refs/heads/{record['target_branch']}", check=False,
                    )
                    if merged.code == 0:
                        raise AgileCodeError(
                            "Integration candidate reached its target branch while deletion was being prepared; deletion was stopped."
                        )
            run_paths = {
                run["id"]: owned_path(run["workspace"], run_root, "ticket worktree")
                for run in runs
            }
            integration_paths = {
                record["id"]: owned_path(record["workspace"], integration_root, "integration worktree")
                for record in integrations
            }

            # A destructive delete may only consume resources uniquely owned by
            # this ticket. Inconsistent shared ownership is a hard stop rather
            # than permission to mutate another ticket.
            worktrees = await self.git.worktrees()
            for run in runs:
                other = await self.store.one(
                    "SELECT t.key FROM runs r JOIN tickets t ON t.id=r.ticket_id "
                    "WHERE r.ticket_id<>? AND (r.workspace=? OR r.branch=?) LIMIT 1",
                    (ticket["id"], run["workspace"], run["branch"]),
                )
                if other:
                    raise Conflict(f"Ticket Git resources are also recorded by {other['key']}; deletion stopped.")
                path = run_paths[run["id"]]
                ref = f"refs/heads/{run['branch']}"
                registered = worktrees.get(str(path))
                if registered and registered.get("branch") != ref:
                    raise Conflict("Recorded ticket worktree no longer owns its ticket branch.")
                other_users = [
                    worktree for worktree, data in worktrees.items()
                    if data.get("branch") == ref and Path(worktree).resolve() != path
                ]
                if other_users:
                    raise Conflict(
                        "Ticket branch is checked out outside its recorded ticket worktree: "
                        + ", ".join(other_users[:3])
                    )
            for record in integrations:
                other = await self.store.one(
                    "SELECT t.key FROM integrations i JOIN tickets t ON t.id=i.ticket_id "
                    "WHERE i.ticket_id<>? AND i.workspace=? LIMIT 1",
                    (ticket["id"], record["workspace"]),
                )
                if other:
                    raise Conflict(f"Integration worktree is also recorded by {other['key']}; deletion stopped.")

            for run in runs:
                runtime = self.runtimes.pop(run["id"], None)
                if runtime:
                    await runtime.shell.close()
                    await runtime.mcp.close()
                self.tasks.pop(run["id"], None)
            self.integration_tasks.pop(ticket["id"], None)
            for record in integrations:
                runtime = self.merges.runtimes.pop(record["id"], None)
                if runtime:
                    await runtime.shell.close()
                    await runtime.mcp.close()

            async def discard_unregistered(path: Path):
                if path.is_symlink():
                    await asyncio.to_thread(path.unlink)
                elif path.exists():
                    await asyncio.to_thread(shutil.rmtree, path)

            # Integration worktrees/refs are ticket-owned scratch state.
            for record in integrations:
                path = integration_paths[record["id"]]
                if str(path) in await self.git.worktrees():
                    await self.git.delete_owned_worktree(path)
                else:
                    await discard_unregistered(path)
                ref = f"refs/agilecode/integrations/{record['id']}"
                actual = await self.git.run("rev-parse", "--verify", ref, check=False)
                if actual.code == 0:
                    await self.git.run("update-ref", "-d", ref, actual.text)

            checkpoint_prefix = f"refs/agilecode/checkpoints/{ticket['id']}/"
            checkpoint_refs = (
                await self.git.run("for-each-ref", "--format=%(refname)", checkpoint_prefix)
            ).text.splitlines()
            for ref in checkpoint_refs:
                actual = await self.git.run("rev-parse", "--verify", ref, check=False)
                if actual.code == 0:
                    await self.git.run("update-ref", "-d", ref, actual.text)

            # Ticket worktrees are deliberately removed even when dirty. Once all
            # worktrees are gone, their dedicated branches are forcibly deleted.
            for run in runs:
                path = run_paths[run["id"]]
                if str(path) in await self.git.worktrees():
                    await self.git.delete_owned_worktree(path, run["branch"])
                else:
                    await discard_unregistered(path)
            for run in runs:
                await self.git.delete_owned_branch(run["branch"])

            artifact_root = (self.storage / "artifacts" / ticket["id"]).resolve()
            artifacts_parent = (self.storage / "artifacts").resolve()
            if artifact_root != artifacts_parent and inside(artifact_root, artifacts_parent):
                await discard_unregistered(artifact_root)

            owned_tool_ids = {
                row["id"] for row in await self.store.query(
                    "SELECT tools.id FROM tools JOIN attempts ON attempts.session_id=tools.session_id "
                    "JOIN runs ON runs.id=attempts.run_id WHERE runs.ticket_id=?",
                    (ticket["id"],),
                )
            }
            owned_job_ids = {
                row["id"] for row in await self.store.query(
                    "SELECT id FROM jobs WHERE ticket_id=?",
                    (ticket["id"],),
                )
            }
            run_ids = {run["id"] for run in runs}
            orphan_activity_ids = []
            for row in await self.store.query("SELECT * FROM activity WHERE ticket_id IS NULL"):
                data = decode(row["data"], {})
                if (
                    data.get("id") in owned_tool_ids
                    or data.get("id") in owned_job_ids
                    or data.get("run_id") in run_ids
                ):
                    orphan_activity_ids.append(row["id"])

            def erase(c):
                c.execute(
                    "DELETE FROM operations WHERE owner_id=? "
                    "OR owner_id IN (SELECT id FROM runs WHERE ticket_id=?) "
                    "OR owner_id IN (SELECT id FROM integrations WHERE ticket_id=?)",
                    (ticket["id"], ticket["id"], ticket["id"]),
                )
                c.execute(
                    "DELETE FROM evidence WHERE attempt_id IN "
                    "(SELECT a.id FROM attempts a JOIN runs r ON r.id=a.run_id WHERE r.ticket_id=?)",
                    (ticket["id"],),
                )
                c.execute("DELETE FROM waits WHERE ticket_id=?", (ticket["id"],))
                c.execute("DELETE FROM jobs WHERE ticket_id=?", (ticket["id"],))
                c.execute(
                    "DELETE FROM tools WHERE session_id IN "
                    "(SELECT a.session_id FROM attempts a JOIN runs r ON r.id=a.run_id WHERE r.ticket_id=?)",
                    (ticket["id"],),
                )
                c.execute(
                    "DELETE FROM requests WHERE session_id IN "
                    "(SELECT a.session_id FROM attempts a JOIN runs r ON r.id=a.run_id WHERE r.ticket_id=?)",
                    (ticket["id"],),
                )
                c.execute(
                    "DELETE FROM messages WHERE session_id IN "
                    "(SELECT a.session_id FROM attempts a JOIN runs r ON r.id=a.run_id WHERE r.ticket_id=?)",
                    (ticket["id"],),
                )
                c.execute(
                    "DELETE FROM attempts WHERE run_id IN (SELECT id FROM runs WHERE ticket_id=?)",
                    (ticket["id"],),
                )
                c.execute("DELETE FROM checkpoints WHERE ticket_id=?", (ticket["id"],))
                c.execute("DELETE FROM integrations WHERE ticket_id=?", (ticket["id"],))
                c.execute("DELETE FROM artifacts WHERE ticket_id=?", (ticket["id"],))
                c.execute("DELETE FROM runs WHERE ticket_id=?", (ticket["id"],))
                c.execute("DELETE FROM discussion_tickets WHERE ticket_id=?", (ticket["id"],))
                c.execute("DELETE FROM dependencies WHERE ticket_id=?", (ticket["id"],))
                c.execute("DELETE FROM activity WHERE ticket_id=?", (ticket["id"],))
                if orphan_activity_ids:
                    marks = ",".join("?" for _ in orphan_activity_ids)
                    c.execute(f"DELETE FROM activity WHERE id IN ({marks})", tuple(orphan_activity_ids))
                c.execute("DELETE FROM tickets WHERE id=?", (ticket["id"],))
            await self.store.transaction(erase)

            result = {"ticket": ticket["id"], "key": ticket["key"], "deleted": True}
            self.events.emit(Event("TicketDeleted", ticket["id"], data={"key": ticket["key"]}))

        self.ticket_locks.pop(ticket["id"], None)
        return result

    async def board(self, query="", *, include_archived=False) -> list[dict]:
        rows = await self.store.query("SELECT t.*,r.id AS run_id,r.state AS execution_state,r.step_index,r.error AS run_error FROM tickets t LEFT JOIN runs r ON r.id=(SELECT id FROM runs WHERE ticket_id=t.id ORDER BY (state NOT IN ('completed','cancelled','failed','integrated')) DESC,created DESC,id DESC LIMIT 1) WHERE (? OR t.archived=0) AND (t.title LIKE ? OR t.key LIKE ? OR t.description LIKE ?) ORDER BY t.priority DESC,t.position,t.created", (int(include_archived),f"%{query}%",f"%{query}%",f"%{query}%"))
        return rows

    async def latest_run(self, ticket_id: str) -> dict | None:
        return await self.store.one("SELECT * FROM runs WHERE ticket_id=? ORDER BY created DESC,id DESC LIMIT 1", (ticket_id,))

    async def active_run(self, ticket_id: str) -> dict | None:
        """Return the ticket's active execution, not merely its newest run record."""
        active = await self.store.one(
            "SELECT * FROM runs WHERE ticket_id=? "
            "AND state NOT IN ('completed','cancelled','failed','integrated') "
            "ORDER BY created DESC,id DESC LIMIT 1",
            (ticket_id,),
        )
        if active:
            return active

        # Repair compatibility for In Progress tickets left in a terminal run
        # state by older builds. Existing development history/worktree remains the
        # execution to resume; provisioning failures without an attempt are excluded.
        return await self.store.one(
            "SELECT r.* FROM runs r JOIN tickets t ON t.id=r.ticket_id "
            "WHERE r.ticket_id=? AND t.state='In Progress' AND r.state IN ('failed','cancelled') "
            "AND EXISTS (SELECT 1 FROM attempts a WHERE a.run_id=r.id AND a.phase='development') "
            "ORDER BY r.created DESC,r.id DESC LIMIT 1",
            (ticket_id,),
        )

    async def execution_status(self, ticket_id: str, run: dict | None = None) -> dict:
        run = run or await self.active_run(ticket_id)
        if not run:
            return {
                "run_id": None,
                "state": None,
                "can_resume": False,
                "can_pause": False,
            }

        snapshot = decode(run["snapshot"])
        process = Team.model_validate(snapshot["team"]).process(snapshot["process_id"])
        step_id = process.steps[run["step_index"]].id if run["step_index"] < len(process.steps) else None
        attempt = None
        if step_id:
            attempt = await self.store.one(
                "SELECT * FROM attempts WHERE run_id=? AND step_id=? AND phase='development' "
                "AND status IN ('running','waiting','interrupted','paused') "
                "ORDER BY number DESC LIMIT 1",
                (run["id"], step_id),
            )

        task = self.tasks.get(run["id"])
        task_running = bool(task and not task.done())
        interrupted = bool(attempt and attempt["status"] == "interrupted")
        can_resume = run["state"] != "pause_requested" and (
            run["state"] in {"paused", "failed", "cancelled"}
            or interrupted
            or (run["state"] == "running" and not task_running)
        )
        return {
            "run_id": run["id"],
            "state": run["state"],
            "can_resume": can_resume,
            "can_pause": run["state"] in {"running", "waiting"} and task_running,
        }

    async def ticket_detail(self, ticket_id: str) -> dict:
        ticket = await self.find_ticket(ticket_id)
        active = await self.active_run(ticket["id"])
        run = active or await self.latest_run(ticket["id"])
        attempts = await self.store.query("SELECT * FROM attempts WHERE run_id=? ORDER BY rowid LIMIT 500", (run["id"],)) if run else []
        for attempt in attempts:
            for name in ("result", "model", "member", "context"):
                attempt[name] = decode(attempt[name])
        return {"ticket":ticket,"run":run,"attempts":attempts,
            "execution":await self.execution_status(ticket["id"], active),
            "source_discussion":await self.discussion.source_for_ticket(ticket["id"]),
            "waits":await self.store.query("SELECT * FROM waits WHERE ticket_id=? AND status='pending' ORDER BY created",(ticket["id"],)),
            "jobs":await self.store.query("SELECT * FROM jobs WHERE ticket_id=? ORDER BY created DESC LIMIT 100",(ticket["id"],)),
            "checkpoints":await self.store.query("SELECT * FROM checkpoints WHERE ticket_id=? ORDER BY created DESC LIMIT 100",(ticket["id"],)),
            "integrations":await self.store.query("SELECT * FROM integrations WHERE ticket_id=? ORDER BY created DESC LIMIT 20",(ticket["id"],)),
            "dependencies":await self.store.query("SELECT t.* FROM tickets t JOIN dependencies d ON d.depends_on=t.id WHERE d.ticket_id=?",(ticket["id"],))}

    async def conversation(self, session_id: str, *, before: int | None = None, limit=200):
        limit = min(limit, 1000)
        if before is None:
            rows = await self.store.query(
                "SELECT * FROM messages WHERE session_id=? ORDER BY seq DESC LIMIT ?",
                (session_id, limit),
            )
        else:
            rows = await self.store.query(
                "SELECT * FROM messages WHERE session_id=? AND seq<? ORDER BY seq DESC LIMIT ?",
                (session_id, before, limit),
            )
        rows.reverse()
        return [{**r,"body":decode(r["body"])} for r in rows]

    async def start_ticket(self, ticket_id: str, *, allow_dirty_base=False) -> dict:
        self._require_execution_enabled()
        ticket = await self.find_ticket(ticket_id)
        async with self._ticket_lock(ticket["id"]):
            self._require_execution_enabled()
            existing = await self.latest_run(ticket["id"])
            if existing and existing["state"] not in {"failed","cancelled","integrated"}:
                return existing  # Idempotent even while awaiting review/integration.
            if ticket["state"] != "Ready" or ticket["archived"]:
                raise AgileCodeError("Only a non-archived Ready ticket can start.")
            unmet = await self.store.query("SELECT t.key FROM tickets t JOIN dependencies d ON t.id=d.depends_on WHERE d.ticket_id=? AND t.state<>'Done'",(ticket["id"],))
            if unmet:
                raise AgileCodeError("Incomplete dependencies: " + ", ".join(t["key"] for t in unmet))
            branch = await self.git.branch()
            base = await self.git.head()
            if await self.git.dirty() and not allow_dirty_base:
                raise AgileCodeError("The original checkout has uncommitted changes. Commit them, or explicitly start from committed HEAD; uncommitted changes are never copied into a ticket.")
            team = await self.config.get_team(ticket["team_ref"])
            team.process(ticket["process_id"])
            identity = uid()
            ticket_branch = f"agilecode/{ticket['key']}/{identity[:8]}"
            path = self.storage/"worktrees"/f"{ticket['key']}-{identity[:8]}"
            ticket_snapshot = dict(ticket)
            source_context = await self.discussion.context_for_ticket(ticket["id"])
            if source_context:
                ticket_snapshot["source_discussion_context"] = source_context
            provider = self.config.get_provider(ticket["provider_id"])
            snapshot = {"ticket":ticket_snapshot,"team":team.model_dump(mode="json"),
                "process_id":ticket["process_id"],"ticket_provider_id":ticket["provider_id"],
                "provider_id":provider.id,"dirty_base_acknowledged":allow_dirty_base}
            run = {"id":identity,"ticket_id":ticket["id"],"snapshot":encode(snapshot),"state":"provisioning","step_index":0,"outputs":"{}",
                "corrections":0,"correction":None,"workspace":str(path),"branch":ticket_branch,"base_branch":branch,"base_oid":base,
                "final_oid":None,"final_tree":None,"error":None,"created":now(),"updated":now()}
            async def provision():
                await self.store.insert("runs",run)
                try:
                    await self.git.add_worktree(path,ticket_branch,base)
                    def ready(c):
                        c.execute("UPDATE runs SET state='running',updated=? WHERE id=?",(now(),identity))
                        c.execute("UPDATE tickets SET state='In Progress',revision=revision+1,updated=? WHERE id=?",(now(),ticket["id"]))
                    await self.store.transaction(ready)
                    prepared = await self.store.get("runs",identity)
                    if self.all_paused:
                        await self.store.update("runs", identity, state="paused", error="Paused by user.", updated=now())
                    else:
                        runtime = self._runtime(prepared)
                        self.runtimes[identity] = runtime
                        self.tasks[identity] = asyncio.create_task(self._run(runtime),name=f"ticket-{ticket['key']}")
                except BaseException as exc:
                    await self.store.update("runs",identity,state="failed",error=str(exc),updated=now())
                    raise

            provisioning = asyncio.create_task(provision(), name=f"provision-{ticket['key']}")
            try:
                await asyncio.shield(provisioning)
            except asyncio.CancelledError:
                if self.all_paused:
                    # PM pause can cancel its start-ticket tool. Finish bounded
                    # worktree setup so Resume All receives a usable paused run.
                    await provisioning
                else:
                    provisioning.cancel()
                    await asyncio.gather(provisioning, return_exceptions=True)
                raise
            self.events.emit(Event("TicketChanged",ticket["id"],identity))
            return await self.store.get("runs",identity)

    def _runtime(self, run: dict, *, workspace: Path | None = None, phase="development") -> Runtime:
        workspace = workspace or Path(run["workspace"])
        scope = Workspace(workspace,self.root,self.global_root)
        shell = ShellService(scope,self.store,self.events,run["ticket_id"],run["id"],self.storage/"artifacts"/run["ticket_id"],event_phase=phase)
        runtime = Runtime(run["id"],scope,shell,McpService(self.config.settings.mcp_servers,workspace))
        return runtime

    async def _boundary(self, runtime: Runtime):
        await runtime.control.check()

    async def _run(self, runtime: Runtime):
        run_id = runtime.run_id
        try:
            run = await self.store.get("runs",run_id)
            if (runtime.shell.shell.process is None or runtime.shell.shell.process.returncode is not None
                    or runtime.prepared_shell_generation != runtime.shell.shell.generation):
                await runtime.control.check()
                from agilecode.adapters.environment import WorktreeEnvironment
                runtime.environment = await WorktreeEnvironment(runtime.shell, self.config.settings).prepare()
                runtime.prepared_shell_generation = runtime.shell.shell.generation
                await self.store.activity("ShellCreated", {"run_id": run_id, "environment": runtime.environment}, run["ticket_id"])
            # Provider/network loss is not a ticket failure. Resume the same durable
            # attempt after reconnection; previously completed tool calls are read
            # from storage and are never sent for execution a second time.
            reconnect_attempt = 0
            while True:
                await runtime.control.check()
                try:
                    await self.processor.run(runtime)
                    break
                except ModelUnavailable as exc:
                    reconnect_attempt += 1
                    run = await self.store.get("runs", run_id)
                    if run["state"] != "running":
                        raise
                    delay = min(2 ** min(reconnect_attempt, 5), 30)
                    message = f"Provider temporarily unavailable; retrying automatically in {delay}s: {exc}"
                    await self.store.update("runs", run_id, error=message, updated=now())
                    if reconnect_attempt == 1:
                        await self.store.activity("ModelConnectionInterrupted",
                                                  {"run_id": run_id, "message": str(exc)}, run["ticket_id"])
                        attempt = await self.store.one(
                            "SELECT session_id FROM attempts WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
                            (run_id,),
                        )
                        if attempt:
                            await self.store.message(attempt["session_id"], {
                                "role": "error", "content": message,
                            })
                            self.events.emit(Event("ConversationChanged", run["ticket_id"], run_id,
                                                   session_id=attempt["session_id"]))
                    self.events.emit(Event("RunChanged", run["ticket_id"], run_id,
                                           data={"message": message, "retrying": True}))
                    await asyncio.sleep(delay)
            run = await self.store.get("runs",run_id)
            if run["state"] == "completed" and run["final_oid"]:
                await self.prepare_integration(run["ticket_id"],wait=False)
        except GenerationStopped as exc:
            run = await self.store.get("runs",run_id)
            if run["state"] != "cancelled":
                await self.store.update("runs",run_id,state="paused",error=str(exc),updated=now())
            await self.store.execute("UPDATE attempts SET status='interrupted' WHERE run_id=? AND status='running'",(run_id,))
            self.events.emit(Event("RunChanged",run["ticket_id"],run_id,data={"message":str(exc)}))
        except asyncio.CancelledError:
            run = await self.store.get("runs",run_id)
            if run["state"] in {"running", "waiting"}:
                await self.store.update("runs",run_id,state="paused",error="Execution interrupted; see the current step conversation for incomplete tool calls.",updated=now())
            await self.store.execute("UPDATE attempts SET status='interrupted' WHERE run_id=? AND status IN ('running','waiting')",(run_id,))
        except Exception as exc:
            run = await self.store.get("runs",run_id)
            if run["state"] not in {"cancelled", "pause_requested"}:
                message = f"{type(exc).__name__}: {exc}"
                await self.store.update("runs",run_id,state="paused",error=message,updated=now())
                attempt = await self.store.one(
                    "SELECT session_id FROM attempts WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
                    (run_id,),
                )
                if attempt:
                    await self.store.message(attempt["session_id"], {
                        "role": "error", "content": message,
                    })
                    self.events.emit(Event("ConversationChanged", run["ticket_id"], run_id,
                                           session_id=attempt["session_id"]))
                await self.store.execute("UPDATE attempts SET status='interrupted' WHERE run_id=? AND status='running'",(run_id,))
                await self.store.activity("ExecutionInterrupted", {
                    "run_id": run_id, "exception": type(exc).__name__,
                    "message": str(exc), "traceback": traceback.format_exc(),
                }, run["ticket_id"])
                self.events.emit(Event("ExecutionInterrupted",run["ticket_id"],run_id,data={"message":message}))
        finally:
            run = await self.store.get("runs",run_id)
            self.events.emit(Event("TicketChanged",run["ticket_id"],run_id))

    async def wait_for_run(self, run_id: str):
        if run_id in self.tasks:
            await self.tasks[run_id]
        return await self.store.get("runs",run_id)

    async def stop_generation(self, ticket_id: str) -> dict:
        ticket = await self.find_ticket(ticket_id)
        run = await self.active_run(ticket["id"])
        if not run:
            raise AgileCodeError("Ticket has no active execution.")
        session_id = await self.processor.agent.stop_run(run["id"])
        return {"stopped": bool(session_id), "session_id": session_id, "run_id": run["id"]}

    def execution_controls(self) -> dict:
        return {"all_paused": self.all_paused}

    def _require_execution_enabled(self):
        if self.all_paused:
            raise AgileCodeError("Everything is paused. Use Resume All to continue.")

    async def _persist_execution_controls(self):
        await self.store.execute(
            "INSERT INTO meta(key,value) VALUES('execution_controls',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (encode({**self.execution_controls(), "deferred_integrations": sorted(self._deferred_integrations)}),),
        )
        self.events.emit(Event("ExecutionControlsChanged", data=self.execution_controls()))

    async def _defer_integration(self, ticket_id: str) -> dict:
        self._deferred_integrations.add(ticket_id)
        await self._persist_execution_controls()
        return await self.store.one(
            "SELECT * FROM integrations WHERE ticket_id=? ORDER BY created DESC LIMIT 1", (ticket_id,),
        ) or {"ticket_id": ticket_id, "state": "paused"}

    async def pause_all(self) -> dict:
        """Pause project management and every ticket, independently of board filters."""
        async with self._all_execution_lock:
            self.all_paused = True
            await self._persist_execution_controls()
            errors = []
            # Stop orchestration before ticket events can ask it to do more work.
            if self.project_manager.state.get("status") != "off":
                try:
                    await self.project_manager.pause()
                except Exception as exc:
                    errors.append(f"Project Manager: {exc}")

            async def pause_ticket(ticket):
                try:
                    async with self._ticket_lock(ticket["id"]):
                        return await self._pause_run(ticket["id"], active_only=True)
                except Exception as exc:
                    errors.append(f"{ticket['key']}: {exc}")
                    return False

            # Include tickets whose worktree start already holds their ticket lock.
            tickets = await self.store.query("SELECT id,key FROM tickets")
            paused = await asyncio.gather(*(pause_ticket(ticket) for ticket in tickets))
            try:
                self._deferred_integrations.update(await self.merges.pause_all())
            except Exception as exc:
                errors.append(f"Ticket integration: {exc}")
            await self._persist_execution_controls()
            return {**self.execution_controls(), "paused_tickets": sum(paused), "resumed_tickets": 0,
                    "tickets": sum(paused), "project_manager_status": self.project_manager.state.get("status"),
                    "errors": errors, "project_manager": await self.project_manager.snapshot()}

    async def resume_all(self) -> dict:
        """Resume paused executions, then let the PM observe the resumed project."""
        async with self._all_execution_lock:
            self.all_paused = False
            await self._persist_execution_controls()
            errors = []

            async def resume_ticket(ticket):
                try:
                    async with self._ticket_lock(ticket["id"]):
                        run = await self.active_run(ticket["id"])
                        if run and run["state"] == "paused":
                            await self._resume_run(ticket["id"])
                            return True
                    return False
                except Exception as exc:
                    errors.append(f"{ticket['key']}: {exc}")
                    return False

            tickets = await self.store.query("SELECT id,key FROM tickets WHERE state='In Progress' AND archived=0")
            resumed = await asyncio.gather(*(resume_ticket(ticket) for ticket in tickets))
            for ticket_id in tuple(self._deferred_integrations):
                try:
                    ticket = await self.store.one("SELECT id,key,state,archived FROM tickets WHERE id=?", (ticket_id,))
                    if ticket and not ticket["archived"] and ticket["state"] != "Done":
                        run = await self.latest_run(ticket_id)
                        if run and run["state"] == "completed" and run["final_oid"]:
                            await self.prepare_integration(ticket_id, wait=False, resume=True)
                    self._deferred_integrations.discard(ticket_id)
                except Exception as exc:
                    errors.append(f"Ticket integration {ticket_id}: {exc}")
            await self._persist_execution_controls()
            if self.project_manager.state.get("status") == "paused":
                try:
                    await self.project_manager.resume()
                except Exception as exc:
                    errors.append(f"Project Manager: {exc}")
            return {**self.execution_controls(), "paused_tickets": 0, "resumed_tickets": sum(resumed),
                    "tickets": sum(resumed), "project_manager_status": self.project_manager.state.get("status"),
                    "errors": errors, "project_manager": await self.project_manager.snapshot()}

    async def pause_run(self, ticket_id: str):
        ticket = await self.find_ticket(ticket_id)
        async with self._ticket_lock(ticket["id"]):
            await self._pause_run(ticket["id"])

    async def _pause_run(self, ticket_id: str, *, active_only=False):
        ticket = await self.find_ticket(ticket_id); run = await self.active_run(ticket["id"])
        if not run:
            if active_only:
                return False
            raise AgileCodeError("Ticket has no active execution.")
        runtime = self.runtimes.get(run["id"])
        if active_only and run["state"] not in {"running", "waiting", "pause_requested"}:
            # Stopping only generation can leave owned background jobs running.
            if run["state"] != "paused" or not runtime or not runtime.shell.has_live_jobs():
                return False
        if runtime:
            runtime.control.permit.clear()
        await self.store.execute(
            "UPDATE runs SET state='pause_requested',updated=? WHERE id=? "
            "AND state NOT IN ('completed','integrated','cancelled','failed')",
            (now(), run["id"]),
        )
        self.events.emit(Event("RunChanged",ticket["id"],run["id"]))
        task = self.tasks.get(run["id"])
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if runtime:
            await runtime.shell.close()
            await runtime.mcp.close()
        await self.store.execute("UPDATE jobs SET status='cancelled',finished=? WHERE run_id=? AND status IN ('starting','running')", (now(),run["id"]))
        current = await self.store.get("runs", run["id"])
        if current["state"] in {"completed", "integrated", "cancelled", "failed"}:
            if self.all_paused and current["state"] == "completed" and current["final_oid"]:
                await self._defer_integration(ticket["id"])
            return False
        await self.store.update("runs",run["id"],state="paused",error="Paused by user.",updated=now())
        self.events.emit(Event("ExecutionPaused",ticket["id"],run["id"]))
        self.events.emit(Event("RunChanged",ticket["id"],run["id"]))
        return True

    async def resume_run(self, ticket_id: str):
        """Resume the ticket's active execution, not an individual step/session."""
        self._require_execution_enabled()
        ticket = await self.find_ticket(ticket_id)
        async with self._ticket_lock(ticket["id"]):
            await self._resume_run(ticket["id"])

    async def _resume_run(self, ticket_id: str):
        self._require_execution_enabled()
        ticket = await self.find_ticket(ticket_id)
        run = await self.active_run(ticket["id"])
        if not run:
            raise AgileCodeError("This ticket has no active execution to resume.")
        runtime = self.runtimes.get(run["id"])
        if run["state"] in {"failed", "cancelled"} and runtime is not None:
            # Older builds may have closed/cancelled the runtime while leaving the
            # ticket In Progress. Recreate runtime services around the same run/worktree.
            await runtime.shell.close()
            await runtime.mcp.close()
            self.runtimes.pop(run["id"], None)
            runtime = None
        self._require_execution_enabled()
        await self.store.update("runs", run["id"], state="running", error=None, updated=now())

        if self.all_paused:
            await self.store.update("runs", run["id"], state="paused", error="Paused by user.", updated=now())
            return
        if runtime:
            runtime.control.cancelled = False
            runtime.control.permit.set()

        task = self.tasks.get(run["id"])
        if task and not task.done():
            self.events.emit(Event("RunChanged", ticket["id"], run["id"]))
            return
        if runtime is None:
            runtime = self._runtime(run)
            self.runtimes[run["id"]] = runtime
        self.tasks[run["id"]] = asyncio.create_task(
            self._run(runtime), name=f"ticket-{ticket['key']}-resume"
        )
        self.events.emit(Event("RunChanged", ticket["id"], run["id"]))

    async def cancel_run(self, ticket_id: str):
        ticket = await self.find_ticket(ticket_id); run = await self.active_run(ticket["id"])
        if not run:
            return
        runtime = self.runtimes.get(run["id"])
        if runtime:
            runtime.control.cancelled = True; runtime.control.permit.set()
        task = self.tasks.get(run["id"])
        if task and not task.done():
            task.cancel(); await asyncio.gather(task,return_exceptions=True)
        integration = self.integration_tasks.get(ticket["id"])
        if integration and not integration.done():
            integration.cancel(); await asyncio.gather(integration,return_exceptions=True)
        if runtime:
            await runtime.shell.close(); await runtime.mcp.close()
        await self.store.update("runs",run["id"],state="cancelled",updated=now())
        self.events.emit(Event("RunChanged",ticket["id"],run["id"]))

    async def _wait(self, context: StepContext, runtime: Runtime, tool_id: str, kind: str, prompt: str, binding: str) -> str:
        existing = await self.store.one("SELECT * FROM waits WHERE tool_id=? ORDER BY created DESC LIMIT 1",(tool_id,))
        if existing and existing["binding"] != binding:
            raise Conflict("Pending operation changed after it was presented.")
        if existing and existing["status"] == "resolved":
            return existing["answer"] or ""
        identity = existing["id"] if existing else uid()
        if not existing:
            await self.store.insert("waits",{"id":identity,"tool_id":tool_id,"ticket_id":context.ticket["id"],"session_id":context.attempt["session_id"],
                "kind":kind,"prompt":prompt,"binding":binding,"status":"pending","answer":None,"created":now()})
        await self.store.update("tools",tool_id,status="waiting")
        await self.store.update("attempts",context.attempt["id"],status="waiting")
        if context.phase == "development":
            await self.store.update("runs",context.run["id"],state="waiting",updated=now())
        future = asyncio.get_running_loop().create_future(); self.pending_answers[identity] = future
        self.events.emit(Event("QuestionRequested" if kind == "question" else "ApprovalRequested",context.ticket["id"],context.run["id"],context.attempt["id"],context.attempt["session_id"],{"id":identity,"prompt":prompt,"kind":kind}))
        try:
            # An answer might have been durably delivered just before the future
            # was installed; re-read rather than relying on a transient signal.
            saved = await self.store.get("waits",identity)
            answer = saved["answer"] if saved["status"] == "resolved" else await future
            await runtime.control.check()
            await self.store.update("attempts",context.attempt["id"],status="running")
            if context.phase == "development":
                await self.store.update("runs",context.run["id"],state="running",updated=now())
            return answer
        finally:
            self.pending_answers.pop(identity,None)

    async def answer(self, wait_id: str, answer: str):
        record = await self.store.get("waits",wait_id)
        if record["kind"] == "approval" and answer not in {"approve","deny"}:
            raise AgileCodeError("Approval answer must be approve or deny.")
        if not answer.strip() or len(answer)>100000:
            raise AgileCodeError("Provide a nonempty answer up to 100,000 characters.")
        if record["status"] == "resolved":
            if record["answer"] == answer:
                return
            raise Conflict("This question/approval already has a different recorded answer.")
        await self.store.update("waits",wait_id,status="resolved",answer=answer,resolved=now())
        future = self.pending_answers.get(wait_id)
        if future and not future.done():
            future.set_result(answer)
        else:
            run = await self.active_run(record["ticket_id"])
            # Recording an answer is not permission to restart an explicitly paused ticket.
            if run and run["state"] != "pause_requested" and run["error"] != "Paused by user.":
                await self.resume_run(record["ticket_id"])
        self.events.emit(Event("WaitResolved",record["ticket_id"],data={"id":wait_id}))

    async def submit_feedback(self,ticket_id: str,text: str,*,interrupt=False):
        text = text.strip()
        if not text:
            raise AgileCodeError("Enter a correction before sending it.")
        ticket = await self.find_ticket(ticket_id); run=await self.active_run(ticket["id"])
        if not run:
            raise AgileCodeError("There is no active ticket execution for feedback.")
        attempt=await self.store.one(
            "SELECT * FROM attempts WHERE run_id=? AND phase='development' ORDER BY created DESC,number DESC LIMIT 1",
            (run["id"],),
        )
        if not attempt:
            raise AgileCodeError("No step has started yet.")
        # Feedback is durable and delivered only at a safe model/tool boundary.
        # Include run identity so corrections cannot leak into another execution.
        await self.store.activity(
            "PendingFeedback",
            {"run_id":run["id"],"session_id":attempt["session_id"],"text":text},
            ticket["id"],
        )
        self.events.emit(Event("FeedbackQueued",ticket["id"],run["id"],attempt["id"],attempt["session_id"]))

        interrupted_session = None
        if interrupt:
            interrupted_session = await self.processor.agent.stop_run(run["id"])
            if interrupted_session:
                task = self.tasks.get(run["id"])
                if task and not task.done():
                    await task
                current = await self.store.get("runs",run["id"])
                if current["state"] == "paused":
                    await self.resume_run(ticket["id"])
        return {"queued":True,"interrupted":bool(interrupted_session),"session_id":attempt["session_id"]}

    async def prepare_integration(self,ticket_id: str,*,wait=True,rebuild=False,resume=False):
        ticket=await self.find_ticket(ticket_id)
        if self.all_paused:
            return await self._defer_integration(ticket["id"])
        task=self.integration_tasks.get(ticket["id"])
        if not task or task.done():
            task=asyncio.create_task(self.merges.prepare(ticket["id"],rebuild=rebuild,resume=resume),name=f"integrate-{ticket['key']}")
            self.integration_tasks[ticket["id"]]=task
            # Exceptions are surfaced to the UI even when this is invoked as the
            # automatic continuation of the standard development process.
            def report(completed):
                if not completed.cancelled() and completed.exception():
                    self.events.emit(Event("IntegrationBlocked",ticket["id"],data={"message":str(completed.exception())}))
            task.add_done_callback(report)
        return await task if wait else {"status":"preparing"}

    async def approve_integration(self,integration_id: str,expected_candidate: str,expected_target: str):
        return await self.merges.apply(integration_id,expected_candidate,expected_target)

    async def retry_cleanup(self,ticket_id: str):
        ticket=await self.find_ticket(ticket_id)
        integration=await self.store.one("SELECT * FROM integrations WHERE ticket_id=? AND state='integrated' ORDER BY created DESC LIMIT 1",(ticket["id"],))
        if not integration:
            raise AgileCodeError("No successful integration is available for cleanup.")
        return await self.merges.cleanup(integration["id"])

    async def checkpoint(self,ticket_id: str,label="User checkpoint"):
        ticket=await self.find_ticket(ticket_id); run=await self.latest_run(ticket["id"])
        if not run:
            raise AgileCodeError("No ticket worktree exists yet.")
        runtime=self.runtimes.get(run["id"])
        if runtime and runtime.shell.has_live_jobs():
            raise NeedsReview("Stop running jobs before taking a checkpoint.")
        if runtime:
            async with runtime.write_lock: return await self.checkpoints.capture(run,label)
        return await self.checkpoints.capture(run,label)

    async def restore_checkpoint(self,ticket_id: str,checkpoint_id: str,expected_tree: str):
        ticket=await self.find_ticket(ticket_id); run=await self.latest_run(ticket["id"])
        if not run or run["state"] not in {"paused","cancelled"} or run["final_oid"]:
            raise AgileCodeError("Restore requires a paused, uncommitted development run.")
        task=self.tasks.get(run["id"])
        if task and not task.done():
            task.cancel(); await asyncio.gather(task,return_exceptions=True)
        runtime=self.runtimes.get(run["id"])
        if runtime:
            await runtime.shell.close(); await runtime.mcp.close()
            self.runtimes.pop(run["id"],None)
        result=await self.checkpoints.restore(run,checkpoint_id,expected_tree)
        snapshot=decode(run["snapshot"]); team=Team.model_validate(snapshot["team"]); process=team.process(snapshot["process_id"])
        implementation=next((i for i,s in enumerate(process.steps) if s.kind=="implement"),0)
        outputs=decode(run["outputs"],{})
        for s in process.steps[implementation:]: outputs.pop(s.id,None)
        await self.store.execute("UPDATE attempts SET status='superseded' WHERE run_id=? AND status IN ('running','waiting','paused','interrupted')",(run["id"],))
        await self.store.update("runs",run["id"],state="paused",step_index=implementation,outputs=encode(outputs),correction="User restored a checkpoint. Reassess current files against the plan; rerun verification and review.",error=None,updated=now())
        self.events.emit(Event("CheckpointRestored",ticket["id"],run["id"],data=result))
        return result

    async def read_source(self,ticket_id: str,path: str):
        ticket=await self.find_ticket(ticket_id); run=await self.latest_run(ticket["id"])
        if run and run["final_oid"] and not Path(run["workspace"]).exists():
            from agilecode.adapters.workspace import decode_text, digest, MAX_FILE_BYTES, READ_LIMIT
            if not self.git.eligible(path, tracked=True):
                raise AgileCodeError("Invalid or protected committed source path.")
            rel = str(Path(path))
            size = await self.git.run("cat-file","-s",run["final_oid"]+":"+rel,check=False)
            if size.code or int(size.text)>MAX_FILE_BYTES:
                raise AgileCodeError("Committed source is missing or exceeds the text-file limit.")
            raw = (await self.git.run("show",run["final_oid"]+":"+rel)).stdout
            text, encoding = decode_text(raw)
            clipped = text.encode("utf-8")[:READ_LIMIT].decode("utf-8",errors="ignore")
            return {"path":rel,"scope":"workspace","hash":digest(raw),"size":len(raw),
                "encoding":encoding,"newline":"CRLF" if b"\r\n" in raw else "LF",
                "total_lines":len(text.splitlines()),"start_line":1,"end_line":len(clipped.splitlines()),
                "partial":clipped!=text,"content":clipped,"commit":run["final_oid"]}
        root=Path(run["workspace"]) if run and Path(run["workspace"]).exists() else self.root
        return await asyncio.to_thread(Workspace(root,self.root,self.global_root,readonly=root==self.root).read,path)

    async def save_source(self,ticket_id: str,path: str,content: str,expected_hash: str):
        ticket=await self.find_ticket(ticket_id); run=await self.latest_run(ticket["id"])
        if not run or run["state"] != "paused" or run["final_oid"]:
            raise AgileCodeError("Manual edits require a paused, uncommitted development run.")
        runtime=self.runtimes.get(run["id"])
        if runtime and runtime.shell.has_live_jobs():
            raise NeedsReview("Stop jobs before a manual edit.")
        task=self.tasks.get(run["id"])
        if task and not task.done():
            task.cancel(); await asyncio.gather(task,return_exceptions=True)
        scope=Workspace(Path(run["workspace"]),self.root,self.global_root)
        result=await asyncio.to_thread(scope.write,path,content,expected_hash,False)
        snapshot=decode(run["snapshot"]); team=Team.model_validate(snapshot["team"]); process=team.process(snapshot["process_id"])
        index=next((i for i,s in enumerate(process.steps) if s.kind=="implement"),0)
        outputs=decode(run["outputs"],{})
        for s in process.steps[index:]: outputs.pop(s.id,None)
        await self.store.execute("UPDATE attempts SET status='superseded' WHERE run_id=? AND status IN ('running','waiting','paused','interrupted')",(run["id"],))
        await self.store.update("runs",run["id"],step_index=index,outputs=encode(outputs),state="paused",correction="User manually edited source; reconcile with the plan and rerun verification/review.",updated=now())
        self.events.emit(Event("SourceChanged",ticket["id"],run["id"],data=result))
        return result

    async def delete_attempt(self, ticket_id: str, attempt_id: str) -> dict:
        """Discard this development attempt and later attempts, restoring its starting tree."""
        ticket = await self.find_ticket(ticket_id)
        async with self._ticket_lock(ticket["id"]):
            run = await self.latest_run(ticket["id"])
            if not run or run["final_oid"] or ticket["state"] == "Done":
                raise AgileCodeError("Only uncommitted, unintegrated ticket attempts can be deleted.")
            if run["state"] in {"integrated", "completed", "provisioning"}:
                raise AgileCodeError("This run can no longer be rewound.")
            if await self.store.one("SELECT 1 FROM integrations WHERE run_id=? LIMIT 1", (run["id"],)):
                raise AgileCodeError("Remove the integration candidate before rewinding this ticket.")
            integration_task = self.integration_tasks.get(ticket["id"])
            if integration_task and not integration_task.done():
                raise Conflict("Integration is running; do not rewind while it is changing the ticket.")

            history = await self.store.query(
                "SELECT rowid AS position, * FROM attempts WHERE run_id=? ORDER BY rowid",
                (run["id"],),
            )
            selected = next((row for row in history if row["id"] == attempt_id and row["phase"] == "development"), None)
            if not selected:
                raise AgileCodeError("This attempt is no longer part of the ticket.")
            discarded = [row for row in history if row["position"] >= selected["position"]]
            if any(row["phase"] != "development" for row in discarded):
                raise Conflict("Integration attempts cannot be removed through ticket rewind.")
            retained = [row for row in history if row["position"] < selected["position"]]
            saved = decode(selected["context"], {})
            target_tree = saved.get("start_tree")
            if not target_tree:
                raise AgileCodeError("The selected attempt has no saved starting tree.")
            snapshot = decode(run["snapshot"])
            process = Team.model_validate(snapshot["team"]).process(snapshot["process_id"])
            step_index = next((i for i, step in enumerate(process.steps) if step.id == selected["step_id"]), None)
            if step_index is None:
                raise Conflict("The attempt's process step no longer exists.")

            # Modern attempts carry exact identifiers. Legacy attempts have their
            # previous StepResult values, so recover the latest matching earlier ID.
            outputs = saved.get("output_attempts")
            if outputs is None:
                outputs = {}
                for step_id, value in saved.get("outputs", {}).items():
                    candidate = next((
                        old for old in reversed(retained)
                        if old["step_id"] == step_id and old["result"]
                        and decode(old["result"]) == value
                    ), None)
                    if candidate is None:
                        raise Conflict("Cannot reconstruct earlier handoff outputs safely.")
                    outputs[step_id] = candidate["id"]
            kept_ids = {row["id"] for row in retained}
            if any(value not in kept_ids for value in outputs.values()):
                raise Conflict("Saved handoff refers to an attempt that would be deleted.")

            corrections = saved.get("corrections")
            if corrections is None:
                discarded_corrections = sum(
                    bool(row["result"]
                         and decode(row["result"], {}).get("status") in {"FAIL", "CHANGES_REQUIRED"}
                         and next((step.correction_target for step in process.steps if step.id == row["step_id"]), None))
                    for row in discarded
                )
                corrections = max(0, run["corrections"] - discarded_corrections)

            await self.git.verify_worktree(Path(run["workspace"]), run["branch"])
            if await self.git.head(Path(run["workspace"])) != run["base_oid"]:
                raise Conflict("Ticket branch contains a commit; rewind cannot discard committed history.")
            await self.git.run("cat-file", "-e", f"{target_tree}^{{tree}}")

            # A running attempt may still have a shell command or model response.
            # Pause and wait for owned execution to end before touching its files.
            task = self.tasks.get(run["id"])
            if run["state"] in {"running", "waiting", "pause_requested"} or (task and not task.done()):
                await self._pause_run(ticket["id"])
            runtime = self.runtimes.pop(run["id"], None)
            self.tasks.pop(run["id"], None)
            if runtime:
                await runtime.shell.close()
                await runtime.mcp.close()
            run = await self.store.get("runs", run["id"])
            if run["final_oid"] or await self.git.head(Path(run["workspace"])) != run["base_oid"]:
                raise Conflict("The ticket was committed during pause; no files were discarded.")

            current_tree = await self.git.snapshot_tree(Path(run["workspace"]))
            await self.checkpoints.restore_tree(run, target_tree, current_tree)
            # Stage state is not part of the recorded tree; discard staged changes
            # while leaving the just-restored file contents intact.
            await self.git.run("reset", "--mixed", "HEAD", cwd=Path(run["workspace"]))
            if await self.git.snapshot_tree(Path(run["workspace"])) != target_tree:
                raise Conflict("Restored file state changed while resetting the index.")

            removed_ids = [row["id"] for row in discarded]
            removed_sessions = [row["session_id"] for row in discarded]
            placeholders = ",".join("?" for _ in removed_ids)
            session_marks = ",".join("?" for _ in removed_sessions)
            checkpoint_rows = await self.store.query(
                "SELECT * FROM checkpoints WHERE run_id=? AND created>=?",
                (run["id"], selected["created"]),
            )
            artifact_rows = await self.store.query(
                "SELECT * FROM artifacts WHERE ticket_id=? AND created>=?",
                (ticket["id"], selected["created"]),
            )
            tool_ids = {
                row["id"] for row in await self.store.query(
                    f"SELECT id FROM tools WHERE session_id IN ({session_marks})",
                    tuple(removed_sessions),
                )
            }
            job_ids = {
                row["id"] for row in await self.store.query(
                    f"SELECT id FROM jobs WHERE attempt_id IN ({placeholders})",
                    tuple(removed_ids),
                )
            }
            orphan_activities = []
            for row in await self.store.query("SELECT * FROM activity WHERE ticket_id=?", (ticket["id"],)):
                data = decode(row["data"], {})
                if not isinstance(data, dict):
                    continue
                if (data.get("attempt_id") in removed_ids
                    or data.get("session_id") in removed_sessions
                    or data.get("id") in tool_ids or data.get("id") in job_ids):
                    orphan_activities.append(row["id"])

            def erase(c):
                c.execute(f"DELETE FROM evidence WHERE attempt_id IN ({placeholders})", removed_ids)
                c.execute(f"DELETE FROM waits WHERE session_id IN ({session_marks})", removed_sessions)
                c.execute(f"DELETE FROM jobs WHERE attempt_id IN ({placeholders})", removed_ids)
                c.execute(f"DELETE FROM tools WHERE session_id IN ({session_marks})", removed_sessions)
                c.execute(f"DELETE FROM requests WHERE session_id IN ({session_marks})", removed_sessions)
                c.execute(f"DELETE FROM messages WHERE session_id IN ({session_marks})", removed_sessions)
                c.execute(f"DELETE FROM attempts WHERE id IN ({placeholders})", removed_ids)
                for row in checkpoint_rows:
                    c.execute("DELETE FROM checkpoints WHERE id=?", (row["id"],))
                for row in artifact_rows:
                    c.execute("DELETE FROM artifacts WHERE id=?", (row["id"],))
                for activity_id in orphan_activities:
                    c.execute("DELETE FROM activity WHERE id=?", (activity_id,))
                c.execute(
                    "UPDATE runs SET outputs=?,step_index=?,corrections=?,correction=?,"
                    "state='paused',error=?,updated=? WHERE id=?",
                    (encode(outputs), step_index, corrections, saved.get("correction_report"),
                     "Attempt deleted; ready to restart this step.", now(), run["id"]),
                )
            await self.store.transaction(erase)

            cleanup_warnings = []
            for checkpoint in checkpoint_rows:
                if checkpoint["oid"]:
                    response = await self.git.run("update-ref", "-d", checkpoint["ref"], checkpoint["oid"], check=False)
                    if response.code:
                        cleanup_warnings.append("Could not remove checkpoint ref: " + checkpoint["ref"])
            artifact_root = (self.storage / "artifacts" / ticket["id"]).resolve()
            for artifact in artifact_rows:
                path = Path(artifact["path"]).resolve()
                if inside(path, artifact_root) and path.is_file():
                    try:
                        path.unlink()
                    except OSError:
                        cleanup_warnings.append("Could not remove artifact: " + artifact["id"])
            self.events.emit(Event("TicketChanged", ticket["id"], run["id"]))
            return {"ticket": ticket["id"], "run": run["id"], "deleted": len(discarded),
                    "step_id": selected["step_id"], "step_index": step_index,
                    "cleanup_warnings": cleanup_warnings}

    async def step_diff(self, ticket_id: str, attempt_id: str) -> dict:
        ticket = await self.find_ticket(ticket_id)
        attempt = await self.store.one(
            "SELECT a.* FROM attempts a JOIN runs r ON r.id=a.run_id "
            "WHERE a.id=? AND r.ticket_id=?",
            (attempt_id, ticket["id"]),
        )
        if not attempt:
            raise AgileCodeError("Step attempt does not belong to this ticket.")
        if not attempt["result"]:
            raise AgileCodeError("Step has not completed.")
        start_tree = decode(attempt["context"], {}).get("start_tree")
        end_tree = attempt["fingerprint"]
        if not start_tree or not end_tree:
            raise AgileCodeError("Saved file states are unavailable for this step.")
        changed = start_tree != end_tree
        diff = ""
        if changed:
            diff = (await self.git.run(
                "diff", "--no-ext-diff", "--no-textconv", "--find-renames",
                start_tree, end_tree, "--",
            )).stdout.decode("utf-8", errors="replace")
        return {"ticket": ticket["id"], "attempt": attempt_id, "changed": changed, "text": diff}

    async def diff(self,ticket_id: str):
        ticket=await self.find_ticket(ticket_id); run=await self.latest_run(ticket["id"])
        if not run:
            return ""
        if Path(run["workspace"]).exists():
            return await self.git.diff(Path(run["workspace"]),run["base_oid"])
        if run["final_oid"]:
            return (await self.git.run("diff","--no-ext-diff",run["base_oid"],run["final_oid"],"--")).stdout.decode(errors="replace")
        return "Worktree unavailable; no final commit was recorded."

    async def backup(self, destination: str | Path):
        from .backup import create_backup
        return await create_backup(self, Path(destination))

    async def export_ticket(self, ticket_id: str, destination: str | Path):
        ticket = await self.find_ticket(ticket_id)
        detail = await self.ticket_detail(ticket["id"])
        detail["format"] = "agilecode-ticket-export"
        detail["version"] = 1
        detail["conversations"] = {}
        for attempt in detail.get("attempts", []):
            detail["conversations"][attempt["session_id"]] = await self.store.messages(attempt["session_id"], include_partial=True)
        path = Path(destination).expanduser().resolve()
        if path.exists(): raise AgileCodeError("Export destination exists; choose a new filename.")
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_text, json.dumps(detail, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"path": str(path)}

    async def job_output(self, job_id: str, offset=0, limit=100000):
        job = await self.store.get("jobs", job_id)
        artifact = await self.store.get("artifacts", job["artifact_id"])
        def read():
            with Path(artifact["path"]).open("rb") as handle:
                handle.seek(max(0, offset)); return handle.read(min(100000, max(1, limit)))
        data = await asyncio.to_thread(read)
        return {**job, "output": data.decode(errors="replace"), "offset": offset, "bytes": len(data),
                "partial": offset + len(data) < Path(artifact["path"]).stat().st_size}

    async def stop_job(self, job_id: str):
        job = await self.store.get("jobs", job_id)
        candidates = list(self.runtimes.values()) + list(self.merges.runtimes.values())
        for runtime in candidates:
            if runtime.run_id == job["run_id"]:
                try:
                    await runtime.shell.stop(job_id)
                    return {"stopped": job_id}
                except AgileCodeError: pass
        raise AgileCodeError("This process no longer owns that job; it is not a live managed process.")

    async def send_stdin(self, job_id: str, text: str):
        job = await self.store.get("jobs", job_id)
        for runtime in list(self.runtimes.values()) + list(self.merges.runtimes.values()):
            if runtime.run_id == job["run_id"]:
                await runtime.shell.send_input(job_id, text)
                return {"sent": True}
        raise AgileCodeError("No live owned shell for that job.")

    async def close(self):
        if self.closed: return
        # Stop project-level orchestration before changing any ticket runtime.
        await self.project_manager.shutdown()
        # Closing the host is explicit shutdown; UI replacement alone need not call this.
        for runtime in list(self.runtimes.values()):
            runtime.control.permit.set()
        for task in list(self.tasks.values())+list(self.integration_tasks.values()):
            if not task.done(): task.cancel()
        await asyncio.gather(*self.tasks.values(),*self.integration_tasks.values(),return_exceptions=True)
        for runtime in self.runtimes.values():
            await runtime.shell.close(); await runtime.mcp.close()
        await self.merges.close()
        await self.client.close()
        await self.store.close()
        if self.owner: self.owner.close()
        self.closed=True
