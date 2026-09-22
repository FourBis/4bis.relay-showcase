"""El disparador de grafos, de punta a punta (2026-08-23).

Hasta hoy el motor del grafo (F1 + F2) estaba completo y no lo llamaba
nadie: `correr_grafo` solo aparecía en los tests. Faltaba quién arma un
grafo desde un pedido humano y quién lo arranca.

Se prueban las dos puertas:

  1. **REST** — `POST /graphs` arma y larga; `GET /graphs/{id}` es lo que
     va a leer el panel del chat; `POST /graphs/{id}/cancel` corta.
  2. **El chat** — `DEMASIADO_GRANDE:` dejaba una propuesta en prosa sin
     ejecutar nada. Ese corte es ahora el disparador: se arma el grafo y
     se larga. Es el caso para el que se construyó todo esto.

El modelo se reemplaza por uno falso: acá se prueba el cableado, no el
criterio del planificador (eso está en test_planificador.py).
"""
from __future__ import annotations
from relay import expert_models, expert_runner, expert_staged_runner, expert_stages

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from relay import experts, planificador
from relay.db import Database

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR")

_GRAFO_OK = json.dumps({"tareas": [
    {"id": "t1", "titulo": "Leer el esquema", "idempotente": True},
    {"id": "t2", "titulo": "Migrar", "deps": ["t1"], "archivos": ["src/db.py"]},
    {"id": "t3", "titulo": "Cargar datos", "deps": ["t2"]},
]})


class _Salida:
    def __init__(self, texto):
        self.output = texto


def _agente_que_dice(texto):
    class _Agente:
        def __init__(self, *a, **kw):
            pass

        async def run(self, prompt, **kw):
            return _Salida(texto)
    return _Agente


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": str(base)})

        from relay.server import create_app
        self.app = create_app()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp.cleanup()

    async def _md_del_chat(self, chat: dict, timeout: float = 5.0) -> str:
        """Contenido del .md, esperando a que la exportación lo escriba.

        Que el chat deje de estar `running` ya NO implica que el .md
        exista: desde c00408f `finalization.finish` guarda primero el
        resultado durable y exporta después, para que un disco lleno no
        deje el run sin cerrar. La ventana entre las dos escrituras es
        real —la UI la contempla cayendo al `output_payload` cuando
        falta `md_path`— así que un test que quiere el artefacto tiene
        que esperarlo en vez de asumirlo.
        """
        fin = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < fin:
            fila = await self.db.get_chat(chat["id"])
            if fila and fila.get("md_path"):
                return Path(fila["md_path"]).read_text(encoding="utf-8")
            await asyncio.sleep(0.02)
        raise AssertionError("la exportación nunca escribió el .md")

    async def _esperar(self, cond, timeout: float = 5.0) -> None:
        fin = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < fin:
            if cond():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("no pasó a tiempo")


class TestEndpoints(_Base):
    async def test_post_graphs_arma_y_larga(self) -> None:
        corridos: list = []

        async def fake_lanzar(db, project, graph_id, **kw):
            corridos.append(graph_id)
            return {"estado": "hecho"}

        with patch.object(experts, "Agent", _agente_que_dice(_GRAFO_OK)), \
             patch.object(experts, "build_model", lambda s: object()), \
             patch("relay.orquestador.lanzar", fake_lanzar):
            r = await self.client.post("/graphs", json={
                "project": "demo", "objetivo": "migrá todo el esquema"})
            self.assertEqual(r.status, 202)
            body = await r.json()
            # El humano ve QUÉ se va a hacer en la misma respuesta en que
            # arrancó: la diferencia entre un plan y una caja negra.
            self.assertEqual([t["titulo"] for t in body["tasks"]],
                             ["Leer el esquema", "Migrar", "Cargar datos"])
            self.assertEqual(body["progreso"]["total"], 3)
            # Los ids del modelo van prefijados con el grafo: `tasks.id`
            # es PK global y el modelo numera `t1`, `t2`… en cada plan,
            # así que sin prefijo el segundo grafo de la base choca. Ver
            # `planificador._ids_unicos`.
            g = body["id"]
            self.assertEqual(body["orden"], [f"{g}:t1", f"{g}:t2", f"{g}:t3"])
            # Las capas son lo que dibuja el panel: una fila por paso.
            self.assertEqual(body["capas"],
                             [[f"{g}:t1"], [f"{g}:t2"], [f"{g}:t3"]])
            await self._esperar(lambda: corridos)
            self.assertEqual(corridos, [body["id"]])

    async def test_post_graphs_sin_arrancar_guarda_pero_no_corre(self) -> None:
        corridos: list = []

        async def fake_lanzar(db, project, graph_id, **kw):
            corridos.append(graph_id)
            return {}

        with patch.object(experts, "Agent", _agente_que_dice(_GRAFO_OK)), \
             patch.object(experts, "build_model", lambda s: object()), \
             patch("relay.orquestador.lanzar", fake_lanzar):
            r = await self.client.post("/graphs", json={
                "project": "demo", "objetivo": "x", "arrancar": False})
            self.assertEqual(r.status, 202)
            gid = (await r.json())["id"]

        self.assertEqual(corridos, [])
        self.assertIsNotNone(await self.db.get_task_graph(gid))

    async def test_un_hilo_no_puede_tener_dos_grafos(self) -> None:
        """Dos planes sobre el mismo repo se pisan los archivos y el
        humano ve dos avances a los tumbos."""
        await self.db.create_conversation(
            project_slug="demo", conversation_id="c1")

        async def fake_lanzar(db, project, graph_id, **kw):
            await asyncio.sleep(5)

        with patch.object(experts, "Agent", _agente_que_dice(_GRAFO_OK)), \
             patch.object(experts, "build_model", lambda s: object()), \
             patch("relay.orquestador.lanzar", fake_lanzar):
            uno = await self.client.post("/graphs", json={
                "project": "demo", "objetivo": "x", "conversation_id": "c1"})
            self.assertEqual(uno.status, 202)
            dos = await self.client.post("/graphs", json={
                "project": "demo", "objetivo": "y", "conversation_id": "c1"})
            self.assertEqual(dos.status, 409)
            # El id del grafo vivo, no el grafo entero: es lo que el
            # llamador necesita para cancelarlo.
            self.assertEqual((await dos.json())["graph_id"],
                             (await uno.json())["id"])

    async def test_proyecto_inexistente_es_404(self) -> None:
        r = await self.client.post("/graphs", json={
            "project": "fantasma", "objetivo": "x"})
        self.assertEqual(r.status, 404)

    async def test_un_planificador_que_no_contesta_es_502(self) -> None:
        """El relay funciona; el que no devolvió algo usable fue el
        modelo. Un 500 mandaría a mirar el lugar equivocado."""
        with patch.object(experts, "Agent", _agente_que_dice("no puedo")), \
             patch.object(experts, "build_model", lambda s: object()):
            r = await self.client.post("/graphs", json={
                "project": "demo", "objetivo": "x"})
        self.assertEqual(r.status, 502)

    async def test_get_graphs_devuelve_progreso_para_el_panel(self) -> None:
        await self.db.create_task_graph("g1", "objetivo", tareas=[
            {"id": "a", "titulo": "A"}, {"id": "b", "titulo": "B",
                                         "deps": ["a"]}])
        await self.db.update_task("a", estado="hecho")

        r = await self.client.get("/graphs/g1")
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body["progreso"]["hechos"], 1)
        self.assertEqual(body["progreso"]["porcentaje"], 50)
        self.assertEqual(body["progreso"]["estado"], "activo")
        self.assertFalse(body["corriendo"])

    async def test_get_graphs_por_conversacion(self) -> None:
        await self.db.create_task_graph("g1", "objetivo", tareas=[
            {"id": "a", "titulo": "A"}, {"id": "b", "titulo": "B"}],
            conversation_id="c9")
        r = await self.client.get("/graphs?conversation=c9")
        self.assertEqual((await r.json())["graph"]["id"], "g1")

        vacio = await self.client.get("/graphs?conversation=nada")
        self.assertIsNone((await vacio.json())["graph"])

    async def test_cancel_corta_el_grafo_y_lo_marca(self) -> None:
        arranco = asyncio.Event()

        async def fake_lanzar(db, project, graph_id, **kw):
            arranco.set()
            await asyncio.sleep(30)

        with patch.object(experts, "Agent", _agente_que_dice(_GRAFO_OK)), \
             patch.object(experts, "build_model", lambda s: object()), \
             patch("relay.orquestador.lanzar", fake_lanzar):
            r = await self.client.post("/graphs", json={
                "project": "demo", "objetivo": "x"})
            gid = (await r.json())["id"]
            await asyncio.wait_for(arranco.wait(), timeout=5)

            r = await self.client.post(f"/graphs/{gid}/cancel")
            self.assertEqual(r.status, 200)
            self.assertEqual((await r.json())["estado"], "cancelado")

    async def test_el_veredicto_del_grafo_sale_en_el_get_del_panel(self) -> None:
        """El requisito que originó la etapa: verificar sin que el
        veredicto llegue a una pantalla es gastar un turno para nada.

        De 68 veredictos `off_plan` de los chats, 33 no se vieron nunca —
        por eso esto se prueba sobre la RESPUESTA HTTP que consume el
        panel (`_grafo_publico`) y no sobre la columna de la base.
        """
        from relay import orquestador

        await self.db.create_task_graph("g_ver", "arregla el login", tareas=[
            {"id": "a", "titulo": "A"}])

        async def fake_verifier(**kw):
            # La 5-tupla de `experts._run_verifier`. Se parchea acá y no
            # más adentro para pinear también el mapeo del factory.
            return ("off_plan", "tocó archivos que nadie pidió",
                    {"tokens_in": 900, "tokens_out": 120}, "", [])

        async def ejecutar(tarea):
            return {"ok": True, "resultado": "listo"}

        with patch.object(experts, "_run_verifier", fake_verifier):
            await orquestador.correr_grafo(
                self.db, "g_ver", ejecutar=ejecutar,
                verificar=orquestador.verificador_del_grafo(
                    {"defaults_json": {"verifier_model": "test"}}))

        body = await (await self.client.get("/graphs/g_ver")).json()
        v = body["verificacion"]
        self.assertEqual(v["verdict"], "off_plan")
        self.assertIn("nadie pidió", v["feedback"])
        self.assertEqual(v["modelo"], "test")
        # El costo de la etapa, visible sin instrumentar de nuevo.
        self.assertEqual((v["tokens_in"], v["tokens_out"]), (900, 120))

    async def test_un_grafo_sin_verificar_no_inventa_veredicto(self) -> None:
        """`{}` y no un `complete` vacío: "no se verificó" no es "está
        bien", y confundirlos es lo que este panel tiene que evitar."""
        await self.db.create_task_graph("g_sin", "x", tareas=[
            {"id": "a", "titulo": "A"}])
        body = await (await self.client.get("/graphs/g_sin")).json()
        self.assertEqual(body["verificacion"], {})

    async def test_el_verificador_del_grafo_viene_prendido(self) -> None:
        """Un knob que arranca apagado es una feature que nadie corre:
        `skills_mode: "compact"` lleva meses sin un solo proyecto."""
        from relay import orquestador

        vistos: list = []

        async def fake_correr(db, graph_id, **kw):
            vistos.append(kw.get("verificar"))
            return {"estado": "hecho"}

        await self.db.create_task_graph("g_on", "x", tareas=[
            {"id": "a", "titulo": "A"}])
        with patch.object(orquestador, "correr_grafo", fake_correr):
            await orquestador.lanzar(self.db, {"slug": "demo"}, "g_on")
            await orquestador.lanzar(
                self.db, {"slug": "demo",
                          "defaults_json": {"graph_verifier": False}}, "g_on")

        self.assertIsNotNone(vistos[0], "el default tiene que ser ON")
        self.assertIsNone(vistos[1], "el opt-out explícito no se respetó")

    async def test_cancel_de_un_grafo_que_nadie_corre_igual_lo_marca(self) -> None:
        """El proceso se reinició y el grafo quedó `activo` sin nadie
        atrás. El humano no quiere distinguir: quiere que pare."""
        await self.db.create_task_graph("g1", "x", tareas=[
            {"id": "a", "titulo": "A"}, {"id": "b", "titulo": "B"}])
        r = await self.client.post("/graphs/g1/cancel")
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())["estado"], "cancelado")


class TestDisparadorDelChat(_Base):
    """`DEMASIADO_GRANDE:` deja de proponer y pasa a ejecutar."""

    def _staged(self, plan: str, fase: str = "planned"):
        async def fake(project, user, **kw):
            return {"content": "descompuesto", "plan": plan,
                    "phase_at_end": fase, "model": "x", "three_stage": True,
                    "tokens_in": 1, "tokens_out": 1, "tool_calls": 0,
                    "duration_ms": 1, "messages_json": "", "last_tool": None,
                    "legs": 1, "steers": 0, "steer_texts": [],
                    "progress_events": [], "verifier_verdict": "",
                    "verifier_feedback": "", "doc": "", "stage_errors": {}}
        return fake

    async def _correr(self, **body):
        pedido = {"target": "demo", "user": "hacé el CRM entero",
                  "source": "test", "author": "pytest"}
        pedido.update(body)
        r = await self.client.post("/experts/run", json=pedido)
        self.assertEqual(r.status, 202)
        chat_id = (await r.json())["id"]
        await self._esperar(
            lambda: asyncio.run_coroutine_threadsafe is not None)
        for _ in range(250):
            chat = await self.db.get_chat(chat_id)
            if chat and chat["status"] != "running":
                return chat
            await asyncio.sleep(0.02)
        raise AssertionError("el run no terminó")

    async def test_un_pedido_grande_arma_el_grafo_y_lo_larga(self) -> None:
        corridos: list = []

        async def fake_lanzar(db, project, graph_id, **kw):
            corridos.append(graph_id)
            return {"estado": "hecho"}

        with patch.object(experts, "run_expert_staged",
                          self._staged("DEMASIADO_GRANDE: son seis cosas")), \
             patch.object(experts, "Agent", _agente_que_dice(_GRAFO_OK)), \
             patch.object(experts, "build_model", lambda s: object()), \
             patch("relay.orquestador.lanzar", fake_lanzar):
            await self._correr()
            await self._esperar(lambda: corridos)

        grafos = await self.db.run("SELECT * FROM task_graphs")
        self.assertEqual(len(grafos), 1)
        self.assertEqual(corridos, [grafos[0]["id"]])

    async def test_el_humano_ve_las_tareas_no_un_id_suelto(self) -> None:
        async def fake_lanzar(db, project, graph_id, **kw):
            return {}

        with patch.object(experts, "run_expert_staged",
                          self._staged("DEMASIADO_GRANDE: son seis cosas")), \
             patch.object(experts, "Agent", _agente_que_dice(_GRAFO_OK)), \
             patch.object(experts, "build_model", lambda s: object()), \
             patch("relay.orquestador.lanzar", fake_lanzar):
            chat = await self._correr()

        # `_correr` vuelve apenas el chat deja de estar `running`, y desde
        # c00408f eso ya NO implica que el .md exista: `finalization.finish`
        # guarda primero el resultado durable y exporta después, justo para
        # que un disco lleno no deje el run sin cerrar. La ventana entre
        # las dos escrituras es real y la UI la contempla (cae al
        # `output_payload` cuando falta `md_path`), así que el test espera
        # el artefacto en vez de asumir que ya está.
        md = await self._md_del_chat(chat)
        self.assertIn("Leer el esquema", md)
        self.assertIn("Cargar datos", md)
        self.assertIn("3 tareas", md)

    async def test_el_flag_apagado_deja_la_propuesta_de_antes(self) -> None:
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name,
            "defaults_json": {"grafo_automatico": False}})

        with patch.object(experts, "run_expert_staged",
                          self._staged("DEMASIADO_GRANDE: son seis cosas")), \
             patch.object(experts, "Agent", _agente_que_dice(_GRAFO_OK)), \
             patch.object(experts, "build_model", lambda s: object()):
            await self._correr()

        self.assertEqual(await self.db.run("SELECT * FROM task_graphs"), [])

    async def test_si_el_planificador_falla_queda_la_descomposicion(self) -> None:
        """Degradar a lo que ya funcionaba es mejor que un error: la
        propuesta en prosa sigue siendo útil para el humano."""
        with patch.object(experts, "run_expert_staged",
                          self._staged("DEMASIADO_GRANDE: son seis cosas")), \
             patch.object(experts, "Agent", _agente_que_dice("no puedo")), \
             patch.object(experts, "build_model", lambda s: object()):
            chat = await self._correr()

        self.assertEqual(await self.db.run("SELECT * FROM task_graphs"), [])
        self.assertEqual(chat["status"], "ok")
        self.assertIn("descompuesto", await self._md_del_chat(chat))

    async def test_un_pedido_normal_no_arma_ningun_grafo(self) -> None:
        """El disparador se cuelga SOLO del corte por pedido grande. Un
        run común no puede convertirse en grafo por accidente."""
        with patch.object(experts, "run_expert_staged",
                          self._staged("", fase="ok")), \
             patch.object(experts, "Agent", _agente_que_dice(_GRAFO_OK)), \
             patch.object(experts, "build_model", lambda s: object()):
            await self._correr()

        self.assertEqual(await self.db.run("SELECT * FROM task_graphs"), [])


class TestRetomar(_Base):
    """F4: un grafo cortado se retoma, y contestar mueve el plan."""

    async def test_resume_relanza_un_grafo_cortado(self) -> None:
        await self.db.create_task_graph("g1", "x", tareas=[
            {"id": "a", "titulo": "A", "idempotente": True},
            {"id": "b", "titulo": "B", "deps": ["a"]}], project_slug="demo")
        await self.db.update_task("a", estado="corriendo")
        corridos: list = []

        async def fake_lanzar(db, project, graph_id, **kw):
            corridos.append(graph_id)
            return {"estado": "hecho"}

        with patch("relay.orquestador.lanzar", fake_lanzar):
            r = await self.client.post("/graphs/g1/resume")
            self.assertEqual(r.status, 202)
            await self._esperar(lambda: corridos)
        self.assertEqual(corridos, ["g1"])

    async def test_resume_de_un_grafo_ya_corriendo_es_409(self) -> None:
        """Largar dos veces el mismo grafo duplicaría cada nodo."""
        await self.db.create_task_graph("g1", "x", tareas=[
            {"id": "a", "titulo": "A"}, {"id": "b", "titulo": "B"}],
            project_slug="demo")
        arranco = asyncio.Event()

        async def fake_lanzar(db, project, graph_id, **kw):
            arranco.set()
            await asyncio.sleep(30)

        with patch("relay.orquestador.lanzar", fake_lanzar):
            await self.client.post("/graphs/g1/resume")
            await asyncio.wait_for(arranco.wait(), timeout=5)
            r = await self.client.post("/graphs/g1/resume")
            self.assertEqual(r.status, 409)

    async def test_retomar_un_grafo_cancelado_lo_vuelve_a_activo(self) -> None:
        """Si no, el panel mostraría un plan 'cancelado' avanzando."""
        await self.db.create_task_graph("g1", "x", tareas=[
            {"id": "a", "titulo": "A"}, {"id": "b", "titulo": "B"}],
            project_slug="demo")
        await self.db.set_task_graph_state("g1", "cancelado")

        async def fake_lanzar(db, project, graph_id, **kw):
            return {}

        with patch("relay.orquestador.lanzar", fake_lanzar):
            r = await self.client.post("/graphs/g1/resume")
        self.assertEqual((await r.json())["estado"], "activo")

    async def test_resume_de_un_grafo_sin_proyecto_avisa(self) -> None:
        """Un grafo cuyo proyecto se borró no se puede retomar, y decirlo
        es mejor que un 500 o un relanzamiento que reventaría adentro."""
        await self.db.create_task_graph("g_huerfano", "x", tareas=[
            {"id": "a", "titulo": "A"}, {"id": "b", "titulo": "B"}],
            project_slug="ya-no-existe")
        r = await self.client.post("/graphs/g_huerfano/resume")
        self.assertEqual(r.status, 409)
        self.assertIn("ya-no-existe", (await r.json())["error"])

    async def test_resume_de_un_grafo_que_no_existe_es_404(self) -> None:
        r = await self.client.post("/graphs/fantasma/resume")
        self.assertEqual(r.status, 404)

    async def test_contestar_con_texto_libre_le_llega_a_la_tarea(self) -> None:
        """El humano puede escribir en vez de elegir una opción, y eso
        que escribe es información que la tarea necesita.

        Antes el endpoint guardaba el texto, retomaba el grafo y no se lo
        pasaba a nadie: el nodo se reintentaba idéntico. Como el grafo sí
        avanzaba, el agujero no se veía.
        """
        from relay import orquestador

        await self.db.create_task_graph("g2", "x", tareas=[
            {"id": "a", "titulo": "migrar", "chat_id": "c2",
             "detalle": "ejecuta la migración"}], project_slug="demo")
        await self.db.update_task("a", estado="corriendo", chat_id="c2")
        await orquestador.sanar(self.db, "g2")     # deja la pregunta
        q = (await self.db.list_expert_questions(chat_id="c2"))[0]
        corridos: list = []

        async def fake_lanzar(db, project, graph_id, **kw):
            corridos.append(graph_id)
            return {"estado": "hecho"}

        with patch("relay.orquestador.lanzar", fake_lanzar):
            r = await self.client.post(
                f"/questions/{q['id']}/answer",
                json={"text": "la base es la de staging, no la de prod"})
            self.assertEqual(r.status, 200)
            body = await r.json()
            await self._esperar(lambda: corridos)

        self.assertEqual(body["grafo"]["graph_id"], "g2")
        fila = next(t for t in (await self.db.get_task_graph("g2"))["tasks"]
                    if t["id"] == "a")
        self.assertEqual(fila["estado"], "pendiente")
        self.assertIn("staging", fila["detalle"])
        self.assertIn("ejecuta la migración", fila["detalle"])

    async def test_contestar_la_pregunta_retoma_el_plan(self) -> None:
        """La mitad de F4 que faltaba: sin esto el orquestador dejaba la
        pregunta, el humano contestaba y el grafo seguía parado."""
        from relay import orquestador

        await self.db.create_task_graph("g1", "x", tareas=[
            {"id": "a", "titulo": "migrar", "chat_id": "c1"},
            {"id": "b", "titulo": "cargar", "deps": ["a"]}],
            project_slug="demo")
        await self.db.update_task("a", estado="corriendo", chat_id="c1")
        await orquestador.sanar(self.db, "g1")     # deja la pregunta
        q = (await self.db.list_expert_questions(chat_id="c1"))[0]
        corridos: list = []

        async def fake_lanzar(db, project, graph_id, **kw):
            corridos.append(graph_id)
            return {"estado": "hecho"}

        with patch("relay.orquestador.lanzar", fake_lanzar):
            r = await self.client.post(f"/questions/{q['id']}/answer",
                                       json={"choice": "reintentar"})
            self.assertEqual(r.status, 200)
            body = await r.json()
            await self._esperar(lambda: corridos)

        self.assertEqual(body["grafo"]["graph_id"], "g1")
        self.assertEqual(body["grafo"]["decision"], "reintentar")
        # El trabajo lo sigue el orquestador: mandar además el
        # `resume_prompt` como turno de chat duplicaría el pedido.
        self.assertEqual(body["resume_prompt"], "")
        self.assertEqual(corridos, ["g1"])
        fila = next(t for t in (await self.db.get_task_graph("g1"))["tasks"]
                    if t["id"] == "a")
        self.assertEqual(fila["estado"], "pendiente")

    async def test_parar_desde_la_pregunta_no_relanza_nada(self) -> None:
        from relay import orquestador

        await self.db.create_task_graph("g1", "x", tareas=[
            {"id": "a", "titulo": "migrar", "chat_id": "c1"},
            {"id": "b", "titulo": "cargar", "deps": ["a"]}],
            project_slug="demo")
        await self.db.update_task("a", estado="corriendo", chat_id="c1")
        await orquestador.sanar(self.db, "g1")
        q = (await self.db.list_expert_questions(chat_id="c1"))[0]
        corridos: list = []

        async def fake_lanzar(db, project, graph_id, **kw):
            corridos.append(graph_id)

        with patch("relay.orquestador.lanzar", fake_lanzar):
            r = await self.client.post(f"/questions/{q['id']}/answer",
                                       json={"choice": "parar"})
            self.assertEqual(r.status, 200)
            await asyncio.sleep(0.2)

        self.assertEqual(corridos, [])
        self.assertEqual((await self.db.get_task_graph("g1"))["estado"],
                         "cancelado")

    async def test_una_pregunta_ajena_al_grafo_sigue_como_siempre(self) -> None:
        """El chat normal no puede cambiar de comportamiento por esto:
        una pregunta de `ask_human` fuera de un grafo tiene que seguir
        devolviendo su `resume_prompt` para que el hilo retome."""
        await self.db.create_expert_question(
            "q_suelta", "chat_sin_grafo",
            json.dumps({"title": "¿Instalo pnpm?",
                        "options": [{"key": "o0", "label": "Dale"}]}),
            project_slug="demo", kind="choice")

        r = await self.client.post("/questions/q_suelta/answer",
                                   json={"choice": "o0"})
        body = await r.json()
        self.assertNotIn("grafo", body)
        self.assertIn("pnpm", body["resume_prompt"])


class TestBootSana(unittest.IsolatedAsyncioTestCase):
    """Al arrancar, un grafo que quedó a medias se sana pero NO se
    relanza solo: soltar reservas y marcar huérfanos es reparación;
    arrancar trabajo que nadie pidió es otra cosa."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        db = Database()
        await db.init_schema()
        await db.upsert_project({"slug": "demo", "name": "Demo",
                                 "repo_path": str(base)})
        await db.create_task_graph("g1", "x", tareas=[
            {"id": "a", "titulo": "A", "idempotente": True,
             "archivos": ["src/a.py"]},
            {"id": "b", "titulo": "B", "deps": ["a"]}])
        await db.update_task("a", estado="corriendo")
        await db.claim_task_files("a", "g1", ["src/a.py"])
        self.db = db

    async def asyncTearDown(self) -> None:
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp.cleanup()

    async def test_el_boot_sana_los_grafos_a_medias(self) -> None:
        from relay.server import create_app

        client = TestClient(TestServer(create_app()))
        await client.start_server()
        try:
            fila = next(t for t in (await self.db.get_task_graph("g1"))["tasks"]
                        if t["id"] == "a")
            self.assertEqual(fila["estado"], "pendiente")
            # Lo que más importa: la reserva quedó libre. El barrido de
            # reservas muertas no toca las de una tarea `corriendo`, así
            # que sin sanar trababa a todo grafo posterior.
            self.assertEqual(await self.db.files_claimed_by_others("otra"),
                             set())
        finally:
            await client.close()


class TestElGrafoQuedaComoRegistro(_Base):
    """Un grafo terminado no desaparece de la vista (2026-08-24).

    `conversation_plan` preguntaba solo por `active_task_graph`, que
    filtra `estado='activo'`: en cuanto la última tarea pasaba a `hecho`
    el panel caía a modo lineal y con él se iba lo único que decía qué se
    hizo, en qué orden, qué falló y cuántos intentos costó. El grafo
    seguía entero en la base, sin forma de leerlo — y es el registro más
    útil que deja un pedido grande, justo el que sirve para aprender del
    run siguiente.
    """

    async def _conv(self) -> str:
        r = await self.client.post("/conversations",
                                   json={"project": "demo"})
        return (await r.json())["id"]

    async def _grafo(self, conv: str, gid: str, estado: str) -> None:
        await self.db.create_task_graph(gid, "migrá todo", tareas=[
            {"id": f"{gid}:t1", "titulo": "Leer"},
            {"id": f"{gid}:t2", "titulo": "Migrar",
             "deps": [f"{gid}:t1"]}], conversation_id=conv)
        if estado != "activo":
            await self.db.set_task_graph_state(gid, estado)

    async def test_un_grafo_terminado_se_sigue_viendo(self) -> None:
        conv = await self._conv()
        await self._grafo(conv, "g_fin", "hecho")

        r = await self.client.get(f"/conversations/{conv}/plan")
        body = await r.json()
        self.assertEqual(body["modo"], "grafo")
        self.assertEqual(body["grafo"]["id"], "g_fin")
        self.assertEqual(body["grafo"]["estado"], "hecho")
        # `corriendo=False` es lo que apaga el cronómetro, el aviso del
        # composer y el botón de parar: se ve como registro, no como algo
        # que está pasando.
        self.assertFalse(body["grafo"]["corriendo"])
        # Y llegan las tareas con su estado: eso es el registro.
        self.assertEqual([t["titulo"] for t in body["grafo"]["tasks"]],
                         ["Leer", "Migrar"])

    async def test_uno_cancelado_tambien(self) -> None:
        conv = await self._conv()
        await self._grafo(conv, "g_cort", "cancelado")
        body = await (await self.client.get(
            f"/conversations/{conv}/plan")).json()
        self.assertEqual(body["modo"], "grafo")
        self.assertEqual(body["grafo"]["estado"], "cancelado")

    async def test_el_activo_le_gana_al_viejo(self) -> None:
        """Dos grafos en el hilo: manda el que está vivo."""
        conv = await self._conv()
        await self._grafo(conv, "g_viejo", "hecho")
        await self._grafo(conv, "g_nuevo", "activo")
        body = await (await self.client.get(
            f"/conversations/{conv}/plan")).json()
        self.assertEqual(body["grafo"]["id"], "g_nuevo")

    async def test_un_run_en_curso_le_gana_al_grafo_terminado(self) -> None:
        """Lo que pasa AHORA importa más que el registro de lo que pasó.

        Sin este corte, seguir charlando en un hilo que tuvo grafo
        mostraría el grafo viejo para siempre, tapando el plan del run
        que está corriendo.

        Escrito de nuevo el 2026-09-07 contra el contrato de la Etapa A:
        el endpoint devuelve **siempre** `modo: "grafo"`, así que "gana
        el run" ya no se lee en el modo sino en cuál de los dos grafos
        salió. El grafo viejo, que no está vivo y quedó atrás de un
        turno humano, cede el lugar al sintético del hilo.
        """
        conv = await self._conv()
        await self._grafo(conv, "g_fin", "hecho")
        # El grafo terminó ANTES del turno vivo, y lo decimos con una
        # fecha explícita en vez de confiar en el reloj: `now_iso()`
        # tiene granularidad de segundo, así que crear el grafo y el
        # chat seguido los deja con el MISMO timestamp y el `>` de
        # `hay_turnos_humanos_despues` da False. Sin esto el test mide
        # la velocidad de la máquina, no el corte del endpoint.
        await self.db.run(
            "UPDATE task_graphs SET created_at=?, updated_at=? WHERE id=?",
            ("2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z", "g_fin"))
        # `create_chat` lo deja en `running`, que es de donde el endpoint
        # saca `corriendo` — no del progress store.
        chat_id = await self.db.create_chat(
            project_slug="demo", source="ui", author="ui", target="demo",
            conversation_id=conv)
        await self.db.set_chat_stages(chat_id, json.dumps(
            {"plan": "1. Leer main.py\n2. Editar", "planner_model": "test"}))

        body = await (await self.client.get(
            f"/conversations/{conv}/plan")).json()
        grafo = body["grafo"]
        # El registro viejo NO es lo que se muestra: el que sale es el
        # sintético, armado con los turnos del hilo.
        self.assertTrue(grafo.get("sintetico"))
        self.assertNotEqual(grafo["id"], "g_fin")
        # Y se ve como algo que está pasando, no como historia: esto es
        # lo que prende el cronómetro y el botón de parar en el panel.
        self.assertTrue(grafo["corriendo"])
        self.assertEqual(grafo["estado"], "activo")
        # El turno vivo está entre los nodos, que es lo que el panel
        # tapaba cuando ganaba el grafo terminado.
        self.assertIn(chat_id, [t["chat_id"] for t in grafo["tasks"]])


class TestEstadoVisibleYPresupuesto(_Base):
    """Dos problemas de visibilidad, mismo diagnóstico: el estado que se
    guarda es correcto para la máquina pero no le alcanza al humano
    (6/9/26).

    1. Seis grafos quedaron `activo` 4-5 días parados: tenían una tarea
       en `esperando_humano` y ninguna `corriendo`, y por fuera se veían
       como si algo siguiera corriendo. `estado_visible` (`grafo.py`)
       deriva la etiqueta de presentación SIN tocar `task_graphs.estado`
       —de ese valor depende el relanzamiento, ver `graphs_resume`.
    2. Dos nodos de un grafo de inventorydemo murieron por `budget_exceeded` el
       1/9 y el grafo quedó `fallado` sin decir por qué. El motivo ya
       estaba en `tasks.error`; `presupuesto_agotado` solo lo expone.
    """

    async def test_esperando_humano_sin_nada_corriendo_se_ve_distinto(
            self) -> None:
        await self.db.create_task_graph("g_espera", "objetivo", tareas=[
            {"id": "a", "titulo": "A"},
            {"id": "b", "titulo": "B", "deps": ["a"]}])
        await self.db.update_task("a", estado="esperando_humano")

        body = await (await self.client.get("/graphs/g_espera")).json()
        # El valor persistido NO se toca: sigue siendo lo que usa
        # `graphs_resume` para saber que el grafo se puede relanzar.
        self.assertEqual(body["estado"], "activo")
        self.assertEqual(body["estado_visible"], "esperando_humano")

    async def test_con_algo_corriendo_de_verdad_no_se_disfraza_de_espera(
            self) -> None:
        await self.db.create_task_graph("g_corre", "objetivo", tareas=[
            {"id": "a", "titulo": "A"}, {"id": "b", "titulo": "B"}])
        await self.db.update_task("a", estado="esperando_humano")
        await self.db.update_task("b", estado="corriendo")

        body = await (await self.client.get("/graphs/g_corre")).json()
        self.assertEqual(body["estado_visible"], "activo")

    async def test_presupuesto_agotado_se_distingue_de_un_fallo_comun(
            self) -> None:
        await self.db.create_task_graph("g_budget", "objetivo", tareas=[
            {"id": "a", "titulo": "A"}, {"id": "b", "titulo": "B"}])
        await self.db.update_task(
            "a", estado="fallado",
            error="el nodo terminó en 'budget_exceeded'")
        await self.db.update_task(
            "b", estado="fallado", error="el nodo terminó en 'error'")

        body = await (await self.client.get("/graphs/g_budget")).json()
        por_id = {t["id"]: t for t in body["tasks"]}
        self.assertTrue(por_id["a"]["presupuesto_agotado"])
        self.assertFalse(por_id["b"]["presupuesto_agotado"])
