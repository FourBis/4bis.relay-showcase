"""Admin can configure an explicit policy without replacing project defaults."""
from __future__ import annotations

import copy

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from relay import admin_projects, identity
from relay.app_state import DB_KEY
from relay.db import Database


POLICY = {"low": {"required_checks": ["CI / tests"], "require_declaration": True}}


@pytest.fixture
async def policy_client(tmp_path, monkeypatch):
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": str(tmp_path),
        "defaults_json": {"timeout": 240, "github_project": {"number": 5}, "read_only": False}})
    who = "admin@example.test"
    monkeypatch.setattr(identity, "_roles", {who: "owner"})
    monkeypatch.setattr(identity, "_disabled", set())
    monkeypatch.setattr(identity, "_project_grants", {who: ["demo"]})
    @web.middleware
    async def actor(request, handler):
        request[identity.IDENTITY_KEY] = who
        return await handler(request)
    app = web.Application(middlewares=[actor, identity.require_role])
    app[DB_KEY] = db
    app.router.add_patch("/admin/api/projects/{slug}", admin_projects.api_projects_patch)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client, db, who
    await client.close()


async def test_policy_patch_preserves_unrelated_defaults_and_can_disable(policy_client):
    client, db, _ = policy_client
    previous = (await db.get_project("demo"))["defaults_json"]
    response = await client.patch("/admin/api/projects/demo", json={"external_pr_policy": POLICY})
    assert response.status == 200
    assert (await response.json())["project"]["defaults_json"] == {**previous, "external_pr_policy": POLICY}
    stored = await db.get_project("demo")
    assert stored["defaults_json"] == {**previous, "external_pr_policy": POLICY}
    assert (await client.patch("/admin/api/projects/demo", json={"external_pr_policy": {}})).status == 200
    assert (await db.get_project("demo"))["defaults_json"] == {**previous, "external_pr_policy": {}}


async def test_policy_is_preserved_by_other_project_edits(policy_client):
    client, db, _ = policy_client
    await client.patch("/admin/api/projects/demo", json={"external_pr_policy": POLICY})
    response = await client.patch("/admin/api/projects/demo", json={"description": "Updated description"})
    assert response.status == 200
    assert (await db.get_project("demo"))["defaults_json"]["external_pr_policy"] == POLICY


@pytest.mark.parametrize("policy", [
    None, [], "invalid", {"unknown": POLICY["low"]},
    {"low": {}}, {"low": {**POLICY["low"], "approve_self": True}},
    {"low": {"required_checks": ["tests"]}},
    {"low": {"required_checks": [], "require_declaration": True}},
    {"low": {"required_checks": "tests", "require_declaration": True}},
    {"low": {"required_checks": [" "], "require_declaration": True}},
    {"low": {"required_checks": [42], "require_declaration": True}},
    {"low": {"required_checks": ["tests", "tests"], "require_declaration": True}},
    {"low": {"required_checks": ["tests", " tests "], "require_declaration": True}},
    {"low": {"required_checks": ["x" * 201], "require_declaration": True}},
    {"low": {"required_checks": [str(index) for index in range(21)], "require_declaration": True}},
    {"low": {"required_checks": ["tests"], "require_declaration": 1}},
])
async def test_invalid_policy_rejected_without_mutating_project(policy_client, policy):
    client, db, _ = policy_client
    previous = await db.get_project("demo")
    response = await client.patch("/admin/api/projects/demo", json={"external_pr_policy": policy, "name": "Must not apply"})
    assert response.status == 422
    assert (await response.json())["error"]
    current = await db.get_project("demo")
    assert current["name"] == previous["name"]
    assert current["defaults_json"] == previous["defaults_json"]


@pytest.mark.parametrize("role", ["member", "subadmin", "finance"])
async def test_policy_configuration_requires_admin_even_with_project_grant(policy_client, monkeypatch, role):
    client, db, who = policy_client
    previous = await db.get_project("demo")
    monkeypatch.setattr(identity, "_roles", {who: role})
    response = await client.patch("/admin/api/projects/demo", json={"external_pr_policy": POLICY})
    assert response.status == 403
    assert (await db.get_project("demo"))["defaults_json"] == previous["defaults_json"]


def test_policy_validator_returns_independent_normalized_mapping():
    original = copy.deepcopy(POLICY)
    original["low"]["required_checks"] = [" CI / tests "]
    validated = admin_projects._external_pr_policy(original)
    assert validated == POLICY
    validated["low"]["required_checks"].append("other")
    assert original["low"]["required_checks"] == [" CI / tests "]
