"""Shell propio del relay + preguntas del experto al humano (2026-08-16).

Dos pedidos con una raíz común: el experto tiene que poder **ejecutar sin
trabarse** y **preguntar cuando de verdad hace falta** — ver
docs/SHELL_Y_PREGUNTAS.md.

  - `relay/shell.py`: el `run_shell` del wrapper MCP se cuelga con
    PowerShell y con cualquier comando que espere stdin. El relay trae el
    suyo: stdin cerrado, sin perfil, entorno no-interactivo, drenaje en
    paralelo y kill del árbol al vencer el timeout.
  - `ask_human`: el experto anota la pregunta y TERMINA el turno. La
    respuesta entra como el turno siguiente del hilo.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from relay import experts, shell
from relay.db import Database

POSIX = sys.platform != "win32"


@pytest.fixture
async def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    await d.init_schema()
    return d


# ---------- 1. el shell no se cuelga ----------


@pytest.mark.skipif(not POSIX, reason="usa comandos POSIX")
async def test_stdin_cerrado_no_cuelga():
    """La causa #1: un comando que lee stdin esperaba para siempre."""
    r = await asyncio.wait_for(
        shell.run("read x; echo \"leí: $x\"", timeout=5), timeout=15)
    assert r["timed_out"] is False
    assert r["duration_ms"] < 3000       # vuelve al toque, no a los 5s


@pytest.mark.skipif(not POSIX, reason="usa comandos POSIX")
async def test_exit_code_va_al_final_de_la_salida():
    """El experto necesita el veredicto, y la elisión conserva la última
    línea corta: por eso el exit va al final y solo."""
    r = await shell.run("echo hola; exit 3", timeout=10)
    assert r["exit"] == 3
    assert r["out"].rstrip().endswith("(exit=3)")


@pytest.mark.skipif(not POSIX, reason="usa comandos POSIX")
async def test_timeout_corta_y_lo_dice():
    r = await shell.run("sleep 30", timeout=1.0)
    assert r["timed_out"] is True
    assert r["duration_ms"] < 15_000     # no esperó los 30s
    assert "TIMEOUT" in r["out"]
    assert "background" in r["out"]      # dice qué hacer


@pytest.mark.skipif(not POSIX, reason="usa comandos POSIX")
async def test_salida_gigante_no_deadlockea():
    """Un pipe que se llena y nadie drena = proceso trabado para siempre."""
    r = await asyncio.wait_for(
        shell.run("for i in $(seq 1 20000); do echo linea-$i-relleno; done",
                  timeout=30),
        timeout=60)
    assert r["exit"] == 0
    assert len(r["out"]) > 100_000


@pytest.mark.skipif(not POSIX, reason="usa comandos POSIX")
async def test_corre_en_el_cwd_pedido(tmp_path):
    (tmp_path / "marca.txt").write_text("ok")
    r = await shell.run("ls", cwd=str(tmp_path), timeout=10)
    assert "marca.txt" in r["out"]


async def test_comando_inexistente_no_explota():
    r = await shell.run("herramienta-que-no-existe-4bis", timeout=10)
    assert r["exit"] != 0
    assert shell.missing_tool_hint(r["out"])       # sugiere preguntar


async def test_cancelar_desde_afuera_tambien_mata_el_arbol(tmp_path):
    """El corte por tool-call cancela la corrutina, no la mata por timeout.

    Sin la rama `except CancelledError` el hijo sobrevivía al run: el
    `anyio.fail_after` del toolset cancela este await, el `except
    TimeoutError` no aplica y nadie llama a `_kill_tree`. Caso real
    (sample-app, 19/8/2026): 13 `node` de Playwright vivos horas después,
    tomando puertos y perfiles de Chromium.

    Sin comandos POSIX a propósito: el bug se ve en Windows, que es
    donde corre el relay.
    """
    marca = tmp_path / "sobrevivi.txt"
    # Sin comillas ni rutas absolutas: `cmd /s` se come la primera y la
    # última comilla del comando y lo rompe. Con `cwd` no hacen falta.
    cmd = ("ping -n 6 127.0.0.1 >nul && echo si> sobrevivi.txt"
           if shell.IS_WINDOWS else
           "sleep 5 && echo si > sobrevivi.txt")

    task = asyncio.create_task(shell.run(cmd, cwd=str(tmp_path), timeout=60))
    await asyncio.sleep(1.5)          # que el proceso ya esté arriba
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(6.0)          # más que el sleep del hijo
    assert not marca.exists(), "el proceso sobrevivió a la cancelación"


@pytest.mark.skipif(not shell.IS_WINDOWS, reason="el bug es del pipe de Windows")
async def test_un_nieto_con_el_pipe_no_cuelga_el_comando(tmp_path):
    """Causa #5: el hijo directo termina y un nieto se queda el stdout.

    `proc.wait()` de asyncio se resuelve cuando el proceso termino Y
    ADEMAS cerraron los pipes, asi que esperarlo es esperar al NIETO.
    Medido el 2026-08-31 en code-hero-rpg: el ejecutor levanto el dev
    server con `Start-Process npm.cmd run dev -RedirectStandardOutput`;
    PowerShell volvio al prompt en <1s, la tool espero su techo entero y
    el `_kill_tree` del timeout se llevo puesto el server. Los pasos que
    seguian eran "levantar Playwright" y "verificar en el browser": sin
    server el plan no podia terminar.

    El nieto dura MAS que el timeout a proposito: con `proc.wait()` esto
    da `timed_out=True` y `exit=-9` (y mata el proceso de fondo), que es
    justo el bug. Que no haya timeout ES la prueba de que sobrevivio:
    `_kill_tree` solo corre por timeout o cancelacion.
    """
    # Las dos redirecciones fuerzan `UseShellExecute=false`, que es lo que
    # hace que el nieto herede NUESTRO pipe. Sin ellas PowerShell abre una
    # consola nueva, no hereda nada y el bug no aparece.
    cmd = ("Start-Process -FilePath 'cmd.exe' "
           "-ArgumentList '/c','ping -n 25 127.0.0.1' "
           "-RedirectStandardOutput 'bg-out.log' "
           "-RedirectStandardError 'bg-err.log' -WindowStyle Hidden; "
           '"lanzado"')

    t0 = asyncio.get_running_loop().time()
    res = await shell.run(cmd, cwd=str(tmp_path), timeout=12.0,
                          shell_kind="powershell")
    tardo = asyncio.get_running_loop().time() - t0

    assert tardo < 8.0, f"espero al nieto, no al hijo: tardo {tardo:.1f}s"
    assert res["timed_out"] is False, "mato el arbol: se lleva el server puesto"
    assert res["exit"] == 0, res["out"]
    assert "lanzado" in res["out"]


def test_hint_de_instalacion_solo_cuando_aplica():
    assert shell.missing_tool_hint("bash: pwsh: command not found")
    assert shell.missing_tool_hint("ModuleNotFoundError: No module named 'x'")
    assert not shell.missing_tool_hint("2 tests failed\n(exit=1)")
    assert not shell.missing_tool_hint("")


def test_una_ruta_inexistente_no_se_lee_como_herramienta_ausente():
    """Una ruta mal escrita y un binario ausente dan el MISMO mensaje.

    Caso real (chat `61572557`, nodo de INVENTORYDEMO, 1/9): el modelo invocó
    `C:\\Users\\demo\\.dotnet\\dotnet.exe` —que no existe; la real es
    `C:\\Program Files\\dotnet\\dotnet.exe`—, y el hint lo mandó a pedirle
    al humano que instalara el SDK de .NET 10. En esa máquina hay cinco
    SDKs y `dotnet build` corre en 7 segundos: el grafo se frenó para
    pedir algo que ya estaba.
    """
    salida_ruta = ('"\\"C:\\Users\\demo\\.dotnet\\dotnet.exe\\"" no se '
                   "reconoce como un comando interno o externo,\n(exit=1)")
    hint = shell.missing_tool_hint(salida_ruta)
    assert "RUTA INEXISTENTE" in hint
    assert "ask_human" not in hint
    assert "instal" not in hint.split("[RUTA INEXISTENTE]")[1].lower() \
        or "No preguntes por una instalación" in hint

    # El binario pelado ausente SIGUE mandando a preguntar: ahí el hint
    # está bien y es lo que evita que el experto instale por su cuenta.
    salida_binaria = ('"tail" no se reconoce como un comando interno o '
                      "externo,\n(exit=1)")
    assert "ask_human" in shell.missing_tool_hint(salida_binaria)
    assert "ask_human" in shell.missing_tool_hint("bash: dotnet: command not found")


def test_powershell_va_sin_perfil_y_no_interactivo():
    """Los dos flags que evitan el cuelgue clásico de Windows."""
    argv, kind = shell.build_argv("Get-ChildItem", shell_kind="powershell")
    assert kind == "powershell"
    assert "-NoProfile" in argv and "-NonInteractive" in argv
    assert argv[-2] == "-Command" and argv[-1].endswith("\nGet-ChildItem")


def test_auto_detecta_powershell_por_el_comando():
    assert shell._looks_like_powershell("Get-ChildItem | Where-Object {$_}")
    assert shell._looks_like_powershell(".\\deploy.ps1")
    assert not shell._looks_like_powershell("git status")


def test_un_pipeline_unix_no_va_a_cmd():
    """El fallo más repetido de la bitácora: 22 comandos con exit 255.

    En Windows el `auto` elegía SOLO entre PowerShell y cmd, así que
    `git ls-files | head -100` terminaba en `cmd.exe`, que no tiene
    `head`. No fallaba a veces: fallaba siempre, y el modelo escribe
    pipelines unix porque es lo que sabe. La máquina tiene bash (viene
    con Git para Windows), que es donde ese comando corre.
    """
    for cmd in ('git ls-files "*.py" | head -100',
                'git ls-files | grep -E "x" | wc -l',
                'git status --short 2>&1 | head -200',
                'cat x 2>/dev/null'):
        assert shell._looks_like_sh(cmd), cmd


def test_los_alias_de_powershell_no_son_senal_de_unix():
    """`sort`, `ls` y `cat` existen como alias en PowerShell, así que
    verlos no prueba nada. Solo cuentan los que no trae NINGÚN shell de
    Windows — si no, mandaríamos a bash comandos con rutas `C:\\...`,
    donde las barras invertidas son escapes."""
    assert not shell._looks_like_sh("ls -la")
    assert not shell._looks_like_sh("sort archivo.txt")
    assert not shell._looks_like_sh("dotnet build")
    # "cut" adentro de una ruta no es una invocación de `cut`.
    assert not shell._looks_like_sh(r"dir C:\Users\uncut\shortcut")


def test_tail_no_va_a_cmd():
    """El fallo más caro que quedaba: 18 en 14 días, todos iguales.

    `<build> 2>&1 | tail -40` es como el modelo pide el FINAL de una
    compilación —donde está el veredicto—, y `tail` no estaba en la
    lista, así que caía en `cmd.exe`, que no lo tiene.
    """
    for cmd in ("docker compose up -d --build 2>&1 | tail -40",
                "dotnet build 2>&1 | tail -n 30",
                "sleep 12",
                "uname -a"):
        _, kind = shell.build_argv(cmd)
        assert kind == "sh", (cmd, kind)


def test_cualquier_cmdlet_va_a_powershell_no_solo_los_de_la_lista():
    """La lista a mano tenía `Select-Object` pero no `Select-String`.

    16 comandos muertos en 14 días por eso: los cmdlets que no estaban
    enumerados caían en `cmd.exe`, que no tiene ninguno. El patrón
    `Verbo-Sustantivo` los cubre a todos sin lista que mantener.
    """
    for cmd in ('Select-String -Path x.log -Pattern "error"',
                'docker ps | Out-String',
                'npm run dev 2>&1 | Tee-Object -FilePath x.log',
                'curl.exe -s http://localhost:8080/health | Out-Null'):
        _, kind = shell.build_argv(cmd)
        assert kind == "powershell", (cmd, kind)


def test_señal_windows_explicita_sigue_yendo_a_cmd():
    """La ruta Windows con backslash es señal EXPLÍCITA de cmd
    (`_looks_like_cmd`): sigue yendo a cmd después de invertir el
    default."""
    _, kind = shell.build_argv(r'dotnet build C:\repo\app.sln --nologo')
    assert kind == "cmd"


def test_comandos_sin_senal_ahora_van_a_sh_por_el_nuevo_default():
    """2026-09-02: se invirtió el default de `build_argv` (medido sobre
    2170 comandos reales: `cmd` fallaba 45.9% contra 32.9% de `sh`, y
    era el default). Estos tres — sin `curl.exe`/`git`/`npm run build`
    ninguna sintaxis atada a un shell en particular— antes caían a cmd
    por default y ahora caen a sh. El riesgo histórico era `curl.exe`
    con `-w "%{http_code}"`: `VAR_CMD` exige letra/underscore después
    del `%` a propósito para NO confundir ese formato con `%VAR%`.
    """
    for cmd in ('curl.exe -s -o NUL -w "%{http_code}" http://localhost:5000/health',
                'git status --short',
                'npm run build'):
        _, kind = shell.build_argv(cmd)
        assert kind == "sh", (cmd, kind)


@pytest.mark.skipif(not shell.IS_WINDOWS, reason="el enredo es de Windows")
def test_el_bash_elegido_no_es_el_de_wsl():
    """`shutil.which("bash")` en Windows devuelve WSL, no Git Bash.

    Causa de raíz medida el 2/9: `C:\\Windows\\System32\\bash.exe` es WSL
    —otro sistema operativo, otro filesystem (`/mnt/c` en vez de `/c`) y
    sin las herramientas de la máquina—, así que TODO lo que el `auto`
    mandaba a `sh` corría ahí. `dotnet --version` andaba por cmd y
    `dotnet --version | tail -1` contestaba `command not found`.
    """
    elegido = shell.bash_exe()
    assert elegido, "esta máquina tiene Git Bash: algo lo dejó de encontrar"
    assert "system32" not in elegido.lower(), f"eligió WSL: {elegido}"
    argv, kind = shell.build_argv("git status | head -3")
    assert kind == "sh"
    assert "system32" not in argv[0].lower()


@pytest.mark.skipif(not shell.IS_WINDOWS, reason="el enredo es de Windows")
@pytest.mark.skipif(not shutil.which("gh"), reason="necesita `gh` instalado")
async def test_bash_encuentra_herramientas_de_la_maquina():
    """MSYS2 ya traduce el PATH de Windows solo al arrancar bash real
    (Git Bash, no el `bash.exe` de WSL que elegía `bash_exe()` hasta el
    2/9): no hace falta convertirlo a mano. `gh` no viene con Git, así
    que si esto pasa es porque bash está viendo el PATH de la máquina."""
    r = await shell.run("command -v gh", shell_kind="sh", timeout=30)
    assert r["shell"] == "sh"
    assert r["out"].strip(), r["out"]


@pytest.mark.skipif(not shell.IS_WINDOWS, reason="el enredo es de Windows")
async def test_un_hijo_windows_lanzado_desde_bash_ve_el_path_completo():
    """El bug del 2/9: `path_para_bash` convertía el PATH a formato POSIX
    (":") para que bash lo lea, pero un hijo WINDOWS (`python.exe`)
    lanzado DESDE ese bash hereda ese mismo string — y en Windows
    `os.pathsep` es ";", no ":", así que veía el PATH entero como UNA
    sola entrada. Medido: pasaba de ~73 entradas a 4. Este es el que no
    puede volver a romperse."""
    r = await shell.run(
        'python -c "import os; print(len(os.environ[\'PATH\'].split(os.pathsep)))"',
        shell_kind="sh", timeout=30)
    assert r["shell"] == "sh"
    n = int(r["out"].splitlines()[0].strip())
    assert n > 10, (n, r["out"])


@pytest.mark.skipif(not shell.IS_WINDOWS, reason="el enredo es de Windows")
async def test_un_binario_de_windows_se_encuentra_dentro_de_un_pipeline():
    """La prueba de fuego: `<herramienta windows> | <filtro unix>`.

    Es la forma que el ruteo a bash venía a habilitar y la que estuvo
    rota: 49 de los 58 comandos rechazados del 1/9 eran esto.
    """
    r = await shell.run("git --version | tail -1", timeout=60)
    assert r["shell"] == "sh"
    assert "command not found" not in r["out"], r["out"]
    assert "git version" in r["out"], r["out"]


def test_alias_unix_no_van_a_cmd_que_es_el_unico_que_no_los_tiene():
    """`ls`, `pwd`, `cat` y compañía: PowerShell los tiene como alias,
    `cmd.exe` no, y el `auto` los mandaba justo a cmd. Medido: 15
    comandos de esta forma en 14 días y los 15 fallaron — no hay ninguno
    que hoy funcione, así que moverlos no puede romper nada.
    """
    for cmd in ("pwd && ls",
                "cat Tests/Tests.csproj",
                "ls Service/Services/INVENTORYDEMO/Courier/",
                "git log --oneline -3 && rm .git/COMMIT_EDITMSG.tmp"):
        _, kind = shell.build_argv(cmd)
        assert kind == "sh", (cmd, kind)


def test_alias_unix_con_rutas_windows_no_se_manda_a_bash():
    """La guarda del backslash: en bash `C:\\Users\\x` pierde las barras
    (son escapes), así que ahí NO se toca el ruteo."""
    for cmd in (r"ls C:\Users\demo\source",
                r"cat C:\Users\demo\.relay\notes\x.md"):
        _, kind = shell.build_argv(cmd)
        assert kind != "sh", (cmd, kind)


def test_powershell_le_gana_a_la_pinta_de_unix():
    """Un cmdlet con un `head` en el medio sigue siendo PowerShell: el
    orden de las heurísticas no cambió."""
    _, kind = shell.build_argv("Get-Content x | Select-Object -First 3")
    assert kind == "powershell"


def test_sh_no_carga_el_profile():
    """`bash -lc` carga el profile: mismo problema que el $PROFILE."""
    argv, kind = shell.build_argv("ls", shell_kind="sh")
    assert kind == "sh"
    if argv[0].endswith("bash"):
        assert "--noprofile" in argv and "--norc" in argv
    assert "-l" not in argv


def test_comillas_simples_posix_se_mandan_a_bash():
    """`gh api graphql -F query='{ ... }'`: en cmd.exe la comilla simple
    no agrupa, así que el string se parte en tokens (caso real: `gh`
    contestando "accepts 1 arg(s), received 11"). En bash sí agrupa."""
    for cmd in (
        "gh api graphql -F query='{ organization(login:\"AuroraDemo\") "
        "{ viewerCanCreateProjects name } viewer { login } }'",
        "git ls-files | head -5",
    ):
        _, kind = shell.build_argv(cmd)
        assert kind == "sh", (cmd, kind)


def test_apostrofo_suelto_no_es_quoting():
    """`echo don't` tiene UNA comilla simple: es texto, no quoting.

    Prueba la heurística directo, no el resultado de `build_argv`: desde
    que el default se invirtió (2026-09-02), `echo don't` sin ninguna
    señal cae a `sh` igual —por default, no por esta heurística— así
    que afirmar sobre `kind` ya no distinguiría un guard roto de un
    default cambiado.
    """
    assert not shell._looks_like_single_quoted("echo don't")


def test_comillas_simples_con_ruta_windows_no_se_manda_a_bash():
    """Misma guarda de backslash que el resto de las heurísticas unix."""
    _, kind = shell.build_argv(r"dir C:\Users\demo")
    assert kind != "sh"


def test_powershell_le_gana_a_comillas_simples():
    """Un cmdlet con comillas simples adentro sigue siendo PowerShell."""
    _, kind = shell.build_argv("Get-ChildItem -Filter '*.cs'")
    assert kind == "powershell"


def test_sintaxis_posix_se_manda_a_bash():
    """`$(...)`, `${...}` y backticks: sustitución/expansión que no
    existe en cmd.exe. Caso real: `cygpath` dentro de un `$(...)` daba
    "El sistema no puede encontrar la ruta especificada." porque
    ninguna heurística por nombre de programa matcheaba."""
    for cmd in (
        'cd "/c/..." && WIN_REPO_ROOT="$(cygpath -w "$(pwd)")" python '
        'scripts/create_project_and_link.py AuroraDemo Example_Limits '
        '"Kanban" organization',
        "echo ${HOME}",
        "echo `date`",
    ):
        _, kind = shell.build_argv(cmd)
        assert kind == "sh", (cmd, kind)


def test_powershell_le_gana_a_sintaxis_posix():
    """`$(...)` también es válido en PowerShell: tiene que seguir
    ganando sobre la heurística de sintaxis unix."""
    _, kind = shell.build_argv('Write-Host "$(Get-Date)"')
    assert kind == "powershell"


def test_sintaxis_posix_con_ruta_windows_no_se_manda_a_bash():
    """Mismo guard de backslash que comillas simples y alias unix."""
    _, kind = shell.build_argv(r"echo $(dir) C:\Users\demo")
    assert kind != "sh"


def test_comando_sin_ninguna_señal_va_a_sh_por_el_nuevo_default():
    """`echo hola`: sin señal unix NI señal cmd, cae al nuevo default
    (sh) — ver `_looks_like_cmd` y el cambio de default en `build_argv`."""
    _, kind = shell.build_argv("echo hola")
    assert kind == "sh"


def test_docker_sin_senal_windows_ahora_va_a_sh():
    """2026-09-02, el cambio del día: `docker compose`/`docker ps` no
    traen ninguna señal de Windows y hoy caían a cmd por default —
    donde `cmd` falla más (45.9% medido) que `sh` (32.9%)."""
    for cmd in ("docker compose up -d --build 2>&1", "docker ps -a"):
        _, kind = shell.build_argv(cmd)
        assert kind == "sh", (cmd, kind)


def test_builtin_de_cmd_manda_a_cmd():
    """`dir` es builtin de cmd.exe y no existe en bash."""
    _, kind = shell.build_argv(r"dir TestResults\coverage /b")
    assert kind == "cmd"


def test_ruta_windows_con_backslash_manda_a_cmd():
    _, kind = shell.build_argv(r'cmd /c "C:\Program Files\Web\Web.exe --urls http://+:80"')
    assert kind == "cmd"


def test_var_cmd_manda_a_cmd():
    """`%VAR%` es expansión de cmd.exe, no existe en bash."""
    _, kind = shell.build_argv("echo SHELL=%SHELL%")
    assert kind == "cmd"


def test_script_bat_o_cmd_manda_a_cmd():
    _, kind = shell.build_argv(r'"C:\Users\demo\scripts\login_a.cmd"')
    assert kind == "cmd"


def test_escape_de_barra_no_es_ruta_windows():
    """El falso positivo que más importa: `\\n` y `\\t` son escapes de
    shell válidos en bash (curl, docker --format), NO rutas Windows.
    `_looks_like_cmd` no puede confundirlos o rompe comandos que hoy
    andan."""
    for cmd in (
        r'curl -sS -w "\nHTTP %{http_code}\n" http://localhost:5000',
        r'docker ps --format "table {{.Names}}\t{{.ID}}"',
    ):
        _, kind = shell.build_argv(cmd)
        assert kind == "sh", (cmd, kind)


def test_flag_estilo_windows_manda_a_cmd():
    """MSYS2/Git Bash convierte un argumento que arranca con `/` en
    ruta Windows ANTES de pasarlo al exe: `ipconfig /all` bajo bash
    daba "línea de comandos desconocida o incorrecta", y `tasklist
    /FI "..."` recibía el flag mangleado como una ruta `C:/...`."""
    for cmd in (
        "ipconfig /all",
        'tasklist /FI "IMAGENAME eq nonexistent"',
        "date /t",
        'findstr /R "algo" f.txt',
        "taskkill /PID 1234 /F",
    ):
        _, kind = shell.build_argv(cmd)
        assert kind == "cmd", (cmd, kind)


def test_ruta_unix_con_segunda_barra_no_es_flag_windows():
    """`/tmp`, `/proc` (con más ruta atrás) y una URL no matchean el
    flag estilo Windows: la heurística exige que la barra venga
    precedida de espacio/inicio Y no tenga otra barra después."""
    for cmd in ("ls /tmp",
                "cd /tmp && pwd",
                "curl -s https://api.github.com/orgs/x"):
        _, kind = shell.build_argv(cmd)
        assert kind == "sh", (cmd, kind)


def test_raiz_unix_de_un_solo_segmento_no_es_flag_windows():
    """El caso que se escapaba: `/proc`, `/opt`, `/root` sin más ruta
    atrás son "espacio + / + letras + espacio", igual de válido que un
    flag `/all` para el regex — hace falta excluirlos por nombre.
    `find` es el caso real: no está en `_SOLO_UNIX`/`_ALIAS_UNIX` a
    propósito (`cmd.exe` tiene su propio builtin `find`, otra sintaxis),
    así que sin esta exclusión no hay heurística unix que lo proteja."""
    for cmd in ("find / -path /proc -prune -o -name x -print",
                "cp a.txt /opt",
                "ls /root/.4bis"):
        _, kind = shell.build_argv(cmd)
        assert kind == "sh", (cmd, kind)


def test_docker_sin_flag_windows_sigue_en_sh():
    _, kind = shell.build_argv("docker compose up -d --build")
    assert kind == "sh"


@pytest.mark.skipif(not shell.IS_WINDOWS, reason="el enredo es de Windows")
async def test_cwd_posix_ya_no_tira_winerror_267(tmp_path):
    """El bug real: `cwd='/c/...'` (lo que el modelo escribe, porque es
    lo que ve en la salida de bash) hacía morir el proceso ENTERO con
    `[WinError 267] El nombre de directorio no es válido`, sin importar
    a qué intérprete rutee. Bloqueó un proyecto 100 minutos: 7 intentos,
    los 7 con el mismo error, terminó en una pregunta al humano."""
    posix_cwd = "/" + tmp_path.drive[0].lower() + str(tmp_path)[2:].replace("\\", "/")
    for kind in ("cmd", "sh"):
        r = await shell.run("echo hola", cwd=posix_cwd, shell_kind=kind, timeout=30)
        assert r["exit"] == 0, (kind, r["out"])
        assert "267" not in r["out"], (kind, r["out"])


def test_cwd_windows_sigue_funcionando(tmp_path):
    """No romper lo que ya andaba: forma Windows pasa intacta."""
    assert shell._cwd_para_windows(str(tmp_path)) == str(tmp_path)
    assert shell._cwd_para_windows("C:/Users/x") == "C:/Users/x"


@pytest.mark.skipif(not shell.IS_WINDOWS, reason="el enredo es de Windows")
async def test_cwd_posix_inexistente_da_error_claro_no_winerror_267():
    r = await shell.run("echo hola", cwd="/c/ruta/que/no/existe-de-verdad",
                         timeout=30)
    assert r["exit"] != 0
    assert "cwd no existe" in r["out"]
    assert "267" not in r["out"]


def test_convierte_solo_drive_posix_real():
    """`/usr/local` no es un drive (dos letras): queda intacto."""
    assert shell._cwd_para_windows("/usr/local") == "/usr/local"
    assert shell._cwd_para_windows("/c/Users/demo") == "C:/Users/demo"
    assert shell._cwd_para_windows("/c") == "C:/"


# ---------- 1.b el `cwd` relativo de la tool (2026-09-04) ----------


async def test_cwd_relativo_se_resuelve_contra_el_repo(tmp_path):
    """El harness le MENTÍA al modelo. La tool promete "vacío = la raíz
    del repo" y `read_file` acepta rutas relativas, pero el call site
    hacía `cwd=(cwd or _repo or None)`: con `cwd='frontend'` el `or`
    cortocircuita, el repo nunca es base, y el shell contestaba
    `cwd no existe: 'frontend'` sobre un directorio que sí existe.

    Medido: el modelo lo desmiente con `Test-Path` y reintenta con ruta
    absoluta — 2 de 8 tool calls y ~6 s por comando. En el run
    `bc5195eb` (2/9) terminó en una pregunta al humano: "no es un
    problema del proyecto, es del harness".
    """
    (tmp_path / "frontend").mkdir()
    r = await shell.run("echo ok", cwd="frontend", base=str(tmp_path),
                        timeout=60)
    assert r["exit"] == 0, r["out"]          # antes: -1, "cwd no existe"
    assert "no existe" not in r["out"], r["out"]


def test_cwd_relativo_apunta_adentro_del_repo(tmp_path):
    (tmp_path / "frontend").mkdir()
    cwd, err = shell._resolver_cwd("frontend", str(tmp_path))
    assert err == ""
    assert Path(cwd) == (tmp_path / "frontend").resolve()


def test_cwd_vacio_sigue_siendo_la_raiz_del_repo(tmp_path):
    """Lo que el docstring de la tool promete, y lo que el `or` ya hacía
    bien: no romperlo al sacarlo del call site."""
    assert shell._resolver_cwd("", str(tmp_path)) == (str(tmp_path), "")
    assert shell._resolver_cwd(None, str(tmp_path)) == (str(tmp_path), "")
    assert shell._resolver_cwd("", "") == (None, "")


def test_cwd_relativo_no_puede_salirse_del_repo(tmp_path):
    """`cwd` es entrada del LLM y el sandbox de `files.Permisos` NO lo
    cubre. Resolver relativos contra la raíz convierte un `'../../otro'`
    —que antes fallaba— en una salida del repo: se rechaza."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "otro").mkdir()
    cwd, err = shell._resolver_cwd("../otro", str(repo))
    assert cwd is None
    assert "se sale del repo" in err


async def test_run_no_ejecuta_nada_con_un_cwd_que_se_escapa(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "otro").mkdir()
    r = await shell.run("echo ok", cwd="../otro", base=str(repo), timeout=30)
    assert r["exit"] == -1
    assert "se sale del repo" in r["out"]


async def test_lanzar_tampoco_larga_nada_con_un_cwd_que_se_escapa(tmp_path):
    """El call site de background compartía la expresión rota, así que
    comparte el guard."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "otro").mkdir()
    r = await shell.lanzar("echo ok", cwd="../otro", base=str(repo))
    assert r["pid"] == 0
    assert "se sale del repo" in r["out"]


def test_cwd_absoluto_fuera_del_repo_sigue_permitido(tmp_path):
    """A propósito NO se toca: hoy se permite, y ampliar o cerrar el
    sandbox de `shell` es otra decisión. El guard nuevo solo alcanza al
    caso que este parche introduce (relativos contra la raíz)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    afuera = tmp_path / "otro"
    afuera.mkdir()
    cwd, err = shell._resolver_cwd(str(afuera), str(repo))
    assert err == ""
    assert Path(cwd) == afuera


def test_entorno_no_interactivo_completo():
    env = shell._env_for_run()
    # Las herramientas que preguntan en medio de un script.
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["PIP_NO_INPUT"] == "1"
    assert env["npm_config_yes"] == "true"
    assert env["DEBIAN_FRONTEND"] == "noninteractive"
    assert env["CI"] == "1"


def test_env_extra_pisa_al_default():
    env = shell._env_for_run({"CI": "0", "MIO": "1"})
    assert env["CI"] == "0" and env["MIO"] == "1"


# ---------- 2. un solo shell: el del wrapper se oculta ----------


async def test_hide_tools_saca_run_shell():
    """Con los dos visibles, el modelo elegía el roto la mitad de las veces."""

    class _Fake:
        label = "fake"

        async def get_tools(self, ctx):
            return {"run_shell": object(), "read_file": object()}

    oculto = experts.HideToolsToolset(
        wrapped=_Fake(), hidden=frozenset({"run_shell"}))
    tools = await oculto.get_tools(None)
    assert "run_shell" not in tools
    assert "read_file" in tools


async def test_hide_tools_sin_lista_no_toca_nada():
    class _Fake:
        label = "fake"

        async def get_tools(self, ctx):
            return {"run_shell": object()}

    pasa = experts.HideToolsToolset(wrapped=_Fake())
    assert "run_shell" in await pasa.get_tools(None)


# ---------- 3. ask_human: anota y termina el turno ----------


async def test_ask_human_registra_y_manda_terminar(db):
    """El contrato: NO bloquea esperando al humano — cierra el turno."""
    from pydantic_ai.models.test import TestModel
    from unittest.mock import patch

    capturado = {}
    _RealAgent = experts.Agent

    class _SpyAgent:
        def __init__(self, *a, **kw):
            capturado["tools"] = [
                t for ts in (kw.get("toolsets") or []) for t in [ts]]
            capturado["kw"] = kw
            self._inner = _RealAgent(*a, **kw)

        def __getattr__(self, n):
            return getattr(self._inner, n)

    proj = {"slug": "demo", "repo_path": str(tempfile.mkdtemp()),
            "system_prompt": "p", "mcp_servers": [], "native_tools": [],
            "defaults_json": {}}
    with patch.object(experts, "Agent", _SpyAgent), \
         patch.object(experts, "build_model", lambda s: TestModel(call_tools=[])), \
         patch.object(experts, "cbm_binary_path", lambda: None):
        await experts.run_expert(
            proj, "hola", db=db, model_override="minimax:MiniMax-M3",
            chat_id="chat-1", conversation_id="conv-1")

    # La tool existe en el agente…
    nombres = _nombres_de_tools(capturado["kw"])
    assert "ask_human" in nombres
    assert "shell" in nombres


def _nombres_de_tools(kw) -> set:
    nombres = set()
    for ts in kw.get("toolsets") or []:
        inner = getattr(ts, "wrapped", ts)
        for t in getattr(inner, "tools", []) or []:
            nombres.add(getattr(t, "name", None) or getattr(t, "__name__", ""))
        # FunctionToolset guarda las tools en un dict
        d = getattr(inner, "_tools", None) or getattr(inner, "tools", None)
        if isinstance(d, dict):
            nombres |= set(d.keys())
    return nombres


async def test_pregunta_persistida_y_respondible(db):
    await db.create_expert_question(
        "q_x", "chat-1",
        json.dumps({"title": "¿instalo pwsh?", "detail": "falta en la máquina",
                    "options": [{"key": "o0", "label": "instalalo"},
                                {"key": "o1", "label": "seguí sin eso"}]}),
        conversation_id="conv-1", project_slug="demo", kind="install")

    abiertas = await db.list_expert_questions(conversation_id="conv-1")
    assert len(abiertas) == 1 and abiertas[0]["kind"] == "install"

    assert await db.answer_expert_question("q_x", json.dumps({"choice": "o0"}))
    # Idempotente: el segundo click no pisa la respuesta del primero.
    assert not await db.answer_expert_question("q_x", json.dumps({"choice": "o1"}))
    assert (await db.get_expert_question("q_x"))["status"] == "answered"
    assert await db.list_expert_questions(conversation_id="conv-1") == []


def test_clasifica_las_preguntas_de_instalacion():
    assert experts._huele_a_instalacion("¿instalo pwsh?", "")
    assert experts._huele_a_instalacion("", "hay que correr pip install rich")
    assert not experts._huele_a_instalacion("¿borro la rama vieja?", "")


# ---------- 4. la pregunta llega al humano en la respuesta ----------


def test_render_de_la_pregunta_para_el_chat():
    from relay import server

    bloque = server._render_pregunta({
        "id": "q_ab12", "kind": "install",
        "question_json": json.dumps({
            "title": "¿instalo PowerShell 7?",
            "detail": "El deploy usa cmdlets que pwsh 5 no tiene.",
            "options": [{"key": "o0", "label": "instalalo"},
                        {"key": "o1", "label": "seguí sin eso"}]}),
    })
    assert "📦" in bloque                       # install se pinta distinto
    assert "¿instalo PowerShell 7?" in bloque
    assert "El deploy usa cmdlets" in bloque
    assert "instalalo" in bloque and "seguí sin eso" in bloque
    assert "q_ab12" in bloque                   # para responder por API


def test_render_sin_opciones_pide_texto_libre():
    from relay import server

    bloque = server._render_pregunta({
        "id": "q_1", "kind": "text",
        "question_json": json.dumps({"title": "¿qué rama uso?"})})
    assert "❓" in bloque
    assert "Respondé en el chat" in bloque


def test_render_tolera_json_roto():
    from relay import server

    bloque = server._render_pregunta({"id": "q_1", "question_json": "{roto"})
    assert "decisión" in bloque         # cae al título por default


# ---------- 4. una decisión por vez (2026-08-16) ----------
#
# Bug reportado: *"la decisión que muestra la UI se comenzó a repetir"*.
# Nada jubilaba las preguntas viejas: si el humano no contestaba y el
# experto volvía a preguntar en el turno siguiente, quedaban dos (o
# cinco) abiertas a la vez. Peor que el ruido: contestar una vieja
# inyectaba como turno siguiente una decisión sobre algo que el hilo ya
# había dejado atrás.


async def test_una_pregunta_nueva_jubila_la_anterior(db):
    await db.create_expert_question(
        "q_vieja01", "chat-1", json.dumps({"title": "¿instalo pwsh?"}),
        conversation_id="conv-1")
    jubiladas = await db.create_expert_question(
        "q_nueva01", "chat-2", json.dumps({"title": "¿uso .NET 10?"}),
        conversation_id="conv-1")

    assert jubiladas == 1
    assert (await db.get_expert_question("q_vieja01"))["status"] == "superseded"
    assert (await db.get_expert_question("q_nueva01"))["status"] == "open"

    abiertas = await db.list_expert_questions(conversation_id="conv-1")
    assert [q["id"] for q in abiertas] == ["q_nueva01"]


async def test_no_jubila_la_pregunta_de_un_nodo_de_grafo_que_espera(db):
    """En un grafo los nodos preguntan EN PARALELO sobre la misma
    conversación, y jubilar por conversación mataba la pregunta del
    vecino: la tarea quedaba en `esperando_humano` para siempre,
    incontestable desde la UI (`answer_expert_question` exige `open`) e
    inalcanzable con `/graphs/{id}/resume`.

    Medido el 31/8: 10 tareas trabadas así en 3 grafos, una desde hacía
    una semana, arrastrando a sus dependientes.
    """
    await db.create_task_graph(
        "g_test", "objetivo", conversation_id="conv-1", project_slug="demo",
        tareas=[{"id": "t_a", "titulo": "nodo A"},
                {"id": "t_b", "titulo": "nodo B"}])
    # El nodo A preguntó y SIGUE CORRIENDO: la tarea recién pasa a
    # `esperando_humano` cuando el run termina, y la pregunta nace a
    # mitad del run. Esa ventana es la que se comió la pregunta del nodo
    # de EmailSender el 1/9, nueve segundos después de hacerla.
    await db.create_expert_question(
        "q_nodo_a", "chat-a", json.dumps({"title": "¿stack de reportes?"}),
        conversation_id="conv-1")
    await db.update_task("t_a", estado="corriendo", chat_id="chat-a")

    # Ahora pregunta el nodo B, del mismo grafo y la misma conversación.
    jubiladas = await db.create_expert_question(
        "q_nodo_b", "chat-b", json.dumps({"title": "¿canal de notif?"}),
        conversation_id="conv-1")

    assert jubiladas == 0
    assert (await db.get_expert_question("q_nodo_a"))["status"] == "open"
    assert (await db.get_expert_question("q_nodo_b"))["status"] == "open"
    # Las dos siguen contestables: son dos decisiones distintas.
    abiertas = {q["id"] for q in await db.list_expert_questions(
        conversation_id="conv-1")}
    assert abiertas == {"q_nodo_a", "q_nodo_b"}


async def test_el_nodo_que_re_pregunta_si_jubila_la_suya(db):
    """El guard no puede dejar dos preguntas abiertas del MISMO nodo.

    Cuando la tarea se re-ejecuta deja de estar en `esperando_humano`,
    así que su pregunta vieja vuelve a ser jubilable — que es justo lo
    que tiene que pasar."""
    await db.create_task_graph(
        "g_test2", "objetivo", conversation_id="conv-2", project_slug="demo",
        tareas=[{"id": "t_solo", "titulo": "nodo"}])
    await db.create_expert_question(
        "q_v1", "chat-v1", json.dumps({"title": "primera"}),
        conversation_id="conv-2")
    await db.update_task("t_solo", estado="esperando_humano", chat_id="chat-v1")
    # El humano destraba la tarea: vuelve a correr y pregunta de nuevo.
    await db.update_task("t_solo", estado="corriendo", chat_id="chat-v2")
    jubiladas = await db.create_expert_question(
        "q_v2", "chat-v2", json.dumps({"title": "segunda"}),
        conversation_id="conv-2")

    assert jubiladas == 1
    assert (await db.get_expert_question("q_v1"))["status"] == "superseded"


async def test_una_jubilada_ya_no_se_puede_contestar(db):
    """Contestarla inyectaría una decisión sobre algo que ya no está en juego."""
    await db.create_expert_question(
        "q_vieja02", "chat-1", json.dumps({"title": "vieja"}),
        conversation_id="conv-2")
    await db.create_expert_question(
        "q_nueva02", "chat-2", json.dumps({"title": "nueva"}),
        conversation_id="conv-2")

    assert await db.answer_expert_question("q_vieja02", "{}") is False
    assert await db.answer_expert_question("q_nueva02", "{}") is True


async def test_no_jubila_las_de_otra_conversacion(db):
    """Dos hilos del mismo proyecto no se pisan las decisiones."""
    await db.create_expert_question(
        "q_hiloA", "chat-1", json.dumps({"title": "A"}),
        conversation_id="conv-A")
    await db.create_expert_question(
        "q_hiloB", "chat-2", json.dumps({"title": "B"}),
        conversation_id="conv-B")

    assert (await db.get_expert_question("q_hiloA"))["status"] == "open"
    assert (await db.get_expert_question("q_hiloB"))["status"] == "open"


async def test_no_jubila_una_ya_contestada(db):
    """Lo contestado es historia: no se re-escribe su estado."""
    await db.create_expert_question(
        "q_hecha", "chat-1", json.dumps({"title": "vieja"}),
        conversation_id="conv-3")
    await db.answer_expert_question("q_hecha", json.dumps({"label": "sí"}))
    await db.create_expert_question(
        "q_otra", "chat-2", json.dumps({"title": "nueva"}),
        conversation_id="conv-3")

    assert (await db.get_expert_question("q_hecha"))["status"] == "answered"


async def test_sin_conversacion_jubila_por_run(db):
    """Un run suelto (sin hilo) igual no acumula decisiones."""
    await db.create_expert_question(
        "q_r1", "chat-solo", json.dumps({"title": "una"}))
    await db.create_expert_question(
        "q_r2", "chat-solo", json.dumps({"title": "otra"}))

    assert (await db.get_expert_question("q_r1"))["status"] == "superseded"
    assert (await db.get_expert_question("q_r2"))["status"] == "open"


# ---------- background: la capacidad que faltaba (2026-08-24) ----------
#
# `run()` siempre espera y al vencer el timeout mata el árbol, así que un
# dev server era imposible de dejar arriba. Medido en sample-app: el ejecutor
# levantó Vite en foreground, la tool cortó a los 180s y el `_kill_tree` se
# llevó el server; el modelo lo diagnosticó solo ("it was killed when the
# timeout happened. Let me start it in the background this time") y su
# segundo intento colgó igual, porque no había forma de hacerlo. Sin server
# no hay capturas: el pedido del manual no podía terminar nunca.


def _server_que_no_termina(tmp_path: Path) -> str:
    """Un proceso que imprime y se queda vivo, como un dev server."""
    p = tmp_path / "server.py"
    p.write_text("import time\nprint('escuchando', flush=True)\ntime.sleep(600)\n",
                 encoding="utf-8")
    return f"{sys.executable} {p}"


async def test_un_server_en_foreground_se_corta_y_lo_dice():
    """El contrapeso: sin `background` esto es exactamente lo que pasaba."""
    import tempfile as _tf
    with _tf.TemporaryDirectory() as d:
        cmd = _server_que_no_termina(Path(d))
        r = await shell.run(cmd, timeout=3)
    assert r["timed_out"] is True
    # Y el mensaje manda al camino correcto en vez de dejar adivinando.
    assert "background" in r["out"]


async def test_background_vuelve_enseguida_y_el_proceso_queda_vivo():
    import subprocess as _sp
    import tempfile as _tf
    with _tf.TemporaryDirectory() as d:
        cmd = _server_que_no_termina(Path(d))
        t0 = asyncio.get_event_loop().time()
        r = await shell.lanzar(cmd)
        tardo = asyncio.get_event_loop().time() - t0
        try:
            # Vuelve en el margen del chequeo de "no murió", no en 600s.
            assert tardo < 10, f"esperó al proceso: {tardo:.1f}s"
            assert r["pid"] > 0
            assert r["log"] in r["out"], "no dice dónde está el log"
            assert "pid=" in r["out"]
            # Sigue vivo cuando la tool devuelve: eso es todo el punto.
            assert _sp.Popen is not None
            await asyncio.sleep(1.0)
            assert Path(r["log"]).exists()
            assert "escuchando" in Path(r["log"]).read_text(
                encoding="utf-8", errors="replace"), \
                "el log quedó vacío: sin él no hay forma de saber si levantó"
        finally:
            if r["pid"]:
                with __import__("contextlib").suppress(Exception):
                    _sp.run(["taskkill", "/T", "/F", "/PID", str(r["pid"])]
                            if sys.platform == "win32"
                            else ["kill", "-9", str(r["pid"])],
                            capture_output=True, check=False)


async def test_background_avisa_cuando_el_comando_no_existe():
    """Devolver un pid que ya murió es peor que decir que falló."""
    r = await shell.lanzar("estonoexiste-xyz-abc --version")
    assert ("murió al instante" in r["out"]) or ("no pude lanzar" in r["out"])


# ---------------------------------------------------------------------------
# 2026-09-03: el envoltorio `powershell -Command "…"` que escribe el modelo.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="ruteo de Windows")
def test_desenvuelve_el_powershell_que_escribe_el_modelo():
    """`powershell -Command "…"` corre PELADO, no dentro de otro shell.

    Medido en relay.db: 640 comandos vienen envueltos y 166 traen `$`
    adentro. Sin desenvolver, la capa de afuera —PowerShell o bash, según
    a dónde caiga el ruteo— expande cada `$var` a vacío antes de que el
    PowerShell de adentro la vea.
    """
    for envuelto, payload in (
        ('powershell -NoProfile -Command "$x = 2+3; Write-Output $x"',
         "$x = 2+3; Write-Output $x"),
        ("""pwsh.exe -Command "$p='hola'; 'valor=' + $p" """.strip(),
         "$p='hola'; 'valor=' + $p"),
        # Flags con valor: hay que saltarse el valor, no leerlo como flag.
        ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$a=1"',
         "$a=1"),
        # Abreviaturas, que powershell.exe acepta.
        ('powershell -nop -c "$a=1"', "$a=1"),
        # Comillas escapadas adentro del payload: llegan peladas.
        (r'powershell -Command "Write-Output \"hola\""',
         'Write-Output "hola"'),
        # 2026-09-04, hallado en review: comillas dobladas ADYACENTES.
        # Antes se colapsaban dos veces —`""` → `"` encima del unescape
        # del backslash— y el payload salía `"a."b".c"`, un bareword
        # suelto que corre con exit=0 y el modelo lee como éxito.
        (r'powershell -Command "Write-Output \"a.\"\"b\"\".c\""',
         'Write-Output "a.""b"".c"'),
        # El `""` del payload es sintaxis de PowerShell (string vacío,
        # o comilla literal dentro de un string), no escape del
        # envoltorio: tiene que llegar intacto.
        (r'powershell -NoProfile -Command "Write-Output 1; \"\"; Write-Output 2"',
         'Write-Output 1; ""; Write-Output 2'),
    ):
        assert shell.desenvolver_powershell(envuelto) == payload, envuelto
        argv, kind = shell.build_argv(envuelto)
        assert kind == "powershell", envuelto
        assert argv[-1].endswith("\n" + payload), envuelto


@pytest.mark.skipif(sys.platform != "win32", reason="ruteo de Windows")
def test_no_desenvuelve_cuando_desenvolver_cambiaria_el_comando():
    """Ante la duda no se toca: desenvolver de más rompe lo que hoy anda.

    El caso caro es el pipe: en `powershell -Command "x" | findstr y` el
    pipe es del shell de AFUERA, así que quedarse solo con el payload
    tiraría la mitad del comando.
    """
    for cmd in (
        'powershell -Command "Get-Date" | findstr 2026',
        'powershell -Command "Get-Date" "Get-Host"',   # dos argumentos
        "powershell -NoProfile -ExecutionPolicy Bypass -File scripts/x.ps1",
        "powershell -EncodedCommand RwBlAHQALQBEAGEAdABlAA==",
        'powershell -Command "sin cerrar',
        "dotnet build",                                # ni siquiera es PS
    ):
        assert shell.desenvolver_powershell(cmd) == "", cmd


@pytest.mark.skipif(sys.platform != "win32", reason="corre PowerShell real")
async def test_las_variables_sobreviven_al_ruteo():
    """El check que falla si vuelve la doble interpolación.

    Los dos comandos son los que fallaron el 2026-09-03 en
    fuentesucursalvirtual, uno por cada ruteo: el que tiene comillas
    simples caía en bash y el otro en PowerShell, y los dos se comían el
    `$`. Se ejecutan de verdad porque el bug no se ve en el argv — se ve
    en lo que el intérprete recibe.
    """
    r = await shell.run('powershell -NoProfile -Command "$x = 2+3; '
                        'Write-Output $x"', timeout=60)
    assert "5" in r["out"], r["out"]
    assert "no se reconoce" not in r["out"], "el $ se interpoló de nuevo"

    r = await shell.run("""powershell -NoProfile -Command "$p='hola'; """
                        """'valor=' + $p" """.strip(), timeout=60)
    assert "valor=hola" in r["out"], r["out"]


# ---------------------------------------------------------------------------
# 2026-09-04: ruta Windows adentro de un comando que va a bash.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="ruteo de Windows")
def test_la_ruta_windows_sobrevive_al_ruteo_a_bash():
    r"""`cd C:\repo && … | tail` va a bash SIN perder los backslashes.

    `_looks_like_sh` es la única de las cuatro heurísticas de ruteo sin
    la guarda de `\`, así que este comando —la forma en que el modelo
    pide el final de una compilación— se iba entero a bash y bash se
    comía cada backslash como escape: `C:\Users\demo` llegaba como
    `C:Usersdemo` y el modelo leía "no existe" de una ruta que había
    escrito bien.

    Se convierte y NO se manda a cmd.exe porque cmd no tiene `tail`:
    la guarda arreglaría la ruta rompiendo el pipe.
    """
    cmd = r"cd C:\Users\demo\repo && dotnet build | tail -40"
    argv, kind = shell.build_argv(cmd)
    assert kind == "sh", "sigue necesitando bash por el `tail`"
    assert argv[-1] == "cd C:/Users/demo/repo && dotnet build | tail -40"
    assert "\\" not in argv[-1]


@pytest.mark.skipif(sys.platform != "win32", reason="ruteo de Windows")
def test_no_toca_backslashes_que_no_son_ruta():
    r"""Solo se convierte lo que arranca en `X:\`; el resto queda igual.

    El caso caro es el escape de una regex: `grep -E "^\+.*foo"` tiene
    backslashes que son del patrón, no de una ruta, y convertirlos
    cambiaría lo que el comando busca.
    """
    for cmd in (
        r'git diff | grep -n -E "^\+.*handleBuy"',
        r'grep -n "useState\|busy" src/App.tsx',
        "grep -r foo /usr/local | head -3",
    ):
        assert shell._rutas_para_bash(cmd) == cmd, cmd


@pytest.mark.skipif(sys.platform != "win32", reason="corre bash de verdad")
async def test_el_comando_con_ruta_windows_corre():
    """El check que falla si vuelve el escaping: se ejecuta de verdad.

    Reproducción exacta del caso medido el 2026-09-04: el directorio
    existe y el comando contestaba `No such file or directory`.
    """
    raiz = str(Path(__file__).resolve().parent.parent).replace("/", "\\")
    r = await shell.run(f"cd {raiz} && git ls-files | head -3", timeout=60)
    assert r["shell"] == "sh"
    assert r["exit"] == 0, r["out"]
    assert "No such file" not in r["out"], r["out"]


# ---------------------------------------------------------------------------
# 2026-09-04: los acentos que el shell escribe en el codepage de consola.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="es el codepage de Windows")
async def test_los_acentos_llegan_enteros_desde_cualquier_shell():
    """Sin esto el modelo lee "l?nea" y "t?rmino" en vez del error real.

    Los shells de Windows escriben a un pipe en el codepage de consola
    (cp850 acá), no en utf-8, y el decode era `utf-8/errors="replace"`:
    cada palabra con tilde llegaba mutilada, en proyectos que son todos
    en español. Los tres shells medidos emiten los mismos bytes cp850.
    """
    texto = "línea término carácter Matías"
    for kind, cmd in (("powershell", f'Write-Output "{texto}"'),
                      ("cmd", f"echo {texto}"),
                      ("sh", f'echo "{texto}"')):
        r = await shell.run(cmd, shell_kind=kind, timeout=60)
        assert texto in r["out"], (kind, r["out"])
        assert "\ufffd" not in r["out"], (kind, r["out"])


@pytest.mark.skipif(sys.platform != "win32", reason="es el texto de PowerShell")
async def test_el_hint_de_herramienta_faltante_tambien_desde_powershell():
    """`build_argv` elige `pwsh`, cuyo error no dice nada de lo que el
    patrón buscaba: el aviso de "falta una herramienta" —el que le pide
    al modelo que use `ask_human` en vez de improvisar una instalación—
    no se disparaba nunca desde PowerShell, solo desde cmd.exe."""
    for kind in ("powershell", "cmd", "sh"):
        r = await shell.run("estonoexiste-xyz-abc --version",
                            shell_kind=kind, timeout=60)
        assert shell.missing_tool_hint(r["out"]), (kind, r["out"])
