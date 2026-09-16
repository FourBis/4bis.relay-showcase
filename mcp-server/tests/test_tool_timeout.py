"""Tests de Sub-ola 2.6: panel editable de FOURBIS_MCP_TIMEOUT.

Cubre:
  - GET /admin/api/config expone tool_timeout_s efectivo
  - PUT /admin/api/config/tool-timeout persiste en system_config
  - reset (value=null) borra el override → vuelve al env/default
  - validación de rango (5..600)
  - config.tool_timeout_s() lee en cascada: runtime > env > default
  - los cambios son visibles para el siguiente run_expert
    (via config.tool_timeout_s() hot-reload)

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_tool_timeout.py -q
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from relay import config
from relay.db import Database
from relay.notify import NotifyClient

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR",
             "FOURBIS_MCP_TIMEOUT")


def _tmp_env(tmp: Path) -> None:
    os.environ["FOURBIS_DB_PATH"] = str(tmp / "test.db")
    os.environ["FOURBIS_CHATS_DIR"] = str(tmp / "chats")
    os.environ["FOURBIS_JSONL_DIR"] = str(tmp / "jsonl")


def _clear_env() -> None:
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


class TestToolTimeoutEndpoint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)
        self.db = Database()
        await self.db.init_schema()

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    async def test_get_config_includes_tool_timeout(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get("/admin/api/config/timeouts")
                body = await r.json()
                self.assertIn("tool_timeout_s", body)
                self.assertIn("tool_timeout_default_s", body)
                self.assertEqual(body["tool_timeout_default_s"], 60.0)
                # sin override ni env → cae al default
                self.assertEqual(body["tool_timeout_s"], 60.0)

    async def test_put_persists_override(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.put(
                    "/admin/api/config/tool-timeout",
                    json={"value": 120})
                self.assertEqual(r.status, 200)
                body = await r.json()
                self.assertEqual(body["tool_timeout_s"], 120.0)

                # verificar persistencia
                stored = await self.db.get_config("FOURBIS_MCP_TIMEOUT")
                self.assertEqual(stored, "120")

                # GET /config/timeouts refleja el nuevo valor
                r = await client.get("/admin/api/config/timeouts")
                self.assertEqual((await r.json())["tool_timeout_s"], 120.0)

    async def test_reset_returns_to_default(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                # set 200
                await client.put(
                    "/admin/api/config/tool-timeout",
                    json={"value": 200})
                # reset (null)
                r = await client.put(
                    "/admin/api/config/tool-timeout",
                    json={"value": None})
                body = await r.json()
                # sin env FOURBIS_MCP_TIMEOUT → cae al default 60
                self.assertEqual(body["tool_timeout_s"], 60.0)
                # system_config queda con value=""
                stored = await self.db.get_config("FOURBIS_MCP_TIMEOUT")
                self.assertEqual(stored, "")

    async def test_range_validation(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                # muy bajo
                r = await client.put(
                    "/admin/api/config/tool-timeout",
                    json={"value": 1})
                self.assertEqual(r.status, 400)
                # muy alto
                r = await client.put(
                    "/admin/api/config/tool-timeout",
                    json={"value": 1000})
                self.assertEqual(r.status, 400)
                # bordes OK
                r = await client.put(
                    "/admin/api/config/tool-timeout",
                    json={"value": 5})
                self.assertEqual(r.status, 200)
                r = await client.put(
                    "/admin/api/config/tool-timeout",
                    json={"value": 600})
                self.assertEqual(r.status, 200)

    async def test_env_var_does_not_override_panel_default(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            os.environ["FOURBIS_MCP_TIMEOUT"] = "90"
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get("/admin/api/config/timeouts")
                body = await r.json()
                self.assertEqual(body["tool_timeout_s"], 60.0)

    async def test_hot_reload_after_set(self) -> None:
        """El set debe refrescar config._runtime para que el próximo
        run_expert vea el valor nuevo sin reiniciar."""
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                # antes del set
                self.assertEqual(config.tool_timeout_s(), 60.0)
                # set vía endpoint
                await client.put(
                    "/admin/api/config/tool-timeout",
                    json={"value": 45})
                # hot-reload: el siguiente call ve 45
                self.assertEqual(config.tool_timeout_s(), 45.0)


class TestConfigCascade(unittest.IsolatedAsyncioTestCase):
    """config.tool_timeout_s() en cascada, sin tocar el relay."""

    def setUp(self) -> None:
        # limpiamos el runtime entre tests
        config._runtime.clear()

    def tearDown(self) -> None:
        config._runtime.clear()
        os.environ.pop("FOURBIS_MCP_TIMEOUT", None)

    def test_default_when_nothing_set(self) -> None:
        self.assertEqual(config.tool_timeout_s(), 60.0)

    def test_env_var_is_ignored(self) -> None:
        os.environ["FOURBIS_MCP_TIMEOUT"] = "75"
        self.assertEqual(config.tool_timeout_s(), 60.0)

    def test_runtime_wins_over_env(self) -> None:
        os.environ["FOURBIS_MCP_TIMEOUT"] = "75"
        config.set_runtime_config({"FOURBIS_MCP_TIMEOUT": "30"})
        self.assertEqual(config.tool_timeout_s(), 30.0)

    def test_malformed_runtime_falls_back_to_default(self) -> None:
        os.environ["FOURBIS_MCP_TIMEOUT"] = "75"
        config.set_runtime_config({"FOURBIS_MCP_TIMEOUT": "basura"})
        self.assertEqual(config.tool_timeout_s(), 60.0)

    def test_malformed_env_falls_back_to_default(self) -> None:
        os.environ["FOURBIS_MCP_TIMEOUT"] = "basura"
        self.assertEqual(config.tool_timeout_s(), 60.0)


if __name__ == "__main__":
    unittest.main()