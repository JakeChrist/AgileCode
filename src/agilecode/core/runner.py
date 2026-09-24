"""Program-controlled process sequence and fresh, durable agent sessions."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .types import (AgileCodeError, Conflict, NeedsReview, StepResult, ResultStatus, Step, StepContext,
                    Team, Process, ModelProfile, Settings, Events, Event, now, uid)
from .context import PromptBuilder, ContextManager, requirement_ids
from .tools import ToolExecutor
from agilecode.adapters.store import ProjectStore, encode, decode
from agilecode.adapters.git import GitRepository
from agilecode.adapters.workspace import Workspace, digest
from agilecode.adapters.shell import ShellService
from agilecode.adapters.skills import SkillRegistry
from agilecode.adapters.mcp import McpService
from agilecode.adapters.model import OpenAICompatibleClient
from agilecode.adapters.checkpoints import CheckpointService


@dataclass
class RunControl:
    permit: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False
    on_pause: Callable[[], Awaitable[None]] | None = None
    def __post_init__(self):
        self.permit.set()
    async def check(self):
        if self.cancelled:
            raise asyncio.CancelledError
        if not self.permit.is_set() and self.on_pause:
            await self.on_pause()
        await self.permit.wait()
        if self.cancelled:
            raise asyncio.CancelledError


@dataclass
class Runtime:
    run_id: str
    workspace: Workspace
    shell: ShellService
    mcp: McpService
    control: RunControl = field(default_factory=RunControl)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class AgentRunner:
    def __init__(self, store: ProjectStore, client: OpenAICompatibleClient, events: Events):
        self.store, self.client, self.events = store, client, events
        self.context_manager = ContextManager()

    async def _dispatch_pending(self, context: StepContext, executor: ToolExecutor, control: RunControl) -> StepResult | None:
        session_id = context.attempt["session_id"]
        messages = await self.store.messages(session_id)
        for index, message in enumerate(messages):
            calls = message["body"].get("tool_calls", [])
            if not calls:
                continue
            # Results are scoped to this assistant response, not globally to a
            # provider's call ID (servers are allowed to reuse IDs across turns).
            result_ids = set()
            for following in messages[index+1:]:
                if following["body"]["role"] != "tool":
                    break
                result_ids.add(following["body"].get("tool_call_id"))
            mixed = len(calls)>1 and any(c["function"]["name"] in {"finish_step", "ask_user"} for c in calls)
            for call in calls:
                if call["id"] in result_ids:
                    if call["function"]["name"] == "finish_step":
                        record = await self.store.one("SELECT result FROM tools WHERE request_id=? AND call_id=?", (message["request_id"], call["id"]))
                        result = decode(record["result"], {}) if record else {}
                        if result.get("status") == "ok":
                            return StepResult.model_validate(result["data"])
                    continue
                await control.check()
                if mixed:
                    payload = {"status": "error", "error": {"code": "mixed_control_batch", "message": "ask_user and finish_step must be the only call in a response. No calls in this batch executed."}}
                    await self.store.message(session_id, {"role": "tool", "tool_call_id": call["id"], "content": encode(payload)}, message["request_id"])
                    continue
                outcome = await executor.execute(call, message["request_id"])
                content = encode(outcome.payload)
                if len(content.encode()) > 300000:
                    # Persist the original structured outcome in tools; provide a
                    # paged artifact rather than dropping it or blocking the next turn.
                    identity = uid(); path = executor.shell.artifact_root / f"{identity}.json"
                    await asyncio.to_thread(path.write_text, content, encoding="utf-8")
                    await self.store.insert("artifacts", {"id": identity, "ticket_id": context.ticket["id"], "name": "Large tool result",
                        "path": str(path), "size": len(content.encode()), "hash": digest(content.encode()), "complete": 1, "created": now()})
                    content = encode({"status": outcome.payload["status"], "_execution_id": outcome.payload.get("_execution_id"),
                                      "partial": True, "artifact_id": identity, "preview": content[:40000]})
                await self.store.message(session_id, {"role": "tool", "tool_call_id": call["id"], "content": content}, message["request_id"])
                if outcome.step_result:
                    return outcome.step_result
        return None

    async def run(self, context: StepContext, executor: ToolExecutor, profile: ModelProfile,
                  control: RunControl, prompt: str, max_requests: int) -> StepResult:
        session = context.attempt["session_id"]
        if not await self.store.messages(session):
            await self.store.message(session, {"role": "system", "content": prompt})
            await self.store.message(session, {"role": "user", "content": "Perform the assigned step. Use finish_step alone when its actual completion conditions are met."})
        while True:
            await control.check()
            pending_result = await self._dispatch_pending(context, executor, control)
            if pending_result:
                return pending_result
            budget_session = context.extra.get("budget_session_id", session)
            count = await self.store.one("SELECT COUNT(*) n FROM requests r LEFT JOIN attempts a ON a.session_id=r.session_id WHERE r.session_id=? OR json_extract(a.context,'$.extra.budget_session_id')=?", (budget_session, budget_session))
            if count["n"] >= max_requests:
                raise NeedsReview("The configured request budget for this attempt is exhausted. Inspect it and raise the budget deliberately to continue.")
            # Feedback is appended only after outstanding call/results are closed.
            def deliver(c):
                rows = c.execute("SELECT * FROM activity WHERE ticket_id=? AND kind='PendingFeedback' ORDER BY id", (context.ticket["id"],)).fetchall()
                for row in rows:
                    data = decode(row["data"])
                    if data.get("run_id") != context.run["id"]: continue
                    seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE session_id=?", (session,)).fetchone()[0]
                    c.execute("INSERT INTO messages(id,session_id,seq,body,created) VALUES(?,?,?,?,?)", (uid(),session,seq,encode({"role":"user","content":"USER FEEDBACK: "+data["text"]}),now()))
                    c.execute("UPDATE activity SET kind='FeedbackDelivered' WHERE id=?", (row["id"],))
            await self.store.transaction(deliver)
            records = await self.store.messages(session)
            model_messages = [dict(r["body"]) for r in records]
            feedback = await self.store.query("SELECT data FROM activity WHERE ticket_id=? AND kind='FeedbackDelivered' ORDER BY id", (context.ticket["id"],))
            pinned_feedback = [decode(r["data"])["text"] for r in feedback if decode(r["data"]).get("run_id") == context.run["id"]]
            if pinned_feedback and model_messages and model_messages[0]["role"] == "system":
                model_messages[0]["content"] += "\n\nUSER FEEDBACK (preserved across compaction and steps):\n" + "\n".join(pinned_feedback)
            messages, budget = self.context_manager.prepare(model_messages, profile)
            request_id = uid()
            await self.store.insert("requests", {"id": request_id, "session_id": session, "model": profile.model or "",
                "input": encode({"messages": messages, "tools": executor.registry.exposed(), "budget": budget,
                                 "profile": profile.model_dump(mode="json")}), "status": "streaming", "created": now()})
            partial: list[str] = []
            def delta(kind, value):
                if kind == "text": partial.append(value)
                self.events.emit(Event({"text":"ModelTextDelta", "reasoning":"ModelReasoningDelta", "tool_delta":"ModelToolDelta"}[kind],
                    context.ticket["id"], context.run["id"], context.attempt["id"], session,
                    {"request_id": request_id, "value": value}))
            try:
                turn = await self.client.stream(profile, messages, executor.registry.exposed(), delta)
                await control.check()
                await self.store.message(session, turn.message, request_id)
                await self.store.update("requests", request_id, status="completed", response_model=turn.model,
                                        usage=encode(turn.usage) if turn.usage is not None else None, finished=now())
            except BaseException as exc:
                if partial:
                    await self.store.message(session, {"role": "assistant", "content": "".join(partial)}, request_id, partial=True)
                await self.store.update("requests", request_id, status="interrupted", error=str(exc) or type(exc).__name__, finished=now())
                raise
            self.events.emit(Event("ConversationChanged", context.ticket["id"], context.run["id"], context.attempt["id"], session))
            if not turn.message.get("tool_calls"):
                if count["n"] >= 2 and all(not r["body"].get("tool_calls") for r in records[-4:] if r["body"]["role"] == "assistant"):
                    raise NeedsReview("The model repeatedly returned prose instead of a native completion/tool call. Check model tool support and inspect this session.")
                await self.store.message(session, {"role": "user", "content": "Your step is not complete until finish_step returns an accepted structured result. Use native tool calling, or ask_user for missing intent."})


class ProcessRunner:
    def __init__(self, store: ProjectStore, git: GitRepository, client: OpenAICompatibleClient,
                 skills: SkillRegistry, checkpoints: CheckpointService, events: Events,
                 settings: Callable[[], Settings], wait: Callable[..., Awaitable[str]],
                 before_boundary: Callable[[Runtime], Awaitable[None]]):
        self.store, self.git, self.client = store, git, client
        self.skills, self.checkpoints, self.events, self.settings = skills, checkpoints, events, settings
        self.wait, self.before_boundary = wait, before_boundary
        self.agent = AgentRunner(store, client, events)
        self.prompt_builder = PromptBuilder()

    async def outputs(self, run: dict) -> dict[str, StepResult]:
        output = {}
        for step_id, attempt_id in decode(run["outputs"], {}).items():
            attempt = await self.store.get("attempts", attempt_id)
            if attempt["result"]:
                output[step_id] = StepResult.model_validate_json(attempt["result"])
        return output

    async def new_attempt(self, run: dict, team: Team, process: Process, step: Step,
                          phase="development", extra=None, workspace: Path | None = None) -> tuple[dict, StepContext, ModelProfile]:
        member = team.member(step.team_member_id)
        settings = self.settings()
        profile_id = member.model_override or settings.active_profile
        if profile_id not in settings.profiles:
            raise AgileCodeError(f"Member {member.name} overrides missing profile {profile_id!r}; no fallback was substituted.")
        profile = await self.client.resolve(settings.profiles[profile_id])
        latest = await self.store.one("SELECT COALESCE(MAX(number),0)+1 n FROM attempts WHERE run_id=? AND step_id=? AND phase=?", (run["id"], step.id, phase))
        workspace = workspace or Path(run["workspace"])
        fingerprint = await self.git.snapshot_tree(workspace)
        attempt = {"id": uid(), "run_id": run["id"], "step_id": step.id, "number": latest["n"], "session_id": uid(),
            "member": member.model_dump_json(), "model": profile.model_dump_json(), "status": "running", "result": None,
            "fingerprint": fingerprint, "phase": phase, "context": "{}", "created": now()}
        outputs = await self.outputs(run)
        ticket = decode(run["snapshot"])["ticket"]
        previous = next(reversed(outputs.values())).output if outputs else None
        ctx = StepContext(ticket, run, attempt, team, process, workspace, outputs, previous, run.get("correction"),
                          self.execute_step, phase=phase, extra=extra or {})
        saved = {"ticket": ticket, "outputs": {k:v.model_dump(mode="json") for k,v in outputs.items()},
                 "previous_output": previous, "correction_report": run.get("correction"), "extra": extra or {},
                 "workspace": str(workspace), "requested_profile": profile_id, "start_tree": fingerprint,
                 "step": step.model_dump(mode="json")}
        attempt["context"] = encode(saved)
        await self.store.insert("attempts", attempt)
        self.events.emit(Event("StepAttemptStarted", run["ticket_id"], run["id"], attempt["id"], attempt["session_id"],
                               {"step": step.name, "number": latest["n"], "model": profile.model, "phase": phase}))
        return attempt, ctx, profile

    async def _gate(self, ctx: StepContext, step: Step, runtime: Runtime, result: StepResult):
        if runtime.shell.has_live_jobs():
            raise AgileCodeError("Stop live jobs before completing the step so its source state can be assessed.")
        if result.status != ResultStatus.PASS:
            return
        current_tree = await self.git.snapshot_tree(ctx.workspace)
        start_tree = decode(ctx.attempt["context"])["start_tree"]
        if step.kind not in {"implement", "commit"} and current_tree != start_tree:
            raise Conflict("Source changed during a read-only/verification stage. Return FAIL/CHANGES_REQUIRED so implementation and downstream checks rerun.")
        if step.kind == "requirements" and not requirement_ids(result.output):
            raise AgileCodeError("Requirements must include stable REQ-1, REQ-2, ... identifiers and verification methods.")
        if step.kind == "verify":
            required = []
            for s in ctx.process.steps:
                if s.kind == "requirements" and s.id in ctx.outputs:
                    required += requirement_ids(ctx.outputs[s.id].output)
            if not required:
                raise AgileCodeError("Verification has no recorded requirement identifiers.")
            rows = await self.store.query("SELECT * FROM evidence WHERE attempt_id=? ORDER BY created,id", (ctx.attempt["id"],))
            last = {r["requirement"]:r for r in rows}
            missing = [r for r in required if r not in last or last[r]["status"] != "PASS" or last[r]["fingerprint"] != current_tree]
            if missing:
                raise AgileCodeError("Missing current passing recorded evidence: " + ", ".join(missing))
            for command in self.settings().verification_commands:
                jobs = await self.store.query("SELECT * FROM jobs WHERE attempt_id=? AND command=? ORDER BY created DESC", (ctx.attempt["id"], command))
                if not jobs or jobs[0]["exit_code"] != 0 or jobs[0]["status"] != "completed":
                    raise AgileCodeError("Required verification command has not passed: " + command)
        if step.kind == "review":
            rows = await self.store.query("SELECT * FROM tools WHERE session_id=? AND name='git_diff' AND status='completed'", (ctx.attempt["session_id"],))
            if not any(decode(r["result"], {}).get("status") == "ok" for r in rows):
                raise AgileCodeError("Code review must inspect git_diff before passing.")
        if step.kind == "commit":
            run = await self.store.get("runs", ctx.run["id"])
            if not run["final_oid"] or await self.git.head(ctx.workspace) != run["final_oid"] or current_tree != run["final_tree"]:
                raise AgileCodeError("Step 8 can pass only after final_commit reports the verified final ticket commit.")

    async def _commit(self, ctx: StepContext, message: str) -> dict:
        run = await self.store.get("runs", ctx.run["id"])
        if run["final_oid"]:
            if await self.git.head(ctx.workspace) == run["final_oid"] and await self.git.snapshot_tree(ctx.workspace) == run["final_tree"]:
                return {"commit": run["final_oid"], "message": "Previously committed; verified without a duplicate commit."}
            raise Conflict("Recorded final commit does not match current ticket HEAD.")
        current = await self.git.snapshot_tree(ctx.workspace)
        outputs = decode(run["outputs"], {})
        for kind in ("verify", "review"):
            stages = [s for s in ctx.process.steps if s.kind == kind]
            if not stages:
                raise AgileCodeError(f"A commit requires a preceding {kind} step.")
            step_id = stages[-1].id
            if step_id not in outputs:
                raise AgileCodeError(f"No current passed {kind} result.")
            prior = await self.store.get("attempts", outputs[step_id])
            if not prior["result"] or decode(prior["result"])["status"] != "PASS" or prior["fingerprint"] != current:
                raise Conflict(f"The {kind} evidence is missing or stale for this source tree.")
        operation_id = f"final-commit:{run['id']}"
        op = await self.store.one("SELECT * FROM operations WHERE id=?", (operation_id,))
        if not op:
            await self.store.insert("operations", {"id": operation_id, "kind": "final_commit", "owner_id": run["id"],
                "data": encode({"base_oid": run["base_oid"], "expected_tree": current, "branch": run["branch"], "message": message}),
                "status": "prepared", "created": now(), "updated": now()})
        oid = await self.git.final_commit(ctx.workspace, run["branch"], run["base_oid"], current, message)
        def persist(c):
            c.execute("UPDATE runs SET final_oid=?,final_tree=?,updated=? WHERE id=?", (oid, current, now(), run["id"]))
            c.execute("UPDATE operations SET status='completed',result=?,updated=? WHERE id=?", (encode({"oid": oid}), now(), operation_id))
        await self.store.transaction(persist)
        return {"commit": oid, "tree": current, "branch": run["branch"], "message": message}

    async def execute_step(self, step: Step, ctx: StepContext) -> StepResult:
        runtime: Runtime = ctx.extra.pop("_runtime")  # Runtime-only handle, never persisted in prompts.
        profile = ModelProfile.model_validate_json(ctx.attempt["model"])
        await runtime.mcp.discover()
        async def wait(tool_id, kind, prompt, binding):
            return await self.wait(ctx, runtime, tool_id, kind, prompt, binding)
        async def gate(result):
            async with runtime.write_lock:
                await self._gate(ctx, step, runtime, result)
        async def commit(message):
            return await self._commit(ctx, message)
        async def delegate(member_id, instructions, parent_tools):
            if member_id not in {m.id for m in ctx.team.members}:
                raise AgileCodeError("Helpers must be members of the current team.")
            phase = "helper:" + ctx.attempt["id"]
            count = await self.store.one("SELECT COUNT(*) n FROM attempts WHERE phase=?", (phase,))
            if count["n"] >= 8:
                raise NeedsReview("This attempt has used its eight read-only helper slots; continue directly or pause for review.")
            team = ctx.team.model_copy(deep=True)
            member = team.member(member_id)
            safe = {"read_file", "list_files", "code_search", "git_status", "git_diff", "git_log", "read_artifact", "read_job_output", "load_skill", "ask_user", "finish_step"}
            # Intersection, not escalation: a helper only sees the parent's read tools.
            member.allowed_tools = sorted(parent_tools & safe)
            helper = Step(id="helper_" + uid()[:12], name="Helper analysis", kind="analysis", team_member_id=member_id,
                          prompt=instructions + "\nReturn your analysis to the parent; do not change source or claim the parent step is complete.")
            extra = {"parent_attempt_id":ctx.attempt["id"], "budget_session_id":ctx.extra.get("budget_session_id",ctx.attempt["session_id"])}
            if "diff_base" in ctx.extra: extra["diff_base"] = ctx.extra["diff_base"]
            attempt, helper_context, _ = await self.new_attempt(ctx.run, team, ctx.process, helper, phase=phase, extra=extra, workspace=ctx.workspace)
            helper_context.extra["_runtime"] = runtime
            try:
                result = await helper.run(helper_context)
                await self._gate(helper_context, helper, runtime, result)
                await self.store.update("attempts",attempt["id"],status="completed",result=result.model_dump_json(),finished=now())
                return {"attempt_id":attempt["id"], "session_id":attempt["session_id"], "result":result.model_dump(mode="json")}
            except BaseException:
                await self.store.execute("UPDATE attempts SET status='interrupted' WHERE id=? AND status IN ('running','waiting')",(attempt["id"],))
                raise
        executor = ToolExecutor(ctx, step, runtime.workspace, self.git, self.store, runtime.shell,
            self.skills, runtime.mcp, self.settings, self.events, wait, gate, commit, runtime.write_lock, delegate=delegate)
        catalog = await asyncio.to_thread(self.skills.catalog, ctx.team.member(step.team_member_id).skills)
        prompt = self.prompt_builder.build(step, ctx, catalog)
        if runtime.mcp.errors:
            prompt += "\n\nMCP DISCOVERY ERRORS (native tools remain available):\n" + encode(runtime.mcp.errors)
        if self.settings().verification_commands and step.kind == "verify":
            prompt += "\n\nREQUIRED VERIFICATION COMMANDS (run each using run_command):\n" + "\n".join(self.settings().verification_commands)
        # Exact prompts are persisted by AgentRunner as the first canonical message.
        return await self.agent.run(ctx, executor, profile, runtime.control, prompt, self.settings().max_requests_per_attempt)

    async def run(self, runtime: Runtime) -> None:
        while True:
            await self.before_boundary(runtime)
            run = await self.store.get("runs", runtime.run_id)
            snapshot = decode(run["snapshot"])
            team = Team.model_validate(snapshot["team"])
            process = team.process(snapshot["process_id"])
            if run["step_index"] >= len(process.steps):
                await self.store.update("runs", run["id"], state="completed", updated=now(), error=None)
                self.events.emit(Event("ExecutionCompleted", run["ticket_id"], run["id"]))
                return
            step = process.steps[run["step_index"]]
            await self.git.verify_worktree(runtime.workspace.root, run["branch"])
            attempt = await self.store.one("SELECT * FROM attempts WHERE run_id=? AND step_id=? AND phase='development' AND status IN ('running','waiting','interrupted','paused') ORDER BY number DESC LIMIT 1", (run["id"], step.id))
            if attempt:
                saved = decode(attempt["context"])
                ctx = StepContext(snapshot["ticket"], run, attempt, team, process, runtime.workspace.root,
                    {k: StepResult.model_validate(v) for k,v in saved["outputs"].items()}, saved["previous_output"], saved["correction_report"],
                    self.execute_step, extra=saved.get("extra", {}))
                await self.store.update("attempts", attempt["id"], status="running")
            else:
                if step.kind == "implement":
                    async with runtime.write_lock:
                        await self.checkpoints.capture(run, "Before implementation/correction")
                attempt, ctx, _ = await self.new_attempt(run, team, process, step)
            ctx.extra["_runtime"] = runtime
            result = await step.run(ctx)
            async with runtime.write_lock:
                await self._gate(ctx, step, runtime, result)
                fingerprint = await self.git.snapshot_tree(runtime.workspace.root)
                current_outputs = decode(run["outputs"], {})
                current_outputs[step.id] = attempt["id"]
                next_index = run["step_index"] + 1
                corrections, correction, state = run["corrections"], None, "running"
                if result.status != ResultStatus.PASS:
                    if result.status in {ResultStatus.FAIL, ResultStatus.CHANGES_REQUIRED} and step.correction_target:
                        next_index = next(i for i,s in enumerate(process.steps) if s.id == step.correction_target)
                        corrections += 1
                        correction = f"{step.name}: {result.summary}\n{result.output}"
                        # Retain earlier implementation output for the correction handoff,
                        # but invalidate all downstream success pointers.
                        for s in process.steps[next_index+1:]:
                            current_outputs.pop(s.id, None)
                        if corrections >= self.settings().max_corrections:
                            state = "paused"
                    else:
                        next_index = run["step_index"]
                        state = "paused"
                        correction = result.output
                def complete(c):
                    c.execute("UPDATE attempts SET status='completed',result=?,fingerprint=?,finished=? WHERE id=?", (result.model_dump_json(), fingerprint, now(), attempt["id"]))
                    c.execute("UPDATE runs SET outputs=?,step_index=?,state=?,corrections=?,correction=?,error=?,updated=? WHERE id=?",
                              (encode(current_outputs), next_index, state, corrections, correction,
                               "Paused for review of the step result/correction budget." if state == "paused" else None, now(), run["id"]))
                await self.store.transaction(complete)
            self.events.emit(Event("StepAttemptCompleted", run["ticket_id"], run["id"], attempt["id"], attempt["session_id"],
                                   {"step": step.name, "result": result.model_dump(mode="json"), "next_index": next_index}))
            if state == "paused":
                return
