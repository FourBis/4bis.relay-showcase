"""Keep external PR receipts within the current project's authorized team."""
from __future__ import annotations

from aiohttp import web

from . import identity
from .app_state import DB_KEY


async def can_read(request, db, conversation):
    if identity.role_of(request) == identity.OWNER_ROLE:
        return True
    task = await db.get_conversation_task(conversation["id"])
    if not isinstance(task, dict) or task.get("mode") != "external_pr":
        return True
    project = await db.get_project(conversation.get("project_slug", ""))
    return identity.can_write_project(request, project)


async def visible_records(request, db, records):
    return [row for row in records if await can_read(request, db, row)]


@web.middleware
async def guard(request, handler):
    # Restrict only external registrations; existing chat permission rules stay
    # in their handlers. Check before rendering detail, history, diff or task.
    cid = request.match_info.get("id")
    db = request.app.get(DB_KEY)
    if db is not None and cid and request.path.startswith("/conversations/"):
        conversation = await db.get_conversation(cid)
        if conversation and not await can_read(request, db, conversation):
            return web.json_response({"error": "no existe"}, status=404)
    return await handler(request)
