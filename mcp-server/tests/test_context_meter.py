"""Medidor de contexto + compactar sin cerrar (2026-07-22).

Cover:
  - experts.context_usage: agrupa por run_id (mide el ÚLTIMO run), base
    vs peak, casos sin usage / historial corrupto
  - experts.format_context_note: solo avisa pasado el warn
  - memory.build_compacted_history: el JSON que sale lo puede replayar
    pydantic-ai (si no, el hilo compactado queda muerto)
  - server: POST /conversations/{id}/compact reemplaza el historial

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_context_meter.py -q
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from pydantic_ai.messages import ModelMessagesTypeAdapter

from relay import config, memory
from relay.db import Database
from relay.experts import (context_usage, context_usage_db,
                           format_context_note)

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR",
             "FOURBIS_COMPACTOR_MODEL", "FOURBIS_MODEL")


def _resp(tokens: int, run_id: str | None) -> dict:
    return {"kind": "response", "run_id": run_id,
            "parts": [{"part_kind": "text", "content": "ok"}],
            "usage": {"input_tokens": tokens, "output_tokens": 10}}


def _req(text: str = "hola") -> dict:
    return {"kind": "request",
            "parts": [{"part_kind": "user-prompt", "content": text}]}


class TestContextUsage(unittest.TestCase):
    """La ventana se fija por env: acá se prueba la aritmética del
    medidor, no el default de `config.model_context_tokens()` (que
    cambia cuando cambia el modelo — bajó a 120k el 2026-08-13)."""

    def setUp(self) -> None:
        self._env = os.environ.get("FOURBIS_MODEL_CONTEXT_TOKENS")
        os.environ["FOURBIS_MODEL_CONTEXT_TOKENS"] = "200000"
        config.set_runtime_config({"FOURBIS_MODEL_CONTEXT_TOKENS": "200000"})

    def tearDown(self) -> None:
        config.set_runtime_config({})
        if self._env is None:
            os.environ.pop("FOURBIS_MODEL_CONTEXT_TOKENS", None)
        else:
            os.environ["FOURBIS_MODEL_CONTEXT_TOKENS"] = self._env

    def test_mide_el_ultimo_run(self) -> None:
        # Run viejo grande + run nuevo: el medidor reporta el nuevo.
        raw = json.dumps([
            _req(), _resp(90_000, "run-1"), _resp(95_000, "run-1"),
            _req(), _resp(30_000, "run-2"), _resp(70_000, "run-2"),
            _resp(50_000, "run-2"),
        ])
        u = context_usage(raw)
        assert u is not None
        self.assertEqual(u["base_tokens"], 30_000)   # primer request del run
        self.assertEqual(u["peak_tokens"], 70_000)   # el mayor, no el último
        self.assertEqual(u["limit"], 200_000)
        self.assertEqual(u["pct"], 15)
        self.assertEqual(u["peak_pct"], 35)

    def test_sin_run_id_todo_es_un_run(self) -> None:
        raw = json.dumps([_req(), _resp(10_000, None), _resp(20_000, None)])
        u = context_usage(raw)
        assert u is not None
        self.assertEqual((u["base_tokens"], u["peak_tokens"]), (10_000, 20_000))

    def test_sin_usage_ni_json_valido(self) -> None:
        self.assertIsNone(context_usage(""))
        self.assertIsNone(context_usage("{no json"))
        self.assertIsNone(context_usage(json.dumps([_req()])))
        # Provider que no reporta tokens → no inventamos un número.
        self.assertIsNone(context_usage(json.dumps(
            [{"kind": "response", "parts": [], "usage": {"input_tokens": 0}}])))

    def test_hot_lo_dispara_el_pico(self) -> None:
        # base baja pero el run casi toca la pared → igual hay que avisar
        u = context_usage(json.dumps(
            [_req(), _resp(40_000, "r"), _resp(150_000, "r")]))
        assert u is not None
        self.assertEqual((u["pct"], u["peak_pct"]), (20, 75))
        self.assertTrue(u["hot"])
        note = format_context_note(u)
        self.assertIn("150k/200k (75%)", note)
        self.assertIn("compactar", note)

    def test_nota_solo_pasado_el_warn(self) -> None:
        self.assertEqual(format_context_note(None), "")
        cold = context_usage(json.dumps([_req(), _resp(20_000, "r")]))
        assert cold is not None
        self.assertFalse(cold["hot"])
        self.assertEqual(format_context_note(cold), "")


class TestVentanaPorModelo(unittest.IsolatedAsyncioTestCase):
    """La ventana sale de la ficha del modelo, no de un fijo global.

    2026-08-31: el medidor dividía SIEMPRE por 120.000 —un número que no
    es la ventana de ningún modelo del catálogo— y no decía que era una
    estimación. Con requests reales de 136.111 tokens, el badge llegaba
    a mostrar 113% de "la ventana".
    """

    def setUp(self) -> None:
        self._env = os.environ.get("FOURBIS_MODEL_CONTEXT_TOKENS")
        os.environ["FOURBIS_MODEL_CONTEXT_TOKENS"] = "120000"
        config.set_runtime_config({"FOURBIS_MODEL_CONTEXT_TOKENS": "120000"})

    def tearDown(self) -> None:
        config.set_runtime_config({})
        if self._env is None:
            os.environ.pop("FOURBIS_MODEL_CONTEXT_TOKENS", None)
        else:
            os.environ["FOURBIS_MODEL_CONTEXT_TOKENS"] = self._env

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(path=Path(self._tmp.name) / "m.db")
        await self.db.init_schema()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _hist(tokens: int, model: str) -> str:
        r = _resp(tokens, "r")
        r["model_name"] = model
        return json.dumps([_req(), r])

    async def test_usa_la_ventana_del_catalogo(self) -> None:
        # El seed pone MiniMax-M3 en 200k con aviso medido a los 92k.
        u = await context_usage_db(self.db, self._hist(100_000, "MiniMax-M3"))
        assert u is not None
        self.assertEqual(u["limit"], 200_000)
        self.assertEqual(u["peak_pct"], 50)
        self.assertTrue(u["limit_medido"])
        # El aviso lo dispara el umbral MEDIDO (92k), no el 80% de la
        # ventana nominal: a 100k el modelo ya se degrada aunque le
        # sobre la mitad del papel.
        self.assertEqual(u["warn_tokens"], 92_000)
        self.assertTrue(u["hot"])

    async def test_modelo_desconocido_cae_al_default_y_lo_dice(self) -> None:
        u = await context_usage_db(self.db, self._hist(60_000, "modelo-raro"))
        assert u is not None
        self.assertEqual(u["limit"], 120_000)
        self.assertFalse(u["limit_medido"])

    async def test_sin_historial_no_rompe(self) -> None:
        self.assertIsNone(await context_usage_db(self.db, ""))


class TestCompactedHistory(unittest.TestCase):
    def test_replayable_por_pydantic_ai(self) -> None:
        raw = memory.build_compacted_history(
            "Se decidió usar SQLite.", facts=["El relay corre en 8413"])
        msgs = ModelMessagesTypeAdapter.validate_json(raw)  # no revienta
        self.assertEqual(len(msgs), 2)
        self.assertIn("Se decidió usar SQLite.", raw)
        self.assertIn("8413", raw)
        # Y el hilo compactado ya no tiene contexto medible (usage limpio).
        self.assertIsNone(context_usage(raw))


class TestCompactEndpoint(unittest.IsolatedAsyncioTestCase):
    """POST /conversations/{id}/compact — el hilo sigue abierto y el
    historial queda reemplazado por el resumen (compactador = TestModel)."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._env_backup = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        os.environ["FOURBIS_COMPACTOR_MODEL"] = "test"
        config.set_runtime_config({"FOURBIS_MODEL": "test",
                                   "FOURBIS_COMPACTOR_MODEL": "test"})
        self.db = Database()
        await self.db.init_schema()
        await self.db.set_config("FOURBIS_MODEL", "test")
        await self.db.set_config("FOURBIS_COMPACTOR_MODEL", "test")
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name,
            "defaults_json": {"model": "test"}})

    async def asyncTearDown(self) -> None:
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    async def test_compact_reemplaza_historial_sin_cerrar(self) -> None:
        from relay.server import create_app
        conv_id = await self.db.create_conversation(project_slug="demo")
        # historial gordo: es lo que el compactador tiene que achicar
        raw = json.dumps([_req("arregla el login " + "x" * 50_000),
                          _resp(120_000, "run-1")])
        await self.db.save_conversation_messages(conv_id, raw)

        app = create_app()
        async with TestClient(TestServer(app)) as client:
            # el medidor viaja en el GET (lo que pinta la UI)
            r = await client.get(f"/conversations/{conv_id}")
            self.assertEqual((await r.json())["context"]["base_tokens"], 120_000)

            r = await client.post(f"/conversations/{conv_id}/compact")
            self.assertEqual(r.status, 200)
            body = await r.json()
            self.assertEqual(body["status"], "open")
            self.assertEqual(body["context_before"]["base_tokens"], 120_000)
            self.assertLess(body["chars_after"], body["chars_before"])

            # sigue abierta, con historial nuevo y sin summary de cierre
            conv = await self.db.get_conversation(conv_id)
            self.assertEqual(conv["status"], "open")
            self.assertNotEqual(conv["messages_json"], raw)
            self.assertIn("Resumen del hilo", conv["messages_json"])
            self.assertFalse(conv["summary"])

            # compactar una cerrada no va (para eso está el close)
            await client.post(f"/conversations/{conv_id}/close")
            r = await client.post(f"/conversations/{conv_id}/compact")
            self.assertEqual(r.status, 409)
            r = await client.post("/conversations/no-existe/compact")
            self.assertEqual(r.status, 404)


if __name__ == "__main__":
    unittest.main()
