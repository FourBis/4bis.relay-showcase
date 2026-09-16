"""Tests del endpoint POST /diagrams/llm (Iter 10.4)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "mcp-server" / "src"))

from aiohttp.test_utils import TestClient, TestServer

from relay.admin import (
    _build_diagram_system_prompt,
    _extract_mermaid,
)
from relay.db import Database


# ── _build_diagram_system_prompt ──

def test_build_prompt_architecture():
    data = {"layers": [{"name": "API", "layer": "api"}]}
    prompt = _build_diagram_system_prompt(data, "architecture", "")
    assert "Arquitectura" in prompt
    assert "```json" in prompt
    assert "API" in prompt


def test_build_prompt_with_user_focus():
    data = {"routes": []}
    prompt = _build_diagram_system_prompt(data, "routes", "solo checkout")
    assert "solo checkout" in prompt
    assert "Enfócate en eso" in prompt


def test_build_prompt_caps_large_data():
    huge = {"items": [{"n": i, "name": "x" * 200} for i in range(200)]}
    prompt = _build_diagram_system_prompt(huge, "architecture", "")
    assert "truncado a 24KB" in prompt
    assert len(prompt) < 40_000


def test_build_prompt_unknown_type_falls_back():
    prompt = _build_diagram_system_prompt({}, "unknown_type", "")
    assert "unknown_type" in prompt


# ── _extract_mermaid ──

def test_extract_mermaid_fence():
    text = "```mermaid\ngraph TD\n  A --> B\n```\n\nExplicacion."
    code, expl = _extract_mermaid(text)
    assert code == "graph TD\n  A --> B"
    assert "Explicacion" in expl


def test_extract_mermaid_no_fence():
    code, expl = _extract_mermaid("graph TD\n  A --> B")
    assert code == "graph TD\n  A --> B"
    assert expl == ""


def test_extract_mermaid_first_fence_only():
    text = "```mermaid\ngraph TD\n  A --> B\n```\n\nBullet\n\n```mermaid\ngraph LR\n  X --> Y\n```"
    code, expl = _extract_mermaid(text)
    assert code == "graph TD\n  A --> B"
    assert "Bullet" in expl


# ── Endpoint HTTP ──

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
            await db.run(
                "INSERT INTO projects (slug, name, repo_path, system_prompt) "
                "VALUES ('test', 'Test', '/tmp/test', '')")
            from relay.server import create_app
            app = create_app()
            cli = TestClient(TestServer(app))
            await cli.start_server()
            try:
                yield cli, db, Path(tmp)
            finally:
                await cli.close()


async def test_diagrams_llm_sin_cbm(env):
    cli, db, tmp = env
    r = await cli.post("/admin/api/projects/test/diagrams/llm",
                       json={"type": "architecture"})
    # Sin cbm, el endpoint responde 503. Pero si el relay interno
    # (RELAY_URL) falla, puede llegar 502. Ambas son válidas: el
    # punto es que el endpoint NO se cuelga ni devuelve 200 vacío.
    assert r.status in (502, 503), f"{r.status}: {await r.text()}"


async def test_diagrams_llm_tipo_invalido(env):
    cli, db, tmp = env
    r = await cli.post("/admin/api/projects/test/diagrams/llm",
                       json={"type": "dinosaurio"})
    assert r.status == 400


async def test_diagrams_llm_sin_tipo(env):
    cli, db, tmp = env
    r = await cli.post("/admin/api/projects/test/diagrams/llm", json={})
    assert r.status == 400


async def test_diagrams_llm_proyecto_inexistente(env):
    cli, db, tmp = env
    r = await cli.post("/admin/api/projects/noexiste/diagrams/llm",
                       json={"type": "architecture"})
    assert r.status == 404


async def test_diagrams_llm_sequence_sin_funcion(env):
    cli, db, tmp = env
    with patch("relay.admin.cbm_binary_path", return_value="/fake/cbm"):
        r = await cli.post("/admin/api/projects/test/diagrams/llm",
                           json={"type": "sequence"})
        assert r.status == 400
        body = await r.json()
        assert "function" in body.get("error", "").lower()


async def test_diagrams_llm_json_invalido(env):
    cli, db, tmp = env
    r = await cli.post("/admin/api/projects/test/diagrams/llm",
                       data="not json",
                       headers={"Content-Type": "application/json"})
    assert r.status == 400


async def test_diagrams_llm_happy_path(env):
    """El camino feliz: cbm devuelve datos, el LLM devuelve un fence.

    Cubre el mapeo del resultado de `run_consult` → JSON de respuesta.
    Sin este test, un cambio en el contrato de run_consult (o leer la
    clave equivocada) pasa verde: fue exactamente el bug de los
    contadores de tokens, que salían None siempre.
    """
    cli, db, tmp = env
    consult_result = {
        "content": "```mermaid\nflowchart LR\n  A --> B\n```\n\n- Dos capas.",
        "model": "minimax/m3",
        "tokens_in": 1234,
        "tokens_out": 56,
        "tool_calls": 0,
        "duration_ms": 900,
    }

    async def fake_consult(**kwargs):
        # El endpoint debe pasar el prompt armado, no el user crudo.
        assert "```json" in kwargs["system_prompt"]
        return consult_result

    async def fake_cbm(*args):
        return ("## rows\nA\tB\n", None)

    with patch("relay.admin.cbm_binary_path", return_value="/fake/cbm"), \
            patch("relay.admin._cbm_cli_text", fake_cbm), \
            patch("relay.experts.run_consult", fake_consult):
        r = await cli.post("/admin/api/projects/test/diagrams/llm",
                           json={"type": "architecture"})
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["mermaid"] == "flowchart LR\n  A --> B"
    assert "Dos capas" in body["explanation"]
    assert body["model"] == "minimax/m3"
    # Los contadores tienen que llegar poblados: la UI los pinta.
    assert body["tokens_in"] == 1234
    assert body["tokens_out"] == 56
