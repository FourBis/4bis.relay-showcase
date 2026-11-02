"""Inspecciones de workspace ligadas a una reanudación explícita de grafo."""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from . import conversation_access

_scope = ContextVar("relay_graph_resume_inspection", default=None)
_FIELDS = frozenset({"workspace_state", "workspace_error", "head_sha", "remote_sha",
                     "state", "error", "validation"})


class ResumeInspectionDenied(PermissionError):
    """La cuenta ya no puede ejecutar esta inspección/reanudación."""


class ResumeInspectionStale(RuntimeError):
    """La observación pertenece a otra versión; se conserva el estado actual."""


def _contract(project):
    return (str(project.get("slug") or "").casefold(),
            str(project.get("_task_source_repo") or project.get("repo_path") or ""),
            bool(project.get("enabled", True)),
            json.dumps(project.get("defaults_json") or {}, ensure_ascii=False, sort_keys=True))


@dataclass(frozen=True)
class _Scope:
    db: object
    graph_id: str
    conversation_id: str
    project_slug: str
    actor: str

    def snapshot(self, conn, project_contract, stopped):
        graph = conn.execute("SELECT conversation_id,project_slug,estado FROM task_graphs WHERE id=?",
                             (self.graph_id,)).fetchone()
        if (not graph or graph["conversation_id"] != self.conversation_id
                or graph["project_slug"] != self.project_slug):
            raise ResumeInspectionDenied("El origen del grafo cambió")
        try:
            access = conversation_access.require_tx(conn, self.conversation_id, self.actor, "control")
        except PermissionError:
            raise ResumeInspectionDenied("La ejecución ya no está autorizada") from None
        conv = conn.execute("SELECT project_slug,requested_by,status,task_json FROM conversations WHERE id=?",
                            (self.conversation_id,)).fetchone()
        if not conv or str(conv["project_slug"]).casefold() != str(self.project_slug).casefold():
            raise ResumeInspectionDenied("El origen de la conversación cambió")
        current = conn.execute("SELECT * FROM projects "
                               "WHERE slug=? COLLATE NOCASE", (self.project_slug,)).fetchone()
        if not current:
            raise ResumeInspectionStale("El proyecto ya no está disponible")
        state = json.loads(conv["task_json"] or "{}")
        if (state.get("mode") == "external_pr" or not access["can_send"]
                or state.get("mode") == "write" and not access["can_collaborate"]):
            raise ResumeInspectionDenied("La ejecución ya no está autorizada")
        if state.get("state") in stopped:
            raise ResumeInspectionStale("La tarea está detenida")
        fresh = self.db._parse_project(dict(current))
        if _contract(fresh) != project_contract:
            raise ResumeInspectionStale("La identidad o política del proyecto cambió")
        version = ((graph["conversation_id"], graph["project_slug"], graph["estado"]),
                   (conv["project_slug"], conv["requested_by"], conv["status"], conv["task_json"]),
                   (current["id"], current["slug"], current["repo_path"],
                    current["enabled"], current["defaults_json"]))
        return _Observation(self, project_contract, version, conv["task_json"])


@dataclass(frozen=True)
class _Observation:
    scope: _Scope
    project_contract: tuple
    version: tuple
    raw_task: str | None

    @property
    def task(self):
        return json.loads(self.raw_task or "{}")

    async def persist(self, fields):
        """Reautoriza y fusiona sólo el diagnóstico sobre la versión observada."""
        if not isinstance(fields, dict) or set(fields) - _FIELDS:
            raise ResumeInspectionStale("La inspección contiene campos ajenos al diagnóstico")
        from .task_service import STOPPED
        stopped = frozenset(STOPPED)
        patch = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        def apply(conn):
            current = self.scope.snapshot(conn, self.project_contract, stopped)
            if current.version != self.version:
                raise ResumeInspectionStale("La tarea cambió durante la inspección")
            # Nunca se serializa el task observado como reemplazo completo.
            row = conn.execute(
                "UPDATE conversations SET task_json=json_patch(COALESCE(task_json,'{}'), ?) "
                "WHERE id=? AND task_json IS ? RETURNING task_json",
                (patch, self.scope.conversation_id, self.raw_task)).fetchone()
            if row is None:
                raise ResumeInspectionStale("La tarea cambió durante la inspección")
            return json.loads(row[0] or "{}")
        return await conversation_access.transaction(self.scope.db, apply, write=True)


async def open_inspection(db, project, conversation_id):
    scope = _scope.get()
    if scope is None:
        return None
    if scope.db is not db or scope.conversation_id != conversation_id:
        raise ResumeInspectionDenied("La inspección pertenece a otra conversación")
    from .task_service import STOPPED
    contract, stopped = _contract(project), frozenset(STOPPED)
    return await conversation_access.transaction(db, lambda conn: scope.snapshot(conn, contract, stopped))


@contextmanager
def resume_inspection_scope(db, graph, actor):
    cid = graph.get("conversation_id")
    scope = _Scope(db, graph["id"], cid, graph.get("project_slug"), actor) if cid else None
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


@contextmanager
def suspend_resume_inspection():
    token = _scope.set(None)
    try:
        yield
    finally:
        _scope.reset(token)
