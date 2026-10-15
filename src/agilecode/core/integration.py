"""Serialized target application, isolated candidates, and AI-assisted merge review.

An integration candidate is bound to target/ticket OIDs. The original checkout is
not a scratch area. No push, force reset, or implicit branch switch occurs here.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

from .types import AgileCodeError, Conflict, NeedsReview, Event, Team, Step, ResultStatus, now, uid
from agilecode.adapters.store import encode, decode
from agilecode.application.coordination import settle
from agilecode.application.policy import policy_lock, permits_automatic_integration

if TYPE_CHECKING:
    from agilecode.application.execution import ExecutionOperations


class MergeCoordinator:
    def __init__(self, app: ExecutionOperations):
        self.app = app
        self.apply_lock = asyncio.Lock()
        self.prepare_locks: dict[str, asyncio.Lock] = {}
        self.runtimes = {}
        self._finishing_tasks: set[asyncio.Task] = set()

    async def _target(self, branch: str) -> str:
        return (await self.app.git.run("rev-parse", "--verify", f"refs/heads/{branch}")).text

    async def pause_ticket(self, ticket_id: str) -> bool:
        """Stop outstanding integration work for a ticket moved off In Progress."""
        task = self.app.integration_tasks.get(ticket_id)
        if not task or task.done():
            return False
        # Never interrupt a Git apply halfway through. Once apply finishes,
        # let its cleanup settle before allowing the manual board move.
        async with self.apply_lock:
            if task.done():
                return False
            if task not in self._finishing_tasks:
                task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def pause_all(self) -> list[str]:
        """Stop preparations after any active target application finishes."""
        interrupted = []
        cancelled = []
        finishing = []
        async with self.apply_lock:
            for ticket_id, task in tuple(self.app.integration_tasks.items()):
                if task.done():
                    continue
                if task in self._finishing_tasks:
                    finishing.append(task)
                    continue
                task.cancel()
                interrupted.append(ticket_id)
                cancelled.append(task)
            if cancelled:
                await asyncio.gather(*cancelled, return_exceptions=True)
        # Successful applications must finish their existing cleanup. Cancelling
        # them would misreport an already integrated candidate as interrupted.
        if finishing:
            await asyncio.gather(*finishing, return_exceptions=True)
        return interrupted

    async def prepare(self, ticket_id: str, *, rebuild=False, resume=False) -> dict:
        owner = 'integration:' + uid()
        result = None
        async with self.app.coordinator.activity(ticket_id,owner,'integration',
                check=lambda:self.app.require_eligible(ticket_id,'integration')):
            result = await self._prepare(ticket_id,rebuild=rebuild,resume=resume,_owner=owner)
        if result and result['state'] == 'integrated':
            await self._complete_board(result)
        return result

    async def _prepare(self, ticket_id: str, *, rebuild=False, resume=False, _owner=None) -> dict:
        app = self.app
        ticket = await app.find_ticket(ticket_id)
        async with self.prepare_locks.setdefault(ticket["id"], asyncio.Lock()):
            run = await app.selected_run(ticket["id"])
            if not run or run["state"] not in {"completed", "integrated"} or not run["final_oid"]:
                raise AgileCodeError("Only a completed, committed ticket can be integrated.")
            old = await app.store.one("SELECT * FROM integrations WHERE run_id=? ORDER BY created DESC LIMIT 1", (run["id"],))
            if old and (old["state"] == "integrated" or (old["state"] == "awaiting_review" and not rebuild)):
                if resume and old["state"] == "awaiting_review" and old["auto"]:
                    return await self.apply(old["id"], old["candidate_oid"], old["target_oid"], automatic=True, _owner=_owner)
                return old
            if await app.git.head(Path(run["workspace"])) != run["final_oid"] or await app.git.snapshot_tree(Path(run["workspace"])) != run["final_tree"]:
                raise Conflict("The completed ticket worktree changed. Do not integrate stale verification.")
            target_oid = await self._target(run["base_branch"])
            identity = uid()
            work = app.storage / "integration" / identity
            auto = permits_automatic_integration(ticket, app.config.settings)
            record = {"id": identity, "ticket_id": ticket["id"], "run_id": run["id"], "target_branch": run["base_branch"],
                      "target_oid": target_oid, "ticket_oid": run["final_oid"], "workspace": str(work), "state": "preparing",
                      "auto": int(auto), "report": "{}", "created": now(), "updated": now()}
            await app.store.insert("integrations", record)
            report = {"target_oid": target_oid, "ticket_oid": run["final_oid"], "validation_attempts": [], "conflicts": [],
                      "completion_origin": {"run_id": run['id'], "transition_id": run['board_transition_id']}}
            try:
                ancestor = await app.git.run("merge-base", "--is-ancestor", target_oid, run["final_oid"], check=False)
                if ancestor.code == 0:
                    # The reviewed ticket tree already contains the exact target.
                    await app.git.add_worktree(work, None, run["final_oid"])
                    candidate, tree = run["final_oid"], run["final_tree"]
                    report["strategy"] = "fast_forward"
                    report["validation"] = "Reuses verification/review for the exact unchanged ticket tree."
                else:
                    await app.git.add_worktree(work, None, target_oid)
                    merge = await app.git.run("merge", "--no-ff", "--no-commit", run["final_oid"], cwd=work, check=False)
                    unmerged = await app.git.run("ls-files", "-u", "-z", cwd=work)
                    report["strategy"] = "merge"
                    if merge.code and not unmerged.stdout:
                        raise AgileCodeError("Git could not prepare a candidate: " + merge.stderr.decode(errors="replace"))
                    conflicts = {}
                    for entry in unmerged.stdout.split(b"\0"):
                        if not entry: continue
                        meta, path = entry.split(b"\t", 1)
                        mode, oid, stage = meta.decode().split()
                        name = path.decode("utf-8", errors="strict")
                        if mode == "160000":
                            raise NeedsReview("Conflicting submodule state requires manual handling.")
                        conflicts.setdefault(name, {})[stage] = {"mode": mode, "oid": oid}
                    report["conflicts"] = conflicts
                    await app.store.update("integrations", identity, report=encode(report))
                    runtime = app._runtime(run, workspace=work, phase=f"integration:{identity}")
                    self.runtimes[identity] = runtime
                    from agilecode.adapters.environment import WorktreeEnvironment
                    runtime.environment = await WorktreeEnvironment(runtime.shell, app.config.settings).prepare()
                    snapshot = decode(run["snapshot"])
                    team = Team.model_validate(snapshot["team"])
                    process = team.process(snapshot["process_id"])
                    context = {"integration_id": identity, "diff_base": target_oid,
                               "merge_base": (await app.git.run("merge-base", target_oid, run["final_oid"])).text,
                               "target_oid": target_oid, "ticket_oid": run["final_oid"],
                               "conflict_stages": conflicts,
                               "instruction": "Validate the combined code, not only this ticket in isolation. Preserve both branches' intended behavior."}
                    # Include recent completed ticket specifications that contributed to this target.
                    contributors = await app.store.query("SELECT tickets.key,tickets.title,tickets.description,runs.final_oid FROM runs JOIN tickets ON tickets.id=runs.ticket_id WHERE runs.state='integrated' ORDER BY runs.updated DESC LIMIT 12")
                    context["recent_integrated_tickets"] = contributors
                    if conflicts:
                        versions = {}
                        for name, stages in conflicts.items():
                            if not app.git.eligible(name, tracked=True):
                                raise NeedsReview("A conflict touches a protected/internal path: " + name)
                            versions[name] = {}
                            for stage, entry in stages.items():
                                data = (await app.git.run("cat-file", "blob", entry["oid"])).stdout
                                if b"\0" in data:
                                    raise NeedsReview("Binary conflict requires explicit human resolution: " + name)
                                versions[name][{"1":"base", "2":"target", "3":"ticket"}[stage]] = data.decode("utf-8", errors="strict")
                        context["conflict_versions"] = versions
                        source = next((s for s in process.steps if s.kind == "implement"), process.steps[0])
                        resolver = Step(id="integration_resolve", name="Resolve merge conflicts", team_member_id=source.team_member_id,
                            kind="implement", prompt="Resolve the recorded conflicts in this isolated candidate worktree. Read the base, target and ticket versions and requirements. Use guarded file edits, never Git add/commit/merge. Ask the user when intent is ambiguous. Return PASS only after every textual conflict is deliberately resolved.")
                        await self._stage(record, run, team, process, resolver, runtime, context, report)
                        for name in conflicts:
                            file = work/name
                            if file.exists() and not file.is_symlink():
                                text = file.read_text(encoding="utf-8", errors="replace")
                                if any(line.startswith(("<<<<<<< ", ">>>>>>> ")) or line == "=======" for line in text.splitlines()):
                                    raise NeedsReview("Conflict markers remain in " + name)
                    for kind, title in (("verify", "Verify integration candidate"), ("review", "Review integration candidate")):
                        source = next((s for s in process.steps if s.kind == kind), None)
                        if not source:
                            raise NeedsReview("The selected process lacks a verification/review member for integration.")
                        stage = source.model_copy(update={"id": "integration_"+kind, "name": title, "correction_target": None})
                        await self._stage(record, run, team, process, stage, runtime, context, report)
                    tree = await app.git.snapshot_tree(work)
                    # Commit a validated combined tree in the detached candidate, never
                    # the ticket branch. Normal Git commit hooks still run.
                    await app.git.run("read-tree", tree, cwd=work)
                    await app.git.run("commit", "-m", f"Integrate {ticket['key']}: {ticket['title']}", cwd=work)
                    candidate = await app.git.head(work)
                    if (await app.git.run("rev-parse", "HEAD^{tree}", cwd=work)).text != tree or await app.git.snapshot_tree(work) != tree:
                        raise Conflict("Commit hooks changed the validated integration tree; review again.")
                    parents = (await app.git.run("rev-list", "--parents", "-n", "1", candidate)).text.split()[1:]
                    if parents != [target_oid, run["final_oid"]]:
                        raise Conflict("The integration candidate has unexpected parents.")
                    report["validation"] = "Fresh requirement evidence and code review passed for the combined tree."
                await app.git.publish_ref(f"refs/agilecode/integrations/{identity}", candidate)
                await app.store.update("integrations", identity, candidate_oid=candidate, tree=tree,
                                       state="awaiting_review", report=encode(report), updated=now())
                app.events.emit(Event("IntegrationReady", ticket["id"], run["id"], data={"integration_id":identity,"candidate_oid":candidate,"target_oid":target_oid,"auto":bool(auto)}))
                if auto:
                    return await self.apply(identity, candidate, target_oid, automatic=True, _owner=_owner)
                return await app.store.get("integrations", identity)
            except asyncio.CancelledError:
                await self._record_preparation_failure(identity, report,
                    'Integration preparation interrupted; retained candidate files require review.')
                raise
            except Exception as exc:
                current = await self._record_preparation_failure(identity, report, str(exc))
                if current['state'] != 'integrated':
                    app.events.emit(Event("IntegrationBlocked", ticket["id"], run["id"], data={"integration_id":identity,"error":str(exc)}))
                return current
            finally:
                runtime = self.runtimes.pop(identity, None)
                if runtime:
                    await settle(runtime.shell.close()); await settle(runtime.mcp.close())

    async def _record_preparation_failure(self, identity, report, error):
        # Application owns the Git outcome. An interrupted prepare waiter cannot
        # turn integrated history or an uncertain target effect into a fresh
        # blocked candidate that recovery would no longer inspect.
        def record(c):
            current = c.execute('SELECT * FROM integrations WHERE id=?', (identity,)).fetchone()
            if current['state'] not in {'integrated', 'applying', 'uncertain'}:
                c.execute("UPDATE integrations SET state='blocked',report=?,error=?,updated=? WHERE id=?",
                          (encode(report), error, now(), identity))
        await self.app.store.transaction(record)
        return await self.app.store.get('integrations', identity)

    async def _stage(self, record, run, team, process, step, runtime, context, report):
        app = self.app
        attempt, ctx, _ = await app.processor.new_attempt(run, team, process, step,
            phase=f"integration:{record['id']}", extra=context, workspace=runtime.workspace.root)
        ctx.extra["_runtime"] = runtime
        try:
            result = await app.processor.execute_step(step, ctx)
            await app.processor._gate(ctx, step, runtime, result)
            fingerprint = await app.git.snapshot_tree(runtime.workspace.root)
            await app.store.update("attempts", attempt["id"], status="completed", result=result.model_dump_json(), fingerprint=fingerprint, finished=now())
            report["validation_attempts"].append(attempt["id"])
            await app.store.update("integrations", record["id"], report=encode(report))
            if result.status != ResultStatus.PASS:
                raise NeedsReview(f"{step.name}: {result.summary}. Inspect the candidate and rebuild after correcting the underlying issue.")
        except BaseException:
            await app.store.execute("UPDATE attempts SET status='interrupted' WHERE id=? AND status IN ('running','waiting')", (attempt["id"],))
            raise

    async def apply(self, identity: str, expected_candidate: str, expected_target: str, *, automatic=False, _owner=None) -> dict:
        app = self.app
        record = await app.store.get('integrations',identity)
        if record['state'] == 'integrated':
            return record
        owner = _owner or 'integration:' + identity
        if _owner:
            return await self._apply_owned(identity,expected_candidate,expected_target,automatic=automatic,owner=owner)
        async with app.coordinator.activity(record['ticket_id'],owner,'integration',
                check=lambda:app.require_eligible(record['ticket_id'],'integration')):
            result = await self._apply_owned(identity,expected_candidate,expected_target,automatic=automatic,owner=owner)
        if result['state'] == 'integrated':
            await self._complete_board(result)
        return result

    async def _apply_owned(self, identity, expected_candidate, expected_target, *, automatic=False, owner):
        app = self.app
        record = await app.store.get('integrations',identity)
        # Admission and pause/move exclusion are one brief boundary. Once target
        # application is admitted, its actual Git and cleanup must settle.
        async with app.coordinator.lock:
            reservation = app.coordinator.reservations.get(record['ticket_id'])
            if reservation and reservation.owner != owner:
                raise Conflict('Ticket control changed before target application was admitted.')
            facts = await app.require_eligible(record['ticket_id'],'integration')
            if facts['selected_run_id'] != record['run_id']:
                raise Conflict('Integration belongs to historical work, not the selected execution.')
            report = decode(record['report'],{})
            report['completion_origin'] = {'run_id':record['run_id'],'transition_id':facts['transition_id']}
            await app.store.update('integrations',identity,report=encode(report),updated=now())
            for activity in app.coordinator.activities.get(record['ticket_id'],[]):
                if activity.owner == owner:
                    activity.cancellable = False
        return await settle(self._apply_effect(identity,expected_candidate,expected_target,automatic=automatic))

    async def _apply_effect(self, identity, expected_candidate, expected_target, *, automatic=False):
        app = self.app
        async with self.apply_lock:
            record = await app.store.get("integrations", identity)
            if record["state"] == "integrated": return record
            ticket = await app.find_ticket(record["ticket_id"])
            if automatic and not permits_automatic_integration(ticket, app.config.settings):
                return record
            if record["state"] != "awaiting_review" or record["candidate_oid"] != expected_candidate or record["target_oid"] != expected_target:
                raise Conflict("This approval does not match an available candidate.")
            if await app.git.branch() != record["target_branch"]:
                raise Conflict("The original checkout is on another branch. AgileCode will not switch it for you.")
            if await app.git.head() != expected_target or await self._target(record["target_branch"]) != expected_target:
                await app.store.update("integrations", identity, state="stale", error="Target advanced; rebuild and revalidate this integration.")
                raise Conflict("Target changed since this candidate was created. Rebuild the candidate.")
            if await app.git.dirty():
                raise Conflict("The original checkout contains uncommitted changes. Commit or resolve them before integration.")
            work = Path(record["workspace"])
            if await app.git.head(work) != expected_candidate or await app.git.snapshot_tree(work) != record["tree"]:
                raise Conflict("Candidate changed after verification. Rebuild rather than approving stale contents.")
            operation_id = "integrate:" + identity
            async with policy_lock(app.store):
                # Recheck after awaited Git validation, ordered with both config
                # publication and ticket permission edits. Dispatch happens under
                # this short gate; Git itself settles after releasing the gate.
                ticket = await app.find_ticket(record['ticket_id'])
                if automatic and not permits_automatic_integration(ticket, app.config.settings):
                    return record
                op = await app.store.one("SELECT * FROM operations WHERE id=?", (operation_id,))
                if not op:
                    await app.store.insert("operations", {"id":operation_id,"kind":"integration_apply","owner_id":identity,
                        "data":encode({"target":expected_target,"candidate":expected_candidate,
                                       "origin":decode(record['report'],{}).get('completion_origin')}),"status":"prepared","created":now(),"updated":now()})
                await app.store.update("integrations", identity, state="applying", updated=now())
                apply_task = asyncio.create_task(app.git.run("merge", "--ff-only", expected_candidate))
            try:
                await settle(apply_task)
                if await app.git.head() != expected_candidate:
                    raise Conflict("Target did not reach the expected candidate; inspect Git before retrying.")
                await self._mark_integrated(record, operation_id)
                task = asyncio.current_task()
                self._finishing_tasks.add(task)
                task.add_done_callback(self._finishing_tasks.discard)
            except BaseException:
                await app.store.update("integrations", identity, state="uncertain", error="Apply outcome needs reconciliation. Do not blindly repeat integration.", updated=now())
                raise
        # Cleanup failure must not undo a successful integration or a Done ticket.
        await self.cleanup(identity, _owned=True)
        return await app.store.get("integrations", identity)

    async def _mark_integrated(self, record, operation_id):
        def finish(c):
            c.execute("UPDATE integrations SET state='integrated',error=NULL,updated=? WHERE id=?", (now(),record["id"]))
            c.execute("UPDATE runs SET state='integrated',updated=? WHERE id=?", (now(),record["run_id"]))
            c.execute("UPDATE operations SET status='completed',result=?,updated=? WHERE id=?", (encode({"candidate":record["candidate_oid"]}),now(),operation_id))
            origin = decode(record['report'],{}).get('completion_origin')
            if origin:
                c.execute("INSERT INTO operations(id,kind,owner_id,data,status,created,updated) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                          ('integration-board:'+record['id'],'integration_completion',record['id'],encode({'origin':origin}),'pending',now(),now()))
        await self.app.store.transaction(finish)
        self.app.events.emit(Event("TicketIntegrated",record["ticket_id"],record["run_id"],data={"candidate":record["candidate_oid"]}))

    async def _complete_board(self, record):
        origin = decode(record['report'],{}).get('completion_origin')
        if origin:
            try:
                result = await self.app.move_ticket(record['ticket_id'],'Done',completion_origin=origin,
                    request_id='integration-complete:'+record['id'],activate=False)
                await self.app.store.activity('IntegrationBoardCompletion',result['movement'],record['ticket_id'])
                await self.app.store.execute("UPDATE operations SET status='completed',result=?,updated=? WHERE id=?",
                    (encode(result['movement']),now(),'integration-board:'+record['id']))
            except Conflict as exc:
                # A concurrent manual movement owns the next board transition.
                # The successful Git outcome remains authoritative and visible.
                await self.app.store.activity('IntegrationBoardCompletionDeferred',{'integration_id':record['id'],'reason':str(exc)},record['ticket_id'])

    async def cleanup(self, identity: str, *, _owned=False) -> dict:
        if _owned:
            return await self._cleanup(identity)
        record = await self.app.store.get('integrations',identity)
        owner = 'cleanup:' + identity
        async with self.app.coordinator.activity(record['ticket_id'],owner,'cleanup',cancellable=False):
            return await settle(self._cleanup(identity))

    async def _cleanup(self, identity: str) -> dict:
        app = self.app
        record = await app.store.get("integrations",identity)
        if record["state"] != "integrated": raise AgileCodeError("Cleanup is only available after successful integration.")
        run = await app.store.get("runs",record["run_id"])
        problems = []
        runtime = app.runtimes.get(run["id"])
        if runtime:
            await settle(runtime.shell.close()); await settle(runtime.mcp.close())
        settings = app.config.settings
        if settings.remove_worktree_after_merge and Path(run["workspace"]).exists():
            try: await app.git.remove_worktree(Path(run["workspace"]),run["final_oid"])
            except Exception as exc: problems.append("Ticket worktree retained: "+str(exc))
        if settings.remove_checkpoints_after_merge:
            try: await app.checkpoints.cleanup(run["ticket_id"])
            except Exception as exc: problems.append("Checkpoint cleanup: "+str(exc))
        if settings.delete_branch_after_merge:
            try:
                actual = await app.git.run("rev-parse","--verify",f"refs/heads/{run['branch']}",check=False)
                if actual.code == 0:
                    if actual.text != run["final_oid"]: raise Conflict("Ticket branch moved after the reviewed commit.")
                    await app.git.run("branch","-d",run["branch"])
            except Exception as exc: problems.append("Branch retained: "+str(exc))
        if Path(record["workspace"]).exists():
            try: await app.git.remove_worktree(Path(record["workspace"]),record["candidate_oid"])
            except Exception as exc: problems.append("Integration worktree retained: "+str(exc))
        # Candidate commit is now reachable from target; keep DB lineage, remove private ref.
        ref = f"refs/agilecode/integrations/{identity}"
        ref_oid = await app.git.run("rev-parse","--verify",ref,check=False)
        if ref_oid.code == 0 and ref_oid.text == record["candidate_oid"]:
            await app.git.run("update-ref","-d",ref,record["candidate_oid"])
        report = decode(record["report"],{})
        report["cleanup"] = {"completed_at":now(),"problems":problems,"branch_retained":not settings.delete_branch_after_merge}
        await app.store.update("integrations",identity,report=encode(report),updated=now())
        app.events.emit(Event("CleanupFinished",run["ticket_id"],run["id"],data=report["cleanup"]))
        return report["cleanup"]

    async def recover(self):
        app = self.app
        for record in await app.store.query("SELECT * FROM integrations WHERE state IN ('applying','uncertain')"):
            if record['ticket_id'] in app.coordinator.blocked_tickets:
                continue
            if await self._target(record["target_branch"]) == record["candidate_oid"]:
                await self._mark_integrated(record,"integrate:"+record["id"])
                await self._complete_board(record)
            else:
                await app.store.update("integrations",record["id"],state="blocked",error="Interrupted apply did not leave the recorded target OID. Inspect Git and rebuild deliberately.")
        for record in await app.store.query("SELECT * FROM integrations WHERE state='preparing'"):
            if record['ticket_id'] not in app.coordinator.blocked_tickets:
                await app.store.update('integrations', record['id'], state='blocked',
                    error='Engine stopped while preparing candidate. Candidate files and attempts retained; rebuild deliberately.')
        for op in await app.store.query("SELECT * FROM operations WHERE kind='integration_completion' AND status='pending'"):
            record = await app.store.get('integrations',op['owner_id'])
            if record['state'] == 'integrated' and record['ticket_id'] not in app.coordinator.blocked_tickets:
                await self._complete_board(record)

    async def close(self):
        for runtime in self.runtimes.values():
            await settle(runtime.shell.close()); await settle(runtime.mcp.close())
        self.runtimes.clear()
