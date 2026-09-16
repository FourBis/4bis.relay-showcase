"""Memoria conversacional — ADR-026 (compactación) y ADR-027 (retrieval).

Al cerrar una conversación (ADR-025) se destila en tres artefactos:
  1. `summary` (prosa): decisiones, contexto, preguntas abiertas.
     → fila `conversations.summary` + índice FTS5 `memories_fts`.
  2. `facts` (hechos atómicos): → tabla `facts`, append-only.
  3. `skill` (OPCIONAL, autoaprendizaje 2026-07-12): si la conversación
     contiene un procedimiento reusable, un borrador de SKILL.md →
     tabla `skill_drafts`, pendiente hasta que el usuario lo apruebe
     desde la Admin UI (recién ahí se escribe a ~/.copilot/skills y el
     SkillCache lo inyecta). NUNCA se instala solo: una skill mala se
     auto-amplifica en todos los pushes futuros.

El compactador es un Agent genérico y chico (NO el experto del
proyecto): destilar es una tarea distinta a razonar sobre el repo.
Modelo: `config.compactor_model_spec()` (FOURBIS_COMPACTOR_MODEL,
fallback FOURBIS_MODEL).

Retrieval de resúmenes: MANUAL (decisión del usuario 2026-07-08). Los
comandos `memoria`/`fact` (commands.py) y el campo `memory` de POST
/experts/run consultan; nada se inyecta solo.

Retrieval de FACTS: opt-in por proyecto (2026-08-20). Con
`defaults_json.facts_always_on` los hechos vigentes entran en las
instructions de cada run vía `build_facts_block` — el upgrade path que
ADR-027 dejó anotado. Se prendió porque la vía manual no se usó NUNCA
(cero `/fact` en `command_logs`) y los 536 hechos destilados no los leía
nadie salvo el propio compactador.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

from . import config
from .experts import build_model, structured_output_settings

logger = logging.getLogger("relay.memory")

COMPACT_TIMEOUT_S = 300.0
# El reintento en prosa no arma JSON ni llama tools y escupe 3 párrafos:
# con 120s sobra. Importa porque "compactar sin cerrar" (Admin UI) espera
# el resultado en el request — 300+300 lo dejaba colgado 10 minutos.
PLAIN_TIMEOUT_S = 120.0
# Cap del transcript que se manda al compactador. Si el hilo excede,
# se trunca lo MÁS VIEJO (el final de la conversación pesa más).
TRANSCRIPT_MAX_CHARS = 60_000

COMPACT_INSTRUCTIONS = """\
Eres un compactador de conversaciones técnicas. Recibes el transcript
de una conversación entre un usuario y un experto de un proyecto de
software. Destilalo en:

1. `summary`: un resumen en prosa (español, 1-3 párrafos) con las
   DECISIONES tomadas, el contexto que las motivó y las preguntas que
   quedaron abiertas. Omite saludos, tanteos y callejones sin salida.
2. `facts`: hechos atómicos y durables, uno por item, autocontenidos
   (entendibles sin leer la conversación). Ej: "SampleApp usa PostgreSQL",
   "el deploy de INVENTORYDEMO corre con publish.ps1". Si no hay hechos durables,
   lista vacía.
3. `skill`: SOLO si la conversación contiene un PROCEDIMIENTO reusable
   — una secuencia de pasos que serviría tal cual para tareas futuras
   similares (cómo deployar X, cómo diagnosticar Y, cómo configurar Z).
   Emitilo como borrador: `name` (kebab-case corto, ej "deploy-inventorydemo"),
   `description` (una línea: cuándo aplicarla) y `content` (markdown
   con los pasos, comandos y gotchas). Un resumen NO es una skill; si
   no hay procedimiento claro y repetible, deja `skill` en null. Es lo
   normal: la mayoría de las conversaciones no producen skill.
4. `obsolete_fact_ids`: si el transcript incluye un bloque "HECHOS
   VIGENTES" (hechos ya registrados, con su [id]), lista los ids de
   los que esta conversación CONTRADICE o reemplaza (la decisión
   cambió, la herramienta se migró, el dato quedó viejo). Solo
   contradicciones claras: ante la duda, no lo marques. Además NO
   repitas en `facts` un hecho que ya figura vigente.

No inventes nada que no esté en el transcript."""


PLAIN_SUMMARY_INSTRUCTIONS = """\
Eres un compactador de conversaciones técnicas. Recibes el transcript de
una conversación entre un usuario y un experto de un proyecto de
software. Devuelve SOLO el resumen en prosa (español, 1-3 párrafos) con
las DECISIONES tomadas, el contexto que las motivó y las preguntas que
quedaron abiertas. Omite saludos, tanteos y callejones sin salida. Texto
plano: sin JSON, sin encabezados, sin bloque de código.

No inventes nada que no esté en el transcript."""


PR_BODY_INSTRUCTIONS = """\
Escribes la descripción de un Pull Request leyendo el DIFF (no los
mensajes de commit: suelen decir solo "fix"). Salida en markdown,
español, sin bloque de código envolvente, con estas secciones:

## Qué cambió
Bullets por área/archivo relevante: qué hace ahora el código que antes
no hacía. Concreto (nombres de funciones, endpoints, flags), no "se
mejoró el manejo de errores".

## Por qué
1-3 líneas con el problema que resuelve, inferido del diff. Si no se
puede inferir, omite la sección entera.

## Cómo probarlo
Comandos o pasos concretos si el diff los sugiere (tests tocados,
endpoints, scripts). Si no hay nada claro, omite la sección.

## Riesgos
Solo si el diff toca migraciones, borrados, seguridad, concurrencia,
contratos públicos o config. Si no, omitila.

No inventes nada que no esté en el diff. Sé breve: es un PR, no un ADR."""

# Cap del diff que se manda al redactor. Si excede, se manda el
# --stat completo + el diff truncado (el stat da la foto global).
DIFF_MAX_CHARS = 60_000


async def describe_changes(diff: str, *, stat: str = "",
                           model_spec: str = "") -> str:
    """Redacta el body de un PR a partir del diff. "" si no hay diff o
    si el run falla (best-effort: el caller usa su fallback)."""
    if not diff.strip():
        return ""
    if len(diff) > DIFF_MAX_CHARS:
        diff = diff[:DIFF_MAX_CHARS] + "\n\n… [diff truncado]"
    prompt = f"ARCHIVOS TOCADOS:\n{stat}\n\nDIFF:\n{diff}" if stat else diff
    try:
        agent = Agent(
            build_model(model_spec or config.compactor_model_spec()),
            instructions=PR_BODY_INSTRUCTIONS,
        )
        result = await asyncio.wait_for(
            agent.run(prompt), timeout=COMPACT_TIMEOUT_S)
        return (result.output or "").strip()
    except Exception:  # noqa: BLE001 — best-effort
        logger.exception("redacción del body del PR falló (sigo con fallback)")
        return ""


class SkillDraft(BaseModel):
    """Borrador de skill destilado de la conversación (autoaprendizaje).

    Queda en `skill_drafts` hasta aprobación humana en la Admin UI."""
    name: str = Field(description="kebab-case corto, ej: deploy-inventorydemo")
    description: str = Field(description="Una línea: cuándo aplicar la skill")
    content: str = Field(description="Markdown con el procedimiento: pasos, comandos, gotchas")


class CompactionResult(BaseModel):
    """Output estructurado del compactador (ADR-026)."""
    summary: str = Field(description="Resumen en prosa: decisiones, contexto, preguntas abiertas")
    facts: list[str] = Field(default_factory=list, description="Hechos atómicos durables, uno por item")
    skill: SkillDraft | None = Field(
        default=None,
        description="Borrador de skill SOLO si hay un procedimiento reusable; null si no")
    obsolete_fact_ids: list[int] = Field(
        default_factory=list,
        description="Ids de HECHOS VIGENTES que esta conversación contradice o reemplaza; [] si ninguno")


def render_transcript(messages_json: str) -> str:
    """Historial pydantic-ai serializado → transcript plano legible.

    Best-effort: recorre `parts` y toma user-prompt/text; tool calls y
    retornos se omiten (ruido para el compactador). Cualquier shape
    inesperado se saltea sin romper.
    """
    try:
        messages = json.loads(messages_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(messages, list):
        return ""
    lines: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        for part in msg.get("parts", []):
            if not isinstance(part, dict):
                continue
            kind = part.get("part_kind", "")
            content = part.get("content", "")
            if not isinstance(content, str):
                continue
            if kind == "user-prompt" and content.strip():
                lines.append(f"[usuario] {content.strip()}")
            elif kind == "text" and content.strip():
                lines.append(f"[experto] {content.strip()}")
    text = "\n\n".join(lines)
    if len(text) > TRANSCRIPT_MAX_CHARS:
        # truncar lo más viejo, en frontera de línea
        text = text[-TRANSCRIPT_MAX_CHARS:]
        nl = text.find("\n")
        if nl > 0:
            text = text[nl + 1:]
        text = "(...transcript truncado, se conserva lo mas reciente...)\n" + text
    return text


# Un hecho que habla DE los hechos, no del proyecto. Sale de que
# `render_existing_facts` pega los vigentes al final del transcript: el
# compactador los lee como material a resumir y devuelve paráfrasis de lo
# que le mostramos. Los 4 casos de ejemplo que había en la DB (example-client,
# 20/8/2026), textuales:
#
#   Un hecho vigente establece que el timeout de 30000ms…
#   Otro hecho vigente señala que la cadena de timeouts…
#   Un hecho vigente menciona que el handler de Execute…
#   Los hechos vigentes indican que ApiM se comunica con…
#
# Son basura doble: ocupan lugar y encima envejecen mal (el "hecho
# vigente" que citan puede haberse marcado obsoleto). Se filtra la SALIDA
# y no solo el prompt porque el filtro tiene que aguantar un cambio de
# modelo o de redacción del bloque; la frase solo aparece porque la
# pusimos nosotros en la cabecera, así que un hecho legítimo no la usa.
_FACT_AUTORREF_RE = re.compile(
    r"\b(hechos?\s+vigentes?|hecho\s+anterior|estos\s+hechos)\b", re.IGNORECASE)


def es_fact_autorreferencial(fact: str) -> bool:
    """¿Este "hecho" habla de la lista de hechos en vez del proyecto?"""
    return bool(_FACT_AUTORREF_RE.search(fact or ""))


def render_existing_facts(facts: list[dict]) -> str:
    """Hechos vigentes → bloque "[id] hecho" para el prompt del
    compactador (detección de obsoletos + no re-emitir duplicados)."""
    lines = [f"[{f['id']}] {f['fact']}" for f in facts
             if f.get("id") is not None and (f.get("fact") or "").strip()]
    if not lines:
        return ""
    return ("--- HECHOS VIGENTES (marca en obsolete_fact_ids los que la "
            "conversación contradiga) ---\n" + "\n".join(lines))


async def compact_conversation(
    messages_json: str, *, model_spec: str = "",
    existing_facts: list[dict] | None = None,
) -> CompactionResult | None:
    """Corre el compactador sobre el historial. None si no hay nada
    que compactar (best-effort; el caller loggea lo que propague).

    `existing_facts` (filas de db.list_facts) se anexa al prompt para
    que el compactador detecte hechos obsoletos y no duplique.

    Si el output estructurado falla, cae a un resumen en prosa (ver
    abajo): devuelve un CompactionResult con `summary` y el resto en
    default. Peor caso: COMPACT_TIMEOUT_S + PLAIN_TIMEOUT_S."""
    transcript = render_transcript(messages_json or "")
    if not transcript.strip():
        return None
    facts_block = render_existing_facts(existing_facts or [])
    if facts_block:
        transcript = f"{transcript}\n\n{facts_block}"
    spec = model_spec or config.compactor_model_spec()
    model = build_model(spec)
    agent = Agent(
        model,
        instructions=COMPACT_INSTRUCTIONS,
        output_type=CompactionResult,
        model_settings=structured_output_settings(spec),
    )
    try:
        result = await asyncio.wait_for(
            agent.run(transcript), timeout=COMPACT_TIMEOUT_S)
        salida = result.output
        # Ver `es_fact_autorreferencial`: el compactador a veces parafrasea
        # los HECHOS VIGENTES que le mostramos en vez de destilar el hilo.
        descartados = [f for f in salida.facts if es_fact_autorreferencial(f)]
        if descartados:
            salida.facts = [f for f in salida.facts
                            if not es_fact_autorreferencial(f)]
            logger.info(
                "compactación: descarto %d hecho(s) autorreferencial(es), "
                "p.ej. %r", len(descartados), descartados[0][:100])
        return salida
    except (UnexpectedModelBehavior, ModelHTTPError) as e:
        # 2026-07-31: MiniMax-M3 devuelve a veces texto vacío ('\n\n') en
        # vez del JSON y agota los retries de pydantic-ai → la conversación
        # quedaba cerrada SIN summary para siempre. Perder facts/skill es
        # barato; perder el resumen no. Segundo intento sin output_type:
        # prosa plana, que es lo que el modelo sabe hacer sin fallar.
        #
        # El 400 entra por la misma puerta: es el provider rechazando el
        # REQUEST estructurado (DeepSeek v4 contesta "Thinking mode does
        # not support this tool_choice", y pydantic-ai fuerza tool_choice
        # cuando hay output_type — ver structured_output_settings). Un
        # 429/500 NO se reintenta: ahí el schema no tiene la culpa y el
        # segundo run falla igual, cobrando de nuevo.
        if isinstance(e, ModelHTTPError) and e.status_code != 400:
            raise
        logger.warning("compactación estructurada falló con %s (%s); "
                       "reintento en prosa plana", spec, type(e).__name__)
    result = await asyncio.wait_for(
        Agent(model, instructions=PLAIN_SUMMARY_INSTRUCTIONS).run(transcript),
        timeout=PLAIN_TIMEOUT_S)
    summary = (result.output or "").strip()
    return CompactionResult(summary=summary) if summary else None


# Presupuesto del puente: si el último turno pesa más que esto (en chars
# del JSON serializado), no se conserva — compactar para liberar contexto
# y arrastrar 60k del turno anterior sería contradictorio. Tunable por env
# para poder ajustarlo sin redeploy.
TAIL_MAX_CHARS = int(os.environ.get("FOURBIS_COMPACT_TAIL_MAX_CHARS", "24000"))


def build_compacted_history(summary: str, *, facts: list[str] | None = None,
                            previous_json: str = "") -> str:
    """Resumen → messages_json que REEMPLAZA el historial de un hilo vivo.

    Compactar sin cerrar (2026-07-22): el hilo sigue abierto, con su rama
    y su thread de Discord, pero arranca el próximo run con ~1k tokens de
    contexto en vez de los 80-100k que arrastraba. Se emite como un turno
    user + assistant (no como `instructions`) porque ese par sobrevive
    intacto a `_slim_history` y al replay de pydantic-ai.

    Puente al último turno (2026-08-15): si viene `previous_json`, se
    CONSERVAN los mensajes posteriores al último user prompt — o sea el
    turno vivo completo, con sus tool calls y sus results. Antes se tiraba
    todo y el resumen era el único puente: el turno siguiente a compactar
    arrancaba con ~1k de prosa y CERO working set (ni un archivo leído, ni
    un diff, ni un comando corrido), que es la sensación de amnesia que
    reportó el usuario. El corte en el último user prompt es el mismo que
    usa `_slim_history`, así que el pairing tool call↔return viaja cerrado.

    El puente se descarta si pesa más de `TAIL_MAX_CHARS`, si el historial
    no se puede parsear, o si no hay un turno posterior al último user
    prompt: en todos esos casos queda el comportamiento viejo
    (solo resumen), que es correcto aunque más pobre.
    """
    from pydantic_ai.messages import (
        ModelMessagesTypeAdapter, ModelRequest, ModelResponse, TextPart,
        UserPromptPart,
    )
    lines = [
        "[Resumen del hilo hasta acá — el historial completo se compactó "
        "para liberar contexto. Trabaja con esto como memoria del hilo.]",
        "",
        summary.strip(),
    ]
    if facts:
        lines += ["", "Hechos registrados del proyecto:"] + [
            f"- {f}" for f in facts]
    messages: list = [
        ModelRequest(parts=[UserPromptPart(content="\n".join(lines))]),
        ModelResponse(parts=[TextPart(
            content="Anotado: tengo el resumen del hilo. Seguimos desde acá.")]),
    ]
    messages += _tail_after_last_user_prompt(previous_json)
    return ModelMessagesTypeAdapter.dump_json(messages).decode("utf-8")


def _tail_after_last_user_prompt(previous_json: str) -> list:
    """Mensajes desde el último user prompt del historial. `[]` si no aplica.

    Best-effort total: cualquier problema (JSON corrupto, esquema que
    cambió, turno demasiado pesado) devuelve `[]` y la compactación queda
    como antes. Compactar nunca puede fallar por intentar conservar más.
    """
    if not (previous_json or "").strip():
        return []
    from pydantic_ai.messages import (
        ModelMessagesTypeAdapter, ModelRequest, UserPromptPart,
    )
    try:
        prev = list(ModelMessagesTypeAdapter.validate_json(previous_json))
        last_user = -1
        for i, m in enumerate(prev):
            if isinstance(m, ModelRequest) and any(
                    isinstance(p, UserPromptPart) for p in m.parts):
                last_user = i
        if last_user < 0 or last_user == len(prev) - 1:
            return []
        tail = prev[last_user:]
        size = len(ModelMessagesTypeAdapter.dump_json(tail))
        if size > TAIL_MAX_CHARS:
            logger.info(
                "compactación: el último turno pesa %d chars (> %d), "
                "no lo conservo", size, TAIL_MAX_CHARS)
            return []
        return tail
    except Exception as e:  # noqa: BLE001 — el puente es un extra
        logger.warning(
            "compactación: no pude conservar el último turno (%r)", e)
        return []


def build_memory_block(hits: list[dict]) -> str:
    """Hits de search_memories → bloque markdown para las instructions.

    Mismo patrón que skills (ADR-010) / workspace (ADR-011) / git
    (ADR-020): el relay aporta hechos del entorno, el LLM decide.
    """
    summaries = [h.get("summary", "").strip() for h in hits]
    summaries = [s for s in summaries if s]
    if not summaries:
        return ""
    lines = ["## Memoria de conversaciones previas", ""]
    for s in summaries:
        lines.append(f"- {s}")
    return "\n".join(lines)


# Tope del bloque de facts, en caracteres. ~4 chars por token, así que
# 8000 son ~2000 tokens: caro pero no absurdo al lado de un system prompt
# que ya ronda los 6-8k. El proyecto más cargado (example-client, 128 facts,
# 22k chars) entra recortado a los ~46 más recientes.
FACTS_BLOCK_MAX_CHARS = 8000


def build_facts_block(facts: list[dict], *,
                      max_chars: int = FACTS_BLOCK_MAX_CHARS) -> str:
    """Hechos vigentes → bloque para las instructions del experto.

    Existe porque los facts eran write-only (2026-08-20): se destilaban
    536 en 11 proyectos y no los leía NADIE. La única lectura automática
    se los pasaba de vuelta al compactador para deduplicar — un lazo
    cerrado. La vía manual que dejó ADR-027 (`/fact <target>`) nunca se
    invocó ni una vez en toda la historia del relay, y aunque se
    invocara solo imprime en el chat: `build_memory_block` arma sus
    líneas con `summary` y nunca miró la tabla `facts`.

    `facts` viene de `db.list_facts` (ya ordenado por `created_at DESC`,
    vigentes primero). Se recorta por `max_chars` quedándose con los MÁS
    RECIENTES: un hecho viejo tiene más chances de estar desactualizado,
    y el corte tiene que ser predecible para no mover el prefijo del
    prompt más de lo necesario (ver `build_instructions` sobre por qué
    importa la estabilidad).

    El encuadre no es decorativo: sin él el modelo trata los hechos como
    órdenes. Son observaciones de conversaciones pasadas, que pueden
    haber quedado viejas, y el repo de HOY siempre gana.
    """
    limpios = [(f.get("fact") or "").strip() for f in facts]
    limpios = [f for f in limpios if f]
    if not limpios:
        return ""
    cabecera = (
        "## Hechos del proyecto (de conversaciones anteriores)\n\n"
        "Observaciones destiladas al cerrar hilos previos, de lo más "
        "reciente a lo más viejo. Son PISTAS, no órdenes ni verdad "
        "actual: si el repo dice otra cosa, gana el repo. Úsalas para no "
        "re-descubrir lo mismo; verifica antes de apoyarte en una.\n")
    lines: list[str] = []
    usados = len(cabecera)
    for f in limpios:
        item = f"- {f}"
        if usados + len(item) + 1 > max_chars:
            break
        lines.append(item)
        usados += len(item) + 1
    if not lines:
        return ""
    if len(lines) < len(limpios):
        lines.append(f"- (…{len(limpios) - len(lines)} hechos más viejos "
                     f"omitidos por tamaño)")
    return cabecera + "\n" + "\n".join(lines)


def format_memory_hits(hits: list[dict]) -> str:
    """Hits → texto para Discord (comando `memoria`)."""
    out = []
    for h in hits:
        s = (h.get("summary") or "").strip()
        if not s:
            continue
        cid = (h.get("conversation_id") or "")[:8]
        out.append(f"• {s}  `[{cid}]`" if cid else f"• {s}")
    return "\n".join(out)
