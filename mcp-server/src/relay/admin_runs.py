"""Rutas de chats zombis, notas, corridas nocturnas y expertos."""
from __future__ import annotations
import asyncio
import json
import logging
import time
from contextlib import nullcontext
from pathlib import Path
import httpx
from aiohttp import web
from . import coordination, identity
from .app_state import DB_KEY, NIGHT_KEY, PROGRESS_KEY, RUNNING_KEY, STATE_DIR_KEY
from .secret_redaction import redact_data, redact_text
from .night_paths import night_plan_mirror

logger = logging.getLogger("relay.admin")


async def _zombie_idle_project(request, chat):
    """Return the lease target only when no executor/control owns this run.

    Managed tasks/graphs keep their own recovery controls. A project lease
    also covers create_chat -> registration and the result-finalization gap.
    """
    db = request.app[DB_KEY]
    if chat.get("status") != "running":
        return None
    if chat["id"] in getattr(db, "_unfinalized_process_chats", set()):
        return None
    worker = request.app.get(RUNNING_KEY, {}).get(chat["id"])
    if worker is not None and not worker.done():
        return None
    if chat["id"] in request.app.get(PROGRESS_KEY, {}):
        return None
    if await db.conversation_event_for_chat(chat["id"]):
        return None
    if cid := chat.get("conversation_id"):
        if (await db.get_conversation_task(cid)).get("mode"):
            return None
    # Graph-owned runs retain their graph controls even if a node link is
    # missing. This endpoint only removes independent empty orphans.
    if chat.get("source") == "grafo":
        return None
    project = await db.get_project(chat.get("project_slug") or chat.get("target") or "")
    if not project or coordination.busy(db, project):
        return None
    return project


async def _post_expert(request: web.Request, payload: dict) -> httpx.Response:
    """Conserva la identidad y las guardas del endpoint en esta aplicación."""
    # Nunca reenviar credenciales a RELAY_URL, Host o un proxy configurable:
    # el puerto sale del socket local que recibió la petición autenticada.
    address = request.transport.get_extra_info("sockname")
    host = "[::1]" if ":" in address[0] else "127.0.0.1"
    headers = {name: request.headers[name] for name in (
        "Cf-Access-Jwt-Assertion",) if name in request.headers}
    from .server_common import _get_api_key
    if api_key := _get_api_key():
        headers["X-Relay-Key"] = api_key
    from .native_auth import SESSION_COOKIE
    if session := request.cookies.get(SESSION_COOKIE):
        headers["Cookie"] = f"{SESSION_COOKIE}={session}"
    async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
        return await client.post(f"{request.scheme}://{host}:{address[1]}/experts/run",
                                 json=payload, headers=headers)


async def api_zombies_list(request: web.Request) -> web.Response:
    """GET /admin/api/chats/zombies — chats en status=running viejos.

    Iter 5.3: un chat zombie figura running en DB pero el proceso del
    relay ya no lo tiene vivo (reinicio, crash sin cleanup, etc.).
    Por convención, > `older_than_s` segundos en running = sospechoso.
    La UI muestra esta lista en un tab aparte con botón "Eliminar de BD".
    """
    db = request.app[DB_KEY]
    try:
        older_than_s = max(60, int(request.query.get("older_than_s", "60")))
        limit = max(1, min(200, int(request.query.get("limit", "200"))))
    except (TypeError, ValueError):
        return web.json_response({"error": "edad y límite deben ser enteros"}, status=400)
    rows = await db.list_zombie_chats(
        older_than_s=older_than_s, limit=limit)
    rows = [row for row in rows if await _zombie_idle_project(request, row)]
    from .private_mcp_access import visible_rows
    rows = await visible_rows(request, rows)
    return web.json_response({
        "zombies": redact_data([dict(r) for r in rows]),
        "older_than_s": older_than_s,
        "count": len(rows),
    })

async def api_chat_delete(request: web.Request) -> web.Response:
    """DELETE an empty orphan only; never remove a result or an active run."""
    db = request.app[DB_KEY]
    chat_id = request.match_info["chat_id"]
    chat = await db.get_chat(chat_id)
    if not chat:
        return web.json_response(
            {"error": f"chat {chat_id!r} no existe"}, status=404)
    if identity.role_of(request) != "owner" and not await identity.can_control_chat(request, db, chat):
        return web.json_response({"error": "No tienes permiso para controlar este run."}, status=403)
    from .task_service import control_lock
    cid = chat.get("conversation_id")
    async with control_lock(db, cid) if cid else nullcontext():
        chat = await db.get_chat(chat_id)
        if chat and identity.role_of(request) != "owner" and not await identity.can_control_chat(request, db, chat):
            return web.json_response({"error": "No tienes permiso para controlar este run."}, status=403)
        project = await _zombie_idle_project(request, chat) if chat else None
        if project is None:
            return web.json_response({"error": "El run está activo, terminó o tiene controles propios; se conserva."}, status=409)
        try:
            lease = coordination.acquire(db, project)
        except coordination.WorkspaceBusy:
            return web.json_response({"error": "El proyecto tiene trabajo en curso; espera a que termine."}, status=409)
        try:
            # Revalidate synchronous registries after the last await and hold
            # the same workspace exclusion as admission until SQLite commits.
            worker = request.app.get(RUNNING_KEY, {}).get(chat_id)
            if ((worker is not None and not worker.done())
                    or chat_id in request.app.get(PROGRESS_KEY, {})
                    or chat_id in getattr(db, "_unfinalized_process_chats", set())):
                return web.json_response({"error": "El run sigue activo; se conserva."}, status=409)
            ok = await db.delete_chat(chat_id, expected_started_at=chat["started_at"])
        finally:
            lease.release()
    if not ok:
        return web.json_response({"error": "El run cambió o conserva resultado, consumo o referencias; no se eliminó."}, status=409)
    return web.json_response({"deleted": chat_id})

async def api_notes_create(request: web.Request) -> web.Response:
    """POST /admin/api/notes — nuevo turno del workspace notes.

    Body: {user, system_prompt?, conversation_id?, new_conversation?, model?}
    Equivalente al POST que la UI de Chats usa para cualquier proyecto,
    pero solo válido para project_slug='notes'. Devuelve 202 con id
    (mismo shape que experts/run) o 503 si el notes-workspace no existe.
    """
    db = request.app[DB_KEY]
    notes = await db.get_project("notes")
    if notes is None:
        return web.json_response(
            {"error": "notes-workspace no existe (boot no completó el seed)"},
            status=503)
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    body = body or {}
    user = (body.get("user") or "").strip()
    if not user:
        return web.json_response({"error": "user requerido"}, status=400)
    sys_extra = body.get("system_prompt") or ""
    model = body.get("model") or ""
    is_async = bool(body.get("async"))

    conv_id = (body.get("conversation") or "").strip()
    if not conv_id and body.get("new_conversation"):
        conv_id = await db.create_conversation(
            project_slug="notes", requested_by=identity.requester(request))

    payload = {"target": "notes", "user": user, "system_extra": sys_extra}
    if model:
        payload["model"] = model
    if conv_id:
        payload["conversation"] = conv_id

    try:
        r = await _post_expert(request, payload)
        if r.status_code >= 400:
            return web.json_response(
                {"error": f"experts/run {r.status_code}: {redact_text(r.text)[:200]}"},
                status=502)
        data = r.json()
        chat_id = data.get("id") or ""
        data["chat_id"] = chat_id
        if conv_id:
            data["conversation_id"] = conv_id
        return web.json_response(data, status=(202 if is_async else 200))
    except Exception as e:  # noqa: BLE001
        return web.json_response(
            {"error": f"no pude llamar /experts/run: {e!r}"}, status=502)

async def api_notes_list(request: web.Request) -> web.Response:
    """GET /admin/api/notes?limit=&offset= — lista de chats del notes."""
    db = request.app[DB_KEY]
    try:
        limit = min(int(request.query.get("limit", "50")), 200)
        offset = max(int(request.query.get("offset", "0")), 0)
    except (TypeError, ValueError):
        limit, offset = 50, 0
    rows = await db.run(
        "SELECT id, target, status, model, started_at, finished_at, "
        "tokens_in, tokens_out, duration_ms, error "
        "FROM chats WHERE target=? ORDER BY started_at DESC LIMIT ? OFFSET ?",
        ("notes", limit, offset))
    from .private_mcp_access import visible_rows
    rows = await visible_rows(request, rows)
    return web.json_response({
        "notes": redact_data([dict(r) for r in rows]),
        "count": len(rows),
    })

async def api_night_runs_list(request: web.Request) -> web.Response:
    """GET /admin/api/night-runs — lista paginada de TODAS las corridas.

    Iter 5.4: el tab dedicado de Night Runs llama este endpoint.
    Filtros: ?project=slug, ?limit=N (default 50), ?since=ISO8601.
    Orden: started_at DESC.
    """
    db = request.app[DB_KEY]
    project = request.query.get("project") or None
    limit = int(request.query.get("limit", "50"))
    rows = await db.list_night_runs(project_slug=project, limit=limit)
    return web.json_response({
        "runs": redact_data([dict(r) for r in rows]),
        "count": len(rows),
    })

async def api_night_run_detail(request: web.Request) -> web.Response:
    """GET /admin/api/night-runs/{run_id} — detalle completo de un run.

    Iter 5.4: incluye tasks + results + plan ledger (parseado desde
    el espejo en state/) + report_path + branch + pr_url.
    """
    db = request.app[DB_KEY]
    run_id = request.match_info["run_id"]
    row = await db.get_night_run(run_id)
    if row is None:
        return web.json_response(
            {"error": f"run {run_id!r} desconocido"}, status=404)
    detail = {"run": dict(row), "tasks": [], "results": [],
               "plan_tasks": [], "plan_mirror": None}
    # Plan ledger: lo leemos del espejo state/. Si no existe todavía
    # (run muy fresco), devolvemos [] y la UI muestra "todavía no
    # hay ledger" en lugar de explotar.
    from . import night as night_mod
    state_dir = request.app[STATE_DIR_KEY]
    mirror = night_plan_mirror(state_dir, run_id)
    # Iter 5.4 — debug: devolvemos el path resuelto + si existe para
    # que la UI pueda mostrar "se buscó en X pero no hay plan ahí".
    detail["plan_mirror"] = str(mirror)
    detail["plan_mirror_exists"] = mirror.is_file()
    if mirror.is_file():
        try:
            text = mirror.read_text(encoding="utf-8")
            detail["plan_tasks"] = [
                {"id": t.id, "title": t.title, "refs": t.refs,
                 "status": t.status, "note": t.note}
                for t in night_mod.parse_plan(text)
            ]
        except OSError:
            detail["plan_tasks"] = []
    # Snapshot vivo si el orquestador está corriendo este run.
    night_registry = request.app.get(NIGHT_KEY) or {}
    if run_id in night_registry:
        orch, _task = night_registry[run_id]
        try:
            detail["snapshot"] = orch.snapshot()
        except Exception:  # noqa: BLE001 — best-effort
            pass
    return web.json_response(redact_data(detail))

async def api_night_run_report(request: web.Request) -> web.Response:
    """GET /admin/api/night-runs/{run_id}/report — devuelve el .md del reporte.

    Lee el path guardado en `night_runs.report_path`. Si el run sigue
    activo y todavía no escribió reporte, devuelve 404 con un mensaje
    claro (la UI muestra "todavía no hay reporte, refresca más tarde").
    El reporte puede ser pesado (cientos de KB en noches largas);
    capeamos a 256 KB en el read por defensa.
    """
    from pathlib import Path as _P

    db = request.app[DB_KEY]
    run_id = request.match_info["run_id"]
    row = await db.get_night_run(run_id)
    if row is None:
        return web.json_response({"error": "run desconocido"}, status=404)
    rp = row.get("report_path")
    if not rp:
        return web.json_response(
            {"error": "el run no tiene reporte todavía "
                      "(¿sigue corriendo?)", "run": dict(row)},
            status=404)
    path = _P(rp)
    if not path.is_file():
        return web.json_response(
            {"error": f"el reporte apunta a {rp} pero no existe en disco",
             "run": dict(row)},
            status=410)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return web.json_response(
            {"error": f"no pude leer el reporte: {e}"}, status=500)
    text = redact_text(text)
    if len(text) > 256 * 1024:
        text = text[: 256 * 1024] + \
            f"\n\n…(truncado a 256 KB; el reporte completo está en {rp})"
    return web.json_response(redact_data({
        "run_id": run_id,
        "project_slug": row.get("project_slug"),
        "end_reason": row.get("end_reason"),
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "report_path": rp,
        "report_md": text,
        "size_bytes": path.stat().st_size,
    }))

async def api_project_expert_run(request: web.Request) -> web.Response:
    """POST /admin/api/projects/{slug}/expert-run — dispara /experts/run.

    Body: {"prompt": "...", "async": false}
    Si async=true devuelve 202 con chat_id; si false espera y devuelve
    el output completo (cap a 32 KB). El modal "🧪 experto" de la Admin
    UI usa esto para no tener que abrir la extensión VS Code.
    """
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return web.json_response(
            {"error": "prompt requerido (no vacío)"}, status=400)
    is_async = bool(body.get("async"))

    # Conversación (2026-07-12): el modal 🧪 puede mantener contexto
    # entre corridas. `new_conversation: true` crea una acá y la
    # devuelve; `conversation: <id>` replaya una existente (la valida
    # /experts/run: 404 desconocida / 409 cerrada).
    conv_id = (body.get("conversation") or "").strip()
    if not conv_id and body.get("new_conversation"):
        conv_id = await db.create_conversation(
            project_slug=project["slug"],
            requested_by=identity.requester(request))

    payload = {"target": slug, "user": prompt}
    if conv_id:
        payload["conversation"] = conv_id

    try:
        r = await _post_expert(request, payload)
        if r.status_code >= 400:
            return web.json_response(
                {"error": f"experts/run {r.status_code}: "
                          f"{redact_text(r.text)[:200]}"},
                status=502)
        data = r.json()
        chat_id = data.get("id") or ""
        data["chat_id"] = chat_id  # alias para la UI
        if conv_id:
            data["conversation_id"] = conv_id
        if is_async:
            return web.json_response(data, status=202)
    except Exception as e:  # noqa: BLE001
        return web.json_response(
            {"error": f"no pude llamar /experts/run: {e!r}"}, status=502)

    # sync: /experts/run es async desde ADR-024 (202 + notify), así que
    # acá esperamos nosotros: poll a la fila de chats hasta finished
    # (max ~5min) y devolvemos el .md completo como output.
    deadline = time.monotonic() + 320.0
    chat = None
    while time.monotonic() < deadline:
        await asyncio.sleep(2.0)
        chat = await db.get_chat(chat_id)
        if chat and chat.get("finished_at"):
            break
    if not chat or not chat.get("finished_at"):
        return web.json_response(
            {"id": chat_id, "chat_id": chat_id, "status": "running",
             "output": "(sigue corriendo — mira el tab Estado o espera "
                       "el notify)"},
            status=202)
    out = ""
    if chat.get("md_path"):
        try:
            out = await asyncio.to_thread(
                Path(chat["md_path"]).read_text, encoding="utf-8")
        except OSError:
            out = "(no pude leer el .md del chat)"
    out = redact_text(out)
    if len(out) > 32 * 1024:
        out = out[:32 * 1024] + "\n…(truncado a 32 KB)"
    return web.json_response(redact_data({
        "id": chat_id, "chat_id": chat_id, "status": chat["status"],
        "error": chat.get("error"), "output": out,
        "tokens_in": chat.get("tokens_in"),
        "tokens_out": chat.get("tokens_out"),
        "conversation_id": conv_id or None,
    }))
