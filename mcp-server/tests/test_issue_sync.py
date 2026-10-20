"""Real SQLite journal with fake GitHub: no test mutates external resources."""
from __future__ import annotations

import asyncio
import copy

import pytest

from relay import github, issue_sync, issue_sync_store as store
from relay.db import Database


SHA_A, SHA_B = "a" * 40, "b" * 40
LINK = {"repo": "AuroraDemo/relay-demo", "repo_id": 42, "number": 7, "node_id": "I_test",
        "url": "https://github.com/AuroraDemo/relay-demo/issues/7", "title": "Issue", "state": "OPEN"}
POLICY = {"enabled": True,
          "issue_states": {"started": "open", "review": "open", "changes_requested": "open", "merged": "closed", "cancelled": "open"},
          "projects": [{"project_id": "P_test", "field_id": "F_test",
                        "options": {key: key for key in ("started", "review", "changes_requested", "merged", "cancelled")}}]}
HUMAN = {"relay_actor": "owner", "github_id": 9, "github_login": "human"}


def pr(number=1, *, sha=SHA_A, merged=False, state="OPEN", draft=False, reviews=None):
    return {"number": number, "url": f"https://github.com/AuroraDemo/relay-demo/pull/{number}",
            "head_sha": sha, "merged": merged, "state": "MERGED" if merged else state,
            "draft": draft, "reviews": reviews or [], "author": {"id": 11, "type": "User"}}


class FakeGitHub:
    def __init__(self):
        self.issue = {**LINK, "state": "open"}
        self.project_value = None
        self.prs = {}
        self.effects = []
        self.fail_before = False
        self.fail_after = False

    async def validate_policy(self, project, value):
        return copy.deepcopy(value or {"enabled": False, "issue_states": {}, "projects": []})

    async def read_issue(self, link):
        assert link["repo_id"] == 42
        return copy.deepcopy(self.issue)

    async def read_pr(self, link, url):
        return copy.deepcopy(self.prs[url])

    async def read_project_value(self, link, mapping):
        return {"item_id": "ITEM_test", "value": self.project_value,
                "project_title": "Tablero de Demo", "field_name": "Estado",
                "option_name": self.project_value or "Sin opción"}

    async def write_issue(self, link, target, **kwargs):
        self.effects.append(("issue", target))
        if self.fail_before:
            raise OSError("fake transport failed")
        self.issue["state"] = target
        if self.fail_after:
            raise OSError("fake response lost")

    async def write_project(self, link, mapping, item_id, target, **kwargs):
        self.effects.append(("project", target))
        if self.fail_before:
            raise OSError("fake transport failed")
        self.project_value = target
        if self.fail_after:
            raise OSError("fake response lost")


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db = Database(path=tmp_path / "sync.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path / "repo"),
                             "defaults_json": {"issue_sync_policy": copy.deepcopy(POLICY)}})
    fake = FakeGitHub()
    for name in ("validate_policy", "read_issue", "read_pr", "read_project_value", "write_issue", "write_project"):
        monkeypatch.setattr(issue_sync.remote, name, getattr(fake, name))

    async def repo_slug(_):
        return "AuroraDemo/relay-demo"
    monkeypatch.setattr(github, "repo_slug", repo_slug)

    async def create(cid="task-1", *, remote_pr=None, complete=False, mode="write"):
        await db.create_conversation(project_slug="demo", conversation_id=cid, requested_by="owner")
        await db.set_conversation_issue(cid, LINK["number"])
        await db.update_conversation_task(cid, mode=mode, state="running", issue_link=copy.deepcopy(LINK),
                                          resolution_complete=complete, resolution_actor=HUMAN if complete else None)
        if remote_pr:
            fake.prs[remote_pr["url"]] = remote_pr
            await db.set_conversation_pr(cid, remote_pr["url"])
        return cid

    async def authorize():
        return await db.get_project("demo")

    async def refresh(cid="task-1", **kwargs):
        return await issue_sync.refresh(db, await db.get_project("demo"), cid, "owner",
                                        authorize=kwargs.get("authorize", authorize))

    return db, fake, create, refresh


async def retry_now(db):
    await db.run("UPDATE issue_sync_events SET next_retry_at=0")


async def test_disabled_policy_only_observes_remote_truth(env):
    db, fake, create, refresh = env
    await create(remote_pr=pr())
    project = await db.get_project("demo")
    await db.upsert_project({**project, "defaults_json": {"issue_sync_policy": {"enabled": False, "issue_states": {}, "projects": []}}})
    result = await refresh()
    assert result["status"] == "disabled"
    assert result["observed"]["pr"]["head_sha"] == SHA_A
    assert result["observed"]["issue"]["state"] == "open"
    assert result["events"] == [] and fake.effects == []


async def test_shared_started_review_changes_requested_and_no_echo(env):
    db, fake, create, refresh = env
    await create()
    assert (await refresh())["status"] == "synced"
    assert fake.effects == [("project", "started")]
    await db.set_conversation_pr("task-1", pr()["url"])
    fake.prs[pr()["url"]] = pr()
    await refresh()
    await refresh()
    assert fake.effects == [("project", "started"), ("project", "review")]
    review = {"user": {"id": 12, "type": "User"}, "commit_id": SHA_A, "state": "CHANGES_REQUESTED"}
    fake.prs[pr()["url"]] = pr(reviews=[review])
    assert (await refresh())["observed"]["transition"] == "changes_requested"
    assert fake.effects[-1] == ("project", "changes_requested")
    count = len(await store.list_events(db, "task-1"))
    await refresh()
    assert len(await store.list_events(db, "task-1")) == count


async def test_author_bot_and_old_head_reviews_do_not_move_to_changes_requested(env):
    _, fake, create, refresh = env
    reviews = [{"user": {"id": user, "type": kind}, "commit_id": sha, "state": "CHANGES_REQUESTED"}
               for user, kind, sha in [(11, "User", SHA_A), (13, "Bot", SHA_A), (14, "User", SHA_B)]]
    await create(remote_pr=pr(reviews=reviews))
    assert (await refresh())["observed"]["transition"] == "review"
    assert fake.project_value == "review"


async def test_chat_issue_responsible_cannot_count_as_independent_review(env):
    db, fake, create, refresh = env
    review = {"user": {"id": HUMAN["github_id"], "type": "User"}, "commit_id": SHA_A, "state": "CHANGES_REQUESTED"}
    await create(remote_pr=pr(reviews=[review]))
    await db.update_conversation_task("task-1", responsible=None, issue_link_actor=HUMAN)
    assert (await refresh())["observed"]["transition"] == "review"
    assert fake.project_value == "review"


async def test_no_callback_never_writes(env):
    _, fake, create, refresh = env
    await create()
    result = await refresh(authorize=None)
    assert fake.effects == []
    assert result["status"] == "error"
    assert "autorización" in result["error"]


async def test_read_only_task_does_not_write_even_with_enabled_policy(env):
    _, fake, create, refresh = env
    await create(mode="read_only")
    result = await refresh()
    assert fake.effects == [] and result["events"] == []


async def test_revocation_after_reads_is_checked_again_before_effect(env):
    _, fake, create, refresh = env
    await create()
    calls = 0
    async def revoked():
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise PermissionError("acceso revocado")
    result = await refresh(authorize=revoked)
    assert calls >= 2 and fake.effects == []
    assert result["status"] == "error"


async def test_closed_without_merge_does_not_close_issue_or_mark_merged(env):
    _, fake, create, refresh = env
    await create(remote_pr=pr(state="CLOSED"), complete=True)
    result = await refresh()
    assert result["observed"]["transition"] == "cancelled"
    assert fake.issue["state"] == "open" and fake.project_value == "cancelled"
    assert ("issue", "closed") not in fake.effects


async def test_partial_merge_neither_closes_issue_nor_moves_projects_to_done(env):
    _, fake, create, refresh = env
    await create(remote_pr=pr(merged=True), complete=False)
    result = await refresh()
    assert result["status"] == "error" and "resolución completa" in result["error"]
    assert fake.effects == []


async def test_raw_boolean_without_verified_human_cannot_authorize_resolution(env):
    db, fake, create, refresh = env
    await create(remote_pr=pr(merged=True))
    await db.update_conversation_task("task-1", resolution_complete=True)
    assert (await refresh())["status"] == "error"
    assert fake.effects == []


async def test_all_linked_prs_must_merge_and_later_dependency_change_rechecks(env):
    _, fake, create, refresh = env
    await create(remote_pr=pr(1, merged=True), complete=True)
    await create("task-2", remote_pr=pr(2), complete=False)
    blocked = await refresh()
    assert "sin merge" in blocked["error"] and fake.effects == []
    fake.prs[pr(2)["url"]] = pr(2, merged=True)
    result = await refresh()
    assert result["status"] == "synced"
    assert fake.issue["state"] == "closed" and fake.project_value == "merged"
    await refresh()
    assert fake.effects.count(("issue", "closed")) == 1


async def test_legacy_numeric_link_remains_blocking_dependency(env):
    db, fake, create, refresh = env
    await create(remote_pr=pr(merged=True), complete=True)
    await db.create_conversation(project_slug="demo", conversation_id="legacy")
    await db.set_conversation_issue("legacy", 7)
    result = await refresh()
    assert result["status"] == "error" and fake.effects == []


@pytest.mark.parametrize("unavailable", ["project", "path", "remote"])
async def test_unverifiable_legacy_repository_blocks_complete_resolution(env, monkeypatch, unavailable):
    db, fake, create, refresh = env
    await create(remote_pr=pr(merged=True), complete=True)
    await db.upsert_project({"slug": "legacy", "name": "Legacy", "repo_path": "legacy-repo"})
    await db.create_conversation(project_slug="legacy", conversation_id="legacy")
    await db.set_conversation_issue("legacy", LINK["number"])
    get_project = db.get_project

    async def project(slug):
        value = await get_project(slug)
        if slug == "legacy":
            return None if unavailable == "project" else {**value, "repo_path": "" if unavailable == "path" else "legacy-repo"}
        return value

    async def repo_slug(path):
        return None if path in {"", "legacy-repo"} else LINK["repo"]

    monkeypatch.setattr(db, "get_project", project)
    monkeypatch.setattr(github, "repo_slug", repo_slug)
    result = await refresh()
    assert result["status"] == "error"
    assert result["observed"]["resolution_complete"] is False
    assert any(item["conversation_id"] == "legacy" and not item["merged"]
               for item in result["observed"]["dependencies"])
    assert fake.effects == [] and fake.issue["state"] == "open"


async def test_dependency_cap_counts_only_matching_repository(env):
    db, fake, create, refresh = env
    await create(remote_pr=pr(merged=True), complete=True)
    for number in range(101):
        cid = f"unrelated-{number}"
        await create(cid)
        await db.update_conversation_task(cid, issue_link={**LINK, "repo_id": 99, "repo": "Other/demo"})
    result = await refresh()
    assert result["status"] == "synced" and fake.issue["state"] == "closed"
    assert len(result["observed"]["dependencies"]) == 1


async def test_dependency_cap_still_blocks_matching_repository(env):
    db, fake, create, refresh = env
    await create(remote_pr=pr(merged=True), complete=True)
    for number in range(100):
        await create(f"related-{number}")
    with pytest.raises(issue_sync.ConflictError, match="100"):
        await refresh()
    assert fake.effects == []


@pytest.mark.parametrize("policy_enabled", [True, False])
async def test_current_writer_reconciles_uncertainty_preserving_original_actor(env, policy_enabled):
    db, fake, create, refresh = env
    await create(remote_pr=pr())
    fake.fail_after = True
    assert (await refresh())["status"] == "uncertain"
    fake.fail_after = False
    await db.run("UPDATE issue_sync_events SET read_attempts=3,next_retry_at=99999999999 WHERE state='uncertain'")
    if not policy_enabled:
        project = await db.get_project("demo")
        disabled = copy.deepcopy(POLICY)
        disabled["enabled"] = False
        await db.upsert_project({**project, "defaults_json": {"issue_sync_policy": disabled}})

    async def authorize():
        return await db.get_project("demo")

    await issue_sync.retry(db, "task-1", "replacement@example.test", authorize=authorize)
    result = await issue_sync.refresh(db, await db.get_project("demo"), "task-1", "replacement@example.test", authorize=authorize)
    assert result["status"] == ("synced" if policy_enabled else "disabled")
    assert fake.effects == [("project", "review")]
    old = next(item for item in await store.list_events(db, "task-1") if item["attempts"] == 1)
    assert old["actor"] == "owner" and old["reconciled_by"] == "replacement@example.test"
    assert old["state"] == "applied"


@pytest.mark.parametrize("revoked", [False, True])
async def test_another_actor_cannot_reconcile_without_current_authorization(env, revoked):
    db, fake, create, refresh = env
    await create(remote_pr=pr())
    fake.fail_after = True
    assert (await refresh())["status"] == "uncertain"
    fake.fail_after = False
    await retry_now(db)

    async def denied():
        raise PermissionError("acceso revocado")

    result = await issue_sync.refresh(db, await db.get_project("demo"), "task-1", "replacement@example.test",
                                      authorize=denied if revoked else None)
    assert result["status"] == "uncertain" and fake.effects == [("project", "review")]
    assert any(item["state"] == "uncertain" for item in await store.list_events(db, "task-1"))


async def test_explicit_retry_by_another_actor_never_resets_exhausted_write_budget(env):
    db, fake, create, refresh = env
    await create()
    fake.fail_before = True
    for _ in range(4):
        await refresh()
        await retry_now(db)
    assert len(fake.effects) == 3

    async def authorize():
        return await db.get_project("demo")

    await issue_sync.retry(db, "task-1", "replacement@example.test", authorize=authorize)
    await issue_sync.refresh(db, await db.get_project("demo"), "task-1", "replacement@example.test", authorize=authorize)
    events = await store.list_events(db, "task-1")
    assert len(fake.effects) == 3
    assert any(item["attempts"] == 3 and item["state"] == "blocked" for item in events)


async def test_conflict_before_effect_replans_only_after_explicit_retry(env, monkeypatch):
    db, fake, create, refresh = env
    await create(remote_pr=pr())
    original = issue_sync._current
    changed = False

    async def manual_change(action):
        nonlocal changed
        if action["target"].startswith("project:") and not changed:
            changed = True
            fake.project_value = "manual-option"
        return await original(action)

    monkeypatch.setattr(issue_sync, "_current", manual_change)
    result = await refresh()
    assert result["status"] == "error" and fake.effects == []
    for _ in range(2):
        await refresh()
    assert fake.effects == [] and fake.project_value == "manual-option"

    async def authorize():
        return await db.get_project("demo")

    await issue_sync.retry(db, "task-1", "owner", authorize=authorize)
    result = await refresh()
    assert result["status"] == "synced" and fake.project_value == "review"
    assert fake.effects == [("project", "review")]
    events = await store.list_events(db, "task-1")
    assert any(item["state"] == "superseded" and item["attempts"] == 0
               and item["payload"]["target"].startswith("project:") for item in events)


@pytest.mark.parametrize("after_attempt", [False, True])
async def test_effect_account_error_propagates_without_losing_durable_outcome(env, monkeypatch, after_attempt):
    from relay.user_accounts import AccountError
    db, fake, create, refresh = env
    await create(remote_pr=pr())

    async def missing_account(*args, **kwargs):
        raise AccountError("Reconecta GitHub")

    monkeypatch.setattr(issue_sync.remote, "write_project", missing_account) if after_attempt else monkeypatch.setattr(issue_sync, "_current", missing_account)
    with pytest.raises(AccountError, match="Reconecta"):
        await refresh()
    events = await store.list_events(db, "task-1")
    assert any(item["state"] == ("uncertain" if after_attempt else "blocked")
               and item["attempts"] == int(after_attempt) for item in events)
    assert fake.effects == []


async def test_uncertain_applied_effect_reconciles_after_reopen_without_replay(env):
    db, fake, create, refresh = env
    await create(remote_pr=pr())
    fake.fail_after = True
    result = await refresh()
    assert result["status"] == "uncertain" and fake.project_value == "review"
    assert fake.effects == [("project", "review")]
    await retry_now(db)
    fake.fail_after = False
    reopened = Database(path=db.path)
    await issue_sync.recover(reopened)
    async def auth():
        return await reopened.get_project("demo")
    result = await issue_sync.refresh(reopened, await reopened.get_project("demo"), "task-1", "owner", authorize=auth)
    assert result["status"] == "synced"
    assert fake.effects == [("project", "review")]


async def test_uncertain_unapplied_effect_has_bounded_retries(env):
    db, fake, create, refresh = env
    await create()
    fake.fail_before = True
    for _ in range(4):
        await refresh()
        await retry_now(db)
    assert len(fake.effects) == 3
    result = await refresh()
    assert result["status"] == "error" and "tres intentos" in result["error"]


async def test_manual_change_after_uncertain_call_is_not_overwritten(env):
    db, fake, create, refresh = env
    await create()
    fake.fail_before = True
    await refresh()
    fake.fail_before = False
    fake.project_value = "manual-option"
    await retry_now(db)
    result = await refresh()
    assert result["status"] == "uncertain" and "fuera de Relay" in result["error"]
    assert len(fake.effects) == 1 and fake.project_value == "manual-option"


async def test_new_sha_supersedes_uncertain_old_intent_before_retry(env):
    db, fake, create, refresh = env
    await create(remote_pr=pr())
    fake.fail_before = True
    await refresh()
    fake.fail_before = False
    fake.prs[pr()["url"]] = pr(sha=SHA_B)
    await retry_now(db)
    result = await refresh()
    assert result["status"] == "synced"
    events = await store.list_events(db, "task-1")
    assert any(event["state"] == "superseded" and event["payload"]["head_sha"] == SHA_A for event in events)
    assert len(fake.effects) == 2


async def test_manual_closed_issue_never_reopens_or_moves_board(env):
    _, fake, create, refresh = env
    await create()
    fake.issue["state"] = "closed"
    result = await refresh()
    assert result["status"] == "error" and fake.effects == []


async def test_manual_project_change_after_success_is_visible_conflict_not_sync(env):
    _, fake, create, refresh = env
    await create(remote_pr=pr())
    assert (await refresh())["status"] == "synced"
    fake.project_value = "manual-option"
    for _ in range(2):
        result = await refresh()
        assert result["status"] == "error" and "fuera de Relay" in result["error"]
        assert result["observed"]["projects"][0]["option_id"] == "manual-option"
        assert fake.effects == [("project", "review")]


async def test_manual_project_conflict_can_be_acknowledged_only_by_explicit_retry(env):
    db, fake, create, refresh = env
    await create(remote_pr=pr())
    await refresh()
    fake.project_value = "manual-option"
    await refresh()
    await refresh()
    async def authorize():
        return await db.get_project("demo")
    assert (await issue_sync.retry(db, "task-1", "owner", authorize=authorize))["status"] == "pending"
    result = await refresh()
    assert result["status"] == "synced" and fake.project_value == "review"
    assert fake.effects == [("project", "review"), ("project", "review")]


async def test_manual_project_conflict_can_replan_when_transition_changes(env):
    _, fake, create, refresh = env
    await create(remote_pr=pr())
    await refresh()
    fake.project_value = "manual-option"
    await refresh()
    await refresh()
    review = {"user": {"id": 12, "type": "User"}, "commit_id": SHA_A, "state": "CHANGES_REQUESTED"}
    fake.prs[pr()["url"]] = pr(reviews=[review])
    result = await refresh()
    assert result["status"] == "synced" and fake.project_value == "changes_requested"
    assert fake.effects == [("project", "review"), ("project", "changes_requested")]


async def test_post_effect_observation_reports_actual_issue_and_board(env):
    _, fake, create, refresh = env
    await create(remote_pr=pr(merged=True), complete=True)
    result = await refresh()
    assert result["observed"]["issue"]["state"] == "closed"
    assert result["observed"]["projects"][0]["option_id"] == "merged"
    assert result["observed"]["projects"][0]["title"] == "Tablero de Demo"
    assert result["observed"]["projects"][0]["field_name"] == "Estado"
    assert result["observed"]["projects"][0]["option_name"] == "merged"
    assert result["status"] == "synced"


async def test_background_authorization_error_is_not_hidden_by_old_success(env):
    db, _, create, refresh = env
    await create()
    await refresh()
    await db.update_conversation_task("task-1", issue_sync_error="cuenta revocada")
    result = await issue_sync.snapshot(db, "task-1")
    assert result["status"] == "error" and result["error"] == "cuenta revocada"


async def test_malformed_policy_fails_closed_without_attribute_error(env):
    db, fake, create, refresh = env
    await create()
    project = await db.get_project("demo")
    await db.upsert_project({**project, "defaults_json": {"issue_sync_policy": "invalid"}})
    result = await refresh()
    assert result["status"] == "disabled" and fake.effects == []


async def test_transient_pre_effect_reads_retry_with_backoff_and_bound(env, monkeypatch):
    db, fake, create, refresh = env
    await create()
    original = issue_sync._current
    async def unavailable(_):
        raise issue_sync.IssueSyncError("lectura temporalmente no disponible")
    monkeypatch.setattr(issue_sync, "_current", unavailable)
    for count in range(1, 4):
        result = await refresh()
        assert fake.effects == []
        assert max(item["read_attempts"] for item in result["events"]) == count
        await retry_now(db)
    assert result["status"] == "error"
    monkeypatch.setattr(issue_sync, "_current", original)
    async def auth():
        return await db.get_project("demo")
    await issue_sync.retry(db, "task-1", "owner", authorize=auth)
    assert (await refresh())["status"] == "synced"
    assert fake.effects == [("project", "started")]


async def test_disabling_policy_reconciles_applied_uncertainty_read_only(env):
    db, fake, create, refresh = env
    await create(remote_pr=pr())
    fake.fail_after = True
    assert (await refresh())["status"] == "uncertain"
    project = await db.get_project("demo")
    disabled = copy.deepcopy(POLICY)
    disabled["enabled"] = False
    await db.upsert_project({**project, "defaults_json": {"issue_sync_policy": disabled}})
    result = await refresh()
    assert result["enabled"] is False and result["policy_enabled"] is False
    assert result["status"] == "disabled"
    assert fake.effects == [("project", "review")]
    assert all(item["state"] in {"applied", "superseded"} for item in result["events"])


async def test_disabling_policy_does_not_hide_or_replay_unconfirmed_effect(env):
    db, fake, create, refresh = env
    await create()
    fake.fail_before = True
    assert (await refresh())["status"] == "uncertain"
    project = await db.get_project("demo")
    await db.upsert_project({**project, "defaults_json": {"issue_sync_policy": None}})
    fake.fail_before = False
    result = await refresh()
    assert result["enabled"] is False and result["status"] == "uncertain"
    assert "no se repite" in result["error"]
    assert fake.effects == [("project", "started")]


async def test_concurrent_workers_only_claim_one_issue_target(env):
    db, fake, create, refresh = env
    await create(remote_pr=pr())
    await asyncio.gather(refresh(), refresh(), refresh())
    assert fake.effects == [("project", "review")]
    assert (await issue_sync.snapshot(db, "task-1"))["status"] == "synced"


async def test_recover_marks_inflight_intent_uncertain_without_effect(env):
    db, fake, create, refresh = env
    await create()
    await refresh(authorize=None)
    await db.run("UPDATE issue_sync_events SET state='processing' WHERE state='blocked'")
    await issue_sync.recover(db)
    assert (await issue_sync.snapshot(db, "task-1"))["status"] == "uncertain"
    assert fake.effects == []
