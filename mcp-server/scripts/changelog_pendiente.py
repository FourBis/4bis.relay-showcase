"""Genera un borrador de lo que falta volcar al CHANGELOG (2026-09-04).

El CHANGELOG (`docs/CHANGELOG.md`) es prosa curada a mano: títulos que
explican el *por qué*, no un dump de mensajes de commit. Eso vale la pena
mantenerlo así — pero juntar y ordenar "qué pasó desde la última entrada"
es trabajo mecánico que se posterga, y por eso el CHANGELOG se atrasa.

Este script NO reemplaza la prosa. Genera un borrador (`docs/
CHANGELOG_PENDIENTE.md`) con los commits de `develop` que quedaron fuera
del CHANGELOG, agrupados y con su `--stat`, para que un humano los lea y
escriba las entradas de verdad. Nunca toca CHANGELOG.md ni escribe nada
si no hay commits nuevos.

Cómo encuentra "hasta dónde llega" el CHANGELOG: busca la fecha más
reciente en los headings `## ... — YYYY-MM-DD` (el formato viejo
`## Iter N — ... — YYYY-MM-DD` también cae acá porque solo se busca la
fecha, no la posición exacta dentro del heading — hay headings con texto
después de la fecha, tipo `## Iter 10.1 — 2026-07-19 (canal Discord...)`).

    python mcp-server/scripts/changelog_pendiente.py     # desde donde sea
"""
from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = REPO_ROOT / "docs" / "CHANGELOG.md"
DRAFT = REPO_ROOT / "docs" / "CHANGELOG_PENDIENTE.md"

DATE_RE = re.compile(r"^## .*?(\d{4}-\d{2}-\d{2})", re.MULTILINE)
CONVENTIONAL_RE = re.compile(r"^(feat|fix|docs|chore|refactor|test)(\([^)]*\))?!?:\s*(.+)$")

TIPO_TITULO = {
    "feat": "feat",
    "fix": "fix",
    "docs": "docs",
    "refactor": "refactor",
    "test": "test",
    "chore": "chore",
    "otros": "sin Conventional Commits",
}
TIPO_ORDEN = ["feat", "fix", "refactor", "test", "docs", "chore", "otros"]

# ponytail: si hay más de 20 días distintos con commits, agrupar por
# semana ISO en vez de por día para que el borrador siga siendo
# hojeable. Un solo umbral, sin config.
UMBRAL_DIAS_PARA_SEMANA = 20


def ultima_fecha_changelog(texto: str) -> str | None:
    """Fecha más reciente (YYYY-MM-DD) entre los headings `## ...` del CHANGELOG.

    No le importa el formato exacto del heading (viejo o nuevo, con o sin
    texto después de la fecha) porque busca la fecha en cualquier parte
    de la línea. Devuelve None si no encuentra ninguna.
    """
    fechas = DATE_RE.findall(texto)
    return max(fechas) if fechas else None


def _git(*args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} fallo: {r.stderr.strip()}")
    return r.stdout


def commits_desde(fecha: str, rama: str = "develop") -> list[dict]:
    """Commits de `rama` con fecha posterior a `fecha` (día completo), en orden cronológico.

    --no-merges: los "Merge pull request #N" no aportan nada al borrador,
    los commits que mergean ya aparecen listados individualmente.
    """
    salida = _git(
        "log", "--no-merges", "--date=short",
        f"--since={fecha}T23:59:59",
        "--reverse", "--pretty=format:%h|%ad|%s", rama,
    )
    commits = []
    for linea in salida.splitlines():
        if not linea.strip():
            continue
        h, fecha_c, subject = linea.split("|", 2)
        commits.append({"hash": h, "fecha": fecha_c, "subject": subject})
    return commits


def stat_de(hash_: str) -> str:
    return _git("show", "--stat", "--format=", hash_).strip()


def tipo_de(subject: str) -> str:
    m = CONVENTIONAL_RE.match(subject)
    return m.group(1) if m else "otros"


def _semana_iso(fecha: str) -> str:
    y, m, d = map(int, fecha.split("-"))
    iso = date(y, m, d).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def agrupar(commits: list[dict]) -> tuple[dict, bool]:
    dias = {c["fecha"] for c in commits}
    por_semana = len(dias) > UMBRAL_DIAS_PARA_SEMANA
    clave_de = _semana_iso if por_semana else (lambda f: f)

    grupos: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for c in commits:
        grupos[clave_de(c["fecha"])][tipo_de(c["subject"])].append(c)
    return grupos, por_semana


def render(grupos: dict, por_semana: bool, fecha_base: str, total: int) -> str:
    hoy = datetime.now().strftime("%Y-%m-%d %H:%M")
    unidad = "semana" if por_semana else "día"
    partes = [
        "# Borrador de cambios pendientes (autogenerado)\n",
        "**Esto NO es parte de `docs/CHANGELOG.md`.** Es un insumo para que",
        "un humano escriba ahí las entradas en prosa, con el criterio editorial",
        "de siempre — no reemplaza eso, solo saca la fricción de juntar y",
        "ordenar los commits.\n",
        f"Se regenera con `python mcp-server/scripts/changelog_pendiente.py`.",
        "**No lo edites a mano**: la próxima corrida lo pisa entero.\n",
        f"Cubre `develop` después de **{fecha_base}** (última fecha vista en",
        f"CHANGELOG.md) hasta ahora ({hoy}). {total} commits, agrupados por {unidad}",
        "y por tipo de Conventional Commit.\n",
        "---\n",
    ]
    for clave in sorted(grupos.keys()):
        por_tipo = grupos[clave]
        n_dia = sum(len(v) for v in por_tipo.values())
        partes.append(f"## {clave} ({n_dia} commits)\n")
        for tipo in TIPO_ORDEN:
            commits = por_tipo.get(tipo)
            if not commits:
                continue
            partes.append(f"### {TIPO_TITULO[tipo]} ({len(commits)})\n")
            for c in commits:
                partes.append(f"- `{c['hash']}` {c['subject']}")
                stat = stat_de(c["hash"])
                if stat:
                    partes.append("  ```")
                    for linea in stat.splitlines():
                        partes.append(f"  {linea}")
                    partes.append("  ```")
            partes.append("")
    return "\n".join(partes) + "\n"


def main() -> int:
    if not CHANGELOG.exists():
        print(f"No encuentro {CHANGELOG}", file=sys.stderr)
        return 1

    texto = CHANGELOG.read_text(encoding="utf-8")
    fecha_base = ultima_fecha_changelog(texto)
    if fecha_base is None:
        print("No encontré ninguna fecha (## ... — YYYY-MM-DD) en el CHANGELOG.", file=sys.stderr)
        return 1

    commits = commits_desde(fecha_base)
    if not commits:
        print(f"CHANGELOG al día: no hay commits en develop después de {fecha_base}.")
        return 0

    grupos, por_semana = agrupar(commits)
    DRAFT.write_text(render(grupos, por_semana, fecha_base, len(commits)), encoding="utf-8")
    print(f"Escribí {DRAFT} con {len(commits)} commits desde {fecha_base}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
