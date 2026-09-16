"""Sesión MCP persistente contra cbm (reemplazo del spawn one-shot).

Cubre la única rama que puede romper el run del experto: si la sesión no
levanta, `cbm_call` tiene que caer al CLI y devolver el mismo JSON — nunca
propagar la excepción de transporte.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_cbm_session.py -q
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from relay import experts


def _reset() -> None:
    """Los globals de la sesión son de módulo: sin esto un test contamina
    al siguiente (`_cbm_session_off` es sticky a propósito)."""
    experts._cbm_toolset = None
    experts._cbm_transport = None
    experts._cbm_session_off = False


class _FakeToolset:
    """Toolset que responde como el real: dict estructurado, no string."""

    def __init__(self, payload=None, boom=None):
        self.payload, self.boom, self.calls = payload, boom, []

    async def __aenter__(self):
        if self.boom:
            raise self.boom
        return self

    async def __aexit__(self, *exc):
        return False

    async def direct_call_tool(self, name, args, **kw):
        self.calls.append((name, args))
        return self.payload


class TestCbmCallRouting(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        _reset()

    async def asyncTearDown(self):
        _reset()

    async def test_sesion_ok_no_toca_el_cli(self):
        fake = _FakeToolset(payload={"nodes": 7})

        async def no_cli(*a, **kw):
            self.fail("cayó al CLI teniendo sesión viva")

        with patch.object(experts, "cbm_binary_path", lambda: "cbm.exe"), \
             patch.object(experts, "cbm_cli_call", no_cli), \
             patch("relay.mcp_pool.make_toolset", lambda *a, **kw: (fake, None)), \
             patch("relay.admin._cbm_env", dict):
            out = await experts.cbm_call("index_status", {"project": "p"})

        # El contrato es JSON string, aunque el toolset devuelva dict.
        self.assertEqual(json.loads(out), {"nodes": 7})
        self.assertEqual(fake.calls, [("index_status", {"project": "p"})])

    async def test_sesion_rota_cae_al_cli_y_no_reintenta(self):
        fake = _FakeToolset(boom=TimeoutError("handshake colgado"))
        cli_calls = []

        async def fake_cli(tool, args, *, timeout=30.0):
            cli_calls.append(tool)
            return '{"desde": "cli"}'

        with patch.object(experts, "cbm_binary_path", lambda: "cbm.exe"), \
             patch.object(experts, "cbm_cli_call", fake_cli), \
             patch("relay.mcp_pool.make_toolset", lambda *a, **kw: (fake, None)), \
             patch("relay.admin._cbm_env", dict):
            first = await experts.cbm_call("index_status", {"project": "p"})
            second = await experts.cbm_call("search_graph", {"project": "p"})

        self.assertEqual(json.loads(first), {"desde": "cli"})
        self.assertEqual(json.loads(second), {"desde": "cli"})
        # La sesión muerta queda apagada: el segundo call no la reintenta.
        self.assertTrue(experts._cbm_session_off)
        self.assertEqual(cli_calls, ["index_status", "search_graph"])

    async def test_flag_apaga_la_sesion(self):
        fake = _FakeToolset(payload={"no": "deberia usarse"})

        async def fake_cli(tool, args, *, timeout=30.0):
            return '{"desde": "cli"}'

        with patch.dict("os.environ", {"CBM_MCP_SESSION": "0"}), \
             patch.object(experts, "cbm_binary_path", lambda: "cbm.exe"), \
             patch.object(experts, "cbm_cli_call", fake_cli), \
             patch("relay.mcp_pool.make_toolset", lambda *a, **kw: (fake, None)):
            out = await experts.cbm_call("index_status", {"project": "p"})

        self.assertEqual(json.loads(out), {"desde": "cli"})
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
