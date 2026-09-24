"""Qt adapter only: background asyncio host, JSON commands, and queued read models."""
from __future__ import annotations
import asyncio
import concurrent.futures
import json
import queue
import threading
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4
from PySide6.QtCore import QObject, Signal, Slot, QTimer
from agilecode.core.application import Application
from agilecode.core.types import Settings, Team, AgileCodeError
from agilecode.adapters.store import decode


class EngineThread(threading.Thread):
    def __init__(self,project,global_root,initialize,outbox):
        super().__init__(name="AgileCode-engine",daemon=True)
        self.project,self.global_root,self.initialize,self.outbox=project,global_root,initialize,outbox
        self.ready=threading.Event();self.stop_requested=threading.Event();self.loop=None;self.app=None
    def run(self):
        self.loop=asyncio.new_event_loop();asyncio.set_event_loop(self.loop)
        async def start():
            self.app=Application(self.project,self.global_root)
            self.app.events.subscribe(lambda event:self.outbox.put(("event",asdict(event))))
            notice=await self.app.open(initialize=self.initialize)
            self.outbox.put(("ready",notice))
        try:
            self.loop.run_until_complete(start());self.ready.set()
            if not self.stop_requested.is_set():
                self.loop.run_forever()
        except BaseException as exc:
            self.outbox.put(("fatal",str(exc)));self.ready.set()
        finally:
            if self.app:
                try:self.loop.run_until_complete(self.app.close())
                except Exception:pass
            for task in asyncio.all_tasks(self.loop):task.cancel()
            self.loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(self.loop),return_exceptions=True))
            self.loop.close()
    def stop(self):
        self.stop_requested.set()
        if self.loop and self.loop.is_running():
            try:
                # During early GUI startup the QML loader can fail before the
                # application object exists or before Application.open() completes.
                # Only request an asynchronous close after engine initialization.
                if self.ready.is_set() and self.app is not None:
                    future=asyncio.run_coroutine_threadsafe(self.app.close(),self.loop)
                    future.result(timeout=15)
            finally:
                self.loop.call_soon_threadsafe(self.loop.stop)
        self.join(timeout=5)


class Bridge(QObject):
    response = Signal(str,str,str)  # request id, action, JSON envelope
    event = Signal(str)            # JSON immutable event
    ready = Signal(str)
    fatal = Signal(str)
    def __init__(self,project,global_root=None,initialize=False):
        super().__init__()
        self.outbox=queue.Queue()
        self.worker=EngineThread(project,global_root,initialize,self.outbox)
        self.windows=[];self.dark=True
        self.timer=QTimer(self);self.timer.setInterval(40);self.timer.timeout.connect(self._poll);self.timer.start()
        self.worker.start()

    @Slot(str,str,result=str)
    def dispatch(self,action,payload="{}"):
        identity=uuid4().hex
        try:
            data=json.loads(payload or "{}")
            if action in {"start","resume","restore_checkpoint","merge","cleanup","prepare_merge"} and any(hasattr(w,"dirty") and w.dirty() for w in self.windows):
                raise AgileCodeError("Save or discard open source buffers before changing the execution/integration state.")
            if not self.worker.ready.is_set() or not self.worker.loop or not self.worker.loop.is_running():
                raise AgileCodeError("Project engine is not ready.")
            future=asyncio.run_coroutine_threadsafe(self._execute(action,data),self.worker.loop)
            def finished(f):
                try:result={"ok":True,"data":f.result()}
                except BaseException as exc:result={"ok":False,"error":str(exc) or type(exc).__name__}
                self.outbox.put(("response",(identity,action,result)))
            future.add_done_callback(finished)
        except Exception as exc:self.outbox.put(("response",(identity,action,{"ok":False,"error":str(exc)})))
        return identity

    async def _execute(self,action,d):
        app=self.worker.app
        if action=="snapshot":
            return {"project":str(app.root),"board":await app.board(d.get("query",""),include_archived=d.get("archived",False)),
                    "settings":app.config.settings.model_dump(mode="json"),"teams":await app.config.list_teams(),
                    "skills":await asyncio.to_thread(app.skills.catalog,["*"])}
        if action=="board":return await app.board(d.get("query",""),include_archived=d.get("archived",False))
        if action=="detail":return await app.ticket_detail(d["ticket"])
        if action=="create":
            ticket=await app.create_ticket(d["title"],d.get("description",""),team_ref=d.get("team_ref"),process_id=d.get("process_id"),merge_auto=d.get("merge_auto"))
            if d.get("ready"):ticket=await app.move_ticket(ticket["id"],"Ready")
            return ticket
        if action=="edit_ticket":return await app.edit_ticket(d.pop("ticket"),expected_revision=d.pop("revision"),**d)
        if action=="move":return await app.move_ticket(d["ticket"],d["state"],expected_revision=d.get("revision"),position=d.get("position"))
        if action=="start":return await app.start_ticket(d["ticket"],allow_dirty_base=d.get("allow_dirty_base",False))
        if action=="resume":await app.resume_run(d["ticket"]);return {"resumed":True}
        if action=="pause":await app.pause_run(d["ticket"]);return {"pause_requested":True}
        if action=="cancel":await app.cancel_run(d["ticket"]);return {"cancelled":True}
        if action=="archive":await app.archive_ticket(d["ticket"],d.get("archived",True));return {}
        if action=="dependency":await app.add_dependency(d["ticket"],(await app.find_ticket(d["depends_on"]))["id"]);return {}
        if action=="remove_dependency":await app.remove_dependency(d["ticket"],d["depends_on"]);return {}
        if action=="feedback":await app.submit_feedback(d["ticket"],d["text"]);return {}
        if action=="answer":await app.answer(d["id"],d["answer"]);return {}
        if action=="conversation":return {"session":d["session"],"messages":await app.conversation(d["session"],after=d.get("after",0))}
        if action=="request":
            rows=await app.store.query("SELECT * FROM requests WHERE session_id=? ORDER BY created",(d["session"],))
            return {"title":"Exact requests and prompts","text":json.dumps([{**r,"input":decode(r["input"])} for r in rows],indent=2),"language":"json"}
        if action=="settings":
            await app.config.save(Settings.model_validate(d["settings"]),d.get("scope","project"));return app.config.settings.model_dump(mode="json")
        if action=="reset_settings":
            await app.store.execute("DELETE FROM settings WHERE key='project'");await app.config.load();return app.config.settings.model_dump(mode="json")
        if action=="save_team":return {"ref":await app.config.save_team(Team.model_validate(d["team"]),d["scope"])}
        if action=="models":return await app.client.models(app.config.settings.profiles[app.config.settings.active_profile])
        if action=="prepare_merge":return await app.prepare_integration(d["ticket"],wait=False,rebuild=d.get("rebuild",False))
        if action=="merge":return await app.approve_integration(d["id"],d["candidate"],d["target"])
        if action=="cleanup":return await app.retry_cleanup(d["ticket"])
        if action=="checkpoint":return await app.checkpoint(d["ticket"],d.get("label","User checkpoint"))
        if action=="preview_checkpoint":
            ticket=await app.find_ticket(d["ticket"]);run=await app.latest_run(ticket["id"])
            return await app.checkpoints.preview(run,d["id"])
        if action=="restore_checkpoint":return await app.restore_checkpoint(d["ticket"],d["id"],d["expected_tree"])
        if action=="recovery":return await app.recovery_items()
        if action=="reconcile":await app.resolve_unknown(d["id"],d["kind"],d["explanation"],not_executed=d.get("not_executed",False));return {}
        if action=="job":return await app.job_output(d["id"],d.get("offset",0))
        if action=="stop_job":return await app.stop_job(d["id"])
        if action=="stdin":return await app.send_stdin(d["id"],d["text"])
        if action=="backup":return await app.backup(d["path"])
        if action=="export":return await app.export_ticket(d["ticket"],d["path"])
        if action=="discuss":return await app.discussion.ask(d["text"])
        if action=="discussion_history":return await app.conversation(app.discussion.session,limit=1000)
        if action=="code":
            data=await app.read_source(d["ticket"],d["path"])
            ticket=await app.find_ticket(d["ticket"]);run=await app.latest_run(ticket["id"])
            return {**data,"ticket":ticket["id"],"editable":bool(run and run["state"]=="paused" and not run["final_oid"] and not data["partial"])}
        if action=="save_code":return {**(await app.save_source(d["ticket"],d["path"],d["content"],d["hash"])),"view_id":d.get("view_id")}
        if action=="diff":return {"title":"Complete ticket diff","text":await app.diff(d["ticket"]),"language":"diff"}
        if action=="integration_diff":
            record=await app.store.get("integrations",d["id"])
            if not record["candidate_oid"]:raise AgileCodeError("The candidate does not have a validated commit yet.")
            text=(await app.git.run("diff","--no-ext-diff",record["target_oid"],record["candidate_oid"],"--")).stdout.decode(errors="replace")
            return {"title":"Integration candidate against exact target","text":text,"language":"diff"}
        if action=="compare_file":
            ticket=await app.find_ticket(d["ticket"]);run=await app.latest_run(ticket["id"])
            data=await app.read_source(ticket["id"],d["path"])
            if not run:raise AgileCodeError("This ticket has no source change yet.")
            old=await app.git.run("show",run["base_oid"]+":"+data["path"],check=False)
            from agilecode.core.diff import file_diff
            document = await asyncio.to_thread(file_diff,data["path"],old.stdout.decode(errors="replace") if old.code==0 else "",data["content"])
            document["partial"] = data["partial"]
            return document
        if action=="file_list":
            from agilecode.adapters.workspace import Workspace
            ticket=await app.find_ticket(d["ticket"]);run=await app.latest_run(ticket["id"])
            if run and run["final_oid"] and not Path(run["workspace"]).exists():
                entries=await app.git.tree_entries(run["final_oid"])
                names=sorted(p for p in entries if app.git.eligible(p,tracked=True))
                return {"ticket":ticket["id"],"files":[{"path":p,"directory":False,"symlink":entries[p][0]=="120000"} for p in names[:1000]],"partial":len(names)>1000}
            path=Path(run["workspace"]) if run and Path(run["workspace"]).exists() else app.root
            return {**(await asyncio.to_thread(Workspace(path,app.root,app.global_root).list_files,recursive=True,limit=1000)),"ticket":ticket["id"]}
        raise AgileCodeError("Unknown UI command: "+action)

    @Slot()
    def _poll(self):
        # Coalesce deltas per polling interval. Lifecycle messages remain ordered;
        # SQLite is authoritative if a view is closed or reconstructed.
        coalesced={}
        for _ in range(512):
            try:kind,value=self.outbox.get_nowait()
            except queue.Empty:break
            if kind=="event" and value["kind"] in {"ModelTextDelta","ModelReasoningDelta"}:
                key=(value["kind"],value["session_id"],value["data"].get("request_id"))
                if key in coalesced:coalesced[key]["data"]["value"]+=value["data"]["value"]
                else:coalesced[key]=value
                continue
            if kind=="ready":self.ready.emit(json.dumps(value))
            elif kind=="fatal":self.fatal.emit(value)
            elif kind=="event":self.event.emit(json.dumps(value,ensure_ascii=False))
            elif kind=="response":
                identity,action,envelope=value
                if envelope["ok"] and action in {"snapshot","settings","reset_settings"}:
                    settings = envelope["data"].get("settings",envelope["data"])
                    self.dark = settings.get("theme","dark") == "dark"
                if envelope["ok"] and action in {"code","compare_file","diff","integration_diff","request"}:
                    self._show_document(action,envelope["data"])
                if envelope["ok"] and action=="save_code":
                    for window in self.windows:
                        if hasattr(window,"saved") and getattr(window,"view_id",None)==envelope["data"].get("view_id"):window.saved(envelope["data"])
                self.response.emit(identity,action,json.dumps(envelope,ensure_ascii=False,default=str))
        for event in coalesced.values():self.event.emit(json.dumps(event,ensure_ascii=False))

    def _show_document(self,action,data):
        from .codeview import CodeWindow, DiffWindow
        dark=self.dark
        if action=="compare_file":window=DiffWindow(data,dark=dark)
        elif action=="code":
            window=CodeWindow(data["path"],data["content"],dark=dark,editable=data["editable"],file_hash=data["hash"])
            window.view_id=uuid4().hex
            def save(text,hash_):self.dispatch("save_code",json.dumps({"ticket":data["ticket"],"path":data["path"],"content":text,"hash":hash_,"view_id":window.view_id}))
            window.save_requested.connect(save)
        else:
            text=data["text"]
            if len(text)>600000: text=text[:600000]+"\n\n[Viewer capped at 600,000 characters; full records remain in SQLite/export.]"
            window=CodeWindow(data["title"],text,dark=dark,language=data.get("language"))
        self.windows.append(window);window.show()

    def close(self):
        self.timer.stop()
        self.worker.stop()
        for window in self.windows:window.close()
