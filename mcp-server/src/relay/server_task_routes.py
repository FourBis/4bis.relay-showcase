"""Estado y controles del trabajo persistente dentro de una conversación."""
from __future__ import annotations

import json
import hashlib
import asyncio
import time
import uuid

from aiohttp import web

from . import identity, task_pr, task_service, task_workspace
from .app_state import DB_KEY, GRAFOS_KEY
from .server_common import _require_auth
from .user_accounts import AccountError


async def _task_snapshot(request, db, cid):
    state = await task_service.snapshot(db, cid)
    if not state:
        return state
    conv = await db.get_conversation(cid)
    project = await db.get_project(conv["project_slug"]) if conv else None
    actions = []
    owner = identity.role_of(request) == "owner"
    if owner:
        actions = ["continue", "pause", "cancel", "publish", "track"]
    elif identity.can_write_project(request, project):
        actions = ["continue", "pause", "cancel"]
    if state.get("mode") == "read_only" and identity.can_write_project(request, project):
        actions.append("enable_write")
    visible = task_service.visible_task(state, owner=owner)
    return {**visible, "allowed_actions": actions}


@_require_auth
async def task_get(request):
    db, cid = request.app[DB_KEY], request.match_info["id"]
    conv = await db.get_conversation(cid)
    if not conv:
        return web.json_response({"error": "No existe la conversación"}, status=404)
    state = await db.get_conversation_task(cid)
    if state:
        project = await db.get_project(conv["project_slug"])
        if project:
            try:
                await task_workspace.inspect_workspace(db, project, cid)
            except (RuntimeError, OSError):
                pass  # el diagnóstico durable forma parte del estado
    return web.json_response(await _task_snapshot(request, db, cid))


def _bounded(body, key, default, lower, upper):
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise ValueError(f"{key} debe ser entero entre {lower} y {upper}")
    return value


async def _continue(db, project, cid, body):
    acknowledge = body.get("acknowledge_uncertain", False)
    if not isinstance(acknowledge, bool):
        raise ValueError("acknowledge_uncertain debe ser booleano")
    events = await db.list_conversation_events(cid, states=["processing"])
    if events:
        raise ValueError("la tarea sigue ejecutándose; espera al writer actual")
    initial = await db.get_conversation_task(cid)
    if initial.get("creation_failed") and not initial.get("mode"):
        await task_workspace.initialize_task(db, project, cid,
            read_only=bool(initial.get("requested_read_only")))
    await task_workspace.inspect_workspace(db, project, cid, fetch=True)
    await task_workspace.resolved_project(db, project, cid)
    events = await db.list_conversation_events(cid, states=["uncertain"])
    for event in events:
        state = await db.get_conversation_task(cid)
        if state.get("publish_uncertain"):
            pr = await task_pr.find_pr(state["workspace_path"], state["branch"])
            if not pr or pr["headRefOid"] != state.get("publish_head"):
                raise ValueError("La publicación sigue incierta: comprueba la PR en GitHub; no se repetirá create")
            await db.set_conversation_pr(cid, pr["url"])
            await db.update_conversation_task(cid, pr_url=pr["url"], publish_uncertain=False)
            if pr.get("state") != "OPEN":
                await db.finish_conversation_event(event["id"], state="applied")
                await db.cancel_pending_conversation_events(cid)
                await db.update_conversation_task(cid, state="finished", tracking={"enabled": False})
                return
        elif not acknowledge:
            chat = await db.get_chat(event.get("chat_id")) if event.get("chat_id") else None
            if event["kind"] == "publish" or not chat or chat.get("status") != "ok":
                raise ValueError("Revisa los archivos y el chat interrumpido; confirma acknowledge_uncertain para continuar sin repetirlo")
        await db.finish_conversation_event(event["id"], state="applied",
                                           error="Reconciliado al continuar; no se repitió")
    pending = await db.list_conversation_events(cid, states=["pending"])
    task = await db.get_conversation_task(cid)
    waiting_feedback = any(event["kind"] == "feedback" for event in pending) and not (task.get("tracking") or {}).get("enabled")
    await db.run("UPDATE conversations SET status='open', closed_at=NULL WHERE id=?", (cid,))
    if not await db.list_conversation_events(cid, states=["processing"]):
        await db.update_conversation_task(cid, state="ready", error=(
            "Hay feedback pendiente; reactiva el seguimiento para procesarlo" if waiting_feedback else ""))


@_require_auth
async def task_action(request):
    db, cid = request.app[DB_KEY], request.match_info["id"]
    if not hasattr(db, "_task_control_locks"):
        # ponytail: un proceso Relay; el executor protege sus fases en SQLite.
        db._task_control_locks = {}
    async with db._task_control_locks.setdefault(cid, asyncio.Lock()):
        return await _apply_task_action(request)


async def _apply_task_action(request):
    db, cid = request.app[DB_KEY], request.match_info["id"]
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("El body debe ser un objeto")
        conv = await db.get_conversation(cid)
        if not conv:
            return web.json_response({"error": "No existe"}, status=404)
        state = await db.get_conversation_task(cid)
        if not state:
            raise ValueError("Conversación histórica sin workspace de tarea; crea una tarea nueva")
        project = await db.get_project(conv["project_slug"])
        if not project or not project.get("enabled"):
            raise ValueError("El proyecto está deshabilitado o no existe")
        action = body.get("action")
        actions = (await _task_snapshot(request, db, cid)).get("allowed_actions", [])
        if action not in actions:
            return web.json_response({"error": "No tienes permiso para esta acción de la tarea."}, status=403)
        terminal = state.get("state") in {"cancelled", "finished", "cleaned"}
        runners = request.app.get(task_service.TASK_RUNNERS_KEY, {})
        writer_active = bool(runners.get(cid) and not runners[cid].done())
        processing = bool(await db.list_conversation_events(cid, states=["processing"]))
        if action == "enable_write":
            if terminal or writer_active or processing:
                raise ValueError("La tarea debe estar detenida y sin ejecuciones antes de habilitar escritura")
            graph = await db.active_task_graph(cid)
            if graph and graph["id"] in request.app.get(GRAFOS_KEY, {}):
                raise ValueError("Espera a que el plan deje de ejecutar antes de habilitar escritura")
            if await db.list_conversation_events(cid, states=["pending", "uncertain"]):
                raise ValueError("Reconcilia los eventos pendientes antes de habilitar escritura")
            await task_workspace.initialize_task(db, project, cid, promote=True)
        elif action == "pause":
            if terminal:
                raise ValueError("La tarea ya terminó")
            await db.update_conversation_task(cid, state="paused", tracking={"enabled": False})
        elif action == "cancel":
            if state.get("state") in {"finished", "cleaned"}:
                raise ValueError("La tarea ya terminó")
            await db.update_conversation_task(cid, state="cancelled", tracking={"enabled": False})
            await db.cancel_pending_conversation_events(cid)
            runner = request.app.get(task_service.TASK_RUNNERS_KEY, {}).get(cid)
            if runner:
                runner.cancel()
            graph = await db.active_task_graph(cid)
            if graph and (running := request.app.get(GRAFOS_KEY, {}).get(graph["id"])):
                running.cancel()
        elif action == "continue":
            if terminal:
                raise ValueError("La tarea ya terminó; el workspace se conserva para revisión")
            if writer_active or processing:
                raise ValueError("la tarea sigue ejecutándose; espera al writer actual")
            await _continue(db, project, cid, body)
            if (await db.get_conversation_task(cid)).get("state") == "ready":
                task_service.start_pending(request.app, cid)
        elif action == "publish":
            key = body.get("request_id")
            if not isinstance(key, str) or not 1 <= len(key) <= 128:
                raise ValueError("request_id inválido")
            if state.get("mode") != "write":
                raise ValueError("Una consulta no publica cambios")
            if await db.run("SELECT id FROM conversation_events WHERE conversation_id=? AND event_key=?",
                            (cid, "publish:" + key)):
                return web.json_response(await _task_snapshot(request, db, cid))
            if state.get("state") in task_service.STOPPED:
                raise ValueError("Reconcilia o continúa la tarea antes de publicar")
            if not writer_active and not processing:
                await db.update_conversation_task(cid, publish_allowed=True, state="ready")
            else:
                await db.update_conversation_task(cid, publish_allowed=True)
            await db.enqueue_conversation_event(
                cid, "publish:" + key, "publish",
                {"role": "owner", "requested_by": identity.requester(request)})
            if not writer_active and not processing:
                task_service.start_pending(request.app, cid)
        elif action == "track":
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("enabled debe ser booleano")
            if enabled and (state.get("state") in {"finished", "cancelled", "paused", "cleaned"} or
                            state.get("mode") != "write" or not conv.get("pr_url") or
                            not state.get("publish_allowed")):
                raise ValueError("El seguimiento necesita una PR de esta tarea y permiso de publicación")
            if enabled:
                tracking = {"enabled": True, "iterations": 0, "tokens": 0, "next_poll_at": 0,
                            "interval_s": _bounded(body, "interval_s", 60, 30, 3600),
                            "max_iterations": _bounded(body, "max_iterations", 3, 1, 20),
                            "max_tokens": _bounded(body, "max_tokens", 50000, 1000, 500000),
                            "expires_at": time.time() + 60 * _bounded(body, "duration_minutes", 60, 1, 1440)}
                key = body.get("request_id") or str(uuid.uuid4())
                if not isinstance(key, str) or not 1 <= len(key) <= 128:
                    raise ValueError("request_id inválido")
                receipt = {"enabled": True, "interval_s": tracking["interval_s"],
                           "max_iterations": tracking["max_iterations"],
                           "max_tokens": tracking["max_tokens"],
                           "duration_minutes": _bounded(body, "duration_minutes", 60, 1, 1440)}
                receipt_id = hashlib.sha256(key.encode()).hexdigest()
                previous = (state.get("tracking_receipts") or {}).get(receipt_id)
                if previous:
                    if previous.get("key") == key and previous.get("payload") != receipt:
                        raise ValueError("request_id ya usado con distinto payload")
                    if previous.get("key") == key:
                        return web.json_response(await _task_snapshot(request, db, cid))
                patch = {"tracking": tracking, "tracking_receipts": {receipt_id: {"key": key, "payload": receipt}},
                         "tracking_error": "", "tracking_stop": "", "error": ""}
                updated = await db.run("UPDATE conversations SET task_json=json_patch(task_json, ?) "
                    "WHERE id=? AND json_extract(task_json, ?) IS NULL RETURNING id",
                    (json.dumps(patch), cid, '$.tracking_receipts.' + receipt_id))
                if not updated:
                    current = await db.get_conversation_task(cid)
                    if current["tracking_receipts"][receipt_id]["payload"] != receipt:
                        raise ValueError("request_id ya usado con distinto payload")
            else:
                await db.update_conversation_task(cid, tracking={"enabled": False})
        else:
            raise ValueError("Acción desconocida")
        return web.json_response(await _task_snapshot(request, db, cid))
    except AccountError as exc:
        return web.json_response({"error": str(exc),
                                  "connect_url": "/admin/#/account",
                                  "conversation_id": cid}, status=422)
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        message = str(exc) if identity.role_of(request) == identity.OWNER_ROLE else (
            "No se pudo completar la acción. Revisa los permisos y el estado de la tarea.")
        return web.json_response({"error": message, "conversation_id": cid}, status=422)
