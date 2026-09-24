"""Política real del equipo: API, roles y datos financieros, sin red externa."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from relay import admin_users, identity, admin_projects, admin_observability, admin_crm, task_service
from relay.app_state import DB_KEY
from relay.db import Database


@pytest.fixture
async def team(tmp_path, monkeypatch):
    db = Database(path=tmp_path / "team.db")
    await db.init_schema()
    for email, role in [("alex@example.test", "owner"),
                        ("subadmin@example.test", "subadmin"),
                        ("sam@example.test", "member"),
                        ("finance@example.test", "finance")]:
        await db.set_user_role(email, role, display_name=email.split("@")[0])
    await db.upsert_project({"slug": "relay", "name": "Relay", "repo_path": "C:/relay"})
    await db.upsert_project({"slug": "disabled", "name": "Disabled",
                             "repo_path": "C:/disabled", "enabled": False})
    for attr in ("_roles", "_names", "_disabled", "_project_grants"):
        monkeypatch.setattr(identity, attr, getattr(identity, attr).copy())
    identity.load_roles(await db.list_users())

    @web.middleware
    async def user(request, handler):
        request[identity.IDENTITY_KEY] = request.headers.get("Test-User", "alex@example.test")
        return await handler(request)

    async def ok(request):
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[user, identity.require_role])
    app[DB_KEY] = db
    app.router.add_get("/admin/api/me", admin_users.api_me)
    app.router.add_get("/admin/api/users", admin_users.api_users_list)
    app.router.add_put("/admin/api/users", admin_users.api_user_upsert)
    app.router.add_delete("/admin/api/users/{email}", admin_users.api_user_delete)
    for path in ("/admin/api/report", "/admin/api/crm/clients", "/conversations"):
        app.router.add_get(path, ok)
    for path in ("/experts/run", "/mcp", "/admin/api/crm/sync", "/admin/api/crm/digest"):
        app.router.add_post(path, ok)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, db
    finally:
        await client.close()


async def test_subadmin_manages_only_devs(team):
    client, db = team
    headers = {"Test-User": "subadmin@example.test"}
    response = await client.get("/admin/api/users", headers=headers)
    assert (await response.json())["roles"] == ["member"]
    base = {"email": "new@example.test", "display_name": "Nueva persona", "enabled": True}
    assert (await client.put("/admin/api/users", headers=headers,
                            json={**base, "role": "member"})).status == 200
    for role in ("owner", "subadmin", "finance"):
        assert (await client.put("/admin/api/users", headers=headers,
                                json={**base, "role": role})).status == 403
    for email in ("alex@example.test", "subadmin@example.test", "finance@example.test"):
        assert (await client.put("/admin/api/users", headers=headers,
                                json={**base, "email": email, "role": "member"})).status == 403
        assert (await client.delete(f"/admin/api/users/{email}", headers=headers)).status == 403


async def test_owner_assigns_projects_and_omitted_list_preserves_them(team):
    client, db = team
    assigned = await client.put("/admin/api/users", json={
        "email": "sam@example.test", "role": "member", "project_slugs": ["relay"]})
    assert assigned.status == 200
    assert (await assigned.json())["project_slugs"] == ["relay"]

    updated = await client.put("/admin/api/users", json={
        "email": "sam@example.test", "role": "member", "display_name": "Dev 2"})
    assert updated.status == 200
    user = next(u for u in await db.list_users() if u["email"] == "sam@example.test")
    assert user["project_slugs"] == ["relay"]
    assert user["display_name"] == "Dev 2"

    me = await client.get("/admin/api/me", headers={"Test-User": "sam@example.test"})
    assert (await me.json())["project_slugs"] == ["relay"]


async def test_only_owner_can_submit_project_assignments(team):
    client, _ = team
    for email in ("subadmin@example.test", "sam@example.test"):
        response = await client.put("/admin/api/users", headers={"Test-User": email},
                                    json={"email": "new@example.test", "role": "member",
                                          "project_slugs": ["relay"]})
        assert response.status == 403


@pytest.mark.parametrize("slugs,role,expected", [
    (["missing"], "member", 400), (["disabled"], "member", 400),
    (["relay", "relay"], "member", 400), (["relay", 3], "member", 400),
    (["relay"] * 51, "member", 400), (["relay"], "finance", 400),
    ([], "owner", 200),
])
async def test_project_assignments_validate_catalog_and_roles(team, slugs, role, expected):
    client, _ = team
    response = await client.put("/admin/api/users", json={
        "email": "new@example.test", "role": role, "project_slugs": slugs})
    assert response.status == expected


async def test_users_catalog_is_safe_and_identifies_owner_capability(team):
    client, _ = team
    response = await client.get("/admin/api/users")
    data = await response.json()
    assert data["can_assign_projects"] is True
    assert data["projects"] == [{"slug": "relay", "name": "Relay"}]
    subadmin = await client.get("/admin/api/users", headers={"Test-User": "subadmin@example.test"})
    assert (await subadmin.json())["can_assign_projects"] is False


async def test_finance_reads_crm_and_reports_but_cannot_execute(team):
    client, _ = team
    headers = {"Test-User": "finance@example.test"}
    for path in ("/admin/api/report", "/admin/api/crm/clients"):
        assert (await client.get(path, headers=headers)).status == 200
    for path in ("/conversations", "/admin/api/users"):
        assert (await client.get(path, headers=headers)).status == 403
    for path in ("/experts/run", "/mcp", "/admin/api/crm/sync", "/admin/api/crm/digest"):
        assert (await client.post(path, headers=headers)).status == 403


async def test_disable_preserves_identity_and_revokes_access(team):
    client, db = team
    response = await client.delete("/admin/api/users/sam@example.test")
    assert response.status == 200
    row = next(u for u in await db.list_users() if u["email"] == "sam@example.test")
    assert row["enabled"] == 0 and row["display_name"] == "sam"
    for email in ("sam@example.test", "unknown@example.test"):
        headers = {"Test-User": email}
        assert (await client.post("/experts/run", headers=headers)).status == 403
        response = await client.get("/admin/api/me", headers=headers)
        assert (await response.json())["allowed_tabs"] == []


async def test_pending_user_edit_rechecks_actor_after_role_change(team):
    _, db = team
    app = web.Application()
    app[DB_KEY] = db
    req = make_mocked_request("PUT", "/admin/api/users", app=app)
    req[identity.IDENTITY_KEY] = "alex@example.test"
    await db.set_user_role("second@example.test", "owner")
    async with db._upsert_lock:
        pending = asyncio.create_task(admin_users._save(
            req, email="new@example.test", role="owner", name="Nueva", enabled=True))
        await asyncio.sleep(0)  # La petición espera el lock con su rol anterior.
        await db.set_user_role("alex@example.test", "finance")
        identity.load_roles(await db.list_users())
    with pytest.raises(web.HTTPForbidden):
        await pending
    assert not any(u["email"] == "new@example.test" for u in await db.list_users())


async def test_pending_project_assignment_rechecks_owner_after_role_change(team):
    _, db = team
    app = web.Application()
    app[DB_KEY] = db
    req = make_mocked_request("PUT", "/admin/api/users", app=app)
    req[identity.IDENTITY_KEY] = "alex@example.test"
    await db.set_user_role("second@example.test", "owner")

    async with db._upsert_lock:
        pending = asyncio.create_task(admin_users._save(
            req, email="sam@example.test", role="member", name=None, enabled=True,
            project_slugs=["relay"]))
        await asyncio.sleep(0)  # La solicitud pasa la espera con su owner cacheado.
        await db.set_user_role("alex@example.test", "subadmin")
        identity.load_roles(await db.list_users())

    response = await pending
    assert response.status == 403
    users = {user["email"]: user for user in await db.list_users()}
    assert users["alex@example.test"]["role"] == "subadmin"
    assert users["sam@example.test"]["project_slugs"] == []


@pytest.mark.parametrize("body", [[], {"email": 123},
    {"email": "alex@example.test", "role": []},
    {"email": "alex@example.test", "role": "member", "enabled": "false"},
    {"email": "alex@example.test", "role": "member", "display_name": {}}])
async def test_user_input_is_validated(team, body):
    response = await team[0].put("/admin/api/users", json=body)
    assert response.status == 400


@pytest.mark.parametrize("origin", ["https://outside.test", "http://[malformed", "null"])
async def test_cross_origin_cannot_change_users(team, origin):
    response = await team[0].put("/admin/api/users", headers={"Origin": origin},
                                json={"email": "alex@example.test", "role": "owner"})
    assert response.status == 403


async def test_finance_project_catalog_has_no_paths_or_tools():
    db = AsyncMock()
    db.list_projects.return_value = [{"slug": "a", "name": "A", "repo_path": "secret-path",
                                     "defaults_json": {"secret": "value"}, "native_tools": ["shell"]}]
    app = web.Application()
    app[DB_KEY] = db
    req = make_mocked_request("GET", "/admin/api/projects", app=app)
    req[identity.IDENTITY_KEY] = "owner"
    from unittest.mock import patch
    with patch.object(identity, "role_of", return_value="finance"):
        data = json.loads((await admin_projects.api_projects(req)).text)
    assert data == {"projects": [{"slug": "a", "name": "A", "enabled": True}]}


async def test_finance_metrics_do_not_expose_raw_errors(monkeypatch):
    db = AsyncMock()
    db.metrics_summary.return_value = {"totals": {"cost_usd": 3},
                                       "error_breakdown": [{"error_type": "secret-path"}]}
    app = web.Application()
    app[DB_KEY] = db
    req = make_mocked_request("GET", "/admin/api/metrics/summary", app=app)
    monkeypatch.setattr(identity, "role_of", lambda _: "finance")
    data = json.loads((await admin_observability.api_metrics_summary(req)).text)
    assert data["error_breakdown"] == [] and data["totals"]["cost_usd"] == 3


def test_finance_crm_exposes_only_commercial_fields():
    data = admin_crm._finance_client({"id": 1, "name": "Cliente", "contacts": [{"email": "private"}],
        "notes": "privado", "deals": [{"name": "Venta", "stage": "open", "amount": 2, "notes": "privado"}]})
    assert data["contacts"] == [] and "notes" not in data
    assert data["deals"] == [{"name": "Venta", "stage": "open", "amount": 2}]


async def test_finance_health_does_not_call_github(monkeypatch):
    db = AsyncMock()
    db.list_crm_clients.return_value = [{"id": 1, "name": "Cliente", "domain": "example.test",
                                         "project_count": 1, "deals": []}]
    app = web.Application()
    app[DB_KEY] = db
    req = make_mocked_request("GET", "/admin/api/crm/health", app=app)
    monkeypatch.setattr(identity, "role_of", lambda _: "finance")
    probe = AsyncMock()
    monkeypatch.setattr(admin_crm.github_mod, "repo_slug", probe)
    data = json.loads((await admin_crm.api_crm_health(req)).text)
    assert data["clients"][0]["open_issues"] is None
    probe.assert_not_awaited()
    db.list_projects_for_client.assert_not_awaited()


@pytest.mark.parametrize("role,enabled", [("owner", False), ("member", True), ("finance", True)])
async def test_queued_write_rechecks_current_user(role, enabled, monkeypatch):
    from relay import execution_policy, task_workspace, task_pr
    db = AsyncMock()
    db.get_conversation_task.return_value = {"state": "active", "mode": "write"}
    db.claim_conversation_event.return_value = {"id": 1, "kind": "publish",
        "payload": {"role": "owner", "requested_by": "person@example.test"}}
    db.list_users.return_value = [{"email": "person@example.test", "role": role, "enabled": enabled}]
    inspect = AsyncMock()
    publish = AsyncMock()
    monkeypatch.setattr(task_workspace, "inspect_workspace", inspect)
    monkeypatch.setattr(task_pr, "publish", publish)
    previous_role = execution_policy.request_role.get()
    await task_service._consume({DB_KEY: db}, {}, "task")
    inspect.assert_not_awaited()
    publish.assert_not_awaited()
    assert db.update_conversation_task.call_args.kwargs["state"] == "blocked"
    assert execution_policy.request_role.get() == previous_role
