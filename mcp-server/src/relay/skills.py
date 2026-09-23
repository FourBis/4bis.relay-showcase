"""Fachada compatible del subsistema de skills.

La implementación está separada en índice/render, almacenamiento local y
navegación/instalación de repositorios. Este módulo solo compone exports.
"""
from __future__ import annotations

from . import skills_browser, skills_index, skills_store

from .skills_index import (  # noqa: F401
    CACHE_TTL_S, SKILLS_DIR, _FRONTMATTER_RE, _KV_RE, Skill, _Index,
    resolve_skills_dir, _unquote, _parse_skill_md, _build_index, skills_dirs,
    build_index_multi, read_skill_multi, render_ondemand_line,
    ONDEMAND_HEADER, render_ondemand_block, render_block, bloque_del_proyecto,
    SKILL_INDEX_HEADER_COMPACT, render_index_compact, REQUESTED_SKILL_CAP,
    render_requested_block,
)
from .skills_store import (  # noqa: F401
    SkillCache, sanitize_skill_name, _safe_skill_dir, render_skill_md,
    write_skill_sync, list_skills_sync, resolve_skill_dir, _read_skill_md,
    read_skill_sync, overwrite_skill_sync, delete_skill_sync, SKILL_STATES,
    skill_state, _set_frontmatter_keys, set_skill_state_sync,
    set_skill_enabled_sync, estimate_tokens, skills_token_budget,
    prompt_token_budget,
)
from .skills_browser import (  # noqa: F401
    GITHUB_CATALOG, _SCAN_MAX_DEPTH, _SCAN_SKIP_DIRS, SKILL_MAX_BYTES,
    _dir_bytes, scan_repo_skills, read_skill_md_from_clone, _resolve_in_clone,
    install_skill_from_clone, skill_installs_root, BrowseJob, _JOB_TTL_S,
    SkillBrowser, _rmtree_quiet,
)
