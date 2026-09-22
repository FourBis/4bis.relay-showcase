"""Composición de configuración y catálogo de modelos del panel Admin."""
from __future__ import annotations

from aiohttp import web

from . import admin_config_models, admin_config_settings
from .app_state import BIND_HOST_KEY, DB_KEY  # noqa: F401
from .experts import ModelUnavailable, build_model  # noqa: F401
from .admin_config_models import (  # noqa: F401
    _MODEL_ROLE_KEYS, _roles_que_usan, _validate_model_role,
    api_models, api_model_upsert, api_model_test, api_model_delete,
    _refrescar_catalogo, _TEST_MAX_TOKENS, _TEST_TIMEOUT_S,
)
from .admin_config_settings import (  # noqa: F401
    _refresh_runtime_config, _public_panel_settings, _number_is_integral,
    _validate_panel_value, _validate_model_prices, api_config_get,
    api_config_put, api_config_timeouts, api_config_expert_timeout,
    api_config_tool_timeout, _CONFIG_DEFAULTS, _VALID_RELAY_HOSTS,
    _EDITABLE_CONFIG_KEYS, _SECRET_NAME_RE, _SECRET_PREFIX,
)

# Compatibilidad para callers internos y tests que importaban admin_config.


def register_model_routes(app: web.Application) -> None:
    admin_config_models.register_model_routes(app)


def register_timeout_routes(app: web.Application) -> None:
    admin_config_settings.register_timeout_routes(app)


def register_settings_routes(app: web.Application) -> None:
    admin_config_settings.register_settings_routes(app)
