"""Flags del proyecto en la Admin UI (2026-08-16).

`read_only`, `sandbox`, `native_files` y compañía vivían solo en el blob
`defaults_json`, editable escribiendo JSON contra la API. Cambian lo que
el experto PUEDE hacer, y la gente no configura lo que no encuentra.

Lo que estos tests cuidan, en orden de importancia:

1. **El merge.** El endpoint toca SOLO los flags del body. Si pisara el
   `defaults_json` entero se llevaría puestos `model`, `timeout` y
   `github_project` — es el mismo bug que ya mordió con `night_config`
   y por el que existe `_merge_defaults`.
2. **La validación es previa a escribir.** Un body con un flag malo no
   deja el proyecto a medio aplicar.
3. **Que el flag efectivamente llegue al run**: un checkbox que no
   cambia el comportamiento es peor que no tener checkbox.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from relay import file_tools
from relay.db import Database


class FlagsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name,
            "defaults_json": {"model": "minimax:MiniMax-M3", "timeout": 900}})
        from relay.server import create_app
        self.client = TestClient(TestServer(create_app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _flags(self) -> dict:
        r = await self.client.get("/admin/api/projects/demo/flags")
        self.assertEqual(r.status, 200)
        return (await r.json())["flags"]

    # ---------- 1. leer el estado ----------

    async def test_get_trae_todos_los_flags_con_su_default(self) -> None:
        flags = await self._flags()
        self.assertTrue(flags["sandbox"]["valor"])
        self.assertTrue(flags["native_files"]["valor"])
        self.assertFalse(flags["read_only"]["valor"])
        self.assertEqual(flags["skills_mode"]["valor"], "embed")
        self.assertEqual(flags["rutas_extra"]["valor"], [])

    async def test_default_vs_fijo_se_distinguen(self) -> None:
        """Un flag en su default y uno puesto a mano en el mismo valor se
        ven igual, pero significan cosas distintas el día que el default
        del relay cambie."""
        flags = await self._flags()
        self.assertFalse(flags["sandbox"]["explicito"])
        await self.client.patch("/admin/api/projects/demo/flags",
                                json={"sandbox": True})
        flags = await self._flags()
        self.assertTrue(flags["sandbox"]["explicito"])
        self.assertTrue(flags["sandbox"]["valor"])

    async def test_cada_flag_trae_tipo_y_descripcion(self) -> None:
        """La UI arma el control desde el tipo; sin descripción, un
        checkbox llamado `native_files` no le dice nada a nadie."""
        for nombre, f in (await self._flags()).items():
            self.assertIn(f["tipo"],
                          ("bool", "list", "model", "enum:embed,compact"),
                          nombre)
            self.assertTrue(f["descripcion"], nombre)

    async def test_proyecto_inexistente_404(self) -> None:
        r = await self.client.get("/admin/api/projects/fantasma/flags")
        self.assertEqual(r.status, 404)
        r = await self.client.patch("/admin/api/projects/fantasma/flags",
                                    json={"sandbox": False})
        self.assertEqual(r.status, 404)

    # ---------- 2. el merge (lo que más importa) ----------

    async def test_patch_no_pisa_las_claves_que_no_son_flags(self) -> None:
        """El bug que este diseño evita, escrito como test.

        `model` y `timeout` viven en el mismo blob. Un PATCH que
        escribiera `defaults_json` entero los borraría, y el proyecto
        pasaría a correr con el modelo global sin que nadie lo note.
        """
        await self.client.patch("/admin/api/projects/demo/flags",
                                json={"sandbox": False})
        d = (await self.db.get_project("demo"))["defaults_json"]
        self.assertEqual(d["model"], "minimax:MiniMax-M3")
        self.assertEqual(d["timeout"], 900)
        self.assertFalse(d["sandbox"])

    async def test_patch_no_pisa_otros_flags(self) -> None:
        """Dos pestañas abiertas no se pisan lo que la otra tocó."""
        await self.client.patch("/admin/api/projects/demo/flags",
                                json={"read_only": True})
        await self.client.patch("/admin/api/projects/demo/flags",
                                json={"sandbox": False})
        d = (await self.db.get_project("demo"))["defaults_json"]
        self.assertTrue(d["read_only"])
        self.assertFalse(d["sandbox"])

    async def test_null_vuelve_al_default_borrando_la_clave(self) -> None:
        """Volver al default es BORRAR la clave, no escribir el default.

        Si se escribiera el valor, el proyecto quedaría clavado a ese
        número el día que cambie el default del relay.
        """
        await self.client.patch("/admin/api/projects/demo/flags",
                                json={"sandbox": False})
        await self.client.patch("/admin/api/projects/demo/flags",
                                json={"sandbox": None})
        d = (await self.db.get_project("demo"))["defaults_json"]
        self.assertNotIn("sandbox", d)
        self.assertFalse((await self._flags())["sandbox"]["explicito"])

    # ---------- 3. validación ----------

    async def test_flag_desconocido_es_400_y_lista_los_validos(self) -> None:
        r = await self.client.patch("/admin/api/projects/demo/flags",
                                    json={"volar": True})
        self.assertEqual(r.status, 400)
        cuerpo = await r.json()
        self.assertIn("volar", cuerpo["error"])
        self.assertIn("sandbox", cuerpo["flags"])

    async def test_un_bool_no_acepta_string(self) -> None:
        r = await self.client.patch("/admin/api/projects/demo/flags",
                                    json={"sandbox": "false"})
        self.assertEqual(r.status, 400)

    async def test_enum_solo_acepta_sus_opciones(self) -> None:
        r = await self.client.patch("/admin/api/projects/demo/flags",
                                    json={"skills_mode": "gigante"})
        self.assertEqual(r.status, 400)
        self.assertIn("compact", (await r.json())["error"])

    async def test_una_lista_no_acepta_string(self) -> None:
        r = await self.client.patch("/admin/api/projects/demo/flags",
                                    json={"rutas_extra": "/a,/b"})
        self.assertEqual(r.status, 400)

    async def test_las_listas_se_limpian(self) -> None:
        r = await self.client.patch(
            "/admin/api/projects/demo/flags",
            json={"rutas_extra": ["  /a  ", "", "   ", "/b"]})
        self.assertEqual(r.status, 200)
        self.assertEqual(
            (await r.json())["flags"]["rutas_extra"]["valor"], ["/a", "/b"])

    async def test_un_body_invalido_no_aplica_nada(self) -> None:
        """Validar todo antes de escribir: media edición de permisos
        aplicada es peor que ninguna."""
        r = await self.client.patch(
            "/admin/api/projects/demo/flags",
            json={"read_only": True, "skills_mode": "gigante"})
        self.assertEqual(r.status, 400)
        d = (await self.db.get_project("demo"))["defaults_json"]
        self.assertNotIn("read_only", d)

    async def test_body_que_no_es_objeto(self) -> None:
        r = await self.client.patch("/admin/api/projects/demo/flags",
                                    json=["sandbox"])
        self.assertEqual(r.status, 400)

    async def test_body_vacio_es_un_no_op_valido(self) -> None:
        """Guardar sin cambios no debería ser un error."""
        r = await self.client.patch("/admin/api/projects/demo/flags", json={})
        self.assertEqual(r.status, 200)

    async def test_json_roto(self) -> None:
        r = await self.client.patch(
            "/admin/api/projects/demo/flags", data="{no json",
            headers={"content-type": "application/json"})
        self.assertEqual(r.status, 400)

    # ---------- 4. el flag llega al run ----------

    async def test_apagar_el_sandbox_desde_la_ui_llega_a_los_permisos(self) -> None:
        """El camino entero: checkbox → defaults_json → Permisos del run.

        Un checkbox que no cambia el comportamiento es peor que no tener
        checkbox: da la sensación de haber configurado algo.
        """
        from relay import experts, files

        await self.client.patch("/admin/api/projects/demo/flags",
                                json={"sandbox": False})
        defaults = (await self.db.get_project("demo"))["defaults_json"]
        abierto, por_que = file_tools.sandbox_abierto(defaults)
        self.assertTrue(abierto)
        self.assertIn("defaults_json", por_que)

        perm = files.Permisos.para(self._tmp.name, abierto=abierto)
        afuera = Path(tempfile.gettempdir()) / "flags-e2e.txt"
        afuera.write_text("x")
        self.assertEqual(perm.resolve(str(afuera)).name, "flags-e2e.txt")

    async def test_las_rutas_extra_de_la_ui_llegan_al_run(self) -> None:
        from relay import experts

        otro = Path(self._tmp.name) / "otro"
        otro.mkdir()
        await self.client.patch("/admin/api/projects/demo/flags",
                                json={"rutas_extra": [str(otro)]})
        defaults = (await self.db.get_project("demo"))["defaults_json"]
        self.assertIn(str(otro), file_tools.rutas_extra(defaults))

    async def test_read_only_de_la_ui_apaga_la_escritura(self) -> None:
        from relay import files

        await self.client.patch("/admin/api/projects/demo/flags",
                                json={"read_only": True})
        defaults = (await self.db.get_project("demo"))["defaults_json"]
        perm = files.Permisos.para(self._tmp.name,
                                   read_only=bool(defaults.get("read_only")))
        with self.assertRaises(files.SinPermiso):
            files.escribir(perm, "x.txt", "y")

    async def test_el_blob_queda_json_serializable(self) -> None:
        """Se guarda en SQLite como texto; un valor raro rompería el boot."""
        await self.client.patch(
            "/admin/api/projects/demo/flags",
            json={"sandbox": False, "rutas_extra": ["/a"],
                  "skills_mode": "compact"})
        d = (await self.db.get_project("demo"))["defaults_json"]
        json.dumps(d)
