"""Round-trip integration del handshake de la extensión VS Code (ADR-009).

Cubre:
- POST /agents/handshake (200 + sesión registrada)
- POST /agents/handshake (400 si body inválido)
- POST /agents/handshake con mismo sid (upsert / LIFO)
- GET /sessions (snapshot de sesiones vivas)
- LIFO: handshake nuevo con mismo sid pisa el anterior

El push por SSE, `POST /prompts` y `POST /prompts/{id}/response` ya no
existen (2026-08-10): la extensión solo handshakea, los expertos van
async por `POST /experts/run` + `POST /notify`.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
MCP_SRC = HERE.parent / "mcp-server" / "src"
sys.path.insert(0, str(MCP_SRC))

from aiohttp.test_utils import TestClient, TestServer

from relay.server import create_app  # noqa: E402


class _FakeNotify:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.url = "stub://no-notify"

    async def send(self, agent_id, kind, message, metadata=None):
        self.sent.append({
            "agent_id": agent_id, "kind": kind, "message": message,
            "metadata": metadata or {},
        })
        return True

    async def aclose(self):
        pass


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp) / "state"
        state_dir.mkdir()
        (state_dir / "prompts").mkdir()

        env_vars = {
            "STATE_DIR": str(state_dir),
            "BOT_NOTIFY_URL": "http://localhost:9999/notify",
            "LOG_LEVEL": "WARNING",
        }
        fake = _FakeNotify()
        with patch.dict("os.environ", env_vars, clear=False):
            with patch("relay.server_lifecycle.NotifyClient", lambda **kw: fake):
                app = create_app()
                server = TestServer(app)
                cli = TestClient(server)
                await cli.start_server()
                try:
                    yield cli, fake, state_dir
                finally:
                    await cli.close()


SID_A = "a" * 64
SID_B = "b" * 64
WS_INVENTORYDEMO = [{"path": "C:/Users/demo/INVENTORYDEMO", "name": "INVENTORYDEMO"}]
WS_FLY = [{"path": "C:/Users/demo/TravelDemo", "name": "TravelDemo"}]


async def _handshake(cli, name, sid, ws_folders=WS_INVENTORYDEMO, machineId="m"):
    return await cli.post("/agents/handshake", json={
        "name": name,
        "sessionId": sid,
        "machineId": machineId,
        "vscodeVersion": "1.95.0",
        "workspaceFolders": ws_folders,
        "ts": "2026-07-06T00:00:00.000Z",
    })


# ---------- handshake ----------


async def test_health(env):
    cli, _, _ = env
    r = await cli.get("/health")
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert body["service"] == "relay"


async def test_handshake_ok(env):
    cli, _, _ = env
    r = await _handshake(cli, "backend", SID_A)
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True


async def test_handshake_validation(env):
    cli, _, _ = env
    body = {"name": "", "sessionId": SID_A, "machineId": "m", "workspaceFolders": [], "ts": ""}
    assert (await cli.post("/agents/handshake", json=body)).status == 400

    # sessionId corto
    body["name"] = "x"
    body["sessionId"] = "short"
    assert (await cli.post("/agents/handshake", json=body)).status == 400

    # sin machineId
    body["sessionId"] = SID_A
    body["machineId"] = ""
    assert (await cli.post("/agents/handshake", json=body)).status == 400


async def test_handshake_upsert(env):
    cli, _, _ = env
    # primer handshake
    r1 = await _handshake(cli, "backend", SID_A)
    assert r1.status == 200
    # segundo handshake, mismo sid, distinto name → upsert
    r2 = await _handshake(cli, "backend2", SID_A)
    assert r2.status == 200

    # snapshot refleja el último
    r = await cli.get("/sessions")
    assert r.status == 200
    body = await r.json()
    names = sorted(s["name"] for s in body["sessions"])
    assert names == ["backend2"]


# ---------- /sessions ----------


async def test_sessions_empty(env):
    cli, _, _ = env
    r = await cli.get("/sessions")
    assert r.status == 200
    body = await r.json()
    assert body["sessions"] == []
    assert body["available_targets"] == []


async def test_sessions_lists_handshakes(env):
    cli, _, _ = env
    await _handshake(cli, "inventorydemo", SID_A, WS_INVENTORYDEMO)
    await _handshake(cli, "fly", SID_B, WS_FLY)
    r = await cli.get("/sessions")
    body = await r.json()
    names = sorted(s["name"] for s in body["sessions"])
    assert names == ["fly", "inventorydemo"]
    assert sorted(body["available_targets"]) == ["INVENTORYDEMO", "TravelDemo"]
    # cada sesión trae el metadata mínimo
    inventorydemo = next(s for s in body["sessions"] if s["name"] == "inventorydemo")
    assert inventorydemo["last_target"] == "INVENTORYDEMO"
    assert inventorydemo["machineId"] == "m"
    assert inventorydemo["last_handshake_ts"] == "2026-07-06T00:00:00.000Z"
    assert inventorydemo["workspace_folders"] == WS_INVENTORYDEMO


async def test_sessions_sin_workspace_no_aparece_en_targets(env):
    cli, _, _ = env
    await _handshake(cli, "nows", SID_A, ws_folders=[])
    r = await cli.get("/sessions")
    body = await r.json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["last_target"] is None
    assert body["available_targets"] == []
