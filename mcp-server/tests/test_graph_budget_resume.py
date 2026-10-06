"""Reanudación explícita de nodos cortados por límite de presupuesto."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from relay import grafo as G
from relay.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(path=tmp_path / "resume.db")
    await database.init_schema()
    await database.create_task_graph("g", "Terminar el trabajo", tareas=[
        {"id": "parent", "titulo": "Trabajo grande", "idempotente": False},
        {"id": "consumer", "titulo": "Consumir resultado", "deps": ["parent"]},
        {"id": "guarded", "titulo": "Depende de otro fallo", "deps": ["parent", "independent"]},
        {"id": "independent", "titulo": "Fallo ajeno"},
    ])
    return database


async def _autosplit_scenario(db, *, error="el nodo terminó en 'budget_exceeded'",
                              intentos=2, max_intentos=2):
    await db.update_task("parent", estado=G.FALLADO,
                         error="subdividido en 2 subtareas: part_budget, part_done", intentos=1)
    await db.add_tasks_to_graph("g", [
        {"id": "part_budget", "titulo": "Parte cortada", "archivos": ["src/a.py"],
         "max_intentos": max_intentos},
        {"id": "part_done", "titulo": "Parte completa"},
    ], reemplaza="parent")
    chat_id = await db.create_chat(project_slug=None, source="grafo", author="orquestador",
                                   target=None, user_prompt="Parte cortada")
    await db.update_task("part_budget", estado=G.FALLADO,
                         error=error, intentos=intentos, chat_id=chat_id)
    await db.update_task("part_done", estado=G.HECHO, resultado="completo")
    await db.update_task("consumer", estado=G.BLOQUEADO, error="dependencia no completada")
    await db.update_task("independent", estado=G.FALLADO, error="prueba funcional fallida")
    await db.update_task("guarded", estado=G.BLOQUEADO,
                         error="dependencia no completada")
    return chat_id


async def test_reabre_solo_corte_de_presupuesto_y_su_dependiente_desbloqueable(db):
    from relay.orchestrator_recovery import prepare_graph_resume

    chat_id = await _autosplit_scenario(db)

    reopened = await prepare_graph_resume(db, await db.get_task_graph("g"))
    graph = await db.get_task_graph("g")
    tasks = {t["id"]: t for t in graph["tasks"]}

    assert set(reopened) == {"part_budget", "consumer"}
    assert tasks["parent"]["estado"] == G.FALLADO
    assert tasks["part_done"]["estado"] == G.HECHO
    assert tasks["part_budget"]["estado"] == tasks["consumer"]["estado"] == G.PENDIENTE
    assert tasks["part_budget"]["intentos"] == 2
    assert tasks["part_budget"]["max_intentos"] >= 3
    assert tasks["part_budget"]["chat_id"] == chat_id
    assert json.loads(tasks["part_budget"]["archivos"]) == ["src/a.py"]
    assert tasks["independent"]["estado"] == G.FALLADO
    assert tasks["guarded"]["estado"] == G.BLOQUEADO

    # Repetir la acción no concede intentos adicionales al nodo ya abierto.
    assert await prepare_graph_resume(db, await db.get_task_graph("g")) == []
    assert (await db.get_task_graph("g"))["estado"] == "activo"


async def test_reanudacion_es_atomica_si_falla_el_checkpoint(db):
    from relay.orchestrator_recovery import prepare_graph_resume

    await _autosplit_scenario(db)
    await db.run("UPDATE task_graphs SET estado='fallado' WHERE id='g'")
    await db.run("CREATE TRIGGER reject_resume BEFORE UPDATE ON task_graphs "
                 "WHEN NEW.estado='activo' BEGIN SELECT RAISE(ABORT, 'resume rejected'); END")

    with pytest.raises(Exception, match="resume rejected"):
        await prepare_graph_resume(db, await db.get_task_graph("g"))

    graph = await db.get_task_graph("g")
    tasks = {t["id"]: t for t in graph["tasks"]}
    assert graph["estado"] == "fallado"
    assert tasks["part_budget"]["estado"] == G.FALLADO
    assert tasks["consumer"]["estado"] == G.BLOQUEADO


async def test_historial_guardado_llega_al_siguiente_intento_del_nodo(db):
    from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, ModelResponse
    from pydantic_ai.messages import TextPart, UserPromptPart
    from relay import experts
    from relay.orchestrator_execution import ejecutor_minimax
    from relay.orchestrator_recovery import _RESUME_NOTICE

    await db.create_task_graph("history", "Continuar", tareas=[
        {"id": "resume", "titulo": "Continuar trabajo"}])
    previous_chat = await db.create_chat(project_slug="demo", source="grafo",
                                         author="orquestador", target="demo")
    history = ModelMessagesTypeAdapter.dump_json([
        ModelRequest(parts=[UserPromptPart(content="evidencia previa")]),
        ModelResponse(parts=[TextPart(content="el archivo ya está creado")]),
    ]).decode()
    await db.finish_chat(previous_chat, status="error", artifact={
        "messages_json": history, "content": "resultado previo con evidencia"})
    await db.update_task("resume", detalle=_RESUME_NOTICE + "Continuar sin repetir",
                         estado=G.PENDIENTE, chat_id=previous_chat)
    graph = await db.get_task_graph("history")
    called = {}

    async def fake_run_expert(_project, _prompt, **kwargs):
        called.update(kwargs)
        return {"content": "terminado", "phase_at_end": "complete", "model": "test"}

    project = {"slug": "demo", "defaults_json": {"graph_verifier": False}}
    with patch.object(experts, "run_expert", fake_run_expert):
        execute = ejecutor_minimax(db, project, graph)
        result = await execute(graph["tasks"][0])

    assert result["ok"] is True
    assert called["message_history_json"] == history
    assert called["chat_id"] != previous_chat


async def test_corte_de_presupuesto_conserva_historial_en_chat_outputs(db):
    from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, ModelResponse
    from pydantic_ai.messages import TextPart, UserPromptPart
    from relay import experts
    from relay.orchestrator_execution import ejecutor_minimax

    await db.create_task_graph("budget-history", "Retener evidencia", tareas=[
        {"id": "budget", "titulo": "Trabajo interrumpido"}])
    history = ModelMessagesTypeAdapter.dump_json([
        ModelRequest(parts=[UserPromptPart(content="inspeccioné el archivo")]),
        ModelResponse(parts=[TextPart(content="queda una función pendiente")]),
    ]).decode()

    async def fake_run_expert(_project, _prompt, **_kwargs):
        return {"content": "avance parcial", "phase_at_end": "budget_exceeded",
                "model": "test", "messages_json": history}

    project = {"slug": "demo", "defaults_json": {"graph_verifier": False}}
    with patch.object(experts, "run_expert", fake_run_expert):
        execute = ejecutor_minimax(
            db, project, await db.get_task_graph("budget-history"))
        result = await execute((await db.list_tasks("budget-history"))[0])

    assert result["ok"] is False
    output = (await db.run(
        "SELECT payload FROM chat_outputs WHERE chat_id=?", (result["chat_id"],)
    ))[0]
    assert json.loads(output["payload"])["messages_json"] == history


async def test_nodo_historico_sin_historial_exige_inspeccionar_archivos(db):
    from relay.orchestrator_recovery import graph_resume_context, prepare_graph_resume

    await _autosplit_scenario(db)
    await prepare_graph_resume(db, await db.get_task_graph("g"))
    task = next(t for t in (await db.get_task_graph("g"))["tasks"] if t["id"] == "part_budget")
    history, context = await graph_resume_context(db, task)
    assert history == ""
    assert "Inspecciona los archivos" in context


@pytest.mark.parametrize("payload,partial", [
    ("{", ""), ("[]", ""),
    ('{"messages_json":"invalid","content":"avance conservado"}', "avance conservado"),
    ('{"messages_json":17,"content":"avance conservado"}', "avance conservado"),
])
async def test_historial_invalido_no_se_reutiliza(db, payload, partial):
    from relay.orchestrator_recovery import graph_resume_context, prepare_graph_resume

    chat_id = await _autosplit_scenario(db)
    await db.finish_chat(chat_id, status="error", artifact={"content": "parcial"})
    await db.run("UPDATE chat_outputs SET payload=? WHERE chat_id=?", (payload, chat_id))
    await prepare_graph_resume(db, await db.get_task_graph("g"))
    task = next(t for t in (await db.get_task_graph("g"))["tasks"] if t["id"] == "part_budget")
    history, context = await graph_resume_context(db, task)
    assert history == ""
    assert "Inspecciona los archivos" in context
    assert partial in context


@pytest.mark.asyncio
async def test_endpoint_resume_ejecuta_solo_fallo_y_dependiente_sin_repetir_hechos(
        monkeypatch):
    # Reusa únicamente el setup HTTP local existente, sin alterar su suite.
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    from test_disparador import _Base
    from relay import orquestador

    class ResumeRoute(_Base):
        async def exercise(self):
            await self.db.upsert_project({"slug": "demo", "name": "Demo",
                                          "repo_path": str(self._tmp.name),
                                          "defaults_json": {"graph_verifier": False}})
            await self.db.create_task_graph("resume-route", "Retomar", tareas=[
                {"id": "parent-route", "titulo": "Trabajo grande"},
                {"id": "consumer-route", "titulo": "Continuar", "deps": ["parent-route"]},
                *[{"id": f"fact-{i}", "titulo": f"Hecho {i}"} for i in range(13)],
            ], project_slug="demo")
            await self.db.update_task("parent-route", estado=G.FALLADO,
                                      error="subdividido en 2 subtareas: budget-route, done-route")
            await self.db.add_tasks_to_graph("resume-route", [
                {"id": "budget-route", "titulo": "Parte cortada"},
                {"id": "done-route", "titulo": "Parte hecha"},
            ], reemplaza="parent-route")
            await self.db.update_task("budget-route", estado=G.FALLADO,
                                      error="el nodo terminó en 'budget_split'",
                                      intentos=1, max_intentos=1)
            await self.db.update_task("done-route", estado=G.HECHO)
            await self.db.update_task("consumer-route", estado=G.BLOQUEADO)
            for i in range(13):
                await self.db.update_task(f"fact-{i}", estado=G.HECHO)

            executed = []
            completed = []

            async def execute(task):
                executed.append(task["id"])
                return {"ok": True, "resultado": "terminado"}

            async def fake_launch(db, project, graph_id, **_):
                result = await orquestador.correr_grafo(db, graph_id, ejecutar=execute)
                completed.append(result)
                return result

            with patch("relay.orquestador.lanzar", fake_launch):
                response = await self.client.post("/graphs/resume-route/resume")
                assert response.status == 202
                await self._esperar(lambda: completed)
            assert executed == ["budget-route", "consumer-route"]
            graph = await self.db.get_task_graph("resume-route")
            tasks = {t["id"]: t for t in graph["tasks"]}
            assert tasks["parent-route"]["estado"] == G.FALLADO
            assert tasks["done-route"]["estado"] == G.HECHO
            assert all(tasks[f"fact-{i}"]["estado"] == G.HECHO for i in range(13))
            assert graph["estado"] == "hecho"

    case = ResumeRoute()
    await case.asyncSetUp()
    try:
        await case.exercise()
    finally:
        await case.asyncTearDown()


@pytest.mark.asyncio
@pytest.mark.parametrize("task_state,error", [
    (G.HECHO, ""),
    (G.FALLADO, "prueba funcional fallida"),
    (G.ESPERANDO, "decisión humana pendiente"),
])
async def test_endpoint_resume_sin_trabajo_lanzable_es_409_sin_mutar(
        monkeypatch, task_state, error):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    from test_disparador import _Base
    from relay import orquestador

    class NoopResumeRoute(_Base):
        async def exercise(self):
            await self.db.create_task_graph(
                "resume-noop", "Retomar", tareas=[{"id": "only", "titulo": "Única"}],
                project_slug="demo")
            await self.db.update_task("only", estado=task_state, error=error)
            if task_state == G.HECHO:
                # El veredicto global no autoriza repetir nodos terminados.
                await self.db.run(
                    "UPDATE task_graphs SET estado='fallado', verificacion_json=? WHERE id=?",
                    (json.dumps({"verdict": "off_plan"}), "resume-noop"))
            before = await self.db.get_task_graph("resume-noop")
            launched = []

            async def fake_launch(*_args, **_kwargs):
                launched.append(True)

            with patch.object(orquestador, "lanzar", fake_launch):
                response = await self.client.post("/graphs/resume-noop/resume")

            assert response.status == 409
            assert launched == []
            assert await self.db.get_task_graph("resume-noop") == before

    case = NoopResumeRoute()
    await case.asyncSetUp()
    try:
        await case.exercise()
    finally:
        await case.asyncTearDown()
