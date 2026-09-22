from relay import expert_instructions, expert_models, expert_planning, expert_runner, expert_selection, expert_toolsets
"""Regresiones en activación, permisos y ciclo de vida del MCP real del runner."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from relay import experts, mcp_pool
from relay.db import Database
from relay.execution_policy import request_role


async def test_on_demand_keeps_native_filters_watchdog_and_evidence(tmp_path):
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    await db.upsert_mcp_server(dict(name="browser", capability="browser", command="node",
                                    enabled=True, on_demand=True))
    calls = []
    original = expert_selection._catalog_toolsets

    async def record(*args, **kwargs):
        result = await original(*args, **kwargs)
        calls.append(result)
        return result

    def make(*args, **kwargs):
        ts = FunctionToolset()
        ts.add_function(lambda path: "must be hidden", name="write_file", takes_ctx=False)
        def snapshot():
            assert calls[-1][0][0].wrapped.inflight, "el watchdog debe ver la tool en vuelo"
            return "snapshot"
        ts.add_function(snapshot, name="browser_snapshot", takes_ctx=False)
        return ts, None

    def model(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        written = [p for p in returns if p.tool_name == "write_file"]
        if not written:
            assert not (tmp_path / "once.txt").exists(), "no debe repetir efectos al activar MCP"
            return ModelResponse(parts=[ToolCallPart("write_file", {"path": "once.txt", "content": "once"})])
        assert len(written) == 1
        if "browser_snapshot" not in {t.name for t in info.function_tools}:
            return ModelResponse(parts=[ToolCallPart("use_capability", {"name": "browser"})])
        assert expert_instructions.EVIDENCE_BLOCK in str(info.instructions)
        if not any(p.tool_name == "browser_snapshot" for p in returns):
            return ModelResponse(parts=[ToolCallPart("browser_snapshot", {})])
        return ModelResponse(parts=[TextPart("ready")])

    with patch.object(expert_models, "build_model", return_value=FunctionModel(model)), \
            patch.object(mcp_pool, "make_toolset", side_effect=make), \
            patch.object(expert_selection, "_catalog_toolsets", side_effect=record):
        result = await expert_runner.run_expert(await db.get_project("demo"), "inspect",
                                         db=db, model_override="function")
    assert result["content"] == "ready"
    assert (tmp_path / "once.txt").read_text() == "once"
    assert len(calls) == 2
    capped = calls[1][0][0].wrapped
    assert "write_file" in capped.wrapped.hidden
    assert capped.inflight is not None


@pytest.mark.parametrize("defaults,role", [({"read_only": True}, "owner"),
                                          ({"rutas_vedadas": ["private"]}, "owner"),
                                          ({}, "member")])
async def test_planner_cannot_bypass_run_permissions(defaults, role):
    db, pool = AsyncMock(), AsyncMock()
    token = request_role.set(role)
    try:
        assert await expert_planning._reasoning_toolset(
            db, {"id": 1, "repo_path": ".", "defaults_json": defaults}, pool) == []
        db.mcp_servers_for_project.assert_not_called()
        pool.acquire.assert_not_called()
    finally:
        request_role.reset(token)


async def test_pool_config_change_does_not_kill_active_session(monkeypatch):
    class Toolset:
        is_running = False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass

    transport = AsyncMock()
    ts = Toolset()
    monkeypatch.setattr(mcp_pool, "make_toolset", lambda *a, **k: (ts, transport))
    pool = mcp_pool.McpPool()
    row = dict(name="browser", updated_at="old", transport="stdio")
    await pool.acquire(row, ".")
    ts.is_running = True
    with pytest.raises(mcp_pool.McpConfigBusy):
        await pool.acquire({**row, "updated_at": "new"}, ".")
    transport.close.assert_not_called()
    ts.is_running = False
    await pool.acquire({**row, "updated_at": "new"}, ".")
    transport.close.assert_awaited_once()
    await pool.shutdown()


def test_mcp_transport_honors_project_tool_timeout():
    ts, _ = mcp_pool.make_toolset(dict(command="node", _tool_call_timeout_s=2400), ".")
    assert ts.client._session_kwargs["read_timeout_seconds"].total_seconds() == 2430


async def test_cancelled_handshake_cleans_pool(monkeypatch):
    entered = asyncio.Event()
    class Toolset:
        is_running = False
        async def __aenter__(self):
            entered.set()
            await asyncio.Future()
        async def __aexit__(self, *args): pass

    transport = AsyncMock()
    monkeypatch.setattr(mcp_pool, "make_toolset", lambda *a, **k: (Toolset(), transport))
    pool = mcp_pool.McpPool()
    task = asyncio.create_task(pool.acquire(dict(name="browser"), "."))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not pool._entries
    transport.close.assert_awaited_once()


async def test_text_only_model_still_delivers_image_to_user(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from pydantic_ai.messages import BinaryImage
    from relay import attachments
    monkeypatch.setenv("FOURBIS_ATTACHMENTS_DIR", str(tmp_path))
    photo = BinaryImage(data=b"png-bytes", media_type="image/png")
    source = SimpleNamespace(call_tool=AsyncMock(return_value=["captura", photo]))
    sink = {}
    capped = expert_toolsets.CappedToolset(wrapped=source, image_artifacts=sink, vision=False)
    output = await capped.call_tool("browser_take_screenshot", {}, SimpleNamespace(messages=[]), None)
    assert photo not in output
    assert "NO se envió al modelo" in str(output)
    assert len(sink) == 1 and attachments.resolve(next(iter(sink))).read_bytes() == photo.data


async def test_read_image_obeys_veto_and_size_cap(tmp_path, monkeypatch):
    from relay import file_tools, files
    (tmp_path / "private").mkdir()
    (tmp_path / "private/secret.png").write_bytes(b"secret")
    (tmp_path / "big.png").write_bytes(b"too-big")
    tool = next(t for t in file_tools.file_tools(files.Permisos.para(str(tmp_path), vedadas=["private"]))
                if t.name == "read_image")
    assert "vedadas" in await tool.function("private/secret.png")
    monkeypatch.setattr(file_tools.attachments_mod, "max_attachment_bytes", lambda: 2)
    assert "excede" in await tool.function("big.png")
