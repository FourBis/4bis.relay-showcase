"""Server domain handlers extracted from the composition entrypoint."""
from __future__ import annotations

import json
import re
from typing import Optional

from aiohttp import web

from .server_common import DB_KEY, _require_auth
from . import attachments as attachments_mod
from .db import Database
@_require_auth
async def expert_question_skip(request: web.Request) -> web.Response:
    """POST /questions/{q_id}/skip — el humano decide no contestar."""
    db = request.app[DB_KEY]
    q_id = request.match_info["q_id"]
    if await db.get_expert_question(q_id) is None:
        return web.json_response({"error": "not found"}, status=404)
    await db.skip_expert_question(q_id)
    return web.json_response({"ok": True, "id": q_id, "status": "skipped"})


# ---- Iter 9.7: preguntas interactivas (night_questions) ----
# Endpoints admin para que el humano responda checkpoints sin abrir
# Discord. Idempotentes: 409 si la pregunta ya está respondida.
# El orchestrator está bloqueado en ask_and_wait() esperando y lo lee
# en su próximo poll (5s).

@_require_auth
async def night_questions_list(request: web.Request) -> web.Response:
    """GET /admin/api/night/questions?run_id=<id>&only_open=1"""
    db: Database = request.app[DB_KEY]
    run_id = request.query.get("run_id", "") or None
    only_open = request.query.get("only_open", "0") == "1"
    rows = await db.list_night_questions(
        run_id=run_id, only_open=only_open)
    # Decodificar question_json y answer_json para el cliente.
    out = []
    for r in rows:
        try:
            q = json.loads(r["question_json"])
        except (json.JSONDecodeError, TypeError):
            q = {"_raw": r["question_json"]}
        try:
            a = json.loads(r["answer_json"]) if r.get("answer_json") else None
        except (json.JSONDecodeError, TypeError):
            a = None
        out.append({
            "id": r["id"],
            "run_id": r["run_id"],
            "phase": r["phase"],
            "question": q,
            "asked_at": r["asked_at"],
            "answered_at": r.get("answered_at"),
            "answer": a,
            "open": r.get("answered_at") is None,
        })
    return web.json_response({"questions": out})


@_require_auth
async def night_question_answer(request: web.Request) -> web.Response:
    """POST /admin/api/night/questions/<q_id>/answer
    body: {"choice": "B", "free_text": null}
    → guarda respuesta, el orchestrator la lee en su próximo poll.
    409 si ya está respondida (idempotente, gana la primera)."""
    db: Database = request.app[DB_KEY]
    q_id = request.match_info.get("q_id", "")
    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    answer = {
        "choice": body.get("choice"),
        "free_text": body.get("free_text"),
    }
    if not answer["choice"] and not answer["free_text"]:
        return web.json_response(
            {"error": "choice o free_text requerido"}, status=400)
    ok = await db.answer_night_question(q_id, answer)
    if not ok:
        # Ya respondida o no existe — distinguir para el cliente.
        existing = await db.get_night_question(q_id)
        if existing is None:
            return web.json_response(
                {"error": f"question {q_id!r} desconocida"},
                status=404)
        return web.json_response(
            {"error": "ya respondida",
             "answered_at": existing.get("answered_at"),
             "answer": existing.get("answer_json")},
            status=409)
    return web.json_response({"ok": True, "q_id": q_id, "answer": answer})


@_require_auth
async def night_question_skip(request: web.Request) -> web.Response:
    """POST /admin/api/night/questions/<q_id>/skip
    → cierra la pregunta sin respuesta. El orchestrator sigue con el
    default de la pregunta. Mismas reglas de idempotencia que /answer."""
    db: Database = request.app[DB_KEY]
    q_id = request.match_info.get("q_id", "")
    ok = await db.skip_night_question(q_id)
    if not ok:
        existing = await db.get_night_question(q_id)
        if existing is None:
            return web.json_response(
                {"error": f"question {q_id!r} desconocida"},
                status=404)
        return web.json_response(
            {"error": "ya respondida",
             "answered_at": existing.get("answered_at")},
            status=409)
    return web.json_response({"ok": True, "q_id": q_id, "skipped": True})


# ---- Iter 9.8: canal Discord para checkpoints ----
# El bot C# hace POST a este endpoint cuando el humano
# clickea un botón de embed (o usa un slash command /day).
# Mismo primitive que /admin/api/night/questions/<q_id>/answer pero
# sin auth: el bot corre en localhost, /discord/* es la convención
# para endpoints de Discord (mismo patrón que la ausencia de auth en
# /notify). Si algún día se mete auth, aplica a TODAS las rutas /discord/*.

async def discord_day_answer(request: web.Request) -> web.Response:
    """POST /discord/day-answer
    body: {"discord_user_id": str, "run_id": str, "question_id": str,
           "choice": str|null, "free_text": str|null}
    → guarda la respuesta. El orchestrator (bloqueado en
    _ask_and_wait) la lee en su próximo poll (5s) y sigue.
    Idempotente: si la pregunta ya está respondida → 409 (la primera
    gana, igual que el endpoint admin). Si no existe → 404.
    Si choice y free_text ambos vacíos → 400.
    """
    db: Database = request.app[DB_KEY]
    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    q_id = body.get("question_id", "")
    choice = body.get("choice")
    free_text = body.get("free_text")
    if not q_id:
        return web.json_response({"error": "question_id requerido"},
                                 status=400)
    if not choice and not free_text:
        return web.json_response(
            {"error": "choice o free_text requerido"}, status=400)
    answer = {"choice": choice, "free_text": free_text}
    ok = await db.answer_night_question(q_id, answer)
    if not ok:
        existing = await db.get_night_question(q_id)
        if existing is None:
            return web.json_response(
                {"error": f"question {q_id!r} desconocida"},
                status=404)
        return web.json_response(
            {"error": "ya respondida",
             "answered_at": existing.get("answered_at"),
             "answer": existing.get("answer_json")},
            status=409)
    return web.json_response({"ok": True, "q_id": q_id, "answer": answer})


# Ids de attachment mencionados en la respuesta del experto. Mismo
# formato que `attachments._ID_RE` (att_ + 16 hex); acá va sin anclas
# porque buscamos dentro de prosa. El match se valida contra el disco.
_ATT_ID_RE = re.compile(r"\batt_[0-9a-f]{16}\b")


# ---- Discord-attachment: upload genérico de bytes por el bot C# ----
# El bot C# descarga el attachment de Discord CDN (con auth del bot)
# y lo sube al relay como multipart. El relay lo guarda en disco con
# id estable (sha256[:16]) y devuelve la ruta local. El bot pasa
# después el id (o los ids) en `POST /experts/run` y el relay inyecta
# un bloque "## Adjuntos" al texto que se le pasa al LLM.
#
# Sin auth (mismo patrón que /discord/day-answer y /notify). Si algún
# día se mete auth, aplica a TODAS las rutas /discord/*.

async def discord_attachments_upload(request: web.Request) -> web.Response:
    """POST /discord/attachments (multipart) — guarda un attachment.

    Campos:
        file (multipart file, requerido): los bytes crudos.
        filename (opcional): nombre original para derivar la extensión.
        mimetype (opcional): ej 'image/png', 'application/pdf'.

    Returns:
        201 -> {id, path, bytes, mimetype}
        400 -> falta `file` o no es multipart válido.
        413 -> file > ATTACHMENT_MAX_BYTES (default 50MB).
    """
    cap = attachments_mod.max_attachment_bytes()
    try:
        reader = await request.multipart()
    except (ValueError, AssertionError):
        return web.json_response(
            {"error": "se espera multipart/form-data"}, status=400)

    file_bytes: Optional[bytes] = None
    filename = ""
    mimetype: Optional[str] = None
    async for part in reader:
        if part.name == "file":
            buf = bytearray()
            while True:
                chunk = await part.read_chunk(64 * 1024)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > cap:
                    return web.json_response(
                        {"error": f"file > {cap} bytes"}, status=413)
            file_bytes = bytes(buf)
            filename = part.filename or ""
            mimetype = part.headers.get("Content-Type") or None
        # Otros campos opcionales (filename/mimetype como FormField).
        elif part.name in ("filename", "mimetype"):
            value = (await part.text()).strip()
            if part.name == "filename":
                filename = value
            elif part.name == "mimetype":
                mimetype = value or None

    if not file_bytes:
        return web.json_response(
            {"error": "falta el archivo (multipart field `file`)"},
            status=400)

    try:
        attach_id, path, size = attachments_mod.store(
            file_bytes, filename=filename, mimetype=mimetype)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    return web.json_response(
        {"id": attach_id,
         "path": str(path).replace("\\", "/"),
         "bytes": size,
         "mimetype": mimetype or "",
         "filename": filename,
         # `inline`: el contenido se inyecta como TEXTO en el prompt.
         # El cliente lo usa para avisarle al usuario en el acto en vez
         # de hacerlo esperar a que el LLM conteste "no puedo verlo".
         "inline": attachments_mod.is_inlineable(str(path)),
         # `viewable`: es una imagen que el modelo VE (2026-07-31). No
         # es inline —no es texto— pero tampoco es opaca. Sin este campo
         # cada cliente tendría que duplicar la lista de extensiones y
         # se irían de sync al primer cambio.
         "viewable": bool(attachments_mod.image_mime(path))},
        status=201)


async def discord_attachments_download(request: web.Request) -> web.Response:
    """GET /discord/attachments/{id} — devuelve los bytes guardados.

    La contraparte del POST. El bot la usa para bajar lo que el experto
    generó durante el run (típico: un `screenshot`) y reenviarlo a
    Discord como archivo.

    El id es content-addressed y `attachments.resolve()` lo valida
    contra un regex estricto (`att_` + 16 hex) antes de tocar el disco,
    así que no hay path traversal posible por acá.

        200 -> los bytes
        404 -> el id no existe (o no matchea el formato)
    """
    attach_id = request.match_info.get("attach_id", "")
    path = attachments_mod.resolve(attach_id)
    if path is None:
        return web.json_response(
            {"error": f"attachment {attach_id!r} no encontrado"}, status=404)
    return web.FileResponse(path)
