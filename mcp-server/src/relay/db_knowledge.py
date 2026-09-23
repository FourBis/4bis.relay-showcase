"""Hechos, borradores, memorias y configuración persistida."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

from .db_support import now_iso

class DatabaseKnowledgeMixin:

    async def add_facts(
        self, project_slug: str, facts: list[str],
        source_conversation: Optional[str] = None,
        status: str = "pending",
    ) -> int:
        """Agrega hechos. `status='pending'` por default: lo que escribe
        el compactador espera aprobación. Quien llama con `approved` es
        el alta manual desde la UI — ahí lo escribió un humano y no hay
        a quién pedirle permiso."""
        if status not in self.FACT_STATES:
            raise ValueError(f"status inválido: {status!r}")
        n = 0
        for fact in facts:
            fact = (fact or "").strip()
            if not fact:
                continue
            await self.run(
                "INSERT INTO facts (project_slug, fact, source_conversation, "
                "status) VALUES (?,?,?,?)",
                (project_slug, fact, source_conversation, status))
            n += 1
        return n

    async def list_facts(
        self, project_slug: str, limit: int = 100,
        include_superseded: bool = False,
        status: Optional[str] = None,
    ) -> list[dict]:
        """Hechos del proyecto, por default solo los VIGENTES (los
        obsoletos quedan en la tabla para auditoría pero no molestan).

        `status` filtra por estado de aprobación. Sin él vienen TODOS los
        vigentes, que es lo que quieren el panel de la UI y el compactador
        (para no re-emitir un hecho que ya está esperando revisión). Quien
        inyecta al experto pide `status="approved"` explícito.
        """
        where = "" if include_superseded else "AND superseded_at IS NULL "
        params: list[Any] = [project_slug]
        if status is not None:
            where += "AND status=? "
            params.append(status)
        params.append(limit)
        return await self.run(
            f"SELECT * FROM facts WHERE project_slug=? COLLATE NOCASE "
            f"{where}ORDER BY created_at DESC, id DESC LIMIT ?",
            tuple(params))

    async def set_fact_status(self, fact_id: int, status: str) -> bool:
        """Aprueba o rechaza un hecho. Devuelve False si el id no existe."""
        if status not in self.FACT_STATES:
            raise ValueError(f"status inválido: {status!r}")
        rows = await self.run(
            "UPDATE facts SET status=?, reviewed_at=? WHERE id=? RETURNING id",
            (status, now_iso(), fact_id))
        return bool(rows)

    async def supersede_facts(
        self, fact_ids: list[int], project_slug: str,
        superseded_by: Optional[str] = None,
    ) -> int:
        """Marca hechos como obsoletos (soft; el compactador detectó que
        una conversación posterior los contradice). Scopeado al proyecto
        para que un id ajeno no pise hechos de otro repo."""
        n = 0
        for fid in fact_ids:
            rows = await self.run(
                "UPDATE facts SET superseded_at=?, superseded_by=? "
                "WHERE id=? AND project_slug=? COLLATE NOCASE "
                "AND superseded_at IS NULL RETURNING id",
                (now_iso(), superseded_by, fid, project_slug))
            n += len(rows)
        return n

    async def delete_fact(self, fact_id: int) -> bool:
        """Borra un fact por id (curación desde la Admin UI, 2026-07-12).
        La tabla sigue siendo append-only para el compactador; esto es
        el botón de "sacar un fact que quedó viejo o mal destilado"."""
        rows = await self.run(
            "DELETE FROM facts WHERE id=? RETURNING id", (fact_id,))
        return bool(rows)

    # ---- skill_drafts (autoaprendizaje 2026-07-12: aprobación humana) ----

    async def add_skill_draft(
        self, *, name: str, description: str, content: str,
        project_slug: Optional[str] = None,
        source_conversation: Optional[str] = None,
    ) -> int:
        rows = await self.run(
            "INSERT INTO skill_drafts (name, description, content, "
            "project_slug, source_conversation) VALUES (?,?,?,?,?) "
            "RETURNING id",
            (name, description, content, project_slug, source_conversation))
        return rows[0]["id"]

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
    ) -> bool:
        """Indexa (o re-indexa) el resumen de una conversación."""
        if not self._fts_available:
            return False
        # idempotente: recompactar reemplaza la entrada previa
        await self.run(
            "DELETE FROM memories_fts WHERE conversation_id=?",
            (conversation_id,))
        await self.run(
            "INSERT INTO memories_fts (summary, project_slug, conversation_id) "
            "VALUES (?,?,?)", (summary, project_slug, conversation_id))
        return True

    async def delete_memory(self, conversation_id: str) -> bool:
        """Saca una memoria del retrieval (curación desde la Admin UI).

        Borra la fila FTS5 y limpia conversations.summary — las DOS rutas
        de search_memories (MATCH y fallback por recientes) dejan de
        devolverla. El historial (messages_json) queda intacto. True si
        la conversación existía y tenía summary.
        """
        if self._fts_available:
            await self.run(
                "DELETE FROM memories_fts WHERE conversation_id=?",
                (conversation_id,))
        rows = await self.run(
            "UPDATE conversations SET summary=NULL "
            "WHERE id=? AND summary IS NOT NULL RETURNING id",
            (conversation_id,))
        return bool(rows)

    @staticmethod
    def _fts_quote(query: str) -> str:
        """Sanitiza el query del usuario para MATCH: cada término entre
        comillas, así `AND`, `-`, `:` y demás operadores FTS5 no rompen."""
        terms = [t.replace('"', "") for t in query.split()]
        return " ".join(f'"{t}"' for t in terms if t)

    async def search_memories(
        self, project_slug: str, query: str = "", limit: int = 5,
    ) -> list[dict]:
        """Busca resúmenes por FTS5 scopeado a un proyecto (ADR-027).

        Sin query (o sin FTS5): devuelve los resúmenes más recientes del
        proyecto desde `conversations` directamente.
        """
        query = (query or "").strip()
        if query and self._fts_available:
            match = self._fts_quote(query)
            if match:
                try:
                    return await self.run(
                        "SELECT conversation_id, project_slug, summary, "
                        "bm25(memories_fts) AS rank FROM memories_fts "
                        "WHERE memories_fts MATCH ? AND project_slug=? "
                        "ORDER BY rank LIMIT ?",
                        (match, project_slug, limit))
                except sqlite3.OperationalError:
                    pass  # query FTS malformado pese al quoting → fallback
        rows = await self.run(
            "SELECT id AS conversation_id, project_slug, summary "
            "FROM conversations WHERE project_slug=? COLLATE NOCASE "
            "AND summary IS NOT NULL AND summary != '' "
            "ORDER BY closed_at DESC LIMIT ?", (project_slug, limit))
        return rows

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
