"""Pool de MCP on-demand — plan MCP_REGISTRY F1.

Tres piezas del plan §4:
  1. La SELECCIÓN vive en experts.run_expert (query al catálogo F0).
  2. SPIN-UP PEREZOSO: `McpPool.acquire()` levanta el proceso stdio al
     primer uso, con probe de handshake bajo timeout (obligatorio por
     el historial de stdio colgándose en Windows — ADR-017). Si el
     handshake no completa, el MCP se saltea con warning: degradación
     limpia, el run sigue sin ese toolset.
  3. REAPER: el subprocess sobrevive entre runs (keep_alive=True de
     fastmcp) y `reap_idle()` lo mata tras `idle_timeout_s` sin uso.
     Bonus: esto arregla el leak del código pre-F1, donde cada run
     creaba un StdioTransport nuevo con keep_alive=True default y el
     subprocess quedaba huérfano al descartar la instancia.

Solo se poolean los stdio (hay proceso que mantener vivo). Los http/sse
se arman por run: la conexión es barata y no hay subprocess.

Secretos (decisión 4 del plan): valores con forma ``secret:NAME`` se
resuelven desde Config. ``env:NAME`` se conserva como alias legado.

F2 reutiliza `probe_handshake()` (exportada a nivel de módulo) para
hacer un probe one-shot durante la install desde GitHub — sin entrar
al pool, no hay state que mantener en ese momento.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

from pydantic_ai.mcp import MCPToolset, StdioTransport

from . import config

logger = logging.getLogger("relay.mcp_pool")

REAP_INTERVAL_S = 60.0


def resolve_env_refs(env: dict) -> dict:
    """Resuelve refs secret/env desde Config sin exponer el valor."""
    out = {}
    for k, v in (env or {}).items():
        if isinstance(v, str) and v.startswith(("secret:", "env:")):
            name = v.split(":", 1)[1]
            resolved = config.get(name)
            if not resolved:
                raise ValueError(f"secreto {name!r} no está configurado")
            out[k] = resolved
        else:
            out[k] = v
    return out


#: El SDK de MCP no hereda el entorno del padre: completa lo que le
#: pasamos con un whitelist de ~12 vars que NO incluye ProgramFiles*.
#: Sin esas, NuGet resuelve el directorio de config machine-wide a null
#: y todo `dotnet` que corra el experto muere con
#: "Value cannot be null. (Parameter 'path1')" en NuGet.targets.
#: No mergeamos os.environ entero a propósito: el relay spawnea MCPs de
#: terceros y su entorno tiene API keys.
_TOOLCHAIN_PASSTHROUGH = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")


def _toolchain_env() -> dict[str, str]:
    """Vars de sistema que la toolchain necesita, + nodeReuse off.

    MSBUILDDISABLENODEREUSE: los nodos worker de MSBuild son globales y
    sobreviven ~15 min al comando. Si uno arranca roto acá adentro, el
    fallo queda cacheado en su type initializer y contamina los builds
    del usuario fuera del relay. Sin reuse, muere con el comando.
    """
    env = {k: os.environ[k] for k in _TOOLCHAIN_PASSTHROUGH if k in os.environ}
    env["MSBUILDDISABLENODEREUSE"] = "1"
    return env


def _resolve_command(command: str) -> str:
    """Windows: "npx"/"node" pelados no se pueden spawnear directo
    (WinError 123/193) y shutil.which(command) puede devolver el shim
    bash sin extensión (p.ej. C:\\nvm4w\\nodejs\\npx). Probar primero
    las extensiones ejecutables reales; en otros SO which alcanza.
    """
    if os.name == "nt" and not Path(command).suffix:
        for ext in (".exe", ".cmd", ".bat"):
            found = shutil.which(command + ext)
            if found:
                return found
    return shutil.which(command) or command


def make_toolset(
    cfg: dict, repo_path: str, *, keep_alive: bool = False,
) -> tuple[Optional[MCPToolset], Optional[StdioTransport]]:
    """Config (fila del catálogo o entrada legacy del blob) → toolset.

    Devuelve (toolset, transport); transport solo para stdio (es lo que
    el pool necesita para matar el subprocess). None si la config es
    inválida — el caller decide loggear/saltear.
    """
    transport = cfg.get("transport", "stdio")
    init_timeout = config.mcp_init_timeout_s()
    # 2026-08-16: el read_timeout de pydantic-ai (300s por default) es
    # otro techo por tool-call, independiente del nuestro. Si queda por
    # debajo de `tool_call_timeout_s`, el que corta es él —con un error
    # de transporte, no con el ModelRetry accionable de CappedToolset—,
    # y subir nuestro techo no serviría de nada. Se alinea con un margen
    # para que el nuestro dispare primero y el modelo reciba el mensaje
    # útil.
    read_timeout = float(cfg.get("_tool_call_timeout_s") or config.tool_call_timeout_s()) + 30.0
    if transport == "stdio":
        command = cfg.get("command") or "python"
        if command == "python":
            command = sys.executable
        else:
            command = _resolve_command(command)
        env = resolve_env_refs(cfg.get("env", {}))
        for k, v in _toolchain_env().items():
            env.setdefault(k, v)
        env.setdefault("FOURBIS_WORKSPACE", repo_path)
        # `@playwright/mcp` no escribe donde le pidas: su sandbox permite
        # SOLO dos raíces, el output dir y el cwd. Medido el 19/8 con el
        # server real:
        #
        #   File access denied: <ruta> is outside allowed roots.
        #   Allowed roots: <output_dir>, <cwd>
        #
        # El cwd tiene que ser `install_dir` (ahí vive el `npx -y .`), así
        # que si el output dir no es el repo, el experto NO puede guardar
        # un screenshot dentro del proyecto —que es el punto de pedirle
        # capturas—. Por eso va el repo y no un directorio del relay.
        #
        # Lo que se paga: los artefactos propios del server (`page-*.yml`
        # de cada snapshot, `console-*.log`) caen en la raíz del repo y
        # ensucian el `git status`. Solo pasa en los runs que usan el
        # browser. Si molesta, el upgrade es agregarlos a
        # `.git/info/exclude` cuando se adjunta la capability.
        #
        # OJO: un `filename` relativo en `browser_take_screenshot` NO se
        # resuelve contra el output dir sino contra el cwd. El prompt le
        # pide al experto ruta absoluta (ver `EVIDENCE_BLOCK`).
        if repo_path:
            env.setdefault("PLAYWRIGHT_MCP_OUTPUT_DIR", repo_path)
        st = StdioTransport(
            command=command,
            args=list(cfg.get("args", [])),
            env=env,
            # Filas instaladas desde GitHub (`npx -y .`) corren sobre su
            # clone (install_dir); el resto sobre el repo del proyecto.
            # cwd="" en Windows revienta CreateProcess (WinError 123) —
            # sin nada, que herede el cwd del relay.
            cwd=cfg.get("install_dir") or repo_path or None,
            keep_alive=keep_alive,
        )
        return MCPToolset(
            st, init_timeout=init_timeout, read_timeout=read_timeout), st
    if transport in ("http", "sse"):
        return MCPToolset(
            cfg["url"], init_timeout=init_timeout,
            read_timeout=read_timeout), None
    raise ValueError(f"transport desconocido {transport!r}")


async def probe_handshake(cfg: dict, *, repo_path: str = "") -> None:
    """Probe one-shot del handshake de un MCP stdio/http (F1 + F2).

    Reusada por `mcp_installer` (F2) antes de crear la fila en
    `mcp_servers`. No usa el pool: no queremos state vivo entre runs,
    solo "este binario responde sí o no". Levanta cualquier excepción
    si falla (timeout, handshake, missing deps). El caller decide qué
    hacer (rechazar la install, marcar health=handshake_failed, etc.).

    Ponyscope: déjala honesta. Si falla → no se crea la fila.
    """
    toolset, _transport = make_toolset(cfg, repo_path, keep_alive=False)
    async with toolset:
        pass


async def probe_and_store_health(db: Any, row: dict) -> tuple[str, str]:
    """Probe una fila del catálogo y persiste su `health`.

    Devuelve `(health, error)` — `health` es `"ok"`,
    `"handshake_failed"` o `"skipped"`; `error` es el repr de la
    excepción cuando falló, `""` cuando no.

    Existe para que el probe del boot y el botón de re-chequeo de la
    Admin UI escriban el MISMO valor. Antes el health se seteaba solo al
    boot, así que arreglar un MCP roto —cambiarle los args, agregar la
    credencial que faltaba— no se podía verificar sin reiniciar el relay
    entero, y la tabla seguía mostrando `handshake ✗` sobre algo que ya
    funcionaba.

    `skipped` se devuelve cuando el MCP declara env vars que no están
    seteadas en el entorno (postgres-mcp necesita POSTGRES_MCP_URI, por
    ejemplo). Sin este corte, el probe intenta spawn-ear el subprocess
    igual y muere con `handshake_failed` cada vez — y el log del boot
    arrastra un warning permanente que confunde y que NO se arregla
    reiniciando. 2026-08-28.
    """
    # 2026-08-28: chequeo de env vars antes de spawn. El formato en la
    # DB es `{"DATABASE_URI": "env:POSTGRES_MCP_URI"}` — el valor "env:X"
    # significa "leer de os.environ[X]". Si falta alguna, skip limpio.
    import os as _os
    import json as _json
    env_json = row.get("env") or "{}"
    try:
        env_map = _json.loads(env_json) if isinstance(env_json, str) else env_json
    except Exception:
        env_map = {}
    for val in (env_map or {}).values():
        if isinstance(val, str) and val.startswith("env:"):
            ref = val[4:]
            if not _os.environ.get(ref):
                if row.get("health") != "skipped":
                    await db.upsert_mcp_server(
                        {"name": row["name"], "health": "skipped"})
                return "skipped", f"env var {ref!r} no seteada"
    try:
        await asyncio.wait_for(probe_handshake(row, repo_path=""),
                               timeout=config.mcp_init_timeout_s())
        health, error = "ok", ""
    except Exception as e:  # noqa: BLE001 — cualquier falla es "no responde"
        health, error = "handshake_failed", repr(e)
    if row.get("health") != health:
        await db.upsert_mcp_server({"name": row["name"], "health": health})
    return health, error


class _Entry:
    __slots__ = ("toolset", "transport", "last_used", "idle_timeout_s",
                 "updated_at")

    def __init__(self, toolset, transport, idle_timeout_s, updated_at):
        self.toolset = toolset
        self.transport = transport
        self.last_used = time.monotonic()
        self.idle_timeout_s = idle_timeout_s
        self.updated_at = updated_at


class McpConfigBusy(RuntimeError):
    """La nueva configuración debe esperar a que termine la sesión activa."""


class McpPool:
    """Toolsets stdio vivos entre runs, keyed por (name, repo_path).

    `acquire(row, repo_path)` → MCPToolset listo (probe de handshake
    hecho) o None si no levantó. El agente entra/sale el contexto por
    run; con keep_alive=True el subprocess queda para el siguiente.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, row: dict, repo_path: str) -> Optional[MCPToolset]:
        key = (row["name"], os.path.normcase(str(Path(repo_path).resolve())) if repo_path else "")
        # El timestamp de SQLite tiene resolución de segundos. La config
        # efectiva detecta también dos ediciones dentro del mismo segundo.
        revision = json.dumps({k: row.get(k) for k in (
            "command", "args", "env", "install_dir", "transport", "url",
            "idle_timeout_s", "_tool_call_timeout_s")}, sort_keys=True)
        async with self._lock:
            entry = self._entries.get(key)
            # Config editada en la Admin UI → rebuild (el updated_at cambia).
            if entry is not None and entry.updated_at != (row.get("updated_at"), revision):
                if entry.toolset.is_running:
                    logger.warning("mcp %r: config pendiente hasta terminar la sesión activa", row["name"])
                    raise McpConfigBusy(row["name"])
                await self._close_entry(key, entry, reason="config cambió")
                entry = None
            if entry is None:
                try:
                    toolset, transport = make_toolset(
                        row, repo_path, keep_alive=True)
                except (ValueError, KeyError) as e:
                    logger.warning("mcp %r: config inválida (%s), salteo",
                                   row["name"], e)
                    return None
                entry = _Entry(toolset, transport,
                               row.get("idle_timeout_s") or 300,
                               (row.get("updated_at"), revision))
                self._entries[key] = entry
            # Probe: entra y sale del contexto bajo timeout. Con
            # keep_alive el subprocess (y la sesión fastmcp) quedan
            # vivos, así que el re-enter del agente es barato.
            try:
                await asyncio.wait_for(
                    _probe_via_context(entry.toolset),
                    timeout=config.mcp_init_timeout_s())
            except asyncio.CancelledError:
                await self._close_entry(key, entry, reason="handshake cancelado")
                raise
            except Exception as e:  # noqa: BLE001 — TimeoutError incluido
                logger.warning(
                    "mcp %r: handshake no completó (%r), salteo (¿stdio "
                    "colgado? sube FOURBIS_MCP_INIT_TIMEOUT si es lento)",
                    row["name"], e)
                await self._close_entry(key, entry, reason="handshake falló")
                return None
            entry.last_used = time.monotonic()
            return entry.toolset

    async def _close_entry(self, key, entry: _Entry, *, reason: str) -> None:
        self._entries.pop(key, None)
        if entry.transport is not None:
            with contextlib.suppress(Exception):
                await entry.transport.close()
        logger.info("mcp %r: proceso cerrado (%s)", key[0], reason)

    async def reap_idle(self, *, now: Optional[float] = None) -> list[str]:
        """Mata subprocesos sin uso hace > idle_timeout_s. Devuelve los
        nombres reapeados (para logging/tests)."""
        n = now if now is not None else time.monotonic()
        reaped = []
        async with self._lock:
            for key, entry in list(self._entries.items()):
                if entry.toolset.is_running:
                    continue  # run en curso — nunca matar abajo del agente
                if n - entry.last_used > entry.idle_timeout_s:
                    await self._close_entry(key, entry, reason="idle")
                    reaped.append(key[0])
        return reaped

    async def reaper_loop(self) -> None:
        """Background task del server. Cancelable."""
        while True:
            await asyncio.sleep(REAP_INTERVAL_S)
            try:
                await self.reap_idle()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("reaper MCP rompió (sigo)")

    async def shutdown(self) -> None:
        async with self._lock:
            for key, entry in list(self._entries.items()):
                await self._close_entry(key, entry, reason="shutdown")


async def _probe_via_context(toolset: MCPToolset) -> None:
    """Wrapper interno: el pool reusa el mismo gesto de probe_handshake
    pero ya tenemos la instancia viva. Igual concepto."""
    async with toolset:
        pass
