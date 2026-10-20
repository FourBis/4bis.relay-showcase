"""Policy writes and automatic sync obey the same live project grants."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from relay import identity, issue_sync, issue_sync_github, server_issue_policy_routes, task_service
from relay.app_state import DB_KEY
from relay.db import Database


@pytest.fixture
async def workflow(tmp_path, monkeypatch):
    db = Database(path=tmp_path / "workflow.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path / "repo"),
                             "defaults_json": {"unrelated": "preserve"}})
    cid = await db.create_conversation(project_slug="demo", requested_by="dev@example.test")
    await db.update_conversation_task(cid, mode="external_pr", issue_link={"repo_id": 42, "number": 7})
    monkeypatch.setattr(identity, "_roles", {"dev@example.test": "member", "reader@example.test": "member"})
    monkeypatch.setattr(identity, "_disabled", set())
    monkeypatch.setattr(identity, "_project_grants", {"dev@example.test": ["demo"]})
    monkeypatch.delenv("RELAY_API_KEY", raising=False)

    @web.middleware
    async def actor(request, handler):
        request[identity.IDENTITY_KEY] = request.headers.get("X-Test-Actor", "owner")
        return await handler(request)

    app = web.Application(middlewares=[actor, identity.require_role])
    app[DB_KEY] = db
    app.router.add_get("/projects/{slug}/issue-sync/discovery", server_issue_policy_routes.discovery)
    app.router.add_post("/projects/{slug}/issue-sync/policy", server_issue_policy_routes.configure)
    async with TestClient(TestServer(app)) as client:
        yield db, cid, client


async def test_policy_owner_only_and_preserves_other_defaults(workflow, monkeypatch):
    db, _, client = workflow
    validate = AsyncMock(return_value={"enabled": False, "issue_states": {}, "projects": []})
    monkeypatch.setattr(issue_sync_github, "validate_policy", validate)
    response = await client.post("/projects/demo/issue-sync/policy", json={"policy": {"enabled": False}},
                                 headers={"X-Test-Actor": "dev@example.test"})
    assert response.status == 403
    validate.assert_not_awaited()
    response = await client.post("/projects/demo/issue-sync/policy", json={"policy": {"enabled": False}})
    assert response.status == 200
    saved = (await db.get_project("demo"))["defaults_json"]
    assert saved == {"unrelated": "preserve", "issue_sync_policy": validate.return_value}
    assert (await response.json())["enabled"] is False


async def test_bad_policy_or_changed_repository_is_not_saved(workflow, monkeypatch):
    db, _, client = workflow
    monkeypatch.setattr(issue_sync_github, "validate_policy", AsyncMock(side_effect=ValueError("Unknown option ID")))
    response = await client.post("/projects/demo/issue-sync/policy", json={"policy": {"enabled": True}})
    assert response.status == 422
    assert "issue_sync_policy" not in (await db.get_project("demo"))["defaults_json"]

    async def changed(*args):
        await db.upsert_project({"slug": "demo", "repo_path": "different-repository"})
        return {"enabled": True}

    monkeypatch.setattr(issue_sync_github, "validate_policy", changed)
    response = await client.post("/projects/demo/issue-sync/policy", json={"policy": {"enabled": True}})
    assert response.status == 422
    assert "issue_sync_policy" not in (await db.get_project("demo"))["defaults_json"]


async def test_discovery_does_not_broaden_project_grants(workflow, monkeypatch):
    _, _, client = workflow
    discover = AsyncMock(return_value={"projects": [{"id": "PVT_fixture"}]})
    monkeypatch.setattr(issue_sync_github, "discover", discover)
    response = await client.get("/projects/demo/issue-sync/discovery?project_id=PVT_fixture",
                                headers={"X-Test-Actor": "reader@example.test"})
    assert response.status == 403
    discover.assert_not_awaited()
    response = await client.get("/projects/demo/issue-sync/discovery?project_id=PVT_fixture",
                                headers={"X-Test-Actor": "dev@example.test"})
    assert response.status == 200
    assert (await response.json())["projects"][0]["id"] == "PVT_fixture"
    assert discover.await_args.args[1] == "PVT_fixture"


async def test_disabled_project_policy_never_runs_background_sync(workflow, monkeypatch):
    db, cid, _ = workflow
    refresh = AsyncMock()
    monkeypatch.setattr(issue_sync, "refresh", refresh)
    await task_service.poll_issue_sync(db, await db.get_project("demo"), cid)
    refresh.assert_not_awaited()


async def test_external_pr_sync_uses_live_actor_grants_and_reserves_cadence(workflow, monkeypatch):
    db, cid, _ = workflow
    await db.upsert_project({"slug": "demo", "defaults_json": {"issue_sync_policy": {"enabled": True}}})
    await db.set_user_role("dev@example.test", "member", project_slugs=["demo"])

    async def refresh(*args, authorize):
        assert args[3] == "dev@example.test"
        assert (await authorize())["slug"] == "demo"
        # Revocation during an observation must be detected before mutation.
        await db.set_user_role("dev@example.test", "member", project_slugs=[])
        with pytest.raises(RuntimeError, match="acceso"):
            await authorize()

    mocked = AsyncMock(side_effect=refresh)
    monkeypatch.setattr(issue_sync, "refresh", mocked)
    await task_service.poll_issue_sync(db, await db.get_project("demo"), cid)
    assert mocked.await_count == 1
    await task_service.poll_issue_sync(db, await db.get_project("demo"), cid)
    assert mocked.await_count == 1


async def test_revoked_actor_and_read_only_project_never_sync(workflow, monkeypatch):
    db, cid, _ = workflow
    await db.upsert_project({"slug": "demo", "defaults_json": {"issue_sync_policy": {"enabled": True}}})
    refresh = AsyncMock()
    monkeypatch.setattr(issue_sync, "refresh", refresh)
    await task_service.poll_issue_sync(db, await db.get_project("demo"), cid)
    refresh.assert_not_awaited()
    assert "acceso" in (await issue_sync.snapshot(db, cid))["error"]
    await db.update_conversation_task(cid, issue_sync_next_poll_at=0)
    await db.upsert_project({"slug": "demo", "defaults_json": {"read_only": True,
                             "issue_sync_policy": {"enabled": True}}})
    await task_service.poll_issue_sync(db, await db.get_project("demo"), cid)
    refresh.assert_not_awaited()


async def test_slow_sync_is_independent_and_deduplicated(workflow, monkeypatch):
    db, cid, _ = workflow
    await db.upsert_project({"slug": "demo", "defaults_json": {"issue_sync_policy": {"enabled": True}}})
    released = asyncio.Event()

    async def slow(*args):
        await released.wait()

    mocked = AsyncMock(side_effect=slow)
    monkeypatch.setattr(task_service, "poll_issue_sync", mocked)
    app = {DB_KEY: db}
    project, state = await db.get_project("demo"), await db.get_conversation_task(cid)
    running = task_service.start_issue_sync(app, project, cid, state)
    assert running is task_service.start_issue_sync(app, project, cid, state)
    assert not running.done()
    released.set()
    await running
    assert mocked.await_count == 1
