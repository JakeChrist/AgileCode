# AgileCode 0.1.0

A headless-first Python workbench for ticket-based coding agents, implemented from the AgileCode v1.1 design specification. Teams run a program-enforced eight-step process in isolated Git worktrees; a replaceable PySide6/QML desktop interface and a CLI use the same engine.

**Release status:** functional first implementation, not a claim that every acceptance target in the design has been qualified. Real Git, Bash, SQLite, recovery, HTTP/SSE, MCP fixtures and workflow paths are tested. The QML/Widgets source is included, but PySide6 could not be installed in the build environment, so GUI startup/rendering is **not runtime-verified**. Live LM Studio/llama.cpp model quality and large-repository performance are also unverified. See `docs/IMPLEMENTATION_STATUS.md` and the generated test report.

## Install and run

Linux, Python 3.13 or later, Git, Bash, and an existing local model server are required. Use a normal virtual environment:

```bash
cd AgileCode
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[desktop]"
agilecode-gui /absolute/path/to/your/project
```

The target must be the **original Git project root**, with at least one commit and a configured Git author identity. AgileCode asks before initializing `.agilecode/` and adding its `.gitignore` entry. Commit that ignore change before starting a clean-base ticket.

For a headless install, use `python -m pip install -e .` instead. The core does not require or import Qt.

## Configure a local model

In **Settings & tools**, set the saved endpoint base URL and optionally a model identifier. LM Studio commonly uses `http://127.0.0.1:1234/v1`; a llama.cpp server configured on port 8080 uses `http://127.0.0.1:8080/v1`. Include `/v1`. Load a tool-capable model in the server. AgileCode does not download, load, or unload models. A blank model is resolved only when `/models` lists exactly one entry; otherwise select the identifier explicitly.

Each team member uses the runtime active profile unless it has a `model_override`. No application-level model scheduler serializes concurrent tickets. Questions, tool approvals and merge review remain separate controls.

For CLI configuration:

```bash
agilecode -p /path/to/project init
# Commit the newly added .gitignore entry using your usual Git workflow.
agilecode -p /path/to/project settings save examples/settings-lmstudio.json
agilecode -p /path/to/project models
agilecode -p /path/to/project create "Fix the parser" --description "Describe the requested behavior." --ready
agilecode -p /path/to/project run T-001
```

The `run` command keeps the engine alive and prompts for questions/approvals. Only one host may open a project's writable engine at a time: close the GUI before using another CLI process for that project. One host can run many tickets concurrently.

## What is implemented

- Four-column board, ticket metadata/dependencies, global/project team bundles, frozen run definitions.
- The eight real stages: understand, requirements, architecture, plan, implement, verify, review, final commit.
- Fresh sessions and durable outputs/attempts, correction loops, questions and tool approvals, explicit recovery.
- One OpenAI-compatible HTTP/SSE client, native tool calls, returned reasoning, bounded coherent context, optional model overrides.
- Scoped exact edits, lexical/regex/path/Python-AST search, skills, configured MCP stdio/Streamable HTTP tools/resources.
- Persistent Bash state and managed jobs, guarded command submission, honest exit status and interrupted-outcome reporting.
- Independent nested Git worktrees, private-ref checkpoints, final commit gates, isolated merge candidates and AI-assisted conflict resolution.
- User-controlled integration/cleanup, portable SQLite/artifact/Git-bundle backup and restoration to a new directory.
- QML board/settings/team/history surfaces and Qt Widgets source/diff windows; headless read models, CLI and deterministic demo.

Code and diff presentation use Python/Pygments and Qt, not an embedded Monaco/browser stack. The console is a persistent command shell, **not a full PTY terminal emulator**. The command guard catches common worktree mistakes; it is **not a sandbox** for arbitrary scripts or external MCP servers.

## Test without downloading a model

```bash
python examples/demo.py
```

This creates a temporary project and a deterministic local HTTP model-protocol fixture, executes all eight stages, runs real unit tests, commits, integrates, and checks cleanup. It removes its temporary repository afterward. It is explicitly **not AI inference** or a model-quality benchmark.

```bash
python -m pip install -e ".[test]"
python -m pytest -q
```

**Recorded result: 88 tests passed.** The test report is included. The tests do not need Qt or a GPU. One syntax-span test is skipped if Pygments is absent. No CI platform matrix or cloud service is required.

## Documentation

- `docs/AgileCode_User_Manual_v0.1.0.pdf` — installation, GUI/CLI usage, configuration, recovery and troubleshooting.
- `docs/USER_MANUAL.md` — matching editable manual source.
- `docs/IMPLEMENTATION_STATUS.md` — implemented behavior, tests and qualification limits.
- `docs/ARCHITECTURE.md` — class/module map and extension boundaries.
- `docs/design_v1.1.md` — authoritative input design, retained for traceability.
- `docs/test-results.txt` and `docs/test-results.xml` — actual build-environment results.

The ZIP contains application source, not a bundled Python/Qt runtime, pretrained model, or third-party font files. Project distribution licensing remains the owner's choice; dependency terms are listed in `THIRD_PARTY_NOTICES.md` for review before redistribution.
