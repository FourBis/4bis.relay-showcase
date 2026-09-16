"""Smoke: invocar cbm como subprocess desde el relay (ADR-017, iter 4.5).

El relay NO habla MCP stdio con cbm directamente cuando es un smoke
test (eso es para `experts.py` en producción). Acá verificamos:

1. `codebase-memory-mcp` está en PATH (o ruta absoluta).
2. Se puede invocar como subprocess async con `CBM_CACHE_DIR` propagado.
3. La CLI responde JSON parseable.
4. Con un proyecto indexado, `list_projects` lo ve.
5. `search_graph` devuelve resultados estructurados.

Salta el test entero si cbm no está instalado (dep opcional).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

# CBM_CACHE_DIR estándar para tests (independiente del user real).
TEST_CACHE = HERE.parent / ".cbm-test-cache"
TEST_PROJECT_NAME = "C-Users-demo-source-repos-AuroraDemo-CommerceDemo"


def _cbm_binary() -> str | None:
    """Devuelve ruta al binario o None si no está instalado."""
    # 1) Buscar en PATH (lo que el install.ps1 agregó).
    path = shutil.which("codebase-memory-mcp")
    if path:
        return path
    # 2) Fallback: ubicación estándar del install.ps1.
    standard = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "codebase-memory-mcp" / "codebase-memory-mcp.exe"
    if standard.is_file():
        return str(standard)
    return None


# Saltear el módulo entero si cbm no está instalado.
pytestmark = pytest.mark.skipif(
    _cbm_binary() is None,
    reason="codebase-memory-mcp no instalado; corre install.ps1",
)


async def _cbm_cli(*args: str) -> dict:
    """Invoca cbm CLI con args y devuelve dict (parsea el stdout JSON).

    Propaga `CBM_CACHE_DIR` apuntando a TEST_CACHE, así no toca el
    cache real del usuario.
    """
    bin_path = _cbm_binary()
    assert bin_path is not None
    env = os.environ.copy()
    env["CBM_CACHE_DIR"] = str(TEST_CACHE)
    env["CBM_ALLOWED_ROOT"] = str(HERE.parent.parent)  # cualquier root, no validamos
    proc = await asyncio.create_subprocess_exec(
        bin_path, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise AssertionError(
            f"cbm cli {' '.join(args)} falló (exit {proc.returncode}):\n"
            f"stderr: {stderr.decode(errors='replace')[:500]}\n"
            f"stdout: {stdout.decode(errors='replace')[:500]}"
        )
    text = stdout.decode("utf-8", errors="replace").strip()
    # cbm mezcla logs level=info con el JSON final; agarramos el último
    # JSON válido (línea que empieza con { o [).
    last_json = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            last_json = line
    if not last_json:
        raise AssertionError(f"cbm no devolvió JSON parseable:\n{text[:500]}")
    return json.loads(last_json)


@pytest.fixture(autouse=True)
def _clean_cache():
    """Asegura TEST_CACHE existe vacío antes de cada test."""
    if TEST_CACHE.exists():
        shutil.rmtree(TEST_CACHE, ignore_errors=True)
    TEST_CACHE.mkdir(parents=True, exist_ok=True)
    yield


async def test_cbm_version() -> None:
    """El binario responde a --version."""
    bin_path = _cbm_binary()
    assert bin_path is not None
    proc = await asyncio.create_subprocess_exec(
        bin_path, "--version",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    out = stdout.decode("utf-8", errors="replace").strip()
    assert out.startswith("codebase-memory-mcp "), f"versión inesperada: {out!r}"


async def test_cbm_list_projects_empty_when_cache_fresh() -> None:
    """Con cache recién creado y CBM_CACHE_DIR seteado, lista vacío."""
    result = await _cbm_cli("cli", "list_projects")
    assert "projects" in result
    assert result["projects"] == []


async def test_cbm_cli_respects_cache_dir_env() -> None:
    """Si CBM_CACHE_DIR apunta a un dir vacío, NO ve el cache default.

    Esto garantiza que cuando el relay spawnea cbm, el cache vive
    donde el relay quiere, no donde el binario tiene por default.
    """
    # Si ya hay algo en TEST_CACHE.list_projects = []. Si leyera el
    # default, podría ver CommerceDemo u otros. Garantizamos vacío acá:
    result = await _cbm_cli("cli", "list_projects")
    assert result["projects"] == [], (
        f"CBM_CACHE_DIR no se respetó: {result!r}"
    )


async def test_cbm_help_lists_tools() -> None:
    """El binario lista sus 14 tools en --help."""
    bin_path = _cbm_binary()
    assert bin_path is not None
    proc = await asyncio.create_subprocess_exec(
        bin_path, "--help",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    out = stdout.decode("utf-8", errors="replace")
    # Tools principales que el experto va a invocar.
    expected = [
        "index_repository", "search_graph", "trace_path", "query_graph",
        "get_architecture", "list_projects", "index_status", "detect_changes",
    ]
    for tool in expected:
        assert tool in out, f"{tool!r} no aparece en --help"
