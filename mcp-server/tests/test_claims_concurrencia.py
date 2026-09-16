"""Concurrencia y scope por proyecto de `Database.claim_task_files`.

Bug medido (2026-09-08): `claim_task_files` hacía SELECT + decisión en
Python + INSERT como operaciones sueltas, sin transacción — dos llamadas
concurrentes leían el mismo estado y las dos se creían dueñas del mismo
archivo. Y el SELECT de conflictos barría toda la tabla sin mirar de qué
proyecto era cada fila, así que una reserva de un repo bloqueaba la
misma ruta relativa en otro repo.

Cada test de acá falla SIN el arreglo de `db.py` y pasa CON él.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from relay.db import Database


# 40 corridas y no 1: la carrera es probabilística (medida en ~22% de
# los intentos). Con 1 sola iteración el test pasaría la mayoría de las
# veces aunque el bug siga ahí — no serviría de nada.
INTENTOS = 40


async def _db_temporal() -> Database:
    tmp = Path(tempfile.mkdtemp())
    db = Database(path=tmp / "t.db")
    await db.init_schema()
    return db


async def test_dos_claims_concurrentes_dejan_un_solo_dueño():
    for i in range(INTENTOS):
        db = await _db_temporal()
        await db.create_task_graph("g", "x", tareas=[
            {"id": "t1", "titulo": "1"}, {"id": "t2", "titulo": "2"}])

        r1, r2 = await asyncio.gather(
            db.claim_task_files("t1", "g", ["src/main.py"]),
            db.claim_task_files("t2", "g", ["src/main.py"]))

        dueños = await db.files_claimed_by_others("nadie")
        assert len(dueños) == 1, (
            f"intento {i}: {len(dueños)} dueños de src/main.py "
            f"(r1={r1}, r2={r2})")
        # Coherencia: al que le rechazaron, no debe figurar como dueño.
        rechazado_t1, rechazado_t2 = bool(r1), bool(r2)
        assert rechazado_t1 != rechazado_t2, (
            f"intento {i}: exactamente uno de los dos se tiene que "
            f"quedar sin el archivo (r1={r1}, r2={r2})")


async def test_dos_proyectos_misma_ruta_relativa_los_dos_la_consiguen():
    db = await _db_temporal()
    await db.upsert_project({"slug": "proy-a", "name": "A",
                             "repo_path": "C:/repos/a"})
    await db.upsert_project({"slug": "proy-b", "name": "B",
                             "repo_path": "C:/repos/b"})
    await db.create_task_graph("gA", "x", tareas=[{"id": "t1", "titulo": "1"}],
                               project_slug="proy-a")
    await db.create_task_graph("gB", "x", tareas=[{"id": "t2", "titulo": "2"}],
                               project_slug="proy-b")

    rechazados_a = await db.claim_task_files("t1", "gA", ["src/main.py"])
    rechazados_b = await db.claim_task_files("t2", "gB", ["src/main.py"])

    assert rechazados_a == []
    assert rechazados_b == []


async def test_mismo_proyecto_dos_grafos_misma_ruta_el_segundo_rebota():
    db = await _db_temporal()
    await db.upsert_project({"slug": "proy-a", "name": "A",
                             "repo_path": "C:/repos/a"})
    await db.create_task_graph("g1", "x", tareas=[{"id": "t1", "titulo": "1"}],
                               project_slug="proy-a")
    await db.create_task_graph("g2", "x", tareas=[{"id": "t2", "titulo": "2"}],
                               project_slug="proy-a")

    rechazados_1 = await db.claim_task_files("t1", "g1", ["src/main.py"])
    rechazados_2 = await db.claim_task_files("t2", "g2", ["src/main.py"])

    assert rechazados_1 == []
    assert rechazados_2 == ["src/main.py"]


async def test_carpeta_vs_archivo_sigue_chocando_dentro_del_mismo_proyecto():
    db = await _db_temporal()
    await db.upsert_project({"slug": "proy-a", "name": "A",
                             "repo_path": "C:/repos/a"})
    await db.create_task_graph("g1", "x", tareas=[
        {"id": "t1", "titulo": "1"}, {"id": "t2", "titulo": "2"}],
        project_slug="proy-a")

    await db.claim_task_files("t1", "g1", ["src/"])
    rechazados = await db.claim_task_files("t2", "g1", ["src/main.py"])

    assert rechazados == ["src/main.py"]
