"""Tests de Sub-ola 2.4: backend para la tab Conversaciones.

Cubre:
  - GET /conversations/{id}/messages parsea user/assistant/tool
  - GET /conversations/{id}/messages con messages_json vacío → []
  - GET /conversations/{id}/messages con JSON corrupto → 200 + warning
  - GET /admin/api/conversations/facts?project=... lista facts
  - GET /admin/api/conversations/facts sin project → 400

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_conversations_ui.py -q
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


class TestConvMessages(unittest.IsolatedAsyncioTestCase):
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

    async def test_empty_messages_returns_empty_list(self) -> None:
        conv_id = await self.db.create_conversation(project_slug="demo")
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(f"/conversations/{conv_id}/messages")
                self.assertEqual(r.status, 200)
                body = await r.json()
                self.assertEqual(body["messages"], [])
                self.assertEqual(body["turn_count"], 0)

    async def test_parses_user_and_assistant(self) -> None:
        conv_id = await self.db.create_conversation(project_slug="demo")
        # historial con 2 turnos (user + assistant)
        history = json.dumps([
            {"kind": "request",
             "parts": [{"part_kind": "user-prompt", "content": "hola"}]},
            {"kind": "response",
             "parts": [{"part_kind": "text", "content": "chau"}]},
        ])
        await self.db.save_conversation_messages(conv_id, history)
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(f"/conversations/{conv_id}/messages")
                self.assertEqual(r.status, 200)
                body = await r.json()
                msgs = body["messages"]
                self.assertEqual(len(msgs), 2)
                self.assertEqual(msgs[0]["role"], "user")
                self.assertEqual(msgs[0]["content"], "hola")
                self.assertEqual(msgs[1]["role"], "assistant")
                self.assertEqual(msgs[1]["content"], "chau")

    async def test_parses_tool_calls(self) -> None:
        conv_id = await self.db.create_conversation(project_slug="demo")
        history = json.dumps([
            {"kind": "response",
             "parts": [
                 {"part_kind": "text", "content": "voy a buscar"},
                 {"part_kind": "tool-call", "tool_name": "cbm_query"},
             ]},
            {"kind": "request",
             "parts": [{"part_kind": "tool-return",
                        "content": "{\"nodes\": 12}"}]},
        ])
        await self.db.save_conversation_messages(conv_id, history)
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(f"/conversations/{conv_id}/messages")
                body = await r.json()
                msgs = body["messages"]
                # assistant text + tool call + tool return
                self.assertGreaterEqual(len(msgs), 3)
                roles = [m["role"] for m in msgs]
                self.assertIn("assistant", roles)
                self.assertIn("tool", roles)
                tool_msgs = [m for m in msgs if m["role"] == "tool"]
                self.assertEqual(tool_msgs[0]["tool_name"], "cbm_query")

    async def test_corrupt_json_does_not_500(self) -> None:
        conv_id = await self.db.create_conversation(project_slug="demo")
        await self.db.save_conversation_messages(conv_id, "{basura no json")
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(f"/conversations/{conv_id}/messages")
                self.assertEqual(r.status, 200)
                body = await r.json()
                self.assertEqual(body["messages"], [])
                self.assertIn("error", body)

    async def test_404_when_conv_missing(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get("/conversations/no-existe/messages")
                self.assertEqual(r.status, 404)


class TestFactsEndpoint(unittest.IsolatedAsyncioTestCase):
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

    async def test_400_without_project(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get("/admin/api/conversations/facts")
                self.assertEqual(r.status, 400)

    async def test_lists_facts_for_project(self) -> None:
        await self.db.add_facts(
            "demo", ["usa postgres", "deploy con publish.ps1"],
            source_conversation="c1")
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    "/admin/api/conversations/facts?project=demo")
                self.assertEqual(r.status, 200)
                body = await r.json()
                self.assertEqual(body["project"], "demo")
                self.assertEqual(len(body["facts"]), 2)
                facts_text = [f["fact"] for f in body["facts"]]
                self.assertIn("usa postgres", facts_text)
                self.assertIn("deploy con publish.ps1", facts_text)

    async def test_other_project_not_listed(self) -> None:
        await self.db.add_facts("demo", ["fact de demo"], source_conversation="c1")
        await self.db.add_facts("otro", ["fact de otro"], source_conversation="c2")
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    "/admin/api/conversations/facts?project=demo")
                body = await r.json()
                facts_text = [f["fact"] for f in body["facts"]]
                self.assertIn("fact de demo", facts_text)
                self.assertNotIn("fact de otro", facts_text)


if __name__ == "__main__":
    unittest.main()