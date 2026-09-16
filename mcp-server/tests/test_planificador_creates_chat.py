"""El planificador debe dejar rastro de que está trabajando (2026-09-06).

Bug observado: `planificador.armar_grafo` tarda >3 min en la cascada de
modelos y no creaba ninguna fila intermedia — ni chat, ni grafo, ni
progreso. Desde afuera era indistinguible de un cuelgue. Por ese
silencio se llegó a lanzar un grafo DUPLICADO sobre el mismo repo: se
consultó la base, no había nada, y se asumió que la primera llamada
había fallado.

Hoy (sin el parche) `armar_grafo` no crea ningún chat; el test falla en
el `assert db.create_chat.called`. Con el parche, abre su chat al
arrancar (mismo patrón que `orquestador.ejecutor_minimax`) y lo cierra
al terminar.
"""
from __future__ import annotations

import json

import pytest

from relay import planificador
from relay.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    await d.init_schema()
    return d


def _json(tareas):
    return json.dumps({"tareas": tareas}, ensure_ascii=False)


class _Salida:
    def __init__(self, texto):
        self.output = texto


def _modelo_que_dice(*respuestas):
    quedan = list(respuestas)

    class _Agente:
        def __init__(self, *a, **kw):
            pass

        async def run(self, prompt, **kw):
            _Agente.ultimo_kw = kw
            return _Salida(quedan.pop(0))

    return _Agente


class _DbSpy:
    """Envuelve el Database real y registra el orden de las llamadas.

    Solo miramos las tres que nos importan: `create_chat`, `create_task_graph`,
    `finish_chat`. El resto se delega sin tocar.
    """

    def __init__(self, real: Database):
        self._real = real
        self.create_chat_calls: list[dict] = []
        self.finish_chat_calls: list[tuple[str, dict]] = []
        self.create_task_graph_calls: list[str] = []
        # orden: ("create_chat",), ("finish_chat",), ("create_task_graph",)
        self.orden: list[tuple[str, ...]] = []

    async def create_chat(self, **kwargs):
        self.create_chat_calls.append(kwargs)
        self.orden.append(("create_chat",))
        return await self._real.create_chat(**kwargs)

    async def finish_chat(self, chat_id, **kwargs):
        self.finish_chat_calls.append((chat_id, kwargs))
        self.orden.append(("finish_chat",))
        return await self._real.finish_chat(chat_id, **kwargs)

    async def create_task_graph(self, graph_id, *args, **kwargs):
        self.create_task_graph_calls.append(graph_id)
        self.orden.append(("create_task_graph",))
        return await self._real.create_task_graph(graph_id, *args, **kwargs)

    async def get_chat(self, chat_id):
        return await self._real.get_chat(chat_id)

    def __getattr__(self, name):
        return getattr(self._real, name)


async def test_armar_grafo_crea_un_chat_inmediatamente(db, monkeypatch):
    """La fila del chat debe existir ANTES de los 3+ min de la cascada."""
    from relay import experts

    agente = _modelo_que_dice(_json([
        {"id": "t1", "titulo": "Leer"},
        {"id": "t2", "titulo": "Escribir", "deps": ["t1"]},
    ]))
    monkeypatch.setattr(experts, "Agent", agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: object())

    spy = _DbSpy(db)
    g = await planificador.armar_grafo(
        {"slug": "demo"}, "hacé todo", db=spy, conversation_id="conv1")

    # 1. se llamó a db.create_chat exactamente una vez, con project_slug
    #    coherente, ANTES de create_task_graph (es decir, antes de la
    #    cascada de modelos de >3 min).
    assert len(spy.create_chat_calls) == 1, (
        "armar_grafo no abrió ningún chat; desde afuera parece colgado")
    cc = spy.create_chat_calls[0]
    assert cc["project_slug"] == "demo"
    assert cc["source"] == "planificador"
    assert cc["conversation_id"] == "conv1"

    create_chat_idx = spy.orden.index(("create_chat",))
    create_task_idx = spy.orden.index(("create_task_graph",))
    assert create_chat_idx < create_task_idx, (
        "create_chat debe ocurrir ANTES de create_task_graph, no después")

    # 2. el chat quedó en la base y es consultable vía get_chat.
    assert spy.finish_chat_calls, "el chat debió cerrarse al final"
    chat_id = spy.finish_chat_calls[0][0]
    fila = await spy.get_chat(chat_id)
    assert fila is not None
    assert fila["project_slug"] == "demo"
    assert fila["status"] == "ok"
    assert fila["model"] == planificador.cascada_planner({"slug": "demo"})[0]

    # 3. igual que antes: el grafo se creó bien, no rompimos el flujo.
    assert g["objetivo"] == "hacé todo"
    assert g["project_slug"] == "demo"
    assert len(g["tasks"]) == 2
