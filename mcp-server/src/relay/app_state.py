"""Estado tipado compartido por la aplicación aiohttp.

RUNNING_KEY contiene ejecuciones activas; BG_TASKS_KEY conserva las tasks
hasta terminar persistencia y notificaciones. GRAFOS_KEY permite localizar
y cancelar la task de un grafo. Las claves se crean una sola vez aquí,
tanto al importar el servidor como al iniciarlo con ``python -m``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp import web

from .commands import CommandRegistry
from .db import Database
from .mcp_installer import McpInstaller
from .mcp_pool import McpPool
from .notify import NotifyClient
from .sessions import SessionRegistry
from .skills import SkillBrowser, SkillCache

SESSIONS_KEY: web.AppKey[SessionRegistry] = web.AppKey("sessions", SessionRegistry)
NOTIFY_KEY: web.AppKey[NotifyClient] = web.AppKey("notify", NotifyClient)
SKILLS_KEY: web.AppKey[SkillCache] = web.AppKey("skills", SkillCache)
DB_KEY: web.AppKey[Database] = web.AppKey("db", Database)
COMMANDS_KEY: web.AppKey[CommandRegistry] = web.AppKey("commands", CommandRegistry)
RUNNING_KEY: web.AppKey[dict] = web.AppKey("running", dict)
BG_TASKS_KEY: web.AppKey[set] = web.AppKey("bg_tasks", set)
GRAFOS_KEY: web.AppKey[dict] = web.AppKey("grafos", dict)
PROGRESS_KEY: web.AppKey[dict] = web.AppKey("progress", dict)
BIND_HOST_KEY: web.AppKey[str] = web.AppKey("bind_host", str)
SWEEPER_KEY: web.AppKey[asyncio.Task] = web.AppKey("sweeper", asyncio.Task)
EXPORT_RETRY_KEY: web.AppKey[asyncio.Task] = web.AppKey(
    "export_retry", asyncio.Task)
CBM_WATCHER_KEY: web.AppKey[asyncio.Task] = web.AppKey("cbm_watcher", asyncio.Task)
CBM_WARMUP_KEY: web.AppKey[asyncio.Task] = web.AppKey("cbm_warmup", asyncio.Task)
STATE_DIR_KEY: web.AppKey[Path] = web.AppKey("state_dir", Path)
NIGHT_KEY: web.AppKey[dict] = web.AppKey("night", dict)
MCP_POOL_KEY: web.AppKey[McpPool] = web.AppKey("mcp_pool", McpPool)
MCP_REAPER_KEY: web.AppKey[asyncio.Task] = web.AppKey("mcp_reaper", asyncio.Task)
MCP_HEALTH_PROBE_KEY: web.AppKey[asyncio.Task] = web.AppKey(
    "mcp_health_probe", asyncio.Task)
MCP_INSTALLER_KEY: web.AppKey[McpInstaller] = web.AppKey("mcp_installer", McpInstaller)
SKILL_BROWSER_KEY: web.AppKey[SkillBrowser] = web.AppKey(
    "skill_browser", SkillBrowser)
