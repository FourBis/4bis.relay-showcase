"""Correo personal autenticado por la cuenta OAuth del actor actual."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import uuid
from email import policy
from email.message import EmailMessage
from time import time
from urllib.parse import quote

import httpx
from aiohttp import web

from .admin_users import _body
from .app_state import DB_KEY
from .user_accounts import AccountError, current_actor, require_account
from .user_mail_content import message_view as _message_view

_GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
_MAX_LIMIT = 50
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_SEND_FIELDS = {"request_id", "to", "cc", "bcc", "subject", "body"}
_SEND_REQUIRED = {"request_id", "to", "subject", "body"}
_DRAFT_TTL = 60 * 60
_DRAFTS_PER_USER = 20


def _json(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status,
                             headers={"Cache-Control": "no-store"})


class _GoogleUnauthorized(Exception):
    pass


class _GoogleFailure(Exception):
    pass


class _GoogleTimeout(Exception):
    pass


def _actor():
    actor = current_actor.get()
    if actor is None:
        raise web.HTTPUnauthorized(text="Inicia sesión para abrir tu correo.")
    return actor


async def _account():
    try:
        return _actor(), await require_account("google")
    except AccountError:
        raise web.HTTPConflict(
            text="Vincula de nuevo tu cuenta de Google para usar Correo personal.") from None


async def _google_json(method: str, path: str, account: dict, **kwargs) -> dict:
    token = account.get("access_token")
    if not isinstance(token, str) or not token:
        raise _GoogleUnauthorized
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.request(
                method, f"{_GMAIL_API}/{path.lstrip('/')}",
                headers={"Authorization": f"Bearer {token}"}, **kwargs)
    except httpx.TimeoutException as exc:
        raise _GoogleTimeout from exc
    except httpx.HTTPError as exc:
        raise _GoogleFailure from exc
    if response.status_code == 401:
        raise _GoogleUnauthorized
    if not response.is_success:
        raise _GoogleFailure
    if response.status_code == 204:
        return {}
    try:
        data = response.json()
    except ValueError as exc:
        raise _GoogleFailure from exc
    if not isinstance(data, dict):
        raise _GoogleFailure
    return data


async def _invalidate(store, email: str) -> None:
    await store.invalidate("google", email)


async def _google_error(exc: Exception, store, email: str, *, sent: bool = False):
    if isinstance(exc, _GoogleUnauthorized):
        await _invalidate(store, email)
        return web.json_response(
            {"error": "google_reconnect_required"}, status=401)
    if isinstance(exc, _GoogleTimeout):
        return web.json_response(
            {"error": "delivery_uncertain" if sent else "google_timeout"},
            status=504)
    return web.json_response(
        {"error": "delivery_uncertain" if sent else "google_unavailable"},
        status=502)


def _header(message: dict, name: str) -> str:
    name = name.lower()
    for item in message.get("payload", {}).get("headers", []):
        if str(item.get("name", "")).lower() == name:
            return str(item.get("value", ""))
    return ""


async def _read_messages(account: dict, query: str, limit: int) -> list[dict]:
    listing = await _google_json(
        "GET", "messages", account,
        params={"q": query, "maxResults": limit})
    ids = [row.get("id") for row in listing.get("messages", [])
           if isinstance(row, dict) and isinstance(row.get("id"), str)]

    async def get_message(message_id: str) -> dict:
        return await _google_json(
            "GET", f"messages/{quote(message_id, safe='')}", account,
            params={"format": "metadata",
                    "metadataHeaders": ["From", "Subject", "Date"]})

    results = await asyncio.gather(*(get_message(mid) for mid in ids))
    return [{"id": msg.get("id", ""), "from": _header(msg, "From"),
             "subject": _header(msg, "Subject"), "date": _header(msg, "Date"),
             "snippet": str(msg.get("snippet", ""))}
            for msg in results]


async def mcp_read_mail(arguments: dict) -> dict:
    """Actor-bound Gmail read for an explicitly requested shared chat turn."""
    actor = current_actor.get()
    if actor is None:
        return {"ok": False, "error": "Conecta Gmail desde Mi cuenta antes de pedir correo personal."}
    store, email = actor
    try:
        account = await require_account("google")
        query = arguments.get("query", "")
        message_id = arguments.get("message_id")
        if message_id is not None:
            if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id):
                return {"ok": False, "error": "El identificador del correo no es válido."}
            message = await _google_json(
                "GET", f"messages/{quote(message_id, safe='')}", account,
                params={"format": "full"})
            return {"ok": True, "message": _message_view(message, message_id)}
        try:
            limit = int(arguments.get("max_results", 10))
        except (TypeError, ValueError):
            return {"ok": False, "error": "Filtro o cantidad de mensajes inválido."}
        if not isinstance(query, str) or len(query) > 500 or not 1 <= limit <= _MAX_LIMIT:
            return {"ok": False, "error": "Filtro o cantidad de mensajes inválido."}
        return {"ok": True, "messages": await _read_messages(account, query, limit)}
    except AccountError:
        return {"ok": False, "error": "Conecta tu cuenta de Gmail en Mi cuenta."}
    except (_GoogleUnauthorized, _GoogleTimeout, _GoogleFailure) as exc:
        if isinstance(exc, _GoogleUnauthorized):
            await _invalidate(store, email)
        return {"ok": False, "error": "Gmail no está disponible; revisa Mi cuenta."}


def _draft_map(store) -> dict[str, dict]:
    return store.mail_drafts


def _prune_drafts(drafts: dict[str, dict], now: float) -> None:
    for draft_id, draft in list(drafts.items()):
        if draft["expires_at"] <= now:
            del drafts[draft_id]


def _public_draft(draft_id: str, draft: dict) -> dict:
    return {"draft_id": draft_id, **{
        key: draft[key] for key in (
            "to", "cc", "bcc", "subject", "body", "created_at")}}


def create_mail_draft(store, email: str, account_email: str, arguments: dict) -> dict:
    allowed = {"to", "cc", "bcc", "subject", "body"}
    if set(arguments) - allowed or not {"to", "subject", "body"} <= set(arguments):
        raise ValueError("invalid_fields")
    recipients = {
        "to": _addresses(arguments["to"], "to", required=True),
        "cc": _addresses(arguments.get("cc"), "cc"),
        "bcc": _addresses(arguments.get("bcc"), "bcc"),
    }
    subject, content = arguments["subject"], arguments["body"]
    _validate_message(subject, content)
    # Assign through EmailMessage to apply the same header parser as send.
    message = EmailMessage(policy=policy.SMTP)
    message["To"] = ", ".join(recipients["to"])
    if recipients["cc"]:
        message["Cc"] = ", ".join(recipients["cc"])
    if recipients["bcc"]:
        message["Bcc"] = ", ".join(recipients["bcc"])
    message["Subject"] = subject
    message["From"] = account_email
    message.set_content(content)

    now = time()
    drafts = _draft_map(store)
    _prune_drafts(drafts, now)
    owned = sorted(
        ((draft["created_at"], draft_id) for draft_id, draft in drafts.items()
         if draft["email"] == email))
    while len(owned) >= _DRAFTS_PER_USER:
        _, oldest = owned.pop(0)
        drafts.pop(oldest, None)
    draft_id = uuid.uuid4().hex
    draft = {"email": email, "account_email": account_email,
             **recipients, "subject": subject, "body": content,
             "created_at": now, "expires_at": now + _DRAFT_TTL}
    drafts[draft_id] = draft
    return _public_draft(draft_id, draft)


async def mcp_create_mail_draft(arguments: dict) -> dict:
    actor = current_actor.get()
    if actor is None:
        return {"ok": False, "error": "Conecta Gmail desde Mi cuenta antes de preparar un correo."}
    store, email = actor
    try:
        account = await require_account("google")
        draft = create_mail_draft(store, email, account["email"], arguments)
    except AccountError:
        return {"ok": False, "error": "Conecta tu cuenta de Gmail en Mi cuenta."}
    except (TypeError, ValueError, KeyError):
        return {"ok": False, "error": "Los destinatarios, asunto o cuerpo del correo no son válidos."}
    return {"ok": True, "draft_id": draft["draft_id"], "draft": draft,
            "open_url": "/admin/#/account"}


def _validate_message(subject, content) -> None:
    if (not isinstance(subject, str) or not subject.strip()
            or "\r" in subject or "\n" in subject or len(subject) > 998
            or not isinstance(content, str) or not content.strip() or len(content) > 100_000):
        raise ValueError("invalid_message")


async def api_gmail_messages(request: web.Request) -> web.Response:
    actor, account = await _account()
    store, owner_email = actor
    query = request.query.get("q", "")
    if len(query) > 500:
        return web.json_response({"error": "invalid_query"}, status=400)
    try:
        limit = int(request.query.get("limit", "20"))
    except ValueError:
        return web.json_response({"error": "invalid_limit"}, status=400)
    if not 1 <= limit <= _MAX_LIMIT:
        return web.json_response({"error": "invalid_limit"}, status=400)

    try:
        results = await _read_messages(account, query, limit)
    except (_GoogleUnauthorized, _GoogleTimeout, _GoogleFailure) as exc:
        return await _google_error(exc, store, owner_email)
    return _json({"messages": results})


async def api_gmail_message(request: web.Request) -> web.Response:
    actor, account = await _account()
    store, owner_email = actor
    message_id = request.match_info.get("id", "")
    if not _MESSAGE_ID.fullmatch(message_id):
        return _json({"error": "message_not_found"}, status=404)
    try:
        message = await _google_json(
            "GET", f"messages/{quote(message_id, safe='')}", account,
            params={"format": "full"})
    except (_GoogleUnauthorized, _GoogleTimeout, _GoogleFailure) as exc:
        return await _google_error(exc, store, owner_email)
    return _json(_message_view(message, message_id))


def _addresses(value, field: str, *, required: bool = False) -> list[str]:
    if value is None and not required:
        return []
    values = [value] if isinstance(value, str) else value
    if (not isinstance(values, list) or (required and not values)
            or len(values) > 100
            or any(not isinstance(item, str) or not item.strip()
                   or len(item) > 320 or "\r" in item or "\n" in item
                   for item in values)):
        raise ValueError(f"invalid_{field}")
    if not values:
        return []
    out = [item.strip() for item in values]
    probe = EmailMessage(policy=policy.SMTP)
    header_name = field.title()
    probe[header_name] = ", ".join(out)
    header = probe[header_name]
    parsed = getattr(header, "addresses", ())
    if (not parsed or header.defects
            or any("@" not in address.addr_spec for address in parsed)):
        raise ValueError(f"invalid_{field}")
    return out


def _payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _prepare_send(body: dict) -> tuple[str, dict, EmailMessage]:
    if set(body) - _SEND_FIELDS or not _SEND_REQUIRED <= set(body):
        raise ValueError("invalid_fields")
    try:
        request_id = str(uuid.UUID(body["request_id"]))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid_request_id") from None
    recipients = {
        "to": _addresses(body["to"], "to", required=True),
        "cc": _addresses(body.get("cc"), "cc"),
        "bcc": _addresses(body.get("bcc"), "bcc"),
    }
    subject, content = body["subject"], body["body"]
    _validate_message(subject, content)
    message = EmailMessage(policy=policy.SMTP)
    message["To"] = ", ".join(recipients["to"])
    if recipients["cc"]:
        message["Cc"] = ", ".join(recipients["cc"])
    if recipients["bcc"]:
        message["Bcc"] = ", ".join(recipients["bcc"])
    message["Subject"] = subject.strip()
    message.set_content(content)
    payload = {"to": recipients["to"], "cc": recipients["cc"],
               "bcc": recipients["bcc"], "subject": subject.strip(),
               "body": content}
    return request_id, payload, message


async def _existing_send(db, email: str, request_id: str, payload_hash: str):
    rows = await db.run(
        "SELECT state, payload_hash, message_id FROM account_mail_sends "
        "WHERE email=? AND request_id=?", (email, request_id))
    if not rows:
        return None
    row = rows[0]
    if row["payload_hash"] != payload_hash:
        return web.json_response({"error": "request_id_conflict"}, status=409)
    if row["state"] == "sent":
        return web.json_response({
            "request_id": request_id, "message_id": row["message_id"],
            "state": "sent", "replayed": True})
    return web.json_response({"error": "delivery_uncertain"}, status=409)


async def api_gmail_send(request: web.Request) -> web.Response:
    try:
        body = await _body(request)
        request_id, payload, message = _prepare_send(body)
    except web.HTTPException:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        code = str(exc) if str(exc).startswith("invalid_") else "invalid_message"
        return _json({"error": code}, status=400)

    try:
        actor = _actor()
    except web.HTTPException:
        raise
    store, owner_email = actor
    db = store.db
    payload_hash = _payload_hash(payload)
    prior = await _existing_send(db, owner_email, request_id, payload_hash)
    if prior is not None:
        return prior

    try:
        account = await require_account("google")
    except AccountError:
        return _json({"error": "google_reconnect_required"}, status=409)
    sender = account.get("email")
    if not isinstance(sender, str) or not sender.strip() or "\r" in sender or "\n" in sender:
        return _json({"error": "google_reconnect_required"}, status=409)
    try:
        message["From"] = sender.strip()
        if message["From"].defects or "@" not in message["From"].addresses[0].addr_spec:
            return _json({"error": "google_reconnect_required"}, status=409)
    except (ValueError, IndexError):
        return web.json_response({"error": "google_reconnect_required"}, status=409)

    claimed = await db.run(
        "INSERT OR IGNORE INTO account_mail_sends "
        "(email, request_id, state, payload_hash, created_at) "
        "VALUES (?, ?, 'pending', ?, ?) RETURNING request_id",
        (owner_email, request_id, payload_hash, time()))
    if not claimed:
        prior = await _existing_send(db, owner_email, request_id, payload_hash)
        return prior or _json({"error": "delivery_uncertain"}, status=409)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    try:
        result = await _google_json(
            "POST", "messages/send", account,
            json={"raw": raw})
    except (_GoogleUnauthorized, _GoogleTimeout, _GoogleFailure) as exc:
        await db.run(
            "UPDATE account_mail_sends SET state='uncertain' "
            "WHERE email=? AND request_id=? AND state='pending'",
            (owner_email, request_id))
        if isinstance(exc, _GoogleUnauthorized):
            await _invalidate(store, owner_email)
            return _json({"error": "google_reconnect_required"}, status=401)
        return _json({"error": "delivery_uncertain"},
                     status=504 if isinstance(exc, _GoogleTimeout) else 502)
    message_id = result.get("id")
    if not isinstance(message_id, str) or not message_id:
        await db.run(
            "UPDATE account_mail_sends SET state='uncertain' "
            "WHERE email=? AND request_id=? AND state='pending'",
            (owner_email, request_id))
        return _json({"error": "delivery_uncertain"}, status=502)
    await db.run(
        "UPDATE account_mail_sends SET state='sent', message_id=? "
        "WHERE email=? AND request_id=? AND state='pending'",
        (message_id, owner_email, request_id))
    return _json({
        "request_id": request_id, "message_id": message_id,
        "state": "sent", "replayed": False})


async def api_gmail_drafts(request: web.Request) -> web.Response:
    store, email = _actor()
    try:
        await store.check_user("google", email)
    except AccountError:
        return _json({"error": "google_reconnect_required"}, status=409)
    now = time()
    drafts = _draft_map(store)
    _prune_drafts(drafts, now)
    items = [_public_draft(draft_id, draft)
             for draft_id, draft in drafts.items()
             if draft["email"] == email]
    items.sort(key=lambda draft: draft["created_at"], reverse=True)
    return _json({"drafts": items})


def register_routes(app: web.Application) -> None:
    app.router.add_get("/admin/api/account/gmail/drafts", api_gmail_drafts)
    app.router.add_get("/admin/api/account/gmail/messages", api_gmail_messages)
    app.router.add_get(
        "/admin/api/account/gmail/messages/{id}", api_gmail_message)
    app.router.add_post("/admin/api/account/gmail/send", api_gmail_send)
