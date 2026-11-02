"""shell process: funciones del flujo shell."""
from __future__ import annotations
import asyncio
import logging
import os
import signal
import subprocess
from contextlib import suppress
from . import shell_environment

logger = logging.getLogger("relay.shell")

async def _drain(stream, buf: bytearray, limit: int) -> None:
    """Lee un stream hasta EOF acumulando hasta `limit` bytes.

    Leer MIENTRAS el proceso corre es lo que evita el deadlock del pipe
    lleno. Pasado el límite se sigue leyendo pero se descarta: si dejamos
    de leer, el proceso se vuelve a trabar — que es justo lo que estamos
    arreglando.
    """
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        if len(buf) < limit:
            buf.extend(chunk[:limit - len(buf)])


#: Cuánto se le da a la salida en vuelo cuando el hijo directo ya murió
#: pero los pipes siguen abiertos.
GRACIA_PIPES_S = 2.0


async def _esperar_al_hijo(proc: asyncio.subprocess.Process) -> None:
    """Vuelve cuando terminó el hijo **directo**, aunque deje pipes.

    En Python 3.12 `proc.wait()` puede esperar también el cierre de los
    pipes; en 3.15 termina al salir el proceso. Por eso comprobamos por
    separado la salida del hijo y el drenaje: un nieto puede heredar
    stdout y mantenerlo abierto después de que termine el hijo directo.
    El caller limita por separado el drenaje de la salida en vuelo.

    Caso real (code-hero-rpg, 2026-08-31): el ejecutor levantó el dev
    server con `Start-Process npm.cmd run dev -RedirectStandardOutput`.
    PowerShell imprimió y salió en <1s; el pipe quedó en manos de `node`,
    la tool esperó su techo entero y el `_kill_tree` del timeout se
    llevó puesto el server. Los pasos que seguían eran "levantar
    Playwright" y "verificar en el browser": sin server no había forma de
    terminar el plan.
    """
    crudo = getattr(proc, "_transport", None)
    crudo = crudo.get_extra_info("subprocess") if crudo is not None else None
    espera = asyncio.ensure_future(proc.wait())
    try:
        if crudo is None:
            await espera
        else:
            while True:
                listo, _ = await asyncio.wait({espera}, timeout=0.1)
                if listo or crudo.poll() is not None:
                    break
    finally:
        # En versiones que esperan los pipes, soltar también la espera.
        if not espera.done():
            espera.cancel()


async def _kill_tree_async(proc) -> None:
    """taskkill puede tardar: esperar su cierre sin bloquear el event loop."""
    worker = asyncio.create_task(asyncio.to_thread(_kill_tree, proc))
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(worker)
        with suppress(asyncio.CancelledError, Exception):
            worker.result()
        raise


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Mata el proceso Y sus hijos. Best-effort.

    Matar solo al padre deja al hijo (el `npm` que lanzó el `node`, el
    `dotnet` que lanzó el `MSBuild`) vivo y escribiendo: el pipe sigue
    abierto y el cuelgue continúa.
    """
    if proc.returncode is not None:
        return
    try:
        if shell_environment.IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, check=False, timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception as e:  # noqa: BLE001 — matar es best-effort
        logger.warning("no pude matar el árbol de %s (%r)", proc.pid, e)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
