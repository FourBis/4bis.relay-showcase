"""OAuth web de GitHub/Google ligado a la identidad verificada de Relay."""
from __future__ import annotations

import base64
import hashlib
import html
import secrets
import time
from urllib.parse import urlencode, urlsplit

from aiohttp import web

from . import identity, user_accounts
from .admin_users import _body, _check_origin
from .app_state import DB_KEY
from .user_accounts import AccountError, GOOGLE_SCOPES, PROVIDERS, provider_config, store_for

COOKIE = "__Host-relay-oauth"
STATE_TTL = 600


def _response(data, status=200):
    return web.json_response(data, status=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def _callback_error(message):
    return web.Response(status=400, content_type="text/html", headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"},
        text='<!doctype html><html lang="es"><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1">'
             '<title>Conectar cuenta · Relay</title><main><h1>No se conectó la cuenta</h1>'
             f'<p>{html.escape(message)}</p><a href="/admin/#/account">Volver a Mi cuenta</a>'
             '</main></html>')


async def connections(request):
    email = identity.requester(request)
    store = store_for(request.app[DB_KEY])
    items = []
    for provider, info in PROVIDERS.items():
        configured, allowed, message = True, True, ""
        try:
            provider_config(provider)
            user_accounts._cipher()
        except (AccountError, ValueError) as exc:
            configured, message = False, str(exc)
        try:
            await store.check_user(provider, email)
        except AccountError as exc:
            allowed, message = False, str(exc)
        row = await store.row(provider, email) if allowed else None
        connected = bool(row and row["status"] == "connected")
        if connected:
            try:
                data = store.decrypt(row["token_blob"])
                connected = (data.get("provider") == provider and data.get("relay_email") == email)
                if row["expires_at"] and row["expires_at"] <= time.time():
                    connected = connected and bool(data.get("refresh_token")) and (
                        not row["refresh_expires_at"] or row["refresh_expires_at"] > time.time())
            except AccountError:
                connected = False
        items.append({"provider": provider, "label": info["label"], "configured": configured,
                      "connected": connected, "login": (row or {}).get("login", ""),
                      "email": (row or {}).get("account_email", ""),
                      "needs_reconnect": bool(row and not connected),
                      "can_connect": configured and allowed, "message": message})
    return _response({"email": email, "local_identity": email == identity.OWNER, "connections": items})


async def connect(request):
    await _body(request)
    provider, email = request.match_info["provider"], identity.requester(request)
    store = store_for(request.app[DB_KEY])
    try:
        await store.check_user(provider, email)
        cfg = provider_config(provider)
        user_accounts._cipher()
        if request.host != urlsplit(cfg["redirect_uri"]).netloc:
            raise AccountError("Abre Relay desde su dirección HTTPS pública para conectar la cuenta.")
    except (AccountError, ValueError) as exc:
        return _response({"error": str(exc)}, 403)
    now = time.time()
    store.pending = {k: v for k, v in store.pending.items() if v["expires_at"] > now}
    if len(store.pending) >= 256:
        return _response({"error": "Hay demasiadas conexiones pendientes. Intenta más tarde."}, 429)
    state, verifier, browser = (secrets.token_urlsafe(32) for _ in range(3))
    store.pending[hashlib.sha256(state.encode()).hexdigest()] = {
        "provider": provider, "email": email, "verifier": verifier,
        "browser": hashlib.sha256(browser.encode()).hexdigest(), "expires_at": now + STATE_TTL,
        "generation": store.generations.get((provider, email), 0)}
    params = {"client_id": cfg["client_id"], "redirect_uri": cfg["redirect_uri"],
              "response_type": "code", "state": state,
              "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
              "code_challenge_method": "S256"}
    if provider == "github":
        params.update(prompt="select_account", allow_signup="false")
    else:
        params.update(scope=" ".join(sorted(GOOGLE_SCOPES | {"openid", "email"})),
                      access_type="offline", prompt="consent select_account", login_hint=email)
    response = _response({"authorization_url": cfg["authorize"] + "?" + urlencode(params)})
    response.set_cookie(COOKIE, browser, max_age=STATE_TTL, secure=True, httponly=True,
                        samesite="Lax", path="/")
    return response


async def callback(request):
    provider, email = request.match_info["provider"], identity.requester(request)
    store = store_for(request.app[DB_KEY])
    state = request.query.get("state", "")
    pending = store.pending.pop(hashlib.sha256(state.encode()).hexdigest(), None)
    browser = hashlib.sha256(request.cookies.get(COOKIE, "").encode()).hexdigest()
    if (not pending or pending["provider"] != provider or pending["email"] != email
            or pending["expires_at"] <= time.time()
            or not secrets.compare_digest(pending["browser"], browser)):
        return _callback_error("La autorización venció o no pertenece a esta sesión. Conecta nuevamente.")
    if request.query.get("error") or not request.query.get("code"):
        return _callback_error("No se autorizó la conexión. Puedes volver a Mi cuenta e intentarlo.")
    try:
        await store.check_user(provider, email)
        cfg = provider_config(provider)
        if request.host != urlsplit(cfg["redirect_uri"]).netloc:
            raise AccountError("El callback debe entrar por la dirección HTTPS registrada de Relay.")
        async with store.lock(provider, email):
            if pending["generation"] != store.generations.get((provider, email), 0):
                raise AccountError("La conexión fue cancelada. Vuelve a conectar tu cuenta.")
            tokens = await user_accounts.oauth_request("POST", cfg["token"],
                headers={"Accept": "application/json"}, data={
                    "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
                    "code": request.query["code"], "redirect_uri": cfg["redirect_uri"],
                    "grant_type": "authorization_code", "code_verifier": pending["verifier"]})
            access_token = tokens.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise AccountError("El proveedor no entregó un token de acceso.")
            profile = await user_accounts.oauth_request("GET", cfg["profile"],
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
            await store.save(provider, email, profile, tokens)
    except AccountError as exc:
        return _callback_error(str(exc))
    response = web.HTTPFound("/admin/#/account", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    response.del_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="Lax")
    return response


async def disconnect(request):
    _check_origin(request)
    provider, email = request.match_info["provider"], identity.requester(request)
    try:
        await store_for(request.app[DB_KEY]).disconnect(provider, email)
    except AccountError as exc:
        return _response({"error": str(exc)}, 403)
    # Retirar tokens de Relay es inmediato. El usuario puede además revocar
    # la autorización en el proveedor; no retrasar el corte por red externa.
    return _response({"disconnected": True})


def register_routes(app):
    app.router.add_get("/admin/api/account/connections", connections)
    app.router.add_post("/admin/api/account/{provider}/connect", connect)
    app.router.add_get("/admin/api/account/{provider}/callback", callback)
    app.router.add_delete("/admin/api/account/{provider}", disconnect)
