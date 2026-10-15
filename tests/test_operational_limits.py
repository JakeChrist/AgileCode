"""Regressions for previously hidden operational ceilings."""
import asyncio
import json

import pytest

from agilecode.adapters.git import GitRepository
from agilecode.adapters.mcp import McpConnection
from agilecode.adapters.model import StreamAssembler
from agilecode.adapters.search import SearchService
from agilecode.adapters.shell import PersistentShell
from agilecode.adapters.workspace import Workspace
from agilecode.core.tools import WriteFileTool
from agilecode.core.types import AgileCodeError, McpServerConfig, Settings, StepResult


def test_fragmented_model_stream_is_not_capped_by_protocol_overhead():
    assembler = StreamAssembler()
    # Formerly rejected after 8 MiB of repeated JSON envelopes.
    for _ in range(4500):
        assembler.feed({"choices": [{"delta": {"reasoning_content": "x" * 2048}}]})
    assembler.feed({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    turn = assembler.finish()
    assert len(turn.message["reasoning_content"]) == 4500 * 2048


def test_large_valid_step_outputs_and_tool_arguments_are_accepted(tmp_path):
    result = StepResult(status="PASS", summary="s" * 5000, output="o" * 250000)
    assert len(result.output) == 250000
    ws = Workspace(tmp_path, tmp_path, tmp_path)
    tool = WriteFileTool(ws, asyncio.Lock())
    args = tool.validate(json.dumps({
        "path": "big.txt", "content": "w" * 4_100_000, "create": True
    }))
    assert len(args["content"]) == 4_100_000


def test_workspace_large_file_and_byte_continuation(tmp_path):
    scope = Workspace(tmp_path, tmp_path, tmp_path, read_limit=1024)
    content = "begin\n" + "x" * (9 * 1024 * 1024) + "\nend\n"
    scope.write("large.txt", content, create=True)
    first = scope.read("large.txt", start_line=1, end_line=1)
    assert first["content"] == "begin\n"
    middle = scope.read("large.txt", byte_offset=6)
    assert middle["content"] == "x" * 1024
    assert middle["next_byte_offset"] == 1030
    assert middle["partial"] is True


def test_byte_paging_does_not_split_utf8_codepoints(tmp_path):
    ws = Workspace(tmp_path, tmp_path, tmp_path, read_limit=3)
    original = "A😀B"
    ws.write("utf8.txt", original, create=True)
    first = ws.read("utf8.txt", byte_offset=0)
    assert first["content"] == "A😀"
    assert first["next_byte_offset"] == 5
    second = ws.read("utf8.txt", byte_offset=first["next_byte_offset"])
    assert first["content"] + second["content"] == original


@pytest.mark.asyncio
async def test_git_snapshot_accepts_files_above_old_32_mib_cap(repository):
    file = repository / "large-source.bin"
    file.write_bytes(b"L" * (33 * 1024 * 1024))
    git = GitRepository(repository)
    tree = await git.snapshot_tree(repository)
    entries = await git.tree_entries(tree)
    assert "large-source.bin" in entries


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_command", [None, "read-tree", "update-index", "write-tree"])
async def test_standalone_snapshot_preserves_head_index_and_cleans_private_index(repository, monkeypatch, failure_command):
    git = GitRepository(repository)
    assert not (repository / ".agilecode").exists()
    original_head = await git.head()
    index_path = repository / ".git" / "index"
    original_index = index_path.read_bytes()
    (repository / "new.txt").write_text("untracked snapshot input\n")
    original_run = git.run

    async def run(*args, **kwargs):
        if args[0] == failure_command:
            raise AgileCodeError("Injected private-index failure")
        return await original_run(*args, **kwargs)

    monkeypatch.setattr(git, "run", run)
    if failure_command:
        with pytest.raises(AgileCodeError, match="Injected private-index failure"):
            await git.snapshot_tree(repository)
    else:
        tree = await git.snapshot_tree(repository)
        entries = await git.tree_entries(tree)
        assert "new.txt" in entries
        assert all(not path.startswith(".agilecode/") for path in entries)

    assert await git.head() == original_head
    assert index_path.read_bytes() == original_index
    assert not list((repository / ".agilecode").glob("agilecode-index-*"))


@pytest.mark.asyncio
async def test_search_cursor_recovers_all_matches(repository, tmp_path):
    file = repository / "sample.txt"
    file.write_text("needle 1\nneedle 2\nneedle 3\nneedle 4\nneedle 5\n")
    ws = Workspace(repository, repository, tmp_path)
    service = SearchService(ws, GitRepository(repository), lambda: Settings())
    first = await service.search("needle", limit=2)
    assert len(first["matches"]) == 2
    assert first["next_cursor"]
    second = await service.search("needle", limit=2, cursor=first["next_cursor"])
    assert [x["start_line"] for x in second["matches"]] == [3, 4]
    third = await service.search("needle", limit=2, cursor=second["next_cursor"])
    assert [x["start_line"] for x in third["matches"]] == [5]
    assert third["partial"] is False


@pytest.mark.asyncio
async def test_mcp_pagination_has_no_arbitrary_100_page_limit(tmp_path):
    config = McpServerConfig(id="stub", command="python", enabled=True)
    conn = McpConnection(config, tmp_path)

    async def request(method, params):
        cursor = int(params["cursor"]) if "cursor" in params else 0
        return {"tools": [{"name": str(cursor)}],
                "nextCursor": str(cursor + 1) if cursor < 105 else None}

    conn.request = request
    tools = await conn.list_items("tools/list", "tools")
    assert len(tools) == 106


@pytest.mark.asyncio
async def test_shell_environment_not_limited_by_preview_size(tmp_path):
    shell = PersistentShell(Workspace(tmp_path, tmp_path, tmp_path), preview_bytes=64)
    try:
        value = "Y" * 70000
        await shell.execute("export LARGE_ENV_VALUE=" + value)
        environment = await shell.exported_environment()
        assert environment["LARGE_ENV_VALUE"] == value
    finally:
        await shell.close()


def test_operational_settings_do_not_add_model_inference_parameters():
    settings = Settings(
        max_corrections=None, git_operation_timeout_seconds=None,
        search_file_budget=None, shell_log_max_bytes=None
    )
    restored = Settings.model_validate_json(settings.model_dump_json())
    assert restored.max_corrections is None
    assert restored.git_operation_timeout_seconds is None
    assert "max_tokens" not in Settings.model_fields
    assert "context_window" not in Settings.model_fields
