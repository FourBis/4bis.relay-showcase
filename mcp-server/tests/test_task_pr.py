from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from relay import task_pr, task_service, task_workspace
from tests.test_task_continuity import create_task, git, make_client


def remote_head(repo: Path, branch: str) -> str:
    out = git(repo, "ls-remote", "--heads", "origin", branch)
    return out.split()[0] if out else ""


def open_pr(branch: str = "", sha: str = "") -> dict:
    return {
        "number": 17, "url": "https://github.test/pr/17", "state": "OPEN",
        "headRefName": branch, "baseRefName": "develop", "headRefOid": sha,
        "mergedAt": None,
    }


async def prepared_task(tmp_path, monkeypatch):
    client, db, source, app = await make_client(tmp_path, monkeypatch)
    cid = await create_task(client)
    task = await db.get_conversation_task(cid)
    project = await db.get_project("demo")
    work = Path(task["workspace_path"])
    return client, db, source, app, cid, task, project, work


@pytest.mark.asyncio
async def test_failed_verification_never_pushes_or_creates_pr(tmp_path, monkeypatch):
    client, db, _source, _app, cid, task, project, work = \
        await prepared_task(tmp_path, monkeypatch)
    calls = {"create": 0}

    async def no_pr(*_args):
        return None

    async def failed_verify(*_args):
        return False, "tests failed"

    async def gh_create(*_args):
        calls["create"] += 1
        return 1, "must not run"

    monkeypatch.setattr(task_pr, "find_pr", no_pr)
    monkeypatch.setattr(task_pr.night, "verify_repo", failed_verify)
    monkeypatch.setattr(task_pr.git_process, "_exec", gh_create)
    try:
        (work / "same.txt").write_text("fails validation\n", encoding="utf-8")

        with pytest.raises(task_pr.TaskPRError, match="Validación fallida"):
            await task_pr.publish(db, project, cid)

        state = await db.get_conversation_task(cid)
        assert state["validation"]["status"] == "failed"
        assert remote_head(work, task["branch"]) == ""
        assert calls["create"] == 0
        assert not state.get("publish_uncertain")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_closed_pr_finishes_and_cancels_pending_without_uncertain(
    tmp_path, monkeypatch,
):
    client, db, _source, _app, cid, task, project, _work = \
        await prepared_task(tmp_path, monkeypatch)
    existing = {**open_pr(task["branch"], task["head_sha"]),
                "state": "MERGED", "mergedAt": "2026-09-22T12:00:00Z"}
    monkeypatch.setattr(task_pr, "find_pr", lambda *_: _async(existing))
    try:
        event = await db.enqueue_conversation_event(
            cid, "feedback:pending", "feedback", {"user": "old feedback"})

        result = await task_pr.publish(db, project, cid)

        rows = await db.list_conversation_events(cid)
        assert result["state"] == "finished"
        assert result["tracking"]["enabled"] is False
        assert {row["id"]: row["state"] for row in rows}[event["id"]] == "cancelled"
        assert all(row["state"] != "uncertain" for row in rows)
    finally:
        await client.close()


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_pause_during_verification_is_preserved_and_not_pushed(
    tmp_path, monkeypatch,
):
    client, db, _source, _app, cid, task, project, work = \
        await prepared_task(tmp_path, monkeypatch)
    calls = {"create": 0}

    async def pause_then_pass(*_args):
        await db.update_conversation_task(cid, state="paused")
        return True, "tests passed"

    async def gh_create(*_args):
        calls["create"] += 1
        return 0, "created"

    monkeypatch.setattr(task_pr, "find_pr", lambda *_: _async(None))
    monkeypatch.setattr(task_pr.night, "verify_repo", pause_then_pass)
    monkeypatch.setattr(task_pr.git_process, "_exec", gh_create)
    try:
        (work / "same.txt").write_text("pause me\n", encoding="utf-8")

        result = await task_pr.publish(db, project, cid)

        assert result["state"] == "paused"
        assert (await db.get_conversation_task(cid))["state"] == "paused"
        assert remote_head(work, task["branch"]) == ""
        assert calls["create"] == 0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_uncertain_create_recovers_same_pr_without_second_create(
    tmp_path, monkeypatch,
):
    client, db, _source, _app, cid, task, project, work = \
        await prepared_task(tmp_path, monkeypatch)
    calls = {"create": 0}
    pr = open_pr()

    async def find(*_args):
        return pr if pr["headRefName"] else None

    async def create_with_timeout(repo, *args):
        assert args[:2] == ("gh", "pr") or args[:2] == ("pr", "create")
        calls["create"] += 1
        pr.update(headRefName=task["branch"],
                  headRefOid=git(Path(repo), "rev-parse", "HEAD"))
        return 1, "timeout after server accepted create"

    monkeypatch.setattr(task_pr, "find_pr", find)
    monkeypatch.setattr(task_pr.night, "verify_repo",
                        lambda *_: _async((True, "tests passed")))
    monkeypatch.setattr(task_pr.git_process, "_exec", create_with_timeout)
    try:
        (work / "same.txt").write_text("publish once\n", encoding="utf-8")

        first = await task_pr.publish(db, project, cid)
        second = await task_pr.publish(db, project, cid)

        assert first["state"] == second["state"] == "review"
        assert calls["create"] == 1
        assert first["pr_url"] == second["pr_url"] == pr["url"]
        assert first["publish_uncertain"] is False
        assert remote_head(work, task["branch"]) == pr["headRefOid"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_external_remote_commit_blocks_and_preserves_both_sides(
    tmp_path, monkeypatch,
):
    client, db, source, _app, cid, task, project, work = \
        await prepared_task(tmp_path, monkeypatch)
    external = tmp_path / "external"
    subprocess.run(["git", "clone", "--branch", "develop",
                    git(source, "remote", "get-url", "origin"), str(external)],
                   check=True, capture_output=True)
    git(external, "config", "user.email", "external@example.test")
    git(external, "config", "user.name", "External")
    git(external, "checkout", "-b", task["branch"])
    (external / "remote.txt").write_text("remote commit\n", encoding="utf-8")
    git(external, "add", "remote.txt")
    git(external, "commit", "-m", "external")
    git(external, "push", "-u", "origin", task["branch"])
    external_sha = git(external, "rev-parse", "HEAD")
    monkeypatch.setattr(task_pr, "find_pr", lambda *_: _async(None))
    try:
        (work / "same.txt").write_text("local pending\n", encoding="utf-8")

        with pytest.raises(task_pr.TaskPRError, match="Workspace bloqueado"):
            await task_pr.publish(db, project, cid)

        state = await db.get_conversation_task(cid)
        assert state["state"] == "blocked"
        assert "detrás del remoto" in state["workspace_error"]
        assert (work / "same.txt").read_text(encoding="utf-8") == "local pending\n"
        assert "same.txt" in git(work, "status", "--short")
        assert remote_head(work, task["branch"]) == external_sha
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_old_sha_feedback_and_exhausted_tracking_do_not_run_model(
    tmp_path, monkeypatch,
):
    client, db, _source, app, cid, task, project, _work = \
        await prepared_task(tmp_path, monkeypatch)
    calls = {"model": 0}

    async def model(**_kwargs):
        calls["model"] += 1

    monkeypatch.setattr("relay.server_expert_jobs._run_expert_bg", model)
    try:
        await db.update_conversation_task(cid, head_sha="new-head", tracking={
            "enabled": True, "iterations": 0, "max_iterations": 1,
            "tokens": 0, "max_tokens": 100, "expires_at": 9999999999,
        })
        old = await db.enqueue_conversation_event(
            cid, "pr:17:ci:old-head:1", "feedback",
            {"user": "old CI", "head_sha": "old-head"})
        claimed = await db.claim_conversation_event(cid)
        resolved = await task_workspace.resolved_project(db, project, cid)
        await task_service._run_event(app, resolved, cid, claimed)
        await db.finish_conversation_event(old["id"])
        old_chat = await db.get_chat(old["chat_id"])

        await db.update_conversation_task(cid, state="ready", tracking={
            "enabled": True, "iterations": 1, "max_iterations": 1,
            "tokens": 0, "max_tokens": 100, "expires_at": 9999999999,
        })
        exhausted = await db.enqueue_conversation_event(
            cid, "pr:17:comment:2", "feedback", {"user": "new feedback"})
        await task_service._drain(app, cid)

        rows = await db.list_conversation_events(cid)
        states = {row["id"]: row["state"] for row in rows}
        assert old_chat["status"] == "cancelled"
        assert states[exhausted["id"]] == "pending"
        assert calls["model"] == 0
    finally:
        await client.close()
