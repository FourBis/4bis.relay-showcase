"""Carreras sintéticas de inspección al retomar, sin Git real ni proveedores."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from relay import (
    coordination, graph_resume_inspection, identity, orquestador,
    orchestrator_recovery, server_common, server_graph_helpers,
    server_graph_routes, task_workspace,
)
from relay.db import Database


@pytest.fixture
async def resume_env(tmp_path, monkeypatch):
    db = Database(path=tmp_path / "resume.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path)})
    actor = "dev@example.test"
    await db.set_user_role(actor, "member", project_slugs=["demo"])
    monkeypatch.setattr(identity, "_roles", {actor: "member"})
    monkeypatch.setattr(identity, "_disabled", set())
    monkeypatch.setattr(identity, "_project_grants", {actor: ["demo"]})
    monkeypatch.setattr(server_common, "_get_api_key", lambda: "")
    monkeypatch.setattr(task_workspace, "_validate_identity", AsyncMock())
    monkeypatch.setattr(task_workspace, "_ref", AsyncMock(
        side_effect=lambda _repo, ref: "a" * 40 if ref == "HEAD" else ""))
    monkeypatch.setattr(task_workspace, "_status_clean", AsyncMock(return_value=True))
    monkeypatch.setattr(task_workspace, "_git", AsyncMock(side_effect=AssertionError("No Git real")))
    repair = AsyncMock()
    monkeypatch.setattr(orquestador, "sanar", repair)
    monkeypatch.setattr(orchestrator_recovery, "prepare_graph_resume", AsyncMock())
    launch = Mock()
    monkeypatch.setattr(server_graph_routes, "_largar_grafo", launch)

    @web.middleware
    async def account(request, handler):
        request[identity.IDENTITY_KEY] = actor
        return await handler(request)

    app = web.Application(middlewares=[account, identity.require_role])
    app[server_common.DB_KEY] = db
    app[server_common.GRAFOS_KEY] = {}
    app[server_common.BG_TASKS_KEY] = set()
    app.router.add_post("/graphs/{id}/resume", server_graph_routes.graphs_resume)
    async with TestClient(TestServer(app)) as client:
        yield client, db, app, launch, repair, actor


async def owned_graph(db, actor, *, mode="write"):
    project = await db.get_project("demo")
    cid = await db.create_conversation(project_slug="demo", requested_by=actor)
    workspace = (Path(db.path).parent / "task-workspaces" / cid
                 if mode == "write" else Path(project["repo_path"]))
    workspace.mkdir(parents=True, exist_ok=True)
    await db.update_conversation_task(cid, mode=mode, read_only=mode == "read_only",
        state="ready", source_repo=project["repo_path"], workspace_path=str(workspace),
        branch=f"codex/task-{cid}" if mode == "write" else "",
        workspace_state="blocked", workspace_error="diagnóstico conservado")
    await db.create_task_graph("g", "Plan sintético", tareas=[
        {"id": "pending", "titulo": "Pendiente"}], project_slug="demo", conversation_id=cid)
    return cid


async def test_disabled_between_http_gate_and_resolver_never_inspects(resume_env, monkeypatch):
    client, db, app, launch, repair, actor = resume_env
    cid = await owned_graph(db, actor)
    before, graph_before = await db.get_conversation_task(cid), await db.get_task_graph("g")
    real = coordination.resolved_project

    async def disable_then_resolve(*args, **kwargs):
        await db.disable_project("demo")
        return await real(*args, **kwargs)

    late = AsyncMock(side_effect=disable_then_resolve)
    monkeypatch.setattr(coordination, "resolved_project", late)
    response = await client.post("/graphs/g/resume")
    assert response.status == 403
    late.assert_awaited_once()
    task_workspace._validate_identity.assert_not_awaited()
    assert await db.get_conversation_task(cid) == before
    assert await db.get_task_graph("g") == graph_before
    assert not app[server_common.GRAFOS_KEY]
    repair.assert_not_awaited()
    launch.assert_not_called()


@pytest.mark.parametrize("restriction", ["revoked", "disabled_project", "read_only_project", "disabled_user"])
async def test_revocation_during_inspection_does_not_write_or_repair(resume_env, monkeypatch, restriction):
    client, db, app, launch, repair, actor = resume_env
    cid = await owned_graph(db, actor)
    before, graph_before = await db.get_conversation_task(cid), await db.get_task_graph("g")
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(*_args):
        entered.set()
        await release.wait()

    monkeypatch.setattr(task_workspace, "_validate_identity", pause)
    unguarded = AsyncMock(wraps=task_workspace._persist)
    monkeypatch.setattr(task_workspace, "_persist", unguarded)
    request = asyncio.create_task(client.post("/graphs/g/resume"))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        # Keep the process cache unchanged: the writer must use current DB ACL.
        if restriction == "revoked":
            await db.set_user_role(actor, "member", project_slugs=[])
        elif restriction == "disabled_user":
            await db.set_user_role(actor, "member", enabled=False, project_slugs=["demo"])
        elif restriction == "disabled_project":
            await db.disable_project("demo")
        else:
            await db.upsert_project({"slug": "demo", "defaults_json": {"read_only": True}})
        release.set()
        response = await asyncio.wait_for(request, 10)
        assert response.status == 403
        assert "diagnóstico conservado" not in await response.text()
        assert await db.get_conversation_task(cid) == before
        assert await db.get_task_graph("g") == graph_before
        assert not app[server_common.GRAFOS_KEY]
        unguarded.assert_not_awaited()
        repair.assert_not_awaited()
        launch.assert_not_called()
    finally:
        release.set()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("change", ["task_write", "cancelled", "project_identity", "graph_origin"])
async def test_changed_observation_preserves_the_concurrent_writer(resume_env, monkeypatch, change):
    client, db, app, launch, repair, actor = resume_env
    cid = await owned_graph(db, actor)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(*_args):
        entered.set()
        await release.wait()

    monkeypatch.setattr(task_workspace, "_validate_identity", pause)
    unguarded = AsyncMock(wraps=task_workspace._persist)
    monkeypatch.setattr(task_workspace, "_persist", unguarded)
    request = asyncio.create_task(client.post("/graphs/g/resume"))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        if change == "task_write":
            await db.update_conversation_task(cid, workspace_error="diagnóstico nuevo",
                concurrent_note="conservar", validation={"status": "ok", "detail": "resultado actual"})
        elif change == "cancelled":
            await db.update_conversation_task(cid, state="cancelled", workspace_error="cancelación conservada")
        elif change == "project_identity":
            await db.upsert_project({"slug": "demo", "repo_path": str(Path(db.path).parent / "other")})
        else:
            other = await db.create_conversation(project_slug="demo", requested_by=actor)
            await db.run("UPDATE task_graphs SET conversation_id=? WHERE id='g'", (other,))
        expected, graph_expected = await db.get_conversation_task(cid), await db.get_task_graph("g")
        release.set()
        response = await asyncio.wait_for(request, 10)
        assert response.status == (403 if change == "graph_origin" else 409)
        if response.status == 409:
            assert (await response.json())["code"] == "workspace_inspection_stale"
        assert await db.get_conversation_task(cid) == expected
        assert await db.get_task_graph("g") == graph_expected
        assert not app[server_common.GRAFOS_KEY]
        unguarded.assert_not_awaited()
        repair.assert_not_awaited()
        launch.assert_not_called()
    finally:
        release.set()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("mode", ["write", "read_only"])
async def test_authorized_inspection_can_resume_and_read_only_creator_needs_no_write_grant(resume_env, mode):
    client, db, app, launch, repair, actor = resume_env
    cid = await owned_graph(db, actor, mode=mode)
    if mode == "read_only":
        await db.set_user_role(actor, "member", project_slugs=[])
        identity._project_grants[actor] = []
    response = await client.post("/graphs/g/resume")
    assert response.status == 202
    saved = await db.get_conversation_task(cid)
    assert saved["workspace_state"] == "ok"
    assert saved["workspace_error"] == ""
    repair.assert_awaited_once()
    launch.assert_called_once()
    assert launch.call_args.args[3] == actor


async def test_ordinary_inspection_keeps_its_existing_diagnostic_writer(resume_env, monkeypatch):
    _, db, _, launch, repair, actor = resume_env
    cid = await owned_graph(db, actor)
    monkeypatch.setattr(task_workspace, "_validate_identity", AsyncMock(
        side_effect=task_workspace.TaskWorkspaceError("diagnóstico ordinario")))
    assert graph_resume_inspection._scope.get() is None
    result = await task_workspace.inspect_workspace(db, await db.get_project("demo"), cid)
    assert result["workspace_error"] == "diagnóstico ordinario"
    assert result["state"] == "blocked"
    launch.assert_not_called()
    repair.assert_not_awaited()


async def test_graph_worker_scope_ends_before_the_orchestrator(resume_env, monkeypatch):
    _, db, app, _, _, actor = resume_env
    await owned_graph(db, actor)
    project, graph = await db.get_project("demo"), await db.get_task_graph("g")
    real = task_workspace.resolved_project
    observed = []

    async def inspect(*args, **kwargs):
        observed.append(graph_resume_inspection._scope.get() is not None)
        return await real(*args, **kwargs)

    async def run(*_args, **_kwargs):
        assert graph_resume_inspection._scope.get() is None

    monkeypatch.setattr(task_workspace, "resolved_project", inspect)
    monkeypatch.setattr(server_graph_helpers, "_correr_grafo_bg", run)
    with graph_resume_inspection.resume_inspection_scope(db, graph, actor):
        server_graph_helpers._largar_grafo(app, project, "g", actor)
        worker = app[server_common.GRAFOS_KEY]["g"]
    await asyncio.wait_for(worker, 10)
    assert observed == [True]
    assert graph_resume_inspection._scope.get() is None
