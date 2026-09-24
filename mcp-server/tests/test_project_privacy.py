"""Regresiones de privacidad para remotes y proyectos.

Los fixtures usan valores sintéticos. No se conectan a proveedores ni leen la
configuración real del usuario.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from relay import admin, identity
from relay.db import Database


def _repo_with_remote(tmp_path: Path, remote: str) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    git = repo / ".git"
    git.mkdir(parents=True)
    config = git / "config"
    config.write_text(
        f'[remote "origin"]\n\turl = {remote}\n', encoding="utf-8"
    )
    return repo, config.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        (
            "https://fixture-user:FAKE_SECRET@github.example/org/repo.git?x=1#frag",
            "https://github.example/org/repo.git",
        ),
        (
            "https://github.example/org/repo.git",
            "https://github.example/org/repo.git",
        ),
        (
            "ssh://git@github.example/org/repo.git?x=1#frag",
            "ssh://github.example/org/repo.git",
        ),
        (
            "git://fixture-user:FAKE_SECRET@github.example/org/repo.git?x=1#frag",
            "git://github.example/org/repo.git",
        ),
        (
            "http://fixture-user:FAKE_SECRET@github.example/org/repo.git?x=1#frag",
            "http://github.example/org/repo.git",
        ),
        (
            "git@github.example:org/repo.git",
            "ssh://github.example/org/repo.git",
        ),
    ],
)
def test_git_remote_public_shape(tmp_path: Path, remote: str, expected: str) -> None:
    repo, before = _repo_with_remote(tmp_path, remote)
    admin._remote_url_cache.pop(str(repo), None)

    assert admin._git_remote_url(str(repo)) == expected
    assert admin._git_remote_url(str(repo)) == expected
    assert (repo / ".git" / "config").read_text(encoding="utf-8") == before
    assert "FAKE_SECRET" not in (admin._git_remote_url(str(repo)) or "")


@pytest.mark.parametrize(
    "remote",
    [
        "C:/local/repo",
        "file:///C:/local/repo",
        "javascript:alert(1)",
        "gopher://github.example/org/repo.git",
        "https://",
        "ssh://",
        "not a URL",
    ],
)
def test_git_remote_local_unknown_or_malformed_is_hidden(
    tmp_path: Path, remote: str
) -> None:
    repo, _ = _repo_with_remote(tmp_path, remote)
    admin._remote_url_cache.pop(str(repo), None)
    assert admin._git_remote_url(str(repo)) is None


async def _make_client(tmp_path: Path, monkeypatch) -> tuple[TestClient, Database, object]:
    db_path = tmp_path / "relay.db"
    # create_app reads these paths during startup; keep every artifact inside
    # pytest's temporary directory.
    monkeypatch.setenv("FOURBIS_DB_PATH", str(db_path))
    monkeypatch.setenv("FOURBIS_CHATS_DIR", str(tmp_path / "chats"))
    monkeypatch.setenv("FOURBIS_JSONL_DIR", str(tmp_path / "jsonl"))
    monkeypatch.setenv("FOURBIS_LOG_DIR", str(tmp_path / "logs"))

    db = Database()
    await db.init_schema()
    repo = tmp_path / "workspace"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.example/org/repo.git\n',
        encoding="utf-8",
    )
    await db.upsert_project(
        {
            "slug": "fixture",
            "name": "Fixture",
            "repo_path": str(repo),
            "description": "SYNTHETIC_DESCRIPTION",
            "system_prompt": "SYNTHETIC_PRIVATE_PROMPT",
            "mcp_servers": [{"name": "fixture", "env": {"TOKEN": "SYNTHETIC_MCP_VALUE"}}],
            "defaults_json": {"rutas_extra": ["SYNTHETIC_PRIVATE_PATH"]},
            "native_tools": ["SYNTHETIC_NATIVE_TOOL"],
            "enabled": True,
        }
    )
    await db.set_config("DISCORD_GUILD_ID", "SYNTHETIC_GUILD")
    await db.set_user_role("member@example.test", "member")

    from relay.server import create_app

    app = create_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, db, app


@pytest.mark.asyncio
async def test_member_payload_is_allowlisted_owner_keeps_config(
    tmp_path: Path, monkeypatch
) -> None:
    client, _db, _app = await _make_client(tmp_path, monkeypatch)
    try:
        member_headers = {"Cf-Access-Jwt-Assertion": "fixture-jwt"}
        with patch("relay.identity.verify", return_value="member@example.test"):
            list_response = await client.get(
                "/admin/api/projects", headers=member_headers
            )
            detail_response = await client.get(
                "/admin/api/projects/fixture", headers=member_headers
            )
        assert list_response.status == 200
        assert detail_response.status == 200

        allowed = {
            "slug",
            "name",
            "description",
            "enabled",
            "indexed",
            "index_stats",
            "has_git",
            "git_remote_url",
        }
        listed = next(
            project
            for project in (await list_response.json())["projects"]
            if project["slug"] == "fixture"
        )
        detailed = (await detail_response.json())["project"]
        assert set(listed) == allowed
        assert set(detailed) == allowed - {"indexed", "index_stats"}
        assert listed["git_remote_url"] == "https://github.example/org/repo.git"
        assert detailed["git_remote_url"] == listed["git_remote_url"]
        assert (await list_response.json()).get("discord_guild_id") != "SYNTHETIC_GUILD"

        # Local owner behavior remains available without Access identity.
        owner_response = await client.get("/admin/api/projects/fixture")
        assert owner_response.status == 200
        owner_project = (await owner_response.json())["project"]
        assert owner_project["system_prompt"] == "SYNTHETIC_PRIVATE_PROMPT"
        assert owner_project["mcp_servers"][0]["env"]["TOKEN"] == "SYNTHETIC_MCP_VALUE"
        assert owner_project["defaults_json"]["rutas_extra"] == [
            "SYNTHETIC_PRIVATE_PATH"
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_member_cannot_read_effective_system_prompt_owner_can(
    tmp_path: Path, monkeypatch
) -> None:
    client, _db, _app = await _make_client(tmp_path, monkeypatch)
    try:
        with patch("relay.identity.verify", return_value="member@example.test"):
            member_response = await client.get(
                "/admin/api/projects/fixture/system-prompt",
                headers={"Cf-Access-Jwt-Assertion": "fixture-jwt"},
            )
        assert member_response.status == 403

        owner_response = await client.get(
            "/admin/api/projects/fixture/system-prompt"
        )
        assert owner_response.status == 200
        owner_body = await owner_response.json()
        assert owner_body["blocks"]["system_prompt"] == "SYNTHETIC_PRIVATE_PROMPT"
    finally:
        await client.close()


def _task_payload(value):
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


def _conversation_task(value):
    if not isinstance(value, dict):
        return {}
    return _task_payload(value.get("task_json", value.get("task", {})))


@pytest.mark.asyncio
async def test_member_task_views_hide_private_metadata_and_git_pr_is_forbidden(
    tmp_path: Path, monkeypatch
) -> None:
    client, db, _app = await _make_client(tmp_path, monkeypatch)
    conv_id = await db.create_conversation(project_slug="fixture")
    await db.update_conversation_task(
        conv_id,
        mode="write",
        state="blocked",
        source_repo="PRIVATE_SOURCE_REPO",
        workspace_path=str(tmp_path / "PRIVATE_WORKSPACE"),
        origin_url="https://fixture:FAKE_SECRET@example.test/repo.git",
        requested_by="PRIVATE_REQUESTED_BY_EMAIL",
        email="PRIVATE_EMAIL",
        futurefield="PRIVATE_FUTURE_FIELD",
        error="PRIVATE_ERROR_DETAIL",
        validation={
            "status": "passed",
            "head_sha": "synthetic-sha",
            "detail": "PRIVATE_VALIDATION_DETAIL",
        },
    )
    member_headers = {"Cf-Access-Jwt-Assertion": "fixture-jwt"}
    inspect = AsyncMock(return_value=None)
    try:
        with patch("relay.task_workspace.inspect_workspace", inspect):
            with patch("relay.identity.verify", return_value="member@example.test"):
                list_response = await client.get(
                    "/conversations", headers=member_headers
                )
                detail_response = await client.get(
                    f"/conversations/{conv_id}", headers=member_headers
                )
                task_response = await client.get(
                    f"/conversations/{conv_id}/task", headers=member_headers
                )
                pr_response = await client.post(
                    f"/conversations/{conv_id}/git/pr",
                    headers=member_headers,
                    json={},
                )

        assert list_response.status == 200
        assert detail_response.status == 200
        assert task_response.status == 200
        assert pr_response.status == 403
        assert not inspect.await_args_list or all(
            call.args[2] == conv_id for call in inspect.await_args_list
        )

        listed = next(
            item
            for item in (await list_response.json())["conversations"]
            if item["id"] == conv_id
        )
        member_views = [
            await list_response.json(),
            await detail_response.json(),
            await task_response.json(),
        ]
        listed_task = _conversation_task(listed)
        detailed_task = _conversation_task(await detail_response.json())
        task_body = await task_response.json()
        task_view = _task_payload(
            task_body.get("task_json", task_body.get("task", task_body))
        )
        assert listed_task
        assert detailed_task
        assert task_view
        private_markers = (
            "PRIVATE_SOURCE_REPO",
            "PRIVATE_WORKSPACE",
            "FAKE_SECRET",
            "PRIVATE_REQUESTED_BY_EMAIL",
            "PRIVATE_EMAIL",
            "PRIVATE_FUTURE_FIELD",
            "PRIVATE_ERROR_DETAIL",
            "PRIVATE_VALIDATION_DETAIL",
        )
        serialized_member_views = json.dumps(member_views, sort_keys=True)
        assert all(marker not in serialized_member_views for marker in private_markers)
        for task_view in (listed_task, detailed_task, task_view):
            if task_view:
                assert task_view.get("can_control") is False
                assert task_view.get("validation") == {
                    "status": "passed",
                    "head_sha": "synthetic-sha",
                }

        # La identidad local conserva el snapshot completo para el owner.
        with patch("relay.task_workspace.inspect_workspace", inspect):
            owner_task_response = await client.get(
                f"/conversations/{conv_id}/task"
            )
        assert owner_task_response.status == 200
        owner_body = await owner_task_response.json()
        owner_task = _task_payload(
            owner_body.get("task_json", owner_body.get("task", owner_body))
        )
        assert owner_task["source_repo"] == "PRIVATE_SOURCE_REPO"
        assert owner_task["workspace_path"].endswith("PRIVATE_WORKSPACE")
        assert owner_task["origin_url"].endswith("FAKE_SECRET@example.test/repo.git")
        assert owner_task["requested_by"] == "PRIVATE_REQUESTED_BY_EMAIL"
        assert owner_task["email"] == "PRIVATE_EMAIL"
        assert owner_task["futurefield"] == "PRIVATE_FUTURE_FIELD"
        assert owner_task["error"] == "PRIVATE_ERROR_DETAIL"
        assert owner_task["validation"]["detail"] == "PRIVATE_VALIDATION_DETAIL"
    finally:
        await client.close()
