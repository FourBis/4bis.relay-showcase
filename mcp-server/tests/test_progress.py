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
from relay import expert_models, expert_runner, progress

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


async def _wait_for_chat(db: Database, chat_id: str) -> dict:
    for _ in range(200):
        chat = await db.get_chat(chat_id)
        if chat and chat["status"] in {"ok", "error"} and chat.get("md_path"):
            return chat
        await asyncio.sleep(0.05)
    raise AssertionError(f"el run {chat_id} no terminó")


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
            r = await expert_runner.run_expert(project, "hola", model_override="test")
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

            r = await expert_runner.run_expert(project, "hola",
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
            r1 = await expert_runner.run_expert(project, "primer turno",
                                          model_override="test")
            self.assertTrue(r1["messages_json"])
            # Re-validable
            history = ModelMessagesTypeAdapter.validate_json(r1["messages_json"])
            self.assertGreater(len(history), 0)
            # Replay crece el historial (igual que test_conversations)
            r2 = await expert_runner.run_expert(project, "segundo turno",
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

        cb = progress.make_progress_callback(
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

        cb = progress.make_progress_callback(
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
        cb = progress.make_progress_callback(
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
        cb = progress.make_progress_callback(
            store=store, notify=None,
            chat_id="cid-4", target="demo", model="test")
        rp = store["cid-4"]
        total = progress.STEPS_KEEP + 15
        for i in range(1, total + 1):
            await cb(phase="tool_call", tool="read_file", tool_calls=i,
                     message=f"paso {i}")
        self.assertEqual(len(rp.steps), progress.STEPS_KEEP)
        # Conserva los MÁS NUEVOS (el web ya renderizó los viejos).
        self.assertEqual(rp.steps[-1]["n"], total)
        self.assertEqual(rp.steps[0]["n"], total - progress.STEPS_KEEP + 1)

    def test_narracion_corta_pasa_intacta(self) -> None:
        """Caso real (website-demo, 2026-08-01): una tabla de opciones A/B/C/D
        de 1086 chars se cortaba a 700 y el usuario solo veía la fila A —
        después el modelo le decía "elegí de la tabla". Con el tope en 1800
        pasa entera."""
        tabla = "| opción | qué necesitás |\n" + ("| A. Reviso local | nada |\n" * 40)
        self.assertGreater(len(tabla), 1000)
        self.assertLess(len(tabla), progress.SAY_MAX_CHARS)
        self.assertEqual(progress._clip_say(tabla), tabla)
        self.assertNotIn(progress.SAY_CLIP_MARK, progress._clip_say(tabla))

    def test_narracion_larga_se_corta_con_marca(self) -> None:
        """El corte tiene que ser VISIBLE: sin marca parece que el modelo
        escribe frases truncadas (así se manifestó el bug)."""
        largo = "x" * (progress.SAY_MAX_CHARS + 500)
        out = progress._clip_say(largo)
        self.assertTrue(out.endswith(progress.SAY_CLIP_MARK))
        self.assertEqual(len(out), progress.SAY_MAX_CHARS + len(progress.SAY_CLIP_MARK))

    async def test_snapshot_serializable(self) -> None:
        import json
        store: dict = {}
        cb = progress.make_progress_callback(
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
        await db.set_config("FOURBIS_MODEL", "test")
        await db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name,
            "defaults_json": {"model": "test"},
        })

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    async def test_404_when_no_run(self) -> None:
        from relay.server import DB_KEY, create_app
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
        """El snapshot HTTP refleja progreso vivo y luego su cierre durable."""
        from relay.server import DB_KEY, create_app

        send = AsyncMock()
        started, release = asyncio.Event(), asyncio.Event()
        original_runner = experts.run_expert_staged

        async def gated_runner(*args, **kwargs):
            started.set()
            await release.wait()
            return await original_runner(*args, **kwargs)

        with patch.object(NotifyClient, "send", send), \
                patch.object(experts, "run_expert_staged", gated_runner):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.post("/experts/run", json={
                    "target": "demo", "user": "hola",
                    "source": "test", "author": "pytest",
                })
                self.assertEqual(r.status, 202)
                chat_id = (await r.json())["id"]
                db: Database = app[DB_KEY]
                try:
                    await asyncio.wait_for(started.wait(), timeout=2)
                    r = await client.get(f"/experts/status/{chat_id}")
                    self.assertEqual(r.status, 200)
                    snap = await r.json()
                    self.assertEqual(snap["target"], "demo")
                    self.assertIn(snap["phase"], ("thinking", "tool_call", "writing",
                                                   "planner", "verifier", "documenter"))
                    self.assertFalse(snap["finished"])
                finally:
                    release.set()
                chat = await _wait_for_chat(db, chat_id)
                self.assertEqual(chat["status"], "ok")
                self.assertTrue(json.loads(chat["progress_events"]))
                r = await client.get(f"/experts/status/{chat_id}")
                self.assertEqual(r.status, 200)
                self.assertTrue((await r.json())["finished"])
                send.assert_not_awaited()

    async def test_run_emits_progress_then_response_e2e(self) -> None:
        """Una lectura autorizada deja progreso y retorno ordenados en SQLite."""
        from relay.server import DB_KEY, create_app
        from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
        from pydantic_ai.models.function import FunctionModel

        send = AsyncMock()
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            skill = skills_dir / "probe" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                "---\nname: probe\ndescription: test\n---\ntexto local",
                encoding="utf-8")

            def model(messages, _info):
                if any(isinstance(part, ToolReturnPart)
                       for message in messages for part in message.parts):
                    return ModelResponse(parts=[TextPart("Leí texto local.")])
                return ModelResponse(parts=[ToolCallPart(
                    "read_skill", {"name": "probe"}, tool_call_id="skill-1")])

            with patch.dict(os.environ, {"FOURBIS_SKILLS_DIR": str(skills_dir)}), \
                    patch.object(NotifyClient, "send", send), \
                    patch.object(expert_models, "build_model",
                                 lambda spec: FunctionModel(model)):
                app = create_app()
                async with TestClient(TestServer(app)) as client:
                    db: Database = app[DB_KEY]
                    await db.run("UPDATE projects SET defaults_json=? WHERE slug=?",
                                 (json.dumps({"three_stage": False}), "demo"))
                    await db.set_config("suggestions_enabled", "0")
                    conversation_id = await db.create_conversation(
                        project_slug="demo", requested_by="owner")
                    r = await client.post("/experts/run", json={
                        "target": "demo", "user": "lee la skill probe",
                        "source": "test", "author": "pytest",
                        "conversation": conversation_id,
                    })
                    self.assertEqual(r.status, 202, await r.text())
                    chat_id = (await r.json())["id"]
                    chat = await _wait_for_chat(db, chat_id)
                    history = await db.run(
                        "SELECT messages_json FROM conversations WHERE id=?",
                        (conversation_id,))
                    result = await client.get(f"/chats/{chat_id}")
                    self.assertEqual(result.status, 200)
                    http_chat = await result.json()
                    self.assertEqual(http_chat["status"], "ok")
                    self.assertIn("Leí texto local", Path(
                        http_chat["md_path"]).read_text(encoding="utf-8"))

        events = json.loads(chat["progress_events"])
        self.assertTrue(any(e.get("phase") == "tool_call"
                            and e.get("tool") == "read_skill" for e in events),
                        f"sin evento de progreso read_skill: {events}")
        messages = json.loads(history[0]["messages_json"])
        parts = [part for message in messages for part in message.get("parts", [])]
        call = next(i for i, part in enumerate(parts)
                    if part.get("part_kind") == "tool-call"
                    and part.get("tool_name") == "read_skill")
        result = next(i for i, part in enumerate(parts)
                      if part.get("part_kind") == "tool-return"
                      and part.get("tool_name") == "read_skill")
        self.assertLess(call, result, "el retorno no sigue a la tool_call")
        self.assertIn("texto local", parts[result]["content"])
        self.assertEqual(chat["status"], "ok")
        send.assert_not_awaited()


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
