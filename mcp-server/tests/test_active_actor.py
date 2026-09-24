"""Checks for actor ownership of live graph/night continuations."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from relay import coordination, server_graph_helpers, user_accounts
from relay.app_state import BG_TASKS_KEY, DB_KEY, GRAFOS_KEY, NIGHT_KEY


class _Db:
    async def get_task_graph(self, _graph_id):
        return {"id": "g1", "conversation_id": None}


@pytest.mark.asyncio
async def test_largar_grafo_hereda_actor_contextual_exacto(monkeypatch):
    db = _Db()
    app = {DB_KEY: db, GRAFOS_KEY: {}, BG_TASKS_KEY: set()}
    seen = {}

    async def runner():
        return None

    def spawn(_db, _project, coro):
        actor = user_accounts.current_actor.get()
        seen["actor"] = actor[1] if actor else None
        return asyncio.create_task(coro)

    monkeypatch.setattr(server_graph_helpers, "_correr_grafo_bg", lambda *_: runner())
    monkeypatch.setattr(coordination, "spawn_workspace", spawn)
    with user_accounts.bind_actor(db, "alex@example.test"):
        server_graph_helpers._largar_grafo(app, {"slug": "demo"}, "g1")
    task = app[GRAFOS_KEY]["g1"]
    await task
    assert task.relay_actor == "alex@example.test"
    assert seen["actor"] == "alex@example.test"


@pytest.mark.asyncio
@pytest.mark.parametrize("active_actor", ["alex@example.test", "sam@example.test", None])
@pytest.mark.parametrize("graph_id_in_question", [True, False])
async def test_graph_answer_checks_actor_before_persisting(monkeypatch, active_actor, graph_id_in_question):
    from relay import identity, orquestador, server_common, server_graph_routes
    question = {"task_id": "t1", "options": [{"key": "retry", "label": "Reintentar"}]}
    if graph_id_in_question:
        question["graph_id"] = "g1"
    db = SimpleNamespace(
        get_expert_question=AsyncMock(return_value={"kind": "grafo" if graph_id_in_question else "experto",
            "question_json": json.dumps(question), "conversation_id": "c1", "chat_id": "chat1"}),
        active_task_graph=AsyncMock(return_value={"id": "g1"}),
        answer_expert_question=AsyncMock(return_value=True),
        get_task_graph=AsyncMock(return_value={"id": "g1", "project_slug": "demo"}),
        get_project=AsyncMock(return_value={"slug": "demo"}))
    transition = AsyncMock(return_value="retry")
    resume = AsyncMock(return_value="g1")
    monkeypatch.setattr(orquestador, "aplicar_respuesta", transition)
    monkeypatch.setattr(orquestador, "responder_a_la_tarea", resume)
    monkeypatch.setattr(server_common, "_check_auth", lambda request: True)
    monkeypatch.setattr(identity, "requester", lambda request: "alex@example.test")
    request = SimpleNamespace(app={DB_KEY: db, GRAFOS_KEY: {"g1": SimpleNamespace(relay_actor=active_actor)}},
        match_info={"q_id": "q1"}, json=AsyncMock(return_value={"choice": "retry"}))
    response = await server_graph_routes.expert_question_answer(request)
    if active_actor == "alex@example.test":
        assert response.status == 200
        db.answer_expert_question.assert_awaited_once()
        (transition if graph_id_in_question else resume).assert_awaited_once()
        assert json.loads(response.text)["grafo"]["corriendo"] is True
    else:
        assert response.status == 409
        db.answer_expert_question.assert_not_awaited()
        transition.assert_not_awaited()
        resume.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("active_actor", ["alex@example.test", "sam@example.test", None])
async def test_night_answer_checks_actor_before_persisting(monkeypatch, active_actor):
    from relay import identity, server_common, server_questions
    db = SimpleNamespace(get_night_question=AsyncMock(return_value={"run_id": "n1"}),
                         answer_night_question=AsyncMock(return_value=True))
    monkeypatch.setattr(server_common, "_check_auth", lambda request: True)
    monkeypatch.setattr(identity, "requester", lambda request: "alex@example.test")
    request = SimpleNamespace(app={DB_KEY: db, NIGHT_KEY: {"n1": (SimpleNamespace(relay_actor=active_actor), None)}},
        match_info={"q_id": "q1"}, json=AsyncMock(return_value={"choice": "continue"}))
    response = await server_questions.night_question_answer(request)
    if active_actor == "alex@example.test":
        assert response.status == 200
        db.answer_night_question.assert_awaited_once()
    else:
        assert response.status == 409
        db.answer_night_question.assert_not_awaited()
