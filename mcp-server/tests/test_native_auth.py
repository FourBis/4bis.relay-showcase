"""Alta desde cero y sesiones locales: GitHub simulado, sin red ni secretos reales."""
import asyncio
import hashlib
import json
import time
import uuid
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import CookieJar, web
from aiohttp.test_utils import TestClient, TestServer

from relay import account_oauth, admin_observability, admin_users, finalization, identity, native_auth, user_accounts, task_service
from relay.app_state import BG_TASKS_KEY, DB_KEY, NOTIFY_KEY, PROGRESS_KEY, RUNNING_KEY, SKILLS_KEY
from relay.db import Database
from relay.server_common import browser_guard, localhost_guard
from relay.server_conversation_routes import conversations_create, conversations_get_messages
from relay.server_expert_routes import experts_run, experts_cancel, experts_status
from relay.server_projects import chats_get, chats_get_md


@pytest.fixture
async def auth(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_OWNER_EMAIL", "")
    monkeypatch.setenv("FOURBIS_DB_PATH", str(tmp_path / "relay.db"))
    for attr in ("_roles", "_disabled", "_names", "_project_grants"):
        monkeypatch.setattr(identity, attr, getattr(identity, attr).copy())
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    identity.load_roles(await db.list_users())
    app = web.Application(middlewares=[browser_guard, localhost_guard,
                                      identity.access_identity, identity.require_role])
    app[DB_KEY] = db
    app[RUNNING_KEY], app[PROGRESS_KEY] = {}, {}
    app[BG_TASKS_KEY] = set()
    app[NOTIFY_KEY] = None
    app[SKILLS_KEY] = AsyncMock()
    app[SKILLS_KEY].get_block.return_value = ""
    native_auth.register_routes(app)
    account_oauth.register_routes(app)
    app.router.add_get("/admin/", admin_observability.admin_index)
    app.router.add_get("/admin/api/me", admin_users.api_me)
    app.router.add_get("/admin/api/users", admin_users.api_users_list)
    app.router.add_post("/conversations", conversations_create)
    app.router.add_get("/conversations/{id}/messages", conversations_get_messages)
    app.router.add_post("/experts/run", experts_run)
    app.router.add_post("/experts/cancel/{chat_id}", experts_cancel)
    app.router.add_get("/experts/status/{chat_id}", experts_status)
    app.router.add_get("/chats/{id}", chats_get)
    app.router.add_get("/chats/{id}/md", chats_get_md)
    async with TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True)) as client:
        yield client, db


async def configure(client):
    response = await client.post("/admin/api/auth/setup", json={
        "client_id": "client-test", "client_secret": "secret-test"})
    assert response.status == 200, await response.text()


async def begin(client):
    response = await client.post("/admin/api/auth/github/start", json={})
    assert response.status == 200, await response.text()
    query = parse_qs(urlsplit((await response.json())["authorization_url"]).query)
    return query


async def finish(client, query, **headers):
    return await client.get("/admin/api/account/github/callback",
        params={"state": query["state"][0], "code": "fixture-code"},
        headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", **headers},
        allow_redirects=False)


def github(monkeypatch, *, subject=123, email="admin@example.test", verified=True):
    remote = AsyncMock(side_effect=[{"access_token": "never-persist-this-token"},
        {"id": subject, "login": "sample-admin"},
        [{"email": email, "primary": True, "verified": verified}]])
    monkeypatch.setattr(user_accounts, "oauth_request", remote)
    return remote


async def login(client, monkeypatch, **kwargs):
    github(monkeypatch, **kwargs)
    response = await finish(client, await begin(client))
    assert response.status == 302, await response.text()
    return response


async def test_first_admin_session_restart_and_logout(auth, monkeypatch):
    client, db = auth
    status = await (await client.get("/admin/api/auth/status")).json()
    assert status["setup_required"] and status["local_setup"] and not status["configured"]
    assert "login-setup-form" in await (await client.get("/admin/")).text()
    await configure(client)
    query = await begin(client)
    assert query["scope"] == ["read:user user:email"]
    assert query["code_challenge_method"] == ["S256"]
    remote = github(monkeypatch)
    response = await finish(client, query)
    assert response.status == 302
    assert remote.await_count == 3 and remote.call_args_list[0].kwargs["data"]["code_verifier"]
    cookie = response.cookies[native_auth.SESSION_COOKIE]
    assert cookie["httponly"] and cookie["samesite"] == "Lax" and not cookie["secure"]
    assert cookie["max-age"] == str(native_auth.SESSION_TTL)
    users = await db.list_users()
    assert len(users) == 1 and users[0]["email"] == "admin@example.test" and users[0]["role"] == "owner"
    assert await db.run("SELECT * FROM user_accounts") == []  # login no concede tools
    connection = await client.post("/admin/api/account/github/connect", json={})
    assert connection.status == 200
    tools_scope = parse_qs(urlsplit((await connection.json())["authorization_url"]).query)["scope"]
    assert tools_scope == ["repo read:user user:email"]
    sessions = await db.run("SELECT * FROM login_sessions")
    assert sessions[0]["token_hash"] == hashlib.sha256(cookie.value.encode()).hexdigest()
    assert "never-persist-this-token" not in json.dumps(sessions)
    public = await (await client.get("/admin/api/auth/status")).text()
    assert json.loads(public)["authenticated"] and "secret-test" not in public
    assert (await client.get("/admin/api/users")).status == 200
    assert (await finish(client, query)).status == 400  # one-use state
    assert (await client.post("/admin/api/auth/setup", json={"client_id": "another-id", "client_secret": "another-secret"})).status == 409

    # Misma DB y archivo de configuración después de reiniciar el proceso.
    monkeypatch.setattr(user_accounts, "_oauth_config", {})
    user_accounts.load_oauth_config()
    assert user_accounts.provider_config("github")["client_secret"] == "secret-test"
    restarted = Database(path=db.path)
    await restarted.init_schema()
    assert await native_auth.enabled(restarted)
    assert (await client.get("/admin/api/users")).status == 200
    logout = await client.post("/admin/api/auth/logout", json={})
    assert logout.status == 200 and await db.run("SELECT * FROM login_sessions") == []
    assert (await client.get("/admin/api/users")).status == 403
    assert "login-setup-form" in await (await client.get("/admin/")).text()
    assert (await client.get("/admin/api/users", headers={"Cookie": f"{native_auth.SESSION_COOKIE}={cookie.value}"})).status == 403


async def test_personal_session_sets_conversation_requester_not_author(auth, monkeypatch, tmp_path):
    client, db = auth
    await configure(client)
    await login(client, monkeypatch, email="personal@example.test")
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path),
                             "defaults_json": {}})
    monkeypatch.setattr("relay.server_conversation_routes.git_flow.is_git_repo",
                        AsyncMock(return_value=False))

    response = await client.post("/conversations", json={
        "project": "demo", "author": "spoof@example.test"})
    assert response.status == 201, await response.text()
    conversation = await db.get_conversation((await response.json())["id"])
    assert conversation["requested_by"] == "personal@example.test"
    assert conversation["author"] == "spoof@example.test"


@pytest.mark.parametrize("role", ["owner", "member"])
async def test_agent_reads_durable_result_while_markdown_export_is_pending(
        auth, monkeypatch, tmp_path, role):
    client, db = auth
    await configure(client)
    await login(client, monkeypatch, email="personal@example.test")
    if role == "member":
        await db.set_user_role("backup@example.test", "owner")
        await db.set_user_role("personal@example.test", role)
        identity.load_roles(await db.list_users())
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    monkeypatch.setattr("relay.server_conversation_routes.git_flow.is_git_repo",
                        AsyncMock(return_value=False))
    monkeypatch.setattr("relay.server_expert_routes._run_expert_bg", AsyncMock())

    created = await client.post("/conversations", json={
        "project": "demo", "author": "spoof@example.test"})
    assert created.status == 201, await created.text()
    conversation_id = (await created.json())["id"]
    conversation = await db.get_conversation(conversation_id)
    assert conversation["requested_by"] == "personal@example.test"
    assert conversation["author"] == "spoof@example.test"

    run = await client.post("/experts/run", json={
        "target": "demo", "conversation": conversation_id,
        "user": "consulta recuperable", "source": "api",
        "author": "spoof@example.test"})
    assert run.status == 202, await run.text()
    result_ref = await run.json()
    assert result_ref["conversation_id"] == conversation_id

    artifact = {"target": "demo", "user": "consulta recuperable",
                "content": "resultado durable", "error": ""}
    monkeypatch.setattr(finalization, "export", AsyncMock(return_value=None))
    await finalization.finish(
        db, result_ref["id"], artifact=artifact, status="ok",
        model="test", phase_at_end="finished")

    client.server.app[PROGRESS_KEY].clear()
    metadata = await (await client.get(f"/chats/{result_ref['id']}")).json()
    assert metadata["status"] == "ok" and not metadata.get("md_path")
    assert (await client.get(f"/chats/{result_ref['id']}/md")).status == 404

    response = await client.get(f"/conversations/{conversation_id}/messages")
    assert response.status == 200, await response.text()
    messages = (await response.json())["messages"]
    assistant = next(message for message in messages
                     if message["role"] == "assistant")
    assert assistant["content"] == "resultado durable"
    assert assistant["truncated"] is False

    await client.post("/admin/api/auth/logout", json={})
    assert (await client.get(
        f"/conversations/{conversation_id}/messages")).status == 403


@pytest.mark.parametrize("role", ["owner", "member"])
@pytest.mark.parametrize("conversation_field", ["conversation", "conversation_id"])
async def test_agent_session_retry_poll_cancel_and_durable_result(auth, monkeypatch, tmp_path, role, conversation_field):
    client, db = auth
    await configure(client)
    await login(client, monkeypatch, email="personal@example.test")
    if role == "member":
        await db.set_user_role("backup@example.test", "owner")
        await db.set_user_role("personal@example.test", role)
        identity.load_roles(await db.list_users())
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    cid = await db.create_conversation(project_slug="demo", requested_by="personal@example.test")
    await db.update_conversation_task(cid, mode="read_only", state="ready")
    monkeypatch.setattr(task_service, "start_pending", lambda *_: None)
    monkeypatch.setattr("relay.server_expert_routes._run_expert_bg", AsyncMock())
    body = {"target": "demo", conversation_field: cid, "user": "revisa", "request_id": "agent-1",
            "source": "api", "author": "spoof@example.test"}
    first = await client.post("/experts/run", json=body)
    assert first.status == 202, await first.text()
    run = await first.json()
    assert run["conversation_id"] == cid
    retry = await client.post("/experts/run", json=body)
    assert retry.status == 202 and (await retry.json())["id"] == run["id"]
    events = await db.list_conversation_events(cid)
    assert len(events) == 1 and events[0]["payload"]["requested_by"] == "personal@example.test"
    mismatch = await client.post("/experts/run", json={**body, "user": "otro pedido"})
    assert mismatch.status == 409 and len(await db.list_conversation_events(cid)) == 1
    conflict = await client.post("/experts/run", json={
        **body, "conversation": cid, "conversation_id": "different"})
    assert conflict.status == 400 and len(await db.list_conversation_events(cid)) == 1
    status = await client.get(f"/experts/status/{run['id']}")
    assert status.status == 200 and (await status.json())["phase"] == "queued"
    assert (await client.post(f"/experts/cancel/{run['id']}")).status == 200
    client.server.app[PROGRESS_KEY].clear()
    status = await client.get(f"/experts/status/{run['id']}")
    assert (await status.json())["finished"] is True
    result = await client.get(f"/chats/{run['id']}")
    assert result.status == 200 and (await result.json())["status"] == "cancelled"
    await client.post("/admin/api/auth/logout", json={})
    assert (await client.post("/experts/run", json=body)).status == 403
    assert (await client.get(f"/chats/{run['id']}")).status == 403
    assert len(await db.list_conversation_events(cid)) == 1


@pytest.mark.parametrize("state", ["ready", "paused"])
@pytest.mark.parametrize("lookup", ["explicit", "discord", "implicit"])
async def test_agent_cannot_append_to_another_members_read_only_task(auth, monkeypatch, tmp_path, state, lookup):
    client, db = auth
    await configure(client)
    await login(client, monkeypatch, email="personal@example.test")
    await db.set_user_role("backup@example.test", "owner")
    await db.set_user_role("personal@example.test", "member")
    identity.load_roles(await db.list_users())
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    cid = await db.create_conversation(project_slug="demo", requested_by="other@example.test",
        discord_thread_id="fixture-thread" if lookup == "discord" else None,
        conversation_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "relay:implicit:demo:intruder")) if lookup == "implicit" else None)
    await db.update_conversation_task(cid, mode="read_only", state=state)
    start = Mock()
    monkeypatch.setattr(task_service, "start_pending", start)
    before = await db.get_conversation(cid)
    body = {"target": "demo", "user": "nuevo trabajo", "request_id": "intruder"}
    if lookup == "explicit":
        body["conversation"] = cid
    elif lookup == "discord":
        body.update(discord_thread_id="fixture-thread", discord_user_id="spoofed")
    else:
        monkeypatch.setattr("relay.git_flow.is_git_repo", AsyncMock(return_value=True))
    response = await client.post("/experts/run", json=body)
    assert response.status == 403, await response.text()
    start.assert_not_called()
    assert await db.get_conversation(cid) == before
    assert await db.list_conversation_events(cid) == []


@pytest.mark.parametrize("change,expected", [("pause", 409), ("revoke", 403)])
async def test_agent_rechecks_task_and_permission_after_preparation(auth, monkeypatch, tmp_path, change, expected):
    client, db = auth
    await configure(client)
    await login(client, monkeypatch, email="personal@example.test")
    await db.set_user_role("backup@example.test", "owner")
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    cid = await db.create_conversation(project_slug="demo", requested_by="personal@example.test")
    await db.update_conversation_task(cid, mode="read_only", state="ready")
    async def change_during_preparation():
        if change == "pause":
            await db.update_conversation_task(cid, state="paused")
        else:
            await db.set_user_role("personal@example.test", "member", enabled=False)
            identity.load_roles(await db.list_users())
        return ""
    client.server.app[SKILLS_KEY].get_block.side_effect = change_during_preparation
    start = Mock()
    monkeypatch.setattr(task_service, "start_pending", start)
    response = await client.post("/experts/run", json={
        "target": "demo", "conversation": cid, "user": "revisa", "request_id": "late-change"})
    assert response.status == expected, await response.text()
    assert await db.list_conversation_events(cid) == []
    start.assert_not_called()


@pytest.mark.parametrize("headers", [{"Origin": "https://evil.test"},
    {"Host": "evil.test"}, {"X-Forwarded-For": "127.0.0.1"}, {"Forwarded": "for=127.0.0.1"},
    {"X-Forwarded-Proto": "https"},
    {"Cf-Access-Authenticated-User-Email": "admin@example.test"}, {"Sec-Fetch-Site": "cross-site"}])
async def test_bootstrap_rejects_untrusted_request_without_writing(auth, headers):
    client, db = auth
    response = await client.post("/admin/api/auth/setup", json={"client_id": "client-test", "client_secret": "secret-test"}, headers=headers)
    assert response.status == 403
    assert not user_accounts.oauth_config_path(db.path).exists()
    assert await db.list_users() == []


@pytest.mark.parametrize("invalid", ["cookie", "expired", "unverified", "subject", "denied"])
async def test_invalid_authorization_never_creates_admin(auth, monkeypatch, invalid):
    client, db = auth
    await configure(client)
    query = await begin(client)
    remote = github(monkeypatch, verified=invalid != "unverified", subject="invalid" if invalid == "subject" else 123)
    if invalid == "cookie":
        client.session.cookie_jar.clear()
    elif invalid == "expired":
        for pending in user_accounts.store_for(db).pending.values():
            pending["expires_at"] = 0
    elif invalid == "denied":
        remote.side_effect = user_accounts.AccountError("El proveedor rechazó la autorización.")
    response = await finish(client, query)
    assert response.status == 400
    assert "never-persist-this-token" not in await response.text()
    assert await db.list_users() == [] and not await native_auth.enabled(db)
    assert await db.run("SELECT * FROM github_logins") == []
    assert await db.run("SELECT * FROM login_sessions") == []
    if invalid in {"cookie", "expired"}:
        remote.assert_not_awaited()


async def test_session_cannot_be_replaced_by_same_email_other_subject(auth, monkeypatch):
    client, db = auth
    await configure(client)
    await login(client, monkeypatch)
    await client.post("/admin/api/auth/logout", json={})
    query = await begin(client)
    github(monkeypatch, subject=456)
    assert (await finish(client, query)).status == 400
    assert (await client.get("/admin/api/users")).status == 403
    # El id estable sigue siendo válido si cambió el correo en GitHub.
    await login(client, monkeypatch, email="changed@example.test")
    assert (await (await client.get("/admin/api/me")).json())["email"] == "admin@example.test"
    await db.set_user_role("backup@example.test", "owner")
    await db.set_user_role("admin@example.test", "owner", enabled=False)
    assert (await client.get("/admin/api/users")).status == 403
    github(monkeypatch)
    assert (await finish(client, await begin(client))).status == 400
    await db.set_user_role("admin@example.test", "owner", enabled=True)
    await db.run("UPDATE login_sessions SET expires_at=?", (time.time() - 1,))
    assert (await client.get("/admin/api/users")).status == 403


async def test_concurrent_first_admin_is_atomic(auth):
    _, db = auth
    results = await asyncio.gather(*(
        asyncio.to_thread(native_auth._finish_login, db, str(i), f"admin{i}@example.test", "admin", True, f"session-{i}")
        for i in range(1, 3)), return_exceptions=True)
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, user_accounts.AccountError) for result in results) == 1
    assert len(await db.list_users()) == len(await db.run("SELECT * FROM github_logins")) == len(await db.run("SELECT * FROM login_sessions")) == 1


async def test_existing_installation_is_not_reconfigured(auth):
    client, db = auth
    await db.set_user_role("existing@example.test", "owner")
    identity.load_roles(await db.list_users())
    assert (await client.get("/admin/api/users")).status == 200
    assert (await client.post("/admin/api/auth/setup", json={"client_id": "client-test", "client_secret": "secret-test"})).status == 409
    assert (await client.post("/admin/api/auth/github/start", json={})).status == 409
    assert not user_accounts.oauth_config_path(db.path).exists()
    assert not await native_auth.enabled(db)


async def test_setup_preserves_secrets_already_loaded_from_environment(auth, monkeypatch):
    client, _ = auth
    monkeypatch.setenv("RELAY_GOOGLE_CLIENT_ID", "google-client-test")
    monkeypatch.setenv("RELAY_GOOGLE_CLIENT_SECRET", "google-secret-test")
    user_accounts.load_oauth_config()
    assert user_accounts._config_value("RELAY_GOOGLE_CLIENT_SECRET") == "google-secret-test"
    await configure(client)
    assert user_accounts.provider_config("google")["client_secret"] == "google-secret-test"


async def test_demoted_admin_can_logout(auth, monkeypatch):
    client, db = auth
    await configure(client)
    await login(client, monkeypatch)
    await db.set_user_role("backup@example.test", "owner")
    await db.set_user_role("admin@example.test", "member")
    identity.load_roles(await db.list_users())
    assert (await client.post("/admin/api/auth/logout", json={})).status == 200
    assert await db.run("SELECT * FROM login_sessions") == []


async def test_configuration_change_during_callback_does_not_create_owner(auth, monkeypatch):
    client, db = auth
    await configure(client)
    query = await begin(client)
    responses = iter([{"access_token": "fixture-token"}, {"id": 123, "login": "sample-admin"},
                      [{"email": "admin@example.test", "primary": True, "verified": True}]])
    async def changing_config(*args, **kwargs):
        user_accounts._oauth_config["RELAY_GITHUB_CLIENT_ID"] = "changed-client-id"
        return next(responses)
    monkeypatch.setattr(user_accounts, "oauth_request", changing_config)
    assert (await finish(client, query)).status == 400
    assert await db.list_users() == [] and not await native_auth.enabled(db)


async def test_real_application_restarts_with_admin_and_session(tmp_path, monkeypatch):
    from relay import server
    monkeypatch.setenv("RELAY_OWNER_EMAIL", "")
    monkeypatch.setenv("FOURBIS_DB_PATH", str(tmp_path / "relay.db"))
    for attr in ("_roles", "_disabled", "_names", "_project_grants"):
        monkeypatch.setattr(identity, attr, getattr(identity, attr).copy())
    async with TestClient(TestServer(server.create_app()), cookie_jar=CookieJar(unsafe=True)) as client:
        assert "login-setup-form" in await (await client.get("/admin/")).text()
        await configure(client)
        response = await login(client, monkeypatch)
        session = response.cookies[native_auth.SESSION_COOKIE].value
        assert (await client.get("/admin/api/users")).status == 200
    monkeypatch.setattr(user_accounts, "_oauth_config", {})
    async with TestClient(TestServer(server.create_app())) as client:
        assert (await client.get("/admin/api/users")).status == 403
        headers = {"Cookie": f"{native_auth.SESSION_COOKIE}={session}"}
        response = await client.get("/admin/api/me", headers=headers)
        me = await response.json()
        assert me["email"] == "admin@example.test" and me["role"] == "owner"
        assert (await client.get("/admin/api/users", headers=headers)).status == 200
        assert user_accounts.provider_config("github")["client_secret"] == "secret-test"


@pytest.mark.parametrize("url,allowed", [("http://localhost:8413", True),
    ("http://127.0.0.1:8413", True), ("http://[::1]:8413", True),
    ("https://relay.example.test", True), ("http://relay.example.test", False),
    ("http://localhost.evil.test", False), ("http://user@localhost", False),
    ("https://relay.example.test/path", False)])
def test_oauth_http_is_only_allowed_for_literal_loopback(monkeypatch, url, allowed):
    monkeypatch.setenv("RELAY_PUBLIC_URL", url)
    monkeypatch.setenv("RELAY_GITHUB_CLIENT_ID", "client-test")
    monkeypatch.setenv("RELAY_GITHUB_CLIENT_SECRET", "secret-test")
    if allowed:
        cfg = user_accounts.provider_config("github")
        assert account_oauth.cookie_options(cfg)[1] == url.startswith("https:")
    else:
        with pytest.raises(user_accounts.AccountError):
            user_accounts.provider_config("github")
