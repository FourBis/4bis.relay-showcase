# Shared issues and GitHub synchronization

[Español](ISSUE_SYNC.md) · [External PRs](EXTERNAL_PRS.en.md)

A Relay conversation and an external PR can share an issue in the authorized repository. The link retains verified canonical identity: repository and ID, issue number, node ID, URL, title and state.

## Link work to an issue

When creating a conversation or registering an external PR, choose an issue or enter its number or canonical GitHub URL. The server verifies repository, identity and access before saving. A failed list request preserves your input and allows retry or direct entry.

An existing task offers **Vincular issue** or **Editar vínculo / resolución**. **Este trabajo resuelve la issue completa** is unchecked initially; checking it records an attributed human declaration. Changing the reference unchecks it again.

Partial work keeps the issue open. Closure requires a human declaration of complete resolution and verified merges for every linked PR and task. Closing a PR without merge, completing a run or model prose does not resolve an issue. Historical links with unverified identity block closure until checked.

Legacy conversations must first be imported as persistent tasks; linking an issue does not silently convert them.

## Explicit Projects policy

The **Management** board remains read-only. Task synchronization is separate: Admin reviews a per-project policy in `projects.defaults_json.issue_sync_policy`. It starts disabled. Linking an issue does not enable remote changes.

The [API](API.md) provides discovery of Projects, fields and options and policy configuration. Use verified exact IDs; column names are not inferred. Only existing Projects V2 single-select fields are supported. The issue must already belong to the Project; Relay does not create its item.

| Verified transition | Issue | Projects, with an enabled policy only |
| --- | --- | --- |
| Work started or Draft PR | Remains open. | Configured `started` option. |
| Open PR outside Draft | Review does not establish approval or resolution. | `review`. |
| Independent reviewer requests changes for the current head | Remains open. | `changes_requested`. |
| Merge | Closure still requires complete resolution and all links verified. | `merged`, subject to policy. |
| Cancellation or PR closed without merge | Does not resolve the issue. | `cancelled`. |

The synchronizer does not reopen manually closed issues. Newly generated PR text uses neutral references. Closing keywords in external PRs, historical commits or other GitHub automation can still operate independently.

## Observe, synchronize and retry

Opening the panel reads recorded state. **Actualizar sincronización** reevaluates remote facts and, with an enabled policy, can apply the authorized mapping. Synchronization state is separate from code evidence and approval.

The monitor checks eligible tasks every 60 seconds; it is not a webhook. Read failures permit up to three attempts with a 60-second backoff. **Revisar y reintentar** asks a person to inspect and confirm remote state: it can replan after a manual conflict, retains write limits and does not discard uncertainty.

A timeout or interruption after an attempted write leaves an uncertain result. Before repeating, Relay rereads the target and revalidates preconditions and permissions. Each event permits at most three write attempts. Disabled policies allow read reconciliation of previous uncertainty, without starting new effects. In-flight or uncertain effects freeze links that determine closure.

Incompatible manual changes are not overwritten automatically. Another authorized operator can reconcile uncertainty, retaining the original actor and reconciler separately.

Projects inspection is limited to 100 elements per queried connection. More pages or incomplete data require manual review. PR inspection permits up to 99 reviews, and each issue permits up to 100 task links. Reaching a limit never proves there are no dependencies.

## Demo and real configuration

The public demo simulates shared issues, commit evidence and an initially disabled policy. IDs and people are fictional; it makes no remote requests. Installing the server does not configure production policies, credentials, apps, webhooks, rules or boards.

Tests use simulated GitHub and temporary databases. Review [security](../SECURITY.md) and personal accounts before enabling a policy in your installation.

