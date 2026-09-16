"""Smoke contra un relay VIVO, sin browser.

Vivía dentro de `test_admin_ui_obscura.py`, que se borró el 2026-08-16
al retirar obscura del catálogo (ver docs/BROWSER_UNICO.md). El único
test de ese archivo que no dependía del binario era este, así que se
rescata acá en vez de perderse con el resto.

Diferencia con el original: si el relay NO está levantado, esto hace
**skip** en vez de fallar. El original fallaba, y como en la práctica
nadie corre la suite con el relay arriba, era un rojo permanente que
enseñaba a ignorar los rojos.

    RELAY_BASE_URL=http://127.0.0.1:8413 pytest tests/test_relay_api_smoke.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request

import pytest

RELAY_BASE = os.environ.get("RELAY_BASE_URL", "http://127.0.0.1:8413")


async def relay_api(path: str, body: dict | None = None,
                    timeout: float = 15.0) -> dict:
    """GET/POST a la API del relay, desde el proceso de test.

    urllib y no aiohttp: contra este relay, un ClientSession de aiohttp
    con keep-alive (el default) se cuelga hasta el timeout, mientras que
    urllib responde en ~40ms y curl en ~66ms. Con
    `headers={'Connection': 'close'}` aiohttp también anda, pero la
    stdlib no necesita el workaround. Verificado 2026-07-21.
    """
    url = f"{RELAY_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET")

    def _do() -> dict:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return {"error": f"status:{e.code}"}

    return await asyncio.to_thread(_do)


async def test_health_endpoint_responds_2xx():
    try:
        body = await relay_api("/admin/api/health", timeout=3.0)
    except (urllib.error.URLError, OSError) as e:
        pytest.skip(f"relay no levantado en {RELAY_BASE} ({e})")
    assert "error" not in body, f"health falló: {body!r}"
    assert body.get("ok"), f"health sin ok=true: {body!r}"
