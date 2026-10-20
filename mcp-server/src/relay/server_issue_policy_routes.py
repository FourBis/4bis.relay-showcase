"""Explicit project policy; discovery reads GitHub and never grants access."""
from __future__ import annotations

import json

from aiohttp import web

from . import identity, issue_sync_github
from .app_state import DB_KEY
from .server_common import _require_auth
from .task_pr import TaskPRError


async def _project(request, *, owner=False):
    if owner and identity.role_of(request) != "owner":
        raise web.HTTPForbidden(text='{"error":"Configurar sincronización requiere Admin"}',
                                content_type="application/json")
    project = await request.app[DB_KEY].get_project(request.match_info["slug"])
    if not project:
        raise web.HTTPNotFound(text='{"error":"No existe el proyecto"}', content_type="application/json")
    if not identity.can_write_project(request, project):
        raise web.HTTPForbidden(text='{"error":"Proyecto no autorizado para tu cuenta"}',
                                content_type="application/json")
    return project


@_require_auth
async def discovery(request):
    project = await _project(request)
    try:
        result = await issue_sync_github.discover(project, request.query.get("project_id") or None)
        await _project(request)
        return web.json_response(result)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=422)
    except (TaskPRError, OSError) as exc:
        return web.json_response({"error": str(exc)}, status=502)


@_require_auth
async def configure(request):
    project = await _project(request, owner=True)
    try:
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("policy"), dict):
            raise ValueError("policy debe ser un objeto JSON")
        policy = await issue_sync_github.validate_policy(project, body["policy"])
        current = await _project(request, owner=True)
        if current.get("repo_path") != project.get("repo_path"):
            raise ValueError("El repositorio cambió durante la validación; vuelve a intentar")
        db = request.app[DB_KEY]

        def save():
            conn = db._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT repo_path,enabled,defaults_json FROM projects WHERE slug=? COLLATE NOCASE",
                                   (project["slug"],)).fetchone()
                if not row or not row["enabled"] or row["repo_path"] != project.get("repo_path"):
                    raise ValueError("El proyecto cambió durante la validación")
                defaults = json.loads(row["defaults_json"] or "{}")
                if defaults.get("read_only"):
                    raise ValueError("El proyecto pasó a solo lectura")
                defaults["issue_sync_policy"] = policy
                conn.execute("UPDATE projects SET defaults_json=? WHERE slug=? COLLATE NOCASE",
                             (json.dumps(defaults, ensure_ascii=False), project["slug"]))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        await db._in_thread(save)
        return web.json_response({"policy": policy, "enabled": policy.get("enabled", False)})
    except (ValueError, json.JSONDecodeError) as exc:
        return web.json_response({"error": str(exc)}, status=422)
    except (TaskPRError, OSError) as exc:
        return web.json_response({"error": str(exc)}, status=502)
