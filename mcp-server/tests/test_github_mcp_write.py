"""`create_issue` / `comment_issue` del github-mcp (2026-08-26).

Por qué existen: el experto recibió el pedido "crea este issue y asignalo
a X" y lo único que podía hacer era reportar el bloqueo — el MCP tenía
siete tools y las siete eran de lectura. Ni el token ni el reinicio
cambiaban eso: la tool no existía.

Lo que se testea es lo que hace confiable el reporte del experto, no el
POST (eso es httpx):

- el aviso de assignees. GitHub **descarta en silencio** a quien no es
  colaborador: devuelve 201 con `assignees: []` y ningún error. Sin el
  aviso, el experto reporta "creado y asignado" sobre una lista vacía —
  exactamente el modo de falla que la bitácora existe para evitar.
- el 404 de un POST. Con token sin permiso de escritura GitHub devuelve
  404, igual que para un repo inexistente. Ya vimos al experto salir a
  probar variantes del nombre por culpa de esa ambigüedad, así que el
  mensaje tiene que nombrar la otra causa.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_RUTA = Path(__file__).resolve().parent.parent / "mcp_servers" / "github_mcp.py"


@pytest.fixture
def gm(monkeypatch):
    spec = importlib.util.spec_from_file_location("gm_write", _RUTA)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gm_write"] = mod
    spec.loader.exec_module(mod)
    mod._token_cache = "gho_falso"
    return mod


def _fake_post(gm, monkeypatch, status=201, body=None):
    visto = {}

    async def _post(path, payload):
        visto["path"], visto["payload"] = path, payload
        return status, (body if body is not None else {
            "number": 42, "html_url": "https://github.com/o/r/issues/42",
            "title": payload.get("title"), "assignees": [], "labels": []})

    monkeypatch.setattr(gm, "_post", _post)
    return visto


def _correr(coro):
    return asyncio.run(coro)


# --- construcción del payload ---

def test_title_vacio_se_rechaza_sin_llamar_a_github(gm, monkeypatch):
    visto = _fake_post(gm, monkeypatch)
    out = _correr(gm.create_issue("o", "r", "   "))
    assert "ERROR" in out and "title" in out
    assert visto == {}                      # ni siquiera salió a la red


def test_listas_se_parten_por_coma(gm, monkeypatch):
    visto = _fake_post(gm, monkeypatch)
    _correr(gm.create_issue("o", "r", "t", "cuerpo",
                            assignees="uno, dos", labels="bug , urgente"))
    assert visto["payload"]["assignees"] == ["uno", "dos"]
    assert visto["payload"]["labels"] == ["bug", "urgente"]
    assert visto["path"] == "/repos/o/r/issues"


def test_campos_vacios_no_viajan(gm, monkeypatch):
    """Mandar `assignees: []` no es lo mismo que no mandarlo."""
    visto = _fake_post(gm, monkeypatch)
    _correr(gm.create_issue("o", "r", "t"))
    assert set(visto["payload"]) == {"title"}


# --- el aviso que evita el reporte falso ---

def test_avisa_cuando_github_descarta_al_assignee(gm, monkeypatch):
    """El caso real: un MAIL no es un login de GitHub."""
    _fake_post(gm, monkeypatch)             # devuelve assignees: []
    out = _correr(gm.create_issue("o", "r", "t",
                                  assignees="assignee@example.test"))
    assert "OK creado #42" in out
    assert "AVISO" in out
    assert "assignee@example.test" in out
    assert "(ninguno)" in out


def test_sin_aviso_cuando_la_asignacion_funciono(gm, monkeypatch):
    _fake_post(gm, monkeypatch, body={
        "number": 7, "html_url": "u", "title": "t",
        "assignees": [{"login": "usuario-demo-a"}], "labels": []})
    out = _correr(gm.create_issue("o", "r", "t", assignees="usuario-demo-a"))
    assert "AVISO" not in out
    assert "usuario-demo-a" in out


# --- errores legibles ---

def test_404_nombra_la_causa_probable(gm, monkeypatch):
    """Sin esto el experto sale a adivinar el nombre del repo."""
    async def _cliente_404(path, payload):
        return 404, ("HTTP 404: el repo no existe, o el token no tiene "
                     "permiso de escritura sobre el.")
    monkeypatch.setattr(gm, "_post", _cliente_404)
    out = _correr(gm.create_issue("o", "r", "t"))
    assert "ERROR (404)" in out
    assert "permiso de escritura" in out


def test_comment_issue_exige_body(gm, monkeypatch):
    visto = _fake_post(gm, monkeypatch)
    assert "ERROR" in _correr(gm.comment_issue("o", "r", 1, "  "))
    assert visto == {}


def test_comment_issue_pega_en_la_ruta_correcta(gm, monkeypatch):
    visto = _fake_post(gm, monkeypatch, body={"html_url": "u"})
    out = _correr(gm.comment_issue("o", "r", 99, "hola"))
    assert visto["path"] == "/repos/o/r/issues/99/comments"
    assert "OK comentado en #99" in out
