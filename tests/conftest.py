from __future__ import annotations
import asyncio
import json
import re
import subprocess
from pathlib import Path
import pytest
from agilecode.adapters.model import Turn
from agilecode.core.types import ModelProfile


def git(root: Path, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.STDOUT).decode().strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path/"project"; root.mkdir()
    git(root, "init", "-b", "master")
    git(root, "config", "user.name", "AgileCode Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root/".gitignore").write_text(".agilecode/\n__pycache__/\n.pytest_cache/\n")
    (root/"app.py").write_text("def answer():\n    return 1\n")
    (root/"tests").mkdir()
    (root/"tests/test_app.py").write_text("import unittest\nfrom app import answer\nclass TestApp(unittest.TestCase):\n    def test_answer(self):\n        self.assertEqual(answer(), 1)\n")
    git(root, "add", "."); git(root, "commit", "-m", "Initial fixture")
    return root


class ScriptClient:
    """Deterministic model double. It never stands in for measured LLM quality."""
    def __init__(self, question=False, review_correction=False, latency=0.002):
        self.latency=latency
        self.calls = 0; self.question = question; self.review_correction = review_correction
        self.review_failed = False; self.active=0; self.max_active=0

    async def resolve(self, profile):
        return profile.model_copy(update={"model": "fixture-model"})
    async def close(self): pass
    async def models(self, profile): return [{"id":"fixture-model"}]

    def action(self, messages):
        system = messages[0]["content"]
        kind = re.search(r"CURRENT STEP: .*?\(([^)]+)\)", system)[1]
        results = []
        for i,m in enumerate(messages):
            if m["role"]=="tool":
                assistant = next(a for a in reversed(messages[:i]) if a.get("tool_calls"))
                call = next(c for c in assistant["tool_calls"] if c["id"]==m["tool_call_id"])
                result=json.loads(m["content"])
                results.append((call["function"]["name"],json.loads(call["function"]["arguments"]),result))
        def finish(output="Completed using the recorded inputs and evidence.", status="PASS"):
            return "finish_step",{"status":status,"summary":kind+" "+status,"output":output}
        def found(name): return [r for r in results if r[0]==name and r[2]["status"]=="ok"]
        # Unexpected protocol/tool errors fail tests loudly instead of hiding them.
        errors=[r for r in results if r[2]["status"]=="error"]
        if errors:
            raise RuntimeError("Fixture received a tool error: "+json.dumps(errors[-1]))
        if kind=="understand" and self.question and not found("ask_user"):
            return "ask_user",{"question":"Should the answer become 2?"}
        if kind=="requirements":
            return finish("REQ-1: answer() returns 2. Verify with python -m unittest discover -s tests -q and code inspection.")
        if kind=="implement":
            changes={"app.py":"def answer():\n    return 2\n", "tests/test_app.py":"import unittest\nfrom app import answer\nclass TestApp(unittest.TestCase):\n    def test_answer(self):\n        self.assertEqual(answer(), 2)\n"}
            for path,content in changes.items():
                reads=[r for r in found("read_file") if r[1]["path"]==path]
                writes=[r for r in found("write_file") if r[1]["path"]==path]
                if not reads: return "read_file",{"path":path}
                if not writes: return "write_file",{"path":path,"content":content,"expected_hash":reads[-1][2]["data"]["hash"]}
            return finish("Implemented the plan; app and regression test now return/expect 2.")
        if kind=="verify":
            if not found("run_command"): return "run_command",{"command":"python -m unittest discover -s tests -q"}
            if not found("record_check"):
                return "record_check",{"requirement":"REQ-1","status":"PASS","method":"Observed unittest exit 0", "source_execution_id":found("run_command")[-1][2]["_execution_id"]}
            return finish("REQ-1 passed with observed unittest evidence.")
        if kind=="review":
            if not found("git_diff"): return "git_diff",{}
            if self.review_correction and not self.review_failed:
                self.review_failed=True
                return finish("Reinspect the implementation and rerun validation.","CHANGES_REQUIRED")
            return finish("Complete diff inspected; scope and tests checked.")
        if kind=="commit" and not found("final_commit"):
            return "final_commit",{"message":"Return two from answer and update its regression test"}
        return finish()

    async def stream(self, profile, messages, tools, on_delta):
        self.active+=1; self.max_active=max(self.active,self.max_active)
        try:
            await asyncio.sleep(self.latency)
            self.calls+=1
            name,args=self.action(messages)
            # Reusing a call ID in each response tests request-scoped identity.
            return Turn({"role":"assistant","content":None,"tool_calls":[{"id":"call_1","type":"function","function":{"name":name,"arguments":json.dumps(args)}}]}, "fixture-model", None,"tool_calls")
        finally: self.active-=1
