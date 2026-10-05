"""Cancelación de grafos históricos, repetición y permisos sin proveedores."""
import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from relay import coordination, identity, orquestador, server_common, server_graph_helpers, server_graph_routes, server_lifecycle, server_questions
from relay.db import Database


@pytest.fixture
async def graph_client(tmp_path, monkeypatch):
    db = Database(path=tmp_path / "graph.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    monkeypatch.setattr(server_common, "_get_api_key", lambda: "")
    monkeypatch.setattr(identity, "_roles", {
        "dev@example.test": "member", "other@example.test": "member"})
    monkeypatch.setattr(identity, "_disabled", set())
    monkeypatch.setattr(identity, "_project_grants", {
        "dev@example.test": ["demo"], "other@example.test": []})

    @web.middleware
    async def actor(request, handler):
        request[identity.IDENTITY_KEY] = request.headers.get("X-Test-Actor", identity.OWNER)
        return await handler(request)

    app = web.Application(middlewares=[actor, identity.require_role])
    app[server_common.DB_KEY] = db
    app[server_common.GRAFOS_KEY] = {}
    app[server_common.BG_TASKS_KEY] = set()
    app.router.add_post("/graphs/{id}/cancel", server_graph_routes.graphs_cancel)
    app.router.add_post("/graphs/{id}/resume", server_graph_routes.graphs_resume)
    app.router.add_get("/graphs/{id}", server_graph_routes.graphs_get)
    app.router.add_post("/questions/{q_id}/answer", server_graph_routes.expert_question_answer)
    app.router.add_post("/questions/{q_id}/skip", server_questions.expert_question_skip)
    launch = Mock()
    monkeypatch.setattr(server_graph_routes, "_largar_grafo", launch)
    async with TestClient(TestServer(app)) as client:
        yield client, db, app, launch


@pytest.mark.parametrize("project_slug", ["demo", None, "removed"])
async def test_legacy_cancel_is_repeatable_and_preserves_nodes(graph_client, project_slug):
    client, db, app, launch = graph_client
    await db.create_task_graph("legacy", "demo", tareas=[
        {"id": "done", "titulo": "Hecho"},
        {"id": "pending", "titulo": "Pendiente", "deps": ["done"]}],
        project_slug=project_slug)
    await db.update_task("done", estado="hecho", resultado="resultado conservado")
    before = (await db.get_task_graph("legacy"))["tasks"]
    denied = await client.post("/graphs/legacy/cancel", headers={"X-Test-Actor": "other@example.test"})
    assert denied.status == 403
    assert (await db.get_task_graph("legacy"))["estado"] == "activo"
    for _ in range(2):
        response = await client.post("/graphs/legacy/cancel")
        assert response.status == 200
        assert (await response.json())["estado"] == "cancelado"
        assert (await db.get_task_graph("legacy"))["tasks"] == before
    assert not app[server_common.GRAFOS_KEY]
    launch.assert_not_called()


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("restriction", [
    "disabled_user", "finance", "unregistered", "revoked", "disabled_project",
    "read_only_project", "subadmin"])
async def test_cancel_respects_current_access_controls(graph_client, linked, restriction):
    client, db, app, launch = graph_client
    actor = "dev@example.test"
    conv_id = await db.create_conversation(project_slug="demo") if linked else None
    if linked:
        await db.update_conversation_task(conv_id, mode="write", state="running")
    await db.create_task_graph("access", "demo", tareas=[
        {"id": "access-node", "titulo": "Nodo"}], project_slug="demo", conversation_id=conv_id)
    if restriction == "disabled_user":
        identity._disabled.add(actor)
    elif restriction == "finance":
        identity._roles[actor] = "finance"
    elif restriction == "unregistered":
        identity._roles.pop(actor)
    elif restriction == "revoked":
        identity._project_grants[actor] = []
    elif restriction == "disabled_project":
        await db.disable_project("demo")
    elif restriction == "read_only_project":
        await db.upsert_project({"slug": "demo", "defaults_json": {"read_only": True}})
    else:
        identity._roles[actor] = "subadmin"
    before = await db.get_task_graph("access")
    expected = 200 if restriction == "subadmin" else 403
    for _ in range(2):
        response = await client.post("/graphs/access/cancel", headers={"X-Test-Actor": actor})
        assert response.status == expected
        graph = await db.get_task_graph("access")
        assert graph["tasks"] == before["tasks"]
        if expected == 403:
            assert graph == before
        else:
            assert graph["estado"] == "cancelado"
    launch.assert_not_called()


async def test_missing_graph_returns_404(graph_client):
    client, db, app, launch = graph_client
    response = await client.post("/graphs/missing/cancel")
    assert response.status == 404
    assert not app[server_common.GRAFOS_KEY]
    launch.assert_not_called()


@pytest.mark.parametrize("mode", [None, "write", "read_only"])
@pytest.mark.parametrize("actor,expected", [
    ("dev@example.test", 200), ("other@example.test", 403)])
async def test_cancel_checks_permissions_before_stopping_worker(graph_client, mode, actor, expected):
    client, db, app, launch = graph_client
    conv_id = None
    if mode:
        conv_id = await db.create_conversation(project_slug="demo", requested_by="dev@example.test")
        await db.update_conversation_task(conv_id, mode=mode, state="running")
    await db.create_task_graph("running", "demo", tareas=[
        {"id": "node", "titulo": "Nodo"}], project_slug="demo", conversation_id=conv_id)
    before = await db.get_task_graph("running")
    started, stopped = asyncio.Event(), asyncio.Event()

    async def worker():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
            app[server_common.GRAFOS_KEY].pop("running", None)

    task = asyncio.create_task(worker())
    app[server_common.GRAFOS_KEY]["running"] = task
    await started.wait()
    try:
        response = await client.post("/graphs/running/cancel", headers={"X-Test-Actor": actor})
        assert response.status == expected
        if expected == 403:
            assert not stopped.is_set()
            assert not task.done()
            assert await db.get_task_graph("running") == before
        else:
            assert stopped.is_set()
            assert task.cancelled()
            assert not app[server_common.GRAFOS_KEY]
            assert (await db.get_task_graph("running"))["estado"] == "cancelado"
            assert (await client.post("/graphs/running/cancel", headers={"X-Test-Actor": actor})).status == 200
        launch.assert_not_called()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_repeated_cancel_does_not_interrupt_worker_cleanup(graph_client, monkeypatch):
    client, db, app, launch = graph_client
    await db.create_task_graph("cleanup", "demo", tareas=[
        {"id": "finished", "titulo": "Hecho"}], project_slug="demo")
    await db.update_task("finished", estado="hecho", resultado="conservar")
    before = (await db.get_task_graph("cleanup"))["tasks"]
    started, cleaning, release, interrupted = (asyncio.Event() for _ in range(4))
    second_authorized = asyncio.Event()
    check = identity.can_write_project
    calls = 0

    def observed_permission(request, project):
        nonlocal calls
        calls += 1
        if calls == 2:
            second_authorized.set()
        return check(request, project)

    monkeypatch.setattr(identity, "can_write_project", observed_permission)

    async def worker():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.set()
                raise
            finally:
                app[server_common.GRAFOS_KEY].pop("cleanup", None)

    task = asyncio.create_task(worker())
    app[server_common.GRAFOS_KEY]["cleanup"] = task
    await started.wait()
    headers = {"X-Test-Actor": "dev@example.test"}
    first = asyncio.create_task(client.post("/graphs/cleanup/cancel", headers=headers))
    await asyncio.wait_for(cleaning.wait(), 2)
    second = asyncio.create_task(client.post("/graphs/cleanup/cancel", headers=headers))
    try:
        await asyncio.wait_for(second_authorized.wait(), 2)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not interrupted.is_set(), "el reintento volvió a cancelar la limpieza"
        assert not first.done() and not second.done()
    finally:
        release.set()
        responses = await asyncio.gather(first, second)
        await asyncio.gather(task, return_exceptions=True)
    assert [response.status for response in responses] == [200, 200]
    assert (await db.get_task_graph("cleanup"))["tasks"] == before
    launch.assert_not_called()


async def test_interrupted_cancel_request_preserves_worker_cleanup(graph_client, monkeypatch):
    client, db, app, launch = graph_client
    await db.create_task_graph("interrupted", "demo", tareas=[
        {"id": "saved", "titulo": "Hecho"}], project_slug="demo")
    await db.update_task("saved", estado="hecho", resultado="conservar")
    before = (await db.get_task_graph("interrupted"))["tasks"]
    started, cleaning, release, interrupted = (asyncio.Event() for _ in range(4))
    persisted = asyncio.Event()
    set_state = db.set_task_graph_state

    async def observe_state(graph_id, state):
        await set_state(graph_id, state)
        if graph_id == "interrupted" and state == "cancelado":
            persisted.set()

    monkeypatch.setattr(db, "set_task_graph_state", observe_state)

    async def worker():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.set()
                raise
            finally:
                app[server_common.GRAFOS_KEY].pop("interrupted", None)

    task = asyncio.create_task(worker())
    app[server_common.GRAFOS_KEY]["interrupted"] = task
    await started.wait()
    request = make_mocked_request("POST", "/graphs/interrupted/cancel", app=app,
                                  match_info={"id": "interrupted"})
    handler = asyncio.create_task(server_graph_routes.graphs_cancel(request))
    await asyncio.wait_for(cleaning.wait(), 2)
    handler.cancel()
    drain = asyncio.create_task(server_lifecycle._drain_running_experts(app))
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(handler, 2)
        assert not interrupted.is_set(), "la interrupción de la petición cortó la limpieza"
        assert not task.done()
        assert not persisted.is_set()
        assert not drain.done(), "el apagado debe esperar el cierre pendiente"
        assert (await db.get_task_graph("interrupted"))["estado"] == "activo"
    finally:
        release.set()
        await asyncio.gather(handler, task, drain, return_exceptions=True)
    await asyncio.wait_for(persisted.wait(), 2)
    assert (await db.get_task_graph("interrupted"))["estado"] == "cancelado"
    assert (await db.get_task_graph("interrupted"))["tasks"] == before
    assert not app[server_common.GRAFOS_KEY]
    launch.assert_not_called()


@pytest.mark.parametrize("running", [False, True])
async def test_resume_waits_for_cancel_persistence(graph_client, monkeypatch, running):
    client, db, app, launch = graph_client
    await db.create_task_graph("persisting", "demo", tareas=[
        {"id": "pending", "titulo": "Pendiente"}], project_slug="demo")
    started = asyncio.Event()
    executions = 0

    async def execute(*args, **kwargs):
        nonlocal executions
        executions += 1
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(orquestador, "lanzar", execute)
    project = await db.get_project("demo")
    if running:
        app[server_common.PROGRESS_KEY] = {}
        app[server_common.NOTIFY_KEY] = None
        server_graph_helpers._largar_grafo(app, project, "persisting")
        await asyncio.wait_for(started.wait(), 2)
    saving, release = asyncio.Event(), asyncio.Event()
    set_state = db.set_task_graph_state

    async def delayed_state(graph_id, state):
        if graph_id == "persisting" and state == "cancelado":
            saving.set()
            await release.wait()
        await set_state(graph_id, state)

    monkeypatch.setattr(db, "set_task_graph_state", delayed_state)
    cancel = asyncio.create_task(client.post("/graphs/persisting/cancel"))
    try:
        await asyncio.wait_for(saving.wait(), 2)
        assert not app[server_common.GRAFOS_KEY]
        status = await client.get("/graphs/persisting")
        assert (await status.json())["corriendo"] is True
        response = await client.post("/graphs/persisting/resume")
        assert response.status == 409
        launch.assert_not_called()
        assert executions == int(running)
    finally:
        release.set()
        response = await asyncio.wait_for(cancel, 2)
    assert response.status == 200
    assert (await db.get_task_graph("persisting"))["estado"] == "cancelado"
    assert not app[server_common.GRAFOS_KEY]
    status = await client.get("/graphs/persisting")
    assert (await status.json())["corriendo"] is False


async def test_cancel_cannot_overtake_resume_state_check(graph_client, monkeypatch):
    client, db, app, launch = graph_client
    await db.create_task_graph("overlap", "demo", tareas=[
        {"id": "pending", "titulo": "Pendiente"}], project_slug="demo")
    reading, release = asyncio.Event(), asyncio.Event()
    get_task_graph = db.get_task_graph
    lock = server_graph_helpers._graph_control_lock(app, "overlap")

    async def delayed_read(graph_id):
        graph = await get_task_graph(graph_id)
        if graph_id == "overlap" and lock.locked() and not reading.is_set():
            reading.set()
            await release.wait()
        return graph

    monkeypatch.setattr(db, "get_task_graph", delayed_read)
    request = make_mocked_request("POST", "/graphs/overlap/resume", app=app,
                                  match_info={"id": "overlap"})
    resume = asyncio.create_task(server_graph_routes.graphs_resume(request))
    await asyncio.wait_for(reading.wait(), 2)
    cancel = asyncio.create_task(server_graph_helpers._cancelar_grafo(app, "overlap"))
    try:
        # Espera a que la cancelación se registre, sin soltar la lectura.
        async with asyncio.timeout(2):
            while not getattr(db, "_graph_cancellations", {}):
                await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert (await get_task_graph("overlap"))["estado"] == "activo"
        assert not cancel.done()
    finally:
        release.set()
        await asyncio.gather(resume, cancel)
    assert resume.result().status == 409
    launch.assert_not_called()
    assert (await get_task_graph("overlap"))["estado"] == "cancelado"


@pytest.mark.parametrize("action", ["resume", "cancel"])
@pytest.mark.parametrize("actor,expected", [
    ("creator@example.test", 200), ("other@example.test", 403)])
async def test_read_only_graph_controls_belong_to_creator(graph_client, monkeypatch, action, actor, expected):
    client, db, app, launch = graph_client
    identity._roles["creator@example.test"] = "member"
    identity._project_grants["creator@example.test"] = []
    cid = await db.create_conversation(project_slug="demo", requested_by="creator@example.test")
    project = await db.get_project("demo")
    await db.update_conversation_task(
        cid, mode="read_only", state="ready", source_repo=project["repo_path"],
        workspace_path=project["repo_path"], workspace_state="blocked",
        workspace_error="diagnóstico que otro actor no debe borrar")
    await db.create_task_graph("owned", "demo", tareas=[
        {"id": "pending", "titulo": "Pendiente"}], project_slug="demo", conversation_id=cid)
    graph_before = await db.get_task_graph("owned")
    task_before = await db.get_conversation_task(cid)
    resolved = AsyncMock(wraps=coordination.resolved_project)
    monkeypatch.setattr(coordination, "resolved_project", resolved)
    response = await client.post(f"/graphs/owned/{action}", headers={"X-Test-Actor": actor})
    assert response.status == (202 if action == "resume" and expected == 200 else expected)
    if expected == 403:
        assert await db.get_task_graph("owned") == graph_before
        assert await db.get_conversation_task(cid) == task_before
        assert not app[server_common.GRAFOS_KEY]
        launch.assert_not_called()
        resolved.assert_not_called()
    elif action == "resume":
        launch.assert_called_once_with(app, project, "owned", actor)
    else:
        assert (await db.get_task_graph("owned"))["estado"] == "cancelado"
        launch.assert_not_called()


@pytest.mark.parametrize("action", ["answer", "skip"])
@pytest.mark.parametrize("kind", ["grafo", "choice"])
@pytest.mark.parametrize("orphaned", [False, True])
@pytest.mark.parametrize("actor,expected", [
    ("creator@example.test", 200), ("other@example.test", 403)])
async def test_graph_question_controls_belong_to_creator(graph_client, action, kind, orphaned, actor, expected):
    client, db, app, launch = graph_client
    identity._roles["creator@example.test"] = "member"
    identity._project_grants["creator@example.test"] = []
    cid = await db.create_conversation(project_slug="demo", requested_by="creator@example.test")
    await db.update_conversation_task(cid, mode="read_only", state="ready")
    if not orphaned:
        await db.create_task_graph("question", "demo", tareas=[
            {"id": "waiting", "titulo": "Espera"}], project_slug="demo", conversation_id=cid)
        await db.update_task("waiting", estado="esperando_humano", chat_id="chat-question")
    payload = {"title": "¿Reintentar?", "options": [{"key": "reintentar", "label": "Sí"}]}
    if kind == "grafo":
        payload.update(graph_id="question", task_id="waiting")
    await db.create_expert_question("q", "chat-question", json.dumps(payload),
                                   conversation_id=cid, project_slug="demo", kind=kind)
    graph_before = await db.get_task_graph("question")
    question_before = await db.get_expert_question("q")
    response = await client.post(f"/questions/q/{action}", json={"choice": "reintentar"},
                                 headers={"X-Test-Actor": actor})
    assert response.status == expected
    if expected == 403:
        assert await db.get_task_graph("question") == graph_before
        assert await db.get_expert_question("q") == question_before
        launch.assert_not_called()
    elif action == "answer":
        assert (await db.get_expert_question("q"))["status"] == "answered"
        if orphaned:
            launch.assert_not_called()
        else:
            launch.assert_called_once()
    else:
        assert (await db.get_expert_question("q"))["status"] == "skipped"
        launch.assert_not_called()


async def test_owner_resume_denial_preserves_workspace_diagnostic(graph_client):
    client, db, app, launch = graph_client
    cid = await db.create_conversation(project_slug="demo")
    await db.update_conversation_task(cid, mode="write", state="ready",
                                      workspace_error="diagnóstico original")
    await db.disable_project("demo")
    await db.create_task_graph("disabled", "demo", tareas=[
        {"id": "pending", "titulo": "Pendiente"}], project_slug="demo", conversation_id=cid)
    before = await db.get_conversation_task(cid)
    response = await client.post("/graphs/disabled/resume")
    assert response.status == 403
    assert await db.get_conversation_task(cid) == before
    launch.assert_not_called()


@pytest.mark.parametrize("action", ["answer", "skip"])
@pytest.mark.parametrize("actor,expected", [
    (identity.OWNER, 200), ("dev@example.test", 200), ("other@example.test", 403)])
async def test_unlinked_question_requires_project_access(graph_client, action, actor, expected):
    client, db, app, launch = graph_client
    await db.create_expert_question("unlinked", "", '{"title":"¿Continuar?"}',
                                   project_slug="demo", kind="choice")
    before = await db.get_expert_question("unlinked")
    response = await client.post(f"/questions/unlinked/{action}", json={"text": "Sí"},
                                 headers={"X-Test-Actor": actor})
    assert response.status == expected
    if expected == 403:
        assert await db.get_expert_question("unlinked") == before
    else:
        assert (await db.get_expert_question("unlinked"))["status"] == (
            "answered" if action == "answer" else "skipped")
    launch.assert_not_called()
