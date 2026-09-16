"""Los tests JS de la Admin UI corren dentro de la suite (2026-08-18).

Existían cuatro `*.test.mjs` sin runner: pytest no los veía y no hay CI
que los pida, así que solo corrían cuando alguien se acordaba de tipear
`node --test` a mano.

Eso no es teórico. `chat-strip-junk.test.mjs` estuvo en ROJO sin que
nadie se enterara: el selector de modelo del chat metió un
`localStorage.getItem(...)` al top-level de `tab-chats.js`, y ese
archivo lo importa el test desde Node, donde no existe `localStorage`.
El módulo moría con ReferenceError antes de llegar a `stripJunk`. Un
test que nadie corre no es una red, es un adorno.

Mismo criterio que test_admin_css_build.py: un test de pytest que
guarda un artefacto que no es Python. La suite es el único lugar por el
que todo el mundo pasa.

Se saltea si no hay `node` en el PATH — no vale romperle la suite a
quien no lo tenga instalado; el guard es para la máquina que sí puede.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
MJS = sorted(TESTS_DIR.glob("*.test.mjs"))


def test_hay_tests_js():
    """Si el glob deja de matchear, el test de abajo pasaría vacío y
    diría 'verde' sobre cero archivos."""
    assert MJS, f"no encontré ningún *.test.mjs en {TESTS_DIR}"


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node no está en el PATH")
@pytest.mark.parametrize("mjs", MJS, ids=lambda p: p.name)
def test_suite_js(mjs: Path):
    """Un caso de pytest por archivo: si falla uno, el reporte nombra
    cuál en vez de decir "los tests JS fallaron"."""
    proc = subprocess.run(
        ["node", "--test", str(mjs)],
        capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        # La salida TAP de node es larga y mete el módulo entero como
        # data: URL cuando el import explota. El tail es donde está el
        # resumen; el ReferenceError va en el stdout completo, así que
        # se recorta y no se tira.
        cola = "\n".join((proc.stdout or "").splitlines()[-40:])
        pytest.fail(
            f"node --test {mjs.name} salió con {proc.returncode}\n"
            f"--- stdout (últimas 40 líneas) ---\n{cola}\n"
            f"--- stderr ---\n{(proc.stderr or '')[-2000:]}")
