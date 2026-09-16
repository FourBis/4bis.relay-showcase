"""El ejecutor no pierde el trabajo cuando el proveedor se cae.

Caso real (2026-08-26, chat `1e069c32` / conversacion `212c7a87` de
example-frontend): 18m31s de ejecutor sobre el endpoint gratis de NVIDIA y
al final un `ModelHTTPError: status_code: 500`. La excepcion subia cruda
hasta el `except Exception` de server.py, que con `status="error"` NO
guarda el historial de la conversacion — el chat quedo con content
vacio, tokens NULL, tool_calls NULL y la conversacion sin un solo turno.

Cubre:
  - un 5xx a mitad del run NO propaga: se rescata el historial y se
    reintenta UNA vez sobre el (el run termina bien)
  - si el reintento tambien se cae: corte reanudable
    (phase_at_end="provider_error", mensaje con **continua**, historial
    parcial no vacio) en vez de excepcion
  - un 429 NO se reintenta (`_que_hacer` dice "cambiar"): corta directo,
    sin quemar el contexto de nuevo
  - un fallo de TRANSPORTE (httpx: conexion reseteada, read timeout) se
    trata igual que un 5xx: sale crudo del cliente, no envuelto en
    ModelHTTPError, y perdia el run por el mismo agujero
  - el stdio de un MCP que se muere a mitad del run (anyio
    ClosedResourceError / BrokenResourceError) entra por ese MISMO
    agujero: 13 runs perdidos en 14 dias, medidos en la DB el 31/8

Como correr:
    cd mcp-server
    python -m pytest tests/test_provider_error.py -q
"""
from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch

import anyio
import httpx
from pydantic_ai import Tool
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.toolsets import FunctionToolset

from relay import experts


def _flaky_model(fail_on: set[int], status: int = 500,
                 exc: Exception | None = None) -> FunctionModel:
    """Pide una tool, y en las llamadas de `fail_on` revienta con un
    fallo del proveedor (`status`, o `exc` si se pasa una excepcion
    cruda). Cuando ya vio un ToolReturn, responde y termina."""
    state = {"n": 0}

    def act(messages, info):
        state["n"] += 1
        if state["n"] in fail_on:
            raise exc if exc is not None else ModelHTTPError(
                status_code=status, model_name="fake",
                body={"message": "Internal server error"})
        seen = sum(
            1 for m in messages for p in getattr(m, "parts", [])
            if isinstance(p, ToolReturnPart))
        if seen >= 1:
            return ModelResponse(parts=[TextPart("listo: tarea completa")])
        # `ping` por nombre: function_tools[0] puede ser cbm_query si el
        # binario cbm esta instalado en la maquina que corre los tests.
        names = [t.name for t in info.function_tools]
        name = "ping" if "ping" in names else names[0]
        return ModelResponse(parts=[ToolCallPart(name, {"x": 1})])
    return FunctionModel(act)


def _ping_toolset() -> list:
    def ping(x: int) -> int:
        """ping"""
        return x
    return [FunctionToolset(tools=[Tool(ping, takes_ctx=False)])]


class _FakeDb:
    async def mcp_servers_for_project(self, *_a, **_k):
        return ([], [])


class TestProviderError(unittest.IsolatedAsyncioTestCase):
    def _project(self, tmp: str) -> dict:
        return {
            "slug": "demo", "repo_path": tmp, "id": 1,
            "system_prompt": "", "mcp_servers": [],
            # timeout > ROUND_MIN_S (30s): el reintento exige ese margen.
            "defaults_json": {"model": "fake", "timeout": 300},
            "native_tools": [],
        }

    async def _run(self, model: FunctionModel) -> dict:
        async def _fake_catalog(*_a, **_k):
            return (_ping_toolset(), [], [])
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(experts, "build_model", lambda spec: model), \
                    patch.object(experts, "_catalog_toolsets", _fake_catalog), \
                    patch.object(experts, "_PROVIDER_BACKOFF_S", 0):
                return await experts.run_expert(
                    self._project(tmp), "haz la tarea", db=_FakeDb())

    async def test_5xx_reintenta_sobre_el_historial_y_termina(self) -> None:
        # Llamada 1 → tool; llamada 2 → 500; llamada 3 (reintento) → listo.
        r = await self._run(_flaky_model({2}))
        self.assertNotEqual(r["phase_at_end"], "provider_error")
        self.assertIn("listo", r["content"].lower())
        self.assertTrue(r["messages_json"])
        # El reintento quedo registrado para el diagnostico.
        self.assertIn(
            "provider_retry",
            [e.get("phase") for e in r["progress_events"]])

    async def test_5xx_persistente_corta_reanudable_sin_excepcion(self) -> None:
        # Llamadas 2 y 3 se caen: se agota el unico reintento.
        r = await self._run(_flaky_model({2, 3}))
        self.assertEqual(r["phase_at_end"], "provider_error")
        self.assertIn("500", r["content"])
        # "contin" y no "continu": el nudge es "continua" (con tilde).
        self.assertIn("contin", r["content"].lower())
        # Lo andado sobrevive → la conversacion queda reanudable.
        self.assertTrue(r["messages_json"])
        msgs = json.loads(r["messages_json"])
        self.assertGreater(len(msgs), 0)
        self.assertGreaterEqual(r["tool_calls"], 1)

    async def test_transporte_caido_se_rescata_igual_que_un_5xx(self) -> None:
        # Una conexion reseteada sale como httpx.ConnectError, NO como
        # ModelHTTPError: mismo agujero, misma perdida del run.
        r = await self._run(
            _flaky_model({2}, exc=httpx.ConnectError("connection reset")))
        self.assertNotEqual(r["phase_at_end"], "provider_error")
        self.assertIn("listo", r["content"].lower())
        self.assertIn(
            "provider_retry",
            [e.get("phase") for e in r["progress_events"]])

    async def test_transporte_persistente_corta_reanudable(self) -> None:
        r = await self._run(
            _flaky_model({2, 3}, exc=httpx.ReadTimeout("read timeout")))
        self.assertEqual(r["phase_at_end"], "provider_error")
        self.assertIn("ReadTimeout", r["content"])
        self.assertTrue(r["messages_json"])
        self.assertGreaterEqual(r["tool_calls"], 1)

    async def test_mcp_muerto_a_mitad_del_run_se_rescata(self) -> None:
        """El stdio de un MCP que se muere no puede costar el run entero.

        2026-08-31: 13 runs en 14 dias terminaron con `status=error`,
        `tokens_in` NULL, `phase_at_end` NULL y la conversacion sin un
        solo turno. El ultimo (`0e9d89cf`, code-hero-rpg) murio a los
        101s con `ClosedResourceError`: la sesion anyio del MCP se cerro
        a mitad del run y la excepcion subia cruda por el mismo agujero
        que los httpx, que ya estaba tapado.
        """
        r = await self._run(
            _flaky_model({2}, exc=anyio.ClosedResourceError()))
        self.assertNotEqual(r["phase_at_end"], "provider_error")
        self.assertIn("listo", r["content"].lower())
        self.assertIn(
            "provider_retry",
            [e.get("phase") for e in r["progress_events"]])

    async def test_mcp_muerto_persistente_corta_reanudable(self) -> None:
        """Si el MCP no vuelve, corte reanudable con lo andado guardado.

        El proximo turno re-adquiere el server por el pool (que hace
        probe de handshake y lo levanta de nuevo), asi que **continua**
        es una salida real y no una promesa vacia.
        """
        r = await self._run(
            _flaky_model({2, 3}, exc=anyio.BrokenResourceError()))
        self.assertEqual(r["phase_at_end"], "provider_error")
        self.assertIn("BrokenResourceError", r["content"])
        self.assertIn("contin", r["content"].lower())
        self.assertTrue(r["messages_json"])
        self.assertGreaterEqual(r["tool_calls"], 1)

    async def test_429_no_se_reintenta(self) -> None:
        r = await self._run(_flaky_model({2}, status=429))
        self.assertEqual(r["phase_at_end"], "provider_error")
        self.assertNotIn(
            "provider_retry",
            [e.get("phase") for e in r["progress_events"]])
        self.assertTrue(r["messages_json"])


if __name__ == "__main__":
    unittest.main()
