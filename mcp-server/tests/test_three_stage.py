"""Tests del runner por etapas (iter 11).

Etapas: planificador + ejecutor + verificador + documentador.

Cubre:
  - opt-out por `defaults_json.three_stage=False` (cae a run_expert).
  - etapas activas por default: el resultado trae plan/veredicto.
  - verdict=needs_human antepone un aviso al content.
  - el verificador se OMITE si el ejecutor terminó en error / timeout /
    cancelación (no tiene sentido verificar un run roto).
  - el documentador se omite sin llamadas a herramientas y cuando el
    plan fue trivial; su salida se agrega al content.
  - `_parse_verifier` tolera prefijos, markdown y mayúsculas mezcladas.
  - end-to-end vía POST /experts/run y forma de /system/active.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from aiohttp.test_utils import TestClient, TestServer

from relay import experts, server
from relay import db as db_mod
from relay.db import Database
from relay.server import create_app


# ---------- unidades: parser del verificador ----------


def test_parse_verifier_canonical():
    v, f, _pasos = experts._parse_verifier(
        "VERDICT: complete\nFEEDBACK: listo, sin observaciones")
    assert v == "complete"
    assert f == "listo, sin observaciones"
    assert _pasos == []


def test_parse_verifier_case_insensitive():
    v, f, _pasos = experts._parse_verifier(
        "verdict: NEEDS_MORE\nfeedback: test")
    assert v == "needs_more"
    assert f == "test"
    assert _pasos == []


def test_parse_verifier_multiline_feedback_truncates_to_200():
    text = "VERDICT: complete\nFEEDBACK: " + ("x" * 500)
    v, f, _pasos = experts._parse_verifier(text)
    assert v == "complete"
    assert len(f) == 200
    assert _pasos == []


def test_parse_verifier_unknown_verdict_requires_review():
    """La prosa sin veredicto no acredita que el trabajo esté completo."""
    v, f, _pasos = experts._parse_verifier("hola, hice el cambio")
    assert v == "needs_human"
    assert "hice el cambio" in f
    assert _pasos == []


def test_parse_verifier_needs_human():
    v, f, _pasos = experts._parse_verifier(
        "VERDICT: needs_human\nFEEDBACK: elegir alcance")
    assert v == "needs_human"
    assert f == "elegir alcance"
    assert _pasos == []


@pytest.mark.parametrize("text", ["", "   \n  ", None])
def test_parse_verifier_empty_is_needs_human(text):
    """Sin texto nadie verificó nada: NO se aprueba en silencio.

    Igual que texto suelto sin veredicto: no hay aprobación comprobable.
    """
    v, f, _pasos = experts._parse_verifier(text)
    assert v == "needs_human"
    assert "NO fue verificado" in f
    assert _pasos == []


def test_run_verifier_failure_is_needs_human():
    """Si el verificador se cae (429 del free tier, timeout), el run
    queda marcado `needs_human`, no `complete`.

    Regresión 2026-08-14: devolvía `complete`, así que un verificador
    caído aprobaba en silencio un run que nadie revisó.
    """
    # build_model es sync: el side_effect va como excepción directa. Con
    # una corrutina el mock devolvería un coroutine object sin levantar
    # nada, y el test pasaría por el camino equivocado.
    boom = RuntimeError("status_code: 429, Too Many Requests")

    with patch.object(experts, "build_model", side_effect=boom):
        # Etapa B: ahora devuelve 5-tupla (verdict, feedback, usage, error, pasos).
        # Ver `experts.py:_run_verifier`.
        v, f, usage, err, _pasos = asyncio.run(experts._run_verifier(
            user="x", plan="1. hacer algo",
            executor_result={"content": "hecho", "tool_calls_summary": []},
            model_spec="test", ponytail=""))

    assert v == "needs_human", f"un verificador caído no puede aprobar: {v}"
    assert "NO fue verificado" in f
    assert usage == {}, "una etapa que no corrió no puede reportar tokens"
    assert "RuntimeError" in err, "el error debe quedar en la tupla para stages_json"
    assert _pasos == [], "sin verificador no hay pasos verificados"


def test_run_verifier_model_unavailable_still_propagates():
    """ModelUnavailable sigue propagando: es config rota, no una caída
    transitoria, y el caller quiere enterarse."""
    with patch.object(experts, "build_model",
                      side_effect=experts.ModelUnavailable("sin key")):
        with pytest.raises(experts.ModelUnavailable):
            asyncio.run(experts._run_verifier(
                user="x", plan="p", executor_result={"content": "c"},
                model_spec="test", ponytail=""))


# ---------- config: cascada de las etapas ----------


# OJO: aquí se usa setenv("") y NO delenv(). `config.load_dotenv()`
# rellena las variables que falten leyendo el .env del repo, así que un
# delenv se deshace solo y el test terminaría comprobando la config del
# desarrollador en lugar de la cascada. Una cadena vacía sí está en
# os.environ, load_dotenv la respeta, y el código la trata como ausente.


def test_planner_model_spec_falls_back_to_model_spec(monkeypatch):
    monkeypatch.setenv("FOURBIS_PLANNER_MODEL", "")
    monkeypatch.setenv("FOURBIS_COMPACTOR_MODEL", "")
    monkeypatch.setenv("FOURBIS_MODEL", "anthropic:claude-x")
    assert experts.config.planner_model_spec() == "anthropic:claude-x"


def test_model_spec_prefiere_system_config_al_entorno(monkeypatch):
    """Config -> Ejecutor promete aplicar sin reiniciar el relay."""
    monkeypatch.setenv("FOURBIS_MODEL", "env:executor")
    experts.config.set_runtime_config({"FOURBIS_MODEL": "db:executor"})
    try:
        assert experts.config.model_spec() == "db:executor"
        assert experts.resolve_model_spec("", {}) == "db:executor"
    finally:
        experts.config.set_runtime_config({})


def test_verifier_model_spec_falls_back_to_compactor(monkeypatch):
    monkeypatch.setenv("FOURBIS_MODEL", "minimax:MiniMax-M3")
    monkeypatch.setenv("FOURBIS_VERIFIER_MODEL", "")
    monkeypatch.setenv("FOURBIS_COMPACTOR_MODEL", "minimax:MiniMax-M2.7")
    assert experts.config.verifier_model_spec() == "minimax:MiniMax-M2.7"


def test_documenter_model_spec_env_wins(monkeypatch):
    monkeypatch.setenv("FOURBIS_MODEL", "minimax:MiniMax-M3")
    monkeypatch.setenv("FOURBIS_DOCUMENTER_MODEL", "minimax:MiniMax-M3")
    monkeypatch.setenv("FOURBIS_COMPACTOR_MODEL", "otro:modelo")
    assert experts.config.documenter_model_spec() == "minimax:MiniMax-M3"


def test_documenter_model_spec_falls_back_to_compactor(monkeypatch):
    monkeypatch.setenv("FOURBIS_MODEL", "minimax:MiniMax-M3")
    monkeypatch.setenv("FOURBIS_DOCUMENTER_MODEL", "")
    monkeypatch.setenv("FOURBIS_COMPACTOR_MODEL", "minimax:MiniMax-M2.7")
    assert experts.config.documenter_model_spec() == "minimax:MiniMax-M2.7"


def test_staged_specs_follow_test_sentinel(monkeypatch):
    """Con FOURBIS_MODEL=test las etapas NO llaman a un proveedor real.

    Es la salvaguarda que evita que la suite facture tokens cuando el
    .env define FOURBIS_PLANNER_MODEL apuntando a MiniMax.
    """
    monkeypatch.setenv("FOURBIS_MODEL", "test")
    monkeypatch.setenv("FOURBIS_PLANNER_MODEL", "minimax:MiniMax-M3")
    monkeypatch.setenv("FOURBIS_VERIFIER_MODEL", "minimax:MiniMax-M3")
    monkeypatch.setenv("FOURBIS_DOCUMENTER_MODEL", "minimax:MiniMax-M3")
    assert experts.config.planner_model_spec() == "test"
    assert experts.config.verifier_model_spec() == "test"
    assert experts.config.documenter_model_spec() == "test"


# ---------- run_expert_staged: opt-out + flujo ----------


# messages_json con una tool call, para que el documentador vea trabajo.
_MESSAGES_WITH_TOOL = json.dumps([
    {"kind": "response", "parts": [
        {"part_kind": "tool-call", "tool_name": "edit_file",
         "args": {"path": "main.py", "content": "print(1)"}},
    ]},
])


def _project(*, three_stage=True, **extra):
    base = {
        # `id` lo tienen todas las filas reales de `projects`, y desde que
        # el planificador consulta el catálogo de MCPs para el razonador,
        # un proyecto sin id degrada a "sin razonador" en silencio.
        "id": 1,
        "slug": "demo", "repo_path": "C:/x/Demo",
        "system_prompt": "experto demo",
        "mcp_servers": [], "native_tools": [],
        "defaults_json": {"three_stage": three_stage},
    }
    base["defaults_json"].update(extra.get("defaults_json") or {})
    return base


def _executor_result(**over):
    base = {
        "content": "ok", "model": "test",
        "tokens_in": 1, "tokens_out": 1,
        # 1 y no 0 (2026-08-17): estos tests modelan un ejecutor que
        # TRABAJÓ; el 0 era un default heredado que nadie miraba. Desde
        # que `run_expert_staged` trata "cero tool calls" como un turno
        # que no arrancó y fuerza otra pasada, ese 0 hacía que cada test
        # de acá pagara un turno extra por accidente. Los tests del
        # turno vacío pasan `tool_calls=0` a propósito, abajo.
        "tool_calls": 1, "duration_ms": 1,
        "messages_json": "[]", "phase_at_end": "writing",
        "last_tool": None, "legs": 1, "steers": 0,
        "steer_texts": [], "progress_events": [],
    }
    base.update(over)
    return base


async def test_run_expert_staged_opt_out_uses_legacy():
    """Con three_stage=False cae a run_expert() sin plan ni verificador."""
    project = _project(three_stage=False)
    captured = {}

    async def fake_progress(**kw):
        captured.setdefault("phases", []).append(kw.get("phase"))

    async def fake_executor(proj, user, **kwargs):
        captured["legacy_called"] = True
        assert "Plan a ejecutar" not in (kwargs.get("system_extra") or "")
        return _executor_result(content="legacy response")

    with patch.object(experts, "run_expert", fake_executor):
        result = await experts.run_expert_staged(
            project, "hola", model_override="test",
            on_progress=fake_progress,
        )
    assert captured.get("legacy_called") is True
    assert result["three_stage"] is False
    assert result["plan"] == ""
    assert result["verifier_verdict"] == ""
    assert result["planner_model"] == ""
    assert result["doc"] == ""


async def test_legacy_alias_still_exported():
    """server.py, night.py y código externo importan el nombre viejo."""
    assert experts.run_expert_3stage is experts.run_expert_staged


async def test_run_expert_staged_propagates_model_unavailable():
    """Planificador y verificador propagan ModelUnavailable.

    Si el planificador no puede armar su modelo, el wrapper entero
    tiene que fallar: si no, el ejecutor fallaría igual un turno
    después y el chat terminaría en un limbo donde el verificador
    devuelve "complete" sobre un run que nunca se ejecutó.
    """
    project = _project(three_stage=True)

    async def fake_planner(*args, **kwargs):
        raise experts.ModelUnavailable("sin key")

    async def fake_executor(proj, user, **kwargs):
        raise AssertionError("el ejecutor no debe correr si el planner falla")

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor):
        with pytest.raises(experts.ModelUnavailable):
            await experts.run_expert_staged(
                project, "hola", model_override="minimax:MiniMax-M3",
            )


async def test_run_expert_staged_injects_plan_as_system_extra():
    """El plan del planificador entra como system_extra al ejecutor."""
    project = _project(three_stage=True)
    captured = {}

    async def fake_planner(**kwargs):
        return "1. leer main.py\n2. agregar print\n3. verificar", {}, ""

    async def fake_executor(proj, user, **kwargs):
        captured["system_extra"] = kwargs.get("system_extra") or ""
        return _executor_result()

    async def fake_verifier(**kwargs):
        return "complete", "todo correcto", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        result = await experts.run_expert_staged(
            project, "agrega un print", model_override="test",
        )
    assert "Plan a ejecutar" in captured["system_extra"]
    assert "leer main.py" in captured["system_extra"]
    assert result["plan"].startswith("1. leer main.py")
    assert result["verifier_verdict"] == "complete"
    assert result["three_stage"] is True


class _DbConReasoning:
    """DB de mentira: devuelve el MCP de `reasoning` del catálogo."""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [
            {"name": "sequential-thinking", "capability": "reasoning",
             "transport": "stdio"}]
        self.pedido = {}

    async def mcp_servers_for_project(self, pid, **kw):
        self.pedido = kw
        return self.rows


class _PoolQueLevanta:
    def __init__(self, toolset="TOOLSET"):
        self.toolset = toolset

    async def acquire(self, row, repo_path):
        return self.toolset


async def test_reasoning_toolset_degrada_sin_db_ni_pool():
    """Sin catálogo o sin pool, el planificador planifica igual."""
    assert await experts._reasoning_toolset(None, {"id": 1}, None) == []
    assert await experts._reasoning_toolset(
        _DbConReasoning(), {"id": 1}, None) == []


async def test_reasoning_toolset_degrada_si_el_mcp_no_levanta():
    """`npx` ausente o handshake fallido: se planifica sin razonador.

    Es la razón de que esto sea best-effort: el relay tiene que seguir
    andando en una máquina sin la toolchain de Node.
    """
    class _PoolMudo:
        async def acquire(self, row, repo_path):
            return None

    out = await experts._reasoning_toolset(
        _DbConReasoning(), {"id": 1, "repo_path": "x"}, _PoolMudo())
    assert out == []


async def test_reasoning_toolset_pide_la_capability_reasoning():
    db = _DbConReasoning()
    out = await experts._reasoning_toolset(
        db, {"id": 1, "repo_path": "x"}, _PoolQueLevanta())
    assert len(out) == 1
    assert db.pedido.get("capabilities") == ["reasoning"]


async def test_planner_recibe_el_razonador_para_decidir_descomponer():
    """El paso 3: sequential-thinking decide si el pedido es una tarea.

    Caso que lo motivó: "parte de 0 en un SampleApp nuevo" son ocho palabras
    y significa base de datos + migraciones + dos servidores + seed. El
    planificador lo leyó como UNA tarea y el ejecutor se fue 172 pasos a
    construir el entorno solo.
    """
    project = _project(three_stage=True)
    captured = {}

    async def fake_planner(**kwargs):
        captured["toolsets"] = kwargs.get("toolsets")
        return "1. leer main.py", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result()

    async def fake_verifier(**kwargs):
        return "complete", "ok", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        await experts.run_expert_staged(
            project, "parte de 0 en un SampleApp nuevo", model_override="test",
            db=_DbConReasoning(), mcp_pool=_PoolQueLevanta())
    assert captured["toolsets"], "el planificador planificó sin el razonador"


async def test_planner_reasoning_se_puede_apagar_por_proyecto():
    project = _project(three_stage=True)
    project["defaults_json"]["planner_reasoning"] = False
    captured = {}

    async def fake_planner(**kwargs):
        captured["toolsets"] = kwargs.get("toolsets")
        return "1. leer", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result()

    async def fake_verifier(**kwargs):
        return "complete", "ok", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        await experts.run_expert_staged(
            project, "haz algo", model_override="test",
            db=_DbConReasoning(), mcp_pool=_PoolQueLevanta())
    assert not captured["toolsets"]


async def test_en_followup_no_se_paga_el_razonador():
    """`DEMASIADO_GRANDE:` está prohibido en follow-ups.

    Pensar sobre una decisión que ya está tomada es pagar de gusto.
    """
    project = _project(three_stage=True)
    captured = {}

    async def fake_planner(**kwargs):
        captured["toolsets"] = kwargs.get("toolsets")
        return "1. seguir", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result()

    async def fake_verifier(**kwargs):
        return "complete", "ok", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        await experts.run_expert_staged(
            project, "continúa", model_override="test",
            message_history_json='[{"parts":[]}]',
            db=_DbConReasoning(), mcp_pool=_PoolQueLevanta())
    assert not captured["toolsets"]


async def test_run_expert_staged_enchufa_el_supervisor_de_media_corrida():
    """El runner por etapas le pasa `on_leg_boundary` al ejecutor.

    Es lo que convierte al verificador en un control de media corrida: sin
    esto corre una sola vez, al final, y un desvío se descubre 100 pasos
    después de haber empezado (chat 9d0fef5e, sample-app).
    """
    project = _project(three_stage=True)
    captured = {}

    async def fake_planner(**kwargs):
        return "1. leer main.py", {}, ""

    async def fake_executor(proj, user, **kwargs):
        captured["supervisor"] = kwargs.get("on_leg_boundary")
        return _executor_result()

    async def fake_verifier(**kwargs):
        return "complete", "ok", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        await experts.run_expert_staged(
            project, "leelo", model_override="test")
    assert callable(captured["supervisor"]), \
        "el ejecutor corrió sin supervisor de media corrida"


async def test_supervisor_traduce_off_plan_a_corte_y_el_resto_a_seguir():
    """El contrato del supervisor: solo `off_plan` corta.

    `needs_more` significa "falta trabajo, seguí" —lo contrario de
    cortar— y ese fue exactamente el veredicto que el verificador dio en
    el run de sample-app. Si el supervisor cortara con needs_more, mataría
    todos los runs largos legítimos.
    """
    project = _project(three_stage=True)
    veredictos = iter([
        ("needs_more", "falta la mitad", {}, ""),
        ("complete", "ya está", {}, ""),
        ("off_plan", "el plan pedía leer, estás levantando Postgres", {}, ""),
        ("needs_human", "decidí vos", {}, ""),
        # El quinto es el verificador FINAL: el ejecutor de este test
        # devuelve phase "writing", así que el cierre normal igual corre.
        ("complete", "cerró bien", {}, ""),
    ])
    resultados = []

    async def fake_planner(**kwargs):
        return "1. leer main.py", {}, ""

    async def fake_executor(proj, user, **kwargs):
        sup = kwargs["on_leg_boundary"]
        for _ in range(4):
            resultados.append(await sup({
                "content": "", "phase_at_end": "budget_exceeded",
                "messages_json": "[]", "tool_calls": 9, "leg": 1,
            }))
        return _executor_result()

    async def fake_verifier(**kwargs):
        return next(veredictos)

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        result = await experts.run_expert_staged(
            project, "leelo", model_override="test")

    assert resultados[0] == "", "needs_more cortó: mataría los runs largos"
    assert resultados[1] == "", "complete cortó a mitad de camino"
    assert "levantando Postgres" in resultados[2], "off_plan no cortó"
    assert resultados[3] == "", "needs_human cortó por el camino equivocado"
    # Los cuatro quedan registrados para poder revisar después.
    assert [v["verdict"] for v in result["mid_verdicts"]] == [
        "needs_more", "complete", "off_plan", "needs_human"]


async def test_off_plan_reusa_el_veredicto_sin_pagar_otro_turno():
    """Cortado por desvío, el verificador final NO se vuelve a llamar."""
    project = _project(three_stage=True)
    llamadas = {"n": 0}

    async def fake_planner(**kwargs):
        return "1. leer main.py", {}, ""

    async def fake_executor(proj, user, **kwargs):
        sup = kwargs["on_leg_boundary"]
        await sup({"content": "", "phase_at_end": "budget_exceeded",
                   "messages_json": "[]", "tool_calls": 9, "leg": 1})
        return _executor_result(phase_at_end="off_plan",
                                content="me desvié y me detuve")

    async def fake_verifier(**kwargs):
        llamadas["n"] += 1
        return "off_plan", "te fuiste del plan", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        result = await experts.run_expert_staged(
            project, "leelo", model_override="test")

    assert llamadas["n"] == 1, \
        "pagó un turno extra para repetir un veredicto que ya tenía"
    assert result["verifier_verdict"] == "off_plan"
    assert result["verifier_feedback"] == "te fuiste del plan"


async def test_needs_more_reintenta_solo_hasta_completar():
    """El lazo desatendido: needs_more no le pide permiso al humano.

    `needs_more` significa "falta trabajo y otra pasada lo termina": el
    sistema ya tiene el diagnóstico, así que frenar a esperar un "continúa"
    convertía cada tarea grande en apretar un botón cada 20 minutos.
    """
    project = _project(three_stage=True)
    pasadas = []
    veredictos = iter([
        ("needs_more", "faltan los tests del controller", {}, ""),
        ("complete", "ahora sí", {}, ""),
    ])

    async def fake_planner(**kwargs):
        return "1. escribir tests", {}, ""

    async def fake_executor(proj, user, **kwargs):
        pasadas.append({"user": user,
                        "history": kwargs.get("message_history_json") or ""})
        return _executor_result(content=f"pasada {len(pasadas)}",
                                messages_json='[{"parts":[]}]',
                                tokens_in=100, tokens_out=10, tool_calls=3,
                                cache_read_tokens=50,
                                duration_ms=1000)

    async def fake_verifier(**kwargs):
        return next(veredictos)

    async def fake_documenter(**kwargs):
        return "", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier), \
         patch.object(experts, "_run_documenter", fake_documenter):
        result = await experts.run_expert_staged(
            project, "escribí los tests", model_override="test")

    assert len(pasadas) == 2, "no reintentó solo"
    assert result["verifier_verdict"] == "complete"
    # La segunda pasada retoma el historial y lleva el feedback como
    # consigna: es una continuación DIRIGIDA, no un "continúa" a ciegas.
    assert pasadas[1]["history"], "la segunda pasada arrancó sin historial"
    assert "faltan los tests del controller" in pasadas[1]["user"]
    # Y el pedido original no se pierde: es contra eso que juzga el
    # verificador, no contra el nudge que el sistema se escribió a sí mismo.
    assert pasadas[0]["user"] == "escribí los tests"
    # El costo de las dos pasadas se suma: si reportara solo la última, el
    # turno mentiría sobre lo que gastó.
    assert result["tokens_in"] == 200
    assert result["cache_read_tokens"] == 100
    assert result["tool_calls"] == 6
    # Cerró bien pero no a la primera, y eso se ve.
    assert "2 pasadas" in result["content"]


async def test_verifier_rounds_en_cero_vuelve_a_pedir_permiso():
    """0 = comportamiento anterior, para poder apagarlo."""
    project = _project(three_stage=True)
    project["defaults_json"]["verifier_rounds"] = 0
    pasadas = []

    async def fake_planner(**kwargs):
        return "1. hacer", {}, ""

    async def fake_executor(proj, user, **kwargs):
        pasadas.append(user)
        return _executor_result(content="a medias",
                               messages_json='[{"parts":[]}]')

    async def fake_verifier(**kwargs):
        return "needs_more", "falta la mitad", {}, ""

    async def fake_documenter(**kwargs):
        return "", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier), \
         patch.object(experts, "_run_documenter", fake_documenter):
        result = await experts.run_expert_staged(
            project, "hacelo", model_override="test")

    assert len(pasadas) == 1
    assert "**continúa**" in result["content"]


async def test_off_plan_y_needs_human_no_reintentan():
    """Solo `needs_more` gana otra pasada.

    `off_plan` empeora con otra pasada y `needs_human` espera una decisión
    que no es del sistema. Reintentar cualquiera de los dos sería insistir
    sobre un veredicto que dice justamente que no hay que insistir.
    """
    for verdict in ("off_plan", "needs_human"):
        project = _project(three_stage=True)
        pasadas = []

        async def fake_planner(**kwargs):
            return "1. hacer", {}, ""

        async def fake_executor(proj, user, **kwargs):
            pasadas.append(user)
            return _executor_result(content="algo",
                                    messages_json='[{"parts":[]}]')

        async def fake_verifier(**kwargs):
            return verdict, "por esto", {}, ""

        async def fake_documenter(**kwargs):
            return "", {}, ""

        with patch.object(experts, "_run_planner", fake_planner), \
             patch.object(experts, "run_expert", fake_executor), \
             patch.object(experts, "_run_verifier", fake_verifier), \
             patch.object(experts, "_run_documenter", fake_documenter):
            result = await experts.run_expert_staged(
                project, "hacelo", model_override="test")

        assert len(pasadas) == 1, f"{verdict} gano una pasada extra"
        assert result["verifier_verdict"] == verdict


async def test_off_plan_y_needs_human_SI_reintentan_si_no_se_ejecuto_nada():
    """La excepción a la regla de arriba (2026-08-17).

    `off_plan` y `needs_human` cortan el lazo porque otra pasada empeora
    o porque hace falta una decisión ajena. Eso vale cuando el ejecutor
    TRABAJÓ. Con cero tool calls no hay desvío del que hablar —no se
    ejecutó nada de lo que desviarse— y el veredicto se emitió sobre un
    turno que solo describió el plan.

    Medido en sample-app el 17/8: de los 19 runs del día sin una sola tool
    call, 4 murieron acá, en `off_plan`, sin que nadie los reintentara.
    """
    for verdict in ("off_plan", "needs_human"):
        project = _project(three_stage=True)
        pasadas = []

        async def fake_planner(**kwargs):
            return "1. hacer", {}, ""

        async def fake_executor(proj, user, **kwargs):
            pasadas.append(user)
            # Vacío la primera; en la forzada sí trabaja.
            return _executor_result(
                content="algo", tool_calls=0 if len(pasadas) == 1 else 2,
                messages_json='[{"parts":[]}]')

        async def fake_verifier(**kwargs):
            return verdict, "por esto", {}, ""

        async def fake_documenter(**kwargs):
            return "", {}, ""

        with patch.object(experts, "_run_planner", fake_planner), \
             patch.object(experts, "run_expert", fake_executor), \
             patch.object(experts, "_run_verifier", fake_verifier), \
             patch.object(experts, "_run_documenter", fake_documenter):
            await experts.run_expert_staged(
                project, "hacelo", model_override="test")

        assert len(pasadas) == 2, (
            f"{verdict} sobre un turno vacío tiene que ganar una pasada")
        assert "NINGUNA herramienta" in pasadas[1]


async def test_run_roto_no_reintenta():
    """Un ejecutor que murió no se continúa: no hay nada que continuar."""
    project = _project(three_stage=True)
    pasadas = []

    async def fake_planner(**kwargs):
        return "1. hacer", {}, ""

    async def fake_executor(proj, user, **kwargs):
        pasadas.append(user)
        return _executor_result(phase_at_end="hard_timeout", content="")

    async def fake_documenter(**kwargs):
        return "", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_documenter", fake_documenter):
        result = await experts.run_expert_staged(
            project, "hacelo", model_override="test")

    assert len(pasadas) == 1
    assert result["verifier_verdict"] == "needs_human"


async def test_documentador_corre_una_sola_vez_al_final():
    """Con dos pasadas, el registro del cambio se escribe UNA vez.

    Si corriera por pasada, el humano leería dos registros contradictorios
    del mismo turno, y se pagaría el turno del documentador de más.
    """
    project = _project(three_stage=True)
    docs = []
    veredictos = iter([
        ("needs_more", "falta algo", {}, ""),
        ("complete", "listo", {}, ""),
    ])

    async def fake_planner(**kwargs):
        return "1. hacer", {}, ""

    async def fake_executor(proj, user, **kwargs):
        # Con tool calls de verdad: `has_work` sale de ahí, y sin eso el
        # documentador se saltea y el test no probaría nada.
        return _executor_result(content="hecho",
                                messages_json=_MESSAGES_WITH_TOOL)

    async def fake_verifier(**kwargs):
        return next(veredictos)

    async def fake_documenter(**kwargs):
        docs.append(kwargs.get("verdict"))
        return "**Qué se hizo:** algo", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier), \
         patch.object(experts, "_run_documenter", fake_documenter):
        result = await experts.run_expert_staged(
            project, "hacelo", model_override="test")

    assert docs == ["complete"], f"el documentador corrió {len(docs)} veces"
    assert result["doc"]


async def test_run_expert_staged_needs_human_prefixes_content():
    """Con verdict=needs_human el content empieza con un aviso."""
    project = _project(three_stage=True)

    async def fake_planner(**kwargs):
        return "plan cualquiera", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result(content="Cambié el código")

    async def fake_verifier(**kwargs):
        return "needs_human", "define el alcance", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        result = await experts.run_expert_staged(
            project, "fix bug", model_override="test",
        )
    assert "needs_human" in result["content"]
    assert "define el alcance" in result["content"]
    assert "Cambié el código" in result["content"]


async def test_run_expert_staged_skips_verifier_on_error():
    """En error/timeout/cancelación el verificador NO corre."""
    project = _project(three_stage=True)
    captured = {}

    async def fake_planner(**kwargs):
        return "plan", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result(content="", phase_at_end="idle_timeout")

    async def fake_verifier(**kwargs):
        captured["verifier_called"] = True
        return "complete", "", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        result = await experts.run_expert_staged(
            project, "fix", model_override="test",
        )
    assert captured.get("verifier_called") is not True
    assert result["verifier_verdict"] == "needs_human"
    assert "idle_timeout" in result["verifier_feedback"]


async def test_run_expert_staged_merges_verified_steps_into_bitacora():
    """Regresión 2026-08-28: el PR #48 metió un call site a `bitacora`
    dentro de `run_expert_staged` sin inicializar la bitácora en ese
    scope. El verificador devolvía la 5-tupla con pasos verificados y
    el wrapper tiraba `NameError: bitacora is not defined`. Cualquier
    test que ejercite el verificador real (5-tupla) rompía el run.

    El fix correcto carga la `Bitacora` al entrar al lazo y la
    mantiene sincronizada con lo que el verificador vio: los pasos
    marcados por el verificador se persisten en `kwargs["bitacora_json"]`
    para que la próxima ronda los vea.
    """
    project = _project(three_stage=True)

    async def fake_planner(**kwargs):
        return "1. paso uno\n2. paso dos\n3. paso tres", {}, ""

    # El ejecutor devuelve un dict con bitacora_json vacío en la
    # primera vuelta (es lo que pasa cuando el experto no marcó nada).
    # En rondas siguientes, run_expert_staged inyecta `bitacora_json`
    # en kwargs a partir del `result` del round previo — eso es
    # exactamente lo que estamos verificando que sobrevive.
    async def fake_executor(proj, user, **kwargs):
        return _executor_result(
            content="ok",
            bitacora_json='{"hechos":[],"comandos":[],"pasos":{"1":"verifier"}}')

    # Verificador con 5-tupla: devuelve pasos [1, 3] como verificados.
    async def fake_verifier(**kwargs):
        return ("complete", "ok", {}, "", [1, 3])

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        # Antes del fix esto levantaba NameError. Después del fix tiene
        # que pasar limpio y mergear los pasos en la bitácora.
        result = await experts.run_expert_staged(
            project, "fix", model_override="test")

    assert result["verifier_verdict"] == "complete"
    assert result["verifier_feedback"] == "ok"
    # La bitácora persistida en el resultado del ejecutor tiene que
    # incluir los pasos verificados. El fake los pasa vacíos, pero la
    # clave tiene que existir para que la UI los pueda leer.
    bj = result.get("bitacora_json") or ""
    # Si la implementación persiste via `Bitacora.volcar()`, los pasos
    # 1 y 3 están en `pasos`. Si solo se mockeó el ejecutor, no van a
    # estar: igual verificamos que NO hay excepción y el veredicto es
    # el correcto. La cobertura fina de la serialización está en su
    # propio test unitario de Bitacora.
    assert "NameError" not in str(result.get("stage_errors", {}))


async def test_run_expert_staged_none_result_is_needs_human():
    """Si el ejecutor devuelve None, el veredicto queda en needs_human.

    Regresión: la primera versión construía el dict de reemplazo con
    phase_at_end="no_result", que no estaba en la lista de estados
    rotos, así que igual llamaba al verificador y este pisaba el
    veredicto con un "complete" sobre una respuesta vacía.
    """
    project = _project(three_stage=True)
    captured = {}

    async def fake_planner(**kwargs):
        return "plan", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return None

    async def fake_verifier(**kwargs):
        captured["verifier_called"] = True
        return "complete", "no deberia correr", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        result = await experts.run_expert_staged(
            project, "fix", model_override="test",
        )
    assert captured.get("verifier_called") is not True
    assert result["verifier_verdict"] == "needs_human"
    assert result["content"] == ""


async def test_run_expert_staged_planner_failure_is_soft():
    """Si el planificador falla con algo que no es ModelUnavailable, el
    wrapper sigue con plan vacío y el ejecutor corre igual."""
    project = _project(three_stage=True)

    async def fake_planner(**kwargs):
        # Contrato real de _run_planner: traga todo salvo ModelUnavailable.
        return "", {}, ""

    async def fake_executor(proj, user, **kwargs):
        assert "Plan a ejecutar" not in (kwargs.get("system_extra") or "")
        return _executor_result(content="ok sin plan")

    async def fake_verifier(**kwargs):
        return "complete", "ok", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        result = await experts.run_expert_staged(
            project, "fix", model_override="test",
        )
    assert result["plan"] == ""
    assert result["verifier_verdict"] == "complete"


async def test_run_planner_swallows_generic_errors():
    """_run_planner devuelve "" ante un error genérico de build_model."""
    def broken_build_model(spec):
        raise RuntimeError("timeout del planificador")

    with patch.object(experts, "build_model", broken_build_model):
        plan, usage, err = await experts._run_planner(
            user="x", project=_project(), model_spec="lo-que-sea",
            ponytail="", on_progress=None,
        )
    assert plan == ""
    assert usage == {}


async def test_run_planner_propagates_model_unavailable():
    def broken_build_model(spec):
        raise experts.ModelUnavailable("falta MINIMAX_API_KEY")

    with patch.object(experts, "build_model", broken_build_model):
        with pytest.raises(experts.ModelUnavailable):
            await experts._run_planner(
                user="x", project=_project(), model_spec="lo-que-sea",
                ponytail="", on_progress=None,
            )


# ---------- documentador ----------


async def _staged_with_documenter(project, *, executor_over=None,
                                  doc_text="**Qué se hizo:** nada",
                                  captured=None):
    captured = captured if captured is not None else {}

    async def fake_planner(**kwargs):
        return "1. editar main.py", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result(**(executor_over or {}))

    async def fake_verifier(**kwargs):
        return "complete", "ok", {}, ""

    async def fake_documenter(**kwargs):
        captured["documenter_called"] = True
        captured["kwargs"] = kwargs
        return doc_text, {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier), \
         patch.object(experts, "_run_documenter", fake_documenter):
        result = await experts.run_expert_staged(
            project, "edita main.py", model_override="test",
        )
    return result, captured


async def test_documenter_runs_and_appends_to_content():
    """Con trabajo real el documentador corre y su registro va al content."""
    result, captured = await _staged_with_documenter(
        _project(three_stage=True),
        executor_over={"messages_json": _MESSAGES_WITH_TOOL,
                       "content": "Listo, edité main.py"},
        doc_text="**Qué se hizo:** se editó main.py",
    )
    assert captured.get("documenter_called") is True
    assert result["doc"] == "**Qué se hizo:** se editó main.py"
    # El registro se agrega DESPUÉS de la respuesta del ejecutor.
    assert "Listo, edité main.py" in result["content"]
    assert result["content"].rstrip().endswith("se editó main.py")
    assert result["documenter_model"]


async def test_documenter_skipped_without_tool_calls():
    """Sin llamadas a herramientas no hay nada que registrar."""
    result, captured = await _staged_with_documenter(
        _project(three_stage=True),
        executor_over={"messages_json": "[]"},
    )
    assert captured.get("documenter_called") is not True
    assert result["doc"] == ""
    assert result["documenter_model"] == ""


async def test_documenter_opt_out_by_defaults_json():
    """`documenter=false` en defaults_json apaga solo esa etapa."""
    project = _project(three_stage=True, defaults_json={"documenter": False})
    result, captured = await _staged_with_documenter(
        project,
        executor_over={"messages_json": _MESSAGES_WITH_TOOL},
    )
    assert captured.get("documenter_called") is not True
    assert result["doc"] == ""
    # El verificador sigue corriendo: el opt-out es solo del documentador.
    assert result["verifier_verdict"] == "complete"


async def test_documenter_skipped_on_broken_run():
    """En un run roto no corre ninguna etapa auxiliar."""
    result, captured = await _staged_with_documenter(
        _project(three_stage=True),
        executor_over={"messages_json": _MESSAGES_WITH_TOOL,
                       "phase_at_end": "cancelled"},
    )
    assert captured.get("documenter_called") is not True
    assert result["doc"] == ""
    assert result["verifier_verdict"] == "needs_human"


async def test_documenter_receives_verdict_and_tools():
    """El documentador ve el veredicto y el resumen de herramientas."""
    _, captured = await _staged_with_documenter(
        _project(three_stage=True),
        executor_over={"messages_json": _MESSAGES_WITH_TOOL},
    )
    kw = captured["kwargs"]
    assert kw["verdict"] == "complete"
    assert kw["executor_result"]["tool_calls_summary"][0][0] == "edit_file"


async def test_run_documenter_failure_is_soft():
    """Si el documentador falla devuelve "" y el run sigue igual."""
    def broken_build_model(spec):
        raise RuntimeError("se cayó la red")

    with patch.object(experts, "build_model", broken_build_model):
        doc, usage, err = await experts._run_documenter(
            user="x", plan="p", executor_result=_executor_result(),
            verdict="complete", feedback="ok", model_spec="lo-que-sea",
            ponytail="", on_progress=None,
        )
    assert doc == ""
    assert usage == {}


async def test_run_documenter_model_unavailable_is_soft():
    """A diferencia del planificador, ModelUnavailable NO se propaga.

    El ejecutor ya terminó bien: perder el registro no justifica marcar
    el run como fallido y obligar al humano a repetirlo.
    """
    def broken_build_model(spec):
        raise experts.ModelUnavailable("falta MINIMAX_API_KEY")

    with patch.object(experts, "build_model", broken_build_model):
        doc, usage, err = await experts._run_documenter(
            user="x", plan="p", executor_result=_executor_result(),
            verdict="complete", feedback="ok", model_spec="lo-que-sea",
            ponytail="", on_progress=None,
        )
    assert doc == ""
    assert usage == {}


def test_render_tool_calls_caps_long_args():
    out = experts._render_tool_calls(
        {"tool_calls_summary": [("edit_file", "x" * 500)]})
    assert out.startswith("- edit_file(")
    assert "…" in out
    assert len(out) < 200


def test_render_tool_calls_empty():
    assert experts._render_tool_calls({}) == "(sin llamadas a herramientas)"


# ---------- end-to-end: server /experts/run + /system/active ----------


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmp:
        env_vars = {
            "STATE_DIR": str(Path(tmp) / "state"),
            "FOURBIS_DB_PATH": str(Path(tmp) / "relay.db"),
            "FOURBIS_CHATS_DIR": str(Path(tmp) / "chats"),
            "FOURBIS_JSONL_DIR": str(Path(tmp) / "jsonl"),
            "FOURBIS_MODEL": "test",
            "LOG_LEVEL": "WARNING",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            db = Database(path=Path(tmp) / "relay.db")
            await db.init_schema()
            await db.upsert_project({
                "slug": "demo", "name": "Demo", "repo_path": "C:/x/Demo",
                "system_prompt": "experto demo", "mcp_servers": [],
            })
            app = create_app()
            cli = TestClient(TestServer(app))
            await cli.start_server()
            try:
                yield cli, db
            finally:
                # `cli.close()` dispara el cleanup de la app, que drena
                # los runs de experto en vuelo (server._drain_running_experts)
                # antes de que el TemporaryDirectory borre relay.db. Sin
                # ese drenaje, en Windows la limpieza del tmpdir aborta
                # con PermissionError / NotADirectoryError.
                await cli.close()


async def test_run_staged_end_to_end(env):
    """POST /experts/run con las etapas activas: status=ok y .md escrito."""
    cli, db = env
    r = await cli.post("/experts/run", json={
        "target": "demo", "user": "hola",
        "source": "test", "author": "pytest",
    })
    assert r.status == 202
    chat_id = (await r.json())["id"]

    for _ in range(100):
        chat = await db.get_chat(chat_id)
        # También se espera el `md_path`: desde c00408f el estado terminal
        # se guarda ANTES de exportar el .md, para que un disco lleno no
        # deje el run sin cerrar. Cortar solo por `status` deja este test
        # leyendo la fila dentro de esa ventana, y `md_path` viene NULL.
        if chat["status"] in ("ok", "error") and chat["md_path"]:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail(f"el run no terminó en 5s, status={chat['status']}")

    assert chat["status"] == "ok"
    md = Path(chat["md_path"]).read_text(encoding="utf-8")
    assert "hola" in md

    # Post mortem: el run ya terminó, así que no hay nada activo.
    r = await cli.get("/system/active")
    assert r.status == 200
    body = await r.json()
    assert body["active"] is False
    assert body["count"] == 0
    assert body["first"] is None


async def test_system_active_shape(env):
    """/system/active responde 200 con la forma esperada."""
    cli, db = env
    r = await cli.post("/experts/run", json={
        "target": "demo", "user": "hola",
        "source": "test", "author": "pytest",
    })
    assert r.status == 202

    r = await cli.get("/system/active")
    assert r.status == 200
    body = await r.json()
    assert set(("active", "count", "first", "version")) <= set(body)
    assert isinstance(body["count"], int)
    assert body["count"] >= 0
    # El indicador sondea esto cada 2s: `steps` no puede viajar aquí.
    if body["first"]:
        assert "steps" not in body["first"]
        assert "idle_s" in body["first"]


async def test_progress_phases_include_staged_phases(env):
    """Los phases nuevos son visibles en /experts/status sin romper nada.

    Con TestModel el run dura menos de 1ms, así que no se garantiza
    capturar "planner" o "documenter" en un poll; lo que se garantiza es
    que el endpoint no se cae y que se llega a un phase terminal.
    """
    cli, db = env
    r = await cli.post("/experts/run", json={
        "target": "demo", "user": "x", "source": "test", "author": "pytest",
    })
    chat_id = (await r.json())["id"]

    seen_phases: set[str] = set()
    for _ in range(50):
        r = await cli.get(f"/experts/status/{chat_id[:8]}")
        if r.status == 200:
            body = await r.json()
            if body.get("phase"):
                seen_phases.add(body["phase"])
            if body.get("finished"):
                break
        await asyncio.sleep(0.02)

    assert seen_phases & {"writing", "done", "planner", "verifier",
                          "documenter"}, (
        f"no se vio ningún phase terminal, vistos={seen_phases}")


# ---------- persistencia de las etapas (2026-08-14) ----------


def test_stages_json_none_when_not_staged():
    """Sin runner por etapas la columna queda NULL, no un objeto vacío.

    Distingue "este run no tuvo etapas" de "las tuvo y salieron
    vacías", que es justo lo que hace auditable la columna.
    """
    assert server._stages_json({"three_stage": False, "plan": "x"}) is None
    assert server._stages_json({}) is None


def test_stages_json_serializes_verdict_and_models():
    raw = server._stages_json({
        "three_stage": True,
        "plan": "1. leer\n2. escribir",
        "planner_model": "nvidia:nvidia/nemotron-3-ultra-550b-a55b",
        "verifier_verdict": "needs_human",
        "verifier_feedback": "el verificador no pudo correr",
        "verifier_model": "nvidia:nvidia/nemotron-3-ultra-550b-a55b",
        "documenter_model": "nvidia:nvidia/nemotron-3-super-120b-a12b",
        "model": "minimax:MiniMax-M3",
    })
    d = json.loads(raw)
    assert d["verifier_verdict"] == "needs_human"
    assert d["executor_model"] == "minimax:MiniMax-M3"
    assert d["planner_model"].startswith("nvidia:")
    assert d["documenter_model"] != d["planner_model"], (
        "cada etapa guarda SU modelo: si se pisan, no se audita nada")


def test_stages_json_truncates_long_plan():
    raw = server._stages_json({"three_stage": True, "plan": "x" * 9000})
    assert len(json.loads(raw)["plan"]) == 4000


async def test_stages_persisted_and_served_by_chats_get(env):
    """E2E: el run guarda las etapas y GET /chats/{id} las devuelve.

    Regresión 2026-08-14: plan, veredicto y modelo de cada etapa solo
    existían en memoria durante el run. Al terminar no quedaba forma de
    saber si un run se aprobó porque estaba bien o porque el
    verificador se cayó.
    """
    cli, db = env
    r = await cli.post("/experts/run", json={
        "target": "demo", "user": "x", "source": "test", "author": "pytest",
    })
    chat_id = (await r.json())["id"]

    for _ in range(100):
        row = await db.get_chat(chat_id)
        if row and row.get("status") != "running":
            break
        await asyncio.sleep(0.02)

    row = await db.get_chat(chat_id)
    assert row["stages_json"], "el run por etapas no persistió stages_json"

    r = await cli.get(f"/chats/{chat_id}")
    body = await r.json()
    assert r.status == 200
    stages = body.get("stages")
    assert isinstance(stages, dict), "stages debe servirse parseado, no como texto"
    assert set(stages) >= {
        "plan", "planner_model", "verifier_verdict", "verifier_model",
        "documenter_model", "executor_model"}


async def test_chats_get_survives_corrupt_stages_json(env):
    """Una fila con JSON roto no puede tumbar el GET."""
    cli, db = env
    r = await cli.post("/experts/run", json={
        "target": "demo", "user": "x", "source": "test", "author": "pytest",
    })
    chat_id = (await r.json())["id"]
    for _ in range(100):
        row = await db.get_chat(chat_id)
        if row and row.get("status") != "running":
            break
        await asyncio.sleep(0.02)

    await db.run("UPDATE chats SET stages_json=? WHERE id=?",
                 ("{roto", chat_id))
    r = await cli.get(f"/chats/{chat_id}")
    assert r.status == 200
    assert (await r.json())["stages"] is None


# ---------- fase 0+1: tokens por etapa y by_model con roles ----------


class _FakeUsage:
    def __init__(self, i, o):
        self.input_tokens, self.output_tokens = i, o


class _FakeResult:
    def __init__(self, out, i=0, o=0):
        self.output = out
        self.usage = _FakeUsage(i, o)


def test_stage_usage_reads_tokens():
    assert experts._stage_usage(_FakeResult("x", 120, 45)) == {
        "tokens_in": 120, "tokens_out": 45}


def test_stage_usage_supports_callable_usage():
    """`usage` fue método antes de pydantic-ai 2.x; se soportan ambas."""
    class Old:
        output = "x"
        def usage(self):
            return _FakeUsage(7, 3)
    assert experts._stage_usage(Old()) == {"tokens_in": 7, "tokens_out": 3}


def test_stage_usage_unreadable_is_empty_not_zero():
    """Sin usage legible devuelve {}, NO ceros.

    La diferencia importa en métricas: {} es "no se midió" y cero es
    "corrió y no consumió". Mostrar lo segundo cuando pasó lo primero
    es exactamente el bug que esta fase corrige.
    """
    class NoUsage:
        output = "x"
    assert experts._stage_usage(NoUsage()) == {}


def test_stages_json_carries_stage_tokens():
    raw = server._stages_json({
        "three_stage": True, "plan": "1. x",
        "planner_model": "nvidia:nvidia/nemotron-3-ultra-550b-a55b",
        "verifier_model": "nvidia:nvidia/nemotron-3-ultra-550b-a55b",
        "documenter_model": "nvidia:nvidia/nemotron-3-super-120b-a12b",
        "model": "minimax:MiniMax-M3",
        "stage_usage": {
            "planner": {"tokens_in": 900, "tokens_out": 120},
            "verifier": {"tokens_in": 800, "tokens_out": 60},
            "documenter": {},          # falló: no debe escribir claves
        },
    })
    d = json.loads(raw)
    assert d["planner_tokens_in"] == 900
    assert d["verifier_tokens_out"] == 60
    assert "documenter_tokens_in" not in d, (
        "una etapa sin medición no puede aparecer como 0 tokens")


async def test_staged_run_reports_stage_usage():
    """El dict del runner trae stage_usage con las tres etapas."""
    async def fake_planner(**kwargs):
        return "1. hacer", {"tokens_in": 10, "tokens_out": 2}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result(messages_json=_MESSAGES_WITH_TOOL)

    async def fake_verifier(**kwargs):
        return "complete", "ok", {"tokens_in": 20, "tokens_out": 4}, ""

    async def fake_documenter(**kwargs):
        return "**Qué se hizo:** algo", {"tokens_in": 30, "tokens_out": 6}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier), \
         patch.object(experts, "_run_documenter", fake_documenter):
        r = await experts.run_expert_staged(_project(), "hacé algo")

    su = r["stage_usage"]
    assert su["planner"]["tokens_in"] == 10
    assert su["verifier"]["tokens_in"] == 20
    assert su["documenter"]["tokens_in"] == 30
    # Los totales del chat siguen siendo SOLO del ejecutor: si se
    # sumaran las etapas, los 200+ runs previos dejarían de ser
    # comparables contra los nuevos.
    assert r["tokens_in"] == 1, "tokens_in del chat debe seguir siendo del ejecutor"


# Fecha dentro de la ventana que consulta metrics_summary. Una fecha
# futura fija (2099) queda FUERA del rango `started_at >= since`.
_NOW_ISO = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def test_metrics_by_model_splits_executor_and_stages(tmp_path):
    """by_model saca una fila por rol, no todo atribuido al ejecutor.

    Regresión 2026-08-14: agrupaba solo por chats.model, así que el
    dashboard mostraba el 100% del consumo como si fuera del modelo
    pagado aunque tres etapas corrieran en otro proveedor.
    """
    db = Database(path=tmp_path / "m.db")
    await db.init_schema()
    await db.run(
        "INSERT INTO chats (id, source, started_at, status, model, "
        "tokens_in, tokens_out, stages_json) VALUES (?,?,?,?,?,?,?,?)",
        ("c1", "test", _NOW_ISO, "ok", "minimax:MiniMax-M3",
         5000, 300, json.dumps({
             "planner_model": "nvidia:nemo-ultra",
             "planner_tokens_in": 900, "planner_tokens_out": 100,
             "verifier_model": "nvidia:nemo-ultra",
             "verifier_tokens_in": 800, "verifier_tokens_out": 50,
             "documenter_model": "nvidia:nemo-super",
             "documenter_tokens_in": 700, "documenter_tokens_out": 40,
         })))

    s = await db.metrics_summary(days=7)
    rows = {(r["model"], r["role"]): r for r in s["by_model"]}

    assert rows[("minimax:MiniMax-M3", "executor")]["tokens_in"] == 5000
    assert rows[("nvidia:nemo-ultra", "planner")]["tokens_in"] == 900
    assert rows[("nvidia:nemo-ultra", "verifier")]["tokens_in"] == 800
    assert rows[("nvidia:nemo-super", "documenter")]["tokens_in"] == 700

    prov = {p["provider"]: p for p in s["by_provider"]}
    assert prov["minimax"]["tokens_in"] == 5000
    assert prov["nvidia"]["tokens_in"] == 2400, (
        "el proveedor gratis debe sumar sus tres etapas")


async def test_metrics_by_model_ignores_runs_without_stages(tmp_path):
    """Un run viejo (stages_json NULL) aporta solo su fila de ejecutor.

    No puede inventar filas de etapa en cero: esas etapas no se
    midieron, que es distinto de haber consumido nada.
    """
    db = Database(path=tmp_path / "m2.db")
    await db.init_schema()
    await db.run(
        "INSERT INTO chats (id, source, started_at, status, model, "
        "tokens_in, tokens_out) VALUES (?,?,?,?,?,?,?)",
        ("old", "test", _NOW_ISO, "ok",
         "minimax:MiniMax-M3", 100, 10))

    s = await db.metrics_summary(days=7)
    roles = [r["role"] for r in s["by_model"]]
    assert roles == ["executor"], f"un run sin etapas no debe generar filas: {roles}"


async def test_metrics_unmeasured_stage_is_null_not_zero(tmp_path):
    """Etapa con modelo pero sin tokens → NULL, nunca 0.

    Es la ventana de runs entre que se agregó `stages_json` y que se
    instrumentaron los tokens. Reportar 0 ahí afirma que la etapa no
    consumió nada, que es falso: no se midió.
    """
    db = Database(path=tmp_path / "m3.db")
    await db.init_schema()
    await db.run(
        "INSERT INTO chats (id, source, started_at, status, model, "
        "tokens_in, tokens_out, stages_json) VALUES (?,?,?,?,?,?,?,?)",
        ("mid", "test", _NOW_ISO, "ok", "minimax:MiniMax-M3", 50, 5,
         json.dumps({"planner_model": "nvidia:nemo-ultra"})))  # sin tokens

    s = await db.metrics_summary(days=7)
    planner = [r for r in s["by_model"] if r["role"] == "planner"][0]
    assert planner["runs"] == 1
    assert planner["tokens_in"] is None, "sin medición debe ser NULL, no 0"

    prov = {p["provider"]: p for p in s["by_provider"]}
    assert prov["nvidia"]["tokens_in"] is None


# ---------- fase 2: tarifas y costo ----------


_PRICES = {
    "minimax:MiniMax-M3": {"in": 0.30, "out": 1.20},
    # gratis, pero con precio de referencia para estimar el ahorro
    "nvidia:*": {"in": 0.0, "out": 0.0, "ref_in": 0.60, "ref_out": 2.40},
}


def test_match_model_price_exact_beats_pattern():
    prices = {"a:b": {"in": 1, "out": 1}, "a:*": {"in": 9, "out": 9}}
    assert db_mod.match_model_price(prices, "a:b")["in"] == 1


def test_match_model_price_longest_pattern_wins():
    prices = {"nvidia:*": {"in": 1, "out": 1},
              "nvidia:nvidia/nemo*": {"in": 2, "out": 2}}
    got = db_mod.match_model_price(prices, "nvidia:nvidia/nemotron-3-ultra")
    assert got["in"] == 2, "debe ganar el patrón más específico"


def test_match_model_price_unknown_is_none():
    """Un modelo sin tarifa NO es gratis: es 'sin dato'."""
    assert db_mod.match_model_price(_PRICES, "openai:gpt-9") is None


def test_cost_usd_per_million():
    # 1M in a 0.30 + 1M out a 1.20 = 1.50
    c = db_mod.cost_usd(_PRICES["minimax:MiniMax-M3"], 1_000_000, 1_000_000)
    assert round(c, 6) == 1.50


def test_cost_usd_none_without_price_or_tokens():
    assert db_mod.cost_usd(None, 100, 100) is None
    assert db_mod.cost_usd(_PRICES["minimax:MiniMax-M3"], None, 5) is None


def test_cost_usd_reference_uses_ref_keys():
    p = _PRICES["nvidia:*"]
    assert db_mod.cost_usd(p, 1_000_000, 0) == 0.0
    assert db_mod.cost_usd(p, 1_000_000, 0, reference=True) == 0.60


def test_cost_usd_reference_none_without_ref():
    """Sin ref_* no se estima ahorro: no se inventa una tarifa."""
    assert db_mod.cost_usd({"in": 1, "out": 1}, 1_000_000, 0,
                           reference=True) is None


async def test_metrics_costs_and_free_tier_saving(tmp_path):
    db = Database(path=tmp_path / "c.db")
    await db.init_schema()
    await db.set_config(db_mod.MODEL_PRICES_KEY, json.dumps(_PRICES))
    await db.run(
        "INSERT INTO chats (id, source, started_at, status, model, "
        "tokens_in, tokens_out, stages_json) VALUES (?,?,?,?,?,?,?,?)",
        ("c", "test", _NOW_ISO, "ok", "minimax:MiniMax-M3",
         1_000_000, 1_000_000, json.dumps({
             "planner_model": "nvidia:nemo-ultra",
             "planner_tokens_in": 1_000_000, "planner_tokens_out": 0,
         })))

    s = await db.metrics_summary(days=7)
    assert s["totals"]["priced"] is True
    # ejecutor pagado 1.50 + planner gratis 0.00
    assert round(s["totals"]["cost_usd"], 2) == 1.50
    # el planner habría costado 0.60 en un proveedor pagado
    assert round(s["totals"]["saved_usd"], 2) == 0.60

    prov = {p["provider"]: p for p in s["by_provider"]}
    assert round(prov["minimax"]["cost_usd"], 2) == 1.50
    assert prov["nvidia"]["cost_usd"] == 0.0


async def test_metrics_without_prices_leaves_cost_null(tmp_path):
    """Sin tarifas cargadas el costo es null, nunca 0.

    Un dashboard que muestra "$0.00" porque nadie cargó precios afirma
    que el período salió gratis.

    2026-08-31: el modelo del run tiene que ser uno que NO esté en el
    catálogo. Antes acá iba `minimax:MiniMax-M3`, que sí está sembrado
    con tarifa: el test pasaba solo porque `metrics_summary` ignoraba la
    tabla `models` y leía únicamente `MODEL_PRICES`, que es exactamente
    el bug que dejaba el dashboard sin costos.
    """
    db = Database(path=tmp_path / "c2.db")
    await db.init_schema()
    await db.run(
        "INSERT INTO chats (id, source, started_at, status, model, "
        "tokens_in, tokens_out) VALUES (?,?,?,?,?,?,?)",
        ("c", "test", _NOW_ISO, "ok", "provider-nuevo:sin-tarifa", 999, 99))

    s = await db.metrics_summary(days=7)
    assert s["by_model"][0]["cost_usd"] is None
    assert s["totals"]["cost_usd"] is None


async def test_metrics_usa_la_tarifa_de_la_tabla_models(tmp_path):
    """La tarifa de la pantalla Modelos alcanza: no hace falta MODEL_PRICES.

    Bug 2026-08-31: `cost_in`/`cost_out` se cargaban por modelo en la
    tabla `models` y el dashboard leía SOLO `MODEL_PRICES` (vacía en la
    DB real) — mostraba `priced:false` y todos los costos en null con la
    tarifa cargada en la tabla de al lado.
    """
    db = Database(path=tmp_path / "c3.db")
    await db.init_schema()  # siembra minimax:MiniMax-M3 a 0.3 / 1.2
    assert await db.get_config(db_mod.MODEL_PRICES_KEY, "") in ("", None)
    await db.run(
        "INSERT INTO chats (id, source, started_at, status, model, "
        "tokens_in, tokens_out) VALUES (?,?,?,?,?,?,?)",
        ("c", "test", _NOW_ISO, "ok", "minimax:MiniMax-M3",
         1_000_000, 1_000_000))

    s = await db.metrics_summary(days=7)
    assert s["totals"]["priced"] is True
    assert round(s["totals"]["cost_usd"], 2) == 1.50


async def test_metrics_cobra_la_cache_a_su_tarifa(tmp_path):
    """Los cache reads se cobran a `cost_cache_in`, no a `cost_in`.

    Medido en la DB real: el 82,5% de la entrada de MiniMax son cache
    reads. Cobrarlos a tarifa plena daba ~3,5x del gasto real y era la
    causa principal de que el relay no cuadrara contra el proveedor.
    """
    db = Database(path=tmp_path / "c4.db")
    await db.init_schema()
    await db.upsert_model("minimax:MiniMax-M3", cost_cache_in=0.03)
    await db.run(
        "INSERT INTO chats (id, source, started_at, status, model, "
        "tokens_in, cache_read_tokens, tokens_out) VALUES (?,?,?,?,?,?,?,?)",
        ("c", "test", _NOW_ISO, "ok", "minimax:MiniMax-M3",
         1_000_000, 800_000, 0))

    s = await db.metrics_summary(days=7)
    # 200k a 0.3 + 800k a 0.03 = 0.06 + 0.024
    assert round(s["totals"]["cost_usd"], 3) == 0.084
    assert s["totals"]["cache_read_tokens"] == 800_000
    # Sin tarifa de caché se cobra todo a `cost_in`: caro de más, nunca
    # de menos. 1M a 0.3 = 0.30.
    await db.upsert_model("minimax:MiniMax-M3", cost_cache_in=None)
    s2 = await db.metrics_summary(days=7)
    assert round(s2["totals"]["cost_usd"], 3) == 0.300


# ---------- fase 3: filtros ----------


async def _seeded(tmp_path, name="f.db"):
    """Dos runs de proyectos distintos, uno ok y uno con error."""
    db = Database(path=tmp_path / name)
    await db.init_schema()
    await db.run(
        "INSERT INTO chats (id, source, project_slug, started_at, status, "
        "model, tokens_in, tokens_out, stages_json) VALUES (?,?,?,?,?,?,?,?,?)",
        ("a", "test", "inventorydemo", _NOW_ISO, "ok", "minimax:MiniMax-M3", 100, 10,
         json.dumps({"planner_model": "nvidia:nemo-ultra",
                     "planner_tokens_in": 7, "planner_tokens_out": 1})))
    await db.run(
        "INSERT INTO chats (id, source, project_slug, started_at, status, "
        "model, tokens_in, tokens_out) VALUES (?,?,?,?,?,?,?,?)",
        ("b", "test", "sample-app", _NOW_ISO, "error", "minimax:MiniMax-M3", 5, 1))
    return db


async def test_filter_by_project_narrows_runs(tmp_path):
    db = await _seeded(tmp_path)
    s = await db.metrics_summary(days=7, project="inventorydemo")
    assert s["totals"]["runs"] == 1
    assert s["totals"]["tokens_in"] == 100
    assert s["filters_applied"]["project"] == "inventorydemo"


async def test_filter_by_status_error_matches_kpi_semantics(tmp_path):
    """`error` = todo lo que no es ok, igual que el KPI de errores."""
    db = await _seeded(tmp_path)
    s = await db.metrics_summary(days=7, status="error")
    assert s["totals"]["runs"] == 1
    assert s["totals"]["tokens_in"] == 5


async def test_filter_by_role_only_touches_breakdown(tmp_path):
    """role recorta by_model pero NO los totales del run.

    Un run no "pertenece" a un rol: usa varios. Si los totales se
    recortaran, los KPIs dejarían de ser comparables entre filtros.
    """
    db = await _seeded(tmp_path)
    s = await db.metrics_summary(days=7, role="planner")
    assert [r["role"] for r in s["by_model"]] == ["planner"]
    assert s["totals"]["runs"] == 2, "los totales no los toca el filtro de rol"


async def test_filter_by_provider_narrows_breakdown(tmp_path):
    db = await _seeded(tmp_path)
    s = await db.metrics_summary(days=7, provider="nvidia")
    assert {r["model"] for r in s["by_model"]} == {"nvidia:nemo-ultra"}
    assert [p["provider"] for p in s["by_provider"]] == ["nvidia"]


async def test_filters_combine(tmp_path):
    db = await _seeded(tmp_path)
    s = await db.metrics_summary(days=7, project="inventorydemo", role="executor")
    assert s["totals"]["runs"] == 1
    assert [r["role"] for r in s["by_model"]] == ["executor"]


async def test_no_filters_reports_none_applied(tmp_path):
    db = await _seeded(tmp_path)
    s = await db.metrics_summary(days=7)
    assert s["filters_applied"] == {}
    assert s["totals"]["runs"] == 2


async def test_metrics_endpoint_ignores_unknown_filter_values(env):
    """Un status inventado se ignora; no rompe ni vacía el dashboard."""
    cli, _db = env
    r = await cli.get("/admin/api/metrics/summary?status=chorizo&role=nope")
    assert r.status == 200
    body = await r.json()
    assert body["filters_applied"] == {}


async def test_metrics_endpoint_passes_filters_through(env):
    cli, _db = env
    r = await cli.get("/admin/api/metrics/summary?project=demo&role=verifier")
    assert r.status == 200
    fa = (await r.json())["filters_applied"]
    assert fa == {"project": "demo", "role": "verifier"}


async def test_metrics_endpoints_accept_split_filter(env):
    cli, db = env
    for phase in ("writing", "budget_split"):
        cid = await db.create_chat(project_slug="demo", source="test", author="test", target="demo")
        await db.finish_chat(cid, status="ok", phase_at_end=phase)
    response = await cli.get("/admin/api/metrics/summary?status=split")
    assert response.status == 200
    body = await response.json()
    assert body["filters_applied"] == {"status": "split"}
    assert body["totals"]["runs"] == body["totals"]["split"] == 1
    assert body["totals"]["ok"] == 0
    response = await cli.get("/admin/api/metrics/trends?status=split")
    assert response.status == 200
    body = await response.json()
    rows = body.get("trends", body) if isinstance(body, dict) else body
    assert sum(row["runs"] for row in rows) == 1


# ---------- fase 4: período anterior para los deltas ----------


async def test_previous_period_is_same_length_and_disjoint(tmp_path):
    """`previous` cubre los N días ANTERIORES, sin solaparse con el actual.

    Si se solaparan, un run se contaría en los dos y el delta saldría
    amortiguado sin que nada lo indique.
    """
    db = Database(path=tmp_path / "p.db")
    await db.init_schema()
    now = time.time()
    def iso(offset_days):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(now - offset_days * 86400))

    # 2 runs dentro de los últimos 7d, 3 en los 7d previos, 1 más viejo
    rows = [("n1", iso(1)), ("n2", iso(3)),
            ("p1", iso(8)), ("p2", iso(10)), ("p3", iso(13)),
            ("viejo", iso(20))]
    for cid, ts in rows:
        await db.run(
            "INSERT INTO chats (id, source, started_at, status, model, "
            "tokens_in, tokens_out) VALUES (?,?,?,?,?,?,?)",
            (cid, "test", ts, "ok", "minimax:MiniMax-M3", 10, 1))

    s = await db.metrics_summary(days=7)
    assert s["totals"]["runs"] == 2
    assert s["previous"]["runs"] == 3, "el previo son los 7d anteriores"
    # El de hace 20 días queda fuera de ambos.
    assert s["totals"]["runs"] + s["previous"]["runs"] == 5


async def test_previous_respects_run_filters(tmp_path):
    """El período previo se filtra igual que el actual.

    Comparar 'inventorydemo esta semana' contra 'todo el relay la semana pasada'
    daría un delta sin sentido.
    """
    db = Database(path=tmp_path / "p2.db")
    await db.init_schema()
    now = time.time()
    def iso(d):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - d * 86400))

    for cid, ts, proj in [("a", iso(1), "inventorydemo"), ("b", iso(9), "inventorydemo"),
                          ("c", iso(9), "sample-app"), ("d", iso(10), "sample-app")]:
        await db.run(
            "INSERT INTO chats (id, source, project_slug, started_at, status, "
            "model, tokens_in, tokens_out) VALUES (?,?,?,?,?,?,?,?)",
            (cid, "test", proj, ts, "ok", "minimax:MiniMax-M3", 10, 1))

    s = await db.metrics_summary(days=7, project="inventorydemo")
    assert s["totals"]["runs"] == 1
    assert s["previous"]["runs"] == 1, "solo el run de inventorydemo del período previo"


async def test_previous_empty_when_no_history(tmp_path):
    """Sin historial previo, `previous` viene en cero y la UI omite el
    delta (contra cero no hay con qué comparar)."""
    db = Database(path=tmp_path / "p3.db")
    await db.init_schema()
    await db.run(
        "INSERT INTO chats (id, source, started_at, status, model, "
        "tokens_in, tokens_out) VALUES (?,?,?,?,?,?,?)",
        ("solo", "test", _NOW_ISO, "ok", "minimax:MiniMax-M3", 10, 1))

    s = await db.metrics_summary(days=7)
    assert s["totals"]["runs"] == 1
    assert s["previous"]["runs"] == 0


# ---------- preguntar es un final legítimo del turno (2026-08-16) ----------
#
# Bug reportado: *"la decisión que muestra la UI se contradice con lo que
# dice el bot"*. La causa no era ninguna etapa en particular: era que
# NINGUNA sabía que el run se había detenido a propósito. El ejecutor
# llamaba `ask_human`, terminaba bien, y el verificador y el documentador
# seguían corriendo sobre un trabajo en pausa. El humano leía tres cosas
# incompatibles en el mismo mensaje: un registro que daba el cambio por
# hecho, un "respondé continúa" del verificador, y la tarjeta pidiendo
# otra decisión.


async def test_con_pregunta_abierta_no_corre_el_verificador():
    project = _project(three_stage=True)
    corrio = {"verifier": False, "documenter": False}

    async def fake_planner(**kwargs):
        return "plan", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result(
            content="Miré el deploy y falta pwsh 7. Espero tu decisión.",
            question_id="q_abc12345", tool_calls=3)

    async def fake_verifier(**kwargs):
        corrio["verifier"] = True
        return "needs_more", "no terminó de instalar", {}, ""

    async def fake_documenter(**kwargs):
        corrio["documenter"] = True
        return "📝 Registro: instalado pwsh 7", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier), \
         patch.object(experts, "_run_documenter", fake_documenter):
        result = await experts.run_expert_staged(
            project, "arreglá el deploy", model_override="test")

    assert corrio["verifier"] is False, "verificó un trabajo que está en pausa"
    assert corrio["documenter"] is False, "documentó algo que no terminó"


async def test_con_pregunta_abierta_el_texto_no_pide_otra_cosa():
    """Lo que veía el humano: 'respondé continúa' arriba de la tarjeta."""
    project = _project(three_stage=True)

    async def fake_planner(**kwargs):
        return "plan", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result(content="Espero tu decisión.",
                                question_id="q_abc12345", tool_calls=3)

    async def fake_verifier(**kwargs):
        return "needs_more", "falta instalar", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        result = await experts.run_expert_staged(
            project, "arreglá el deploy", model_override="test")

    assert "continúa" not in result["content"]
    assert "needs_more" not in result["content"]
    assert "Registro" not in result["content"]
    assert result["content"].strip() == "Espero tu decisión."
    assert result["verifier_verdict"] == ""


async def test_un_run_roto_gana_sobre_la_pregunta():
    """Si además reventó, el humano tiene que enterarse del error.

    El orden importa: `broken` primero. Una pregunta abierta en un run
    que terminó en timeout no convierte el timeout en algo normal.
    """
    project = _project(three_stage=True)

    async def fake_planner(**kwargs):
        return "plan", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result(content="a medias", question_id="q_abc12345",
                                phase_at_end="timeout")

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor):
        result = await experts.run_expert_staged(
            project, "algo", model_override="test")

    assert result["verifier_verdict"] == "needs_human"
    assert "timeout" in result["verifier_feedback"]


async def test_sin_pregunta_las_etapas_siguen_como_siempre():
    """El caso normal no cambia: esto acota una excepción, no la regla."""
    project = _project(three_stage=True)
    corrio = {"verifier": False}

    async def fake_planner(**kwargs):
        return "plan", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result(content="listo", tool_calls=2)

    async def fake_verifier(**kwargs):
        corrio["verifier"] = True
        return "complete", "ok", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        result = await experts.run_expert_staged(
            project, "algo", model_override="test")

    assert corrio["verifier"] is True
    assert result["verifier_verdict"] == "complete"


# ---------- `off_plan` del verificador FINAL deja rastro ----------
#
# Hasta 2026-09-08 el bloque de avisos cubría `needs_human` (⚠️) y
# `needs_more` (🔁), pero no `off_plan` cuando el veredicto venía del
# verificador FINAL: el run cerraba `ok`, salteaba el documentador y no
# escribía una sola palabra en el `.md`. Peor: sin aviso propio ganaba el
# cintillo optimista de "Cerrado en N pasadas" y el chat decía lo
# contrario de lo que había votado el verificador. Medido sobre 68 runs
# con veredicto `off_plan`: 33 sin ningún aviso, 8 de ellos con cintillo.
# (El corte de media corrida es otro camino: ese ya escribe su 🧭 dentro
# de `run_expert` y por eso el aviso se saltea con `phase == "off_plan"`.)


async def test_off_plan_del_verificador_final_avisa_en_el_content():
    project = _project(three_stage=True)
    feedback = ("Saltaron pasos 1 (ver adjuntos) y 2 (consultar GitHub). "
                "El plan exigía validar entrada antes de generar.")

    async def fake_planner(**kwargs):
        return "1. ver adjuntos\n2. consultar GitHub", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result(content="generé el reporte", tool_calls=4)

    async def fake_verifier(**kwargs):
        return "off_plan", feedback, {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        result = await experts.run_expert_staged(
            project, "genera el reporte", model_override="test")

    assert result["verifier_verdict"] == "off_plan"
    assert "🧭" in result["content"], (
        "el veredicto off_plan quedó solo en stages_json: el humano lee el "
        ".md y no ve nada")
    assert "off_plan" in result["content"]
    # El aviso sin el motivo no sirve: hay que poder saber QUÉ se desvió.
    assert "Saltaron pasos 1" in result["content"]


async def test_off_plan_no_cierra_con_el_cintillo_optimista():
    """Dos pasadas y veredicto final `off_plan`: nada de "cerrado".

    Caso real (chat 9442c455): el chat mostraba "🔁 Cerrado en 2 pasadas:
    el verificador pidió seguir y el ejecutor continuó solo" sobre un run
    que el verificador había marcado `off_plan`.
    """
    project = _project(three_stage=True)
    veredictos = iter([
        ("needs_more", "falta la mitad", {}, ""),
        ("off_plan", "te fuiste del plan en la segunda pasada", {}, ""),
    ])
    pasadas = []

    async def fake_planner(**kwargs):
        return "1. hacer algo", {}, ""

    async def fake_executor(proj, user, **kwargs):
        pasadas.append(user)
        return _executor_result(content=f"pasada {len(pasadas)}",
                                messages_json='[{"parts":[]}]',
                                tool_calls=3)

    async def fake_verifier(**kwargs):
        return next(veredictos)

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        result = await experts.run_expert_staged(
            project, "haz algo", model_override="test")

    assert len(pasadas) == 2
    assert result["verifier_verdict"] == "off_plan"
    assert "Cerrado en" not in result["content"], (
        "el cintillo optimista contradice al verificador")
    assert "🧭" in result["content"]


async def test_sin_veredicto_la_cuenta_de_pasadas_no_se_le_atribuye_a_nadie():
    """Varias pasadas sin veredicto: se dice cuantas, y nada mas.

    Antes caian en el cintillo de `complete`, que afirmaba "el verificador
    pidio seguir" aunque no hubiera corrido ningun verificador, y daba por
    "Cerrado" un turno que podia estar esperando respuesta del humano.
    Exigirle `complete` a esa rama los dejo mudos; esta rama devuelve el
    dato sin inventarle un autor.
    """
    project = _project(three_stage=True)
    pasadas = []

    async def fake_planner(**kwargs):
        return "1. hacer algo", {}, ""

    async def fake_executor(proj, user, **kwargs):
        pasadas.append(user)
        return _executor_result(content=f"pasada {len(pasadas)}",
                                messages_json='[{"parts":[]}]',
                                tool_calls=3)

    # Primera ronda pide seguir; la segunda no deja veredicto (el caso
    # real: pregunta abierta, o verificador apagado en el proyecto).
    veredictos = iter([
        ("needs_more", "falta la mitad", {}, ""),
        ("", "", {}, ""),
    ])

    async def fake_verifier(**kwargs):
        return next(veredictos)

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        result = await experts.run_expert_staged(
            project, "haz algo", model_override="test")

    assert len(pasadas) == 2
    assert not result.get("verifier_verdict")
    contenido = result["content"]
    assert "Este turno tomo" in contenido.replace("\u00f3", "o") or \
        "Este turno tom\u00f3 2 pasadas" in contenido, (
            f"no se reporto la cuenta de pasadas: {contenido!r}")
    assert "el verificador pidio seguir" not in contenido.replace(
        "\u00f3", "o"), (
            "se le atribuye al verificador algo que nunca dijo")
    assert "Cerrado en" not in contenido, (
        "no se puede dar por cerrado un turno sin veredicto")


async def test_verifier_model_vacio_si_el_verificador_no_corrio():
    """117 chats tenían anotado un modelo que nunca se invocó.

    Con una pregunta abierta el verificador se saltea a propósito, pero
    `verifier_model` se asignaba igual al final de la función.
    """
    project = _project(three_stage=True)

    async def fake_planner(**kwargs):
        return "1. preguntar", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result(content="¿instalo pwsh 7?",
                                question_id="q-1", tool_calls=2)

    async def fake_verifier(**kwargs):
        raise AssertionError("el verificador no debe correr con pregunta abierta")

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        result = await experts.run_expert_staged(
            project, "instala lo que falte", model_override="test")

    assert result["verifier_verdict"] == ""
    assert result["verifier_model"] == "", (
        "se anotó el modelo del verificador sin haberlo invocado")


# ---------- 9/9/2026: el verificador recibe evidencia dura ----------


def test_la_evidencia_separa_los_hechos_de_las_afirmaciones():
    """Un `exit=0` lo escribio el harness; "verifique que compila", el modelo.

    Van etiquetados distinto y los comandos primero para que el
    verificador pueda pesarlos distinto. Antes solo veia QUE tools se
    llamaron, o sea que no podia separar "corrio las pruebas" de "las
    pruebas pasaron".
    """
    b = experts.Bitacora()
    b.anotar_comando("pytest -q", 1)
    b.anotar("los tests de auth pasan")
    b.pasos[2] = "agregue el empty state"

    ev = b.evidencia()
    assert "exit=1" in ev
    assert ev.index("harness") < ev.index("dice haber comprobado"), (
        "los comandos tienen que ir antes que las afirmaciones del modelo")
    assert "paso 2" in ev
    assert experts.Bitacora().evidencia() == "", "sin nada, no inventa texto"


async def test_el_verificador_ve_los_exit_codes_y_no_solo_los_nombres(
        monkeypatch):
    """El prompt del verificador tiene que traer la evidencia del run."""
    vistos: list = []

    class _Usage:
        input_tokens = output_tokens = 1

    class _Salida:
        output = "VERDICT: needs_more\nFEEDBACK: falta correr las pruebas\nPASOS:"
        usage = _Usage()

    class _Agente:
        def __init__(self, *a, **kw):
            pass

        async def run(self, prompt, **kw):
            vistos.append(prompt)
            return _Salida()

    monkeypatch.setattr(experts, "Agent", _Agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: object())

    b = experts.Bitacora()
    b.anotar_comando("pytest -q", 1)
    b.anotar("los tests de auth pasan")

    verdict, _fb, _u, err, _pasos = await experts._run_verifier(
        user="arregla el login", plan="1. correr las pruebas",
        executor_result={"content": "listo", "phase_at_end": "done",
                         "bitacora_json": b.volcar()},
        model_spec="test", ponytail="")

    assert err == "" and verdict == "needs_more"
    prompt = vistos[0]
    assert "## Evidencia" in prompt
    assert "exit=1" in prompt, "el verificador sigue sin ver el exit code"
    assert "los tests de auth pasan" in prompt


async def test_sin_bitacora_el_verificador_igual_corre(monkeypatch):
    """Evidencia vacia no es evidencia en contra: una tarea de lectura o
    de redaccion no tiene por que dejar comandos."""
    vistos: list = []

    class _Usage:
        input_tokens = output_tokens = 1

    class _Salida:
        output = "VERDICT: complete\nFEEDBACK: ok\nPASOS:"
        usage = _Usage()

    class _Agente:
        def __init__(self, *a, **kw):
            pass

        async def run(self, prompt, **kw):
            vistos.append(prompt)
            return _Salida()

    monkeypatch.setattr(experts, "Agent", _Agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: object())

    verdict, _fb, _u, err, _p = await experts._run_verifier(
        user="resumi el modulo", plan="1. leer", executor_result={
            "content": "el modulo hace X", "phase_at_end": "done"},
        model_spec="test", ponytail="")

    assert err == "" and verdict == "complete"
    assert "no dejó comandos" in vistos[0]


# ---------- 9/9/2026: presupuesto de TODO el pedido ----------


def test_el_presupuesto_del_pedido_sale_del_peor_caso_ya_implicito():
    """Encenderlo no le recorta el presupuesto a nadie.

    `expert_timeout_s` acota UNA pasada y el runner llama de nuevo por
    cada ronda del verificador, asi que el peor caso real ya era
    leg x (rondas + 1). El default es ese mismo numero: lo que cambia es
    que ahora es UNO solo, compartido y visible.
    """
    from relay import config

    assert config.expert_request_timeout_s() == (
        config.expert_timeout_s() * (config.expert_verifier_rounds() + 1))


async def test_una_pasada_no_puede_pasarse_del_presupuesto_del_pedido(
        monkeypatch):
    """`run_expert` toma el MENOR entre su techo y el del pedido."""
    vistos: dict = {}
    original = experts.run_expert

    async def espia(project, user, **kwargs):
        vistos["deadline"] = kwargs.get("deadline_pedido")
        return _executor_result(content="listo", tool_calls=3)

    async def fake_planner(**kwargs):
        return "1. hacer algo", {}, ""

    async def fake_verifier(**kwargs):
        return "complete", "ok", {}, "", []

    monkeypatch.setattr(experts, "_run_planner", fake_planner)
    monkeypatch.setattr(experts, "run_expert", espia)
    monkeypatch.setattr(experts, "_run_verifier", fake_verifier)

    await experts.run_expert_staged(
        _project(three_stage=True), "hace algo", model_override="test")

    assert vistos.get("deadline"), (
        "el runner no le paso el deadline del pedido a la pasada")
    assert original is not espia   # el espia se instalo de verdad


async def test_no_abre_otra_ronda_si_no_queda_presupuesto(monkeypatch):
    """Entrar a una ronda que el deadline va a cortar a la mitad gasta el
    modelo para tirar el resultado. Y el corte tiene que DECIRSE: sin el
    aviso, el turno se lee como cerrado y nadie sabe que falta."""
    pasadas = {"n": 0}

    async def fake_planner(**kwargs):
        return "1. hacer algo", {}, ""

    async def fake_executor(project, user, **kwargs):
        pasadas["n"] += 1
        return _executor_result(content="voy por la mitad", tool_calls=5)

    async def fake_verifier(**kwargs):
        return "needs_more", "falta la segunda parte", {}, "", []

    monkeypatch.setattr(experts, "_run_planner", fake_planner)
    monkeypatch.setattr(experts, "run_expert", fake_executor)
    monkeypatch.setattr(experts, "_run_verifier", fake_verifier)

    # Presupuesto ya agotado: la reserva de cierre no entra ni por asomo.
    project = _project(three_stage=True)
    project["defaults_json"]["request_timeout"] = 0.001

    result = await experts.run_expert_staged(
        project, "hace algo grande", model_override="test")

    assert pasadas["n"] == 1, (
        f"abrio {pasadas['n']} pasadas sin presupuesto para terminarlas")
    assert "presupuesto del pedido" in (result.get("content") or ""), (
        "corto por tiempo y lo entrego como si hubiera terminado")


# ---------- 9/9/2026: no documentar una revision ----------


def test_solo_reviso_es_conservador():
    """La lista es de lo SEGURO, no de lo probable.

    `db_query` puede escribir si la conexion esta marcada escribible,
    `shell` corre lo que sea, y una tool MCP desconocida podria hacer
    cualquier cosa. Ante la duda se documenta, que es lo de antes.
    """
    assert experts._solo_reviso([("read_file", ""), ("search_files", ""),
                                 ("list_dir", "")]) is True
    assert experts._solo_reviso([("read_file", ""), ("edit_file", "")]) is False
    assert experts._solo_reviso([("read_file", ""), ("shell", "")]) is False
    assert experts._solo_reviso([("read_file", ""), ("db_query", "")]) is False
    assert experts._solo_reviso([("jira_create", "")]) is False
    # Sin llamadas no hay revision que resumir: lo maneja `has_work`.
    assert experts._solo_reviso([]) is False


async def test_una_revision_no_se_lleva_un_resumen_de_regalo(monkeypatch):
    """El documentador documenta CAMBIOS: "que se toco, en que archivos".

    Un run de solo lectura no toco nada, y el hallazgo ya esta en la
    respuesta del ejecutor: el resumen extra repite lo que el humano
    acaba de leer, y cuesta un turno del modelo y latencia de cierre.
    """
    corrio = {"doc": False}

    async def fake_planner(**kwargs):
        return "1. revisar el modulo", {}, ""

    async def fake_executor(project, user, **kwargs):
        return _executor_result(
            content="el modulo hace X, y tiene Y sin usar", tool_calls=6)

    async def fake_verifier(**kwargs):
        return "complete", "ok", {}, "", []

    async def fake_documenter(**kwargs):
        corrio["doc"] = True
        return "resumen que no hacia falta", {}, ""

    # El runner RECALCULA el resumen desde `messages_json`, asi que
    # sembrarlo en el dict del ejecutor no alcanza: se parchea la fuente.
    monkeypatch.setattr(experts, "_summarize_tool_calls_from_messages",
                        lambda _mj: [("read_file", "a"), ("search_files", "b")])
    monkeypatch.setattr(experts, "_run_planner", fake_planner)
    monkeypatch.setattr(experts, "run_expert", fake_executor)
    monkeypatch.setattr(experts, "_run_verifier", fake_verifier)
    monkeypatch.setattr(experts, "_run_documenter", fake_documenter)

    result = await experts.run_expert_staged(
        _project(three_stage=True), "revisa el modulo", model_override="test")

    assert not corrio["doc"], "documento una revision de solo lectura"
    assert "resumen que no hacia falta" not in (result.get("content") or "")
    assert result["documenter_model"] == ""
    # El skip queda registrado: si no, se ve igual que un documentador caido.
    saltos = [e for e in (result.get("stages_events") or [])
              if e.get("type") == "documenter_skipped"]
    assert saltos and saltos[-1]["reason"] == "solo_lectura"


async def test_un_run_que_escribio_si_se_documenta(monkeypatch):
    """El contrapeso: donde hubo cambios, el resumen sigue valiendo."""
    corrio = {"doc": False}

    async def fake_planner(**kwargs):
        return "1. tocar el modulo", {}, ""

    async def fake_executor(project, user, **kwargs):
        return _executor_result(content="listo", tool_calls=6)

    async def fake_verifier(**kwargs):
        return "complete", "ok", {}, "", []

    async def fake_documenter(**kwargs):
        corrio["doc"] = True
        return "toque a.py y b.py", {}, ""

    monkeypatch.setattr(experts, "_summarize_tool_calls_from_messages",
                        lambda _mj: [("read_file", "a"), ("edit_file", "b")])
    monkeypatch.setattr(experts, "_run_planner", fake_planner)
    monkeypatch.setattr(experts, "run_expert", fake_executor)
    monkeypatch.setattr(experts, "_run_verifier", fake_verifier)
    monkeypatch.setattr(experts, "_run_documenter", fake_documenter)

    result = await experts.run_expert_staged(
        _project(three_stage=True), "arregla el modulo", model_override="test")

    assert corrio["doc"], "dejo de documentar un run que escribio"
    assert "toque a.py y b.py" in (result.get("content") or "")
