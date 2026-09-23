"""Botón "Probar" del catálogo de modelos (2026-08-26).

Nació de un caso real: una API key de xAI perfectamente válida contra una
cuenta sin créditos. El proveedor lo explicaba en una línea —con el link
para comprarlos— y esa línea no llegaba a ninguna pantalla: el modelo
solo se podía "probar" mandando un run, y ahí el error salía envuelto
como `provider_error`, que es el síntoma que menos dice.

Lo que se testea es lo que hace útil al botón, no la plomería HTTP:

- que el error del proveedor llegue CRUDO y con su status (si lo
  tradujéramos a "no se pudo conectar" volveríamos al problema);
- que un modelo sin key se distinga de un modelo que el proveedor
  rechaza, porque se arreglan en lugares distintos;
- que el éxito informe tokens, que es lo que confirma que hubo un turno
  real y no un 200 vacío.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))


@pytest.fixture
async def cliente(monkeypatch):
    """Cliente contra una app mínima con solo este endpoint montado.

    `TestClient(TestServer(app))` a mano porque el repo no trae la
    fixture `aiohttp_client` de pytest-aiohttp — mismo patrón que
    `test_admin_projects.py`.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from relay import admin_config_models as model_test

    async def _get_model(spec):
        return None if spec == "no:existe" else {"spec": spec}

    async def _noop(_app):
        return None

    app = web.Application()
    app[model_test.DB_KEY] = type(
        "_Db", (), {"get_model": staticmethod(_get_model)})()
    app.router.add_post("/admin/api/models/{spec}/test", model_test.api_model_test)
    monkeypatch.setattr(model_test, "_refrescar_catalogo", _noop)

    c = TestClient(TestServer(app))
    await c.start_server()
    yield c
    await c.close()


@pytest.mark.asyncio
async def test_modelo_desconocido_da_404(cliente):
    r = await cliente.post("/admin/api/models/no%3Aexiste/test")
    assert r.status == 404


@pytest.mark.asyncio
async def test_sin_key_se_distingue_del_fallo_del_proveedor(cliente, monkeypatch):
    """`sin_key` es config nuestra; un HTTP del proveedor es del otro
    lado. Se arreglan en lugares distintos, así que no pueden confundirse."""
    from relay import admin_config_models as model_test
    monkeypatch.setattr(model_test, "build_model", _que_tira(
        model_test.ModelUnavailable("grok:x no tiene API key: cargala en la fila")))
    r = await cliente.post("/admin/api/models/grok%3Ax/test")
    assert r.status == 200          # el test corrió; lo que falló es el modelo
    body = await r.json()
    assert body["ok"] is False
    assert body["kind"] == "sin_key"
    assert "no tiene API key" in body["error"]


@pytest.mark.asyncio
async def test_el_error_del_proveedor_llega_crudo_con_status(cliente, monkeypatch):
    """EL test de este archivo: el caso xAI, tal cual pasó."""
    from pydantic_ai.exceptions import ModelHTTPError
    from relay import admin_config_models as model_test

    cuerpo = ('{"code":"permission-denied","error":"Your newly created team '
              'doesn\'t have any credits or licenses yet. You can purchase '
              'those on https://console.x.ai/team/b386e98f."}')
    monkeypatch.setattr(model_test, "build_model", lambda _s: object())
    _parchar_agent(monkeypatch, ModelHTTPError(
        status_code=403, model_name="grok-4.6", body=cuerpo))

    body = await (await cliente.post("/admin/api/models/grok%3Agrok-4.6/test")).json()
    assert body["ok"] is False
    assert body["kind"] == "http"
    assert body["status"] == 403
    # Lo accionable —el porqué y el link— tiene que sobrevivir entero.
    assert "credits or licenses" in body["error"]
    assert "console.x.ai" in body["error"]


@pytest.mark.asyncio
async def test_timeout_no_se_reporta_como_error_del_proveedor(cliente, monkeypatch):
    import asyncio
    from relay import admin_config_models as model_test
    monkeypatch.setattr(model_test, "build_model", lambda _s: object())
    monkeypatch.setattr(model_test, "_TEST_TIMEOUT_S", 0.05)
    _parchar_agent(monkeypatch, asyncio.TimeoutError(), demora=1.0)
    body = await (await cliente.post("/admin/api/models/lento%3Ax/test")).json()
    assert body["ok"] is False and body["kind"] == "timeout"


@pytest.mark.asyncio
async def test_exito_informa_tokens(cliente, monkeypatch):
    """Sin tokens no hay prueba de que haya habido un turno real.

    Cubre además el bug que ya me comí una vez: `usage` es PROPIEDAD en
    pydantic-ai 2.x, y con paréntesis el éxito reportaba error.
    """
    from relay import admin_config_models as model_test
    monkeypatch.setattr(model_test, "build_model", lambda _s: object())
    _parchar_agent(monkeypatch, None)
    body = await (await cliente.post("/admin/api/models/ok%3Ax/test")).json()
    assert body["ok"] is True
    assert body["tokens_in"] == 11 and body["tokens_out"] == 2
    assert body["reply"] == "ok"
    assert isinstance(body["ms"], int)


# ---------- helpers ----------

def _que_tira(exc):
    def _f(_spec):
        raise exc
    return _f


class _Usage:
    input_tokens = 11
    output_tokens = 2


class _Resultado:
    output = "ok"
    usage = _Usage()          # PROPIEDAD, no método — igual que la real


def _parchar_agent(monkeypatch, exc, demora: float = 0.0):
    """Reemplaza `pydantic_ai.Agent` por uno que falla (o no) a pedido.

    Se parcha en `pydantic_ai` y no en `admin` porque el handler lo
    importa adentro de la función, así que el import corre en cada
    request y toma lo que haya en el módulo original.
    """
    import asyncio
    import pydantic_ai

    class _AgenteFalso:
        def __init__(self, *_a, **_k): pass

        async def run(self, *_a, **_k):
            if demora:
                await asyncio.sleep(demora)
            if exc is not None:
                raise exc
            return _Resultado()

    monkeypatch.setattr(pydantic_ai, "Agent", _AgenteFalso)
