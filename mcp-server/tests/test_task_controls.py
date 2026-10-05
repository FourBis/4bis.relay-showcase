from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.test_utils import make_mocked_request

from relay import github_credentials, planificador, server_graph_routes, server_task_routes, task_service
from relay.app_state import GRAFOS_KEY
from tests.test_task_continuity import create_task, make_client


async def _fake_github_account(provider):
    assert provider == "github"
    return {"access_token": "test-token", "subject": "123", "login": "test-user"}


@pytest.mark.parametrize("has_graph", [False, True])
@pytest.mark.parametrize("disconnect", [False, True])
async def test_cancel_waits_for_cleanup_and_survives_request_cancellation(
        tmp_path, monkeypatch, has_graph, disconnect):
    client, db, _source, app = await make_client(tmp_path, monkeypatch)
    release, cleaning = asyncio.Event(), asyncio.Event()
    calls, workers = [], []
    try:
        cid = await create_task(client, publish_allowed=False)
        await db.enqueue_conversation_event(cid, "waiting", "run", {"user": "no ejecutar"})

        async def graph_worker():
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        if has_graph:
            await db.create_task_graph("cancel", "Cancelar", tareas=[
                {"id": "node", "titulo": "Nodo"}], project_slug="demo", conversation_id=cid)
            graph = asyncio.create_task(graph_worker())
            workers.append(graph)
            app[GRAFOS_KEY]["cancel"] = graph

        async def runner():
            try:
                if has_graph:
                    await graph
                else:
                    await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        worker = asyncio.create_task(runner())
        workers.append(worker)
        app[task_service.TASK_RUNNERS_KEY] = {cid: worker}
        await asyncio.sleep(0)
        request = make_mocked_request("POST", f"/conversations/{cid}/task", app=app,
                                      match_info={"id": cid})
        monkeypatch.setattr(request, "json", AsyncMock(return_value={"action": "cancel"}))
        first = asyncio.create_task(server_task_routes.task_action(request))
        calls.append(first)
        await asyncio.wait_for(cleaning.wait(), 2)
        status = await client.get(f"/conversations/{cid}/task")
        assert (await status.json())["state"] == "cancelling"
        assert not first.done(), "no confirmar cancelación mientras se limpian recursos"
        if disconnect:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            denied = await client.post(f"/conversations/{cid}/task", json={"action": "continue"})
            assert denied.status == 422
            assert (await db.get_conversation_task(cid))["state"] == "cancelled"
        repeat = asyncio.create_task(client.post(f"/conversations/{cid}/task", json={"action": "cancel"}))
        calls.append(repeat)
        status = await client.get(f"/conversations/{cid}/task")
        assert (await status.json())["state"] == "cancelling"
        assert not repeat.done()
        assert all(not task.done() for task in workers)
        assert all(task.cancelling() == 1 for task in workers)
        if has_graph:
            assert (await db.get_task_graph("cancel"))["estado"] == "activo"
        release.set()
        response = await asyncio.wait_for(repeat, 2)
        assert response.status == 200
        assert (await response.json())["state"] == "cancelled"
        assert all(task.done() for task in workers)
        if not disconnect:
            assert (await first).status == 200
        if has_graph:
            assert (await db.get_task_graph("cancel"))["estado"] == "cancelado"
        assert (await db.list_conversation_events(cid))[0]["state"] == "cancelled"
        status = await client.get(f"/conversations/{cid}/task")
        assert (await status.json())["state"] == "cancelled"
    finally:
        release.set()
        await asyncio.gather(*calls, *workers, return_exceptions=True)
        await client.close()


async def test_cancel_queue_failure_still_waits_for_worker_and_can_retry(tmp_path, monkeypatch):
    client, db, _source, app = await make_client(tmp_path, monkeypatch)
    cleaning, release = asyncio.Event(), asyncio.Event()
    worker = request = None
    try:
        cid = await create_task(client, publish_allowed=False)
        await db.enqueue_conversation_event(cid, "waiting", "run", {"user": "no ejecutar"})
        cancel_pending = db.cancel_pending_conversation_events
        monkeypatch.setattr(db, "cancel_pending_conversation_events",
                            AsyncMock(side_effect=OSError("queue unavailable")))

        async def work():
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        worker = asyncio.create_task(work())
        app[task_service.TASK_RUNNERS_KEY] = {cid: worker}
        request = asyncio.create_task(client.post(f"/conversations/{cid}/task", json={"action": "cancel"}))
        await asyncio.wait_for(cleaning.wait(), 2)
        assert not request.done()
        status = await client.get(f"/conversations/{cid}/task")
        assert (await status.json())["state"] == "cancelling"
        release.set()
        assert (await asyncio.wait_for(request, 2)).status == 422
        assert worker.done()
        status = await client.get(f"/conversations/{cid}/task")
        failed = await status.json()
        assert failed["state"] == "cancelled"
        assert failed["cancellation_error"] and failed["error"] == failed["cancellation_error"]
        assert (await db.list_conversation_events(cid))[0]["state"] == "pending"
        monkeypatch.setattr(db, "cancel_pending_conversation_events", cancel_pending)
        retried = await client.post(f"/conversations/{cid}/task", json={"action": "cancel"})
        assert retried.status == 200
        assert not (await retried.json())["cancellation_error"]
        assert (await db.list_conversation_events(cid))[0]["state"] == "cancelled"
    finally:
        release.set()
        if worker is not None and not worker.done():
            worker.cancel()
        await asyncio.gather(*(task for task in (request, worker) if task is not None), return_exceptions=True)
        await client.close()


async def test_cancel_stops_planner_and_late_graph_cannot_become_active(tmp_path, monkeypatch):
    client, db, _source, app = await make_client(tmp_path, monkeypatch)
    planning, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    launch = Mock()
    monkeypatch.setattr(server_graph_routes, "_largar_grafo", launch)
    try:
        cid = await create_task(client, publish_allowed=False)

        async def plan(*args, **kwargs):
            try:
                planning.set()
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                await db.create_task_graph("late", "Resultado tardío", tareas=[
                    {"id": "late-node", "titulo": "No ejecutar"}], project_slug="demo", conversation_id=cid)

        monkeypatch.setattr(planificador, "armar_grafo", plan)
        request = make_mocked_request("POST", "/graphs", app=app)
        monkeypatch.setattr(request, "json", AsyncMock(return_value={
            "project": "demo", "conversation_id": cid, "objetivo": "Preparar plan"}))
        create = asyncio.create_task(server_graph_routes.graphs_create(request))
        calls.append(create)
        await asyncio.wait_for(planning.wait(), 2)
        cancel = asyncio.create_task(client.post(f"/conversations/{cid}/task", json={"action": "cancel"}))
        calls.append(cancel)
        await asyncio.wait_for(cleaning.wait(), 2)
        assert not cancel.done()
        status = await client.get(f"/conversations/{cid}/task")
        assert (await status.json())["state"] == "cancelling"
        release.set()
        assert (await asyncio.wait_for(cancel, 2)).status == 200
        assert (await asyncio.wait_for(create, 2)).status == 409
        assert (await db.get_task_graph("late"))["estado"] == "cancelado"
        # Una escritura SQLite ya iniciada puede terminar después de la petición.
        later = await db.create_task_graph("later", "Persistencia tardía", tareas=[
            {"id": "later-node", "titulo": "No ejecutar"}], project_slug="demo", conversation_id=cid)
        assert later["estado"] == "cancelado"
        assert await db.active_task_graph(cid) is None
        launch.assert_not_called()
        assert cid not in server_graph_routes._PLANIFICANDO
    finally:
        release.set()
        for call in calls:
            if not call.done():
                call.cancel()
        await asyncio.gather(*calls, return_exceptions=True)
        await client.close()


@pytest.fixture(autouse=True)
def explicit_github_oauth_fixture(monkeypatch):
    monkeypatch.setattr(
        github_credentials.user_accounts, "require_account", _fake_github_account)


@pytest.mark.asyncio
async def test_controls_validate_unknown_ack_and_request_before_mutation(tmp_path, monkeypatch):
    client, db, _source, _app = await make_client(tmp_path, monkeypatch)
    try:
        missing = await client.get("/conversations/missing/task")
        assert missing.status == 404
        assert (await client.post("/conversations/missing/task", json={"action": "pause"})).status == 404
        cid = await create_task(client, publish_allowed=True)
        await db.update_conversation_task(cid, mode="write", state="implemented")
        before = await db.get_conversation_task(cid)
        bad_ack = await client.post(f"/conversations/{cid}/task", json={
            "action": "continue", "acknowledge_uncertain": "yes"})
        assert bad_ack.status == 422
        bad_publish = await client.post(f"/conversations/{cid}/task", json={
            "action": "publish", "request_id": ""})
        assert bad_publish.status == 422
        assert await db.get_conversation_task(cid) == before
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_publish_while_writer_active_only_enqueues(tmp_path, monkeypatch):
    client, db, _source, app = await make_client(tmp_path, monkeypatch)
    try:
        cid = await create_task(client, publish_allowed=True)
        await db.update_conversation_task(cid, mode="write", state="implemented")
        blocker = asyncio.create_task(asyncio.sleep(60))
        app[task_service.TASK_RUNNERS_KEY] = {cid: blocker}
        response = await client.post(f"/conversations/{cid}/task", json={
            "action": "publish", "request_id": "pub-1"})
        assert response.status == 200
        state = await db.get_conversation_task(cid)
        assert state["state"] == "implemented"
        event = (await db.list_conversation_events(cid))[0]
        assert event["kind"] == "publish" and event["state"] == "pending"
        blocker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocker
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_publish_retry_preserves_completed_receipt_and_review_state(tmp_path, monkeypatch):
    client, db, _source, _app = await make_client(tmp_path, monkeypatch)
    try:
        cid = await create_task(client, publish_allowed=True)
        event = await db.enqueue_conversation_event(cid, "publish:done", "publish", {"role": "owner"})
        await db.finish_conversation_event(event["id"])
        await db.update_conversation_task(cid, state="review")
        response = await client.post(f"/conversations/{cid}/task", json={
            "action": "publish", "request_id": "done"})
        assert response.status == 200
        assert (await response.json())["state"] == "review"
        assert len(await db.list_conversation_events(cid)) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_track_receipt_does_not_reset_budget_or_accept_conflict(tmp_path, monkeypatch):
    client, db, _source, _app = await make_client(tmp_path, monkeypatch)
    try:
        cid = await create_task(client, publish_allowed=True)
        await db.set_conversation_pr(cid, "https://github.test/pr/1")
        await db.update_conversation_task(cid, mode="write", state="review",
                                          publish_allowed=True,
                                          tracking={"enabled": False, "iterations": 2, "tokens": 40})
        payload = {"action": "track", "enabled": True, "request_id": "track-1",
                   "interval_s": 30, "max_iterations": 3, "max_tokens": 1000,
                   "duration_minutes": 10}
        assert (await client.post(f"/conversations/{cid}/task", json=payload)).status == 200
        await db.update_conversation_task(cid, tracking={"iterations": 2, "tokens": 40})
        assert (await client.post(f"/conversations/{cid}/task", json=payload)).status == 200
        state = await db.get_conversation_task(cid)
        assert state["tracking"]["iterations"] == 2
        conflict = {**payload, "max_tokens": 2000}
        assert (await client.post(f"/conversations/{cid}/task", json=conflict)).status == 422
        await db.update_conversation_task(cid, state="paused")
        paused = await client.post(f"/conversations/{cid}/task", json=payload | {"request_id": "track-2"})
        assert paused.status == 422
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_uncertain_publish_requires_ack_and_paused_feedback_stays_pending(tmp_path, monkeypatch):
    client, db, _source, _app = await make_client(tmp_path, monkeypatch)
    try:
        cid = await create_task(client, publish_allowed=True)
        await db.update_conversation_task(cid, mode="write", state="implemented",
                                          publish_allowed=True, publish_uncertain=False)
        event = await db.enqueue_conversation_event(cid, "publish:old", "publish", {"role": "owner"})
        await db.claim_conversation_event(cid)
        await db.finish_conversation_event(event["id"], state="uncertain", error="before create")
        no_ack = await client.post(f"/conversations/{cid}/task", json={
            "action": "continue", "acknowledge_uncertain": False})
        assert no_ack.status == 422
        ack = await client.post(f"/conversations/{cid}/task", json={
            "action": "continue", "acknowledge_uncertain": True})
        assert ack.status == 200
        assert (await db.list_conversation_events(cid))[0]["state"] == "applied"

        await db.update_conversation_task(cid, state="paused", tracking={"enabled": False})
        await db.enqueue_conversation_event(cid, "feedback:1", "feedback",
                                            {"user": "review", "role": "owner"})
        blocked = await client.post(f"/conversations/{cid}/task", json={
            "action": "continue", "acknowledge_uncertain": True})
        assert blocked.status == 200
        assert "reactiva" in (await blocked.json())["error"]
        assert (await db.get_conversation_task(cid))["state"] == "ready"
        assert (await db.list_conversation_events(cid, states=["pending"]))[0]["kind"] == "feedback"
    finally:
        await client.close()
