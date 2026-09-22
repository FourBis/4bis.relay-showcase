"""Persistencia mínima de tareas y eventos por conversación."""
from __future__ import annotations

import asyncio
import json
import uuid
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

        return await asyncio.to_thread(work)

    async def enqueue_conversation_event(
        self, conv_id: str, event_key: str, kind: str, payload: dict[str, Any],
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
                existing = conn.execute(
                    "SELECT * FROM conversation_events WHERE conversation_id=? AND event_key=?",
                    (conv_id, event_key)).fetchone()
                if existing is not None:
                    item = dict(existing)
                    if item["kind"] != kind or _json(json.loads(item["payload"] or "{}")) != encoded:
                        raise ValueError("event_key ya existe con distinto payload")
                    conn.commit()
                    return _event(item)
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

        return await asyncio.to_thread(work)

    async def claim_conversation_event(self, conv_id: str) -> dict[str, Any] | None:
        def work() -> dict[str, Any] | None:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("SELECT 1 FROM conversation_events WHERE conversation_id=? AND state IN ('processing','uncertain') LIMIT 1", (conv_id,)).fetchone():
                    conn.rollback()
                    return None
                row = conn.execute(
                    "SELECT id FROM conversation_events WHERE conversation_id=? AND state='pending' ORDER BY id LIMIT 1",
                    (conv_id,)).fetchone()
                if row is None:
                    conn.rollback()
                    return None
                updated = conn.execute(
                    "UPDATE conversation_events SET state='processing' WHERE id=? RETURNING *", (row[0],)).fetchone()
                if updated["chat_id"]:
                    conn.execute("UPDATE chats SET status='running', started_at=COALESCE(started_at,?) WHERE id=?", (now_iso(), updated["chat_id"]))
                conn.commit()
                return _event(dict(updated))
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        return await asyncio.to_thread(work)

    async def finish_conversation_event(
        self, event_id: int, *, state: str = "applied", error: str = "", commit_sha: str | None = None,
    ) -> None:
        if state not in {"applied", "uncertain", "cancelled"}:
            raise ValueError("estado terminal inválido")
        def work() -> None:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "UPDATE conversation_events SET state=?, finished_at=?, error=?, commit_sha=? "
                    "WHERE id=? RETURNING chat_id",
                    (state, now_iso(), error or None, commit_sha, event_id)).fetchone()
                if row is not None and row[0]:
                    chat_state = "ok" if state == "applied" else "error" if state == "uncertain" else state
                    conn.execute(
                        "UPDATE chats SET status=?, finished_at=? WHERE id=? AND status IN ('queued','running')",
                        (chat_state, now_iso(), row[0]))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        await asyncio.to_thread(work)

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
