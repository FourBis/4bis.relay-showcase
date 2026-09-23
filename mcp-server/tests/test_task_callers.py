from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic_ai.usage import RunUsage

from relay import expert_iteration, file_tools, identity, planificador, server, task_service
from relay.app_state import DB_KEY, GRAFOS_KEY, PROGRESS_KEY, RUNNING_KEY
from relay.db import Database
from relay.execution_policy import ExecutionPolicy
from relay.expert_run_state import ExpertRunState
from relay.server_expert_routes import experts_cancel, experts_steer
from tests.test_task_continuity import make_repo


async def _managed_db(tmp_path: Path) -> tuple[Database, str]:
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    cid = await db.create_conversation(project_slug="demo")
    await db.update_conversation_task(cid, mode="write", state="ready",
                                      tracking={"enabled": False})
    return db, cid


async def _event_client(db: Database, monkeypatch) -> TestClient:
    app = web.Application()
    app[DB_KEY] = db
    app[RUNNING_KEY] = {}
    app[PROGRESS_KEY] = {}
    app.router.add_post("/experts/cancel/{chat_id}", experts_cancel)
    app.router.add_post("/experts/steer/{chat_id}", experts_steer)
    monkeypatch.setattr(task_service, "start_pending", lambda *_args: None)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_managed_steer_request_id_is_durable_and_idempotent(tmp_path, monkeypatch):
    db, cid = await _managed_db(tmp_path)
    original = await db.enqueue_conversation_event(
        cid, "request:original", "run", {"user": "trabaja", "role": "owner"})
    client = await _event_client(db, monkeypatch)
    try:
        body = {"message": "prioriza el bug", "request_id": "steer-1"}
        first = await client.post(f"/experts/steer/{original['chat_id']}", json=body)
        second = await client.post(f"/experts/steer/{original['chat_id']}", json=body)

        assert first.status == second.status == 200
        assert (await first.json())["chat_id"] == (await second.json())["chat_id"]
        events = await db.list_conversation_events(cid)
        assert [event["event_key"] for event in events] == [
            "request:original", "steer:steer-1"]
        assert events[1]["payload"]["user"] == "prioriza el bug"
    finally:
        await client.close()


async def test_cancel_pending_managed_event_pauses_without_model(tmp_path, monkeypatch):
    db, cid = await _managed_db(tmp_path)
    event = await db.enqueue_conversation_event(
        cid, "request:cancel", "run", {"user": "trabaja", "role": "owner"})
    client = await _event_client(db, monkeypatch)
    try:
        response = await client.post(f"/experts/cancel/{event['chat_id']}")
        assert response.status == 200
        assert (await response.json())["conversation_id"] == cid
        stored = await db.conversation_event_for_chat(event["chat_id"])
        assert stored["state"] == "cancelled"
        assert (await db.get_conversation_task(cid))["state"] == "paused"
        assert (await db.get_chat(event["chat_id"]))["status"] == "cancelled"
    finally:
        await client.close()


async def test_member_cannot_pause_write_task_through_legacy_cancel(tmp_path, monkeypatch):
    db, cid = await _managed_db(tmp_path)
    event = await db.enqueue_conversation_event(
        cid, "request:owner", "run", {"user": "trabaja", "role": "owner"})
    client = await _event_client(db, monkeypatch)
    monkeypatch.setattr(identity, "role_of", lambda _request: "member")
    try:
        response = await client.post(f"/experts/cancel/{event['chat_id']}")
        assert response.status == 403
        assert (await db.get_conversation_task(cid))["state"] == "ready"
        assert (await db.conversation_event_for_chat(event["chat_id"]))["state"] == "pending"
    finally:
        await client.close()


async def test_graph_without_conversation_plans_inside_persisted_workspace(tmp_path, monkeypatch):
    source = make_repo(tmp_path)
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo",
                             "repo_path": str(source), "defaults_json": {}})
    captured = {}

    async def fake_plan(project, objetivo, *, db, conversation_id, **_kwargs):
        captured.update(project=project, conversation_id=conversation_id)
        return await db.create_task_graph(
            "g_workspace", objetivo,
            tareas=[{"id": "g_workspace_t1", "titulo": "Aplicar cambio"}],
            conversation_id=conversation_id,
            project_slug=project["slug"])

    monkeypatch.setattr(planificador, "armar_grafo", fake_plan)
    app = web.Application()
    app[DB_KEY] = db
    app[GRAFOS_KEY] = {}
    app.router.add_post("/graphs", server.graphs_create)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/graphs", json={
            "project": "demo", "objetivo": "corrige el archivo",
            "request_id": "graph-workspace", "arrancar": False})
        assert response.status == 202, await response.text()
        body = await response.json()

    cid = body["conversation_id"]
    task = await db.get_conversation_task(cid)
    assert captured["conversation_id"] == cid
    assert Path(captured["project"]["repo_path"]) == Path(task["workspace_path"])
    assert Path(task["workspace_path"]) != source
    graph = await db.get_task_graph("g_workspace")
    assert graph["conversation_id"] == cid
    assert (await db.get_conversation(cid))["project_slug"] == "demo"


def test_feedback_forces_closed_file_sandbox_and_restricted_tools(tmp_path, monkeypatch):
    extra = tmp_path / "extra"
    extra.mkdir()
    monkeypatch.setenv("FOURBIS_SANDBOX", "0")
    monkeypatch.setenv("FOURBIS_EXTRA_ROOTS", str(extra))
    defaults = {"task_feedback": True, "sandbox": False,
                "rutas_extra": [str(extra)]}

    permisos, abierto, _reason = file_tools.permisos_del_run(
        {"slug": "demo", "repo_path": str(tmp_path)}, defaults,
        conversation_id="feedback-conversation")
    policy = ExecutionPolicy.for_run(defaults)

    assert not abierto and not permisos.abierto
    assert permisos.extras == ()
    assert not policy.unrestricted_tools and policy.sql_read_only


async def test_iteration_passes_task_budget_to_actual_sdk_usage_limits():
    captured = {}

    class FakeRun:
        def __init__(self):
            self.result = SimpleNamespace(
                output="ok", usage=RunUsage(), all_messages=lambda: [])

        def __aiter__(self):
            async def empty():
                if False:
                    yield None
            return empty()

    class IterContext:
        async def __aenter__(self):
            return FakeRun()

        async def __aexit__(self, *_args):
            return False

    class FakeAgent:
        def iter(self, *_args, **kwargs):
            captured.update(kwargs)
            return IterContext()

    state = ExpertRunState(
        _idle_to=60, _mcp_tool_to=60, _think_to=60, user="trabaja",
        steer=[], request_limit=7, task_token_limit=1234, task_usage=RunUsage())

    await expert_iteration.run_iteration(FakeAgent(), state, lambda **_kwargs: None)

    limits = captured["usage_limits"]
    assert limits.request_limit == 7
    assert limits.total_tokens_limit == 1234
    assert captured["usage"] is state.task_usage
