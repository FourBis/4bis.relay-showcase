"""Persistencia del grafo de tareas (F1, 2026-08-17).

Lo que estos tests cuidan, además del CRUD: que el grafo **sobreviva a
un reinicio**. Ese es el punto de que el plan deje de ser un string —
antes, si el relay se caía a mitad de un proceso largo, el plan
desaparecía con el proceso y no había forma de retomar sin rehacerlo
todo.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from relay import grafo
from relay.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    await d.init_schema()
    return d


TAREAS = [
    {"id": "t1", "titulo": "leer el esquema", "idempotente": True},
    {"id": "t2", "titulo": "migrar", "deps": ["t1"]},
    {"id": "t3", "titulo": "cargar datos", "deps": ["t2"]},
    {"id": "t4", "titulo": "documentar", "deps": ["t1"], "idempotente": True},
]


async def test_guardar_y_leer_con_sus_aristas(db):
    g = await db.create_task_graph("g_1", "arreglar el deploy", tareas=TAREAS,
                                   conversation_id="conv-1")
    assert g["objetivo"] == "arreglar el deploy"
    assert len(g["tasks"]) == 4
    por_id = {t["id"]: t for t in g["tasks"]}
    assert por_id["t3"]["deps"] == ["t2"]
    assert por_id["t1"]["deps"] == []
    assert por_id["t1"]["idempotente"] == 1


async def test_un_grafo_invalido_no_se_guarda_a_medias(db):
    """Validar ANTES de escribir: un grafo a medio guardar se descubre
    tres nodos después, con trabajo hecho encima."""
    with pytest.raises(grafo.GrafoInvalido):
        await db.create_task_graph("g_malo", "x", tareas=[
            {"id": "a", "titulo": "a", "deps": ["b"]},
            {"id": "b", "titulo": "b", "deps": ["a"]}])
    assert await db.get_task_graph("g_malo") is None
    assert await db.list_tasks("g_malo") == []


async def test_el_grafo_sobrevive_al_reinicio(db, tmp_path):
    """Una Database nueva sobre el mismo archivo ve todo, con estados."""
    await db.create_task_graph("g_2", "objetivo", tareas=TAREAS,
                               conversation_id="conv-2")
    await db.update_task("t1", estado=grafo.HECHO, resultado="leí 12 tablas")

    otra = Database(path=tmp_path / "test.db")
    await otra.init_schema()
    g = await otra.active_task_graph("conv-2")
    assert g["id"] == "g_2"
    por_id = {t["id"]: t for t in g["tasks"]}
    assert por_id["t1"]["estado"] == grafo.HECHO
    assert por_id["t1"]["resultado"] == "leí 12 tablas"


async def test_el_ciclo_completo_de_ejecucion(db):
    """De punta a punta con la lógica pura decidiendo qué sigue."""
    await db.create_task_graph("g_3", "objetivo", tareas=TAREAS,
                               conversation_id="conv-3")

    def nodos(g):
        return [grafo.Nodo.desde_fila(t, t["deps"]) for t in g["tasks"]]

    g = await db.get_task_graph("g_3")
    # Al principio solo t1: los demás dependen de él.
    assert [x.id for x in grafo.listas(nodos(g))] == ["t1"]

    await db.update_task("t1", estado=grafo.HECHO)
    g = await db.get_task_graph("g_3")
    # Ahora t2 y t4 en paralelo — el punto del DAG.
    assert [x.id for x in grafo.listas(nodos(g))] == ["t2", "t4"]

    # t2 falla y arrastra a t3, pero t4 no se toca.
    await db.update_task("t2", estado=grafo.FALLADO, error="permiso denegado")
    g = await db.get_task_graph("g_3")
    for tid in grafo.bloqueados_por(nodos(g), "t2"):
        await db.update_task(tid, estado=grafo.BLOQUEADO)
    g = await db.get_task_graph("g_3")
    por_id = {t["id"]: t for t in g["tasks"]}
    assert por_id["t3"]["estado"] == grafo.BLOQUEADO
    assert por_id["t4"]["estado"] == grafo.PENDIENTE
    assert [x.id for x in grafo.listas(nodos(g))] == ["t4"]

    # Con t4 hecho el grafo queda fallado, no "activo para siempre".
    await db.update_task("t4", estado=grafo.HECHO)
    g = await db.get_task_graph("g_3")
    assert grafo.estado_del_grafo(nodos(g)) == "fallado"


async def test_update_task_rechaza_columnas_desconocidas(db):
    """El orquestador escribe acá con datos que vienen de un modelo."""
    await db.create_task_graph("g_4", "x", tareas=TAREAS)
    with pytest.raises(ValueError):
        await db.update_task("t1", graph_id="otro")


async def test_active_task_graph_ignora_los_cerrados(db):
    await db.create_task_graph("g_5", "viejo", tareas=TAREAS,
                               conversation_id="conv-5")
    await db.set_task_graph_state("g_5", "hecho")
    assert await db.active_task_graph("conv-5") is None


async def test_borrar_el_grafo_se_lleva_tareas_y_aristas(db):
    """FK ON DELETE CASCADE: sin esto quedan tareas huérfanas que la UI
    mostraría sin poder decir de qué plan son."""
    await db.create_task_graph("g_6", "x", tareas=TAREAS)
    await db.run("DELETE FROM task_graphs WHERE id='g_6'")
    assert await db.run("SELECT * FROM tasks WHERE graph_id='g_6'") == []
    assert await db.run("SELECT * FROM task_deps WHERE graph_id='g_6'") == []


# ---------- Fase 2A: agregar tareas a un grafo en vuelo (2026-09-02) ----------


async def test_agregar_tareas_a_un_grafo_existente(db):
    """`orquestador._vueltas` relee el grafo cada vuelta: alcanza con
    que las nuevas queden en la base para que se levanten solas."""
    await db.create_task_graph("g_10", "x", tareas=TAREAS)
    agregados = await db.add_tasks_to_graph("g_10", [
        {"id": "s1", "titulo": "sub 1"},
        {"id": "s2", "titulo": "sub 2", "deps": ["s1"]},
        {"id": "s3", "titulo": "sub 3", "deps": ["s1"]},
    ])
    assert agregados == ["s1", "s2", "s3"]
    g = await db.get_task_graph("g_10")
    por_id = {t["id"]: t for t in g["tasks"]}
    assert {"s1", "s2", "s3"} <= por_id.keys()
    assert por_id["s2"]["deps"] == ["s1"]


async def test_reemplaza_reapunta_dependientes_y_hereda_deps(db):
    """El caso central: X se subdivide en 3, Y (que dependía de X) pasa
    a depender de las 3, y las 3 heredan las deps que tenía X."""
    await db.create_task_graph("g_11", "x", tareas=[
        {"id": "t1", "titulo": "base"},
        {"id": "t2", "titulo": "el que se subdivide", "deps": ["t1"]},
        {"id": "t3", "titulo": "depende de t2", "deps": ["t2"]},
    ])
    await db.add_tasks_to_graph("g_11", [
        {"id": "t2a", "titulo": "sub a"},
        {"id": "t2b", "titulo": "sub b"},
        {"id": "t2c", "titulo": "sub c"},
    ], reemplaza="t2")

    g = await db.get_task_graph("g_11")
    por_id = {t["id"]: t for t in g["tasks"]}
    # t3 ya NO depende de t2, depende de las 3 subtareas.
    assert "t2" not in por_id["t3"]["deps"]
    assert por_id["t3"]["deps"] == ["t2a", "t2b", "t2c"]
    # Las 3 heredan la dep original de t2 (t1), para no arrancar antes.
    for sub in ("t2a", "t2b", "t2c"):
        assert por_id[sub]["deps"] == ["t1"]
    # t2 sigue ahí, intacto — esta función no decide qué pasa con él.
    assert "t2" in por_id


async def test_parent_id_se_guarda_y_vuelve(db):
    await db.create_task_graph("g_12", "x", tareas=TAREAS)
    await db.add_tasks_to_graph("g_12", [
        {"id": "s1", "titulo": "sub 1"},
    ], reemplaza="t1")
    g = await db.get_task_graph("g_12")
    por_id = {t["id"]: t for t in g["tasks"]}
    assert por_id["s1"]["parent_id"] == "t1"
    assert por_id["t1"]["parent_id"] is None


async def test_split_completo_cierra_el_grafo_sin_borrar_el_fallo_original(db):
    from relay.orquestador import correr_grafo

    await db.create_task_graph("g_split", "terminar la tarea", tareas=[
        {"id": "original", "titulo": "original"},
        {"id": "despues", "titulo": "despues", "deps": ["original"]},
    ])
    await db.update_task("original", estado=grafo.FALLADO,
                         error="subdividido en 2 subtareas: sub1, sub2",
                         resultado="trabajo parcial", chat_id="chat-original")
    await db.add_tasks_to_graph("g_split", [
        {"id": "sub1", "titulo": "primera mitad"},
        {"id": "sub2", "titulo": "segunda mitad", "deps": ["sub1"]},
    ], reemplaza="original")
    ejecutadas = []

    async def ejecutar(tarea):
        ejecutadas.append(tarea["id"])
        return {"ok": True, "resultado": "trabajo terminado"}

    prog = await correr_grafo(db, "g_split", ejecutar=ejecutar)
    assert ejecutadas == ["sub1", "sub2", "despues"]
    assert prog["estado"] == "hecho"
    assert prog["hechos"] == prog["total"] == 3
    assert prog["total_historico"] == 4
    assert prog["sustituidos"] == 1
    assert prog["fallados"] == 0
    g = await db.get_task_graph("g_split")
    assert g["estado"] == "hecho"
    original = next(t for t in g["tasks"] if t["id"] == "original")
    assert original["estado"] == grafo.FALLADO
    assert original["error"] == "subdividido en 2 subtareas: sub1, sub2"
    assert original["resultado"] == "trabajo parcial"
    assert original["chat_id"] == "chat-original"
    assert "verificacion_json" not in g or not g["verificacion_json"]


async def test_dep_a_id_inexistente_no_inserta_nada(db):
    await db.create_task_graph("g_13", "x", tareas=TAREAS)
    with pytest.raises(grafo.GrafoInvalido):
        await db.add_tasks_to_graph("g_13", [
            {"id": "s1", "titulo": "sub 1", "deps": ["no_existe"]},
        ])
    g = await db.get_task_graph("g_13")
    assert "s1" not in {t["id"] for t in g["tasks"]}
    assert len(g["tasks"]) == len(TAREAS)


async def test_ciclo_entre_dos_tareas_nuevas_no_inserta_nada(db):
    """Dos tareas del MISMO batch que se ciclan entre sí (nuevo↔nuevo)."""
    await db.create_task_graph("g_14", "x", tareas=[
        {"id": "t1", "titulo": "a"},
        {"id": "t2", "titulo": "b", "deps": ["t1"]},
    ])
    with pytest.raises(grafo.GrafoInvalido):
        await db.add_tasks_to_graph("g_14", [
            {"id": "t1_v2", "titulo": "choca", "deps": ["s_x"]},
            {"id": "s_x", "titulo": "x", "deps": ["t1_v2"]},
        ])
    g = await db.get_task_graph("g_14")
    assert len(g["tasks"]) == 2


async def test_ciclo_creado_por_el_reapunte_de_reemplaza_no_inserta_nada(db):
    """El caso viejo↔nuevo real: el ciclo no está en los datos que
    llegan, lo CREA el reapunte de `reemplaza`.

    `t1 → t2 → t3`. Subdividir t2 en `s1` (que depende de `t3`) reapunta
    a t3 como dependiente de t2 hacia s1 — pero s1 YA depende de t3.
    Resultado si no se detecta: `t3 → s1 → t3`, un ciclo que ninguno de
    los dos nodos puede resolver nunca (deadlock mudo, sin excepción).
    `validar()` tiene que correr sobre el grafo CON el reapunte hecho,
    no sobre `t3.deps` tal como estaba antes.
    """
    await db.create_task_graph("g_16", "x", tareas=[
        {"id": "t1", "titulo": "a"},
        {"id": "t2", "titulo": "el que se subdivide", "deps": ["t1"]},
        {"id": "t3", "titulo": "depende de t2", "deps": ["t2"]},
    ])
    with pytest.raises(grafo.GrafoInvalido):
        await db.add_tasks_to_graph("g_16", [
            {"id": "s1", "titulo": "sub", "deps": ["t3"]},
        ], reemplaza="t2")
    g = await db.get_task_graph("g_16")
    por_id = {t["id"]: t for t in g["tasks"]}
    assert len(g["tasks"]) == 3, "insertó s1 pese al ciclo"
    assert por_id["t3"]["deps"] == ["t2"], "reapuntó t3 pese al ciclo"


async def test_id_repetido_falla_limpio(db):
    await db.create_task_graph("g_15", "x", tareas=TAREAS)
    with pytest.raises(grafo.GrafoInvalido):
        await db.add_tasks_to_graph("g_15", [
            {"id": "t1", "titulo": "choca con uno que ya existe"},
        ])
    g = await db.get_task_graph("g_15")
    assert len(g["tasks"]) == len(TAREAS)
    # Nada de basura: ni la tarea ni sus (inexistentes) deps quedaron.
    assert await db.run(
        "SELECT * FROM task_deps WHERE graph_id='g_15' AND task_id='t1' "
        "AND depende_de NOT IN (SELECT id FROM tasks)") == []
