"""Read-only fs helpers para auditar repos clonados.

Extraído del wrapper MCP de 4bis.vscode (mcp_wrapper.py) en iter 9.4
(2026-07-18). Las funciones son las MISMAS — mismas defaults duros
de exclusion (.git, node_modules, bin, obj, etc.), misma heurística de
.gitignore. Lo único que sacamos es todo lo que dependía del SDK
`mcp.*` (Server, list_tools decorator, TextContent wrappers). Eso nos
permite usarlas en proceso desde el relay sin sumar deps.

Por qué existe:
- El install-on-demand de MCPs necesita leer el código del clon para
  decidir si es seguro (`vet_with_llm`).
- El LLM con el que votamos verdict+reasons NO tiene read_file en su
  Agent (el proyecto __vet__ no tiene mcp_servers). Antes caíamos a
  "unknown" + pedir confirmación humana, lo que chamuya al usuario
  en cada install.
- Solución: el relay lee el árbol + los archivos clave en Python (esto,
  deterministic + read-only), le pasa al LLM un resumen estructurado,
  y el LLM solo interpreta y vota. Read-only en el relay: el auditor
  jamas corre código del clon, ni siquiera lo lee más allá de los
  archivos clave.

Ponytail:
- Sin clase, sin abstract factory. Funciones planas + dataclasses si hace
  falta.
- Mismas firmas que las del wrapper original (read_file(path) → str,
  list_dir(path, max_depth) → str con árbol textual). Si en el futuro
  reusamos el wrapper como MCP server, swapear las llamadas es trivial.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Cap de defensa (256 KB). Mismo valor que el wrapper original.
MAX_READ_BYTES = 256 * 1024

# Cap del listado (500 entries). Mismo valor que el wrapper original.
MAX_TREE_ENTRIES = 500


# --- Defaults duros (ADR-016, 4bis.relay) -----------------------------
# SIEMPRE excluidos, aunque estén en git. El .gitignore solo AGREGA
# a esta lista (no la reemplaza). Esto evita que un experto consuma
# 60K tokens en paths de artefactos de build.

_DEFAULT_IGNORED_DIRS = frozenset({
    # VCS / metadata
    ".git", ".hg", ".svn",
    # Python
    "__pycache__", ".venv", "venv", "env", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".nox", "eggs", "*.egg-info",
    # Node / JS
    "node_modules", ".next", ".nuxt", ".cache", ".parcel-cache",
    # .NET
    "bin", "obj", "Debug", "Release", "packages", "publish",
    # Java / JVM
    "target", ".gradle", "build", ".idea", ".settings",
    # Go
    "vendor",
    # Rust
    "target",
    # IDEs / OS
    ".vscode", ".idea", ".vs", ".DS_Store", "Thumbs.db",
})

_DEFAULT_IGNORED_FILES = frozenset({
    ".DS_Store", "Thumbs.db", "desktop.ini",
    # Coverage / lock transitorios
    ".coverage", "coverage.json", "*.log",
})


def _is_default_ignored(name: str) -> bool:
    if name in _DEFAULT_IGNORED_DIRS or name in _DEFAULT_IGNORED_FILES:
        return True
    if name.endswith(".egg-info"):
        return True
    return False


# --- .gitignore (heurístico) ------------------------------------------

def _is_gitignored(ws: Path, target: Path) -> bool:
    """Heurística barata: parsea el .gitignore del root.

    No es un parser completo (reglas con `**` están fuera de scope). Para
    tooling alcanza con: líneas no vacías, sin `#`, que matcheen el
    basename o el path relativo por fnmatch. Falso negativo > falso
    positivo.
    """
    gi = ws / ".gitignore"
    if not gi.is_file():
        return False
    try:
        lines = gi.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    rel = target.relative_to(ws).as_posix()
    name = target.name
    matched = False
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        rule = s[1:].strip() if s.startswith("!") else s
        negated = s.startswith("!")
        # Quitar slash final (suele aparecer en directorios: `node_modules/`)
        if rule.endswith("/"):
            rule = rule[:-1]
        # Regla anclada al root: empieza con `/`. Quitar el slash y
        # exigir que matchee contra el path relativo sin posibilidad de
        # anidar.
        anchored_to_root = rule.startswith("/")
        if anchored_to_root:
            rule = rule[1:]
            rule_rel = rule
        else:
            rule_rel = rule
        rule_matches = (
            fnmatch.fnmatch(name, rule)
            or fnmatch.fnmatch(rel, rule_rel)
            or fnmatch.fnmatch("/" + rel, "/" + rule_rel)
        )
        if rule_matches:
            if negated:
                return False  # override explícito
            matched = True
    return matched


def _is_ignored(ws: Path, p: Path) -> bool:
    if _is_default_ignored(p.name):
        return True
    if _is_gitignored(ws, p):
        return True
    return False


# --- Resolve (anti `..` escape) ---------------------------------------

def _resolve(ws: Path, rel_or_abs: str) -> Path:
    """Resuelve una ruta relativa contra el workspace y verifica que el
    resultado siga adentro. Permitimos absolutas SOLO si ya están
    adentro del workspace.
    """
    p = Path(rel_or_abs)
    if not p.is_absolute():
        p = ws / p
    p = p.resolve()
    ws_str = str(ws)
    p_str = str(p)
    if p_str != ws_str and not p_str.startswith(ws_str + os.sep):
        raise ValueError(f"path fuera del workspace: {rel_or_abs}")
    return p


# --- Read ------------------------------------------------------------

@dataclass
class ReadResult:
    ok: bool
    content: str = ""
    error: str = ""


def read_file(ws: Path, path: str) -> ReadResult:
    """Lee el contenido de un archivo dentro del workspace.

    Falla si excede MAX_READ_BYTES (el caller decide: el auditor del MCP
    quiere saber, el humano lo lee igual).
    """
    try:
        p = _resolve(ws, path)
    except ValueError as e:
        return ReadResult(ok=False, error=str(e))
    if not p.is_file():
        return ReadResult(ok=False, error=f"no es archivo regular: {p}")
    size = p.stat().st_size
    if size > MAX_READ_BYTES:
        return ReadResult(
            ok=False,
            error=(f"archivo pesa {size} bytes, excede cap de {MAX_READ_BYTES}. "
                   f"Pide un sub-rango o usa grep_search / file_search."),
        )
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return ReadResult(ok=False, error=f"no pude leer {p}: {e}")
    return ReadResult(ok=True, content=content)


# --- List ------------------------------------------------------------

def list_dir_tree(ws: Path, root: Path, max_depth: int) -> str:
    """Genera un árbol textual. depth=1 → solo root, depth=2 → hijos, etc.

    Aplica DOS filtros (ADR-016):
    1. Defaults duros: SIEMPRE excluidos (obj/, bin/, .venv/, etc.).
    2. .gitignore del repo: respeta lo que el dev marcó.
    Cap MAX_TREE_ENTRIES para defendernos de repos sin .gitignore y
    miles de archivos.
    """
    if max_depth < 1:
        max_depth = 1
    if max_depth > 10:
        max_depth = 10
    out: list[str] = []
    base_depth = len(root.relative_to(ws).parts) if root != ws else 0
    truncated = False

    def walk(p: Path, depth: int) -> None:
        nonlocal truncated
        if len(out) >= MAX_TREE_ENTRIES:
            truncated = True
            return
        if depth > max_depth:
            return
        rel = p.relative_to(ws)
        prefix = "  " * (len(rel.parts) - base_depth - 1) if rel.parts else ""
        marker = "[D] " if p.is_dir() else "[F] "
        out.append(f"{prefix}{marker}{p.name}{'/' if p.is_dir() else ''}")
        if not p.is_dir():
            return
        try:
            entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except (PermissionError, OSError):
            out.append(f"{prefix}  [permiso denegado]")
            return
        for entry in entries:
            if entry.name.startswith("."):
                # Dejamos pasar .gitignore (info útil) pero NO .git/.vscode/etc.
                if entry.name == ".gitignore":
                    walk(entry, depth)
                continue
            if _is_ignored(ws, entry):
                continue
            walk(entry, depth + 1)

    walk(root, 0)
    if truncated:
        out.append(
            f"⚠ listado truncado a {MAX_TREE_ENTRIES} entries. "
            f"Aplica un filtro más fino (path más específico, menor max_depth) "
            f"o agrega exclusiones al .gitignore del repo."
        )
    return "\n".join(out) if out else "(directorio vacío)"


def list_dir(ws: Path, path: str = ".", max_depth: int = 3) -> str:
    """Wrapper con sintaxis friendly: `list_dir(ws, path=".py", max_depth=2)`.

    Path relativo al workspace, o absoluto adentro.
    """
    try:
        p = _resolve(ws, path or ".")
    except ValueError as e:
        return f"error: {e}"
    if not p.is_dir():
        return f"error: no es directorio: {p}"
    return list_dir_tree(ws, p, max_depth)


# --- Helper "audit a repo" -------------------------------------------
# Esto es lo que el install MCP usa para generar el resumen que se le
# pasa al LLM. No es parte del wrapper — es una composición de read +
# list_dir pensada para una sola pasada.

@dataclass
class RepoSummary:
    """Resumen de un clon, listo para mandarle al LLM auditor."""
    name: str
    tree: str
    key_files: dict[str, ReadResult]
    total_size_kb: int

    def to_prompt_text(self) -> str:
        """Render a texto para meter en el user message del auditor."""
        out = [f"Repo: {self.name}", "", "─── ESTRUCTURA ───", self.tree]
        if self.key_files:
            out.append("")
            out.append("─── ARCHIVOS CLAVE ───")
            for path, r in self.key_files.items():
                if r.ok:
                    out.append(f"\n### {path}\n{r.content}")
                else:
                    out.append(f"\n### {path}\n(error: {r.error})")
        return "\n".join(out)


# Candidatos a "archivo clave": los que casi seguro sirven para auditar
# un MCP server. No es exhaustivo — si none matchea, igual sirve el árbol.
_KEY_FILE_CANDIDATES = (
    "README.md", "README", "README.rst", "README.txt",
    "package.json", "pyproject.toml", "setup.py", "Cargo.toml",
    "go.mod",
    # MCP-specific (los que el LLM debería chequear sí o sí)
    "manifest.json",
    "src/index.ts", "src/index.js", "src/server.ts", "src/server.py",
    "src/main.py", "src/main.ts",
    "index.ts", "index.js", "main.py",
)

# Prioridad de "entry point": el primero que matchea es el más informativo
# para entender qué corre el binario del clon.
_ENTRY_POINT_HINTS = (
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
)


def _pick_entry_point(ws: Path) -> str | None:
    for rel in _ENTRY_POINT_HINTS:
        if (ws / rel).is_file():
            return rel
    return None


def summarize(ws: Path, max_depth: int = 3,
              key_files: Iterable[str] = _KEY_FILE_CANDIDATES) -> RepoSummary:
    """Genera el resumen estructurado del clon.

    - Lista el árbol (filtrando defaults duros + .gitignore).
    - Lee los archivos clave (los que están). No rompe si alguno falla.
    - Calcula tamaño total (estimación: suma recursiva de file size).

    read-only: no escribe nada. Lo único que toca el FS es listar y leer.
    """
    tree = list_dir(ws, ".", max_depth=max_depth)
    keys: dict[str, ReadResult] = {}
    for rel in key_files:
        if (ws / rel).is_file():
            keys[rel] = read_file(ws, rel)
    # Si package.json/pyproject.toml está entre los leídos, ya tenemos
    # el entry point. Si no, pegar el tree igual.
    total = 0
    try:
        for p in ws.rglob("*"):
            if not p.is_file():
                continue
            if _is_ignored(ws, p):
                continue
            try:
                total += p.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return RepoSummary(
        name=ws.name,
        tree=tree,
        key_files=keys,
        total_size_kb=total // 1024,
    )


# Public API ------------------------------------------------------------
__all__ = [
    "MAX_READ_BYTES",
    "MAX_TREE_ENTRIES",
    "ReadResult",
    "RepoSummary",
    "read_file",
    "list_dir",
    "summarize",
]


if __name__ == "__main__":  # pragma: no cover
    # Mini-selfcheck: leer un file, listar un dir, resumir. Útil para
    # depurar y para que los tests rápidos no necesiten pytest.
    import sys
    if len(sys.argv) < 2:
        print("uso: python repo_reader.py <dir>")
        raise SystemExit(2)
    target = Path(sys.argv[1]).resolve()
    print("#", target)
    print(list_dir(target, ".", 2))
    print("---")
    s = summarize(target, max_depth=2)
    print(f"total ~ {s.total_size_kb} KB")
    for path in s.key_files:
        r = s.key_files[path]
        print(f"[{path}] ok={r.ok} len={len(r.content)} err={r.error[:80]!r}")
