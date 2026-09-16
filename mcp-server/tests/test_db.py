"""Tests de relay/db.py — schema, repos, índice de chats, auditoría."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "mcp-server" / "src"))

from relay.db import Database  # noqa: E402


@pytest.fixture
async def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    await d.init_schema()
    return d


async def test_init_schema_idempotente(db):
    await db.init_schema()  # segunda vez no explota
    assert (await db.list_projects()) == []


async def test_init_schema_no_asigna_owner_por_defecto(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_OWNER_EMAIL", raising=False)
    d = Database(path=tmp_path / "sin-owner.db")
    await d.init_schema()
    assert await d.list_users() == []


async def test_project_upsert_y_parse(db):
    p = await db.upsert_project({
        "slug": "inventorydemo", "name": "INVENTORYDEMO", "repo_path": "C:/x/INVENTORYDEMO",
        "system_prompt": "eres experto",
        "mcp_servers": [{"name": "w", "transport": "stdio"}],
        "defaults_json": {"model": "test"},
    })
    assert p["id"] is not None
    assert p["mcp_servers"][0]["name"] == "w"       # JSON parseado
    assert p["defaults_json"]["model"] == "test"
    assert p["enabled"] is True

    # update por slug no duplica
    p2 = await db.upsert_project({"slug": "inventorydemo", "name": "INVENTORYDEMO v2"})
    assert p2["id"] == p["id"]
    assert p2["name"] == "INVENTORYDEMO v2"
    assert p2["repo_path"] == "C:/x/INVENTORYDEMO"            # no se pisó
    assert len(await db.list_projects()) == 1


async def test_project_soft_delete(db):
    await db.upsert_project({"slug": "x", "name": "X", "repo_path": "C:/x"})
    assert await db.disable_project("x") is True
    assert await db.list_projects(enabled_only=True) == []
    assert len(await db.list_projects(enabled_only=False)) == 1
    assert await db.disable_project("nope") is False


async def test_command_upsert(db):
    c = await db.upsert_command({
        "name": "sesiones", "description": "lista",
        "handler": "relay.commands.list_sessions",
        "args_schema": {"type": "object"},
    })
    assert c["args_schema"] == {"type": "object"}
    assert (await db.get_command("sesiones"))["handler"].endswith("list_sessions")
    assert await db.disable_command("sesiones") is True
    assert await db.list_commands() == []


async def test_chats_index(db):
    cid = await db.create_chat(
        project_slug="inventorydemo", source="discord", author="usuario-demo-a", target="inventorydemo")
    chat = await db.get_chat(cid)
    assert chat["status"] == "running"

    await db.finish_chat(cid, status="ok", md_path="C:/tmp/x.md",
                         tokens_in=10, tokens_out=20, tool_calls=2)
    chat = await db.get_chat(cid)
    assert chat["status"] == "ok"
    assert chat["md_path"] == "C:/tmp/x.md"
    assert chat["tokens_out"] == 20

    assert len(await db.list_chats(project_slug="inventorydemo")) == 1
    assert await db.list_chats(project_slug="otro") == []


async def test_command_logs_y_stats(db):
    await db.log_command(name="sesiones", source="discord", author="j",
                         args={}, response="ok", duration_ms=5, status="ok")
    stats = await db.stats()
    assert stats["commands_today"] == 1
    assert stats["chats_today"] == 0


async def test_system_config_get_set(db):
    # tabla vacía → default
    assert await db.get_config("RELAY_HOST") is None
    assert await db.get_config("RELAY_HOST", "127.0.0.1") == "127.0.0.1"

    await db.set_config("RELAY_HOST", "0.0.0.0")
    assert await db.get_config("RELAY_HOST") == "0.0.0.0"

    # upsert: segunda escritura pisa la primera, no duplica
    await db.set_config("RELAY_HOST", "127.0.0.1")
    assert await db.get_config("RELAY_HOST") == "127.0.0.1"

    await db.set_config("FOURBIS_REPOS_ROOT", "C:/repos")
    cfg = await db.all_config()
    # init_schema() inyecta flags de migración one-shot (mcp_blob_migrated
    # de F0, obscura_retired y mcps_seeded_v1 de 2026-08-16) y los seeds
    # CONTEXT_* del Sprint 1. Filtramos las keys internas para no acoplar
    # el test a efectos colaterales legítimos del boot.
    cfg_public = {k: v for k, v in cfg.items()
                  if k not in ("mcp_blob_migrated", "obscura_retired",
                               "wrapper_retired", "mcps_seeded_v1",
                               "CONTEXT_LIMIT_TOKENS", "CONTEXT_TRIM_STRATEGY",
                               "CONTEXT_WARN_AT")}
    assert cfg_public == {"RELAY_HOST": "127.0.0.1", "FOURBIS_REPOS_ROOT": "C:/repos"}


async def test_system_config_sync_reader(db, monkeypatch):
    """read_system_config_sync lee la misma DB sin el loop (bind en main)."""
    from relay.db import read_system_config_sync

    await db.set_config("RELAY_HOST", "0.0.0.0")
    monkeypatch.setenv("FOURBIS_DB_PATH", str(db.path))
    assert read_system_config_sync("RELAY_HOST", "127.0.0.1") == "0.0.0.0"
    assert read_system_config_sync("NO_EXISTE", "def") == "def"

    # DB inexistente → default, sin explotar
    monkeypatch.setenv("FOURBIS_DB_PATH", str(db.path.parent / "nope" / "x.db"))
    assert read_system_config_sync("RELAY_HOST", "127.0.0.1") == "127.0.0.1"


async def test_finish_chat_coalesce_nulls_no_pisan(db):
    """Bug fix iter 10.4: finish_chat con None no debe pisar valores
    ya escritos (tokens, error, tool_calls). El primer finish_chat
    escribe 10/20/3; un segundo con None mantiene esos valores."""
    cid = await db.create_chat(
        project_slug="inventorydemo", source="discord", author="j", target="inventorydemo")
    await db.finish_chat(cid, status="ok", md_path="/tmp/x.md",
                         tokens_in=100, tokens_out=200, tool_calls=5,
                         error="primer error", phase_at_end="done",
                         last_tool="read_file")
    # Segundo finish_chat con None: los COALESCE deben preservar
    await db.finish_chat(cid, status="cancelled",
                         tokens_in=None, tokens_out=None, tool_calls=None,
                         error=None, phase_at_end=None, last_tool=None)
    chat = await db.get_chat(cid)
    assert chat["status"] == "cancelled"  # status NO tiene COALESCE (escribir siempre)
    assert chat["tokens_in"] == 100
    assert chat["tokens_out"] == 200
    assert chat["tool_calls"] == 5
    assert chat["error"] == "primer error"
    assert chat["phase_at_end"] == "done"
    assert chat["last_tool"] == "read_file"


async def test_finish_chat_tool_bytes(db):
    """tool_bytes (2026-07-26) se persiste y un segundo finish_chat
    con None no lo pisa (COALESCE)."""
    cid = await db.create_chat(
        project_slug="inventorydemo", source="discord", author="j", target="inventorydemo")
    await db.finish_chat(cid, status="ok",
                         tool_bytes='{"read_file": 12345}')
    chat = await db.get_chat(cid)
    assert chat["tool_bytes"] == '{"read_file": 12345}'

    # Segundo finish_chat sin tool_bytes → COALESCE preserva
    await db.finish_chat(cid, status="ok", tool_bytes=None)
    chat = await db.get_chat(cid)
    assert chat["tool_bytes"] == '{"read_file": 12345}'
