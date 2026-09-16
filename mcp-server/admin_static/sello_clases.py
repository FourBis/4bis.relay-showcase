"""Sella admin.css con el hash de las CLASES que usan las fuentes.

Por qué existe, además del sello de `admin.src.css` que ya había: ese
sello detecta "editaste la fuente CSS y no rebuildeaste". No detecta el
otro olvido, que es igual de silencioso y pasó de verdad el 2026-08-16:
**agregar una clase de Tailwind en el HTML o en un .js y no rebuildear**.
El bundle no la tiene, la clase no hace nada, y no falla nada — la UI
simplemente se ve un poco distinta de lo que alguien escribió.

Qué se hashea: el conjunto ORDENADO de tokens de clase que aparecen en
los mismos archivos que escanea Tailwind (`content` de
tailwind.admin.config.js). El conjunto y no los archivos: así editar
lógica de un .js no pide un rebuild de CSS —sería un guard que grita en
falso, y un guard que grita en falso enseña a ignorarlo— pero agregar o
sacar una clase sí.

**Una sola implementación, dos llamadores.** `build-css.ps1` invoca este
script y el test lee el sello que dejó. Tener el cálculo escrito dos
veces (PowerShell + Python) es exactamente cómo nació el bug de CRLF del
2026-08-16: dos implementaciones del mismo hash que discrepaban, y un
test que acusaba a un bundle que estaba al día.

Uso:
    python sello_clases.py            # imprime el hash
    python sello_clases.py --write    # lo escribe en static/admin.css
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
BUNDLE = AQUI / "static" / "admin.css"
MARCA = re.compile(r"/\*!classes-sha256:([0-9a-f]{64})\*/")

# Los mismos archivos que escanea Tailwind (content en la config).
def _fuentes() -> list[Path]:
    return [AQUI / "index.html"] + sorted((AQUI / "static").glob("*.js"))


def _tokens(texto: str, es_js: bool) -> set[str]:
    """Tokens de clase de un archivo.

    Aproximado a propósito y no un parser: alcanza con que sea
    DETERMINISTA y que cubra cómo se escriben las clases en este repo
    (`class="…"` en el HTML, y en los .js los mismos literales adentro de
    template strings, más `className=` y `classList.add/remove/toggle`).
    Un token interpolado (`text-${color}-400`) se descarta: Tailwind
    tampoco lo ve, así que incluirlo haría ruido sin detectar nada.
    """
    if es_js:
        bloques = re.findall(r'class(?:Name)?\s*=\s*["\'`]([^"\'`]*)["\'`]', texto)
        bloques += re.findall(r'classList\.(?:add|remove|toggle)\(([^)]*)\)', texto)
    else:
        bloques = re.findall(r'class="([^"]*)"', texto)
    salida = set()
    for b in bloques:
        for tok in re.split(r"[\s,'\"]+", b):
            tok = tok.strip()
            if tok and "${" not in tok and "+" not in tok:
                salida.add(tok)
    return salida


def clases_usadas() -> list[str]:
    todas: set[str] = set()
    for f in _fuentes():
        todas |= _tokens(f.read_text(encoding="utf-8", errors="replace"),
                         f.suffix == ".js")
    return sorted(todas)


def sello() -> str:
    return hashlib.sha256("\n".join(clases_usadas()).encode("utf-8")).hexdigest()


def escribir(h: str) -> None:
    txt = BUNDLE.read_text(encoding="utf-8")
    marca = f"/*!classes-sha256:{h}*/"
    txt = MARCA.sub("", txt).rstrip() + marca
    BUNDLE.write_text(txt, encoding="utf-8", newline="")


if __name__ == "__main__":
    h = sello()
    if "--write" in sys.argv:
        escribir(h)
        print(f"admin.css sellado con classes-sha256:{h}")
    else:
        print(h)
