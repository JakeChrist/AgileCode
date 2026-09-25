"""Public headless application API. Desktop and CLI are replaceable clients."""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .types import (AgileCodeError, AccessDenied, Conflict, NeedsReview, GenerationStopped, BoardState, Settings, Team,
                    StepResult, StepContext, Events, Event, uid, now)
from .runner import Runtime, ProcessRunner, RunControl
from agilecode.adapters.store import ProjectStore, encode, decode
from agilecode.adapters.config import Configuration
from agilecode.adapters.lock import ProjectLock
from agilecode.adapters.git import GitRepository
from agilecode.adapters.workspace import Workspace, digest
from agilecode.adapters.shell import ShellService
from agilecode.adapters.model import OpenAICompatibleClient
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
        self.pending_answers: dict[str, asyncio.Future] = {}
        self.owner: ProjectLock | None = None
        self.opened = False
        self.closed = False
        self.processor = ProcessRunner(self.store, self.git, self.client, self.skills, self.checkpoints,
                                       self.events, lambda:self.config.settings, self._wait, self._boundary)
        from .integration import MergeCoordinator
        self.merges = MergeCoordinator(self)
        from .discussion import ProjectDiscussion
        self.discussion = ProjectDiscussion(self)

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
            await self._recover()
            self.opened = True
            return {"project": str(self.root), "initialized": not exists, "gitignore_changed": ignored_changed,
                    "notice": "Commit the .gitignore change before starting a clean-base ticket." if ignored_changed else ""}
        except BaseException:
            await self.store.close()
            self.owner.close()
            raise

    async def _install_builtin_skills(self):
        source = Path(__file__).resolve().parents[1]/"resources"/"skills"
        if source.exists():
            for folder in source.iterdir():
                if folder.is_dir() and (folder/"SKILL.md").exists():
                    target = self.global_root/"skills"/folder.name
                    if not target.exists():
                        await asyncio.to_thread(shutil.copytree, folder, target)

    async def _recover(self):
        # A single engine owns the project. No inference or unknown external action
        # is automatically replayed at startup.
        await self.store.execute("UPDATE requests SET status='interrupted',error='Engine stopped during response',finished=? WHERE status='streaming'", (now(),))
        await self.store.execute("UPDATE jobs SET status='unknown' WHERE status IN ('running','starting')")
        await self.store.execute("UPDATE tools SET status='unknown' WHERE status='running' AND category IN ('write','destructive','execute','mcp','commit')")
        await self.store.execute("UPDATE tools SET status='pending' WHERE status='running'")
        await self.store.execute("UPDATE attempts SET status='interrupted' WHERE status='running'")
        await self.store.execute("UPDATE runs SET state='paused',error='Engine restarted. Shell state was lost; review recovery before resuming.' WHERE state IN ('running','pause_requested')")
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
                    for tool in await self.store.query("SELECT tools.* FROM tools JOIN attempts ON tools.session_id=attempts.session_id WHERE attempts.run_id=? AND tools.name='final_commit' AND tools.status='unknown'", (run["id"],)):
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

    def _ticket_lock(self, ticket_id):
        return self.ticket_locks.setdefault(ticket_id, asyncio.Lock())

    async def create_ticket(self, title: str, description: str, *, team_ref: str | None = None,
                            process_id: str | None = None, parent_id: str | None = None,
                            merge_auto: bool | None = None, source_discussion_id: str | None = None,
                            request_id: str | None = None) -> dict:
        if not title.strip() or len(title)>200 or len(description)>100000:
            raise AgileCodeError("Provide a title (1–200 characters) and a description up to 100,000 characters.")
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
        source_discussion = await self.discussion.get(source_discussion_id) if source_discussion_id else None
        identity, timestamp = uid(), now()
        def create(c):
            row = c.execute("SELECT value FROM meta WHERE key='ticket_sequence'").fetchone()
            number = int(row[0])+1 if row else 1
            c.execute("INSERT INTO meta VALUES('ticket_sequence',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(number),))
            c.execute("INSERT INTO tickets(id,key,title,description,state,team_ref,process_id,parent_id,merge_auto,position,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (identity, f"T-{number:03d}", title.strip(), description, "Backlog", reference, process_id, parent_id,
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
        allowed = {"title", "description", "team_ref", "process_id", "priority", "merge_auto"}
        if not changes.keys() <= allowed:
            raise AgileCodeError("Unsupported ticket field.")
        ticket = await self.find_ticket(ticket_id)
        if ticket["state"] == "In Progress" and changes.keys() & {"description", "team_ref", "process_id"}:
            raise AgileCodeError("Ticket/process inputs are frozen during execution. Cancel and start a revised run to change them.")
        team = await self.config.get_team(changes.get("team_ref",ticket["team_ref"]))
        if changes.get("process_id",ticket["process_id"]) not in {p.id for p in team.processes}:
            raise AgileCodeError("The selected process does not belong to this team.")
        if not changes:
            return ticket
        sql = "UPDATE tickets SET " + ",".join(f"{k}=?" for k in changes) + ",revision=revision+1,updated=? WHERE id=? AND revision=?"
        count = await self.store.execute(sql, (*changes.values(),now(),ticket["id"],expected_revision))
        if count != 1:
            raise Conflict("Ticket changed since it was opened. Refresh before saving.")
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
        can_resume = (
            run["state"] in {"paused", "pause_requested", "failed", "cancelled"}
            or interrupted
            or (run["state"] == "running" and not task_running)
        )
        return {
            "run_id": run["id"],
            "state": run["state"],
            "can_resume": can_resume,
            "can_pause": run["state"] == "running" and task_running,
        }

    async def ticket_detail(self, ticket_id: str) -> dict:
        ticket = await self.find_ticket(ticket_id)
        active = await self.active_run(ticket["id"])
        run = active or await self.latest_run(ticket["id"])
        attempts = await self.store.query("SELECT * FROM attempts WHERE run_id=? ORDER BY created,number LIMIT 500", (run["id"],)) if run else []
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

    async def conversation(self, session_id: str, *, after=0, limit=200):
        rows = await self.store.query("SELECT * FROM messages WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?", (session_id,after,min(limit,1000)))
        return [{**r,"body":decode(r["body"])} for r in rows]

    async def start_ticket(self, ticket_id: str, *, allow_dirty_base=False) -> dict:
        ticket = await self.find_ticket(ticket_id)
        async with self._ticket_lock(ticket["id"]):
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
            snapshot = {"ticket":ticket_snapshot,"team":team.model_dump(mode="json"),"process_id":ticket["process_id"],"dirty_base_acknowledged":allow_dirty_base}
            run = {"id":identity,"ticket_id":ticket["id"],"snapshot":encode(snapshot),"state":"provisioning","step_index":0,"outputs":"{}",
                "corrections":0,"correction":None,"workspace":str(path),"branch":ticket_branch,"base_branch":branch,"base_oid":base,
                "final_oid":None,"final_tree":None,"error":None,"created":now(),"updated":now()}
            await self.store.insert("runs",run)
            try:
                await self.git.add_worktree(path,ticket_branch,base)
                def ready(c):
                    c.execute("UPDATE runs SET state='running',updated=? WHERE id=?",(now(),identity))
                    c.execute("UPDATE tickets SET state='In Progress',revision=revision+1,updated=? WHERE id=?",(now(),ticket["id"]))
                await self.store.transaction(ready)
                runtime = self._runtime(await self.store.get("runs",identity))
                self.runtimes[identity] = runtime
                self.tasks[identity] = asyncio.create_task(self._run(runtime),name=f"ticket-{ticket['key']}")
            except BaseException as exc:
                await self.store.update("runs",identity,state="failed",error=str(exc),updated=now())
                raise
            self.events.emit(Event("TicketChanged",ticket["id"],identity))
            return await self.store.get("runs",identity)

    def _runtime(self, run: dict, *, workspace: Path | None = None, phase="development") -> Runtime:
        workspace = workspace or Path(run["workspace"])
        scope = Workspace(workspace,self.root,self.global_root)
        shell = ShellService(scope,self.store,self.events,run["ticket_id"],run["id"],self.storage/"artifacts"/run["ticket_id"],event_phase=phase)
        runtime = Runtime(run["id"],scope,shell,McpService(self.config.settings.mcp_servers,workspace))
        async def acknowledge_pause():
            if phase == "development":
                await self.store.update("runs",run["id"],state="paused",updated=now())
                self.events.emit(Event("ExecutionPaused",run["ticket_id"],run["id"]))
        runtime.control.on_pause = acknowledge_pause
        return runtime

    async def _boundary(self, runtime: Runtime):
        if not runtime.control.permit.is_set():
            run = await self.store.get("runs",runtime.run_id)
            await self.store.update("runs",runtime.run_id,state="paused",updated=now())
            self.events.emit(Event("RunChanged",run["ticket_id"],run["id"]))
        await runtime.control.check()

    async def _run(self, runtime: Runtime):
        run_id = runtime.run_id
        try:
            run = await self.store.get("runs",run_id)
            if runtime.shell.shell.generation == 0:
                await runtime.shell.shell.start()
                await self.store.activity("ShellCreated",{"run_id":run_id,"notice":"New shell; prior volatile state is not restored."},run["ticket_id"])
                for command in self.config.settings.setup_commands:
                    # Declared setup commands were explicitly saved by the user, but
                    # still pass the worktree guard. They are recorded as actual jobs.
                    result = await runtime.shell.run(
                        command, None,
                        timeout=self.config.settings.shell_command_timeout_seconds,
                        approved_unknown=True,
                    )
                    if result["exit_code"] != 0:
                        raise AgileCodeError("A configured shell setup command failed: " + command)
            await self.processor.run(runtime)
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
            if run["state"] != "cancelled":
                await self.store.update("runs",run_id,state="paused",error="Execution interrupted; inspect any unknown tool/job outcomes before resuming.",updated=now())
            await self.store.execute("UPDATE attempts SET status='interrupted' WHERE run_id=? AND status='running'",(run_id,))
        except Exception as exc:
            run = await self.store.get("runs",run_id)
            await self.store.update("runs",run_id,state="paused",error=str(exc),updated=now())
            await self.store.execute("UPDATE attempts SET status='interrupted' WHERE run_id=? AND status='running'",(run_id,))
            self.events.emit(Event("RecoveryRequired",run["ticket_id"],run_id,data={"message":str(exc)}))
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

    async def pause_run(self, ticket_id: str):
        ticket = await self.find_ticket(ticket_id); run = await self.active_run(ticket["id"])
        if not run:
            raise AgileCodeError("Ticket has no active execution.")
        runtime = self.runtimes.get(run["id"])
        if runtime:
            runtime.control.permit.clear()
        await self.store.update("runs",run["id"],state="pause_requested" if runtime and self.tasks.get(run["id"]) and not self.tasks[run["id"]].done() else "paused",updated=now())
        self.events.emit(Event("RunChanged",ticket["id"],run["id"]))

    async def resume_run(self, ticket_id: str):
        """Resume the ticket's active execution, not an individual step/session."""
        ticket = await self.find_ticket(ticket_id)
        run = await self.active_run(ticket["id"])
        if not run:
            raise AgileCodeError("This ticket has no active execution to resume.")
        unknown = await self.store.query(
            "SELECT tools.id FROM tools JOIN attempts ON tools.session_id=attempts.session_id "
            "WHERE attempts.run_id=? AND tools.status='unknown'", (run["id"],)
        )
        unknown_jobs = await self.store.query(
            "SELECT id FROM jobs WHERE run_id=? AND status='unknown'", (run["id"],)
        )
        if unknown or unknown_jobs:
            raise NeedsReview("Unknown operations/jobs need explicit reconciliation in Recovery before resuming.")

        runtime = self.runtimes.get(run["id"])
        if run["state"] in {"failed", "cancelled"} and runtime is not None:
            # Older builds may have closed/cancelled the runtime while leaving the
            # ticket In Progress. Recreate runtime services around the same run/worktree.
            self.runtimes.pop(run["id"], None)
            runtime = None
        if runtime:
            runtime.control.cancelled = False
            runtime.control.permit.set()
        await self.store.update("runs", run["id"], state="running", error=None, updated=now())

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
            await self.resume_run(record["ticket_id"])
        self.events.emit(Event("WaitResolved",record["ticket_id"],data={"id":wait_id}))

    async def submit_feedback(self,ticket_id: str,text: str):
        ticket = await self.find_ticket(ticket_id); run=await self.latest_run(ticket["id"])
        if not run:
            raise AgileCodeError("There is no active session for feedback.")
        attempt=await self.store.one("SELECT * FROM attempts WHERE run_id=? ORDER BY created DESC LIMIT 1",(run["id"],))
        if not attempt:
            raise AgileCodeError("No step has started yet.")
        # Feedback must wait until any pending tool exchange is complete. It is
        # stored as an activity item and drained by the runner at a safe boundary.
        await self.store.activity("PendingFeedback",{"session_id":attempt["session_id"],"text":text},ticket["id"])
        self.events.emit(Event("FeedbackQueued",ticket["id"],run["id"],attempt["id"],attempt["session_id"]))

    async def prepare_integration(self,ticket_id: str,*,wait=True,rebuild=False):
        ticket=await self.find_ticket(ticket_id)
        task=self.integration_tasks.get(ticket["id"])
        if not task or task.done():
            task=asyncio.create_task(self.merges.prepare(ticket["id"],rebuild=rebuild),name=f"integrate-{ticket['key']}")
            self.integration_tasks[ticket["id"]]=task
            # Exceptions are surfaced to the UI even when this is invoked as the
            # automatic continuation of the standard development process.
            def report(completed):
                if not completed.cancelled() and completed.exception():
                    self.events.emit(Event("RecoveryRequired",ticket["id"],data={"message":str(completed.exception())}))
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

    async def diff(self,ticket_id: str):
        ticket=await self.find_ticket(ticket_id); run=await self.latest_run(ticket["id"])
        if not run:
            return ""
        if Path(run["workspace"]).exists():
            return await self.git.diff(Path(run["workspace"]),run["base_oid"])
        if run["final_oid"]:
            return (await self.git.run("diff","--no-ext-diff",run["base_oid"],run["final_oid"],"--")).stdout.decode(errors="replace")
        return "Worktree unavailable; no final commit was recorded."

    async def recovery_items(self):
        return {"tools":await self.store.query("SELECT * FROM tools WHERE status='unknown'"),
                "jobs":await self.store.query("SELECT * FROM jobs WHERE status='unknown'"),
                "operations":await self.store.query("SELECT * FROM operations WHERE status='prepared'")}

    async def resolve_unknown(self,identity: str,kind: str,explanation: str,*,not_executed=False):
        if len(explanation.strip())<8:
            raise AgileCodeError("Record what you inspected and established before reconciling an unknown outcome.")
        if kind=="tool":
            tool=await self.store.get("tools",identity)
            if tool["status"]!="unknown": raise Conflict("Tool no longer has an unknown outcome.")
            if not_executed:
                await self.store.update("tools",identity,status="pending")
            else:
                await self.store.update("tools",identity,status="completed",result=encode({"status":"error","_execution_id":identity,
                    "error":{"code":"user_reconciled","message":explanation+". Reinspect the actual state before continuing."}}),finished=now())
        elif kind=="job":
            job=await self.store.get("jobs",identity)
            if job["status"]!="unknown": raise Conflict("Job no longer has an unknown outcome.")
            await self.store.update("jobs",identity,status="reconciled",finished=now())
        else:
            raise AgileCodeError("Reconciliation kind must be tool or job.")
        await self.store.activity("UserReconciliation",{"id":identity,"kind":kind,"explanation":explanation,"not_executed":not_executed})

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
        raise AgileCodeError("This process no longer owns that job; inspect recovery rather than killing a reused PID.")

    async def send_stdin(self, job_id: str, text: str):
        job = await self.store.get("jobs", job_id)
        for runtime in list(self.runtimes.values()) + list(self.merges.runtimes.values()):
            if runtime.run_id == job["run_id"]:
                await runtime.shell.send_input(job_id, text)
                return {"sent": True}
        raise AgileCodeError("No live owned shell for that job.")

    async def close(self):
        if self.closed: return
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
