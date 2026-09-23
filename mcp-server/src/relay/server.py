"""Entrypoint público del servidor Relay.

La composición vive en :mod:`server_app`; este módulo conserva el contrato
histórico de imports para scripts y tests, con reexportes explícitos.
"""
from __future__ import annotations

from . import (
    attachments as attachments_mod,
    config as relay_config,
    bot_control,
    coordination,
    experts,
    finalization,
    git_flow,
    grafo as grafo_mod,
    github as github_mod,
    identity,
    logctx,
    mcp_pool,
    memory,
    persist,
    tracing,
    voice,
    voice_routes,
)
from .app_state import (
    BG_TASKS_KEY, BIND_HOST_KEY, CBM_WARMUP_KEY, CBM_WATCHER_KEY,
    COMMANDS_KEY, DB_KEY, EXPORT_RETRY_KEY, GRAFOS_KEY,
    MCP_HEALTH_PROBE_KEY, MCP_INSTALLER_KEY, MCP_POOL_KEY, MCP_REAPER_KEY,
    NIGHT_KEY, NOTIFY_KEY, PROGRESS_KEY, RUNNING_KEY, SESSIONS_KEY,
    SKILLS_KEY, SKILL_BROWSER_KEY, STATE_DIR_KEY, SWEEPER_KEY,
)
from .commands import CommandContext, CommandRegistry, UnknownCommand
from .db import Database, read_system_config_sync
from .mcp_installer import McpInstaller
from .mcp_pool import McpPool
from .notify import NotifyClient, default_bot_url
from .sessions import validate_name, validate_sid
from .skills import SkillBrowser, SkillCache
from .tools import _registry as tool_registry
from .server_common import (
    browser_guard, _loopback_peer,
    LAN_SAFE_EXACT, LAN_SAFE_PREFIXES, SessionRegistry, _LOCALHOST_PEERS,
    _bg_tasks, _check_auth, _get_api_key, _is_lan_safe, _log_utf8_configured,
    _require_auth, _rpc_error, _spawn_bg, localhost_guard, logger,
    RELAY_VERSION,
)
from .server_app import create_app, main
from .server_core import (
    handshake, health, list_sessions_view, mcp_endpoint, system_active,
)
from .server_expert_jobs import (
    _render_pregunta, _request_bot_create_thread, _resultado_del_trabajo,
    _run_expert_bg, _stages_json, _suggest_followups,
    _RESULTADO_POR_VERDICT,
    SUGGEST_TIMEOUT_S,
)
from .server_expert_routes import (
    experts_cancel, experts_run, experts_status, experts_steer,
)
from .server_conversation_jobs import (
    _COMPACTING, _PR_JOBS, _autoclose_sweeper, _compact_and_store,
    _export_retry_loop, _finalize_pr_bg, _horas_legibles,
    EXPORT_RETRY_INTERVAL_S, SWEEP_INTERVAL_S,
)
from .server_conversation_routes import (
    conversations_create, conversations_get, conversations_get_messages,
    conversations_list,
)
from .server_conversation_helpers import (
    _STEP_MAX, _STEP_MSG_CAP, _STEP_PHASES, _cap_text,
    _steps_from_progress, compact_live_conversation,
)
from .server_conversation_actions import (
    conversation_branch_delete, conversation_branch_status,
    conversation_diff, conversation_git_action, conversation_pr_status,
    conversation_set_discord_user, conversations_close, conversations_compact,
)
from .server_projects import (
    chats_get, chats_get_md, chats_get_status, chats_list, commands_delete,
    commands_list, commands_run, commands_upsert, project_set_discord_channel,
    projects_delete, projects_get, projects_list, projects_upsert, stats_view,
)
from .server_night import (
    _grafo_en_vez_de_proponer, _grafo_estancado, db_connection_delete,
    db_connection_test, db_connection_upsert, db_connections_list,
    night_start, night_status, night_stop,
)
from .server_graph_helpers import (
    _CAP_NODOS_SINTETICOS, _PHASE_FALLIDO, _PLANIFICANDO, _STATUS_FALLIDO,
    _correr_grafo_bg, _costo_en_nodos, _detalle_del_turno,
    _estado_del_paso, _estado_del_turno, _grafo_publico, _grafo_sintetico,
    _largar_grafo, _stages_de, _tiene_pregunta_abierta,
    _titulo_de_respaldo, _verificacion_publica,
)
from .server_graph_routes import (
    _ETAPA, _retomar_grafo_tras_respuesta, conversation_plan, expert_question_answer,
    expert_questions_list, graphs_cancel, graphs_create, graphs_get,
    graphs_list, graphs_resume,
)
from .server_questions import (
    _ATT_ID_RE, discord_attachments_download, discord_attachments_upload,
    discord_day_answer, expert_question_skip, night_question_answer,
    night_question_skip, night_questions_list,
)
from .server_lifecycle import (
    _avisar_globales_apagados, _drain_running_experts, _frenar_productores,
    _on_cleanup, _on_startup, _probe_external_mcps_health, _reap_zombie_chats,
    _warm_cbm_session, _DRAIN_RUNNING_TIMEOUT_S,
)

if __name__ == "__main__":
    main()
