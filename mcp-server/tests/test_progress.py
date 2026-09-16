"""Tests de Fase 1: liveness on-demand (A) + progreso SSE al bot (B).

Cubre:
  - run_expert: messages_json persiste igual (contrato ADR-025)
  - run_expert: phase_at_end y last_tool se devuelven en el dict
  - run_expert: on_progress se invoca con (thinking, tool_call)
  - server: GET /experts/status/{chat_id} devuelve snapshot durante run
  - server: GET /experts/status/{chat_id} devuelve 404 si no existe
  - server: notify recibe kind=progress durante tool_call (TestModel
    con custom_tool no llama tools reales; usamos un Tool nativo propio
    que se ejecuta via TestModel.call_tools=[...] — ver `build_agent`
    helper más abajo)
  - db: finish_chat persiste phase_at_end y last_tool

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_progress.py -q
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from relay import experts
from relay.db import Database
from relay.notify import NotifyClient

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR")


# ---------- helpers ----------


def _tmp_env(tmp: Path) -> None:
    """Setea FOURBIS_* en el env para que create_app() use el tmp."""
    os.environ["FOURBIS_DB_PATH"] = str(tmp / "test.db")
    os.environ["FOURBIS_CHATS_DIR"] = str(tmp / "chats")
    os.environ["FOURBIS_JSONL_DIR"] = str(tmp / "jsonl")


def _clear_env() -> None:
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


async def _wait(predicate, timeout: float = 10.0):
    """Espera async hasta que predicate() sea truthy. Falla con timeout."""
    import time
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("timeout esperando condición async")


# ---------- run_expert directo ----------


class TestRunExpertProgress(unittest.IsolatedAsyncioTestCase):
    """Cambio interno: run_expert ahora usa Agent.iter() y emite
    progreso. Estos tests verifican que el contrato externo no cambió."""

    async def test_returns_phase_at_end_and_last_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = {
                "slug": "demo", "repo_path": tmp,
                "system_prompt": "", "mcp_servers": [],
                "defaults_json": {}, "native_tools": [],
            }
            r = await experts.run_expert(project, "hola", model_override="test")
            # TestModel con call_tools=[]: no ejecuta tools. phase queda
            # en "writing" (ModelResponseNode) — suficiente para validar
            # que el campo se está devolviendo.
            self.assertIn("phase_at_end", r)
            self.assertIn(r["phase_at_end"], ("thinking", "tool_call", "writing"))
            self.assertIn("last_tool", r)
            self.assertIsNone(r["last_tool"])

    async def test_on_progress_callback_invoked(self) -> None:
        """El callback se llama al menos una vez por nodo del agente."""
        with tempfile.TemporaryDirectory() as tmp:
            project = {
                "slug": "demo", "repo_path": tmp,
                "system_prompt": "", "mcp_servers": [],
                "defaults_json": {}, "native_tools": [],
            }
            calls: list[dict] = []

            async def cb(*, phase: str, tool, tool_calls=None):
                calls.append({"phase": phase, "tool": tool,
                              "tool_calls": tool_calls})

            r = await experts.run_expert(project, "hola",
                                         model_override="test",
                                         on_progress=cb)
            # Mínimo 1 llamada (el "thinking" inicial).
            self.assertGreaterEqual(len(calls), 1)
            self.assertEqual(calls[0]["phase"], "thinking")
            # TestModel sin tools → no debería haber tool_call en el log
            tool_calls = [c for c in calls if c["phase"] == "tool_call"]
            self.assertEqual(len(tool_calls), 0)

    async def test_messages_json_still_persists(self) -> None:
        """Contrato ADR-025: messages_json sigue siendo el historial
        serializado, callable para re-validar con ModelMessagesTypeAdapter.
        """
        from pydantic_ai.messages import ModelMessagesTypeAdapter
        with tempfile.TemporaryDirectory() as tmp:
            project = {
                "slug": "demo", "repo_path": tmp,
                "system_prompt": "", "mcp_servers": [],
                "defaults_json": {}, "native_tools": [],
            }
            r1 = await experts.run_expert(project, "primer turno",
                                          model_override="test")
            self.assertTrue(r1["messages_json"])
            # Re-validable
            history = ModelMessagesTypeAdapter.validate_json(r1["messages_json"])
            self.assertGreater(len(history), 0)
            # Replay crece el historial (igual que test_conversations)
            r2 = await experts.run_expert(project, "segundo turno",
                                          model_override="test",
                                          message_history_json=r1["messages_json"])
            self.assertGreater(len(r2["messages_json"]),
                               len(r1["messages_json"]))


class TestMakeProgressCallback(unittest.IsolatedAsyncioTestCase):
    """El helper que arma RunProgress + callback."""

    async def test_stores_progress_and_emits_notify(self) -> None:
        store: dict = {}
        notifications: list[dict] = []

        class FakeNotify:
            async def send(self, agent_id, kind, message, metadata=None):
                notifications.append({"agent_id": agent_id, "kind": kind,
                                      "message": message,
                                      "metadata": metadata or {}})
                return True

        cb = experts.make_progress_callback(
            store=store, notify=FakeNotify(),
            chat_id="cid-1", target="demo", model="test",
        )
        self.assertIn("cid-1", store)
        rp = store["cid-1"]
        self.assertEqual(rp.phase, "thinking")
        self.assertEqual(rp.target, "demo")

        # Simular un tool_call
        await cb(phase="tool_call", tool="cbm_query", tool_calls=1)
        self.assertEqual(rp.phase, "tool_call")
        self.assertEqual(rp.last_tool, "cbm_query")
        self.assertEqual(rp.tool_calls, 1)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["kind"], "progress")
        self.assertEqual(notifications[0]["metadata"]["tool"], "cbm_query")

        # Un "thinking" posterior NO debe notificar (filtrado en el cb)
        await cb(phase="thinking", tool=None)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(rp.phase, "thinking")

    async def test_progress_carries_message_and_diff(self) -> None:
        """2026-07-20d: el cb reenvía la línea legible (`message`) y el
        diff de edit_file al notify para el timeline vivo del bot."""
        store: dict = {}
        notifications: list[dict] = []

        class FakeNotify:
            async def send(self, agent_id, kind, message, metadata=None):
                notifications.append({"message": message,
                                      "metadata": metadata or {}})
                return True

        cb = experts.make_progress_callback(
            store=store, notify=FakeNotify(),
            chat_id="cid-2", target="demo", model="test")

        await cb(phase="tool_call", tool="edit_file", tool_calls=1,
                 message="✏️ editó `a.cs` (+1 −1)",
                 diff="--- a.cs\n+++ a.cs\n-int x = 1;\n+int x = 10;")
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["message"], "✏️ editó `a.cs` (+1 −1)")
        self.assertIn("+int x = 10;", notifications[0]["metadata"]["diff"])

        # Sin diff (tool no-edit): no aparece la clave `diff` en metadata.
        await cb(phase="tool_call", tool="read_file", tool_calls=2,
                 message="📄 leyó `a.cs`")
        self.assertEqual(notifications[1]["message"], "📄 leyó `a.cs`")
        self.assertNotIn("diff", notifications[1]["metadata"])

    async def test_steps_are_stored_for_live_web_panel(self) -> None:
        """Fase 3b: el cb guarda los pasos ricos en RunProgress.steps y
        salen en snapshot() — es lo que el panel web poll-ea para renderear
        el hilo en vivo con path + diff (igual que el embed de Discord).
        Sin notify (el bot puede estar caído): los steps se guardan igual.
        """
        store: dict = {}
        cb = experts.make_progress_callback(
            store=store, notify=None,
            chat_id="cid-3", target="demo", model="test")
        rp = store["cid-3"]

        await cb(phase="tool_call", tool="read_file", tool_calls=1,
                 message="📄 leyó `a.cs`")
        await cb(phase="tool_call", tool="edit_file", tool_calls=2,
                 message="✏️ editó `a.cs` (+1 −1)", diff="-viejo\n+nuevo")

        self.assertEqual(len(rp.steps), 2)
        # `kind` (2026-07-25) distingue estos pasos de los de narración
        # ("say") y de las correcciones del humano ("steer").
        self.assertEqual(rp.steps[0],
            {"n": 1, "kind": "tool_call", "tool": "read_file",
             "message": "📄 leyó `a.cs`", "diff": None})
        self.assertEqual(rp.steps[1]["n"], 2)
        self.assertIn("+nuevo", rp.steps[1]["diff"])
        # El snapshot los expone (lo que consume /experts/status).
        snap = rp.snapshot()
        self.assertEqual(len(snap["steps"]), 2)
        json.dumps(snap)  # debe seguir siendo JSON-serializable

        # Las fases sin tool NO generan steps (no ensucian el hilo).
        await cb(phase="writing", tool=None)
        self.assertEqual(len(rp.steps), 2)

    async def test_steps_are_capped(self) -> None:
        """El relay guarda solo los últimos STEPS_KEEP: un run de 200 tool
        calls no puede inflar el payload de /experts/status."""
        store: dict = {}
        cb = experts.make_progress_callback(
            store=store, notify=None,
            chat_id="cid-4", target="demo", model="test")
        rp = store["cid-4"]
        total = experts.STEPS_KEEP + 15
        for i in range(1, total + 1):
            await cb(phase="tool_call", tool="read_file", tool_calls=i,
                     message=f"paso {i}")
        self.assertEqual(len(rp.steps), experts.STEPS_KEEP)
        # Conserva los MÁS NUEVOS (el web ya renderizó los viejos).
        self.assertEqual(rp.steps[-1]["n"], total)
        self.assertEqual(rp.steps[0]["n"], total - experts.STEPS_KEEP + 1)

    def test_narracion_corta_pasa_intacta(self) -> None:
        """Caso real (website-demo, 2026-08-01): una tabla de opciones A/B/C/D
        de 1086 chars se cortaba a 700 y el usuario solo veía la fila A —
        después el modelo le decía "elegí de la tabla". Con el tope en 1800
        pasa entera."""
        tabla = "| opción | qué necesitás |\n" + ("| A. Reviso local | nada |\n" * 40)
        self.assertGreater(len(tabla), 1000)
        self.assertLess(len(tabla), experts.SAY_MAX_CHARS)
        self.assertEqual(experts._clip_say(tabla), tabla)
        self.assertNotIn(experts.SAY_CLIP_MARK, experts._clip_say(tabla))

    def test_narracion_larga_se_corta_con_marca(self) -> None:
        """El corte tiene que ser VISIBLE: sin marca parece que el modelo
        escribe frases truncadas (así se manifestó el bug)."""
        largo = "x" * (experts.SAY_MAX_CHARS + 500)
        out = experts._clip_say(largo)
        self.assertTrue(out.endswith(experts.SAY_CLIP_MARK))
        self.assertEqual(len(out), experts.SAY_MAX_CHARS + len(experts.SAY_CLIP_MARK))

    async def test_snapshot_serializable(self) -> None:
        import json
        store: dict = {}
        cb = experts.make_progress_callback(
            store=store, notify=None,
            chat_id="cid-x", target="t", model="m",
        )
        await cb(phase="tool_call", tool="read_file", tool_calls=1)
        snap = store["cid-x"].snapshot()
        # Debe ser JSON-serializable (lo mandamos al cliente así)
        json.dumps(snap)
        self.assertEqual(snap["phase"], "tool_call")
        self.assertEqual(snap["last_tool"], "read_file")
        self.assertEqual(snap["tool_calls"], 1)
        self.assertGreaterEqual(snap["elapsed_s"], 0.0)
        self.assertGreaterEqual(snap["idle_s"], 0.0)


# ---------- server: endpoint /experts/status ----------


class TestExpertsStatusEndpoint(unittest.IsolatedAsyncioTestCase):
    """GET /experts/status/{chat_id} — liveness on-demand."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)

        db = Database()
        await db.init_schema()
        await db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name,
            "defaults_json": {"model": "test"},
        })

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    async def test_404_when_no_run(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, agent_id, kind, message, metadata=None):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get("/experts/status/00000000")
                self.assertEqual(r.status, 404)

    async def test_404_after_restart(self) -> None:
        """Sin progress store, devolvemos 404 con hint de restart."""
        from relay.server import create_app, DB_KEY
        async def fake_send(self_, agent_id, kind, message, metadata=None):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                # existe un chat en SQLite pero NO hay progress store
                # (simula relay reiniciado).
                db: Database = app[DB_KEY]
                cid = await db.create_chat(
                    project_slug="demo", source="test", author="x",
                    target="demo")
                r = await client.get(f"/experts/status/{cid}")
                self.assertEqual(r.status, 404)
                body = await r.json()
                self.assertTrue(body.get("lost_on_restart_possible"))

    async def test_snapshot_during_run(self) -> None:
        """Mientras el run corre, /status devuelve snapshot vivo.
        Después de terminar, /status devuelve finished=true."""
        from relay.server import create_app
        notifications: list[dict] = []

        async def fake_send(self_, agent_id, kind, message, metadata=None):
            notifications.append({"agent_id": agent_id, "kind": kind,
                                  "message": message,
                                  "metadata": metadata or {}})
            return True

        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.post("/experts/run", json={
                    "target": "demo", "user": "hola",
                    "source": "test", "author": "pytest",
                })
                self.assertEqual(r.status, 202)
                chat_id = (await r.json())["id"]

                # status mientras corre: phase puede ser thinking/tool_call
                # según timing, pero el snapshot debe existir.
                r = await client.get(f"/experts/status/{chat_id[:8]}")
                self.assertEqual(r.status, 200)
                snap = await r.json()
                self.assertEqual(snap["target"], "demo")
                self.assertIn(snap["phase"], ("thinking", "tool_call", "writing",
                                               "planner", "verifier",
                                               "documenter"))
                self.assertFalse(snap["finished"])

                # esperar a que termine
                await _wait(lambda: len(notifications) > 0
                            and notifications[-1]["kind"] == "response")

                # status post-mortem: finished=true. TestModel responde
                # tan rápido que la phase puede no haber avanzado de
                # "thinking" — lo único que garantizamos acá es finished.
                r = await client.get(f"/experts/status/{chat_id}")
                self.assertEqual(r.status, 200)
                snap = await r.json()
                self.assertTrue(snap["finished"])
                self.assertIn(snap["phase"],
                              ("thinking", "tool_call", "writing", "done",
                               "planner", "verifier", "documenter"))

    async def test_run_emits_progress_then_response_e2e(self) -> None:
        """END-TO-END de la data que colgaba a Discord: un run que usa una
        tool emite notify(kind=progress) DURANTE y notify(kind=response) al
        FINAL. El bot debe tratar progress como NO-terminal y cerrar solo en
        response — antes cerraba en el primer progress → embed colgado en 🟡.

        TestModel default (call_tools=[]) no llama tools, así que forzamos un
        TestModel que llame `cbm_query` (cbm mockeado) para reproducir la
        secuencia real progress→response."""
        from relay.server import create_app
        from pydantic_ai.models.test import TestModel

        notifications: list[dict] = []

        async def fake_send(self_, agent_id, kind, message, metadata=None):
            notifications.append({"agent_id": agent_id, "kind": kind})
            return True

        async def fake_cbm(tool, args, *, timeout=30.0):
            return '{"results": []}'

        with patch.object(NotifyClient, "send", fake_send), \
             patch.object(experts, "cbm_binary_path", lambda: "cbm"), \
             patch.object(experts, "cbm_call", fake_cbm), \
             patch.object(experts, "build_model",
                          lambda spec: TestModel(call_tools=["cbm_query"])):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.post("/experts/run", json={
                    "target": "demo", "user": "busca el timeout",
                    "source": "test", "author": "pytest",
                })
                self.assertEqual(r.status, 202)
                chat_id = (await r.json())["id"]
                await _wait(lambda: any(
                    n["kind"] == "response" for n in notifications))

        kinds = [n["kind"] for n in notifications]
        # la data exacta: al menos un progress, y el ÚLTIMO es response
        self.assertIn("progress", kinds, f"sin progress en {kinds}")
        self.assertEqual(kinds[-1], "response", f"secuencia: {kinds}")
        # el progress vino ANTES del response terminal (no al revés)
        self.assertLess(kinds.index("progress"), len(kinds) - 1)
        # todos los notify son del mismo chat (el bot los rutea por chat_id)
        self.assertTrue(
            all(n["agent_id"] == f"chat:{chat_id}" for n in notifications))


# ---------- db: fase_at_end + last_tool persiste ----------


class TestFinishChatPersistsPhase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(path=Path(self._tmp.name) / "test.db")
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name})

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_finish_chat_writes_phase_and_tool(self) -> None:
        cid = await self.db.create_chat(
            project_slug="demo", source="test", author="x", target="demo")
        await self.db.finish_chat(
            cid, status="ok", tokens_in=10, tokens_out=20, tool_calls=2,
            phase_at_end="tool_call", last_tool="cbm_query",
        )
        chat = await self.db.get_chat(cid)
        self.assertEqual(chat["phase_at_end"], "tool_call")
        self.assertEqual(chat["last_tool"], "cbm_query")

    async def test_finish_chat_default_phase_null(self) -> None:
        """Sin phase_at_end ni last_tool, las columnas quedan en NULL."""
        cid = await self.db.create_chat(
            project_slug="demo", source="test", author="x", target="demo")
        await self.db.finish_chat(cid, status="ok")
        chat = await self.db.get_chat(cid)
        self.assertIsNone(chat["phase_at_end"])
        self.assertIsNone(chat["last_tool"])


if __name__ == "__main__":
    unittest.main()