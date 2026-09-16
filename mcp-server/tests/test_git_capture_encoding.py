"""El diff que se le pasa al experto se decodifica como UTF-8, no con la locale.

Bug 2026-07-21: `subprocess.run(..., text=True)` sin `encoding` usa la
locale del sistema (cp1252 en Windows). Un diff con acentos/emoji tiraba
`UnicodeDecodeError` DENTRO del reader thread de subprocess: el traceback
salía suelto en el log del relay y el diff volvía vacío, en silencio.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_git_capture_encoding.py -q
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from relay.experts import _capture_git_diff_sync


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


class TestGitCaptureEncoding(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "t@t.com")
        _git(self.repo, "config", "user.name", "Test")
        (self.repo / "a.txt").write_text("base\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "init")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_diff_con_utf8_no_rompe(self) -> None:
        # 0x8f (el byte del crash real) no existe en cp1252; llega acá
        # como parte de un emoji UTF-8, más acentos del español.
        (self.repo / "a.txt").write_text(
            "compilación ñandú — 🏗️ diseño\n", encoding="utf-8")
        out = _capture_git_diff_sync(str(self.repo))
        self.assertTrue(out["ok"], out)
        self.assertIn("compilación", out["diff"])
        self.assertIn("a.txt", out["status"])


if __name__ == "__main__":
    unittest.main()
