"""Server domain handlers extracted from the composition entrypoint."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp import web

from .server_common import (
    COMMANDS_KEY, DB_KEY, PROGRESS_KEY, RUNNING_KEY, SESSIONS_KEY, SessionRegistry,
    _require_auth,
)
from . import progress
from .commands import CommandContext, CommandRegistry, UnknownCommand
from .db import Database
# ---------- projects (config CRUD, ADR-013) ----------


@_require_auth
async def projects_list(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    enabled_only = request.query.get("all", "") != "1"
    return web.json_response({"projects": await db.list_projects(enabled_only)})


@_require_auth
async def projects_get(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    project = await db.get_project(request.match_info["slug"])
    if project is None:
        return web.json_response({"error": "no existe"}, status=404)
    return web.json_response(project)


@_require_auth
async def projects_upsert(request: web.Request) -> web.Response:
    """POST /projects (o PUT /projects/{slug}) — crea o actualiza."""
    db: Database = request.app[DB_KEY]
    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    slug = request.match_info.get("slug") or body.get("slug")
    if not slug:
        return web.json_response({"error": "slug requerido"}, status=400)
    body["slug"] = slug
    if "repo_path" in body and not isinstance(body["repo_path"], str):
        return web.json_response({"error": "repo_path debe ser string"}, status=400)
    existing = await db.get_project(slug)
    if existing is None and not body.get("repo_path"):
        return web.json_response({"error": "repo_path requerido para proyecto nuevo"}, status=400)
    try:
        project = await db.upsert_project(body)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response(project, status=200 if existing else 201)


@_require_auth
async def projects_delete(request: web.Request) -> web.Response:
    """DELETE /projects/{slug} — soft delete (enabled=0)."""
    db: Database = request.app[DB_KEY]
    ok = await db.disable_project(request.match_info["slug"])
    if not ok:
        return web.json_response({"error": "no existe"}, status=404)
    return web.json_response({"ok": True})


@_require_auth
async def project_set_discord_channel(request: web.Request) -> web.Response:
    """PATCH /projects/{slug}/discord-channel — setea/limpia el canal
    Discord default del proyecto (Iter 10.1).

    Body:
        discord_channel_id:  str | null
            - string no vacío: upsert al id del canal
            - null (explícito): clear (set NULL en la fila)
            - string vacío: 400 (ambigüedad: ¿se olvidó o quiso limpiar?)

    Returns:
        200 -> {slug, discord_channel_id} (o cleared=true si null)
        400 -> string vacío o tipo inválido
        404 -> proyecto desconocido
    """
    db: Database = request.app[DB_KEY]
    slug = request.match_info["slug"]
    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    raw = body.get("discord_channel_id")
    # Iter 10.1: signal de clear. Ausencia o null explícito → clear.
    # Cualquier string vacío o no-string → 400 (mirror del endpoint
    # de conversaciones, para que el cliente no se confunda entre
    # "olvidé" y "quiero limpiar").
    if raw is None:
        existing = await db.get_project(slug)
        if existing is None:
            return web.json_response(
                {"error": f"proyecto {slug!r} desconocido"}, status=404)
        await db.set_project_discord_channel(slug, channel_id=None, clear=True)
        return web.json_response({
            "slug": slug,
            "discord_channel_id": None,
            "cleared": True,
        })
    if not isinstance(raw, str):
        return web.json_response(
            {"error": "discord_channel_id debe ser string o null"},
            status=400)
    if not raw.strip():
        return web.json_response(
            {"error": "discord_channel_id string vacío (manda null "
                      "explícito para limpiar)"},
            status=400)
    existing = await db.get_project(slug)
    if existing is None:
        return web.json_response(
            {"error": f"proyecto {slug!r} desconocido"}, status=404)
    await db.set_project_discord_channel(slug, channel_id=raw.strip())
    return web.json_response({"slug": slug, "discord_channel_id": raw})


# ---------- commands (ADR-013) ----------


@_require_auth
async def commands_list(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    enabled_only = request.query.get("all", "") != "1"
    return web.json_response({"commands": await db.list_commands(enabled_only)})


@_require_auth
async def commands_upsert(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    registry: CommandRegistry = request.app[COMMANDS_KEY]
    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    name = request.match_info.get("name") or body.get("name")
    if not name:
        return web.json_response({"error": "name requerido"}, status=400)
    body["name"] = name
    existing = await db.get_command(name)
    if existing is None and not (body.get("handler") and body.get("description")):
        return web.json_response(
            {"error": "handler y description requeridos para comando nuevo"}, status=400)
    try:
        cmd = await db.upsert_command(body)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    await registry.load_from_db()
    return web.json_response(cmd, status=200 if existing else 201)


@_require_auth
async def commands_delete(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    registry: CommandRegistry = request.app[COMMANDS_KEY]
    ok = await db.disable_command(request.match_info["name"])
    if not ok:
        return web.json_response({"error": "no existe"}, status=404)
    await registry.load_from_db()
    return web.json_response({"ok": True})


@_require_auth
async def commands_run(request: web.Request) -> web.Response:
    """POST /commands/{name}/run — ejecuta un comando dinámico.

    Body: {"args": {...}, "author": "...", "source": "discord"}
    Returns: {"text": <respuesta para publicar>}
    """
    db: Database = request.app[DB_KEY]
    sessions: SessionRegistry = request.app[SESSIONS_KEY]
    registry: CommandRegistry = request.app[COMMANDS_KEY]
    running: dict = request.app[RUNNING_KEY]
    name = request.match_info["name"]
    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    args = body.get("args") or {}
    if not isinstance(args, dict):
        return web.json_response({"error": "args debe ser objeto"}, status=400)
    ctx = CommandContext(
        db=db, sessions=sessions, running=running,
        source=body.get("source") or "api", author=body.get("author") or "",
    )
    try:
        text = await registry.dispatch(name, args, ctx)
    except UnknownCommand:
        return web.json_response(
            {"error": f"comando {name!r} desconocido",
             "available_commands": registry.names()},
            status=404,
        )
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except RuntimeError as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"text": text})


# ---------- chats (read-only, índice) ----------


@_require_auth
async def chats_list(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    project = request.query.get("project") or None
    status = request.query.get("status") or None
    limit = min(int(request.query.get("limit", "20")), 200)
    return web.json_response(
        {"chats": await db.list_chats(
            project_slug=project, limit=limit, status=status)})


@_require_auth
async def chats_get(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    chat = await db.get_chat(request.match_info["id"])
    if chat is None:
        return web.json_response({"error": "no existe"}, status=404)
    # `stages_json` se guarda como TEXT pero se sirve como objeto: el
    # consumidor quiere chat["stages"]["verifier_verdict"], no volver a
    # parsear. Se deja también el TEXT crudo por compatibilidad.
    raw = chat.get("stages_json")
    if raw:
        try:
            chat["stages"] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            chat["stages"] = None  # fila vieja o corrupta: no rompe el GET
    return web.json_response(chat)


@_require_auth
async def chats_get_md(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    chat = await db.get_chat(request.match_info["id"])
    if chat is None or not chat.get("md_path"):
        return web.json_response({"error": "no existe o sin .md"}, status=404)
    try:
        text = await asyncio.to_thread(
            Path(chat["md_path"]).read_text, encoding="utf-8")
    except OSError:
        return web.json_response({"error": ".md no legible"}, status=404)
    return web.Response(text=text, content_type="text/markdown", charset="utf-8")


@_require_auth
async def chats_get_status(request: web.Request) -> web.Response:
    """GET /chats/{id}/status — fila de chat + RunProgress vivo.

    Para la UI "En curso". Devuelve:
      - la fila de chats (id, status, started_at, project, author, source,
        tokens, tool_calls, error, phase_at_end, last_tool)
      - si hay un RunProgress vivo (progress store), el snapshot actual
      - has_progress: bool para que el front sepa si mostrar "vivo"
        o "post-mortem"

    Si el chat no existe → 404.
    Si el chat existe pero NO hay progress store (relay reinició
    mientras corría, o ya terminó y se limpió) → 200 con
    `progress: null` y `has_progress: false`.
    """
    db: Database = request.app[DB_KEY]
    progress: dict = request.app[PROGRESS_KEY]
    chat_id = request.match_info["id"]
    chat = await db.get_chat(chat_id)
    if chat is None:
        return web.json_response({"error": "no existe"}, status=404)
    # Soporte de prefijo, igual que /experts/status y /experts/cancel
    matches = [cid for cid in progress if cid.startswith(chat_id)]
    snap = None
    if len(matches) == 1:
        snap = progress[matches[0]].snapshot()
    elif len(matches) > 1:
        snap = [progress[c].snapshot() for c in matches]
    return web.json_response({
        "chat": chat,
        "progress": snap,
        "has_progress": bool(snap),
        "is_running": bool(chat.get("status") == "running"),
    })


@_require_auth
async def stats_view(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    sessions: SessionRegistry = request.app[SESSIONS_KEY]
    running: dict = request.app[RUNNING_KEY]
    stats = await db.stats()
    stats["vscode_sessions"] = len(await sessions.list_sessions())
    stats["experts_running"] = len(running)
    return web.json_response(stats)
