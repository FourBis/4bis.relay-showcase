# Register external PRs and commit evidence

[Español](EXTERNAL_PRS.md) · [Issues and Projects](ISSUE_SYNC.en.md)

**Scope reviewed on October 26, 2026:** this workflow is implemented in the public code and integrated into `develop` through [PR #38](https://github.com/FourBis/4bis.relay-showcase/pull/38), dated October 20, with a synchronization correction in [PR #39](https://github.com/FourBis/4bis.relay-showcase/pull/39). The demo and documented tests use synthetic data and simulated GitHub responses. Availability depends on the installed version; this documentation does not establish a connected deployment. See the [evidence and limits of this review](qa/showcase-verified-status-2026-10-26.md).

In **Chat → Registrar PR externo**, register a PR created with another tool or manually. Relay verifies its repository and identity in GitHub and links it to a task. Registration does not generate code or create a worktree.

## Workflow

1. Connect your personal GitHub account in **My account**. You need current write access to the project.
2. Choose an authorized project. The form has a single field: paste the PR URL or search by `#number` or title words. Selecting a PR shows a card read from GitHub with its title, description, branches, totals and commits; nothing is typed by hand. A missing description is flagged, because it is the review context.
3. Confirm with **Registrar PR**. The record takes the PR title and the `medium` risk policy. If the PR is already registered in that project, the button opens its record instead of registering it again. The API still accepts optional `title`, `risk`, `issue` and `actor_label`; the tool label is a declaration and GitHub verifies the PR author.
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

A new head makes previous evidence historical. A dismissed review, withdrawn policy or remote error can also remove eligibility. Observations have a five-minute technical validity window. Registration alone does not enable automatic monitoring.

Admin explicitly configures `projects.defaults_json.external_pr_policy` by risk through project editing in the [API](API.md). Without a valid policy, the task cannot become eligible. Because the quick form registers with the `medium` risk, configure that entry first. Registration grants no publication, merge or deployment permission.

External registration does not execute code changes or prepare a local workspace
diff. Change the code with your chosen tool and review it in GitHub. Merged and
closed PRs can be refreshed without becoming execution tasks. The separate local
parity candidate below adds advisory review and optional publication.

## Shared issue and limits

The optional issue is verified against its identity and repository and can be shared with a Relay task. Linking it does not declare full resolution or authorize closure. See [Shared issues and synchronization](ISSUE_SYNC.en.md).

The public demo uses fictional people, repository, PRs and commits. Its controls only update a local scenario: no GitHub access, test execution or connected installation is implied. Local installations use the accounts and permissions you configure.

Tests use synthetic data and simulated GitHub responses. They do not establish branch protection, required checks, permissions or real account changes.

## Local parity candidate: advisory review and PNG evidence

**Status on November 1, 2026:** these additions are prepared on a separate local
branch. This documentation does not claim remote publication, integration,
deployment or activation. This round used static review only: no application,
DB, models, providers, unit tests or suites ran. The October evidence above
belongs to that earlier version.

The candidate preserves same-repository base/head verification, fork rejection
and fresh remote identity checks. It adds a project work register, paginated
PR/issue selectors and advisory reports bound to head, base, objective, linked
issue, evidence and verified instructions from the base commit.

Review is disabled initially. An active Admin must select an enabled model and
explicitly enable the project's review policy. The reviewer has no tools and
does not run code or tests. Its findings remain separate from independent human
review. Interrupted or invalid attempts require explicit retry with their
provenance preserved.

GitHub comment publication is a separate option, also off initially. It requires
an explicit account mode and verified identity. An uncertain response is
reconciled without automatically repeating the POST. Notifications use a local
Relay inbox for explicitly configured authorized accounts; they send nothing
to external messaging applications.

PNG evidence uses an owned temporary copy that the person downloads and uploads
manually through GitHub's interface. Relay verifies the permalink, author,
destination, marker and byte digest before cleaning its own copy. It does not
generate or upload the image, send it to the model, or count it as a test or
human approval.

See [External review, states and permissions](EXTERNAL_REVIEW.md) (Spanish) for
the implementation contract and limits. [Issues and Projects](ISSUE_SYNC.en.md)
retains its separate policy and concurrent-observation protections. Evidence
or advisory review does not authorize closing an issue.
