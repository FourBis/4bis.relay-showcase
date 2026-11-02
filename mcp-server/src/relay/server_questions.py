"""Server domain handlers extracted from the composition entrypoint."""
from __future__ import annotations

import json
import re
from typing import Optional

from aiohttp import web

from .server_common import DB_KEY, NIGHT_KEY, _require_auth
from . import identity
from . import attachments as attachments_mod
from .db import Database


async def _question_graph(db, question):
    """Resuelve el grafo o conserva la conversación para autorizar huérfanos."""
    if question.get("kind") == "grafo":
        try:
            payload = json.loads(question.get("question_json") or "{}")
        except (TypeError, ValueError):
            payload = {}
        if isinstance(payload, dict) and payload.get("graph_id"):
            graph = await db.get_task_graph(payload["graph_id"])
            if graph:
                return graph
    if chat_id := question.get("chat_id"):
        nodes = await db.run(
            "SELECT graph_id FROM tasks WHERE chat_id=? "
            "ORDER BY (estado='esperando_humano') DESC LIMIT 1", (chat_id,))
        if nodes:
            graph = await db.get_task_graph(nodes[0]["graph_id"])
            if graph:
                return graph
    cid = question.get("conversation_id")
    if not cid and question.get("chat_id"):
        chat = await db.get_chat(question["chat_id"])
        cid = (chat or {}).get("conversation_id")
    if cid:
        graph = await db.active_task_graph(cid)
        if graph:
            return graph
    return {"conversation_id": cid, "project_slug": question.get("project_slug")}


@_require_auth
async def expert_question_skip(request: web.Request) -> web.Response:
    """POST /questions/{q_id}/skip — el humano decide no contestar."""
    db = request.app[DB_KEY]
    q_id = request.match_info["q_id"]
    question = await db.get_expert_question(q_id)
    if question is None:
        return web.json_response({"error": "not found"}, status=404)
    graph = await _question_graph(db, question)
    if not await identity.can_control_graph(request, db, graph):
        return web.json_response({"error": "No tienes permiso para controlar esta tarea."}, status=403)
    await db.skip_expert_question(q_id)
    return web.json_response({"ok": True, "id": q_id, "status": "skipped"})


# ---- Iter 9.7: preguntas interactivas (night_questions) ----
# Endpoints admin para que el humano responda checkpoints sin abrir
# Discord. Idempotentes: 409 si la pregunta ya está respondida.
# El orchestrator está bloqueado en ask_and_wait() esperando y lo lee
# en su próximo poll (5s).

def _night_question_access_tx(conn, actor, question, *, write=False, active=False, live_actor=None):
    """A question, its run and current account belong to one read snapshot."""
    from .conversation_access import user_tx
    user = user_tx(conn, actor)
    if not user:
        return False
    if write and active and live_actor != actor:
        return False
    if user.get("role") == identity.OWNER_ROLE:
        return True  # Administrative history access does not reassign the run.
    run = conn.execute("SELECT requested_by,project_slug FROM night_runs WHERE id=?",
                       (question.get("run_id") or "",)).fetchone()
    if not run or not run["requested_by"] or run["requested_by"] != actor:
        return False
    project = conn.execute("SELECT slug,enabled,defaults_json FROM projects WHERE slug=? COLLATE NOCASE",
                           (run["project_slug"],)).fetchone()
    if not project or not project["enabled"]:
        return False
    try:
        defaults = json.loads(project["defaults_json"] or "{}")
    except (ValueError, TypeError):
        return False
    return bool(isinstance(defaults, dict) and not defaults.get("read_only")
                and identity.user_can_write_project(user, project["slug"]))


async def _night_question_access(request, question, *, write=False):
    """Keep live write ownership and durable authorization distinct."""
    from . import conversation_access
    active = (request.app.get(NIGHT_KEY) or {}).get(question.get("run_id"))
    live_actor = getattr(active[0], "relay_actor", None) if active else None
    return await conversation_access.transaction(request.app[DB_KEY], lambda conn:
        _night_question_access_tx(conn, identity.requester(request), question,
                                  write=write, active=bool(active), live_actor=live_actor))


def _night_question_denied():
    # Misma respuesta para id ajeno o inexistente: no filtrar su contenido.
    return web.json_response({"error": "Pregunta no disponible para esta cuenta."}, status=404)

@_require_auth
async def night_questions_list(request: web.Request) -> web.Response:
    """GET /admin/api/night/questions: authorize and read in one snapshot."""
    from . import conversation_access
    db = request.app[DB_KEY]
    run_id = request.query.get("run_id", "") or None
    only_open = request.query.get("only_open", "0") == "1"
    actor = identity.requester(request)
    def snapshot(conn):
        where, params = [], []
        if run_id:
            where.append("run_id=?")
            params.append(run_id)
        if only_open:
            where.append("answered_at IS NULL")
        sql = "SELECT * FROM night_questions" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY asked_at DESC"
        out = []
        for row in conn.execute(sql, params):
            item = dict(row)
            if not _night_question_access_tx(conn, actor, item):
                continue
            try:
                question = json.loads(item["question_json"])
            except (ValueError, TypeError):
                question = {"_raw": item["question_json"]}
            try:
                answer = json.loads(item["answer_json"]) if item.get("answer_json") else None
            except (ValueError, TypeError):
                answer = None
            out.append({"id": item["id"], "run_id": item["run_id"], "phase": item["phase"],
                        "question": question, "asked_at": item["asked_at"],
                        "answered_at": item.get("answered_at"), "answer": answer,
                        "open": item.get("answered_at") is None})
        return out
    out = await conversation_access.transaction(db, snapshot)
    return web.json_response({"questions": out}, headers={"Cache-Control": "no-store"})


@_require_auth
async def night_question_answer(request: web.Request) -> web.Response:
    """POST /admin/api/night/questions/<q_id>/answer
    body: {"choice": "B", "free_text": null}
    → guarda respuesta, el orchestrator la lee en su próximo poll.
    409 si ya está respondida (idempotente, gana la primera)."""
    db: Database = request.app[DB_KEY]
    q_id = request.match_info.get("q_id", "")
    existing = await db.get_night_question(q_id)
    if existing is None or not await _night_question_access(request, existing, write=True):
        return _night_question_denied()
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
    try:
        ok = await db.answer_night_question(q_id, answer, actor=identity.requester(request))
    except PermissionError:
        return _night_question_denied()
    if not ok:
        # Ya respondida o no existe — distinguir para el cliente.
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
    existing = await db.get_night_question(q_id)
    if existing is None or not await _night_question_access(request, existing, write=True):
        return _night_question_denied()
    try:
        ok = await db.skip_night_question(q_id, actor=identity.requester(request))
    except PermissionError:
        return _night_question_denied()
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
# El bot C# (OdyMazo) hace POST a este endpoint cuando el humano
# clickea un botón de embed (o usa un slash command /day).
# Usa el mismo control de actor que Admin. El transporte local del bot
# no acredita al humano que eligió la respuesta; requiere una sesión
# personal verificable hasta disponer de un mapping Discord autorizado.

async def discord_day_answer(request: web.Request) -> web.Response:
    """POST /discord/day-answer
    body: {"discord_user_id": str, "run_id": str, "question_id": str,
           "choice": str|null, "free_text": str|null}
    Con identidad personal verificada, guarda la respuesta. El orchestrator (bloqueado en
    _ask_and_wait) la lee en su próximo poll (5s) y sigue.
    Idempotente: si la pregunta ya está respondida → 409 (la primera
    gana, igual que el endpoint admin). Si no existe → 404.
    Si choice y free_text ambos vacíos → 400.
    """
    db: Database = request.app[DB_KEY]
    # El bot local y sus discord_user_id/run_id del body no acreditan a
    # la persona que pulsó un botón. No hay mapping durable verificado
    # Discord→cuenta Relay: hasta tenerlo, decidir desde la sesión personal.
    if identity.requester(request) in {identity.OWNER, "anonymous", ""}:
        return web.json_response({
            "error": "actor_required",
            "message": "Responde esta pregunta en Relay con tu cuenta; el puente Discord no verifica al actor.",
        }, status=403)
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
    existing = await db.get_night_question(q_id)
    if (existing is None or body.get("run_id") != existing.get("run_id")
            or not await _night_question_access(request, existing, write=True)):
        return _night_question_denied()
    answer = {"choice": choice, "free_text": free_text}
    try:
        ok = await db.answer_night_question(q_id, answer, actor=identity.requester(request))
    except PermissionError:
        return _night_question_denied()
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

    await request.app[DB_KEY].run("INSERT OR IGNORE INTO attachment_uploaders(attachment_id,actor) VALUES(?,?)",
                                (attach_id, identity.requester(request)))
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
    from . import conversation_access
    if not await conversation_access.can_read_attachment(request.app[DB_KEY], identity.requester(request), attach_id):
        return web.json_response({"error": "Adjunto no disponible"}, status=404)
    path = attachments_mod.resolve(attach_id)
    if path is None:
        return web.json_response(
            {"error": f"attachment {attach_id!r} no encontrado"}, status=404)
    # Uploaded HTML/SVG/PDF and unknown bytes must never become an active
    # document in Relay's authenticated origin. Only the passive image types
    # already supported by the attachment pipeline may render inline. These
    # headers also cover existing blobs, without changing storage or ACLs.
    image_type = attachments_mod.image_mime(path)
    disposition = "inline" if image_type else "attachment"
    return web.FileResponse(path, headers={
        "Content-Type": image_type or "application/octet-stream",
        "Content-Disposition": f'{disposition}; filename="{path.name}"',
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'",
        "Cache-Control": "private, no-store",
    })
