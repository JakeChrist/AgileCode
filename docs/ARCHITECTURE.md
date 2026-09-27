# Implementation architecture

## Dependency direction

`desktop / CLI -> Application -> domain + workflow services -> adapters`

`agilecode.core` imports no Qt. One application host owns a project SQLite connection through a worker thread and runs asynchronous model/tool processes. The QML bridge owns an asyncio thread and sends immutable read models and events to Qt. There is no local HTTP application server; HTTP is used only for configured external endpoints.

## Module map

| Module | Responsibility |
| --- | --- |
| core/types.py, defaults.py | Validated Team, TeamMember, Process, executable Step, StepResult, StepContext, Settings, ModelProfile; built-in eight-stage definition. |
| core/application.py | Composition root; ticket commands, ownership, lifecycle, waits, manual edits, recovery, integration entry points. |
| core/runner.py | Process progression, persisted attempts, fresh sessions, helper analysis, step gates, request budgets and final-commit authorization. |
| core/tools.py | Concrete Tool objects and one ToolManager for exact-name exposure, validation, authorization and durable call/result dispatch; RunCommandTool owns command policy and containment. |
| core/context.py | Pinned requirements/plan/findings, coherent tool-exchange selection, explicit omission notices. |
| core/integration.py | Isolated candidates, normal Git merge, conflict-resolution sessions, fresh combined-tree verification/review, exact-OID approval and cleanup. |
| core/backup.py, discussion.py, diff.py | Portable backup/restore, read-only project chat, headless aligned/unified diff data. |
| adapters/store.py | One project SQLite authority, transactions, sequential message IDs and operation journals. |
| adapters/config.py | Global settings/team files, project settings/team records, explicit scope references. |
| adapters/model.py | OpenAI-compatible Chat Completions streaming, response-scoped tool assembly, reasoning and error handling. |
| adapters/workspace.py, search.py, skills.py | Canonical scoped paths, exact edits, bounded code search, skill catalogs and content. |
| adapters/guard.py, shell.py | Practical command checks, one persistent Bash session per runtime, foreground/background jobs, stdin and process-group cancellation. |
| adapters/git.py, checkpoints.py, lock.py | Git argv operations, raw snapshots/private refs, restored-content checks, one engine-owner lock. |
| adapters/mcp.py | Configured MCP subset: initialization, stdio/Streamable HTTP, tools/resources, pagination, roots and process lifecycle. |
| desktop/bridge.py | UI adapter only: command dispatch, event coalescing, read-model responses and source-window identity. |
| desktop/qml/ | Primary desktop presentation: board, discussion, team/settings forms, ticket inspector, waits/recovery. |
| desktop/codeview.py, syntax.py | Selective Widgets source/diff panes; off-thread syntax spans; guarded save requests. |

## Durable boundaries

A completed step result and next-step pointer commit together. A pending external mutation is recorded before execution. Neither SQLite nor an operation journal makes arbitrary shell/MCP calls exactly-once: unknown outcomes stop for inspection. Complete native tool call/result pairs precede another user/model turn. Questions and approval waits have stable IDs tied to the owning attempt, independent of focused UI card.

Source writes use returned byte hashes. Verification evidence and review records are bound to a Git tree fingerprint. Step 8 checks those fingerprints before the final branch commit. Integration binds an approval to both the candidate and target OID; a changed target requires a rebuilt candidate. Checkpoints use private Git refs and a temporary index without moving ticket HEAD.

## Extension points

Save team bundles as data rather than importing arbitrary user code. Add native capabilities as Tool subclasses and pass their objects through the common ToolManager execution path; discovered MCP capabilities are wrapped as MCPTool objects. Extend SearchService without changing the process or agent protocol. An alternative UI calls Application methods and subscribes to Events without modifying core behavior. Do not introduce model-request semaphores, provider families, separate process libraries or per-ticket databases.

The implementation is not a distributed system or an adversarial-code sandbox. Bash, user scripts, Git hooks and MCP servers remain real external programs with ordinary OS permissions. Engine checks focus on explicit workflow boundaries and observable evidence.
