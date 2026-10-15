"""Explicit startup reconciliation, using the same operation/result owners."""
from pathlib import Path
from agilecode.domain.types import AgileCodeError, now
from agilecode.adapters.store import encode, decode
from agilecode.runtime.conversation_records import complete_execution_exchange_once, recover_execution_payload, reconcile_final_commit_execution


async def recover_project(self):
    # Restore durable state without replaying interrupted tool calls.
    await self.store.execute("UPDATE requests SET status='interrupted',error='Engine stopped during response',finished=? WHERE status='streaming'", (now(),))
    await self.store.execute("UPDATE attempts SET status='interrupted' WHERE status='running'")
    await self.store.execute("UPDATE runs SET state='paused',error='Engine restarted. Interrupted tool results are recorded in the step conversation.' WHERE state IN ('running','pause_requested')")
    await self.checkpoints.recover()
    for run in await self.store.query("SELECT * FROM runs WHERE state='provisioning'"):
        try:
            await self.git.verify_worktree(Path(run["workspace"]), run["branch"])
            if await self.git.head(Path(run["workspace"])) == run["base_oid"]:
                await self.store.update("runs", run["id"], state="paused", error="Recovered worktree provisioning; resume when ready.")
                ticket = await self.find_ticket(run["ticket_id"])
                if (ticket.get("selected_run_id") == run["id"]
                        and ticket["state"] == "In Progress"
                        and ticket.get("board_transition_id") == run.get("board_transition_id")):
                    await self.move_ticket(ticket["id"], "In Progress",
                        request_id="recover-provision:" + run["id"], activate=False)
        except AgileCodeError:
            await self.store.update("runs", run["id"], state="failed", error="Worktree provisioning was incomplete; original files were not changed.")
    await self.merges.recover()
    # One shared outcome/transcript writer also repairs a lost projection of
    # an already-recorded result; never invoke the underlying tool on restart.
    for tool in await self.store.query("SELECT * FROM tools WHERE result IS NOT NULL OR status IN ('running','unknown')"):
        payload = await recover_execution_payload(self.store, tool)
        await complete_execution_exchange_once(self.store, tool["id"], payload)
    for tool in await self.store.query("SELECT id FROM tools WHERE name='final_commit' AND status='unknown'"):
        await reconcile_final_commit_execution(self.store, tool["id"], git=self.git)

    # Recover analysis runs whose steps finished before their board status
    # was updated, including completions stranded by older versions.
    for completed in await self.store.query(
        "SELECT * FROM runs WHERE state='completed' AND final_oid IS NULL"
    ):
        if completed["ticket_id"] not in self.coordinator.blocked_tickets:
            await self._complete_read_only_run(completed)
