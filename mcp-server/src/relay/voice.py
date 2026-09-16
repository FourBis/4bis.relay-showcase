"""Voice input — Track D / F5 (docs/VOICE_INPUT.md).

El bot Discord (bot-demo) captura el audio (VC session o attachment) y
lo manda entero por multipart a `POST /voice/transcribe`. Acá se
persiste el audio, se transcribe con el ASR de MiniNax (misma
API key / base_url que el LLM) y se escribe el transcript como KB
permanente en `state/transcripts/trx_<id>.jsonl`.

El artifact es la TRANSCRIPCIÓN, no una respuesta de LLM: el
procesamiento (`/process`) es opcional y posterior.

Ponytail: un módulo, sin clases. El único punto que habla con la red
es `transcribe_minimax()` — los tests lo mockean ahí.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import httpx

from . import config

logger = logging.getLogger("relay.voice")

AUDIO_EXTS = {".opus", ".mp3", ".ogg", ".wav", ".m4a"}


def max_audio_bytes() -> int:
    return int(config.get("VOICE_MAX_AUDIO_BYTES"))


def audio_dir() -> Path:
    return Path(config.get("VOICE_AUDIO_DIR"))


def transcripts_dir() -> Path:
    return Path(config.get("VOICE_TRANSCRIPTS_DIR"))


def stt_timeout_s() -> float:
    return float(config.get("MINIMAX_STT_TIMEOUT"))


def new_trx_id(audio_bytes: bytes) -> str:
    """trx_<sha256(audio + timestamp)[:8]> — estable dentro del request."""
    h = hashlib.sha256(audio_bytes + str(time.time_ns()).encode())
    return f"trx_{h.hexdigest()[:8]}"


async def transcribe_minimax(audio_path: Path) -> dict:
    """Llama al ASR configurado y devuelve el bloque `stt` + transcript.

    Nombre histórico (`_minimax`): el request es multipart estándar
    OpenAI (file+model+language) — MiniMax fue el proveedor original
    previsto, pero su API pública NO tiene ASR (confirmado 2026-07-10,
    ver docs oficiales). El STT real hoy es Groq (whisper-large-v3,
    mismo shape) vía `MINIMAX_STT_URL` + `VOICE_STT_API_KEY` del panel —
    enchufar cualquier proveedor con este mismo shape es config, no
    código. Modelo `MINIMAX_STT_MODEL`, idioma `MINIMAX_STT_LANGUAGE`
    (default es-419 — OJO: proveedores que exigen ISO-639-1 puro
    quieren "es", no "es-419").

    Levanta:
        asyncio.TimeoutError  → el handler responde 504
        RuntimeError          → el handler responde 502
    """
    # VOICE_STT_API_KEY pisa a MINIMAX_API_KEY: MiniMax no tiene ASR
    # público (confirmado 2026-07-10), así que el STT real hoy viene de
    # otro proveedor (ej. Groq whisper-large-v3, mismo shape multipart
    # OpenAI) con su propia key — no la de MiniMax.
    api_key = config.get("VOICE_STT_API_KEY")
    if not api_key:
        raise RuntimeError(
            "VOICE_STT_API_KEY no configurada en el panel")
    stt_url = config.get("MINIMAX_STT_URL")
    # provider: derivado del host real, no hardcodeado — MINIMAX_STT_URL
    # puede apuntar a cualquier proveedor con este shape (Groq hoy).
    provider = urlsplit(stt_url).hostname or "unknown"
    model = config.get("MINIMAX_STT_MODEL")
    language = config.get("MINIMAX_STT_LANGUAGE")

    t0 = time.monotonic()
    audio_bytes = await asyncio.to_thread(audio_path.read_bytes)
    try:
        async with httpx.AsyncClient(timeout=stt_timeout_s()) as client:
            r = await client.post(
                stt_url,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (audio_path.name, audio_bytes)},
                data={"model": model, "language": language},
            )
    except httpx.TimeoutException as e:
        raise asyncio.TimeoutError(str(e)) from e
    except httpx.HTTPError as e:
        raise RuntimeError(f"ASR request falló: {e}") from e
    if r.status_code >= 400:
        raise RuntimeError(f"ASR {r.status_code}: {r.text[:300]}")
    try:
        data = r.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ASR devolvió no-JSON: {r.text[:200]}") from e
    text = data.get("text")
    if not isinstance(text, str):
        raise RuntimeError(f"ASR sin campo text: {str(data)[:200]}")
    return {
        "provider": provider,
        "model": model,
        "language": language,
        "chunks": 1,
        "tokens_in": int(data.get("usage", {}).get("total_tokens", 0) or 0),
        "duration_ms_stt": int((time.monotonic() - t0) * 1000),
        "text": text,
    }


async def persist_transcript(record: dict) -> Path:
    """Escribe `state/transcripts/<id>.jsonl` (un objeto JSON por archivo)."""
    tdir = transcripts_dir()
    await asyncio.to_thread(tdir.mkdir, parents=True, exist_ok=True)
    path = tdir / f"{record['id']}.jsonl"
    payload = json.dumps(record, ensure_ascii=False)
    await asyncio.to_thread(path.write_text, payload + "\n", "utf-8")
    return path


async def read_transcript(trx_id: str) -> Optional[dict]:
    """Lee un transcript persistido. None si no existe / id inválido."""
    # ids son trx_<hex>; rechazar cualquier cosa con separadores de path
    if not trx_id.startswith("trx_") or any(c in trx_id for c in "/\\."):
        return None
    path = transcripts_dir() / f"{trx_id}.jsonl"
    try:
        text = await asyncio.to_thread(path.read_text, "utf-8")
        return json.loads(text)
    except (OSError, json.JSONDecodeError):
        return None


async def list_transcripts(limit: int = 50) -> list[dict]:
    """Índice liviano de transcripts (para debug / futura UI admin)."""
    tdir = transcripts_dir()

    def _scan() -> list[dict]:
        if not tdir.is_dir():
            return []
        out = []
        files = sorted(tdir.glob("trx_*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[:limit]:
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append({k: rec.get(k) for k in (
                "id", "ts", "author", "mode", "topic", "related_project")}
                | {"duration_s": (rec.get("audio") or {}).get("duration_s"),
                   "processed": bool(rec.get("processed"))})
        return out

    return await asyncio.to_thread(_scan)
