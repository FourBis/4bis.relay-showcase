"""Modelo por rol para UN run (popover del composer).

Hasta 2026-08-26 las etapas del runner solo se podian cambiar por .env
(global, pide reiniciar) o por `defaults_json` del proyecto (permanente).
El popover del chat manda `stage_models` en el POST /experts/run y vale
solo para ese turno, igual que `model` para el ejecutor.

Cubre:
- `stage_models` gana sobre `defaults_json`, que gana sobre la cascada
- un rol ausente NO pisa nada (sigue el default del proyecto)
- con `three_stage=false` el kwarg no llega a `run_expert`, que no tiene
  **kwargs y moriria con TypeError

Como correr:
    cd mcp-server
    python -m pytest tests/test_stage_models.py -q
"""
from __future__ import annotations
from relay import expert_history, expert_runner, expert_staged_runner, expert_stages

import tempfile
import unittest
from unittest.mock import patch

from pydantic_ai.messages import (
    ModelRequest, ModelResponse, ToolCallPart, UserPromptPart)

from relay import experts


def _historial_con_tool() -> str:
    """messages_json con UNA tool call QUE ESCRIBE.

    El documentador se saltea si no hubo trabajo, y "hubo trabajo" se
    deriva de `messages_json`: `tool_calls_summary` se recalcula ahi
    mismo, pisando lo que traiga el dict del ejecutor. Un `[]` deja al
    documentador sin correr y el test no probaria nada.

    Desde 6f90d73 tampoco alcanza con una tool cualquiera: un run que
    SOLO lee es una revision y no se documenta. Por eso `edit_file` y no
    `read_file` — estos tests son sobre que modelo corre cada etapa, no
    sobre la guarda.
    """
    return expert_history._dump_messages([
        ModelRequest(parts=[UserPromptPart(content="hace algo")]),
        ModelResponse(parts=[ToolCallPart("edit_file", {"path": "x.py"})]),
    ])


def _proyecto(tmp: str, **defaults) -> dict:
    return {
        "slug": "demo", "repo_path": tmp, "id": 1,
        "system_prompt": "", "mcp_servers": [],
        "defaults_json": {"model": "test", **defaults},
        "native_tools": [],
    }


class TestStageModels(unittest.IsolatedAsyncioTestCase):
    async def _specs(self, proyecto: dict, **kwargs) -> dict:
        """Corre el runner por etapas capturando el spec de cada etapa."""
        vistos: dict[str, str] = {}

        async def _planner(*, model_spec, **_k):
            vistos["planner"] = model_spec
            return "1. hacer algo\n2. y otra cosa", {}, ""

        async def _verifier(*, model_spec, **_k):
            vistos["verifier"] = model_spec
            return "complete", "", {}, ""

        async def _documenter(*, model_spec, **_k):
            vistos["documenter"] = model_spec
            return "registro del cambio", {}, ""

        async def _ejecutor(_proj, _user, **_k):
            return {"content": "listo", "model": "test", "tokens_in": 1,
                    "tokens_out": 1, "tool_calls": 1, "duration_ms": 1,
                    "messages_json": _historial_con_tool(),
                    "phase_at_end": "writing",
                    "last_tool": "read_file", "legs": 1, "steers": 0,
                    "steer_texts": [], "progress_events": []}

        with patch.object(expert_stages, "_run_planner", _planner), \
                patch.object(expert_stages, "_run_verifier", _verifier), \
                patch.object(expert_stages, "_run_documenter", _documenter), \
                patch.object(expert_runner, "run_expert", _ejecutor):
            await expert_staged_runner.run_expert_staged(proyecto, "hace algo", **kwargs)
        return vistos

    async def test_stage_models_gana_sobre_el_default_del_proyecto(self):
        with tempfile.TemporaryDirectory() as tmp:
            proyecto = _proyecto(tmp, planner_model="proj:planner",
                                 verifier_model="proj:verifier")
            vistos = await self._specs(
                proyecto,
                stage_models={"planner": "run:planner",
                              "documenter": "run:doc"})
        # Lo del turno manda...
        self.assertEqual(vistos["planner"], "run:planner")
        self.assertEqual(vistos["documenter"], "run:doc")
        # ...y lo que el turno NO nombro sigue con el default del proyecto.
        self.assertEqual(vistos["verifier"], "proj:verifier")

    async def test_sin_stage_models_no_cambia_nada(self):
        with tempfile.TemporaryDirectory() as tmp:
            proyecto = _proyecto(tmp, planner_model="proj:planner")
            vistos = await self._specs(proyecto)
        self.assertEqual(vistos["planner"], "proj:planner")

    async def test_opt_out_no_le_pasa_el_kwarg_al_ejecutor(self):
        """`run_expert` no tiene **kwargs: un `stage_models` que
        sobreviva al opt-out lo mata con TypeError."""
        recibido = {}

        async def _ejecutor(_proj, _user, **kw):
            recibido.update(kw)
            return {"content": "ok", "model": "test", "phase_at_end": "writing"}

        with tempfile.TemporaryDirectory() as tmp:
            proyecto = _proyecto(tmp, three_stage=False)
            with patch.object(expert_runner, "run_expert", _ejecutor):
                r = await expert_staged_runner.run_expert_staged(
                    proyecto, "hace algo",
                    stage_models={"planner": "run:planner"})
        self.assertNotIn("stage_models", recibido)
        self.assertFalse(r["three_stage"])


if __name__ == "__main__":
    unittest.main()
