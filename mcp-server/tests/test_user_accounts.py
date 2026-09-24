"""OAuth multiusuario offline: no usa cuentas ni credenciales reales."""
import asyncio
import json
import os
import time
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from cryptography.fernet import Fernet

from relay import account_oauth, identity, user_accounts
from relay.app_state import DB_KEY
from relay.db import Database
from relay.user_accounts import AccountError, GOOGLE_SCOPES, bind_actor, require_account, store_for


@pytest.fixture
async def accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_OAUTH_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RELAY_PUBLIC_URL", "https://relay.example.test")
    for provider in ("GITHUB", "GOOGLE"):
        monkeypatch.setenv(f"RELAY_{provider}_CLIENT_ID", "client-test")
        monkeypatch.setenv(f"RELAY_{provider}_CLIENT_SECRET", "secret-test")
    monkeypatch.setenv("RELAY_OWNER_EMAIL", "")
    db = Database(path=tmp_path / "accounts.db")
    await db.init_schema()
    for email, role in (("alex@example.test", "member"), ("sam@example.test", "member"),
                        ("finance@example.test", "finance")):
        await db.set_user_role(email, role)
    for attr in ("_roles", "_disabled", "_names"):
        monkeypatch.setattr(identity, attr, getattr(identity, attr).copy())
    identity.load_roles(await db.list_users())
    store = store_for(db)

    @web.middleware
    async def actor(request, handler):
        email = request.headers.get("Test-User", "alex@example.test")
        request[identity.IDENTITY_KEY] = email
        with bind_actor(db, email):
            return await handler(request)

    app = web.Application(middlewares=[actor, identity.require_role])
    app[DB_KEY] = db
    account_oauth.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, store
    finally:
        await client.close()


def token(value="token-a", **extra):
    return {"access_token": value, "token_type": "bearer", "expires_in": 3600,
            "refresh_token": "refresh-a", "scope": " ".join(GOOGLE_SCOPES), **extra}


async def save_github(store, email="alex@example.test", subject=123, **extra):
    await store.save("github", email, {"id": subject, "login": f"user-{subject}"}, token(**extra))


async def start(client, provider="github", **headers):
    response = await client.post(f"/admin/api/account/{provider}/connect", json={},
        headers={"Host": "relay.example.test", "Origin": "https://relay.example.test", **headers})
    assert response.status == 200, await response.text()
    query = parse_qs(urlsplit((await response.json())["authorization_url"]).query)
    return query, response.cookies[account_oauth.COOKIE].value


async def finish(client, query, cookie, provider="github", **headers):
    return await client.get(f"/admin/api/account/{provider}/callback",
        params={"state": query["state"][0], "code": "one-use-code"}, allow_redirects=False,
        headers={"Host": "relay.example.test", "Cookie": f"{account_oauth.COOKIE}={cookie}", **headers})


async def test_tokens_encrypted_and_not_exposed(accounts):
    client, store = accounts
    await save_github(store)
    row = await store.row("github", "alex@example.test")
    assert "token-a" not in row["token_blob"] and "refresh-a" not in row["token_blob"]
    with bind_actor(store.db, "alex@example.test"):
        assert (await require_account("github"))["access_token"] == "token-a"
    public = await (await client.get("/admin/api/account/connections")).text()
    assert "token-a" not in public and "token_blob" not in public and "secret-test" not in public
    assert json.loads(public)["connections"][0]["connected"]


def test_oauth_config_loader_prefers_process_scrubs_secrets_and_whitelists(monkeypatch):
    key = Fernet.generate_key().decode()
    registry = {
        "RELAY_PUBLIC_URL": "https://registry.example.test",
        "RELAY_GITHUB_CLIENT_ID": "registry-id",
        "RELAY_GITHUB_CLIENT_SECRET": "registry-secret",
        "RELAY_GOOGLE_CLIENT_ID": "google-id",
        "RELAY_OAUTH_KEY": key,
        "UNRELATED_SECRET": "must-not-load",
    }
    monkeypatch.setattr(user_accounts, "_read_user_environment", lambda: registry)
    for name in user_accounts.OAUTH_CONFIG_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RELAY_GITHUB_CLIENT_SECRET", "process-secret")
    user_accounts.load_oauth_config()

    assert user_accounts._config_value("RELAY_GITHUB_CLIENT_SECRET") == "process-secret"
    assert user_accounts._config_value("RELAY_GOOGLE_CLIENT_ID") == "google-id"
    assert user_accounts._config_value("UNRELATED_SECRET") == ""
    assert "RELAY_GITHUB_CLIENT_SECRET" not in os.environ
    assert "RELAY_OAUTH_KEY" not in os.environ
    assert user_accounts._cipher().encrypt(b"test")


async def test_callback_error_escapes_html(accounts):
    payload = "<script>alert('x')</script>"
    response = account_oauth._callback_error(payload)
    assert response.status == 400
    assert "&lt;script&gt;" in response.text
    assert payload not in response.text
    assert "Content-Security-Policy" in response.headers


async def test_identity_middleware_returns_409_and_restores_actor(accounts):
    _, store = accounts
    request = make_mocked_request("GET", "/health", app={DB_KEY: store.db})
    previous = (store, "alex@example.test")
    token_value = user_accounts.current_actor.set(previous)

    async def rejected(_request):
        assert user_accounts.current_actor.get() == (store, identity.OWNER)
        raise AccountError("connection unavailable")

    try:
        response = await identity.access_identity(request, rejected)
        assert response.status == 409
        assert user_accounts.current_actor.get() == previous
    finally:
        user_accounts.current_actor.reset(token_value)


async def test_id_unique_and_account_isolation(accounts):
    _, store = accounts
    await save_github(store)
    with pytest.raises(AccountError, match="otro integrante"):
        await save_github(store, email="sam@example.test")
    for email in ("sam@example.test", "owner", "unknown@example.test", "finance@example.test"):
        with bind_actor(store.db, email), pytest.raises(AccountError):
            await require_account("github")
    with pytest.raises(AccountError):
        await require_account("github")


async def test_disabled_and_wrong_encryption_key_block(accounts, monkeypatch):
    _, store = accounts
    await save_github(store)
    await store.db.set_user_role("alex@example.test", "member", enabled=False)
    with bind_actor(store.db, "alex@example.test"), pytest.raises(AccountError, match="habilitada"):
        await require_account("github")
    await store.db.set_user_role("alex@example.test", "member")
    monkeypatch.setenv("RELAY_OAUTH_KEY", Fernet.generate_key().decode())
    with bind_actor(store.db, "alex@example.test"), pytest.raises(AccountError, match="abrir"):
        await require_account("github")


async def test_refresh_serialized_and_revocation_fails_closed(accounts, monkeypatch):
    _, store = accounts
    await save_github(store, expires_in=1)
    renew = AsyncMock(return_value=token("fresh-token"))
    monkeypatch.setattr(user_accounts, "oauth_request", renew)
    with bind_actor(store.db, "alex@example.test"):
        first, second = await asyncio.gather(require_account("github"), require_account("github"))
    assert first["access_token"] == second["access_token"] == "fresh-token"
    assert renew.await_count == 1
    await save_github(store, expires_in=1)
    renew.side_effect = AccountError("Conecta nuevamente", reauth=True)
    with bind_actor(store.db, "alex@example.test"), pytest.raises(AccountError):
        await require_account("github")
    assert (await store.row("github", "alex@example.test"))["status"] == "reconnect"


async def test_google_requires_own_verified_email_and_scopes(accounts):
    _, store = accounts
    profile = {"sub": "google-123", "email": "alex@example.test", "email_verified": True}
    for wrong in ({**profile, "email": "sam@example.test"}, {**profile, "email_verified": False}):
        with pytest.raises(AccountError):
            await store.save("google", "alex@example.test", wrong, token())
    with pytest.raises(AccountError, match="lectura y envío"):
        await store.save("google", "alex@example.test", profile, token(scope="openid email"))
    await store.save("google", "alex@example.test", profile, token())
    with bind_actor(store.db, "alex@example.test"):
        assert (await require_account("google"))["email"] == "alex@example.test"


async def test_oauth_pkce_callback_binds_identity_and_consumes_state(accounts, monkeypatch):
    client, store = accounts
    remote = AsyncMock(side_effect=[token(), {"id": 123, "login": "user-123"}])
    monkeypatch.setattr(user_accounts, "oauth_request", remote)
    query, cookie = await start(client)
    assert query["code_challenge_method"] == ["S256"] and len(query["code_challenge"][0]) == 43
    response = await finish(client, query, cookie)
    assert response.status == 302 and response.headers["Location"] == "/admin/#/account"
    assert remote.call_args_list[0].kwargs["data"]["code_verifier"]
    assert (await finish(client, query, cookie)).status == 400
    assert remote.await_count == 2


@pytest.mark.parametrize("invalid", ["cookie", "actor", "expired", "provider"])
async def test_oauth_rejects_invalid_state_before_network(accounts, monkeypatch, invalid):
    client, store = accounts
    remote = AsyncMock()
    monkeypatch.setattr(user_accounts, "oauth_request", remote)
    query, cookie = await start(client)
    if invalid == "expired":
        for state in store.pending.values():
            state["expires_at"] = time.time() - 1
    response = await finish(client, query, "wrong" if invalid == "cookie" else cookie,
        provider="google" if invalid == "provider" else "github",
        **({"Test-User": "sam@example.test"} if invalid == "actor" else {}))
    assert response.status == 400
    remote.assert_not_awaited()


async def test_disconnect_blocks_future_use_and_pending_callback(accounts):
    client, store = accounts
    await save_github(store)
    query, cookie = await start(client)
    response = await client.delete("/admin/api/account/github")
    assert response.status == 200
    with bind_actor(store.db, "alex@example.test"), pytest.raises(AccountError):
        await require_account("github")
    assert (await finish(client, query, cookie)).status == 400


async def test_connect_rejects_cross_origin_and_finance_github(accounts):
    client, _ = accounts
    for headers in ({"Origin": "https://evil.test"}, {"Test-User": "finance@example.test"}):
        response = await client.post("/admin/api/account/github/connect", json={}, headers=headers)
        assert response.status == 403


async def test_expired_without_refresh_and_changed_key_show_reconnect(accounts, monkeypatch):
    client, store = accounts
    await save_github(store, refresh_token="")
    await store.db.run("UPDATE user_accounts SET expires_at=?", (time.time() - 1,))
    status = (await (await client.get("/admin/api/account/connections")).json())["connections"][0]
    assert not status["connected"] and status["needs_reconnect"]
    await save_github(store)
    monkeypatch.setenv("RELAY_OAUTH_KEY", Fernet.generate_key().decode())
    status = (await (await client.get("/admin/api/account/connections")).json())["connections"][0]
    assert not status["connected"] and status["needs_reconnect"]


@pytest.mark.parametrize("ttl", [0, -1, True, "invalid"])
async def test_invalid_expiry_never_saves_credentials(accounts, ttl):
    _, store = accounts
    with pytest.raises(AccountError, match="vigencia"):
        await save_github(store, expires_in=ttl)
    assert await store.row("github", "alex@example.test") is None


async def test_disconnect_invalidates_callback_already_waiting_for_lock(accounts, monkeypatch):
    client, store = accounts
    query, cookie = await start(client)
    remote = AsyncMock()
    monkeypatch.setattr(user_accounts, "oauth_request", remote)
    lock = store.lock("github", "alex@example.test")
    await lock.acquire()
    callback = asyncio.create_task(finish(client, query, cookie))
    try:
        for _ in range(100):
            if not store.pending:
                break
            await asyncio.sleep(.01)
        assert not store.pending  # callback consumió state y espera el lock
        store.generations[("github", "alex@example.test")] = 1  # desconexión que obtuvo lock primero
    finally:
        lock.release()
    assert (await callback).status == 400
    remote.assert_not_awaited()


async def test_native_comments_in_shared_chat_use_current_actor(accounts, monkeypatch):
    import httpx
    from relay.account_tools import account_tools
    from relay import github

    _, store = accounts
    await save_github(store)
    await save_github(store, email="sam@example.test", subject=456, value="token-b")
    monkeypatch.setattr(github, "repo_slug", AsyncMock(return_value="fourbis/project"))
    seen = []
    async def respond(request):
        seen.append((request.url.path, request.headers["Authorization"], json.loads(request.content)))
        return httpx.Response(201, json={"id": len(seen)})
    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_class(transport=httpx.MockTransport(respond)))
    async def comment(email, body):
        with bind_actor(store.db, email):
            tools = {tool.name: tool.function for tool in account_tools({"repo_path": "repo"})}
            return json.loads(await tools["github_comment"](12, body))
    a, b = await asyncio.gather(comment("alex@example.test", "A"), comment("sam@example.test", "B"))
    assert {a["actor"], b["actor"]} == {"user-123", "user-456"}
    assert {(auth, body["body"]) for _, auth, body in seen} == {("Bearer token-a", "A"), ("Bearer token-b", "B")}
    assert all(path == "/repos/fourbis/project/issues/12/comments" for path, _, _ in seen)
    await store.disconnect("github", "sam@example.test")
    assert "error" in await comment("sam@example.test", "blocked")
    assert len(seen) == 2


async def test_oauth_transport_errors_do_not_expose_provider_body(accounts, monkeypatch):
    import httpx
    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(401,
        json={"error": "invalid_grant", "detail": "secret-never-exposed"}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_class(transport=transport))
    with pytest.raises(AccountError) as caught:
        await user_accounts.oauth_request("POST", "https://oauth2.googleapis.com/token")
    assert caught.value.reauth and "secret-never-exposed" not in str(caught.value)
