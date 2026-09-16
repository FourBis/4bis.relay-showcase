"""Manejador de interrupción Ejecutor→Planificador en `run_expert_staged`.

Hasta 2026-08-26 el ejecutor era opaco: si el plan original resultaba
inviable (compile_fail, type_mismatch, etc.), el verificador era el único
que podia redirigir y solo después de que el ejecutor cerrara su turno.

Estos tests cubren el flujo nuevo: cuando el ejecutor emite la señal
`EXECUTOR_INTERRUPTION` (canónica, en fence) o el fallback
`<<INTERRUPT:EXECUTOR>>` (regex), `run_expert_staged` debe:

  (a) no auto-avanzar al Verificador;
  (b) anexar un evento `executor_interruption` a `stages_events`;
  (c) re-pasar al Planificador con el payload de la interrupción como
      contexto adicional;
  (d) anexar un evento `plan_revision` con el plan revisado;
  (e) mergear el plan revisado al `system_extra` del ejecutor, así los
      turnos siguientes ven el plan revisado, no el original.

Y los casos negativos: la señal mal formada NO dispara la
re-planificación.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from relay import experts


# ---------- unidades: parser de la señal ----------


def test_parse_executor_interruption_canonical_fence():
    sig = {
        "reason": "compile_fail",
        "affected_step_id": "step-2",
        "error_context": "missing `time` import",
        "partial_progress": {"step-1": "ok", "step-2": "failed"},
        "timestamp": "2026-08-26T12:00:00Z",
    }
    out = (
        "ya empecé pero el plan no sobrevive:\n"
        "```EXECUTOR_INTERRUPTION\n"
        + json.dumps(sig)
        + "\n```\nfin del turno"
    )
    parsed = experts._parse_executor_interruption(out)
    assert parsed == sig


def test_parse_executor_interruption_fallback_marker():
    sig = {
        "reason": "wrong_assumption",
        "affected_step_id": "step-3",
        "error_context": "the API was already removed",
        "partial_progress": {"step-1": "done", "step-2": "done"},
        "timestamp": "2026-08-26T13:00:00Z",
    }
    out = "<<INTERRUPT:EXECUTOR>>" + json.dumps(sig)
    parsed = experts._parse_executor_interruption(out)
    assert parsed == sig


def test_parse_executor_interruption_absent_returns_none():
    out = "todo bien, cerré el turno"
    assert experts._parse_executor_interruption(out) is None


def test_parse_executor_interruption_missing_field_returns_none():
    sig = {
        "reason": "compile_fail",
        "affected_step_id": "step-2",
        # falta `error_context`, `partial_progress`, `timestamp`
    }
    out = "```EXECUTOR_INTERRUPTION\n" + json.dumps(sig) + "\n```"
    assert experts._parse_executor_interruption(out) is None


def test_parse_executor_interruption_garbage_returns_none():
    out = "```EXECUTOR_INTERRUPTION\n{not valid json}\n```"
    assert experts._parse_executor_interruption(out) is None


# ---------- integración: run_expert_staged ----------


def _project():
    return {
        "id": 1, "slug": "demo", "repo_path": "C:/x/Demo",
        "system_prompt": "experto demo",
        "mcp_servers": [], "native_tools": [],
        "defaults_json": {"three_stage": True},
    }


def _executor_result(**over):
    base = {
        "content": "ok", "model": "test",
        "tokens_in": 1, "tokens_out": 1, "tool_calls": 1,
        "duration_ms": 1, "messages_json": "[]",
        "phase_at_end": "writing", "last_tool": None,
        "legs": 1, "steers": 0, "steer_texts": [],
        "progress_events": [],
    }
    base.update(over)
    return base


_INTERRUPTION_SIG = {
    "reason": "compile_fail",
    "affected_step_id": "step-2",
    "error_context": "missing `time` import",
    "partial_progress": {"step-1": "ok"},
    "timestamp": "2026-08-26T12:00:00Z",
}


async def test_run_expert_staged_interruption_invokes_planner_again():
    """Una sola emisión de la señal dispara UNA re-planificación extra."""
    project = _project()
    planner_calls: list[dict] = []
    executor_calls: list[dict] = []
    verifier_calls: list[dict] = []

    plan_v1 = "1. leer\n2. editar\n3. verificar"
    plan_v2 = "1. leer\n2. agregar import time\n3. editar\n4. verificar"

    async def fake_planner(**kwargs):
        planner_calls.append(kwargs)
        if len(planner_calls) == 1:
            return plan_v1, {}, ""
        return plan_v2, {}, ""

    async def fake_executor(proj, user, **kwargs):
        executor_calls.append({"user": user,
                               "system_extra": kwargs.get("system_extra") or ""})
        # Primer turno: emite la señal. Segundo turno: cierra normal.
        if len(executor_calls) == 1:
            sig_block = "```EXECUTOR_INTERRUPTION\n" + json.dumps(_INTERRUPTION_SIG) + "\n```"
            return _executor_result(content="me trabé\n" + sig_block)
        return _executor_result(phase_at_end="complete")

    async def fake_verifier(**kwargs):
        verifier_calls.append(kwargs)
        return "complete", "ok", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier), \
         patch.object(experts, "_run_documenter",
                      new=AsyncMock(return_value=("bitácora", {}))):
        result = await experts.run_expert_staged(
            project, "editame el archivo",
            defaults={"planner_model": "x", "executor_model": "x",
                      "verifier_model": "x", "documenter_model": "x"},
        )

    # El planificador se llamó DOS veces: la inicial y la de revisión.
    assert len(planner_calls) == 2
    # El ejecutor corrió DOS veces también (el segundo con plan revisado).
    assert len(executor_calls) == 2
    # El verificador se llamó UNA sola vez: la del segundo turno del
    # ejecutor. La primera vez que se emitió la interrupción NO se
    # avanzó al verificador.
    assert len(verifier_calls) == 1

    # El SEGUNDO turno del ejecutor vio el plan revisado (v2), no el v1.
    second_extra = executor_calls[1]["system_extra"]
    assert "agregar import time" in second_extra
    assert "1. leer\n2. editar" not in second_extra  # ya no es el plan v1

    # `stages_events` registra ambos eventos en orden.
    events = result["stages_events"]
    types = [e["type"] for e in events]
    assert types == ["executor_interruption", "plan_revision"]
    assert events[0]["reason"] == "compile_fail"
    assert events[0]["affected_step_id"] == "step-2"
    assert events[0]["error_context"] == "missing `time` import"
    assert events[0]["partial_progress"] == {"step-1": "ok"}
    assert events[0]["timestamp"] == "2026-08-26T12:00:00Z"
    assert events[1]["plan"].startswith("1. leer\n2. agregar import time")


async def test_run_expert_staged_no_signal_skips_interruption_path():
    """Sin señal, `stages_events` queda vacío y el flujo sigue igual."""
    project = _project()
    planner_calls: list[dict] = []
    executor_calls: list[dict] = []
    verifier_calls: list[dict] = []

    async def fake_planner(**kwargs):
        planner_calls.append(kwargs)
        return "1. leer", {}, ""

    async def fake_executor(proj, user, **kwargs):
        executor_calls.append(kwargs)
        return _executor_result(phase_at_end="complete")

    async def fake_verifier(**kwargs):
        verifier_calls.append(kwargs)
        return "complete", "ok", {}, ""

    async def fake_documenter(**kwargs):
        return "doc emitido", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier), \
         patch.object(experts, "_run_documenter", fake_documenter):
        result = await experts.run_expert_staged(
            project, "editame", model_override="test",
        )

    assert len(planner_calls) == 1
    assert len(executor_calls) == 1
    assert len(verifier_calls) == 1
    assert result["stages_events"] == []


async def test_run_expert_staged_malformed_signal_does_not_interrupt():
    """Señal con JSON inválido NO dispara la re-planificación."""
    project = _project()
    planner_calls: list[dict] = []

    async def fake_planner(**kwargs):
        planner_calls.append(kwargs)
        return "1. leer", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result(
            content="casi pero no\n```EXECUTOR_INTERRUPTION\n{no json}\n```",
            phase_at_end="complete",
        )

    async def fake_verifier(**kwargs):
        return "complete", "ok", {}, ""

    async def fake_documenter(**kwargs):
        return "doc", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier), \
         patch.object(experts, "_run_documenter", fake_documenter):
        result = await experts.run_expert_staged(
            project, "editame", model_override="test",
        )

    # Planificador UNA vez (la inicial), sin revisión.
    assert len(planner_calls) == 1
    assert result["stages_events"] == []

async def test_run_expert_staged_interrupcion_tiene_tope():
    """Un ejecutor que SIEMPRE interrumpe no puede colgar el lazo.

    Regresión 2026-09-02. El camino de interrupción hace `continue` sin
    tocar `rondas`, que es lo ÚNICO que corta el `while True`, y cada
    pasada estrena deadline (el de `run_expert` se calcula con un `t0`
    nuevo por llamada). Sin `MAX_INTERRUPCIONES` el lazo no tenía cota
    —ni de vueltas ni de tiempo— a dos LLM calls por vuelta.

    El caso peor es determinista: con la replanificación fallando el
    plan queda igual, el ejecutor ve el mismo input y repite la señal.
    Acá el ejecutor la emite SIEMPRE, que es ese escenario.
    """
    project = _project()
    planner_calls: list[dict] = []
    executor_calls: list[dict] = []
    verifier_calls: list[dict] = []

    async def fake_planner(**kwargs):
        planner_calls.append(kwargs)
        return f"plan v{len(planner_calls)}", {}, ""

    async def fake_executor(proj, user, **kwargs):
        executor_calls.append(kwargs)
        # Red de seguridad: si el tope desaparece, esto corta el test en
        # vez de dejarlo colgado hasta el timeout de la suite.
        if len(executor_calls) > 10:
            raise AssertionError(
                "el lazo de interrupción no tiene tope: el ejecutor ya "
                "corrió 10 veces sin llegar nunca al verificador")
        sig = "```EXECUTOR_INTERRUPTION\n" + json.dumps(_INTERRUPTION_SIG) + "\n```"
        return _executor_result(content="me trabé otra vez\n" + sig)

    async def fake_verifier(**kwargs):
        verifier_calls.append(kwargs)
        return "complete", "ok", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier), \
         patch.object(experts, "_run_documenter",
                      new=AsyncMock(return_value=("bitácora", {}))):
        result = await experts.run_expert_staged(
            project, "editame el archivo",
            defaults={"planner_model": "x", "executor_model": "x",
                      "verifier_model": "x", "documenter_model": "x"},
        )

    # Se honran 2 interrupciones; en la 3ra vuelta la señal se ignora y
    # el run sigue al verificador. O sea: el lazo TERMINA.
    assert len(executor_calls) == 3, executor_calls
    # Planificador: el inicial + una revisión por interrupción honrada.
    assert len(planner_calls) == 3
    # Y se llegó al verificador, que es lo que el bug impedía para
    # siempre.
    assert len(verifier_calls) == 1

    tipos = [e["type"] for e in result["stages_events"]]
    assert tipos.count("executor_interruption") == 2
    assert tipos.count("plan_revision") == 2
    # El evento que deja rastro de por qué se dejó de honrar la señal.
    assert "executor_interruption_ignored" in tipos
    ignorado = next(e for e in result["stages_events"]
                    if e["type"] == "executor_interruption_ignored")
    assert ignorado["limit"] == 2
    assert ignorado["reason"] == "compile_fail"


async def test_plan_revision_apunta_a_la_interrupcion_que_lo_disparo():
    """`trigger_event` indexa la interrupción, no el evento anterior.

    Regresión 2026-09-02: era `ev_idx - 1`, y como `ev_idx` YA es el
    índice de la interrupción, apuntaba al evento previo — con la
    interrupción como primer evento del run daba -1, que en Python
    indexa el ÚLTIMO, o sea el propio `plan_revision`.
    """
    project = _project()
    executor_calls: list[dict] = []

    async def fake_planner(**kwargs):
        return "plan revisado", {}, ""

    async def fake_executor(proj, user, **kwargs):
        executor_calls.append(kwargs)
        if len(executor_calls) == 1:
            sig = ("```EXECUTOR_INTERRUPTION\n"
                   + json.dumps(_INTERRUPTION_SIG) + "\n```")
            return _executor_result(content="me trabé\n" + sig)
        return _executor_result(phase_at_end="complete")

    async def fake_verifier(**kwargs):
        return "complete", "ok", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier), \
         patch.object(experts, "_run_documenter",
                      new=AsyncMock(return_value=("bitácora", {}))):
        result = await experts.run_expert_staged(
            project, "editame el archivo",
            defaults={"planner_model": "x", "executor_model": "x",
                      "verifier_model": "x", "documenter_model": "x"},
        )

    eventos = result["stages_events"]
    rev = next(e for e in eventos if e["type"] == "plan_revision")
    apuntado = eventos[rev["trigger_event"]]
    assert apuntado["type"] == "executor_interruption", eventos
    # Y no se apunta a sí mismo (lo que pasaba con el -1).
    assert eventos.index(rev) != rev["trigger_event"]
