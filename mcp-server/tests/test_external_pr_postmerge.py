"""Post-merge review regressions: real SQLite and fake GitHub only."""
import asyncio
import copy
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from relay import admin_projects, external_pr, identity, task_service
from relay.app_state import DB_KEY
from tests.test_external_pr import SHA_A, SHA_B, WHO, declare, register, registration
from tests.test_external_pr_review_regressions import ready_task


@pytest.mark.parametrize("terminal", ["CLOSED", "MERGED"])
async def test_terminal_registration_is_archived_and_can_reopen(registration, terminal):
    db, project, _, remote = registration
    remote["state"] = terminal
    task = await register(registration)
    cid = task["id"]
    closed = await db.get_conversation(cid)
    assert closed["status"] == "closed" and closed["closed_at"]
    await external_pr.refresh(db, project, cid, WHO)
    assert (await db.get_conversation(cid))["closed_at"] == closed["closed_at"]
    remote["state"] = "OPEN"
    await external_pr.refresh(db, project, cid, WHO)
    opened = await db.get_conversation(cid)
    assert opened["status"] == "open" and opened["closed_at"] is None
    remote["state"] = terminal
    await register(registration)
    assert (await db.get_conversation(cid))["status"] == "closed"


@pytest.mark.parametrize("terminal", ["CLOSED", "MERGED"])
async def test_refresh_archives_terminal_pr_without_losing_receipts(registration, terminal):
    db, project, _, remote = registration
    task = await ready_task(registration)
    remote["state"] = terminal
    result = await external_pr.refresh(db, project, task["id"], WHO)
    closed = await db.get_conversation(task["id"])
    assert closed["status"] == "closed" and closed["closed_at"]
    assert result["evidence"] == task["evidence"]
    assert result["history"][:-1] == task["history"]
    again = await external_pr.refresh(db, project, task["id"], WHO)
    assert (await db.get_conversation(task["id"]))["closed_at"] == closed["closed_at"]
    assert again["history"] == result["history"]


@pytest.mark.parametrize("defaults", [{}, {"external_pr_policy": {"low": {
    "require_declaration": True, "required_checks": ["Stricter check"]}}}])
async def test_evidence_retry_reassesses_current_policy_without_rewriting_receipt(registration, monkeypatch, defaults):
    db, project, _, _ = registration
    task = await ready_task(registration)
    await db.upsert_project({"slug": "demo", "defaults_json": defaults})
    remote = AsyncMock(side_effect=AssertionError("Idempotent retry does not re-read the PR"))
    monkeypatch.setattr(external_pr, "read_snapshot", remote)
    result = await external_pr.add_evidence(db, project, task["id"],
        {"request_id": "evidence-1", "head_sha": SHA_A, "summary": "pytest: 12 passed"}, WHO,
        authorize=lambda: db.get_project("demo"))
    assert result["assessment"]["state"] == "missing_evidence"
    assert result["evidence"] == task["evidence"] and result["history"] == task["history"]
    remote.assert_not_awaited()


async def test_new_evidence_uses_policy_revalidated_after_refresh(registration):
    db, project, _, _ = registration
    task = await ready_task(registration)
    calls = 0

    async def authorize():
        nonlocal calls
        calls += 1
        if calls == 2:
            await db.upsert_project({"slug": "demo", "defaults_json": {}})
        return await db.get_project("demo")

    result = await external_pr.add_evidence(db, project, task["id"],
        {"request_id": "new-policy", "head_sha": SHA_A, "summary": "New receipt"}, WHO, authorize=authorize)
    assert calls == 2 and result["assessment"]["state"] == "missing_evidence"
    assert len(result["evidence"]) == 2


@pytest.mark.parametrize("same_key,move_head", [(False, False), (True, False), (False, True)])
async def test_concurrent_real_timestamp_refreshes_preserve_evidence_or_reject_moved_head(
        registration, monkeypatch, same_key, move_head):
    from relay.db import Database
    db, project, _, remote = registration
    task = await register(registration)
    both_read = asyncio.Event()
    reads = 0
    stamp = datetime.now(timezone.utc)

    async def fresh_snapshot(*args, **kwargs):
        nonlocal reads
        reads += 1
        sequence = reads
        if sequence <= 2:
            if sequence == 2:
                both_read.set()
            await asyncio.wait_for(both_read.wait(), 3)
        result = copy.deepcopy(remote)
        result["observed_at"] = (stamp + timedelta(microseconds=sequence)).isoformat()
        if move_head and sequence > 2:
            result["head_sha"] = SHA_B
        return result

    monkeypatch.setattr(external_pr, "read_snapshot", fresh_snapshot)
    other = Database(path=db.path)
    results = await asyncio.gather(*[
        external_pr.add_evidence(store, project, task["id"],
            {"request_id": key, "head_sha": SHA_A, "summary": "Concurrent evidence"}, WHO)
        for store, key in [(db, "one"), (other, "one" if same_key else "two")]
    ], return_exceptions=True)
    errors = [result for result in results if isinstance(result, BaseException)]
    if move_head:
        # The new head may arrive before either append or between the two.
        assert 1 <= len(errors) <= 2
        assert all(isinstance(error, ValueError) and "commit cambi" in str(error) for error in errors)
    else:
        assert errors == []
    saved = await db.get_conversation_task(task["id"])
    expected = 2 - len(errors) if move_head else 1 if same_key else 2
    assert len(saved["evidence"]) == expected
    assert sum(item["kind"] == "declared_evidence" for item in saved["history"]) == expected


@pytest.mark.parametrize("role", ["owner", "member", "finance", "disabled"])
async def test_external_registration_catalog_only_returns_eligible_projects(monkeypatch, role):
    projects = [{"slug": slug, "name": slug, "enabled": slug != "disabled", "repo_path": slug,
                 "defaults_json": {"read_only": slug == "readonly"}}
                for slug in ("allowed", "unassigned", "readonly", "disabled", "notes", "nonhub")]
    db = Mock(list_projects=AsyncMock(return_value=projects), client_name_by_id=AsyncMock(return_value={}))
    monkeypatch.setattr(identity, "role_of", lambda request: role)
    monkeypatch.setattr(identity, "project_slugs", lambda request: ["allowed", "readonly", "disabled", "notes", "nonhub"])
    monkeypatch.setattr(admin_projects, "_cbm_list_projects", AsyncMock(return_value={"projects": []}))
    monkeypatch.setattr(admin_projects.relay_config, "discord_guild_id", lambda: "")
    monkeypatch.setattr(admin_projects, "_has_git", lambda path: str(path) != "notes")
    monkeypatch.setattr(admin_projects, "_git_remote_url", lambda path:
        "https://gitlab.com/team/demo.git" if path == "nonhub" else "git@github.com:team/demo.git")
    request = make_mocked_request("GET", "/admin/api/projects?capability=external_pr", app={DB_KEY: db})
    result = json.loads((await admin_projects.api_projects(request)).text)
    allowed = ["allowed", "unassigned"] if role == "owner" else ["allowed"] if role == "member" else []
    assert result == {"projects": [{"slug": slug, "name": slug} for slug in allowed]}


@pytest.mark.parametrize("legacy_case", [False, True])
async def test_monitor_skips_external_histories_but_preserves_due_closed_issue_sync(registration, monkeypatch, legacy_case):
    db, _, _, _ = registration
    await db.upsert_project({"slug": "sync", "name": "Sync", "repo_path": "fake",
                             "defaults_json": {"issue_sync_policy": {"enabled": True}}})
    history = [{"kind": "old_snapshot", "details": "x" * 1000}] * 30
    for cid, project, mode, issue, due in [
        ("disabled-external", "demo", "external_pr", {"repo_id": 42}, 0),
        ("future-external", "sync", "external_pr", {"repo_id": 42}, time.time() + 3600),
        ("unlinked-external", "sync", "external_pr", None, 0),
        ("due-closed-external", "sync", "external_pr", {"repo_id": 42}, 0),
        ("writer", "demo", "write", None, 0),
    ]:
        if legacy_case and cid == "due-closed-external":
            project = project.upper()
        await db.create_conversation(project_slug=project, conversation_id=cid, requested_by=WHO)
        await db.update_conversation_task(cid, mode=mode, issue_link=issue, issue_sync_next_poll_at=due, history=history)
    await db.close_conversation("due-closed-external")
    full_read = AsyncMock(wraps=db.get_conversation_task)
    monkeypatch.setattr(db, "get_conversation_task", full_read)
    sync = Mock()
    feedback, pending = AsyncMock(), Mock()
    monkeypatch.setattr(task_service, "start_issue_sync", sync)
    monkeypatch.setattr(task_service, "poll_feedback", feedback)
    monkeypatch.setattr(task_service, "start_pending", pending)

    async def stop_after_iteration(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(task_service.asyncio, "sleep", stop_after_iteration)
    with pytest.raises(asyncio.CancelledError):
        await task_service.loop({DB_KEY: db})
    assert {call.args[2] for call in sync.call_args_list} == {"writer", "due-closed-external"}
    full_read.assert_not_awaited()
    assert [call.args[2] for call in feedback.await_args_list] == ["writer"]
    assert [call.args[1] for call in pending.call_args_list] == ["writer"]
    # Recovery still receives full external records; only the monitor is filtered.
    recovered = await db.list_managed_conversations()
    assert len(recovered) == 5 and all(len(row["task_json"]["history"]) == 30 for row in recovered)


@pytest.mark.parametrize("raw_defaults, eligible", [
    ("not-json", False),
    ("null", False),
    ("[]", False),
    ('"text"', False),
    ("123", False),
    ("true", False),
    ('{"issue_sync_policy":{"enabled":true}}', True),
    ('{"issue_sync_policy":{"enabled":false}}', False),
    ('{"issue_sync_policy":{"enabled":true},"read_only":true}', False),
])
async def test_monitor_isolates_invalid_project_defaults(registration, monkeypatch, raw_defaults, eligible):
    db, _, _, _ = registration
    for slug in ("healthy-sync", "candidate"):
        await db.upsert_project({"slug": slug, "name": slug, "repo_path": "fake",
                                 "defaults_json": {"issue_sync_policy": {"enabled": True}}})
    # Legacy/API string values can be stored verbatim, including malformed JSON.
    await db.run("UPDATE projects SET defaults_json=? WHERE slug=?", (raw_defaults, "candidate"))
    for cid, project, mode in [
        ("writer", "demo", "write"),
        ("healthy-external", "healthy-sync", "external_pr"),
        ("candidate-external", "candidate", "external_pr"),
    ]:
        await db.create_conversation(project_slug=project, conversation_id=cid, requested_by=WHO)
        await db.update_conversation_task(cid, mode=mode, issue_link={"repo_id": 42}, issue_sync_next_poll_at=0)
    sync = Mock()
    feedback, pending = AsyncMock(), Mock()
    monkeypatch.setattr(task_service, "start_issue_sync", sync)
    monkeypatch.setattr(task_service, "poll_feedback", feedback)
    monkeypatch.setattr(task_service, "start_pending", pending)

    async def stop_after_iteration(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(task_service.asyncio, "sleep", stop_after_iteration)
    with pytest.raises(asyncio.CancelledError):
        await task_service.loop({DB_KEY: db})
    expected = {"writer", "healthy-external"}
    if eligible:
        expected.add("candidate-external")
    assert {call.args[2] for call in sync.call_args_list} == expected
    assert [call.args[2] for call in feedback.await_args_list] == ["writer"]
    assert [call.args[1] for call in pending.call_args_list] == ["writer"]
