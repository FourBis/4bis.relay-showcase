"""Authorized local records of external GitHub PRs."""
import json
import re

from aiohttp import web

from . import external_pr, external_pr_access, external_review, external_review_publish, identity
from .app_state import DB_KEY
from .server_common import _require_auth
from .task_pr import TaskPRError
from .user_accounts import AccountError


async def _project(request, slug):
    try:
        project, _ = await external_pr_access.require_project(request.app[DB_KEY], slug, identity.requester(request))
    except LookupError:
        raise web.HTTPNotFound(text='{"error":"No existe el proyecto"}', content_type="application/json")
    except PermissionError:
        raise web.HTTPForbidden(text='{"error":"Proyecto no autorizado para tu cuenta"}', content_type="application/json")
    return project


async def _body(request):
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("Se requiere un objeto JSON")
    return body


@_require_auth
async def listing(request):
    """Local project work register. GET never observes GitHub or enqueues work."""
    project = await _project(request, request.match_info["slug"])
    cursor = request.query.get("cursor", "")
    if (set(request.query) - {"cursor"} or len(request.query.getall("cursor", [])) > 1
            or cursor and not re.fullmatch(r"[0-9a-f-]{36}", cursor)):
        return web.json_response({"error": "Cursor de registros inválido"}, status=422)
    rows = await request.app[DB_KEY].run(
        "SELECT id,task_json FROM conversations WHERE project_slug=? COLLATE NOCASE "
        "AND json_extract(CASE WHEN json_valid(task_json) THEN task_json ELSE '{}' END,'$.mode')='external_pr' "
        "AND id>? ORDER BY id LIMIT 51",
        (project["slug"], cursor))
    # Recheck membership/project state after reading, before exposing any row.
    await _project(request, project["slug"])
    tasks = []
    for row in rows[:50]:
        task = external_pr.snapshot(json.loads(row["task_json"]), project)
        pr = task["external_pr"]
        tasks.append({key: task.get(key) for key in ("id", "mode", "title", "risk", "head_sha", "assessment")} | {
            "project_slug": project["slug"],
            "external_pr": {key: pr.get(key) for key in ("repo", "number", "url", "title", "state", "head_sha", "base_sha")}})
    return web.json_response({"tasks": tasks, "can_register": True,
        "next_cursor": rows[49]["id"] if len(rows) > 50 else None}, headers={"Cache-Control": "no-store"})


@_require_auth
async def preview(request):
    """GET only reads GitHub for the form; it never stores or enqueues work."""
    project = await _project(request, request.match_info["slug"])
    headers = {"Cache-Control": "no-store"}
    try:
        if set(request.query) != {"url"} or len(request.query.getall("url")) != 1:
            raise ValueError("Usa únicamente url una vez por consulta")
        result = await external_pr.preview(project, request.query["url"].strip())
        # Recheck membership after reading GitHub, before exposing anything.
        await _project(request, project["slug"])
        # Only this project's register: never reveal records of other projects.
        rows = await request.app[DB_KEY].run(
            "SELECT 1 FROM conversations WHERE id=? AND project_slug=? COLLATE NOCASE LIMIT 1",
            (result["registration_id"], project["slug"]))
        result["registered"] = bool(rows)
        return web.json_response(result, headers=headers)
    except AccountError as exc:
        return web.json_response({"error": str(exc), "code": "account_required"}, status=409, headers=headers)
    except (TaskPRError, OSError) as exc:
        return web.json_response({"error": str(exc)}, status=502, headers=headers)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=422, headers=headers)


@_require_auth
async def register(request):
    project = await _project(request, request.match_info["slug"])
    try:
        result = await external_pr.register(request.app[DB_KEY], project, await _body(request), identity.requester(request),
                                            authorize=lambda: _project(request, project["slug"]))
        return web.json_response(await external_pr_access.snapshot(request.app[DB_KEY], result["id"], identity.requester(request)))
    except PermissionError:
        return web.json_response({"error": "Proyecto no autorizado para tu cuenta"}, status=403)
    except (TaskPRError, OSError) as exc:
        return web.json_response({"error": str(exc)}, status=502)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=422)


@_require_auth
async def action(request):
    db, cid = request.app[DB_KEY], request.match_info["id"]
    conv = await db.get_conversation(cid)
    if not conv:
        return web.json_response({"error": "No existe la tarea"}, status=404)
    project = await _project(request, conv["project_slug"])
    try:
        body, who = await _body(request), identity.requester(request)
        if body.get("action") == "refresh":
            result = await external_pr.refresh(db, project, cid, who, authorize=lambda: _project(request, project["slug"]))
        elif body.get("action") == "evidence":
            result = await external_pr.add_evidence(db, project, cid, body, who, authorize=lambda: _project(request, project["slug"]))
        elif body.get("action") == "review_retry":
            state = await db.get_conversation_task(cid)
            if body.get("head_sha") != (state.get("external_pr") or {}).get("head_sha"):
                raise ValueError("El commit cambió; actualiza antes de reanudar la revisión")
            base = (state.get("external_pr") or {}).get("base_sha")
            if "base_sha" in body and body["base_sha"] != base:
                raise ValueError("La base cambió; actualiza antes de reanudar la revisión")
            current = await external_review.snapshot(db, cid, body["head_sha"], base_sha=base)
            if current is None:
                # Legacy tasks may have observed a new base without rotating
                # their same-head job. Verify that durable predecessor before
                # the core atomically retires its context and enqueues the new one.
                current = await external_review.snapshot(db, cid, body["head_sha"])
            if "context_revision" in body and (type(body["context_revision"]) is not int
                    or body["context_revision"] < 0
                    or body["context_revision"] != (current or {}).get("context_revision", 0)):
                raise ValueError("El contexto de revisión cambió; actualiza antes de reanudar")
            if body.get("job_id") and (not current or body["job_id"] != current.get("job_id")):
                raise ValueError("La revisión ya no corresponde al trabajo actual")
            await external_review.request(db, project, cid, who, head_sha=body["head_sha"], retry=True,
                base_sha=base, context_revision=(current or {}).get("context_revision", 0))
            await external_review_publish.retry(db, cid, who)
            result = external_pr.snapshot(await db.get_conversation_task(cid), project)
        else:
            raise ValueError("Acción desconocida")
        return web.json_response(await external_pr_access.snapshot(db, cid, who))
    except PermissionError:
        return web.json_response({"error": "Tu cuenta no puede reanudar esta revisión"}, status=403)
    except (TaskPRError, OSError) as exc:
        return web.json_response({"error": str(exc), "conversation_id": cid}, status=502)
    except ValueError as exc:
        return web.json_response({"error": str(exc), "conversation_id": cid}, status=422)
