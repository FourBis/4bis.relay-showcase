"""Cancelación de grafos históricos, repetición y permisos sin proveedores."""
import asyncio
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from relay import identity, server_common, server_graph_routes
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
    app.router.add_post("/graphs/{id}/cancel", server_graph_routes.graphs_cancel)
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


async def test_interrupted_cancel_request_preserves_worker_cleanup(graph_client):
    client, db, app, launch = graph_client
    await db.create_task_graph("interrupted", "demo", tareas=[
        {"id": "saved", "titulo": "Hecho"}], project_slug="demo")
    await db.update_task("saved", estado="hecho", resultado="conservar")
    before = (await db.get_task_graph("interrupted"))["tasks"]
    started, cleaning, release, interrupted = (asyncio.Event() for _ in range(4))

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
    try:
        response = await asyncio.wait_for(handler, 2)
        assert response.status == 200
        assert not interrupted.is_set(), "la interrupción de la petición cortó la limpieza"
        assert not task.done()
    finally:
        release.set()
        await asyncio.gather(handler, task, return_exceptions=True)
    assert (await db.get_task_graph("interrupted"))["tasks"] == before
    assert not app[server_common.GRAFOS_KEY]
    launch.assert_not_called()
