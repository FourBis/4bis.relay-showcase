"""Rutas HTTP del flujo de voz; proveedor y persistencia viven en ``voice``."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Optional

from aiohttp import web

from . import experts, voice
from .app_state import DB_KEY
from .db import Database

logger = logging.getLogger("relay.server")

# Dos /process simultáneos para el mismo transcript esperan y respetan la
# marca `processed` que persiste el primero.
_voice_process_locks: dict[str, asyncio.Lock] = {}


async def voice_transcribe(request: web.Request) -> web.Response:
    """POST /voice/transcribe — audio (multipart) → STT → transcript."""
    cap = voice.max_audio_bytes()
    try:
        reader = await request.multipart()
    except (ValueError, AssertionError):
        return web.json_response(
            {"error": "se espera multipart/form-data"}, status=400)

    fields: dict[str, str] = {}
    audio_bytes: Optional[bytes] = None
    audio_name = ""
    async for part in reader:
        if part.name == "audio":
            buf = bytearray()
            while True:
                chunk = await part.read_chunk(64 * 1024)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > cap:
                    return web.json_response(
                        {"error": f"audio > {cap} bytes"}, status=413)
            audio_bytes = bytes(buf)
            audio_name = part.filename or ""
        elif part.name:
            fields[part.name] = (await part.text()).strip()

    missing = [f for f in ("author", "mode", "discord_channel", "duration_s")
               if not fields.get(f)]
    if audio_bytes is None or not audio_bytes:
        missing.insert(0, "audio")
    if missing:
        return web.json_response(
            {"error": f"faltan campos requeridos: {', '.join(missing)}"},
            status=400)
    mode = fields["mode"]
    if mode not in ("vc", "cli"):
        return web.json_response(
            {"error": 'mode debe ser "vc" o "cli"'}, status=400)
    try:
        duration_s = float(fields["duration_s"])
    except ValueError:
        return web.json_response(
            {"error": "duration_s debe ser numérico"}, status=400)
    if not math.isfinite(duration_s) or duration_s < 0:
        return web.json_response(
            {"error": "duration_s debe ser un número finito >= 0"},
            status=400)
    if duration_s > 4 * 3600:
        return web.json_response(
            {"error": "duration_s demasiado grande (max 4h)"}, status=400)
    ext = Path(audio_name).suffix.lower()
    if ext not in voice.AUDIO_EXTS:
        return web.json_response(
            {"error": f"formato de audio no soportado: {ext or '(sin ext)'}"
                       f" — esperado {sorted(voice.AUDIO_EXTS)}"},
            status=400)

    trx_id = voice.new_trx_id(audio_bytes)
    adir = voice.audio_dir()
    await asyncio.to_thread(adir.mkdir, parents=True, exist_ok=True)
    audio_path = adir / f"{trx_id}{ext}"
    await asyncio.to_thread(audio_path.write_bytes, audio_bytes)

    try:
        stt = await voice.transcribe_minimax(audio_path)
    except asyncio.TimeoutError:
        return web.json_response(
            {"error": "STT timeout", "id": trx_id}, status=504)
    except RuntimeError as e:
        logger.warning("voice: ASR falló trx=%s: %s", trx_id, e)
        return web.json_response(
            {"error": "STT failed", "detail": str(e)[:300], "id": trx_id},
            status=502)

    transcript = stt.pop("text")
    participants = [p.strip() for p in
                    (fields.get("participants") or "").split(",") if p.strip()]
    record = {
        "id": trx_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "author": fields["author"],
        "discord_channel": fields["discord_channel"],
        "mode": mode,
        "audio": {"path": str(audio_path).replace("\\", "/"),
                  "duration_s": duration_s, "size": len(audio_bytes)},
        "stt": stt,
        "transcript": transcript,
        "participants": participants or [fields["author"]],
        "topic": fields.get("topic") or None,
        "related_project": fields.get("related_project") or None,
        "processed": None,
    }
    await voice.persist_transcript(record)
    logger.info("voice: transcript %s persistido (%s, %.0fs, %d chars)",
                trx_id, mode, duration_s, len(transcript))
    return web.json_response(
        {"id": trx_id, "transcript": transcript,
         "duration_s": duration_s, "stt": stt}, status=201)


async def voice_transcripts_list(request: web.Request) -> web.Response:
    """GET /voice/transcripts — índice liviano (debug / futura UI)."""
    limit = min(int(request.query.get("limit", "50")), 200)
    return web.json_response(
        {"transcripts": await voice.list_transcripts(limit)})


async def voice_transcripts_get(request: web.Request) -> web.Response:
    """GET /voice/transcripts/{id} — el JSONL completo (debug)."""
    rec = await voice.read_transcript(request.match_info["id"])
    if rec is None:
        return web.json_response({"error": "no existe"}, status=404)
    return web.json_response(rec)


async def voice_transcript_process(request: web.Request) -> web.Response:
    """Resume un transcript con el experto del proyecto asociado."""
    db: Database = request.app[DB_KEY]
    trx_id = request.match_info["id"]
    lock = _voice_process_locks.setdefault(trx_id, asyncio.Lock())
    async with lock:
        rec = await voice.read_transcript(trx_id)
        if rec is None:
            return web.json_response({"error": "no existe"}, status=404)
        prev = rec.get("processed")
        if prev:
            return web.json_response(
                {"id": trx_id, "summary": prev.get("summary", ""),
                 "model": prev.get("model"), "already_processed": True})
        transcript = (rec.get("transcript") or "").strip()
        if not transcript:
            return web.json_response(
                {"error": "transcript vacío: nada que procesar"}, status=400)
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        context = str((body or {}).get("context") or "").strip()

        project = await db.get_project(rec.get("related_project") or "general")
        if project is None:
            project = {"slug": "voice-process", "repo_path": os.getcwd(),
                       "defaults_json": {}, "mcp_servers": [],
                       "native_tools": []}
        prompt = (
            "Resume este transcript de mi sesión de voz y extrae las "
            "acciones concretas. Si no hay acciones, lista los puntos "
            "clave.\n\n"
            + (f"Contexto extra: {context}\n\n" if context else "")
            + f"--- TRANSCRIPT ---\n{transcript}")
        try:
            result = await experts.run_expert(project, prompt, db=db)
        except experts.ModelUnavailable as e:
            return web.json_response({"error": str(e)}, status=503)
        except asyncio.TimeoutError:
            return web.json_response(
                {"error": "timeout del experto"}, status=504)
        except Exception as e:  # noqa: BLE001 — el KB no debe romperse
            logger.exception("voice: process %s falló", trx_id)
            return web.json_response(
                {"error": f"experto falló: {type(e).__name__}: {str(e)[:200]}"},
                status=502)
        summary = (result.get("content") or "").strip()
        rec["processed"] = {
            "summary": summary, "model": result.get("model"),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        await voice.persist_transcript(rec)
        logger.info("voice: transcript %s procesado (summary %d chars)",
                    trx_id, len(summary))
        return web.json_response(
            {"id": trx_id, "summary": summary,
             "model": result.get("model"), "already_processed": False})


def register_voice_routes(app: web.Application, require_auth) -> None:
    """Registra las rutas autenticadas de voz en la aplicación."""
    app.router.add_post("/voice/transcribe", require_auth(voice_transcribe))
    app.router.add_get("/voice/transcripts", require_auth(voice_transcripts_list))
    app.router.add_get(
        "/voice/transcripts/{id}", require_auth(voice_transcripts_get))
    app.router.add_post(
        "/voice/transcripts/{id}/process",
        require_auth(voice_transcript_process))
