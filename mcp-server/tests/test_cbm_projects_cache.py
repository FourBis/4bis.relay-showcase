"""Cache TTL de `cbm list_projects` (perf 2026-07-19, refactor 2026-07-23).

Historia: el spawn del exe cbm (onefile ~270MB sin firmar) cuesta
~30s cuando hay 50+ proyectos indexados — la operación agrega trabajo
de validación que escala con el catálogo. La UI pollea health cada
15s y abre drawers de proyecto que disparan este endpoint. El cache
TTL garantiza UN fetch por ventana.

2026-07-23: el fetch dejó de ir al binario (`_cbm_cli`) y ahora lee
los sqlite directo del cache (`_read_cbm_cache_index`). Beneficio:
~30s → ~150ms. Los tests de este archivo se adaptaron al nuevo
backend pero el contrato del cache (segundo call hit, errores no se
cachean, TTL expira, invalidate) sigue valiendo — se monkey-patchea
_read_cbm_cache_index en vez de _cbm_cli.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_cbm_projects_cache.py -q
"""
from __future__ import annotations
from relay.app_state import DB_KEY

import unittest

from relay import admin, admin_cbm, admin_projects


class TestCbmProjectsCache(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Estado module-level: cada test arranca con cache frío.
        admin_cbm._cbm_projects_cache = None
        admin_cbm._cbm_projects_at = 0.0
        self.calls = 0
        self._orig = admin_cbm._read_cbm_cache_index

        def fake_index() -> dict:
            self.calls += 1
            return {"projects": [{"name": "demo", "nodes": 1}]}

        admin_cbm._read_cbm_cache_index = fake_index

    def tearDown(self) -> None:
        admin_cbm._read_cbm_cache_index = self._orig
        admin_cbm._cbm_projects_cache = None

    async def test_second_call_hits_cache(self) -> None:
        d1 = await admin_cbm._cbm_list_projects()
        d2 = await admin_cbm._cbm_list_projects()
        self.assertEqual(self.calls, 1)
        self.assertEqual(d1, d2)

    async def test_invalidate_forces_refetch(self) -> None:
        await admin_cbm._cbm_list_projects()
        admin_cbm._invalidate_cbm_projects_cache()
        await admin_cbm._cbm_list_projects()
        self.assertEqual(self.calls, 2)

    async def test_mark_job_done_invalidates(self) -> None:
        await admin_cbm._cbm_list_projects()
        admin_cbm._mark_job_done("job_x")
        await admin_cbm._cbm_list_projects()
        self.assertEqual(self.calls, 2)

    async def test_errors_not_cached(self) -> None:
        def fail_index() -> dict:
            self.calls += 1
            return {"error": "boom"}

        admin_cbm._read_cbm_cache_index = fail_index
        await admin_cbm._cbm_list_projects()
        await admin_cbm._cbm_list_projects()
        self.assertEqual(self.calls, 2)  # sin cache de errores

    async def test_ttl_expiry_refetches(self) -> None:
        await admin_cbm._cbm_list_projects()
        admin_cbm._cbm_projects_at = 0.0  # simula TTL vencido
        await admin_cbm._cbm_list_projects()
        self.assertEqual(self.calls, 2)


if __name__ == "__main__":
    unittest.main()
