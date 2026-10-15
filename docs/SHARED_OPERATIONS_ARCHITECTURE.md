# Shared operations: implemented architecture

Interruption follow-up: see `CONVERSATION_INTERRUPTION.md` for the shared whole-turn
controller, operation cancellation boundaries and GUI submission correction. The
initial consolidation did not fully unify interruption; this follow-up closes
that specifically reproduced gap.

## Where a change belongs

The application has three responsibilities. Definitions describe tickets, teams,
processes, settings and providers. Application operations change project state.
Runtime code runs agents using those operations. GUI and CLI code display state
and translate requests; they do not implement a second version of a project
operation.

```
GUI action ──────────────┐
CLI command ─────────────┼── application operation ── storage / Git / files
PM or ticket tool ──────┘
                         ↑
                  one policy and ownership boundary
```

`core.application.Application` is the composition root and compatibility facade.
Its operation methods delegate to the owners below. Existing public imports stay
usable without leaving another implementation behind them.

| Responsibility | Canonical owner |
|---|---|
| Validated definitions | `domain/types.py` |
| Ticket creation, edits, relationships and movement | `application/tickets.py` |
| Execution start/resume/pause/cancel, feedback | `application/execution.py` |
| Admission, physical-work ownership, shutdown barriers | `application/coordination.py` |
| Run selection, eligibility, ticket read models | `application/queries.py`, `eligibility.py` |
| Conversation pages, exact requests, PM read models | `application/query_views.py` |
| Source views and comparisons | `application/source_queries.py` |
| Manual source saves and checkpoint restore | `application/services/workspace.py` |
| Agent file operations and mutation receipts | `application/services/agent_workspace.py` |
| Process progress/evidence operations | `application/process_operations.py` |
| Agent conversation loop and exchange records | `runtime/conversation.py`, `conversation_records.py` |
| Tool dispatch and role-specific construction | `runtime/tools.py`, `tool_factories.py`, `pm_tools.py` |
| Settings/providers, shared resource locking | `application/services/configuration.py`, `adapters/config_lock.py` |
| Skills/profiles/catalog operations | `application/services/catalog.py`, `catalog_resources.py` |
| Jobs and artifacts | `application/services/jobs.py`, `artifacts.py` |
| Portable backup and migration rollback | `application/services/backup.py`, `maintenance.py` |

Adapters retain physical storage, Git, shell and model transport work. A shared
operation can use different execution scopes without duplicating its rules.
For example, a PM Git command operates in the project scope and a ticket command
in its worktree; both use the same command operation and validator.

## Moving a ticket

GUI, CLI and PM call the same move operation. Request identity survives wrappers.
A retried request returns its recorded outcome; a changed payload conflicts.
A rejected movement is terminal for that request. Startup does not reinterpret
an old rejected request as permission to move a ticket later.

Leaving In Progress settles owned ticket activity before the move commits.
An operation holding a source-write or Git-apply boundary keeps that ownership
until the physical operation finishes, even if its caller is cancelled.
Completion compares the selected run and transition identity, so an old result
cannot move a ticket after it has left and re-entered In Progress.

The generic storage update helper rejects ticket-column updates. This is a
regression guard, not a substitute for the shared operation's checks.

## Running conversations

Ticket steps, PM and project discussions use the same conversation implementation.
Their prompts, exposed tools and completion rules remain role-specific. Tool
result storage and transcript projection use one writer. Interrupted mutations
retain honest unknown outcomes rather than pretending cancellation undid them.

Input is accepted durably before waiting for older work to settle. Late feedback
that loses the race with final completion is retained as needing follow-up and
shown in the ticket view. It does not silently start another run. Final-commit
reconciliation uses verified Git and journal evidence; the original interruption
warning is retained and later evidence gets its own record.

Permission is rechecked at final tool admission against current policy. Auto
Complete remains a permission on completion through the same operation; there
is no separate Auto Complete executor or state machine.

## Closing and restarting

Close blocks new admission and waits for owned physical work, including manual
operations and post-worker continuations. The desktop host does not stop its
event loop after an arbitrary timeout while a protected file/Git write is alive.
A slow physical operation can therefore delay shutdown; force-killing the process
still requires the durable recovery rules.

Source saves invalidate affected outputs before replacing files and retain
intent. Checkpoint restores record original/target evidence and resolve exact
before/after states. Partial or different files remain blocked for explicit
reconciliation instead of being overwritten blindly.

A process lost in a crash can outlive the host. When safe ownership cannot be
proved, job recovery keeps the ticket blocked. It neither kills an unverified
PID nor marks the process stopped because its Python handle is gone. There is
currently no public “acknowledge and unblock” shortcut for this case; independent
proof that the process has stopped is required before a safe recovery path can
be offered. See `SHARED_SUPPORT_OPERATIONS.md` for the precise limitation.

## Editing settings and definitions

GUI drafts retain the original snapshot rather than silently replacing their
comparison base on refresh. Shared saves compare that captured base. CLI
`--for-edit` exports provide the same contract for existing definitions/settings.
A stale edit fails instead of overwriting another editor's changes.

Definition-only CLI hosts do not construct a model client or agent processor.
An In Progress move in a one-shot host can persist the board change while reporting
that no execution host is available. It does not pretend to leave a worker running.

## Migration and rollback

Older database formats are backed up before a transactional upgrade. Unsupported
future formats fail before migration. Declared global/project resource changes
have separate before-images, post-images and write intents. Rollback preserves
later unrelated edits. Portable exports do not include private migration copies
of global settings.

The code rollback marker and exact baseline are in
`SHARED_OPERATIONS_IMPLEMENTATION.md`. Code rollback is separate from data
rollback: do not launch an older binary against a newer unsupported database.

## Boundaries retained from the approved plan

This consolidation does not decide a new policy for editing unfinished inputs,
rerunning completed tickets, individual pauses during Resume All, PM Start's
existing automatic-integration default, live customized skill replacement, or
one-shot CLI hosting. Existing behavior is retained unless the shared operation
must reject a stale, conflicting, or physically unsafe request.
