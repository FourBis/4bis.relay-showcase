from __future__ import annotations

import asyncio
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from relay import admin_workspace, coordination, task_workspace, github_credentials
from relay.app_state import DB_KEY
from relay.db import Database
from relay.task_workspace import (
    TaskWorkspaceError,
    initialize_task,
    inspect_workspace,
    resolved_project,
)


@pytest.fixture(autouse=True)
def explicit_github_account(monkeypatch):
    async def fake_account(provider):
        assert provider == "github"
        return {"access_token": "test-token", "subject": "123", "login": "test-user"}
    monkeypatch.setattr(github_credentials.user_accounts, "require_account", fake_account)


def test_run_git_normaliza_timeout_sin_filtrar_comando(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(
                ["git", "fetch", "https://token-secreto@example.test/repo"],
                60, stderr="clave-secreta")))

    with pytest.raises(TaskWorkspaceError) as caught:
        task_workspace._run_git(tmp_path, "fetch", "origin")

    message = str(caught.value)
    assert "60 segundos" in message
    assert "token-secreto" not in message
    assert "clave-secreta" not in message


def git(repo: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and p.returncode:
        raise AssertionError(p.stderr or p.stdout)
    return p.stdout.strip() if p.returncode == 0 else ""


class FakeDb:
    def __init__(self, path: Path):
        self.path = path
        self.tasks: dict[str, dict] = {}
        self.conversations: dict[str, dict] = {}

    async def get_conversation_task(self, conv_id):
        return dict(self.tasks.get(conv_id) or {})

    async def update_conversation_task(self, conv_id, **fields):
        self.tasks.setdefault(conv_id, {}).update(fields)
        return dict(self.tasks[conv_id])

    async def get_conversation(self, conv_id):
        return self.conversations.get(conv_id, {"id": conv_id, "pr_url": None})

    async def list_conversation_events(self, conv_id, **kwargs):
        return []


@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    source = tmp_path / "source"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   capture_output=True)
    subprocess.run(["git", "init", "-b", "develop", str(seed)], check=True,
                   capture_output=True)
    git(seed, "config", "user.email", "relay@example.test")
    git(seed, "config", "user.name", "Relay Test")
    (seed / "same.txt").write_text("base\n", encoding="utf-8")
    git(seed, "add", "same.txt")
    git(seed, "commit", "-m", "base")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "develop")
    subprocess.run(["git", "clone", "--branch", "develop", str(remote),
                    str(source)], check=True, capture_output=True)
    git(source, "config", "user.email", "relay@example.test")
    git(source, "config", "user.name", "Relay Test")
    return source, remote


def ctx(tmp_path, repo):
    source, _ = repo
    db = FakeDb(tmp_path / "state" / "relay.db")
    db.path.parent.mkdir(parents=True)
    return db, {"slug": "demo", "repo_path": str(source),
                "defaults_json": {}}


@pytest.mark.asyncio
async def test_dos_tareas_aislan_el_mismo_archivo(tmp_path, repo):
    db, project = ctx(tmp_path, repo)
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    a = await initialize_task(db, project, first)
    b = await initialize_task(db, project, second)

    Path(a["workspace_path"], "same.txt").write_text("uno\n", encoding="utf-8")
    Path(b["workspace_path"], "same.txt").write_text("dos\n", encoding="utf-8")

    assert Path(project["repo_path"], "same.txt").read_text() == "base\n"
    assert Path(a["workspace_path"], "same.txt").read_text() == "uno\n"
    assert Path(b["workspace_path"], "same.txt").read_text() == "dos\n"


@pytest.mark.asyncio
async def test_metadata_previa_no_se_confunde_con_provision(tmp_path, repo):
    db, project = ctx(tmp_path, repo)
    conv_id = str(uuid.uuid4())
    db.tasks[conv_id] = {
        "role": "worker", "publish_allowed": False,
        "tracking": {"enabled": False},
    }

    task = await initialize_task(db, project, conv_id)

    assert task["state"] == "ready"
    assert task["mode"] == "write"
    assert task["role"] == "worker"
    assert Path(task["workspace_path"]).is_dir()


@pytest.mark.asyncio
async def test_initialize_concurrente_aprovisiona_una_vez(tmp_path, repo):
    db, project = ctx(tmp_path, repo)
    conv_id = str(uuid.uuid4())

    first, second = await asyncio.gather(
        initialize_task(db, project, conv_id),
        initialize_task(db, project, conv_id),
    )

    assert first["workspace_path"] == second["workspace_path"]
    assert first["branch"] == second["branch"]
    assert second["workspace_state"] == "ok"
    assert len(git(Path(project["repo_path"]), "worktree", "list", "--porcelain").split("worktree ")) == 3


@pytest.mark.asyncio
async def test_reinicio_conserva_workspace_sucio(tmp_path, repo):
    db, project = ctx(tmp_path, repo)
    conv_id = str(uuid.uuid4())
    task = await initialize_task(db, project, conv_id)
    changed = Path(task["workspace_path"], "same.txt")
    changed.write_text("pendiente\n", encoding="utf-8")

    again = await initialize_task(db, project, conv_id)

    assert again["state"] == "ready"
    assert changed.read_text() == "pendiente\n"
    assert "same.txt" in git(Path(task["workspace_path"]), "status", "--short")


@pytest.mark.asyncio
async def test_workspace_ausente_bloquea_y_no_recrea(tmp_path, repo):
    db, project = ctx(tmp_path, repo)
    conv_id = str(uuid.uuid4())
    task = await initialize_task(db, project, conv_id)
    workspace = Path(task["workspace_path"])
    shutil.rmtree(workspace)

    inspected = await inspect_workspace(db, project, conv_id)

    assert inspected["state"] == "blocked"
    assert inspected["workspace_state"] == "blocked"
    assert "ausente" in inspected["workspace_error"]
    assert not workspace.exists()
    with pytest.raises(TaskWorkspaceError, match="ausente"):
        await resolved_project(db, project, conv_id)


@pytest.mark.asyncio
async def test_identidad_de_branch_invalida_bloquea(tmp_path, repo):
    db, project = ctx(tmp_path, repo)
    conv_id = str(uuid.uuid4())
    task = await initialize_task(db, project, conv_id)
    db.tasks[conv_id]["branch"] = "codex/task-otra"

    inspected = await inspect_workspace(db, project, conv_id)

    assert inspected["state"] == "blocked"
    assert "branch" in inspected["workspace_error"]
    assert Path(task["workspace_path"]).exists()


@pytest.mark.asyncio
async def test_origin_cambiado_bloquea_y_conserva_workspace(tmp_path, repo):
    db, project = ctx(tmp_path, repo)
    source = Path(project["repo_path"])
    conv_id = str(uuid.uuid4())
    task = await initialize_task(db, project, conv_id)
    workspace = Path(task["workspace_path"])
    git(source, "remote", "set-url", "origin", str(tmp_path / "otro.git"))

    inspected = await inspect_workspace(db, project, conv_id)

    assert inspected["state"] == "blocked"
    assert inspected["workspace_state"] == "blocked"
    assert "origin" in inspected["workspace_error"]
    assert workspace.is_dir()


@pytest.mark.asyncio
async def test_head_nuevo_invalida_validacion(tmp_path, repo):
    db, project = ctx(tmp_path, repo)
    conv_id = str(uuid.uuid4())
    task = await initialize_task(db, project, conv_id)
    work = Path(task["workspace_path"])
    db.tasks[conv_id]["validation"] = {"status": "ok"}
    (work / "nuevo.txt").write_text("x", encoding="utf-8")
    git(work, "add", "nuevo.txt")
    git(work, "commit", "-m", "nuevo")

    inspected = await inspect_workspace(db, project, conv_id)

    assert inspected["state"] == "blocked"
    assert inspected["workspace_state"] == "ok"
    assert inspected["workspace_error"] == ""
    assert "HEAD cambió" in inspected["error"]
    assert inspected["validation"]["status"] == "stale"
    assert inspected["head_sha"] == git(work, "rev-parse", "HEAD")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["paused", "cancelled", "review", "finished"])
async def test_inspect_y_resolve_no_reactivan_estado(tmp_path, repo, state):
    db, project = ctx(tmp_path, repo)
    conv_id = str(uuid.uuid4())
    await initialize_task(db, project, conv_id)
    db.tasks[conv_id]["state"] = state

    inspected = await inspect_workspace(db, project, conv_id)
    resolved = await resolved_project(db, project, conv_id)

    assert inspected["state"] == state
    assert db.tasks[conv_id]["state"] == state
    assert resolved["_task_id"] == conv_id


@pytest.mark.asyncio
async def test_sucio_mismo_sha_invalida_validacion_verde(tmp_path, repo):
    db, project = ctx(tmp_path, repo)
    conv_id = str(uuid.uuid4())
    task = await initialize_task(db, project, conv_id)
    old_sha = task["head_sha"]
    db.tasks[conv_id]["state"] = "review"
    db.tasks[conv_id]["validation"] = {
        "status": "ok", "head_sha": old_sha, "detail": "12 tests",
    }
    Path(task["workspace_path"], "same.txt").write_text(
        "cambio sin commit\n", encoding="utf-8")

    inspected = await inspect_workspace(db, project, conv_id)

    assert inspected["state"] == "review"
    assert inspected["head_sha"] == old_sha
    assert inspected["validation"]["status"] == "stale"


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["dirty", "untracked", "unpushed", "missing"])
async def test_guard_develop_rechaza_sin_crear_rama_o_worktree(
    tmp_path, repo, problem,
):
    db, project = ctx(tmp_path, repo)
    source = Path(project["repo_path"])
    if problem == "dirty":
        (source / "same.txt").write_text("dirty\n", encoding="utf-8")
    elif problem == "untracked":
        (source / "untracked.txt").write_text("x", encoding="utf-8")
    elif problem == "unpushed":
        (source / "local.txt").write_text("x", encoding="utf-8")
        git(source, "add", "local.txt")
        git(source, "commit", "-m", "local only")
    else:
        git(source, "branch", "-m", "main")
    conv_id = str(uuid.uuid4())

    with pytest.raises(TaskWorkspaceError):
        await initialize_task(db, project, conv_id)

    failed = db.tasks[conv_id]
    assert failed["creation_failed"] is True
    assert failed["requested_read_only"] is False
    assert failed["state"] == failed["workspace_state"] == "blocked"
    assert failed["workspace_error"]
    assert not (db.path.parent / "task-workspaces" / conv_id).exists()
    assert not git(source, "branch", "--list", f"codex/task-{conv_id}")


@pytest.mark.asyncio
async def test_fetch_fallido_deja_diagnostico_reintentable(tmp_path, repo):
    db, project = ctx(tmp_path, repo)
    source = Path(project["repo_path"])
    git(source, "remote", "set-url", "origin", str(tmp_path / "no-existe.git"))
    conv_id = str(uuid.uuid4())

    with pytest.raises(TaskWorkspaceError, match="fetch origin"):
        await initialize_task(db, project, conv_id)

    failed = await inspect_workspace(db, project, conv_id)
    assert failed["creation_failed"] is True
    assert not failed.get("mode")
    assert "fetch origin" in failed["workspace_error"]


@pytest.mark.asyncio
async def test_fallo_inicial_puede_reintentarse_read_only(tmp_path, repo):
    db, project = ctx(tmp_path, repo)
    source = Path(project["repo_path"])
    (source / "untracked.txt").write_text("x", encoding="utf-8")
    conv_id = str(uuid.uuid4())

    with pytest.raises(TaskWorkspaceError):
        await initialize_task(db, project, conv_id)
    failed = await inspect_workspace(db, project, conv_id)
    assert failed["creation_failed"] is True and not failed.get("mode")

    task = await initialize_task(db, project, conv_id, read_only=True)

    assert task["mode"] == "read_only"
    assert task["creation_failed"] is False
    assert task["state"] == "ready"
    assert task["error"] == task["workspace_error"] == ""


@pytest.mark.asyncio
async def test_read_only_no_crea_worktree_y_aplica_policy(tmp_path, repo):
    db, project = ctx(tmp_path, repo)
    conv_id = str(uuid.uuid4())

    task = await initialize_task(db, project, conv_id, read_only=True)
    resolved = await resolved_project(db, project, conv_id)

    assert task["mode"] == "read_only"
    assert task["workspace_path"] == str(Path(project["repo_path"]).resolve())
    assert resolved["defaults_json"]["read_only"] is True
    assert resolved["_task_source_repo"] == task["source_repo"]


@pytest.mark.asyncio
async def test_promote_read_only_keeps_identity_and_creates_isolated_workspace(tmp_path, repo):
    db, project = ctx(tmp_path, repo)
    cid = str(uuid.uuid4())
    await db.update_conversation_task(cid, requested_by="dev@example.test", publish_allowed=False)
    original = await initialize_task(db, project, cid, read_only=True)
    unchanged = await initialize_task(db, project, cid)
    assert unchanged["mode"] == "read_only"

    promoted = await initialize_task(db, project, cid, promote=True)
    assert promoted["mode"] == "write"
    assert promoted["workspace_path"] != original["workspace_path"]
    assert promoted["requested_by"] == "dev@example.test"
    assert promoted["publish_allowed"] is False
    assert promoted["branch"] == f"codex/task-{cid}"
    assert not git(Path(project["repo_path"]), "status", "--porcelain")
    assert git(Path(project["repo_path"]), "branch", "--show-current") == "develop"


@pytest.mark.asyncio
async def test_promote_refuses_dirty_develop_and_preserves_read_only_task(tmp_path, repo):
    db, project = ctx(tmp_path, repo)
    cid = str(uuid.uuid4())
    before = await initialize_task(db, project, cid, read_only=True)
    Path(project["repo_path"], "same.txt").write_text("user change\n", encoding="utf-8")
    with pytest.raises(TaskWorkspaceError):
        await initialize_task(db, project, cid, promote=True)
    assert await db.get_conversation_task(cid) == before
    assert Path(project["repo_path"], "same.txt").read_text() == "user change\n"


@pytest.mark.asyncio
async def test_workspace_put_respeta_lease_y_escribe_en_root_de_tarea(
    tmp_path, repo,
):
    source, _remote = repo
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project({
        "slug": "demo", "name": "Demo", "repo_path": str(source),
        "defaults_json": {}})
    conv_id = await db.create_conversation(project_slug="demo")
    project = await db.get_project("demo")
    task = await initialize_task(db, project, conv_id)
    effective = await resolved_project(db, project, conv_id)

    app = web.Application()
    app[DB_KEY] = db
    app.router.add_put(
        "/admin/api/projects/{slug}/workspace/file",
        admin_workspace.api_workspace_file_put)
    async with TestClient(TestServer(app)) as client:
        contradictory = await client.put(
            "/admin/api/projects/demo/workspace/file"
            f"?conversation={conv_id}&conversation_id=otra",
            json={"path": "lease.txt", "content": "no\n"})
        assert contradictory.status == 400

        lease = coordination.acquire(db, effective)
        try:
            blocked = await client.put(
                "/admin/api/projects/demo/workspace/file"
                f"?conversation={conv_id}",
                json={"path": "lease.txt", "content": "bloqueado\n"})
            assert blocked.status == 409
            assert (await blocked.json())["error"] == "workspace_busy"
            assert not Path(task["workspace_path"], "lease.txt").exists()
        finally:
            lease.release()

        written = await client.put(
            "/admin/api/projects/demo/workspace/file"
            f"?conversation={conv_id}",
            json={"path": "lease.txt", "content": "tarea\n"})
        assert written.status == 200, await written.text()

    assert Path(task["workspace_path"], "lease.txt").read_text(
        encoding="utf-8") == "tarea\n"
    assert not Path(source, "lease.txt").exists()
