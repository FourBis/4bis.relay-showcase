"""shell process: funciones del flujo shell."""
from __future__ import annotations
import asyncio
import contextlib
import logging
import os
import signal
import subprocess
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
#: pero los pipes siguen abiertos (ver `_esperar_al_hijo`).
GRACIA_PIPES_S = 2.0


async def _esperar_al_hijo(proc: asyncio.subprocess.Process) -> bool:
    """Vuelve cuando terminó el hijo **directo**. `True` si dejó pipes.

    `proc.wait()` a secas no alcanza: el transport de asyncio resuelve
    ese future cuando el proceso terminó Y ADEMÁS se cerraron los pipes
    (`BaseSubprocessTransport._try_finish`). Si el comando lanzó algo que
    heredó el stdout —`Start-Process` con redirección, un `start`, un
    server que se queda— el pipe no cierra nunca y esperamos al proceso
    equivocado.

    En el caso normal gana `proc.wait()` y esto no cambia nada: los pipes
    cierran junto con el proceso. La rama de abajo solo corre cuando el
    `Popen` crudo ya tiene returncode y el future sigue pendiente; ahí se
    le da `GRACIA_PIPES_S` a lo que quede en vuelo y se sale.

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
        if crudo is None:           # sin acceso al Popen: comportamiento viejo
            await espera
            return False
        while True:
            listo, _ = await asyncio.wait({espera}, timeout=0.1)
            if listo:
                return False
            if crudo.poll() is not None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(espera),
                                           timeout=GRACIA_PIPES_S)
                return not espera.done()
    finally:
        # `shield` deja el future vivo si nos vamos por la gracia, y en
        # un cancel de afuera hay que soltarlo igual: sin esto queda un
        # "Task was destroyed but it is pending" por cada comando.
        if not espera.done():
            espera.cancel()


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
