"""Tests F3 del plan MCP_REGISTRY: endpoints /admin/api/mcp (CRUD).

El install desde GitHub es F2 → acá solo se verifica el stub 501.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_mcp_admin.py -q
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from relay import mcp_pool
from relay.db import Database

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR")


class TestMcpAdminApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name})

        from relay.server import create_app
        self.app = create_app()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp.cleanup()

    async def test_el_wrapper_ya_no_se_siembra(self) -> None:
        """2026-08-16: el wrapper MCP se retiró del catálogo.

        Antes este test verificaba lo contrario (que el boot lo sembrara
        como fila global). Cambió la decisión, no el test: VS Code dejó
        de usarse, sus siete tools son nativas del relay, y tenerlo
        adjunto solo agregaba caps más chicos que ganaban en silencio por
        estar más adentro en la cadena. Ver docs/WRAPPER.md.
        """
        r = await self.client.get("/admin/api/mcp")
        self.assertEqual(r.status, 200)
        body = await r.json()
        names = [m["name"] for m in body["mcp_servers"]]
        self.assertNotIn("4bis-wrapper", names)

    async def test_crud_completo(self) -> None:
        # alta con link a demo
        r = await self.client.post("/admin/api/mcp", json={
            "name": "postgres-demo", "capability": "db",
            "command": "npx", "args": ["-y", "@x/pg"],
            "env": {"DATABASE_URL": "env:DEMO_DSN"},
            "project_slugs": ["demo"],
        })
        self.assertEqual(r.status, 201)
        m = (await r.json())["mcp"]
        self.assertEqual(m["project_slugs"], ["demo"])
        self.assertFalse(m["enabled"])  # default: off hasta handshake

        # toggle enabled por PATCH
        r = await self.client.patch(
            "/admin/api/mcp/postgres-demo", json={"enabled": True})
        self.assertEqual(r.status, 200)
        self.assertTrue((await r.json())["mcp"]["enabled"])

        # PATCH links → global (lista vacía reemplaza)
        r = await self.client.patch(
            "/admin/api/mcp/postgres-demo", json={"project_slugs": []})
        self.assertEqual((await r.json())["mcp"]["project_slugs"], [])

        # delete
        r = await self.client.delete("/admin/api/mcp/postgres-demo")
        self.assertEqual(r.status, 200)
        r = await self.client.delete("/admin/api/mcp/postgres-demo")
        self.assertEqual(r.status, 404)

    async def test_validaciones(self) -> None:
        r = await self.client.post("/admin/api/mcp", json={"name": ""})
        self.assertEqual(r.status, 400)
        # alta nueva sin capability
        r = await self.client.post("/admin/api/mcp", json={"name": "x"})
        self.assertEqual(r.status, 400)
        # env no-objeto
        r = await self.client.post("/admin/api/mcp", json={
            "name": "x", "capability": "db", "env": "no"})
        self.assertEqual(r.status, 400)
        # slug desconocido en links
        r = await self.client.post("/admin/api/mcp", json={
            "name": "x", "capability": "db",
            "project_slugs": ["no-existe"]})
        self.assertEqual(r.status, 400)
        # patch de MCP inexistente
        r = await self.client.patch(
            "/admin/api/mcp/fantasma", json={"enabled": True})
        self.assertEqual(r.status, 404)

    async def test_install_delega_al_installer(self) -> None:
        """F2: /install ya no es stub — arranca un job real. Lo cubrimos
        en test_mcp_install.py con monkeypatches del pipeline; acá
        verificamos solamente que devuelve 202 y un job_id."""
        from unittest.mock import AsyncMock, patch
        from relay.mcp_installer import (
            clone_repo, static_scan, vet_with_llm,
            detect_run_command, run_handshake,
        )
        patches = [
            patch(f"{m.__module__}.{m.__name__}", v) for m, v in [
                (clone_repo, AsyncMock(return_value="abc")),
                (static_scan, AsyncMock(return_value=[])),
                (vet_with_llm, AsyncMock(return_value=("safe", ""))),
                (detect_run_command, lambda _d: {
                    "command": "echo", "args": ["hi"],
                    "env": {}, "needs_manual": False}),
                (run_handshake, AsyncMock(return_value=(True, ""))),
            ]
        ]
        for p in patches:
            p.start()
        try:
            r = await self.client.post(
                "/admin/api/mcp/install",
                json={"url": "https://github.com/org/sanity"})
            self.assertEqual(r.status, 202)
            self.assertIn("job_id", await r.json())
        finally:
            for p in patches:
                p.stop()


class TestMcpHealthRecheck(TestMcpAdminApi):
    """POST /admin/api/mcp/{name}/health — re-chequeo sin reiniciar.

    El `health` se escribía SOLO en el probe del boot: arreglar un MCP
    roto y confirmarlo obligaba a reiniciar el relay entero mientras la
    tabla seguía mostrando `handshake ✗` sobre algo que ya andaba.

    El probe real levanta procesos (npx/uvx), que es justo lo que un test
    no debe hacer — se mockea `probe_handshake`, que es la frontera con
    el mundo exterior. Lo que se verifica es lo nuestro: que el veredicto
    se persista y que el error llegue al humano.
    """

    async def _fila(self) -> dict:
        return await self.db.get_mcp_server("fetch")

    async def test_probe_ok_persiste_el_health(self) -> None:
        from unittest.mock import AsyncMock, patch
        await self.db.upsert_mcp_server(
            {"name": "fetch", "health": "handshake_failed"})
        with patch("relay.mcp_pool.probe_handshake", AsyncMock()):
            r = await self.client.post("/admin/api/mcp/fetch/health")
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body["health"], "ok")
        self.assertEqual(body["error"], "")
        self.assertEqual((await self._fila())["health"], "ok")

    async def test_probe_roto_devuelve_el_error_y_lo_persiste(self) -> None:
        """El error del handshake es LA información para arreglarlo."""
        from unittest.mock import AsyncMock, patch
        await self.db.upsert_mcp_server({"name": "fetch", "health": "ok"})
        boom = AsyncMock(side_effect=ImportError("cannot import McpError"))
        with patch("relay.mcp_pool.probe_handshake", boom):
            r = await self.client.post("/admin/api/mcp/fetch/health")
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body["health"], "handshake_failed")
        self.assertIn("McpError", body["error"])
        self.assertEqual((await self._fila())["health"], "handshake_failed")

    async def test_probe_salta_si_env_var_falta(self) -> None:
        """MCP sembrado con `env:NAME` y NAME sin setear.

        2026-08-28: antes el probe intentaba spawn-ear igual, el
        subprocess moría al instante y el boot arrastraba un warning
        permanente de `handshake_failed`. Ahora devuelve `skipped` y
        NO toca subprocess. El test verifica que `probe_handshake` NO
        se llama (que es lo que evita el spawn) y que el health se
        persiste como `skipped`.
        """
        import os
        from unittest.mock import AsyncMock, patch
        # Sembramos un MCP que necesita POSTGRES_MCP_URI.
        await self.db.upsert_mcp_server({
            "name": "test-pg-skip",
            "capability": "postgres",
            "transport": "stdio",
            "command": "uvx",
            "args": '["--with","mcp<2","postgres-mcp"]',
            "env": '{"DATABASE_URI":"env:POSTGRES_MCP_URI_TEST"}',
            "on_demand": 1, "enabled": 1, "read_only": 1,
        })
        env_saved = os.environ.pop("POSTGRES_MCP_URI_TEST", None)
        try:
            with patch("relay.mcp_pool.probe_handshake",
                       AsyncMock(side_effect=AssertionError(
                           "probe NO debería haberse llamado: "
                           "falta env var"))) as ph:
                health, error = await mcp_pool.probe_and_store_health(
                    self.db,
                    await self.db.get_mcp_server("test-pg-skip"))
            self.assertEqual(health, "skipped")
            self.assertIn("POSTGRES_MCP_URI_TEST", error)
            # probe_handshake NO se llamó.
            ph.assert_not_called()
            # El health quedó persistido como skipped.
            fila = await self.db.get_mcp_server("test-pg-skip")
            self.assertEqual(fila["health"], "skipped")
        finally:
            if env_saved is not None:
                os.environ["POSTGRES_MCP_URI_TEST"] = env_saved

    async def test_mcp_inexistente_da_404(self) -> None:
        r = await self.client.post("/admin/api/mcp/no-existe/health")
        self.assertEqual(r.status, 404)


if __name__ == "__main__":
    unittest.main()
