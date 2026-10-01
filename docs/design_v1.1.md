# AgileCode

## Software Architecture and Detailed Design Specification

**Version 1.1 — Consolidated design decisions**  
**22 September 2026**  
Status: implementation specification; supersedes v1.0.

# 1. Purpose, revision authority, and agreed baseline

## 1.1 Purpose

AgileCode is a local application for executing software-development tickets through teams of coding agents. The user organizes work on a conventional Kanban board. A programmatically controlled process assigns each step to a team member, preserves the result, and advances only when the step's completion conditions are met. Each ticket works in its own Git worktree. Completed work is committed on that ticket's branch and integrated according to the user's merge policy.

This is a clean, object-oriented Python implementation of the functionality, not a port of Zoo Code's class hierarchy. Zoo Code is a behavioral reference only. Cline/Roo configuration, extension APIs, inherited prompts, task stacks, migration formats, cloud services, and provider-specific infrastructure are not part of the implementation.

The document is intended for implementation, review, and testing. It specifies responsibilities, interfaces, persistent records, lifecycle rules, exceptional paths, and observable acceptance behavior. It describes intended software; it does not claim that the software or the performance targets have already been implemented or verified.

## 1.2 Revision authority

Version 1.1 replaces the version 1.0 specification with the decisions confirmed in the subsequent design discussion. The latest 65-page DOCX edition of v1.0 is the source baseline; the earlier Markdown edition had different content and is not used as the authority for this revision. The revised Word, PDF, and Markdown editions are generated from one common text.

A confirmed user decision takes precedence over an earlier proposal, example, or assistant recommendation. Rejected alternatives are recorded only in the supersession register, not retained as competing defaults. Remaining numerical limits, module names, serialization choices, and platform packaging details are identified as engineering defaults or implementation details. They are not newly attributed to the user.

The requirement vocabulary is: **MUST** for a required behavior, **SHOULD** for the engineering default, and **MAY** for a permitted extension. The detailed decision-to-section map is in Appendix D. No previously resolved architectural question is reopened by this document.

## 1.3 Confirmed baseline

| Area | Confirmed decision |
| --- | --- |
| Functional implementation | Headless, object-oriented Python core. The UI is replaceable and contains no development-workflow authority. |
| Desktop presentation | PySide6 with Qt Quick/QML as the primary presentation layer. Widgets are permitted selectively, especially for code/diff components. |
| Models | One OpenAI-compatible client, initially targeting local LM Studio and llama.cpp servers. No additional provider families or model-request scheduler. |
| Model selection | Use the active runtime model by default; a member may specify an optional override. Record the resolved model for each step attempt. |
| Board | Backlog, Ready, In Progress, Done. Detailed execution and integration statuses are internal badges/details, not additional columns. |
| Team ownership | Team owns its members and one or more processes. Process owns ordered steps. Each step references a member of that team. |
| Definition scope | Save a team together with its members and processes globally or within the current project. |
| Standard process | Understand ticket; requirements and verification criteria; architecture; detailed plan; implementation; verification and validation; code review; final commit. |
| Step execution | Step has a name, assigned member, prompt, and run(context) method. Sequence and gates are enforced by the program, not a skill. |
| Handoffs and recovery | Fresh agent session for each step attempt. Persist completed outputs immediately. Pass the ticket, workspace, and relevant prior outputs through StepContext. |
| Corrections | Structured StepResult; separate persisted attempts. Failed verification or requested review changes return to implementation, then verification, then review. |
| Worktree and branch | One worktree per ticket beneath <project>/.agilecode/worktrees/. Start from the branch active in the original project when execution begins; record its exact base commit. |
| Parallel work | Multiple tickets can execute concurrently. The provider handles inference concurrency. AgileCode coordinates only its own state and conflicting repository/worktree mutations. |
| Shell | A real persistent shell belongs to each active ticket/worktree, not to each command or step. |
| File access | Ticket agents can read AgileCode's own global home directory and project context, but edit only their own worktree. Global does not mean the entire disk. |
| Command guard | Catch ordinary commands that would leave or write outside the worktree. No mandatory external sandbox; this is workflow protection, not hostile-code containment. |
| Storage | One project-local SQLite database for structured application state. File-like artifacts and worktrees remain on disk. No per-ticket databases or parallel JSONL transcripts. |
| Commits and checkpoints | Standard process has one final ticket-branch commit at step 8. Internal checkpoints use Git commit objects under AgileCode-owned refs, outside normal ticket history. |
| Integration | Normal Git merge first; AI-assisted candidate conflict resolution when needed; verification before acceptance. User selects merge on completion or inspect before merge. |
| Autonomy and questions | User can allow autonomous tools, require approval, or prevent selected tools. Questions pause and resume the same step/session. |
| Assignment | Manual ticket creation exposes team/process selection. Otherwise use the configured default team and one of its processes. |
| Discovery and integrations | Skills and MCP are included in the first complete implementation. Strong lexical, path, and symbol discovery is core. Embedding-based semantic retrieval is deferred. |
| Cleanup | After successful integration, remove the ticket worktree and hidden checkpoint refs by default; retain the ticket branch and database history. Cleanup remains user-configurable. |

“Python core” does not forbid QML. The accepted exception is declarative presentation in QML; domain behavior, process control, tools, persistence, shell handling, and integration remain Python. QML contains presentation bindings and event handling, not business rules. There is no required JavaScript/React frontend, Node build, embedded browser editor, or internal HTTP service.

## 1.4 Engineering defaults, not unresolved product questions

The implementation may initially use CPython 3.13 on Linux, explicit SQL through the standard library, validated JSON configuration, a single writable engine owner per project, and an asyncio loop independent of Qt. These are concrete implementation starting points retained or refined from v1.0, not a new platform commitment. Actual dependency versions and supported platforms are established by tests and packaging work.

Full autonomy is a user choice, not a bypass of process sequencing or worktree guards. A confirmed directory policy is not a guarantee that arbitrary programs cannot write elsewhere. A worktree does not isolate every Git ref. A database transaction does not encompass a shell command or Git merge. Resumability does not resurrect a dead shell's volatile state. These distinctions constrain implementation without adding a security product or a distributed workflow framework.

## 1.5 Terminology

A **ticket** is persistent board work. A **run** is an execution of a frozen ticket/team/process definition. A **step** is a reusable definition; a **StepAttempt** is one execution of it. A **session** is the conversation for that attempt. A **checkpoint** is a Git-backed snapshot and a recorded recovery boundary. A **final ticket commit** is the reviewed deliverable from step 8. An **IntegrationAttempt** combines that deliverable with a particular target revision. An **approval** authorizes a specific action; a **review-before-merge decision** authorizes the displayed integration candidate.

The pinned Zoo Code reference remains commit 77e422faf56afe32e10236e4aaf129a8ea2cff53. It is historical context, not a claim about the latest upstream code. [1–4]


# 2. Product scope and capability map

## 2.1 Product shape

AgileCode is a development workbench centered on tickets and agent teams. It must provide a normal interactive Kanban board, not merely a monitor of autonomous tasks. The other principal surfaces are a ticket workspace, process timeline, agent conversation, source/diff viewer, command console, evidence view, and settings/team editor.

The headless engine must also be usable without constructing a window or importing PySide6. The desktop frontend is one adapter to that engine. An alternative UI should reuse the same commands, questions, approvals, and read models rather than reimplementing ticket execution. This requirement does not imply that a web server, daemon, remote API, or extensive CLI must be built in v1.

The user's existing source editor remains usable. AgileCode must show code professionally and support bounded manual correction, but is not required to reproduce all of an IDE: debugger integration, multi-caret editing, every language server, and a full terminal emulator are separate capabilities.

## 2.2 First complete implementation

| Capability | Scope | Required treatment |
| --- | --- | --- |
| Project identity and four-column board | Core | CRUD, ordering, filtering, dependencies, drag-and-drop, and idempotent start. |
| Global/project teams and attached processes | Core | Save/load the whole team bundle; valid references to owned members. |
| Standard eight-step process | Core | Executable Step objects, no silent omission, explicit completion gates. |
| Session and step-attempt history | Core | Fresh sessions, accumulated handoffs, questions, correction attempts, durable resume. |
| Local compatible endpoints | Core | LM Studio and llama.cpp through one Chat Completions client. |
| Streaming and native tools | Core | Text and tool calls, returned reasoning when available, honest usage/error handling. |
| Files and strong code discovery | Core | Literal/regex search, path discovery, symbol/class/function lookup, source ranges, iterative investigation. |
| Exact edits and reviewable diffs | Core | Hash preconditions, guarded writes, unified/side-by-side presentation. |
| Persistent ticket shell | Core | Persistent cwd/environment while alive, jobs, real exit status, bounded output, cancellation. |
| Workflow containment | Core | Scoped file tools and CommandGuard; no external sandbox dependency. |
| Concurrent tickets | Core | Independent worktrees and sessions; no global model generation semaphore. |
| Git-backed checkpoints | Core | Private refs and temporary index; restore without advancing ticket branch history. |
| Final ticket commit | Core | Step 8 only in the standard process, with a descriptive message and reviewed content. |
| Integration coordinator | Core | Merge queue, normal Git integration, AI-assisted conflict candidates, verification, review/auto policy. |
| SQLite and artifacts | Core | One structured project store, owned file artifacts, backup, recovery, exports. |
| Skills | Core | Global/project discovery, readable metadata, on-demand content, explicit discovery errors. |
| MCP client | Core | Configured servers, tool/resource discovery, common tool policy and results. No marketplace needed. |
| Cleanup policy | Core | Default post-merge removal of worktree/checkpoint refs; keep branch/history. |
| Read-only project discussion | Retained supporting capability | Uses the same engine; creating executable work remains a visible ticket action. |
| Image input | Optional endpoint feature | Only when the selected model/server supports it. |
| Embedding-based semantic retrieval | Deferred | Search interface leaves room for a later hybrid implementation; no mandatory embeddings/index service now. |
| Additional providers, cloud accounts, billing, telemetry backend | Excluded | Do not create speculative adapters or cloud dependencies. |
| Roo/Cline migration and VS Code extension hosting | Excluded | No legacy compatibility surface. |

Core MCP support means the application implements the client and its UI. It does not require users to configure an MCP server, and it does not make any third-party server necessary for ordinary coding. Similarly, skills are a first-class feature without making every process step load a skill or forcing irrelevant skill use.

## 2.3 What autonomy means

Autonomy controls which permitted actions need user confirmation. In full autonomy, the agent may proceed through allowed tools and process steps without ordinary approval dialogs. In restricted operation, exact named tools can require confirmation or be disabled. Command-specific rules may additionally decide Allow, Ask or Deny for individual shell commands. Explicitly denied tools remain denied; an agent cannot grant itself a capability.

Questions are different from approvals. A genuinely ambiguous ticket can pause any autonomous run for an answer. The same attempt and session resume after that answer. A failed verification or review is different again: it creates a correction attempt and re-runs the required downstream checks.

The user controls merge-on-completion independently of tool autonomy. Full tool autonomy does not silently enable automatic integration, and manual tool approvals do not automatically prohibit a previously selected auto-merge policy.

## 2.4 Avoiding new baggage

Prefer a small number of cohesive modules and ordinary objects. The number of tables is not a reason to split related project state into separate databases. The existence of concurrency is not a reason to add an inference scheduler. Process extensibility is not a reason to invent a graph language, service bus, standalone process library, or plugin marketplace.

Use established mechanisms where they fit: Git for worktrees and snapshots, SQLite for relational state, Python asynchronous I/O for sessions and tools, and Qt Quick for presentation. Add implementation boundaries for real substitution or testing needs, not for hypothetical future providers.


# 3. Functional and quality requirements

## 3.1 Functional requirements

These v1.1 requirements supersede the corresponding v1.0 assumptions. IDs are local to this edition. Section 23 maps them to observable acceptance scenarios; the scenarios are a verification plan, not claims that tests have already passed.

| ID | Required behavior | Design sections |
| --- | --- | --- |
| FR-01 | Open the external original project and verify its Git/worktree identity. | 4, 15 |
| FR-02 | Provide ticket CRUD, archive/reopen, ordering, filtering and dependencies. | 4, 6, 7, 20 |
| FR-03 | Keep exactly Backlog, Ready, In Progress and Done; show detailed status internally. | 7, 20 |
| FR-04 | Start a Ready ticket from drag/drop or an explicit idempotent command. | 4, 7, 15 |
| FR-05 | Create a dedicated nested worktree from the currently active project branch and record its exact base commit. | 15 |
| FR-06 | Freeze the ticket and complete team/process definition for each run. | 6, 8, 18 |
| FR-07 | Team owns members/processes; each process step references an owned member. | 6, 8, 21 |
| FR-08 | Save team bundles globally or project-locally; manual tickets choose a team/process, otherwise use defaults. | 4, 21 |
| FR-09 | Execute all eight standard steps in program-controlled order, with no model-selected skips. | 7, 8 |
| FR-10 | Persist completed step outputs and hand them off through ticket/workspace/prior-output context. | 6, 8, 18, 22 |
| FR-11 | Create a fresh agent session for every step attempt. | 8, 9 |
| FR-12 | Return a minimal validated StepResult with status, summary and output. | 6, 8, 9 |
| FR-13 | Persist retries as separate attempts; route failed verification/review through implementation, verification, review. | 7, 8 |
| FR-14 | Pause for questions and resume the same attempt/session with the answer. | 4, 9, 22 |
| FR-15 | Support LM Studio and llama.cpp using one local OpenAI-compatible Chat Completions client. | 10 |
| FR-16 | Use the runtime active model by default, allow member overrides, and record resolved identities per attempt. | 6, 10, 21 |
| FR-17 | Do not impose an application model-request scheduler; handle normal timeout/retry/cancellation errors. | 7, 10 |
| FR-18 | Stream text/tools and returned reasoning; preserve valid call/result ordering and honest usage state. | 9, 10 |
| FR-19 | Let users allow, require approval for, or deny tools/categories; apply policies to native and MCP tools. | 12, 19, 21 |
| FR-20 | Read own global/project context while restricting ticket source writes to its own worktree and excluding sibling runtime trees. | 13, 19 |
| FR-21 | Guard recognizable shell directory escapes, off-worktree writes and unauthorized Git operations without an external sandbox. | 14, 19 |
| FR-22 | Keep one real persistent shell per active ticket/worktree across commands and process steps. | 14 |
| FR-23 | Record jobs, cwd/environment, output, exit status and outcome certainty; cancel only owned work. | 14, 22 |
| FR-24 | Provide strong lexical, regex, filename/path and symbol discovery; do not require vector embeddings. | 13, 21 |
| FR-25 | Provide exact hash-aware edits and consistent code/diff views, including guarded manual corrections. | 13, 20 |
| FR-26 | Include global/project skill discovery, provenance, validation feedback and on-demand loading in v1. | 11, 21 |
| FR-27 | Include configured MCP clients, tools and resources through the common pipeline in v1. | 12, 19, 21 |
| FR-28 | Create hidden Git checkpoint refs without altering ticket HEAD/normal index; restore eligible files with conflict checks. | 16 |
| FR-29 | Make no standard deliverable intermediate commits; step 8 creates a descriptive final commit on the ticket branch. | 8, 15, 17 |
| FR-30 | Allow multiple concurrent tickets with independent worktrees and no mid-process base rebasing. | 7, 15 |
| FR-31 | Coordinate normal merges and AI-assisted conflict candidates, with verification and review before applying. | 17 |
| FR-32 | Provide user-controlled merge on completion versus inspection, including stale candidate checks. | 4, 7, 17 |
| FR-33 | After integration, default to removing worktree/checkpoint refs and retaining branch/database history. | 15, 16, 17 |
| FR-34 | Use one project-local SQLite store for structured state; keep file artifacts on disk. | 18 |
| FR-35 | Resume completed steps and reconcile uncertain Git/file/shell/MCP effects without blind replay. | 16–18, 22 |
| FR-36 | Expose a fully headless core through commands, read models and events independent of Qt. | 5, 24 |
| FR-37 | Use PySide6/QML as the primary UI with Widgets only where useful. | 20, 24 |
| FR-38 | Show actual member/model/prompts, tool policy, attempt history, evidence and integration lineage. | 10, 20, 22 |
| FR-39 | Provide versioned exports and coherent backups that account for artifacts and retained Git checkpoint objects. | 18, 21 |
| FR-40 | Retain read-only project discussion and explicit conversion to executable tickets. | 2, 4 |
| FR-41 | Manage context without silently losing requirements, plan, relevant findings or coherent tool exchanges. | 8, 11 |
| FR-42 | Support pause, feedback, interruption, cancellation, manual edits and UI reconstruction without cross-ticket state changes. | 4, 5, 9, 13, 22 |

## 3.2 Quality requirements

| ID | Quality | Acceptance interpretation |
| --- | --- | --- |
| NFR-01 | Headless independence | Import and run the core without PySide6, a GUI event loop or QML resources. |
| NFR-02 | Responsive presentation | No blocking model/Git/database/search work on the GUI thread; bounded/coalesced rendering. |
| NFR-03 | Durable step progress | A completed result and next-step pointer are committed together before advancing. |
| NFR-04 | Honest external-effect recovery | Unknown outcomes are reconciled or surfaced; do not claim universal exactly-once execution. |
| NFR-05 | No implicit loss of user work | Hash/OID preconditions and cleanup guards protect changed/unknown contents. |
| NFR-06 | Explicit ownership | Every event/tool/question has immutable ticket/run/attempt/session identity independent of UI focus. |
| NFR-07 | Concurrency without hidden serialization | Multiple tickets and model requests can progress; serialize only actual conflicting mutations. |
| NFR-08 | Bounded resources | Page histories/logs/searches; label truncation; avoid blocking or unbounded intermediate buffers. |
| NFR-09 | Accessible professional UI | Readable code, coherent light/dark themes, keyboard operations and tested mixed-component focus. |
| NFR-10 | Single authoritative record | No separate persistent frontend transcript or per-ticket JSONL state store. |
| NFR-11 | Configuration is data | Validate team ownership, schema and IDs; no eval/pickle/import hooks from saved definitions. |
| NFR-12 | Credential hygiene | No secrets in prompts, normal logs, team exports or uncontrolled child environments. |
| NFR-13 | Testable packaging | Normal virtual-environment source install; declare tested versions/platforms and dependencies. |
| NFR-14 | Truthful evidence | No green test without recorded evidence, no absent reasoning invented, no cleaned checkpoint advertised as restorable. |

## 3.3 Proposed measurement targets

Initial engineering targets are a warm interactive board within 3 seconds, visible acknowledgment of ordinary user input within 100 milliseconds, and board filtering below 150 milliseconds for 1,000 tickets on the designated test machine. A 1 MiB source file should open within 1 second in its supported viewing mode. These are targets, not measured claims or user-mandated hardware requirements.

Record machine, OS, dependency versions, repository size, display scale, warm/cold conditions, and percentiles with every measurement. Model latency and external build/test duration are reported separately. Coalesce streaming redraws rather than flooding Qt, while preserving complete durable messages, tool outcomes and lifecycle events.

Correctness precedes throughput. Do not meet a target by dropping questions, skipping required steps, using stale diffs, omitting source scopes, or serializing all tickets behind a hidden model semaphore. Large files, large histories, and capped searches must remain explicit states with retrieval paths for omitted data.


# 4. User workflows and interaction contracts

## 4.1 Open a project

The user selects the original project directory. AgileCode resolves the repository root and common Git directory, opens <project>/.agilecode/agilecode.db, acquires the project's engine-owner lock, and presents its board. Opening an existing managed worktree locates its owning project/ticket rather than recursively creating another AgileCode project inside it.

Initial setup creates the managed directory and adds the .agilecode/ exclusion with the user's setup authorization. It does not discard existing .gitignore changes. The original root remains distinct from every ticket execution root. Initializing AgileCode's database and Git metadata is an application administrative operation, not permission for a ticket agent to edit original source.

If recovery is needed, show affected tickets and pending operations without blocking unrelated readable project state. A missing model server does not prevent opening the board, reading history, editing a ticket, or configuring a team.

## 4.2 Create and assign a ticket

Manual creation includes team and process selectors. The process selector lists only processes owned by the selected team. The configured default team/process is preselected, but the user can change it. If a ticket is created through an otherwise authorized automatic path, assign the configured default without opening a mandatory configuration wizard.

A ticket records objective, scope, constraints, and known acceptance expectations. Step 2 is responsible for clearly stating and reviewing detailed requirements and verification methods; ticket creation must not demand that the user already perform all of step 2. Missing essential information can instead produce a question during understanding or requirements work.

A ticket's saved assignment identifies the team scope, ID, and process within that team. Starting a run snapshots the resolved team, members, steps, prompts, and ticket revision. Editing a reusable global team does not rewrite a run already in progress.

## 4.3 Start and supervise

Moving Ready to In Progress or selecting Start sends the same StartTicket command with a unique operation key and expected revision. The engine records the active original branch and exact starting commit, creates the dedicated ticket branch/worktree, opens its persistent shell, and starts step 1. Creation is recoverable; duplicate commands cannot create duplicate worktrees or runs.

Other tickets can start while this ticket is running. Selecting a card changes focus only. Each ticket detail view shows its step, member, actual resolved model, worktree, branch, current action, questions, and attempt history. Model requests are sent when needed; LM Studio or llama.cpp handles its own batching and queues.

During a tool approval, show the owning ticket and exact requested action. A denied tool produces a recorded result, not a fake success. During a question, keep the step paused and the question visible even if the user navigates elsewhere. Answering resolves the same pending question and resumes the same session.

## 4.4 Correction and review

A verification failure returns the process to implementation with the original plan, current worktree, requirement set, and failure report. The correction is a new attempt with a fresh session. After it passes, verification runs again. Review findings follow the same path: implementation, verification, review. An earlier green result cannot authorize changed code.

The UI distinguishes technical failure, completed verification with FAIL, completed review with CHANGES_REQUIRED, and waiting for the user. They are not interchangeable. Previous outputs and evidence remain inspectable; the next attempt sees the latest applicable outputs rather than a misleading combination of obsolete approvals.

## 4.5 Final commit and integration choice

After steps 1–7 complete successfully, step 8 creates the final commit on the ticket branch with a message describing the work. This completes the development process. It does not give the agent direct access to the original checkout or create a ninth implicit development step.

With Merge on completion enabled, the MergeCoordinator prepares and verifies an integration candidate and applies it when its guards pass. With Inspect before merge enabled, the ticket remains In Progress with an Awaiting merge review badge. The user sees the final ticket commit, target revision, combined diff, validation results, and any AI conflict-resolution changes before deciding.

For standard code-change tickets, Done means successful integration under the selected policy, not simply that the model or step 8 finished. Process completion and integration completion remain separate facts. A pending merge, failed merge, or pending inspection does not add a board column or falsely mark the ticket Done.

## 4.6 Cleanup and reopen

Successful integration triggers the configured cleanup policy. By default the engine removes the ticket worktree and hidden checkpoint refs, keeps the ticket branch, and retains all ticket/process history in SQLite. A cleanup error is a recoverable badge on a completed ticket; it does not undo a verified merge or erase history.

Before removal, verify that no owned shell/job or unsaved UI edit remains, no unreviewed source changes would be lost, and retained artifact references do not point into a directory being deleted. Tickets not successfully integrated retain their branch, worktree, and checkpoints. Reopening creates a new run from an explicitly observed current project base; the old run remains historical.

## 4.7 Restart and headless use

Completed step results are immediately durable. After a crash following step 4, the process can resume with implementation using the stored understanding, requirements, architecture, and plan. The interrupted in-flight step is reconciled before it resumes or receives a new attempt. Uncertain shell or remote effects are never blindly replayed.

A headless caller can perform the same start, pause, answer, approve, inspect, and merge operations using application commands and read models. Detaching/recreating a UI is not a process transition. Terminating the owning application process does stop its engine unless it is deliberately hosted separately; headless architecture alone is not a background-service implementation.


# 5. Headless architecture and execution model

## 5.1 Dependency direction

The functional application is a headless Python package. Qt and QML are presentation adapters. Domain and application modules MUST import and run without PySide6, QML resources, a GUI event loop, or a visible window.

```text
PySide6 bridge + QML/Qt Quick views
             |
             | commands, immutable read models, events
             v
Headless Python application services
             |
             +-- Domain: Ticket, Team, Process, Step, StepResult
             +-- Execution: ProcessRunner, AgentRunner, run controls
             +-- Adapters: SQLite, compatible HTTP, Git, shell, MCP
```

This is one modular application, not a distributed system. The desktop build may run GUI and engine in one process with separate threads. A headless entry point uses the same composition root without creating Qt objects. Future frontends may add transport adapters, but the baseline does not introduce an internal HTTP server, broker, Rust sidecar, or microservice boundary.

## 5.2 Principal objects and responsibilities

| Object | Owns | Does not own |
| --- | --- | --- |
| Application | Dependency wiring and startup/shutdown. | Ticket decision-making or widget state. |
| ProjectService | Original-root identity, project store, global/project definition resolution. | UI-selected current ticket as tool authority. |
| TicketService | Board transitions, revisions, assignments, ordering, dependencies. | Model streams or raw Git commands. |
| RunCoordinator | Ticket start, run controls, persistent workspace, recovery coordination. | The role's reasoning or frontend presentation. |
| Team | Members, attached processes, default process. | A mandatory model choice for every member. |
| Process | Ordered Step definitions and declared backward correction rules. | An arbitrary executable graph language. |
| Step | Name, owned-team member reference, step prompt, run(context). | Database connection, GUI, or mutable global session. |
| ProcessRunner | Sequence, gates, attempt persistence, context handoff, correction routing. | Inference scheduling or free-form next-step selection by a model. |
| AgentRunner | One session's request/tool loop. | Board completion or branch integration. |
| ToolManager | Model-facing tool schemas, correlated call/result lifecycle and dispatch to Tool objects. | Tool-specific behavior or a separate provider-specific path. |
| OpenAICompatibleClient | Request encoding and response normalization. | Model loading, GPU allocation, generation concurrency limits. |
| PersistentShell / CommandGuard | Ticket shell lifetime / pre-dispatch workflow checks. | An operating-system sandbox guarantee. |
| GitRepository / CheckpointService | Typed Git operations / private-ref snapshots. | Free-form model authority over shared refs. |
| MergeCoordinator | Per-target integration queue, candidates, AI resolution, verification, application. | The standard process's eight-step sequence. |
| ProjectStore | Transactions and read models for one project. | Atomic transactions across Git and the OS. |
| SearchService / SkillRegistry / McpService | Scoped discovery and external tool access. | Uncontrolled directory access or automatic installations. |
| QtBridge | Translation between core messages and Qt models/signals. | SQL writes, direct tools, process advancement, or approval decisions. |

Service boundaries group related behavior; they do not require a file or class for every database table. Small immutable dataclasses are appropriate for records. Use Protocols at actual adapter/test boundaries, not an inheritance hierarchy for every possible model provider.

## 5.3 Thread and lifetime ownership

The GUI thread owns QObjects exposed to QML and every QWidget/scene object. The headless engine runs its own asyncio loop in an engine thread in the desktop host. Network streams, shell communication, ticket orchestration, and MCP lifetimes belong to that loop. A dedicated database worker owns the SQLite connection, and bounded workers handle blocking parsing or filesystem work.

The bridge submits commands thread-safely and applies returned read models/events on the GUI thread. Qt signals are confined to the adapter. The core event sink uses ordinary Python messages, not QObject, Signal, or Qt-specific tasks. Standard Python cancellation and coroutine facilities underpin the engine. [12]

There is no global model-request permit and no per-endpoint ModelScheduler. Independent ticket sessions can be waiting on simultaneous requests. Locks exist for actual shared mutations: a single writer in a ticket worktree, brief Git administration, and serialized integration to a target branch. These locks must not be held during unrelated model inference or user waits.

## 5.4 Application API

Representative commands are CreateTicket, MoveTicket, StartTicket, PauseRun, CancelRun, ResumeRun, SubmitFeedback, AnswerQuestion, ResolveApproval, SaveTeam, SelectActiveModel, RestoreCheckpoint, PrepareIntegration, ApproveIntegration, and RetryCleanup. An operation ID identifies duplicate submissions; expected revisions reject stale actions.

Read models include BoardSnapshot, TicketSnapshot, ProcessProgress, StepAttemptDetail, ConversationPage, FileSnapshot, DiffDocument, QuestionSnapshot, ApprovalSnapshot, and IntegrationPreview. They are plain immutable data suitable for either a GUI or a headless consumer.

Events include TicketChanged, StepAttemptStarted, StepAttemptCompleted, QuestionRequested, ModelTextDelta, ModelReasoningDelta, ToolStateChanged, ShellOutput, IntegrationStateChanged, CleanupStateChanged, and RecoveryRequired. Critical state events follow durable storage. Streaming deltas may be coalesced for presentation but cannot create an executable action.

Every event carries its project, ticket/run/attempt/session identifiers where applicable, a sequence, timestamp, and correlation ID. The runtime assigns these identifiers. Changing UI focus or receiving model-supplied IDs never changes event ownership.

## 5.5 Replaceability and authority tests

A fake UI must be able to drive a complete ticket, including a question, approval, failed verification, correction, commit, inspection, and merge through the same API. A fake model client supplies deterministic responses for that test. Removing the desktop package must not break imports or the headless test suite.

The UI can be disposed and reconstructed from read models without losing attempt identity or terminal jobs while the engine host remains alive. If the user closes the entire desktop application, the host executes an explicit shutdown policy; it does not assume the core will survive process exit.

Application services are the only authority for domain mutations. A QML drop handler sends MoveTicket; it does not set a database status. A code editor sends a guarded save command; it does not write files itself. A merge button sends a candidate-specific approval; it does not run git merge from a widget.


# 6. Object model, identities, and invariants

## 6.1 Ownership model

```text
Team
  +-- members: TeamMember[]
  +-- processes: Process[]
        +-- steps: Step[]
              +-- team_member_id -> member of this Team

Ticket -> selected Team + one of its Processes
  +-- TicketRun -> frozen definition snapshot
        +-- StepAttempt[]
              +-- AgentSession -> Messages, ModelRequests, ToolExecutions
        +-- ticket Worktree + PersistentShell
        +-- Checkpoints
  +-- IntegrationAttempt[]
```

A Process is a separate class but not an independent reusable library entry detached from a team. Saving, copying, exporting, or importing a team carries its members and all attached processes. Process/member IDs are stable within the team. A process cannot refer to a member of an unrelated team.

## 6.2 Principal records

| Record | Important data | Required rule |
| --- | --- | --- |
| Project | UUID, original root, common Git directory, storage root. | Verify identity before source or ref mutation. |
| Ticket | UUID, visible key, board state, revision, selected team/process, parent/dependencies. | One nonterminal development run per ticket; multiple tickets can run. |
| TicketRun | Frozen ticket/team/process, workspace identity, current step, run state. | Later definition edits do not alter history or an active process silently. |
| Team | ID, name, members, processes, default process ID, scope/version. | Default and every step reference resolve inside the same bundle. |
| TeamMember | ID, name, system prompt, allowed tools, skills, optional model override. | No required fixed model binding. |
| Process | ID, name, ordered steps, limited declared correction rules. | Standard development uses exactly the agreed eight steps in order. |
| Step | ID, name, member ID, prompt; run(StepContext) -> StepResult. | Definition reusable; execution state belongs to StepAttempt. |
| StepContext | Ticket snapshot, worktree scope, relevant completed outputs, previous output, correction report. | Handoff is explicit and does not require the previous session's full transcript. |
| StepResult | status, summary, output. | Small, validated result; never prose-parsed success. |
| StepAttempt | Run/step ID, attempt number, member snapshot, resolved model, session ID, status, result, timestamps. | Append a new attempt for a retry; preserve older outputs. |
| AgentSession | Attempt ID, conversation, execution generation, pending question/call. | One fresh session per attempt; a question resumes that same session. |
| WorktreeRecord | Ticket ID, canonical path, branch ref, base branch/OID, lifecycle status. | Ticket tools cannot substitute another worktree. |
| Checkpoint | Ticket/run/attempt, private ref, commit/tree OIDs, recovery boundary, fingerprint. | Not an ancestor on the deliverable ticket branch by default. |
| IntegrationAttempt | Ticket final OID, target ref/OID, candidate OID, policy, tests, resolution reports, state. | Approval and tests refer to this candidate against this target. |
| ToolExecution / Job | Scope, call/job identity, input, status, output/evidence, certainty. | One recorded outcome; unknown external effects are reconciled. |

UUIDs are internal identifiers. Visible keys such as AC-0042 are allocated transactionally and never reused. Use full Git object IDs as opaque strings, not an assumed fixed digest length in database constraints.

## 6.3 Definitions versus execution

The reusable Step contains instructions and an assigned member. It must not carry mutable counters, the last conversation, or the most recent output: the same global team can be used by concurrent tickets. Those values live in StepAttempt and TicketRun.

The public concept is Step.run(context). Internally, a bound Step receives an execution adapter when constructed for a run. That adapter creates the attempt session and performs agent interaction; the ProcessRunner surrounds it with persistence, gate checking, and sequencing. This preserves the simple object model without letting a definition depend on a GUI or raw database.

StepContext exposes the immediately previous output for ordinary pipeline handoff and a keyed collection of applicable earlier outputs. Requirements from step 2 remain available to verification at step 6; the plan from step 4 remains available for implementation retries. Long outputs can be retrieved by reference, but required constraints cannot silently disappear from the effective context.

## 6.4 Revisions and model resolution

Freeze the ticket specification and complete team/process definition at run start. A team edit applies to future runs unless the user deliberately restarts or revises a run. A scope amendment invalidates affected downstream outputs and is a recorded user action, not an agent deciding to expand the ticket.

Model selection is the deliberate exception to mandatory definition freezing: a member with no override uses the active runtime model when its next step attempt starts. That actual endpoint, requested model ID, response-reported model ID when available, and relevant options are recorded on the attempt/request. Changing the active model does not mutate an in-flight request or silently move an existing session to a different model.

## 6.5 Invariants

The system MUST enforce: four board states only; one active run per ticket; independent concurrent tickets; one writable workspace per ticket; no team process with foreign/missing members; no standard step skipped; one completed result per attempt; no attempt rewritten as if a retry never occurred; no standard deliverable commit before current verification and review pass; no standard deliverable branch commits before step 8; no approval reused for a different candidate; and no cleanup of unintegrated work by the automatic policy.

A step's PASS is necessary but not sufficient for progression when objective evidence is required. An agent cannot declare a failing recorded command successful. Verification/review results are tied to a workspace fingerprint; a later code change makes them stale. Programmatic sequencing guarantees that steps are performed, not that model reasoning is infallible.

## 6.6 Tickets versus delegated sessions

Ticket parentage organizes work on the board. Optional helper-session delegation is internal to a ticket run. A helper shares the ticket worktree and inherits its policy; it does not receive a new board ticket or a new root identity. The default eight-step process needs no delegation to operate.

Dependencies are a separate graph with cycle checks. Creating a helper is never a way to escape step gates, reset correction history, select a different worktree, or acquire blocked tools. Within one worktree, helper writes remain serialized; parallelism across isolated tickets is the primary supported concurrency model.


# 7. Board, process, attempt, and integration lifecycle

## 7.1 Board transitions

| Transition | Guard and effect |
| --- | --- |
| Backlog -> Ready | Save a usable ticket specification and resolve its team/process assignment. No execution yet. |
| Ready -> In Progress | Idempotent start accepted; record/provision the ticket's worktree and run. Other active tickets do not prevent admission. |
| In Progress -> Ready | Explicit cancel/requeue after owned actions settle; preserve attempts and files. |
| In Progress -> Done | Standard process committed successfully and integration succeeded under the selected merge policy. |
| Done -> Ready | Explicit reopen; keep historical run/integration facts and create a new execution when started. |
| Same-column reorder | Update position/revision only; no execution side effects. |

Dragging a card cannot bypass required process or integration gates. Backlog-to-In Progress first resolves readiness. Dragging a running ticket backwards requests cancellation; it does not leave a hidden running shell behind a Ready card. Dragging to Done cannot manufacture passed verification or a merge.

Internal labels include Starting, Understanding, Requirements, Architecture, Planning, Implementing, Verifying, Reviewing, Committing, Awaiting merge review, Integrating, Paused, Waiting for answer, and Recovery required. They remain badges or ticket-detail status, not extra columns.

## 7.2 Process lifecycle

A run has an execution state such as starting, running, waiting_user, paused, cancelling, interrupted, failed, cancelled, process_complete, or succeeded. The current step and its attempt are separate fields. process_complete is reached when step 8's final commit has been observed and stored; it does not by itself mark the board Done.

The ProcessRunner advances through ordered steps on an accepted PASS. FAIL from verification and CHANGES_REQUIRED from code review invoke the declared correction path. Technical failures stop or pause the affected run with a cause. A model does not return an arbitrary next-step number; routing belongs to the process definition and runner.

Persist the accepted result, gate evidence references, and next-step pointer together. The next step must not start before that transaction succeeds. This makes a crash between steps resumable without rerunning already completed work.

## 7.3 Attempt and session lifecycle

A StepAttempt is planned, running, waiting_user, paused, completed, failed, cancelled, or interrupted. A completed attempt can contain StepResult.status FAIL or CHANGES_REQUIRED: the agent successfully performed the check and found a defect. A failed attempt instead means the step could not complete because of an execution/protocol problem.

PASS, FAIL, and CHANGES_REQUIRED are the minimal StepResult outcome values. Question/pause/cancel are runtime controls, not extra prose statuses stuffed into the result. If additional outcomes are needed later, add them deliberately with validation and routing rather than accepting arbitrary strings.

Every new attempt creates a fresh session. A question suspends the existing attempt/session with one pending question record. The answer is persisted and delivered to that same conversation. It does not increment the attempt count, clear prior tool results, or restart the process.

## 7.4 Corrections and stale evidence

```text
Preparation (in order):
1 Understand -> 2 Requirements -> 3 Architecture -> 4 Plan

Delivery (after preparation):
5 Implement -> 6 Verify -> 7 Review -> 8 Final commit
      ^           | FAIL      | CHANGES_REQUIRED
      +-----------+-----------+
          new implementation attempt;
          then new verification and review attempts
```

On correction, keep requirements, architecture, and plan unless the finding demonstrates they are invalid. Pass the original plan and the failure/review report explicitly. If a change requires scope or architectural replanning, pause or perform an explicit recorded restart from the affected earlier step; do not quietly rewrite the plan or skip revalidation.

Whenever code changes after verification or review, invalidate downstream evidence for that code snapshot. Even a change described as cosmetic must pass the configured verification and review policy again. A configurable repetition limit may pause a non-progressing loop, but the threshold is a user setting/engineering safeguard, not an additional required step.

## 7.5 Concurrency and mutation ownership

Multiple tickets can execute their processes at once. Each has its own worktree, persistent shell, sessions, questions, and policy snapshot. An asyncio task may own each active process; none holds a project-wide ticket slot while awaiting inference or approval.

AgileCode does not gate generation through a ModelScheduler, impose one request per endpoint, or negotiate GPU capacity. The provider handles batching, queuing, overload, and parallel inference. Ordinary timeout/error/retry behavior remains necessary; connection-pool defaults must not accidentally recreate a one-request scheduler.

Within one ticket, the standard process is sequential and file/command writes are serialized. Git administrative operations that share metadata use short coordination. Integration into a target branch is serialized by MergeCoordinator. A SQLite worker serializes brief database transactions without serializing whole tickets.

## 7.6 Integration and cleanup states

Integration has its own record: not_requested, preparing, resolving_conflicts, verifying, awaiting_review, applying, integrated, blocked, failed, or interrupted. The record binds the ticket final commit and exact target revision. A change to the target invalidates a stale candidate, not the completed historical ticket process.

The project setting selects auto or inspect; a per-ticket override is allowed. Successful integration moves the run from process_complete to succeeded and the standard ticket to Done. process_complete remains a nonterminal run state while integration is pending, preventing a second run from taking ownership of the same ticket. A manual inspection wait or unresolved conflict keeps it In Progress. Cleanup is pending, running, complete, retained_by_policy, or failed independently of the integration state.

Cancellation affects only the owning run's requests/jobs; it never stops another ticket. No database transaction or worktree write lock may span a user wait. A project-owner lock identifies the engine, not permission to monopolize every ticket action.


# 8. Team-owned processes and the standard eight steps

## 8.1 Definitions and ownership

A Team is the saved unit. It contains TeamMembers, one or more Processes, and a default process reference. Each Process contains an ordered list of Step objects. A Step identifies a member of its owning team, holds a step-specific prompt, and runs against a StepContext to return a StepResult.

A member contributes its base role instructions, permitted tools, available skills, and optional model override. A step contributes the task to perform now. The effective instructions combine the member's role, the step's prompt, the ticket specification, the current workspace, and relevant earlier outputs. A member can perform several steps without those steps sharing one long conversation.

The built-in standard team/process is always available. A user can save a custom team globally or locally, including custom processes, without editing Python code. Built-in definitions can be cloned rather than mutated in place. A process is never independently saved into a global process library with unresolved role placeholders.

## 8.2 Standard process contract

The standard process contains exactly the following eight development steps. Start/worktree provisioning occurs before step 1; integration and cleanup occur after step 8. They are engine operations, not extra development steps disguised as required user workflow.

| # | Step | Typical assigned member | Required output and completion condition |
| --- | --- | --- | --- |
| 1 | Read and understand the ticket | Analyst | Accurate understanding of objective, scope, constraints, relevant code/context, and uncertainties. Ask when essential intent is missing. |
| 2 | Review/create requirements and verification criteria | Analyst | Clear, stable requirement IDs with a method to verify each one. Preserve exclusions and distinguish requirements from suggestions. |
| 3 | Architect necessary code changes | Architect | Identify affected components/files, interfaces and dependencies; explain the minimal structural changes needed. |
| 4 | Create detailed implementation plan | Architect | Ordered, actionable implementation steps tied to requirements and affected code; include testing work and dependencies. |
| 5 | Implement by following the plan | Programmer | Apply planned changes in the ticket worktree; report completed plan items, deviations, and unresolved work. |
| 6 | Verification and validation | Tester | Evaluate the actual result against step 2. Record methods, commands, outcomes and evidence. Return PASS or FAIL. |
| 7 | Code review | Reviewer | Independent inspection of scope, correctness, regressions, maintainability and verification coverage. Return PASS or CHANGES_REQUIRED. |
| 8 | Final commit | Committer or Programmer | Describe the completed work in a commit message and create/verify the final commit on the ticket branch. |

These member names are illustrative roles, not eight required subclasses or eight mandatory distinct models. The same member may be assigned to several steps. The sequence itself is not optional: a model cannot decide that a small ticket should skip requirements, architecture, planning, verification, or review.

## 8.3 Step.run and ProcessRunner

The conceptual interface is deliberately small:

```python
class Step:
    name: str
    team_member_id: str
    prompt: str

    async def run(self, context: StepContext) -> StepResult:
        ...

class Process:
    steps: list[Step]
```

A bound Step.run delegates agent execution to the headless session service. ProcessRunner owns the outer orchestration: resolve the member, create the StepAttempt, resolve its model, build context, invoke the step, validate the result and gate, persist completion, and advance or route backward. Runtime execution state does not live on a globally shared Step instance.

StepResult remains status, summary, and output. Evidence, worktree fingerprints, timestamps, actual model, and tool records are attached by the runner/store as attempt metadata. This avoids a giant model-generated schema while preserving objective validation data.

## 8.4 Default prompts

The following are the baseline prompts supplied programmatically. They are precise step instructions, not a skill that an agent can choose to ignore. Project rules and member-role instructions are composed separately.

**Step 1 — Understand.** Read the ticket and relevant project context. State the requested outcome, included and excluded scope, known constraints, and the relevant components or files. Identify uncertainties that would change implementation or acceptance. Use scoped reading/search tools as necessary; do not implement changes. Ask the user when essential intent cannot be established from the ticket or existing project. Return a concise but complete understanding that subsequent steps can rely on.

**Step 2 — Requirements.** Review the original ticket and the understanding output. Create or refine a clear list of requirements without expanding scope. Give every requirement a stable identifier and a concrete verification method, including expected observable behavior, regression coverage, and inspection where execution is not appropriate. Distinguish mandatory requirements from suggestions. Resolve contradictory expectations through a question. The output must be usable by the verifier as the acceptance checklist, not merely as general advice.

**Step 3 — Architecture.** Determine the code changes needed to satisfy the recorded requirements. Inspect the relevant code. Identify affected components, files, interfaces, data flows, dependencies, and any migration or compatibility consequence actually required by the ticket. Prefer changes proportional to the work. Explain the intended structure and why it meets the requirements. Do not begin implementation or introduce unrelated abstractions. Flag a missing requirement or necessary scope change instead of assuming approval.

**Step 4 — Plan.** Turn the requirements and architecture into a detailed ordered implementation plan. Identify concrete work items, affected files or components, dependencies between items, tests to add or adjust, and how completion will be checked. Reference requirement IDs. Include enough detail for a programmer to execute the plan without repeating architectural discovery, while leaving implementation details that do not affect the design to the programmer. Do not edit production code in this step.

**Step 5 — Implement.** Implement the approved scope by following the plan in order inside the ticket worktree. Read current files before changing them and use the exact-edit/command tools under the effective policy. Update progress and explain necessary deviations. Do not create a deliverable Git commit. On a correction attempt, use the current code plus the supplied verification/review findings; do not discard existing work or repeat the original implementation blindly. Report the changes, plan coverage, and any incomplete requirement.

**Step 6 — Verify and validate.** Evaluate the current implementation against every requirement and its specified verification method. Run appropriate commands, inspect actual outputs, and attach the recorded evidence. Distinguish passed, failed, not run, and not applicable checks. Do not claim a test ran without a corresponding tool/job result. Do not modify production code to make a check pass in this step. Return PASS only when required checks are satisfied; otherwise return FAIL with concrete findings for implementation.

**Step 7 — Review.** Independently review the complete ticket diff, requirements, plan, and current verification evidence. Check correctness, regressions, unnecessary scope, maintainability, error handling, and whether the tests genuinely cover the requirements. Do not merely repeat the implementer's summary. Return PASS when the change is acceptable or CHANGES_REQUIRED with actionable findings. Do not silently repair code during review; changes must return through implementation and fresh verification.

**Step 8 — Commit.** Review the passed verification/review results and current change summary. Prepare a clear commit message describing the completed work and relevant verification. Request the engine-owned final-commit operation for this ticket branch/worktree. Do not merge, push, switch branches, or commit unrelated files. Return PASS only after the operation reports a verified commit whose content matches the reviewed candidate. Include the commit identifier and summary in the step output.

## 8.5 Handoffs and correction attempts

Each completed output is stored immediately in the project database. StepContext carries the immutable ticket, worktree identity, last completed output, relevant earlier outputs indexed by step ID, and any correction report. The normal handoff does not copy every previous model/tool message into a giant conversation.

A verification failure starts a new implementation attempt, followed by new verification and review attempts. A review rejection follows the same route. Store each attempt separately with the exact prompt/context snapshot, model, dates, result, and assessed workspace fingerprint. Attempts are not overwritten, and failed checks are not erased when a later one passes.

A question pauses the same session; it is not a correction attempt. A crash after a completed step resumes at the first incomplete step once side effects and workspace identity are reconciled. A crash during a step may resume its stored session or require an explicitly identified replacement attempt, depending on what actually executed.

## 8.6 Custom processes without a workflow framework

A custom team may contain several custom processes. Each still consists of ordered steps with owned-member references and explicit prompts. Users can create a different sequence intentionally; the engine executes that selected sequence rather than treating its steps as suggestions.

The standard process's fixed order and correction mapping are defined in code. Custom processes can declare a limited backward correction target and the point from which downstream gates must rerun. Validate unique IDs, member references, nonempty prompts, legal ordering, and correction bounds. Do not add arbitrary code hooks, Python imports, model-selected transitions, or a general graph-expression language.

Deleting or renaming a member must preserve stable IDs or require reassignment of all affected steps before the team can be saved. Copying a team copies/remaps all its internal references together. A reusable process is coherent because it travels with the team whose members it names.


# 9. Agent execution and conversation protocol

## 9.1 Session creation per step attempt

For each attempt, resolve the assigned TeamMember from the run's frozen team, choose its optional model override or the active runtime model, and create a fresh AgentSession. Persist that identity and the resolved settings before sending a request. The session receives the step prompt and StepContext, not a shared conversation inherited from earlier steps.

The ticket worktree and persistent shell survive ordinary step transitions. A fresh session therefore means fresh model history, not a fresh directory or a new shell. The session can inspect current files and prior step artifacts without inheriting unrelated tool chatter.

An attempt can contain many model/tool turns. Completion occurs through a validated finish_step result and the step's gate. A single model answer is not automatically a completed step, and a successful tool does not automatically imply the step's goal is satisfied.

## 9.2 Iterative request/tool loop

The runner is an explicit asynchronous loop. It checks controls, composes bounded context, sends a compatible request, collects and validates its terminal response, stores the assistant message, executes complete permitted tool calls, stores correlated results, and continues. It does not use a recursive task stack or a UI provider object.

```python
async def run_session(session):
    while session.is_runnable:
        await controls.apply_pending(session)
        await recovery.require_known_effects(session)
        request = await context.prepare(session)
        turn = await client.collect_stream(request, events)
        await store.finalize_turn(turn)
        await controls.check_generation(session, turn.generation)

        if not turn.is_complete:
            await controls.interrupt(session, turn.problem)
            return
        if not turn.tool_calls:
            await controls.handle_text_response(session, turn)
            continue

        await tools.validate_batch(turn.tool_calls)
        for call in turn.tool_calls:
            await controls.require_runnable(session)
            outcome = await tools.dispatch(call, session.scope)
            if outcome.waits_for_user:
                await controls.persist_wait(session, call, outcome)
                return
            await store.append_tool_result(call, outcome.result)
            if outcome.proposes_step_result:
                return outcome.step_result
```

This is ordering pseudocode, not a complete implementation. In particular, a proposed result still passes ProcessRunner's gate, and error paths must finalize/reconcile tool outcomes. There is intentionally no scheduler.model_slot around the client call.

## 9.3 Streaming and execution boundaries

Partial text, reasoning, and tool arguments may be shown in the UI. Side effects start only after complete arguments have been assembled, decoded, schema-validated, authorized, and accepted as part of a complete response. A partial path displayed twice is not proof it is complete.

The canonical assistant message must be durable before side-effecting tools start. A terminal call result is then persisted exactly once per request/call identity. A broken stream never authorizes a half-formed edit or command. Sequential dispatch within a session preserves ordinary shell/file dependencies without preventing different tickets from running simultaneously.

## 9.4 Step completion

finish_step accepts the small StepResult schema. The status is an enum, the summary is a short report, and output is the actual handoff artifact, such as requirements or the implementation plan. The model cannot set the ticket state, current process index, worktree path, or another member's approval.

For steps 6 and 7, the gate checks recorded evidence and the assessed code fingerprint. For step 8, it checks the engine-owned final commit result. For earlier analysis steps, it checks that the required artifact is present and concerns the current ticket. These gates catch missing steps and unsupported claims; they do not purport to prove every written requirement or architectural argument mathematically correct.

If a development session produces ordinary prose without finishing, the application can request a bounded clarification of completion or display an actionable pause. It must not loop indefinitely demanding tools merely because tools exist. Read-only discussion sessions may finish a response normally without a process result.

## 9.5 Questions and approvals

ask_user creates a question with its originating attempt/session, prompt, timestamp, and optional answer choices. The session enters waiting_user and retains its conversation and pending tool call. The user's answer is durably recorded, resolves that same call, and resumes the same session. Changing the selected ticket does not reroute the answer.

Approval is a separate record bound to normalized arguments, relevant content/target hashes, generation, and policy revision. A tool may wait for approval without completing its attempt. A policy change can affect a future invocation after revalidation; it does not retroactively change a denial or approve a different command.

A suspending or finishing control call must not share a batch with unrelated side-effecting calls whose ordering would become ambiguous. Reject an invalid mixed batch before any of its actions execute and record coherent errors for the calls.

## 9.6 Message integrity, feedback, and cancellation

Provider call IDs are scoped by application request/session IDs; they are not assumed globally unique. A complete call receives one terminal result, including denial or error. Unknown external effects remain unresolved until reconciliation; missing database output alone never authorizes replay.

Each session has a generation. Interrupting increments it, cancels the active response where possible, and prevents stale chunks/approvals from launching work. Unstarted calls receive cancellation outcomes. Feedback is a user-authored message queued at an explicit boundary, not silently rewritten as model instructions.

Compaction preserves complete assistant-call/tool-result exchanges. An explicit checkpoint/history restore branches the effective conversation without deleting the historical record. Optional helper delegation uses the same ownership and correlated-result rules and does not bypass the surrounding step's completion gate.


# 10. Local OpenAI-compatible model integration

## 10.1 Deliberately narrow scope

Implement one OpenAICompatibleClient around Chat Completions. The initial practical targets are LM Studio and llama.cpp serving local models. A configurable API base URL, active model identifier, optional credential, and request options are sufficient as the primary user-facing configuration. Do not build native Anthropic/Gemini adapters, a provider registry, a capability-negotiation framework, or a mandatory compatibility matrix.

LM Studio documents compatible /v1/chat/completions and /v1/models endpoints. llama.cpp's server documents compatible chat completion and tool-call behavior, with model/template configuration relevant to tool use. These sources establish the transport choice, not a guarantee that every loaded model performs agentic coding equally well. [10, 31]

The application does not load/download models, manage VRAM, or start an inference service automatically. The user runs the server and selects/loads the model there. AgileCode connects to that service. OpenAI Responses API support is not required for v1.

## 10.2 Active model and optional overrides

The default member setting is Use active model. Team definitions therefore remain valid when a model is replaced. Changing the runtime selection once affects subsequent step attempts across teams without editing every member or process.

If the endpoint exposes one usable current model, AgileCode can select it automatically. If several are exposed, use the current explicit selection rather than guessing. /models discovery is a convenience, not a universal claim that every listed model is loaded. A manual model field remains available. The client sends the identifier expected by the selected compatible endpoint.

A member may override the default with a saved endpoint/model profile. Resolve the choice at step-attempt creation and retain it for that session unless the user deliberately restarts/reconfigures it at a safe boundary. Persist the requested identifier and any response-reported model identity separately. Do not pretend an alias proves a particular quantization or weights version.

## 10.3 Minimal settings and transport

| Setting | Treatment |
| --- | --- |
| base_url | Preserve the complete configured prefix, commonly ending in /v1. |
| active_model | Runtime choice or single-model discovery; not a required team-member field. |
| credential reference | Optional; actual secrets kept out of project exports/prompts. |
| context/output limits | User/server configuration; estimates are labeled rather than guessed from a model name. |
| request_options | Small optional map for supported generation parameters. Do not send every possible feature flag by default. |
| timeouts/retries | Ordinary client behavior with bounded retries and visible failures. Not a concurrency scheduler. |
| reasoning handling | Preserve returned fields where supported; absence is allowed and visible. |

Use one reusable asynchronous HTTP transport adapter with connection pooling and explicit lifetime. HTTPX is the proposed transport, not a second model provider framework. It supports asynchronous streaming; responses and clients must be closed deterministically. [11]

HTTP to a user-selected loopback server is expected. Remote origins remain explicit configuration, with TLS verification by default. No telemetry, discovery of unrelated remote services, or silent fallback from a local model to a hosted model is introduced.

## 10.4 Request and tool-call construction

Encode the canonical conversation as compatible messages, tools, and model settings. The member/step/ticket instructions are assembled by PromptBuilder, not hardcoded provider-specific prompts. Send only exposed tools and parameters needed by the current session.

The engine must support native function calls that arrive as one complete chunk as well as fragmented arguments. Optional flags such as parallel_tool_calls or streamed usage are not a precondition for connecting. An endpoint that returns valid calls in fewer chunks is acceptable; one that cannot return usable calls cannot perform tool-using steps and should produce an actionable error, not silent prose execution.

OpenAI's function-calling documentation describes accumulation of streamed calls by index and reconstruction of arguments. AgileCode applies its own complete-call validation before execution. [9]

## 10.5 Normalized stream events

Normalize responses into TextDelta, ReasoningDelta, ToolCallDelta, UsageUpdate, and ResponseFinished/ResponseError. Assemble each response locally; parsing state must never be shared between concurrent tickets. Retain request ID, response ID when supplied, call index/ID/name, argument fragments, and finish reason.

An event is presentation data until the response and call are complete. Validate JSON, required fields, result type, and argument sizes. Invalid call output is recorded and returned as a bounded corrective error; do not execute XML-looking text or arbitrary JSON found in normal prose.

A finish reason indicating truncation or an interrupted stream is not success. Partial text can remain visible as partial. Incomplete calls do not enter the executable next-turn history. If usage arrives separately, merge it into the request's usage record without creating another conversation message.

## 10.6 Reasoning, usage, and ordinary error handling

Display reasoning only when the server returns it. Preserve supported reasoning metadata separately from normal assistant text and retain any documented round-trip fields required by that endpoint. Do not fabricate a trace, interpret all tags globally, or promise a universal request switch that enables reasoning on every server.

Usage can be reported, estimated, or unknown. Missing values remain null, not zero. Configured costs remain unavailable when the required usage/pricing basis is missing. Request diagnostics include redacted settings and payload shape so failures can be investigated without revealing credentials.

Connection failures and temporary rejection may receive bounded retries before usable output is accepted. Respect an applicable Retry-After response. Authentication or malformed requests do not trigger endless retries. A stream broken after partial output requires an identified new request or a pause; it is not replayed as though nothing happened.

Multiple ticket requests are submitted as needed. No max_concurrent_requests setting or ModelScheduler is introduced. Server queuing, batching, and capacity are provider responsibilities. Transport pooling, timeout behavior, and cancellation still must work with more than one simultaneous request.


# 11. Prompts, context, skills, and reusable instructions

## 11.1 Prompt composition

PromptBuilder deterministically assembles the resolved TeamMember role/system prompt, the current Step.prompt, original ticket constraints, trusted project rules, worktree/shell identity, and the relevant StepContext outputs. Tool schemas are sent separately. It performs no configuration writes, model loading, or skill installation.

The standard process's prompt is supplied programmatically at each step. It is not a single skill telling the agent to remember eight stages. The step's assigned member and completion gate are fixed by ProcessRunner, not selected from whatever UI mode happens to be active.

Store the exact assembled prompt/context snapshot or a lossless reference plus hashes per attempt/request. A user can inspect which text came from the member, step, ticket, project rules, and loaded skills. A fresh step session receives durable artifacts rather than a giant concatenation of all earlier transcripts.

A new runtime active model is resolved at the next attempt boundary unless there is a member override. Prompt/template changes to reusable team definitions apply to future runs; an in-flight request is never mutated. A deliberately revised run records its new inputs and invalidates downstream artifacts where needed.

## 11.2 Context construction

ContextManager selects a coherent request view of stored history. The canonical transcript remains intact. Context selection prioritizes the exact active ticket specification, current step, unresolved user feedback, required protocol state, current plan and findings, recent relevant exchanges, and then older context.

A tool-call message and its results form an indivisible exchange for compaction. The context manager cannot retain a tool result after dropping its call, or send a new request while a delegation result remains pending. Summaries do not replace approval records or acceptance evidence.

Attached file contents are data with their path, content hash, read range, and provenance. A file watcher invalidates stale cached reads but does not silently rewrite previous conversation messages. Before an edit, the current file hash is checked independently of what the prompt claims is current.

## 11.3 Token budget and compaction

A request budget reserves output tokens and a configurable safety margin. A profile may use an available tokenizer or endpoint-provided count. Otherwise a conservative estimate is clearly labeled and corrected against reported usage over time. A generic character-to-token heuristic is not presented as an exact count for every model.

Compaction begins before the estimated input reaches the usable context budget. The default preserves exact requirements and a recent tail, summarizes complete older exchanges, and records the source message range and summarizer profile. Summarization itself consumes requests and budget. It cannot widen permissions or edit the ticket.

When exact requirements plus required protocol context cannot fit, the run pauses with an actionable explanation. It must not silently drop scope exclusions, acceptance criteria, or the latest user correction. Endpoint context-window errors trigger one explicit re-budgeting pass; repeated errors pause instead of starting an infinite retry loop.

## 11.4 Skills are included in v1

SkillRegistry discovers directories containing SKILL.md and its metadata, instructions, and optional reference assets. Retain the Agent Skills packaging convention without inheriting .roo paths or Roo's behavior. The standard describes the file convention; scope, precedence, and tool authorization are AgileCode policy. [23]

Global user skills live under ~/.agilecode/skills/. Project skills live under <project>/.agilecode/skills/ when used; code-owned skills in an eligible source directory are resolved against the ticket worktree. Built-in assets are exposed through the skill service or initialized into the managed global directory, not by granting arbitrary reads across the installation tree.

A team member lists available skill references. Discovery exposes short descriptions, provenance, validation status, and supported assets. Loading a skill supplies the full instructions on demand. A process does not require using a skill merely because one is available, and skills cannot replace executable step gates.

Choose project-local overrides before global and built-in entries when the override is deliberate and visible. Resolve and record the concrete skill path/hash used by an attempt. Display duplicates, invalid frontmatter, inaccessible assets, and name mismatches in settings; do not silently hide a skill and leave the user guessing.

Skill documents are read-only to ticket agents outside their worktree. A script packaged with a skill does not execute on load. An explicit command or tool invocation still passes the common policy and CommandGuard. Skill instructions cannot grant access, enable a blocked tool, or authorize writes to global settings.

## 11.5 Slash commands and mentions

Slash commands are user-invoked templates that expand into a visible user message, selected skill, or approved application command. They are not arbitrary Python plugins. Template arguments are plain data and are not interpolated into shell commands without the shell tool's validation and approval.

File mentions resolve relative to the session's workspace, not the tab that happens to be active. Range selection produces a hashed attachment. Images are copied to managed artifacts and sent only when the profile supports them. A mention never bypasses protected-path rules.



# 12. Tool framework and execution contracts

## 12.1 Core interfaces

The generic tool system has two concepts: `Tool` and `ToolManager`.

A `Tool` object defines one model-callable capability. It owns its unique name, description, argument schema, dependencies, validation and asynchronous `execute` behavior. Native capabilities are concrete classes such as `ReadFileTool`, `WriteFileTool`, `RunCommandTool` and `GitCommandTool`. There is no separate ToolSpec, ToolContext, ToolRegistry, ToolExecutor or ToolFactory abstraction.

A `ToolManager` receives already-created Tool objects for one model session. It exposes only the exact tool names listed in that member's `allowed_tools`, converts those tools to provider schemas, resolves returned tool-call names, validates arguments, records the call/result lifecycle and invokes `tool.execute(...)`. Guessing the name of a tool that was not exposed is rejected.

`TeamMember.allowed_tools` contains explicit tool names only. Version 2 team bundles reject category selectors such as `read`, `execute` or `git` and wildcard capability selectors. Version 1 bundles expand legacy selectors to known native names once on load and retain the formerly implicit completion/control tools. Dynamic MCP access must be selected by explicit alias after discovery.

Pydantic models or equivalently strict validators define native tool inputs so provider schemas and runtime validation derive from the same definition. Unknown fields are rejected unless a tool explicitly accepts an extension schema.

## 12.2 Execution pipeline

For a model tool call, ToolManager performs one generic lifecycle:

1. resolve the named Tool from the tools exposed to that session;
2. validate the returned arguments;
3. resolve the Tool's approval decision;
4. obtain a correlated approval only when that Tool's policy says Ask;
5. enforce any Tool-specific hard boundary;
6. record Running, execute the Tool object and record one terminal result.

The Tool object owns operation-specific behavior. ToolManager does not contain a switch statement implementing every tool.

Approval is bound to the exact normalized operation and relevant policy state. A stale approval cannot authorize a changed operation. Hard boundaries are not converted into approval prompts.

## 12.3 Core tool catalog

| Tool | Purpose |
| --- | --- |
| list_files / read_file | Bounded path listing and hashed file reads. |
| code_search | Lexical, regex, path, symbol and reference discovery. |
| write_file / edit_file | Create or exactly replace worktree content against expected state. |
| move_path / delete_path | Explicit worktree rename/deletion. |
| run_command | Submit a command to the ticket's persistent shell after command permission and containment checks. |
| read_job_output / stop_job | Inspect or control an owned command/job. |
| read_artifact | Read bounded stored output by owned artifact ID. |
| git_command | Execute any command whose executable is Git in the ticket worktree. |
| update_progress | Record implementation-plan progress. |
| load_skill | Load an enabled skill. |
| ask_user | Pause for a correlated question. |
| record_check | Record verification evidence. |
| finish_step | Return the step result through process gates. |
| final_commit | Request the engine-owned final reviewed ticket commit. |
| MCPTool / mcp_read_resource | Wrap discovered MCP capabilities behind the same Tool interface. |

Worktree provisioning, restoration, settings changes, integration and cleanup remain application operations rather than model tools.

## 12.4 MCP tools

MCP does not use a parallel execution architecture. Discovery produces stable AgileCode aliases, descriptions and JSON schemas. Each discovered remote capability is wrapped in an `MCPTool` object whose `execute` method calls the owning MCP connection. Those objects are handed to the same ToolManager as native Tool objects.

An MCP alias must be explicitly present in a member's `allowed_tools` before that session receives it. The fixed `mcp_read_resource` capability is likewise a normal Tool. Legacy category-based MCP settings may be migrated for compatibility, but categories are not part of the new authorization model.

## 12.5 Command permission versus containment

`run_command` is a normal Tool with one extra concern: a single tool can represent many different shell operations.

Command permission and command containment are separate:

- `CommandPolicy` returns Allow, Ask or Deny. Existing Full tool autonomy supplies the ordinary default (Allow in full autonomy, Ask in restricted mode). Ordered command rules can override that default for command patterns; the first match wins. The initial rule list is empty so existing autonomy behavior is unchanged; users can configure software-installation and privilege-escalation rules in the command-permissions editor. Patterns match the whole trimmed command string, not parsed shell operations.
- `CommandGuard` is containment only. It returns OK or BLOCK for identified hard boundary violations. It never asks for permission.

A command must pass both systems before execution. Permission cannot override a known containment violation, and parser uncertainty cannot turn an allowed command into a second approval request.



# 13. File operations, search, and code changes

## 13.1 File identity and encoding

Workspace returns a FileSnapshot containing canonical relative path, byte hash, size, encoding, newline style, permissions relevant to editing, and content or artifact reference. Binary and undecodable files are recognized instead of being silently rewritten with replacement characters. Text decoding defaults to UTF-8 with BOM detection; a different encoding requires an explicit supported choice.

Public line ranges are one-based and inclusive. Internal text spans use zero-based offsets with their unit declared. Byte offsets, Python Unicode code-point offsets, and Qt UTF-16 positions must not be interchanged. Code viewers convert spans through an explicit mapping, including non-BMP characters.

A normal source read is capped initially at 256 KiB of returned content. Larger files remain readable by range. The result includes total size, selected range, hash, and whether it is partial. Null bytes, huge single lines, generated files, and decoding errors produce deliberate UI states.

## 13.2 Strong codebase discovery is core

SearchService implements one coherent code_search contract with explicit search kinds: text, regex, path, and symbol. Text search returns exact literal matches; regex supports bounded patterns; path search locates files by name/glob; symbol search identifies class/function/module definitions and provides reference/import candidates. An agent can search, inspect ranges, follow imports/calls, and refine without knowing a filename in advance.

Implement fast lexical/path search first, using a Python scanner or an optional ripgrep accelerator behind the same interface. Symbol discovery should use Python's AST for Python code and appropriate parser-backed discovery where supported. Textual reference candidates for other languages must be labeled as candidates, not a complete semantic reference graph. This is more useful than exposing only recursive directory listing, without requiring a vector database.

Every result includes scope, path, range, bounded context, content hash, search kind, and any truncation/staleness indicator. Default to the active ticket worktree so code edited by that ticket is searchable immediately. Original-project reads are deliberate. Never merge matches from another ticket merely because it lives beneath the same .agilecode directory.

Exclude Git metadata, application runtime data, unrelated worktrees, caches, and configured generated/dependency folders. Return partial-search indicators when file, byte, match, or time limits are hit. Large/hostile regex evaluation must run in a cancellable process or use an implementation with reliable time bounds; cancelling an asyncio future does not necessarily interrupt synchronous regex work in a thread.

The initial search may keep small derivative symbol/cache records in the project store or memory, invalidated by content hashes. It must not require an embeddings endpoint, index-building wizard, vector service, or network transmission just to search code. Later hybrid semantic retrieval can add an implementation behind the same result contract when evidence justifies it.

## 13.3 Exact edit contract

edit_file accepts a relative path, expected byte hash, and an ordered list of exact old/new replacements. Each replacement specifies its expected match count; the default is exactly one. No fuzzy substitution is enabled in the initial implementation. Ambiguous, missing, overlapping, or stale replacements are rejected with enough context for a fresh read.

All replacements are applied in memory to the captured content before a preview is presented. The executor verifies the expected hash again after approval and immediately before writing. Newline style and file permissions are preserved. The resulting bytes receive a new hash, and the tool returns a change artifact describing what happened.

write_file distinguishes creating an absent path from replacing an existing one. Replacing requires an expected hash. Moving checks both source identity and destination absence. Deleting previews exact paths and content identities; recursive deletion is not a hidden convenience operation.

## 13.4 Write durability and recovery

For a single file, write a temporary sibling, flush it, preserve required metadata, replace the target atomically where supported, and flush the containing directory when durability requires it. Directory-descriptor and no-follow facilities are available on supported operating systems; the adapter must test the actual platform rather than pretend every operation is portable. [27]

Before mutation, persist a write intent with before/after hashes and backup/staging artifact IDs. After mutation, verify the actual file and record completion. A crash between replacement and database completion is reconciled using those hashes. A file that matches neither expected state is a conflict, not permission to overwrite it.

A multi-file tool operation is not falsely advertised as one atomic filesystem transaction. Prepare and validate the entire batch, retain backups, apply files in deterministic order, and record per-file progress. On failure, stop and offer a guarded recovery plan. Compensating rollback may itself conflict with external changes and must not erase them.

## 13.5 Manual editing and diffs

The native code component supports reading and bounded manual edits, not a full IDE. Enabling manual editing for a ticket pauses agent writes and obtains the same worktree lease. Saving calls the same hash-aware file service as a tool. Unsaved buffers are visible and block operations that would overwrite them.

Diffs are generated from explicit base and target contents. Git supplies repository differences; the application maintains one DiffDocument with files, hunks, old/new line mappings, change kinds, and content fingerprints. Git's documented diff formats are the input contract, not text inferred from colored terminal output. [16]

Unified and side-by-side views render that same object. Binary files, renames, deletions, permission changes, missing final newlines, and untracked files are represented explicitly. A new untracked text file is compared against empty content. Hunk-level approval is deferred; baseline approval applies to the complete previewed operation or final change set.



# 14. Persistent shell and command guarding

## 14.1 Ticket-owned shell semantics

Every active ticket/worktree has a real persistent shell. A command runs in that shell's state, so its cwd, exported environment, virtual-environment activation, and supported shell functions persist across subsequent commands and across process steps while the shell lives. Each new agent step gets a fresh model session but the same ticket shell.

Commands are not all launched as unrelated fresh subprocesses. The application does not simulate persistence solely by remembering a cwd string. A Linux implementation may begin with Bash as the tested shell, behind a Python ShellService; adding another shell requires testing its command framing and state semantics.

A shell is an OS process, not a serialized object. A crash, explicit exit, or forced termination destroys its volatile state. Recreating it is visible and uses declared setup/environment settings. AgileCode never tells a resumed agent that an old virtualenv activation survived when it did not.

## 14.2 Execution and completion protocol

Serialize commands submitted to the same shell. Evaluate them in the parent shell context rather than a per-command subshell that discards environment changes. Use a per-command identifier and a separate control channel to report exit status and the actual cwd. A prompt regex, output silence, or a model's interpretation of a test summary must not determine command completion.

Drain stdout and stderr with bounded buffers, stream readable output, and retain full permitted output in owned artifact files. Python provides asynchronous subprocess primitives, but output draining, shell state, and cancellation policy remain application responsibilities. [13]

The initial UI is a command console with streamed output and stdin support where the adapter supports it, not a claimed full terminal emulator. Programs requiring a TTY must be identified and supported by a tested PTY path or reported clearly; they must not appear to work while input is actually lost. An external terminal is an explicit user action and outside agent CommandGuard control.

## 14.3 CommandGuard's purpose

CommandGuard prevents ordinary agent workflow mistakes: changing into the original checkout, writing through an absolute path outside the ticket, using git -C on another worktree, or redirecting output into global settings. It is not a security sandbox and does not attempt to prove all possible effects of arbitrary programs.

ShellService checks CommandGuard immediately before dispatch. The guard receives the command and observed shell cwd relative to the ticket worktree and returns only Allow or Deny. It has no approval state. Full autonomy never overrides a known forbidden destination.

The shell stays anchored to its worktree. Reading approved original-project/global context normally uses scoped file tools and does not require cd outside. A command's explicit cwd, directory changes, redirects, and recognizable mutation destinations are checked against the same workspace identity as file edits.

## 14.4 Recognized checks

| Form | Required behavior |
| --- | --- |
| cd src, cd .. within the ticket | Allow when the canonical destination remains in the ticket worktree. |
| cd outside, pushd outside, cd to original project | Reject before execution; explain the permitted root. |
| cp/mv/install with explicit destination | Inspect destination and, where applicable, source deletion. Reject recognized writes outside. |
| >, >>, tee, heredoc output redirection | Check each recognized output target; reject a destination outside the ticket. |
| git -C or --work-tree selecting another checkout | Reject; ordinary ticket Git reads use the current ticket context. |
| GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE redirection | Reject agent attempts to retarget repository administration. Private indexes are engine-owned operations only. |
| git commit/reset/merge/rebase/push in a standard coding step | Reject uncontrolled lifecycle changes; final commit/integration use their designated application operations. |
| python script.py, pytest, build commands | Permit under the configured execution policy with the explicit limitation that script-internal writes are not statically proven. |
| Unknown shell constructs or indeterminate direct effects | Do not create an approval merely because parsing is incomplete. If no explicit boundary violation is identified, leave authorization to CommandPolicy and preserve the documented best-effort containment limitation. |

Parsing must respect quoting, escapes, pipelines, sequential commands, and common redirections. Substring checks such as searching for '..' are insufficient. Shell-specific parsing is isolated inside the adapter and tested against ordinary workflows; it does not become a new general-purpose shell-language project.

A directory change hidden inside sourced code or a script can evade pre-inspection. Observe actual cwd after execution. If it is outside the worktree, stop further dispatch and restore a valid shell context or recreate the shell before continuing. This is a detected policy violation, not retroactive prevention of earlier effects.

## 14.5 Environment and jobs

Initialize cwd to the ticket worktree and use a declared environment profile. Do not place model/MCP credentials into every child environment. Ordinary interpreter executables and system libraries remain usable; the context-read policy is not a chroot that hides the runtime from the OS.

Long-running services may be represented as managed jobs launched with an explicit snapshot of the shell's selected environment and worktree cwd. They remain owned by the ticket and appear with job IDs, status, output, and stop controls. Starting such a job does not replace the persistent shell or imply that changes made by a child can modify the parent shell environment.

Verification should record its actual cwd, executable, environment fingerprint, command, exit status and logs. It may use the persistent shell or a managed job with explicitly captured settings; it must not silently ignore an activated environment by switching to a different execution mode.

## 14.6 Cancellation and uncertainty

Interrupt a managed command using the tested shell/job mechanism, then verify whether the shell remains usable. Escalate to terminating the owned process group when the stop policy requires it. Do not mark a mutating job stopped while it is known to keep running, and do not cancel another ticket's processes.

A shell losing its control channel, exiting unexpectedly, or failing to report a command outcome becomes unavailable/interrupted. Preserve partial output and mark uncertain effects. Reconciliation must not rerun a command automatically just because no exit code was stored.

Live shell state, stdin input, environment dumps, and process identity are not interchangeable. Persist enough redacted metadata to explain what ran and detect stale processes; never rely on a PID alone after a restart. The baseline deliberately has no bubblewrap/container dependency or guarantee against hostile shell escape.


# 15. Project layout, Git branches, and worktree lifecycle

## 15.1 Minimal storage layout

AgileCode operates on an external project. Its installation directory is not the project root. Use the following logical layout:

```text
~/.agilecode/
    settings.json                 # global settings and saved team bundles
    skills/                       # global skill documents/assets

MyProject/
    .git/
    .gitignore
    src/
    tests/
    .agilecode/
        agilecode.db              # all structured state for this project
        artifacts/                # logs, reports, exported diffs, images
        worktrees/
            AC-0042/
            AC-0043/
        integration/              # temporary engine-owned merge candidates
```

The specific global configuration filename is an implementation default. Global teams, their members and processes are stored together in the user-level configuration; local team bundles and project settings are in the project database. Skills and large artifacts are inherently files. No ticket.json, per-session JSONL transcript, per-ticket database, separate process library, or nested project store is required.

The .agilecode/ entry is ignored by normal project history. The same exclusion must work in ticket checkouts even when the original setup change to .gitignore has not yet been committed. The engine can maintain an equivalent Git local exclusion during authorized setup. Do not silently modify the ticket's source .gitignore or assume ignored files stop being tracked automatically.

## 15.2 Original root, execution root, and metadata

Git linked worktrees have distinct working files and indexes but share repository objects and most refs/configuration. Use Git's documented commands to resolve these paths; do not assume .git is a directory or independently writable repository. [14]

Record the original root, common Git directory, ticket worktree root, ticket branch ref, active source branch ref at start, and exact starting commit. Each operation uses these recorded identities, not the visible UI project or a mutable global cwd.

Ticket agents write source only in their own worktree. Application services legitimately write the project database/artifacts and shared Git metadata to implement that work. The MergeCoordinator has a separate, explicit integration scope. These administrative writes are not general agent write permission to the original tree.

## 15.3 Start from the currently active branch

At execution start, observe the branch checked out in the original project and its committed HEAD. Create the ticket branch from that exact OID and record the branch as the integration target. Do not assume main or master, and do not choose the user's later active branch as a new target for an already-running ticket.

Disclose uncommitted original-tree changes: a new Git worktree contains committed content, not those unsaved changes. Never stash, commit, copy, or discard them automatically. A detached HEAD, unborn repository, unresolved operation, or branch/path collision yields a concrete preflight issue rather than guessing a target.

The existing branch may advance while the ticket runs. Do not automatically merge/rebase it into the ticket mid-process. Integration handles the current target later, including normal merge and AI-assisted resolution. This is the already-settled conflict policy, not a separate workflow question.

## 15.4 Recoverable provisioning

Persist a start intent with operation key, ticket/run IDs, intended branch, expected base OID, and worktree path. Validate existing paths/refs, create the branch and worktree through typed Git operations, verify their actual identity, and record provisioning complete. Then open the shell and begin step 1.

If an operation fails, keep evidence of what was created. On replay, reuse only resources whose identity exactly matches the stored intent. Do not adopt an arbitrary directory because its name resembles a ticket key. A completed start returns the existing run for the same operation key.

Each active ticket can provision independently. Short common-repository mutations are coordinated; a waiting model call or a long test must not hold the repository administration lock. One nonterminal run per ticket prevents two processes from using the same worktree without preventing another ticket from running.

## 15.5 Git operations during development

Built-in status/diff/log operations are read-oriented and scoped to the ticket. Agent commands cannot switch branches, rewrite common refs, configure shared repository state, or create standard deliverable commits before step 8. The application uses argument arrays and explicit working directories rather than concatenating model strings into Git commands.

Clear inherited Git location-override variables for application Git operations. Intentional temporary-index variables are constructed only for the checkpoint/candidate operation that needs them. Do not globally alter repository configuration or disable the user's hooks for unrelated work. Hooks/filters relevant to commit creation must be handled visibly and their effects checked.

Manual source edits in the ticket use the same write lease and stale-content checks as agent edits. External Git/ref changes invalidate assumed base/head state and require reconciliation. A user may work in the original tree independently; AgileCode never treats those changes as part of a ticket simply because it can read them.

## 15.6 Cleanup after successful integration

Default policy: remove the ticket worktree, delete its hidden checkpoint refs, retain the ticket branch, and retain SQLite history. The user can change cleanup settings. Do not clean automatically before successful integration, on an ambiguous merge, or merely after the final ticket commit.

Stop and settle ticket shells/jobs before removal. Verify no unsaved editor buffer, uncommitted user change, unexpected untracked source, or unresolved operation would be discarded. Runtime artifacts already stored under the project's artifacts directory survive worktree deletion; any outstanding worktree-local output must be retained or explicitly classified as disposable.

Use git worktree remove through the lifecycle service; do not recursively delete a guessed path. Forced removal of known engine-owned disposable files must never become forced deletion of unknown user changes. Unexpected contents block automatic cleanup with a recoverable explanation.

Delete only checkpoint refs belonging to the completed ticket/run according to policy. Keep checkpoint metadata/history marked as cleaned, so the UI does not present a working Restore action after its object retention has ended. Keeping the branch is a default, not a claim that deleting a merged branch is inherently irreversible.


# 16. Hidden Git checkpoints and guarded restoration

## 16.1 Agreed checkpoint model

Checkpoints are Git commit objects stored under AgileCode-owned refs, for example refs/agilecode/checkpoints/<ticket-id>/<checkpoint-id>. They do not advance the ticket branch or appear in its normal history. The standard process therefore still produces one deliverable ticket commit at step 8, while retaining recovery points during implementation and corrections.

```text
Ticket branch:       B ---------------- F
                     base               final step-8 commit

Checkpoint refs:     B -- C1 -- C2 -- C3
                         internal snapshots, not ticket-branch ancestors
```

“Hidden” means outside normal branch history, not secret or invisible to all Git tooling. Ref enumeration, backups, or a mirror push can expose custom refs. Automatic push is not part of AgileCode's baseline. Checkpoint retention and export must account for their existence.

Git supports writing tree/commit objects and updating refs separately. Temporary-index use and namespaced refs are the implementation mechanisms; checkpoint orchestration, coverage, recovery, and retention are AgileCode policies. [32–36]

## 16.2 Coverage and exclusions

Capture the eligible project-file state in the ticket worktree, including tracked modifications/deletions and permitted new nonignored files. Store file paths, raw file bytes, executable bits, and symlink objects using Git representations. Do not follow symlinks into the original project or elsewhere. A snapshot never includes another ticket's worktree.

Exclude .git administration, .agilecode runtime content, virtualenv/dependency caches, declared disposable build output, and protected credentials. An ignored file already tracked as project source must be handled deliberately; ordinary ignore rules do not silently remove tracked source from checkpoint coverage. Show excluded or unsupported data in checkpoint details.

The checkpoint is not an OS snapshot. It does not restore shell processes, database services, network effects, arbitrary permissions/ACLs, timestamps, or empty directories. Submodule internals, special files, and large-file filter workflows need explicit support before being advertised. A file that is excluded is not falsely reported as safely recoverable.

## 16.3 Capture without altering the ticket index

Acquire the ticket's write lease at a safe boundary with no mutating job active. Enumerate eligible source files, validate paths, record modes and content identity, and detect file changes during capture. An externally changing file requires retry/review; an inconsistent snapshot must not be labeled complete.

Create a private temporary Git index using GIT_INDEX_FILE for this one operation. Build the checkpoint tree independently of the user's normal staging area. Write raw blob content without clean/smudge or end-of-line filtering, populate index entries from those blob OIDs, and call write-tree. Raw-byte storage prevents a checkpoint from unintentionally normalizing CRLF or running a project filter during capture; hash-object documents this no-filter behavior. [35]

Create a commit object with commit-tree, parented to the previous checkpoint or recorded base, and publish its unique private ref with an expected-absence check using update-ref. A schema-versioned DB record retains the ref, commit/tree OIDs, parent checkpoint, scope, byte fingerprint, timestamp, and process/session recovery boundary. The operation must leave ticket HEAD, its ordinary index, and the original checkout unchanged. [32–34]

Use a prepared operation journal before publishing the ref. After publication, record checkpoint completion. A crash after Git succeeds but before SQLite acknowledges is reconciled by the exact ref/OID, not by creating another snapshot blindly. Temporary indexes are disposable operation files, not additional authoritative databases or a shadow repository.

## 16.4 Capture timing

Capture an initial baseline after worktree provisioning, before a potentially mutating step attempt, and before a restore. A completed implementation/correction can produce a checkpoint supporting verification/review identity. Avoid an expensive full capture after every streamed token or harmless read.

Timing is an engineering policy. It must provide a recoverable state before destructive engine-owned changes without creating so many refs that ordinary use becomes unwieldy. A checkpoint requested while a mutating command runs waits for a safe boundary or fails clearly; it cannot claim an atomic view of active external writes.

## 16.5 Restore files

Pause the affected run, stop/settle mutating jobs, and obtain the worktree lease. Compare the chosen checkpoint tree with current eligible files. Produce a preview listing replacements, creations, removals, excluded data, and any changes not attributable to known ticket work. Create a pre-restore checkpoint before applying a permitted restoration.

Read checkpoint blobs and write them through the same guarded atomic file service. Restore only paths in scope and recorded file modes. Remove files created after the checkpoint only when they are explicitly included in the preview; do not use blanket git clean or reset --hard. Validate all destination parents again to avoid writing through a changed symlink.

Apply and journal per-file progress. Multiple filesystem writes are not one SQLite transaction. If interrupted, compare observed hashes with intended before/after states and resume or request review. Do not overwrite a third, externally modified version merely to finish a restore.

Restoration must not move the deliverable branch backward or replace its normal index silently. In the standard process, checkpoint rollback normally occurs before the final commit. Post-commit correction/reopen is an explicit new execution decision, not an invisible history rewrite.

## 16.6 Restore process/session position

Offer a distinction between restoring files only and restoring files with a prior execution boundary. File-only restoration invalidates downstream verification/review and records an explicit restoration event. It does not pretend completed checks remain applicable.

Restoring an execution boundary preserves historical attempts and tool outcomes. Branch the effective session/context or begin a new identified attempt where required; do not delete history. Previously executed shell/MCP side effects remain historical facts even if their source changes are rolled back. A recorded output can be reused only when still applicable to the restored state.

## 16.7 Cleanup and backup

After successful integration, the default cleanup removes private checkpoint refs for the completed ticket while keeping metadata and normal ticket history. Objects become eligible for Git's normal garbage collection when no remaining ref retains them; ref deletion does not promise immediate disk reclamation.

A database backup alone does not contain the Git checkpoint objects. A full project backup that promises checkpoint restoration must preserve the relevant Git objects/refs, source repository, and application artifacts as well as SQLite. An export without those objects must clearly label checkpoint restoration as unavailable. These are explicit retention semantics, not another bespoke snapshot format.


# 17. Verification, final commit, and AI-assisted integration

## 17.1 Verification and review evidence

Step 2 defines requirements with verification methods. Step 6 evaluates those requirements against the actual ticket worktree. Record executed commands, exit codes, scoped inspections, source hashes, test/report artifact references, and the applicable requirement IDs. Passed, Failed, Not Run, and Not Applicable are distinct check outcomes.

Step 7 reviews the whole ticket change against the recorded base, not just the latest correction. Its report addresses correctness, scope, regressions, unnecessary complexity, and adequacy of verification. A review session is fresh even if the same runtime model was used earlier. It must inspect the code and evidence rather than simply trust the programmer's summary.

Record the assessed workspace fingerprint on both steps. Any later relevant file change invalidates those results for commit authorization. Missing mandatory evidence blocks PASS. User-authorized exceptions, when supported, are recorded explicitly and visible in the final report; they must not masquerade as tests that passed.

## 17.2 Step 8: one final ticket-branch commit

The standard process makes no deliverable intermediate commits. Internal checkpoint commit objects are a different mechanism and never count as completing step 8. A custom process may explicitly include additional commit steps, but this must not weaken the standard process's contract.

The assigned member prepares a descriptive commit message using the actual changes, requirement/plan outcomes, and verification summary. The engine owns staging and commit execution. It verifies branch/base identity, stops competing writes, checks that current content matches the latest passed verification/review, stages only the reviewed eligible file set, and creates the commit in the ticket worktree.

After the commit, inspect its parent(s), tree, message, and worktree status. A Git hook or clean filter can change what was committed; do not report a verified deliverable when the actual result differs from the reviewed candidate. Record the discrepancy and require reconciliation/validation. Never compensate with an automatic destructive reset.

Step 8 returns PASS only after the final commit has been observed and durably linked to the attempt. If there are no changes, report that condition instead of fabricating a meaningful commit or silently skipping the step. A no-change resolution must be explicitly handled, such as an authorized empty final commit or a revised ticket disposition.

## 17.3 Integration policy is user-controlled

The project provides a simple Merge on completion toggle. When off, the result waits for user inspection before merge. A per-ticket override is permitted. This choice is independent of tool approval settings and is recorded for each IntegrationAttempt.

Under either policy, the MergeCoordinator owns integration; individual agents and ticket shells cannot merge themselves into the original project concurrently. Standard tickets remain In Progress while awaiting integration and become Done after successful integration. The development process can be complete earlier, with its final commit clearly shown.

Review mode presents the exact candidate diff, ticket commit, target revision, test results, and conflict-resolution report. Approval applies to those identities. Automatic mode supplies policy authorization to proceed when the same objective guards succeed; it does not accept unresolved conflicts or unsupported claims of correctness.

## 17.4 Normal Git first

Serialize integration operations for a target ref. Read the current target OID and the ticket final OID, verify the intended original checkout/target, and prepare the candidate in an engine-owned temporary integration worktree beneath .agilecode/integration/. Ordinary Git merge is the first resolution mechanism; there is no default automatic rebase. Git's merge behavior supplies the textual combination, while AgileCode supplies validation and workflow. [26]

When a fast-forward is possible, the candidate may simply be the final ticket commit. When branches diverge, create a normal merge candidate from the exact target and ticket revisions. Do not update the original tree while experimenting with conflict resolution.

Even a conflict-free Git merge can combine incompatible behavior. Run the required integration checks against the candidate, particularly when the target advanced. Do not treat the absence of textual conflict markers as evidence that both tickets' requirements still hold.

## 17.5 AI-assisted conflict resolution

If Git reports conflicts, retain the common base, both revisions, conflict stages, affected file paths, both available ticket specifications/requirements, relevant diffs, and tests. If one side came from outside AgileCode, use its commit/diff context and identify the missing ticket intent rather than inventing it.

Create a fresh integration-resolution session using an appropriate member from the selected team and the normal runtime model selection. Its write scope is the integration candidate worktree, not the original project or either completed ticket worktree. This is a MergeCoordinator operation, not a ninth hidden step in the standard process.

The AI proposes candidate edits that preserve intended behavior on both sides and explains the resolution. Apply them through the ordinary guarded tool pipeline. Check that unmerged entries and conflict markers are resolved, run the relevant verification, and obtain code review of the resolution. Preserve failed candidate attempts and rationale for inspection.

Conflicts involving incompatible requirements, unclear intent, unsupported binary/submodule changes, failed checks, or repeated unsuccessful repair become a user-review/question state. An agent's self-reported confidence is not a calibrated acceptance signal. Automatic integration can proceed only after objective checks and the configured review gate pass; otherwise it pauses rather than selecting ours/theirs indiscriminately.

## 17.6 Apply the verified candidate

Before applying, re-read the target OID, verify that the original checkout is on the recorded target branch, and require a clean destination with no unresolved Git operation. Unsaved or uncommitted original-tree changes are not discarded. If the original branch is different, block integration with a precise explanation rather than switching it behind the user's back.

Approval/tests are stale if the target or candidate changed. Rebuild or revalidate the candidate, then require fresh user approval in inspection mode. In automatic mode the policy can authorize a newly validated candidate, but it does not waive the revalidation.

Apply the candidate through a guarded fast-forward update in the correct checkout. Do not force-update a checked-out branch with a raw ref write. Observe both the target ref and checkout status before recording integrated. External Git clients do not honor AgileCode's locks, so rechecks and explicit race recovery are still needed.

## 17.7 Recovery and completion

Persist integration intent before any target mutation with old target, ticket/candidate OIDs, policy, and approval. If the application crashes, inspect actual refs and checkout state. Matching the candidate can establish that a ref moved, but remaining dirty or unresolved state still needs reconciliation before a fully integrated result is reported.

Once successful integration is durable, move the ticket to Done and schedule the configured cleanup. A cleanup failure preserves the merged result and produces a retryable maintenance state. A failed or unresolved merge preserves the original final ticket commit and all unintegrated resources.

Remote push remains separate and never follows automatically from final commit, merge-on-completion, or Done. AI resolution, local integration, and publication are different actions with different destinations.


# 18. Persistence, transactions, and data contracts

## 18.1 One authoritative database per project

Use <project>/.agilecode/agilecode.db for all structured project state: board records, team bundles saved locally, runs, step attempts and outputs, sessions/messages, tool calls, questions/approvals, jobs, checkpoint metadata, integration records, and recovery operations. Related tables in one database are intentional. Do not split them into per-ticket databases or parallel JSON/JSONL histories.

Source remains in Git worktrees. Large logs, reports, diffs, images, and other file-like artifacts live under .agilecode/artifacts/ and are referenced from SQLite. Git itself stores checkpoint trees/commits; the database stores their identities and recovery boundaries, not duplicate source-file blobs.

Global user settings and global team bundles remain in AgileCode's own home directory. They are not a global database containing every project's activity. Project-local settings and team bundles belong to that project's database and can be exported as ordinary versioned data when requested.

## 18.2 Connection and concurrency policy

Use a dedicated worker that owns the sqlite3 connection and executes complete short operations. The engine awaits that worker without blocking the GUI or holding a transaction across a model request, command, user question, or Git action. There can be multiple active tickets while database writes remain serialized.

The proposed initial settings are WAL, explicit foreign-key enforcement, bounded busy timeout, and durable synchronization for authoritative records. SQLite supports concurrent readers and a writer in WAL mode, but not simultaneous independent writers or a claim that WAL works on every network filesystem. Keep the live store local and use proper backup rather than syncing a live WAL directory indiscriminately. [17, 18, 25]

One writable engine owns a project at a time. This is an application-process ownership rule, not a one-ticket concurrency restriction. A second frontend should attach to the owner when such hosting exists or open a read-only view; it must not independently mutate the same project without coordination.

## 18.3 Logical schema

The following are cohesive records, not a mandate for a service per table. Simple JSON payloads are appropriate for team bundles, snapshots, settings, result output, and optional metadata; searchable identities/statuses remain columns.

| Table/group | Principal contents | Important constraints |
| --- | --- | --- |
| project_settings | Project identity, settings, default team/process, integration/cleanup policy. | One owning project; versioned validated settings. |
| team_definitions | Project-saved complete Team bundle, name/version and source scope. | Members/processes saved together; validate internal references. |
| tickets / ticket_revisions | Board state, ordering, labels, objective/scope snapshots, team/process selection. | Four states only; unique visible key; immutable revision records. |
| ticket_dependencies | Ticket/prerequisite pairs; parent remains on ticket. | No self/cyclic dependencies or parentage. |
| runs | Ticket revision, team/process snapshot, current step, state, worktree, controls. | One nonterminal run per ticket, not per project. |
| worktrees | Ticket, path, branch, base branch/OID, lifecycle status. | Unique live path and recorded Git identity. |
| step_attempts | Run/step/attempt number, member/model snapshots, session, status, StepResult, fingerprints, timestamps. | Unique run/step/attempt; immutable completed result. |
| sessions / messages | Attempt and optional parent, conversation sequence/content, state/generation. | Canonical transcript; no independent UI transcript. |
| conversation branches | Optional restore/fork ancestry and boundary. | Historical messages/effects are not deleted on restore. |
| model_requests | Session/generation, input/options, requested/reported model, status, usage. | Per-response parsing identity; null means unknown. |
| tool_executions | Request/call identity, tool name, arguments, policy, state and result. | Unique request/call; one terminal result. |
| questions / approvals | Originating attempt/session, normalized request, hashes, answer/decision. | Resolve once against matching pending identity. |
| jobs | Shell/job identity, command, cwd/environment fingerprint, status, exit/certainty, output refs. | Ticket ownership; no replay merely from missing exit record. |
| artifacts | Owner, relative locator, media type, size/hash, retention classification. | Locator under managed artifact root; publish after durable write. |
| checkpoints | Ticket/run/attempt, private Git ref, commit/tree OIDs, context boundary, coverage/retention. | Never substitute a file-manifest artifact for the agreed Git-backed model. |
| evidence | Requirement/check/review outcome, producer, fingerprint, command/artifact references. | Current evidence required for commit/integration. |
| integration_attempts | Target/ticket/candidate OIDs, merge policy, AI results, checks/review, status. | Candidate approval invalid after target/content change. |
| operations / activity | Idempotent intents, external progress/outcomes, meaningful audit events. | Unique request keys; incomplete-operation index. |
| schema_migrations | Applied version and identifier. | Ordered, backed-up migrations. |

Outputs are stored as text inside StepResult data for the ordinary case. Exceptionally large output can reference a retained artifact through attempt metadata, with bounded previews. This does not create a second authoritative per-session file transcript.

## 18.4 Atomic transitions

A start transaction freezes the ticket/team/process and records worktree intent. Starting an attempt records its sequence, member, resolved model, and session together. Completing a step records its StepResult, evidence references, and next-step/correction pointer in one transaction before the next step starts.

A question answer or tool approval updates the matching pending record and the corresponding resumption state together. Repeated submission of the same operation ID returns the existing outcome. Final-commit completion records the observed OID and step-8 result; integration completion records the observed target/candidate and board Done. Cleanup is independently recoverable.

Use expected revisions to reject stale UI edits or approvals. Archiving is not physical deletion and must not cascade through evidence, pending operations, or child history. The database's relational consistency is useful precisely because these records remain together.

## 18.5 External effects and artifact publication

File writes, command execution, Git commits/ref changes, and remote MCP actions are outside SQLite atomicity. Use prepare intent -> execute -> observe -> persist result -> notify. An uncertain outcome remains Unknown; no universal exactly-once guarantee is claimed.

Publish artifact files only after a durable write and hash verification, then store their references. Clean orphaned temporary artifacts conservatively. Large command output may stream into a pending artifact while its metadata indicates incomplete status; the finalized byte length/hash is recorded on completion.

Private checkpoint refs use expected old values or expected absence when published/deleted. Recovery observes the ref/object rather than assuming that a missing DB acknowledgment means the Git operation failed. The same principle applies to final commits and merge application.

## 18.6 Serialization, backup, and migration

Use validated JSON for saved/exported definitions and records; never pickle user-imported state or execute Python expressions from configuration. Team exports contain members and processes together, with format version and internal IDs. Global export omits credentials; model overrides remain references and can be remapped visibly during import when needed.

Use SQLite's backup API to create a consistent database snapshot. A full recovery backup also needs referenced artifacts and the repository Git data, including private refs/objects whose checkpoint restoration is promised. A database copy alone cannot restore worktree source or checkpoint blobs. [19]

Before destructive schema changes, create/verify a backup, then apply versioned migrations transactionally where possible. A newer unsupported database version opens read-only with an explanation. The clean-slate design does not add migration from Roo/Cline formats. Backups, exports, and retained histories do not turn the live application directory into a cloud synchronization product.


# 19. Access scopes, tool autonomy, and operational boundaries

## 19.1 Agreed access scopes

The term global means AgileCode's own user-level directory, typically ~/.agilecode/. It never means the entire home directory or disk. For a ticket agent, the intended scopes are:

| Scope | Read | Write | Interpretation |
| --- | --- | --- | --- |
| Global AgileCode directory | Yes | No | Approved configuration, skills, and context; application-managed user settings and team resources. |
| Original project source/context | Yes | No | Read-only context; not the ticket's editing workspace. |
| Current ticket worktree | Yes | Yes | The only writable project-source workspace for that ticket. |
| Other ticket worktrees and runtime internals | No | No | No general traversal access merely because they sit under the original root. |
| Other disk locations | No | No | No general agent-file access or blanket home/disk permission. |

Approved credential-free settings can be exposed through read models. Credentials and internal database files are not automatically put into an agent prompt or returned by directory traversal. Built-in resources are read through scoped application services.

## 19.2 Nested-root precedence

Because ticket worktrees live beneath <project>/.agilecode/worktrees/, a naive “anything beneath project_root is readable” check would expose sibling worktrees. Path policy must explicitly recognize owned workspace access, approved original-project context, approved global context, and excluded runtime/other-worktree subtrees.

Canonicalize paths and compare path components, not textual prefixes. Resolve existing symlink parents and validate destinations again immediately before writing. A path that starts inside the worktree but resolves to original source is not writable. Never use the current GUI selection as a fallback root.

Normal coding reads default to the ticket worktree. Tools may accept an explicit scope selector for project/global context, but no custom URI scheme is required. A relative src/module.py should not accidentally read the original checkout while the agent is editing the worktree copy.

## 19.3 Application operations versus agent file writes

Application services must write SQLite, artifacts, team settings, checkpoint refs, and worktree administration. The MergeCoordinator can deliberately update a verified original target under merge policy. Those are narrow application operations with their own authorization and journal, not general file writes available to ticket agents.

A team-management UI saves a global or local team through SaveTeam. It does not need to grant the ticket's Programmer role permission to edit global definitions. A report artifact is stored through an owned artifact service; the agent cannot choose an arbitrary path in another ticket's history.

Git administration shares a repository boundary beyond one worktree. Restrict raw metadata mutation even when the command's cwd is within the ticket. Running git config or retargeting GIT_DIR can affect more than working files and is not authorized merely by being executed inside the correct directory.

## 19.4 User-configurable autonomy

Expose three clear decisions for exact tool names: Allow automatically, Ask before use, and Deny. Full autonomy sets permitted effectful tools to automatic execution; restricted operation asks by default. A member's explicit `allowed_tools` list determines which named tools the model receives.

`run_command` additionally evaluates ordered command rules because many shell operations share that one tool name. Those rules decide Allow, Ask or Deny for a particular command; CommandGuard is not part of that permission decision.

The agent cannot enable a blocked or unexposed tool through a skill, prompt, guessed name or MCP alias. Deny is not the same as Ask, and the UI must not substitute confirmation for a requested prohibition.

Approvals bind to the exact operation, hashes, owner, generation, and policy revision. Full autonomy does not bypass stale-edit checks, standard step order, final-commit gates, or merge review when the user selected inspection. Questions can still pause autonomous work when essential intent is missing.

## 19.5 Shell and MCP boundaries

CommandGuard applies to the ticket shell and catches recognized cwd escapes, explicit off-worktree writes, and unauthorized Git operations. It is deliberately not advertised as an OS security sandbox. Running a general program can have indirect effects the guard cannot statically determine. No mandatory bubblewrap/container adapter or speculative sandbox hierarchy is introduced.

MCP tools use the same Allow/Ask/Deny and result pipeline, but arbitrary remote servers do not automatically honor AgileCode's filesystem policy. For a filesystem-capable local server, configure a ticket-scoped instance/root or use an adapter that validates known path arguments. A global server that cannot respect the intended write boundary must not be presented as a guarded worktree file tool.

General external-effect MCP tools require an explicit user-selected server/tool policy and visible effect scope. Claimed read-only/destructive annotations are hints, not proof of behavior. This is an honest boundary of external tool integration, not a requirement to build an additional sandbox product.

## 19.6 Credentials and readable content

Store credential references in settings and use an appropriate OS credential backend when required; local endpoints can omit credentials. Redact keys from request logs, support bundles, team exports, and prompt previews. Do not copy every application environment variable into the shell or a stdio MCP server.

Repository files, skill content, and tool output are data with provenance. They cannot override application controls simply by containing instructions. Network origins are explicit configuration; no silent hosted-model fallback or telemetry destination is added. These hygiene rules protect the user's work without conflating the agreed worktree guard with hostile-code security isolation.


# 20. PySide6 and Qt Quick desktop design

## 20.1 Presentation baseline

Use PySide6 with Qt Quick/QML as the primary UI technology. The headless Python core remains independent of both Qt Quick and Widgets. QML owns layout, visual states, bindings, and interaction affordances; it does not own ticket transitions, SQL, model clients, tool execution, or Git.

Use a thin Python QtBridge exposing read-only list/detail models, properties, and explicit command methods. Qt's model/view infrastructure supports presenting application models to Qt Quick, and QQuickWidget provides the supported direction for embedding a Qt Quick scene inside a widget application. It is not a mechanism for inserting arbitrary QWidget instances directly into a QML scene. [29, 30]

A practical mixed composition can use a minimal QWidget host with QML panels and a selective QWidget code/diff component. The host is a layout/interop adapter, not a return to an all-Widgets product. If no widget component earns its complexity, a QML application shell can remain entirely Qt Quick. Only one selected composition is built for release; no requirement exists to maintain two complete frontend implementations.

## 20.2 Shell and navigation

The application shell includes a project selector/sidebar, top-level Board and Discussion views, a ticket workspace, and Settings/Teams access. The board is the normal entry point. Opening a ticket reveals a persistent header with key, title, branch, execution status, active step/member/model, and pause/stop controls.

The ticket workspace has resizable areas for process/attempt timeline, conversation and questions, source/diff review, and command output. Integration status and its review controls remain visible after the development process completes. The screen distinguishes the ticket worktree from the original project with explicit scope labels.

Switching tabs or closing a ticket detail panel does not cancel its run. All actions carry explicit ticket/run/attempt identities; a background event for ticket A never lands in ticket B because the user changed focus. The bridge reloads read models after missed event sequences or reconnection.

## 20.3 Visual system

Define shared QML design tokens for typography, spacing, surfaces, borders, focus, selection, and semantic status. Use a restrained neutral palette, one accent, consistent radii, and a small spacing scale. Provide both light and dark themes rather than relying on operating-system defaults to make a dense developer tool look coherent.

Use a readable UI face and independently configurable monospace code font. Separate line height, text zoom, gutter width, and density from overall application scaling. Meaningful color must have a label/icon counterpart. Aim for accessible text contrast and visible focus; the WCAG reference supplies a useful contrast benchmark, not an untested compliance claim. [28]

Animations should clarify state changes and panel transitions, not delay work. Respect reduced-motion preferences. Loading, empty, disabled, denied, interrupted, conflicting, and unavailable states must be designed as real components rather than emergency modal dialogs added after implementation.

## 20.4 Kanban board

The board has exactly Backlog, Ready, In Progress, and Done. Each column uses available height, has its own bounded/virtualized scrolling list, shows counts, and retains useful filter context. Cards show key, concise title, priority/labels where configured, team, and current execution badge.

Drag-and-drop can reorder within a column or request a state transition. Drop handlers call TicketService commands and display resulting state/errors. A Ready-to-In Progress drop starts execution after preflight; it does not directly mutate a property and hope the backend notices. Equivalent keyboard actions are required.

Multiple In Progress cards can be active simultaneously. Show Waiting for model response, tool activity, question, or merge review on the owning card; do not imply that only the currently open ticket is running. Filters and archived-ticket views remain ordinary project-management functions.

## 20.5 Team and process editor

A Team editor contains Members and Processes within the same saved entity. A process editor shows ordered steps with name, assigned member selector limited to the owning team, and prompt. The standard eight-step process is visible and clonable. Custom processes are edited intentionally rather than generated invisibly by the model.

Saving provides a Global / This project scope choice. Moving or copying a team carries its processes and members. Show the definition origin and override status; do not merge a local process list with unrelated global members. Validate dangling references before save.

Member settings include role/system prompt, allowed tools, available skills, and Model: Use active model by default. A specific override is optional. The active runtime model selector lives in the application connection controls, so changing a model does not require editing every saved team.

## 20.6 Conversation, questions, and attempts

Render canonical messages as text, returned reasoning, tool cards, code blocks, attachments, and explicit result/error states. Long histories use paging/virtualization. The UI must not maintain a separate persistent chat transcript that drifts from the engine's records.

The process timeline shows all eight steps and their attempts. A verification FAIL appears as a completed check with findings; a network failure appears as an interrupted/failed attempt. Selecting an older attempt reveals its member, actual model, prompts, output, and evidence without changing the active session.

Questions and approvals have stable ownership and remain actionable when a panel is reopened. Answering a question resumes the same session. Denying a tool is clearly distinguished from cancelling a step. Returned reasoning is labeled as returned content; no spinner or empty field implies the server supplied hidden text when it did not.

## 20.7 Source/code component

Code presentation requires line numbers, syntax highlighting, selectable/copyable text, search, range navigation, current-file identity, and clear read-only versus editable scope. Provide bounded manual edits through the same application file service and write lease used by agents. The original project and global resource views are read-only.

QPlainTextEdit with QSyntaxHighlighter is a viable selective widget foundation, not a finished professional editor by itself. Qt documents these components; AgileCode still has to implement gutters, search, scrolling, large-file behavior, dirty-buffer state, and consistent styling. A QML implementation can satisfy the same CodeDocument contract if it passes the same tests. [5, 6]

Keep syntax analysis independent of Qt where practical. Pygments or another selected parser produces spans; the presentation adapter maps them to the chosen component. Convert Python Unicode/code-point or byte offsets to Qt's text indexing explicitly. Test non-BMP characters, tabs, CRLF, missing final newline, and long lines rather than assuming ASCII-only source.

Large files open in bounded/read-only modes with a visible explanation. Syntax highlighting runs incrementally or off the GUI thread where supported by the component contract. Unsaved edits are tracked and block destructive restore, merge review against stale buffers, or cleanup.

## 20.8 Diff and integration review

Use one headless DiffDocument representing files, hunks, additions/deletions, old/new ranges, renames, binary/mode changes, and fingerprints. Unified and side-by-side views render this model; they do not each invent a diff or parse ANSI terminal colors.

Provide synchronized scrolling, collapsed unchanged sections, per-file navigation, and clear left/right labels. The initial review approves the whole candidate; partial-hunk approval is not required. An integration view labels Base, Target, Ticket, and Candidate where conflict context is shown, avoiding the ambiguous “current/incoming” labels that can reverse meaning during merges.

Show AI-produced resolution changes separately from the original ticket diff, with the explanation and subsequent verification/review. Merge approval includes the exact target and candidate revisions. If either changes, stale review is visible and the old approval button cannot apply a different result.

## 20.9 Interoperability, performance, and accessibility

For mixed QML/Widgets, test focus traversal, clipboard, font metrics, high-DPI behavior, clipping, scrolling and theme consistency across the boundary. QQuickWidget has rendering costs and changes render-loop behavior; measure the chosen composition rather than assuming a widget host is free. [30]

Batch text-delta redraws while preserving durable state transitions. Use incremental model updates instead of resetting the whole board on every event. No network call, regex scan, syntax pass over a huge file, or synchronous Git command belongs in a GUI event handler.

All primary operations are keyboard-accessible. Suggested bindings such as file search, command palette, pane focus cycling, and local find are configurable implementation defaults. Focus indicators, accessible names, tooltips, error text, and context-aware shortcuts must be present in both themes.

## 20.10 Acceptance gate

Build and test the intended QML-first composition with realistic synthetic tickets, a long conversation, approvals/questions, failed attempts, source files, and multi-file/conflict diffs. Exercise resizing, theme changes, high-DPI displays, keyboard navigation, streaming, and simultaneous ticket updates.

The gate evaluates professional visual coherence and actual usability: typography, spacing, readable code, meaningful empty/error states, no clipping, responsive input, accurate focus, and clear ticket/workspace identity. It is a component-quality test, not a request to reconsider the already selected PySide6/QML architecture. The headless engine remains testable while the presentation is refined.


# 21. Configuration, team bundles, skills, and MCP

## 21.1 Global and local configuration

Global settings live only in AgileCode's own home directory. They include active connection/model selection, saved endpoint profiles, global Team bundles, UI preferences, and configured MCP connections. Project settings and local Team bundles live in that project's SQLite database. Global never means unrestricted disk access or a database mixing every project's execution history.

Save Team is the unit of definition persistence. The bundle includes all TeamMembers, attached Processes and Steps, and default_process_id. There is no standalone process directory or library that must remap members when imported. Save scope is Global or This project.

Built-ins are available as templates. A local override of a global team is a complete team bundle with an explicit identity/origin, not a field-by-field merge that can leave steps pointing at missing members. The UI exposes scope and provenance. Global changes affect future resolution; a running ticket keeps its frozen definition snapshot.

## 21.2 Team bundle example

This shortened JSON demonstrates ownership and default model behavior. Full baseline step prompts are specified in Section 8; saved definitions contain the actual text, not executable imports.

```json
{
  "format_version": 1,
  "id": "development",
  "name": "Development Team",
  "default_process_id": "standard-development",
  "members": [
    {"id": "analyst", "name": "Analyst",
     "system_prompt": "Clarify scope and requirements.",
     "allowed_tools": ["read_file", "code_search", "ask_user"],
     "skills": [], "model_override": null},
    {"id": "architect", "name": "Architect",
     "system_prompt": "Design proportional changes and plans.",
     "allowed_tools": ["read_file", "code_search", "ask_user"],
     "skills": [], "model_override": null},
    {"id": "programmer", "name": "Programmer",
     "system_prompt": "Implement the stated plan and findings.",
     "allowed_tools": ["read_file", "code_search", "edit_file",
                       "write_file", "run_command", "final_commit"],
     "skills": ["python"], "model_override": null},
    {"id": "tester", "name": "Tester",
     "system_prompt": "Verify requirements using real evidence.",
     "allowed_tools": ["read_file", "code_search", "run_command"],
     "skills": [], "model_override": null},
    {"id": "reviewer", "name": "Reviewer",
     "system_prompt": "Independently review correctness and scope.",
     "allowed_tools": ["read_file", "code_search", "git_command"],
     "skills": [], "model_override": null}
  ],
  "processes": [
    {
      "id": "standard-development",
      "name": "Standard Development",
      "steps": [
        {"id": "understand", "name": "Understand ticket",
         "team_member_id": "analyst", "prompt": "Read and understand..."},
        {"id": "requirements", "name": "Requirements",
         "team_member_id": "analyst", "prompt": "State requirements and checks..."},
        {"id": "architecture", "name": "Architecture",
         "team_member_id": "architect", "prompt": "Determine necessary changes..."},
        {"id": "plan", "name": "Implementation plan",
         "team_member_id": "architect", "prompt": "Produce the ordered plan..."},
        {"id": "implement", "name": "Implement",
         "team_member_id": "programmer", "prompt": "Follow the plan..."},
        {"id": "verify", "name": "Verification and validation",
         "team_member_id": "tester", "prompt": "Evaluate each requirement..."},
        {"id": "review", "name": "Code review",
         "team_member_id": "reviewer", "prompt": "Review the complete change..."},
        {"id": "commit", "name": "Final commit",
         "team_member_id": "programmer", "prompt": "Commit the reviewed work..."}
      ]
    }
  ]
}
```

Control capabilities such as finish_step and asking required questions are supplied consistently by the runtime policy. The example's abbreviated allowed-tools lists do not bypass step restrictions: final_commit remains unavailable to the Programmer outside an authorized commit step. Model override null always means use the runtime active model at attempt start.

## 21.3 Project behavior settings

Store the selected default team and a process belonging to it. Manual ticket creation exposes overrides; otherwise use those defaults. Invalid/missing defaults produce a clear configuration error, not a silent fallback to an unrelated team.

Keep independent settings for tool autonomy, command rules, integration policy, and cleanup. Tool decisions are Allow, Ask, or Deny by exact tool name; run_command also evaluates ordered command-specific rules. Integration is Merge on completion or Inspect before merge. Default post-integration cleanup removes the worktree and checkpoint refs but retains the branch/history. Per-ticket overrides can be recorded without altering the global team bundle.

There is no per-model generation concurrency field. Active model selection, endpoint options, tool policy, correction limits, and lifecycle settings must not be combined into an opaque “mode” object that every session consults globally during execution.

## 21.4 MCP is a first-release capability

Use the official Python MCP SDK through one McpService adapter. Implement connection lifecycle, tool listing/calls, resource discovery/reads, and user-visible connection errors for supported stdio and Streamable HTTP transports. Pin the SDK version and test the actual interfaces selected for implementation. [22]

Each server has a user-selected identity, transport, executable/URL, approved environment/credential references, and enabled tools/resources. No marketplace or automatic server installation is required. The user may configure no servers and still perform normal coding through native tools.

Remote tools are wrapped as Tool instances and dispatched through the same validation, ownership, approval, output limits, logging, and result pipeline. Server/tool names map to collision-safe internal identities. Do not infer authorization by splitting a model-generated name string.

## 21.5 MCP scope and failure handling

Scope filesystem-capable server instances to the current ticket when the server supports roots or explicit worktree configuration. Reuse a global connection only when its operations are stateless/read-only or explicitly authorized external operations; do not share a mutable server cwd between simultaneous tickets.

A server failure disables affected tools visibly and leaves unrelated work usable. An unavailable required tool can pause the step or let the agent choose another permitted method; it must not return synthetic success. Timeouts with uncertain external side effects are recorded as Unknown and not automatically retried as harmless reads.

Server-provided descriptions and annotations do not override application policy. Skills cannot auto-connect a new server or widen its permissions. Some user-selected stdio servers may require Node or another runtime; those are dependencies of that server, not a requirement to add a Node frontend or toolchain to AgileCode itself.

## 21.6 Search extension boundary

Strong lexical/path/symbol discovery ships in v1. The code_search interface exposes results with paths, ranges, hashes, and search kind so a future hybrid semantic backend can be added without redesigning Process or Step.

Embedding-based retrieval, vector persistence, embedding-model lifecycle, and incremental re-indexing are not part of the first implementation. Do not ship a half-built optional vector subsystem merely because the interface is extensible. A later implementation needs its own evaluation of discovery quality, repository scale, staleness, resource usage, and permission boundaries.

## 21.7 Validation and import rules

Definitions are validated data. Imports cannot execute code, install dependencies, activate credentials, create arbitrary paths, or change an in-flight run. Verify format version, stable internal IDs, team ownership, ordered-step validity, tools/skills references, model overrides when present, and correction rules.

A definition referencing a missing optional skill/model/server is shown as incomplete with actionable fields; it is not silently replaced. Export/import retains complete team/process ownership. Scope changes or override collisions require a deliberate save/copy action, not hidden precedence surprises.


# 22. Recovery, diagnostics, and operational behavior

## 22.1 Recovery starts from durable facts

On project open, verify ownership/schema, load stored runs and attempts, inspect incomplete external operations, and reconcile worktree/ref identities. Do not immediately resubmit model calls or rerun shell commands. The board should remain usable for unaffected tickets while a particular run needs recovery.

Completed step outputs and the next-step pointer are durable together. If the application stopped after planning, reuse the completed understanding, requirements, architecture, and plan. If it stopped while implementing, inspect recorded tool effects and current files before resuming that session or creating an identified new attempt.

| Interrupted point | Evidence | Required behavior |
| --- | --- | --- |
| Worktree start | Intended branch/path/base; actual Git worktree/ref list. | Reuse only matching resources or offer repair. |
| Step result saved, next session absent | Completed attempt and stored next-step pointer. | Start the first incomplete step without regenerating prior artifacts. |
| Question pending | Question ID, same attempt/session, any stored answer. | Redisplay or deliver the answer once; no new attempt merely for waiting. |
| Partial model response | Request/generation and fragments. | Mark partial/interrupted; no execution of incomplete calls. |
| File write not acknowledged | Before/after hashes and per-file progress. | Establish known effect or report third-state conflict. |
| Shell/job exit missing | Shell identity, output/control records, current process evidence. | Do not blindly rerun; mark unknown when necessary. |
| Private checkpoint ref published | Exact ref/commit/tree and operation intent. | Reconcile the existing checkpoint record. |
| Final commit outcome missing | Branch/parent/tree/message and commit intent. | Locate/verify the actual commit rather than create a duplicate. |
| Integration partly applied | Target/candidate OIDs and original checkout status. | Confirm actual integration or pause for reconciliation. |
| Cleanup partly done | Worktree/ref existence and retained artifact records. | Continue only remaining owned cleanup; never broaden deletion. |

## 22.2 Error taxonomy

Use stable error codes and user-facing explanations: InvalidDefinition, MissingMember, MissingModel, ToolDenied, OutOfWorkspace, GuardIndeterminate, StaleContent, IncompleteResponse, InvalidStepResult, VerificationFailed, ReviewChangesRequired, UnknownEffect, StaleMergeCandidate, IntegrationBlocked, and CleanupBlocked.

A failed check is not a transport error. A denied tool is not a crashed engine. A missing model setting is not a reason to destroy the worktree. Show owner, cause, current state, and the action needed, without exposing secret values or an unreadable raw exception as the only explanation.

## 22.3 Retry and progress controls

Use bounded transport retries for eligible temporary failures. Use separate, user-configurable correction/progress limits for repeated failed verification or review. Retry counters must not reset simply because the agent invokes a helper or creates another tool call. Limit exhaustion pauses with the unresolved findings rather than inventing success.

The exact request, time, byte, and correction limits are engineering/user settings, not previously agreed fixed values such as 100 requests or three cycles. They must not impose a hidden one-request-per-model concurrency policy. Long legitimate local-model responses need configurable timeouts and a visible Cancel action.

A step that cannot make progress should explain the blocker and ask a focused question. An agent may not expand the ticket, skip the standard plan, discard failing checks, or commit early to escape a loop.

## 22.4 Diagnostics and exports

Log meaningful lifecycle events with project/ticket/run/attempt/session/operation IDs. Retain model request metadata, redacted settings, actual model identity where available, tool/job outcomes, workspace fingerprints, and merge candidate lineage. Do not persist every UI redraw as a database event.

A support export is user-triggered and previewed. It includes selected diagnostics, not the user's entire project by default. Credentials, unrelated global settings, and other tickets' private content are excluded. Full prompts/source can be included only deliberately when needed to diagnose a problem.

Unknown usage remains unknown. A cleanup record marked complete cannot point to a still-running ticket shell. A restored checkpoint marked unavailable after retention cleanup must not keep an enabled restore button. Observability must reflect actual state rather than optimistic UI assumptions.

## 22.5 Shutdown and UI detachment

Detaching a view is not the same as shutting down the engine. While the host remains alive, a replacement UI reconstructs state through read models. When the application process is actually closing, stop accepting new actions, pause/cancel according to the chosen action, settle owned jobs, flush durable records, close streams/MCP clients, and release project ownership.

If a process cannot settle cleanly, record the remaining uncertainty and recovery requirements. Do not promise that persistent shells survive termination or that a headless architecture automatically includes a resident daemon. Hosting the engine as a background service later can reuse the same contracts without moving workflow logic into a frontend.


# 23. Verification and acceptance plan

## 23.1 Verification strategy

Unit tests cover objects, team references, ordered steps, correction routing, policy decisions, path handling, request assembly, and result validation. Integration tests use temporary Git repositories, real subprocess shells, SQLite, fixture MCP servers, and a deterministic compatible model endpoint. Desktop tests drive the intended QML/selected widget composition. Headless end-to-end tests exercise the same application services without Qt.

The scenarios below define evidence required for completion. They are not a report of executed tests. The selected real LM Studio/llama.cpp models and server builds must be recorded when their interoperability is tested; fake-server tests do not prove every real model can use tools reliably.

## 23.2 Acceptance scenarios and traceability

| Scenario | Expected evidence | Req. IDs |
| --- | --- | --- |
| AT-01 · Headless ticket execution | Run all eight steps with a fake model and non-Qt event consumer, including a question, correction and final commit. No PySide6 imports or QApplication creation. | FR-09, FR-10, FR-11, FR-12, FR-14, FR-29, FR-36 |
| AT-02 · UI replacement | Dispose/recreate the frontend while the engine host lives; read models restore board, attempts, question and running-job identity without restarting work. | FR-36, FR-37, FR-38, FR-42 |
| AT-03 · Four-column board | Verify CRUD, filters, ordering and keyboard/drag movement; no separate Verification/Review/Integrating columns appear. | FR-02, FR-03, FR-37 |
| AT-04 · Idempotent drag start | Repeat the same Ready-to-In Progress start operation; exactly one run, branch, worktree and initial attempt result. | FR-04, FR-05, FR-35 |
| AT-05 · Active-branch base | Start from a branch other than main/master; persist that ref and exact OID, preserving original dirty files and omitting them from the ticket checkout. | FR-01, FR-05 |
| AT-06 · Nested layout | All ticket worktrees are under the original .agilecode/worktrees directory; the managed directory stays out of normal source commits. | FR-01, FR-05, FR-20 |
| AT-07 · Concurrent tickets | Run two tickets with independent writes, shells and questions. A slow model request in one does not block the other ticket or its request. | FR-17, FR-20, FR-22, FR-30 |
| AT-08 · Same-ticket write ownership | Attempt competing manual/tool/job writes within one worktree; serialize or pause correctly without blocking another worktree unnecessarily. | FR-23, FR-25, FR-30, FR-42 |
| AT-09 · Team bundle scope | Save/copy/import a global and local team including multiple processes; every step remains attached to the correct owned member. | FR-07, FR-08, FR-39 |
| AT-10 · Dangling member rejection | Delete a referenced member or import a process naming another team member; reject save until explicitly reassigned. | FR-07, FR-08 |
| AT-11 · Manual/default assignment | Manual creation exposes team/process choice; an otherwise authorized automatic ticket uses configured defaults without another configuration step. | FR-08 |
| AT-12 · Frozen definitions | Edit a global team during a run; existing step prompts/assignments retain the frozen bundle while a new run uses the revised bundle. | FR-06, FR-07, FR-38 |
| AT-13 · Exact standard sequence | A model asks to skip requirements, architecture, planning or review; the runner refuses progression and no implementation/commit occurs early. | FR-09, FR-12, FR-29 |
| AT-14 · Handoff coverage | Verification receives step-2 requirements and methods; implementation receives step-4 plan and latest correction findings, not only the prior prose string. | FR-10, FR-41 |
| AT-15 · Fresh step sessions | Each successful step and each retry has a different primary session ID; shared ticket shell/worktree identity remains stable. | FR-11, FR-13, FR-22 |
| AT-16 · Minimal results | Accept status/summary/output with valid enum and bounds; reject malformed output and prose-only success without inventing a verdict. | FR-12, FR-18 |
| AT-17 · Verification correction | Record verification attempt 1 FAIL, implementation attempt 2, verification attempt 2, review. Keep all prior outputs and actual job evidence. | FR-13, FR-35, FR-38 |
| AT-18 · Review correction | CHANGES_REQUIRED returns to implementation then fresh verification and review; even a claimed cosmetic change cannot reuse stale green evidence. | FR-13, FR-29 |
| AT-19 · Question continuity | Pause in an autonomous step, navigate away, restart the UI, answer once, and resume the same attempt/session with correlated tool result. | FR-14, FR-19, FR-42 |
| AT-20 · Resume after planning | Crash after result/next-pointer transaction for step 4; reuse steps 1–4 and proceed to the first incomplete step. | FR-10, FR-35 |
| AT-21 · Interrupted side effect | Crash before/after an edit, command or final commit acknowledgment; reconcile observed state and never automatically duplicate an unknown action. | FR-23, FR-29, FR-35 |
| AT-22 · Default model selection | Change the active runtime model between steps; saved teams stay unchanged, next attempt uses the new model, old attempt identity remains accurate. | FR-16, FR-38 |
| AT-23 · Member override | A reviewer override resolves only for that member; missing override produces an actionable error, not silent fallback or team-wide edits. | FR-16 |
| AT-24 · Local compatible endpoints | Against selected tested LM Studio and llama.cpp configurations, complete a text/tool loop with configurable URL/model and optional key. Record server/model versions. | FR-15, FR-18 |
| AT-25 · No model scheduler | Simultaneous requests from two tickets reach the fake/local endpoint; no per-profile semaphore or UI-selected-ticket gate serializes generation. | FR-17, FR-30 |
| AT-26 · Stream fragmentation | Test calls delivered in one chunk and many fragments, null content, final usage-only chunks, malformed arguments and truncated streams. No partial call executes. | FR-18 |
| AT-27 · Reasoning and missing usage | Returned reasoning is separate; absent reasoning/usage stays absent/unknown. A response-reported model alias is recorded without claiming weights identity. | FR-16, FR-18, FR-38 |
| AT-28 · Allow/Ask/Deny | Full autonomy executes permitted tools; restricted policy asks or denies as selected; blocked tools cannot be invoked by guessing a name. | FR-19 |
| AT-29 · Approval staleness | Change arguments, file hashes or policy after preview; reject old approval and present the new operation rather than applying different content. | FR-19, FR-25, FR-32 |
| AT-30 · Scoped file reads/writes | Permit own global context/project reads and ticket writes; reject writes to original/global roots and reads into unrelated runtime/worktree subtrees. | FR-20 |
| AT-31 · Path aliasing | Test prefix siblings, relative escapes, symlinked parents and symlinks into the original tree. No built-in write follows a path outside the owned workspace. | FR-20, FR-25 |
| AT-32 · CommandGuard known forms | Test cd/pushd, git -C, Git env overrides, redirects, cp/mv and compound commands. Known forbidden destinations are denied even in full autonomy. | FR-21 |
| AT-33 · Guard limitations visible | An indeterminate direct destination reports review/limitation; arbitrary scripts are not falsely described as OS-sandboxed or exhaustively analyzed. | FR-21, FR-38 |
| AT-34 · Persistent shell state | Across steps, change cwd within scope, export a value, activate a venv and define a supported function; subsequent commands observe the same shell state. | FR-22 |
| AT-35 · Shell termination | Terminate/exit the shell; the next action sees state loss and controlled recreation, not a false promise that volatile state persisted after restart. | FR-22, FR-23, FR-35 |
| AT-36 · Job truth and cancellation | Verify actual cwd/exit/signal, concurrent stdout/stderr draining, stdin behavior, partial logs and ticket-specific cancellation; silence is not completion. | FR-23, FR-42 |
| AT-37 · Lexical/path/symbol discovery | Find a literal, regex, filename and Python class/function with ranges/hashes; label approximate references and partial results accurately. | FR-24 |
| AT-38 · Search isolation and freshness | Edits in one worktree appear immediately; caches invalidate by hash and no search accidentally traverses sibling ticket trees or .git/runtime directories. | FR-20, FR-24 |
| AT-39 · Exact edits and manual saves | Reject stale/missing/multiple replacement matches; preserve newline/encoding; manual UI writes use the same service and write lease. | FR-25, FR-42 |
| AT-40 · Skill discovery | Load global/project skills on demand, resolve visible overrides, record hashes and display invalid metadata/name/access errors instead of silently dropping skills. | FR-26, FR-38 |
| AT-41 · MCP pipeline parity | Configure stdio and HTTP test servers; tool/resource results, errors, approvals and denials use the same execution records as native tools. | FR-19, FR-27 |
| AT-42 · MCP scope and failure | Verify ticket-root configuration for local filesystem servers and honest remote-scope labels; a server failure leaves unrelated tools/tickets usable. | FR-20, FR-27, FR-35 |
| AT-43 · Checkpoint invariants | Create/edit/delete files and capture private refs; ticket HEAD, normal index and original checkout remain unchanged, including binary/CRLF/source modes. | FR-28, FR-29 |
| AT-44 · Checkpoint restore | Preview/restore a prior source tree, report excluded data, remove only explicitly selected post-checkpoint files, and preserve unexpected third-state user edits. | FR-25, FR-28, FR-35 |
| AT-45 · Checkpoint crash recovery | Crash after private-ref publish and before DB acknowledgment; reconcile the exact ref/OID and avoid duplicate or false-complete checkpoints. | FR-28, FR-35 |
| AT-46 · Final commit gate | Verify one standard deliverable commit with descriptive message, reviewed paths and expected tree; a hook/filter difference prevents unsupported PASS. | FR-09, FR-29 |
| AT-47 · Clean integration | Merge a fast-forward candidate under each policy; review mode waits, auto mode proceeds only after gates, and Done reflects successful integration. | FR-03, FR-31, FR-32 |
| AT-48 · Diverged/conflicting integration | Normal Git first; AI writes only in candidate worktree, records rationale, reruns checks/review, and preserves both original final commits. | FR-30, FR-31 |
| AT-49 · Semantic conflict without markers | A clean textual merge breaks a requirement; integration checks fail and automatic application is blocked. | FR-31, FR-32 |
| AT-50 · Ambiguous AI resolution | Incompatible requirements or repeated failed fixes produce a user question/review state, not an ours/theirs shortcut or self-confidence-based approval. | FR-14, FR-31, FR-32 |
| AT-51 · Target advance during inspection | Move the target after candidate review; reject stale approval, rebuild/revalidate and obtain new approval in inspection mode. | FR-31, FR-32 |
| AT-52 · Post-merge cleanup | After observed integration, stop owned jobs and remove worktree/private refs; retain branch, step history, prompts and artifacts. | FR-23, FR-33, FR-34 |
| AT-53 · Cleanup exception | Unmerged work, unknown user files or pending writes prevent automatic deletion; cleanup can retry without undoing a completed merge. | FR-33, FR-35 |
| AT-54 · Single-store consistency | Board, attempts, messages and integrations live in one project DB; a second project is independent and no JSONL transcript acts as another source of truth. | FR-34 |
| AT-55 · Backup restore | Restore DB plus referenced artifacts and retained Git checkpoint objects; a DB-only export clearly lacks source/checkpoint restoration. | FR-28, FR-34, FR-39 |
| AT-56 · Code/diff and accessibility | Exercise themes, Unicode, large files, synchronized diff views, resizing, keyboard flow and mixed QML/widget focus with no clipped labels. | FR-25, FR-37, FR-38 |
| AT-57 · Read-only discussion | Inspect original project context and propose a ticket without writing source; only the authorized creation action creates executable work. | FR-20, FR-40 |
| AT-58 · Context compaction | Compact long sessions while retaining exact ticket constraints, requirements/plan, recent correction findings and complete call/result exchanges. | FR-18, FR-41 |
| AT-59 · Cross-ticket event identity | Stream/update/answer/cancel in different visible tickets; events and actions always affect the recorded owner, never the current UI selection. | FR-14, FR-30, FR-38, FR-42 |
| AT-60 · No silent publication | Final commit, automatic merge, Done and cleanup never trigger a remote push or mirror of private checkpoint refs. | FR-29, FR-31, FR-32, FR-33 |

## 23.3 Fault injection

Inject failure immediately before and after each external-effect boundary: worktree creation, step-result commit, question answer, file replacement, command dispatch/exit, private-ref publication, final branch commit, target update, and cleanup deletion. Test repeated user operation IDs and late stream chunks after cancellation. Verify actual state in Git/filesystem, not just expected event counts.

Also test invalid team imports, deleted member/model overrides, server disconnects, stale UI approvals, unavailable checkpoint refs after retention, disk exhaustion, oversized logs, malformed tool JSON and a project database from a newer schema version. Recovery must preserve unrelated tickets and never silently broaden file access or delete unknown contents.

## 23.4 Completion gate

The complete baseline requires all core behaviors to pass their applicable scenarios and every functional requirement to have traceable evidence. A feature explicitly included in scope cannot be relabeled optional just because an earlier increment did not implement it. In particular, skills, MCP, concurrent tickets, the eight-step process, private-ref checkpoints and AI-assisted integration are required.

Document any supported-platform or endpoint limits precisely. UI polish is assessed through interactive review and screenshots in both themes; correctness is assessed through assertions and recorded external outcomes. No demonstration transcript or model-generated PASS replaces verification evidence.


# 24. Package organization, dependencies, and delivery sequence

## 24.1 Cohesive source organization

The following is a proposed module layout, not a requirement to create a class/file for every concept. Related small records can live together. The import boundary between core and interfaces is mandatory; the exact filenames are not.

```text
agilecode/
    core/
        domain.py                 # Ticket, Team, Process, Step contracts
        application.py            # commands and use-case services
        process_runner.py         # order, gates, attempts, correction
        agent_runner.py           # request/tool conversation loop
        context.py                # handoffs, prompts, compaction
        tools.py                  # registry, executor, policy
        workspace.py              # scopes and guarded file service
        shell.py                  # persistent shell + CommandGuard
        git.py                    # worktrees, final commit, checkpoints
        integration.py            # MergeCoordinator and AI candidates
        store.py                  # SQLite repositories/transactions
        model.py                  # one compatible client adapter
        search.py                 # lexical/path/symbol discovery
        skills.py
        mcp.py
        contracts.py              # commands, events, read models
    interfaces/
        desktop/
            bridge.py
            models.py             # Qt list/detail models
            host.py               # QML host and optional widget interop
            code_components.py
            qml/
                Main.qml
                Board.qml
                TicketWorkspace.qml
                ProcessTimeline.qml
                Conversation.qml
                TeamEditor.qml
                IntegrationReview.qml
                Theme.qml
        headless.py               # same core, no Qt dependency
    resources/
        teams/                    # bundled teams include processes
        skills/
        icons/
    __main__.py

tests/
    unit/
    integration/
    desktop/
    fixtures/
pyproject.toml
```

There is no independent workflows/process library outside teams, no provider family package, no ModelScheduler, and no required frontend server. Split a growing file into a cohesive package when implementation size warrants it, not preemptively into dozens of service abstractions.

## 24.2 Dependency budget

The proposed initial source environment is CPython 3.13 on Linux in a normal virtual environment, consistent with the previous specification's delivery baseline. Pin tested versions in implementation; do not claim every Python/OS combination works without installation and behavior tests.

Use PySide6 for the optional desktop package, an asynchronous HTTP transport, input/config validators, the official MCP Python SDK, and syntax-highlighting/parser support where useful. Python supplies asyncio, subprocess interfaces, SQLite access, hashes, filesystem utilities, and logging. Git is an explicit executable dependency. A safe YAML reader is needed for SKILL.md frontmatter if not provided by the chosen parser; team/process serialization can remain JSON.

The headless installation must not require importing/installing GUI resources to execute its tests and core entry point. MCP client support is part of the complete feature set, not deferred; user-selected external servers may bring their own runtimes. Lexical search can optionally accelerate with ripgrep without making it a core architecture dependency. There is no required bubblewrap, vector server, Node frontend toolchain, ORM, or cloud SDK family.

## 24.3 Composition and lifecycle

The composition root constructs one core application with project/store services, model transport, tool executor, shell/Git/search/skill/MCP adapters, process runner, and merge coordinator. Desktop startup adds the QtBridge and QML host; headless startup attaches a non-Qt command/event consumer.

Open projects acquire a writable owner lock, initialize the store and recovery view, load team bundles and effective settings, and publish a BoardSnapshot. The model server need not be reachable just to browse tickets. Closing follows the engine's explicit shutdown policy, not an arbitrary window destructor terminating jobs.

Model selection is runtime configuration. Tool restrictions and merge/cleanup policy are user configuration. Process steps and member identity are frozen run definitions. Keeping those responsibilities separate avoids reintroducing the “global mode/current task” coupling the redesign is intended to remove.

## 24.4 Implementation increments

| Increment | Deliverable | Exit evidence |
| --- | --- | --- |
| I-1: core and presentation contracts | Domain, team-owned processes, commands/read models, SQLite skeleton, QML-first board/code/diff prototype. | Headless import/run tests; visual/focus/board behavior tests. |
| I-2: workspace and shell | Ticket worktrees from active branch, scoped files, persistent shell, CommandGuard, operation journal. | Multiple-worktree isolation; persistence of shell state; forbidden command fixtures. |
| I-3: agent loop and search | Single local-compatible client, streaming assembler, common tools, strong lexical/path/symbol search, controls. | LM Studio/llama.cpp fixtures, concurrent requests, exact edits, questions and tool denial. |
| I-4: full standard process | Eight Step objects, member binding, context handoffs, attempt history, correction loops, global/local team editor, skills. | No-skip tests; resume after step 4; fresh sessions; repeated verify/review attempts. |
| I-5: MCP and checkpoints | MCP client/catalog/policy, Git-backed private-ref checkpoints, guarded restoration. | Tool/result parity; server scope tests; unchanged normal Git index/history during capture. |
| I-6: final commit and integration | Step-8 commit, MergeCoordinator, AI conflict candidates, auto/inspect toggle, post-merge cleanup. | Clean/diverged/conflicting/semantic merges; stale approval rejection; retained history/branch. |
| I-7: complete baseline | End-to-end UI wiring, recovery/backup/export, performance and accessibility, install documentation. | All core acceptance scenarios, with recorded limitations and no silent feature deferrals. |

This is implementation ordering, not a schedule or an invitation to drop later increments from v1 without a scope decision. The first complete implementation includes skills, MCP, concurrent tickets, and AI-assisted integration as agreed. Embedding-based semantic search remains a future extension.

## 24.5 Packaging and source provenance

Before distribution, inventory the actual Qt modules, Python/native dependencies, bundled assets, and licenses. Qt for Python publishes component licensing information, but packaging choices and obligations require review of the exact shipped artifacts. [20]

Expected Qt modules include QtCore, QtGui, QtQml, QtQuick, QtQuickControls2 as used, and QtWidgets/QtQuickWidgets only for the selected host/components. QtWebEngine and Monaco are not introduced by default. Neither Spyder nor JEDITOR source is assumed copied.

The project license and binary-packaging tool remain release implementation choices, not blockers that require reopening settled product architecture. No Windows/macOS release pipeline, remote service, or existing AgileCode/Zoo migration is silently added to the scope.


# 25. Decision record, superseded proposals, and risks

## 25.1 Architectural decision record

| ADR | Decision in v1.1 | Consequence |
| --- | --- | --- |
| ADR-01 | Clean-slate Python functionality; no Roo/Cline dependency. | Reimplement useful behavior with explicit ownership and narrow adapters. |
| ADR-02 | One compatible local-model client. | LM Studio/llama.cpp first; ordinary errors, no inference scheduler. |
| ADR-03 | Headless core plus QML-first PySide6 frontend. | UI replacement does not change process/tools/persistence; Widgets selective. |
| ADR-04 | Team owns members and processes; Process owns ordered Steps. | Save coherent team bundles globally or locally; no detached process library. |
| ADR-05 | Eight-step standard sequence enforced by code. | Prompts are step inputs, not the mechanism guaranteeing order. |
| ADR-06 | Fresh session and durable result per attempt. | Focused handoffs, immediate persistence, recoverable resume and visible retries. |
| ADR-07 | Runtime model default, optional member override. | Model updates do not require editing every team. |
| ADR-08 | One SQLite database per project plus file artifacts. | All related structured state remains transactional and queryable. |
| ADR-09 | Concurrent tickets on separate nested worktrees. | Short mutation locks only; stable ticket bases until integration. |
| ADR-10 | Real ticket shell with CommandGuard. | Persistent state with ordinary workflow protection, not OS sandboxing. |
| ADR-11 | Private-ref Git checkpoints and final step-8 branch commit. | Recovery history is separate from deliverable history. |
| ADR-12 | MergeCoordinator with AI-assisted verified candidates. | Original checkout untouched during candidate resolution; merge policy user-controlled. |
| ADR-13 | Four board states with internal process/integration statuses. | No extra Review/Verification/Integrating columns. |
| ADR-14 | User tool policy and same-session questions. | Allow/Ask/Deny is distinct from questions and merge approval. |
| ADR-15 | Skills, MCP, and strong code discovery are core. | Vector/embedding retrieval is deferred without weakening basic navigation. |
| ADR-16 | Automatic post-integration cleanup defaults. | Remove worktree/checkpoint refs; keep branch/database history; exceptions block unsafe deletion. |

## 25.2 Superseded material removed from v1.0

The following earlier assumptions no longer apply: all-Widgets presentation; forbidding QML to preserve Python-only UI source; one executing ticket per project; per-endpoint model semaphores; mandatory fixed model assignments for all members; detached global process definitions; an analysis stage that silently combines/omits standard steps; a giant conversation carried across the process; per-ticket JSONL execution files; Python file-manifest checkpoint storage; optional/deferred MCP; broad global-home reads; mandatory external shell sandboxes; mandatory manual acceptance before every merge; and a Done ticket awaiting its first integration under the standard policy.

The retained implementation safeguards are not rejected architecture: stable IDs, short transactions, file hashes, ordinary tool argument validation, consistent conversation records, explicit commit/ref identities, and recovery after uncertain effects. They support the agreed model without adding user-facing workflow complexity.

## 25.3 Risk register

| Risk | Consequence | Design response |
| --- | --- | --- |
| Local server/model cannot produce usable native calls. | A step cannot use its required tools. | Minimal endpoint test/clear failure; no silent prose execution or hosted fallback. |
| QML/Widgets interop falls short visually. | Inconsistent focus, sizing, styling or responsiveness. | Test the actual selected composition early; keep CodeDocument/UI bridge independent. |
| Shell hides writes inside arbitrary scripts. | CommandGuard misses an indirect off-worktree effect. | Honest workflow-guard scope; block recognized escapes and inspect actual cwd; no sandbox claim. |
| Read-only project prefix exposes nested ticket data. | Cross-ticket context leakage or wrong-file reads. | Explicit exclusions and owned-worktree precedence before broad root checks. |
| Concurrent integration invalidates a candidate. | Stale approval or incorrect target update. | Per-target coordination plus target/candidate rechecks and revalidation. |
| Git merges cleanly but breaks behavior. | Silent semantic regression. | Integration checks even without textual conflicts; preserve ticket requirements. |
| AI resolution guesses incompatible intent. | Incorrect combined code. | Candidate-only writes, retained rationale, validation/review, pause for ambiguity. |
| Git hooks/filters alter a reviewed commit. | Final tree differs from the checked snapshot. | Verify the actual committed tree and stop on mismatch; no hidden reset. |
| Capture touches normal index or follows external paths. | Polluted staging or checkpoint of the wrong data. | Private index, raw Git blobs, path/mode checks, explicit tests. |
| Crash leaves an unknown external effect. | Duplicate command/commit or misleading success. | Intent/observation journal; reconcile, never infer nonexecution from missing result. |
| Global team edit changes active execution. | Nonreproducible prompts/roles. | Freeze bundle per run; resolve only runtime model defaults at declared attempt boundaries. |
| Cleanup encounters unreviewed data. | Lost user files. | Verify identities/content, stop jobs, block deletion rather than force it. |

## 25.4 Remaining implementation validation

No additional major product/architecture clarification is required before implementation against this baseline. Engineers still need to test the chosen Qt composition, shell framing, endpoint behavior, Git recovery sequences, search performance, and packaging. These are empirical implementation tasks, not reasons to ask the user again whether to use QML, one database, concurrent tickets, or the agreed process.

Numeric performance targets, default timeouts, schema column details, and class/module names may be refined with evidence while preserving the confirmed behavior. Any genuine scope change must be labeled as a change rather than introduced as a supposedly unresolved decision.


# 26. End-to-end reference execution

## 26.1 Example setup

The original project is MyProject on branch feature/imports at base B. The user has a global Development Team containing Analyst, Architect, Programmer, Tester and Reviewer, with Standard Development as its default process. All members use the runtime active model; the Reviewer could have an optional override, but none is required.

Ticket AC-0042 requests a filename-filter fix and regression coverage. AC-0043 changes adjacent import behavior. Both use project-local database records and separate worktrees beneath MyProject/.agilecode/worktrees/. The examples are intended acceptance scenarios, not claims of completed implementation.

## 26.2 Standard process

The user starts AC-0042 by dragging Ready to In Progress. RunCoordinator snapshots the ticket/team/process, records branch feature/imports and commit B, creates the ticket branch/worktree, opens its persistent shell, and captures an internal baseline checkpoint.

Step 1 creates an Analyst session to understand the ticket. Step 2 creates a new Analyst session using the understanding plus ticket and states requirements R1–R3 with verification methods. Step 3 creates an Architect session to identify necessary code changes. Step 4 uses another fresh session to produce the ordered plan. Each completed output is persisted before the next step starts.

Step 5 creates the Programmer session and implements in the ticket worktree. Its shell environment persists when it activates a local virtualenv and later runs commands. A command attempting to copy output into the original project is rejected by CommandGuard. A permitted exact edit verifies the current file hash and records its result through the common tool pipeline.

Step 6 creates the Tester session and evaluates R1–R3. One regression fails, producing a completed FAIL result with actual command output and exit status. The runner creates implementation attempt 2 with the plan, current worktree and failure report. New verification passes. A fresh Reviewer session for step 7 inspects the full diff and evidence and returns PASS.

Step 8 prepares a message describing the filter fix and verified behavior. The engine checks current evidence, stages only reviewed source, creates final ticket commit F42 on the ticket branch, verifies the actual result, and persists PASS. Earlier checkpoints remain under private refs rather than appearing as WIP commits in that branch's normal history.

## 26.3 Questions, model changes, and restart

If the Analyst asks how an ambiguous file suffix should behave, the same step attempt/session waits. The user's answer resolves its pending question and resumes it; no new attempt is created solely because a question was asked.

Changing the runtime active model before the next step does not require editing the saved team. The next attempt resolves and records the new model. Previous attempts retain their actual model/request history. An in-flight request does not silently change servers.

If the application crashes after step 4, startup reconciles the worktree and side effects, then resumes at implementation using the stored outputs. It does not regenerate requirements or pretend the previous shell process is still alive. A replacement shell's lost volatile state is visible.

## 26.4 Concurrent ticket and merge handling

AC-0043 can run at the same time and issue its own model requests. The local server handles inference scheduling. AgileCode coordinates only its database writes and actual shared mutations. Its tools and shell cannot target AC-0042's worktree.

Suppose AC-0043 integrates first, moving the target from B to T. AC-0042 continues its stable process without a mid-run rebase. After F42 completes, MergeCoordinator prepares a candidate combining T and F42 in the integration worktree. Normal Git merge runs first.

If there is a textual conflict, a fresh AI resolution session receives base/target/ticket context, both available requirement sets, conflict files and tests. Its changes remain in the candidate. Verification/review determine whether the candidate is acceptable. A clean textual merge still receives relevant integration checks to catch behavioral interaction.

## 26.5 Automatic versus inspection policy

With Merge on completion, the coordinator applies the candidate when all checks and destination preconditions pass. With inspection enabled, it waits while showing the exact candidate and evidence. The ticket remains In Progress with an internal review badge. A target update during inspection invalidates the old candidate and requires revalidation and renewed approval.

Successful application is observed and persisted, then AC-0042 moves to Done. Default cleanup stops the ticket shell/jobs, removes the ticket worktree and hidden checkpoint refs, retains the ticket branch, and keeps process/attempt/conversation history in SQLite. Cleanup blocks rather than deletes unexpected user changes.

An unresolved conflict, failed integration test, or unknown target update leaves the ticket recoverable and unintegrated. Nothing is pushed to a remote automatically, and no alternative UI would change these core behaviors.


# Appendix A. Reference Python contracts

## A.1 Domain and executable step interface

The following code is a typed design reference, not the delivered application. Saved definitions contain only names, IDs, prompts, members and processes. The executor shown here is injected by the composition root when binding definitions to a run; it is never imported from user configuration or serialized with a team.

```python
from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID


class TicketState(StrEnum):
    BACKLOG = "backlog"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class StepStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    CHANGES_REQUIRED = "changes_required"


@dataclass(frozen=True, slots=True)
class StepResult:
    status: StepStatus
    summary: str
    output: str


@dataclass(frozen=True, slots=True)
class TeamMember:
    id: str
    name: str
    system_prompt: str
    allowed_tools: frozenset[str]
    skills: tuple[str, ...] = ()
    model_override: str | None = None


@dataclass(frozen=True, slots=True)
class TicketSnapshot:
    id: UUID
    key: str
    revision: int
    objective: str
    scope: str
    constraints: str


@dataclass(frozen=True, slots=True)
class StepContext:
    run_id: UUID
    attempt_id: UUID
    ticket: TicketSnapshot
    workspace: Path
    outputs: Mapping[str, StepResult]
    previous_output: str | None = None
    correction_report: str | None = None


class StepExecutor(Protocol):
    async def execute(
        self, step: Step, context: StepContext
    ) -> StepResult: ...


@dataclass(frozen=True, slots=True)
class Step:
    id: str
    name: str
    team_member_id: str
    prompt: str
    _executor: StepExecutor = field(repr=False, compare=False)

    async def run(self, context: StepContext) -> StepResult:
        return await self._executor.execute(self, context)


@dataclass(frozen=True, slots=True)
class Process:
    id: str
    name: str
    steps: tuple[Step, ...]


@dataclass(frozen=True, slots=True)
class Team:
    id: str
    name: str
    members: tuple[TeamMember, ...]
    processes: tuple[Process, ...]
    default_process_id: str
```

The ProcessRunner creates a run-bound set of Step instances and supplies its headless executor. Validate each team_member_id against that Team before execution. The executor resolves the member snapshot and attempt model, creates the fresh session, and runs the request/tool loop. ProcessRunner persists and validates the returned result before advancing.

Mapping/tuple annotations express the intended immutable handoff. Implementations must actually copy/freeze mutable backing dictionaries; a frozen dataclass alone does not make a nested dictionary immutable. Serialization must explicitly omit _executor and never use pickle to save/import these runtime objects.

## A.2 Standard ordering and correction map

```python
STANDARD_STEP_IDS = (
    "understand",
    "requirements",
    "architecture",
    "plan",
    "implement",
    "verify",
    "review",
    "commit",
)

STANDARD_CORRECTIONS = {
    ("verify", StepStatus.FAIL): "implement",
    ("review", StepStatus.CHANGES_REQUIRED): "implement",
}
```

The normal successor is the next ordered step, accepted only on PASS and a satisfied gate. After returning to implement, the normal successors are verify and review again. A result does not contain a next_step field. In the standard process, the ordered IDs are validated before start; user-created custom processes are separate deliberate definitions.

The runner's durable control sequence is:

```text
load next incomplete step and current run snapshot
    -> reconcile unresolved external operations
    -> create a new StepAttempt (or resume a known interrupted one)
    -> resolve member override OR runtime active model
    -> record member/model/session identity
    -> construct StepContext with applicable earlier outputs
    -> await step.run(context)
    -> validate StepResult and current evidence
    -> transaction: save result + next/correction pointer
    -> publish StepAttemptCompleted
    -> advance, correct, pause, or finish the development process
```

A question suspends inside step execution and resumes that same attempt/session. A verification/review correction returns a completed result and produces a new implementation attempt. These paths must not be conflated.

## A.3 Headless application boundary

```python
@dataclass(frozen=True, slots=True)
class CommandReceipt:
    operation_id: UUID
    accepted: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ApplicationEvent:
    project_id: UUID
    ticket_id: UUID | None
    run_id: UUID | None
    attempt_id: UUID | None
    kind: str
    sequence: int
    payload: Mapping[str, object]


class AgileCodeService(Protocol):
    async def start_ticket(
        self, ticket_id: UUID, expected_revision: int,
        operation_id: UUID
    ) -> CommandReceipt: ...

    async def pause_run(self, run_id: UUID) -> CommandReceipt: ...

    async def answer_question(
        self, question_id: UUID, answer: str, operation_id: UUID
    ) -> CommandReceipt: ...

    async def resolve_approval(
        self, approval_id: UUID, decision: str,
        expected_revision: int, operation_id: UUID
    ) -> CommandReceipt: ...

    async def approve_integration(
        self, integration_id: UUID, candidate_oid: str,
        expected_target_oid: str, operation_id: UUID
    ) -> CommandReceipt: ...

    def events(self) -> AsyncIterator[ApplicationEvent]: ...
```

Production command types validate enum decisions and include any additional owner/revision information required by the operation. Read-model methods are omitted here for space but form the other half of the boundary. No interface exposes a QWidget, QObject, raw database connection, shell PID chosen by the model, or mutable global current ticket.

## A.4 Tool and result separation

StepResult describes a process artifact/verdict. ToolResult describes one operation's execution. A tool's successful edit or exit code does not automatically produce a step PASS, and a completed verification step can validly return FAIL. The common executor records each operation and exposes evidence to the step gate without making every tool aware of the entire process.


# Appendix B. Persistence constraints and external-operation recipes

## B.1 Representative SQL constraints

This executable SQL fragment illustrates enforcement, not the complete migration/schema. The logical schema is in Section 18. Team-internal JSON references, process gates, workspace ownership, and state-transition rules also require application validation inside their transactions.

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE tickets (
    id TEXT PRIMARY KEY,
    display_key TEXT NOT NULL UNIQUE,
    board_state TEXT NOT NULL CHECK (
        board_state IN ('backlog', 'ready', 'in_progress', 'done')
    ),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    parent_id TEXT REFERENCES tickets(id),
    CHECK (parent_id IS NULL OR parent_id <> id)
);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    state TEXT NOT NULL,
    current_step_id TEXT,
    definition_json TEXT NOT NULL CHECK (json_valid(definition_json)),
    final_commit_oid TEXT
);

CREATE UNIQUE INDEX one_nonterminal_run_per_ticket
ON runs(ticket_id)
WHERE state NOT IN ('succeeded', 'failed', 'cancelled');

CREATE TABLE step_attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    step_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    state TEXT NOT NULL,
    member_json TEXT NOT NULL CHECK (json_valid(member_json)),
    model_json TEXT NOT NULL CHECK (json_valid(model_json)),
    result_json TEXT CHECK (
        result_json IS NULL OR json_valid(result_json)
    ),
    workspace_fingerprint TEXT,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE (run_id, step_id, attempt_number),
    CHECK (state <> 'completed' OR result_json IS NOT NULL)
);

CREATE TABLE operations (
    id TEXT PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    run_id TEXT REFERENCES runs(id),
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    intent_json TEXT NOT NULL CHECK (json_valid(intent_json)),
    outcome_json TEXT CHECK (
        outcome_json IS NULL OR json_valid(outcome_json)
    )
);

CREATE TABLE checkpoint_refs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    ref_name TEXT NOT NULL UNIQUE,
    commit_oid TEXT NOT NULL,
    tree_oid TEXT NOT NULL,
    history_boundary_json TEXT NOT NULL
        CHECK (json_valid(history_boundary_json)),
    retention_state TEXT NOT NULL
);
```

The final implementation may call checkpoint_refs simply checkpoints; no parallel authoritative checkpoint table is intended. Complete the remaining tables and foreign keys from Section 18. Parent/session coherence is enforced by composite references or equivalent transactional checks, not by a parent-ID foreign key alone.

A run in process_complete still reserves its ticket while integration is pending. The run becomes succeeded when integration is recorded; the board state becomes Done. There is deliberately no unique index limiting an entire project to one running ticket.

## B.2 Result and resumption transaction

The accepted result, completion timestamp, evidence references, and next/correction pointer are one atomic store operation. Publish the completion event only after commit. A crash after commit but before notification leaves a recoverable completed step; a replacement UI reconstructs it from the store.

A pending question similarly binds answer and resumption to the same attempt/session. Do not create a new question or attempt when replaying an already committed answer. New correction attempts increment attempt_number and keep previous StepResult data immutable.

## B.3 Checkpoint Git recipe

Within the ticket write lease, create a new temporary index path outside the ordinary Git index and use GIT_INDEX_FILE only in the checkpoint subprocess environment. Initialize that index empty. For each eligible validated source path, write the captured raw bytes as a Git blob, then insert the path/mode/blob OID into the private index. NUL-safe file records and argument arrays are required for unusual filenames. [35, 36, 37]

Write the tree, create its commit with the recorded previous checkpoint/base parent, and publish refs/agilecode/checkpoints/<ticket-uuid>/<checkpoint-uuid> with expected absence. Verify ticket HEAD and normal-index identity are unchanged. Record the exact private ref/commit/tree and recovery boundary in the database. [32–34]

Do not use the ticket's normal staging area, commit on its branch, repoint HEAD, or create a second shadow repository. Do not pass model-authored strings as an index path or ref name. Capture coverage and excluded files remain explicit in the DB metadata.

A failed ref publication leaves unreferenced Git objects eligible for later normal maintenance; it does not authorize changing an existing checkpoint ref. A crash after successful publication is reconciled by the operation's intended ref and exact object identities.

## B.4 Final commit and integration ordering

```text
FINAL TICKET COMMIT
current requirements + passed verification + passed review
    -> settle writes and validate worktree/branch/fingerprint
    -> prepare durable commit intent
    -> stage only reviewed eligible source
    -> create descriptive commit on ticket branch
    -> inspect actual parent/tree/status
    -> persist final commit + step-8 PASS

INTEGRATION
freeze current target OID + final ticket OID
    -> ordinary Git candidate in integration worktree
    -> AI candidate resolution only when needed
    -> verification + resolution review
    -> user inspection OR configured auto-merge authorization
    -> recheck target, candidate, destination cleanliness
    -> apply verified candidate without force
    -> observe actual ref AND checkout
    -> persist integrated + ticket Done
    -> run configured cleanup
```

A changed target invalidates the candidate's previous evidence/approval applicability. Cleanup is not part of the Git update transaction and can fail independently. No step in either recipe silently pushes a branch or mirrors checkpoint refs to a remote.

## B.5 Outcome certainty

Database request keys prevent duplicate application submissions; request-scoped tool IDs prevent duplicate dispatch in known histories. They do not prove exactly-once side effects across process crashes. Git operations use ref/tree/parent observations; file operations use hashes; shell and remote MCP operations may remain Unknown.

The enforced recovery promise is: never knowingly redispatch a recorded side-effecting action merely because its result notification is missing. Reconcile or obtain a distinct user-authorized retry, and retain both the original uncertainty and any new attempt.


# Appendix C. Sources and reference provenance

## C.1 Source use and revision provenance

The source baseline is the latest 65-page AgileCode Software Design Specification v1.0 DOCX supplied in this conversation. Version 1.1 integrates the subsequently confirmed design decisions throughout the requirements, object model, interfaces, lifecycle, UI, persistence, Git operations, and acceptance plan. Appendix D identifies the decisions and where they are implemented in this specification. Earlier conflicting proposals are superseded, not retained as alternative requirements.

External sources establish library and protocol behavior. They do not prove that AgileCode or its performance targets have been implemented. References 1–4 preserve the pinned Zoo Code source snapshot used in the original analysis; they are not a claim about current upstream code or a dependency. References 5–28 retain the original technical bibliography. The updated Qt Quick, local-server, Git checkpoint, skill, and client documentation was consulted for this revision on 22 September 2026. Implementers must pin and validate the dependency versions they actually ship.

Product behavior, ordering, approval policy, ownership, and cleanup semantics are requirements or design choices in this document, not claims made by the cited libraries. Code and SQL examples are design contracts or representative mechanisms, not a delivered implementation. The long-term runtime claims in this document are acceptance obligations, not measured results.

## C.2 Reference list

[1] Zoo Code, TaskRegistry.ts, commit 77e422faf56afe32e10236e4aaf129a8ea2cff53. Reference for the legacy stack/map migration boundary. https://github.com/Zoo-Code-Org/Zoo-Code/blob/77e422faf56afe32e10236e4aaf129a8ea2cff53/src/core/task/TaskRegistry.ts

[2] Zoo Code, Task.ts, same pinned commit. Behavioral reference for the existing agent task and its integration surface. https://github.com/Zoo-Code-Org/Zoo-Code/blob/77e422faf56afe32e10236e4aaf129a8ea2cff53/src/core/task/Task.ts

[3] Zoo Code, BaseTool.ts, same pinned commit. Reference for typed tool execution and task coupling. https://github.com/Zoo-Code-Org/Zoo-Code/blob/77e422faf56afe32e10236e4aaf129a8ea2cff53/src/core/tools/BaseTool.ts

[4] Zoo Code, NewTaskTool.ts, same pinned commit. Reference for delegated-task behavior; not a Python implementation dependency. https://github.com/Zoo-Code-Org/Zoo-Code/blob/77e422faf56afe32e10236e4aaf129a8ea2cff53/src/core/tools/NewTaskTool.ts

[5] Qt for Python, QPlainTextEdit. https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QPlainTextEdit.html

[6] Qt for Python, QSyntaxHighlighter. https://doc.qt.io/qtforpython-6/PySide6/QtGui/QSyntaxHighlighter.html

[7] Qt for Python, QThread. https://doc.qt.io/qtforpython-6/PySide6/QtCore/QThread.html

[8] OpenAI, Chat API reference. https://developers.openai.com/api/reference/resources/chat

[9] OpenAI, Function calling guide. https://developers.openai.com/api/docs/guides/function-calling

[10] LM Studio, OpenAI-compatible endpoints. https://lmstudio.ai/docs/developer/openai-compat

[11] HTTPX, Async support. https://www.python-httpx.org/async/

[12] Python documentation, Coroutines and tasks. https://docs.python.org/3/library/asyncio-task.html

[13] Python documentation, Asyncio subprocesses. https://docs.python.org/3/library/asyncio-subprocess.html

[14] Git documentation, git-worktree. https://git-scm.com/docs/git-worktree

[15] Git documentation, git-status. https://git-scm.com/docs/git-status

[16] Git documentation, git-diff. https://git-scm.com/docs/git-diff

[17] SQLite, Write-Ahead Logging. https://sqlite.org/wal.html

[18] SQLite, Foreign Key Support. https://sqlite.org/foreignkeys.html

[19] SQLite, Online Backup API. https://sqlite.org/backup.html

[20] Qt for Python, Licenses Used in Qt for Python. https://doc.qt.io/qtforpython-6/licenses.html

[21] Pygments, Introduction and Quickstart. https://pygments.org/docs/quickstart/

[22] MCP Python SDK, The Client. https://py.sdk.modelcontextprotocol.io/client/

[23] Agent Skills, Specification. https://agentskills.io/specification

[24] Python keyring library documentation. https://keyring.readthedocs.io/en/latest/

[25] Python documentation, sqlite3. https://docs.python.org/3/library/sqlite3.html

[26] Git documentation, git-merge. https://git-scm.com/docs/git-merge

[27] Python documentation, os module and descriptor-relative operations. https://docs.python.org/3/library/os.html

[28] W3C, Understanding WCAG 2.2 Success Criterion 1.4.3: Contrast (Minimum). https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html


[29] Qt, Exposing C++ Models to QML (model/view contracts also used through Qt for Python). https://doc.qt.io/qt-6/qtquick-modelviewsdata-cppmodels.html

[30] Qt for Python, QQuickWidget. Qt Quick content inside a QWidget host; includes integration and rendering limitations. https://doc.qt.io/qtforpython-6/PySide6/QtQuickWidgets/QQuickWidget.html

[31] llama.cpp, HTTP server documentation. Local OpenAI-compatible serving, streaming, and model/template-dependent tool use. https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md

[32] Git documentation, git-commit-tree. Create a commit object without advancing a normal branch. https://git-scm.com/docs/git-commit-tree

[33] Git documentation, git-write-tree. Create a tree object from an index. https://git-scm.com/docs/git-write-tree

[34] Git documentation, git-update-ref. Checked ref updates and ref transactions. https://git-scm.com/docs/git-update-ref

[35] Git documentation, git-hash-object. Raw object creation and no-filter semantics. https://git-scm.com/docs/git-hash-object

[36] Git documentation, git-read-tree. Populate a private index, including an empty tree. https://git-scm.com/docs/git-read-tree

[37] Git documentation, git-update-index. Populate index entries from explicit mode/object/path records. https://git-scm.com/docs/git-update-index


# Appendix D. Confirmed decision traceability and revision log

## D.1 Decision-to-design map

This map records the decisions accepted during the design discussion. It is not a second alternative design. The main sections implement these decisions, and the acceptance references specify how the implementation will demonstrate them.

| Decision | Agreed direction | Implementation consequence | Sections | Acceptance |
| --- | --- | --- | --- | --- |
| D-01 | Headless Python core and replaceable UI | No functional behavior in Qt/QML; common commands/read models/events. | 5, 20, A | AT-01,02 |
| D-02 | PySide6, QML primary, Widgets selective | Replace all-Widgets assumption; test actual interop rather than reopening toolkit choice. | 20, 24 | AT-56 |
| D-03 | LM Studio/llama.cpp compatible client only | One Chat Completions path; no other provider families or capability framework. | 10 | AT-24,26 |
| D-04 | Provider owns inference concurrency | Remove ModelScheduler and per-model request semaphore/settings. | 7, 10 | AT-07,25 |
| D-05 | Active runtime model with optional member override | Resolve on attempt start; do not edit every team to change the loaded/selected model. | 6, 10, 21 | AT-22,23 |
| D-06 | Record actual model used | Keep requested/reported identity and options with attempt/request history. | 6, 10, 18 | AT-22,27 |
| D-07 | One project-local database | Structured state stays together; no per-ticket DB/JSONL decomposition. | 15, 18 | AT-54 |
| D-08 | Files remain files | Source in Git; large artifacts on disk; DB references are authoritative bookkeeping. | 15, 18 | AT-52,55 |
| D-09 | Concurrent tickets allowed | Separate worktree, shell, session and question identities per ticket. | 7, 15 | AT-07,08,59 |
| D-10 | Four board columns | Detailed process/integration state is internal, not new Kanban columns. | 4, 7, 20 | AT-03,47 |
| D-11 | Eight standard steps in fixed order | Understand, requirements, architecture, plan, implement, verify, review, commit. | 8 | AT-13,46 |
| D-12 | Executable Step abstraction | Member reference + prompt + run(context); Process is an ordered Step list/class. | 6, 8, A | AT-13,16 |
| D-13 | Context includes prior outputs | Preserve requirements/plan/findings across later steps, not just the previous string. | 6, 8, 11 | AT-14,58 |
| D-14 | Completed steps persist immediately | Commit output and next-step pointer before progressing; resume incomplete work. | 8, 18, 22 | AT-20,21 |
| D-15 | Member role separated from step prompt | Member describes role/tools/skills; step describes current responsibility. | 6, 8, 21 | AT-12,14 |
| D-16 | Fresh session per step | Every new attempt has a new primary session; shared worktree/shell remain. | 8, 9 | AT-15 |
| D-17 | Minimal structured StepResult | status, summary, output; objective evidence remains attempt metadata. | 6, 8, A | AT-16 |
| D-18 | Separate persisted correction attempts | Verification/review findings cause implement -> verify -> review; retain previous attempts. | 7, 8 | AT-17,18 |
| D-19 | Step 8 final commit in ticket branch | Descriptive reviewed commit; integration is separate afterward. | 8, 17 | AT-46,47 |
| D-20 | No standard intermediate branch commits | Checkpoints are internal recovery, not WIP deliverable history. | 15–17 | AT-43,46 |
| D-21 | Hidden Git refs for checkpoints | Git commit/tree objects with private index; no shadow repo or custom file-manifest store. | 16, B | AT-43,44,45 |
| D-22 | AI-assisted merge conflict management | Normal Git first, scoped candidate resolution, validation/review, human pause for ambiguity. | 17 | AT-48,49,50 |
| D-23 | User toggle for merge or inspection | Project policy with ticket override; exact candidate/target approval. | 4, 7, 17 | AT-47,51 |
| D-24 | Team owns processes | Processes name members and travel inside the saved team, not a detached library. | 6, 8, 21 | AT-09,10 |
| D-25 | Global/local team saving | Whole team bundle saved globally or in the project via an explicit scope toggle. | 21 | AT-09,12 |
| D-26 | Global scope is own home settings directory | Read approved ~/.agilecode resources, not the entire home/disk. | 15, 19 | AT-30,31 |
| D-27 | Project reads; own worktree writes | Default code reads use own workspace; exclude sibling/runtime trees. | 13, 19 | AT-30,31,38 |
| D-28 | Real persistent shell | Ticket/worktree-owned shell state survives normal commands and step transitions. | 14 | AT-34,35,36 |
| D-29 | Command guard, not mandatory outside sandbox | Deny recognizable directory/write escapes; disclose indirect script limitations. | 14, 19 | AT-32,33 |
| D-30 | Worktrees under original project | <project>/.agilecode/worktrees/<ticket>/; one ignore entry for managed data. | 15 | AT-06 |
| D-31 | Start from active branch and exact commit | Resolve at execution start; no main/master assumption. | 15 | AT-05 |
| D-32 | Stable ticket base until integration | No automatic rebase while executing; merge against current target afterward. | 7, 15, 17 | AT-07,48,51 |
| D-33 | User determines autonomy/tools | Allow, Ask or Deny; full autonomy does not bypass invariants. | 12, 19 | AT-19,28,29 |
| D-34 | Pause for questions | Resume the exact asking step/session; do not allocate a correction attempt for an answer. | 9, 22 | AT-19 |
| D-35 | Manual assignment or defaults | Manual creation exposes team/process; otherwise configured default pair. | 4, 21 | AT-11 |
| D-36 | Skills definitely in first implementation | Global/project metadata, on-demand loading, clear invalid/discovery states. | 11, 21 | AT-40 |
| D-37 | MCP in first implementation | Configured client transports, tool/resource discovery, common policy/results. | 12, 19, 21 | AT-41,42 |
| D-38 | Strong code search now; embeddings later | Lexical/regex/path/symbol discovery; clean search contract, no mandatory vector pipeline. | 13, 21 | AT-37,38 |
| D-39 | Post-merge cleanup defaults | Remove worktree/checkpoint refs; keep ticket branch and all DB history. | 15–18 | AT-52,53 |
| D-40 | Do not reopen settled design questions | Remaining component tests and implementation choices are engineering work, not repeated product clarification. | 1, 25 | Document consistency audit |

## D.2 Revision summary

Version 1.1 revises the architecture, object model, process contracts, session/attempt lifecycle, database schema, global/local team configuration, model behavior, shell guarding, source scopes, checkpoint implementation, merge policy, QML UI design, feature scope, cleanup, implementation sequence and acceptance plan. Superseded v1.0 defaults are removed rather than retained as unresolved choices.

The original v1.0 files are preserved as the prior edition. The v1.1 Word, PDF and Markdown files contain the same revised specification. Numeric requirements/test IDs belong to this edition; use this decision map to trace the discussion rather than relying on old table counts or earlier summaries of document length.

## D.3 Engineering-default discipline

Details such as the global settings filename, temporary integration path naming, exact SQLite columns, target hardware, request timeouts and packaging versions are engineering choices. They may change with evidence without changing the accepted ownership, process, isolation, autonomy and integration behavior. The specification does not attribute those implementation defaults to the user.

