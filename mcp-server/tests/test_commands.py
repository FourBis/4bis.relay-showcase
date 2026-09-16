"""Tests de comandos dinámicos (ADR-013): registry, dispatch,
validación de args, pause/resume integrado con el routing de prompts."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "mcp-server" / "src"))

from aiohttp.test_utils import TestClient, TestServer

from relay.commands import CommandContext, CommandRegistry, UnknownCommand  # noqa: E402
from relay.db import Database  # noqa: E402
from relay.server import DB_KEY, create_app  # noqa: E402
from relay.sessions import SessionRegistry  # noqa: E402

SEED_COMMANDS = [
    {"name": "sesiones", "description": "lista sesiones",
     "handler": "relay.commands.list_sessions", "args_schema": None},
    {"name": "proyectos", "description": "lista proyectos",
     "handler": "relay.commands.list_projects", "args_schema": None},
    {"name": "logs", "description": "ultimos chats de un target",
     "handler": "relay.commands.show_logs", "args_schema": None},
    {"name": "roto", "description": "handler inexistente",
     "handler": "relay.no_existe.nada", "args_schema": None},
]


@pytest.fixture
async def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    await d.init_schema()
    for c in SEED_COMMANDS:
        await d.upsert_command(c)
    await d.upsert_project(
        {"slug": "demo", "name": "Demo", "repo_path": "C:/x/Demo"})
    return d


# ---------- registry ----------


async def test_registry_carga_y_saltea_rotos(db):
    reg = CommandRegistry(db)
    n = await reg.load_from_db()
    assert n == 3                       # "roto" se salteó con warning
    assert "roto" not in reg.names()


async def test_dispatch_ok_y_log(db):
    reg = CommandRegistry(db)
    await reg.load_from_db()
    ctx = CommandContext(db=db, sessions=SessionRegistry(),
                         source="test", author="usuario-demo-a")
    text = await reg.dispatch("proyectos", {}, ctx)
    assert "demo" in text
    logs = await db.run("SELECT * FROM command_logs")
    assert len(logs) == 1 and logs[0]["status"] == "ok"


async def test_ayuda_sale_de_la_db(db):
    """El texto se arma con lo que hay en commands + mcp_servers: si un MCP
    pasa a on-demand tiene que aparecer en el `--con` sin tocar código."""
    await db.upsert_mcp_server({"name": "playwright-mcp", "capability": "browser",
                                "on_demand": 1, "enabled": 1})
    # Un always-on cualquiera. Nombre neutro a propósito: el catálogo ya
    # no siembra el wrapper y `_retire_wrapper` borra ese nombre en el
    # boot, así que usarlo acá sería un test frágil por accidente.
    await db.upsert_mcp_server({"name": "files-global", "capability": "files",
                                "on_demand": 0, "enabled": 1})
    await db.upsert_command({"name": "ayuda", "description": "ayuda",
                             "handler": "relay.commands.ayuda",
                             "args_schema": None})
    reg = CommandRegistry(db)
    await reg.load_from_db()
    ctx = CommandContext(db=db, sessions=SessionRegistry())
    text = await reg.dispatch("ayuda", {}, ctx)

    assert "!proyectos" in text                 # comandos de la DB
    assert "ayuda" not in text.split("**Flags")[0].replace("**Comandos**", "")
    assert "--con" in text and "browser" in text  # on-demand → pedible
    assert "playwright-mcp" in text
    assert "TODOS sus MCPs" not in text          # una sola por capability
    # el always-on NO se ofrece como --con, se informa como ya adjunto
    assert "Ya adjuntas siempre" in text and "files-global" in text
    assert "--solo" in text


async def test_ayuda_agrupa_capabilities_duplicadas(db):
    """`--con browser` adjunta UNO solo de los MCP con esa capability
    (declaran las mismas tools y pydantic-ai aborta el run si entran los
    dos). Si hay dos, el help tiene que avisarlo y decir cuál gana."""
    for name in ("chrome-devtools-mcp", "playwright-mcp"):
        await db.upsert_mcp_server({"name": name, "capability": "browser",
                                    "on_demand": 1, "enabled": 1})
    await db.upsert_command({"name": "ayuda", "description": "ayuda",
                             "handler": "relay.commands.ayuda",
                             "args_schema": None})
    reg = CommandRegistry(db)
    await reg.load_from_db()
    text = await reg.dispatch(
        "ayuda", {}, CommandContext(db=db, sessions=SessionRegistry()))

    assert "`browser` → chrome-devtools-mcp, playwright-mcp" in text  # una entrada, dos nombres
    assert "(default: el primero)" in text
    assert "adjunta solo uno" in text


async def test_dispatch_desconocido(db):
    reg = CommandRegistry(db)
    await reg.load_from_db()
    ctx = CommandContext(db=db, sessions=SessionRegistry())
    with pytest.raises(UnknownCommand):
        await reg.dispatch("nope", {}, ctx)


async def test_dispatch_args_invalidos(db):
    """Un handler que requiere args explícitos y se llama con {} → ValueError.
    Usamos `logs` que internamente no rompe, así que necesitamos un comando
    con args_schema que valide. Acá mockeamos: lo importante es que la
    cascada JSON Schema → ValueError se dispare."""
    reg = CommandRegistry(db)
    await reg.load_from_db()
    # forzar un comando con schema required (no hay uno en el seed hoy;
    # igual validamos que el comportamiento del dispatch es razonable).
    await db.upsert_command({
        "name": "necesita_name",
        "description": "x",
        "handler": "relay.commands.show_logs",
        "args_schema": {"type": "object", "required": ["name"],
                        "properties": {"name": {"type": "string"}}},
    })
    await reg.load_from_db()
    ctx = CommandContext(db=db, sessions=SessionRegistry())
    with pytest.raises(ValueError):
        await reg.dispatch("necesita_name", {}, ctx)  # falta "name"


# ---------- HTTP + pause/resume integrado ----------


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmp:
        env_vars = {
            "STATE_DIR": str(Path(tmp) / "state"),
            "FOURBIS_DB_PATH": str(Path(tmp) / "relay.db"),
            "FOURBIS_CHATS_DIR": str(Path(tmp) / "chats"),
            "FOURBIS_JSONL_DIR": str(Path(tmp) / "jsonl"),
            "LOG_LEVEL": "WARNING",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            db = Database(path=Path(tmp) / "relay.db")
            await db.init_schema()
            for c in SEED_COMMANDS[:4]:
                await db.upsert_command(c)
            app = create_app()
            cli = TestClient(TestServer(app))
            await cli.start_server()
            try:
                yield cli
            finally:
                await cli.close()


SID = "c" * 64
WS = [{"path": "C:/Users/demo/INVENTORYDEMO", "name": "INVENTORYDEMO"}]


async def _handshake(cli):
    return await cli.post("/agents/handshake", json={
        "name": "ventana", "sessionId": SID, "machineId": "m",
        "workspaceFolders": WS, "ts": "2026-07-07T00:00:00.000Z",
    })


async def test_commands_run_http(env):
    cli = env
    r = await cli.post("/commands/sesiones/run", json={"args": {}})
    assert r.status == 200
    assert "Sin sesiones" in (await r.json())["text"]


async def test_bang_en_el_chat_corre_el_comando(env):
    """`!comando` escrito en el chat NO puede irse al LLM como prompt.
    Caso real (website-demo, 2026-08-01): dos "!ayuda" = dos runs de experto
    quemados, porque el `!` solo lo entendía el bot de Discord."""
    cli = env
    db = cli.server.app[DB_KEY]
    await db.upsert_project(
        {"slug": "demo", "name": "Demo", "repo_path": "C:/x/Demo"})
    r = await cli.post("/experts/run", json={
        "target": "demo", "user": "!proyectos", "source": "ui"})
    assert r.status == 200                       # 200 con texto, no 202 + run
    body = await r.json()
    assert body["command"] == "proyectos"
    assert "demo" in body["text"]
    assert "id" not in body                      # no se creó chat


async def test_bang_desconocido_sigue_al_experto(env):
    """El guard es aditivo: un `!` que no es comando registrado tiene que
    caer al experto como siempre (un "!ojo con esto" no se secuestra)."""
    cli = env
    db = cli.server.app[DB_KEY]
    await db.upsert_project(
        {"slug": "demo", "name": "Demo", "repo_path": "C:/x/Demo",
         "defaults_json": {"model": "test"}})
    r = await cli.post("/experts/run", json={
        "target": "demo", "user": "!ojo con esto", "source": "ui"})
    assert r.status == 202                       # run normal
    assert "command" not in (await r.json())


async def test_commands_run_desconocido(env):
    cli = env
    r = await cli.post("/commands/zzz/run", json={"args": {}})
    assert r.status == 404
    assert "sesiones" in (await r.json())["available_commands"]


async def test_commands_run_args_invalidos(env):
    cli = env
    r = await cli.post("/commands/zzz_no_existe/run", json={"args": {}})
    assert r.status == 404
