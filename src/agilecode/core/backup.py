"""Portable backup archive: consistent SQLite snapshot, artifacts, and Git bundle.

Restoration deliberately requires a new directory. It never overwrites a project.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from .types import AgileCodeError, now, uid
from agilecode.adapters.store import decode, encode
from agilecode.adapters.git import GitRepository


async def create_backup(app, destination: Path) -> dict:
    if app.project_manager.state.get("status") == "running":
        raise AgileCodeError("Pause the Project Manager before creating a backup.")
    if any(not t.done() for t in list(app.tasks.values())+list(app.integration_tasks.values())):
        raise AgileCodeError("Backup requires an idle engine. Stop the current run or close/reopen the application before backing up interrupted work.")
    if any(r.shell.has_live_jobs() for r in app.runtimes.values()):
        raise AgileCodeError("Stop background jobs before backup.")
    if await app.git.dirty():
        raise AgileCodeError("Commit original-checkout changes before backup; this archive does not capture your unrelated uncommitted original files.")
    destination = destination.expanduser().resolve()
    if destination.exists(): raise AgileCodeError("Backup destination already exists; choose a new filename.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agilecode-backup-") as temporary:
        temp = Path(temporary)
        restores = {}
        for run in await app.store.query("SELECT * FROM runs WHERE state<>'integrated'"):
            if Path(run["workspace"]).exists() and not run["final_oid"]:
                checkpoint = await app.checkpoints.capture(run,"Portable backup recovery point")
                restores[run["id"]] = checkpoint["id"]
        await app.store.backup(temp/"agilecode.db")
        await app.git.run("bundle","create",str(temp/"repository.bundle"),"--all")
        manifest = {"format":"agilecode-backup","version":1,"created":now(),"project":str(app.root),
                    "active_branch":await app.git.branch(),"global_root":str(app.global_root),
                    "restore_checkpoints":restores,"files":{},"global_teams":[],"skills":[]}
        members = [(temp/"agilecode.db","agilecode.db"),(temp/"repository.bundle","repository.bundle")]
        for folder, prefix in (
            (app.storage/"artifacts","artifacts"),
            (app.storage/"skills","project_skills"),
            (app.root/".agile_code"/"project_manager","project_manager"),
            (app.global_root/"teams","global_teams"),
            (app.global_root/"skills","global_skills"),
        ):
            if folder.exists():
                for path in sorted(folder.rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        members.append((path,str(Path(prefix)/path.relative_to(folder))))
        staging = destination.with_name(destination.name+".partial")
        try:
            with zipfile.ZipFile(staging,"w",zipfile.ZIP_DEFLATED) as archive:
                for path,name in members:
                    content = await asyncio.to_thread(path.read_bytes)
                    manifest["files"][name] = {"bytes":len(content),"sha256":hashlib.sha256(content).hexdigest()}
                    archive.writestr(name,content)
                archive.writestr("manifest.json",json.dumps(manifest,indent=2))
            staging.replace(destination)
        finally:
            staging.unlink(missing_ok=True)
    return {"path":str(destination),"files":len(members),"recovery_checkpoints":restores,
            "note":"Includes local committed Git history, private refs, database and artifacts. API key values, original uncommitted files, caches and shell memory are excluded."}


async def restore_backup(source: Path, destination: Path, global_root: Path | None = None) -> dict:
    """Restore to a fresh path. Repository-local hooks/config are intentionally not imported."""
    destination = destination.expanduser().resolve()
    if destination.exists(): raise AgileCodeError("Restore destination must not exist.")
    global_root = (global_root or Path.home()/".agilecode").expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="agilecode-restore-") as temporary:
        temp=Path(temporary)
        with zipfile.ZipFile(source) as archive:
            manifest=json.loads(archive.read("manifest.json"))
            if manifest.get("format")!="agilecode-backup" or manifest.get("version")!=1:
                raise AgileCodeError("Unsupported backup format.")
            total=0
            for name,meta in manifest["files"].items():
                rel=Path(name)
                if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                    raise AgileCodeError("Invalid archive member path.")
                info=archive.getinfo(name); total+=info.file_size
                if total>10*1024**3 or info.file_size!=meta["bytes"]:
                    raise AgileCodeError("Archive size is invalid or exceeds the 10 GiB restore limit.")
                content=archive.read(name)
                if hashlib.sha256(content).hexdigest()!=meta["sha256"]:
                    raise AgileCodeError("Backup hash mismatch: "+name)
                path=temp/rel; path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
        # Verify before creating the requested destination.
        async def call(*args,cwd=None):
            p=await asyncio.create_subprocess_exec(*args,cwd=cwd,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            out,err=await p.communicate()
            if p.returncode: raise AgileCodeError(err.decode(errors="replace"))
            return out.decode().strip()
        destination.mkdir(parents=True)
        await call("git","init","-b","agilecode-restore-"+uid(),str(destination))
        repo=GitRepository(destination)
        await repo.run("bundle","verify",str(temp/"repository.bundle"))
        # Empty destination ensures no existing refs can be overwritten.
        await repo.run("fetch",str(temp/"repository.bundle"),"refs/*:refs/*")
        await repo.run("checkout",manifest["active_branch"])
        storage=destination/".agilecode";storage.mkdir(exist_ok=True)
        shutil.copy2(temp/"agilecode.db",storage/"agilecode.db")
        for prefix,target in (("artifacts",storage/"artifacts"),("project_skills",storage/"skills")):
            if (temp/prefix).exists(): shutil.copytree(temp/prefix,target,dirs_exist_ok=True)
        if (temp/"project_manager").exists():
            pm_target=destination/".agile_code"/"project_manager"
            shutil.copytree(temp/"project_manager",pm_target,dirs_exist_ok=True)
            state_path=pm_target/"state.json"
            if state_path.exists():
                state=json.loads(state_path.read_text(encoding="utf-8"))
                state["status"]="paused"
                state["current_activity"]="Paused after backup restore"
                state_path.write_text(json.dumps(state,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
        # Recovered global bundles are offered as files, not silently installed over
        # the user's existing global settings or enabled MCP configuration.
        for prefix in ("global_teams","global_skills"):
            if (temp/prefix).exists(): shutil.copytree(temp/prefix,storage/"recovered_global"/prefix)
        connection=sqlite3.connect(storage/"agilecode.db")
        connection.row_factory=sqlite3.Row
        identity=await repo.identify()
        old=str(Path(manifest["project"])/".agilecode")
        for table,field in (("runs","workspace"),("integrations","workspace"),("artifacts","path")):
            for row in connection.execute(f"SELECT id,{field} FROM {table}").fetchall():
                previous=row[field]
                if previous and (previous==old or previous.startswith(old+"/")):
                    replacement=str(storage)+previous[len(old):]
                    connection.execute(f"UPDATE {table} SET {field}=? WHERE id=?",(replacement,row["id"]))
        prior=json.loads(connection.execute("SELECT value FROM meta WHERE key='project'").fetchone()[0])
        identity["id"]=prior["id"]
        connection.execute("UPDATE meta SET value=? WHERE key='project'",(json.dumps(identity),))
        connection.execute("UPDATE runs SET state='paused',error='Restored backup: inspect model settings, tools and shell setup before resume.' WHERE state NOT IN ('completed','integrated','failed','cancelled')")
        connection.execute("UPDATE attempts SET status='interrupted' WHERE status IN ('running','waiting')")
        connection.execute("UPDATE integrations SET state='blocked',error='Rebuild candidate after backup restoration.' WHERE state<>'integrated'")
        # Disable external servers until the operator reviews paths and credentials.
        settings=connection.execute("SELECT value FROM settings WHERE key='project'").fetchone()
        data = json.loads(settings[0]) if settings else {}
        data["merge_on_completion"] = False
        # An explicit empty project list also prevents inheriting enabled servers
        # from this machine's global settings when no project override existed.
        data.setdefault("mcp_servers", [])
        for config in data["mcp_servers"]:
            config["enabled"] = False
        connection.execute("INSERT INTO settings(key,value) VALUES('project',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(data),))
        connection.execute("UPDATE tickets SET merge_auto=0 WHERE state<>'Done'")
        connection.commit()
        runs=[dict(r) for r in connection.execute("SELECT * FROM runs WHERE state<>'integrated'")]
        for run in runs:
            path=Path(run["workspace"])
            # Branch exists from the full-ref fetch. worktree add without -b.
            await repo.run("worktree","add",str(path),run["branch"])
            checkpoint_id=manifest.get("restore_checkpoints",{}).get(run["id"])
            if checkpoint_id:
                cp=connection.execute("SELECT oid FROM checkpoints WHERE id=?",(checkpoint_id,)).fetchone()
                if cp:
                    await repo.run("read-tree","--reset","-u",cp[0],cwd=path)
                    await repo.run("reset","--mixed","HEAD",cwd=path)
        connection.close()
    return {"project":str(destination),"note":"Restored to a new repository. Configure Git identity, inspect model/MCP settings, and review recovered_global bundles before resuming."}
