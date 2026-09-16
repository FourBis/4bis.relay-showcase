"""Tests del notes-workspace (iter 9.8).

Cubrir:
  - db.ensure_notes_project: idempotente, no pisa proyectos reales
  - experts.run_expert con project[slug]=notes: sin tools, sin cbm,
    sin git diff, system_prompt básico.
  - admin endpoint POST /admin/api/notes → 202 con id
  - admin endpoint GET /admin/api/notes → lista con target=notes
  - compat alias /admin/api/consults → mismo shape que /notes

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_notes_workspace.py -q
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from relay.db import Database

_ENV_KEYS = (
    "FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR",
)


def _tmp_env(tmp: Path) -> None:
    os.environ["FOURBIS_DB_PATH"] = str(tmp / "test.db")
    os.environ["FOURBIS_CHATS_DIR"] = str(tmp / "chats")
    os.environ["FOURBIS_JSONL_DIR"] = str(tmp / "jsonl")


def _restore_env(backup: dict) -> None:
    for k, v in backup.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


# ---------- DB layer ----------


class TestNotesWorkspaceDb(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._env_backup = {k: os.environ.get(k) for k in _ENV_KEYS}
        _tmp_env(base)
        self.db = Database()
        await self.db.init_schema()

    async def asyncTearDown(self) -> None:
        _restore_env(self._env_backup)
        self._tmp.cleanup()

    async def test_ensure_notes_idempotent(self) -> None:
        """ensure_notes_project corre 2 veces → solo 1 fila."""
        a = await self.db.ensure_notes_project()
        b = await self.db.ensure_notes_project()
        assert a["slug"] == "notes"
        assert b["slug"] == "notes"
        rows = await self.db.run("SELECT slug FROM projects WHERE slug=?",
                                 ("notes",))
        assert len(rows) == 1

    async def test_ensure_notes_path_set(self) -> None:
        """El proyecto notes debe tener repo_path no-vacío + system_prompt."""
        proj = await self.db.ensure_notes_project()
        assert proj["repo_path"]
        assert proj["system_prompt"]
        assert "nota" in proj["system_prompt"].lower() or \
               "decisión" in proj["system_prompt"].lower() or \
               "bitácora" in proj["system_prompt"].lower()

    async def test_ensure_notes_no_mcp_no_cbm(self) -> None:
        """El proyecto notes NO debe tener mcp_servers (sin tools)."""
        proj = await self.db.ensure_notes_project()
        assert proj["mcp_servers"] == []
        assert proj["native_tools"] == []


# ---------- admin endpoints ----------


class TestNotesAdminEndpoints(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._env_backup = {k: os.environ.get(k) for k in _ENV_KEYS}
        _tmp_env(base)
        self.db = Database()
        await self.db.init_schema()
        from relay.server import create_app
        app = create_app()
        self._test_server = TestServer(app)
        self.client = TestClient(self._test_server)

    async def asyncTearDown(self) -> None:
        await self._test_server.close()
        _restore_env(self._env_backup)
        self._tmp.cleanup()

    async def _client_ctx(self):
        """Async context: server up, clean state, yield self.client."""
        await self.client.start_server()
        await self.db.ensure_notes_project()
        return self.client

    async def test_notes_list_empty(self) -> None:
        """GET /admin/api/notes sin filas → notes=[], count=0."""
        await self._client_ctx()
        r = await self.client.get("/admin/api/notes")
        assert r.status == 200
        data = await r.json()
        assert data["notes"] == []
        assert data["count"] == 0

    async def test_consults_alias_is_removed(self) -> None:
        """Iter 9.10: /admin/api/consults ya no existe (alias legacy
        removido). El único endpoint del notes-workspace es /notes.
        """
        await self._client_ctx()
        r = await self.client.get("/admin/api/consults")
        assert r.status == 404
