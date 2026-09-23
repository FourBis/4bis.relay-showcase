"""shell: funciones del flujo shell."""
from __future__ import annotations
import asyncio
import contextlib
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional
from . import shell_environment, shell_process, shell_syntax

logger = logging.getLogger("relay.shell")

async def run(
    cmd: str, *, cwd: Optional[str] = None, timeout: float = 300.0,
    shell_kind: str = "auto", env_extra: Optional[dict] = None,
    base: Optional[str] = None,
) -> dict:
    """Ejecuta `cmd` y devuelve `{exit, out, timed_out, duration_ms, shell}`.

    Nunca lanza por culpa del comando: un comando que falla es un
    resultado con `exit != 0`, no una excepción. Lo que sí puede lanzar
    es un problema al CREAR el proceso (intérprete inexistente), y eso
    también vuelve como resultado con `exit=-1`.

    `base` es la raíz del repo: contra ella se resuelve un `cwd`
    relativo y se lo contiene (ver `_resolver_cwd`). Sin `base` el
    comportamiento es el de antes.
    """
    argv, kind = shell_syntax.build_argv(cmd, shell_kind=shell_kind)
    t0 = time.monotonic()
    cwd, err = shell_environment._resolver_cwd(cwd, base)
    if err:
        return {"exit": -1, "out": err, "timed_out": False,
                "duration_ms": 0, "shell": kind}
    if cwd and not os.path.isdir(cwd):
        # Sin este chequeo, `CreateProcess` tira WinError 267 crudo
        # ("El nombre de directorio no es válido") sin decir CUÁL
        # directorio ni por qué — el modelo no puede autocorregirse con
        # eso. Un mensaje que nombra la ruta sí se lo permite.
        return {"exit": -1, "out": f"cwd no existe: {cwd!r}",
                "timed_out": False, "duration_ms": 0, "shell": kind}
    # start_new_session en POSIX: le da al hijo su propio grupo de
    # procesos para poder matarlo entero con killpg.
    kwargs: dict = {}
    if not shell_environment.IS_WINDOWS:
        kwargs["start_new_session"] = True
    spawn_kw = dict(
        stdin=subprocess.DEVNULL,           # ← la causa #1 de cuelgues
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,           # una sola cronología
        cwd=cwd or None,
        env=shell_environment._env_for_run(env_extra, kind=kind),
        **kwargs,
    )
    try:
        if shell_environment.IS_WINDOWS and kind == "cmd":
            # `create_subprocess_exec` pasa la lista por
            # `subprocess.list2cmdline`, que escapa cada `"` como `\"`:
            # la convención del CRT de Microsoft, la que entienden
            # `bash.exe` y `pwsh.exe`. `cmd.exe` es el único de los tres
            # que NO la usa —para él la `\` es literal—, así que por esta
            # rama la línea le llega tal cual (`create_subprocess_shell`
            # delega en `Popen(shell=True)`, que en Windows arma
            # `comspec /c "<línea>"` sin pasar por `list2cmdline`).
            proc = await asyncio.create_subprocess_shell(argv[-1], **spawn_kw)
        else:
            proc = await asyncio.create_subprocess_exec(*argv, **spawn_kw)
    except (OSError, ValueError) as e:
        return {"exit": -1, "out": f"no pude ejecutar {argv[0]!r}: {e}",
                "timed_out": False,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "shell": kind}

    buf = bytearray()
    drenaje = asyncio.create_task(shell_process._drain(proc.stdout, buf, shell_environment.MAX_CAPTURE_BYTES))
    timed_out = False
    huerfano = False
    try:
        huerfano = await asyncio.wait_for(
            shell_process._esperar_al_hijo(proc), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        shell_process._kill_tree(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.CancelledError:
        # Nos cancelan DESDE AFUERA (el corte por tool-call, el watchdog,
        # un /cancel). Sin esta rama el árbol de procesos sobrevive al
        # run: `anyio.fail_after` del toolset cancela esta corrutina, el
        # `except TimeoutError` de arriba no aplica, y el hijo queda
        # huérfano. Verificado el 19/8/2026: 13 procesos `node` de
        # Playwright seguían vivos horas después del run que los lanzó,
        # tomando puertos y perfiles de Chromium que hacían fallar a los
        # intentos siguientes.
        shell_process._kill_tree(proc)
        raise
    finally:
        # El drenaje termina solo cuando el proceso cierra el pipe; si lo
        # matamos, puede quedar colgado: se le da un margen y se corta.
        # Con `huerfano` sabemos que el pipe está en manos de un nieto y
        # que no va a cerrar nunca: ese margen ya se pagó como
        # `GRACIA_PIPES_S`, y esperarlo de nuevo son 10s regalados por
        # comando (medido: 12,5s para un `Start-Process` que vuelve al
        # prompt en menos de uno).
        try:
            await asyncio.wait_for(drenaje, timeout=0.1 if huerfano else 10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            drenaje.cancel()
        if huerfano:
            # Nuestra punta del pipe no sirve más y el proceso ya murió,
            # así que `close()` no mata nada: solo suelta el handle. Sin
            # esto queda un transport vivo por cada comando de estos.
            with contextlib.suppress(Exception):
                proc._transport.close()  # noqa: SLF001

    out = shell_environment._decodificar(buf)
    if len(buf) >= shell_environment.MAX_CAPTURE_BYTES:
        out += (f"\n…[salida recortada a {shell_environment.MAX_CAPTURE_BYTES} bytes en "
                "memoria; el proceso siguió escribiendo]")
    dur = int((time.monotonic() - t0) * 1000)
    # `proc.returncode` sigue en None si salimos por la gracia de pipes
    # (asyncio no cosechó); el `Popen` crudo sí tiene el código real.
    _crudo = getattr(proc, "_transport", None)
    _crudo = _crudo.get_extra_info("subprocess") if _crudo is not None else None
    _rc = proc.returncode
    if _rc is None and _crudo is not None:
        _rc = _crudo.returncode
    code = -9 if timed_out else (_rc if _rc is not None else -1)
    if timed_out:
        out += (
            f"\n\n[TIMEOUT] El comando no terminó en {timeout:.0f}s y se mató "
            "el árbol de procesos. Si es un server o algo que no retorna "
            "solo (`npm run dev`, `dotnet run`, `docker compose up` sin "
            "`-d`), volvé a lanzarlo con `background=True`: vuelve enseguida "
            "y el proceso queda vivo. Si de verdad tarda más, decilo en la "
            "respuesta para que el humano suba el techo.")
    # El exit code al FINAL y en su propia línea: es lo que sobrevive a
    # la elisión del historial (`_elide_tail` se queda con la última
    # línea corta) y lo que el experto necesita para saber si funcionó.
    out = f"{out.rstrip()}\n(exit={code})"
    return {"exit": code, "out": out, "timed_out": timed_out,
            "duration_ms": dur, "shell": kind}



#: Dónde van los logs de los procesos largados en background.
BG_LOG_DIR = Path(os.environ.get(
    "FOURBIS_SHELL_BG_DIR", str(Path.home() / ".4bis" / "bg")))


async def lanzar(
    cmd: str, *, cwd: Optional[str] = None, shell_kind: str = "auto",
    env_extra: Optional[dict] = None, base: Optional[str] = None,
) -> dict:
    """Larga `cmd` y **no lo espera**. Devuelve `{pid, log, shell, out}`.

    La capacidad que faltaba. `run()` siempre espera a que el proceso
    termine y le mata el árbol al vencer el timeout, así que un dev
    server era imposible de dejar arriba: el docstring de la tool `shell`
    pedía "lanzalos en background" y no había con qué.

    Medido el 24/8 en sample-app: el ejecutor levantó Vite en foreground, la
    tool cortó a los 180s y el `_kill_tree` se llevó el server puesto. El
    modelo lo entendió perfecto —"it was killed when the timeout
    happened. Let me start it in the background this time"— y su segundo
    intento colgó igual, porque no existía la forma. Sin server no hay
    capturas, así que el pedido del manual no podía terminar nunca.

    Dos decisiones que hacen que esto sirva de verdad:

    - **La salida va a un archivo, no a un pipe.** Un server que no
      arranca hay que poder leerlo; y un pipe sin nadie drenando se
      llena y bloquea al proceso, que es el cuelgue #4 de arriba.
    - **El proceso tiene su propio grupo** (`CREATE_NEW_PROCESS_GROUP`
      en Windows, `start_new_session` en POSIX). El cierre de esta llamada
      no lo espera ni lo cancela; un kill externo del árbol puede alcanzarlo.

    No hay `timeout`: el que larga esto se hace cargo de bajarlo. Por eso
    devuelve el pid.

    `base`: mismo trato que en `run()` — un `cwd` relativo se resuelve
    contra la raíz del repo y no puede salirse de ella (`_resolver_cwd`).
    """
    argv, kind = shell_syntax.build_argv(cmd, shell_kind=shell_kind)
    cwd, err = shell_environment._resolver_cwd(cwd, base)
    if err:
        return {"pid": 0, "exit": -1, "log": "", "shell": kind, "out": f"{err}\n(exit=-1)"}
    BG_LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Nombre por comando + reloj: dos `npm run dev` seguidos no se pisan
    # el log, y el humano puede saber cuál es cuál sin abrirlos.
    slug = re.sub(r"[^a-z0-9]+", "-", cmd.lower())[:40].strip("-") or "cmd"
    # Creación exclusiva: dos comandos iguales en el mismo segundo no
    # deben truncar el log que el primero todavía está escribiendo.
    fd, log_name = tempfile.mkstemp(
        prefix=f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}-", suffix=".log", dir=BG_LOG_DIR)
    os.close(fd)
    log = Path(log_name)

    kwargs: dict = {}
    if shell_environment.IS_WINDOWS:
        # Solo NEW_PROCESS_GROUP: aísla al hijo de nuestro Ctrl+C y le da
        # su propio grupo, que es lo que se necesita.
        #
        # NO va `DETACHED_PROCESS`, aunque suene a lo correcto: medido el
        # 24/8, con ese flag el proceso arranca y vive pero su salida
        # NUNCA llega al archivo (0 bytes contra 20 sin el flag, mismo
        # comando). Un server en background cuyo log queda vacío es peor
        # que no tenerlo: no hay forma de saber si levantó, y ese log es
        # todo lo que queda para diagnosticarlo.
        kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    def _abrir():
        # `subprocess.Popen` y NO `asyncio.create_subprocess_exec`: el
        # transport de asyncio hace `self._proc.kill()` al cerrarse
        # (`BaseSubprocessTransport.close`), así que el hijo se muere
        # junto con el event loop. Medido el 24/8: el server quedaba vivo
        # mientras el padre corría y desaparecía en cuanto el padre
        # terminaba — o sea, no sobrevivía a un reinicio del relay, que es
        # justo lo único que este modo tiene que garantizar. `Popen` al
        # recolectarse solo avisa con un ResourceWarning; no mata.
        fh = open(log, "wb")
        try:
            return subprocess.Popen(
                argv[-1] if shell_environment.IS_WINDOWS and kind == "cmd" else argv,
                shell=shell_environment.IS_WINDOWS and kind == "cmd",
                stdin=subprocess.DEVNULL, stdout=fh,
                stderr=subprocess.STDOUT, cwd=cwd or None,
                env=shell_environment._env_for_run(env_extra, kind=kind), **kwargs)
        finally:
            # El hijo ya tiene su copia del handle; la nuestra no sirve.
            fh.close()

    starting = asyncio.create_task(asyncio.to_thread(_abrir))
    try:
        proc = await asyncio.shield(starting)
        # Distingue un fallo inmediato de un proceso vivo; aún no acredita readiness.
        await asyncio.sleep(1.5)
    except asyncio.CancelledError:
        # El worker no se cancela con asyncio: recuperar su PID antes de
        # devolver el control. Si no se entregó al caller, debemos limpiarlo.
        try:
            proc = await starting
            await asyncio.to_thread(shell_process._kill_tree, proc)
            await asyncio.to_thread(proc.wait, timeout=10)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            logger.exception("no pude limpiar el lanzamiento background cancelado")
        raise
    except (OSError, ValueError) as e:
        return {"pid": 0, "exit": -1, "log": str(log), "shell": kind,
                "out": f"no pude lanzar {argv[0]!r}: {e}\n(exit=-1)"}

    murio = proc.poll() is not None
    cola = ""
    with contextlib.suppress(Exception):
        cola = log.read_text(encoding="utf-8", errors="replace")[-1500:]

    if murio:
        return {"pid": proc.pid, "exit": proc.returncode, "log": str(log), "shell": kind, "out": (
            f"el proceso murió al instante (exit={proc.returncode}).\n"
            f"{cola}\n(exit={proc.returncode})")}
    return {"pid": proc.pid, "exit": None, "log": str(log), "shell": kind, "out": (
        f"lanzado en background: pid={proc.pid}\n"
        f"log: {log}\n"
        f"{('primeras líneas:' + chr(10) + cola) if cola.strip() else ''}\n"
        f"Sigue vivo cuando esta tool devuelve. Para ver cómo va, leé el "
        f"log y comprobá el puerto o endpoint: el PID no demuestra que "
        f"el servicio esté listo. Para bajarlo, matá el árbol del pid.\n"
        f"(background activo; disponibilidad sin verificar)")}


def python_exe() -> str:
    """El python del venv del relay. Para que el experto no adivine."""
    return sys.executable


from .shell_environment import (
    IS_WINDOWS,
    MAX_CAPTURE_BYTES,
    _MISSING_PATTERNS,
    _MISSING_RE,
    _NONINTERACTIVE_ENV,
    _POSIX_DRIVE_RE,
    _QUE_FALTA_RE,
    _codepage_consola,
    _cwd_para_windows,
    _decodificar,
    _env_for_run,
    _parece_ruta,
    _resolver_cwd,
    bash_exe,
    missing_tool_hint,
)
from .shell_syntax import (
    _ALIAS_RE,
    _ALIAS_UNIX,
    _CITADO_RE,
    _CMD_BAT_RE,
    _CMD_BUILTIN_RE,
    _CMD_RUTA_WIN_RE,
    _CMD_VAR_RE,
    _CMD_WIN_FLAG_RE,
    _POSIX_SYNTAX_RE,
    _PS_ENVOLTORIO_RE,
    _PS_FLAGS_CON_VALOR,
    _PS_FLAGS_SOLOS,
    _PS_OTRAS,
    _PS_RE,
    _PS_VERBOS,
    _RAICES_UNIX,
    _RUTA_WIN_EN_CMD_RE,
    _SH_RE,
    _SINGLE_QUOTE_RE,
    _SOLO_UNIX,
    _looks_like_alias_unix,
    _looks_like_cmd,
    _looks_like_posix_syntax,
    _looks_like_powershell,
    _looks_like_sh,
    _looks_like_single_quoted,
    _payload_entre_comillas,
    _rutas_para_bash,
    _tiene_punto_y_coma,
    build_argv,
    desenvolver_powershell,
)
from .shell_process import (
    GRACIA_PIPES_S,
    _drain,
    _esperar_al_hijo,
    _kill_tree,
)
