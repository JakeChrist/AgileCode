# Ticket pause, conversation scrolling, and Python environments

Historical record: this document predates the shared-operations consolidation.
For current ownership, see `SHARED_OPERATIONS_ARCHITECTURE.md`; for the current
rollback boundary and qualification status, see `SHARED_OPERATIONS_IMPLEMENTATION.md`.
The results below describe their recorded revision, not the current checkout.

Based on master `70554d6d8390c739170f8213b28ac88784b50685`.

## Behavior

- Pause blocks dispatch, cancels the ticket task and model stream (including context summarization), stops foreground/background process groups and closes MCP connections before acknowledging Paused. Partial model output and command effects are retained. Stopped commands get an interrupted result and are not automatically replayed. Other uncertain mutations still require recovery. Answers do not implicitly restart an explicitly paused ticket. Pause and Resume are serialized per ticket.
- MCP discovery can reconnect after pause. Cancelling discovery closes its in-progress connection. Ticket Python isolation does not alter separately configured MCP environments.
- Conversation rows are reconciled by identity and the streaming row is promoted in place. Tail following is throttled without debounce starvation. Wheel and scrollbar input stop following; a visible message/offset anchors the reader through history refreshes and prepends. Disclosure clicks preserve reading position. Jump to latest explicitly restores following.
- Python projects receive a worktree-local `.venv`, selected automatically for foreground and background commands. Settings expose automatic/always/disabled modes, the interpreter and custom setup commands. Automatic installation respects uv.lock, requirements.txt, or project packaging; other dependency managers require a declared setup command. Environments are reused across pause and restart, ignored by Git, and removed with worktree cleanup. Failed setup must succeed before model execution can resume.

## Validation

- Full suite checkpoint: **204 passed in 94.45 seconds**.
- After the final setup-retry fix and its new regression, the complete pause/environment group passes: **13 passed in 41.88 seconds**. This adds one test beyond that full-suite checkpoint.
- Tests exercise real Qt wheel input and scrollbar dragging, rapid streaming, live-to-persisted identity, reading-position preservation, disclosure state, settings save/load, real process termination, late delta suppression, setup and context-summary cancellation, unaffected concurrent tickets, MCP reconnection, and environment creation/reuse/background inheritance. The application lifecycle test pauses, closes/reopens the application, resumes the same ticket, integrates it and verifies that worktree cleanup removes its environment.
- Corrected existing UI test mechanics: retain PySide wrappers, inspect the Qt visual tree, wait for drawer animation before pointer input, use valid multiline Markdown, and replace obsolete implementation-string scroll assertions with behavioral checks.
- `git diff --check` passes.

Validation runtime: CPython 3.12.14 and PySide6 6.11.2 on Linux, using offscreen Qt rendering. Python 3.13+ was not available for confirmation. The full-suite command unsets the execution environment's proxy variables for the loopback HTTP fixture; no external model server was used.

The compiled PDF manual is not regenerated; `docs/USER_MANUAL.md` contains the updated behavior and environment configuration instructions.
