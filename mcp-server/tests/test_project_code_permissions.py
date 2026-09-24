from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from relay import (execution_policy, identity, server_conversation_actions,
                   server_task_routes, task_workspace, user_accounts)
from relay.app_state import DB_KEY
from tests.test_task_continuity import create_task, make_client


PROJECT_A = {"slug": "aurora-demo", "enabled": True, "defaults_json": {}}
PROJECT_B = {"slug": "demo-sandbox", "enabled": True, "defaults_json": {}}


class UsersDb:
    def __init__(self, users):
        self.users = users

    async def list_users(self):
        return self.users


@pytest.fixture(autouse=True)
def isolated_identity(monkeypatch):
    for name in ("_roles", "_disabled", "_names", "_project_grants"):
        monkeypatch.setattr(identity, name, getattr(identity, name).copy())


@pytest.mark.parametrize("slug,expected", [("aurora-demo", True), ("demo-sandbox", False)])
def test_member_write_grant_is_scoped_to_one_project(slug, expected):
    identity.load_roles([{"email": "sam@example.test", "role": "member",
                          "enabled": True, "project_slugs": ["aurora-demo"]}])
    request = make_mocked_request("POST", "/conversations")
    request[identity.IDENTITY_KEY] = "sam@example.test"

    assert identity.can_write_project(request, {"slug": slug, "enabled": True,
                                                "defaults_json": {}}) is expected


def test_owner_keeps_existing_project_write_access():
    request = make_mocked_request("POST", "/conversations")
    request[identity.IDENTITY_KEY] = identity.OWNER

    assert identity.can_write_project(request, PROJECT_A)


@pytest.mark.parametrize("user,project", [
    ({"role": "member", "enabled": False, "project_slugs": ["aurora-demo"]}, PROJECT_A),
    ({"role": "member", "enabled": True, "project_slugs": []}, PROJECT_A),
])
def test_disabled_or_unassigned_member_cannot_be_written(user, project):
    assert not identity.user_can_write_project(user, project["slug"])


def test_project_read_only_overrides_an_active_member_grant():
    identity.load_roles([{"email": "sam@example.test", "role": "member",
                          "enabled": True, "project_slugs": ["aurora-demo"]}])
    request = make_mocked_request("POST", "/conversations")
    request[identity.IDENTITY_KEY] = "sam@example.test"

    assert not identity.can_write_project(
        request, {**PROJECT_A, "defaults_json": {"read_only": True}})
    assert not identity.can_write_project(request, {**PROJECT_A, "enabled": False})


@pytest.mark.asyncio
async def test_granted_member_gets_shell_but_not_unrestricted_tools_or_sql_write():
    db = UsersDb([{"email": "sam@example.test", "role": "member", "enabled": True,
                   "project_slugs": ["aurora-demo"]}])
    role_token = execution_policy.request_role.set("member")
    try:
        with user_accounts.bind_actor(db, "sam@example.test"):
            policy = await execution_policy.ExecutionPolicy.for_project(
                db, {**PROJECT_A, "_task_mode": "write"})
    finally:
        execution_policy.request_role.reset(role_token)

    assert policy.read_only is False
    assert policy.shell_allowed is True
    assert policy.unrestricted_tools is False
    assert policy.sql_read_only is True


@pytest.mark.asyncio
async def test_unassigned_or_revoked_member_cannot_keep_write_workspace():
    user = {"email": "sam@example.test", "role": "member", "enabled": True,
            "project_slugs": ["aurora-demo"]}
    identity.load_roles([user])  # La caché HTTP todavía ve la asignación.
    db = UsersDb([{**user, "project_slugs": []}])  # La DB ya la revocó.
    role_token = execution_policy.request_role.set("member")
    try:
        with user_accounts.bind_actor(db, user["email"]), pytest.raises(
                RuntimeError, match="Ya no tienes permiso de escritura"):
            await execution_policy.ExecutionPolicy.for_project(
                db, {**PROJECT_A, "_task_mode": "write"})
    finally:
        execution_policy.request_role.reset(role_token)


@pytest.mark.asyncio
async def test_for_project_revalidates_current_role_and_disabled_state_from_db():
    cases = [
        {"email": "sam@example.test", "role": "finance", "enabled": True,
         "project_slugs": ["aurora-demo"]},
        {"email": "sam@example.test", "role": "member", "enabled": False,
         "project_slugs": ["aurora-demo"]},
    ]
    for current_user in cases:
        db = UsersDb([current_user])
        role_token = execution_policy.request_role.set("member")
        try:
            with user_accounts.bind_actor(db, current_user["email"]), pytest.raises(
                    RuntimeError, match="Ya no tienes permiso de escritura"):
                await execution_policy.ExecutionPolicy.for_project(
                    db, {**PROJECT_A, "_task_mode": "write"})
        finally:
            execution_policy.request_role.reset(role_token)


@pytest.mark.asyncio
async def test_owner_policy_stays_unrestricted_and_project_read_only_stays_strict():
    db = UsersDb([])
    owner_token = execution_policy.request_role.set("owner")
    try:
        with user_accounts.bind_actor(db, identity.OWNER):
            owner_policy = await execution_policy.ExecutionPolicy.for_project(
                db, {**PROJECT_A, "_task_mode": "write"})
            read_only_policy = await execution_policy.ExecutionPolicy.for_project(
                db, {**PROJECT_A, "defaults_json": {"read_only": True}})
    finally:
        execution_policy.request_role.reset(owner_token)

    assert owner_policy.unrestricted_tools and owner_policy.shell_allowed
    assert read_only_policy.read_only and read_only_policy.sql_read_only
    assert not read_only_policy.unrestricted_tools and not read_only_policy.shell_allowed


@pytest.mark.asyncio
async def test_member_task_actions_are_project_scoped_and_never_publish(monkeypatch):
    db = AsyncMock()
    db.get_conversation.return_value = {"project_slug": "aurora-demo"}
    db.get_project.return_value = PROJECT_A
    monkeypatch.setattr(server_task_routes.task_service, "snapshot",
                        AsyncMock(return_value={"mode": "write", "state": "ready",
                                                "workspace_path": "PRIVATE_WORKSPACE",
                                                "origin_url": "PRIVATE_REPO",
                                                "error": "PRIVATE_ERROR"}))
    request = make_mocked_request("GET", "/tasks/task-a")
    request[identity.IDENTITY_KEY] = "sam@example.test"
    identity.load_roles([{"email": "sam@example.test", "role": "member",
                          "enabled": True, "project_slugs": ["aurora-demo"]}])

    allowed = await server_task_routes._task_snapshot(request, db, "conversation-a")
    assert allowed["allowed_actions"] == ["continue", "pause", "cancel"]
    assert "publish" not in allowed["allowed_actions"]
    assert allowed["can_control"] is False
    assert "workspace_path" not in allowed and "origin_url" not in allowed
    assert allowed["error"] == "La tarea requiere revisión del propietario."

    db.get_project.return_value = PROJECT_B
    denied = await server_task_routes._task_snapshot(request, db, "conversation-b")
    assert denied["allowed_actions"] == []


@pytest.mark.asyncio
async def test_http_enable_write_requires_grant_and_member_cannot_publish_or_track(
        tmp_path, monkeypatch):
    client, db, _source, _app = await make_client(tmp_path, monkeypatch)
    email = "sam@example.test"
    monkeypatch.setattr(identity, "requester", lambda _request: email)
    identity.load_roles([{"email": email, "role": "member", "enabled": True,
                          "project_slugs": []}])
    try:
        cid = await create_task(client)
        state = await db.get_conversation_task(cid)
        assert state["mode"] == "read_only"

        initialize = AsyncMock(return_value={"mode": "write"})
        monkeypatch.setattr(task_workspace, "initialize_task", initialize)
        denied = await client.post(f"/conversations/{cid}/task", json={
            "action": "enable_write"})
        assert denied.status == 403
        initialize.assert_not_awaited()

        identity.load_roles([{"email": email, "role": "member", "enabled": True,
                              "project_slugs": ["demo"]}])
        enabled = await client.post(f"/conversations/{cid}/task", json={
            "action": "enable_write"})
        assert enabled.status == 200
        initialize.assert_awaited_once_with(db, await db.get_project("demo"), cid,
                                            promote=True)

        initialize.side_effect = RuntimeError("C:\\private\\workspace")
        failure = await client.post(f"/conversations/{cid}/task", json={
            "action": "enable_write"})
        assert failure.status == 422
        assert "private" not in (await failure.text()).lower()
        initialize.side_effect = user_accounts.AccountError(
            "Conecta tu cuenta de GitHub desde Mi cuenta.")
        account_failure = await client.post(f"/conversations/{cid}/task", json={
            "action": "enable_write"})
        account_body = await account_failure.json()
        assert account_failure.status == 422
        assert "Mi cuenta" in account_body["error"]
        assert account_body["connect_url"] == "/admin/#/account"
        initialize.side_effect = None

        publish = await client.post(f"/conversations/{cid}/task", json={
            "action": "publish", "request_id": "member-publish"})
        track = await client.post(f"/conversations/{cid}/task", json={
            "action": "track", "enabled": True})
        assert publish.status == 403
        assert track.status == 403
        assert await db.list_conversation_events(cid) == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_member_can_close_legacy_conversation_without_finalizing_pr(monkeypatch):
    class LegacyDb:
        def __init__(self):
            self.conversation = {"id": "legacy-1", "project_slug": "aurora-demo",
                                 "status": "open", "branch": "feature/legacy",
                                 "messages_json": "", "summary": ""}
            self.project = {**PROJECT_A, "repo_path": "C:/repo"}
            self.closed = False

        async def get_conversation(self, _conv_id):
            return self.conversation

        async def get_conversation_task(self, _conv_id):
            return None  # legacy: no task_json

        async def get_project(self, _slug):
            return self.project

        async def close_conversation(self, _conv_id):
            self.closed = True

    db = LegacyDb()
    app = web.Application()
    app[DB_KEY] = db
    request = make_mocked_request("POST", "/conversations/legacy-1/close", app=app,
                                  match_info={"id": "legacy-1"})
    request[identity.IDENTITY_KEY] = "sam@example.test"
    identity.load_roles([{"email": "sam@example.test", "role": "member", "enabled": True,
                          "project_slugs": ["aurora-demo"]}])
    spawn = AsyncMock()
    finalize = AsyncMock()
    monkeypatch.setattr(server_conversation_actions, "_spawn_bg", spawn)
    monkeypatch.setattr(server_conversation_actions, "_finalize_pr_bg", finalize)

    response = await server_conversation_actions.conversations_close(request)

    assert response.status == 200
    assert db.closed
    assert not spawn.called
    assert not finalize.called


def test_member_without_actor_or_grant_has_no_shell_or_write_access():
    policy = execution_policy.ExecutionPolicy.for_run({}, role="member")
    assert policy.read_only and policy.sql_read_only
    assert not policy.shell_allowed and not policy.unrestricted_tools
    assert not identity.user_can_write_project(
        {"role": "member", "enabled": True, "project_slugs": []}, "aurora-demo")
