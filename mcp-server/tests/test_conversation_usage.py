"""Tiempo de uso de una conversación (2026-08-31).

El pedido: "la sesión está bien que tenga el tiempo total pero
preferiría el tiempo por uso, y al cerrar el PR agregar las horas
realizadas". El reloj de pared no sirve para eso — una conversación
queda abierta entre pedido y pedido, y medido sobre `50379bac` (inventorydemo)
la pared decía 16h16 contra 2h49 de trabajo real.

Cover:
  - db.conversation_usage: suma SOLO los runs de esa conversación; un
    run sin `duration_ms` (relay reiniciado a mitad) cuenta en `runs`
    pero suma 0; sin runs devuelve ceros.
  - server._horas_legibles: los saltos de segundos → minutos → horas.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_conversation_usage.py -q
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from relay.db import Database
from relay.server import _horas_legibles


class TestHorasLegibles(unittest.TestCase):
    def test_saltos(self) -> None:
        self.assertEqual(_horas_legibles(0), "0s")
        self.assertEqual(_horas_legibles(45_000), "45s")
        self.assertEqual(_horas_legibles(59_400), "59s")
        # Pasado el minuto los segundos son ruido.
        self.assertEqual(_horas_legibles(60_000), "1m")
        self.assertEqual(_horas_legibles(810_000), "13m")
        self.assertEqual(_horas_legibles(3_600_000), "1h 00m")
        # El caso real medido: 10.163.982 ms de la conversación de inventorydemo.
        self.assertEqual(_horas_legibles(10_163_982), "2h 49m")

    def test_none_no_rompe(self) -> None:
        """El body del PR se arma con esto: un None no puede tirar."""
        self.assertEqual(_horas_legibles(None), "0s")


class TestConversationUsage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(path=Path(self._tmp.name) / "test.db")
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name})

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _run_de(self, conv_id, ms):
        chat_id = await self.db.create_chat(
            project_slug="demo", source="test", author=None, target=None,
            conversation_id=conv_id)
        await self.db.finish_chat(chat_id, status="ok", duration_ms=ms)
        return chat_id

    async def test_suma_solo_los_runs_propios(self) -> None:
        a = await self.db.create_conversation(project_slug="demo")
        b = await self.db.create_conversation(project_slug="demo")
        await self._run_de(a, 60_000)
        await self._run_de(a, 90_000)
        await self._run_de(b, 500_000)

        self.assertEqual(await self.db.conversation_usage(a),
                         {"runs": 2, "ms": 150_000})
        self.assertEqual(await self.db.conversation_usage(b),
                         {"runs": 1, "ms": 500_000})

    async def test_run_sin_duracion_cuenta_pero_no_suma(self) -> None:
        """Relay reiniciado a mitad: la fila queda sin `duration_ms`.
        Preferimos subestimar las horas antes que inventarlas."""
        conv = await self.db.create_conversation(project_slug="demo")
        await self._run_de(conv, 30_000)
        await self.db.create_chat(
            project_slug="demo", source="test", author=None, target=None,
            conversation_id=conv)  # sin finish_chat → duration_ms NULL

        self.assertEqual(await self.db.conversation_usage(conv),
                         {"runs": 2, "ms": 30_000})

    async def test_sin_runs_devuelve_ceros(self) -> None:
        """Una conversación recién creada no puede romper el chip ni el
        body del PR (por eso `COALESCE`, no None)."""
        conv = await self.db.create_conversation(project_slug="demo")
        self.assertEqual(await self.db.conversation_usage(conv),
                         {"runs": 0, "ms": 0})
