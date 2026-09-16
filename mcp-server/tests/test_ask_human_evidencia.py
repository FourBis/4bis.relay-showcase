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

import json
import tempfile
from unittest.mock import patch

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
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
    assert experts._evidencia_insuficiente("") is not None
    assert experts._evidencia_insuficiente("   ") is not None


def test_evidencia_muy_corta_rechaza():
    assert experts._evidencia_insuficiente("está bien") is not None


def test_evidencia_sin_archivo_rechaza():
    motivo = experts._evidencia_insuficiente(
        "revisé el código y las pruebas pasan, todo funciona correctamente "
        "según lo esperado")
    assert motivo is not None


def test_evidencia_pura_especulacion_rechaza():
    motivo = experts._evidencia_insuficiente(
        "asumo que aparentemente supongo que no leí el resto del código "
        "pero creo que está bien igual, sin dudas")
    assert motivo is not None


def test_evidencia_con_archivo_concreto_pasa():
    assert experts._evidencia_insuficiente(
        "leí Service/Foo.cs y el método Bar no valida null antes de "
        "usarlo, por eso tira NullReferenceException") is None


def test_un_archivo_pegado_a_nada_no_alcanza():
    """El relleno que SI cumple la forma: cita un archivo y no dice nada.

    Medido el 2026-09-06 con el piso viejo de 20: "Lei Foo.cs y esta
    mal" pasaba. Cumple el requisito (hay un nombre con extension) y
    deja al humano exactamente donde estaba: teniendo que abrir el repo
    para poder contestar. El piso de largo es lo que lo ataja.
    """
    assert experts._evidencia_insuficiente("leí Foo.cs y está mal") is not None
    assert experts._evidencia_insuficiente(
        "revisé el archivo config.json y todo parece estar bien") is not None


def test_evidencia_caso_real_con_hedge_y_archivo_pasa():
    """El caso real del pedido: un hedge NO invalida si cita el archivo
    donde de verdad está ese hedge — reportarlo es evidencia legítima."""
    assert experts._evidencia_insuficiente(CASO_REAL) is None


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
    with patch.object(experts, "build_model", lambda s: FunctionModel(act)), \
         patch.object(experts, "cbm_binary_path", lambda: None):
        await experts.run_expert(
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
        return ModelResponse(parts=[ToolCallPart("ask_human", {
            "pregunta": "¿el inventario BUG-S-15..21 sigue vigente?",
            "evidencia": CASO_REAL})])

    proj = _project(str(tempfile.mkdtemp()))
    with patch.object(experts, "build_model", lambda s: FunctionModel(act)), \
         patch.object(experts, "cbm_binary_path", lambda: None):
        await experts.run_expert(
            proj, "hola", db=db, model_override="minimax:MiniMax-M3",
            chat_id="chat-con-evidencia", conversation_id="conv-con-evidencia")

    abiertas = await db.list_expert_questions(
        conversation_id="conv-con-evidencia")
    assert len(abiertas) == 1
    q = json.loads(abiertas[0]["question_json"])
    # Sin el parche, la clave "evidencia" ni siquiera se guarda en `q`.
    assert "NotificableAttribute.cs" in q["evidencia"]
