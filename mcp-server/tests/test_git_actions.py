"""Tests del visor de diff navegable y sus acciones git (2026-08-24).

Cover:
  - git_diff.diff_file_list: numstat por archivo, badge de pendiente,
    untracked aparte, rename detectado, modos all/committed/pending.
  - git_diff.diff_file: diff de UN archivo, untracked sintetizado,
    binario marcado, path fuera del repo rechazado.
  - guards de las acciones: commit con HEAD en otra rama, push a rama
    protegida, restore fuera del repo, merge a una base que no es develop.
  - endpoints: `?view=files`, `?path=`, POST /conversations/{id}/git/{action}.

Los tests que necesitarían `gh` (pr/merge felices) NO están: en esta
máquina y en CI `gh` puede no existir ni estar logueado. Lo que sí se
prueba es que los GUARDS rechazan antes de llegar a `gh` — que es la
parte que importa (un merge a main no se arregla con un revert).

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_git_actions.py -q
"""
from __future__ import annotations
from relay import git_actions, git_conversations, git_diff, git_process

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from relay import git_flow
from relay.db import Database

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR",
             "FOURBIS_COMPACTOR_MODEL", "FOURBIS_MODEL")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    remote = repo.parent / f"{repo.name}.origin.git"
    _git(repo, "init", "-b", "develop")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    (repo / "viejo.py").write_text("print(1)\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    _git(repo, "init", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "develop")


# =====================================================================
# Parsers de la salida -z de git (unit, sin repo)
# =====================================================================

class TestParsersZ(unittest.TestCase):
    def test_numstat_normal_y_rename(self) -> None:
        raw = ("3\t1\tsrc/a.py\0"
               "10\t0\t\0src/viejo.py\0src/nuevo.py\0"
               "-\t-\timg/logo.png\0")
        out = git_diff._parse_numstat_z(raw)
        self.assertEqual(out["src/a.py"], (3, 1, ""))
        self.assertEqual(out["src/nuevo.py"], (10, 0, "src/viejo.py"))
        # binario: numstat pone `-`, lo marcamos con -1
        self.assertEqual(out["img/logo.png"], (-1, -1, ""))
        self.assertNotIn("src/viejo.py", out)

    def test_name_status_normal_y_rename(self) -> None:
        raw = "M\0src/a.py\0R096\0src/viejo.py\0src/nuevo.py\0A\0src/b.py\0"
        out = git_diff._parse_name_status_z(raw)
        self.assertEqual(out, {"src/a.py": "M", "src/nuevo.py": "R",
                               "src/b.py": "A"})

    def test_paths_con_acentos_no_se_citan(self) -> None:
        """El motivo de usar -z: sin él git devuelve `"src/a\\303\\261o.py"`
        y ese path no existe cuando se lo pasás de vuelta a `git diff --`."""
        out = git_diff._parse_numstat_z("1\t0\tsrc/año.py\0")
        self.assertIn("src/año.py", out)


class TestRelInside(unittest.TestCase):
    def test_rechaza_lo_que_sale_del_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(git_diff._rel_inside(tmp, "src/a.py"), "src/a.py")
            self.assertEqual(git_diff._rel_inside(tmp, "src\\a.py"), "src/a.py")
            for malo in ("../../.ssh/id_rsa", "/etc/passwd", "C:/Windows/x",
                         "", "   "):
                self.assertIsNone(git_diff._rel_inside(tmp, malo), malo)


# =====================================================================
# Lista de archivos y diff por archivo (repo real)
# =====================================================================

class TestDiffPorArchivo(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _init_repo(self.repo)
        self.branch = await git_conversations.open_conversation_branch(
            str(self.repo), "usuario-demo-a")
        # Un commit en la rama + cambios sin commitear + un untracked.
        (self.repo / "hecho.txt").write_text("commiteado\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "trabajo commiteado")
        (self.repo / "README.md").write_text("# demo\npendiente\n",
                                             encoding="utf-8")
        (self.repo / "nuevo.txt").write_text("sin add\n", encoding="utf-8")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_lista_all_junta_commiteado_y_pendiente(self) -> None:
        out = await git_diff.diff_file_list(str(self.repo), self.branch)
        self.assertIsNone(out["error"])
        self.assertEqual(out["mode"], "all")
        self.assertTrue(out["is_current"])
        self.assertEqual(out["commits"], 1)
        paths = {f["path"]: f for f in out["files"]}
        self.assertEqual(set(paths), {"hecho.txt", "README.md"})
        # el badge de "sin commitear" solo en el que lo está
        self.assertTrue(paths["README.md"]["pending"])
        self.assertFalse(paths["hecho.txt"]["pending"])
        self.assertEqual(paths["hecho.txt"]["status"], "A")
        self.assertEqual(paths["README.md"]["added"], 1)
        # untracked aparte: no sale en el diff de git
        self.assertEqual(out["untracked"], ["nuevo.txt"])
        self.assertEqual(out["totals"]["files"], 2)
        # rama sin pushear
        self.assertEqual(out["remote_ahead"], -1)

    async def test_modos_committed_y_pending_separan(self) -> None:
        solo_commit = await git_diff.diff_file_list(
            str(self.repo), self.branch, mode="committed")
        self.assertEqual([f["path"] for f in solo_commit["files"]],
                         ["hecho.txt"])
        solo_pend = await git_diff.diff_file_list(
            str(self.repo), self.branch, mode="pending")
        self.assertEqual([f["path"] for f in solo_pend["files"]],
                         ["README.md"])

    async def test_rename_se_detecta_con_old_path(self) -> None:
        _git(self.repo, "mv", "viejo.py", "renombrado.py")
        _git(self.repo, "commit", "-m", "rename")
        out = await git_diff.diff_file_list(str(self.repo), self.branch,
                                            mode="committed")
        renombrado = [f for f in out["files"] if f["path"] == "renombrado.py"]
        self.assertEqual(len(renombrado), 1, out["files"])
        self.assertEqual(renombrado[0]["status"], "R")
        self.assertEqual(renombrado[0]["old_path"], "viejo.py")

    async def test_rename_necesita_los_dos_paths(self) -> None:
        """git detecta renames DESPUÉS de limitar por pathspec: pidiendo
        solo el destino, un archivo renombrado se ve como alta completa.
        Con `old_path` en el pathspec vuelve a leerse como rename."""
        _git(self.repo, "mv", "viejo.py", "renombrado.py")
        _git(self.repo, "commit", "-m", "rename")
        solo = await git_diff.diff_file(str(self.repo), self.branch,
                                        "renombrado.py")
        self.assertIn("new file", solo["diff"])
        con_origen = await git_diff.diff_file(str(self.repo), self.branch,
                                              "renombrado.py",
                                              old_path="viejo.py")
        self.assertIn("rename from viejo.py", con_origen["diff"])
        self.assertNotIn("new file", con_origen["diff"])

    async def test_rename_con_old_path_fuera_del_repo_se_ignora(self) -> None:
        """El `old` viene del cliente igual que el `path`: si apunta
        afuera se descarta en vez de llegar al pathspec."""
        out = await git_diff.diff_file(str(self.repo), self.branch,
                                       "README.md", old_path="../../secreto")
        self.assertIsNone(out["error"])

    async def test_diff_de_un_archivo_solo(self) -> None:
        out = await git_diff.diff_file(str(self.repo), self.branch, "README.md")
        self.assertIsNone(out["error"])
        self.assertIn("+pendiente", out["diff"])
        self.assertNotIn("commiteado", out["diff"])   # el otro archivo no
        self.assertFalse(out["truncated"])
        self.assertFalse(out["untracked"])

    async def test_untracked_se_sintetiza_como_altas(self) -> None:
        out = await git_diff.diff_file(str(self.repo), self.branch, "nuevo.txt")
        self.assertIsNone(out["error"])
        self.assertTrue(out["untracked"])
        self.assertIn("--- /dev/null", out["diff"])
        self.assertIn("+sin add", out["diff"])
        # y el índice del repo quedó intacto (no usamos `git add -N`)
        rc, staged = await git_process._git(str(self.repo), "diff", "--cached",
                                        "--name-only")
        self.assertEqual(staged.strip(), "")

    async def test_binario_se_marca_y_no_manda_bytes(self) -> None:
        (self.repo / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
        out = await git_diff.diff_file(str(self.repo), self.branch, "blob.bin")
        self.assertTrue(out["binary"])

    async def test_cap_por_archivo_trunca_y_avisa(self) -> None:
        (self.repo / "grande.txt").write_text("x\n" * 5000, encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "grande")
        out = await git_diff.diff_file(str(self.repo), self.branch,
                                       "grande.txt", cap=500)
        self.assertTrue(out["truncated"])
        self.assertLessEqual(len(out["diff"]), 500)
        self.assertGreater(out["full_size"], 500)

    async def test_path_fuera_del_repo_se_rechaza(self) -> None:
        out = await git_diff.diff_file(str(self.repo), self.branch,
                                       "../../.ssh/id_rsa")
        self.assertEqual(out["error"], "path fuera del repo")
        self.assertEqual(out["diff"], "")

    async def test_los_warnings_de_git_no_envenenan_el_parseo(self) -> None:
        """Regresión (medido en Windows, 2026-08-24): con `core.autocrlf`
        git escribe un `warning: … LF will be replaced by CRLF` POR archivo
        antes de la salida. `_git` junta stderr con stdout, así que esas
        líneas se comían los primeros registros del stream `-z`: el primer
        archivo del diff aparecía como binario, con status "w" y +0/−0.
        Todo lo que se parsea usa `_git_out`, que separa stderr."""
        _git(self.repo, "config", "core.autocrlf", "true")
        _git(self.repo, "config", "core.safecrlf", "warn")
        (self.repo / "crlf.txt").write_bytes(b"una\ndos\ntres\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "archivo con LF")
        (self.repo / "crlf.txt").write_bytes(b"una\nDOS\ntres\n")
        out = await git_diff.diff_file_list(str(self.repo), self.branch)
        fila = [f for f in out["files"] if f["path"] == "crlf.txt"]
        self.assertEqual(len(fila), 1, out["files"])
        self.assertFalse(fila[0]["binary"])
        self.assertIn(fila[0]["status"], ("A", "M"))
        self.assertGreater(fila[0]["added"], 0)
        uno = await git_diff.diff_file(str(self.repo), self.branch, "crlf.txt")
        self.assertTrue(uno["diff"].startswith("diff --git"), uno["diff"][:120])
        self.assertNotIn("warning:", uno["diff"])

    async def test_lista_con_rama_borrada_explica(self) -> None:
        _git(self.repo, "checkout", "develop")
        _git(self.repo, "branch", "-D", self.branch)
        out = await git_diff.diff_file_list(str(self.repo), self.branch)
        self.assertFalse(out["exists"])
        self.assertIn("ya no existe", out["error"])


# =====================================================================
# Acciones git: los guards
# =====================================================================

class TestAccionesGit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _init_repo(self.repo)
        self.branch = await git_conversations.open_conversation_branch(
            str(self.repo), "usuario-demo-a")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_commit_de_paths_selectivo(self) -> None:
        (self.repo / "uno.txt").write_text("1\n", encoding="utf-8")
        (self.repo / "dos.txt").write_text("2\n", encoding="utf-8")
        out = await git_actions.commit_paths(str(self.repo), self.branch,
                                          message="solo uno",
                                          paths=["uno.txt"])
        self.assertIsNone(out["error"])
        self.assertTrue(out["committed"])
        self.assertEqual(out["files"], 1)
        self.assertTrue(out["sha"])
        # dos.txt sigue sin trackear
        rc, uns = await git_process._git(str(self.repo), "ls-files", "--others",
                                     "--exclude-standard")
        self.assertEqual(uns.strip(), "dos.txt")

    async def test_commit_sin_mensaje_o_sin_nada_staged(self) -> None:
        sin_msg = await git_actions.commit_paths(str(self.repo), self.branch,
                                               message="   ")
        self.assertIn("mensaje", sin_msg["error"])
        vacio = await git_actions.commit_paths(str(self.repo), self.branch,
                                             message="nada")
        self.assertIn("nada staged", vacio["error"])

    async def test_commit_con_head_en_otra_rama_no_toca_nada(self) -> None:
        (self.repo / "x.txt").write_text("x\n", encoding="utf-8")
        _git(self.repo, "stash", "-u")
        _git(self.repo, "checkout", "develop")
        out = await git_actions.commit_paths(str(self.repo), self.branch,
                                          message="no deberia")
        self.assertFalse(out["committed"])
        self.assertIn("no en", out["error"])
        rc, head = await git_process._git(str(self.repo), "rev-parse",
                                      "--abbrev-ref", "HEAD")
        self.assertEqual(head.strip(), "develop")

    async def test_commit_con_path_fuera_del_repo(self) -> None:
        out = await git_actions.commit_paths(str(self.repo), self.branch,
                                          message="x",
                                          paths=["../../.ssh/id_rsa"])
        self.assertIn("fuera del repo", out["error"])

    async def test_push_rechaza_ramas_protegidas(self) -> None:
        for protegida in ("main", "master", "develop"):
            out = await git_actions.push_branch(str(self.repo), protegida)
            self.assertFalse(out["pushed"])
            self.assertIn("protegida", out["error"])

    async def test_restore_descarta_incluso_lo_staged(self) -> None:
        (self.repo / "viejo.py").write_text("print(2)\n", encoding="utf-8")
        _git(self.repo, "add", "viejo.py")          # staged
        (self.repo / "README.md").write_text("# tocado\n", encoding="utf-8")
        out = await git_actions.restore_paths(str(self.repo), self.branch,
                                           ["viejo.py"])
        self.assertIsNone(out["error"])
        self.assertEqual(out["restored"], ["viejo.py"])
        self.assertEqual((self.repo / "viejo.py").read_text(encoding="utf-8"),
                         "print(1)\n")
        # el otro archivo NO se tocó
        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"),
                         "# tocado\n")

    async def test_restore_valida_paths_y_rama(self) -> None:
        fuera = await git_actions.restore_paths(str(self.repo), self.branch,
                                             ["../x"])
        self.assertIn("fuera del repo", fuera["error"])
        vacio = await git_actions.restore_paths(str(self.repo), self.branch, [])
        self.assertIn("al menos un path", vacio["error"])
        protegida = await git_actions.restore_paths(str(self.repo), "main",
                                                  ["README.md"])
        self.assertIn("protegida", protegida["error"])

    async def test_merge_sin_pr_no_explota(self) -> None:
        """Sin `gh` (o sin PR) devuelve error, no excepción. El guard de
        base=develop vive en el mismo camino y se ejercita en el test de
        método inválido, que corta antes de tocar la red."""
        out = await git_actions.merge_pr(str(self.repo), self.branch)
        self.assertFalse(out["merged"])
        self.assertIsNotNone(out["error"])

    async def test_merge_valida_metodo_y_rama(self) -> None:
        malo = await git_actions.merge_pr(str(self.repo), self.branch,
                                        method="force-push")
        self.assertIn("inválido", malo["error"])
        protegida = await git_actions.merge_pr(str(self.repo), "main")
        self.assertIn("protegida", protegida["error"])

    async def test_pr_sin_commits_no_llama_a_gh(self) -> None:
        out = await git_actions.open_pr(str(self.repo), self.branch,
                                     title="vacío")
        self.assertIsNone(out["pr_url"])
        self.assertIn("commits propios", out["error"])


# =====================================================================
# Endpoints
# =====================================================================

class TestVisorHttp(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)
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

    async def _conv(self, client: TestClient) -> str:
        r = await client.post("/conversations", json={
            "project": "demo", "author": "usuario-demo-a"})
        self.assertEqual(r.status, 201, await r.text())
        return (await r.json())["id"]

    async def test_view_files_y_path(self) -> None:
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            conv = await self._conv(client)
            task = await self.db.get_conversation_task(conv)
            workspace = Path(task["workspace_path"])
            (workspace / "tocado.txt").write_text("hola\n", encoding="utf-8")
            _git(workspace, "add", "-A")
            _git(workspace, "commit", "-m", "trabajo")

            r = await client.get(f"/conversations/{conv}/diff?view=files")
            self.assertEqual(r.status, 200)
            body = await r.json()
            self.assertEqual([f["path"] for f in body["files"]],
                             ["tocado.txt"])
            self.assertEqual(body["totals"]["added"], 1)

            r2 = await client.get(
                f"/conversations/{conv}/diff?path=tocado.txt&context=1")
            self.assertEqual(r2.status, 200)
            self.assertIn("+hola", (await r2.json())["diff"])

            # sin query params: sigue respondiendo el payload viejo
            r3 = await client.get(f"/conversations/{conv}/diff")
            legacy = await r3.json()
            self.assertIn("pending_diff", legacy)
            self.assertIn("+hola", legacy["diff"])

    async def test_accion_commit_y_accion_desconocida(self) -> None:
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            conv = await self._conv(client)
            task = await self.db.get_conversation_task(conv)
            workspace = Path(task["workspace_path"])
            (workspace / "a.txt").write_text("a\n", encoding="utf-8")
            r = await client.post(f"/conversations/{conv}/git/commit",
                                  json={"message": "desde la UI"})
            self.assertEqual(r.status, 200, await r.text())
            body = await r.json()
            self.assertTrue(body["committed"])
            self.assertEqual(body["action"], "commit")

            r2 = await client.post(f"/conversations/{conv}/git/rebase-todo",
                                   json={})
            self.assertEqual(r2.status, 400)
            self.assertIn("desconocida", (await r2.json())["error"])

    async def test_accion_que_falla_devuelve_422_con_motivo(self) -> None:
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            conv = await self._conv(client)
            r = await client.post(f"/conversations/{conv}/git/restore",
                                  json={"paths": ["../../fuera"]})
            self.assertEqual(r.status, 422)
            self.assertIn("fuera del repo", (await r.json())["error"])


if __name__ == "__main__":
    unittest.main()
