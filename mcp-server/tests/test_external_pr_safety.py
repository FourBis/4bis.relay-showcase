"""External registrations never acquire a Git workspace or run an agent."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from relay import coordination, external_pr_access, identity, task_service, task_workspace
from relay.admin_observability import api_search
from relay.app_state import DB_KEY
from relay.db import Database
from relay.server_conversation_actions import (
    conversation_branch_status, conversation_diff, conversation_git_action,
    conversation_pr_status, conversation_set_discord_user,
    conversations_close, conversations_compact,
)
from relay.server_expert_routes import experts_run
from relay.server_conversation_routes import (
    conversations_get, conversations_get_messages, conversations_list,
)
from relay.server_task_routes import task_get


async def external_task(tmp_path):
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    cid = await db.create_conversation(project_slug="demo", requested_by="dev@example.test",
                                       discord_thread_id="external-thread")
    state = await db.update_conversation_task(cid, id=cid, mode="external_pr", state="registered", risk="low",
        requested_by="dev@example.test", external_pr={"head_sha": "a" * 40,
            "state": "OPEN", "draft": False},
        evidence=[{"head_sha": "a" * 40, "summary": "Declared tests"}])
    return db, cid, state, await db.get_project("demo")


async def test_external_registration_does_not_occupy_or_autoclose_workspace(tmp_path):
    db, cid, _state, project = await external_task(tmp_path)
    await db.run("UPDATE conversations SET last_activity_at='2000-01-01T00:00:00Z' WHERE id=?", (cid,))
    assert await db.get_open_conversation_for_workspace(project) is None
    assert await db.get_open_conversation_for_project("demo") is None
    assert await db.stale_open_conversations(1) == []
    # Historical workspace users retain the existing exclusion rule.
    legacy = await db.create_conversation(project_slug="demo")
    assert (await db.get_open_conversation_for_workspace(project))["id"] == legacy
    assert (await db.get_open_conversation_for_project("demo"))["id"] == legacy


async def test_external_registration_survives_recovery_and_never_claims_events(tmp_path, monkeypatch):
    db, cid, state, project = await external_task(tmp_path)
    inspect = AsyncMock(side_effect=AssertionError("workspace inspection forbidden"))
    feedback = AsyncMock(side_effect=AssertionError("GitHub polling forbidden"))
    claim = AsyncMock(side_effect=AssertionError("agent event claim forbidden"))
    monkeypatch.setattr(task_workspace, "inspect_workspace", inspect)
    monkeypatch.setattr(task_service.task_pr, "feedback_snapshot", feedback)
    monkeypatch.setattr(db, "claim_conversation_event", claim)
    await task_service.recover(db)
    await task_service._drain({DB_KEY: db}, cid)
    assert await task_service._consume({DB_KEY: db}, project, cid) is False
    await task_service.poll_feedback(db, project, cid)
    assert await db.get_conversation_task(cid) == state
    inspect.assert_not_awaited()
    feedback.assert_not_awaited()
    claim.assert_not_awaited()


async def test_external_workspace_read_is_neutral_but_execution_resolution_is_rejected(tmp_path, monkeypatch):
    db, cid, state, project = await external_task(tmp_path)
    git = AsyncMock(side_effect=AssertionError("Git forbidden"))
    monkeypatch.setattr(task_workspace, "_git", git)
    assert await task_workspace.inspect_workspace(db, project, cid, fetch=True) == state
    for operation in (task_workspace.initialize_task, task_workspace.resolved_project):
        with pytest.raises(task_workspace.TaskWorkspaceError, match="PR externo"):
            await operation(db, project, cid)
    assert await db.get_conversation_task(cid) == state
    git.assert_not_awaited()


@pytest.mark.parametrize("path,body", [
    ("/experts/run", {"target": "demo", "user": "Run a bot", "conversation": "CID"}),
    ("/experts/run", {"target": "demo", "user": "!build", "discord_thread_id": "external-thread"}),
    ("/conversations/CID/close", {}),
    ("/conversations/CID/compact", {}),
    ("/conversations/CID/git/push", {}),
    ("/conversations/CID/git/merge", {}),
    ("/conversations/CID/git/sync-base", {}),
    ("/conversations/CID/set-discord-user", {"discord_user_id": "123", "create_thread": True}),
])
async def test_execution_routes_reject_external_before_any_workspace_or_bot_effect(
        tmp_path, monkeypatch, path, body):
    db, cid, state, _project = await external_task(tmp_path)
    def no_acquire(*_args, **_kwargs):
        raise AssertionError("No workspace lease for external registrations")
    monkeypatch.setattr(coordination, "acquire", no_acquire)
    app = web.Application()
    app[DB_KEY] = db
    app.router.add_post("/experts/run", experts_run)
    app.router.add_post("/conversations/{id}/close", conversations_close)
    app.router.add_post("/conversations/{id}/compact", conversations_compact)
    app.router.add_post("/conversations/{id}/git/{action}", conversation_git_action)
    app.router.add_post("/conversations/{id}/set-discord-user", conversation_set_discord_user)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        payload = {key: cid if value == "CID" else value for key, value in body.items()}
        response = await client.post(path.replace("CID", cid), json=payload)
        assert response.status == 409
        assert (await response.json())["error"] == "external_pr_read_only"
        assert await db.get_conversation_task(cid) == state
        assert await db.list_conversation_events(cid) == []
        assert await db.run("SELECT id FROM chats WHERE conversation_id=?", (cid,)) == []
    finally:
        await client.close()


@pytest.mark.parametrize("actor,role,grants,allowed", [
    ("dev@example.test", "member", [], False),
    ("peer@example.test", "member", ["demo"], True),
    ("admin@example.test", "owner", [], True),
    ("owner", "owner", [], True),
])
async def test_external_read_surfaces_require_current_project_grant(
        tmp_path, monkeypatch, actor, role, grants, allowed):
    db, cid, _state, _project = await external_task(tmp_path)
    for attr in ("_roles", "_disabled", "_names", "_project_grants"):
        monkeypatch.setattr(identity, attr, getattr(identity, attr).copy())
    identity.load_roles([{"email": actor, "role": role, "enabled": True,
                          "project_slugs": grants}])
    await db.set_conversation_summary(cid, "EXTERNAL_PRIVATE_SUMMARY")

    @web.middleware
    async def test_actor(request, handler):
        request[identity.IDENTITY_KEY] = actor
        return await handler(request)

    app = web.Application(middlewares=[test_actor, identity.require_role, external_pr_access.guard])
    app[DB_KEY] = db
    app.router.add_get("/conversations", conversations_list)
    app.router.add_get("/conversations/{id}", conversations_get)
    app.router.add_get("/conversations/{id}/messages", conversations_get_messages)
    app.router.add_get("/conversations/{id}/task", task_get)
    app.router.add_get("/conversations/{id}/pr", conversation_pr_status)
    app.router.add_get("/conversations/{id}/branch", conversation_branch_status)
    app.router.add_get("/conversations/{id}/diff", conversation_diff)
    app.router.add_get("/admin/api/search", api_search)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        listed = await client.get("/conversations?project=demo")
        assert listed.status == 200
        assert bool((await listed.json())["conversations"]) is allowed
        searched = await client.get(f"/admin/api/search?q={cid}")
        assert searched.status == 200
        assert bool((await searched.json())["conversations"]) is allowed
        for suffix in ("", "/messages", "/task", "/pr", "/branch", "/diff"):
            detail = await client.get(f"/conversations/{cid}{suffix}")
            if allowed:
                assert detail.status == (422 if suffix == "/diff" else 200), await detail.text()
            else:
                assert detail.status == 404, await detail.text()
                assert "Declared tests" not in await detail.text()
        assert (await db.get_conversation_task(cid))["mode"] == "external_pr"
    finally:
        await client.close()


async def test_external_title_never_enters_shared_chat_memory(tmp_path):
    db, cid, _state, _project = await external_task(tmp_path)
    marker = "EXTERNAL_PRIVATE_SUMMARY"
    await db.set_conversation_summary(cid, marker)
    assert await db.search_memories("demo") == []
    # Even a historical index row cannot turn registration data into chat context.
    await db.add_memory(cid, "demo", marker)
    assert await db.search_memories("demo", marker) == []


@pytest.mark.parametrize("kind", ["run", "feedback", "publish"])
async def test_external_persistence_refuses_execution_events(tmp_path, kind):
    db, cid, _state, _project = await external_task(tmp_path)
    with pytest.raises(ValueError, match="PR externo"):
        await db.enqueue_conversation_event(cid, "attempt", kind, {"user": "No execution"})
    assert await db.list_conversation_events(cid) == []
    assert await db.claim_conversation_event(cid) is None
    assert await db.run("SELECT id FROM chats WHERE conversation_id=?", (cid,)) == []
