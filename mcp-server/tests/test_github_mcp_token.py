"""El github-mcp tiene que autenticarse (2026-08-26).

El bug: `_auth_headers` solo miraba `GITHUB_TOKEN`, que no está seteado en
ningún lado —ni en el .env, ni en el `env` del MCP en la tabla—, así que
el subproceso corría ANÓNIMO. Con 60 req/h eso ya molesta, pero lo que
rompe de verdad es que GitHub contesta **404** (no 403) a un repo privado
que el llamador no puede ver: el síntoma es "el repo no existe".

Costo real: el experto recibió 404 de `get_repo`, `list_issues`,
`search_issues` y `list_commits` sobre `AuroraDemo/auth-demo`, concluyó que
el nombre estaba mal, y gastó un run entero preguntándole al humano cómo
se llamaba el repo. El nombre siempre estuvo bien.

Se testea la resolución del token, no la llamada HTTP: que use `gh` cuando
falta la env var, que la env var gane, que un `gh` roto degrade a anónimo
en vez de tirar, y que el resultado se cachee (cada spawn de `gh` en
Windows cuesta ~200ms cargando la imagen del exe).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_RUTA = Path(__file__).resolve().parent.parent / "mcp_servers" / "github_mcp.py"


@pytest.fixture
def gm(monkeypatch):
    """Módulo recién importado, con el caché de token limpio."""
    spec = importlib.util.spec_from_file_location("gm_bajo_test", _RUTA)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gm_bajo_test"] = mod
    spec.loader.exec_module(mod)
    mod._token_cache = None
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return mod


def _falso_gh(monkeypatch, gm, salida="gho_desde_el_cli", rc=0, excepcion=None):
    llamadas = []

    def _run(cmd, **kw):
        llamadas.append(cmd)
        if excepcion is not None:
            raise excepcion
        return subprocess.CompletedProcess(cmd, rc, stdout=salida, stderr="")

    monkeypatch.setattr(gm.subprocess, "run", _run)
    return llamadas


def test_usa_el_token_del_gh_cli_cuando_no_hay_env(gm, monkeypatch):
    """El caso real: la máquina ya está logueada, nadie seteó la env var."""
    llamadas = _falso_gh(monkeypatch, gm)
    assert gm._resolver_token() == "gho_desde_el_cli"
    assert llamadas == [["gh", "auth", "token"]]


def test_manda_el_header_authorization(gm, monkeypatch):
    """Lo que de verdad decide si ves un repo privado."""
    _falso_gh(monkeypatch, gm)
    h = gm._auth_headers()
    assert h["Authorization"] == "Bearer gho_desde_el_cli"


def test_la_env_var_gana_sobre_el_cli(gm, monkeypatch):
    """Un token explícito es una decisión; no se pisa con el del CLI."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_explicito")
    llamadas = _falso_gh(monkeypatch, gm)
    assert gm._resolver_token() == "ghp_explicito"
    assert llamadas == []          # ni siquiera spawnea `gh`


def test_gh_ausente_degrada_a_anonimo_sin_romper(gm, monkeypatch):
    """`gh` no instalado: se pierde el acceso privado, no el MCP entero."""
    _falso_gh(monkeypatch, gm, excepcion=FileNotFoundError("gh"))
    assert gm._resolver_token() == ""
    assert "Authorization" not in gm._auth_headers()


def test_gh_sin_login_degrada_a_anonimo(gm, monkeypatch):
    """`gh auth token` con exit != 0 no debe colarse como token."""
    _falso_gh(monkeypatch, gm, salida="", rc=1)
    assert gm._resolver_token() == ""


def test_gh_colgado_no_cuelga_el_mcp(gm, monkeypatch):
    _falso_gh(monkeypatch, gm, excepcion=subprocess.TimeoutExpired("gh", 15))
    assert gm._resolver_token() == ""


def test_se_spawnea_gh_una_sola_vez(gm, monkeypatch):
    """Cada spawn en Windows cuesta ~200ms; el MCP hace muchas llamadas."""
    llamadas = _falso_gh(monkeypatch, gm)
    for _ in range(5):
        gm._resolver_token()
        gm._auth_headers()
    assert len(llamadas) == 1
