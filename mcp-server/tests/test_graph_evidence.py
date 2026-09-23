from relay import bitacora, expert_models, expert_runner, expert_stages, expert_steps, expert_verdicts
"""Regresiones de la verificación AuroraDemo: correlación y recuperación tardía."""
import json
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

from relay import experts, orquestador


def call(cmd, *, ident=None, ts="2026-09-10T18:00:00Z"):
    return {"phase": "tool_call", "tool": "shell", "cmd": cmd,
            "tool_call_id": ident, "ts": ts}


def result(output, *, ident=None, ts="2026-09-10T18:00:01Z"):
    return {"phase": "tool_result", "tool": "shell", "output": output,
            "tool_call_id": ident, "ts": ts}


def test_historico_consume_background_y_no_adivina_un_lote_intercalado():
    rows, omitted = orquestador._comandos_con_resultado([
        call("dotnet run --background"), result("background activo"),
        call("newman run old.json"), call("Get-Content report.json"),
        result("report\n(exit=0)"), result("failed\n(exit=1)"),
        call("New-Item -PassThru"), result("invalid parameter\n(exit=1)"),
        call("newman run final.json"),
        result("requests=3 assertions=3 failures=0\n(exit=0)"),
    ])
    assert [(r["cmd"], r["exit"]) for r in rows] == [
        ("dotnet run --background", None), ("New-Item -PassThru", 1),
        ("newman run final.json", 0)]
    assert omitted == 2
    assert rows[-1]["resumen"] == "requests=3 assertions=3 failures=0"


def test_ids_asocian_resultados_fuera_de_orden_y_no_leen_exit_citado():
    rows, omitted = orquestador._comandos_con_resultado([
        call("newman run final.json", ident="newman"),
        call("New-Item -PassThru", ident="mkdir"),
        result("bad parameter\n(exit=1)", ident="mkdir"),
        result("previous example (exit=1)\nrequests=3 failures=0\n(exit=0)",
               ident="newman"),
    ])
    assert [(r["cmd"], r["exit"]) for r in rows] == [
        ("New-Item -PassThru", 1), ("newman run final.json", 0)]
    assert omitted == 0


def test_el_id_viaja_desde_las_partes_del_modelo_hasta_la_salida():
    requested = ModelResponse(parts=[ToolCallPart(
        "shell", {"cmd": "newman run final.json"}, tool_call_id="n")])
    calls, _, _ = expert_steps._clasificar_partes(requested)
    assert calls == [("shell", {"cmd": "newman run final.json"}, "n")]
    returned = SimpleNamespace(request=ModelRequest(parts=[ToolReturnPart(
        "shell", "requests=3 failures=0\n(exit=0)", tool_call_id="n")]))
    assert expert_steps._console_tool_outputs(returned) == [
        ("shell", "requests=3 failures=0\n(exit=0)", "n")]


@pytest.mark.asyncio
async def test_la_evidencia_final_conserva_recuperacion_y_avisa_recortes(monkeypatch):
    tasks = []
    chats = {}
    for i in range(25):
        cid = f"c{i}"
        tasks.append({"id": f"t{i}", "titulo": f"tarea {i}", "chat_id": cid,
                      "estado": "hecho", "orden": i, "error": "", "deps": [],
                      "ended_at": f"2026-09-10T17:{i:02}:00Z",
                      "resultado": "anterior 7/7 " + "x" * 800})
        chats[cid] = {"progress_events": json.dumps([
            call("dotnet test", ts=f"2026-09-10T17:{i:02}:00Z"),
            result("Passed: 7\n(exit=0)", ts=f"2026-09-10T17:{i:02}:01Z")])}
    # Orden bajo como los hijos de autosplit, pero es el último cierre.
    tasks.append({"id": "final", "titulo": "Prueba final", "chat_id": "final",
                  "estado": "hecho", "orden": 0, "error": "", "deps": [],
                  "ended_at": "2026-09-10T18:40:00Z",
                  "resultado": "Newman 3/3 requests, cero fallas; tests 23/23."})
    chats["final"] = {"progress_events": json.dumps([
        call("New-Item -PassThru"), result("bad parameter\n(exit=1)"),
        call("newman run old.json"), result("failed\n(exit=1)"),
        call("newman run final.json", ts="2026-09-10T18:37:00Z"),
        result("requests=3 assertions=3 failures=0\n(exit=0)",
               ts="2026-09-10T18:37:07Z")])}

    class DB:
        async def get_chat(self, cid):
            return chats[cid]

    graph = {"tasks": tasks}
    raw = await orquestador._evidencia_de_los_nodos(DB(), graph)
    rendered = bitacora.Bitacora.cargar(raw).evidencia(max_chars=4000)
    assert "newman run final.json → exit=0" in rendered
    assert "requests=3 assertions=3 failures=0" in rendered
    assert "comandos omitidos" in rendered
    assert "New-Item -PassThru → exit=0" not in rendered
    assert len(rendered) <= 4000
    summary = orquestador._resumen_de_nodos(graph)
    assert "Newman 3/3 requests, cero fallas; tests 23/23" in summary
    assert summary.index("23/23") < summary.index("7/7")
    assert "sin detalle" in summary

    prompts = []

    class Agent:
        def __init__(self, *a, **kw):
            pass

        async def run(self, prompt):
            prompts.append(prompt)
            return SimpleNamespace(output="VERDICT: complete\nFEEDBACK: comprobado")

    monkeypatch.setattr(expert_stages, "Agent", Agent)
    monkeypatch.setattr(expert_models, "build_model", lambda _: object())
    monkeypatch.setattr(expert_verdicts, "_stage_usage", lambda _: {})
    await expert_stages._run_verifier(
        user="Probar integración", plan="Newman y tests", model_spec="test", ponytail="",
        executor_result={"graph_id": "g", "content": summary, "bitacora_json": raw})
    assert "newman run final.json → exit=0" in prompts[0]
    assert "Newman 3/3 requests, cero fallas; tests 23/23" in prompts[0]
    assert "comprobación más reciente" in prompts[0]


def test_bitacora_no_recorta_evidencia_sin_decirlo():
    bit = bitacora.Bitacora()
    for i in range(20):
        bit.anotar_comando(f"test {i} " + "x" * 120, 0)
    evidence = bit.evidencia(max_chars=500)
    assert len(evidence) <= 500
    assert "evidencia recortada" in evidence


def test_resumen_distingue_padre_sustituido_de_un_fallo_operativo():
    graph = {"tasks": [
        {"id": "p", "orden": 0, "titulo": "padre", "estado": "fallado",
         "error": "subdividido en dos subtareas", "deps": []},
        {"id": "s1", "orden": 1, "titulo": "hijo 1", "estado": "hecho",
         "parent_id": "p", "deps": []},
        {"id": "s2", "orden": 2, "titulo": "hijo 2", "estado": "hecho",
         "parent_id": "p", "deps": []},
    ]}
    summary = orquestador._resumen_de_nodos(graph)
    assert "[subdividido; historial: fallado] padre" in summary
    assert "[fallado] padre" not in summary
    assert graph["tasks"][0]["estado"] == "fallado"
