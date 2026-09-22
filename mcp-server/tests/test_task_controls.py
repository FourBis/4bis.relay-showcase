from __future__ import annotations

import asyncio

import pytest

from relay import task_service
from tests.test_task_continuity import create_task, make_client


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
