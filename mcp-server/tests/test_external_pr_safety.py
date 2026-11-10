"""External registrations never acquire a Git workspace or run an agent."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from relay import (
    conversation_access, coordination, external_pr_access, identity,
    private_mcp_access, task_service, task_workspace,
)
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
from relay.server_conversation_sharing import admin_listing


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
        denied = await response.json()
        if path.endswith("/set-discord-user"):
            # Private audience is rejected before any attempted Discord link,
            # independently of the registration's read-only workspace policy.
            assert denied["code"] == "discord_audience_unverified"
            assert denied["linked"] is False
            assert "audiencia" in denied["error"]
        else:
            assert denied["error"] == "external_pr_read_only"
        assert await db.get_conversation_task(cid) == state
        assert await db.list_conversation_events(cid) == []
        assert await db.run("SELECT id FROM chats WHERE conversation_id=?", (cid,)) == []
    finally:
        await client.close()


@pytest.mark.parametrize("actor,role,grants,shared,read_only,register_allowed,chat_allowed", [
    ("dev@example.test", "member", [], False, False, False, False),
    ("dev@example.test", "member", ["demo"], False, False, True, True),
    ("dev@example.test", "member", ["demo"], False, True, False, False),
    ("peer@example.test", "member", ["demo"], False, False, True, False),
    ("peer@example.test", "member", ["demo"], True, False, True, True),
    ("admin@example.test", "owner", [], False, False, True, True),
    ("owner", "owner", [], False, False, True, True),
])
async def test_external_read_surfaces_require_current_project_grant_and_chat_access(
        tmp_path, monkeypatch, actor, role, grants, shared, read_only, register_allowed, chat_allowed):
    db, cid, _state, _project = await external_task(tmp_path)
    # Authorization snapshots read current durable accounts, not only the
    # process role cache. A project grant alone never shares a conversation.
    await db.set_user_role("dev@example.test", "member", project_slugs=["demo"])
    if actor != identity.OWNER:
        await db.set_user_role(actor, role, project_slugs=grants)
    if shared:
        await conversation_access.set_share(db, cid, "dev@example.test", actor, "read")
    if read_only:
        await db.upsert_project({"slug": "demo", "defaults_json": {"read_only": True}})
    for attr in ("_roles", "_disabled", "_names", "_project_grants"):
        monkeypatch.setattr(identity, attr, getattr(identity, attr).copy())
    identity.load_roles(await db.list_users())
    await db.set_conversation_summary(cid, "EXTERNAL_PRIVATE_SUMMARY")

    @web.middleware
    async def test_actor(request, handler):
        request[identity.IDENTITY_KEY] = actor
        return await handler(request)

    app = web.Application(middlewares=[
        test_actor, identity.require_role, private_mcp_access.guard, external_pr_access.guard,
    ])
    app[DB_KEY] = db
    app.router.add_get("/conversations", conversations_list)
    app.router.add_get("/admin/api/conversations", admin_listing)
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
        # Owner administration is a separate list. The public list defaults
        # to mine; a recipient must deliberately select shared.
        list_path = ("/admin/api/conversations?project=demo" if role == "owner" else
                     "/conversations?project=demo&scope="
                     + ("mine" if actor == "dev@example.test" else "shared"))
        listed = await client.get(list_path)
        assert listed.status == 200
        assert bool((await listed.json())["conversations"]) is chat_allowed
        if not chat_allowed:
            assert "EXTERNAL_PRIVATE_SUMMARY" not in await listed.text()
            assert "Declared tests" not in await listed.text()
        searched = await client.get(f"/admin/api/search?q={cid}")
        assert searched.status == 200
        assert bool((await searched.json())["conversations"]) is chat_allowed
        if not chat_allowed:
            assert "EXTERNAL_PRIVATE_SUMMARY" not in await searched.text()
            assert "Declared tests" not in await searched.text()
        for suffix in ("", "/messages", "/task", "/pr", "/branch", "/diff"):
            detail = await client.get(f"/conversations/{cid}{suffix}")
            # The register task is available through current project access;
            # ordinary chat surfaces additionally require their own live ACL.
            allowed = register_allowed and (suffix == "/task" or chat_allowed)
            if allowed:
                assert detail.status == (422 if suffix == "/diff" else 200), await detail.text()
            else:
                assert detail.status == 404, await detail.text()
                assert "Declared tests" not in await detail.text()
                assert "EXTERNAL_PRIVATE_SUMMARY" not in await detail.text()
        assert (await db.get_conversation_task(cid))["mode"] == "external_pr"
    finally:
        await client.close()


@pytest.mark.parametrize("read_only", [False, True])
async def test_ordinary_creator_reads_survive_project_grant_revocation(
        tmp_path, monkeypatch, read_only):
    db, external, _state, _project = await external_task(tmp_path)
    actor = "dev@example.test"
    await db.set_user_role(actor, "member", project_slugs=["demo"])
    ordinary = await db.create_conversation(project_slug="demo", requested_by=actor)
    await db.set_conversation_summary(ordinary, "ORDINARY_PRIVATE_SUMMARY")
    await db.set_conversation_summary(external, "EXTERNAL_PRIVATE_SUMMARY")
    await db.set_user_role(actor, "member", project_slugs=[])
    if read_only:
        await db.upsert_project({"slug": "demo", "defaults_json": {"read_only": True}})
    for attr in ("_roles", "_disabled", "_names", "_project_grants"):
        monkeypatch.setattr(identity, attr, getattr(identity, attr).copy())
    identity.load_roles(await db.list_users())

    @web.middleware
    async def test_actor(request, handler):
        request[identity.IDENTITY_KEY] = actor
        return await handler(request)

    app = web.Application(middlewares=[
        test_actor, identity.require_role, private_mcp_access.guard, external_pr_access.guard,
    ])
    app[DB_KEY] = db
    app.router.add_get("/conversations", conversations_list)
    app.router.add_get("/conversations/{id}", conversations_get)
    app.router.add_get("/admin/api/search", api_search)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        listed = await client.get("/conversations?project=demo&scope=mine")
        assert listed.status == 200
        assert [row["id"] for row in (await listed.json())["conversations"]] == [ordinary]
        assert "EXTERNAL_PRIVATE_SUMMARY" not in await listed.text()
        searched = await client.get(f"/admin/api/search?q={ordinary}")
        assert searched.status == 200
        assert [row["id"] for row in (await searched.json())["conversations"]] == [ordinary]
        detail = await client.get(f"/conversations/{ordinary}")
        assert detail.status == 200
        assert (await detail.json())["summary"] == "ORDINARY_PRIVATE_SUMMARY"
        denied = await client.get(f"/conversations/{external}")
        assert denied.status == 404
        assert "EXTERNAL_PRIVATE_SUMMARY" not in await denied.text()
    finally:
        await client.close()


async def test_external_title_never_enters_shared_chat_memory(tmp_path):
    db, cid, _state, _project = await external_task(tmp_path)
    marker = "EXTERNAL_PRIVATE_SUMMARY"
    await db.set_conversation_summary(cid, marker)
    actor = identity.OWNER
    assert await db.search_memories("demo", actor=actor) == []
    # Even an administrator cannot index an external registration as chat
    # knowledge. A valid actor ensures this exercises the source boundary.
    with pytest.raises(PermissionError, match="Origen de conocimiento"):
        await db.add_memory(cid, "demo", marker, actor=actor)
    assert (await db.get_conversation(cid))["summary"] == marker
    # Model a historical index row directly; the public writer now correctly
    # refuses this origin. No fixture disables the live source/ACL checks.
    if db._fts_available:
        await db.run("INSERT INTO memories_fts(summary,project_slug,conversation_id) VALUES(?,?,?)",
                     (marker, "demo", cid))
    assert await db.search_memories("demo", marker, actor=actor) == []
    # Positive control: retrieval remains available for an ordinary live source.
    ordinary = await db.create_conversation(project_slug="demo", requested_by=actor)
    await db.add_memory(ordinary, "demo", "ORDINARY_CHAT_MEMORY", actor=actor)
    memories = await db.search_memories("demo", "ORDINARY_CHAT_MEMORY", actor=actor)
    assert [entry["conversation_id"] for entry in memories] == [ordinary]


@pytest.mark.parametrize("kind", ["run", "feedback", "publish"])
async def test_external_persistence_refuses_execution_events(tmp_path, kind):
    db, cid, _state, _project = await external_task(tmp_path)
    with pytest.raises(ValueError, match="PR externo"):
        await db.enqueue_conversation_event(cid, "attempt", kind, {"user": "No execution"})
    assert await db.list_conversation_events(cid) == []
    assert await db.claim_conversation_event(cid) is None
    assert await db.run("SELECT id FROM chats WHERE conversation_id=?", (cid,)) == []
