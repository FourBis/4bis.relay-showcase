"""Bug fix 2026-08-10: respuesta vacía cuando el LLM solo invoca tools.

Síntoma: un LLM (no TestModel — un modelo real como minimax M3) a veces
termina el run con un ModelResponse que tiene SOLO un ToolCallPart y
ningún TextPart. `result.output` queda None o "", `output_text` se
queda como "", y el chat se persiste como status="ok" con content="".
El usuario en Discord/UI ve un "✓ done" sin ninguna respuesta textual.

Fix: en `run_expert`, justo antes de armar el return dict, si
`output_text` está vacío y el último `ModelResponse` tiene un
`ToolCallPart` sin `TextPart`, generamos un fallback accionable y
marcamos `last_phase = "no_final_text"`. NO es error (el LLM hizo lo
que pidió), pero el humano tiene que saber que falta la redacción
final.

Por qué NO propagar como error: la convención de la casa es que un
run sin texto es un bug del LLM, no un fallo del relay. El humano
siempre puede mandar `continúa` y retomar.

Por qué NO auto-continuar: ya tenemos un `max_legs` que hace eso
para budget_exceeded. Un LLM que eligió terminar sin texto debería
ser el humano el que decida si quiere otra pasada.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ModelMessagesTypeAdapter,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from relay import experts


# ---------- helpers de tests ----------


def _hist_json(*parts) -> str:
    """Serializa un historial arbitrario a JSON (vía ModelMessagesTypeAdapter)."""
    return ModelMessagesTypeAdapter.dump_json(list(parts)).decode("utf-8")


def _hist_with_answered_tool_call_and_empty_final() -> str:
    """Historial típico del bug: tool call respondido, response final vacío.

    pydantic-ai NO permite que un Agent activo termine con parts=[] (lo
    trata como "no actionable" y reintenta). Pero el historial GUARDADO
    puede perfectamente tener ese último response si el LLM real lo
    emitió y el provider lo aceptó. Esto es lo que entra al replay en
    el próximo turno de la conversación — y es lo que estamos
    detectando.
    """
    return _hist_json(
        ModelRequest(parts=[UserPromptPart("hola")]),
        ModelResponse(parts=[ToolCallPart(
            tool_name="read_file", args={"path": "a.py"},
            tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(
            tool_name="read_file", tool_call_id="c1",
            content="contenido del archivo")]),
        ModelResponse(parts=[]),  # ← el bug: respuesta final sin texto
    )


def _hist_with_answered_tool_call_and_text_final() -> str:
    """Historial normal: tool call respondido y response final con texto."""
    return _hist_json(
        ModelRequest(parts=[UserPromptPart("hola")]),
        ModelResponse(parts=[ToolCallPart(
            tool_name="read_file", args={"path": "a.py"},
            tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(
            tool_name="read_file", tool_call_id="c1",
            content="contenido del archivo")]),
        ModelResponse(parts=[TextPart("el archivo dice hola")]),
    )


def _hist_without_tool_calls_and_empty_final() -> str:
    """Historial sin tool calls y response vacío: el LLM no quiso hablar."""
    return _hist_json(
        ModelRequest(parts=[UserPromptPart("hola")]),
        ModelResponse(parts=[]),
    )


# ---------- tests del helper (unit, sin Agent real) ----------


class TestSynthesizeNoFinalText(unittest.TestCase):
    """El helper `_synthesize_no_final_text` detecta el patrón del bug."""

    def test_sintetiza_fallback_con_historial_de_tool_call(self) -> None:
        """Caso real del bug: tool call respondido, response final vacío.
        El helper devuelve un fallback con la lista de tools."""
        from relay.experts import _synthesize_no_final_text
        out, phase = _synthesize_no_final_text(
            _hist_with_answered_tool_call_and_empty_final(),
            current_phase="thinking")
        self.assertIn("read_file", out)
        self.assertIn("continúa", out.lower())
        self.assertEqual(phase, "no_final_text")

    def test_no_sintetiza_si_ya_hay_texto(self) -> None:
        """Si el último response tiene texto, no hace nada."""
        from relay.experts import _synthesize_no_final_text
        out, phase = _synthesize_no_final_text(
            _hist_with_answered_tool_call_and_text_final(),
            current_phase="writing")
        self.assertEqual(out, "")
        self.assertEqual(phase, "writing")  # sin cambios

    def test_no_sintetiza_sin_tool_calls(self) -> None:
        """Si no hubo tool calls (LLM simplemente no quiso responder),
        no inventamos un fallback:尊重 el silencio del modelo."""
        from relay.experts import _synthesize_no_final_text
        out, phase = _synthesize_no_final_text(
            _hist_without_tool_calls_and_empty_final(),
            current_phase="thinking")
        self.assertEqual(out, "")
        self.assertEqual(phase, "thinking")

    def test_no_toca_phases_de_corte(self) -> None:
        """Si el run ya cortó por idle/timeout/budget, el fallback ya
        tiene su propio mensaje — no lo pisamos."""
        from relay.experts import _synthesize_no_final_text
        for phase in ("idle_timeout", "hard_timeout", "budget_exceeded",
                      "tool_retries_exhausted"):
            out, p = _synthesize_no_final_text(
                _hist_with_answered_tool_call_and_empty_final(),
                current_phase=phase)
            self.assertEqual(out, "", phase)
            self.assertEqual(p, phase, phase)

    def test_no_rompe_con_historial_invalido(self) -> None:
        """Si el JSON está corrupto, el helper devuelve ('', phase) sin
        reventar — el run ya terminó, no podemos hacer más que seguir."""
        from relay.experts import _synthesize_no_final_text
        out, phase = _synthesize_no_final_text(
            "not json at all", current_phase="writing")
        self.assertEqual(out, "")
        self.assertEqual(phase, "writing")

    def test_lista_las_tools_invocado_en_orden(self) -> None:
        """Si el último response pide más de una tool, las lista todas."""
        from relay.experts import _synthesize_no_final_text
        h = _hist_json(
            ModelRequest(parts=[UserPromptPart("hola")]),
            ModelResponse(parts=[ToolCallPart(
                tool_name="read_file", args={"path": "a.py"},
                tool_call_id="c1")]),
            ModelRequest(parts=[ToolReturnPart(
                tool_name="read_file", tool_call_id="c1",
                content="contenido a")]),
            ModelResponse(parts=[ToolCallPart(
                tool_name="read_file", args={"path": "b.py"},
                tool_call_id="c2")]),
            ModelRequest(parts=[ToolReturnPart(
                tool_name="read_file", tool_call_id="c2",
                content="contenido b")]),
            ModelResponse(parts=[]),  # ← vacío
        )
        out, _ = _synthesize_no_final_text(h, current_phase="writing")
        self.assertIn("read_file", out)
        # Múltiples menciones porque la lista de tools tiene un solo
        # elemento (todas son read_file), pero el fallback se generó.
        self.assertIn("continúa", out.lower())


if __name__ == "__main__":
    unittest.main()
