"""Server domain handlers extracted from the composition entrypoint."""
from __future__ import annotations

import json


from .server_common import logger
from . import experts
from . import memory
from .db import Database
from .server_conversation_jobs import _COMPACTING
# Fases sin `message`: ruido de heartbeat/telemetría, no razonamiento.
# `thinking` y `writing` son "sigue vivo", no "pensó esto".
_STEP_PHASES = {"say": "say", "tool_call": "tool", "steer": "steer"}
_STEP_MSG_CAP = 2000
_STEP_MAX = 120


def _steps_from_progress(raw: str | None) -> list[dict]:
    """`chats.progress_events` → los pasos que la UI muestra colapsados.

    El razonamiento del experto (fase `say`) y sus tool calls YA se
    guardaban acá run a run, pero nadie los servía: el .md solo tiene
    `## Usuario` / `## Respuesta`, así que al recargar el hilo todo el
    proceso desaparecía y quedaba la respuesta sola sin el cómo.

    Best-effort: JSON roto o formato inesperado ⇒ [] (el hilo se ve como
    antes, nunca 500). Capeado: un run largo son 200+ eventos y esto
    viaja en el mismo response que los turnos.
    """
    if not raw:
        return []
    try:
        events = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    if not isinstance(events, list):
        return []
    steps: list[dict] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        # `tool_result` no es un paso: es la salida de la terminal, y va
        # colgada del paso de SU tool. Mismo criterio que el timeline en
        # vivo (`_attach_output` en experts.py): el paso MÁS VIEJO de esa
        # tool que todavía no tiene salida. Así el hilo releído se ve
        # igual que el hilo en vivo, que es la regla de esta función.
        #
        # 2026-09-04: esto buscaba de atrás para adelante, y con dos
        # llamadas a la misma tool en un turno cruzaba las salidas —el
        # primer resultado se colgaba del último paso y viceversa—.
        # Medido: pydantic-ai entrega los `ToolReturnPart` en el ORDEN
        # en que se pidieron las tools, así que el que corresponde es el
        # primero sin salida (FIFO). No se veía antes porque el turno
        # dejaba un solo paso y los resultados de más se descartaban.
        if e.get("phase") == "tool_result":
            out = e.get("output")
            if isinstance(out, str) and out:
                for step in steps:
                    if (step.get("kind") == "tool"
                            and step.get("tool") == e.get("tool")
                            and (not e.get("tool_call_id") or
                                 step.get("tool_call_id") == e["tool_call_id"])
                            and not step.get("output")):
                        step["output"] = out[:_STEP_MSG_CAP * 3]
                        break
            continue
        kind = _STEP_PHASES.get(e.get("phase") or "")
        msg = e.get("message")
        if not kind or not isinstance(msg, str) or not msg.strip():
            continue
        step = {"kind": kind, "message": msg[:_STEP_MSG_CAP]}
        if e.get("tool"):
            step["tool"] = str(e["tool"])
        if e.get("tool_call_id"):
            step["tool_call_id"] = str(e["tool_call_id"])
        # El diff de edit_file es lo que hace que valga la pena abrir el
        # bloque; se capea igual que el mensaje pero más generoso.
        if isinstance(e.get("diff"), str):
            step["diff"] = e["diff"][:_STEP_MSG_CAP * 3]
        # El comando entero de la shell (el encabezado solo lleva la
        # primera línea recortada).
        if isinstance(e.get("cmd"), str) and e["cmd"]:
            step["cmd"] = e["cmd"][:_STEP_MSG_CAP]
        steps.append(step)
        if len(steps) >= _STEP_MAX:
            break
    return steps


def _cap_text(s: str, cap: int) -> dict:
    """Capa un string a `cap` chars. Devuelve dict listo para mergear.

    Mantiene `text` como key principal para retrocompatibilidad con
    callers que esperan `{text: ..., truncated: ..., total_chars: ...}`.
    El endpoint luego splatea estos campos sobre el turno con
    `content=text` (key pública del contrato).

    Si el original es <= cap, devuelve tal cual con truncated=False.
    Si se cortó, agrega sufijo claro para que el usuario sepa que
    falta contenido y cómo pedirlo (?content_cap=N).
    """
    if not s or len(s) <= cap:
        return {"text": s or "", "truncated": False,
                "total_chars": len(s or "")}
    cut = s[:cap]
    last_nl = cut.rfind("\n")
    if last_nl > cap // 2:
        cut = cut[:last_nl]
    return {
        "text": cut + f"\n\n… [truncado, {len(s)} chars totales. "
                      f"Sube el cap con ?content_cap={max(cap * 2, cap + 1000)}]",
        "truncated": True,
        "total_chars": len(s),
    }


async def compact_live_conversation(db: Database, conv: dict) -> dict:
    """Compacta un hilo ABIERTO sin cerrarlo (2026-07-22).

    Destila el historial a un resumen y lo REEMPLAZA como historial: la
    conversación sigue siendo la misma (misma rama, mismo hilo de
    Discord, mismo id), pero el próximo run arranca con ~1k tokens en
    vez de arrastrar 80-100k. Es la alternativa a `cerrar` + `nuevo`
    cuando el tema no cambió y solo molesta el peso del contexto.

    Los hechos destilados se guardan (valen igual), pero NO se toca
    `conversations.summary` ni el índice FTS5: esos son el artefacto del
    cierre, y escribirlos acá haría que el `close` posterior se saltee
    la compactación final (guard de idempotencia).

    Devuelve {ok, before, after, summary, facts} — el caller reporta.
    """
    conv_id = conv["id"]
    raw = conv.get("messages_json") or ""
    before = await experts.context_usage_db(db, raw)
    if conv_id in _COMPACTING:
        return {"ok": False, "error": "ya hay una compactación en curso"}
    _COMPACTING.add(conv_id)
    try:
        existing = await db.list_facts(conv["project_slug"], limit=100)
        result = await memory.compact_conversation(raw, existing_facts=existing)
        if result is None or not result.summary.strip():
            return {"ok": False, "error": "el compactador no devolvió resumen"}
        n_facts = await db.add_facts(
            conv["project_slug"], result.facts, source_conversation=conv_id)
        shown = {f["id"] for f in existing}
        obsolete = [i for i in result.obsolete_fact_ids if i in shown]
        if obsolete:
            await db.supersede_facts(
                obsolete, conv["project_slug"], superseded_by=conv_id)
        # `previous_json` (2026-08-15): la compactación conserva el último
        # turno además del resumen. Sin eso, el turno siguiente arrancaba
        # con la prosa y cero working set — ver la nota en
        # `build_compacted_history`.
        new_json = memory.build_compacted_history(
            result.summary, facts=result.facts, previous_json=raw)
        await db.save_conversation_messages(conv_id, new_json, expected=raw)
        logger.info(
            "compactación en vivo conv=%s: %d → %d chars de historial "
            "(%s → sin usage todavía), facts=%d",
            conv_id[:8], len(raw), len(new_json),
            f"{before['base_tokens']} tok" if before else "sin medir", n_facts)
        return {"ok": True, "before": before, "summary": result.summary,
                "facts": n_facts,
                "chars_before": len(raw), "chars_after": len(new_json)}
    except Exception as e:  # noqa: BLE001 — el hilo queda intacto si falla
        logger.exception("compactación en vivo de conv=%s falló", conv_id[:8])
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        _COMPACTING.discard(conv_id)
