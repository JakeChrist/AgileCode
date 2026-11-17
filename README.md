AgileCode

AgileCode is a local application for managing AI-assisted software development through a ticket-based workflow. It combines a graphical project board, specialized AI development teams, configurable development processes, and Git-based workspace isolation.

AgileCode supports multiple concurrent development tickets, each operating in its own Git worktree. A project manager coordinates work across tickets, while individual team members perform engineering tasks using configured language-model providers and development tools.

The application provides a native desktop interface and a command-line interface backed by the same execution engine.

Features

Ticket management

Projects are organized around tickets with four states: Backlog, Ready, In Progress, and Done. Tickets contain descriptions, dependencies, assigned teams, and execution history.

AI development teams

Development teams consist of configurable AI team members with defined responsibilities, instructions, and tool permissions. Teams operate through configurable processes covering requirements, architecture, planning, implementation, verification, review, and completion.

Project management

An AI project manager assists with project organization, ticket creation, task delegation, and coordination between concurrent development activities. The project manager can communicate with the user through a dedicated conversation interface.

Concurrent execution

Multiple tickets can execute simultaneously, subject to user-configured concurrency limits. Each ticket maintains its own execution context, working directory, and development history.

Git integration

Each ticket operates in an isolated Git worktree and associated branch. AgileCode manages execution checkpoints, commits, integration, and merge operations while preserving isolation between concurrent tasks.

Language-model providers

A configurable Provider Pool supports OpenAI-compatible model endpoints, including local inference servers such as LM Studio and llama.cpp. Providers can be assigned to individual tickets and team members.

Development tools

AI team members can access development tools for reading and modifying files, searching source code, executing commands, running tests, and interacting with Git. Additional capabilities are supported through Model Context Protocol (MCP) integrations and reusable skills.

Persistent execution

Project state, ticket conversations, execution records, and artifacts are retained across application sessions. Interrupted work can be resumed through the application’s execution and recovery mechanisms.

Requirements

AgileCode currently targets Linux.

Requirements:

* Python 3.13 or newer
* Git
* Bash
* A compatible language-model provider
* PySide6 for the desktop interface

A locally hosted model is not required if an accessible compatible provider is available.

Installation

Clone the repository and navigate to the project directory.

Create a Python virtual environment:

python3.13 -m venv .venv
source .venv/bin/activate

Install AgileCode with desktop dependencies:

python -m pip install -e ".[desktop]"

For a headless installation:

python -m pip install -e .

Running AgileCode

Launch the desktop application:

agilecode-gui /path/to/project

The selected project must be an initialized Git repository with at least one commit and a configured Git identity.

AgileCode maintains its project-specific application data in the .agilecode directory, which is excluded from source control.

Command-line interface

Initialize a project:

agilecode -p /path/to/project init

Configure an LM Studio provider:

agilecode -p /path/to/project providers save examples/provider-lmstudio.json --make-default

List available providers:

agilecode -p /path/to/project providers list

Create a ticket:

agilecode -p /path/to/project create "Fix the parser" --description "Correct parser behavior." --ready

Execute the ticket:

agilecode -p /path/to/project run T-001

Only one AgileCode host can maintain writable access to a given project’s execution engine at a time.

Development workflow

AgileCode organizes development into tickets that represent distinct units of work.

A typical development process includes requirements analysis, architecture, implementation planning, coding, verification, and review. The precise sequence, team assignments, and instructions are configurable.

Each ticket maintains a dedicated conversation and execution record. Team members can inspect project files, produce development artifacts, use authorized tools, and communicate findings through the ticket conversation.

Execution proceeds through the configured development process, with review and correction cycles as required.

Git worktrees isolate modifications until the resulting changes are ready for integration into the project’s primary development branch.

Provider configuration

Language-model connections are managed through the global Provider Pool.

Each provider instance defines an endpoint and model configuration. A default provider can be selected, and individual tickets or team members can use other configured instances.

Common local endpoints include:

LM Studio: http://127.0.0.1:1234/v1

llama.cpp: http://127.0.0.1:8080/v1

AgileCode communicates with compatible providers through the OpenAI API protocol.

Model loading, unloading, and inference resource management remain the responsibility of the provider.

Testing

AgileCode includes automated tests covering its application services, execution engine, Git operations, model communication, and ticket workflows.

Install testing dependencies:

python -m pip install -e ".[test]"

Run the test suite:

python -m pytest -q

A deterministic demonstration is also included:

python examples/demo.py

The demonstration creates a temporary Git project and exercises development operations using a simulated model endpoint. It does not require a running language model.

Architecture

AgileCode separates its application logic, execution engine, external integrations, and desktop interface.

The application uses Python for its backend and PySide6 with QML for the desktop interface. SQLite provides persistent project storage, while Git worktrees and branches manage isolated source-code modifications.

Its execution tools include filesystem operations, source-code search, persistent shell sessions, Git operations, and MCP integrations.

Development commands execute within the host environment. Git workspace isolation is not an operating-system security sandbox.

Documentation

Additional documentation is available in the docs/ directory.

* USER_MANUAL.md — application installation, configuration, and usage
* ARCHITECTURE.md — software architecture and major components
* IMPLEMENTATION_STATUS.md — development and verification status
* design_v1.1.md — original software design specification

## License

AgileCode is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

Third-party dependency and licensing information is available in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
