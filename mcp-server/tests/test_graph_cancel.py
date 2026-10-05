"""Cancelación de grafos históricos, repetición y permisos sin proveedores."""
import asyncio
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from relay import identity, orquestador, server_common, server_graph_helpers, server_graph_routes, server_lifecycle
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
        conv_id = await db.create_conversation(project_slug="demo")
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
    # Las consultas read_only conservan el permiso de cancelación existente.
    expected = 200 if mode == "read_only" else expected
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
    reads = 0

    async def delayed_read(graph_id):
        nonlocal reads
        graph = await get_task_graph(graph_id)
        reads += 1
        # guard_workspace lee primero; el handler toma el segundo snapshot.
        if graph_id == "overlap" and reads == 2:
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
