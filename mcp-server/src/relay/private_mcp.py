"""MCP privado de consultas: identidad personal, cola durable y resultados propios."""
from __future__ import annotations

import json
import logging
import uuid

from aiohttp import web
from mcp.types import JSONRPCNotification, JSONRPCRequest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import __version__, config, conversation_access, identity, task_service, task_workspace
from .app_state import DB_KEY, PROGRESS_KEY
from .server_common import _require_auth, _rpc_error
from .private_mcp_access import (allowed_project_tx, authorized_project,
                                 personal_user)
from .secret_redaction import redact_data

logger = logging.getLogger(__name__)
VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class ListProjects(Arguments):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class StartConsultation(Arguments):
    project: str = Field(min_length=1, max_length=128, description="Slug del catálogo autorizado.")
    prompt: str = Field(min_length=1, max_length=16000, description="Consulta de solo lectura sobre ese proyecto.")
    request_id: str = Field(min_length=1, max_length=128, description="ID estable del encargo; reutilizar al reintentar.")


class GetResult(Arguments):
    chat_id: str = Field(pattern=r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=12000, ge=1, le=16000)


TOOLS = {
    "relay_list_projects": (ListProjects, "Lista proyectos habilitados y autorizados, sin rutas ni configuración."),
    "relay_start_consultation": (StartConsultation, "Encola una consulta de solo lectura; devuelve IDs para consultar el resultado. No edita código, publica ni envía mensajes. Reintenta con el mismo request_id y argumentos."),
    "relay_get_result": (GetResult, "Lee estado y resultado durable de un encargo propio por su UUID completo. Usa next_offset para continuar el texto; no reinicia la ejecución."),
}


async def _user(request):
    who = identity.requester(request)
    if who in {"", identity.OWNER, "anonymous"}:
        raise web.HTTPUnauthorized(text="Se requiere una sesión personal de Relay o Cloudflare Access.")
    user = await personal_user(request)
    if (not user.get("enabled") or user.get("archived_at")
            or user.get("role") not in {"owner", "subadmin", "member"}):
        raise web.HTTPForbidden(text="La cuenta no tiene acceso técnico a Relay.")
    return user


async def _start(request, user, args):
    db = request.app[DB_KEY]
    project = await authorized_project(db, user["email"], args.project)
    if project is None:
        raise PermissionError("Proyecto no disponible para tu cuenta.")
    actor = user["email"]
    cid = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(["relay:mcp", actor, args.request_id])))
    async with task_service.control_lock(db, cid):
        user = await conversation_access.current_user(db, actor)
        project = await authorized_project(db, actor, project["slug"])
        if project is None:
            raise PermissionError("Proyecto no disponible para tu cuenta.")
        conv = await db.get_conversation(cid)
        if conv and (conv["requested_by"] != actor or conv["project_slug"] != project["slug"]):
            raise ValueError("request_id ya utilizado con otro proyecto.")
        events = await db.list_conversation_events(cid) if conv else []
        if events:
            event = events[0]
            if event["payload"].get("user") != args.prompt:
                raise ValueError("request_id ya utilizado con otra consulta.")
        else:
            if not conv:
                await db.create_conversation(project_slug=project["slug"], requested_by=actor,
                                             conversation_id=cid, author=actor)
            await db.update_conversation_task(cid, role=user["role"], requested_by=actor,
                publish_allowed=False, tracking={"enabled": False}, mcp_private=True)
            await task_workspace.initialize_task(db, project, cid, read_only=True)
            state = await db.get_conversation_task(cid)
            if state.get("mode") != "read_only" or state.get("state") in task_service.STOPPED:
                raise ValueError("Consulta bloqueada; revisa su estado en Relay.")
            event = await db.enqueue_conversation_event(cid, "request:" + args.request_id, "run", {
                "user": args.prompt, "target": project["slug"], "source": "mcp",
                "author": actor, "requested_by": actor, "role": user["role"],
            }, authorized_actor=actor)
        if event["state"] == "pending":
            task_service.start_pending(request.app, cid)
        chat = await db.get_chat(event["chat_id"])
        return {"chat_id": chat["id"], "conversation_id": cid, "status": chat["status"]}


async def _result(request, user, args):
    db = request.app[DB_KEY]
    actor = user["email"]
    chat_id = str(uuid.UUID(args.chat_id))

    def snapshot(conn):
        # Authorization and content belong to one read snapshot. A revocation
        # committed before this read cannot be bypassed by an earlier await.
        current_user = conversation_access.user_tx(conn, actor)
        row = conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        chat = dict(row) if row else {}
        project_row = conn.execute(
            "SELECT * FROM projects WHERE slug=? COLLATE NOCASE",
            (chat.get("project_slug"),)).fetchone()
        project = db._parse_project(dict(project_row)) if project_row else None
        if (not chat or chat.get("source") != "mcp"
                or chat.get("requested_by") != actor
                or not allowed_project_tx(conn, current_user, project)
                or not conversation_access.access_tx(
                    conn, chat.get("conversation_id"), actor)["can_read"]):
            raise PermissionError("Encargo no disponible para tu cuenta.")
        conversation = conn.execute(
            "SELECT task_json FROM conversations WHERE id=?",
            (chat["conversation_id"],)).fetchone()
        state = json.loads(conversation[0] or "{}") if conversation else {}
        if not isinstance(state, dict) or state.get("mcp_private") is not True:
            raise PermissionError("Encargo no disponible para tu cuenta.")
        row = conn.execute("SELECT payload FROM chat_outputs WHERE chat_id=?", (chat_id,)).fetchone()
        output = json.loads(row[0]) if row else {}
        text = output.get("content") or ""
        end = args.offset + args.limit
        stages = json.loads(chat.get("stages_json") or "{}")
        return {"chat_id": chat["id"], "conversation_id": chat["conversation_id"],
                "project": chat["project_slug"], "status": chat["status"],
                "task_state": state.get("state"), "phase": chat.get("phase_at_end"),
                "content": text[args.offset:end], "next_offset": end if end < len(text) else None,
                "error": chat.get("error") or state.get("error") or None,
                "verifier_verdict": stages.get("verifier_verdict"), "outcome": stages.get("resultado"),
                "tokens_in": chat.get("tokens_in"), "tokens_out": chat.get("tokens_out")}

    result = await conversation_access.transaction(db, snapshot)
    live = request.app[PROGRESS_KEY].get(chat_id)
    if live is not None:
        result["phase"] = live.phase
    return result


def _tool_result(value, *, error=False):
    value = redact_data(value)
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
            "structuredContent": value, "isError": error}


@_require_auth
async def endpoint(request):
    if not config.private_mcp_enabled():
        raise web.HTTPNotFound(text="Interfaz MCP deshabilitada.")
    user = await _user(request)
    if request.method != "POST":
        raise web.HTTPMethodNotAllowed(request.method, ["POST"])
    if request.headers.get("MCP-Protocol-Version", VERSIONS[0]) not in VERSIONS:
        raise web.HTTPBadRequest(text="MCP-Protocol-Version no soportada.")
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="Se requiere application/json.")
    # Un POST contiene un mensaje acotado; no se habilitan sesiones ni streams SSE.
    raw = bytearray()
    async for chunk in request.content.iter_chunked(8192):
        raw.extend(chunk)
        if len(raw) > 131072:
            raise web.HTTPRequestEntityTooLarge(max_size=131072, actual_size=len(raw))
    try:
        rpc = json.loads(raw)
    except (ValueError, UnicodeError):
        return _rpc_error(None, -32700, "JSON inválido.")
    try:
        message = (JSONRPCRequest if isinstance(rpc, dict) and "id" in rpc
                   else JSONRPCNotification).model_validate(rpc, strict=True)
    except ValidationError:
        return _rpc_error(None, -32600, "Mensaje JSON-RPC inválido.")
    if isinstance(message, JSONRPCNotification):
        # Las notificaciones jamás despachan tools ni crean encargos.
        return web.Response(status=202)
    params = message.params or {}
    if message.method == "initialize":
        requested = params.get("protocolVersion")
        result = {"protocolVersion": requested if requested in VERSIONS else VERSIONS[-1],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "relay_private_mcp", "version": __version__}}
    elif message.method == "ping":
        result = {}
    elif message.method == "tools/list":
        result = {"tools": [{"name": name, "description": description,
            "inputSchema": model.model_json_schema(), "annotations": {
                "readOnlyHint": name != "relay_start_consultation", "destructiveHint": False,
                "idempotentHint": True, "openWorldHint": name == "relay_start_consultation"}}
            for name, (model, description) in TOOLS.items()]}
    elif message.method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOLS:
            return _rpc_error(message.id, -32602, "Herramienta desconocida.")
        try:
            args = TOOLS[name][0].model_validate(params.get("arguments", {}))
            if name == "relay_list_projects":
                db = request.app[DB_KEY]
                def project_snapshot(conn):
                    current_user = conversation_access.user_tx(conn, user["email"])
                    projects = []
                    for row in conn.execute("SELECT * FROM projects ORDER BY name"):
                        project = db._parse_project(dict(row))
                        if allowed_project_tx(conn, current_user, project):
                            projects.append({"slug": project["slug"],
                                             "name": project.get("name") or project["slug"]})
                    return projects
                projects = await conversation_access.transaction(db, project_snapshot)
                end = args.offset + args.limit
                value = {"projects": projects[args.offset:end],
                         "next_offset": end if end < len(projects) else None}
            elif name == "relay_start_consultation":
                value = await _start(request, user, args)
            else:
                value = await _result(request, user, args)
            result = _tool_result(value)
        except ValidationError:
            result = _tool_result({"error": "Argumentos inválidos; revisa el esquema de la herramienta."}, error=True)
        except (ValueError, PermissionError) as exc:
            result = _tool_result({"error": str(exc)}, error=True)
        except task_workspace.TaskWorkspaceError:
            result = _tool_result({"error": "Workspace no disponible; revisa la consulta en Relay antes de reintentar."}, error=True)
        except Exception:
            logger.exception("Falló la herramienta MCP %s", name)
            result = _tool_result({"error": "No se pudo confirmar el resultado. Reintenta con el mismo request_id."}, error=True)
    else:
        return _rpc_error(message.id, -32601, "Método no soportado.")
    return web.json_response({"jsonrpc": "2.0", "id": message.id, "result": result},
                             headers={"Cache-Control": "no-store"})
