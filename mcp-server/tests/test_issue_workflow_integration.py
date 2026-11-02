"""Policy writes and automatic sync obey the same live project grants."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from relay import identity, issue_sync, issue_sync_github, server_common, server_issue_policy_routes, task_service
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
    db, _, client = workflow
    await db.set_user_role("reader@example.test", "member", project_slugs=[])
    await db.set_user_role("dev@example.test", "member", project_slugs=["demo"])
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

# Current DB authority is independent from the intentionally stale identity cache.
_POLICY_ACTOR = "admin@example.test"
_POLICY_MEMBER = "member@example.test"
_NEW_POLICY = {"enabled": False, "issue_states": {}, "projects": []}


@pytest.fixture
async def policy_accounts(tmp_path, monkeypatch):
    db = Database(path=tmp_path / "policy-accounts.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path / "repo"),
                             "defaults_json": {"unrelated": {"keep": "sentinel"},
                                              "issue_sync_policy": {"enabled": False}}})
    await db.set_user_role(_POLICY_ACTOR, "owner", project_slugs=[])
    await db.set_user_role("backup@example.test", "owner", project_slugs=[])
    await db.set_user_role(_POLICY_MEMBER, "member", project_slugs=["demo"])
    monkeypatch.setattr(identity, "_roles", {_POLICY_ACTOR: "owner", _POLICY_MEMBER: "member"})
    monkeypatch.setattr(identity, "_disabled", set())
    monkeypatch.setattr(identity, "_project_grants", {_POLICY_ACTOR: [], _POLICY_MEMBER: ["demo"]})
    monkeypatch.setattr(server_common, "_get_api_key", lambda: "")

    @web.middleware
    async def actor(request, handler):
        request[identity.IDENTITY_KEY] = request.headers.get("X-Test-Actor", _POLICY_ACTOR)
        return await handler(request)

    app = web.Application(middlewares=[actor, identity.require_role])
    app[DB_KEY] = db
    app.router.add_get("/projects/{slug}/issue-sync/discovery", server_issue_policy_routes.discovery)
    app.router.add_post("/projects/{slug}/issue-sync/policy", server_issue_policy_routes.configure)
    async with TestClient(TestServer(app)) as client:
        yield db, client


async def _change_policy_actor(db, change):
    if change == "disabled":
        await db.set_user_role(_POLICY_ACTOR, "owner", enabled=False, project_slugs=[])
    else:
        await db.set_user_role(_POLICY_ACTOR, "member",
                               project_slugs=["demo"] if change == "member" else [])


def _policy_request(client, route, actor=_POLICY_ACTOR):
    headers = {"X-Test-Actor": actor}
    if route == "configure":
        return client.post("/projects/demo/issue-sync/policy",
                           json={"policy": _NEW_POLICY}, headers=headers)
    return client.get("/projects/demo/issue-sync/discovery?project_id=SYNTHETIC_PROJECT",
                      headers=headers)


async def _race_policy_request(client, route, entered, release, change):
    pending = asyncio.create_task(_policy_request(client, route))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await change()
        release.set()
        return await asyncio.wait_for(pending, 2)
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("change", ["member", "disabled"])
async def test_configure_rechecks_db_owner_after_validation(policy_accounts, monkeypatch, change):
    db, client = policy_accounts
    before = (await db.get_project("demo"))["defaults_json"]
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def validate(project, policy):
        calls.append((project["slug"], policy))
        entered.set()
        await release.wait()
        return _NEW_POLICY

    monkeypatch.setattr(issue_sync_github, "validate_policy", validate)
    response = await _race_policy_request(
        client, "configure", entered, release, lambda: _change_policy_actor(db, change))
    assert calls == [("demo", _NEW_POLICY)]
    assert identity._roles[_POLICY_ACTOR] == "owner"
    assert response.status == 403
    assert await response.json() == {"error": "Proyecto no autorizado para tu cuenta"}
    assert (await db.get_project("demo"))["defaults_json"] == before


@pytest.mark.parametrize("change", ["member", "disabled"])
async def test_configure_rechecks_db_owner_inside_save_transaction(policy_accounts, monkeypatch, change):
    db, client = policy_accounts
    before = (await db.get_project("demo"))["defaults_json"]
    validate = AsyncMock(return_value=_NEW_POLICY)
    monkeypatch.setattr(issue_sync_github, "validate_policy", validate)
    in_thread, reached = db._in_thread, []

    async def before_save(operation, *args):
        if operation.__name__ == "save":
            reached.append(operation.__name__)
            # Both pre-save snapshots already accepted the current owner.
            # Revoke at the IO scheduling boundary, without mocking any guard.
            await _change_policy_actor(db, change)
        return await in_thread(operation, *args)

    monkeypatch.setattr(db, "_in_thread", before_save)
    response = await _policy_request(client, "configure")
    assert reached == ["save"]
    assert validate.await_count == 1
    assert identity._roles[_POLICY_ACTOR] == "owner"
    assert response.status == 403
    assert await response.json() == {"error": "Proyecto no autorizado para tu cuenta"}
    assert (await db.get_project("demo"))["defaults_json"] == before


@pytest.mark.parametrize("change", ["member_without_grant", "disabled", "grant_revoked"])
async def test_discovery_rechecks_db_access_before_disclosing_remote_result(
        policy_accounts, monkeypatch, change):
    db, client = policy_accounts
    if change == "grant_revoked":
        await db.set_user_role(_POLICY_ACTOR, "member", project_slugs=["demo"])
    before = (await db.get_project("demo"))["defaults_json"]
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def discover(project, selected):
        calls.append((project["slug"], selected))
        entered.set()
        await release.wait()
        return {"projects": [{"id": "SYNTHETIC_NOT_VISIBLE"}]}

    monkeypatch.setattr(issue_sync_github, "discover", discover)
    response = await _race_policy_request(
        client, "discovery", entered, release, lambda: _change_policy_actor(db, change))
    assert calls == [("demo", "SYNTHETIC_PROJECT")]
    assert identity._roles[_POLICY_ACTOR] == "owner"
    assert response.status == 403
    assert await response.json() == {"error": "Proyecto no autorizado para tu cuenta"}
    assert "SYNTHETIC_NOT_VISIBLE" not in await response.text()
    assert response.headers["Cache-Control"] == "no-store"
    assert (await db.get_project("demo"))["defaults_json"] == before


async def test_discovery_keeps_current_member_grant_after_owner_demotion(policy_accounts, monkeypatch):
    db, client = policy_accounts
    entered, release = asyncio.Event(), asyncio.Event()
    expected = {"projects": [{"id": "SYNTHETIC_ALLOWED"}]}
    calls = []

    async def discover(project, selected):
        calls.append((project["slug"], selected))
        entered.set()
        await release.wait()
        return expected

    monkeypatch.setattr(issue_sync_github, "discover", discover)
    response = await _race_policy_request(
        client, "discovery", entered, release, lambda: _change_policy_actor(db, "member"))
    assert calls == [("demo", "SYNTHETIC_PROJECT")]
    assert response.status == 200
    assert await response.json() == expected
    assert identity._roles[_POLICY_ACTOR] == "owner"


@pytest.mark.parametrize("actor", [_POLICY_ACTOR, identity.OWNER])
async def test_current_personal_owner_and_explicit_local_owner_can_configure(
        policy_accounts, monkeypatch, actor):
    db, client = policy_accounts
    before = (await db.get_project("demo"))["defaults_json"]
    validate = AsyncMock(return_value=_NEW_POLICY)
    monkeypatch.setattr(issue_sync_github, "validate_policy", validate)
    response = await _policy_request(client, "configure", actor)
    assert response.status == 200
    assert await response.json() == {"policy": _NEW_POLICY, "enabled": False}
    assert validate.await_count == 1
    assert validate.await_args.args[0]["slug"] == "demo"
    assert validate.await_args.args[1] == _NEW_POLICY
    assert (await db.get_project("demo"))["defaults_json"] == {
        **before, "issue_sync_policy": _NEW_POLICY}
    assert response.headers["Cache-Control"] == "no-store"


async def test_current_db_member_can_discover_assigned_project(policy_accounts, monkeypatch):
    db, client = policy_accounts
    before = (await db.get_project("demo"))["defaults_json"]
    expected = {"projects": [{"id": "SYNTHETIC_ALLOWED"}]}
    discover = AsyncMock(return_value=expected)
    monkeypatch.setattr(issue_sync_github, "discover", discover)
    response = await _policy_request(client, "discovery", _POLICY_MEMBER)
    assert response.status == 200
    assert await response.json() == expected
    assert discover.await_count == 1
    assert discover.await_args.args[0]["slug"] == "demo"
    assert discover.await_args.args[1] == "SYNTHETIC_PROJECT"
    assert (await db.get_project("demo"))["defaults_json"] == before


@pytest.mark.parametrize("route,change", [
    ("configure", "member"), ("configure", "disabled"),
    ("discovery", "member_without_grant"), ("discovery", "disabled")])
async def test_denied_db_actor_never_enters_github(policy_accounts, monkeypatch, route, change):
    db, client = policy_accounts
    await _change_policy_actor(db, change)
    before = (await db.get_project("demo"))["defaults_json"]
    validate, discover = AsyncMock(return_value=_NEW_POLICY), AsyncMock(return_value={"projects": []})
    monkeypatch.setattr(issue_sync_github, "validate_policy", validate)
    monkeypatch.setattr(issue_sync_github, "discover", discover)
    response = await _policy_request(client, route)
    assert identity._roles[_POLICY_ACTOR] == "owner"
    assert response.status == 403
    assert await response.json() == {"error": "Proyecto no autorizado para tu cuenta"}
    validate.assert_not_awaited()
    discover.assert_not_awaited()
    assert (await db.get_project("demo"))["defaults_json"] == before


@pytest.mark.parametrize("route", ["configure", "discovery"])
@pytest.mark.parametrize("error_type", [OSError, ValueError, server_issue_policy_routes.TaskPRError])
async def test_revocation_hides_remote_errors_instead_of_returning_gateway_detail(
        policy_accounts, monkeypatch, route, error_type):
    db, client = policy_accounts
    before = (await db.get_project("demo"))["defaults_json"]
    calls = []

    async def remote(*args):
        calls.append(args[0]["slug"])
        await _change_policy_actor(db, "disabled")
        raise error_type("SYNTHETIC_REMOTE_DETAIL")

    monkeypatch.setattr(issue_sync_github, "validate_policy", remote)
    monkeypatch.setattr(issue_sync_github, "discover", remote)
    response = await _policy_request(client, route)
    assert calls == ["demo"]
    assert response.status == 403
    assert await response.json() == {"error": "Proyecto no autorizado para tu cuenta"}
    assert "SYNTHETIC_REMOTE_DETAIL" not in await response.text()
    assert (await db.get_project("demo"))["defaults_json"] == before


@pytest.mark.parametrize("route", ["configure", "discovery"])
@pytest.mark.parametrize("error_type,status", [
    (ValueError, 422), (OSError, 502), (server_issue_policy_routes.TaskPRError, 502)])
async def test_current_owner_keeps_validation_and_gateway_statuses(
        policy_accounts, monkeypatch, route, error_type, status):
    db, client = policy_accounts
    before = (await db.get_project("demo"))["defaults_json"]
    remote = AsyncMock(side_effect=error_type("Synthetic upstream failure"))
    monkeypatch.setattr(issue_sync_github, "validate_policy", remote)
    monkeypatch.setattr(issue_sync_github, "discover", remote)
    response = await _policy_request(client, route)
    assert response.status == status
    assert await response.json() == {"error": "Synthetic upstream failure"}
    assert remote.await_count == 1
    assert (await db.get_project("demo"))["defaults_json"] == before


async def test_discovery_does_not_disclose_result_from_replaced_repository(policy_accounts, monkeypatch):
    db, client = policy_accounts
    before = (await db.get_project("demo"))["defaults_json"]
    calls = []

    async def discover(*args):
        calls.append(args[0]["repo_path"])
        await db.upsert_project({"slug": "demo", "repo_path": "synthetic-replacement"})
        return {"projects": [{"id": "SYNTHETIC_WRONG_REPOSITORY"}]}

    monkeypatch.setattr(issue_sync_github, "discover", discover)
    response = await _policy_request(client, "discovery")
    assert len(calls) == 1
    assert response.status == 422
    assert await response.json() == {"error": "El repositorio cambió durante la consulta; vuelve a intentar"}
    assert "SYNTHETIC_WRONG_REPOSITORY" not in await response.text()
    assert (await db.get_project("demo"))["defaults_json"] == before


@pytest.mark.parametrize("change", ["disabled", "read_only", "repository"])
async def test_configure_transaction_preserves_project_guards(policy_accounts, monkeypatch, change):
    db, client = policy_accounts
    before = (await db.get_project("demo"))["defaults_json"]
    validate = AsyncMock(return_value=_NEW_POLICY)
    monkeypatch.setattr(issue_sync_github, "validate_policy", validate)
    in_thread, reached = db._in_thread, []

    async def before_save(operation, *args):
        if operation.__name__ == "save":
            reached.append(operation.__name__)
            if change == "disabled":
                await db.disable_project("demo")
            elif change == "read_only":
                await db.upsert_project({"slug": "demo", "defaults_json": {**before, "read_only": True}})
            else:
                await db.upsert_project({"slug": "demo", "repo_path": "synthetic-replacement"})
        return await in_thread(operation, *args)

    monkeypatch.setattr(db, "_in_thread", before_save)
    response = await _policy_request(client, "configure")
    assert reached == ["save"]
    assert validate.await_count == 1
    assert response.status == 422
    saved = (await db.get_project("demo"))["defaults_json"]
    assert saved["unrelated"] == before["unrelated"]
    assert saved["issue_sync_policy"] == before["issue_sync_policy"]
