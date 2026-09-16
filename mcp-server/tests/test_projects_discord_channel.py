"""Tests del iter 10.1: canal Discord default por proyecto.

Cubre:
  - projects schema: columna discord_channel_id persiste y se lee.
  - db.set_project_discord_channel (helper low-level): upsert y clear.
  - PATCH /projects/{slug}/discord-channel: string no-vacío → upsert.
  - PATCH /projects/{slug}/discord-channel: null → clear (200 + cleared=true).
  - PATCH /projects/{slug}/discord-channel: string vacío → 400.
  - PATCH /projects/{slug}/discord-channel: slug desconocido → 404.
  - PATCH /admin/api/projects/{slug}/discord-channel (alias admin).
  - GET /admin/api/projects/{slug} expone discord_channel_id en el JSON.
  - PATCH /admin/api/projects/{slug} acepta discord_channel_id.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_projects_discord_channel.py -v
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from relay.db import Database
from relay.notify import NotifyClient


_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR")


def _tmp_env(tmp: Path) -> None:
    os.environ["FOURBIS_DB_PATH"] = str(tmp / "test.db")
    os.environ["FOURBIS_CHATS_DIR"] = str(tmp / "chats")
    os.environ["FOURBIS_JSONL_DIR"] = str(tmp / "jsonl")


def _clear_env() -> None:
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


# ---------- schema + db helper ----------

class TestDbHelper(unittest.IsolatedAsyncioTestCase):
    """db.set_project_discord_channel: upsert y clear."""

    async def test_set_project_discord_channel_persists_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _tmp_env(Path(tmp))
            try:
                db = Database()
                await db.init_schema()
                # Crear proyecto.
                await db.upsert_project({
                    "slug": "demo",
                    "name": "Demo",
                    "repo_path": str(Path(tmp) / "repo"),
                    "system_prompt": "",
                    "description": "",
                    "enabled": True,
                    "include_in_index": True,
                    "night_mode_enabled": False,
                    "night_config": {},
                    "mcp_servers": [],
                    "defaults_json": {},
                    "native_tools": [],
                })
                # Setear canal.
                ok = await db.set_project_discord_channel(
                    "demo", channel_id="12345")
                self.assertTrue(ok)
                project = await db.get_project("demo")
                self.assertEqual(project["discord_channel_id"], "12345")
            finally:
                _clear_env()

    async def test_set_project_discord_channel_with_null_clears(self) -> None:
        """Helper con clear=True y channel_id=None pone el campo a NULL."""
        with tempfile.TemporaryDirectory() as tmp:
            _tmp_env(Path(tmp))
            try:
                db = Database()
                await db.init_schema()
                await db.upsert_project({
                    "slug": "demo",
                    "name": "Demo",
                    "repo_path": "",
                    "system_prompt": "",
                    "description": "",
                    "enabled": True,
                    "include_in_index": True,
                    "night_mode_enabled": False,
                    "night_config": {},
                    "mcp_servers": [],
                    "defaults_json": {},
                    "native_tools": [],
                })
                await db.set_project_discord_channel("demo", channel_id="12345")
                # Clear.
                ok = await db.set_project_discord_channel(
                    "demo", channel_id=None, clear=True)
                self.assertTrue(ok)
                project = await db.get_project("demo")
                self.assertIsNone(project["discord_channel_id"])
            finally:
                _clear_env()

    async def test_set_project_discord_channel_unknown_slug(self) -> None:
        """Helper devuelve False si el slug no existe (no crea fila)."""
        with tempfile.TemporaryDirectory() as tmp:
            _tmp_env(Path(tmp))
            try:
                db = Database()
                await db.init_schema()
                ok = await db.set_project_discord_channel(
                    "ghost", channel_id="12345")
                self.assertFalse(ok)
            finally:
                _clear_env()


# ---------- endpoint PATCH /projects/{slug}/discord-channel ----------

class _BaseEndpoint(unittest.IsolatedAsyncioTestCase):
    """Boilerplate mínimo: app de aiohttp con db + notify mock.

    Auth: el server chequea RELAY_API_KEY; si está vacío (_check_auth),
    deja pasar. Los tests NO setean la env var, así que auth queda
    desactivada por default (mismo patrón que test_chats_discord_bridge)."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        _tmp_env(tmp)
        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo",
            "name": "Demo",
            "repo_path": str(tmp / "repo"),
            "system_prompt": "",
            "description": "",
            "enabled": True,
            "include_in_index": True,
            "night_mode_enabled": False,
            "night_config": {},
            "mcp_servers": [],
            "defaults_json": {},
            "native_tools": [],
        })

        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            from relay.server import create_app
            self.app = create_app()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self._tmp.cleanup()
        _clear_env()


class TestEndpoint(_BaseEndpoint):
    """PATCH /projects/{slug}/discord-channel: contratos del endpoint."""

    async def test_endpoint_persists_value(self) -> None:
        """String no-vacío → 200 con {slug, discord_channel_id}."""
        r = await self.client.patch(
            "/projects/demo/discord-channel",
            json={"discord_channel_id": "1528183096631890011"})
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertEqual(data["slug"], "demo")
        self.assertEqual(data["discord_channel_id"], "1528183096631890011")

        # Verificar persistencia.
        project = await self.db.get_project("demo")
        self.assertEqual(project["discord_channel_id"], "1528183096631890011")

    async def test_endpoint_with_null_clears(self) -> None:
        """null → 200 con {cleared: true, discord_channel_id: null}."""
        # Primero setear.
        await self.db.set_project_discord_channel("demo", channel_id="999")
        # Clear via endpoint.
        r = await self.client.patch(
            "/projects/demo/discord-channel",
            json={"discord_channel_id": None})
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertTrue(data.get("cleared"))
        self.assertIsNone(data["discord_channel_id"])

        project = await self.db.get_project("demo")
        self.assertIsNone(project["discord_channel_id"])

    async def test_endpoint_with_empty_string_returns_400(self) -> None:
        """String vacío → 400 (ambigüedad entre 'olvidé' y 'limpiar')."""
        r = await self.client.patch(
            "/projects/demo/discord-channel",
            json={"discord_channel_id": ""})
        self.assertEqual(r.status, 400)

    async def test_endpoint_with_non_string_returns_400(self) -> None:
        """Tipo inválido (int, list, etc) → 400."""
        r = await self.client.patch(
            "/projects/demo/discord-channel",
            json={"discord_channel_id": 12345})
        self.assertEqual(r.status, 400)

    async def test_endpoint_with_unknown_slug_returns_404(self) -> None:
        """Slug desconocido → 404 (no crea fila nueva)."""
        r = await self.client.patch(
            "/projects/ghost/discord-channel",
            json={"discord_channel_id": "12345"})
        self.assertEqual(r.status, 404)
        # La fila "ghost" no se creó.
        project = await self.db.get_project("ghost")
        self.assertIsNone(project)

    async def test_get_project_includes_discord_channel_id(self) -> None:
        """GET /admin/api/projects/{slug} expone discord_channel_id en JSON.

        El endpoint admin api_projects_slug serializa el dict del
        proyecto vía _serialize(dict(project)), así que cualquier columna
        nueva fluye automáticamente. Verificamos que el campo está
        presente en la response JSON."""
        # Setear primero.
        await self.db.set_project_discord_channel("demo", channel_id="999")
        r = await self.client.get("/admin/api/projects/demo")
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertIn("project", data)
        self.assertIn("discord_channel_id", data["project"])
        self.assertEqual(data["project"]["discord_channel_id"], "999")


class TestAdminEndpoint(_BaseEndpoint):
    """PATCH /admin/api/projects/{slug}/discord-channel: alias admin."""

    async def test_admin_endpoint_persists_value(self) -> None:
        r = await self.client.patch(
            "/admin/api/projects/demo/discord-channel",
            json={"discord_channel_id": "7777"})
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertEqual(data["slug"], "demo")
        self.assertEqual(data["discord_channel_id"], "7777")

    async def test_admin_endpoint_unknown_slug_returns_404(self) -> None:
        r = await self.client.patch(
            "/admin/api/projects/ghost/discord-channel",
            json={"discord_channel_id": "7777"})
        self.assertEqual(r.status, 404)

    async def test_admin_endpoint_with_null_clears(self) -> None:
        await self.db.set_project_discord_channel("demo", channel_id="888")
        r = await self.client.patch(
            "/admin/api/projects/demo/discord-channel",
            json={"discord_channel_id": None})
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertTrue(data.get("cleared"))
        project = await self.db.get_project("demo")
        self.assertIsNone(project["discord_channel_id"])

    async def test_admin_endpoint_with_empty_string_returns_400(self) -> None:
        r = await self.client.patch(
            "/admin/api/projects/demo/discord-channel",
            json={"discord_channel_id": ""})
        self.assertEqual(r.status, 400)


class TestProjectsPatchChannel(_BaseEndpoint):
    """PATCH /admin/api/projects/{slug} acepta discord_channel_id."""

    async def test_projects_patch_persists_channel(self) -> None:
        r = await self.client.patch(
            "/admin/api/projects/demo",
            json={"discord_channel_id": "5555"})
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertIn("project", data)
        self.assertEqual(data["project"]["discord_channel_id"], "5555")
        project = await self.db.get_project("demo")
        self.assertEqual(project["discord_channel_id"], "5555")

    async def test_projects_patch_with_null_clears_channel(self) -> None:
        await self.db.set_project_discord_channel("demo", channel_id="6666")
        r = await self.client.patch(
            "/admin/api/projects/demo",
            json={"discord_channel_id": None})
        self.assertEqual(r.status, 200)
        project = await self.db.get_project("demo")
        self.assertIsNone(project["discord_channel_id"])


if __name__ == "__main__":
    unittest.main()