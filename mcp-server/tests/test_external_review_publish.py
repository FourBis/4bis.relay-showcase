"""La publicación personal se identifica como Relay y el informe se replica en el issue vinculado."""
from __future__ import annotations

import json

import pytest

from relay import external_pr, external_review_publish as publish, external_review_runtime as runtime
from tests.test_external_pr import BASE, SHA_A, WHO, registration  # noqa: F401  (fixture)

COMMENT = {"path": "src/demo.py", "line": 2, "side": "RIGHT", "body": "Synthetic finding"}
MARK = "🤖 Relay · revisión automática: "


def _event(kind, *, mode="personal", comments=None):
    return {"id": "effect-1", "kind": kind, "head_sha": SHA_A,
            "publisher": {"login": "dev", "mode": mode},
            "payload": {"model": "fixture-model", "summary": "Resumen sintético", "base_sha": BASE,
                        "comments": [COMMENT] if comments is None else comments,
                        "pr": {"url": "https://github.com/AuroraDemo/relay-demo/pull/17"}}}


def test_inline_comments_are_marked_once_as_relay():
    marked = publish._comments([COMMENT], marked=True)
    assert marked[0]["body"] == MARK + COMMENT["body"]
    assert publish._comments(marked, marked=True) == marked
    assert publish._comments([COMMENT]) == [COMMENT]


def test_personal_review_body_names_relay_and_the_account():
    body = publish._body(_event("review"))
    assert body.startswith("## 🤖 Relay · revisión automática")
    assert "con la cuenta de @dev; no lo escribió esa persona" in body
    assert "no ejecutó pruebas ni aprueba merge o despliegue" in body
    assert "@dev" not in publish._body(_event("review", mode="bot"))
    assert "### Hallazgos" not in body


def test_issue_copy_lists_findings_by_file_and_line():
    body = publish._body(_event("review_issue"))
    assert body.startswith("## 🤖 Relay · revisión automática")
    assert "### Hallazgos en el PR" in body and "`src/demo.py:2` — Synthetic finding" in body
    assert SHA_A in body and "https://github.com/AuroraDemo/relay-demo/pull/17" in body
    assert "Sin hallazgos en el diff revisado." in publish._body(_event("review_issue", comments=[]))


async def test_issue_copy_never_targets_the_pr(registration):  # noqa: F811
    db, project, body, _remote = registration
    task = await external_pr.register(db, project, body, WHO)
    with pytest.raises(ValueError, match="issue vinculado"):
        await publish.enqueue_effect(db, task["id"], "review_issue", "job-1", task["external_pr"]["head_sha"],
                                     {"summary": "Informe", "model": "m", "comments": [COMMENT],
                                      "target": {"kind": "pr", "number": 17}}, WHO)


async def test_pr_report_is_copied_once_to_the_linked_issue_only(registration):  # noqa: F811
    db, project, body, _remote = registration
    task = await external_pr.register(db, project, body, WHO)
    cid = task["id"]
    await publish.enqueue_effect(db, cid, "review", "job-1", task["external_pr"]["head_sha"],
                                 {"summary": "Informe", "model": "m", "comments": [COMMENT]}, WHO)
    await runtime._mirror_to_issue(db)
    assert [e["kind"] for e in (await publish.status(db, cid))["effects"]] == ["review"]
    await db.update_conversation_task(cid, issue_link={
        "repo": task["external_pr"]["repo"], "repo_id": task["external_pr"]["repo_id"], "number": 9, "node_id": "I_9"})
    await runtime._mirror_to_issue(db)
    await runtime._mirror_to_issue(db)
    effects = (await publish.status(db, cid))["effects"]
    assert [e["kind"] for e in effects] == ["review", "review_issue"]
    rows = await db.run("SELECT payload FROM external_review_effects WHERE kind='review_issue'")
    payload = json.loads(rows[0]["payload"])
    assert payload["target"] == {"kind": "issue", "number": 9}
    # La réplica conserva el hallazgo sin la marca inline: el encabezado ya identifica a Relay.
    assert payload["comments"][0]["body"] == COMMENT["body"]
