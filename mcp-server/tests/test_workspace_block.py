"""Tests unitarios de `extract_workspace_block` (ADR-011).

El helper arma el bloque "## Workspace abierto" que se inyecta en el
system prompt de los expertos. Antes también se inyectaba en el
push (POST /prompts, ya no existe), pero el helper se sigue usando
en `experts.build_instructions` con la lista del proyecto.

Cubre:
- extract_workspace_block con lista vacía / folders sin path /
  múltiples folders.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "mcp-server" / "src"))

from relay.sessions import extract_workspace_block  # noqa: E402


def test_extract_empty_returns_empty_string() -> None:
    assert extract_workspace_block(None) == ""
    assert extract_workspace_block([]) == ""
    assert extract_workspace_block("no es lista") == ""


def test_extract_skips_folders_without_path() -> None:
    """Si todos los folders no tienen path, devolvemos ""."""
    folders = [
        {"name": "no-path-1"},
        {"name": "no-path-2"},
    ]
    assert extract_workspace_block(folders) == ""


def test_extract_skips_non_dict_entries() -> None:
    folders = [
        "string suelta",
        123,
        {"path": "C:/ok", "name": "INVENTORYDEMO"},
    ]
    block = extract_workspace_block(folders)
    assert "## Workspace abierto" in block
    assert "C:/ok" in block
    assert "[INVENTORYDEMO]" in block


def test_extract_single_folder_with_name() -> None:
    folders = [{"path": "C:/Users/demo/INVENTORYDEMO", "name": "INVENTORYDEMO"}]
    block = extract_workspace_block(folders)
    assert "## Workspace abierto" in block
    assert "`C:/Users/demo/INVENTORYDEMO`" in block
    assert "[INVENTORYDEMO]" in block
    # single folder → no hay sub-bullets
    assert "Otras carpetas" not in block


def test_extract_multi_root_lists_extras() -> None:
    folders = [
        {"path": "C:/Users/demo/INVENTORYDEMO", "name": "INVENTORYDEMO"},
        {"path": "C:/Users/demo/Tools", "name": "Tools"},
        {"path": "C:/Users/demo/SharedLib", "name": "SharedLib"},
    ]
    block = extract_workspace_block(folders)
    assert "`C:/Users/demo/INVENTORYDEMO`" in block
    assert "[INVENTORYDEMO]" in block
    assert "Otras carpetas abiertas en esta ventana:" in block
    assert "- **Tools**: `C:/Users/demo/Tools`" in block
    assert "- **SharedLib**: `C:/Users/demo/SharedLib`" in block


def test_extract_uses_path_basename_if_no_name() -> None:
    folders = [{"path": "C:/Users/demo/MyRepo", "name": ""}]
    block = extract_workspace_block(folders)
    # sin name → usa basename del path
    assert "[MyRepo]" in block


def test_extract_includes_tool_examples() -> None:
    """El bloque tiene que mencionar las tools para que el LLM entienda."""
    folders = [{"path": "D:/proj", "name": "Proj"}]
    block = extract_workspace_block(folders)
    assert "read_file" in block
    assert "list_directory" in block
    assert "run_in_terminal" in block
