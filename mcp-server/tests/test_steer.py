"""Tests del steer en vivo + la narración del experto (2026-07-25).

El agujero que los motivó: el panel de chat mostraba las tool calls en
vivo pero no POR QUÉ las hacía, y mientras el run corría el composer
estaba deshabilitado — verlo irse por el camino equivocado y no poder
decirle nada. La única salida era cancelar, que además tiraba todo el
avance (el historial rescatado moría en el early-return de
`_run_expert_bg`).

Cubre:
  - run_expert: una corrección encolada en `steer` corta el run en el
    borde de nodo, rescata el historial y re-entra con ese texto como
    prompt (no pierde las tools ya hechas)
  - run_expert: `rescue` sale con el historial cuando el run se cancela
  - make_progress_callback: los pasos "say"/"steer" van al timeline con
    `n` monótono y NO pisan la fase real ni notifican al bot
  - server: POST /experts/steer encola; 409 si el run ya terminó, 404 si
    no existe

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_steer.py -q
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic_ai import Tool
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.toolsets import FunctionToolset

from relay import experts
from relay.db import Database
from relay.notify import NotifyClient

NUDGE = "Detén: el bug está en admin.py, no en config.py"


def _ping_toolset(*_a, **_k) -> list:
    def ping(x: int) -> int:
        """ping"""
        return x
    return [FunctionToolset(tools=[Tool(ping, takes_ctx=False)])]


def _last_user_text(messages) -> str:
    """Último UserPromptPart del historial (el prompt de esta tanda)."""
    for m in reversed(messages):
        for p in getattr(m, "parts", []):
            if isinstance(p, UserPromptPart):
                return str(p.content or "")
    return ""


def _steerable_model(steer: list[str]) -> FunctionModel:
    """Modelo que llamaría `ping` para siempre, salvo que le llegue la
    corrección como prompt. Encola el steer en la 1ª llamada: eso es
    exactamente un humano posteando /experts/steer a mitad del run."""
    calls = {"n": 0}

    def act(messages, info):
        if NUDGE in _last_user_text(messages):
            return ModelResponse(parts=[
                TextPart("ok, me voy a admin.py como pediste")])
        calls["n"] += 1
        # En la 2ª pasada: así la 1ª se procesa completa (narración +
        # tool ejecutada) y el corte cae sobre una tool que NUNCA corre —
        # que es el contrato: el steer se aplica en el borde de nodo.
        if calls["n"] == 2:
            steer.append(NUDGE)
        names = [t.name for t in info.function_tools]
        name = "ping" if "ping" in names else names[0]
        return ModelResponse(parts=[
            TextPart("voy a mirar config.py"),
            ToolCallPart(name, {"x": 1}),
        ])
    return FunctionModel(act)


def _project(tmp: str) -> dict:
    return {
        "slug": "demo", "repo_path": tmp, "id": 1,
        "system_prompt": "", "mcp_servers": [],
        # timeout > ROUND_MIN_S (30s): re-entrar exige ese margen.
        "defaults_json": {"model": "fake", "timeout": 300},
        "native_tools": [],
    }


class TestSteer(unittest.IsolatedAsyncioTestCase):
    async def test_steer_cuts_and_reenters_with_the_correction(self) -> None:
        """La corrección corta el run y se aplica SIN perder lo hecho."""
        async def _fake_catalog(*_a, **_k):
            return (_ping_toolset(), [], [])
        steer: list[str] = []
        events: list[dict] = []

        async def on_progress(**kw):
            events.append(kw)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(experts, "build_model",
                              lambda spec: _steerable_model(steer)), \
                    patch.object(experts, "_catalog_toolsets", _fake_catalog), \
                    patch.object(experts.config, "expert_request_limit",
                                 lambda: 50):
                r = await experts.run_expert(
                    _project(tmp), "arregla el bug de CORS", db=object(),
                    steer=steer, on_progress=on_progress)

        # Re-entró una vez con el texto del humano y terminó obedeciendo.
        self.assertEqual(r["steers"], 1, "no re-entró con la corrección")
        self.assertIn("admin.py", r["content"])
        self.assertNotEqual(r["phase_at_end"], "steered")
        self.assertFalse(steer, "la cola quedó sin consumir")
        # El trabajo previo al corte sigue en el historial (esto es lo que
        # cancelar-y-reescribir tiraba a la basura).
        self.assertTrue(r["messages_json"])
        self.assertIn("config.py", r["messages_json"])
        # El timeline recibió el POR QUÉ y la corrección, no solo tools.
        phases = [e["phase"] for e in events]
        self.assertIn("say", phases, "no emitió la narración del modelo")
        self.assertIn("steer", phases, "la corrección no salió al timeline")
        says = [e["message"] for e in events if e["phase"] == "say"]
        self.assertTrue(any("config.py" in s for s in says))

    async def test_cancel_leaves_the_history_in_rescue(self) -> None:
        """Al cancelar, `rescue` sale con el historial parcial: server.py
        lo persiste y el hilo queda reanudable con "continúa"."""
        async def _fake_catalog(*_a, **_k):
            return (_ping_toolset(), [], [])

        def _slow_model() -> FunctionModel:
            def act(messages, info):
                names = [t.name for t in info.function_tools]
                name = "ping" if "ping" in names else names[0]
                return ModelResponse(parts=[
                    TextPart("laburando"), ToolCallPart(name, {"x": 1})])
            return FunctionModel(act)

        rescue: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(experts, "build_model",
                              lambda spec: _slow_model()), \
                    patch.object(experts, "_catalog_toolsets", _fake_catalog), \
                    patch.object(experts.config, "expert_request_limit",
                                 lambda: 500):
                task = asyncio.create_task(experts.run_expert(
                    _project(tmp), "itera", db=object(), rescue=rescue))
                # Dejar que haga algunas vueltas y cancelar como el humano.
                await asyncio.sleep(0.4)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertTrue(rescue.get("messages_json"),
                        "el cancel tiró el avance (bug 2026-07-25)")
        msgs = json.loads(rescue["messages_json"])
        self.assertGreater(len(msgs), 0)
        self.assertGreaterEqual(rescue.get("tool_calls", 0), 1)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_steer_counts_tools_before_and_after_reentry(tmp_path, cancelled):
    """Dos tools previas + una tras corregir cuentan tres al cerrar o cancelar."""
    steer: list[str] = []
    executed: list[int] = []
    final_request = asyncio.Event()

    def ping(x: int) -> int:
        """Registra trabajo real de ambas pasadas."""
        executed.append(x)
        return x

    async def catalog(*_a, **_k):
        return ([FunctionToolset(tools=[Tool(ping, takes_ctx=False)])], [], [])

    async def act(messages, info):
        if not executed:
            return ModelResponse(parts=[
                ToolCallPart("ping", {"x": n}, tool_call_id=f"ping-{n}")
                for n in (1, 2)])
        if NUDGE not in _last_user_text(messages):
            steer.append(NUDGE)
            return ModelResponse(parts=[TextPart("cambio de rumbo")])
        if len(executed) == 2:
            return ModelResponse(parts=[
                ToolCallPart("ping", {"x": 3}, tool_call_id="ping-3")])
        final_request.set()
        if cancelled:
            await asyncio.Event().wait()
        return ModelResponse(parts=[TextPart("listo")])

    rescue: dict = {}
    with patch.object(experts, "build_model", return_value=FunctionModel(act)), \
            patch.object(experts, "_catalog_toolsets", catalog), \
            patch.object(experts.config, "expert_request_limit", return_value=50):
        task = asyncio.create_task(experts.run_expert(
            _project(str(tmp_path)), "haz la tarea", db=object(),
            steer=steer, rescue=rescue))
        try:
            await asyncio.wait_for(final_request.wait(), timeout=5)
            if cancelled:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                result = rescue
            else:
                result = await asyncio.wait_for(task, timeout=5)
                assert result["steers"] == 1
                assert result["legs"] == 1
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    assert sorted(executed) == [1, 2, 3]
    events = [e for e in result["progress_events"] if e["phase"] == "tool_call"]
    assert [e["tool_call_id"] for e in events] == ["ping-1", "ping-2", "ping-3"]
    assert [e["tool_calls"] for e in events] == [1, 2, 3]
    assert result["tool_calls"] == 3


class TestNarrationSteps(unittest.IsolatedAsyncioTestCase):
    async def test_say_and_steer_steps_dont_clobber_the_phase(self) -> None:
        notifications: list[dict] = []

        class _Notify:
            async def send(self_, **kw):
                notifications.append(kw)

        store: dict = {}
        cb = experts.make_progress_callback(
            store=store, notify=_Notify(), chat_id="cid-s", target="demo",
            model="test")
        rp = store["cid-s"]

        await cb(phase="say", tool=None, message="voy a leer admin.py")
        await cb(phase="tool_call", tool="read_file", tool_calls=1,
                 message="📄 leyó `admin.py`")
        await cb(phase="steer", tool=None, message="no, mira config.py")

        # `n` monótono aunque se intercalen narración y tools: la UI
        # deduplica con n > lastStepN y con n=tool_calls se comía pasos.
        self.assertEqual([s["n"] for s in rp.steps], [1, 2, 3])
        self.assertEqual([s["kind"] for s in rp.steps],
                         ["say", "tool_call", "steer"])
        # La fase real la marca la tool, no la narración.
        self.assertEqual(rp.phase, "tool_call")
        # El bot solo entiende pasos de tool: una notificación, no tres.
        self.assertEqual(len(notifications), 1)
        json.dumps(rp.snapshot())  # sigue siendo serializable


# ---------- endpoint ----------

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR")


class TestSteerEndpoint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        db = Database()
        await db.init_schema()
        await db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name,
            "defaults_json": {"model": "test"},
        })

    async def asyncTearDown(self) -> None:
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp.cleanup()

    async def test_queue_404_and_409(self) -> None:
        from relay.server import create_app, PROGRESS_KEY

        async def fake_send(self_, agent_id, kind, message, metadata=None):
            return True

        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                # sin run vivo → 404 (la UI cae a mensaje normal)
                r = await client.post("/experts/steer/deadbeef",
                                      json={"message": "x"})
                self.assertEqual(r.status, 404)

                cb = experts.make_progress_callback(
                    store=app[PROGRESS_KEY], notify=None,
                    chat_id="cafe1234", target="demo", model="test")
                self.assertTrue(callable(cb))
                rp = app[PROGRESS_KEY]["cafe1234"]

                # body vacío → 400 (no encolamos ruido)
                r = await client.post("/experts/steer/cafe1234",
                                      json={"message": "   "})
                self.assertEqual(r.status, 400)

                # por prefijo, como /status y /cancel
                r = await client.post("/experts/steer/cafe12",
                                      json={"message": NUDGE})
                self.assertEqual(r.status, 200)
                self.assertEqual((await r.json())["queued"], 1)
                self.assertEqual(rp.steer, [NUDGE])

                # run terminado → 409: ya no es un steer, es un turno nuevo
                rp.finished = True
                r = await client.post("/experts/steer/cafe1234",
                                      json={"message": NUDGE})
                self.assertEqual(r.status, 409)


if __name__ == "__main__":
    unittest.main()
