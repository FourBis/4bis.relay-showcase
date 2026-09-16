"""Tests del endpoint /mcp (JSON-RPC) y los tools Google mockeados.

Historia: este archivo cubría el diseño viejo de "agents" (POST /agents,
agt_*, AgentStore) que se eliminó del relay — hoy las sesiones VS Code
van por /agents/handshake (ADR-009) y los expertos por /experts/run
(ADR-012/024). Queda lo vigente: gmail mock + /mcp.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_roundtrip.py
"""
from __future__ import annotations

import json
import unittest

from aiohttp.test_utils import TestClient, TestServer

from relay.server import create_app
from relay.tools.gmail import GmailReadTool


class TestGmailMock(unittest.IsolatedAsyncioTestCase):
    async def test_empty_query_returns_all(self) -> None:
        tool = GmailReadTool()
        out = await tool.call({"max_results": 10})
        self.assertTrue(out["ok"])
        self.assertEqual(out["source"], "mock")
        self.assertGreaterEqual(len(out["messages"]), 1)

    async def test_query_filters(self) -> None:
        tool = GmailReadTool()
        out = await tool.call({"query": "example-client", "max_results": 10})
        self.assertTrue(out["ok"])
        self.assertTrue(any("example-client" in m["from"] for m in out["messages"]))


class TestMcpEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_invoke_tool(self) -> None:
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            })
            self.assertEqual(r.status, 200)
            body = await r.json()
            names = [t["name"] for t in body["result"]["tools"]]
            self.assertIn("gmail_read", names)

            r = await client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "gmail_read", "arguments": {"query": "example-client"}},
            })
            body = await r.json()
            self.assertIn("result", body)
            content = json.loads(body["result"]["content"][0]["text"])
            self.assertTrue(content["ok"])
            self.assertEqual(content["source"], "mock")


if __name__ == "__main__":
    unittest.main(verbosity=2)
