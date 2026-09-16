"""Tests del auto-reindex incremental por file-watcher (Opción A).

Cover:
  - is_noise: filtra .git/node_modules/logs; deja pasar código.
  - run() degrada a no-op con CBM_AUTO_WATCH=0 o sin binario cbm.
  - integración con watchdog real: evento de archivo → debounce →
    _start_index_job(slug, repo_path); ruido solo NO dispara.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_cbm_watcher.py -q
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from relay import cbm_watcher
from relay.admin import DB_KEY  # la misma AppKey que usa el watcher
from relay.db import Database

_ENV_KEYS = ("FOURBIS_DB_PATH", "CBM_AUTO_WATCH")


class TestIsNoise(unittest.TestCase):
    def test_filters_infra_dirs_and_volatile_files(self) -> None:
        self.assertTrue(cbm_watcher.is_noise(r"C:\repo\.git\index.lock"))
        self.assertTrue(cbm_watcher.is_noise(r"C:\repo\node_modules\a\b.js"))
        self.assertTrue(cbm_watcher.is_noise(r"C:\repo\obj\Debug\x.dll"))
        self.assertTrue(cbm_watcher.is_noise(r"C:\repo\relay.err.log"))
        self.assertTrue(cbm_watcher.is_noise(r"C:\repo\state\relay.db"))
        self.assertFalse(cbm_watcher.is_noise(r"C:\repo\src\main.cs"))
        self.assertFalse(cbm_watcher.is_noise(r"C:\repo\README.md"))


class TestWatcherRun(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.repo = base / "repo"
        (self.repo / "src").mkdir(parents=True)
        self._env_backup = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": str(self.repo)})
        self.app = {DB_KEY: self.db}

    async def asyncTearDown(self) -> None:
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    async def test_flag_off_is_noop(self) -> None:
        os.environ["CBM_AUTO_WATCH"] = "0"
        await asyncio.wait_for(cbm_watcher.run(self.app), timeout=5)

    async def test_no_cbm_binary_is_noop(self) -> None:
        os.environ["CBM_AUTO_WATCH"] = "1"
        with patch("relay.experts.cbm_binary_path", return_value=None):
            await asyncio.wait_for(cbm_watcher.run(self.app), timeout=5)

    async def test_event_triggers_incremental_job_and_noise_does_not(self) -> None:
        os.environ["CBM_AUTO_WATCH"] = "1"
        calls: list[tuple[str, str]] = []

        def fake_start(slug: str, repo_path: str) -> str:
            calls.append((slug, repo_path))
            return f"job_{slug}_1"

        (self.repo / ".git").mkdir()
        with patch("relay.experts.cbm_binary_path", return_value="cbm.exe"), \
                patch("relay.admin._start_index_job", side_effect=fake_start), \
                patch("relay.admin._get_job", return_value={"status": "ok"}), \
                patch.object(cbm_watcher, "QUIET_S", 0.3), \
                patch.object(cbm_watcher, "TICK_S", 0.05):
            task = asyncio.create_task(cbm_watcher.run(self.app))
            try:
                await asyncio.sleep(0.5)  # dejar arrancar el observer
                # Solo ruido: .git y un .log no disparan reindex.
                (self.repo / ".git" / "index.lock").write_text("x")
                (self.repo / "build.log").write_text("x")
                await asyncio.sleep(1.0)
                self.assertEqual(calls, [])
                # Código de verdad → un job incremental tras el debounce.
                (self.repo / "src" / "nuevo.cs").write_text("class C {}")
                t0 = time.monotonic()
                while not calls and time.monotonic() - t0 < 10:
                    await asyncio.sleep(0.05)
                self.assertEqual(calls, [("demo", str(self.repo))])
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


if __name__ == "__main__":
    unittest.main()
