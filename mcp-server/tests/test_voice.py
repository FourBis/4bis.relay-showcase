"""Tests de voice input (Track D / F5 — docs/VOICE_INPUT.md).

El ASR de MiniMax se mockea en `voice.transcribe_minimax` (único
punto de red del módulo). Storage en dirs temporales vía env vars.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_voice.py -q
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
import aiohttp.web
from aiohttp.test_utils import TestClient, TestServer

from relay import voice
from relay import config as relay_config
from relay.db import Database
from relay.server import create_app

_ENV_KEYS = ("FOURBIS_DB_PATH", "VOICE_AUDIO_DIR", "VOICE_TRANSCRIPTS_DIR",
             "VOICE_MAX_AUDIO_BYTES")


def _form(audio: bytes | None = b"fake-opus-bytes",
          filename: str = "audio.ogg", **overrides) -> aiohttp.FormData:
    fields = {"author": "usuario-demo-a", "mode": "cli",
              "discord_channel": "general-voice", "duration_s": "4.2"}
    fields.update({k: v for k, v in overrides.items() if v is not None})
    for k in overrides:
        if overrides[k] is None:
            fields.pop(k, None)
    form = aiohttp.FormData()
    if audio is not None:
        form.add_field("audio", audio, filename=filename,
                       content_type="application/octet-stream")
    for k, v in fields.items():
        # content_type fuerza multipart aun sin el file field (si no,
        # FormData degrada a urlencoded y el handler devuelve otro 400)
        form.add_field(k, v, content_type="text/plain")
    return form


async def _fake_stt(audio_path: Path) -> dict:
    return {"provider": "minimax", "model": "minimax-asr",
            "language": "es-419", "chunks": 1, "tokens_in": 12,
            "duration_ms_stt": 5, "text": "hola esto es una prueba"}


class TestVoiceEndpoints(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._env_backup = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ["FOURBIS_DB_PATH"] = str(base / "relay.db")
        os.environ["VOICE_AUDIO_DIR"] = str(base / "audio")
        os.environ["VOICE_TRANSCRIPTS_DIR"] = str(base / "transcripts")
        os.environ.pop("VOICE_MAX_AUDIO_BYTES", None)
        self._db = Database()
        await self._db.init_schema()
        await self._db.set_config("VOICE_AUDIO_DIR", str(base / "audio"))
        await self._db.set_config("VOICE_TRANSCRIPTS_DIR", str(base / "transcripts"))
        relay_config.set_runtime_config(await self._db.all_config())

    async def asyncTearDown(self) -> None:
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        relay_config.set_runtime_config({})
        self._tmp.cleanup()

    async def test_transcribe_happy_path_and_get(self) -> None:
        with patch.object(voice, "transcribe_minimax", _fake_stt):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.post("/voice/transcribe", data=_form())
                self.assertEqual(r.status, 201)
                body = await r.json()
                trx_id = body["id"]
                self.assertTrue(trx_id.startswith("trx_"))
                self.assertEqual(body["transcript"],
                                 "hola esto es una prueba")
                self.assertEqual(body["stt"]["provider"], "minimax")
                self.assertNotIn("text", body["stt"])

                # audio persistido con la extensión original
                audio = Path(os.environ["VOICE_AUDIO_DIR"]) / f"{trx_id}.ogg"
                self.assertTrue(audio.exists())

                # GET del transcript completo
                r = await client.get(f"/voice/transcripts/{trx_id}")
                self.assertEqual(r.status, 200)
                rec = await r.json()
                self.assertEqual(rec["transcript"],
                                 "hola esto es una prueba")
                self.assertEqual(rec["mode"], "cli")
                self.assertEqual(rec["participants"], ["usuario-demo-a"])
                self.assertIsNone(rec["processed"])

                # listado
                r = await client.get("/voice/transcripts")
                items = (await r.json())["transcripts"]
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["id"], trx_id)

    async def test_missing_fields_400(self) -> None:
        with patch.object(voice, "transcribe_minimax", _fake_stt):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.post("/voice/transcribe",
                                      data=_form(author=None))
                self.assertEqual(r.status, 400)
                self.assertIn("author", (await r.json())["error"])

                r = await client.post("/voice/transcribe",
                                      data=_form(audio=None))
                self.assertEqual(r.status, 400)
                self.assertIn("audio", (await r.json())["error"])

                r = await client.post("/voice/transcribe",
                                      data=_form(mode="podcast"))
                self.assertEqual(r.status, 400)

                r = await client.post(
                    "/voice/transcribe", data=_form(filename="nota.txt"))
                self.assertEqual(r.status, 400)
                self.assertIn("formato", (await r.json())["error"])

    async def test_oversize_413(self) -> None:
        await self._db.set_config("VOICE_MAX_AUDIO_BYTES", "100")
        relay_config.set_runtime_config(await self._db.all_config())
        with patch.object(voice, "transcribe_minimax", _fake_stt):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.post("/voice/transcribe",
                                      data=_form(audio=b"x" * 200))
                self.assertEqual(r.status, 413)

    async def test_stt_error_502_and_timeout_504(self) -> None:
        async def boom(audio_path):
            raise RuntimeError("ASR 500: caput")

        async def slow(audio_path):
            raise asyncio.TimeoutError()

        with patch.object(voice, "transcribe_minimax", boom):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.post("/voice/transcribe", data=_form())
                self.assertEqual(r.status, 502)
                self.assertEqual((await r.json())["error"], "STT failed")

        with patch.object(voice, "transcribe_minimax", slow):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.post("/voice/transcribe", data=_form())
                self.assertEqual(r.status, 504)

    async def test_get_unknown_404(self) -> None:
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.get("/voice/transcripts/trx_00000000")
            self.assertEqual(r.status, 404)
            # path traversal defensivo
            r = await client.get("/voice/transcripts/..%2Fetc")
            self.assertEqual(r.status, 404)

    async def test_process_happy_path_and_idempotent(self) -> None:
        """POST /voice/transcripts/{id}/process: corre el experto, persiste
        processed={summary,model,ts} y el segundo call NO re-corre el
        experto (pitfall 5 del spec — idempotencia)."""
        calls = {"n": 0}

        async def fake_expert(project, user, **kw):
            calls["n"] += 1
            assert "hola esto es una prueba" in user
            return {"content": "Resumen: era una prueba.", "model": "test:m"}

        from relay import experts, voice_routes
        with patch.object(voice, "transcribe_minimax", _fake_stt), \
             patch.object(experts, "run_expert", fake_expert), \
             patch.dict(voice_routes._voice_process_locks, clear=True):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.post("/voice/transcribe", data=_form())
                trx_id = (await r.json())["id"]

                r = await client.post(f"/voice/transcripts/{trx_id}/process",
                                      json={"context": "es una prueba"})
                self.assertEqual(r.status, 200)
                body = await r.json()
                self.assertEqual(body["summary"], "Resumen: era una prueba.")
                self.assertFalse(body["already_processed"])
                self.assertEqual(calls["n"], 1)

                # el JSONL quedó con processed persistido
                r = await client.get(f"/voice/transcripts/{trx_id}")
                rec = await r.json()
                self.assertEqual(rec["processed"]["summary"],
                                 "Resumen: era una prueba.")
                self.assertEqual(rec["processed"]["model"], "test:m")

                # segundo /process → already_processed, sin re-correr
                r = await client.post(f"/voice/transcripts/{trx_id}/process")
                self.assertEqual(r.status, 200)
                body = await r.json()
                self.assertTrue(body["already_processed"])
                self.assertEqual(body["summary"], "Resumen: era una prueba.")
                self.assertEqual(calls["n"], 1, "el experto NO debe re-correr")

    async def test_process_unknown_404_and_expert_error_502(self) -> None:
        async def boom_expert(project, user, **kw):
            raise RuntimeError("modelo explotó")

        from relay import experts, voice_routes
        with patch.object(voice, "transcribe_minimax", _fake_stt), \
             patch.object(experts, "run_expert", boom_expert), \
             patch.dict(voice_routes._voice_process_locks, clear=True):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.post(
                    "/voice/transcripts/trx_00000000/process")
                self.assertEqual(r.status, 404)

                r = await client.post("/voice/transcribe", data=_form())
                trx_id = (await r.json())["id"]
                r = await client.post(f"/voice/transcripts/{trx_id}/process")
                self.assertEqual(r.status, 502)
                # el transcript NO quedó marcado como procesado
                r = await client.get(f"/voice/transcripts/{trx_id}")
                self.assertIsNone((await r.json())["processed"])


class TestTranscribeMinimaxProvider(unittest.IsolatedAsyncioTestCase):
    """transcribe_minimax() es genérico (shape multipart OpenAI): el
    nombre es histórico, cualquier proveedor con este shape enchufa por
    config. Acá probamos la función real (no mockeada) contra un server
    local que imita el shape de Groq — sin pegarle a la red real."""

    async def asyncSetUp(self) -> None:
        self._env_backup = {
            k: os.environ.get(k) for k in (
                "FOURBIS_DB_PATH",
                "MINIMAX_STT_URL", "MINIMAX_API_KEY", "VOICE_STT_API_KEY",
                "MINIMAX_STT_MODEL", "MINIMAX_STT_LANGUAGE")}
        self._seen_auth: list[str] = []

        async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
            self._seen_auth.append(request.headers.get("Authorization", ""))
            await request.post()  # consume el multipart
            return aiohttp.web.json_response(
                {"text": "hola desde el fake",
                 "usage": {"total_tokens": 7}})

        app = aiohttp.web.Application()
        app.router.add_post("/v1/audio/transcriptions", handler)
        self._server = TestServer(app)
        await self._server.start_server()
        base = f"http://{self._server.host}:{self._server.port}"
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["FOURBIS_DB_PATH"] = str(Path(self._tmp.name) / "relay.db")
        self._db = Database()
        await self._db.init_schema()
        await self._db.set_config("MINIMAX_STT_URL", f"{base}/v1/audio/transcriptions")
        await self._db.set_config("secret:VOICE_STT_API_KEY", "gsk_test_key")
        await self._db.set_config("secret:MINIMAX_API_KEY", "sk-should-not-be-used")
        await self._db.set_config("MINIMAX_STT_MODEL", "whisper-large-v3-turbo")
        await self._db.set_config("MINIMAX_STT_LANGUAGE", "es")
        relay_config.set_runtime_config(await self._db.all_config())

    async def asyncTearDown(self) -> None:
        await self._server.close()
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        relay_config.set_runtime_config({})
        self._tmp.cleanup()

    async def test_uses_voice_stt_api_key_and_derives_provider(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(b"RIFF....WAVEfmt ")
            path = Path(f.name)
        try:
            result = await voice.transcribe_minimax(path)
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(result["text"], "hola desde el fake")
        self.assertEqual(result["model"], "whisper-large-v3-turbo")
        self.assertEqual(result["tokens_in"], 7)
        # provider viene del host de MINIMAX_STT_URL, no hardcodeado
        self.assertEqual(result["provider"], self._server.host)
        # VOICE_STT_API_KEY gana sobre MINIMAX_API_KEY
        self.assertEqual(self._seen_auth, ["Bearer gsk_test_key"])


if __name__ == "__main__":
    unittest.main()
