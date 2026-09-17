"""Trazas OTel (2026-08-26): el switch y la regla de contenido.

Ponytail: no testeamos que OTel exporte —eso es la librería—. Lo no
trivial acá son dos cosas, y son las dos que duelen si se rompen:

1. El switch está APAGADO por default. Si un día arranca prendido, cada
   run empieza a emitir spans sin que nadie lo pida.
2. La regla de contenido. Los spans llevan el prompt y la respuesta
   completos = el código del repo del cliente. La regla es "a un tercero
   no, salvo pedido explícito". Si esto se invierte en silencio, se
   filtra código y nadie se entera hasta que es tarde.

El check de humo (`test_setup_emite_spans`) ejerce la cadena real
—configure → instrument_all → span— porque sin eso los dos de arriba
estarían testeando una función que podría no hacer nada.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from relay import config as relay_config  # noqa: E402
from relay import tracing  # noqa: E402

_VARS = ("FOURBIS_TRACING", "FOURBIS_TRACING_CONTENT", "LOGFIRE_TOKEN",
         "OTEL_EXPORTER_OTLP_ENDPOINT", "FOURBIS_ENV")


@pytest.fixture(autouse=True)
def _entorno_limpio(monkeypatch):
    """Config neutra y switch global restaurado.

    Las tres hacen falta:
    - Sin limpiar el entorno, un LOGFIRE_TOKEN real en la máquina de dev
      haría que los tests midan otra cosa (y, peor, exporten al cloud).
    - `Agent.instrument_all` es un switch de PROCESO, no de instancia:
      `test_setup_emite_spans` lo prende de verdad y, sin restaurarlo,
      queda instrumentando cada agent del resto de la suite. Nada falla
      de entrada, que es justo lo que lo hace peligroso.
    """
    from pydantic_ai import Agent
    previo = Agent._instrument_default
    for var in _VARS:
        monkeypatch.delenv(var, raising=False)
    relay_config.set_runtime_config({})
    yield
    relay_config.set_runtime_config({})
    Agent.instrument_all(previo)


# --- 1. el switch -----------------------------------------------------

def test_apagado_por_default():
    """Sin FOURBIS_TRACING no se instrumenta nada."""
    assert tracing.enabled() is False
    assert tracing.setup_tracing() is False


def test_prendido_con_el_flag(monkeypatch):
    monkeypatch.setenv("FOURBIS_TRACING", "1")
    relay_config.set_runtime_config({"FOURBIS_TRACING": "1"})
    try:
        assert tracing.enabled() is True
    finally:
        relay_config.set_runtime_config({})


def test_flag_lee_truthy_y_respeta_el_default(monkeypatch):
    monkeypatch.setenv("FOURBIS_TRACING", "on")
    relay_config.set_runtime_config({"FOURBIS_TRACING": "on"})
    assert tracing._flag("FOURBIS_TRACING", False) is True
    monkeypatch.setenv("FOURBIS_TRACING", "0")
    relay_config.set_runtime_config({"FOURBIS_TRACING": "0"})
    assert tracing._flag("FOURBIS_TRACING", True) is False
    # Ausente o vacío → gana el default, en los dos sentidos.
    monkeypatch.setenv("FOURBIS_TRACING", "   ")
    relay_config.set_runtime_config({"FOURBIS_TRACING": "   "})
    assert tracing._flag("FOURBIS_TRACING", True) is True
    monkeypatch.delenv("FOURBIS_TRACING")
    relay_config.set_runtime_config({})
    assert tracing._flag("FOURBIS_TRACING", False) is False


# --- 2. la regla de contenido ----------------------------------------

def _contenido_con(monkeypatch, _capturado=None, _runtime=None, **env) -> bool:
    """Corre `setup_tracing` con el entorno dado y devuelve el
    `include_content` con el que armó InstrumentationSettings."""
    monkeypatch.setenv("FOURBIS_TRACING", "1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    runtime = {"FOURBIS_TRACING": "1", **env}
    if _runtime is not None:
        runtime = {"FOURBIS_TRACING": "1", **_runtime}
    relay_config.set_runtime_config(runtime)
    capturado = {}

    class _FakeSettings:
        def __init__(self, include_content=True, **_kw):
            capturado["content"] = include_content

    from pydantic_ai import Agent
    fake_logfire = types.ModuleType("logfire")
    fake_logfire.configure = lambda **_kw: (
        _capturado.update(_kw) if _capturado is not None else None)
    monkeypatch.setitem(sys.modules, "logfire", fake_logfire)
    monkeypatch.setattr("pydantic_ai.agent.InstrumentationSettings",
                        _FakeSettings)
    monkeypatch.setattr(Agent, "instrument_all",
                        staticmethod(lambda *_a, **_k: None))

    assert tracing.setup_tracing() is True
    return capturado["content"]


def test_contenido_va_cuando_el_destino_es_local(monkeypatch):
    """Sin token no sale nada de la máquina: el contenido es gratis."""
    assert _contenido_con(monkeypatch) is True


def test_contenido_va_con_collector_propio(monkeypatch):
    """Con OTLP el destino lo elegiste vos, aunque haya token."""
    assert _contenido_con(
        monkeypatch,
        LOGFIRE_TOKEN="pylf_v1_xx",
        OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:4318") is True


def test_contenido_NO_va_al_cloud_por_default(monkeypatch):
    """El caso que importa: token solo = tercero = sin código del cliente."""
    assert _contenido_con(monkeypatch, LOGFIRE_TOKEN="pylf_v1_xx") is False


def test_contenido_al_cloud_solo_con_opt_in_explicito(monkeypatch):
    assert _contenido_con(monkeypatch,
                          LOGFIRE_TOKEN="pylf_v1_xx",
                          FOURBIS_TRACING_CONTENT="1") is True


def test_token_ambiental_no_activa_exportacion(monkeypatch):
    """Las variables operativas solo cuentan si están en runtime/SQLite."""
    capturado = {}
    monkeypatch.setenv("LOGFIRE_TOKEN", "synthetic-ambient-token")
    assert _contenido_con(monkeypatch, _capturado=capturado, _runtime={}) is True
    assert capturado["send_to_logfire"] is False
    assert capturado["token"] is None


def test_token_explicito_configura_logfire_cloud(monkeypatch):
    capturado = {}
    assert _contenido_con(
        monkeypatch,
        _capturado=capturado,
        LOGFIRE_TOKEN="synthetic-explicit-token") is False
    assert capturado["send_to_logfire"] is True
    assert capturado["token"] == "synthetic-explicit-token"


def test_token_con_otlp_no_activa_logfire_cloud(monkeypatch):
    capturado = {}
    assert _contenido_con(
        monkeypatch,
        _capturado=capturado,
        LOGFIRE_TOKEN="synthetic-explicit-token",
        OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:4318") is True
    assert capturado["send_to_logfire"] is False
    assert capturado["token"] == "synthetic-explicit-token"


# --- 3. humo: la cadena real escribe un span -------------------------

def test_setup_emite_spans(monkeypatch, capsys):
    """Sin destino configurado, logfire imprime los spans por consola.

    Único punto donde se ejercen `logfire.configure` y
    `Agent.instrument_all` de verdad: si alguna firma cambia entre
    versiones, falla acá y no en producción.
    """
    logfire = pytest.importorskip("logfire")
    monkeypatch.setenv("FOURBIS_TRACING", "1")
    relay_config.set_runtime_config({"FOURBIS_TRACING": "1"})
    assert tracing.setup_tracing() is True
    with logfire.span("check-de-humo"):
        pass
    salida = capsys.readouterr()
    assert "check-de-humo" in (salida.out + salida.err)


def test_sin_logfire_no_rompe(monkeypatch):
    """El relay tiene que arrancar igual con el extra sin instalar."""
    monkeypatch.setenv("FOURBIS_TRACING", "1")
    relay_config.set_runtime_config({"FOURBIS_TRACING": "1"})
    assert tracing.enabled() is True
    monkeypatch.setitem(sys.modules, "logfire", None)  # import → ImportError
    assert tracing.setup_tracing() is False
