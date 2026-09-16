"""B2: la fila de la tarea debe tener `chat_id` desde el arranque (2026-09-06).

Bug observado: en `orquestador.ejecutor_minimax` el nodo hace
`chat_id = await db.create_chat(...)` ANTES de ejecutar, pero la fila de
la tarea no quedaba asociada al chat hasta el cierre. Justo durante la
ventana en que uno querría mirar el trabajo en vuelo -- saltar del nodo
al chat desde el panel, ver qué está haciendo el bot en tiempo real --
no se podía: la FK lógica estaba vacía.

Medición del caso real: un nodo consumió 790.142 tokens en 61 tool
calls; mientras corría, `tasks.chat_id` era NULL y el chat correspondiente
ya tenía 161 eventos en `chats.progress_events`. La asociación recién se
escribía al cerrar la tarea -- cuando ya no sirve para mirar in-flight.

Este test cubre la `ejecutar` interna real (la que arma `correr_grafo`
vía `ejecutor_minimax`) con `experts.run_expert` mockeado: lo único
externo. Lo que verifica es que, en el momento en que `run_expert`
empieza a correr, la fila de la tarea YA tiene escrito el `chat_id`
que el nodo está usando, y que la asociación se escribió ANTES de
la ejecución -- no durante el cierre.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "mcp-server" / "src"))

from relay import orquestador  # noqa: E402
from relay.db import Database  # noqa: E402


@pytest.fixture
async def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    await d.init_schema()
    return d


async def _tarea(db, task_id: str) -> dict:
    """La fila de UNA tarea. `Database` solo expone `list_tasks` por grafo."""
    for t in await db.list_tasks("g"):
        if t["id"] == task_id:
            return t
    raise AssertionError(f"no existe la tarea {task_id}")


async def test_la_tarea_tiene_chat_id_antes_de_ejecutar_el_nodo(db, tmp_path):
    # Un proyecto y un grafo con un solo nodo: alcanza para cubrir la línea.
    await db.upsert_project({
        "slug": "inventorydemo", "name": "INVENTORYDEMO",
        "repo_path": str(tmp_path),
    })
    await db.create_task_graph(
        "g", "objetivo",
        tareas=[{"id": "t1", "titulo": "uno", "idempotente": True}],
        project_slug="inventorydemo",
    )

    proyecto = await db.get_project("inventorydemo")
    grafo_row = await db.get_task_graph("g")
    ejecutar = orquestador.ejecutor_minimax(db, proyecto, grafo_row)

    # Lo que vio `run_expert` cuando empezó a correr: el chat_id real.
    visto_en_run_expert: dict = {}

    async def fake_run_expert(*args, **kwargs):
        # run_expert recibe el `prompt` armado por el orquestador; el
        # chat_id no está en los args, está en la fila de la tarea. Lo
        # leemos acá, en el momento en que "el nodo empezó a ejecutar".
        visto_en_run_expert["chat_id"] = (
            await _tarea(db, "t1")
        ).get("chat_id") or ""
        return {
            "ok": True,
            "resultado": "hecho",
            "modelo": "minimax:MiniMax-M3",
        }

    with patch("relay.experts.run_expert", side_effect=fake_run_expert):
        out = await ejecutar({
            "id": "t1", "titulo": "uno", "deps": [],
            "graph_id": "g",
        })

    assert out["ok"] is True
    assert visto_en_run_expert["chat_id"], (
        "el nodo arrancó sin chat_id en la fila de la tarea: el panel "
        "no podría saltar del nodo al chat mientras corre"
    )

    # Tras cerrar, la fila debe seguir teniendo ese mismo chat_id.
    fila_al_final = await _tarea(db, "t1")
    assert fila_al_final["chat_id"] == visto_en_run_expert["chat_id"], (
        "el chat_id que tenía la fila durante la ejecución no es el que "
        "queda al cerrar: la asociación temprana se perdió"
    )

    # Y el chat_id escrito durante el arranque tiene que coincidir con
    # el de la fila final (idempotencia: la asignación al cerrar lo
    # reescribe con el mismo valor, no rompe nada).
    assert fila_al_final["chat_id"]


async def test_la_asociacion_se_escribe_antes_de_run_expert(db, tmp_path):
    """El update del chat_id tiene que ocurrir ANTES de que arranque el
    nodo, no después -- si se hace durante el cierre, mirar in-flight
    sigue sin servir aunque la fila termine bien al final.
    """
    await db.upsert_project({
        "slug": "inventorydemo", "name": "INVENTORYDEMO",
        "repo_path": str(tmp_path),
    })
    await db.create_task_graph(
        "g", "objetivo",
        tareas=[{"id": "t1", "titulo": "uno", "idempotente": True}],
        project_slug="inventorydemo",
    )

    proyecto = await db.get_project("inventorydemo")
    grafo_row = await db.get_task_graph("g")
    ejecutar = orquestador.ejecutor_minimax(db, proyecto, grafo_row)

    chat_id_al_arrancar = {"valor": ""}
    async def fake_run_expert(*args, **kwargs):
        # Mientras el nodo está "corriendo", la fila ya tiene que tener
        # el chat_id escrito. Si está vacío, la asociación temprana
        # nunca ocurrió.
        fila = await _tarea(db, "t1")
        chat_id_al_arrancar["valor"] = fila.get("chat_id") or ""
        return {"ok": True, "resultado": "ok", "modelo": "minimax:MiniMax-M3"}

    with patch("relay.experts.run_expert", side_effect=fake_run_expert):
        await ejecutar({
            "id": "t1", "titulo": "uno", "deps": [],
            "graph_id": "g",
        })

    assert chat_id_al_arrancar["valor"], (
        "en el momento en que run_expert arrancó, la fila de la tarea "
        "no tenía chat_id escrito: la asociación se hace al cerrar, no "
        "al crear el chat -- justo lo que rompe la observación in-flight"
    )
