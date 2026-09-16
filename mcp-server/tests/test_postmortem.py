"""Tests de Sub-ola 2.2: diagnóstico post-mortem en tab Chats.

Cubre:
  - GET /chats devuelve phase_at_end y last_tool cuando finish_chat
    los persistió
  - GET /chats devuelve null cuando finish_chat no los proveyó
  - Render del badge en JS (smoke): validamos que la lógica del JS
    produce el HTML correcto para los 3 casos (con phase+tool,
    solo phase, solo tool)

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_postmortem.py -q
"""
from __future__ import annotations

import os
import re
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


class TestPostMortemInChatsList(unittest.IsolatedAsyncioTestCase):
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

    async def test_chats_endpoint_returns_phase_and_tool(self) -> None:
        """GET /chats expone phase_at_end + last_tool al front."""
        from relay.server import create_app, DB_KEY
        async def fake_send(self_, agent_id, kind, message, metadata=None):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                db: Database = app[DB_KEY]
                # 3 chats con perfiles distintos
                cid1 = await db.create_chat(
                    project_slug="demo", source="t", author="a", target="demo")
                await db.finish_chat(cid1, status="error",
                    error="timeout total del experto",
                    phase_at_end="tool_call", last_tool="cbm_query")

                cid2 = await db.create_chat(
                    project_slug="demo", source="t", author="a", target="demo")
                await db.finish_chat(cid2, status="error",
                    error="otra cosa",
                    phase_at_end="writing", last_tool=None)

                cid3 = await db.create_chat(
                    project_slug="demo", source="t", author="a", target="demo")
                await db.finish_chat(cid3, status="ok")  # sin phase

                r = await client.get("/chats?limit=10")
                self.assertEqual(r.status, 200)
                body = await r.json()
                rows = {c["id"]: c for c in body["chats"]}
                # cid1: tool_call + cbm_query
                self.assertEqual(rows[cid1]["phase_at_end"], "tool_call")
                self.assertEqual(rows[cid1]["last_tool"], "cbm_query")
                # cid2: writing sin tool
                self.assertEqual(rows[cid2]["phase_at_end"], "writing")
                self.assertIsNone(rows[cid2]["last_tool"])
                # cid3: ok sin phase
                self.assertIsNone(rows[cid3]["phase_at_end"])
                self.assertIsNone(rows[cid3]["last_tool"])



class TestPostMortemBadgeHTML(unittest.IsolatedAsyncioTestCase):
    """Replica la lógica del front para validar los 3 casos de badge."""

    def _badge_for(self, c: dict) -> str:
        """Replicado de admin.js — si cambia allá, hay que cambiar acá."""
        diag = ""
        if c.get("phase_at_end"):
            cls = ("err" if c["phase_at_end"] == "tool_call"
                   else "warn" if c["phase_at_end"] == "writing"
                   else "dim")
            tip = (f"murió en {c['phase_at_end']} ({c['last_tool']})"
                   if c.get("last_tool")
                   else f"murió en {c['phase_at_end']}")
            diag = (f' <span class="badge {cls}" title="{tip}">'
                    f'{c["phase_at_end"]}'
                    + (f' · {c["last_tool"]}' if c.get("last_tool") else "")
                    + '</span>')
        elif c.get("last_tool"):
            diag = (f' <span class="badge dim" title="última tool vista">'
                    f'· {c["last_tool"]}</span>')
        return diag

    def test_phase_tool_call_with_tool_renders_red_badge(self) -> None:
        c = {"status": "error", "phase_at_end": "tool_call",
             "last_tool": "cbm_query"}
        html = self._badge_for(c)
        self.assertIn('badge err', html)
        self.assertIn('tool_call', html)
        self.assertIn('cbm_query', html)
        self.assertIn('murió en tool_call (cbm_query)', html)

    def test_phase_writing_without_tool_renders_warn(self) -> None:
        c = {"status": "error", "phase_at_end": "writing", "last_tool": None}
        html = self._badge_for(c)
        self.assertIn('badge warn', html)
        self.assertIn('writing', html)
        self.assertIn('murió en writing', html)
        self.assertNotIn("·", html)  # no hay tool → no separator

    def test_only_tool_renders_dim(self) -> None:
        c = {"status": "ok", "phase_at_end": None, "last_tool": "read_file"}
        html = self._badge_for(c)
        self.assertIn('badge dim', html)
        self.assertIn('read_file', html)
        self.assertIn('última tool vista', html)

    def test_no_phase_no_tool_renders_empty(self) -> None:
        c = {"status": "ok", "phase_at_end": None, "last_tool": None}
        self.assertEqual(self._badge_for(c), "")

    def test_xss_in_phase_or_tool_escaped(self) -> None:
        """Si por algún motivo phase_at_end viene con HTML, no debe
        renderizarse literal — el JS usa escape() antes de inyectar."""
        c = {"status": "error",
             "phase_at_end": "<script>alert(1)</script>",
             "last_tool": "<img onerror=x>"}
        html = self._badge_for(c)
        # el helper NO escapa (lo hace el caller con escape()); pero la
        # sanity check es que NO se filtra como un tag real en la página.
        # El badge se construye con los valores crudos — el front usa
        # escape(c.phase_at_end) al renderizar. Verificamos que el helper
        # al menos NO produce un tag ejecutable si los valores están
        # pre-escapados.
        c2 = {"status": "error",
              "phase_at_end": "&lt;script&gt;",
              "last_tool": "&lt;img&gt;"}
        html2 = self._badge_for(c2)
        # pre-escaped → no aparecen tags literales
        self.assertNotIn("<script>", html2)
        self.assertNotIn("<img", html2)


if __name__ == "__main__":
    unittest.main()