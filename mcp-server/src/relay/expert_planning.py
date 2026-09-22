"""Contexto del plan, detección de continuación y herramientas de razonamiento."""
from __future__ import annotations
import json
import logging
import os
import re
import time
from typing import Any, Optional
from . import config, expert_toolsets, expert_verdicts

logger = logging.getLogger("relay.experts")


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
    lines = [ln for ln in expert_verdicts._clean_stage_output(plan).splitlines() if ln.strip()]
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
    for linea in expert_verdicts._clean_stage_output(plan or "").splitlines():
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
    lineas = [ln for ln in expert_verdicts._clean_stage_output(plan).splitlines() if ln.strip()]
    return any(_PASO_RE.match(ln) for ln in lineas)


def _format_decomposition(plan: str) -> str:
    """`DEMASIADO_GRANDE: ...` → mensaje de propuesta para el humano.

    Conserva el encuadre del planificador de prompts grandes que vivía
    en server.py (Opción 4): deja claro que NO se ejecutó nada y pide
    que el humano elija por dónde empezar.
    """
    body = expert_verdicts._clean_stage_output(plan)
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
            return [expert_toolsets.OptionalToolset(wrapped=expert_toolsets.CappedToolset(
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
# por presupuesto (sample-app, 23/8: 2,79 M tokens en dos runs, cero salida
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
