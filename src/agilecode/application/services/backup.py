"""Portable version-one backup, verified restoration, and selected-run export.

Maintenance owns admission; this service receives only the resources it snapshots.
Portable archives deliberately do not contain the global settings/provider files.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import zipfile

from agilecode.adapters.config_lock import shared_file_lock
from agilecode.adapters.git import GitRepository
from agilecode.application.coordination import settle
from agilecode.application.queries import classify_run
from agilecode.core.types import AgileCodeError, now, uid


class BackupService:
    def __init__(self, *, root, storage, global_root, pm_root, store, git,
                 checkpoints, coordinator, busy_reason, ticket_execution):
        self.root, self.storage = Path(root), Path(storage)
        self.global_root, self.pm_root = Path(global_root), Path(pm_root)
        self.store, self.git, self.checkpoints = store, git, checkpoints
        self.coordinator = coordinator
        self.busy_reason, self.ticket_execution = busy_reason, ticket_execution

    async def create(self, destination: Path) -> dict:
        # Block all new admissions before inspecting existing work. Backup does
        # not pause work as a side effect, nor settle someone else's reservation.
        owner = "backup:" + uid()
        async with self.coordinator.maintenance(owner):
            reason = self.busy_reason()
            if reason:
                raise AgileCodeError(reason)
            # A cancelled waiter must not release admission while SQLite's worker
            # or a Git/filesystem operation is still changing snapshot resources.
            return await settle(self._create(Path(destination), owner))

    async def _resources(self, owner):
        resources, restores, selected = {}, {}, {}
        for ticket in await self.store.query("SELECT id FROM tickets"):
            facts = await self.ticket_execution(ticket["id"])
            if facts.get("run"):
                selected[facts["run"]["id"]] = facts
        for run in await self.store.query("SELECT * FROM runs ORDER BY created,id"):
            facts = selected.get(run["id"])
            item = {"role": "history", "branch": run["branch"], "base_oid": run["base_oid"],
                    "final_oid": run.get("final_oid"), "checkpoint_id": None}
            resources[run["id"]] = item
            if not facts or facts["classification"] in {"integrated", "analysis_completed"}:
                continue
            # Only a selected retained execution may own a restored worktree.
            item["role"] = "unavailable"
            item["reason"] = "No verified branch and recovery point for retained work."
            ref = "refs/heads/" + run["branch"]
            head = await self.git.run("rev-parse", "--verify", ref + "^{commit}", check=False)
            if head.code:
                continue
            item["branch_oid"] = head.text
            base = await self.git.run("cat-file", "-e", run["base_oid"] + "^{commit}", check=False)
            if base.code:
                continue
            path = Path(run["workspace"])
            if path.exists():
                try:
                    _owned_relative(path, self.storage / "worktrees", "worktree")
                    await self.git.verify_worktree(path, run["branch"])
                except AgileCodeError as exc:
                    item["reason"] = str(exc)
                    continue
                if await self.git.head(path) != head.text or await self.git.dirty(path):
                    item["reason"] = "This run has uncommitted edits. Finish the step before backing it up."
                    continue
            # All passed steps are already on the ticket branch. The Git bundle
            # preserves them even if the ticket worktree is absent.
            if run.get("final_oid") and run["final_oid"] != head.text:
                item["reason"] = "Recorded final commit differs from the retained branch."
                continue
            item["role"] = "retained"
            item.pop("reason", None)
        return resources, restores

    async def _create(self, destination, owner):
        if await self.git.dirty():
            raise AgileCodeError("Commit original-checkout changes before backup; this archive does not capture your unrelated uncommitted original files.")
        destination = destination.expanduser().resolve()
        if destination.exists():
            raise AgileCodeError("Backup destination already exists; choose a new filename.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # One lock order, shared with all global writers. The stable lock files
        # themselves are not portable content. Hold through DB/Git/file copying.
        paths = [self.global_root / "settings.json", self.global_root / "providers.json",
                 self.global_root / "teams" / ".catalog", self.global_root / "skills" / ".catalog",
                 self.global_root / "project_manager" / "profiles" / ".catalog"]
        with ExitStack() as locks, tempfile.TemporaryDirectory(prefix="agilecode-backup-") as temporary:
            for path in sorted(paths):
                locks.enter_context(shared_file_lock(path))
            temp = Path(temporary)
            resources, restores = await self._resources(owner)
            await self.store.backup(temp / "agilecode.db")
            await self.git.run("bundle", "create", str(temp / "repository.bundle"), "--all")
            manifest = {"format": "agilecode-backup", "version": 1, "created": now(),
                        "project": str(self.root), "active_branch": await self.git.branch(),
                        "global_root": str(self.global_root), "restore_checkpoints": restores,
                        "resources": resources, "files": {}, "global_teams": [], "skills": []}
            members = [(temp / "agilecode.db", "agilecode.db"),
                       (temp / "repository.bundle", "repository.bundle")]
            lock_files = {path.with_name(path.name + ".lock") for path in paths}
            for folder, prefix in ((self.storage / "artifacts", "artifacts"),
                                   (self.storage / "skills", "project_skills"),
                                   (self.pm_root, "project_manager"),
                                   (self.global_root / "teams", "global_teams"),
                                   (self.global_root / "skills", "global_skills")):
                if folder.exists():
                    for path in sorted(folder.rglob("*")):
                        if path.is_file() and not path.is_symlink() and not _has_symlink_parent(path, folder) and path not in lock_files:
                            members.append((path, (Path(prefix) / path.relative_to(folder)).as_posix()))
            # This worker is awaited to settlement while locks/admission remain held.
            await settle(asyncio.to_thread(_write_archive, destination, members, manifest))
        return {"path": str(destination), "files": len(members), "recovery_checkpoints": restores,
                "unavailable_resources": {k: v["reason"] for k, v in resources.items() if v["role"] == "unavailable"},
                "note": "Includes committed Git history, private refs, database and artifacts. Global settings/provider credentials, original uncommitted files, caches and shell memory are excluded; included history and artifacts are not scrubbed for secrets."}


def _has_symlink_parent(path, root):
    return root.is_symlink() or any(p.is_symlink() for p in path.parents if p == root or root in p.parents)


def _owned_relative(path, root, label, *, verify_filesystem=True):
    path, root = Path(path), Path(root)
    try:
        rel = path.relative_to(root)
    except ValueError:
        raise AgileCodeError(f"Recorded {label} path is outside project-owned storage.") from None
    if not rel.parts or ".." in rel.parts or (verify_filesystem and (path.is_symlink() or _has_symlink_parent(path, root))):
        raise AgileCodeError(f"Unsafe recorded {label} path.")
    return rel


def _write_archive(destination, members, manifest):
    staging = destination.with_name(destination.name + "." + uid() + ".partial")
    try:
        with zipfile.ZipFile(staging, "x", zipfile.ZIP_DEFLATED) as archive:
            for path, name in members:
                digest, size = hashlib.sha256(), 0
                with path.open("rb") as source, archive.open(name, "w") as entry:
                    while chunk := source.read(1024 * 1024):
                        entry.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                manifest["files"][name] = {"bytes": size, "sha256": digest.hexdigest()}
            # Captured global versions identify only the safe portable definitions.
            # Never hash/export secret-bearing global settings into this manifest.
            manifest["global_versions"] = {k: v["sha256"] for k, v in manifest["files"].items()
                                           if k.startswith(("global_teams/", "global_skills/"))}
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        with staging.open("rb") as handle:
            os.fsync(handle.fileno())
        # link() is an exclusive, same-filesystem publication. A racing creator's
        # destination is preserved instead of replaced after an earlier exists().
        try:
            os.link(staging, destination)
        except FileExistsError:
            raise AgileCodeError("Backup destination already exists; choose a new filename.") from None
    finally:
        staging.unlink(missing_ok=True)


def _extract_archive(source, temp):
    try:
        with zipfile.ZipFile(source) as archive:
            if len(archive.namelist()) != len(set(archive.namelist())):
                raise AgileCodeError("Duplicate archive member path.")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != "agilecode-backup" or manifest.get("version") != 1:
                raise AgileCodeError("Unsupported backup format.")
            files = manifest["files"]
            if not isinstance(files, dict) or not {"agilecode.db", "repository.bundle"} <= files.keys():
                raise AgileCodeError("Backup is missing required database or repository content.")
            for name, meta in files.items():
                rel = PurePosixPath(name)
                if rel.is_absolute() or ".." in rel.parts or not rel.parts or "\\" in name or ":" in rel.parts[0] or str(rel) != name or name == "manifest.json":
                    raise AgileCodeError("Invalid archive member path.")
                info = archive.getinfo(name)
                if info.is_dir() or info.file_size != meta["bytes"]:
                    raise AgileCodeError("Archive member size does not match the manifest.")
                path = temp.joinpath(*rel.parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                digest, size = hashlib.sha256(), 0
                with archive.open(name) as data, path.open("xb") as target:
                    while chunk := data.read(1024 * 1024):
                        target.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                if size != meta["bytes"] or digest.hexdigest() != meta["sha256"]:
                    raise AgileCodeError("Backup content or checksum mismatch: " + name)
            return manifest
    except (KeyError, ValueError, TypeError, zipfile.BadZipFile) as exc:
        raise AgileCodeError("Invalid backup manifest or archive: " + str(exc)) from exc


async def restore_backup(source: Path, destination: Path, global_root: Path | None = None) -> dict:
    """Verify and stage a fresh project; recovered globals never modify this host."""
    destination = Path(destination).expanduser().resolve()
    if destination.exists():
        raise AgileCodeError("Restore destination must not exist.")
    # Restoring has no global writes, irrespective of the caller's configured home.
    return await settle(_restore(Path(source), destination))


async def _restore(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".agilecode-restore-", dir=destination.parent) as temporary:
        temp = Path(temporary)
        manifest = await asyncio.to_thread(_extract_archive, source, temp / "archive")
        archive = temp / "archive"
        stage = temp / "project"
        stage.mkdir()
        repo = GitRepository(stage)
        await repo.run("init", "-b", "agilecode-restore-" + uid())
        await repo.run("bundle", "verify", str(archive / "repository.bundle"))
        await repo.run("fetch", str(archive / "repository.bundle"), "refs/*:refs/*")
        branch = manifest.get("active_branch")
        if not isinstance(branch, str) or (await repo.run("check-ref-format", "--branch", branch, check=False)).code:
            raise AgileCodeError("Invalid original branch in backup.")
        await repo.run("checkout", branch)
        storage = stage / ".agilecode"
        if storage.exists() or storage.is_symlink():
            raise AgileCodeError("Repository content occupies the internal .agilecode storage path.")
        storage.mkdir()
        shutil.copy2(archive / "agilecode.db", storage / "agilecode.db")
        for prefix, target in (("artifacts", storage / "artifacts"), ("project_skills", storage / "skills"),
                               ("project_manager", storage / "project_manager"),
                               ("global_teams", storage / "recovered_global" / "global_teams"),
                               ("global_skills", storage / "recovered_global" / "global_skills")):
            if (archive / prefix).exists():
                shutil.copytree(archive / prefix, target, dirs_exist_ok=True)
        pm_state = storage / "project_manager" / "state.json"
        if pm_state.exists():
            state = json.loads(pm_state.read_text(encoding="utf-8"))
            state.update(status="paused", current_activity="Paused after backup restore")
            pm_state.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        connection = sqlite3.connect(storage / "agilecode.db")
        connection.row_factory = sqlite3.Row
        restored, warnings = [], []
        try:
            from agilecode.adapters.store import SCHEMA_VERSION
            if connection.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
                raise AgileCodeError("Backup database is newer than this application.")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise AgileCodeError("Backup database failed its integrity check.")
            old = Path(manifest["project"]) / ".agilecode"
            final_storage = destination / ".agilecode"
            runs = [dict(row) for row in connection.execute("SELECT * FROM runs ORDER BY created,id")]
            declared = manifest.get("resources")
            if declared is not None and (not isinstance(declared, dict) or set(declared) != {r["id"] for r in runs}):
                raise AgileCodeError("Backup resource declarations do not match its runs.")
            for run in runs:
                evidence = connection.execute(
                    "SELECT 1 FROM operations WHERE id=? AND kind='analysis_completion' AND status IN ('validated','completed')",
                    ("analysis:" + run["id"],),
                ).fetchone()
                classification = classify_run(run, analysis_evidence=bool(evidence))["classification"]
                resource = declared[run["id"]] if declared is not None else _legacy_resource(run, manifest, analysis_evidence=bool(evidence))
                role = resource.get("role")
                if role not in {"history", "retained", "unavailable"}:
                    raise AgileCodeError("Unsupported backup resource role.")
                # Only verified completion proves that analysis resources are
                # historical. A domain-only host does not validate completion.
                if classification in {"analysis_completed", "integrated"}:
                    if role == "retained":
                        raise AgileCodeError("Historical execution cannot declare a live worktree.")
                    role = "history"
                if role == "retained":
                    await _restore_worktree(repo, connection, run, resource, old, storage)
                    restored.append(run["id"])
                elif role == "unavailable":
                    reason = resource.get("reason", "Legacy backup cannot prove the retained worktree's resources.")
                    warnings.append({"run_id": run["id"], "reason": reason})
                    connection.execute("UPDATE runs SET error=? WHERE id=?", ("Restore blocked: " + reason, run["id"]))
                    timestamp = now()
                    connection.execute(
                        "INSERT INTO operations(id,kind,owner_id,data,status,result,created,updated) VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET data=excluded.data,status=excluded.status,updated=excluded.updated",
                        ("resource-restore:" + run["id"], "resource_restore", run["ticket_id"],
                         json.dumps({"run_id": run["id"], "reason": reason}), "blocked", None, timestamp, timestamp),
                    )
                # Relocate owned resource identities only. Job cwd/request strings
                # are historical evidence and are intentionally never rewritten.
                previous = Path(run["workspace"])
                try:
                    relative = _owned_relative(previous, old / "worktrees", "worktree", verify_filesystem=False)
                except AgileCodeError:
                    if role != "history":
                        raise
                else:
                    connection.execute("UPDATE runs SET workspace=? WHERE id=?", (str(final_storage / "worktrees" / relative), run["id"]))
            for row in connection.execute("SELECT id,workspace FROM integrations").fetchall():
                previous = Path(row["workspace"])
                try:
                    relative = _owned_relative(previous, old / "integration", "integration worktree", verify_filesystem=False)
                except AgileCodeError:
                    continue
                connection.execute("UPDATE integrations SET workspace=? WHERE id=?", (str(final_storage / "integration" / relative), row["id"]))
            for row in connection.execute("SELECT * FROM artifacts").fetchall():
                relative = _owned_relative(Path(row["path"]), old / "artifacts", "artifact", verify_filesystem=False)
                path = storage / "artifacts" / relative
                if not path.is_file():
                    raise AgileCodeError("Backup is missing referenced artifact: " + row["id"])
                if row["complete"] and (path.stat().st_size != row["size"] or (row["hash"] and _file_hash(path) != row["hash"])):
                    raise AgileCodeError("Backup artifact does not match its recorded content: " + row["id"])
                connection.execute("UPDATE artifacts SET path=? WHERE id=?", (str(final_storage / "artifacts" / relative), row["id"]))
            # Every available checkpoint must retain its exact private ref/object.
            for checkpoint in connection.execute("SELECT * FROM checkpoints WHERE status='available'").fetchall():
                await _verify_checkpoint(repo, dict(checkpoint))
            _restore_defaults(connection, destination)
            connection.commit()
        finally:
            connection.close()
        _relocate_worktree_metadata(stage, destination)
        # Reserve a new destination exclusively, then publish the verified tree.
        try:
            destination.mkdir()
        except FileExistsError:
            raise AgileCodeError("Restore destination must not exist.") from None
        try:
            os.replace(stage, destination)
        except BaseException:
            # Only remove our own still-empty reservation. Never delete a path
            # another process populated while publication was being attempted.
            try:
                destination.rmdir()
            except OSError:
                pass
            raise
    return {"project": str(destination), "restored_runs": restored, "unavailable_resources": warnings,
            "note": "Restored to a new repository with execution paused. Configure Git identity, inspect model/MCP settings, and review recovered_global bundles before resuming."}


def _legacy_resource(run, manifest, *, analysis_evidence=False):
    classification = classify_run(run, analysis_evidence=analysis_evidence)["classification"]
    if classification in {"integrated", "analysis_completed"}:
        return {"role": "history"}
    checkpoint = manifest.get("restore_checkpoints", {}).get(run["id"])
    if checkpoint or run.get("final_oid"):
        return {"role": "retained", "branch": run["branch"], "checkpoint_id": checkpoint,
                "base_oid": run["base_oid"], "final_oid": run.get("final_oid")}
    return {"role": "unavailable"}


async def _verify_checkpoint(repo, checkpoint):
    oid, tree = checkpoint.get("oid"), checkpoint.get("tree")
    if not oid or not tree:
        raise AgileCodeError("Invalid step commit identity.")
    actual = await repo.run("rev-parse", "--verify", oid + "^{tree}", check=False)
    if actual.code or actual.text != tree:
        raise AgileCodeError("Recorded step commit is missing or changed.")


async def _restore_worktree(repo, connection, run, resource, old_storage, storage):
    branch = run["branch"]
    if resource.get("branch") != branch or (await repo.run("check-ref-format", "--branch", branch, check=False)).code:
        raise AgileCodeError("Retained branch identity is invalid.")
    if resource.get("base_oid") != run["base_oid"] or resource.get("final_oid") != run.get("final_oid"):
        raise AgileCodeError("Retained resource objects do not match the recorded run.")
    head = await repo.run("rev-parse", "--verify", "refs/heads/" + branch + "^{commit}", check=False)
    if head.code or (resource.get("branch_oid") and resource["branch_oid"] != head.text):
        raise AgileCodeError("Required retained branch is missing or changed.")
    if (await repo.run("cat-file", "-e", run["base_oid"] + "^{commit}", check=False)).code:
        raise AgileCodeError("Required retained base object is missing.")
    if run.get("final_oid") and run["final_oid"] != head.text:
        raise AgileCodeError("Retained final commit differs from the branch.")
    relative = _owned_relative(Path(run["workspace"]), old_storage / "worktrees", "worktree", verify_filesystem=False)
    path = storage / "worktrees" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    await repo.run("worktree", "add", str(path), branch)


def _restore_defaults(connection, destination):
    prior = json.loads(connection.execute("SELECT value FROM meta WHERE key='project'").fetchone()[0])
    prior.update(root=str(destination), common_git_dir=str(destination / ".git"))
    connection.execute("UPDATE meta SET value=? WHERE key='project'", (json.dumps(prior),))
    connection.execute("UPDATE runs SET state='paused',error=COALESCE(error,'Restored backup: inspect model settings, tools and shell setup before resume.') WHERE state NOT IN ('completed','integrated','failed','cancelled')")
    connection.execute("UPDATE attempts SET status='interrupted' WHERE status IN ('running','waiting')")
    connection.execute("UPDATE jobs SET status='interrupted',finished=? WHERE status IN ('starting','running','unknown')", (now(),))
    connection.execute("UPDATE integrations SET state='blocked',error='Rebuild candidate after backup restoration.' WHERE state<>'integrated'")
    settings = connection.execute("SELECT value FROM settings WHERE key='project'").fetchone()
    data = json.loads(settings[0]) if settings else {}
    data["merge_on_completion"] = False
    data.setdefault("mcp_servers", [])
    for config in data["mcp_servers"]:
        config["enabled"] = False
    connection.execute("INSERT INTO settings(key,value) VALUES('project',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(data),))
    connection.execute("UPDATE tickets SET merge_auto=0 WHERE state<>'Done'")
    connection.execute("DELETE FROM meta WHERE key='execution_controls'")


def _file_hash(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _relocate_worktree_metadata(stage, destination):
    directory = stage / ".git" / "worktrees"
    if not directory.exists():
        return
    for admin in directory.iterdir():
        gitdir_file = admin / "gitdir"
        old_gitfile = Path(gitdir_file.read_text(encoding="utf-8").strip())
        relative = _owned_relative(old_gitfile, stage / ".agilecode" / "worktrees", "Git link")
        gitdir_file.write_text(str(destination / ".agilecode" / "worktrees" / relative) + "\n", encoding="utf-8")
        old_gitfile.write_text("gitdir: " + str(destination / ".git" / "worktrees" / admin.name) + "\n", encoding="utf-8")


async def export_ticket(ticket_id, destination, *, ticket_detail, messages):
    """Version-one selected-run scope, preserving ticket-wide related records."""
    detail = await ticket_detail(ticket_id, complete=True)
    detail.update(format="agilecode-ticket-export", version=1, conversations={})
    for attempt in detail.get("attempts", []):
        detail["conversations"][attempt["session_id"]] = await messages(attempt["session_id"], include_partial=True)
    path = Path(destination).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    def write():
        try:
            with path.open("x", encoding="utf-8") as output:
                json.dump(detail, output, indent=2, ensure_ascii=False)
        except FileExistsError:
            raise AgileCodeError("Export destination exists; choose a new filename.") from None
    await settle(asyncio.to_thread(write))
    return {"path": str(path)}
