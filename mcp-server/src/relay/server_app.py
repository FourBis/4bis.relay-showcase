"""Application composition for the Relay HTTP server."""
from __future__ import annotations

import io
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from aiohttp import web

from . import identity, logctx, server_common, tracing, voice, voice_routes
from .db import read_system_config_sync
from .server_common import (
    browser_guard,
    BIND_HOST_KEY, _require_auth, localhost_guard,
    logger, relay_config,
)
from .server_core import health, handshake, list_sessions_view, mcp_endpoint, system_active
from .server_expert_routes import experts_cancel, experts_run, experts_status, experts_steer
from .server_conversation_routes import (
    conversations_create, conversations_get, conversations_get_messages,
    conversations_list,
)
from .server_conversation_actions import (
    conversation_branch_delete, conversation_branch_status,
    conversation_diff, conversation_git_action, conversation_pr_status,
    conversation_set_discord_user, conversations_close, conversations_compact,
)
from .server_projects import (
    chats_get, chats_get_md, chats_get_status, chats_list, commands_delete,
    commands_list, commands_run, commands_upsert, project_set_discord_channel,
    projects_delete, stats_view,
)
from .server_night import (
    db_connection_delete, db_connection_test, db_connection_upsert,
    db_connections_list, night_start, night_status, night_stop,
)
from .server_graph_routes import (
    conversation_plan, graphs_cancel, graphs_create, graphs_get, graphs_list,
    graphs_resume, expert_question_answer,
    expert_questions_list,
)
from .server_questions import (
    discord_attachments_download, discord_attachments_upload,
    discord_day_answer, night_question_answer, night_question_skip,
    night_questions_list, expert_question_skip,
)
from .server_lifecycle import _on_cleanup, _on_startup
from .server_task_routes import task_action, task_get

def create_app(bind_host: str = "127.0.0.1") -> web.Application:

    # FIX: logging.basicConfig sin forzar encoding usa cp1252 en
    # Windows y revienta con emojis / acentos en logs de subprocess
    # (ej: cbm, git). Forzar utf-8 SOLO la primera vez (idempotente
    # para re-entrancy desde tests / PyInstance que llama create_app N
    # veces). Si pytest capturó stdout, `sys.stdout` es un wrapper y su
    # `.buffer` puede estar cerrado entre tests — caer a `sys.stderr`).
    if not server_common._log_utf8_configured:
        logging.basicConfig(
            level=os.environ.get("LOG_LEVEL", "INFO"),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            force=True,
        )
        # Reconfigurar el StreamHandler default para utf-8 (Windows cp1252
        # → UnicodeDecodeError al loggear emojis / acentos). Si stdout.buffer
        # está cerrado (tests con capsys), usar stderr.buffer que pytest no
        # intercepta.
        target = (sys.stdout if sys.stdout and not getattr(
            sys.stdout, "closed", False) else sys.stderr)
        buf = getattr(target, "buffer", None)
        if buf is not None and not getattr(buf, "closed", False):
            try:
                handler_unicode = logging.StreamHandler(io.TextIOWrapper(
                    buf, encoding="utf-8", errors="replace"))
                logging.getLogger().handlers = [handler_unicode]  # type: ignore[arg-type]  # noqa
            except (ValueError, OSError):
                # Stream cerrado entre tests; dejar el StreamHandler default.
                pass

        # --- Correlación por chat (2026-07-25) --------------------------
        # El filter va en los HANDLERS y no en el root logger: a nivel
        # logger solo vería los records emitidos directo contra el root,
        # y todo lo interesante propaga desde los hijos (relay.experts,
        # relay.mcp_pool, ...). Ver logctx.ChatContextFilter.
        # Va DESPUÉS de la asignación de arriba a propósito: esa línea
        # pisa la lista entera de handlers, así que cualquier cosa que
        # agreguemos antes desaparece en silencio.
        ctx_filter = logctx.ChatContextFilter()
        for _h in logging.getLogger().handlers:
            _h.addFilter(ctx_filter)

        # --- Ruido de terceros -----------------------------------------
        # Medido al prender el log a disco: de 7.700 líneas, 2.252 eran
        # de `asyncio` y 824 de `aiohttp.access` (una por request, y la
        # Admin UI poll-ea cada 5s). Para un post-mortem de "por qué
        # murió este chat" no aportan nada y hacen rotar el archivo
        # antes de que sirva. WARNING salvo que pidas lo contrario.
        for _noisy in ("asyncio", "aiohttp.access", "aiohttp.server",
                       "httpx", "httpcore", "watchdog", "mcp"):
            logging.getLogger(_noisy).setLevel(
                os.environ.get("LOG_LEVEL_LIBS", "WARNING"))

        # --- Log a disco (2026-07-25) -----------------------------------
        # Hasta hoy no se persistía nada: solo el ring buffer de 500
        # líneas en memoria de la Admin UI. Un run ocupado (248 tool
        # calls) lo desborda entero y cualquier reinicio lo borra, así
        # que un post-mortem no tenía de dónde salir. 10 MB x 5 = 50 MB
        # de techo; nunca crece sin control.
        try:
            _log_dir = relay_config.log_dir()
            _log_dir.mkdir(parents=True, exist_ok=True)
            _fh = RotatingFileHandler(
                _log_dir / "relay.log",
                maxBytes=10 * 1024 * 1024, backupCount=5,
                # Sin encoding explícito, Windows abre en cp1252 y
                # revienta con el primer emoji — el mismo bug que el
                # comentario de arriba describe para el StreamHandler.
                encoding="utf-8",
                delay=True)
            _fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s "
                "[chat=%(chat_id)s proj=%(project)s] %(message)s"))
            _fh.addFilter(ctx_filter)
            logging.getLogger().addHandler(_fh)
        except OSError as e:
            # Disco lleno, permisos, path inválido: seguimos en memoria.
            # Un log que no se puede escribir no puede tumbar el relay.
            logging.getLogger("relay.server").warning(
                "no pude abrir el log a disco (%r): sigo solo en memoria", e)

        # --- Trazas OTel (2026-08-26) ----------------------------------
        # Va acá adentro del guard a propósito: `instrument_all` es un
        # switch global de proceso, y create_app se llama N veces desde
        # los tests. Apagado por default; ver `tracing.py` para el
        # destino y la advertencia sobre contenido de prompts.
        tracing.setup_tracing()

        server_common._log_utf8_configured = True
    app = web.Application(
        # El cap real por tipo de request lo ponen los handlers; este es
        # el techo del transporte. Subido de 1 MB por /voice/transcribe
        # (audio hasta VOICE_MAX_AUDIO_BYTES, default 50 MB).
        client_max_size=voice.max_audio_bytes() + 1024 * 1024,
        # Origen/Host y peer antes de identidad: chequeos baratos y sin red.
        # access_identity después, y solo sobre lo que ya pasó el guard.
        # require_role al final: necesita la identidad ya resuelta.
        middlewares=[browser_guard, localhost_guard, identity.access_identity,
                     identity.require_role],
    )
    # El host efectivo del bind se muestra en la configuración.
    # y el endpoint /admin/api/config lo reporta a la UI.
    app[BIND_HOST_KEY] = bind_host

    # core
    app.router.add_get("/health", health)
    app.router.add_get("/system/active", system_active)
    app.router.add_get("/sessions", list_sessions_view)

    # handshake de la extensión VS Code (liveness: arma menú de
    # targets vivos en /sessions). El push por SSE ya no se ejerce.
    app.router.add_post("/agents/handshake", handshake)

    # expertos pydantic-ai (ADR-012; async ADR-024)
    app.router.add_post("/experts/run", experts_run)
    app.router.add_post("/experts/cancel/{chat_id}", experts_cancel)
    app.router.add_post("/experts/steer/{chat_id}", experts_steer)
    app.router.add_get("/experts/status/{chat_id}", experts_status)

    # conversaciones (ADR-025) — /nuevo y /cerrar del bot
    app.router.add_post("/conversations", conversations_create)
    app.router.add_get("/conversations", conversations_list)
    app.router.add_get("/conversations/{id}", conversations_get)
    app.router.add_get("/conversations/{id}/messages",
                       conversations_get_messages)
    app.router.add_post("/conversations/{id}/compact", conversations_compact)
    app.router.add_post("/conversations/{id}/close", conversations_close)
    app.router.add_get("/conversations/{id}/pr", conversation_pr_status)
    app.router.add_get("/conversations/{id}/task", task_get)
    app.router.add_post("/conversations/{id}/task", task_action)
    # Iter 10.3: ver / borrar la rama local acumulada tras /cerrar.
    app.router.add_get("/conversations/{id}/diff", conversation_diff)
    app.router.add_post("/conversations/{id}/git/{action}",
                        conversation_git_action)
    app.router.add_get("/conversations/{id}/plan", conversation_plan)
    app.router.add_get("/conversations/{id}/branch", conversation_branch_status)
    app.router.add_delete("/conversations/{id}/branch", conversation_branch_delete)
    # Iter 10.0: vincular un chat de UI a un Discord user (bridge).
    app.router.add_post(
        "/conversations/{id}/set-discord-user",
        conversation_set_discord_user)
    app.router.add_delete("/projects/{slug}", projects_delete)
    # Iter 10.1: setear/limpiar discord_channel_id por proyecto.
    app.router.add_patch(
        "/projects/{slug}/discord-channel",
        project_set_discord_channel)
    app.router.add_get("/commands", commands_list)
    app.router.add_post("/commands", commands_upsert)
    app.router.add_put("/commands/{name}", commands_upsert)
    app.router.add_delete("/commands/{name}", commands_delete)
    app.router.add_post("/commands/{name}/run", commands_run)

    # voice input (Track D / F5 — docs/VOICE_INPUT.md)
    voice_routes.register_voice_routes(app, _require_auth)
    # modo nocturno (ADR-028)
    app.router.add_post("/night-mode/start", night_start)
    app.router.add_post("/night-mode/stop", night_stop)
    app.router.add_get("/night-mode/status", night_status)
    # Iter 9.7: preguntas interactivas (responder checkpoints sin Discord).
    app.router.add_get(
        "/admin/api/night/questions", night_questions_list)
    app.router.add_get(
        "/admin/api/db-connections", db_connections_list)
    app.router.add_post(
        "/admin/api/db-connections", db_connection_upsert)
    app.router.add_delete(
        "/admin/api/db-connections/{alias}", db_connection_delete)
    app.router.add_post(
        "/admin/api/db-connections/{alias}/test", db_connection_test)
    app.router.add_post("/graphs", graphs_create)
    app.router.add_get("/graphs", graphs_list)
    app.router.add_get("/graphs/{id}", graphs_get)
    app.router.add_post("/graphs/{id}/resume", graphs_resume)
    app.router.add_post("/graphs/{id}/cancel", graphs_cancel)

    app.router.add_get("/questions", expert_questions_list)
    app.router.add_post("/questions/{q_id}/answer", expert_question_answer)
    app.router.add_post("/questions/{q_id}/skip", expert_question_skip)
    app.router.add_post(
        "/admin/api/night/questions/{q_id}/answer", night_question_answer)
    app.router.add_post(
        "/admin/api/night/questions/{q_id}/skip", night_question_skip)
    # Iter 9.8: el bot de Discord manda la respuesta del humano al relay.
    # Sin auth (mismo patrón que /notify, ver docstring del handler).
    app.router.add_post("/discord/day-answer", discord_day_answer)
    # Adjuntos: bytes de un archivo (foto, pdf, txt) que el usuario metió
    # en el canal de Discord o en el composer de la Admin UI. Sin auth.
    # `/attachments` es el nombre canónico desde 2026-07-31 (la UI también
    # sube); `/discord/attachments` queda como alias — el bot ya
    # desplegado apunta ahí y romperlo no compra nada.
    app.router.add_post("/attachments", discord_attachments_upload)
    app.router.add_get("/attachments/{attach_id}",
                       discord_attachments_download)
    app.router.add_post("/discord/attachments", discord_attachments_upload)
    app.router.add_get("/discord/attachments/{attach_id}",
                       discord_attachments_download)

    # chats (índice read-only) + stats
    app.router.add_get("/chats", chats_list)
    app.router.add_get("/chats/{id}", chats_get)
    app.router.add_get("/chats/{id}/md", chats_get_md)
    app.router.add_get("/chats/{id}/status", chats_get_status)
    app.router.add_get("/stats", stats_view)

    # MCP (Iter 4: Google tools reales, hoy mocked)
    app.router.add_post("/mcp", mcp_endpoint)

    # Admin UI (ADR-014 / docs/ADMIN_UI_SPEC.md).
    # Mismo proceso y puerto (:8413), bind 127.0.0.1.
    from . import admin as admin_module  # import lazy para no romper tests
    admin_module.register_admin_routes(app)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def main() -> None:
    # FIX Windows: forzar stdout/stderr a utf-8 a nivel de proceso ANTES
    # de crear loggers / spawnar subprocess. Sin esto, cp1252 revienta
    # al loggear emojis / acentos (cbm CLI devuelve UTF-8 con símbolos
    # → UnicodeDecodeError en el reader thread). Es seguro en otras
    # plataformas: stdout.buffer existe en todas, solo cambia el codec.
    for _stream_name in ("stdout", "stderr"):
        _stream = getattr(sys, _stream_name, None)
        if _stream is not None and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — best-effort
                pass
    from . import config as relay_config
    # Bind host: system_config (SQLite, editable desde la Admin UI) manda;
    # MCP_HOST (env/.env) es fallback para el primer arranque con la
    # tabla vacía; último default: loopback.
    host = (read_system_config_sync("RELAY_HOST", "").strip()
            or os.environ.get("MCP_HOST", "").strip()
            or "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8413"))
    app = create_app(bind_host=host)
    if host == "0.0.0.0":
        logger.warning(
            "RELAY_HOST=0.0.0.0: el relay queda expuesto a la LAN. "
            "/admin/* y /api/* siguen restringidos a localhost "
            "(localhost_guard activo).")
    web.run_app(app, host=host, port=port, access_log=None)


if __name__ == "__main__":
    main()
