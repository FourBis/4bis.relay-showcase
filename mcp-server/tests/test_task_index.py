"""Las lecturas de arquitectura/índice respetan el workspace de conversación."""
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from relay import admin_diagrams, admin_indexing, admin_workspace, identity
from relay.app_state import DB_KEY


class _Db:
    async def get_project(self, slug):
        return {"slug": slug, "repo_path": "C:/source-root"}

    async def get_conversation(self, cid):
        return {"id": cid, "project_slug": "demo"}


def _request(slug="demo", conversation="conv-1"):
    db = _Db()
    app = web.Application()
    app[DB_KEY] = db
    query = "?conversation=" + conversation if conversation else ""
    request = make_mocked_request("GET", "/projects/" + slug + query,
                                  app=app, match_info={"slug": slug})
    request[identity.IDENTITY_KEY] = identity.OWNER
    return request


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
async def test_cross_project_conversation_is_rejected(monkeypatch):
    # Unidad del vínculo proyecto/conversación: aun con lectura autorizada,
    # una conversación de otro proyecto debe ser rechazada.
    authorize = AsyncMock()
    monkeypatch.setattr(admin_workspace.conversation_access, "require", authorize)
    request = _request(slug="other")
    response = await admin_diagrams.api_project_architecture(request)
    assert response.status == 400
    authorize.assert_awaited_once_with(request.app[DB_KEY], "conv-1", identity.OWNER, "read")
