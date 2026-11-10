"""Synthetic MCP retry regressions, authored but NOT EXECUTED in this change.

No MCP transport, provider, credential store or real file is involved.
"""
import asyncio
import json
from types import SimpleNamespace
import traceback

import pytest
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ModelRequest, ToolCallPart
from pydantic_ai.tool_manager import ToolManager

from relay import expert_toolsets
from relay.expert_history import _dump_messages


class SyntheticToolset:
    label = "synthetic-only"

    def __init__(self, *, error=None, result=None):
        self.error, self.result, self.calls = error, result, 0

    async def call_tool(self, name, tool_args, ctx, tool):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.parametrize("timeout", [None, 1.0])
@pytest.mark.parametrize("raw, absent", [
    ("Authorization: Bearer SYNTHETIC-NOT-A-REAL-TOKEN", "SYNTHETIC-NOT-A-REAL-TOKEN"),
    ("password=synthetic-only-password", "synthetic-only-password"),
    ("postgres://demo:synthetic-only-password@invalid.example/db", "synthetic-only-password"),
    ("token sk-" + "synthetic" * 4, "synthetic" * 4),
    ('{\r\n  "name":"relay", "password" : "synthetic-only-password", "n":1e+02\r\n}', "synthetic-only-password"),
])
async def test_retry_message_str_sdk_prompt_and_serialized_history_are_sanitized(timeout, raw, absent):
    original = ModelRetry(raw)
    original.__cause__ = RuntimeError(raw)  # SDK-equivalent raw cause, never a real transport.
    wrapped = SyntheticToolset(error=original)
    inflight = {}
    ctx = SimpleNamespace(messages=[], retries={"probe": 1})
    capped = expert_toolsets.CappedToolset(wrapped=wrapped, timeout=timeout, inflight=inflight)
    with pytest.raises(ModelRetry) as raised:
        await capped.call_tool("probe", {}, ctx, None)
    assert raised.value is original
    assert absent not in original.message and absent not in str(original)
    assert original.args == (original.message,)
    assert original.__cause__ is None and original.__suppress_context__ is True
    assert absent not in "".join(traceback.format_exception(original))
    retry = ToolManager._wrap_error_as_retry("probe", ToolCallPart("probe", {}, "synthetic-call"), original)
    assert retry.tool_retry.content == original.message
    saved = _dump_messages([ModelRequest(parts=[retry.tool_retry])])
    assert absent not in saved
    part = json.loads(saved)[0]["parts"][0]
    assert part["content"] == original.message
    assert part["tool_name"] == "probe" and part["tool_call_id"] == "synthetic-call"
    assert wrapped.calls == 1 and ctx.retries == {"probe": 1}
    assert inflight == {}


@pytest.mark.parametrize("text", [
    "diagram.mmd:83: unexpected '#' in label",
    ' {\r\n "name":"rel\\u0061y", "n":1e+02, "items":[1, 2] }\r\n',
    '{ "password": null, "secret": "" }',
    "Unsupported expression: value = get_value()\nUse a string instead.",
])
async def test_innocuous_diagnostic_preserves_exact_text_arguments_and_subclass(text):
    class SpecificRetry(ModelRetry):
        pass
    original = SpecificRetry(text)
    original.diagnostic_tag = "keep-type-and-metadata"
    wrapped = SyntheticToolset(error=original)
    capped = expert_toolsets.CappedToolset(wrapped=wrapped, inflight={})
    with pytest.raises(SpecificRetry) as raised:
        await capped.call_tool("probe", {}, SimpleNamespace(messages=[]), None)
    assert raised.value is original and original.message == text and original.args == (text,)
    assert original.diagnostic_tag == "keep-type-and-metadata"
    assert wrapped.calls == 1 and capped.inflight == {}


async def test_sensitive_retry_keeps_subclass_and_existing_metadata():
    class SpecificRetry(ModelRetry):
        pass
    original = SpecificRetry("password=synthetic-only-password")
    original.diagnostic_tag = "preserved"
    capped = expert_toolsets.CappedToolset(wrapped=SyntheticToolset(error=original), inflight={})
    with pytest.raises(SpecificRetry) as raised:
        await capped.call_tool("probe", {}, SimpleNamespace(messages=[]), None)
    assert raised.value is original and original.diagnostic_tag == "preserved"
    assert original.message == "password=[REDACTED]" and str(original) == original.message
    assert capped.inflight == {}


async def test_direct_mcp_error_keeps_retry_contract_and_suppresses_raw_cause():
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData
    raw = "Authorization: Bearer SYNTHETIC-NOT-A-REAL-TOKEN"
    original = McpError(ErrorData(code=-32603, message=raw))
    wrapped = SyntheticToolset(error=original)
    capped = expert_toolsets.CappedToolset(wrapped=wrapped, inflight={})
    with pytest.raises(ModelRetry) as raised:
        await capped.call_tool("probe", {}, SimpleNamespace(messages=[]), None)
    assert "SYNTHETIC-NOT-A-REAL-TOKEN" not in raised.value.message
    assert raised.value.__cause__ is None and raised.value.__suppress_context__ is True
    assert wrapped.calls == 1 and capped.inflight == {}


@pytest.mark.parametrize("timeout", [None, 1.0])
@pytest.mark.parametrize("error", [RuntimeError("synthetic bug"), asyncio.CancelledError(),
    BaseExceptionGroup("synthetic mixed group", [ModelRetry("unchanged"), asyncio.CancelledError()])])
async def test_other_errors_and_cancellation_propagate_unchanged_and_release_inflight(timeout, error):
    wrapped = SyntheticToolset(error=error)
    capped = expert_toolsets.CappedToolset(wrapped=wrapped, timeout=timeout, inflight={})
    with pytest.raises(type(error)) as raised:
        await capped.call_tool("probe", {}, SimpleNamespace(messages=[]), None)
    assert raised.value is error and wrapped.calls == 1 and capped.inflight == {}


@pytest.mark.parametrize("source", ['{"name":"relay"}', "secret = get_secret()\n"])
async def test_successful_read_contract_is_not_replaced_by_diagnostic_redaction(source):
    wrapped = SyntheticToolset(result=source)
    capped = expert_toolsets.CappedToolset(wrapped=wrapped, inflight={})
    result = await capped.call_tool("read_file", {"path": "module.py"}, SimpleNamespace(messages=[]), None)
    assert result == source and wrapped.calls == 1 and capped.inflight == {}
