"""Admin configuration endpoints grouped by responsibility."""
from __future__ import annotations
import json
import logging
import math
import re
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse
from aiohttp import web
from . import __version__ as RELAY_VERSION
from . import config as relay_config
from .app_state import BIND_HOST_KEY, DB_KEY

from .admin_config_models import _MODEL_ROLE_KEYS, _validate_model_role

logger = logging.getLogger("relay.admin")

async def _refresh_runtime_config(db) -> None:
    """Refresca config._runtime desde db.all_config() sin reiniciar."""
    try:
        all_cfg = await db.all_config()
        relay_config.set_runtime_config(all_cfg or {})
    except Exception as e:  # noqa: BLE001 — best-effort, no rompemos el set
        logger.warning("refresh runtime config falló: %r", e)

_EDITABLE_CONFIG_KEYS = (*relay_config.PANEL_SETTINGS,
                         *_MODEL_ROLE_KEYS.values())

_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

_SECRET_PREFIX = "secret:"

def _public_panel_settings(stored: dict[str, str]) -> tuple[dict, dict]:
    """Schema + state for Config, without serializing secret values."""
    schema: dict[str, dict] = {}
    states: dict[str, dict] = {}
    for key, raw_meta in relay_config.PANEL_SETTINGS.items():
        meta = dict(raw_meta)
        kind = meta.get("type")
        # A default is useful to the form, except where it would invite a
        # caller to mistake a secret's state for a readable value.
        schema[key] = meta
        if kind == "secret":
            states[key] = {"configured": bool(stored.get(_SECRET_PREFIX + key))}
        else:
            states[key] = {"value": stored.get(key, str(meta.get("default", "")))}

    # Dynamic MCP credentials use the same public shape.  The name is not
    # sensitive; the stored value never leaves this function.
    for key, value in stored.items():
        if key.startswith(_SECRET_PREFIX):
            name = key[len(_SECRET_PREFIX):]
            states.setdefault(name, {"configured": bool(value)})
    return schema, states

def _number_is_integral(meta: Mapping[str, object]) -> bool:
    """Counters and ports reject fractions; explicitly fractional bounds don't."""
    return all(not isinstance(meta.get(k), float) for k in ("default", "min", "max"))

def _validate_panel_value(key: str, raw: object, meta: Mapping[str, object]) -> tuple[str, str]:
    """Normalize one public Config value without leaking its input in errors."""
    kind = meta.get("type")
    if kind == "boolean":
        if raw is True or raw in (1, "1", "true", "on"):
            return "1", ""
        if raw is False or raw in (0, "0", "false", "off"):
            return "0", ""
        return "", f"{key}: booleano inválido"
    if kind == "number":
        if isinstance(raw, bool):
            return "", f"{key}: número inválido"
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return "", f"{key}: número inválido"
        if not math.isfinite(number):
            return "", f"{key}: número finito requerido"
        if _number_is_integral(meta) and not number.is_integer():
            return "", f"{key}: entero requerido"
        minimum, maximum = meta.get("min"), meta.get("max")
        if minimum is not None and number < float(minimum):
            return "", f"{key}: mínimo {minimum}"
        if maximum is not None and number > float(maximum):
            return "", f"{key}: máximo {maximum}"
        return (str(int(number)) if _number_is_integral(meta) else str(number)), ""
    if not isinstance(raw, str):
        return "", f"{key}: texto requerido"
    value = raw.strip()
    if kind == "url" and value:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return "", f"{key}: URL http/https requerida"
    if kind == "path" and key == "FOURBIS_REPOS_ROOT" and value:
        if not Path(value).expanduser().is_dir():
            return "", f"{key}: directorio inexistente"
    if key == "DISCORD_GUILD_ID" and value and not value.isdigit():
        return "", "DISCORD_GUILD_ID: número requerido"
    return value, ""

def _validate_model_prices(raw: str) -> tuple[str, str]:
    """Valida el JSON de tarifas → `(normalizado, error)`.

    Entrada de la Admin UI: se valida forma y rango acá, no al leerlo.
    Un JSON roto guardado sin chequear haría que las métricas dejen de
    calcular costo en silencio y sin decir por qué.

    Vacío es válido: significa "sin tarifas", y deja los costos en null.
    """
    txt = (raw or "").strip()
    if not txt:
        return "", ""
    try:
        data = json.loads(txt)
    except (json.JSONDecodeError, TypeError) as e:
        return "", f"JSON inválido: {e}"
    if not isinstance(data, dict):
        return "", "se esperaba un objeto {modelo: {in, out}}"
    for spec, val in data.items():
        if not isinstance(spec, str) or not spec.strip():
            return "", "hay una clave de modelo vacía"
        if not isinstance(val, dict):
            return "", f"{spec}: se esperaba {{in, out}}"
        for k in ("in", "out"):
            if k not in val:
                return "", f"{spec}: falta '{k}'"
        for k, v in val.items():
            if k not in ("in", "out", "ref_in", "ref_out"):
                return "", f"{spec}: clave desconocida {k!r}"
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                return "", f"{spec}.{k}: se esperaba un número"
            if v < 0:
                return "", f"{spec}.{k}: no puede ser negativo"
    return json.dumps(data, ensure_ascii=False), ""

_CONFIG_DEFAULTS = {"RELAY_HOST": "127.0.0.1"}

_VALID_RELAY_HOSTS = ("127.0.0.1", "0.0.0.0")

async def api_config_get(request: web.Request) -> web.Response:
    """GET /admin/api/config — system_config + valores efectivos.

    `config` son las filas de la tabla (con defaults aplicados);
    `effective` es lo que el proceso está usando AHORA: el bind real
    (RELAY_HOST recién aplica al próximo arranque) y el repos_root
    resuelto por config.repos_root().
    """
    db = request.app[DB_KEY]
    stored = await db.all_config()
    bind_host = request.app.get(BIND_HOST_KEY) or "127.0.0.1"
    return web.json_response({
        # Una clave por entrada de la whitelist: si se agrega una editable
        # y no se devuelve acá, el form la guarda pero nunca la muestra de
        # vuelta (pasó con GITHUB_BOARD_* el 2026-08-01).
        "config": {k: stored.get(k, _CONFIG_DEFAULTS.get(k, ""))
                   for k in _EDITABLE_CONFIG_KEYS},
        "effective": {
            "bind_host": bind_host,
            "localhost_guard_active": True,
            "repos_root": relay_config.repos_root(),
            "version": RELAY_VERSION,
        },
        "editable_keys": list(_EDITABLE_CONFIG_KEYS),
        "restart_required_keys": ["RELAY_HOST"],
        # Qué modelo resuelve cada rol AHORA. Un rol vacío en el form no
        # dice nada por sí solo: hay que saberse la cascada de memoria
        # (system_config → .env → compactor → ejecutor) para entender con
        # qué va a correr. Acá se ve el resultado.
        "model_roles": {
            "keys": dict(_MODEL_ROLE_KEYS),
            "effective": {
                "executor": relay_config.model_spec(),
                "planner": relay_config.planner_model_spec(),
                "verifier": relay_config.verifier_model_spec(),
                "documenter": relay_config.documenter_model_spec(),
                "compactor": relay_config.compactor_model_spec(),
            },
        },
    })

async def api_config_put(request: web.Request) -> web.Response:
    """PUT /admin/api/config — body {clave: valor}, whitelist estricta.

    RELAY_HOST toma efecto en el próximo arranque (el bind de aiohttp
    no se puede cambiar en vivo); FOURBIS_REPOS_ROOT aplica al instante
    (refresca el snapshot runtime).
    """
    db = request.app[DB_KEY]
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    if not isinstance(body, dict) or not body:
        return web.json_response({"error": "body vacío"}, status=400)

    unknown = sorted(k for k in body if k not in _EDITABLE_CONFIG_KEYS)
    if unknown:
        return web.json_response(
            {"error": f"claves no editables: {', '.join(unknown)}",
             "editable_keys": list(_EDITABLE_CONFIG_KEYS)},
            status=400,
        )

    changed: dict[str, str] = {}
    if "RELAY_HOST" in body:
        host = str(body["RELAY_HOST"]).strip()
        if host not in _VALID_RELAY_HOSTS:
            return web.json_response(
                {"error": f"RELAY_HOST inválido: {host!r}",
                 "valid": list(_VALID_RELAY_HOSTS)},
                status=400,
            )
        changed["RELAY_HOST"] = host
    if "FOURBIS_REPOS_ROOT" in body:
        root = str(body["FOURBIS_REPOS_ROOT"]).strip()
        if root and not Path(root).is_dir():
            return web.json_response(
                {"error": f"no es un directorio: {root}"}, status=400)
        changed["FOURBIS_REPOS_ROOT"] = root
    # Tablero de empresa (fase 5). El owner no se valida contra GitHub
    # acá: guardar config no debería depender de que `gh` conteste. Si
    # está mal, el panel lo dice cuando lo abrís.
    if "MODEL_PRICES" in body:
        norm, err = _validate_model_prices(str(body["MODEL_PRICES"] or ""))
        if err:
            return web.json_response({"error": f"MODEL_PRICES: {err}"},
                                     status=400)
        changed["MODEL_PRICES"] = norm
    if "GITHUB_BOARD_OWNER" in body:
        changed["GITHUB_BOARD_OWNER"] = str(body["GITHUB_BOARD_OWNER"]).strip()
    if "GITHUB_BOARD_NUMBER" in body:
        raw = str(body["GITHUB_BOARD_NUMBER"]).strip()
        if raw and not raw.isdigit():
            return web.json_response(
                {"error": f"GITHUB_BOARD_NUMBER debe ser un número: {raw!r}"},
                status=400)
        changed["GITHUB_BOARD_NUMBER"] = raw
    # Guild de Discord (snowflake numérico). Vacío = limpiar. Habilita el
    # link al canal en el tab Proyectos (discord.com/channels/<guild>/<ch>).
    if "DISCORD_GUILD_ID" in body:
        raw = str(body["DISCORD_GUILD_ID"]).strip()
        if raw and not raw.isdigit():
            return web.json_response(
                {"error": f"DISCORD_GUILD_ID debe ser un número: {raw!r}"},
                status=400)
        changed["DISCORD_GUILD_ID"] = raw
    # Modelo por rol del runner por etapas. Vacío = cascada.
    for _rol, _key in _MODEL_ROLE_KEYS.items():
        if _key not in body:
            continue
        _spec = str(body[_key] or "").strip()
        _err = await _validate_model_role(db, _spec)
        if _err:
            return web.json_response({"error": f"{_rol}: {_err}"}, status=400)
        changed[_key] = _spec

    for k, v in changed.items():
        await db.set_config(k, v)
    # Refresca el snapshot en memoria: repos_root aplica al instante.
    relay_config.set_runtime_config(await db.all_config())

    bind_host = request.app.get(BIND_HOST_KEY) or "127.0.0.1"
    restart_required = ("RELAY_HOST" in changed
                        and changed["RELAY_HOST"] != bind_host)
    return web.json_response({
        "saved": changed,
        "restart_required": restart_required,
    })

async def api_config_timeouts(request: web.Request) -> web.Response:
    """GET /admin/api/config/timeouts — timeouts efectivos (expert + tool).

    Muestra el timeout efectivo del expert (resuelto en cascada),
    los defaults de cbm MCP y los envs relevantes. Solo lectura;
    para cambiarlos usar /admin/api/config/expert-timeout (PUT).
    Renombrado desde `api_config` (2026-07-09): se separó del
    `api_config_get` (whitelist editable: RELAY_HOST, FOURBIS_REPOS_ROOT)
    para no pisar la ruta /admin/api/config — antes convivían en el
    mismo path y ganaba este por orden de registro, lo que rompía
    `test_system_config.py::test_config_get_defaults`.
    """
    db = request.app[DB_KEY]
    overrides = {}
    try:
        all_cfg = await db.all_config()
        overrides = {k: v for k, v in (all_cfg or {}).items()
                     if not k.startswith(_SECRET_PREFIX)
                     and relay_config.PANEL_SETTINGS.get(k, {}).get("type")
                     != "secret"}
    except Exception:  # noqa: BLE001
        pass

    # Lee el valor resuelto (no el default suelto), pasando el override
    # que ya tengamos en system_config.
    eff_to = relay_config.expert_timeout_s()
    eff_tool_to = relay_config.tool_timeout_s()

    return web.json_response({
        "relay_version": RELAY_VERSION,
        "expert_timeout_s": eff_to,
        "expert_timeout_default_s": float(relay_config.PANEL_SETTINGS["expert_timeout_s"]["default"]),
        "tool_timeout_s": eff_tool_to,
        "tool_timeout_default_s": 60.0,
        "overrides": overrides,
    })

async def api_config_expert_timeout(request: web.Request) -> web.Response:
    """PUT /admin/api/config/expert-timeout — setea el timeout global.

    Body: {"value": 600} (segundos, float).
    Persiste en system_config (sobrevive reinicios). Permite null
    para volver al default.
    """
    db = request.app[DB_KEY]
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body debe ser un objeto JSON"}, status=400)
    raw = body.get("value", None)
    if raw is None or raw == "":
        await db.set_config("expert_timeout_s", "")
    else:
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return web.json_response(
                {"error": "value debe ser número o null"}, status=400,
            )
        if not math.isfinite(v) or v < 30 or v > 3600:
            return web.json_response(
                {"error": "rango permitido: 30..3600 segundos"}, status=400,
            )
        await db.set_config("expert_timeout_s", str(int(v)))
    await _refresh_runtime_config(db)
    eff = float((await db.get_config("expert_timeout_s"))
                or relay_config.expert_timeout_s())
    return web.json_response({"expert_timeout_s": eff})

async def api_config_tool_timeout(request: web.Request) -> web.Response:
    """PUT /admin/api/config/tool-timeout — setea el FOURBIS_MCP_TIMEOUT.

    Body: {"value": 60} (segundos, float).
    Persiste en system_config (sobrevive reinicios). Permite null
    para volver al env (o default 60s).

    Cascada efectiva (al usar el valor):
      1. system_config.FOURBIS_MCP_TIMEOUT (este endpoint)
      2. env var FOURBIS_MCP_TIMEOUT
      3. default 60s

    Rango: 5..600s. Sub-ola 2.6.
    """
    db = request.app[DB_KEY]
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body debe ser un objeto JSON"}, status=400)
    raw = body.get("value", None)
    if raw is None or raw == "":
        await db.set_config("FOURBIS_MCP_TIMEOUT", "")
    else:
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return web.json_response(
                {"error": "value debe ser número o null"}, status=400,
            )
        if not math.isfinite(v) or v < 5 or v > 600:
            return web.json_response(
                {"error": "rango permitido: 5..600 segundos"}, status=400,
            )
        await db.set_config("FOURBIS_MCP_TIMEOUT", str(int(v)))
    # refresca el snapshot en memoria para que el siguiente run lo vea
    await _refresh_runtime_config(db)
    eff = relay_config.tool_timeout_s()
    return web.json_response({"tool_timeout_s": eff})


def register_timeout_routes(app: web.Application) -> None:
    app.router.add_get("/admin/api/config/timeouts", api_config_timeouts)
    app.router.add_put("/admin/api/config/expert-timeout", api_config_expert_timeout)
    app.router.add_put("/admin/api/config/tool-timeout", api_config_tool_timeout)


def register_settings_routes(app: web.Application) -> None:
    app.router.add_get("/admin/api/config", api_config_get)
    app.router.add_put("/admin/api/config", api_config_put)
