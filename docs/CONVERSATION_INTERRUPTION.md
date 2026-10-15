# One interruption path for every conversation

## Why this correction was needed

The earlier consolidation shared the model/tool loop but left different
interruption behavior in the role wrappers. PM interruption cancelled its whole
turn; ticket interruption could miss a tool because it looked only for active
model generation. A protected PM `start_ticket` operation could finish provisioning
and launch a ticket after the user's correction had already been accepted.

The PM also delayed its history notification until old work finished stopping,
while the GUI cleared the input immediately. The message could be stored safely
and still appear to have vanished. Failed sends did not restore the draft.

Those were real gaps. The earlier passing tests did not establish end-to-end
interruption equivalence. This correction adds tests for the actual effects,
public controls and GUI routes described below.

## Shared ownership

`runtime/conversation.py` owns the complete conversation turn, including setup,
context preparation, model generation, tool execution and final settlement.
`conversation_records.py` supplies the shared controller and durable exchange/input
records. PM, ticket and discussion adapters use that controller rather than their
own generation-only cancellation logic.

The shared engine tracks request wrappers that have not yet reached turn admission
as well as admitted turns and queued callers. Shutdown therefore cannot close the
database while an accepted send is still suspended before starting its turn.
Backup uses that same whole-request ownership instead of checking only model
generation. Cancelled partial MCP discovery closes its client handles before a
correction retries setup, avoiding retained aliases and leaked connections.

An input receipt is committed before cancellation or waiting for old work. Its
version invalidates unstarted work from an older input version. The accepted-input
event is published immediately; it does not wait for old tool cleanup.

## What interruption does

1. Mark the shared turn interrupted and signal its cancellation token.
2. Cancel cancellable current work, including model requests and executing tools.
3. Keep ownership of any already-entered file/Git effect until it has settled.
4. Refuse subsequent operation phases and new launches under that cancelled token.
5. Record known outcomes honestly, close every unstarted sibling tool call as
   not executed, and retain uncertainty where an external effect cannot be proved.
6. For a correction, continue with the saved input once the old work is safely
   settled and the caller's lifecycle policy permits it.

`application/cancellation.py` provides the cooperative token. Application
operations check it at admission and before later physical/execution phases.
This is different from shielding an entire routine and allowing all of its later
steps to run. An already-started worktree creation can finish safely without
launching the ticket afterward.

The result of an already-completed protected effect is not replaced with a generic
unknown error merely because its caller was cancelled. Truly uncertain effects
remain blocked for review; interruption is not a claim that they were undone.

## Policies that remain distinct

The mechanics are shared. The controls still have their intended meanings:

| Control | Lifecycle policy |
|---|---|
| Send a correction during a turn | Interrupt old turn; use the accepted correction after safe settlement |
| Stop response | End that conversation response without disabling the project manager or cancelling unrelated tickets |
| PM Pause / Turn Off | Stop its conversation and persist the corresponding automation state; new input stays queued until allowed to run |
| Ticket Pause / Cancel | Use shared conversation interruption, then settle the ticket's broader runtime, shell/MCP and recorded execution state |
| Discussion archive | Refuse active work rather than silently becoming a second Stop implementation |
| Application close | Close admission and drain owned requests, turns and physical work before releasing storage |

Already-admitted independent ticket runs and already-returned background jobs have
separate owners. Stop response is not Pause All and is not an undo operation.
Use the relevant ticket/job control for those owners. A cancelled operation cannot
use that distinction to launch new independent work afterward.
Independent tasks also detach the launching conversation's identity. Their later
ticket lifecycle events must reach PM monitoring rather than being mistaken for
the PM's own synchronous management-tool events.

Interrupted deletion/rewind journals distinguish deliberately stopped work from
crash-recovery work. Reopening does not silently resume a deliberately stopped
operation. Partial destructive effects retain an explicit blocker. Checkpoint
restoration that already entered its physical phase settles the required
file/index/history consistency work before releasing ownership.

## GUI submission and activity

All three send wrappers use the same submission helper. It adds a visible pending
user row with a stable input identity, then reconciles it with the durable receipt
and saved history. Receipt events, command replies and history can arrive in a
different order without creating duplicate messages.

A confirmed failed, unaccepted send preserves its original text. Restoration does
not overwrite a newer draft. If acceptance cannot be determined, the UI reports
unknown send status and keeps the original input identity for a safe retry.
A failure after durable acceptance does not turn an accepted message into an
unsent draft.
Ticket feedback retries recover the original receipt before selecting the current
attempt or checking whether execution is still active. The same input ID cannot
create a second correction merely because the ticket advanced to another step.

Stop and correction availability use shared whole-turn activity, not merely the
presence of streaming model tokens. Common turn-start/finish events and initial
snapshots carry turn identities and per-session revisions, so an old snapshot or
finish event cannot hide a newer active turn. Pending user inputs are labelled as
sending/queued/failed rather than as interrupted assistant responses.

## Verification targets

- The same public PM, ticket and discussion controls reach shared interruption.
- A correction during a running read/command or approval wait prevents later
  unwanted mutations and reaches the subsequent model request when safe.
- PM correction/Stop/Pause/Off/shutdown during provisioning never launches the
  ticket afterward; reopening twice does not launch it later.
- Repeated and concurrent corrections preserve every accepted input, without
  duplicate effects or an old Stop poisoning a later turn.
- Unknown effects block continuation but still close unstarted sibling exchanges.
- Pause/Off during slow state persistence cannot admit a fresh response; queued
  input is consumed only after explicit Resume.
- Close waits for requests accepted before turn admission and for physical work.
- Real Qt tests cover all three send controls, delayed/out-of-order receipts,
  failure recovery, newer drafts, stable retry identity and tool-phase Stop.

The tests are in `test_shared_interruption.py`, `test_interruption_contract.py`,
`test_operation_interruption_boundaries.py`, `test_message_submission_qml.py` and
`test_submission_receipt_query.py`. Final suite/package results belong to the
qualification record for the published correction; these targets alone are not
a declaration of an error-free application.
