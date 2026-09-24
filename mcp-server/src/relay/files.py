"""Tools de archivo nativas del relay (2026-08-16).

Reemplazan a las del wrapper MCP (`mcp_wrapper`, repo `4bis.vscode`).
Decisión del usuario: VS Code ya no se usa, así que el wrapper dejó de
tener un consumidor propio y solo agregaba una capa con sus propios caps
—6.000 chars de salida de shell, 256 KB de lectura, 60 s de timeout— que
ganaban en silencio por estar más adentro en la cadena. Una sola fuente
de verdad: el relay. Ver docs/WRAPPER.md.

Lo que se PORTA del wrapper, no se reescribe de memoria, porque son
decisiones que costaron sangre y perderlas sería el peor resultado
posible de esta migración:

- el **sandbox de rutas** (`resolve`): toda ruta cae adentro del repo, y
  las absolutas se permiten solo si ya están adentro;
- el **matcher de .gitignore** (`_pattern_matches`) con su bugfix del
  2026-07-26 sobre las tres formas en que la gente escribe un patrón:
  `out/` con barra, `/build` anclado y `**/tmp`;
- los **defaults duros** de directorios que nunca se listan aunque el
  repo los trackee (`obj/`, `node_modules`, …);
- el **cap del árbol** para que un repo sin `.gitignore` no reviente el
  contexto.

Lo que se AGREGA: permisos explícitos. `Permisos` decide qué puede hacer
un run —leer siempre, escribir solo si el proyecto lo permite— en vez de
que la única defensa sea que el modelo se porte bien.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import logging
import os
import shutil
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("relay.files")

MAX_READ_BYTES = int(os.environ.get("FOURBIS_READ_MAX_BYTES", str(512 * 1024)))
MAX_TREE_ENTRIES = int(os.environ.get("FOURBIS_TREE_MAX_ENTRIES", "500"))

# Directorios que NUNCA se listan, aunque el repo los trackee: listar
# `obj/Debug/net10.0/…` le aporta cero al LLM y varios KB de tokens.
_IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "__pycache__", ".venv", "venv", "env", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".nox", "eggs",
    "node_modules", ".next", ".nuxt", ".cache", ".parcel-cache",
    "bin", "obj", "Debug", "Release", "packages", "publish",
    "target", ".gradle", "build", ".idea", ".settings",
    "vendor",
    ".vscode", ".vs", ".DS_Store", "Thumbs.db",
})
_IGNORED_FILES = frozenset({
    ".DS_Store", "Thumbs.db", "desktop.ini", ".coverage", "coverage.json",
})


class FueraDelRepo(ValueError):
    """La ruta pedida cae fuera del repo del proyecto."""


class SinPermiso(PermissionError):
    """La operación existe pero este run no tiene permiso para hacerla."""


@dataclasses.dataclass(frozen=True)
class Permisos:
    """Qué puede hacer un run con el filesystem.

    Rústico a propósito: unos flags y una lista de raíces. La
    alternativa —una ACL por path— se ve más completa y en la práctica
    nadie la configura, así que termina siendo permitir todo con más
    pasos.

    **Qué es y qué NO es esta caja.** Es una barrera contra el
    *accidente*: que un `write_file("../../otro-repo/x")` mal calculado
    no pise algo que nadie estaba mirando. **No es una barrera de
    seguridad**, porque el mismo run tiene la tool `shell`, que corre
    cualquier comando en cualquier directorio. Quien quiera decir la
    verdad sobre este módulo tiene que decir las dos cosas: sirve para
    que el modelo no se equivoque, no para contener a un modelo que
    quiera salirse. Contener eso, con `shell` en la mesa, no es posible
    —y capar `shell` era justo lo que sacamos.

    `raiz` es el repo del proyecto. `extras` (2026-08-16) son las otras
    raíces que el humano habilitó: otro repo, una carpeta de datos, el
    temp del sistema. Adentro de cualquiera de ellas se puede trabajar
    igual que en la propia; afuera de todas, no.

    `abierto` (2026-08-16, a pedido) apaga el chequeo de raíces: las
    tools de archivo llegan a cualquier lado del disco. Lo que **no**
    apaga, y es a propósito, son las otras dos defensas: `read_only`
    sigue prohibiendo escribir y `vedadas` sigue tapando lo que el humano
    marcó. Son preguntas distintas —*dónde* vs *si* vs *qué nunca*— y
    mezclarlas en un solo switch haría que apagar una apague tres.
    """

    raiz: Path
    escribir: bool = True
    # Rutas que ni se leen aunque estén dentro de la raíz. Para secretos
    # que viven en el repo (un `.env` con keys de producción).
    vedadas: frozenset = frozenset()
    # Raíces extra habilitadas por el humano. Vacío = solo el repo.
    extras: tuple = ()
    # Sandbox apagado: todo el disco al alcance de las tools de archivo.
    abierto: bool = False
    # Archivos que OTRA tarea del grafo tiene tomados (F2, 2026-08-17).
    # Ya normalizados (unix, minusculas). Solo bloquean ESCRITURA: dos
    # tareas pueden leer el mismo archivo sin problema, y prohibirlo
    # serializaria el grafo sin ganar nada.
    reservadas: frozenset = frozenset()
    # Raices que se LEEN pero no se escriben (2026-08-26). Distinto de
    # `read_only`, que apaga la escritura del run entero, y de `vedadas`,
    # que tampoco deja leer. Existe para las raices extra que no son area
    # de trabajo: hoy el store de adjuntos, que el experto tiene que
    # poder abrir pero nunca modificar —esta indexado por sha256, asi que
    # sobrescribir un archivo rompe en silencio la correspondencia entre
    # el id y el contenido, para TODAS las conversaciones que lo citen.
    solo_lectura: tuple = ()

    @classmethod
    def para(cls, repo_path: str, *, read_only: bool = False,
             vedadas: Optional[list] = None,
             extras: Optional[list] = None,
             abierto: bool = False,
             reservadas: Optional[Iterable] = None,
             solo_lectura: Optional[Iterable] = None) -> "Permisos":
        # `expanduser`/`expandvars` como en las extras de abajo
        # (2026-08-26). La raíz era el único path que NO se expandía: un
        # proyecto con `repo_path = ~/.4bis/notes` terminaba con el
        # sandbox apuntando a `<cwd>/~/.4bis/notes`, un directorio
        # literal llamado `~` que no existe. El experto no veía un error
        # de configuración: veía un repo vacío.
        raiz = Path(os.path.expanduser(os.path.expandvars(
            str(repo_path or "")))).resolve()
        fuera = []
        for e in (extras or []):
            e = str(e or "").strip()
            if not e:
                continue
            try:
                p = Path(os.path.expanduser(os.path.expandvars(e))).resolve()
            except (OSError, ValueError):
                logger.warning("ruta extra ilegible, la ignoro: %r", e)
                continue
            # Una extra que ya está adentro del repo no agrega nada, y
            # duplicarla haría que `base_de` conteste la más específica
            # en vez de la raíz (rompe los paths relativos del árbol).
            if p != raiz and not str(p).startswith(str(raiz) + os.sep):
                fuera.append(p)
        return cls(
            raiz=raiz,
            escribir=not read_only,
            vedadas=frozenset(v.strip().lower() for v in (vedadas or []) if v.strip()),
            extras=tuple(dict.fromkeys(fuera)),
            abierto=bool(abierto),
            reservadas=frozenset(reservadas or ()),
            # Se resuelven igual que las extras: el caller pasa lo mismo
            # en los dos lados y acá tienen que quedar comparables, o el
            # prefijo de `exigir_escribible` no matchea nunca.
            solo_lectura=tuple(dict.fromkeys(
                Path(os.path.expanduser(os.path.expandvars(str(s)))).resolve()
                for s in (solo_lectura or []) if str(s or "").strip())),
        )

    @property
    def raices(self) -> tuple:
        return (self.raiz,) + tuple(self.extras)

    def base_de(self, p: Path) -> Optional[Path]:
        """Qué raíz contiene a `p`, o None si ninguna.

        Devuelve la más específica: con `/repos` y `/repos/a` habilitados
        a la vez, un archivo de `a` se muestra relativo a `a` y no con
        medio path de más.
        """
        s = str(p)
        candidatas = [r for r in self.raices
                      if s == str(r) or s.startswith(str(r) + os.sep)]
        return max(candidatas, key=lambda r: len(str(r))) if candidatas else None

    def resolve(self, ruta: str) -> Path:
        """Ruta del modelo → Path absoluto validado. Portado del wrapper.

        Las relativas se resuelven contra el repo; las absolutas se
        aceptan si caen en el repo o en alguna raíz extra — eso le da
        margen al modelo (que a veces pega un path completo sacado de un
        error) sin abrir `C:\\Windows\\System32` por default.

        `.resolve()` ANTES de comparar es lo que corta el escape con
        `..`: sin eso, `repo/../../etc/passwd` pasa el startswith.
        """
        p = Path(ruta or "")
        if not p.is_absolute():
            p = self.raiz / p
        p = p.resolve()
        base = self.base_de(p)
        if base is None and not self.abierto:
            permitidas = "\n".join(f"  - {r}" for r in self.raices)
            raise FueraDelRepo(
                f"`{ruta}` cae fuera de las rutas habilitadas. Podés "
                f"trabajar en:\n{permitidas}\n"
                "Si de verdad necesitás otra, pedísela al humano con "
                "`ask_human` diciendo cuál y para qué: la habilita en el "
                "proyecto (`rutas_extra`) y seguís. Para una lectura "
                "suelta afuera, `shell` no tiene esta restricción.")
        if self.vedadas:
            # Con el sandbox abierto no hay raíz contra la cual medir, así
            # que el veto se aplica sobre el path completo. Que `vedadas`
            # siga funcionando con el sandbox apagado es el punto: es la
            # lista de lo que NUNCA se toca, no una consecuencia de dónde
            # esté parado el run.
            rel = (p.relative_to(base).as_posix().lower() if base is not None
                   else p.as_posix().lower())
            partes = rel.split("/")
            if any(rel == v or rel.startswith(v.rstrip("/") + "/")
                   or (self.abierto and v.rstrip("/") in partes)
                   for v in self.vedadas):
                raise SinPermiso(
                    f"`{ruta}` está en la lista de rutas vedadas de este "
                    "proyecto. Si de verdad lo necesitás, pedíselo al humano "
                    "con `ask_human`.")
        return p

    def exigir_escritura(self, que: str) -> None:
        if not self.escribir:
            raise SinPermiso(
                f"este proyecto está en modo solo-lectura: no puedo {que}. "
                "El humano revisa la asignación del proyecto en Equipo y el modo de esta tarea. "
                "Cambiar read_only del proyecto no habilita una tarea que nació solo lectura.")

    def exigir_escribible(self, p: Path) -> None:
        """Lanza si `p` cae en una raíz de solo lectura.

        Complementa a `exigir_escritura`, que mira el switch del proyecto
        entero: esta mira DÓNDE. Un run perfectamente escribible sobre su
        repo igual no tiene por qué poder tocar una raíz extra que le
        prestamos para leer.
        """
        for r in self.solo_lectura:
            s, base = str(p), str(r)
            if s == base or s.startswith(base + os.sep):
                raise SinPermiso(
                    f"`{p.name}` está en un directorio que puedes leer pero "
                    "no modificar. Si necesitas una versión cambiada, copiala "
                    "primero adentro del repo y trabaja sobre la copia.")

    def exigir_libre(self, p: Path) -> None:
        """Lanza si otra tarea del grafo tiene tomado este archivo.

        Esta es la capa que GARANTIZA que dos bots en paralelo no se
        pisen. La otra —que el planificador declare qué archivos toca
        cada tarea— evita el choque al planificar, pero una declaración
        de un LLM puede quedarse corta: descubre a mitad del trabajo que
        también hay que tocar otro archivo, y lo toca.

        Solo escritura. Leer el archivo que otro está editando es
        legítimo y frecuente.
        """
        if not self.reservadas:
            return
        base = self.base_de(p)
        rel = (p.relative_to(base).as_posix().lower() if base is not None
               else p.as_posix().lower())
        entero = p.as_posix().lower()
        for r in self.reservadas:
            if rel == r or entero == r or rel.startswith(r.rstrip("/") + "/"):
                raise SinPermiso(
                    f"`{rel}` lo está trabajando otra tarea del plan en este "
                    "momento. No lo edites en paralelo: terminá lo tuyo y "
                    "dejalo anotado en tu resultado, o pedile al humano que "
                    "reordene el plan si de verdad hay que tocarlo acá.")


# ---------- .gitignore (portado del wrapper, con su bugfix) ----------


def _pattern_matches(pat: str, rel: str, name: str) -> bool:
    """¿El patrón de .gitignore matchea este path?

    Portado tal cual del wrapper, incluido el bugfix del 2026-07-26: con
    `fnmatch` crudo NO matcheaba ninguna de las tres formas en que la
    gente escribe un .gitignore —`out/` con barra final, `/build`
    anclado, `**/tmp`—, o sea que todo patrón de DIRECTORIO se ignoraba
    en silencio y `dist/` se listaba igual.

    Sigue sin ser un parser completo (no hay `**` en el medio ni
    .gitignore anidados). Criterio de ADR-016: ante la duda mostrar de
    más, nunca esconder algo que el dev quería ver.
    """
    pat = pat.rstrip("/")
    if not pat:
        return False
    anclado = pat.startswith("/")
    pat = pat.lstrip("/")
    if pat.startswith("**/"):
        pat, anclado = pat[3:], False
    if not pat:
        return False
    if fnmatch.fnmatch(rel, pat):
        return True
    if anclado:
        return False
    if fnmatch.fnmatch(name, pat):
        return True
    # Un patrón de directorio tapa todo lo que cuelga de él.
    return any(fnmatch.fnmatch(parte, pat) for parte in rel.split("/")[:-1])


def esta_gitignorado(raiz: Path, target: Path) -> bool:
    """Heurística barata sobre el .gitignore del root. Las negaciones ganan."""
    gi = raiz / ".gitignore"
    if not gi.is_file():
        return False
    try:
        lineas = gi.read_text(encoding="utf-8", errors="replace").splitlines()
        rel = target.relative_to(raiz).as_posix()
    except (OSError, ValueError):
        return False
    name = target.name
    matcheo = False
    for linea in lineas:
        s = linea.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("!"):
            if _pattern_matches(s[1:].strip(), rel, name):
                return False
            continue
        if _pattern_matches(s, rel, name):
            matcheo = True
    return matcheo


def _ignorado_por_default(name: str) -> bool:
    return (name in _IGNORED_DIRS or name in _IGNORED_FILES
            or name.endswith(".egg-info") or name.endswith(".log"))


# ---------- operaciones ----------


def leer(perm: Permisos, ruta: str, *, desde: int = 0, hasta: int = 0) -> str:
    """Contenido de un archivo. `desde`/`hasta` son líneas 1-based.

    El rango existe porque el cap del wrapper (256 KB) fallaba y ya:
    el modelo tenía que adivinar cómo pedir menos. Acá, si el archivo no
    entra, el mensaje dice exactamente qué rango pedir.
    """
    p = perm.resolve(ruta)
    if not p.is_file():
        return f"error: no existe o no es un archivo: {ruta}"
    tam = p.stat().st_size
    if tam > MAX_READ_BYTES and not (desde or hasta):
        lineas = _contar_lineas(p)
        return (f"error: `{ruta}` pesa {tam // 1024} KB (el tope es "
                f"{MAX_READ_BYTES // 1024} KB) y tiene ~{lineas} líneas. "
                f"Pedí un rango: read_file(ruta, desde=1, hasta=400).")
    texto = p.read_text(encoding="utf-8", errors="replace")
    if desde or hasta:
        todas = texto.splitlines()
        ini = max(1, desde or 1)
        fin = min(len(todas), hasta or len(todas))
        if ini > len(todas):
            return f"error: `{ruta}` tiene {len(todas)} líneas; pediste desde {ini}."
        cuerpo = "\n".join(todas[ini - 1:fin])
        return (f"[{ruta} · líneas {ini}-{fin} de {len(todas)}]\n{cuerpo}")
    return texto


def _contar_lineas(p: Path) -> int:
    try:
        with p.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def escribir(perm: Permisos, ruta: str, contenido: str) -> str:
    perm.exigir_escritura(f"escribir `{ruta}`")
    p = perm.resolve(ruta)
    perm.exigir_escribible(p)
    perm.exigir_libre(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    nuevo = not p.exists()
    p.write_text(contenido, encoding="utf-8")
    n = contenido.count("\n") + 1 if contenido else 0
    return f"ok: {'creado' if nuevo else 'sobrescrito'} `{ruta}` ({n} líneas)"


def editar(perm: Permisos, ruta: str, viejo: str, nuevo: str) -> str:
    """Reemplaza la PRIMERA ocurrencia de `viejo`. Portado del wrapper.

    Que sea la primera y no todas es deliberado: un reemplazo global es
    la forma más fácil de romper un archivo sin darse cuenta. Si el
    fragmento aparece más de una vez, se lo decimos al modelo para que
    agregue contexto en vez de adivinar.
    """
    perm.exigir_escritura(f"editar `{ruta}`")
    p = perm.resolve(ruta)
    perm.exigir_escribible(p)
    perm.exigir_libre(p)
    if not p.is_file():
        return f"error: no existe o no es un archivo: {ruta}"
    texto = p.read_text(encoding="utf-8", errors="replace")
    if viejo not in texto:
        return (f"error: no encontré ese fragmento en `{ruta}`. "
                "Leé el archivo y copiá el texto exacto (con su indentación).")
    veces = texto.count(viejo)
    if veces > 1:
        return (f"error: ese fragmento aparece {veces} veces en `{ruta}`. "
                "Agregá líneas de contexto alrededor para que sea único.")
    p.write_text(texto.replace(viejo, nuevo, 1), encoding="utf-8")
    return f"ok: editado `{ruta}`"


def mover(perm: Permisos, origen: str, destino: str) -> str:
    perm.exigir_escritura(f"mover `{origen}`")
    o = perm.resolve(origen)
    d = perm.resolve(destino)     # el destino también va al sandbox
    perm.exigir_escribible(o)     # …los dos extremos, a las raíces RO…
    perm.exigir_escribible(d)
    perm.exigir_libre(o)          # …y los dos extremos, a las reservas
    perm.exigir_libre(d)
    if not o.exists():
        return f"error: no existe: {origen}"
    if d.exists():
        return f"error: el destino ya existe: {destino}"
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(o), str(d))
    return f"ok: {origen} → {destino}"


def arbol(perm: Permisos, ruta: str = "", max_depth: int = 3) -> str:
    """Árbol textual del directorio. Portado del wrapper.

    Dos filtros, como allá (ADR-016): los defaults duros SIEMPRE, y
    después el `.gitignore` del repo. Más el cap de entries para que un
    repo sin `.gitignore` no se lleve puesto el contexto.
    """
    root = perm.resolve(ruta) if ruta else perm.raiz
    # La raíz de referencia es la que CONTIENE lo que se pide, no siempre
    # el repo: si se lista una ruta extra, los paths relativos y el
    # `.gitignore` que aplica son los de esa raíz.
    # Con el sandbox abierto un path puede no estar bajo ninguna raíz;
    # ahí la referencia es el propio directorio pedido (`relative_to` de
    # otra cosa explotaría). Listar `/etc` muestra paths relativos a
    # `/etc`, que es lo que uno espera igual.
    raiz = perm.base_de(root) or (root if perm.abierto else perm.raiz)
    if not root.is_dir():
        return f"error: no es un directorio: {ruta or '.'}"
    max_depth = min(max(int(max_depth or 1), 1), 10)

    salida: list[str] = []
    base = len(root.relative_to(raiz).parts) if root != raiz else 0
    truncado = False

    def caminar(p: Path, depth: int) -> None:
        nonlocal truncado
        if len(salida) >= MAX_TREE_ENTRIES:
            truncado = True
            return
        if depth > max_depth:
            return
        rel = p.relative_to(raiz)
        prefijo = "  " * (len(rel.parts) - base - 1) if rel.parts else ""
        marca = "[D] " if p.is_dir() else "[F] "
        salida.append(f"{prefijo}{marca}{p.name}{'/' if p.is_dir() else ''}")
        if not p.is_dir():
            return
        try:
            entradas = sorted(p.iterdir(),
                              key=lambda x: (not x.is_dir(), x.name.lower()))
        except (PermissionError, OSError):
            salida.append(f"{prefijo}  [permiso denegado]")
            return
        for e in entradas:
            if e.name.startswith("."):
                # El .gitignore sí se muestra: es info útil sobre el repo.
                if e.name == ".gitignore":
                    caminar(e, depth)
                continue
            if _ignorado_por_default(e.name) or esta_gitignorado(raiz, e):
                continue
            caminar(e, depth + 1)

    caminar(root, 0)
    if truncado:
        salida.append(
            f"⚠ listado truncado a {MAX_TREE_ENTRIES} entradas. Pedí un path "
            "más específico o bajá max_depth.")
    return "\n".join(salida)


def buscar(perm: Permisos, patron: str, *, glob: str = "*",
           max_hits: int = 60, en: str = "") -> str:
    """Grep simple sobre el repo. NO estaba en el wrapper.

    Se agrega porque su ausencia era la razón por la que el experto caía
    a `run_shell` con `grep`/`Select-String` para cualquier búsqueda —
    con el ruteo de shell, las comillas y el mojibake que eso arrastra.
    Una tool que hace lo mismo sin salir de Python es más barata y no
    depende de qué coreutils tenga la máquina.

    `en` acota a un subdirectorio o apunta a una raíz extra habilitada.
    """
    if not (patron or "").strip():
        return "error: patrón vacío"
    root = perm.resolve(en) if en else perm.raiz
    if not root.is_dir():
        return f"error: no es un directorio: {en}"
    base = perm.base_de(root) or (root if perm.abierto else perm.raiz)
    hits: list[str] = []
    for p in sorted(root.rglob(glob or "*")):
        if len(hits) >= max_hits:
            hits.append(f"⚠ corté en {max_hits} coincidencias; afiná el patrón.")
            break
        if not p.is_file() or _ignorado_por_default(p.name):
            continue
        if any(_ignorado_por_default(parte) for parte in p.relative_to(base).parts):
            continue
        if esta_gitignorado(base, p):
            continue
        try:
            if p.stat().st_size > MAX_READ_BYTES:
                continue
            for i, linea in enumerate(
                    p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if patron in linea:
                    rel = p.relative_to(base).as_posix()
                    hits.append(f"{rel}:{i}: {linea.strip()[:200]}")
                    if len(hits) >= max_hits:
                        break
        except OSError:
            continue
    return "\n".join(hits) if hits else f"sin coincidencias para {patron!r}"
