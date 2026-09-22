"""Cierre de una corrida: historial recuperable, uso y avisos visibles del resultado."""
import logging
import time
from pydantic_ai.messages import ModelMessagesTypeAdapter
from . import attachments as attachments_mod, expert_context, expert_history, expert_stages

logger = logging.getLogger("relay.experts")

def finish_run(
    state, *, image_artifacts, bitacora, legs, cap_rounds,
    provider_retries, steers, steer_texts, _q_state,
):
    duration_ms = int((time.monotonic() - state.t0) * 1000)

    if not state.messages_json:
        logger.warning(
            "run_expert: messages_json vacío al finalizar (chat seguirá sin "
            "historial persistido para replay)")

    if not state.output_text:
        _fb, _ph = expert_context._synthesize_no_final_text(
            state.messages_json, current_phase=state.last_phase)
        if _fb:
            state.output_text = _fb
            state.last_phase = _ph
            logger.info(
                "run_expert: output vacío con tools ejecutadas — "
                "fallback 'no_final_text' aplicado (output=%d chars)",
                len(_fb))

    if (state.last_phase not in expert_context._CUT_PHASES and not state.tool_calls_count
            and state.output_text.rstrip().rstrip("*").endswith(":")):
        state.last_phase = "announced_no_tools"
        logger.warning(
            "run_expert: el modelo anunció una acción y terminó sin ejecutar "
            "ninguna tool — historial con narración decapitada o demasiado "
            "pesado; `compactar` el hilo")

    if state.last_phase == "off_plan" and state.messages_json:
        try:
            _todos = list(ModelMessagesTypeAdapter.validate_json(state.messages_json))
            state.messages_json = expert_history._dump_messages(
                expert_history._slim_history(_todos, corte=len(_todos)))
        except Exception as e:  # noqa: BLE001 — nunca voltear el run por esto
            logger.warning("off_plan: no pude podar el working set (%r)", e)

    if image_artifacts:
        state.output_text += "\n\n" + attachments_mod.generated_markdown(image_artifacts)
    if state.task_usage is not None:
        # El SDK acumula toda la continuación para imponer un único presupuesto.
        state.usage = state.task_usage
        state.tokens_in_prev = state.tokens_out_prev = state.cache_prev = 0
    return {
        "content": state.output_text,
        "image_artifacts": image_artifacts,
        "model": state.spec,
        "tokens_in": (
            (state.usage.input_tokens or 0) + state.tokens_in_prev if state.usage
            else (state.tokens_in_prev or None)),
        "tokens_out": (
            (state.usage.output_tokens or 0) + state.tokens_out_prev if state.usage
            else (state.tokens_out_prev or None)),
        "cache_read_tokens": (
            (state.usage.cache_read_tokens or 0) + state.cache_prev if state.usage
            else (state.cache_prev or None)),
        "tool_calls": (
            state.usage.tool_calls
            if legs == 1 and not (cap_rounds or provider_retries or steers) and state.usage
            and state.usage.tool_calls is not None
            else state.tool_calls_count),
        "duration_ms": duration_ms,
        "messages_json": state.messages_json,
        "phase_at_end": state.last_phase,
        "last_tool": state.last_tool_name,
        "bitacora_json": bitacora.volcar(),
        "plan_steps_done": {str(k): v for k, v in bitacora.pasos.items()},
        "legs": legs,  # tandas de presupuesto usadas (1 = sin auto-continue)
        "steers": steers,  # correcciones que metió el humano en vivo
        "steer_texts": steer_texts,
        "progress_events": state.progress_events,
        "tool_meter": expert_context.summarize_tool_meter(state.tool_meter, state.meter_turn),
        "question_id": _q_state.get("asked", ""),
    }


def finish_staged_result(
    result, *, doc, verdict, feedback, phase, rondas,
    corte_por_presupuesto, sin_trabajo,
):
    if doc:
        result["content"] = f"{(result.get('content') or '').rstrip()}\n\n---\n\n{doc}"
        if result.get("messages_json"):
            result["messages_json"] = expert_stages._merge_doc_into_history(
                result["messages_json"], doc)

    if verdict == "needs_human" and result.get("content"):
        feedback_short = (feedback or "").strip()[:200]
        result["content"] = (
            f"⚠️ El verificador marcó este run como `needs_human`: "
            f"{feedback_short}\n\n"
            f"{result['content']}"
        )
    if verdict == "needs_more" and result.get("content"):
        feedback_short = (feedback or "").strip()[:200]
        intentos = len(rondas)
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
        result["content"] = (
            f"🧭 **El verificador marcó este run como `off_plan`.** Lo hecho "
            f"está guardado, pero NO es lo que pedía el plan: revísalo "
            f"antes de darlo por bueno.\n\n"
            f"> {(feedback or '').strip()[:200]}\n\n"
            f"{result['content'].lstrip()}"
        )
    elif verdict == "complete" and len(rondas) > 1 and result.get("content"):
        result["content"] = (
            f"{result['content'].rstrip()}\n\n"
            f"_🔁 Cerrado en {len(rondas)} pasadas: el verificador pidió "
            f"seguir y el ejecutor continuó solo._"
        )
    elif corte_por_presupuesto and result.get("content"):
        result["content"] = (
            f"{result['content'].rstrip()}\n\n"
            f"_⏳ Corté por el presupuesto del pedido en la pasada "
            f"{len(rondas)}: lo hecho está guardado y falta lo que pedía "
            f"el verificador. Responde **continúa** para seguir._"
        )
    elif not verdict and len(rondas) > 1 and result.get("content"):
        result["content"] = (
            f"{result['content'].rstrip()}\n\n"
            f"_🔁 Este turno tomó {len(rondas)} pasadas._"
        )

    if sin_trabajo and result.get("content"):
        result["content"] = (
            "⚠️ Este turno **no ejecutó ninguna herramienta**: describió el "
            "trabajo sin hacerlo, y al pedírselo de nuevo tampoco lo hizo. "
            "Lo que sigue es una propuesta, no un resultado — nada de esto "
            "está verificado ni aplicado.\n\n"
            f"{result['content']}")
        result["sin_trabajo"] = True
    return result
