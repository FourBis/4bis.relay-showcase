"""El progreso no espera al bot; cierre/preguntas sí verifican aceptación.

Transporte HTTP y herramientas simulados: no usa red ni ejecuta comandos.
"""
import asyncio
import json

import httpx
import pytest
from tenacity import wait_none

from relay import experts, logctx, notify


async def _client(handler):
    client = notify.NotifyClient("http://bot.invalid/notify")
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


async def test_batch_of_25_tools_runs_while_bot_is_stalled(tmp_path, monkeypatch):
    from pydantic_ai import Tool
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.toolsets import FunctionToolset

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def bot(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    executed = []

    async def check(index: int) -> str:
        """Registra una comprobación sin efectos externos."""
        await started.wait()
        executed.append(index)
        return "ok"

    def model(messages, info):
        if any(isinstance(part, ToolReturnPart)
               for message in messages for part in message.parts):
            return ModelResponse(parts=[TextPart("listo")])
        return ModelResponse(parts=[
            ToolCallPart("check", {"index": i}, tool_call_id=f"call-{i}")
            for i in range(25)])

    async def catalog(*args, **kwargs):
        return [FunctionToolset(tools=[Tool(check, takes_ctx=False)])], [], []

    monkeypatch.setattr(experts, "build_model", lambda spec: FunctionModel(model))
    monkeypatch.setattr(experts, "_catalog_toolsets", catalog)
    client = await _client(bot)
    store = {}
    progress = experts.make_progress_callback(
        store, client, "batch", "demo", "test")
    try:
        result = await asyncio.wait_for(experts.run_expert(
            {"slug": "demo", "repo_path": str(tmp_path), "id": 1,
             "system_prompt": "", "mcp_servers": [], "native_tools": [],
             "defaults_json": {"model": "fake", "timeout": 5,
                               "idle_timeout_s": 0.2, "native_shell": False,
                               "native_files": False}},
            "comprobar", db=object(), on_progress=progress), timeout=3)
        assert result["phase_at_end"] == "writing"
        assert sorted(executed) == list(range(25))
        assert result["tool_calls"] == store["batch"].tool_calls == 25
        assert len(store["batch"].steps) >= 25
        assert not cancelled.is_set()  # El run acabó sin esperar al bot.
    finally:
        await client.aclose()
    assert cancelled.is_set()
    assert client._progress_task is None


@pytest.mark.parametrize("kind", ["response", "question", "done", "error",
                                  "cancelled", "input_needed"])
async def test_critical_message_cancels_stale_progress_and_waits_for_acceptance(kind):
    started, cancelled, accept = asyncio.Event(), asyncio.Event(), asyncio.Event()
    received = []

    async def bot(request):
        payload = json.loads(request.content)
        received.append(payload["kind"])
        if payload["kind"] == "progress":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        await accept.wait()
        return httpx.Response(200, json={"ok": True})

    client = await _client(bot)
    try:
        await client.queue_progress("chat:a", "progress", "primero")
        await asyncio.wait_for(started.wait(), 1)
        await client.queue_progress("chat:a", "progress", "obsoleto")
        final = asyncio.create_task(client.send("chat:a", kind, "importante"))
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.sleep(0)
        assert not final.done()  # Encolado no significa aceptado.
        accept.set()
        assert await asyncio.wait_for(final, 1) is True
        assert received == ["progress", kind]
        assert client._progress_pending == {}
        assert client._progress_task is None
    finally:
        accept.set()
        await client.aclose()


async def test_pending_progress_is_coalesced_bounded_and_cancelled_on_close():
    sent = []

    def bot(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200)

    client = await _client(bot)
    for i in range(1000):
        await client.queue_progress(f"chat:{i}", "progress", str(i))
    assert len(client._progress_pending) == notify._PROGRESS_PENDING_MAX
    await client.queue_progress("chat:999", "progress", "último")
    assert client._progress_pending["chat:999"]["message"] == "último"
    await client.aclose()  # La task todavía no arrancó: también debe limpiarse.
    await client.queue_progress("chat:late", "progress", "no enviar")
    assert client._progress_pending == {}
    assert client._progress_task is None
    assert sent == []


async def test_finishing_one_agent_preserves_other_agents_latest_progress():
    started = asyncio.Event()
    received = []

    async def bot(request):
        payload = json.loads(request.content)
        received.append((payload["agent_id"], payload["message"]))
        if payload["agent_id"] == "chat:a" and payload["kind"] == "progress":
            started.set()
            await asyncio.Event().wait()
        return httpx.Response(200)

    client = await _client(bot)
    try:
        await client.queue_progress("chat:a", "progress", "a1")
        await asyncio.wait_for(started.wait(), 1)
        await client.queue_progress("chat:b", "progress", "b1")
        await client.queue_progress("chat:b", "progress", "b2")
        await client.queue_progress("chat:a", "progress", "a2 obsoleto")
        assert await client.send("chat:a", "done", "a fin") is True
        worker = client._progress_task
        if worker:
            await asyncio.wait_for(worker, 1)
        assert sorted(received) == sorted([
            ("chat:a", "a1"), ("chat:a", "a fin"), ("chat:b", "b2")])
        assert client._progress_task is None
        assert client._progress_pending == {}
    finally:
        await client.aclose()


async def test_failed_bot_does_not_retry_for_every_progress_event(monkeypatch):
    attempts = []

    def bot(request):
        attempts.append(request)
        raise httpx.ConnectTimeout("bot caído")

    monkeypatch.setattr(notify, "wait_exponential", lambda **kwargs: wait_none())
    client = await _client(bot)
    try:
        await client.queue_progress("chat:a", "progress", "primero")
        worker = client._progress_task
        await asyncio.wait_for(worker, 1)
        assert len(attempts) == 3
        for i in range(1000):
            await client.queue_progress(f"chat:{i}", "progress", "paso")
        assert client._progress_pending == {}
        assert client._progress_task is None
        assert len(attempts) == 3
        # La pausa de progreso no suprime mensajes críticos ni su False real.
        assert await client.send("chat:a", "question", "decisión") is False
        assert len(attempts) == 6
    finally:
        await client.aclose()


async def test_shared_worker_logs_each_payloads_chat_without_changing_callers(caplog):
    started, release = asyncio.Event(), asyncio.Event()

    async def bot(request):
        payload = json.loads(request.content)
        if payload["agent_id"] == "chat:a":
            started.set()
            await release.wait()
            return httpx.Response(200)
        return httpx.Response(200, json={"discarded": True, "reason": "test-b"})

    client = await _client(bot)
    chat_token = logctx.current_chat.set("caller-a")
    project_token = logctx.current_project.set("caller-project-a")
    context_filter = logctx.ChatContextFilter()
    caplog.handler.addFilter(context_filter)
    try:
        await client.queue_progress("chat:a", "progress", "a", {
            "chat_id": "a", "target": "project-a"})
        await asyncio.wait_for(started.wait(), 1)
        assert logctx.current_chat.get() == "caller-a"
        logctx.bind("caller-b", "caller-project-b")
        await client.queue_progress("chat:b", "progress", "b", {
            "chat_id": "b", "target": "project-b"})
        worker = client._progress_task
        release.set()
        await asyncio.wait_for(worker, 1)
        record = next(r for r in caplog.records if "test-b" in r.getMessage())
        assert (record.chat_id, record.project) == ("b", "project-b")
        assert logctx.current_chat.get() == "caller-b"
        assert logctx.current_project.get() == "caller-project-b"
    finally:
        release.set()
        await client.aclose()
        caplog.handler.removeFilter(context_filter)
        logctx.current_chat.reset(chat_token)
        logctx.current_project.reset(project_token)


async def test_concurrent_closures_do_not_lose_other_agents_worker():
    started_a, started_b, release_b = asyncio.Event(), asyncio.Event(), asyncio.Event()
    delivered_b = []

    async def bot(request):
        payload = json.loads(request.content)
        if payload["agent_id"] == "chat:a" and payload["kind"] == "progress":
            started_a.set()
            await asyncio.Event().wait()
        if payload["agent_id"] == "chat:b":
            started_b.set()
            await release_b.wait()
            delivered_b.append(payload["message"])
        return httpx.Response(200)

    client = await _client(bot)
    try:
        await client.queue_progress("chat:a", "progress", "a1")
        await asyncio.wait_for(started_a.wait(), 1)
        await client.queue_progress("chat:b", "progress", "b1")
        assert await asyncio.wait_for(asyncio.gather(
            client.send("chat:a", "done", "a fin"),
            client.send("chat:a", "response", "a respuesta")), 1) == [True, True]
        await asyncio.wait_for(started_b.wait(), 1)
        worker = client._progress_task
        assert worker is not None and not worker.done()
        await client.queue_progress("chat:b", "progress", "b2")
        assert client._progress_task is worker
        release_b.set()
        await asyncio.wait_for(worker, 1)
        assert delivered_b == ["b1", "b2"]
        assert client._progress_task is None
    finally:
        release_b.set()
        await client.aclose()


async def test_live_outputs_match_call_ids_even_when_results_arrive_out_of_order():
    store = {}
    progress = experts.make_progress_callback(store, None, "ids", "demo", "test")
    await progress(phase="tool_call", tool="shell", cmd="build",
                   tool_call_id="build-id")
    await progress(phase="tool_call", tool="shell", cmd="test",
                   tool_call_id="test-id")
    await progress(phase="tool_result", tool="shell", output="(exit=1)",
                   tool_call_id="test-id")
    await progress(phase="tool_result", tool="shell", output="(exit=7)",
                   tool_call_id="unknown-id")
    assert "output" not in store["ids"].steps[0]
    await progress(phase="tool_result", tool="shell", output="(exit=0)",
                   tool_call_id="build-id")
    assert [(step["cmd"], step["output"]) for step in store["ids"].steps] == [
        ("build", "(exit=0)"), ("test", "(exit=1)")]
