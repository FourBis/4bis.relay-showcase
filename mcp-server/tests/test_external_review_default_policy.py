"""Configuración de revisión por defecto: proyectos sin política propia no dependen del propietario."""
from __future__ import annotations

import copy
import json

import pytest

from relay import external_review_publish as publish, external_review_runtime as runtime
from relay import external_review_store as jobs
from relay.db import Database
from relay.issue_sync_store import transaction

WHO = "dev@example.test"
SHA_A, BASE = "a" * 40, "c" * 40
DEFAULT = {"enabled": True, "model": "test", "publication_mode": "personal",
           "publisher_actor": "", "recipients": []}


@pytest.fixture
async def review_store(tmp_path):
    db = Database(path=tmp_path / "review-jobs.db")
    await db.init_schema()
    await jobs.ensure_schema(db)
    await db.upsert_project({
        "slug": "demo", "name": "Synthetic review project", "repo_path": str(tmp_path / "repo"),
        "defaults_json": {"external_pr_review_policy": {"enabled": True, "model": "test"}},
    })
    cid = await db.create_conversation(project_slug="demo", requested_by=WHO)
    task = await db.update_conversation_task(
        cid, id=cid, mode="external_pr", requested_by=WHO,
        external_pr={"repo": "example/demo", "repo_id": 42, "number": 17,
                     "head_sha": SHA_A, "base_sha": BASE},
    )
    return db, task, await db.get_project("demo")


async def enqueue(db, task, project, *, actor=WHO):
    task = copy.deepcopy(task)

    def write(conn):
        job = jobs.enqueue(conn, task, project, actor)
        conn.execute("UPDATE conversations SET task_json=? WHERE id=?", (json.dumps(task), task["id"]))
        return job

    return await transaction(db, write)


@pytest.fixture(autouse=True)
def isolated_default():
    jobs._set_default({})
    yield
    jobs._set_default({})


def _without_policy(project):
    project = copy.deepcopy(project)
    project["defaults_json"] = {k: v for k, v in (project.get("defaults_json") or {}).items()
                                if k != "external_pr_review_policy"}
    return project


def test_effective_policy_prefers_project_then_default_then_nothing():
    own = {"defaults_json": {"external_pr_review_policy": {"enabled": False, "model": "x"}}}
    bare = {"defaults_json": {}}
    emptied = {"defaults_json": {"external_pr_review_policy": {}}}
    assert jobs.policy(bare) == {} and jobs.policy_source(bare) == "none"
    jobs._set_default(DEFAULT)
    assert jobs.policy(bare) == DEFAULT and jobs.policy_source(bare) == "default"
    assert jobs.policy(emptied) == DEFAULT, "una política vaciada vuelve a la de instalación"
    # Una política propia, aunque esté desactivada, es una decisión explícita del proyecto.
    assert jobs.policy(own) == {"enabled": False, "model": "x"} and jobs.policy_source(own) == "project"
    jobs.policy(bare)["recipients"].append("x@example.test")
    assert jobs.default_policy()["recipients"] == [], "la copia no altera la configuración guardada"


async def test_project_without_policy_queues_with_default_and_publishes_as_requester(review_store):
    db, task, project = review_store
    project = _without_policy(project)
    assert (await enqueue(db, task, project))["state"] == "blocked"
    await jobs.save_default(db, DEFAULT)
    job = await enqueue(db, task, project)
    assert job["state"] == "queued" and job["model"] == "test" and not job["error"]
    # Sin identidad fija, publica quien registró el PR, no el propietario.
    assert publish._policy(project, WHO, "review") == {"publication_mode": "personal", "publisher_actor": WHO}


async def test_default_survives_restart_and_can_be_removed(review_store):
    db, _task, _project = review_store
    await jobs.save_default(db, DEFAULT)
    jobs._set_default({})
    assert await jobs.load_default(db) == DEFAULT
    await jobs.save_default(db, {})
    assert await jobs.load_default(db) == {} and jobs.policy({"defaults_json": {}}) == {}


async def test_observer_includes_projects_without_policy_only_when_default_is_on(review_store, monkeypatch):
    db, task, project = review_store
    await db.upsert_project({**_without_policy(project), "slug": "demo"})
    seen = []

    async def refresh(db, project, cid, actor, authorize):
        seen.append(cid)

    async def authorized(db, slug, actor):
        return await db.get_project(slug)

    monkeypatch.setattr(runtime.external_pr, "refresh", refresh)
    monkeypatch.setattr(runtime, "authorized_project", authorized)
    await runtime._observe(db)
    assert seen == []
    await jobs.save_default(db, DEFAULT)
    await runtime._observe(db)
    assert seen == [task["id"]]
