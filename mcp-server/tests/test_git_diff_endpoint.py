"""Tests de Sub-ola 2.7: GET /admin/api/projects/{slug}/git-diff.

Cubre:
  - 200 con {ok: true, status, diff, sha, branch} en repo git
  - 200 con {ok: false, status: "not_git_repo"} en repo no git
  - 404 si el slug no existe
  - el diff matchea lo que devolvería `git diff HEAD` directamente
  - el flag truncated aparece si el diff excede DIFF_MAX_BYTES
  - el endpoint no rompe el run del experto
    (no muta el working tree)

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_git_diff_endpoint.py -q
"""
from __future__ import annotations
from relay import expert_git

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from relay import experts
from relay.db import Database
from relay.notify import NotifyClient

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR")


def _tmp_env(tmp: Path) -> None:
    os.environ["FOURBIS_DB_PATH"] = str(tmp / "test.db")
    os.environ["FOURBIS_CHATS_DIR"] = str(tmp / "chats")
    os.environ["FOURBIS_JSONL_DIR"] = str(tmp / "jsonl")


def _clear_env() -> None:
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


class TestGitDiffEndpoint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)
        # init git + commit
        subprocess.run(["git", "init", "-q"], cwd=self._tmp.name, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"],
                       cwd=self._tmp.name, check=True)
        subprocess.run(["git", "config", "user.name", "t"],
                       cwd=self._tmp.name, check=True)
        (Path(self._tmp.name) / "x.txt").write_text("hello\n")
        subprocess.run(["git", "add", "x.txt"], cwd=self._tmp.name, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=self._tmp.name, check=True)
        # ahora un cambio sin commitear
        (Path(self._tmp.name) / "x.txt").write_text("hello\nworld\n")

        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "gitdemo", "name": "GitDemo",
            "repo_path": self._tmp.name})

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    async def test_200_on_git_repo(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    "/admin/api/projects/gitdemo/git-diff")
                self.assertEqual(r.status, 200)
                body = await r.json()
                self.assertEqual(body["slug"], "gitdemo")
                self.assertTrue(body["ok"])
                self.assertIn("x.txt", body["status"])
                self.assertIn("+world", body["diff"])
                self.assertNotEqual(body["sha"], "?")
                self.assertNotEqual(body["branch"], "?")

    async def test_not_git_repo(self) -> None:
        # creo otro proyecto que NO es git
        no_git = Path(self._tmp.name + "_nogit")
        os.makedirs(no_git, exist_ok=True)
        (no_git / "z.txt").write_text("nope")
        await self.db.upsert_project({
            "slug": "nogit", "name": "NoGit", "repo_path": str(no_git)})

        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    "/admin/api/projects/nogit/git-diff")
                self.assertEqual(r.status, 200)
                body = await r.json()
                self.assertFalse(body["ok"])
                self.assertEqual(body["status"], "not_git_repo")
                # diff vacío
                self.assertEqual(body.get("diff"), "")

    async def test_404_when_slug_missing(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    "/admin/api/projects/inexistente/git-diff")
                self.assertEqual(r.status, 404)

    async def test_diff_matches_git_command(self) -> None:
        """El diff devuelto es el mismo que `git diff HEAD --no-color`."""
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    "/admin/api/projects/gitdemo/git-diff")
                body = await r.json()
                expected = subprocess.run(
                    ["git", "diff", "HEAD", "--no-color"],
                    cwd=self._tmp.name, capture_output=True, text=True,
                    check=True).stdout
                self.assertEqual(body["diff"], expected)

    async def test_does_not_mutate_working_tree(self) -> None:
        """El endpoint es read-only: no toca archivos."""
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                # antes
                before = (Path(self._tmp.name) / "x.txt").read_text()
                r = await client.get(
                    "/admin/api/projects/gitdemo/git-diff")
                self.assertEqual(r.status, 200)
                # después
                after = (Path(self._tmp.name) / "x.txt").read_text()
                self.assertEqual(before, after)


class TestGitDiffStderrSeparation(unittest.IsolatedAsyncioTestCase):
    """Bug fix Sub-ola 2.7: stdout y stderr ya NO se mezclan en `diff`.

    Antes: `(proc.stdout or "") + (proc.stderr or "")` → warnings de git
    (mensajes en stderr según locale: 'nothing to commit, working tree
    clean', warnings de permisos, etc.) se filtraban al JSON como si
    fueran parte del diff. Eso inflaba el response y rompía el render
    del modal en la UI (browser colgado, memoria acumulada).
    """

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)
        subprocess.run(["git", "init", "-q"], cwd=self._tmp.name, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"],
                       cwd=self._tmp.name, check=True)
        subprocess.run(["git", "config", "user.name", "t"],
                       cwd=self._tmp.name, check=True)
        (Path(self._tmp.name) / "a.txt").write_text("a\n")
        subprocess.run(["git", "add", "a.txt"], cwd=self._tmp.name, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=self._tmp.name, check=True)
        (Path(self._tmp.name) / "a.txt").write_text("b\n")
        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo",
            "repo_path": self._tmp.name})

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    async def test_endpoint_does_not_leak_stderr_to_diff(self) -> None:
        """El response del endpoint NO trae stderr mezclado en diff."""
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    "/admin/api/projects/demo/git-diff")
                body = await r.json()
                # diff matchea exactamente git diff HEAD stdout
                expected = subprocess.run(
                    ["git", "diff", "HEAD", "--no-color"],
                    cwd=self._tmp.name, capture_output=True,
                    text=True, check=True).stdout
                self.assertEqual(body["diff"], expected)

    async def test_debug_param_exposes_stderr(self) -> None:
        """Con ?debug=1, el response incluye stderr para diagnóstico."""
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    "/admin/api/projects/demo/git-diff?debug=1")
                body = await r.json()
                self.assertIn("stderr", body)
                # sin debug, NO está
                r2 = await client.get(
                    "/admin/api/projects/demo/git-diff")
                body2 = await r2.json()
                self.assertNotIn("stderr", body2)

    async def test_endpoint_caps_response_at_max_kb(self) -> None:
        """Si el diff > max_kb, el response se corta en frontera de línea."""
        # armamos un diff grande (>10KB)
        big = "x" * 12_000
        (Path(self._tmp.name) / "a.txt").write_text(big + "\n")
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                # cap a 8KB (8192 chars)
                r = await client.get(
                    "/admin/api/projects/demo/git-diff?max_kb=8")
                body = await r.json()
                self.assertLessEqual(len(body["diff"]), 8192)
                self.assertTrue(body["truncated"])
                self.assertGreater(body["full_size"], 8192)

    async def test_max_kb_param_validation(self) -> None:
        """max_kb inválido cae al default."""
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                # basura → default 64KB
                r = await client.get(
                    "/admin/api/projects/demo/git-diff?max_kb=basura")
                self.assertEqual(r.status, 200)


class TestGitDiffTruncation(unittest.IsolatedAsyncioTestCase):
    """Si el diff excede DIFF_MAX_BYTES (20KB), el flag truncated=true."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)
        subprocess.run(["git", "init", "-q"], cwd=self._tmp.name, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"],
                       cwd=self._tmp.name, check=True)
        subprocess.run(["git", "config", "user.name", "t"],
                       cwd=self._tmp.name, check=True)
        # commit inicial con un archivo chico
        (Path(self._tmp.name) / "small.txt").write_text("a\n")
        subprocess.run(["git", "add", "small.txt"], cwd=self._tmp.name, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=self._tmp.name, check=True)
        # cambio que excede 20KB
        big = "x" * (expert_git.DIFF_MAX_BYTES + 5000)
        (Path(self._tmp.name) / "small.txt").write_text(big + "\nb\n")

        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "big", "name": "Big", "repo_path": self._tmp.name})

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    async def test_truncated_flag_when_diff_exceeds_cap(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    "/admin/api/projects/big/git-diff")
                body = await r.json()
                self.assertTrue(body["ok"])
                self.assertTrue(body.get("truncated"))


if __name__ == "__main__":
    unittest.main()
