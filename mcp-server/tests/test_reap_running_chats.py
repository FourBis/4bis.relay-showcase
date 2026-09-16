"""Chats que quedaron en `running` al reiniciar el relay (2026-09-07).

Ningún run sobrevive a un reinicio del proceso, pero la fila de `chats`
sí: quedaba en `running` para siempre. La UI la cuenta como viva y le
corre el reloj sola.

`orquestador.sanar` ya reparaba las TAREAS de los grafos activos, pero
nunca tocó `chats` — y los chats de un grafo CANCELADO ni siquiera
entran en `list_active_graphs`, así que no los miraba nadie. Visto hoy:
dos nodos de un grafo cancelado sobrevivieron al reinicio como zombies.
"""
from __future__ import annotations

import pytest

from relay.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(path=tmp_path / "reap.db")
    await d.init_schema()
    await d.upsert_project({
        "slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    return d


async def _chat(d, status):
    """`create_chat` genera el id — devuelve el que quedó."""
    cid = await d.create_chat(project_slug="demo", source="api",
                              author="test", target="demo", user_prompt="x")
    if status != "running":
        await d.finish_chat(cid, status=status)
    return cid


async def test_cierra_los_running_y_deja_el_resto(db):
    vivos = [await _chat(db, "running"), await _chat(db, "running")]
    listo = await _chat(db, "ok")

    n = await db.reap_running_chats("se cortó el proceso")
    assert n == 2, f"esperaba cerrar 2 zombies, cerré {n}"

    for cid in vivos:
        c = await db.get_chat(cid)
        assert c["status"] == "cancelled", f"{cid} quedó en {c['status']}"
        assert c["finished_at"], (
            f"{cid} sin finished_at: la UI le sigue corriendo el reloj")
        assert c["phase_at_end"] == "cancelled"
        assert "cortó" in (c["error"] or "")

    # Un chat ya cerrado no se toca: reabrirlo o repisarle el status
    # sería peor que el bug.
    assert (await db.get_chat(listo))["status"] == "ok"


async def test_es_idempotente(db):
    await _chat(db, "running")
    assert await db.reap_running_chats("motivo") == 1
    # Segunda corrida (dos reinicios seguidos): ya no hay nada que cerrar.
    assert await db.reap_running_chats("motivo") == 0


async def test_no_pisa_el_error_que_ya_tenia(db):
    """`COALESCE`: si el chat ya explicaba por qué murió, gana lo suyo."""
    cid = await _chat(db, "running")
    await db.run("UPDATE chats SET error=? WHERE id=?",
                 ("el proveedor cortó", cid))
    await db.reap_running_chats("se cortó el proceso")
    c = await db.get_chat(cid)
    assert c["error"] == "el proveedor cortó"
