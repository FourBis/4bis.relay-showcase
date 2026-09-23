"""shell environment: funciones del flujo shell."""
from __future__ import annotations
import codecs
import functools
import os
import re
import shutil
from typing import Optional

_NONINTERACTIVE_ENV = {
    # git: nunca pidas usuario/password ni abras el credential manager.
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "echo",
    "GCM_INTERACTIVE": "never",
    # Señal universal de "estoy en un pipeline, no preguntes".
    "CI": "1",
    "DEBIAN_FRONTEND": "noninteractive",
    # npm/yarn: asumir sí, no pintar barras de progreso.
    "npm_config_yes": "true",
    "npm_config_audit": "false",
    "npm_config_fund": "false",
    "npm_config_progress": "false",
    # pip: sin chequeo de versión ni caché de rueda a mitad de camino.
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",
    # dotnet / powershell: sin telemetría ni banner (ruido en la salida).
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "DOTNET_NOLOGO": "1",
    "POWERSHELL_TELEMETRY_OPTOUT": "1",
    "POWERSHELL_UPDATECHECK": "Off",
    # Salida limpia: los códigos ANSI se comen el presupuesto de tokens.
    "NO_COLOR": "1",
    "TERM": "dumb",
    "PYTHONUNBUFFERED": "1",
    "PYTHONIOENCODING": "utf-8",
}

# Pistas de que falta instalar algo. No se instala solo: se le dice al
# experto que pregunte (ver `ask_human` en experts.py). El contrato con
# el usuario es "libertad para ejecutar, pregunta para instalar".
_MISSING_PATTERNS = (
    r"command not found",
    r"no se reconoce como un comando",
    r"is not recognized as an internal or external command",
    r"CommandNotFoundException",
    # 2026-09-04: las dos de arriba son la redacción de `cmd.exe`, y
    # `CommandNotFoundException` solo aparece en el error largo de
    # powershell.exe 5.1. `build_argv` resuelve `pwsh` primero, y pwsh 7
    # imprime un texto corto que no trae ninguna de las tres: el hint de
    # "falta una herramienta" NUNCA se disparaba desde PowerShell.
    r"is not recognized as a name of a cmdlet",
    r"no se reconoce como nombre de un cmdlet",
    r"No such file or directory: '?(?P<what>[\w.\-]+)'?",
    r"ModuleNotFoundError: No module named",
    r"ImportError: No module named",
    r"could not be found",
    r"unable to locate package",
)
_MISSING_RE = re.compile("|".join(_MISSING_PATTERNS), re.IGNORECASE)

IS_WINDOWS = os.name == "nt"
# Cuánto de la salida se guarda EN MEMORIA mientras el proceso corre. El
# cap que ve el LLM lo aplica `_cap_tool_result` después; esto es la red
# para que un proceso que escupe gigabytes no se lleve puesto el relay.
MAX_CAPTURE_BYTES = int(os.environ.get("FOURBIS_SHELL_MAX_CAPTURE", "2000000"))


#: Verbos aprobados de PowerShell. Un cmdlet es `Verbo-Sustantivo`, así
#: que reconocerlos con el patrón cubre TODOS los del verbo — la lista a


def bash_exe() -> str:
    """El bash que hay que usar. "" si no hay ninguno servible.

    2026-09-02, la causa de raíz del día: en Windows
    `shutil.which("bash")` devuelve `C:\\Windows\\System32\\bash.exe`, que
    es **WSL**, no Git Bash. Todo lo que el `auto` ruteaba a `sh` se
    estaba ejecutando en otro sistema operativo: otro filesystem (`/mnt/c`
    en vez de `/c`), otro PATH y ninguna de las herramientas de la
    máquina. Por eso `dotnet build … | tail -30` contestaba
    `dotnet: command not found` mientras `dotnet --version` andaba
    perfecto por cmd, y por eso el `git` de esos pipelines era 2.43.0
    (el de Ubuntu) y no el 2.52.0 que tiene Windows.

    Lo disimulaba que `head`, `grep` y `tail` existen en los dos lados:
    los pipelines de puro texto funcionaban y solo se rompían los que
    mezclaban un filtro unix con una herramienta instalada en Windows —
    que son 49 de los 58 comandos rechazados del 1/9.

    Git Bash primero, y `System32` excluido explícitamente: si algún día
    se quiere WSL, va a ser una decisión escrita, no el resultado de un
    `which`.
    """
    if not IS_WINDOWS:
        return shutil.which("bash") or shutil.which("sh") or ""
    candidatos = []
    for base in (os.environ.get("ProgramFiles"),
                 os.environ.get("ProgramFiles(x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base:
            candidatos.append(os.path.join(base, "Git", "bin", "bash.exe"))
    # Git instalado en otro lado: `git.exe` suele vivir en `…\Git\cmd\`.
    git = shutil.which("git")
    if git:
        candidatos.append(
            os.path.join(os.path.dirname(os.path.dirname(git)), "bin", "bash.exe"))
    for c in candidatos:
        if c and os.path.exists(c):
            return c
    suelto = shutil.which("bash")
    if suelto and "system32" not in suelto.lower():
        return suelto
    return ""


def _env_for_run(extra: Optional[dict] = None, *, kind: str = "") -> dict:
    env = dict(os.environ)
    env.update(_NONINTERACTIVE_ENV)
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    # 2026-09-02: hubo acá una conversión del PATH de Windows a formato
    # POSIX para bash (`path_para_bash`, ya borrada), motivada por
    # `dotnet build | tail -30` contestando "dotnet: command not found"
    # dentro de bash. La causa real de ESE síntoma era otra, arreglada
    # el mismo día: `bash_exe()` elegía el bash de WSL
    # (`C:\Windows\System32\bash.exe`), que corre en OTRO filesystem con
    # su propio PATH — no le llegaba el de Windows ni convertido ni sin
    # convertir. Con Git Bash real (el que `bash_exe()` elige ahora),
    # MSYS2 ya traduce el PATH solo al arrancar, así que la conversión
    # de acá quedó redundante — y dañina: un hijo WINDOWS lanzado desde
    # ese bash (`python.exe`, `gh.exe`) heredaba el PATH ya convertido a
    # POSIX, y en Windows `os.pathsep` es `;`, no `:` — así que ese hijo
    # veía el PATH entero como UNA sola entrada inutilizable (medido:
    # pasó de ~73 entradas a 4). Un experto vio `gh` desaparecer y
    # concluyó "cmd.exe no está en el PATH de Python": no era eso.
    # Medido sin la conversión, con Git Bash y entorno limpio: bash
    # encuentra `gh`/`dotnet` igual (se lo da MSYS2), y el hijo Windows
    # vuelve a ver el PATH completo.
    return env



#: Lo que `cmd.exe` / bash / PowerShell ponen entre comillas cuando no
#: encuentran algo. Sirve para separar "falta el binario" de "la ruta que
#: escribiste no existe", que dan EXACTAMENTE el mismo mensaje.
#
# Se toma TODO lo que hay antes de la frase, sin intentar desarmar las
# comillas: cmd re-cita lo que recibió (`"\"C:\...\.exe\""`), así que un
# patrón que exija comillas balanceadas no matchea justo en el caso que
# importa. Limpiar el token es tarea de `_parece_ruta`.
_QUE_FALTA_RE = re.compile(
    r"^(?P<t>.{1,200}?)\s+(?:no se reconoce|is not recognized)"
    r"|(?:^|\s)(?P<u>[^\s:]{1,200}): command not found",
    re.IGNORECASE | re.MULTILINE)


def _parece_ruta(token: str) -> bool:
    """¿Lo que no se encontró es una RUTA y no el nombre de un binario?"""
    t = (token or "").strip().strip('"').strip("'").replace('\\"', "")
    return bool(t) and ("\\" in t or "/" in t or ":" in t)


def missing_tool_hint(salida: str) -> str:
    """Si la salida dice "no existe tal comando", devuelve el aviso.

    "" cuando no aplica. El texto va dirigido al MODELO: le dice que la
    salida no es un error suyo y que la acción correcta es preguntarle al
    humano, no improvisar una instalación.

    2026-09-01: una RUTA que no existe da el mismo mensaje que un binario
    que falta, y este hint leía los dos como "falta la herramienta". Caso
    medido (un chat de ejemplo, nodo de INVENTORYDEMO): el modelo inventó
    `C:\\Users\\demo\\.dotnet\\dotnet.exe` —la real es
    `C:\\Program Files\\dotnet\\dotnet.exe`—, cmd contestó "no se
    reconoce", el hint lo mandó a `ask_human` y el nodo frenó el grafo
    para pedir que instalaran el SDK de .NET 10. En esa máquina hay
    CINCO SDKs instalados y `dotnet build` corre en 7 segundos. Cuando lo
    que falta trae `\\`, `/` o `:`, el problema es el path, no la
    herramienta — y decírselo evita la interrupción al humano.
    """
    if not _MISSING_RE.search(salida or ""):
        return ""
    m = _QUE_FALTA_RE.search(salida or "")
    token = next((g for g in (m.groups() if m else ()) if g), "")
    if _parece_ruta(token):
        return (
            f"\n\n[RUTA INEXISTENTE] `{token.strip()}` no existe en esta "
            "máquina; NO significa que falte la herramienta. Invocá el "
            "comando por su nombre a secas (si está en el PATH funciona) "
            "o averiguá dónde está de verdad antes de dar por ausente "
            "nada. No preguntes por una instalación por esto.")
    return (
        "\n\n[FALTA UNA HERRAMIENTA] La salida indica que el comando o el "
        "paquete no está instalado en esta máquina. NO intentes instalarlo "
        "por tu cuenta ni busques un workaround silencioso: usa la tool "
        "`ask_human` para preguntar si lo instala, diciendo QUÉ falta y "
        "PARA QUÉ lo necesitás. Si el humano dice que sí, ahí sí ejecutá "
        "la instalación.")


_POSIX_DRIVE_RE = re.compile(r"^/([A-Za-z])(/.*)?$")


def _cwd_para_windows(p: str) -> str:
    """`/c/Users/x` → `C:/Users/x`. Cualquier otra cosa vuelve intacta.

    El modelo escribe `cwd` en forma POSIX porque es lo que ve en la
    salida de bash y en los paths que le mostramos — es entrada del
    LLM, un trust boundary: no hay que confiar en que venga en forma
    Windows. `CreateProcess` no entiende `/c/...` y tira
    `[WinError 267] El nombre de directorio no es válido` sin decirle
    nada útil al modelo (le pasó real: 7 intentos fallidos seguidos,
    100 minutos bloqueado, terminó abriendo una pregunta al humano).

    Solo convierte pinta de drive POSIX real: `/<UNA letra>` o
    `/<UNA letra>/...`. `/usr/local` NO es un drive (dos letras) y
    queda como está, igual que cualquier cosa ya en forma Windows
    (`C:/...` o `C:\\...`, que ni arranca con `/`).
    """
    m = _POSIX_DRIVE_RE.match(p or "")
    if not m:
        return p
    return f"{m.group(1).upper()}:{m.group(2) or '/'}"


def _resolver_cwd(cwd: Optional[str],
                  base: Optional[str]) -> tuple[Optional[str], str]:
    """`cwd` de la tool → directorio real. Devuelve `(cwd, error)`.

    Con `error != ""` no hay que ejecutar nada: el texto ya está escrito
    para que lo lea el modelo.

    **El bug que arregla (2026-09-04).** El call site hacía
    `cwd=(cwd or _repo or None)`: el `or` cortocircuita, así que con un
    `cwd` RELATIVO el repo nunca se usaba de base y la ruta se resolvía
    contra el cwd del proceso del relay. `os.path.isdir` fallaba y el
    harness contestaba `"cwd no existe: 'frontend'"` — una afirmación
    falsa, dicha con seguridad, sobre un directorio que sí existe. El
    modelo la desmiente con `Test-Path` y reintenta: 2 de 8 tool calls
    (25 %) y ~6 s tirados en un run medido, y el run `un run de ejemplo` del 2/9
    hundido hasta que el experto abrió una pregunta al humano diciendo
    "no es un problema del proyecto, es del harness". Encima el harness
    era incoherente consigo mismo: `read_file` sí acepta rutas relativas
    al repo, y el docstring de la tool promete "vacío = la raíz del
    repo".

    **La contención.** `cwd` es entrada del LLM, un trust boundary, y el
    sandbox de `files.Permisos` NO lo cubre (lo dice su propio
    docstring: no contiene a `shell`). Resolver relativos contra la raíz
    convierte `"..\\..\\otro-repo"` —que HOY falla— en una salida del
    repo, así que el relativo que se escapa se rechaza. Deliberadamente
    NO se toca el `cwd` absoluto fuera del repo, que hoy se permite:
    ampliar o cerrar el sandbox de `shell` es otra decisión, no esta.
    """
    cwd = (cwd or "").strip() or None
    if not cwd:
        # "Vacío = la raíz del repo", como promete el docstring de la tool.
        return (base or None), ""
    if IS_WINDOWS:
        cwd = _cwd_para_windows(cwd)
    if not base or os.path.isabs(cwd):
        return cwd, ""
    raiz = os.path.realpath(base)
    destino = os.path.realpath(os.path.join(raiz, cwd))
    try:
        adentro = os.path.commonpath([raiz, destino]) == raiz
    except ValueError:      # rutas en drives distintos: no hay ancestro común
        adentro = False
    if not adentro:
        return None, (f"cwd relativo se sale del repo: {cwd!r} → {destino!r} "
                      f"(raíz: {raiz!r}). Usa una ruta dentro del repo, o "
                      f"una absoluta si de verdad necesitas salir.")
    return destino, ""


@functools.lru_cache(maxsize=1)
def _codepage_consola() -> str:
    """El encoding en que los shells de Windows escriben a un pipe.

    NO es utf-8 y NO es el `locale.getpreferredencoding()` (que acá da
    cp1252 y decodifica MAL). Medido el 2026-09-04 en esta máquina:
    `GetConsoleOutputCP()` = 850, y los tres shells —pwsh 7,
    powershell.exe 5.1 y cmd.exe— emiten los mismos bytes cp850:

        b'l¡nea trmino'   →  utf-8  ERROR
                                   →  cp1252 'l¡nea t‚rmino'
                                   →  cp850  'línea término'   ✓

    Sin consola adjunta (servicio, proceso sin ventana)
    `GetConsoleOutputCP` devuelve 0; ahí sirve el codepage OEM del
    sistema, que es el que igual usan los hijos.
    """
    if not IS_WINDOWS:
        return "utf-8"
    try:
        import ctypes
        k = ctypes.windll.kernel32
        cp = k.GetConsoleOutputCP() or k.GetOEMCP()
        codecs.lookup(f"cp{cp}")
        return f"cp{cp}"
    except Exception:  # noqa: BLE001 — sin codepage se sigue con utf-8
        return "utf-8"


def _decodificar(buf: bytes) -> str:
    """Bytes de un shell → texto, sin comerse los acentos.

    2026-09-04, el bug: esto era `buf.decode("utf-8", errors="replace")`
    a secas, y en Windows los shells NO escriben utf-8 (ver
    `_codepage_consola`). Cada palabra con tilde llegaba mutilada al
    modelo —"línea"→"l?nea", "término"→"t?rmino"— en proyectos que son
    todos en español, y con ella el error nativo que el modelo tenía que
    leer para decidir qué hacer.

    utf-8 va PRIMERO y en modo estricto porque casi todo lo que corre
    acá (git, node, python, dotnet) sí emite utf-8; el codepage de
    consola es el plan B y, por ser de un byte, nunca falla.

    El decoder incremental es por el recorte a `MAX_CAPTURE_BYTES`: una
    salida utf-8 cortada al medio de un carácter deja una secuencia
    incompleta al final, y con `decode` estricto eso mandaría TODA la
    salida al plan B. Con `final=False` esa cola incompleta se retiene
    (se pierden ≤3 bytes del recorte, que ya estaba recortado).
    """
    try:
        return codecs.getincrementaldecoder("utf-8")().decode(buf)
    except UnicodeDecodeError:
        pass
    try:
        return buf.decode(_codepage_consola())
    except (UnicodeDecodeError, LookupError):
        return buf.decode("utf-8", errors="replace")
