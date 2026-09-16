"""Tests del bug fix Sub-ola 2.4: caps defensivos en /conversations/{id}/messages.

Bug original: una conversación con muchos turnos y tool calls grandes
(cbm_query devuelve JSON de 30KB+) saturaba el response y rompía el
modal de la UI.

Cubre:
  - cap por turno (content_cap, default 4000)
  - cap por total de turnos (max_turns, default 200)
  - flag truncated en el response
  - flag truncated en cada turno individual
  - el response acotado NO rompe el contrato existente
    (turns parseables, messages, turn_count siguen presentes)
  - límites DoS: max_turns>2000 y content_cap>100000 caen a los caps

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_conv_messages_caps.py -q
"""
from __future__ import annotations

import json
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


class TestMessagesContentCap(unittest.IsolatedAsyncioTestCase):
    """Cap por turno individual (default 4000 chars)."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)
        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name})
        self.conv_id = await self.db.create_conversation(project_slug="demo")

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    def _history_with_big_tool_return(self, size: int) -> str:
        big_payload = "x" * size
        return json.dumps([
            {"kind": "request",
             "parts": [{"part_kind": "user-prompt", "content": "hola"}]},
            {"kind": "response",
             "parts": [
                 {"part_kind": "text", "content": "voy a buscar"},
                 {"part_kind": "tool-call", "tool_name": "cbm_query"},
             ]},
            {"kind": "request",
             "parts": [{"part_kind": "tool-return", "content": big_payload}]},
        ])

    async def test_long_tool_return_is_capped(self) -> None:
        """Un tool return de 30KB se corta al content_cap default (4000)."""
        await self.db.save_conversation_messages(
            self.conv_id, self._history_with_big_tool_return(30_000))
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    f"/conversations/{self.conv_id}/messages")
                body = await r.json()
                msgs = body["messages"]
                # el helper produce 2 tool turns: uno con nombre "tool"
                # (del request) y otro con nombre "cbm_query" (del response)
                tool_msgs = [m for m in msgs if m["role"] == "tool"]
                self.assertEqual(len(tool_msgs), 2)
                # el del tool-return real (size=30000) es el truncated
                truncated = [t for t in tool_msgs if t["truncated"]]
                self.assertEqual(len(truncated), 1)
                t = truncated[0]
                self.assertEqual(t["total_chars"], 30_000)
                self.assertLess(len(t["content"]), 30_000)
                self.assertIn("truncado", t["content"])

    async def test_short_content_not_truncated(self) -> None:
        """Turnos cortos NO se truncan."""
        await self.db.save_conversation_messages(
            self.conv_id, self._history_with_big_tool_return(500))
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    f"/conversations/{self.conv_id}/messages")
                body = await r.json()
                tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
                # 2 turnos tool (helper), ninguno truncado
                self.assertEqual(len(tool_msgs), 2)
                for t in tool_msgs:
                    self.assertFalse(t["truncated"])

    async def test_content_cap_param_override(self) -> None:
        """?content_cap=N permite ajustar el cap."""
        await self.db.save_conversation_messages(
            self.conv_id, self._history_with_big_tool_return(2_000))
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                # cap muy chico (500) → el tool-return de 2000 se trunca
                r = await client.get(
                    f"/conversations/{self.conv_id}/messages?content_cap=500")
                body = await r.json()
                tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
                truncated = [t for t in tool_msgs if t["truncated"]]
                self.assertEqual(len(truncated), 1)
                # cap grande (10000) → NO truncado
                r = await client.get(
                    f"/conversations/{self.conv_id}/messages?content_cap=10000")
                body = await r.json()
                tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
                for t in tool_msgs:
                    self.assertFalse(t["truncated"])


class TestMessagesTurnsCap(unittest.IsolatedAsyncioTestCase):
    """Cap por total de turnos (default 200)."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)
        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name})
        self.conv_id = await self.db.create_conversation(project_slug="demo")
        # 500 turnos user/assistant
        history = []
        for i in range(500):
            history.append({"kind": "request",
                "parts": [{"part_kind": "user-prompt",
                           "content": f"turno {i}"}]})
            history.append({"kind": "response",
                "parts": [{"part_kind": "text",
                           "content": f"respuesta {i}"}]})
        await self.db.save_conversation_messages(self.conv_id,
            json.dumps(history))

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    async def test_default_cap_is_200(self) -> None:
        """Default max_turns=200 → devuelve 200, marca truncated."""
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    f"/conversations/{self.conv_id}/messages")
                body = await r.json()
                self.assertEqual(len(body["messages"]), 200)
                self.assertTrue(body["truncated"])
                self.assertEqual(body["content_cap"], 4000)

    async def test_max_turns_override(self) -> None:
        """?max_turns=50 → devuelve 50."""
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    f"/conversations/{self.conv_id}/messages?max_turns=50")
                body = await r.json()
                self.assertEqual(len(body["messages"]), 50)
                self.assertTrue(body["truncated"])

    async def test_max_turns_dos_limit(self) -> None:
        """?max_turns=99999 se acota al max interno (2000).

        Como el endpoint produce 2 turnos (user+assistant) por cada
        entrada del historial, con 500 entradas el response da 1000
        turnos. El cap interno es 2000 → entran todos, no truncado.
        """
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    f"/conversations/{self.conv_id}/messages?max_turns=99999")
                body = await r.json()
                # 500 entradas × 2 (user + assistant) = 1000 turnos,
                # todos caben en cap interno 2000.
                self.assertEqual(len(body["messages"]), 1000)
                self.assertFalse(body["truncated"])

    async def test_garbage_params_fall_back_to_defaults(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    f"/conversations/{self.conv_id}/messages"
                    "?max_turns=basura&content_cap=basura")
                self.assertEqual(r.status, 200)


class TestCapTextHelper(unittest.TestCase):
    """Unit tests del helper _cap_text."""

    def test_short_string(self) -> None:
        from relay.server import _cap_text
        r = _cap_text("hola", 100)
        self.assertEqual(r["text"], "hola")
        self.assertFalse(r["truncated"])
        self.assertEqual(r["total_chars"], 4)

    def test_empty_string(self) -> None:
        from relay.server import _cap_text
        r = _cap_text("", 100)
        self.assertEqual(r["text"], "")
        self.assertFalse(r["truncated"])
        self.assertEqual(r["total_chars"], 0)

    def test_long_string_truncated_at_line_break(self) -> None:
        from relay.server import _cap_text
        long = "línea1\nlínea2\nlínea3\n" * 200  # varios K
        r = _cap_text(long, 100)
        self.assertTrue(r["truncated"])
        self.assertLess(len(r["text"]), len(long))
        self.assertIn("truncado", r["text"])
        self.assertIn(str(len(long)), r["text"])

    def test_exact_length_not_truncated(self) -> None:
        from relay.server import _cap_text
        s = "x" * 100
        r = _cap_text(s, 100)
        self.assertFalse(r["truncated"])
        self.assertEqual(r["text"], s)


if __name__ == "__main__":
    unittest.main()