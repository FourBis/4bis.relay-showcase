"""Límites de herramientas, visibilidad y vigilancia de actividad."""
from __future__ import annotations
import asyncio
import contextlib
import dataclasses
import logging
import time
import uuid
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import BinaryContent
from pydantic_ai.toolsets import WrapperToolset
from typing import Optional
from . import attachments as attachments_mod, config, expert_history

logger = logging.getLogger("relay.experts")
try:
    from mcp.shared.exceptions import McpError
except ImportError:  # pragma: no cover
    class McpError(Exception):  # type: ignore[no-redef]
        """Placeholder: nunca se levanta si no hay MCP instalado."""



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
            expert_history._elide_old_tool_returns(ctx.messages)
            expert_history._elide_old_response_parts(ctx.messages)
        except Exception as e:  # noqa: BLE001 — la elisión nunca rompe un run
            logger.warning("elisión de historial falló: %r", e)
        result = expert_history._cap_tool_result(result, tool_name=name)
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
