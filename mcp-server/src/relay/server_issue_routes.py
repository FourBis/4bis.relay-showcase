"""Authorized issue selection, linking and explicit synchronization."""
from __future__ import annotations

from aiohttp import web

from . import github_picker, identity, issue_links
from .app_state import DB_KEY
from .server_common import _require_auth
from .user_accounts import AccountError

EXTERNAL_RECORD_KEY = web.RequestKey("external_issue_record", str)


async def _project(request, slug):
    if EXTERNAL_RECORD_KEY in request:
        from . import external_pr_access
        try:
            project, _, _ = await external_pr_access.require_record(
                request.app[DB_KEY], request[EXTERNAL_RECORD_KEY], identity.requester(request))
            if project["slug"].casefold() != slug.casefold():
                raise PermissionError("El proyecto del registro cambió")
            return project
        except LookupError:
            raise web.HTTPNotFound() from None
        except (PermissionError, ValueError):
            raise web.HTTPForbidden(text='{"error":"Registro externo no autorizado"}', content_type="application/json") from None
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
    if (await db.get_conversation_task(cid)).get("mode") == "external_pr":
        # Keep the project authorization callback bound to this external record
        # across GitHub IO. Ordinary issue handlers retain their existing ACL.
        request[EXTERNAL_RECORD_KEY] = cid
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
    # Existing consumers keep their original shape; paginated pickers send page.
    if "q" in request.query or "page" in request.query:
        return await _options(request, "issues")
    project = await _project(request, request.match_info["slug"])
    try:
        rows = await issue_links.list_issues(project)
        await issue_links.authorize_project(project, identity.requester(request),
                                            lambda: _project(request, project["slug"]))
        return web.json_response({"issues": rows}, headers={"Cache-Control": "no-store"})
    except (ValueError, RuntimeError, OSError) as exc:
        return _error(exc)


async def _options(request, kind):
    project = await _project(request, request.match_info["slug"])
    try:
        if set(request.query) - {"q", "page"} or any(len(request.query.getall(key)) != 1 for key in request.query):
            raise ValueError("Usa únicamente q y page una vez por consulta")
        result = await github_picker.options(project, kind, request.query.get("q", ""), request.query.get("page", "1"))
        await issue_links.authorize_project(project, identity.requester(request),
                                            lambda: _project(request, project["slug"]))
        return web.json_response(result, headers={"Cache-Control": "no-store"})
    except github_picker.PickerError as exc:
        body = {"error": str(exc), "code": exc.code, "manual_entry_allowed": True}
        if exc.retry_after is not None:
            body["retry_after"] = exc.retry_after
        return web.json_response(body, status=exc.status, headers={"Cache-Control": "no-store"})
    except ValueError as exc:
        return web.json_response({"error": str(exc), "code": "query_invalid", "manual_entry_allowed": True},
                                 status=422, headers={"Cache-Control": "no-store"})
    except PermissionError:
        return web.json_response({"error": "Proyecto no autorizado para tu cuenta"},
                                 status=403, headers={"Cache-Control": "no-store"})
    except OSError:
        return web.json_response({"error": "No se pudo consultar GitHub. Puedes ingresar la URL manualmente.",
                                 "code": "github_unavailable", "manual_entry_allowed": True},
                                 status=502, headers={"Cache-Control": "no-store"})


@_require_auth
async def pulls(request):
    return await _options(request, "pulls")


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
