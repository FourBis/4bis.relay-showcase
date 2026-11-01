"""Privacidad de consultas MCP y registros de PR en los accesos HTTP existentes."""
from __future__ import annotations

import json

from aiohttp import web

from . import config, identity, conversation_access, external_pr_access
from .app_state import DB_KEY

USER_KEY = web.RequestKey("private_mcp_user", dict)
VISIBILITY_KEY = web.RequestKey("private_mcp_visibility", dict)


async def personal_user(request):
    if USER_KEY not in request:
        actor = identity.requester(request)
        request[USER_KEY] = next((u for u in await request.app[DB_KEY].list_users()
            if u["email"] == actor and actor not in {identity.OWNER, "anonymous"}), {})
    return request[USER_KEY]


def _scope_allowed(user, project):
    """Require explicit project scope and current personal grants."""
    defaults = (project or {}).get("defaults_json") or {}
    return bool(user.get("email") not in {None, "", identity.OWNER, "anonymous"}
        and user.get("enabled") and not user.get("archived_at")
        and project and project.get("enabled")
        and isinstance(defaults, dict) and defaults.get("mcp_consultations") is True
        and identity.user_can_write_project(user, project["slug"]))


def allowed_project(user, project):
    return config.private_mcp_enabled() and _scope_allowed(user, project)


def allowed_project_tx(conn, user, project):
    """Use the durable flag inside the final authorization transaction."""
    row = conn.execute("SELECT value FROM system_config WHERE key=?",
                       ("RELAY_PRIVATE_MCP_ENABLED",)).fetchone()
    return bool(row and row[0] == "1" and _scope_allowed(user, project))


async def authorized_project(db, actor, slug):
    """Fresh account/project check for queued consultation admission."""
    user = await conversation_access.current_user(db, actor)
    project = await db.get_project(slug)
    return project if allowed_project(user, project) else None


async def visible(request, conversation_id):
    if not conversation_id:
        return (await conversation_access.current_user(request.app[DB_KEY], identity.requester(request))).get("role") == "owner"
    return (await conversation_access.get_access(request.app[DB_KEY], conversation_id,
                                                identity.requester(request)))["can_read"]


def _visible_rows_tx(conn, actor, rows, kind, *, project_query=None, project_limit=25):
    """Resolve one list's real origins inside the caller's read snapshot."""
    user = conversation_access.user_tx(conn, actor)
    if not user:
        return []
    if kind == "project":
        from .admin_projects import _member_project_metadata
        owner = user.get("role") == "owner"
        if project_query is not None:
            # Re-read the small project group in this snapshot. Filtering a
            # path-matched, capped list would still leak private match behavior.
            like = f"%{project_query}%"
            columns = "slug,name,repo_path,enabled" if owner else "slug,name,enabled"
            predicate = "slug LIKE ? COLLATE NOCASE OR name LIKE ? COLLATE NOCASE"
            parameters = [like, like]
            if owner:
                predicate += " OR repo_path LIKE ? COLLATE NOCASE"
                parameters.append(like)
            rows = [dict(row) for row in conn.execute(
                f"SELECT {columns} FROM projects WHERE {predicate} ORDER BY slug LIMIT ?",
                (*parameters, project_limit))]
        return list(rows) if owner else [_member_project_metadata(row) for row in rows]
    out = []
    for row in rows:
        if kind == "conversation":
            cid = row.get("id")
            if cid and conversation_access.access_tx(conn, cid, actor)["can_read"]:
                out.append(row)
            continue

        cid = row.get("conversation_id")
        chat_id = row.get("chat_id") or row.get("id")
        if kind == "question":
            question = conn.execute(
                "SELECT conversation_id,chat_id FROM expert_questions WHERE id=?",
                (row.get("id"),)).fetchone()
            if (not question or question["conversation_id"] != cid
                    or question["chat_id"] != chat_id):
                continue

        chat = conn.execute(
            "SELECT conversation_id,requested_by FROM chats WHERE id=?",
            (chat_id,)).fetchone() if chat_id else None
        graph_id = row.get("graph_id")
        if graph_id:
            # A graph parent may have no chats row. Its real durable
            # conversation is authoritative; an id prefix never is.
            graph = conn.execute(
                "SELECT conversation_id FROM task_graphs WHERE id=?",
                (graph_id,)).fetchone()
            graph_cid = graph["conversation_id"] if graph else None
            if (graph is not None and not graph_cid and not cid
                    and (chat is None or not chat["conversation_id"])
                    and user.get("role") == "owner"):
                # Historical graph parents can lack a conversation. Only an
                # actual graph row plus the current owner preserves this view.
                out.append(row)
                continue
            if (not graph_cid or (cid and cid != graph_cid)
                    or chat and chat["conversation_id"] != graph_cid):
                continue
            cid = graph_cid
        elif chat:
            if cid and chat["conversation_id"] != cid and kind != "question":
                continue
            if (cid and chat["conversation_id"]
                    and chat["conversation_id"] != cid):
                continue
            cid = cid or chat["conversation_id"]
        elif kind != "question":
            continue

        if cid:
            if conversation_access.access_tx(conn, cid, actor)["can_read"]:
                out.append(row)
        elif chat and (user.get("role") == "owner"
                       or str(chat["requested_by"] or "").lower() == str(actor).lower()):
            # Preserve the current owner/original requester rule for
            # legacy standalone chats, using the stored actor only.
            out.append(row)
    return out



async def visible_rows(request, rows, *, kind="chat"):
    """Filter a whole list under one current account/origin/ACL snapshot."""
    db, actor = request.app[DB_KEY], identity.requester(request)
    return await conversation_access.transaction(
        db, lambda conn: _visible_rows_tx(conn, actor, rows, kind))


async def _visible_groups(request, groups, *, project_query=None, project_limit=25):
    """Filter grouped lists in one snapshot without adding wire metadata."""
    db, actor = request.app[DB_KEY], identity.requester(request)
    return await conversation_access.transaction(db, lambda conn: {
        key: _visible_rows_tx(conn, actor, rows, kind, project_query=project_query,
                              project_limit=project_limit)
        for key, (kind, rows) in groups.items()})


@web.middleware
async def guard(request, handler):
    """Autorizar antes del handler, incluidos prefijos y referencias en el body."""
    db = request.app.get(DB_KEY)
    if db is None or request.path == "/mcp/relay":
        return await handler(request)
    route = request.match_info.route.resource
    path = route.canonical if route else ""
    cids = []
    for key in ("conversation", "conversation_id"):
        if request.query.get(key):
            cids.append(request.query[key])
    if len(set(cids)) > 1:
        raise web.HTTPBadRequest(text="Referencias de conversación incompatibles.")
    if path.startswith(("/conversations/", "/admin/api/conversations/")):
        cids.append(request.match_info.get("id") or request.match_info.get("conv_id"))
    elif path.startswith(("/chats/", "/experts/", "/admin/api/chats/")):
        prefix = request.match_info.get("chat_id") or request.match_info.get("id")
        if prefix:
            rows = await db.run("SELECT DISTINCT conversation_id FROM chats WHERE " +
                ("id=?" if len(prefix) == 36 else "substr(id,1,?)=?"),
                (prefix,) if len(prefix) == 36 else (len(prefix), prefix))
            cids.extend(r["conversation_id"] for r in rows)
            current_user = await conversation_access.current_user(db, identity.requester(request))
            if not rows and current_user.get("role") != "owner":
                raise web.HTTPNotFound(text="Consulta no disponible.")
            if any(not r["conversation_id"] for r in rows):
                chats = await db.run("SELECT id FROM chats WHERE " +
                    ("id=?" if len(prefix) == 36 else "substr(id,1,?)=?"),
                    (prefix,) if len(prefix) == 36 else (len(prefix), prefix))
                for chat in chats:
                    if not await conversation_access.can_read_chat(db, chat["id"], identity.requester(request)):
                        raise web.HTTPNotFound(text="Consulta no disponible.")
    elif path.startswith("/questions/"):
        row = await db.get_expert_question(request.match_info["q_id"])
        cid = (row or {}).get("conversation_id")
        if not cid and (row or {}).get("chat_id"):
            chat = await db.get_chat(row["chat_id"])
            cid = (chat or {}).get("conversation_id")
            if not cid and not await conversation_access.can_read_chat(db, row["chat_id"], identity.requester(request)):
                raise web.HTTPNotFound(text="Consulta no disponible.")
        cids.append(cid)
    elif path.startswith("/graphs/"):
        row = await db.get_task_graph(request.match_info["id"])
        cids.append((row or {}).get("conversation_id"))
    if request.method == "POST" and path in {"/experts/run", "/graphs"}:
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            body = {}
        if isinstance(body, dict):
            references = [body[key] for key in ("conversation", "conversation_id") if body.get(key)]
            if any(not isinstance(cid, str) for cid in references) or len(set(references)) > 1:
                raise web.HTTPBadRequest(text="Referencias de conversación incompatibles.")
            cids.extend(references)
    for cid in set(cids) - {None, ""}:
        permission = "read" if request.method in {"GET", "HEAD"} else "control"
        if request.method == "POST" and path in {"/experts/run", "/experts/steer/{chat_id}"}:
            permission = "send"
        if request.method == "POST" and path == "/graphs":
            permission = "collaborate"
        if "/shares" in path:
            permission = "share"
        if request.method not in {"GET", "HEAD"} and "/workspace" in path:
            permission = "collaborate"
        try:
            if (cid == request.match_info.get("id")
                    and await external_pr_access.authorize_route(db, cid, identity.requester(request), path, request.method)):
                continue
            await conversation_access.require(db, cid, identity.requester(request), permission)
        except PermissionError:
            raise web.HTTPNotFound(text="Consulta no disponible.") from None
        if request.method not in {"GET", "HEAD"} and await db.get_conversation(cid):
            state = await db.get_conversation_task(cid)
            if state.get("mcp_private") and path != "/experts/cancel/{chat_id}":
                raise web.HTTPForbidden(text="La consulta MCP es de solo lectura; inicia otra consulta por MCP.")
    return await handler(request)
