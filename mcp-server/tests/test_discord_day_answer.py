"""Tests Iter 9.8 — endpoint /discord/day-answer (canal Discord para checkpoints).

Cover:
  - 200 con body válido → guarda respuesta, idempotente en DB
  - 400 si falta question_id
  - 400 si choice y free_text ambos vacíos
  - 404 si question_id no existe
  - 409 si la pregunta ya está respondida (gana la primera)

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_discord_day_answer.py -q
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from relay.db import Database

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR")


class DiscordDayAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        self.db = Database()
        await self.db.init_schema()
        # FK target para night_questions.
        await self.db.run(
            "INSERT INTO night_runs (id, project_slug, started_at, "
            "deadline_at) VALUES (?,?,?,?)",
            ("run_da", "demo", "2026-07-18T00:00:00Z",
             "2026-07-19T07:00:00-04:00"))
        await self.db.create_night_question(
            "q_aaaa1111", "run_da", "phase1",
            json.dumps({"title": "Fase 1 falló", "options": []}))

        from relay.server import create_app
        self.app = create_app()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp.cleanup()

    async def _post(self, body: dict) -> tuple[int, dict]:
        r = await self.client.post("/discord/day-answer", json=body)
        return r.status, await r.json()

    async def test_200_with_choice(self) -> None:
        status, body = await self._post({
            "discord_user_id": "1234567890",
            "run_id": "run_da",
            "question_id": "q_aaaa1111",
            "choice": "B",
            "free_text": None,
        })
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("q_id"), "q_aaaa1111")
        self.assertEqual(body["answer"]["choice"], "B")
        # Verifica que quedó en la DB (el orchestrator lo lee acá en su poll).
        ans = await self.db.get_night_question_answer("q_aaaa1111")
        self.assertEqual(ans, {"choice": "B", "free_text": None})

    async def test_200_with_free_text(self) -> None:
        status, body = await self._post({
            "discord_user_id": "1234567890",
            "run_id": "run_da",
            "question_id": "q_aaaa1111",
            "choice": None,
            "free_text": "Prueba con M2.5 por favor",
        })
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        ans = await self.db.get_night_question_answer("q_aaaa1111")
        self.assertEqual(ans["free_text"], "Prueba con M2.5 por favor")

    async def test_400_missing_question_id(self) -> None:
        status, body = await self._post({
            "discord_user_id": "x",
            "run_id": "run_da",
            "question_id": "",
            "choice": "A",
            "free_text": None,
        })
        self.assertEqual(status, 400)
        self.assertIn("question_id", body["error"])

    async def test_400_neither_choice_nor_free_text(self) -> None:
        status, body = await self._post({
            "discord_user_id": "x",
            "run_id": "run_da",
            "question_id": "q_aaaa1111",
            "choice": None,
            "free_text": None,
        })
        self.assertEqual(status, 400)
        self.assertIn("choice o free_text", body["error"])

    async def test_400_both_choice_and_free_text_empty_string(self) -> None:
        # Strings vacíos cuentan como "no choice, no free_text".
        status, body = await self._post({
            "discord_user_id": "x",
            "run_id": "run_da",
            "question_id": "q_aaaa1111",
            "choice": "",
            "free_text": "",
        })
        self.assertEqual(status, 400)

    async def test_404_unknown_question(self) -> None:
        status, body = await self._post({
            "discord_user_id": "x",
            "run_id": "run_da",
            "question_id": "q_does_not_exist",
            "choice": "A",
            "free_text": None,
        })
        self.assertEqual(status, 404)
        self.assertIn("desconocida", body["error"])

    async def test_409_already_answered(self) -> None:
        # Primera respuesta gana.
        s1, b1 = await self._post({
            "discord_user_id": "x", "run_id": "run_da",
            "question_id": "q_aaaa1111", "choice": "A", "free_text": None,
        })
        self.assertEqual(s1, 200)
        # Segunda respuesta (otro user / retry del bot) → 409.
        s2, b2 = await self._post({
            "discord_user_id": "y", "run_id": "run_da",
            "question_id": "q_aaaa1111", "choice": "B", "free_text": None,
        })
        self.assertEqual(s2, 409)
        self.assertIn("ya respondida", b2["error"])
        self.assertIn("answered_at", b2)
        # La primera gana en la DB.
        ans = await self.db.get_night_question_answer("q_aaaa1111")
        self.assertEqual(ans["choice"], "A")

    async def test_400_invalid_json_body(self) -> None:
        r = await self.client.post(
            "/discord/day-answer",
            data="not json",
            headers={"Content-Type": "application/json"})
        self.assertEqual(r.status, 400)
        body = await r.json()
        self.assertIn("json", body["error"])
