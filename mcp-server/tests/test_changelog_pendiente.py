"""Check de mcp-server/scripts/changelog_pendiente.py (2026-09-04).

Todo el script depende de una sola función: encontrarle a CHANGELOG.md
la fecha más reciente para saber desde dónde pedirle commits a git. Si
esa función se rompe (por ejemplo porque alguien "mejora" el formato de
los headings), el script arranca desde la fecha equivocada en silencio
— no explota, así que no hay señal salvo este test. No corre `git`: eso
requeriría un repo de verdad, y lo que puede romperse acá es el regex,
no la llamada a git.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from changelog_pendiente import ultima_fecha_changelog  # noqa: E402


def test_heading_nuevo_y_viejo_y_con_texto_despues_de_la_fecha():
    texto = "\n".join([
        "## Iter 11 — verificación, documentador y consolidación — 2026-08-10",
        "## El panel muestra el plan — 2026-08-23",
        "## Iter 10.1 — 2026-07-19 (canal Discord default por proyecto)",
    ])
    assert ultima_fecha_changelog(texto) == "2026-08-23"


def test_sin_fechas_no_explota():
    assert ultima_fecha_changelog("# Nada acá\n\nsolo texto suelto.") is None
