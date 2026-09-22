"""Rutas del catálogo de proyectos y su configuración operativa."""
from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Mapping
from aiohttp import web
from . import config as relay_config
from . import admin_cbm, identity
from . import github as github_mod
from .experts import cbm_binary_path
from .app_state import DB_KEY, NIGHT_KEY, SKILLS_KEY
from .admin_common import _safe_git_remote_url, _git_remote_url, _has_git, _remote_url_cache, _serialize

from .admin_cbm import (
    _CBM_PROJECTS_TIMEOUT_S, _cbm_list_projects, _cbm_project_name,
    _norm_path_key,
)

logger = logging.getLogger("relay.admin")

async def api_projects(request: web.Request) -> web.Response:
    """GET /admin/api/projects — lista con estado de index."""
    db = request.app[DB_KEY]
    projects = await db.list_projects(enabled_only=False)
    indexed: dict[str, dict] = {}
    try:
        # Bug 2026-07-22: el listado NO puede colgarse por las stats de
        # index. El spawn de cbm se va a 20s+ cuando el watcher tiene el
        # store lockeado (ver _cbm_indexed_count) y el front corta a los
        # 15s → la sidebar de Chats quedaba en "el server no respondió"
        # y la lista de conversaciones ni se pedía. Degradamos a lo
        # último cacheado y seguimos: el resto del payload es sqlite.
        try:
            cbm_resp = await asyncio.wait_for(
                _cbm_list_projects(), timeout=_CBM_PROJECTS_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(
                "admin/projects: cbm tardó >%ss (store lockeado?), sirvo "
                "sin stats de index", _CBM_PROJECTS_TIMEOUT_S)
            cbm_resp = admin_cbm._cbm_projects_cache or {}
        for c in cbm_resp.get("projects", []):
            # Match por root_path completo normalizado, NO por basename:
            # dos repos llamados igual en carpetas distintas colisionan.
            root = _norm_path_key(c.get("root_path", ""))
            if root:
                indexed[root] = {
                    "nodes": c.get("nodes"),
                    "edges": c.get("edges"),
                    "size_bytes": c.get("size_bytes"),
                }
    except Exception as e:  # noqa: BLE001
        logger.warning("admin/projects: cbm fail: %r", e)

    # Cliente CRM de cada fila. `_parse_project` ya trae `client_id`
    # (hace SELECT *), pero este listado lo descartaba al armar la
    # respuesta, así que la grid no tenía forma de mostrar el dueño del
    # proyecto. Los nombres se resuelven en un solo SELECT y no de a uno.
    client_names = await db.client_name_by_id()

    out = []
    for p in projects:
        rp = p.get("repo_path", "")
        idx = indexed.get(_norm_path_key(rp), {})
        cid = p.get("client_id")
        out.append({
            "slug": p["slug"],
            "name": p["name"],
            "client_id": cid,
            "client_name": client_names.get(cid) if cid else None,
            "repo_path": rp,
            "enabled": bool(p.get("enabled", 1)),
            "include_in_index": bool(p.get("include_in_index", 1)),
            "description": p.get("description"),
            "indexed": bool(idx),
            "index_stats": idx or None,
            "native_tools": p.get("native_tools", []),
            "has_git": _has_git(Path(rp)) if rp else False,
            "git_remote_url": _git_remote_url(rp) if rp else None,
            "night_mode_enabled": bool(p.get("night_mode_enabled", 0)),
            # El listado lo omitía → la grid mostraba "sin canal" aunque
            # estuviera seteado (el detalle sí lo devolvía). La columna 📡
            # lo lee de acá.
            "discord_channel_id": p.get("discord_channel_id"),
            # Tablero vinculado (ADR-036). Va en el listado y no en una
            # request aparte: con 51 proyectos, saber cuáles tienen
            # tablero no puede costar 51 fetches. Sale de defaults_json,
            # que ya está en memoria acá.
            "github_project": (p.get("defaults_json") or {}).get(
                "github_project"),
            # Marcado como "nunca va a tener tablero": sale de la lista
            # de pendientes del tab Gestión sin desaparecer del sistema.
            "github_project_skip": bool(
                (p.get("defaults_json") or {}).get("github_project_skip")),
        })
    owner = identity.role_of(request) == identity.OWNER_ROLE
    if not owner:
        out = [_member_project_metadata(p) for p in out]
    # discord_guild_id (system_config): el front lo usa para linkear el
    # canal de cada fila. Va una vez a nivel top, no por proyecto.
    return web.json_response({
        "projects": _serialize(out),
        "discord_guild_id": relay_config.discord_guild_id() if owner else None,
    })

async def api_projects_slug(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug} — detalle."""
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    data = dict(project)
    rp = project.get("repo_path", "")
    data["git_remote_url"] = _git_remote_url(rp)
    # has_git: el detalle lo omitía (solo lo mandaba el listado), así que
    # el panel de admin no sabía si el repo era git → la sección de remoto
    # caía en "no es repo git" aunque lo fuera.
    data["has_git"] = _has_git(Path(rp)) if rp else False
    if identity.role_of(request) != identity.OWNER_ROLE:
        data = _member_project_metadata(data)
    return web.json_response({"project": _serialize(data)})

async def api_project_git_remote(request: web.Request) -> web.Response:
    """PUT /admin/api/projects/{slug}/git-remote {url} — vincula o cambia
    el remote `origin` del repo local (git remote set-url/add).

    Solo GitHub: validamos que la URL parsee a owner/repo (https o ssh)
    porque el panel de seguimiento deriva de ahí y un typo dejaría el
    origin roto sin aviso. La escritura vive en github.set_remote.
    """
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    url = (body.get("url") or "").strip()
    if not url:
        return web.json_response({"error": "url requerida"}, status=400)
    repo = github_mod._parse_remote(url)
    if repo is None:
        return web.json_response(
            {"error": "la URL no parece un repo de GitHub "
             "(https://github.com/owner/repo o git@github.com:owner/repo)"},
            status=400)
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    repo_path = project.get("repo_path") or ""
    if not _has_git(Path(repo_path)):
        return web.json_response(
            {"error": "el proyecto no es un repo git (no tiene .git); "
             "inicializá git primero"}, status=409)
    ok, motivo = await github_mod.set_remote(repo_path, url)
    if not ok:
        return web.json_response(
            {"error": f"git remote falló: {motivo}" if motivo
             else "git remote falló"}, status=500)
    # Invalidar el cache del read-path (mtime del .git/config puede no
    # cambiar de forma perceptible entre lecturas rápidas).
    _remote_url_cache.pop(repo_path, None)
    return web.json_response({"ok": True, "url": _safe_git_remote_url(url), "repo": repo})

async def api_project_system_prompt(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug}/system-prompt — system_prompt efectivo.

    Reproduce EXACTAMENTE lo que el LLM vio en su `instructions=`:
    1. Ponytail (filosofía del usuario, best-effort)
    2. projects.system_prompt (lo específico del repo)
    3. Índice de skills (ADR-010, vía SkillCache)
    4. Bloque workspace (ADR-011, desde repo_path)
    5. Bloque git diff (ADR-020, si repo_path es git) — capturado en
       to_thread para no bloquear el loop

    Devuelve los bloques por separado + el texto ensamblado, para que
    la UI pueda colapsar/copiar cada parte individualmente. El git diff
    puede ser pesado (hasta 20KB); mandarlo separado permite que el front
    muestre un preview + "ver completo" sin tragar el body entero.

    Sub-ola 2.3 — server-side replica la lógica de experts.run_expert.
    """
    from . import experts, sessions
    from .skills import SkillCache

    db = request.app[DB_KEY]
    skills: SkillCache = request.app[SKILLS_KEY]
    slug = request.match_info["slug"]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)

    # Reusamos exactamente las mismas primitivas que run_expert.
    # Si cambian allá, este endpoint queda desfasado — pero el bloque
    # ya está testeado (test_experts) y el contrato está documentado
    # en ADR-020 + comments de build_instructions.
    ponytail = await experts.read_ponytail()
    defaults = project.get("defaults_json") or {}
    skills_mode = defaults.get("skills_mode", "embed")
    skills_block = ""
    try:
        if skills_mode == "compact":
            from .skills import _build_index, render_index_compact, resolve_skills_dir
            dir_ = resolve_skills_dir()
            idx = _build_index(dir_)
            # render_index_compact ya devuelve "" si no hay nada; el
            # guard extra por auto_skills() escondía el preview cuando
            # todas las skills eran on-demand (`when: manual`).
            skills_block = render_index_compact(idx)
        else:
            skills_block = await skills.get_block()
    except Exception as e:  # noqa: BLE001 — best-effort, mismo fallback que _run_expert_bg
        logger.warning("system-prompt: skills.get_block rompio: %r", e)
    workspace_block = sessions.extract_workspace_block(
        [{"path": project["repo_path"], "name": project["slug"]}]
    )

    # 2026-07-28: el bloque de git diff YA NO va en el system prompt
    # (rompía la cache del provider en cada resume — ver
    # `build_instructions`). El experto lo pide con la tool `git_diff()`,
    # así que este preview no lo muestra: mostraría un bloque que el
    # modelo no recibe.
    blocks = {
        "ponytail": ponytail or "",
        "system_prompt": project.get("system_prompt", "") or "",
        "skills": skills_block or "",
        "workspace": workspace_block or "",
        # 2026-07-20: build_instructions cierra con el bloque de
        # fallback de tools; sin él este replica queda desfasado.
        "tool_fallback": experts.TOOL_FALLBACK_BLOCK,
        # 2026-08-17: idem con la regla de lotes. El test de
        # replicabilidad de test_system_prompt.py es el que avisa cuando
        # este dict se queda atrás de build_instructions.
        "batch_artifacts": experts.BATCH_ARTIFACTS_BLOCK,
        # 2026-08-17: la bitácora. Acá va solo el bloque de
        # instrucciones (el que se paga siempre); los hechos que el run
        # va anotando viajan aparte, en las instructions dinámicas.
        "bitacora": experts.BITACORA_BLOCK,
    }
    assembled = "\n\n".join(v for v in blocks.values() if v)
    return web.json_response({
        "slug": slug,
        "blocks": blocks,
        "assembled": assembled,
        "stats": {k: len(v) for k, v in blocks.items()} | {"total": len(assembled)},
    })

async def api_project_night(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug}/night — estado del modo nocturno.

    Devuelve el flag + config + últimas corridas (tabla night_runs) para
    que el tab Proyectos muestre el estado sin arrancar nada. El
    start/stop/status en vivo van por los endpoints raíz /night-mode/*
    (ADR-028) que UI y CLI comparten.
    """
    from . import night as night_mod

    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    cfg = night_mod.NightConfig.from_project(project)
    runs = await db.list_night_runs(project["slug"], limit=5)
    active = next((r for r in runs if not r.get("ended_at")), None)
    # Enriquecer el run activo con el snapshot VIVO del orquestador en
    # memoria (current_task + bloque `live`: step/idle_s/tool del experto).
    # La fila de la DB sólo tiene id/deadline; la observabilidad async
    # vive en el registry NIGHT_KEY. Sin esto el modal 🌙 no muestra en
    # qué anda el run mientras corre (que es cuando se mira de noche).
    if active is not None:
        registry = request.app.get(NIGHT_KEY) or {}
        entry = registry.get(active["id"])
        if entry is not None:
            try:
                active = {**active, **entry[0].snapshot()}
            except Exception:  # noqa: BLE001 — el modal no debe romperse
                pass
    return web.json_response({
        "slug": project["slug"],
        "night_mode_enabled": bool(project.get("night_mode_enabled")),
        "config": {
            "build_cmd": cfg.build_cmd,
            "test_cmd": cfg.test_cmd,
            "max_diff_lines": cfg.max_diff_lines,
            "discord_channel": cfg.discord_channel,
            "base_branch": cfg.base_branch,
        },
        "active_run": active,
        "runs": runs,
    })

async def api_project_cbm(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug}/cbm — drilldown del índice cbm.

    Devuelve las stats del proyecto en cbm (nodes/edges/size_bytes)
    más un sample de tipos de nodos y el nombre canónico que cbm le
    da al proyecto (útil para debuggear el mapeo repo_path → cbm name).
    La UI usa esto para mostrar el modal "📊 cbm" con drilldown +
    botón "reindex" que dispara POST /admin/api/projects/{slug}/reindex.
    """
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    rp = project["repo_path"]
    cbm_name = _cbm_project_name(rp) if rp else ""
    installed = cbm_binary_path() is not None

    stats: dict = {}
    node_types: list[dict] = []
    note = ""
    if installed and rp:
        try:
            data = await _cbm_list_projects()
            norm = _norm_path_key(rp)
            for c in data.get("projects", []):
                if _norm_path_key(c.get("root_path", "")) == norm:
                    stats = {
                        "nodes": c.get("nodes"),
                        "edges": c.get("edges"),
                        "size_bytes": c.get("size_bytes"),
                    }
                    break
        except Exception as e:  # noqa: BLE001
            note = f"cbm cli error: {e!r}"
    return web.json_response({
        "slug": slug,
        "repo_path": rp,
        "cbm_project_name": cbm_name,
        "installed": installed,
        "stats": stats,
        "node_types": node_types,
        "note": note,
    })

async def api_projects_patch(request: web.Request) -> web.Response:
    """PATCH /admin/api/projects/{slug} — edita nombre/desc/ruta."""
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    editable = {"name", "description", "repo_path", "system_prompt",
                "enabled", "include_in_index", "night_mode_enabled",
                "night_config", "discord_channel_id"}  # Iter 10.1
    for field in editable:
        if field in body:
            if field in ("name", "description", "repo_path", "system_prompt"):
                if not isinstance(body[field], str):
                    continue
            elif field in ("enabled", "include_in_index", "night_mode_enabled"):
                if not isinstance(body[field], bool):
                    continue
            elif field == "night_config":
                if not isinstance(body[field], dict):
                    continue
            elif field == "discord_channel_id":
                # Iter 10.1: null explícito limpia; string vacío se
                # ignora (la UI manda el campo siempre — si quedó vacío,
                # NO queremos pisar el valor existente con NULL por
                # accidente; usamos endpoint dedicado para clear).
                if body[field] is None:
                    project[field] = None
                elif isinstance(body[field], str) and body[field].strip():
                    project[field] = body[field].strip()
                continue
            project[field] = body[field]
    await db.upsert_project(project)
    return web.json_response({"project": _serialize(dict(project))})

async def api_projects_set_discord_channel(request: web.Request) -> web.Response:
    """PATCH /admin/api/projects/{slug}/discord-channel — set/clear del
    canal Discord default por proyecto (Iter 10.1).

    Body:
        discord_channel_id: str | null
            - string no vacío: upsert
            - null: clear (campo a NULL)
            - string vacío: 400

    Esta ruta la usa principalmente el dropdown del DM que el bot
    manda cuando detecta missing_discord_channel en metadata. El form
    general PATCH /admin/api/projects/{slug} también acepta el campo
    (ver `api_projects_patch` arriba).
    """
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    raw = body.get("discord_channel_id")
    if raw is None:
        existing = await db.get_project(slug)
        if existing is None:
            return web.json_response(
                {"error": f"proyecto {slug!r} desconocido"}, status=404)
        ok = await db.set_project_discord_channel(slug, channel_id=None, clear=True)
        if not ok:
            return web.json_response(
                {"error": f"proyecto {slug!r} desconocido"}, status=404)
        return web.json_response({
            "slug": slug,
            "discord_channel_id": None,
            "cleared": True,
        })
    if not isinstance(raw, str) or not raw.strip():
        return web.json_response(
            {"error": "discord_channel_id string vacío o tipo inválido "
                      "(manda null explícito para limpiar)"},
            status=400)
    existing = await db.get_project(slug)
    if existing is None:
        return web.json_response(
            {"error": f"proyecto {slug!r} desconocido"}, status=404)
    ok = await db.set_project_discord_channel(slug, channel_id=raw.strip())
    if not ok:
        return web.json_response(
            {"error": f"proyecto {slug!r} desconocido"}, status=404)
    return web.json_response({"slug": slug, "discord_channel_id": raw})

async def api_projects_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/projects/{slug} — purga hard de la fila.

    NO es soft. La fila se borra de la DB (incluyendo system_prompt,
    mcp_servers, etc). Si el repo sigue indexado en cbm, vuelve a
    aparecer como huérfano en /cbm/orphans y se puede re-importar
    con /projects/from-cbm.

    El soft-delete alternativo es PATCH {enabled: false}.
    """
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    existing = await db.get_project(slug)
    if not existing:
        return web.json_response({"error": "no existe"}, status=404)
    await db.delete_project(slug)
    return web.json_response({"deleted": True, "slug": slug})


def _member_project_metadata(project: Mapping[str, Any]) -> dict:
    # Allowlist: nuevos campos de configuración no se publican por accidente.
    fields = {"slug", "name", "description", "enabled", "indexed", "index_stats",
              "has_git", "git_remote_url"}
    return {key: value for key, value in project.items() if key in fields}
