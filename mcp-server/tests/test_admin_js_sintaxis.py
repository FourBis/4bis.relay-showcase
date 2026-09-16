"""Todo `.js` de la Admin UI tiene que parsear como ES module (2026-09-04).

El día que se escribió esto, `develop` tenía el panel **entero caído** por
dos errores de sintaxis que nadie vio:

  1. `tab-diagrams.js` — `stateDiagram-v2: "..."` como clave de objeto sin
     comillas: `SyntaxError: Unexpected token '-'`.
  2. `tab-status.js` — un refactor borró las líneas que definían `r` y
     `tbody` y dejó un `return;` + `}` huérfanos que cerraban el `try`
     antes de tiempo: `SyntaxError: Missing catch or finally after try`.

`main.js` importa los dos de forma estática, así que cualquiera de los dos
por separado mataba el arranque completo: nada de JS corría, el health
quedaba en "sondeando…" para siempre y el nav no respondía.

**Por qué no lo agarró nadie.** Se había corrido `node --check` sobre los
33 archivos y dio verde. Es un falso negativo: sin `"type": "module"` en el
`package.json` del directorio, Node valida el archivo como script CommonJS,
no como el `<script type="module">` que usa el navegador. Ahí `import`/
`export` y las reglas de módulo no se aplican igual, y los dos errores
pasan. La única forma de que Node valide con semántica de módulo es que la
extensión sea `.mjs` — de ahí la copia a un temporal.

`node --check` solo PARSEA: no resuelve imports ni ejecuta nada. Por eso la
copia fuera del árbol no rompe los `import ./api.js` de al lado, y por eso
el test es barato (~33 procesos, ningún side effect).

Mismo criterio que test_js_suite.py y test_admin_css_build.py: un test de
pytest que guarda un artefacto que no es Python, porque la suite es el
único lugar por el que todo el mundo pasa.

Se saltea si no hay `node` en el PATH, igual que su hermano.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_admin_js_sintaxis.py -q
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

STATIC_DIR = (Path(__file__).resolve().parent.parent
              / "admin_static" / "static")
JS = sorted(STATIC_DIR.glob("*.js"))


def test_hay_js_de_admin():
    """Si el glob deja de matchear (se movió la carpeta, se renombró la
    extensión), el test de abajo pasaría vacío y diría 'verde' sobre cero
    archivos — que es exactamente el modo de falla que estamos tapando."""
    assert JS, f"no encontré ningún .js en {STATIC_DIR}"


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node no está en el PATH")
@pytest.mark.parametrize("js", JS, ids=lambda p: p.name)
def test_parsea_como_es_module(js: Path):
    """Un caso por archivo: si falla uno, el reporte nombra cuál en vez de
    decir "el JS de admin está roto"."""
    with tempfile.TemporaryDirectory() as tmp:
        copia = Path(tmp) / (js.stem + ".mjs")
        copia.write_bytes(js.read_bytes())
        proc = subprocess.run(
            ["node", "--check", str(copia)],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
    if proc.returncode != 0:
        # node imprime el fragmento culpable + el caret en stderr; con el
        # nombre del temporal adentro, que no le sirve a nadie.
        detalle = (proc.stderr or "").replace(str(copia), js.name)
        pytest.fail(
            f"{js.name} no parsea como ES module (el navegador lo carga "
            f"con <script type=\"module\">)\n{detalle.strip()[:2000]}")
