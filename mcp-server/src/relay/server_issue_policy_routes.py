"""Explicit project policy; discovery reads GitHub and never grants access."""
from __future__ import annotations

import json

from aiohttp import web

from . import conversation_access, identity, issue_sync_github
from .app_state import DB_KEY
from .server_common import _require_auth
from .task_pr import TaskPRError


async def _project(request, *, owner=False):
    db, actor = request.app[DB_KEY], identity.requester(request)
    slug = request.match_info["slug"]

    def snapshot(conn):
        # user_tx retains the explicit trusted local OWNER identity. Personal
        # accounts use their current DB role/grants, never the request cache.
        user = conversation_access.user_tx(conn, actor)
        if not user or owner and user["role"] != "owner":
            raise PermissionError("Proyecto no autorizado para tu cuenta")
        row = conn.execute("SELECT * FROM projects WHERE slug=? COLLATE NOCASE",
                           (slug,)).fetchone()
        if not row:
            raise web.HTTPNotFound(text='{"error":"No existe el proyecto"}',
                                   content_type="application/json")
        project = db._parse_project(dict(row))
        if (not project["enabled"] or (project.get("defaults_json") or {}).get("read_only")
                or not identity.user_can_write_project(user, project["slug"])):
            raise PermissionError("Proyecto no autorizado para tu cuenta")
        return project

    return await conversation_access.transaction(db, snapshot)


@_require_auth
async def discovery(request):
    try:
        project = await _project(request)
        try:
            result = await issue_sync_github.discover(project, request.query.get("project_id") or None)
        finally:
            # A remote exception can contain project data too. Recheck before
            # returning either its result or its error to a revoked account.
            current = await _project(request)
            if current.get("repo_path") != project.get("repo_path"):
                raise ValueError("El repositorio cambió durante la consulta; vuelve a intentar")
        return web.json_response(result, headers={"Cache-Control": "no-store"})
    except PermissionError:
        return web.json_response({"error": "Proyecto no autorizado para tu cuenta"}, status=403,
                                 headers={"Cache-Control": "no-store"})
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=422,
                                 headers={"Cache-Control": "no-store"})
    except (TaskPRError, OSError) as exc:
        return web.json_response({"error": str(exc)}, status=502,
                                 headers={"Cache-Control": "no-store"})


@_require_auth
async def configure(request):
    try:
        project = await _project(request, owner=True)
        actor = identity.requester(request)
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("policy"), dict):
            raise ValueError("policy debe ser un objeto JSON")
        try:
            policy = await issue_sync_github.validate_policy(project, body["policy"])
        finally:
            current = await _project(request, owner=True)
            if current.get("repo_path") != project.get("repo_path"):
                raise ValueError("El repositorio cambió durante la validación; vuelve a intentar")
        db = request.app[DB_KEY]

        def save():
            conn = db._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                user = conversation_access.user_tx(conn, actor)
                if not user or user["role"] != "owner":
                    raise PermissionError("Configurar sincronización requiere Admin")
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
        return web.json_response({"policy": policy, "enabled": policy.get("enabled", False)},
                                 headers={"Cache-Control": "no-store"})
    except PermissionError:
        # PermissionError subclasses OSError; it must not become a 502 or
        # expose a remote error body after a role/grant revocation.
        return web.json_response({"error": "Proyecto no autorizado para tu cuenta"}, status=403,
                                 headers={"Cache-Control": "no-store"})
    except (ValueError, json.JSONDecodeError) as exc:
        return web.json_response({"error": str(exc)}, status=422,
                                 headers={"Cache-Control": "no-store"})
    except (TaskPRError, OSError) as exc:
        return web.json_response({"error": str(exc)}, status=502,
                                 headers={"Cache-Control": "no-store"})
