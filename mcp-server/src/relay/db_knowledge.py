"""Hechos, borradores, memorias y configuración persistida."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

from .db_support import now_iso


def _source_tx(conn, conversation_id, project_slug, actor, permission="read"):
    """Knowledge inherits its live source ACL; approval never publishes it."""
    from .conversation_access import access_tx
    if not actor or not conversation_id:
        return None
    row = conn.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
    if not row or not row["requested_by"] or str(row["project_slug"]).casefold() != str(project_slug).casefold():
        return None
    try:
        task = json.loads(row["task_json"] or "{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(task, dict) or task.get("mcp_private") or task.get("mode") == "external_pr":
        return None
    if not access_tx(conn, conversation_id, actor).get("can_" + permission):
        return None
    return dict(row)


def _require_source_tx(conn, conversation_id, project_slug, actor):
    source = _source_tx(conn, conversation_id, project_slug, actor, "control")
    if source is None:
        raise PermissionError("Origen de conocimiento no disponible para esta cuenta")
    return source

def _facts_tx(conn, project_slug, *, limit=100, include_superseded=False,
              status=None, actor=None, source_conversation=None):
    if not actor:
        return []
    where = "" if include_superseded else "AND superseded_at IS NULL "
    params = [project_slug]
    if status is not None:
        where += "AND status=? "
        params.append(status)
    if source_conversation is not None:
        where += "AND source_conversation=? "
        params.append(source_conversation)
    result = []
    for row in conn.execute(f"SELECT * FROM facts WHERE project_slug=? COLLATE NOCASE {where}ORDER BY created_at DESC,id DESC", params):
        if _source_tx(conn, row["source_conversation"], project_slug, actor):
            result.append(dict(row))
            if len(result) >= max(1, min(int(limit), 500)):
                break
    return result


class DatabaseKnowledgeMixin:

    async def add_facts(
        self, project_slug: str, facts: list[str],
        source_conversation: Optional[str] = None,
        status: str = "pending",
        *, actor: Optional[str] = None,
    ) -> int:
        """Store facts with a verified origin and its current control ACL.

        Review state controls model eligibility; it never changes the audience.
        """
        if status not in self.FACT_STATES:
            raise ValueError(f"status inválido: {status!r}")
        from .conversation_access import transaction
        def work(conn):
            _require_source_tx(conn, source_conversation, project_slug, actor)
            n = 0
            for fact in facts:
                fact = (fact or "").strip()
                if fact:
                    conn.execute("INSERT INTO facts(project_slug,fact,source_conversation,status) VALUES(?,?,?,?)",
                                 (project_slug, fact, source_conversation, status))
                    n += 1
            return n
        return await transaction(self, work, write=True)

    async def list_facts(
        self, project_slug: str, limit: int = 100,
        include_superseded: bool = False,
        status: Optional[str] = None,
        *, actor: Optional[str] = None, source_conversation: Optional[str] = None,
    ) -> list[dict]:
        """Hechos del proyecto, por default solo los VIGENTES (los
        obsoletos quedan en la tabla para auditoría pero no molestan).

        `status` filtra por estado de aprobación. Sin él vienen TODOS los
        vigentes, que es lo que quieren el panel de la UI y el compactador
        (para no re-emitir un hecho que ya está esperando revisión). Quien
        inyecta al experto pide `status="approved"` explícito.
        """
        from .conversation_access import transaction
        return await transaction(self, lambda conn: _facts_tx(conn, project_slug,
            limit=limit, include_superseded=include_superseded, status=status,
            actor=actor, source_conversation=source_conversation))

    async def facts_view(self, project_slug: str, *, limit=100, status=None, actor=None):
        """List and pending count share one current source/account snapshot."""
        from .conversation_access import transaction
        def work(conn):
            return {"facts": _facts_tx(conn, project_slug, limit=limit, status=status, actor=actor),
                    "pendientes": len(_facts_tx(conn, project_slug, limit=500, status="pending", actor=actor))}
        return await transaction(self, work)

    async def set_fact_status(self, fact_id: int, status: str, *, actor=None) -> bool:
        """Aprueba o rechaza un hecho. Devuelve False si el id no existe."""
        if status not in self.FACT_STATES:
            raise ValueError(f"status inválido: {status!r}")
        return await self._mutate_fact(fact_id, actor,
            "UPDATE facts SET status=?,reviewed_at=? WHERE id=?", (status, now_iso(), fact_id))

    async def supersede_facts(
        self, fact_ids: list[int], project_slug: str,
        superseded_by: Optional[str] = None,
        *, actor=None,
    ) -> int:
        """Marca hechos como obsoletos (soft; el compactador detectó que
        una conversación posterior los contradice). Scopeado al proyecto
        para que un id ajeno no pise hechos de otro repo."""
        from .conversation_access import transaction
        def work(conn):
            _require_source_tx(conn, superseded_by, project_slug, actor)
            n = 0
            for fid in fact_ids:
                # A compactor may only supersede facts of that exact source.
                n += conn.execute("UPDATE facts SET superseded_at=?,superseded_by=? WHERE id=? "
                    "AND project_slug=? COLLATE NOCASE AND source_conversation=? AND superseded_at IS NULL",
                    (now_iso(), superseded_by, fid, project_slug, superseded_by)).rowcount
            return n
        return await transaction(self, work, write=True)

    async def delete_fact(self, fact_id: int, *, actor=None) -> bool:
        """Borra un fact por id (curación desde la Admin UI, 2026-07-12).
        La tabla sigue siendo append-only para el compactador; esto es
        el botón de "sacar un fact que quedó viejo o mal destilado"."""
        return await self._mutate_fact(fact_id, actor, "DELETE FROM facts WHERE id=?", (fact_id,))

    async def _mutate_fact(self, fact_id, actor, sql, params):
        from .conversation_access import transaction
        def work(conn):
            row = conn.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
            if not row or not _source_tx(conn, row["source_conversation"], row["project_slug"], actor, "control"):
                return False
            return bool(conn.execute(sql, params).rowcount)
        return await transaction(self, work, write=True)

    async def knowledge_source(self, conversation_id, project_slug, *, actor=None, permission="read"):
        from .conversation_access import transaction
        return await transaction(self, lambda conn: _source_tx(conn, conversation_id, project_slug, actor, permission))

    async def save_compacted_history(self, conversation_id, project_slug, messages_json, *, expected, actor=None):
        """The final history rewrite has its own fresh control fence and CAS."""
        from .conversation_access import transaction
        def work(conn):
            _require_source_tx(conn, conversation_id, project_slug, actor)
            updated = conn.execute("UPDATE conversations SET messages_json=?,last_activity_at=? "
                "WHERE id=? AND COALESCE(messages_json,'')=?",
                (messages_json, now_iso(), conversation_id, expected))
            if not updated.rowcount:
                raise RuntimeError("el historial cambió durante la compactación; no se sobrescribió")
        await transaction(self, work, write=True)

    # ---- skill_drafts (autoaprendizaje 2026-07-12: aprobación humana) ----

    async def add_skill_draft(
        self, *, name: str, description: str, content: str,
        project_slug: Optional[str] = None,
        source_conversation: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> int:
        from .conversation_access import transaction
        def work(conn):
            if actor is not None:
                _require_source_tx(conn, source_conversation, project_slug, actor)
            return conn.execute("INSERT INTO skill_drafts(name,description,content,project_slug,source_conversation) "
                "VALUES(?,?,?,?,?) RETURNING id", (name, description, content, project_slug, source_conversation)).fetchone()[0]
        return await transaction(self, work, write=True)

    async def list_skill_drafts(
        self, status: Optional[str] = None, limit: int = 100,
    ) -> list[dict]:
        """Lista SIN content (puede ser largo; pedirlo por id)."""
        cols = ("id, name, description, project_slug, source_conversation, "
                "status, approved_path, created_at, reviewed_at, "
                "length(content) AS content_len")
        where = "WHERE status=?" if status else ""
        params = ([status] if status else []) + [limit]
        return await self.run(
            f"SELECT {cols} FROM skill_drafts {where} "
            "ORDER BY created_at DESC, id DESC LIMIT ?", tuple(params))

    async def get_skill_draft(self, draft_id: int) -> Optional[dict]:
        rows = await self.run(
            "SELECT * FROM skill_drafts WHERE id=?", (draft_id,))
        return rows[0] if rows else None

    async def update_skill_draft(
        self, draft_id: int, *, name: Optional[str] = None,
        description: Optional[str] = None, content: Optional[str] = None,
    ) -> bool:
        """Edición pre-aprobación (solo drafts pending). True si actualizó."""
        sets, params = [], []
        for col, val in (("name", name), ("description", description),
                         ("content", content)):
            if val is not None:
                sets.append(f"{col}=?")
                params.append(val)
        if not sets:
            return False
        rows = await self.run(
            f"UPDATE skill_drafts SET {', '.join(sets)} "
            "WHERE id=? AND status='pending' RETURNING id",
            (*params, draft_id))
        return bool(rows)

    async def set_skill_draft_status(
        self, draft_id: int, status: str,
        approved_path: Optional[str] = None,
    ) -> bool:
        rows = await self.run(
            "UPDATE skill_drafts SET status=?, approved_path=?, "
            "reviewed_at=? WHERE id=? RETURNING id",
            (status, approved_path, now_iso(), draft_id))
        return bool(rows)

    async def delete_skill_draft(self, draft_id: int) -> bool:
        rows = await self.run(
            "DELETE FROM skill_drafts WHERE id=? RETURNING id", (draft_id,))
        return bool(rows)

    async def count_skill_drafts_pending(self) -> int:
        rows = await self.run(
            "SELECT COUNT(*) AS n FROM skill_drafts WHERE status='pending'")
        return rows[0]["n"]


    # ---- memories (ADR-027: FTS5 sobre resúmenes compactados) ----

    async def add_memory(
        self, conversation_id: str, project_slug: str, summary: str,
        *, actor=None,
    ) -> bool:
        """Indexa (o re-indexa) el resumen de una conversación."""
        from .conversation_access import transaction
        def work(conn):
            _require_source_tx(conn, conversation_id, project_slug, actor)
            conn.execute("UPDATE conversations SET summary=? WHERE id=?", (summary, conversation_id))
            if self._fts_available:
                conn.execute("DELETE FROM memories_fts WHERE conversation_id=?", (conversation_id,))
                conn.execute("INSERT INTO memories_fts(summary,project_slug,conversation_id) VALUES(?,?,?)",
                             (summary, project_slug, conversation_id))
            return True
        return await transaction(self, work, write=True)

    async def delete_memory(self, conversation_id: str, *, actor=None) -> bool:
        """Saca una memoria del retrieval (curación desde la Admin UI).

        Borra la fila FTS5 y limpia conversations.summary — las DOS rutas
        de search_memories (MATCH y fallback por recientes) dejan de
        devolverla. El historial (messages_json) queda intacto. True si
        la conversación existía y tenía summary.
        """
        from .conversation_access import transaction
        def work(conn):
            row = conn.execute("SELECT project_slug FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if not row or not _source_tx(conn, conversation_id, row["project_slug"], actor, "control"):
                return False
            if self._fts_available:
                conn.execute("DELETE FROM memories_fts WHERE conversation_id=?", (conversation_id,))
            return bool(conn.execute("UPDATE conversations SET summary=NULL WHERE id=? AND summary IS NOT NULL",
                                     (conversation_id,)).rowcount)
        return await transaction(self, work, write=True)

    @staticmethod
    def _fts_quote(query: str) -> str:
        """Sanitiza el query del usuario para MATCH: cada término entre
        comillas, así `AND`, `-`, `:` y demás operadores FTS5 no rompen."""
        terms = [t.replace('"', "") for t in query.split()]
        return " ".join(f'"{t}"' for t in terms if t)

    async def search_memories(
        self, project_slug: str, query: str = "", limit: int = 5, *, actor=None, source_conversation=None,
    ) -> list[dict]:
        """Retrieve only live sources readable by this actor in one snapshot."""
        from .conversation_access import transaction
        def work(conn):
            if not actor:
                return []
            rows = None
            match = self._fts_quote((query or "").strip())
            if match and self._fts_available:
                try:
                    rows = conn.execute("SELECT c.id AS conversation_id,c.project_slug,c.summary,"
                        "bm25(memories_fts) AS rank FROM memories_fts JOIN conversations c "
                        "ON c.id=memories_fts.conversation_id AND c.project_slug=memories_fts.project_slug COLLATE NOCASE "
                        "WHERE memories_fts MATCH ? AND c.project_slug=? COLLATE NOCASE "
                        "AND c.summary IS NOT NULL AND c.summary<>'' ORDER BY rank", (match, project_slug)).fetchall()
                except sqlite3.OperationalError:
                    pass
            if rows is None:
                rows = conn.execute("SELECT id AS conversation_id,project_slug,summary FROM conversations "
                    "WHERE project_slug=? COLLATE NOCASE AND summary IS NOT NULL AND summary<>'' "
                    "ORDER BY closed_at DESC,id", (project_slug,)).fetchall()
            result = []
            for row in rows:
                if source_conversation is not None and row["conversation_id"] != source_conversation:
                    continue
                if _source_tx(conn, row["conversation_id"], project_slug, actor):
                    result.append(dict(row))
                    if len(result) >= max(1, min(int(limit), 500)):
                        break
            return result
        return await transaction(self, work)

    # ---- system_config (red + paths, gestionable desde la Admin UI) ----

    async def get_config(self, key: str, default: Optional[str] = None) -> Optional[str]:
        rows = await self.run(
            "SELECT value FROM system_config WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    async def set_config(self, key: str, value: str) -> None:
        await self.run(
            "INSERT INTO system_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=datetime('now')",
            (key, str(value)))

    async def all_config(self) -> dict[str, str]:
        rows = await self.run(
            "SELECT key, value FROM system_config ORDER BY key")
        return {r["key"]: r["value"] for r in rows}

    # ---- command_logs ----

    async def log_command(
        self, *, name: str, source: str, author: Optional[str],
        args: dict, response: str, duration_ms: int, status: str,
    ) -> None:
        await self.run(
            "INSERT INTO command_logs (command_name, source, author, args_json, "
            "response, duration_ms, status) VALUES (?,?,?,?,?,?,?)",
            (name, source, author, json.dumps(args, ensure_ascii=False),
             response[:4000], duration_ms, status),
        )

    # ---- stats ----

    async def stats(self) -> dict:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        chats = await self.run(
            "SELECT COUNT(*) AS n, COALESCE(SUM(tokens_in),0) AS tin, "
            "COALESCE(SUM(tokens_out),0) AS tout FROM chats "
            "WHERE started_at >= ?", (today,))
        cmds = await self.run(
            "SELECT COUNT(*) AS n FROM command_logs WHERE ts >= ?", (today,))
        return {
            "chats_today": chats[0]["n"],
            "tokens_in_today": chats[0]["tin"],
            "tokens_out_today": chats[0]["tout"],
            "commands_today": cmds[0]["n"],
        }
