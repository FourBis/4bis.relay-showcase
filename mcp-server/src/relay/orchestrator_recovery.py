"""Reanudación explícita de nodos cortados por presupuesto."""
from __future__ import annotations

import json

from . import grafo as G

_RESUME_NOTICE = (
    "## Continuación autorizada tras un límite de ejecución\n"
    "Continúa desde los archivos y resultados existentes. Inspecciona el estado "
    "actual, corrige los fallos y verifica los criterios pendientes. No repitas "
    "trabajo ya terminado ni efectos externos; si un efecto previo es incierto, "
    "pide la decisión correspondiente.\n\n"
)


async def prepare_graph_resume(db, graph: dict) -> list[str]:
    """La acción de retomar concede otro intento a los cortes por presupuesto.

    Conserva fallos funcionales, preguntas y padres sustituidos. El scheduler
    normal no llama a este helper: no se trata de un reintento automático.
    """
    nodes = [G.Nodo.desde_fila(t, t.get("deps") or ()) for t in graph["tasks"]]
    replaced = G.sustituidos(nodes)
    retry = {t["id"] for t in graph["tasks"]
             if t["id"] not in replaced and t["estado"] == G.FALLADO
             and G.es_error_de_presupuesto(t.get("error") or "")}
    if not retry:
        return []
    states = {n.id: G.PENDIENTE if n.id in retry else n.estado for n in nodes}
    by_id = {t["id"]: t for t in graph["tasks"]}
    reopened = set(retry)
    for tid in G.orden_topologico(nodes):
        deps = by_id[tid].get("deps") or ()
        if (states[tid] == G.BLOQUEADO and tid not in replaced
                and any(d in reopened for d in deps)
                and all(states[d] not in (G.FALLADO, G.BLOQUEADO) for d in deps)):
            states[tid] = G.PENDIENTE
            reopened.add(tid)
    projected = [G.Nodo.desde_fila(dict(t, estado=states[t["id"]]), t.get("deps") or ())
                 for t in graph["tasks"]]
    if not G.listas(projected) and not any(n.estado == G.CORRIENDO for n in projected):
        return []  # Otra dependencia aún impide ejecutar: conservar todo el estado.
    statements = []
    for tid in sorted(reopened):
        task = by_id[tid]
        detail = task.get("detalle") or ""
        if tid in retry:
            detail = _RESUME_NOTICE + detail.removeprefix(_RESUME_NOTICE)
        statements.append((
            "UPDATE tasks SET estado=?, error='', ended_at=NULL, detalle=?, "
            "max_intentos=MAX(max_intentos, intentos+1) WHERE id=? AND estado=?",
            (G.PENDIENTE, detail, tid, task["estado"])))
    from .db_support import now_iso
    statements.append((
        "UPDATE task_graphs SET estado='activo', updated_at=? WHERE id=?",
        (now_iso(), graph["id"])))
    await db.run_tx(statements)
    return sorted(reopened)


async def graph_resume_context(db, task: dict) -> tuple[str, str]:
    """Lee el historial anterior antes de asignar el chat del nuevo intento."""
    if not (task.get("detalle") or "").startswith(_RESUME_NOTICE) or not task.get("chat_id"):
        return "", ""
    rows = await db.run("SELECT payload FROM chat_outputs WHERE chat_id=?", (task["chat_id"],))
    history, context = "", ""
    try:
        data = json.loads(rows[0]["payload"]) if rows else {}
        context = str(data.get("content") or "")[-6000:]
        history = data.get("messages_json") or ""
        if not isinstance(history, str):
            history = ""
        if history:
            from pydantic_ai.messages import ModelMessagesTypeAdapter
            ModelMessagesTypeAdapter.validate_json(history)
    except (ValueError, TypeError, AttributeError):
        history = ""
    if not history:
        context = ("El intento anterior no conserva un historial de herramientas "
                   "reutilizable. Inspecciona los archivos y vuelve a comprobar "
                   "el estado antes de actuar.\n" + context)
    return history, context
