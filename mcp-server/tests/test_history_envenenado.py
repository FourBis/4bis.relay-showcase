"""Regresión 2026-08-27: la conversación que deja de aceptar mensajes.

Síntoma reportado: *"después de terminar algún flujo se obliga a empezar
otra vez… hay que cerrar la conversación, si no deja de responder"*.

Causa medida en `~/.4bis/relay.db`: 20 runs muertos con
`UserError: Cannot provide a new user prompt when the message history
contains unprocessed tool calls`, repartidos en 9 conversaciones. Tres
de ellas (58297e9f, 7d464e43, a08944c4) quedaron guardadas con **un
solo** user prompt y 60+ tool calls: el primer turno murió con una tool
en vuelo, el par call↔return quedó abierto, y desde ahí TODO turno
siguiente explotaba antes de llegar al modelo. Por eso el primer prompt
andaba bien y el segundo no.

`_close_orphan_tool_calls` ya existía, pero solo lo llamaban las rutas
de rescate del run que se cortaba. Este test cubre el guard que faltaba:
el que corre sobre el historial que ENTRA, sin importar quién lo dejó
abierto.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_history_envenenado.py -q
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from pydantic_ai.messages import (  # noqa: E402
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from relay.experts import run_expert  # noqa: E402


def _historial_envenenado() -> str:
    """Lo que quedó guardado tras un primer turno cortado a mitad de tool."""
    msgs = [
        ModelRequest(parts=[UserPromptPart(content="arregla el build")]),
        ModelResponse(parts=[
            TextPart(content="miro el proyecto"),
            ToolCallPart(tool_name="shell", args={"cmd": "dotnet build"},
                         tool_call_id="call_ok")]),
        ModelRequest(parts=[ToolReturnPart(
            tool_name="shell", tool_call_id="call_ok", content="(exit=0)")]),
        # …y acá el run murió: esta call nunca recibió su return.
        ModelResponse(parts=[ToolCallPart(
            tool_name="shell", args={"cmd": "npm run dev"},
            tool_call_id="call_huerfano")]),
    ]
    return ModelMessagesTypeAdapter.dump_json(msgs).decode()


class TestHistorialEnvenenado(unittest.IsolatedAsyncioTestCase):

    async def test_el_segundo_turno_corre(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = {"slug": "demo", "repo_path": tmp,
                       "system_prompt": "", "mcp_servers": [],
                       "defaults_json": {}, "native_tools": []}
            r = await run_expert(
                project, "segundo turno", model_override="test",
                message_history_json=_historial_envenenado())
            # Sin el guard esto vuelve con error y content vacío.
            self.assertTrue(r["content"], "el turno no llegó al modelo")
            # Y el historial que se guarda ya no arrastra el huérfano.
            msgs = json.loads(r["messages_json"])
            calls, returns = set(), set()
            for m in msgs:
                for p in m.get("parts", []):
                    if p.get("part_kind") == "tool-call":
                        calls.add(p.get("tool_call_id"))
                    elif p.get("part_kind") in ("tool-return", "retry-prompt"):
                        returns.add(p.get("tool_call_id"))
            self.assertEqual(calls - returns, set(), "quedó un call sin return")


if __name__ == "__main__":
    unittest.main()
