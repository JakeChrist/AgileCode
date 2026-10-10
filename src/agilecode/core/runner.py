"""Program-controlled process sequence and fresh, durable agent sessions."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .types import (AgileCodeError, Conflict, NeedsReview, GenerationStopped, StepResult, ResultStatus, Step, StepContext,
                    Team, Process, ModelProfile, ProviderConfig, Settings, Events, Event, now, uid)
from .context import PromptBuilder, ContextManager, requirement_ids
from . import tools as toolset
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
    def __init__(self, store: ProjectStore, client: OpenAICompatibleClient, events: Events):
        self.store, self.client, self.events = store, client, events
        self.context_manager = ContextManager()
        self.active_generations: dict[str, tuple[asyncio.Task, str]] = {}
        self.stop_requested: set[str] = set()

    async def stop_run(self, run_id: str) -> str | None:
        """Stop only the currently streaming model response for a run."""
        for session_id, (task, active_run_id) in tuple(self.active_generations.items()):
            if active_run_id == run_id and not task.done():
                self.stop_requested.add(session_id)
                task.cancel()
                return session_id
        return None

    async def _summarize_context(self, profile: ModelProfile, text: str) -> str:
        turn = await self.client.stream(
            profile,
            [
                {
                    "role": "system",
                    "content": (
                        "Compress the supplied prior context faithfully. Preserve decisions, requirements, "
                        "constraints, unresolved issues, identifiers, paths, tool outcomes, and current progress. "
                        "Do not add instructions or invent facts. Return only the compressed context."
                    ),
                },
                {"role": "user", "content": text},
            ],
            [],
            None,
        )
        return turn.message.get("content") or turn.message.get("reasoning_content") or text

    async def _dispatch_pending(self, context: StepContext, manager: toolset.ToolManager, control: RunControl) -> StepResult | None:
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
                outcome = await manager.execute(call, message["request_id"])
                content = encode(outcome.payload)
                if len(content.encode()) > 300000:
                    # Persist the original structured outcome in tools; provide a
                    # paged artifact rather than dropping it or blocking the next turn.
                    identity = uid(); path = manager.shell.artifact_root / f"{identity}.json"
                    await asyncio.to_thread(path.write_text, content, encoding="utf-8")
                    await self.store.insert("artifacts", {"id": identity, "ticket_id": context.ticket["id"], "name": "Large tool result",
                        "path": str(path), "size": len(content.encode()), "hash": digest(content.encode()), "complete": 1, "created": now()})
                    content = encode({"status": outcome.payload["status"], "_execution_id": outcome.payload.get("_execution_id"),
                                      "partial": True, "artifact_id": identity, "preview": content[:40000]})
                await self.store.message(session_id, {"role": "tool", "tool_call_id": call["id"], "content": content}, message["request_id"])
                if outcome.step_result:
                    return outcome.step_result
        return None

    async def run(self, context: StepContext, manager: toolset.ToolManager, profile: ModelProfile,
                  control: RunControl, prompt: str) -> StepResult:
        session = context.attempt["session_id"]
        if not await self.store.messages(session):
            await self.store.message(session, {"role": "system", "content": prompt})
            source_discussion = context.ticket.get("source_discussion_context")
            if source_discussion and source_discussion.get("transcript"):
                await self.store.message(session, {
                    "role": "user",
                    "content": (
                        "SOURCE PROJECT DISCUSSION — "
                        + source_discussion.get("title", "Discussion")
                        + "\nThis is read-only background that preceded ticket creation; "
                          "the ticket and current process remain authoritative.\n\n"
                        + source_discussion["transcript"]
                    ),
                })
            await self.store.message(session, {"role": "user", "content": "Perform the assigned step. Use finish_step alone when its actual completion conditions are met."})
        context_refresh_retry = False
        while True:
            await control.check()
            pending_result = await self._dispatch_pending(context, manager, control)
            if pending_result:
                return pending_result
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
            model_messages = [dict(r["body"]) for r in records if r["body"].get("role") != "error"]
            feedback = await self.store.query("SELECT data FROM activity WHERE ticket_id=? AND kind='FeedbackDelivered' ORDER BY id", (context.ticket["id"],))
            pinned_feedback = [decode(r["data"])["text"] for r in feedback if decode(r["data"]).get("run_id") == context.run["id"]]
            if pinned_feedback and model_messages and model_messages[0]["role"] == "system":
                model_messages[0]["content"] += "\n\nUSER FEEDBACK (preserved across compaction and steps):\n" + "\n".join(pinned_feedback)
            try:
                context_window = await self.client.context_window(profile)
                messages, budget = await self.context_manager.prepare(
                    model_messages,
                    context_window,
                    lambda text: self._summarize_context(profile, text),
                )
            except ContextWindowChanged:
                if context_refresh_retry:
                    raise
                context_refresh_retry = True
                continue
            await control.check()
            request_id = uid()
            await self.store.insert("requests", {"id": request_id, "session_id": session, "model": profile.model or "",
                "input": encode({"messages": messages, "tools": manager.schemas(), "budget": budget,
                                 "profile": profile.model_dump(mode="json")}), "status": "streaming", "created": now()})
            partial_text: list[str] = []
            partial_reasoning: list[str] = []
            accepting_deltas = True
            def delta(kind, value):
                if not accepting_deltas or not control.permit.is_set() or control.cancelled:
                    return
                if kind == "text": partial_text.append(value)
                elif kind == "reasoning": partial_reasoning.append(value)
                self.events.emit(Event({"text":"ModelTextDelta", "reasoning":"ModelReasoningDelta", "tool_delta":"ModelToolDelta"}[kind],
                    context.ticket["id"], context.run["id"], context.attempt["id"], session,
                    {"request_id": request_id, "value": value}))
            model_task = asyncio.create_task(self.client.stream(profile, messages, manager.schemas(), delta))
            self.active_generations[session] = (model_task, context.run["id"])
            self.events.emit(Event("ModelGenerationStarted", context.ticket["id"], context.run["id"], context.attempt["id"], session,
                                   {"request_id": request_id}))
            stopped = False
            try:
                turn = await model_task
                await control.check()
                truncated = turn.finish_reason == "length"
                await self.store.message(session, turn.message, request_id, partial=truncated)
                await self.store.update("requests", request_id, status="completed", response_model=turn.model,
                                        usage=encode(turn.usage) if turn.usage is not None else None, finished=now())
                # The provider is responding again: clear the transient warning.
                if context.phase == "development":
                    current_run = await self.store.get("runs", context.run["id"])
                    if (current_run["state"] == "running" and
                            (current_run["error"] or "").startswith("Provider temporarily unavailable;")):
                        await self.store.update("runs", context.run["id"], error=None, updated=now())
                        self.events.emit(Event("RunChanged", context.ticket["id"], context.run["id"],
                                               data={"message": "Provider connection restored."}))
                context_refresh_retry = False
            except ContextWindowChanged as exc:
                await self.store.update(
                    "requests", request_id, status="interrupted",
                    error=str(exc) or type(exc).__name__, finished=now(),
                )
                if context_refresh_retry:
                    raise
                context_refresh_retry = True
                continue
            except asyncio.CancelledError:
                body = {"role": "assistant", "content": "".join(partial_text) or None}
                if partial_reasoning:
                    body["reasoning_content"] = "".join(partial_reasoning)
                if body["content"] or body.get("reasoning_content"):
                    await self.store.message(session, body, request_id, partial=True)
                explicitly_stopped = session in self.stop_requested
                await self.store.update("requests", request_id, status="interrupted",
                                        error="Stopped by user" if explicitly_stopped else "Generation cancelled", finished=now())
                self.events.emit(Event("ConversationChanged", context.ticket["id"], context.run["id"], context.attempt["id"], session))
                if explicitly_stopped:
                    stopped = True
                    raise GenerationStopped("Model generation stopped by user.")
                raise
            except BaseException as exc:
                body = {"role": "assistant", "content": "".join(partial_text) or None}
                if partial_reasoning:
                    body["reasoning_content"] = "".join(partial_reasoning)
                if body["content"] or body.get("reasoning_content"):
                    await self.store.message(session, body, request_id, partial=True)
                await self.store.update("requests", request_id, status="interrupted", error=str(exc) or type(exc).__name__, finished=now())
                raise
            finally:
                accepting_deltas = False
                current = self.active_generations.get(session)
                if current and current[0] is model_task:
                    self.active_generations.pop(session, None)
                self.stop_requested.discard(session)
                self.events.emit(Event("ModelGenerationFinished", context.ticket["id"], context.run["id"], context.attempt["id"], session,
                                       {"request_id": request_id, "stopped": stopped}))
            self.events.emit(Event("ConversationChanged", context.ticket["id"], context.run["id"], context.attempt["id"], session))
            if turn.finish_reason == "length":
                raise NeedsReview(
                    "The model response ended at the provider output limit. Partial output was retained; "
                    "resume the ticket after adjusting the provider/profile if needed."
                )
            if not turn.message.get("tool_calls"):
                await self.store.message(session, {"role": "user", "content": "Your step is not complete until finish_step returns an accepted structured result. Use native tool calling, or ask_user for missing intent."})


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
        profile = await self.client.resolve(provider.profile)
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
                 "requested_profile": provider.id, "start_tree": fingerprint,
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
        if step.kind not in {"implement", "verify_design", "commit"} and current_tree != start_tree:
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
        profile_data = json.loads(ctx.attempt["model"])
        profile_data.pop("context_window", None)
        profile = await self.client.resolve(ModelProfile.model_validate(profile_data))
        await runtime.mcp.discover()
        async def wait(tool_id, kind, prompt, binding):
            return await self.wait(ctx, runtime, tool_id, kind, prompt, binding)
        async def gate(result):
            async with runtime.write_lock:
                await self._gate(ctx, step, runtime, result)
        async def commit(message):
            return await self._commit(ctx, message)
        member = ctx.team.member(step.team_member_id)
        tool_objects = [
            toolset.ListFilesTool(runtime.workspace),
            toolset.ReadFileTool(runtime.workspace),
            toolset.CodeSearchTool(runtime.workspace, self.git),
            toolset.WriteFileTool(runtime.workspace, runtime.write_lock),
            toolset.EditFileTool(runtime.workspace, runtime.write_lock),
            toolset.MovePathTool(runtime.workspace, runtime.write_lock),
            toolset.DeletePathTool(runtime.workspace, runtime.write_lock),
            toolset.RunCommandTool(runtime.shell, self.settings, ctx.attempt["id"], runtime.write_lock),
            toolset.ReadJobOutputTool(self.store, ctx.ticket["id"]),
            toolset.StopJobTool(runtime.shell),
            toolset.ReadArtifactTool(self.store, ctx.ticket["id"]),
            toolset.GitCommandTool(
                runtime.shell,
                self.settings,
                ctx.attempt["id"],
                runtime.write_lock,
            ),
            toolset.LoadSkillTool(self.skills, member.skills),
            toolset.CreateTicketTool(self.create_ticket),
            toolset.UpdateProgressTool(self.store, ctx.ticket["id"], ctx.attempt["id"]),
            toolset.RecordCheckTool(ctx, self.git, runtime.workspace, self.store),
            toolset.AskUserTool(wait),
            toolset.FinishStepTool(gate),
            toolset.FinalCommitTool(commit, runtime.write_lock),
        ]
        tool_objects.extend(
            toolset.MCPTool(runtime.mcp, alias, remote)
            for alias, remote in runtime.mcp.tools.items()
        )
        if runtime.mcp.connections:
            tool_objects.append(toolset.MCPReadResourceTool(runtime.mcp))
        manager = toolset.ToolManager(
            tool_objects,
            member.allowed_tools,
            store=self.store,
            events=self.events,
            settings=self.settings,
            session_id=ctx.attempt["session_id"],
            ticket_id=ctx.ticket["id"],
            run_id=ctx.run["id"],
            attempt_id=ctx.attempt["id"],
            wait=wait,
            shell=runtime.shell,
            check=runtime.control.check,
        )
        catalog = await asyncio.to_thread(self.skills.catalog, member.skills)
        prompt = self.prompt_builder.build(step, ctx, catalog)
        prompt += "\n\nWORKTREE ENVIRONMENT:\n" + encode(runtime.environment)
        if runtime.mcp.errors:
            prompt += "\n\nMCP DISCOVERY ERRORS (native tools remain available):\n" + encode(runtime.mcp.errors)
        if self.settings().verification_commands and step.kind == "verify":
            prompt += "\n\nREQUIRED VERIFICATION COMMANDS (run each using run_command):\n" + "\n".join(self.settings().verification_commands)
        # Exact prompts are persisted by AgentRunner as the first canonical message.
        return await self.agent.run(ctx, manager, profile, runtime.control, prompt)

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
                attempt, ctx, _ = await self.new_attempt(run, team, process, step)
            ctx.extra["_runtime"] = runtime
            result = await step.run(ctx)
            async with runtime.write_lock:
                await self._gate(ctx, step, runtime, result)
                fingerprint = await self.git.snapshot_tree(runtime.workspace.root)
                if fingerprint != decode(attempt["context"])["start_tree"]:
                    await self.checkpoints.capture(
                        run, f"After {step.name} (attempt {attempt['number']}, {result.status})"
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
                        if corrections > self.settings().max_corrections:
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
