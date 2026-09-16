"""Convivencia entre modelos: etapas auxiliares en otro proveedor.

Desde el 2026-08-14 el ejecutor corre en MiniMax y las tres etapas
auxiliares (planificador, verificador, documentador) en los endpoints
NIM de NVIDIA. Los contratos de texto entre etapas se habían afinado
contra un solo modelo, y el contexto del hilo no cruzaba de una etapa a
otra. Estos tests cubren los arreglos del 2026-08-15 — ver
docs/MULTIMODELO_FIXES.md:

  - `_clean_stage_output`: saca el andamiaje de los modelos que razonan
    en voz alta (bloques <think>, fences) antes de parsear.
  - `_parse_verifier`: el veredicto se ancla a la línea `VERDICT:`, así
    una mención en prosa no lo invierte.
  - `_plan_signal`: `TRIVIAL:` / `DEMASIADO_GRANDE:` se detectan por
    línea y no por `startswith` sobre el texto crudo.
  - `_history_recap` + `_run_planner(history_recap=...)`: el
    planificador deja de planificar a ciegas en los follow-ups.
  - `stage_errors`: una etapa caída deja rastro auditable en vez de
    degradar en silencio.
  - `_merge_doc_into_history`: el registro del documentador entra al
    historial, no solo al content que ve el humano.
  - `_strip_foreign_thinking`: el razonamiento de un modelo no se le
    replaya a otro.
  - `memory.build_compacted_history(previous_json=...)`: compactar
    conserva el último turno.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from pydantic_ai.messages import (
    ModelMessagesTypeAdapter, ModelRequest, ModelResponse, TextPart,
    ThinkingPart, ToolCallPart, ToolReturnPart, UserPromptPart,
)

from relay import experts, memory, server


# ---------- helpers ----------


def _project(**defaults):
    return {
        "slug": "demo", "repo_path": "C:/x/Demo",
        "system_prompt": "experto demo",
        "mcp_servers": [], "native_tools": [],
        "defaults_json": {"three_stage": True, **defaults},
    }


def _executor_result(**over):
    base = {
        "content": "ok", "model": "test",
        "tokens_in": 1, "tokens_out": 1,
        "tool_calls": 1, "duration_ms": 1,
        "messages_json": "[]", "phase_at_end": "writing",
        "last_tool": None, "legs": 1, "steers": 0,
        "steer_texts": [], "progress_events": [],
    }
    base.update(over)
    return base


def _history(turns):
    """[(role, texto)] → messages_json serializado."""
    messages = []
    for role, text in turns:
        if role == "user":
            messages.append(ModelRequest(parts=[UserPromptPart(content=text)]))
        else:
            messages.append(ModelResponse(parts=[TextPart(content=text)]))
    return ModelMessagesTypeAdapter.dump_json(messages).decode("utf-8")


class _FakeRun:
    """Sustituto de `Agent` que captura el prompt y devuelve texto fijo."""

    def __init__(self, output, sink):
        self._output = output
        self._sink = sink

    def __call__(self, *args, **kwargs):
        self._sink["instructions"] = kwargs.get("instructions", "")
        return self

    async def run(self, prompt, **kwargs):
        self._sink["prompt"] = prompt
        return type("R", (), {"output": self._output, "usage": lambda s=None: None})()


# ---------- 1. saneamiento de la salida de las etapas ----------


def test_clean_stage_output_quita_bloque_think():
    raw = "<think>me pregunto si…</think>\nVERDICT: complete"
    assert experts._clean_stage_output(raw) == "VERDICT: complete"


def test_clean_stage_output_quita_think_sin_cerrar():
    """Apertura huérfana: sobrevive lo que viene DESPUÉS de la etiqueta.

    No se puede distinguir razonamiento de respuesta cuando el cierre no
    llegó, así que se conserva la cola: perder la respuesta sería peor
    que colar un poco de razonamiento.
    """
    raw = "<think>razono y razono\nVERDICT: complete"
    out = experts._clean_stage_output(raw)
    assert out.startswith("razono y razono")
    assert "<think>" not in out


def test_clean_stage_output_conserva_lo_posterior_a_think_abierto():
    raw = "<thinking>ruido\nVERDICT: needs_more"
    assert "VERDICT: needs_more" in experts._clean_stage_output(raw)


def test_clean_stage_output_quita_fence_envolvente():
    raw = "```markdown\n1. leer main.py\n2. editar\n```"
    assert experts._clean_stage_output(raw) == "1. leer main.py\n2. editar"


def test_clean_stage_output_no_toca_texto_limpio():
    """Idempotente: lo que ya venía bien queda igual (caso MiniMax)."""
    raw = "VERDICT: complete\nFEEDBACK: listo"
    assert experts._clean_stage_output(raw) == raw
    assert experts._clean_stage_output(raw) == experts._clean_stage_output(raw)


def test_clean_stage_output_no_rompe_un_fence_interno():
    """Un plan con un bloque de código adentro no se desarma."""
    raw = "1. correr:\n\n```bash\npytest -q\n```\n\n2. revisar"
    assert experts._clean_stage_output(raw) == raw


# ---------- 2. parser del verificador ----------


def test_parse_verifier_no_se_invierte_con_prosa_previa():
    """El bug del multi-modelo: el modelo razona antes de contestar.

    `_VERDICT_RE.search` agarraba la primera palabra que apareciera, así
    que este texto daba `needs_human` cuando el veredicto es `complete`.
    """
    text = ("Analizando: no es needs_human porque no hay que decidir nada, "
            "y tampoco needs_more.\n"
            "VERDICT: complete\nFEEDBACK: el plan se cumplió")
    v, f, _pasos = experts._parse_verifier(text)
    assert v == "complete"
    assert f == "el plan se cumplió"
    assert _pasos == [], "sin línea PASOS: la lista viene vacía"


def test_parse_verifier_con_bloque_think():
    text = ("<think>Veamos… podría ser needs_more</think>\n"
            "VERDICT: complete\nFEEDBACK: ok")
    assert experts._parse_verifier(text)[0] == "complete"


def test_parse_verifier_linea_sola_sin_etiqueta():
    v, _, _pasos = experts._parse_verifier("needs_more\nfalta correr los tests")
    assert v == "needs_more"
    assert _pasos == []


def test_parse_verifier_markdown_alrededor_de_la_etiqueta():
    v, _, _ = experts._parse_verifier("**VERDICT:** `needs_human`\nFEEDBACK: x")
    assert v == "needs_human"


def test_parse_verifier_fallback_suelto_sigue_andando():
    """Una mención suelta todavía puede pedir trabajo, nunca aprobarlo."""
    v, _, _ = experts._parse_verifier("me parece que esto es needs_more todavía")
    assert v == "needs_more"


def test_parse_verifier_feedback_siempre_acotado():
    v, f, _pasos = experts._parse_verifier("x" * 900)
    assert v == "needs_human" and len(f) <= 200
    assert _pasos == []


# ---------- 3. señales del planificador ----------


def test_plan_signal_con_preambulo():
    """El modelo saluda antes de la señal: antes esto anulaba el corte."""
    plan = "Claro, acá va:\nDEMASIADO_GRANDE:\n1. una\n2. otra"
    assert experts._plan_signal(plan) == "too_large"


def test_plan_signal_con_markdown():
    assert experts._plan_signal("**TRIVIAL:** la respuesta es 42") == "trivial"


def test_plan_signal_ignora_mencion_tardia():
    """Un plan que MENCIONA la palabra en el paso 7 no es una señal."""
    plan = "\n".join([f"{i}. paso {i}" for i in range(1, 7)]
                     + ["7. avisar DEMASIADO_GRANDE: si no entra"])
    assert experts._plan_signal(plan) == ""


def test_plan_signal_vacio_sin_senal():
    assert experts._plan_signal("1. leer\n2. escribir") == ""


# Salidas REALES del planificador, copiadas de `chats.stages_json`. Que
# sean literales importa: inventar la basura a mano habría dado casos más
# prolijos que los que manda el proveedor.
_PLANES_BASURA = [
    'cbm_query({"tool": "search_graph", "name_pattern": "docker-compose"})',
    'puan\ncbm_query({"tool": "search_graph", "name_pattern": "Cancelled"})\n```',
    'sequentialthinking({"thoughts": ["El usuario quiere ejecutar un night run"]})',
    ('Voy a explorar el repositorio para entender el estado actual antes de '
     'crear el{"tool": "cbm_query", "args": {"query": "OracleSvtRepository"}}'),
    'ponytail\nVoy a revisar el estado del repo, hacer commit, push.',
    'No puedo ver la imagen adjunta directamente desde esta interfaz.',
    'Thought 1: The user wants a plan to fix Oracle connection timeout.',
]

_PLANES_BUENOS = [
    "1. leer el repo\n2. levantar la app\n3. capturar",
    "**1.** leer el repo\n**2.** escribir",
    "Paso 1: inspeccionar\nPaso 2: corregir",
    "- 1) una cosa\n- 2) otra cosa",
    "Voy a hacer esto:\n\n1. primero\n2. después",
]


def test_plan_utilizable_rechaza_la_basura_real():
    """El guard que evita cortar runs buenos por `off_plan` contra ruido.

    Medido el 19/8/2026: 101 de 156 planes no eran planes, y el
    verificador los usaba igual como contrato. Ver `_plan_utilizable`.
    """
    for basura in _PLANES_BASURA:
        assert not experts._plan_utilizable(basura), basura[:60]


def test_plan_utilizable_acepta_los_planes_de_verdad():
    """Rechazar de más sería peor: el ejecutor se quedaría sin guía."""
    for bueno in _PLANES_BUENOS:
        assert experts._plan_utilizable(bueno), bueno[:60]


def test_format_decomposition_saca_la_senal_con_preambulo():
    out = experts._format_decomposition(
        "Claro:\nDEMASIADO_GRANDE:\n1. una\n2. otra")
    assert "DEMASIADO_GRANDE" not in out
    assert "1. una" in out and "2. otra" in out


# ---------- 4. memoria del hilo para el planificador ----------


def test_history_recap_arma_el_resumen():
    raw = _history([
        ("user", "arreglá el login"),
        ("assistant", "listo, toqué auth.py"),
        ("user", "ahora los tests"),
        ("assistant", "corrí pytest, 3 fallan"),
    ])
    recap = experts._history_recap(raw)
    assert "arreglá el login" in recap
    assert "corrí pytest, 3 fallan" in recap
    assert recap.index("arreglá el login") < recap.index("corrí pytest")


def test_history_recap_respeta_el_presupuesto():
    raw = _history([("user", "u" * 5000), ("assistant", "a" * 5000)])
    recap = experts._history_recap(raw, max_chars=500)
    assert 0 < len(recap) <= 1500   # un mensaje entero, recortado a _RECAP_PART_CAP


def test_history_recap_se_queda_con_los_ultimos_turnos():
    raw = _history([(r, f"turno {i}")
                    for i, r in enumerate(["user", "assistant"] * 6)])
    recap = experts._history_recap(raw, max_turns=2)
    assert "turno 11" in recap
    assert "turno 0" not in recap


@pytest.mark.parametrize("bad", ["", "no soy json", "{}", None])
def test_history_recap_best_effort(bad):
    assert experts._history_recap(bad) == ""


async def test_run_planner_recibe_el_recap_en_el_prompt():
    sink = {}
    fake = _FakeRun("1. seguir", sink)
    with patch.object(experts, "Agent", fake), \
         patch.object(experts, "build_model", lambda spec: "m"):
        plan, _usage, err = await experts._run_planner(
            user="continúa", project=_project(), model_spec="test",
            ponytail="", is_followup=True,
            history_recap="usuario: arreglá el login\n\nexperto: toqué auth.py",
        )
    assert err == "" and plan == "1. seguir"
    assert "arreglá el login" in sink["prompt"]
    assert "NO es el pedido" in sink["prompt"]
    # El pedido real sigue siendo el pedido.
    assert sink["prompt"].rstrip().endswith("continúa")


async def test_planner_no_pide_el_razonador_si_no_lo_tiene():
    """Pedir una tool ausente es lo que produce el "plan" basura.

    Medido el 19/8/2026: con `toolsets=[]` y la regla puesta igual, el
    modelo intenta la llamada y el proveedor la serializa como TEXTO. La
    salida cruda de MiniMax que lo delató:

        Voy a usar pensamiento secuencial …]<]minimax[>[<tool_call>{"thou…

    Misma firma que los 101 planes basura de producción. La condición la
    evalúa Python ahora, no el modelo sobre sí mismo.
    """
    sink = {}
    with patch.object(experts, "Agent", _FakeRun("1. hacer algo", sink)), \
         patch.object(experts, "build_model", lambda spec: "m"):
        await experts._run_planner(
            user="documenta la app", project=_project(), model_spec="test",
            ponytail="", toolsets=[])
    assert "sequentialthinking" not in sink["instructions"]


async def test_planner_pide_el_razonador_cuando_esta_adjunto():
    sink = {}
    with patch.object(experts, "Agent", _FakeRun("1. hacer algo", sink)), \
         patch.object(experts, "build_model", lambda spec: "m"):
        await experts._run_planner(
            user="documenta la app", project=_project(), model_spec="test",
            ponytail="", toolsets=[object()])
    assert "sequentialthinking" in sink["instructions"]
    assert "UNA pregunta" in sink["instructions"]   # el cuerpo de la regla


async def test_staged_pasa_el_recap_al_planificador():
    """El cableado: `run_expert_staged` deriva el recap del historial."""
    seen = {}

    async def fake_planner(**kwargs):
        seen["recap"] = kwargs.get("history_recap", "")
        seen["is_followup"] = kwargs.get("is_followup")
        return "1. seguir", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result()

    async def fake_verifier(**kwargs):
        return "complete", "", {}, ""

    async def fake_documenter(**kwargs):
        return "", {}, ""

    raw = _history([("user", "arreglá el login"),
                    ("assistant", "listo, toqué auth.py")])
    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier), \
         patch.object(experts, "_run_documenter", fake_documenter):
        await experts.run_expert_staged(
            _project(), "continúa", model_override="test",
            message_history_json=raw)
    assert seen["is_followup"] is True
    assert "arreglá el login" in seen["recap"]


async def test_staged_sin_historial_no_manda_recap():
    seen = {}

    async def fake_planner(**kwargs):
        seen["recap"] = kwargs.get("history_recap", "")
        return "1. hacer", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result()

    async def fake_verifier(**kwargs):
        return "complete", "", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        await experts.run_expert_staged(
            _project(), "primer pedido", model_override="test")
    assert seen["recap"] == ""


# ---------- 5. etapas caídas: auditables, no silenciosas ----------


async def test_planner_caido_deja_stage_error():
    async def fake_planner(**kwargs):
        return "", {}, "TimeoutError: planificador"

    async def fake_executor(proj, user, **kwargs):
        return _executor_result()

    async def fake_verifier(**kwargs):
        return "complete", "", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier):
        result = await experts.run_expert_staged(
            _project(), "hola", model_override="test")
    assert result["stage_errors"]["planner"].startswith("TimeoutError")


async def test_run_planner_reporta_el_error_y_avisa_por_progress():
    eventos = []

    async def on_progress(**kw):
        eventos.append(kw)

    def broken(spec):
        raise RuntimeError("429 del free tier")

    with patch.object(experts, "build_model", broken):
        plan, usage, err = await experts._run_planner(
            user="x", project=_project(), model_spec="lo-que-sea",
            ponytail="", on_progress=on_progress)
    assert plan == "" and usage == {}
    assert "RuntimeError" in err
    assert any("sin plan" in (e.get("message") or "") for e in eventos)


def test_stages_json_persiste_los_errores():
    raw = server._stages_json({
        "three_stage": True, "plan": "", "planner_model": "nvidia:x",
        "verifier_verdict": "complete", "verifier_feedback": "",
        "verifier_model": "nvidia:x", "documenter_model": "", "model": "mini",
        "stage_usage": {}, "stage_errors": {"planner": "TimeoutError: x"},
    })
    assert json.loads(raw)["planner_error"] == "TimeoutError: x"


def test_stages_json_sin_errores_no_escribe_las_claves():
    """Ausente = "no falló". Igual que los tokens: no se inventa un cero."""
    raw = server._stages_json({
        "three_stage": True, "plan": "p", "planner_model": "x",
        "verifier_verdict": "complete", "verifier_feedback": "",
        "verifier_model": "x", "documenter_model": "", "model": "mini",
        "stage_usage": {}, "stage_errors": {},
    })
    assert "planner_error" not in json.loads(raw)


# ---------- 6. verificador: opt-out y needs_more visible ----------


async def test_verificador_opt_out_por_proyecto():
    llamado = {}

    async def fake_planner(**kwargs):
        return "1. hacer", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result()

    async def fake_verifier(**kwargs):
        llamado["si"] = True
        return "complete", "", {}, ""

    async def fake_documenter(**kwargs):
        return "", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier), \
         patch.object(experts, "_run_documenter", fake_documenter):
        result = await experts.run_expert_staged(
            _project(verifier=False), "hola", model_override="test")
    assert "si" not in llamado
    # Ni `complete` ni `needs_human`: nadie verificó, y eso no es aprobar.
    assert result["verifier_verdict"] == ""
    assert "⚠️" not in result["content"]


async def test_needs_more_se_ve_en_el_content():
    async def fake_planner(**kwargs):
        return "1. hacer", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result(content="hice la mitad")

    async def fake_verifier(**kwargs):
        return "needs_more", "falta correr los tests", {}, ""

    async def fake_documenter(**kwargs):
        return "", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier), \
         patch.object(experts, "_run_documenter", fake_documenter):
        result = await experts.run_expert_staged(
            _project(), "hola", model_override="test")
    assert result["content"].startswith("hice la mitad")
    # El feedback del verificador tiene que llegar al humano: es lo que le
    # dice QUÉ falta. Eso no cambió.
    assert "falta correr los tests" in result["content"]
    # Lo que sí cambió (2026-08-17): el aviso ya no aparece a la primera.
    # El runner reintenta solo y este texto es el último recurso, así que
    # ahora tiene que decir cuántas veces intentó antes de molestar. Con un
    # verificador que siempre contesta needs_more, son 1 + verifier_rounds.
    assert "3 pasada" in result["content"]
    assert "**continúa**" in result["content"]
    assert [r["verdict"] for r in result["verifier_rounds"]] == [
        "needs_more", "needs_more", "needs_more"]


# ---------- 6b. el turno que no ejecutó nada ----------
#
# El síntoma medido en sample-app el 17/8: 19 de 43 runs del día cerraron con
# CERO tool calls, prolijos, y el verificador dejó pasar 3 como
# `complete`. "Todo queda como bien pero no hace nada."


async def _staged_contando(executor, verdict="complete", **proj_kw):
    """Corre el staged runner con un ejecutor de mentira. → (result, llamadas)."""
    llamadas: list[str] = []

    async def fake_planner(**kwargs):
        return "1. hacer algo real", {}, ""

    async def fake_executor(proj, user, **kwargs):
        llamadas.append(user)
        return executor(len(llamadas))

    async def fake_verifier(**kwargs):
        return verdict, "", {}, ""

    async def fake_documenter(**kwargs):
        return "", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier), \
         patch.object(experts, "_run_documenter", fake_documenter):
        result = await experts.run_expert_staged(
            _project(**proj_kw), "hacé el trabajo", model_override="test")
    return result, llamadas


async def test_turno_vacio_se_reintenta_aunque_el_verificador_diga_complete():
    """`tool_calls == 0` es un dato del runner, no una opinión del LLM."""
    def exec_(n):
        # Primera pasada vacía; en la segunda sí trabaja.
        return _executor_result(tool_calls=0 if n == 1 else 3,
                                content="hecho")

    result, llamadas = await _staged_contando(exec_, verdict="complete")
    assert len(llamadas) == 2, "no reintentó un turno que no ejecutó nada"
    assert "⚠️" not in result["content"], "la segunda pasada sí trabajó"


async def test_el_reintento_le_dice_que_llame_la_tool_no_que_la_escriba():
    """El modo de fallo real: escribió ```PS> docker --version``` como texto."""
    def exec_(n):
        return _executor_result(tool_calls=0 if n == 1 else 1)

    _, llamadas = await _staged_contando(exec_, verdict="complete")
    nudge = llamadas[1]
    assert "NINGUNA herramienta" in nudge
    assert "`shell`" in nudge
    assert "ask_human" in nudge, "tiene que quedar la salida legítima"


async def test_turno_vacio_persistente_avisa_arriba_y_no_cicla():
    """Se fuerza UNA pasada. Si tampoco trabaja, se dice; no se insiste."""
    def exec_(n):
        return _executor_result(tool_calls=0, content="acá va mi plan")

    result, llamadas = await _staged_contando(exec_, verdict="complete")
    assert len(llamadas) == 2, "forzó más de una pasada por turno vacío"
    # El aviso va PRIMERO: abajo es donde no se lee.
    assert result["content"].startswith("⚠️")
    assert "no ejecutó ninguna herramienta" in result["content"]
    assert "acá va mi plan" in result["content"]
    assert result.get("sin_trabajo") is True


async def test_off_plan_no_salva_a_un_turno_vacio():
    """`off_plan` cortaba el lazo; un turno sin trabajo se reintenta igual."""
    def exec_(n):
        return _executor_result(tool_calls=0 if n == 1 else 2)

    _, llamadas = await _staged_contando(exec_, verdict="off_plan")
    assert len(llamadas) == 2


async def test_pregunta_abierta_no_cuenta_como_turno_vacio():
    """`ask_human` es un final legítimo: el trabajo está en pausa a propósito."""
    def exec_(n):
        return _executor_result(tool_calls=0, question_id="q_123")

    result, llamadas = await _staged_contando(exec_, verdict="complete")
    assert len(llamadas) == 1, "reintentó un turno que dejó una pregunta"
    assert "⚠️" not in result["content"]


async def test_turno_con_trabajo_no_dispara_reintento():
    def exec_(n):
        return _executor_result(tool_calls=1)

    result, llamadas = await _staged_contando(exec_, verdict="complete")
    assert len(llamadas) == 1
    assert "sin_trabajo" not in result


# ---------- 7. el registro del documentador entra al historial ----------


def test_merge_doc_into_history_anexa_al_ultimo_texto():
    raw = _history([("user", "hacé X"), ("assistant", "hecho")])
    out = _merge = experts._merge_doc_into_history(raw, "**Pendiente:** correr tests")
    msgs = ModelMessagesTypeAdapter.validate_json(out)
    assert len(msgs) == 2                      # no agrega mensajes
    texto = msgs[-1].parts[-1].content
    assert texto.startswith("hecho")
    assert "correr tests" in texto


def test_merge_doc_sobrevive_a_slim_history():
    """La prueba que importa: el registro cruza al turno siguiente.

    `_slim_history` deja un solo response por turno viejo, así que un
    mensaje aparte se habría llevado puesta la respuesta del ejecutor.
    """
    raw = _history([("user", "hacé X"), ("assistant", "hecho")])
    merged = experts._merge_doc_into_history(raw, "**Pendiente:** correr tests")
    msgs = list(ModelMessagesTypeAdapter.validate_json(merged))
    msgs.append(ModelRequest(parts=[UserPromptPart(content="continuá")]))
    slim = experts._slim_history(msgs)
    textos = [p.content for m in slim if isinstance(m, ModelResponse)
              for p in m.parts if isinstance(p, TextPart)]
    assert any("correr tests" in t for t in textos)
    assert any("hecho" in t for t in textos)


def test_merge_doc_salta_el_response_sin_texto():
    """Un response que solo tiene tool calls no es el cierre del turno."""
    messages = [
        ModelRequest(parts=[UserPromptPart(content="hacé X")]),
        ModelResponse(parts=[TextPart(content="voy")]),
        ModelResponse(parts=[ToolCallPart(
            tool_name="read_file", args="{}", tool_call_id="c1")]),
    ]
    raw = ModelMessagesTypeAdapter.dump_json(messages).decode("utf-8")
    out = experts._merge_doc_into_history(raw, "registro")
    msgs = ModelMessagesTypeAdapter.validate_json(out)
    assert "registro" in msgs[1].parts[-1].content


def test_merge_doc_marca_el_registro_como_no_imitable():
    """Sin la marca el modelo lee el registro como prosa suya y lo copia.

    Es el bug de sample-app del 17/8: el bloque del documentador crecía de a
    uno por turno en la misma conversación hasta que la respuesta eran
    cinco registros y ninguna respuesta.
    """
    raw = _history([("user", "hacé X"), ("assistant", "hecho")])
    out = experts._merge_doc_into_history(raw, "**Pendiente:** correr tests")
    texto = ModelMessagesTypeAdapter.validate_json(out)[-1].parts[-1].content
    assert experts._DOC_MARCA in texto
    assert texto.index(experts._DOC_MARCA) < texto.index("correr tests")


def test_merge_doc_reemplaza_el_registro_previo_en_vez_de_apilar():
    """Dos registros son dos ejemplos del formato a imitar, no más contexto."""
    raw = _history([("user", "hacé X"), ("assistant", "hecho")])
    una = experts._merge_doc_into_history(raw, "**Pendiente:** correr tests")
    dos = experts._merge_doc_into_history(una, "**Pendiente:** subir el PR")
    texto = ModelMessagesTypeAdapter.validate_json(dos)[-1].parts[-1].content

    assert texto.count(experts._DOC_MARCA) == 1, "se apilaron dos registros"
    assert "subir el PR" in texto          # queda el nuevo
    assert "correr tests" not in texto     # se fue el viejo
    assert texto.startswith("hecho")       # la respuesta real sobrevive


def test_merge_doc_es_idempotente():
    raw = _history([("user", "hacé X"), ("assistant", "hecho")])
    una = experts._merge_doc_into_history(raw, "**Pendiente:** correr tests")
    otra = experts._merge_doc_into_history(una, "**Pendiente:** correr tests")
    assert una == otra


@pytest.mark.parametrize("doc,raw", [("", "[]"), ("x", ""), ("x", "roto{")])
def test_merge_doc_best_effort(doc, raw):
    assert experts._merge_doc_into_history(raw, doc) == raw


async def test_staged_mete_el_doc_en_el_historial():
    # El historial tiene que traer tool calls QUE ESCRIBAN: el
    # documentador se omite cuando el run no ejecutó nada (`has_work`) y,
    # desde 6f90d73, también cuando solo leyó. El resumen de tools se
    # deriva del messages_json, no del dict del ejecutor.
    #
    # Hilo propio y no `_thread_with_tools()`: ese lo comparten los tests
    # de compactación, que sí quieren un turno de solo lectura.
    raw = ModelMessagesTypeAdapter.dump_json([
        ModelRequest(parts=[UserPromptPart(content="arreglá config.py")]),
        ModelResponse(parts=[ToolCallPart(
            tool_name="edit_file", args='{"path":"config.py"}',
            tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(
            tool_name="edit_file", content="ok", tool_call_id="c1")]),
        ModelResponse(parts=[TextPart(content="listo")]),
    ]).decode("utf-8")

    async def fake_planner(**kwargs):
        return "1. hacer", {}, ""

    async def fake_executor(proj, user, **kwargs):
        return _executor_result(content="hecho", messages_json=raw)

    async def fake_verifier(**kwargs):
        return "complete", "", {}, ""

    async def fake_documenter(**kwargs):
        return "**Pendiente:** correr tests", {}, ""

    with patch.object(experts, "_run_planner", fake_planner), \
         patch.object(experts, "run_expert", fake_executor), \
         patch.object(experts, "_run_verifier", fake_verifier), \
         patch.object(experts, "_run_documenter", fake_documenter):
        result = await experts.run_expert_staged(
            _project(), "hacé X", model_override="test")
    assert "correr tests" in result["content"]          # lo ve el humano
    assert "correr tests" in result["messages_json"]    # …y el modelo


# ---------- 8. thinking ajeno entre modelos ----------


def _resp(model_name, *, thinking=True):
    parts = [TextPart(content="respondo")]
    if thinking:
        parts.insert(0, ThinkingPart(content="razono largo y tendido"))
    return ModelResponse(parts=parts, model_name=model_name)


def test_strip_foreign_thinking_mismo_modelo_no_toca():
    msgs = [ModelRequest(parts=[UserPromptPart(content="x")]),
            _resp("MiniMax-M3")]
    assert experts._strip_foreign_thinking(msgs, "minimax:MiniMax-M3") == 0
    assert any(isinstance(p, ThinkingPart) for p in msgs[-1].parts)


def test_strip_foreign_thinking_otro_modelo_saca():
    msgs = [ModelRequest(parts=[UserPromptPart(content="x")]),
            _resp("MiniMax-M3")]
    n = experts._strip_foreign_thinking(msgs, "nvidia:nvidia/nemotron-3-ultra")
    assert n == 1
    assert not any(isinstance(p, ThinkingPart) for p in msgs[-1].parts)
    assert any(isinstance(p, TextPart) for p in msgs[-1].parts)


def test_strip_foreign_thinking_no_vacia_un_response():
    """Un assistant sin partes lo rechazan varios providers."""
    solo_thinking = ModelResponse(parts=[ThinkingPart(content="…")],
                                  model_name="MiniMax-M3")
    msgs = [solo_thinking, _resp("MiniMax-M3")]
    experts._strip_foreign_thinking(msgs, "openai:gpt-4o")
    assert solo_thinking.parts


def test_strip_foreign_thinking_sin_model_name_no_adivina():
    msgs = [ModelResponse(parts=[ThinkingPart(content="…"),
                                 TextPart(content="hola")])]
    assert experts._strip_foreign_thinking(msgs, "nvidia:x") == 0


def test_model_key_normaliza():
    assert experts._model_key("minimax:MiniMax-M3") == "minimax-m3"
    assert experts._model_key("MiniMax-M3") == "minimax-m3"
    assert experts._model_key("nvidia:nvidia/nemotron-3-ultra") == "nvidia/nemotron-3-ultra"


# ---------- 9. compactación con puente ----------


def _thread_with_tools():
    """Hilo con un turno viejo y un turno vivo con tool call cerrada."""
    return ModelMessagesTypeAdapter.dump_json([
        ModelRequest(parts=[UserPromptPart(content="turno viejo")]),
        ModelResponse(parts=[TextPart(content="respuesta vieja")]),
        ModelRequest(parts=[UserPromptPart(content="leé config.py")]),
        ModelResponse(parts=[ToolCallPart(
            tool_name="read_file", args='{"path":"config.py"}',
            tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(
            tool_name="read_file", content="PORT = 8413", tool_call_id="c1")]),
        ModelResponse(parts=[TextPart(content="el puerto es 8413")]),
    ]).decode("utf-8")


def test_compactacion_conserva_el_ultimo_turno():
    out = memory.build_compacted_history(
        "resumen del hilo", facts=["el relay corre en 8413"],
        previous_json=_thread_with_tools())
    assert "leé config.py" in out
    assert "PORT = 8413" in out          # el working set sobrevive
    assert "respuesta vieja" not in out  # lo viejo no
    assert "resumen del hilo" in out


def test_compactacion_con_puente_es_replayable():
    """Lo que sale se tiene que poder replayar (pares call↔return cerrados)."""
    out = memory.build_compacted_history(
        "resumen", previous_json=_thread_with_tools())
    msgs = list(ModelMessagesTypeAdapter.validate_json(out))
    calls = {p.tool_call_id for m in msgs if isinstance(m, ModelResponse)
             for p in m.parts if isinstance(p, ToolCallPart)}
    returns = {p.tool_call_id for m in msgs if isinstance(m, ModelRequest)
               for p in m.parts if isinstance(p, ToolReturnPart)}
    assert calls == returns


def test_compactacion_descarta_un_puente_gigante():
    """Compactar y arrastrar 60k del último turno sería contradictorio."""
    gordo = ModelMessagesTypeAdapter.dump_json([
        ModelRequest(parts=[UserPromptPart(content="dame todo")]),
        ModelResponse(parts=[TextPart(content="x" * 40_000)]),
    ]).decode("utf-8")
    out = memory.build_compacted_history("resumen", previous_json=gordo)
    assert "xxxxx" not in out
    assert "resumen" in out


@pytest.mark.parametrize("bad", ["", "no json", "[]"])
def test_compactacion_sin_puente_queda_como_antes(bad):
    out = memory.build_compacted_history("resumen", previous_json=bad)
    msgs = ModelMessagesTypeAdapter.validate_json(out)
    assert len(msgs) == 2


def test_strip_foreign_thinking_tolera_sufijo_de_version():
    """Falso positivo encontrado en la revisión del 2026-08-16.

    OpenAI responde `gpt-4o-2024-08-06` cuando pediste `gpt-4o`, y varios
    OpenAI-compat agregan sufijo de versión. Con comparación estricta,
    CADA turno de un hilo que nunca cambió de modelo borraba el thinking
    del turno anterior: pérdida de contexto silenciosa, exactamente lo
    que este guard existe para evitar.
    """
    msgs = [ModelRequest(parts=[UserPromptPart(content="x")]),
            _resp("gpt-4o-2024-08-06")]
    assert experts._strip_foreign_thinking(msgs, "openai:gpt-4o") == 0
    assert any(isinstance(p, ThinkingPart) for p in msgs[-1].parts)


def test_distinto_modelo_reconoce_familias_distintas():
    assert experts._distinto_modelo("minimax-m3", "nvidia/nemotron-3-ultra")
    assert not experts._distinto_modelo("gpt-4o-2024-08-06", "gpt-4o")
    assert not experts._distinto_modelo("gpt-4o", "gpt-4o-2024-08-06")
    assert not experts._distinto_modelo("minimax-m3", "minimax-m3")
    # Ante la duda (una clave vacía) NO se toca el historial.
    assert not experts._distinto_modelo("", "gpt-4o")
    assert not experts._distinto_modelo("gpt-4o", "")


# ---------- 10. el bloque de evidencia se enciende por CAPACIDAD ----------
#
# Hueco detectado en la revisión del 2026-08-16: la condición pasó de
# `"obscura" in _attached_terms` (un NOMBRE) a `"browser" in ...` (una
# CAPACIDAD) sin test. Si eso se rompe, el experto pierde la política de
# evidencia en silencio — o peor, la recibe sin browser adjunto y se le
# pide que llame tools que no tiene.


async def _instructions_con_mcps(attached, monkeypatch):
    """Corre run_expert con MCPs falsos y devuelve el system prompt."""
    from pydantic_ai.models.test import TestModel

    sink = {}
    _RealAgent = experts.Agent          # antes de parchear: si no, recursión

    class _SpyAgent:
        def __init__(self, *a, **kw):
            sink.setdefault("instructions", kw.get("instructions", ""))
            self._inner = _RealAgent(*a, **kw)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    async def fake_catalog(db, project, selection, pool, timeout=None,
                           inflight=None, hide_tools=frozenset(), image_artifacts=None, vision=True):
        return [], attached, attached

    monkeypatch.setattr(experts, "_catalog_toolsets", fake_catalog)
    monkeypatch.setattr(experts, "build_model", lambda spec: TestModel(call_tools=[]))
    monkeypatch.setattr(experts, "Agent", _SpyAgent)
    monkeypatch.setattr(experts, "cbm_binary_path", lambda: None)

    proj = _project()
    proj["id"] = 1                      # habilita el camino del catálogo
    proj["repo_path"] = "."
    await experts.run_expert(
        proj, "hola", db=object(), model_override="minimax:MiniMax-M3")
    instr = sink.get("instructions", "")
    # `instructions=` es una lista desde la bitácora (2026-08-17): el
    # bloque estable + el callable que la renderiza. Se aplana porque los
    # tests preguntan por el system prompt completo — y porque un `in`
    # contra la lista cruda pasaría por membresía de elementos, o sea que
    # los asserts negativos ("no se inyecta") pasarían siempre.
    if isinstance(instr, (list, tuple)):
        partes = [p() if callable(p) else p for p in instr]
        instr = "\n\n".join(p for p in partes if p)
    return instr


async def test_evidencia_se_inyecta_con_capability_browser(monkeypatch):
    instr = await _instructions_con_mcps(
        [{"name": "playwright-mcp", "capability": "browser", "on_demand": 0}],
        monkeypatch)
    assert "Evidencia obligatoria" in instr
    # …y nombra las tools que el browser REALMENTE tiene, que son las de
    # `@playwright/mcp` (la fila del catálogo apunta ahí).
    #
    # 2026-08-19: este assert decía `get_text` y `browser_snapshot not in`,
    # con el comentario "las de obscura, no". Al revés: `browser_snapshot`
    # es del browser vigente y `get_text` es del módulo huérfano
    # `mcp_servers/playwright_mcp.py`, que nadie spawnea. O sea que el
    # test estaba fijando el prompt equivocado — por eso el bug sobrevivió
    # tres días de runs fallidos. El contraste contra los nombres reales
    # lo hace `test_evidence_block_nombra_tools_que_existen`.
    for viva in ("browser_navigate", "browser_snapshot", "browser_click",
                 "browser_type", "browser_take_screenshot"):
        assert viva in instr, viva
    for muerta in ("`get_text(", "`get_html(", "`screenshot(path)`"):
        assert muerta not in instr, muerta


async def test_evidencia_no_se_inyecta_sin_browser(monkeypatch):
    instr = await _instructions_con_mcps(
        [{"name": "github-mcp", "capability": "github", "on_demand": 0}],
        monkeypatch)
    assert "Evidencia obligatoria" not in instr


# ---------- 9/9/2026: rondas y vocabulario de resultado ----------


def test_stages_json_conserva_las_rondas_que_el_runner_ya_calculaba():
    """Sin esto, "resuelto en una pasada" y "resuelto tras tres rondas de
    needs_more" quedaban IDENTICOS en la fila.

    El runner arma `verifier_rounds` para cortar su propio lazo y hasta
    hoy lo tiraba al serializar. Medir si un cambio de prompt o de modelo
    mejora algo obligaba a parsear los logs a mano.
    """
    rondas = [{"ronda": 1, "verdict": "needs_more", "feedback": "falta el test",
               "phase_at_end": "tool", "tool_calls": 4},
              {"ronda": 2, "verdict": "complete", "feedback": "ok",
               "phase_at_end": "done", "tool_calls": 9}]
    d = json.loads(server._stages_json({
        "three_stage": True, "verifier_verdict": "complete",
        "verifier_rounds": rondas}))
    assert d["rondas"] == 2
    assert [r["verdict"] for r in d["rondas_detalle"]] == ["needs_more",
                                                           "complete"]
    assert d["rondas_detalle"][1]["tool_calls"] == 9


def test_stages_json_conserva_los_veredictos_de_media_corrida():
    """Un corte por desvio se veia en el mensaje al humano y no quedaba
    en ningun lado para revisar despues POR QUE se fue del plan."""
    d = json.loads(server._stages_json({
        "three_stage": True, "verifier_verdict": "off_plan",
        "mid_verdicts": [{"leg": 2, "verdict": "off_plan",
                          "feedback": "toco otro modulo", "usage": {},
                          "error": ""}]}))
    assert d["mid_verdicts"][0]["leg"] == 2
    assert d["mid_verdicts"][0]["feedback"] == "toco otro modulo"
    # El `usage` no viaja: los tokens ya tienen su lugar propio.
    assert "usage" not in d["mid_verdicts"][0]


def test_el_resultado_del_trabajo_no_es_el_estado_de_la_ejecucion():
    """`chats.status` dice si el run termino; esto, si lo que salio sirve.

    Contar "exito" como "runs sin error" hacia que un run prolijo que el
    verificador marco `needs_more` contara igual que uno aprobado.
    """
    def r(**kw):
        return json.loads(server._stages_json(
            {"three_stage": True, **kw}))["resultado"]

    assert r(verifier_verdict="complete") == "aprobado"
    assert r(verifier_verdict="needs_more") == "pendiente"
    assert r(verifier_verdict="needs_human") == "intervencion"
    assert r(verifier_verdict="off_plan") == "desviado"


def test_un_verificador_caido_no_cuenta_como_aprobado():
    """Separar "el trabajo estaba mal" de "el verificador no corrio" es lo
    que evita que una degradacion del supervisor infle las metricas."""
    def r(**kw):
        return json.loads(server._stages_json(
            {"three_stage": True, **kw}))["resultado"]

    assert r(verifier_verdict="", stage_errors={"verifier": "timeout"}) == \
        "sin_verificar"
    assert r(verifier_verdict="") == "sin_verificar"
    # Y un verdict que no conocemos tampoco se cuela como aprobado.
    assert r(verifier_verdict="lo_que_sea") == "sin_verificar"
