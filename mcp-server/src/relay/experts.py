"""Expertos pydantic-ai — ADR-012: el LLM vive en el relay.

Un experto NO es un módulo Python por proyecto: es la fila de la tabla
`projects` (system_prompt + mcp_servers + defaults_json) más un runner
genérico. Ponytail: config es data, no código. Si un proyecto algún
día necesita tools Python propias, se agrega una columna
`expert_module` — upgrade path, no ahora.

System prompt final de un experto (en orden):
    1. Filosofía Ponytail (~/.copilot/copilot-instructions.md, best-effort)
    2. projects.system_prompt (lo específico del repo)
    3. Índice de skills (SkillCache, ADR-010 — misma fuente que VS Code)
    4. Bloque workspace (ADR-011, armado desde repo_path)

Tools: MCP nativo de pydantic-ai (MCPToolset + StdioTransport /
StreamableHttpTransport). No hay wrapper artesanal.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
from collections import deque
import os
import re
import shutil
import subprocess
import time

import anyio
import httpx
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

# ADR-034 (anulado 2026-07-17): evaluamos mover los imports de
# pydantic_ai/mcp a scope de función para acelerar el startup del
# relay. NO funcionó: 12 tests hacen `patch("relay.<mod>.Agent")`
# (admin/memory/experts) y esperan el símbolo a nivel de módulo.
# El startup queda en ~1.9s con cold .pyc — aceptable. Si vuelve a
# ser problema, usar `__getattr__` lazy (PEP 562) en vez de mover
# imports a scope de función.

from pydantic_ai import Agent, Tool
from pydantic_ai.exceptions import (
    ModelHTTPError, ModelRetry, UnexpectedModelBehavior, UsageLimitExceeded)
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.toolsets import FunctionToolset, WrapperToolset

# El error de protocolo de un MCP. Import defensivo: si algún día el
# paquete `mcp` no está (pydantic-ai lo trae como extra), el relay
# arranca igual y el guard de abajo simplemente no aplica.
try:
    from mcp.shared.exceptions import McpError
except ImportError:  # pragma: no cover
    class McpError(Exception):  # type: ignore[no-redef]
        """Placeholder: nunca se levanta si no hay MCP instalado."""
from pydantic_ai.usage import UsageLimits

from . import attachments as attachments_mod
from . import config
from . import dbtool
from . import files as files_mod
from . import shell as shell_mod
from . import file_tools as file_tools_mod
from . import shell_tools as shell_tools_mod
from . import mcp_pool as mcp_pool_mod
from .sessions import extract_workspace_block
# El criterio de "esto se reintenta / esto no" ante un fallo del
# proveedor ya estaba escrito y medido para el planificador; el ejecutor
# usa EL MISMO en vez de tener su propia tabla de status codes. Import a
# nivel de módulo: planificador importa experts adentro de una función,
# así que no hay ciclo.
from .planificador import _que_hacer as _que_hacer_con_el_proveedor

logger = logging.getLogger("relay.experts")

PONYTAIL_PATH = Path.home() / ".copilot" / "copilot-instructions.md"

# Cache simple del ponytail: (mtime, texto). El archivo casi nunca cambia.
_ponytail_cache: tuple[float, str] | None = None


def _read_ponytail_sync() -> str:
    global _ponytail_cache
    try:
        mtime = PONYTAIL_PATH.stat().st_mtime
        if _ponytail_cache and _ponytail_cache[0] == mtime:
            return _ponytail_cache[1]
        text = PONYTAIL_PATH.read_text(encoding="utf-8").strip()
        _ponytail_cache = (mtime, text)
        return text
    except OSError:
        return ""


async def read_ponytail() -> str:
    """Filosofía base del usuario. Best-effort: sin archivo → ""."""
    return await asyncio.to_thread(_read_ponytail_sync)


def build_model(spec: str) -> Any:
    """`provider:modelo` → objeto modelo pydantic-ai (o string passthrough).

    minimax:*, nvidia:* y ollama:* van como OpenAI-compatible con
    base_url propia. openai:* / anthropic:* se delegan a la inferencia
    nativa de pydantic-ai. `test` → TestModel (sin red).
    """
    if spec == "test":
        from pydantic_ai.models.test import TestModel
        # call_tools=[]: NO llama tools. TestModel por default llama
        # TODAS con args dummy — con write_file/run_shell de un MCP
        # real eso ensuciaría el repo. Para probar tool calls reales:
        # scripts/smoke_expert.py.
        return TestModel(call_tools=[])

    provider_name, _, model_name = spec.partition(":")

    # Provider por tabla (2026-08-18): si la fila del catálogo trae
    # base_url + api_key_env, se arma OpenAI-compatible con eso. Es lo
    # que permite sumar DeepSeek —o cualquier endpoint compatible— con
    # un INSERT en el catálogo, sin tocar este archivo. La key
    # NUNCA sale de la tabla: la tabla dice CÓMO SE LLAMA la env var.
    fila = _catalog.get(spec) or {}
    if fila.get("base_url"):
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        # La key de la fila gana; si no hay, el nombre de env var que
        # diga la fila. Así conviven los dos estilos: los providers
        # viejos pueden referenciar una key y los nuevos se cargan enteros
        # desde la tabla.
        env = (fila.get("api_key_env") or "").strip()
        api_key = (fila.get("api_key") or "").strip() or (
            config.get(env) if env else "")
        if not api_key:
            raise ModelUnavailable(
                f"{spec} no tiene API key: cargala en la fila del modelo "
                + (f"o configurá {env} en el panel del relay." if env
                   else "(campo api_key) o indicá qué env var usar.")
            )
        return OpenAIChatModel(
            model_name or spec,
            provider=OpenAIProvider(
                base_url=fila["base_url"], api_key=api_key),
        )

    if provider_name == "minimax":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        api_key = config.get("MINIMAX_API_KEY")
        if not api_key:
            raise ModelUnavailable(
                "MINIMAX_API_KEY no está configurada. Cargala en el "
                "catálogo del panel o elegí otro modelo."
            )
        base_url = config.get("MINIMAX_BASE_URL")
        return OpenAIChatModel(
            model_name or "MiniMax-M3",
            provider=OpenAIProvider(base_url=base_url, api_key=api_key),
        )
    if provider_name == "nvidia":
        # Endpoints NIM compatibles con OpenAI. Los model_name llevan barra —
        # `nvidia:minimaxai/minimax-m3` — y partition(":") corta en el
        # primer ":", así que la barra viaja intacta.
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        api_key = config.get("NVIDIA_API_KEY")
        if not api_key:
            raise ModelUnavailable(
                "NVIDIA_API_KEY no está seteada. Generá una en "
                "build.nvidia.com/settings/api-keys y cargala en el "
                "catálogo del panel."
            )
        base_url = config.get("NVIDIA_BASE_URL")
        return OpenAIChatModel(
            model_name,
            provider=OpenAIProvider(base_url=base_url, api_key=api_key),
        )
    if provider_name == "ollama":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        base_url = config.get("OLLAMA_BASE_URL")
        return OpenAIChatModel(
            model_name, provider=OpenAIProvider(base_url=base_url, api_key="ollama"),
        )
    api_key = (fila.get("api_key") or "").strip()
    env = (fila.get("api_key_env") or "").strip()
    api_key = api_key or (config.get(env) if env else "")
    if provider_name == "openai" and api_key:
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        return OpenAIChatModel(model_name, provider=OpenAIProvider(api_key=api_key))
    if provider_name == "anthropic" and api_key:
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider
        return AnthropicModel(model_name, provider=AnthropicProvider(api_key=api_key))
    # Filas nativas sin credencial quedan como error explícito: el SDK no
    # debe volver a buscar secretos ocultos en el entorno del proceso.
    if provider_name in {"openai", "anthropic"}:
        raise ModelUnavailable(
            f"{spec} no tiene API key: cargala en el catálogo del panel.")
    return spec


# ---------- catálogo de modelos (2026-08-18) ----------
#
# Lo que la UI ofrece en el selector del chat. `vision` NO es una
# propiedad del modelo sino DEL ENDPOINT: los pesos de minimax-m3 ven
# imágenes, pero servidos por los NIM gratis de NVIDIA el endpoint no
# acepta partes de imagen. Por eso la lista es a mano — el dato se
# comprueba a mano, no se deduce del nombre.
# Catálogo de modelos. La fuente de verdad es la tabla `models` de
# relay.db; acá vive el CACHE, que se llena en el startup igual que el
# de roles: es un lookup por run y la tabla la edita un humano.
#
# `vision` es tri-estado y eso importa: 0 es "medido, no ve", NULL es
# "nadie lo probó". Solo cortamos con el 0. Importar los 102 modelos de
# NVIDIA mete 102 NULL, y tratarlos como ciegos rompería runs que hoy
# andan.
#
# El caso que justifica medir en vez de deducir es glm-5.2: NO devuelve
# error con una imagen adentro, la acepta y contesta igual. Preguntándole
# derecho dice que no la ve. Un modelo así no se detecta mirando errores
# en producción.
_catalog: dict[str, dict] = {}


def load_catalog(rows) -> None:
    """Refresca el cache del catálogo. `rows` son filas de `models`."""
    global _catalog
    _catalog = {r["spec"]: dict(r) for r in rows}
    logger.info("catálogo de modelos: %d", len(_catalog))


def catalog() -> list[dict]:
    return list(_catalog.values())


def has_vision(spec: str) -> bool:
    """¿Ese modelo acepta imágenes?

    True si está medido que ve, y TAMBIÉN si no lo midió nadie: solo
    cortamos cuando SABEMOS que no ve. Asumir lo contrario haría que un
    modelo recién importado se tragara la imagen en silencio, que es
    exactamente el bug que esto viene a arreglar.
    """
    row = _catalog.get(spec)
    if row is None:
        return True
    return row.get("vision") != 0


def resolve_model_spec(model_override: str, project: Optional[dict] = None) -> str:
    """La misma cascada que usa `run_expert`, en un solo lugar.

    Existe para que el guard de imágenes en `/experts/run` mire
    EXACTAMENTE el modelo que va a correr y no una aproximación.
    """
    defaults = (project or {}).get("defaults_json") or {}
    return model_override or defaults.get("model") or config.model_spec()


def structured_output_settings(spec: str) -> Optional[dict]:
    """model_settings para un Agent con `output_type` (structured output).

    DeepSeek v4 (deepseek-v4-pro / -flash) rechaza con 400 "Thinking mode
    does not support this tool_choice" cuando pydantic-ai fuerza
    `tool_choice` — que es exactamente lo que hace al haber `output_type`.
    La doc de v4 dice deshabilitar thinking por `extra_body`.

    Solo para agentes con output_type: el experto normal usa tool_choice
    auto y con thinking anda bien, no le metemos mano.

    Los otros providers (minimax, anthropic, openai) no tienen el bug →
    None, que es "no toques nada".
    """
    return ({"extra_body": {"thinking": {"type": "disabled"}}}
            if "deepseek-v4" in (spec or "").lower() else None)


class ModelUnavailable(RuntimeError):
    """El modelo pedido no se puede armar (falta key, config, etc.)."""


# ---------- F1 (plan MCP_REGISTRY): selección explícita + on-demand ----------

_MCP_FLAG_RE = re.compile(r"(?:^|\s)--(?:con|with)[ =]([\w,\-]+)", re.IGNORECASE)


def parse_mcp_flags(text: str) -> tuple[str, list[str]]:
    """Extrae `--con db` / `--with db,docs` del mensaje del usuario.

    Devuelve (texto limpio, selección). La selección son términos que el
    selector F0 matchea contra capability O name (case-insensitive).
    Varios flags se acumulan.
    """
    selection: list[str] = []
    def _grab(m: re.Match) -> str:
        selection.extend(
            t.strip() for t in m.group(1).split(",") if t.strip())
        return ""
    clean = _MCP_FLAG_RE.sub(_grab, text).strip()
    return clean, selection


# Selección explícita de skills: `--skill pdf` / `--skills pdf,docx`.
# Paralelo a `--con` para MCPs: fuerza una skill al run aunque tenga
# `when: manual` (que si no NO se auto-inyecta). El bloque lo arma
# skills.render_requested_block; el `!ayuda` lista las disponibles.
_SKILL_FLAG_RE = re.compile(r"(?:^|\s)--skills?[ =]([\w,\-]+)", re.IGNORECASE)


def parse_skill_flags(text: str) -> tuple[str, list[str]]:
    """Extrae `--skill pdf` / `--skills pdf,docx` del mensaje del usuario.

    Devuelve (texto limpio, nombres pedidos). Mismos alias-y-coma que
    parse_mcp_flags; los nombres se resuelven contra el dir o el `name`
    del frontmatter (case-insensitive) en skills.render_requested_block.
    """
    names: list[str] = []
    def _grab(m: re.Match) -> str:
        names.extend(t.strip() for t in m.group(1).split(",") if t.strip())
        return ""
    clean = _SKILL_FLAG_RE.sub(_grab, text).strip()
    return clean, names


# Working set scope (2026-07-20d): `--solo src/auth/*` / `--scope a,b`
# acota la atención del experto a esos paths. El valor es un token
# sin espacios (glob(s) separados por coma). Sin espacios porque el
# resto del mensaje es prosa — un glob con espacios sería ambiguo.
_SCOPE_FLAG_RE = re.compile(
    r"(?:^|\s)--(?:solo|scope|only)[ =](\S+)", re.IGNORECASE)


def parse_scope_flags(text: str) -> tuple[str, list[str]]:
    """Extrae `--solo <glob[,glob]>` del mensaje. Devuelve (texto
    limpio, lista de paths/globs). Varios flags se acumulan."""
    scopes: list[str] = []
    def _grab(m: re.Match) -> str:
        scopes.extend(
            t.strip() for t in m.group(1).split(",") if t.strip())
        return ""
    clean = _SCOPE_FLAG_RE.sub(_grab, text).strip()
    return clean, scopes


def scope_block(scopes: list[str]) -> str:
    """Bloque de instrucciones para un run acotado a `scopes`."""
    paths = ", ".join(f"`{s}`" for s in scopes)
    return (
        "## Working set acotado (pedido EXPLÍCITO del usuario)\n"
        f"Limita tu atención a estos paths: {paths}. Ignora el resto del "
        "repo salvo que sea imprescindible para entender esos archivos. "
        "NO edites archivos fuera de ese alcance; si crees que hace falta, "
        "explica por qué en vez de hacerlo."
    )


class _CapabilityRequested(Exception):
    """El LLM pidió una capacidad vía use_capability → re-run con ella."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def _one_per_capability(
    rows: list[dict], selection: set[str] | None = None,
) -> list[dict]:
    """Deja UN MCP por capacidad. Devuelve las filas que se adjuntan.

    Bug fix 2026-08-01: una capacidad puede resolver a varios MCPs
    (entonces `browser` = obscura + playwright-mcp) y `--con browser` los
    adjuntaba a los dos. Como ambos declaraban las mismas tools,
    `CombinedToolset.get_tools` aborta el run entero con
    `UserError: ... defines a tool whose name conflicts with existing
    tool ...: 'browser_close'` — el experto muere antes de escribir una
    línea. Y aunque los nombres no chocaran, dos implementaciones de lo
    mismo pagan dos veces el catálogo de tools en tokens.

    Criterio de desempate, en orden:

    1. El always-on (on_demand=0): el sistema lo da por adjunto siempre,
       no lo puede desplazar un on-demand.
    2. El pedido por NOMBRE exacto en `selection`. Importa en el re-run
       de use_capability: si el humano arrancó con `--con playwright-mcp`
       y después el modelo pide `use_capability("browser")`, la selección
       pasa a ser {playwright-mcp, browser} y sin esto el desempate
       alfabético le cambiaba el MCP a mitad del run.
    3. El primero alfabético.

    2026-08-16: `browser` volvió a resolver a UN solo MCP
    (`playwright-mcp`); obscura salió del catálogo. La función se queda
    igual: el desempate sigue haciendo falta para cualquier capacidad que
    algún día vuelva a tener dos implementaciones, y es lo que evita que
    el run muera con `UserError: ... name conflicts`.
    """
    sel = {s.lower() for s in (selection or set())}
    best: dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: (
            r.get("on_demand", 1),
            0 if (r.get("name") or "").lower() in sel else 1,
            r.get("name") or "")):
        cap = row.get("capability") or row.get("name") or ""
        if cap in best:
            logger.info(
                "mcp %r: no se adjunta, %r ya cubre la capacidad %r "
                "(pídelo por nombre exacto para usar este)",
                row.get("name"), best[cap].get("name"), cap)
            continue
        best[cap] = row
    return list(best.values())


async def _catalog_toolsets(
    db: Any, project: dict, selection: set[str], pool: Any,
    tool_call_timeout: Optional[float] = None,
    inflight: Optional[dict] = None,
    hide_tools: frozenset = frozenset(),
    image_artifacts: Optional[dict] = None,
    vision: bool = True,
) -> tuple[list[Any], list[dict], list[dict]]:
    """Arma los toolsets del run desde el catálogo F0.

    Devuelve (toolsets, adjuntados, visibles): adjuntados = filas cuyos
    toolsets efectivamente entraron; visibles = TODOS los MCPs
    habilitados del proyecto (para el menú de use_capability). TODOS los
    stdio van por el pool (spin-up con probe + reaper); http/sse se arman
    por run. Un MCP que no levanta se saltea (degradación limpia) y su
    health queda en la DB para la Admin UI.

    2026-07-26: el pool era solo para los `on_demand`. Como en la práctica
    todas las filas son always-on (on_demand=0), NINGUNA pasaba por el
    probe: el primer contacto con el subprocess era el `__aenter__` del
    agente, y un `npx -y`/`uvx` que no completa el handshake mataba el run
    (RuntimeError de fastmcp). Poolearlos también les da proceso vivo
    entre runs, que es justo lo que necesitan los que tardan en arrancar.
    """
    repo_path = project["repo_path"]
    sel = list(selection) or None
    rows = _one_per_capability(await db.mcp_servers_for_project(
        project["id"], names=sel, capabilities=sel), selection)
    visible = await db.mcp_servers_for_project(project["id"], all_visible=True)

    toolsets: list[Any] = []
    attached: list[dict] = []
    for row in rows:
        try:
            runtime_row = {**row, "_tool_call_timeout_s": tool_call_timeout}
            if pool is not None and row["transport"] == "stdio":
                toolset = await pool.acquire(runtime_row, repo_path)
                if toolset is None:  # no levantó — queda visible en la UI
                    if row.get("health") != "handshake_failed":
                        await db.upsert_mcp_server(
                            {"name": row["name"],
                             "health": "handshake_failed"})
                    continue
                if row.get("health") != "ok":
                    await db.upsert_mcp_server(
                        {"name": row["name"], "health": "ok"})
            else:
                toolset, _ = mcp_pool_mod.make_toolset(
                    runtime_row, repo_path, keep_alive=False)
            # Cap/elisión (ahorro de tokens) + guard de arranque. Se
            # envuelve ACÁ y no en run_expert porque el re-run de
            # use_capability reasigna la lista desde esta misma función:
            # envolver allá dejaba los toolsets del segundo round crudos.
            capped = CappedToolset(
                wrapped=toolset, timeout=tool_call_timeout,
                image_artifacts=image_artifacts, vision=vision,
                inflight=inflight if inflight is not None else {})
            if hide_tools:
                capped = CappedToolset(
                    wrapped=HideToolsToolset(
                        wrapped=toolset, hidden=hide_tools),
                    image_artifacts=image_artifacts, vision=vision,
                    timeout=tool_call_timeout,
                    inflight=inflight if inflight is not None else {})
            toolsets.append(OptionalToolset(wrapped=capped))
            attached.append(row)
        except mcp_pool_mod.McpConfigBusy:
            logger.warning("mcp %r: configuración pendiente, no es un fallo de health", row["name"])
        except Exception as e:  # noqa: BLE001
            logger.warning("mcp %r: no se pudo armar (%r), salteando",
                           row["name"], e)
    return toolsets, attached, visible


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
                from .admin import _cbm_env  # lazy: evita ciclo de imports
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


# ---------- ahorro de tokens (2026-07-20) ----------
# Motivación medida: runs largos en workshopdemo llegaron a 28.7M
# tokens_in acumulados (95 tool calls). Dos causas: (a) tool results
# gordos (search_graph default limit=200, read_file de archivos
# enteros) y (b) cada turno re-manda TODO el historial, así que un
# result de 100KB se re-paga en CADA turno siguiente (blowup ~N²).
# Tres capas, todas acá:
#   1. _cap_tool_result: cap por resultado (el LLM ve un marcador y
#      refina/pagina en vez de tragar 200 nodos).
#   2. _elide_old_tool_returns: en runs largos, los results viejos se
#      reemplazan por un stub — corta el N². Corre en cada tool call
#      vía CappedToolset (ctx.messages es la lista viva del run).
#   2b. _elide_old_response_parts: lo mismo del lado del assistant —
#      thinking viejo y args de calls ya respondidas. Medido sobre un
#      run de kimi-k3 (206 requests): thinking 50% del gasto, args de
#      tool call 26%, results 19%. La capa 2 sola cubría ese 19%.
#   3. _slim_history: al recargar un hilo (ADR-025), los turnos
#      ANTERIORES al último user prompt quedan solo user/assistant
#      text — el tool spam viejo no viaja más.
# Tunables por env para no requerir redeploy si un cap queda corto.

TOOL_RESULT_CAP = int(os.environ.get("FOURBIS_TOOL_RESULT_CAP", "16000"))
# Tools cuyo resultado es salida de consola: builds, tests, git. Tienen su
# propio cap —más grande— y se les conserva el final (2026-08-16). El
# nombre `run_shell` es el del wrapper 4bis; los otros dos son alias que
# aparecen según el MCP que esté adjunto.
_CONSOLE_TOOLS = frozenset({"run_shell", "shell", "run_command"})
SHELL_RESULT_CAP = int(os.environ.get("FOURBIS_SHELL_RESULT_CAP", "48000"))
SHELL_RESULT_KEEP_TAIL = int(
    os.environ.get("FOURBIS_SHELL_RESULT_KEEP_TAIL", "12000"))
TOOL_KEEP_FULL = int(os.environ.get("FOURBIS_TOOL_KEEP_FULL", "8"))
# Thinking: ventana MUCHO más corta que la de los results. El
# razonamiento de hace 20 tool calls no le sirve al modelo — pero se
# re-manda igual (pydantic-ai lo mapea de vuelta a `reasoning_content`),
# y medido sobre un run real de kimi-k3 era el 50% del gasto: 136
# partes, 217KB, re-enviadas 206 veces.
THINK_KEEP_FULL = int(os.environ.get("FOURBIS_THINK_KEEP_FULL", "2"))
_ELIDE_MIN_CHARS = 600   # results más chicos que esto no vale la pena elidir
_ELIDE_KEEP_HEAD = 200   # chars que sobreviven del result elidido
_ELIDE_KEEP_TAIL = 80    # …más la última línea si entra acá: `(exit=1)`
_ELIDE_MARK = "[…result viejo elidido para ahorrar contexto; "\
    "re-ejecuta la tool si lo necesitas]"
# Args de una tool call YA respondida: el return dice si funcionó, así
# que el payload (write_file manda el archivo entero) es peso muerto.
# Se deja JSON válido: los providers OpenAI-compat parsean `arguments`.
_ELIDED_ARGS = '{"_elided": "args de una call vieja ya respondida"}'


# Rescate de errores del tramo elidido (2026-09-04). Al truncar la salida
# de consola se conservan cabeza y cola; el error de compilación vive en el
# MEDIO y se perdía. Lo sufre sobre todo `_STEP_OUT_MAX` (2400), que es lo
# que queda en `chats.progress_events`: el único rastro post-mortem de lo
# que dijo la terminal. Los topes son duros a propósito —`error` matchea
# ruido tipo `0 Error(s)`— y el bloque se DESCUENTA de la cabeza: el texto
# final mide lo mismo que antes, ningún cap sube.
_ERR_RE = re.compile(r"\b(error|fail(ed|ure)?|exception|FAILED)\b", re.I)
_ERR_KEEP_LINES = 20
_ERR_KEEP_CHARS = 1200
_ERR_HEAD_MIN = 200      # la cabeza nunca baja de acá; si no entra, no hay rescate


def _rescatar_errores(medio: str, budget: int) -> str:
    """Líneas con pinta de error del tramo elidido, deduplicadas.

    El dedupe no es cosmético: MSBuild repite el MISMO `error CSxxxx` una
    vez por target framework, y sin dedupe un build con 300 errores gasta
    el presupuesto entero en la misma línea repetida.
    """
    vistas: set[str] = set()
    lineas: list[str] = []
    usado = 0
    for cruda in medio.splitlines():
        linea = cruda.strip()
        if not linea or linea in vistas or not _ERR_RE.search(linea):
            continue
        vistas.add(linea)
        if len(lineas) >= _ERR_KEEP_LINES or usado + len(linea) + 1 > budget:
            break
        lineas.append(linea)
        usado += len(linea) + 1
    if not lineas:
        return ""
    return (f"\n…[{len(lineas)} líneas con error, rescatadas del medio]…\n"
            + "\n".join(lineas) + "\n")


def _cap_text(s: str, cap: int = TOOL_RESULT_CAP, *, keep_tail: int = 0) -> str:
    """Acota un texto a `cap` chars.

    `keep_tail` (2026-08-16): reserva ese pedazo para el FINAL del texto.
    Nació de `run_shell`: el cap se quedaba con la cabeza, y en la salida
    de un build o una suite de tests la cabeza es el banner de la
    herramienta — el veredicto (`exit=1`, el traceback, "3 failed") vive
    al final. El experto veía 16.000 chars de compilación y ni una línea
    del error, así que respondía "compiló" sobre un build roto o
    reintentaba a ciegas. Es la misma lógica que `_elide_tail` ya aplicaba
    del lado de la elisión.
    """
    if len(s) <= cap:
        return s
    if keep_tail > 0 and keep_tail < cap:
        # El bloque de errores sale del presupuesto de la CABEZA, así que
        # achicarla elide líneas nuevas que también hay que escanear: dos
        # pasadas alcanzan (`len(bloque)` está topeado, no diverge).
        budget = min(_ERR_KEEP_CHARS, cap - keep_tail - _ERR_HEAD_MIN)
        bloque = ""
        if budget > 0:
            for _ in range(2):
                bloque = _rescatar_errores(
                    s[cap - keep_tail - len(bloque):-keep_tail], budget)
        head = cap - keep_tail - len(bloque)
        return (
            s[:head]
            + bloque
            + f"\n…[TRUNCADO: {len(s) - head - keep_tail} chars del medio. "
            f"El resultado "
            f"completo tenía {len(s)} chars; abajo va el FINAL, que es "
            "donde suele estar el veredicto (exit code, error, resumen).]…\n"
            + s[-keep_tail:])
    return (
        s[:cap]
        + f"\n…[TRUNCADO: el resultado completo tenía {len(s)} chars. "
        "Refina la consulta (limit/offset, file_pattern, path más "
        "específico) para ver el resto.]")


def _caps_for(tool_name: str) -> tuple[int, int]:
    """`(cap, keep_tail)` para una tool. Default: el cap global, sin cola.

    Un cap único para todas las tools trataba igual a un `read_file` y a
    un `dotnet test`. Los de salida-de-consola necesitan más presupuesto
    Y que se les respete el final; el resto se paginan solos (el mensaje
    de truncado les dice cómo).
    """
    if tool_name in _CONSOLE_TOOLS:
        return SHELL_RESULT_CAP, SHELL_RESULT_KEEP_TAIL
    return TOOL_RESULT_CAP, 0


def _cap_tool_result(result: Any, *, tool_name: str = "") -> Any:
    """Capa 1: acota un tool result. Defensivo — tipo desconocido pasa igual."""
    cap, keep_tail = _caps_for(tool_name)
    if isinstance(result, str):
        return _cap_text(result, cap, keep_tail=keep_tail)
    if isinstance(result, list):
        # MCP content parts (TextContent y afines con .text). Presupuesto
        # compartido entre items; los que no entran se descartan con nota.
        budget = cap
        out = []
        for item in result:
            text = item if isinstance(item, str) else getattr(item, "text", None)
            if not isinstance(text, str):
                out.append(item)
                continue
            if budget <= 0:
                continue  # todavía pueden venir imágenes después del texto
            if len(text) > budget:
                capped = _cap_text(text, budget, keep_tail=min(
                    keep_tail, max(0, budget - 1)))
                if isinstance(item, str):
                    item = capped
                else:
                    item = item.model_copy(update={"text": capped})
                out.append(item)
                budget = 0
            else:
                out.append(item)
                budget -= len(text)
        return out
    return result


def _elide_old_tool_returns(messages: list) -> None:
    """Capa 2: stub-ea in-place los tool results viejos del run.

    Deja intactos los últimos TOOL_KEEP_FULL (working set del LLM);
    los anteriores quedan con head + marcador. Idempotente: un part ya
    elidido queda corto y el len-check lo saltea. Muta los objetos del
    historial vivo — pydantic-ai re-arma cada request desde estos
    mismos objetos, y el messages_json persistido hereda la elisión
    (win extra: los hilos guardados también adelgazan).
    """
    returns = [
        p
        for m in messages if isinstance(m, ModelRequest)
        for p in m.parts if isinstance(p, ToolReturnPart)
    ]
    for part in returns[:-TOOL_KEEP_FULL or None]:
        content = part.content
        if isinstance(content, str) and len(content) > _ELIDE_MIN_CHARS:
            part.content = (content[:_ELIDE_KEEP_HEAD] + "\n" + _ELIDE_MARK
                            + _elide_tail(content))


def _elide_tail(content: str) -> str:
    """Última línea (si es corta) de un result elidido: el veredicto.

    2026-07-31: el head son los primeros 200 chars — en un `run_shell`
    eso es el comando y el arranque del build, nunca el `(exit=N)` que
    va al final. Releyendo el hilo, el modelo no sabía si ese build
    había pasado. Medidos 579 results del historial en ese estado.
    """
    last = content.rstrip().rsplit("\n", 1)[-1].strip()
    return f"\n{last}" if 0 < len(last) <= _ELIDE_KEEP_TAIL else ""


def _elide_old_response_parts(messages: list) -> None:
    """Capa 2b: adelgaza los ModelResponse viejos del run (2026-07-28).

    Dos cosas que la capa 2 no tocaba y que medidas sobre runs reales
    pesaban 3 de cada 4 tokens re-enviados:

    - **thinking**: se descarta pasadas las últimas THINK_KEEP_FULL
      respuestas. Nunca se vacía un response por completo: si el
      thinking era su única parte, se deja como está (un assistant sin
      contenido lo rechazan varios providers).
    - **args de tool calls ya respondidas**: pasadas las mismas
      TOOL_KEEP_FULL que usan los results, el payload se reemplaza por
      un stub JSON. Una call sin responder NUNCA se toca: el par
      call↔return tiene que seguir cerrado.

    Idempotente y in-place, igual que `_elide_old_tool_returns`.
    """
    responses = [m for m in messages if isinstance(m, ModelResponse)]
    for m in responses[:-THINK_KEEP_FULL or None]:
        parts = [p for p in m.parts if not isinstance(p, ThinkingPart)]
        if parts and len(parts) != len(m.parts):
            m.parts[:] = parts

    answered = {
        p.tool_call_id
        for m in messages if isinstance(m, ModelRequest)
        for p in m.parts if isinstance(p, ToolReturnPart)
    }
    for m in responses[:-TOOL_KEEP_FULL or None]:
        for p in m.parts:
            if (isinstance(p, ToolCallPart)
                    and p.tool_call_id in answered
                    and len(str(p.args)) > _ELIDE_MIN_CHARS):
                p.args = _ELIDED_ARGS


def _close_orphan_tool_calls(messages: list, *, reason: str) -> int:
    """Contesta con un ToolReturnPart sintético cada tool call sin respuesta.

    Bug fix 2026-07-25: si cortamos el run mientras una tool estaba
    corriendo (watchdog de idle, tope global, cancel del usuario), el
    historial rescatado termina en un tool call huérfano. pydantic-ai
    rechaza el turno siguiente con
    `UserError: Cannot provide a new user prompt when the message
    history contains unprocessed tool calls` — o sea, el **continúa**
    que le ofrecemos al humano en el mensaje de corte explotaba SIEMPRE
    (caso anonimizado: un chat de ejemplo sobre un hilo de ejemplo).

    Devuelve cuántos huérfanos cerró. Muta `messages` in-place.
    """
    answered = {
        p.tool_call_id
        for m in messages if isinstance(m, ModelRequest)
        for p in m.parts if isinstance(p, ToolReturnPart)
    }
    # RetryPromptPart también cuenta como respuesta, pero no está
    # importado y llega por el mismo camino: filtramos por atributo.
    for m in messages:
        for p in getattr(m, "parts", []) or []:
            if (type(p).__name__ == "RetryPromptPart"
                    and getattr(p, "tool_call_id", None)):
                answered.add(p.tool_call_id)

    orphans = [
        p
        for m in messages if isinstance(m, ModelResponse)
        for p in getattr(m, "parts", []) or []
        if getattr(p, "tool_call_id", None)
        and getattr(p, "tool_name", None)
        and type(p).__name__ == "ToolCallPart"
        and p.tool_call_id not in answered
    ]
    if not orphans:
        return 0
    messages.append(ModelRequest(parts=[
        ToolReturnPart(
            tool_name=p.tool_name,
            tool_call_id=p.tool_call_id,
            content=f"(sin resultado: {reason})",
        )
        for p in orphans
    ]))
    logger.info(
        "cerré %d tool call(s) huérfano(s) para dejar el hilo reanudable: %s",
        len(orphans), ", ".join(p.tool_name for p in orphans))
    return len(orphans)


# Umbral de "el modelo está pensando" — por debajo de esto no se emite
# latido (un run normal emite nodes seguido y no hace falta ruido).
_BEAT_AFTER_S = 45.0
# Margen que se le da a una tool por encima de SU propio techo antes de
# que el watchdog la dé por trabada. El corte normal lo hace
# `CappedToolset.timeout`; esto cubre el caso en que ese corte falle (un
# subprocess que no muere, un transport trabado).
_TOOL_OVERRUN_GRACE_S = 30.0


def _think_cap(defaults: dict, idle_to: float) -> float:
    """Cap de idle mientras el modelo genera.

    La regla no es "siempre el numero grande": si el proyecto APRETO el
    idle a mano y no dijo nada del cap de pensar, manda el suyo. Un
    humano que puso `idle_timeout_s: 5` no quiere que el modelo tenga
    600s por otra puerta. El cap grande existe para el DEFAULT, no para
    pisar una decision explicita.
    """
    if defaults.get("think_timeout_s"):
        return float(defaults["think_timeout_s"])
    if "idle_timeout_s" in defaults:
        return float(idle_to)
    return float(config.expert_think_timeout_s())


def _watchdog_verdict(
    *, idle_s: float, idle_timeout: float,
    tool_s: float | None, tool_timeout: float | None,
    pensando: bool = False, think_timeout: float | None = None,
) -> str:
    """Decisión del watchdog: `"tool_wait"` | `"kill"` | `"beat"` | `"ok"`.

    Función pura para poder probarla: la lógica vivía dentro del closure
    `_idle_watchdog` y no había forma de ejercitarla sin levantar un run.

    - `tool_wait`: hay una tool corriendo dentro de su presupuesto. El
      experto NO está idle — está esperando un comando. Antes esto
      contaba como idle y el watchdog mataba el run entero a los 180s,
      así que el techo real de cualquier build era el watchdog.
    - `kill`: pasó el cap de idle sin actividad. Si además había una tool
      en vuelo, es que su propio corte falló y esta es la red de
      seguridad.
    - `beat`: sin actividad pero dentro del cap, y ya lleva lo suficiente
      como para que valga la pena avisar que sigue vivo.
    """
    if tool_s is not None and tool_s <= (tool_timeout or 0.0) + _TOOL_OVERRUN_GRACE_S:
        return "tool_wait"
    # El modelo generando NO es idle, aunque se vea igual desde acá: el
    # loop late por node y mientras genera no llega ninguno. Ver
    # `config.expert_think_timeout_s`.
    if pensando and idle_s <= (think_timeout or idle_timeout):
        return "beat" if idle_s > _BEAT_AFTER_S else "ok"
    if idle_s > idle_timeout:
        return "kill"
    if idle_s > _BEAT_AFTER_S:
        return "beat"
    return "ok"


def _anotar_en_vuelo(inflight: dict, nombre: str) -> str:
    """Registra una tool en vuelo. Devuelve la clave para darla de baja.

    Una clave POR LLAMADA, no por nombre: pydantic-ai corre los
    tool-calls de un mismo turno en PARALELO y son la misma tool con el
    mismo nombre. Ver `tool_en_vuelo`.
    """
    marca = uuid.uuid4().hex
    inflight[marca] = (nombre, time.monotonic())
    return marca


def tool_en_vuelo(inflight: dict) -> tuple[Optional[str], Optional[float]]:
    """`(nombre, since)` de la tool en vuelo que arrancó PRIMERO.

    `(None, None)` si no hay ninguna. Manda la más vieja porque es la que
    dice si esto avanza o está trabado: que una hermana rápida termine no
    dice nada sobre la que sigue corriendo.

    Antes `inflight` era UN solo par `{tool, since}` y eso se rompía con
    llamadas en paralelo. Medido el 2026-08-31 en code-hero-rpg: el
    modelo pidió dos `shell` en el mismo turno —`npm run dev` con el
    techo default (300s) y `npx vitest run` con `timeout_s=120`—; al
    cortar el vitest a los 120s, su `finally` borró la marca de LOS DOS.
    El watchdog dejó de ver una tool en vuelo, contó el `npm run dev`
    como "el experto no hace nada" y mató el run entero a los 180s. Dos
    runs seguidos murieron así, los dos con el mismo diagnóstico
    ("la tool `shell` no devolvió en >180s") y el plan quedó clavado.
    """
    if not inflight:
        return None, None
    nombre, since = min(inflight.values(), key=lambda v: v[1])
    return nombre, since


@dataclasses.dataclass
class CappedToolset(WrapperToolset):
    """Envuelve cualquier toolset con las capas 1 y 2 + un corte por
    tool-call.

    `timeout` (segundos): corta ESTE tool-call si no devuelve a tiempo.
    Existe porque pydantic-ai aplica su `tool_timeout` SOLO al
    FunctionToolset interno (tools nativas); a un MCPToolset le queda el
    `read_timeout` default de pydantic-ai (300s), POR ENCIMA del idle
    watchdog (180s). Resultado: un `run_shell` colgado (server en
    foreground, prompt interactivo, hijo zombie) nunca fallaba solo —
    el watchdog mataba el run ENTERO a los 180s. Con `timeout` seteado el
    tool-call se corta a tiempo y el modelo recibe un ModelRetry
    accionable en vez de perder el run. None = sin corte extra (las
    nativas ya traen el suyo por el FunctionToolset).

    Y traduce `McpError` a `ModelRetry`: un MCP que contesta error de
    protocolo no puede voltear el run (ver el comentario en `call_tool`).
    """

    timeout: float | None = None
    # Estado compartido con el watchdog de idle (2026-08-16): mientras
    # una tool está en vuelo, el experto NO está idle aunque no emita
    # nodes. Es un dict por lo mismo que en OptionalToolset: `for_run` /
    # `for_run_step` hacen `dataclasses.replace()` y la copia tiene que
    # ver el MISMO estado, no una instantánea.
    inflight: dict = dataclasses.field(default_factory=dict)
    image_artifacts: Optional[dict] = None
    vision: bool = True

    async def call_tool(self, name, tool_args, ctx, tool):  # noqa: ANN001
        _marca = _anotar_en_vuelo(self.inflight, name)
        try:
            if self.timeout is not None:
                try:
                    result = await asyncio.wait_for(
                        super().call_tool(name, tool_args, ctx, tool),
                        timeout=self.timeout)
                except asyncio.TimeoutError:
                    raise ModelRetry(
                        f"La tool `{name}` no devolvió en {self.timeout:.0f}s "
                        "y se cortó. Suele ser un comando que no termina solo "
                        "(un server en foreground, un prompt interactivo). "
                        "Evitá comandos que no retornan: corré servers en "
                        "background o agregales un timeout.") from None
            else:
                result = await super().call_tool(name, tool_args, ctx, tool)
        except McpError as e:
            # Un MCP que contesta con error JSON-RPC MATABA el run entero
            # (2026-08-26). pydantic-ai convierte a ModelRetry el
            # `ToolError` de fastmcp, pero un `McpError` pelado —el server
            # respondiendo `error: {code, message}`— cae en el default de
            # `on_tool_execute_error`, que es `raise error`. O sea: el
            # experto perdía el run por un argumento mal formado.
            #
            # El caso que lo destapó: mcp-mermaid devolviendo -32603
            # "Failed to generate mermaid: Parse error on line 83" porque
            # el modelo puso un `#` en un label (en Mermaid `#` abre una
            # entidad y hay que escribirlo `#35;`). Es un typo, y un typo
            # no puede costar un run de dos millones de tokens.
            #
            # El texto del server va COMPLETO y sin traducir: ahí está el
            # número de línea y el token que falló, que es exactamente lo
            # que el modelo necesita para corregir.
            raise ModelRetry(
                f"La tool `{name}` falló del lado del MCP: {e} "
                "El error viene del server, no del relay — la tool sigue "
                "disponible. Corrige los argumentos según ese mensaje y "
                "vuelve a llamarla, o resuelve el paso de otra forma.") from e
        finally:
            self.inflight.pop(_marca, None)
        try:
            _elide_old_tool_returns(ctx.messages)
            _elide_old_response_parts(ctx.messages)
        except Exception as e:  # noqa: BLE001 — la elisión nunca rompe un run
            logger.warning("elisión de historial falló: %r", e)
        result = _cap_tool_result(result, tool_name=name)
        parts = result if isinstance(result, list) else [result]
        images = [p for p in parts if isinstance(p, BinaryContent)
                  and p.media_type.startswith("image/")]
        if not images:
            return result
        notes = []
        for part in images:
            if self.image_artifacts is not None:
                try:
                    if len(part.data) > attachments_mod.max_attachment_bytes():
                        raise ValueError("imagen excede ATTACHMENT_MAX_BYTES")
                    aid, path, _ = await asyncio.to_thread(
                        attachments_mod.store, part.data, mimetype=part.media_type)
                    self.image_artifacts[aid] = path.name
                    notes.append(f"Imagen generada: {path.name} (/attachments/{aid}).")
                except (OSError, ValueError) as exc:
                    notes.append(f"No se pudo guardar la imagen para el usuario: {exc}")
        if not self.vision:
            parts = [p for p in parts if p not in images]
            notes.append("El modelo configurado no admite imágenes: la captura "
                         "NO se envió al modelo. No afirmes haberla inspeccionado.")
        return [*parts, *notes]


@contextlib.asynccontextmanager
async def _tool_en_vuelo(inflight: dict, nombre: str):
    """Marca una tool como en vuelo para el watchdog de idle.

    Misma convención que `CappedToolset.call_tool` —y existe porque esa
    era la ÚNICA que la escribía—. El watchdog decide con `inflight`: sin
    entrada ahí, un comando que tarda cuenta como "el experto no hace
    nada" y el run entero muere a los `idle_timeout` segundos.

    Eso es exactamente lo que le pasó al nodo `Corregir bugs de
    Infrastructure y Web` en inventorydemo el 30/8: el arreglo del 16/8 sacó al
    watchdog de la ventana de una tool, pero `native_shell` (default) le
    quita el `run_shell` al wrapper MCP y lo reemplaza por una tool del
    relay que no pasa por `CappedToolset` — o sea que el shell, la única
    tool con techo propio POR ENCIMA del watchdog (300s contra 180s),
    era también la única que no se anunciaba. Un `dotnet build` de tres
    minutos mataba el run.
    """
    marca = _anotar_en_vuelo(inflight, nombre)
    try:
        yield
    finally:
        inflight.pop(marca, None)


@dataclasses.dataclass
class HideToolsToolset(WrapperToolset):
    """Oculta tools de un toolset por nombre (2026-08-16).

    Existe para que haya UN solo shell. El `4bis-wrapper` (repo
    `4bis.vscode`, retirado del catálogo por default desde 2026-08-16 —
    docs/WRAPPER.md) exponía `run_shell`, que se cuelga con PowerShell y
    con cualquier comando que espere stdin (ver `relay/shell.py`); el
    relay expone `shell`, que no. Si alguien re-adjunta el wrapper
    (opt-out `native_shell=false`), con los dos visibles el modelo elige
    cualquiera —y la mitad de las veces elige el roto—, así que el bueno
    desplaza al otro en vez de convivir con él.

    Ocultar y no renombrar: el par tool-call ↔ tool-return se cierra por
    nombre, y renombrar del lado del relay dejaría al MCP contestando un
    nombre que el modelo nunca llamó.
    """

    hidden: frozenset = frozenset()

    async def get_tools(self, ctx):  # noqa: ANN001
        tools = await self.wrapped.get_tools(ctx)
        if not self.hidden:
            return tools
        return {k: v for k, v in tools.items() if k not in self.hidden}


@dataclasses.dataclass
class OptionalToolset(WrapperToolset):
    """Un MCP que no levanta NO puede matar el run (2026-07-26).

    `agent.iter` entra a TODOS los toolsets en un solo exit stack: si el
    `__aenter__` de uno tira, la excepción sube por CombinedToolset y
    revienta el run entero — el experto muere sin escribir una línea y
    el usuario ve un traceback de fastmcp. Pasó con los always-on que
    spawnean `npx -y` / `uvx`: el `initialize` no completa en
    FOURBIS_MCP_INIT_TIMEOUT y fastmcp levanta "Failed to initialize
    server session".

    El pool ya degradaba limpio, pero solo para los `on_demand`. Este
    wrapper es el guard genérico: el toolset que no abre queda en cero
    tools y el run sigue con los demás.

    `state` es un dict porque `for_run`/`for_run_step` de WrapperToolset
    hacen `dataclasses.replace()`: la copia comparte el mismo dict y ve
    el flag, un bool se quedaría en la instancia vieja.
    """

    state: dict = dataclasses.field(default_factory=dict)

    async def __aenter__(self):
        try:
            await self.wrapped.__aenter__()
        except Exception as e:  # noqa: BLE001 — cualquier fallo degrada
            self.state["dead"] = True
            logger.warning("mcp %s: no levantó (%r), el run sigue sin ese "
                           "toolset", self.wrapped.label, e)
        return self

    async def __aexit__(self, *args):
        if self.state.get("dead"):
            return None
        return await self.wrapped.__aexit__(*args)

    async def get_tools(self, ctx):  # noqa: ANN001
        if self.state.get("dead"):
            return {}
        return await self.wrapped.get_tools(ctx)

    async def get_instructions(self, ctx):  # noqa: ANN001
        if self.state.get("dead"):
            return None
        return await self.wrapped.get_instructions(ctx)


def _slim_history(messages: list, *, corte: Optional[int] = None) -> list:
    """Capa 3: adelgaza el historial ANTERIOR al último user prompt.

    Los turnos viejos quedan user/assistant text puro (sin ToolCallPart/
    ToolReturnPart/thinking); el último turno viaja intacto para que
    'continúa' retome con el working set completo. El pairing tool
    call↔return nunca cruza un user prompt, así que cortar ahí es
    seguro para providers OpenAI-compat.

    Bug fix 2026-08-13: de cada turno viejo sobrevive SOLO el último
    response. Los anteriores son la narración de mitad de run ("veo el
    appsettings:", "compilo:") cuya tool call esta misma función acaba de
    borrar — y un historial lleno de "el asistente anuncia una acción y no
    pasa nada" le enseña al modelo a hacer exactamente eso. Medido en el
    un hilo de ejemplo: 115 de 166 textos terminaban en `:` y el experto
    encadenó 14 runs sin ejecutar una sola tool. Ver
    docs/DIAG_HILO_EXAMPLE.md.

    `corte` es el índice desde el cual el historial viaja intacto; por
    default, el último user prompt. Pasar `len(messages)` adelgaza
    TAMBIÉN el último turno — es lo que hace `run_expert` cuando el
    verificador cortó por `off_plan` (ver ahí el porqué).
    """
    last_user = corte if corte is not None else -1
    if corte is None:
        for i, m in enumerate(messages):
            if isinstance(m, ModelRequest) and any(
                    isinstance(p, UserPromptPart) for p in m.parts):
                last_user = i
    if last_user <= 0:
        return messages
    # Último response de cada turno viejo = la respuesta al humano; los
    # anteriores son narración de tool. Se marca de atrás para adelante:
    # los tool results van en ModelRequest sin UserPromptPart, así que
    # "el siguiente mensaje" no alcanza para distinguirlos.
    keep_resp: set[int] = set()
    seen_resp = False
    for i in range(last_user - 1, -1, -1):
        m = messages[i]
        if isinstance(m, ModelResponse):
            if not seen_resp:
                keep_resp.add(i)
                seen_resp = True
        elif isinstance(m, ModelRequest) and any(
                isinstance(p, UserPromptPart) for p in m.parts):
            seen_resp = False
    slim: list = []
    for i, m in enumerate(messages[:last_user]):
        if isinstance(m, ModelRequest):
            parts = [p for p in m.parts if isinstance(p, UserPromptPart)]
        elif isinstance(m, ModelResponse):
            if i not in keep_resp:
                continue
            parts = [p for p in m.parts if isinstance(p, TextPart)]
        else:
            slim.append(m)
            continue
        if parts:
            slim.append(dataclasses.replace(m, parts=parts))
    return slim + messages[last_user:]


_INSTALL_HINTS = ("instal", "npm i ", "pip install", "winget", "choco",
                  "apt-get", "apt install", "dotnet tool", "descargar",
                  "bajar el binario", "falta el paquete", "no está instalado")


def _huele_a_instalacion(*textos: str) -> bool:
    """¿La pregunta es sobre instalar algo? Marca `kind='install'`.

    Solo para que la UI la pinte distinta y para poder contar cuántas
    veces el experto se frenó por una herramienta que falta — que es el
    caso que el humano pidió que SIEMPRE se pregunte.
    """
    blob = " ".join(t or "" for t in textos).lower()
    return any(h in blob for h in _INSTALL_HINTS)


# 2026-09-06. Se acumularon 11 preguntas de `ask_human` sin responder (la
# más vieja de 3 semanas): el humano no las contestaba porque verificar la
# afirmación costaba casi lo mismo que hacer el trabajo — la pregunta decía
# una conclusión ("el inventario está desactualizado") sin decir qué leyó
# para llegar ahí. `_evidencia_insuficiente` es el guard que obliga a
# `ask_human` a traer esa lectura.
#
# ponytail: heurística de texto, no un parser — detecta "hay una ruta o un
# nombre de archivo con extensión" y "el texto es puro hedge sin esa
# referencia". No entiende si la evidencia es CORRECTA, solo que no está
# vacía ni es pura especulación. Subir a algo más estricto (ej. verificar
# que el archivo citado existe de verdad en el repo) el día que el modelo
# aprenda a colar un `Foo.cs` inventado.
#: Piso de largo para la evidencia. 2026-09-06: estaba en 20 y dejaba
#: pasar "Lei Foo.cs y esta mal" —un nombre de archivo pegado a nada—,
#: que cumple la forma y no sirve: el humano igual tiene que abrir el
#: repo, que es justo el costo que esto viene a sacarle. 80 es una
#: oracion corta; la evidencia real medida en los tests da 100-132.
#: No es infalible (nada que valide lenguaje natural lo es): sube el
#: costo de inventar por encima del de mirar de verdad.
_MIN_EVIDENCIA_CHARS = 80
_FILE_REF_RE = re.compile(r'[\w][\w./\\-]*\.[A-Za-z]{2,6}\b')
_HEDGE_HINTS = ("asumo", "aparentemente", "supongo", "no leí",
                "parece que", "creo que")


def _evidencia_insuficiente(evidencia: str) -> str | None:
    """`None` si la evidencia alcanza el mínimo; si no, el motivo (para el
    `ModelRetry` que le pide al modelo completarla).

    Dos rechazos, en orden:
    1. Vacía o demasiado corta.
    2. Sin ninguna referencia a un archivo concreto (ruta con `/` o `\\`,
       o un nombre con extensión tipo `Foo.cs`).

    Un hedge ("asumo", "parece que") NO invalida por sí solo — reportar
    "el inventario dice textual 'asumo, no leí el resto'" citando el
    archivo real donde lo dice es evidencia legítima. Lo que se rechaza es
    la combinación: puro hedge y CERO archivo citado, que es la firma de
    una conclusión inventada.
    """
    ev = (evidencia or "").strip()
    if len(ev) < _MIN_EVIDENCIA_CHARS:
        return ("está vacía o es demasiado corta. Necesito qué archivos "
                "leíste concretamente y qué encontraste en ellos, no una "
                "frase de una línea")
    if not _FILE_REF_RE.search(ev):
        blob = ev.lower()
        if any(h in blob for h in _HEDGE_HINTS):
            return ("es pura especulación: solo tiene frases como 'asumo' "
                     "o 'parece que' sin nombrar ningún archivo concreto "
                     "que hayas leído")
        return ("no menciona ningún archivo concreto (una ruta o un "
                 "nombre con extensión, ej. `Service/Foo.cs`). Decime QUÉ "
                 "archivos leíste")
    return None


def _model_key(spec_or_name: str) -> str:
    """`minimax:MiniMax-M3` / `MiniMax-M3` → clave comparable.

    Los `ModelResponse` guardan el `model_name` que reportó el proveedor,
    que casi nunca trae el prefijo `provider:` con el que lo pedimos. Se
    compara por el tramo del modelo, en minúsculas.
    """
    s = (spec_or_name or "").strip().lower()
    if ":" in s:
        s = s.partition(":")[2] or s
    return s


def _distinto_modelo(prev: str, cur: str) -> bool:
    """¿Dos claves de modelo son de modelos REALMENTE distintos?

    Un prefijo compartido cuenta como el mismo modelo. Los proveedores no
    siempre devuelven el id que pediste: OpenAI responde
    `gpt-4o-2024-08-06` cuando pediste `gpt-4o`, y varios OpenAI-compat
    agregan sufijo de versión. Comparar por igualdad estricta daba un
    FALSO POSITIVO en cada turno de un hilo que nunca cambió de modelo, y
    el precio de ese falso positivo es borrar el razonamiento del último
    turno — o sea, pérdida de contexto silenciosa, justo lo que este
    módulo está tratando de evitar.

    Ante la duda NO se toca el historial: solo se limpia cuando los dos
    nombres son inequívocamente de modelos distintos.
    """
    if not prev or not cur:
        return False
    return not (prev.startswith(cur) or cur.startswith(prev))


def _strip_foreign_thinking(messages: list, current_spec: str) -> int:
    """Saca los `ThinkingPart` del historial si lo escribió OTRO modelo.

    Por qué (2026-08-15): `_slim_history` deja el ÚLTIMO turno intacto —
    thinking incluido— para que "continúa" retome con el working set
    completo. Si entre dos turnos del mismo hilo cambia el modelo del
    ejecutor (override por run desde la UI, o un cambio en
    `defaults_json.model`), ese thinking se le replaya a un proveedor
    distinto del que lo generó. En OpenAI-compat viaja como
    `reasoning_content`: en el mejor caso es ruido que el modelo nuevo no
    puede interpretar, en el peor el endpoint lo rechaza.

    El razonamiento propio de un modelo no es contexto compartible; el
    texto y las tool calls sí. Devuelve cuántos parts sacó (0 = no hubo
    cambio de modelo, o no había thinking). Muta `messages` in-place.
    """
    cur = _model_key(current_spec)
    if not cur:
        return 0
    # El modelo del historial es el del último response que lo declare.
    prev = ""
    for m in reversed(messages):
        if isinstance(m, ModelResponse) and getattr(m, "model_name", None):
            prev = _model_key(m.model_name)
            break
    if not prev or not _distinto_modelo(prev, cur):
        return 0
    removed = 0
    for m in messages:
        if not isinstance(m, ModelResponse):
            continue
        parts = [p for p in m.parts if not isinstance(p, ThinkingPart)]
        # Nunca se vacía un response: un assistant sin contenido lo
        # rechazan varios providers (misma regla que `_elide_old_response_parts`).
        if parts and len(parts) != len(m.parts):
            removed += len(m.parts) - len(parts)
            m.parts[:] = parts
    if removed:
        logger.info(
            "historial escrito por %r y ahora corre %r: saqué %d thinking "
            "parts ajenos", prev, cur, removed)
    return removed


# ---------- medidor de contexto del hilo (2026-07-22) ----------
#
# El síntoma que lo motivó: hilos largos que terminan en timeout sin estar
# colgados — el historial que se replaya ya ocupa la mitad de la ventana y
# lo que queda no alcanza para los tool results del run.
#
# No estimamos por chars (con la elisión de la capa 2 el blob guardado no
# se parece a lo que viaja): leemos el `usage` REAL que el provider
# devolvió y que pydantic-ai serializa en cada ModelResponse. Los
# responses se agrupan por `run_id`, así que el último grupo = el último
# run del hilo, y ahí:
#   base = input_tokens del PRIMER request  → lo que pesa el hilo antes
#          de que el experto haga nada (historial + system + prompt)
#   peak = el mayor input_tokens del run    → base + tool results
# El aviso lo dispara `peak` (lo cerca que estuvo de la pared); `base`
# dice cuánto bajaría compactando — el margen real del próximo run es la
# diferencia entre la ventana y `base`.

def context_usage(messages_json: str, *, limit: Optional[int] = None,
                  warn_tokens: Optional[int] = None) -> Optional[dict]:
    """Ocupación de la ventana de contexto según el último run del hilo.

    None si no hay usage que leer (hilo vacío, historial recién
    compactado o provider que no reporta tokens).

    2026-08-31: `limit` y `warn_tokens` entran por parámetro (los saca
    de la tabla `models` el wrapper async `context_usage_db`). Sin
    ellos cae al default global, que es un número fijo para TODOS los
    modelos — el que hacía que un request real de 136.111 tokens
    apareciera como "113% de la ventana". `model_name` va en la salida
    para que la UI pueda decir contra qué ventana está midiendo.
    """
    try:
        messages = json.loads(messages_json or "")
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(messages, list):
        return None
    last_run: list[int] = []
    last_id = object()
    model_name = ""
    for m in messages:
        if not isinstance(m, dict) or m.get("kind") != "response":
            continue
        tokens = (m.get("usage") or {}).get("input_tokens")
        if not isinstance(tokens, int) or tokens <= 0:
            continue
        run_id = m.get("run_id")
        # run_id None (historiales viejos) ⇒ todo cuenta como un run.
        if run_id is not None and run_id != last_id:
            last_run = []
        last_id = run_id
        last_run.append(tokens)
        model_name = m.get("model_name") or model_name
    if not last_run:
        return None
    medida = limit is not None and limit > 0
    limit = limit if medida else config.model_context_tokens()
    warn = config.context_warn_pct()
    base, peak = last_run[0], max(last_run)
    peak_pct = round(100 * peak / limit)
    # El umbral por modelo es en TOKENS y no en %: el punto donde el
    # modelo se degrada es un dato medido del modelo (92k en MiniMax
    # M3), no una fracción de su ventana nominal. Sin dato por modelo,
    # el % global de siempre.
    umbral = (warn_tokens if warn_tokens and warn_tokens > 0
              else round(limit * warn / 100))
    return {
        "base_tokens": base,
        "peak_tokens": peak,
        "limit": limit,
        "pct": round(100 * base / limit),
        "peak_pct": peak_pct,
        "warn_pct": round(100 * umbral / limit),
        "warn_tokens": umbral,
        "model_name": model_name,
        # `False` cuando el límite salió del default global y no de la
        # ficha del modelo: la UI lo dice en vez de vender un porcentaje
        # contra una ventana inventada.
        "limit_medido": bool(medida),
        # El disparador es el PICO, no la base: es lo cerca que el run
        # llegó de la pared. La base dice cuánto bajarías compactando.
        "hot": peak >= umbral,
    }


async def context_usage_db(db: Any, messages_json: str) -> Optional[dict]:
    """`context_usage` con la ventana REAL del modelo que corrió.

    Wrapper async porque la ventana vive en la tabla `models` y los
    cuatro callers ya tienen el `db` a mano. Si el modelo no está en el
    catálogo (o la fila no tiene ventana cargada), cae al default
    global y `limit_medido` queda en False.
    """
    ctx = context_usage(messages_json)
    if ctx is None or not ctx.get("model_name"):
        return ctx
    try:
        ventana, umbral = await db.model_context(ctx["model_name"])
    except Exception as e:  # noqa: BLE001 — el medidor nunca voltea un run
        logger.warning("no pude leer la ventana de %r: %r",
                       ctx.get("model_name"), e)
        return ctx
    if not ventana:
        return ctx
    # Se recalcula sobre el dict y NO se llama de nuevo a context_usage:
    # el blob del historial llega al MB y este endpoint lo pollea la UI
    # cada 2,5s. Todo lo que cambia es aritmética sobre base/peak.
    base, peak = ctx["base_tokens"], ctx["peak_tokens"]
    if not umbral or umbral <= 0:
        umbral = round(ventana * config.context_warn_pct() / 100)
    ctx.update(
        limit=ventana, limit_medido=True, warn_tokens=umbral,
        pct=round(100 * base / ventana),
        peak_pct=round(100 * peak / ventana),
        warn_pct=round(100 * umbral / ventana),
        hot=peak >= umbral)
    return ctx


def _strip_instructions(messages: list) -> list:
    """Copia del historial sin las `instructions` de cada ModelRequest.

    pydantic-ai guarda el system prompt COMPLETO en cada ModelRequest,
    pero manda UNA sola copia al provider (el agente re-inyecta las
    suyas en cada request; las históricas solo son fallback cuando el
    agente no tiene). Medido sobre un hilo de ejemplo: 52 requests con
    ~33 KB de instructions c/u = 94.8% del blob de 1.81 MB, contra un
    payload real de 96 KB con un único mensaje `role=system`.

    Separada de sus dos usos a propósito, porque sirve para ambos:
      - estimar contexto sin contar 52 veces lo que viaja una sola;
      - persistir el hilo sin inflarlo ~20x.
    """
    return [
        dataclasses.replace(m, instructions=None)
        if isinstance(m, ModelRequest) and getattr(m, "instructions", None)
        else m
        for m in messages
    ]


def _prompt_con_imagenes(user: str, images: Optional[list] = None):
    """`user` solo, o [texto, BinaryContent…] si hay imágenes adjuntas.

    Devolver el str pelado cuando no hay imágenes no es cosmético: es
    el camino que ya estaba probado, y así los runs sin adjuntos no
    cambian ni un byte de lo que viaja al provider.
    """
    if not images:
        return user
    return [user, *(BinaryContent(data=d, media_type=m) for d, m in images)]


def _strip_images(messages: list) -> list:
    """Reemplaza las imágenes del historial por una nota de texto.

    Se aplica al PERSISTIR, no al correr: dentro del run el modelo sigue
    viendo la imagen en cada vuelta (es parte del prompt del usuario),
    pero el historial guardado no se la lleva a los turnos siguientes.
    Sin esto, un screenshot de 1MB se re-manda en cada turno futuro de
    la conversación para siempre — el mismo problema N² que ya atacan
    las capas de elisión de tool results y thinking.

    ponytail: la imagen NO se puede recuperar del historial. Si algún
    día hace falta "seguí mirando la captura", el upgrade es guardar el
    attach_id en la nota y re-cargarla por id.
    """
    out = []
    for m in messages:
        parts = getattr(m, "parts", None)
        if not parts:
            out.append(m)
            continue
        nuevas, tocado = [], False
        for p in parts:
            c = getattr(p, "content", None)
            if isinstance(c, list) and any(isinstance(x, BinaryContent) for x in c):
                textos = [x for x in c if isinstance(x, str)]
                n = sum(1 for x in c if isinstance(x, BinaryContent))
                nuevas.append(dataclasses.replace(p, content="\n".join(
                    textos + [f"[{n} imagen(es) adjunta(s) en su momento; "
                              "no se re-envían en los turnos siguientes]"])))
                tocado = True
            else:
                nuevas.append(p)
        out.append(dataclasses.replace(m, parts=nuevas) if tocado else m)
    return out


def _dump_messages(messages: list) -> str:
    """Serializa el historial para persistir. Único choke point: si algo
    más hay que sacar antes de guardar, va acá y no en cada call site."""
    return ModelMessagesTypeAdapter.dump_json(
        _strip_images(_strip_instructions(messages))).decode("utf-8")


# Fases de corte que ya tienen su propio mensaje accionable en
# `output_text` — el helper no debe pisar ese mensaje.
_CUT_PHASES = frozenset({
    "idle_timeout", "hard_timeout", "budget_exceeded",
    "tool_retries_exhausted", "cancelled", "steered",
    # 2026-08-17: el corte por veredicto off_plan del verificador de
    # media corrida ya escribe su propio mensaje con el feedback.
    "off_plan",
    # 2026-08-26: el corte por caída del proveedor ya escribe su propio
    # mensaje con el status y el porqué no se pudo retomar.
    "provider_error",
    # 2026-09-02: el tope de subdivisión (Fase 1) ya escribe su propio
    # mensaje con el consejo de partir en lotes.
    "budget_split",
})

#: Reintentos del EJECUTOR ante una caída del proveedor a mitad del run
#: (2026-08-26). Uno solo, y no una cascada como la del planificador: el
#: ejecutor lleva el historial y las tools del proyecto encima, así que
#: bajarlo a otro modelo a mitad de tarea cambia quién la termina. Si el
#: segundo intento también se cae, cortamos con el trabajo guardado y el
#: humano retoma con **continúa** cuando el endpoint vuelva.
_PROVIDER_RETRIES = 1
_PROVIDER_BACKOFF_S = 5.0


def _synthesize_no_final_text(
    messages_json: str, *, current_phase: str,
) -> tuple[str, str]:
    """Genera un fallback accionable cuando el LLM terminó sin texto.

    Bug 2026-08-10: un LLM real (p.ej. minimax M3) puede terminar el
    run con un ModelResponse que solo trae ToolCallPart(s) y ningún
    TextPart, o con un response vacío. `result.output` queda None o "",
    `output_text` se queda como "", y el chat se persiste como
    status="ok" con content="". El usuario en Discord/UI ve un "✓ done"
    sin ninguna respuesta textual — la interacción parece vacía.

    Detección: el ÚLTIMO `ModelResponse` no tiene `TextPart`, pero en
    el historial hay al menos UN `ToolReturnPart` (la tool respondió).
    Eso descarta el caso "el LLM no quiso responder desde el arranque"
    y enfoca el caso real: ejecutó tools y se quedó mudo.

    Devuelve (output_text, last_phase). Si NO detecta el patrón,
    devuelve ("", current_phase) — el caller no se entera. Si el run
    ya cortó por algo accionable (idle/timeout/budget/retries), no
    pisa ese mensaje.

    Idempotente y best-effort: historial corrupto o inesperado → no
    hace nada, no rompe el run.
    """
    if current_phase in _CUT_PHASES:
        return "", current_phase
    if not messages_json:
        return "", current_phase
    try:
        messages = ModelMessagesTypeAdapter.validate_json(messages_json)
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.debug(
            "_synthesize_no_final_text: historial inválido (%r)", e)
        return "", current_phase
    if not messages:
        return "", current_phase
    # ¿Hubo AL MENOS una tool call respondida?
    answered_tools = {
        p.tool_name
        for m in messages if isinstance(m, ModelRequest)
        for p in m.parts if isinstance(p, ToolReturnPart)
    }
    if not answered_tools:
        return "", current_phase
    # ¿El ÚLTIMO response del assistant está mudo?
    last_response = next(
        (m for m in reversed(messages) if isinstance(m, ModelResponse)),
        None)
    if last_response is None:
        return "", current_phase
    if any(isinstance(p, TextPart) for p in getattr(last_response, "parts", [])):
        return "", current_phase
    # Patrón confirmado: tools ejecutadas, respuesta final sin texto.
    tools_list = ", ".join(f"`{n}`" for n in sorted(answered_tools))
    fallback = (
        f"⚠️ El bot ejecutó {tools_list} pero no escribió una respuesta "
        "final. Mandá **continúa** para que redacte el resumen sobre lo "
        "que ya hizo, o pedile explícitamente que cierre con un párrafo "
        "sobre el resultado.")
    return fallback, "no_final_text"

# ---------- medidor de peso de tool results (2026-07-26) ----------
# Instrumentación pura: no cambia ni un byte de lo que viaja al modelo.
# Existe para elegir los caps con datos en vez de a ojo — hoy `run_shell`
# devuelve stdout sin truncar y no sabemos cuánto pesa de verdad.

# Escalera de caps candidatos (chars). Para cada uno acumulamos lo que
# se habría recortado, así el reporte responde "¿dónde pongo el cap?"
# con el ahorro EXACTO en vez de estimarlo desde el promedio — que
# miente feo cuando la distribución tiene cola larga (un `dotnet test`
# de 140KB entre veinte `git status` de 200 chars).
CAP_LADDER = (2_000, 4_000, 8_000, 16_000, 32_000, 64_000)


def _measure_tool_returns(
    node, meter: dict[str, dict], turn: int,
) -> None:
    """Suma los ToolReturnPart del request al acumulador `meter`.

    Best-effort y sin excepciones: si pydantic-ai cambia la forma del
    nodo, dejamos de medir — nunca rompemos un run por el medidor.
    """
    try:
        req = getattr(node, "request", None)
        for part in getattr(req, "parts", []) or []:
            if not isinstance(part, ToolReturnPart):
                continue
            name = getattr(part, "tool_name", None) or "?"
            content = getattr(part, "content", "")
            size = len(content if isinstance(content, str) else repr(content))
            slot = meter.get(name)
            if slot is None:
                slot = meter[name] = {
                    "n": 0, "chars": 0, "max": 0, "weighted": 0,
                    # cap → [chars por encima, esos chars × su turno]
                    "over": {c: [0, 0] for c in CAP_LADDER},
                }
            slot["n"] += 1
            slot["chars"] += size
            slot["max"] = max(slot["max"], size)
            slot["weighted"] += size * turn
            for cap, acc in slot["over"].items():
                excess = size - cap
                if excess > 0:
                    acc[0] += excess
                    acc[1] += excess * turn
    except Exception as e:  # noqa: BLE001
        logger.debug("tool meter: no pude medir el turno %d: %r", turn, e)


def summarize_tool_meter(
    meter: dict[str, dict], turns: int,
) -> dict:
    """Cierra el acumulador: agrega el carry y ordena por costo real.

    `carry_chars` es lo que ese tool le costó al run entero contando el
    reenvío: Σ size×(N−k). Es el número con el que se eligen los caps —
    `chars` a secas subestima a las tools que disparan temprano.

    `cap_saves` responde la otra mitad: para cada cap candidato, cuánto
    carry se habría ahorrado. Misma identidad aplicada al excedente.
    """
    tools = {}
    for name, s in meter.items():
        carry = max(0, turns * s["chars"] - s["weighted"])
        tools[name] = {
            "n": s["n"], "chars": s["chars"], "max": s["max"],
            "avg": s["chars"] // s["n"] if s["n"] else 0,
            "carry_chars": carry,
            "cap_saves": {
                str(cap): max(0, turns * over[0] - over[1])
                for cap, over in (s.get("over") or {}).items()
            },
        }
    return {
        "turns": turns,
        "total_chars": sum(t["chars"] for t in tools.values()),
        "total_carry": sum(t["carry_chars"] for t in tools.values()),
        "tools": dict(sorted(tools.items(),
                             key=lambda kv: -kv[1]["carry_chars"])),
    }


def format_context_note(ctx: Optional[dict]) -> str:
    """Aviso para el humano cuando el hilo se está llenando. "" si hay
    margen de sobra (o si no se pudo medir): no gastamos línea en eso."""
    if not ctx or not ctx["hot"]:
        return ""
    k = lambda n: f"{n / 1000:.0f}k"  # noqa: E731
    return (
        f"\n\n---\n🧠 **Contexto: el run llegó a {k(ctx['peak_tokens'])}/"
        f"{k(ctx['limit'])} ({ctx['peak_pct']}%)** de la ventana; el hilo ya "
        f"arranca en {k(ctx['base_tokens'])} ({ctx['pct']}%) antes de "
        "trabajar. Queda poco margen para tool results, y ahí es donde "
        "aparecen los timeouts.\n"
        "→ `compactar` deja el hilo, la rama y el Discord como están y baja "
        "el contexto a un resumen; si además cambiaste de tema, `cerrar` + "
        "`nuevo`.")


# ---------- streaming al bot: línea legible + diff por tool call ----------
#
# El bot mantiene un timeline vivo (una línea por tool call). El relay
# arma la línea acá porque conoce la semántica de cada tool; el bot solo
# acumula el `message` del notify. Para edit_file el diff sale de los
# args (old→new) — sin tocar el filesystem, robusto ante paths raros.

_STEP_DIFF_MAX_LINES = 40
_STEP_DIFF_MAX_CHARS = 1600


def _args_de_part(part: Any) -> dict:
    """Args de UN `ToolCallPart`. {} si no se pueden parsear.

    Por part y no por nombre a propósito: buscar por nombre devuelve
    siempre el PRIMER match, así que en un response con dos llamadas a
    la misma tool las dos quedaban con los args de la primera.
    """
    args = getattr(part, "args", None)
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


#: Credenciales que los expertos pasan por línea de comandos y que, sin
#: esto, quedaban guardadas para siempre. Barrido del 2026-09-02 sobre
#: `relay.db` + los 1240 `.md` de chats: 1 token de GitHub (94 copias del
#: mismo), 22 headers `Authorization`, 22 `password=`, 8 connection
#: strings, 7 passwords en URL, 3 `api_key` y 1 AWS access key, en siete
#: proyectos de clientes.
#:
#: Se redacta SOLO el secreto y se conserva la etiqueta: `Password=[…]`
#: sigue diciendo que había un password ahí, que es lo que necesita quien
#: lee el log para entender qué pasó. Un `[REDACTED]` que se come la línea
#: entera vuelve el timeline inútil.
_SECRETOS_RE: tuple[tuple[re.Pattern, str], ...] = (
    # Header Authorization: se guarda el esquema, se tapa el valor.
    (re.compile(r"(Authorization\s*:\s*(?:Bearer|token|Basic)\s+)\S+", re.I),
     r"\1[REDACTED]"),
    # Tokens con prefijo reconocible: el prefijo dice de qué era.
    (re.compile(r"\b(gh[pousr]_)[A-Za-z0-9]{20,}"), r"\1[REDACTED]"),
    (re.compile(r"\b(github_pat_)[A-Za-z0-9_]{30,}"), r"\1[REDACTED]"),
    (re.compile(r"\b(sk-(?:ant-)?)[A-Za-z0-9_\-]{20,}"), r"\1[REDACTED]"),
    (re.compile(r"\b(xox[baprs]-)[A-Za-z0-9\-]{10,}"), r"\1[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED-AWS-KEY]"),
    # Credenciales en URL: `postgres://user:pass@host` → tapa solo `pass`.
    (re.compile(r"([a-z][a-z0-9+.\-]*://[^\s:/@]+:)([^\s@]{3,})(@)"),
     r"\1[REDACTED]\3"),
    # `password=`, `pwd=`, `api_key=`, `secret=` y compañía. La comilla
    # de apertura va DENTRO del grupo que se conserva: sin eso,
    # `PASSWORD='secreto'` no matcheaba —la clase excluye comillas, así
    # que el valor no arrancaba nunca— y se filtraban 3 de 17 connection
    # strings del corpus real (medido 2026-09-02).
    (re.compile(r"((?:password|passwd|pwd|api[_-]?key|apikey|secret|"
                r"access[_-]?key|client[_-]?secret)\s*[=:]\s*['\"]?)"
                r"(?!\[REDACTED)([^\s;,'\"]{3,})", re.I),
     r"\1[REDACTED]"),
    # Clave privada PEM: se tapa el cuerpo entero, no solo la cabecera.
    (re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]*?"
                r"(-----END [A-Z ]*PRIVATE KEY-----)"),
     r"\1[REDACTED]\2"),
)


def _redactar(texto: Optional[str]) -> Optional[str]:
    """Tapa credenciales en un string que va a persistirse o mostrarse.

    Se aplica en `_format_tool_step`, que es el productor ÚNICO de las
    cadenas que terminan en `chats.progress_events` (y de ahí en los
    `.md` y en el contexto que se le re-inyecta al LLM). Redactar acá
    cubre los dos `_emit_progress` y los dos callers sin tocarlos.

    Es seguro porque estas cadenas son SOLO display: el comando que se
    ejecuta viaja por los args de la tool, no por acá. Verificado
    2026-09-02: el `cmd` persistido solo se asigna a `turn["cmd"]`,
    `step["cmd"]` y `paso["cmd"]`, nunca se ejecuta.

    Lo que NO hace: el secreto sigue existiendo en memoria mientras el
    comando corre, y en la salida del proceso si la herramienta lo
    imprime. Esto corta la ACUMULACIÓN, que es lo que convertía un uso
    puntual en un archivo permanente.
    """
    if not texto:
        return texto
    for rx, repl in _SECRETOS_RE:
        texto = rx.sub(repl, texto)
    return texto


def _clasificar_partes(mr: Any) -> tuple[list, list[str], list[str]]:
    """Parte un `ModelResponse` en (tools pedidas, textos a narrar, bitácora).

    2026-09-04: `ThinkingPart` estaba en la misma rama que `TextPart` y se
    emitía como `phase="say"`. MiniMax-M3 es de razonamiento —pydantic-ai
    mapea `reasoning_content` → `ThinkingPart`—, así que la UI mostraba el
    chain-of-thought crudo, en inglés, apareado con la narración real
    (mismo timestamp, 6 pares en un run de 6 turnos). Contradecía el
    contrato del consumidor: `server._STEP_PHASES` dice que "pensar" es
    heartbeat (`phase="thinking"`), no contenido — y el resto del módulo
    ya trata `ThinkingPart` como algo a FILTRAR (`_elide_old_response_parts`,
    `_strip_foreign_thinking`).
    Narrar = `TextPart` y nada más.
    """
    pedidas: list[tuple[str, dict, str]] = []
    says: list[str] = []
    recent: list[str] = []
    for part in getattr(mr, "parts", []) or []:
        t = getattr(part, "tool_name", None)
        if t:
            pedidas.append((t, _args_de_part(part),
                            getattr(part, "tool_call_id", "") or ""))
            recent.append(f"{t}:{getattr(part, 'args', None)!r}"[:400])
        elif type(part).__name__ == "TextPart":
            txt = str(getattr(part, "content", "") or "").strip()
            if txt:
                says.append(txt)
    return pedidas, says, recent


def _format_tool_step(
    name: str, args: dict,
) -> tuple[str, str | None, str | None]:
    """(línea legible, diff|None, comando|None) para un tool call.

    Las tres cadenas salen redactadas (ver `_redactar`): son las que se
    persisten en `chats.progress_events`, y los expertos pasan tokens y
    passwords por línea de comandos.
    """
    msg, diff, cmd = _format_tool_step_crudo(name, args)
    return _redactar(msg) or "", _redactar(diff), _redactar(cmd)


def _format_tool_step_crudo(
    name: str, args: dict,
) -> tuple[str, str | None, str | None]:
    """Implementación sin redactar. No llamar directo: usar
    `_format_tool_step`, que es el que tapa credenciales.

    La línea va al timeline del bot y al encabezado de la tarjeta de la
    UI; el diff (solo edit_file) se postea aparte; el `comando` es el
    texto ENTERO de la shell, para que la tarjeta lo muestre sin
    recortar.

    2026-08-27: `shell` —la tool nativa del relay, la que el experto usa
    de verdad desde que `HideToolsToolset` esconde el `run_shell` del
    wrapper— no tenía rama acá y caía al genérico `🔧 {name}`. O sea que
    la UI decía "shell" y nada más: ni qué comando corrió ni con qué
    salió. Ese era el agujero reportado.
    """
    def _clip(s: Any, n: int = 80) -> str:
        s = str(s or "")
        return s if len(s) <= n else s[: n - 1] + "…"

    if name == "read_file":
        return f"📄 leyó `{_clip(args.get('path'))}`", None, None
    if name == "list_dir":
        return f"📂 listó `{_clip(args.get('path') or '.')}`", None, None
    if name == "directory_tree":
        return f"🌳 árbol de `{_clip(args.get('path') or '.')}`", None, None
    if name == "write_file":
        content = args.get("content") or ""
        n_lines = content.count("\n") + 1 if content else 0
        return (f"📝 escribió `{_clip(args.get('path'))}` ({n_lines} líneas)",
                None, None)
    if name == "move_file":
        return (f"🔀 movió `{_clip(args.get('src'))}` → "
                f"`{_clip(args.get('dst'))}`", None, None)
    if name in _CONSOLE_TOOLS:
        cmd = str(args.get("cmd") or args.get("command") or "")
        bg = " [background]" if args.get("background") else ""
        cwd = str(args.get("cwd") or "")
        # El encabezado lleva la PRIMERA línea: un heredoc o un script
        # de varias líneas no puede empujar el resto de la tarjeta.
        # El comando entero (y el cwd, que cambia lo que significa) va
        # al cuerpo desplegable.
        detalle = cmd + (f"\n# cwd: {cwd}" if cwd else "")
        return (f"⚙️ shell{bg}: `{_clip(cmd.split(chr(10))[0], 100)}`",
                None, detalle or None)
    if name == "edit_file":
        path = _clip(args.get("path"))
        old = str(args.get("old") or "")
        new = str(args.get("new") or "")
        diff = _unified_diff(old, new, path)
        added = sum(
            1 for ln in diff.splitlines()
            if ln.startswith("+") and not ln.startswith("+++")) if diff else 0
        removed = sum(
            1 for ln in diff.splitlines()
            if ln.startswith("-") and not ln.startswith("---")) if diff else 0
        return f"✏️ editó `{path}` (+{added} −{removed})", diff, None
    if name == "cbm_query":
        tool = args.get("tool") or "query"
        return f"🔎 cbm `{_clip(tool, 40)}`", None, None
    return f"🔧 {name}", None, None


def _unified_diff(old: str, new: str, path: str) -> str | None:
    """Diff unificado capeado de old→new. None si son iguales o vacío."""
    if old == new:
        return None
    import difflib
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=path, tofile=path, lineterm="", n=2))
    if not lines:
        return None
    if len(lines) > _STEP_DIFF_MAX_LINES:
        lines = lines[:_STEP_DIFF_MAX_LINES] + [
            f"… (+{len(lines) - _STEP_DIFF_MAX_LINES} líneas más)"]
    text = "\n".join(lines)
    if len(text) > _STEP_DIFF_MAX_CHARS:
        text = text[:_STEP_DIFF_MAX_CHARS] + "\n…(diff truncado)"
    return text


# Salida de consola que se muestra en la tarjeta. Más chica que el cap
# que ve el modelo (`SHELL_RESULT_CAP`, 48k) a propósito: esto viaja en
# CADA poll de /experts/status (cada 1.5s) y se persiste en
# `chats.progress_events`. Se reserva cola porque el veredicto —el
# `exit=N`, el traceback, el "3 failed"— vive al final.
_STEP_OUT_MAX = 2400
_STEP_OUT_TAIL = 900
# …y un techo para el RUN entero: `progress_events` se guarda en la fila
# del chat sin recorte, y un run de 300 tools escribiendo 2.4KB cada una
# le mete 700KB a la DB. Pasado el techo se deja de adjuntar salida (las
# tarjetas siguen mostrando el comando).
_STEP_OUT_BUDGET = 120_000


def _console_tool_outputs(node) -> list[tuple[str, str, str]]:
    """(tool, salida, tool_call_id) de las tools de consola que devolvieron.

    Lo que contesta la terminal era el dato que la UI nunca mostraba:
    se veía QUÉ tool corrió, nunca con qué salió, así que mirar un run
    en vivo no decía si iba bien o mal.

    ponytail: solo consola. Meter el resultado de TODAS las tools
    multiplicaría por diez el payload del poll y el tamaño de
    `progress_events` en la DB, para mostrar el `read_file` de un
    archivo que ya está en el repo. Si hace falta otra tool, se agrega
    su nombre acá.

    Best-effort como `_measure_tool_returns`: si pydantic-ai cambia la
    forma del nodo dejamos de mostrar salida, nunca rompemos el run.
    """
    out: list[tuple[str, str, str]] = []
    try:
        req = getattr(node, "request", None)
        for part in getattr(req, "parts", []) or []:
            if not isinstance(part, ToolReturnPart):
                continue
            name = getattr(part, "tool_name", None) or ""
            if name not in _CONSOLE_TOOLS:
                continue
            content = getattr(part, "content", "")
            text = content if isinstance(content, str) else repr(content)
            out.append((name, _cap_text(
                text.strip() or "(sin salida)",
                _STEP_OUT_MAX, keep_tail=_STEP_OUT_TAIL),
                getattr(part, "tool_call_id", "") or ""))
    except Exception as e:  # noqa: BLE001
        logger.debug("no pude leer la salida de consola del turno: %r", e)
    return out


def _tool_loop_detected(recent: "deque[str]") -> bool:
    """Ventana llena de tool calls idénticas (tool + args) = loop.

    Es el gate del auto-continue de presupuesto (2026-07-20c): si las
    últimas N llamadas son exactamente la misma operación, extender el
    presupuesto solo alarga el loop. Tools variadas = trabajo real.
    ponytail: heurística naive — no ve loops alternados (A,B,A,B…);
    subir a detección con período si aparece ese caso real.
    """
    return len(recent) == recent.maxlen and len(set(recent)) == 1


def build_instructions(
    project: dict, ponytail: str, skills_block: str,
) -> str:
    """Arma el system prompt completo del experto SIN bloque git.

    El system prompt es ESTABLE a propósito (2026-07-28): es el prefijo
    que cachean los providers, y cualquier byte que cambie entre runs
    invalida la cache de TODO el historial que va detrás. Por eso el
    bloque de git diff — que cambia cada vez que el experto escribe un
    archivo — salió de acá y vive en la tool nativa `git_diff()`.
    Medido en `relay.db`: los resumes con el diff adentro arrancaban
    en 0% de cache hit contra 94% de los que no lo movían.

    Per-project:
    - `defaults_json.inject_skills == false` → bloque de skills vacío.
    - `defaults_json.skills_mode == "compact"` → solo nombres de skills
      (no descripciones); el LLM llama `read_skill(name)` on-demand.
      Requiere la tool nativa `read_skill` registrada (iter 10.4).
    - default → bloque completo embebido (modo histórico).
    """
    workspace_block = extract_workspace_block(
        [{"path": project["repo_path"], "name": project["slug"]}]
    )
    defaults = project.get("defaults_json") or {}
    inject_skills = defaults.get("inject_skills", True)
    skills_mode = defaults.get("skills_mode", "embed")
    if not inject_skills:
        chosen_skills_block = ""
    elif skills_mode == "compact":
        # Re-construimos el bloque compact acá (en vez de recibirlo
        # como param) porque el caller siempre pasa el bloque "embed".
        # _build_index lee el dir de skills cada vez (es un glob de
        # ~20 dirs); no usamos SkillCache porque build_instructions es
        # sync y la cache es async. El costo es trivial.
        from . import skills as _skills_mod  # lazy
        try:
            dirs_ = _skills_mod.skills_dirs(project.get("repo_path") or "")
            index = _skills_mod.build_index_multi(dirs_)
            chosen_skills_block = _skills_mod.render_index_compact(index)
        except Exception:  # noqa: BLE001 — best-effort
            chosen_skills_block = skills_block  # fallback al embed
    else:
        # El `skills_block` que llega por parametro sale de un SkillCache
        # con el directorio GLOBAL, compartido por los 50 proyectos: no ve
        # las skills del repo ni respeta el filtro. Lo rearmamos solo
        # cuando hace falta —el repo tiene skills propias, o el proyecto
        # declara una lista— y en el caso comun seguimos usando el cache.
        _filtro = defaults.get("skills")
        _repo = project.get("repo_path") or ""
        from . import skills as _skills_mod  # lazy
        try:
            _propio = len(_skills_mod.skills_dirs(_repo)) > 1
        except Exception:  # noqa: BLE001 — best-effort
            _propio = False
        if _propio or _filtro is not None:
            try:
                chosen_skills_block = _skills_mod.bloque_del_proyecto(
                    _repo, _filtro)
            except Exception:  # noqa: BLE001 — un dir raro no voltea el run
                logger.warning(
                    "skills: no pude armar el bloque de %s; uso el global",
                    project.get("slug"), exc_info=True)
                chosen_skills_block = skills_block
        else:
            chosen_skills_block = skills_block
    parts = [
        ponytail,
        project.get("system_prompt", ""),
        chosen_skills_block,
        workspace_block,
        TOOL_FALLBACK_BLOCK,
        BATCH_ARTIFACTS_BLOCK,
        BITACORA_BLOCK,
    ]
    return "\n\n".join(p for p in parts if p)


# 2026-08-17. Va en el bloque SIEMPRE-ON a propósito: `EVIDENCE_BLOCK`
# tiene la regla equivalente para el browser, pero solo se inyecta cuando
# hay un MCP de browser adjunto — y el caso real que motivó esto no usó el
# browser.
#
# Qué pasó (un chat de ejemplo, sample-shop): el experto escribió su propio script
# de Playwright, lo corrió UNA vez, y salieron 40 PNG. La SPA rebotaba
# cada navegación a `/accept-terms`, así que 31 de esas 40 eran la misma
# pantalla de términos y condiciones. El script terminó con exit 0, el
# experto contó 40 archivos y siguió como si hubiera avanzado. Recién al
# final escribió un `dedup.py`, hasheó, y descubrió el problema —después
# de gastar 20 minutos y 172 tool calls.
#
# Ningún detector de loops podía verlo: las 40 capturas salieron de UNA
# sola tool call. La repetición pasó ADENTRO del script, donde el harness
# no mira. Por eso la regla es para el modelo y no un guard en Python.
BATCH_ARTIFACTS_BLOCK = """\
## Lotes de artefactos: verifica DOS antes de generar cuarenta

Si produces varios archivos de una sola pasada —capturas, exports, PDFs,
fixtures, reportes— con un script tuyo, que el script termine sin error
NO significa que los archivos sean distintos. Un script que navega y
captura escribe 40 archivos igual de contento si las 40 navegaciones
rebotaron al mismo login.

Regla: genera DOS, compáralos, y recién entonces genera el resto.
Comparar es comparar, no mirar: `sha256sum` / `Get-FileHash`, o el tamaño
en bytes. Si salen idénticos donde deberían diferir, PARA y arregla la
causa; seguir generando multiplica el error, no lo avanza.

Los hashes detectan duplicados, pero no prueban calidad visual. Para
imágenes usa read_image o una captura con bytes y un modelo con visión;
si no recibiste la imagen, declara ese límite. "El script corrió y escribió N archivos" no es
progreso verificado; es un conteo.

Y si al comparar descubres que estabas repitiendo, dilo en la respuesta
con el número. Un "31 de 40 salieron iguales, la causa es X" es una
respuesta útil; cuarenta archivos entregados como si estuvieran bien, no."""


# 2026-08-17. La contracara de la elisión (capa 2, `TOOL_KEEP_FULL`): los
# tool results viejos se reemplazan por un muñón de 200 chars, así que en
# un run largo el experto NO PUEDE VER lo que verificó hace diez llamadas.
#
# Qué pasó (sample-shop, 42 runs en un día, 172 tool calls en el más largo): el
# experto reportó como hechos lotes de lint que `git status` desmentía, y
# terminó pidiéndole al humano que confirmara a mano un `docker --version`
# que él mismo podía correr. No mentía: tenía amnesia. Reconstruía desde
# muñones y rellenaba los huecos.
#
# Subir `TOOL_KEEP_FULL` no lo arregla — reintroduce el blowup N² que la
# elisión existe para cortar. Lo que hace falta es un lugar CHICO y
# DURABLE donde lo verificado se acumule. La bitácora se re-renderiza en
# las instructions de cada request (ver `_build_agent`), o sea que vive
# fuera del historial elidible y sobrevive todo el run.
BITACORA_BLOCK = """\
## Bitácora: anota lo que verificas, porque te vas a olvidar

Tu historial se recorta. Los resultados de tools viejas se reemplazan por
un resumen de dos líneas para ahorrar contexto, así que dentro de veinte
tool calls NO vas a poder releer lo que hoy tienes delante.

Por eso: cada vez que compruebes un hecho que vas a necesitar después,
llama `anotar(hecho)`. Un hecho es algo que verificaste con una tool y
que sobrevive al olvido:

- `anotar("docker 29.6.1 responde; el daemon está arriba")`
- `anotar("31 de las 40 capturas tienen el mismo sha256")`
- `anotar("npm run lint: 3 errores, 215 warnings (exit=1)")`
- `anotar("el seeder crea 3 roles: admin@example.test, usuario-demo@example.test, member@example.test")`

Anota el RESULTADO, no la intención: "corrí el build" no sirve, "el build
pasó en 7.66s" sí. Si algo falló, anótalo igual — un hecho negativo
verificado vale tanto como uno positivo.

Cuando escribas la respuesta final, ARMALA DESDE LA BITÁCORA. Si vas a
afirmar que algo quedó hecho y no está anotado, no lo afirmes: o lo
verificas de nuevo ahora, o lo dices como pendiente. Un reporte que dice
"hice X" sobre algo que no hiciste cuesta más que no haber hecho nada,
porque el humano lo descubre un día después.

## Ejecutar no es describir

Escribir un comando en tu respuesta NO lo ejecuta. Esto no sirve de nada:

    Verificación del host:
    ```
    PS> docker --version
    ```
    ¿Me confirmas si el comando se ejecutó?

El comando corre cuando llamas la tool `shell`, y ahí recibes su salida.
Nunca pidas que un humano te confirme el resultado de algo que puedes
ejecutar: ya tienes permiso para leer, escribir y ejecutar, y esperar esa
confirmación convierte un turno de treinta segundos en un día perdido.

Vale para todo el turno, no solo para el cierre: si terminas de escribir
tu respuesta y no llamaste ni una herramienta, no hiciste el trabajo —
describiste el plan de hacerlo. La excepción única es `ask_human`, para
cuando de verdad hace falta una decisión que no es tuya.

## Al escribir un hallazgo: leído vs. inferido

Distingue lo que LEÍSTE de lo que DEDUCES a partir de eso, y marca lo
segundo como tal ("infiero que…", "no leí X, asumo que…"). Una conclusión
sin esa marca se lee como verificada aunque no lo esté.

## Todo documento nace con el commit contra el que se verificó

Si escribes un reporte, review o análisis, pon en el encabezado el SHA
corto del HEAD actual (`git rev-parse --short HEAD`). Así una corrida
futura puede hacer `git diff <sha>..HEAD` sobre los archivos que citas y
saber si hace falta re-verificar en vez de confiar a ciegas.

## Cita el símbolo, no la línea

Al referenciar código usa `archivo:símbolo` (función, clase, atributo), no
`archivo:línea` — el número se pudre con el primer commit que entra en el
medio, el símbolo se encuentra grepeando."""


# Constantes exportadas para que tests/imports no tengan que entrar a la clase.
# Las marcas reales viven en `Bitacora.MAX_*`.
BITACORA_MAX_PASOS = 12  # pasos marcados del plan (Etapa B, P2)


class Bitacora:
    """Los hechos verificados de un run, inmunes a la elisión.

    Contracara de la capa 2 (`_elide_old_tool_returns`): esa recorta el
    historial para cortar el N², y al hacerlo le borra al experto lo que
    ya comprobó. Acá se acumula lo que él decide que vale guardar, y
    `render()` se engancha como instructions dinámicas —fuera del
    historial elidible, re-armadas en cada request.

    Los topes no son decorativos: esto se re-manda en CADA turno. Sin
    techo, arreglar la amnesia costaría el mismo blowup que la elisión
    vino a evitar. Peor caso ~4KB por request, contra los ~48KB que puede
    pesar UN solo result de shell.
    """

    MAX = 60            # hechos; pasado el tope se cae el más viejo
    MAX_CHARS = 4000    # techo duro de lo que se re-manda por request
    MAX_COMANDOS = 20   # comandos; ring aparte, ver `anotar_comando`
    MAX_PASOS = 12      # pasos marcados del plan (Etapa B, P2)

    def __init__(self) -> None:
        self.hechos: list[str] = []
        self.comandos: list[str] = []
        self.pasos: dict[int, str] = {}   # paso -> nota corta

    def anotar_comando(self, cmd: str, code: Optional[int]) -> None:
        """Registra un comando ejecutado. Lo llama el harness, no el modelo.

        La bitácora de `anotar` depende de que el experto elija usarla, y
        lo que se midió es justamente que no se auto-reporta bien: llegó a
        pedirle al humano que confirmara a mano un `docker --version` que
        él mismo había podido correr. Esto no le pide permiso a nadie —
        cada comando deja su rastro con el exit code.

        Ring propio y no la lista de hechos: un run de 172 tool calls
        vaciaría los 60 hechos del modelo con puro log de comandos.
        """
        estado = f"exit={code}" if code is not None else "activo; disponibilidad sin verificar"
        linea = f"$ {' '.join((cmd or '').split())[:120]} → {estado}"
        self.comandos.append(linea)
        del self.comandos[:-self.MAX_COMANDOS]

    def anotar(self, hecho: str) -> str:
        """Agrega un hecho. Devuelve el ack que ve el LLM."""
        h = " ".join((hecho or "").split())[:300]
        if not h:
            return "Bitácora sin cambios: el hecho venía vacío."
        if h in self.hechos:
            return f"Ya estaba anotado ({len(self.hechos)} hechos)."
        if len(self.hechos) >= self.MAX:
            # En un run largo lo reciente es lo que necesita para
            # cerrar, y un techo que cede deja de ser un techo.
            self.hechos.pop(0)
        self.hechos.append(h)
        return f"Anotado ({len(self.hechos)} hechos en la bitácora)."

    def evidencia(self, max_chars: int = 1500) -> str:
        """Lo comprobable del run, para el verificador. `""` si no hay nada.

        El verificador venía recibiendo el resultado del ejecutor y una
        lista de nombres de tools con sus argumentos. Con eso puede ver
        que se LLAMÓ a `shell` con `pytest`, y no puede distinguir
        "corrió las pruebas" de "las pruebas pasaron" — que es la
        diferencia entre aprobar trabajo terminado y aprobar una
        intención.

        Los comandos van PRIMERO y separados del resto a propósito: un
        `exit=0` lo escribió el harness y es un hecho; un "verifiqué que
        compila" lo escribió el modelo y es una afirmación suya. El
        verificador tiene que poder pesarlas distinto, así que se le
        entregan etiquetadas distinto.

        El tope es más chico que `MAX_CHARS` porque esto viaja en el
        prompt de una etapa auxiliar que corre en el modelo barato: la
        evidencia tiene que caber sin desplazar al plan ni al resultado.
        """
        partes = []
        if self.comandos:
            partes.append("### Comandos que corrió el harness (exit code real)\n"
                          + "\n".join(self.comandos))
        if self.pasos:
            partes.append(
                "### Pasos del plan que el ejecutor marcó como hechos\n"
                + "\n".join(f"- paso {k}: {v}"
                            for k, v in sorted(self.pasos.items())))
        if self.hechos:
            partes.append("### Lo que el ejecutor dice haber comprobado\n"
                          + "\n".join(f"- {h}" for h in self.hechos))
        texto = "\n\n".join(partes)
        aviso = "\n[evidencia recortada; las omisiones no prueban éxito ni fallo]"
        return (texto if len(texto) <= max_chars
                else texto[:max(0, max_chars - len(aviso))] + aviso[:max_chars])

    def volcar(self) -> str:
        """La bitácora como JSON, para guardarla entre turnos. `""` si está vacía.

        2026-08-26. Hasta hoy esto vivía solo en memoria del run: se
        engancha como *instructions*, y el choke point de persistencia
        (`_dump_messages`) hace `_strip_instructions` antes de guardar.
        Resultado: lo único que registraba qué había verificado el
        experto se perdía al terminar el turno — y en un corte por
        `off_plan`, donde el historial además pierde las tool calls
        enteras, el siguiente **continuá** arrancaba sin nada.

        Etapa B (2026-09): `pasos` es lo que el ejecutor va marcando con
        `plan_step_done`. Se serializa con claves string porque json no
        acepta int como key, y el destino es `chats.stages_json` que es
        texto SQLite.
        """
        if not self.hechos and not self.comandos and not self.pasos:
            return ""
        return json.dumps(
            {"hechos": self.hechos, "comandos": self.comandos,
             "pasos": {str(k): v for k, v in self.pasos.items()}},
            ensure_ascii=False)

    @classmethod
    def cargar(cls, raw: str) -> "Bitacora":
        """Reconstruye desde `volcar()`. Best-effort: JSON roto → vacía.

        Nunca tira: una bitácora ilegible es peor contexto, no un run
        muerto. Y se re-aplican los topes al cargar, porque el JSON pudo
        haberse guardado con una versión que tenía otros límites.
        """
        def _lista(v, tope):
            # `isinstance(list)` y no un `or []`: un JSON con
            # `"hechos": "texto"` es iterable, y sin este chequeo la
            # comprensión recorre CARACTERES y le mete al modelo una
            # bitácora de letras sueltas. Lo encontró el test, no yo.
            if not isinstance(v, list):
                return []
            return [str(x) for x in v][-tope:]

        b = cls()
        try:
            d = json.loads(raw or "")
            if not isinstance(d, dict):
                raise ValueError("la bitácora no es un objeto")
            b.hechos = _lista(d.get("hechos"), cls.MAX)
            b.comandos = _lista(d.get("comandos"), cls.MAX_COMANDOS)
            # Etapa B: pasos marcados por `plan_step_done`.
            ps = d.get("pasos")
            if isinstance(ps, dict):
                # Los dicts no soportan `[-N:]` — el último run
                # tiraba KeyError. Hay que ir por `items()` y volver
                # a armar el dict.
                pares = [(int(k), str(v)[:200])
                         for k, v in ps.items()
                         if str(k).isdigit()][-cls.MAX_PASOS:]
                b.pasos = dict(pares)
        except (ValueError, TypeError, AttributeError):
            logger.warning("bitácora ilegible al cargar; sigo con una vacía")
        return b

    def fusionar_pasos_verificados(self, pasos) -> None:
        """Fusión desde el verificador (Etapa B P5).

        El verificador vio qué pasos quedaron cubiertos aunque el
        ejecutor no los haya marcado con `plan_step_done`. Los sumamos
        a `self.pasos` con una nota estándar, sin pisar los que ya
        marcó el ejecutor. Tope igual que `marcar_paso`.
        """
        for raw in (pasos or []):
            try:
                idx = int(raw)
            except (TypeError, ValueError):
                continue
            if idx < 1:
                continue
            if idx in self.pasos:
                continue
            self.pasos[idx] = "verificado por el verificador"
        if len(self.pasos) > self.MAX_PASOS:
            for viejo in sorted(self.pasos)[:-self.MAX_PASOS]:
                del self.pasos[viejo]

    def marcar_paso(self, n: int, nota: str = "") -> None:
        """Marca el paso `n` (1-based) como completado (Etapa B, P2).

        Acepta cualquier entero (incluso fuera de rango): el cap a
        `MAX_PASOS` mantiene el techo del 4KB por request, y la
        validación contra el plan la hace el orquestador al armar el
        dict de salida (no la tool — un número mal puesto no puede
        cortar el run).
        """
        try:
            n = int(n)
        except (TypeError, ValueError):
            return
        if n < 1:
            return
        # Notas largas se cortan acá: la UI las muestra al hover y 200
        # chars es más que suficiente.
        self.pasos[n] = " ".join((nota or "").split())[:200]
        # Techo: si el modelo decide marcar 50 pasos, dejamos los
        # últimos MAX_PASOS (los recientes son los que importan).
        if len(self.pasos) > self.MAX_PASOS:
            for viejo in sorted(self.pasos)[:-self.MAX_PASOS]:
                del self.pasos[viejo]

    def render(self) -> str:
        """El bloque para las instructions. "" si no hay nada anotado."""
        bloques: list[str] = []
        if self.hechos:
            lineas: list[str] = []
            total = 0
            # De atrás para adelante: si hay que recortar, se recorta lo
            # viejo, no lo último que verificó.
            for h in reversed(self.hechos):
                total += len(h) + 3
                if total > self.MAX_CHARS:
                    lineas.append(
                        f"- […{len(self.hechos) - len(lineas)} hechos más "
                        "viejos omitidos por espacio]")
                    break
                lineas.append(f"- {h}")
            bloques.append(
                "## Bitácora de este run (lo que YA verificaste)\n"
                + "\n".join(reversed(lineas)))
        if self.comandos:
            bloques.append(
                f"## Últimos {len(self.comandos)} comandos que ejecutaste\n"
                + "\n".join(self.comandos)
                + "\n\nEsta lista la escribe el harness, no el modelo: si "
                "un comando aparece aquí, se ejecutó de verdad. No pidas "
                "confirmación humana de algo que ya ejecutaste.")
        if not bloques:
            return ""
        return "\n\n".join(bloques) + (
            "\n\nArma tu respuesta final desde esto. Lo que no aparezca "
            "aquí, no lo afirmes como hecho.")


TOOL_FALLBACK_BLOCK = """\
## Fallback de tools (cuando una tool falla, no insistas: cambia a otra)

Regla dura: si una tool te da error 2 veces seguidas con la misma
operación, NO la llames una tercera vez. Pasa al fallback:

- `cbm_query` falla o devuelve `{"error": ...}`:
  1. Prueba una vez más con args distintos (p.ej. agregar `limit=10`
     o cambiar el `name_pattern`).
  2. Si sigue fallando, NO llames `cbm_query` de nuevo. Usa
     `list_dir(path)` para navegar el repo manualmente, y
     `read_file(path)` para leer el archivo puntual que necesitabas.

- Archivos que editaste TÚ en esta corrida (`edit_file`, `write_file`,
  `move_file`): léelos con `read_file`, NUNCA con `cbm_query`. El índice
  se refresca ~6s después del edit (watcher con debounce), así que
  dentro del mismo turno cbm te devuelve la versión vieja. Para el
  resto del repo, que no tocaste, `cbm_query` sigue siendo lo primero.

- `read_file` falla con path inválido o "file not found":
  1. Verifica con `list_dir(<directorio_padre>)` qué archivos existen.
  2. Si encontraste el archivo real con nombre/casing distinto,
     usa ese path. NO repitas el path original.

- `shell` falla con exit != 0:
  1. Relee el stderr. Si es un error transitorio (timeout de red, lock
     de archivo), reintenta UNA vez.
  2. Si es un error de tu propio comando (sintaxis, flag mal),
     corrígelo y reintenta. NO repitas el mismo comando.

- `list_dir` devuelve resultado vacío donde esperabas contenido:
  1. Sube el `max_depth` o navega un nivel más adentro. NO asumas
     que el repo está vacío.

- En general: si una tool no te da lo que necesitas, piensa QUÉ OTRA
  tool del set te puede dar esa info, y úsala. NO insistas en la
  misma tool con los mismos args.

Este fallback es preferible a que el experto aborte el chat con
`Tool 'X' exceeded max retries count of 3` — tú decides cuándo
abandonar una tool, no pydantic-ai."""


# Se inyecta SOLO cuando hay un MCP de browser adjunto al run (ver
# run_expert). Sin browser el bloque sería ruido que se paga en tokens.
#
# 2026-08-19: este bloque describía las tools de `mcp_servers/
# playwright_mcp.py` (`navigate`, `get_text`, `screenshot(path)`), que
# NO es lo que se enchufa. La fila `playwright-mcp` del catálogo apunta
# a `@playwright/mcp` de Microsoft (npx sobre el clone de install_dir),
# cuyas tools son `browser_*`. O sea, otra vez el bug de 2026-08-16 —
# el prompt nombrando tools inexistentes — pero al revés: el rewrite de
# ese día sacó los nombres `browser_*` justo cuando pasaron a ser los
# correctos, y encima agregó "no hay click, ni type, ni form fill"
# cuando los tres existen. Costo medido: el pedido de manual de usuario
# de sample-shop (19/8) nunca llamó a `use_capability("browser")` —el prompt
# le decía que no servía— y se fue una hora escribiendo scripts de
# Playwright a mano por `shell`.
#
# Si cambia la fila del catálogo, este bloque cambia con ella: el guard
# es `test_evidence_block_nombra_tools_que_existen`.
#
# El `mcp_servers/playwright_mcp.py` del repo quedó huérfano (nadie lo
# spawnea). No lo borro en este diff, pero es candidato: hoy lo único
# que hace es confundir a quien lee estos comentarios.
EVIDENCE_BLOCK = """\
## Evidencia obligatoria al comprobar un flujo de browser

Usa los nombres y parámetros del catálogo ACTIVO. No inventes herramientas.
El Playwright MCP instalado expone `browser_navigate({url})`,
`browser_snapshot()`, `browser_click({target})` y
`browser_type({target, text, submit})`. `target` acepta un ref del snapshot
actual o un selector único comprobado. `element` es una descripción opcional.
No existe `browser_find` en este catálogo.

Navega, toma un snapshot, opera el control y comprueba el resultado con otro
snapshot y, cuando corresponda, `browser_console_messages()` y
`browser_network_requests()`. En un SPA espera el estado esperado con
`browser_wait_for({text})`. Ante un fallo, inspecciona el estado antes de
repetir una acción que podría tener efectos. Un login que vuelve a /login
no demuestra éxito.

### Capturas y verificación visual
`browser_take_screenshot({type: "png", fullPage: true})` devuelve la imagen
al relay además de guardar un archivo. Omite `filename` para recibir bytes:
el proceso instalado devuelve SOLO texto si especificas `filename`.
Si necesitas un nombre, usa una ruta absoluta dentro del repo y después
`read_image({path: "ruta absoluta"})`. Esta lectura respeta raíces, rutas
vedadas y el límite ATTACHMENT_MAX_BYTES; solo admite PNG/JPEG/GIF/WebP.

El relay archiva las imágenes devueltas por herramientas y las agrega a la
respuesta final para el panel y Discord. No necesitas subirlas con curl ni
recordar un id. Si el modelo admite visión, recibe los bytes; si no, la tool
lo indica explícitamente. No afirmes haber visto una imagen que no recibiste.
Un snapshot es evidencia de estructura y texto, no de apariencia visual.
Un PID no comprueba disponibilidad; un PNG o su hash no comprueba entrega
al modelo ni calidad visual. Para lotes, prueba dos casos distintos y compara
su contenido antes de producir el resto. Cita las comprobaciones reales y
sus límites; si algo falla o no se verificó, dilo.

### Entorno y cierre
El browser comparte la máquina del relay: localhost permite acceder a un
servidor local previamente comprobado. Usa perfiles aislados para pruebas.
`browser_close()` libera el navegador cuando termines."""


GIT_CAPTURE_TIMEOUT_S = 5.0
DIFF_MAX_BYTES = 20_000
# Mensaje UNA VEZ por repo si git no está, para no spammear en cada run.
_git_unavailable_logged: set[str] = set()


def _capture_git_diff_sync(repo_path: str) -> dict:
    """Captura `git status --porcelain` y `git diff HEAD --no-color`.

    Devuelve dict:
        {"ok": bool, "status": str, "diff": str, "sha": str, "branch": str,
         "stderr": str}

    Best-effort total: cualquier excepción / repo sin git / git no
    instalado → {"ok": False, "status": "skipped"}.

    Cambio 2026-07-08 (Sub-ola 2.7): ahora stdout y stderr van separados.
    Antes se concatenaban, lo que contaminaba el campo `diff` con
    warnings de git (mensajes en stderr según la locale: "nothing to
    commit, working tree clean", warnings de permisos, etc.) que
    terminaban inflando el response del endpoint admin y colgando el
    modal de la UI. `stderr` se expone para diagnóstico pero NO se
    mezcla con `diff`.
    """
    def _run(args: list[str], timeout: float = GIT_CAPTURE_TIMEOUT_S) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=repo_path, capture_output=True, text=True,
                # `text=True` a secas decodifica con la locale (cp1252 en
                # Windows) y un diff con acentos/UTF-8 revienta con
                # UnicodeDecodeError DENTRO del reader thread de
                # subprocess: el traceback se imprime suelto en el log y
                # acá volvía stdout vacío, o sea el diff se perdía en
                # silencio. git habla UTF-8: decodificamos como tal, y lo
                # que no entre se reemplaza (bug 2026-07-21).
                encoding="utf-8", errors="replace",
                timeout=timeout, check=False,
            )
            return proc.returncode, (proc.stdout or ""), (proc.stderr or "")
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return 1, "", ""

    # 1) ¿es un repo git?
    rc, _, _ = _run(["rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        if str(repo_path) not in _git_unavailable_logged:
            logger.info("git: %s no es working tree git (salteando bloque diff)", repo_path)
            _git_unavailable_logged.add(str(repo_path))
        return {"ok": False, "status": "not_git_repo"}

    # 2) branch + HEAD sha (best-effort, 2 calls en paralelo? no, simple)
    _, branch_text, _ = _run(["rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch_text.strip() or "HEAD"
    _, sha_text, _ = _run(["rev-parse", "--short", "HEAD"])
    sha = sha_text.strip() or "?"

    # 3) status --porcelain (solo stdout; stderr es warnings de git)
    _, status_text, status_err = _run(["status", "--porcelain"])

    # 4) diff --no-color (solo stdout)
    rc, diff_text, diff_err = _run(["diff", "HEAD", "--no-color"])

    # stderr combinado para diagnóstico (NO se mezcla con diff)
    diag_stderr = (status_err + diff_err).strip()

    return {
        "ok": True,
        "status": status_text.strip(),
        "diff": diff_text,
        "sha": sha,
        "branch": branch,
        "stderr": diag_stderr,
    }


def _build_git_diff_block_sync(repo_path: str) -> str:
    """Compone el bloque markdown '## Cambios en el workspace (git)'.

    Devuelve "" si no es repo git o si git no está disponible.
    Aplica cap DIFF_MAX_BYTES al diff (trunca con footer).
    """
    info = _capture_git_diff_sync(repo_path)
    if not info.get("ok"):
        return ""

    status = info.get("status", "")
    diff = info.get("diff", "")
    branch = info.get("branch", "?")
    sha = info.get("sha", "?")

    lines = [
        "## Cambios en el workspace (git)",
        "",
        f"Branch: `{branch}` • HEAD: `{sha}`",
        "",
    ]
    if not status and not diff:
        lines.append("Working tree clean. Sin cambios pendientes.")
    else:
        if status:
            lines.append("Status (`git status --porcelain`):")
            lines.append("```")
            for ln in status.splitlines():
                if ln.strip():
                    lines.append(ln)
            lines.append("```")
            lines.append("")
        if diff:
            diff_text = diff
            truncated = False
            if len(diff_text.encode("utf-8")) > DIFF_MAX_BYTES:
                # Truncar en frontera de línea.
                truncated_bytes = diff_text.encode("utf-8")[:DIFF_MAX_BYTES].decode("utf-8", errors="ignore")
                last_nl = truncated_bytes.rfind("\n")
                if last_nl > 0:
                    truncated_bytes = truncated_bytes[:last_nl]
                diff_text = truncated_bytes
                truncated = True
            lines.append("Diff (`git diff HEAD --no-color`):")
            lines.append("```diff")
            lines.append(diff_text.rstrip())
            lines.append("```")
            if truncated:
                lines.append("")
                lines.append(
                    f"_Diff truncado a {DIFF_MAX_BYTES // 1024}KB. "
                    f"Para el resto: `git show {sha}` o `git diff {sha}~1..{sha}`._"
                )
    return "\n".join(lines)


async def run_expert(
    project: dict, user: str, *, skills_block: str = "",
    system_extra: str = "", model_override: str = "",
    db: Any = None, message_history_json: str = "",
    on_progress: Any = None,
    steer: Optional[list[str]] = None, rescue: Optional[dict] = None,
    mcp_with: Optional[list[str]] = None, mcp_pool: Any = None,
    images: Optional[list[tuple[bytes, str]]] = None,
    chat_id: str = "", conversation_id: str = "",
    archivos_reservados: Optional[Iterable[str]] = None,
    on_leg_boundary: Any = None,
    bitacora_json: str = "",
    deadline_pedido: float = 0.0,
    image_artifacts: Optional[dict] = None,
) -> dict:
    """Corre el experto del proyecto. Devuelve dict con content/usage.

    ADR-025: `message_history_json` es el historial pydantic-ai
    serializado (ModelMessagesTypeAdapter) de la conversación; si viene,
    se replaya con `agent.run(user, message_history=...)`. Como el
    experto usa `instructions=` (no system_prompt), pydantic-ai las
    re-aplica frescas cada run y NO quedan en el historial — el bloque
    de git diff (ADR-020) se recalcula cada turno sin duplicarse.
    El result dict incluye `messages_json` (historial completo
    actualizado) para que el caller lo persista.

    Levanta ModelUnavailable si el modelo no se puede armar; cualquier
    otra excepción del run se propaga (el caller decide el status).

    `steer` y `rescue` son canales MUTABLES del caller (2026-07-25), no
    configuración. El caller los crea vacíos y los comparte con el
    endpoint HTTP:
      - `steer`: cola de correcciones del humano. Si aparece algo, el run
        corta en el próximo borde de nodo, rescata el historial y re-entra
        con ese texto como prompt (mismo camino que el auto-continue de
        presupuesto). Sirve para redirigir sin perder el trabajo hecho.
      - `rescue`: por dónde sale el historial cuando el run NO puede
        devolver un dict — el cancel del usuario re-lanza CancelledError.
        Se escribe sin `await` a propósito: en ese punto la task ya está
        cancelada y un await ahí es territorio minado.

    `db` es opcional. Si está, lo usa para leer `system_config` y
    resolver el timeout en cascada:
        1. defaults_json.timeout del project (per-project override)
        2. system_config.expert_timeout_s (global, editable desde UI)
        3. FOURBIS_EXPERT_TIMEOUT env var
        4. default 600s

    Tools disponibles para el agente:
      - Nativas del relay, siempre: `shell`, los archivos
        (read/write/edit/move/list/search), `ask_human` y el SQL
        (`db_connections`/`db_query`). Desde el 2026-08-16 no dependen
        de ningún MCP — el wrapper se retiró (docs/WRAPPER.md).
      - MCP toolsets del catálogo, los que el proyecto tenga adjuntos
        (ADR-017 cbm-via-MCP se descartó — ver `cbm_cli_call` abajo).
      - Tool nativa `cbm_query` cuando el binario cbm está instalado
        (ADR-017). Llama al CLI del binario de codebase-memory-mcp.

    `images` (2026-07-31) son [(bytes, media_type)] que viajan como
    partes binarias del prompt del usuario. El `user` sigue siendo str
    en todo el resto del camino (jsonl, .md, historial): las imágenes
    se agregan SOLO al armar el prompt del agente. Ver
    attachments.load_images.

    `on_leg_boundary` (2026-08-17) es el supervisor de media corrida.
    Se invoca en cada corte de tanda del auto-continue con un dict
    parcial del run (`content`, `phase_at_end`, `messages_json`,
    `tool_calls`, `leg`) y debe devolver:
      - `""` / None  → el rumbo está bien, seguí;
      - un string    → el feedback de por qué se desvió; corta el run.

    Lo provee `run_expert_staged`, que es quien tiene el plan contra el
    cual comparar; `run_expert` a secas no sabe de verificadores. Dos
    consecuencias de tenerlo:
      1. un desvío se detecta en el primer corte de tanda en vez de al
         final (en el run de sample-shop: paso 64 en vez de 172);
      2. `max_legs` deja de ser el techo y pasa a ser el punto donde se
         empieza a pedir permiso — mientras el supervisor no objete, la
         tarea se TERMINA en vez de quedar a medias. El techo duro pasa
         a ser `max_legs_hard` y el anillo real sigue siendo el
         deadline global.
    Si el supervisor levanta una excepción, se ignora y el run sigue
    como si no existiera: un verificador caído no puede matar trabajo.
    """
    # t0 al PRINCIPIO (no después de armar toolsets) para que duration_ms
    # refleje el trabajo total, no solo agent.run(). Bug fix 2026-07-17.
    # monotonic (segundos, float). Bug fix 2026-07-18: NO perf_counter_ns
    # porque mezclaba nanosegundos con segundos en `deadline = t0 + timeout`
    # → deadline ≈ 1e18, `wait_for(timeout=...)` quedaba en ~10⁶ años,
    # y `duration_ms = int((monotonic - ns) * 1000)` salía negativo.
    # `monotonic` da resolución de ~0.1µs en Win/Linux, suficiente.
    t0 = time.monotonic()
    defaults = project.get("defaults_json") or {}
    from .execution_policy import ExecutionPolicy
    policy = ExecutionPolicy.for_run(defaults, archivos_reservados)
    image_artifacts = image_artifacts if image_artifacts is not None else {}
    if rescue is not None:
        rescue["image_artifacts"] = image_artifacts
    spec = resolve_model_spec(model_override, project)
    model = build_model(spec)
    # Cascada de timeout (segundos):
    #   1. defaults_json.timeout del project
    #   2. system_config.expert_timeout_s (global, editable desde UI)
    #   3. FOURBIS_EXPERT_TIMEOUT env var
    #   4. default 600s (10 min)
    _global_to = None
    if db is not None:
        try:
            _global_to = await db.get_config("expert_timeout_s")
        except Exception:
            _global_to = None
    timeout = float(
        defaults.get("timeout")
        or (_global_to if _global_to else None)
        or config.expert_timeout_s()
    )
    tool_timeout = config.tool_timeout_s()
    # Idle watchdog (per-project override). Si no está seteado, usa el
    # default de config (180s). Caso real: tareas de auditoría sobre
    # docs grandes donde un tool cbm tarda >60s legítimamente — sin
    # override, el watchdog mata al experto a los 180s aunque esté
    # haciendo trabajo útil.
    _idle_to = defaults.get("idle_timeout_s") or config.expert_idle_timeout_s()
    _think_to = _think_cap(defaults, _idle_to)
    # Corte por tool-call MCP. Un `run_shell` colgado (server en
    # foreground, prompt interactivo) falla SOLO con un ModelRetry
    # accionable —el run sigue— en vez de que el watchdog mate el run
    # entero.
    #
    # 2026-08-16: era `_idle_to * 0.9` (162s con los defaults), o sea el
    # techo de un comando estaba atado al watchdog de idle. Eso mataba
    # cualquier build o suite de tests legítimamente larga y el humano lo
    # leía como "el experto se equivoca al llamar tools". Ahora tiene
    # límite propio (`tool_call_timeout_s`, 300s default) y el watchdog
    # no cuenta el tiempo de una tool en vuelo, así que los dos números
    # dejaron de pelearse. Per-project: defaults_json.tool_call_timeout_s.
    _mcp_tool_to = float(
        defaults.get("tool_call_timeout_s") or config.tool_call_timeout_s())

    # F1 (plan MCP_REGISTRY): toolsets desde el catálogo si hay db y el
    # project vino de la tabla (tiene id). spec "test" NO adjunta MCPs:
    # TestModel no llama tools (call_tools=[]) y el spawn del wrapper
    # solo sumaría latencia/flakiness a los tests.
    # Bug fix 2026-07-18: borrado el fallback al blob projects.mcp_servers.
    # Todos los callers reales (chat en server.py, night._expert_work,
    # voice process) pasan db; los tests usan spec="test" y no necesitan
    # tools; un project sintético sin id (voice sin proyecto relacionado)
    # tampoco necesita tools. Una rama, dos checks, sin path legacy.
    selection: set[str] = {
        s.strip().lower() for s in (mcp_with or []) if s.strip()}
    use_catalog = (
        db is not None and project.get("id") is not None and spec != "test")
    visible_mcps: list[dict] = []
    attached_mcps: list[dict] = []
    # Tool en vuelo (2026-08-16): lo escribe CappedToolset y lo lee el
    # watchdog de idle. Vive acá —fuera del `if use_catalog`— para que el
    # watchdog siempre tenga algo que leer, aunque el run no adjunte MCPs.
    _inflight: dict = {}
    # Iter 9.8 — notes workspace: si el proyecto es `notes`, NO
    # adjuntamos MCPs del proyecto (no tiene `mcp_servers` por seed),
    # NO exponemos cbm_query, NO injectamos use_capability ni el
    # menú de capacidades. Todo eso es ruido para un asistente de
    # notas. Tampoco armamos rama git (no hay working tree de
    # conversación). Ponytail: 1 flag, 3 salvadas.
    _is_notes = (project.get("slug") or "").lower() == "notes"
    if _is_notes or spec == "test" or not policy.unrestricted_tools:
        # Sin catálogo, sin tools. Notas y tests nunca llaman tools.
        toolsets = []
        attached_mcps = []
        visible_mcps = []
    elif use_catalog:
        # Una sola fuente de verdad: lo que el relay implementa nativo NO
        # se le ofrece además desde un MCP. Dos tools con el mismo trabajo
        # y distintos caps es cómo se llega a que gane el más chico sin
        # que nadie lo note (docs/WRAPPER.md).
        _hide: set[str] = set()
        if defaults.get("native_shell", True):
            _hide.add("run_shell")
        if defaults.get("native_files", True):
            _hide |= {"read_file", "write_file", "edit_file", "move_file",
                      "list_dir", "directory_tree"}
        _hide = frozenset(_hide)
        toolsets, attached_mcps, visible_mcps = await _catalog_toolsets(
            db, project, selection, mcp_pool, _mcp_tool_to, _inflight, _hide,
            image_artifacts, has_vision(spec))
    else:
        # Sin db o sin project.id (voice sintético, etc.): corremos el
        # experto sin tools. No leemos el blob legacy projects.mcp_servers
        # — si un caller nuevo llega sin db, el bug es suyo, no nuestro.
        toolsets = []
        attached_mcps = []
        visible_mcps = []

    # Ahorro de tokens: TODO toolset pasa por el cap + elisión (ver
    # sección "ahorro de tokens" arriba). Los tools nativos (cbm_query)
    # se capean en su propio return. El wrap lo hace _catalog_toolsets,
    # que es la única fuente de esta lista.

    ponytail = await read_ponytail()
    instructions = build_instructions(project, ponytail, skills_block)
    if not policy.unrestricted_tools:
        instructions += (
            "\n\nEste run tiene permisos restringidos. Shell y MCP externos no están "
            "disponibles: usa las herramientas nativas de archivos y SQL. "
            "No intentes eludir restricciones cambiando de herramienta.")
    if _is_notes:
        # Notas NO quieren el bloque de git diff, skills-ponderado, ni
        # las menciones de skills de proyectos reales. Solo el system
        # prompt del proyecto (el que se metió en seed_notes_project).
        # Reiniciamos: instructions = system_prompt del proyecto + ponytail.
        pony = ponytail or ""
        sysp = project.get("system_prompt") or ""
        instructions = "\n\n".join(p for p in (pony, sysp) if p)
    # Facts always-on (2026-08-20). Apagado por default: prenderlo con
    # `defaults_json.facts_always_on=true` en el proyecto donde valga la
    # pena. ADR-027 había dejado el retrieval manual a propósito ("el
    # manual es más predecible"), pero la vía manual nunca se usó —cero
    # invocaciones de `/fact` en `command_logs`— así que los hechos se
    # destilaban para nadie. Esto es el upgrade path que la propia ADR
    # anotó: "facts always-on por proyecto si resultan baratos".
    #
    # Va ANTES de `system_extra` para que lo del caller (plan, memoria
    # pedida a mano, issue de GitHub) quede después y gane en la lectura.
    #
    # Costo medido por request, con el tope de `build_facts_block`:
    # sample-app ~2000 tokens (recortado de 5.5k), sample-shop ~930. Se
    # re-manda en cada turno, así que no es gratis; por eso el flag.
    # No rompe la cache del prefijo salvo al cerrar un hilo, que es
    # cuando cambian los hechos (a diferencia del git diff, que cambiaba
    # con cada archivo escrito — ver `build_instructions`).
    if db is not None and not _is_notes and defaults.get("facts_always_on"):
        try:
            # Import adentro: `memory` importa de `experts` (build_model,
            # structured_output_settings), así que arriba sería circular.
            from . import memory as memory_mod
            # `approved` explícito: un hecho recién destilado está en
            # `pending` y no puede entrar al prompt hasta que un humano lo
            # mire (2026-08-21). Sin este filtro la aprobación no existe.
            _facts = await db.list_facts(
                project["slug"], limit=200, status="approved")
            _fb = memory_mod.build_facts_block(_facts)
            if _fb:
                instructions = f"{instructions}\n\n{_fb}"
        except Exception as e:  # noqa: BLE001 — la memoria nunca mata un run
            logger.warning("facts always-on: no pude leerlos (%r)", e)

    if system_extra:
        instructions = f"{instructions}\n\n{system_extra}" if instructions else system_extra

    # Bloque MCP on-demand (iter 9.6): el LLM no sabe que existe la meta-tool
    # `use_capability` salvo que se lo digamos. Lo enumeramos solo si hay
    # capacidades pendientes (no-attached, on-demand) — si todas están
    # adjuntas o no hay catálogo, no contaminamos el prompt.
    _attached_terms = set()
    for m in attached_mcps:
        _attached_terms.add(m["name"].lower())
        _attached_terms.add(m["capability"].lower())
    _pending = [
        m for m in visible_mcps
        if m["on_demand"] and m["name"].lower() not in _attached_terms
    ]
    if _pending:
        _menu = ", ".join(sorted(
            f"{m['name']} ({m['capability']})" for m in _pending))
        instructions = (
            f"{instructions}\n\n"
            f"## Capacidades externas disponibles (on-demand)\n"
            f"Si el pedido del usuario requiere una capacidad que no esta "
            f"cubierta por tus tools (browser, DB, github, etc.), invoca "
            f"`use_capability(name)` y el run se reinicia con esa toolset "
            f"adjunta. Disponibles ahora: {_menu}.")
    elif use_catalog and visible_mcps and all(
            m["name"].lower() in _attached_terms for m in visible_mcps):
        # Todas las visibles ya están adjuntas: el bloque sería ruido. Pero
        # igual le decimos al LLM qué hay, por si quiere razonar sobre qué
        # ya tiene sin necesidad de pedirlo.
        _menu = ", ".join(sorted(
            f"{m['name']} ({m['capability']})" for m in visible_mcps))
        instructions = (
            f"{instructions}\n\n"
            f"## Capacidades externas (adjuntas en este run)\n"
            f"Ya tienes estas capabilities activas: {_menu}.")

    # Política de evidencia (iter 2026-07-22). Solo si el browser está
    # adjunto: sin browser el bloque no aplica y sería pagar tokens por
    # instrucciones que el experto no puede ejecutar.
    #
    # 2026-08-16: la condición era `"obscura" in _attached_terms`, o sea
    # el NOMBRE de un MCP puntual. Ahora va por CAPACIDAD: cualquier fila
    # del catálogo con `capability=browser` enciende el bloque. Con
    # obscura retirado, atarlo a un nombre habría apagado la política de
    # evidencia sin que nadie lo notara.
    if "browser" in _attached_terms:
        instructions = f"{instructions}\n\n{EVIDENCE_BLOCK}"

    # Tools nativas: cbm_query si el binario está instalado. ADR-017.
    # Una sola tool Python que envuelve el CLI del binario. NO se mete
    # cbm vía MCP stdio: verificado 2026-07-19, el binario (0.8.1)
    # procesa stdin recién al EOF — no responde con el pipe abierto,
    # así que una sesión MCP persistente es imposible hasta que lo
    # arreglen upstream (por eso el handshake "se colgaba").
    tools: list[Any] = []

    # ---- bitácora del run (2026-08-17) ----
    # Lo que el experto verifica y no quiere olvidar, más el log de
    # comandos que escribe el harness. Se engancha por `instructions=` en
    # `_build_agent`: ahí está el porqué sobrevive a la elisión, que es
    # todo el punto. Ver `BITACORA_BLOCK`.
    # Se carga la del turno anterior si la hay (2026-08-26): sin esto el
    # experto llega a un **continuá** sabiendo lo que dice el historial
    # recortado y nada de lo que él mismo verificó.
    bitacora = Bitacora.cargar(bitacora_json) if bitacora_json else Bitacora()
    if bitacora.hechos or bitacora.comandos:
        logger.info("bitácora retomada: %d hechos, %d comandos",
                    len(bitacora.hechos), len(bitacora.comandos))

    # ---- shell propio del relay (2026-08-16) ----
    # El `run_shell` del wrapper MCP (`4bis.vscode`, retirado del catálogo
    # 2026-08-16 — docs/WRAPPER.md) se colgaba con PowerShell y con
    # cualquier comando que espere stdin. Por eso el relay trae el suyo:
    # mismo trabajo, sin los cuelgues (ver relay/shell.py). Opt-out por
    # proyecto con `defaults_json.native_shell=false`: si alguien
    # re-adjunta el wrapper a mano, vuelve a aparecer su `run_shell` en la
    # lista de tools visibles.
    _native_shell = (defaults.get("native_shell", True) and not _is_notes
                     and policy.unrestricted_tools)
    if _native_shell:
        tools += shell_tools_mod.shell_tools(
            repo=project.get("repo_path") or "",
            techo_s=float(_mcp_tool_to), bitacora=bitacora,
            en_vuelo=lambda n: _tool_en_vuelo(_inflight, n))

    _native_files = defaults.get("native_files", True) and not _is_notes
    _perm = None
    if project.get("repo_path") and not _is_notes:
        _perm, _abierto, _por_que = file_tools_mod.permisos_del_run(
            project, defaults, conversation_id=conversation_id,
            reservadas=archivos_reservados or ())
        if _abierto:
            # WARNING y no INFO: es el estado excepcional, y cuando algo
            # aparezca escrito donde nadie lo esperaba, esta línea es la
            # que explica por qué se pudo.
            logger.warning(
                "sandbox de archivos APAGADO para %s (%s): las tools de "
                "archivo llegan a todo el disco. read_only y rutas_vedadas "
                "siguen aplicando.", project.get("slug") or "?", _por_que)
        if _native_files:
            tools += file_tools_mod.file_tools(_perm)

    if db is not None and defaults.get("sql_tools", True) and not _is_notes:
        from .sql_tools import sql_tools
        tools.extend(sql_tools(db, policy, perm=_perm))

    # ---- preguntarle al humano (2026-08-16) ----
    # El experto tiene libertad para ejecutar, pero NO para instalar ni
    # para tomar decisiones que el humano no delegó. `ask_human` es la
    # válvula: deja la pregunta anotada y TERMINA el turno.
    #
    # Por qué termina el turno y no bloquea esperando: el humano puede
    # tardar horas (Discord, otra máquina, mañana). Bloquear dentro de la
    # tool-call significaría un run vivo consumiendo su presupuesto
    # contra un techo de minutos. El turno cierra, la pregunta queda en
    # `expert_questions`, y la respuesta entra como el turno siguiente de
    # la conversación — que es como el hilo ya sabe pasarse contexto.
    # El id de la pregunta abierta de este turno, si el experto pregunta.
    # Vive acá y no dentro del `if` porque lo lee el result de más abajo,
    # que se arma igual cuando `ask_human` no está registrada.
    _q_state: dict = {}
    if db is not None and chat_id:

        async def ask_human(pregunta: str, evidencia: str, opciones: str = "",
                            detalle: str = "") -> str:
            """Pregunta algo al humano y TERMINA tu turno.

            Usala cuando de verdad no podés seguir sin una decisión suya:
            hay que INSTALAR algo, hay que elegir entre caminos que no son
            equivalentes, o el pedido es ambiguo de una forma que cambia
            el resultado. No la uses para pedir permiso de rutina: tenés
            libertad para leer, escribir y ejecutar.

            Después de llamarla, cerrá con un resumen de lo que hiciste y
            de qué estás esperando. NO sigas trabajando ni inventes la
            respuesta: el turno termina acá y la respuesta del humano
            llega como el mensaje siguiente.

            Args:
                pregunta: la pregunta, en una línea y concreta.
                evidencia: OBLIGATORIO. Qué archivos leíste concretamente
                    y qué encontraste en ellos — no un resumen de tu
                    conclusión. Mal: "el inventario está desactualizado".
                    Bien: "leí Service/Notifications/NotificableAttribute.cs:
                    tiene el namespace anidado; el inventario dice textual
                    'asumo, no leí el resto'". Sin esto, quien responda
                    tiene que reabrir el repo para verificar tu pregunta a
                    mano — y eso es lo que hizo que 11 preguntas quedaran
                    sin contestar semanas enteras.
                opciones: alternativas separadas por `|`. Ej:
                    "instalalo vos|lo instalo yo|seguí sin eso".
                    Vacío = respuesta libre.
                detalle: contexto que el humano necesita para decidir
                    (qué falta, para qué, qué pasa si dice que no).
            """
            motivo = _evidencia_insuficiente(evidencia)
            if motivo:
                raise ModelRetry(
                    f"La `evidencia` {motivo}. Volvé a llamar `ask_human` "
                    "con los archivos concretos que leíste y lo que "
                    "encontraste en ellos — sin eso, quien responda no "
                    "puede verificar la pregunta sin reabrir el repo.")
            opts = [o.strip() for o in (opciones or "").split("|") if o.strip()]
            q = {
                "title": (pregunta or "").strip()[:400],
                "detail": (detalle or "").strip()[:2000],
                "evidencia": evidencia.strip()[:2000],
                "options": [{"key": f"o{i}", "label": o}
                            for i, o in enumerate(opts[:6])],
            }
            q_id = f"q_{uuid.uuid4().hex[:8]}"
            try:
                await db.create_expert_question(
                    q_id, chat_id, json.dumps(q, ensure_ascii=False),
                    conversation_id=conversation_id or None,
                    project_slug=project.get("slug"),
                    kind="install" if _huele_a_instalacion(pregunta, detalle)
                    else ("choice" if opts else "text"))
            except Exception as e:  # noqa: BLE001 — preguntar no rompe el run
                logger.warning("no pude registrar la pregunta (%r)", e)
                return ("No pude registrar la pregunta. Explicá en tu "
                        "respuesta final qué necesitás del humano.")
            _q_state["asked"] = q_id
            logger.info("pregunta al humano %s (chat=%s): %s",
                        q_id, chat_id[:8], q["title"][:80])
            try:
                await _emit_progress(phase="question", tool="ask_human",
                                     message=q["title"][:200])
            except Exception:  # noqa: BLE001
                pass
            return (
                f"Pregunta registrada ({q_id}). El humano la va a ver en el "
                "chat. TERMINÁ TU TURNO AHORA: escribí un resumen corto de "
                "lo que hiciste y de qué estás esperando. No sigas "
                "trabajando ni asumas una respuesta.")

        tools.append(Tool(ask_human, takes_ctx=False))

    # Iter 9.8: cbm_query no se adjunta en el workspace de notas.
    if cbm_binary_path() is not None and not _is_notes:
        # El experto está scopeado a UN proyecto: derivamos el nombre de cbm
        # del repo_path y lo inyectamos en cada query. El LLM NO puede adivinar
        # este nombre (es el path mangleado, ej "C-Users-developer-source-repos-
        # AuroraDemo-SampleApp") — antes lo omitía y cbm respondía "project not found
        # or not indexed", forzando al experto a explorar a mano con read_file/
        # list_dir (19 tool calls en vez de 1). night.py ya usaba _cbm_project_name.
        from .admin import _cbm_project_name  # lazy: evita ciclo de imports
        cbm_project = _cbm_project_name(project["repo_path"])

        async def cbm_query(tool: str, args_json: str = "{}") -> str:
            """Consulta el grafo de código del proyecto actual (codebase-memory).

            USALA ANTES de explorar a mano con read_file/list_dir: una
            búsqueda bien elegida reemplaza decenas de lecturas. Cada
            llamada tiene ~1s de costo fijo — prefiere pocas llamadas
            precisas sobre muchas exploratorias.

            NO pases `project` en args_json: se inyecta solo.

            Tools disponibles (nombre en `tool`, args en `args_json`):

            - search_graph — buscar funciones/clases/rutas/variables.
              Modos combinables: query="update settings" (full-text BM25),
              name_pattern=".*regex.*", semantic_query=["send","publish"]
              (ARRAY de keywords, búsqueda vectorial). Filtros: label,
              file_pattern, min_degree. Paginación: limit (default 40) +
              offset; el response trae total y has_more.
            - trace_path — callers/callees/impacto de una función. args:
              function_name*, direction (inbound|outbound|both), depth
              (default 3), mode (calls|data_flow|cross_service).
            - get_code_snippet — leer el código de un símbolo. args:
              qualified_name* (obtenelo con search_graph primero).
            - search_code — grep enriquecido con el grafo (dedup por
              función, ranking estructural). args: pattern*, file_pattern
              (glob), path_filter (regex), mode (compact|full|files),
              regex (bool), limit (default 10).
            - get_architecture — vista de alto nivel: paquetes, servicios,
              dependencias, clusters (módulos de facto). args: path (scope
              a un subdirectorio), aspects (["overview"] = compacto).
            - query_graph — Cypher para patrones multi-hop y métricas de
              complejidad por función (complexity, transitive_loop_depth,
              linear_scan_in_loop, alloc_in_loop...). args: query*.
            - detect_changes — cambios vs base_branch (default main) y su
              impacto. args: scope, depth, since (ej "HEAD~5").
            - get_graph_schema — labels y edge types del grafo.
            - index_status / list_projects — estado del índice.
            - manage_adr — leer/actualizar ADRs (mode get|update|sections).

            Args:
                tool: nombre del tool cbm (ver lista de arriba).
                args_json: JSON string con los args (SIN `project`).
                    Vacío = {}.
            """
            try:
                args = json.loads(args_json) if args_json else {}
            except json.JSONDecodeError as e:
                return json.dumps({"error": f"args_json inválido: {e}"})
            if not isinstance(args, dict):
                args = {}
            # Forzamos el project correcto (el LLM lo omite o lo adivina mal).
            if tool != "list_projects":
                args["project"] = cbm_project
            # Cap de tokens: search_graph sin limit devuelve hasta 200
            # nodos — un default más chico obliga a paginar consciente.
            if tool == "search_graph":
                args.setdefault("limit", 40)
            raw = await cbm_call(tool, args, timeout=30.0)
            capped = _cap_text(raw)
            # Bug fix 2026-07-20: si cbm devolvió error, inyectar un
            # `fallback_hint` explícito para que el LLM sepa qué hacer.
            # Sin esto, el modelo recibía `{"error": "..."}` y volvía
            # a llamar cbm_query con los mismos args (loop que terminaba
            # en `Tool exceeded max retries count of 3`). Con el hint
            # en el response, el LLM lee la sugerencia en el mismo turno
            # y cae a list_dir/read_file directo.
            # Ponytail: el hint NO se inyecta cuando cbm anduvo bien.
            try:
                parsed = json.loads(capped)
                if isinstance(parsed, dict) and parsed.get("error"):
                    parsed["fallback_hint"] = (
                        "cbm no respondió con esta query. NO la repitas. "
                        "Caé a `list_dir(path)` para navegar el repo a "
                        "mano, y `read_file(path)` para leer el archivo "
                        "puntual que necesitabas. La próxima tool call "
                        "debería ser filesystem, no otra cbm_query."
                    )
                    return json.dumps(parsed, ensure_ascii=False)
            except (json.JSONDecodeError, ValueError):
                # cbm devolvió JSON inválido o string suelto — no
                # podemos enriquecer, devolvemos lo que hay.
                pass
            return capped

        tools.append(Tool(cbm_query, takes_ctx=False))

    # Iter 10.4: tool nativa `read_skill(name)` para modo compact.
    # Reusa `read_skill_sync` (síncrona, hace FS read) envuelta en
    # asyncio.to_thread para no bloquear el event loop. Cap de 20KB
    # en el output: una SKILL.md razonable entra; un script de 200KB
    # no. Devuelve "" + hint si la skill no existe, así el LLM cae a
    # otra acción en vez de loop.
    # Ponytail: si la skill no está en el índice, NO la inventamos.
    # El LLM tiene que saber que pidió algo que no existe.
    from . import skills as _skills_mod  # lazy: evita ciclo de imports
    # El repo primero: una skill versionada junto al codigo que describe
    # le gana a la global del mismo nombre (ver `skills.skills_dirs`).
    _skills_dirs = _skills_mod.skills_dirs(project.get("repo_path") or "")

    async def read_skill(name: str) -> str:
        """Lee el cuerpo de una skill por nombre (ej: "ponytail", "4bis-shortcuts").

        ÚSALA cuando el system prompt te diga que una skill matchea
        tu tarea. La descripción que viste en el bloque de skills es
        un resumen de 1 línea: el cuerpo tiene las reglas, los
        comandos concretos, y los casos de uso. Inventar el cuerpo
        te sale caro: bug.

        Las skills de la línea "On-demand" del system prompt viajan
        SOLO con el nombre: si el nombre suena a tu tarea, esta tool
        es la única forma de saber qué dicen. No las descartes por no
        tener descripción.

        Args:
            name: nombre de la skill (el del frontmatter, ej
                "ponytail"). Case-insensitive en la búsqueda.
        """
        # to_thread: read_skill_sync hace open() + read(), bloqueante.
        # Para una skill chica no importa, pero la API es pública y
        # no queremos que un LLM lento + skill grande nos frene el
        # loop del relay.
        try:
            content = await asyncio.to_thread(
                _skills_mod.read_skill_multi, _skills_dirs, name,
            )
        except Exception as e:  # noqa: BLE001 — best-effort
            return f"[read_skill] error leyendo {name!r}: {e!r}"
        if content is None:
            # _build_index lee el dir (un glob) y es suficientemente
            # rápido para un tool call ocasional; no comparte cache con
            # el SkillCache app-level.
            # auto + manual: listar solo las auto le decía al modelo que
            # las on-demand no existen —justo las que esta tool está
            # para leer— y lo mandaba a abandonar una skill instalada.
            index = _skills_mod.build_index_multi(_skills_dirs)
            available = ", ".join(
                s.name for s in index.auto_skills() + index.manual_skills()
            ) or "(ninguna)"
            await _emit_progress(phase="tool_call", tool="read_skill",
                                 skill=name, encontrada=False)
            return (
                f"[read_skill] skill {name!r} no encontrada. "
                f"Disponibles: {available}. "
                "Si ninguna matchea, no la invoques de nuevo."
            )
        # Cap defensivo: una SKILL.md puede ser un libro si alguien
        # la importó mal. 20KB ≈ 5K tokens, alcanza para cualquier
        # skill razonable y previene que un bad actor infle el
        # context del LLM.
        if len(content) > 20_000:
            content = content[:20_000] + (
                f"\n\n[read_skill] truncado a 20KB; total "
                f"{len(content):,} chars."
            )
        # Fase 0 (2026-09-08): registrar CUAL skill se leyo. El evento de
        # tool call ya guardaba `tool="read_skill"` pero no el argumento,
        # asi que de 57 chats que la llamaron no se podia saber que
        # leyeron ni si servia. Sin este dato, medir si las skills
        # mejoran algo es imposible: `progress_events` es lo unico que se
        # persiste por turno.
        await _emit_progress(phase="tool_call", tool="read_skill",
                             skill=name, encontrada=True)
        return content

    tools.append(Tool(read_skill, takes_ctx=False))

    # Cambios sin commitear: antes viajaban SIEMPRE en el system prompt
    # (ADR-020). El bloque cambia en cuanto el experto escribe un
    # archivo, así que el system prompt dejaba de ser estable y el
    # provider tiraba la cache del historial entero en cada resume
    # (medido: 0% de hit contra 94% de los runs que no lo tocaban).
    # Como tool el costo es on-demand y el result entra en la elisión.
    if not _is_notes:
        _repo_path = project["repo_path"]

        async def git_diff() -> str:
            """Cambios sin commitear del repo (git status + git diff HEAD).

            Úsala al empezar si necesitas saber qué hay pendiente en el
            working tree. El diff va capeado a 20KB.
            """
            return await asyncio.to_thread(
                _build_git_diff_block_sync, _repo_path,
            ) or "Working tree clean (o el repo no es git)."

        tools.append(Tool(git_diff, takes_ctx=False))

    def anotar(hecho: str) -> str:
        """Anota un hecho VERIFICADO en la bitácora del run.

        Tu historial se recorta: los resultados de tools viejas se
        reemplazan por un muñón. Lo que anotes aquí sobrevive todo el run
        y lo seguirás viendo al final, cuando escribas la respuesta.

        Anota el resultado, no la intención: "el build pasó en 7.66s",
        no "corrí el build". Los hechos negativos también valen.

        Args:
            hecho: una línea, concreta y comprobada. Ej:
                "npm run lint: 3 errores, 215 warnings (exit=1)".
        """
        return bitacora.anotar(hecho)

    tools.append(Tool(anotar, takes_ctx=False))

    def plan_step_done(paso: int, nota: str = "") -> str:
        """Marca un paso del plan como completado (Etapa B, P1).

        El panel dibuja el plan del run como un grafo, y necesita saber
        en qué paso vas para pintar la columna que está verde. Vos
        sos la única fuente honesta de ese puntero — el verificador lo
        corrobora al final, pero no puede adivinar.

        Args:
            paso: el número del paso (1-based, igual al que dice el
                plan que viste en el system prompt). Si el plan dice
                "1. ... 2. ... 3. ...", pasás 1, 2 o 3.
            nota: una línea corta opcional con qué quedó hecho en
                ese paso. Aparece al hover del nodo.

        Devuelve confirmación corta. Acepta cualquier entero (un número
        mal puesto no puede cortar el run): el orquestador filtra los
        que caen fuera del rango del plan antes de armar el dict que
        sale a la UI.
        """
        bitacora.marcar_paso(paso, nota)
        return f"paso {paso} marcado" + (f" ({nota})" if nota else "")

    tools.append(Tool(plan_step_done, takes_ctx=False))

    def _build_agent(extra_tools: list[Any]) -> Agent:
        all_tools = tools + extra_tools
        # Ahorro de tokens 2026-07-28: las tools NATIVAS (cbm_query,
        # read_skill, use_capability) también tienen que pasar por las
        # capas 1 y 2. Pasadas por `tools=`, pydantic-ai las mete en su
        # FunctionToolset interno, que no podemos envolver: sus results
        # no se capeaban ni se elidían, así que un run que solo usa
        # tools nativas re-pagaba cada result en TODOS los turnos
        # siguientes (el N² que la capa 2 existe para cortar).
        # Registrándolas como toolset propio ya envuelto, un solo
        # camino de cap/elisión cubre nativas + MCP.
        # max_retries replica lo que Agent le pone al FunctionToolset
        # interno (retries=3).
        #
        # Bug fix 2026-08-19: el timeout era `tool_timeout`
        # (FOURBIS_MCP_TIMEOUT, 60s), que es el techo del HANDSHAKE de un
        # MCP, no el de un comando. O sea: `shell` documentaba un
        # `timeout_s` de hasta `tool_call_timeout_s` (300s), el watchdog
        # de idle daba por buena una tool en vuelo hasta ese mismo número
        # (`_watchdog_verdict(tool_timeout=_mcp_tool_to)`), y pydantic-ai
        # la mataba igual a los 60 con un `ModelRetry("Timed out after
        # 60.0 seconds.")` que no dice de dónde salió el número. Caso
        # real (sample-shop, 19/8): 16 cortes de 60s en un run de 1h, el
        # modelo escribió 10 variantes del mismo script de captura para
        # esquivarlo y no entregó nada. El `+ grace` deja que el techo
        # propio de la tool dispare PRIMERO: el de `shell` mata el árbol
        # de procesos y devuelve un mensaje accionable; este es la red.
        native = [
            CappedToolset(wrapped=FunctionToolset(
                all_tools, max_retries=3,
                timeout=_mcp_tool_to + _TOOL_OVERRUN_GRACE_S),
                image_artifacts=image_artifacts, vision=has_vision(spec))
        ] if all_tools else []
        # Bug fix 2026-07-20: retries=3 (default 1) le da al LLM 2 chances
        # más de corregir un tool call que falló por path malformado /
        # archivo inexistente antes de que pydantic-ai aborte con
        # `Tool 'X' exceeded max retries count of 1`. Caso real: el
        # experto en modo auditoría llama read_file con un path que la
        # wrapper MCP del proyecto rechaza, el LLM repite el mismo path
        # en el retry, y abortamos la tarea sin contexto útil.
        # 3 es el sweet spot: con 1 falla, con 5+ es loop.
        # ponytail: si MiniMax M3 cambia, revisar este número.
        return Agent(
            model,
            # El string estable primero (es el prefijo que cachean los
            # providers) y la bitácora al final, que es lo único que
            # cambia entre requests. Al revés invalidaría la cache de
            # todo el prompt en cada anotación.
            instructions=[instructions, bitacora.render],
            toolsets=native + toolsets,
            tool_timeout=tool_timeout,
            retries=3,
        )

    def _make_use_capability() -> Optional[Tool]:
        """Meta-tool F1: el LLM pide una capacidad on-demand y el run se
        re-corre con ese toolset adjunto (plan §4.1). Solo existe si hay
        MCPs on-demand visibles que aún no están adjuntos."""
        attached_terms = set()
        for m in attached_mcps:
            attached_terms.add(m["name"].lower())
            attached_terms.add(m["capability"].lower())
        pending = [
            m for m in visible_mcps
            if m["on_demand"] and m["name"].lower() not in attached_terms]
        if not pending:
            return None
        by_term = set()
        for m in pending:
            by_term.add(m["name"].lower())
            by_term.add(m["capability"].lower())
        menu = ", ".join(sorted(
            f"{m['name']} (capability: {m['capability']})" for m in pending))

        async def use_capability(name: str) -> str:
            term = (name or "").strip().lower()
            if term in attached_terms:
                return f"{name!r} ya está activa en este run."
            if term in by_term:
                raise _CapabilityRequested(term)
            return (f"No existe la capacidad {name!r}. "
                    f"Disponibles: {menu}")

        use_capability.__doc__ = (
            "Activa una capacidad externa (MCP on-demand) para esta "
            "conversación. El run se reinicia con las tools de esa "
            f"capacidad disponibles. Disponibles: {menu}.\n\n"
            "Args:\n"
            "    name: nombre del MCP o de la capability (ej. 'db').")
        return Tool(use_capability, takes_ctx=False)

    # ADR-025: replay del historial de la conversación (si viene).
    # Best-effort: historial corrupto → run fresco + warning (no romper).
    message_history = None
    if message_history_json:
        try:
            message_history = ModelMessagesTypeAdapter.validate_json(
                message_history_json)
            # Ahorro de tokens (capa 3): los turnos viejos del hilo
            # viajan sin tool spam; solo el último turno va completo.
            n_before = len(message_history)
            message_history = _slim_history(list(message_history))
            if len(message_history) != n_before:
                logger.info(
                    "historial adelgazado: %d → %d mensajes",
                    n_before, len(message_history))
            # Convivencia entre modelos (2026-08-15): si el hilo lo venía
            # escribiendo otro modelo, su razonamiento no se le manda a
            # este. Va DESPUÉS de _slim_history porque esa capa ya se
            # llevó el thinking de los turnos viejos: acá queda el del
            # último turno, que es justo el que sobrevive intacto.
            _strip_foreign_thinking(message_history, spec)
            # Historial ENVENENADO (2026-08-27): si el turno anterior
            # murió con una tool en vuelo y nadie cerró el par, este
            # historial tiene un tool call sin respuesta — y pydantic-ai
            # rechaza el turno entero con "Cannot provide a new user
            # prompt when the message history contains unprocessed tool
            # calls". No es un run que sale mal: es la conversación que
            # deja de aceptar mensajes PARA SIEMPRE, y la única salida
            # era cerrarla y abrir otra. Medido en la DB: 20 runs
            # muertos así en 9 conversaciones; tres quedaron con
            # 1 solo user prompt y 63 tool calls.
            #
            # El cierre ya existía en las rutas de rescate (cancel,
            # steer, watchdog), pero solo tapaba las que ese run
            # conocía. Acá es el guard compartido: cualquier historial
            # que ENTRA se cierra, no importa quién lo dejó abierto.
            # Va DESPUÉS de _slim_history a propósito — esa capa borra
            # los tool calls viejos, así que cerrar antes dejaría el
            # ToolReturnPart sintético colgando sin su call.
            _close_orphan_tool_calls(
                message_history,
                reason="el turno anterior se cortó con esta tool en vuelo")
        except Exception as e:  # noqa: BLE001 — pydantic ValidationError y afines
            logger.warning(
                "message_history corrupto (%r): corro sin historial", e)


    # Sprint 1 — Progress events accumulator. Each phase/tool call
    # adds an entry; the caller (server.py) persists to chats.progress_events.
    progress_events: list[dict[str, Any]] = []
    # Progress callback (Fase 1 — liveness A + notify progreso B).
    # Si no viene on_progress (caso test, o callers viejos), es noop.
    # El callback NO debe lanzar — si tira, se loggea y se sigue.
    progress_sink = on_progress

    async def _emit_progress(**fields) -> None:
        event = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **{k: v for k, v in fields.items() if v is not None},
        }
        progress_events.append(event)
        if progress_sink is None:
            return
        try:
            await progress_sink(**fields)
        except Exception as e:  # noqa: BLE001 — el callback es best-effort
            logger.warning("on_progress callback falló: %r", e)

    await _emit_progress(phase="thinking", tool=None)

    # NOTA: NO reasignar t0 acá — el de arriba (top-of-function) es el
    # correcto para duration_ms. Bug fix 2026-07-17: el segundo t0
    # quedaba después de los setup (model build, toolsets, history
    # replay) y duration_ms reportaba 0 cuando TestModel respondía en <1ms.
    output_text = ""
    usage = None
    tool_calls_count = 0
    # Presupuesto de salida de consola adjuntada a los pasos (ver
    # `_STEP_OUT_BUDGET`): se descuenta por tool_result emitido.
    out_budget = _STEP_OUT_BUDGET
    last_phase = "thinking"
    last_tool_name: str | None = None
    messages_json = ""
    budget_exceeded = False
    # Caída del proveedor rescatada por `_run_iter` (2026-08-26). Quién
    # reintenta y quién corta lo decide el round loop, no el handler.
    provider_error: BaseException | None = None
    provider_retries = 0
    request_limit = config.expert_request_limit()
    # 2026-07-20c: ventana de las últimas tool calls (tool + args) para
    # el gate anti-loop del auto-continue de presupuesto. Persiste entre
    # tandas a propósito: un loop que cruza el corte también cuenta.
    recent_tool_calls: deque[str] = deque(maxlen=8)
    # 2026-07-26 — medidor de peso de tool results. Un result no cuesta su
    # tamaño: cuesta tamaño × las vueltas que le quedan al run, porque se
    # reenvía en cada una. Acumulamos O(1) por tool y el carry sale al
    # final con la identidad:
    #     Σ size×(N−k) = N×Σsize − Σ(size×k)
    # con N = vueltas totales y k = la vuelta en que llegó el result.
    tool_meter: dict[str, dict[str, int]] = {}
    meter_turn = 0
    # Corrección del humano consumida de la cola `steer`, esperando a que
    # el round loop la use como prompt de la próxima tanda.
    steer_text = ""
    steers = 0
    # Las correcciones aplicadas, en orden. El caller las anexa al .md:
    # sin eso el archivo archivado muestra una respuesta que no le
    # corresponde al prompt que figura arriba.
    steer_texts: list[str] = []

    async def _run_iter(agent: Agent) -> None:
        """Cuerpo de iter() — corre FUERA del wait_for para que el
        timeout duro cancele la task entera (no esperamos a un cancel
        cooperativo que pydantic-ai 2.5.1 no garantiza).

        Bug fix 2026-07-20: incluye un watchdog de idle. Si pasan
        >`expert_idle_timeout_s` segundos sin que el agente emita
        NINGÚN node (ni CallToolsNode, ni ModelResponseNode, ni
        ModelRequestNode), el experto está colgado esperando al
        provider (MiniMax M3 a veces deja la conexión abierta sin
        responder). Cancelamos la agent_run y propagamos
        asyncio.TimeoutError con un mensaje accionable — antes esto
        se manifestaba como "tarea muerta a 600s" sin contexto de
        que el problema fue idle, no budget. Cubre el caso del
        usuario donde el experto AUDITABA bien pero un tool call
        largo lo dejaba colgado.
        """
        nonlocal output_text, usage, tool_calls_count, out_budget
        nonlocal last_phase, last_tool_name, messages_json, budget_exceeded
        nonlocal steer_text, meter_turn, provider_error
        nonlocal message_history
        # Per-project override del cap idle (defaults_json.idle_timeout_s).
        # Si no está seteado en el project, usa el global del config.
        idle_timeout = float(_idle_to)
        async with agent.iter(
            _prompt_con_imagenes(user, images),
            message_history=message_history,
            usage_limits=UsageLimits(request_limit=request_limit),
        ) as agent_run:
            # Watchdog: corre en paralelo al async for. Si pasan
            # >idle_timeout sin un solo node nuevo, asumimos provider
            # colgado y cancelamos LA TASK que corre el async for.
            # Bug fix 2026-07-20b: `raise CancelledError` DENTRO del
            # watchdog solo mataba al watchdog mismo — el run seguía
            # vivo hasta el tope global (por eso los chats morían a los
            # 600s exactos como "timeout total" sin mensaje de idle).
            # Además: heartbeat hacia el bot/UI cuando el modelo piensa
            # largo sin emitir nodes (contexto largo ⇒ minutos por
            # respuesta) para que el hilo no parezca muerto.
            last_node_at = time.monotonic()
            idle_cause: list[str] = [None]  # mutable para que el watchdog escriba
            runner_task = asyncio.current_task()

            async def _idle_watchdog() -> None:
                last_beat = time.monotonic()
                while True:
                    await asyncio.sleep(min(15.0, idle_timeout / 4))
                    now = time.monotonic()
                    idle_s = now - last_node_at
                    # Tool en vuelo (2026-08-16): el experto NO está
                    # idle — está esperando un comando que puede tardar
                    # minutos (`dotnet test`, `npm ci`, un build). Antes
                    # esto contaba como idle y el watchdog mataba el run
                    # entero a los 180s, así que el techo real de
                    # cualquier comando era el watchdog. Quien acota la
                    # tool es su propio timeout (`_mcp_tool_to`), no
                    # esto.
                    #
                    # El margen: si la tool lleva MÁS que su propio
                    # techo + 30s, es que ese corte falló (un
                    # subprocess que no muere, un transport trabado) y
                    # el watchdog vuelve a ser la red de seguridad.
                    running_tool, _desde = tool_en_vuelo(_inflight)
                    tool_s = now - (_desde or now)
                    # `thinking`/`writing` = el agente esta esperando al
                    # modelo. Es la unica ventana donde "generando" y
                    # "colgado" son indistinguibles desde el loop, y por
                    # eso es la unica que paga el cap grande.
                    verdict = _watchdog_verdict(
                        idle_s=idle_s, idle_timeout=idle_timeout,
                        tool_s=tool_s if running_tool else None,
                        tool_timeout=_mcp_tool_to,
                        pensando=last_phase in ("thinking", "writing"),
                        think_timeout=_think_to)
                    if verdict == "tool_wait":
                        # Latido, NO una fase nueva: `make_progress_callback`
                        # trata cualquier fase desconocida pisando `rp.phase`
                        # y refrescando `last_activity_at` — o sea, mentiría
                        # la fase real (`tool_call`) y el idle_s de /status.
                        # La rama `heartbeat` ya hace lo correcto: avisa al
                        # bot/UI sin tocar el estado. El nombre de la tool
                        # viaja igual, en `rp.last_tool`.
                        if (now - last_beat) >= 60.0:
                            last_beat = now
                            await _emit_progress(
                                phase="heartbeat", tool=None)
                        continue
                    if verdict == "kill":
                        idle_cause[0] = (
                            f"experto idle por >{idle_timeout:.0f}s "
                            "(probable provider colgado, no budget ni "
                            "work real)"
                            + (f"; la tool `{running_tool}` pasó su propio "
                               f"techo de {_mcp_tool_to:.0f}s sin cortar"
                               if running_tool else ""))
                        logger.warning(
                            "run_expert: %s — cancelando runner",
                            idle_cause[0])
                        runner_task.cancel()
                        return
                    # >45s sin nodes pero aún bajo el cap: el modelo
                    # está pensando (típico con historial largo). Un
                    # latido por minuto hacia la UI/Discord.
                    if verdict == "beat" and (now - last_beat) >= 60.0:
                        last_beat = now
                        await _emit_progress(phase="heartbeat", tool=None)

            watchdog_task = asyncio.create_task(
                _idle_watchdog(), name=f"expert-idle-{id(agent_run)}")
            try:
                async for node in agent_run:
                    last_node_at = time.monotonic()
                    # Steer del humano (2026-07-25): cortamos ACÁ, en el
                    # borde de nodo, no a mitad de una tool. El historial
                    # rescatado + su corrección se re-inyectan en el round
                    # loop, así redirigir cuesta un round-trip y no el run
                    # entero (antes la única salida era cancelar, que
                    # además tiraba el avance).
                    if steer:
                        _nudge = "\n".join(steer).strip()
                        steer.clear()
                        try:
                            _msgs = agent_run.all_messages()
                            _close_orphan_tool_calls(
                                _msgs,
                                reason="el humano corrigió el rumbo mientras "
                                       "esta tool corría")
                            messages_json = _dump_messages(_msgs)
                        except Exception as e:  # noqa: BLE001
                            logger.warning(
                                "steer: no pude rescatar historial (%r) — "
                                "sigo sin cortar, se pierde la corrección", e)
                            _nudge = ""
                        if _nudge:
                            _u = getattr(agent_run, "usage", None)
                            usage = _u() if callable(_u) else _u
                            steer_text = _nudge
                            last_phase = "steered"
                            logger.info(
                                "run_expert: steer del humano tras %d tools — "
                                "re-entro con la corrección", tool_calls_count)
                            await _emit_progress(
                                phase="steer", tool=None,
                                message=_clip_say(_nudge))
                            # `return`, no `break`: al salir del async for
                            # normalmente el código de abajo lee
                            # agent_run.result, que en un corte a mitad es
                            # None. Mismo patrón que el corte por
                            # presupuesto. El finally mata el watchdog.
                            return
                    # ModelRequestNode → el agente está pensando (post-tool)
                    # CallToolsNode → el modelo respondió (con tools o con
                    #   el texto final; pydantic-ai 2.x NO emite un
                    #   ModelResponseNode, la rama que lo esperaba estaba
                    #   muerta y por eso la fase quedaba clavada en la tool
                    #   vieja mientras el modelo redactaba)
                    node_kind = type(node).__name__
                    if node_kind == "CallToolsNode":
                        # Tools pedidas en este turno, CON sus args: el
                        # modelo pide varias en un mismo response y cada
                        # una necesita su propio evento (ver abajo).
                        mr = getattr(node, "model_response", None)
                        pedidas, says, _recent = _clasificar_partes(mr)
                        recent_tool_calls.extend(_recent)
                        # El POR QUÉ (2026-07-25): el mismo response que pide
                        # las tools trae el TextPart donde el modelo dice qué
                        # va a hacer y por qué. Lo tirábamos — la UI mostraba
                        # "📄 leyó x.py" sin motivo, y un run de 20 pasos era
                        # una lista de tools sin hilo narrativo. Va ANTES del
                        # paso de tool: primero dice, después hace.
                        for txt in says:
                            await _emit_progress(
                                phase="say", tool=None,
                                message=_clip_say(txt))
                        if pedidas:
                            last_tool_name = pedidas[-1][0]
                            last_phase = "tool_call"
                            # Streaming al bot (2026-07-20d): en vez de un
                            # genérico "🔧 tool", armamos una línea legible
                            # por cada tool call (con el path/cmd) y, para
                            # edit_file, el diff old→new (que ya viene en
                            # los args — no tocamos el filesystem). El bot
                            # acumula estas líneas en un timeline vivo.
                            #
                            # UNO POR TOOL, no uno por turno (2026-09-04).
                            # Antes se contaban N y se emitía solo el
                            # último, con los args del PRIMER part de ese
                            # nombre: el timeline perdía el 30,8% de las
                            # tool calls (2.586 de 8.383 medidas sobre 228
                            # chats) y, cuando el turno traía dos shell,
                            # mostraba un comando pegado a la salida del
                            # otro. Se ejecutaban igual — lo que faltaba
                            # era el rastro para el humano.
                            for nombre, args, call_id in pedidas:
                                tool_calls_count += 1
                                step_msg, step_diff, step_cmd = (
                                    _format_tool_step(nombre, args))
                                await _emit_progress(
                                    phase="tool_call",
                                    tool=nombre,
                                    tool_calls=tool_calls_count,
                                    message=step_msg,
                                    diff=step_diff,
                                    cmd=step_cmd,
                                    tool_call_id=call_id,
                                )
                        elif says:
                            # Response con texto y sin tools = la respuesta
                            # final ya está aterrizando.
                            last_phase = "writing"
                            await _emit_progress(phase="writing", tool=None)
                    elif node_kind == "ModelRequestNode":
                        # El request que vuelve al modelo trae los
                        # ToolReturnPart de las tools que acaban de correr:
                        # es el único lugar donde se ve lo que realmente
                        # entra al historial (y por lo tanto lo que se
                        # reenvía en cada vuelta desde acá hasta el final).
                        meter_turn += 1
                        _measure_tool_returns(
                            node, tool_meter, meter_turn)
                        # …y es también el único lugar donde se ve QUÉ
                        # contestó la terminal. Se engancha al paso de la
                        # tool que ya está en el timeline, así la tarjeta
                        # queda "comando + salida" en vez de solo el
                        # comando (2026-08-27).
                        for _tname, _tout, _call_id in _console_tool_outputs(node):
                            if out_budget <= 0:
                                break
                            out_budget -= len(_tout)
                            await _emit_progress(
                                phase="tool_result", tool=_tname,
                                output=_tout, tool_call_id=_call_id)
                        # Volvió a pensar (arranque o post-tool). Sin esto la
                        # UI se quedaba con el "🔧 read_file" de hace 50s en
                        # pantalla mientras el modelo redactaba, y parecía
                        # colgado. El guard evita repetir el evento.
                        if last_phase != "thinking":
                            last_phase = "thinking"
                            await _emit_progress(phase="thinking", tool=None)
                    # UserPromptNode y EndNode no nos dicen nada nuevo.
            except _CapabilityRequested:
                message_history = list(agent_run.all_messages())
                _close_orphan_tool_calls(message_history, reason="activación de capacidad en curso")
                messages_json = _dump_messages(message_history)
                usage = agent_run.usage()
                raise
            except asyncio.CancelledError:
                # Cancel de la task del runner. Tres causas: watchdog
                # (idle), wait_for (tope global) o cancel del usuario.
                # En TODAS rescatamos el historial parcial PRIMERO —
                # sin esto el avance se pierde y el próximo turno de la
                # conversación arranca de cero (el "falla a contextos
                # largos": cada retry re-pagaba todo el trabajo).
                if rescue is not None:
                    rescue.update(
                        progress_events=progress_events, tool_calls=tool_calls_count,
                        duration_ms=int((time.monotonic() - t0) * 1000),
                        model=spec, last_tool=last_tool_name)
                try:
                    _msgs = agent_run.all_messages()
                    # Bug fix 2026-07-25: si cortamos con una tool en
                    # vuelo, el historial queda con un tool call sin
                    # respuesta y el **continúa** que ofrecemos abajo
                    # muere con UserError. Lo cerramos antes de serializar.
                    _close_orphan_tool_calls(
                        _msgs,
                        reason="el run se cortó mientras esta tool corría")
                    messages_json = _dump_messages(_msgs)
                    # Bug fix 2026-07-25: el cancel del humano re-lanza y
                    # server.py retorna temprano, así que el historial que
                    # acabamos de rescatar moría acá — cancelar costaba TODO
                    # el avance y el próximo turno arrancaba de cero. `rescue`
                    # lo saca sin await (la task ya está cancelada).
                    if rescue is not None:
                        rescue["messages_json"] = messages_json
                        rescue["tool_calls"] = tool_calls_count
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "no pude rescatar historial parcial tras cancel: %r", e)
                # Bug fix 2026-07-25: rescatar el usage igual que hacen
                # las ramas de retries/budget. Sin esto, todo run cortado
                # por idle se guardaba con tokens_in/out NULL — el trabajo
                # se pagó igual y las métricas lo contaban como 0→0.
                usage = getattr(agent_run, "usage", None)
                if callable(usage):
                    try:
                        usage = usage()
                    except Exception:  # noqa: BLE001
                        usage = None
                if rescue is not None and usage is not None:
                    rescue["tokens_in"] = tokens_in_prev + (usage.input_tokens or 0)
                    rescue["tokens_out"] = tokens_out_prev + (usage.output_tokens or 0)
                    rescue["cache_read_tokens"] = cache_prev + (usage.cache_read_tokens or 0)
                if idle_cause[0] is None:
                    # No fue el watchdog: tope global o cancel real.
                    # Que el round loop / caller clasifique.
                    raise
                # Bug fix 2026-07-25: no culpar al provider a ciegas. Si
                # el corte nos agarró en fase tool_call, el sospechoso es
                # la tool que nunca devolvió — y su nombre ya lo tenemos.
                # Culpar al provider mandaba a debuggear MiniMax cuando el
                # cuelgue real era un `npx vite --host` local que dejaba
                # un node vivo reteniendo los pipes (un chat de ejemplo).
                if last_phase == "tool_call" and last_tool_name:
                    culprit = (
                        f"la tool `{last_tool_name}` no devolvió en "
                        f">{idle_timeout:.0f}s. Suele ser un comando que "
                        "no termina solo (un server en foreground, un "
                        "prompt interactivo) o un proceso hijo que quedó "
                        "colgado — no el provider.")
                else:
                    culprit = (
                        f"el experto quedó idle >{idle_timeout:.0f}s sin "
                        "emitir ningún evento. Probable provider colgado.")
                last_phase = "idle_timeout"
                output_text = (
                    f"⚠️ Corté el run: {culprit} Guardé lo avanzado: "
                    "manda **continúa** para retomar desde acá, o sube "
                    "`expert_idle_timeout_s` si de verdad esperabas algo "
                    "tan largo.")
                logger.warning(
                    "run_expert: idle_timeout tras %.0fs (tool=%s, "
                    "tool_calls=%d)", idle_timeout, last_tool_name,
                    tool_calls_count)
                raise
            except UnexpectedModelBehavior as umb:
                # Bug fix 2026-07-20: capturar específicamente el caso
                # "Tool 'X' exceeded max retries" en vez de propagar la
                # excepción cruda. El LLM insistió N veces con un tool
                # call que la tool rechazó (path inválido, archivo que
                # no existe, etc.) y pydantic-ai cortó. Con retries=3
                # arriba le dimos más chances, pero si aún así falla,
                # NO matamos el chat: rescatamos el historial parcial
                # y devolvemos un mensaje accionable que liste los
                # archivos problemáticos. El humano puede retomar el
                # chat con la info concreta.
                msg = str(umb.message or "")
                if "exceeded max retries" in msg or "max retries" in msg:
                    last_phase = "tool_retries_exhausted"
                    # Extraer la tool y el último path intentado del
                    # historial parcial para que el humano sepa qué
                    # archivo fue el problema.
                    failing_tool = "?"
                    failing_args: dict = {}
                    try:
                        for m in reversed(
                                agent_run.all_messages()):
                            for part in getattr(m, "parts", []) or []:
                                tname = getattr(part, "tool_name", None)
                                if tname and getattr(
                                        part, "args", None):
                                    failing_tool = tname
                                    failing_args = dict(
                                        part.args or {})
                                    break
                            if failing_tool != "?":
                                break
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "tool_retries_exhausted: no pude extraer "
                            "args fallidos: %r", e)
                    # Guardar el historial para resume. Igual que en el
                    # cancel: la tool que agotó los retries puede quedar
                    # sin respuesta y romper el **continúa**.
                    try:
                        _msgs = agent_run.all_messages()
                        _close_orphan_tool_calls(
                            _msgs,
                            reason="la tool agotó los reintentos")
                        messages_json = _dump_messages(_msgs)
                    except Exception:  # noqa: BLE001
                        pass
                    usage = getattr(agent_run, "usage", None)
                    if callable(usage):
                        try:
                            usage = usage()
                        except Exception:  # noqa: BLE001
                            usage = None
                    output_text = (
                        f"⚠️ La tool `{failing_tool}` rechazó el "
                        "tool call 3 veces seguidas con los mismos "
                        f"args (`{json.dumps(failing_args, ensure_ascii=False)[:300]}`). "
                        "El bot se quedó insistiendo con el mismo path/"
                        "argumento en vez de corregirlo. Guardé lo "
                        "avanzado: manda **continúa** para que siga, o "
                        "indica explícitamente el path correcto del "
                        "archivo que quieres que lea/edite."
                    )
                    logger.warning(
                        "run_expert: tool_retries_exhausted tool=%s "
                        "args=%s tool_calls=%d", failing_tool,
                        failing_args, tool_calls_count)
                    return  # salimos con status ok + output_text accionable
                # Cualquier otra UnexpectedModelBehavior: propagamos
                # para que el caller decida (server.py lo convierte en
                # mensaje al humano con el contexto del chat).
                raise
            except UsageLimitExceeded:
                # Corte por presupuesto (request_limit): el modelo loopeó sin
                # converger (típico de tareas mal acotadas — ver ESTADO.md).
                # NO perdemos lo andado: rescatamos el historial parcial
                # (agent_run.all_messages() sirve mid-run) para que la
                # conversación quede REANUDABLE, y devolvemos un mensaje útil
                # en vez de propagar el stacktrace crudo. status sigue "ok";
                # phase_at_end="budget_exceeded" marca el corte para diagnóstico.
                budget_exceeded = True
                last_phase = "budget_exceeded"
                try:
                    messages_json = _dump_messages(agent_run.all_messages())
                except Exception as e:  # noqa: BLE001 — rescate best-effort
                    logger.warning(
                        "no pude rescatar historial parcial tras corte por "
                        "presupuesto: %r", e)
                usage = getattr(agent_run, "usage", None)
                if callable(usage):
                    try:
                        usage = usage()
                    except Exception:  # noqa: BLE001
                        usage = None
                output_text = (
                    f"⚠️ Esta tarea superó el presupuesto de {request_limit} "
                    "pasos del bot antes de terminar (probablemente es muy "
                    "amplia para un solo mensaje). Guardé lo avanzado: puedes "
                    "escribir **continúa** para que siga desde acá, o acotar "
                    "el alcance (menos archivos/objetivos por vez)."
                )
                return
            except (ModelHTTPError, httpx.HTTPError,
                    anyio.ClosedResourceError, anyio.BrokenResourceError,
                    anyio.EndOfStream) as e:
                # Caída del proveedor a mitad del run (2026-08-26). El
                # endpoint gratis de NVIDIA devolvió 500 a los 18 minutos
                # de ejecutor y la excepción subía CRUDA hasta el
                # `except Exception` de server.py — donde `status="error"`
                # apaga el guardado del historial y el canal `rescue` solo
                # se lee en la rama de cancel. Resultado medido (chat
                # un run de ejemplo / conversación un hilo de ejemplo): content vacío, tokens
                # NULL, tool_calls NULL y la conversación sin un solo
                # turno: 18 minutos de trabajo tirados y el próximo
                # mensaje arrancando de cero.
                #
                # `httpx.HTTPError` cubre el otro lado de la misma
                # falla: pydantic-ai envuelve los status en
                # ModelHTTPError, pero una conexión reseteada, un DNS
                # caído o un read timeout del transporte salen crudos y
                # perdían el run igual. `_que_hacer` ya los contempla —
                # sin `status_code` devuelve "reintentar".
                #
                # ponytail: una tool que haga HTTP por su cuenta (un MCP
                # remoto) también puede tirar httpx.HTTPError y acá se
                # lee como "se cayó el proveedor". El veredicto queda mal
                # etiquetado, pero el resultado es el correcto igual:
                # reintento y corte reanudable en vez de perder el run.
                # Separarlos pide envolver cada toolset, que es bastante
                # más diff; hacerlo cuando aparezca uno real.
                #
                # Los errores de anyio son el MCP local: cuando el stdio
                # de un server se muere a mitad del run, la sesión tira
                # `ClosedResourceError` y subía cruda, con el mismo daño
                # que describe el párrafo de arriba. Aparecieron 13 runs
                # así en 14 días (14 min de trabajo tirado); el último,
                # `un run de ejemplo` en code-hero-rpg, murió a los 101s con la
                # conversación sin un solo turno. El reintento acá vale
                # la pena aunque la sesión siga muerta: el modelo puede
                # terminar sin volver a tocar ESE server, y si vuelve a
                # caer, el corte ya es reanudable — el próximo turno
                # re-adquiere el MCP por el pool, que lo prueba y lo
                # levanta de nuevo.
                #
                # Rescatamos igual que el corte por presupuesto y volvemos
                # SIN excepción: quién reintenta y quién corta lo decide
                # el round loop con `_que_hacer`.
                provider_error = e
                last_phase = "provider_error"
                try:
                    _msgs = agent_run.all_messages()
                    # Si el corte agarró una tool en vuelo, el historial
                    # queda con un tool call sin respuesta y el
                    # **continúa** muere con UserError. Mismo cierre que
                    # en el cancel.
                    _close_orphan_tool_calls(
                        _msgs,
                        reason="el proveedor cortó mientras esta tool corría")
                    messages_json = _dump_messages(_msgs)
                except Exception as _e:  # noqa: BLE001 — rescate best-effort
                    logger.warning(
                        "no pude rescatar historial parcial tras caída del "
                        "proveedor (%s): %r", getattr(e, "status_code", "?"), _e)
                usage = getattr(agent_run, "usage", None)
                if callable(usage):
                    try:
                        usage = usage()
                    except Exception:  # noqa: BLE001
                        usage = None
                return
            finally:
                # Pase lo que pase (normal, UsageLimitExceeded, cancel
                # por watchdog), matamos la task de watchdog para que
                # no quede colgada consumiendo CPU/event-loop slots.
                if not watchdog_task.done():
                    watchdog_task.cancel()
                    try:
                        await watchdog_task
                    except (asyncio.CancelledError, Exception):
                        pass

        # Al salir del async for, agent_run.result es el AgentRunResult
        # completo (output, usage, all_messages). Capturamos TODO acá
        # para no perder referencias (el result se libera al salir del
        # async with, así que dump_json tiene que pasar adentro).
        result = agent_run.result
        output_text = str(result.output) if result.output is not None else ""
        usage = result.usage
        messages_json = _dump_messages(result.all_messages())

    # F1: hasta 3 rounds — si el LLM llama use_capability, el run se
    # reinicia con el toolset pedido adjunto (mismo user + historial;
    # lo andado en el round abortado se descarta — v1 aceptable). El
    # timeout es un deadline global, no por round.
    #
    # `deadline_pedido` (9/9/2026) es el del PEDIDO entero, que el runner
    # por etapas calcula una vez y le pasa a cada pasada. Sin él, cada
    # llamada a `run_expert` estrenaba su `t0` y el numero configurado no
    # representaba nada: con los defaults eran tres pasadas de 300s. Se
    # toma el MENOR de los dos — una pasada tampoco puede pasarse de su
    # propio techo, y el del pedido no puede estirarlo.
    deadline = t0 + timeout
    if deadline_pedido:
        deadline = min(deadline, deadline_pedido)
    max_rounds = 3
    # Iter 9.11 fix4: piso mínimo para entrar a un round. Sin esto,
    # un LLM que pide 2 capabilities problemáticas puede consumir
    # 2x el timeout (300s + 300s = 600s) y recién ahí cortar. Si
    # quedan <30s al deadline, abortamos limpio en vez de entrar
    # a un round que no va a terminar.
    ROUND_MIN_S = 30.0
    # 2026-07-20c: auto-continue de presupuesto. El corte por
    # request_limit dejó de ser guillotina: si agarra al experto
    # PROGRESANDO (tools variadas, sin loop — caso real: tarea de
    # relaydemobot cortada a los 50 pasos con 69 tools y 431s de
    # trabajo útil), re-entramos con el historial rescatado + nudge
    # "continúa" (mismo mecanismo que el resume manual, que ya anda)
    # hasta max_legs tandas. Los anillos de seguridad reales son el
    # idle watchdog y el tope global; el budget queda como checkpoint
    # anti-loop. La capa 2 (_elide_old_tool_returns) viaja en el
    # historial rescatado, así que el contexto entre tandas no explota.
    max_legs = int(defaults.get("max_legs") or config.expert_max_legs())
    # Techo duro cuando HAY supervisor de media corrida (2026-08-17). No
    # es un presupuesto: es el backstop para el caso de que el verificador
    # nunca objete y el deadline global no llegue nunca. La regla de
    # verdad la pone el supervisor; esto solo evita el bucle infinito si
    # se queda dormido diciendo "todo bien".
    max_legs_hard = int(
        defaults.get("max_legs_hard") or config.expert_max_legs_hard())
    if max_legs_hard < max_legs:
        max_legs_hard = max_legs
    # Tope de subdivisión automática (Fase 1, 2026-09-02) — ver el
    # comentario en el `if budget_exceeded:` de más abajo para la
    # medición completa.
    max_tool_calls = config.expert_max_tool_calls()
    legs = 1
    cap_rounds = 0
    tokens_in_prev = 0   # usage acumulado de tandas anteriores
    tokens_out_prev = 0
    cache_prev = 0       # 2026-08-31: cache reads de tandas anteriores
    while True:
        remaining = deadline - time.monotonic()
        # 2026-07-20b: el piso NO aplica a la primera pasada — con un
        # timeout per-project chico (<30s) el experto directamente no
        # corría nunca (abortaba acá con phase "thinking" y cero trabajo).
        if (cap_rounds or legs > 1 or steers) and remaining < ROUND_MIN_S:
            logger.warning(
                "run_expert: abortando, %.1fs restantes < %.0fs piso",
                remaining, ROUND_MIN_S)
            break
        extra: list[Any] = []
        if use_catalog and cap_rounds < max_rounds - 1:
            uc = _make_use_capability()
            if uc is not None:
                extra.append(uc)
        agent = _build_agent(extra)
        try:
            # deadline y time.monotonic() en SEGUNDOS (consistente).
            await asyncio.wait_for(
                _run_iter(agent),
                timeout=max(1.0, deadline - time.monotonic()))
        except _CapabilityRequested as e:
            logger.info("use_capability(%r): re-run con el toolset adjunto",
                        e.name)
            cap_rounds += 1
            selection.add(e.name)
            if usage:
                tokens_in_prev += usage.input_tokens or 0
                tokens_out_prev += usage.output_tokens or 0
                cache_prev += usage.cache_read_tokens or 0
                usage = None
            toolsets, attached_mcps, visible_mcps = await _catalog_toolsets(
                db, project, selection, mcp_pool, _mcp_tool_to, _inflight, _hide,
                image_artifacts, has_vision(spec))
            if any(m["capability"] == "browser" for m in attached_mcps) \
                    and EVIDENCE_BLOCK not in instructions:
                instructions += f"\n\n{EVIDENCE_BLOCK}"
            user = ("Continúa desde las herramientas ya ejecutadas. Revisa las "
                    "capacidades disponibles ahora; no repitas efectos ya realizados.")
            images = None  # ya están en message_history
            await _emit_progress(phase="thinking", tool=None)
            continue
        except asyncio.CancelledError:
            if last_phase == "idle_timeout":
                # El watchdog cortó por idle: output_text y messages_json
                # ya quedaron rescatados en _run_iter. Soft-cut: devolvemos
                # resultado reanudable en vez de propagar (propagar lo
                # etiquetaba "cancelado"/"timeout" en la UI y tiraba el
                # historial). uncancel() limpia el cancel del watchdog
                # (3.12: wait_for corre en ESTA task, no en una hija).
                _t = asyncio.current_task()
                if _t is not None:
                    _t.uncancel()
                break
            raise  # cancel real (usuario /experts/cancel o shutdown)
        except asyncio.TimeoutError:
            # Tope global (segundo anillo, safety). Si llegamos acá el
            # experto PROGRESABA — el idle real lo corta el watchdog
            # antes. Matar trabajo productivo sin rescate era el bug de
            # contextos largos: soft-cut reanudable, mismo patrón que
            # budget_exceeded. messages_json ya quedó rescatado en el
            # except CancelledError de _run_iter (wait_for cancela la
            # task interna al vencer).
            last_phase = "hard_timeout"
            output_text = (
                f"⚠️ La tarea superó el tope global de {timeout:.0f}s de "
                "trabajo continuo (venía progresando, no colgada). Guardé "
                "lo avanzado: manda **continúa** para que siga desde acá, "
                "o sube `expert_timeout_s` para tareas largas.")
            logger.warning(
                "run_expert: hard_timeout tras %.0fs (tool=%s, "
                "tool_calls=%d, historial rescatado=%s)",
                timeout, last_tool_name, tool_calls_count,
                bool(messages_json))
            break
        # _run_iter terminó sin excepción. ¿Cortó porque el humano
        # corrigió el rumbo? → re-entramos con su texto como prompt sobre
        # el historial rescatado. Es el MISMO mecanismo que el
        # auto-continue de presupuesto (ver abajo), solo cambia quién
        # escribe el nudge. El anillo de seguridad sigue siendo el
        # deadline global: no hay tope de steers porque cada uno exige un
        # POST de un humano.
        if last_phase == "steered" and steer_text:
            if not messages_json:
                logger.warning("steer: sin historial rescatado — corto acá")
                break
            try:
                message_history = ModelMessagesTypeAdapter.validate_json(
                    messages_json)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "steer: historial rescatado inválido (%r) — corto acá", e)
                break
            if usage:
                tokens_in_prev += usage.input_tokens or 0
                tokens_out_prev += usage.output_tokens or 0
                cache_prev += usage.cache_read_tokens or 0
            user = steer_text
            steer_texts.append(steer_text)
            steer_text = ""
            steers += 1
            last_phase = "thinking"
            continue
        # ¿Se cayó el proveedor a mitad de la tanda? (2026-08-26). Un
        # 5xx, un 408 o un corte de red pueden salir distinto en cinco
        # segundos, así que reintentamos sobre el historial rescatado —
        # el mismo mecanismo del auto-continue de presupuesto. Un 429 o
        # un 4xx no: `_que_hacer` los manda a cambiar de modelo y acá no
        # hay cascada (ver `_PROVIDER_RETRIES`), así que ahí cortamos con
        # el trabajo guardado en vez de quemar el contexto de nuevo.
        if provider_error is not None:
            _err, provider_error = provider_error, None
            _sig = getattr(_err, "status_code", None) or type(_err).__name__
            if (_que_hacer_con_el_proveedor(_err) == "reintentar"
                    and provider_retries < _PROVIDER_RETRIES
                    and (deadline - time.monotonic()) >= ROUND_MIN_S):
                provider_retries += 1
                # El `usage` de la próxima pasada pisa al de esta, y esta
                # se pagó igual: acumular va ANTES de decidir si el
                # historial sirve.
                if usage:
                    tokens_in_prev += usage.input_tokens or 0
                    tokens_out_prev += usage.output_tokens or 0
                    cache_prev += usage.cache_read_tokens or 0
                # Con trabajo hecho retomamos DESDE el historial
                # rescatado; sin trabajo se repite la request tal cual
                # estaba, que es lo que significa "reintentar".
                if tool_calls_count and messages_json:
                    try:
                        message_history = (
                            ModelMessagesTypeAdapter.validate_json(
                                messages_json))
                        user = (
                            "continúa con la tarea desde donde quedaste; si "
                            "ya está completa, responde con el resumen final")
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "provider_error: historial rescatado inválido "
                            "(%r) — reintento sin él", e)
                last_phase = "thinking"
                progress_events.append(
                    {"phase": "provider_retry", "attempt": provider_retries})
                logger.warning(
                    "run_expert: el proveedor cortó (%s) tras %d tools — "
                    "reintento %d/%d sobre el historial rescatado",
                    _sig, tool_calls_count, provider_retries,
                    _PROVIDER_RETRIES)
                await _emit_progress(phase="heartbeat", tool=None)
                await asyncio.sleep(_PROVIDER_BACKOFF_S)
                continue
            last_phase = "provider_error"
            output_text = (
                f"⚠️ El proveedor del modelo cortó el run ({_sig}) y no se "
                "pudo retomar. Guardé lo avanzado: manda **continúa** para "
                "que siga desde acá, o cambia de modelo si el endpoint "
                "sigue caído."
            )
            logger.warning(
                "run_expert: corte por caída del proveedor (%s) tras %d "
                "tools y %d reintento(s); historial rescatado=%s",
                _sig, tool_calls_count, provider_retries, bool(messages_json))
            break
        # ¿Corte por presupuesto con trabajo real en curso? → otra tanda.
        # Si no, listo.
        if budget_exceeded:
            loop_hit = _tool_loop_detected(recent_tool_calls)
            has_time = (deadline - time.monotonic()) >= ROUND_MIN_S
            # Control de media corrida (2026-08-17). El corte de tanda es el
            # único punto natural donde alguien puede mirar si el ejecutor
            # sigue el plan, y hasta ahora nadie miraba: el verificador
            # corría UNA vez, al final. En el run de sample-shop eso significó
            # descubrir a los 172 pasos algo que ya era visible a los 64.
            #
            # `on_leg_boundary` lo provee run_expert_staged (que es quien
            # tiene el plan). Sin supervisor, None → todo sigue igual.
            off_plan_feedback = ""
            if (on_leg_boundary is not None and messages_json
                    and not loop_hit and has_time):
                try:
                    off_plan_feedback = await on_leg_boundary({
                        "content": output_text,
                        "phase_at_end": last_phase,
                        "messages_json": messages_json,
                        "tool_calls": tool_calls_count,
                        "leg": legs,
                    }) or ""
                except Exception as e:  # noqa: BLE001
                    # Un supervisor caído no puede matar el run: sin
                    # veredicto, no hay objeción, y el run sigue.
                    #
                    # Decisión deliberada: el techo extendido se MANTIENE
                    # aunque el supervisor se esté cayendo. La alternativa
                    # —degradar a max_legs y truncar— castiga al run por
                    # una falla del proveedor del verificador, y deja la
                    # tarea a medias justo por el motivo más ajeno a ella.
                    # El anillo real sigue siendo el deadline global.
                    logger.warning(
                        "run_expert: on_leg_boundary rompió (%r) — sigo sin "
                        "veredicto", e)
                    off_plan_feedback = ""
            if off_plan_feedback:
                # Otra tanda no lo arregla, lo aleja más. Cortamos con el
                # porqué y el trabajo guardado: el humano corrige el rumbo
                # con un mensaje, que es más barato que 50 pasos perdidos.
                last_phase = "off_plan"
                logger.warning(
                    "run_expert: corte por off_plan en la tanda %d "
                    "(tools=%d): %s", legs, tool_calls_count,
                    off_plan_feedback[:160])
                output_text += (
                    f"\n\n🧭 **Me desvié del plan y me detuve en el paso "
                    f"{tool_calls_count}.** El verificador dice: "
                    f"_{off_plan_feedback}_\n\nLo hecho está guardado. "
                    "Corrígeme el rumbo en un mensaje (o escribe "
                    "**continúa** si querías que siguiera por acá).")
                break
            # Tope de subdivisión automática (Fase 1, 2026-09-02;
            # corregido dos veces el mismo día. V1: umbral en TANDAS,
            # mal inferido (`tool_calls // request_limit` no mide
            # tandas de verdad — un request trae varias tool calls;
            # falló en 2 de 4 nodos contra timeline real). V2: umbral
            # de 250 `tool_calls` (métrica real, medida sobre 238 nodos
            # de grafo: 0-200 → 0-4% se cortan; 200-250 → 36%; 250+ →
            # 85%) pero aplicado SIEMPRE, con o sin supervisor — y eso
            # también estaba mal: separando por camino, con 250+ tool
            # calls los nodos de grafo (sin supervisor) cortan 85% de
            # las veces, pero los CHATS staged (con supervisor)
            # terminan bien 60% de las veces (3 de 5 medidos, uno con
            # 324 tool calls) — la extensión por supervisor se gana el
            # sueldo justo donde hay supervisor. SampleApp (172 tool
            # calls, con supervisor) es de ESE camino, no del de grafo.
            #
            # Versión final: el tope SOLO aplica sin supervisor
            # (`on_leg_boundary is None`) — que es exactamente el caso
            # de los nodos de grafo (`orquestador.py` llama a
            # `run_expert` sin ese hook). Con supervisor no se toca
            # nada: sigue decidiendo el verificador con `max_legs_hard`,
            # como antes de las tres versiones de hoy. Fase propia
            # (`budget_split`, ni `budget_exceeded` ni `off_plan`) para
            # distinguir "lo corté yo a tiempo" de "se acabó el
            # presupuesto sin más": el nodo queda fallado y reanudable,
            # no `hecho` (ver `grafo.FASES_INCOMPLETAS`).
            if on_leg_boundary is None and tool_calls_count >= max_tool_calls:
                last_phase = "budget_split"
                logger.warning(
                    "run_expert: corte por budget_split (tools=%d >= "
                    "%d) en la tanda %d — tope de subdivisión "
                    "alcanzado", tool_calls_count, max_tool_calls, legs)
                output_text += (
                    f"\n\n✂️ **Paré en el paso {tool_calls_count} "
                    f"(tanda {legs}).** A esta altura el 85% de los "
                    "nodos no cierran — esta tarea es más grande de lo "
                    "que entra en un nodo. Lo hecho está guardado. "
                    "Convendría partirla en lotes (ej: \"lote 1\", "
                    "\"lote 2\"), que es como sí funcionaron los nodos "
                    "hermanos.")
                break
            # Con supervisor, `max_legs` deja de ser el techo y pasa a ser
            # el punto donde empezamos a pedir permiso: mientras el
            # verificador no diga off_plan, la tarea se termina. Es lo que
            # pidió el usuario — que tarde no importa, que quede a medias
            # sí. Sin supervisor el techo sigue siendo max_legs.
            techo = max_legs_hard if on_leg_boundary is not None else max_legs
            if legs < techo and messages_json and not loop_hit and has_time:
                try:
                    message_history = ModelMessagesTypeAdapter.validate_json(
                        messages_json)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "auto-continue: historial rescatado inválido (%r) — "
                        "corto acá", e)
                    break
                if usage:
                    tokens_in_prev += usage.input_tokens or 0
                    tokens_out_prev += usage.output_tokens or 0
                    cache_prev += usage.cache_read_tokens or 0
                legs += 1
                progress_events.append(
                    {"phase": "auto_continue", "leg": legs})
                budget_exceeded = False
                user = (
                    "continúa con la tarea desde donde quedaste; si ya "
                    "está completa, responde con el resumen final")
                logger.info(
                    "run_expert: budget auto-continue → tanda %d/%d "
                    "(tools acumuladas=%d, supervisada=%s)", legs, techo,
                    tool_calls_count, on_leg_boundary is not None)
                await _emit_progress(phase="heartbeat", tool=None)
                continue
            # No seguimos solos: explicar el porqué en el mensaje.
            if legs > 1:
                output_text += (
                    f" (Ya se auto-extendió {legs} tandas: "
                    f"{legs * request_limit} pasos totales.)")
            if loop_hit:
                output_text += (
                    " Corté porque venía repitiendo exactamente la misma "
                    "tool con los mismos args (loop, no progreso).")
        break
    duration_ms = int((time.monotonic() - t0) * 1000)

    if not messages_json:
        # No debería pasar salvo bug en pydantic-ai. Loggeamos y
        # devolvemos vacío — el caller (server.py) no rompe.
        logger.warning(
            "run_expert: messages_json vacío al finalizar (chat seguirá sin "
            "historial persistido para replay)")

    # Bug fix 2026-08-10: si el LLM terminó el run sin texto pero dejó
    # tools ejecutadas en el historial, persistimos como "ok" un chat
    # con content="" — el usuario ve un ✓ done sin respuesta. El helper
    # arma un fallback accionable y marca phase_at_end="no_final_text"
    # para que la UI y Discord puedan pintarlo distinto. Solo aplica
    # en el camino "natural" (last_phase en thinking/writing); los
    # cortes por idle/timeout/budget/retries ya tienen su mensaje.
    if not output_text:
        _fb, _ph = _synthesize_no_final_text(
            messages_json, current_phase=last_phase)
        if _fb:
            output_text = _fb
            last_phase = _ph
            logger.info(
                "run_expert: output vacío con tools ejecutadas — "
                "fallback 'no_final_text' aplicado (output=%d chars)",
                len(_fb))

    # El gemelo del bug de arriba (2026-08-13): el LLM escribe SOLO el
    # anuncio de la tool ("voy al archivo:") y cierra el run sin ejecutar
    # nada. Se persistía como ok/writing, así que el hilo se degradaba en
    # silencio (14 runs seguidos en un hilo de ejemplo). No cambiamos el content —
    # solo marcamos la fase para que la UI y el log lo puedan pintar.
    if (last_phase not in _CUT_PHASES and not tool_calls_count
            and output_text.rstrip().rstrip("*").endswith(":")):
        last_phase = "announced_no_tools"
        logger.warning(
            "run_expert: el modelo anunció una acción y terminó sin ejecutar "
            "ninguna tool — historial con narración decapitada o demasiado "
            "pesado; `compactar` el hilo")

    # Corte por `off_plan`: el working set NO se conserva (2026-08-23).
    #
    # `_slim_history` deja el último turno intacto para que "continúa"
    # retome con todo lo que el ejecutor tenía abierto. Esa apuesta vale
    # cuando el turno fue trabajo bueno — y `off_plan` es exactamente el
    # sistema diciendo lo contrario: que el ejecutor se fue a otra cosa.
    # Conservarlo entero garantiza que el turno siguiente arranque
    # leyendo el descarrilamiento y se descarrile igual.
    #
    # Medido en un hilo de ejemplo de sample-shop: el último turno eran 101
    # mensajes / 127 KB —el 89% del contexto— de 82 `read_file` sobre
    # modelos C# que no tenían nada que ver con el manual que se había
    # pedido. Tres runs seguidos cortados por lo mismo.
    #
    # Se poda igual que a un turno viejo, así que sobreviven el pedido
    # del humano y el texto final (el "Qué se hizo / Pendiente"): lo que
    # se cae es el spam de tool calls. El trabajo real está en el disco y
    # la narración completa queda en el .md del chat.
    if last_phase == "off_plan" and messages_json:
        try:
            _todos = list(ModelMessagesTypeAdapter.validate_json(messages_json))
            messages_json = _dump_messages(
                _slim_history(_todos, corte=len(_todos)))
        except Exception as e:  # noqa: BLE001 — nunca voltear el run por esto
            logger.warning("off_plan: no pude podar el working set (%r)", e)

    if image_artifacts:
        output_text += "\n\n" + attachments_mod.generated_markdown(image_artifacts)
    return {
        "content": output_text,
        "image_artifacts": image_artifacts,
        "model": spec,
        # Con auto-continue, usage es el de la ÚLTIMA tanda; sumamos
        # lo acumulado de las anteriores para que el reporte al humano
        # refleje el gasto real del run completo.
        "tokens_in": (
            (usage.input_tokens or 0) + tokens_in_prev if usage
            else (tokens_in_prev or None)),
        "tokens_out": (
            (usage.output_tokens or 0) + tokens_out_prev if usage
            else (tokens_out_prev or None)),
        # Parte de `tokens_in` que el provider sirvió de su caché
        # (2026-08-31). Va aparte y no restada: `tokens_in` tiene que
        # seguir siendo lo que se le mandó al modelo. Lo usa `cost_usd`
        # para cobrar esos tokens a la tarifa de caché — sin esto el
        # relay facturaba el 82,5% de la entrada de MiniMax a precio
        # pleno y ningún total podía cuadrar contra el proveedor.
        "cache_read_tokens": (
            (usage.cache_read_tokens or 0) + cache_prev if usage
            else (cache_prev or None)),
        "tool_calls": (
            usage.tool_calls
            # El usage sólo cubre la última pasada, también después de steer.
            if legs == 1 and not (cap_rounds or provider_retries or steers) and usage
            and usage.tool_calls is not None
            else tool_calls_count),
        "duration_ms": duration_ms,
        "messages_json": messages_json,
        # diagnóstico post-mortem (Fase 1, decisión 6): última fase
        # y última tool cuando terminó (ok, error, timeout, cancelled).
        "phase_at_end": last_phase,
        "last_tool": last_tool_name,
        # Lo verificado, para que el próximo turno no lo re-averigüe. Va
        # SIEMPRE, tambien en los cortes: ahi es donde mas sirve.
        "bitacora_json": bitacora.volcar(),
        # Etapa B, P3: pasos que el ejecutor marcó con `plan_step_done`.
        # Las claves son strings porque el destino es JSON. La UI los
        # cruza con `experts.pasos_del_plan(plan)` para saber qué
        # pintar verde y qué dejar pendiente.
        "plan_steps_done": {str(k): v for k, v in bitacora.pasos.items()},
        "legs": legs,  # tandas de presupuesto usadas (1 = sin auto-continue)
        "steers": steers,  # correcciones que metió el humano en vivo
        "steer_texts": steer_texts,
        "progress_events": progress_events,
        # 2026-07-26: peso de los tool results (para calibrar los caps).
        "tool_meter": summarize_tool_meter(tool_meter, meter_turn),
        # 2026-08-16: el id de la pregunta que el experto dejó abierta en
        # este turno, si preguntó. Existe porque preguntar es un FINAL
        # legítimo del turno y las etapas de después tienen que saberlo:
        # sin esto, el verificador juzgaba como incompleto un run que se
        # detuvo a propósito, y el humano terminaba leyendo "respondé
        # continúa" arriba de una tarjeta que le pedía elegir otra cosa.
        "question_id": _q_state.get("asked", ""),
    }


# ---------- Iter 11: runner por etapas (planner + executor + verifier
# ---------- + documenter) ----------
#
# Motivado por: el experto termina tareas con un porcentaje alto de
# iteraciones extra porque el LLM empieza a explorar sin comprometerse
# a un plan, y nadie le dice "ya hiciste lo que te pedí" cuando termina
# (caso típico: bugfix que aplicó un cambio parcial y respondió "listo"
# antes de verificar). El plan + verify fuerza ambas puntas:
#   - PLANNER: turno breve con modelo económico → plan numerado. Se
#     inyecta al ejecutor como `system_extra` para que el LLM empiece
#     enfocado (sin volver a pagar el costo de planificar en cada tool
#     call).
#   - EXECUTOR: el run_expert existente, intacto. Recibe el plan y lo
#     sigue. Si el presupuesto se agota, el auto-leg existente
#     (max_legs) sigue funcionando: estas etapas NO lo reemplazan, lo
#     complementan.
#   - VERIFIER: turno breve → veredicto complete / needs_more /
#     needs_human más una línea de feedback. Si el veredicto es
#     "needs_more" y el ejecutor ya gastó sus legs, el verificador le
#     dice al humano qué falta (en vez de quedar mudo). Si es
#     "needs_human", la salida lleva un prefijo claro para que el chat
#     lo marque distinto en la UI.
#   - DOCUMENTER: turno breve de redacción → resumen del cambio (qué se
#     tocó, en qué archivos, qué queda pendiente). A diferencia del
#     plan y del veredicto, este SÍ se agrega al `content` que ve el
#     humano: una etapa que nadie lee es costo puro.
#
# Por qué modelos separados: planner, verifier y documenter son turnos
# cortos con razonamiento acotado; pagar un modelo pesado para ellos es
# desperdicio. Cada etapa resuelve su spec por separado
# (`planner_model` / `verifier_model` / `documenter_model` en
# `defaults_json`, o las variables FOURBIS_*_MODEL). OJO: si esas
# variables quedan vacías la cascada termina en `model_spec()`, o sea
# el mismo modelo pesado del ejecutor — conviene fijarlas explícitas.
#
# Por qué wrapper y no función nueva: run_expert queda como el
# ejecutor; los callers de server.py / night.py / voice.py eligen entre
# `run_expert()` (un solo turno) y `run_expert_staged()` (default
# nuevo). Los tests existentes siguen llamando run_expert directo, sin
# cambios.
#
# Nota de nombres: la bandera de opt-out se sigue llamando
# `three_stage` en `defaults_json` aunque ahora las etapas sean cuatro.
# Renombrarla obligaría a migrar filas de `projects` ya escritas; el
# nombre viejo queda como identificador de datos, no como descripción.

# Los prompts viven en el módulo para que los diffs los marquen fácil.
# No van al .env: cada vez que se cambia la estrategia de prompting
# conviene un commit, no un edit de config que queda fuera de la review.

# El bullet "El plan no termina en la edición" sale de un A/B medido el
# 8/9/2026: el mismo pedido, el mismo modelo, editaba un CSS fuente y no
# corría el build cuando el pedido decía "no toques ningún otro
# archivo"; con "deja el repo en un estado consistente" sí lo corría.
# Pasó en los dos brazos del experimento y pesó más que inyectar una
# skill que describía el paso de build textualmente.
PLANNER_INSTRUCTIONS = """\
Eres el planificador de un experto técnico. Recibes el pedido del
usuario y algo de contexto del proyecto (system prompt, slug, ruta del
repositorio). Tu trabajo es producir un plan EJECUTABLE y CONCRETO, no
un resumen.

Reglas:
- Numera los pasos del 1 al N. Cada paso es una ACCIÓN, no una idea.
- Cada paso menciona QUÉ herramientas usar (read_file, list_dir,
  shell, cbm_query, edit_file, etc.) si aplica, y QUÉ
  archivo/sección/endpoint.
- El plan no termina en la edición. Si el cambio necesita un paso
  derivado para tener efecto —recompilar, regenerar un artefacto,
  migrar, correr el test que lo cubre—, ese paso es un paso más del
  plan. Y no escribas prohibiciones: una restricción de más ("no toques
  ningún otro archivo") apaga justo el paso que faltaba.
- Si el pedido es trivial (una sola línea, una pregunta directa),
  responde literalmente: `TRIVIAL: <una línea con la respuesta>` y
  nada más. No inventes pasos donde no los hay.
- Si el pedido es ambiguo, enumera las INTERPRETACIONES posibles y la
  evidencia mínima que las desambigua, en vez de elegir una y empezar.
  Máximo 3.
- Si el pedido es DEMASIADO GRANDE para una sola corrida (varios
  objetivos independientes entre sí, alcance del tipo "todos los" o
  "100% de", o una enumeración de tres o más puntos que tocan áreas
  distintas), NO planifiques la ejecución. Responde `DEMASIADO_GRANDE:`
  en la primera línea y debajo la descomposición en subtareas
  numeradas, una por línea, cada una acotada y verificable por
  separado. No uses esta salida si el pedido RETOMA algo en curso
  ("continúa", "sigue con la 2"); que el hilo ya venga de antes no lo
  hace una continuación. No agregues introducción ni cierre, y no
  preguntes por dónde empezar: ese cierre lo agrega el sistema y si lo
  escribes también queda duplicado.

  Ojo con el pedido CORTO que esconde un proyecto entero. El tamaño del
  texto no dice nada del tamaño del trabajo: "parte de 0 en un SampleApp
  nuevo" son ocho palabras y significa levantar una base de datos,
  migrar el esquema, arrancar dos servidores, sembrar datos y recién
  entonces empezar. Antes de dar un plan, preguntate qué tiene que
  existir para que el paso 1 sea posible: si la respuesta son tres
  cosas que hoy no existen, el pedido es `DEMASIADO_GRANDE:` aunque
  entre en una línea. Este error ya pasó: un pedido así se planificó
  como una sola tarea y el ejecutor se fue 20 minutos y 172 pasos a
  construir un entorno completo por su cuenta.
- No edites archivos. No escribas código. No ejecutes comandos. De eso
  se encarga el ejecutor.
- Máximo 12 pasos. Si el pedido exige más, agrupa en fases con
  numeración 1, 2, 3 y subítems.
- Idioma: español neutro, sin regionalismos. Tono: ingeniero senior,
  sin emojis.
- Devuelve SOLO el plan, sin introducción ni cierre."""

# Regla del razonador. Se agrega SOLO cuando el toolset está de verdad
# adjunto — antes vivía dentro de PLANNER_INSTRUCTIONS arrancando con
# "Si tienes disponible la herramienta…", o sea un condicional en prosa
# que el modelo tenía que evaluar sobre sus propias capacidades.
#
# Ahí estaba el bug (medido el 19/8/2026): sin la tool adjunta, el modelo
# igual intenta llamarla y el proveedor serializa el intento como TEXTO.
# La salida cruda que lo delató, de MiniMax con `toolsets=[]`:
#
#   Voy a usar pensamiento secuencial para decidir si esto es una sola
#   tarea o varias antes de escribir el plan.]<]minimax[>[<tool_call>
#   {"thou…
#
# Eso —delimitadores del chat template en el canal de texto— es la misma
# firma que los 101 planes basura de producción (`cbm_query({...})`,
# `sequentialthinking({...})`, `{"tool": ...}`). No es un modelo malo ni
# un endpoint malo: es un prompt que pide usar una tool que no está.
#
# La condición ahora la evalúa Python, que sí sabe la respuesta.
PLANNER_REASONING_RULE = """\
- Tienes la herramienta de pensamiento secuencial (`sequentialthinking`).
  Úsala ANTES de escribir el plan y solo para UNA pregunta: ¿esto es una
  tarea o son varias? Un pensamiento por cada cosa que tiene que existir
  para que el pedido sea posible, y un último pensamiento con la
  conclusión: una tarea, o `DEMASIADO_GRANDE:` con el corte. No la uses
  para redactar los pasos ni para explorar el repositorio —no tienes
  acceso al repositorio— y no repitas su contenido en la salida: el plan
  final es lo único que se lee. Tres o cuatro pensamientos alcanzan; si
  llevas más de seis, la respuesta ya es `DEMASIADO_GRANDE:`."""

VERIFIER_INSTRUCTIONS = """\
Eres el verificador de un experto técnico. Recibes:
  - el plan original del planificador,
  - el resultado del ejecutor (texto final),
  - una lista corta de las herramientas que ejecutó,
  - la EVIDENCIA del run: los comandos que corrió el harness con su
    exit code real, los pasos del plan que el ejecutor marcó, y lo que
    dice haber comprobado,
  - y el estado final del run (ok / budget_exceeded / cancelled / error).

Sobre la evidencia, que es lo que separa un veredicto de una impresión:
- Un `exit=0` lo escribió el harness: es un hecho. "Verifiqué que
  compila" lo escribió el ejecutor: es una afirmación suya. Cuando las
  dos hablan de lo mismo y no coinciden, gana el exit code.
- Que se HAYA LLAMADO a una herramienta no dice que haya funcionado.
  Un `pytest` con `exit=1` en la lista de comandos es trabajo sin
  terminar aunque el ejecutor cierre diciendo que está listo.
- Si el plan pedía algo comprobable —correr pruebas, compilar, migrar—
  y NO hay un comando que lo respalde, eso es `needs_more`: falta la
  comprobación, no alcanza con que la respuesta la dé por hecha.
- Evidencia vacía no es evidencia en contra. Una tarea de solo lectura
  o de redacción no tiene por qué dejar comandos; júzgala por el
  resultado, como antes.

Tu trabajo es UN veredicto, en este formato EXACTO (sin markdown):

VERDICT: <complete|needs_more|off_plan|needs_human>
FEEDBACK: <una línea, máximo 200 caracteres, en español, explicando el veredicto>
PASOS: <lista CSV de numeros de paso cubiertos, o vacio>

La línea `PASOS:` es OBLIGATORIA pero puede ser una lista vacía si el
plan era trivial o ninguno de los pasos quedó visiblemente cumplido.
Sirve de red de seguridad para los pasos que el ejecutor olvidó marcar
con `plan_step_done`: si vos los vés hechos, eso ya alcanza.

Reglas:
- `complete`: el ejecutor cumplió el plan. La respuesta del experto
  tiene la información, los archivos o los cambios que el plan pedía.
- `needs_more`: el plan quedó a medias y el ejecutor SEGUIRÍA
  progresando con otra pasada. No lo confundas con `needs_human`: aquí
  no hay ninguna decisión que el humano deba tomar, solo falta trabajo.
- `off_plan`: el ejecutor está haciendo algo DISTINTO de lo que el plan
  pedía. No es "va lento" ni "le falta": es que abandonó el plan y se
  fue por otro camino. Señales: el plan pedía leer y el ejecutor está
  levantando servicios; el plan pedía un archivo y hay veinte tocados;
  la cantidad de herramientas no guarda ninguna relación con el tamaño
  del plan; o el ejecutor repite una operación cuyo resultado no
  cambia. Otra pasada NO lo arregla —lo aleja más—, así que este
  veredicto CORTA el run y devuelve el control al humano.
  Ojo con el falso positivo: un plan de un paso puede necesitar varias
  herramientas legítimas para cumplirlo. Lo que define `off_plan` es el
  RUMBO, no el volumen.
- `needs_human`: el ejecutor necesita una decisión del humano (alcance
  ambiguo, riesgo que el humano tiene que aceptar, datos que solo el
  humano tiene). No es un fallo técnico: es un "detente y pregunta".
- `budget_exceeded` NO es `off_plan` por sí solo. Un ejecutor que se
  quedó sin presupuesto SIGUIENDO el plan es `needs_more`. Antes de
  votar `off_plan` con ese estado, nombra qué hizo que el plan NO
  pedía. Si no puedes nombrarlo, no es `off_plan`.
- Sé HONESTO: si el plan era trivial y la respuesta lo cubre, es
  `complete`. Si la respuesta es vaga o dice "podría..." sin haber
  verificado, es `needs_more`. No suavices el veredicto.
- No ejecutes herramientas. No edites archivos. No analices el código
  fuente: solo comparas el PLAN contra el RESULTADO.
- Una sola pasada. Sin ciclos de autocorrección.
- Idioma: español neutro, sin regionalismos.

Corrección dinámica del plan (obligatoria cuando verdict != complete):

Cuando votás `needs_more` u `off_plan`, el orquestador necesita
reconstruir el plan del Ejecutor a partir de tu salida. Para eso,
ADEMÁS del bloque `VERDICT/FEEDBACK/PASOS` de arriba, emití al final
de tu respuesta un único bloque JSON FENCED con tag `VERIFIER_VERDICT`
(un bloque por respuesta). NO uses el fence genérico de ```json: el
parser distingue tu veredicto por el tag del fence, y ```VERIFIER_VERDICT
garantiza que no se confunda con cualquier otro JSON que puedas emitir
en tu razonamiento.

```VERIFIER_VERDICT
{
  "verdict": "needs_more",
  "feedback": "<eco de FEEDBACK arriba, mismo string>",
  "steps_completed": ["1", "2"],
  "plan_correction": {
    "revised_steps": [
      {
        "id": "1",
        "description": "<acción concreta>",
        "expected_output": "<criterio verificable>",
        "status": "done"
      },
      {
        "id": "nueva-1",
        "description": "<paso NUEVO que faltaba>",
        "expected_output": "<criterio verificable>",
        "status": "pending"
      }
    ],
    "feedback_to_executor": "<instrucción precisa: nombra la desviación (off_plan) o el item faltante concreto (needs_more). Tono ingeniero senior, sin relleno>",
    "remove_step_ids": [],
    "add_step_ids": ["nueva-1"]
  }
}
```

El objeto debe validar contra `schemas/verifier_verdict.json`:
- `verdict` ∈ {`complete`, `needs_more`, `off_plan`} (EXACTO, sin
  variantes). Si tu veredicto real es `needs_human`, NO emitas este
  bloque: el orquestador trata `needs_human` como caso especial y no
  necesita `plan_correction`.
- `plan_correction` es OBLIGATORIO para `needs_more` y `off_plan`.
  Incluí TODOS los steps que siguen vigentes (los originales no
  eliminados + los nuevos), en orden de ejecución.
- `add_step_ids` lista SOLO los ids NUEVOS (los que NO estaban en el
  plan original). `remove_step_ids` lista los ids del plan previo que
  ya no aplican.
- `feedback_to_executor` ≤ 2000 chars, accionable. NO repitas
  `feedback` (ese es para el humano); este es para el Ejecutor.

Para `complete` NO emitas el bloque: el camino `complete` no cambió,
y un bloque ahí solo confundiría al parser."""

DOCUMENTER_INSTRUCTIONS = """\
Eres el documentador de un experto técnico. Recibes el pedido del
usuario, el plan, las herramientas que ejecutó el experto y su
respuesta final. Tu trabajo es redactar el registro del cambio para
que otra persona entienda qué pasó sin releer todo el hilo.

Formato EXACTO (markdown, sin encabezado de nivel 1):

**Qué se hizo:** <una o dos líneas>
**Archivos tocados:** <lista separada por comas, o "ninguno">
**Cómo verificarlo:** <un comando o una comprobación concreta, o "no aplica">
**Pendiente:** <una línea, o "nada">

Reglas:
- Solo describes lo que consta en las herramientas ejecutadas y en la
  respuesta del ejecutor. No inventes archivos, comandos ni pruebas.
- Si no hay evidencia de que algo se haya modificado, dilo: "ninguno"
  en archivos y "no aplica" en verificación. Es preferible un registro
  escueto a uno inventado.
- Nada de relleno, elogios ni resúmenes del resumen. Máximo 8 líneas
  en total.
- No ejecutes herramientas. No edites archivos.
- Idioma: español neutro, sin regionalismos. Sin emojis."""

# Techo por etapa. El `expert_timeout_s` se aplica DENTRO de
# `run_expert`, así que estos turnos quedan fuera de ese presupuesto y
# se suman al total del run: con tres etapas auxiliares el peor caso
# agrega 3 * _PV_TIMEOUT_S. 60s por turno es holgado para una llamada
# de "arma un plan" / "verifica esto" / "documenta esto"; si se cuelgan,
# cada una corta sola y el run sigue.
_PV_TIMEOUT_S = 60.0
#: Lo que se le reserva al cierre del presupuesto del pedido. Despues del
#: ultimo `run_expert` todavia corren el verificador, el documentador y
#: la persistencia: si el lazo se come el presupuesto entero, esas etapas
#: arrancan sin tiempo y el turno termina sin veredicto ni resumen — o
#: sea, gastando el modelo para tirar el resultado.
RESERVA_CIERRE_S = 90.0

#: Tools que NO pueden cambiar nada. Un run que solo llamo a estas es una
#: revision, y el documentador no tiene nada que documentar: su trabajo
#: es "que se toco, en que archivos, que queda pendiente" y ahi no se
#: toco nada. El hallazgo YA esta en la respuesta del ejecutor, asi que
#: el resumen extra repite lo que el humano acaba de leer y encima suma
#: latencia y un turno del modelo.
#:
#: La lista es de lo seguro, no de lo probable: `db_query` queda AFUERA
#: porque una conexion marcada escribible si modifica, `shell` corre lo
#: que sea, y una tool MCP desconocida podria hacer cualquier cosa. Ante
#: la duda se documenta, que es el comportamiento anterior.
_TOOLS_SOLO_LECTURA = frozenset({
    "read_file", "list_dir", "search_files", "rutas_habilitadas",
    "git_diff", "read_skill", "cbm_query", "db_connections",
    "anotar", "plan_step_done",
})


def _solo_reviso(tool_calls_summary) -> bool:
    """¿El run solo miro? `False` si no llamo a nada (no hay revision)."""
    llamadas = list(tool_calls_summary or [])
    if not llamadas:
        return False
    return all((t[0] if isinstance(t, (tuple, list)) else t)
               in _TOOLS_SOLO_LECTURA for t in llamadas)
# Techo del planificador CUANDO tiene el razonador enchufado. Con
# sequential-thinking el turno son varias vueltas de pensamiento antes de
# escribir el plan, y en 60s no entra. Sigue siendo un techo duro: si el
# razonador se va por las ramas, la etapa corta y el ejecutor arranca sin
# plan, que es el mismo degradado de siempre.
_PV_REASONING_TIMEOUT_S = 180.0

# Parsers tolerantes: los modelos a veces meten markdown, prefijos
# extra, o capitalización distinta. Si no matchea, default conservador.
_VERDICT_RE = re.compile(
    r"\b(?P<v>needs_more|off_plan|needs_human)\b", re.IGNORECASE)
_FEEDBACK_RE = re.compile(
    r"FEEDBACK\s*:\s*(?P<f>.+?)(?:\n|$)", re.IGNORECASE | re.DOTALL)
# Línea `VERDICT: x` (o `VEREDICTO:`, o con markdown alrededor). Ancla al
# principio de línea: es el contrato que pide VERIFIER_INSTRUCTIONS, y a
# diferencia de `_VERDICT_RE` no la confunde una mención en prosa.
_VERDICT_LINE_RE = re.compile(
    r"^[\s>*_#`-]*(?:VERDICT|VEREDICTO)\s*[:=]\s*[*_`\"']*"
    r"(?P<v>complete|needs_more|off_plan|needs_human)\b",
    re.IGNORECASE | re.MULTILINE)
# Una línea que es SOLO el veredicto (el modelo respetó el vocabulario
# pero se saltó la etiqueta).
_VERDICT_ALONE_RE = re.compile(
    r"^[\s>*_#`-]*(?P<v>complete|needs_more|off_plan|needs_human)[\s*_`.]*$",
    re.IGNORECASE | re.MULTILINE)
# `PASOS: 1,3,5` o `PASOS: 1 3 5` o `PASOS: ` (lista vacía).
# Lista de ints positivos; tolerante a espacios y separadores.
_PASOS_LINE_RE = re.compile(
    r"^[\s>*_#`-]*PASOS\s*[:=]\s*[*_`\"']*(?P<nums>[0-9,\s]*)\s*$",
    re.IGNORECASE | re.MULTILINE)
_NUM_RE = re.compile(r"\d+")

# ---------- saneamiento de la salida de las etapas (2026-08-15) ----------
#
# Las tres etapas auxiliares corren en otro proveedor que el ejecutor
# (iter 11 + provider nvidia del 2026-08-14). Los contratos de texto
# —`VERDICT:`, `TRIVIAL:`, `DEMASIADO_GRANDE:`— se afinaron contra
# MiniMax, que contesta pelado. Los modelos de razonamiento del catálogo
# NIM anteponen su cadena de pensamiento en el MISMO campo `content`
# cuando el endpoint no la separa en `reasoning_content`, así que el
# contrato se rompe por un prefijo, no por el contenido.
#
# Este bloque es el adaptador: saca el andamiaje y deja el texto que el
# parser espera. Barato, idempotente y sin red — si el modelo ya
# contestaba limpio, no cambia nada.

_THINK_BLOCK_RE = re.compile(
    r"<(think|thinking|reasoning)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
# Bloque de razonamiento que quedó ABIERTO (el modelo cortó por límite de
# tokens o el endpoint truncó): todo lo anterior al cierre que nunca
# llegó es andamiaje. Solo se aplica si NO hay cierre en el texto.
_THINK_OPEN_RE = re.compile(
    r"<(think|thinking|reasoning)\b[^>]*>", re.IGNORECASE)
_FENCE_RE = re.compile(
    r"^\s*```[a-zA-Z0-9_-]*\s*\n(?P<body>.*?)\n\s*```\s*$", re.DOTALL)


def _clean_stage_output(text: str) -> str:
    """Salida cruda de una etapa auxiliar → texto parseable.

    Quita, en este orden:
      1. bloques `<think>…</think>` (y variantes `<thinking>`,
         `<reasoning>`) que algunos endpoints NIM devuelven inline;
      2. un bloque de razonamiento sin cerrar — se descarta todo lo
         anterior a la etiqueta de apertura huérfana;
      3. un fence markdown que envuelva TODA la respuesta.

    No toca nada más: el texto de las etapas es el producto (el plan que
    ve el ejecutor, el registro que ve el humano), así que este helper
    solo saca andamiaje, nunca reescribe contenido.
    """
    s = (text or "").strip()
    if not s:
        return ""
    s = _THINK_BLOCK_RE.sub("", s).strip()
    # Apertura huérfana: el modelo abrió el razonamiento y nunca cerró.
    # Nos quedamos con lo que venga DESPUÉS de la última apertura; si no
    # hay nada después, el texto entero era razonamiento y no hay salida.
    if "</" not in s:
        opens = list(_THINK_OPEN_RE.finditer(s))
        if opens:
            s = s[opens[-1].end():].strip()
    m = _FENCE_RE.match(s)
    if m:
        s = m.group("body").strip()
    return s


def _stage_usage(result: Any) -> dict:
    """Tokens de un turno de etapa → `{"tokens_in": N, "tokens_out": N}`.

    Devuelve `{}` cuando no se pudo leer el usage. El dict vacío NO es lo
    mismo que cero: significa "no se sabe" (la etapa falló, o el provider
    no reportó), y las métricas lo tienen que distinguir de una etapa que
    corrió y consumió poco.

    `usage` es propiedad en pydantic-ai 2.x y era método antes; se
    soportan las dos formas para no atarse a la versión.
    """
    try:
        u = result.usage() if callable(result.usage) else result.usage
        return {"tokens_in": int(u.input_tokens or 0),
                "tokens_out": int(u.output_tokens or 0)}
    except Exception:  # noqa: BLE001 — medir nunca puede romper el run
        return {}


# ---------- corrección dinámica del Verificador → Planificador/Ejecutor (t5) ---
#
# Convención de marker (ver `schemas/verifier_verdict.json`,
# `x-marker-convention.fenced_block`):
#   bloque fenced cuyo fence de apertura lleva el token
#   `VERIFIER_VERDICT`, p.ej.
#       ```VERIFIER_VERDICT
#       {"verdict": "needs_more", "feedback": "...",
#        "steps_completed": [1, 2],
#        "plan_correction": {...}}
#       ```
#
# Solo se emite cuando `verdict != complete` (el path `complete` no
# requiere corrección → el marker queda omitido y el texto plano
# `VERDICT:/FEEDBACK:/PASOS:` sigue siendo el contrato para el
# Documentador y para el chat del humano). Cuando está, el objeto
# quepa en `schemas/verifier_verdict.json`; `schemas/_check.py` valida
# los ejemplos canónicos.
#
# Validación best-effort: este helper NO carga jsonschema para no
# sumar dependencia runtime en el relay. Comprueba a mano las claves
# obligatorias y los enums del schema; cualquier inconsistencia cae
# a `None` y se registra en `usage["plan_correction_error"]` para
# que t6 decida si reintenta. La validación completa vive en
# `schemas/_check.py`, fuera del hot path de la API.

_VERDICT_FENCE_RE = re.compile(
    r"```VERIFIER_VERDICT\s*\n(?P<body>.*?)\n\s*```",
    re.DOTALL | re.IGNORECASE)
# Enum estricto del schema: complete | needs_more | off_plan.
_VERDICT_VALUES = {"complete", "needs_more", "off_plan"}
# Claves top-level obligatorias del schema.
_VERDICT_KEYS = (
    "verdict", "feedback", "steps_completed", "plan_correction")
# Cuando verdict != complete, el schema obliga a plan_correction con
# feedback_to_executor / revised_steps / remove_step_ids / add_step_ids.
_VERDICT_PC_KEYS = (
    "feedback_to_executor", "revised_steps",
    "remove_step_ids", "add_step_ids")
# Status del step en revised_steps.
_STEP_STATUSES = {"pending", "in_progress", "done", "failed"}


def _validate_verifier_verdict_payload(payload: dict) -> Optional[str]:
    """Valida el payload completo de `VERIFIER_VERDICT` → mensaje de error o None.

    Best-effort contra `schemas/verifier_verdict.json`. Devuelve el motivo de
    rechazo (string) o None si todo cuadra.
    """
    for k in _VERDICT_KEYS:
        if k not in payload:
            return f"falta clave top-level: {k!r}"
    if payload["verdict"] not in _VERDICT_VALUES:
        return f"verdict invalido: {payload['verdict']!r}"
    if not isinstance(payload["feedback"], str):
        return "feedback no es string"
    sc = payload["steps_completed"]
    if (not isinstance(sc, list)
            or not all(isinstance(x, (int, str)) for x in sc)):
        return "steps_completed no es lista de enteros o strings"
    pc = payload.get("plan_correction")
    if payload["verdict"] != "complete":
        if not isinstance(pc, dict):
            return "plan_correction obligatorio cuando verdict != complete"
        return _validate_plan_correction(pc)
    return None


def _validate_plan_correction(pc: dict) -> Optional[str]:
    """Valida `plan_correction` → mensaje de error o None."""
    fbe = pc.get("feedback_to_executor")
    if not isinstance(fbe, str) or not fbe.strip():
        return "plan_correction.feedback_to_executor vacio o no es string"
    rs = pc.get("revised_steps")
    if not isinstance(rs, list) or not rs:
        return "plan_correction.revised_steps vacio o no es lista"
    for i, s in enumerate(rs):
        if not isinstance(s, dict):
            return f"plan_correction.revised_steps[{i}] no es objeto"
        if not all(k in s for k in ("id", "description", "expected_output", "status")):
            return (f"plan_correction.revised_steps[{i}] le faltan claves")
        if s["status"] not in _STEP_STATUSES:
            return (f"plan_correction.revised_steps[{i}].status invalido: "
                    f"{s['status']!r}")
    for k in ("remove_step_ids", "add_step_ids"):
        v = pc.get(k)
        if (not isinstance(v, list)
                or not all(isinstance(x, str) for x in v)):
            return f"plan_correction.{k} no es lista de strings"
    return None


def _extract_plan_correction(
        text: str, verdict_hint: Optional[str] = None) -> Optional[dict]:
    """Salida del Verificador → payload `plan_correction` parseado, o None.

    Busca el bloque fenced `\\`\\`\\`VERIFIER_VERDICT ... \\`\\`\\`` con el
    objeto veredicto extendido de `schemas/verifier_verdict.json` y
    devuelve solo el sub-objeto `plan_correction` validado, listo para
    que t6 lo re-inyecte al Ejecutor.

    Devuelve `None` cuando:
      - no hay marker (camino `complete`, texto vacío, o el LLM omitió
        el bloque por error);
      - el JSON no parsea;
      - el payload pasa `_parse_verifier` y `_validate_verifier_verdict_payload`
        pero el `verdict_hint` (si viene) discrepa;
      - cualquier clave obligatoria del schema falta o es del tipo
        incorrecto.

    Es deliberadamente silenciosa: cualquier inconsistencia se loggea
    en `usage["plan_correction_error"]` desde el call-site, no acá —
    el parser no debe tirar excepciones para no romper el lazo del
    runner cuando el LLM emite un bloque malformado.
    """
    if not text:
        return None
    m = _VERDICT_FENCE_RE.search(text)
    if not m:
        return None
    try:
        payload = json.loads(m.group("body").strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    err = _validate_verifier_verdict_payload(payload)
    if err is not None:
        return None
    if verdict_hint is not None and payload["verdict"] != verdict_hint:
        return None
    return payload["plan_correction"]


# ---------- interrupción del Ejecutor → Planificador (Etapa B, t2) ----------
#
# Convención de marker (ver `schemas/executor_interruption.json`):
#   * canónica — bloque fenced cuyo fence de apertura lleva el token
#     EXECUTOR_INTERRUPTION, p.ej.
#         ```EXECUTOR_INTERRUPTION
#         {"reason": "compile_fail", ...}
#         ```
#   * fallback — una sola línea que arranca con el marcador y trae el
#     JSON al lado:
#         <<INTERRUPT:EXECUTOR>>{"reason": "compile_fail", ...}
#
# El Ejecutor está obligado a emitir la canónica (lo pide el prompt).
# El fallback existe para el caso degradado en que el Ejecutor no puede
# cerrar el fence (límite de tokens truncó la salida, log scrapeado, etc.)
# — `schemas/_selfcheck.py` valida que ambos lleguen al mismo payload.

_INTERRUPTION_FENCE_RE = re.compile(
    r"```EXECUTOR_INTERRUPTION\s*\n(?P<body>.*?)\n\s*```",
    re.DOTALL | re.IGNORECASE)
# El fallback del schema es estricto (`^<<INTERRUPT:EXECUTOR>>\\s*({.*})`),
# pero el texto real puede traer ruido antes/después (markdown, prefijos
# del modelo). Se busca el ancla en cualquier posición y se captura el
# primer {...} balanceado que viene después.
_INTERRUPTION_LINE_RE = re.compile(
    r"<<INTERRUPT:EXECUTOR>>\s*(\{.*\})\s*$", re.IGNORECASE)
# Campos requeridos por el schema. La validación es best-effort — un JSON
# que parsea pero le falta una clave cae a None, igual que si no hubiera
# señal, para no romper el lazo del Ejecutor.
_INTERRUPTION_REQUIRED = (
    "reason", "affected_step_id", "error_context",
    "partial_progress", "timestamp")
_VALID_REASONS = {
    "compile_fail", "type_mismatch", "file_read_error",
    "wrong_assumption", "other"}


def _parse_executor_interruption(output: str) -> Optional[dict]:
    """Salida del Ejecutor → señal de interrupción parseada, o None.

    Busca primero el bloque fenced canónico, después el fallback de una
    línea, y devuelve el dict si cumple los 5 campos requeridos con
    tipos básicos razonables. NO valida el schema completo (eso es
    trabajo de `schemas/_selfcheck.py`); este helper es el adaptador
    barato que el lazo del runner necesita para decidir si aborta la
    etapa actual.

    Devuelve `None` cuando:
      - no hay marker,
      - hay marker pero el cuerpo no parsea como JSON,
      - parsea pero le falta un campo requerido,
      - parsea pero `reason` no es uno de los valores del enum.
    """
    if not output:
        return None
    body: Optional[str] = None
    m = _INTERRUPTION_FENCE_RE.search(output)
    if m:
        body = m.group("body").strip()
    else:
        m = _INTERRUPTION_LINE_RE.search(output.strip())
        if m:
            body = m.group(1).strip()
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    for k in _INTERRUPTION_REQUIRED:
        if k not in payload:
            return None
    if payload.get("reason") not in _VALID_REASONS:
        return None
    if not isinstance(payload.get("partial_progress"), dict):
        return None
    return payload


def _verdict_match(text: str):
    # Solo un veredicto explícito puede aprobar; "not complete" no aprueba.
    return (_VERDICT_LINE_RE.search(text) or _VERDICT_ALONE_RE.search(text)
            or _VERDICT_RE.search(text))


def _parse_verifier(text: str) -> tuple[str, str, list[int]]:
    """`(complete|needs_more|off_plan|needs_human, feedback, pasos_cubiertos)`.

    Tolerante con prefijos/markdown. Sin veredicto reconocible devuelve
    `needs_human`, sin acreditar pasos. `_run_verifier` registra el fallo
    de formato para distinguirlo de una verificación que exige intervención.

    Pasos cubiertos: lista de enteros extraída de la línea `PASOS: 1,3,5`
    que pide VERIFIER_INSTRUCTIONS. Sirve de red para los pasos que el
    ejecutor olvidó marcar con `plan_step_done`. Si no hay línea `PASOS:`
    válida, se devuelve lista vacía (la fusión con el ejecutor lo trata
    como "no hay red").

    Bug fix 2026-08-15 (multi-modelo): el veredicto se busca en TRES
    pasadas, de la más específica a la más laxa:

      1. una línea `VERDICT: x` — el contrato que pide el prompt;
      2. una línea que sea SOLO el veredicto;
      3. una mención conservadora: needs_more, off_plan o needs_human.

    La 3 era la única que existía, y con un modelo que razona en voz
    alta antes de contestar ("no es `needs_human`, es `complete`") daba
    el veredicto INVERTIDO. Con MiniMax nunca se notó porque contestaba
    con el formato pedido; desde que las etapas corren en los endpoints
    NIM (2026-08-14) el caso es real. La pasada 3 se conserva como
    último recurso únicamente para pedir revisión, nunca para aprobar.
    """
    text = _clean_stage_output(text)
    if not text:
        return "needs_human", (
            "el verificador no devolvió texto: este run NO fue verificado"), []
    m = _verdict_match(text)
    if not m:
        return "needs_human", ("NO fue verificado: falta un veredicto válido. " + text)[:200], []
    verdict = m.group("v").lower()
    # Cap igual que las otras ramas: sin esto, un modelo que contesta con
    # un ensayo deja el ensayo entero en `chats.stages_json`.
    feedback = text[:200]
    fm = _FEEDBACK_RE.search(text)
    if fm:
        feedback = fm.group("f").strip()[:200]
    elif m:
        # Encontró el verdict pero no la línea FEEDBACK: agarramos lo
        # que viene después del verdict.
        rest = text[m.end():].strip().lstrip(":").strip()
        if rest:
            feedback = rest[:200]
    # Red de pasos cubiertos (P5 de la Etapa B).
    pasos: list[int] = []
    pm = _PASOS_LINE_RE.search(text)
    if pm:
        nums = pm.group("nums") or ""
        # `int("0")` es válido pero un paso "0" no existe; filtramos.
        pasos = [int(x) for x in _NUM_RE.findall(nums) if int(x) > 0]
    return verdict, feedback, pasos


# ---------- memoria del hilo para las etapas auxiliares (2026-08-15) ----------
#
# El agujero que tapa: `_run_planner` no recibía NADA del hilo. En un
# turno de seguimiento ("continúa", "ahora el paso 3") planificaba sobre
# una línea suelta y ese plan se inyectaba igual al ejecutor como bloque
# de system prompt. O sea: el ejecutor, que SÍ recuerda el hilo, recibía
# con voz de sistema una orden armada sin él — la mecánica más directa
# para que un follow-up arranque en otra dirección o re-explore lo ya
# resuelto. Era la limitación conocida de iter 11 (ver ESTADO.md).
#
# No se le pasa el historial entero: el planificador es un turno barato y
# meterle 90k de tool results lo volvería tan caro como el ejecutor —
# además de reintroducir el "lost in the middle" que estamos combatiendo.
# Va un RECAP: los últimos turnos como texto, sin tool calls ni results,
# con presupuesto de caracteres. Es lo mismo que sobrevive a
# `_slim_history`, que es exactamente "lo que el hilo se acuerda".

RECAP_MAX_CHARS = int(os.environ.get("FOURBIS_RECAP_MAX_CHARS", "2400"))
RECAP_MAX_TURNS = int(os.environ.get("FOURBIS_RECAP_MAX_TURNS", "3"))
_RECAP_PART_CAP = 600   # por mensaje: un turno gigante no se come el recap


def _history_recap(messages_json: str, *, max_chars: int = RECAP_MAX_CHARS,
                   max_turns: int = RECAP_MAX_TURNS) -> str:
    """Historial serializado → recap corto en texto para las etapas.

    Devuelve "" si no hay historial o si no se pudo parsear (best-effort
    total: una etapa auxiliar nunca puede romper el run).

    Se lee del JSON crudo, no de `ModelMessagesTypeAdapter`, por lo mismo
    que `_summarize_tool_calls_from_messages`: es más barato y no se ata
    al esquema de pydantic-ai. Se toman los últimos `max_turns` pares
    user/assistant, en orden cronológico, recortando cada mensaje a
    `_RECAP_PART_CAP` y el total a `max_chars` (se descartan los turnos
    MÁS VIEJOS primero: el final del hilo es lo que importa).
    """
    try:
        messages = json.loads(messages_json or "")
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(messages, list):
        return ""

    turns: list[tuple[str, str]] = []   # [(role, text)]
    for m in messages:
        if not isinstance(m, dict):
            continue
        kind = m.get("kind")
        for part in m.get("parts", []) or []:
            if not isinstance(part, dict):
                continue
            pk = part.get("part_kind")
            content = part.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if kind == "request" and pk == "user-prompt":
                turns.append(("usuario", content.strip()[:_RECAP_PART_CAP]))
            elif kind == "response" and pk == "text":
                turns.append(("experto", content.strip()[:_RECAP_PART_CAP]))
    if not turns:
        return ""

    # Un "turno" a los efectos del recap es un mensaje del usuario; se
    # cuentan de atrás para adelante y se corta ahí.
    keep_from = 0
    seen_users = 0
    for i in range(len(turns) - 1, -1, -1):
        if turns[i][0] == "usuario":
            seen_users += 1
            if seen_users > max_turns:
                keep_from = i + 1
                break
    chosen = turns[keep_from:]

    lines: list[str] = []
    total = 0
    for role, text in reversed(chosen):   # de atrás para adelante
        entry = f"{role}: {text}"
        if total + len(entry) > max_chars and lines:
            break
        lines.append(entry)
        total += len(entry)
    if not lines:
        return ""
    return "\n\n".join(reversed(lines))


def _render_tool_calls(executor_result: dict, *, keep: int = 12) -> str:
    """Últimas `keep` tool calls como texto, para verificador/documentador.

    Los argumentos se recortan por entrada para que un `edit_file` con
    un payload grande no infle el prompt de las etapas auxiliares.
    """
    tool_calls = executor_result.get("tool_calls_summary") or []
    lines = []
    # 2026-08-15: si hay más llamadas que `keep`, se dice cuántas. Sin
    # esto el verificador leía 12 llamadas de un run de 40 y juzgaba
    # "no hizo nada" sobre una muestra, sin saber que era una muestra.
    if len(tool_calls) > keep:
        lines.append(
            f"({len(tool_calls)} llamadas en total; se listan las últimas "
            f"{keep})")
    for name, args in tool_calls[-keep:]:
        a = (args or "")
        if len(a) > 120:
            a = a[:120] + "…"
        lines.append(f"- {name}({a})")
    return "\n".join(lines) or "(sin llamadas a herramientas)"


def _head_tail(text: str, *, head: int, tail: int) -> str:
    """Principio + final de un texto largo, con marca de lo omitido.

    Las etapas auxiliares recibían SOLO el final de la respuesta del
    ejecutor (`[-2000:]`), así que en un run largo verificaban y
    documentaban la punta del iceberg — y como la respuesta suele abrir
    con lo que se hizo y cerrar con los detalles, lo que se perdía era
    justo el "qué se hizo". Cortar por los dos lados es la misma
    estrategia que ya usa `_elide_tail` con los tool results.
    """
    s = (text or "").strip()
    if len(s) <= head + tail:
        return s
    omitted = len(s) - head - tail
    return (f"{s[:head]}\n\n…[{omitted} caracteres omitidos del medio]…\n\n"
            f"{s[-tail:]}")


# Señales que el planificador puede emitir en la primera línea.
_TRIVIAL_PREFIX = "TRIVIAL:"
_TOO_LARGE_PREFIX = "DEMASIADO_GRANDE:"
# Cuántas líneas del principio se miran buscando la señal. El prompt pide
# que vaya primera; el margen cubre un encabezado o una línea en blanco
# que el modelo agregue por su cuenta. No es "buscar en todo el texto":
# un plan que MENCIONA "DEMASIADO_GRANDE" en el paso 7 no es una señal.
_SIGNAL_SCAN_LINES = 3
_SIGNAL_RE = re.compile(
    r"^[\s>*_#`-]*(?P<sig>TRIVIAL|DEMASIADO_GRANDE)\s*[:：]", re.IGNORECASE)


def _plan_signal(plan: str) -> str:
    """`"trivial"` | `"too_large"` | `""` según la señal del planificador.

    Bug fix 2026-08-15 (multi-modelo): antes esto era
    `plan.upper().startswith(...)` sobre el texto crudo, así que
    cualquier preámbulo del modelo anulaba la señal — un pedido enorme se
    ejecutaba en vez de descomponerse y uno trivial se exploraba. Con las
    etapas en otro proveedor (2026-08-14) el preámbulo dejó de ser raro.
    Ahora se miran las primeras `_SIGNAL_SCAN_LINES` líneas no vacías del
    texto YA saneado, tolerando viñetas y markdown alrededor.
    """
    lines = [ln for ln in _clean_stage_output(plan).splitlines() if ln.strip()]
    for line in lines[:_SIGNAL_SCAN_LINES]:
        m = _SIGNAL_RE.match(line)
        if m:
            return ("trivial" if m.group("sig").upper() == "TRIVIAL"
                    else "too_large")
    return ""


# Un paso del plan: una línea que arranca con un número. Mismo criterio
# laxo que `_SIGNAL_RE` para el markdown de alrededor (viñetas, negritas,
# citas), porque el planificador corre en modelos distintos y cada uno
# adorna a su manera.
_PASO_RE = re.compile(
    r"^[\s>*_#`•-]*(?:\*\*)?(?:paso\s+)?[1-9][0-9]?\s*[.)\]:]", re.IGNORECASE)


def pasos_del_plan(plan: str) -> list[str]:
    """El plan en prosa → la lista de sus pasos numerados.

    Existe para MOSTRARLO (F3+, 2026-08-23). Hasta hoy el plan del
    planificador se generaba en cada run por etapas, se inyectaba en el
    system prompt del ejecutor y ahí moría: el humano nunca lo veía. Con
    el grafo pasó lo mismo que acá — el dato existía y nadie lo sacaba a
    la superficie.

    Mismo criterio laxo de `_PASO_RE` para el markdown de alrededor
    (viñetas, negritas, citas), porque el planificador corre en modelos
    distintos y cada uno adorna a su manera. Las líneas que NO arrancan
    un paso se pegan al paso anterior: un modelo que parte un paso en
    dos renglones no debería inventar un paso de más.

    Devuelve `[]` si no hay pasos — para una señal (`TRIVIAL:`,
    `DEMASIADO_GRANDE:`) o para lo que salga cuando el modelo se
    descarrila, que es lo que mide `_plan_utilizable`.
    """
    pasos: list[str] = []
    for linea in _clean_stage_output(plan or "").splitlines():
        if not linea.strip():
            continue
        if _SIGNAL_RE.match(linea):
            continue
        if _PASO_RE.match(linea):
            # Se saca la numeración: la UI numera sola, y dejarla dentro
            # del texto daba "1. 1. Leer el esquema" cuando el modelo la
            # escribía con un formato y la lista con otro.
            pasos.append(re.sub(r"^[\s>*_#`•-]*(?:\*\*)?(?:paso\s+)?"
                                r"[1-9][0-9]?\s*[.)\]:]\s*", "", linea,
                                flags=re.IGNORECASE).strip())
        elif pasos:
            pasos[-1] = f"{pasos[-1]} {linea.strip()}".strip()
    return [p for p in pasos if p][:30]


def _plan_utilizable(plan: str) -> bool:
    """¿Esto es un plan, o es lo que salió cuando el modelo se descarriló?

    Un plan tiene pasos numerados; el prompt del planificador los pide
    explícitos. Todo lo demás que llega —y llega seguido— es ruido que
    NO se le puede pasar al ejecutor ni usar de vara para medirlo.

    Por qué existe (medido el 19/8/2026 sobre 156 runs por etapas, todos
    planificados con nemotron-3-ultra): solo 55 traían un plan. Los otros
    101 eran, en este orden de frecuencia:

      - una tool call escrita como TEXTO, que es el plan entero:
        `cbm_query({"tool": "search_graph", "name_pattern": "docker-compose"})`
      - prosa con el JSON incrustado a mitad de frase:
        `...antes de crear el{"tool": "cbm_query", "args": {...`
      - volcados de razonamiento (`Thought 1: The user wants...`), la
        palabra `ponytail` suelta, o texto vacío.

    Lo caro no era el plan perdido —el ejecutor sabe trabajar sin plan—
    sino que el VERIFICADOR lo tomaba como el contrato a cumplir y
    cortaba el run por `off_plan` a mitad de camino. En sample-shop frenó
    runs en los pasos 78, 100, 106 y 150 mientras el ejecutor hacía
    exactamente lo que el humano había pedido.

    Las señales (`TRIVIAL:` / `DEMASIADO_GRANDE:`) son planes válidos sin
    pasos numerados, y las resuelve el caller ANTES de llamar acá.
    """
    lineas = [ln for ln in _clean_stage_output(plan).splitlines() if ln.strip()]
    return any(_PASO_RE.match(ln) for ln in lineas)


def _format_decomposition(plan: str) -> str:
    """`DEMASIADO_GRANDE: ...` → mensaje de propuesta para el humano.

    Conserva el encuadre del planificador de prompts grandes que vivía
    en server.py (Opción 4): deja claro que NO se ejecutó nada y pide
    que el humano elija por dónde empezar.
    """
    body = _clean_stage_output(plan)
    # La señal puede no estar en el primer caracter (ver `_plan_signal`):
    # se saca de la línea donde esté, dejando lo que venga después.
    out = []
    dropped = False
    for i, line in enumerate(body.splitlines()):
        m = (_SIGNAL_RE.match(line)
             if not dropped and i < _SIGNAL_SCAN_LINES else None)
        if m:
            dropped = True
            rest = line[m.end():].strip()
            if rest:
                out.append(rest)
            continue
        out.append(line)
    body = "\n".join(out).strip()
    return (
        "📋 Este pedido es grande, así que primero lo **descompuse en "
        "subtareas** (todavía no ejecuté nada):\n\n"
        f"{body}\n\n"
        "Dime por cuáles empiezo (por ejemplo *\"empieza con la 1 y la 2\"*) "
        "y las hago una por una, o ejecuta el set completo como "
        "**night run**."
    )


async def _reasoning_toolset(db: Any, project: dict, pool: Any) -> list[Any]:
    """El MCP de `reasoning` (sequential-thinking) para el planificador.

    Devuelve `[]` cuando no está en el catálogo, no levanta, o falta `npx`
    en el PATH. Degradación limpia a propósito: el planificador tiene que
    seguir funcionando en una máquina sin la toolchain de Node, solo con
    un turno de razonamiento menos.

    Por qué SOLO al planificador y no al ejecutor: la decisión que se
    quiere mejorar es "¿esto es una tarea o son cinco?", y esa se toma una
    vez, antes de tocar nada. El ejecutor ya tiene el repo entero para
    razonar contra evidencia; el planificador solo tiene el texto del
    pedido, y ahí un paso de pensamiento explícito es lo que separa
    "partir de 0 en un SampleApp nuevo" leído como UNA tarea de leerlo como
    las cinco que era.
    """
    from .execution_policy import ExecutionPolicy
    if db is None or pool is None or not ExecutionPolicy.for_run(
            project.get("defaults_json") or {}).unrestricted_tools:
        return []
    pid = project.get("id")
    if pid is None:
        # Un proyecto sin id no puede consultar el catálogo. Pasa con los
        # dobles de test y con los dicts armados a mano; no es un error
        # que valga un warning.
        return []
    try:
        rows = await db.mcp_servers_for_project(
            pid, capabilities=["reasoning"])
    except Exception as e:  # noqa: BLE001
        logger.warning("planner reasoning: no pude leer el catálogo (%r)", e)
        return []
    for row in rows:
        if row.get("transport") != "stdio":
            continue
        try:
            toolset = await pool.acquire(row, project.get("repo_path") or "")
            if toolset is None:
                logger.info(
                    "planner reasoning: %r no levantó — planifico sin él",
                    row["name"])
                continue
            # Capeado como cualquier otro MCP: un tool call colgado del
            # razonador no puede comerse el timeout de la etapa entera.
            return [OptionalToolset(wrapped=CappedToolset(
                wrapped=toolset, timeout=config.tool_timeout_s(),
                inflight={}))]
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "planner reasoning: %r falló al armarse (%r)", row["name"], e)
    return []


# ---------- ¿Este pedido retoma, o abre algo nuevo? (2026-08-23) ----------
#
# Hasta hoy `DEMASIADO_GRANDE:` estaba prohibido en TODO follow-up, y el
# guard se leía razonable: proponer una descomposición arriba de un
# "sigue con la 2" es secuestrar la conversación. El costo real, medido:
# `task_graphs` estaba vacía — el grafo no se armó NUNCA, porque todo
# pedido que entra por un hilo abierto es follow-up. Un pedido nuevo y
# grande en un hilo que ya existe es justo el caso para el que se
# construyó el grafo, y se ejecutaba igual hasta morir por `off_plan` o
# por presupuesto (sample-shop, 23/8: 2,79 M tokens en dos runs, cero salida
# usable).
#
# Lo que el guard quería proteger no era "el hilo tiene historial", era
# "este mensaje retoma lo anterior". Eso se lee del mensaje, no del hilo.

#: Los nudges que escribe el propio harness al retomar (el de presupuesto
#: y el del verificador). Son continuaciones por definición y son largos,
#: así que van por prefijo exacto y no por el tope de abajo.
_NUDGES_DEL_HARNESS = ("continúa la tarea.", "continúa con la tarea")

#: Tope para el "continúa" que escribe un humano. Un pedido nuevo lo
#: bastante grande como para partirse en grafo no entra acá; esto separa
#: "seguí" de "Seguí el flujo de checkout y documentá cada pantalla…".
_CONTINUACION_MAX_CHARS = 80

_CONTINUACION_RE = re.compile(
    r"^[\s>*_#`-]*(?:s[ií]|ok|dale|listo|perfecto|bien)?[\s,.:;]*"
    r"(?:contin[uú]|sigu[eé]|sigue|seg[uú][ií]|retom|prosegu)",
    re.IGNORECASE)


def _es_continuacion(user: str) -> bool:
    """¿El pedido retoma lo que venía en curso?

    Solo se consulta cuando ya hay hilo: sin historial no hay nada que
    retomar. Ante la duda devuelve False —o sea, "es un pedido nuevo"—
    porque el error caro es el otro: un pedido grande que no se parte en
    grafo se come el presupuesto entero y no entrega nada.
    """
    t = (user or "").strip()
    if t.lower().startswith(_NUDGES_DEL_HARNESS):
        return True
    return (len(t) <= _CONTINUACION_MAX_CHARS
            and bool(_CONTINUACION_RE.match(t)))


def _stage_timeout(limit: float, deadline: Optional[float]) -> float:
    return limit if deadline is None else max(0.0, min(limit, deadline - time.monotonic()))


async def _run_planner(
    *, user: str, project: dict, model_spec: str,
    ponytail: str, on_progress: Any = None, is_followup: bool = False,
    history_recap: str = "", toolsets: Optional[list] = None,
    es_continuacion: bool = False, deadline: Optional[float] = None,
) -> tuple[str, dict, str]:
    """Un turno breve con el modelo económico: prompt → (plan, usage, error).

    `usage` son los tokens de ESTA etapa (ver `_stage_usage`): desde que
    cada etapa puede correr en un proveedor distinto, atribuirlos al
    ejecutor hacía que el dashboard reportara todo el consumo como si
    fuera del modelo pagado. `error` es "" cuando la etapa corrió: si
    trae algo, el ejecutor va a correr SIN plan y eso tiene que quedar
    auditable (2026-08-15) en vez de vivir solo en un WARNING del log.

    Sin herramientas, sin MCPs, sin cbm_query: el planificador NO toca
    el repositorio. Si devuelve "TRIVIAL: ...", el ejecutor recibe el
    plan como system_extra igual; es solo una señal para la UI ("esta
    tarea no necesitaba planificación").

    `history_recap` (2026-08-15) es el resumen corto de los últimos
    turnos del hilo — ver `_history_recap`. Antes de eso el planificador
    no veía nada de la conversación y en cada follow-up planificaba desde
    cero, contra un ejecutor que sí la recordaba.
    """
    spec = model_spec or config.planner_model_spec()
    pony = (ponytail or "").strip()
    sysp = (project.get("system_prompt") or "").strip()
    instructions = "\n\n".join(p for p in (pony, PLANNER_INSTRUCTIONS) if p)
    # La regla del razonador entra solo si el razonador está. Ver el
    # comentario de `PLANNER_REASONING_RULE`: pedirle que use una tool que
    # no tiene es lo que hacía que el intento de llamada saliera como
    # texto y el "plan" fuera un tool call serializado.
    if toolsets:
        instructions = f"{instructions}\n{PLANNER_REASONING_RULE}"
    # Ponemos el system_prompt del proyecto al final: el planner debe
    # conocer la "voz" del experto, pero sus instrucciones mandan.
    if sysp:
        instructions = f"{instructions}\n\n{sysp}"

    if on_progress is not None:
        try:
            await on_progress(phase="planner", tool=None, message=spec)
        except Exception:  # noqa: BLE001 — callback best-effort
            pass

    project_ctx = (
        f"## Contexto del proyecto\n"
        f"Slug: `{project.get('slug', '?')}`\n"
        f"Repo: `{project.get('repo_path', '?')}`\n"
    )
    # Retomar ("sigue con la 2") y pedir algo nuevo dentro de un hilo son
    # cosas distintas: en lo primero, proponer una descomposición
    # secuestra la conversación en vez de avanzarla; en lo segundo es
    # exactamente lo que hay que hacer. Ver `_es_continuacion`.
    if es_continuacion:
        project_ctx += (
            "Este pedido RETOMA algo que ya estaba en curso: planifica\n"
            "el siguiente paso y no uses `DEMASIADO_GRANDE:`.\n"
        )
    elif is_followup:
        project_ctx += (
            "Este pedido llega en un hilo que ya venía, pero abre algo\n"
            "NUEVO: juzga su tamaño por sí mismo, sin darlo por chico\n"
            "porque la conversación ya existía.\n"
        )
    # Memoria del hilo (2026-08-15). Va ANTES del pedido y con el encuadre
    # explícito de que es contexto, no la tarea: sin esa aclaración el
    # planificador tiende a re-planificar el turno viejo que está leyendo.
    recap_block = ""
    if history_recap.strip():
        recap_block = (
            "\n## Turnos previos del hilo (contexto, NO es el pedido)\n"
            "Resumen recortado de la conversación hasta acá. Úsalo para no\n"
            "re-planificar lo ya hecho y para entender a qué se refiere el\n"
            "pedido cuando dice \"eso\", \"continúa\" o \"el paso 3\".\n\n"
            f"{history_recap.strip()}\n"
        )
    prompt = (f"{project_ctx}{recap_block}\n"
              f"## Pedido del usuario\n{user.strip()}")
    # build_model va DENTRO del try: si queda afuera, un fallo al armar
    # el modelo que no sea ModelUnavailable escapa y mata el run entero,
    # justo lo que este best-effort quiere evitar.
    try:
        agent = Agent(
            build_model(spec), instructions=instructions,
            tool_timeout=config.tool_timeout_s(),
            toolsets=list(toolsets or []),
        )
        # Con el razonador enchufado el turno deja de ser "una pregunta,
        # una respuesta": son varias vueltas de pensamiento antes de
        # escribir el plan, y 60s no alcanzan.
        limite = _PV_REASONING_TIMEOUT_S if toolsets else _PV_TIMEOUT_S
        result = await asyncio.wait_for(agent.run(prompt), timeout=_stage_timeout(limite, deadline))
        return _clean_stage_output(result.output or ""), _stage_usage(result), ""
    except ModelUnavailable:
        # El planner Y el ejecutor comparten la misma falta de key
        # cuando el modelo es el mismo: si el planner propaga, el
        # ejecutor también lo haría. Mejor propagar al server una sola
        # vez que terminar con un ejecutor que va a fallar igual, y
        # dejar el chat en un limbo que el test_end_to_end confunde con
        # "ok porque respondió algo" (caso real: el patch de os.environ
        # del test se cierra ANTES de que corra el background task →
        # build_model ve la key real → el LLM responde de verdad).
        raise
    except Exception as e:  # noqa: BLE001 — planner best-effort
        # 2026-08-15: además del WARNING, el error viaja de vuelta. Un
        # planificador caído (429 del free tier, timeout de 60s) dejaba al
        # ejecutor corriendo a ciegas sin que quedara rastro fuera del log:
        # el turno se veía idéntico a uno planificado. Ahora se persiste en
        # `chats.stages_json` y se emite como evento de progreso.
        err = f"{type(e).__name__}: {e}"[:200]
        logger.warning("planner falló (%r) — ejecutor corre sin plan", e)
        if on_progress is not None:
            try:
                await on_progress(phase="planner", tool=None,
                                  message=f"sin plan ({err})")
            except Exception:  # noqa: BLE001 — callback best-effort
                pass
        return "", {}, err


async def _run_verifier(
    *, user: str, plan: str, executor_result: dict,
    model_spec: str, ponytail: str, on_progress: Any = None, deadline: Optional[float] = None,
) -> tuple[str, str, dict, str, list[int]]:
    """Un turno breve con el modelo económico: plan + resultado → veredicto.

    Devuelve `(verdict, feedback, usage, error, pasos_verificados)`;
    `usage` son los tokens de esta etapa (ver `_stage_usage`), `{}` si
    no se pudieron medir, `error` es "" salvo que la etapa se haya
    caído (2026-08-15: queda en `chats.stages_json` para poder separar
    "el trabajo estaba mal" de "el verificador no corrió"), y
    `pasos_verificados` es la lista de pasos del plan que el verificador
    vio como cubiertos (Etapa B de GRAFO_SIEMPRE_PLAN.md).

    Nunca rompe el run: si el verificador falla, devuelve
    `needs_human` con el error como feedback. NO devuelve `complete`
    —eso aprobaría en silencio un run que nadie revisó—, y tampoco
    `needs_more`, que haría reintentar al ejecutor por una caída que
    no tiene nada que ver con la calidad del trabajo.
    """
    spec = model_spec or config.verifier_model_spec()
    pony = (ponytail or "").strip()
    instructions = "\n\n".join(p for p in (pony, VERIFIER_INSTRUCTIONS) if p)

    if on_progress is not None:
        try:
            await on_progress(phase="verifier", tool=None, message=spec)
        except Exception:  # noqa: BLE001
            pass

    tools_text = _render_tool_calls(executor_result)
    # Evidencia dura, del `bitacora_json` que el ejecutor ya devuelve:
    # comandos con su exit code, pasos marcados y hechos anotados. Sin
    # esto el verificador solo veía qué tools se LLAMARON, o sea que no
    # podía separar "corrió las pruebas" de "las pruebas pasaron".
    # Se deriva acá adentro y no se pasa por parámetro para que los dos
    # call sites —el supervisor de media corrida y el de cierre— la
    # tengan sin tocar sus llamadas.
    evidencia = ""
    try:
        raw = executor_result.get("bitacora_json") or ""
        if raw:
            evidencia = Bitacora.cargar(raw).evidencia(
                max_chars=4000 if executor_result.get("graph_id") else 1500)
    except Exception:  # noqa: BLE001 — sin evidencia se verifica peor, no se rompe
        logger.warning("verificador: no pude armar la evidencia", exc_info=True)
    prompt = (
        f"## Pedido del usuario\n{user.strip()[:2000]}\n\n"
        f"## Plan\n{(plan or '(sin plan: tarea trivial o el planificador falló)').strip()[:2000]}\n\n"
        f"## Estado del run\n{executor_result.get('phase_at_end', '?')}\n\n"
        f"## Herramientas ejecutadas\n{tools_text}\n\n"
        f"## Evidencia\n"
        f"{evidencia or '(el run no dejó comandos ni hechos anotados)'}\n\n"
        + ("En este grafo, usa la comprobación más reciente de cada alcance; "
           "un fallo anterior o de un comando auxiliar no invalida una "
           "comprobación posterior. Los recortes y resultados sin correlación "
           "son límites de evidencia. Un exit=0 no demuestra por sí solo que "
           "se cumplió el contrato solicitado.\n\n"
           if executor_result.get("graph_id") else "")
        + f"## Resultado del ejecutor\n"
        f"{_head_tail(executor_result.get('content') or '', head=1200, tail=1800)}"
    )

    # build_model va dentro del try por lo mismo que en _run_planner.
    try:
        agent = Agent(
            build_model(spec), instructions=instructions,
            tool_timeout=config.tool_timeout_s(),
        )
        result = await asyncio.wait_for(agent.run(prompt), timeout=_stage_timeout(_PV_TIMEOUT_S, deadline))
        verifier_text = str(result.output or "")
        verdict, feedback, _pasos_verificados = _parse_verifier(verifier_text)
        # Corrección dinámica del plan (Etapa B, t5). Solo se publica
        # cuando el veredicto EXIGE re-trabajo (needs_more u off_plan):
        # el camino `complete` no debe contaminar `stages_json` con
        # payloads vacíos, y re-ejecuciones del mismo diff contra el
        # mismo veredicto deben dar el mismo estado de usage.
        # Idempotente: si el LLM no emitió el bloque fenced
        # VERIFIER_VERDICT, el helper devuelve None y usage queda sin
        # la clave (no se mete un `null` que después se confunda con
        # "había corrección y se rompió al validar"). Lo mismo si
        # falla `_validate_verifier_verdict_payload` — en ese caso
        # sí registramos el motivo en `usage["plan_correction_error"]`
        # para que t6 (orquestador) decida si reintenta o descarta.
        usage = _stage_usage(result)
        if verdict in ("needs_more", "off_plan"):
            pc = _extract_plan_correction(verifier_text, verdict_hint=verdict)
            if pc is not None:
                usage = {**usage, "plan_correction": pc}
            else:
                # El bloque estaba y falló, o no estaba. Distinguimos
                # los dos casos: si el regex encontró el fence pero
                # la validación tiró, lo decimos; si directamente no
                # había fence, no es error (el LLM puede haber emitido
                # solo el bloque de prosa).
                if _VERDICT_FENCE_RE.search(verifier_text):
                    usage = {
                        **usage,
                        "plan_correction_error": (
                            "bloque VERIFIER_VERDICT presente pero invalido"),
                    }
        return (
            verdict, feedback, usage,
            "" if _verdict_match(_clean_stage_output(verifier_text)) else "veredicto ausente o inválido",
            list(_pasos_verificados or []),
        )
    except ModelUnavailable:
        # Mismo razonamiento que el planificador: si el verificador no
        # puede armar su modelo, se propaga. El ejecutor ya corrió bien;
        # el caller (server) prefiere enterarse de un ModelUnavailable
        # único a recibir un content "completo" con un verificador que
        # no pudo verificar.
        raise
    except Exception as e:  # noqa: BLE001 — el verificador no debe romper el run
        # 2026-08-14: antes esto devolvía `complete`. Con MiniMax pagado
        # casi nunca se activaba; desde que las etapas corren en los
        # endpoints GRATIS de NVIDIA (429 / timeout son esperables), un
        # verificador caído aprobaba el run en silencio — justo el caso
        # en que menos se sabe si el trabajo está bien.
        # `needs_human` es el mismo estado que usa `run_expert_staged`
        # cuando el ejecutor termina roto: no rompe el run, pero le
        # antepone el aviso ⚠️ al content para que el humano mire.
        logger.warning("verificador falló (%r): needs_human, run sin verificar", e)
        if on_progress is not None:
            try:
                await on_progress(phase="verifier", tool=None,
                                  message=f"sin verificar ({type(e).__name__})")
            except Exception:  # noqa: BLE001 — callback best-effort
                pass
        return "needs_human", (
            f"el verificador no pudo correr ({type(e).__name__}): este run "
            f"NO fue verificado, revisá el resultado a mano"), {}, (
            f"{type(e).__name__}: {e}"[:200]), []


async def _run_documenter(
    *, user: str, plan: str, executor_result: dict, verdict: str,
    feedback: str, model_spec: str, ponytail: str, on_progress: Any = None, deadline: Optional[float] = None,
) -> tuple[str, dict, str]:
    """Un turno breve de redacción: run → (registro, usage, error).

    Devuelve el bloque markdown que `run_expert_staged` agrega al
    `content`. Best-effort: si falla, devuelve "" y el run sigue con la
    respuesta del ejecutor tal cual — documentar nunca debe romper un
    run que ya terminó bien. `error` queda en `chats.stages_json`
    (2026-08-15) para distinguir "no había nada que documentar" de "el
    documentador se cayó".
    """
    spec = model_spec or config.documenter_model_spec()
    pony = (ponytail or "").strip()
    instructions = "\n\n".join(p for p in (pony, DOCUMENTER_INSTRUCTIONS) if p)

    if on_progress is not None:
        try:
            await on_progress(phase="documenter", tool=None, message=spec)
        except Exception:  # noqa: BLE001
            pass

    tools_text = _render_tool_calls(executor_result)
    prompt = (
        f"## Pedido del usuario\n{user.strip()[:2000]}\n\n"
        f"## Plan\n{(plan or '(sin plan)').strip()[:2000]}\n\n"
        f"## Herramientas ejecutadas\n{tools_text}\n\n"
        f"## Veredicto del verificador\n{verdict}: {feedback}\n\n"
        f"## Respuesta final del ejecutor\n"
        f"{_head_tail(executor_result.get('content') or '', head=1500, tail=2500)}"
    )

    # build_model va dentro del try por lo mismo que en _run_planner.
    try:
        agent = Agent(
            build_model(spec), instructions=instructions,
            tool_timeout=config.tool_timeout_s(),
        )
        result = await asyncio.wait_for(agent.run(prompt), timeout=_stage_timeout(_PV_TIMEOUT_S, deadline))
        return _clean_stage_output(result.output or ""), _stage_usage(result), ""
    except ModelUnavailable as e:
        # A diferencia del planificador y del verificador, aquí NO se
        # propaga: el ejecutor ya terminó y su respuesta es válida.
        # Perder el registro del cambio no justifica marcar el run como
        # fallido y hacer que el humano lo repita.
        logger.warning("documentador sin modelo disponible: sigo sin registro")
        return "", {}, f"ModelUnavailable: {e}"[:200]
    except Exception as e:  # noqa: BLE001 — documentador best-effort
        logger.warning("documentador falló (%r): sigo sin registro", e)
        return "", {}, f"{type(e).__name__}: {e}"[:200]


# La marca existe porque el registro, anexado crudo al texto del
# asistente, es indistinguible de algo que escribió el experto — así que
# al turno siguiente el modelo lo imita, y encima el documentador le
# anexa uno nuevo. Medido en sample-shop el 17/8: los bloques "Qué se hizo /
# Archivos tocados / Cómo verificarlo / Pendiente" crecían de a uno por
# turno dentro de la misma conversación (2 → 3 → 4 → 5) hasta que la
# respuesta eran cinco registros casi idénticos y ninguna respuesta.
#
# Va SOLO en el historial: el `content` que lee el humano se queda
# limpio, que para eso el documentador escribe en prosa.
_DOC_MARCA = "[registro automático del relay — NO lo reproduzcas]"
_DOC_SEP = f"\n\n---\n\n{_DOC_MARCA}\n"


def _merge_doc_into_history(messages_json: str, doc: str) -> str:
    """Mete el registro del documentador en el último texto del historial.

    Por qué (2026-08-15): el bloque del documentador se concatenaba al
    `content` que ve el humano pero NO al `messages_json` que se replaya
    al turno siguiente. Resultado: vos leías "Archivos tocados: X ·
    Pendiente: Y" y al pedir "seguí con el pendiente que anotaste" el
    experto no tenía ese texto. Un desajuste entre lo que ve el humano y
    lo que ve el modelo, en el único artefacto que la capa 3
    (`_slim_history`) deja cruzar el user prompt.

    Se ANEXA al último `TextPart` del último `ModelResponse` en vez de
    agregar un response nuevo, justamente por `_slim_history`: de cada
    turno viejo sobrevive un solo response, así que un mensaje aparte se
    llevaría puesta la respuesta real del ejecutor.

    Best-effort: ante cualquier problema devuelve el historial original.
    """
    if not (doc or "").strip() or not (messages_json or "").strip():
        return messages_json
    try:
        messages = list(ModelMessagesTypeAdapter.validate_json(messages_json))
        for m in reversed(messages):
            if not isinstance(m, ModelResponse):
                continue
            texts = [p for p in m.parts if isinstance(p, TextPart)]
            if not texts:
                # Un response sin texto (solo tool calls) no es el cierre
                # del turno: seguimos buscando hacia atrás.
                continue
            # Si ya hay un registro de un turno anterior en este texto, se
            # REEMPLAZA en vez de apilarse: dos registros en el historial
            # son dos ejemplos del formato a imitar, no el doble de
            # contexto útil. Idempotente por el mismo camino.
            previo = texts[-1].content or ""
            corte = previo.find(_DOC_SEP)
            if corte != -1:
                previo = previo[:corte]
            texts[-1].content = f"{previo.rstrip()}{_DOC_SEP}{doc.strip()}"
            return ModelMessagesTypeAdapter.dump_json(messages).decode("utf-8")
        return messages_json
    except Exception as e:  # noqa: BLE001 — nunca romper por documentar
        logger.warning("no pude anexar el registro al historial (%r)", e)
        return messages_json


async def run_expert_staged(
    project: dict, user: str, **kwargs: Any,
) -> dict:
    """Wrapper por etapas sobre `run_expert`.

    Etapas: planificador → ejecutor → verificador → documentador.

    Acepta los mismos kwargs que `run_expert` (skills_block,
    system_extra, model_override, db, message_history_json, on_progress,
    steer, rescue, mcp_with, mcp_pool, images) y devuelve su dict de
    resultado extendido con:
      - `plan`: el plan del planificador ("" si fue trivial o falló)
      - `planner_model`: spec del modelo del planificador
      - `verifier_verdict`: `complete` | `needs_more` | `needs_human`
      - `verifier_feedback`: una línea del verificador
      - `verifier_model`: spec del modelo del verificador
      - `doc`: el registro del cambio ("" si se omitió o falló)
      - `documenter_model`: spec del modelo del documentador
      - `three_stage`: True (bandera de diagnóstico; nombre heredado)
      - `stage_errors`: `{etapa: error}` de las etapas que se cayeron
        (2026-08-15). Solo lleva las que fallaron: dict vacío = todas
        corrieron.

    El verificador se omite si `phase_at_end` quedó en (`error`,
    `timeout`, `hard_timeout`, `idle_timeout`, `cancelled`): no tiene
    sentido verificar un run roto. El documentador se omite en esos
    mismos casos y además cuando no hubo llamadas a herramientas o el
    plan fue TRIVIAL, porque no habría nada que registrar.

    Opt-out: `defaults_json.three_stage=False` (o `three_stage=False`
    por kwargs) cae al `run_expert()` de un solo turno. El documentador y
    el verificador tienen su propio interruptor,
    `defaults_json.documenter=False` y `defaults_json.verifier=False`.

    Costo: hasta TRES turnos extra por run. Con un modelo económico son
    ~1-3s cada uno, menos del 10% en runs de 30s+. Pero si las
    variables FOURBIS_*_MODEL quedan vacías, estas etapas corren con el
    MISMO modelo pesado del ejecutor y el costo deja de ser marginal:
    conviene fijarlas explícitas en el .env.

    Memoria del hilo (2026-08-15): el planificador recibe un recap corto
    de los últimos turnos (`_history_recap`). Antes no recibía NADA del
    hilo, así que en cada follow-up planificaba desde cero y ese plan se
    le inyectaba al ejecutor —que sí recordaba— como orden de sistema.
    Era la limitación conocida de iter 11.
    """
    defaults = project.get("defaults_json") or {}
    # Una captura de la primera ronda sigue perteneciendo al pedido
    # aunque la última ronda solo escriba la respuesta.
    kwargs.setdefault("image_artifacts", {})
    limite_pedido = min(kwargs.get("deadline_pedido") or float("inf"),
        time.monotonic() + float(defaults.get("request_timeout") or config.expert_request_timeout_s()))
    kwargs["deadline_pedido"] = limite_pedido
    # Modelo por rol para ESTE run (2026-08-26). El pop va ACÁ y no
    # junto a su uso: `run_expert` no tiene `**kwargs`, así que un
    # `stage_models` que sobreviva al opt-out de abajo lo mata con
    # TypeError — justo en los proyectos con `three_stage=false`, que
    # son los que menos tienen que ver con esto.
    etapas = kwargs.pop("stage_models", None) or {}
    # Opt-out: lo explícito por kwargs gana sobre defaults_json.
    enabled = kwargs.pop("three_stage", None)
    if enabled is None:
        enabled = defaults.get("three_stage", True)
    doc_enabled = kwargs.pop("documenter", None)
    if doc_enabled is None:
        doc_enabled = defaults.get("documenter", True)
    # Interruptor propio del verificador (2026-08-15), simétrico al del
    # documentador. Hasta ahora la única forma de apagarlo era apagar el
    # runner entero: un proyecto que no quiere pagar el turno de
    # verificación tenía que renunciar también al plan.
    ver_enabled = kwargs.pop("verifier", None)
    if ver_enabled is None:
        ver_enabled = defaults.get("verifier", True)
    if not enabled:
        # Un solo turno. Se marca el dict igual para que la UI sepa que
        # el proyecto optó por no planificar.
        result = await run_expert(project, user, **kwargs)
        result["three_stage"] = False
        result["plan"] = ""
        result["verifier_verdict"] = ""
        result["verifier_feedback"] = ""
        result["planner_model"] = ""
        result["verifier_model"] = ""
        result["doc"] = ""
        result["documenter_model"] = ""
        result["stage_errors"] = {}
        return result

    on_progress = kwargs.get("on_progress")
    ponytail = await read_ponytail()
    # Modelo por rol para ESTE run (2026-08-26). Gana sobre el default
    # del proyecto, que gana sobre la cascada global. Es el mismo orden
    # que ya tenía el ejecutor con `model_override`: lo puntual manda
    # sobre lo permanente, y no deja nada escrito.
    planner_spec = (etapas.get("planner") or defaults.get("planner_model")
                    or config.planner_model_spec())
    verifier_spec = (etapas.get("verifier") or defaults.get("verifier_model")
                     or config.verifier_model_spec())
    documenter_spec = (etapas.get("documenter")
                       or defaults.get("documenter_model")
                       or config.documenter_model_spec())

    # 1) PLANIFICADOR
    history_json = kwargs.get("message_history_json") or ""
    is_followup = bool(history_json)
    # Retomar ≠ pedir algo nuevo en un hilo que ya existe. Ver
    # `_es_continuacion`: es lo que decide si `DEMASIADO_GRANDE:` puede
    # armar un grafo o si secuestraría la conversación.
    es_continuacion = is_followup and _es_continuacion(user)
    stage_errors: dict[str, str] = {}
    # Razonador para la decisión de descomponer (2026-08-17). Se salta
    # solo cuando el pedido RETOMA: ahí `DEMASIADO_GRANDE:` está
    # prohibido, así que pensar sobre esa decisión sería pagar por una
    # pregunta que ya está contestada. En un pedido nuevo el razonador va
    # aunque el hilo venga de antes — que es justo donde hacía falta.
    planner_toolsets: list[Any] = []
    if defaults.get("planner_reasoning", True) and not es_continuacion:
        planner_toolsets = await _reasoning_toolset(
            kwargs.get("db"), project, kwargs.get("mcp_pool"))
    plan, planner_usage, planner_err = await _run_planner(
        user=user, project=project, model_spec=planner_spec,
        ponytail=ponytail, on_progress=on_progress,
        is_followup=is_followup, es_continuacion=es_continuacion,
        history_recap=_history_recap(history_json),
        toolsets=planner_toolsets,
        deadline=limite_pedido,
    )
    if planner_err:
        stage_errors["planner"] = planner_err
    # El plan, disponible YA (2026-08-23). `finish_chat` lo guarda igual
    # al terminar, pero el panel del chat lo quiere mientras el run
    # corre: en un run de diez minutos, un plan que aparece al final no
    # sirve para saber en qué anda. Best-effort — que no se pueda
    # guardar no puede voltear el run.
    if (_db := kwargs.get("db")) is not None and kwargs.get("chat_id") \
            and plan.strip():
        try:
            await _db.set_chat_stages(kwargs["chat_id"], json.dumps(
                {"plan": plan[:4000], "planner_model": planner_spec},
                ensure_ascii=False))
        except Exception as _e:  # noqa: BLE001
            logger.debug("no pude adelantar el plan a la DB: %r", _e)
    signal = _plan_signal(plan)
    trivial = signal == "trivial"

    # 1a) Guard del plan basura (2026-08-19). Ver `_plan_utilizable` para
    # qué llega y con qué frecuencia. Descartarlo es tratarlo igual que a
    # un planificador caído, camino que ya existe y está probado: sin
    # plan, el `plan_block` no se inyecta y el supervisor de media
    # corrida no se instala (`if ver_enabled and plan.strip()`), así que
    # NO se puede cortar por `off_plan` contra algo que no era un plan.
    # El ejecutor corre como si nadie hubiera planificado, que es
    # exactamente lo que pasó.
    #
    # Va DESPUÉS de `_plan_signal` a propósito: `TRIVIAL:` y
    # `DEMASIADO_GRANDE:` son salidas válidas que no llevan pasos
    # numerados, y pasarlas por acá las tiraría a la basura.
    if not signal and plan.strip() and not _plan_utilizable(plan):
        logger.warning(
            "planner (%s) devolvió algo que no es un plan (%d chars) — "
            "el ejecutor corre sin plan y sin supervisor: %r",
            planner_spec, len(plan), plan[:160])
        stage_errors.setdefault(
            "planner", "plan descartado: la salida no tiene pasos numerados")
        plan = ""
        if on_progress is not None:
            try:
                await on_progress(phase="planner", tool=None,
                                  message="sin plan (salida no utilizable)")
            except Exception:  # noqa: BLE001 — callback best-effort
                pass

    # 1b) Pedido demasiado grande: se corta acá con la descomposición y
    # sin ejecutar. Absorbe lo que hacía `_maybe_plan_large_prompt`
    # (Opción 4) en server.py, que pagaba una corrida entera de
    # night.TaskGenerator para llegar a lo mismo; aquí sale del turno de
    # planificación que ya se estaba pagando. Río abajo, server.py toma
    # esta salida (`phase_at_end="planned"`) y la convierte en un grafo
    # que sí corre — ver `_grafo_en_vez_de_proponer`.
    #
    # El guard se repite acá aunque el prompt ya lo pida: si el modelo lo
    # ignora, no queremos secuestrar un pedido que solo retomaba. Mira
    # `es_continuacion` y NO `is_followup` (2026-08-23): con follow-up a
    # secas el grafo no se armaba nunca, porque todo pedido que entra por
    # un hilo abierto es follow-up.
    if signal == "too_large" and not es_continuacion:
        logger.info("pedido grande: propongo descomposición sin ejecutar")
        propuesta = _format_decomposition(plan)
        # El turno SÍ se guarda en el hilo (2026-08-24). Antes esto
        # devolvía `messages_json: ""` y server.py no guarda lo vacío, así
        # que la descomposición no quedaba en el historial del modelo — y
        # el mensaje siguiente a una descomposición es casi siempre
        # "dale", "empezá por la 1" o "realiza el plan", que sin contexto
        # no significan nada.
        #
        # Medido el 24/8 en un hilo de ejemplo: se propuso el plan, el
        # humano contestó "Realiza el plan", el planificador lo recibió
        # con el historial en CERO y respondió que el pedido estaba
        # vacío; el ejecutor salió a adivinar 19 minutos y 1,96 M tokens.
        # Y el humano no tenía cómo notarlo: la UI arma los turnos desde
        # las filas de `chats`, así que en pantalla la conversación se
        # veía completa mientras el modelo no recibía ninguno de los dos.
        historial = _dump_messages([
            ModelRequest(parts=[UserPromptPart(content=user)]),
            ModelResponse(parts=[TextPart(content=propuesta)]),
        ])
        return {
            "content": propuesta,
            "model": planner_spec,
            "tokens_in": None, "tokens_out": None,
            "tool_calls": None, "duration_ms": 0,
            "messages_json": historial, "phase_at_end": "planned",
            "last_tool": None, "legs": 0, "steers": 0,
            "steer_texts": [], "progress_events": [],
            "plan": plan, "planner_model": planner_spec,
            "verifier_verdict": "", "verifier_feedback": "",
            "verifier_model": "", "doc": "", "documenter_model": "",
            "three_stage": True,
            # El corte por pedido grande NO ejecuta nada, pero el turno
            # del planificador sí se pagó: si no viaja acá, esos tokens
            # desaparecen de las métricas.
            "stage_usage": {"planner": planner_usage},
            "stage_errors": stage_errors,
        }

    # 2) EJECUTOR — el plan se inyecta como system_extra. Entra como
    # bloque "Plan a ejecutar" al final del system prompt, así no pisa
    # la voz del experto ni el bloque de git diff (que va antes). El
    # prompt del usuario queda limpio para que el LLM no duplique el
    # pedido.
    plan_block = ""
    if plan and not trivial:
        plan_block = (
            "## Plan a ejecutar (del planificador)\n"
            f"{plan}\n\n"
            "Sigue estos pasos en orden. Si descubris que un paso es\n"
            "incorrecto, o que el pedido del humano es mas simple que el\n"
            "plan, indicarlo en la respuesta final y continuar igual: el\n"
            "plan es una guia, no un contrato.\n"
            # Etapa B, P4: una linea, no un parrafo. Ese bloque compite
            # por la atencion de un modelo que ya tiene 20 tools; un
            # parrafo extra se ignora.
            "Al terminar cada paso, marcalo con `plan_step_done(nro)`."
        )
    elif trivial:
        # Tarea trivial: el ejecutor recibe la respuesta del
        # planificador como guía. Evita que el LLM "explore" un pedido
        # de una línea.
        plan_block = (
            "## Plan a ejecutar (del planificador)\n"
            f"{plan}\n\n"
            "Pedido trivial: resuélvelo directo, sin exploración."
        )
    # Si el planificador devolvió "" (falló o vino vacío) no se inyecta
    # nada y el ejecutor trabaja como siempre.

    # system_extra puede venir ya definido por el caller. Se antepone el
    # plan para que lo del caller quede al final y gane en la lectura.
    extra_orig = kwargs.get("system_extra") or ""
    if plan_block:
        kwargs["system_extra"] = (
            f"{plan_block}\n\n{extra_orig}" if extra_orig else plan_block)

    # Supervisor de media corrida (2026-08-17). El verificador dejó de
    # correr solo al final: en cada corte de tanda compara el plan contra
    # lo que el ejecutor viene haciendo, y si se desvió, corta ahí.
    #
    # Solo `off_plan` corta. `needs_more` significa "falta trabajo,
    # seguí" —justo lo contrario— y `complete` a mitad de camino se deja
    # seguir a propósito: el ejecutor cierra solo y el verificador final
    # dictamina. Cortar en un `complete` intermedio arriesga truncar por
    # un veredicto optimista, y acá lo que importa es que la tarea quede
    # bien, no que termine rápido.
    #
    # Se salta cuando el verificador está opt-out por proyecto o cuando
    # no hay plan contra el que comparar: sin plan, "off_plan" no
    # significa nada.
    mid_verdicts: list[dict] = []
    # Bitácora de interrupciones Ejecutor→Planificador y de las
    # replanificaciones que esas interrupciones disparan. Se acumula
    # durante todo el run y se persiste al final junto con los
    # `mid_verdicts`, para que el documentador pueda leer la historia
    # completa: qué bloqueó al ejecutor, por qué cambió la estrategia,
    # y cuál fue el plan definitivo que terminó aprobando el verificador.
    stages_events: list[dict] = []
    # El pedido ORIGINAL. `user` se reescribe en cada ronda del lazo de
    # continuación con el nudge del verificador, y las etapas auxiliares
    # tienen que juzgar contra lo que pidió el humano, no contra el
    # "seguí con esto" que se escribió a sí mismo el sistema.
    user_original = user

    async def _supervisar(parcial: dict) -> str:
        v_tuple = await _run_verifier(
            user=user_original, plan=plan,
            executor_result={
                **parcial,
                "tool_calls_summary": _summarize_tool_calls_from_messages(
                    parcial.get("messages_json") or ""),
            },
            model_spec=verifier_spec, ponytail=ponytail,
            on_progress=on_progress, deadline=limite_pedido,
        )
        # 5-tupla (Etapa B P5) o 4-tupla vieja — `_supervisar` ignora
        # los pasos verificados, así que descartamos el 5to si está.
        if len(v_tuple) == 5:
            v, f, u, err = v_tuple[:4]
        else:
            v, f, u, err = v_tuple
        mid_verdicts.append({
            "leg": parcial.get("leg"), "verdict": v, "feedback": f,
            "usage": u, "error": err,
        })
        if err:
            # El verificador se cayó: no es evidencia de desvío. Seguir es
            # lo conservador — cortar por una caída del supervisor sería
            # perder trabajo bueno por un fallo ajeno.
            return ""
        if v != "off_plan":
            return ""
        return f or "el verificador marcó off_plan sin dar detalle"

    if ver_enabled and plan.strip():
        kwargs["on_leg_boundary"] = _supervisar

    # Lazo de continuación (2026-08-17). `needs_more` significa "falta
    # trabajo y otra pasada lo termina" — o sea que el sistema YA sabe qué
    # hacer, y hasta ahora igual devolvía la pelota al humano con un
    # "respondé continúa". Eso convertía cada tarea grande en una sesión de
    # apretar un botón cada 20 minutos.
    #
    # Ahora el runner cierra el lazo solo: re-entra al ejecutor con el
    # historial rescatado y el feedback del verificador como consigna. Es
    # la misma maquinaria del auto-continue de presupuesto, con una
    # diferencia que importa: el nudge no es "continúa" a secas, es lo que
    # el verificador dijo que falta. Una continuación dirigida.
    #
    # Corta con `complete` (listo), `off_plan` (otra pasada empeora),
    # `needs_human` (hay una decisión que no es nuestra), un run roto, una
    # pregunta abierta, o al agotar las rondas. El anillo real sigue siendo
    # el deadline global del experto, que aplica a cada pasada.
    max_rondas = int(
        defaults.get("verifier_rounds")
        if defaults.get("verifier_rounds") is not None
        else config.expert_verifier_rounds())
    rondas: list[dict] = []
    # Rondas disparadas por "el turno no ejecutó nada", aparte de las de
    # `needs_more`. Se permite UNA: ver `reintento_vacio`.
    forzadas = 0
    # Interrupciones Ejecutor→Planificador honradas en este run. Tienen
    # que tener tope propio: ese camino hace `continue` SIN tocar
    # `rondas`, que es lo único que corta el `while True`, y cada pasada
    # estrena deadline (el de `run_expert` se calcula con un `t0` nuevo
    # en cada llamada). Sin este contador el lazo no tiene cota — ni de
    # vueltas ni de tiempo — a dos LLM calls por vuelta (ejecutor +
    # planificador). El caso peor es determinista: si la
    # replanificación falla, el plan queda igual, el ejecutor recibe el
    # mismo input y emite la misma señal.
    interrupciones = 0
    MAX_INTERRUPCIONES = 2
    sin_trabajo = False
    doc = ""
    verifier_usage: dict = {}
    # ¿Llegó a correr el verificador? `verifier_usage` no alcanza: una
    # etapa puede correr y no reportar tokens.
    verifier_corrio = False
    documenter_usage: dict = {}
    verdict, feedback = "", ""
    # Lo acumulado de las rondas ANTERIORES: el `result` que sobrevive es
    # el de la última, así que sin esto el turno reportaría el costo de la
    # pasada final como si fuera el total.
    acum = {"tokens_in": 0, "tokens_out": 0, "cache_read_tokens": 0, "tool_calls": 0,
            "duration_ms": 0, "legs": 0, "progress_events": []}
    # Presupuesto del PEDIDO, calculado una sola vez. `expert_timeout_s`
    # acota UNA llamada a `run_expert`, y este lazo la llama de nuevo por
    # cada ronda que pide el verificador — con un `t0` nuevo cada vez. El
    # docstring de `expert_verifier_rounds` decia que "cada pasada sigue
    # acotada por el deadline global": no lo estaba, porque ese deadline
    # se rehacia. Con los defaults el peor caso eran tres pasadas enteras.
    # El plazo se fijó al entrar, antes de planificar; no se renueva aquí.
    corte_por_presupuesto = False
    # Declarado antes del loop porque la cabeza lo lee para encadenar la
    # bitácora entre rondas; en la primera vuelta todavía no hay ronda
    # previa y `{}` es exactamente eso.
    result: dict = {}

    # 2026-08-28: la bitácora vivía solo dentro de `run_expert` y acá se
    # referenciaba sin inicializar (`bitacora.fusionar_pasos_verificados`
    # en el bloque del verificador). El PR #48 dejó el call site pero
    # la inicialización quedó del otro lado del wrapper. Cargamos acá y
    # la pasamos por kwargs como cualquier otra: `run_expert` la retoma
    # desde `bitacora_json` si la recibe serializada.
    bitacora = Bitacora.cargar(kwargs.get("bitacora_json") or "") \
        if kwargs.get("bitacora_json") else Bitacora()

    while True:
        # La ronda N+1 arranca con lo que la N verificó (2026-08-26).
        # Sin esto cada ronda recibia la bitacora del turno ANTERIOR y
        # perdia lo que acababa de comprobar la ronda de recien, que es
        # justo el contexto que el verificador le pidio completar.
        if result.get("bitacora_json"):
            kwargs["bitacora_json"] = result["bitacora_json"]
        result = await run_expert(project, user, **kwargs)

        # `result` puede llegar como None cuando el caller (sobre todo el
        # night worker) sustituye el ejecutor por un doble que devuelve
        # None. Se trata como needs_human: el humano tiene que mirar por
        # qué el ejecutor devolvió vacío, en lugar de propagar.
        if result is None:
            result = {
                "content": "", "model": planner_spec,
                "tokens_in": None, "tokens_out": None,
                "tool_calls": 0, "duration_ms": 0,
                "messages_json": "", "phase_at_end": "no_result",
                "last_tool": None, "legs": 1, "steers": 0,
                "steer_texts": [], "progress_events": [],
            }

        # 2b) INTERRUPCIÓN DEL EJECUTOR (Etapa B — `executor_interruption`).
        # Si el ejecutor emite la señal `EXECUTOR_INTERRUPTION` (o el
        # fallback `<<INTERRUPT:EXECUTOR>>`) en su salida, frena la etapa
        # AHORA: no llama al verificador, no auto-avanza, no entra al
        # lazo de continuación. En vez de eso registra el evento,
        # re-pasa por el planificador una vez más con el payload de la
        # interrupción como contexto, y deja que el lazo principal
        # reintente al ejecutor con el plan revisado.
        #
        # El verificador y el documentador son ciegos a esta señal — la
        # entrega nunca les llega mientras la ronda esté en estado de
        # interrupción. Es por diseño: el feedback del ejecutor se
        # inyecta al planificador, que es quien decide cómo seguir.
        interrupcion = _parse_executor_interruption(result.get("content") or "")
        if interrupcion is not None and interrupciones >= MAX_INTERRUPCIONES:
            # Tope alcanzado: dejamos de honrar la señal y caemos al
            # verificador como si el ejecutor no la hubiera emitido. Es
            # la salida menos destructiva — el trabajo hecho se
            # verifica igual — y queda el evento para que se vea en la
            # traza por qué se ignoró.
            logger.warning(
                "run_expert_staged: %d interrupciones del ejecutor en el "
                "mismo run (tope %d) — ignoro la señal y sigo al "
                "verificador", interrupciones, MAX_INTERRUPCIONES)
            stages_events.append({
                "type": "executor_interruption_ignored",
                "reason": interrupcion.get("reason") or "",
                "limit": MAX_INTERRUPCIONES,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime()),
                "turn": len(rondas) + 1,
            })
            interrupcion = None
        if interrupcion is not None:
            interrupciones += 1
            ev = {
                "type": "executor_interruption",
                "reason": interrupcion.get("reason") or "",
                "affected_step_id": interrupcion.get("affected_step_id") or "",
                "error_context": interrupcion.get("error_context") or "",
                "partial_progress": interrupcion.get("partial_progress") or {},
                "timestamp": (interrupcion.get("timestamp")
                              or time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime())),
                "turn": len(rondas) + 1,
            }
            stages_events.append(ev)
            ev_idx = len(stages_events) - 1
            # Adelantar el evento al panel del chat (best-effort).
            if (_db_ev := kwargs.get("db")) is not None \
                    and kwargs.get("chat_id"):
                try:
                    await _db_ev.set_chat_stages(
                        kwargs["chat_id"],
                        json.dumps({"events": list(stages_events)},
                                   ensure_ascii=False))
                except Exception as _e:  # noqa: BLE001
                    logger.debug(
                        "no pude adelantar executor_interruption a la "
                        "DB: %r", _e)
            # Construir el contexto extra para el segundo planificador:
            # el pedido ORIGINAL del humano más un bloque que cuenta
            # exactamente qué falló, dónde, y hasta dónde llegó el
            # ejecutor. El planificador recibe `is_followup=True` y
            # `es_continuacion=True` porque está retomando el mismo
            # trabajo, no abriendo un hilo nuevo.
            interrupt_ctx = (
                "\n\n## Interrupción reportada por el ejecutor\n"
                "El ejecutor paró antes de terminar y emitió la señal\n"
                "`EXECUTOR_INTERRUPTION`. El plan actual no le sirvió.\n\n"
                f"- Razón: {ev['reason']}\n"
                f"- Paso afectado: {ev['affected_step_id']}\n"
                "- Contexto del error:\n"
                f"  {ev['error_context']}\n"
                "- Avance parcial registrado:\n"
                f"  {json.dumps(ev['partial_progress'], ensure_ascii=False)}\n\n"
                "Emití un PLAN REVISADO que esquive el problema: cambia el\n"
                "orden, reemplaza el paso bloqueado por un equivalente, o\n"
                "parte el paso en subtareas. NO repitas el plan original.\n"
                "Mantené todo lo que el ejecutor YA avanzó (ver\n"
                "`partial_progress`): empezar de cero perdería ese\n"
                "trabajo.\n"
            )
            revised_user = user_original + interrupt_ctx
            # El planificador tiene que ver lo que el ejecutor acaba de
            # dejar en el hilo, no solo el resumen viejo que recibió el
            # ejecutor al arrancar. Si la corrida anterior pasó por un
            # verificador (rondas > 0) el `history_json` viejo ya está
            # desfasado.
            planner_history = (
                result.get("messages_json") or history_json
            )
            try:
                (rev_plan, _rev_usage, rev_err) = await _run_planner(
                    user=revised_user, project=project,
                    model_spec=planner_spec, ponytail=ponytail,
                    on_progress=on_progress, is_followup=True,
                    es_continuacion=True,
                    history_recap=_history_recap(planner_history),
                    toolsets=planner_toolsets,
                    deadline=limite_pedido,
                )
            except Exception as _e:  # noqa: BLE001
                logger.warning(
                    "run_expert_staged: el segundo planificador falló "
                    "tras una interrupción del ejecutor: %r", _e)
                rev_plan, rev_err = "", str(_e)
            # Registrar el resultado de la replanificación como evento.
            rev_ev = {
                "type": "plan_revision",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime()),
                "turn": len(rondas) + 1,
                "previous_plan_chars": len(plan or ""),
                "new_plan_chars": len(rev_plan or ""),
                "plan": rev_plan,
                # `ev_idx` YA es el índice de la interrupción que
                # disparó esta replanificación (se calcula justo
                # después de su append). Restarle 1 apuntaba al evento
                # anterior — y con la interrupción como primer evento
                # del run daba -1, que en Python indexa el ÚLTIMO, o
                # sea este mismo `plan_revision` una vez agregado.
                "trigger_event": ev_idx,
            }
            if rev_err:
                rev_ev["error"] = rev_err
                # Planificador caído en la replanificación: el lazo
                # sigue con el plan viejo. El ejecutor ya tiene la
                # señal de interrupción en su `messages_json`, así que
                # puede reorientarse sin un plan nuevo.
                logger.warning(
                    "run_expert_staged: replanificación falló — sigo "
                    "con el plan original")
            else:
                plan = rev_plan
                # El bloque del plan viejo se descarta entero: el plan
                # revisado es lo que el ejecutor tiene que ver de ahí
                # en más. Se reconstruye el mismo bloque "Plan a
                # ejecutar" que se armó para el primer plan, para no
                # desalinear la voz del experto ni las instrucciones
                # de cierre (`plan_step_done`). `ponytail:` el bloque
                # trivial se ignora a propósito: si la replanificación
                # degradó el plan a trivial, el ejecutor ya estaba en
                # el camino corto de todas formas.
                new_plan_block = ""
                if plan and not trivial and signal != "trivial":
                    new_plan_block = (
                        "## Plan a ejecutar (del planificador)\n"
                        f"{plan}\n\n"
                        "Sigue estos pasos en orden. Si descubris que un "
                        "paso es\nincorrecto, o que el pedido del humano "
                        "es mas simple que el\nplan, indicarlo en la "
                        "respuesta final y continuar igual: el\nplan es "
                        "una guia, no un contrato.\n"
                        "Al terminar cada paso, marcalo con "
                        "`plan_step_done(nro)`."
                    )
                kwargs["system_extra"] = (
                    f"{new_plan_block}\n\n{extra_orig}"
                    if new_plan_block else extra_orig
                )
                rev_ev["new_plan"] = plan[:4000]
                # Adelantar el plan revisado al panel.
                if (_db_ev := kwargs.get("db")) is not None \
                        and kwargs.get("chat_id") and plan.strip():
                    try:
                        await _db_ev.set_chat_stages(
                            kwargs["chat_id"],
                            json.dumps({"plan": plan[:4000],
                                        "planner_model": planner_spec,
                                        "events": list(stages_events)},
                                       ensure_ascii=False))
                    except Exception as _e:  # noqa: BLE001
                        logger.debug(
                            "no pude adelantar el plan revisado a la "
                            "DB: %r", _e)
            stages_events.append(rev_ev)
            # NO se llama al verificador en esta vuelta, NO se entra
            # al lazo de continuación, NO se cuenta como ronda. La
            # próxima iteración del `while True:` corre el ejecutor de
            # nuevo con el plan revisado (o el original si la
            # replanificación falló).
            continue

        # 3) VERIFICADOR — solo si el run terminó en un estado revisable.
        # En error / timeout / cancelación no hay nada que verificar: el
        # humano ya sabe que falló.
        phase = result.get("phase_at_end") or ""
        broken = phase in ("error", "timeout", "hard_timeout", "idle_timeout",
                           "cancelled", "no_result",
                           # 2026-08-26: verificar un run que cortó el
                           # proveedor da `needs_more` y eso re-lanza al
                           # ejecutor contra el mismo endpoint caído.
                           "provider_error")
    # 2026-08-16: preguntar es un final LEGÍTIMO del turno, no un run a
    # medias. Antes las dos etapas de después seguían corriendo sin
    # enterarse, y el humano terminaba leyendo tres cosas que se
    # contradecían en el mismo mensaje:
    #
    #   📝 Registro del cambio: …   ← el documentador da el trabajo por hecho
    #   🔁 needs_more … respondé **continúa**   ← el verificador pide otra cosa
    #   ❓ ¿Instalo pwsh 7? [sí|no] ← la tarjeta pide una decisión distinta
    #
    # Nada de eso es un bug de una etapa suelta: es que ninguna sabía que
    # el run se había detenido a propósito. Con la pregunta abierta no hay
    # nada que verificar (el trabajo está en pausa) ni que documentar (no
    # terminó), así que las dos se saltean y el turno cierra con el
    # resumen del ejecutor + la pregunta, que es lo único coherente.
        pregunto = bool(result.get("question_id"))
        # El corte por desvío ya trae su veredicto: lo emitió el supervisor
        # de media corrida en el borde de tanda. Volver a preguntarle al
        # verificador cuesta otro turno y puede contradecir el corte que ya
        # se le mostró al humano en el `content`.
        if phase == "off_plan" and mid_verdicts:
            ultimo = mid_verdicts[-1]
            verdict = "off_plan"
            feedback = ultimo.get("feedback") or ""
            logger.info(
                "run_expert_staged: corte por off_plan en la tanda %s — reuso "
                "el veredicto del supervisor en vez de pagar otro turno",
                ultimo.get("leg"))
        elif pregunto and not broken:
            verdict, feedback = "", ""
            stage_errors.pop("verifier", None)
            logger.info(
                "el experto dejó una pregunta abierta (%s): salteo "
                "verificador y documentador para que la respuesta no se "
                "contradiga con ella", result.get("question_id"))
        elif broken:
            verdict, feedback = "needs_human", (
                f"el ejecutor terminó en estado {phase!r}: verificación "
                "omitida")
        else:
            # Resumen de tool calls para las etapas auxiliares. Se deriva
            # de `messages_json` porque el dict del ejecutor no lo trae
            # aparte.
            result["tool_calls_summary"] = (
                _summarize_tool_calls_from_messages(
                    result.get("messages_json") or ""))
            if ver_enabled:
                v_tuple = await _run_verifier(
                    user=user_original, plan=plan, executor_result=result,
                    model_spec=verifier_spec, ponytail=ponytail,
                    on_progress=on_progress, deadline=limite_pedido,
                )
                # 5-tupla con `_pasos_verificados` (Etapa B P5);
                # toleramos el shape viejo de 4 por si quedó alguien.
                if len(v_tuple) == 5:
                    (verdict, feedback, verifier_usage, ver_err,
                     _pasos_v) = v_tuple
                else:
                    (verdict, feedback, verifier_usage,
                     ver_err) = v_tuple
                    _pasos_v = []
                verifier_corrio = True
                if _pasos_v:
                    # 2026-08-28: el PR #48 metió este call site sin
                    # inicializar la bitácora en `run_expert_staged`. Ya
                    # está cargada arriba del lazo: fusionar contra la
                    # misma instancia y volver a serializarla para que
                    # la próxima ronda (y el `run_expert` de abajo) la
                    # vean actualizada.
                    bitacora.fusionar_pasos_verificados(_pasos_v)
                    kwargs["bitacora_json"] = bitacora.volcar()
                if ver_err:
                    stage_errors["verifier"] = ver_err
            else:
                # Opt-out explícito del proyecto: no es un fallo ni una
                # aprobación. Queda "" para que la UI y las métricas no lo
                # cuenten como un `complete` que nadie emitió.
                verdict, feedback = "", ""

        rondas.append({"ronda": len(rondas) + 1, "verdict": verdict,
                       "feedback": feedback,
                       "phase_at_end": phase,
                       "tool_calls": result.get("tool_calls")})

        # Turno que no ejecutó NADA (2026-08-17). El síntoma que más
        # molesta no es el error: es el turno que cierra prolijo sin
        # haber tocado nada. Medido sobre sample-shop el 17/8: 19 de 43 runs
        # del día terminaron con CERO tool calls, y el verificador dejó
        # pasar 3 de ellos como `complete`.
        #
        # Esto NO se le pregunta a un LLM: `tool_calls` es un número que
        # el runner ya tiene. Si la tarea no era trivial, el run no está
        # roto y el experto no dejó una pregunta abierta, un turno sin
        # una sola tool call es un turno que describió el trabajo en vez
        # de hacerlo — y eso se reintenta aunque el verificador haya
        # dicho `complete` u `off_plan`.
        sin_trabajo = (not trivial and not broken and not pregunto
                       and not (result.get("tool_calls") or 0))
        # Una sola vez: si la pasada forzada TAMPOCO ejecuta nada, el
        # problema no es que no se haya enterado, y seguir insistiendo
        # quema turnos del modelo pesado sin cambiar el final.
        reintento_vacio = sin_trabajo and not forzadas

        # ¿Otra pasada? Con needs_more o con un turno vacío, historial
        # para retomar, y rondas disponibles. Un run roto o con pregunta
        # abierta NO se reintenta: en el primero no hay nada que
        # continuar y en el segundo el trabajo está en pausa esperando al
        # humano a propósito.
        if not ((verdict == "needs_more" or reintento_vacio)
                and len(rondas) <= max_rondas
                and result.get("messages_json") and not broken
                and not pregunto):
            break

        # …y que quede tiempo para la pasada Y para cerrar. Entrar a una
        # ronda que el deadline va a cortar a la mitad gasta el modelo
        # para tirar el resultado: es peor que parar y decir que falta.
        # La reserva es para el verificador, el documentador y la
        # persistencia, que corren DESPUES del ultimo `run_expert`.
        if limite_pedido - time.monotonic() < RESERVA_CIERRE_S:
            corte_por_presupuesto = True
            logger.info(
                "run_expert_staged: corto por el presupuesto del pedido "
                "(ronda %d/%d, quedaban %.0fs)", len(rondas) + 1,
                max_rondas + 1, max(0.0, limite_pedido - time.monotonic()))
            break

        acum["tokens_in"] += result.get("tokens_in") or 0
        acum["tokens_out"] += result.get("tokens_out") or 0
        acum["cache_read_tokens"] += result.get("cache_read_tokens") or 0
        acum["tool_calls"] += result.get("tool_calls") or 0
        acum["duration_ms"] += result.get("duration_ms") or 0
        acum["legs"] += result.get("legs") or 0
        acum["progress_events"].extend(result.get("progress_events") or [])

        kwargs["message_history_json"] = result["messages_json"]
        if reintento_vacio:
            forzadas += 1
            # El nudge nombra el modo de fallo exacto que se midió: el
            # experto ESCRIBIÓ el comando en la respuesta (```PS> docker
            # --version```) y pidió que le confirmaran la salida, en vez
            # de llamar la tool. Decirle "continúa" no alcanza: no cree
            # que le falte nada.
            user = (
                "No ejecutaste NINGUNA herramienta en el turno anterior: "
                "describiste el trabajo y cerraste. Hazlo ahora, de verdad.\n"
                "- Si necesitas la salida de un comando, LLÁMALO con la tool "
                "`shell`. Escribir el comando en tu respuesta y pedir que te "
                "confirmen el resultado no es ejecutarlo.\n"
                "- Si necesitas ver un archivo, léelo con `read_file`.\n"
                "- No pidas permiso para leer ni para ejecutar: ya lo tienes.\n"
                "- Si de verdad hace falta una decisión humana (instalar algo, "
                "elegir entre caminos que no son equivalentes), usa "
                "`ask_human`: es la única forma válida de frenar el turno.\n"
                "- Si el pedido era solo una pregunta y ya la respondiste, "
                "dilo en una línea y cierra. No inventes trabajo para "
                "justificar el turno.")
            logger.info(
                "run_expert_staged: turno sin tool calls → ronda %d/%d "
                "forzada (verdict del verificador: %s)",
                len(rondas) + 1, max_rondas + 1, verdict or "(sin verificar)")
        else:
            # El nudge dirigido: lo que el verificador dijo que falta, no un
            # "continúa" a ciegas.
            user = (
                "Continúa la tarea. El verificador revisó lo que hiciste y dice "
                f"que falta esto: {feedback or 'completar el plan'}. Atiéndelo y "
                "cierra con el resumen final. Si al mirarlo descubres que ya "
                "estaba cubierto, dilo y no rehagas el trabajo.")
            logger.info(
                "run_expert_staged: needs_more → ronda %d/%d sola (feedback: %s)",
                len(rondas) + 1, max_rondas + 1, (feedback or "")[:120])
        if on_progress is not None:
            try:
                await on_progress(phase="verifier", tool=None,
                                  message=f"needs_more → ronda {len(rondas) + 1}")
            except Exception:  # noqa: BLE001 — callback best-effort
                pass

    # Lo de las rondas previas se suma a lo de la última.
    if len(rondas) > 1:
        for k in ("tokens_in", "tokens_out", "cache_read_tokens", "tool_calls", "duration_ms",
                  "legs"):
            if result.get(k) is not None:
                result[k] = (result.get(k) or 0) + acum[k]
        result["progress_events"] = (
            acum["progress_events"] + (result.get("progress_events") or []))

    # 4) DOCUMENTADOR — una sola vez, sobre la última pasada. Se omite
    # cuando no hay nada que registrar.
    # ponytail: la condición de "hubo trabajo" es simplemente "hubo al
    # menos una tool call y el plan no fue trivial". Es una heurística
    # barata; si algún día hace falta distinguir lectura de escritura,
    # filtrar por nombre de herramienta (edit_file/write_file/run_shell)
    # en _summarize_tool_calls.
    #
    # t7 (2026-09-01): el Documentador es la fase FINAL de cierre y solo
    # corre cuando el Verificador emite verdict=='complete'. Para
    # needs_more/off_plan el run vuelve al loop del Ejecutor (manejado
    # en t6) y la bitácora queda corta a propósito: documentar un run
    # que aún no terminó contaminaría el changelog con trabajo a medio
    # hacer. La guarda que ya existía (not broken and not pregunto and
    # phase != 'off_plan') no atrapaba el caso verdict=='needs_more',
    # que es justamente el más común.
    if not broken and not pregunto and phase != "off_plan":
        # `has_work` era "hubo tools y el plan no era trivial", y una
        # revision de solo lectura cumple las dos: 20 `read_file` y
        # ninguna escritura pasaban por trabajo documentable. Ver
        # `_solo_reviso`.
        solo_reviso = _solo_reviso(result.get("tool_calls_summary"))
        has_work = (bool(result.get("tool_calls_summary")) and not trivial
                    and not solo_reviso)
        documenter_blocked = (verdict != "complete")
        if doc_enabled and has_work and not documenter_blocked:
            # Persistir el evento `complete` ANTES de invocar al
            # Documentador. Si el Documentador falla, la traza igual
            # registra que llegamos al cierre.
            stages_events.append({
                "turn": len(rondas),
                "type": "verifier_complete",
                "verdict": "complete",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime()),
            })
            doc, documenter_usage, doc_err = await _run_documenter(
                user=user_original, plan=plan, executor_result=result,
                verdict=verdict or "(sin verificar)", feedback=feedback,
                model_spec=documenter_spec, ponytail=ponytail,
                on_progress=on_progress, deadline=limite_pedido,
            )
            if doc_err:
                stage_errors["documenter"] = doc_err
        elif doc_enabled and has_work and documenter_blocked:
            # Solo registramos el skip si el Documentador HABRÍA corrido
            # (tenía trabajo y estaba habilitado). Si el motivo del skip
            # era "no hubo tool calls", ya quedó en result.tool_calls y
            # no hace falta duplicarlo.
            stages_events.append({
                "turn": len(rondas),
                "type": "documenter_skipped",
                "verdict": verdict,
                "reason": "verdict_no_complete",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime()),
            })
        elif doc_enabled and solo_reviso and not trivial:
            # Este skip SÍ se registra, a diferencia del de "no hubo tool
            # calls": aquel se deduce mirando `result.tool_calls`, este
            # no. Sin el evento, un run de revisión sin resumen se ve
            # igual que uno donde el documentador se cayó.
            stages_events.append({
                "turn": len(rondas),
                "type": "documenter_skipped",
                "verdict": verdict,
                "reason": "solo_lectura",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime()),
            })

    result["verifier_rounds"] = rondas
    result["plan"] = plan
    result["planner_model"] = planner_spec
    result["verifier_verdict"] = verdict
    result["verifier_feedback"] = feedback
    # Los veredictos de media corrida, uno por borde de tanda. Van al
    # `stages_json` del chat: sin esto, un corte por desvío se vería en el
    # mensaje pero no quedaría en ningún lado para revisar después por qué
    # el ejecutor se fue del plan.
    result["mid_verdicts"] = mid_verdicts
    # Lo mismo para las interrupciones Ejecutor→Planificador (signal
    # `EXECUTOR_INTERRUPTION`) y las replanificaciones que
    # dispararon. Cada evento trae los 5 campos del esquema más un
    # `turn` que cuenta en qué vuelta del lazo ocurrió.
    result["stages_events"] = stages_events
    result["verifier_model"] = verifier_spec if verifier_corrio else ""
    result["doc"] = doc
    result["documenter_model"] = documenter_spec if doc else ""
    result["three_stage"] = True
    # Tokens por etapa. NO se suman a result["tokens_in"/"tokens_out"]:
    # esas dos siguen siendo del ejecutor, para que los 200+ runs previos
    # a esto sigan siendo comparables. Una etapa sin medición queda como
    # {} y las métricas la muestran como "sin dato", no como cero.
    result["stage_usage"] = {
        "planner": planner_usage,
        "verifier": verifier_usage,
        "documenter": documenter_usage,
    }
    result["stage_errors"] = stage_errors

    # El registro del documentador se agrega al content: es la única
    # etapa auxiliar que el humano ve directamente. El plan y el
    # veredicto siguen viajando solo en el dict, a la espera de que la
    # UI los muestre.
    if doc:
        result["content"] = f"{(result.get('content') or '').rstrip()}\n\n---\n\n{doc}"
        # …y TAMBIÉN al historial (2026-08-15): si el registro solo va al
        # content, el turno siguiente arranca sin el texto que el humano
        # está leyendo, y "seguí con el pendiente que anotaste" no tiene
        # referente. Ver `_merge_doc_into_history`.
        if result.get("messages_json"):
            result["messages_json"] = _merge_doc_into_history(
                result["messages_json"], doc)

    # Si el verificador marca needs_human, se antepone un aviso al
    # content para que la UI lo distinga (mismo patrón que
    # budget_exceeded). Si todavía quedan legs, el caller (server.py)
    # puede reagendar otra corrida usando el feedback como prompt: esa
    # decisión es del caller, no del wrapper.
    if verdict == "needs_human" and result.get("content"):
        feedback_short = (feedback or "").strip()[:200]
        result["content"] = (
            f"⚠️ El verificador marcó este run como `needs_human`: "
            f"{feedback_short}\n\n"
            f"{result['content']}"
        )
    # `needs_more` (2026-08-15: dejó de ser invisible · 2026-08-17: dejó de
    # pedir permiso). Antes esto siempre cerraba con "respondé continúa",
    # incluso sabiendo exactamente qué faltaba: el sistema tenía el
    # diagnóstico y aun así frenaba a esperar un humano. Ahora el lazo de
    # arriba reintenta solo, y este aviso queda para el único caso en que
    # el humano hace falta de verdad: se agotaron las rondas y el
    # verificador SIGUE diciendo que falta. Ahí la tercera pasada seguida
    # ya no es un problema de una pasada más.
    #
    # (La nota vieja decía que el re-run automático se había descartado
    # porque un verificador caído podía gastar un turno del modelo pesado.
    # Ese riesgo sigue existiendo y se acota distinto: `_run_verifier`
    # nunca devuelve `needs_more` cuando falla —devuelve `needs_human`—,
    # así que una caída no dispara reintentos.)
    if verdict == "needs_more" and result.get("content"):
        feedback_short = (feedback or "").strip()[:200]
        intentos = len(rondas)
        # Por qué se paró en esas pasadas, que no siempre es lo mismo. Sin
        # esto el aviso dice "seguí solo N" y deja creer que se agotaron
        # las rondas, cuando puede haber sido el reloj — dos causas con
        # dos remedios distintos (subir `verifier_rounds` no arregla un
        # presupuesto corto, y al revés tampoco).
        motivo = (" (corté por el presupuesto del pedido, no por las rondas)"
                  if corte_por_presupuesto else "")
        result["content"] = (
            f"{result['content'].rstrip()}\n\n"
            f"🔁 Seguí solo {intentos} pasada(s){motivo} y el verificador "
            f"todavía dice que falta: {feedback_short}\n"
            f"Acá sí te necesito: dime si el pedido cambió, o responde "
            f"**continúa** para darle otra vuelta."
        )
    elif verdict == "off_plan" and phase != "off_plan" and result.get("content"):
        # El corte de media corrida ya escribe su propio 🧭 en el content
        # (ver `run_expert`); este es el otro camino: el verificador FINAL
        # vota off_plan y hasta hoy no lo decía nadie.
        #
        # Va ARRIBA, no abajo, por el mismo motivo que el aviso de
        # `sin_trabajo` unas lineas mas abajo: "abajo es justo donde no se
        # lee". Aca pesa mas que en `needs_more`, porque este veredicto
        # dice que el resultado NO es lo que se pidio: leerlo despues del
        # texto prolijo que lo hace pasar por trabajo bien hecho llega
        # tarde.
        result["content"] = (
            f"🧭 **El verificador marcó este run como `off_plan`.** Lo hecho "
            f"está guardado, pero NO es lo que pedía el plan: revísalo "
            f"antes de darlo por bueno.\n\n"
            f"> {(feedback or '').strip()[:200]}\n\n"
            f"{result['content'].lstrip()}"
        )
    elif verdict == "complete" and len(rondas) > 1 and result.get("content"):
        # Cerró bien, pero no en la primera. Que se vea: son tokens que se
        # gastaron y contexto para leer el resultado.
        result["content"] = (
            f"{result['content'].rstrip()}\n\n"
            f"_🔁 Cerrado en {len(rondas)} pasadas: el verificador pidió "
            f"seguir y el ejecutor continuó solo._"
        )
    elif corte_por_presupuesto and result.get("content"):
        # Cortar por tiempo NO es haber terminado. Sin este aviso el turno
        # se lee como cerrado y el humano no sabe que hay trabajo
        # pendiente ni por que se paro — que es justo lo que hace que un
        # corte deje de ser recuperable.
        result["content"] = (
            f"{result['content'].rstrip()}\n\n"
            f"_⏳ Corté por el presupuesto del pedido en la pasada "
            f"{len(rondas)}: lo hecho está guardado y falta lo que pedía "
            f"el verificador. Responde **continúa** para seguir._"
        )
    elif not verdict and len(rondas) > 1 and result.get("content"):
        # Varias pasadas SIN veredicto: el verificador está apagado en el
        # proyecto, o el turno terminó con una pregunta abierta que pisó el
        # veredicto de la ronda previa.
        #
        # Hasta 1f1364b caían en el cintillo de arriba, que les atribuía al
        # verificador algo que nunca dijo ("el verificador pidió seguir") y
        # daba por "Cerrado" un turno que podía estar esperando respuesta.
        # Exigirle `complete` a esa rama los dejó mudos, así que el dato
        # —cuántas pasadas costó— vuelve acá, pero sin dueño: se afirma lo
        # único que se sabe con certeza.
        result["content"] = (
            f"{result['content'].rstrip()}\n\n"
            f"_🔁 Este turno tomó {len(rondas)} pasadas._"
        )

    # Un turno que no tocó nada NO se entrega como si estuviera bien
    # (2026-08-17). Se reintentó arriba y siguió sin ejecutar, así que
    # acá lo único honesto es decirlo primero, antes del texto prolijo
    # que lo hacía pasar por trabajo hecho. El aviso va ARRIBA a
    # propósito: abajo es justo donde no se lee.
    if sin_trabajo and result.get("content"):
        result["content"] = (
            "⚠️ Este turno **no ejecutó ninguna herramienta**: describió el "
            "trabajo sin hacerlo, y al pedírselo de nuevo tampoco lo hizo. "
            "Lo que sigue es una propuesta, no un resultado — nada de esto "
            "está verificado ni aplicado.\n\n"
            f"{result['content']}")
        result["sin_trabajo"] = True
    return result


# Nombre anterior. Se mantiene porque server.py, night.py y los tests
# ya lo importan, y porque `3stage` describía bien el diseño original.
run_expert_3stage = run_expert_staged


def _summarize_tool_calls_from_messages(messages_json: str) -> list[tuple[str, str]]:
    """Lista [(tool_name, args_str)] desde el messages_json serializado.

    Solo los nombres y los argumentos como string: alcanza para el
    verificador y el documentador, y evita inflar sus prompts. Sin
    recorte aquí; el recorte lo aplica `_render_tool_calls` (últimas 12
    llamadas, argumentos a 120 caracteres).
    """
    try:
        messages = json.loads(messages_json or "")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(messages, list):
        return []
    out: list[tuple[str, str]] = []
    for m in messages:
        if not isinstance(m, dict) or m.get("kind") != "response":
            continue
        for part in m.get("parts", []) or []:
            if part.get("part_kind") != "tool-call":
                continue
            name = part.get("tool_name") or "?"
            args = part.get("args")
            args_str = (args if isinstance(args, str)
                        else json.dumps(args, ensure_ascii=False, default=str)
                        if args is not None else "")
            out.append((name, args_str))
    return out


# ---------- ADR-029: Consultas (chat sin workspace ni tools) ----------
#
# Una consulta es UN turno de LLM con system = ponytail + system_prompt
# provisto, sin tools, sin repo, sin git, sin cbm. Reusa pydantic-ai y
# build_model — solo cambia el set de bloques y la construcción del
# Agent (sin tools/toolsets). Llamado por:
#   - admin.api_apply_skill_to_transcript (Voz tab: skill -> issue)
#   - mcp_installer.vet_with_llm (ADR-033: vetting LLM de MCPs)
# Bug fix 2026-07-18: el cleanup b8715b7 borró esta función sin
# actualizar los callers — quedaron AttributeError en runtime.
# Restaurada con el fix C1 aplicado (t0 en segundos, no nanosegundos).

async def run_consult(
    *, user: str, system_prompt: str = "", skills_block: str = "",
    model_override: str = "", db: Any = None,
    on_progress: Any = None,
) -> dict:
    """Corre una consulta sin tools. Devuelve dict con content/usage.

    Misma cascada de timeout que run_expert (system_config > env >
    default), pero NO usa tools ni MCPs. Levanta ModelUnavailable si
    el modelo no se puede armar; cualquier otra excepción se propaga.
    """
    # t0 al PRINCIPIO para que duration_ms refleje el trabajo total.
    # Bug fix 2026-07-18 (C1): antes era perf_counter_ns() y el cálculo
    # de duration_ms mezclaba ns con segundos → negativo gigante.
    t0 = time.monotonic()
    spec = model_override or config.model_spec()
    model = build_model(spec)

    _global_to = None
    if db is not None:
        try:
            _global_to = await db.get_config("expert_timeout_s")
        except Exception:
            _global_to = None
    timeout = float(_global_to or config.expert_timeout_s())

    ponytail = await read_ponytail()
    parts = [ponytail, system_prompt, skills_block]
    instructions = "\n\n".join(p for p in parts if p)

    progress_sink = on_progress

    async def _emit_progress(**fields) -> None:
        if progress_sink is None:
            return
        try:
            await progress_sink(**fields)
        except Exception as e:  # noqa: BLE001 — el callback es best-effort
            logger.warning("on_progress callback falló: %r", e)

    await _emit_progress(phase="thinking", tool=None)
    agent = Agent(
        model, instructions=instructions,
        tool_timeout=config.tool_timeout_s(),
    )

    try:
        result = await asyncio.wait_for(agent.run(user), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"consulta timeout ({timeout}s)")
    except UsageLimitExceeded as e:
        raise RuntimeError(f"consulta cortada por presupuesto: {e}")

    duration_ms = int((time.monotonic() - t0) * 1000)
    usage = result.usage
    return {
        "content": str(result.output) if result.output is not None else "",
        "model": spec,
        "tokens_in": usage.input_tokens if usage else None,
        "tokens_out": usage.output_tokens if usage else None,
        "tool_calls": 0,  # siempre 0 por contrato (sin tools)
        "duration_ms": duration_ms,
    }


# ---------- Sugerencias de continuación (2026-07-26) ----------
#
# Después de cada respuesta, UN turno chico de LLM propone los próximos
# pasos probables. La UI y Discord los pintan como botones: un tap
# continúa el hilo sin escribir nada (el caso "voy manejando / estoy
# cocinando"). El texto del botón ES el prompt que se manda — por eso
# cada línea tiene que ser un mensaje válido por sí solo.
#
# ponytail: un turno extra por respuesta (no structured output, no
# agente nuevo: reusa run_consult). El techo es el costo — si molesta,
# se apaga con system_config suggestions_enabled=0 o se le pone un
# modelo barato en suggestions_model.

SUGGESTIONS_MAX = 3
# 80 = tope de `label` de un botón de Discord. Como el label es el
# prompt, pasarse significaría mandar algo distinto de lo que se leyó.
SUGGESTION_MAX_CHARS = 80
# Cuánta respuesta le mostramos al que sugiere. La cola es lo que
# importa (conclusiones, "próximos pasos" que el experto ya insinuó).
SUGGESTION_CONTEXT_CHARS = 3000

SUGGESTIONS_SYSTEM = """\
Propone los próximos pasos de una conversación técnica.

Te doy el último pedido del humano y la respuesta del experto. Devuelve
como máximo 3 continuaciones probables, UNA POR LÍNEA, sin numerar, sin
viñetas, sin comillas y sin ningún texto alrededor.

Reglas:
- Cada línea es el mensaje que el humano le mandaría al experto, en
  imperativo ("Muestra el diff", "Ejecuta los tests").
- Máximo 80 caracteres por línea.
- Concretas y accionables sobre ESTA respuesta; nada genérico
  ("continúa", "ok", "explica más").
- Distintas entre sí: cada una abre un camino diferente.
- Español neutro, sin modismos regionales.
- Si no hay nada útil que proponer, no devuelvas nada.
"""

# Viñetas / numeración / comillas con las que el modelo suele decorar.
_SUGGESTION_DECOR_RE = re.compile(r"^\s*(?:[-*•>]|\d+[.)]|[A-Z][.)])\s*")


def parse_suggestions(text: str) -> list[str]:
    """Texto crudo del sugeridor → lista de prompts listos para botón.

    Limpia viñetas/numeración/comillas, deduplica (case-insensitive),
    capea a SUGGESTIONS_MAX y recorta a SUGGESTION_MAX_CHARS en el
    último espacio (un botón truncado a mitad de palabra se lee como
    error, y el label ES el prompt).
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = _SUGGESTION_DECOR_RE.sub("", raw).strip().strip('"“”\'')
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) > SUGGESTION_MAX_CHARS:
            cut = line[:SUGGESTION_MAX_CHARS - 1]
            sp = cut.rfind(" ")
            line = (cut[:sp] if sp > 20 else cut).rstrip(" ,;:.") + "…"
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= SUGGESTIONS_MAX:
            break
    return out


async def suggest_followups(
    *, user: str, answer: str, model_spec: str = "",
) -> list[str]:
    """2-3 próximos pasos para la respuesta que acaba de salir.

    Best-effort por contrato: el caller la envuelve en try/except — sin
    sugerencias la respuesta sale igual, solo sin botones.
    """
    if not (answer or "").strip():
        return []
    prompt = (
        f"## Último pedido del humano\n{user.strip()[:1200]}\n\n"
        f"## Respuesta del experto\n{answer.strip()[-SUGGESTION_CONTEXT_CHARS:]}"
    )
    result = await run_consult(
        user=prompt, system_prompt=SUGGESTIONS_SYSTEM,
        model_override=model_spec,
    )
    return parse_suggestions(result.get("content", ""))


# ---------- Fase 1: liveness on-demand + progreso SSE al bot ----------
#
# Diseñados para "pregunta y te digo" (sin heartbeat periódico):
# - server.py mantiene un dict AppKey[PROGRESS_KEY][chat_id] = RunProgress
# - el iter() de run_expert actualiza ese dict vía on_progress
# - GET /experts/status/{chat_id} lo serializa (o 404 si ya terminó)
#
# El dataclass vive en memoria del proceso. Si el relay reinicia, se
# pierde — el caller lo documenta en el response ("lost_on_restart": true).

from dataclasses import dataclass, field

# Fase 3b (2026-07-20f): tope de pasos guardados en el snapshot. El web
# pollea /experts/status y renderea los nuevos; guardar TODOS inflaría el
# payload en runs de 200 tool calls. Los últimos N alcanzan: el web ya
# renderizó los viejos, y al terminar loadMessages() trae el persistido
# completo. Cada diff ya viene capeado por _format_tool_step (~1.6KB).
STEPS_KEEP = 30

# Tope del texto de narración por paso (el "por qué" que dice el modelo
# antes de cada tool). Un modelo verborrágico puede tirar párrafos; el
# snapshot lo pollea la UI cada 1.5s y no queremos pagar eso.
#
# 700 → 1800 (2026-08-01): con 700 se cortaban las tablas de opciones a
# la mitad. Caso anonimizado (website-demo): el modelo ofreció A/B/C/D en una tabla
# de 1086 chars, el usuario vio hasta la fila A, y el mensaje final le
# decía "decime A, B o C de la tabla" — una tabla que nunca vio entera.
# En ese run 5 de 30 narraciones tocaron el tope. Peor caso del snapshot
# con STEPS_KEEP=30: ~54KB, y solo si el modelo escribe párrafos largos.
SAY_MAX_CHARS = 1800

#: Marca del corte. Sin esto el recorte es invisible y parece que el
#: modelo escribe frases truncadas (así se manifestó el bug de arriba).
SAY_CLIP_MARK = "… (recortado)"


def _clip_say(text: str) -> str:
    """Recorta la narración al tope dejando marca visible del corte."""
    if len(text) <= SAY_MAX_CHARS:
        return text
    return text[:SAY_MAX_CHARS] + SAY_CLIP_MARK


@dataclass
class RunProgress:
    chat_id: str
    target: str
    started_at: float            # time.monotonic()
    last_activity_at: float      # time.monotonic(), refrescado por nodo
    phase: str                   # "thinking" | "tool_call" | "writing"
    last_tool: str | None
    tool_calls: int
    tokens_in: int | None
    tokens_out: int | None
    model: str
    error: str | None = None
    graph_id: str = ""           # nodo de grafo, para no contar también al padre
    finished: bool = False       # True cuando el run terminó (para que
                                 # /status siga respondiendo post-mortem)
    # Pasos ricos del run (Fase 3b): mismos datos que van al embed de
    # Discord (línea legible + diff de edit_file). El web los renderea
    # en vivo como las MISMAS tarjetas que quedan al persistir.
    steps: list[dict] = field(default_factory=list)
    # `n` de los steps: contador propio y monótono. Antes era rp.tool_calls,
    # que dejó de servir cuando empezamos a intercalar pasos de narración
    # (dos pasos con el mismo n ⇒ la UI, que deduplica con n > lastStepN,
    # se comía el segundo).
    seq: int = 0
    # Cola de correcciones del humano (2026-07-25). La escribe
    # POST /experts/steer/{chat_id}; la consume run_expert en el próximo
    # borde de nodo. Vive acá porque el store ya está indexado por
    # chat_id y el endpoint ya lo tiene a mano — cero plumbing nuevo.
    steer: list[str] = field(default_factory=list)

    def snapshot(self, *, now: float | None = None) -> dict:
        """Serializa para el endpoint /experts/status."""
        n = now if now is not None else time.monotonic()
        elapsed_s = max(0.0, n - self.started_at)
        idle_s = max(0.0, n - self.last_activity_at)
        return {
            "chat_id": self.chat_id,
            "target": self.target,
            "elapsed_s": round(elapsed_s, 2),
            "idle_s": round(idle_s, 2),
            "phase": self.phase,
            "last_tool": self.last_tool,
            "tool_calls": self.tool_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "model": self.model,
            "error": self.error,
            "finished": self.finished,
            **({"graph_id": self.graph_id} if self.graph_id else {}),
            # Fase 3b: el web appendea los steps con n > lastStepN.
            "steps": self.steps,
        }


def make_progress_callback(
    store: dict[str, "RunProgress"], notify: Any | None,
    chat_id: str, target: str, model: str,
) -> Any:
    """Devuelve un callable async que actualiza store + emite notify.

    server.py lo pasa como `on_progress=` a run_expert. El store es
    el AppKey[PROGRESS_KEY] (dict compartido). El notify es el
    NotifyClient del relay (puede ser None en tests).
    """
    rp = RunProgress(
        chat_id=chat_id, target=target,
        started_at=time.monotonic(),
        last_activity_at=time.monotonic(),
        phase="thinking", last_tool=None, tool_calls=0,
        tokens_in=None, tokens_out=None, model=model,
    )
    store[chat_id] = rp
    # NotifyClient coalesce en segundo plano; los doubles/callers antiguos
    # conservan el contrato send. El panel nunca espera la conexión al bot.
    send_progress = (getattr(notify, "queue_progress", notify.send)
                     if notify is not None else None)

    def _push_step(kind: str, msg: str, *, tool: str | None = None,
                   diff: str | None = None, cmd: str | None = None,
                   tool_call_id: str | None = None) -> None:
        rp.seq += 1
        paso = {
            "n": rp.seq, "kind": kind, "tool": tool,
            "message": msg, "diff": diff or None,
        }
        # `cmd` y `output` solo cuando los hay: esta lista viaja entera en
        # CADA poll de /experts/status (1.5s), así que las claves nulas de
        # los pasos que no son shell son peso puro.
        if cmd:
            paso["cmd"] = cmd
        if tool_call_id:
            paso["tool_call_id"] = tool_call_id
        rp.steps.append(paso)
        if len(rp.steps) > STEPS_KEEP:
            del rp.steps[:-STEPS_KEEP]

    def _attach_output(tool: str | None, output: str,
                       tool_call_id: str | None = None) -> None:
        """Cuelga la salida de la terminal del paso que la disparó.

        Usa el ID de llamada cuando existe. Para eventos legacy sin ID,
        busca el paso más viejo de esa tool que todavía no tiene salida.

        2026-09-04, medido: pydantic-ai entrega los `ToolReturnPart` en
        el ORDEN en que se pidieron las tools. Esto buscaba de atrás
        para adelante —el comentario viejo afirmaba lo contrario, que el
        orden de los returns no era el de las calls— y con dos llamadas
        a la misma tool en un turno colgaba el primer resultado del
        último paso: las salidas salían cruzadas. No se notaba porque el
        turno dejaba un solo paso y los resultados de más se perdían.

        Si no aparece (el paso ya se cayó del tope de STEPS_KEEP), la
        salida se descarta en silencio: es decoración de un paso que la
        UI ya no muestra.

        La UI deduplica por `n > lastStepN`, así que un paso mutado
        DESPUÉS de haberse enviado no se repinta en vivo — se ve al
        recargar el hilo, que es cuando el humano lo va a mirar en
        serio. ponytail: si molesta, hace falta versionar el paso.
        """
        for step in rp.steps:
            if (step.get("kind") == "tool_call"
                    and step.get("tool") == tool
                    and (not tool_call_id or step.get("tool_call_id") == tool_call_id)
                    and not step.get("output")):
                step["output"] = output
                return

    async def _cb(*, phase: str, tool: str | None, tool_calls: int | None = None,
                  message: str | None = None, diff: str | None = None,
                  cmd: str | None = None, output: str | None = None,
                  tool_call_id: str | None = None) -> None:
        if phase == "tool_result":
            # No es una fase del run: es el resultado del paso anterior.
            # No pisa rp.phase ni notifica al bot (que ya recibe el
            # timeline por `message`).
            if output:
                _attach_output(tool, output, tool_call_id)
            return
        if phase in ("say", "steer") and message:
            # El "por qué" del modelo y las correcciones del humano: van al
            # timeline vivo pero NO pisan rp.phase (la fase real la marca la
            # tool o el writing que viene atrás) ni notifican al bot, que
            # solo entiende pasos de tool.
            rp.last_activity_at = time.monotonic()
            _push_step(phase, message)
            return
        if phase == "heartbeat":
            # Latido del watchdog (2026-07-20b): el run sigue vivo pero
            # el modelo no emitió nodes (contexto largo ⇒ respuestas
            # lentas). NO refresca last_activity_at (mentiría el idle_s
            # de /status) ni pisa la fase real; solo avisa al bot/UI
            # para que el hilo de Discord/web no parezca muerto.
            if notify is not None:
                elapsed = round(time.monotonic() - rp.started_at)
                try:
                    await send_progress(
                        agent_id=f"chat:{chat_id}",
                        kind="progress",
                        message=(
                            f"⏳ sigue trabajando ({rp.phase}, "
                            f"{rp.tool_calls} tools, {elapsed}s)"),
                        metadata={
                            "chat_id": chat_id,
                            "target": target,
                            "phase": rp.phase,
                            "heartbeat": True,
                            "tool": rp.last_tool,
                            "tool_calls": rp.tool_calls,
                            "elapsed_s": elapsed,
                        },
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("notify heartbeat falló: %r", e)
            return
        rp.phase = phase
        rp.last_activity_at = time.monotonic()
        if tool is not None:
            rp.last_tool = tool
        if tool_calls is not None:
            rp.tool_calls = tool_calls
        # Notify best-effort al bot (Fase 1 B): un "progress" por tool
        # call. El ModelResponseNode que no pidió tools NO notifica
        # (evita spam — solo el LLM pensando). `message` es la línea
        # legible que arma run_expert (📄 leyó `x`, ✏️ editó `y`…); si
        # no vino, caemos al genérico. `diff` (solo edit_file) va en
        # metadata para que el bot lo postee aparte del timeline.
        # Fase 3b: guardar el paso rico en el snapshot ANTES del notify —
        # el web lo lee por /experts/status aunque el bot de Discord esté
        # caído (notify es best-effort y puede fallar).
        if phase == "tool_call" and tool:
            _push_step("tool_call", message or f"🔧 {tool}", tool=tool,
                       diff=diff, cmd=cmd, tool_call_id=tool_call_id)

        if notify is not None and phase == "tool_call" and tool:
            meta = {
                "chat_id": chat_id,
                "target": target,
                "phase": phase,
                "tool": tool,
                "tool_calls": rp.tool_calls,
                "elapsed_s": round(time.monotonic() - rp.started_at, 2),
            }
            if diff:
                meta["diff"] = diff
            try:
                await send_progress(
                    agent_id=f"chat:{chat_id}",
                    kind="progress",
                    message=message or f"🔧 {tool}",
                    metadata=meta,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("notify progress falló: %r", e)

    # Compartir la cola real: /experts/steer agrega mensajes durante el run.
    _cb.steer = rp.steer
    return _cb
