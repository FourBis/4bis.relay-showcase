"""Equipo: identidad de Access, perfiles visibles y administración de roles."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from aiohttp import web

from . import identity
from .app_state import DB_KEY


async def api_me(request: web.Request) -> web.Response:
    role = identity.role_of(request)
    email = identity.requester(request)
    users = await request.app[DB_KEY].list_users()
    actor = next((user for user in users
                  if user["email"] == email.strip().lower()), None)
    return web.json_response({
        "email": email, "role": role,
        "display_name": identity.display_name(request),
        "role_label": identity.ROLE_LABELS.get(role, "Sin acceso"),
        "allowed_tabs": identity.ROLE_TABS.get(role, []),
        "project_slugs": (actor or {}).get("project_slugs", []),
    })


def _managed_roles(request: web.Request) -> list[str]:
    role = identity.role_of(request)
    if role == "owner":
        return list(identity.ROLE_LABELS)
    if role == "subadmin":
        return ["member"]
    raise web.HTTPForbidden(text="No puedes administrar el equipo.")


async def api_users_list(request: web.Request) -> web.Response:
    roles = _managed_roles(request)
    projects = await request.app[DB_KEY].list_projects()
    return web.json_response({
        "users": [{**user, "enabled": bool(user.get("enabled", 1))}
                  for user in await request.app[DB_KEY].list_users()],
        "me": identity.requester(request), "roles": roles,
        "role_labels": identity.ROLE_LABELS,
        "can_assign_projects": identity.role_of(request) == "owner",
        "projects": [{"slug": project["slug"], "name": project["name"]}
                     for project in projects if project.get("enabled", True)],
    })


def _check_origin(request: web.Request) -> None:
    origin = request.headers.get("Origin")
    if not origin:
        return
    try:
        parsed = urlsplit(origin)
        valid = parsed.scheme in ("http", "https") and parsed.netloc == request.host
    except ValueError:
        valid = False
    if not valid:
        raise web.HTTPForbidden(text="Origen no permitido.")


async def _body(request: web.Request) -> dict:
    _check_origin(request)
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="Se requiere application/json.")
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        raise web.HTTPBadRequest(text="JSON inválido.") from None
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="Se requiere un objeto JSON.")
    return body


async def _save(request: web.Request, *, email: str, role: str,
                name: str | None, enabled: bool,
                project_slugs: list[str] | None = None) -> web.Response:
    db = request.app[DB_KEY]
    # La transacción de DB protege al último Admin y los roles del target;
    # este lock también mantiene la recarga del cache en orden de escritura.
    async with db._upsert_lock:
        roles = _managed_roles(request)
        if project_slugs is not None and identity.role_of(request) != "owner":
            return web.json_response({"error": "Solo un owner puede asignar proyectos."}, status=403)
        if role not in roles:
            return web.json_response({"error": "No puedes asignar ese rol."}, status=403)
        try:
            await db.set_user_role(email, role, display_name=name, enabled=enabled,
                                   manageable_roles=tuple(roles),
                                   project_slugs=project_slugs)
        except ValueError as exc:
            if str(exc) == "last_admin":
                return web.json_response(
                    {"error": "Debe quedar al menos un Admin activo."}, status=409)
            if str(exc) == "forbidden_role":
                return web.json_response(
                    {"error": "No puedes modificar a ese usuario."}, status=403)
            if str(exc) == "project_slugs_role":
                return web.json_response(
                    {"error": "Solo miembros y subadmins pueden tener proyectos asignados."},
                    status=400)
            if str(exc) == "project_slugs_catalog":
                return web.json_response(
                    {"error": "La lista contiene proyectos inexistentes o deshabilitados."},
                    status=400)
            return web.json_response({"error": str(exc)}, status=400)
        identity.load_roles(await db.list_users())
    return web.json_response({"email": email, "role": role, "enabled": enabled,
                              **({"project_slugs": project_slugs}
                                 if project_slugs is not None else {})})


async def api_user_upsert(request: web.Request) -> web.Response:
    _managed_roles(request)
    body = await _body(request)
    email = body.get("email")
    role = body.get("role")
    name = body.get("display_name")
    enabled = body.get("enabled", True)
    project_slugs = body.get("project_slugs")
    if (not isinstance(email, str) or len(email) > 254
            or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email.strip())):
        return web.json_response({"error": "Correo inválido."}, status=400)
    if not isinstance(role, str) or role not in identity.ROLE_LABELS:
        return web.json_response({"error": "Rol inválido."}, status=400)
    if name is not None and (not isinstance(name, str) or len(name) > 100
                             or any(ord(c) < 32 for c in name)):
        return web.json_response({"error": "Nombre inválido (máximo 100 caracteres)."}, status=400)
    if not isinstance(enabled, bool):
        return web.json_response({"error": "enabled debe ser booleano."}, status=400)
    if "project_slugs" in body:
        if identity.role_of(request) != "owner":
            return web.json_response(
                {"error": "Solo un owner puede asignar proyectos."}, status=403)
        if (not isinstance(project_slugs, list) or len(project_slugs) > 50
                or any(not isinstance(slug, str) or not slug or len(slug) > 100
                       for slug in project_slugs)
                or len(set(project_slugs)) != len(project_slugs)):
            return web.json_response({"error": "Lista de proyectos inválida."}, status=400)
        if project_slugs and role not in ("member", "subadmin"):
            return web.json_response(
                {"error": "Solo miembros y subadmins pueden tener proyectos asignados."},
                status=400)
        projects = await request.app[DB_KEY].list_projects()
        enabled_slugs = {project["slug"] for project in projects
                         if project.get("enabled", True)}
        if any(slug not in enabled_slugs for slug in project_slugs):
            return web.json_response(
                {"error": "La lista contiene proyectos inexistentes o deshabilitados."},
                status=400)
    return await _save(request, email=email.strip().lower(), role=role,
                       name=name.strip() if name is not None else None, enabled=enabled,
                       project_slugs=project_slugs)


async def api_user_delete(request: web.Request) -> web.Response:
    """Compatibilidad de ruta: desactivar conserva la identidad histórica."""
    _managed_roles(request)
    _check_origin(request)
    email = request.match_info["email"].strip().lower()
    users = await request.app[DB_KEY].list_users()
    user = next((u for u in users if u["email"] == email), None)
    if user is None:
        return web.json_response({"error": "No existe ese usuario."}, status=404)
    return await _save(request, email=email, role=user["role"], name=None, enabled=False)
