"""Cierre de una corrida: historial recuperable, uso y avisos visibles del resultado."""
from copy import deepcopy
import logging
import time
from pydantic_ai.messages import ModelMessagesTypeAdapter
from . import attachments as attachments_mod, expert_context, expert_history, expert_stages

logger = logging.getLogger("relay.experts")


def recovery_seed(result=None, *, messages_json="", bitacora_json="", image_artifacts=None):
    """Keep recoverable work, never a prior executor's usage, for preparation."""
    result = result or {}
    value = {key: result[key] for key in (
        "content", "messages_json", "bitacora_json", "model", "last_tool",
        "image_artifacts", "plan_steps_done",
    ) if result.get(key)}
    if messages_json:
        value["messages_json"] = messages_json
    if bitacora_json:
        value["bitacora_json"] = bitacora_json
    if image_artifacts:
        value["image_artifacts"] = {**value.get("image_artifacts", {}), **image_artifacts}
    value["_executor_attempted"] = False
    return deepcopy(value)


def merge_recovery_snapshot(completed, partial):
    """An empty child snapshot cannot erase the last recoverable work.

    This merge is for rescue only. A normal, empty model result remains empty;
    old content must never stand in for a new successful answer.
    """
    value = {**completed, **partial}
    if "_executor_attempted" not in partial:
        value.pop("_executor_attempted", None)
    for key in ("content", "messages_json", "bitacora_json", "model", "last_tool"):
        if not partial.get(key) and completed.get(key):
            value[key] = completed[key]
    previous, current = completed.get("content") or "", partial.get("content") or ""
    if previous and current and not current.startswith(previous):
        value["content"] = previous
        if not previous.endswith(current):
            # A live diagnostic/partial can grow after a completed prefix. Keep
            # that prefix once, even when thinking/steer changes its phase.
            boundary = previous.find("\n\n")
            while boundary >= 0 and not current.startswith(previous[boundary + 2:]):
                boundary = previous.find("\n\n", boundary + 2)
            value["content"] = (previous[:boundary + 2] + current if boundary >= 0
                                else f"{previous}\n\n{current}")
    for key in ("image_artifacts", "plan_steps_done"):
        value[key] = {**(completed.get(key) or {}), **(partial.get(key) or {})}
    return deepcopy(value)


def finish_run(
    state, *, image_artifacts, bitacora, legs, cap_rounds,
    provider_retries, steers, steer_texts, _q_state, recovery=None,
):
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
    value = snapshot_run(state, image_artifacts=image_artifacts, bitacora=bitacora,
        legs=legs, cap_rounds=cap_rounds, provider_retries=provider_retries,
        steers=steers, steer_texts=steer_texts, _q_state=_q_state)
    if recovery and state.last_phase in expert_context._CUT_PHASES | {"error", "timeout", "no_result"}:
        return merge_recovery_snapshot(recovery, value)
    return value


def snapshot_run(
    state, *, image_artifacts, bitacora, legs, cap_rounds,
    provider_retries, steers, steer_texts, _q_state,
):
    """Copy recoverable state without finishing/mutating the live executor.

    A shared task_usage is authoritative for every leg; otherwise usage is the
    current, not-yet-carried leg plus prior legs. No SDK/state objects escape.
    This is safe to call repeatedly before a supervisor or continuation await.
    """
    shared = state.task_usage is not None
    usage = state.task_usage if shared else state.usage
    tokens_in_prev = 0 if shared else state.tokens_in_prev
    tokens_out_prev = 0 if shared else state.tokens_out_prev
    cache_prev = 0 if shared else state.cache_prev
    return deepcopy({
        "content": state.output_text,
        "image_artifacts": image_artifacts,
        "model": state.spec,
        "tokens_in": (
            (usage.input_tokens or 0) + tokens_in_prev if usage is not None
            else (tokens_in_prev or None)),
        "tokens_out": (
            (usage.output_tokens or 0) + tokens_out_prev if usage is not None
            else (tokens_out_prev or None)),
        "cache_read_tokens": (
            (usage.cache_read_tokens or 0) + cache_prev if usage is not None
            else (cache_prev or None)),
        "tool_calls": (
            # An in-flight tool can be observed before the SDK counts it.
            # Keep that evidence without adding the two counters together.
            max(usage.tool_calls, state.tool_calls_count)
            if legs == 1 and not (cap_rounds or provider_retries or steers) and usage is not None
            and usage.tool_calls is not None
            else state.tool_calls_count),
        "requests": (
            usage.requests if shared else
            usage.requests if legs == 1 and not (
                cap_rounds or provider_retries or steers) and usage is not None
            else None),
        "duration_ms": int((time.monotonic() - state.t0) * 1000),
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
    })


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
            f"_🔁 Cerrado en {len(rondas)} pasadas: el ejecutor continuó "
            f"desde el historial guardado._"
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


def finish_unstaged_result(result):
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


def planned_result(propuesta, planner_spec, historial, plan, planner_usage, stage_errors):
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
        "stage_usage": {"planner": planner_usage},
        "stage_errors": stage_errors,
    }


def staged_recovery_usage(result, partial, acum, measured, executor_requests_used, requests_known):
    value = merge_recovery_snapshot(result, partial or {})
    for key in ("tokens_in", "tokens_out", "cache_read_tokens", "tool_calls", "duration_ms", "legs"):
        extra = (partial or {}).get(key)
        value[key] = (acum[key] + (extra or 0)
                      if key in measured or extra is not None else None)
    partial_requests_known = (partial is None or partial.get("requests") is not None
                             or partial.get("_executor_attempted") is False)
    value["requests"] = (executor_requests_used + int((partial or {}).get("requests") or 0)
                         if requests_known and partial_requests_known else None)
    value["progress_events"] = list(acum["progress_events"]) + list((partial or {}).get("progress_events") or [])
    return value
