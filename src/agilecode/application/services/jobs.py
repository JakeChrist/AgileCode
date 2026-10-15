"""Scoped job commands. Persisted process IDs never confer live ownership."""
from __future__ import annotations

import hashlib

from agilecode.core.types import AccessDenied, AgileCodeError, now, uid
from agilecode.adapters.store import decode, encode
from .artifacts import ArtifactService


class JobService:
    def __init__(self, store, artifacts: ArtifactService | None = None, runtime_supplier=None, *, shell_supplier=None):
        self.store = store
        self.artifacts = artifacts or ArtifactService(store)
        self.runtime_supplier = runtime_supplier or (lambda: ())
        self.shell_supplier = shell_supplier

    async def _job(self, job_id, ticket_id):
        job = await self.store.get("jobs", job_id)
        if ticket_id is not None and job["ticket_id"] != ticket_id:
            raise AccessDenied("Job belongs to another ticket.")
        return job

    def _live_shell(self, job):
        shells = self.shell_supplier() if self.shell_supplier else (r.shell for r in self.runtime_supplier())
        for shell in shells:
            if shell.run_id != job["run_id"] or shell.ticket_id != job["ticket_id"]:
                continue
            if shell.owns_job(job["id"]):
                return shell
        raise AgileCodeError("This process no longer owns that job; it is not a live managed process.")

    async def register(self, shell, command, attempt_id, *, background=False):
        """Own persistent job/artifact identity; the shell owns process mechanics."""
        if shell.run_id is not None:
            run = await self.store.get("runs", shell.run_id)
            if run["ticket_id"] != shell.ticket_id:
                raise AccessDenied("Job run belongs to another ticket.")
        if attempt_id is not None:
            attempt = await self.store.get("attempts", attempt_id)
            if attempt["run_id"] != shell.run_id:
                raise AccessDenied("Job attempt belongs to another execution.")
        artifact = await self.artifacts.create(shell.ticket_id, "Command output")
        job = {"id": uid(), "ticket_id": shell.ticket_id, "run_id": shell.run_id,
               "attempt_id": attempt_id, "command": command, "cwd": str(shell.shell.cwd),
               "status": "starting", "artifact_id": artifact["id"],
               "background": int(background), "created": now()}
        await self.store.insert("jobs", job)
        return job, artifact

    async def _owned_record(self, job_id, shell):
        job = await self._job(job_id, shell.ticket_id)
        if job["run_id"] != shell.run_id or self._live_shell(job) is not shell:
            raise AccessDenied("Job is not owned by this live execution shell.")
        return job

    async def started(self, job_id, shell, *, environment=None):
        await self._owned_record(job_id, shell)
        changes = {"status": "running"}
        if environment is not None:
            changes["env_hash"] = hashlib.sha256(str(sorted(environment.items())).encode()).hexdigest()
        await self.store.update("jobs", job_id, **changes)

    async def finished(self, job_id, shell, outcome, *, exit_code=None, cwd=None):
        await self._owned_record(job_id, shell)
        if outcome not in {"completed", "cancelled", "interrupted"}:
            raise AgileCodeError("Unsupported managed process outcome.")
        changes = {"status": outcome, "finished": now()}
        if outcome == "completed":
            changes["exit_code"] = exit_code
        if cwd is not None:
            changes["cwd"] = cwd
        await self.store.update("jobs", job_id, **changes)

    async def recover_unowned(self, *, run_id: str | None = None) -> dict[str, str]:
        """Retain crash-orphan uncertainty, never equate a lost handle with exit.

        A PID alone cannot identify a process after restart, and a dead group
        leader cannot prove its descendants stopped. Legacy launches therefore
        remain blocked until an explicit recovery operation supplies independently
        verified cessation evidence. This read/reconciliation route never sends
        a signal or clears an existing blocker merely because it reopened again.

        Startup reconciles the whole host before work is admitted. A live pause
        must supply its stopped run: another run can legitimately be between
        durable job registration and installation of its live process handle.
        """
        sql = "SELECT * FROM jobs WHERE status IN ('starting','running','unknown')"
        parameters = ()
        if run_id is not None:
            sql += " AND run_id=?"
            parameters = (run_id,)
        candidates = await self.store.query(sql, parameters)
        unowned = []
        for job in candidates:
            try:
                self._live_shell(job)
            except AgileCodeError:
                unowned.append(job["id"])
        def reconcile(conn):
            for identity in unowned:
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
                if (row is None or row["status"] not in {"starting", "running", "unknown"}
                        or (run_id is not None and row["run_id"] != run_id)):
                    continue
                operation_id = "job_recovery:" + identity
                if conn.execute("SELECT 1 FROM operations WHERE id=?", (operation_id,)).fetchone():
                    continue
                reason = ("Job " + identity + " lost its live process owner after engine exit. "
                          "Its command may still be running. Verify that the original process and its children "
                          "have stopped before recovering this ticket; a stored PID is not proof of ownership.")
                data = {"job_id": identity, "run_id": row["run_id"], "attempt_id": row["attempt_id"],
                        "previous_status": row["status"], "reason": reason}
                timestamp = now()
                conn.execute(
                    "INSERT INTO operations(id,kind,owner_id,data,status,result,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                    (operation_id, "job_recovery", row["ticket_id"], encode(data), "blocked", None, timestamp, timestamp),
                )
                conn.execute("UPDATE jobs SET status='unknown' WHERE id=?", (identity,))
            blockers = {}
            for row in conn.execute("SELECT owner_id,data FROM operations WHERE kind='job_recovery' AND status='blocked' ORDER BY created,id"):
                data = decode(row['data'])
                if run_id is None or data.get('run_id') == run_id:
                    blockers[row['owner_id']] = data['reason']
            return blockers
        return await self.store.transaction(reconcile)

    async def output(self, job_id, offset=0, limit=100000, *, ticket_id=None):
        job = await self._job(job_id, ticket_id)
        page = await self.artifacts.read(job["artifact_id"], offset, limit, ticket_id=job["ticket_id"])
        return {**job, **page, "output": page["content"]}

    async def stop(self, job_id, *, ticket_id=None):
        job = await self._job(job_id, ticket_id)
        await self._live_shell(job).stop(job_id)
        return {"stopped": job_id}

    async def send_input(self, job_id, text, *, ticket_id=None):
        job = await self._job(job_id, ticket_id)
        await self._live_shell(job).send_input(job_id, text)
        return {"sent": True}
