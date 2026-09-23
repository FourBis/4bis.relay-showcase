"""CRUD del catálogo MCP e instalador de repositorios MCP."""
from __future__ import annotations

import json
import os

from aiohttp import web
from . import mcp_pool
from .app_state import DB_KEY, MCP_INSTALLER_KEY
from .mcp_installer import McpInstaller
from .admin_common import _serialize

# ---------- MCPs (plan MCP_REGISTRY F3: CRUD del catálogo) ----------

# Campos editables desde la UI. health/vet_* los setea el sistema
# (pool F1 / pipeline de install F2), no el usuario.
_MCP_EDITABLE = ("capability", "transport", "command", "args", "url", "env",
                 "read_only", "on_demand", "idle_timeout_s", "enabled")

async def _mcp_with_links(db) -> list[dict]:
    rows = await db.list_mcp_servers()
    links = await db.run(
        "SELECT l.mcp_id, p.slug FROM project_mcp_servers l "
        "JOIN projects p ON p.id = l.project_id ORDER BY p.slug")
    by_mcp: dict[int, list[str]] = {}
    for ln in links:
        by_mcp.setdefault(ln["mcp_id"], []).append(ln["slug"])
    for r in rows:
        # sin links = global (sirve a todos los proyectos)
        r["project_slugs"] = by_mcp.get(r["id"], [])
    return rows

async def _mcp_set_links(db, mcp_id: int, slugs: list) -> str | None:
    """Reemplaza los links de un MCP. Devuelve mensaje de error o None."""
    ids = []
    for slug in slugs:
        p = await db.get_project(str(slug).strip())
        if p is None:
            return f"proyecto desconocido: {slug!r}"
        ids.append(p["id"])
    await db.run("DELETE FROM project_mcp_servers WHERE mcp_id=?", (mcp_id,))
    for pid in ids:
        await db.link_mcp(pid, mcp_id)
    return None

async def api_mcp_list(request: web.Request) -> web.Response:
    """GET /admin/api/mcp — catálogo completo (con project_slugs)."""
    db = request.app[DB_KEY]
    rows = await _mcp_with_links(db)
    return web.json_response({"mcp_servers": _serialize(rows)})

async def api_mcp_upsert(request: web.Request) -> web.Response:
    """POST /admin/api/mcp — alta/edición manual de un MCP.

    Body: {name, capability, transport?, command?, args?, url?, env?,
           read_only?, on_demand?, idle_timeout_s?, enabled?,
           project_slugs?: [..]}  (project_slugs vacío/omitido = global)

    Para el alta vía GitHub con vetting está POST /admin/api/mcp/install
    (F2, aún no implementada).
    """
    db = request.app[DB_KEY]
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    body = body or {}
    name = (body.get("name") or "").strip()
    if not name:
        return web.json_response({"error": "name requerido"}, status=400)
    existing = await db.get_mcp_server(name)
    if existing is None and not (body.get("capability") or "").strip():
        return web.json_response({"error": "capability requerida"}, status=400)
    data = {"name": name}
    for col in _MCP_EDITABLE:
        if col in body:
            data[col] = body[col]
    if "env" in data and not isinstance(data["env"], dict):
        return web.json_response(
            {"error": "env debe ser objeto JSON (refs \"env:NAME\" para "
                      "credenciales)"}, status=400)
    if "args" in data and not isinstance(data["args"], list):
        return web.json_response({"error": "args debe ser lista"}, status=400)
    row = await db.upsert_mcp_server(data)
    if isinstance(body.get("project_slugs"), list):
        err = await _mcp_set_links(db, row["id"], body["project_slugs"])
        if err:
            return web.json_response({"error": err}, status=400)
    rows = await _mcp_with_links(db)
    row = next(r for r in rows if r["name"] == row["name"])
    return web.json_response({"mcp": _serialize(row)},
                             status=200 if existing else 201)

async def api_mcp_patch(request: web.Request) -> web.Response:
    """PATCH /admin/api/mcp/{name} — toggles / campos parciales / links."""
    db = request.app[DB_KEY]
    name = request.match_info["name"]
    existing = await db.get_mcp_server(name)
    if existing is None:
        return web.json_response({"error": "no encontrado"}, status=404)
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    body = body or {}
    data = {"name": existing["name"]}
    for col in _MCP_EDITABLE:
        if col in body:
            data[col] = body[col]
    if len(data) > 1:
        await db.upsert_mcp_server(data)
    if isinstance(body.get("project_slugs"), list):
        err = await _mcp_set_links(db, existing["id"], body["project_slugs"])
        if err:
            return web.json_response({"error": err}, status=400)
    rows = await _mcp_with_links(db)
    row = next(r for r in rows if r["id"] == existing["id"])
    return web.json_response({"mcp": _serialize(row)})

async def api_mcp_health(request: web.Request) -> web.Response:
    """POST /admin/api/mcp/{name}/health — re-chequea el handshake AHORA.

    El `health` de la tabla se escribía solo en el probe del boot, así
    que arreglar un MCP roto (cambiarle los args, cargar la credencial
    que faltaba) y confirmarlo obligaba a reiniciar el relay entero
    mientras la UI seguía mostrando `handshake ✗` sobre algo que ya
    andaba.

    Levanta el proceso de verdad — es el punto: un handshake que no
    spawnea no prueba nada. Devuelve el error cuando falla, que es lo
    que hace falta para arreglarlo.
    """
    db = request.app[DB_KEY]
    name = request.match_info["name"]
    row = await db.get_mcp_server(name)
    if row is None:
        return web.json_response({"error": "no encontrado"}, status=404)
    health, error = await mcp_pool.probe_and_store_health(db, row)
    return web.json_response({"name": row["name"], "health": health,
                              "error": error})

async def api_mcp_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/mcp/{name} — baja (links caen por cascade)."""
    db = request.app[DB_KEY]
    name = request.match_info["name"]
    deleted = await db.delete_mcp_server(name)
    if not deleted:
        return web.json_response({"error": "no encontrado"}, status=404)
    return web.json_response({"deleted": True, "name": name})

async def api_mcp_install_start(request: web.Request) -> web.Response:
    """POST /admin/api/mcp/install {url} — arranca el pipeline F2.

    Crea un job transitorio y dispara clone → scan → vetting en
    background. Responde inmediato con `{job_id, state: "pending"}`
    y la UI hace polling a `GET /admin/api/mcp/install/{job_id}`.
    """
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    url = (body.get("url") or "").strip()
    if not url:
        return web.json_response(
            {"error": "url requerida (https://github.com/owner/repo)"},
            status=400)
    if url.startswith("file://"):
        if not os.environ.get("FOURBIS_MCP_ALLOW_FILE_URL"):
            return web.json_response(
                {"error": "url debe ser http(s) o git@ "
                          "(file:// solo con FOURBIS_MCP_ALLOW_FILE_URL=1, "
                          "para E2E/tests)"},
                status=400)
    elif not (url.startswith("http://") or url.startswith("https://")
              or url.startswith("git@")):
        return web.json_response(
            {"error": "url debe ser http(s) o git@"}, status=400)
    installer: McpInstaller = request.app[MCP_INSTALLER_KEY]
    try:
        job = await installer.start(url)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"job_id": job.id, **job.to_public()},
                             status=202)

async def api_mcp_install_status(request: web.Request) -> web.Response:
    """GET /admin/api/mcp/install/{job_id} — estado actual del job."""
    installer: McpInstaller = request.app[MCP_INSTALLER_KEY]
    job_id = request.match_info["job_id"]
    job = installer.get(job_id)
    if job is None:
        return web.json_response(
            {"error": "job no encontrado (¿expiró? reinstala)"},
            status=404)
    return web.json_response(job.to_public())

async def api_mcp_install_confirm(request: web.Request) -> web.Response:
    """POST /admin/api/mcp/install/{job_id}/confirm — corre install + handshake.

    Body opcional: `{command?, args?, env?, capability?, name?}` — si
    el detector automático no acertó (proposal con `needs_manual=true`)
    o quieres cambiarle el nombre/capability, pasalo acá. El override
    reemplaza la propuesta antes del install.
    """
    installer: McpInstaller = request.app[MCP_INSTALLER_KEY]
    db = request.app[DB_KEY]
    job_id = request.match_info["job_id"]
    override: dict = {}
    try:
        body = await request.json() if request.body_exists else {}
        if isinstance(body, dict):
            override = body
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)

    # Filtro override: solo campos válidos (los mismos que acepta
    # api_mcp_upsert para proposal más capability/name).
    allowed = {"command", "args", "env", "capability", "name"}
    override = {k: v for k, v in override.items() if k in allowed}

    job = installer.get(job_id)
    if job is None:
        return web.json_response(
            {"error": "job no encontrado (¿expiró? reinstala)"},
            status=404)
    if job.state.value == "awaiting_confirm" \
            and job.proposal.get("needs_manual") \
            and "command" not in override:
        return web.json_response(
            {"error": "el detector no pudo inferir comando; pasa "
                      "{command, args} en el body"}, status=400)

    try:
        job = await installer.confirm(job_id, db, override=override or None)
    except KeyError as e:
        return web.json_response({"error": str(e)}, status=404)
    except RuntimeError as e:
        return web.json_response(
            {"error": str(e),
             "job": job.to_public(),
             "hint": "ajusta override y reintenta"}, status=409)

    return web.json_response(job.to_public())

def register_routes(app: web.Application) -> None:
    app.router.add_get("/admin/api/mcp", api_mcp_list)
    app.router.add_post("/admin/api/mcp", api_mcp_upsert)
    app.router.add_post("/admin/api/mcp/install", api_mcp_install_start)
    app.router.add_get("/admin/api/mcp/install/{job_id}", api_mcp_install_status)
    app.router.add_post("/admin/api/mcp/install/{job_id}/confirm", api_mcp_install_confirm)
    app.router.add_post("/admin/api/mcp/{name}/health", api_mcp_health)
    app.router.add_patch("/admin/api/mcp/{name}", api_mcp_patch)
    app.router.add_delete("/admin/api/mcp/{name}", api_mcp_delete)
