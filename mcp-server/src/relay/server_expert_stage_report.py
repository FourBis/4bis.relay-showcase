"""Proyección de etapas del experto para persistencia y métricas."""
from __future__ import annotations

import json
from typing import Optional


def stages_json(result: dict, *, outcome) -> Optional[str]:
    """Etapas del runner → JSON para `chats.stages_json`, o None.

    Devuelve None cuando el run NO pasó por el runner por etapas
    (`run_expert` directo, opt-out del proyecto): así la columna queda
    NULL en vez de con un objeto de campos vacíos, y "no hubo etapas"
    se distingue de "hubo etapas y salieron vacías".

    El plan se recorta: es texto del planificador y no tiene por qué
    entrar entero en una fila de la DB — el valor de auditoría está en
    el veredicto y en qué modelo corrió cada etapa.
    """
    if not result.get("three_stage"):
        return None
    usage = result.get("stage_usage") or {}
    out = {
        "plan": (result.get("plan") or "")[:4000],
        "planner_model": result.get("planner_model") or "",
        "verifier_verdict": result.get("verifier_verdict") or "",
        "verifier_feedback": result.get("verifier_feedback") or "",
        "verifier_model": result.get("verifier_model") or "",
        "documenter_model": result.get("documenter_model") or "",
        "executor_model": result.get("model") or "",
        # Qué pasó con el trabajo. Ver `_resultado_del_trabajo`: no es
        # `chats.status`, que habla de la ejecución.
        "resultado": outcome(result),
    }
    # Cuántas pasadas necesitó el turno. El runner lo calcula y hasta hoy
    # lo tiraba: sin esto, "resuelto en una pasada" y "resuelto después
    # de tres rondas de needs_more" quedaban idénticos en la fila, y era
    # imposible medir si un cambio de prompt o de modelo mejoraba algo
    # sin volver a parsear los logs a mano.
    #
    # Plano el conteo (SQL lo alcanza), anidado el detalle (para leer un
    # caso puntual). El feedback se recorta como el resto del texto libre.
    rondas = result.get("verifier_rounds") or []
    if rondas:
        out["rondas"] = len(rondas)
        out["rondas_detalle"] = [
            {"ronda": r.get("ronda"), "verdict": r.get("verdict") or "",
             "phase_at_end": r.get("phase_at_end") or "",
             "tool_calls": r.get("tool_calls"),
             "feedback": (r.get("feedback") or "")[:200]}
            for r in rondas]
    # Veredictos de media corrida, uno por borde de tanda. Un corte por
    # desvío se veía en el mensaje al humano y no quedaba en ningún lado
    # para revisar después POR QUÉ el ejecutor se fue del plan.
    medios = result.get("mid_verdicts") or []
    if medios:
        out["mid_verdicts"] = [
            {"leg": m.get("leg"), "verdict": m.get("verdict") or "",
             "feedback": (m.get("feedback") or "")[:200],
             "error": str(m.get("error") or "")[:200]}
            for m in medios]
    # Tokens por etapa, planos para que `json_extract` los alcance desde
    # SQL sin recorrer objetos anidados. Una etapa que no reportó usage
    # NO escribe sus claves: ausente = "no se midió", que no es cero.
    for stage in ("planner", "verifier", "documenter"):
        u = usage.get(stage) or {}
        if u:
            out[f"{stage}_tokens_in"] = u.get("tokens_in", 0)
            out[f"{stage}_tokens_out"] = u.get("tokens_out", 0)
    # Etapas caídas (2026-08-15). Misma regla que los tokens: la clave
    # solo existe si hubo error, así que `IS NOT NULL` en SQL alcanza para
    # contar degradaciones. Sin esto, un planificador que corta por
    # timeout dejaba al ejecutor corriendo sin plan y el único rastro era
    # un WARNING en el log — el run se veía idéntico a uno planificado.
    for stage, err in (result.get("stage_errors") or {}).items():
        if err:
            out[f"{stage}_error"] = str(err)[:200]
    # Etapa B, P3: pasos que el ejecutor marcó con `plan_step_done`.
    # Se guarda como dict {paso_str: nota}, mismo criterio que el resto
    # de `stages_json`: ausente = nadie marcó nada, presente = al menos
    # uno. Si la key está vacía la omitimos para no ensuciar el JSON.
    ps = result.get("plan_steps_done")
    if isinstance(ps, dict) and ps:
        out["plan_steps_done"] = {str(k): str(v)[:200]
                                  for k, v in ps.items()}
    return json.dumps(out, ensure_ascii=False)
