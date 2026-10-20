"""Aceptación y reanudación de nodos con respuestas y datos sintéticos."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter, ModelRequest, ModelResponse, TextPart, UserPromptPart,
)

from relay import expert_models, expert_runner, expert_staged_runner, expert_stages, experts
from relay import grafo as G
from relay.db import Database
from relay.orchestrator_execution import ejecutor_minimax
from relay.orchestrator_recovery import graph_resume_context, prepare_graph_resume


@pytest.fixture
async def db(tmp_path):
    database = Database(path=tmp_path / "acceptance.db")
    await database.init_schema()
    await database.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    await database.create_task_graph("g", "Completar una función", tareas=[
        {"id": "node", "titulo": "Implementar la función", "idempotente": False},
        {"id": "consumer", "titulo": "Comprobar resultado", "deps": ["node"]},
        {"id": "done", "titulo": "Trabajo conservado"},
    ], project_slug="demo")
    return database


def _history():
    return ModelMessagesTypeAdapter.dump_json([
        ModelRequest(parts=[UserPromptPart(content="Comprueba la función existente")]),
        ModelResponse(parts=[TextPart(content="Falta comprobar el caso vacío")]),
    ]).decode()


async def test_existing_plan_skips_planner_and_documenter_with_bounded_rounds(monkeypatch):
    planner = AsyncMock(side_effect=AssertionError("El nodo ya tiene plan"))
    documenter = AsyncMock(side_effect=AssertionError("El nodo no documenta"))
    executor = AsyncMock(return_value={
        "content": "Avance guardado", "phase_at_end": "complete", "model": "test",
        "messages_json": _history(), "tool_calls": 1,
    })
    verifier = AsyncMock(return_value=("needs_more", "Falta un caso", {}, "", []))
    monkeypatch.setattr(expert_models, "read_ponytail", AsyncMock(return_value=""))
    monkeypatch.setattr(expert_stages, "_run_planner", planner)
    monkeypatch.setattr(expert_stages, "_run_documenter", documenter)
    monkeypatch.setattr(expert_stages, "_run_verifier", verifier)
    monkeypatch.setattr(expert_runner, "run_expert", executor)

    result = await expert_staged_runner.run_expert_staged(
        {"slug": "demo", "defaults_json": {"verifier_rounds": 1}}, "Completar función",
        existing_plan="1. Implementar y comprobar el caso vacío", documenter=False,
    )

    planner.assert_not_awaited()
    documenter.assert_not_awaited()
    assert executor.await_count == verifier.await_count == 2
    assert result["verifier_verdict"] == "needs_more"
    assert result["plan"] == "1. Implementar y comprobar el caso vacío"
    assert "existing_plan" not in executor.call_args.kwargs
    assert result["stage_usage"]["planner"] == {}


@pytest.mark.parametrize("verdict", ["complete", "needs_more", "needs_human", "off_plan", "", "unknown"])
async def test_node_requires_complete_verdict_and_preserves_evidence(db, monkeypatch, verdict):
    captured = {}
    history = _history()

    async def staged(_project, _prompt, **kwargs):
        captured.update(kwargs)
        return {
            "content": "Archivo editado", "phase_at_end": "complete", "model": "test",
            "verifier_verdict": verdict, "verifier_feedback": "Comprueba el caso vacío",
            "three_stage": True, "messages_json": history,
        }

    monkeypatch.setattr(experts, "run_expert_staged", staged)
    monkeypatch.setattr(experts, "run_expert", AsyncMock(side_effect=AssertionError("Se debe verificar")))
    graph = await db.get_task_graph("g")
    result = await ejecutor_minimax(db, {"slug": "demo"}, graph)(graph["tasks"][0])

    assert result["ok"] is (verdict == "complete")
    assert captured["existing_plan"].startswith("1. Completa los criterios")
    assert captured["verifier"] is True and captured["documenter"] is False
    chat = await db.get_chat(result["chat_id"])
    assert chat["status"] == ("ok" if verdict == "complete" else "error")
    stages = json.loads(chat["stages_json"])
    assert stages["verifier_verdict"] == verdict
    assert stages["verifier_feedback"] == "Comprueba el caso vacío"
    if verdict != "complete":
        output = await db.run("SELECT payload FROM chat_outputs WHERE chat_id=?", (chat["id"],))
        assert json.loads(output[0]["payload"])["messages_json"] == history
    if verdict == "off_plan":
        await db.update_task("node", estado=G.FALLADO, error=result["error"],
                             chat_id=result["chat_id"], intentos=1, max_intentos=1)
        assert await prepare_graph_resume(db, await db.get_task_graph("g")) == ["node"]
        node = next(t for t in await db.list_tasks("g") if t["id"] == "node")
        restored, context = await graph_resume_context(db, node)
        assert restored == history
        assert "Comprueba el caso vacío" in context


async def test_graph_verifier_optout_preserves_single_turn_executor(db, monkeypatch):
    executor = AsyncMock(return_value={"content": "Listo", "phase_at_end": "complete"})
    monkeypatch.setattr(experts, "run_expert", executor)
    monkeypatch.setattr(experts, "run_expert_staged", AsyncMock(side_effect=AssertionError("Opt-out")))
    graph = await db.get_task_graph("g")
    result = await ejecutor_minimax(
        db, {"slug": "demo", "defaults_json": {"graph_verifier": False}}, graph,
    )(graph["tasks"][0])
    assert result["ok"] is True
    assert "existing_plan" not in executor.call_args.kwargs


async def test_dependency_results_remain_context_without_becoming_system_plan(db, monkeypatch):
    await db.update_task("node", estado=G.HECHO, resultado="DEPENDENCY_RESULT_MARKER")
    captured = {}

    async def staged(_project, prompt, **kwargs):
        captured.update(kwargs, prompt=prompt)
        return {"content": "Comprobado", "phase_at_end": "complete", "verifier_verdict": "complete"}

    monkeypatch.setattr(experts, "run_expert_staged", staged)
    graph = await db.get_task_graph("g")
    task = next(t for t in graph["tasks"] if t["id"] == "consumer")
    assert (await ejecutor_minimax(db, {"slug": "demo"}, graph)(task))["ok"] is True
    assert "DEPENDENCY_RESULT_MARKER" in captured["prompt"]
    assert "DEPENDENCY_RESULT_MARKER" not in captured["existing_plan"]


async def test_question_pauses_without_turning_chat_into_verification_error(db, monkeypatch):
    monkeypatch.setattr(experts, "run_expert_staged", AsyncMock(return_value={
        "content": "¿Qué formato necesitas?", "phase_at_end": "writing", "question_id": "q_demo",
    }))
    graph = await db.get_task_graph("g")
    result = await ejecutor_minimax(db, {"slug": "demo"}, graph)(graph["tasks"][0])
    assert result["ok"] is False and result["pregunta"] == "q_demo"
    assert (await db.get_chat(result["chat_id"]))["status"] == "ok"


async def test_explicit_off_plan_resume_restores_feedback_without_repeating_done_nodes(db):
    previous_history = _history()
    chat_id = await db.create_chat(project_slug="demo", source="grafo", target="demo", author="orquestador")
    await db.finish_chat(chat_id, status="error", stages_json=json.dumps({
        "verifier_verdict": "off_plan", "verifier_feedback": "Conserva la API y corrige el caso vacío",
    }), artifact={"messages_json": previous_history, "content": "Implementación parcial"})
    await db.update_task("node", estado=G.FALLADO, error="el nodo terminó en 'off_plan'",
                         chat_id=chat_id, intentos=2, max_intentos=2)
    await db.update_task("consumer", estado=G.BLOQUEADO)
    await db.update_task("done", estado=G.HECHO, resultado="Comprobado")

    reopened = await prepare_graph_resume(db, await db.get_task_graph("g"))
    tasks = {t["id"]: t for t in await db.list_tasks("g")}
    assert set(reopened) == {"node", "consumer"}
    assert tasks["node"]["max_intentos"] == 3
    assert tasks["done"]["estado"] == G.HECHO
    history, context = await graph_resume_context(db, tasks["node"])
    assert history == previous_history
    assert "Conserva la API y corrige el caso vacío" in context
    assert await prepare_graph_resume(db, await db.get_task_graph("g")) == []


@pytest.mark.parametrize("stages", ["{", "[]", "null"])
async def test_invalid_previous_stage_data_does_not_block_resume(db, stages):
    chat_id = await db.create_chat(project_slug="demo", source="grafo", target="demo", author="orquestador")
    await db.finish_chat(chat_id, status="error", stages_json=stages)
    await db.update_task("node", estado=G.FALLADO, error="el nodo terminó en 'off_plan'", chat_id=chat_id)
    await prepare_graph_resume(db, await db.get_task_graph("g"))
    task = next(t for t in await db.list_tasks("g") if t["id"] == "node")
    history, context = await graph_resume_context(db, task)
    assert history == "" and "Inspecciona los archivos" in context
