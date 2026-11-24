"""Autorización en base de datos para reanudar un grafo ya bajo sus controles."""
from __future__ import annotations

import json

from aiohttp import web

from . import identity
from .server_common import DB_KEY


async def _resume_access_denial(request, graph):
    """Control permite detener; retomar vuelve a comprobar permisos de ejecución."""
    cid = graph.get("conversation_id")
    if not cid:
        return None  # Los grafos históricos conservan su autorización existente.
    from . import conversation_access
    db, actor = request.app[DB_KEY], identity.requester(request)
    def check(conn):
        current = conn.execute("SELECT conversation_id,project_slug FROM task_graphs WHERE id=?",
                               (graph["id"],)).fetchone()
        if (not current or current["conversation_id"] != cid
                or current["project_slug"] != graph.get("project_slug")):
            raise PermissionError("El origen del grafo cambió")
        access = conversation_access.require_tx(conn, cid, actor, "control")
        if not conn.execute("SELECT 1 FROM projects WHERE slug=? COLLATE NOCASE",
                            (current["project_slug"],)).fetchone():
            return  # El handler conserva el conflicto de proyecto eliminado.
        row = conn.execute("SELECT task_json FROM conversations WHERE id=?", (cid,)).fetchone()
        state = json.loads(row["task_json"] or "{}")
        if not access["can_send"] or state.get("mode") == "write" and not access["can_collaborate"]:
            raise PermissionError("La ejecución ya no está autorizada")
    try:
        await conversation_access.transaction(db, check)
    except PermissionError:
        return web.json_response({"error": "No tienes permiso para reanudar este plan."}, status=403)
