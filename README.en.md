# FourBis Relay

[Español](README.md) · **English**

FourBis Relay is a local workspace for working with conversational AI across multiple repositories. It keeps project conversations and execution context together, while letting you inspect tasks, tools, and results in one browser workspace.

**Explore the [static demo](https://fourbis.github.io/4bis.relay-showcase/)** — a simulated interface with fictional data. It does not connect to Relay, call an AI provider, or run tools. Relay itself is experimental software intended for local use.

Relay is a FourBis and Jeremías Badilla portfolio project, released under the MIT License. See [LICENSE](LICENSE) for the project license and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for third-party notices.

## What you can explore

- Persistent conversations and searchable project context.
- Tasks that can keep a worktree between messages and associate validation with a commit.
- Optional expert runs organized into planning, execution, verification, and documentation stages.
- A browser workspace where chats, task graphs, tables, and other tools can remain visible together.
- Optional native tools and MCP servers for repository files, shell commands, SQL, browser, and GitHub workflows.

Three concrete ways to use it:

1. **Review a repository change:** ask an expert to inspect a project, keep the task in its worktree, and review the verification result tied to the commit.
2. **Follow a multi-step investigation:** keep the conversation beside its task graph while checking dependencies, progress, and results.
3. **Compare project work:** open separate conversations, arrange them together, and keep each draft and project context independent.

These examples describe available workflows; they do not imply provider setup, external-service validation, or production readiness. See [the workspace guide](docs/UI_WORKSPACE.md) and [persistent task guide](docs/PERSISTENT_TASKS.md) for behavior and limits.

## Screenshots

![FourBis Relay workspace](docs/images/workspace.png)

Local workspace view.

![Separate conversation windows](docs/images/workspace-chats.png)

Multiple conversations with independent drafts.

![Task graph](docs/images/workflow-graph.png)

Task dependencies and progress.

*These screenshots show a demonstration with fictional data. They do not represent provider activity or a public installation.*

## Quick start on Windows

Requirements: Python 3.11 or newer and PowerShell. Windows is the only validated installation target; portability to other systems has not been established.

```powershell
cd C:\path\to\repository\mcp-server
python -m venv .venv
& ".\.venv\Scripts\Activate.ps1"
python -m pip install --upgrade pip
pip install -e .
.\start.ps1
```

In another PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:8413/health
Start-Process http://127.0.0.1:8413/admin/
```

`start.ps1` sets local runtime defaults, including `GOOGLE_REAL=0`, before starting the server. It does not automatically read a `.env` file. Configure model credentials in the Admin UI's **Models** catalog if you want to run an expert; the server can start without a model provider, but expert execution then cannot complete. Optional remote providers receive the content included in a run.

## Learn more

- [Complete setup](docs/SETUP.md)
- [HTTP API](docs/API.md)
- [Admin UI](docs/ADMIN_UI.md)
- [Workspace behavior and keyboard access](docs/UI_WORKSPACE.md)
- [Persistent tasks, validation, and limits](docs/PERSISTENT_TASKS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Validation evidence and known limits](docs/VALIDATION.md)
- [Security boundaries](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

## Status and feedback

Relay is experimental software for local use. The documented installation and validation target Windows. The recorded test results use synthetic data and simulated providers; they do not establish production readiness, multi-user isolation, provider integration, or deployment. Read [VALIDATION.md](docs/VALIDATION.md) and [SECURITY.md](SECURITY.md) before using it with real repositories or credentials.

Found an issue or have a focused suggestion? [Open an issue](https://github.com/FourBis/4bis.relay-showcase/issues).

## Attribution and license

FourBis · Jeremías Badilla. The project is distributed under the MIT License; third-party components retain their own licenses and notices.
