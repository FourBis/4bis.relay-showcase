"""Conversaciones y búsqueda global del índice Relay."""
from __future__ import annotations

import time
import json
import re
import sqlite3
import uuid
from typing import Optional

from .db_support import now_iso


_CREATION_CONFLICT = "El identificador de creación pertenece a otro pedido; no se modificó"

# Only a server-stored v2 marker can identify another canonical spelling of
# the same creation. Legacy conversations never acquire an invented request ID.
_LOGICAL_CREATION_ROW = (
    "project_slug=? COLLATE NOCASE AND CASE WHEN json_valid(task_json) THEN "
    "json_type(task_json,'$.creation_request.version')='integer' AND "
    "json_extract(task_json,'$.creation_request.version')=2 AND "
    "json_type(task_json,'$.creation_request.request_id')='text' AND "
    "json_extract(task_json,'$.creation_request.request_id')=? ELSE 0 END"
)


def _rejection_row(conn, conversation_id, project_slug=None, request_id=None):
    sql = "SELECT * FROM creation_rejections WHERE conversation_id=?"
    params = [conversation_id]
    if project_slug is not None and request_id is not None:
        sql += " OR (project_slug=? COLLATE NOCASE AND request_id=?)"
        params.extend((project_slug, request_id))
    # Older data could contain multiple case aliases: prefer the exact CID,
    # otherwise the first immutable barrier, never a row selected by its hash.
    sql += " ORDER BY CASE WHEN conversation_id=? THEN 0 ELSE 1 END,rejected_at,conversation_id LIMIT 1"
    params.append(conversation_id)
    return conn.execute(sql, tuple(params)).fetchone()


def _rejection_contract(value):
    if (not isinstance(value, dict) or value.get("version") not in {1, 2}
            or not isinstance(value.get("request_id"), str)
            or not 1 <= len(value["request_id"]) <= 128
            or not isinstance(value.get("payload_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["payload_sha256"])):
        raise ValueError("Contrato de creación no verificable")
    return {key: value[key] for key in ("version", "request_id", "payload_sha256")}


def _rejection_actor(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Actor de creación no verificable")
    return value.strip().lower()


def _require_rejection_scope(conn, actor, project_slug):
    """Only an active chat actor can seal/read its own request proof.

    Existing policy permits a personal read-only consultation without project
    write assignment. Sealing must not impose a new assignment gate or grant
    access to a row: reconciliation has its own control ACL, proof delivery
    checks the original actor/hash, and creation revalidates permissions.
    """
    from .conversation_access import user_tx
    user = user_tx(conn, actor)
    if not user or user["role"] not in {"owner", "member", "subadmin"}:
        raise PermissionError("Creación no disponible para esta cuenta")


def _rejection_proof(row, *, actor, project_slug=None, creation_contract=None):
    """Return only the immutable public identity, never actor, input or error text."""
    if row is None:
        return None
    if (row["requested_by"] != actor
            or (project_slug is not None and row["project_slug"].casefold() != project_slug.casefold())
            or (creation_contract is not None and any(
                row[column] != creation_contract[key]
                for key, column in (("version", "contract_version"),
                                    ("request_id", "request_id"),
                                    ("payload_sha256", "payload_sha256"))))):
        raise ValueError(_CREATION_CONFLICT)
    return {"version": row["contract_version"], "state": "rejected",
            "conversation_id": row["conversation_id"], "project_slug": row["project_slug"],
            "request_id": row["request_id"], "payload_sha256": row["payload_sha256"],
            "rejected_at": row["rejected_at"]}


_AUTOCLOSE_ELIGIBLE = (
    "c.status='open' AND c.last_activity_at < ? "
    "AND COALESCE(json_extract(c.task_json, '$.mode'), '') <> 'external_pr' "
    "AND COALESCE(json_extract(c.task_json, '$.workspace_path'), '') = '' "
    "AND NOT EXISTS (SELECT 1 FROM task_graphs g "
    "WHERE g.conversation_id=c.id AND g.estado='activo') "
    "AND NOT EXISTS (SELECT 1 FROM expert_questions q "
    "LEFT JOIN chats h ON h.id=q.chat_id WHERE q.status='open' "
    "AND (q.conversation_id=c.id OR h.conversation_id=c.id))"
)


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
        initial_task: Optional[dict] = None,
    ) -> str:
        """Crea conversación. Iter 10.0: si viene de Discord, persistir
        discord_user_id + discord_author para que la UI pueda mandar
        replies de vuelta al autor."""
        conv_id = conversation_id or str(uuid.uuid4())
        if initial_task is not None and not isinstance(initial_task, dict):
            raise ValueError("initial_task debe ser un objeto del servidor")
        ts = now_iso()
        task_json = json.dumps(initial_task or {}, ensure_ascii=False)
        origin = (initial_task or {}).get("creation_request")
        logical_contract = (_rejection_contract(origin)
                            if isinstance(origin, dict) and origin.get("version") == 2 else None)
        def work():
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                rejected = _rejection_row(conn, conv_id, project_slug,
                                           logical_contract["request_id"] if logical_contract else None)
                if rejected is not None:
                    from .conversation_creation import CreationRejected
                    requested_contract = _rejection_contract((initial_task or {}).get("creation_request"))
                    actor = _rejection_actor(requested_by)
                    _require_rejection_scope(conn, actor, project_slug)
                    proof = _rejection_proof(
                        rejected, actor=actor, project_slug=project_slug,
                        creation_contract=requested_contract)
                    raise CreationRejected(proof)
                if logical_contract and conn.execute(
                    f"SELECT 1 FROM conversations WHERE {_LOGICAL_CREATION_ROW} LIMIT 1",
                    (project_slug, logical_contract["request_id"]),
                ).fetchone():
                    # A case-alias producer may have a different deterministic
                    # CID. The route must reconcile the actual row under ACL.
                    raise sqlite3.IntegrityError("La solicitud de creación ya tiene una conversación")
                conn.execute(
                    "INSERT INTO conversations (id, project_slug, discord_thread_id, "
                    "discord_user_id, discord_author, status, started_at, "
                    "last_activity_at, author, branch, requested_by, task_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (conv_id, project_slug, discord_thread_id, discord_user_id,
                     discord_author, "open", ts, ts, author, branch, requested_by, task_json))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
        await self._in_thread(work)
        return conv_id

    async def get_creation_rejection(
        self, conversation_id: str, *, requested_by: str,
        project_slug: Optional[str] = None, creation_contract: Optional[dict] = None,
        request_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Read terminal pre-row proof for its actor; absence is not rejection proof."""
        actor = _rejection_actor(requested_by)
        expected = _rejection_contract(creation_contract) if creation_contract is not None else None
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
            raise ValueError("Identidad de creación no verificable")
        if expected:
            if request_id is not None and request_id != expected["request_id"]:
                raise ValueError(_CREATION_CONFLICT)
            request_id = expected["request_id"]
        if project_slug is not None and (not isinstance(project_slug, str) or not project_slug):
            raise ValueError("Identidad de creación no verificable")
        def work():
            conn = self._connect()
            try:
                conn.execute("BEGIN")
                row = _rejection_row(conn, conversation_id, project_slug, request_id)
                _require_rejection_scope(conn, actor, project_slug or (row["project_slug"] if row else None))
                proof = _rejection_proof(row, actor=actor, project_slug=project_slug, creation_contract=expected)
                conn.commit()
                return proof
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
        return await self._in_thread(work)

    async def reject_creation(
        self, *, project_slug: str, conversation_id: str, requested_by: str,
        creation_contract: dict,
    ) -> Optional[dict]:
        """Prove a settled validation rejection iff no conversation won the race.

        The rejection and every subsequent conversation INSERT exclude each other
        under SQLite's write lock. A late producer cannot create the rejected ID.
        None means a row already exists and must be reconciled, never a safe retry.
        """
        expected = _rejection_contract(creation_contract)
        actor = _rejection_actor(requested_by)
        if (not isinstance(project_slug, str) or not project_slug
                or conversation_id != str(uuid.uuid5(
                    uuid.NAMESPACE_URL, f"relay:{project_slug}:{expected['request_id']}"))):
            raise ValueError("Identidad de creación no verificable")
        def work():
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                _require_rejection_scope(conn, actor, project_slug)
                if conn.execute(
                    f"SELECT 1 FROM conversations WHERE id=? OR ({_LOGICAL_CREATION_ROW}) LIMIT 1",
                    (conversation_id, project_slug, expected["request_id"]),
                ).fetchone():
                    conn.commit()
                    return None
                row = _rejection_row(conn, conversation_id, project_slug, expected["request_id"])
                if row is None:
                    conn.execute(
                        "INSERT INTO creation_rejections "
                        "(conversation_id,project_slug,requested_by,contract_version,request_id,payload_sha256,rejected_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (conversation_id, project_slug, actor, expected["version"], expected["request_id"],
                         expected["payload_sha256"], now_iso()))
                    row = conn.execute(
                        "SELECT * FROM creation_rejections WHERE conversation_id=?", (conversation_id,)
                    ).fetchone()
                proof = _rejection_proof(row, actor=actor, project_slug=project_slug,
                                         creation_contract=expected)
                conn.commit()
                return proof
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
        return await self._in_thread(work)

    async def get_open_conversation_for_workspace(self, project: dict) -> Optional[dict]:
        from .coordination import workspace_key
        rows = await self.run(
            "SELECT c.*, p.repo_path FROM conversations c JOIN projects p "
            "ON p.slug=c.project_slug COLLATE NOCASE WHERE c.status='open' "
            "AND COALESCE(json_extract(c.task_json, '$.workspace_path'), '') = '' "
            "AND COALESCE(json_extract(c.task_json, '$.creation_failed'), 0) = 0 "
            "AND COALESCE(json_extract(c.task_json, '$.mcp_private'), 0) = 0 "
            "AND COALESCE(json_extract(c.task_json, '$.media_only'), 0) = 0 "
            "AND COALESCE(json_extract(c.task_json, '$.no_checkout'), 0) = 0 "
            "AND COALESCE(json_extract(c.task_json, '$.mode'), '') <> 'external_pr' "
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
            "AND status='open' AND COALESCE(json_extract(task_json, '$.mcp_private'), 0) = 0 "
            "AND COALESCE(json_extract(task_json, '$.mode'), '') <> 'external_pr' "
            "ORDER BY started_at DESC LIMIT 1",
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
        """Inactivas y sin trabajo o decisiones pendientes para el sweeper."""
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
        return await self.run(
            "SELECT c.id, c.project_slug, c.messages_json, c.requested_by FROM conversations c "
            f"WHERE {_AUTOCLOSE_ELIGIBLE}", (cutoff,))

    async def close_stale_conversation(self, conv_id: str, hours: float) -> bool:
        """Revalida al cerrar: una pregunta o grafo nuevos ganan al temporizador."""
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
        rows = await self.run(
            "UPDATE conversations AS c SET status='closed', closed_at=? "
            f"WHERE c.id=? AND {_AUTOCLOSE_ELIGIBLE} RETURNING id",
            (now_iso(), conv_id, cutoff))
        return bool(rows)

    # ---- facts (ADR-026: hechos atómicos, append-only + supersede soft) ----

    #: Estados de un fact. `pending` = destilado por el compactador y
    #: todavía sin revisar: cuenta para deduplicar pero NO llega al
    #: experto. Ver la nota de `status` en el schema.
    FACT_STATES = ("pending", "approved", "rejected")
