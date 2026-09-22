"""Coordinación de etapas y reintentos a partir del veredicto."""
from __future__ import annotations
import json
import logging
import time
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from typing import Any
from . import (
    config,
    expert_consult,
    expert_history,
    expert_models,
    expert_planning,
    expert_result,
    expert_runner,
    expert_stage_prompts,
    expert_stages,
    expert_verdicts,
)
from .bitacora import Bitacora
from .task_workspace import resolved_project

logger = logging.getLogger("relay.experts")


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
    if (db := kwargs.get("db")) is not None and kwargs.get("conversation_id") \
            and not project.get("_task_id"):
        project = await resolved_project(db, project, kwargs["conversation_id"])
    defaults = project.get("defaults_json") or {}
    kwargs.setdefault("image_artifacts", {})
    limite_pedido = min(kwargs.get("deadline_pedido") or float("inf"),
        time.monotonic() + float(defaults.get("request_timeout") or config.expert_request_timeout_s()))
    kwargs["deadline_pedido"] = limite_pedido
    etapas = kwargs.pop("stage_models", None) or {}
    enabled = kwargs.pop("three_stage", None)
    if enabled is None:
        enabled = defaults.get("three_stage", True)
    doc_enabled = kwargs.pop("documenter", None)
    if doc_enabled is None:
        doc_enabled = defaults.get("documenter", True)
    ver_enabled = kwargs.pop("verifier", None)
    if ver_enabled is None:
        ver_enabled = defaults.get("verifier", True)
    if not enabled:
        result = await expert_runner.run_expert(project, user, **kwargs)
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
    ponytail = await expert_models.read_ponytail()
    planner_spec = (etapas.get("planner") or defaults.get("planner_model")
                    or config.planner_model_spec())
    verifier_spec = (etapas.get("verifier") or defaults.get("verifier_model")
                     or config.verifier_model_spec())
    documenter_spec = (etapas.get("documenter")
                       or defaults.get("documenter_model")
                       or config.documenter_model_spec())

    history_json = kwargs.get("message_history_json") or ""
    is_followup = bool(history_json)
    es_continuacion = is_followup and expert_planning._es_continuacion(user)
    stage_errors: dict[str, str] = {}
    planner_toolsets: list[Any] = []
    if defaults.get("planner_reasoning", True) and not es_continuacion:
        planner_toolsets = await expert_planning._reasoning_toolset(
            kwargs.get("db"), project, kwargs.get("mcp_pool"))
    plan, planner_usage, planner_err = await expert_stages._run_planner(
        user=user, project=project, model_spec=planner_spec,
        ponytail=ponytail, on_progress=on_progress,
        is_followup=is_followup, es_continuacion=es_continuacion,
        history_recap=expert_planning._history_recap(history_json),
        toolsets=planner_toolsets,
        deadline=limite_pedido,
    )
    if planner_err:
        stage_errors["planner"] = planner_err
    if (_db := kwargs.get("db")) is not None and kwargs.get("chat_id") \
            and plan.strip():
        try:
            await _db.set_chat_stages(kwargs["chat_id"], json.dumps(
                {"plan": plan[:4000], "planner_model": planner_spec},
                ensure_ascii=False))
        except Exception as _e:  # noqa: BLE001
            logger.debug("no pude adelantar el plan a la DB: %r", _e)
    signal = expert_planning._plan_signal(plan)
    trivial = signal == "trivial"

    if not signal and plan.strip() and not expert_planning._plan_utilizable(plan):
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

    if signal == "too_large" and not es_continuacion:
        logger.info("pedido grande: propongo descomposición sin ejecutar")
        propuesta = expert_planning._format_decomposition(plan)
        historial = expert_history._dump_messages([
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
            "stage_usage": {"planner": planner_usage},
            "stage_errors": stage_errors,
        }

    plan_block = ""
    if plan and not trivial:
        plan_block = (
            "## Plan a ejecutar (del planificador)\n"
            f"{plan}\n\n"
            "Sigue estos pasos en orden. Si descubris que un paso es\n"
            "incorrecto, o que el pedido del humano es mas simple que el\n"
            "plan, indicarlo en la respuesta final y continuar igual: el\n"
            "plan es una guia, no un contrato.\n"
            "Al terminar cada paso, marcalo con `plan_step_done(nro)`."
        )
    elif trivial:
        plan_block = (
            "## Plan a ejecutar (del planificador)\n"
            f"{plan}\n\n"
            "Pedido trivial: resuélvelo directo, sin exploración."
        )

    extra_orig = kwargs.get("system_extra") or ""
    if plan_block:
        kwargs["system_extra"] = (
            f"{plan_block}\n\n{extra_orig}" if extra_orig else plan_block)

    mid_verdicts: list[dict] = []
    stages_events: list[dict] = []
    user_original = user

    async def _supervisar(parcial: dict) -> str:
        v_tuple = await expert_stages._run_verifier(
            user=user_original, plan=plan,
            executor_result={
                **parcial,
                "tool_calls_summary": expert_consult._summarize_tool_calls_from_messages(
                    parcial.get("messages_json") or ""),
            },
            model_spec=verifier_spec, ponytail=ponytail,
            on_progress=on_progress, deadline=limite_pedido,
        )
        if len(v_tuple) == 5:
            v, f, u, err = v_tuple[:4]
        else:
            v, f, u, err = v_tuple
        mid_verdicts.append({
            "leg": parcial.get("leg"), "verdict": v, "feedback": f,
            "usage": u, "error": err,
        })
        if err:
            return ""
        if v != "off_plan":
            return ""
        return f or "el verificador marcó off_plan sin dar detalle"

    if ver_enabled and plan.strip():
        kwargs["on_leg_boundary"] = _supervisar

    max_rondas = int(
        defaults.get("verifier_rounds")
        if defaults.get("verifier_rounds") is not None
        else config.expert_verifier_rounds())
    rondas: list[dict] = []
    forzadas = 0
    interrupciones = 0
    MAX_INTERRUPCIONES = 2
    sin_trabajo = False
    doc = ""
    verifier_usage: dict = {}
    verifier_corrio = False
    documenter_usage: dict = {}
    verdict, feedback = "", ""
    acum = {"tokens_in": 0, "tokens_out": 0, "cache_read_tokens": 0, "tool_calls": 0,
            "duration_ms": 0, "legs": 0, "progress_events": []}
    corte_por_presupuesto = False
    result: dict = {}

    bitacora = Bitacora.cargar(kwargs.get("bitacora_json") or "") \
        if kwargs.get("bitacora_json") else Bitacora()

    while True:
        if result.get("bitacora_json"):
            kwargs["bitacora_json"] = result["bitacora_json"]
        result = await expert_runner.run_expert(project, user, **kwargs)

        if result is None:
            result = {
                "content": "", "model": planner_spec,
                "tokens_in": None, "tokens_out": None,
                "tool_calls": 0, "duration_ms": 0,
                "messages_json": "", "phase_at_end": "no_result",
                "last_tool": None, "legs": 1, "steers": 0,
                "steer_texts": [], "progress_events": [],
            }

        interrupcion = expert_verdicts._parse_executor_interruption(result.get("content") or "")
        if interrupcion is not None and interrupciones >= MAX_INTERRUPCIONES:
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
            planner_history = (
                result.get("messages_json") or history_json
            )
            try:
                (rev_plan, _rev_usage, rev_err) = await expert_stages._run_planner(
                    user=revised_user, project=project,
                    model_spec=planner_spec, ponytail=ponytail,
                    on_progress=on_progress, is_followup=True,
                    es_continuacion=True,
                    history_recap=expert_planning._history_recap(planner_history),
                    toolsets=planner_toolsets,
                    deadline=limite_pedido,
                )
            except Exception as _e:  # noqa: BLE001
                logger.warning(
                    "run_expert_staged: el segundo planificador falló "
                    "tras una interrupción del ejecutor: %r", _e)
                rev_plan, rev_err = "", str(_e)
            rev_ev = {
                "type": "plan_revision",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime()),
                "turn": len(rondas) + 1,
                "previous_plan_chars": len(plan or ""),
                "new_plan_chars": len(rev_plan or ""),
                "plan": rev_plan,
                "trigger_event": ev_idx,
            }
            if rev_err:
                rev_ev["error"] = rev_err
                logger.warning(
                    "run_expert_staged: replanificación falló — sigo "
                    "con el plan original")
            else:
                plan = rev_plan
                # Reemplaza el plan; si es trivial, conserva el camino corto.
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
            continue

        phase = result.get("phase_at_end") or ""
        broken = phase in ("error", "timeout", "hard_timeout", "idle_timeout",
                           "cancelled", "no_result",
                           "provider_error")
        pregunto = bool(result.get("question_id"))
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
            result["tool_calls_summary"] = (
                expert_consult._summarize_tool_calls_from_messages(
                    result.get("messages_json") or ""))
            if ver_enabled:
                v_tuple = await expert_stages._run_verifier(
                    user=user_original, plan=plan, executor_result=result,
                    model_spec=verifier_spec, ponytail=ponytail,
                    on_progress=on_progress, deadline=limite_pedido,
                )
                if len(v_tuple) == 5:
                    (verdict, feedback, verifier_usage, ver_err,
                     _pasos_v) = v_tuple
                else:
                    (verdict, feedback, verifier_usage,
                     ver_err) = v_tuple
                    _pasos_v = []
                verifier_corrio = True
                if _pasos_v:
                    bitacora.fusionar_pasos_verificados(_pasos_v)
                    kwargs["bitacora_json"] = bitacora.volcar()
                if ver_err:
                    stage_errors["verifier"] = ver_err
            else:
                verdict, feedback = "", ""

        rondas.append({"ronda": len(rondas) + 1, "verdict": verdict,
                       "feedback": feedback,
                       "phase_at_end": phase,
                       "tool_calls": result.get("tool_calls")})

        sin_trabajo = (not trivial and not broken and not pregunto
                       and not (result.get("tool_calls") or 0))
        reintento_vacio = sin_trabajo and not forzadas

        if not ((verdict == "needs_more" or reintento_vacio)
                and len(rondas) <= max_rondas
                and result.get("messages_json") and not broken
                and not pregunto):
            break

        if limite_pedido - time.monotonic() < expert_stage_prompts.RESERVA_CIERRE_S:
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

    if len(rondas) > 1:
        for k in ("tokens_in", "tokens_out", "cache_read_tokens", "tool_calls", "duration_ms",
                  "legs"):
            if result.get(k) is not None:
                result[k] = (result.get(k) or 0) + acum[k]
        result["progress_events"] = (
            acum["progress_events"] + (result.get("progress_events") or []))

    # Documenta trabajo completo; las revisiones de solo lectura se omiten.
    if not broken and not pregunto and phase != "off_plan":
        solo_reviso = expert_verdicts._solo_reviso(result.get("tool_calls_summary"))
        has_work = (bool(result.get("tool_calls_summary")) and not trivial
                    and not solo_reviso)
        documenter_blocked = (verdict != "complete")
        if doc_enabled and has_work and not documenter_blocked:
            stages_events.append({
                "turn": len(rondas),
                "type": "verifier_complete",
                "verdict": "complete",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime()),
            })
            doc, documenter_usage, doc_err = await expert_stages._run_documenter(
                user=user_original, plan=plan, executor_result=result,
                verdict=verdict or "(sin verificar)", feedback=feedback,
                model_spec=documenter_spec, ponytail=ponytail,
                on_progress=on_progress, deadline=limite_pedido,
            )
            if doc_err:
                stage_errors["documenter"] = doc_err
        elif doc_enabled and has_work and documenter_blocked:
            stages_events.append({
                "turn": len(rondas),
                "type": "documenter_skipped",
                "verdict": verdict,
                "reason": "verdict_no_complete",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime()),
            })
        elif doc_enabled and solo_reviso and not trivial:
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
    result["mid_verdicts"] = mid_verdicts
    result["stages_events"] = stages_events
    result["verifier_model"] = verifier_spec if verifier_corrio else ""
    result["doc"] = doc
    result["documenter_model"] = documenter_spec if doc else ""
    result["three_stage"] = True
    result["stage_usage"] = {
        "planner": planner_usage,
        "verifier": verifier_usage,
        "documenter": documenter_usage,
    }
    result["stage_errors"] = stage_errors

    return expert_result.finish_staged_result(
        result, doc=doc, verdict=verdict, feedback=feedback, phase=phase,
        rondas=rondas, corte_por_presupuesto=corte_por_presupuesto, sin_trabajo=sin_trabajo)
