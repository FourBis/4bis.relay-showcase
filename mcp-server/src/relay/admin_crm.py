"""CRM Admin API: snapshot, sync, digest y vínculo con proyectos."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from . import crm as crm_mod, github as github_mod
from .app_state import DB_KEY, NOTIFY_KEY
from .admin_common import _get_job, _git_remote_url, _has_git, _serialize, _set_job

async def api_project_client_link(request: web.Request) -> web.Response:
    """PUT /admin/api/projects/{slug}/client — vincula un cliente CRM.

    Body: `{client_id: N}` para vincular, `{client_id: null}` (o `{}`)
    para desvincular.

    Este endpoint faltaba y por eso la cadena cliente→proyecto no
    funcionaba: la columna `projects.client_id`, su FK y el método
    `db.set_project_client()` existían desde F1, pero nada los
    alcanzaba — ni HTTP ni UI. El comentario del schema afirmaba que lo
    llenaba la UI al "convertir un deal ganado en proyecto"; esa
    conversión nunca se implementó.

    A diferencia de `github-project`, acá SÍ se valida que el cliente
    exista: es una FK real, y un id inventado dejaría el proyecto
    apuntando a la nada con ON DELETE SET NULL sin avisar.
    """
    slug = request.match_info["slug"]
    db = request.app[DB_KEY]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    body = body or {}
    raw = body.get("client_id")
    if raw in (None, "", 0):
        await db.set_project_client(project_slug=project["slug"],
                                    client_id=None)
        return web.json_response({"client_id": None, "client_name": None})
    try:
        client_id = int(raw)
    except (TypeError, ValueError):
        return web.json_response(
            {"error": "client_id debe ser entero o null"}, status=400)
    client = await db.get_crm_client(client_id)
    if not client:
        return web.json_response(
            {"error": f"no existe el cliente {client_id}"}, status=404)
    await db.set_project_client(project_slug=project["slug"],
                                client_id=client_id)
    return web.json_response(
        {"client_id": client_id, "client_name": client.get("name")})

async def api_project_deal_link(request: web.Request) -> web.Response:
    """PUT /admin/api/projects/{slug}/deal — vincula un deal del CRM.

    Body: `{deal_id: "cuid"}` para vincular, `{deal_id: null}` (o `{}`)
    para desvincular.

    Un proyecto es un deal, y un deal cuelga de una company: por eso el
    `client_id` NO se pide, se deduce del deal. Pedirlo aparte dejaría
    armar la combinación imposible (proyecto de un cliente con el deal de
    otro).

    Desvincular el deal deja el cliente como estaba: se puede saber de
    quién es un proyecto sin haber cerrado la venta todavía.
    """
    slug = request.match_info["slug"]
    db = request.app[DB_KEY]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    raw = (body or {}).get("deal_id")
    if raw in (None, ""):
        await db.set_project_deal(project_slug=project["slug"], deal_id=None,
                                  client_id=project.get("client_id"))
        return web.json_response({"deal_id": None})
    deal_id = str(raw)
    client = await db.find_crm_client_by_deal(deal_id)
    if not client:
        return web.json_response(
            {"error": f"no existe el deal {deal_id} en el snapshot; "
                      f"sincronizá el CRM"}, status=404)
    deal = next(d for d in client["deals"] if str(d.get("id")) == deal_id)
    await db.set_project_deal(project_slug=project["slug"], deal_id=deal_id,
                              client_id=client["id"])
    return web.json_response({
        "deal_id": deal_id,
        "deal_name": deal.get("name"),
        "client_id": client["id"],
        "client_name": client.get("name"),
    })

def _finance_client(client: dict) -> dict:
    """Vista comercial: no publicar blobs libres ni contactos personales."""
    result = {k: v for k, v in client.items() if k in {
        "id", "name", "domain", "project_count", "last_sync_at",
        "last_sync_status", "last_activity_at"}}
    result["deals"] = [{k: v for k, v in d.items()
                        if k in {"id", "name", "stage", "amount", "value", "currency"}}
                       for d in client.get("deals", []) if isinstance(d, dict)]
    result["contacts"] = []
    result["restricted_view"] = True
    return result


async def api_crm_client_detail(request: web.Request) -> web.Response:
    """GET /admin/api/crm/clients/{cid} — el cliente y su cadena.

    Devuelve el cliente (contactos + deals del espejo del CRM) y sus
    proyectos, cada uno con lo necesario para seguir bajando:
    `repo_path` + `has_git` (git) y `github_project` (kanban, que a su
    vez es la entrada a issues y PRs vía
    `/admin/api/projects/{slug}/github`).

    Va en una sola llamada a propósito: la cadena
    cliente → proyecto → git → kanban se navega de arriba hacia abajo, y
    pedir un fetch por eslabón la vuelve inusable con varios proyectos.
    """
    db = request.app[DB_KEY]
    try:
        cid = int(request.match_info["cid"])
    except (TypeError, ValueError):
        return web.json_response({"error": "cid inválido"}, status=400)
    client = await db.get_crm_client(cid)
    if not client:
        return web.json_response({"error": "not found"}, status=404)
    projects = await db.list_projects_for_client(cid)
    # Deals del cliente por id, para poder mostrar el nombre del deal de
    # cada proyecto sin que la UI tenga que cruzarlos a mano.
    deals_by_id = {str(d.get("id")): d for d in (client.get("deals") or [])}
    out_projects = []
    for p in projects:
        rp = p.get("repo_path", "")
        defaults = p.get("defaults_json") or {}
        deal = deals_by_id.get(str(p.get("deal_id") or ""))
        out_projects.append({
            "slug": p["slug"],
            "name": p["name"],
            "repo_path": rp,
            "has_git": _has_git(Path(rp)) if rp else False,
            "git_remote_url": _git_remote_url(rp) if rp else None,
            "github_project": defaults.get("github_project"),
            "enabled": bool(p.get("enabled", 1)),
            "deal_id": p.get("deal_id"),
            "deal_name": deal.get("name") if deal else None,
            "deal_stage": deal.get("stage") if deal else None,
        })
    from . import identity
    if identity.role_of(request) == "finance":
        client = _finance_client(client)
        out_projects = [{k: v for k, v in p.items()
                         if k in {"slug", "name", "enabled", "deal_id", "deal_name", "deal_stage"}}
                        for p in out_projects]
    return web.json_response(_serialize({
        "client": client,
        "projects": out_projects,
    }))

async def api_crm_clients_list(request: web.Request) -> web.Response:
    """GET /admin/api/crm/clients — snapshot de la última sync."""
    db = request.app[DB_KEY]
    clients = await db.list_crm_clients()
    # _parse_crm_client ya inyectó `deals` y `contacts` como objetos (no JSON).
    out = [{
        "id": c["id"],
        "ext_id": c["ext_id"],
        "name": c["name"],
        "domain": c.get("domain") or "",
        "contacts": c.get("contacts") or [],
        "deals": c.get("deals") or [],
        "project_count": c.get("project_count", 0),
        "last_sync_at": c.get("last_sync_at"),
        "last_sync_status": c.get("last_sync_status"),
        "last_activity_at": c.get("last_activity_at"),
    } for c in clients]
    from . import identity
    if identity.role_of(request) == "finance":
        out = [_finance_client(c) for c in out]
    return web.json_response({"clients": out})

async def api_crm_sync_post(request: web.Request) -> web.Response:
    """POST /admin/api/crm/sync — dispara sync en background."""
    db = request.app[DB_KEY]
    # Chequear la conexión ANTES de arrancar el job: si el CRM está
    # apagado devolvemos 503 inmediato en vez de dejar un job que falla
    # tres segundos después, y el feedback en la UI queda claro.
    try:
        await crm_mod.check()
    except crm_mod.CrmError as e:
        try:
            await db.mark_crm_sync_error(e.message)
        except Exception:  # noqa: BLE001
            pass
        return web.json_response(
            {"error": e.message, "status_code": e.status_code},
            status=e.status_code or 503)
    job_id = f"crm_{int(asyncio.get_event_loop().time() * 1000) % 100000}"

    async def _do_sync() -> None:
        try:
            _set_job(job_id, {"status": "running",
                               "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                          time.gmtime())})
            stats = await crm_mod.sync_once(db)
            _set_job(job_id, {"status": "ok", **stats,
                               "finished_at": time.strftime(
                                   "%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        except crm_mod.CrmError as e:
            try:
                await db.mark_crm_sync_error(e.message)
            except Exception:  # noqa: BLE001
                pass  # si falla el mark, igual devolvemos el error abajo
            _set_job(job_id, {"status": "error", "error": e.message})
        except Exception as e:  # noqa: BLE001
            _set_job(job_id, {"status": "error",
                               "error": f"{type(e).__name__}: {e}"})

    asyncio.create_task(_do_sync())  # fire-and-forget; _do_sync muta el slot del job
    return web.json_response({"job_id": job_id})

async def api_crm_sync_status(request: web.Request) -> web.Response:
    """GET /admin/api/crm/sync/status?job_id=... — patrón del reindex."""
    job_id = request.query.get("job_id", "")
    if not job_id:
        return web.json_response({"error": "falta job_id"}, status=400)
    val = _get_job(job_id)
    if val is None:
        return web.json_response({"error": "job desconocido"}, status=404)
    if isinstance(val, asyncio.Task):
        if not val.done():
            return web.json_response({"job_id": job_id, "status": "running"})
        return web.json_response({"job_id": job_id, "status": "unknown",
                                  "error": "job terminó sin payload"})
    if isinstance(val, dict):
        out = {k: v for k, v in val.items() if k != "_task"}
        return web.json_response(out)
    return web.json_response({"job_id": job_id})

async def api_crm_check(request: web.Request) -> web.Response:
    """GET /admin/api/crm/check — ¿está arriba el CRM y migrado?

    Devuelve 200 con los conteos del CRM, o 503 si el Postgres no
    responde / el schema no está. La UI lo usa para el semáforo del tab
    antes de dejar sincronizar."""
    try:
        r = await crm_mod.check()
    except crm_mod.CrmError as e:
        return web.json_response(
            {"ok": False, "error": e.message, "dsn": _crm_dsn_public(),
             "app_url": crm_mod.crm_app_url(),
             "app_up": await crm_mod.app_up(),
             "workspace": crm_mod.CRM_WORKSPACE},
            status=e.status_code or 503)
    except Exception as e:  # noqa: BLE001
        return web.json_response(
            {"ok": False, "error": f"{type(e).__name__}: {e}"},
            status=500)
    # `app_up` es aparte de `ok`: el Postgres del CRM vuelve solo con
    # Docker, el dev server no. Es lo que decide si mostrar "Levantar CRM".
    return web.json_response({**r, "dsn": _crm_dsn_public(),
                              "app_url": crm_mod.crm_app_url(),
                              "app_up": await crm_mod.app_up(),
                              "workspace": crm_mod.CRM_WORKSPACE})

async def api_crm_start(request: web.Request) -> web.Response:
    """POST /admin/api/crm/start — levanta el stack del CRM.

    El CRM no es parte del relay: hay que arrancarlo a mano después de cada
    reinicio (docs/CRM_LOCAL.md). Devuelve enseguida; el tab poll-ea
    /crm/check hasta que `app_up` da true.
    """
    if await crm_mod.app_up():
        return web.json_response({"started": False, "already_up": True,
                                  "notes": ["el CRM ya estaba respondiendo"]})
    try:
        return web.json_response(await crm_mod.start_stack())
    except crm_mod.CrmError as e:
        return web.json_response({"error": e.message, "log": str(crm_mod.crm_dev_log())},
                                 status=e.status_code or 500)

def _crm_dsn_public() -> str:
    """DSN sin la contraseña — se muestra en la UI para diagnosticar."""
    dsn = crm_mod.crm_dsn()
    if "@" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    return f"{scheme}://{rest.split('@', 1)[1]}"

async def _crm_health_rows(db: Any, *, include_github: bool = True) -> list[dict]:
    """Una fila por cliente CRM con proyectos vinculados: silencio +
    resumen de GitHub. Base de `/crm/health` y `/crm/digest`.

    Solo clientes con `project_count > 0`: el sync trae cualquier
    empresa que apareció en un email o una reunión (decenas, la mayoría
    gente con la que se habló una vez), y de esas solo importan acá las
    que son clientes de verdad — es decir, tienen un proyecto vinculado.
    """
    clients = [c for c in await db.list_crm_clients() if c["project_count"] > 0]
    rows: list[dict] = []
    for c in clients:
        projects = await db.list_projects_for_client(c["id"]) if include_github else []
        repos = []
        for p in projects:
            slug = await github_mod.repo_slug(p.get("repo_path") or "")
            if slug:
                repos.append(slug)
        # Un cliente puede tener varios proyectos → varios repos; se piden
        # todos en paralelo (mismo criterio que api_project_github: dos
        # spawns de `gh` por repo que no dependen entre sí).
        results = await asyncio.gather(
            *(github_mod.issues(r) for r in repos),
            *(github_mod.pulls(r) for r in repos),
        ) if repos else []
        issues_lists = results[:len(repos)]
        pulls_lists = results[len(repos):]
        # None si NINGÚN repo pudo leerse (sin `gh`/auth); si al menos uno
        # respondió, se suma lo que haya — parcial es mejor que ocultar todo.
        got_any = any(x is not None for x in issues_lists + pulls_lists)
        open_issues = (sum(len(x or []) for x in issues_lists)
                      if got_any else None)
        open_prs = sum(len(x or []) for x in pulls_lists) if got_any else None

        rows.append({
            "client_id": c["id"],
            "name": c["name"],
            "domain": c["domain"],
            "days_silent": crm_mod.days_since(c.get("last_activity_at")),
            "last_activity_at": c.get("last_activity_at"),
            "project_count": c["project_count"],
            "deals": [{"name": d.get("name"), "stage": d.get("stage")}
                     for d in c.get("deals") or []],
            "open_issues": open_issues,
            "open_prs": open_prs,
        })
    return rows

async def api_crm_health(request: web.Request) -> web.Response:
    """GET /admin/api/crm/health?stale_days=N — salud por cliente.

    Silencio (`days_since(last_activity_at)`) + issues/PRs abiertos por
    cliente, solo para los que tienen proyecto vinculado. Es la data
    detrás del digest; separado del endpoint que lo envía a Discord para
    poder pedirla sin disparar una notificación (ver docs/CRM_DIGEST.md).
    """
    db = request.app[DB_KEY]
    try:
        stale_days = int(request.query.get("stale_days",
                                           crm_mod.DEFAULT_STALE_DAYS))
    except ValueError:
        return web.json_response({"error": "stale_days debe ser entero"},
                                 status=400)
    from . import identity
    rows = await _crm_health_rows(db, include_github=identity.role_of(request) != "finance")
    stale = sum(1 for r in rows
               if r["days_silent"] is None or r["days_silent"] >= stale_days)
    return web.json_response({
        "stale_days": stale_days, "stale_count": stale, "clients": rows,
    })

async def api_crm_digest(request: web.Request) -> web.Response:
    """POST /admin/api/crm/digest — arma el digest y (salvo dry_run) lo
    manda a Discord vía el bot C#.

    Body opcional: `{"stale_days": 14, "channel": "#equipo-demo",
    "dry_run": false}`. Con `dry_run` devuelve el texto sin enviarlo —
    para probar el mensaje antes de spamear el canal.

    Ver docs/CRM_DIGEST.md: es la acción que se le pide al sistema para
    un reporte on-demand además del uso desde el botón de la Admin UI.
    """
    db = request.app[DB_KEY]
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    body = body or {}
    stale_days = int(body.get("stale_days") or crm_mod.DEFAULT_STALE_DAYS)
    channel = body.get("channel") or crm_mod.DEFAULT_DIGEST_CHANNEL
    dry_run = bool(body.get("dry_run"))

    rows = await _crm_health_rows(db)
    text = crm_mod.render_digest(rows, stale_days=stale_days)
    stale = sum(1 for r in rows
               if r["days_silent"] is None or r["days_silent"] >= stale_days)

    sent = False
    if not dry_run:
        notify: NotifyClient = request.app[NOTIFY_KEY]
        today = time.strftime("%Y-%m-%d", time.gmtime())
        sent = await notify.send(
            agent_id=f"crm-digest:{today}", kind="progress", message=text,
            metadata={"discord_channel": channel, "stale_count": stale})

    return web.json_response({
        "sent": sent, "dry_run": dry_run, "channel": channel,
        "stale_days": stale_days, "stale_count": stale,
        "client_count": len(rows), "text": text,
    })

def register_routes(app: web.Application) -> None:
    app.router.add_get("/admin/api/crm/clients", api_crm_clients_list)
    app.router.add_get("/admin/api/crm/clients/{cid}", api_crm_client_detail)
    app.router.add_put("/admin/api/projects/{slug}/client", api_project_client_link)
    app.router.add_put("/admin/api/projects/{slug}/deal", api_project_deal_link)
    app.router.add_post("/admin/api/crm/sync", api_crm_sync_post)
    app.router.add_get("/admin/api/crm/sync/status", api_crm_sync_status)
    app.router.add_get("/admin/api/crm/check", api_crm_check)
    app.router.add_post("/admin/api/crm/start", api_crm_start)
    app.router.add_get("/admin/api/crm/health", api_crm_health)
    app.router.add_post("/admin/api/crm/digest", api_crm_digest)
