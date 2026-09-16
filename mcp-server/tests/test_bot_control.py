"""Sonda y arranque del bot de Discord (relay.bot_control).

Lo que se cubre acá es la razón de ser del módulo: el `/health` del bot
devuelve 200 mientras el proceso viva, así que "el bot está bien" y "el
bot está conectado a Discord" son dos preguntas distintas. La sonda tiene
que distinguir los tres estados, y `start()` tiene que elegir el arreglo
que corresponde a cada uno.

Cómo correr:
    python -m pytest mcp-server/tests/test_bot_control.py -q
"""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from relay import bot_control


class _FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("body no es JSON")
        return self._payload


class _FakeClient:
    """AsyncClient de mentira. `gets` se consume en orden y se COMPARTE
    entre instancias: cada probe() abre su propio cliente, así que con una
    copia por instancia la secuencia se reiniciaría y el poll de
    _wait_gateway nunca avanzaría. `post` devuelve `post_resp`."""

    def __init__(self, gets, post_resp, calls):
        self._gets = gets
        self._post_resp = post_resp
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        self._calls.append(("GET", url))
        # El último se repite: evita quedarse sin respuestas si el
        # poll de _wait_gateway da más vueltas de las previstas.
        r = self._gets.pop(0) if len(self._gets) > 1 else self._gets[0]
        if isinstance(r, Exception):
            raise r
        return r

    async def post(self, url, **kw):
        self._calls.append(("POST", url))
        if isinstance(self._post_resp, Exception):
            raise self._post_resp
        return self._post_resp


def _fake_httpx(gets, post_resp=None):
    """(patcher, calls) — el patcher reemplaza httpx.AsyncClient."""
    calls: list[tuple[str, str]] = []
    cola = list(gets)  # una sola cola para todos los clientes del test

    def factory(*a, **kw):
        return _FakeClient(cola, post_resp, calls)

    return patch.object(bot_control.httpx, "AsyncClient", factory), calls


# ---------- probe: los tres estados ----------

async def test_probe_proceso_caido():
    """Nadie escucha en el puerto → el proceso no está."""
    p, _ = _fake_httpx([httpx.ConnectError("connection refused")])
    with p:
        st = await bot_control.probe()
    assert st["process"] == "down"
    assert st["gateway"] == "down"
    assert "no responde" in st["detail"]


async def test_probe_gateway_caido():
    """El caso que dejaba vínculos fantasma: proceso vivo, Discord no.

    Es el que `/health` del bot no distingue (devuelve 200 igual)."""
    p, _ = _fake_httpx([_FakeResp(200, {"running": False})])
    with p:
        st = await bot_control.probe()
    assert st["process"] == "up"
    assert st["gateway"] == "down"
    # El detalle tiene que ser accionable: apunta a dónde está el token.
    assert "appsettings.Development.json" in st["detail"]


async def test_probe_todo_ok():
    p, _ = _fake_httpx([_FakeResp(200, {"running": True})])
    with p:
        st = await bot_control.probe()
    assert st["process"] == "up"
    assert st["gateway"] == "up"


async def test_probe_body_no_json():
    """Un 200 que no es JSON no puede leerse como gateway arriba."""
    p, _ = _fake_httpx([_FakeResp(200, None)])
    with p:
        st = await bot_control.probe()
    assert st["process"] == "up"
    assert st["gateway"] == "down"


# ---------- start: un arreglo por estado ----------

async def test_start_no_toca_nada_si_ya_esta_conectado():
    p, calls = _fake_httpx([_FakeResp(200, {"running": True})])
    with p:
        r = await bot_control.start()
    assert r["ok"] is True
    assert r["action"] == "none"
    assert not [c for c in calls if c[0] == "POST"], "no debía pedir arranque"


async def test_start_conecta_el_gateway_sin_spawnear():
    """Proceso vivo + gateway caído → POST /api/Bot/start, sin tocar el .exe."""
    gets = [_FakeResp(200, {"running": False}),   # probe inicial
            _FakeResp(200, {"running": False}),   # primer poll
            _FakeResp(200, {"running": True})]    # ya conectó
    p, calls = _fake_httpx(gets, post_resp=_FakeResp(200, {}))
    with p, patch.object(bot_control, "_spawn") as spawn:
        r = await bot_control.start(timeout=5)
    assert r["ok"] is True
    assert r["action"] == "gateway"
    spawn.assert_not_called()
    assert ("POST", f"{bot_control.bot_base_url()}/api/Bot/start") in calls


async def test_start_sin_exe_da_error_accionable():
    """Proceso caído y sin binario: no hay nada que spawnear."""
    p, _ = _fake_httpx([httpx.ConnectError("refused")])
    with p, patch.object(bot_control, "bot_exe_path", return_value=None):
        r = await bot_control.start(timeout=1)
    assert r["ok"] is False
    assert r["action"] == "spawn"
    assert "FOURBIS_BOT_EXE" in r["detail"]


async def test_start_spawnea_con_environment_development(tmp_path):
    """El spawn tiene que ir con ASPNETCORE_ENVIRONMENT=Development: el
    token de Discord vive en appsettings.Development.json, y sin él el
    proceso levanta, sirve health 200 y nunca conecta."""
    exe = tmp_path / "RelayDemoBot.exe"
    exe.write_text("")
    gets = [httpx.ConnectError("refused"),        # probe inicial
            _FakeResp(200, {"running": True})]    # ya arrancó
    p, _ = _fake_httpx(gets)
    with p, patch.object(bot_control, "bot_exe_path", return_value=exe), \
            patch.object(bot_control.subprocess, "Popen") as popen:
        r = await bot_control.start(timeout=5)
    assert r["ok"] is True
    assert r["action"] == "spawn"
    env = popen.call_args.kwargs["env"]
    assert env["ASPNETCORE_ENVIRONMENT"] == "Development"


# ---------- resolución de config ----------

@pytest.mark.parametrize("raw,esperado", [
    ("http://127.0.0.1:8297/notify", "http://127.0.0.1:8297"),
    ("http://127.0.0.1:8297/", "http://127.0.0.1:8297"),
    ("http://bot.local:9000/notify/", "http://bot.local:9000"),
])
def test_bot_base_url_saca_el_sufijo_notify(raw, esperado, monkeypatch):
    monkeypatch.setenv("BOT_NOTIFY_URL", raw)
    assert bot_control.bot_base_url() == esperado
