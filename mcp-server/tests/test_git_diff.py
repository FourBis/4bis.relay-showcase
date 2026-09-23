"""Tests de _capture_git_diff (ADR-020, iter 4.6).

Cubrimos:
- Repo sin git → {"ok": False, "status": "not_git_repo"} + bloque vacío.
- Repo git clean → bloque con "Working tree clean".
- Repo con archivos modificados → bloque con diff truncable.
- Cap de 20KB funciona y emite footer.
- block_in_text_format devuelve "" si no es repo git.
"""
from __future__ import annotations
from relay import expert_git, expert_instructions

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "mcp-server" / "src"))

from relay import experts  # noqa: E402


def _make_git_repo(tmp: Path) -> Path:
    """Inicializa un repo git mínimo con un archivo committed + dirty."""
    (tmp / "README.md").write_text("# init\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.email", "test@x"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=tmp, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp, check=True)
    (tmp / "README.md").write_text("# init\nmodified\n")
    (tmp / "new.txt").write_text("nuevo")
    return tmp


def test_capture_returns_not_git_repo_for_non_git(tmp_path: Path) -> None:
    """Un directorio sin .git devuelve ok=False."""
    info = expert_git._capture_git_diff_sync(str(tmp_path))
    assert info["ok"] is False
    assert info["status"] == "not_git_repo"


def test_capture_git_diff_clean_tree(tmp_path: Path) -> None:
    """Repo sin cambios devuelve status vacío y diff vacío."""
    (tmp_path / "a.txt").write_text("a")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=tmp_path, check=True)
    info = expert_git._capture_git_diff_sync(str(tmp_path))
    assert info["ok"] is True
    assert info["status"] == ""
    assert info["diff"] == ""


def test_capture_git_diff_modified_and_untracked(tmp_path: Path) -> None:
    """Repo con un archivo modificado y uno nuevo devuelve ambos en status."""
    _make_git_repo(tmp_path)
    info = expert_git._capture_git_diff_sync(str(tmp_path))
    assert info["ok"] is True
    assert "README.md" in info["status"]
    assert "?? new.txt" in info["status"]
    assert "modified" in info["diff"]


def test_build_block_returns_empty_for_non_git(tmp_path: Path) -> None:
    """Sin git, build devuelve '' (no se incluye en el system)."""
    block = expert_git._build_git_diff_block_sync(str(tmp_path))
    assert block == ""


def test_build_block_clean_repo(tmp_path: Path) -> None:
    """Repo limpio incluye 'Working tree clean'."""
    _make_git_repo(tmp_path)
    # commitear también new.txt para que quede clean
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "tweak"], cwd=tmp_path, check=True)
    block = expert_git._build_git_diff_block_sync(str(tmp_path))
    assert "Working tree clean" in block
    assert "Branch:" in block
    assert "HEAD:" in block


def test_build_block_with_changes(tmp_path: Path) -> None:
    """Repo con cambios produce bloque con status y diff."""
    _make_git_repo(tmp_path)
    block = expert_git._build_git_diff_block_sync(str(tmp_path))
    assert "## Cambios en el workspace (git)" in block
    assert "Status" in block
    assert "Diff" in block
    assert "```diff" in block
    assert "modified" in block


def test_build_block_truncates_huge_diff(tmp_path: Path, monkeypatch) -> None:
    """Si diff > 20KB, se trunca con footer."""
    _make_git_repo(tmp_path)
    # Generar un archivo grandote que ensucie el diff
    (tmp_path / "big.txt").write_text("x" * 30_000)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    # Tweak adicional para que el diff sea > 20KB
    (tmp_path / "big.txt").write_text("y" * 30_000)

    # Bajar el cap a 1KB para testear rápido
    monkeypatch.setattr(expert_git, "DIFF_MAX_BYTES", 1000)
    block = expert_git._build_git_diff_block_sync(str(tmp_path))
    assert "Diff truncado" in block


def test_system_prompt_no_lleva_el_diff(tmp_path: Path) -> None:
    """El diff NO viaja en el system prompt (2026-07-28).

    Iba ahí desde ADR-020, pero cambia en cuanto el experto escribe un
    archivo: el prefijo deja de ser estable y el provider tira la cache
    del historial entero en cada resume (0% de hit medido en relay.db
    contra 94% de los runs que no lo tocaban). Ahora es `git_diff()`.
    """
    _make_git_repo(tmp_path)
    project = {
        "slug": "test", "repo_path": str(tmp_path),
        "system_prompt": "", "mcp_servers": [], "native_tools": [],
    }
    out = expert_instructions.build_instructions(project, "", "")
    assert "## Cambios en el workspace (git)" not in out
    # el bloque sigue existiendo — solo cambió quién lo pide
    assert "modified" in expert_git._build_git_diff_block_sync(str(tmp_path))
