"""Tests del workspace (iter 4.7): GET/PUT files + scaffold LLM.

Cubre:
  - _safe_repo_path: traversal (../), repo inexistente, etc
  - GET files: lista, filtra binarios e ignored dirs, subdir, cap
  - GET file: lee texto, rechaza binario, rechaza > 1MB
  - PUT file: crea, sobrescribe (con overwrite=true), rechaza ext
    no soportada, rechaza > 64KB, valida path safety
  - POST scaffold: mockea Agent (no llama LLM real), valida paths
    rechazados, valida apply vs preview

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_workspace.py -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer

from relay.db import Database

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR")


class _FakeScaffoldFile:
    def __init__(self, path: str, content: str):
        self.path = path
        self.content = content


class _FakeScaffoldOutput:
    def __init__(self, files, rationale=""):
        self.files = [_FakeScaffoldFile(p, c) for p, c in files]
        self.rationale = rationale


class _FakeAgentResult:
    def __init__(self, output):
        self.output = output


class TestSafeRepoPath(unittest.TestCase):
    """Pruebas unitarias puras del helper (sin app/server)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rel_vacio_devuelve_base(self) -> None:
        from relay.admin import _safe_repo_path
        out = _safe_repo_path(str(self.repo), "")
        self.assertEqual(out, self.repo.resolve())

    def test_rel_normal(self) -> None:
        from relay.admin import _safe_repo_path
        (self.repo / "docs").mkdir()
        out = _safe_repo_path(str(self.repo), "docs")
        self.assertEqual(out, (self.repo / "docs").resolve())

    def test_rel_anidado(self) -> None:
        from relay.admin import _safe_repo_path
        deep = self.repo / "docs" / "sub"
        deep.mkdir(parents=True)
        out = _safe_repo_path(str(self.repo), "docs/sub/file.md")
        self.assertEqual(out, deep / "file.md")

    def test_traversal_rechazado(self) -> None:
        from relay.admin import _safe_repo_path
        out = _safe_repo_path(str(self.repo), "../etc/passwd")
        self.assertIsNone(out)

    def test_traversal_con_subdir_rechazado(self) -> None:
        from relay.admin import _safe_repo_path
        out = _safe_repo_path(str(self.repo), "docs/../../etc")
        self.assertIsNone(out)

    def test_repo_inexistente(self) -> None:
        from relay.admin import _safe_repo_path
        out = _safe_repo_path("/no/existe/aqui", "docs")
        self.assertIsNone(out)

    def test_symlink_fuera_rechazado(self) -> None:
        from relay.admin import _safe_repo_path
        # Crear un symlink que apunta afuera del repo
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        link = self.repo / "evil"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks no soportados en este FS")
        out = _safe_repo_path(str(self.repo), "evil/passwd")
        self.assertIsNone(out)

    def test_backslash_se_normaliza(self) -> None:
        from relay.admin import _safe_repo_path
        (self.repo / "docs").mkdir()
        # Windows-style path → deberia funcionar igual
        out = _safe_repo_path(str(self.repo), "docs\\sub")
        self.assertEqual(out, (self.repo / "docs" / "sub").resolve())


class TestWorkspaceApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")

        # Repo pre-poblado con archivos mezclados
        self.repo = base / "repo"
        self.repo.mkdir()
        (self.repo / "README.md").write_text("# Test", encoding="utf-8")
        (self.repo / "main.py").write_text("print('hi')", encoding="utf-8")
        (self.repo / "data.bin").write_bytes(b"\x00\x01\x02binary")
        (self.repo / ".env").write_text("FOO=bar", encoding="utf-8")
        (self.repo / ".gitignore").write_text("node_modules/", encoding="utf-8")
        # docs/
        docs = self.repo / "docs"
        docs.mkdir()
        (docs / "PLAN.md").write_text("# Plan", encoding="utf-8")
        # dirs ignorados
        for ignored in (".git", "node_modules", "__pycache__", "dist", "build"):
            d = self.repo / ignored
            d.mkdir()
            (d / "ignored.txt").write_text("skip me", encoding="utf-8")
            (d / "important.md").write_text("# still skip", encoding="utf-8")
        # Dockerfile (sin ext)
        (self.repo / "Dockerfile").write_text("FROM scratch", encoding="utf-8")

        self.db = Database()
        await self.db.init_schema()
        await self.db.set_config("FOURBIS_MODEL", "test")
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo",
            "repo_path": str(self.repo),
            "description": "for testing",
        })

        from relay.server import create_app
        self.app = create_app()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp.cleanup()

    # ---------- GET files ----------

    async def test_files_listar_raiz(self) -> None:
        r = await self.client.get("/admin/api/projects/demo/workspace/files")
        self.assertEqual(r.status, 200)
        body = await r.json()
        names = [e["name"] for e in body["entries"]]
        self.assertIn("README.md", names)
        self.assertIn("main.py", names)
        self.assertIn("Dockerfile", names)
        self.assertIn("docs", names)
        # binarios excluidos
        self.assertNotIn("data.bin", names)
        # archivos en dirs ignorados excluidos
        self.assertFalse(any("important.md" in n for n in names))
        self.assertFalse(any("ignored.txt" in n for n in names))

    async def test_files_listar_subdir(self) -> None:
        r = await self.client.get(
            "/admin/api/projects/demo/workspace/files?subdir=docs")
        self.assertEqual(r.status, 200)
        body = await r.json()
        names = [e["name"] for e in body["entries"]]
        self.assertEqual(names, ["PLAN.md"])

    async def test_files_paths_siempre_relativos_al_repo(self) -> None:
        """Bug iter 4.7: el listado devolvía paths relativos al SUBDIR,
        no al repo root, así que 'docs/PLAN.md' venía como 'PLAN.md'
        y GET ?path=PLAN.md 404'eaba. Fix: entries.path es relativo
        al repo, así el cliente puede usarlo directo sin reconstruir.
        """
        r = await self.client.get(
            "/admin/api/projects/demo/workspace/files?subdir=docs")
        body = await r.json()
        paths = [e["path"] for e in body["entries"]]
        self.assertIn("docs/PLAN.md", paths)
        self.assertNotIn("PLAN.md", paths)  # el bug viejo

        # Y el GET con ese path debe funcionar (round-trip)
        r2 = await self.client.get(
            "/admin/api/projects/demo/workspace/file?path=docs%2FPLAN.md")
        self.assertEqual(r2.status, 200)

    async def test_files_404_si_repo_no_existe(self) -> None:
        await self.db.upsert_project({
            "slug": "fantasma", "name": "F",
            "repo_path": "/no/existe/aqui"})
        r = await self.client.get(
            "/admin/api/projects/fantasma/workspace/files")
        self.assertEqual(r.status, 400)

    async def test_files_truncado_con_cap(self) -> None:
        # Crear muchos archivos para forzar el cap
        many = self.repo / "many"
        many.mkdir()
        for i in range(WORKSPACE_LIST_CAP_OVERRIDE := 600):
            (many / f"f{i:04d}.md").write_text("x", encoding="utf-8")
        r = await self.client.get(
            "/admin/api/projects/demo/workspace/files?subdir=many")
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(len(body["entries"]), 500)
        self.assertTrue(body["truncated"])

    async def test_files_404_si_subdir_no_existe(self) -> None:
        r = await self.client.get(
            "/admin/api/projects/demo/workspace/files?subdir=nope")
        self.assertEqual(r.status, 404)

    # ---------- GET file ----------

    async def test_file_read_ok(self) -> None:
        r = await self.client.get(
            "/admin/api/projects/demo/workspace/file?path=README.md")
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body["content"], "# Test")
        self.assertEqual(body["size"], 6)

    async def test_file_read_subdir(self) -> None:
        r = await self.client.get(
            "/admin/api/projects/demo/workspace/file?path=docs/PLAN.md")
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body["content"], "# Plan")

    async def test_file_read_path_traversal(self) -> None:
        r = await self.client.get(
            "/admin/api/projects/demo/workspace/file?path=../../../etc/passwd")
        self.assertEqual(r.status, 400)

    async def test_file_read_binario_415(self) -> None:
        r = await self.client.get(
            "/admin/api/projects/demo/workspace/file?path=data.bin")
        self.assertEqual(r.status, 415)

    async def test_file_read_no_existe_404(self) -> None:
        r = await self.client.get(
            "/admin/api/projects/demo/workspace/file?path=missing.md")
        self.assertEqual(r.status, 404)

    async def test_file_read_cap_excedido_413(self) -> None:
        # Archivo > 1MB
        big = self.repo / "big.md"
        big.write_bytes(b"x" * (1024 * 1024 + 10))
        r = await self.client.get(
            "/admin/api/projects/demo/workspace/file?path=big.md")
        self.assertEqual(r.status, 413)

    async def test_file_read_dockerfile_por_basename(self) -> None:
        r = await self.client.get(
            "/admin/api/projects/demo/workspace/file?path=Dockerfile")
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body["content"], "FROM scratch")

    # ---------- PUT file ----------

    async def test_file_put_crea_nuevo(self) -> None:
        r = await self.client.put(
            "/admin/api/projects/demo/workspace/file",
            json={"path": "NEW.md", "content": "hola\n"})
        self.assertEqual(r.status, 200)
        self.assertTrue((self.repo / "NEW.md").exists())
        self.assertEqual((self.repo / "NEW.md").read_text(encoding="utf-8"),
                         "hola\n")

    async def test_file_put_crea_subdir_padres(self) -> None:
        r = await self.client.put(
            "/admin/api/projects/demo/workspace/file",
            json={"path": "newdir/sub/notes.md", "content": "x"})
        self.assertEqual(r.status, 200)
        self.assertTrue((self.repo / "newdir" / "sub" / "notes.md").exists())

    async def test_file_put_existente_409_sin_overwrite(self) -> None:
        r = await self.client.put(
            "/admin/api/projects/demo/workspace/file",
            json={"path": "README.md", "content": "otro"})
        self.assertEqual(r.status, 409)
        # contenido intacto
        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"),
                         "# Test")

    async def test_file_put_existente_200_con_overwrite(self) -> None:
        r = await self.client.put(
            "/admin/api/projects/demo/workspace/file",
            json={"path": "README.md", "content": "# Updated",
                  "overwrite": True})
        self.assertEqual(r.status, 200)
        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"),
                         "# Updated")

    async def test_file_put_path_traversal_rechazado(self) -> None:
        r = await self.client.put(
            "/admin/api/projects/demo/workspace/file",
            json={"path": "../evil.md", "content": "x"})
        self.assertEqual(r.status, 400)
        # Confirmar que NUNCA se creo fuera del repo
        evil = Path(self._tmp.name).parent / "evil.md"
        self.assertFalse(evil.exists())

    async def test_file_put_ext_binaria_rechazada_415(self) -> None:
        r = await self.client.put(
            "/admin/api/projects/demo/workspace/file",
            json={"path": "x.bin", "content": "x"})
        self.assertEqual(r.status, 415)

    async def test_file_put_svg_aceptado_iter_10_4(self) -> None:
        """Iter 10.4: el tab Diagramas guarda SVGs de mermaid.
        .svg es XML/texto — debe pasar la whitelist WORKSPACE_TEXT_EXTS.
        Antes del fix daba 415 y el botón 🖼️ .svg no funcionaba."""
        svg = ('<svg id="mermaid-abc" xmlns="http://www.w3.org/2000/svg" '
               'width="100"><rect/></svg>')
        r = await self.client.put(
            "/admin/api/projects/demo/workspace/file",
            json={"path": "docs/diagrams/test.svg", "content": svg})
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body["size"], len(svg.encode("utf-8")))
        # Confirmar que el archivo quedó en disco con el contenido exacto
        saved = (self.repo / "docs" / "diagrams" / "test.svg")
        self.assertTrue(saved.exists())
        self.assertEqual(saved.read_text(encoding="utf-8"), svg)

    async def test_file_read_svg_aceptado_iter_10_4(self) -> None:
        """Iter 10.4: GET de un .svg previamente guardado también debe
        pasar la whitelist (regresión del fix)."""
        svg_dir = self.repo / "docs" / "diagrams"
        svg_dir.mkdir(parents=True)
        (svg_dir / "test.svg").write_text(
            '<svg id="x" xmlns="http://www.w3.org/2000/svg"/>',
            encoding="utf-8")
        r = await self.client.get(
            "/admin/api/projects/demo/workspace/file?path=docs/diagrams/test.svg")
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertIn("<svg", body["content"])

    async def test_file_put_cap_excedido_413(self) -> None:
        big = "x" * (64 * 1024 + 10)
        r = await self.client.put(
            "/admin/api/projects/demo/workspace/file",
            json={"path": "big.md", "content": big})
        self.assertEqual(r.status, 413)
        self.assertFalse((self.repo / "big.md").exists())

    async def test_file_put_path_vacio_400(self) -> None:
        r = await self.client.put(
            "/admin/api/projects/demo/workspace/file",
            json={"path": "", "content": "x"})
        self.assertEqual(r.status, 400)

    async def test_file_put_pisa_directorio_409(self) -> None:
        r = await self.client.put(
            "/admin/api/projects/demo/workspace/file",
            json={"path": "docs", "content": "x",
                  "overwrite": True})
        self.assertEqual(r.status, 409)

    # ---------- POST scaffold (mockeado) ----------

    async def test_scaffold_preview_no_escribe(self) -> None:
        """Sin apply=true, el scaffold devuelve los files pero NO toca disco."""
        fake_files = [
            ("README.md", "# Demo\n\nStack: TODO"),
            ("docs/PLAN.md", "# Plan\n\n- TBD"),
            ("docs/ARCHITECTURE.md", "# Arch\n\n- TBD"),
            ("docs/NOTES.md", "# Notes\n\n_(vacío)_"),
        ]
        fake_output = _FakeScaffoldOutput(
            fake_files, rationale="minimal seed")
        with patch("relay.admin.Agent") as AgentCls:
            mock_agent = MagicMock()
            mock_agent.run = AsyncMock(
                return_value=_FakeAgentResult(fake_output))
            AgentCls.return_value = mock_agent
            r = await self.client.post(
                "/admin/api/projects/demo/workspace/scaffold",
                json={"prompt": "SaaS .NET + React"})

        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(len(body["files"]), 4)
        self.assertEqual(body["rationale"], "minimal seed")
        self.assertEqual(body["applied"], False)
        self.assertEqual(body["written"], [])
        # Ningun archivo se creo en disco (apply=false)
        self.assertFalse((self.repo / "docs" / "PLAN.md").exists()
                         and (self.repo / "docs" / "PLAN.md").read_text()
                         == "# Plan\n\n- TBD")
        # El PLAN.md original (del setUp) sigue intacto
        self.assertEqual((self.repo / "docs" / "PLAN.md").read_text(),
                         "# Plan")

    async def test_scaffold_apply_escribe_a_disco(self) -> None:
        fake_files = [
            ("README.md", "# Demo\n"),
            ("docs/NEW.md", "# New\n"),
        ]
        fake_output = _FakeScaffoldOutput(fake_files, rationale="ok")
        with patch("relay.admin.Agent") as AgentCls:
            mock_agent = MagicMock()
            mock_agent.run = AsyncMock(
                return_value=_FakeAgentResult(fake_output))
            AgentCls.return_value = mock_agent
            r = await self.client.post(
                "/admin/api/projects/demo/workspace/scaffold",
                json={"prompt": "seed", "apply": True,
                      "overwrite": True})

        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body["applied"], True)
        self.assertEqual(len(body["written"]), 2)
        self.assertTrue((self.repo / "docs" / "NEW.md").exists())
        # README.md original pisado (overwrite=true explícito)
        self.assertEqual((self.repo / "README.md").read_text(), "# Demo\n")

    async def test_scaffold_rechaza_paths_malos(self) -> None:
        """El LLM puede alucinar paths con traversal o absolutos. El
        endpoint tiene que filtrarlos y reportar en `rejected`."""
        fake_files = [
            ("README.md", "ok"),
            ("../escapee.md", "no deberia escribirse"),  # traversal
            ("/etc/passwd", "tampoco"),                    # absoluto
            ("evil.exe", "binario"),                        # ext mala
        ]
        fake_output = _FakeScaffoldOutput(fake_files, rationale="mixed")
        with patch("relay.admin.Agent") as AgentCls:
            mock_agent = MagicMock()
            mock_agent.run = AsyncMock(
                return_value=_FakeAgentResult(fake_output))
            AgentCls.return_value = mock_agent
            r = await self.client.post(
                "/admin/api/projects/demo/workspace/scaffold",
                json={"prompt": "x", "apply": True})

        # README.md se acepta; los demas van a rejected
        self.assertEqual(r.status, 200)
        body = await r.json()
        accepted = [f["path"] for f in body["files"]]
        self.assertEqual(accepted, ["README.md"])
        self.assertGreaterEqual(len(body["rejected"]), 3)
        # Confirmar que NUNCA se escribio nada fuera del repo
        escapee = Path(self._tmp.name).parent / "escapee.md"
        self.assertFalse(escapee.exists())

    async def test_scaffold_prompt_vacio_400(self) -> None:
        r = await self.client.post(
            "/admin/api/projects/demo/workspace/scaffold",
            json={"prompt": ""})
        self.assertEqual(r.status, 400)

    async def test_scaffold_sin_archivos_validos_502(self) -> None:
        """Si TODOS los paths del LLM son inválidos (traversal, abs,
        binarios), el endpoint devuelve 502 con la lista de rechazados
        en `rejected`. Con 2 paths malos, ambos van a `rejected` y
        `files` queda vacío → 502."""
        # 4 paths, todos rechazables por distintos motivos:
        # ../ → traversal
        # /etc/passwd → absoluto
        # /abs.md → absoluto (también .md, pero el filtro abs gana)
        # evil.exe → extensión binaria (path sí válido)
        fake_files = [
            ("../escape.md", "x"),      # traversal
            ("/etc/passwd", "x"),        # absoluto
            ("/abs.md", "x"),            # absoluto
            ("evil.exe", "x"),           # binario (path válido)
        ]
        fake_output = _FakeScaffoldOutput(fake_files, rationale="all bad")
        with patch("relay.admin.Agent") as AgentCls:
            mock_agent = MagicMock()
            mock_agent.run = AsyncMock(
                return_value=_FakeAgentResult(fake_output))
            AgentCls.return_value = mock_agent
            r = await self.client.post(
                "/admin/api/projects/demo/workspace/scaffold",
                json={"prompt": "x"})
        # DEBUG
        self.assertEqual(r.status, 502)
        body = await r.json()
        self.assertIn("rejected", body)
        self.assertGreaterEqual(len(body["rejected"]), 4)
        # Confirmar que NO se escribio nada fuera del repo
        evil = Path(self._tmp.name).parent / "evil.md"
        self.assertFalse(evil.exists())

    async def test_scaffold_llm_falla_502(self) -> None:
        with patch("relay.admin.Agent") as AgentCls:
            mock_agent = MagicMock()
            mock_agent.run = AsyncMock(
                side_effect=RuntimeError("API timeout"))
            AgentCls.return_value = mock_agent
            r = await self.client.post(
                "/admin/api/projects/demo/workspace/scaffold",
                json={"prompt": "x"})
        self.assertEqual(r.status, 502)

    async def test_scaffold_sin_api_key_502(self) -> None:
        from relay.experts import ModelUnavailable
        with patch("relay.admin.build_model",
                   side_effect=ModelUnavailable("no key")):
            r = await self.client.post(
                "/admin/api/projects/demo/workspace/scaffold",
                json={"prompt": "x"})
        self.assertEqual(r.status, 502)


# Helper: el cap está en admin.py; importamos para usar en tests.
# (no es patch: referenciamos el valor real.)
WORKSPACE_LIST_CAP_OVERRIDE = 600


if __name__ == "__main__":
    unittest.main()
