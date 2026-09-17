"""Tests 2026-07-26 — sugerencias de continuación (botones de "próximo paso").

Cover:
  - parse_suggestions: viñetas/numeración/comillas, dedupe, tope de 3,
    recorte a 80 chars en el último espacio (el label ES el prompt).
  - _suggest_followups: apagado por config, y el best-effort (si el
    sugeridor explota, la respuesta sale igual sin botones).
  - DB: set_chat_suggestions persiste y sale por get_chat.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from relay import server
from relay.db import Database
from relay.experts import (
    SUGGESTION_MAX_CHARS,
    SUGGESTIONS_MAX,
    parse_suggestions,
)


class TestParseSuggestions(unittest.TestCase):
    def test_limpia_decoracion(self):
        raw = ('- Muestra el diff\n'
               '2) Ejecuta los tests\n'
               '• "Abre un PR a develop"\n')
        self.assertEqual(
            parse_suggestions(raw),
            ["Muestra el diff", "Ejecuta los tests", "Abre un PR a develop"])

    def test_dedupe_y_tope(self):
        raw = "Ejecuta los tests\nejecuta los tests\nA\nB\nC\nD"
        out = parse_suggestions(raw)
        self.assertEqual(len(out), SUGGESTIONS_MAX)
        self.assertEqual(out, ["Ejecuta los tests", "A", "B"])

    def test_recorta_en_el_ultimo_espacio(self):
        largo = ("Agrega un test de integración que cubra el parser de "
                 "sugerencias y el endpoint nuevo del relay")
        out = parse_suggestions(largo)
        self.assertEqual(len(out), 1)
        self.assertLessEqual(len(out[0]), SUGGESTION_MAX_CHARS)
        self.assertTrue(out[0].endswith("…"))
        # Corta en palabra completa: nada de "sugerenci…"
        self.assertTrue(largo.startswith(out[0][:-1]))
        self.assertFalse(out[0][-2].isspace())

    def test_vacio(self):
        self.assertEqual(parse_suggestions(""), [])
        self.assertEqual(parse_suggestions("\n  \n"), [])


class TestSuggestFollowups(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "relay.db")
        await self.db.init_schema()

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_apagado_por_config(self):
        await self.db.set_config("suggestions_enabled", "0")
        with patch("relay.experts.suggest_followups") as m:
            out = await server._suggest_followups(
                self.db, user="hola", answer="chau")
        self.assertEqual(out, [])
        m.assert_not_called()

    async def test_si_el_sugeridor_explota_no_rompe(self):
        async def boom(**_kw):
            raise RuntimeError("provider caído")

        with patch("relay.experts.suggest_followups", boom):
            out = await server._suggest_followups(
                self.db, user="hola", answer="chau")
        self.assertEqual(out, [])

    async def test_timeout_no_rompe(self):
        async def lento(**_kw):
            await asyncio.sleep(5)
            return ["nunca"]

        with patch("relay.experts.suggest_followups", lento), \
                patch.object(server, "SUGGEST_TIMEOUT_S", 0.05):
            out = await server._suggest_followups(
                self.db, user="hola", answer="chau")
        self.assertEqual(out, [])

    async def test_persiste_en_el_chat(self):
        chat_id = await self.db.create_chat(
            project_slug="demo", source="ui", author="test", target="demo")
        await self.db.set_chat_suggestions(
            chat_id, json.dumps(["Muestra el diff"], ensure_ascii=False))
        chat = await self.db.get_chat(chat_id)
        self.assertEqual(json.loads(chat["suggestions"]), ["Muestra el diff"])


class TestSuggestionsEndToEnd(unittest.IsolatedAsyncioTestCase):
    """El seam relay↔bot: las sugerencias tienen que salir en el
    metadata del /notify Y quedar en la fila del chat. Si una de las dos
    se cae, los botones no aparecen o el click no resuelve el texto."""

    _ENV = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR",
            "FOURBIS_COMPACTOR_MODEL", "FOURBIS_MODEL")

    async def asyncSetUp(self):
        import os
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._backup = {k: os.environ.get(k) for k in self._ENV}
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        os.environ["FOURBIS_MODEL"] = "test"
        db = Database()
        await db.init_schema()
        await db.set_config("FOURBIS_MODEL", "test")
        await db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name,
            "defaults_json": {"model": "test"},
        })

    async def asyncTearDown(self):
        import os
        for k, v in self._backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    async def test_notify_metadata_y_fila_del_chat(self):
        from aiohttp.test_utils import TestClient, TestServer

        from relay.notify import NotifyClient
        from relay.server import create_app

        notifications: list[dict] = []

        async def fake_send(self_, agent_id, kind, message, metadata=None):
            notifications.append({"kind": kind, "metadata": metadata or {}})
            return True

        async def fake_suggest(**_kw):
            return ["Muestra el diff", "Ejecuta los tests"]

        with patch.object(NotifyClient, "send", fake_send), \
                patch("relay.experts.suggest_followups", fake_suggest):
            async with TestClient(TestServer(create_app())) as client:
                r = await client.post("/experts/run", json={
                    "target": "demo", "user": "hola", "source": "test"})
                chat_id = (await r.json())["id"]
                for _ in range(200):
                    if notifications:
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(notifications, "no llegó el notify")
                self.assertEqual(
                    notifications[0]["metadata"].get("suggestions"),
                    ["Muestra el diff", "Ejecuta los tests"])

                chat = await (await client.get(f"/chats/{chat_id}")).json()
                self.assertEqual(
                    json.loads(chat["suggestions"]),
                    ["Muestra el diff", "Ejecuta los tests"])


if __name__ == "__main__":
    unittest.main()
