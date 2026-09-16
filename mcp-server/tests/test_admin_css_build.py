"""admin.css es output de build commiteado: verifica que este al dia.

static/admin.css lo genera admin_static/build-css.ps1 desde
static/admin.src.css (Tailwind CLI v3). Como el bundle vive en el repo
y el relay lo sirve tal cual, editar el .src.css y olvidar el build
deja el admin sirviendo CSS viejo en silencio: nada falla, la UI
simplemente no tiene la regla nueva.

build-css.ps1 sella el bundle con /*!src-sha256:<hash>*/ al final.
Aca comparamos ese sello contra el hash real de la fuente.

Que hacer si este test falla:
    pwsh mcp-server/admin_static/build-css.ps1

ponytail: sella la fuente, no el output. No detecta que alguien edite
admin.css a mano (el header ya dice NO EDITAR); detecta el olvido, que
es el caso real.

2026-08-16 — el sello se calcula sobre la fuente con los saltos de linea
NORMALIZADOS a LF. Antes se hasheaban los bytes crudos, y como
.gitattributes fuerza `eol=lf` en el repo mientras que la copia de
trabajo en Windows tiene CRLF, el hash sellado por build-css.ps1 (CRLF)
nunca coincidia con el de un checkout LF. El test fallaba en toda maquina
que no fuera la del autor, con un mensaje que acusaba a un bundle viejo
cuando el bundle estaba al dia. Un guard que grita en falso ensenia a
ignorarlo, que es exactamente lo contrario de para lo que existe.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest


def test_failed_compiler_does_not_seal_stale_css(tmp_path):
    """Ejecuta el script real con un compilador que sale en error (Windows)."""
    import os
    import shutil
    import subprocess
    if os.name != "nt" or not shutil.which("pwsh"):
        pytest.skip("requiere PowerShell y herramientas Windows")
    source = Path(__file__).resolve().parents[1] / "admin_static"
    work = tmp_path / "admin"
    shutil.copytree(source, work)
    fake_cli = tmp_path / "twcli"
    fake_cli.mkdir()
    # hostname.exe rechaza los flags de Tailwind; no depende de
    # runtimes externos y permite probar el manejo real del exit nativo.
    shutil.copyfile(shutil.which("hostname.exe"), fake_cli / "tailwindcss.exe")
    css = work / "static/admin.css"
    before = css.read_bytes()
    process = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-File", str(work / "build-css.ps1")],
        env={**os.environ, "TEMP": str(tmp_path)}, capture_output=True, timeout=30)
    assert process.returncode != 0
    assert css.read_bytes() == before, "un build fallido no acredita CSS viejo con sellos nuevos"

_STATIC = Path(__file__).resolve().parents[1] / "admin_static" / "static"
_SRC = _STATIC / "admin.src.css"
_BUNDLE = _STATIC / "admin.css"
_MARKER = re.compile(rb"/\*!src-sha256:([0-9a-f]{64})\*/")


def _hash_normalizado(path: Path) -> str:
    """sha256 de la fuente con CRLF -> LF.

    Insensible al line ending para que el sello valga en cualquier
    checkout: el repo guarda LF (.gitattributes) y la copia de trabajo
    en Windows tiene CRLF. `build-css.ps1` calcula el suyo igual.
    """
    return hashlib.sha256(
        path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def test_admin_css_esta_sincronizado_con_su_fuente() -> None:
    assert _SRC.is_file(), f"falta la fuente {_SRC}"
    assert _BUNDLE.is_file(), f"falta el bundle {_BUNDLE}"

    m = _MARKER.search(_BUNDLE.read_bytes())
    if m is None:
        pytest.fail(
            "admin.css no tiene el sello src-sha256 — se genero con un "
            "build-css.ps1 viejo. Corre: pwsh mcp-server/admin_static/"
            "build-css.ps1"
        )

    sellado = m.group(1).decode()
    real = _hash_normalizado(_SRC)
    assert sellado == real, (
        "admin.src.css cambio y admin.css no se regenero: el relay esta "
        "sirviendo CSS viejo.\n"
        f"  sellado en el bundle: {sellado}\n"
        f"  hash real de la fuente: {real}\n"
        "  fix: pwsh mcp-server/admin_static/build-css.ps1"
    )


# ---------- el otro olvido: clases nuevas sin rebuild (2026-08-16) ----------
#
# El test de arriba detecta "editaste admin.src.css y no rebuildeaste".
# No detecta el caso simétrico, que pasó de verdad: agregar una clase de
# Tailwind en el HTML o en un .js y no rebuildear. El bundle no la tiene,
# la clase no hace nada, y nada falla — la UI simplemente se ve distinta
# de lo que alguien escribió.
#
# Se hashea el CONJUNTO de clases usadas, no los archivos: editar lógica
# de un .js no pide rebuild de CSS (sería un guard que grita en falso, y
# eso enseña a ignorarlo), pero agregar o sacar una clase sí.

_MARCA_CLASES = re.compile(rb"/\*!classes-sha256:([0-9a-f]{64})\*/")


def _sello_clases():
    """Importa el script que usa build-css.ps1 — una sola implementación."""
    import importlib.util

    ruta = _STATIC.parent / "sello_clases.py"
    spec = importlib.util.spec_from_file_location("sello_clases", ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_el_bundle_conoce_las_clases_que_usan_las_fuentes() -> None:
    mod = _sello_clases()
    m = _MARCA_CLASES.search(_BUNDLE.read_bytes())
    if m is None:
        pytest.fail(
            "admin.css no tiene el sello classes-sha256 — se generó con un "
            "build-css.ps1 viejo. Corré: pwsh mcp-server/admin_static/"
            "build-css.ps1"
        )
    assert m.group(1).decode() == mod.sello(), (
        "cambiaron las clases que usan index.html o los .js y admin.css no "
        "se regeneró: las clases nuevas no están en el bundle y no hacen "
        "nada.\n  fix: pwsh mcp-server/admin_static/build-css.ps1"
    )


def test_ninguna_clase_de_tailwind_usada_falta_en_el_bundle() -> None:
    """Red de seguridad por si el sello se regenera sin correr Tailwind.

    Solo mira los tokens que TIENEN pinta de utilidad de Tailwind: los
    hooks de JS (`flag-bool`, `db-perm-btn`, `.tab`) no son CSS y no
    tienen por qué estar en el bundle. La lista de prefijos es de este
    proyecto, no de Tailwind entero: no busca completitud, busca no
    tener falsos positivos.
    """
    mod = _sello_clases()
    css = _BUNDLE.read_text(encoding="utf-8")

    utilidad = re.compile(
        r"^(?:hover:|focus:|md:|lg:|sm:|dark:|group-hover:|empty:|first:|last:)*"
        r"(?:text|bg|border|rounded|p|px|py|pt|pb|pl|pr|m|mx|my|mt|mb|ml|mr"
        r"|w|h|min-w|min-h|max-w|max-h|flex|grid|gap|space|items|justify"
        r"|font|leading|tracking|opacity|shadow|overflow|z|top|left|right"
        # `col`/`row` solo con sus sufijos reales: sueltos matcheaban
        # hooks como `row-channel`, que es un <td>, no una utilidad.
        r"|bottom|col-(?:span|start|end|auto)|row-(?:span|start|end|auto)"
        r"|order|cursor|select|transition|duration|truncate"
        r"|break|whitespace|align|tabular|uppercase|lowercase|capitalize"
        r"|hidden|block|inline|absolute|relative|fixed|sticky)"
        r"(?:-[\w./\[\]()%#,-]+)?$")

    faltan = []
    for clase in mod.clases_usadas():
        if not utilidad.match(clase):
            continue
        sel = "." + re.sub(r"([.\[\]().,:/%#!])", r"\\\1", clase)
        # Tailwind escapa la coma de un valor arbitrario como `\2c `.
        alterno = sel.replace(r"\,", r"\2c ")
        if not (re.search(re.escape(sel) + r"(?![\w-])", css)
                or re.search(re.escape(alterno) + r"(?![\w-])", css)):
            faltan.append(clase)

    assert not faltan, (
        "estas clases se usan en el HTML/JS y NO están en el bundle, así "
        f"que no hacen nada:\n  {', '.join(faltan)}\n"
        "  fix: pwsh mcp-server/admin_static/build-css.ps1 — y si después "
        "de rebuildear siguen faltando, es un typo (pasó con `btn-danger`, "
        "que en este repo se escribe `btn danger`)."
    )
