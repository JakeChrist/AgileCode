"""Program-controlled process sequence and fresh, durable agent sessions."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .types import (AgileCodeError, Conflict, NeedsReview, GenerationStopped, StepResult, ResultStatus, Step, StepContext,
                    Team, Process, ModelProfile, ProviderConfig, Settings, Events, Event, now, uid)
from .context import PromptBuilder, requirement_ids
from agilecode.runtime.conversation import ConversationConfig, ConversationEngine
from agilecode.runtime import conversation_records
from agilecode.runtime import tools as toolset
from agilecode.runtime.tool_factories import ticket_tools
from agilecode.adapters.store import ProjectStore, encode, decode
from agilecode.adapters.git import GitRepository
from agilecode.adapters.workspace import Workspace, digest
from agilecode.adapters.shell import ShellService
from agilecode.adapters.skills import SkillRegistry
from agilecode.adapters.mcp import McpService
from agilecode.adapters.model import OpenAICompatibleClient, ContextWindowChanged
from agilecode.adapters.checkpoints import CheckpointService


@dataclass
class RunControl:
    permit: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False
    def __post_init__(self):
        self.permit.set()
    async def check(self):
        if self.cancelled:
            raise asyncio.CancelledError
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
    environment: dict = field(default_factory=dict)
    prepared_shell_generation: int = 0


class AgentRunner:
    """Ticket-specific prompt, events and completion policy for the shared engine."""
    def __init__(self, store: ProjectStore, client: OpenAICompatibleClient, events: Events):
        self.store, self.client, self.events = store, client, events
        self.engine = ConversationEngine(store, client)
        self.context_manager = self.engine.context
        self.active_generations = self.engine.active_generations
        self.stop_requested = self.engine.stop_requested

    async def stop_run(self, run_id: str) -> str | None:
        return self.engine.interrupt_tag(run_id, reason="stop")

    def _config(self, context, manager, profile, control, prompt, initial_history=()):
        session = context.attempt["session_id"]
        def emit(kind, data):
            events = {"started": "ModelGenerationStarted", "finished": "ModelGenerationFinished", "changed": "ConversationChanged",
                      "turn_started": "ConversationTurnStarted", "turn_finished": "ConversationTurnFinished",
                      "turn_invalidated": "ConversationTurnInvalidated",
                      "technical_failure": "ExecutionTechnicalFailure",
                      "technical_recovered": "ExecutionTechnicalRecovered"}
            if kind == "delta":
                name = {"text": "ModelTextDelta", "reasoning": "ModelReasoningDelta", "tool_delta": "ModelToolDelta"}[data["kind"]]
            else:
                name = events[kind]
            self.events.emit(Event(name, context.ticket["id"], context.run["id"], context.attempt["id"], session,
                                   {k: v for k, v in data.items() if k != "task"}))
        async def initialize():
            if not await self.store.messages(session):
                await self.store.message(session, {"role": "system", "content": str(prompt)})
                for body in initial_history:
                    await self.store.message(session, body)
                source = context.ticket.get("source_discussion_context")
                if source and source.get("transcript"):
                    await self.store.message(session, {"role": "user", "content":
                        "SOURCE PROJECT DISCUSSION — " + source.get("title", "Discussion") +
                        "\nThis is read-only background that preceded ticket creation; the ticket and current process remain authoritative.\n\n" + source["transcript"]})
                await self.store.message(session, {"role": "user", "content": "Perform the assigned step. Use finish_step alone when its actual completion conditions are met."})
        async def view(messages):
            # Promote pre-receipt legacy feedback without losing it. Newly accepted
            # receipts are already included by the engine, so never append twice.
            pending = await self.store.query("SELECT * FROM activity WHERE ticket_id=? AND kind='PendingFeedback' ORDER BY id", (context.ticket["id"],))
            for row in pending:
                data = decode(row["data"])
                if data.get("run_id") == context.run["id"] and data.get("receipt_id") and data.get("session_id") != session:
                    routed = await conversation_records.accept_input(self.store, session, "USER FEEDBACK: " + data["text"], data.get("input_id") or str(row["id"]))
                    await self.store.update("operations", data["receipt_id"], status="transferred",
                        result=encode({"status": "routed", "session_id": session, "receipt_id": routed["id"]}), updated=now())
                    data.update(session_id=session, receipt_id=routed["id"])
                    await self.store.execute("UPDATE activity SET data=? WHERE id=?", (encode(data), row["id"]))
            def deliver(c):
                for row in c.execute("SELECT * FROM activity WHERE ticket_id=? AND kind='PendingFeedback' ORDER BY id", (context.ticket["id"],)).fetchall():
                    data = decode(row["data"])
                    if data.get("run_id") != context.run["id"]:
                        continue
                    if not data.get("receipt_id"):
                        conversation_records.append_message_in(c, session, {"role": "user", "content": "USER FEEDBACK: " + data["text"]})
                    c.execute("UPDATE activity SET kind='FeedbackDelivered' WHERE id=?", (row["id"],))
            await self.store.transaction(deliver)
            messages = [dict(r["body"]) for r in await self.store.messages(session) if r["body"].get("role") != "error"]
            feedback = await self.store.query("SELECT data FROM activity WHERE ticket_id=? AND kind='FeedbackDelivered' ORDER BY id", (context.ticket["id"],))
            pinned = [decode(r["data"])["text"] for r in feedback if decode(r["data"]).get("run_id") == context.run["id"]]
            if pinned and messages and messages[0]["role"] == "system":
                messages[0]["content"] += "\n\nUSER FEEDBACK (preserved across compaction and steps):\n" + "\n".join(pinned)
            return messages
        async def restored():
            if context.phase == "development":
                run = await self.store.get("runs", context.run["id"])
                if run["state"] == "running" and (run["error"] or "").startswith("Provider temporarily unavailable;"):
                    await self.store.update("runs", run["id"], error=None, updated=now())
                    self.events.emit(Event("RunChanged", context.ticket["id"], run["id"], data={"message": "Provider connection restored."}))
        return ConversationConfig(session, profile=lambda: profile, tools=lambda: manager,
            initialize=initialize, request_view=view, check=control.check, emit=emit,
            after_generation=restored, tag=context.run["id"], mixed_control_names=frozenset({"finish_step", "ask_user"}),
            continuation="Your step is not complete until finish_step returns an accepted structured result. Use native tool calling, or ask_user for missing intent.")

    async def run(self, context: StepContext, manager: toolset.ToolManager, profile: ModelProfile,
                  control: RunControl, prompt: str, initial_history=(), *, prepare=None) -> StepResult:
        initial_history = initial_history or getattr(prompt, "history", ())
        config = self._config(context, manager, profile, control, prompt, initial_history)
        if prepare is not None:
            async def prepared_config():
                manager, profile, prompt, history = await prepare()
                return self._config(context, manager, profile, control, prompt, history)
            config.prepare = prepared_config
        outcome = await self.engine.run_turn(config, trigger=context.attempt["id"])
        if outcome["status"] == "finished":
            return StepResult.model_validate(outcome["step_result"])
        if outcome["status"] == "stopped":
            raise GenerationStopped("Model generation stopped by user.")
        if outcome["status"] == "blocked":
            raise NeedsReview(outcome["content"])
        raise NeedsReview(f"The step did not return an accepted finish_step result (status: {outcome['status']}).")


class ProcessRunner:
    def __init__(self, store: ProjectStore, git: GitRepository, client: OpenAICompatibleClient,
                 skills: SkillRegistry, checkpoints: CheckpointService, events: Events,
                 settings: Callable[[], Settings], provider: Callable[[str | None], ProviderConfig],
                 wait: Callable[..., Awaitable[str]], before_boundary: Callable[[Runtime], Awaitable[None]],
                 create_ticket: Callable[..., Awaitable[dict]]):
        self.store, self.git, self.client = store, git, client
        self.skills, self.checkpoints, self.events, self.settings = skills, checkpoints, events, settings
        self.provider = provider
        self.wait, self.before_boundary, self.create_ticket = wait, before_boundary, create_ticket
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
        snapshot = decode(run["snapshot"])
        ticket = snapshot["ticket"]
        provider_id = snapshot.get("ticket_provider_id") or member.model_override or snapshot.get("provider_id")
        try:
            provider = self.provider(provider_id)
        except AgileCodeError as exc:
            if member.model_override:
                raise AgileCodeError(
                    f"Member {member.name} overrides missing provider {member.model_override!r}; no fallback was substituted."
                ) from exc
            raise
        # Provider resolution belongs to the registered shared conversation,
        # not the process boundary before a session exists.
        profile = provider.profile
        latest = await self.store.one("SELECT COALESCE(MAX(number),0)+1 n FROM attempts WHERE run_id=? AND step_id=? AND phase=?", (run["id"], step.id, phase))
        workspace = workspace or Path(run["workspace"])
        fingerprint = await self.git.snapshot_tree(workspace)
        attempt = {"id": uid(), "run_id": run["id"], "step_id": step.id, "number": latest["n"], "session_id": uid(),
            "member": member.model_dump_json(), "model": profile.model_dump_json(), "status": "running", "result": None,
            "fingerprint": fingerprint, "phase": phase, "context": "{}", "created": now()}
        outputs = await self.outputs(run)
        previous = next(reversed(outputs.values())).output if outputs else None
        ctx = StepContext(ticket, run, attempt, team, process, workspace, outputs, previous, run.get("correction"),
                          self.execute_step, phase=phase, extra=extra or {})
        saved = {"ticket": ticket, "outputs": {k:v.model_dump(mode="json") for k,v in outputs.items()},
                 "output_attempts": decode(run["outputs"], {}), "corrections": run["corrections"],
                 "previous_output": previous, "correction_report": run.get("correction"), "extra": extra or {},
                 "workspace": str(workspace), "requested_provider": provider.id,
                 "requested_profile": provider.id, "start_tree": fingerprint, "base_oid": run["base_oid"],
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
        if step.kind not in {"analysis", "implement", "verify_design", "commit"} and current_tree != start_tree:
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
            rows = await self.store.query("SELECT * FROM tools WHERE session_id=? AND name='git_command' AND status='completed'", (ctx.attempt["session_id"],))
            if not any(
                decode(r["result"], {}).get("status") == "ok"
                and decode(r["result"], {}).get("data", {}).get("exit_code") == 0
                for r in rows
            ):
                raise AgileCodeError("Code review must successfully inspect the Git changes with git_command before passing.")
        if step.kind == "commit":
            run = await self.store.get("runs", ctx.run["id"])
            if not run["final_oid"] or await self.git.head(ctx.workspace) != run["final_oid"] or current_tree != run["final_tree"]:
                raise AgileCodeError("The commit step can pass only after final_commit reports the verified final ticket commit.")

    async def _commit(self, ctx: StepContext, message: str) -> dict:
        run = await self.store.get("runs", ctx.run["id"])
        if run["final_oid"]:
            if await self.git.head(ctx.workspace) == run["final_oid"] and await self.git.snapshot_tree(ctx.workspace) == run["final_tree"]:
                return {"commit": run["final_oid"], "files": await self.git.changed_files(run["base_oid"], run["final_oid"]), "message": "Previously committed; verified without a duplicate commit."}
            raise Conflict("Recorded final commit does not match current ticket HEAD.")
        current = await self.git.snapshot_tree(ctx.workspace)
        outputs = decode(run["outputs"], {})
        assigned = decode(ctx.attempt["context"])["step"]
        if ctx.phase != "development" or assigned["kind"] != "commit":
            raise AgileCodeError("Only the assigned final commit stage may commit ticket deliverables.")
        commit_index = next(i for i, step in enumerate(ctx.process.steps) if step.id == assigned["id"])
        if commit_index != len(ctx.process.steps) - 1:
            raise AgileCodeError("The commit stage must be the final configured process step.")
        predecessors = ctx.process.steps[:commit_index]
        for step in predecessors:
            prior_id = outputs.get(step.id)
            prior = await self.store.get("attempts", prior_id) if prior_id else None
            if (not prior or prior["run_id"] != run["id"] or prior["step_id"] != step.id
                    or prior["status"] != "completed" or not prior["result"]
                    or decode(prior["result"])["status"] != "PASS"):
                raise AgileCodeError(f"No current passed result for configured step {step.name}.")
            # Configured verification/review gates are never waived. For analysis
            # workflows, the final producing stage must describe this exact tree.
            if (step.kind in {"verify", "review"} or step is predecessors[-1]) and prior["fingerprint"] != current:
                raise Conflict(f"The {step.kind} evidence is missing or stale for this source tree.")
        if not predecessors:
            raise AgileCodeError("A commit requires completed work from a preceding process step.")
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
        return {"commit": oid, "tree": current, "branch": run["branch"], "message": message, "files": await self.git.changed_files(run["base_oid"], oid)}

    async def execute_step(self, step: Step, ctx: StepContext) -> StepResult:
        runtime: Runtime = ctx.extra.pop("_runtime")
        async def prepare():
            profile_data = json.loads(ctx.attempt["model"])
            profile_data.pop("context_window", None)
            profile = await self.client.resolve(ModelProfile.model_validate(profile_data))
            await self.store.update("attempts", ctx.attempt["id"], model=profile.model_dump_json())
            ctx.attempt["model"] = profile.model_dump_json()
            await runtime.mcp.discover()
            async def wait(tool_id, kind, prompt, binding):
                return await self.wait(ctx, runtime, tool_id, kind, prompt, binding)
            async def gate(result):
                async with runtime.write_lock:
                    await self._gate(ctx, step, runtime, result)
            async def commit(message):
                return await self._commit(ctx, message)
            member = ctx.team.member(step.team_member_id)
            manager = ticket_tools(self, step, ctx, runtime, wait=wait, gate=gate, commit=commit)
            catalog = await asyncio.to_thread(self.skills.catalog, member.skills)
            prompt = self.prompt_builder.build(step, ctx, catalog)
            initial_history = getattr(prompt, "history", ())
            prompt += "\n\nWORKTREE ENVIRONMENT:\n" + encode(runtime.environment)
            if runtime.mcp.errors:
                prompt += "\n\nMCP DISCOVERY ERRORS (native tools remain available):\n" + encode(runtime.mcp.errors)
            if self.settings().verification_commands and step.kind == "verify":
                prompt += "\n\nREQUIRED VERIFICATION COMMANDS (run each using run_command):\n" + "\n".join(self.settings().verification_commands)
            return manager, profile, prompt, initial_history
        return await self.agent.run(ctx, None, None, runtime.control, "", prepare=prepare)

    async def run(self, runtime: Runtime) -> None:
        while True:
            await self.before_boundary(runtime)
            run = await self.store.get("runs", runtime.run_id)
            snapshot = decode(run["snapshot"])
            team = Team.model_validate(snapshot["team"])
            process = team.process(snapshot["process_id"])
            if run["step_index"] >= len(process.steps):
                if not run["final_oid"]:
                    raise NeedsReview("The process has no final committed deliverable. Resume with a commit stage; retained outputs are unchanged.")
                if (await self.git.head(runtime.workspace.root) != run["final_oid"]
                        or await self.git.snapshot_tree(runtime.workspace.root) != run["final_tree"]):
                    raise Conflict("Completed work no longer matches the final committed deliverable.")
                def complete_run(c):
                    c.execute("UPDATE runs SET state='completed',updated=?,error=NULL WHERE id=?", (now(), run["id"]))
                    return conversation_records.settle_terminal_feedback_in(c, run["id"], run["ticket_id"])
                later_feedback = await self.store.transaction(complete_run)
                if later_feedback:
                    self.events.emit(Event("FeedbackQueued", run["ticket_id"], run["id"],
                        data={"status": "needs_followup", "feedback": later_feedback}))
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
                attempt, ctx, _ = await self.new_attempt(run, team, process, step)
            ctx.extra["_runtime"] = runtime
            result = await self.execute_step(step, ctx)
            if step.kind == "commit" and result.status == ResultStatus.PASS:
                committed = await self.store.get("runs", run["id"])
                if committed["final_oid"]:
                    files = await self.git.changed_files(committed["base_oid"], committed["final_oid"])
                    result = result.model_copy(update={"output": result.output + "\n\nCommitted files:\n" + "\n".join(files)})
            async with runtime.write_lock:
                await self._gate(ctx, step, runtime, result)
                fingerprint = await self.git.snapshot_tree(runtime.workspace.root)
                if fingerprint != decode(attempt["context"])["start_tree"]:
                    await self.checkpoints.capture(
                        run, f"After {step.name} (attempt {attempt['number']}, {result.status})",
                        owner=getattr(runtime, "operation_owner", None),
                    )
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
                        if self.settings().max_corrections is not None and corrections > self.settings().max_corrections:
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
