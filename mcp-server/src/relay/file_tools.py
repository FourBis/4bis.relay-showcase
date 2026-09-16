"""Las tools de archivos del relay y el sandbox que las acota.

Segundo corte del punto 7, después de `shell_tools`. Acá vive el límite
de permisos de verdad —`Permisos.para` con `read_only`, `rutas_vedadas`,
las raíces extra y los archivos reservados por otras tareas del grafo—,
y tenerlo en 200 líneas propias en vez de enterrado en las 2.300 de
`run_expert` es la mitad del punto: era imposible revisar de un vistazo
que archivos, shell y SQL aplicaran la misma política.

Mismo molde que `shell_tools` y `sql_tools`: una función que recibe lo
que necesita y devuelve `list[Tool]`. Los docstrings de cada tool son la
descripción que ve el modelo, no comentarios: se mueven verbatim.
"""
import os
import tempfile
import asyncio

from pydantic_ai import Tool
from pydantic_ai.messages import BinaryContent

from . import attachments as attachments_mod
from . import config
from . import files as files_mod


def rutas_extra(defaults: dict) -> list:
    """Raíces habilitadas además del repo del proyecto (2026-08-16).

    Pedido: *"el sandbox como que igual puede saltar arena afuera de la
    caja cuando necesitemos hacer otras cosas"*. La caja se queda —evita
    que un path mal calculado pise otro repo— pero deja de ser el único
    lugar del mundo. Tres fuentes, de la más específica a la más general:

    1. `defaults_json.rutas_extra` del proyecto: la lista del humano.
       Acepta el token `repos`, que se expande al `FOURBIS_REPOS_ROOT`
       configurado — es el caso común ("dejalo tocar mis repos") y
       escribir la ruta a mano en cada proyecto envejece mal.
    2. `FOURBIS_EXTRA_ROOTS` (separadas por `os.pathsep`): lo mismo pero
       para toda la instalación.
    3. El temp del sistema, SIEMPRE. Escribir un scratch, un diff o un
       log para leerlo después es la escapada legítima más común, y no
       tener dónde hacerlo empujaba al modelo a dejar basura en el repo.

    Lo que NO se agrega solo es el home ni la raíz del disco: ahí la
    diferencia entre "necesito esto" y "me equivoqué de path" deja de
    existir.
    """
    salida: list[str] = []
    for r in (defaults.get("rutas_extra") or []):
        r = str(r or "").strip()
        if r.lower() in ("repos", "repos_root"):
            raiz = config.repos_root()
            if raiz:
                salida.append(raiz)
            continue
        if r:
            salida.append(r)
    entorno = os.environ.get("FOURBIS_EXTRA_ROOTS", "")
    salida.extend(p for p in entorno.split(os.pathsep) if p.strip())
    salida.append(tempfile.gettempdir())
    return salida


def sandbox_abierto(defaults: dict) -> tuple[bool, str]:
    """¿El sandbox de archivos está apagado? → `(abierto, por_qué)`.

    Se pidió el flag después de que le explicara que el sandbox es un
    guardarraíl contra accidentes y no una jaula (docs/SANDBOX.md): con
    `shell` en la mesa, lo que cierra el sandbox ya estaba abierto por
    otro lado. Apagarlo sube la chance de accidente sin cambiar lo que el
    run puede alcanzar, así que la decisión es del humano y queda
    registrada.

    Dos fuentes, y devolvemos CUÁL ganó porque es lo que hace auditable
    un flag de este tipo: cuando alguien vea el warning en el log y se
    pregunte "¿quién lo apagó?", la respuesta está en la misma línea.
    """
    if "sandbox" in defaults:
        return (not defaults.get("sandbox"), "defaults_json.sandbox=false")
    crudo = os.environ.get("FOURBIS_SANDBOX", "").strip().lower()
    if crudo in ("0", "off", "false", "no"):
        return True, f"FOURBIS_SANDBOX={crudo}"
    return False, ""


def permisos_del_run(project: dict, defaults: dict, *, conversation_id: str,
                     reservadas=()) -> tuple:
    """`(Permisos, abierto, por_qué)` para este run.

    Va aparte de `file_tools` para que el warning del sandbox abierto lo
    emita quien tiene el logger del run, y para poder probar la política
    sin armar siete tools.
    """
    abierto, por_que = sandbox_abierto(defaults)
    # La VISTA de adjuntos de este run, no el store entero. La
    # derivación vive en `attachments.scope_for` porque el handler
    # arma con ella las rutas que le nombra al experto: si los dos
    # lados no coinciden, el prompt le da un path que su sandbox no
    # habilita.
    vista_adjuntos = str(attachments_mod.scope_dir(
        attachments_mod.scope_for(conversation_id, project.get("slug", ""))))
    perm = files_mod.Permisos.para(
        project["repo_path"],
        read_only=bool(defaults.get("read_only")),
        vedadas=defaults.get("rutas_vedadas") or [],
        # La vista de adjuntos de este run entra SIEMPRE como raíz
        # extra (2026-08-26). Antes los adjuntos que no eran texto ni
        # imagen se le anunciaban al experto con un "no puedes ver su
        # contenido": no era una limitación del modelo sino del
        # sandbox — los archivos viven en state/ del relay y las
        # tools solo llegaban al repo. Con la raíz extra, un .xlsx o
        # un .zip los abre él con `shell`, que es justo lo que
        # las tools ya sabían hacer.
        #
        # Va siempre y no solo cuando el turno trae adjuntos: un id
        # nombrado tres mensajes atrás sigue resolviendo, porque su
        # hardlink quedó en la vista desde el turno que lo citó.
        #
        # Es la VISTA y no el store entero: el store es plano y lo
        # comparten todos los proyectos, así que darlo completo ponía
        # los adjuntos del cliente B delante de un run del cliente A.
        extras=[*rutas_extra(defaults), vista_adjuntos],
        # …y de solo lectura. Los archivos de la vista son hardlinks
        # al blob real: escribir sobre uno modifica el contenido que
        # ven TODAS las conversaciones que citaron ese sha256, sin
        # ningún error visible. El experto no tiene motivo legítimo
        # para escribir ahí —si necesita una versión modificada,
        # copia al repo—, así que se lo sacamos en vez de confiar en
        # que no se le ocurra.
        solo_lectura=[vista_adjuntos],
        abierto=abierto,
        # F2: archivos que otra tarea del grafo tiene tomados. El run
        # los puede LEER pero no escribir — es lo que impide que dos
        # nodos en paralelo se pisen (docs/GRAFO_DE_TAREAS.md).
        reservadas=reservadas or ())
    return perm, abierto, por_que


def file_tools(perm) -> list[Tool]:
    """Tools de archivo e imágenes atadas a un `Permisos` ya resuelto."""

    def _fs(fn, *a, **kw) -> str:
        """Corre una operación de archivo traduciendo los errores.

        El modelo recibe un mensaje accionable en vez de un traceback:
        un `FueraDelRepo` o un `SinPermiso` no son bugs suyos, son el
        harness diciéndole que ese camino está cerrado.
        """
        try:
            return fn(perm, *a, **kw)
        except (files_mod.FueraDelRepo, files_mod.SinPermiso) as e:
            return f"error: {e}"
        except OSError as e:
            return f"error de filesystem: {e}"

    _rutas_txt = "\n".join(f"  - {r}" for r in perm.raices)

    async def read_file(path: str, desde: int = 0, hasta: int = 0) -> str:
        """Lee un archivo.

        Args:
            path: relativa a la raíz del repo, o absoluta si cae en
                una de las rutas habilitadas (`rutas_habilitadas`).
            desde: primera línea (1-based). 0 = desde el principio.
            hasta: última línea. 0 = hasta el final.
        """
        return _fs(files_mod.leer, path, desde=desde, hasta=hasta)

    async def read_image(path: str):
        """Entrega una imagen al modelo. Usa esto después de capturar con filename.

        Respeta las mismas rutas y vetos que read_file. Leer el PNG como
        texto o comprobar su tamaño no permite inspeccionarlo visualmente.
        """
        try:
            source = perm.resolve(path)
            mime = attachments_mod.image_mime(source)
            if not mime:
                return "error: se requiere PNG, JPEG, GIF o WebP"
            if source.stat().st_size > attachments_mod.max_attachment_bytes():
                return "error: imagen excede ATTACHMENT_MAX_BYTES"
            def read_bounded():
                with source.open("rb") as f:
                    return f.read(attachments_mod.max_attachment_bytes() + 1)
            data = await asyncio.to_thread(read_bounded)
            if len(data) > attachments_mod.max_attachment_bytes():
                return "error: imagen excede ATTACHMENT_MAX_BYTES"
            return BinaryContent(data=data, media_type=mime)
        except (files_mod.FueraDelRepo, files_mod.SinPermiso, OSError) as exc:
            return f"error: {exc}"

    async def rutas_habilitadas() -> str:
        """Dónde puedes leer y escribir con las tools de archivo.

        Si necesitas una ruta que no está en esta lista, no lo
        intentes de rebote: pídesela al humano con `ask_human`
        diciendo cuál y para qué.
        """
        modo = "lectura y escritura" if perm.escribir else "solo lectura"
        if perm.abierto:
            # Que el modelo sepa que no hay red no es un detalle: es
            # la diferencia entre "prueba, total si te pasas te frena"
            # y "mide bien el path antes de escribir".
            return (f"Sandbox APAGADO: puedes {modo} en cualquier ruta "
                    f"del disco. El repo del proyecto es {perm.raiz}.\n"
                    "Nada te va a frenar si te equivocas de path, así "
                    "que verifica antes de escribir o mover.")
        return (f"Raíces habilitadas ({modo}):\n{_rutas_txt}\n"
                "Una ruta afuera de todas se rechaza. `shell` no tiene "
                "esta restricción: corre donde le digas.")

    async def write_file(path: str, content: str) -> str:
        """Escribe un archivo (lo crea o lo sobrescribe entero)."""
        return _fs(files_mod.escribir, path, content)

    async def edit_file(path: str, old: str, new: str) -> str:
        """Reemplaza la PRIMERA ocurrencia de `old` por `new`.

        Si el fragmento aparece más de una vez, falla y te lo dice:
        agrega contexto alrededor hasta que sea único.
        """
        return _fs(files_mod.editar, path, old, new)

    async def move_file(src: str, dst: str) -> str:
        """Mueve o renombra. Los dos extremos van en rutas habilitadas."""
        return _fs(files_mod.mover, src, dst)

    async def list_dir(path: str = "", max_depth: int = 3) -> str:
        """Árbol del directorio. Respeta .gitignore y saltea build dirs."""
        return _fs(files_mod.arbol, path, max_depth=max_depth)

    async def search_files(pattern: str, glob: str = "*",
                           en: str = "") -> str:
        """Busca un texto literal.

        Args:
            pattern: el texto a buscar (literal, no regex).
            glob: filtro de archivos. Ej: `*.py`, `**/*.cs`.
            en: subdirectorio o raíz habilitada donde buscar.
                Vacío = el repo del proyecto.
        """
        return _fs(files_mod.buscar, pattern, glob=glob, en=en)

    return [Tool(f, takes_ctx=False) for f in
            (read_file, read_image, write_file, edit_file, move_file, list_dir,
             search_files, rutas_habilitadas)]
