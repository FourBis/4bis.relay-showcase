"""Consultas de solo texto y sugerencias de continuación."""
from __future__ import annotations
import asyncio
import json
import logging
import re
import time
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded
from typing import Any
from . import config, expert_models

logger = logging.getLogger("relay.experts")


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
    model = expert_models.build_model(spec)

    _global_to = None
    if db is not None:
        try:
            _global_to = await db.get_config("expert_timeout_s")
        except Exception:
            _global_to = None
    timeout = float(_global_to or config.expert_timeout_s())

    ponytail = await expert_models.read_ponytail()
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
