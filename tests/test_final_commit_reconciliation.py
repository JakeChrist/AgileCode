"""A later verified commit receipt never rewrites an interrupted exchange."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

from agilecode.adapters.git import GitRepository
from agilecode.adapters.store import decode, encode
from agilecode.core.application import Application
from agilecode.core.tools import FinalCommitTool, ToolManager
from agilecode.core.types import Conflict, Events, Settings
from agilecode.runtime import conversation_records as records
from conftest import ScriptClient, git


class ReconciliationClient(ScriptClient):
    """Continue using the explicit later evidence, retaining the real warning."""
    def __init__(self):
        super().__init__()
        self.reconciled_requests = []

    def action(self, messages):
        notices = [message for message in messages
                   if (message.get("content") or "").startswith("VERIFIED TOOL OUTCOME RECONCILIATION")]
        if notices and "(commit)" in messages[0]["content"]:
            self.reconciled_requests.append(copy.deepcopy(messages))
            result = json.loads(notices[-1]["content"].split("\n", 1)[1])
            assert result["status"] == "ok" and result["outcome_known"]
            original = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
            assert sum(item.get("_execution_id") == result["_execution_id"] and
                       item.get("error", {}).get("code") == "interrupted" for item in original) == 1
            return "finish_step", {"status": "PASS", "summary": "Verified recovered final commit",
                                   "output": "The preserved interrupted result was resolved by journal and Git evidence."}
        return super().action(messages)


async def interrupted_commit(repository, home, *, boundary="after"):
    app = Application(repository, home, client=ReconciliationClient())
    await app.open(initialize=True)
    await app.config.save(Settings(autonomy="full"))
    ticket = await app.create_ticket("Answer two", "Return two and update its regression test.")
    await app.move_ticket(ticket["id"], "Ready")
    reached = asyncio.Event()
    original = app.store.transaction

    async def interrupt_transaction(callback):
        is_commit = callback.__qualname__ == "ProcessRunner._commit.<locals>.persist"
        if is_commit and boundary == "before":
            reached.set()
            await asyncio.Event().wait()
        result = await original(callback)
        if is_commit and boundary == "after":
            reached.set()
            await asyncio.Event().wait()
        return result

    app.store.transaction = interrupt_transaction
    try:
        run = await app.start_ticket(ticket["id"])
        await asyncio.wait_for(reached.wait(), 30)
        await asyncio.wait_for(app.pause_run(ticket["id"]), 15)
    except BaseException:
        app.store.transaction = original
        await app.close()
        raise
    app.store.transaction = original
    tool = await app.store.one("SELECT tools.* FROM tools JOIN attempts ON tools.session_id=attempts.session_id "
                               "WHERE attempts.run_id=? AND tools.name='final_commit'", (run["id"],))
    assert tool["status"] == "unknown"
    assert decode(tool["result"])["error"]["code"] == "interrupted"
    return app, ticket, run, tool


async def original_tool_messages(app, tool):
    return [row for row in await app.store.messages(tool["session_id"])
            if row["request_id"] == tool["request_id"] and row["body"].get("role") == "tool"
            and row["body"].get("tool_call_id") == tool["call_id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before", "after"])
async def test_reopen_twice_resume_keeps_original_warning_and_one_commit(repository, tmp_path, monkeypatch, boundary):
    effects, tool_calls = [], []
    original_commit, original_execute = GitRepository.final_commit, FinalCommitTool.execute

    async def count_commit(self, *args, **kwargs):
        effects.append(args)
        return await original_commit(self, *args, **kwargs)

    async def count_tool(self, args, execution_id):
        tool_calls.append(execution_id)
        return await original_execute(self, args, execution_id)

    monkeypatch.setattr(GitRepository, "final_commit", count_commit)
    monkeypatch.setattr(FinalCommitTool, "execute", count_tool)
    home = tmp_path / "global"
    app, ticket, run, tool = await interrupted_commit(repository, home, boundary=boundary)
    try:
        work = Path(run["workspace"])
        oid = git(work, "rev-parse", "HEAD")
        warning_messages = await original_tool_messages(app, tool)
        assert len(warning_messages) == 1
        warning = warning_messages[0]
        journal = await app.store.get("operations", "final-commit:" + run["id"])
        assert journal["status"] == ("prepared" if boundary == "before" else "completed")
        await app.close()
        first = Application(repository, home, client=ReconciliationClient())
        app = first
        await first.open()
        known = await records.reconciled_execution_payload(first.store, tool["id"])
        assert known["data"]["commit"] == oid
        assert known["outcome_known"]
        assert (await original_tool_messages(first, tool)) == [warning]
        assert (await first.store.get("tools", tool["id"]))["result"] == tool["result"]
        assert (await first.store.get("runs", run["id"]))["final_oid"] == oid
        await first.close()

        client = ReconciliationClient()
        reopened = Application(repository, home, client=client)
        app = reopened
        await reopened.open()
        assert await records.reconciled_execution_payload(reopened.store, tool["id"]) == known
        assert (await original_tool_messages(reopened, tool)) == [warning]
        manager = ToolManager([], [], store=reopened.store, events=Events(),
                              settings=lambda: Settings(), session_id=tool["session_id"])
        call = {"id": tool["call_id"], "function": {"name": "final_commit", "arguments": tool["args"]}}
        assert (await manager.execute(call, tool["request_id"])).payload == known
        with pytest.raises(Conflict):
            await records.complete_execution_exchange_once(reopened.store, tool["id"], known)
        await reopened.resume_run(ticket["id"])
        completed = await asyncio.wait_for(reopened.wait_for_run(run["id"]), 30)
        assert completed["state"] == "completed", completed
        assert completed["final_oid"] == oid == git(work, "rev-parse", "HEAD")
        assert git(repository, "rev-list", "--count", f"{run['base_oid']}..{oid}") == "1"
        assert len(effects) == len(tool_calls) == 1
        assert client.reconciled_requests
        assert (await original_tool_messages(reopened, tool)) == [warning]
        all_messages = await reopened.store.messages(tool["session_id"])
        notices = [row for row in all_messages
                   if (row["body"].get("content") or "").startswith("VERIFIED TOOL OUTCOME RECONCILIATION")]
        assert len(notices) == 1
        reconciliations = await reopened.store.query("SELECT * FROM operations WHERE kind='conversation_reconciliation'")
        assert len(reconciliations) == 1
        evidence = decode(reconciliations[0]["data"])["evidence"]
        assert evidence["oid"] == oid and evidence["parents"] == [run["base_oid"]]
    finally:
        await app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before", "after"])
@pytest.mark.parametrize("mismatch", ["branch", "head", "parents", "worktree", "journal_tree", "journal_base", "journal_oid", "journal_message"])
async def test_mismatched_evidence_stays_unknown_without_fabricated_recovery(repository, tmp_path, mismatch, boundary):
    home = tmp_path / "global"
    app, ticket, run, tool = await interrupted_commit(repository, home, boundary=boundary)
    work = Path(run["workspace"])
    try:
        warning = (await original_tool_messages(app, tool))[0]
        if mismatch == "branch":
            git(work, "switch", "-c", "unrelated-branch")
        elif mismatch == "head":
            git(work, "commit", "--allow-empty", "-m", "Unexpected subsequent commit")
        elif mismatch == "parents":
            git(work, "commit", "--allow-empty", "-m", decode(tool["args"])["message"])
            if boundary == "after":
                oid = git(work, "rev-parse", "HEAD")
                await app.store.update("runs", run["id"], final_oid=oid)
                await app.store.update("operations", "final-commit:" + run["id"], result=encode({"oid": oid}))
        elif mismatch == "worktree":
            (work / "app.py").write_text("def answer():\n    return 3\n")
        else:
            operation_id = "final-commit:" + run["id"]
            operation = await app.store.get("operations", operation_id)
            data = decode(operation["data"])
            if mismatch == "journal_tree":
                data["expected_tree"] = git(repository, "rev-parse", f"{run['base_oid']}^{{tree}}")
            elif mismatch == "journal_base":
                data["base_oid"] = git(work, "rev-parse", "HEAD")
            elif mismatch == "journal_message":
                data["message"] = "A different requested commit"
            else:
                await app.store.update("operations", operation_id, result=encode({"oid": run["base_oid"]}))
            await app.store.update("operations", operation_id, data=encode(data))
        prior_run = await app.store.get("runs", run["id"])
        prior_operation = await app.store.get("operations", "final-commit:" + run["id"])
        await app.close()
        reopened = Application(repository, home, client=ReconciliationClient())
        app = reopened
        await reopened.open()
        assert await records.reconciled_execution_payload(reopened.store, tool["id"]) is None
        assert await records.reconcile_final_commit_execution(reopened.store, tool["id"], git=reopened.git) is None
        assert (await reopened.store.get("tools", tool["id"]))["result"] == tool["result"]
        assert (await original_tool_messages(reopened, tool)) == [warning]
        assert not await reopened.store.query("SELECT * FROM operations WHERE kind='conversation_reconciliation'")
        current_run = await reopened.store.get("runs", run["id"])
        assert (current_run["final_oid"], current_run["final_tree"]) == (prior_run["final_oid"], prior_run["final_tree"])
        assert await reopened.store.get("operations", "final-commit:" + run["id"]) == prior_operation
        assert not any((row["body"].get("content") or "").startswith("VERIFIED TOOL OUTCOME RECONCILIATION")
                       for row in await reopened.store.messages(tool["session_id"]))
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_same_process_resume_verifies_commit_without_replay(repository, tmp_path, monkeypatch):
    app, ticket, run, tool = await interrupted_commit(repository, tmp_path / "global")
    try:
        warning = (await original_tool_messages(app, tool))[0]
        oid = git(Path(run["workspace"]), "rev-parse", "HEAD")
        async def forbidden_replay(*args, **kwargs):
            pytest.fail("A reconciled final_commit must never be redispatched")
        monkeypatch.setattr(FinalCommitTool, "execute", forbidden_replay)
        await app.resume_run(ticket["id"])
        completed = await asyncio.wait_for(app.wait_for_run(run["id"]), 30)
        assert completed["state"] == "completed", completed
        assert completed["final_oid"] == oid
        assert (await records.reconciled_execution_payload(app.store, tool["id"]))["data"]["commit"] == oid
        assert (await original_tool_messages(app, tool)) == [warning]
    finally:
        await app.close()
