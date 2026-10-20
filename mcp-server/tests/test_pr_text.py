"""Cuerpos de PR neutrales con procesos y efectos externos simulados."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from relay import github, git_process, github_credentials, night_worker
from relay import server_conversation_jobs, task_pr
from relay.pr_text import neutralize_issue_closing_keywords


@pytest.mark.parametrize("keyword", [
    "close", "closes", "closed", "fix", "fixes", "fixed",
    "resolve", "resolves", "resolved",
])
@pytest.mark.parametrize("separator", [" ", ": ", ":", "\n"])
@pytest.mark.parametrize("reference", [
    "#7", "demo-org/demo.repo#8", "https://github.com/demo-org/demo/issues/9",
])
def test_closing_reference_becomes_neutral_without_losing_surrounding_text(
        keyword, separator, reference):
    body = f"Resumen. {keyword.upper()}{separator}{reference}. Pruebas aprobadas."
    expected = f"Resumen. Relacionado con {reference}. Pruebas aprobadas."
    assert neutralize_issue_closing_keywords(body) == expected
    assert neutralize_issue_closing_keywords(expected) == expected


@pytest.mark.parametrize("body", [
    "Corrige el login.\nPruebas aprobadas.",
    "Close the dialog; fixes are listed below.",
    "Fixed a regression in #7.",
    "The prefixes #7 and fixes_count are identifiers.",
    "Closes: this is not an issue reference.",
    "",
])
def test_ordinary_prose_is_preserved(body):
    assert neutralize_issue_closing_keywords(body) == body


@pytest.mark.parametrize("executor", ["shared", "night"])
@pytest.mark.parametrize("body_args", [
    ("--body", "Closes #7"), ("-b", "Closes #7"), ("--body=Closes #7",),
])
async def test_cli_creators_send_neutral_body_to_the_mock_process(
        monkeypatch, executor, body_args):
    process = type("Process", (), {
        "returncode": 0,
        "communicate": AsyncMock(return_value=(b"created", b"")),
    })()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(git_process.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(github_credentials, "gh_env", AsyncMock(return_value={}))
    if executor == "shared":
        result = await git_process._exec(
            "demo-repo", "gh", "pr", "create", "--title", "Fixes #99", *body_args)
    else:
        worker = type("Worker", (), {"repo": "demo-repo"})()
        result = await night_worker.BranchWorker._exec(
            worker, "gh", "pr", "create", "--title", "Fixes #99", *body_args)
    assert result == (0, "created")
    args = spawn.await_args.args
    assert "Fixes #99" in args  # La transformación solo afecta al cuerpo.
    assert any("Relacionado con #7" in arg for arg in args)
    assert not any("Closes #7" in arg for arg in args)


async def test_unrelated_cli_operations_keep_their_body(monkeypatch):
    process = type("Process", (), {
        "returncode": 0,
        "communicate": AsyncMock(return_value=(b"ok", b"")),
    })()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(git_process.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(github_credentials, "gh_env", AsyncMock(return_value={}))
    await git_process._exec("demo-repo", "gh", "pr", "view", "--body", "Closes #7")
    assert spawn.await_args.args[-1] == "Closes #7"


def test_issue_context_does_not_declare_complete_resolution():
    block = github.issue_block({"number": 7, "title": "Login", "body": "Reproducir"})
    assert "está relacionada con ese issue" in block
    assert "no declara su resolución completa" in block
    assert "Reproducir" in block


async def test_legacy_creator_neutralizes_summary_and_generated_evidence(monkeypatch):
    from relay import memory, night

    db = type("DB", (), {
        "conversation_usage": AsyncMock(return_value={"runs": 0, "ms": 0}),
    })()
    captured = {}

    async def finalize(repo, branch, *, title, body, body_builder):
        captured["body"] = body
        captured["generated"], draft = await body_builder("stat", "diff")
        assert draft is False
        return {"error": "simulado: no se crea una PR"}

    monkeypatch.setattr(server_conversation_jobs.git_flow, "finalize_conversation_pr", finalize)
    monkeypatch.setattr(night, "verify_repo", AsyncMock(return_value=(True, "Fixed: #8")))
    monkeypatch.setattr(memory, "describe_changes", AsyncMock(return_value="Resolves demo-org/demo#9"))
    await server_conversation_jobs._finalize_pr_bg_bound(
        db, "demo-conversation", "demo", {"repo_path": "demo-repo"}, "demo-branch",
        "Closes #7", issue_number=10)
    assert "Relacionado con #7" in captured["body"]
    assert "Relacionado con #10 (sin cierre automático)." in captured["body"]
    assert captured["generated"] == "Relacionado con #8\n\nRelacionado con demo-org/demo#9"


async def test_task_body_is_neutral_without_rewriting_validation_evidence(monkeypatch):
    sha = "a" * 40
    detail = "Pruebas aprobadas. Fixes #7"
    project = {"slug": "demo", "repo_path": "demo-repo"}
    task = {"mode": "write", "publish_allowed": True,
            "branch": "demo-branch", "state": "implemented"}
    captured = {}

    class DB:
        async def get_conversation_task(self, cid):
            return dict(task)

        async def update_conversation_task(self, cid, **fields):
            task.update(fields)
            return dict(task)

        async def set_conversation_pr(self, cid, url):
            captured["url"] = url

    async def fake_git(repo, *args):
        return sha if args[0] == "rev-parse" else "1" if args[0] == "rev-list" else ""

    async def fake_exec(repo, program, *args):
        assert program == "gh" and args[:2] == ("pr", "create")
        captured["body"] = args[args.index("--body") + 1]
        return 0, "created"

    pr = {"state": "OPEN", "headRefOid": sha, "number": 7,
          "url": "https://github.com/demo-org/demo/pull/7"}
    monkeypatch.setattr(task_pr.task_workspace, "resolved_project", AsyncMock(return_value=project))
    monkeypatch.setattr(task_pr.task_workspace, "inspect_workspace", AsyncMock())
    monkeypatch.setattr(task_pr, "find_pr", AsyncMock(side_effect=[None, pr]))
    monkeypatch.setattr(task_pr, "git", fake_git)
    monkeypatch.setattr(task_pr.git_process, "_git", AsyncMock(return_value=(0, "")))
    monkeypatch.setattr(task_pr.git_process, "_exec", fake_exec)
    monkeypatch.setattr(task_pr, "set_phase", AsyncMock(return_value=True))
    monkeypatch.setattr(task_pr.night, "verify_repo", AsyncMock(return_value=(True, detail)))
    await task_pr.publish(DB(), project, "demo-conversation")
    assert "Pruebas aprobadas. Relacionado con #7" in captured["body"]
    assert task["validation"]["detail"] == detail
    assert captured["url"] == pr["url"]
