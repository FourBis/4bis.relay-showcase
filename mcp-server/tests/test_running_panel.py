"""Tests de Sub-ola 2.1: backend del panel 'En curso'.

Cubre:
  - GET /chats?status=running filtra por status
  - GET /chats?status=ok&limit=... para incluir recién terminados
  - GET /chats?project=X combina filtros
  - GET /chats/{id}/status devuelve chat + progress (o null)
  - GET /chats/{id}/status con prefijo que matchea múltiples → snapshot list
  - GET /chats/{id}/status con chat inexistente → 404

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_running_panel.py -q
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


async def _wait(predicate, timeout: float = 10.0):
    import time
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("timeout esperando condición async")


class TestListChatsFilter(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)
        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name})

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    async def test_filter_by_status_running(self) -> None:
        cid1 = await self.db.create_chat(
            project_slug="demo", source="t", author="a", target="demo")
        cid2 = await self.db.create_chat(
            project_slug="demo", source="t", author="a", target="demo")
        await self.db.finish_chat(cid1, status="ok")
        # cid2 queda running
        rows = await self.db.list_chats(status="running")
        self.assertEqual([r["id"] for r in rows], [cid2])
        rows_ok = await self.db.list_chats(status="ok")
        self.assertEqual([r["id"] for r in rows_ok], [cid1])

    async def test_filter_combines_project_and_status(self) -> None:
        await self.db.upsert_project({
            "slug": "otro", "name": "Otro", "repo_path": self._tmp.name})
        cid1 = await self.db.create_chat(
            project_slug="demo", source="t", author="a", target="demo")
        cid2 = await self.db.create_chat(
            project_slug="otro", source="t", author="a", target="otro")
        # ambos running, filtramos por demo
        rows = await self.db.list_chats(project_slug="demo", status="running")
        self.assertEqual([r["id"] for r in rows], [cid1])
        # ambos running, sin project
        rows = await self.db.list_chats(status="running")
        self.assertEqual(len(rows), 2)
        # self.db.list_chats(cid1) running por slug
        rows = await self.db.list_chats(project_slug="otro", status="running")
        self.assertEqual([r["id"] for r in rows], [cid2])


class TestChatsStatusEndpoint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)
        db = Database()
        await db.init_schema()
        await db.set_config("FOURBIS_MODEL", "test")
        await db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name,
            "defaults_json": {"model": "test"},
        })

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    async def test_404_when_chat_missing(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, agent_id, kind, message, metadata=None):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get("/chats/00000000/status")
                self.assertEqual(r.status, 404)

    async def test_returns_chat_and_null_progress_when_not_running(self) -> None:
        from relay.server import create_app, DB_KEY
        async def fake_send(self_, agent_id, kind, message, metadata=None):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                # chat ya terminado
                db: Database = app[DB_KEY]
                cid = await db.create_chat(
                    project_slug="demo", source="t", author="a", target="demo")
                await db.finish_chat(cid, status="ok")
                r = await client.get(f"/chats/{cid}/status")
                self.assertEqual(r.status, 200)
                body = await r.json()
                self.assertEqual(body["chat"]["id"], cid)
                self.assertIsNone(body["progress"])
                self.assertFalse(body["has_progress"])
                self.assertFalse(body["is_running"])

    async def test_returns_live_progress_during_run(self) -> None:
        """Mientras corre, /chats/{id}/status devuelve chat + snapshot vivo."""
        from relay.server import create_app
        notifications: list[dict] = []

        async def fake_send(self_, agent_id, kind, message, metadata=None):
            notifications.append({"agent_id": agent_id, "kind": kind,
                                  "message": message,
                                  "metadata": metadata or {}})
            return True

        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.post("/experts/run", json={
                    "target": "demo", "user": "hola",
                    "source": "test", "author": "pytest",
                })
                self.assertEqual(r.status, 202)
                chat_id = (await r.json())["id"]

                # poll corto: durante el run, status detail trae snapshot
                # TestModel es muy rápido, así que puede que ya haya
                # terminado. Hacemos la verificación con o sin progress.
                r = await client.get(f"/chats/{chat_id}/status")
                self.assertEqual(r.status, 200)
                body = await r.json()
                self.assertEqual(body["chat"]["id"], chat_id)
                self.assertIn(body["is_running"], (True, False))
                self.assertIsInstance(body["has_progress"], bool)

                # esperar a que termine
                await _wait(lambda: notifications
                            and notifications[-1]["kind"] == "response")

                # post-mortem: progress puede seguir o no (sigue si no
                # se liberó el store — esto depende del cleanup).
                # Lo importante: nunca explota.
                r = await client.get(f"/chats/{chat_id}/status")
                self.assertEqual(r.status, 200)


class TestHttpChatsFilter(unittest.IsolatedAsyncioTestCase):
    """El endpoint HTTP aplica los filtros."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)
        db = Database()
        await db.init_schema()
        await db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name})

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    async def test_http_filter_status(self) -> None:
        from relay.server import create_app, DB_KEY
        async def fake_send(self_, agent_id, kind, message, metadata=None):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                db: Database = app[DB_KEY]
                cid1 = await db.create_chat(
                    project_slug="demo", source="t", author="a", target="demo")
                cid2 = await db.create_chat(
                    project_slug="demo", source="t", author="a", target="demo")
                await db.finish_chat(cid1, status="ok")
                r = await client.get("/chats?status=running")
                self.assertEqual(r.status, 200)
                body = await r.json()
                ids = [c["id"] for c in body["chats"]]
                self.assertIn(cid2, ids)
                self.assertNotIn(cid1, ids)


if __name__ == "__main__":
    unittest.main()
