"""Sonda y arranque del bot de Discord (RelayDemoBot).

Por qué existe: el `/health` del bot tiene `Predicate = _ => false`, o sea
devuelve 200 mientras el PROCESO viva, sin correr ningún check. El gateway
de Discord puede estar caído y el health sigue en verde. Ese es el gotcha
que dejaba vínculos fantasma: la UI creía que el bot estaba vivo, el
`POST /threads` devolvía 503 `bot_discord_no_conectado`, y el DM nunca
llegaba.

La sonda correcta es `GET /api/Bot/status` → `{running: bool}`, que lee
`DiscordBotService.IsRunning` — el estado real del gateway. Tres estados,
que son tres fallas distintas y se arreglan distinto:

    proceso  gateway   qué pasó                        cómo se arregla
    ───────  ───────   ─────────────────────────────   ───────────────────
    down     down      el .exe no está corriendo       spawn del proceso
    up       down      corre sin token / se cayó       POST /api/Bot/start
    up       up        todo bien                       nada

El token de Discord vive en `appsettings.Development.json`, así que el
spawn TIENE que ir con `ASPNETCORE_ENVIRONMENT=Development`: sin eso el
proceso levanta, sirve health 200 y nunca conecta.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import httpx

from . import config as relay_config

logger = logging.getLogger("relay.bot_control")

# Sonda: corta rápido. Si el bot no contesta en 2s, para la UI está caído.
PROBE_TIMEOUT = 2.0
# Techo del arranque. La UI llama con un timeout de fetch mayor que esto.
START_TIMEOUT = float(os.environ.get("FOURBIS_BOT_START_TIMEOUT", "25"))


def bot_base_url() -> str:
    """Base HTTP del bot, derivada de BOT_NOTIFY_URL.

    Kestrel escucha /notify, /threads y /api/Bot/* en TODOS sus puertos
    (5044 y 8297), así que el puerto de notify sirve para sondear.
    """
    raw = os.environ.get("BOT_NOTIFY_URL", "http://127.0.0.1:8297/notify")
    base = raw.rstrip("/")
    if base.endswith("/notify"):
        base = base[: -len("/notify")]
    return base


def bot_exe_path() -> Optional[Path]:
    """Ruta al .exe del bot. `FOURBIS_BOT_EXE` gana; si no, convención.

    Devuelve None si no existe: el caller lo reporta como error accionable
    en vez de spawnear algo que no está.
    """
    explicit = (os.environ.get("FOURBIS_BOT_EXE", "")
                or relay_config.runtime_get("FOURBIS_BOT_EXE", "")).strip()
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    guess = (Path(relay_config.repos_root()) / "RelayDemoBot" / "bin"
             / "Debug" / "net10.0" / "RelayDemoBot.exe")
    return guess if guess.is_file() else None


async def probe(*, timeout: float = PROBE_TIMEOUT) -> dict:
    """Estado real del bot: {process, gateway, url, detail}.

    `process` y `gateway` son "up" | "down". Nunca levanta: un bot caído
    es un estado, no un error del relay.
    """
    url = bot_base_url()
    out = {"process": "down", "gateway": "down", "url": url, "detail": ""}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{url}/api/Bot/status")
    except httpx.HTTPError as e:
        out["detail"] = f"el bot no responde en {url} ({type(e).__name__})"
        return out
    out["process"] = "up"
    if r.status_code != 200:
        out["detail"] = f"/api/Bot/status devolvió {r.status_code}"
        return out
    try:
        running = bool(r.json().get("running"))
    except (ValueError, AttributeError):
        out["detail"] = "/api/Bot/status devolvió un body que no es JSON"
        return out
    if running:
        out["gateway"] = "up"
    else:
        out["detail"] = ("el proceso corre pero el gateway de Discord no "
                         "está conectado (¿falta el token de "
                         "appsettings.Development.json?)")
    return out


def _spawn(exe: Path) -> None:
    """Lanza el .exe desacoplado del relay.

    DETACHED_PROCESS: el bot tiene que sobrevivir a un reinicio del relay.
    stdio a DEVNULL para no dejar handles colgados del padre.
    """
    env = dict(os.environ)
    # Sin esto el proceso levanta, sirve health 200 y nunca entra a Discord:
    # el token está en appsettings.Development.json.
    env.setdefault("ASPNETCORE_ENVIRONMENT", "Development")
    flags = 0
    if sys.platform == "win32":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen(
        [str(exe)], cwd=str(exe.parent), env=env, creationflags=flags,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True)


async def _wait_gateway(deadline: float) -> dict:
    """Sondea hasta que el gateway esté arriba o se acabe el tiempo."""
    st = await probe()
    while st["gateway"] != "up" and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(1.0)
        st = await probe()
    return st


async def start(*, timeout: float = START_TIMEOUT) -> dict:
    """Deja el bot conectado a Discord. Devuelve {ok, action, ...probe}.

    `action` dice qué hizo falta: "none" (ya estaba), "gateway" (el proceso
    vivía y solo faltaba conectar) o "spawn" (hubo que levantar el .exe).
    """
    st = await probe()
    if st["gateway"] == "up":
        return {**st, "ok": True, "action": "none"}

    deadline = asyncio.get_event_loop().time() + timeout

    if st["process"] == "up":
        # Barato: el proceso ya está, solo hay que conectar el gateway.
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(f"{bot_base_url()}/api/Bot/start")
            if r.status_code >= 400:
                st["detail"] = (f"POST /api/Bot/start devolvió "
                                f"{r.status_code}: {r.text[:200]}")
                return {**st, "ok": False, "action": "gateway"}
        except httpx.HTTPError as e:
            st["detail"] = f"no se pudo pedir el arranque del gateway: {e}"
            return {**st, "ok": False, "action": "gateway"}
        st = await _wait_gateway(deadline)
        return {**st, "ok": st["gateway"] == "up", "action": "gateway"}

    exe = bot_exe_path()
    if exe is None:
        return {
            **st, "ok": False, "action": "spawn",
            "detail": ("no encuentro el .exe del bot. Compilalo "
                       "(`dotnet build`) o apuntá FOURBIS_BOT_EXE al "
                       "binario."),
        }
    try:
        _spawn(exe)
    except OSError as e:
        return {**st, "ok": False, "action": "spawn",
                "detail": f"no se pudo lanzar {exe}: {e}"}
    logger.info("bot lanzado desde %s, esperando gateway", exe)
    st = await _wait_gateway(deadline)
    if st["gateway"] != "up" and not st["detail"]:
        st["detail"] = f"el bot no conectó en {timeout:.0f}s"
    return {**st, "ok": st["gateway"] == "up", "action": "spawn"}
