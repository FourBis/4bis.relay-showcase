"""Authorized local records of external GitHub PRs."""
from aiohttp import web

from . import external_pr, identity
from .app_state import DB_KEY
from .server_common import _require_auth
from .task_pr import TaskPRError


async def _project(request, slug):
    project = await request.app[DB_KEY].get_project(slug)
    if not project:
        raise web.HTTPNotFound(text='{"error":"No existe el proyecto"}', content_type="application/json")
    if not identity.can_write_project(request, project):
        raise web.HTTPForbidden(text='{"error":"Proyecto no autorizado para tu cuenta"}', content_type="application/json")
    return project


async def _body(request):
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("Se requiere un objeto JSON")
    return body


@_require_auth
async def register(request):
    project = await _project(request, request.match_info["slug"])
    try:
        result = await external_pr.register(request.app[DB_KEY], project, await _body(request), identity.requester(request),
                                            authorize=lambda: _project(request, project["slug"]))
        return web.json_response(result)
    except (TaskPRError, OSError) as exc:
        return web.json_response({"error": str(exc)}, status=502)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=422)


@_require_auth
async def action(request):
    db, cid = request.app[DB_KEY], request.match_info["id"]
    conv = await db.get_conversation(cid)
    if not conv:
        return web.json_response({"error": "No existe la tarea"}, status=404)
    project = await _project(request, conv["project_slug"])
    try:
        body, who = await _body(request), identity.requester(request)
        if body.get("action") == "refresh":
            result = await external_pr.refresh(db, project, cid, who, authorize=lambda: _project(request, project["slug"]))
        elif body.get("action") == "evidence":
            result = await external_pr.add_evidence(db, project, cid, body, who, authorize=lambda: _project(request, project["slug"]))
        else:
            raise ValueError("Acción desconocida")
        return web.json_response(result)
    except (TaskPRError, OSError) as exc:
        return web.json_response({"error": str(exc), "conversation_id": cid}, status=502)
    except ValueError as exc:
        return web.json_response({"error": str(exc), "conversation_id": cid}, status=422)
