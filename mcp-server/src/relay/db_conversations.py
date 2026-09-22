"""Conversaciones y búsqueda global del índice Relay."""
from __future__ import annotations

import time
import uuid
from typing import Optional

from .db_support import now_iso

class DatabaseConversationsMixin:

    async def global_search(self, q: str, limit: int = 8) -> dict:
        """Búsqueda agrupada para el Cmd+K de la Admin UI (UI 2026-07-20).

        3 queries (projects + chats + conversations), todas LIKE case-insensitive
        y con cap por tipo. Read-only. Más barato que el browser pegue a
        /projects + /chats + /conversations y filtre client-side.
        """
        like = f"%{q}%"
        projects = await self.run(
            "SELECT slug, name, repo_path, enabled FROM projects "
            "WHERE slug LIKE ? COLLATE NOCASE "
            "OR name LIKE ? COLLATE NOCASE "
            "OR repo_path LIKE ? COLLATE NOCASE "
            "ORDER BY slug LIMIT ?",
            (like, like, like, limit))
        chats = await self.run(
            "SELECT id, project_slug, source, status, started_at, "
            "COALESCE(model,'') AS model, "
            "COALESCE(tokens_in,0) AS tokens_in "
            "FROM chats "
            "WHERE id LIKE ? COLLATE NOCASE "
            "OR project_slug LIKE ? COLLATE NOCASE "
            "ORDER BY started_at DESC LIMIT ?",
            (f"{q}%", like, limit))
        conversations = await self.run(
            "SELECT id, project_slug, status, last_activity_at, "
            "discord_thread_id, "
            "substr(COALESCE(summary,''),1,140) AS summary "
            "FROM conversations "
            "WHERE id LIKE ? COLLATE NOCASE "
            "OR project_slug LIKE ? COLLATE NOCASE "
            "ORDER BY last_activity_at DESC LIMIT ?",
            (f"{q}%", like, limit))
        return {"q": q, "projects": projects,
                "chats": chats, "conversations": conversations}

    # ---- conversations (ADR-025: working memory por hilo) ----

    async def create_conversation(
        self, *, project_slug: str, discord_thread_id: Optional[str] = None,
        author: Optional[str] = None, branch: Optional[str] = None,
        discord_user_id: Optional[str] = None,
        discord_author: Optional[str] = None,
        requested_by: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> str:
        """Crea conversación. Iter 10.0: si viene de Discord, persistir
        discord_user_id + discord_author para que la UI pueda mandar
        replies de vuelta al autor."""
        conv_id = conversation_id or str(uuid.uuid4())
        ts = now_iso()
        await self.run(
            "INSERT INTO conversations (id, project_slug, discord_thread_id, "
            "discord_user_id, discord_author, status, started_at, "
            "last_activity_at, author, branch, requested_by, task_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (conv_id, project_slug, discord_thread_id, discord_user_id,
             discord_author, "open", ts, ts, author, branch, requested_by, "{}"),
        )
        return conv_id

    async def get_open_conversation_for_workspace(self, project: dict) -> Optional[dict]:
        from .coordination import workspace_key
        rows = await self.run(
            "SELECT c.*, p.repo_path FROM conversations c JOIN projects p "
            "ON p.slug=c.project_slug COLLATE NOCASE WHERE c.status='open' "
            "AND COALESCE(json_extract(c.task_json, '$.workspace_path'), '') = '' "
            "AND COALESCE(json_extract(c.task_json, '$.creation_failed'), 0) = 0 "
            "ORDER BY c.started_at DESC")
        key = workspace_key(project)
        return next((r for r in rows if workspace_key(
            {"slug": r["project_slug"], "repo_path": r["repo_path"]}) == key), None)

    async def get_open_conversation_for_project(
        self, project_slug: str,
    ) -> Optional[dict]:
        """Conversación ABIERTA de un proyecto (regla: una por repo).

        Se usa en /nuevo para rechazar una segunda conversación sobre el
        mismo repo (un solo working tree → pelearían por la rama activa)."""
        rows = await self.run(
            "SELECT * FROM conversations WHERE project_slug=? COLLATE NOCASE "
            "AND status='open' ORDER BY started_at DESC LIMIT 1",
            (project_slug,))
        return rows[0] if rows else None

    async def set_conversation_pr(self, conv_id: str, pr_url: str) -> None:
        await self.run(
            "UPDATE conversations SET pr_url=? WHERE id=?", (pr_url, conv_id))

    async def set_conversation_issue(self, conv_id: str, number: int) -> None:
        """Vincula la conversación a un issue de GitHub (fase 4)."""
        await self.run(
            "UPDATE conversations SET issue_number=? WHERE id=?",
            (int(number), conv_id))

    async def set_conversation_discord_user(
        self, conv_id: str, *, discord_user_id: Optional[str],
        discord_author: Optional[str] = None,
        clear: bool = False,
    ) -> None:
        """Iter 10.0: vincula/desvincula una conversación con autor Discord.

        Modos:
          - normal: discord_user_id es string no vacío → setea ambos campos
            (discord_author solo si lo pasan — COALESCE para no pisar).
          - clear: discord_user_id=None y clear=True → NULL en ambos
            campos. Usado por el botón "Desvincular Discord" del chat panel.

        Se usa cuando la UI recibe un chat que NO vino de Discord (sin
        discord_thread_id) pero queremos habilitar el bridge bidireccional.
        El bot C# necesita discord_user_id para saber a quién mandarle el
        reply cuando el experto termina.
        """
        if clear:
            await self.run(
                "UPDATE conversations SET discord_user_id=NULL, "
                "discord_author=NULL WHERE id=?", (conv_id,))
            return
        await self.run(
            "UPDATE conversations SET discord_user_id=?, "
            "discord_author=COALESCE(?, discord_author) WHERE id=?",
            (discord_user_id, discord_author, conv_id))

    # ---- projects.discord_channel_id (Iter 10.1: canal default por proyecto) ----

    async def set_project_discord_channel(
        self, slug: str, *, channel_id: Optional[str],
        clear: bool = False,
    ) -> bool:
        """Iter 10.1: setea/limpia el canal Discord default del proyecto.

        Modos:
          - normal: channel_id es string no vacío → guarda (upsert).
          - clear:  channel_id=None y clear=True → NULL. Devuelve True si
            la fila existía, False si el slug es desconocido.

        El endpoint PATCH valida el slug y el shape; este helper NO
        valida (es low-level, mirror de set_conversation_discord_user).
        """
        if clear:
            row = await self.run(
                "UPDATE projects SET discord_channel_id=NULL, "
                "updated_at=datetime('now') WHERE slug=? COLLATE NOCASE "
                "RETURNING slug", (slug,))
            return bool(row)
        # Upsert tolerante: si el slug no existe, no creamos fila nueva
        # (el endpoint valida slug primero → 404 antes de llegar acá).
        # Si llega con string vacío, lo tratamos como clear.
        if not channel_id:
            row = await self.run(
                "UPDATE projects SET discord_channel_id=NULL, "
                "updated_at=datetime('now') WHERE slug=? COLLATE NOCASE "
                "RETURNING slug", (slug,))
            return bool(row)
        row = await self.run(
            "UPDATE projects SET discord_channel_id=?, "
            "updated_at=datetime('now') WHERE slug=? COLLATE NOCASE "
            "RETURNING slug", (channel_id, slug))
        return bool(row)

    async def get_conversation_messages(
        self, conv_id: str,
    ) -> Optional[str]:
        """Devuelve el messages_json persistido de una conversación.
        None si la conversación no existe. Usado por la UI para
        reconstruir el historial cuando abres un chat.
        """
        rows = await self.run(
            "SELECT messages_json FROM conversations WHERE id=?",
            (conv_id,))
        return rows[0]["messages_json"] if rows else None

    async def get_conversation(self, conv_id: str) -> Optional[dict]:
        rows = await self.run(
            "SELECT * FROM conversations WHERE id=?", (conv_id,))
        return rows[0] if rows else None

    async def conversation_usage(self, conv_id: str) -> dict:
        """Tiempo de USO de una conversacion: lo que duraron sus runs.

        No es el reloj de pared. Los dos numeros no se parecen porque una
        conversacion queda abierta entre pedido y pedido: medido el
        2026-08-31 sobre `un run de ejemplo` (inventorydemo), 16h16 de pared contra 2h49 de
        trabajo real. Para "cuantas horas lleva esto" —el chip del header
        y las horas que van al PR de /cerrar— sirve el segundo.

        `duration_ms` puede venir NULL en un run que el relay no llego a
        cerrar (reinicio a mitad); ese run cuenta en `runs` pero suma 0.
        Preferimos subestimar antes que inventar.
        """
        rows = await self.run(
            "SELECT COUNT(*) AS runs, COALESCE(SUM(duration_ms), 0) AS ms "
            "FROM chats WHERE conversation_id=?", (conv_id,))
        r = rows[0] if rows else {}
        return {"runs": int(r.get("runs") or 0), "ms": int(r.get("ms") or 0)}

    async def get_open_conversation_by_thread(
        self, project_slug: str, discord_thread_id: str,
    ) -> Optional[dict]:
        """Conversación ABIERTA de un hilo Discord (auto-attach, ADR-025).

        Puede haber conversaciones cerradas con el mismo thread_id (hilo
        que siguió tras un /cerrar): se toma la abierta más reciente."""
        rows = await self.run(
            "SELECT * FROM conversations WHERE project_slug=? COLLATE NOCASE "
            "AND discord_thread_id=? AND status='open' "
            "ORDER BY started_at DESC LIMIT 1",
            (project_slug, discord_thread_id))
        return rows[0] if rows else None

    async def list_conversations(
        self, project_slug: Optional[str] = None,
        status: Optional[str] = None, limit: int = 20,
    ) -> list[dict]:
        """Lista SIN messages_json (blob pesado; pedirlo por id).

        Iter 10.3: incluye `branch` y `pr_url` para que la sidebar
        muestre el nombre de la rama local acumulada y el link al PR
        sin pedir `/conversations/{id}` por cada item. Son dos strings
        chicas; pesan nada contra el messages_json que seguimos
        excluyendo.

        Iter 10.6: incluye `messages_len` derivado de `messages_json`
        con `json_array_length`, para que la sidebar pinte el contador
        sin pedir el detalle. COALESCE a 0 cubre las conversaciones
        con `messages_json` NULL (la columna puede ser NULL — la
        lista las devuelve con 0).
        """
        cols = ("id, project_slug, discord_thread_id, status, started_at, "
                "closed_at, last_activity_at, summary, branch, pr_url, "
                "COALESCE(json_array_length(messages_json), 0) AS messages_len, "
                "COALESCE(task_json, '{}') AS task_json")
        where, params = [], []
        if project_slug:
            where.append("project_slug=? COLLATE NOCASE")
            params.append(project_slug)
        if status:
            where.append("status=?")
            params.append(status)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return await self.run(
            f"SELECT {cols} FROM conversations {clause} "
            "ORDER BY last_activity_at DESC LIMIT ?", (*params, limit))

    async def save_conversation_messages(
        self, conv_id: str, messages_json: str, *, expected: Optional[str] = None,
    ) -> None:
        """Persiste sin pisar cambios posteriores al snapshot del caller."""
        if expected is not None:
            rows = await self.run(
                "UPDATE conversations SET messages_json=?, last_activity_at=? "
                "WHERE id=? AND COALESCE(messages_json, '')=? RETURNING id",
                (messages_json, now_iso(), conv_id, expected))
            if not rows:
                raise RuntimeError("el historial cambió durante la ejecución; no se sobrescribió")
            return
        await self.run(
            "UPDATE conversations SET messages_json=?, last_activity_at=? "
            "WHERE id=?", (messages_json, now_iso(), conv_id))

    async def save_conversation_bitacora(
        self, conv_id: str, bitacora_json: str,
    ) -> None:
        """Persiste la bitácora del run. `""` la borra.

        Aparte de `save_conversation_messages` porque los dos se caen en
        momentos distintos: el historial se guarda solo cuando el run
        termina `ok`, y la bitácora tiene que sobrevivir también a los
        cortes —que es justo cuando sirve—.
        """
        await self.run(
            "UPDATE conversations SET bitacora_json=? WHERE id=?",
            (bitacora_json, conv_id))

    async def touch_conversation(self, conv_id: str) -> None:
        """Marca actividad. Se llama al INICIO de cada run: así el
        sweeper de auto-close nunca cierra una conversación con run en
        curso (el run dura ≤ expert_timeout << 24h)."""
        await self.run(
            "UPDATE conversations SET last_activity_at=? WHERE id=?",
            (now_iso(), conv_id))

    async def close_conversation(self, conv_id: str) -> bool:
        """Cierra (idempotente). Devuelve True si estaba abierta."""
        rows = await self.run(
            "UPDATE conversations SET status='closed', closed_at=? "
            "WHERE id=? AND status='open' RETURNING id",
            (now_iso(), conv_id))
        return bool(rows)

    async def set_conversation_summary(self, conv_id: str, summary: str) -> None:
        await self.run(
            "UPDATE conversations SET summary=? WHERE id=?",
            (summary, conv_id))

    async def stale_open_conversations(self, hours: float) -> list[dict]:
        """Conversaciones abiertas sin actividad hace > `hours` (sweeper)."""
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
        return await self.run(
            "SELECT id, project_slug, messages_json FROM conversations "
            "WHERE status='open' AND last_activity_at < ? "
            "AND COALESCE(json_extract(task_json, '$.workspace_path'), '') = ''", (cutoff,))

    # ---- facts (ADR-026: hechos atómicos, append-only + supersede soft) ----

    #: Estados de un fact. `pending` = destilado por el compactador y
    #: todavía sin revisar: cuenta para deduplicar pero NO llega al
    #: experto. Ver la nota de `status` en el schema.
    FACT_STATES = ("pending", "approved", "rejected")
