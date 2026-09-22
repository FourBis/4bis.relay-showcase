"""Sesión compartida de Codebase Memory y fallback al CLI."""
from __future__ import annotations
import asyncio
import contextlib
import json
import logging
import os
import shutil
from pathlib import Path
from pydantic_ai.exceptions import ModelRetry
from typing import Any, Optional

logger = logging.getLogger("relay.experts")


def cbm_binary_path() -> str | None:
    """Devuelve la ruta absoluta al binario de codebase-memory-mcp, o None.

    Orden de búsqueda (ADR-017):
    1. shutil.which() en el PATH del proceso.
    2. Ubicación estándar del install.ps1 en Windows.

    Esto resuelve [WinError 2] cuando el relay corre desde un shell
    que NO heredó el PATH user-level actualizado por install.ps1.
    """
    found = shutil.which("codebase-memory-mcp")
    if found:
        return found
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "codebase-memory-mcp" / "codebase-memory-mcp.exe",
        Path.home() / "AppData" / "Local" / "Programs" / "codebase-memory-mcp" / "codebase-memory-mcp.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


#: Sesión MCP persistente contra cbm. Un solo proceso sirve a TODOS los
#: proyectos (el `project` va como arg de cada tool), así que no hay nada
#: que keyear: un toolset de módulo alcanza.
#:
#: ADR-017 mandaba one-shot CLI porque el stdio de cbm se colgaba con los
#: clientes reales — verificado de nuevo 2026-07-21: el 0.8.1 timeoutea en
#: `initialize()` con mcp.ClientSession Y con pydantic_ai.MCPToolset. El
#: binario que buildeamos (rama `fourbis` de codebase-memory-mcp, commits
#: b41463c + a3867f5) lo arregla, y medido acá: ~335ms por call contra
#: ~1000ms del spawn one-shot (que es el costo de cargar la imagen del
#: exe, NO antivirus — ver el docstring de `cbm_cli_call`).
#:
#: Si la sesión no levanta — binario viejo sin los parches, handshake
#: colgado, lo que sea — se apaga sola y todo cae al CLI: más lento,
#: mismo resultado. CBM_MCP_SESSION=0 fuerza el CLI a mano.
_cbm_toolset: Any = None
_cbm_transport: Any = None   # lo que hay que cerrar para matar el subprocess
_cbm_toolset_lock = asyncio.Lock()
_cbm_session_off = False


async def close_cbm_session() -> None:
    """Mata el subprocess de la sesión (server._on_cleanup).

    Sin esto el cbm de 273MB sobrevive al relay: es un hijo nuestro, y
    `stop.ps1` solo matchea `relay.server` en la CommandLine."""
    global _cbm_toolset, _cbm_transport
    transport, _cbm_toolset, _cbm_transport = _cbm_transport, None, None
    if transport is not None:
        with contextlib.suppress(Exception):
            await transport.close()


async def _cbm_session_call(tool: str, args: dict, *, timeout: float) -> Optional[str]:
    """cbm por sesión MCP persistente, o None si no está disponible.

    None significa "caé al CLI", nunca "falló la query": un problema de
    transporte no puede tumbar el run del experto.
    """
    global _cbm_toolset, _cbm_transport, _cbm_session_off
    if _cbm_session_off or os.environ.get("CBM_MCP_SESSION") == "0":
        return None
    bin_path = cbm_binary_path()
    if bin_path is None:
        return None
    try:
        async with _cbm_toolset_lock:
            if _cbm_toolset is None:
                from .admin_cbm import _cbm_env
                from .mcp_pool import make_toolset
                # env completo a propósito: es NUESTRO binario (no un MCP
                # de terceros como los del pool) y el camino CLI ya le
                # pasaba _cbm_env() entero. Paridad, no descuido.
                env = _cbm_env()
                # install_dir = cwd del subprocess (ver mcp_pool.make_toolset).
                # Sin esto, con repo_path="" el cwd cae al del relay y cbm
                # auto-indexa/auto-vigila el repo del relay mismo (medido
                # 2026-09-02: un archivo nuevo ahí disparó watcher.reindex
                # solo). CBM_CACHE_DIR no es un repo — cwd neutro. `cwd`
                # inexistente revienta CreateProcess en Windows, así que lo
                # creamos si hace falta.
                # Efecto secundario conocido y ACEPTADO: cbm auto-indexa el
                # cwd que le toque, sea cual sea, y acá falla con 2x
                # `index.supervisor.worker_failed exit_code=1` por boot. No
                # deja artefacto (no se crea .db para ese pseudo-proyecto)
                # ni rompe nada: es ruido de log. Antes de "arreglarlo" con
                # un directorio vacío dedicado, saber que YA SE PROBÓ
                # (2026-09-02) y da exactamente el mismo ruido — cbm
                # auto-indexa el dir vacío igual. La única palanca real
                # sería `cbm config set auto_index false`, que persiste
                # GLOBAL y afectaría a cbm para los otros 40+ clientes de
                # esta máquina; por eso no se toca.
                cache_dir = env.get("CBM_CACHE_DIR")
                if cache_dir:
                    Path(cache_dir).mkdir(parents=True, exist_ok=True)
                _cbm_toolset, _cbm_transport = make_toolset(
                    {"transport": "stdio", "command": bin_path, "args": [],
                     "env": env, "install_dir": cache_dir},
                    "", keep_alive=True)
        # keep_alive=True: el subprocess sobrevive al salir del contexto,
        # así que esto re-usa el proceso en vez de spawnear (mismo gesto
        # que mcp_pool._probe_via_context).
        async with _cbm_toolset:
            res = await asyncio.wait_for(
                _cbm_toolset.direct_call_tool(tool, args), timeout=timeout)
    except ModelRetry as e:
        # cbm señaliza errores de dominio (función inexistente, proyecto
        # sin indexar) como error de tool, y pydantic-ai los sube como
        # ModelRetry con el JSON útil adentro —incluido el `hint`. Es un
        # RESULTADO, no una falla de transporte: se devuelve tal cual y la
        # sesión queda VIVA. Antes caía en el `except Exception` de abajo,
        # y un solo nombre de función inexistente prendía
        # `_cbm_session_off` para todo el proceso: cada llamada posterior
        # se degradaba al CLI de ~1.13s (medido 2026-09-02).
        # Acá ModelRetry nunca es un timeout: este toolset se crea con
        # `mcp_pool.make_toolset` directo, SIN CappedToolset; los timeouts
        # llegan como asyncio.TimeoutError al `except Exception`.
        return str(e)
    except Exception as e:  # noqa: BLE001 — TimeoutError incluido
        logger.warning(
            "cbm: sesión MCP no disponible (%r), caigo a CLI one-shot "
            "por lo que queda del proceso", e)
        _cbm_session_off = True
        await close_cbm_session()
        return None
    if isinstance(res, str):
        return res
    return json.dumps(res, ensure_ascii=False, default=str)


async def cbm_call(tool: str, args: dict, *, timeout: float = 30.0) -> str:
    """Llama un tool de cbm y devuelve el JSON final como string.

    Sesión MCP persistente si se puede (~335ms), CLI one-shot si no
    (~1000ms). El resultado es el mismo JSON en los dos caminos.
    """
    out = await _cbm_session_call(tool, args, timeout=timeout)
    if out is not None:
        return out
    return await cbm_cli_call(tool, args, timeout=timeout)


async def cbm_cli_call(tool: str, args: dict, *, timeout: float = 30.0) -> str:
    """Llama al binario cbm por CLI y devuelve el JSON final como string.

    Por qué CLI y no sesión MCP persistente: el binario (0.8.1) procesa
    stdin recién al EOF — no responde con el pipe abierto (verificado
    2026-07-19), así que cada llamada es un proceso nuevo por diseño.
    OJO: cada spawn paga ~1.1s de piso; la query en sí es de <10ms.
    Causa: cargar e inicializar la imagen del exe (295MB en 0.10.0)
    ANTES de `main()` — `--version` y `--help`, que no hacen trabajo,
    cuestan exactamente lo mismo. Escala con el tamaño, ~4.8ms/MB
    (`gh.exe`, 40MB, paga ~200ms por el mismo motivo).

    NO es Defender, aunque este docstring lo afirmó hasta 2026-09-01.
    Descartado con medición: con `ExclusionPath` sobre el exe Y sobre
    su carpeta (confirmadas con `Get-MpPreference` elevado) el spawn
    quedó en 1141ms contra 1201ms sin exclusión, y una copia del mismo
    exe en una ruta NO excluida costó 1193ms. **No volver a proponer
    exclusiones de antivirus para esto**: es un agujero permanente a
    cambio de ruido. Los fixes reales son achicar el binario (sidecar
    de datos, upstream en el repo cbm) o no spawnear — ver `cbm_call`,
    que reusa la sesión MCP persistente y amortiza el spawn a uno por
    proceso del relay.
    ponytail: se tolera el spawn tax por tool call (los runs igual
    tardan decenas de segundos contra el LLM).
    """
    bin_path = cbm_binary_path()
    if bin_path is None:
        return '{"error": "codebase-memory-mcp no instalado"}'
    env = os.environ.copy()  # hereda CBM_CACHE_DIR del .env del relay
    proc = await asyncio.create_subprocess_exec(
        bin_path, "cli", tool, json.dumps(args),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return json.dumps({"error": f"cbm cli timeout ({timeout}s)"})
    text = stdout.decode("utf-8", errors="replace").strip()
    # cbm mezcla logs level=info con JSON final; agarramos el último JSON.
    last_json = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("{") or s.startswith("["):
            last_json = s
    if last_json:
        return last_json
    # Si no devolvió JSON, devolvemos stderr como error.
    err = stderr.decode("utf-8", errors="replace").strip()
    return json.dumps({"error": f"cbm no devolvió JSON. stderr={err[:200]}"})
