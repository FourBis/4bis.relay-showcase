"""Authorized issue selection, linking and explicit synchronization."""
from __future__ import annotations

from aiohttp import web

from . import identity, issue_links
from .app_state import DB_KEY
from .server_common import _require_auth
from .user_accounts import AccountError


async def _project(request, slug):
    project = await request.app[DB_KEY].get_project(slug)
    if not project:
        raise web.HTTPNotFound(text='{"error":"No existe el proyecto"}', content_type="application/json")
    if not identity.can_write_project(request, project):
        raise web.HTTPForbidden(text='{"error":"Proyecto no autorizado para tu cuenta"}', content_type="application/json")
    return project


async def _conversation(request):
    db, cid = request.app[DB_KEY], request.match_info["id"]
    conv = await db.get_conversation(cid)
    if not conv:
        raise web.HTTPNotFound(text='{"error":"No existe la tarea"}', content_type="application/json")
    return db, cid, await _project(request, conv["project_slug"])


async def _body(request):
    try:
        body = await request.json()
    except ValueError as exc:
        raise web.HTTPBadRequest(text='{"error":"JSON inválido"}', content_type="application/json") from exc
    if not isinstance(body, dict):
        raise ValueError("Se requiere un objeto JSON")
    return body


def _error(exc):
    if isinstance(exc, AccountError):
        raise exc
    status = 403 if isinstance(exc, PermissionError) else 502 if isinstance(exc, (RuntimeError, OSError)) else 422
    return web.json_response({"error": str(exc)}, status=status)


@_require_auth
async def issues(request):
    project = await _project(request, request.match_info["slug"])
    try:
        rows = await issue_links.list_issues(project)
        await issue_links.authorize_project(project, identity.requester(request),
                                            lambda: _project(request, project["slug"]))
        return web.json_response({"issues": rows})
    except (ValueError, RuntimeError, OSError) as exc:
        return _error(exc)


@_require_auth
async def link(request):
    db, cid, project = await _conversation(request)
    try:
        body = await _body(request)
        await issue_links.bind(db, project, cid, body.get("issue"), identity.requester(request),
                               resolution_complete=body.get("resolution_complete"),
                               authorize=lambda: _project(request, project["slug"]))
        from . import issue_sync
        return web.json_response(await issue_sync.snapshot(db, cid))
    except (ValueError, RuntimeError, OSError) as exc:
        return _error(exc)


@_require_auth
async def snapshot(request):
    db, cid, _project_info = await _conversation(request)
    from . import issue_sync
    return web.json_response(await issue_sync.snapshot(db, cid))


@_require_auth
async def refresh(request):
    db, cid, project = await _conversation(request)
    try:
        action = (await _body(request)).get("action")
        if action not in {"refresh", "retry"}:
            raise ValueError("action debe ser refresh o retry")
        from . import issue_sync
        if action == "retry":
            await issue_sync.retry(db, cid, identity.requester(request),
                                   authorize=lambda: _project(request, project["slug"]))
        result = await issue_sync.refresh(db, project, cid, identity.requester(request),
                                           authorize=lambda: _project(request, project["slug"]))
        return web.json_response(result)
    except (ValueError, RuntimeError, OSError) as exc:
        return _error(exc)
