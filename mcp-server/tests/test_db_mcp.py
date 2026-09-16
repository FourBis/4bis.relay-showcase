"""Tests F0 del plan MCP_REGISTRY: catálogo mcp_servers en SQLite.

Cover:
  - schema: tablas nuevas + init_schema idempotente
  - migración one-shot del blob projects.mcp_servers (wrapper global
    sin links; entradas no-wrapper → fila + link al proyecto)
  - CRUD (upsert parcial, delete + cascade de links)
  - selector mcp_servers_for_project (global vs linkeado, enabled,
    filtros por name/capability, always-on siempre entra)

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_db_mcp.py -q
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from relay.db import Database


class TestMcpSchema(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(path=Path(self._tmp.name) / "test.db")
        await self.db.init_schema()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_db_nueva_arranca_con_los_mcps_on_demand(self) -> None:
        """2026-08-16: el boot siembra `context7` y `fetch`, y nada más.

        Dos cambios de decisión encadenados y conviene no perder ninguno:
        el wrapper dejó de sembrarse (sus tools son nativas,
        docs/WRAPPER.md) y en su lugar entran dos MCPs que traen algo que
        el relay NO tiene — docs de librerías al día y traer una URL
        (docs/MCPS.md).

        Lo que este test cuida es que ninguno sea always-on: un MCP
        sembrado que arranca solo es un subprocess que nadie pidió.
        """
        filas = await self.db.list_mcp_servers()
        self.assertEqual(sorted(f["name"] for f in filas),
                         ["context7", "fetch", "postgres-mcp"])
        for f in filas:
            self.assertTrue(f["on_demand"], f"{f['name']} no es on-demand")

    async def test_init_schema_idempotent(self) -> None:
        """Re-bootear no duplica ni resucita: el flag one-shot lo corta."""
        await self.db.init_schema()
        await self.db.init_schema()
        self.assertEqual(sorted(f["name"] for f in await self.db.list_mcp_servers()),
                         ["context7", "fetch", "postgres-mcp"])

    async def test_los_uvx_van_pineados_a_mcp1(self) -> None:
        """`mcp` 2.x rompe a los MCP de Python: sin el pin no levantan.

        2.x sacó `mcp.server.fastmcp` (postgres-mcp) y renombró
        `McpError` → `MCPError` (mcp-server-fetch). `fetch` se sembró sin
        el pin y quedó en `handshake_failed` hasta que se detectó al
        registrar postgres-mcp.
        """
        for f in await self.db.list_mcp_servers():
            if f["command"] != "uvx":
                continue
            self.assertIn("mcp<2", f["args"],
                          f"{f['name']} corre por uvx sin pinear mcp<2")

    async def test_postgres_mcp_toma_el_dsn_por_referencia(self) -> None:
        """La credencial no se guarda: el catálogo lleva la ref al .env."""
        fila = await self.db.get_mcp_server("postgres-mcp")
        self.assertEqual(fila["env"], {"DATABASE_URI": "env:POSTGRES_MCP_URI"})
        # `postgres` y no `database`: esa es del MCP de sqlite, y solo se
        # adjunta uno por capacidad (lo desplazaría en silencio).
        self.assertEqual(fila["capability"], "postgres")
        self.assertIn("--access-mode=restricted", fila["args"])

    async def test_borrar_un_mcp_sembrado_no_lo_resucita(self) -> None:
        """Lo que el flag one-shot compra de verdad.

        Si la siembra corriera en cada boot, borrar un MCP que no querés
        lo traería de vuelta al reiniciar y no habría forma de sacártelo
        de encima.
        """
        await self.db.run("DELETE FROM mcp_servers WHERE name='fetch'")
        await self.db.init_schema()
        self.assertIsNone(await self.db.get_mcp_server("fetch"))

    async def test_la_siembra_no_pisa_lo_configurado_a_mano(self) -> None:
        """Si alguien lo dejó con otra versión o con env propio, gana el suyo."""
        await self.db.run(
            "DELETE FROM system_config WHERE key='mcps_seeded_v1'")
        await self.db.upsert_mcp_server({
            "name": "context7", "capability": "docs", "command": "npx",
            "args": ["-y", "@upstash/context7-mcp@3.0.0"], "enabled": 1})
        await self.db.init_schema()
        fila = await self.db.get_mcp_server("context7")
        self.assertIn("3.0.0", json.dumps(fila["args"]))

    async def test_el_wrapper_se_retira_y_no_resucita(self) -> None:
        """La migración lo saca de una DB vieja, y el flag lo deja afuera.

        Se agrega a mano como lo tendría una instalación previa al
        2026-08-16, se corre el boot, y tiene que desaparecer — pero si
        el humano lo vuelve a agregar (porque volvió a VS Code), el boot
        siguiente lo respeta.
        """
        await self.db.upsert_mcp_server({
            "name": "4bis-wrapper", "capability": "files",
            "command": "python", "args": ["-m", "mcp_wrapper"]})
        await self.db.run(
            "DELETE FROM system_config WHERE key='wrapper_retired'")
        await self.db.init_schema()
        nombres = [f["name"] for f in await self.db.list_mcp_servers()]
        self.assertNotIn("4bis-wrapper", nombres)
        self.assertEqual(await self.db.get_config("wrapper_retired"), "1")

        # Vuelto a agregar a mano: el boot no lo borra de nuevo.
        await self.db.upsert_mcp_server({
            "name": "4bis-wrapper", "capability": "files",
            "command": "python", "args": ["-m", "mcp_wrapper"]})
        await self.db.init_schema()
        self.assertIsNotNone(await self.db.get_mcp_server("4bis-wrapper"))


class TestMcpBlobMigration(unittest.IsolatedAsyncioTestCase):
    """Simula el primer boot con DB legacy: proyectos con blob poblado."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(path=Path(self._tmp.name) / "test.db")
        await self.db.init_schema()
        # Volver al estado pre-migración: flag afuera, catálogo vacío.
        await self.db.run("DELETE FROM mcp_servers")
        await self.db.run(
            "DELETE FROM system_config WHERE key='mcp_blob_migrated'")
        self.project = await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name,
            "mcp_servers": [
                {"name": "4bis-wrapper", "transport": "stdio",
                 "command": "python", "args": ["-m", "mcp_wrapper"],
                 "env": {"FOURBIS_WORKSPACE": self._tmp.name}},
                {"name": "custom-thing", "transport": "http",
                 "url": "http://localhost:9999/mcp"},
            ]})

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_migration_from_blob(self) -> None:
        await self.db.init_schema()  # corre la migración
        # 2026-08-16: el wrapper del blob NO se migra — `_retire_wrapper`
        # lo saca en el mismo boot. Sus tools son nativas del relay.
        self.assertIsNone(await self.db.get_mcp_server("4bis-wrapper"))

        custom = await self.db.get_mcp_server("custom-thing")
        self.assertIsNotNone(custom)
        self.assertEqual(custom["transport"], "http")
        self.assertEqual(custom["url"], "http://localhost:9999/mcp")
        self.assertEqual(
            await self.db.mcp_project_ids(custom["id"]),
            [self.project["id"]])

        # el blob queda intacto (audit/rollback)
        proj = await self.db.get_project("demo")
        self.assertEqual(len(proj["mcp_servers"]), 2)

        # flag → segunda corrida no duplica ni resucita
        await self.db.delete_mcp_server("custom-thing")
        await self.db.init_schema()
        self.assertIsNone(await self.db.get_mcp_server("custom-thing"))
        self.assertEqual(await self.db.list_mcp_servers(), [])

    async def test_migration_tolerates_bad_blob(self) -> None:
        await self.db.run(
            "UPDATE projects SET mcp_servers='no es json' WHERE slug='demo'")
        await self.db.init_schema()  # no explota
        # Un blob roto no rompe el boot; simplemente no migra nada.
        self.assertEqual(await self.db.list_mcp_servers(), [])


class TestMcpCrud(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(path=Path(self._tmp.name) / "test.db")
        await self.db.init_schema()
        self.project = await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name})

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_upsert_roundtrip_and_partial_update(self) -> None:
        m = await self.db.upsert_mcp_server({
            "name": "postgres-demo", "capability": "db",
            "transport": "stdio", "command": "npx",
            "args": ["-y", "@x/pg", "--ro"],
            "env": {"DSN": "env:DEMO_PG_DSN"},
        })
        self.assertEqual(m["args"], ["-y", "@x/pg", "--ro"])
        self.assertEqual(m["env"], {"DSN": "env:DEMO_PG_DSN"})
        self.assertTrue(m["read_only"])   # default
        self.assertTrue(m["on_demand"])   # default
        self.assertFalse(m["enabled"])    # default: apagado hasta handshake
        self.assertEqual(m["health"], "unknown")

        # update parcial: solo toggle enabled, el resto queda
        m2 = await self.db.upsert_mcp_server(
            {"name": "postgres-demo", "enabled": True})
        self.assertTrue(m2["enabled"])
        self.assertEqual(m2["command"], "npx")

    async def test_upsert_requires_name(self) -> None:
        with self.assertRaises(ValueError):
            await self.db.upsert_mcp_server({"capability": "db"})

    async def test_delete_cascades_links(self) -> None:
        m = await self.db.upsert_mcp_server(
            {"name": "x", "capability": "db"})
        await self.db.link_mcp(self.project["id"], m["id"])
        self.assertEqual(
            await self.db.mcp_project_ids(m["id"]), [self.project["id"]])
        self.assertTrue(await self.db.delete_mcp_server("x"))
        self.assertFalse(await self.db.delete_mcp_server("x"))
        rows = await self.db.run("SELECT * FROM project_mcp_servers")
        self.assertEqual(rows, [])

    async def test_link_unlink_idempotent(self) -> None:
        m = await self.db.upsert_mcp_server({"name": "x", "capability": "db"})
        await self.db.link_mcp(self.project["id"], m["id"])
        await self.db.link_mcp(self.project["id"], m["id"])  # no explota
        self.assertEqual(len(await self.db.mcp_project_ids(m["id"])), 1)
        await self.db.unlink_mcp(self.project["id"], m["id"])
        self.assertEqual(await self.db.mcp_project_ids(m["id"]), [])


class TestMcpSelector(unittest.IsolatedAsyncioTestCase):
    """mcp_servers_for_project: la query que alimenta build_toolsets (F1)."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(path=Path(self._tmp.name) / "test.db")
        await self.db.init_schema()
        self.p1 = await self.db.upsert_project(
            {"slug": "p1", "name": "P1", "repo_path": self._tmp.name})
        self.p2 = await self.db.upsert_project(
            {"slug": "p2", "name": "P2", "repo_path": self._tmp.name})
        # Un always-on global de referencia. Hasta el 2026-08-16 este rol
        # lo cubría el `4bis-wrapper` que sembraba el boot; ahora que el
        # catálogo arranca vacío, el test se lo crea.
        self.wrapper = await self.db.upsert_mcp_server({
            "name": "files-global", "capability": "files",
            "on_demand": 0, "enabled": True})
        self.pg = await self.db.upsert_mcp_server({
            "name": "postgres-demo", "capability": "db", "enabled": True})
        self.docs = await self.db.upsert_mcp_server({
            "name": "web-docs", "capability": "docs", "enabled": True})
        self.off = await self.db.upsert_mcp_server({
            "name": "apagado", "capability": "db", "enabled": False})
        # postgres-demo scopeado a p1; web-docs global
        await self.db.link_mcp(self.p1["id"], self.pg["id"])

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _names(self, project, **kw) -> list[str]:
        rows = await self.db.mcp_servers_for_project(project["id"], **kw)
        return [r["name"] for r in rows]

    async def test_no_selection_only_always_on(self) -> None:
        """Sin selección explícita solo entran los always-on."""
        self.assertEqual(await self._names(self.p1), ["files-global"])

    async def test_select_by_capability(self) -> None:
        self.assertEqual(
            await self._names(self.p1, capabilities=["db"]),
            ["files-global", "postgres-demo"])
        # p2 no está linkeado a postgres-demo → no lo ve
        self.assertEqual(
            await self._names(self.p2, capabilities=["db"]),
            ["files-global"])

    async def test_select_by_name(self) -> None:
        self.assertEqual(
            await self._names(self.p1, names=["postgres-demo"]),
            ["files-global", "postgres-demo"])
        # case-insensitive, como los slugs
        self.assertEqual(
            await self._names(self.p1, names=["Postgres-Demo"]),
            ["files-global", "postgres-demo"])

    async def test_global_on_demand_visible_para_todos(self) -> None:
        self.assertIn(
            "web-docs", await self._names(self.p2, capabilities=["docs"]))

    async def test_disabled_never_selected(self) -> None:
        self.assertNotIn(
            "apagado", await self._names(self.p1, names=["apagado"]))

    async def test_name_and_capability_combinan_en_or(self) -> None:
        self.assertEqual(
            await self._names(
                self.p1, names=["web-docs"], capabilities=["db"]),
            ["files-global", "postgres-demo", "web-docs"])


if __name__ == "__main__":
    unittest.main()


class TestSeedMcps(unittest.IsolatedAsyncioTestCase):
    """El catálogo base de MCPs on-demand (2026-08-16, docs/MCPS.md).

    Lo que importa acá no es que existan las dos filas: es que **duerman**
    y que sean **borrables**. Un MCP sembrado que arranca solo es un
    subprocess que nadie pidió, y uno que resucita al reiniciar no hay
    forma de sacárselo de encima.
    """

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(path=Path(self._tmp.name) / "test.db")
        await self.db.init_schema()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_los_dos_entran_apagados_hasta_que_alguien_los_pida(self) -> None:
        for nombre in ("context7", "fetch"):
            fila = await self.db.get_mcp_server(nombre)
            self.assertIsNotNone(fila, nombre)
            self.assertTrue(fila["on_demand"], nombre)
            self.assertTrue(fila["enabled"], nombre)

    async def test_capabilities_sin_choques(self) -> None:
        """Dos MCPs con la misma capability obligan a un desempate que se
        resuelve alfabéticamente — así `use_capability("browser")` nunca
        llegaba a playwright (docs/BROWSER_UNICO.md)."""
        caps = [f["capability"] for f in await self.db.list_mcp_servers()]
        self.assertEqual(len(caps), len(set(caps)), caps)

    async def test_no_se_siembra_nada_que_ya_sea_nativo(self) -> None:
        """El criterio de entrada: traer algo que el relay NO tenga.

        Filesystem, shell y SQL son nativos; sembrar un MCP que los
        duplique es volver al problema del wrapper.
        """
        caps = {f["capability"] for f in await self.db.list_mcp_servers()}
        self.assertFalse(caps & {"files", "db", "shell"}, caps)

    async def test_los_comandos_son_los_esperados(self) -> None:
        """Si el paquete cambia de nombre, que falle acá y no en un run."""
        c7 = await self.db.get_mcp_server("context7")
        self.assertEqual(c7["command"], "npx")
        self.assertIn("@upstash/context7-mcp", json.dumps(c7["args"]))
        fe = await self.db.get_mcp_server("fetch")
        self.assertEqual(fe["command"], "uvx")
        self.assertIn("mcp-server-fetch", json.dumps(fe["args"]))
