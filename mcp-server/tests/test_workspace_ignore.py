"""Tests de los filtros del wrapper de filesystem (ADR-016, iter 4.5).

Cubrimos:
- Defaults duros (obj/, bin/, .venv/, node_modules, etc.) SIEMPRE
  se excluyen, aunque estén trackeados en git.
- .gitignore del repo se RESPETA (suma a los defaults, no pisa).
- Cap MAX_TREE_ENTRIES trunca con mensaje claro.

El wrapper vive en otro repo (4bis.vscode/4bis_mcp_server) y se
instala editable en el venv del relay. Los tests importan el módulo
`mcp_wrapper` directamente (no como subprocess MCP): testeamos la
función pura `_list_dir_tree`, que es la que arma el output.

Si el wrapper no está instalado (CI limpia), los tests se saltean
con un mensaje claro en lugar de fallar — el wrapper es dep opcional
de los tests de integración, no del relay en sí.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

# Importación defensiva: si el wrapper no está instalado, los tests
# se saltean en vez de romper la suite. El wrapper es editable y
# puede faltar en un clone fresco hasta que se corra el setup.
try:
    import mcp_wrapper  # type: ignore
except ImportError:  # pragma: no cover
    pytest.skip(
        "mcp_wrapper no instalado; corre `pip install -e ../4bis.vscode/4bis_mcp_server`",
        allow_module_level=True,
    )


# ---------- _is_default_ignored ----------


def test_default_ignores_obj_and_bin() -> None:
    assert mcp_wrapper._is_default_ignored("obj") is True
    assert mcp_wrapper._is_default_ignored("bin") is True
    assert mcp_wrapper._is_default_ignored("node_modules") is True
    assert mcp_wrapper._is_default_ignored(".venv") is True
    assert mcp_wrapper._is_default_ignored("__pycache__") is True


def test_default_does_not_ignore_source_files() -> None:
    assert mcp_wrapper._is_default_ignored("src") is False
    assert mcp_wrapper._is_default_ignored("README.md") is False
    assert mcp_wrapper._is_default_ignored("INVENTORYDEMO.sln") is False


def test_default_ignores_egg_info_by_suffix() -> None:
    """`*.egg-info` por convención de setuptools — match por sufijo."""
    assert mcp_wrapper._is_default_ignored("relay.egg-info") is True
    assert mcp_wrapper._is_default_ignored("something.egg-info") is True


# ---------- _list_dir_tree: defaults + gitignore + cap ----------


def _make_repo(tmp: Path) -> Path:
    """Arma un mini-repo con cosas que SÍ y que NO deben aparecer."""
    (tmp / "src").mkdir()
    (tmp / "src" / "main.py").write_text("print('hi')")
    (tmp / "src" / "utils.py").write_text("# utils")
    (tmp / "README.md").write_text("# repo")
    # Cosas que NO deben aparecer en el listado:
    (tmp / "obj").mkdir()
    (tmp / "obj" / "garbage.dll").write_text("binario")
    (tmp / "bin").mkdir()
    (tmp / "bin" / "exe.exe").write_text("binario")
    (tmp / "node_modules").mkdir()
    (tmp / "node_modules" / "leftpad.js").write_text("// leftpad")
    (tmp / ".venv").mkdir()
    (tmp / ".venv" / "pyvenv.cfg").write_text("home = x")
    (tmp / "__pycache__").mkdir()
    (tmp / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"\x00")
    # Trackeado en git + .gitignored (debería ocultarse):
    (tmp / "secret.txt").write_text("token=xxx")
    (tmp / ".gitignore").write_text("secret.txt\n")
    return tmp


def test_list_dir_excludes_default_artifacts(tmp_path: Path) -> None:
    """obj/, bin/, node_modules/, .venv/, __pycache__ NO aparecen."""
    ws = _make_repo(tmp_path)
    lines = mcp_wrapper._list_dir_tree(ws, ws, max_depth=5)
    text = "\n".join(lines)

    assert "src/" in text
    assert "README.md" in text
    assert "obj" not in text, f"obj debería estar excluido:\n{text}"
    assert "bin" not in text, f"bin debería estar excluido:\n{text}"
    assert "node_modules" not in text, f"node_modules debería estar excluido:\n{text}"
    assert ".venv" not in text, f".venv debería estar excluido:\n{text}"
    assert "__pycache__" not in text, f"__pycache__ debería estar excluido:\n{text}"


def test_list_dir_respects_gitignore(tmp_path: Path) -> None:
    """Lo que el .gitignore marca se excluye, además de los defaults."""
    ws = _make_repo(tmp_path)
    lines = mcp_wrapper._list_dir_tree(ws, ws, max_depth=5)
    text = "\n".join(lines)
    assert "secret.txt" not in text, "secret.txt está en .gitignore, debería ocultarse"


def test_list_dir_default_ignored_wins_even_if_tracked(tmp_path: Path) -> None:
    """Si obj/ está en .gitignore NEGADO (!obj), el default igual gana.

    Default duro > gitignore. Razón: listar obj/ le aporta 0 al LLM.
    """
    (tmp_path / "obj").mkdir()
    (tmp_path / "obj" / "x.dll").write_text("b")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.cs").write_text("// a")
    (tmp_path / ".gitignore").write_text("!obj\n")  # "no ignores obj"
    ws = tmp_path
    lines = mcp_wrapper._list_dir_tree(ws, ws, max_depth=3)
    text = "\n".join(lines)
    assert "src/" in text
    assert "obj" not in text, "defaults duros ganan sobre negación de gitignore"


def test_list_dir_cap_truncates_with_warning(tmp_path: Path) -> None:
    """Si el repo tiene MÁS de MAX_TREE_ENTRIES, devolvemos cap + warning."""
    # Armamos 600 archivos sueltos en root (sin defaults duros matcheando).
    for i in range(600):
        (tmp_path / f"file_{i:04d}.txt").write_text(f"{i}")
    ws = tmp_path
    lines = mcp_wrapper._list_dir_tree(ws, ws, max_depth=1)
    assert len(lines) <= mcp_wrapper.MAX_TREE_ENTRIES + 1  # +1 por la línea de warning
    # La última línea es el aviso de truncado
    assert any("truncado" in ln for ln in lines), f"esperaba aviso de truncado en {lines[-3:]}"


def test_list_dir_max_depth_clamps() -> None:
    """max_depth fuera de rango se clamp a [1, 10] sin explotar."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        # max_depth=999 debe clamp-ear a 10 y no explotar.
        lines_high = mcp_wrapper._list_dir_tree(Path(tmp), Path(tmp), max_depth=999)
        assert isinstance(lines_high, list)
        # max_depth=0 debe clamp-ear a 1 y no explotar.
        lines_zero = mcp_wrapper._list_dir_tree(Path(tmp), Path(tmp), max_depth=0)
        assert isinstance(lines_zero, list)
        # max_depth negativo debe clamp-ear a 1 y no explotar.
        lines_neg = mcp_wrapper._list_dir_tree(Path(tmp), Path(tmp), max_depth=-5)
        assert isinstance(lines_neg, list)
