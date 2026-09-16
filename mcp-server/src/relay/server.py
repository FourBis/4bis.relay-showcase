"""aiohttp app: expertos async + conversaciones + Admin API.

El relay es UN proceso que escucha en :8413 y hace de hub entre el bot
de Discord, los repos y el LLM. El LLM vive acá (ADR-012): no se
delega a la extensión de VS Code.

El flujo principal, de punta a punta:

    bot C# ──POST /experts/run──▶ relay ──202 {id}──▶ bot
                                   │
                                   │  corre en background: planificador
                                   │  → ejecutor → verificador →
                                   │  documentador (experts.py)
                                   │
                                   └──POST /notify──▶ bot C# (:8297)

Referencia rápida por uso real:

| Endpoint                        | Quién lo usa         | Estado          |
| ------------------------------- | -------------------- | --------------- |
| `POST /experts/run` (+ /status) | bot C# / admin / CLI | flujo principal |
| `POST /experts/cancel|steer`    | admin / bot          | en uso          |
| `POST /conversations` (+ close) | bot C# / admin       | en uso (ADR-025) |
| `POST /agents/handshake`        | extensión VS Code    | en uso (liveness) |
| `GET /sessions`                 | bot C# / admin       | en uso (targets vivos) |
| `POST /notify` (saliente)       | — el relay lo llama  | salida de los expertos |
| `/admin/api/*`                  | Admin UI             | ver admin.py    |

**El push por SSE ya no existe** (iter 10.10, 2026-08-10): `GET /events`,
`POST /prompts`, `POST /prompts/{id}/response` y `GET /prompts/{id}` se
eliminaron —700 líneas y 18 tests— cuando quedó claro que el bot ya no
los usaba y la extensión solo handshakea para liveness. Si buscás esa
maquinaria por un doc viejo: no está, y el reemplazo es `/experts/run`
async. Contratos vigentes en docs/API.md.

Middlewares, en orden: origen/Host del navegador → guard de localhost (chequeo de peer,
barato y sin red) → identidad (ADR-037, verifica el JWT de Cloudflare
Access) → roles (owner/member, deny by default, necesita la identidad
ya resuelta). Ver identity.py.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import ipaddress
import json
import logging
import math
import os
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import httpx
from aiohttp import web

from . import __version__ as RELAY_VERSION
from . import bot_control
from . import coordination, finalization
from . import experts, git_flow, identity, logctx, memory, persist, voice
from . import grafo as grafo_mod
from . import tracing
from . import github as github_mod
from . import attachments as attachments_mod
from .commands import CommandContext, CommandRegistry, UnknownCommand
from .db import Database, read_system_config_sync
from . import mcp_pool
from .mcp_pool import McpPool
from .mcp_installer import McpInstaller
from . import config as relay_config
from .notify import NotifyClient, default_bot_url
from .sessions import (
    SessionRegistry,
    validate_name,
    validate_sid,
)
from .skills import SkillBrowser, SkillCache
from .tools import _registry as tool_registry

logger = logging.getLogger("relay.server")
_log_utf8_configured = False  # flag para que create_app sea idempotente

# Esta función se llama en cada request para aplicar cambios del panel.
def _get_api_key() -> str:
    return relay_config.get("RELAY_API_KEY").strip()

# AppKeys — discoverable, evitan NotAppKeyWarning.
SESSIONS_KEY: web.AppKey[SessionRegistry] = web.AppKey("sessions", SessionRegistry)
NOTIFY_KEY: web.AppKey[NotifyClient] = web.AppKey("notify", NotifyClient)
SKILLS_KEY: web.AppKey[SkillCache] = web.AppKey("skills", SkillCache)
DB_KEY: web.AppKey[Database] = web.AppKey("db", Database)
COMMANDS_KEY: web.AppKey[CommandRegistry] = web.AppKey("commands", CommandRegistry)
RUNNING_KEY: web.AppKey[dict] = web.AppKey("running", dict)
# Referencias a las tasks de `_run_expert_bg` que siguen vivas. NO es lo
# mismo que RUNNING_KEY: ese dict lo vacía `_run_expert_bg` en su
# `finally`, y después la función todavía persiste sugerencias y manda el
# notify. Para saber cuándo la task terminó DE VERDAD hace falta esto,
# que solo se limpia por done_callback. Además evita que el GC se lleve
# una task cuya única referencia era la del dict ya vaciado.
BG_TASKS_KEY: web.AppKey[set] = web.AppKey("bg_tasks", set)
#: graph_id -> asyncio.Task del grafo que lo está corriendo. Separado de
#: BG_TASKS_KEY (que es solo para que el apagado las espere) porque
#: /graphs/{id}/cancel necesita encontrar UNA por id.
GRAFOS_KEY: web.AppKey[dict] = web.AppKey("grafos", dict)
PROGRESS_KEY: web.AppKey[dict] = web.AppKey("progress", dict)
BIND_HOST_KEY: web.AppKey[str] = web.AppKey("bind_host", str)
SWEEPER_KEY: web.AppKey[asyncio.Task] = web.AppKey("sweeper", asyncio.Task)
EXPORT_RETRY_KEY: web.AppKey[asyncio.Task] = web.AppKey(
    "export_retry", asyncio.Task)
# Opción A (2026-07-14): watcher de FS que dispara reindex incremental
# de cbm (el auto_watch de cbm nunca corre — CLI one-shot).
# 2026-09-02: ADR-017 revertido — ver docs/DECISIONS.md. cbm SÍ corre
# residente ahora (CBM_WARMUP_KEY); este watcher sigue haciendo falta
# porque el residente tiene cwd neutro y no vigila ningún repo.
CBM_WATCHER_KEY: web.AppKey[asyncio.Task] = web.AppKey("cbm_watcher", asyncio.Task)
# Fase 5 (2026-09-02): task que calienta la sesión MCP residente de cbm
# al boot — ver `_warm_cbm_session`.
CBM_WARMUP_KEY: web.AppKey[asyncio.Task] = web.AppKey("cbm_warmup", asyncio.Task)
# State dir de night runs (Iter 5.4): usado por el detail endpoint
# para leer state/night-runs/<run_id>/plan.md. Si no se setea, fallback
# a "./state" en el endpoint (no lo descubre del cwd solo).
STATE_DIR_KEY: web.AppKey[Path] = web.AppKey("state_dir", Path)
# ADR-028: night runs vivos — {run_id: (NightOrchestrator, asyncio.Task)}
NIGHT_KEY: web.AppKey[dict] = web.AppKey("night", dict)
# F1 (plan MCP_REGISTRY): pool de MCPs on-demand + su reaper.
MCP_POOL_KEY: web.AppKey[McpPool] = web.AppKey("mcp_pool", McpPool)
MCP_REAPER_KEY: web.AppKey[asyncio.Task] = web.AppKey("mcp_reaper", asyncio.Task)
# Iter 9.11: probe async de health al boot para MCPs stdio externos.
# Background task — no bloquea el startup si una probe se cuelga.
MCP_HEALTH_PROBE_KEY: web.AppKey[asyncio.Task] = web.AppKey(
    "mcp_health_probe", asyncio.Task)
# F2 (plan MCP_REGISTRY): installer de MCPs desde GitHub (jobs transient).
MCP_INSTALLER_KEY: web.AppKey[McpInstaller] = web.AppKey("mcp_installer", McpInstaller)
# Alta de skills desde GitHub (2026-07-25): mismo patrón que el installer
# de MCPs pero sin ejecutar nada — clona, lista SKILL.md, el humano elige.
SKILL_BROWSER_KEY: web.AppKey[SkillBrowser] = web.AppKey(
    "skill_browser", SkillBrowser)

# Tunables.
# (push SSE eliminado 2026-08-10; ya no hay PUSH_DEADLINE_S ni keepalive)


def _check_auth(request: web.Request) -> bool:
    api_key = _get_api_key()
    if not api_key:
        return True
    return request.headers.get("X-Relay-Key", "") == api_key


def _require_auth(handler):
    async def wrapper(request: web.Request) -> web.StreamResponse:
        if not _check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)
    wrapper.__name__ = handler.__name__
    return wrapper


# ---------- guard localhost (system_config: RELAY_HOST) ----------

# Rutas que SÍ se sirven a la LAN cuando el bind es 0.0.0.0: bot C#,
# extensión VS Code, liveness probes. Todo lo demás (admin, expertos,
# projects, commands, voice, night-mode, chats, mcp, stats, discord)
# queda forzado a localhost — la superficie de relay escribe FS y
# spawnea procesos, exponerla a la LAN con auth apagada es regalar la
# máquina.
# Bug fix 2026-07-18: antes el guard era una blacklist por prefijo
# (/admin, /api) y dejaba /experts/run, /projects, /commands/*/run y
# /discord/day-answer abiertos si bind=0.0.0.0 y RELAY_API_KEY="".
LAN_SAFE_EXACT = frozenset((
    "/health",            # GET, liveness probe
    "/agents/handshake",  # POST, la extensión se identifica
    "/sessions",          # GET, polling de targets disponibles
))
# Sub-paths que también son LAN-safe (match por startswith). Acá no
# entran prefijos genéricos como "/admin" — al revés, esa ruta vive
# abajo en LAN_BLOCKED.
LAN_SAFE_PREFIXES = ()
_LOCALHOST_PEERS = frozenset({"127.0.0.1", "::1"})


def _loopback_peer(peer: str | None) -> bool:
    try:
        address = ipaddress.ip_address(peer or "")
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or (mapped is not None and mapped.is_loopback)


@web.middleware
async def browser_guard(request: web.Request, handler):
    """El socket local no autentica al sitio web que lo está usando.

    Bloquea DNS rebinding y CSRF antes de resolver OWNER. Un host público
    requiere un JWT que access_identity verificará; no basta con enviarlo.
    """
    denied = web.HTTPForbidden(text='{"error":"untrusted request origin"}',
                               content_type="application/json")
    try:
        host = urlsplit("//" + request.host)
        if (not host.hostname or host.username or host.password or host.path
                or host.query or host.fragment):
            raise denied
        host.port  # valida también el puerto de una autoridad malformada
        access_token = request.headers.get("Cf-Access-Jwt-Assertion", "").strip()
        if (_loopback_peer(request.remote)
                and host.hostname not in _LOCALHOST_PEERS | {"localhost"}
                and not access_token):
            raise denied
        origin = request.headers.get("Origin")
        if origin is not None:
            parsed = urlsplit(origin)
            schemes = {"https", "http"} if access_token else {request.scheme}
            if (parsed.scheme not in schemes or parsed.netloc.lower() != host.netloc.lower()
                    or parsed.path or parsed.query or parsed.fragment):
                raise denied
        # Abrir la UI desde un enlace es seguro; una web ajena no puede
        # usar esa excepción para llamar APIs, formularios o subrecursos.
        ui_navigation = (request.method == "GET" and request.path in {"/admin", "/admin/"}
                         and request.headers.get("Sec-Fetch-Mode") == "navigate")
        if request.headers.get("Sec-Fetch-Site") == "cross-site" and not ui_navigation:
            raise denied
    except ValueError:
        raise denied from None
    return await handler(request)


def _is_lan_safe(path: str) -> bool:
    if path in LAN_SAFE_EXACT:
        return True
    return any(path.startswith(p) for p in LAN_SAFE_PREFIXES)


@web.middleware
async def localhost_guard(request: web.Request, handler):
    """403 cuando el peer no es localhost y la ruta NO es LAN-safe.

    Siempre decide por el peer real, incluso con un bind IPv6 o una IP
    específica. Los headers de proxy no pueden convertir la LAN en owner.
    """
    peer = request.remote or ""
    if _loopback_peer(peer):
        return await handler(request)
    if _is_lan_safe(request.path):
        return await handler(request)
    return web.json_response(
        {"error": "forbidden",
         "message": "Esta ruta solo se sirve a localhost. "
                    f"Peer detectado: {peer or 'desconocido'}. "
                    "Usa la máquina donde corre el relay."},
        status=403,
    )


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


# ---------- expertos (ADR-012 + ADR-024: async, el resultado va por /notify) ----------

# Tasks fire-and-forget (compactación, sweeper spawns): guardamos la
# referencia para que el GC no las mate a mitad de camino.
_bg_tasks: set[asyncio.Task] = set()


def _spawn_bg(coro, *, hold_workspace: bool = True) -> asyncio.Task:
    task = asyncio.create_task(coro)
    if hold_workspace:
        coordination.hold_current(task)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


# Nota (2026-08-10): acá vivía `_maybe_plan_large_prompt` (Opción 4), un
# segundo planificador que detectaba prompts grandes con un regex y
# pagaba una corrida entera de night.TaskGenerator para proponer una
# descomposición. Quedó absorbido por el planificador del runner por
# etapas (experts._run_planner + la señal `DEMASIADO_GRANDE:`), que ya
# corre en cada request: el mismo comportamiento sin el LLM extra ni el
# acoplamiento del chat con la maquinaria del modo nocturno.


async def _request_bot_create_thread(
    *, discord_user_id: str, conversation_id: str, project_slug: str,
) -> tuple[Optional[str], Optional[str]]:
    """Iter 10.4: pide al bot C# crear un DM thread para la conversación.

    Llama POST /threads en el bot. Devuelve (thread_id, None) si ok,
    o (None, mensaje_error) si falla — nunca crashea el handler caller.
    """
    bot_notify_url = os.environ.get(
        "BOT_NOTIFY_URL", "http://127.0.0.1:8297/notify")
    base = bot_notify_url.rstrip("/")
    if base.endswith("/notify"):
        base = base[:-len("/notify")]
    url = f"{base}/threads"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, json={
                "discord_user_id": discord_user_id,
                "conversation_id": conversation_id,
                "project_slug": project_slug,
            })
            r.raise_for_status()
            data = r.json()
            tid = data.get("discord_thread_id") or data.get("channel_id") or ""
            if not tid:
                return None, f"bot respondió sin discord_thread_id: {data}"
            return str(tid), None
    except httpx.HTTPError as e:
        logger.warning(
            "bot create-thread falló conv=%s discord_user=%s err=%r",
            conversation_id, discord_user_id, e)
        return None, f"no se pudo crear thread: {e}"
    except Exception as e:
        logger.warning(
            "bot create-thread excepción inesperada conv=%s err=%r",
            conversation_id, e)
        return None, f"error inesperado al crear thread: {e}"


def _render_pregunta(row: dict) -> str:
    """Fila de `expert_questions` → bloque markdown para el humano.

    Se pinta como una pregunta con opciones numeradas para que se pueda
    contestar de la forma más barata posible: escribiendo en el chat. El
    `id` va visible porque es lo que necesita quien quiera responder por
    API en vez de por texto.
    """
    try:
        q = json.loads(row.get("question_json") or "{}")
    except json.JSONDecodeError:
        q = {}
    titulo = (q.get("title") or "").strip() or "El experto necesita una decisión"
    icono = "📦" if row.get("kind") == "install" else "❓"
    lineas = [f"{icono} **{titulo}**"]
    detalle = (q.get("detail") or "").strip()
    if detalle:
        lineas += ["", detalle]
    opciones = q.get("options") or []
    if opciones:
        lineas += [""] + [f"- **{o.get('label', '')}**" for o in opciones]
        lineas += ["", "_Respondé en el chat con la opción que prefieras._"]
    else:
        lineas += ["", "_Respondé en el chat y el experto retoma desde ahí._"]
    lineas += ["", f"`{row.get('id', '')}`"]
    return "\n".join(lineas)


#: Qué pasó con el TRABAJO, que no es lo mismo que qué pasó con la
#: ejecución. `chats.status` ya dice si el run terminó o reventó; esto
#: dice si lo que salió sirve. Contar "éxito" como "runs sin error"
#: mezclaba las dos cosas: un run que termina prolijo y que el
#: verificador marcó `needs_more` contaba igual que uno aprobado.
#:
#: `sin_verificar` es su propia categoría y no un `aprobado` optimista:
#: separar "el trabajo estaba mal" de "el verificador no corrió" es
#: justo lo que permite saber si una degradación del supervisor está
#: inflando las métricas.
_RESULTADO_POR_VERDICT = {
    "complete": "aprobado",
    "needs_more": "pendiente",
    "needs_human": "intervencion",
    "off_plan": "desviado",
}


def _resultado_del_trabajo(result: dict) -> str:
    """Veredicto del verificador → vocabulario de resultado."""
    if (result.get("stage_errors") or {}).get("verifier"):
        return "sin_verificar"
    verdict = (result.get("verifier_verdict") or "").strip()
    if not verdict:
        # Sin veredicto y sin error: la etapa no corrió (opt-out del
        # proyecto, o cortó antes de llegar).
        return "sin_verificar"
    return _RESULTADO_POR_VERDICT.get(verdict, "sin_verificar")


def _stages_json(result: dict) -> Optional[str]:
    """Etapas del runner → JSON para `chats.stages_json`, o None.

    Devuelve None cuando el run NO pasó por el runner por etapas
    (`run_expert` directo, opt-out del proyecto): así la columna queda
    NULL en vez de con un objeto de campos vacíos, y "no hubo etapas"
    se distingue de "hubo etapas y salieron vacías".

    El plan se recorta: es texto del planificador y no tiene por qué
    entrar entero en una fila de la DB — el valor de auditoría está en
    el veredicto y en qué modelo corrió cada etapa.
    """
    if not result.get("three_stage"):
        return None
    usage = result.get("stage_usage") or {}
    out = {
        "plan": (result.get("plan") or "")[:4000],
        "planner_model": result.get("planner_model") or "",
        "verifier_verdict": result.get("verifier_verdict") or "",
        "verifier_feedback": result.get("verifier_feedback") or "",
        "verifier_model": result.get("verifier_model") or "",
        "documenter_model": result.get("documenter_model") or "",
        "executor_model": result.get("model") or "",
        # Qué pasó con el trabajo. Ver `_resultado_del_trabajo`: no es
        # `chats.status`, que habla de la ejecución.
        "resultado": _resultado_del_trabajo(result),
    }
    # Cuántas pasadas necesitó el turno. El runner lo calcula y hasta hoy
    # lo tiraba: sin esto, "resuelto en una pasada" y "resuelto después
    # de tres rondas de needs_more" quedaban idénticos en la fila, y era
    # imposible medir si un cambio de prompt o de modelo mejoraba algo
    # sin volver a parsear los logs a mano.
    #
    # Plano el conteo (SQL lo alcanza), anidado el detalle (para leer un
    # caso puntual). El feedback se recorta como el resto del texto libre.
    rondas = result.get("verifier_rounds") or []
    if rondas:
        out["rondas"] = len(rondas)
        out["rondas_detalle"] = [
            {"ronda": r.get("ronda"), "verdict": r.get("verdict") or "",
             "phase_at_end": r.get("phase_at_end") or "",
             "tool_calls": r.get("tool_calls"),
             "feedback": (r.get("feedback") or "")[:200]}
            for r in rondas]
    # Veredictos de media corrida, uno por borde de tanda. Un corte por
    # desvío se veía en el mensaje al humano y no quedaba en ningún lado
    # para revisar después POR QUÉ el ejecutor se fue del plan.
    medios = result.get("mid_verdicts") or []
    if medios:
        out["mid_verdicts"] = [
            {"leg": m.get("leg"), "verdict": m.get("verdict") or "",
             "feedback": (m.get("feedback") or "")[:200],
             "error": str(m.get("error") or "")[:200]}
            for m in medios]
    # Tokens por etapa, planos para que `json_extract` los alcance desde
    # SQL sin recorrer objetos anidados. Una etapa que no reportó usage
    # NO escribe sus claves: ausente = "no se midió", que no es cero.
    for stage in ("planner", "verifier", "documenter"):
        u = usage.get(stage) or {}
        if u:
            out[f"{stage}_tokens_in"] = u.get("tokens_in", 0)
            out[f"{stage}_tokens_out"] = u.get("tokens_out", 0)
    # Etapas caídas (2026-08-15). Misma regla que los tokens: la clave
    # solo existe si hubo error, así que `IS NOT NULL` en SQL alcanza para
    # contar degradaciones. Sin esto, un planificador que corta por
    # timeout dejaba al ejecutor corriendo sin plan y el único rastro era
    # un WARNING en el log — el run se veía idéntico a uno planificado.
    for stage, err in (result.get("stage_errors") or {}).items():
        if err:
            out[f"{stage}_error"] = str(err)[:200]
    # Etapa B, P3: pasos que el ejecutor marcó con `plan_step_done`.
    # Se guarda como dict {paso_str: nota}, mismo criterio que el resto
    # de `stages_json`: ausente = nadie marcó nada, presente = al menos
    # uno. Si la key está vacía la omitimos para no ensuciar el JSON.
    ps = result.get("plan_steps_done")
    if isinstance(ps, dict) and ps:
        out["plan_steps_done"] = {str(k): str(v)[:200]
                                  for k, v in ps.items()}
    return json.dumps(out, ensure_ascii=False)


@finalization.supervise
async def _run_expert_bg(
    *, db: Database, notify: NotifyClient, running: dict,
    progress: dict,
    chat_id: str, project: dict, user: str, skills_block: str,
    system_extra: str, model_override: str, target: str,
    source: str, author: str, conversation: Optional[dict],
    stage_models: Optional[dict] = None,
    mcp_with: Optional[list] = None, mcp_pool: Optional[McpPool] = None,
    images: Optional[list] = None,
    app: Optional[web.Application] = None,
) -> None:
    """Ciclo de vida completo de un run de experto en background.

    ADR-024: el HTTP ya respondió 202; acá corremos, persistimos
    (.md + JSONL + finish_chat + historial de la conversación) y
    avisamos al bot por /notify con kind response|error|cancelled y
    la metadata que necesita para postear en el hilo correcto.
    """
    # Correlación (2026-07-25): de acá en más, TODA línea que se loguee
    # en esta task —y en las que cree, watchdog y callbacks incluidos—
    # sale con el chat_id. Sin esto, un run muerto había que
    # reconstruirlo a mano desde la DB. Va primero: si algo revienta en
    # el setup, queremos que ese error también quede atribuido.
    logctx.bind(chat_id, project.get("slug", ""))

    conv_id = conversation["id"] if conversation else None
    thread_id = conversation.get("discord_thread_id") if conversation else None
    history_json = (conversation.get("messages_json") or "") if conversation else ""
    # Lo que el experto verificó en turnos anteriores (2026-08-26). Va
    # aparte del historial porque sobrevive a cosas distintas: un corte
    # por `off_plan` le saca al historial las tool calls enteras, y la
    # bitácora viaja como instructions, que no se persisten.
    bitacora_json = (conversation.get("bitacora_json") or "") if conversation else ""

    # Fase 1: progress store + callback. Si run_expert no acepta
    # on_progress (versión vieja), el callback es noop — best-effort.
    progress_cb = experts.make_progress_callback(
        store=progress, notify=notify,
        chat_id=chat_id, target=target,
        model=model_override or (project.get("defaults_json") or {}).get("model") or "",
    )
    # Canales vivos con el run (2026-07-25). `steer` es la MISMA lista que
    # muta POST /experts/steer (make_progress_callback ya dejó el
    # RunProgress en el store bajo este chat_id). `rescue` es por dónde
    # sale el historial cuando el run no puede devolver un dict — o sea,
    # cuando el humano cancela.
    steer_queue: list[str] = progress[chat_id].steer
    rescue: dict = {}

    error: str | None = None
    result: dict = {}
    status = "ok"
    t0 = time.monotonic()

    try:
        # Iter 11 (por etapas): el experto corre con planificador +
        # ejecutor + verificador + documentador. El opt-out por proyecto
        # es defaults_json.three_stage=false (nombre heredado). Si el
        # planificador juzga que el pedido es demasiado grande, el
        # wrapper corta ahí y devuelve la descomposición con
        # phase_at_end="planned", sin ejecutar nada. Los tests
        # existentes siguen llamando run_expert directo: esta ruta es
        # solo la del chat.
        result = await experts.run_expert_staged(
            project, user, skills_block=skills_block,
            system_extra=system_extra, model_override=model_override,
            stage_models=stage_models or {},
            db=db, message_history_json=history_json,
            bitacora_json=bitacora_json,
            on_progress=progress_cb,
            steer=steer_queue, rescue=rescue,
            mcp_with=mcp_with, mcp_pool=mcp_pool,
            images=images,
            # 2026-08-16: `ask_human` necesita saber a qué chat y a qué
            # hilo pertenece la pregunta que registra.
            chat_id=chat_id, conversation_id=conv_id or "",
        )
        if result.get("phase_at_end") == "planned":
            result = await _grafo_en_vez_de_proponer(
                app, db, project, user, conv_id, result)
    except experts.ModelUnavailable as e:
        error, status = str(e), "error"
    except asyncio.TimeoutError:
        error, status = "timeout total del experto", "error"
    except asyncio.CancelledError:
        # Cancelar usa el mismo cierre durable que éxito/error; incluso si
        # todavía no había historial, la fila y los artefactos quedan cerrados.
        status = "cancelled"
        result = {**rescue, "content": "run cancelado", "phase_at_end": "cancelled"}
    except Exception as e:
        logger.exception("experto %s falló", target)
        error, status = f"{type(e).__name__}: {e}", "error"
    finally:
        running.pop(chat_id, None)
        # Estado post-mortem para que /status siga respondiendo hasta que el
        # sweeper lo limpie. OJO: `finished` NO se prende acá — se prende
        # abajo, DESPUÉS de persistir el historial (ver el comentario).
        rp = progress.get(chat_id)
        if rp is not None:
            if status == "ok":
                rp.phase = result.get("phase_at_end", "done")
            else:
                rp.phase = status  # "error" o "timeout"
                rp.error = error
            lt = result.get("last_tool")
            if lt is not None:
                rp.last_tool = lt
            ti, to = result.get("tokens_in"), result.get("tokens_out")
            if ti is not None:
                rp.tokens_in = ti
            if to is not None:
                rp.tokens_out = to

    # ADR-025: persistir el historial de la conversación. Va ANTES de
    # prender `finished` (bug 2026-07-27): la UI poll-ea /experts/status y
    # en cuanto ve finished=True hace GET .../messages. Con el flag prendido
    # primero, ese GET devolvía el historial VIEJO — las burbujas del turno
    # (prompt + respuesta + tool calls) no aparecían hasta recargar la
    # página, que era justo cuando el historial ya estaba guardado.
    if conv_id and status in ("ok", "cancelled") and result.get("messages_json"):
        try:
            await db.save_conversation_messages(
                conv_id, result["messages_json"], expected=history_json)
        except Exception as e:
            status, error = "error", f"No se pudo guardar el historial: {e}"
            logger.exception("no pude persistir el historial de conv=%s",
                             conv_id[:8])
    # La bitácora se guarda SIN el gate de `status == "ok"`, a propósito:
    # el turno que más la necesita es el que se cortó, y ese no llega acá
    # como "ok". Es lo único que le queda al **continuá** cuando el
    # historial vino recortado.
    if conv_id and result.get("bitacora_json"):
        try:
            await db.save_conversation_bitacora(
                conv_id, result["bitacora_json"])
        except Exception:
            logger.exception("no pude persistir la bitácora de conv=%s",
                             conv_id[:8])
    duration_ms = result.get("duration_ms", int((time.monotonic() - t0) * 1000))
    content = result.get("content", "")
    generated = result.get("image_artifacts") or {}
    if generated and not all(f"/attachments/{aid}" in content for aid in generated):
        content += "\n\n" + attachments_mod.generated_markdown(generated)
    # 2026-08-16: si el experto dejó una pregunta abierta, va al final de
    # la respuesta. Que se vea en el MISMO mensaje es lo que la hace
    # accionable sin UI nueva: el humano lee la pregunta y contesta en el
    # chat como contestaría cualquier otra cosa. Los endpoints
    # /questions/{id}/answer existen para que la UI ponga botones encima,
    # pero el flujo funciona sin ellos.
    preguntas = []
    if status == "ok":
        try:
            preguntas = await db.list_expert_questions(
                chat_id=chat_id, only_open=True)
        except Exception as e:  # noqa: BLE001 — preguntar no rompe el run
            logger.warning("no pude leer las preguntas de %s: %r", chat_id, e)
    if preguntas:
        content = f"{content.rstrip()}\n\n{_render_pregunta(preguntas[0])}"
    # Medidor de contexto (2026-07-22): se calcula del historial que
    # acabamos de producir y se ANEXA al texto de la respuesta (no al
    # historial: el LLM no tiene que leer esto). Va antes de persistir
    # para que el .md y Discord vean lo mismo.
    ctx = await experts.context_usage_db(
        db, result.get("messages_json") or "")
    if ctx and content:
        content += experts.format_context_note(ctx)
    # Las correcciones en vivo (steer) son parte del pedido: sin esto el
    # .md archivado queda incoherente — el prompt dice "lee los 3
    # primeros archivos" y la respuesta contesta otra cosa, que es lo que
    # el humano pidió a mitad del run.
    for _s in result.get("steer_texts") or []:
        user += f"\n\n🧭 corrección en vivo: {_s}"
    artifact = dict(
        target=target, chat_id=chat_id, user=user, content=content,
        source=source, author=author, model=result.get("model", model_override),
        status=status, duration_ms=duration_ms, error=error,
        # Bitácora compacta al .md: `result["progress_events"]` ya es la
        # lista (la misma que se persiste a chats.progress_events). Sin
        # esto, el .md solo tiene pedido + respuesta — ver el bug de los
        # 790.142 tokens / 61 tool calls / .md de 1 KB.
        events=result.get("progress_events"),
    )
    md_path = await finalization.finish(
        db, chat_id, status=status, artifact=artifact,
        tokens_in=result.get("tokens_in"), tokens_out=result.get("tokens_out"),
        cache_read_tokens=result.get("cache_read_tokens"),
        tool_calls=result.get("tool_calls"), error=error,
        phase_at_end=result.get("phase_at_end"),
        last_tool=result.get("last_tool"),
        # Bug fix 2026-07-20: duration_ms/model se calculaban acá arriba
        # y se escribían al .md pero NO a la fila de chats (quedaba NULL).
        duration_ms=duration_ms,
        model=result.get("model") or model_override or None,
        # Sprint 1: timeline de eventos + context trim
        progress_events=json.dumps(result.get("progress_events") or []),
        trimmed_turns=result.get("trimmed_turns", 0),
        # 2026-07-26: peso de los tool results, para calibrar los caps.
        tool_bytes=(json.dumps(result["tool_meter"])
                    if result.get("tool_meter") else None),
        # 2026-08-14: etapas del runner (iter 11). Antes esto vivía solo
        # en el dict en memoria: al terminar el run se perdía el plan, el
        # veredicto y qué modelo corrió cada etapa. Con las etapas
        # repartidas entre proveedores (ejecutor pagado, auxiliares en
        # los endpoints gratis de NVIDIA) eso dejó de ser cosmético: sin
        # esto no hay forma de saber si un run se aprobó porque estaba
        # bien o porque el verificador se cayó.
        stages_json=_stages_json(result),
    )
    if rp is not None:
        rp.finished = True
        if error:
            rp.error, rp.phase = error, "error"
    # Sugerencias de continuación (2026-07-26): 2-3 próximos pasos que
    # la UI y Discord pintan como botones. Van DESPUÉS de finish_chat
    # (el run ya está cerrado y persistido: si el turno extra falla o
    # tarda, no se pierde nada) y ANTES del notify, para que el bot
    # pinte los botones en el MISMO mensaje de la respuesta.
    suggestions = await _suggest_followups(
        db, user=user, answer=content,
        run_model=result.get("model") or model_override or "",
    ) if status == "ok" else []
    if suggestions:
        try:
            await db.set_chat_suggestions(
                chat_id, json.dumps(suggestions, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            logger.warning("no pude guardar sugerencias de %s: %r", chat_id, e)

    kind = "cancelled" if status == "cancelled" else ("error" if error else "response")
    # Iter 10.0: bridge Discord↔UI. Si la conversación tiene
    # discord_user_id (autor Discord original), se lo pasamos al bot en
    # el notify para que sepa a quién mandarle el reply — sin este campo
    # el bot no puede rutear la respuesta a Discord cuando el chat nació
    # en la UI. discord_thread_id es para hilos existentes (auto-attach);
    # discord_user_id es para chats UI→Discord (este bridge nuevo).
    notify_discord_user_id = (
        conversation.get("discord_user_id") if conversation else None)
    notify_discord_author = (
        conversation.get("discord_author") if conversation else None)
    # Iter 10.1: guard soft. Si el run vino de Discord y el proyecto
    # NO tiene discord_channel_id seteado, marcamos el notify para que
    # el bot sepa que tiene que preguntar al admin qué canal elegir.
    # El run corre normal (no rompemos nada). El flag NO se manda si:
    #   - source != "discord" (UI/CLI no necesitan canal — tienen su
    #     propio panel)
    #   - el proyecto ya tiene canal (comportamiento actual)
    missing_channel = (
        source == "discord"
        and not (project.get("discord_channel_id") or "").strip())
    if missing_channel:
        logger.warning(
            "experts/run: source=discord pero proyecto %r sin "
            "discord_channel_id (chat_id=%s conv_id=%s) — el bot "
            "preguntará al admin qué canal usar",
            target, chat_id, conv_id)
    # El runner agrega los adjuntos recibidos al texto aun si el modelo
    # no cita sus ids. Conservamos también las menciones de uploads manuales.
    run_attachments = [
        aid for aid in dict.fromkeys(_ATT_ID_RE.findall(content or ""))
        if attachments_mod.resolve(aid) is not None
    ]
    if run_attachments:
        logger.info("experts/run: chat=%s adjunta %d archivo(s) generados: %s",
                    chat_id, len(run_attachments), ", ".join(run_attachments))

    await notify.send(
        agent_id=f"chat:{chat_id}",
        kind=kind,
        message=error if error else content,
        metadata={
            "chat_id": chat_id,
            "conversation_id": conv_id,
            "discord_thread_id": thread_id,
            **({"attachments": run_attachments} if run_attachments else {}),
            # Iter 10.0: campos nuevos para bridge bidireccional
            "discord_user_id": notify_discord_user_id,
            "discord_author": notify_discord_author,
            # Canal default del proyecto: si está seteado, el bot postea la
            # respuesta ahí (aunque el run haya nacido en la UI/CLI). Sin
            # esto el bot no sabía el canal vinculado y no podía responder.
            # El bot arbitra la precedencia (thread existente > canal).
            "discord_channel_id": (project.get("discord_channel_id") or None),
            "target": target,
            "source": source,
            "status": status,
            "resultado": _resultado_del_trabajo(result),
            "verifier_verdict": result.get("verifier_verdict") or "",
            "verifier_feedback": result.get("verifier_feedback") or "",
            "verifier_error": (result.get("stage_errors") or {}).get("verifier") or "",
            "model": result.get("model", model_override),
            "tokens_in": result.get("tokens_in"),
            "cache_read_tokens": result.get("cache_read_tokens"),
            "tokens_out": result.get("tokens_out"),
            "tool_calls": result.get("tool_calls"),
            "duration_ms": duration_ms,
            # Medidor de contexto: el aviso ya va en el texto; esto es
            # para que el bot/UI puedan pintarlo aparte si quieren.
            **({"context_tokens": ctx["base_tokens"],
                "context_limit": ctx["limit"],
                "context_pct": ctx["pct"]} if ctx else {}),
            # Iter 10.1: el bot usa esto para decidir si pregunta al
            # admin qué canal elegir (solo si source=discord y el
            # proyecto no tiene canal).
            **({"missing_discord_channel": True} if missing_channel else {}),
            # El bot los pinta como botones; el custom_id lleva el
            # índice y resuelve el texto con GET /chats/{chat_id}.
            **({"suggestions": suggestions} if suggestions else {}),
        },
    )


# Tope del turno extra que propone los próximos pasos. Corto a
# propósito: es un turno de una línea sobre texto ya escrito, y el
# humano está esperando la respuesta que ya terminó de generarse.
SUGGEST_TIMEOUT_S = 45.0


async def _suggest_followups(
    db: Database, *, user: str, answer: str, run_model: str = "",
) -> list[str]:
    """Sugerencias de continuación del run. Best-effort: [] si algo falla.

    Modelo: `suggestions_model` (system_config) > el modelo con el que
    corrió ESTE run > default global. Caer al modelo del run importa:
    un proyecto pineado a otro provider no tiene por qué tener key del
    default (y si no la tiene, el turno extra se cuelga hasta el timeout).

    Se apaga entero con `suggestions_enabled=0`.
    """
    model = run_model
    try:
        enabled = await db.get_config("suggestions_enabled", "1")
        if (enabled or "").strip().lower() in ("0", "false", "no", "off"):
            return []
        model = (await db.get_config("suggestions_model", "")) or run_model
    except Exception as e:  # noqa: BLE001
        logger.warning("sugerencias: no pude leer config (%r), sigo con default", e)
    try:
        return await asyncio.wait_for(
            experts.suggest_followups(user=user, answer=answer, model_spec=model),
            timeout=SUGGEST_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        # Sin botones se sigue pudiendo escribir: nunca rompemos el run.
        logger.info("sugerencias: la respuesta sale sin botones (%r)", e)
        return []


@_require_auth
@coordination.guard_workspace(DB_KEY)
async def experts_run(request: web.Request) -> web.Response:
    """POST /experts/run — corre el experto pydantic-ai de un proyecto.

    ADR-024: ASYNC. Responde 202 inmediato; el resultado (o error)
    llega al bot por POST /notify con agent_id "chat:<id>" y metadata
    {chat_id, conversation_id, discord_thread_id, tokens, ...}.

    Body:
        target:       str  (requerido, slug del proyecto)
        user:         str  (requerido)
        system:       str  (opcional, se concatena a las instructions)
        source:       str  (opcional, ej "discord")
        author:       str  (opcional)
        model:        str  (opcional, override "provider:modelo")
        conversation: str  (opcional, id de conversación abierta — ADR-025;
                            replaya el historial y lo persiste al terminar)
        discord_thread_id: str  (opcional; si no viene `conversation`, el
                            relay busca la conversación abierta de ese hilo
                            o crea una nueva — el bot queda sin estado)
        memory:       str  (opcional, query FTS5 — ADR-027; inyecta el
                            bloque "Memoria de conversaciones previas")

    Returns:
        202 -> {id, status: "running", conversation_id}
        400 -> body mal formado / conversación de otro proyecto
        404 -> proyecto o conversación desconocidos
        409 -> conversación cerrada (cerrar es cerrar, ADR-025)
    """
    db: Database = request.app[DB_KEY]
    skills: SkillCache = request.app[SKILLS_KEY]
    running: dict = request.app[RUNNING_KEY]
    progress: dict = request.app[PROGRESS_KEY]
    notify: NotifyClient = request.app[NOTIFY_KEY]

    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)

    target = body.get("target")
    user = body.get("user")
    if not isinstance(target, str) or not target.strip():
        return web.json_response({"error": "target requerido (string)"}, status=400)
    if not isinstance(user, str) or not user.strip():
        return web.json_response({"error": "user requerido (string)"}, status=400)
    target = target.strip()
    source = body.get("source") or "api"
    author = body.get("author") or ""
    system_extra = body.get("system") or ""
    model_override = body.get("model") or ""
    # Modelo por rol, SOLO para este run (2026-08-26). `model` sigue
    # siendo el del ejecutor; esto cubre las otras tres etapas, que
    # hasta ahora solo se podían cambiar por .env (global) o por
    # `defaults_json` del proyecto (permanente). El popover del chat
    # manda esto; el default sigue siendo no mandar nada.
    #
    # No se valida contra el catálogo a propósito: el desplegable que lo
    # manda ya se llena con los modelos prendidos, y un spec inventado
    # muere con un ModelUnavailable que nombra el modelo. Lo que sí se
    # valida es la forma, que es lo que hace de esto un borde de
    # confianza: claves conocidas, valores string, nada más.
    raw_stage = body.get("stage_models")
    stage_models: dict[str, str] = {}
    _stage_roles = {"planner", "verifier", "documenter"}
    if raw_stage is not None and not isinstance(raw_stage, dict):
        return web.json_response(
            {"error": "stage_models debe ser un objeto"}, status=400)
    if isinstance(raw_stage, dict):
        if _unknown := sorted(set(raw_stage) - _stage_roles):
            return web.json_response(
                {"error": "stage_models tiene roles desconocidos: "
                          + ", ".join(_unknown)}, status=400)
        if _invalid := sorted(k for k, v in raw_stage.items()
                              if not isinstance(v, str)):
            return web.json_response(
                {"error": "stage_models requiere valores string: "
                          + ", ".join(_invalid)}, status=400)
        for _rol in ("planner", "verifier", "documenter"):
            _v = raw_stage.get(_rol)
            if isinstance(_v, str) and _v.strip():
                stage_models[_rol] = _v.strip()
    # F1 (plan MCP_REGISTRY): selección explícita de MCPs on-demand.
    # `--con db` / `--with db,docs` en el texto, o body "with": [...].
    user, mcp_with = experts.parse_mcp_flags(user)
    # Working set scope (2026-07-20d): `--solo src/auth/*` acota la
    # atención del experto. Se inyecta como system_extra (mismo canal
    # que ya fluye a run_expert), no toca los tools — es una directiva.
    user, scope_paths = experts.parse_scope_flags(user)
    if scope_paths:
        _sb = experts.scope_block(scope_paths)
        system_extra = f"{system_extra}\n\n{_sb}" if system_extra else _sb
    # Selección explícita de skills: `--skill pdf` / body "skills": [...].
    # Fuerza al run una skill `manual` (no auto-inyectada), mismo canal
    # system_extra que --solo. La lista de disponibles sale en !ayuda.
    user, skill_names = experts.parse_skill_flags(user)
    body_skills = body.get("skills")
    if isinstance(body_skills, list):
        skill_names.extend(str(s) for s in body_skills if s)
    if skill_names:
        _skb = await asyncio.to_thread(
            skills.render_requested_block, skills.dir, skill_names)
        if _skb:
            system_extra = f"{system_extra}\n\n{_skb}" if system_extra else _skb
    if not user:
        return web.json_response(
            {"error": "user vacío (solo flags --con/--with/--solo/--skill)"},
            status=400)
    body_with = body.get("with")
    if isinstance(body_with, list):
        mcp_with.extend(str(s) for s in body_with if s)

    # `!comando` escrito en el chat (2026-08-01). El prefijo `!` lo
    # entendía SOLO el bot de Discord; en el chat web se iba al LLM como
    # prompt cualquiera — medido: dos "!ayuda" quemaron dos runs de
    # experto en website-demo. El guard va ACÁ y no en la UI porque este
    # endpoint es el camino compartido de todos los callers (web, bot,
    # extensión): uno solo cubre a los tres.
    #
    # Estrictamente aditivo: si el primer token no es un comando cuyo
    # handler resolvió, sigue de largo al experto como hasta hoy (un
    # "!ojo con esto" no se secuestra). El target del chat entra como
    # `project`/`target` para los handlers que lo piden (build, fact);
    # si al comando le falta un arg, el ValueError del registry se
    # devuelve tal cual, que ya es un mensaje entendible.
    if user.startswith("!"):
        registry: CommandRegistry = request.app[COMMANDS_KEY]
        cmd_name = user[1:].split()[0].lower() if len(user) > 1 else ""
        if cmd_name and cmd_name in registry.names():
            ctx = CommandContext(
                db=db, sessions=request.app[SESSIONS_KEY], running=running,
                source=source, author=author)
            try:
                text = await registry.dispatch(
                    cmd_name, {"project": target, "target": target}, ctx)
            except (ValueError, RuntimeError) as e:
                text = f"`!{cmd_name}` falló: {e}"
            return web.json_response({"command": cmd_name, "text": text})

    project = await db.get_project(target)
    if project is None or not project["enabled"]:
        available = [p["slug"] for p in await db.list_projects()]
        return web.json_response(
            {"error": f"proyecto {target!r} desconocido o deshabilitado",
             "available_projects": available},
            status=404,
        )

    # Etapas apagadas en el proyecto (2026-08-26). El popover del chat
    # ofrece los tres roles sin saber la config del proyecto, así que
    # `stage_models` podía traer un verificador para un proyecto que
    # tiene el verificador apagado: `run_expert_staged` lo descartaba en
    # silencio y el humano se quedaba creyendo que había elegido algo.
    # Se descarta igual —no es un error que justifique tirar el run—
    # pero se dice, y la UI lo muestra.
    ignored_stages: list[str] = []
    if stage_models:
        _d = project.get("defaults_json") or {}
        if not _d.get("three_stage", True):
            ignored_stages = list(stage_models)      # no corre ninguna
        else:
            ignored_stages = [
                rol for rol in ("verifier", "documenter")
                if rol in stage_models and not _d.get(rol, True)]
        for _rol in ignored_stages:
            stage_models.pop(_rol, None)
        if ignored_stages:
            logger.info(
                "experts_run: %s pidió modelo para etapa(s) apagada(s) en "
                "%s: %s", author or source, target, ", ".join(ignored_stages))

    # ADR-025: conversación (opcional). Validar ANTES de crear el chat.
    conversation: Optional[dict] = None
    conv_id = body.get("conversation") or ""
    if conv_id:
        if not isinstance(conv_id, str):
            return web.json_response(
                {"error": "conversation debe ser string"}, status=400)
        conversation = await db.get_conversation(conv_id)
        if conversation is None:
            return web.json_response(
                {"error": f"conversación {conv_id!r} desconocida"}, status=404)
        if conversation["status"] != "open":
            return web.json_response(
                {"error": "conversación cerrada (cerrar es cerrar; abre una "
                          "nueva con /nuevo)", "conversation_id": conv_id},
                status=409)
        if conversation["project_slug"].lower() != project["slug"].lower():
            return web.json_response(
                {"error": f"la conversación pertenece a "
                          f"{conversation['project_slug']!r}, no a "
                          f"{project['slug']!r}"},
                status=400)
        # touch al INICIO: el sweeper nunca ve stale una conv con run vivo
        await db.touch_conversation(conv_id)

    # Auto-attach por hilo Discord (fix 2026-07-12): si el caller no
    # trae `conversation` pero sí `discord_thread_id`, el relay resuelve
    # la conversación solo — sin esto, cada mensaje del hilo era un chat
    # amnésico y el /notify salía con discord_thread_id null.
    thread_id = body.get("discord_thread_id")
    discord_user_id = body.get("discord_user_id")  # Iter 10.0
    discord_author = body.get("discord_author")    # Iter 10.0
    if conversation is None and thread_id:
        if not isinstance(thread_id, str):
            return web.json_response(
                {"error": "discord_thread_id debe ser string"}, status=400)
        conversation = await db.get_open_conversation_by_thread(
            project["slug"], thread_id)
        if conversation is None:
            existing = await db.get_open_conversation_for_workspace(project)
            if existing is not None:
                return web.json_response(
                    {"error": "ya hay una conversación abierta para este repositorio",
                     "conversation_id": existing["id"]}, status=409)
            new_id = await db.create_conversation(
                project_slug=project["slug"], discord_thread_id=thread_id,
                discord_user_id=discord_user_id,
                discord_author=discord_author,
                requested_by=identity.requester(request))
            conversation = await db.get_conversation(new_id)
        else:
            await db.touch_conversation(conversation["id"])
            # Si la conversación ya existía pero le llegan estos campos
            # por primera vez (caso: thread viejo sin discord_user_id),
            # los guardamos — útil para que el bridge funcione con
            # hilos que ya estaban abiertos cuando se creó el campo.
            if discord_user_id and not conversation.get("discord_user_id"):
                await db.set_conversation_discord_user(
                    conversation["id"],
                    discord_user_id=discord_user_id,
                    discord_author=discord_author)
                conversation = await db.get_conversation(conversation["id"])

    # Seguimiento por GitHub (fase 4): si la conversación nació de un
    # issue, el experto arranca sabiendo qué tiene que resolver. Se lee
    # de GitHub en cada run (caché de 60s) en vez de copiarse a la DB:
    # si alguien edita el issue, el run siguiente ve la versión nueva.
    # Best-effort: sin `gh` el run sigue igual, solo sin el bloque.
    if conversation and conversation.get("issue_number"):
        gh_slug = await github_mod.repo_slug(project["repo_path"] or "")
        data = (await github_mod.issue(gh_slug, conversation["issue_number"])
                if gh_slug else None)
        if data:
            blk = github_mod.issue_block(data)
            system_extra = f"{system_extra}\n\n{blk}" if system_extra else blk

    # ADR-027: retrieval manual de memoria (si el caller lo pide).
    memory_query = body.get("memory") or ""
    if memory_query and isinstance(memory_query, str):
        hits = await db.search_memories(project["slug"], memory_query, limit=3)
        block = memory.build_memory_block(hits)
        if block:
            system_extra = f"{system_extra}\n\n{block}" if system_extra else block

    # Iter Discord-attachment: si el bot mandó una lista de `attachments`
    # (ids que ya subió via POST /discord/attachments), armamos un bloque
    # estándar y lo concatenamos al `user` que se le pasa al LLM. Si el
    # campo está ausente o vacío, no tocamos nada (back-compat al 100%).
    # Scope de la vista de adjuntos (2026-08-26): la conversación cuando
    # hay una, si no el proyecto. Los dos aíslan entre clientes, que es
    # lo que importa; la conversación además hace que un id nombrado tres
    # mensajes atrás siga resolviendo, porque su hardlink ya quedó en la
    # vista desde el turno en que se citó.
    attach_scope = attachments_mod.scope_for(
        conversation["id"] if conversation else "", project["slug"])
    raw_attachments = body.get("attachments")
    images: list[tuple[bytes, str]] = []
    if isinstance(raw_attachments, list) and raw_attachments:
        # Filtramos strings vacíos y casteamos a str (defensivo).
        valid_ids = [str(a).strip() for a in raw_attachments
                     if isinstance(a, (str,)) and a.strip()]
        block = attachments_mod.format_user_block(valid_ids, attach_scope)
        if block:
            user = f"{user.rstrip()}\n{block}"
        # Las imágenes van aparte, como partes binarias del prompt
        # (2026-07-31): el `user` sigue siendo str para el jsonl, el .md
        # y el historial. El bloque de arriba las nombra; acá viajan.
        images = attachments_mod.load_images(valid_ids)
        if images:
            # 2026-08-18: no todos los endpoints aceptan imágenes. Se
            # corta ACÁ y no adentro del run: un modelo ciego igual
            # contesta —con seguridad y sobre lo que no vio— y eso es
            # peor que un error. Cortar cuesta 0 tokens y el selector de
            # la UI está a un click. El guard va en el endpoint y no en
            # la UI por el mismo motivo que los roles: esconder no es
            # impedir.
            spec_efectivo = experts.resolve_model_spec(model_override, project)
            if not experts.has_vision(spec_efectivo):
                con_vista = ", ".join(
                    m["spec"] for m in experts.catalog()
                    if m.get("vision") == 1 and m.get("enabled")) or "ninguno cargado"
                return web.json_response(
                    {"error": "modelo sin visión",
                     "model": spec_efectivo,
                     "message": (
                         f"{spec_efectivo} no ve imágenes y adjuntaste "
                         f"{len(images)}. Elegí un modelo con visión "
                         f"({con_vista}) en el selector del chat, o sacá "
                         f"el adjunto.")},
                    status=400)
            logger.info("adjuntos: %d imagen(es) van al modelo en chat de %s",
                        len(images), target)

    try:
        skills_block = await skills.get_block()
    except Exception as e:
        logger.warning("skills: get_block rompio: %r (sigo sin skills)", e)
        skills_block = ""

    chat_id = await db.create_chat(
        project_slug=target, source=source, author=author, target=target,
        conversation_id=conversation["id"] if conversation else None,
        requested_by=identity.requester(request),
        user_prompt=user,
    )
    await persist.append_jsonl(target, "user", user, author=author, chat=chat_id)

    task = asyncio.create_task(_run_expert_bg(
        db=db, notify=notify, running=running, progress=progress,
        chat_id=chat_id,
        project=project, user=user, skills_block=skills_block,
        system_extra=system_extra, model_override=model_override,
        stage_models=stage_models,
        target=target, source=source, author=author,
        conversation=conversation,
        mcp_with=mcp_with, mcp_pool=request.app.get(MCP_POOL_KEY),
        images=images,
        # Para el disparador del grafo: la task de fondo se registra en
        # la app, así el apagado la espera y /graphs/{id}/cancel la ubica.
        app=request.app,
    ))
    running[chat_id] = task
    coordination.hold_current(task)
    bg_tasks: set = request.app[BG_TASKS_KEY]
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)

    return web.json_response(
        {"id": chat_id, "status": "running",
         "conversation_id": conversation["id"] if conversation else None,
         # Vacío en el caso normal; la UI solo avisa si trae algo.
         "ignored_stages": ignored_stages},
        status=202,
    )


@_require_auth
async def experts_cancel(request: web.Request) -> web.Response:
    """POST /experts/cancel/{chat_id} — cancela un run en curso.

    Iter 5.3: si el chat figura `running` en DB pero NO está en el
    RUNNING_KEY (memoria) — el proceso se reinició, el experto crasheó
    sin actualizar DB, o cualquier otro zombie — marcarlo `cancelled`
    directamente igual (no devolver 404). El usuario no quiere
    distinguir: quiere que desaparezca de "En curso".
    """
    db: Database = request.app[DB_KEY]
    running: dict = request.app[RUNNING_KEY]
    progress: dict = request.app[PROGRESS_KEY]
    chat_id = request.match_info["chat_id"]
    matches = [cid for cid in running if cid.startswith(chat_id)]
    cancelled: list[str] = []
    zombies: list[str] = []
    if matches:
        # Caso normal: el proceso está vivo, mandamos cancel().
        for cid in matches:
            running[cid].cancel()
            rp = progress.get(cid)
            if rp is not None:
                rp.finished = True
                rp.error = "cancelled"
                rp.phase = "cancelled"
            await db.finish_chat(cid, status="cancelled")
            cancelled.append(cid)
    else:
        # Caso zombie: el chat figura running en DB pero no hay proceso
        # vivo. Lo cerramos igual (el sweep después lo limpia, pero el
        # cliente quiere respuesta inmediata). NO 404: si la intención
        # del usuario es "sácalo de En curso", lo sacamos.
        chat = await db.get_chat(chat_id)
        if chat and chat.get("status") == "running":
            await db.finish_chat(
                chat_id, status="cancelled",
                error="zombie: cancelado sin proceso vivo (relay "
                "reinició o experto crasheó)")
            zombies.append(chat_id)
        else:
            # Sin match en memoria NI chat running en DB: 404 honesto.
            return web.json_response(
                {"error": "no hay run en curso con ese id"}, status=404)
    return web.json_response(
        {"cancelled": cancelled, "zombies_cleaned": zombies})


@_require_auth
async def experts_steer(request: web.Request) -> web.Response:
    """POST /experts/steer/{chat_id} — corrige el rumbo sin matar el run.

    Body: {"message": "no toques config.py, el bug está en admin.py"}.

    Encola el texto en el RunProgress; run_expert lo consume en el próximo
    borde de nodo (o sea: cuando termine la tool en vuelo, no a mitad),
    rescata el historial y re-entra con eso como prompt. El humano no
    pierde el trabajo hecho, que era lo que pasaba con la única salida que
    había antes: cancelar.

    409 si el run ya terminó — el caller (UI) reintenta como mensaje
    normal, que en una conversación es exactamente lo mismo.
    """
    progress: dict = request.app[PROGRESS_KEY]
    chat_id = request.match_info["chat_id"]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "body JSON inválido"}, status=400)
    message = (body.get("message") or "").strip()
    if not message:
        return web.json_response({"error": "message vacío"}, status=400)
    matches = [cid for cid in progress if cid.startswith(chat_id)]
    if not matches:
        return web.json_response(
            {"error": "no hay run con ese id (o el relay reinició)"},
            status=404)
    if len(matches) > 1:
        return web.json_response(
            {"error": "prefijo ambiguo", "matches": matches}, status=409)
    rp = progress[matches[0]]
    if rp.finished:
        return web.json_response(
            {"error": "el run ya terminó — mandalo como mensaje normal",
             "finished": True}, status=409)
    rp.steer.append(message)
    logger.info("steer encolado para %s (%d en cola, fase %s)",
                matches[0], len(rp.steer), rp.phase)
    return web.json_response(
        {"queued": len(rp.steer), "chat_id": matches[0], "phase": rp.phase})


@_require_auth
async def experts_status(request: web.Request) -> web.Response:
    """GET /experts/status/{chat_id} — liveness on-demand (Fase 1 A).

    Devuelve un snapshot del RunProgress: elapsed_s, idle_s, phase,
    last_tool, tool_calls, tokens, model, finished, error.

    Si el chat ya terminó, devolvemos el último estado conocido
    (finished=true). Si nunca existió, 404. El store es en memoria
    del proceso — si el relay reinició, los runs viejos se pierden
    y devolvemos 404 aunque el chat exista en SQLite (no podemos
    reconstruir el progreso, solo el .md final).
    """
    progress: dict = request.app[PROGRESS_KEY]
    chat_id = request.match_info["chat_id"]
    matches = [cid for cid in progress if cid.startswith(chat_id)]
    if not matches:
        # Distinguir "no existe" de "existe pero reinicié": el caller
        # puede ir a /chats/{id} para ver el resultado final.
        return web.json_response(
            {"error": "no hay run con ese id (o el relay reinició)",
             "lost_on_restart_possible": True},
            status=404)
    if len(matches) > 1:
        return web.json_response(
            {"matches": [m for m in matches],
             "snapshots": [progress[m].snapshot() for m in matches]})
    return web.json_response(progress[matches[0]].snapshot())


# ---------- conversaciones (ADR-025) + compactación (ADR-026) ----------

SWEEP_INTERVAL_S = 1800.0  # cada 30 min el sweeper revisa auto-close
#: El reintento de exportaciones tiene cadencia propia (2026-09-09).
#: Viajaba de colada en el sweeper de auto-close, o sea que una
#: exportación pendiente esperaba hasta media hora por una cadencia que
#: no tiene nada que ver con ella. Son dos trabajos distintos.
EXPORT_RETRY_INTERVAL_S = 60.0


_COMPACTING: set[str] = set()

# Estado del PR en background por conversación (ADR-025 / fix 2026-07-21).
# En proceso, no en la DB: es efímero (dura lo que el job) y el resultado
# durable ya queda en conversations.pr_url. Si el relay reinicia a mitad,
# el poll de la UI cae en "unknown" y el usuario ve el PR en GitHub.
_PR_JOBS: dict[str, dict] = {}


def _horas_legibles(ms: int) -> str:
    """`ms` -> "2h 49m" / "13m" / "40s". Para leer, no para parsear."""
    s = round((ms or 0) / 1000)
    if s < 60:
        return f"{s}s"
    m = s // 60
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


async def _finalize_pr_bg(db: Database, conv_id: str, project_slug: str,
                          project: dict, branch: str, summary: str,
                          issue_number: Optional[int] = None) -> None:
    """Verify + PR a develop, fuera del request de /close.

    Corre build/test del proyecto (hasta 420s) y le pide al LLM la
    descripción del diff antes de abrir el PR; eso no entra en el
    timeout de un fetch. Publica progreso en `_PR_JOBS[conv_id]` para
    GET /conversations/{id}/pr. Best-effort: nunca lanza.
    """
    from . import night
    job = _PR_JOBS[conv_id] = {"state": "verifying", "pr_url": None,
                               "error": None, "draft": False,
                               "committed": False}
    title = f"[{project_slug}] {branch}"
    # Horas del PR (2026-08-31): el tiempo de USO —la suma de lo que
    # duraron los runs—, no el reloj de pared de la conversación. Es el
    # mismo número que muestra el chip ⏱ del header, y el único con
    # sentido para "cuánto llevó esto": medido sobre `un run de ejemplo`, la
    # pared decía 16h16 y el trabajo real fueron 2h49.
    #
    # En su propio try porque acá todavía no entramos al `try` grande:
    # una excepción dejaría el job clavado en "verifying" y el PR sin
    # abrir, que es justo lo que el docstring promete que no pasa. Sin
    # las horas el PR sale igual.
    try:
        uso = await db.conversation_usage(conv_id)
    except Exception:  # noqa: BLE001
        logger.exception("no pude calcular el tiempo de uso de conv=%s",
                         conv_id[:8])
        uso = {"runs": 0, "ms": 0}
    trabajo = (f"{_horas_legibles(uso['ms'])} de trabajo en {uso['runs']} "
               f"run{'' if uso['runs'] == 1 else 's'} — ") if uso["runs"] else ""
    body = ((summary or "").strip() or "Cambios de la conversación de 4bis.relay.") + (
        f"\n\n_Conversación `{conv_id[:8]}` — {trabajo}4bis.relay_")
    # Seguimiento por GitHub (fase 4): la keyword la interpreta GitHub al
    # mergear — cierra el issue y el tablero lo mueve a Done sin que nadie
    # actualice estado a mano. Va en el body base, no en el `body_builder`,
    # porque ese es best-effort y puede no correr.
    if issue_number:
        body += f"\n\nCloses #{issue_number}"

    async def _build_body(stat: str, diff: str) -> tuple[str, bool]:
        """Verify (build+test) + descripción del diff → body del PR.

        El verify NO bloquea el PR: si viene rojo el PR se abre igual
        pero como draft y con el ❌ arriba de todo. Bloquear dejaría el
        trabajo del experto varado en una rama que nadie mira.
        """
        ok, verify_md = await night.verify_repo(project["repo_path"], project)
        job["verify"] = {True: "ok", False: "failed", None: "none"}[ok]
        job["state"] = "describing"
        described = await memory.describe_changes(diff, stat=stat)
        job["state"] = "opening"
        return "\n\n".join([verify_md] + ([described] if described else [])), ok is False

    try:
        outcome = await git_flow.finalize_conversation_pr(
            project["repo_path"], branch, title=title, body=body,
            body_builder=_build_body)
    except Exception as e:  # noqa: BLE001 — best-effort; el /close ya respondió
        logger.exception("PR en background de conv=%s rompió", conv_id[:8])
        job.update(state="error", error=f"{type(e).__name__}: {e}")
        return
    job.update(state="done" if outcome.get("pr_url") else "error",
               pr_url=outcome.get("pr_url"), error=outcome.get("error"),
               draft=outcome.get("draft", False),
               committed=outcome.get("committed", False))
    if outcome.get("pr_url"):
        await db.set_conversation_pr(conv_id, outcome["pr_url"])
        # 2026-07-27: la rama local ya está pusheada y con PR, así que no
        # aporta nada — antes quedaba una por conversación esperando que
        # alguien la borrara a mano desde la Admin UI. El repo vuelve a
        # develop. git_flow no toca ni el remoto ni ramas protegidas.
        cleanup = await git_flow.cleanup_conversation_branch(
            project["repo_path"], branch)
        job["branch_deleted"] = cleanup["deleted"]
        job["branch"] = branch
        if cleanup.get("error"):
            logger.warning("no pude limpiar la rama %s: %s",
                           branch, cleanup["error"])
    logger.info("PR background conv=%s → %s", conv_id[:8],
                outcome.get("pr_url") or outcome.get("error"))


async def _compact_and_store(db: Database, conv_id: str,
                             project_slug: str, messages_json: str) -> None:
    """Compacta una conversación cerrada → summary + facts + FTS5.

    Best-effort total (ADR-026): si el compactador falla, la
    conversación queda closed sin summary y se loggea. Reintentable:
    volver a llamar POST /conversations/{id}/close re-dispara.
    Guard anti-doble-gasto: dos /close casi simultáneos (o /close +
    sweeper) compactarían dos veces — el segundo se saltea.
    """
    if conv_id in _COMPACTING:
        logger.info("compactación conv=%s ya en curso, salteo", conv_id[:8])
        return
    _COMPACTING.add(conv_id)
    try:
        # Hechos vigentes → el compactador detecta obsoletos y no duplica.
        existing_facts = await db.list_facts(project_slug, limit=100)
        result = await memory.compact_conversation(
            messages_json, existing_facts=existing_facts)
        if result is None:
            logger.info("compactación conv=%s: transcript vacío, salteo",
                        conv_id[:8])
            return
        if result.summary.strip():
            await db.set_conversation_summary(conv_id, result.summary)
            await db.add_memory(conv_id, project_slug, result.summary)
        n_facts = await db.add_facts(
            project_slug, result.facts, source_conversation=conv_id)
        # Supersede soft: solo ids que realmente le mostramos (el LLM no
        # puede invalidar hechos fuera de su vista).
        shown_ids = {f["id"] for f in existing_facts}
        obsolete = [i for i in result.obsolete_fact_ids if i in shown_ids]
        n_superseded = 0
        if obsolete:
            n_superseded = await db.supersede_facts(
                obsolete, project_slug, superseded_by=conv_id)
        # Autoaprendizaje (2026-07-12): si el compactador destiló un
        # procedimiento reusable, queda como BORRADOR pendiente de
        # aprobación en la Admin UI (tab Skills). Nunca se instala solo.
        draft_id = None
        if result.skill is not None and result.skill.content.strip():
            draft_id = await db.add_skill_draft(
                name=result.skill.name,
                description=result.skill.description,
                content=result.skill.content,
                project_slug=project_slug,
                source_conversation=conv_id)
        logger.info(
            "compactación conv=%s ok: summary=%d chars, facts=%d, "
            "superseded=%d%s",
            conv_id[:8], len(result.summary), n_facts, n_superseded,
            f", skill draft #{draft_id} pendiente" if draft_id else "")
    except Exception:
        logger.exception("compactación conv=%s falló (queda sin summary)",
                         conv_id[:8])
    finally:
        _COMPACTING.discard(conv_id)


async def _export_retry_loop(app: web.Application) -> None:
    """Reintenta las exportaciones pendientes, con su propia cadencia.

    El backoff de `finalization` decide CUÁNDO le toca a cada una; esto
    solo pregunta seguido. Un loop aparte y no un pedazo del sweeper de
    auto-close porque son dos trabajos con dos relojes: media hora es
    razonable para cerrar conversaciones viejas y es una eternidad para
    recuperar el .md de un chat que el humano está mirando.
    """
    db: Database = app[DB_KEY]
    while True:
        await asyncio.sleep(EXPORT_RETRY_INTERVAL_S)
        try:
            await finalization.retry_pending(db)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — un barrido caído no baja el relay
            logger.exception("reintento de exportaciones: falló la vuelta")


async def _autoclose_sweeper(app: web.Application) -> None:
    """Cierra conversaciones abiertas sin actividad > N horas y las
    compacta (ADR-025). N = FOURBIS_CONV_AUTOCLOSE_H (default 24).

    No hace falta chequear runs vivos: touch_conversation se llama al
    INICIO de cada run, y un run dura <= expert_timeout << 24h.
    """
    from . import config as relay_config
    db: Database = app[DB_KEY]
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_S)
        try:
            hours = relay_config.conv_autoclose_hours()
            for conv in await db.stale_open_conversations(hours):
                project = await db.get_project(conv["project_slug"])
                if project and coordination.busy(db, project):
                    continue
                if await db.close_conversation(conv["id"]):
                    logger.info("auto-close conv=%s (> %sh sin actividad)",
                                conv["id"][:8], hours)
                    if conv.get("messages_json"):
                        _spawn_bg(_compact_and_store(
                            db, conv["id"], conv["project_slug"],
                            conv["messages_json"]))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sweeper de auto-close rompió (sigo)")


@_require_auth
@coordination.guard_workspace(DB_KEY)
async def conversations_create(request: web.Request) -> web.Response:
    """POST /conversations — abre una conversación (comando /nuevo).

    Body:
        project:           str  (requerido, slug)
        discord_thread_id: str  (opcional; el bot lo manda al crear el hilo)
        author:            str  (opcional; username Discord → nombre de rama)

    Abre además la rama git de trabajo `<autor>-<fecha>[-N]` (siempre una
    rama nueva: el sufijo esquiva las locales Y las remotas que ya
    existan) desde la base limpia. Regla: UNA conversación abierta por
    repo (un solo working tree → dos ramas pelearían). 409 si ya hay una.

    Returns:
        201 -> {id, project_slug, status: "open", branch, base_branch}
        404 -> proyecto desconocido
        409 -> ya hay una conversación abierta para ese proyecto
        422 -> el repo no está en un estado git limpio (no se pudo ramificar)
    """
    db: Database = request.app[DB_KEY]
    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    slug = body.get("project")
    if not isinstance(slug, str) or not slug.strip():
        return web.json_response({"error": "project requerido (string)"}, status=400)
    project = await db.get_project(slug.strip())
    if project is None or not project["enabled"]:
        available = [p["slug"] for p in await db.list_projects()]
        return web.json_response(
            {"error": f"proyecto {slug!r} desconocido o deshabilitado",
             "available_projects": available},
            status=404,
        )
    thread_id = body.get("discord_thread_id")
    if thread_id is not None and not isinstance(thread_id, str):
        return web.json_response(
            {"error": "discord_thread_id debe ser string"}, status=400)
    author = body.get("author")
    if author is not None and not isinstance(author, str):
        return web.json_response({"error": "author debe ser string"}, status=400)
    # Iter 10.0: bridge Discord↔UI. Si el bot C# crea una conversación
    # trayendo el user_id del autor, lo guardamos para que la UI pueda
    # mandar replies de vuelta a Discord después.
    discord_user_id = body.get("discord_user_id")
    if discord_user_id is not None and not isinstance(discord_user_id, str):
        return web.json_response(
            {"error": "discord_user_id debe ser string"}, status=400)
    discord_author = body.get("discord_author")
    if discord_author is not None and not isinstance(discord_author, str):
        return web.json_response(
            {"error": "discord_author debe ser string"}, status=400)

    # Vinculación automática (2026-08-17). Si hay un Discord por default
    # configurado y el caller no trajo uno, la conversación NACE avisando.
    # Sin esto había que acordarse de apretar el botón antes de irse —
    # justo el momento en el que uno no se acuerda. Lo explícito gana:
    # una conversación que ya trae su user_id (las que abre el bot) no se
    # toca.
    if not (discord_user_id or "").strip():
        auto_id, auto_author = relay_config.default_discord_user()
        if auto_id:
            discord_user_id = auto_id
            discord_author = (discord_author or "").strip() or auto_author or None

    # Regla: una sola conversación abierta por repo (un working tree).
    existing = await db.get_open_conversation_for_workspace(project)
    if existing is not None:
        return web.json_response(
            {"error": "ya hay una conversación abierta para este proyecto "
                      "(cerrala con /cerrar antes de abrir otra)",
             "conversation_id": existing["id"],
             "branch": existing.get("branch")},
            status=409)

    # Seguimiento por GitHub (fase 4): `issue: N` ata la conversación a un
    # issue. Se valida ACÁ contra GitHub — una conversación atada a un
    # issue inexistente después abre un PR con un `Closes #N` que no
    # cierra nada, y eso se descubre tarde y a mano.
    issue_number = body.get("issue")
    if issue_number is not None:
        if isinstance(issue_number, bool) or not isinstance(issue_number, int):
            return web.json_response(
                {"error": "issue debe ser un entero"}, status=400)
        slug_gh = await github_mod.repo_slug(project["repo_path"] or "")
        if not slug_gh:
            return web.json_response(
                {"error": "el repo no tiene remote de GitHub: no puedo atar "
                          "la conversación a un issue"}, status=422)
        if await github_mod.issue(slug_gh, issue_number) is None:
            return web.json_response(
                {"error": f"issue #{issue_number} no existe en {slug_gh} "
                          f"(o `gh` no está autenticado)"}, status=404)

    # Rama de trabajo determinista (el relay controla git, no el experto).
    branch = None
    base_branch = None
    repo_path = project["repo_path"]
    if repo_path and await git_flow.is_git_repo(repo_path):
        if not await git_flow.has_commits(repo_path):
            # Repo `git init` sin commit local (HEAD unborn), aunque tenga
            # refs de origin/*. No hay base local de la cual ramificar, y
            # forzar checkout de origin/<base> destruiría los archivos
            # untracked de la working tree (caso real: rewrite local no
            # commiteado sobre un remote con otro proyecto). Degradamos a
            # conversación SIN rama —el chat corre igual— en vez de romper.
            logger.info(
                "conversación en %s sin rama: el repo no tiene commit local "
                "(HEAD unborn). Commiteá tu trabajo para que /nuevo ramifique.",
                project["slug"])
        else:
            # La base REAL de la rama de trabajo (develop si existe), no el
            # trunk: el 201 mentía cuando no coincidían.
            base_branch = await git_flow.work_base_branch(repo_path)
            try:
                branch = await git_flow.open_conversation_branch(
                    repo_path, author)
            except git_flow.GitFlowError as e:
                return web.json_response(
                    {"error": f"no se pudo crear la rama de trabajo: {e}"},
                    status=422)

    conv_id = await db.create_conversation(
        project_slug=project["slug"], discord_thread_id=thread_id,
        author=author, branch=branch,
        discord_user_id=discord_user_id, discord_author=discord_author,
        requested_by=identity.requester(request))
    if issue_number is not None:
        await db.set_conversation_issue(conv_id, issue_number)
    logger.info("conversación creada id=%s project=%s thread=%s branch=%s "
                "discord_user=%s issue=%s",
                conv_id[:8], project["slug"], thread_id, branch,
                discord_user_id, issue_number)
    return web.json_response(
        {"id": conv_id, "project_slug": project["slug"], "status": "open",
         "branch": branch, "base_branch": base_branch,
         "discord_user_id": discord_user_id,
         "discord_author": discord_author,
         "issue_number": issue_number},
        status=201)


@_require_auth
async def conversations_list(request: web.Request) -> web.Response:
    """GET /conversations?project=&status=&limit= — sin messages_json."""
    db: Database = request.app[DB_KEY]
    limit = min(int(request.query.get("limit", "20")), 200)
    items = await db.list_conversations(
        project_slug=request.query.get("project") or None,
        status=request.query.get("status") or None,
        limit=limit)
    return web.json_response({"conversations": items})


@_require_auth
async def conversations_get(request: web.Request) -> web.Response:
    """GET /conversations/{id} — detalle (messages_json como longitud)."""
    db: Database = request.app[DB_KEY]
    conv = await db.get_conversation(request.match_info["id"])
    if conv is None:
        return web.json_response({"error": "no existe"}, status=404)
    conv = dict(conv)
    raw = conv.pop("messages_json") or ""
    # Cantidad de elementos del array, no largo del string serializado
    # (bug 2026-09-06: `len(raw)` sobre el JSON como cadena devolvía
    # 39037 para 8 mensajes en un run de ejemplo). Debe coincidir con lo que
    # proyecta `db.list_conversations` vía `json_array_length`, o la
    # sidebar mezcla números en el mismo listado.
    conv["messages_len"] = len(json.loads(raw)) if raw else 0
    conv["context"] = await experts.context_usage_db(db, raw)
    # Tiempo de uso (2026-08-31): la suma de los runs, no el reloj de
    # pared. Lo pinta el chip ⏱ del header y es la misma cuenta que va
    # a las horas del PR en /cerrar.
    conv["usage_time"] = await db.conversation_usage(conv["id"])
    return web.json_response(conv)


@_require_auth
async def conversations_get_messages(request: web.Request) -> web.Response:
    """GET /conversations/{id}/messages — messages_json parseado a turnos.

    Devuelve una lista de turnos legibles para la UI:
        [{"role": "user"|"assistant"|"tool", "content": str, "tool_name"?: str}]

    Best-effort: si el JSON está corrupto, devuelve 200 con `messages: []`
    y un warning en `error`. Nunca 500 por basura del cliente.

    Caps defensivos (bug fix 2026-07-08 — Sub-ola 2.4):
      - `?max_turns=N` (default 200): tope de turnos en el response
      - `?content_cap=N` (default 4000): tope de chars por turno
        individual. Turnos más largos se truncan con sufijo
        `… [truncado, N chars totales]`.
      - Sin estos caps, una conversación con 50+ turnos y tool calls
        grandes (cbm_query sobre INVENTORYDEMO = 30KB de JSON por tool return)
        podía devolver varios MB al cliente, lo que colgaba el modal
        de la UI. Ahora el response es bounded.
    """
    db: Database = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    # parsear params con fallback a defaults
    try:
        max_turns = int(request.query.get("max_turns", "200"))
    except (TypeError, ValueError):
        max_turns = 200
    try:
        content_cap = int(request.query.get("content_cap", "4000"))
    except (TypeError, ValueError):
        content_cap = 4000
    # acotar a límites razonables (anti-DoS del cliente)
    max_turns = max(1, min(max_turns, 2000))
    content_cap = max(100, min(content_cap, 100_000))

    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response({"error": "no existe"}, status=404)

    # Resultado durable primero: un MD viejo, inaccesible o pendiente no
    # debe ocultar una respuesta confirmada. MD para los chats históricos;
    # messages_json queda como último fallback, porque puede estar compactado.
    md_turns: list[dict] = []
    chats = await db.list_chats_by_conversation(conv_id)
    for c in chats:
        md_path = c.get("md_path")
        turns = []
        if c.get("output_payload"):
            try:
                turns = finalization.turns(c["output_payload"])
            except (ValueError, TypeError, AttributeError, KeyError):
                logger.warning("chat output inválido: %s", c.get("id"))
        try:
            if not turns and md_path:
                turns = await asyncio.to_thread(persist.parse_chat_md_turns, md_path)
        except Exception as e:  # noqa: BLE001 — best-effort por run
            logger.warning(
                "skipping chat md conv=%s chat=%s err=%r",
                conv_id[:8], c.get("id", "?")[:8], e,
            )
            continue
        # Los pasos del run cuelgan de SU turno assistant: en la UI son el
        # bloque "proceso" colapsado que va arriba de esa respuesta.
        # Con ellos van el contador de tools y la fase final: sin eso, un
        # turno que anunció y no ejecutó (0 tools) se leía igual que uno
        # que trabajó — el agujero de un hilo de ejemplo (2026-08-13).
        steps = _steps_from_progress(c.get("progress_events"))
        for t in turns:
            if t.get("role") == "assistant":
                if steps:
                    t["steps"] = steps
                t["tool_calls"] = c.get("tool_calls")
                t["phase_at_end"] = c.get("phase_at_end")
                t["run_status"] = c.get("status")
                t["duration_ms"] = c.get("duration_ms")
                try:
                    stages = json.loads(c.get("stages_json") or "{}")
                    t["stages"] = stages if isinstance(stages, dict) else {}
                except (ValueError, TypeError):
                    t["stages"] = {}
                t["export_pending"] = c.get("exported") == 0
                t["export_error"] = c.get("export_error") or ""
                break
        # Quién pidió ESTE turno (ADR-037). Cuelga del turno `user`
        # porque es su prompt: en un hilo compartido por el túnel, dos
        # turnos seguidos pueden ser de dos personas distintas, y sin
        # esto la UI los pinta a los dos como "tú".
        for t in turns:
            if t.get("role") == "user":
                t["requested_by"] = c.get("requested_by")
                break
        md_turns.extend(turns)

    if md_turns:
        # Aplicar caps (max_turns / content_cap) igual que el camino viejo.
        out: list[dict] = []
        truncated_turns = 0
        for t in md_turns:
            role = t.get("role", "")
            content = t.get("content", "") or ""
            capped = _cap_text(content, content_cap)
            turn = {
                "role": role,
                "content": capped["text"],
                "truncated": capped["truncated"],
                "total_chars": capped["total_chars"],
            }
            if "tool_name" in t:
                turn["tool_name"] = t["tool_name"]
            if t.get("steps"):
                turn["steps"] = t["steps"]
            if t.get("tool_calls") is not None:
                turn["tool_calls"] = t["tool_calls"]
            if t.get("phase_at_end"):
                turn["phase_at_end"] = t["phase_at_end"]
            if t.get("requested_by"):
                turn["requested_by"] = t["requested_by"]
            for key in ("run_status", "duration_ms", "stages", "export_pending", "export_error"):
                if key in t:
                    turn[key] = t[key]
            if capped["text"] or role == "tool" or "tool_name" in t:
                out.append(turn)
            elif role == "assistant":
                # assistant vacío (modelo de razonamiento): placeholder
                # accionable, igual que el camino viejo. Los pasos van
                # igual — es justo el caso donde el proceso es TODO lo
                # que quedó del run.
                out.append({**turn, "content": "", "truncated": False, "total_chars": 0})
            if len(out) >= max_turns:
                truncated_turns += 1
                break
        return web.json_response({
            "conversation_id": conv_id,
            "messages": out,
            "turn_count": len(out),
            "truncated": truncated_turns > 0,
            "content_cap": content_cap,
            "max_turns": max_turns,
        })

    # Hay chats pero todos los .md estan rotos/ilegibles: NO caemos al
    # JSON (puede ser el resumen de la compactacion). Mejor [] honesto.
    if chats:
        return web.json_response({
            "conversation_id": conv_id,
            "messages": [],
            "turn_count": 0,
            "truncated": False,
            "content_cap": content_cap,
            "max_turns": max_turns,
        })

    raw = conv.get("messages_json") or ""
    if not raw.strip():
        return web.json_response({"conversation_id": conv_id, "messages": [],
                                 "turn_count": 0, "truncated": False})
    try:
        msgs = json.loads(raw)
    except json.JSONDecodeError as e:
        return web.json_response(
            {"conversation_id": conv_id, "messages": [],
             "error": f"JSON corrupto: {e}", "truncated": False})
    out: list[dict] = []
    truncated_turns = 0
    for m in msgs:
        kind = m.get("kind", "")
        parts = m.get("parts") or []
        if kind == "request":
            # juntar texto de UserPromptPart
            text_parts = []
            for p in parts:
                if p.get("part_kind") == "user-prompt":
                    c = p.get("content", "")
                    text_parts.append(
                        c if isinstance(c, str) else json.dumps(c))
            content = _cap_text("\n".join(text_parts).strip(), content_cap)
            if content["text"]:
                out.append({
                    "role": "user",
                    "content": content["text"],
                    "truncated": content["truncated"],
                    "total_chars": content["total_chars"],
                })
            # tool returns también vienen como "request" en algunos flujos
            for p in parts:
                if p.get("part_kind") == "tool-return":
                    tc = p.get("content", "")
                    capped = _cap_text(
                        tc if isinstance(tc, str) else json.dumps(tc),
                        content_cap)
                    out.append({"role": "tool", "tool_name": "tool",
                                "content": capped["text"],
                                "truncated": capped["truncated"],
                                "total_chars": capped["total_chars"]})
        elif kind == "response":
            text_parts, tool_parts = [], []
            for p in parts:
                pk = p.get("part_kind", "")
                if pk == "text":
                    text_parts.append(p.get("content", ""))
                elif pk == "tool-call":
                    # Guardamos el part entero (no solo el nombre): los args
                    # sirven para reconstruir el diff de edit_file al releer
                    # el hilo (Fase 2 UI, 2026-07-20e).
                    tool_parts.append(p)
            if text_parts:
                content = _cap_text("".join(text_parts).strip(), content_cap)
                if content["text"]:
                    out.append({
                        "role": "assistant",
                        "content": content["text"],
                        "truncated": content["truncated"],
                        "total_chars": content["total_chars"],
                    })
                elif not tool_parts:
                    # Respuesta final con texto vacío: modelos de
                    # razonamiento (MiniMax-M3) a veces vuelcan todo en el
                    # `thinking` y devuelven texto en blanco. Antes este
                    # turno se dropeaba y la UI mostraba "1 turno" sin la
                    # respuesta → parecía que el experto no contestó.
                    # Emitimos el turno vacío para que la UI muestre un
                    # placeholder accionable. (Si hubo tool-calls NO va:
                    # ese turno ya se representa con los chips de tool.)
                    out.append({
                        "role": "assistant", "content": "",
                        "truncated": False, "total_chars": 0,
                    })
            for tp in tool_parts:
                tn = tp.get("tool_name", "?")
                # args puede venir dict o JSON string (pydantic-ai).
                args = tp.get("args")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, ValueError):
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                # línea legible (📄 leyó `x` · ✏️ editó `y` +N−M) + diff
                # (solo edit_file). Mismo formato que el streaming en vivo.
                summary, diff, cmd = experts._format_tool_step(tn, args)
                turn = {"role": "tool", "tool_name": tn,
                        "summary": summary,
                        "content": f"(llamó {tn})",
                        "truncated": False,
                        "total_chars": len(f"(llamó {tn})")}
                if diff:
                    turn["diff"] = diff
                # El comando entero. Acá no hay salida: este camino lee
                # `messages_json`, donde el tool return es otro turno.
                if cmd:
                    turn["cmd"] = cmd
                out.append(turn)
        # cortar si pasamos max_turns (respetar orden natural del historial)
        if len(out) >= max_turns:
            truncated_turns += 1
            break
    return web.json_response({
        "conversation_id": conv_id,
        "messages": out,
        "turn_count": len(out),
        "truncated": truncated_turns > 0,
        "content_cap": content_cap,
    })


# Fases sin `message`: ruido de heartbeat/telemetría, no razonamiento.
# `thinking` y `writing` son "sigue vivo", no "pensó esto".
_STEP_PHASES = {"say": "say", "tool_call": "tool", "steer": "steer"}
_STEP_MSG_CAP = 2000
_STEP_MAX = 120


def _steps_from_progress(raw: str | None) -> list[dict]:
    """`chats.progress_events` → los pasos que la UI muestra colapsados.

    El razonamiento del experto (fase `say`) y sus tool calls YA se
    guardaban acá run a run, pero nadie los servía: el .md solo tiene
    `## Usuario` / `## Respuesta`, así que al recargar el hilo todo el
    proceso desaparecía y quedaba la respuesta sola sin el cómo.

    Best-effort: JSON roto o formato inesperado ⇒ [] (el hilo se ve como
    antes, nunca 500). Capeado: un run largo son 200+ eventos y esto
    viaja en el mismo response que los turnos.
    """
    if not raw:
        return []
    try:
        events = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    if not isinstance(events, list):
        return []
    steps: list[dict] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        # `tool_result` no es un paso: es la salida de la terminal, y va
        # colgada del paso de SU tool. Mismo criterio que el timeline en
        # vivo (`_attach_output` en experts.py): el paso MÁS VIEJO de esa
        # tool que todavía no tiene salida. Así el hilo releído se ve
        # igual que el hilo en vivo, que es la regla de esta función.
        #
        # 2026-09-04: esto buscaba de atrás para adelante, y con dos
        # llamadas a la misma tool en un turno cruzaba las salidas —el
        # primer resultado se colgaba del último paso y viceversa—.
        # Medido: pydantic-ai entrega los `ToolReturnPart` en el ORDEN
        # en que se pidieron las tools, así que el que corresponde es el
        # primero sin salida (FIFO). No se veía antes porque el turno
        # dejaba un solo paso y los resultados de más se descartaban.
        if e.get("phase") == "tool_result":
            out = e.get("output")
            if isinstance(out, str) and out:
                for step in steps:
                    if (step.get("kind") == "tool"
                            and step.get("tool") == e.get("tool")
                            and (not e.get("tool_call_id") or
                                 step.get("tool_call_id") == e["tool_call_id"])
                            and not step.get("output")):
                        step["output"] = out[:_STEP_MSG_CAP * 3]
                        break
            continue
        kind = _STEP_PHASES.get(e.get("phase") or "")
        msg = e.get("message")
        if not kind or not isinstance(msg, str) or not msg.strip():
            continue
        step = {"kind": kind, "message": msg[:_STEP_MSG_CAP]}
        if e.get("tool"):
            step["tool"] = str(e["tool"])
        if e.get("tool_call_id"):
            step["tool_call_id"] = str(e["tool_call_id"])
        # El diff de edit_file es lo que hace que valga la pena abrir el
        # bloque; se capea igual que el mensaje pero más generoso.
        if isinstance(e.get("diff"), str):
            step["diff"] = e["diff"][:_STEP_MSG_CAP * 3]
        # El comando entero de la shell (el encabezado solo lleva la
        # primera línea recortada).
        if isinstance(e.get("cmd"), str) and e["cmd"]:
            step["cmd"] = e["cmd"][:_STEP_MSG_CAP]
        steps.append(step)
        if len(steps) >= _STEP_MAX:
            break
    return steps


def _cap_text(s: str, cap: int) -> dict:
    """Capa un string a `cap` chars. Devuelve dict listo para mergear.

    Mantiene `text` como key principal para retrocompatibilidad con
    callers que esperan `{text: ..., truncated: ..., total_chars: ...}`.
    El endpoint luego splatea estos campos sobre el turno con
    `content=text` (key pública del contrato).

    Si el original es <= cap, devuelve tal cual con truncated=False.
    Si se cortó, agrega sufijo claro para que el usuario sepa que
    falta contenido y cómo pedirlo (?content_cap=N).
    """
    if not s or len(s) <= cap:
        return {"text": s or "", "truncated": False,
                "total_chars": len(s or "")}
    cut = s[:cap]
    last_nl = cut.rfind("\n")
    if last_nl > cap // 2:
        cut = cut[:last_nl]
    return {
        "text": cut + f"\n\n… [truncado, {len(s)} chars totales. "
                      f"Sube el cap con ?content_cap={max(cap * 2, cap + 1000)}]",
        "truncated": True,
        "total_chars": len(s),
    }


async def compact_live_conversation(db: Database, conv: dict) -> dict:
    """Compacta un hilo ABIERTO sin cerrarlo (2026-07-22).

    Destila el historial a un resumen y lo REEMPLAZA como historial: la
    conversación sigue siendo la misma (misma rama, mismo hilo de
    Discord, mismo id), pero el próximo run arranca con ~1k tokens en
    vez de arrastrar 80-100k. Es la alternativa a `cerrar` + `nuevo`
    cuando el tema no cambió y solo molesta el peso del contexto.

    Los hechos destilados se guardan (valen igual), pero NO se toca
    `conversations.summary` ni el índice FTS5: esos son el artefacto del
    cierre, y escribirlos acá haría que el `close` posterior se saltee
    la compactación final (guard de idempotencia).

    Devuelve {ok, before, after, summary, facts} — el caller reporta.
    """
    conv_id = conv["id"]
    raw = conv.get("messages_json") or ""
    before = await experts.context_usage_db(db, raw)
    if conv_id in _COMPACTING:
        return {"ok": False, "error": "ya hay una compactación en curso"}
    _COMPACTING.add(conv_id)
    try:
        existing = await db.list_facts(conv["project_slug"], limit=100)
        result = await memory.compact_conversation(raw, existing_facts=existing)
        if result is None or not result.summary.strip():
            return {"ok": False, "error": "el compactador no devolvió resumen"}
        n_facts = await db.add_facts(
            conv["project_slug"], result.facts, source_conversation=conv_id)
        shown = {f["id"] for f in existing}
        obsolete = [i for i in result.obsolete_fact_ids if i in shown]
        if obsolete:
            await db.supersede_facts(
                obsolete, conv["project_slug"], superseded_by=conv_id)
        # `previous_json` (2026-08-15): la compactación conserva el último
        # turno además del resumen. Sin eso, el turno siguiente arrancaba
        # con la prosa y cero working set — ver la nota en
        # `build_compacted_history`.
        new_json = memory.build_compacted_history(
            result.summary, facts=result.facts, previous_json=raw)
        await db.save_conversation_messages(conv_id, new_json, expected=raw)
        logger.info(
            "compactación en vivo conv=%s: %d → %d chars de historial "
            "(%s → sin usage todavía), facts=%d",
            conv_id[:8], len(raw), len(new_json),
            f"{before['base_tokens']} tok" if before else "sin medir", n_facts)
        return {"ok": True, "before": before, "summary": result.summary,
                "facts": n_facts,
                "chars_before": len(raw), "chars_after": len(new_json)}
    except Exception as e:  # noqa: BLE001 — el hilo queda intacto si falla
        logger.exception("compactación en vivo de conv=%s falló", conv_id[:8])
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        _COMPACTING.discard(conv_id)


@_require_auth
@coordination.guard_workspace(DB_KEY, source="conversation")
async def conversations_compact(request: web.Request) -> web.Response:
    """POST /conversations/{id}/compact — baja el contexto sin cerrar.

    Corre inline (el compactador tarda decenas de segundos y el caller
    quiere el número): si el cliente corta antes, la compactación
    termina igual del lado del relay.

    Returns:
        200 -> {id, status, context_before, chars_before, chars_after, summary}
        404 -> no existe
        409 -> cerrada, sin historial, o con un run en curso
        502 -> el compactador falló (el hilo queda intacto)
    """
    db: Database = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response({"error": "no existe"}, status=404)
    if conv.get("status") != "open":
        return web.json_response(
            {"error": "la conversación está cerrada"}, status=409)
    if not (conv.get("messages_json") or "").strip():
        return web.json_response(
            {"error": "el hilo todavía no tiene historial que compactar"},
            status=409)
    # Un run vivo reescribe messages_json al terminar: compactar ahora
    # sería trabajo tirado (y el humano vería el contexto igual de alto).
    runs = await db.list_chats(status="running", limit=50)
    if any(c.get("conversation_id") == conv_id for c in runs):
        return web.json_response(
            {"error": "hay un run en curso en este hilo; espera a que "
                      "termine (o cancelalo) y compacta después"},
            status=409)
    out = await compact_live_conversation(db, conv)
    if not out["ok"]:
        return web.json_response({"error": out["error"]}, status=502)
    return web.json_response({
        "id": conv_id, "status": "open",
        "context_before": out["before"],
        "chars_before": out["chars_before"],
        "chars_after": out["chars_after"],
        "facts": out["facts"],
        "summary": out["summary"],
    })


@_require_auth
@coordination.guard_workspace(DB_KEY, source="conversation")
async def conversations_close(request: web.Request) -> web.Response:
    """POST /conversations/{id}/close — cierra + compacta (comando /cerrar).

    Idempotente: cerrar una cerrada re-dispara la compactación si no
    tiene summary todavía (reintento manual del compactador).

    El PR a develop se abre EN BACKGROUND (`pr: "running"`): incluye
    verify (build+test, hasta 420s) y la redacción del body con el LLM,
    y eso no entra en un request HTTP — la UI cortaba a los 30s y
    mostraba "no pude cerrar" mientras el PR se abría igual (bug
    2026-07-21). El estado se consulta con GET /conversations/{id}/pr.
    El PR develop→main lo hace el usuario a mano.

    Returns:
        200 -> {id, status: "closed", compaction, pr, pr_url?}
        404 -> no existe
    """
    db: Database = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response({"error": "no existe"}, status=404)
    was_open = conv.get("status") == "open"
    await db.close_conversation(conv_id)

    # PR a develop (solo la primera vez que se cierra y si hubo rama).
    pr_url = conv.get("pr_url")
    project = await db.get_project(conv["project_slug"])
    branch = conv.get("branch")
    pr_state = "skipped"
    if was_open and branch and project and project.get("repo_path") and not pr_url:
        _spawn_bg(_finalize_pr_bg(db, conv_id, conv["project_slug"],
                                  project, branch, conv.get("summary") or "",
                                  issue_number=conv.get("issue_number")))
        pr_state = "running"

    compaction = "skipped"
    if conv.get("messages_json") and not (conv.get("summary") or "").strip():
        _spawn_bg(_compact_and_store(
            db, conv_id, conv["project_slug"], conv["messages_json"]),
            hold_workspace=False)
        compaction = "running"
    logger.info("conversación cerrada id=%s compaction=%s branch=%s pr=%s",
                conv_id[:8], compaction, branch, pr_url or pr_state)
    resp: dict = {"id": conv_id, "status": "closed", "compaction": compaction,
                  "pr": pr_state}
    if pr_url:  # ya tenía PR de un /cerrar anterior
        resp["pr_url"] = pr_url
    return web.json_response(resp)


@_require_auth
async def conversation_pr_status(request: web.Request) -> web.Response:
    """GET /conversations/{id}/pr — estado del PR lanzado por /close.

    La UI poll-ea esto mientras corre el verify. `state`:
    verifying|describing|opening|done|error, o `unknown` si el job no
    está en memoria (relay reiniciado, o /close viejo): en ese caso
    igual devolvemos el `pr_url` persistido si ya existe.

    Returns:
        200 -> {state, pr_url?, error?, draft, committed, verify?}
        404 -> la conversación no existe
    """
    db: Database = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    job = _PR_JOBS.get(conv_id)
    if job is not None:
        return web.json_response({"id": conv_id, **job})
    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response({"error": "no existe"}, status=404)
    pr_url = conv.get("pr_url")
    return web.json_response({
        "id": conv_id, "state": "done" if pr_url else "unknown",
        "pr_url": pr_url, "error": None, "draft": False, "committed": False,
    })


@_require_auth
async def conversation_branch_status(request: web.Request) -> web.Response:
    """GET /conversations/{id}/branch — estado de la rama local de la conv.

    La UI del panel de chat usa esto para decidir si el botón "borrar
    rama local" es seguro (merged=True, solo se ve merged en develop) o
    destructivo (merged=False, hay commits sin mergear). Devolver
    `ahead`/`behind`/`is_current` le permite al confirm del modal
    decirle al humano exactamente qué se va a perder si insiste.

    Si la conversación no tiene rama (no era un repo git en el /nuevo,
    o el /nuevo falló antes de crear la rama), devuelve 200 con
    `{branch: null, exists: false, ...}`: la UI muestra el botón
    deshabilitado o lo esconde.

    Returns:
        200 -> {id, branch, base, exists, merged, ahead, behind,
                is_current, current_branch, error?}
        404 -> conv desconocida
    """
    db: Database = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response(
            {"error": f"conversación {conv_id!r} desconocida"}, status=404)
    branch = conv.get("branch")
    if not branch:
        # Conv sin rama (no era repo git al crearse, o el relay no creó
        # una). Devolvemos un payload neutral para que la UI oculte el botón.
        return web.json_response({
            "id": conv_id, "branch": None, "base": None,
            "exists": False, "merged": False, "ahead": 0, "behind": 0,
            "is_current": False, "current_branch": "", "error": None,
        })
    project = await db.get_project(conv["project_slug"])
    repo_path = (project or {}).get("repo_path") or ""
    if not repo_path:
        return web.json_response({
            "id": conv_id, "branch": branch, "base": None,
            "exists": False, "merged": False, "ahead": 0, "behind": 0,
            "is_current": False, "current_branch": "",
            "error": "proyecto sin repo_path",
        })
    # Base = develop si existe local/remoto, si no el trunk. Misma
    # lógica que el resto del flujo de git.
    base = await git_flow.work_base_branch(repo_path)
    status = await git_flow.branch_status(repo_path, branch, base=base)
    return web.json_response({"id": conv_id, **status})


@_require_auth
@coordination.guard_workspace(DB_KEY, source="conversation")
async def conversation_branch_delete(request: web.Request) -> web.Response:
    """DELETE /conversations/{id}/branch — borra la rama LOCAL de la conv.

    El endpoint existe porque cada /cerrar deja la rama
    `<autor>-<YYYY-MM-DD>` en el disco del repo, y eso se va
    acumulando. La rama remota la maneja el humano por GitHub (merge
    de develop→main, delete branch on merge, etc.); acá solo
    limpiamos la copia local.

    Reglas:
      1. La conv tiene que estar `closed`. Una conv abierta = trabajo
         en curso, no se toca.
      2. Si la rama es la actualmente checked-out, rechaza: borrar
         bajo tus pies te deja en detached HEAD. Devuelve 409 con el
         nombre de la rama actual para que el humano haga checkout.
      3. `?force=true` permite borrar aunque no esté mergeada a base
         (destructivo). Sin force usa `git branch -d` que falla si
         hay commits sin mergear — eso es el "safe path".
      4. NO toca el remoto. Ni `git push --delete`, ni `gh api`.

    Body: ninguno (todo en query string).

    Returns:
        200 -> {id, branch, deleted, was_current, error}
        404 -> conv desconocida
        409 -> conv abierta, o la rama es la actual del checkout
        422 -> git falló (`error` trae el detalle)
    """
    db: Database = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response(
            {"error": f"conversación {conv_id!r} desconocida"}, status=404)
    if conv.get("status") == "open":
        return web.json_response(
            {"error": "conversación abierta; cerrala antes de borrar la rama",
             "status": "open"},
            status=409)
    branch = conv.get("branch")
    if not branch:
        return web.json_response(
            {"error": "esta conversación no tiene rama"}, status=422)
    project = await db.get_project(conv["project_slug"])
    repo_path = (project or {}).get("repo_path") or ""
    if not repo_path:
        return web.json_response(
            {"error": "proyecto sin repo_path"}, status=422)
    force = request.query.get("force", "").lower() in ("1", "true", "yes")
    result = await git_flow.delete_local_branch(repo_path, branch, force=force)
    if result.get("was_current"):
        return web.json_response(
            {"error": result["error"], "branch": branch,
             "current_branch": result.get("current_branch", branch)},
            status=409)
    if not result.get("deleted"):
        return web.json_response(
            {"error": result.get("error") or "git no pudo borrar la rama",
             "branch": branch, "repo_path": repo_path},
            status=422)
    logger.info("rama local borrada conv=%s branch=%s force=%s",
                conv_id[:8], branch, force)
    return web.json_response({
        "id": conv_id, "branch": branch, "deleted": True,
        "was_current": False, "error": None,
    })


@_require_auth
async def conversation_diff(request: web.Request) -> web.Response:
    """GET /conversations/{id}/diff — qué cambió en la rama de la conv.

    Git puro, sin LLM: `git diff base...branch` (los commits que van al
    PR) + lo que quedó sin commitear en el working tree. El `git-diff`
    por proyecto que ya existía solo mira el tree, así que en cuanto el
    experto commitea deja de mostrar nada — este es el que responde
    "¿qué me cambió el experto en esta conversación?".

    Tres vistas sobre lo mismo (2026-08-24, visor navegable):

      - sin query params: el diff ENTERO en dos bloques (compat: es lo
        que consume el `/diff` del bot y los tests viejos).
      - `?view=files`: LISTA de archivos con contadores, sin texto. Es
        lo primero que pide el visor — `--numstat` no lo corta el cap,
        así que la lista está completa aunque el diff pese megas.
      - `?path=<archivo>`: el diff de ESE archivo. Uno por vez, así el
        browser no pinta 5MB de una.

    Query:
        cap:     chars máximos por diff (default 200k, tope 1M).
        view:    `files` para la lista de archivos.
        path:    archivo puntual (relativo al repo).
        mode:    rango — `all` (default, merge-base→working tree, lo que
                 va a quedar en el PR), `committed`, `pending`.
        context: líneas de contexto del diff de un archivo (default 3).

    Returns:
        200 -> {id, branch, base, exists, is_current, commits, stat, diff,
                full_size, truncated, pending_*, untracked, error?}
        200 (view=files) -> {id, pr_url, files, totals, remote_ahead, …}
        200 (path=…) -> {id, path, diff, truncated, binary, untracked, …}
        404 -> conv desconocida
        422 -> conv sin rama, o proyecto sin repo_path
    """
    db: Database = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response(
            {"error": f"conversación {conv_id!r} desconocida"}, status=404)
    branch = conv.get("branch")
    if not branch:
        return web.json_response(
            {"error": "esta conversación no tiene rama de trabajo"}, status=422)
    project = await db.get_project(conv["project_slug"])
    repo_path = (project or {}).get("repo_path") or ""
    if not repo_path:
        return web.json_response(
            {"error": "proyecto sin repo_path"}, status=422)
    try:
        cap = min(max(int(request.query.get("cap", git_flow.DIFF_CAP)), 1_000),
                  1_000_000)
    except ValueError:
        cap = git_flow.DIFF_CAP
    mode = (request.query.get("mode") or "all").strip().lower()
    path = (request.query.get("path") or "").strip()
    if path:
        try:
            context = int(request.query.get("context", 3))
        except ValueError:
            context = 3
        out = await git_flow.diff_file(
            repo_path, branch, path, mode=mode, cap=cap, context=context,
            old_path=(request.query.get("old") or "").strip())
        return web.json_response({"id": conv_id, "branch": branch, **out})
    if (request.query.get("view") or "").strip().lower() == "files":
        out = await git_flow.diff_file_list(repo_path, branch, mode=mode)
        return web.json_response({"id": conv_id, "pr_url": conv.get("pr_url"),
                                  **out})
    out = await git_flow.conversation_diff(repo_path, branch, cap=cap)
    return web.json_response({"id": conv_id, "pr_url": conv.get("pr_url"),
                              **out})


@_require_auth
@coordination.guard_workspace(DB_KEY, source="conversation")
async def conversation_git_action(request: web.Request) -> web.Response:
    """POST /conversations/{id}/git/{action} — git de la rama del hilo.

    Las acciones que un dev hace después de mirar el diff. Las corre el
    RELAY, no el experto (mismo criterio que /nuevo y /cerrar: el git es
    determinista o no es), y con los guards del flujo de ramas puestos en
    `git_flow`, no acá — así valen igual si mañana los llama el bot.

      commit     {message, paths?}  — paths vacío = todo (`git add -A`)
      push       {}                 — `git push -u origin <rama>`
      pr         {title?, body?}    — push + PR a develop SIN cerrar el hilo
      merge      {method?}          — `gh pr merge` del PR, solo si va a develop
      sync-base  {}                 — fetch + fast-forward de la base
      restore    {paths}            — descarta cambios sin commitear (destructivo)

    `merge` y `restore` son las dos destructivas y la UI las pone detrás
    de un confirm; el guard del server es el que importa igual:
    `merge_pr` rechaza cualquier PR que no apunte a develop (main/master
    no se mergean desde un botón) y `restore_paths` exige que HEAD esté
    en la rama del hilo.

    Returns:
        200 -> payload de la acción (`error: null`)
        400 -> acción desconocida o body inválido
        404 -> conv desconocida
        422 -> conv sin rama / sin repo_path, o git falló (`error` explica)
    """
    db: Database = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    accion = request.match_info["action"]
    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response(
            {"error": f"conversación {conv_id!r} desconocida"}, status=404)
    branch = conv.get("branch")
    if not branch:
        return web.json_response(
            {"error": "esta conversación no tiene rama de trabajo"}, status=422)
    project = await db.get_project(conv["project_slug"])
    repo_path = (project or {}).get("repo_path") or ""
    if not repo_path:
        return web.json_response({"error": "proyecto sin repo_path"}, status=422)
    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    if not isinstance(body, dict):
        return web.json_response({"error": "body debe ser un objeto"}, status=400)

    if accion == "commit":
        out = await git_flow.commit_paths(
            repo_path, branch, message=str(body.get("message") or ""),
            paths=body.get("paths"))
    elif accion == "push":
        out = await git_flow.push_branch(repo_path, branch)
    elif accion == "pr":
        titulo = str(body.get("title") or "").strip() or f"{branch}: cambios del hilo"
        out = await git_flow.open_pr(repo_path, branch, title=titulo,
                                     body=str(body.get("body") or ""))
        if out.get("pr_url"):
            await db.set_conversation_pr(conv_id, out["pr_url"])
    elif accion == "merge":
        out = await git_flow.merge_pr(
            repo_path, branch, method=str(body.get("method") or "squash"),
            delete_branch=bool(body.get("delete_branch", True)))
    elif accion == "sync-base":
        out = await git_flow.sync_base(repo_path)
    elif accion == "restore":
        out = await git_flow.restore_paths(repo_path, branch,
                                           body.get("paths") or [])
    else:
        return web.json_response(
            {"error": f"acción git desconocida: {accion!r}",
             "acciones": ["commit", "push", "pr", "merge", "sync-base",
                          "restore"]}, status=400)
    payload = {"id": conv_id, "branch": branch, "action": accion, **out}
    if out.get("error"):
        logger.info("git action %s falló conv=%s: %s", accion, conv_id[:8],
                    out["error"])
        return web.json_response(payload, status=422)
    logger.info("git action %s ok conv=%s branch=%s", accion, conv_id[:8], branch)
    return web.json_response(payload)


@_require_auth
async def conversation_set_discord_user(request: web.Request) -> web.Response:
    """POST /conversations/{id}/set-discord-user — vincula un chat de UI
    con un Discord user para el bridge bidireccional (iter 10.0 + 10.4).

    Caso de uso: el humano inicia un chat en la UI. Después quiere
    contestarlo desde Discord (porque está en el celu). El relay:
    1. Guarda discord_user_id en la conversación.
    2. Si `create_thread: true`, pide al bot crear un DM thread con el
       usuario y guarda discord_thread_id.
    A partir de ahí, las respuestas del experto van al DM del usuario,
    y si el usuario responde en Discord, el bot llama /experts/run con
    discord_thread_id y el relay auto-attacha al mismo hilo.

    Body:
        discord_user_id:  str|null (null = desvincular)
        discord_author:   str (opcional, display name)
        create_thread:    bool (opcional, default false) — si true,
                          pide al bot crear un DM thread para seguir
                          la conversación desde Discord.

    Si `discord_user_id` es null/ausente Y no se manda discord_author,
    se interpreta como DESVINCULAR: ambos campos quedan NULL y la
    conversación deja de tener bridge con Discord.

    **Excepción** (2026-08-17, botón "seguir en el celu"): un body que
    pide `create_thread: true` SIN user_id no es una desvinculación —
    es "mandame el hilo al Discord de siempre". Ahí se usa el default de
    `FOURBIS_DEFAULT_DISCORD_USER`, y si no hay ninguno configurado se
    devuelve 400 diciendo qué falta. Es la diferencia entre un click y
    abrir un modal a copiar un id de 18 dígitos.

    Returns:
        200 -> {id, discord_user_id, discord_author, discord_thread_id,
                cleared?}
        400 -> tipo inválido, o ambos campos vacíos sin intención clara
        404 -> conv desconocida
        502 -> se pidió hilo y el bot no pudo crearlo (no se vincula nada)
    """
    db: Database = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    discord_user_id = body.get("discord_user_id")
    discord_author = body.get("discord_author")
    create_thread = body.get("create_thread", False)
    if create_thread and not (discord_user_id or "").strip():
        # "Seguir en el celu": un click, sin modal. Si la conversación ya
        # está vinculada reusamos su user; si no, el default configurado.
        conv_previa = await db.get_conversation(conv_id)
        heredado = (conv_previa or {}).get("discord_user_id") or ""
        auto_id, auto_author = relay_config.default_discord_user()
        discord_user_id = heredado.strip() or auto_id
        if not discord_user_id:
            return web.json_response({
                "error": "no hay un Discord por default configurado. "
                         "Configurá FOURBIS_DEFAULT_DISCORD_USER (o pasá "
                         "discord_user_id explícito).",
                "needs_user_id": True,
            }, status=400)
        if not (discord_author or "").strip():
            discord_author = (
                (conv_previa or {}).get("discord_author") or auto_author or None)
    # Iter 10.0: signal de clear. null explícito en discord_user_id sin
    # author → desvincula. Cualquier string vacío o no-string → 400.
    if discord_user_id is None and not discord_author:
        conv = await db.get_conversation(conv_id)
        if conv is None:
            return web.json_response(
                {"error": f"conversación {conv_id!r} desconocida"}, status=404)
        await db.set_conversation_discord_user(
            conv_id, discord_user_id=None, clear=True)
        # También limpiar discord_thread_id al desvincular
        await db.run(
            "UPDATE conversations SET discord_thread_id=NULL WHERE id=?",
            (conv_id,))
        return web.json_response({
            "id": conv_id,
            "discord_user_id": None,
            "discord_author": None,
            "discord_thread_id": None,
            "cleared": True,
        })
    if not isinstance(discord_user_id, str) or not discord_user_id.strip():
        return web.json_response(
            {"error": "discord_user_id requerido (string no vacío) o "
                      "null para desvincular"},
            status=400)
    if discord_author is not None and not isinstance(discord_author, str):
        return web.json_response(
            {"error": "discord_author debe ser string"}, status=400)
    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response(
            {"error": f"conversación {conv_id!r} desconocida"}, status=404)
    # Iter 10.4: si el caller pide thread, pedir al bot que cree un DM.
    # El pedido va ANTES de escribir el vínculo (2026-08-16). Al revés
    # dejaba vínculos fantasma: con el bot caído la conv quedaba marcada
    # como vinculada, la UI escondía el botón de vincular y mostraba
    # "Discord · @vos", pero el DM no existía y el error se iba en un
    # toast de tres segundos. Quien cerraba la notebook confiando en eso
    # no recibía nada.
    discord_thread_id = None
    if create_thread:
        discord_thread_id, thread_error = await _request_bot_create_thread(
            discord_user_id=discord_user_id.strip(),
            conversation_id=conv_id,
            project_slug=conv["project_slug"],
        )
        if not discord_thread_id:
            return web.json_response({
                "error": thread_error or "no se pudo crear el hilo DM",
                # La sonda va en el body para que la UI pueda ofrecer
                # "arrancar el bot" sin una segunda vuelta.
                "bot": await bot_control.probe(),
                "linked": False,
            }, status=502)

    await db.set_conversation_discord_user(
        conv_id, discord_user_id=discord_user_id.strip(),
        discord_author=discord_author.strip() if discord_author else None)
    if discord_thread_id:
        await db.run(
            "UPDATE conversations SET discord_thread_id=? WHERE id=?",
            (discord_thread_id, conv_id))

    resp = {
        "id": conv_id,
        "discord_user_id": discord_user_id,
        "discord_author": discord_author,
    }
    if discord_thread_id:
        resp["discord_thread_id"] = discord_thread_id
    return web.json_response(resp)


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


# ---------- voice input (Track D / F5 — docs/VOICE_INPUT.md) ----------


@_require_auth
async def voice_transcribe(request: web.Request) -> web.Response:
    """POST /voice/transcribe — audio (multipart) → STT MiniMax → KB.

    Campos multipart: audio (file, requerido), author, mode ("vc"|"cli"),
    discord_channel, duration_s (requeridos); participants (CSV), topic,
    related_project (opcionales).

    Returns:
        201 -> {id, transcript, duration_s, stt}
        400 -> falta campo / formato de audio desconocido
        413 -> audio > VOICE_MAX_AUDIO_BYTES (50 MB default)
        502 -> el ASR de MiniMax falló
        504 -> timeout del ASR (MINIMAX_STT_TIMEOUT, default 90s)
    """
    cap = voice.max_audio_bytes()
    try:
        reader = await request.multipart()
    except (ValueError, AssertionError):
        return web.json_response(
            {"error": "se espera multipart/form-data"}, status=400)

    fields: dict[str, str] = {}
    audio_bytes: Optional[bytes] = None
    audio_name = ""
    async for part in reader:
        if part.name == "audio":
            buf = bytearray()
            while True:
                chunk = await part.read_chunk(64 * 1024)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > cap:
                    return web.json_response(
                        {"error": f"audio > {cap} bytes"}, status=413)
            audio_bytes = bytes(buf)
            audio_name = part.filename or ""
        elif part.name:
            fields[part.name] = (await part.text()).strip()

    missing = [f for f in ("author", "mode", "discord_channel", "duration_s")
               if not fields.get(f)]
    if audio_bytes is None or not audio_bytes:
        missing.insert(0, "audio")
    if missing:
        return web.json_response(
            {"error": f"faltan campos requeridos: {', '.join(missing)}"},
            status=400)
    mode = fields["mode"]
    if mode not in ("vc", "cli"):
        return web.json_response(
            {"error": 'mode debe ser "vc" o "cli"'}, status=400)
    try:
        duration_s = float(fields["duration_s"])
    except ValueError:
        return web.json_response(
            {"error": "duration_s debe ser numérico"}, status=400)
    # Bug fix 2026-07-18: float() acepta "nan" e "inf" sin chistar
    # (ValueError solo en strings no-numéricos). Sin este check, un
    # payload basura (NaN/Inf) se persiste tal cual al KB y rompe la
    # UI de voice (que asume un número finito para formatear mm:ss).
    # math.isfinite también filtra NaN.
    if not math.isfinite(duration_s) or duration_s < 0:
        return web.json_response(
            {"error": "duration_s debe ser un número finito >= 0"},
            status=400)
    # Tope defensivo: 4h. Un audio de voz más largo es un abuse vector
    # o un cliente roto. Ajustar si alguna vez hay un caso real.
    if duration_s > 4 * 3600:
        return web.json_response(
            {"error": "duration_s demasiado grande (max 4h)"},
            status=400)
    ext = Path(audio_name).suffix.lower()
    if ext not in voice.AUDIO_EXTS:
        return web.json_response(
            {"error": f"formato de audio no soportado: {ext or '(sin ext)'}"
                      f" — esperado {sorted(voice.AUDIO_EXTS)}"},
            status=400)

    trx_id = voice.new_trx_id(audio_bytes)
    adir = voice.audio_dir()
    await asyncio.to_thread(adir.mkdir, parents=True, exist_ok=True)
    audio_path = adir / f"{trx_id}{ext}"
    await asyncio.to_thread(audio_path.write_bytes, audio_bytes)

    try:
        stt = await voice.transcribe_minimax(audio_path)
    except asyncio.TimeoutError:
        return web.json_response(
            {"error": "STT timeout", "id": trx_id}, status=504)
    except RuntimeError as e:
        logger.warning("voice: ASR falló trx=%s: %s", trx_id, e)
        return web.json_response(
            {"error": "STT failed", "detail": str(e)[:300], "id": trx_id},
            status=502)

    transcript = stt.pop("text")
    participants = [p.strip() for p in
                    (fields.get("participants") or "").split(",") if p.strip()]
    record = {
        "id": trx_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "author": fields["author"],
        "discord_channel": fields["discord_channel"],
        "mode": mode,
        "audio": {"path": str(audio_path).replace("\\", "/"),
                  "duration_s": duration_s, "size": len(audio_bytes)},
        "stt": stt,
        "transcript": transcript,
        "participants": participants or [fields["author"]],
        "topic": fields.get("topic") or None,
        "related_project": fields.get("related_project") or None,
        "processed": None,
    }
    await voice.persist_transcript(record)
    logger.info("voice: transcript %s persistido (%s, %.0fs, %d chars)",
                trx_id, mode, duration_s, len(transcript))
    return web.json_response(
        {"id": trx_id, "transcript": transcript,
         "duration_s": duration_s, "stt": stt},
        status=201)


@_require_auth
async def voice_transcripts_list(request: web.Request) -> web.Response:
    """GET /voice/transcripts — índice liviano (debug / futura UI)."""
    limit = min(int(request.query.get("limit", "50")), 200)
    return web.json_response(
        {"transcripts": await voice.list_transcripts(limit)})


@_require_auth
async def voice_transcripts_get(request: web.Request) -> web.Response:
    """GET /voice/transcripts/{id} — el JSONL completo (debug)."""
    rec = await voice.read_transcript(request.match_info["id"])
    if rec is None:
        return web.json_response({"error": "no existe"}, status=404)
    return web.json_response(rec)


# Locks por trx_id: pitfall 5 del spec — dos /process casi simultáneos
# sobre el mismo transcript no deben correr el experto dos veces; el
# segundo espera y ve el `processed` que dejó el primero.
_voice_process_locks: dict[str, asyncio.Lock] = {}


@_require_auth
async def voice_transcript_process(request: web.Request) -> web.Response:
    """POST /voice/transcripts/{id}/process — follow-up opcional del spec
    (VOICE_INPUT.md): corre el transcript por el experto del proyecto
    (`related_project` o el proyecto 'general' si existe; si no, un
    experto sintético sin proyecto — para resumir no hacen falta tools)
    y persiste `processed={summary, model, ts}` en el JSONL.

    Idempotente: si ya está procesado devuelve el summary existente con
    `already_processed: true` (el bot lo traduce a "ya está procesado").

    Body JSON opcional: {"context": "texto extra para el prompt"}.

    Returns:
        200 -> {id, summary, model, already_processed}
        400 -> transcript vacío
        404 -> trx_id desconocido
        502 -> el experto falló
        503 -> modelo no disponible
        504 -> timeout del experto
    """
    db: Database = request.app[DB_KEY]
    trx_id = request.match_info["id"]
    lock = _voice_process_locks.setdefault(trx_id, asyncio.Lock())
    async with lock:
        rec = await voice.read_transcript(trx_id)
        if rec is None:
            return web.json_response({"error": "no existe"}, status=404)
        prev = rec.get("processed")
        if prev:
            return web.json_response(
                {"id": trx_id, "summary": prev.get("summary", ""),
                 "model": prev.get("model"), "already_processed": True})
        transcript = (rec.get("transcript") or "").strip()
        if not transcript:
            return web.json_response(
                {"error": "transcript vacío: nada que procesar"}, status=400)
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        context = str((body or {}).get("context") or "").strip()

        project = await db.get_project(rec.get("related_project") or "general")
        if project is None:
            # Sin proyecto en DB: experto sintético mínimo (mismo patrón
            # que el vetting de ADR-033).
            project = {"slug": "voice-process", "repo_path": os.getcwd(),
                       "defaults_json": {}, "mcp_servers": [],
                       "native_tools": []}
        prompt = (
            "Resume este transcript de mi sesión de voz y extrae las "
            "acciones concretas. Si no hay acciones, lista los puntos "
            "clave.\n\n"
            + (f"Contexto extra: {context}\n\n" if context else "")
            + f"--- TRANSCRIPT ---\n{transcript}")
        try:
            # db=self.db: bug fix 2026-07-18 — antes voice no pasaba db
            # y caía al blob legacy; ahora con db, run_expert resuelve
            # el timeout en cascada desde system_config (lo mismo que
            # night + chat). Si el project es el sintético (no tiene
            # id), run_expert salta el catálogo y corre sin tools
            # (no las necesita para resumir).
            result = await experts.run_expert(project, prompt, db=db)
        except experts.ModelUnavailable as e:
            return web.json_response({"error": str(e)}, status=503)
        except asyncio.TimeoutError:
            return web.json_response(
                {"error": "timeout del experto"}, status=504)
        except Exception as e:  # noqa: BLE001 — el KB no debe romperse
            logger.exception("voice: process %s falló", trx_id)
            return web.json_response(
                {"error": f"experto falló: {type(e).__name__}: {str(e)[:200]}"},
                status=502)
        summary = (result.get("content") or "").strip()
        rec["processed"] = {
            "summary": summary, "model": result.get("model"),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        await voice.persist_transcript(rec)
        logger.info("voice: transcript %s procesado (summary %d chars)",
                    trx_id, len(summary))
        return web.json_response(
            {"id": trx_id, "summary": summary,
             "model": result.get("model"), "already_processed": False})


# ---------- modo nocturno (ADR-028: pipeline de dos fases) ----------


@_require_auth
@coordination.guard_workspace(DB_KEY)
async def night_start(request: web.Request) -> web.Response:
    """POST /night-mode/start — arranca un night run para un proyecto.

    Body:
        project:      str  (requerido, slug)
        deadline_iso: str  (opcional; default próximas 7am local)
        directive:    str  (opcional; semilla de la Fase 1)
        error_logs:   str  (opcional; input alternativo de la Fase 1)

    Returns:
        202 -> {run_id, project, started_at, deadline_at}
        400 -> night_mode_enabled=0 / body o deadline malformados
        404 -> proyecto desconocido
        409 -> ya hay un run activo para el proyecto
    """
    from datetime import datetime as _dt

    from . import night as night_mod

    db: Database = request.app[DB_KEY]
    registry: dict = request.app[NIGHT_KEY]
    notify: NotifyClient = request.app[NOTIFY_KEY]

    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)

    slug = body.get("project")
    if not isinstance(slug, str) or not slug.strip():
        return web.json_response({"error": "project requerido (string)"},
                                 status=400)
    project = await db.get_project(slug.strip())
    if project is None or not project["enabled"]:
        return web.json_response(
            {"error": f"proyecto {slug!r} desconocido o deshabilitado"},
            status=404)
    if not project.get("night_mode_enabled"):
        return web.json_response(
            {"error": f"night_mode_enabled=0 en {project['slug']!r}: "
                      "activalo antes (PATCH del proyecto)"},
            status=400)

    # Lock: 1 run activo por proyecto (memoria + DB por si el relay reinició).
    for orch, task in registry.values():
        if (orch.project["slug"].lower() == project["slug"].lower()
                and not task.done()):
            return web.json_response(
                {"error": "ya hay un night run activo para este proyecto",
                 "run_id": orch.run_id}, status=409)
    stale = await db.active_night_run(project["slug"])
    if stale and stale["id"] not in registry:
        # Fila colgada de un run que murió con el proceso: cerrarla para
        # no bloquear runs nuevos para siempre.
        await db.finish_night_run(
            stale["id"], end_reason="crashed",
            error="proceso del relay reiniciado con el run activo")

    # Plantilla (2026-08-27): `template` reemplaza a `directive` cuando
    # esta viene vacia. No al reves — una directiva explicita SIEMPRE
    # gana, porque el caso "arranque desde la plantilla pero con un
    # retoque" se resuelve editando el texto en el composer, y si la
    # plantilla pisara eso el retoque se perderia sin aviso.
    directiva = (body.get("directive") or "").strip()
    tpl_nombre = (body.get("template") or "").strip()
    if tpl_nombre and not directiva:
        tpl = await db.get_night_template(tpl_nombre, project["slug"])
        if tpl is None:
            return web.json_response(
                {"error": f"no hay plantilla {tpl_nombre!r} para "
                          f"{project['slug']!r} ni global"}, status=404)
        directiva = tpl["directiva"]

    deadline_iso = body.get("deadline_iso") or ""
    if deadline_iso:
        try:
            deadline = _dt.fromisoformat(deadline_iso)
            if deadline.tzinfo is None:
                deadline = deadline.astimezone()
        except ValueError:
            return web.json_response(
                {"error": f"deadline_iso inválido: {deadline_iso!r}"},
                status=400)
    else:
        deadline = night_mod.default_deadline()

    state_dir = Path(os.environ.get("STATE_DIR", "./state"))
    orch = night_mod.NightOrchestrator(
        db=db, project=project, deadline=deadline,
        directive=directiva,
        error_logs=body.get("error_logs") or "",
        notify=notify, state_dir=state_dir)
    task = asyncio.create_task(orch.run())
    coordination.hold_current(task)
    registry[orch.run_id] = (orch, task)

    return web.json_response(
        {"run_id": orch.run_id, "project": project["slug"],
         "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "deadline_at": deadline.isoformat(),
         # Iter 9.7: el cliente sabe si el run es interactivo.
         "interactive": bool(project.get("interactive_mode")),
         "questions_endpoint":
             f"/admin/api/night/questions?run_id={orch.run_id}"},
        status=202)


@_require_auth
async def night_stop(request: web.Request) -> web.Response:
    """POST /night-mode/stop {run_id} — para el loop después de la tarea
    actual. Idempotente: parar un run ya parado devuelve 200 igual."""
    registry: dict = request.app[NIGHT_KEY]
    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    run_id = body.get("run_id") or ""
    entry = registry.get(run_id)
    if entry is None:
        return web.json_response(
            {"error": f"run {run_id!r} desconocido (o el relay reinició)"},
            status=404)
    orch, _task = entry
    orch.stop()
    return web.json_response({"run_id": run_id, "status": orch.status})


@_require_auth
async def night_status(request: web.Request) -> web.Response:
    """GET /night-mode/status?run_id=... — snapshot del run (memoria) o
    la fila night_runs si el run ya no está en memoria.
    Sin run_id: {"active": [snapshots de los runs vivos]}."""
    db: Database = request.app[DB_KEY]
    registry: dict = request.app[NIGHT_KEY]
    run_id = request.query.get("run_id", "")
    if not run_id:
        # Sin run_id: snapshot de todos los runs vivos (puede ser []).
        return web.json_response(
            {"active": [orch.snapshot() for orch, _task in registry.values()]})
    entry = registry.get(run_id)
    if entry is not None:
        return web.json_response(entry[0].snapshot())
    row = await db.get_night_run(run_id)
    if row is None:
        return web.json_response({"error": f"run {run_id!r} desconocido"},
                                 status=404)
    return web.json_response(row)


# ---- 2026-08-16: conexiones SQL que el chat puede consultar ----
#
# El DSN se guarda acá y el experto usa un alias, así la contraseña no
# entra al historial del hilo. `GET` nunca devuelve el DSN entero.


@_require_auth
async def db_connections_list(request: web.Request) -> web.Response:
    """GET /admin/api/db-connections — sin credenciales.

    `relay` siempre viene en la lista, tenga fila o no: es la conexión
    que el experto puede usar sin que nadie le registre nada, así que la
    UI necesita mostrarla para poder darle permiso de escritura.
    """
    from . import dbtool
    db = request.app[DB_KEY]
    filas = await db.list_db_connections()
    propia = next((f for f in filas if f["alias"] == "relay"), None)
    reservada = {
        "alias": "relay",
        "dsn": dbtool.redactar(str(getattr(db, "path", ""))),
        "motor": "sqlite",
        "descripcion": (propia or {}).get("descripcion")
                       or "la base del propio relay (chats, tokens, runs)",
        "escribir": bool((propia or {}).get("escribir")),
        "reservada": True,
    }
    return web.json_response({"connections": [reservada] + [
        {"alias": f["alias"], "dsn": dbtool.redactar(f["dsn"]),
         "motor": dbtool.motor(f["dsn"]),
         "descripcion": f.get("descripcion") or "",
         "escribir": bool(f.get("escribir"))}
        for f in filas if f["alias"] != "relay"]})


@_require_auth
async def db_connection_upsert(request: web.Request) -> web.Response:
    """POST /admin/api/db-connections {alias, dsn, descripcion?, escribir?}"""
    from . import dbtool
    db = request.app[DB_KEY]
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    alias = (body.get("alias") or "").strip().lower()
    dsn = (body.get("dsn") or "").strip()
    if alias == "relay":
        # Reservado a medias, y la mitad importa: la RUTA se pisa con la
        # base real del relay (si el alias se pudiera re-apuntar,
        # `db_query("relay", …)` iría a otra base y el experto creería
        # estar mirando la suya). El PERMISO sí se guarda: habilitar la
        # escritura sobre su propia base es una decisión del humano
        # (2026-08-16, a pedido).
        dsn = str(getattr(db, "path", "")) or dsn
    if alias and not dsn:
        # Sin `dsn` sobre un alias que ya existe = "cambiame solo el
        # permiso". No es comodidad: el GET devuelve el DSN REDACTADO, así
        # que una UI que quisiera reenviarlo guardaría
        # `postgres://…@host/base` como cadena real y rompería la
        # conexión. La única forma segura de editar el permiso es no
        # tocar la cadena.
        previa = await db.get_db_connection(alias)
        if previa:
            dsn = previa["dsn"]
    if not alias or not dsn:
        return web.json_response(
            {"error": "alias y dsn son requeridos"}, status=400)
    fila = await db.upsert_db_connection(
        alias, dsn, descripcion=(body.get("descripcion") or "").strip(),
        escribir=bool(body.get("escribir")))
    return web.json_response({
        "ok": True, "alias": fila["alias"],
        "dsn": dbtool.redactar(fila["dsn"]),
        "motor": dbtool.motor(fila["dsn"]),
        "escribir": bool(fila["escribir"])})


@_require_auth
async def db_connection_delete(request: web.Request) -> web.Response:
    db = request.app[DB_KEY]
    alias = request.match_info["alias"]
    if not await db.delete_db_connection(alias):
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"ok": True, "alias": alias})


@_require_auth
async def db_connection_test(request: web.Request) -> web.Response:
    """POST /admin/api/db-connections/{alias}/test — ¿conecta?

    Existe porque el momento de descubrir que el DSN está mal es cuando
    lo cargás, no tres días después en medio de un run del experto.
    """
    from . import dbtool
    db = request.app[DB_KEY]
    alias = request.match_info["alias"]
    try:
        dsn, _ro = await dbtool.resolver(
            db, alias, relay_db_path=str(getattr(db, "path", "")))
    except dbtool.ConexionDesconocida as e:
        return web.json_response({"error": str(e)}, status=404)
    sonda = ("SELECT 1 AS ok" if dbtool.motor(dsn) == "postgres"
             else "SELECT 1 AS ok")
    out = await dbtool.consultar(dsn, sonda)
    ok = not out.startswith("error")
    return web.json_response({"ok": ok, "alias": alias,
                              "motor": dbtool.motor(dsn), "salida": out[:400]})


# ---- 2026-08-16: preguntas del experto al humano (chat) ----
#
# Contrato, calcado del harness de Claude Code y adaptado a que acá el
# humano puede tardar horas: el experto NO bloquea esperando. Llama a
# `ask_human`, deja la pregunta anotada, termina el turno con lo que
# hizo, y la respuesta del humano entra como el TURNO SIGUIENTE de la
# conversación. El hilo ya sabe pasar contexto entre turnos; no hacía
# falta inventar un canal nuevo.


async def _grafo_estancado(app: Optional[web.Application], db: Database,
                           g: dict) -> bool:
    """¿Este grafo `activo` ya no puede avanzar por su cuenta?

    Un grafo activo le impide al hilo armar otro, y con razón: dos
    planes sobre el mismo repo se pisan los archivos. La excepción es el
    que quedó colgado — nadie lo está corriendo, no tiene ningún nodo
    vivo, y la pregunta que lo dejó esperando ya no está abierta. Ese no
    protege nada: solo deja la conversación sin poder volver a
    planificar, para siempre. Hay uno así en la base desde el 24/8, con
    tres nodos en `esperando_humano` y sus cinco preguntas contestadas.

    Las tres condiciones son AND y el default es "bloquea": ante la
    duda, el error caro es dejar correr dos planes, no hacer esperar
    uno. Un grafo cortado a mitad (nodos en `corriendo` que nadie
    ejecuta) NO entra acá a propósito — de ese se encarga el barrido del
    boot, que lo vuelve a largar y lo sana.
    """
    # `.get` y no `[...]`: los tests pasan un dict pelado como app.
    if g["id"] in ((app or {}).get(GRAFOS_KEY) or {}):
        return False
    if any((t.get("estado") or "") == grafo_mod.CORRIENDO
           for t in (g.get("tasks") or ())):
        return False
    abiertas = await db.list_expert_questions(
        conversation_id=g.get("conversation_id") or "", only_open=True,
        limit=1)
    return not abiertas


async def _grafo_en_vez_de_proponer(
    app: Optional[web.Application], db: Database, project: dict, user: str,
    conv_id: Optional[str], result: dict,
) -> dict:
    """`DEMASIADO_GRANDE` deja de ser una propuesta y pasa a ser un plan
    que corre.

    Hasta hoy, cuando el planificador juzgaba que el pedido no entra en
    un run, el relay devolvía la descomposición en prosa y **no ejecutaba
    nada**: el humano tenía que elegir por dónde empezar y volver a
    pedirlo, una tarea por vez. Ese era el techo del que hablábamos —
    justo el caso para el que se construyó el grafo.

    Ahora ese mismo corte arma el grafo y lo larga. Se engancha acá y no
    en `run_expert_staged` a propósito: la task de fondo tiene que
    quedar registrada en la app para que el apagado la espere y para que
    `/graphs/{id}/cancel` la encuentre, y eso es cosa del servidor.

    Si algo sale mal —el modelo no devuelve un grafo válido, la base
    falla— se vuelve al texto de antes. Degradar a lo que ya funcionaba
    es mejor que un error: la descomposición sigue siendo útil.

    Se apaga con el flag `grafo_automatico` del proyecto.
    """
    from . import planificador

    defaults = project.get("defaults_json") or {}
    if app is None or not defaults.get("grafo_automatico", True):
        return result
    if conv_id and (vivo := await db.active_task_graph(conv_id)) \
            and not await _grafo_estancado(app, db, vivo):
        # Ya hay un grafo vivo en este hilo: dos planes sobre el mismo
        # repo se pisan los archivos y el humano ve dos avances a los
        # tumbos. Se queda la propuesta en texto.
        #
        # Pero se lo decimos. Antes esto devolvía la descomposición tal
        # cual —"todavía no ejecuté nada, dime por cuáles empiezo"—, que
        # es indistinguible de un pedido grande normal: el humano no
        # tenía forma de saber que lo único que faltaba era esperar.
        # Pasó el 30/8 a las 07:25: el planificador gastó 17k tokens en
        # cortar el pedido y la respuesta se leyó como una propuesta más.
        p = _grafo_publico(vivo).get("progreso") or {}
        result["content"] = (result.get("content") or "") + (
            f"\n\n⏸️ **No lo ejecuté todavía**: en este hilo ya hay un plan "
            f"corriendo ({p.get('hechos', 0)} de {p.get('total', 0)} tareas "
            "listas). Dos planes sobre el mismo repo se pisan los archivos. "
            "Cuando termine, vuelve a pedirme esto y lo ejecuto — o cancela "
            "el plan actual desde el panel del plan.")
        return result
    try:
        g = await planificador.armar_grafo(
            project, user, db=db, conversation_id=conv_id or "",
            # La descomposición en prosa que ya se pagó entra como
            # contexto: el razonador no vuelve a decidir el corte, solo
            # lo estructura.
            contexto=result.get("plan") or "")
    except Exception as e:  # noqa: BLE001 — sin grafo queda la propuesta
        logger.warning("no pude armar el grafo del pedido grande (%r): "
                       "devuelvo la descomposición en texto", e)
        return result

    _largar_grafo(app, project, g["id"])
    result["graph_id"] = g["id"]
    result["phase_at_end"] = "graph"
    result["content"] = (
        "📋 Este pedido es grande, así que lo **partí en "
        f"{len(g['tasks'])} tareas** y las estoy ejecutando:\n\n"
        f"{planificador.resumen(g)}\n\n"
        "Van de a dos en paralelo, y ninguna toca un archivo que otra "
        "esté tocando. Si una falla y no se puede reintentar sola, te "
        "pregunto antes de seguir.\n\n"
        f"`{g['id']}` — para pararlo: `POST /graphs/{g['id']}/cancel`"
    )
    return result


# =====================================================================
# Grafos de tareas: el disparador (2026-08-23)
# =====================================================================
#
# Hasta hoy el motor del grafo (F1 + F2) estaba entero y no lo llamaba
# nadie: `correr_grafo` solo aparecía en los tests. Estos endpoints son
# la puerta — arman el grafo desde un pedido, lo largan en background y
# lo dejan consultable, que es lo que va a leer el panel del chat.
#
# El grafo corre en una task de fondo registrada en BG_TASKS_KEY, igual
# que un run de experto: sin eso, el apagado del relay no la espera y
# quedan nodos escribiendo mientras el proceso se cierra.


# Estados terminales de un chat cuando el run cortó por timeout, presupuesto
# o porque el experto se fue por las ramas. Sin este set, un `phase_at_end`
# raro caía al default "pendiente" y el nodo se quedaba amarillo para
# siempre. Mantenido chico a propósito: agregar un valor acá debería ser
# decisión de producto, no de implementación.
# Una sola definición, compartida con el orquestador (`grafo`): tener
# dos listas fue el bug — el panel pintaba de rojo un turno que el grafo
# daba por `hecho`. Sumar `provider_error` acá además arregla lo suyo:
# un turno que murió en un 429 tiene `status='ok'`, así que sin la fase
# salía verde. Pasaron tres seguidos en code-hero-rpg el 31/8.
_PHASE_FALLIDO = grafo_mod.FASES_INCOMPLETAS
_STATUS_FALLIDO = frozenset({"error", "cancelled"})

# Cuántos nodos como máximo dibujamos en el grafo sintético. Una cadena
# de 40 turnos son 40 capas y el SVG se vuelve ilegible (medido el día
# que pusimos 25 en pantalla: el nodo final salía del viewport y nadie
# se enteraba de que el run había terminado). 12 es lo que entra en
# pantalla sin scroll y deja el primero arriba. Subirlo requiere decisión.
_CAP_NODOS_SINTETICOS = 12


def _estado_del_turno(fila: dict, tiene_pregunta_abierta: bool) -> str:
    """El estado real de un turno, sin heurísticas (Etapa A, P2).

    La función es pura para poder testearla: misma entrada → misma
    salida, y nada de "status" inferido a partir del campo contiguo.
    El orden de los chequeos ES el orden de prioridad:
    1. corriendo: el run está activo y todavía no cortó.
    2. esperando_humano: el run cortó con una pregunta abierta.
    3. fallado: el run cortó por un motivo terminal (timeout, budget,
       off-plan, error, cancelado). Aunque el status diga "ok", una
       fase terminal lo invalida — un run que cerró por budget_exceeded
       igual tiene que mostrarse en rojo.
    4. hecho: terminó ok en una fase normal.
    5. pendiente: cualquier otro caso (ej. status desconocido).
    """
    status = (fila.get("status") or "").strip()
    phase = fila.get("phase_at_end") or ""
    # La pregunta abierta gana sobre "running": el ejecutor cortó a
    # esperar, el status todavía no se actualizó y mentir con "corriendo"
    # confundiría al humano que la contestó.
    if tiene_pregunta_abierta:
        return "esperando_humano"
    if status == "running":
        return "corriendo"
    if status in _STATUS_FALLIDO or phase in _PHASE_FALLIDO:
        return "fallado"
    if status == "ok":
        return "hecho"
    return "pendiente"


def _estado_del_paso(
    idx_paso: int,
    *,
    marcados: set,
    turno_corriendo: bool,
    turno_pregunta_abierta: bool,
    ultimo_paso_corriendo: Optional[int],
) -> str:
    """Estado de un paso individual (Etapa B, P7).

    Pura, testeable:
    - marcado por el ejecutor o por el verificador → "hecho"
    - el primer paso sin marcar de un turno que sigue corriendo →
      "corriendo" (un solo nodo corriendo a la vez; coherente con
      cómo el humano lee la cadena)
    - los posteriores → "pendiente"
    - si el turno terminó y quedaron pasos sin marcar → "pendiente"
      (NUNCA "fallado": que no se haya marcado no prueba que no se
      haya hecho, y pintarlo en rojo sería la mentira que este diseño
      evita).
    """
    if idx_paso in marcados:
        return "hecho"
    if turno_pregunta_abierta:
        # El ejecutor paró a esperar; los pasos sin marcar no
        # pueden contar como "hecho" ni como "corriendo".
        return "pendiente"
    if turno_corriendo and idx_paso == ultimo_paso_corriendo:
        return "corriendo"
    return "pendiente"


def _stages_de(fila: dict) -> dict:
    """`chats.stages_json` parseado a dict (Etapa B). Vacío si está mal."""
    import json
    raw = fila.get("stages_json") or ""
    try:
        etapas = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        etapas = {}
    if not isinstance(etapas, dict):
        return {}
    return etapas


def _detalle_del_turno(fila: dict) -> str:
    """El texto que se ve al hacer hover sobre el nodo de un turno.

    Sale de los datos que ya tenemos guardados en `chats.stages_json`:
    si el planificador corrió, los pasos numerados; si no, la respuesta
    del experto. El veredicto del verificador se agrega al final para
    que se lea de una sola mirada.
    """
    import json
    etapas = _stages_de(fila)
    pasos = experts.pasos_del_plan(etapas.get("plan") or "")
    veredicto = (etapas.get("verifier_verdict") or "").strip()
    if pasos:
        # Numeramos para que el LLM y el humano vean la misma referencia
        # que `pasos_del_plan` extrajo del system prompt.
        cuerpo = "\n".join(f"{i+1}. {p}" for i, p in enumerate(pasos))
    else:
        # `last_response` no es una columna de `chats` (la respuesta vive
        # en el .md): sin pasos, lo más cercano que tenemos al contenido
        # del turno es el pedido que lo abrió.
        cuerpo = (fila.get("last_response")
                  or fila.get("user_prompt") or "").strip()
        if len(cuerpo) > 400:
            cuerpo = cuerpo[:400].rstrip() + "…"
    return f"{cuerpo}\n\nVerificador: {veredicto or '—'}" if veredicto \
        else cuerpo


def _titulo_de_respaldo(fila: dict) -> str:
    """Cómo llamar a un turno del que no guardamos el pedido.

    Son las filas anteriores a la migración del 30/8 (`user_prompt`).
    La hora no dice de qué se trata, pero un nodo con etiqueta se puede
    señalar y abrir; uno en blanco no se distingue del de al lado, que
    es como se veía el panel entero hasta hoy.
    """
    t = (fila.get("started_at") or "").strip()
    return f"turno de {t[11:16]}" if len(t) >= 16 else "turno"


def _costo_en_nodos(fila: dict, expandir_pasos: bool) -> int:
    """Cuántos nodos va a rendir este turno. Espeja la rama de abajo.

    Se calcula aparte del armado porque el cap tiene que decidir a qué
    turnos entra ANTES de construirlos. Si las dos condiciones se
    separan, el cap cuenta una cosa y el dibujo hace otra — y el panel
    vuelve a pasarse de largo sin que nadie lo note.
    """
    if not expandir_pasos:
        return 1
    etapas = _stages_de(fila)
    pasos = experts.pasos_del_plan(etapas.get("plan") or "")
    marcados = {k for k in (etapas.get("plan_steps_done") or {}).keys()
                if str(k).isdigit() and int(k) >= 1}
    return len(pasos) if (pasos and marcados) else 1


def _grafo_sintetico(
    chats: list[dict],
    *,
    corriendo: bool = False,
    preguntas_por_chat: Optional[dict[str, bool]] = None,
    expandir_pasos: bool = False,
) -> dict:
    """Un nodo por turno de la conversación, encadenado (Etapa A, P1).

    Existe porque el panel del plan tiene que poder mostrar ALGO en
    cualquier conversación, no solo en las que el disparador partió en
    tareas (Etapa A). Sale de los datos que ya están en `chats`, sin
    escribir en la DB ni inventar tablas.

    Modos:
      - Default (`expandir_pasos=False`): un nodo por turno con el
        detalle de los pasos numerado adentro (Etapa A). Es el modo
        que el panel dibuja hoy.
      - `expandir_pasos=True`: un nodo por paso del plan, encadenado,
        con el estado que sale de `plan_steps_done` (Etapa B, P6).
        Requiere que el ejecutor haya usado `plan_step_done` para que
        `plan_steps_done` no venga vacío — si nadie marcó, este modo
        colapsa al mismo resultado que el default (no expande pasos
        sin marcadores, para no mostrar "pendiente" en todo).

    El cap lo aplicamos acá y no en la UI: si lo hiciéramos del lado del
    JS, el server mandaría el set entero y gastaríamos ancho de banda
    por turnos que ya no se dibujan.

    `preguntas_por_chat` se puede pasar para testear la función pura sin
    DB: si no se pasa, se calcula por chat con `_tiene_pregunta_abierta`.
    """
    from . import grafo as graf
    # Los nodos de un grafo real corren como chats del MISMO hilo
    # (`source='grafo'`). Encadenarlos acá los mostraría como turnos del
    # humano, en fila y en un orden que no tuvieron —el grafo los corrió
    # en paralelo— y de paso empujarían fuera del cap a los turnos que sí
    # escribió una persona. El grafo ya tiene su propia vista.
    chats = [c for c in chats if (c.get("source") or "") != "grafo"]
    total_turnos = len(chats)
    # El cap es sobre NODOS y no sobre turnos. Con `expandir_pasos` un
    # turno rinde tantos nodos como pasos tenga su plan, así que 12
    # turnos daban 43 nodos en un panel lateral (medido el 30/8 en
    # `transformadorplanos`): ilegible, y el turno más viejo se dibujaba
    # con el mismo peso que el que está corriendo. Recortamos por la
    # cola —los turnos nuevos son los que importan— y nunca a mitad de
    # un turno: los pasos se leen en orden dentro del turno y cortar
    # entre el 2 y el 3 deja algo que no refleja nada.
    elegidos: list[dict] = []
    presupuesto = _CAP_NODOS_SINTETICOS
    for c in reversed(chats):                    # del más nuevo al más viejo
        costo = _costo_en_nodos(c, expandir_pasos)
        if elegidos and costo > presupuesto:
            break
        presupuesto -= costo
        elegidos.append(c)
    elegidos.reverse()
    if len(elegidos) < total_turnos:
        objetivo = f"últimos {len(elegidos)} de {total_turnos} turnos"
    else:
        # La tabla `chats` no tiene columna `objetivo` (la tiene el
        # grafo real); usamos el prompt del último turno como título.
        objetivo = (elegidos[-1].get("user_prompt") or "").strip() \
            if elegidos else ""
        if not objetivo:
            # Sin prompt guardado (filas anteriores a la migración del
            # 30/8) decir "sin turnos todavía" sobre un hilo con turnos
            # es la mentira que el panel no puede darse el lujo de
            # contar: preferimos contarlos.
            objetivo = (f"{total_turnos} turno(s) en este hilo"
                        if total_turnos else "sin turnos todavía")
        if len(objetivo) > 80:
            objetivo = objetivo[:80].rstrip() + "…"
    chats = elegidos

    # `chat_id` sirve de id de nodo: es único, es lo que la UI ya conoce,
    # y nos ahorra inventar una capa de mapping.
    # La pregunta abierta la consultamos una sola vez por turno para no
    # pegarle a la DB N veces; `_estado_del_turno` la recibe como bool.
    # (ponytail: lookup O(N) sobre `expert_questions`; si el cuello se
    # vuelve visible, mover a una sola query agregada por turno.)
    if preguntas_por_chat is None:
        preguntas_abiertas: dict[str, bool] = {}
        for c in chats:
            cid = c.get("id") or ""
            if not cid:
                continue
            preguntas_abiertas[cid] = _tiene_pregunta_abierta(c)
    else:
        preguntas_abiertas = preguntas_por_chat

    nodos = []
    ids: list[str] = []
    id_anterior: Optional[str] = None
    for c in chats:
        cid = c.get("id") or ""
        if not cid:
            continue
        prompt = (c.get("user_prompt") or c.get("prompt") or "").strip()
        pregunta_abierta = preguntas_abiertas.get(cid, False)
        etapas = _stages_de(c)
        pasos = experts.pasos_del_plan(etapas.get("plan") or "")
        pasos_marcados = {
            int(k) for k in (etapas.get("plan_steps_done") or {}).keys()
            if str(k).isdigit() and int(k) >= 1
        }
        estado_turno = _estado_del_turno(c, pregunta_abierta)
        turno_corriendo = estado_turno == graf.CORRIENDO
        ultimo_paso_corriendo: Optional[int] = None
        if turno_corriendo and pasos:
            # El primer paso sin marcar es el "corriendo". Si todos
            # están marcados, ninguno está corriendo.
            for idx in range(1, len(pasos) + 1):
                if idx not in pasos_marcados:
                    ultimo_paso_corriendo = idx
                    break
        if pasos and expandir_pasos and pasos_marcados:
            # Un nodo por paso: el id es `<chat_id>#<paso>` para que
            # la UI los distinga del nodo-de-turno viejo y los pueda
            # mergear con los grafos reales que vienen de la DB.
            #
            # (ponytail: entrar solo si `pasos_marcados` no es vacío
            # evita el caso "todos pendientes" cuando nadie usó la
            # tool — sería el peor resultado posible para el panel,
            # peor que el modo A. El switch está acá y no en la UI
            # porque el costo de la decisión es uno por turno.)
            for idx, paso in enumerate(pasos, start=1):
                pid = f"{cid}.{idx}"
                titulo = f"Paso {idx}: {paso[:55]}"
                if len(paso) > 55:
                    titulo = f"Paso {idx}: {paso[:54].rstrip()}…"
                estado = _estado_del_paso(
                    idx_paso=idx,
                    marcados=pasos_marcados,
                    turno_corriendo=turno_corriendo,
                    turno_pregunta_abierta=pregunta_abierta,
                    ultimo_paso_corriendo=ultimo_paso_corriendo,
                )
                nodos.append({
                    "id": pid,
                    "titulo": titulo,
                    "detalle": paso,
                    "estado": estado,
                    "deps": [id_anterior] if id_anterior else [],
                    "idempotente": True,
                    "intentos": 0,
                    "max_intentos": 1,
                    "orden": len(nodos),
                    "chat_id": cid,
                    "resultado": "",
                    "error": "",
                    "started_at": c.get("started_at"),
                    "ended_at": c.get("finished_at") or c.get("ended_at"),
                })
                ids.append(pid)
                id_anterior = pid
        else:
            # Turno sin pasos parseables: un nodo por turno (modo viejo
            # de la Etapa A). Sigue siendo útil para hilos donde el
            # planificador no se disparó (chico o trivial).
            titulo = (prompt[:60] + ("…" if len(prompt) > 60 else "")
                      or _titulo_de_respaldo(c))
            nodos.append({
                "id": cid,
                "titulo": titulo,
                "detalle": _detalle_del_turno(c),
                "estado": estado_turno,
                "deps": [id_anterior] if id_anterior else [],
                "idempotente": True,
                "intentos": 0,
                "max_intentos": 1,
                "orden": len(nodos),
                "chat_id": cid,
                "resultado": "",
                "error": "",
                "started_at": c.get("started_at"),
                "ended_at": c.get("finished_at") or c.get("ended_at"),
            })
            ids.append(cid)
            id_anterior = cid

    # `capas()` y `progreso()` esperan `list[Nodo]` (con atributos), no
    # dicts: convertimos acá para reusar exactamente la misma lógica que
    # `_grafo_publico`. Si la lista queda vacía, `capas` tira
    # `GrafoInvalido` — y lo cazamos porque la UI tiene que poder
    # mostrar un grafo vacío sin romperse.
    nodos_grafo = [graf.Nodo.desde_fila(t, t["deps"]) for t in nodos]
    if nodos_grafo:
        # `capas()` tira `GrafoInvalido` con lista vacía: por eso el
        # `if`. Dentro del try van las dos llamadas para no repetir el
        # guard con `progreso()` — esa no tira.
        try:
            capas = graf.capas(nodos_grafo)
        except graf.GrafoInvalido:
            capas = []
        progreso = graf.progreso(nodos_grafo)
    else:
        capas = []
        progreso = {
            "total": 0, "hechos": 0, "corriendo": 0, "pendientes": 0,
            "bloqueados": 0, "fallados": 0, "esperando_humano": 0,
            "porcentaje": 0, "estado": "hecho",
        }
    corriendo = (any(n["estado"] == graf.CORRIENDO for n in nodos)
                 or corriendo)
    return {
        "id": "",
        "objetivo": objetivo,
        "estado": "activo" if corriendo else "hecho",
        "corriendo": corriendo,
        "progreso": progreso,
        "orden": ids,
        "capas": capas,
        "tasks": nodos,
        "sintetico": True,
    }


def _tiene_pregunta_abierta(chat: dict) -> bool:
    """¿Este chat tiene una expert_question `open`? (Etapa A, P1).

    **No lo uses desde el endpoint**: `chats` no tiene columna
    `question_id`, así que esto devuelve siempre False y el estado
    `esperando_humano` no se dibujaba nunca (2026-08-30). Quien sabe la
    respuesta es `expert_questions`, y `conversation_plan` la consulta
    de una y pasa el resultado por `preguntas_por_chat`. Esto queda como
    default para los llamadores que arman la fila a mano (los tests).
    """
    qid = (chat.get("question_id") or "").strip()
    return bool(qid)


def _verificacion_publica(g: dict) -> dict:
    """`task_graphs.verificacion_json` parseado, o `{}`.

    El veredicto de la verificación de cierre del grafo (ver
    `orquestador._verificar_al_cerrar`). Sale en el MISMO payload que el
    panel ya consume para pintar el grafo y no en un endpoint aparte,
    porque una perspectiva que habla y no llega a ninguna pantalla es
    peor que no tenerla: de 68 veredictos `off_plan` de los chats, 33 no
    se vieron nunca. `{}` = todavía no se verificó (o el grafo se
    canceló, que no se verifica a propósito).
    """
    raw = g.get("verificacion_json") or ""
    try:
        v = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        v = {}
    return v if isinstance(v, dict) else {}


def _grafo_publico(g: dict) -> dict:
    """El grafo como lo quiere la UI: nodos + progreso ya calculado.

    Suma dos campos de PRESENTACIÓN además de lo que ya había (6/9/26,
    ver `grafo.estado_visible` y `grafo.es_error_de_presupuesto` para el
    porqué): `estado_visible` en la raíz distingue "esperando a un
    humano" de "activo" de verdad sin tocar `estado` (que sigue siendo
    el valor crudo de `task_graphs.estado` — de eso depende el
    relanzamiento) y `presupuesto_agotado` en cada tarea distingue un
    corte por presupuesto de un fallo común. Único lugar donde se
    calculan: el endpoint y el panel leen esto, no reinventan el
    criterio cada uno por su lado.
    """
    nodos = [grafo_mod.Nodo.desde_fila(t, t["deps"]) for t in g["tasks"]]
    sustituidos = grafo_mod.sustituidos(nodos)
    # Un grafo sin tareas no se puede ordenar ni dibujar, y `capas`
    # levanta `GrafoInvalido` — que salía como 500 y dejaba el panel del
    # chat sin poder mostrar NADA del hilo. No debería existir (ver el
    # guard de `create_task_graph`), pero los que quedaron de antes del
    # fix del 24/8 siguen en la base: se devuelven vacíos y visibles, que
    # es lo que deja verlos para poder cancelarlos.
    if not nodos:
        return {
            "id": g["id"], "objetivo": g["objetivo"], "estado": g["estado"],
            "estado_visible": g["estado"],
            "conversation_id": g.get("conversation_id"),
            "project_slug": g.get("project_slug"),
            "created_at": g.get("created_at"), "updated_at": g.get("updated_at"),
            "progreso": {"total": 0, "hechos": 0, "corriendo": 0,
                         "pendientes": 0, "bloqueados": 0, "fallados": 0,
                         "esperando_humano": 0, "porcentaje": 0,
                         "estado": g["estado"]},
            "verificacion": _verificacion_publica(g),
            "orden": [], "capas": [], "tasks": [],
        }
    return {
        "id": g["id"], "objetivo": g["objetivo"], "estado": g["estado"],
        "estado_visible": grafo_mod.estado_visible(nodos, g["estado"]),
        "verificacion": _verificacion_publica(g),
        "conversation_id": g.get("conversation_id"),
        "project_slug": g.get("project_slug"),
        "created_at": g.get("created_at"), "updated_at": g.get("updated_at"),
        "progreso": grafo_mod.progreso(nodos),
        "orden": grafo_mod.orden_topologico(nodos),
        # El grafo en filas: cada capa es lo que puede correr a la vez.
        # Va calculado desde acá y no en el JS por lo mismo que el orden
        # topológico — es lógica de grafo, y se prueba en Python.
        "capas": grafo_mod.capas(nodos),
        "tasks": [{
            "id": t["id"], "titulo": t["titulo"], "detalle": t["detalle"],
            "estado": t["estado"], "deps": t["deps"],
            "parent_id": t.get("parent_id"), "sustituido": t["id"] in sustituidos,
            "idempotente": bool(t["idempotente"]),
            "intentos": t["intentos"], "max_intentos": t["max_intentos"],
            "orden": t["orden"], "chat_id": t.get("chat_id") or "",
            "resultado": t.get("resultado") or "", "error": t.get("error") or "",
            "presupuesto_agotado": grafo_mod.es_error_de_presupuesto(
                t.get("error") or ""),
            "started_at": t.get("started_at"), "ended_at": t.get("ended_at"),
        } for t in sorted(g["tasks"], key=lambda x: (x["orden"], x["id"]))],
    }


async def _correr_grafo_bg(app: web.Application, project: dict,
                           graph_id: str) -> None:
    """Corre el grafo entero fuera del request. Nunca lanza."""
    from . import orquestador

    db = app[DB_KEY]
    logctx.bind(graph_id, project.get("slug") or "")
    # Cada nodo reporta como un run del chat (2026-08-24). Sin esto los
    # nodos del grafo no reportaban a NADIE: `/experts/status/{chat_id}`
    # contestaba "no hay run con ese id" y el panel no podía decir en qué
    # herramienta estaba. Medido el 24/8: veinte minutos de un nodo
    # trabajando —escribiendo PNGs— sin una sola señal en pantalla, y lo
    # dimos por colgado.
    #
    # Es una factory porque `make_progress_callback` registra el
    # RunProgress bajo un `chat_id` y el chat de cada nodo se crea dentro
    # del ejecutor. Ver `orquestador.ejecutor_minimax`.
    experts.make_progress_callback(
        store=app[PROGRESS_KEY], notify=None, chat_id=graph_id,
        target=project.get("slug") or "", model="")
    graph_progress = app[PROGRESS_KEY][graph_id]
    graph_progress.phase = "graph"
    graph_progress.graph_id = graph_id

    def progreso_de(chat_id: str):
        # Esta factory corre dentro de la task del nodo, no del padre.
        logctx.bind(chat_id, project.get("slug") or "")
        callback = experts.make_progress_callback(
            store=app[PROGRESS_KEY], notify=app[NOTIFY_KEY],
            chat_id=chat_id, target=project.get("slug") or "",
            model=(project.get("defaults_json") or {}).get("model") or "")
        rp = app[PROGRESS_KEY][chat_id]
        rp.graph_id = graph_id
        task = asyncio.current_task()
        if task is not None:
            def terminado(_task):
                rp.finished = True
                graph_progress.last_activity_at = time.monotonic()

            task.add_done_callback(terminado)
        return callback

    try:
        prog = await orquestador.lanzar(db, project, graph_id,
                                        progreso_de=progreso_de)
        logger.info("grafo %s terminó: %s", graph_id, prog.get("estado"))
    except asyncio.CancelledError:
        logger.info("grafo %s cancelado", graph_id)
        raise
    except Exception:  # noqa: BLE001 — un grafo roto no voltea el relay
        logger.exception("el grafo %s se cayó", graph_id)
        with contextlib.suppress(Exception):
            await db.set_task_graph_state(graph_id, "fallado")
    finally:
        graph_progress.finished = True


def _largar_grafo(app: web.Application, project: dict, graph_id: str) -> None:
    grafos: dict = app[GRAFOS_KEY]
    task = coordination.spawn_workspace(
        app[DB_KEY], project, _correr_grafo_bg(app, project, graph_id))
    grafos[graph_id] = task
    bg_tasks: set = app[BG_TASKS_KEY]
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)
    task.add_done_callback(lambda _t: grafos.pop(graph_id, None))


# Proyectos con el planificador corriendo ahora mismo. La guarda de
# `graphs_create` mira `task_graphs`, y esa fila recién existe cuando el
# planificador vuelve (1-2 min): durante todo ese rato el proyecto queda
# sin reservar y un segundo POST pasa limpio. Pasó el 2026-09-07 —
# un grafo de ejemplo y un grafo de ejemplo, 99 segundos aparte, los dos sobre
# workshopdemo y apuntando al mismo .md — con un cliente que cortó por
# timeout y reintentó. En memoria alcanza porque el relay es UN proceso;
# si algún día son varios, esto tiene que ser una fila con TTL como las
# reservas de archivo.
# ponytail: set en memoria, no reserva persistida. Upgrade cuando el
# relay corra en más de un proceso.
_PLANIFICANDO: set[str] = set()


@_require_auth
@coordination.guard_workspace(DB_KEY)
async def graphs_create(request: web.Request) -> web.Response:
    """POST /graphs  {project, objetivo, conversation_id?, arrancar?}

    Arma el grafo con el razonador y —salvo que pidas `arrancar: false`—
    lo larga. Devuelve 202 con el grafo entero: el humano ve QUÉ se va a
    hacer en la misma respuesta en que arrancó, que es la diferencia
    entre un plan y una caja negra.
    """
    from . import planificador

    db = request.app[DB_KEY]
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)

    slug = (body.get("project") or body.get("target") or "").strip()
    objetivo = (body.get("objetivo") or body.get("user") or "").strip()
    if not slug or not objetivo:
        return web.json_response(
            {"error": "mandá `project` y `objetivo`"}, status=400)
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": f"no existe el proyecto {slug}"},
                                 status=404)

    conv_id = (body.get("conversation_id") or "").strip()
    # Dos grafos sobre el mismo repo se pisarían los archivos sin que
    # ninguno se entere (las reservas los frenarían de a uno, y el
    # humano vería dos planes avanzando a los tumbos). El chequeo por
    # proyecto se hace SIEMPRE, incluso sin `conversation_id`: dos POST
    # sin conv_id al mismo proyecto pasaban la guarda anterior y hoy
    # arrancaron dos grafos sobre el mismo repositorio.
    # `active_task_graph_by_project` devuelve el grafo ENTERO, no el id.
    if (previo := await db.active_task_graph_by_project(slug)):
        return web.json_response(
            {"error": "este proyecto ya tiene un grafo corriendo",
             "graph_id": previo["id"], "message":
                 "Esperá a que termine o cancelalo con "
                 f"POST /graphs/{previo['id']}/cancel."},
            status=409)
    # La guarda por conversación se mantiene ADEMÁS: cubre el caso
    # histórico — un hilo con grafo vivo sigue bloqueado, como antes.
    if conv_id and (previo := await db.active_task_graph(conv_id)):
        return web.json_response(
            {"error": "esta conversación ya tiene un grafo corriendo",
             "graph_id": previo["id"], "message":
                 "Esperá a que termine o cancelalo con "
                 f"POST /graphs/{previo['id']}/cancel."},
            status=409)

    if slug in _PLANIFICANDO:
        return web.json_response(
            {"error": "este proyecto ya está armando un grafo",
             "message": "Esperá a que el planificador termine y mirá "
                        "el grafo que salga de ahí."},
            status=409)
    _PLANIFICANDO.add(slug)
    try:
        g = await planificador.armar_grafo(
            project, objetivo, db=db, conversation_id=conv_id,
            contexto=(body.get("contexto") or "").strip())
    except RuntimeError as e:
        # El planificador no pudo. Es un 502 y no un 500: el relay
        # funciona, el que no contestó algo usable fue el modelo.
        return web.json_response({"error": str(e)}, status=502)
    finally:
        # Sale sí o sí: si el planificador rompe y no soltamos el slug,
        # el proyecto queda trabado hasta reiniciar el relay.
        _PLANIFICANDO.discard(slug)

    if body.get("arrancar", True):
        _largar_grafo(request.app, project, g["id"])
    return web.json_response(_grafo_publico(g), status=202)


_ETAPA = {
    "planner": "planificando", "verifier": "verificando",
    "documenter": "documentando", "question": "te está preguntando",
    "thinking": "ejecutando", "writing": "ejecutando",
    "tool_call": "ejecutando", "say": "ejecutando",
    "heartbeat": "ejecutando", "steer": "ejecutando",
}


@_require_auth
async def conversation_plan(request: web.Request) -> web.Response:
    """GET /conversations/{id}/plan — lo que el panel necesita, en un pedido.

    **Un pedido y no tres.** El panel tiene que poder responder "¿en qué
    va esto?" sin encadenar `/graphs` → `/chats` → `/experts/status`, y
    sobre todo sin que el navegador decida cuál de las tres respuestas
    manda. La decisión de qué mostrar es del servidor.

    Desde la Etapa A, este endpoint devuelve **siempre** un grafo:

    - Si la conversación tiene un grafo real (activo o viejo), sale por
      `_grafo_publico` sin la clave `sintetico`.
    - Si no, sale por `_grafo_sintetico`: un nodo por turno, encadenado,
      con `sintetico: true`. El panel no distingue entre los dos:
      `pintar()` lee la misma forma en los dos casos.

    Lo que este endpoint **no** dice, a propósito: en qué PASO del plan
    va el ejecutor. Nadie lleva ese puntero — el plan es prosa y el
    ejecutor no reporta contra él. Inventarlo sería la misma clase de
    mentira que la barra de progreso que contaba las falladas como
    avance. La Etapa B ataca eso con `plan_step_done` y la red del
    verificador.
    """
    db = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    if await db.get_conversation(conv_id) is None:
        return web.json_response({"error": "not found"}, status=404)

    def _grafo(g: dict, corriendo: bool) -> web.Response:
        salida = _grafo_publico(g)
        salida["corriendo"] = corriendo
        return web.json_response({"modo": "grafo", "grafo": salida})

    # El grafo del hilo —el vivo si hay, si no el último— gana **mientras
    # siga siendo lo que está pasando**.
    #
    # Hasta el 30/8 ganaba siempre, y el costo era exactamente lo que el
    # panel existe para evitar: un hilo que terminó su plan y siguió
    # trabajando mostraba el plan viejo al 100% —badge "hecho", barra
    # llena, sin aviso— mientras un run nuevo corría abajo. Nueve
    # conversaciones así en la base, una con diez turnos invisibles.
    # Terminado y con trabajo posterior, el grafo dejó de ser el estado
    # del hilo y pasó a ser su historia; el panel muestra el estado.
    #
    # `vivo` (¿lo está corriendo ESTE proceso?) y no `estado='activo'`:
    # un grafo que quedó `activo` sin nadie ejecutándolo —cortado, o con
    # nodos esperando una respuesta que ya se dio— si no seguiría
    # tapando el hilo para siempre. Hay uno así en la base desde el 24/8.
    g = (await db.active_task_graph(conv_id)
         or await db.last_task_graph(conv_id))
    if g is not None:
        vivo = g["id"] in request.app[GRAFOS_KEY]
        if vivo or not await db.hay_turnos_humanos_despues(
                conv_id, g.get("updated_at") or g.get("created_at") or ""):
            return _grafo(g, vivo)

    # Sin grafo real (o con uno ya superado): un nodo por turno,
    # encadenado (Etapa A, P1). Si hay un run "running" en el hilo,
    # marcamos el grafo entero como `corriendo` para que el panel no se
    # duerma.
    # Pedimos más del cap para que el `_grafo_sintetico` haga su propio
    # cap (12 nodos, no los 10 que viene por default). El cap se hace
    # acá y no en el cliente para que el `objetivo` pueda decir
    # "últimos N de M" cuando recortamos.
    chats = await db.list_chats_of_conversation(conv_id, limit=200)
    # La lista viene DESC (más nuevo primero) para el `corriendo` de arriba;
    # pero el grafo se dibuja de izquierda (inicio) a derecha (fin), así que
    # lo invertimos a ASC antes de armar los nodos. Sin esto, un chat largo
    # muestra el último turno a la izquierda como si fuera el primero y la
    # UI "no se completa" desde la mirada del humano (2026-08-28).
    chats_para_grafo = list(reversed(chats))
    corriendo = any((c.get("status") or "") == "running" for c in chats)
    # Etapa B, P7: si ALGÚN turno del hilo tiene `plan_steps_done`,
    # expandimos a un nodo por paso. Sin esto, un chat largo con muchos
    # pasos marcados por el ejecutor colapsa al modo A (un nodo por
    # turno) y el panel nunca ve el progreso fino. El propio
    # `_grafo_sintetico` desactiva el modo si nadie marcó, así que
    # pasa por default sin costo.
    hay_markers = any(
        _stages_de(c).get("plan_steps_done") for c in chats)
    # `_tiene_pregunta_abierta` resuelve por un campo (`question_id`) que
    # la tabla `chats` no tiene, así que siempre daba False y el estado
    # `esperando_humano` no se dibujaba nunca. Una query al hilo entero
    # lo contesta de verdad y cuesta lo mismo que no contestarlo.
    abiertas = await db.list_expert_questions(
        conversation_id=conv_id, only_open=True, limit=50)
    con_pregunta = {(q.get("chat_id") or "") for q in abiertas}
    salida = _grafo_sintetico(
        chats_para_grafo, corriendo=corriendo, expandir_pasos=hay_markers,
        preguntas_por_chat={(c.get("id") or ""): (c.get("id") or "")
                            in con_pregunta for c in chats_para_grafo})
    return web.json_response({"modo": "grafo", "grafo": salida})



@_require_auth
async def graphs_get(request: web.Request) -> web.Response:
    """GET /graphs/{id} — el grafo con su progreso. Lo lee el panel."""
    db = request.app[DB_KEY]
    g = await db.get_task_graph(request.match_info["id"])
    if g is None:
        return web.json_response({"error": "not found"}, status=404)
    salida = _grafo_publico(g)
    salida["corriendo"] = request.match_info["id"] in request.app[GRAFOS_KEY]
    return web.json_response(salida)


@_require_auth
async def graphs_list(request: web.Request) -> web.Response:
    """GET /graphs?conversation=<id> — el grafo activo de un hilo."""
    db = request.app[DB_KEY]
    conv = (request.query.get("conversation") or "").strip()
    if not conv:
        return web.json_response({"error": "mandá `conversation`"}, status=400)
    g = await db.active_task_graph(conv)
    if g is None:
        return web.json_response({"graph": None})
    return web.json_response({"graph": _grafo_publico(g)})


@_require_auth
@coordination.guard_workspace(DB_KEY, source="graph")
async def graphs_resume(request: web.Request) -> web.Response:
    """POST /graphs/{id}/resume — retoma un grafo cortado.

    `correr_grafo` sana al arrancar (nodos que quedaron `corriendo`,
    reservas huérfanas), así que acá no hay nada especial que hacer más
    que volver a largarlo. Lo que sí hace falta es no largar dos: un
    grafo ya corriendo devuelve 409 en vez de duplicar los nodos.
    """
    db = request.app[DB_KEY]
    graph_id = request.match_info["id"]
    g = await db.get_task_graph(graph_id)
    if g is None:
        return web.json_response({"error": "not found"}, status=404)
    if graph_id in request.app[GRAFOS_KEY]:
        return web.json_response(
            {"error": "ese grafo ya está corriendo", "graph_id": graph_id},
            status=409)
    project = await db.get_project(g.get("project_slug") or "")
    if not project:
        return web.json_response(
            {"error": f"el proyecto {g.get('project_slug')!r} ya no existe"},
            status=409)
    # Un grafo cancelado o fallado vuelve a `activo`: retomarlo es
    # justamente decir "esto sigue". Si no, `estado_del_grafo` lo dejaría
    # como estaba y el panel mostraría un plan muerto avanzando.
    if g["estado"] != "activo":
        await db.set_task_graph_state(graph_id, "activo")
    _largar_grafo(request.app, project, graph_id)
    return web.json_response(
        _grafo_publico(await db.get_task_graph(graph_id)), status=202)


@_require_auth
async def graphs_cancel(request: web.Request) -> web.Response:
    """POST /graphs/{id}/cancel — corta el grafo y sus nodos en vuelo.

    Cancelar la task de fondo alcanza: el `finally` de `correr_grafo`
    corta los nodos vivos, les suelta los archivos y les aplica la regla
    (ver `_cerrar_las_que_quedaron`). Sin eso, cancelar dejaría runs
    escribiendo archivos que ya nadie mira.
    """
    db = request.app[DB_KEY]
    graph_id = request.match_info["id"]
    g = await db.get_task_graph(graph_id)
    if g is None:
        return web.json_response({"error": "not found"}, status=404)
    task = request.app[GRAFOS_KEY].get(graph_id)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    # Igual que en /experts/cancel: si el proceso se reinició y el grafo
    # quedó `activo` en la base sin nadie corriéndolo, marcarlo cancelado
    # lo mismo. El humano no quiere distinguir, quiere que pare.
    await db.set_task_graph_state(graph_id, "cancelado")
    return web.json_response(
        _grafo_publico(await db.get_task_graph(graph_id)))


@_require_auth
async def expert_questions_list(request: web.Request) -> web.Response:
    """GET /questions?conversation=<id>&chat=<id>&only_open=1"""
    db = request.app[DB_KEY]
    only_open = request.query.get("only_open", "1") not in ("0", "false")
    rows = await db.list_expert_questions(
        conversation_id=request.query.get("conversation") or None,
        chat_id=request.query.get("chat") or None,
        only_open=only_open)
    out = []
    for r in rows:
        try:
            pregunta = json.loads(r.get("question_json") or "{}")
        except json.JSONDecodeError:
            pregunta = {"title": r.get("question_json") or ""}
        out.append({
            "id": r["id"], "chat_id": r["chat_id"],
            "conversation_id": r.get("conversation_id"),
            "kind": r.get("kind"), "status": r.get("status"),
            "asked_at": r.get("asked_at"),
            "question": pregunta,
        })
    return web.json_response({"questions": out})


@_require_auth
async def expert_question_answer(request: web.Request) -> web.Response:
    """POST /questions/{q_id}/answer  {choice?, text?}

    Responder es la mitad del trabajo: la otra mitad es que el experto
    RETOME. Por eso el endpoint devuelve `resume_prompt`, el texto listo
    para mandar como turno siguiente. Quien responde (Admin UI o el bot)
    lo postea a /experts/run con la misma conversación y el hilo sigue
    donde quedó.
    """
    db = request.app[DB_KEY]
    q_id = request.match_info["q_id"]
    q = await db.get_expert_question(q_id)
    if q is None:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)

    choice = (body.get("choice") or "").strip()
    texto = (body.get("text") or "").strip()
    if not choice and not texto:
        return web.json_response(
            {"error": "mandá `choice` (la key de una opción) o `text`"},
            status=400)

    try:
        pregunta = json.loads(q.get("question_json") or "{}")
    except json.JSONDecodeError:
        pregunta = {}
    etiqueta = texto
    if choice:
        for opt in pregunta.get("options", []):
            if opt.get("key") == choice:
                etiqueta = opt.get("label", choice)
                break
        else:
            return web.json_response(
                {"error": f"opción {choice!r} no está en la pregunta"},
                status=400)

    ok = await db.answer_expert_question(
        q_id, json.dumps({"choice": choice, "label": etiqueta,
                          "free_text": texto}, ensure_ascii=False))
    if not ok:
        # Ya no está abierta. Dos motivos distintos, y decir cuál importa:
        # "ya respondida" es un doble click (Discord y la UI a la vez);
        # "superseded" es que el experto preguntó otra cosa después, y ahí
        # el humano tiene que mirar la decisión NUEVA, no insistir con
        # esta. Un mensaje único mandaba a la persona a buscar una
        # respuesta que ya existía cuando en realidad cambió la pregunta.
        estado = (await db.get_expert_question(q_id)).get("status")
        detalle = {
            "answered": "esa pregunta ya estaba respondida",
            "skipped": "esa pregunta la habías descartado",
            "superseded": "esa decisión quedó vieja: el experto preguntó "
                          "otra cosa después. Mirá la última.",
        }.get(estado, f"esa pregunta ya no está abierta ({estado})")
        return web.json_response({"error": detalle, "status": estado},
                                 status=409)

    # El texto que retoma el hilo. Lleva la pregunta adentro porque el
    # experto la hizo en un turno anterior y la capa 3 del historial se
    # queda solo con su texto final: sin repetirla, "sí, dale" no tiene
    # referente.
    titulo = (pregunta.get("title") or "").strip()
    resume = (f"Respuesta a tu pregunta «{titulo}»: {etiqueta}"
              if titulo else f"Respuesta: {etiqueta}")
    if texto and choice:
        resume += f"\n\n{texto}"

    # F4: si la pregunta era de una tarea de un grafo, contestarla tiene
    # que MOVER el grafo. Sin esto, "para y pregunta" era un callejón sin
    # salida: el orquestador dejaba la pregunta, el humano la contestaba
    # y el plan seguía parado igual porque nadie llevaba la respuesta a
    # la tarea.
    grafo_info = await _retomar_grafo_tras_respuesta(
        request, q, pregunta, choice, texto, etiqueta)

    return web.json_response({
        "ok": True, "id": q_id, "answer": etiqueta,
        "conversation_id": q.get("conversation_id"),
        "project": q.get("project_slug"),
        # Cuando la respuesta retomó un grafo NO hay que mandar el
        # `resume_prompt` como turno nuevo: el trabajo lo sigue el
        # orquestador, y un turno de chat encima duplicaría el pedido.
        "resume_prompt": "" if grafo_info else resume,
        **({"grafo": grafo_info} if grafo_info else {}),
    })


async def _retomar_grafo_tras_respuesta(
    request: web.Request, q: dict, pregunta: dict, choice: str, texto: str,
    etiqueta: str = "",
) -> Optional[dict]:
    """Lleva la respuesta del humano a la tarea del grafo y lo relanza.

    Dos formas de llegar acá, y son distintas:

    1. **La pregunta la hizo el orquestador** (`kind="grafo"`): el humano
       decidió QUÉ HACER con la tarea — reintentarla, darla por fallada
       o parar el plan. La decisión viaja en la `key` de la opción y no
       en su etiqueta, que es texto en castellano y cambia. Si además
       (o en vez de eso) escribió a mano, ese texto va con la decisión:
       contestar con palabras es tan válido como elegir una opción, y
       antes se descartaba en silencio — la tarea se reintentaba a
       ciegas y el grafo se reanudaba igual, así que no se notaba.
       Va `texto` pelado, NO `etiqueta`: pegarle al detalle de la tarea
       "Reintentala igual" no le dice nada al run siguiente.
    2. **La hizo el nodo desde adentro** (`ask_human`): el humano no
       decidió nada sobre la tarea, contestó algo que la tarea
       necesitaba. Vuelve a `pendiente` con la respuesta pegada al
       detalle, y el run siguiente la lee.

    Devuelve `{graph_id, decision, corriendo}` o None si no era de un
    grafo. Nunca lanza: no poder retomar no puede volver 500 una
    respuesta que YA se guardó.
    """
    from . import orquestador

    db = request.app[DB_KEY]
    try:
        if q.get("kind") == "grafo" and pregunta.get("task_id"):
            gid = pregunta.get("graph_id") or ""
            decision = await orquestador.aplicar_respuesta(
                db, gid, pregunta["task_id"], choice, texto)
        else:
            gid = await orquestador.responder_a_la_tarea(
                db, q.get("chat_id") or "", texto or etiqueta)
            decision = "responder" if gid else ""
        if not gid or not decision:
            return None
        if decision == "parar":
            # Ya quedó `cancelado`; si además estaba corriendo, cortarlo.
            if tarea := request.app[GRAFOS_KEY].get(gid):
                tarea.cancel()
            return {"graph_id": gid, "decision": decision, "corriendo": False}

        g = await db.get_task_graph(gid)
        project = await db.get_project((g or {}).get("project_slug") or "")
        if not g or not project or gid in request.app[GRAFOS_KEY]:
            return {"graph_id": gid, "decision": decision,
                    "corriendo": gid in request.app[GRAFOS_KEY]}
        _largar_grafo(request.app, project, gid)
        return {"graph_id": gid, "decision": decision, "corriendo": True}
    except Exception:  # noqa: BLE001
        logger.exception("no pude retomar el grafo tras la respuesta")
        return None


@_require_auth
async def expert_question_skip(request: web.Request) -> web.Response:
    """POST /questions/{q_id}/skip — el humano decide no contestar."""
    db = request.app[DB_KEY]
    q_id = request.match_info["q_id"]
    if await db.get_expert_question(q_id) is None:
        return web.json_response({"error": "not found"}, status=404)
    await db.skip_expert_question(q_id)
    return web.json_response({"ok": True, "id": q_id, "status": "skipped"})


# ---- Iter 9.7: preguntas interactivas (night_questions) ----
# Endpoints admin para que el humano responda checkpoints sin abrir
# Discord. Idempotentes: 409 si la pregunta ya está respondida.
# El orchestrator está bloqueado en ask_and_wait() esperando y lo lee
# en su próximo poll (5s).

@_require_auth
async def night_questions_list(request: web.Request) -> web.Response:
    """GET /admin/api/night/questions?run_id=<id>&only_open=1"""
    db: Database = request.app[DB_KEY]
    run_id = request.query.get("run_id", "") or None
    only_open = request.query.get("only_open", "0") == "1"
    rows = await db.list_night_questions(
        run_id=run_id, only_open=only_open)
    # Decodificar question_json y answer_json para el cliente.
    out = []
    for r in rows:
        try:
            q = json.loads(r["question_json"])
        except (json.JSONDecodeError, TypeError):
            q = {"_raw": r["question_json"]}
        try:
            a = json.loads(r["answer_json"]) if r.get("answer_json") else None
        except (json.JSONDecodeError, TypeError):
            a = None
        out.append({
            "id": r["id"],
            "run_id": r["run_id"],
            "phase": r["phase"],
            "question": q,
            "asked_at": r["asked_at"],
            "answered_at": r.get("answered_at"),
            "answer": a,
            "open": r.get("answered_at") is None,
        })
    return web.json_response({"questions": out})


@_require_auth
async def night_question_answer(request: web.Request) -> web.Response:
    """POST /admin/api/night/questions/<q_id>/answer
    body: {"choice": "B", "free_text": null}
    → guarda respuesta, el orchestrator la lee en su próximo poll.
    409 si ya está respondida (idempotente, gana la primera)."""
    db: Database = request.app[DB_KEY]
    q_id = request.match_info.get("q_id", "")
    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    answer = {
        "choice": body.get("choice"),
        "free_text": body.get("free_text"),
    }
    if not answer["choice"] and not answer["free_text"]:
        return web.json_response(
            {"error": "choice o free_text requerido"}, status=400)
    ok = await db.answer_night_question(q_id, answer)
    if not ok:
        # Ya respondida o no existe — distinguir para el cliente.
        existing = await db.get_night_question(q_id)
        if existing is None:
            return web.json_response(
                {"error": f"question {q_id!r} desconocida"},
                status=404)
        return web.json_response(
            {"error": "ya respondida",
             "answered_at": existing.get("answered_at"),
             "answer": existing.get("answer_json")},
            status=409)
    return web.json_response({"ok": True, "q_id": q_id, "answer": answer})


@_require_auth
async def night_question_skip(request: web.Request) -> web.Response:
    """POST /admin/api/night/questions/<q_id>/skip
    → cierra la pregunta sin respuesta. El orchestrator sigue con el
    default de la pregunta. Mismas reglas de idempotencia que /answer."""
    db: Database = request.app[DB_KEY]
    q_id = request.match_info.get("q_id", "")
    ok = await db.skip_night_question(q_id)
    if not ok:
        existing = await db.get_night_question(q_id)
        if existing is None:
            return web.json_response(
                {"error": f"question {q_id!r} desconocida"},
                status=404)
        return web.json_response(
            {"error": "ya respondida",
             "answered_at": existing.get("answered_at")},
            status=409)
    return web.json_response({"ok": True, "q_id": q_id, "skipped": True})


# ---- Iter 9.8: canal Discord para checkpoints ----
# El bot C# (bot-demo) hace POST a este endpoint cuando el humano
# clickea un botón de embed (o usa un slash command /day).
# Mismo primitive que /admin/api/night/questions/<q_id>/answer pero
# sin auth: el bot corre en localhost, /discord/* es la convención
# para endpoints de Discord (mismo patrón que la ausencia de auth en
# /notify). Si algún día se mete auth, aplica a TODAS las rutas /discord/*.

async def discord_day_answer(request: web.Request) -> web.Response:
    """POST /discord/day-answer
    body: {"discord_user_id": str, "run_id": str, "question_id": str,
           "choice": str|null, "free_text": str|null}
    → guarda la respuesta. El orchestrator (bloqueado en
    _ask_and_wait) la lee en su próximo poll (5s) y sigue.
    Idempotente: si la pregunta ya está respondida → 409 (la primera
    gana, igual que el endpoint admin). Si no existe → 404.
    Si choice y free_text ambos vacíos → 400.
    """
    db: Database = request.app[DB_KEY]
    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    q_id = body.get("question_id", "")
    choice = body.get("choice")
    free_text = body.get("free_text")
    if not q_id:
        return web.json_response({"error": "question_id requerido"},
                                 status=400)
    if not choice and not free_text:
        return web.json_response(
            {"error": "choice o free_text requerido"}, status=400)
    answer = {"choice": choice, "free_text": free_text}
    ok = await db.answer_night_question(q_id, answer)
    if not ok:
        existing = await db.get_night_question(q_id)
        if existing is None:
            return web.json_response(
                {"error": f"question {q_id!r} desconocida"},
                status=404)
        return web.json_response(
            {"error": "ya respondida",
             "answered_at": existing.get("answered_at"),
             "answer": existing.get("answer_json")},
            status=409)
    return web.json_response({"ok": True, "q_id": q_id, "answer": answer})


# Ids de attachment mencionados en la respuesta del experto. Mismo
# formato que `attachments._ID_RE` (att_ + 16 hex); acá va sin anclas
# porque buscamos dentro de prosa. El match se valida contra el disco.
_ATT_ID_RE = re.compile(r"\batt_[0-9a-f]{16}\b")


# ---- Discord-attachment: upload genérico de bytes por el bot C# ----
# El bot C# descarga el attachment de Discord CDN (con auth del bot)
# y lo sube al relay como multipart. El relay lo guarda en disco con
# id estable (sha256[:16]) y devuelve la ruta local. El bot pasa
# después el id (o los ids) en `POST /experts/run` y el relay inyecta
# un bloque "## Adjuntos" al texto que se le pasa al LLM.
#
# Sin auth (mismo patrón que /discord/day-answer y /notify). Si algún
# día se mete auth, aplica a TODAS las rutas /discord/*.

async def discord_attachments_upload(request: web.Request) -> web.Response:
    """POST /discord/attachments (multipart) — guarda un attachment.

    Campos:
        file (multipart file, requerido): los bytes crudos.
        filename (opcional): nombre original para derivar la extensión.
        mimetype (opcional): ej 'image/png', 'application/pdf'.

    Returns:
        201 -> {id, path, bytes, mimetype}
        400 -> falta `file` o no es multipart válido.
        413 -> file > ATTACHMENT_MAX_BYTES (default 50MB).
    """
    cap = attachments_mod.max_attachment_bytes()
    try:
        reader = await request.multipart()
    except (ValueError, AssertionError):
        return web.json_response(
            {"error": "se espera multipart/form-data"}, status=400)

    file_bytes: Optional[bytes] = None
    filename = ""
    mimetype: Optional[str] = None
    async for part in reader:
        if part.name == "file":
            buf = bytearray()
            while True:
                chunk = await part.read_chunk(64 * 1024)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > cap:
                    return web.json_response(
                        {"error": f"file > {cap} bytes"}, status=413)
            file_bytes = bytes(buf)
            filename = part.filename or ""
            mimetype = part.headers.get("Content-Type") or None
        # Otros campos opcionales (filename/mimetype como FormField).
        elif part.name in ("filename", "mimetype"):
            value = (await part.text()).strip()
            if part.name == "filename":
                filename = value
            elif part.name == "mimetype":
                mimetype = value or None

    if not file_bytes:
        return web.json_response(
            {"error": "falta el archivo (multipart field `file`)"},
            status=400)

    try:
        attach_id, path, size = attachments_mod.store(
            file_bytes, filename=filename, mimetype=mimetype)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    return web.json_response(
        {"id": attach_id,
         "path": str(path).replace("\\", "/"),
         "bytes": size,
         "mimetype": mimetype or "",
         "filename": filename,
         # `inline`: el contenido se inyecta como TEXTO en el prompt.
         # El cliente lo usa para avisarle al usuario en el acto en vez
         # de hacerlo esperar a que el LLM conteste "no puedo verlo".
         "inline": attachments_mod.is_inlineable(str(path)),
         # `viewable`: es una imagen que el modelo VE (2026-07-31). No
         # es inline —no es texto— pero tampoco es opaca. Sin este campo
         # cada cliente tendría que duplicar la lista de extensiones y
         # se irían de sync al primer cambio.
         "viewable": bool(attachments_mod.image_mime(path))},
        status=201)


async def discord_attachments_download(request: web.Request) -> web.Response:
    """GET /discord/attachments/{id} — devuelve los bytes guardados.

    La contraparte del POST. El bot la usa para bajar lo que el experto
    generó durante el run (típico: un `screenshot`) y reenviarlo a
    Discord como archivo.

    El id es content-addressed y `attachments.resolve()` lo valida
    contra un regex estricto (`att_` + 16 hex) antes de tocar el disco,
    así que no hay path traversal posible por acá.

        200 -> los bytes
        404 -> el id no existe (o no matchea el formato)
    """
    attach_id = request.match_info.get("attach_id", "")
    path = attachments_mod.resolve(attach_id)
    if path is None:
        return web.json_response(
            {"error": f"attachment {attach_id!r} no encontrado"}, status=404)
    return web.FileResponse(path)


# ---------- app factory ----------


async def _reap_zombie_chats(db: Database) -> int:
    """Cierra los chats que quedaron `running` de un proceso anterior.

    Al boot no hay ambigüedad: el registro de runs vivos (`RUNNING_KEY`)
    es de memoria, así que cualquier fila en `running` perdió a su dueño
    cuando el proceso murió. Sin este barrido quedan en "En curso" para
    siempre — había 8 acumulados, el más viejo de 10 días.

    Reusa `list_zombie_chats`, que ya trae el criterio (y su margen de
    60s, que acá sobra pero no molesta: lo que entre en ese margen lo
    levanta el boot siguiente o la grid de zombies del admin).

    Devuelve cuántos cerró. Best-effort: no rompe el arranque.
    """
    try:
        muertos = await db.list_zombie_chats(older_than_s=60)
    except Exception as e:  # noqa: BLE001 — un barrido no tumba el boot
        logger.warning("no pude listar chats zombies: %r", e)
        return 0
    cerrados = 0
    for chat in muertos:
        try:
            await db.finish_chat(
                chat["id"], status="cancelled",
                error="zombie: el relay reinició y el run murió con el "
                      "proceso anterior")
            cerrados += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("no pude cerrar el zombie %s: %r",
                           chat["id"][:8], e)
    return cerrados


def _avisar_globales_apagados() -> None:
    """Avisa si un modelo global apunta a un spec apagado en el catálogo.

    No es cosmético: `enabled` NO gatea `build_model`, así que el spec
    corre igual — pero la pantalla Modelos lo muestra apagado, y
    `admin._validar_flag` RECHAZA asignárselo a un proyecto por no estar
    prendido. O sea, el relay usa por default algo que no te deja elegir
    a vos, y las dos pantallas que deberían explicarlo dicen lo
    contrario. Un WARNING nombra la contradicción en el boot en vez de
    dejarla para el día que alguien se pregunte por qué el documentador
    corre con un modelo que "está apagado".

    Solo avisa: prender el modelo o cambiar el .env es decisión del
    humano, y fallar el arranque por esto sería peor que el problema.
    """
    from . import config as relay_config
    prendidos = {m["spec"] for m in experts.catalog() if m.get("enabled")}
    if not prendidos:
        return          # catálogo vacío: nada que contrastar
    globales = {
        "FOURBIS_MODEL": relay_config.model_spec(),
        "FOURBIS_PLANNER_MODEL": relay_config.planner_model_spec(),
        "FOURBIS_VERIFIER_MODEL": relay_config.verifier_model_spec(),
        "FOURBIS_DOCUMENTER_MODEL": relay_config.documenter_model_spec(),
        "FOURBIS_COMPACTOR_MODEL": relay_config.compactor_model_spec(),
    }
    for var, spec in globales.items():
        if spec and spec != "test" and spec not in prendidos:
            logger.warning(
                "%s=%s no está PRENDIDO en el catálogo de modelos: el relay "
                "lo usa igual, pero la pantalla Modelos lo muestra apagado y "
                "no vas a poder asignárselo a un proyecto. Prendelo en "
                "/admin/ (tab Modelos) o cambiá la variable.", var, spec)


async def _warm_cbm_session(app: web.Application) -> None:
    """Levanta la sesión MCP residente de cbm al boot (Fase 5, 2026-09-02).

    Sin esto, la sesión recién arranca en el primer tool call de un
    experto o de la Admin UI, que paga el spawn de ~1.1s. Acá se paga
    ese costo al boot, en background, contra un proyecto cualquiera ya
    indexado — después de esto queda UN proceso cbm residente que sirve
    a todos los proyectos (`experts._cbm_toolset` es un singleton de
    módulo, no por proyecto).

    Degrada solo: sin binario cbm, sin proyectos indexados, o cualquier
    falla del spawn → warning y el relay sigue con el camino viejo
    (spawn por CLI, ~1.1s por llamada). Igual que `cbm_watcher.run`.
    """
    try:
        if not experts.cbm_binary_path():
            return
        from . import admin
        db = app[DB_KEY]
        projects = [p for p in await db.list_projects(enabled_only=True)
                    if p.get("include_in_index", 1) and p.get("repo_path")
                    and Path(p["repo_path"]).is_dir()]
        if not projects:
            logger.info("cbm warmup: sin proyectos indexados; nada que calentar")
            return
        cbm_proj = admin._cbm_project_name(projects[0]["repo_path"])
        await experts.cbm_call("index_status", {"project": cbm_proj}, timeout=45.0)
        logger.info("cbm warmup: sesión residente lista (proyecto=%s)", cbm_proj)
    except Exception:
        logger.warning(
            "cbm warmup falló; primer spawn real pagará ~1.1s", exc_info=True)


async def _on_startup(app: web.Application) -> None:
    from . import config as relay_config
    # Health-check al iniciar (bug fix 2026-07-08): avisar de qué
    # ejecutable está cargando este código. Si NO es el venv del
    # proyecto, warning claro — suele pasar cuando hay un zombie de
    # un start previo sirviendo el puerto y tú levantas otro encima.
    # Mira stop.ps1/start.ps1 mejorados en ese turno.
    import sys as _sys
    from pathlib import Path as _Path
    _server_file = _Path(__file__).resolve()
    _server_marker = _sys.prefix != _sys.base_prefix
    logger.info(
        "boot: python=%s cwd=%s server=%s in_venv=%s",
        _sys.executable,
        os.getcwd(),
        _server_file,
        _server_marker,
    )
    state_dir = Path(os.environ.get("STATE_DIR", "./state"))
    sessions = SessionRegistry()
    notify = NotifyClient(base_url=os.environ.get("BOT_NOTIFY_URL", default_bot_url()))
    skills = SkillCache()
    db = Database()
    await db.init_schema()
    await finalization.retry_pending(db)
    # ADR-037 fase 2: el cache de roles se llena una vez acá. Es un
    # lookup por request y la tabla la edita un humano por SQL, así que
    # una consulta por request no compraría nada.
    identity.load_roles(await db.list_users())
    # Mismo criterio que los roles: el catálogo de modelos es un lookup
    # por run y lo edita un humano. Se refresca solo al escribir por la
    # API (ver admin._refrescar_catalogo).
    experts.load_catalog(await db.list_models())
    # Los avisos resuelven los valores efectivos, incluida system_config.
    relay_config.set_runtime_config(await db.all_config())
    _avisar_globales_apagados()
    # Chats que quedaron en `running` de un relay anterior. Va ANTES de
    # sanar los grafos porque `sanar` repara las TAREAS y esto repara la
    # fila del chat, que nadie tocaba: los chats de un grafo cancelado o
    # fallado no entran en `list_active_graphs` y quedaban en `running`
    # para siempre, contados como vivos por la UI y con el reloj
    # corriendo solo. Visto hoy: dos nodos de un grafo cancelado
    # sobrevivieron al reinicio como zombies.
    try:
        _huerfanos = await db.reap_running_chats(
            "se cortó el proceso del relay mientras este chat corría")
        if _huerfanos:
            logger.warning(
                "boot: cerré %d chat(s) que quedaron en running de una "
                "corrida anterior", _huerfanos)
    except Exception:  # noqa: BLE001 — un chat colgado no impide el boot
        logger.exception("boot: no pude cerrar los chats huérfanos")
    # F4: grafos que quedaron a medias cuando este relay (o el anterior)
    # se cortó. Se SANAN pero NO se relanzan solos: soltar reservas y
    # marcar los nodos huérfanos es reparación, y arrancar trabajo nuevo
    # sin que nadie lo pida es otra cosa. El humano lo retoma con
    # `POST /graphs/{id}/resume` o con el ▶ del panel.
    try:
        from . import orquestador as _orq
        for _gid in await db.list_active_graphs():
            if _sanadas := await _orq.sanar(db, _gid):
                logger.warning(
                    "boot: el grafo %s quedó a medias; sané %d tareas. "
                    "No lo relanzo solo: retomalo desde el panel.",
                    _gid, _sanadas)
    except Exception:  # noqa: BLE001 — un grafo roto no impide el boot
        logger.exception("boot: no pude sanar los grafos a medias")
    # Iter 9.8: auto-seed del proyecto `notes` (workspace de notas/
    # bitácora/decisiones, sin repo). Idempotente — se hace al boot
    # aunque la DB ya tenga datos. La carpeta en disco también se
    # crea acá (la DB no toca filesystem).
    try:
        notes = await db.ensure_notes_project()
        if notes is not None:
            from pathlib import Path as _P
            notes_root = _P(notes["repo_path"]).expanduser()
            notes_root.mkdir(parents=True, exist_ok=True)
            logger.info("notes-workspace listo: %s", notes_root)
    except Exception as e:  # noqa: BLE001
        logger.warning("ensure_notes_project falló: %r (sigo sin)", e)
    # Iter 9.10: la migración consults → chats ya corrió en el boot
    # de iter 9.8 y la tabla consults se dropeó en este pase. Nada
    # que migrar.
    # Iter 9.7: cerrar preguntas abiertas de runs muertos (el
    # orchestrator no pudo cerrarlas porque el proceso cayó).
    # Sin esto, las preguntas quedan abiertas para siempre.
    try:
        n_closed = await db.orphan_open_questions()
        if n_closed:
            logger.info(
                "startup: cerradas %d preguntas huérfanas de runs muertos",
                n_closed)
    except Exception as e:  # noqa: BLE001
        logger.warning("orphan_open_questions falló: %r", e)
    # …y lo mismo con los chats: un `running` al boot es de un proceso
    # que ya no existe (2026-08-17).
    n_zombies = await _reap_zombie_chats(db)
    if n_zombies:
        logger.info("startup: cerrados %d chats zombies de runs muertos",
                    n_zombies)
    registry = CommandRegistry(db)
    n_cmds = await registry.load_from_db()
    app[SESSIONS_KEY] = sessions
    app[NOTIFY_KEY] = notify
    app[SKILLS_KEY] = skills
    app[DB_KEY] = db
    app[COMMANDS_KEY] = registry
    app[RUNNING_KEY] = {}
    # Ver BG_TASKS_KEY: referencias vivas a las tasks de experto, para
    # poder drenarlas al cerrar (RUNNING_KEY se vacía antes de tiempo).
    app[BG_TASKS_KEY] = set()
    app[GRAFOS_KEY] = {}
    # Fase 1: progress store para liveness on-demand + progreso SSE.
    app[PROGRESS_KEY] = {}
    # Iter 5.4: state_dir de night runs (plan mirror). Lo necesitan
    # los endpoints /admin/api/night-runs/* para leer el espejo del plan
    # ledger. Sin esto el detail devuelve plan_tasks=[] aunque el
    # plan.md exista en disco.
    app[STATE_DIR_KEY] = state_dir
    # ADR-028: registry de night runs vivos.
    app[NIGHT_KEY] = {}
    # F1: pool de MCPs on-demand + reaper por idle.
    pool = McpPool()
    # F2: installer de MCPs desde GitHub (jobs en memoria, no reaper).
    app[MCP_INSTALLER_KEY] = McpInstaller()
    # Alta de skills desde GitHub (jobs en memoria; TTL 1h, sweep lazy).
    app[SKILL_BROWSER_KEY] = SkillBrowser()
    app[MCP_POOL_KEY] = pool
    app[MCP_REAPER_KEY] = asyncio.create_task(pool.reaper_loop())
    # Iter 9.11: probe de health al boot para MCPs stdio externos
    # on-demand. Sin esto el campo health queda 'unknown' hasta que un
    # experto los use (el código de update vive dentro de
    # _catalog_toolsets, en experts.py). Solo externos:
    # heurística = command sin path absoluto (los wrappers propios
    # usan sys.executable + ruta .py absoluta y andan sin probe).
    # Bound por MCP_INIT_TIMEOUT_S por probe, no bloquea el boot.
    app[MCP_HEALTH_PROBE_KEY] = asyncio.create_task(
        _probe_external_mcps_health(app))
    # ADR-025: sweeper de auto-close (24h de inactividad → cierra + compacta)
    app[SWEEPER_KEY] = asyncio.create_task(_autoclose_sweeper(app))
    app[EXPORT_RETRY_KEY] = asyncio.create_task(_export_retry_loop(app))
    # Opción A: auto-reindex incremental de cbm por file-watcher.
    # Degrada solo (flag off / sin cbm / sin watchdog → no-op con log).
    from . import cbm_watcher
    app[CBM_WATCHER_KEY] = asyncio.create_task(cbm_watcher.run(app))
    # Fase 5 (2026-09-02): calentar la sesión MCP residente de cbm.
    # Medido: sin proceso cbm vivo, cada spawn paga ~1.1s (295MB de
    # imagen a cargar); con UNO residente, 15-30ms — factor 65x, y
    # abarata TODOS los spawns del relay (index_repository,
    # _cbm_cli_text, futuros), no solo el primero. Fire-and-forget:
    # no puede bloquear el boot ni voltearlo si cbm no está instalado.
    app[CBM_WARMUP_KEY] = asyncio.create_task(_warm_cbm_session(app))
    # Health-check: exponer qué versión cargó realmente el proceso
    # (Fase 1.5 — bug fix 2026-07-08 "no se actualizaba el relay").
    # El admin UI /health ya devuelve relay_version; este log deja
    # rastro en consola para que un `Get-Content logs\relay.log`
    # muestre de un vistazo qué PID carga qué archivos.
    _server_filename = _server_file.name
    _in_venv_hint = "✓ venv" if _server_marker else "⚠ NO venv"
    # 2026-08-28: guard contra la clase entera de bugs
    # "variable no inicializada en run_expert_staged" (T1.2 fue uno).
    # Si la inicialización de `Bitacora` o el call site al verificador
    # cambian, este check lo detecta al boot en vez de esperar al
    # primer chat. Falla rápido y ruidoso antes de aceptar tráfico.
    try:
        from .experts import Bitacora, run_expert_staged  # noqa: F401
        btest = Bitacora()
        btest.marcar_paso(1, "boot-check")
        bj = btest.volcar()
        if not bj or "pasos" not in bj:
            raise RuntimeError(f"Bitacora.volcar() no serializó pasos: {bj!r}")
        logger.info("boot-check Bitacora OK: %s", bj)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "boot-check FALLÓ (Bitacora / run_expert_staged): %s — "
            "el relay NO va a poder ejecutar el verificador. "
            "Revisar el último PR mergeado a develop antes de seguir.",
            exc, exc_info=True)
        raise SystemExit(2) from exc
    logger.info(
        "boot completo: %d commands, modulo cargado=%s [%s]",
        n_cmds, _server_filename, _in_venv_hint)
    logger.info(
        "relay listo (push+expertos): prompts=%s notify=%s api_key=%s db=%s commands=%d",
        state_dir / "prompts", notify.url, "set" if _get_api_key() else "off",
        db.path, n_cmds,
    )


async def _probe_external_mcps_health(app: web.Application) -> None:
    """Probe de health al boot para MCPs stdio externos on-demand.

    Bug fix iter 9.11: el código de update de health vivía dentro de
    `_catalog_toolsets` (experts.py), entonces el campo `health` se
    quedaba en 'unknown' para MCPs que nadie usaba nunca. Ahora probe
    al boot: external stdio on-demand → handshake one-shot → setea
    'ok' o 'handshake_failed'. No bloquea el boot (background task,
    bounded por MCP_INIT_TIMEOUT_S por probe).

    Heurística de "externo": command sin path absoluto. Wrappers
    propios (github_mcp.py, playwright_mcp.py) usan sys.executable +
    ruta .py absoluta y ya tienen health=ok sin probe. Siempre probe
    para cbm / sequential-thinking / cualquier npx / cmd.
    """
    # 2026-08-16: apagable. El probe LEVANTA PROCESOS de verdad (`npx -y
    # …`, `uvx …`) y en la primera corrida además los descarga. Eso está
    # bien en una máquina con red, y está mal en la suite: desde que el
    # catálogo trae MCPs sembrados, cada create_app() de un test spawnea
    # dos subprocesses y espera su timeout. Medido: test_conversations_ui
    # pasó de 2.1s a 19.3s. Mismo criterio que CBM_AUTO_WATCH=0 en el
    # conftest — un test no debería tocar procesos ni red de la máquina.
    if os.environ.get("FOURBIS_MCP_HEALTH_PROBE", "").strip() in ("0", "off"):
        logger.debug("health-probe: apagado por FOURBIS_MCP_HEALTH_PROBE")
        return
    db = app[DB_KEY]
    try:
        rows = await db.list_mcp_servers(enabled_only=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("health-probe: list_mcp_servers falló (%r), skip", e)
        return
    n_ok = n_failed = n_skipped = 0
    n_skipped_config = 0  # env vars faltantes (postgres-mcp sin POSTGRES_MCP_URI)
    for row in rows:
        if row.get("transport") != "stdio":
            continue
        if not row.get("on_demand"):
            continue
        cmd = (row.get("command") or "").strip()
        # Heurística externo: el path no es absoluto O apunta a algo
        # sin extensión .exe/.py local (cmd/npx/node pelados).
        is_external = not Path(cmd).is_absolute() or Path(cmd).name.lower() in {
            "cmd", "npx", "node", "npm",
        }
        if not is_external:
            n_skipped += 1
            continue
        # El probe y la escritura del health viven en mcp_pool para que
        # el botón de re-chequeo de la Admin UI no escriba otra cosa.
        health, error = await mcp_pool.probe_and_store_health(db, row)
        if health == "ok":
            n_ok += 1
            logger.info("health-probe: %r → ok", row["name"])
        elif health == "skipped":
            # 2026-08-28: separado de los wrappers locales. Son MCPs
            # sembrados pero sin config de runtime (env vars faltantes).
            # El log sale en INFO, no WARNING: NO es un fallo.
            n_skipped_config += 1
            logger.info(
                "health-probe: %r → skipped (%s) — setea la env var para "
                "que arranque en el próximo boot", row["name"], error)
        else:
            n_failed += 1
            logger.warning(
                "health-probe: %r → handshake_failed (%s)", row["name"], error)
    logger.info(
        "health-probe completo: %d ok, %d failed, %d skipped "
        "(%d wrappers locales, %d sin env var)",
        n_ok, n_failed, n_skipped + n_skipped_config,
        n_skipped, n_skipped_config)


# Cuánto se espera a los runs de experto vivos al apagar la app. Corto a
# propósito: acota el cierre y alcanza de sobra para los runs con
# TestModel de la suite, que es donde este drenaje importa de verdad.
_DRAIN_RUNNING_TIMEOUT_S = 10.0


async def _drain_running_experts(app: web.Application) -> None:
    """Espera a los runs de experto en vuelo antes de terminar de cerrar.

    Sin esto, `_run_expert_bg` sigue escribiendo en la DB después de que
    el test cerró su TestClient, y cuando el `TemporaryDirectory` borra
    el tmpdir Windows aborta la limpieza con PermissionError /
    NotADirectoryError. Se manifestaba como un ERROR intermitente que
    saltaba de archivo en archivo según el orden de la suite, y que no
    tenía nada que ver con el test al que se le atribuía.

    Va acá, en el cleanup de la app, y no en cada fixture: son 38
    archivos de test con la misma fixture copiada, y arreglarla uno por
    uno deja el mismo bug latente en el número 39.

    Se drena BG_TASKS_KEY y no RUNNING_KEY: `_run_expert_bg` vacía el
    segundo en su `finally` y después todavía calcula sugerencias,
    persiste y notifica. Esperar sobre RUNNING_KEY es esperar sobre un
    dict que ya se vació — que es exactamente por qué la primera versión
    de este drenaje no arregló nada.
    """
    # …y `_bg_tasks`, que es la MISMA lección una colección más allá: las
    # que larga `_spawn_bg` (compactación, spawns del sweeper) vivían en
    # un set de módulo que nadie esperaba. Una compactación seguía
    # escribiendo la base después de que la app cerró; en los tests eso
    # borra el tempdir abajo de la task, y en producción deja la reserva
    # del workspace tomada por una task que ya nadie mira.
    # Y se vuelve a mirar en cada vuelta, porque una foto no alcanza: una
    # tarea que está terminando puede largar otra —un turno que dispara
    # su grafo es el caso normal, no el raro— y esa hija nacía después
    # del `asyncio.wait` y quedaba afuera. El cierre seguía igual y le
    # cerraba los clientes MCP/HTTP en la cara.
    #
    # El plazo es global y no por vuelta: si no, una cadena de tareas que
    # se largan entre sí estira el cierre indefinidamente, cada una con
    # su tope entero.
    limite = time.monotonic() + _DRAIN_RUNNING_TIMEOUT_S
    while True:
        pendientes = [t for t in ((app.get(BG_TASKS_KEY) or set()) | _bg_tasks)
                      if not t.done()]
        if not pendientes:
            return
        resto = limite - time.monotonic()
        if resto <= 0:
            break
        await asyncio.wait(pendientes, timeout=resto)

    for task in pendientes:
        # Se pasó del tope: cancelar es mejor que colgar el cierre.
        task.cancel()
    logger.warning(
        "cierre: %d run(s) de experto no terminaron en %.0fs, cancelados",
        len(pendientes), _DRAIN_RUNNING_TIMEOUT_S)
    # Dejar que el rescate persista antes de cerrar sus clientes MCP/HTTP.
    _, sin_responder = await asyncio.wait(pendientes, timeout=5)
    if sin_responder:
        logger.warning("cierre: %d tareas no respondieron a la cancelación",
                       len(sin_responder))


async def _frenar_productores(app: web.Application) -> None:
    """Corta los loops periódicos que pueden largar trabajo nuevo.

    Va ANTES del drenaje: el sweeper de auto-close larga tareas con
    `_spawn_bg`, así que cancelarlo después significaba drenar mientras
    alguien seguía agregando. Con el productor frenado, la lista de
    pendientes solo puede achicarse.
    """
    for task_key in (SWEEPER_KEY, EXPORT_RETRY_KEY, CBM_WATCHER_KEY,
                     CBM_WARMUP_KEY, MCP_HEALTH_PROBE_KEY):
        task = app.get(task_key)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


async def _on_cleanup(app: web.Application) -> None:
    await _frenar_productores(app)
    await _drain_running_experts(app)
    # F1: apagar reaper y matar los subprocesos MCP vivos del pool.
    reaper = app.get(MCP_REAPER_KEY)
    if reaper is not None:
        reaper.cancel()
        try:
            await reaper
        except asyncio.CancelledError:
            pass
    pool = app.get(MCP_POOL_KEY)
    if pool is not None:
        await pool.shutdown()
    # La sesión de cbm no vive en el pool (un proceso sirve a todos los
    # proyectos), así que se cierra aparte o queda huérfana: son 273MB.
    await experts.close_cbm_session()
    # ADR-028: night runs vivos — cancel duro; la fila queda colgada y
    # night_start la cierra como crashed en el próximo arranque.
    for _orch, task in (app.get(NIGHT_KEY) or {}).values():
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    notify: NotifyClient = app[NOTIFY_KEY]
    await notify.aclose()


def create_app(bind_host: str = "127.0.0.1") -> web.Application:
    # FIX: logging.basicConfig sin forzar encoding usa cp1252 en
    # Windows y revienta con emojis / acentos en logs de subprocess
    # (ej: cbm, git). Forzar utf-8 SOLO la primera vez (idempotente
    # para re-entrancy desde tests / PyInstance que llama create_app N
    # veces). Si pytest capturó stdout, `sys.stdout` es un wrapper y su
    # `.buffer` puede estar cerrado entre tests — caer a `sys.stderr`).
    global _log_utf8_configured
    if not _log_utf8_configured:
        logging.basicConfig(
            level=os.environ.get("LOG_LEVEL", "INFO"),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            force=True,
        )
        # Reconfigurar el StreamHandler default para utf-8 (Windows cp1252
        # → UnicodeDecodeError al loggear emojis / acentos). Si stdout.buffer
        # está cerrado (tests con capsys), usar stderr.buffer que pytest no
        # intercepta.
        target = (sys.stdout if sys.stdout and not getattr(
            sys.stdout, "closed", False) else sys.stderr)
        buf = getattr(target, "buffer", None)
        if buf is not None and not getattr(buf, "closed", False):
            try:
                handler_unicode = logging.StreamHandler(io.TextIOWrapper(
                    buf, encoding="utf-8", errors="replace"))
                logging.getLogger().handlers = [handler_unicode]  # type: ignore[arg-type]  # noqa
            except (ValueError, OSError):
                # Stream cerrado entre tests; dejar el StreamHandler default.
                pass

        # --- Correlación por chat (2026-07-25) --------------------------
        # El filter va en los HANDLERS y no en el root logger: a nivel
        # logger solo vería los records emitidos directo contra el root,
        # y todo lo interesante propaga desde los hijos (relay.experts,
        # relay.mcp_pool, ...). Ver logctx.ChatContextFilter.
        # Va DESPUÉS de la asignación de arriba a propósito: esa línea
        # pisa la lista entera de handlers, así que cualquier cosa que
        # agreguemos antes desaparece en silencio.
        ctx_filter = logctx.ChatContextFilter()
        for _h in logging.getLogger().handlers:
            _h.addFilter(ctx_filter)

        # --- Ruido de terceros -----------------------------------------
        # Medido al prender el log a disco: de 7.700 líneas, 2.252 eran
        # de `asyncio` y 824 de `aiohttp.access` (una por request, y la
        # Admin UI poll-ea cada 5s). Para un post-mortem de "por qué
        # murió este chat" no aportan nada y hacen rotar el archivo
        # antes de que sirva. WARNING salvo que pidas lo contrario.
        for _noisy in ("asyncio", "aiohttp.access", "aiohttp.server",
                       "httpx", "httpcore", "watchdog", "mcp"):
            logging.getLogger(_noisy).setLevel(
                os.environ.get("LOG_LEVEL_LIBS", "WARNING"))

        # --- Log a disco (2026-07-25) -----------------------------------
        # Hasta hoy no se persistía nada: solo el ring buffer de 500
        # líneas en memoria de la Admin UI. Un run ocupado (248 tool
        # calls) lo desborda entero y cualquier reinicio lo borra, así
        # que un post-mortem no tenía de dónde salir. 10 MB x 5 = 50 MB
        # de techo; nunca crece sin control.
        try:
            _log_dir = relay_config.log_dir()
            _log_dir.mkdir(parents=True, exist_ok=True)
            _fh = RotatingFileHandler(
                _log_dir / "relay.log",
                maxBytes=10 * 1024 * 1024, backupCount=5,
                # Sin encoding explícito, Windows abre en cp1252 y
                # revienta con el primer emoji — el mismo bug que el
                # comentario de arriba describe para el StreamHandler.
                encoding="utf-8",
                delay=True)
            _fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s "
                "[chat=%(chat_id)s proj=%(project)s] %(message)s"))
            _fh.addFilter(ctx_filter)
            logging.getLogger().addHandler(_fh)
        except OSError as e:
            # Disco lleno, permisos, path inválido: seguimos en memoria.
            # Un log que no se puede escribir no puede tumbar el relay.
            logging.getLogger("relay.server").warning(
                "no pude abrir el log a disco (%r): sigo solo en memoria", e)

        # --- Trazas OTel (2026-08-26) ----------------------------------
        # Va acá adentro del guard a propósito: `instrument_all` es un
        # switch global de proceso, y create_app se llama N veces desde
        # los tests. Apagado por default; ver `tracing.py` para el
        # destino y la advertencia sobre contenido de prompts.
        tracing.setup_tracing()

        _log_utf8_configured = True
    app = web.Application(
        # El cap real por tipo de request lo ponen los handlers; este es
        # el techo del transporte. Subido de 1 MB por /voice/transcribe
        # (audio hasta VOICE_MAX_AUDIO_BYTES, default 50 MB).
        client_max_size=voice.max_audio_bytes() + 1024 * 1024,
        # Origen/Host y peer antes de identidad: chequeos baratos y sin red.
        # access_identity después, y solo sobre lo que ya pasó el guard.
        # require_role al final: necesita la identidad ya resuelta.
        middlewares=[browser_guard, localhost_guard, identity.access_identity,
                     identity.require_role],
    )
    # El host efectivo del bind se muestra en la configuración.
    # y el endpoint /admin/api/config lo reporta a la UI.
    app[BIND_HOST_KEY] = bind_host

    # core
    app.router.add_get("/health", health)
    app.router.add_get("/system/active", system_active)
    app.router.add_get("/sessions", list_sessions_view)

    # handshake de la extensión VS Code (liveness: arma menú de
    # targets vivos en /sessions). El push por SSE ya no se ejerce.
    app.router.add_post("/agents/handshake", handshake)

    # expertos pydantic-ai (ADR-012; async ADR-024)
    app.router.add_post("/experts/run", experts_run)
    app.router.add_post("/experts/cancel/{chat_id}", experts_cancel)
    app.router.add_post("/experts/steer/{chat_id}", experts_steer)
    app.router.add_get("/experts/status/{chat_id}", experts_status)

    # conversaciones (ADR-025) — /nuevo y /cerrar del bot
    app.router.add_post("/conversations", conversations_create)
    app.router.add_get("/conversations", conversations_list)
    app.router.add_get("/conversations/{id}", conversations_get)
    app.router.add_get("/conversations/{id}/messages",
                       conversations_get_messages)
    app.router.add_post("/conversations/{id}/compact", conversations_compact)
    app.router.add_post("/conversations/{id}/close", conversations_close)
    app.router.add_get("/conversations/{id}/pr", conversation_pr_status)
    # Iter 10.3: ver / borrar la rama local acumulada tras /cerrar.
    app.router.add_get("/conversations/{id}/diff", conversation_diff)
    app.router.add_post("/conversations/{id}/git/{action}",
                        conversation_git_action)
    app.router.add_get("/conversations/{id}/plan", conversation_plan)
    app.router.add_get("/conversations/{id}/branch", conversation_branch_status)
    app.router.add_delete("/conversations/{id}/branch", conversation_branch_delete)
    # Iter 10.0: vincular un chat de UI a un Discord user (bridge).
    app.router.add_post(
        "/conversations/{id}/set-discord-user",
        conversation_set_discord_user)
    app.router.add_delete("/projects/{slug}", projects_delete)
    # Iter 10.1: setear/limpiar discord_channel_id por proyecto.
    app.router.add_patch(
        "/projects/{slug}/discord-channel",
        project_set_discord_channel)
    app.router.add_get("/commands", commands_list)
    app.router.add_post("/commands", commands_upsert)
    app.router.add_put("/commands/{name}", commands_upsert)
    app.router.add_delete("/commands/{name}", commands_delete)
    app.router.add_post("/commands/{name}/run", commands_run)

    # voice input (Track D / F5 — docs/VOICE_INPUT.md)
    app.router.add_post("/voice/transcribe", voice_transcribe)
    app.router.add_get("/voice/transcripts", voice_transcripts_list)
    app.router.add_get("/voice/transcripts/{id}", voice_transcripts_get)
    app.router.add_post("/voice/transcripts/{id}/process",
                        voice_transcript_process)

    # modo nocturno (ADR-028)
    app.router.add_post("/night-mode/start", night_start)
    app.router.add_post("/night-mode/stop", night_stop)
    app.router.add_get("/night-mode/status", night_status)
    # Iter 9.7: preguntas interactivas (responder checkpoints sin Discord).
    app.router.add_get(
        "/admin/api/night/questions", night_questions_list)
    app.router.add_get(
        "/admin/api/db-connections", db_connections_list)
    app.router.add_post(
        "/admin/api/db-connections", db_connection_upsert)
    app.router.add_delete(
        "/admin/api/db-connections/{alias}", db_connection_delete)
    app.router.add_post(
        "/admin/api/db-connections/{alias}/test", db_connection_test)
    app.router.add_post("/graphs", graphs_create)
    app.router.add_get("/graphs", graphs_list)
    app.router.add_get("/graphs/{id}", graphs_get)
    app.router.add_post("/graphs/{id}/resume", graphs_resume)
    app.router.add_post("/graphs/{id}/cancel", graphs_cancel)

    app.router.add_get("/questions", expert_questions_list)
    app.router.add_post("/questions/{q_id}/answer", expert_question_answer)
    app.router.add_post("/questions/{q_id}/skip", expert_question_skip)
    app.router.add_post(
        "/admin/api/night/questions/{q_id}/answer", night_question_answer)
    app.router.add_post(
        "/admin/api/night/questions/{q_id}/skip", night_question_skip)
    # Iter 9.8: el bot de Discord manda la respuesta del humano al relay.
    # Sin auth (mismo patrón que /notify, ver docstring del handler).
    app.router.add_post("/discord/day-answer", discord_day_answer)
    # Adjuntos: bytes de un archivo (foto, pdf, txt) que el usuario metió
    # en el canal de Discord o en el composer de la Admin UI. Sin auth.
    # `/attachments` es el nombre canónico desde 2026-07-31 (la UI también
    # sube); `/discord/attachments` queda como alias — el bot ya
    # desplegado apunta ahí y romperlo no compra nada.
    app.router.add_post("/attachments", discord_attachments_upload)
    app.router.add_get("/attachments/{attach_id}",
                       discord_attachments_download)
    app.router.add_post("/discord/attachments", discord_attachments_upload)
    app.router.add_get("/discord/attachments/{attach_id}",
                       discord_attachments_download)

    # chats (índice read-only) + stats
    app.router.add_get("/chats", chats_list)
    app.router.add_get("/chats/{id}", chats_get)
    app.router.add_get("/chats/{id}/md", chats_get_md)
    app.router.add_get("/chats/{id}/status", chats_get_status)
    app.router.add_get("/stats", stats_view)

    # MCP (Iter 4: Google tools reales, hoy mocked)
    app.router.add_post("/mcp", mcp_endpoint)

    # Admin UI (ADR-014 / docs/ADMIN_UI_SPEC.md).
    # Mismo proceso y puerto (:8413), bind 127.0.0.1.
    from . import admin as admin_module  # import lazy para no romper tests
    admin_module.register_admin_routes(app)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def main() -> None:
    # FIX Windows: forzar stdout/stderr a utf-8 a nivel de proceso ANTES
    # de crear loggers / spawnar subprocess. Sin esto, cp1252 revienta
    # al loggear emojis / acentos (cbm CLI devuelve UTF-8 con símbolos
    # → UnicodeDecodeError en el reader thread). Es seguro en otras
    # plataformas: stdout.buffer existe en todas, solo cambia el codec.
    for _stream_name in ("stdout", "stderr"):
        _stream = getattr(sys, _stream_name, None)
        if _stream is not None and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — best-effort
                pass
    from . import config as relay_config
    # Bind host: system_config (SQLite, editable desde la Admin UI) manda;
    # MCP_HOST (env/.env) es fallback para el primer arranque con la
    # tabla vacía; último default: loopback.
    host = (read_system_config_sync("RELAY_HOST", "").strip()
            or os.environ.get("MCP_HOST", "").strip()
            or "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8413"))
    app = create_app(bind_host=host)
    if host == "0.0.0.0":
        logger.warning(
            "RELAY_HOST=0.0.0.0: el relay queda expuesto a la LAN. "
            "/admin/* y /api/* siguen restringidos a localhost "
            "(localhost_guard activo).")
    web.run_app(app, host=host, port=port, access_log=None)


if __name__ == "__main__":
    main()
