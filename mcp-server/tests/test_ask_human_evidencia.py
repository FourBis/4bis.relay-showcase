"""`ask_human` exige evidencia (2026-09-06).

Se acumularon 11 preguntas sin responder (la más vieja de 3 semanas): el
humano no las contestaba porque verificar la afirmación costaba casi lo
mismo que hacer el trabajo — la pregunta decía una conclusión ("el
inventario está desactualizado") sin decir qué archivo leyó para llegar
ahí. `_evidencia_insuficiente` es el guard que lo obliga.

Cubre:
  - la validación pura (`_evidencia_insuficiente`): vacía, sin archivo,
    con archivo, y el caso real de un hedge ("asumo, no leí el resto")
    que SÍ cita un archivo y tiene que pasar.
  - el wiring en `ask_human`: evidencia insuficiente dispara `ModelRetry`
    (el run sigue vivo, el modelo puede corregir); evidencia válida queda
    guardada en la pregunta persistida.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_ask_human_evidencia.py -q
"""
from __future__ import annotations
from relay import cbm_runtime, expert_evidence, expert_models, expert_runner

import json
import tempfile
from unittest.mock import patch

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from relay import experts
from relay.db import Database

# El caso real que motivó el guard (ver docstring del módulo): el
# inventario reconoce su propio hueco, y ESO es evidencia legítima aunque
# esté cargado de hedges.
CASO_REAL = (
    "leí Service/Notifications/NotificableAttribute.cs: tiene el "
    "namespace anidado; el inventario dice textual 'asumo, no leí el "
    "resto'"
)


@pytest.fixture
async def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    await d.init_schema()
    return d


# ---------- validación pura ----------


def test_evidencia_vacia_rechaza():
    assert expert_evidence._evidencia_insuficiente("") is not None
    assert expert_evidence._evidencia_insuficiente("   ") is not None


def test_evidencia_muy_corta_rechaza():
    assert expert_evidence._evidencia_insuficiente("está bien") is not None


def test_evidencia_sin_archivo_rechaza():
    motivo = expert_evidence._evidencia_insuficiente(
        "revisé el código y las pruebas pasan, todo funciona correctamente "
        "según lo esperado")
    assert motivo is not None


def test_evidencia_pura_especulacion_rechaza():
    motivo = expert_evidence._evidencia_insuficiente(
        "asumo que aparentemente supongo que no leí el resto del código "
        "pero creo que está bien igual, sin dudas")
    assert motivo is not None


def test_evidencia_con_archivo_concreto_pasa():
    assert expert_evidence._evidencia_insuficiente(
        "leí Service/Foo.cs y el método Bar no valida null antes de "
        "usarlo, por eso tira NullReferenceException") is None


def test_un_archivo_pegado_a_nada_no_alcanza():
    """El relleno que SI cumple la forma: cita un archivo y no dice nada.

    Medido el 2026-09-06 con el piso viejo de 20: "Lei Foo.cs y esta
    mal" pasaba. Cumple el requisito (hay un nombre con extension) y
    deja al humano exactamente donde estaba: teniendo que abrir el repo
    para poder contestar. El piso de largo es lo que lo ataja.
    """
    assert expert_evidence._evidencia_insuficiente("leí Foo.cs y está mal") is not None
    assert expert_evidence._evidencia_insuficiente(
        "revisé el archivo config.json y todo parece estar bien") is not None


def test_evidencia_caso_real_con_hedge_y_archivo_pasa():
    """El caso real del pedido: un hedge NO invalida si cita el archivo
    donde de verdad está ese hedge — reportarlo es evidencia legítima."""
    assert expert_evidence._evidencia_insuficiente(CASO_REAL) is None


# ---------- wiring de la tool ----------


def _project(repo_path: str) -> dict:
    return {"slug": "demo", "repo_path": repo_path, "system_prompt": "p",
            "mcp_servers": [], "native_tools": [], "defaults_json": {}}


async def test_ask_human_sin_evidencia_reintenta_y_no_registra_pregunta(db):
    """Evidencia insuficiente: `ModelRetry` corrige al modelo en vivo, la
    pregunta NO queda registrada hasta que trae una evidencia válida."""
    calls = {"n": 0}

    def act(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            return ModelResponse(parts=[ToolCallPart("ask_human", {
                "pregunta": "¿sigo con la migración?",
                "evidencia": "creo que aparentemente está bien"})])
        return ModelResponse(parts=[TextPart("listo, quedé esperando")])

    proj = _project(str(tempfile.mkdtemp()))
    await db.create_conversation(
        project_slug="demo", conversation_id="conv-sin-evidencia")
    with patch.object(expert_models, "build_model", lambda s: FunctionModel(act)), \
         patch.object(cbm_runtime, "cbm_binary_path", lambda: None):
        await expert_runner.run_expert(
            proj, "hola", db=db, model_override="minimax:MiniMax-M3",
            chat_id="chat-sin-evidencia", conversation_id="conv-sin-evidencia")

    # Sin el parche, `ask_human` no tiene `evidencia` obligatoria: acepta
    # la primera llamada tal cual y este assert falla (llama una sola vez).
    assert calls["n"] == 2, "el ModelRetry tiene que forzar un segundo intento"
    abiertas = await db.list_expert_questions(
        conversation_id="conv-sin-evidencia")
    assert abiertas == []


async def test_ask_human_con_evidencia_valida_la_persiste(db):
    """Evidencia que cumple el mínimo: la pregunta se registra Y la
    evidencia queda adentro para que quien responda no tenga que
    reabrir el repo."""
    def act(messages, info):
        if any(isinstance(part, ToolReturnPart)
               for message in messages for part in message.parts):
            return ModelResponse(parts=[TextPart("Espero la decisión del usuario.")])
        return ModelResponse(parts=[ToolCallPart("ask_human", {
            "pregunta": "¿el inventario BUG-S-15..21 sigue vigente?",
            "evidencia": CASO_REAL})])

    proj = _project(str(tempfile.mkdtemp()))
    await db.create_conversation(
        project_slug="demo", conversation_id="conv-con-evidencia")
    with patch.object(expert_models, "build_model", lambda s: FunctionModel(act)), \
         patch.object(cbm_runtime, "cbm_binary_path", lambda: None):
        await expert_runner.run_expert(
            proj, "hola", db=db, model_override="minimax:MiniMax-M3",
            chat_id="chat-con-evidencia", conversation_id="conv-con-evidencia")

    abiertas = await db.list_expert_questions(
        conversation_id="conv-con-evidencia")
    assert len(abiertas) == 1
    q = json.loads(abiertas[0]["question_json"])
    # Sin el parche, la clave "evidencia" ni siquiera se guarda en `q`.
    assert "NotificableAttribute.cs" in q["evidencia"]


@pytest.mark.parametrize("batch", [False, True])
async def test_pregunta_persistida_detiene_modelo_y_tools(db, tmp_path, monkeypatch, batch):
    import asyncio
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    from pydantic_ai.toolsets import FunctionToolset
    from relay import account_tools, expert_selection

    effects, requests = [], []

    async def action(label: str) -> str:
        await asyncio.sleep(0)
        effects.append(label)
        return label

    async def external() -> str:
        effects.append("external")
        return "external"

    monkeypatch.setattr(account_tools, "account_tools", lambda *a, **kw: [action])
    async def catalog(*args):
        return [FunctionToolset([external])], [], []
    monkeypatch.setattr(expert_selection, "_catalog_toolsets", catalog)
    question = {"pregunta": "¿Continúo con la migración?", "evidencia": CASO_REAL}

    def act(messages, info):
        requests.append(messages)
        # Acotado incluso sin el guard: la segunda petición evidencia el fallo.
        if len(requests) > 1:
            return ModelResponse(parts=[TextPart("Continué sin esperar.")])
        parts = [ToolCallPart("ask_human", question, tool_call_id="question")]
        if batch:
            parts = [ToolCallPart("action", {"label": "before"}, tool_call_id="before"),
                     *parts,
                     ToolCallPart("action", {"label": "after"}, tool_call_id="after"),
                     ToolCallPart("external", {}, tool_call_id="external"),
                     ToolCallPart("ask_human", question, tool_call_id="duplicate")]
        return ModelResponse(parts=parts)

    project = {**_project(str(tmp_path)), "id": 1}
    await db.create_conversation(project_slug="demo", conversation_id="pause")
    monkeypatch.setattr(expert_models, "build_model", lambda _: FunctionModel(act))
    monkeypatch.setattr(cbm_runtime, "cbm_binary_path", lambda: None)
    nudges = []
    async def progress(**event):
        if event.get("phase") == "question":
            nudges.append("Continúa investigando")
    result = await expert_runner.run_expert(
        project, "hola", db=db, model_override="minimax:MiniMax-M3",
        chat_id="pause", conversation_id="pause", steer=nudges, on_progress=progress)

    assert effects == (["before"] if batch else [])
    assert len(requests) == 1
    questions = await db.list_expert_questions(conversation_id="pause")
    assert len(questions) == 1
    assert result["question_id"] == questions[0]["id"]
    assert result["phase_at_end"] == "question"
    assert "migración" in result["content"]
    assert result["legs"] == 1 and result["steers"] == 0
    history = ModelMessagesTypeAdapter.validate_json(result["messages_json"])
    calls = [part for message in history for part in message.parts if isinstance(part, ToolCallPart)]
    returns = [part for message in history for part in message.parts if isinstance(part, ToolReturnPart)]
    assert {part.tool_call_id for part in calls} == {part.tool_call_id for part in returns}
    assert any(result["question_id"] in str(part.content) for part in returns)
    if batch:
        assert all("no ejecutada" in part.content.lower() for part in returns
                   if part.tool_call_id in {"after", "external", "duplicate"})
    assert result["tokens_in"] and result["tokens_out"]

    # El historial cerrado admite la respuesta humana sin repetir las tools.
    monkeypatch.setattr(expert_models, "build_model", lambda _: FunctionModel(
        lambda messages, info: ModelResponse(parts=[TextPart("Respuesta recibida.")])))
    resumed = await expert_runner.run_expert(
        project, "Puedes continuar", db=db, model_override="minimax:MiniMax-M3",
        chat_id="resumed", conversation_id="pause", message_history_json=result["messages_json"])
    assert resumed["content"] == "Respuesta recibida."
    assert resumed["question_id"] == ""
    assert effects == (["before"] if batch else [])


async def test_fallo_al_guardar_pregunta_no_inventa_pausa(db, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(db, "create_expert_question", AsyncMock(side_effect=OSError("db unavailable")))
    calls = []
    def act(messages, info):
        calls.append(1)
        if len(calls) == 1:
            return ModelResponse(parts=[ToolCallPart("ask_human", {
                "pregunta": "¿Continúo?", "evidencia": CASO_REAL})])
        return ModelResponse(parts=[TextPart("No pude registrar la consulta.")])
    monkeypatch.setattr(expert_models, "build_model", lambda _: FunctionModel(act))
    monkeypatch.setattr(cbm_runtime, "cbm_binary_path", lambda: None)
    result = await expert_runner.run_expert(
        _project(str(tmp_path)), "hola", db=db, model_override="minimax:MiniMax-M3", chat_id="failure")
    assert len(calls) == 2
    assert result["question_id"] == ""
    assert result["phase_at_end"] != "question"


async def test_cancelar_tras_guardar_pregunta_conserva_historial_sin_mas_efectos(db, tmp_path, monkeypatch):
    import asyncio
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    from relay import account_tools

    saved, effects, rescue = asyncio.Event(), [], {}
    async def action() -> str:
        effects.append("executed")
        return "executed"
    monkeypatch.setattr(account_tools, "account_tools", lambda *a, **kw: [action])
    def act(messages, info):
        return ModelResponse(parts=[
            ToolCallPart("ask_human", {"pregunta": "¿Continúo?", "evidencia": CASO_REAL}, tool_call_id="ask"),
            ToolCallPart("action", {}, tool_call_id="after")])
    monkeypatch.setattr(expert_models, "build_model", lambda _: FunctionModel(act))
    monkeypatch.setattr(cbm_runtime, "cbm_binary_path", lambda: None)
    async def progress(**event):
        if event.get("phase") == "question":
            saved.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(expert_runner.run_expert(
        _project(str(tmp_path)), "hola", db=db, model_override="minimax:MiniMax-M3",
        chat_id="cancel", rescue=rescue, on_progress=progress))
    try:
        await asyncio.wait_for(saved.wait(), 15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert not effects
    assert len(await db.list_expert_questions(chat_id="cancel")) == 1
    history = ModelMessagesTypeAdapter.validate_json(rescue["messages_json"])
    calls = {p.tool_call_id for m in history for p in m.parts if isinstance(p, ToolCallPart)}
    returns = {p.tool_call_id for m in history for p in m.parts if isinstance(p, ToolReturnPart)}
    assert calls == returns == {"ask", "after"}
