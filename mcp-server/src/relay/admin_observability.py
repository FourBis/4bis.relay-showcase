"""Rutas de estado operativo: assets, health, métricas, logs y búsqueda."""
from __future__ import annotations
from datetime import datetime
import collections
import logging
import time
from pathlib import Path
from typing import Mapping
from aiohttp import web
from . import __version__ as RELAY_VERSION
from . import bot_control
from . import config as relay_config
from . import logctx
from .experts import cbm_binary_path
from .reporting import parse_window
from .app_state import DB_KEY, SESSIONS_KEY

from .admin_cbm import _cbm_indexed_count

logger = logging.getLogger("relay.admin")

ADMIN_STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "admin_static"

ADMIN_INDEX: tuple[float, str] | None = None

async def admin_index(request: web.Request) -> web.Response:
    """Sirve el index.html del UI admin (single page app vanilla)."""
    global ADMIN_INDEX
    path = ADMIN_STATIC_DIR / "index.html"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if mtime is None:
        return web.Response(
            text="<h1>Admin UI no compilada</h1><p>Falta admin_static/index.html</p>",
            content_type="text/html",
            status=500,
        )
    if ADMIN_INDEX is None or ADMIN_INDEX[0] != mtime:
        ADMIN_INDEX = (mtime, path.read_text(encoding="utf-8"))
    # Mismo cache-control que admin_static: la UI cambia seguido,
    # no queremos servir HTML viejo si el user mantiene la tab abierta.
    resp = web.Response(text=ADMIN_INDEX[1], content_type="text/html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

async def admin_static(request: web.Request) -> web.Response:
    """Sirve JS/CSS del UI admin bajo /admin/static/*."""
    rel = request.match_info["filename"]
    if "/" in rel or "\\" in rel or ".." in rel:
        return web.Response(status=404)
    path = ADMIN_STATIC_DIR / "static" / rel
    if not path.is_file():
        return web.Response(status=404)
    ext = path.suffix.lower()
    ctype = {
        ".js": "application/javascript",
        ".css": "text/css",
        ".html": "text/html",
        ".ico": "image/x-icon",
    }.get(ext, "application/octet-stream")
    # Vendored libs (vendor-<lib>-<version>.js) no cambian con la UI:
    # cache largo para no re-bajarlas en cada F5. El cache-bust es el
    # nombre — un upgrade cambia la versión y con eso la URL, así que
    # el archivo viejo nunca se sirve stale. NO vendorizar sin versión
    # en el nombre: quedaría cacheado 24h sin forma de invalidarlo.
    # El resto va no-store abajo.
    if rel.startswith("vendor-"):
        resp = web.Response(body=path.read_bytes(), content_type=ctype)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
    # Cache-control agresivo: la UI cambia seguido (sub-olas 2.x) y un
    # browser con cache stale puede mostrar modales rotos / versiones
    # viejas del JS sin que el usuario sepa. Forzamos always-revalidate.
    resp = web.Response(body=path.read_bytes(), content_type=ctype)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

async def api_bot_status(request: web.Request) -> web.Response:
    """GET /admin/api/bot/status — estado real del bot de Discord.

    Tres estados, no uno: proceso y gateway se caen por separado y se
    arreglan distinto. Ver el docstring de `bot_control`.
    """
    return web.json_response(await bot_control.probe())

async def api_bot_start(request: web.Request) -> web.Response:
    """POST /admin/api/bot/start — deja el bot conectado a Discord.

    Bloquea hasta que el gateway conecte o se acabe el timeout, así el
    botón de la UI no miente: cuando responde, el estado que devuelve es
    el que hay. 200 igual si falla — el body trae `ok:false` y el
    `detail` accionable, que es lo que la UI pinta.
    """
    return web.json_response(await bot_control.start())

async def api_health(request: web.Request) -> web.Response:
    """GET /admin/api/health — estado del relay."""
    db = request.app[DB_KEY]
    sessions = request.app[SESSIONS_KEY]
    try:
        sessions_list = await sessions.list_sessions()
        sessions_count = len(sessions_list)
    except Exception:
        sessions_count = -1

    projects = await db.list_projects(enabled_only=True)
    # health tiene que ser barato: contamos los .db del cache en vez de
    # spawnear cbm (ver _cbm_indexed_count). El detalle por proyecto
    # —nodes/edges/size— lo sigue trayendo /admin/api/projects, que se
    # pide cuando abres el tab, no cada 15s.
    indexed_count = _cbm_indexed_count()

    try:
        override_to = await db.get_config("expert_timeout_s")
    except Exception:
        override_to = None
    eff_to = float(override_to or relay_config.expert_timeout_s())

    # Autoaprendizaje: count de borradores pendientes para el badge del
    # sidebar (la UI pollea health cada 15s). Best-effort.
    try:
        drafts_pending = await db.count_skill_drafts_pending()
    except Exception:  # noqa: BLE001
        drafts_pending = 0

    return web.json_response({
        "ok": True,
        "relay_version": RELAY_VERSION,
        "projects_total": len(projects),
        "projects_indexed": indexed_count,
        "vscode_sessions_live": sessions_count,
        "cbm_binary": cbm_binary_path() is not None,
        "expert_timeout_s": eff_to,
        "skill_drafts_pending": drafts_pending,
    })

async def api_report(request: web.Request) -> web.Response:
    """GET /admin/api/report?days=N&project=slug — informe de uso.

    UI 2026-07-20: agregados de tokens/runs por día, proyecto y autor.
    El GROUP BY vive en SQL (ver Database.report_usage) porque /chats
    capea en 200 filas. Read-only.
    """
    db = request.app[DB_KEY]
    try:
        days = min(max(int(request.query.get("days", "30")), 1), 365)
    except ValueError:
        days = 30
    project = request.query.get("project") or None
    data = await db.report_usage(days, project_slug=project)
    return web.json_response(data)

_METRICS_STATUSES = ("ok", "error", "running", "cancelled", "split")

_METRICS_ROLES = ("executor", "planner", "verifier", "documenter")

_METRICS_RANGE_MAX_DAYS = 366

def _parse_metrics_window(query: Mapping[str, str]) -> dict:
    """Resuelve la ventana de tiempo de los endpoints de métricas.

    Acepta `from`+`to` (YYYY-MM-DD) o, como fallback, `days=N`. Si vienen
    los tres o `days` mezclado con uno de los dos, gana el par
    `from`/`to` (es lo más explícito que tiene el dashboard). Devuelve
    siempre algo usable por `Database.metrics_summary/_trends`:

        {"from_date": "YYYY-MM-DD", "to_date": "YYYY-MM-DD", "days": None}
        o {"from_date": "", "to_date": "", "days": 7}

    Si el rango está invertido o la fecha no parsea, devuelve
    `{"error": web.Response(400, ...)}` listo para que el handler lo
    devuelva. No se valida acá si `to` es pasado reciente: el backend
    no tiene reloj de negocio, y "sin datos en ese día" ya es la
    respuesta correcta.
    """
    f = (query.get("from") or "").strip()
    t = (query.get("to") or "").strip()
    if f or t:
        # Si solo viene uno de los dos, el usuario a medio completar
        # inputs: mejor 400 que devolver un rango silencioso.
        if not f or not t:
            return {"error": web.json_response(
                {"ok": False, "error": "from y to deben venir juntos"},
                status=400)}
        try:
            f_date = datetime.strptime(f, "%Y-%m-%d").date()
            t_date = datetime.strptime(t, "%Y-%m-%d").date()
        except ValueError:
            return {"error": web.json_response(
                {"ok": False,
                 "error": "from/to deben ser YYYY-MM-DD"},
                status=400)}
        if t_date < f_date:
            return {"error": web.json_response(
                {"ok": False,
                 "error": "to no puede ser anterior a from"},
                status=400)}
        # Amplitud inclusiva: from=01/01 to=01/01 es 1 día, no 0. El
        # cap en días se hace comparando la diferencia de fechas; el
        # límite de 366 ya cubre el peor caso de año bisiesto.
        span = (t_date - f_date).days + 1
        if span > _METRICS_RANGE_MAX_DAYS:
            return {"error": web.json_response(
                {"ok": False,
                 "error": f"rango máximo {_METRICS_RANGE_MAX_DAYS} días"},
                status=400)}
        return {"from_date": f, "to_date": t, "days": None}
    # Fallback a `days`. Misma lista blanca que ya tenía summary: 1..90,
    # y un valor fuera de rango se ignora silenciosamente (dashboard,
    # no API de escritura). `days=0` o negativo cae al default.
    raw = (query.get("days") or "").strip()
    try:
        d = int(raw) if raw else 7
    except ValueError:
        d = 7
    d = min(max(d, 1), 90)
    return {"from_date": "", "to_date": "", "days": d}

async def api_metrics_summary(request: web.Request) -> web.Response:
    """GET /admin/api/metrics/summary — KPIs y distribuciones.

    Query: `from`/`to` (YYYY-MM-DD, rango absoluto, máximo 366 días) o
    `days` (1-90, fallback); más `project`, `status`, `provider`,
    `role`. `project`/`status` filtran runs; `provider`/`role`
    filtran turnos (ver `Database.metrics_summary`). Un valor fuera
    de la lista blanca se ignora en vez de devolver 400: es un
    dashboard, no una API de escritura, y un filtro mal tipeado no
    debería romper la pantalla.
    """
    db = request.app[DB_KEY]
    q = request.query
    win = _parse_metrics_window(q)
    if "error" in win:
        return win["error"]
    status = q.get("status", "").strip()
    role = q.get("role", "").strip()
    data = await db.metrics_summary(
        win["days"],
        from_date=win["from_date"],
        to_date=win["to_date"],
        project=q.get("project", "").strip(),
        status=status if status in _METRICS_STATUSES else "",
        provider=q.get("provider", "").strip(),
        role=role if role in _METRICS_ROLES else "",
    )
    from . import identity
    if identity.role_of(request) != "owner":
        # Los mensajes de error pueden contener código, rutas o datos de tools.
        data["error_breakdown"] = []
    return web.json_response(data)

async def api_metrics_trends(request: web.Request) -> web.Response:
    """GET /admin/api/metrics/trends — slice diario para gráfica.

    Misma ventana que summary (`from`/`to` o `days`). Acepta los
    mismos `project`/`status` que summary, con la misma lista blanca:
    el gráfico va al lado de los KPIs y tiene que estar filtrado
    igual. `provider`/`role` no aplican (filtran turnos, no runs) y
    se ignoran.
    """
    db = request.app[DB_KEY]
    q = request.query
    win = _parse_metrics_window(q)
    if "error" in win:
        return win["error"]
    status = q.get("status", "").strip()
    return web.json_response(await db.metrics_trends(
        win["days"],
        from_date=win["from_date"],
        to_date=win["to_date"],
        project=q.get("project", "").strip(),
        status=status if status in _METRICS_STATUSES else "",
    ))

async def api_search(request: web.Request) -> web.Response:
    """GET /admin/api/search?q=foo — buscador global de la topbar.

    UI 2026-07-20: para el overlay de Cmd+K. Devuelve projects + chats +
    conversations en una sola respuesta (3 queries chiquitas en SQL).
    Cap por tipo para no explotar el payload si el usuario tipea
    poco (1-2 chars matchean miles).
    """
    q = (request.query.get("q") or "").strip()
    if len(q) < 2:
        return web.json_response({"q": q, "projects": [],
                                  "chats": [], "conversations": []})
    try:
        limit = min(max(int(request.query.get("limit", "8")), 1), 25)
    except ValueError:
        limit = 8
    db = request.app[DB_KEY]
    data = await db.global_search(q, limit=limit)
    return web.json_response(data)

_LOG_BUFFER: collections.deque = collections.deque(maxlen=2000)

_log_handler_installed = False

class _RingLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _LOG_BUFFER.append({
                "ts": time.strftime(
                    "%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage()[:500],
                # Puesto por logctx.ChatContextFilter. El getattr cubre
                # los records que entran antes de que create_app instale
                # el filter (arranque) y los de los tests.
                "chat_id": getattr(record, "chat_id", ""),
                "project": getattr(record, "project", ""),
            })
        except Exception:  # noqa: BLE001 — un log NUNCA rompe nada
            pass

def _install_ring_handler() -> None:
    global _log_handler_installed
    if _log_handler_installed:
        return
    h = _RingLogHandler(level=logging.INFO)
    # Este handler se instala DESPUÉS del bootstrap de create_app, así
    # que no lo alcanzó el loop que filtra los handlers de ahí: se lo
    # ponemos acá o el ring buffer queda sin chat_id.
    h.addFilter(logctx.ChatContextFilter())
    logging.getLogger().addHandler(h)
    _log_handler_installed = True

async def api_logs(request: web.Request) -> web.Response:
    """GET /admin/api/logs?limit=200&level=WARNING&chat=<id> — ring buffer.

    `chat` es la razón de ser de todo esto: filtrar por un run concreto
    para poder contestar "qué pasó en este chat" sin reconstruirlo a
    mano desde la DB. Acepta el id completo o el prefijo corto que
    muestra la UI.
    """
    try:
        limit = min(max(int(request.query.get("limit", "200")), 1), 2000)
    except ValueError:
        limit = 200
    level = (request.query.get("level") or "").upper()
    chat = (request.query.get("chat") or "").strip().lower()
    logs = list(_LOG_BUFFER)
    if level in ("WARNING", "ERROR"):
        keep = {"WARNING", "ERROR", "CRITICAL"} if level == "WARNING" \
            else {"ERROR", "CRITICAL"}
        logs = [l for l in logs if l["level"] in keep]
    if chat:
        logs = [l for l in logs
                if (l.get("chat_id") or "").lower().startswith(chat)]
    return web.json_response({"logs": logs[-limit:]})
