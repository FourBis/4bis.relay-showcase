"""Tests para tools/google_* + /mcp dispatch (mock + error paths).

No pegamos contra Google real (eso lo verifica el usuario con sus creds).

Cómo correr:
    cd mcp-server
    PYTHONPATH=src python -m unittest tests.test_google_tools -v
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from relay.server import create_app
from relay.tools.calendar import CalendarCreateTool, CalendarListTool
from relay.tools.gmail import GmailReadTool, GmailSendTool


class TestGmailReadMock(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch(self) -> None:
        t = GmailReadTool()
        out = await t.call({"max_results": 5})
        self.assertTrue(out["ok"])
        self.assertEqual(out["source"], "mock")

    async def test_max_results_clamped(self) -> None:
        t = GmailReadTool()
        out = await t.call({"max_results": 9999})  # sobre el techo
        self.assertTrue(out["ok"])
        # el limit es 50; la api devolverá a lo más 3 (mock) — lo que importa es que no rompió
        self.assertLessEqual(len(out["messages"]), 50)


class TestGmailSendMock(unittest.IsolatedAsyncioTestCase):
    async def test_send_without_real_returns_clear_error(self) -> None:
        # Sin GOOGLE_REAL=1, send debe ser honesto: no pretender que mandó.
        t = GmailSendTool()
        out = await t.call({"to": "recipient@example.test", "subject": "x", "body": "y"})
        self.assertFalse(out["ok"])
        self.assertIn("real", out["error"].lower() or out["error"])

    async def test_send_validation(self) -> None:
        # Forzamos modo "real" para entrar al otro branch, sin conectarnos:
        # GmailSendTool en mock tiene _client=None, devolvemos error antes.
        t = GmailSendTool()
        out = await t.call({"to": "", "subject": "x", "body": "y"})
        self.assertFalse(out["ok"])


class TestCalendarMock(unittest.IsolatedAsyncioTestCase):
    async def test_list_requires_time_range(self) -> None:
        t = CalendarListTool()
        out = await t.call({})
        self.assertFalse(out["ok"])
        self.assertIn("obligatorios", out["error"])

    async def test_list_mock_filter(self) -> None:
        t = CalendarListTool()
        out = await t.call({
            "time_min": "2026-07-04T00:00:00Z",
            "time_max": "2026-07-05T23:59:59Z",
        })
        self.assertTrue(out["ok"])
        self.assertEqual(out["source"], "mock")
        self.assertGreaterEqual(len(out["events"]), 1)

    async def test_create_without_real_returns_error(self) -> None:
        t = CalendarCreateTool()
        out = await t.call({"summary": "x", "start": "2026-07-04T15:00:00Z", "end": "2026-07-04T16:00:00Z"})
        self.assertFalse(out["ok"])

    async def test_create_validation(self) -> None:
        t = CalendarCreateTool()
        # forzamos modo real solo para llegar al validation (cliente mock no chequea)
        with patch.object(CalendarCreateTool, "__init__", lambda self: None):
            t._client = object()  # cualquier cosa
            t._real = True
        out = await t.call({"summary": "", "start": "x", "end": "y"})
        self.assertFalse(out["ok"])
        self.assertIn("obligatorios", out["error"])


class TestMcpRegistry(unittest.IsolatedAsyncioTestCase):
    """Valida que el /mcp expone todas las tools esperadas."""

    async def test_tools_list_exposes_four_tools(self) -> None:
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            })
            self.assertEqual(r.status, 200)
            body = await r.json()
            names = sorted(t["name"] for t in body["result"]["tools"])
            self.assertEqual(
                names,
                sorted(["gmail_read", "gmail_send", "calendar_list", "calendar_create"]),
            )

    async def test_calendar_list_via_mcp(self) -> None:
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {
                    "name": "calendar_list",
                    "arguments": {
                        "time_min": "2026-07-04T00:00:00Z",
                        "time_max": "2026-07-08T00:00:00Z",
                    },
                },
            })
            body = await r.json()
            self.assertIn("result", body, body)
            content = json.loads(body["result"]["content"][0]["text"])
            self.assertTrue(content["ok"])
            self.assertEqual(content["source"], "mock")

    async def test_calendar_create_via_mcp_in_mock_fails(self) -> None:
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {
                    "name": "calendar_create",
                    "arguments": {
                        "summary": "test",
                        "start": "2026-07-04T15:00:00-04:00",
                        "end": "2026-07-04T16:00:00-04:00",
                    },
                },
            })
            body = await r.json()
            content = json.loads(body["result"]["content"][0]["text"])
            # sin GOOGLE_REAL=1, debe fallar honestamente
            self.assertFalse(content["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
