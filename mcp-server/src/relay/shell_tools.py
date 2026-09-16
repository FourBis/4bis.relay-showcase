"""La tool `shell` nativa del relay, con su techo y su bitácora.

Sale de `run_expert` —2.400 líneas— para que el límite de permisos sea
legible de un vistazo: el punto 3 de la auditoría era justamente que
`read_only` y `rutas_vedadas` se aplicaban a las tools de archivos y la
shell se habilitaba por su cuenta, 200 líneas más abajo, sin enterarse.
Acá la decisión entra por la puerta: quien no la llama, no tiene shell.

Es el mismo molde que `sql_tools`: una función que recibe lo que necesita
y devuelve `list[Tool]`. Nada de estado de módulo — cada run tiene su
repo, su techo y su bitácora.

Dos cosas que NO se tocaron al mover, porque son contrato y no estilo:
el nombre de cada parámetro de `shell` (es el schema que ve el modelo, y
renombrar `timeout_s` rompe a cualquiera que lo mande) y su docstring
entero (es la descripción de la tool, no un comentario). El techo del
run entra como `techo_s` justamente para no pisar el `timeout_s` del
modelo.

`en_vuelo` viene por parámetro y no importado: el bookkeeping del
watchdog vive en `experts` junto a `CappedToolset`, que es el otro que lo
escribe, y traerlo para acá haría un ciclo de imports sin comprar nada.
"""
from pydantic_ai import Tool
from typing import Literal

from . import shell as shell_mod


def shell_tools(*, repo: str, techo_s: float, bitacora, en_vuelo) -> list[Tool]:
    """La tool `shell` lista para adjuntar.

    `en_vuelo(nombre)` es el context manager que le avisa al watchdog que
    hay una tool corriendo. Sin eso, esperar un comando cuenta como estar
    idle y el run muere a los `idle_timeout` aunque el comando esté
    dentro de su propio techo.
    """

    async def shell(cmd: str, cwd: str = "", timeout_s: int = 0,
                    background: bool = False,
                    shell_kind: Literal["auto", "powershell", "cmd", "sh"] = "auto") -> str:
        """Ejecuta un comando y devuelve su salida + el exit code.

        Corre sin terminal interactiva: stdin cerrado, sin perfil de
        PowerShell, y con el entorno no-interactivo de las
        herramientas comunes (git, npm, pip, dotnet). Un comando que
        pediría confirmación falla rápido en vez de colgarse.

        Puedes usar PowerShell, cmd o sh según la máquina: se elige
        solo por el comando.

        **Un server va con `background=True`.** `npm run dev`,
        `dotnet run`, `docker compose up` sin `-d`, `vite`, `uvicorn`:
        todo lo que se queda escuchando y no vuelve al prompt. En modo
        normal esta tool espera, corta al vencer el timeout y mata el
        árbol de procesos — o sea que el server que acabas de levantar
        se muere con el corte. Con `background=True` vuelve enseguida
        con el pid y la ruta del log, y el proceso queda vivo.

        Args:
            cmd: el comando, tal como lo escribirías en la terminal.
            cwd: directorio donde correrlo. Vacío = la raíz del repo.
            timeout_s: techo en segundos. 0 = el default del proyecto.
                Se recorta al techo del proyecto
                (`defaults_json.tool_call_timeout_s`): pedir más no
                sirve, porque por encima está el corte del toolset.
                Se ignora con `background=True` (no hay nada que
                esperar).
            background: largarlo y no esperarlo. La salida va a un
                archivo: léelo con shell (o read_file si está en sus
                raíces). Comprueba el endpoint para confirmar disponibilidad.
                Para bajarlo, mata el árbol del pid que devuelve.
            shell_kind: intérprete explícito; auto lo deduce. Bash corta
                ante errores y usa pipefail; PowerShell corta errores de
                cmdlets y, desde 7.3, nativos. En cmd usa && entre comandos
                dependientes y comprueba cada errorlevel; un pipeline de
                cmd no verifica todos sus pasos.
        """
        if background:
            res = await shell_mod.lanzar(
                cmd, cwd=cwd or None, base=repo, shell_kind=shell_kind)
            # Misma bitácora que el camino normal: "largué esto y me
            # dio este pid" tiene que sobrevivir a la elisión del
            # historial, sobre todo porque después hay que bajarlo.
            bitacora.anotar_comando(
                f"{cmd}  [background]", res["exit"])
            return res["out"]
        secs = float(timeout_s) if timeout_s and timeout_s > 0 else techo_s
        # El recorte existe para que el corte lo haga SIEMPRE esta
        # tool y no el toolset: el de acá mata el árbol de procesos y
        # devuelve texto accionable; el de arriba solo cancela la
        # corrutina y deja los hijos vivos (13 `node` huérfanos en el
        # run de sample-app del 19/8).
        secs = min(secs, techo_s)
        # Anunciarse al watchdog: sin esto, esperar un comando cuenta
        # como estar idle y el run muere a los `idle_timeout` aunque
        # el comando esté dentro de su propio techo.
        async with en_vuelo("shell"):
            # `base=repo` y NO `cwd or repo`: con el `or`, un `cwd`
            # relativo ("frontend") nunca se resolvía contra el repo y
            # el harness contestaba que no existe. Ver
            # `shell._resolver_cwd`.
            res = await shell_mod.run(
                cmd, cwd=cwd or None, base=repo, timeout=secs, shell_kind=shell_kind)
        salida = res["out"]
        # El rastro va a la bitácora ANTES de devolver: la salida se
        # va a elidir en unas cuantas llamadas más, pero "corrí esto
        # y dio exit=N" tiene que sobrevivir hasta el final del run.
        bitacora.anotar_comando(cmd, res["exit"])
        # Si falta una herramienta, el hint le dice al modelo que
        # pregunte en vez de improvisar una instalación.
        if res["exit"] != 0:
            salida += shell_mod.missing_tool_hint(salida)
        return salida

    return [Tool(shell, takes_ctx=False)]
