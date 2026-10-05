from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from pydantic_ai.usage import RunUsage

from relay import expert_iteration, file_tools, github_credentials, identity, planificador, server, server_expert_routes, task_service
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


@pytest.mark.parametrize("mode", ["read_only", "write"])
async def test_pregunta_detiene_cierre_y_publicacion_de_tarea(tmp_path, monkeypatch, mode):
    from unittest.mock import AsyncMock
    from relay import server_expert_jobs, task_pr, task_workspace
    from relay.app_state import NOTIFY_KEY, SKILLS_KEY

    db, cid = await _managed_db(tmp_path)
    await db.update_conversation_task(cid, mode=mode, publish_allowed=True)
    event = await db.enqueue_conversation_event(cid, "question", "run", {"user": "trabaja"})
    async def asking(**kwargs):
        await db.create_expert_question("q", event["chat_id"], '{"title":"¿Continúo?"}',
                                        conversation_id=cid, project_slug="demo")
        await db.finish_chat(event["chat_id"], status="ok", phase_at_end="question")
    monkeypatch.setattr(server_expert_jobs, "_run_expert_bg", asking)
    inspect = AsyncMock()
    publish = AsyncMock()
    monkeypatch.setattr(task_workspace, "inspect_workspace", inspect)
    monkeypatch.setattr(task_pr, "publish", publish)
    app = {DB_KEY: db, RUNNING_KEY: {}, PROGRESS_KEY: {}, GRAFOS_KEY: {},
           NOTIFY_KEY: None, SKILLS_KEY: SimpleNamespace(get_block=AsyncMock(return_value=""))}
    await task_service._run_event(app, await db.get_project("demo"), cid, event)
    assert (await db.get_conversation_task(cid))["state"] == "blocked"
    assert (await db.get_expert_question("q"))["status"] == "open"
    inspect.assert_not_awaited()
    publish.assert_not_awaited()

    # Responder conserva la pausa; el control explícito permite el siguiente turno.
    from relay.server_graph_routes import _answer_question
    from relay.server_task_routes import _continue
    import json
    web_app = web.Application()
    web_app.update(app)
    request = make_mocked_request("POST", "/questions/q/answer", app=web_app)
    request[identity.IDENTITY_KEY] = identity.OWNER
    response = await _answer_question(
        request, await db.get_expert_question("q"),
        {"conversation_id": cid, "project_slug": "demo"}, {"text": "Puedes continuar"})
    assert response.status == 200
    assert "Puedes continuar" in json.loads(response.text)["resume_prompt"]
    assert (await db.get_conversation_task(cid))["state"] == "blocked"
    await db.finish_conversation_event(event["id"])
    project = await db.get_project("demo")
    monkeypatch.setattr(task_workspace, "resolved_project", AsyncMock(return_value=project))
    await _continue(db, project, cid, {})
    assert (await db.get_conversation_task(cid))["state"] == "ready"
    publish.assert_not_awaited()


async def _event_client(db: Database, monkeypatch) -> TestClient:
    @web.middleware
    async def actor(request, handler):
        request[identity.IDENTITY_KEY] = request.headers.get("X-Test-Actor", identity.OWNER)
        return await handler(request)

    app = web.Application(middlewares=[actor])
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


@pytest.mark.parametrize(("actor", "action", "expected"), [
    ("creator@example.test", "cancel", 200),
    ("creator@example.test", "steer", 200),
    ("other@example.test", "cancel", 403),
    ("other@example.test", "steer", 403),
])
async def test_read_only_managed_event_controls_follow_creator(
        tmp_path, monkeypatch, actor, action, expected):
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    cid = await db.create_conversation(
        project_slug="demo", requested_by="creator@example.test")
    await db.update_conversation_task(cid, mode="read_only", state="ready",
                                      requested_by="creator@example.test")
    original = await db.enqueue_conversation_event(
        cid, "request:readonly", "run", {"user": "solo leer", "role": "member"})
    monkeypatch.setattr(identity, "_roles", {
        "creator@example.test": "member", "other@example.test": "member"})
    monkeypatch.setattr(identity, "_project_grants", {})
    client = await _event_client(db, monkeypatch)
    try:
        before_events = await db.list_conversation_events(cid)
        before_task = await db.get_conversation_task(cid)
        if action == "cancel":
            response = await client.post(
                f"/experts/cancel/{original['chat_id']}",
                headers={"X-Test-Actor": actor})
        else:
            response = await client.post(
                f"/experts/steer/{original['chat_id']}",
                json={"message": "prioriza la lectura", "request_id": "readonly-steer"},
                headers={"X-Test-Actor": actor})
        assert response.status == expected
        if expected == 403:
            assert await db.list_conversation_events(cid) == before_events
            assert await db.get_conversation_task(cid) == before_task
            assert (await db.conversation_event_for_chat(original["chat_id"]))["state"] == "pending"
        elif action == "cancel":
            assert (await db.conversation_event_for_chat(original["chat_id"]))["state"] == "cancelled"
            assert (await db.get_conversation_task(cid))["state"] == "paused"
        else:
            events = await db.list_conversation_events(cid)
            assert len(events) == 2
            assert events[1]["payload"]["requested_by"] == actor
    finally:
        await client.close()


async def test_assigned_writer_can_cancel_managed_write_event(tmp_path, monkeypatch):
    db, cid = await _managed_db(tmp_path)
    event = await db.enqueue_conversation_event(
        cid, "request:writer", "run", {"user": "trabaja", "role": "owner"})
    monkeypatch.setattr(identity, "_roles", {"writer@example.test": "member"})
    monkeypatch.setattr(identity, "_project_grants", {"writer@example.test": ["demo"]})
    client = await _event_client(db, monkeypatch)
    try:
        response = await client.post(
            f"/experts/cancel/{event['chat_id']}",
            headers={"X-Test-Actor": "writer@example.test"})
        assert response.status == 200
        assert (await db.conversation_event_for_chat(event["chat_id"]))["state"] == "cancelled"
        assert (await db.get_conversation_task(cid))["state"] == "paused"
    finally:
        await client.close()


async def test_unrelated_member_cannot_cancel_standalone_chat(tmp_path, monkeypatch):
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    chat_id = await db.create_chat(
        project_slug="demo", source="test", author="owner", target="demo",
        requested_by="owner@example.test")
    monkeypatch.setattr(identity, "_roles", {"other@example.test": "member"})
    monkeypatch.setattr(identity, "_project_grants", {})
    client = await _event_client(db, monkeypatch)
    async def wait_for_cancel():
        await asyncio.Future()

    worker = asyncio.create_task(wait_for_cancel())
    client.server.app[RUNNING_KEY][chat_id] = worker
    try:
        response = await client.post(
            f"/experts/cancel/{chat_id}",
            headers={"X-Test-Actor": "other@example.test"})
        assert response.status == 403
        assert (await db.get_chat(chat_id))["status"] == "running"
        assert not worker.done() and not worker.cancelling()
        assert client.server.app[RUNNING_KEY][chat_id] is worker
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await client.close()


@pytest.mark.parametrize(("actor", "requested_by", "grants"), [
    ("creator@example.test", "creator@example.test", {}),
    ("writer@example.test", "owner@example.test", {"writer@example.test": ["demo"]}),
])
async def test_standalone_chat_cancel_allows_creator_or_project_writer(
        tmp_path, monkeypatch, actor, requested_by, grants):
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    chat_id = await db.create_chat(
        project_slug="demo", source="test", author="owner", target="demo",
        requested_by=requested_by)
    monkeypatch.setattr(identity, "_roles", {actor: "member"})
    monkeypatch.setattr(identity, "_project_grants", grants)
    client = await _event_client(db, monkeypatch)

    async def wait_for_cancel():
        try:
            await asyncio.Future()
        finally:
            await db.finish_chat(chat_id, status="cancelled")

    worker = asyncio.create_task(wait_for_cancel())
    client.server.app[RUNNING_KEY][chat_id] = worker
    try:
        response = await client.post(
            f"/experts/cancel/{chat_id}", headers={"X-Test-Actor": actor})
        assert response.status == 200
        assert (await db.get_chat(chat_id))["status"] == "cancelled"
        assert not worker.done() or worker.cancelling()
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await client.close()


@pytest.mark.parametrize("disconnect", [False, True], ids=["connected", "disconnected"])
async def test_standalone_cancel_waits_for_worker_finalization(tmp_path, monkeypatch, disconnect):
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    actor = "creator@example.test"
    chat_id = await db.create_chat(
        project_slug="demo", source="test", author="member", target="demo",
        requested_by=actor)
    monkeypatch.setattr(identity, "_roles", {actor: "member"})
    monkeypatch.setattr(identity, "_project_grants", {})
    client = await _event_client(db, monkeypatch)
    app = client.server.app
    cleanup, release = asyncio.Event(), asyncio.Event()

    async def worker_body():
        try:
            await asyncio.Future()
        finally:
            cleanup.set()
            await release.wait()
            await db.finish_chat(chat_id, status="cancelled")

    worker = asyncio.create_task(worker_body())
    app[RUNNING_KEY][chat_id] = worker
    background = []
    spawn_bg = server_expert_routes._spawn_bg

    def track_background(coro, **kwargs):
        task = spawn_bg(coro, **kwargs)
        background.append(task)
        return task

    monkeypatch.setattr(server_expert_routes, "_spawn_bg", track_background)
    try:
        if disconnect:
            request = make_mocked_request(
                "POST", f"/experts/cancel/{chat_id}", app=app,
                match_info={"chat_id": chat_id})
            request[identity.IDENTITY_KEY] = actor
            request_task = asyncio.create_task(server_expert_routes.experts_cancel(request))
            await asyncio.wait_for(cleanup.wait(), 2)
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task
            assert not worker.done()
            assert (await db.get_chat(chat_id))["status"] == "running"
            release.set()
            await asyncio.gather(worker, return_exceptions=True)
            await asyncio.wait_for(asyncio.gather(*background), 2)
        else:
            request_task = asyncio.create_task(client.post(
                f"/experts/cancel/{chat_id}", headers={"X-Test-Actor": actor}))
            await asyncio.wait_for(cleanup.wait(), 2)
            assert not request_task.done()
            assert (await db.get_chat(chat_id))["status"] == "running"
            release.set()
            response = await asyncio.wait_for(request_task, 2)
            assert response.status == 200
            await asyncio.gather(worker, return_exceptions=True)
        assert worker.done()
        assert (await db.get_chat(chat_id))["status"] == "cancelled"
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, *background, return_exceptions=True)
        await client.close()


async def test_second_standalone_cancel_does_not_recancel_closing_worker(
        tmp_path, monkeypatch):
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    actor = "creator@example.test"
    chat_id = await db.create_chat(
        project_slug="demo", source="test", author="member", target="demo",
        requested_by=actor)
    monkeypatch.setattr(identity, "_roles", {actor: "member"})
    monkeypatch.setattr(identity, "_project_grants", {})
    client = await _event_client(db, monkeypatch)
    cleanup, release = asyncio.Event(), asyncio.Event()

    async def worker_body():
        try:
            await asyncio.Future()
        finally:
            cleanup.set()
            await release.wait()
            await db.finish_chat(chat_id, status="cancelled")

    worker = asyncio.create_task(worker_body())
    client.server.app[RUNNING_KEY][chat_id] = worker
    original_get_chat = db.get_chat
    get_chat_calls = 0
    second_finalizer_started = asyncio.Event()

    async def observed_get_chat(requested_id):
        nonlocal get_chat_calls
        get_chat_calls += 1
        if get_chat_calls >= 4:
            second_finalizer_started.set()
        return await original_get_chat(requested_id)

    monkeypatch.setattr(db, "get_chat", observed_get_chat)
    try:
        first = asyncio.create_task(client.post(
            f"/experts/cancel/{chat_id}", headers={"X-Test-Actor": actor}))
        await asyncio.wait_for(cleanup.wait(), 2)
        assert worker.cancelling() == 1
        second = asyncio.create_task(client.post(
            f"/experts/cancel/{chat_id}", headers={"X-Test-Actor": actor}))
        await asyncio.wait_for(second_finalizer_started.wait(), 2)
        await asyncio.sleep(0)
        assert worker.cancelling() == 1
        release.set()
        first_response, second_response = await asyncio.wait_for(
            asyncio.gather(first, second), 2)
        assert first_response.status == 200
        assert second_response.status in {200, 409}
        assert worker.done() and worker.cancelling() == 1
        assert (await original_get_chat(chat_id))["status"] == "cancelled"
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await client.close()


async def test_closing_standalone_run_returns_409_without_mutation(tmp_path, monkeypatch):
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    actor = "creator@example.test"
    chat_id = await db.create_chat(
        project_slug="demo", source="test", author="member", target="demo",
        requested_by=actor)
    monkeypatch.setattr(identity, "_roles", {actor: "member"})
    monkeypatch.setattr(identity, "_project_grants", {})
    client = await _event_client(db, monkeypatch)

    async def wait_for_cancel():
        await asyncio.Future()

    worker = asyncio.create_task(wait_for_cancel())
    app = client.server.app
    app[RUNNING_KEY][chat_id] = worker
    progress = SimpleNamespace(closing=True, finished=False)
    app[PROGRESS_KEY][chat_id] = progress
    try:
        before = await db.get_chat(chat_id)
        response = await client.post(
            f"/experts/cancel/{chat_id}", headers={"X-Test-Actor": actor})
        assert response.status == 409
        assert await db.get_chat(chat_id) == before
        assert not worker.done() and not worker.cancelling()
        assert app[RUNNING_KEY][chat_id] is worker
        assert progress.closing and not progress.finished
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await client.close()


async def test_unrelated_member_cannot_cancel_standalone_zombie(tmp_path, monkeypatch):
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    chat_id = await db.create_chat(
        project_slug="demo", source="test", author="owner", target="demo",
        requested_by="owner@example.test")
    monkeypatch.setattr(identity, "_roles", {"other@example.test": "member"})
    monkeypatch.setattr(identity, "_project_grants", {})
    client = await _event_client(db, monkeypatch)
    try:
        before = await db.get_chat(chat_id)
        response = await client.post(
            f"/experts/cancel/{chat_id}",
            headers={"X-Test-Actor": "other@example.test"})
        assert response.status == 403
        assert await db.get_chat(chat_id) == before
    finally:
        await client.close()


@pytest.mark.parametrize(("actor", "roles", "grants", "expected"), [
    pytest.param("member@example.test", {"member@example.test": "member"}, {}, 403,
                 id="mixed-prefix-unauthorized"),
    pytest.param(identity.OWNER, {}, {}, 409, id="owner"),
    pytest.param("writer@example.test", {"writer@example.test": "member"},
                 {"writer@example.test": ["demo"]}, 409, id="project-writer"),
])
async def test_ambiguous_cancel_prefix_never_mutates(
        tmp_path, monkeypatch, actor, roles, grants, expected):
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    original_ids = [
        await db.create_chat(project_slug="demo", source="test", author="member",
                             target="demo", requested_by=actor),
        await db.create_chat(project_slug="demo", source="test", author="owner",
                             target="demo", requested_by="owner@example.test"),
    ]
    chat_ids = [
        "10000000-0000-0000-0000-000000000001",
        "10000000-0000-0000-0000-000000000002",
    ]
    for original_id, chat_id in zip(original_ids, chat_ids):
        await db.run("UPDATE chats SET id=? WHERE id=?", (chat_id, original_id))
    monkeypatch.setattr(identity, "_roles", roles)
    monkeypatch.setattr(identity, "_project_grants", grants)
    client = await _event_client(db, monkeypatch)

    async def wait_for_cancel():
        await asyncio.Future()

    workers = [asyncio.create_task(wait_for_cancel()) for _ in chat_ids]
    client.server.app[RUNNING_KEY].update(zip(chat_ids, workers))
    try:
        before = [await db.get_chat(chat_id) for chat_id in chat_ids]
        response = await client.post(
            "/experts/cancel/10000000-0000-0000-0000-00000000000",
            headers={"X-Test-Actor": actor})
        assert response.status == expected
        assert [await db.get_chat(chat_id) for chat_id in chat_ids] == before
        assert all(not worker.done() and not worker.cancelling() for worker in workers)
    finally:
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await client.close()


async def test_standalone_cancel_tolerates_natural_completion_during_authorization(
        tmp_path, monkeypatch):
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    actor = "creator@example.test"
    chat_id = await db.create_chat(
        project_slug="demo", source="test", author="member", target="demo",
        requested_by=actor)
    monkeypatch.setattr(identity, "_roles", {actor: "member"})
    monkeypatch.setattr(identity, "_project_grants", {})
    client = await _event_client(db, monkeypatch)
    completed = asyncio.Event()

    async def worker_body():
        await completed.wait()

    worker = asyncio.create_task(worker_body())
    running = client.server.app[RUNNING_KEY]
    running[chat_id] = worker
    get_chat = db.get_chat
    first_read = True

    async def finish_during_authorization(requested_chat_id):
        nonlocal first_read
        snapshot = await get_chat(requested_chat_id)
        if requested_chat_id == chat_id and first_read:
            first_read = False
            completed.set()
            await worker
            running.pop(chat_id, None)
            await db.finish_chat(chat_id, status="ok")
        return snapshot

    monkeypatch.setattr(db, "get_chat", finish_during_authorization)
    try:
        response = await client.post(
            f"/experts/cancel/{chat_id}", headers={"X-Test-Actor": actor})
        assert response.status < 500
        body = await response.json()
        assert chat_id not in body.get("cancelled", [])
        assert (await get_chat(chat_id))["status"] == "ok"
    finally:
        completed.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await client.close()


async def test_graph_without_conversation_plans_inside_persisted_workspace(tmp_path, monkeypatch):
    async def fake_github_account(provider):
        assert provider == "github"
        return {"access_token": "test-token", "subject": "123", "login": "test-user"}
    monkeypatch.setattr(
        github_credentials.user_accounts, "require_account", fake_github_account)
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
