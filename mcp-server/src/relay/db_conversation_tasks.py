"""Persistencia mínima de tareas y eventos por conversación."""
from __future__ import annotations

import json
import uuid
import time
from typing import Any

from .db_support import _CAP_PROMPT_FILA, now_iso


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    try:
        row["payload"] = json.loads(row.get("payload") or "{}")
    except (TypeError, json.JSONDecodeError):
        row["payload"] = {}
    return row


def _next_pending(conn, conv_id, state):
    from .task_service import tracking_limit
    try:
        feedback_allowed = not tracking_limit(state)
    except (TypeError, ValueError, AttributeError):
        feedback_allowed = False
    return conn.execute(
        "SELECT * FROM conversation_events WHERE conversation_id=? AND state='pending' "
        "AND (kind<>'feedback' OR ?) ORDER BY id LIMIT 1", (conv_id, feedback_allowed)).fetchone()


class DatabaseConversationTasksMixin:
    async def get_conversation_task(self, conv_id: str) -> dict[str, Any]:
        rows = await self.run("SELECT task_json FROM conversations WHERE id=?", (conv_id,))
        if not rows:
            raise KeyError(f"conversation inexistente: {conv_id}")
        try:
            value = json.loads(rows[0].get("task_json") or "{}")
        except json.JSONDecodeError:
            value = {}
        return value if isinstance(value, dict) else {}

    async def update_conversation_task(self, conv_id: str, **fields: Any) -> dict[str, Any]:
        patch = _json(fields)

        def work() -> dict[str, Any]:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "UPDATE conversations SET task_json=json_patch(COALESCE(task_json,'{}'), ?) "
                    "WHERE id=? RETURNING task_json", (patch, conv_id)).fetchone()
                if row is None:
                    raise KeyError(f"conversation inexistente: {conv_id}")
                conn.commit()
                value = json.loads(row[0] or "{}")
                return value if isinstance(value, dict) else {}
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        return await self._in_thread(work)

    async def enqueue_conversation_event(
        self, conv_id: str, event_key: str, kind: str, payload: dict[str, Any],
        *, authorized_actor: str | None = None,
    ) -> dict[str, Any]:
        if kind not in {"run", "feedback", "publish"}:
            raise ValueError("kind inválido")
        if kind in {"run", "feedback"} and not isinstance(payload.get("user"), str):
            raise ValueError("payload.user requerido")
        encoded = _json(payload)

        def work() -> dict[str, Any]:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                mode = conn.execute("SELECT json_extract(task_json,'$.mode') FROM conversations WHERE id=?", (conv_id,)).fetchone()
                if authorized_actor is not None:
                    from .conversation_access import require_tx
                    require_tx(conn, conv_id, authorized_actor, "send" if kind == "run" else "control")
                if mode and mode[0] == "external_pr":
                    raise ValueError("Un PR externo no ejecuta eventos de código")
                existing = conn.execute(
                    "SELECT * FROM conversation_events WHERE conversation_id=? AND event_key=?",
                    (conv_id, event_key)).fetchone()
                if existing is not None:
                    item = dict(existing)
                    if item["kind"] != kind or _json(json.loads(item["payload"] or "{}")) != encoded:
                        raise ValueError("event_key ya existe con distinto payload")
                    conn.commit()
                    return _event(item)
                if kind == "run":
                    from .conversation_creation import require_enqueue_tx
                    require_enqueue_tx(conn, conv_id, payload.get("expected_creation"))
                if conn.execute("SELECT 1 FROM conversations WHERE id=?", (conv_id,)).fetchone() is None:
                    raise KeyError(f"conversation inexistente: {conv_id}")
                chat_id = None
                if kind in {"run", "feedback"}:
                    chat_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{conv_id}/{event_key}"))
                    conn.execute(
                        "INSERT INTO chats (id, project_slug, source, author, target, started_at, "
                        "status, conversation_id, user_prompt, requested_by) SELECT ?, project_slug, ?, ?, ?, ?, "
                        "'queued', id, ?, ? FROM conversations WHERE id=?",
                        (chat_id, payload.get("source") or "task", payload.get("author"),
                         payload.get("target"), now_iso(), payload["user"][:_CAP_PROMPT_FILA],
                         payload.get("requested_by"), conv_id))
                cur = conn.execute(
                    "INSERT INTO conversation_events "
                    "(conversation_id,event_key,kind,payload,state,chat_id,created_at) "
                    "VALUES (?,?,?,?, 'pending', ?, ?)",
                    (conv_id, event_key, kind, encoded, chat_id, now_iso()))
                row = conn.execute("SELECT * FROM conversation_events WHERE id=?", (cur.lastrowid,)).fetchone()
                conn.commit()
                return _event(dict(row))
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        return await self._in_thread(work)

    async def next_conversation_event(self, conv_id: str) -> dict[str, Any] | None:
        def work():
            conn = self._connect()
            try:
                conn.execute("BEGIN")
                row = conn.execute("SELECT task_json FROM conversations WHERE id=?", (conv_id,)).fetchone()
                state = json.loads(row[0] or "{}") if row else {}
                event = _next_pending(conn, conv_id, state)
                return _event(event) if event else None
            finally:
                conn.close()
        return await self._in_thread(work)

    async def claim_conversation_event(self, conv_id: str, *, claimed: dict | None = None,
                                       expected_event_id: int | None = None) -> dict[str, Any] | None:
        from .task_service import STOPPED
        def work() -> dict[str, Any] | None:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT task_json FROM conversations WHERE id=?", (conv_id,)).fetchone()
                state = json.loads(row[0] or "{}") if row else {}
                if isinstance(state, dict) and state.get("mode") == "external_pr":
                    conn.rollback()
                    return None
                if isinstance(state, dict) and state.get("state") in STOPPED:
                    conn.rollback()
                    return None
                if conn.execute("SELECT 1 FROM conversation_events WHERE conversation_id=? AND state IN ('processing','uncertain') LIMIT 1", (conv_id,)).fetchone():
                    conn.rollback()
                    return None
                row = _next_pending(conn, conv_id, state)
                if row is None or (expected_event_id is not None and row["id"] != expected_event_id):
                    conn.rollback()
                    return None
                updated = conn.execute(
                    "UPDATE conversation_events SET state='processing' WHERE id=? RETURNING *", (row["id"],)).fetchone()
                if updated["chat_id"]:
                    conn.execute("UPDATE chats SET status='running', started_at=COALESCE(started_at,?) WHERE id=?", (now_iso(), updated["chat_id"]))
                conn.commit()
                event = _event(dict(updated))
                if claimed is not None:
                    # _in_thread waits for the commit before propagating cancellation.
                    # Keep its result available even when the await cannot return it.
                    claimed["event"] = event
                return event
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        return await self._in_thread(work)

    async def reserve_feedback_execution(self, conv_id: str, event_id: int, *, start=True, deadline=None,
                                         write_required=False, private_required=False,
                                         admitted: dict | None = None) -> dict | None:
        """Recheck the exact claimed feedback immediately before its executor.

        A known skip changes neither tracking counters nor task phase. A valid
        reservation increments the iteration and enters running atomically.
        The optional server-owned observer publishes commit outcome inside the
        worker, even when cancellation prevents its await from returning.
        """
        from .task_service import STOPPED, tracking_limit
        def work():
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                event = conn.execute("SELECT * FROM conversation_events WHERE id=? AND conversation_id=? "
                    "AND kind='feedback' AND state='processing'", (event_id, conv_id)).fetchone()
                if event is None:
                    conn.rollback()
                    return None
                row = conn.execute("SELECT task_json FROM conversations WHERE id=?", (conv_id,)).fetchone()
                try:
                    state = json.loads(row[0] or "{}") if row else {}
                    reason = ("La tarea fue detenida" if state.get("state") in STOPPED
                              or state.get("mode") == "external_pr" else tracking_limit(state))
                except (TypeError, ValueError, AttributeError):
                    reason = "Política de seguimiento inválida"
                if not reason and deadline is not None and time.monotonic() >= deadline:
                    reason = "Se cumplió el plazo absoluto del pedido"
                payload = _event(event)["payload"]
                if not reason and any(part in event["event_key"] for part in (":ci:", ":status:", ":review:", ":review_comment:")) and payload.get("head_sha") != state.get("head_sha"):
                    reason = "Feedback de un commit anterior"
                if not reason:
                    from .conversation_access import require_tx
                    try:
                        access = require_tx(conn, conv_id, payload.get("requested_by"), "send")
                        if (state.get("mode") == "write" or write_required) and not access["can_collaborate"]:
                            reason = "El proyecto o la cuenta ya no permiten ejecutar escritura"
                        elif (state.get("mcp_private") or private_required) and (
                                state.get("mode") != "read_only" or not access["project_assigned"]):
                            reason = "La consulta MCP privada ya no está autorizada para este proyecto"
                    except PermissionError:
                        reason = "La cuenta que pidió el feedback ya no tiene acceso a esta conversación"
                if reason:
                    error = "Feedback automático omitido antes del ejecutor: " + reason
                    changed = conn.execute("UPDATE conversation_events SET state='cancelled',finished_at=?,error=? "
                        "WHERE id=? AND conversation_id=? AND state='processing'", (now_iso(), error, event_id, conv_id))
                    if changed.rowcount and event["chat_id"]:
                        conn.execute("UPDATE chats SET status='cancelled',finished_at=?,error=? WHERE id=? "
                            "AND conversation_id=? AND status IN ('queued','running')", (now_iso(), error, event["chat_id"], conv_id))
                    conn.commit()
                    return {"execute": False, "reason": reason}
                tracking = state["tracking"]
                if not start:
                    conn.commit()
                    return {"execute": True, "tracking": tracking}
                patch = _json({"state": "running", "error": "",
                               "tracking": {"iterations": tracking.get("iterations", 0) + 1}})
                conn.execute("UPDATE conversations SET task_json=json_patch(task_json,?) WHERE id=?", (patch, conv_id))
                if admitted is not None:
                    admitted["uncertain"] = True  # A failed/unknown commit must not become a known non-admission.
                conn.commit()
                if admitted is not None:
                    # Publish inside the worker, before _in_thread can discard
                    # the return value when cancellation arrives after commit.
                    admitted.update(started=True, uncertain=False)
                return {"execute": True, "tracking": tracking}
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
        return await self._in_thread(work)

    async def finish_conversation_event(
        self, event_id: int, *, state: str = "applied", error: str = "", commit_sha: str | None = None,
        expected_state: str | None = None,
    ) -> None:
        if state not in {"applied", "uncertain", "cancelled"}:
            raise ValueError("estado terminal inválido")
        def work() -> None:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "UPDATE conversation_events SET state=?, finished_at=?, error=?, commit_sha=? "
                    "WHERE id=?" + (" AND state=?" if expected_state is not None else "") + " RETURNING chat_id",
                    (state, now_iso(), error or None, commit_sha, event_id,
                     *((expected_state,) if expected_state is not None else ()))).fetchone()
                if row is not None and row[0]:
                    chat_state = "ok" if state == "applied" else "error" if state == "uncertain" else state
                    conn.execute(
                        "UPDATE chats SET status=?, finished_at=?, error=? WHERE id=? AND status IN ('queued','running')",
                        (chat_state, now_iso(), error or None, row[0]))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        await self._in_thread(work)

    async def list_conversation_events(
        self, conv_id: str, *, states: list[str] | tuple[str, ...] | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [conv_id]
        clause = ""
        if states:
            clause = " AND state IN (" + ",".join("?" for _ in states) + ")"
            params.extend(states)
        params.append(limit)
        rows = await self.run(
            f"SELECT * FROM conversation_events WHERE conversation_id=?{clause} ORDER BY id LIMIT ?",
            tuple(params))
        return [_event(row) for row in rows]

    async def recover_conversation_events(self) -> list[str]:
        rows = await self.run(
            "UPDATE conversation_events SET state='uncertain', finished_at=?, "
            "error=COALESCE(error,'interrumpido por reinicio') WHERE state='processing' "
            "RETURNING conversation_id", (now_iso(),))
        return sorted({row["conversation_id"] for row in rows})

    async def cancel_pending_conversation_events(self, conv_id: str) -> None:
        await self.run_tx([
            ("UPDATE conversation_events SET state='cancelled', finished_at=? "
             "WHERE conversation_id=? AND state='pending'", (now_iso(), conv_id)),
            ("UPDATE chats SET status='cancelled', finished_at=? WHERE conversation_id=? AND status='queued'",
             (now_iso(), conv_id)),
        ])

    async def conversation_event_for_chat(self, chat_id: str) -> dict[str, Any] | None:
        rows = await self.run("SELECT * FROM conversation_events WHERE chat_id=?", (chat_id,))
        return _event(rows[0]) if rows else None

    async def list_managed_conversations(self) -> list[dict[str, Any]]:
        rows = await self.run(
            "SELECT * FROM conversations WHERE COALESCE(task_json,'{}') <> '{}' "
            "ORDER BY last_activity_at DESC")
        for row in rows:
            try:
                row["task_json"] = json.loads(row.get("task_json") or "{}")
            except json.JSONDecodeError:
                row["task_json"] = {}
        return rows

    async def list_task_monitor_conversations(self, now: float) -> list[dict[str, Any]]:
        """Code tasks plus due external issue sync, without external histories."""
        rows = await self.run("""
            WITH tasks AS (
                SELECT id,project_slug,last_activity_at,
                       CASE WHEN json_valid(task_json) THEN task_json ELSE '{}' END AS doc
                FROM conversations WHERE COALESCE(task_json,'{}') <> '{}'
            ), project_defaults AS (
                SELECT slug,enabled,
                       CASE WHEN json_valid(defaults_json) THEN defaults_json ELSE '{}' END AS doc
                FROM projects
            )
            SELECT t.id,t.project_slug,
                   CASE WHEN json_extract(t.doc,'$.mode')='external_pr' THEN json_object(
                       'mode','external_pr','issue_link',json_extract(t.doc,'$.issue_link'),
                       'issue_sync_next_poll_at',COALESCE(json_extract(t.doc,'$.issue_sync_next_poll_at'),0))
                   ELSE t.doc END AS task_json
            FROM tasks t JOIN project_defaults p ON p.slug=t.project_slug COLLATE NOCASE
            WHERE p.enabled=1 AND t.doc <> '{}' AND (
                COALESCE(json_extract(t.doc,'$.mode'),'') <> 'external_pr' OR (
                    json_type(p.doc,'$.issue_sync_policy.enabled')='true'
                    AND COALESCE(json_extract(p.doc,'$.read_only'),0)=0
                    AND json_type(t.doc,'$.issue_link')='object'
                    AND json_extract(t.doc,'$.issue_link') <> '{}'
                    AND COALESCE(json_extract(t.doc,'$.issue_sync_next_poll_at'),0)<=?
                )
            ) ORDER BY t.last_activity_at DESC
            """, (now,))
        for row in rows:
            row["task_json"] = json.loads(row["task_json"])
        return rows
