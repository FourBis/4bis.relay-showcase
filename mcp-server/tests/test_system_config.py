"""Config de red en SQLite + guard localhost + papercuts.

Cubre:
- GET/PUT /admin/api/config (whitelist, validación, restart_required)
- modelo por rol del runner (FOURBIS_*_MODEL): solo specs del catálogo
  y prendidos; vacío = cascada
- localhost_guard: 403 desde la LAN, independiente del bind
- default_bot_url sin host.docker.internal
- versión unificada desde relay.__version__
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote
from unittest import mock
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "mcp-server" / "src"))

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from relay import __version__ as RELAY_VERSION  # noqa: E402
from relay.server import (  # noqa: E402
    BIND_HOST_KEY,
    DB_KEY,
    create_app,
    localhost_guard,
)


@pytest.fixture
async def cli():
    """App real con state temporal (la DB ya está aislada por conftest)."""
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp) / "state"
        (state_dir / "prompts").mkdir(parents=True)
        env_vars = {"STATE_DIR": str(state_dir), "LOG_LEVEL": "WARNING"}
        with patch.dict("os.environ", env_vars, clear=False):
            app = create_app()
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                yield client
            finally:
                await client.close()


# ---------- GET/PUT /admin/api/config ----------


async def test_config_get_defaults(cli):
    r = await cli.get("/admin/api/config")
    assert r.status == 200
    body = await r.json()
    assert body["config"]["RELAY_HOST"] in ("127.0.0.1", "0.0.0.0")
    assert body["effective"]["bind_host"] == "127.0.0.1"  # default create_app
    assert body["effective"]["localhost_guard_active"] is True
    assert body["effective"]["version"] == RELAY_VERSION
    assert "RELAY_HOST" in body["editable_keys"]


async def test_config_put_relay_host_roundtrip(cli):
    # 0.0.0.0 se guarda y pide restart (el bind actual es 127.0.0.1).
    r = await cli.put("/admin/api/config", json={"RELAY_HOST": "0.0.0.0"})
    assert r.status == 200
    body = await r.json()
    assert body["saved"] == {"RELAY_HOST": "0.0.0.0"}
    assert body["restart_required"] is True

    r = await cli.get("/admin/api/config")
    assert (await r.json())["config"]["RELAY_HOST"] == "0.0.0.0"

    # Volver a loopback: coincide con el bind actual → sin restart.
    r = await cli.put("/admin/api/config", json={"RELAY_HOST": "127.0.0.1"})
    body = await r.json()
    assert body["restart_required"] is False


async def test_config_put_validation(cli):
    # host fuera de la whitelist
    r = await cli.put("/admin/api/config", json={"RELAY_HOST": "192.168.1.10"})
    assert r.status == 400
    # clave desconocida
    r = await cli.put("/admin/api/config", json={"HACKME": "1"})
    assert r.status == 400
    assert "editable_keys" in (await r.json())
    # repos root inexistente
    r = await cli.put("/admin/api/config",
                      json={"FOURBIS_REPOS_ROOT": "C:/no/existe/nunca"})
    assert r.status == 400
    # body vacío
    r = await cli.put("/admin/api/config", json={})
    assert r.status == 400


async def test_config_roles_expone_el_efectivo(cli):
    """El GET dice con qué modelo corre CADA rol, no solo lo guardado.

    Un rol vacío en el form no informa nada por sí solo: hay que saberse
    la cascada (system_config → .env → compactador → ejecutor) para
    saber con qué va a correr.
    """
    r = await cli.get("/admin/api/config")
    body = await r.json()
    roles = body["model_roles"]
    assert set(roles["effective"]) == {
        "executor", "planner", "verifier", "documenter", "compactor"}
    assert all(v for v in roles["effective"].values())
    # Las claves que el form manda de vuelta tienen que ser editables.
    assert set(roles["keys"].values()) <= set(body["editable_keys"])


async def test_config_put_rol_solo_acepta_modelos_del_catalogo(cli):
    """Un spec inexistente o apagado dejaría al rol tirando
    ModelUnavailable en cada run, y el síntoma aparecería lejos de acá."""
    r = await cli.put("/admin/api/config",
                      json={"FOURBIS_PLANNER_MODEL": "nvidia:no-existe"})
    assert r.status == 400
    assert "catálogo" in (await r.json())["error"]

    db = cli.app[DB_KEY]
    await db.upsert_model(spec="fake:apagado", label="apagado",
                          provider="test", enabled=False)
    r = await cli.put("/admin/api/config",
                      json={"FOURBIS_PLANNER_MODEL": "fake:apagado"})
    assert r.status == 400
    assert "apagado" in (await r.json())["error"]

    await db.upsert_model(spec="fake:prendido", label="prendido",
                          provider="test", enabled=True)
    r = await cli.put("/admin/api/config",
                      json={"FOURBIS_PLANNER_MODEL": "fake:prendido"})
    assert r.status == 200
    # Vacío es válido: vuelve a la cascada.
    r = await cli.put("/admin/api/config",
                      json={"FOURBIS_PLANNER_MODEL": ""})
    assert r.status == 200


async def test_config_put_ejecutor_aplica_al_proximo_run(cli):
    """El valor guardado debe ser el que resuelve el ejecutor en vivo."""
    from relay import config, experts

    db = cli.app[DB_KEY]
    await db.upsert_model(spec="fake:executor", label="executor",
                          provider="test", enabled=True)
    r = await cli.put("/admin/api/config",
                      json={"FOURBIS_MODEL": "fake:executor"})
    assert r.status == 200
    assert config.model_spec() == "fake:executor"
    assert experts.resolve_model_spec("", {"defaults_json": {}}) == \
        "fake:executor"
    r = await cli.put("/admin/api/config", json={"FOURBIS_MODEL": ""})
    assert r.status == 200


@pytest.mark.parametrize("stage_models", [
    [],
    {"desconocido": "test"},
    {"planner": 123},
])
async def test_experts_run_rechaza_stage_models_malformado(cli, stage_models):
    r = await cli.post("/experts/run", json={
        "target": "demo", "user": "hola", "stage_models": stage_models})
    assert r.status == 400
    assert "stage_models" in (await r.json())["error"]


async def test_modelo_referenciado_por_proyecto_no_se_apaga_ni_borra(
        cli, tmp_path):
    db = cli.app[DB_KEY]
    usado = "fake:usado-por-proyecto"
    libre = "fake:sin-referencias"
    for spec in (usado, libre):
        await db.upsert_model(spec=spec, label=spec, provider="test", enabled=True)
    await db.upsert_project({
        "slug": "modelo-referenciado",
        "name": "Modelo referenciado",
        "repo_path": str(tmp_path),
        "enabled": False,
        "defaults_json": {"graph_planner_model": usado},
    })

    path_usado = "/admin/api/models/" + quote(usado, safe="")
    r = await cli.put(path_usado, json={"enabled": False})
    assert r.status == 409
    assert "project:modelo-referenciado:graph_planner_model" in \
        (await r.json())["roles"]
    r = await cli.delete(path_usado)
    assert r.status == 409

    r = await cli.delete("/admin/api/models/" + quote(libre, safe=""))
    assert r.status == 200
    assert await db.get_model(libre) is None


async def test_config_put_repos_root_applies_live(cli, tmp_path):
    r = await cli.put("/admin/api/config",
                      json={"FOURBIS_REPOS_ROOT": str(tmp_path)})
    assert r.status == 200
    r = await cli.get("/admin/api/config")
    body = await r.json()
    assert body["config"]["FOURBIS_REPOS_ROOT"] == str(tmp_path)
    # Aplica al instante (snapshot runtime), sin restart.
    assert body["effective"]["repos_root"] == str(tmp_path)
    # limpiar para no ensuciar otros tests de la sesión
    await cli.put("/admin/api/config", json={"FOURBIS_REPOS_ROOT": ""})


# ---------- localhost_guard ----------


def _mocked_request(path: str, peer: Any, app: web.Application):
    transport = mock.Mock()
    transport.get_extra_info = lambda key, default=None: (
        (peer, 12345) if key == "peername" else default)
    return make_mocked_request("GET", path, app=app, transport=transport)


async def _ok_handler(request):
    return web.json_response({"ok": True})


async def test_guard_403_from_lan_when_exposed():
    app = web.Application()
    app[BIND_HOST_KEY] = "0.0.0.0"
    req = _mocked_request("/admin/api/health", "192.168.1.50", app)
    resp = await localhost_guard(req, _ok_handler)
    assert resp.status == 403


async def test_guard_allows_localhost_when_exposed():
    app = web.Application()
    app[BIND_HOST_KEY] = "0.0.0.0"
    for peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        req = _mocked_request("/admin/api/health", peer, app)
        resp = await localhost_guard(req, _ok_handler)
        assert resp.status == 200


async def test_guard_only_covers_admin_and_api():
    """Con bind 0.0.0.0, /health desde la LAN pasa (solo /admin y /api
    están guardadas — exponer el resto es la decisión explícita del
    operador al setear 0.0.0.0)."""
    app = web.Application()
    app[BIND_HOST_KEY] = "0.0.0.0"
    req = _mocked_request("/health", "192.168.1.50", app)
    resp = await localhost_guard(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.parametrize("bind", ["127.0.0.1", "::", "192.168.1.20"])
async def test_guard_rejects_remote_peer_for_every_bind(bind):
    """El bind no sustituye la comprobación del peer en el trust boundary."""
    app = web.Application()
    app[BIND_HOST_KEY] = bind
    req = _mocked_request("/admin/api/health", "10.0.0.99", app)
    resp = await localhost_guard(req, _ok_handler)
    assert resp.status == 403


async def test_guard_wired_in_create_app(cli):
    """Sanity full-stack: TestClient pega desde loopback → 200 con guard."""
    r = await cli.get("/admin/api/config")
    assert r.status == 200


# ---------- papercuts ----------


def test_default_bot_url_sin_docker(monkeypatch):
    from relay.notify import default_bot_url
    monkeypatch.delenv("BOT_NOTIFY_URL", raising=False)
    assert default_bot_url() == "http://127.0.0.1:8297/notify"
    monkeypatch.setenv("BOT_NOTIFY_URL", "http://otra:9/notify")
    assert default_bot_url() == "http://otra:9/notify"


async def test_health_version_unificada(cli):
    r = await cli.get("/health")
    assert (await r.json())["version"] == RELAY_VERSION


# ---------- MODEL_PRICES (fase 2 de docs/METRICAS_PLAN.md) ----------
#
# El PUT es trust boundary: lo que se guarde acá alimenta el cálculo de
# costo del dashboard. Un JSON con forma inválida guardado sin validar
# haría que las métricas dejen de calcular en silencio.


@pytest.mark.parametrize("bad,motivo", [
    ("{no json", "JSON roto"),
    ('["a"]', "array en vez de objeto"),
    ('{"m": 3}', "el valor no es un objeto"),
    ('{"m": {"in": 1}}', "falta 'out'"),
    ('{"m": {"in": 1, "out": 1, "raro": 2}}', "clave desconocida"),
    ('{"m": {"in": "gratis", "out": 1}}', "precio no numérico"),
    ('{"m": {"in": -1, "out": 1}}', "precio negativo"),
    ('{"m": {"in": true, "out": 1}}', "bool no es precio"),
    ('{"": {"in": 1, "out": 1}}', "modelo vacío"),
])
async def test_model_prices_rejects_bad_input(cli, bad, motivo):
    r = await cli.put("/admin/api/config", json={"MODEL_PRICES": bad})
    assert r.status == 400, f"deberia rechazar ({motivo}): {bad}"
    assert "MODEL_PRICES" in (await r.json())["error"]


async def test_model_prices_accepts_valid_and_roundtrips(cli):
    good = ('{"minimax:MiniMax-M3": {"in": 0.3, "out": 1.2}, '
            '"nvidia:*": {"in": 0, "out": 0, "ref_in": 0.6, "ref_out": 2.4}}')
    r = await cli.put("/admin/api/config", json={"MODEL_PRICES": good})
    assert r.status == 200, await r.text()

    r = await cli.get("/admin/api/config")
    saved = json.loads((await r.json())["config"]["MODEL_PRICES"])
    assert saved["minimax:MiniMax-M3"]["in"] == 0.3
    assert saved["nvidia:*"]["ref_out"] == 2.4


async def test_model_prices_empty_clears(cli):
    """Vaciar la tarifa es válido: vuelve a 'sin precios'."""
    r = await cli.put("/admin/api/config", json={"MODEL_PRICES": ""})
    assert r.status == 200
    r = await cli.get("/admin/api/config")
    assert (await r.json())["config"]["MODEL_PRICES"] == ""
