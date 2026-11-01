"""Explicit invitations and a separate administrative conversation list."""
from aiohttp import web

from . import conversation_access, identity
from .app_state import DB_KEY
from .server_common import _require_auth


async def _respond(request, operation):
    try:
        return web.json_response(await operation(), headers={"Cache-Control": "no-store"})
    except PermissionError:
        raise web.HTTPNotFound(text="Conversación no disponible.") from None
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=422)


@_require_auth
async def sharing_list(request):
    return await _respond(request, lambda: conversation_access.share_view(
        request.app[DB_KEY], request.match_info["id"], identity.requester(request)))


@_require_auth
async def sharing_put(request):
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        body = None
    if (not isinstance(body, dict) or set(body) != {"permission"}
            or not isinstance(body["permission"], str) or body["permission"] not in {"read", "collaborate"}):
        return web.json_response({"error": "Indica únicamente permission: read o collaborate"}, status=422)
    return await _respond(request, lambda: conversation_access.set_share(request.app[DB_KEY], request.match_info["id"],
        identity.requester(request), request.match_info["email"].strip().lower(), body["permission"]))


@_require_auth
async def sharing_delete(request):
    return await _respond(request, lambda: conversation_access.revoke_share(request.app[DB_KEY], request.match_info["id"],
        identity.requester(request), request.match_info["email"].strip().lower()))


@_require_auth
async def admin_listing(request):
    if identity.role_of(request) != "owner":
        raise web.HTTPForbidden()
    try:
        limit = max(1, min(int(request.query.get("limit", "100")), 200))
    except ValueError:
        return web.json_response({"error": "Límite inválido"}, status=422)
    return await _respond(request, lambda: _admin_rows(request, limit))


async def _admin_rows(request, limit):
    rows = await conversation_access.listing(request.app[DB_KEY], identity.requester(request), scope="admin",
        project=request.query.get("project"), status=request.query.get("status"), limit=limit)
    return {"conversations": rows, "scope": "admin"}


def register(app):
    app.router.add_get("/admin/api/conversations", admin_listing)
    app.router.add_get("/conversations/{id}/shares", sharing_list)
    app.router.add_put("/conversations/{id}/shares/{email}", sharing_put)
    app.router.add_delete("/conversations/{id}/shares/{email}", sharing_delete)
