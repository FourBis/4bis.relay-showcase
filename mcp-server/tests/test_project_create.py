"""Tests del alta directa de proyectos: POST /admin/api/projects.

Cubre el contrato de docs/NUEVO_PROYECTO.md: validaciones de path,
slug único (409), create_dir + README seed, git_init best-effort y
la degradación sin cbm (index_now sin binario → nota, no error).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "mcp-server" / "src"))

from aiohttp.test_utils import TestClient, TestServer

from relay.db import Database  # noqa: E402
from relay.server import create_app  # noqa: E402


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmp:
        env_vars = {
            "STATE_DIR": str(Path(tmp) / "state"),
            "FOURBIS_DB_PATH": str(Path(tmp) / "relay.db"),
            "FOURBIS_CHATS_DIR": str(Path(tmp) / "chats"),
            "FOURBIS_JSONL_DIR": str(Path(tmp) / "jsonl"),
            "FOURBIS_MODEL": "test",
            "LOG_LEVEL": "WARNING",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            db = Database(path=Path(tmp) / "relay.db")
            await db.init_schema()
            app = create_app()
            cli = TestClient(TestServer(app))
            await cli.start_server()
            try:
                yield cli, db, Path(tmp)
            finally:
                await cli.close()


async def test_create_ok_repo_existente(env):
    cli, db, tmp = env
    repo = tmp / "Mi Proyecto X"
    repo.mkdir()
    # index_now=False para no depender de si cbm está instalado acá.
    r = await cli.post("/admin/api/projects", json={
        "repo_path": str(repo), "description": "demo",
    })
    assert r.status == 201
    body = await r.json()
    # slug auto del basename, slugificado
    assert body["project"]["slug"] == "mi-proyecto-x"
    assert body["project"]["name"] == "Mi Proyecto X"
    assert body["index_job_id"] is None

    saved = await db.get_project("mi-proyecto-x")
    assert saved is not None
    assert saved["repo_path"] == str(repo)
    assert "cbm_query" in saved["system_prompt"]
    assert saved["native_tools"] == ["cbm"]
    # 2026-08-16: un proyecto nuevo YA NO nace con el wrapper MCP
    # (docs/WRAPPER.md). Filesystem y shell son nativos del relay, así
    # que un proyecto recién creado no arranca ningún subprocess: los MCP
    # que quiera se agregan a mano desde el módulo de MCP.
    assert saved["mcp_servers"] == []
    assert "4bis-wrapper" not in json.dumps(saved["mcp_servers"])


async def test_create_slug_duplicado_409(env):
    cli, db, tmp = env
    repo = tmp / "repo1"
    repo.mkdir()
    await db.upsert_project({
        "slug": "repo1", "name": "Repo1", "repo_path": str(repo),
        "system_prompt": "", "mcp_servers": [],
    })
    r = await cli.post("/admin/api/projects", json={"repo_path": str(repo)})
    assert r.status == 409
    assert "ya existe" in (await r.json())["error"]


async def test_create_validaciones_400(env):
    cli, _, tmp = env
    # sin repo_path
    r = await cli.post("/admin/api/projects", json={})
    assert r.status == 400
    # path relativo
    r = await cli.post("/admin/api/projects", json={"repo_path": "rel/x"})
    assert r.status == 400
    # dir inexistente sin create_dir
    r = await cli.post("/admin/api/projects",
                       json={"repo_path": str(tmp / "no-existe")})
    assert r.status == 400
    assert "create_dir" in (await r.json())["error"]
    # repo_path apunta a un archivo
    f = tmp / "archivo.txt"
    f.write_text("x", encoding="utf-8")
    r = await cli.post("/admin/api/projects", json={"repo_path": str(f)})
    assert r.status == 400


async def test_create_dir_seed_y_git_init(env):
    cli, db, tmp = env
    repo = tmp / "desde-cero"
    r = await cli.post("/admin/api/projects", json={
        "repo_path": str(repo),
        "description": "proyecto nuevo de cero",
        "create_dir": True,
        "git_init": True,
    })
    assert r.status == 201
    body = await r.json()
    assert repo.is_dir()
    readme = repo / "README.md"
    assert readme.exists()
    text = readme.read_text(encoding="utf-8")
    assert "proyecto nuevo de cero" in text
    assert "directorio creado" in body["notes"]
    assert "README.md seed escrito" in body["notes"]
    # git init es best-effort: si git está en PATH tiene que haber .git
    if shutil.which("git"):
        assert (repo / ".git").is_dir()
        assert "git init OK" in body["notes"]
    assert await db.get_project("desde-cero") is not None


async def test_create_dir_no_pisa_contenido_existente(env):
    cli, _, tmp = env
    repo = tmp / "con-cosas"
    repo.mkdir()
    (repo / "main.py").write_text("print('hola')", encoding="utf-8")
    r = await cli.post("/admin/api/projects", json={
        "repo_path": str(repo), "create_dir": True,
    })
    assert r.status == 201
    body = await r.json()
    # carpeta no vacía: nada de README seed
    assert not (repo / "README.md").exists()
    assert "README.md seed escrito" not in body["notes"]


async def test_index_now_sin_cbm_degrada_honesto(env):
    cli, db, tmp = env
    repo = tmp / "sin-cbm"
    repo.mkdir()
    with patch("relay.admin.cbm_binary_path", return_value=None):
        r = await cli.post("/admin/api/projects", json={
            "repo_path": str(repo), "index_now": True,
        })
    assert r.status == 201
    body = await r.json()
    assert body["index_job_id"] is None
    assert "cbm no instalado: sin indexación" in body["notes"]
    # el proyecto se creó igual
    assert await db.get_project("sin-cbm") is not None
