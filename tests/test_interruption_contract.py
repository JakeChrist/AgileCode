"""Independent cross-role interruption contract using real public adapters.

The provider is deterministic; SQLite, role services, execution ownership and
tool dispatch are real. Barriers make forbidden post-interrupt effects observable.
"""
import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agilecode.adapters.model import Turn
from agilecode.adapters.store import decode
from agilecode.core.application import Application
from agilecode.core.types import AgileCodeError, Settings, now
from agilecode.runtime import conversation_records as records
from agilecode.runtime.tools import RunCommandTool, Tool, ToolManager
from conftest import ScriptClient


class HeldRead(Tool):
    name = "contract_read"

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finished = False

    async def execute(self, args, execution_id):
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        self.finished = True
        return {"read": True}


class ForbiddenMutation(Tool):
    name = "contract_mutation"
    uncertain_on_interrupt = True

    def __init__(self):
        self.calls = 0

    async def execute(self, args, execution_id):
        self.calls += 1
        return {"changed": True}


class ContractClient:
    def __init__(self, *, hold_answer=False):
        self.calls = []
        self.hold_answer = hold_answer
        self.answer_entered = asyncio.Event()
        self.answer_release = asyncio.Event()

    async def resolve(self, profile):
        return profile.model_copy(update={"model": "contract-model"})

    async def context_window(self, profile):
        return 32768

    async def close(self):
        pass

    async def stream(self, profile, messages, tools, on_delta):
        self.calls.append(messages)
        if len(self.calls) == 1:
            return Turn({"role": "assistant", "content": None, "tool_calls": [
                {"id": name, "type": "function", "function": {"name": name, "arguments": "{}"}}
                for name in (HeldRead.name, ForbiddenMutation.name)
            ]}, "contract-model", None, "tool_calls")
        self.answer_entered.set()
        if self.hold_answer:
            await self.answer_release.wait()
        return Turn({"role": "assistant", "content": "The latest direction is included."},
                    "contract-model", None, "stop")


async def start_role(repository, tmp_path, monkeypatch, role, *, hold_answer=False, phase="read", uncertain=False):
    client = ContractClient(hold_answer=hold_answer or role == "ticket")
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    await app.config.save(Settings(autonomy="full", tool_rules={HeldRead.name: "ask"} if phase == "approval" else {}))
    read, mutation = HeldRead(), ForbiddenMutation()
    read.uncertain_on_interrupt = uncertain
    approval_entered, approval_cancelled = asyncio.Event(), asyncio.Event()

    async def approval(*args):
        approval_entered.set()
        try:
            await read.release.wait()
        except asyncio.CancelledError:
            approval_cancelled.set()
            raise
        return "approve"

    def manager(session, **options):
        return ToolManager([read, mutation], [read.name, mutation.name],
            store=app.store, events=app.events, settings=lambda: app.config.settings,
            session_id=session, wait=approval if phase == "approval" else None, **options)

    if role == "pm":
        monkeypatch.setattr(app.project_manager, "_tool_manager", lambda: manager("project-manager"))
        await app.project_manager.resume()
        task = asyncio.create_task(app.project_manager.message("Original direction", "original"))
        identity, session = "project-manager", "project-manager"
    elif role == "discussion":
        from agilecode.core import discussion
        monkeypatch.setattr(discussion, "discussion_tools", lambda app, identity, session: manager(session))
        saved = await app.discussion.create()
        identity, session = saved["id"], saved["session_id"]
        task = asyncio.create_task(app.discussion.ask(identity, "Original direction", "original"))
    else:
        from agilecode.core import runner
        monkeypatch.setattr(runner, "ticket_tools", lambda processor, step, ctx, runtime, **kwargs:
            manager(ctx.attempt["session_id"], ticket_id=ctx.ticket["id"], run_id=ctx.run["id"],
                    attempt_id=ctx.attempt["id"], check=runtime.control.check))
        ticket = await app.create_ticket("Interruption contract", "Do only the current direction.")
        await app.move_ticket(ticket["id"], "Ready")
        run = await app.start_ticket(ticket["id"])
        identity, session = ticket["id"], None
        task = app.tasks[run["id"]]
    await asyncio.wait_for((approval_entered if phase == "approval" else read.entered).wait(), 5)
    if session is None:
        attempt = await app.store.one("SELECT session_id FROM attempts WHERE run_id=? ORDER BY created DESC LIMIT 1", (run["id"],))
        session = attempt["session_id"]
    return SimpleNamespace(app=app, client=client, read=read, mutation=mutation, role=role,
                           identity=identity, session=session, task=task,
                           approval_cancelled=approval_cancelled)


async def stop_role(harness, control):
    app, identity = harness.app, harness.identity
    if harness.role == "pm":
        return await getattr(app.project_manager, control)()
    if harness.role == "discussion":
        return await app.discussion.shutdown() if control == "shutdown" else await app.discussion.stop(identity)
    return await getattr(app, control)(identity)


async def send_correction(harness, index):
    text, input_id = f"Correction {index}: never mutate", f"correction-{index}"
    if harness.role == "pm":
        return await harness.app.project_manager.message(text, input_id)
    if harness.role == "discussion":
        return await harness.app.discussion.ask(harness.identity, text, input_id)
    return await harness.app.submit_feedback(harness.identity, text, interrupt=True, input_id=input_id)


@pytest.mark.parametrize("role,control", [
    ("pm", "stop_response"), ("pm", "pause"), ("pm", "turn_off"), ("pm", "shutdown"),
    ("discussion", "stop"), ("discussion", "shutdown"),
    ("ticket", "stop_generation"), ("ticket", "pause_run"), ("ticket", "cancel_run"),
])
async def test_public_controls_interrupt_tool_phase_without_later_mutation(
        repository, tmp_path, monkeypatch, role, control):
    harness = await start_role(repository, tmp_path, monkeypatch, role)
    try:
        await asyncio.wait_for(stop_role(harness, control), 5)
        await asyncio.wait_for(asyncio.gather(harness.task, return_exceptions=True), 5)
        assert harness.read.cancelled.is_set(), "Stop must reach the active tool, not just model generation"
        assert not harness.read.finished
        assert harness.mutation.calls == 0, "An unstarted sibling tool must never execute after interruption"
        assert len(harness.client.calls) == 1, "Stopping a response must not request another model response"
        tools = await harness.app.store.query("SELECT * FROM tools WHERE session_id=?", (harness.session,))
        assert {row["name"] for row in tools} == {HeldRead.name, ForbiddenMutation.name}
        mutation = next(row for row in tools if row["name"] == ForbiddenMutation.name)
        assert decode(mutation["result"])["error"]["code"] == "not_executed"
        if role == "ticket":
            run = await harness.app.selected_run(harness.identity)
            assert run["state"] == ("cancelled" if control == "cancel_run" else "paused")
    finally:
        harness.read.release.set()
        await harness.app.close()
        await asyncio.gather(harness.task, return_exceptions=True)


@pytest.mark.parametrize("role", ["pm", "discussion", "ticket"])
async def test_concurrent_corrections_cancel_read_and_preserve_every_input(
        repository, tmp_path, monkeypatch, role):
    harness = await start_role(repository, tmp_path, monkeypatch, role, hold_answer=True)
    corrections = []
    try:
        corrections = [asyncio.create_task(send_correction(harness, index)) for index in range(4)]
        async with asyncio.timeout(5):
            while True:
                rows = await harness.app.store.query(
                    "SELECT * FROM operations WHERE kind='conversation_input' AND owner_id=?", (harness.session,))
                if sum(decode(row["data"])["input_id"].startswith("correction-") for row in rows) == 4:
                    break
                await asyncio.sleep(0)
        assert harness.read.cancelled.is_set()
        assert not harness.read.finished and harness.mutation.calls == 0
        # Wait for a request made after all four durable receipts, without
        # assuming that overlapping arrivals need exactly one provider request.
        async with asyncio.timeout(5):
            while not any(all(any(f"Correction {index}:" in (message.get("content") or "")
                                 for message in request if message["role"] == "user")
                             for index in range(4)) for request in harness.client.calls):
                await asyncio.sleep(0)
        harness.client.answer_release.set()
        if role == "ticket":
            await asyncio.wait_for(asyncio.gather(*corrections), 5)
            await harness.app.stop_generation(harness.identity)
        else:
            await asyncio.wait_for(asyncio.gather(*corrections), 5)
        visible = await harness.app.store.messages(harness.session, include_partial=True)
        for index in range(4):
            assert sum(f"Correction {index}:" in (row["body"].get("content") or "")
                       for row in visible if row["body"]["role"] == "user") == 1
        assert harness.mutation.calls == 0
    finally:
        harness.read.release.set()
        await harness.app.close()
        await asyncio.gather(harness.task, *corrections, return_exceptions=True)


async def test_discussion_archive_is_a_lifecycle_guard_not_an_alternate_stop(
        repository, tmp_path, monkeypatch):
    harness = await start_role(repository, tmp_path, monkeypatch, "discussion")
    try:
        with pytest.raises(AgileCodeError, match="finish|busy"):
            await harness.app.discussion.archive(harness.identity)
        assert not (await harness.app.discussion.get(harness.identity))["archived"]
        assert not harness.task.done()
        await harness.app.discussion.stop(harness.identity)
        await asyncio.wait_for(harness.task, 5)
        assert (await harness.app.discussion.archive(harness.identity))["archived"] == 1
        with pytest.raises(AgileCodeError, match="Restore"):
            await harness.app.discussion.ask(harness.identity, "Do not reopen automatically")
        assert harness.mutation.calls == 0
    finally:
        harness.read.release.set()
        await harness.app.close()
        await asyncio.gather(harness.task, return_exceptions=True)


@pytest.mark.parametrize("role", ["pm", "discussion", "ticket"])
async def test_correction_during_approval_never_enters_old_tool(
        repository, tmp_path, monkeypatch, role):
    harness = await start_role(repository, tmp_path, monkeypatch, role, hold_answer=True, phase="approval")
    correction = None
    try:
        correction = asyncio.create_task(send_correction(harness, 0))
        await asyncio.wait_for(harness.client.answer_entered.wait(), 5)
        assert harness.approval_cancelled.is_set()
        assert not harness.read.entered.is_set()
        assert harness.mutation.calls == 0
        harness.read.release.set()  # A late approval cannot revive the retired dispatch.
        await asyncio.sleep(0)
        assert not harness.read.entered.is_set()
        assert any("Correction 0:" in (message.get("content") or "")
                   for message in harness.client.calls[-1] if message["role"] == "user")
        if role == "ticket":
            await asyncio.wait_for(correction, 5)
            await harness.app.stop_generation(harness.identity)
        else:
            harness.client.answer_release.set()
            await asyncio.wait_for(correction, 5)
    finally:
        harness.read.release.set()
        await harness.app.close()
        await asyncio.gather(harness.task, *([correction] if correction else []), return_exceptions=True)


@pytest.mark.parametrize("role", ["pm", "discussion", "ticket"])
async def test_stop_reaches_provider_setup_before_first_model_request(
        repository, tmp_path, monkeypatch, role):
    client = ContractClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = client.resolve

    async def resolve(profile):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return await original(profile)

    monkeypatch.setattr(client, "resolve", resolve)
    try:
        if role == "ticket":
            ticket = await app.create_ticket("Stop provider setup", "Do not begin after Stop.")
            await app.move_ticket(ticket["id"], "Ready")
            run = await app.start_ticket(ticket["id"])
            task = app.tasks[run["id"]]
        elif role == "pm":
            await app.project_manager.resume()
            task = asyncio.create_task(app.project_manager.message("Do not begin after Stop."))
        else:
            discussion = await app.discussion.create()
            task = asyncio.create_task(app.discussion.ask(discussion["id"], "Do not begin after Stop."))
        await asyncio.wait_for(entered.wait(), 5)
        if role == "ticket":
            await app.stop_generation(ticket["id"])
        elif role == "pm":
            await app.project_manager.stop_response()
        else:
            await app.discussion.stop(discussion["id"])
        async with asyncio.timeout(5):
            await cancelled.wait()
            await asyncio.gather(task, return_exceptions=True)
        assert not client.calls
        if role == "ticket":
            assert (await app.store.get("runs", run["id"]))["state"] == "paused"
    finally:
        release.set()
        await app.close()


@pytest.mark.parametrize("control", ["correction", "stop_response", "pause", "turn_off", "shutdown"])
async def test_pm_interrupt_during_provisioning_never_launches_ticket(
        repository, tmp_path, monkeypatch, control):
    class StartClient(ContractClient):
        async def stream(self, profile, messages, tools, on_delta):
            self.calls.append(messages)
            if len(self.calls) == 1:
                return Turn({"role": "assistant", "content": None, "tool_calls": [{
                    "id": "start", "type": "function", "function": {
                        "name": "start_ticket", "arguments": json.dumps({"ticket": self.ticket})}
                }]}, "contract-model", None, "tool_calls")
            return Turn({"role": "assistant", "content": "The correction is included."},
                        "contract-model", None, "stop")

    client = StartClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    entered, release, launched = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = app.git.add_worktree

    async def provision(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    async def run(runtime):
        launched.set()
        await asyncio.Future()

    monkeypatch.setattr(app.git, "add_worktree", provision)
    monkeypatch.setattr(app.execution_ops, "_run", run)
    first = interrupt = None
    try:
        ticket = await app.create_ticket("Unwanted launch", "Cancel before execution can begin.")
        await app.move_ticket(ticket["id"], "Ready")
        client.ticket = ticket["id"]
        await app.project_manager.resume()
        first = asyncio.create_task(app.project_manager.message("Start this ticket", "start"))
        await asyncio.wait_for(entered.wait(), 5)
        active = app.project_manager.engine.active_turns["project-manager"]
        if control == "correction":
            interrupt = asyncio.create_task(app.project_manager.message("Do not start that ticket", "cancel-start"))
            async with asyncio.timeout(5):
                while (await records.state(app.store, "project-manager"))["input_version"] < 2:
                    await asyncio.sleep(0)
        else:
            interrupt = asyncio.create_task(getattr(app.project_manager, control)())
        # Ensure interruption has actually reached the owned old PM turn before
        # finishing the already-started physical worktree operation.
        async with asyncio.timeout(5):
            while not active.token.cancelled:
                await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, interrupt, return_exceptions=True), 5)
        assert not launched.is_set(), "Provisioning settlement must not launch fresh ticket execution"
        run_record = await app.selected_run(ticket["id"])
        assert run_record is None or run_record["state"] == "paused"
        if control == "correction":
            assert len(client.calls) == 2
            assert any(message.get("content") == "Do not start that ticket"
                       for message in client.calls[-1] if message["role"] == "user")
            row = await app.store.one("SELECT * FROM operations WHERE kind='conversation_input' AND data LIKE '%cancel-start%'")
            assert decode(row["result"])["status"] == "completed"
            assert app.project_manager.state["status"] == "running"
        else:
            assert len(client.calls) == 1
    finally:
        release.set()
        await app.close()
        await asyncio.gather(*[task for task in (first, interrupt) if task], return_exceptions=True)


@pytest.mark.parametrize("role", ["pm", "discussion"])
async def test_close_owns_accepted_message_before_turn_admission(repository, tmp_path, monkeypatch, role):
    app = Application(repository, tmp_path / "global", client=ContractClient())
    await app.open(initialize=True)
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    service = app.project_manager if role == "pm" else app.discussion
    original = service.engine.accept_input

    async def accepted(*args, **kwargs):
        receipt = await original(*args, **kwargs)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return receipt

    monkeypatch.setattr(service.engine, "accept_input", accepted)
    sending = closing = None
    try:
        if role == "pm":
            await service.resume()
            sending = asyncio.create_task(service.message("Keep this accepted input", "pre-admission"))
        else:
            discussion = await service.create()
            sending = asyncio.create_task(service.ask(discussion["id"], "Keep this accepted input", "pre-admission"))
        await asyncio.wait_for(entered.wait(), 5)
        closing = asyncio.create_task(app.close())
        await asyncio.wait_for(asyncio.shield(closing), 5)
        assert sending.done(), "Host close must retain ownership of accepted sends before session admission"
        assert cancelled.is_set(), "Shutdown should cancel the accepted wrapper through shared ownership"
        assert app.closed and app.store.closed
    finally:
        release.set()
        await asyncio.gather(*[task for task in (sending, closing) if task], return_exceptions=True)
        await app.close()


async def test_ticket_correction_terminates_real_command_without_replay(repository, tmp_path):
    command = "echo once >> contract-command.txt; echo $$ > contract-command.pid; sleep 60"

    class ShellClient(ScriptClient):
        def __init__(self):
            super().__init__()
            self.sent = False
            self.feedback_seen = False

        async def stream(self, profile, messages, tools, on_delta):
            self.feedback_seen |= any("Stop the old command" in (message.get("content") or "")
                                      for message in messages if message["role"] == "user")
            if not self.sent and "(implement)" in messages[0]["content"]:
                self.sent = True
                return Turn({"role": "assistant", "content": None, "tool_calls": [{
                    "id": "contract-shell", "type": "function", "function": {
                        "name": "run_command", "arguments": json.dumps({"command": command})}
                }]}, "fixture-model", None, "tool_calls")
            # The fixture deliberately acknowledges the actual interrupted
            # command outcome, then continues the ordinary deterministic work.
            messages = [message for message in messages if message.get("tool_call_id") != "contract-shell"
                        and not any(call["id"] == "contract-shell" for call in message.get("tool_calls", []))]
            return await super().stream(profile, messages, tools, on_delta)

    client = ShellClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    await app.config.save(Settings(autonomy="full"))
    try:
        ticket = await app.create_ticket("Correct the running command", "Make answer return two and update its test.")
        await app.move_ticket(ticket["id"], "Ready")
        run = await app.start_ticket(ticket["id"])
        worktree = Path(run["workspace"])
        async with asyncio.timeout(10):
            while not (worktree / "contract-command.pid").exists():
                await asyncio.sleep(.01)
        pid = int((worktree / "contract-command.pid").read_text())
        await asyncio.wait_for(app.submit_feedback(ticket["id"], "Stop the old command and do not replay it.",
                                                 interrupt=True, input_id="real-command-correction"), 5)
        completed = await asyncio.wait_for(app.wait_for_run(run["id"]), 20)
        assert completed["state"] == "completed", completed
        assert client.feedback_seen
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert (worktree / "contract-command.txt").read_text().splitlines() == ["once"]
        tool = await app.store.one("SELECT * FROM tools WHERE call_id='contract-shell'")
        payload = decode(tool["result"])
        assert payload["error"]["code"] == "interrupted" and payload["outcome_known"]
        assert not await app.store.query("SELECT id FROM jobs WHERE run_id=? AND status IN ('running','starting')", (run["id"],))
    finally:
        await app.close()


@pytest.mark.parametrize("control,status", [("pause", "paused"), ("turn_off", "off")])
async def test_pm_control_signals_interrupt_before_slow_state_persistence(
        repository, tmp_path, monkeypatch, control, status):
    harness = await start_role(repository, tmp_path, monkeypatch, "pm")
    entered, release = threading.Event(), threading.Event()
    original = harness.app.project_manager._write_json
    active = harness.app.project_manager.engine.active_turns["project-manager"]

    def write(path, value):
        if path == harness.app.project_manager.state_path and value.get("status") == status:
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release PM state writer")
        return original(path, value)

    monkeypatch.setattr(harness.app.project_manager, "_write_json", write)
    control_task = late_message = None
    try:
        control_task = asyncio.create_task(stop_role(harness, control))
        assert await asyncio.to_thread(entered.wait, 5)
        assert active.token.cancelled, "Pause/Off must not leave old dispatch alive while saving state"
        late_message = asyncio.create_task(harness.app.project_manager.message(
            "Keep this direction for explicit Resume", "during-pause-write"))
        async with asyncio.timeout(5):
            while (await records.state(harness.app.store, "project-manager"))["input_version"] < 2:
                await asyncio.sleep(0)
        assert len(harness.client.calls) == 1
        harness.read.release.set()
        release.set()
        await asyncio.wait_for(control_task, 5)
        await asyncio.wait_for(asyncio.gather(harness.task, late_message, return_exceptions=True), 5)
        assert harness.mutation.calls == 0
        assert len(harness.client.calls) == 1, "New input during Pause/Off must not reopen dispatch"
        assert harness.app.project_manager.state["status"] == status
        receipt = await harness.app.store.one("SELECT * FROM operations WHERE kind='conversation_input' AND data LIKE '%during-pause-write%'")
        assert receipt and receipt["result"] is None
        await harness.app.project_manager.resume()
        await asyncio.wait_for(harness.client.answer_entered.wait(), 5)
        assert any(message.get("content") == "Keep this direction for explicit Resume"
                   for message in harness.client.calls[-1] if message["role"] == "user")
    finally:
        release.set()
        harness.read.release.set()
        await harness.app.close()
        await asyncio.gather(harness.task, *[task for task in (control_task, late_message) if task], return_exceptions=True)


@pytest.mark.parametrize("role", ["pm", "discussion", "ticket"])
async def test_uncertain_tool_blocks_continuation_but_retires_every_unstarted_sibling(
        repository, tmp_path, monkeypatch, role):
    harness = await start_role(repository, tmp_path, monkeypatch, role, uncertain=True)
    correction = None
    try:
        correction = asyncio.create_task(send_correction(harness, 0))
        await asyncio.wait_for(asyncio.gather(harness.task, correction, return_exceptions=True), 5)
        assert harness.read.cancelled.is_set()
        assert harness.mutation.calls == 0 and len(harness.client.calls) == 1
        tools = await harness.app.store.query("SELECT * FROM tools WHERE session_id=?", (harness.session,))
        assert {tool["name"] for tool in tools} == {HeldRead.name, ForbiddenMutation.name}
        held = next(tool for tool in tools if tool["name"] == HeldRead.name)
        assert held["status"] == "unknown" and not decode(held["result"]).get("outcome_known")
        sibling = next(tool for tool in tools if tool["name"] == ForbiddenMutation.name)
        assert decode(sibling["result"])["error"]["code"] == "not_executed"
        history = await harness.app.store.messages(harness.session)
        results = [row["body"]["tool_call_id"] for row in history if row["body"]["role"] == "tool"]
        assert results.count(HeldRead.name) == results.count(ForbiddenMutation.name) == 1
    finally:
        harness.read.release.set()
        await harness.app.close()
        await asyncio.gather(harness.task, *([correction] if correction else []), return_exceptions=True)


@pytest.mark.parametrize("role,control", [
    ("pm", "stop_response"), ("discussion", "stop"), ("ticket", "stop_generation"),
])
async def test_repeated_stop_and_close_drain_concurrent_corrections(
        repository, tmp_path, monkeypatch, role, control):
    harness = await start_role(repository, tmp_path, monkeypatch, role, hold_answer=True)
    commands = []
    try:
        commands = [asyncio.create_task(send_correction(harness, index)) for index in range(4)]
        async with asyncio.timeout(5):
            while True:
                rows = await harness.app.store.query(
                    "SELECT data FROM operations WHERE kind='conversation_input' AND owner_id=?", (harness.session,))
                if sum(decode(row["data"])["input_id"].startswith("correction-") for row in rows) == 4:
                    break
                await asyncio.sleep(0)
        commands.extend(asyncio.create_task(stop_role(harness, control)) for _ in range(3))
        closing = asyncio.create_task(harness.app.close())
        commands.append(closing)
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.wait_for(asyncio.gather(harness.task, *commands, return_exceptions=True), 5)
        assert harness.app.closed and harness.app.store.closed
        controller = harness.app.project_manager.engine.controller
        assert not controller.turns and not controller.admitted and not controller.requests
        assert harness.mutation.calls == 0
    finally:
        harness.read.release.set()
        await harness.app.close()
        await asyncio.gather(harness.task, *commands, return_exceptions=True)


async def test_pause_preserves_unowned_job_uncertainty_instead_of_claiming_cancellation(
        repository, tmp_path, monkeypatch):
    harness = await start_role(repository, tmp_path, monkeypatch, "ticket")
    try:
        run = await harness.app.selected_run(harness.identity)
        await harness.app.store.insert("jobs", {
            "id": "unowned-contract-job", "ticket_id": harness.identity, "run_id": run["id"],
            "command": "A prior process whose exit is unverified", "cwd": run["workspace"],
            "status": "running", "created": now(),
        })
        failure = None
        try:
            await harness.app.pause_run(harness.identity)
        except AgileCodeError as exc:
            failure = exc
        job = await harness.app.store.get("jobs", "unowned-contract-job")
        assert job["status"] == "unknown", "Losing a process handle is not proof that cancellation succeeded"
        assert job["finished"] is None
        blocker = await harness.app.store.get("operations", "job_recovery:unowned-contract-job")
        assert blocker["status"] == "blocked"
        assert failure is not None, "Pause must report unresolved physical termination"
        facts = await harness.app.execution_status(harness.identity)
        assert not facts["can_resume"] and facts["blocking_reason"]
        with pytest.raises(AgileCodeError):
            await harness.app.move_ticket(harness.identity, "Ready")
        assert (await harness.app.find_ticket(harness.identity))["state"] == "In Progress"
    finally:
        harness.read.release.set()
        await harness.app.close()
        await asyncio.gather(harness.task, return_exceptions=True)


@pytest.mark.parametrize("control", ["stop", "correction"])
async def test_response_owned_background_job_stops_and_later_launch_is_retired(
        repository, tmp_path, monkeypatch, control):
    first_command = "echo $$ > owned-background.pid; sleep 60"
    later_command = "touch forbidden-background-launch"
    read = HeldRead()

    class BackgroundClient(ContractClient):
        async def stream(self, profile, messages, tools, on_delta):
            self.calls.append(messages)
            if len(self.calls) == 1:
                specs = [("background-first", "run_command", {"command": first_command, "background": True}),
                         ("held-read", read.name, {}),
                         ("background-later", "run_command", {"command": later_command, "background": True})]
                return Turn({"role": "assistant", "content": None, "tool_calls": [
                    {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                    for identity, name, args in specs]}, "contract-model", None, "tool_calls")
            self.answer_entered.set()
            await asyncio.Future()

    client = BackgroundClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    await app.config.save(Settings(autonomy="full"))
    from agilecode.core import runner

    def manager(processor, step, ctx, runtime, **kwargs):
        command = RunCommandTool(runtime.shell, lambda: app.config.settings, ctx.attempt["id"], runtime.write_lock)
        return ToolManager([command, read], [command.name, read.name], store=app.store, events=app.events,
            settings=lambda: app.config.settings, session_id=ctx.attempt["session_id"],
            ticket_id=ctx.ticket["id"], run_id=ctx.run["id"], attempt_id=ctx.attempt["id"],
            shell=runtime.shell, check=runtime.control.check)

    monkeypatch.setattr(runner, "ticket_tools", manager)
    try:
        ticket = await app.create_ticket("Background lifetime", "Preserve independently owned work.")
        await app.move_ticket(ticket["id"], "Ready")
        run = await app.start_ticket(ticket["id"])
        await asyncio.wait_for(read.entered.wait(), 5)
        job = await app.store.one("SELECT * FROM jobs WHERE command=?", (first_command,))
        proc, _ = app.runtimes[run["id"]].shell.background[job["id"]]
        if control == "stop":
            await app.stop_generation(ticket["id"])
            await asyncio.wait_for(app.tasks[run["id"]], 5)
        else:
            await app.submit_feedback(ticket["id"], "Do not launch the second background job", interrupt=True)
            await asyncio.wait_for(client.answer_entered.wait(), 5)
        assert proc.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(proc.pid, 0)
        assert (await app.store.get("jobs", job["id"]))["status"] == "cancelled"
        assert not (Path(run["workspace"]) / "forbidden-background-launch").exists()
        assert not await app.store.one("SELECT id FROM jobs WHERE command=?", (later_command,))
        retired = await app.store.one("SELECT result FROM tools WHERE call_id='background-later'")
        assert decode(retired["result"])["error"]["code"] == "not_executed"
        await app.pause_run(ticket["id"])
        assert proc.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(proc.pid, 0)
        assert not app.runtimes[run["id"]].shell.has_live_jobs()
    finally:
        read.release.set()
        await app.close()


async def test_pm_response_controls_leave_previously_admitted_ticket_running(repository, tmp_path, monkeypatch):
    class StartThenWaitClient(ContractClient):
        async def stream(self, profile, messages, tools, on_delta):
            self.calls.append(messages)
            if len(self.calls) == 1:
                return Turn({"role": "assistant", "content": None, "tool_calls": [{
                    "id": "start-independent", "type": "function", "function": {
                        "name": "start_ticket", "arguments": json.dumps({"ticket": self.ticket})}
                }]}, "contract-model", None, "tool_calls")
            self.answer_entered.set()
            await asyncio.Future()

    client = StartThenWaitClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    launched = asyncio.Event()

    async def runtime(runtime):
        launched.set()
        await asyncio.Future()

    monkeypatch.setattr(app.execution_ops, "_run", runtime)
    sending = None
    try:
        ticket = await app.create_ticket("Independent admitted ticket", "Its execution has its own owner.")
        await app.move_ticket(ticket["id"], "Ready")
        client.ticket = ticket["id"]
        await app.project_manager.resume()
        sending = asyncio.create_task(app.project_manager.message("Start this ticket", "start-independent"))
        await asyncio.wait_for(launched.wait(), 5)
        await asyncio.wait_for(client.answer_entered.wait(), 5)
        run = await app.selected_run(ticket["id"])
        ticket_task = app.tasks[run["id"]]
        await app.project_manager.stop_response()
        await asyncio.gather(sending, return_exceptions=True)
        assert not ticket_task.done()
        await app.project_manager.pause()
        assert not ticket_task.done()
        await app.project_manager.turn_off()
        assert not ticket_task.done()
        assert (await app.store.get("runs", run["id"]))["state"] == "running"
        await app.pause_run(ticket["id"])
        assert ticket_task.done()
    finally:
        await app.close()
        if sending:
            await asyncio.gather(sending, return_exceptions=True)


async def test_pause_does_not_reconcile_another_runs_inflight_job_registration(
        repository, tmp_path, monkeypatch):
    harness = await start_role(repository, tmp_path, monkeypatch, "ticket")
    app = harness.app
    entered, release = asyncio.Event(), asyncio.Event()
    starting = None
    try:
        ticket = await app.create_ticket("Other active execution", "Its job admission is independent.")
        await app.move_ticket(ticket["id"], "Ready")
        run = await app.start_ticket(ticket["id"])
        async with asyncio.timeout(5):
            while not await app.store.one(
                    "SELECT q.id FROM requests q JOIN attempts a ON a.session_id=q.session_id "
                    "WHERE a.run_id=? AND q.status='streaming'", (run["id"],)):
                await asyncio.sleep(0)
        runtime = app.runtimes[run["id"]]
        original = runtime.shell.shell.exported_environment

        async def environment():
            entered.set()
            await release.wait()
            return await original()

        monkeypatch.setattr(runtime.shell.shell, "exported_environment", environment)
        attempt = await app.store.one("SELECT id FROM attempts WHERE run_id=?", (run["id"],))
        starting = asyncio.create_task(runtime.shell.run("sleep 60", attempt["id"], background=True))
        await asyncio.wait_for(entered.wait(), 5)
        job = await app.store.one("SELECT * FROM jobs WHERE run_id=? AND command='sleep 60'", (run["id"],))
        assert job["status"] == "starting" and not runtime.shell.owns_job(job["id"])
        await app.pause_run(harness.identity)
        assert (await app.store.get("jobs", job["id"]))["status"] == "starting"
        assert not await app.store.one("SELECT id FROM operations WHERE id=?", ("job_recovery:" + job["id"],))
        assert ticket["id"] not in app.coordinator.blocked_tickets
    finally:
        release.set()
        harness.read.release.set()
        if starting:
            await asyncio.gather(starting, return_exceptions=True)
        await app.close()
        await asyncio.gather(harness.task, return_exceptions=True)


async def test_late_pm_correction_wins_completed_answers_stale_pause_projection(
        repository, tmp_path, monkeypatch):
    class NeedDirectionClient(ContractClient):
        async def stream(self, profile, messages, tools, on_delta):
            self.calls.append(messages)
            return Turn({"role": "assistant", "content":
                "NEEDS_USER: Please choose the direction." if len(self.calls) == 1 else "The new direction is answered."},
                "contract-model", None, "stop")

    client = NeedDirectionClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    await app.project_manager.save_settings({**app.project_manager.settings.model_dump(mode="json"), "uncertainty": "ask"})
    entered, release = threading.Event(), threading.Event()
    original = app.project_manager._write_json

    def write(path, value):
        if path == app.project_manager.state_path and value.get("current_activity") == "Waiting for user direction":
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release completed-answer projection")
        return original(path, value)

    monkeypatch.setattr(app.project_manager, "_write_json", write)
    first = correction = None
    try:
        await app.project_manager.resume()
        first = asyncio.create_task(app.project_manager.message("Initial question", "initial-question"))
        assert await asyncio.to_thread(entered.wait, 5)
        old = await app.store.one("SELECT result FROM operations WHERE kind='conversation_input' AND data LIKE '%initial-question%'")
        assert decode(old["result"])["status"] == "completed"
        correction = asyncio.create_task(app.project_manager.message("Use the new direction now", "late-final-correction"))
        async with asyncio.timeout(5):
            while (await records.state(app.store, "project-manager"))["input_version"] < 2:
                await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, correction, return_exceptions=True), 5)
        assert len(client.calls) == 2, "The new accepted direction must not be paused by the old answer"
        assert any(message.get("content") == "Use the new direction now"
                   for message in client.calls[-1] if message["role"] == "user")
        latest = await app.store.one("SELECT result FROM operations WHERE kind='conversation_input' AND data LIKE '%late-final-correction%'")
        assert decode(latest["result"])["status"] == "completed"
        assert app.project_manager.state["status"] == "running"
    finally:
        release.set()
        await app.close()
        await asyncio.gather(*[task for task in (first, correction) if task], return_exceptions=True)


@pytest.mark.asyncio
async def test_independent_ticket_events_do_not_inherit_launching_pm_session(
        repository, tmp_path, monkeypatch):
    from contextvars import ContextVar
    from agilecode.application.cancellation import CancellationToken, bind_cancellation, current_cancellation
    from agilecode.core.types import Event

    client = ContractClient()
    app = Application(repository, tmp_path / "global", client=client)
    await app.open(initialize=True)
    pm = app.project_manager
    # Exercise the real event subscription and queue without scheduling a model.
    monkeypatch.setattr(pm, "_schedule_updates", lambda: None)
    trace = ContextVar("contract_trace", default=None)
    seen = []
    try:
        ticket = await app.create_ticket("Independent ownership", "Report lifecycle changes.")
        await pm.save_settings({"mode": "fully_automated"})
        await pm.resume()
        pm._pending_updates.clear()
        pm_identity = records.session_identity(app.store, "project-manager")
        parent = records.active_session.set(pm_identity)
        trace_binding = trace.set("preserved-context")
        token = CancellationToken()

        async def child():
            seen.append(("child", records.active_session.get(), current_cancellation(), trace.get()))
            nested = records.active_session.set(records.session_identity(app.store, "ticket-session"))
            try:
                await asyncio.sleep(0)
            finally:
                records.active_session.reset(nested)
            app.events.emit(Event("ExecutionCompleted", ticket["id"]))

        async def after():
            seen.append(("after", records.active_session.get(), current_cancellation(), trace.get()))
            app.events.emit(Event("TicketChanged", ticket["id"]))

        try:
            with bind_cancellation(token):
                app.events.emit(Event("RunChanged", ticket["id"]))
                assert not pm._pending_updates, "Synchronous PM tool events must still avoid self-prompting"
                task = await app.coordinator.spawn(ticket["id"], "independent-owner", "execution", child(), after=after)
                token.cancel()
                await asyncio.wait_for(task, 5)
                assert records.active_session.get() == pm_identity
                assert current_cancellation() is token
        finally:
            records.active_session.reset(parent)
            trace.reset(trace_binding)

        assert pm._pending_updates == {
            f"ExecutionCompleted: ticket {ticket['id']}",
            f"TicketChanged: ticket {ticket['id']}",
        }, f"Independent lifecycle events were suppressed; inherited contexts: {seen!r}"
        assert seen == [(phase, None, None, "preserved-context") for phase in ("child", "after")]
        assert not client.calls
    finally:
        await app.close()
