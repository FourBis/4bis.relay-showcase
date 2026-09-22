"""Índice de chats y persistencia de runs."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Optional

from .db_support import _CAP_PROMPT_FILA, now_iso

class DatabaseChatsMixin:

    async def create_chat(
        self, *, project_slug: Optional[str], source: str,
        author: Optional[str], target: Optional[str],
        vscode_session_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        requested_by: Optional[str] = None,
        user_prompt: Optional[str] = None,
    ) -> str:
        """Abre la fila del run. `user_prompt` es el título del turno.

        Se recorta a `_CAP_PROMPT_FILA` porque no es el registro del
        pedido —ese es el .md— sino lo que el panel del plan muestra
        como título del nodo. Guardar el pedido entero engordaría una
        tabla que se lee en cada poll para mostrar 60 caracteres.
        """
        chat_id = str(uuid.uuid4())
        prompt = (user_prompt or "").strip()[:_CAP_PROMPT_FILA] or None
        await self.run(
            "INSERT INTO chats (id, project_slug, vscode_session_id, source, "
            "author, target, started_at, conversation_id, requested_by, "
            "user_prompt) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (chat_id, project_slug, vscode_session_id, source, author,
             target, now_iso(), conversation_id, requested_by, prompt),
        )
        return chat_id

    async def finish_chat(
        self, chat_id: str, *, status: str, md_path: Optional[str] = None,
        tokens_in: Optional[int] = None, tokens_out: Optional[int] = None,
        cache_read_tokens: Optional[int] = None,  # 2026-08-31
        tool_calls: Optional[int] = None, error: Optional[str] = None,
        phase_at_end: Optional[str] = None, last_tool: Optional[str] = None,
        duration_ms: Optional[int] = None,
        model: Optional[str] = None,
        progress_events: Optional[str] = None,  # Sprint 1: JSON array
        trimmed_turns: int = 0,  # Sprint 1
        tool_bytes: Optional[str] = None,  # 2026-07-26: JSON {tool: {...}}
        stages_json: Optional[str] = None,  # 2026-08-14: etapas del runner
        artifact: Optional[dict] = None,
    ) -> None:
        # Iter 9.8: agregamos model + duration_ms. Para que la migración
        # consults → chats y el path legacy consults puedan guardarlos
        # sin re-mapear. Defaults=None para no tocar las llamadas viejas.
        statement = (
            "UPDATE chats SET status=?, finished_at=?, "
            "md_path=COALESCE(?, md_path), "
            "tokens_in=COALESCE(?, tokens_in), "
            "tokens_out=COALESCE(?, tokens_out), "
            "cache_read_tokens=COALESCE(?, cache_read_tokens), "
            "tool_calls=COALESCE(?, tool_calls), "
            "duration_ms=COALESCE(?, duration_ms), "
            "model=COALESCE(?, model), "
            "progress_events=COALESCE(?, progress_events), "
            "tool_bytes=COALESCE(?, tool_bytes), "
            "stages_json=COALESCE(?, stages_json), "
            "error=COALESCE(?, error), "
            "phase_at_end=COALESCE(?, phase_at_end), "
            "last_tool=COALESCE(?, last_tool), "
            "trimmed=?, trimmed_turns=? WHERE id=?",
            (status, now_iso(), md_path, tokens_in, tokens_out,
             cache_read_tokens, tool_calls,
             duration_ms, model, progress_events, tool_bytes, stages_json,
             error, phase_at_end, last_tool,
             1 if trimmed_turns > 0 else 0, trimmed_turns,
             chat_id),
        )
        if artifact is None:
            await self.run(*statement)
        else:
            await self.run_tx([
                statement,
                ("INSERT INTO chat_outputs(chat_id, payload) VALUES (?, ?) "
                 "ON CONFLICT(chat_id) DO UPDATE SET payload=excluded.payload, exported=0",
                 (chat_id, json.dumps(artifact, ensure_ascii=False))),
            ])

    async def reap_running_chats(self, motivo: str) -> int:
        """Cierra los chats que quedaron en `running` de un relay anterior.

        Ningún run sobrevive a un reinicio del proceso, así que una fila
        en `running` al bootear es siempre mentira: la UI la cuenta como
        viva y le corre el reloj sola. `orquestador.sanar` repara las
        TAREAS de los grafos activos pero nunca tocó `chats`, y los
        chats de un grafo cancelado o fallado no los mira nadie.

        Devuelve cuántos cerró. Idempotente.

        Ponytail: cierra TODOS los `running`, sin mirar antigüedad. Es
        correcto porque el relay es un solo proceso (start.ps1 mata al
        anterior). Si algún día conviven dos, esto tiene que filtrar por
        dueño del run.
        """
        rows = await self.run("SELECT id FROM chats WHERE status='running'")
        if not rows:
            return 0
        await self.run(
            "UPDATE chats SET status='cancelled', finished_at=?, "
            "error=COALESCE(error, ?), "
            "phase_at_end=COALESCE(phase_at_end, 'cancelled') "
            "WHERE status='running'",
            (now_iso(), motivo))
        return len(rows)

    async def set_chat_suggestions(self, chat_id: str, suggestions_json: str) -> None:
        """Guarda las sugerencias de continuación del run (JSON array).

        Aparte de finish_chat porque se generan DESPUÉS de cerrar el chat
        (un turno extra de LLM que no debe demorar el cierre ni perderse
        si falla).
        """
        await self.run(
            "UPDATE chats SET suggestions=? WHERE id=?",
            (suggestions_json, chat_id))

    async def get_chat(self, chat_id: str) -> Optional[dict]:
        rows = await self.run("SELECT * FROM chats WHERE id=?", (chat_id,))
        return rows[0] if rows else None

    async def list_zombie_chats(
        self, *, older_than_s: int = 60, limit: int = 200,
    ) -> list[dict]:
        """Chats en status=running con started_at muy viejo (Iter 5.3).

        Un zombie es un chat que figura running pero el relay perdió el
        proceso (reinicio, crash sin DB update, etc.). Por convención
        los consideramos zombies después de `older_than_s` segundos sin
        terminar — un run interactivo normal termina en <60s, así que
        un running de hace 2min es sospechoso.

        Si el relay está corriendo y el chat TIENE proceso vivo, NO
        aparece acá — el relay se va a enterar por sí solo.
        """
        threshold = time.time() - older_than_s
        # started_at es ISO 8601 UTC ("...Z"). strftime '%Y-%m-%dT%H:%M:%SZ'.
        # Lo filtramos por strftime en SQLite — las fechas vienen del
        # mismo formato siempre (now_iso), así que el orden lexicográfico
        # matchea el orden cronológico.
        threshold_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(threshold))
        return await self.run(
            "SELECT * FROM chats WHERE status='running' "
            "AND started_at < ? ORDER BY started_at DESC LIMIT ?",
            (threshold_iso, limit))

    async def delete_chat(self, chat_id: str) -> bool:
        """Hard-delete de un chat zombie (Iter 5.3).

        Borra la fila de `chats` y best-effort el .md y el JSONL
        asociado. Devuelve True si borró, False si no existía.
        El .md/JSONL son best-effort — si no existen o fallan, igual
        la fila queda fuera.
        """
        chat = await self.get_chat(chat_id)
        if chat is None:
            return False
        md_path = chat.get("md_path")
        await self.run("DELETE FROM chats WHERE id=?", (chat_id,))
        # Cascade best-effort de archivos. Si falla (permisos, etc.),
        # la fila de DB ya está fuera — el humano verá el .md suelto
        # en state/ y decidirá qué hacer.
        if md_path:
            try:
                Path(md_path).unlink(missing_ok=True)
            except OSError:
                pass
        # JSONL lives under chats/<target>/...jsonl (relative to the
        # chat dir). Best-effort: skip — los JSONL no pesan, no urge.
        return True

    async def set_chat_stages(self, chat_id: str, stages_json: str) -> None:
        """Escribe `stages_json` en un chat que TODAVÍA está corriendo.

        `finish_chat` ya lo guarda, pero recién al terminar — y el plan
        del planificador existe a los pocos segundos de arrancar. Servía
        de nada para mirar un run de diez minutos: el panel lo mostraba
        justo cuando el run ya no estaba. Esto lo deja disponible apenas
        se conoce; `finish_chat` después lo pisa con el objeto completo.
        """
        await self.run("UPDATE chats SET stages_json=? WHERE id=?",
                       (stages_json, chat_id))

    async def list_chats(
        self, project_slug: Optional[str] = None, limit: int = 20,
        status: Optional[str] = None,
    ) -> list[dict]:
        """Índice de chats. Filtros opcionales combinables."""
        where, params = [], []
        if project_slug:
            where.append("project_slug=?")
            params.append(project_slug)
        if status:
            where.append("status=?")
            params.append(status)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        return await self.run(
            f"SELECT * FROM chats {clause} "
            "ORDER BY started_at DESC LIMIT ?", tuple(params))

    async def list_chats_by_conversation(
        self, conversation_id: str,
    ) -> list[dict]:
        """Lista los chats (runs) de una conversación, en orden cronológico.

        Usado por GET /conversations/{id}/messages para armar la vista
        del humano desde el resultado durable o los .md históricos.

        `tool_calls`/`phase_at_end` van para que la burbuja del experto
        pueda decir cuántas tools corrió: un turno que anunció y no
        ejecutó se veía idéntico a uno que trabajó (2026-08-13)."""
        return await self.run(
            "SELECT c.id, c.md_path, c.started_at, c.progress_events, c.tool_calls, "
            "c.phase_at_end, c.requested_by, c.status, c.stages_json, c.duration_ms, "
            "o.payload AS output_payload, "
            # Estado de la exportación: la UI ya cae al payload cuando
            # falta el .md, pero no podía DECIR que estaba pendiente ni
            # por qué. Sin esto, un chat sin archivo se ve igual que uno
            # que nunca lo iba a tener.
            "o.exported, o.intentos AS export_intentos, "
            "o.ultimo_error AS export_error, "
            "o.proximo_intento_at AS export_proximo "
            "FROM chats c LEFT JOIN chat_outputs o ON o.chat_id=c.id "
            "WHERE c.conversation_id=? AND (c.md_path IS NOT NULL OR o.chat_id IS NOT NULL) "
            "ORDER BY c.started_at ASC",
            (conversation_id,))

    async def list_chats_of_conversation(
        self, conversation_id: str, limit: int = 10,
    ) -> list[dict]:
        """Los últimos runs de un hilo, con lo que el panel del plan pide.

        No reusa `list_chats_by_conversation` porque ese filtra
        `md_path IS NOT NULL` — y el `.md` se escribe al TERMINAR, así
        que no ve el run que está corriendo. Ese filtro es correcto para
        la vista de mensajes (un run sin `.md` no tiene nada que
        mostrar) y equivocado para esto, donde el run en curso es
        justamente el que interesa.

        Del más nuevo al más viejo: quien llama quiere el último que
        haya dejado un plan.

        **Las columnas importan.** Hasta el 30/8 esto traía cinco y
        `_grafo_sintetico` leía nueve: `user_prompt` (no existía),
        `phase_at_end`, `source`, `finished_at`. El resultado no era un
        error sino algo peor — un panel entero de nodos sin título, con
        los turnos que murieron por budget o timeout pintados de verde
        ("hecho") porque sin `phase_at_end` no hay fase terminal que
        mirar. Lo que la fila no trae, el panel lo inventa en silencio.
        """
        return await self.run(
            "SELECT id, status, started_at, finished_at, tool_calls, "
            "stages_json, phase_at_end, source, user_prompt "
            "FROM chats WHERE conversation_id=? "
            "ORDER BY started_at DESC LIMIT ?",
            (conversation_id, limit))
