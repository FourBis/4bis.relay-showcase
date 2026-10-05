"""Primer Admin e inicio de sesión local con identidad verificada por GitHub."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import tempfile
import time
from urllib.parse import urlencode, urlsplit

from aiohttp import web
from cryptography.fernet import Fernet

from . import identity, user_accounts
from .admin_users import _body
from .app_state import DB_KEY
from .db_support import now_iso
from .user_accounts import AccountError

SESSION_COOKIE = "relay-session"
SESSION_TTL = 12 * 60 * 60
ENABLED = "github_login_enabled"


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _local(request):
    from .server_common import _loopback_peer
    return (_loopback_peer(request.remote)
            and request.url.host in {"127.0.0.1", "localhost", "::1"}
            and not any(name in request.headers for name in (
                "Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto",
                "Cf-Access-Jwt-Assertion", "Cf-Access-Authenticated-User-Email")))


async def enabled(db):
    return await db.get_config(ENABLED) == "1"


async def session_actor(request):
    """None conserva el acceso anterior; cadena vacía exige iniciar sesión."""
    db = request.app[DB_KEY] if DB_KEY in request.app else None
    if db is None or not await enabled(db):
        return None
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token or len(token) > 128:
        return ""
    rows = await db.run(
        "SELECT s.email FROM login_sessions s JOIN users u ON u.email=s.email "
        "WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1",
        (_hash(token), time.time()))
    return rows[0]["email"] if rows else ""


async def status(request):
    db = request.app[DB_KEY]
    initial = not await db.list_users()
    configured = user_accounts.oauth_config_path(db.path).is_file()
    from .account_oauth import _response
    return _response({"setup_required": initial, "configured": configured,
        "enabled": await enabled(db), "authenticated": bool(await session_actor(request)),
        "local_setup": _local(request),
        "callback_url": f"{request.scheme}://{request.host}/admin/api/account/github/callback"})


async def setup(request):
    body = await _body(request)
    from .account_oauth import _response
    db = request.app[DB_KEY]
    if not _local(request):
        return _response({"error": "Configura el primer Admin desde localhost, sin proxy."}, 403)
    client_id, secret = body.get("client_id"), body.get("client_secret")
    if (not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{8,128}", client_id)
            or not isinstance(secret, str) or not 8 <= len(secret) <= 256
            or any(c.isspace() or ord(c) < 32 for c in secret)):
        return _response({"error": "Revisa el Client ID y el Client secret de GitHub."}, 400)
    async with db._upsert_lock:
        if await db.list_users() or await enabled(db):
            return _response({"error": "El alta inicial ya está cerrada."}, 409)
        path = user_accounts.oauth_config_path(db.path)
        settings = {"RELAY_PUBLIC_URL": f"{request.scheme}://{request.host}",
                    "RELAY_GITHUB_CLIENT_ID": client_id, "RELAY_GITHUB_CLIENT_SECRET": secret,
                    "RELAY_GITHUB_SCOPES": "repo read:user user:email",
                    "RELAY_OAUTH_KEY": Fernet.generate_key().decode()}
        # ponytail: configuración de una instalación local, fuera del repo y
        # con permisos del usuario; un servicio multiusuario necesita un vault.
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".oauth-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(settings, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        user_accounts.load_oauth_config()
        user_accounts.store_for(db).pending.clear()
    return _response({"configured": True})


async def start(request):
    await _body(request)
    from .account_oauth import STATE_TTL, _response, cookie_options
    db, now = request.app[DB_KEY], time.time()
    initial = not await db.list_users()
    if initial and not _local(request):
        return _response({"error": "Crea el primer Admin desde localhost, sin proxy."}, 403)
    if not initial and not await enabled(db):
        return _response({"error": "Esta instalación conserva su acceso existente."}, 409)
    try:
        cfg = user_accounts.provider_config("github")
        if request.host != urlsplit(cfg["redirect_uri"]).netloc:
            raise AccountError("Abre la dirección registrada de esta instalación.")
    except AccountError as exc:
        return _response({"error": str(exc)}, 400)
    store = user_accounts.store_for(db)
    store.pending = {k: v for k, v in store.pending.items() if v["expires_at"] > now}
    if len(store.pending) >= 256:
        return _response({"error": "Hay demasiados accesos pendientes. Intenta más tarde."}, 429)
    state, verifier, browser = (secrets.token_urlsafe(32) for _ in range(3))
    store.pending[_hash(state)] = {"purpose": "login", "provider": "github",
        "bootstrap": initial, "browser": _hash(browser), "verifier": verifier,
        "expires_at": now + STATE_TTL}
    params = {"client_id": cfg["client_id"], "redirect_uri": cfg["redirect_uri"],
              "scope": "read:user user:email", "state": state,
              "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
              "code_challenge_method": "S256", "prompt": "select_account"}
    response = _response({"authorization_url": cfg["authorize"] + "?" + urlencode(params)})
    cookie, secure = cookie_options(cfg)
    response.set_cookie(cookie, browser, max_age=STATE_TTL, secure=secure,
                        httponly=True, samesite="Lax", path="/")
    return response


def pending_login(request):
    pending = user_accounts.store_for(request.app[DB_KEY]).pending.get(
        _hash(request.query.get("state", "")), {})
    return pending.get("purpose") == "login"


async def callback(request):
    from .account_oauth import _callback_error, cookie_options
    db = request.app[DB_KEY]
    pending = user_accounts.store_for(db).pending.pop(_hash(request.query.get("state", "")), None)
    try:
        cfg = user_accounts.provider_config("github")
        cookie, secure = cookie_options(cfg)
        if (not pending or pending["expires_at"] <= time.time()
                or not secrets.compare_digest(pending["browser"], _hash(request.cookies.get(cookie, "")))
                or request.host != urlsplit(cfg["redirect_uri"]).netloc
                or (pending["bootstrap"] and not _local(request))):
            raise AccountError("La autorización venció o no pertenece a este navegador. Inicia sesión nuevamente.")
        if request.query.get("error") or not request.query.get("code"):
            raise AccountError("No se autorizó el acceso. Puedes volver a intentarlo.")
        tokens = await user_accounts.oauth_request("POST", cfg["token"],
            headers={"Accept": "application/json"}, data={
                "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
                "code": request.query["code"], "redirect_uri": cfg["redirect_uri"],
                "code_verifier": pending["verifier"]})
        token = tokens.get("access_token")
        if not isinstance(token, str) or not token or str(tokens.get("token_type", "bearer")).lower() != "bearer":
            raise AccountError("GitHub no entregó una autorización válida.")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        profile = await user_accounts.oauth_request("GET", cfg["profile"], headers=headers)
        subject, login = str(profile.get("id") or ""), profile.get("login")
        if (not subject.isdecimal() or int(subject) <= 0 or not isinstance(login, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login)):
            raise AccountError("GitHub no confirmó la identidad de la cuenta.")
        addresses = await user_accounts.oauth_request("GET", "https://api.github.com/user/emails",
            headers=headers, params={"per_page": 100}, response_type=list)
        address = next((row.get("email") for row in addresses if isinstance(row, dict)
                        and row.get("primary") is True and row.get("verified") is True), None)
        if not isinstance(address, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address):
            raise AccountError("GitHub debe confirmar un correo principal verificado.")
        session = secrets.token_urlsafe(32)
        # SQLite decide el primer Admin y guarda identidad/sesión en una sola
        # transacción. Un callback concurrente no puede crear otro propietario.
        async with db._upsert_lock:
            if user_accounts.provider_config("github") != cfg:
                raise AccountError("La configuración cambió. Inicia sesión nuevamente.")
            await asyncio.to_thread(
                _finish_login, db, subject, address.lower(), login, pending["bootstrap"], session)
            identity.load_roles(await db.list_users())
    except AccountError as exc:
        return _callback_error(str(exc))
    except sqlite3.IntegrityError:
        return _callback_error("Esa identidad ya está vinculada; inicia sesión nuevamente.")
    response = web.HTTPFound("/admin/#/account", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    response.del_cookie(cookie, path="/", secure=secure, httponly=True, samesite="Lax")
    response.set_cookie(SESSION_COOKIE, session, max_age=SESSION_TTL, secure=secure,
                        httponly=True, samesite="Lax", path="/")
    return response


def _finish_login(db, subject, email, login, bootstrap, session):
    conn = db._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if bootstrap:
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise AccountError("El primer Admin ya fue configurado. Inicia sesión con su cuenta.")
            conn.execute("INSERT INTO users(email,role,display_name,created_at) VALUES(?,'owner',?,?)",
                         (email, login, now_iso()))
            conn.execute("INSERT INTO github_logins(subject,email) VALUES(?,?)", (subject, email))
            conn.execute("INSERT INTO system_config(key,value) VALUES(?,'1') "
                         "ON CONFLICT(key) DO UPDATE SET value='1'", (ENABLED,))
        else:
            bound = conn.execute("SELECT email FROM github_logins WHERE subject=?", (subject,)).fetchone()
            if not bound:
                raise AccountError("Esta cuenta GitHub no está habilitada para iniciar sesión en esta instalación.")
            email = bound["email"]
        user = conn.execute("SELECT enabled FROM users WHERE email=?", (email,)).fetchone()
        if not user or not user["enabled"]:
            raise AccountError("La cuenta de Relay no está habilitada.")
        conn.execute("DELETE FROM login_sessions WHERE expires_at<=?", (time.time(),))
        conn.execute("INSERT INTO login_sessions(token_hash,email,expires_at) VALUES(?,?,?)",
                     (_hash(session), email, time.time() + SESSION_TTL))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


async def logout(request):
    await _body(request)
    from .account_oauth import _response
    await request.app[DB_KEY].run("DELETE FROM login_sessions WHERE token_hash=?",
                                (_hash(request.cookies.get(SESSION_COOKIE, "")),))
    response = _response({"logged_out": True})
    response.del_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="Lax")
    return response


def register_routes(app):
    app.router.add_get("/admin/api/auth/status", status)
    app.router.add_post("/admin/api/auth/setup", setup)
    app.router.add_post("/admin/api/auth/github/start", start)
    app.router.add_post("/admin/api/auth/logout", logout)
