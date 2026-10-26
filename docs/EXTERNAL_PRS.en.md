# Register external PRs and commit evidence

[Español](EXTERNAL_PRS.md) · [Issues and Projects](ISSUE_SYNC.en.md)

**Scope reviewed on October 26, 2026:** this workflow is implemented in the public code and integrated into `develop` through [PR #38](https://github.com/FourBis/4bis.relay-showcase/pull/38), dated October 20, with a synchronization correction in [PR #39](https://github.com/FourBis/4bis.relay-showcase/pull/39). The demo and documented tests use synthetic data and simulated GitHub responses. Availability depends on the installed version; this documentation does not establish a connected deployment. See the [evidence and limits of this review](qa/showcase-verified-status-2026-10-26.md).

In **Chat → Registrar PR externo**, register a PR created with another tool or manually. Relay verifies its repository and identity in GitHub and links it to a task. Registration does not generate code or create a worktree.

## Workflow

1. Connect your personal GitHub account in **My account**. You need current write access to the project.
2. Choose an authorized project, enter the canonical PR URL and describe the goal. The task title is separate from the GitHub title.
3. Declare the risk and optionally the tool used and an issue in the same repository. The tool label is a declaration; GitHub verifies the PR author.
4. Use **Declarar evidencia** to describe your checks and their results for the displayed head SHA. The declaration records your identity and that commit.
5. Use **Actualizar desde GitHub** to recheck state, head/base, checks and reviews. Previous observations and declarations remain in history.

Failed requests preserve the form. Retrying the same evidence declaration keeps its request identifier. Duplicate registration reconciles with the existing task.

## Evidence and eligibility

| Data | Meaning |
| --- | --- |
| Human declaration | Describes checks a person says they performed. Relay did not run them. |
| GitHub check | Records the result reported by GitHub for its commit SHA. |
| Human review | Must be independent of the author and responsible person and apply to the current head. A bot does not replace it. |
| Apto en Relay | The latest observation meets the configured risk policy. It does not block or authorize GitHub merges. |

A new head makes previous evidence historical. A dismissed review, withdrawn policy or remote error can also remove eligibility. Observations have a five-minute technical validity window; registration is not continuous monitoring.

Admin explicitly configures `projects.defaults_json.external_pr_policy` by risk through project editing in the [API](API.md). Without a valid policy, the task cannot become eligible. Registration grants no publication, merge or deployment permission.

External registrations do not expose an executor, publication, automatic follow-up or a local workspace diff. Change the code with your chosen tool and review it in GitHub. Merged and closed PRs can be refreshed without turning their registrations into execution tasks.

## Shared issue and limits

The optional issue is verified against its identity and repository and can be shared with a Relay task. Linking it does not declare full resolution or authorize closure. See [Shared issues and synchronization](ISSUE_SYNC.en.md).

The public demo uses fictional people, repository, PRs and commits. Its controls only update a local scenario: no GitHub access, test execution or connected installation is implied. Local installations use the accounts and permissions you configure.

Tests use synthetic data and simulated GitHub responses. They do not establish branch protection, required checks, permissions or real account changes.
