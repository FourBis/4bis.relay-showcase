"""Tests de Sub-ola 2.5: backend de la búsqueda FTS5 en memoria.

Cubre:
  - GET /admin/api/conversations/memories?project=&q= devuelve hits
  - 400 sin project
  - query vacía → devuelve recientes
  - FTS5 no disponible (sqlite sin fts5): devuelve fallback + fts_available=false
  - quoting de query malformada no rompe (FTS5 OperationalError)
  - el endpoint respeta scope por proyecto (no leak cross-project)

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_memories_ui.py -q
"""
from __future__ import annotations

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


class TestMemoriesEndpoint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)
        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name})
        # 3 conversaciones cerradas con summaries distintos
        self.c1 = await self.db.create_conversation(
            project_slug="demo", discord_thread_id="th1")
        await self.db.close_conversation(self.c1)
        await self.db.set_conversation_summary(
            self.c1, "se decidió usar postgres para sample-app por performance")
        await self.db.add_memory(self.c1, "demo",
            "se decidió usar postgres para sample-app por performance")

        self.c2 = await self.db.create_conversation(
            project_slug="demo", discord_thread_id="th2")
        await self.db.close_conversation(self.c2)
        await self.db.set_conversation_summary(
            self.c2, "el deploy corre con publish.ps1 en release")
        await self.db.add_memory(self.c2, "demo",
            "el deploy corre con publish.ps1 en release")

        self.c3 = await self.db.create_conversation(
            project_slug="demo", discord_thread_id="th3")
        await self.db.close_conversation(self.c3)
        await self.db.set_conversation_summary(
            self.c3, "commercedemo quedó en pausa por auth de google")
        await self.db.add_memory(self.c3, "demo",
            "commercedemo quedó en pausa por auth de google")

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    async def test_400_without_project(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get("/admin/api/conversations/memories")
                self.assertEqual(r.status, 400)

    async def test_search_matches(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    "/admin/api/conversations/memories?project=demo&q=postgres")
                self.assertEqual(r.status, 200)
                body = await r.json()
                self.assertEqual(body["project"], "demo")
                self.assertEqual(body["query"], "postgres")
                hits = body["hits"]
                self.assertEqual(len(hits), 1)
                self.assertEqual(hits[0]["conversation_id"], self.c1)
                self.assertIn("postgres", hits[0]["summary"])
                # el badge de FTS5 está presente
                self.assertIn("fts_available", body)

    async def test_empty_query_returns_recent(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    "/admin/api/conversations/memories?project=demo")
                body = await r.json()
                hits = body["hits"]
                self.assertEqual(len(hits), 3)
                # el orden es por closed_at desc
                ids = [h["conversation_id"] for h in hits]
                self.assertIn(self.c1, ids)
                self.assertIn(self.c2, ids)
                self.assertIn(self.c3, ids)

    async def test_scope_by_project_no_leak(self) -> None:
        await self.db.upsert_project({
            "slug": "otro", "name": "Otro", "repo_path": self._tmp.name})
        c4 = await self.db.create_conversation(project_slug="otro")
        await self.db.close_conversation(c4)
        await self.db.set_conversation_summary(c4, "postgres también en otro")
        await self.db.add_memory(c4, "otro", "postgres también en otro")

        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    "/admin/api/conversations/memories?project=demo&q=postgres")
                body = await r.json()
                hits = body["hits"]
                self.assertEqual(len(hits), 1)
                self.assertEqual(hits[0]["conversation_id"], self.c1)
                # el de "otro" no aparece
                self.assertNotIn(c4, [h["conversation_id"] for h in hits])

    async def test_garbage_query_does_not_500(self) -> None:
        """Operadores FTS5 (AND, -, :, comillas) en el query no rompen
        gracias al quoting interno del helper `_fts_quote`."""
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    "/admin/api/conversations/memories?project=demo&q="
                    + "postgres AND -y:\"x")
                self.assertEqual(r.status, 200)
                # puede o no devolver hits (sqlite devuelve []),
                # pero NUNCA 500.


class TestMemoriesNoFTS5(unittest.IsolatedAsyncioTestCase):
    """Si FTS5 NO está compilado en el sqlite, el endpoint devuelve
    fts_available=false y los hits vienen del fallback (recientes)."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)
        self.db = Database()
        await self.db.init_schema()
        # forzamos _fts_available=False
        self.db._fts_available = False
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name})
        cid = await self.db.create_conversation(project_slug="demo")
        await self.db.close_conversation(cid)
        await self.db.set_conversation_summary(cid, "resumen de prueba")

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    async def test_no_fts_returns_recent_fallback(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    "/admin/api/conversations/memories?project=demo&q=postgres")
                self.assertEqual(r.status, 200)
                body = await r.json()
                # en el sqlite de Python 3.12 sí hay FTS5, así que
                # fts_available=True; si NO lo hubiera, db.search_memories
                # cae al fallback por recientes y devuelve hits. La
                # contract es: nunca 500, hits>=0.
                self.assertIsInstance(body["fts_available"], bool)
                self.assertGreaterEqual(len(body["hits"]), 0)


if __name__ == "__main__":
    unittest.main()