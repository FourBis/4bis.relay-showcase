"""Guarda de duplicados en `graphs_create` debe mirar el proyecto, no la conversación (2026-09-06).

Bug observado: el handler `POST /graphs` rechazaba un segundo POST
mientras había un grafo vivo, pero solo si el body traía
`conversation_id`. Dos POST sin `conversation_id` al mismo proyecto
pasaban la guarda y arrancaban DOS grafos en paralelo sobre el mismo
repositorio — la cascada del planificador tarda >3 min y durante esa
ventana no había fila intermedia que mostrara que el primero seguía
vivo, así que el segundo se lanzaba creyendo que el primero había
colgado.

El propio comentario de la guarda decía que el riesgo era "dos grafos
sobre el mismo repo se pisarían los archivos", pero la condición
chequeaba `conversation_id` y exigía que `conv_id` existiera. Faltaba
el chequeo por proyecto.

Hoy (sin el parche) el segundo POST sin `conversation_id` al mismo
proyecto pasa la guarda y entra al planificador → el test detecta la
llamada y falla. Con el parche, devuelve 409 con `graph_id` del grafo
vivo y un mensaje de cómo cancelarlo.
"""
from __future__ import annotations

import asyncio

import aiohttp.web
import pytest
from aiohttp.test_utils import TestClient, TestServer

from relay import planificador, server
from relay.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    await d.init_schema()
    await d.upsert_project({
        "slug": "demo",
        "name": "Demo",
        "repo_path": str(tmp_path),
    })
    # Dejo un grafo vivo en la base para simular la carrera que
    # motivó el bug: el primer POST ya pasó la guarda y dejó un grafo
    # activo en `task_graphs`.
    await d.create_task_graph(
        "g_vivo", "objetivo previo",
        tareas=[{"id": "t1", "titulo": "x"}],
        project_slug="demo",
    )
    return d


def _app(db: Database) -> aiohttp.web.Application:
    app = aiohttp.web.Application()
    app[server.DB_KEY] = db
    # `graphs_create` -> `_largar_grafo` accede a esta key. En
    # `create_app` se inicializa como `{}`; acá lo replicamos con el
    # mínimo necesario para que el path "sin duplicado" no rebote con
    # KeyError.
    app[server.GRAFOS_KEY] = {}
    app.router.add_post("/graphs", server.graphs_create)
    return app


class _PlanificadorSpy:
    """Cuenta cuántas veces el handler invocó al planificador.

    Si la guarda funciona, no debería ser invocado NINGUNA VEZ para el
    segundo POST. Si la guarda falta (caso sin `conversation_id` que
    motivó el bug), se invoca y este test lo detecta.
    """

    def __init__(self):
        self.llamadas: list[dict] = []

    async def armar_grafo(self, *args, **kwargs):
        self.llamadas.append({"args": args, "kwargs": kwargs})
        # `_grafo_publico` accede a `tasks` — devolvemos lista
        # vacía para no romper el path 202 en el test de control.
        return {
            "id": "fake", "objetivo": kwargs.get("objetivo", ""),
            "tasks": [], "estado": "activo",
            "created_at": "", "conv_id": None,
        }


async def test_segundo_post_sin_conv_id_mismo_proyecto_da_409(
    db, monkeypatch
):
    """El caso que motivó el bug: dos POST sin conv_id al mismo proyecto.

    Sin el parche: la guarda por conv_id es None y no se evalúa, el
    handler entra al planificador y este test ve la llamada y falla.
    Con el parche: la guarda por project_slug encuentra el grafo vivo
    y devuelve 409.
    """
    spy = _PlanificadorSpy()
    monkeypatch.setattr(planificador, "armar_grafo", spy.armar_grafo)

    app = _app(db)
    async with TestClient(TestServer(app)) as client:
        r = await client.post("/graphs", json={
            "project": "demo", "objetivo": "hacé todo",
            # sin conversation_id — el caso que pasó la guarda
            "arrancar": False,
        })
        assert r.status == 409, (
            f"esperaba 409 por duplicado en proyecto 'demo', obtuve "
            f"{r.status}; el bug de la guarda por conversation_id "
            f"sigue sin arreglarse")
        body = await r.json()
        assert body["graph_id"] == "g_vivo"
        assert "cancel" in body["message"].lower()

        # Crítico: el planificador NO se invocó. Si se invocó, el
        # segundo grafo se está armando en paralelo sobre el mismo
        # repo — exactamente el bug que estamos arreglando.
        assert spy.llamadas == [], (
            f"el planificador fue llamado {len(spy.llamadas)} veces "
            f"después de un 409; la guarda por proyecto no lo frenó. "
            f"Llamadas: {spy.llamadas!r}")


async def test_segundo_post_con_conv_id_mismo_proyecto_da_409(
    db, monkeypatch
):
    """Variante con conversation_id: la guarda vieja cubría este caso.

    Pero la guarda NUEVA (por proyecto) debe frenarlo primero, antes
    de evaluar la de conv_id. Si la nueva guarda no existiera y la
    vieja sí, este test también pasaría — por eso el caso sin conv_id
    es el decisivo. Acá probamos que con conv_id también se frena y
    sigue sin invocar al planificador.
    """
    spy = _PlanificadorSpy()
    monkeypatch.setattr(planificador, "armar_grafo", spy.armar_grafo)

    app = _app(db)
    async with TestClient(TestServer(app)) as client:
        r = await client.post("/graphs", json={
            "project": "demo", "objetivo": "hacé todo",
            "conversation_id": "otra-conv",
            "arrancar": False,
        })
        assert r.status == 409
        body = await r.json()
        # La guarda por proyecto se dispara primero: el mensaje y el
        # error son los de proyecto, no los de conversación.
        assert body["graph_id"] == "g_vivo"
        assert "proyecto" in body["error"]
        assert spy.llamadas == [], (
            f"el planificador fue llamado {len(spy.llamadas)} veces "
            f"después de un 409; llamadas: {spy.llamadas!r}")


async def test_primer_post_sin_conv_id_pasa_y_arriba_el_grafo(
    db, monkeypatch
):
    """Contrapartida: sin grafo vivo, el primer POST sí pasa.

    Si este test fallara, sería señal de que la nueva guarda se está
    disparando cuando NO debe — la sobreprotegería. La idea es que
    el chequeo rechace SOLO cuando hay un grafo activo.
    """
    # Grafo nuevo, sin activo previo
    d2 = Database(path=db.path.parent / "otro.db")
    await d2.init_schema()
    await d2.upsert_project({
        "slug": "limpio",
        "name": "Limpio",
        "repo_path": str(db.path.parent),
    })

    spy = _PlanificadorSpy()
    monkeypatch.setattr(planificador, "armar_grafo", spy.armar_grafo)

    app = _app(d2)
    async with TestClient(TestServer(app)) as client:
        r = await client.post("/graphs", json={
            "project": "limpio", "objetivo": "algo",
            "arrancar": False,
        })
        assert r.status == 202, (
            f"sin grafo previo, espero 202 (grafo nuevo), obtuve "
            f"{r.status}. La guarda está bloqueando casos válidos.")
        assert len(spy.llamadas) == 1

@pytest.fixture
async def db_limpio(tmp_path):
    """Igual que `db` pero SIN grafo vivo: la carrera que probamos abajo
    ocurre justamente cuando todavía no hay ninguna fila en
    `task_graphs`."""
    d = Database(path=tmp_path / "carrera.db")
    await d.init_schema()
    await d.upsert_project({
        "slug": "demo", "name": "Demo", "repo_path": str(tmp_path),
    })
    return d


class _PlanificadorLento:
    """Planificador que se queda adentro hasta que el test lo suelta.

    Reproduce la ventana real: `armar_grafo` tarda 1-2 minutos y la
    fila de `task_graphs` recién se escribe cuando vuelve.
    """

    def __init__(self):
        self.llamadas = 0
        self.entro = asyncio.Event()
        self.soltar = asyncio.Event()

    async def armar_grafo(self, *args, **kwargs):
        self.llamadas += 1
        self.entro.set()
        await self.soltar.wait()
        return {
            "id": "g_primero", "objetivo": kwargs.get("objetivo", ""),
            "tasks": [], "estado": "activo",
            "created_at": "", "conv_id": None,
        }


async def test_segundo_post_mientras_el_planificador_corre_da_409(
    db_limpio, monkeypatch
):
    """La carrera que la guarda por proyecto NO cubría (2026-09-07).

    La guarda mira `task_graphs`, pero esa fila recién existe cuando el
    planificador vuelve. Durante esos 1-2 minutos el proyecto queda sin
    reservar: un cliente que cortó por timeout y reintentó arranca un
    SEGUNDO grafo sobre el mismo repo. Pasó de verdad —g_ea388eef y
    g_05e6c360, 99 segundos aparte, los dos sobre workshopdemo y
    apuntando al mismo .md.

    Sin el parche el planificador se invoca DOS veces y el test falla.
    """
    plan = _PlanificadorLento()
    monkeypatch.setattr(planificador, "armar_grafo", plan.armar_grafo)

    app = _app(db_limpio)
    async with TestClient(TestServer(app)) as client:
        cuerpo = {"project": "demo", "objetivo": "auditá todo",
                  "arrancar": False}
        primero = asyncio.ensure_future(client.post("/graphs", json=cuerpo))
        # El segundo POST sale recién cuando el primero YA está adentro
        # del planificador: sin esto la carrera no se reproduce.
        await asyncio.wait_for(plan.entro.wait(), timeout=5)

        # `wait_for` a propósito: SIN el parche el segundo POST entra al
        # planificador y se queda esperando el mismo `soltar` que el
        # primero, así que el test colgaría para siempre en vez de
        # fallar. Con timeout, la ausencia de guarda sale como fallo.
        try:
            segundo = await asyncio.wait_for(
                client.post("/graphs", json=cuerpo), timeout=5)
        except asyncio.TimeoutError:
            plan.soltar.set()
            pytest.fail(
                "el segundo POST se quedó adentro del planificador: el "
                "proyecto no está reservado mientras se arma el grafo")
        assert segundo.status == 409, (
            f"esperaba 409 mientras el planificador arma el primer grafo, "
            f"obtuve {segundo.status}: el proyecto quedó sin reservar "
            f"durante la planificación")
        assert plan.llamadas == 1, (
            f"el planificador se invocó {plan.llamadas} veces; se están "
            f"armando dos grafos en paralelo sobre el mismo repo")

        plan.soltar.set()
        r1 = await primero
        assert r1.status == 202, "el primer POST tiene que seguir su curso"

    # Y el slug queda libre después: si el `finally` no soltara la
    # reserva, el proyecto quedaría trabado hasta reiniciar el relay.
    assert "demo" not in server._PLANIFICANDO
