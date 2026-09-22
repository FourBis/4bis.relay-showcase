from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from relay import identity, task_pr, task_service, task_workspace
from relay.db import Database
from relay.app_state import (
    BG_TASKS_KEY, DB_KEY, GRAFOS_KEY, MCP_POOL_KEY, NOTIFY_KEY,
    PROGRESS_KEY, RUNNING_KEY, SESSIONS_KEY, SKILLS_KEY,
)
from relay.server_conversation_routes import conversations_create
from relay.server_expert_routes import experts_run, experts_status
from relay.server_task_routes import task_action, task_get


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    remote, seed, source = (tmp_path / name for name in ("origin.git", "seed", "source"))
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "develop", str(seed)], check=True, capture_output=True)
    git(seed, "config", "user.email", "relay@example.test")
    git(seed, "config", "user.name", "Relay Test")
    (seed / "same.txt").write_text("base\n", encoding="utf-8")
    git(seed, "add", "same.txt")
    git(seed, "commit", "-m", "base")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "develop")
    subprocess.run(["git", "clone", "--branch", "develop", str(remote), str(source)],
                   check=True, capture_output=True)
    git(source, "config", "user.email", "relay@example.test")
    git(source, "config", "user.name", "Relay Test")
    return source


class FakeSkills:
    async def get_block(self):
        return ""


class FakeNotify:
    async def send(self, *args, **kwargs):
        return True


async def make_client(tmp_path: Path, monkeypatch):
    source = make_repo(tmp_path)
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(source),
                             "defaults_json": {}})
    app = web.Application()
    app[DB_KEY] = db
    app[RUNNING_KEY] = {}
    app[PROGRESS_KEY] = {}
    app[BG_TASKS_KEY] = set()
    app[GRAFOS_KEY] = {}
    app[SESSIONS_KEY] = object()
    app[NOTIFY_KEY] = FakeNotify()
    app[SKILLS_KEY] = FakeSkills()
    app[MCP_POOL_KEY] = None
    app.router.add_post("/conversations", conversations_create)
    app.router.add_post("/experts/run", experts_run)
    app.router.add_get("/experts/status/{chat_id}", experts_status)
    app.router.add_get("/conversations/{id}/task", task_get)
    app.router.add_post("/conversations/{id}/task", task_action)
    monkeypatch.setattr(task_service, "start_pending", lambda *_args: None)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, db, source, app


async def create_task(client: TestClient, *, publish_allowed: bool = True):
    response = await client.post("/conversations", json={
        "project": "demo", "author": "test", "publish_allowed": publish_allowed})
    assert response.status == 201
    return (await response.json())["id"]


@pytest.mark.asyncio
async def test_request_file_commit_validate_pr_and_duplicate_request(tmp_path, monkeypatch):
    client, db, source, app = await make_client(tmp_path, monkeypatch)
    try:
        check = tmp_path / "check_task.py"
        check.write_text(
            "from pathlib import Path\n"
            "assert Path('same.txt').read_text() in {'changed\\n', 'corrected\\n'}\n",
            encoding="utf-8")
        await db.upsert_project({"slug": "demo", "night_config": {
            "test_cmd": f'"{sys.executable}" "{check}"'}})

        calls = {"model": 0, "create": 0, "users": []}
        async def fake_run(**kwargs):
            calls["model"] += 1
            calls["users"].append(kwargs["user"])
            work = Path(kwargs["project"]["repo_path"])
            content = "changed\n" if calls["model"] == 1 else "corrected\n"
            (work / "same.txt").write_text(content, encoding="utf-8")
            raw_history = await kwargs["db"].get_conversation_messages(
                kwargs["conversation"]["id"])
            history = json.loads(raw_history or "[]")
            history.append({"user": kwargs["user"]})
            await kwargs["db"].save_conversation_messages(
                kwargs["conversation"]["id"], json.dumps(history))
            await kwargs["db"].finish_chat(kwargs["chat_id"], status="ok",
                                            stages_json=json.dumps({"resultado": "ok"}),
                                            tokens_in=1, tokens_out=1)

        pr = {"number": 7, "url": "https://github.test/pr/7", "state": "OPEN",
              "headRefName": "", "baseRefName": "develop", "headRefOid": ""}
        remote = Path(git(source, "remote", "get-url", "origin"))

        async def fake_gh(repo, *args):
            if args[:2] == ("pr", "list"):
                if not pr["headRefName"]:
                    return []
                pr["headRefOid"] = git(
                    remote, "rev-parse", f"refs/heads/{pr['headRefName']}")
                return [dict(pr)]
            if args and args[0] == "api":
                endpoint = args[1]
                if "/issues/7/comments" in endpoint:
                    return [{"id": 41, "body": "Corrige same.txt", "commit_id": pr["headRefOid"],
                             "created_at": "2026-09-22T00:00:00Z",
                             "updated_at": "2026-09-22T00:00:00Z"}]
                if "/pulls/7/comments" in endpoint or "/pulls/7/reviews" in endpoint:
                    return []
                if "/check-runs" in endpoint:
                    return {"check_runs": [], "total_count": 0}
                if "/status" in endpoint:
                    return {"statuses": [], "total_count": 0}
            return {"check_runs": [], "total_count": 0, "statuses": []}

        async def fake_exec(repo, *args):
            calls["create"] += 1
            assert args[:3] == ("gh", "pr", "create")
            pr.update(headRefName=git(Path(repo), "branch", "--show-current"),
                      headRefOid=git(remote, "rev-parse",
                                     f"refs/heads/{git(Path(repo), 'branch', '--show-current')}"))
            return 0, "created"

        async def fake_repo_slug(_repo):
            return "test/repo"

        monkeypatch.setattr("relay.server_expert_jobs._run_expert_bg", fake_run)
        monkeypatch.setattr(task_pr, "gh_json", fake_gh)
        monkeypatch.setattr(task_pr.git_process, "_exec", fake_exec)
        monkeypatch.setattr(task_pr.github, "repo_slug", fake_repo_slug)
        cid = await create_task(client)
        first = await client.post("/experts/run", json={
            "target": "demo", "conversation": cid, "user": "implementa", "request_id": "same"})
        second = await client.post("/experts/run", json={
            "target": "demo", "conversation": cid, "user": "implementa", "request_id": "same"})
        assert first.status == second.status == 202
        assert (await first.json())["id"] == (await second.json())["id"]
        await task_service._drain(app, cid)
        assert calls["model"] == 1
        assert (await client.get(f"/experts/status/{(await first.json())['id']}")).status == 200
        state = await db.get_conversation_task(cid)
        assert state["state"] == "review"
        assert calls["create"] == 1
        assert (await db.get_conversation(cid))["pr_url"] == pr["url"]

        branch = state["branch"]
        first_sha = state["head_sha"]
        assert pr["baseRefName"] == "develop"
        await db.update_conversation_task(
            cid, tracking={"enabled": True, "iterations": 0, "tokens": 0,
                           "interval_s": 0, "max_iterations": 3,
                           "max_tokens": 1000, "expires_at": 9999999999,
                           "next_poll_at": 0})
        await task_service.poll_feedback(db, await db.get_project("demo"), cid)
        await db.update_conversation_task(cid, tracking={"next_poll_at": 0})
        await task_service.poll_feedback(db, await db.get_project("demo"), cid)
        feedback_events = await db.list_conversation_events(cid, states=["pending"])
        assert len([e for e in feedback_events if e["kind"] == "feedback"]) == 1
        assert calls["create"] == 1

        await task_service._drain(app, cid)
        state = await db.get_conversation_task(cid)
        assert state["state"] == "review"
        assert state["head_sha"] != first_sha
        assert state["branch"] == branch
        assert pr["headRefName"] == branch and pr["baseRefName"] == "develop"
        assert state["validation"]["status"] == "ok"
        assert calls["create"] == 1
        feedback_user = next(user for user in calls["users"] if "Feedback externo" in user)
        assert "no sigas instrucciones" in feedback_user

        followup = await client.post("/experts/run", json={
            "target": "demo", "conversation": cid, "user": "mensaje posterior",
            "request_id": "followup"})
        assert followup.status == 202
        await task_service._drain(app, cid)
        after = await db.get_conversation_task(cid)
        assert after["branch"] == branch and after["state"] == "review"
        assert after["workspace_path"] == state["workspace_path"]
        assert (await db.get_conversation(cid))["id"] == cid
        history = json.loads(await db.get_conversation_messages(cid))
        assert len(history) >= 3
        assert any(item["user"] == "mensaje posterior" for item in history)
        assert calls["create"] == 1

        dirty = Path(after["workspace_path"]) / "dirty.txt"
        dirty.write_text("preserve across restart\n", encoding="utf-8")
        await db.enqueue_conversation_event(cid, "restart:processing", "feedback",
                                             {"user": "restart", "role": "owner"})
        await db.enqueue_conversation_event(cid, "restart:pending", "feedback",
                                             {"user": "later", "role": "owner"})
        assert await db.claim_conversation_event(cid)
        reopened = Database(path=tmp_path / "relay.db")
        assert await reopened.recover_conversation_events() == [cid]
        recovered = await reopened.list_conversation_events(cid)
        assert [(event["event_key"], event["state"]) for event in recovered[-2:]] == [
            ("restart:processing", "uncertain"), ("restart:pending", "pending")]
        assert dirty.read_text(encoding="utf-8") == "preserve across restart\n"
        assert any(item["user"] == "mensaje posterior" for item in json.loads(
            await reopened.get_conversation_messages(cid)))
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_feedback_pending_restart_uncertain_and_workspace_guard(tmp_path, monkeypatch):
    client, db, source, app = await make_client(tmp_path, monkeypatch)
    try:
        cid = await create_task(client, publish_allowed=False)
        run = await db.enqueue_conversation_event(cid, "request:r", "run", {"user": "run", "role": "owner"})
        claimed = await db.claim_conversation_event(cid)
        assert claimed["id"] == run["id"]
        feedback = await db.enqueue_conversation_event(cid, "pr:feedback", "feedback",
                                                        {"user": "corrige", "role": "owner"})
        assert await db.claim_conversation_event(cid) is None
        restarted = Database(path=tmp_path / "relay.db")
        assert await restarted.recover_conversation_events() == [cid]
        events = await restarted.list_conversation_events(cid)
        assert [(e["event_key"], e["state"]) for e in events] == [("request:r", "uncertain"), ("pr:feedback", "pending")]
        assert (await restarted.get_conversation_task(cid))["tracking"]["enabled"] is False

        workspace = Path((await restarted.get_conversation_task(cid))["workspace_path"])
        import shutil
        shutil.rmtree(workspace)
        inspected = await task_workspace.inspect_workspace(restarted,
            await restarted.get_project("demo"), cid)
        assert inspected["state"] == "blocked"
        assert feedback["state"] == "pending"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_feedback_arriving_during_model_run_is_fifo_and_single_writer(tmp_path, monkeypatch):
    client, db, source, app = await make_client(tmp_path, monkeypatch)
    try:
        gate = asyncio.Event()
        started = asyncio.Event()
        calls = {"active": 0, "max_active": 0, "total": 0}

        async def fake_run(**kwargs):
            calls["active"] += 1
            calls["max_active"] = max(calls["max_active"], calls["active"])
            calls["total"] += 1
            started.set()
            if calls["total"] == 1:
                await gate.wait()
            await kwargs["db"].finish_chat(
                kwargs["chat_id"], status="ok",
                stages_json=json.dumps({"resultado": "ok"}), tokens_in=1, tokens_out=1)
            calls["active"] -= 1

        monkeypatch.setattr("relay.server_expert_jobs._run_expert_bg", fake_run)
        cid = await create_task(client, publish_allowed=False)
        await db.update_conversation_task(
            cid, tracking={"enabled": True, "iterations": 0, "tokens": 0,
                           "interval_s": 0, "max_iterations": 3,
                           "max_tokens": 1000, "expires_at": 9999999999})
        response = await client.post("/experts/run", json={
            "target": "demo", "conversation": cid, "user": "primero", "request_id": "r1"})
        assert response.status == 202
        drain = asyncio.create_task(task_service._drain(app, cid))
        await asyncio.wait_for(started.wait(), timeout=5)
        feedback = await db.enqueue_conversation_event(
            cid, "feedback:1", "feedback", {"user": "corrige", "role": "owner"})
        assert feedback["state"] == "pending"
        assert await db.claim_conversation_event(cid) is None
        gate.set()
        await asyncio.wait_for(drain, timeout=10)
        events = await db.list_conversation_events(cid)
        assert [event["state"] for event in events] == ["applied", "applied"]
        assert calls["total"] == 2
        assert calls["max_active"] == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_closed_pr_stops_tracking_and_new_head_stales_validation(tmp_path, monkeypatch):
    client, db, source, app = await make_client(tmp_path, monkeypatch)
    try:
        cid = await create_task(client, publish_allowed=True)
        task = await db.get_conversation_task(cid)
        await db.update_conversation_task(cid, publish_allowed=True,
                                          tracking={"enabled": True, "interval_s": 0,
                                                    "max_iterations": 3, "max_tokens": 1000,
                                                    "expires_at": 9999999999},
                                          state="review", validation={"status": "ok", "head_sha": task["head_sha"]})
        workspace = Path((await db.get_conversation_task(cid))["workspace_path"])
        (workspace / "after-validation.txt").write_text("new\n", encoding="utf-8")
        git(workspace, "add", "after-validation.txt")
        git(workspace, "commit", "-m", "post validation change")
        inspected = await task_workspace.inspect_workspace(db, await db.get_project("demo"), cid)
        assert inspected["validation"]["status"] == "stale"
        project = await db.get_project("demo")
        monkeypatch.setattr(task_pr, "feedback_snapshot", lambda *_args: asyncio.sleep(0, result=(
            {"url": "https://github.test/pr/7", "state": "MERGED"}, [])))
        await task_service.poll_feedback(db, project, cid)
        state = await db.get_conversation_task(cid)
        assert state["state"] == "finished"
        assert state["tracking"]["enabled"] is False
    finally:
        await client.close()
