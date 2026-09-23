"""Constantes y utilidades compartidas por los repositorios SQLite."""
from __future__ import annotations

import sqlite3
import time
from typing import Optional

from . import config

_PROJECT_COLS = (
    "slug", "name", "repo_path", "system_prompt", "mcp_servers",
    "defaults_json", "native_tools", "description", "enabled",
    "include_in_index", "night_mode_enabled", "night_config",
    "discord_channel_id",  # Iter 10.1: canal Discord default por proyecto
)
#: Cuánto del pedido se copia a `chats.user_prompt` (ver `create_chat`).
_CAP_PROMPT_FILA = 300

_COMMAND_COLS = ("name", "description", "handler", "args_schema", "enabled")
_MCP_COLS = (
    "name", "capability", "transport", "command", "args", "url", "env",
    "read_only", "on_demand", "idle_timeout_s", "enabled",
    "source_url", "source_commit", "install_dir",
    "vet_verdict", "vet_report", "health",)


#: Los únicos modelos con `vision` MEDIDA (2026-08-18, probando cada
#: endpoint con un PNG de un color liso y pidiendo el color). Reproducir
#: con `scripts/probe_vision.py`. El resto del catálogo entra por
#: `scripts/import_models.py` con vision=NULL, que es la verdad.
_MINIMAX_URL = "https://api.minimax.io/v1"
_NVIDIA_URL = "https://integrate.api.nvidia.com/v1"

#: Los únicos modelos con `vision` MEDIDA (2026-08-18, mandando a cada
#: endpoint un PNG de un color liso y pidiendo el color, con dos colores
#: distintos para que acertar de casualidad no cuente). Reproducir con
#: `scripts/probe_vision.py`. El resto del catálogo entra por
#: `scripts/import_models.py` con vision=NULL, que es la verdad.
#:
#: Dicts y no tuplas: la primera versión eran tuplas posicionales y
#: agregar `base_url` corrió todos los índices de los tests. Un seed que
#: se rompe al sumar una columna es un seed mal escrito.
_MODELS_SEED = [
    {"spec": "minimax:MiniMax-M3", "label": "MiniMax M3",
     "provider": "minimax", "base_url": _MINIMAX_URL,
     "api_key_env": "MINIMAX_API_KEY", "vision": 1, "enabled": 1,
     "cost_in": 0.3, "cost_out": 1.2, "verified_at": "2026-08-18",
     # `cost_cache_in` queda en NULL A PROPÓSITO: la tarifa de cache read
     # de MiniMax no está verificada acá, y un número inventado en la
     # columna de la plata es peor que no tenerlo (se cobra a `cost_in`
     # hasta que alguien pegue el valor del tarifario).
     "cost_cache_in": None,
     "context_tokens": 200_000, "context_warn_tokens": 92_000,
     "notes": "el default, pago; ve imágenes"},
    {"spec": "nvidia:minimaxai/minimax-m3",
     "label": "MiniMax M3 · NVIDIA (respaldo)",
     "provider": "nvidia", "base_url": _NVIDIA_URL,
     "api_key_env": "NVIDIA_API_KEY", "vision": 1, "enabled": 1,
     "cost_in": 0.0, "cost_out": 0.0, "verified_at": "2026-08-18",
     "cost_cache_in": 0.0,
     # Mismos pesos que el de arriba: misma ventana y mismo punto de
     # degradación medido.
     "context_tokens": 200_000, "context_warn_tokens": 92_000,
     "notes": "respaldo: ve imágenes pero gasta la cuota gratis de NVIDIA "
              "en un modelo que ya pagamos"},
    {"spec": "nvidia:z-ai/glm-5.2", "label": "GLM 5.2 · NVIDIA",
     "provider": "nvidia", "base_url": _NVIDIA_URL,
     "api_key_env": "NVIDIA_API_KEY", "vision": 0, "enabled": 1,
     "cost_in": 0.0, "cost_out": 0.0, "verified_at": "2026-08-18",
     "notes": "gratis; NO ve imágenes y encima no da error: contesta igual"},
    {"spec": "nvidia:nvidia/nemotron-3-ultra-550b-a55b",
     "label": "Nemotron 3 Ultra · NVIDIA",
     "provider": "nvidia", "base_url": _NVIDIA_URL,
     "api_key_env": "NVIDIA_API_KEY", "vision": 0, "enabled": 1,
     "cost_in": 0.0, "cost_out": 0.0, "verified_at": "2026-08-18",
     "notes": "gratis; solo texto (corta con 400 si mandás una imagen)"},
]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _vence_en(segundos: int) -> str:
    """`now_iso()` corrido N segundos. Mismo formato fijo y en UTC, así
    que comparar con `<` como texto es comparar fechas."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() + segundos))


#: Clave de `system_config` con la tarifa por modelo. JSON:
#:   {"minimax:MiniMax-M3": {"in": 0.3, "out": 1.2},
#:    "nvidia:*":           {"in": 0, "out": 0,
#:                           "ref_in": 0.6, "ref_out": 2.4}}
#: `in`/`out` son USD por MILLÓN de tokens y es lo que se paga.
#: `ref_*` es el precio de mercado del mismo modelo en un proveedor
#: pagado, y solo se usa para estimar cuánto ahorra el free tier; sin
#: `ref_*` el ahorro de esa entrada es 0 (no se inventa una tarifa).
MODEL_PRICES_KEY = "MODEL_PRICES"


def match_model_price(prices: dict, spec: str) -> Optional[dict]:
    """Tarifa de `spec`: exacta > patrón `prefijo*` (el más largo) > None.

    None significa "sin tarifa cargada", que NO es lo mismo que gratis:
    el costo de esa fila queda en null y la UI lo muestra como sin dato.
    Un modelo nuevo sin precio no puede aparecer como si saliera 0.
    """
    if not spec or not isinstance(prices, dict):
        return None
    exact = prices.get(spec)
    if isinstance(exact, dict):
        return exact
    best: Optional[dict] = None
    best_len = -1
    for pat, val in prices.items():
        if not isinstance(val, dict) or not pat.endswith("*"):
            continue
        head = pat[:-1]
        if spec.startswith(head) and len(head) > best_len:
            best, best_len = val, len(head)
    return best


def cost_usd(price: Optional[dict], tokens_in, tokens_out,
             *, reference: bool = False, cache_read=None) -> Optional[float]:
    """Costo en USD de un turno, o None si no hay con qué calcularlo.

    `reference=True` usa `ref_in`/`ref_out` (lo que habría costado en un
    proveedor pagado) en vez de la tarifa real; es la base del "ahorro".

    `cache_read` (2026-08-31) son los tokens de `tokens_in` que el
    provider sirvió de su caché. Se cobran a `cache_in` si la tarifa
    está cargada; si NO está, se cobran a `in` como antes — caro de más,
    nunca de menos. No aplica al modo `reference`: el ahorro del free
    tier se estima contra el precio de lista, que no tiene caché.

    Devuelve None cuando falta la tarifa O faltan los tokens: sumar un
    cero silencioso ahí haría que el total parezca completo cuando no lo
    está.
    """
    if not price or tokens_in is None or tokens_out is None:
        return None
    ki, ko = ("ref_in", "ref_out") if reference else ("in", "out")
    if ki not in price or ko not in price:
        return None
    try:
        entrada = float(tokens_in)
        salida = float(tokens_out)
        rate_in = float(price[ki])
        # max(0,…) porque `cache_read` viene del provider: si alguna vez
        # reporta más caché que entrada, el costo no puede irse a negativo.
        cacheados = 0.0
        if not reference and cache_read is not None:
            cacheados = min(max(float(cache_read), 0.0), entrada)
        rate_cache = price.get("cache_in")
        rate_cache = rate_in if rate_cache is None else float(rate_cache)
        return ((entrada - cacheados) * rate_in
                + cacheados * rate_cache
                + salida * float(price[ko])) / 1_000_000
    except (TypeError, ValueError):
        return None


def prices_from_models(rows: list) -> dict:
    """Tarifario con la forma de `MODEL_PRICES` armado desde `models`.

    2026-08-31: la pantalla Modelos guarda `cost_in`/`cost_out` por spec
    y el dashboard leía SOLO la clave `MODEL_PRICES` de system_config —
    que estaba vacía. Resultado: `priced=false` y todos los costos en
    null aunque la tarifa estuviera cargada en la tabla de al lado. Dos
    fuentes de verdad y la UI leía la vacía.

    `MODEL_PRICES` sigue ganando (ver `metrics_summary`): es el lugar
    donde se escriben los patrones `nvidia:*` y los `ref_*`, que la
    tabla no tiene. Una fila sin las DOS tarifas no entra: media tarifa
    no es una tarifa.
    """
    out: dict = {}
    for row in rows or []:
        r = dict(row)
        spec = r.get("spec")
        if not spec or r.get("cost_in") is None or r.get("cost_out") is None:
            continue
        precio = {"in": r["cost_in"], "out": r["cost_out"]}
        if r.get("cost_cache_in") is not None:
            precio["cache_in"] = r["cost_cache_in"]
        out[spec] = precio
    return out


def read_system_config_sync(key: str, default: str = "") -> str:
    """Lectura sync de system_config para ANTES de arrancar el loop.

    `main()` necesita RELAY_HOST para el bind de aiohttp, que ocurre
    antes de que exista la app (y su Database async). Tolera DB o
    tabla inexistentes: primer arranque devuelve el default.
    """
    path = config.db_path()
    try:
        conn = sqlite3.connect(path, timeout=5)
        try:
            cur = conn.execute(
                "SELECT value FROM system_config WHERE key=?", (key,))
            row = cur.fetchone()
            return row[0] if row else default
        finally:
            conn.close()
    except sqlite3.Error:
        return default
