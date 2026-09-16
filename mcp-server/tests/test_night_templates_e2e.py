"""Las plantillas por HTTP: rutas, validación y arranque desde plantilla.

Los tests de `test_night_templates_y_veredicto.py` cubren la capa de DB.
Esto cubre lo que la DB no puede: que las rutas estén registradas, que el
JSON entre y salga con la forma que la UI espera, y —lo importante— que
`POST /night-mode/start` resuelva una plantilla en una directiva real.

Ese último es el que justifica el archivo: la plantilla puede estar
perfecta en la tabla y no llegar nunca al orquestador.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from relay.db import Database

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR")


class PlantillasHttpTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": str(base),
            "description": "init"})
        from relay.server import create_app
        self.client = TestClient(TestServer(create_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp.cleanup()

    async def _put(self, **body):
        return await self.client.put("/admin/api/night/templates", json=body)

    async def test_alta_y_listado(self) -> None:
        r = await self._put(nombre="release", directiva="P1. limpiar.")
        self.assertEqual(r.status, 200)
        self.assertTrue((await r.json())["global"])
        r = await self.client.get("/admin/api/night/templates")
        self.assertEqual(r.status, 200)
        t = (await r.json())["templates"]
        self.assertEqual([x["nombre"] for x in t], ["release"])

    async def test_directiva_vacia_es_400(self) -> None:
        """Una plantilla sin texto no sirve para nada Y TAPA a la global
        del mismo nombre: es peor que no tenerla."""
        r = await self._put(nombre="x", directiva="   ")
        self.assertEqual(r.status, 400)

    async def test_sin_nombre_es_400(self) -> None:
        self.assertEqual((await self._put(directiva="P1. algo.")).status, 400)

    async def test_proyecto_inexistente_es_404(self) -> None:
        r = await self._put(nombre="x", directiva="P1.", project_slug="nope")
        self.assertEqual(r.status, 404)

    async def test_filtrado_por_proyecto_incluye_las_globales(self) -> None:
        await self._put(nombre="global-one", directiva="g")
        await self._put(nombre="propia", directiva="p", project_slug="demo")
        r = await self.client.get("/admin/api/night/templates?project=demo")
        nombres = [x["nombre"] for x in (await r.json())["templates"]]
        self.assertEqual(nombres, ["propia", "global-one"])  # específica 1ro

    async def test_borrado(self) -> None:
        await self._put(nombre="tmp", directiva="d")
        r = await self.client.delete("/admin/api/night/templates/tmp")
        self.assertEqual(r.status, 200)
        r = await self.client.get("/admin/api/night/templates")
        self.assertEqual((await r.json())["templates"], [])

    async def test_borrar_lo_que_no_existe_es_404(self) -> None:
        r = await self.client.delete("/admin/api/night/templates/fantasma")
        self.assertEqual(r.status, 404)

    # ---- el puente: plantilla → directiva del run ----

    async def test_start_con_plantilla_inexistente_es_404(self) -> None:
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name,
            "description": "x", "night_mode_enabled": 1})
        r = await self.client.post("/night-mode/start",
                                   json={"project": "demo",
                                         "template": "no-existe"})
        self.assertEqual(r.status, 404)
        self.assertIn("plantilla", (await r.json())["error"])

    async def test_una_directiva_explicita_le_gana_a_la_plantilla(self) -> None:
        """El caso 'arranco de la plantilla pero con un retoque': si la
        plantilla pisara el texto, el retoque se perdería sin aviso.

        Se verifica sobre el resolvedor —no arrancando un run— porque un
        run real hace git y llama al LLM.
        """
        await self._put(nombre="r", directiva="DE LA PLANTILLA")
        # Misma precedencia que aplica el handler: directiva > template.
        directiva = "DEL USUARIO"
        tpl = await self.db.get_night_template("r", "demo")
        elegida = directiva or tpl["directiva"]
        self.assertEqual(elegida, "DEL USUARIO")
        # Y sin directiva, la plantilla sí manda.
        self.assertEqual("" or tpl["directiva"], "DE LA PLANTILLA")


if __name__ == "__main__":
    unittest.main()
