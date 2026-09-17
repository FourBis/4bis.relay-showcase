"""Config global de los tests del relay.

CBM_AUTO_WATCH=0: en la máquina de dev el binario cbm SÍ está instalado,
así que sin esto cualquier test que haga create_app() levantaría
observers de watchdog sobre los repos tmp y podría disparar reindexes
reales de cbm en medio de la suite. setdefault: un test del watcher
puede prenderlo explícitamente.

RELAY_URL a puerto muerto (2026-07-20): los handlers admin expert-run
hacen POST interno a RELAY_URL (default :8413). Con el relay REAL
corriendo en la máquina de dev, ese POST llegaba al relay vivo y
lanzaba runs MiniMax de verdad desde la suite (visto: run "dummy" en
el proyecto demo, 5 min colgado y tokens quemados). Puerto 9 (discard)
cerrado → ConnectError inmediato → 502, que es lo que los tests
esperan. Un test que quiera un relay real puede pisar la var.

FOURBIS_MCP_HEALTH_PROBE=0 (2026-08-16): desde que el catálogo trae MCPs
sembrados (context7, fetch), el probe de health del boot LEVANTA esos
procesos de verdad — `npx -y @upstash/context7-mcp`, `uvx
mcp-server-fetch` — y espera su timeout. Cada create_app() de un test
pagaba eso: medido, test_conversations_ui pasó de 2.1s a 19.3s y la
suite entera dejó de terminar. Mismo criterio que las de abajo: un test
no toca procesos, red ni estado real de la máquina.

FOURBIS_LOG_DIR a temp (2026-07-25): desde que create_app() instala un
RotatingFileHandler, cada test que lo llama escribía al log REAL del
usuario (~/.4bis/logs/relay.log). Una corrida de la suite metía 1.5 MB
de ruido —asyncSetUp, GET /admin/api/projects/demo— y rotaba el log de
verdad fuera de existencia. Mismo problema que RELAY_URL: la suite
tocando estado real de la máquina.
"""
import os
import logging
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("CBM_AUTO_WATCH", "0")
os.environ.setdefault("FOURBIS_MCP_HEALTH_PROBE", "0")
os.environ.setdefault("RELAY_URL", "http://127.0.0.1:9")
os.environ.setdefault(
    "FOURBIS_LOG_DIR", os.path.join(tempfile.gettempdir(), "relay-test-logs"))


@pytest.fixture(scope="session")
def _fourbis_tmp():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            yield Path(tmp)
        finally:
            # Windows cannot remove a temporary log while a handler owns it.
            for handler in list(logging.getLogger().handlers):
                filename = getattr(handler, "baseFilename", "")
                if filename and Path(filename).is_relative_to(tmp):
                    logging.getLogger().removeHandler(handler)
                    handler.close()


@pytest.fixture(autouse=True)
def _isolated_fourbis_env(_fourbis_tmp, monkeypatch):
    """Aísla ~/.4bis (BD, chats, jsonl) de la suite.

    Sin esto, `Database()` y `create_app()` caen al default de
    config.db_path() — el relay.db REAL del usuario. Medido el
    2026-07-31: una corrida de la suite pisó system_config.RELAY_HOST y
    FOURBIS_REPOS_ROOT de la instalación real (venía de que este fixture
    vivía en `conftest_raiz.py`, nombre que pytest no carga).

    Function-scoped a propósito: varios tests hacen os.environ.pop() de
    estas vars en su tearDown, así que un fixture de sesión dejaba sin
    aislamiento a todo lo que corriera después.
    """
    os.environ["FOURBIS_DB_PATH"] = str(_fourbis_tmp / "relay.db")
    os.environ["FOURBIS_CHATS_DIR"] = str(_fourbis_tmp / "chats")
    os.environ["FOURBIS_JSONL_DIR"] = str(_fourbis_tmp / "jsonl")
    monkeypatch.setenv("FOURBIS_ATTACHMENTS_DIR", str(_fourbis_tmp / "attachments"))
    monkeypatch.setenv("STATE_DIR", str(_fourbis_tmp / "state"))
    os.environ["FOURBIS_MODEL"] = "test"  # TestModel: sin red, sin tokens
    monkeypatch.setenv("GOOGLE_REAL", "0")  # Las pruebas optan por Google real explícitamente.
    # El warmup es independiente del watcher y antes iniciaba el CBM real
    # desde cada TestServer, aun con CBM_AUTO_WATCH=0.
    from relay import config, server
    from relay import shell
    from relay.db import Database

    config.set_runtime_config({})

    # ponytail: tests use temporary schema defaults because runtime settings
    # intentionally ignore environment variables. No writes to the real home.
    # A fixture needing other paths can still set runtime configuration.
    for key, dirname in {
        "FOURBIS_LOG_DIR": "logs", "FOURBIS_CHATS_DIR": "chats",
        "FOURBIS_JSONL_DIR": "jsonl", "FOURBIS_ATTACHMENTS_DIR": "attachments",
        "FOURBIS_SKILLS_DIR": "skills", "FOURBIS_MCP_INSTALLS_DIR": "mcps",
        "STATE_DIR": "state", "VOICE_AUDIO_DIR": "audio",
        "VOICE_TRANSCRIPTS_DIR": "transcripts", "FOURBIS_REPOS_ROOT": "repos",
    }.items():
        monkeypatch.setitem(config.PANEL_SETTINGS, key, {
            **config.PANEL_SETTINGS[key], "default": str(_fourbis_tmp / dirname),
        })

    async def no_warmup(app):
        pass

    monkeypatch.setattr(server, "_warm_cbm_session", no_warmup)
    monkeypatch.setattr(shell, "BG_LOG_DIR", _fourbis_tmp / "bg-logs")
    monkeypatch.setattr(Database, "NOTES_ROOT_DEFAULT", str(_fourbis_tmp / "notes"))
    yield
    config.set_runtime_config({})
