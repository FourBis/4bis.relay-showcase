"""Tests del contador `messages_len` en los endpoints de conversaciones.

Bug apilado (medido 2026-09-06 sobre la conversación `c645fea5`):

  (1) La sidebar del panel admin muestra siempre "0 turnos" porque
      `GET /conversations` (listado) llama a `db.list_conversations`, que
      EXCLUYE `messages_json` a propósito (es un blob pesado) pero nunca
      derivaba un contador. El front hacía `${c.messages_len ?? 0} turnos`
      y el `?? 0` se disparaba en TODAS las conversaciones. No es que el
      contador se quedara desactualizado: directamente no existía.

      Fix: `db.list_conversations` proyecta
      `COALESCE(json_array_length(messages_json), 0) AS messages_len`
      para traer el conteo sin traer el blob. La guarda COALESCE es
      necesaria porque hay conversaciones con `messages_json IS NULL`
      (medido: 7 conversaciones en la base de ese momento) y
      `json_array_length(NULL)` devuelve NULL, no 0 — sin COALESCE el
      listado entero se cae.

  (2) Aunque el bug (1) se arregle solo, la sidebar diría "39037
      turnos" en vez de 8 para `c645fea5`, porque `GET /conversations/{id}`
      (`conversations_get`, server.py:1736) calcula
      `conv["messages_len"] = len(raw)` sobre `messages_json` como
      STRING. Eso es el largo del texto en caracteres, no la cantidad de
      mensajes del array JSON.

      Fix: el handler tiene que contar elementos del array (parseo y
      `len(...)`) en vez de medir el largo del string serializado. El
      mismo criterio que `list_conversations` aplica vía SQL: número de
      mensajes, no número de bytes.

Estos tests son la red de seguridad: si alguien revierte cualquiera de
los dos arreglos, el assert correspondiente cae. La semántica de
`messages_len` tiene que ser la misma en los dos endpoints.

Cómo correr:
    cd mcp-server
    ./.venv/Scripts/python.exe -m pytest tests/test_conversations_messages_len.py -q
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from relay.db import Database
from relay.notify import NotifyClient

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR")


def _tmp_env(tmp: Path) -> None:
    os.environ["FOURBIS_DB_PATH"] = str(tmp / "test.db")
    os.environ["FOURBIS_CHATS_DIR"] = str(tmp / "chats")
    os.environ["FOURBIS_JSONL_DIR"] = str(tmp / "jsonl")


def _clear_env() -> None:
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


def _history_with_n_messages(n: int, payload_chars: int = 0) -> str:
    """Arma un `messages_json` con N mensajes estilo pydantic-ai.

    `payload_chars` rellena cada mensaje con un string de ese largo
    para forzar que `len(serializado)` sea >> N (es el caso real: 8
    mensajes grandes daban 39037 chars de JSON). Si vale 0, los
    mensajes son mínimos y `len(raw)` queda del orden de N.

    Cada mensaje tiene la forma mínima que `json_array_length` cuenta
    como un elemento. Los `parts` son irrelevantes para el conteo: lo
    que cuenta `messages_len` es la cantidad de mensajes del array, no
    la cantidad de turnos humanos — por eso la etiqueta del front dice
    "mensajes" y no "turnos".
    """
    return json.dumps([
        {
            "kind": "request",
            "parts": [{
                "part_kind": "user-prompt",
                "content": ("x" * payload_chars) if payload_chars else f"msg {i}",
            }],
        }
        for i in range(n)
    ])


class TestMessagesLen(unittest.IsolatedAsyncioTestCase):
    """`messages_len` debe ser la cantidad de mensajes, en ambos endpoints."""

    N = 8  # mismo N que la conversación c645fea5 de la medición

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)
        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name})
        self.conv_id = await self.db.create_conversation(project_slug="demo")

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    # ---- bug (1): el listado debe derivar el contador ----------------

    async def test_list_conversations_returns_messages_len(self) -> None:
        """`GET /conversations` incluye `messages_len` = N para N mensajes.

        Sin el parche (1) `messages_len` ni siquiera está en el dict y
        el `?? 0` del front muestra cero. Con el parche, el SELECT
        proyecta el conteo vía `json_array_length` y el assert pasa.
        """
        await self.db.save_conversation_messages(
            self.conv_id, _history_with_n_messages(self.N))
        from relay.server import create_app

        async def fake_send(self_, *a, **kw):
            return True

        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get("/conversations")
                self.assertEqual(r.status, 200)
                items = (await r.json())["conversations"]
                self.assertEqual(len(items), 1)
                conv = items[0]
                self.assertIn(
                    "messages_len", conv,
                    "el listado debe devolver messages_len; sin el parche "
                    "(1) no viene en el dict y la sidebar muestra 0",
                )
                self.assertEqual(
                    conv["messages_len"], self.N,
                    f"esperado {self.N} mensajes; "
                    f"obtenido {conv['messages_len']!r}",
                )

    async def test_list_messages_json_not_returned_to_keep_payload_light(
        self,
    ) -> None:
        """El listado NO debe traer el blob `messages_json` (es pesado).

        La razón original de excluirlo es que pesan KB por conversación;
        el fix (1) trae SOLO el contador, no el blob. Esto asegura que
        el parche no se "sobrecorrige" volviendo a meter el blob.
        """
        await self.db.save_conversation_messages(
            self.conv_id, _history_with_n_messages(self.N, payload_chars=5000))
        from relay.server import create_app

        async def fake_send(self_, *a, **kw):
            return True

        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get("/conversations")
                items = (await r.json())["conversations"]
                conv = items[0]
                self.assertNotIn(
                    "messages_json", conv,
                    "el listado ya trae el contador; no debería "
                    "volver a incluir el blob completo",
                )

    # ---- bug (2): el detalle debe contar elementos del array --------

    async def test_get_conversation_messages_len_is_count_not_string_len(
        self,
    ) -> None:
        """`GET /conversations/{id}` devuelve `messages_len` = N, no |JSON|.

        La medición sobre `c645fea5` (8 mensajes grandes) dio 39037 con
        `len(raw)` sobre el string JSON. El handler actual hace
        `conv["messages_len"] = len(raw)` y por eso devolvería un
        número del orden de los miles para conversaciones reales.

        Con el fix, cuenta los elementos del array (`len(json.loads(raw))`
        o equivalente) y devuelve 8. El assert rechaza cualquier número
        mayor a N+1 — explícitamente, no es válido el "largo del
        string" ni el `0` que devolvería si `raw` quedara vacío.
        """
        # payloads grandes para que `len(serializado)` >> N (igual que
        # la medición real: 8 mensajes → raw de 39037 chars)
        raw = _history_with_n_messages(self.N, payload_chars=5000)
        self.assertGreater(
            len(raw), 1000,
            "el helper debe producir un JSON claramente más largo que "
            "N para reproducir la condición del bug (2)",
        )
        await self.db.save_conversation_messages(self.conv_id, raw)

        from relay.server import create_app

        async def fake_send(self_, *a, **kw):
            return True

        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(f"/conversations/{self.conv_id}")
                self.assertEqual(r.status, 200)
                conv = await r.json()
                self.assertIn("messages_len", conv)
                got = conv["messages_len"]
                # rechazo explícito de los dos modos de error que
                # diagnostica la medición: 0 (no se cuenta nada) o
                # un número del orden de los miles (se cuenta el
                # string en vez del array)
                self.assertNotEqual(
                    got, 0,
                    "messages_len=0: el handler midió algo que no es "
                    "la cantidad de mensajes (lista vacía o campo no "
                    "derivado)",
                )
                self.assertLessEqual(
                    got, self.N + 1,
                    f"messages_len={got} parece el largo del string "
                    f"JSON en caracteres, no la cantidad de mensajes "
                    f"del array (esperado {self.N}); medición real: "
                    f"8 mensajes → 39037",
                )
                self.assertEqual(got, self.N)

    # ---- guarda para messages_json NULL ------------------------------

    async def test_messages_json_null_does_not_break(self) -> None:
        """`messages_json` NULL devuelve 0 sin reventar.

        La columna es nullable (medido: 7 conversaciones con NULL al
        momento del bug). En el listado, `json_array_length(NULL)` sin
        COALESCE devuelve NULL y rompería la sidebar. En el detalle,
        si el handler asume `raw` siempre string y hace `len(raw)`, un
        `pop` sobre `None` puede caer; la versión actual usa `or ""`
        para protegerse — y este test pinea esa garantía.
        """
        # NO se llama a save_conversation_messages → messages_json NULL
        from relay.server import create_app

        async def fake_send(self_, *a, **kw):
            return True

        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                # /conversations/{id}: handler hace pop(...) or "" → debe
                # responder 200 con messages_len=0 (no explotar con
                # TypeError sobre None).
                r = await client.get(f"/conversations/{self.conv_id}")
                self.assertEqual(
                    r.status, 200,
                    "messages_json NULL no debe romper el handler",
                )
                conv = await r.json()
                self.assertEqual(conv["messages_len"], 0)

                # /conversations: con COALESCE devuelve 0; sin
                # COALESCE, el JSON sería "null" y la sidebar
                # pintaría literalmente "null turnos".
                r2 = await client.get("/conversations")
                self.assertEqual(r2.status, 200)
                items = (await r2.json())["conversations"]
                self.assertEqual(len(items), 1)
                self.assertEqual(
                    items[0]["messages_len"], 0,
                    f"messages_json NULL debe serializarse como 0 en "
                    f"el listado, no como null. "
                    f"obtenido={items[0]['messages_len']!r}",
                )

    # ---- consistencia entre los dos endpoints ------------------------

    async def test_list_and_get_agree_on_messages_len(self) -> None:
        """Para la misma conversación, list y get devuelven el mismo N.

        Este es el contrato que la diagnosis llamaba "la sidebar diría
        39037" si los dos endpoints midieran cosas distintas. Los dos
        tienen que coincidir exactamente.
        """
        await self.db.save_conversation_messages(
            self.conv_id, _history_with_n_messages(self.N, payload_chars=5000))
        from relay.server import create_app

        async def fake_send(self_, *a, **kw):
            return True

        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r_list = await client.get("/conversations")
                list_items = (await r_list.json())["conversations"]
                list_len = list_items[0]["messages_len"]

                r_get = await client.get(f"/conversations/{self.conv_id}")
                get_len = (await r_get.json())["messages_len"]

                self.assertEqual(
                    list_len, get_len,
                    f"list={list_len} vs get={get_len}: el campo "
                    f"messages_len debe significar lo mismo en los dos "
                    f"endpoints (medición bug c645fea5: list=0, "
                    f"get=39037)",
                )
                self.assertEqual(list_len, self.N)


if __name__ == "__main__":
    unittest.main()
