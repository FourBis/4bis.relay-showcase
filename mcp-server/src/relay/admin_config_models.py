"""Admin configuration endpoints grouped by responsibility."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import time
from aiohttp import web
from pydantic_ai import Agent
from . import config as relay_config
from .app_state import DB_KEY
from .experts import ModelUnavailable, build_model

logger = logging.getLogger("relay.admin")

_MODEL_ROLE_KEYS = {
    "executor": "FOURBIS_MODEL",
    "planner": "FOURBIS_PLANNER_MODEL",
    "verifier": "FOURBIS_VERIFIER_MODEL",
    "documenter": "FOURBIS_DOCUMENTER_MODEL",
    "compactor": "FOURBIS_COMPACTOR_MODEL",
}

_EDITABLE_CONFIG_KEYS = (*relay_config.PANEL_SETTINGS,
                         *_MODEL_ROLE_KEYS.values())

_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

async def _roles_que_usan(db, spec: str) -> list[str]:
    """Referencias globales o de proyecto que apuntan a `spec`.

    La contracara de `_validate_model_role` (2026-08-26). Aquella valida
    al ESCRIBIR el rol; esta valida al borrar o apagar el modelo, que es
    el otro lado por donde se rompe la misma invariante.

    Sin esto el rol queda huérfano y el síntoma sale lejísimos de la
    causa: `build_model` no falla —para un provider con base_url fija
    arma el objeto igual— así que el error recién aparece cuando el
    proveedor rechaza un nombre de modelo que no existe, y se reporta
    como `provider_error`. O sea que borrar un modelo un martes se
    manifiesta el jueves como "se cayó minimax".
    """
    txt = (spec or "").strip()
    if not txt:
        return []
    cfg = await db.all_config()
    usados = {rol for rol, key in _MODEL_ROLE_KEYS.items()
              if (cfg.get(key) or "").strip() == txt}
    efectivos = {
        "executor": relay_config.model_spec,
        "planner": relay_config.planner_model_spec,
        "verifier": relay_config.verifier_model_spec,
        "documenter": relay_config.documenter_model_spec,
        "compactor": relay_config.compactor_model_spec,
    }
    for rol, getter in efectivos.items():
        if getter() == txt:
            usados.add(rol)
    if txt in (v.strip() for v in
               os.environ.get("FOURBIS_PLANNER_FALLBACK", "").split(",")):
        usados.add("planner_fallback")
    for project in await db.list_projects(enabled_only=False):
        defaults = project.get("defaults_json") or {}
        slug = project.get("slug") or "?"
        for rol in ("model", "planner_model", "graph_planner_model",
                    "verifier_model", "documenter_model"):
            if str(defaults.get(rol) or "").strip() == txt:
                usados.add(f"project:{slug}:{rol}")
        fallback = defaults.get("planner_fallback") or []
        if isinstance(fallback, str):
            fallback = fallback.split(",")
        elif not isinstance(fallback, (list, tuple)):
            fallback = []
        if txt in (str(v).strip() for v in fallback):
            usados.add(f"project:{slug}:planner_fallback")
    return sorted(usados)

async def _validate_model_role(db, spec: str) -> str:
    """`""` si el spec sirve para un rol, si no el motivo del rechazo.

    Vacío es válido: significa "cae por la cascada". Lo que NO puede
    pasar es guardar un spec inexistente o apagado — el rol quedaría
    tirando `ModelUnavailable` en cada run y el síntoma aparecería
    lejos de este form.
    """
    txt = (spec or "").strip()
    if not txt:
        return ""
    fila = await db.get_model(txt)
    if fila is None:
        return f"{txt}: no está en el catálogo de modelos"
    if not fila.get("enabled"):
        return f"{txt}: está apagado en el catálogo"
    return ""

async def api_models(request: web.Request) -> web.Response:
    """GET /admin/api/models[?all=1] — catálogo de modelos.

    Sin `all`, solo los prendidos: es lo que come el selector del chat.
    Con `all=1`, todo (la pantalla de administración), que puede ser
    >100 filas si se importó el catálogo de NVIDIA.

    `vision` viaja para que la UI avise ANTES de mandar en vez de dejar
    que el humano se coma el 400. El 400 sigue existiendo: el selector
    es comodidad, no control.
    """
    db: Database = request.app[DB_KEY]
    todos = request.query.get("all") in ("1", "true", "yes")
    filas = await db.list_models(only_enabled=not todos)
    return web.json_response({
        "models": [db.mask_key(f) for f in filas],
        "default": relay_config.model_spec(),
    })

async def api_model_upsert(request: web.Request) -> web.Response:
    """PUT /admin/api/models/{spec} — alta o edición de un modelo.

    Owner-only por el middleware de roles (no está en MEMBER_ALLOWED).
    Un `api_key` vacío NO borra la que hay: para borrarla hay que
    mandar null explícito, si no cualquier edición de la tarifa desde
    un form que no la muestra te la dejaría sin key.
    """
    db: Database = request.app[DB_KEY]
    spec = request.match_info["spec"]
    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    campos = {k: v for k, v in body.items() if k != "spec"}
    if campos.get("api_key", "sin tocar") == "":
        campos.pop("api_key")
    if "vision" in campos and campos["vision"] is not None:
        campos["vision"] = 1 if campos["vision"] else 0
    if "enabled" in campos:
        campos["enabled"] = 1 if campos["enabled"] else 0
        # Apagar un modelo que un rol está usando lo deja huérfano igual
        # que borrarlo: el rol sigue apuntando al spec y `_staged_model_spec`
        # lo devuelve sin mirar el catálogo. Se corta acá, que es donde
        # está el humano que lo apagó.
        if not campos["enabled"]:
            usados = await _roles_que_usan(db, spec)
            if usados:
                return web.json_response(
                    {"error": (f"{spec} está en uso por: {', '.join(usados)}. "
                               "Cambiá esas referencias antes de apagarlo."),
                     "roles": usados}, status=409)
    try:
        await db.upsert_model(spec, **campos)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    await _refrescar_catalogo(request.app)
    fila = await db.get_model(spec)
    return web.json_response(db.mask_key(fila) if fila else {})

async def api_model_test(request: web.Request) -> web.Response:
    """POST /admin/api/models/{spec}/test — ¿este modelo contesta?

    Owner-only por el middleware de roles (no está en MEMBER_ALLOWED).

    Existe porque hasta hoy cargar un modelo era a ciegas: se guardaba la
    fila y recién se sabía si servía cuando un run fallaba, con el error
    del proveedor envuelto como `provider_error` — el síntoma que menos
    dice. El caso real (2026-08-26) fue una key de xAI perfectamente
    válida contra una cuenta sin créditos: el proveedor lo explicaba en
    una línea y esa línea no llegaba a ninguna pantalla.

    Por eso lo que se devuelve es el error CRUDO, con su status HTTP.
    Traducirlo a "no se pudo conectar" sería volver al problema.

    Hace un turno real y mínimo (8 tokens) en vez de pinchar un endpoint
    de catálogo: lo que interesa es si ESTE spec responde, y un
    `/v1/models` sano convive con un nombre de modelo que no existe.
    """
    db: Database = request.app[DB_KEY]
    spec = request.match_info["spec"]
    if await db.get_model(spec) is None:
        return web.json_response(
            {"error": f"{spec} no está en el catálogo"}, status=404)
    # El cache es lo que lee `build_model`; refrescarlo acá evita el
    # "lo edité y sigue probando lo viejo" cuando la fila se tocó por
    # fuera del upsert.
    await _refrescar_catalogo(request.app)

    from pydantic_ai import Agent
    from pydantic_ai.exceptions import ModelHTTPError

    t0 = time.monotonic()
    try:
        modelo = build_model(spec)
    except ModelUnavailable as e:
        # Falta la key: no es un fallo del proveedor, es config nuestra.
        return web.json_response(
            {"ok": False, "kind": "sin_key", "error": str(e)})
    except Exception as e:  # noqa: BLE001 — spec roto, provider raro
        return web.json_response(
            {"ok": False, "kind": "spec", "error": f"{type(e).__name__}: {e}"})

    try:
        agent = Agent(modelo)
        r = await asyncio.wait_for(
            agent.run("Responde exactamente: ok",
                      model_settings={"max_tokens": _TEST_MAX_TOKENS}),
            timeout=_TEST_TIMEOUT_S)
    except asyncio.TimeoutError:
        return web.json_response({
            "ok": False, "kind": "timeout",
            "error": f"no contestó en {_TEST_TIMEOUT_S:.0f}s"})
    except ModelHTTPError as e:
        # El caso que motiva todo esto. `body` trae el JSON del proveedor
        # (código, mensaje, a veces hasta el link para arreglarlo).
        return web.json_response({
            "ok": False, "kind": "http", "status": e.status_code,
            "error": str(getattr(e, "body", "") or e)[:600]})
    except Exception as e:  # noqa: BLE001 — red, TLS, provider exótico
        return web.json_response({
            "ok": False, "kind": type(e).__name__, "error": str(e)[:600]})

    # `usage` es PROPIEDAD en pydantic-ai 2.x, no método: con paréntesis
    # tira `'RunUsage' object is not callable` y el botón reportaría
    # error justo en el caso en que el modelo anduvo.
    u = r.usage
    return web.json_response({
        "ok": True,
        "ms": int((time.monotonic() - t0) * 1000),
        "reply": str(r.output)[:120],
        "tokens_in": getattr(u, "input_tokens", None),
        "tokens_out": getattr(u, "output_tokens", None),
    })

async def api_model_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/models/{spec}"""
    db: Database = request.app[DB_KEY]
    spec = request.match_info["spec"]
    usados = await _roles_que_usan(db, spec)
    if usados:
        return web.json_response(
            {"error": (f"{spec} está en uso por: {', '.join(usados)}. "
                       "Cambiá esas referencias antes de borrarlo."),
             "roles": usados}, status=409)
    await db.delete_model(spec)
    await _refrescar_catalogo(request.app)
    return web.json_response({"deleted": request.match_info["spec"]})

async def _refrescar_catalogo(app: web.Application) -> None:
    """El cache de experts se llena en el startup; editar por la UI
    tiene que verse sin reiniciar, si no la pantalla miente."""
    from . import experts as experts_mod
    db: Database = app[DB_KEY]
    experts_mod.load_catalog(await db.list_models())

_TEST_MAX_TOKENS = 8

_TEST_TIMEOUT_S = 45.0

def register_model_routes(app: web.Application) -> None:
    app.router.add_get("/admin/api/models", api_models)
    app.router.add_put("/admin/api/models/{spec}", api_model_upsert)
    app.router.add_delete("/admin/api/models/{spec}", api_model_delete)
    app.router.add_post("/admin/api/models/{spec}/test", api_model_test)
