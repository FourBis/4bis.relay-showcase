"""Dependencias y piezas comunes del servidor aiohttp.

Los módulos de dominio importan estas piezas explícitamente. Este módulo no
registra rutas ni importa la composición de la aplicación.
"""
from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import urlsplit
import json
import logging
import time
from typing import Optional

from aiohttp import web

from . import __version__ as RELAY_VERSION
from . import config as relay_config, coordination
from .app_state import (
    BG_TASKS_KEY,
    BIND_HOST_KEY,
    CBM_WARMUP_KEY,
    CBM_WATCHER_KEY,
    COMMANDS_KEY,
    DB_KEY,
    EXPORT_RETRY_KEY,
    GRAFOS_KEY,
    MCP_HEALTH_PROBE_KEY,
    MCP_INSTALLER_KEY,
    MCP_POOL_KEY,
    MCP_REAPER_KEY,
    NIGHT_KEY,
    NOTIFY_KEY,
    PROGRESS_KEY,
    RUNNING_KEY,
    SESSIONS_KEY,
    SKILLS_KEY,
    SKILL_BROWSER_KEY,
    STATE_DIR_KEY,
    SWEEPER_KEY,
)
from .sessions import SessionRegistry, validate_name, validate_sid
from .tools import _registry as tool_registry

logger = logging.getLogger("relay.server")

_log_utf8_configured = False


def _get_api_key() -> str:
    """Read the current API key so panel changes apply to new requests."""
    return relay_config.get("RELAY_API_KEY").strip()


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


LAN_SAFE_EXACT = frozenset((
    "/health",
    "/agents/handshake",
    "/sessions",
))
LAN_SAFE_PREFIXES = ()
_LOCALHOST_PEERS = frozenset({"127.0.0.1", "::1"})


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


# Fire-and-forget jobs must stay strongly referenced until completion.
_bg_tasks: set[asyncio.Task] = set()


def _spawn_bg(coro, *, hold_workspace: bool = True) -> asyncio.Task:
    """Schedule a background coroutine and retain it until it finishes."""
    task = asyncio.create_task(coro)
    if hold_workspace:
        coordination.hold_current(task)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


def _rpc_error(req_id, code, message):
    return web.json_response(
        {"jsonrpc": "2.0", "id": req_id,
         "error": {"code": code, "message": message}},
        status=200,
    )


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
