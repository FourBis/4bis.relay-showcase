"""External PR registration never executes code or turns a green check into review."""
from __future__ import annotations

import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from relay import external_pr, github, identity
from relay.app_state import DB_KEY
from relay.db import Database


READ_SNAPSHOT = external_pr.read_snapshot
ACTOR_IDENTITY = external_pr.actor_identity
SHA_A, SHA_B, BASE = "a" * 40, "b" * 40, "c" * 40
URL = "https://github.com/AuroraDemo/relay-demo/pull/17"
WHO = "dev@example.test"
ACTOR = {"relay_actor": WHO, "github_id": 123, "github_login": "dev"}
POLICY = {"low": {"required_checks": ["CI / tests"], "require_declaration": True}}


def remote_snapshot(sha=SHA_A, *, approved=False, reviewer_id=456, reviewer_type="User"):
    return {
        "repo": "AuroraDemo/relay-demo", "repo_id": 42, "number": 17, "url": URL,
        "title": "External change", "state": "OPEN", "draft": False,
        "base_sha": BASE, "head_sha": sha, "base_ref": "develop", "head_ref": "bot/change",
        "author": {"id": 11, "login": "author", "type": "User"},
        "checks": [{"name": "CI / tests", "head_sha": sha, "status": "completed",
                    "conclusion": "success", "html_url": "https://github.com/AuroraDemo/relay-demo/actions/runs/1"}],
        "reviews": ([{"id": 99, "user": {"id": reviewer_id, "login": "reviewer", "type": reviewer_type},
                      "state": "APPROVED", "commit_id": sha, "submitted_at": "2026-10-20T12:00:00Z"}]
                    if approved else []),
        "observed_at": external_pr.now(),
    }


@pytest.fixture
async def registration(tmp_path, monkeypatch):
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path / "repo"),
                             "defaults_json": {"external_pr_policy": POLICY}})
    project = await db.get_project("demo")
    remote = remote_snapshot()
    monkeypatch.setattr(external_pr, "read_snapshot", AsyncMock(side_effect=lambda *_, **__: copy.deepcopy(remote)))
    monkeypatch.setattr(external_pr, "actor_identity", AsyncMock(side_effect=lambda who: {**ACTOR, "relay_actor": who}))
    body = {"url": URL, "title": "External change", "risk": "low", "actor_label": "Any coding bot"}
    return db, project, body, remote


async def register(registration):
    db, project, body, _ = registration
    return await external_pr.register(db, project, body, WHO)


async def declare(registration, cid, *, sha=SHA_A, key="evidence-1", summary="pytest: 12 passed"):
    db, project, _, _ = registration
    return await external_pr.add_evidence(db, project, cid,
        {"request_id": key, "head_sha": sha, "summary": summary}, WHO)


async def test_external_registration_has_no_workspace_and_does_not_occupy_repo(registration):
    db, project, _, _ = registration
    task = await register(registration)
    cid = task["id"]
    saved = await db.get_conversation_task(cid)
    assert saved["mode"] == "external_pr"
    assert not saved.get("workspace_path")
    assert await db.get_open_conversation_for_workspace(project) is None
    assert await db.get_open_conversation_for_project("demo") is None
    assert await db.list_conversation_events(cid) == []
    assert (await db.get_conversation(cid))["requested_by"] == WHO
    assert task["assessment"]["state"] == "missing_evidence"


async def test_registration_duplicate_concurrent_and_after_database_reopen(registration):
    db, project, body, _ = registration
    other = Database(path=db.path)
    first, second = await asyncio.gather(
        external_pr.register(db, project, body, WHO),
        external_pr.register(other, project, body, WHO))
    assert first["id"] == second["id"]
    third = await external_pr.register(Database(path=db.path), project, body, WHO)
    assert third["id"] == first["id"]
    assert len(await db.run("SELECT id FROM conversations")) == 1


async def test_registration_failure_leaves_no_orphan_and_retry_creates_once(registration, monkeypatch):
    db, project, body, remote = registration
    monkeypatch.setattr(external_pr, "read_snapshot", AsyncMock(side_effect=external_pr.TaskPRError("GitHub failed")))
    with pytest.raises(external_pr.TaskPRError):
        await register(registration)
    assert await db.run("SELECT id FROM conversations") == []
    monkeypatch.setattr(external_pr, "read_snapshot", AsyncMock(return_value=copy.deepcopy(remote)))
    first = await register(registration)
    assert (await external_pr.register(db, project, body, WHO))["id"] == first["id"]
    assert len(await db.run("SELECT id FROM conversations")) == 1


async def test_green_checks_declaration_and_independent_review_are_separate(registration):
    db, project, _, remote = registration
    task = await register(registration)
    cid = task["id"]
    assert task["assessment"]["state"] != "ready"
    task = await declare(registration, cid)
    assert task["assessment"]["state"] == "review_pending"
    remote.update(remote_snapshot(approved=True))
    task = await external_pr.refresh(db, project, cid, WHO)
    assert task["assessment"]["state"] == "ready"
    assert task["evidence"][0]["actor"] == ACTOR
    assert task["evidence"][0]["head_sha"] == SHA_A


@pytest.mark.parametrize("reviewer_id,reviewer_type", [(11, "User"), (123, "User"), (456, "Bot")])
async def test_author_responsible_and_bot_are_not_independent_reviewers(
        registration, reviewer_id, reviewer_type):
    db, project, _, remote = registration
    task = await register(registration)
    await declare(registration, task["id"])
    remote.update(remote_snapshot(approved=True, reviewer_id=reviewer_id, reviewer_type=reviewer_type))
    task = await external_pr.refresh(db, project, task["id"], WHO)
    assert task["assessment"]["state"] == "review_pending"


async def test_new_head_invalidates_old_evidence_and_review_without_losing_history(registration):
    db, project, _, remote = registration
    task = await register(registration)
    cid = task["id"]
    await declare(registration, cid)
    remote.update(remote_snapshot(approved=True))
    assert (await external_pr.refresh(db, project, cid, WHO))["assessment"]["state"] == "ready"
    remote.update(remote_snapshot(SHA_B))
    task = await external_pr.refresh(db, project, cid, WHO)
    assert task["head_sha"] == SHA_B
    assert task["assessment"]["state"] == "missing_evidence"
    assert task["evidence"][0]["head_sha"] == SHA_A
    history = json.dumps(task["history"])
    assert SHA_A in history and SHA_B in history
    with pytest.raises((ValueError, RuntimeError)):
        await declare(registration, cid, key="stale", sha=SHA_A)
    task = await declare(registration, cid, key="new", sha=SHA_B)
    assert task["assessment"]["state"] == "review_pending"


async def test_evidence_retry_is_idempotent_and_cannot_rewrite_receipt(registration):
    task = await register(registration)
    first = await declare(registration, task["id"])
    second = await declare(registration, task["id"])
    assert first["evidence"] == second["evidence"]
    assert len(second["evidence"]) == 1
    with pytest.raises((ValueError, RuntimeError)):
        await declare(registration, task["id"], summary="Different claim")


async def test_concurrent_evidence_preserves_both_declarations_and_history(registration):
    db, _, _, _ = registration
    task = await register(registration)
    await asyncio.gather(declare(registration, task["id"], key="first"),
                         declare(registration, task["id"], key="second"))
    saved = await db.get_conversation_task(task["id"])
    assert {entry["request_id"] for entry in saved["evidence"]} == {"first", "second"}
    assert sum(row["kind"] == "declared_evidence" for row in saved["history"]) == 2


@pytest.mark.parametrize("latest_state", ["DISMISSED", "CHANGES_REQUESTED"])
async def test_later_review_state_replaces_old_approval(registration, latest_state):
    db, project, _, remote = registration
    task = await register(registration)
    await declare(registration, task["id"])
    remote.update(remote_snapshot(approved=True))
    previous = remote["reviews"][0]
    remote["reviews"].append({**previous, "id": 100, "state": latest_state,
                              "submitted_at": "2026-10-20T13:00:00Z"})
    assert (await external_pr.refresh(db, project, task["id"], WHO))["assessment"]["state"] == "review_pending"


async def test_missing_policy_never_becomes_ready(registration):
    db, project, _, remote = registration
    project = {**project, "defaults_json": {}}
    task = await external_pr.register(db, project, {"url": URL, "title": "External change", "risk": "low"}, WHO)
    remote.update(remote_snapshot(approved=True))
    await external_pr.add_evidence(db, project, task["id"],
        {"request_id": "claim", "head_sha": SHA_A, "summary": "tests passed"}, WHO)
    task = await external_pr.refresh(db, project, task["id"], WHO)
    assert task["assessment"]["state"] != "ready"


async def test_remote_failure_invalidates_readiness_then_recovers_without_duplicate(registration, monkeypatch):
    db, project, body, remote = registration
    task = await register(registration)
    cid = task["id"]
    await declare(registration, cid)
    remote.update(remote_snapshot(approved=True))
    assert (await external_pr.refresh(db, project, cid, WHO))["assessment"]["state"] == "ready"
    monkeypatch.setattr(external_pr, "read_snapshot", AsyncMock(side_effect=external_pr.TaskPRError("GitHub temporarily unavailable")))
    try:
        await external_pr.refresh(db, project, cid, WHO)
    except RuntimeError:
        pass
    failed = external_pr.snapshot(await db.get_conversation_task(cid))
    assert failed["assessment"]["state"] != "ready"
    assert failed.get("verification_error")
    assert len(failed["evidence"]) == 1
    monkeypatch.setattr(external_pr, "read_snapshot", AsyncMock(return_value=copy.deepcopy(remote)))
    recovered = await external_pr.refresh(db, project, cid, WHO)
    assert recovered["assessment"]["state"] == "ready"
    again = await external_pr.register(db, project, body, WHO)
    assert again["id"] == cid


@pytest.mark.parametrize("url", [
    "http://github.com/AuroraDemo/relay-demo/pull/17", "https://evil.test/AuroraDemo/relay-demo/pull/17",
    "https://github.com.evil.test/AuroraDemo/relay-demo/pull/17", "https://github.com@127.0.0.1/AuroraDemo/relay-demo/pull/17",
    "https://github.com:443/AuroraDemo/relay-demo/pull/17", "https://github.com/AuroraDemo/relay-demo/pull/0",
    "https://github.com/AuroraDemo/relay-demo/pull/17?url=http://127.0.0.1", "https://github.com/AuroraDemo/relay-demo/pull/17#x",
    "https://github.com/AuroraDemo/relay-demo/issues/17", "https://github.com/AuroraDemo/relay-demo/pull/17/files",
])
async def test_invalid_urls_are_rejected_before_github(registration, monkeypatch, url):
    _, project, _, _ = registration
    remote_call = AsyncMock(side_effect=AssertionError("No network for invalid URL"))
    monkeypatch.setattr(github, "_gh", remote_call)
    with pytest.raises((ValueError, RuntimeError)):
        await READ_SNAPSHOT(project, url)
    remote_call.assert_not_called()


async def test_repository_must_match_authorized_project(registration, monkeypatch):
    _, project, _, _ = registration
    monkeypatch.setattr(github, "repo_slug", AsyncMock(return_value="AuroraDemo/relay-demo"))
    async def authorized_metadata_only(*args, **kwargs):
        assert args[-1] == "repos/AuroraDemo/relay-demo", "Never query the user-supplied unrelated repository"
        return 0, json.dumps({"id": 42, "full_name": "AuroraDemo/relay-demo"})
    remote_call = AsyncMock(side_effect=authorized_metadata_only)
    monkeypatch.setattr(github, "_gh", remote_call)
    with pytest.raises((ValueError, RuntimeError)):
        await READ_SNAPSHOT(project, "https://github.com/another/private/pull/17")
    remote_call.assert_awaited_once()


def github_payloads():
    repo = {"id": 42, "full_name": "AuroraDemo/relay-demo"}
    pr = {"number": 17, "html_url": URL, "title": "External change", "state": "open", "draft": False,
          "merged_at": None, "user": {"id": 11, "login": "author", "type": "User"},
          "base": {"repo": repo, "sha": BASE, "ref": "develop"},
          "head": {"sha": SHA_A, "ref": "bot/change"}}
    return {
        "user": {"id": 123, "login": "dev", "type": "User"},
        "repos/AuroraDemo/relay-demo": repo,
        "repos/AuroraDemo/relay-demo/pulls/17": pr,
        f"repos/AuroraDemo/relay-demo/commits/{SHA_A}/check-runs?per_page=100&filter=latest": {
            "total_count": 1, "check_runs": [{"id": 5, "name": "CI / tests", "head_sha": SHA_A,
                "status": "completed", "conclusion": "success", "html_url": URL}]},
        f"repos/AuroraDemo/relay-demo/commits/{SHA_A}/status?per_page=100": {
            "sha": SHA_A, "total_count": 0, "statuses": []},
        "repos/AuroraDemo/relay-demo/pulls/17/reviews?per_page=100&page=1": [],
        "repos/AuroraDemo/relay-demo/issues/12": {
            "number": 12, "repository_url": "https://api.github.com/repos/AuroraDemo/relay-demo",
            "node_id": "I_demo12", "html_url": "https://github.com/AuroraDemo/relay-demo/issues/12",
            "title": "Issue twelve", "state": "open", "updated_at": "2026-10-20T12:00:00Z"},
    }


@pytest.fixture
def github_reader(monkeypatch):
    payloads, calls = github_payloads(), []
    monkeypatch.setattr(github, "repo_slug", AsyncMock(return_value="AuroraDemo/relay-demo"))
    async def read(*args, **kwargs):
        assert args[:4] == ("api", "--hostname", "github.com", args[-1])
        endpoint = args[-1]
        assert endpoint in payloads, f"Unexpected GitHub endpoint: {endpoint}"
        calls.append(endpoint)
        return 0, json.dumps(payloads[endpoint])
    monkeypatch.setattr(github, "_gh", read)
    return payloads, calls


async def test_reader_verifies_pr_identity_shas_state_and_issue(github_reader, tmp_path):
    payloads, calls = github_reader
    result = await READ_SNAPSHOT({"repo_path": str(tmp_path)}, URL, 12)
    assert result["repo_id"] == 42 and result["repo"] == "AuroraDemo/relay-demo"
    assert result["number"] == 17 and result["head_sha"] == SHA_A and result["base_sha"] == BASE
    assert result["author"] == payloads["repos/AuroraDemo/relay-demo/pulls/17"]["user"]
    assert result["state"] == "OPEN" and result["draft"] is False
    assert calls.count("repos/AuroraDemo/relay-demo/pulls/17") == 2
    assert "repos/AuroraDemo/relay-demo/issues/12" in calls
    assert all(not endpoint.startswith("http") for endpoint in calls)


@pytest.mark.parametrize("change", ["number", "base_repo", "head_sha", "canonical_url"])
async def test_reader_rejects_mismatched_remote_identity(github_reader, tmp_path, change):
    payloads, _ = github_reader
    pr = payloads["repos/AuroraDemo/relay-demo/pulls/17"]
    if change == "number":
        pr["number"] = 18
    elif change == "base_repo":
        pr["base"]["repo"] = {"id": 55, "full_name": "another/repo"}
    elif change == "head_sha":
        pr["head"]["sha"] = "not-a-commit"
    else:
        pr["html_url"] = "https://evil.test/pull/17"
    with pytest.raises(external_pr.TaskPRError):
        await READ_SNAPSHOT({"repo_path": str(tmp_path)}, URL)


async def test_actor_identity_comes_from_github_user_not_payload(github_reader):
    assert await ACTOR_IDENTITY(WHO) == ACTOR
    github_reader[0]["user"]["type"] = "Bot"
    with pytest.raises(external_pr.TaskPRError):
        await ACTOR_IDENTITY(WHO)


async def test_github_failure_never_persists_provider_output(monkeypatch):
    secret_like_output = "provider dump: sensitive-test-token"
    monkeypatch.setattr(github, "_gh", AsyncMock(return_value=(1, secret_like_output)))
    with pytest.raises(external_pr.TaskPRError) as error:
        await ACTOR_IDENTITY(WHO)
    assert secret_like_output not in str(error.value)


@pytest.mark.parametrize("role,granted,enabled,read_only,expected", [
    ("member", True, True, False, 200), ("subadmin", True, True, False, 200),
    ("member", False, True, False, 403), ("finance", True, True, False, 403),
    ("member", True, False, False, 403), ("member", True, True, True, 403),
])
async def test_routes_enforce_project_permission_and_authenticated_attribution(
        registration, monkeypatch, role, granted, enabled, read_only, expected):
    from relay import server_common, server_external_pr_routes
    db, project, body, _ = registration
    if read_only:
        await db.upsert_project({"slug": "demo", "defaults_json": {"read_only": True}})
    monkeypatch.setattr(identity, "_roles", {WHO: role})
    monkeypatch.setattr(identity, "_disabled", set() if enabled else {WHO})
    monkeypatch.setattr(identity, "_project_grants", {WHO: ["demo"] if granted else []})
    monkeypatch.setattr(server_common, "_get_api_key", lambda: "")
    @web.middleware
    async def test_identity(request, handler):
        request[identity.IDENTITY_KEY] = WHO
        return await handler(request)
    app = web.Application(middlewares=[test_identity, identity.require_role])
    app[DB_KEY] = db
    app.router.add_post("/projects/{slug}/external-pr", server_external_pr_routes.register)
    app.router.add_post("/conversations/{id}/external-pr", server_external_pr_routes.action)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post("/projects/demo/external-pr", json={**body, "requested_by": "spoofed", "author": "spoofed"})
        assert response.status == expected
        if expected == 200:
            result = await response.json()
            assert result["responsible"] == ACTOR
            assert result["requested_by"] == WHO
            cid = result["id"]
            monkeypatch.setattr(identity, "_project_grants", {WHO: []})
            denied = await client.post(f"/conversations/{cid}/external-pr", json={"action": "refresh"})
            assert denied.status == 403
        else:
            assert not await db.run("SELECT id FROM conversations")
    finally:
        await client.close()


async def test_task_get_is_local_and_denied_after_project_grant_revocation(registration, monkeypatch):
    from relay import server_common, server_task_routes
    db, _, _, _ = registration
    task = await register(registration)
    monkeypatch.setattr(identity, "_roles", {WHO: "member"})
    monkeypatch.setattr(identity, "_disabled", set())
    monkeypatch.setattr(identity, "_project_grants", {WHO: ["demo"]})
    monkeypatch.setattr(server_common, "_get_api_key", lambda: "")
    monkeypatch.setattr(external_pr, "read_snapshot", AsyncMock(side_effect=AssertionError("GET must not call GitHub")))
    @web.middleware
    async def test_identity(request, handler):
        request[identity.IDENTITY_KEY] = WHO
        return await handler(request)
    app = web.Application(middlewares=[test_identity, identity.require_role])
    app[DB_KEY] = db
    app.router.add_get("/conversations/{id}/task", server_task_routes.task_get)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(f"/conversations/{task['id']}/task")
        assert response.status == 200
        assert (await response.json())["mode"] == "external_pr"
        monkeypatch.setattr(identity, "_project_grants", {WHO: []})
        assert (await client.get(f"/conversations/{task['id']}/task")).status == 403
    finally:
        await client.close()
