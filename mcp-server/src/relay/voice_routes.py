"""Rutas HTTP del flujo de voz; proveedor y persistencia viven en ``voice``."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import logging
import math
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from aiohttp import web

from . import experts, voice
from .app_state import DB_KEY
from .db import Database

logger = logging.getLogger("relay.server")

# Dos /process simultáneos para el mismo transcript esperan y respetan la
# marca `processed` que persiste el primero.
_voice_process_locks: dict[tuple[str, str], asyncio.Lock] = {}
# Sólo resultados ya obtenidos cuyo guardado aún no fue confirmado. Al
# reintentar se guarda el mismo resultado, sin volver a llamar al modelo.
_voice_process_receipts: dict[tuple[str, str], dict] = {}


def _transcript_source(record):
    return json.dumps({key: value for key, value in record.items()
        if key not in {"processed", "_process_result", "_process_result_id"}},
        sort_keys=True, ensure_ascii=False)


async def _save_processed_transcript(key, record, *, already_processed):
    processed = record["processed"]
    response = {"id": record["id"], "summary": processed.get("summary", ""),
                "model": processed.get("model"), "already_processed": already_processed}
    try:
        await voice.persist_transcript(record, directory=Path(key[0]))
    except asyncio.CancelledError:
        # El worker termina antes de propagar. Si confirmó el archivo, el
        # próximo POST lo leerá; si falló, el recibo sigue disponible aquí.
        raise
    except Exception:
        logger.exception("voice: resumen obtenido, guardado pendiente trx=%s", record["id"])
        return web.json_response({**response, "persistence_pending": True,
            "error": "El resumen ya se generó y se conserva en esta instancia. No se pudo guardar; reintentar sólo guardará ese mismo resultado."}, status=503)
    _voice_process_receipts.pop(key, None)
    return web.json_response(response)


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
    """GET /voice/transcripts/{id}: registro público, sin rescate interno."""
    rec = await voice.read_transcript(request.match_info["id"])
    if rec is None:
        return web.json_response({"error": "no existe"}, status=404)
    # Historial/uso crudo del ejecutor es metadata interna de recuperación;
    # no ampliar el contenido que expone el endpoint de transcripts.
    return web.json_response({key: value for key, value in rec.items()
        if key not in {"_process_result", "_process_result_id"}})


async def voice_transcript_process(request: web.Request) -> web.Response:
    """Resume un transcript con el experto del proyecto asociado."""
    db: Database = request.app[DB_KEY]
    trx_id = request.match_info["id"]
    key = (str(voice.transcripts_dir().resolve()), trx_id)
    lock = _voice_process_locks.setdefault(key, asyncio.Lock())
    async with lock:
        rec = await voice.read_transcript(trx_id, directory=Path(key[0]))
        if rec is None:
            return web.json_response({"error": "no existe"}, status=404)
        pending = _voice_process_receipts.get(key)
        prev = rec.get("processed")
        if prev is not None:
            if (not isinstance(prev, dict) or not isinstance(prev.get("summary"), str)
                    or not (prev.get("model") is None or isinstance(prev.get("model"), str))):
                return web.json_response({"error": "El estado de procesamiento guardado no es válido. Se conservó el registro y no se ejecutó el modelo.",
                    "code": "voice_process_state_invalid"}, status=409)
            if pending is not None and rec.get("_process_result_id") != pending["record"].get("_process_result_id"):
                return web.json_response({"error": "Se encontró otro resultado guardado para este transcript. Se conserva el resultado pendiente y no se sobrescribió ninguno.",
                    "code": "voice_process_result_changed", "persistence_pending": True}, status=409)
            _voice_process_receipts.pop(key, None)
            return web.json_response(
                {"id": trx_id, "summary": prev.get("summary", ""),
                 "model": prev.get("model"), "already_processed": True})
        if pending is not None:
            if _transcript_source(rec) != pending["source"]:
                return web.json_response({"error": "El transcript cambió mientras su resultado estaba pendiente de guardar. Se conservó el resultado anterior y no se volvió a ejecutar el modelo.",
                    "code": "voice_process_source_changed", "persistence_pending": True}, status=409)
            return await _save_processed_transcript(key, pending["record"], already_processed=True)
        if not isinstance(rec.get("transcript"), str):
            return web.json_response({"error": "El transcript guardado no es texto válido; no se ejecutó el modelo.",
                "code": "voice_process_source_invalid"}, status=409)
        transcript = rec["transcript"].strip()
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
        # Snapshot síncrono antes de cualquier await posterior al modelo.
        # No concede acceso a los adjuntos mencionados en su metadata.
        rec["_process_result"] = deepcopy(result)
        rec["_process_result_id"] = uuid.uuid4().hex
        completed = deepcopy(rec)
        _voice_process_receipts[key] = {"source": _transcript_source(rec), "record": completed}
        logger.info("voice: transcript %s procesado (summary %d chars)",
                    trx_id, len(summary))
        return await _save_processed_transcript(key, completed, already_processed=False)


def register_voice_routes(app: web.Application, require_auth) -> None:
    """Registra las rutas autenticadas de voz en la aplicación."""
    app.router.add_post("/voice/transcribe", require_auth(voice_transcribe))
    app.router.add_get("/voice/transcripts", require_auth(voice_transcripts_list))
    app.router.add_get(
        "/voice/transcripts/{id}", require_auth(voice_transcripts_get))
    app.router.add_post(
        "/voice/transcripts/{id}/process",
        require_auth(voice_transcript_process))
