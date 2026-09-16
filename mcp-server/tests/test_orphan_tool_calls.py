"""Regression del bug del 2026-07-25: el **continúa** roto tras un corte.

Cuando el watchdog de idle (o el tope global, o un cancel) corta el run
con una tool en vuelo, el historial rescatado termina en un tool call
sin ToolReturnPart. pydantic-ai rechaza el turno siguiente con
`UserError: Cannot provide a new user prompt when the message history
contains unprocessed tool calls`, así que el "manda continúa" que le
ofrecemos al humano en el mensaje de corte fallaba SIEMPRE.

Caso real: hilo 4295b474 (workshopdemo). El chat b5d3be17 murió por
idle con un `run_shell` huérfano; el intento de retomar (ee9db4c3)
explotó a los 7s con ese UserError.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_orphan_tool_calls.py -q
"""
from __future__ import annotations

import unittest

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel

from relay.experts import _close_orphan_tool_calls


def _history_with_orphan() -> list:
    """Historial que termina en un run_shell que nunca devolvió."""
    return [
        ModelRequest(parts=[UserPromptPart(content="levanta el front")]),
        ModelResponse(parts=[ToolCallPart(
            tool_name="run_shell",
            args={"cmd": "npx --prefix frontend vite --host"},
            tool_call_id="call_huerfano")]),
    ]


class TestCloseOrphanToolCalls(unittest.TestCase):

    def test_cierra_el_huerfano(self) -> None:
        msgs = _history_with_orphan()
        n = _close_orphan_tool_calls(msgs, reason="corte por idle")
        self.assertEqual(n, 1)
        last = msgs[-1]
        self.assertIsInstance(last, ModelRequest)
        part = last.parts[0]
        self.assertIsInstance(part, ToolReturnPart)
        self.assertEqual(part.tool_call_id, "call_huerfano")
        self.assertEqual(part.tool_name, "run_shell")
        self.assertIn("corte por idle", part.content)

    def test_no_toca_un_historial_sano(self) -> None:
        """Idempotente: sin huérfanos no agrega nada."""
        msgs = _history_with_orphan()
        _close_orphan_tool_calls(msgs, reason="x")
        n_before = len(msgs)
        n = _close_orphan_tool_calls(msgs, reason="x")
        self.assertEqual(n, 0)
        self.assertEqual(len(msgs), n_before)

    def test_pydantic_ai_acepta_el_historial_cerrado(self) -> None:
        """El check que importa: que el 'continúa' realmente corra.

        Sin el fix esto tira UserError; con el fix el agente arranca
        el turno nuevo normalmente.
        """
        agent = Agent(TestModel())

        # 1. Sin cerrar → UserError (el bug).
        with self.assertRaises(Exception) as ctx:
            agent.run_sync("continúa", message_history=_history_with_orphan())
        self.assertIn("unprocessed tool calls", str(ctx.exception))

        # 2. Cerrado → corre.
        msgs = _history_with_orphan()
        _close_orphan_tool_calls(msgs, reason="corte por idle")
        result = agent.run_sync("continúa", message_history=msgs)
        self.assertTrue(result.output)


if __name__ == "__main__":
    unittest.main()
