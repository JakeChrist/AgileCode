# Implementation and qualification record — 0.1.0

Design authority: `design_v1.1.md`. This is a functional first source implementation, not a declaration that all 60 design acceptance scenarios or all performance/accessibility targets have passed.

## Evidence actually obtained

- **88 automated tests passed**, with no failures or skips in the recorded build environment. See `test-results.txt` and `test-results.xml`. The final run includes the terminal-run pause guard and backup restore policy corrections.
- Tests exercise real temporary Git repositories, worktrees, hidden checkpoint refs, commits, merge candidates, SQLite databases, Bash processes and file operations. Native tool streaming is exercised against both protocol fixtures and a real loopback HTTP server.
- The deterministic HTTP demo completes all eight process steps, performs real edits and unit tests, creates the final commit, integrates it and checks cleanup. It uses a protocol fixture, not pretrained inference.
- MCP tests exercise a real local stdio process, initialization/roots, paginated discovery, tools and resources. Streamable HTTP is exercised with a controlled HTTP transport fixture.
- Recovery tests cover a question surviving an engine restart without changing its attempt/session, uncertain-operation handling, private-ref reconciliation and backup restoration into a different project location.
- A real Git textual-conflict test generates an isolated resolution through the model-protocol fixture, preserves both branches' intended changes, runs fresh verification/review, and holds the target branch until approval.
- The concurrency test observes overlapping requests from two ticket executions; there is no inference semaphore.
- Measured statement coverage is recorded in `coverage.json` and the test report. The approximately **69% overall figure includes unexecuted GUI and CLI modules**; it is not evidence of full feature qualification.
- The demo and CLI help also passed from a fresh extraction of the source ZIP, with imports explicitly pointed at the extracted source. See `release-smoke-test.txt`.
- Python source compilation and editable installation were checked separately. The source ZIP excludes caches and installed dependencies. A wheel packaging check is described in `packaging-check.txt`.

## Implemented feature map

| Specification area | Delivered implementation | Principal executable evidence |
| --- | --- | --- |
| FR-01–08: projects, tickets, board and team ownership | Original-root identity, four board states, CRUD/archive/reopen/order/dependencies, global/project whole-team definitions, frozen run snapshots, idempotent starts | `test_domain_storage.py`, `test_workflow.py`, `test_controls_views.py` |
| FR-09–14: executable process | Eight enforced steps, Step.run(context), fresh sessions, structured results, durable completion/pointer transaction, separate correction attempts, same-session questions | `test_workflow.py`, `test_git_recovery.py`, `test_integrations_protocol.py` |
| FR-15–18: model protocol | Generic compatible Chat Completions client, profile overrides, exact request records, SSE/native tools, returned reasoning and nullable usage, ordinary error handling | `test_shell_protocol.py`, `test_integrations_protocol.py`, concurrency test in `test_git_recovery.py` |
| FR-19–23: tools, permissions and shell | Common allow/ask/deny path, role/stage checks, scoped file tools, practical CommandGuard, real persistent Bash, owned jobs/stdin/output/cancellation | `test_workspace.py`, `test_shell_protocol.py`, `test_controls_views.py` |
| FR-24–27: discovery, changes, skills, MCP | Bounded literal/regex/path search, Python AST symbols and textual references, exact hash-aware edits, skill catalogs/content, MCP stdio/Streamable HTTP tool/resource subset | `test_workspace.py`, `test_integrations_protocol.py`, `test_controls_views.py` |
| FR-28–33: Git and integration | Temporary-index raw snapshots/private refs, guarded restoration, step-8 commit gate, concurrent worktrees, normal merge then AI candidate, fresh combined-tree checks, stale approval rejection and configurable cleanup | `test_git_recovery.py`, `test_integrations_protocol.py`, `test_workflow.py` |
| FR-34–36: persistence, recovery and headless operation | One project SQLite store, artifact files, operation journals, explicit unknown-outcome reconciliation, engine-owner lock, UI-independent Application API | `test_domain_storage.py`, `test_git_recovery.py`, `test_workflow.py` |
| FR-37–38: desktop and inspectability | QML board/inspector/settings/team/recovery surfaces; selective QWidget source/diff windows; actual member/model/prompt/evidence read models | Core read models and syntax/diff transformations tested. **Qt runtime and QML presentation unverified.** |
| FR-39–42: exports, discussion, context and control | Hash-manifest project backups with Git bundle, ticket export, read-only discussion, coherent context trimming, pause/feedback/manual-edit and cancellation paths | `test_git_recovery.py`, `test_controls_views.py`, `test_workflow.py`; not every interruption interleaving is covered |

The mapping above is a feature-to-test guide. The numbered design acceptance scenarios are retained unchanged in the input specification. No equivalence between “88 tests” and “60 acceptance scenarios” is implied.

## Qualification boundaries and narrower choices

### Desktop interface

The QML and Qt Widgets implementation is present. **PySide6 could not be installed in this environment**, including a direct package-download attempt. Consequently QML loading, signals/properties under a real Qt runtime, drag-and-drop, window focus, native platform plugins, DPI scaling, visual polish and screen-reader behavior are not runtime-qualified. Do not treat the provided UI source as a certified release build. Core source/diff transformations and UTF-16 syntax positions have executable tests independent of Qt.

The initial team editor edits and validates a complete JSON team bundle, with scope selection. It is not a graphical workflow designer or a form for every nested team property. Advanced settings/MCP configuration also expose validated JSON. The main board uses QML list data supplied by the bridge rather than a fully optimized database-backed virtual model. Large-board performance targets are not measured.

### Model behavior

LM Studio and llama.cpp are the intended actual server targets. Tests validate the compatible wire protocol and workflow controls with deterministic fixtures; **no live model weights or actual LM Studio/llama.cpp server were available**. Model tool-use quality, correct template configuration, long reasoning streams, context limits and throughput need local validation. A model identifier is only auto-selected when the server advertises one candidate. AgileCode does not manage model loading.

### Editing and discovery

The source viewer supports bounded manual edits and unified/aligned differences, not a full IDE. Python symbols use the standard AST; other languages rely on lexical/path/reference-candidate searches. There is no multi-language semantic reference graph, language-server integration or embeddings index. The exact-edit tool does not fuzzily apply guessed patches. Very large/binary files are explicitly limited rather than loaded into the normal text editor.

### Context and process

Older model/tool exchanges are retained or omitted coherently with explicit notices. Required handoff artifacts are pinned. The implementation does not create an additional model-generated rolling summary; oversized pinned artifacts lead to an explicit context-budget stop. Read-only helpers are attached to the parent run and cannot replace standard steps. The standard process requires recorded verification sources for requirements and a review of the current diff; this does not mathematically prove an agent's conclusion is correct.

### Shell and external tools

The persistent Bash implementation uses a control channel and pipes. It is **not a PTY terminal emulator**: full-screen programs, terminal-password interactions, interactive shell job-control applications and arbitrary shell-profile behavior are not supported as if it were a normal terminal emulator. Shell volatile state cannot survive process death. Managed background jobs inherit a captured cwd/exported environment rather than unexported functions/aliases.

CommandGuard catches ordinary path/directory and unauthorized Git mistakes. It cannot infer every indirect file effect of `python script.py`, a build tool, a Git hook or an MCP server. It is workflow guarding, not OS isolation. There is no bubblewrap dependency.

### MCP

Implemented: configured stdio and Streamable HTTP clients, initialization, roots, tool/resource discovery (including pagination), argument validation, result processing and process/session cleanup. Unsupported advanced capabilities are not simulated. There is no OAuth onboarding, marketplace, complete durable SSE resumability, server-side sampling or elicitation UI, or remote-server sandbox. HTTP behavior is fixture-tested; interoperability with an arbitrary third-party MCP server is not certified.

### Git, merging and recovery

Textual conflicts can be resolved by an agent in an isolated candidate and then verified/reviewed. Binary/submodule/oversized or otherwise ambiguous conflict cases stop for inspection; a full binary/manual-conflict editor is not implemented. No auto-push or branch-retention scheduler is added. Stale/abandoned integration candidates may remain for inspection rather than being silently garbage-collected.

Checkpoint restore guards current content, preserves a pre-restore recovery point and does not rewind the normal ticket branch. Backup restoration requires a new directory; it is not an in-place system image restore. Unknown shell/MCP effects require a user decision instead of blind replay. Tests cover selected failure windows, not every possible machine/power-loss fault.

### Platform and distribution

The qualified core environment is Linux x86-64 with Python 3.13.5, Git and Bash. This is a source installation, not a packaged Linux desktop installer or Windows/macOS release. Dependency ranges are declared in `pyproject.toml`; actual test versions are in `environment.json`. Dependency upgrades and operating-system changes need retesting. No CI matrix, cloud services or downloaded model weights are required or included.

## Remaining release work

Run the UI and a small real-model ticket on the target Linux desktop; validate configuration, keyboard/focus and unsaved-change interactions before autonomous use on important work. Measure the specification's responsiveness/large-repository targets on representative projects. Add broader endpoint/server/GUI regression evidence and close the narrower capability gaps above before declaring full v1.1 acceptance.

These are documented implementation/qualification boundaries, not newly requested product decisions.
