# FourBis Relay

[Español](README.md) · **English**

**From sales follow-up to project execution and documentation.**

FourBis Relay is a local workspace for coordinating people, repositories, and
AI agents around a company's work. Bring a repository and a team together,
keep the client's context in view, and review changes within your GitHub workflow.

It helps connect what a client needs with who does the work, what is running,
and the evidence available to review a delivery. A request can continue as a
task, conversation, code changes, and pull request, keeping its context when
feedback arrives or work resumes another day.

**Explore the [static demo](https://fourbis.github.io/4bis.relay-showcase/)** — a simulated interface with fictional data. It does not connect to Relay, call an AI provider, or run tools. Relay itself is experimental software intended for local use.

Relay is a FourBis and Jeremías Badilla portfolio project, released under the MIT License. See [LICENSE](LICENSE) for the project license and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for third-party notices.

## A workflow from client to delivery

1. **Follow the client and the opportunity.** Consult the CRM and link a client
   or deal to its projects. From there, reach the repository, its GitHub board,
   issues, and pull requests to review the associated work.
2. **Organize the team.** Register repositories and assign projects to team
   members. Admin, Subadmin, Dev, and Finance have different scopes. GitHub
   actions use the account of the person who performs them.
3. **Turn the request into work.** Open a task, optionally linked to an issue.
   Relay can plan it, execute it with the project's tools, and split large work
   into subtasks with dependencies. The team can follow progress, answer
   questions, and keep the files when pausing or resuming.
4. **Review within GitHub.** Inspect the diff and tests on the task's branch.
   When an administrator authorizes publication, Relay validates the commit
   and prepares its pull request. Optional tracking can pick up comments,
   requested changes, and CI failures, then correct and update the same PR.
5. **Keep what the team learns.** The conversation, results, tests, and requested
   documentation stay with the project. Ask for an explanation, correct a
   detail, or continue on the same task while retaining its connection to the work.

The team defines the scope and decides what to deliver. The current commercial
integration reads the configured CRM and lets you link its records to projects;
turning an opportunity into work requires that explicit decision. Publishing a
pull request leaves the change awaiting review: merging and deployment require
their own authorization. PR tracking is enabled with time, usage, and iteration limits.

## An example

A client requests an application migration. Link the opportunity to the project,
assign the repository to the team, and open a task linked to its issue. Relay
organizes the migration into tasks; when a large task needs to be broken down,
it can split it during execution. Review the changes and checks, request the
migration documentation, and authorize a pull request. Review adjustments
continue on that same work.

This workflow can serve a large migration, a focused fix, or ongoing maintenance
across several client projects.

## A process that evolves

Relay grew out of everyday work at FourBis. It has developed alongside model
capabilities and the team's needs for organizing, executing, and documenting
projects. Its choices around continuity, permissions, and review reflect that
experience of using it.

Choose models for different stages and configure tools for each project. This
separation allows new capabilities to be incorporated while keeping the project, team,
and GitHub workflow as the reference. Each new integration needs configuration
and verification in the process where it will be used.

See [team access](docs/TEAM_ACCESS.md), [personal accounts](docs/USER_ACCOUNTS.md),
[persistent tasks and PRs](docs/PERSISTENT_TASKS.md), and [long-running work](docs/LONG_RUNNING_WORK.md)
for behavior and limits (Spanish). The [workspace guide](docs/UI_WORKSPACE.md)
shows how to keep chats, graphs, files, and results in view.

## Screenshots

![FourBis Relay workspace](docs/images/workspace.png)

Local workspace view.

![Separate conversation windows](docs/images/workspace-chats.png)

Multiple conversations with independent drafts.

![Task graph](docs/images/workflow-graph.png)

Task dependencies and progress.

![Team: explicit project access](docs/images/team-access.png)

Team roles and project assignment, using fictional identities.

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
- [Long-running work and task subdivision](docs/LONG_RUNNING_WORK.md) (Spanish)
- [Team access and project assignment](docs/TEAM_ACCESS.md) (Spanish)
- [Personal GitHub and Google/Gmail accounts](docs/USER_ACCOUNTS.md) (Spanish)
- [Architecture](docs/ARCHITECTURE.md)
- [Validation evidence and known limits](docs/VALIDATION.md)
- [Security boundaries](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

## Status and feedback

Relay is experimental software for local use. The documented installation and validation target Windows. The recorded test results use synthetic data and simulated providers; they do not establish production readiness, multi-user isolation, provider integration, or deployment. Read [VALIDATION.md](docs/VALIDATION.md) and [SECURITY.md](SECURITY.md) before using it with real repositories or credentials.

Found an issue or have a focused suggestion? [Open an issue](https://github.com/FourBis/4bis.relay-showcase/issues).

## Attribution and license

FourBis · Jeremías Badilla. The project is distributed under the MIT License; third-party components retain their own licenses and notices.
