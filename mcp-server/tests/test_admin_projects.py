"""Tests del endpoint PATCH /admin/api/projects/{slug} (ADR-014).

Cubre el contrato que necesita el form de edición para no perder claves:
el backend es REPLACE puro (no merge), entonces el caller (frontend) es
responsable de mandar la config completa. Acá verificamos que el backend
no filtre nada de lo que recibe.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_admin_projects.py -q
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from relay import admin
from relay.db import Database

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR")


class TestProjectsPatch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo",
            "repo_path": str(base), "description": "init"})

        from relay.server import create_app
        self.app = create_app()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp.cleanup()

    async def _get_project(self, slug: str) -> dict:
        r = await self.client.get(f"/admin/api/projects/{slug}")
        self.assertEqual(r.status, 200)
        return (await r.json())["project"]

    async def _patch_project(self, slug: str, body: dict) -> dict:
        r = await self.client.patch(
            f"/admin/api/projects/{slug}", json=body)
        self.assertEqual(r.status, 200)
        return (await r.json())["project"]

    async def test_patch_night_config_preserva_claves_desconocidas(self) -> None:
        """REGRESIÓN (iter 4.7): el form de edición debe mergear
        night_config con el existente antes de mandar el PATCH. Si
        manda solo los 5 inputs, el backend (que hace replace puro)
        borra cualquier clave extra. Acá verificamos el contrato
        backend: lo que entra, queda.
        """
        # 1. simular estado inicial con clave desconocida
        #    (seteada por API o feature futura — NO está en los 5 inputs).
        await self._patch_project("demo", {
            "night_config": {
                "build_cmd": "make build",
                "test_cmd": "make test",
                "custom_feature_flag": "experimental",  # ← desconocida
                "tweak_threshold": 42,                   # ← desconocida
            },
        })
        p = await self._get_project("demo")
        self.assertEqual(p["night_config"]["custom_feature_flag"],
                         "experimental")
        self.assertEqual(p["night_config"]["tweak_threshold"], 42)

        # 2. simular lo que hace el form fixed: merge existing + overrides.
        existing = p["night_config"]
        merged = {**existing, "build_cmd": "make build-fast"}
        await self._patch_project("demo", {"night_config": merged})

        # 3. re-fetch: ambas desconocidas siguen + el override se aplicó.
        p = await self._get_project("demo")
        self.assertEqual(p["night_config"]["build_cmd"], "make build-fast")
        self.assertEqual(p["night_config"]["custom_feature_flag"],
                         "experimental")
        self.assertEqual(p["night_config"]["tweak_threshold"], 42)
        self.assertEqual(p["night_config"]["test_cmd"], "make test")

    async def test_patch_night_config_truncado_borra_extras(self) -> None:
        """Contrato del bug original: si el caller manda SOLO los 5
        inputs del form (sin mergear), el backend hace replace y
        BORRA las claves extra. Este test documenta el riesgo y
        sirve de red de seguridad: si alguien rompe el merge en el
        frontend, este test sigue verde (es backend puro), pero el
        contrato de UI queda explícito en el otro test.
        """
        await self._patch_project("demo", {
            "night_config": {
                "build_cmd": "make build",
                "custom_feature_flag": "experimental",
            },
        })
        # caller olvida mergear → solo manda los 5 inputs
        await self._patch_project("demo", {
            "night_config": {"build_cmd": "make build-fast"},
        })
        p = await self._get_project("demo")
        self.assertEqual(p["night_config"]["build_cmd"], "make build-fast")
        self.assertNotIn("custom_feature_flag", p["night_config"])

    async def test_patch_secuencial_no_corrompe_estado(self) -> None:
        """REGRESIÓN (iter 4.7): dos PATCHes seguidos en la misma
        sesión deben aplicar ambos. Antes, el botón #edit-save quedaba
        disabled=true tras el primer éxito y el segundo click no
        disparaba nada (ver tab-projects.js → wireEditForm). Este
        test no prueba el bug de UI directamente (eso requiere
        Playwright); prueba que el backend acepta N PATCHes seguidos
        sin corrupcion — base mínima del fix.
        """
        await self._patch_project("demo", {"description": "save 1"})
        await self._patch_project("demo", {"description": "save 2"})
        await self._patch_project("demo", {"description": "save 3"})

        p = await self._get_project("demo")
        self.assertEqual(p["description"], "save 3")
        self.assertEqual(p["name"], "Demo")  # otros campos intactos

    async def test_patch_rechaza_night_config_no_dict(self) -> None:
        await self._patch_project("demo", {
            "night_config": {"build_cmd": "x"},
        })
        # string en lugar de dict → backend ignora silenciosamente
        # (mismo trato que el resto de campos con type mismatch).
        await self._patch_project("demo", {"night_config": "not a dict"})
        p = await self._get_project("demo")
        self.assertEqual(p["night_config"]["build_cmd"], "x")

    async def test_patch_descripcion_vacia_se_trata_como_string(self) -> None:
        """El backend acepta string vacío (no es None). El form manda
        `.trim()` así que '' llega como '' — esto verifica que el
        path de string vacío no se rompe con la whitelist."""
        await self._patch_project("demo", {"description": ""})
        p = await self._get_project("demo")
        self.assertEqual(p["description"], "")


class TestCbmIndexedCount(unittest.TestCase):
    """`_cbm_indexed_count` reemplaza un spawn de cbm en /admin/api/health.

    Lo que puede romper es el filtro: si cuenta de más (los -wal/-shm que
    SQLite deja al lado de cada store, o el `_config.db` que es de cbm y
    no un proyecto) el numero deja de coincidir con `list_projects` y el
    badge de la UI miente.
    """

    def test_cuenta_solo_stores_de_proyecto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for name in ("repo-uno.db", "repo-dos.db", "repo-tres.db"):
                (d / name).write_bytes(b"")
            # ruido real que hay en el cache dir de cbm
            (d / "_config.db").write_bytes(b"")          # de cbm, no proyecto
            (d / "repo-uno.db-wal").write_bytes(b"")     # SQLite
            (d / "repo-uno.db-shm").write_bytes(b"")
            (d / "notas.txt").write_bytes(b"")
            with patch.dict(os.environ, {"CBM_CACHE_DIR": str(d)}):
                self.assertEqual(admin._cbm_indexed_count(), 3)

    def test_dir_inexistente_da_cero_y_no_explota(self) -> None:
        """health no puede caerse porque falte el cache dir."""
        with patch.dict(os.environ,
                        {"CBM_CACHE_DIR": "Z:/no/existe/cbm-cache"}):
            self.assertEqual(admin._cbm_indexed_count(), 0)


if __name__ == "__main__":
    unittest.main()
