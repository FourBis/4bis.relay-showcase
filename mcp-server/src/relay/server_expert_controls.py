"""Control de ejecuciones existentes: cancelación y cambios de rumbo."""
from __future__ import annotations

import asyncio
import json
import uuid

from aiohttp import web

from . import identity, task_service
from .app_state import DB_KEY, GRAFOS_KEY, PROGRESS_KEY, RUNNING_KEY
from .server_common import logger


async def _graph_chat_cancel_conflict(request, chat):
    """Graph workers live outside RUNNING_KEY; do not clean them as zombies."""
    db = request.app[DB_KEY]
    registries = (request.app.get(GRAFOS_KEY, {}), getattr(db, "_graph_cancellations", {}))
    def alive(gid):
        return any((worker := registry.get(gid)) is not None and not worker.done()
                   for registry in registries)
    links = await db.run(
        "SELECT g.id,g.project_slug,g.conversation_id,t.estado AS node_state "
        "FROM tasks t JOIN task_graphs g ON g.id=t.graph_id WHERE t.chat_id=?", (chat["id"],))
    for graph in links:
        if graph["node_state"] != "corriendo" and not alive(graph["id"]):
            continue
        body = {"code": "graph_managed_run",
                "error": "Este run pertenece a un plan; usa sus controles para detenerlo o revisar su estado."}
        if (graph.get("conversation_id") and graph["conversation_id"] == chat.get("conversation_id")
                and graph.get("project_slug") == chat.get("project_slug")):
            from . import conversation_access
            actor = identity.requester(request)
            if (await conversation_access.get_access(db, graph["conversation_id"], actor))["can_control"]:
                body["graph_id"] = graph["id"]
        return web.json_response(body, status=409)
    if not links and chat.get("source") == "grafo":
        # create_chat precedes task.chat_id during protected node setup. A
        # matching live parent prevents cleanup in that brief linkage window;
        # without the exact node link do not claim or expose a graph ID.
        parents = await db.run(
            "SELECT id FROM task_graphs WHERE estado='activo' AND "
            "conversation_id IS ? AND project_slug IS ?",
            (chat.get("conversation_id"), chat.get("project_slug")))
        if any(alive(graph["id"]) for graph in parents):
            return web.json_response({"code": "graph_managed_run",
                "error": "Este run pertenece a un plan que sigue en curso; usa sus controles para revisar su estado."}, status=409)
    return None



async def cancel_expert(request, chat_id, *, graph_conflict):
    db, running = request.app[DB_KEY], request.app[RUNNING_KEY]
    progress = request.app[PROGRESS_KEY]
    chat = await db.get_chat(chat_id)
    if not await identity.can_control_chat(request, db, chat):
        return web.json_response({"error": "No tienes permiso para controlar este run."}, status=403)
    event = await db.conversation_event_for_chat(chat_id)
    rp = progress.get(chat_id)
    if getattr(rp, "closing", False):
        return web.json_response({"error": "La ejecución está guardando su resultado; espera."}, status=409)
    if event:
        cid = event["conversation_id"]
        if not await identity.can_control_graph(request, db, {"conversation_id": cid}):
            return web.json_response({"error": "No tienes permiso para controlar esta tarea."}, status=403)
        # No pausar otro turno si el solicitado terminó durante la autorización.
        paused = await db.run(
            "UPDATE conversations SET task_json=json_patch(COALESCE(task_json,'{}'),?) "
            "WHERE id=(SELECT conversation_id FROM conversation_events WHERE id=? "
            "AND state IN ('pending','processing','uncertain')) RETURNING id",
            (json.dumps({"state": "paused", "tracking": {"enabled": False}}), event["id"]))
        if not paused:
            return web.json_response({"cancelled": [], "finished": [chat_id]})
        event = await db.conversation_event_for_chat(chat_id)
        if getattr(progress.get(chat_id), "closing", False):
            return web.json_response({"error": "La ejecución está guardando su resultado; espera.",
                                      "conversation_id": cid, "paused": True}, status=409)
        if event["state"] == "uncertain":
            return web.json_response({"error": "El evento tiene efectos inciertos; revisa el resultado antes de continuar.",
                                      "conversation_id": cid, "paused": True}, status=409)
        if event["state"] not in {"pending", "processing"}:
            return web.json_response({"cancelled": [], "finished": [chat_id], "conversation_id": cid})
        if event["state"] == "pending":
            await db.finish_conversation_event(event["id"], state="cancelled", error="Cancelado por el usuario")
        elif event["state"] == "processing":
            runner = request.app.get(task_service.TASK_RUNNERS_KEY, {}).get(cid)
            if runner is None or runner.done():
                return web.json_response({"error": "No hay un proceso vivo; revisa el evento antes de continuar.",
                                          "conversation_id": cid, "paused": True}, status=409)
            if getattr(progress.get(chat_id), "finished", False):
                # El experto terminó; pueden quedar un grafo y el cierre del evento.
                from .server_graph_helpers import _cancelar_grafo
                if graph := await db.active_task_graph(cid):
                    await _cancelar_grafo(request.app, graph["id"])
                await asyncio.gather(runner, return_exceptions=True)
                return web.json_response({"cancelled": [], "finished": [chat_id], "conversation_id": cid})
            if not runner.cancelling():
                runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        return web.json_response({"cancelled": [chat_id], "conversation_id": cid})
    worker = running.get(chat_id)
    if worker is not None:
        if not worker.done() and not worker.cancelling():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        chat = await db.get_chat(chat_id)
        if chat and chat.get("status") in {"queued", "running"}:
            return web.json_response({"error": "La ejecución terminó sin guardar su cierre; revisa el registro."}, status=409)
        return web.json_response({"cancelled": [chat_id] if (chat or {}).get("status") == "cancelled" else [],
                                  "finished": [chat_id]})
    if chat and chat.get("status") in {"queued", "running"}:
        conflict = await graph_conflict(request, chat)
        if conflict is not None:
            return conflict
    if chat and chat.get("status") == "running":
        # Authorization/graph checks await while the executor can finish. The
        # transition, not the earlier snapshot, must prove this was a zombie.
        # Never overwrite a completed result or report a cleanup we did not do.
        changed = await db.cancel_running_chat(
            chat_id, error="zombie: cancelado sin proceso vivo")
        if changed:
            return web.json_response({"cancelled": [], "zombies_cleaned": [chat_id]})
        chat = await db.get_chat(chat_id)
        if chat and chat.get("status") in {"queued", "running"}:
            return web.json_response({"error": "La ejecución cambió de estado; revisa su registro."}, status=409)
    if chat:
        return web.json_response({"cancelled": [], "finished": [chat_id]})
    return web.json_response({"error": "no hay run en curso con ese id"}, status=404)



async def steer_expert(request: web.Request) -> web.Response:
    """POST /experts/steer/{chat_id} — corrige el rumbo sin matar el run.

    Body: {"message": "no toques config.py, el bug está en admin.py"}.

    Encola el texto en el RunProgress; run_expert lo consume en el próximo
    borde de nodo (o sea: cuando termine la tool en vuelo, no a mitad),
    rescata el historial y re-entra con eso como prompt. El humano no
    pierde el trabajo hecho, que era lo que pasaba con la única salida que
    había antes: cancelar.

    409 si el run ya terminó — el caller (UI) reintenta como mensaje
    normal, que en una conversación es exactamente lo mismo.
    """
    progress: dict = request.app[PROGRESS_KEY]
    chat_id = request.match_info["chat_id"]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "body JSON inválido"}, status=400)
    if not isinstance(body, dict) or not isinstance(body.get("message"), str):
        return web.json_response({"error": "message debe ser texto"}, status=400)
    message = body["message"].strip()
    if not message:
        return web.json_response({"error": "message vacío"}, status=400)
    db = request.app[DB_KEY]
    event = await db.conversation_event_for_chat(chat_id)
    if event:
        cid = event["conversation_id"]
        from . import conversation_access
        if not (await conversation_access.get_access(db, cid, identity.requester(request)))["can_send"]:
            return web.json_response({"error": "No tienes permiso para controlar esta tarea."}, status=403)
        state = await db.get_conversation_task(cid)
        if state.get("state") in task_service.STOPPED:
            return web.json_response({"error": "Continúa la tarea desde sus controles"}, status=409)
        key = body.get("request_id") or str(uuid.uuid4())
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            return web.json_response({"error": "request_id inválido"}, status=400)
        try:
            queued = await db.enqueue_conversation_event(cid, "steer:" + key, "run", {
                "user": message, "role": identity.role_of(request), "source": "steer",
                "requested_by": identity.requester(request),
            },
                authorized_actor=identity.requester(request))
        except PermissionError:
            return web.json_response({"error": "Conversación no disponible"}, status=404)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        task_service.start_pending(request.app, cid)
        return web.json_response({"queued": 1, "chat_id": queued["chat_id"],
                                  "phase": "queued", "durable": True})
    matches = [cid for cid in progress if cid.startswith(chat_id)]
    if not matches:
        return web.json_response(
            {"error": "no hay run con ese id (o el relay reinició)"},
            status=404)
    if len(matches) > 1:
        return web.json_response(
            {"error": "prefijo ambiguo", "matches": matches}, status=409)
    rp = progress[matches[0]]
    live_chat = await db.get_chat(matches[0])
    owner = (live_chat or {}).get("requested_by")
    if not owner or owner != identity.requester(request):
        return web.json_response(
            {"error": "Este run pertenece a otra persona o no tiene actor; inicia un nuevo turno."},
            status=409)
    if rp.finished:
        return web.json_response(
            {"error": "el run ya terminó — mandalo como mensaje normal",
             "finished": True}, status=409)
    rp.steer.append(message)
    logger.info("steer encolado para %s (%d en cola, fase %s)",
                matches[0], len(rp.steer), rp.phase)
    return web.json_response(
        {"queued": len(rp.steer), "chat_id": matches[0], "phase": rp.phase})
