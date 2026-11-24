"""Admisión actual de una nueva invocación de modelo por conversación."""
from __future__ import annotations


def _active_durable_task(state):
    mode = state.get("mode")
    if (mode not in {"write", "read_only"}
            or state.get("state") not in {"ready", "running", "implemented", "validating", "publishing", "review"}
            or state.get("read_only") is not (mode == "read_only")):
        return False
    if state.get("no_checkout") is True:
        return mode == "read_only"
    return (all(isinstance(state.get(key), str) and state[key]
                for key in ("source_repo", "workspace_path"))
            and (mode != "write"
                 or isinstance(state.get("branch"), str) and bool(state["branch"])))


async def require_model_admission(db, conversation_id, *, write_required=False):
    """Recheck current actor and live conversation policy after preparation."""
    if db is None or not conversation_id:
        return
    from . import conversation_access, user_accounts
    from .task_service import STOPPED
    import json

    bound = user_accounts.current_actor.get()
    if not bound or bound[0].db is not db or not bound[1]:
        raise PermissionError("La ejecución requiere la cuenta que la solicitó")
    actor = bound[1]

    def check(conn):
        access = conversation_access.require_tx(conn, conversation_id, actor, "send")
        row = conn.execute("SELECT status,task_json FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        try:
            state = json.loads(row["task_json"] or "{}") if row else {}
        except (TypeError, ValueError):
            raise PermissionError("La política de la conversación no es válida") from None
        if (not row or not isinstance(state, dict)
                or state.get("state") in STOPPED or state.get("mode") == "external_pr"):
            raise PermissionError("La conversación está cerrada o la tarea está detenida")
        # Closing a managed conversation closes its view, not its accepted
        # FIFO. A legacy closed conversation has no durable task to continue.
        if row["status"] != "open" and not (row["status"] == "closed" and _active_durable_task(state)):
            raise PermissionError("La conversación está cerrada o la tarea está detenida")
        if (write_required or state.get("mode") == "write") and not access["can_collaborate"]:
            raise PermissionError("Ya no tienes permiso para ejecutar cambios en este proyecto")

    await conversation_access.transaction(db, check)
    if conversation_id in getattr(db, "_task_cancellations", {}):
        raise PermissionError("La cancelación de la tarea está en curso")
