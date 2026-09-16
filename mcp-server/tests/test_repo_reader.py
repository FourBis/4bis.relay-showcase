"""Tests para repo_reader.py (iter 9.4)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from relay.repo_reader import (
    MAX_READ_BYTES, MAX_TREE_ENTRIES,
    ReadResult, RepoSummary,
    read_file, list_dir, summarize,
    _is_default_ignored, _is_gitignored,
)


@pytest.fixture
def fake_repo():
    """Crea un repo sintético con .gitignore, defaults excluidos, etc."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        # Archivos legítimos
        (ws / "README.md").write_text("# Fake repo\n\nEsto es una prueba.", encoding="utf-8")
        (ws / "package.json").write_text('{"name": "fake-mcp", "version": "0.1.0"}', encoding="utf-8")
        (ws / "src").mkdir()
        (ws / "src" / "server.py").write_text("# MCP server entry\nprint('hello')", encoding="utf-8")
        # Archivos de build que NO deberían aparecer en el listado
        (ws / "node_modules").mkdir()
        (ws / "node_modules" / "left-pad").write_text("module", encoding="utf-8")
        (ws / "__pycache__").mkdir()
        (ws / "__pycache__" / "x.cpython-312.pyc").write_text("bytecode", encoding="utf-8")
        (ws / ".gitignore").write_text(
            "*.log\n"
            "/private-data/\n",
            encoding="utf-8",
        )
        (ws / "secret.log").write_text("oh no", encoding="utf-8")
        (ws / "private-data").mkdir()
        (ws / "private-data" / "secrets.txt").write_text("pwn", encoding="utf-8")
        yield ws


def test_read_file_basic(fake_repo):
    r = read_file(fake_repo, "README.md")
    assert r.ok
    assert "Fake repo" in r.content


def test_read_file_missing(fake_repo):
    r = read_file(fake_repo, "no-existe.md")
    assert not r.ok
    assert "no es archivo" in r.error


def test_read_file_path_outside(fake_repo):
    # El resolver anti-`..` esc debería rechazar paths fuera del ws.
    r = read_file(fake_repo, "..")
    # .. resuelve al tmpdir padre, NO adentro de ws.
    assert not r.ok
    assert "fuera del workspace" in r.error


def test_read_file_too_big(fake_repo):
    # Forzar un archivo >MAX_READ_BYTES
    big = "x" * (MAX_READ_BYTES + 1)
    (fake_repo / "huge.txt").write_text(big, encoding="utf-8")
    r = read_file(fake_repo, "huge.txt")
    assert not r.ok
    assert "excede cap" in r.error


def test_list_dir_excludes_node_modules(fake_repo):
    out = list_dir(fake_repo, ".", max_depth=3)
    assert "node_modules" not in out
    assert "__pycache__" not in out
    assert "README.md" in out
    assert "[F] package.json" in out


def test_list_dir_respects_gitignore(fake_repo):
    out = list_dir(fake_repo, ".", max_depth=3)
    # *.log está en .gitignore
    assert "secret.log" not in out
    # /private-data/ está en .gitignore
    assert "private-data" not in out


def test_list_dir_allows_gitignore_file(fake_repo):
    out = list_dir(fake_repo, ".", max_depth=3)
    # .gitignore siempre se ve (es meta del repo)
    assert ".gitignore" in out


def test_summarize_picks_key_files(fake_repo):
    s = summarize(fake_repo, max_depth=2)
    assert isinstance(s, RepoSummary)
    assert s.name == fake_repo.name
    assert s.total_size_kb >= 0
    # README + package.json son key files default
    assert "README.md" in s.key_files
    assert "package.json" in s.key_files
    for r in s.key_files.values():
        assert r.ok, f"un key file falló: {r.error}"
    # Texto renderizado
    text = s.to_prompt_text()
    assert s.name in text
    assert "ESTRUCTURA" in text
    assert "package.json" in text


