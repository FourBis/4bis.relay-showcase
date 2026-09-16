"""Las tarjetas de la UI tienen que decir QUÉ comando y con qué salió.

Reportado 2026-08-27: *"aún no veo las herramientas en la UI, solo dice
shell o cosas así; quiero que ahí escriba el comando encerrado y
colapsable para ver qué comandos va usando, para saber si va bien o mal,
y qué está respondiendo la terminal"*.

Dos agujeros medidos:

  1. `_format_tool_step` tenía rama para `run_shell` (el del wrapper MCP)
     pero no para `shell` —la tool NATIVA del relay, que es la que el
     experto usa desde que `HideToolsToolset` esconde la otra—, así que
     caía al genérico `🔧 {name}`: la UI mostraba la palabra "shell" y
     nada más.
  2. La salida de la tool no se emitía a ningún lado. El `ToolReturnPart`
     se medía (`_measure_tool_returns`) y se descartaba.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_pasos_shell_ui.py -q
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from relay.experts import _format_tool_step  # noqa: E402
from relay.server import _steps_from_progress  # noqa: E402


class TestFormatToolStep(unittest.TestCase):

    def test_shell_nativa_muestra_el_comando(self) -> None:
        linea, diff, cmd = _format_tool_step(
            "shell", {"cmd": "dotnet build -c Release"})
        self.assertIn("dotnet build -c Release", linea)
        self.assertIsNone(diff)
        self.assertEqual(cmd, "dotnet build -c Release")

    def test_multilinea_no_desborda_el_encabezado(self) -> None:
        """El encabezado lleva la primera línea; el cuerpo, todo."""
        script = "cd frontend\nnpm ci\nnpm run build"
        linea, _diff, cmd = _format_tool_step("shell", {"cmd": script})
        self.assertNotIn("\n", linea)
        self.assertIn("cd frontend", linea)
        self.assertEqual(cmd, script)

    def test_cwd_y_background_viajan(self) -> None:
        linea, _d, cmd = _format_tool_step(
            "shell", {"cmd": "npm run dev", "cwd": "web", "background": True})
        self.assertIn("[background]", linea)
        self.assertIn("# cwd: web", cmd)

    def test_alias_del_wrapper_siguen_andando(self) -> None:
        for name in ("run_shell", "run_command"):
            linea, _d, cmd = _format_tool_step(name, {"cmd": "git status"})
            self.assertIn("git status", linea, name)
            self.assertEqual(cmd, "git status", name)

    def test_las_otras_tools_no_cambiaron(self) -> None:
        linea, diff, cmd = _format_tool_step("read_file", {"path": "a.py"})
        self.assertIn("a.py", linea)
        self.assertIsNone(diff)
        self.assertIsNone(cmd)


class TestStepsFromProgress(unittest.TestCase):

    def test_la_salida_se_cuelga_de_su_paso(self) -> None:
        eventos = json.dumps([
            {"phase": "say", "message": "voy a compilar"},
            {"phase": "tool_call", "tool": "shell",
             "message": "⚙️ shell: `dotnet build`", "cmd": "dotnet build"},
            {"phase": "tool_result", "tool": "shell",
             "output": "error CS0103\n(exit=1)"},
        ])
        steps = _steps_from_progress(eventos)
        # El tool_result NO es un paso propio: se fusiona con el suyo.
        self.assertEqual([s["kind"] for s in steps], ["say", "tool"])
        self.assertEqual(steps[1]["cmd"], "dotnet build")
        self.assertIn("(exit=1)", steps[1]["output"])

    def test_dos_shells_no_se_pisan(self) -> None:
        eventos = json.dumps([
            {"phase": "tool_call", "tool": "shell", "message": "a",
             "cmd": "uno"},
            {"phase": "tool_result", "tool": "shell", "output": "salida uno"},
            {"phase": "tool_call", "tool": "shell", "message": "b",
             "cmd": "dos"},
            {"phase": "tool_result", "tool": "shell", "output": "salida dos"},
        ])
        steps = _steps_from_progress(eventos)
        self.assertEqual([s["output"] for s in steps],
                         ["salida uno", "salida dos"])

    def test_salida_huerfana_no_rompe(self) -> None:
        """Un result sin su call (el paso se cayó del tope) se descarta."""
        eventos = json.dumps([
            {"phase": "tool_result", "tool": "shell", "output": "x"},
            {"phase": "tool_call", "tool": "read_file", "message": "leyó"},
        ])
        steps = _steps_from_progress(eventos)
        self.assertEqual(len(steps), 1)
        self.assertNotIn("output", steps[0])


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 2026-09-04: un evento por tool call, no uno por turno.
# ---------------------------------------------------------------------------

class TestUnEventoPorToolCall(unittest.IsolatedAsyncioTestCase):
    """El modelo pide varias tools en un mismo response; el timeline las
    tiene que mostrar todas.

    Medido antes del fix sobre 228 chats: de 8.383 tool calls solo se
    emitían 5.797 eventos `tool_call` (faltaba el 30,8%), porque se
    emitía uno solo por turno con `tool_names[-1]`. Y como los args se
    buscaban por NOMBRE (y esa búsqueda devolvía el primer match),
    un turno con dos `shell` mostraba el comando de la primera pegado al
    nombre de la última — o sea el timeline podía emparejar un comando
    con la salida de otro. En 261 turnos reales hubo 2+ shell juntos.
    """

    async def test_dos_shell_en_un_turno_dejan_dos_eventos(self) -> None:
        import tempfile
        from unittest.mock import patch

        from pydantic_ai import Tool
        from pydantic_ai.models.function import FunctionModel
        from pydantic_ai.messages import (
            ModelResponse, TextPart, ToolCallPart, ToolReturnPart)
        from pydantic_ai.toolsets import FunctionToolset

        from relay import experts

        def act(messages, info):
            ya = sum(1 for m in messages for p in getattr(m, "parts", [])
                     if isinstance(p, ToolReturnPart))
            if ya:
                return ModelResponse(parts=[TextPart("listo")])
            # Los dos comandos en UN solo response: esto es lo que el
            # modelo hace de verdad y lo que el timeline se comía.
            return ModelResponse(parts=[
                ToolCallPart("shell", {"cmd": "git status"}),
                ToolCallPart("shell", {"cmd": "dotnet build"}),
            ])

        def shell(cmd: str) -> str:
            """corre un comando"""
            return f"ok: {cmd}"

        async def _fake_catalog(*_a, **_k):
            return ([FunctionToolset(tools=[Tool(shell, takes_ctx=False)])],
                    [], [])

        vistos: list[dict] = []

        async def _on_progress(**fields):
            vistos.append(fields)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(experts, "build_model",
                              lambda spec: FunctionModel(act)), \
                    patch.object(experts, "_catalog_toolsets", _fake_catalog):
                r = await experts.run_expert(
                    {"slug": "demo", "repo_path": tmp, "id": 1,
                     "system_prompt": "", "mcp_servers": [],
                     # `native_shell=false`: sin esto la tool nativa
                     # `shell` choca de nombre con la de mentira de acá.
                     "defaults_json": {"model": "fake", "timeout": 300,
                                       "native_shell": False,
                                       "native_files": False},
                     "native_tools": []},
                    "hacé las dos cosas", db=object(),
                    on_progress=_on_progress)

        pasos = [e for e in vistos if e.get("phase") == "tool_call"]
        self.assertEqual(len(pasos), 2,
                         f"se perdió una tool call: {pasos}")
        # Cada evento con SU comando, en orden. Con el bug viejo salía un
        # solo evento y con `git status` repetido.
        self.assertEqual([p.get("cmd") for p in pasos],
                         ["git status", "dotnet build"])
        # El contador avanza de a uno y llega al total real.
        self.assertEqual([p.get("tool_calls") for p in pasos], [1, 2])
        self.assertEqual(r["tool_calls"], 2)

        # Y cada salida colgada de SU comando. Emitir un paso por tool
        # destapó esto: el emparejamiento resultado→paso escaneaba de
        # atrás para adelante, así que con dos pasos de la misma tool
        # las salidas salían CRUZADAS (`git status` mostrando lo de
        # `dotnet build`). pydantic-ai entrega los returns en orden de
        # llamada, así que el criterio correcto es FIFO.
        eventos = [{k: v for k, v in e.items() if v is not None}
                   for e in vistos]
        tools = [s for s in _steps_from_progress(json.dumps(eventos))
                 if s.get("kind") == "tool"]
        self.assertEqual([(t.get("cmd"), t.get("output")) for t in tools],
                         [("git status", "ok: git status"),
                          ("dotnet build", "ok: dotnet build")])
