"""Shared issue links use verified repository identity and atomic task binding."""
from __future__ import annotations

import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from relay import external_pr, github, identity, issue_links, server_common, server_issue_routes
from relay.app_state import DB_KEY
from relay.db import Database

WHO = "dev@example.test"
REPO = "AuroraDemo/relay-demo"
URL = f"https://github.com/{REPO}/issues/12"


def issue(number=12):
    return {"number": number, "node_id": f"I_{number}", "html_url": f"https://github.com/{REPO}/issues/{number}",
            "repository_url": f"https://api.github.com/repos/{REPO}", "title": f"Issue {number}",
            "state": "open", "updated_at": "2026-10-20T12:00:00Z"}


@pytest.fixture
async def linked(tmp_path, monkeypatch):
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path / "repo")})
    project = await db.get_project("demo")
    cid = await db.create_conversation(project_slug="demo", requested_by=WHO)
    await db.update_conversation_task(cid, mode="write", state="working", untouched={"value": 1})
    monkeypatch.setattr(identity, "_roles", {WHO: "member"})
    monkeypatch.setattr(identity, "_project_grants", {WHO: ["demo"]})
    monkeypatch.setattr(identity, "_disabled", set())
    monkeypatch.setattr(github, "repo_slug", AsyncMock(return_value=REPO))
    payloads = {f"repos/{REPO}": {"id": 42, "full_name": REPO}, "user": {"id": 5, "login": "dev", "type": "User"},
                f"repos/{REPO}/issues/12": issue(), f"repos/{REPO}/issues/13": issue(13)}
    calls = []

    async def gh(*args, **kwargs):
        assert args[:3] == ("api", "--hostname", "github.com")
        assert len(args) == 4, "Reads only; no method, body or mutation arguments"
        calls.append(args[3])
        return 0, json.dumps(payloads[args[3]])
    monkeypatch.setattr(github, "_gh", gh)
    return db, project, cid, payloads, calls


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "12", 2147483648,
    "http://github.com/AuroraDemo/relay-demo/issues/12", "https://github.com.evil.test/AuroraDemo/relay-demo/issues/12",
    "https://github.com/AuroraDemo/relay-demo/pull/12", "https://github.com/AuroraDemo/relay-demo/issues/12#x",
    "https://github.com/AuroraDemo/relay-demo/issues/12?x=1", "https://github.com/AuroraDemo/relay-demo/issues/2147483648"])
async def test_invalid_references_fail_without_remote_io(linked, value):
    _, project, _, _, calls = linked
    with pytest.raises(ValueError):
        await issue_links.resolve(project, value)
    assert calls == []


async def test_number_and_url_resolve_same_canonical_issue(linked):
    _, project, _, _, _ = linked
    first = await issue_links.resolve(project, 12)
    assert first == await issue_links.resolve(project, URL)
    assert first == {"repo": REPO, "repo_id": 42, "number": 12, "node_id": "I_12", "url": URL,
                     "title": "Issue 12", "state": "OPEN", "updated_at": "2026-10-20T12:00:00Z"}


async def test_other_repository_same_number_rejected_by_id(linked):
    _, project, _, payloads, _ = linked
    payloads["repos/Other/demo"] = {"id": 99, "full_name": "Other/demo"}
    with pytest.raises(ValueError, match="repositorio autorizado"):
        await issue_links.resolve(project, "https://github.com/Other/demo/issues/12")


@pytest.mark.parametrize("patch", [{"pull_request": {}}, {"repository_url": "https://api.github.com/repos/Other/demo"},
    {"html_url": "https://evil.test/12"}, {"node_id": ""}, {"number": 13}, {"state": "unknown"},
    {"updated_at": None}, {"repository_url": None}])
async def test_remote_issue_identity_and_metadata_are_required(linked, patch):
    _, project, _, payloads, _ = linked
    payloads[f"repos/{REPO}/issues/12"].update(patch)
    with pytest.raises(ValueError):
        await issue_links.resolve(project, 12)


async def test_binding_is_atomic_idempotent_and_preserves_task(linked):
    db, project, cid, _, _ = linked
    first = await issue_links.bind(db, project, cid, URL, WHO)
    assert first["issue_link"]["node_id"] == "I_12"
    assert first["resolution_complete"] is False
    assert first["state"] == "working" and first["untouched"] == {"value": 1}
    assert (await db.get_conversation(cid))["issue_number"] == 12
    assert await issue_links.bind(db, project, cid, 12, WHO) == first
    with pytest.raises(ValueError, match="otro issue"):
        await issue_links.bind(db, project, cid, 13, WHO)
    assert await db.get_conversation_task(cid) == first
    assert (await db.get_conversation(cid))["issue_number"] == 12


async def test_concurrent_different_bindings_have_one_winner(linked):
    db, project, cid, _, _ = linked
    results = await asyncio.gather(*(issue_links.bind(db, project, cid, value, WHO) for value in (12, 13)),
                                   return_exceptions=True)
    assert sum(isinstance(result, ValueError) for result in results) == 1
    state = await db.get_conversation_task(cid)
    assert state["issue_number"] == state["issue_link"]["number"] == (await db.get_conversation(cid))["issue_number"]


async def test_resolution_requires_explicit_human_boolean_and_records_actor(linked):
    db, project, cid, payloads, _ = linked
    with pytest.raises(ValueError):
        await issue_links.bind(db, project, cid, 12, WHO, resolution_complete="true")
    payloads["user"]["type"] = "Bot"
    with pytest.raises(external_pr.TaskPRError, match="humana"):
        await issue_links.bind(db, project, cid, 12, WHO, resolution_complete=True)
    assert (await db.get_conversation(cid))["issue_number"] is None
    payloads["user"]["type"] = "User"
    task = await issue_links.bind(db, project, cid, 12, WHO, resolution_complete=True)
    assert task["resolution_complete"] is True
    assert task["resolution_actor"] == {"relay_actor": WHO, "github_id": 5, "github_login": "dev"}
    assert task["resolution_at"]
    assert await issue_links.bind(db, project, cid, URL, WHO, resolution_complete=True) == task


async def test_unassigned_actor_cannot_read_remote_or_bind(linked):
    db, project, cid, _, calls = linked
    identity._project_grants[WHO] = []
    with pytest.raises(PermissionError):
        await issue_links.bind(db, project, cid, 12, WHO)
    assert calls == [] and (await db.get_conversation(cid))["issue_number"] is None


async def test_legacy_conversation_is_not_silently_promoted(linked):
    db, project, _, _, calls = linked
    cid = await db.create_conversation(project_slug="demo", requested_by=WHO)
    with pytest.raises(ValueError, match="Importa la conversación legacy"):
        await issue_links.bind(db, project, cid, 12, WHO)
    assert calls == []
    assert await db.get_conversation_task(cid) == {}
    assert (await db.get_conversation(cid))["issue_number"] is None


async def test_revoked_grant_before_transaction_prevents_persistence(linked):
    db, project, cid, _, _ = linked
    count = 0

    async def authorize():
        nonlocal count
        count += 1
        if count == 2:
            identity._project_grants[WHO] = []
        return project
    with pytest.raises(PermissionError):
        await issue_links.bind(db, project, cid, 12, WHO, authorize=authorize)
    assert (await db.get_conversation(cid))["issue_number"] is None
    assert "issue_link" not in await db.get_conversation_task(cid)


async def test_selector_filters_pull_requests(linked):
    _, project, _, payloads, _ = linked
    payloads[f"repos/{REPO}/issues?state=open&per_page=100"] = [issue(), {**issue(13), "pull_request": {}}]
    rows = await issue_links.list_issues(project)
    assert [row["number"] for row in rows] == [12]


async def test_issue_route_denies_unassigned_user_before_remote_io(linked, monkeypatch):
    db, _, cid, _, calls = linked
    identity._project_grants[WHO] = []
    monkeypatch.setattr(server_common, "_get_api_key", lambda: "")

    @web.middleware
    async def actor(request, handler):
        request[identity.IDENTITY_KEY] = WHO
        return await handler(request)
    app = web.Application(middlewares=[actor])
    app[DB_KEY] = db
    app.router.add_post("/conversations/{id}/issue", server_issue_routes.link)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(f"/conversations/{cid}/issue", json={"issue": 12})
        assert response.status == 403
        assert calls == []
    finally:
        await client.close()


@pytest.mark.parametrize("operation", ["list", "link", "refresh", "retry", "real_refresh", "real_retry"])
async def test_issue_routes_preserve_account_reconnection_response(linked, monkeypatch, operation):
    from relay import issue_sync
    from relay.user_accounts import AccountError

    db, project, cid, _, _ = linked
    monkeypatch.setattr(server_common, "_get_api_key", lambda: "")
    failure = AsyncMock(side_effect=AccountError("Reconecta GitHub"))
    real_adapter = operation.startswith("real_")
    if real_adapter:
        canonical = await issue_links.resolve(project, 12)
        await db.update_conversation_task(cid, issue_link=canonical)
        await db.set_conversation_issue(cid, 12)
        monkeypatch.setattr(github, "_gh", failure)
    else:
        monkeypatch.setattr(issue_links, "list_issues", failure)
        monkeypatch.setattr(issue_links, "bind", failure)
        monkeypatch.setattr(issue_sync, "refresh", failure)
        monkeypatch.setattr(issue_sync, "retry", failure)

    @web.middleware
    async def actor(request, handler):
        request[identity.IDENTITY_KEY] = WHO
        return await handler(request)

    app = web.Application(middlewares=[actor, identity.access_identity])
    app[DB_KEY] = db
    app.router.add_get("/projects/{slug}/issues", server_issue_routes.issues)
    app.router.add_post("/conversations/{id}/issue", server_issue_routes.link)
    app.router.add_post("/conversations/{id}/issue-sync", server_issue_routes.refresh)
    async with TestClient(TestServer(app)) as client:
        if operation == "list":
            response = await client.get("/projects/demo/issues")
        elif operation == "link":
            response = await client.post(f"/conversations/{cid}/issue", json={"issue": 12})
        else:
            response = await client.post(f"/conversations/{cid}/issue-sync", json={"action": operation.removeprefix("real_")})
        assert response.status == 409
        assert await response.json() == {"error": "Reconecta GitHub", "connect_url": "/admin/#/account"}
        assert response.headers["Cache-Control"] == "no-store"
    assert (await db.get_conversation(cid))["issue_number"] == (12 if real_adapter else None)


async def test_external_registration_uses_shared_canonical_snapshot(linked, monkeypatch):
    db, project, _, _, _ = linked
    canonical = await issue_links.resolve(project, URL)
    remote = {"repo": REPO, "repo_id": 42, "number": 17, "url": f"https://github.com/{REPO}/pull/17",
              "head_sha": "a" * 40, "base_sha": "b" * 40, "state": "OPEN", "draft": True,
              "author": {"id": 8, "login": "author", "type": "User"}, "checks": [], "reviews": [],
              "observed_at": external_pr.now(), "issue_link": canonical}
    monkeypatch.setattr(external_pr, "read_snapshot", AsyncMock(return_value=copy.deepcopy(remote)))
    body = {"url": remote["url"], "title": "Task", "risk": "low", "issue": URL, "resolution_complete": True}
    task = await external_pr.register(db, project, body, WHO)
    assert task["issue_link"] == canonical and task["issue_number"] == 12
    assert task["resolution_complete"] is True and task["mode"] == "external_pr"
    assert (await db.get_conversation(task["id"]))["issue_number"] == 12


@pytest.mark.parametrize("event_state", ["processing", "uncertain"])
async def test_pending_effect_freezes_new_dependencies_and_resolution(linked, monkeypatch, event_state):
    from relay import issue_sync_store
    db, project, cid, _, _ = linked
    existing = await issue_links.bind(db, project, cid, 12, WHO, resolution_complete=True)
    await issue_sync_store.ensure_schema(db)
    await db.run("INSERT INTO issue_sync_events(conversation_id,issue_key,sequence,event_key,transition,actor,"
                 "policy_digest,payload,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (cid, "42:12", 1, "closure", "merged", WHO, "policy", "{}", event_state, "now", "now"))
    other = await db.create_conversation(project_slug="demo", requested_by=WHO)
    await db.update_conversation_task(other, mode="write", state="working")
    with pytest.raises(ValueError, match="en curso o incierta"):
        await issue_links.bind(db, project, other, 12, WHO)
    with pytest.raises(ValueError, match="en curso o incierta"):
        await issue_links.bind(db, project, cid, 12, WHO, resolution_complete=False)
    assert (await db.get_conversation(other))["issue_number"] is None
    assert await db.get_conversation_task(cid) == existing
    # A retry with the same identity and declaration does not alter dependencies.
    assert await issue_links.bind(db, project, cid, 12, WHO, resolution_complete=True) == existing
    remote = {"repo": REPO, "repo_id": 42, "number": 17, "url": f"https://github.com/{REPO}/pull/17",
              "head_sha": "a" * 40, "base_sha": "b" * 40, "state": "OPEN", "draft": True,
              "author": {"id": 8, "login": "author", "type": "User"}, "checks": [], "reviews": [],
              "observed_at": external_pr.now(), "issue_link": existing["issue_link"]}
    monkeypatch.setattr(external_pr, "read_snapshot", AsyncMock(return_value=remote))
    before = await db.run("SELECT id FROM conversations")
    with pytest.raises(ValueError, match="en curso o incierta"):
        await external_pr.register(db, project, {"url": remote["url"], "title": "Partial PR", "risk": "low", "issue": 12}, WHO)
    assert await db.run("SELECT id FROM conversations") == before
