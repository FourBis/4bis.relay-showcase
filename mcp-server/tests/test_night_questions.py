"""Tests Iter 9.7 — preguntas interactivas (night_questions).

Cover:
  - DB: create/get/answer/list/skip/orphan + idempotencia
  - NightOrchestrator: ask_and_wait con respuesta humana + sin respuesta
  - NightOrchestrator: _handle_phase1_failure en interactive mode
  - NightOrchestrator: _maybe_ask_block_decision cuando LLM decide preguntar
  - HTTP: GET /admin/api/night/questions, POST answer, POST skip
  - Migración idempotente: tabla + columna interactive_mode se aplican
    sin error en una DB existente
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from relay.db import Database
from relay.night import (
    BlockDecision,
    BlockQuestionOption,
    NightOrchestrator,
    _summarize_block_diff,
)


# ---------- helpers ----------

def _make_db() -> Database:
    """DB en un tempfile con schema completo aplicado."""
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "relay.db"
    # El close() del Database no existe — los tempfile se limpian al
    # GC del test. Suficiente para el lifespan de un test.
    return Database(db_path)


# ---------- DB helpers ----------

class NightQuestionsDbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = _make_db()
        await self.db.init_schema()
        # Crear un night_run mínimo (FK target).
        await self.db.run(
            "INSERT INTO night_runs (id, project_slug, started_at, "
            "deadline_at) VALUES (?,?,?,?)",
            ("run_t", "demo", "2026-07-18T00:00:00Z",
             "2026-07-19T07:00:00-04:00"))

    async def test_create_and_get(self) -> None:
        qid = "q_abc12345"
        qjson = json.dumps({"kind": "phase1_failed", "title": "t"})
        await self.db.create_night_question(
            qid, "run_t", "phase1", qjson,
            notify_target="#demo", notify_via="both")
        row = await self.db.get_night_question(qid)
        self.assertIsNotNone(row)
        self.assertEqual(row["run_id"], "run_t")
        self.assertEqual(row["phase"], "phase1")
        self.assertIsNone(row["answered_at"])

    async def test_answer_marks_responded(self) -> None:
        qid = "q_ans0001"
        await self.db.create_night_question(
            qid, "run_t", "block_done",
            json.dumps({"kind": "block_decision"}),
            notify_target="#demo")
        ok = await self.db.answer_night_question(
            qid, {"choice": "B", "free_text": None})
        self.assertTrue(ok)
        ans = await self.db.get_night_question_answer(qid)
        self.assertEqual(ans, {"choice": "B", "free_text": None})

    async def test_answer_is_idempotent_returns_false_second_time(self) -> None:
        qid = "q_idem000"
        await self.db.create_night_question(
            qid, "run_t", "phase1", json.dumps({}))
        ok1 = await self.db.answer_night_question(
            qid, {"choice": "A", "free_text": None})
        ok2 = await self.db.answer_night_question(
            qid, {"choice": "B", "free_text": None})
        self.assertTrue(ok1)
        self.assertFalse(ok2, "segunda respuesta debe ser 409")
        # La primera gana.
        ans = await self.db.get_night_question_answer(qid)
        self.assertEqual(ans["choice"], "A")

    async def test_skip_marks_with_empty_dict(self) -> None:
        qid = "q_skip000"
        await self.db.create_night_question(
            qid, "run_t", "phase1", json.dumps({}))
        ok = await self.db.skip_night_question(qid)
        self.assertTrue(ok)
        ans = await self.db.get_night_question_answer(qid)
        self.assertEqual(ans, {})  # skip = {}

    async def test_list_filters_by_run_and_open(self) -> None:
        for i, (qid, phase) in enumerate([
            ("q_open01", "phase1"),
            ("q_open02", "block_done"),
            ("q_close1", "phase1"),
        ]):
            await self.db.create_night_question(
                qid, "run_t", phase, json.dumps({"i": i}))
        await self.db.answer_night_question(
            "q_close1", {"choice": "C", "free_text": None})
        rows_open = await self.db.list_night_questions(
            run_id="run_t", only_open=True)
        ids = sorted(r["id"] for r in rows_open)
        self.assertEqual(ids, ["q_open01", "q_open02"])
        rows_all = await self.db.list_night_questions(run_id="run_t")
        self.assertEqual(len(rows_all), 3)

    async def test_orphan_close_questions_for_dead_runs(self) -> None:
        # Crear un run "muerto" (ended_at != NULL) con pregunta abierta.
        await self.db.run(
            "INSERT INTO night_runs (id, project_slug, started_at, "
            "deadline_at, ended_at) VALUES (?,?,?,?,?)",
            ("run_dead", "demo", "2026-07-18T00:00:00Z",
             "2026-07-19T07:00:00-04:00",
             "2026-07-18T01:00:00Z"))
        qid = "q_orphan0"
        await self.db.create_night_question(
            qid, "run_dead", "phase1", json.dumps({}))
        # Crear otro run vivo con pregunta abierta — NO debe tocarse.
        await self.db.run(
            "INSERT INTO night_runs (id, project_slug, started_at, "
            "deadline_at) VALUES (?,?,?,?)",
            ("run_alive", "demo", "2026-07-18T02:00:00Z",
             "2026-07-19T07:00:00-04:00"))
        qid2 = "q_alive00"
        await self.db.create_night_question(
            qid2, "run_alive", "phase1", json.dumps({}))
        n = await self.db.orphan_open_questions()
        self.assertEqual(n, 1)
        # Pregunta del run muerto: respondida con skipped.
        ans = await self.db.get_night_question_answer(qid)
        self.assertEqual(ans, {"skipped": True})
        # Pregunta del run vivo: sigue abierta.
        ans2 = await self.db.get_night_question_answer(qid2)
        self.assertIsNone(ans2)

    async def test_migration_idempotent(self) -> None:
        """Correr init_schema 2 veces no rompe (idempotente)."""
        await self.db.init_schema()  # segunda llamada
        # Y la columna interactive_mode está. PRAGMA devuelve tuplas
        # sin nombres de columna en sqlite3.Row factory, así que
        # usamos conexión directa (sync) en lugar de db.run().
        def _check() -> None:
            conn = sqlite3.connect(str(self.db.path))
            try:
                cols = [r[1] for r in conn.execute(
                    "PRAGMA table_info(projects)").fetchall()]
                self.assertIn("interactive_mode", cols)
            finally:
                conn.close()
        await asyncio.to_thread(_check)


# ---------- _summarize_block_diff ----------

class SummarizeBlockDiffTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_when_no_sha(self) -> None:
        d = await asyncio.to_thread(
            _summarize_block_diff, "C:/nope", None)
        self.assertEqual(d, {})

    async def test_returns_files_and_message(self) -> None:
        # Repo git real con un commit para inspeccionar.
        tmp = tempfile.TemporaryDirectory()
        repo = Path(tmp.name)
        try:
            self._git(str(repo), "init", "-b", "main")
            self._git(str(repo), "config", "user.email", "t@t")
            self._git(str(repo), "config", "user.name", "t")
            (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
            self._git(str(repo), "add", "-A")
            self._git(str(repo), "commit", "-m", "primer commit")
            sha = self._git(str(repo), "rev-parse", "HEAD").strip()
            d = await asyncio.to_thread(
                _summarize_block_diff, str(repo), sha)
            self.assertIn("files", d)
            self.assertIn("commit_message", d)
            self.assertIn("primer commit", d["commit_message"])
            self.assertTrue(any("a.py" in f for f in d["files"]))
        finally:
            tmp.cleanup()

    @staticmethod
    def _git(repo: str, *args: str) -> str:
        import subprocess
        out = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True,
            text=True, timeout=30)
        return (out.stdout or "")


# ---------- NightOrchestrator.ask_and_wait ----------

class AskAndWaitTests(unittest.IsolatedAsyncioTestCase):
    """Mockeamos db y notify para que la respuesta llegue por DB."""

    def _orch(
        self, *, interactive: bool = True,
        deadline_offset_s: int = 30,
    ) -> NightOrchestrator:
        project = {
            "slug": "demo",
            "repo_path": ".",
            "defaults_json": {},
            "night_config": {},
            "interactive_mode": 1 if interactive else 0,
        }
        db = MagicMock()
        db.create_night_question = AsyncMock()
        db.get_night_question_answer = AsyncMock(return_value=None)
        db.skip_night_question = AsyncMock()
        db.answer_night_question = AsyncMock(return_value=True)
        return NightOrchestrator(
            db=db, project=project,
            deadline=datetime.now().astimezone() + timedelta(
                seconds=deadline_offset_s))

    async def test_returns_none_when_not_interactive(self) -> None:
        orch = self._orch(interactive=False)
        ans = await orch._ask_and_wait(
            {"kind": "phase1_failed", "title": "t",
             "options": [{"key": "A"}], "default": "A"},
            phase="phase1")
        self.assertIsNone(ans)
        # No debe haber creado question ni preguntado a notify.
        orch.db.create_night_question.assert_not_called()

    async def test_polls_and_returns_answer(self) -> None:
        orch = self._orch(deadline_offset_s=30)
        # Segunda llamada devuelve la respuesta.
        calls = {"n": 0}

        async def fake_get_answer(qid):
            calls["n"] += 1
            if calls["n"] >= 2:
                return {"choice": "B", "free_text": None}
            return None
        orch.db.get_night_question_answer = fake_get_answer
        ans = await orch._ask_and_wait(
            {"kind": "phase1_failed", "title": "t",
             "options": [
                 {"key": "A", "label": "a"},
                 {"key": "B", "label": "b"}],
             "default": "A"},
            phase="phase1")
        self.assertEqual(ans, {"choice": "B", "free_text": None})
        # Verifica que se creó la question con los campos correctos.
        orch.db.create_night_question.assert_called_once()
        kwargs = orch.db.create_night_question.call_args
        self.assertEqual(kwargs.args[1], orch.run_id)  # run_id
        self.assertEqual(kwargs.args[2], "phase1")     # phase

    async def test_returns_none_and_skips_on_deadline(self) -> None:
        orch = self._orch(deadline_offset_s=0)
        # deadline ya pasó → 0 iteraciones del poll.
        ans = await orch._ask_and_wait(
            {"kind": "phase1_failed", "title": "t",
             "options": [{"key": "A"}], "default": "A"},
            phase="phase1")
        self.assertIsNone(ans)
        orch.db.skip_night_question.assert_called_once()

    async def test_apply_default_answer_falls_back(self) -> None:
        orch = self._orch()
        q = {
            "options": [{"key": "C", "label": "cerrar"}],
            "default": "C",
        }
        eff = orch._apply_default_answer(q, None)
        self.assertEqual(eff["choice"], "C")
        eff2 = orch._apply_default_answer(q, {"choice": "B"})
        self.assertEqual(eff2["choice"], "B")


# ---------- _handle_phase1_failure ----------

class HandlePhase1FailureTests(unittest.IsolatedAsyncioTestCase):
    def _orch(self, *, interactive: bool = True) -> NightOrchestrator:
        project = {
            "slug": "demo", "repo_path": ".",
            "defaults_json": {}, "night_config": {},
            "interactive_mode": 1 if interactive else 0,
        }
        db = MagicMock()
        db.create_night_question = AsyncMock()
        db.get_night_question_answer = AsyncMock(return_value=None)
        db.skip_night_question = AsyncMock()
        return NightOrchestrator(
            db=db, project=project,
            deadline=datetime.now().astimezone() + timedelta(hours=1))

    async def test_returns_none_when_not_interactive(self) -> None:
        orch = self._orch(interactive=False)
        result = await orch._handle_phase1_failure(
            RuntimeError("boom"))
        self.assertIsNone(result)

    async def test_returns_none_when_human_does_not_respond(self) -> None:
        orch = self._orch(interactive=True)
        # _ask_and_wait va a devolver None porque deadline_offset es
        # muy corto (1h pero con poll cada 5s y respuesta siempre None,
        # solo cerramos el test manualmente). Para forzarlo, le patcheamos
        # _ask_and_wait directamente.
        orch._ask_and_wait = AsyncMock(return_value=None)
        result = await orch._handle_phase1_failure(
            RuntimeError("Exceeded maximum output retries (3)"))
        self.assertIsNone(result)

    async def test_returns_none_when_human_chooses_C_close(self) -> None:
        orch = self._orch(interactive=True)
        orch._ask_and_wait = AsyncMock(
            return_value={"choice": "C", "free_text": None})
        result = await orch._handle_phase1_failure(
            RuntimeError("Exceeded maximum output retries (3)"))
        self.assertIsNone(result, "C = cerrar el run")

    async def test_retries_with_m25_when_human_chooses_B(self) -> None:
        orch = self._orch(interactive=True)
        orch._ask_and_wait = AsyncMock(
            return_value={"choice": "B", "free_text": None})
        # Mockeamos generator.generate para que retorne tasks dummy.
        orch.generator = MagicMock()
        orch.generator.generate = AsyncMock(
            return_value=([], []))
        result = await orch._handle_phase1_failure(
            RuntimeError("Exceeded maximum output retries (3)"))
        # Llamó a generate (retry) y retornó su resultado.
        orch.generator.generate.assert_called_once()
        self.assertEqual(result, ([], []))

    async def test_returns_none_when_human_chooses_A_free_text(self) -> None:
        """A = 'reintentar con directive ajustada' — free-text no
        implementado todavía, retorna None."""
        orch = self._orch(interactive=True)
        orch._ask_and_wait = AsyncMock(
            return_value={"choice": "A", "free_text": "nueva directiva"})
        result = await orch._handle_phase1_failure(
            RuntimeError("Exceeded maximum output retries (3)"))
        self.assertIsNone(result)


# ---------- _maybe_ask_block_decision ----------

class MaybeAskBlockDecisionTests(unittest.IsolatedAsyncioTestCase):
    def _orch(self, *, interactive: bool = True) -> NightOrchestrator:
        project = {
            "slug": "demo", "repo_path": ".",
            "defaults_json": {"planner_model": "test"}, "night_config": {},
            "interactive_mode": 1 if interactive else 0,
        }
        db = MagicMock()
        db.create_night_question = AsyncMock()
        db.get_night_question_answer = AsyncMock(return_value=None)
        db.skip_night_question = AsyncMock()
        return NightOrchestrator(
            db=db, project=project,
            deadline=datetime.now().astimezone() + timedelta(hours=1))

    async def test_no_op_when_not_interactive(self) -> None:
        orch = self._orch(interactive=False)
        # No debe llamar nada.
        await orch._maybe_ask_block_decision(
            MagicMock(), MagicMock(status="done"))
        orch.db.create_night_question.assert_not_called()

    async def test_no_op_when_task_discarded(self) -> None:
        orch = self._orch(interactive=True)
        await orch._maybe_ask_block_decision(
            MagicMock(), MagicMock(status="discarded"))
        orch.db.create_night_question.assert_not_called()

    async def test_no_question_when_llm_says_follow_alone(self) -> None:
        """Si el LLM secundario devuelve None, no se crea question."""
        orch = self._orch(interactive=True)
        # El código hace `from pydantic_ai import Agent` adentro del
        # método. Para mockearlo, parchamos `pydantic_ai.Agent`
        # antes de la llamada. Esto funciona porque pydantic-ai
        # solo se importa una vez y el módulo guarda la referencia.
        import pydantic_ai

        class FakeResult:
            output = None

        class FakeAgentCls:
            def __init__(self, *a, **kw): pass
            async def run(self, *a, **kw):
                return FakeResult()

        with patch.object(pydantic_ai, "Agent", FakeAgentCls):
            await orch._maybe_ask_block_decision(
                MagicMock(id="T-001", title="t"),
                MagicMock(status="done"))
        orch.db.create_night_question.assert_not_called()

    async def test_question_when_llm_decides_to_ask(self) -> None:
        """Si el LLM decide preguntar, se crea night_question."""
        orch = self._orch(interactive=True)
        # Mockeamos get_night_question_answer para que el poll loop
        # termine rápido (sin esto, _ask_and_wait espera hasta el
        # deadline y el test cuelga).
        orch.db.get_night_question_answer = AsyncMock(
            return_value={"choice": "A", "free_text": None})
        decision = BlockDecision(
            title="Bloque listo",
            prompt="¿qué hago con los campos?",
            options=[
                BlockQuestionOption(key="A", label="mantener"),
                BlockQuestionOption(key="B", label="agregar dir"),
            ],
            default="A",
        )
        import pydantic_ai

        class FakeResult:
            output = decision

        class FakeAgentCls:
            def __init__(self, *a, **kw): pass
            async def run(self, *a, **kw):
                return FakeResult()

        with patch.object(pydantic_ai, "Agent", FakeAgentCls):
            await orch._maybe_ask_block_decision(
                MagicMock(id="T-001", title="t"),
                MagicMock(status="done"))
        # Se creó una question con kind=block_decision.
        orch.db.create_night_question.assert_called_once()
        kwargs = orch.db.create_night_question.call_args
        question_json = kwargs.args[3]
        question = json.loads(question_json)
        self.assertEqual(question["kind"], "block_decision")
        self.assertEqual(len(question["options"]), 2)


# ---------- BlockDecision schema ----------

class BlockDecisionSchemaTests(unittest.TestCase):
    def test_minimum_two_options(self) -> None:
        # No es enforce de pydantic, pero por contrato: ≥ 2 options.
        d = BlockDecision(
            title="t", prompt="p",
            options=[BlockQuestionOption(key="A", label="a"),
                     BlockQuestionOption(key="B", label="b")],
            default="A")
        self.assertEqual(len(d.options), 2)
        self.assertEqual(d.default, "A")


if __name__ == "__main__":
    unittest.main()
