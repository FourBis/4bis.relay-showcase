"""Rutas de catálogo, edición y exploración de skills."""
from __future__ import annotations
import asyncio
import json
import logging
import shutil
from pathlib import Path
from aiohttp import web
from .app_state import DB_KEY, SKILL_BROWSER_KEY, SKILLS_KEY

logger = logging.getLogger("relay.admin")

async def api_skills_list(request: web.Request) -> web.Response:
    """GET /admin/api/skills — skills instaladas con metadata."""
    from . import skills as skills_mod
    cache = request.app[SKILLS_KEY]
    items = await asyncio.to_thread(skills_mod.list_skills_sync, cache.dir)
    return web.json_response({"dir": str(cache.dir), "skills": items})

async def api_skills_get(request: web.Request) -> web.Response:
    """GET /admin/api/skills/{name} — SKILL.md completo de una instalada."""
    from . import skills as skills_mod
    cache = request.app[SKILLS_KEY]
    name = request.match_info["name"]
    content = await asyncio.to_thread(
        skills_mod.read_skill_sync, cache.dir, name)
    if content is None:
        return web.json_response({"error": "no existe"}, status=404)
    return web.json_response({"name": name, "content": content})

async def api_skills_put(request: web.Request) -> web.Response:
    """PUT /admin/api/skills/{name} {content} — reescribe el SKILL.md.

    El humano edita el archivo ENTERO (frontmatter incluido) desde la
    UI: era lo único que faltaba para poder recortar una descripción
    gorda —que es lo que el propio medidor de presupuesto te pide— sin
    abrir el disco a mano.

    Deja `SKILL.md.bak`. Devuelve `valid` para que la UI avise si el
    frontmatter quedó roto (una skill inválida NO se inyecta).
    """
    from . import skills as skills_mod
    cache = request.app[SKILLS_KEY]
    name = request.match_info["name"]
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    content = body.get("content") if isinstance(body, dict) else None
    if not isinstance(content, str) or not content.strip():
        return web.json_response({"error": "falta `content` (string no vacío)"},
                                 status=400)
    ok = await asyncio.to_thread(
        skills_mod.overwrite_skill_sync, cache.dir, name, content)
    if ok is None:
        return web.json_response({"error": "no existe"}, status=404)
    cache.invalidate()
    # Releer por el mismo camino que usa el runtime: si el frontmatter
    # quedó roto, la skill desaparece del listado y hay que decirlo.
    items = await asyncio.to_thread(skills_mod.list_skills_sync, cache.dir)
    row = next((s for s in items if s.get("dir") == name), None)
    return web.json_response({
        "name": name, "saved": True,
        "valid": bool(row and row.get("valid")),
        "state": (row or {}).get("state"),
        "tokens": skills_mod.estimate_tokens(content),
        "bullet_tokens": (row or {}).get("bullet_tokens"),
    })

async def api_skills_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/skills/{name} — borra el directorio de la skill.

    Hard delete del FS (no hay papelera). La UI pide confirm() antes.
    """
    from . import skills as skills_mod
    cache = request.app[SKILLS_KEY]
    name = request.match_info["name"]
    ok = await asyncio.to_thread(
        skills_mod.delete_skill_sync, cache.dir, name)
    if not ok:
        return web.json_response({"error": "no existe"}, status=404)
    cache.invalidate()
    return web.json_response({"deleted": True, "name": name})

async def api_skills_patch(request: web.Request) -> web.Response:
    """PATCH /admin/api/skills/{name} {state} — cambia el estado.

    `state` ∈ auto | manual | off. Escribe `enabled:`/`when:` en el
    frontmatter del SKILL.md:
      auto   → entra al bloque de skills del system prompt
      manual → on-demand: se lista pero se dispara a mano
      off    → ni se inyecta ni se ofrece (el archivo queda)

    Acepta también `{enabled: bool}` como atajo (no toca `when`).
    """
    from . import skills as skills_mod
    cache = request.app[SKILLS_KEY]
    name = request.match_info["name"]
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body debe ser un objeto"},
                                 status=400)

    if "state" in body:
        state = str(body["state"])
        if state not in skills_mod.SKILL_STATES:
            return web.json_response(
                {"error": "`state` debe ser "
                          + " | ".join(skills_mod.SKILL_STATES)},
                status=400)
        ok = await asyncio.to_thread(
            skills_mod.set_skill_state_sync, cache.dir, name, state)
    elif "enabled" in body:
        state = "auto" if body["enabled"] else "off"
        ok = await asyncio.to_thread(
            skills_mod.set_skill_enabled_sync, cache.dir, name,
            bool(body["enabled"]))
    else:
        return web.json_response(
            {"error": "falta `state` (auto|manual|off) o `enabled` (bool)"},
            status=400)

    if not ok:
        return web.json_response(
            {"error": "no existe o su SKILL.md no tiene frontmatter"},
            status=404)
    cache.invalidate()
    return web.json_response({"name": name, "state": state})

async def api_skills_budget(request: web.Request) -> web.Response:
    """GET /admin/api/skills/budget[?project=slug] — cuánto pesa el prompt fijo.

    Mide lo que REALMENTE se manda en cada push, no una estimación
    teórica: el ponytail (copilot-instructions.md), el bloque de skills
    que arma render_block(), y el bloque de fallback de tools. Devuelve
    tokens estimados + los umbrales para que la UI pinte la barra.

    `project` (2026-09-08) es lo que mantiene cierta esa primera frase.
    Desde que el bloque de skills se arma por proyecto —las del repo
    entran siempre, y `defaults_json.skills` filtra las globales— un
    único número global dejó de describir lo que recibe cualquier
    proyecto en particular. Sin el parámetro se mide el bloque global,
    como antes; `scope` en la respuesta dice cuál de los dos se midió,
    para que la UI nunca muestre un número sin decir de qué es.
    """
    from . import skills as skills_mod
    from .experts import (
        BATCH_ARTIFACTS_BLOCK, BITACORA_BLOCK, TOOL_FALLBACK_BLOCK,
        read_ponytail)
    cache = request.app[SKILLS_KEY]

    try:
        ponytail = await read_ponytail()
    except Exception as e:  # noqa: BLE001
        logger.warning("budget: read_ponytail rompió: %r", e)
        ponytail = ""
    slug = (request.query.get("project") or "").strip()
    proyecto = None
    if slug:
        proyecto = await request.app[DB_KEY].get_project(slug)
        if proyecto is None:
            return web.json_response(
                {"error": f"no existe el proyecto {slug}"}, status=404)

    scope = "global"
    dirs_medidos = [cache.dir]
    if proyecto is not None:
        repo = proyecto.get("repo_path") or ""
        filtro = (proyecto.get("defaults_json") or {}).get("skills")
        try:
            skills_block = await asyncio.to_thread(
                skills_mod.bloque_del_proyecto, repo, filtro)
            dirs_medidos = await asyncio.to_thread(
                skills_mod.skills_dirs, repo)
            scope = proyecto.get("slug") or slug
        except Exception as e:  # noqa: BLE001 — medir no puede romper el panel
            logger.warning("budget: bloque de %s rompió: %r", slug, e)
            proyecto = None
    if proyecto is None:
        try:
            skills_block = await cache.get_block()
        except Exception as e:  # noqa: BLE001
            logger.warning("budget: get_block rompió: %r", e)
            skills_block = ""

    # El ranking tiene que salir de los MISMOS directorios que el bloque
    # medido: con el global, recortar la que "más pesa" podía apuntar a
    # una skill que ese proyecto ni siquiera recibe.
    items: list = []
    vistos: set = set()
    for d in dirs_medidos:
        for it in await asyncio.to_thread(skills_mod.list_skills_sync, d):
            clave = (it.get("name") or "").strip().lower()
            if clave and clave in vistos:
                continue
            vistos.add(clave)
            items.append(it)
    on = [s for s in items if s["enabled"] and not s["manual"]]
    est = skills_mod.estimate_tokens
    parts = {
        "ponytail": est(ponytail),
        "skills": est(skills_block),
        "tool_fallback": est(TOOL_FALLBACK_BLOCK),
        # El medidor de presupuesto tiene que contar TODO lo que viaja en
        # cada turno, o subestima justo lo que se paga siempre.
        "batch_artifacts": est(BATCH_ARTIFACTS_BLOCK),
        "bitacora": est(BITACORA_BLOCK),
    }
    return web.json_response({
        "parts": parts,
        "total": sum(parts.values()),
        "skills_budget": skills_mod.skills_token_budget(),
        "prompt_budget": skills_mod.prompt_token_budget(),
        # `off` (apagadas a mano) y `manual` (on-demand) se cuentan por
        # separado: `off` no entra al prompt de ninguna forma; `manual`
        # entra solo como nombre (~3 tokens, ver render_ondemand_line),
        # sin la descripción que cuenta `bullet_tokens`.
        "counts": {
            "installed": len(items), "injected": len(on),
            "off": sum(1 for s in items if not s["enabled"]),
            "manual": sum(1 for s in items if s["enabled"] and s["manual"]),
        },
        # Ranking para saber a quién recortar primero.
        "top": sorted(
            ({"name": s["name"], "dir": s["dir"],
              "tokens": s["bullet_tokens"]} for s in on),
            key=lambda s: s["tokens"], reverse=True)[:10],
        "estimator": "chars/4 (aproximado)",
        # Qué se midió. La UI lo muestra: un número de presupuesto sin
        # decir de qué proyecto es, es un número que no se puede usar.
        "scope": scope,
    })

async def api_instructions_get(request: web.Request) -> web.Response:
    """GET /admin/api/instructions — el archivo que lee read_ponytail()."""
    from . import skills as skills_mod
    from .experts import PONYTAIL_PATH
    try:
        content = await asyncio.to_thread(
            PONYTAIL_PATH.read_text, "utf-8")
    except OSError as e:
        return web.json_response(
            {"path": str(PONYTAIL_PATH), "content": "", "exists": False,
             "error": str(e), "tokens": 0})
    return web.json_response({
        "path": str(PONYTAIL_PATH), "content": content, "exists": True,
        "tokens": skills_mod.estimate_tokens(content),
    })

async def api_instructions_put(request: web.Request) -> web.Response:
    """PUT /admin/api/instructions {content} — reescribe el ponytail.

    Deja un `.bak` con la versión anterior antes de pisar (este archivo
    va en el system prompt de CADA push; un guardado en falso sin copia
    se paga caro). El cache de read_ponytail() es por mtime, así que el
    cambio entra solo en el próximo run.
    """
    from . import skills as skills_mod
    from .experts import PONYTAIL_PATH
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    content = body.get("content") if isinstance(body, dict) else None
    if not isinstance(content, str):
        return web.json_response({"error": "falta `content` (string)"},
                                 status=400)

    def _write() -> None:
        PONYTAIL_PATH.parent.mkdir(parents=True, exist_ok=True)
        if PONYTAIL_PATH.is_file():
            shutil.copy2(PONYTAIL_PATH,
                         PONYTAIL_PATH.with_suffix(".md.bak"))
        PONYTAIL_PATH.write_text(content, encoding="utf-8")

    try:
        await asyncio.to_thread(_write)
    except OSError as e:
        return web.json_response({"error": f"no pude escribir: {e}"},
                                 status=500)
    return web.json_response({
        "path": str(PONYTAIL_PATH), "saved": True,
        "backup": str(PONYTAIL_PATH.with_suffix(".md.bak")),
        "tokens": skills_mod.estimate_tokens(content),
    })

async def api_skills_catalog(request: web.Request) -> web.Response:
    """GET /admin/api/skills/catalog — repos sugeridos para el dropdown."""
    from . import skills as skills_mod
    return web.json_response({"catalog": skills_mod.GITHUB_CATALOG})

async def api_skills_browse_start(request: web.Request) -> web.Response:
    """POST /admin/api/skills/browse {url} — clona y escanea en background."""
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    url = (body.get("url") or "").strip() if isinstance(body, dict) else ""
    if not url:
        return web.json_response(
            {"error": "url requerida (https://github.com/owner/repo)"},
            status=400)
    browser = request.app[SKILL_BROWSER_KEY]
    try:
        job = await browser.start(url)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response(job.to_public(), status=202)

async def api_skills_browse_status(request: web.Request) -> web.Response:
    """GET /admin/api/skills/browse/{job_id} — estado + skills encontradas."""
    browser = request.app[SKILL_BROWSER_KEY]
    job = browser.get(request.match_info["job_id"])
    if job is None:
        return web.json_response(
            {"error": "job no encontrado (¿expiró? vuelve a clonar)"},
            status=404)
    return web.json_response(job.to_public())

async def api_skills_browse_preview(request: web.Request) -> web.Response:
    """GET /admin/api/skills/browse/{job_id}/preview?path=… — SKILL.md crudo.

    Leer antes de instalar es la única defensa real contra un SKILL.md
    hostil: no ejecuta nada, pero entra al system prompt.
    """
    from . import skills as skills_mod
    browser = request.app[SKILL_BROWSER_KEY]
    job = browser.get(request.match_info["job_id"])
    if job is None:
        return web.json_response({"error": "job no encontrado"}, status=404)
    rel = request.query.get("path", "")
    content = await asyncio.to_thread(
        skills_mod.read_skill_md_from_clone, Path(job.clone_dir), rel)
    if content is None:
        return web.json_response({"error": "ruta inválida"}, status=404)
    return web.json_response({"path": rel, "content": content})

async def api_skills_browse_install(request: web.Request) -> web.Response:
    """POST /admin/api/skills/browse/{job_id}/install {paths[], overwrite}

    Copia las skills tildadas a ~/.copilot/skills. Best-effort por item:
    una que falle no aborta las demás, se reporta en `errors`.
    """
    from . import skills as skills_mod
    browser = request.app[SKILL_BROWSER_KEY]
    cache = request.app[SKILLS_KEY]
    job = browser.get(request.match_info["job_id"])
    if job is None:
        return web.json_response(
            {"error": "job no encontrado (¿expiró? vuelve a clonar)"},
            status=404)
    if job.state != "ready":
        return web.json_response(
            {"error": f"job en estado {job.state!r}; espera al scan"},
            status=409)
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    paths = body.get("paths") if isinstance(body, dict) else None
    if not isinstance(paths, list) or not paths:
        return web.json_response({"error": "falta `paths` (lista)"},
                                 status=400)
    overwrite = bool(body.get("overwrite"))
    # Instalar apagadas es la opción segura para tandas grandes: entran
    # al disco pero no al prompt hasta que las prendas una por una.
    enabled = bool(body.get("enabled", True))

    installed: list[str] = []
    errors: list[dict] = []

    def _install_all() -> None:
        for rel in paths:
            if not isinstance(rel, str):
                continue
            try:
                name = skills_mod.install_skill_from_clone(
                    Path(job.clone_dir), rel, cache.dir,
                    overwrite=overwrite, enabled=enabled)
                installed.append(name)
            except FileExistsError as e:
                errors.append({"path": rel,
                               "error": f"ya existe una skill '{e}' "
                                        "(marca sobrescribir)"})
            except (ValueError, OSError) as e:
                errors.append({"path": rel, "error": str(e)})

    await asyncio.to_thread(_install_all)
    if installed:
        cache.invalidate()
    return web.json_response({"installed": installed, "errors": errors,
                              "enabled": enabled})


async def api_skills_browse_discard(request: web.Request) -> web.Response:
    """DELETE /admin/api/skills/browse/{job_id} — tira el clon."""
    browser = request.app[SKILL_BROWSER_KEY]
    ok = browser.discard(request.match_info["job_id"])
    return web.json_response({"discarded": ok})
