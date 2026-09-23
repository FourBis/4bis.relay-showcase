"""Rutas de comandos dinámicos, huérfanos CBM y usuarios."""
from __future__ import annotations
import json
import logging
import os
import time
from pathlib import Path
from aiohttp import web
from . import identity
from .experts import cbm_binary_path
from .app_state import COMMANDS_KEY, DB_KEY, SESSIONS_KEY
from .admin_common import _has_git, _serialize

from .admin_cbm import _cbm_list_projects

logger = logging.getLogger("relay.admin")

async def api_commands_list(request: web.Request) -> web.Response:
    """GET /admin/api/commands — lista todos los comandos dinámicos."""
    db = request.app[DB_KEY]
    cmds = await db.list_commands(enabled_only=False)
    # Si la DB está vacía pero el registry en memoria tiene comandos built-in,
    # los devolvemos igual (los built-in se cargan en _on_startup).
    if not cmds:
        from .commands import CommandRegistry
        reg: CommandRegistry | None = request.app.get(COMMANDS_KEY)
        if reg is not None:
            # El registry tiene dispatch() pero no un dump. Listamos por nombre.
            for n in reg.names():
                cmds.append({
                    "name": n, "description": "(built-in)",
                    "handler": "(built-in)", "args_schema": None,
                    "enabled": True,
                })
    return web.json_response({"commands": _serialize(cmds)})

async def api_commands_upsert(request: web.Request) -> web.Response:
    """POST /admin/api/commands — crea o actualiza un comando.

    Body: {name, description, handler, args_schema, enabled?}.

    `handler` es un dotted path Python del estilo `relay.handlers.foo`
    o `relay.commands.builtin_build`. El server NO verifica que
    importe — eso lo hace `CommandRegistry.load_from_db` al recibir
    una conexión nueva. Si el import falla, el comando queda en la
    DB pero no se ejecuta; desde esta UI vas a ver el error.
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
    handler = (body.get("handler") or "").strip()
    if not handler:
        return web.json_response({"error": "handler requerido"}, status=400)
    cmd = {
        "name": name,
        "description": (body.get("description") or "").strip(),
        "handler": handler,
        "args_schema": body.get("args_schema"),
        "enabled": bool(body.get("enabled", True)),
    }
    await db.upsert_command(cmd)
    return web.json_response({"command": _serialize(cmd)}, status=201)

async def api_commands_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/commands/{name} — borra un comando por nombre."""
    db = request.app[DB_KEY]
    name = request.match_info["name"]
    deleted = await db.delete_command(name)
    if not deleted:
        return web.json_response({"error": "no encontrado"}, status=404)
    return web.json_response({"deleted": True, "name": name})

async def api_commands_run(request: web.Request) -> web.Response:
    """POST /admin/api/commands/{name}/run — corre el comando en el relay.

    Body: {args: {...}}. Útil para probar un comando desde la UI sin
    pasar por Discord. Devuelve {output: str, duration_ms: int}.
    """
    from .commands import CommandContext
    db = request.app[DB_KEY]
    reg: CommandRegistry | None = request.app.get(COMMANDS_KEY)
    if reg is None:
        return web.json_response({"error": "registry no disponible"}, status=503)
    name = request.match_info["name"]
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    body = body or {}
    args = body.get("args") or {}
    # No hay `project` acá a propósito: la UI mandaba uno y este handler
    # lo leía en una variable que no usaba nadie, así que el campo del
    # form no hacía nada. No se puede arreglar pasándolo al contexto —
    # cada comando pide su proyecto con SU clave (`build` usa `project`,
    # `memoria` usa `target`, `cancel` usa `chat`), así que el proyecto
    # viaja dentro de `args` y el placeholder de la UI lo dice.
    t0 = time.monotonic()
    try:
        out = await reg.dispatch(name, args, CommandContext(
            db=db, sessions=request.app.get(SESSIONS_KEY),
            source="admin-ui",
        ))
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": str(e)}, status=500)
    dur = int((time.monotonic() - t0) * 1000)
    return web.json_response({"output": out, "duration_ms": dur})

async def api_cbm_orphans(request: web.Request) -> web.Response:
    """GET /admin/api/cbm/orphans — lista cbm projects sin fila en projects.

    Devuelve una lista con root_path, cbm_name, nodes, edges, has_git,
    suggested_slug (derivado del basename). El cliente decide qué crear.
    """
    db = request.app[DB_KEY]
    if not cbm_binary_path():
        return web.json_response({"error": "cbm no instalado"}, status=503)

    try:
        data = await _cbm_list_projects()
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": str(e)}, status=500)

    all_projects = await db.list_projects(enabled_only=False)
    by_path: dict[str, dict] = {}
    for p in all_projects:
        rp = (p.get("repo_path") or "").replace("\\", "/").rstrip("/").lower()
        if rp:
            by_path[rp] = p

    orphans: list[dict] = []
    for c in data.get("projects", []):
        rp_raw = c.get("root_path", "")
        rp_key = rp_raw.replace("\\", "/").rstrip("/").lower()
        if not rp_key:
            continue
        if rp_key in by_path:
            continue  # ya tiene experto
        path = Path(rp_raw)
        base = path.name
        # Slug sugerido: minúsculas, espacios y puntos → guion.
        slug = base.lower().replace(" ", "-")
        slug = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in slug)
        while "--" in slug:
            slug = slug.replace("--", "-")
        slug = slug.strip("-") or "unnamed"
        orphans.append({
            "root_path": rp_raw,
            "cbm_name": c.get("name", ""),
            "nodes": c.get("nodes", 0),
            "edges": c.get("edges", 0),
            "size_bytes": c.get("size_bytes", 0),
            "has_git": _has_git(path),
            "suggested_slug": slug,
            "exists_in_db": False,
        })

    # Filtrar los que el usuario marcó como ignorados.
    ignored = set(await db.list_ignored_orphans())
    if ignored:
        orphans = [o for o in orphans if o["cbm_name"] not in ignored]

    # Ordenar por nodes desc — los más grandes primero son los más útiles.
    orphans.sort(key=lambda o: -o["nodes"])
    return web.json_response({
        "count": len(orphans),
        "orphans": orphans,
        "indexed_total": len(data.get("projects", [])),
        "with_expert": len(by_path),
        "ignored_count": len(ignored),
    })

async def api_orphan_ignore(request: web.Request) -> web.Response:
    """POST /admin/api/cbm/orphans/ignore — marca un cbm_name como ignorado.

    Body: {"cbm_name": "C-Users-..."}
    """
    db = request.app[DB_KEY]
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    body = body or {}
    cbm_name = (body.get("cbm_name") or "").strip()
    if not cbm_name:
        return web.json_response({"error": "cbm_name requerido"}, status=400)
    reason = (body.get("reason") or "").strip()
    ok = await db.ignore_orphan(cbm_name, reason)
    if not ok:
        return web.json_response(
            {"error": "ya estaba ignorado", "cbm_name": cbm_name},
            status=409,
        )
    return web.json_response({"ignored": True, "cbm_name": cbm_name})

async def api_orphan_unignore(request: web.Request) -> web.Response:
    """DELETE /admin/api/cbm/orphans/ignore?cbm_name=X — des-ignora."""
    db = request.app[DB_KEY]
    cbm_name = (request.query.get("cbm_name") or "").strip()
    if not cbm_name:
        return web.json_response({"error": "cbm_name requerido"}, status=400)
    ok = await db.unignore_orphan(cbm_name)
    if not ok:
        return web.json_response(
            {"error": "no estaba ignorado", "cbm_name": cbm_name},
            status=404,
        )
    return web.json_response({"unignored": True, "cbm_name": cbm_name})

async def api_orphan_ignored_list(request: web.Request) -> web.Response:
    """GET /admin/api/cbm/orphans/ignored — lista los ignorados."""
    db = request.app[DB_KEY]
    rows = await db.run(
        "SELECT cbm_name, reason, created_at FROM ignored_orphans "
        "ORDER BY created_at DESC"
    )
    return web.json_response({"ignored": _serialize(rows), "count": len(rows)})

def _slugify(raw: str) -> str:
    """Normaliza a slug seguro: [a-z0-9-], sin dobles guiones."""
    slug = (raw or "").strip().lower().replace(" ", "-")
    slug = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in slug)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "unnamed"

async def api_admin_restart(request: web.Request) -> web.Response:
    """POST /admin/api/restart — escribe un marker file en state/ y
    devuelve 200. NO mata el proceso (eso lo hace el script PowerShell
    o el supervisor externo leyendo el marker).

    Patron: la UI dispara, el handler escribe state/restart-requested-at.txt
    con timestamp + pid del relay, devuelve. El script externo
    (restart-4bis-relay.ps1) o un supervisor mira ese archivo y
    decide qué hacer. Si no hay supervisor, el usuario corre el
    .ps1 manualmente.

    Razon: os.execv mata el proceso en seco y si el proceso no
    arranca de nuevo (supervisor ausente), el relay queda MUERTO
    y la UI no se puede reconectar nunca sin reiniciar la maquina.
    Con marker file + script externo el peor caso es: usuario ve
    banner en UI "restart solicitado", corre .ps1 cuando puede.
    """
    state_dir = Path("state")
    state_dir.mkdir(exist_ok=True)
    marker = state_dir / "restart-requested-at.txt"
    marker.write_text(
        f"{time.time()}\nPID={os.getpid()}\nrequested_by={request.remote}\n",
        encoding="utf-8")
    return web.json_response({
        "status": "marker_written",
        "marker_path": str(marker),
        "pid": os.getpid(),
        "next_step": "ejecutar restart-4bis-relay.ps1 o relanzar manualmente",
    })

async def api_me(request: web.Request) -> web.Response:
    """GET /admin/api/me — quién está mirando.

    Hoy no hay forma de saber en qué sesión estás. Esto lo resuelve y de
    paso es la verificación end-to-end del middleware de identidad: por
    el túnel devuelve tu mail, desde localhost devuelve 'owner'.

    El `role` que devuelve es informativo — sirve para que la UI esconda
    lo que no corresponde. Quien mande es `require_role`, del lado del
    server: mentir acá no habilita nada.
    """
    return web.json_response({
        "email": identity.requester(request),
        "role": identity.role_of(request),
    })

async def _recargar_roles(db) -> None:
    """Refresca `identity._roles` desde la tabla. Se llama después de
    CADA escritura: el cache es por proceso, así que un cambio sin esto
    no aplica hasta el próximo reinicio."""
    identity.load_roles(await db.list_users())

async def api_users_list(request: web.Request) -> web.Response:
    """GET /admin/api/users — quiénes son owners."""
    db = request.app[DB_KEY]
    return web.json_response({
        "users": await db.list_users(),
        # Para que la UI pueda avisar "este sos vos" y no dejarte
        # quitarte el owner a vos mismo sin querer.
        "me": identity.requester(request),
        "roles": ["owner", "member"],
    })

async def api_user_upsert(request: web.Request) -> web.Response:
    """PUT /admin/api/users — alta o cambio de rol. Body: {email, role}."""
    db = request.app[DB_KEY]
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    role = (body.get("role") or "").strip()
    if "@" not in email or len(email) < 3:
        return web.json_response({"error": "email inválido"}, status=400)
    if role not in ("owner", "member"):
        return web.json_response(
            {"error": "role debe ser owner o member"}, status=400)
    # Bajarse a sí mismo a member deja el relay sin quien lo administre
    # si es el último owner. El check es sobre la TABLA, no sobre quién
    # pide: desde localhost `requester` es "owner" (sin mail) y ahí este
    # guard no aplica ni hace falta — esa sesión manda igual.
    if role == "member":
        owners = [u for u in await db.list_users() if u["role"] == "owner"]
        if len(owners) == 1 and owners[0]["email"] == email:
            return web.json_response(
                {"error": "es el único owner: dejarías el relay sin "
                          "administrador. Agregá otro owner primero."},
                status=409)
    await db.set_user_role(email, role)
    await _recargar_roles(db)
    return web.json_response({"email": email, "role": role})

async def api_user_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/users/{email} — lo devuelve a `member`."""
    db = request.app[DB_KEY]
    email = (request.match_info["email"] or "").strip().lower()
    owners = [u for u in await db.list_users() if u["role"] == "owner"]
    if len(owners) == 1 and owners[0]["email"] == email:
        return web.json_response(
            {"error": "es el único owner: dejarías el relay sin "
                      "administrador. Agregá otro owner primero."},
            status=409)
    if not await db.delete_user(email):
        return web.json_response({"error": "no existe"}, status=404)
    await _recargar_roles(db)
    return web.json_response({"deleted": True, "email": email})
