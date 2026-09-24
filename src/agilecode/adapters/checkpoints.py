"""Private-ref checkpoints, raw-byte restore, and explicit recovery boundaries."""
from __future__ import annotations
import asyncio
import os
import stat
from pathlib import Path
from agilecode.core.types import AgileCodeError, Conflict, now, uid
from .git import GitRepository
from .store import ProjectStore, encode, decode
from .workspace import inside


class CheckpointService:
    def __init__(self, git: GitRepository, store: ProjectStore):
        self.git, self.store = git, store

    async def capture(self, run: dict, label: str) -> dict:
        identity = uid()
        ref = f"refs/agilecode/checkpoints/{run['ticket_id']}/{identity}"
        row = {"id": identity, "ticket_id": run["ticket_id"], "run_id": run["id"], "ref": ref,
               "oid": None, "tree": None, "label": label, "status": "preparing",
               "step_index": run["step_index"], "outputs": run["outputs"], "created": now()}
        await self.store.insert("checkpoints", row)
        try:
            tree = await self.git.snapshot_tree(Path(run["workspace"]))
            oid = await self.git.commit_object(tree, [await self.git.head(Path(run["workspace"]))], f"AgileCode checkpoint: {label}\n")
            # Persist intended object before publishing. Recovery can compare this exact ref.
            await self.store.update("checkpoints", identity, oid=oid, tree=tree)
            await self.git.publish_ref(ref, oid)
            await self.store.update("checkpoints", identity, status="available")
        except BaseException:
            # A preparing record intentionally remains recoverable after uncertain publication.
            raise
        return await self.store.get("checkpoints", identity)

    async def recover(self):
        for row in await self.store.query("SELECT * FROM checkpoints WHERE status='preparing'"):
            actual = await self.git.run("rev-parse", "--verify", row["ref"], check=False)
            status = "available" if row["oid"] and actual.code == 0 and actual.text == row["oid"] else "incomplete"
            await self.store.update("checkpoints", row["id"], status=status)

    async def preview(self, run: dict, checkpoint_id: str) -> dict:
        checkpoint = await self.store.get("checkpoints", checkpoint_id)
        if checkpoint["run_id"] != run["id"] or checkpoint["status"] != "available":
            raise AgileCodeError("Checkpoint does not belong to this run or is no longer available.")
        cwd = Path(run["workspace"])
        await self.git.verify_worktree(cwd, run["branch"])
        current = await self.git.snapshot_tree(cwd)
        diff = (await self.git.run("diff", "--no-ext-diff", "--no-textconv", current, checkpoint["tree"], "--", cwd=cwd)).stdout.decode(errors="replace")
        return {"checkpoint_id": checkpoint_id, "expected_tree": current, "target_tree": checkpoint["tree"], "diff": diff}

    async def restore(self, run: dict, checkpoint_id: str, expected_tree: str) -> dict:
        preview = await self.preview(run, checkpoint_id)
        if preview["expected_tree"] != expected_tree:
            raise Conflict("Restore preview is stale. New user changes were preserved; preview again.")
        cwd = Path(run["workspace"])
        before = await self.capture(run, "Before explicit checkpoint restore")
        old = await self.git.tree_entries(expected_tree)
        new = await self.git.tree_entries(preview["target_tree"])
        # Check all parents before any mutation. A restored symlink must not act as a
        # traversable directory for another entry in the same tree.
        for rel in set(old) | set(new):
            target = cwd / rel
            if not self.git.eligible(rel, tracked=True) or not inside(target.parent.resolve(), cwd.resolve()):
                raise Conflict(f"Restore path has unsafe parents: {rel}")
            for parent in Path(rel).parents:
                entry = new.get(str(parent))
                if entry and entry[0] == "120000":
                    raise Conflict("Snapshot contains a symlink used as a directory.")
        for rel in sorted(set(old)-set(new), key=lambda p: len(Path(p).parts), reverse=True):
            target = cwd / rel
            if target.is_file() or target.is_symlink():
                target.unlink()
        for rel, (mode, oid) in new.items():
            if old.get(rel) == (mode, oid):
                continue
            target = cwd / rel
            if target.is_symlink():
                target.unlink()
            if target.is_dir():
                try:
                    target.rmdir()
                except OSError as exc:
                    raise Conflict(f"Restore preserves unexpected directory contents at {rel}.") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            data = (await self.git.run("cat-file", "blob", oid)).stdout
            if mode == "120000":
                os.symlink(os.fsdecode(data), target)
            else:
                temp = target.with_name(f".agilecode-restore-{uid()}")
                try:
                    with temp.open("wb") as handle:
                        handle.write(data); handle.flush(); os.fsync(handle.fileno())
                    os.chmod(temp, 0o755 if mode == "100755" else 0o644)
                    os.replace(temp, target)
                finally:
                    if temp.exists():
                        temp.unlink()
        observed = await self.git.snapshot_tree(cwd)
        if observed != preview["target_tree"]:
            raise Conflict("Restore did not reproduce the expected source tree. Pre-restore checkpoint retained.")
        return {"tree": observed, "before_checkpoint": before["id"], "checkpoint_id": checkpoint_id}

    async def cleanup(self, ticket_id: str):
        for row in await self.store.query("SELECT * FROM checkpoints WHERE ticket_id=? AND status='available'", (ticket_id,)):
            await self.git.run("update-ref", "-d", row["ref"], row["oid"])
            await self.store.update("checkpoints", row["id"], status="removed")
