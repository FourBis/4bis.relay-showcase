"""Tests de PUT /admin/api/config/expert-timeout (el gemelo sin cobertura
de tool-timeout, ver test_tool_timeout.py).

El contrato que importa: el valor queda en `system_config.expert_timeout_s`
como entero-string. Eso es EXACTAMENTE lo que lee la cascada de
`experts.run_expert` (`db.get_config("expert_timeout_s")`, experts.py) para
decidir a los cuántos segundos mata el run. Si alguien cambia la clave o
guarda "600.0", el override deja de aplicar en silencio: los runs vuelven
al default y nadie se entera hasta que un run largo muere a los 600s.

Cubre:
  - GET /admin/api/config/timeouts expone expert_timeout_s efectivo
  - PUT persiste en system_config con la clave/formato que lee run_expert
  - reset (value=null) borra el override → vuelve al env/default
  - validación de rango (30..3600) y de tipo (no numérico → 400)

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_expert_timeout_endpoint.py -q
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from relay import config
from relay.db import Database
from relay.notify import NotifyClient

# El .env del repo setea FOURBIS_EXPERT_TIMEOUT=300, así que el "default"
# depende de la máquina. Lo fijamos acá para que las aserciones no cambien
# según quién corra la suite.
_ENV_TIMEOUT = "600"


class TestExpertTimeoutEndpoint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        os.environ["FOURBIS_EXPERT_TIMEOUT"] = _ENV_TIMEOUT
        self.db = Database()          # el conftest ya apunta a un tmp
        await self.db.init_schema()
        await self.db.set_config("expert_timeout_s", "")  # arranca sin override

    async def asyncTearDown(self) -> None:
        os.environ.pop("FOURBIS_EXPERT_TIMEOUT", None)
        config._runtime.clear()       # el PUT hace _refresh_runtime_config

    def _app(self):
        """create_app con el notify mockeado (no queremos Discord)."""
        from relay.server import create_app

        async def fake_send(self_, *a, **kw):
            return True

        return patch.object(NotifyClient, "send", fake_send), create_app

    async def test_get_timeouts_expone_el_expert_timeout(self) -> None:
        ctx, create_app = self._app()
        with ctx:
            async with TestClient(TestServer(create_app())) as client:
                body = await (await client.get(
                    "/admin/api/config/timeouts")).json()
                self.assertIn("expert_timeout_s", body)
                self.assertEqual(body["expert_timeout_default_s"], 600.0)
                # sin override → cae al env
                self.assertEqual(body["expert_timeout_s"], 600.0)

    async def test_put_persiste_el_override(self) -> None:
        ctx, create_app = self._app()
        with ctx:
            async with TestClient(TestServer(create_app())) as client:
                r = await client.put("/admin/api/config/expert-timeout",
                                     json={"value": 900})
                self.assertEqual(r.status, 200)
                self.assertEqual((await r.json())["expert_timeout_s"], 900.0)

                # Clave y formato: lo que lee la cascada de run_expert.
                # int-string, no "900.0" — float("900.0") anda, pero el
                # día que alguien compare strings esto avisa.
                self.assertEqual(
                    await self.db.get_config("expert_timeout_s"), "900")

                r = await client.get("/admin/api/config/timeouts")
                timeouts = await r.json()
                self.assertEqual(timeouts["expert_timeout_s"], 900.0)
                self.assertEqual(timeouts["expert_timeout_default_s"], 600.0)

    async def test_reset_vuelve_al_default(self) -> None:
        ctx, create_app = self._app()
        with ctx:
            async with TestClient(TestServer(create_app())) as client:
                await client.put("/admin/api/config/expert-timeout",
                                 json={"value": 1200})
                r = await client.put("/admin/api/config/expert-timeout",
                                     json={"value": None})
                self.assertEqual(r.status, 200)
                self.assertEqual((await r.json())["expert_timeout_s"], 600.0)
                # El override queda vacío: la cascada lo trata como ausente.
                self.assertEqual(
                    await self.db.get_config("expert_timeout_s"), "")

    async def test_validacion_de_rango(self) -> None:
        ctx, create_app = self._app()
        with ctx:
            async with TestClient(TestServer(create_app())) as client:
                for malo in (29, 3601, 0, -5):
                    r = await client.put("/admin/api/config/expert-timeout",
                                         json={"value": malo})
                    self.assertEqual(r.status, 400, f"value={malo}")
                for borde in (30, 3600):
                    r = await client.put("/admin/api/config/expert-timeout",
                                         json={"value": borde})
                    self.assertEqual(r.status, 200, f"value={borde}")

    async def test_value_no_numerico_no_toca_el_override(self) -> None:
        ctx, create_app = self._app()
        with ctx:
            async with TestClient(TestServer(create_app())) as client:
                await client.put("/admin/api/config/expert-timeout",
                                 json={"value": 900})
                r = await client.put("/admin/api/config/expert-timeout",
                                     json={"value": "basura"})
                self.assertEqual(r.status, 400)
                # el override anterior sigue intacto
                self.assertEqual(
                    await self.db.get_config("expert_timeout_s"), "900")

    async def test_timeout_endpoints_reject_invalid_shapes_and_nonfinite_values(self):
        ctx, create_app = self._app()
        with ctx:
            async with TestClient(TestServer(create_app())) as client:
                for path, key, value in (
                    ("expert-timeout", "expert_timeout_s", 900),
                    ("tool-timeout", "FOURBIS_MCP_TIMEOUT", 90),
                ):
                    url = "/admin/api/config/" + path
                    assert (await client.put(url, json={"value": value})).status == 200
                    for body in ([], False, "invalid", {"value": "NaN"}, {"value": "Infinity"}):
                        assert (await client.put(url, json=body)).status == 400
                        assert await self.db.get_config(key) == str(value)


if __name__ == "__main__":
    unittest.main()
