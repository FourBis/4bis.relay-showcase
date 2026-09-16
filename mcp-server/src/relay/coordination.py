"""Exclusión por working tree durante HTTP y el trabajo que deja en fondo.

El relay tiene un proceso: la reserva se adquiere sin awaits y se conserva
hasta que terminan todos los hijos registrados, incluida la finalización.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

from aiohttp import web


_current: ContextVar = ContextVar("relay_workspace_lease", default=None)


def workspace_key(project: dict) -> str:
    repo = project.get("repo_path")
    return (os.path.normcase(str(Path(repo).resolve())) if repo
            else "project:" + project["slug"].casefold())


class WorkspaceBusy(RuntimeError):
    pass


class Lease:
    def __init__(self, registry, key):
        self.registry, self.key = registry, key
        self.refs = 1
        self.done = asyncio.Event()
        registry[key] = self

    def release(self):
        self.refs -= 1
        if self.refs == 0:
            self.registry.pop(self.key, None)
            self.done.set()

    def hold(self, task):
        self.refs += 1
        task.add_done_callback(lambda _: self.release())


def _registry(db):
    if not hasattr(db, "_workspace_leases"):
        db._workspace_leases = {}
    return db._workspace_leases


def busy(db, project):
    return workspace_key(project) in _registry(db)


def acquire(db, project):
    registry = _registry(db)
    key = workspace_key(project)
    if key in registry:
        raise WorkspaceBusy("El proyecto tiene trabajo en curso; espera a que termine.")
    return Lease(registry, key)


def hold_current(task):
    lease = _current.get()
    if lease is not None and not lease.done.is_set():
        lease.hold(task)
    return task


def spawn_workspace(db, project, coro):
    """Los grafos heredan la reserva del turno; reanudaciones externas esperan."""
    lease = _current.get()
    if (lease is not None and not lease.done.is_set()
            and lease.key == workspace_key(project)):
        return hold_current(asyncio.create_task(coro))

    async def run():
        started = False
        try:
            while busy(db, project):
                await _registry(db)[workspace_key(project)].done.wait()
            owned = acquire(db, project)
            token = _current.set(owned)
            try:
                started = True
                return await coro
            finally:
                _current.reset(token)
                owned.release()
        finally:
            if not started:
                coro.close()
    return asyncio.create_task(run())


async def _quien_ocupa(db, project, cuerpo: dict) -> None:
    """Agrega al 409 el id del trabajo que está ocupando el workspace.

    Un 409 que no dice CUÁL es ese trabajo es un "no" a secas. Los
    handlers viejos devolvían el id justamente para que el cliente
    ofreciera la salida: el bot arma `POST /graphs/{graph_id}/cancel` y
    el panel linkea la conversación abierta. La reserva no lo sabe
    —solo conoce el working tree—, así que hay que preguntárselo a la
    base.

    Best-effort a propósito: si la consulta falla, el 409 sigue siendo
    la respuesta correcta, apenas menos útil. Un rechazo no se convierte
    en un 500 porque no pudimos adornarlo.
    """
    slug = project.get("slug")
    for clave, getter in (("graph_id", "active_task_graph_by_project"),
                          ("conversation_id", "get_open_conversation_for_project")):
        fn = getattr(db, getter, None)
        if fn is None:
            continue
        try:
            fila = await fn(slug)
        except Exception:  # noqa: BLE001 — adorno, no puede romper el 409
            continue
        if not fila:
            continue
        cuerpo[clave] = fila["id"]
        if clave == "conversation_id" and fila.get("branch"):
            cuerpo["branch"] = fila["branch"]


def guard_workspace(db_key, *, source="body"):
    """Valida el proyecto, reserva y conserva el contrato HTTP del handler."""
    def decorate(handler):
        @wraps(handler)
        async def guarded(request):
            db = request.app[db_key]
            if source == "body":
                try:
                    body = await request.json()
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return web.json_response({"error": "json inválido"}, status=400)
                if not isinstance(body, dict):
                    return web.json_response({"error": "el body debe ser un objeto"}, status=400)
                # Los handlers NO resuelven el proyecto igual que este
                # guard: `/conversations` y `/night-mode/start` miran
                # solo `project`, `/graphs` hace `project or target` —al
                # revés que acá— y `/experts/run` usa `target`. Con los
                # dos campos distintos, el guard reservaba un workspace y
                # el handler trabajaba sobre otro: la exclusión cubría el
                # repo equivocado y entraban dos conversaciones al mismo.
                #
                # Se corta acá en vez de unificar las precedencias porque
                # mientras haya dos resoluciones van a volver a divergir.
                # Sin contradicción, cualquier orden da el mismo slug.
                destino = (body.get("target") or "").strip() if isinstance(
                    body.get("target"), str) else ""
                propio = (body.get("project") or "").strip() if isinstance(
                    body.get("project"), str) else ""
                if destino and propio and destino != propio:
                    return web.json_response(
                        {"error": "target y project no coinciden",
                         "message": ("Envía uno solo: con los dos distintos "
                                     "no se puede saber sobre qué repo se "
                                     "reserva ni sobre cuál se trabaja."),
                         "target": destino, "project": propio}, status=400)
                slug = destino or propio
            else:
                getter = db.get_task_graph if source == "graph" else db.get_conversation
                row = await getter(request.match_info["id"])
                slug = row.get("project_slug") if row else None
            project = await db.get_project(slug.strip()) if isinstance(slug, str) else None
            if not project:
                return await handler(request)
            try:
                lease = acquire(db, project)
            except WorkspaceBusy as e:
                cuerpo = {"error": "workspace_busy", "message": str(e),
                          "project": project["slug"]}
                await _quien_ocupa(db, project, cuerpo)
                return web.json_response(cuerpo, status=409)
            token = _current.set(lease)
            try:
                return await handler(request)
            finally:
                _current.reset(token)
                lease.release()
        return guarded
    return decorate
