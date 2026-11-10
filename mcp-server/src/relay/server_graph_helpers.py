"""Server domain handlers extracted from the composition entrypoint."""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Optional

from aiohttp import web

from .server_common import BG_TASKS_KEY, DB_KEY, GRAFOS_KEY, NOTIFY_KEY, PROGRESS_KEY, _spawn_bg, logger
from . import coordination
from . import experts
from . import grafo as grafo_mod
from . import logctx
from . import orquestador
from .orchestrator_verification import (
    verification_close_state, verification_interrupt_intent, verification_pending,
    verification_signature,
)
from .server_graph_projection import (
    _CAP_NODOS_SINTETICOS, _PHASE_FALLIDO, _STATUS_FALLIDO,
    _costo_en_nodos, _detalle_del_turno, _estado_del_paso, _estado_del_turno,
    _grafo_publico, _grafo_sintetico, _stages_de, _tiene_pregunta_abierta,
    _titulo_de_respaldo, _verificacion_publica,
)


async def _correr_grafo_bg(app: web.Application, project: dict,
                           graph_id: str, *, verification_only: bool = False) -> None:
    """Corre el grafo entero fuera del request. Nunca lanza."""
    from . import orquestador

    db = app[DB_KEY]
    logctx.bind(graph_id, project.get("slug") or "")
    # Cada nodo reporta como un run del chat (2026-08-24). Sin esto los
    # nodos del grafo no reportaban a NADIE: `/experts/status/{chat_id}`
    # contestaba "no hay run con ese id" y el panel no podía decir en qué
    # herramienta estaba. Medido el 24/8: veinte minutos de un nodo
    # trabajando —escribiendo PNGs— sin una sola señal en pantalla, y lo
    # dimos por colgado.
    #
    # Es una factory porque `make_progress_callback` registra el
    # RunProgress bajo un `chat_id` y el chat de cada nodo se crea dentro
    # del ejecutor. Ver `orquestador.ejecutor_minimax`.
    experts.make_progress_callback(
        store=app[PROGRESS_KEY], notify=None, chat_id=graph_id,
        target=project.get("slug") or "", model="")
    graph_progress = app[PROGRESS_KEY][graph_id]
    graph_progress.phase = "graph"
    graph_progress.graph_id = graph_id

    def progreso_de(chat_id: str):
        # Esta factory corre dentro de la task del nodo, no del padre.
        logctx.bind(chat_id, project.get("slug") or "")
        callback = experts.make_progress_callback(
            # No authenticated Discord audience is bound to graph results.
            store=app[PROGRESS_KEY], notify=None,
            chat_id=chat_id, target=project.get("slug") or "",
            model=(project.get("defaults_json") or {}).get("model") or "")
        rp = app[PROGRESS_KEY][chat_id]
        rp.graph_id = graph_id
        task = asyncio.current_task()
        if task is not None:
            def terminado(_task):
                rp.finished = True
                graph_progress.last_activity_at = time.monotonic()

            task.add_done_callback(terminado)
        return callback

    try:
        prog = await orquestador.lanzar(db, project, graph_id,
                                        progreso_de=progreso_de,
                                        **({"verification_only": True} if verification_only else {}))
        logger.info("grafo %s terminó: %s", graph_id, prog.get("estado"))
    except asyncio.CancelledError:
        logger.info("grafo %s cancelado", graph_id)
        raise
    except Exception:  # noqa: BLE001 — un grafo roto no voltea el relay
        logger.exception("el grafo %s se cayó", graph_id)
        if not verification_only:
            with contextlib.suppress(Exception):
                await db.set_task_graph_state(graph_id, "fallado")
        # A failed close-only checkpoint must retain its existing pending or
        # cancelled state. Do not overwrite a newer cancellation or pretend
        # that a verifier result was confirmed.
    finally:
        graph_progress.finished = True


def _graph_control_lock(app: web.Application, graph_id: str):
    db = app[DB_KEY]
    if not hasattr(db, "_graph_control_locks"):
        db._graph_control_locks = {}
    return db._graph_control_locks.setdefault(graph_id, asyncio.Lock())


def _grafo_en_curso(app: web.Application, graph_id: str):
    return (app[GRAFOS_KEY].get(graph_id)
            or getattr(app[DB_KEY], "_graph_cancellations", {}).get(graph_id))


async def _cancelar_grafo(app: web.Application, graph_id: str) -> None:
    db = app[DB_KEY]
    if not hasattr(db, "_graph_cancellations"):
        # ponytail: un proceso; usar reserva persistente al distribuir Relay.
        db._graph_cancellations = {}
    pending = db._graph_cancellations
    finalizer = pending.get(graph_id)
    if finalizer is None:
        async def finish_cancel():
            async with _graph_control_lock(app, graph_id):
                await _finish_graph_cancel(app, graph_id)

        # Conserva el worker original para los controles de tarea; la
        # reserva adicional bloquea reanudaciones hasta persistir el cierre.
        finalizer = _spawn_bg(finish_cancel(), hold_workspace=False)
        finalizer.relay_actor = getattr(app[GRAFOS_KEY].get(graph_id), "relay_actor", None)
        pending[graph_id] = finalizer
        finalizer.add_done_callback(lambda _: pending.pop(graph_id, None))
    await asyncio.shield(finalizer)


async def _finish_graph_cancel(app: web.Application, graph_id: str) -> None:
    """El caller conserva el lock del grafo y protege el cierre de desconexiones."""
    task = app[GRAFOS_KEY].get(graph_id)
    was_live = task is not None and not task.done()
    if task is not None:
        asyncio.current_task().relay_actor = getattr(task, "relay_actor", None)
        if not task.cancelling():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if (was_live and task.cancelled()
            and getattr(task, "relay_verifier_enabled", None) is True):
        # Cancellation may land after the last terminal node but before the
        # verifier persisted its intent. Only this observed live worker and
        # its actual enabled policy can establish that missing intent.
        graph = await app[DB_KEY].get_task_graph(graph_id)
        intent = verification_interrupt_intent(graph) if graph else None
        if intent is not None:
            await app[DB_KEY].set_task_graph_verificacion(
                graph_id, json.dumps(intent, ensure_ascii=False))
    await app[DB_KEY].set_task_graph_state(graph_id, "cancelado")


def _largar_grafo(app: web.Application, project: dict, graph_id: str,
                  actor_email: str | None = None, *,
                  verification_only: bool = False) -> None:
    from . import user_accounts
    grafos: dict = app[GRAFOS_KEY]
    current = user_accounts.current_actor.get()
    actor = actor_email or (current[1] if current else None)
    async def in_workspace():
        from . import task_service, task_workspace
        db = app[DB_KEY]
        graph = await db.get_task_graph(graph_id)
        effective = project
        if graph and graph.get("conversation_id"):
            task_state = await db.get_conversation_task(graph["conversation_id"])
            if task_state.get("state") in task_service.STOPPED:
                raise RuntimeError("La tarea está detenida; continúa desde sus controles")
            effective = await task_workspace.resolved_project(db, project, graph["conversation_id"])
        owner = asyncio.current_task()
        if owner is not None and not verification_only:
            owner.relay_verifier_enabled = bool(
                (effective.get("defaults_json") or {}).get("graph_verifier", True))
        if verification_only:
            from . import conversation_access
            if not actor or graph is None:
                raise PermissionError("No hay una identidad autorizada para verificar este plan")
            if (graph.get("conversation_id") and not
                    (await conversation_access.get_access(db, graph["conversation_id"], actor))["can_control"]):
                raise PermissionError("Ya no tienes permiso para verificar este plan")
        from .graph_resume_inspection import suspend_resume_inspection
        with user_accounts.bind_actor(db, actor), suspend_resume_inspection():
            child = coordination.spawn_workspace(
                db, effective, _correr_grafo_bg(
                    app, effective, graph_id,
                    **({"verification_only": True} if verification_only else {})))
            child.relay_graph_owner = owner
            return await child
    task = asyncio.create_task(in_workspace())
    task.relay_actor = actor
    task.relay_verifier_enabled = None
    coordination.hold_current(task)
    grafos[graph_id] = task
    bg_tasks: set = app[BG_TASKS_KEY]
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)
    task.add_done_callback(lambda _t: grafos.pop(graph_id, None))


# Proyectos con el planificador corriendo ahora mismo. La guarda de
# `graphs_create` mira `task_graphs`, y esa fila recién existe cuando el
# planificador vuelve (1-2 min): durante todo ese rato el proyecto queda
# sin reservar y un segundo POST pasa limpio. Dos pedidos sobre el
# mismo proyecto y archivo pueden solaparse si un cliente corta por
# timeout y reintentó. En memoria alcanza porque el relay es UN proceso;
# si algún día son varios, esto tiene que ser una fila con TTL como las
# reservas de archivo.
# ponytail: tareas en memoria, no reserva persistida. Upgrade cuando el
# relay corra en más de un proceso.
_PLANIFICANDO: dict[str, asyncio.Task] = {}
