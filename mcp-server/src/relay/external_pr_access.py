"""Project work-register access; it never grants access to ordinary chat routes."""
from __future__ import annotations

import json

from . import conversation_access, identity


def _project_tx(db, conn, slug, actor):
    user = conversation_access.user_tx(conn, actor)
    row = conn.execute("SELECT * FROM projects WHERE slug=? COLLATE NOCASE", (slug,)).fetchone()
    if not row:
        raise LookupError("No existe el proyecto")
    project = db._parse_project(row)
    defaults = project["defaults_json"]
    if (not user or not project["enabled"] or not isinstance(defaults, dict) or defaults.get("read_only")
            or not identity.user_can_write_project(user, project["slug"])):
        raise PermissionError("Proyecto no autorizado para tu cuenta")
    return project, user


async def require_project(db, slug, actor):
    return await conversation_access.transaction(db, lambda conn: _project_tx(db, conn, slug, actor))


def _record_tx(db, conn, cid, actor):
    row = conn.execute("SELECT project_slug,task_json FROM conversations WHERE id=?", (cid,)).fetchone()
    if not row:
        raise LookupError("No existe la tarea")
    try:
        task = json.loads(row["task_json"] or "{}")
    except (ValueError, TypeError):
        task = {}
    if not isinstance(task, dict) or task.get("mode") != "external_pr" or task.get("mcp_private"):
        raise ValueError("La tarea no es un PR externo")
    project, user = _project_tx(db, conn, row["project_slug"], actor)
    return project, task, user


async def require_record(db, cid, actor):
    return await conversation_access.transaction(db, lambda conn: _record_tx(db, conn, cid, actor))


async def authorize_route(db, cid, actor, path, method):
    """Only these explicit register operations use project membership, never chat ACL."""
    allowed = {
        "/conversations/{id}/task": {"GET", "HEAD"},
        "/conversations/{id}/external-pr": {"POST"},
        "/conversations/{id}/external-pr/images": {"GET", "HEAD", "POST"},
        "/conversations/{id}/external-pr/images/{draft_id}": {"GET", "HEAD"},
        "/conversations/{id}/external-pr/images/{draft_id}/confirm": {"POST"},
        "/conversations/{id}/issue": {"POST"},
        "/conversations/{id}/issue-sync": {"GET", "HEAD", "POST"},
    }
    if method not in allowed.get(path, set()):
        return False
    try:
        await require_record(db, cid, actor)
    except (LookupError, ValueError):
        return False
    return True  # PermissionError deliberately propagates, including to creators/admins.


async def snapshot(db, cid, actor):
    """Read local state only and recheck fresh access before exposing it."""
    from . import external_pr, external_review_view
    project, task, user = await require_record(db, cid, actor)
    state = await external_review_view.enrich(db, external_pr.snapshot(task, project),
                                            can_configure=user["role"] == "owner")
    _, _, current = await require_record(db, cid, actor)
    if current["role"] != "owner":
        state.pop("review_configuration", None)
        state["allowed_actions"] = [action for action in state.get("allowed_actions", [])
                                    if action != "external_review_configure"]
    return state


# Compatibility with the public candidate's middleware registration. Ordinary
# conversations retain their own handlers; this shim grants no register writes.
async def can_read(request, db, conversation):
    task = await db.get_conversation_task(conversation["id"])
    if not isinstance(task, dict) or task.get("mode") != "external_pr":
        return True
    try:
        await require_record(db, conversation["id"], identity.requester(request))
    except (LookupError, ValueError, PermissionError):
        return False
    return True


async def visible_records(request, db, records):
    return [row for row in records if await can_read(request, db, row)]


from aiohttp import web
from .app_state import DB_KEY


@web.middleware
async def guard(request, handler):
    cid = request.match_info.get("id")
    db = request.app.get(DB_KEY)
    if db is not None and cid and request.path.startswith("/conversations/"):
        conversation = await db.get_conversation(cid)
        if conversation and not await can_read(request, db, conversation):
            return web.json_response({"error": "no existe"}, status=404)
    return await handler(request)
