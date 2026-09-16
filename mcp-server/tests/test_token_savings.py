"""Ahorro de tokens en runs de expertos (2026-07-20).

Tres capas en experts.py: cap por tool result, elisión de results
viejos in-run, y slim del historial entre turnos de conversación.
Motivación: runs reales llegaron a 28.7M tokens_in acumulados.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_token_savings.py -q
"""
from __future__ import annotations

import json
import unittest

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from relay import experts


def _turn(user: str, tools: list[tuple[str, str]], answer: str) -> list:
    """Arma un turno completo: user → tool calls → returns → texto."""
    msgs: list = [ModelRequest(parts=[UserPromptPart(content=user)])]
    for i, (name, result) in enumerate(tools):
        cid = f"c{i}"
        msgs.append(ModelResponse(
            parts=[ToolCallPart(tool_name=name, args="{}", tool_call_id=cid)]))
        msgs.append(ModelRequest(
            parts=[ToolReturnPart(tool_name=name, content=result,
                                  tool_call_id=cid)]))
    msgs.append(ModelResponse(parts=[TextPart(content=answer)]))
    return msgs


class TestCapToolResult(unittest.TestCase):
    def test_sdk_mapped_text_is_capped_and_images_survive(self):
        from pydantic_ai.messages import BinaryImage
        image = BinaryImage(data=b"png", media_type="image/png")
        result = ["x" * (experts.TOOL_RESULT_CAP + 5000), "more text", image]
        out = experts._cap_tool_result(result)
        self.assertLess(sum(len(x) for x in out if isinstance(x, str)),
                        experts.TOOL_RESULT_CAP + 500)
        self.assertIn(image, out)
        self.assertIn("TRUNCADO", out[0])

    def test_short_passthrough(self) -> None:
        self.assertEqual(experts._cap_tool_result("hola"), "hola")

    def test_long_str_truncated_with_marker(self) -> None:
        fat = "x" * (experts.TOOL_RESULT_CAP + 5000)
        out = experts._cap_tool_result(fat)
        self.assertLess(len(out), len(fat))
        self.assertIn("TRUNCADO", out)

    def test_unknown_type_passthrough(self) -> None:
        obj = {"a": 1}
        self.assertIs(experts._cap_tool_result(obj), obj)


class TestElide(unittest.TestCase):
    def test_keeps_last_k_and_stubs_old(self) -> None:
        n = experts.TOOL_KEEP_FULL + 3
        fat = "y" * 2000
        msgs = _turn("hola", [(f"t{i}", fat) for i in range(n)], "listo")
        experts._elide_old_tool_returns(msgs)
        returns = [p for m in msgs if isinstance(m, ModelRequest)
                   for p in m.parts if isinstance(p, ToolReturnPart)]
        stubbed = [p for p in returns if experts._ELIDE_MARK in p.content]
        self.assertEqual(len(stubbed), 3)
        # los últimos K intactos
        for p in returns[-experts.TOOL_KEEP_FULL:]:
            self.assertEqual(p.content, fat)

    def test_conserva_el_veredicto_del_final(self) -> None:
        """El `(exit=N)` de run_shell vive en la última línea: sin esto,
        releyendo el hilo el modelo no sabe si el build pasó."""
        build = "dotnet build\n" + ("warning CS0168\n" * 200) + "(exit=1)"
        msgs = _turn("compilá", [("run_shell", build)] * 12, "listo")
        experts._elide_old_tool_returns(msgs)
        stub = [p for m in msgs if isinstance(m, ModelRequest)
                for p in m.parts if isinstance(p, ToolReturnPart)
                and experts._ELIDE_MARK in p.content][0]
        self.assertTrue(stub.content.endswith("(exit=1)"), stub.content[-60:])
        self.assertIn("dotnet build", stub.content)      # cabeza intacta
        self.assertLess(len(stub.content), experts._ELIDE_MIN_CHARS)  # idempotente
        # una cola larga (no es un veredicto) no se copia
        largo = "x\n" * 400 + "y" * 300
        msgs = _turn("dale", [("read_file", largo)] * 12, "ok")
        experts._elide_old_tool_returns(msgs)
        stub = [p for m in msgs if isinstance(m, ModelRequest)
                for p in m.parts if isinstance(p, ToolReturnPart)
                and experts._ELIDE_MARK in p.content][0]
        self.assertTrue(stub.content.endswith(experts._ELIDE_MARK))

    def test_idempotente_y_cortos_intactos(self) -> None:
        msgs = _turn("hola", [("t", "corto")] * 12, "listo")
        experts._elide_old_tool_returns(msgs)
        experts._elide_old_tool_returns(msgs)
        returns = [p for m in msgs if isinstance(m, ModelRequest)
                   for p in m.parts if isinstance(p, ToolReturnPart)]
        for p in returns:
            self.assertEqual(p.content, "corto")


class TestElideResponseParts(unittest.TestCase):
    """Capa 2b (2026-07-28): thinking viejo y args de calls respondidas."""

    def _run_with_thinking(self, n: int) -> list:
        msgs: list = [ModelRequest(parts=[UserPromptPart(content="dale")])]
        for i in range(n):
            cid = f"c{i}"
            msgs.append(ModelResponse(parts=[
                ThinkingPart(content="razonando " * 50),
                ToolCallPart(tool_name="write_file", args="w" * 3000,
                             tool_call_id=cid)]))
            msgs.append(ModelRequest(parts=[ToolReturnPart(
                tool_name="write_file", content="ok", tool_call_id=cid)]))
        return msgs

    def test_thinking_viejo_se_va_y_el_reciente_queda(self) -> None:
        msgs = self._run_with_thinking(experts.THINK_KEEP_FULL + 4)
        experts._elide_old_response_parts(msgs)
        resp = [m for m in msgs if isinstance(m, ModelResponse)]
        con_think = [m for m in resp
                     if any(isinstance(p, ThinkingPart) for p in m.parts)]
        self.assertEqual(len(con_think), experts.THINK_KEEP_FULL)
        self.assertEqual(con_think, resp[-experts.THINK_KEEP_FULL:])
        # el tool call sobrevive: sacar thinking no rompe el par
        for m in resp:
            self.assertTrue(any(isinstance(p, ToolCallPart) for p in m.parts))

    def test_args_viejos_stubbeados_y_json_valido(self) -> None:
        msgs = self._run_with_thinking(experts.TOOL_KEEP_FULL + 3)
        experts._elide_old_response_parts(msgs)
        calls = [p for m in msgs if isinstance(m, ModelResponse)
                 for p in m.parts if isinstance(p, ToolCallPart)]
        stubbed = [p for p in calls if p.args == experts._ELIDED_ARGS]
        self.assertEqual(len(stubbed), 3)
        json.loads(experts._ELIDED_ARGS)  # el provider tiene que poder parsearlo
        for p in calls[-experts.TOOL_KEEP_FULL:]:
            self.assertEqual(p.args, "w" * 3000)

    def test_call_sin_responder_nunca_se_toca(self) -> None:
        """Un par call↔return abierto roto = UserError en el turno siguiente."""
        msgs = self._run_with_thinking(experts.TOOL_KEEP_FULL + 3)
        msgs.append(ModelResponse(parts=[ToolCallPart(
            tool_name="write_file", args="z" * 3000, tool_call_id="huerfano")]))
        experts._elide_old_response_parts(msgs)
        orphan = [p for m in msgs if isinstance(m, ModelResponse)
                  for p in m.parts if isinstance(p, ToolCallPart)
                  and p.tool_call_id == "huerfano"][0]
        self.assertEqual(orphan.args, "z" * 3000)

    def test_no_deja_un_response_vacio(self) -> None:
        """Thinking solo, sin texto ni call: se deja. Un assistant sin
        contenido lo rechazan varios providers."""
        msgs = [ModelResponse(parts=[ThinkingPart(content="solo pienso")])]
        msgs += self._run_with_thinking(experts.THINK_KEEP_FULL + 1)
        experts._elide_old_response_parts(msgs)
        self.assertEqual(len(msgs[0].parts), 1)

    def test_idempotente(self) -> None:
        msgs = self._run_with_thinking(experts.TOOL_KEEP_FULL + 3)
        experts._elide_old_response_parts(msgs)
        snapshot = experts._dump_messages(msgs)
        experts._elide_old_response_parts(msgs)
        self.assertEqual(experts._dump_messages(msgs), snapshot)


class TestSlimHistory(unittest.TestCase):
    def test_old_turns_lose_tools_last_turn_intact(self) -> None:
        old = _turn("pregunta 1", [("tool_a", "z" * 5000)], "respuesta 1")
        recent = _turn("pregunta 2", [("tool_b", "data")], "respuesta 2")
        slim = experts._slim_history(old + recent)
        # el turno viejo quedó user + text, sin partes de tools
        for m in slim[:-len(recent)]:
            for p in getattr(m, "parts", []):
                self.assertIsInstance(p, (UserPromptPart, TextPart))
        # el turno reciente viaja completo (mismo count de mensajes)
        self.assertEqual(slim[-len(recent):], recent)

    def test_single_turn_untouched(self) -> None:
        msgs = _turn("solo una", [("t", "r")], "ok")
        self.assertEqual(experts._slim_history(list(msgs)), msgs)

    def test_shrinks_size(self) -> None:
        old = _turn("p1", [("t", "w" * 20000)] * 3, "r1")
        recent = _turn("p2", [], "r2")
        full = old + recent
        slim = experts._slim_history(list(full))
        size = lambda ms: sum(  # noqa: E731
            len(str(getattr(p, "content", ""))) for m in ms
            for p in getattr(m, "parts", []))
        self.assertLess(size(slim), size(full) * 0.1)


class TestNativeToolsAreCapped(unittest.TestCase):
    """2026-07-28: las tools nativas iban por `tools=` y se salteaban las
    capas 1 y 2 (el FunctionToolset interno de Agent no se puede
    envolver). Este test falla si alguien vuelve a pasarlas por ahí."""

    def test_native_tool_result_capped_in_history(self) -> None:
        import asyncio

        from pydantic_ai import Agent, Tool
        from pydantic_ai.models.test import TestModel
        from pydantic_ai.toolsets import FunctionToolset

        fat = "x" * (experts.TOOL_RESULT_CAP + 10_000)

        async def gorda() -> str:
            """Devuelve un result gordo."""
            return fat

        agent = Agent(
            TestModel(call_tools=["gorda"]),
            toolsets=[experts.CappedToolset(wrapped=FunctionToolset(
                [Tool(gorda, takes_ctx=False)], max_retries=3))],
        )
        res = asyncio.run(agent.run("dale"))
        returns = [p for m in res.all_messages() if isinstance(m, ModelRequest)
                   for p in m.parts if isinstance(p, ToolReturnPart)]
        self.assertEqual(len(returns), 1)
        self.assertLess(len(returns[0].content), len(fat))
        self.assertIn("TRUNCADO", returns[0].content)


class TestCappedToolsetTimeout(unittest.IsolatedAsyncioTestCase):
    """2026-08-02: un tool-call MCP colgado (p.ej. `run_shell` con un server
    en foreground) no debe matar el run entero. pydantic-ai aplica su
    `tool_timeout` SOLO al FunctionToolset interno; a un MCPToolset le queda
    el read_timeout default (300s), por encima del idle watchdog (180s), así
    que el watchdog cortaba el run. CappedToolset con `timeout` corta el
    tool-call a tiempo y lo convierte en ModelRetry accionable."""

    async def test_hanging_tool_becomes_modelretry(self) -> None:
        import asyncio
        import time

        from pydantic_ai.exceptions import ModelRetry

        class _Hang:
            label = "hang"

            async def call_tool(self, name, tool_args, ctx, tool):
                await asyncio.sleep(3600)  # nunca devuelve

        capped = experts.CappedToolset(wrapped=_Hang(), timeout=0.2)
        t0 = time.monotonic()
        with self.assertRaises(ModelRetry) as cm:
            await capped.call_tool("run_shell", {}, None, None)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 5.0, "no cortó a tiempo (¿quedó colgado?)")
        self.assertIn("run_shell", str(cm.exception))

    async def test_no_timeout_passes_through(self) -> None:
        """timeout=None (tools nativas): delega tal cual, sin wait_for."""
        import types

        class _Fast:
            label = "fast"

            async def call_tool(self, name, tool_args, ctx, tool):
                return "ok"

        ctx = types.SimpleNamespace(messages=[])
        capped = experts.CappedToolset(wrapped=_Fast(), timeout=None)
        self.assertEqual(await capped.call_tool("x", {}, ctx, None), "ok")


if __name__ == "__main__":
    unittest.main()
