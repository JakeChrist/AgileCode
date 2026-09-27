# 1. About this release

AgileCode runs software-development tickets through a team of local coding agents. It separates the development engine from its interface: the Python core owns the workflow, tools, database, shells and Git operations; PySide6/QML presents that state. The CLI uses the same core.

This manual describes source release **0.1.0**, implemented against the **AgileCode v1.1 design specification**. The project works on an external Git repository, not on its own installation directory unless you explicitly open that repository as a project.

## Release qualification

The supplied automated tests exercise real Git worktrees, checkpoint objects, SQLite persistence, persistent Bash, local HTTP/SSE, MCP fixtures, step progression, correction loops, questions, recovery, merge preparation, approval and cleanup. A diagnostic model fixture makes these tests reproducible; it is not AI inference and does not establish how well a particular local model codes.

**The desktop source is included but has not been runtime-verified in the build environment.** PySide6 was unavailable there. QML startup, rendering, platform plugins, high-DPI behavior and accessibility therefore require validation on your desktop. Live LM Studio/llama.cpp servers and large-repository performance also require local qualification. See `IMPLEMENTATION_STATUS.md` and the test reports for exact evidence and remaining limits.

The first release targets **Linux with Python 3.13 or later, Git and Bash**. It is not a Windows/macOS distribution. The console supports persistent shell commands and stdin, not a full terminal emulator. The source package does not include an interpreter, Qt binaries, model weights, or external MCP servers.

## Core concepts

| Term | Meaning |
| --- | --- |
| Ticket | Persistent work on the four-column board. |
| Team | A saved bundle containing members and their attached processes. |
| Process | An ordered list of executable steps assigned to that team's members. |
| Run | An execution with frozen ticket/team/process definitions and its own worktree. |
| Step attempt | One execution of a step, with a fresh session and a durable structured result. |
| Session | A step attempt's model conversation, tool exchanges and questions. |
| Checkpoint | A raw-source snapshot held by a private Git ref; not a ticket-branch deliverable commit. |
| Integration candidate | An isolated, validated combination of a ticket commit and an exact target revision. |

# 2. Install from source

## Desktop installation

Extract the ZIP and open a terminal inside its `AgileCode` directory. Use a normal virtual environment:

```bash
cd AgileCode
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[desktop]"
agilecode-gui /absolute/path/to/your/project
```

The editable installation keeps changes to the Python source visible without reinstalling. QML files are shipped as package data. No Node, npm, React, Cline/Roo extension or external sandbox is required.

Use an existing supported Python interpreter; do not replace your operating system's Python. Install Git and Bash through your normal operating-system tooling if they are not already available. Verify that `git --version`, `bash --version`, and `python3.13 --version` work before troubleshooting AgileCode itself.

## Headless installation

A headless installation does not need PySide6 or Pygments:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
agilecode --help
```

To run the tests, install `.[test]`. To regenerate the PDF manual, install `.[manual]`. The runtime core, tests and manual builder are separate optional dependency groups.

## Launch commands

| Command | Purpose |
| --- | --- |
| `agilecode-gui /path/to/project` | Open the QML desktop interface. |
| `agilecode -p /path/to/project gui` | Open the same interface through the common CLI. |
| `agilecode -p /path/to/project board` | Display the project board without Qt. |
| `python examples/demo.py` | Exercise the complete workflow with a temporary diagnostic fixture. |

Every new terminal session needs the virtual environment activated, or you can invoke `.venv/bin/agilecode` and `.venv/bin/agilecode-gui` directly. Source code and its dependencies remain separate; no third-party font files or Qt binaries are bundled in this ZIP.

# 3. Verify the installation without a model

From the extracted source directory, with the virtual environment active:

```bash
python examples/demo.py
```

The demo starts an HTTP server bound to loopback on a temporary port. That server emits deterministic OpenAI-compatible streaming tool calls for a tiny `answer()` project. AgileCode creates a real worktree, executes all eight stages, changes the function and its regression test, runs the tests, makes the final commit, prepares integration, approves it and checks cleanup.

A successful run prints a `Done` result, all eight completed step IDs, a request count and a final commit ID. Its temporary repository and temporary global settings are removed. It does not touch an existing project or your normal `~/.agilecode` configuration.

**This is a protocol and workflow diagnostic, not a substitute for a local AI model.** It only knows the bundled fixture. Do not configure it as the server for real tickets.

## Automated tests

```bash
python -m pip install -e ".[test]"
python -m pytest -q
```

The test suite creates temporary repositories and requires working Git and Bash executables. Tests configure a temporary Git identity; they do not change your global Git identity. The optional syntax-span test requires Pygments and is skipped when that package is absent.

The archive contains the actual test output and JUnit XML from the build environment. The qualification list distinguishes deterministic integration tests, mocked HTTP tests, source compilation, and untested GUI/live-model behavior.

# 4. Connect LM Studio or llama.cpp

## Endpoint settings

AgileCode uses one **OpenAI-compatible Chat Completions client**. It calls `/models` and `/chat/completions` beneath the configured base URL. It does not use a provider-specific SDK, model marketplace, remote cloud account or Responses API.

| Setting | LM Studio example | llama.cpp example |
| --- | --- | --- |
| Base URL | `http://127.0.0.1:1234/v1` | `http://127.0.0.1:8080/v1` |
| Model | Exact identifier listed by your server, or blank when exactly one is listed | Exact served model identifier/alias, or blank when exactly one is listed |
| API key | Optional environment-variable name | Optional environment-variable name |

LM Studio documents the compatible `/v1/models` and `/v1/chat/completions` endpoints and uses port 1234 in its examples [1]. A llama.cpp server supports OpenAI-style function calling; its model template and server options must enable correct tool formatting [2]. The ports above are examples, not discovery guarantees.

For LM Studio, load an appropriate model and start its local server before running a ticket. For llama.cpp, start your existing `llama-server` with a model/template that supports native tool calls; its official function-calling documentation describes `--jinja` and template overrides [2]. AgileCode neither downloads nor configures the server for you.

## Select a model

Open **Settings & tools**, enter the base URL and save it. Use **List models** to inspect the saved endpoint. Choose its exact identifier and save again. A blank model field is resolved only if `/models` returns exactly one identifier. Some servers list several available models rather than only the one loaded; AgileCode deliberately does not guess between them.

All ordinary team members use the runtime active profile. Changing it affects newly started step attempts. An attempt already in progress keeps its recorded model/profile, and the timeline preserves which model it used. A member's optional `model_override` selects another configured profile by ID.

## Context and authentication

AgileCode does not configure a model's context window. Before context condensation, it asks the provider for the effective loaded context capacity. LM Studio exposes this in the loaded instance configuration, while llama.cpp exposes the active `n_ctx` through `/props`. If a provider does not report an effective context size, AgileCode stops rather than guessing or substituting a configured value.

For an authenticated server, set an environment variable before launching AgileCode and enter its **name**, not its secret value, in `api_key_env`:

```bash
export LOCAL_LLM_KEY='your-local-server-key'
agilecode-gui /path/to/project
```

Leave the setting empty when no key is needed. Credentials are not placed in team definitions. Returned reasoning content is displayed only when the server supplies it; absent reasoning or usage is not invented. Model concurrency is the server's responsibility: multiple active tickets may issue requests simultaneously.

# 5. Open a project and configure defaults

Select the **original Git repository root**. The repository must have at least one commit and a configured commit author identity. A linked ticket worktree is not a second AgileCode project; open its original repository instead.

On first use, AgileCode asks permission to create project data and add `.agilecode/` to `.gitignore`. Existing ignore-file content is preserved. Commit the new ignore entry through your usual Git workflow before starting from a clean base.

```bash
agilecode -p /path/to/project init
```

Project initialization does not copy uncommitted original source into a ticket. By default, starting a ticket requires a clean original checkout. The CLI offers `run --allow-dirty-base` for deliberately starting from committed HEAD while leaving unrelated original changes in place. Those changes will still block integration until resolved.

## Storage layout

```text
~/.agilecode/
    settings.json
    teams/
    skills/

YourProject/
    .git/
    .agilecode/
        agilecode.db
        engine.lock
        worktrees/
        integration/
        artifacts/
        skills/
    src/ ...
```

The one project SQLite database stores tickets, team copies, run definitions, attempts, conversations, tool records, waits, evidence and checkpoint/integration metadata. Source trees and bulky artifacts remain files. Private checkpoint refs live in the project's normal Git object store. There is no per-ticket database or separate JSONL transcript authority.

Global settings and teams live in AgileCode's own home directory, not arbitrary user folders. Project-local settings override the saved global defaults. **Saving a project settings document saves a complete project override**; later global changes do not replace those fields until the project override is reset. Global and project team references remain explicit, such as `global:my-team` and `project:my-team`.

## One host, multiple tickets

One writable application host owns a project at a time. Do not open a second CLI process against a project already owned by the GUI. Within that host, many tickets can execute concurrently. Different projects have independent databases and can have separate hosts.

# 6. Work with the ticket board

The board always has four columns: **Backlog, Ready, In Progress, Done**. Internal states such as waiting for a question, verification, review, preparing integration and awaiting inspection appear in the ticket details rather than becoming additional columns.

Use **New ticket** to enter the objective and description. The configured default team and one of its processes are preselected. Manual creation allows a different team/process; the process selector only shows processes owned by that team. Use the per-ticket integration selection to inherit project policy, require inspection or merge on completion.

You do not need to write a full implementation plan before creating a ticket. State the intended behavior, scope, exclusions and important constraints. The requirements and planning steps develop the details and can ask focused questions when essential intent is missing.

## Board actions

Drag a card between appropriate columns or use its detail controls. Moving a Ready ticket into In Progress starts execution. A duplicate start is idempotent. Reordering within a column updates position without restarting a run. Explicit priority sorts ahead of position.

A card cannot be dragged directly to Done to bypass verification, commit or integration. A running ticket cannot be moved out of In Progress until its run is cancelled or integrated. Reopen a Done ticket into Backlog before starting another run.

Dependencies use ticket keys. An incomplete dependency blocks starting the dependent ticket, and a dependency cycle is rejected. Archived tickets are hidden by default but remain available through the Archived filter. Archiving is not deletion of the history.

## Ticket detail views

| View | What it shows |
| --- | --- |
| Overview | Description, run status/error, branch/base/worktree, dependencies and feedback. |
| Steps | Each attempt, assigned member/model, phase and structured result. |
| Conversation | Canonical messages, returned tool calls/results and available reasoning. |
| Files | Source listing, source windows, ticket diffs and checkpoint controls. |
| Jobs | Command, observed output, status, exit code, stdin and stop controls. |
| Merge | Isolated candidates, exact target/candidate revisions, inspection and cleanup. |

The UI's previews are bounded. Long transcripts can be paged; long messages remain available in request inspection and ticket export. Closing a ticket's detail pane does not cancel its execution.

# 7. Understand the enforced process

The built-in development team runs this sequence:

| Step | Assigned member | Required result |
| --- | --- | --- |
| 1. Understand ticket | Analyst | Objective, scope, relevant context and clarified intent. |
| 2. Requirements and verification | Analyst | Stable REQ identifiers and explicit verification methods. |
| 3. Architect changes | Architect | Minimal required file/interface/behavior changes. |
| 4. Detailed plan | Architect | Ordered implementation actions and verification work. |
| 5. Implement | Programmer | Changes in the ticket worktree following the plan. |
| 6. Verify and validate | Tester | Observed evidence for each requirement and configured tests. |
| 7. Code review | Reviewer | Independent complete-diff review with findings or acceptance. |
| 8. Final commit | Programmer | Descriptive final commit produced through the engine gate. |

These are executable steps, not suggestions hidden in a skill. A model cannot jump to implementation, commit from an earlier stage or mark the whole ticket Done. `finish_step` returns a structured status, summary and output; the engine decides whether the stage may advance.

Each attempt receives a fresh conversation, with the ticket, workspace and relevant persisted outputs of earlier stages. Requirements, the plan, correction findings and queued user feedback remain pinned rather than relying on the model to remember a long prior chat.

## Evidence and correction

Requirements use identifiers such as `REQ-1`. Verification records a requirement check against a real tool execution ID and the current source fingerprint. A PASS without recorded current evidence is rejected. Configured verification commands must actually exit successfully; code review must inspect the full diff.

Failed verification returns to implementation. Requested review changes return to implementation, then verification, then review. Each retry is a separate attempt and keeps the earlier records intact. The default correction limit is three; reaching it pauses the run for inspection rather than looping without limit.

The engine can enforce ordering and recorded evidence. It cannot guarantee that an agent understood a requirement correctly or that a chosen test proves every relevant property. Human inspection remains useful, particularly for changes with limited tests.

# 8. Configure teams and processes

A team owns its members and all attached processes. A process step references a member of that same team. Save the **whole team bundle** globally for reuse or project-locally for a project-specific copy. Built-in definitions are not overwritten; clone the standard bundle under a new ID.

The first-release team editor is a validated JSON editor with scope selection, not a graphical process designer. Exporting, editing and saving the same bundle through the CLI is also supported:

```bash
agilecode -p /path/to/project teams export builtin:standard > my-team.json
# Edit the bundle: give the team a new id and name.
agilecode -p /path/to/project teams save my-team.json --scope global
agilecode -p /path/to/project teams list
```

A complete starting example is in `examples/team.json`. Its member definitions include a name, system prompt, explicit allowed tool names, available skills, and an optional model override. Each process has a name, ordered steps and a standard-process flag.

## Member versus step instructions

A member's system prompt defines its role. A step's prompt defines the current assignment. Both are combined with ticket context and durable earlier results. Reusing an architect for two steps does not reuse one giant conversation; each step gets a new session.

```json
{
  "id": "plan",
  "name": "Implementation plan",
  "team_member_id": "architect",
  "kind": "plan",
  "prompt": "Create a detailed ordered plan tied to the requirements.",
  "correction_target": null
}
```

Step IDs must be unique within a process. Member references must be valid. A correction target must name an earlier step. The built-in standard process retains all eight required kinds in order. A custom process may change its sequence, but final-commit and integration gates still require appropriate verification and review evidence; a custom definition does not grant unrestricted Git commands.

## Frozen runs and model overrides

Starting a ticket snapshots the complete team/process and ticket definition. Editing a global bundle does not rewrite an active run. Future runs use the revised definition.

Normally `model_override` is null, meaning the active runtime profile. A deliberate override is a profile ID, not a separate copy of URL/key/model configuration. Missing profile IDs cause a clear error; they do not silently switch models.

# 9. Control autonomy, questions and interruption

**Tool autonomy and merge policy are independent.** Full tool autonomy does not enable merge-on-completion. Inspection before merge does not require asking about every read operation.

Each team member has an explicit list of tool names, such as `read_file`, `git_diff` and `run_command`. If a tool name is absent, that model session does not receive that tool. Tool categories and wildcard capability groups are not part of current team authorization.

When Full tool autonomy is enabled, permitted effectful tools run automatically unless an explicit rule says otherwise. Restricted mode asks before effectful tools unless an exact tool-name rule says otherwise. `tool_rules` therefore uses exact tool names:

```json
{
  "autonomy": "restricted",
  "tool_rules": {
    "write_file": "allow",
    "run_command": "ask",
    "final_commit": "deny"
  },
  "merge_on_completion": false
}
```

Commands have one additional, command-specific ordered rule list because many different shell operations pass through the single `run_command` tool. The first matching command rule wins; when no rule matches, `run_command` follows its exact tool rule or the existing autonomy default. The initial command rule list is empty, preserving the existing autonomy behavior. Add, reorder or remove rules in Settings & tools → Command permissions. Patterns are case-sensitive shell-style globs matched against the whole trimmed command string; they do not parse shell semantics or separately authorize each part of a compound command. A broad Allow pattern can therefore allow compound commands too. An explicit run_command Deny always wins.

```json
{
  "command_rules": [
    {"pattern": "sudo *", "action": "ask"},
    {"pattern": "*pip install*", "action": "ask"},
    {"pattern": "*npm install*", "action": "ask"}
  ]
}
```

Edit these fields in the complete settings document rather than replacing unrelated settings. Approval does not override worktree containment, invalid arguments, stale hashes or process-stage gates.

## Questions and approvals

A question pauses the same attempt/session and resumes it with your answer. It does not discard completed prior stages or create a correction attempt by itself. Approval requests identify a specific tool call and bind its arguments; stale or changed requests are not silently reused.

In the desktop, use the question/approval panel in the ticket details. In an interactive CLI run, answer at the terminal prompt. `--non-interactive` leaves a durable wait rather than guessing; use the GUI or `answer WAIT_ID TEXT` to continue.

## Pause, feedback and cancel

Pause cancels the active model request (including context summarization), prevents further tool calls, and stops the ticket’s foreground and managed background command processes. The UI reports Paused after shutdown completes. Partial text and command effects remain available for inspection; Resume continues the same ticket and worktree without automatically replaying stopped commands. An answer to a pending approval or question does not restart an explicitly paused ticket. Remote MCP operations with uncertain effects still require recovery. Feedback is queued after outstanding tool results. Cancel ends the run while retaining source, checkpoints and history. Delete is the destructive corrective action for an unintegrated ticket: after explicit confirmation it stops ticket-owned activity and removes the ticket worktrees, dedicated branches, checkpoint/integration refs, artifacts, conversations, execution history and ticket record, including dirty/uncommitted work. It never edits another ticket to make deletion possible, and it refuses deletion once the ticket has reached project history.

Closing the application host stops in-flight model requests and owned shells. Restarting restores durable process state, not a dead shell's aliases, activated environment or running subprocess memory. Unknown external effects require inspection before the application will replay an operation.

# 10. Inspect and edit code

Use **Files** to list ticket source, open a path or compare it with the ticket's starting revision. The optional Widgets source windows provide line numbers, syntax coloring, search, line navigation, zoom and guarded manual edits. The primary desktop remains QML. The aligned diff view shows before/after line mappings and distinguishes changed rows.

The full unified ticket diff is generated by Git against the recorded base, including eligible untracked files during development. After successful cleanup, source inspection reads the final ticket commit instead of pretending the current original checkout is still that ticket's result.

## Guarded manual corrections

Pause an uncommitted run before editing. Open the source through AgileCode, make the change and choose **Save guarded edit**. The engine compares the original byte hash with the current file; a stale buffer is rejected rather than overwriting someone else's change.

A successful manual edit invalidates implementation/verification/review success pointers. Resume the process to reconcile the code with the plan and run downstream checks again. You cannot manually edit an already committed result through the ticket source window and retain its earlier verification.

Ctrl+F focuses search, Ctrl+L opens line navigation, and Ctrl++ / Ctrl+- zoom the code pane. An unsaved editable buffer prompts before being discarded. State-changing GUI actions require open editable buffers to be saved or discarded first.

## Supported file behavior

UTF-8 and UTF-8 with BOM are supported. Exact editing preserves established CRLF conventions. Binary/non-UTF-8 files are not text-edited. Text-file operations have an 8 MiB input limit; individual read previews are capped at 256 KiB and explicitly marked partial. Partial reads are not editable buffers.

Checkpoint storage can preserve eligible binary files, symlinks and executable bits even though the text editor cannot edit them. Complex encodings, Git LFS/filter workflows, submodule edits and pathological file layouts are not fully qualified by this release. Keep those cases under explicit review.

# 11. Search and skills

## Code discovery

The `code_search` tool supports literal text, bounded regular expressions, filename/path queries, Python AST symbol definitions, and textual reference candidates. Results identify scope, path, source lines and a file hash. Agents can search, read the relevant region, follow imports/callers and refine the query.

Python symbols are parsed structurally. Other-language symbol patterns and reference hits are candidates, not language-server semantic resolution. Searches are bounded by time, file count and result limits. Truncation and skipped files are visible; a partial result is not evidence that the rest of the repository contains no match.

There is no embedding model, vector database or semantic-index daemon in v0.1.0. Strong conventional discovery is core; a later hybrid search implementation can be added behind the same tool contract.

## Skill locations

```text
~/.agilecode/skills/<skill-name>/SKILL.md
<project>/.agilecode/skills/<skill-name>/SKILL.md
```

The global folder is AgileCode's own directory. A same-name project skill overrides the global skill for that project. Each file needs valid metadata:

```text
---
name: my-skill
description: Explain when this skill is relevant.
---
# Instructions
Write reusable, bounded guidance here.
```

The name must match the containing directory. Invalid metadata appears as a discovery error; it is not silently ignored. Add the skill ID to a member's `skills` list to make it available. The agent loads full content on demand through `load_skill`; loading does not execute scripts or alter tool permissions.

Three original bundled skills cover codebase investigation, focused Python development, and verification/review. They are installed into the global skills folder only when a same-name folder does not already exist. The standard team makes the relevant skill available to each role without requiring it to be loaded every time.

# 12. Persistent shell and managed jobs

Each active ticket runtime owns one real Bash shell across commands and process steps. `cd` within the worktree, exported environment variables, activated virtual environments and shell functions persist while that shell is alive. Commands run in the ticket worktree rather than the original checkout.

The shell starts without profile/rc files and does not inherit AgileCode’s own Python environment. In **Settings & tools**, Python environment defaults to **Automatic for Python projects**. A Python project with a dependency manifest gets its own `.venv` inside each ticket worktree and each integration worktree. Choose **Always create a worktree .venv** for Python projects without a manifest, or **Disabled** for projects that manage environments themselves. Set Python interpreter to the required executable (for example, `python3.13` or its full path).

AgileCode creates the venv, selects it for foreground and background commands, and tells the model the interpreter path. No activation command is needed. With no custom setup commands, dependency setup uses `uv sync --frozen --active` for `uv.lock`, `python -m pip install -r requirements.txt` for requirements, or an editable project installation. Other lockfile managers require a declared setup command. Successful automatic setup is reused on shell/application restart and repeated if the dependency manifest changes. The `.venv` is ignored by Git and removed with normal worktree cleanup.

**Worktree setup commands** run inside the environment whenever a new shell starts. Use them for project-specific dependencies and shell initialization. If provided, these commands replace automatic dependency installation and should be repeatable. For this repository’s test dependencies, an example is:

```json
{
  "python_environment": "auto",
  "python_interpreter": "python3.13",
  "setup_commands": ["python -m pip install -e '.[desktop,test]'"]
}
```

Do not activate the original checkout’s environment in these commands. Setup fails visibly if it selects Python outside the ticket `.venv`. Pausing also stops a running setup command; environment creation can resume without moving or copying a venv.

## Command output and cancellation

Each job records its command, owner, working directory, artifact, status and real exit code. Completion is determined through the shell's control channel, not by guessing that quiet output means the process finished. Output previews are bounded; use the job output offset to retrieve more retained content.

Foreground commands use the persistent shell and support stdin. Managed background jobs inherit a snapshot of its exported environment and working directory; they do not share later changes to nonexported shell variables or functions. Request background execution explicitly rather than hiding an unmanaged `&` process in a command.

Use **Stop job** to terminate only an owned job. Cancellation or timeout may recreate the shell, which loses its volatile state. Stop live jobs before checkpoints, manual edits or step completion so source can be inspected consistently.

## Workflow guard, not security sandbox

The CommandGuard is containment only. It rejects identified directory escapes, explicit write destinations outside the worktree, and unauthorized Git lifecycle commands. `cd src`, ordinary tests and permitted reads are allowed; changing to the original checkout or writing a file there is rejected. CommandGuard never creates an approval request. If it cannot fully understand a complicated command but finds no explicit boundary violation, command permission remains the responsibility of the command policy.

The guard does not prove what an arbitrary Python program, build tool, shell function, Git hook or external MCP server will do. Such programs can still write outside the worktree under the OS user's permissions. This is practical workflow protection, not hostile-code containment. No bubblewrap/container dependency is introduced.

# 13. Checkpoints and the final commit

Before each implementation/correction attempt, AgileCode captures a checkpoint. You can also create one explicitly when the workspace is stable. Checkpoint metadata is in SQLite; source snapshots are Git objects referenced beneath:

```text
refs/agilecode/checkpoints/<ticket-id>/<checkpoint-id>
```

Snapshot construction uses a private temporary index and does not move ticket HEAD or alter the normal ticket index. Eligible newly created files are included. Credential files and AgileCode runtime folders are excluded, and an unsafe tracked file is surfaced rather than silently omitted from a deliverable.

## Restore a checkpoint

Pause the uncommitted run and stop live jobs. Select an available checkpoint and inspect the restore preview. The preview identifies the expected current tree and the intended target. Confirming it first saves a pre-restore checkpoint, then applies guarded file restoration.

If anything changed after the preview, restoration is rejected. Refresh the preview instead of forcing the old decision. The restore does not rewind the ticket branch or erase prior attempt history; downstream implementation/verification/review are invalidated and must run again.

```bash
agilecode -p /path/to/project preview-checkpoint T-001 CHECKPOINT_ID
agilecode -p /path/to/project restore-checkpoint T-001 CHECKPOINT_ID \
  --expected-tree TREE_FROM_PREVIEW
```

Removed checkpoint refs are displayed as removed, not as restorable snapshots. Backup archives include retained checkpoint objects. Ordinary Git garbage collection can eventually remove objects once all retaining refs have been deleted.

## Step 8

The standard process makes no intermediate deliverable commits on the ticket branch. After verification and independent review pass for the exact current tree, Step 8 requests a descriptive final commit. The engine checks that the branch still has its recorded base and that the verified tree has not changed.

Normal Git commit hooks run. A hook that changes the reviewed source invalidates the result; it is not quietly accepted. The commit's existence and exact identity are recorded durably, so recovery can distinguish an already-completed commit from an action that still needs inspection.

# 14. Integration and AI-assisted conflicts

A completed ticket has a final branch commit. It is not automatically the target branch. The separate merge coordinator prepares an isolated candidate for the **exact current target revision** and the ticket commit. Active ticket worktrees are not continually rebased as other work completes.

With **inspect before merge**, the candidate waits while you review the diff, conflicts and validation report. With **merge on completion**, the engine attempts the same preparation and validated application automatically. A per-ticket choice can override project policy.

## Candidate preparation

When the target is an ancestor of the exact reviewed ticket tree, the candidate can reuse that unchanged verification. When the target has diverged, Git first prepares a normal merge in a separate integration worktree. Even a clean textual merge receives fresh combined-tree verification and review.

For textual conflicts, an assigned team member receives the recorded base, target and ticket versions, the ticket requirements and recent integrated-ticket context. It proposes a candidate through guarded file edits. The original checkout remains untouched. The candidate then receives fresh verification and independent review.

A model's confidence statement is not acceptance evidence. Remaining conflict markers, missing requirements, failed checks, stale content or ambiguous intent block application. Binary/large conflicts and unsupported submodule cases stop for manual handling rather than being guessed at. The first release does not provide a complete interactive binary-conflict repair UI.

## Apply an inspected candidate

Approval binds both the candidate commit ID and target commit ID. AgileCode checks that the original checkout is still on the intended branch, is clean, and still has the expected target revision. It then advances that target to the validated candidate without an implicit branch switch or force reset. No push occurs.

```bash
agilecode -p /path/to/project prepare-merge T-001
agilecode -p /path/to/project merge INTEGRATION_ID \
  --candidate CANDIDATE_OID --target TARGET_OID
```

If the target advances during inspection, rebuild the candidate and repeat validation/approval. After successful application, the ticket reaches Done. Cleanup errors do not undo a successful integration or erase that fact.

# 15. Cleanup and retained history

The default post-integration policy removes the ticket worktree and hidden checkpoint refs, retains the ticket branch, and retains all ticket/process history in SQLite. The user can change these defaults in settings.

```json
{
  "remove_worktree_after_merge": true,
  "remove_checkpoints_after_merge": true,
  "delete_branch_after_merge": false
}
```

Automatic cleanup only happens after a successful integration. An unmerged, failed or cancelled ticket otherwise retains its worktree and useful recovery data until the user explicitly deletes that ticket. The final ticket commit remains reachable through the retained branch unless the unintegrated ticket is deleted; integrated Git history is never rewritten by ticket deletion.

The cleanup service checks ownership, expected commit and workspace contents before removing a directory. Unexpected ignored files are retained and reported rather than deleted merely because `git status` says clean. Known disposable caches can be removed with their owned worktree. Retrying cleanup after resolving the problem is explicit:

```bash
agilecode -p /path/to/project cleanup T-001
```

Old, stale or blocked integration candidates may remain for inspection. This release does not automatically age out every abandoned candidate or old branch. Inspect the recorded paths and Git references before manual housekeeping; never recursively delete the whole `.agilecode/` directory as routine cleanup.

# 16. Configure MCP

MCP support is included but optional to use. Native file/search/shell/Git tools do not require an MCP server. Only explicitly configured and enabled servers are launched or contacted. No marketplace, automatic package installation or OAuth browser flow is included.

Supported operations are initialization, tool discovery/calls, resource discovery/reads, pagination, ping and root reporting over **stdio** or **Streamable HTTP**. The client does not execute server-requested sampling, elicitation or arbitrary callbacks. The wire protocol follows the documented MCP transport subset [4].

## Try the bundled read-only example

Merge this entry into the full settings document, replacing both paths with real absolute paths. The sample begins disabled; enable it only after inspection.

```json
{
  "mcp_servers": [{
    "id": "echo",
    "transport": "stdio",
    "command": "/absolute/path/to/AgileCode/.venv/bin/python",
    "args": ["/absolute/path/to/AgileCode/examples/mcp_echo_server.py"],
    "enabled": true,
    "scope": "ticket"
  }]
}
```

The example echoes text and reports its working directory; it does not modify files. The client supplies the ticket workspace as its working directory/root. `{workspace}` can be substituted in arguments when a server needs an explicit path. Discovered MCP tools receive stable AgileCode aliases. To expose one to a team member, add that exact alias to the member's `allowed_tools`; an exact `tool_rules` entry can then set Allow, Ask or Deny for that alias.

## Credentials and effects

A stdio configuration can reference a named environment variable as `${ENV:NAME}` in an environment value. An HTTP configuration can use `api_key_env` for a Bearer credential and an explicit HTTP(S) URL. Do not save literal secrets in team or settings exports.

The `scope` label describes intended effects; it is not a filesystem sandbox imposed on the server. External services can affect external state. MCP calls use the same user approval/deny policy and durable result pipeline, and uncertain calls are not blindly retried after a crash.

Discovery errors are shown while native tools remain available. This release is not a full implementation of every optional MCP feature or every third-party server's authentication extensions. Start with a small, known server and inspect its behavior before granting full autonomy.

# 17. Restart, recover and reconcile

Completed step outputs are persisted as the process advances. Restarting does not ask the model to regenerate stages that already completed. An interrupted attempt resumes its recorded conversation when the outcome is known; a pending question remains attached to that same session.

A shell process cannot be reconstructed from a database row. On restart, a new shell is created and configured setup commands are run. Old command jobs that may have been running are marked unknown instead of being assigned a fabricated exit code. Arbitrary shell or MCP calls cannot be guaranteed exactly-once across every crash boundary.

## Recovery workflow

Open **Recovery** or run `agilecode recovery`. Inspect the listed unknown tool/job, its inputs, output artifact and actual workspace/external effect. Record what you established. Only mark a tool as not executed when you have confirmed that it never ran; that permits the original call to be retried.

```bash
agilecode -p /path/to/project recovery
agilecode -p /path/to/project reconcile tool TOOL_ID \
  "Inspected the file and confirmed the write completed."
agilecode -p /path/to/project resume T-001
```

Reconciliation is an operator statement, not an automatic undo. Do not guess that a missing output means a command had no effect. A stopped process may have modified some files before interruption.

Final commits, checkpoint refs and target application use recorded object IDs/intents to reconcile outcomes where Git provides an observable state. A stale merge is rebuilt instead of replayed against a new target. An interrupted integration question may require candidate rebuilding; the completed development run remains intact.

## Host ownership

A second host cannot bypass an active project's engine lock. This release does not include a daemon/IPC attach command for controlling a GUI from another CLI terminal. Use the current host's controls, or close it before opening a new one. The limitation does not serialize tickets inside the host or model requests at the endpoint.

# 18. Back up, restore and export

A portable backup contains a coherent SQLite snapshot, retained artifacts, project skills, copies of global team/skill bundles and a Git bundle containing committed history and private refs. For interrupted uncommitted ticket work, the backup creates a recovery checkpoint before bundling.

Backups require an **idle engine**, no live jobs and a clean original checkout. Close the active GUI/CLI run and reopen the project without resuming work before invoking backup. Unrelated uncommitted files in the original checkout, caches, active shell memory and environment credentials are not captured.

```bash
agilecode -p /path/to/project backup /path/to/backups/project-001.zip
```

Choose a new filename; existing backup files are not overwritten. A manifest records hashes and sizes. Treat backups as private because source, exact prompts, conversations and model/tool outputs can contain sensitive project information.

## Restore to a new location

```bash
agilecode restore-backup /path/to/backups/project-001.zip \
  /path/to/new-restored-project
```

The destination must not already exist. Restore validates archive paths, sizes and hashes before constructing the project. It restores Git refs, rewrites owned absolute project paths, rebuilds worktrees and restores captured uncommitted source without moving the original ticket HEAD.

Restored work remains paused for inspection. Review model profiles and global team references, configure Git identity and dependencies, and explicitly re-enable any needed MCP servers or automatic merge policy. Recovered global bundles are placed under `.agilecode/recovered_global/`; they do not overwrite your existing global configuration. Install/import those bundles deliberately.

The restore has a 10 GiB extracted-content safety limit. Git remotes, hooks, local author configuration and every external tool dependency are not automatically imported. This is portable project recovery, not a full system image.

## Ticket export

```bash
agilecode -p /path/to/project export-ticket T-001 /path/to/T-001.json
```

The export contains the ticket detail and retained conversations for its recorded attempts. It is an inspection/audit artifact, not a replacement for a portable project backup or the authoritative database. Deleting an export does not delete the ticket.

# 19. CLI reference and headless use

Place global options before the command. `-p` selects the original project; `--global-dir` selects an alternate AgileCode home; `--json` requests structured output. The default project is the current directory.

| Command group | Principal commands |
| --- | --- |
| Project/board | `init`, `board`, `create`, `show`, `move`, `archive`, `reopen`, `delete` |
| Execution | `run`, `resume`, `pause`, `cancel`, `feedback`, `answer` |
| Inspection | `conversation`, `diff`, `job`, `skills`, `models`, `discuss` |
| Checkpoints | `checkpoint`, `preview-checkpoint`, `restore-checkpoint` |
| Integration | `prepare-merge`, `merge`, `cleanup` |
| Configuration | `settings show/save/reset-project`, `teams list/export/save` |
| Recovery/data | `recovery`, `reconcile`, `backup`, `restore-backup`, `export-ticket` |

Use `agilecode COMMAND --help` for the exact arguments. `run` and `resume` keep the engine alive. `move ... "In Progress"` directs CLI users to `run` rather than starting a task and immediately exiting its owner process.

## Example inspection sequence

```bash
agilecode -p /path/to/project board
agilecode -p /path/to/project show T-001
agilecode -p /path/to/project diff T-001
agilecode -p /path/to/project conversation SESSION_ID --after 200
agilecode -p /path/to/project job JOB_ID --offset 65536
```

Only one host can own the project at a time. The standalone `pause` command does not attach to an already-running GUI/CLI host. A headless integration should keep its `Application` object alive and call its methods on the same asyncio loop.

## Python application API

```python
import asyncio
from agilecode.core.application import Application

async def main():
    app = Application("/path/to/project")
    await app.open()
    try:
        ticket = await app.create_ticket(
            "Implement the change", "Describe scope and intended behavior."
        )
        await app.move_ticket(ticket["id"], "Ready")
        run = await app.start_ticket(ticket["id"])
        await app.wait_for_run(run["id"])
    finally:
        await app.close()

asyncio.run(main())
```

A real headless host must subscribe to events and provide an approval/question handler, or use an appropriate autonomous policy. Otherwise a legitimate wait remains paused. Multiple `start_ticket` calls can progress concurrently in the same host; do not add a model-request semaphore around them.

# 20. Troubleshooting and practical limits

| Symptom | Action |
| --- | --- |
| Desktop dependency missing | Activate the correct virtual environment and install `.[desktop]`. Use the headless demo to separate core installation from Qt problems. |
| QML or platform plugin fails to load | Preserve the Qt diagnostic output. Run with `QT_DEBUG_PLUGINS=1` to identify the missing system plugin/library. GUI runtime is not qualified in the supplied build environment. |
| Endpoint cannot list models | Start the server; verify the saved URL, port, `/v1` suffix and authentication environment variable. |
| Several models listed with blank selection | Select the exact identifier. AgileCode will not infer which available model you intended. |
| Model prints tool JSON as prose | Use a native-tool-capable model/template and the correct server configuration. AgileCode does not execute arbitrary prose as a tool call. |
| Context budget exceeded | Increase context within the server's actual capacity, reduce ticket scope, or split work. Pinned requirements/plans are not silently discarded. |
| Verification cannot pass | Inspect each REQ's real recorded source result and the configured test command. A prose PASS is not sufficient. |
| Source changed after review | Rerun the relevant implementation/verification/review path; do not force an old fingerprint to pass. |
| Original checkout is dirty | Commit or deliberately resolve your original edits. Do not let integration discard them. |
| Candidate approval is stale | Rebuild against the new target, revalidate and inspect again. |
| Worktree not removed after merge | Inspect the cleanup report for unexpected ignored/untracked files. Preserve needed content, then retry cleanup. |
| Project is already open | Use the current host or close it. The CLI does not attach through an internal server. |
| Shell environment disappeared | The shell was restarted, stopped or timed out. Reapply intended setup commands; durable step history remains. |
| Command rejected by guard | Inspect its cwd, destinations and Git operation. Simplify ordinary commands. Unknown constructs may need user approval, but direct violations remain denied. |
| MCP discovery fails | Check the configured executable/URL and server protocol. Leave it disabled while verifying native tools still work. |

## Release boundaries

This first implementation does not claim certified GUI polish/accessibility, a full PTY terminal, every language-server feature, embedding search, all optional MCP features, binary conflict editing, automatic branch retention schedules or production-scale performance. It provides explicit stopping and inspection paths rather than pretending unsupported cases succeeded.

The design's quality targets remain targets until measured on representative hardware and repositories. Passing workflow tests does not prove arbitrary model-generated code is correct. Start with a small disposable or backed-up project, inspect the first real model run, and then select the autonomy and integration settings that fit your work.

# 21. Reference sources and build record

The implementation authority is the user's AgileCode v1.1 design and confirmed decisions, retained as `docs/design_v1.1.md`. Source claims about the delivered application can be checked against the modules and actual test report in this package.

[1] LM Studio, OpenAI Compatibility Endpoints. Confirms the compatible endpoint paths and base-URL configuration. https://lmstudio.ai/docs/developer/openai-compat

[2] llama.cpp, Function Calling. Describes native/generic tool formats, Jinja/template configuration and OpenAI-style tool calls. https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md

[3] Qt for Python, Python-QML integration. Describes the supported separation between Python objects and QML presentation. https://doc.qt.io/qtforpython-6/tutorials/qmlintegration/qmlintegration.html

[4] Model Context Protocol, 2025-03-26 transport specification. Documents stdio and Streamable HTTP transport behavior. https://modelcontextprotocol.io/specification/2025-03-26/basic/transports

[5] Git documentation: worktrees, commit-tree, update-ref and bundle. https://git-scm.com/docs

External references were checked on 23 September 2026. The manual paraphrases only the relevant integration contracts; it does not reproduce upstream manuals. Exact dependency versions and test outcomes for this build are recorded in `environment.json`, `test-results.txt` and `test-results.xml`.

The project owner has not been assigned an overall redistribution license by this package. Before publishing an installer, review the installed third-party and Qt-module licenses and retain required notices. The source archive does not bundle those distributions or third-party fonts.


### Tool configuration compatibility

Team bundles now save `format_version: 2`. `allowed_tools` contains exact native tool names or discovered MCP aliases. The team editor offers checkboxes, Select all and Clear; Select all saves the current names and does not grant tools discovered later. Discover MCP tools connects only to enabled configured servers, shows each server/tool name, and stores its stable alias when selected. Saved aliases remain selected when a server is temporarily offline.

Version 1 team bundles and saved run snapshots expand the old native categories and `*` once on load, including the previously implicit `update_progress`, `ask_user` and `finish_step` tools. Saving writes version 2. New definitions reject category names and wildcard selectors. Legacy wildcard selectors expand only to known native names. Dynamic MCP wildcard access is not transferred; select the explicit aliases from discovery. Existing MCP category approval rules are retained only as a compatibility fallback so a saved Deny cannot accidentally become Allow.

CommandGuard returns only OK or BLOCK. It never asks for approval; unrecognized syntax alone does not trigger a prompt. Recognized direct worktree or Git-boundary violations remain blocked even after command approval. These checks do not sandbox arbitrary programs. Interrupted effectful calls are retained as unknown for recovery and are never silently replayed after a restart.
