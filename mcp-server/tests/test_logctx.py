"""Correlación de logs por chat (2026-07-25).

Lo que se rompía sin esto: no había forma de preguntar "qué pasó en el
chat X". El diagnóstico de un run muerto salía de reconstruirlo a mano
desde `chats`, el `messages_json` y procesos vivos del sistema.

El test que de verdad importa es `test_filter_en_handler_ve_los_hijos`:
es el error clásico de esta feature. Un `logging.Filter` puesto en el
LOGGER root no ve los records que propagan desde los hijos, y todo el
codebase loguea contra `relay.experts`, `relay.mcp_pool`, etc. Si el
filter se instala en el lugar equivocado, la feature queda muda y no se
nota hasta que la necesitas.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_logctx.py -q
"""
from __future__ import annotations

import asyncio
import logging
import unittest

from relay import logctx


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _LogctxTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.cap = _Capture()
        self.root = logging.getLogger()
        self._prev_level = self.root.level
        self.root.setLevel(logging.INFO)
        self.root.addHandler(self.cap)
        self.addCleanup(self.root.removeHandler, self.cap)
        self.addCleanup(self.root.setLevel, self._prev_level)
        logctx.bind("", "")


class TestChatContextFilter(_LogctxTestBase):

    def test_filter_en_handler_ve_los_hijos(self) -> None:
        """El gotcha: en el handler sí, en el logger no.

        Emitimos contra un logger hijo (como hace todo el codebase) y
        exigimos que el record llegue con chat_id.
        """
        self.cap.addFilter(logctx.ChatContextFilter())
        logctx.bind("chat-abc", "workshopdemo")
        logging.getLogger("relay.experts").warning("la tool no volvió")

        self.assertEqual(len(self.cap.records), 1)
        self.assertEqual(self.cap.records[0].chat_id, "chat-abc")
        self.assertEqual(self.cap.records[0].project, "workshopdemo")

    def test_sin_bind_no_rompe_el_formato(self) -> None:
        """Un record fuera de todo run igual tiene el campo (vacío)."""
        self.cap.addFilter(logctx.ChatContextFilter())
        logging.getLogger("relay.server").info("arrancando")
        rec = self.cap.records[0]
        self.assertEqual(rec.chat_id, "")
        self.assertEqual(
            logging.Formatter("[chat=%(chat_id)s] %(message)s").format(rec),
            "[chat=] arrancando")


class TestPorQueVaEnElHandler(unittest.TestCase):
    """Documenta el gotcha en un árbol de loggers propio.

    Hermético a propósito: no toca el root. Otros tests de la suite
    instalan handlers ahí (create_app, ring buffer) y un assert negativo
    sobre el root depende del orden de ejecución — este test falló
    exactamente así antes de aislarlo.
    """

    def test_un_filter_en_el_logger_NO_ve_los_records_de_los_hijos(self) -> None:
        parent = logging.getLogger("relay_test_aislado")
        parent.propagate = False          # no contaminamos el root
        parent.setLevel(logging.INFO)
        cap = _Capture()                  # handler SIN filter
        parent.addHandler(cap)
        parent.addFilter(logctx.ChatContextFilter())   # filter en el LOGGER
        self.addCleanup(parent.removeHandler, cap)
        self.addCleanup(parent.filters.clear)

        logctx.bind("chat-abc", "p")
        self.addCleanup(logctx.bind, "", "")
        logging.getLogger("relay_test_aislado.hijo").warning("desde un hijo")

        self.assertEqual(len(cap.records), 1)
        self.assertFalse(
            hasattr(cap.records[0], "chat_id"),
            "si esto pasa a tener chat_id, logging cambió y la "
            "instalación del filter se puede simplificar")


class TestAislamientoEntreRuns(_LogctxTestBase):

    def test_dos_runs_en_paralelo_no_se_pisan(self) -> None:
        """Dos tasks concurrentes: cada línea con su propio chat.

        Es la razón de usar ContextVar y no una global.
        """
        self.cap.addFilter(logctx.ChatContextFilter())

        async def run(chat: str, delay: float) -> None:
            logctx.bind(chat, "proj")
            await asyncio.sleep(delay)          # se intercalan
            logging.getLogger("relay.experts").info("paso de %s", chat)

        async def main() -> None:
            await asyncio.gather(run("chat-A", 0.02), run("chat-B", 0.01))

        asyncio.run(main())

        got = {r.getMessage(): r.chat_id for r in self.cap.records}
        self.assertEqual(got, {
            "paso de chat-A": "chat-A",
            "paso de chat-B": "chat-B",
        })

    def test_las_tasks_hijas_heredan_el_bind(self) -> None:
        """El watchdog de idle y los callbacks corren en tasks aparte
        creadas adentro del run: tienen que salir con el mismo chat."""
        self.cap.addFilter(logctx.ChatContextFilter())

        async def main() -> None:
            logctx.bind("chat-padre", "proj")

            async def hija() -> None:
                logging.getLogger("relay.experts").warning("watchdog")

            await asyncio.create_task(hija())

        asyncio.run(main())
        self.assertEqual(self.cap.records[0].chat_id, "chat-padre")


if __name__ == "__main__":
    unittest.main()
