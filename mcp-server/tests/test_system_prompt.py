"""Tests de Sub-ola 2.3: GET /admin/api/projects/{slug}/system-prompt.

Cubre:
  - 200 con bloques correctos cuando el proyecto existe
  - 404 cuando el slug no existe
  - assembled = bloques en orden, sin vacíos
  - stats reporta chars por bloque + total
  - proyecto sin system_prompt propio: bloque vacío, no rompe
  - replicabilidad: el assembled coincide con experts.build_instructions
    cuando NO hay git diff (sync) — si el repo no es git, debe ser igual

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_system_prompt.py -q
"""
from __future__ import annotations

import asyncio
import os
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


class TestSystemPromptEndpoint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)
        self.db = Database()
        await self.db.init_schema()
        # proyecto CON system_prompt propio, repo_path = tmp (no es git)
        await self.db.upsert_project({
            "slug": "demo",
            "name": "Demo",
            "repo_path": self._tmp.name,
            "system_prompt": "Eres el experto de demo. Sé breve.",
        })

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    async def test_404_when_slug_missing(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    "/admin/api/projects/inexistente/system-prompt")
                self.assertEqual(r.status, 404)

    async def test_returns_blocks_and_assembled(self) -> None:
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    "/admin/api/projects/demo/system-prompt")
                self.assertEqual(r.status, 200)
                body = await r.json()

                self.assertEqual(body["slug"], "demo")
                self.assertIn("blocks", body)
                self.assertIn("assembled", body)
                self.assertIn("stats", body)

                # los bloques están (git_diff salió el 28/7: ya no viaja
                # en el system prompt, es la tool `git_diff()`)
                for key in ("ponytail", "system_prompt", "skills",
                            "workspace"):
                    self.assertIn(key, body["blocks"])
                self.assertNotIn("git_diff", body["blocks"])

                # el system_prompt del proyecto llega al bloque
                self.assertIn("experto de demo",
                              body["blocks"]["system_prompt"])

                # stats: chars por bloque
                self.assertIn("total", body["stats"])
                self.assertEqual(body["stats"]["total"],
                                 len(body["assembled"]))

                # assembled = concatenación de bloques no vacíos en orden
                expected = "\n\n".join(
                    v for v in body["blocks"].values() if v)
                self.assertEqual(body["assembled"], expected)

    async def test_replicates_experts_build_instructions_when_no_git(self) -> None:
        """El assembled tiene que coincidir exactamente con
        experts.build_instructions (sync).

        Esto es el guard de "no desfasamos" — si alguien edita
        build_instructions y no actualiza el endpoint, este test falla.
        """
        from relay.server import create_app, SKILLS_KEY
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    "/admin/api/projects/demo/system-prompt")
                body = await r.json()
                # Reconstruimos con las MISMAS primitivas que usa el
                # endpoint (SkillCache real del app).
                project = await self.db.get_project("demo")
                ponytail = await experts.read_ponytail()
                skills_block = ""
                try:
                    skills_block = await app[SKILLS_KEY].get_block()
                except Exception:
                    pass
                # Sin git diff, el assembled debe ser lo que arma
                # build_instructions (sync) con los mismos inputs.
                expected = experts.build_instructions(
                    project, ponytail, skills_block)
                self.assertEqual(body["assembled"], expected)

    async def test_empty_system_prompt_does_not_break(self) -> None:
        """Si el proyecto tiene system_prompt vacío, el endpoint igual
        devuelve 200 (el bloque queda vacío, no se omite)."""
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                # limpiamos el system_prompt del demo
                p = await self.db.get_project("demo")
                p["system_prompt"] = ""
                await self.db.upsert_project(p)
                r = await client.get(
                    "/admin/api/projects/demo/system-prompt")
                self.assertEqual(r.status, 200)
                body = await r.json()
                self.assertEqual(body["blocks"]["system_prompt"], "")
                # el assembled sigue funcionando
                self.assertIsInstance(body["assembled"], str)


class TestSystemPromptGitBlock(unittest.IsolatedAsyncioTestCase):
    """Si el repo ES git, el bloque git_diff aparece no vacío."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)
        # init git en el tmp
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=self._tmp.name, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=self._tmp.name, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self._tmp.name, check=True)
        # un commit para que el repo tenga HEAD
        (Path(self._tmp.name) / "x.txt").write_text("hello\n")
        subprocess.run(["git", "add", "x.txt"], cwd=self._tmp.name, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self._tmp.name, check=True)

        db = Database()
        await db.init_schema()
        await db.upsert_project({
            "slug": "gitdemo", "name": "GitDemo",
            "repo_path": self._tmp.name,
            "system_prompt": "x",
        })

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    async def test_git_diff_fuera_del_system_prompt(self) -> None:
        """Aunque el repo SEA git, el diff no viaja en el system prompt.

        Guard del fix del 28/7: el bloque cambia en cuanto el experto
        escribe un archivo, y el system prompt es el prefijo que cachea
        el provider — un byte distinto invalida el historial entero.
        El diff sigue disponible, pero por la tool `git_diff()`.
        """
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    "/admin/api/projects/gitdemo/system-prompt")
                self.assertEqual(r.status, 200)
                body = await r.json()
                self.assertNotIn("git_diff", body["blocks"])
                self.assertNotIn("Branch:", body["assembled"])
        # el bloque en sí sigue vivo — solo cambió quién lo pide
        block = experts._build_git_diff_block_sync(self._tmp.name)
        self.assertIn("Branch:", block)
        self.assertIn("HEAD:", block)


if __name__ == "__main__":
    unittest.main()