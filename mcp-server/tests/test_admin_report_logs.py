"""Backend mínimo del refresh de UI 2026-07-20.

Tres additions read-only: /admin/api/report (GROUP BY de chats),
/admin/api/logs (ring buffer en memoria) y git_remote_url en
/admin/api/projects. Justificación en docs/CHANGELOG.md.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_admin_report_logs.py -q
"""
from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from relay import admin as admin_mod
from relay.db import Database

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR")


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        self.db = Database()
        await self.db.init_schema()
        from relay.server import create_app
        self.app = create_app()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp.cleanup()


class TestReport(_Base):
    async def _seed_chat(self, slug: str, author: str, ti: int,
                         to: int) -> None:
        cid = await self.db.create_chat(
            project_slug=slug, source="discord", author=author,
            target=slug)
        await self.db.finish_chat(
            cid, status="ok", tokens_in=ti, tokens_out=to,
            tool_calls=3, duration_ms=1200)

    async def test_report_aggregates_by_project_and_author(self) -> None:
        await self._seed_chat("alpha", "joaco", 1000, 50)
        await self._seed_chat("alpha", "joaco", 2000, 70)
        await self._seed_chat("beta", "maria", 500, 10)
        r = await self.client.get("/admin/api/report?days=7")
        self.assertEqual(r.status, 200)
        data = await r.json()
        projects = {p["project"]: p for p in data["by_project"]}
        self.assertEqual(projects["alpha"]["runs"], 2)
        self.assertEqual(projects["alpha"]["tokens_in"], 3000)
        self.assertEqual(projects["beta"]["tokens_out"], 10)
        authors = {a["author"]: a for a in data["by_author"]}
        self.assertEqual(authors["joaco"]["tokens_in"], 3000)
        self.assertTrue(data["daily"])  # al menos el día de hoy

    async def test_report_filters_by_project(self) -> None:
        await self._seed_chat("alpha", "joaco", 1000, 50)
        await self._seed_chat("beta", "maria", 500, 10)
        r = await self.client.get("/admin/api/report?days=7&project=alpha")
        data = await r.json()
        self.assertEqual(len(data["by_project"]), 1)
        self.assertEqual(data["by_project"][0]["project"], "alpha")


class TestLogs(_Base):
    async def test_logs_capture_and_filter(self) -> None:
        logging.getLogger("relay.test").info("mensaje-info-xyz")
        logging.getLogger("relay.test").warning("mensaje-warn-xyz")
        r = await self.client.get("/admin/api/logs?limit=100")
        data = await r.json()
        msgs = [l["msg"] for l in data["logs"]]
        self.assertIn("mensaje-info-xyz", msgs)
        self.assertIn("mensaje-warn-xyz", msgs)
        # filtro level=WARNING deja afuera el INFO
        r = await self.client.get("/admin/api/logs?level=WARNING")
        data = await r.json()
        msgs = [l["msg"] for l in data["logs"]]
        self.assertNotIn("mensaje-info-xyz", msgs)
        self.assertIn("mensaje-warn-xyz", msgs)


class TestGitRemoteUrl(unittest.TestCase):
    def test_parses_origin_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gitdir = Path(tmp) / ".git"
            gitdir.mkdir()
            (gitdir / "config").write_text(
                '[core]\n\trepositoryformatversion = 0\n'
                '[remote "origin"]\n'
                '\turl = https://github.com/AuroraDemo/demo.git\n'
                '\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
                '[remote "backup"]\n\turl = https://otro/x.git\n',
                encoding="utf-8")
            self.assertEqual(
                admin_mod._git_remote_url(tmp),
                "https://github.com/AuroraDemo/demo.git")

    def test_no_git_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(admin_mod._git_remote_url(tmp))


if __name__ == "__main__":
    unittest.main()
