"""Tests de los bugs críticos de timing/notify encontrados en la
auditoría del 2026-07-18.

Cubre:
  - C1: run_expert devuelve `duration_ms` siempre >= 0 (regression test
    contra el bug de unidades mezcladas ns/segundos que daba valores
    negativos gigantes y nunca disparaba el deadline).
  - C1: el deadline global SÍ dispara asyncio.TimeoutError cuando el
    experto excede `defaults_json.timeout`.
  - C2: NotifyClient.send(kind="question") no levanta ValueError
    (bug histórico: kind="question" no estaba en NOTIFY_KINDS y los
    checkpoints interactivos de iter 9.7/9.8 jamás llegaban a Discord).

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_expert_timeout.py -q
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from relay import experts
from relay.notify import NOTIFY_KINDS, NotifyClient


# ---------- C1: duration_ms positivo + deadline dispara ----------


class TestDurationMsSanity(unittest.IsolatedAsyncioTestCase):
    """Regression del bug C1 (2026-07-18)."""

    async def test_duration_ms_is_non_negative_int(self) -> None:
        """C1: duration_ms debe ser un int >= 0.

        Antes del fix, t0 era perf_counter_ns() (~1e18) y
        `int((time.monotonic() - t0) * 1000)` daba un negativo
        gigantesco (overflow de int). Este test cazaría esa
        regresión en cualquier refactor futuro del timing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            project = {
                "slug": "demo", "repo_path": tmp,
                "system_prompt": "", "mcp_servers": [],
                "defaults_json": {}, "native_tools": [],
            }
            r = await experts.run_expert(project, "hola", model_override="test")
            self.assertIsInstance(r["duration_ms"], int)
            self.assertGreaterEqual(r["duration_ms"], 0,
                f"duration_ms negativo: {r['duration_ms']}")
            # Un test TestModel responde en <1s. Si supera 60s, hay
            # un deadlock o el timeout no cortó.
            self.assertLess(r["duration_ms"], 60_000,
                f"duration_ms excesivo: {r['duration_ms']}ms")

    async def test_deadline_actually_triggers(self) -> None:
        """C1: asyncio.wait_for recibe un timeout en segundos razonable.

        El bug original: `deadline = perf_counter_ns() + timeout_seg`
        daba ~1e18, y `wait_for(timeout=...)` quedaba en ~10⁶ años.
        Verificamos que el timeout que wait_for recibe está en el orden
        de los segundos (no nanosegundos). Si está en 1e18 o más, el
        bug C1 está de vuelta.

        2026-07-20b: el tope global ya NO propaga TimeoutError — es un
        soft-cut reanudable (phase_at_end="hard_timeout", mensaje
        accionable), mismo patrón que budget_exceeded. El test verifica
        ese contrato nuevo.
        """
        captured_timeouts: list[float] = []

        async def spy_wait_for(awaitable, *, timeout=None, **kw):
            captured_timeouts.append(timeout)
            # Cerramos la coroutine entrante para no dejar el
            # warning "coroutine was never awaited".
            try:
                awaitable.close()
            except Exception:
                pass
            # Cancelamos inmediatamente para no esperar de más — la
            # intención del test es verificar el timeout, no el agente.
            raise asyncio.TimeoutError()

        with tempfile.TemporaryDirectory() as tmp:
            project = {
                "slug": "demo", "repo_path": tmp,
                "system_prompt": "", "mcp_servers": [],
                # Default 600s. El test captura ese orden de magnitud.
                "defaults_json": {}, "native_tools": [],
            }
            with patch("relay.experts.asyncio.wait_for", spy_wait_for):
                r = await experts.run_expert(
                    project, "hola", model_override="test")

            # Soft-cut: no explota, devuelve resultado reanudable.
            self.assertEqual(r["phase_at_end"], "hard_timeout")
            self.assertIn("tope global", r["content"])
            self.assertIn("continúa", r["content"])

            self.assertTrue(captured_timeouts,
                "wait_for no fue invocado")
            to = captured_timeouts[0]
            self.assertIsInstance(to, (int, float))
            # El timeout debe estar en segundos razonables (600 default,
            # floor 1.0 si deadline ya pasó). NUNCA 1e18.
            self.assertLess(to, 60 * 60 * 24,
                f"timeout parece estar en nanosegundos: {to}")
            self.assertGreater(to, 0,
                f"timeout no positivo: {to}")


# ---------- 2026-07-20b: watchdog de idle corta de verdad ----------


class _HangingRun:
    """agent_run falso: nunca emite un node (provider colgado)."""

    def all_messages(self):
        return []

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)  # cancelable — jamás produce


class _HangingIterCM:
    async def __aenter__(self):
        return _HangingRun()

    async def __aexit__(self, *exc):
        return False


class _HangingAgent:
    """Reemplaza pydantic_ai.Agent: iter() cuelga para siempre."""

    def __init__(self, *a, **kw):
        pass

    def iter(self, *a, **kw):
        return _HangingIterCM()


class TestIdleWatchdogCuts(unittest.IsolatedAsyncioTestCase):
    """Regression del bug 2026-07-20b: el watchdog hacía
    `raise CancelledError` dentro de su PROPIA task — solo se mataba a
    sí mismo y el run colgado sobrevivía hasta el tope global (600s),
    muriendo como "timeout total del experto" sin rescate de historial.
    """

    async def test_idle_watchdog_cancels_and_returns_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = {
                "slug": "demo", "repo_path": tmp,
                "system_prompt": "", "mcp_servers": [],
                "native_tools": [],
                # timeout total chico: si el watchdog vuelve a romperse,
                # el test falla a los ~8s en vez de colgar la suite.
                "defaults_json": {"timeout": 8, "idle_timeout_s": 0.5},
            }
            with patch("relay.experts.Agent", _HangingAgent):
                r = await experts.run_expert(
                    project, "hola", model_override="test")
            # Soft-cut por idle: resultado reanudable, no excepción.
            self.assertEqual(r["phase_at_end"], "idle_timeout",
                f"esperaba corte por idle, phase={r['phase_at_end']}")
            self.assertIn("idle", r["content"])
            self.assertIn("continúa", r["content"])
            # Historial parcial rescatado (acá vacío, pero presente).
            self.assertEqual(r["messages_json"], "[]")
            # Cortó por idle (~0.6s), no por el tope total de 8s.
            self.assertLess(r["duration_ms"], 5_000,
                "el watchdog no cortó: llegó al tope global")


class TestHeartbeatCallback(unittest.IsolatedAsyncioTestCase):
    """El latido del watchdog notifica a la UI sin fingir actividad."""

    async def test_heartbeat_notifies_without_touching_liveness(self) -> None:
        sent: list[dict] = []

        class _FakeNotify:
            async def send(self, **kw):
                sent.append(kw)
                return True

        store: dict = {}
        cb = experts.make_progress_callback(
            store=store, notify=_FakeNotify(),
            chat_id="c1", target="demo", model="test")
        rp = store["c1"]
        rp.phase = "tool_call"
        before = rp.last_activity_at

        await cb(phase="heartbeat", tool=None)

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["kind"], "progress")
        self.assertTrue(sent[0]["metadata"]["heartbeat"])
        self.assertEqual(rp.phase, "tool_call",
            "heartbeat no debe pisar la fase real")
        self.assertEqual(rp.last_activity_at, before,
            "heartbeat no debe refrescar last_activity_at (mentiría idle_s)")


# ---------- C2: kind="question" soportado ----------


class TestNotifyQuestionKind(unittest.IsolatedAsyncioTestCase):
    """Regression del bug C2 (2026-07-18)."""

    def test_question_in_notify_kinds(self) -> None:
        """C2: 'question' está en NOTIFY_KINDS.

        Antes del fix, NotifyClient.send(kind="question") levantaba
        ValueError y night.py lo tragaba como best-effort → los
        checkpoints interactivos jamás llegaban a Discord.
        """
        self.assertIn("question", NOTIFY_KINDS)

    async def test_send_question_does_not_raise(self) -> None:
        """C2: send(kind='question') no levanta ValueError antes de
        tocar la red (la request httpx se mockea para no pegar al bot).
        """
        client = NotifyClient(base_url="http://127.0.0.1:9999")
        async def fake_post(*a, **kw):
            class _R:
                status_code = 200
                def raise_for_status(self):
                    pass
                # `json()` porque desde 2026-09-04 `send` lee el cuerpo
                # para detectar el `{"discarded": true}` que el bot
                # contesta CON status 200. Un doble sin `json()` no es
                # una httpx.Response: dejarlo así probaba el fallback de
                # error en vez del camino feliz que este test dice medir.
                def json(self):
                    return {"ok": True}
            return _R()
        with patch.object(client._client, "post", fake_post):
            # Antes del fix: ValueError. Después: True (2xx).
            ok = await client.send(
                agent_id="night:abc", kind="question",
                message="test checkpoint",
                metadata={"q_id": "q-1"})
            self.assertTrue(ok)
        await client.aclose()


if __name__ == "__main__":
    unittest.main()