"""shell syntax: funciones del flujo shell."""
from __future__ import annotations
import os
import re
import shutil
from . import shell_environment

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
        if shell_environment.IS_WINDOWS:
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
                    and shell_environment.bash_exe():
                kind = "sh"
            elif _looks_like_cmd(cmd):
                kind = "cmd"
            elif shell_environment.bash_exe():
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
    if shell_environment.IS_WINDOWS:
        cmd = _rutas_para_bash(cmd)
    shell = shell_environment.bash_exe() or "/bin/sh"
    if shell.lower().endswith("bash.exe") or shell.endswith("bash"):
        return ([shell, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", cmd], kind)
    return ([shell, "-e", "-c", cmd], kind)
