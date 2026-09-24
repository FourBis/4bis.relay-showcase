"""El MCP legado no puede heredar la cuenta GitHub de la máquina."""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def gm(monkeypatch):
    path = Path(__file__).resolve().parent.parent / "mcp_servers" / "github_mcp.py"
    spec = importlib.util.spec_from_file_location("gm_actor_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("GITHUB_TOKEN", "machine-token-must-not-be-used")
    monkeypatch.delenv("RELAY_GITHUB_ACTOR", raising=False)
    return mod


def test_global_token_without_actor_is_not_used(gm):
    assert gm._resolver_token() == ""
    assert "Authorization" not in gm._auth_headers()


def test_only_explicit_actor_token_is_used(gm, monkeypatch):
    monkeypatch.setenv("RELAY_GITHUB_ACTOR", "person@example.test")
    monkeypatch.setenv("GITHUB_TOKEN", "user-token")
    assert gm._auth_headers()["Authorization"] == "Bearer user-token"
    monkeypatch.delenv("GITHUB_TOKEN")
    assert gm._resolver_token() == ""


@pytest.mark.asyncio
async def test_without_actor_never_opens_network_client(gm):
    assert (await gm._get("/user"))[0] == 403
    assert (await gm._post("/repos/o/r/issues", {"title": "blocked"}))[0] == 403
    assert gm._client is None
