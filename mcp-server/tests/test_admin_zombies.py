"""Tests Iter 5.3: zombie chat handling + DELETE.

Cubrir:
  - list_zombie_chats: filtra por edad + status=running
  - delete_chat: hard delete + best-effort del .md
  - experts_cancel: si el chat figura running pero no hay proceso,
    marcarlo cancelled (zombie path) — antes daba 404
  - GET /admin/api/chats/zombies: endpoint
  - DELETE /admin/api/chats/{id}: endpoint

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_admin_zombies.py -q
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from relay.db import Database

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR")


def _tmp_env(tmp: Path) -> None:
    os.environ["FOURBIS_DB_PATH"] = str(tmp / "test.db")
    os.environ["FOURBIS_CHATS_DIR"] = str(tmp / "chats")
    os.environ["FOURBIS_JSONL_DIR"] = str(tmp / "jsonl")


def _restore_env(backup: dict) -> None:
    for k, v in backup.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------- db layer ----------


class TestListZombies(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._env_backup = {k: os.environ.get(k) for k in _ENV_KEYS}
        _tmp_env(base)
        self.db = Database()
        await self.db.init_schema()

    async def asyncTearDown(self) -> None:
        _restore_env(self._env_backup)
        self._tmp.cleanup()

    async def _insert_chat(self, cid: str, started_at: str,
                           status: str = "running") -> None:
        await self.db.run(
            "INSERT INTO chats (id, project_slug, target, started_at, "
            "status, source) VALUES (?,?,?,?,?,?)",
            (cid, "demo", "demo", started_at, status, "test"))

    async def test_old_running_is_zombie(self) -> None:
        await self._insert_chat("c1", "2020-01-01T00:00:00Z")
        z = await self.db.list_zombie_chats(older_than_s=60)
        self.assertEqual([r["id"] for r in z], ["c1"])

    async def test_recent_running_is_not_zombie(self) -> None:
        import time as _t
        now_iso = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
        await self._insert_chat("c1", now_iso)
        z = await self.db.list_zombie_chats(older_than_s=60)
        self.assertEqual(z, [])

    async def test_terminated_chat_is_not_zombie(self) -> None:
        await self._insert_chat("c1", "2020-01-01T00:00:00Z",
                                status="ok")
        z = await self.db.list_zombie_chats(older_than_s=60)
        self.assertEqual(z, [])

    async def test_zombie_filter_by_age(self) -> None:
        await self._insert_chat("old", "2020-01-01T00:00:00Z")
        await self._insert_chat("recent", "2099-01-01T00:00:00Z")
        z_old = await self.db.list_zombie_chats(older_than_s=60)
        self.assertEqual([r["id"] for r in z_old], ["old"])

    async def test_reap_al_boot_cierra_los_running_huerfanos(self) -> None:
        """2026-08-17: al boot, un `running` es de un proceso que ya murió.

        Sin esto quedaban en "En curso" para siempre — había 8 acumulados,
        el más viejo de 10 días.
        """
        from relay.server import _reap_zombie_chats

        await self._insert_chat("viejo", "2020-01-01T00:00:00Z")
        n = await _reap_zombie_chats(self.db)
        self.assertEqual(n, 1)
        chat = await self.db.get_chat("viejo")
        self.assertEqual(chat["status"], "cancelled")
        self.assertIn("zombie", chat["error"])

    async def test_reap_no_toca_los_terminados(self) -> None:
        from relay.server import _reap_zombie_chats

        await self._insert_chat("listo", "2020-01-01T00:00:00Z", status="ok")
        self.assertEqual(await _reap_zombie_chats(self.db), 0)
        self.assertEqual((await self.db.get_chat("listo"))["status"], "ok")

    async def test_reap_sin_zombies_es_cero(self) -> None:
        from relay.server import _reap_zombie_chats

        self.assertEqual(await _reap_zombie_chats(self.db), 0)

    async def test_delete_chat_removes_row(self) -> None:
        await self._insert_chat("c1", "2020-01-01T00:00:00Z")
        ok = await self.db.delete_chat("c1")
        self.assertTrue(ok)
        z = await self.db.list_zombie_chats(older_than_s=60)
        self.assertEqual(z, [])

    async def test_delete_chat_returns_false_for_missing(self) -> None:
        ok = await self.db.delete_chat("nope")
        self.assertFalse(ok)

    async def test_delete_chat_removes_md_file(self) -> None:
        # crear un .md real y asociarlo
        await self._insert_chat("c1", "2020-01-01T00:00:00Z")
        md = Path(self._tmp.name) / "report.md"
        md.write_text("reporte", encoding="utf-8")
        await self.db.run(
            "UPDATE chats SET md_path=? WHERE id=?", (str(md), "c1"))
        ok = await self.db.delete_chat("c1")
        self.assertTrue(ok)
        self.assertFalse(md.exists(),
            "delete debe borrar el .md asociado (best-effort)")


# ---------- endpoint layer: experts_cancel zombie path ----------


class TestCancelZombie(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._env_backup = {k: os.environ.get(k) for k in _ENV_KEYS}
        _tmp_env(base)
        self.db = Database()
        await self.db.init_schema()

    async def asyncTearDown(self) -> None:
        _restore_env(self._env_backup)
        self._tmp.cleanup()

    async def _zombie(self, cid: str) -> None:
        """Un zombie nacido CON el relay arriba.

        Se siembra después del boot a propósito (2026-08-17): el barrido
        de `_reap_zombie_chats` cierra los `running` que sobreviven a un
        reinicio, así que un zombie sembrado antes de `create_app()` ya
        no llega vivo al test. El caso que queda —y el que la grid del
        admin existe para atender— es el de un run que muere en sesión
        sin actualizar la DB.
        """
        await self.db.run(
            "INSERT INTO chats (id, project_slug, target, started_at, "
            "status, source) VALUES (?,?,?,?,?,?)",
            (cid, "demo", "demo", "2020-01-01T00:00:00Z", "running", "test"))

    async def test_cancel_zombie_marks_cancelled(self) -> None:
        """Iter 5.3: cancel sobre un chat running SIN proceso vivo
        (RUNNING_KEY vacío) debe marcarlo cancelled, no devolver 404."""
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            await self._zombie("c1-zombie")
            r = await client.post("/experts/cancel/c1-zombie")
            self.assertEqual(r.status, 200)
            body = await r.json()
            self.assertIn("c1-zombie", body["zombies_cleaned"])
            # DB: ya no figura running
            row = await self.db.get_chat("c1-zombie")
            self.assertEqual(row["status"], "cancelled")
            self.assertIn("zombie", row["error"])

    async def test_cancel_unknown_returns_404(self) -> None:
        """Si el chat no existe en DB NI en memoria, 404 honesto."""
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/experts/cancel/nope")
            self.assertEqual(r.status, 404)


# ---------- endpoint layer: /admin/api/chats/zombies ----------


class TestZombiesEndpoint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._env_backup = {k: os.environ.get(k) for k in _ENV_KEYS}
        _tmp_env(base)
        self.db = Database()
        await self.db.init_schema()

    async def asyncTearDown(self) -> None:
        _restore_env(self._env_backup)
        self._tmp.cleanup()

    async def _seed(self) -> None:
        """Dos zombies viejos + uno fresh + uno terminado.

        Se siembra DESPUÉS del boot (2026-08-17): `_reap_zombie_chats`
        cierra al arrancar los `running` que quedaron de un proceso
        anterior, así que sembrar antes de `create_app()` probaría un
        arreglo que en producción ya no existe. Lo que la grid atiende
        es el zombie que nace con el relay arriba.
        """
        for cid, started in [
            ("z1", "2020-01-01T00:00:00Z"),
            ("z2", "2020-06-15T12:00:00Z"),
        ]:
            await self.db.run(
                "INSERT INTO chats (id, project_slug, target, "
                "started_at, status, source) VALUES (?,?,?,?,?,?)",
                (cid, "demo", "demo", started, "running", "test"))
        import time as _t
        now_iso = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
        await self.db.run(
            "INSERT INTO chats (id, project_slug, target, started_at, "
            "status, source) VALUES (?,?,?,?,?,?)",
            ("fresh", "demo", "demo", now_iso, "running", "test"))
        await self.db.run(
            "INSERT INTO chats (id, project_slug, target, started_at, "
            "status, source) VALUES (?,?,?,?,?,?)",
            ("ok1", "demo", "demo", "2020-01-01T00:00:00Z", "ok", "test"))

    async def test_zombies_returns_only_old_running(self) -> None:
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            await self._seed()
            r = await client.get("/admin/api/chats/zombies")
            self.assertEqual(r.status, 200)
            body = await r.json()
            ids = {z["id"] for z in body["zombies"]}
            self.assertIn("z1", ids)
            self.assertIn("z2", ids)
            self.assertNotIn("fresh", ids)
            self.assertNotIn("ok1", ids)
            self.assertEqual(body["count"], 2)

    async def test_zombies_custom_age(self) -> None:
        """El threshold de edad es configurable: con threshold ENORME,
        ningún running califica (threshold_iso queda en el pasado
        lejano). Con threshold chico, sí cuentan los viejos."""
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            await self._seed()
            # Threshold de 30 años → ningún chat tiene 30 años de running
            r = await client.get(
                "/admin/api/chats/zombies?older_than_s=999999999")
            self.assertEqual(r.status, 200)
            body = await r.json()
            self.assertEqual(body["count"], 0,
                "threshold enorme = ningún running califica")
            # Con threshold chico, los zombies viejos sí cuentan
            r2 = await client.get(
                "/admin/api/chats/zombies?older_than_s=60")
            body2 = await r2.json()
            self.assertEqual(body2["count"], 2,
                "threshold chico = solo los zombies viejos")


# ---------- endpoint layer: DELETE /admin/api/chats/{id} ----------


class TestDeleteChatEndpoint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._env_backup = {k: os.environ.get(k) for k in _ENV_KEYS}
        _tmp_env(base)
        self.db = Database()
        await self.db.init_schema()
        await self.db.run(
            "INSERT INTO chats (id, project_slug, target, started_at, "
            "status, source) VALUES (?,?,?,?,?,?)",
            ("c1", "demo", "demo", "2020-01-01T00:00:00Z",
             "running", "test"))

    async def asyncTearDown(self) -> None:
        _restore_env(self._env_backup)
        self._tmp.cleanup()

    async def test_delete_ok(self) -> None:
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.delete("/admin/api/chats/c1")
            self.assertEqual(r.status, 200)
            body = await r.json()
            self.assertEqual(body["deleted"], "c1")
            # la fila ya no está
            self.assertIsNone(await self.db.get_chat("c1"))

    async def test_delete_unknown_returns_404(self) -> None:
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.delete("/admin/api/chats/nope")
            self.assertEqual(r.status, 404)