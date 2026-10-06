"""Reanudación explícita por presupuesto y corrección de criterios pendientes."""
from __future__ import annotations

import json
import uuid

from . import grafo as G
from .expert_verdicts import _validate_plan_correction

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


def _criteria(steps: list[dict], *, done: bool) -> set[str]:
    return {" ".join((step["description"] + " " + step["expected_output"]).split()).casefold()
            for step in steps if (step["status"] == "done") == done}


async def continue_verification(db, graph_id: str, graph: dict, verdict: dict,
                                *, allow_continue: bool = True) -> bool:
    """Una tarea nueva, sin repetir las anteriores ni reinterpretar sus efectos.

    Solo se corrige un grafo cuyos nodos cerraron bien. Los fallos de ejecución,
    permisos, cancelación o una pregunta siguen su circuito de recuperación.
    Una nueva pasada exige que el verificador dé por resuelto algún criterio
    pendiente anterior. Cambiar la redacción del feedback no alcanza.
    """
    if (graph.get("estado") == "cancelado" or verdict.get("error")
            or verdict.get("verdict") != "needs_more"):
        return False
    nodes = [G.Nodo.desde_fila(t, t.get("deps") or ()) for t in graph["tasks"]]
    if G.estado_del_grafo(nodes) != "hecho":
        return False

    correction = verdict.get("plan_correction")
    valid = isinstance(correction, dict)
    if valid:
        try:
            valid = _validate_plan_correction(correction) is None
            steps = correction["revised_steps"]
            valid = valid and all(isinstance(s.get(k), str) and s[k].strip()
                                  for s in steps for k in ("id", "description", "expected_output"))
        except (KeyError, TypeError, ValueError):
            valid = False
    pending = _criteria(steps, done=False) if valid else set()
    try:
        previous = json.loads(graph.get("verificacion_json") or "{}")
        recovery = previous.get("recovery") or {}
    except (TypeError, ValueError, AttributeError):
        recovery = {}
    prior = set(recovery.get("pending") or ())
    done = _criteria(steps, done=True) if valid else set()
    progressed = not prior or bool(prior & done)
    seen = {tuple(items) for items in recovery.get("seen") or []}
    signature = tuple(sorted(pending))
    proceed = bool(allow_continue and pending and progressed and signature not in seen)

    task_id = f"{graph_id}:verify:{uuid.uuid4().hex[:8]}"
    detail = ("Continúa el objetivo del grafo desde el estado actual del workspace. "
              "Conserva lo terminado; inspecciona lo existente antes de modificar. "
              "Corrige la causa de estos fallos y ejecuta sus comprobaciones reales. "
              "No repitas migraciones, publicaciones ni otros efectos ya realizados; "
              "si un efecto previo es incierto, pide la decisión correspondiente.\n\n")
    if valid:
        detail += correction["feedback_to_executor"][:2000] + "\n\n"
        detail += "\n".join(f"- {s['description']}: {s['expected_output']}"
                            for s in steps if s["status"] != "done")
    else:
        detail += str(verdict.get("feedback") or "Falta una corrección verificable.")
    if proceed:
        seen.add(signature)
        verdict["recovery"] = {"pending": sorted(pending), "seen": sorted(seen)}
    else:
        verdict["recovery"] = recovery
    reason = ("Se alcanzó el límite global de vueltas del grafo. " if not allow_continue
              else "La verificación repite criterios sin avance comprobado. " if valid
              else "El verificador no entregó una corrección válida. ")
    # ponytail: un nodo independiente con ID local, sin aristas del modelo.
    # Alta + checkpoint + estado deben sobrevivir juntos a reinicios/cancelación.
    from .db_support import now_iso
    await db.run_tx([
        ("INSERT INTO tasks (id, graph_id, titulo, detalle, idempotente, max_intentos, "
         "orden, estado, error) VALUES (?,?,?,?,0,1,?,?,?)",
         (task_id, graph_id, "Corregir criterios pendientes de la verificación final",
          detail[:8000], max(int(t.get("orden") or 0) for t in graph["tasks"]) + 1,
          G.PENDIENTE if proceed else G.FALLADO,
          "" if proceed else reason + str(verdict.get("feedback") or "")[:1200])),
        ("UPDATE task_graphs SET verificacion_json=?, estado=?, updated_at=? WHERE id=?",
         (json.dumps(verdict, ensure_ascii=False), "activo" if proceed else "fallado",
          now_iso(), graph_id)),
    ])
    return proceed
