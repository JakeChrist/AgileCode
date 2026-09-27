# Tool-system refactor verification

Completed from `refactor/simple-tool-manager` at `bb4bd454d3fc813ae52799cacefc70e85189f71a`, based on master `5c8e6f7bb68217c3391036c7506522b557178b49`.

## Implemented behavior

- Native tools and discovered MCP tools implement the same Tool interface. ToolManager exposes exact allowed names, validates calls, delegates authorization and execution to the tool, and persists correlated results.
- No ToolSpec, ToolRegistry, ToolExecutor, ToolContext or ToolFactory remains in the implementation. Member permissions have no category matching, wildcard matching, automatic control-tool grants or step-kind filtering.
- Team format version 2 saves exact names. Version 1 native selectors and implicit control tools migrate once; dynamic MCP permissions must be explicitly selected from discovery.
- CommandPolicy produces allow/ask/deny; CommandGuard produces ok/block. The command tool owns these decisions. Full autonomy is unchanged unless an explicit command/tool rule applies. Rules default to an empty list.
- Settings support adding, editing, ordering and removing command rules. Team editing supports tool checkboxes, Select all, Clear and MCP discovery with stable aliases.
- Interrupted effectful calls remain unknown across restart. Completed calls are not replayed, stale approvals are rejected, and unexposed or invalid calls do not execute.

## Results

- 35 new behavioral tests pass (`test_tool_system.py` and `test_tool_settings_qml.py`). They cover exact-name exposure, migration, rule ordering, approval and denial, stale approval, containment, persistence/replay, interrupted-call recovery, real stdio MCP dispatch, discovery, schema errors, and real Qt settings save/load.
- The complete suite reports **179 passed, 8 failed**.
- The remaining failures are in the existing conversation Qt tests and a discussion UI source assertion. Testing the untouched starting branch separately also reports **8 failed, 23 passed** in those two UI test modules. Timing-sensitive conversation failures vary between runs. They involve Markdown styling, disposed/deferred QML items, streaming rows, scrolling/disclosure behavior, description line counts, and an outdated source-string assertion. They are not presented as passing.
- The existing 111-test core/workflow/recovery group passes, as do the 10 protocol integration tests, including the real local HTTP/SSE workflow demo. The diagnostic demo now advertises its context length as provider metadata so that end-to-end test can reach tool execution.
- `git diff --check` passes.

Tests ran with the available CPython 3.12.14 environment and installed PySide6 offscreen backend. The project's declared CPython 3.13+ runtime was not available here; that version still needs CI/local confirmation. Tests use deterministic model fixtures and real local Git, shell, HTTP/SSE and MCP processes; they do not measure live-model performance.
