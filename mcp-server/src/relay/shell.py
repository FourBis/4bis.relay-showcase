"""Ejecución de comandos que NO se cuelga (2026-08-16).

Por qué existe: el `run_shell` del wrapper MCP (`mcp_wrapper`, repo
externo) se traba con PowerShell y con cualquier comando que espere algo
por stdin. Como ese repo no se toca desde acá, el relay pasa a tener su
propio ejecutor y deja de depender de él — el experto ejecuta con las
herramientas que necesite, sin pedir permiso para cada comando.

Las cuatro causas reales de cuelgue en Windows, y qué hace este módulo
con cada una:

1. **stdin abierto.** Un proceso que lee stdin —`Read-Host`, un prompt de
   confirmación, `git` pidiendo credenciales— espera para siempre contra
   un pipe que nadie cierra. Acá stdin va a DEVNULL: el read devuelve EOF
   inmediato y el comando falla rápido en vez de colgarse.
2. **El perfil de PowerShell.** `powershell.exe -Command` carga el
   `$PROFILE` del usuario, que puede imprimir, preguntar o tardar. Va con
   `-NoProfile -NonInteractive`.
3. **Prompts de las herramientas.** npm, apt, dotnet, git y pip preguntan
   o esperan TTY. Se les setea el entorno no-interactivo conocido
   (`_NONINTERACTIVE_ENV`).
4. **Pipes que se llenan.** Un proceso que escribe mucho a stdout se
   bloquea cuando el buffer del pipe se llena y nadie lee. Acá se drena
   en paralelo mientras corre, no al final.

5. **Un nieto que hereda el stdout.** El hijo directo termina, pero algo
   que él lanzó se queda con el pipe abierto y el `proc.wait()` de
   asyncio no vuelve nunca: ese future se resuelve cuando el proceso
   terminó **y** los pipes se cerraron. Acá se espera al hijo DIRECTO
   (ver `_esperar_al_hijo`). Medido el 2026-08-31: un
   `Start-Process ... -RedirectStandardOutput` de PowerShell vuelve al
   prompt en menos de un segundo y `shell.run` seguía esperando a los
   25s, hasta que el timeout mató el árbol — y con él, el server que el
   comando acababa de levantar.

Y cuando el timeout dispara, se mata el **árbol** de procesos: matar solo
al padre deja al hijo escribiendo al pipe y el cuelgue sigue.
"""
from __future__ import annotations

import asyncio
import codecs
import contextlib
import functools
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("relay.shell")

# Entorno no-interactivo. Todo esto existe porque alguna herramienta,
# alguna vez, decidió preguntar en medio de un script.
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
#: mano que había acá enumeraba `Select-Object` y `Where-Object` pero no
#: `Select-String`, `Out-String`, `Out-Null` ni `Tee-Object`, y esos
#: caían en `cmd.exe`, que no tiene ninguno: 16 comandos muertos en 14
#: días, siempre con el mismo "no se reconoce como un comando interno".
_PS_VERBOS = (
    "Add|Clear|Compare|Convert|ConvertFrom|ConvertTo|Copy|Disable|Enable|"
    "Export|Format|ForEach|Get|Group|Import|Invoke|Join|Measure|Move|New|"
    "Out|Read|Remove|Rename|Resolve|Restart|Select|Set|Sort|Split|Start|"
    "Stop|Tee|Test|Wait|Where|Write"
)
_PS_RE = re.compile(r"\b(?:" + _PS_VERBOS + r")-[A-Za-z]\w+", re.I)
#: Lo que delata PowerShell sin ser un cmdlet.
_PS_OTRAS = ("-erroraction", "$env:", "$psversiontable", ".ps1", "|%", "| %")


def _looks_like_powershell(cmd: str) -> bool:
    """Heurística: ¿este comando es PowerShell y no cmd/sh?

    No hace falta acertar siempre: en Windows, `cmd.exe` no entiende
    cmdlets, así que ante la duda conviene PowerShell, que sí entiende
    casi todo lo que entiende cmd.

    Sin anclar al principio de la línea a propósito: el modelo escribe
    mucho `powershell -NoProfile -Command "<cmdlets>"`, y ahí el cmdlet
    va DENTRO de las comillas. Medido sobre los 1.241 comandos reales de
    14 días, ampliar esto mueve 32 comandos de `cmd` a PowerShell y los
    32 son de esa forma — o sea, ya eran PowerShell.
    """
    return bool(_PS_RE.search(cmd)) or any(
        s in cmd.lower() for s in _PS_OTRAS)


#: Programas que NO trae ni `cmd.exe` ni PowerShell. `sort`, `ls`, `cat`
#: y `rm` quedan afuera a propósito: PowerShell los tiene como alias, así
#: que verlos no prueba que el comando sea unix.
#:
#: 2026-08-31: faltaba `tail`, y era el que más caro salía — 18 fallos en
#: 14 días, todos `<build> 2>&1 | tail -40`, que es exactamente cómo el
#: modelo pide el FINAL de una compilación (donde está el veredicto, ver
#: `_cap_text` en experts.py). `sleep` también entra: PowerShell lo tiene
#: como alias de `Start-Sleep`, pero cuando cae en `cmd.exe` no existe, y
#: bash lo corre igual. Los otros cuatro son inequívocos.
_SOLO_UNIX = ("head", "tail", "grep", "wc", "awk", "sed", "xargs", "cut",
              "tr", "uniq", "tee", "uname", "touch", "basename", "dirname",
              "sleep")
#: Solo cuenta si se INVOCA: al principio, o después de un pipe o un
#: separador. Sin esto, una ruta con "cut" adentro mandaría el comando al
#: shell equivocado.
_SH_RE = re.compile(r"(?:^|[|;]|&&)\s*(?:" + "|".join(_SOLO_UNIX) + r")\b")


# 2026-08-31, medido y DESCARTADO: mandar a `cmd.exe` los comandos que
# ya nombran su intérprete (`powershell -Command "…$_.Name…"`) para que
# el `$` no se interpole. Probado contra el comando real que falla:
#
#   envuelto → cmd         exit=0 y ECHO del comando con el `$_` comido
#   envuelto → powershell  exit=0 con ".Name no se reconoce"
#   payload solo, sin envolver, → powershell   funciona
#
# O sea: cmd.exe no lo arregla, lo vuelve PEOR — cambia un error
# ruidoso por un resultado vacío con exit 0, que el modelo lee como
# éxito. El problema no es a qué shell va sino el envoltorio.
#
# 2026-09-03, HECHO — `desenvolver_powershell` abajo. La causa medida es
# doble interpolación: el modelo escribe `powershell -Command "…"` y
# `build_argv` lo mete DENTRO de otro `powershell -Command` (o de
# `bash -c`), así que la capa de afuera expande cada `$var` a vacío
# antes de que el PowerShell de adentro la vea. Reproducido con los dos
# ruteos:
#
#   envuelto + comillas simples  → sh          bash se come `$b`
#   envuelto sin comillas simples → powershell  el PS de afuera se come `$x`
#
# Y así queda el comando que corrió hoy en fuentesucursalvirtual:
#   `$b=[IO.File]::ReadAllBytes($p); 'size='+$b.Length`
#   →  `=[IO.File]::ReadAllBytes(); 'size='+.Length`
#
# Medido sobre los 2.609 comandos con `cmd` registrado en relay.db: 640
# vienen envueltos y 166 además traen `$` adentro — o sea, estaban rotos
# garantizado. Este desenvoltorio agarra 120 de esos 166; los otros 46
# son 41 `-File` (que no sufren el problema, el payload vive en un
# archivo) y 5 que el log guardó truncados.

#: `powershell` / `pwsh`, con o sin `.exe`, al principio del comando.
_PS_ENVOLTORIO_RE = re.compile(r"^\s*(?:powershell|pwsh)(?:\.exe)?\s+", re.I)
#: Flags de `powershell.exe` que NO llevan valor.
_PS_FLAGS_SOLOS = ("noprofile", "noninteractive", "nologo", "noexit",
                   "sta", "mta")
#: Flags que llevan valor: hay que saltar el token siguiente también.
_PS_FLAGS_CON_VALOR = ("executionpolicy", "windowstyle", "inputformat",
                       "outputformat", "version", "configurationname")


def _payload_entre_comillas(resto: str) -> str:
    """El interior de `resto` si es UNA sola cadena citada. "" si no.

    La exigencia de que la cadena cubra todo el resto no es capricho:
    con `powershell -Command "x" | findstr y` el pipe es del shell de
    afuera, así que desenvolver cambiaría lo que el comando hace.
    """
    if len(resto) < 2 or resto[0] not in "\"'" or resto[-1] != resto[0]:
        return ""
    interior = resto[1:-1]
    comilla = resto[0]
    # Comillas sin escapar en el medio ⇒ eran varios argumentos, no uno.
    if comilla in interior.replace("\\" + comilla, ""):
        return ""
    if comilla == '"':
        # SOLO la comilla escapada con backslash, y en UNA pasada.
        #
        # 2026-09-04, encontrado en review: acá también se colapsaba
        # `""` → `"`, encadenado después del otro replace, y se comía
        # las comillas que el primero acababa de crear. Con
        # `\"a.\"\"b\"\".c\"` el payload salía `"a."b".c"` —un bareword
        # suelto— y encima con exit=0, que es el modo de falla que este
        # módulo evita a propósito: el modelo lo lee como éxito.
        #
        # El `""` no se toca porque es AMBIGUO: puede ser una comilla
        # escapada para la capa de afuera o el escape nativo de
        # PowerShell dentro de un string (`"a.""b"""`). Medido sobre los
        # 640 comandos envueltos de relay.db: 43 usan `\"` y CERO usan
        # `""` como escape del envoltorio, así que la regla no cubría
        # ningún caso real y sí rompía los que traen `""` propio.
        interior = interior.replace('\\"', '"')
    return interior


def desenvolver_powershell(cmd: str) -> str:
    """El payload de un `powershell -Command "…"`, o "" si no es eso.

    Correrlo con `shell_kind="powershell"` es lo que evita la doble
    interpolación (ver el bloque de arriba). Devuelve "" ante cualquier
    duda —`-File`, `-EncodedCommand`, un flag desconocido, comillas que
    no cierran— porque desenvolver de más corrompe un comando que hoy
    anda, y no desenvolver solo deja las cosas como estaban.
    """
    m = _PS_ENVOLTORIO_RE.match(cmd)
    if not m:
        return ""
    resto = cmd[m.end():].lstrip()
    while resto[:1] in ("-", "/"):
        tok, _, cola = resto.partition(" ")
        nombre = tok.lstrip("-/").lower()
        if not nombre:
            return ""
        # PowerShell acepta abreviar los flags (`-nop`, `-c`); `command`
        # va primero porque `-c` también es prefijo de `configurationname`
        # y ahí gana `-Command`, igual que en powershell.exe.
        if "command".startswith(nombre):
            return _payload_entre_comillas(cola.strip())
        if any(f.startswith(nombre) for f in _PS_FLAGS_CON_VALOR):
            _, _, cola = cola.lstrip().partition(" ")
        elif not any(f.startswith(nombre) for f in _PS_FLAGS_SOLOS):
            return ""
        resto = cola.lstrip()
    return ""


#: Comandos unix que PowerShell tiene como alias y `cmd.exe` NO tiene.
#: Distintos de `_SOLO_UNIX`: acá el problema no es que falten en los dos
#: shells, es que el `auto` los mandaba al ÚNICO que no los tiene.
_ALIAS_UNIX = ("ls", "pwd", "rm", "cat", "mv", "cp", "which", "df", "du")
_ALIAS_RE = re.compile(r"(?:^|[|;]|&&)\s*(?:" + "|".join(_ALIAS_UNIX) + r")\b")


def _looks_like_alias_unix(cmd: str) -> bool:
    """`ls Service/Courier/` y compañía: unix, pero sin binario unix.

    Van a bash y no a PowerShell —que también los tiene— porque el
    modelo que escribe `ls` escribe todo lo demás en unix: rutas con
    barra normal, `2>&1`, `&&`. La guarda es el backslash: con rutas
    Windows (`ls C:\\Users\\...`) bash se come las barras como escapes,
    así que ahí se deja como está.

    Medido: 15 comandos de esta forma en 14 días, los 15 fallaron. No
    hay ninguno que hoy funcione, así que mover esto no puede romper
    nada — es la definición de un caso sin contra.
    """
    return bool(_ALIAS_RE.search(cmd)) and "\\" not in cmd


def _looks_like_sh(cmd: str) -> bool:
    """¿Este comando está escrito para un shell unix?

    En Windows el `auto` elegía SOLO entre PowerShell y cmd, así que un
    `git ls-files | head -100` caía en `cmd.exe`, que no tiene `head`, y
    moría con exit 255 — siempre, no a veces. Medido sobre la bitácora:
    22 de los 76 comandos fallados del último mes son exactamente esto
    (`| head`, `| grep`, `| wc -l`).

    El modelo escribe pipelines unix porque es lo que sabe; la máquina
    tiene bash (viene con Git para Windows). Mandárselo a bash convierte
    un fallo seguro en un comando que corre. No se toca el orden con
    PowerShell: si el comando trae cmdlets, gana PowerShell como antes.
    """
    return bool(_SH_RE.search(cmd)) or "/dev/null" in cmd


_SINGLE_QUOTE_RE = re.compile(r"'[^']*'")


def _looks_like_single_quoted(cmd: str) -> bool:
    """`gh api graphql -F query='{ ... }'`: quoting POSIX de comillas simples.

    En `cmd.exe` y PowerShell la comilla simple no agrupa —es un
    carácter literal—, así que el string se parte en tokens: el caso
    real que disparó esto fue `gh` quejándose de "accepts 1 arg(s),
    received 11" porque el JSON entre comillas simples le llegó
    partido por espacios. En bash sí agrupa (comprobado: el mismo
    comando por `bash -c` da los 2 argumentos que corresponden).

    Exige un PAR cerrado (`'[^']*'`), no un apóstrofo suelto: `echo
    don't` trae una sola comilla —es texto, no quoting— y tiene que
    seguir yendo a cmd. Mismo guard de backslash que
    `_looks_like_alias_unix`: una ruta Windows (`C:\\Users\\...`) se
    come las barras como escapes en bash.
    """
    return bool(_SINGLE_QUOTE_RE.search(cmd)) and "\\" not in cmd


_POSIX_SYNTAX_RE = re.compile(r"\$\(|\$\{|`")


def _looks_like_posix_syntax(cmd: str) -> bool:
    """`$(...)`, `${...}` o backticks: sintaxis que SOLO existe en un shell unix.

    A diferencia de las heurísticas de arriba —que miran nombres de
    programa (`ls`, `head`) o un estilo de comillas—, esto no depende
    de qué palabra aparezca: sustitución de comandos (`$(...)`,
    backticks) y expansión de parámetros (`${...}`) no tienen
    equivalente en `cmd.exe`. Caso real que lo disparó:

        cd "/c/..." && WIN_REPO_ROOT="$(cygpath -w "$(pwd)")" python ...

    Las cuatro heurísticas anteriores daban False —ningún nombre de la
    lista, ninguna comilla simple— así que caía a `cmd`, que no
    entiende `$(...)` y contesta "El sistema no puede encontrar la
    ruta especificada." Un comando casi idéntico había andado antes
    solo porque traía un `ls` de encima: funcionaba por accidente, no
    porque algo reconociera la sintaxis.

    `$(...)` también es válido en PowerShell (subexpression operator,
    `Write-Host "$(Get-Date)"`), así que esto NO puede evaluarse antes
    que `_looks_like_powershell` — sigue yendo primero en `build_argv`
    y gana. Mismo guard de backslash que `_looks_like_alias_unix` y
    `_looks_like_single_quoted`: una ruta Windows no se manda a bash
    por esto.
    """
    return bool(_POSIX_SYNTAX_RE.search(cmd)) and "\\" not in cmd


#: Ruta Windows de verdad: `C:\`, `\\server\share` (UNC), o `\algo\` /
#: `\algo/` con 2+ caracteres. A propósito NO matchea escapes de una
#: sola letra (`\n`, `\t`, `\"`): `curl -w "\nHTTP %{http_code}\n"` y
#: `docker ps --format "...\t..."` son bash perfectamente válido y no
#: tienen que irse a cmd por eso.
_CMD_RUTA_WIN_RE = re.compile(r"[A-Za-z]:\\|\\\\|\\[A-Za-z0-9_.\-]{2,}[\\/]")
#: Builtins de cmd.exe que no existen en bash. Anclado a inicio o
#: después de `|`/`&`/`;` para no matchear la palabra en medio de un
#: argumento (ej. un archivo que se llame `move.txt`).
_CMD_BUILTIN_RE = re.compile(
    r"(^|[|&;]\s*)(dir|copy|del|erase|type|cls|ren|rename|move|start|"
    r"set|title|assoc|ver|vol|xcopy|robocopy|where|call)\b", re.I)
#: Expansión `%VAR%` de cmd. Exige letra/underscore después del primer
#: `%` para no confundir el `%{http_code}` de un `curl -w` (formato,
#: no variable) con `%SHELL%`.
_CMD_VAR_RE = re.compile(r"%[A-Za-z_][A-Za-z0-9_]*%")
#: Scripts .bat/.cmd: solo corren bajo cmd.exe.
_CMD_BAT_RE = re.compile(r"\.(bat|cmd)\b", re.I)
#: Flag estilo Windows (`/all`, `/FI "..."`, `/t`): MSYS2/Git Bash
#: convierte un argumento que empieza con `/` en ruta Windows antes de
#: pasarlo al ejecutable, así que `ipconfig /all` bajo bash le llega a
#: `ipconfig.exe` como una ruta, no un flag. Termina en espacio o fin de
#: línea —sin otra barra— para no matchear una ruta unix real de MÁS de
#: un segmento: `/usr/local` tiene una segunda barra y no matchea; una
#: URL con la barra pegada a texto (`.../orgs/x`) tampoco, porque exige
#: espacio o inicio de línea ANTES de la barra.
#:
#: 2026-09-02: eso solo no alcanza contra una ruta unix de UN solo
#: segmento (`/proc`, `/tmp`, `/opt`) — son "espacio + / + letras +
#: espacio", estructuralmente idénticas a un flag `/all`. El caso real
#: que lo mostró: `find / -path /proc -prune` mandaba a cmd porque
#: `find` no está en `_SOLO_UNIX` ni `_ALIAS_UNIX` —a propósito:
#: `cmd.exe` tiene su PROPIO builtin `find` (búsqueda de texto, otra
#: sintaxis), así que agregarlo a esas listas rompería un `find "texto"
#: archivo.txt` real de cmd al revés, mandándolo a bash. La solución no
#: es esa ni ensanchar el regex en general (pierde precisión sobre
#: flags reales): es excluir por nombre los directorios raíz de unix,
#: un conjunto cerrado y conocido donde ningún flag de Windows cae.
#: Medido contra el regex sin esta exclusión: 13 casos de control pasan
#: de 4 errores a 0, y de 2170 comandos reales 12 cambian de veredicto
#: —los 12 son rutas unix legítimas (`find / -path /proc`, `cd /tmp`,
#: `ls /root/.4bis`, `cp a.txt /opt`) que sin esto irían mal a cmd. Los
#: 5 flags de Windows medidos (`/all`, `/FI`, `/t`, `/R`, `/PID`) no
#: bajan: ninguno se llama como una raíz unix.
_RAICES_UNIX = ("proc|tmp|usr|etc|dev|var|opt|home|bin|sbin|root"
                "|mnt|media|srv|lib|boot|run")
_CMD_WIN_FLAG_RE = re.compile(
    r"(?:^|\s)/(?!(?:" + _RAICES_UNIX + r")(?:\s|$|/))"
    r"[A-Za-z?][A-Za-z0-9_?-]*(?=\s|$)")


#: Segmentos citados: se borran antes de buscar el `;` para no confundir
#: `echo "a;b"` (texto) con `cd x; git status` (encadenado). Entiende
#: `\"` porque el modelo escribe payloads ya escapados.
_CITADO_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')


def _tiene_punto_y_coma(cmd: str) -> bool:
    """`;` FUERA de comillas: encadena en PowerShell y en sh, NO en cmd."""
    return ";" in _CITADO_RE.sub("", cmd)


def _looks_like_cmd(cmd: str) -> bool:
    """¿Este comando trae una señal EXPLÍCITA de que es para cmd.exe?

    2026-09-02: medido sobre 2170 comandos reales de 1003 chats, la
    tasa de fallo por intérprete es `cmd` 45.9%, `sh` 32.9%,
    `powershell` 28.7% — `cmd` es el PEOR de los tres y hasta ahora era
    el default (`build_argv`, rama `else`). De los 582 comandos que
    caían ahí por default, 335 no traían ninguna señal de Windows y
    fallaban un 45.5%. El default se invierte: ahora hace falta esto
    (o alguna de las cuatro heurísticas unix, que se evalúan antes)
    para ELEGIR cmd; todo lo demás cae a bash si hay `bash_exe()`.

    De acá en más, ESTA es la lista que hay que mantener actualizada
    —señales de que un comando es para cmd—, no las de unix: son las
    que quedan como excepción al nuevo default.

    TRADE-OFF ACEPTADO, medido: un apóstrofo suelto (`echo don't`) antes
    caía a cmd y funcionaba; ahora va a bash y muere con "unexpected EOF
    while looking for matching quote". En el corpus de 2170 comandos hay
    2 con comillas simples impares (0.09%) y el ÚNICO que rutea a sh es
    `grep -nE "doc[\\"']|..."`, donde la comilla va dentro de comillas
    dobles y bash lo ejecuta bien (verificado, exit=0). O sea: cero
    roturas reales.
    NO agregar una guarda de "comillas impares → cmd": mandaría ese grep
    a cmd, donde SÍ rompe. La guarda haría más daño que el problema.

    2026-09-02, quinta señal (`_CMD_WIN_FLAG_RE`): faltaba el flag
    estilo Windows (`ipconfig /all`, `tasklist /FI "..."`). Medido
    corriendo por el shell del relay: `ipconfig /all` bajo bash daba
    "línea de comandos desconocida o incorrecta" y `tasklist /FI "..."`
    devolvía el argumento mangleado como ruta (`"C:/..."` en vez de
    `/FI`) — es MSYS2/Git Bash reescribiendo cualquier argumento que
    arranca con `/` antes de pasarlo al exe. Sobre el corpus de 2170,
    la regresión exacta (antes cmd, ahora sh, con flag `/X`) son 9
    comandos (0.41%) — pero utilitarios como `ipconfig`/`tasklist` ni
    aparecen ahí porque el corpus refleja lo que el experto SIGUIÓ
    intentando después de que fallara, no lo que debería andar.

    2026-09-07, sexta señal — en realidad una DESCALIFICACIÓN: un `;`
    fuera de comillas encadena en PowerShell y en sh, pero no existe en
    `cmd.exe` (ahí es texto: `cd x; git status` deja "; git status"
    pegado al argumento de `cd`). Si aparece, esto vuelve False sin
    mirar el resto de las señales — no importa que el comando también
    traiga una ruta `C:\\` o un builtin de cmd, el `;` ya lo descarta.
    """
    if _tiene_punto_y_coma(cmd):
        return False
    return bool(
        _CMD_RUTA_WIN_RE.search(cmd) or _CMD_BUILTIN_RE.search(cmd)
        or _CMD_VAR_RE.search(cmd) or _CMD_BAT_RE.search(cmd)
        or _CMD_WIN_FLAG_RE.search(cmd))


#: Una ruta Windows absoluta DENTRO del comando: `C:\\Users\\x`. Corta en
#: espacio, comilla o metacaracter de shell, que es donde termina el
#: argumento.
_RUTA_WIN_EN_CMD_RE = re.compile(r"""([A-Za-z]):(\\[^\s"'|&;<>]*)""")


def _rutas_para_bash(cmd: str) -> str:
    """`cd C:\\Users\\x && …` → `cd C:/Users/x && …`.

    2026-09-04, medido: `_looks_like_sh` es la ÚNICA de las cuatro
    heurísticas de ruteo sin la guarda de `\\`; las otras tres la
    tienen. Así que un comando que mezcla ruta Windows con utilidad unix
    —`cd C:\\repo && dotnet build | tail -40`, que es exactamente la
    forma en que el modelo pide el final de una compilación— se va
    entero a bash, y bash trata cada `\\` como escape del carácter
    siguiente:

        cd C:\\Users\\demo\\source\\repos\\FourBis\\4bis.relay && git ls-files
        → cd: C:UsersdemosourcereposFourBis4bis.relay: No such file or directory

    El directorio existe: el modelo lee "no existe" de una ruta que él
    escribió bien y no tiene cómo saber que el problema es el escaping.
    Medido sobre el historial: 54 comandos con esta forma, 9 de los
    cuales todavía rutean a `sh`, y un chat que pegó 4 veces seguidas
    contra esto variando el pipe del final.

    Se convierte en vez de mandar el comando a `cmd.exe` porque `cmd`
    no tiene `tail`: la guarda arreglaría la ruta rompiendo el pipe. Git
    Bash entiende `C:/Users/x` igual que `/c/Users/x`.

    ponytail: sustitución textual, así que también toca una ruta que
    estuviera adentro de comillas simples (`grep 'C:\\temp'`). Hoy ese
    caso ya está roto —bash se come las barras igual—, así que no hay
    nada que preservar; si aparece uno real, hace falta tokenizar.
    """
    return _RUTA_WIN_EN_CMD_RE.sub(
        lambda m: f"{m.group(1)}:{m.group(2).replace(chr(92), '/')}", cmd)


def build_argv(cmd: str, *, shell_kind: str = "auto") -> tuple[list[str], str]:
    """`(argv, interprete_usado)` para correr `cmd` sin colgarse.

    `shell_kind`: `auto` | `powershell` | `cmd` | `sh`. En `auto` se elige
    por sistema operativo y por la pinta del comando.

    Los flags no son decorativos:
      - `-NoProfile`: no cargar el $PROFILE del usuario (puede preguntar,
        imprimir o tardar segundos).
      - `-NonInteractive`: cualquier prompt falla en vez de esperar.
      - `-ExecutionPolicy Bypass`: un relay que no puede correr un .ps1
        por policy es un relay inútil en Windows; el vetting de qué se
        ejecuta lo hace el harness, no la policy de PowerShell.
      - `/d`: sin AutoRun del registro, que es otra fuente de sorpresas.
      - `sh -c` y no `bash -lc`: `-l` carga el profile, mismo problema
        que el $PROFILE de PowerShell.
    """
    kind = (shell_kind or "auto").lower()
    if kind == "auto":
        if IS_WINDOWS:
            # Primero de todo: si el modelo ya envolvió el comando en un
            # `powershell -Command "…"`, se corre el payload PELADO. Meter
            # el envoltorio dentro de otro shell interpola los `$var` dos
            # veces y llegan vacíos (ver el bloque de arriba).
            payload = desenvolver_powershell(cmd)
            if payload:
                cmd, kind = payload, "powershell"
            elif _looks_like_powershell(cmd):
                kind = "powershell"
            elif (_looks_like_sh(cmd) or _looks_like_alias_unix(cmd)
                    or _looks_like_single_quoted(cmd)
                    or _looks_like_posix_syntax(cmd)) \
                    and bash_exe():
                kind = "sh"
            elif _looks_like_cmd(cmd):
                kind = "cmd"
            elif bash_exe():
                kind = "sh"
            else:
                kind = "cmd"
        else:
            kind = "sh"

    if kind == "powershell":
        exe = (shutil.which("pwsh") or shutil.which("powershell")
               or "powershell.exe")
        # PowerShell 7.3+ también eleva los errores de ejecutables nativos.
        # En 5.1 hay que comprobar LASTEXITCODE después de cada nativo.
        cmd = ("$ErrorActionPreference = 'Stop'; "
               "$PSNativeCommandUseErrorActionPreference = $true;\n" + cmd)
        return ([exe, "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", cmd], kind)
    if kind == "cmd":
        exe = os.environ.get("COMSPEC") or "cmd.exe"
        return ([exe, "/d", "/s", "/c", cmd], kind)
    # Ya está decidido que va a bash: las rutas Windows que traiga el
    # comando se pasan a barra normal o bash se come los backslashes
    # (ver `_rutas_para_bash`). Va acá y no en el `auto` para que las
    # heurísticas de ruteo sigan viendo el comando tal como lo escribió
    # el modelo, y para que también cubra un `shell_kind="sh"` explícito.
    if IS_WINDOWS:
        cmd = _rutas_para_bash(cmd)
    shell = bash_exe() or "/bin/sh"
    if shell.lower().endswith("bash.exe") or shell.endswith("bash"):
        return ([shell, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", cmd], kind)
    return ([shell, "-e", "-c", cmd], kind)


@functools.lru_cache(maxsize=1)
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
        if IS_WINDOWS:
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
    argv, kind = build_argv(cmd, shell_kind=shell_kind)
    t0 = time.monotonic()
    cwd, err = _resolver_cwd(cwd, base)
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
    if not IS_WINDOWS:
        kwargs["start_new_session"] = True
    spawn_kw = dict(
        stdin=subprocess.DEVNULL,           # ← la causa #1 de cuelgues
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,           # una sola cronología
        cwd=cwd or None,
        env=_env_for_run(env_extra, kind=kind),
        **kwargs,
    )
    try:
        if IS_WINDOWS and kind == "cmd":
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
    drenaje = asyncio.create_task(_drain(proc.stdout, buf, MAX_CAPTURE_BYTES))
    timed_out = False
    huerfano = False
    try:
        huerfano = await asyncio.wait_for(
            _esperar_al_hijo(proc), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        _kill_tree(proc)
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
        _kill_tree(proc)
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

    out = _decodificar(buf)
    if len(buf) >= MAX_CAPTURE_BYTES:
        out += (f"\n…[salida recortada a {MAX_CAPTURE_BYTES} bytes en "
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
    argv, kind = build_argv(cmd, shell_kind=shell_kind)
    cwd, err = _resolver_cwd(cwd, base)
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
    if IS_WINDOWS:
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
                argv[-1] if IS_WINDOWS and kind == "cmd" else argv,
                shell=IS_WINDOWS and kind == "cmd",
                stdin=subprocess.DEVNULL, stdout=fh,
                stderr=subprocess.STDOUT, cwd=cwd or None,
                env=_env_for_run(env_extra, kind=kind), **kwargs)
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
            await asyncio.to_thread(_kill_tree, proc)
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
