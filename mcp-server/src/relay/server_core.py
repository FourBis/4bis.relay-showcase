"""Server domain handlers extracted from the composition entrypoint."""
from __future__ import annotations

import json
import time

from aiohttp import web

from .server_common import (
    GRAFOS_KEY, PROGRESS_KEY, RELAY_VERSION, RUNNING_KEY, SESSIONS_KEY,
    SessionRegistry, _require_auth, _rpc_error, logger, tool_registry, validate_name,
    validate_sid,
)
from . import progress
# ---------- core: health + push ----------


@_require_auth
async def health(request: web.Request) -> web.Response:
    return web.json_response(
        {"ok": True, "service": "relay", "version": RELAY_VERSION})


@_require_auth
async def system_active(request: web.Request) -> web.Response:
    """GET /system/active — resumen breve de los runs en curso (Iter 11).

    Lo consume el indicador de bandeja (tray.ps1) cada ~2s para pintar
    verde/amarillo/rojo. Es un sondeo barato: NO abre el .md, NO toca la
    base y descarta `steps` del snapshot, que con STEPS_KEEP=30 y los
    diffs de edit_file adentro puede pesar decenas de KB — inaceptable
    para algo que se pide cada dos segundos. Quien necesite los pasos
    tiene /experts/status/{chat_id}.

    Devuelve:
      - `active`: bool — hay al menos un run vivo
      - `count`: int — cantidad de runs vivos
      - `first`: dict|None — snapshot del run MÁS ANTIGUO (chat_id,
        target, phase, last_tool, elapsed_s, idle_s, model), o None.
        `idle_s` viaja ahí adentro para que el indicador detecte "el
        experto lleva N segundos sin avanzar" sin tener que consultar
        /experts/status/{chat_id} por separado.
    """
    running: dict = request.app[RUNNING_KEY]
    progress: dict = request.app[PROGRESS_KEY]
    graphs = request.app.get(GRAFOS_KEY, {})
    now = time.monotonic()
    items: list[dict] = []
    for chat_id, rp in progress.items():
        graph_task = graphs.get(rp.graph_id)
        graph_node = (chat_id != rp.graph_id and graph_task is not None
                      and not graph_task.done())
        if rp.finished or (chat_id not in running and not graph_node):
            continue
        snap = rp.snapshot(now=now)
        snap.pop("steps", None)
        snap["chat_id"] = chat_id
        items.append(snap)
    # Entre nodos (o durante la verificación final) el grafo sigue vivo.
    # No sumar también el padre de un nodo vivo. RUNNING_KEY conserva
    # su contrato de cancelación de expertos independientes.
    represented = {s.get("graph_id") for s in items}
    for graph_id, task in graphs.items():
        if graph_id in represented or task.done():
            continue
        rp = progress.get(graph_id)
        if rp is not None and not rp.finished:
            snap = rp.snapshot(now=now)
            snap.pop("steps", None)
            items.append(snap)
    # Mayor elapsed_s primero, o sea el run más antiguo. El indicador
    # muestra ese; si hay varios, `count` avisa del resto.
    items.sort(key=lambda s: s.get("elapsed_s", 0), reverse=True)
    first = items[0] if items else None
    return web.json_response({
        "active": bool(items),
        "count": len(items),
        "first": first,
        "version": RELAY_VERSION,
    })


@_require_auth
async def list_sessions_view(request: web.Request) -> web.Response:
    """GET /sessions — lista las sesiones VS Code registradas.

    Devuelve `sessions` (snapshot serializable) y `available_targets`
    (lista única y ordenada de last_target != None), para que el bot
    Discord pueda armar un menú de targets sin pegarle a /prompts y
    esperar un 404.
    """
    sessions: SessionRegistry = request.app[SESSIONS_KEY]
    items = await sessions.list_sessions()
    targets = sorted({i["last_target"] for i in items if i["last_target"]})
    return web.json_response({"sessions": items, "available_targets": targets})


# ---------- handshake + SSE (extensión VS Code) ----------


@_require_auth
async def handshake(request: web.Request) -> web.Response:
    """POST /agents/handshake — la extensión se identifica.

    Body:
        name: str                         (requerido)
        sessionId: str                    (requerido, vscode.env.sessionId)
        machineId: str                    (requerido)
        vscodeVersion: str                (opcional, loggeado)
        workspaceFolders: [{path, name}]  (opcional; routing target)
        ts: str                           (opcional, ISO 8601)

    Returns 200 OK siempre (best-effort). Si el body es inválido → 400.
    """
    sessions: SessionRegistry = request.app[SESSIONS_KEY]
    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)

    try:
        sid = validate_sid(body.get("sessionId", ""))
        name = validate_name(body.get("name", ""))
        machine_id = body.get("machineId")
        if not isinstance(machine_id, str) or not machine_id:
            raise ValueError("machineId requerido")
        ts = body.get("ts", "")
        if not isinstance(ts, str):
            ts = ""
        ws_folders = body.get("workspaceFolders", [])
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    sess = await sessions.upsert_from_handshake(
        session_id=sid,
        name=name,
        machine_id=machine_id,
        workspace_folders=ws_folders,
        ts=ts,
    )
    logger.info(
        "handshake sid=%s name=%s machineId=%s target=%s",
        sid[:8], name, machine_id[:8], sess.last_target,
    )
    return web.json_response({"ok": True}, status=200)


# ---------- MCP (Google tools — secundario) ----------


@_require_auth
async def mcp_endpoint(request: web.Request) -> web.Response:
    """Mini MCP JSON-RPC 2.0 hacia /mcp. Solo tools de Google en Iter 1.

    Métodos soportados: initialize, tools/list, tools/call, ping.
    """
    try:
        rpc: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response(
            {"jsonrpc": "2.0", "error": {"code": -32700, "message": "parse error"}},
            status=400,
        )

    req_id = rpc.get("id")
    method = rpc.get("method")
    params = rpc.get("params", {})

    if rpc.get("jsonrpc") != "2.0":
        return web.json_response(
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32600, "message": "invalid request"}},
            status=200,
        )

    if method == "initialize":
        result = {
            "protocolVersion": "2025-03-26",
            "serverInfo": {"name": "relay", "version": RELAY_VERSION},
            "capabilities": {"tools": {"listChanged": False}},
        }
    elif method == "tools/list":
        result = {"tools": tool_registry.all_schemas()}
    elif method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        hit = tool_registry.get(tool_name)
        if hit is None:
            return _rpc_error(req_id, -32602, f"tool desconocida: {tool_name}")
        tool_cls, _ = hit
        tool = tool_cls()
        try:
            out = await tool.call(arguments)
        except Exception as e:
            return _rpc_error(req_id, -32000, f"tool error: {e!r}")
        return web.json_response({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": json.dumps(out)}]},
        })
    elif method == "ping":
        result = {}
    else:
        return _rpc_error(req_id, -32601, f"método no soportado: {method}")

    return web.json_response({"jsonrpc": "2.0", "id": req_id, "result": result})


def _rpc_error(req_id, code, message):
    return web.json_response(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
        status=200,
    )
