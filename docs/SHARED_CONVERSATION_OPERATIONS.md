# Shared conversation and tool operations

Interruption follow-up: see `CONVERSATION_INTERRUPTION.md` for the shared whole-turn
controller, operation cancellation boundaries and GUI submission correction. The
initial consolidation did not fully unify interruption; this follow-up closes
that specifically reproduced gap.

This note maps the conversation changes from baseline `411cd20`. It complements
`SHARED_OPERATIONS_ARCHITECTURE.md`; it describes the implemented boundaries and
tested cases, rather than promising recovery from every external failure.

## Ownership and actual callers

| Previous implementation | Canonical owner | Current callers |
|---|---|---|
| Ticket `AgentRunner.run`, PM `_execute_turn`, discussion `ask` stream/dispatch loops | `runtime/conversation.py`: `ConversationEngine.run_turn`, `generate_and_record`, `close_outstanding_calls` | `AgentRunner.run`, `ProjectManager._execute_turn`, `ProjectDiscussion.ask` |
| Role-local summary streams and discussion draft streams | Engine context preparation and recorded `one_shot` requests | All three roles; `ProjectDiscussion.draft_ticket` uses `one_shot` without executing its draft schema as a mutation |
| Role-local tool-result append/recovery paths | `runtime/conversation_records.py`: `complete_execution_exchange_once`, `append_tool_result_once` | Shared engine, `ToolManager`, startup recovery |
| Inline role tool registries | `runtime/tool_factories.py`: `ticket_tools`, `project_manager_tools`, `discussion_tools` | `ProcessRunner.execute_step`, PM `_tool_manager`, discussion `_config` |
| `core/tools.py` implementations and PM-local tool classes | `runtime/tools.py`, `runtime/pm_tools.py` | Role factories; old `core.tools` import aliases the same module, including private compatibility names |
| Progress/evidence database writes inside tool classes | `application/process_operations.py` | `UpdateProgressTool`, `RecordCheckTool`; conversation recovery reads their execution receipts |
| File mutation thread/lock handling inside individual adapters | `application/services/agent_workspace.py` | Write/edit/move/delete tool adapters; paused manual saves use the same write operation with their own workflow checks |
| Ticket/PM Git command validation and execution bodies | `application/services/git_commands.py` | `GitCommandTool` and `ProjectGitCommandTool`, retaining worktree/project execution scopes |
| PM compact ticket and pending-question SQL | `application/query_views.py`: `pm_ticket`, `pm_pending_waits` | PM `_get_ticket`, `_pending_questions` |

The old role-specific provider loops, summary streams, pending-call dispatch
implementations, and tool-result transcript writers were removed rather than
retained as fallback engines. PM `_dispatch_pending` remains a compatibility
delegate. `ProcessRunner.run` still sequences process steps; `_gate` and `_commit`
remain the process lifecycle owners bound by `FinishStepTool` and `FinalCommitTool`.
Factories construct tools and do not run conversations.

## Admission, role boundaries, and context

- Input acceptance stores the exact text and an idempotent receipt before waiting
  for older session work. Request input versions and control revisions govern
  unstarted dispatch; a correction that wins admission retires obsolete calls.
- Tool identity includes the request and provider call ID. Reusing an identity
  with different arguments/session or supplying a conflicting saved result is
  rejected. Large results retain the full outcome and one stable transcript preview.
- `ToolManager` owns validation and authorization. Its final current-settings,
  exposure, and dispatch checks share `application/policy.py`'s short reservation
  with settings publication. The reservation ends after task admission; it is not
  held while asking for approval or awaiting the entered tool. The engine does
  not implement a second permission policy.
- Ticket tools remain limited to the assigned member's allowed tools and ticket
  policy. PM retains its separate full-autonomy tool settings and optional
  question-answer/archive/delete exposure. Restricted PM still responds to user
  direction; resuming accepted paused input does not add an autonomous loop.
- Discussion tools remain exactly `read_file`, `list_files`, `code_search`,
  `load_skill`, and `create_ticket`. Draft generation does not dispatch tools.
- `core/context.py` remains the context algorithm owner used by the engine.
  Control messages and current input stay exact, complete tool exchanges remain
  grouped, summaries must make byte-based progress, and persisted history is not
  rewritten by request compaction. Summary generation uses the same recorded,
  cancellable request path.

## Context summarization recovery

All three conversation roles share context compression through `ContextManager`.
Only final assistant text is accepted as a summary: empty, truncated, reasoning-only
and non-reducing replies are rejected. Each unsuccessful compression can be retried
with a different complete-exchange selection and smaller summarization chunks.
The project/global application setting `context_summarization_retry_attempts`
defaults to three attempts including the first; successful compression resets
the counter. Provider transport retry settings are independent.

Summary generations remain recorded as `context_preparation` requests. Their
input metadata records source bytes and attempt number; failed validations record
the reason and input/output byte counts. The successful main request view records
aggregate attempts and failures. Full conversation history remains unchanged,
and failed attempts never replace the retained context checkpoint.

## Settlement and recovery

`conversation_records.py` stores dispatch claims, input/turn receipts and result
projection. Cancelled entered mutations remain uncertain unless a local durable
receipt proves their outcome. An unresolved external effect blocks continuation;
recovery does not generally retry mutations. A durable `ask_user` wait can resume
after pause or restart without creating another attempt or replaying a mutation.

PM turn outcomes first record a database intent. `_reconcile_state_intents` applies
that intent through the single reserved `_change_state` file writer before another
turn is admitted. A newer control revision supersedes an older outcome; a failed
file settlement remains pending. The reservation survives cancellation of its
awaiting caller. Discussion shutdown similarly owns the complete ask/draft task
and waits for its engine/tool finalizers before the store is released.

If final-step completion wins the feedback race, its accepted result is retained.
The exact later input gets a `needs_followup` receipt and `FeedbackNeedsFollowup`
activity bound to that run/session, exposed by ticket queries and the existing UI.
It is neither marked answered nor used to start another run automatically.

`reconcile_final_commit_execution` is a narrow exception to leaving uncertainty
unresolved: it checks the saved commit intent against Git worktree/branch, HEAD,
tree, parent and message evidence. It records verified recovery separately while
preserving the original interrupted result and warning. A separate context notice
is projected after outstanding tool exchanges close. Reopening or resuming uses
that evidence without making another commit; mismatched evidence stays unknown.

## Focused regression coverage

- `test_shared_conversation_engine.py`: input/dispatch ordering, policy publication,
  replay identity, one result projection, current-input retries, shared session
  ownership, PM state-intent cancellation, paused-input resume and role delegation.
- `test_conversation_lifecycle.py`: real Application feedback in both finish/input
  orders, current configuration replacement, discussion shutdown and actual role
  factory bindings.
- `test_context_compaction.py`: controls, exchange grouping, Unicode byte budgets,
  progress checks, cancellation and unchanged stored history.
- `test_process_tool_operations.py`: real shared operation bindings, evidence gates,
  execution receipts, concurrent replay, worker cancellation and restart recovery.
- `test_final_commit_reconciliation.py`: cancellation at prepared/completed journal
  boundaries, reopen twice and resume without another effect, original warning
  preservation, and rejection of mismatched evidence.
- Existing tool-system, PM, ticket-pause and Git-recovery tests cover the retained
  role policies, pause/resume behavior and durable question restart path.
