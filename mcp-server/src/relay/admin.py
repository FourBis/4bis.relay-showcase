"""Composición de las rutas REST de la Admin UI.

Los handlers viven en módulos por responsabilidad; este módulo conserva el
entrypoint histórico y el orden de registro de aiohttp.
"""
from __future__ import annotations

from aiohttp import web
from pydantic import BaseModel  # noqa: F401
from pydantic_ai import Agent  # noqa: F401

from .app_state import (  # noqa: F401
    COMMANDS_KEY, DB_KEY, NIGHT_KEY, NOTIFY_KEY, SESSIONS_KEY,
    SKILL_BROWSER_KEY, SKILLS_KEY,
)
from . import admin_cbm, admin_commands, admin_config, admin_crm, admin_diagrams
from . import admin_github, admin_indexing, admin_mcp, admin_memory
from . import admin_observability, admin_project_lifecycle, admin_projects
from . import admin_runs, admin_skill_drafts, admin_skills, admin_workspace
from .experts import ModelUnavailable, build_model, cbm_binary_path  # noqa: F401
from .admin_config_models import _roles_que_usan, api_model_test  # noqa: F401
from .admin_common import (
    _safe_git_remote_url,  # noqa: F401
    _get_job, _git_remote_url, _has_git, _remote_url_cache, _serialize,
    _set_job, mark_job_done,
)

# API interna histórica: estos nombres siguen siendo importables desde
# relay.admin, pero el código vive en el dueño cohesivo correspondiente.
from .admin_cbm import (  # noqa: F401
    _cbm_env, _cbm_cli_text, _cbm_cli_text_subprocess, _split_row,
    _parse_cbm_sections, _cbm_list_projects, _read_cbm_cache_index,
    _cbm_indexed_count, _invalidate_cbm_projects_cache, _mark_job_done,
    _norm_path_key, _cbm_project_name, _cbm_projects_cache, _cbm_projects_at,
)
from .admin_observability import (  # noqa: F401
    admin_index, admin_static, api_bot_status, api_bot_start, api_health,
    api_report, api_metrics_summary, api_metrics_trends, api_search, api_logs,
    _install_ring_handler, ADMIN_STATIC_DIR, ADMIN_INDEX, _LOG_BUFFER,
)
from .admin_projects import (  # noqa: F401
    api_projects, api_projects_slug, api_project_git_remote,
    api_project_system_prompt, api_project_night, api_project_cbm,
    api_projects_patch, api_projects_set_discord_channel, api_projects_delete,
)
from .admin_memory import (  # noqa: F401
    api_facts_list, api_project_git_diff, api_memories_search, api_fact_delete,
    api_fact_status, api_fact_create, api_conversation_extract_facts,
    api_memory_delete,
)
from .admin_skills import (  # noqa: F401
    api_skills_list, api_skills_get, api_skills_put, api_skills_delete,
    api_skills_patch, api_skills_budget, api_instructions_get,
    api_instructions_put, api_skills_catalog, api_skills_browse_start,
    api_skills_browse_status, api_skills_browse_preview,
    api_skills_browse_install, api_skills_browse_discard,
)
from .admin_skill_drafts import (  # noqa: F401
    api_apply_skill_to_transcript, api_skill_drafts_list, api_skill_draft_get,
    api_skill_draft_patch, api_skill_draft_approve, api_skill_draft_reject,
    api_skill_draft_delete,
)
from .admin_workspace import (  # noqa: F401
    _is_text_file, _safe_repo_path, api_workspace_files, api_workspace_file_get,
    api_workspace_file_put, api_workspace_scaffold, _start_index_job,
    api_reindex_post, api_reindex_status,
)
from .admin_github import (  # noqa: F401
    _looks_like_repo, api_project_github, api_github_board,
    _merge_defaults, _flags_state, _validar_flag, api_project_flags_get,
    api_project_flags_patch, api_project_github_link, api_project_github_create,
    api_github_boards, api_fs_browse,
    _PROJECT_FLAGS, _MODEL_FLAG_GLOBAL,
)
from .admin_diagrams import (  # noqa: F401
    _parse_cbm_search_graph, api_project_architecture, _parse_cbm_grouped,
    api_project_graph, _build_diagram_system_prompt, _extract_mermaid,
    api_project_diagrams_llm,
)
from .admin_indexing import api_index_files, api_index_bulk, api_index_status  # noqa: F401
from .admin_commands import (  # noqa: F401
    api_commands_list, api_commands_upsert, api_commands_delete,
    api_commands_run, api_cbm_orphans, api_orphan_ignore,
    api_orphan_unignore, api_orphan_ignored_list, api_admin_restart, api_me,
    api_users_list, api_user_upsert, api_user_delete,
)
from .admin_project_lifecycle import (  # noqa: F401
    _slugify, _new_project_row, api_project_create, api_project_from_cbm,
    api_project_open_vscode, api_night_templates, api_night_template_save,
    api_night_template_delete,
)
from .admin_runs import (  # noqa: F401
    api_zombies_list, api_chat_delete, api_notes_create, api_notes_list,
    api_night_runs_list, api_night_run_detail, api_night_run_report,
    api_project_expert_run,
)


def register_admin_routes(app: web.Application) -> None:
    """Registra todas las rutas /admin/* en el orden contractual."""
    admin_observability._install_ring_handler()
    from . import account_oauth, user_mail
    account_oauth.register_routes(app)
    user_mail.register_routes(app)
    app.router.add_get("/admin/api/me", admin_commands.api_me)
    app.router.add_get("/admin/api/users", admin_commands.api_users_list)
    app.router.add_put("/admin/api/users", admin_commands.api_user_upsert)
    app.router.add_delete("/admin/api/users/{email}", admin_commands.api_user_delete)
    admin_config.register_model_routes(app)
    app.router.add_get("/admin/api/night/templates", admin_project_lifecycle.api_night_templates)
    app.router.add_put("/admin/api/night/templates", admin_project_lifecycle.api_night_template_save)
    app.router.add_delete("/admin/api/night/templates/{nombre}", admin_project_lifecycle.api_night_template_delete)
    app.router.add_get("/admin/", admin_observability.admin_index)
    app.router.add_get("/admin", admin_observability.admin_index)
    app.router.add_get("/admin/static/{filename}", admin_observability.admin_static)
    app.router.add_get("/admin/api/health", admin_observability.api_health)
    app.router.add_get("/admin/api/bot/status", admin_observability.api_bot_status)
    app.router.add_post("/admin/api/bot/start", admin_observability.api_bot_start)
    app.router.add_get("/admin/api/report", admin_observability.api_report)
    app.router.add_get("/admin/api/metrics/summary", admin_observability.api_metrics_summary)
    app.router.add_get("/admin/api/metrics/trends", admin_observability.api_metrics_trends)
    app.router.add_get("/admin/api/logs", admin_observability.api_logs)
    app.router.add_get("/admin/api/search", admin_observability.api_search)
    admin_config.register_timeout_routes(app)
    app.router.add_get("/admin/api/projects", admin_projects.api_projects)
    app.router.add_post("/admin/api/projects", admin_project_lifecycle.api_project_create)
    app.router.add_get("/admin/api/projects/{slug}", admin_projects.api_projects_slug)
    app.router.add_patch("/admin/api/projects/{slug}", admin_projects.api_projects_patch)
    app.router.add_delete("/admin/api/projects/{slug}", admin_projects.api_projects_delete)
    app.router.add_put("/admin/api/projects/{slug}/git-remote", admin_projects.api_project_git_remote)
    app.router.add_patch("/admin/api/projects/{slug}/discord-channel", admin_projects.api_projects_set_discord_channel)
    app.router.add_get("/admin/api/projects/{slug}/workspace/files", admin_workspace.api_workspace_files)
    app.router.add_get("/admin/api/projects/{slug}/workspace/file", admin_workspace.api_workspace_file_get)
    app.router.add_put("/admin/api/projects/{slug}/workspace/file", admin_workspace.api_workspace_file_put)
    app.router.add_post("/admin/api/projects/{slug}/workspace/scaffold", admin_workspace.api_workspace_scaffold)
    app.router.add_get("/admin/api/projects/{slug}/night", admin_projects.api_project_night)
    app.router.add_get("/admin/api/projects/{slug}/cbm", admin_projects.api_project_cbm)
    app.router.add_get("/admin/api/projects/{slug}/system-prompt", admin_projects.api_project_system_prompt)
    app.router.add_get("/admin/api/projects/{slug}/git-diff", admin_memory.api_project_git_diff)
    app.router.add_post("/admin/api/projects/{slug}/expert-run", admin_runs.api_project_expert_run)
    app.router.add_get("/admin/api/night-runs/{run_id}/report", admin_runs.api_night_run_report)
    app.router.add_get("/admin/api/chats/zombies", admin_runs.api_zombies_list)
    app.router.add_delete("/admin/api/chats/{chat_id}", admin_runs.api_chat_delete)
    app.router.add_get("/admin/api/notes", admin_runs.api_notes_list)
    app.router.add_post("/admin/api/notes", admin_runs.api_notes_create)
    app.router.add_get("/admin/api/night-runs", admin_runs.api_night_runs_list)
    app.router.add_get("/admin/api/night-runs/{run_id}", admin_runs.api_night_run_detail)
    app.router.add_get("/admin/api/conversations/facts", admin_memory.api_facts_list)
    app.router.add_get("/admin/api/conversations/memories", admin_memory.api_memories_search)
    app.router.add_post("/admin/api/conversations/facts", admin_memory.api_fact_create)
    app.router.add_patch("/admin/api/facts/{id}", admin_memory.api_fact_status)
    app.router.add_post("/admin/api/conversations/{conv_id}/extract-facts", admin_memory.api_conversation_extract_facts)
    app.router.add_delete("/admin/api/facts/{id}", admin_memory.api_fact_delete)
    app.router.add_delete("/admin/api/conversations/{conv_id}/memory", admin_memory.api_memory_delete)
    app.router.add_get("/admin/api/skills", admin_skills.api_skills_list)
    app.router.add_get("/admin/api/skills/budget", admin_skills.api_skills_budget)
    app.router.add_get("/admin/api/skills/catalog", admin_skills.api_skills_catalog)
    app.router.add_post("/admin/api/skills/browse", admin_skills.api_skills_browse_start)
    app.router.add_get("/admin/api/skills/browse/{job_id}", admin_skills.api_skills_browse_status)
    app.router.add_get("/admin/api/skills/browse/{job_id}/preview", admin_skills.api_skills_browse_preview)
    app.router.add_post("/admin/api/skills/browse/{job_id}/install", admin_skills.api_skills_browse_install)
    app.router.add_delete("/admin/api/skills/browse/{job_id}", admin_skills.api_skills_browse_discard)
    app.router.add_get("/admin/api/instructions", admin_skills.api_instructions_get)
    app.router.add_put("/admin/api/instructions", admin_skills.api_instructions_put)
    app.router.add_get("/admin/api/skills/{name}", admin_skills.api_skills_get)
    app.router.add_put("/admin/api/skills/{name}", admin_skills.api_skills_put)
    app.router.add_patch("/admin/api/skills/{name}", admin_skills.api_skills_patch)
    app.router.add_delete("/admin/api/skills/{name}", admin_skills.api_skills_delete)
    app.router.add_post("/admin/api/skills/{name}/apply-to-transcript", admin_skill_drafts.api_apply_skill_to_transcript)
    app.router.add_get("/admin/api/skill-drafts", admin_skill_drafts.api_skill_drafts_list)
    app.router.add_get("/admin/api/skill-drafts/{id}", admin_skill_drafts.api_skill_draft_get)
    app.router.add_patch("/admin/api/skill-drafts/{id}", admin_skill_drafts.api_skill_draft_patch)
    app.router.add_post("/admin/api/skill-drafts/{id}/approve", admin_skill_drafts.api_skill_draft_approve)
    app.router.add_post("/admin/api/skill-drafts/{id}/reject", admin_skill_drafts.api_skill_draft_reject)
    app.router.add_delete("/admin/api/skill-drafts/{id}", admin_skill_drafts.api_skill_draft_delete)
    app.router.add_post("/admin/api/projects/{slug}/reindex", admin_workspace.api_reindex_post)
    app.router.add_post("/admin/api/projects/{slug}/open-vscode", admin_project_lifecycle.api_project_open_vscode)
    app.router.add_post("/admin/api/projects/from-cbm", admin_project_lifecycle.api_project_from_cbm)
    app.router.add_post("/admin/api/projects/index/bulk", admin_indexing.api_index_bulk)
    app.router.add_get("/admin/api/projects/{slug}/index/status", admin_indexing.api_index_status)
    app.router.add_get("/admin/api/projects/{slug}/index/files", admin_indexing.api_index_files)
    app.router.add_get("/admin/api/github/board", admin_github.api_github_board)
    app.router.add_get("/admin/api/github/boards", admin_github.api_github_boards)
    app.router.add_get("/admin/api/projects/{slug}/github", admin_github.api_project_github)
    app.router.add_put("/admin/api/projects/{slug}/github-project", admin_github.api_project_github_link)
    app.router.add_post("/admin/api/projects/{slug}/github-project", admin_github.api_project_github_create)
    app.router.add_get("/admin/api/projects/{slug}/flags", admin_github.api_project_flags_get)
    app.router.add_patch("/admin/api/projects/{slug}/flags", admin_github.api_project_flags_patch)
    app.router.add_get("/admin/api/projects/{slug}/architecture", admin_diagrams.api_project_architecture)
    app.router.add_get("/admin/api/projects/{slug}/graph/{kind}", admin_diagrams.api_project_graph)
    app.router.add_post("/admin/api/projects/{slug}/diagrams/llm", admin_diagrams.api_project_diagrams_llm)
    app.router.add_get("/admin/api/reindex/{job_id}", admin_workspace.api_reindex_status)
    app.router.add_get("/admin/api/fs/browse", admin_github.api_fs_browse)
    admin_config.register_settings_routes(app)
    app.router.add_get("/admin/api/cbm/orphans", admin_commands.api_cbm_orphans)
    app.router.add_get("/admin/api/cbm/orphans/ignored", admin_commands.api_orphan_ignored_list)
    app.router.add_post("/admin/api/cbm/orphans/ignore", admin_commands.api_orphan_ignore)
    app.router.add_delete("/admin/api/cbm/orphans/ignore", admin_commands.api_orphan_unignore)
    app.router.add_get("/admin/api/commands", admin_commands.api_commands_list)
    app.router.add_post("/admin/api/commands", admin_commands.api_commands_upsert)
    app.router.add_post("/admin/api/commands/{name}/run", admin_commands.api_commands_run)
    app.router.add_delete("/admin/api/commands/{name}", admin_commands.api_commands_delete)
    admin_mcp.register_routes(app)
    admin_crm.register_routes(app)
    app.router.add_post("/admin/api/restart", admin_commands.api_admin_restart)
