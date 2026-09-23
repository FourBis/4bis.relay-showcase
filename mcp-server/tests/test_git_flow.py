"""Tests del flujo git determinista en conversaciones (/nuevo, /cerrar).

Cover:
  - git_flow: branch_name sanitiza autor; detect_base_branch; open branch.
  - server: POST /conversations crea rama/workspace aislado desde develop,
    incluso para otra tarea del mismo repo; `/close` conserva los workspaces
    administrados y el flujo legacy abre PR best-effort.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_git_flow.py -q
"""
from __future__ import annotations
from relay import git_branches, git_conversations, git_process

import asyncio
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from relay import git_flow
from relay.db import Database

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR",
             "FOURBIS_COMPACTOR_MODEL", "FOURBIS_MODEL")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    """Repo git con un commit inicial en `main` (working tree limpio)."""
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")


class TestBranchName(unittest.TestCase):
    def test_sanitizes_author(self) -> None:
        today = time.strftime("%Y-%m-%d")
        self.assertEqual(git_branches.branch_name("Example User#123"),
                         f"example-user-123-{today}")
        self.assertEqual(git_branches.branch_name(None), f"user-{today}")
        self.assertEqual(git_branches.branch_name("   "), f"user-{today}")


class TestExecCleanup(unittest.IsolatedAsyncioTestCase):
    async def test_spawn_failure_is_an_operational_result(self) -> None:
        with patch("relay.git_process.asyncio.create_subprocess_exec",
                   AsyncMock(side_effect=FileNotFoundError("gh"))):
            rc, out = await git_process._exec(".", "gh", "pr")
        self.assertEqual(rc, 127)
        self.assertIn("spawn falló", out)

    async def test_timeout_reaps_the_process(self) -> None:
        """Un timeout no puede dejar el gh hijo como zombie."""
        blocked = asyncio.Event()

        class Proc:
            returncode = None
            killed = False
            waited = False

            async def communicate(self):
                await blocked.wait()

            def kill(self):
                self.killed = True
                self.returncode = -9

            async def wait(self):
                self.waited = True
                return self.returncode

        proc = Proc()
        with patch("relay.git_process.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            rc, _ = await git_process._exec(".", "gh", "pr", timeout=0.01)
        self.assertEqual(rc, 124)
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)

    async def test_cancellation_reaps_the_process(self) -> None:
        blocked = asyncio.Event()

        class Proc:
            returncode = None
            killed = False
            waited = False

            async def communicate(self):
                await blocked.wait()

            def kill(self):
                self.killed = True
                self.returncode = -9

            async def wait(self):
                self.waited = True
                return self.returncode

        proc = Proc()
        with patch("relay.git_process.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            task = asyncio.create_task(git_process._exec(".", "gh", "pr"))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)


class TestOpenBranch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _init_repo(self.repo)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_creates_and_checks_out_branch(self) -> None:
        self.assertTrue(await git_branches.is_git_repo(str(self.repo)))
        self.assertEqual(await git_branches.detect_base_branch(str(self.repo)), "main")
        branch = await git_conversations.open_conversation_branch(str(self.repo), "usuario-demo-a")
        self.assertEqual(branch, git_branches.branch_name("usuario-demo-a"))
        rc, out = await git_process._git(str(self.repo), "rev-parse", "--abbrev-ref", "HEAD")
        self.assertEqual(out.strip(), branch)

    async def test_base_es_develop_cuando_existe(self) -> None:
        """Bug 22/7: /nuevo ramificaba desde main aunque el PR va a develop.

        Con develop adelantado, cada /nuevo rebobinaba el working tree
        (la Admin UI volvió al diseño anterior) y el experto trabajaba
        sobre código viejo."""
        repo = str(self.repo)
        _git(self.repo, "checkout", "-b", "develop")
        (self.repo / "nuevo.txt").write_text("solo en develop\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "adelanto develop")
        _git(self.repo, "checkout", "main")

        self.assertEqual(await git_branches.work_base_branch(repo), "develop")
        # detect_base_branch NO cambia: sigue siendo el trunk (lo usa
        # _ensure_develop y el PR).
        self.assertEqual(await git_branches.detect_base_branch(repo), "main")

        branch = await git_conversations.open_conversation_branch(repo, "usuario-demo-a")
        # La rama arranca desde develop: el commit de develop está acá.
        self.assertTrue((self.repo / "nuevo.txt").exists())
        rc, out = await git_process._git(
            repo, "rev-list", "--count", f"develop..{branch}")
        self.assertEqual(out.strip(), "0")

    async def test_cada_nuevo_estrena_rama(self) -> None:
        """Dos conversaciones del mismo autor el mismo día = dos ramas."""
        repo = str(self.repo)
        first = await git_conversations.open_conversation_branch(repo, "usuario-demo-a")
        self.assertEqual(first, git_branches.branch_name("usuario-demo-a"))
        _git(self.repo, "checkout", "main")
        second = await git_conversations.open_conversation_branch(repo, "usuario-demo-a")
        self.assertEqual(second, f"{first}-2")

    async def test_respeta_la_rama_remota_del_pr_anterior(self) -> None:
        """La local se borra al abrir el PR, pero la remota sigue viva con
        su PR: un /nuevo homónimo pushearía commits de otra conversación
        al PR anterior. El nombre tiene que esquivar `origin/*` también."""
        repo = str(self.repo)
        name = git_branches.branch_name("usuario-demo-a")
        rc, sha = await git_process._git(repo, "rev-parse", "HEAD")
        _git(self.repo, "update-ref", f"refs/remotes/origin/{name}", sha.strip())
        branch = await git_conversations.open_conversation_branch(repo, "usuario-demo-a")
        self.assertEqual(branch, f"{name}-2")

    async def test_base_cae_al_trunk_sin_develop(self) -> None:
        self.assertEqual(await git_branches.work_base_branch(str(self.repo)), "main")

    async def test_untracked_no_bloquea(self) -> None:
        """2026-08-02: un archivo untracked (típico: `.claude/`, `.mcp.json`,
        que dejan las herramientas y no están en .gitignore) NO
        debe bloquear /nuevo. Branchear sobre untracked es seguro y esos
        'cambios' el usuario nunca los hizo."""
        (self.repo / ".mcp.json").write_text("{}", encoding="utf-8")
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "x").write_text("y", encoding="utf-8")
        branch = await git_conversations.open_conversation_branch(str(self.repo), "usuario-demo-a")
        self.assertEqual(branch, git_branches.branch_name("usuario-demo-a"))
        # el untracked sigue ahí: no se pierde ni se commitea al crear la rama
        self.assertTrue((self.repo / ".mcp.json").exists())

    async def test_tracked_modificado_bloquea(self) -> None:
        """Un cambio en un archivo TRACKEADO sí bloquea: branchear ahí
        mezclaría WIP ajeno con la conversación."""
        (self.repo / "README.md").write_text("# modificado\n", encoding="utf-8")
        with self.assertRaises(git_process.GitFlowError):
            await git_conversations.open_conversation_branch(str(self.repo), "usuario-demo-a")

    async def test_has_commits(self) -> None:
        self.assertTrue(await git_branches.has_commits(str(self.repo)))
        with tempfile.TemporaryDirectory() as t:
            _git(Path(t), "init", "-b", "main")  # sin commit → HEAD unborn
            self.assertFalse(await git_branches.has_commits(t))


class TestLocalExcludes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _init_repo(self.repo)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_excluye_artefactos_y_es_idempotente(self) -> None:
        repo = str(self.repo)
        (self.repo / ".mcp.json").write_text("{}", encoding="utf-8")
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "x").write_text("y", encoding="utf-8")
        # antes: git los ve como untracked
        _, before = await git_branches.working_tree_clean(repo)
        self.assertIn(".mcp.json", before)
        await git_conversations.ensure_local_excludes(repo)
        # después: git ya no los ve (ni para /nuevo ni para el add -A de /cerrar)
        clean, after = await git_branches.working_tree_clean(repo)
        self.assertTrue(clean, f"aún sucio: {after}")
        # idempotente: segunda corrida no duplica el bloque
        await git_conversations.ensure_local_excludes(repo)
        exclude = self.repo / ".git" / "info" / "exclude"
        self.assertEqual(
            exclude.read_text(encoding="utf-8").count(git_conversations._EXCLUDE_MARK), 1)

    async def test_worktree_config_se_resuelve(self) -> None:
        """En un worktree `.git` es un archivo; el remote vive en el config
        compartido del repo principal. admin_common._git_remote_url debe encontrarlo
        (antes devolvía None → panel 'sin remoto')."""
        from relay import admin_common
        _git(self.repo, "remote", "add", "origin",
             "https://github.com/AuroraDemo/demo.git")
        wt = Path(self._tmp.name).parent / (self.repo.name + "-wt")
        try:
            _git(self.repo, "worktree", "add", str(wt), "-b", "wt")
            self.assertTrue((wt / ".git").is_file())  # worktree: .git es archivo
            self.assertEqual(
                admin_common._git_remote_url(str(wt)),
                "https://github.com/AuroraDemo/demo.git")
        finally:
            if wt.exists():
                _git(self.repo, "worktree", "remove", "--force", str(wt))


class TestSyncBaseWithOrigin(unittest.IsolatedAsyncioTestCase):
    """Bug 27/7: /nuevo fetcheaba pero ramificaba desde el `develop` LOCAL,
    que quedaba días atrás → las ramas de trabajo nacían desactualizadas."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.up = base / "upstream"
        self.up.mkdir()
        _init_repo(self.up)
        _git(self.up, "checkout", "-b", "develop")
        self.repo = base / "clone"
        subprocess.run(["git", "clone", str(self.up), str(self.repo)],
                       check=True, capture_output=True, text=True)
        _git(self.repo, "config", "user.email", "t@t.com")
        _git(self.repo, "config", "user.name", "Test")
        # El remoto avanza; el clon no se enteró todavía.
        (self.up / "remoto.txt").write_text("nuevo del remoto\n", encoding="utf-8")
        _git(self.up, "add", "-A")
        _git(self.up, "commit", "-m", "avanza develop")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _sha(self, ref: str) -> str:
        rc, out = await git_process._git(str(self.repo), "rev-parse", ref)
        return out.strip()

    async def test_nuevo_ramifica_desde_el_remoto_actualizado(self) -> None:
        branch = await git_conversations.open_conversation_branch(str(self.repo), "usuario-demo-a")
        # El commit que solo estaba en el remoto está en la rama de trabajo.
        self.assertTrue((self.repo / "remoto.txt").exists())
        self.assertEqual(await self._sha("develop"),
                         await self._sha("origin/develop"))
        rc, count = await git_process._git(
            str(self.repo), "rev-list", "--count", f"origin/develop..{branch}")
        self.assertEqual(count.strip(), "0")

    async def test_divergencia_no_toca_el_local(self) -> None:
        # develop local con un commit propio + el remoto adelantado.
        _git(self.repo, "commit", "--allow-empty", "-m", "local propio")
        await git_branches.fetch_origin_safe(str(self.repo))
        before = await self._sha("develop")
        ref = await git_branches.sync_base_with_origin(str(self.repo), "develop")
        self.assertEqual(ref, "origin/develop")
        self.assertEqual(await self._sha("develop"), before)


class TestConversationDiff(unittest.IsolatedAsyncioTestCase):
    """El diff de la rama sale de git, sin gastar un turno de experto."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _init_repo(self.repo)
        self.branch = await git_conversations.open_conversation_branch(
            str(self.repo), "usuario-demo-a")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_separa_commiteado_de_pendiente(self) -> None:
        (self.repo / "hecho.txt").write_text("commiteado\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "trabajo commiteado")
        (self.repo / "README.md").write_text("# demo\npendiente\n",
                                             encoding="utf-8")
        (self.repo / "nuevo.txt").write_text("sin add\n", encoding="utf-8")

        out = await git_conversations.conversation_diff(str(self.repo), self.branch)
        self.assertIsNone(out["error"])
        self.assertTrue(out["exists"])
        self.assertTrue(out["is_current"])
        self.assertEqual(out["commits"], 1)
        # Commiteado: el archivo nuevo del commit.
        self.assertIn("hecho.txt", out["stat"])
        self.assertIn("+commiteado", out["diff"])
        # Pendiente: la edición sin commitear, y el untracked aparte.
        self.assertIn("README.md", out["pending_stat"])
        self.assertIn("+pendiente", out["pending_diff"])
        self.assertEqual(out["untracked"], ["nuevo.txt"])
        self.assertFalse(out["truncated"])

    async def test_cap_trunca_y_avisa(self) -> None:
        (self.repo / "grande.txt").write_text("x\n" * 5000, encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "archivo grande")
        out = await git_conversations.conversation_diff(str(self.repo), self.branch,
                                              cap=500)
        self.assertTrue(out["truncated"])
        self.assertLessEqual(len(out["diff"]), 500)
        self.assertGreater(out["full_size"], 500)

    async def test_rama_borrada_explica_el_caso(self) -> None:
        _git(self.repo, "checkout", "main")
        _git(self.repo, "branch", "-D", self.branch)
        out = await git_conversations.conversation_diff(str(self.repo), self.branch)
        self.assertFalse(out["exists"])
        self.assertIn("ya no existe", out["error"])


class TestCleanupBranch(unittest.IsolatedAsyncioTestCase):
    """/cerrar borra la rama local después del PR (nunca main/develop)."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _init_repo(self.repo)
        self.branch = await git_conversations.open_conversation_branch(
            str(self.repo), "usuario-demo-a")
        (self.repo / "feature.txt").write_text("nuevo\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "trabajo del experto")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_borra_la_rama_y_vuelve_a_la_base(self) -> None:
        # Sin mergear a la base: tiene que usar -D, no -d.
        out = await git_branches.cleanup_conversation_branch(
            str(self.repo), self.branch)
        self.assertTrue(out["deleted"], out["error"])
        self.assertIsNone(out["error"])
        rc, head = await git_process._git(
            str(self.repo), "rev-parse", "--abbrev-ref", "HEAD")
        self.assertEqual(head.strip(), "main")
        self.assertFalse(await git_branches._branch_exists(
            str(self.repo), self.branch))

    async def test_no_borra_ramas_protegidas(self) -> None:
        for protegida in ("main", "master", "develop"):
            out = await git_branches.cleanup_conversation_branch(
                str(self.repo), protegida)
            self.assertFalse(out["deleted"])
            self.assertIn("protegida", out["error"])
        self.assertTrue(await git_branches._branch_exists(str(self.repo), "main"))


class TestFinalizePr(unittest.IsolatedAsyncioTestCase):
    """finalize_conversation_pr commitea lo pendiente ANTES de push/PR.

    Sin remoto `origin` el push falla (esperado en CI); lo que se
    verifica es el auto-commit determinista y el estado del repo."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _init_repo(self.repo)
        self.branch = await git_conversations.open_conversation_branch(
            str(self.repo), "usuario-demo-a")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_autocommits_pending_changes(self) -> None:
        # El experto editó pero NO commiteó (el caso que rompía /cerrar).
        (self.repo / "feature.txt").write_text("nuevo\n", encoding="utf-8")
        out = await git_conversations.finalize_conversation_pr(
            str(self.repo), self.branch, title="[demo] cierre", body="b")
        self.assertTrue(out["committed"])
        # El commit quedó en la rama y el tree limpio (no bloquea /nuevo).
        clean, _ = await git_branches.working_tree_clean(str(self.repo))
        self.assertTrue(clean)
        rc, count = await git_process._git(
            str(self.repo), "rev-list", "--count", f"main..{self.branch}")
        self.assertEqual(count.strip(), "1")
        # Sin remoto: el push falla pero el error es post-commit.
        self.assertIsNotNone(out["error"])
        self.assertNotIn("sin commits propios", out["error"])

    async def test_body_builder_ve_el_diff_real(self) -> None:
        """El body del PR se redacta del diff, no del mensaje de commit."""
        (self.repo / "feature.txt").write_text("nuevo\n", encoding="utf-8")
        seen: dict = {}

        async def builder(stat: str, diff: str) -> tuple[str, bool]:
            seen["stat"], seen["diff"] = stat, diff
            return "## Qué cambió\n- agrega feature.txt", False

        await git_conversations.finalize_conversation_pr(
            str(self.repo), self.branch, title="t", body="b",
            body_builder=builder)
        self.assertIn("feature.txt", seen["stat"])
        self.assertIn("+nuevo", seen["diff"])

    async def test_no_changes_reports_no_commits(self) -> None:
        out = await git_conversations.finalize_conversation_pr(
            str(self.repo), self.branch, title="t", body="b")
        self.assertFalse(out["committed"])
        self.assertIn("sin commits propios", out["error"])

    async def test_checks_out_branch_if_head_moved(self) -> None:
        # Algo movió HEAD a main; con tree limpio, /cerrar vuelve solo.
        _git(self.repo, "checkout", "main")
        out = await git_conversations.finalize_conversation_pr(
            str(self.repo), self.branch, title="t", body="b")
        rc, head = await git_process._git(
            str(self.repo), "rev-parse", "--abbrev-ref", "HEAD")
        self.assertEqual(head.strip(), self.branch)
        self.assertIn("sin commits propios", out["error"])

    async def test_dirty_on_wrong_branch_is_clear_error(self) -> None:
        _git(self.repo, "checkout", "main")
        (self.repo / "dirty.txt").write_text("x", encoding="utf-8")
        out = await git_conversations.finalize_conversation_pr(
            str(self.repo), self.branch, title="t", body="b")
        self.assertFalse(out["committed"])
        self.assertIn("resuelve a mano", out["error"])
        # No commiteó nada en main.
        rc, head = await git_process._git(
            str(self.repo), "rev-parse", "--abbrev-ref", "HEAD")
        self.assertEqual(head.strip(), "main")


class TestConversationBranchHttp(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)
        _git(self.repo, "branch", "-m", "develop")
        self.origin = base / "origin.git"
        _git(base, "init", "--bare", str(self.origin))
        _git(self.repo, "remote", "add", "origin", str(self.origin))
        _git(self.repo, "push", "-u", "origin", "develop")
        self._env_backup = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        os.environ["FOURBIS_COMPACTOR_MODEL"] = "test"
        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": str(self.repo),
            "defaults_json": {"model": "test"}})

    async def asyncTearDown(self) -> None:
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    async def test_nuevo_creates_isolated_workspaces_for_multiple_tasks(self) -> None:
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/conversations", json={
                "project": "demo", "author": "usuario-demo-a", "discord_thread_id": "t1"})
            self.assertEqual(r.status, 201)
            body = await r.json()
            self.assertEqual(body["branch"], f"codex/task-{body['id']}")
            self.assertEqual(body["base_branch"], "develop")
            # La rama vive en el workspace de esta conversación; el checkout
            # de origen permanece en develop.
            task = await self.db.get_conversation_task(body["id"])
            workspace = Path(task["workspace_path"])
            rc, out = await git_process._git(
                str(workspace), "rev-parse", "--abbrev-ref", "HEAD")
            self.assertEqual(out.strip(), body["branch"])
            rc, out = await git_process._git(
                str(self.repo), "rev-parse", "--abbrev-ref", "HEAD")
            self.assertEqual(out.strip(), "develop")

            # Otra conversación puede trabajar a la vez en otro workspace.
            r2 = await client.post("/conversations", json={
                "project": "demo", "author": "otro"})
            self.assertEqual(r2.status, 201, await r2.text())
            second = await r2.json()
            self.assertNotEqual(second["branch"], body["branch"])
            second_task = await self.db.get_conversation_task(second["id"])
            self.assertNotEqual(second_task["workspace_path"], task["workspace_path"])

    async def test_consulta_sin_commit_local_arranca_sin_rama(self) -> None:
        """Una consulta en un repo sin commits conserva archivos sin seguimiento."""
        from relay.server import create_app
        unborn = Path(self._tmp.name) / "unborn"
        unborn.mkdir()
        _git(unborn, "init", "-b", "master")  # sin commit
        _git(unborn, "config", "user.email", "t@t.com")
        _git(unborn, "config", "user.name", "Test")
        (unborn / "local.ts").write_text("// trabajo sin commitear\n",
                                         encoding="utf-8")  # untracked
        db = Database()
        await db.upsert_project({
            "slug": "unborn-demo", "name": "Unborn", "repo_path": str(unborn),
            "defaults_json": {"model": "test"}})
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/conversations", json={
                "project": "unborn-demo", "author": "usuario-demo-a",
                "read_only": True})
            self.assertEqual(r.status, 201, await r.text())
            self.assertFalse((await r.json())["branch"])
            # el trabajo untracked NO se tocó
            self.assertTrue((unborn / "local.ts").exists())

    async def test_diff_endpoint_devuelve_el_diff_de_la_rama(self) -> None:
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/conversations", json={
                "project": "demo", "author": "usuario-demo-a"})
            body = await r.json()
            conv_id = body["id"]
            workspace = Path((await self.db.get_conversation_task(conv_id))["workspace_path"])
            # El experto edita y commitea en la rama de la conv.
            (workspace / "hecho.txt").write_text("cambio\n", encoding="utf-8")
            _git(workspace, "add", "-A")
            _git(workspace, "commit", "-m", "trabajo")
            (workspace / "hecho.txt").write_text("cambio\nsin commitear\n",
                                                  encoding="utf-8")
            rd = await client.get(f"/conversations/{conv_id}/diff")
            self.assertEqual(rd.status, 200)
            body = await rd.json()
            self.assertEqual(body["branch"], (await self.db.get_conversation_task(conv_id))["branch"])
            self.assertEqual(body["commits"], 1)
            self.assertIn("+cambio", body["diff"])
            self.assertIn("+sin commitear", body["pending_diff"])
            self.assertIsNone(body["error"])

    async def test_close_without_commits_reports_pr_error(self) -> None:
        from relay.server import create_app
        branch = "usuario-demo-legacy"
        _git(self.repo, "switch", "-c", branch)
        conv_id = await self.db.create_conversation(
            project_slug="demo", author="usuario-demo-a", branch=branch)
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            # El PR corre en background: /close responde ya mismo (antes
            # esperaba al verify y la UI cortaba a los 30s).
            rc = await client.post(f"/conversations/{conv_id}/close", json={})
            self.assertEqual(rc.status, 200)
            body = await rc.json()
            self.assertEqual(body["status"], "closed")
            self.assertEqual(body["pr"], "running")
            self.assertIsNone(body.get("pr_url"))
            # …y el error (sin commits ni remoto) llega por el endpoint de poll.
            for _ in range(100):
                pr = await (await client.get(
                    f"/conversations/{conv_id}/pr")).json()
                if pr["state"] in ("done", "error"):
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(pr["state"], "error")
            self.assertIn("sin commits propios", pr["error"])
            self.assertIsNone(pr["pr_url"])

    async def test_close_managed_task_preserves_workspace_and_branch(self) -> None:
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            created = await client.post("/conversations", json={
                "project": "demo", "author": "usuario-demo-a"})
            self.assertEqual(created.status, 201, await created.text())
            conv_id = (await created.json())["id"]
            before = await self.db.get_conversation_task(conv_id)
            workspace = Path(before["workspace_path"])
            self.assertTrue(workspace.is_dir())

            closed = await client.post(f"/conversations/{conv_id}/close", json={})
            self.assertEqual(closed.status, 200)
            body = await closed.json()
            self.assertEqual(body["pr"], "skipped")
            self.assertEqual(body["compaction"], "skipped")

            after = await self.db.get_conversation_task(conv_id)
            self.assertEqual(after["workspace_path"], before["workspace_path"])
            self.assertEqual(after["branch"], before["branch"])
            self.assertTrue(workspace.is_dir())


if __name__ == "__main__":
    unittest.main()
