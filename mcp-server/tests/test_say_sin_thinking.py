"""El chain-of-thought no es narración: no se emite como `say`.

Medido 2026-09-04 en un run real de MiniMax-M3: los eventos `say` salían
APAREADOS, mismo timestamp, el crudo en inglés primero y la narración en
español después. Seis pares en los 6 turnos del run:

    #23 say 19:20:14 :: All tests pass: 271 succeeded... Let me annotate
                        this and mark steps 4-7 done.
    #24 say 19:20:14 :: 271 tests, todos verdes. Anoto y cierro los pasos.

Causa: `ThinkingPart` estaba en la misma tupla que `TextPart` en el
bloque de clasificación de partes. MiniMax-M3 es un modelo de
razonamiento y pydantic-ai mapea su `reasoning_content` a `ThinkingPart`,
así que el razonamiento crudo salía a la UI como si el modelo lo hubiera
dicho. `server._STEP_PHASES` documenta lo contrario: pensar es heartbeat
(`phase="thinking"`), no contenido.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_say_sin_thinking.py -q
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from relay.experts import _clasificar_partes  # noqa: E402


class _Part:
    """Doble mínimo: la clasificación mira el NOMBRE de la clase."""

    def __init__(self, content=None, tool_name=None, args=None):
        self.content = content
        if tool_name is not None:
            self.tool_name = tool_name
        self.args = args


class TextPart(_Part):
    pass


class ThinkingPart(_Part):
    pass


class ToolCallPart(_Part):
    pass


class _MR:
    def __init__(self, parts):
        self.parts = parts


class TestSayNoLlevaThinking(unittest.TestCase):

    def test_thinking_no_se_narra(self) -> None:
        mr = _MR([
            ThinkingPart("raw reasoning: let me check the build first"),
            TextPart("Voy a correr el build"),
            ToolCallPart(tool_name="shell", args={"cmd": "dotnet build"}),
        ])
        pedidas, says, recent = _clasificar_partes(mr)
        self.assertEqual(says, ["Voy a correr el build"])
        self.assertEqual([p[0] for p in pedidas], ["shell"])
        self.assertEqual(len(recent), 1)
        self.assertIn("shell", recent[0])

    def test_el_par_duplicado_del_run_real(self) -> None:
        """El caso exacto que se filtró: inglés crudo + español narrado."""
        mr = _MR([
            ThinkingPart(
                "All tests pass: 271 succeeded... Let me annotate this "
                "and mark steps 4-7 done."),
            TextPart("271 tests, todos verdes. Anoto y cierro los pasos."),
        ])
        _pedidas, says, _r = _clasificar_partes(mr)
        self.assertEqual(len(says), 1)
        self.assertNotIn("Let me annotate", says[0])

    def test_turno_solo_thinking_queda_sin_narracion(self) -> None:
        """Regresión conocida y ACEPTADA (ver server.py:1956-1962).

        Hay modelos que vuelcan todo en `thinking` y devuelven texto
        vacío. Con el fix esos turnos no narran nada; el heartbeat
        `phase="thinking"` sigue avisando que el run está vivo.
        """
        _p, says, _r = _clasificar_partes(_MR([ThinkingPart("solo pienso")]))
        self.assertEqual(says, [])

    def test_texto_vacio_no_ensucia(self) -> None:
        _p, says, _r = _clasificar_partes(
            _MR([TextPart("   "), TextPart("real")]))
        self.assertEqual(says, ["real"])

    def test_mr_none_no_revienta(self) -> None:
        self.assertEqual(_clasificar_partes(None), ([], [], []))


if __name__ == "__main__":
    unittest.main()
