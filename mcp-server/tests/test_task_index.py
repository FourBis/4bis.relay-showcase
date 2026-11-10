"""Las lecturas de arquitectura/índice respetan el workspace de conversación."""
import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

from relay import admin_diagrams, admin_indexing, admin_workspace, identity
from relay.app_state import DB_KEY
from relay.db import Database


class _Db:
    async def get_project(self, slug):
        return {"slug": slug, "repo_path": "C:/source-root"}

    async def get_conversation(self, cid):
        return {"id": cid, "project_slug": "demo"}


def _request(slug="demo", conversation="conv-1"):
    db = _Db()
    return SimpleNamespace(
        app={DB_KEY: db},
        match_info={"slug": slug},
        query={"conversation": conversation} if conversation else {},
    )


@pytest.mark.asyncio
async def test_index_and_architecture_use_task_root(monkeypatch):
    seen = []
    monkeypatch.setattr(admin_diagrams, "_arch_cache", {})

    monkeypatch.setattr(admin_diagrams, "cbm_binary_path", lambda: "cbm")
    monkeypatch.setattr(admin_indexing, "cbm_binary_path", lambda: "cbm")

    async def fake_cli(*args):
        payload = json.loads(args[2])
        seen.append(payload["project"])
        if args[1] == "get_architecture":
            return "layers: 0 (cols: name layer reason)\n", ""
        return "total: 0\nresults: 0 (rows: name label lines in out)\nhas_more: false\n", ""

    monkeypatch.setattr(admin_diagrams, "_cbm_cli_text", fake_cli)
    monkeypatch.setattr(admin_indexing, "_cbm_cli_text", fake_cli)
    monkeypatch.setattr(admin_diagrams, "_cbm_project_name", lambda path: seen.append(path) or "task-root")
    monkeypatch.setattr(admin_indexing, "_cbm_project_name", lambda path: seen.append(path) or "task-root")
    async def _task_project(request):
        return {"slug": "demo", "repo_path": "C:/task-root"}, None

    monkeypatch.setattr(admin_diagrams, "_project_for_request", _task_project)
    monkeypatch.setattr(admin_workspace, "_project_for_request", _task_project)

    await admin_diagrams.api_project_architecture(_request())
    await admin_indexing.api_index_files(_request())
    assert seen == ["C:/task-root", "task-root", "C:/task-root", "task-root"]


@pytest.mark.asyncio
async def test_cross_project_conversation_is_rejected(tmp_path, monkeypatch):
    db = Database(tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "enabled": True,
                             "repo_path": str(tmp_path / "demo")})
    await db.upsert_project({"slug": "other", "name": "Other", "enabled": True,
                             "repo_path": str(tmp_path / "other")})
    actor = "dev@example.test"
    await db.set_user_role(actor, "member", project_slugs=["demo"])
    conversation = await db.create_conversation(
        project_slug="demo", requested_by=actor)
    calls = []

    async def should_not_run(*args):
        calls.append(args)

    monkeypatch.setattr(admin_diagrams, "_cbm_cli_text", should_not_run)
    request = make_mocked_request(
        "GET", f"/admin/api/projects/other/architecture?conversation={conversation}",
        app={DB_KEY: db}, match_info={"slug": "other"})
    request[identity.IDENTITY_KEY] = actor

    response = await admin_diagrams.api_project_architecture(request)
    assert response.status == 400
    assert "pertenece a otro proyecto" in response.text
    assert calls == []
