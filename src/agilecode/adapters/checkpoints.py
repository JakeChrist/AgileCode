"""Ticket checkpoints are ordinary Git commits on the ticket branch."""
from __future__ import annotations

from pathlib import Path
from agilecode.core.types import AgileCodeError, Conflict, now, uid
from .git import GitRepository
from .store import ProjectStore, encode
from agilecode.application.cancellation import cancellation_checkpoint


class CheckpointService:
    def __init__(self, git: GitRepository, store: ProjectStore):
        self.git, self.store = git, store

    async def record(self, run: dict, label: str, oid: str, *,
                     step_index: int | None = None, outputs: dict | None = None) -> dict:
        identity = uid()
        tree = (await self.git.run("rev-parse", f"{oid}^{{tree}}")).text
        row = {
            "id": identity, "ticket_id": run["ticket_id"], "run_id": run["id"],
            "ref": f"commit:{identity}", "oid": oid, "tree": tree,
            "label": label, "status": "available",
            "step_index": run["step_index"] if step_index is None else step_index,
            "outputs": run["outputs"] if outputs is None else encode(outputs),
            "created": now(),
        }
        await self.store.insert("checkpoints", row)
        return row

    async def capture(self, run: dict, label: str, *, owner: str | None = None) -> dict:
        """An explicitly requested checkpoint is itself a ticket-branch commit."""
        cancellation_checkpoint()
        cwd = Path(run["workspace"])
        oid = await self.git.commit_step(cwd, run["branch"], f"Checkpoint: {label}")
        return await self.record(run, label, oid)

    async def recover(self):
        # Git retains the commits; there are no unpublished private refs.
        return None

    async def preview(self, run: dict, checkpoint_id: str) -> dict:
        row = await self.store.get("checkpoints", checkpoint_id)
        if row["run_id"] != run["id"] or row["status"] != "available":
            raise AgileCodeError("Checkpoint does not belong to this run.")
        cwd = Path(run["workspace"])
        await self.git.verify_worktree(cwd, run["branch"])
        if (await self.git.run("merge-base", "--is-ancestor", row["oid"], "HEAD",
                               cwd=cwd, check=False)).code:
            raise Conflict("That checkpoint commit is no longer on the ticket branch.")
        current_tree = await self.git.snapshot_tree(cwd)
        diff = (await self.git.run("diff", "--no-ext-diff", "--no-textconv",
                                   current_tree, row["tree"], "--",
                                   cwd=cwd)).stdout.decode(errors="replace")
        return {
            "checkpoint_id": checkpoint_id, "expected_tree": current_tree,
            "expected_oid": await self.git.head(cwd),
            "target_tree": row["tree"], "target_oid": row["oid"],
            "restore_blocker": None, "diff": diff,
        }

    async def restore(self, run: dict, checkpoint_id: str, expected_tree: str,
                      *, owner: str | None = None, before_mutation=None) -> dict:
        preview = await self.preview(run, checkpoint_id)
        if preview["expected_tree"] != expected_tree:
            raise Conflict("Checkpoint preview is stale; preview again.")
        if before_mutation is not None:
            await before_mutation(preview, None)
        cancellation_checkpoint()
        cwd = Path(run["workspace"])
        if await self.git.head(cwd) != preview["expected_oid"]:
            raise Conflict("The ticket branch moved since the restore preview.")
        oid = await self.git.reset_to_commit(cwd, run["branch"], preview["target_oid"])
        await self.prune(run)
        return {"tree": (await self.git.run("rev-parse", "HEAD^{tree}", cwd=cwd)).text,
                "commit": oid, "checkpoint_id": checkpoint_id, "before_checkpoint": None}

    async def prune(self, run: dict):
        """Remove restore options whose commits were discarded by a reset."""
        cwd = Path(run["workspace"])
        for row in await self.store.query(
                "SELECT * FROM checkpoints WHERE run_id=? AND status='available'", (run["id"],)):
            if (await self.git.run("merge-base", "--is-ancestor", row["oid"], "HEAD",
                                   cwd=cwd, check=False)).code:
                await self.store.update("checkpoints", row["id"], status="removed")

    async def cleanup(self, ticket_id: str):
        # Only metadata is retired. Ordinary step commits remain in Git history.
        for row in await self.store.query(
                "SELECT * FROM checkpoints WHERE ticket_id=? AND status='available'", (ticket_id,)):
            await self.store.update("checkpoints", row["id"], status="removed")
