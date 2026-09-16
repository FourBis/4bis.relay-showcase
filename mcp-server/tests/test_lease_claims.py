"""La reserva de archivo vence si nadie la renueva (2026-09-04).

**El agujero.** Las tres piezas que manejan reservas no tenían el mismo
alcance:

  - `files_claimed_by_others` bloquea **global**: mira todas las
    reservas vivas, sin importar de qué grafo son.
  - `sanar()` cura **por grafo**: recorre `g["tasks"]` del grafo que
    está arrancando, y de ningún otro.
  - `release_dead_claims` deja en paz a las tareas que dicen
    `estado='corriendo'`, porque por definición están vivas.

Juntas dejan un cuelgue permanente: un grafo que murió con nodos en
`corriendo` —relay reiniciado, proceso matado, nodo colgado a las 3am—
retiene sus archivos para siempre. Cuando arranca otro grafo, `sanar()`
no lo toca (es de otro grafo), el barrido lo saltea (dice estar
corriendo) y el bloqueo sí lo ve. El grafo nuevo espera por un muerto y
desde afuera parece un cuelgue sin causa.

Es la misma asimetría que el docstring de `release_dead_claims` ya
explica para su propio caso —"barrer con menos alcance del que bloquea
es justo el caso que deja el archivo tomado por nadie"— un nivel más
arriba.

**Por qué no se arregla haciendo `sanar()` global.** Porque mataría las
tareas de un grafo que SÍ está corriendo en paralelo (F2 corre dos).
Hay que distinguir *vivo* de *huérfano*, y desde la tabla no se puede:
las dos filas dicen `corriendo`. La señal tiene que venir de quien
realmente está ejecutando, y por eso es un latido: el loop renueva las
reservas de lo que tiene en `en_curso`; el que murió no renueva nada y
su lease vence.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_lease_claims.py -q
"""
from __future__ import annotations

import pytest

from relay import grafo
from relay.db import Database

VENCIDA = "2000-01-01T00:00:00Z"


@pytest.fixture
async def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    await d.init_schema()
    return d


async def _grafo_muerto_con_reserva(db):
    """Un grafo con un nodo que quedó `corriendo` y su archivo tomado.

    Es el estado exacto que deja un relay que se murió a mitad de un
    nodo: la fila sigue diciendo que corre y nadie la va a sanar,
    porque sanar solo mira el grafo que arranca.
    """
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    await db.claim_task_files("t1", "g", ["src/db.py"])
    await db.update_task("t1", estado=grafo.CORRIENDO)


async def test_la_reserva_de_una_tarea_colgada_vence_y_se_suelta(db):
    """El caso que hoy cuelga el grafo para siempre."""
    await _grafo_muerto_con_reserva(db)
    # Vencerla a mano en vez de esperar el TTL real (5 min por default).
    await db.run("UPDATE task_file_claims SET vence_at=? WHERE task_id=?",
                 (VENCIDA, "t1"))

    assert await db.release_dead_claims("g") == 1
    assert await db.files_claimed_by_others("t2") == set()


async def test_renovar_le_salva_la_reserva_a_la_tarea_viva(db):
    """El latido. Sin esto el TTL sería peor que el bug: a una tarea
    larga y viva se le vencerían los archivos mientras trabaja."""
    await _grafo_muerto_con_reserva(db)
    await db.run("UPDATE task_file_claims SET vence_at=? WHERE task_id=?",
                 (VENCIDA, "t1"))

    assert await db.renovar_claims(["t1"]) == 1

    assert await db.release_dead_claims("g") == 0
    assert await db.files_claimed_by_others("t2") == {"src/db.py"}


async def test_una_reserva_anterior_a_la_lease_no_vence_por_reloj(db):
    """Migración: las filas que ya existían tienen `vence_at` NULL. No
    pueden vencer por reloj —nunca se les fechó nada— y solo las alcanza
    la regla vieja, la de estado. Si vencieran, el deploy soltaría de
    golpe reservas de tareas que sí están corriendo."""
    await _grafo_muerto_con_reserva(db)
    await db.run("UPDATE task_file_claims SET vence_at=NULL WHERE task_id=?",
                 ("t1",))

    assert await db.release_dead_claims("g") == 0
    assert await db.files_claimed_by_others("t2") == {"src/db.py"}


async def test_renovar_sin_tareas_no_toca_nada(db):
    """`en_curso` vacío es normal (el loop arranca así). Un IN () vacío
    en SQL es un error de sintaxis, así que la guarda no es cosmética."""
    await _grafo_muerto_con_reserva(db)
    assert await db.renovar_claims([]) == 0
    assert await db.files_claimed_by_others("t2") == {"src/db.py"}
