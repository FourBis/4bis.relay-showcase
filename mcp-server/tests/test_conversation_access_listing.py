"""El listado aplica el ACL vigente antes del límite, sin releerlo por fila."""
import json
import uuid
from types import SimpleNamespace

import pytest

from relay import conversation_access as acl
from relay import identity
from relay.db import Database

A = "dev-a@example.test"
B = "dev-b@example.test"
ADMIN = "admin@example.test"


@pytest.fixture
async def env(tmp_path):
    (tmp_path / "source").mkdir()
    db = Database(tmp_path / "listing.db")
    await db.init_schema()
    await db.upsert_project({"slug": "example", "name": "Example", "enabled": True,
                             "repo_path": str(tmp_path / "source"), "defaults_json": {}})
    for email, role, projects in [(A, "member", ["example"]), (B, "member", ["example"]), (ADMIN, "owner", [])]:
        await db.set_user_role(email, role, project_slugs=projects, display_name=email.split("@")[0])
    a = await db.create_conversation(project_slug="example", requested_by=A)
    b = await db.create_conversation(project_slug="example", requested_by=B)
    return SimpleNamespace(db=db, tmp=tmp_path, a=a, b=b)


async def _conversation(db, actor, *, task=None, status="open"):
    cid = await db.create_conversation(project_slug="example", requested_by=actor, initial_task=task or {})
    await db.run("UPDATE conversations SET status=? WHERE id=?", (status, cid))
    return cid


async def _enable_private_mcp(db):
    await db.run("INSERT OR REPLACE INTO system_config(key,value) VALUES('RELAY_PRIVATE_MCP_ENABLED','1')")
    await db.run("UPDATE projects SET defaults_json=? WHERE slug='example'",
                 (json.dumps({"mcp_consultations": True}),))


async def _same_as_public_acl(db, rows, actor):
    for row in rows:
        assert row["access"] == await acl.get_access(db, row["id"], actor), row["id"]


async def test_listing_scopes_match_public_acl_and_private_mcp_rules(env):
    mine, shared = env.a, env.b
    private_mcp = await _conversation(env.db, A, task={"mcp_private": True})
    await acl.set_share(env.db, shared, B, A, "read")

    # Con las consultas MCP personales apagadas, ni su creador las ve.
    assert {r["id"] for r in await acl.listing(env.db, A, scope="mine")} == {mine}
    await _enable_private_mcp(env.db)
    assert (await acl.get_access(env.db, private_mcp, A))["can_read"]
    assert not (await acl.get_access(env.db, private_mcp, identity.OWNER))["can_read"]
    rows = await acl.listing(env.db, A, scope="mine")
    assert {r["id"] for r in rows} == {mine, private_mcp}
    await _same_as_public_acl(env.db, rows, A)
    assert {r["id"] for r in await acl.listing(env.db, A.upper(), scope="mine")} == {mine, private_mcp}
    rows = await acl.listing(env.db, A, scope="shared")
    assert [row["id"] for row in rows] == [shared]
    assert rows[0]["access"] == await acl.get_access(env.db, shared, A)
    assert rows[0]["access"]["share_mode"] == "read"
    assert {r["id"] for r in await acl.listing(env.db, identity.OWNER, scope="admin")} == {mine, shared}
    assert {r["id"] for r in await acl.listing(env.db, ADMIN, scope="admin")} == {mine, shared, private_mcp}
    # Un share previo no autoriza a leer una conversación que pasó a privada.
    await env.db.update_conversation_task(shared, mcp_private=True)
    assert await acl.listing(env.db, A, scope="shared") == []
    with pytest.raises(PermissionError):
        await acl.listing(env.db, B, scope="admin")


async def test_listing_projects_task_without_owner_configuration(env):
    cid = await _conversation(env.db, A, task={"mode": "write", "state": "ready", "repo_path": "/synthetic/source"})
    member = next(r for r in await acl.listing(env.db, A, scope="mine") if r["id"] == cid)
    projected = json.loads(member["task_json"])
    assert projected["mode"] == "write" and projected["state"] == "ready" and "repo_path" not in projected
    admin = next(r for r in await acl.listing(env.db, ADMIN, scope="admin") if r["id"] == cid)
    assert json.loads(admin["task_json"])["repo_path"] == "/synthetic/source"


async def test_listing_filters_acl_before_sorting_and_limit(env):
    readable = [await _conversation(env.db, A, status="listing-order") for _ in range(4)]
    unreadable = [await _conversation(env.db, B, task={"mcp_private": True}, status="listing-order")
                  for _ in range(12)]
    # Las candidatas privadas están dentro del scope admin y el dueño local
    # no puede leerlas. Si el LIMIT corre antes del ACL, la página queda vacía.
    await env.db.run("UPDATE conversations SET last_activity_at='2026-11-04T12:00:00Z' WHERE id IN (%s)" %
                     ",".join("?" for _ in unreadable), tuple(unreadable))
    await env.db.run("UPDATE conversations SET last_activity_at='2026-11-04T10:00:00Z' WHERE id IN (%s)" %
                     ",".join("?" for _ in readable), tuple(readable))
    result = await acl.listing(env.db, identity.OWNER, scope="admin", project="example",
                               status="listing-order", limit=3)
    assert [row["id"] for row in result] == sorted(readable)[:3]
    assert all(row["access"]["can_read"] for row in result)


async def test_listing_matches_live_role_grant_share_and_revocation(env):
    own, shared = env.a, env.b
    await acl.set_share(env.db, shared, B, A, "collaborate")
    assert [r["id"] for r in await acl.listing(env.db, A, scope="shared")] == [shared]
    await env.db.run("UPDATE users SET enabled=0 WHERE email=?", (B,))
    assert not (await acl.get_access(env.db, shared, B))["can_read"]
    with pytest.raises(PermissionError):
        await acl.listing(env.db, B, scope="mine")
    await env.db.set_user_role(B, "subadmin", project_slugs=["example"])
    assert (await acl.get_access(env.db, shared, B))["can_read"]
    await env.db.set_user_role(A, "member", project_slugs=[])
    assert await acl.listing(env.db, A, scope="shared") == []
    assert own in {r["id"] for r in await acl.listing(env.db, A, scope="mine")}
    await env.db.set_user_role(A, "member", project_slugs=["example"])
    await env.db.run("UPDATE projects SET enabled=0 WHERE slug='example'")
    assert await acl.listing(env.db, A, scope="shared") == []
    assert own in {r["id"] for r in await acl.listing(env.db, A, scope="mine")}
    await env.db.run("UPDATE projects SET enabled=1 WHERE slug='example'")
    await acl.revoke_share(env.db, shared, B, A)
    assert await acl.listing(env.db, A, scope="shared") == []


async def test_listing_project_case_alias_uses_same_first_project_acl(env):
    await env.db.upsert_project({"slug": "EXAMPLE", "name": "Later disabled alias",
                                 "enabled": False, "repo_path": str(env.tmp / "alias"),
                                 "defaults_json": {"read_only": True}})
    cid = await _conversation(env.db, A)
    await env.db.run("UPDATE conversations SET project_slug='EXAMPLE' WHERE id=?", (cid,))
    public_access = await acl.get_access(env.db, cid, A)
    listed = await acl.listing(env.db, A, scope="mine", project="example")
    assert public_access["can_read"] and public_access["can_send"]
    assert [row["id"] for row in listed].count(cid) == 1
    assert next(row for row in listed if row["id"] == cid)["access"] == public_access


@pytest.mark.parametrize(("task_json", "personal", "local"), [
    ("{", False, False), ("[]", False, False), ('"x"', False, False),
    ("0", False, False), ("false", False, False),
    # Un valor truthy marca la consulta como privada; sin MCP personal habilitado nadie la lista.
    ('{"mcp_private":"false"}', False, False), ('{"mcp_private":[]}', True, True),
])
async def test_listing_handles_invalid_json_and_python_truthiness(env, task_json, personal, local):
    cid = await _conversation(env.db, A)
    await env.db.run("UPDATE conversations SET task_json=? WHERE id=?", (task_json, cid))
    assert (cid in {r["id"] for r in await acl.listing(env.db, A, scope="mine")}) is personal
    assert (cid in {r["id"] for r in await acl.listing(env.db, identity.OWNER, scope="admin")}) is local
    assert (await acl.get_access(env.db, cid, A))["can_read"] is personal


async def test_listing_denies_non_object_project_defaults(env):
    await env.db.run("UPDATE projects SET defaults_json='[]' WHERE slug='example'")
    assert await acl.listing(env.db, A, scope="mine") == []
    assert await acl.listing(env.db, ADMIN, scope="admin") == []


async def test_listing_query_count_is_constant_for_result_size(env, monkeypatch):
    ids = [str(uuid.uuid4()) for _ in range(500)]
    stamp = "2026-11-04T12:00:00Z"

    def seed(conn):
        conn.executemany(
            "INSERT INTO conversations(id,project_slug,status,started_at,last_activity_at,requested_by) "
            "VALUES(?, 'example', 'listing-test', ?, ?, ?)",
            ((cid, stamp, stamp, A) for cid in ids),
        )
        conn.executemany(
            "INSERT INTO conversation_shares(conversation_id,email,permission,created_by,created_at,updated_at) "
            "VALUES(?,?,'read',?,?,?)",
            ((cid, B, A, stamp, stamp) for cid in ids),
        )

    await acl.transaction(env.db, seed, write=True)
    original = Database._connect
    statements = []

    def traced(db):
        conn = original(db)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(Database, "_connect", traced)
    for scope, actor in (("mine", A), ("admin", ADMIN), ("shared", B)):
        counts = []
        for limit in (100, 500):
            statements.clear()
            rows = await acl.listing(env.db, actor, scope=scope, project="example",
                                     status="listing-test", limit=limit)
            assert len(rows) == limit
            counts.append(len(statements))
        assert counts[0] == counts[1] < 20, (scope, counts)
