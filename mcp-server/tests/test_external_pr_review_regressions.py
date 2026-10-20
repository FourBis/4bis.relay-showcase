"""Regressions found while independently reviewing external PR registration."""
from __future__ import annotations

import asyncio
import copy
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from relay import external_pr, identity, server_common, server_external_pr_routes, server_task_routes
from relay.app_state import DB_KEY
from relay.user_accounts import AccountError
from tests.test_external_pr import SHA_A, SHA_B, URL, WHO, declare, register, registration, remote_snapshot


async def ready_task(registration):
    db, project, _, remote = registration
    remote.update(remote_snapshot(approved=True))
    task = await register(registration)
    task = await declare(registration, task["id"])
    assert task["assessment"]["state"] == "ready"
    return task


async def test_duplicate_registration_observes_new_head_and_preserves_old_evidence(registration):
    db, _, _, remote = registration
    first = await ready_task(registration)
    remote.update(remote_snapshot(SHA_B))
    second = await register(registration)
    assert second["id"] == first["id"]
    assert second["head_sha"] == SHA_B
    assert second["assessment"]["state"] == "missing_evidence"
    assert second["evidence"] == first["evidence"]
    assert second["evidence"][0]["head_sha"] == SHA_A
    assert sum(h.get("snapshot", {}).get("head_sha") == SHA_B for h in second["history"]) == 1
    again = await register(registration)
    assert again["history"] == second["history"]
    assert len(await db.run("SELECT id FROM conversations")) == 1


async def test_duplicate_registration_observes_dismissed_review(registration):
    _, _, _, remote = registration
    first = await ready_task(registration)
    remote["reviews"][0]["state"] = "DISMISSED"
    remote["observed_at"] = external_pr.now()
    second = await register(registration)
    assert second["id"] == first["id"]
    assert second["head_sha"] == SHA_A
    assert second["assessment"]["state"] == "review_pending"
    assert any(h.get("snapshot", {}).get("reviews", [{}])[0].get("state") == "APPROVED"
               for h in second["history"] if h.get("snapshot", {}).get("reviews"))


@pytest.mark.parametrize("mode", [None, "write"])
async def test_registration_cannot_duplicate_a_pr_already_linked_to_internal_task(registration, mode):
    db, _, _, _ = registration
    cid = await db.create_conversation(project_slug="demo", requested_by="original@example.test")
    await db.set_conversation_pr(cid, URL.replace("AuroraDemo/relay-demo", "AURORADEMO/RELAY-DEMO") + "/")
    if mode:
        await db.update_conversation_task(cid, mode=mode, state="review", requested_by="original@example.test")
    before = await db.get_conversation(cid)
    with pytest.raises(ValueError):
        await register(registration)
    assert await db.get_conversation(cid) == before
    assert len(await db.run("SELECT id FROM conversations")) == 1


async def client_for(db, monkeypatch):
    monkeypatch.setattr(identity, "_roles", {WHO: "member"})
    monkeypatch.setattr(identity, "_disabled", set())
    monkeypatch.setattr(identity, "_project_grants", {WHO: ["demo"]})
    monkeypatch.setattr(server_common, "_get_api_key", lambda: "")

    @web.middleware
    async def actor(request, handler):
        request[identity.IDENTITY_KEY] = WHO
        return await handler(request)

    app = web.Application(middlewares=[actor, identity.require_role])
    app[DB_KEY] = db
    app.router.add_post("/projects/{slug}/external-pr", server_external_pr_routes.register)
    app.router.add_post("/conversations/{id}/external-pr", server_external_pr_routes.action)
    app.router.add_get("/conversations/{id}/task", server_task_routes.task_get)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.parametrize("replacement", [{}, {"external_pr_policy": {"low": {
    "require_declaration": True, "required_checks": ["New required integration check"]}}}])
async def test_get_reassesses_current_project_policy_without_remote_io(registration, monkeypatch, replacement):
    db, _, _, _ = registration
    task = await ready_task(registration)
    await db.upsert_project({"slug": "demo", "defaults_json": replacement})
    remote = AsyncMock(side_effect=AssertionError("GET must remain local"))
    monkeypatch.setattr(external_pr, "read_snapshot", remote)
    client = await client_for(db, monkeypatch)
    try:
        result = await client.get(f"/conversations/{task['id']}/task")
        assert result.status == 200
        assert (await result.json())["assessment"]["state"] == "missing_evidence"
        remote.assert_not_awaited()
    finally:
        await client.close()


@pytest.mark.parametrize("action,remote_fails", [
    ("register", False), ("refresh", False), ("evidence", False),
    ("refresh", True), ("evidence", True),
])
async def test_project_grant_revoked_during_github_io_aborts_without_mutation(
        registration, monkeypatch, action, remote_fails):
    db, _, body, remote = registration
    task = await ready_task(registration) if action != "register" else None
    before = await db.get_conversation_task(task["id"]) if task else None

    async def revoke(*args, **kwargs):
        identity._project_grants[WHO] = []
        if remote_fails:
            raise external_pr.TaskPRError("GitHub unavailable after grant revocation")
        return copy.deepcopy(remote)

    monkeypatch.setattr(external_pr, "read_snapshot", revoke)
    client = await client_for(db, monkeypatch)
    try:
        path = "/projects/demo/external-pr" if action == "register" else f"/conversations/{task['id']}/external-pr"
        payload = body if action == "register" else {"action": action}
        if action == "evidence":
            payload.update(request_id="new-after-revocation", head_sha=SHA_A, summary="Must not be accepted")
        response = await client.post(path, json=payload)
        assert response.status == 403
        if task:
            assert await db.get_conversation_task(task["id"]) == before
        else:
            assert not await db.run("SELECT id FROM conversations")
    finally:
        await client.close()


async def test_account_error_during_refresh_invalidates_readiness_and_preserves_history(registration, monkeypatch):
    db, project, _, _ = registration
    task = await ready_task(registration)
    monkeypatch.setattr(external_pr, "actor_identity", AsyncMock(side_effect=AccountError("Reconnect GitHub")))
    with pytest.raises(AccountError):
        await external_pr.refresh(db, project, task["id"], WHO)
    saved = await db.get_conversation_task(task["id"])
    assert external_pr.snapshot(saved)["assessment"]["state"] == "missing_evidence"
    assert saved["evidence"] == task["evidence"]
    assert saved["history"][:-1] == task["history"]
    assert saved["history"][-1]["kind"] == "verification_failed"
    assert saved["history"][-1]["actor"]["relay_actor"] == WHO


async def test_failed_duplicate_invalidates_once_and_recovers_same_record(registration, monkeypatch):
    db, _, _, remote = registration
    task = await ready_task(registration)
    monkeypatch.setattr(external_pr, "read_snapshot", AsyncMock(side_effect=external_pr.TaskPRError("Remote failed")))
    for _ in range(2):
        with pytest.raises(external_pr.TaskPRError):
            await register(registration)
    saved = await db.get_conversation_task(task["id"])
    assert external_pr.snapshot(saved)["assessment"]["state"] == "missing_evidence"
    assert saved["evidence"] == task["evidence"]
    assert sum(h["kind"] == "verification_failed" for h in saved["history"]) == 1
    monkeypatch.setattr(external_pr, "read_snapshot", AsyncMock(return_value=copy.deepcopy(remote)))
    recovered = await register(registration)
    assert recovered["id"] == task["id"]
    assert recovered["assessment"]["state"] == "ready"
    assert len(await db.run("SELECT id FROM conversations")) == 1


async def test_slow_failed_read_cannot_invalidate_a_newer_success(registration, monkeypatch):
    db, project, _, remote = registration
    task = await ready_task(registration)
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_failure(*args, **kwargs):
        started.set()
        await release.wait()
        raise external_pr.TaskPRError("Old failed read")

    monkeypatch.setattr(external_pr, "read_snapshot", delayed_failure)
    old = asyncio.create_task(external_pr.refresh(db, project, task["id"], WHO))
    try:
        await asyncio.wait_for(started.wait(), 2)
        latest = copy.deepcopy(remote)
        latest["observed_at"] = external_pr.now()
        monkeypatch.setattr(external_pr, "read_snapshot", AsyncMock(return_value=latest))
        confirmed = await external_pr.refresh(db, project, task["id"], WHO)
        release.set()
        with pytest.raises(external_pr.TaskPRError):
            await old
        saved = await db.get_conversation_task(task["id"])
        assert saved["external_pr"]["observed_at"] == confirmed["external_pr"]["observed_at"]
        assert saved["history"] == confirmed["history"]
        assert external_pr.snapshot(saved)["assessment"]["state"] == "ready"
    finally:
        release.set()
        await asyncio.gather(old, return_exceptions=True)
