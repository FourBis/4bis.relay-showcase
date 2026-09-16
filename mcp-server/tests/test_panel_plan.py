"""El panel del plan para CUALQUIER pedido, no solo los grandes (2026-08-23).

Pedido del usuario: *"quiero verlo en la ui para poder guiarme dónde va,
independiente si es chico o grande"*.

Después de la Etapa A (grafo de un nodo): el endpoint SIEMPRE devuelve
`{"modo": "grafo", "grafo": {...}}`. Si hay un grafo real, es ese y sin
`sintetico`; si no, un grafo sintético encadenando los últimos 12
turnos de la conversación, con `sintetico: True`. Esto reescribe los
tests que afirmaban `modo == "lineal"` y `modo == "ninguno"`.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from relay import experts
from relay.db import Database
from relay.server import _estado_del_turno, _grafo_sintetico

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR")


# ---------- el parser de pasos (puro) ----------


class TestPasosDelPlan(unittest.TestCase):
    def test_pasos_numerados(self) -> None:
        self.assertEqual(
            experts.pasos_del_plan("1. Leer\n2. Migrar\n3. Cargar"),
            ["Leer", "Migrar", "Cargar"])

    def test_la_numeracion_no_queda_dentro_del_texto(self) -> None:
        """Si no, la UI numera sola y sale '1. 1. Leer el esquema'."""
        for p in experts.pasos_del_plan("1) Leer\n2) Migrar"):
            self.assertFalse(p[0].isdigit(), p)

    def test_una_linea_suelta_se_pega_al_paso_anterior(self) -> None:
        """Un modelo que parte un paso en dos renglones no debería
        inventar un paso de más."""
        pasos = experts.pasos_del_plan(
            "1. Leer el esquema\n   y anotar las FK\n2. Migrar")
        self.assertEqual(len(pasos), 2)
        self.assertIn("FK", pasos[0])

    def test_tolera_el_markdown_de_cada_modelo(self) -> None:
        pasos = experts.pasos_del_plan(
            "- **Paso 1:** Leer\n> 2. Migrar\n  3) Cargar")
        self.assertEqual(len(pasos), 3, pasos)

    def test_una_senal_no_es_un_plan(self) -> None:
        """`TRIVIAL:` y `DEMASIADO_GRANDE:` son salidas válidas SIN pasos:
        el panel tiene que caer a 'sin plan', no inventar uno."""
        self.assertEqual(experts.pasos_del_plan("TRIVIAL: es una línea"), [])
        self.assertEqual(
            experts.pasos_del_plan("DEMASIADO_GRANDE: son seis cosas"), [])

    def test_prosa_sin_pasos_no_da_pasos(self) -> None:
        self.assertEqual(experts.pasos_del_plan("Voy a mirar el repo"), [])
        self.assertEqual(experts.pasos_del_plan(""), [])

    def test_un_plan_absurdo_se_recorta(self) -> None:
        largo = "\n".join(f"{i}. paso" for i in range(1, 60))
        self.assertLessEqual(len(experts.pasos_del_plan(largo)), 30)


# ---------- _estado_del_turno (puro) ----------


class TestEstadoDelTurno(unittest.TestCase):
    """Los cinco casos del P2 de la Etapa A.

    La función es pura y se testea sin DB: las cinco ramas tienen que
    ganar en orden, sin heurística, para que un nodo no termine en rojo
    porque el modelo no marcó un paso.
    """

    def _fila(self, status="ok", phase_at_end=None):
        return {"status": status, "phase_at_end": phase_at_end}

    def test_running_es_corriendo(self) -> None:
        self.assertEqual(_estado_del_turno(self._fila("running"), False),
                         "corriendo")

    def test_pregunta_abierta_es_esperando_humano(self) -> None:
        # Aunque el status siga siendo "running", una pregunta abierta
        # abierta tiene prioridad — el ejecutor cortó a esperar.
        self.assertEqual(
            _estado_del_turno(self._fila("running"), True),
            "esperando_humano")

    def test_ok_y_fase_normal_es_hecho(self) -> None:
        self.assertEqual(_estado_del_turno(self._fila("ok"), False), "hecho")

    def test_fase_de_corte_es_fallado(self) -> None:
        for fase in ("idle_timeout", "hard_timeout",
                     "budget_exceeded", "off_plan"):
            with self.subTest(fase=fase):
                self.assertEqual(
                    _estado_del_turno(self._fila("ok", fase), False),
                    "fallado")

    def test_status_error_o_cancelled_es_fallado(self) -> None:
        for status in ("error", "cancelled"):
            with self.subTest(status=status):
                self.assertEqual(
                    _estado_del_turno(self._fila(status), False),
                    "fallado")

    def test_cualquier_otro_caso_es_pendiente(self) -> None:
        # "pending", "queued", status vacío: nadie sabe todavía.
        self.assertEqual(_estado_del_turno(self._fila("pending"), False),
                         "pendiente")
        self.assertEqual(_estado_del_turno({"status": ""}, False),
                         "pendiente")


# ---------- el endpoint ----------


class TestEndpointPlan(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({"slug": "demo", "name": "Demo",
                                      "repo_path": str(base)})
        self.conv = await self.db.create_conversation(project_slug="demo")

        from relay.server import create_app
        self.client = TestClient(TestServer(create_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp.cleanup()

    async def _plan(self) -> dict:
        r = await self.client.get(f"/conversations/{self.conv}/plan")
        self.assertEqual(r.status, 200)
        return await r.json()

    async def _chat_con_plan(self, plan: str, status: str = "ok",
                             prompt: str = "hazlo", source: str = "test",
                             phase: str = "writing") -> str:
        chat_id = await self.db.create_chat(
            project_slug="demo", source=source, author="pytest",
            target="demo", conversation_id=self.conv, user_prompt=prompt)
        await self.db.set_chat_stages(chat_id, json.dumps(
            {"plan": plan, "planner_model": "nemo"}))
        if status != "running":
            await self.db.finish_chat(chat_id, status=status,
                                      phase_at_end=phase)
        return chat_id

    async def test_un_hilo_sin_runs_devuelve_un_grafo_vacio(self) -> None:
        """Antes: `modo: "ninguno"`. Ahora: `modo: "grafo"` con
        `tasks: []` (un grafo sin tareas no rompe el panel)."""
        body = await self._plan()
        self.assertEqual(body["modo"], "grafo")
        self.assertEqual(body["grafo"]["tasks"], [])
        self.assertTrue(body["grafo"]["sintetico"])
        self.assertFalse(body["grafo"]["corriendo"])

    async def test_un_run_normal_aparece_en_el_grafo_sintetico(self) -> None:
        """Antes: `modo: "lineal"` con `body["plan"]["pasos"]`. Ahora: un
        nodo por turno, con el detalle sacando los pasos del plan."""
        await self._chat_con_plan("1. Leer el esquema\n2. Migrar\n3. Cargar")
        body = await self._plan()
        self.assertEqual(body["modo"], "grafo")
        self.assertTrue(body["grafo"]["sintetico"])
        tasks = body["grafo"]["tasks"]
        self.assertEqual(len(tasks), 1)
        # El detalle trae los pasos numerados del plan.
        detalle = tasks[0]["detalle"]
        self.assertIn("1. Leer el esquema", detalle)
        self.assertIn("2. Migrar", detalle)
        self.assertIn("3. Cargar", detalle)

    async def test_el_grafo_muestra_estado_corriendo_mientras_el_run_corre(self) -> None:
        """El `progress.callback` lo detecta por `status=running`."""
        await self._chat_con_plan("1. Leer\n2. Migrar", status="running")
        body = await self._plan()
        self.assertEqual(body["modo"], "grafo")
        self.assertTrue(body["grafo"]["sintetico"])
        tasks = body["grafo"]["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["estado"], "corriendo")

    async def test_un_followup_sin_plan_sigue_en_el_grafo(self) -> None:
        """Un follow-up corto que no trajo plan igual aparece como nodo:
        el panel no se queda en blanco cuando el humano está mirando."""
        await self._chat_con_plan("1. Leer\n2. Migrar")
        vacio = await self.db.create_chat(
            project_slug="demo", source="test", author="pytest",
            target="demo", conversation_id=self.conv)
        await self.db.finish_chat(vacio, status="ok")

        body = await self._plan()
        self.assertEqual(body["modo"], "grafo")
        self.assertTrue(body["grafo"]["sintetico"])
        self.assertEqual(len(body["grafo"]["tasks"]), 2)

    async def test_una_senal_trivial_no_inventa_nodos(self) -> None:
        """`TRIVIAL:` es un pedido de una línea: el turno existe pero no
        tiene pasos que mostrar. El grafo igual lo tiene como un nodo
        (es un turno de la conversación); el detalle es la respuesta
        del experto, no pasos numerados."""
        await self._chat_con_plan("TRIVIAL: es una línea")
        body = await self._plan()
        self.assertEqual(body["modo"], "grafo")
        self.assertEqual(len(body["grafo"]["tasks"]), 1)
        self.assertFalse(body["grafo"]["tasks"][0]["detalle"]
                         .startswith("1."))

    async def test_el_grafo_gana_cuando_existe(self) -> None:
        """Si el pedido se partió en tareas, eso es el plan — el sintético
        del run que lo armó sería el plan de otra cosa. La forma y los
        ids del grafo real tienen que ser exactamente lo que arma
        `_grafo_publico` (sin la clave `sintetico`)."""
        await self._chat_con_plan("1. Leer\n2. Migrar")
        await self.db.create_task_graph("g1", "objetivo", tareas=[
            {"id": "a", "titulo": "A"}, {"id": "b", "titulo": "B",
                                         "deps": ["a"]}],
            conversation_id=self.conv, project_slug="demo")

        body = await self._plan()
        self.assertEqual(body["modo"], "grafo")
        self.assertEqual(body["grafo"]["capas"], [["a"], ["b"]])
        self.assertFalse(body["grafo"]["corriendo"])
        self.assertNotIn("sintetico", body["grafo"])

    async def test_una_conversacion_que_no_existe_es_404(self) -> None:
        r = await self.client.get("/conversations/fantasma/plan")
        self.assertEqual(r.status, 404)

    # ---------- regresiones del 2026-08-30 ----------
    #
    # Las cuatro salen del mismo diagnóstico: el panel mostraba algo
    # distinto de lo que estaba pasando. Van juntas porque comparten la
    # causa —la fila del chat no traía lo que el grafo sintético lee— y
    # separadas por síntoma, que es como se van a volver a romper.

    async def test_el_nodo_trae_el_titulo_del_pedido(self) -> None:
        """El bug de fondo: `list_chats_of_conversation` no seleccionaba
        `user_prompt` (que además no existía como columna), así que TODOS
        los nodos del grafo sintético salían con el título vacío. El test
        va de punta a punta —DB real, endpoint real— porque el error no
        estaba en la lógica sino entre la query y quien la lee."""
        await self._chat_con_plan("", prompt="arregla el login")
        body = await self._plan()
        tasks = body["grafo"]["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["titulo"], "arregla el login")
        self.assertIn("arregla el login", body["grafo"]["objetivo"])

    async def test_un_turno_que_murio_por_budget_no_se_pinta_de_verde(
            self) -> None:
        """Sin `phase_at_end` en la fila, `_estado_del_turno` nunca veía
        una fase terminal y un run muerto por presupuesto se dibujaba
        `hecho`. El panel no puede decir que salió bien algo que no."""
        await self._chat_con_plan("", status="ok", phase="budget_exceeded")
        body = await self._plan()
        self.assertEqual(body["grafo"]["tasks"][0]["estado"], "fallado")

    async def test_los_nodos_del_grafo_no_son_turnos_del_hilo(self) -> None:
        """Los runs `source='grafo'` son nodos de un plan, no turnos del
        humano: encadenarlos los muestra en un orden que no tuvieron
        (corrieron en paralelo) y empujan fuera del cap a los turnos que
        sí escribió una persona."""
        await self._chat_con_plan("", prompt="pedido del humano")
        for i in range(3):
            await self._chat_con_plan("", prompt=f"nodo {i}", source="grafo")
        body = await self._plan()
        titulos = [t["titulo"] for t in body["grafo"]["tasks"]]
        self.assertEqual(titulos, ["pedido del humano"])

    async def test_el_grafo_viejo_cede_ante_los_turnos_posteriores(
            self) -> None:
        """El síntoma que reportó el humano: terminado el plan, el hilo
        siguió trabajando y el panel seguía mostrando el plan al 100%
        —badge "hecho", barra llena— mientras corría un run nuevo. Un
        grafo terminado con trabajo posterior dejó de ser el estado del
        hilo; es su historia."""
        await self.db.create_task_graph("g1", "objetivo", tareas=[
            {"id": "a", "titulo": "A"}, {"id": "b", "titulo": "B"}],
            conversation_id=self.conv, project_slug="demo")
        # Mientras el grafo es lo último que pasó, gana él.
        self.assertNotIn("sintetico", (await self._plan())["grafo"])

        # El grafo terminó hace rato. Se envejece a mano porque
        # `now_iso()` tiene granularidad de SEGUNDOS: creado y superado
        # en el mismo segundo no es un caso real (un grafo corre
        # minutos) y empatarlos acá probaría el redondeo, no la regla.
        await self.db.run(
            "UPDATE task_graphs SET updated_at=? WHERE id='g1'",
            ("2020-01-01T00:00:00Z",))
        await self._chat_con_plan("", prompt="y ahora esto otro")
        body = await self._plan()
        self.assertTrue(body["grafo"]["sintetico"])
        self.assertEqual([t["titulo"] for t in body["grafo"]["tasks"]],
                         ["y ahora esto otro"])

    async def test_un_plan_que_esta_corriendo_gana_igual(self) -> None:
        """La otra mitad de la regla, y la que no se puede romper: si el
        plan está corriendo AHORA manda él, aunque el humano haya escrito
        en el hilo mientras tanto (que es lo normal — el aviso del panel
        justamente invita a corregir el rumbo). Lo que cede es el plan
        terminado, no el que trabaja."""
        from relay.server import GRAFOS_KEY

        await self.db.create_task_graph("g1", "objetivo", tareas=[
            {"id": "a", "titulo": "A"}, {"id": "b", "titulo": "B"}],
            conversation_id=self.conv, project_slug="demo")
        await self.db.run(
            "UPDATE task_graphs SET updated_at=? WHERE id='g1'",
            ("2020-01-01T00:00:00Z",))
        await self._chat_con_plan("", prompt="oye, cambia el rumbo")
        # El orquestador lo está ejecutando en este proceso.
        self.client.server.app[GRAFOS_KEY]["g1"] = object()

        body = await self._plan()
        self.assertNotIn("sintetico", body["grafo"])
        self.assertTrue(body["grafo"]["corriendo"])

    async def test_20_turnos_quedan_capeados_a_12(self) -> None:
        """El cap del P4 de la Etapa A: una cadena de 40 turnos son 40
        capas y el SVG se vuelve ilegible. Si se recortaron, el objetivo
        lo dice."""
        for i in range(20):
            chat_id = await self.db.create_chat(
                project_slug="demo", source="test", author="pytest",
                target="demo", conversation_id=self.conv)
            await self.db.set_chat_stages(chat_id, json.dumps(
                {"plan": f"{i}. paso"}))
            await self.db.finish_chat(chat_id, status="ok")

        body = await self._plan()
        self.assertEqual(body["modo"], "grafo")
        tasks = body["grafo"]["tasks"]
        self.assertEqual(len(tasks), 12)
        self.assertIn("12", body["grafo"]["objetivo"])
        self.assertIn("20", body["grafo"]["objetivo"])

    async def test_pasos_marcados_expanden_el_grafo(self) -> None:
        """Etapa B, P7: si hay `plan_steps_done` en algún turno, el
        endpoint activa `expandir_pasos=True` y cada turno se parte en
        N nodos `<cid>.<n>`. Sin markers, el grafo sigue siendo un nodo
        por turno (Etapa A intacta)."""
        # Turno 1: con markers, debe expandirse a 3 nodos.
        chat1 = await self.db.create_chat(
            project_slug="demo", source="test", author="pytest",
            target="demo", conversation_id=self.conv)
        await self.db.set_chat_stages(chat1, json.dumps({
            "plan": "1. uno\n2. dos\n3. tres",
            "plan_steps_done": {"1": "ok", "3": "ok"},
        }))
        await self.db.finish_chat(chat1, status="running")
        # Turno 2: sin markers, un solo nodo.
        chat2 = await self.db.create_chat(
            project_slug="demo", source="test", author="pytest",
            target="demo", conversation_id=self.conv)
        await self.db.set_chat_stages(chat2, json.dumps(
            {"plan": "1. siguiendo"}))
        await self.db.finish_chat(chat2, status="ok")

        body = await self._plan()
        self.assertEqual(body["modo"], "grafo")
        ids = [t["id"] for t in body["grafo"]["tasks"]]
        # chat1 → 3 pasos; chat2 → 1 nodo turno.
        self.assertEqual(ids, [f"{chat1}.1", f"{chat1}.2", f"{chat1}.3", chat2])
        estados = {t["id"]: t["estado"] for t in body["grafo"]["tasks"]}
        self.assertEqual(estados[f"{chat1}.1"], "hecho")
        self.assertEqual(estados[f"{chat1}.2"], "corriendo")
        self.assertEqual(estados[f"{chat1}.3"], "hecho")
        self.assertEqual(estados[chat2], "hecho")


# ---------- _grafo_sintetico (puro, sin DB) ----------


class TestGrafoSintetico(unittest.TestCase):
    """La función pura: con un dict de chats armado a mano."""

    def _fila(self, cid, status="ok", phase=None, plan="",
              plan_steps_done=None):
        stages = {}
        if plan:
            stages["plan"] = plan
        if plan_steps_done is not None:
            stages["plan_steps_done"] = plan_steps_done
        return {"id": cid, "status": status, "phase_at_end": phase,
                "stages_json": json.dumps(stages) if stages else "",
                "user_prompt": "algo"}

    def test_sin_turnos_no_invente_una_cadena(self) -> None:
        g = _grafo_sintetico([], corriendo=False, preguntas_por_chat={})
        self.assertEqual(g["tasks"], [])
        self.assertEqual(g["capas"], [])
        self.assertTrue(g["sintetico"])
        self.assertFalse(g["corriendo"])

    def test_tres_turnos_forman_una_cadena_de_tres_nodos(self) -> None:
        filas = [self._fila("a", status="ok", plan="1. leer"),
                 self._fila("b", status="ok", plan="1. migrar"),
                 self._fila("c", status="ok", plan="1. cargar")]
        g = _grafo_sintetico(filas, corriendo=False, preguntas_por_chat={})
        ids = [t["id"] for t in g["tasks"]]
        self.assertEqual(ids, ["a", "b", "c"])
        deps = [t["deps"] for t in g["tasks"]]
        self.assertEqual(deps, [[], ["a"], ["b"]])
        # Cada capa es un nodo: una cadena pura.
        self.assertEqual(g["capas"], [["a"], ["b"], ["c"]])

    def test_el_estado_de_cada_nodo_sale_de_la_fila(self) -> None:
        filas = [self._fila("a", status="running"),
                 self._fila("b", status="ok"),
                 self._fila("c", status="error")]
        g = _grafo_sintetico(filas, corriendo=True, preguntas_por_chat={})
        estados = {t["id"]: t["estado"] for t in g["tasks"]}
        self.assertEqual(estados["a"], "corriendo")
        self.assertEqual(estados["b"], "hecho")
        self.assertEqual(estados["c"], "fallado")

    def test_20_turnos_quedan_en_12(self) -> None:
        filas = [self._fila(f"c{i}") for i in range(20)]
        g = _grafo_sintetico(filas, corriendo=False, preguntas_por_chat={})
        self.assertEqual(len(g["tasks"]), 12)
        self.assertIn("12", g["objetivo"])

    def test_turno_con_4_pasos_y_2_marcados_genera_4_nodos(self) -> None:
        """Etapa B, P6/P7: si el turno tiene pasos, un nodo por paso,
        con el estado que sale de plan_steps_done.

        Caso: 4 pasos, 2 marcados (1 y 3), run corriendo.
        Esperado: 4 nodos encadenados con estados
        hecho / corriendo / hecho / pendiente.
        (P7: el primer paso sin marcar de un turno corriendo es el que
         está corriendo AHORA; los posteriores están pendientes.)"""
        filas = [self._fila("a", status="running",
                            plan="1. uno\n2. dos\n3. tres\n4. cuatro",
                            plan_steps_done={"1": "ok", "3": "ok"})]
        g = _grafo_sintetico(filas, corriendo=True, preguntas_por_chat={},
                             expandir_pasos=True)
        self.assertEqual(len(g["tasks"]), 4)
        ids = [t["id"] for t in g["tasks"]]
        self.assertEqual(ids, ["a.1", "a.2", "a.3", "a.4"])
        estados = {t["id"]: t["estado"] for t in g["tasks"]}
        self.assertEqual(estados["a.1"], "hecho")
        self.assertEqual(estados["a.2"], "corriendo")
        self.assertEqual(estados["a.3"], "hecho")
        self.assertEqual(estados["a.4"], "pendiente")
        # Un nodo por paso, encadenados.
        deps = [t["deps"] for t in g["tasks"]]
        self.assertEqual(deps, [[], ["a.1"], ["a.2"], ["a.3"]])