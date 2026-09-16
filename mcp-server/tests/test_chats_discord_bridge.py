"""Tests del iter 10.0: bridge Discord↔UI.

Cubre:
  - conversations schema: columnas nuevas discord_user_id/discord_author
    persisten y se leen.
  - POST /conversations acepta discord_user_id + discord_author.
  - POST /experts/run con discord_user_id los persiste en la conv.
  - GET /conversations/{id} devuelve los nuevos campos.
  - SET_CONVERSATION_DISCORD_USER para chats UI que se "vinculan"
    a un Discord user después.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_chats_discord_bridge.py -v
"""
from __future__ import annotations

import asyncio
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


# ---------- schema ----------

class TestSchema(unittest.IsolatedAsyncioTestCase):
    """Las columnas nuevas existen y son NULL por default."""

    async def test_conversations_has_discord_user_id_and_author(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _tmp_env(Path(tmp))
            try:
                db = Database()
                await db.init_schema()
                # Crear conv sin discord_user_id (caso UI).
                conv_id = await db.create_conversation(
                    project_slug="demo", discord_thread_id=None)
                conv = await db.get_conversation(conv_id)
                self.assertIsNotNone(conv)
                # Columnas nuevas existen y son None.
                self.assertIn("discord_user_id", conv)
                self.assertIn("discord_author", conv)
                self.assertIsNone(conv["discord_user_id"])
                self.assertIsNone(conv["discord_author"])
            finally:
                _clear_env()

    async def test_create_conversation_with_discord_user_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _tmp_env(Path(tmp))
            try:
                db = Database()
                await db.init_schema()
                conv_id = await db.create_conversation(
                    project_slug="demo",
                    discord_thread_id="thread_abc",
                    discord_user_id="user_123",
                    discord_author="Alice")
                conv = await db.get_conversation(conv_id)
                self.assertEqual(conv["discord_user_id"], "user_123")
                self.assertEqual(conv["discord_author"], "Alice")
                self.assertEqual(conv["discord_thread_id"], "thread_abc")
            finally:
                _clear_env()

    async def test_set_conversation_discord_user(self) -> None:
        """El helper set_conversation_discord_user actualiza solo
        los campos nuevos sin tocar los demás."""
        with tempfile.TemporaryDirectory() as tmp:
            _tmp_env(Path(tmp))
            try:
                db = Database()
                await db.init_schema()
                conv_id = await db.create_conversation(
                    project_slug="demo",
                    discord_thread_id="thread_x")
                # Inicialmente sin user_id.
                conv = await db.get_conversation(conv_id)
                self.assertIsNone(conv["discord_user_id"])
                # Setear después (caso UI: chat nace sin autor Discord
                # pero después el humano se identifica).
                await db.set_conversation_discord_user(
                    conv_id, discord_user_id="user_late",
                    discord_author="Late Bob")
                conv = await db.get_conversation(conv_id)
                self.assertEqual(conv["discord_user_id"], "user_late")
                self.assertEqual(conv["discord_author"], "Late Bob")
                # discord_thread_id intacto.
                self.assertEqual(conv["discord_thread_id"], "thread_x")
            finally:
                _clear_env()


# ---------- HTTP ----------

class TestDiscordBridgeHttp(unittest.IsolatedAsyncioTestCase):
    """POST /conversations y POST /experts/run aceptan los nuevos campos."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)
        self.db = Database()
        await self.db.init_schema()
        # Proyecto dummy para /experts/run.
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo",
            "repo_path": str(base), "description": "test"})

        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            from relay.server import create_app
            self.app = create_app()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        _clear_env()
        self._tmp.cleanup()

    async def test_create_conversation_with_discord_user(self) -> None:
        body = {
            "project": "demo",
            "discord_user_id": "user_xyz",
            "discord_author": "TestUser",
        }
        r = await self.client.post("/conversations", json=body)
        self.assertEqual(r.status, 201)
        data = await r.json()
        self.assertEqual(data["discord_user_id"], "user_xyz")
        self.assertEqual(data["discord_author"], "TestUser")
        # GET /conversations/{id} los devuelve.
        r = await self.client.get(f"/conversations/{data['id']}")
        self.assertEqual(r.status, 200)
        conv = await r.json()
        self.assertEqual(conv["discord_user_id"], "user_xyz")

    async def test_experts_run_auto_attaches_with_discord_user(self) -> None:
        """Si /experts/run viene con discord_user_id (sin conv previa),
        lo crea en la conversation auto-attach."""
        body = {
            "target": "demo",
            "user": "hola",
            "discord_thread_id": "thread_new",
            "discord_user_id": "user_new",
            "discord_author": "New",
            "source": "discord",
            "author": "New",
            "model": "test",
        }
        r = await self.client.post("/experts/run", json=body)
        self.assertEqual(r.status, 202)
        data = await r.json()
        conv_id = data["conversation_id"]
        # La conv auto-creada tiene discord_user_id.
        r = await self.client.get(f"/conversations/{conv_id}")
        conv = await r.json()
        self.assertEqual(conv["discord_user_id"], "user_new")
        self.assertEqual(conv["discord_author"], "New")

    async def test_set_conversation_discord_user_via_endpoint(self) -> None:
        """POST /conversations/{id}/set-discord-user: UI vincula el chat
        a un Discord user después (caso: chat de UI que el humano
        quiere contestar desde Discord)."""
        # Crear conv sin user.
        r = await self.client.post("/conversations", json={
            "project": "demo",
        })
        self.assertEqual(r.status, 201)
        conv_id = (await r.json())["id"]

        # Setear via endpoint.
        r = await self.client.post(
            f"/conversations/{conv_id}/set-discord-user",
            json={"discord_user_id": "user_linked",
                  "discord_author": "Linked"})
        self.assertEqual(r.status, 200)
        # Verificar.
        r = await self.client.get(f"/conversations/{conv_id}")
        conv = await r.json()
        self.assertEqual(conv["discord_user_id"], "user_linked")
        self.assertEqual(conv["discord_author"], "Linked")

    async def test_clear_conversation_discord_user_via_endpoint(self) -> None:
        """Iter 10.0: POST con discord_user_id=null desvincula la conv.

        Caso: el humano vinculó un chat, después se arrepintió y quiere
        desvincularlo. La UI manda un POST con null y el relay limpia
        ambos campos."""
        # Crear conv y vincularla.
        r = await self.client.post("/conversations", json={
            "project": "demo",
            "discord_user_id": "user_will_unlink",
            "discord_author": "Unlink Me",
        })
        self.assertEqual(r.status, 201)
        conv_id = (await r.json())["id"]

        # Verificar vínculo inicial.
        r = await self.client.get(f"/conversations/{conv_id}")
        conv = await r.json()
        self.assertEqual(conv["discord_user_id"], "user_will_unlink")

        # Desvincular con null.
        r = await self.client.post(
            f"/conversations/{conv_id}/set-discord-user",
            json={"discord_user_id": None})
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertTrue(data.get("cleared"))
        self.assertIsNone(data["discord_user_id"])
        self.assertIsNone(data["discord_author"])

        # Confirmar en DB.
        r = await self.client.get(f"/conversations/{conv_id}")
        conv = await r.json()
        self.assertIsNone(conv["discord_user_id"])
        self.assertIsNone(conv["discord_author"])

    async def test_create_thread_fallido_no_deja_vinculo_fantasma(self) -> None:
        """Si el DM no se pudo crear, la conv NO queda vinculada.

        Antes (hasta 2026-08-16) el relay escribía el vínculo y DESPUÉS
        pedía el hilo: con el bot caído la conv quedaba marcada como
        vinculada, la UI escondía el botón de vincular y mostraba
        "Discord · @vos", pero el DM no existía. Quien cerraba la
        notebook confiando en eso no recibía nada.
        """
        r = await self.client.post("/conversations", json={"project": "demo"})
        conv_id = (await r.json())["id"]

        async def bot_caido(**kw):
            return None, "no se pudo crear thread: connection refused"

        with patch("relay.server._request_bot_create_thread", bot_caido):
            r = await self.client.post(
                f"/conversations/{conv_id}/set-discord-user",
                json={"discord_user_id": "example-user-id",
                      "discord_author": "Yo", "create_thread": True})
        self.assertEqual(r.status, 502)
        body = await r.json()
        self.assertFalse(body["linked"])
        self.assertIn("bot", body)  # la sonda viaja para poder ofrecer arrancarlo

        # Lo que importa: la conversación quedó intacta.
        r = await self.client.get(f"/conversations/{conv_id}")
        conv = await r.json()
        self.assertIsNone(conv["discord_user_id"])
        self.assertIsNone(conv["discord_thread_id"])

    async def test_create_thread_ok_vincula_usuario_y_hilo(self) -> None:
        """El camino feliz sigue escribiendo ambos campos."""
        r = await self.client.post("/conversations", json={"project": "demo"})
        conv_id = (await r.json())["id"]

        async def bot_ok(**kw):
            return "dm_channel_42", None

        with patch("relay.server._request_bot_create_thread", bot_ok):
            r = await self.client.post(
                f"/conversations/{conv_id}/set-discord-user",
                json={"discord_user_id": "example-user-id",
                      "discord_author": "Yo", "create_thread": True})
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())["discord_thread_id"], "dm_channel_42")

        r = await self.client.get(f"/conversations/{conv_id}")
        conv = await r.json()
        self.assertEqual(conv["discord_user_id"], "example-user-id")
        self.assertEqual(conv["discord_thread_id"], "dm_channel_42")

    async def test_conversacion_nace_vinculada_al_discord_por_default(self) -> None:
        """Con FOURBIS_DEFAULT_DISCORD_USER, no hay que apretar nada.

        El botón de vincular se aprieta ANTES de irse, que es justo cuando
        uno no se acuerda: se lanza el run, se cierra la notebook, y la
        respuesta queda esperando en una pantalla que nadie mira.
        """
        with patch.dict("relay.config._runtime", {
                "FOURBIS_DEFAULT_DISCORD_USER": "example-user-id",
                "FOURBIS_DEFAULT_DISCORD_AUTHOR": "ExampleUser"}):
            r = await self.client.post("/conversations", json={"project": "demo"})
        self.assertEqual(r.status, 201)
        data = await r.json()
        self.assertEqual(data["discord_user_id"], "example-user-id")
        self.assertEqual(data["discord_author"], "ExampleUser")

    async def test_lo_explicito_le_gana_al_default(self) -> None:
        """Una conv que ya trae su user (las que abre el bot) no se toca."""
        with patch.dict("relay.config._runtime", {
                "FOURBIS_DEFAULT_DISCORD_USER": "example-user-id"}):
            r = await self.client.post("/conversations", json={
                "project": "demo", "discord_user_id": "other-user-id",
                "discord_author": "Otro"})
        data = await r.json()
        self.assertEqual(data["discord_user_id"], "other-user-id")

    async def test_sin_default_la_conversacion_nace_sin_vincular(self) -> None:
        """Sin configurar, el comportamiento es el de antes."""
        with patch.dict("relay.config._runtime", {"FOURBIS_DEFAULT_DISCORD_USER": ""}):
            r = await self.client.post("/conversations", json={"project": "demo"})
        data = await r.json()
        self.assertIsNone(data["discord_user_id"])

    async def test_seguir_en_el_celu_usa_el_default_sin_pedir_id(self) -> None:
        """`create_thread` sin user_id = "mandámelo al Discord de siempre".

        NO es una desvinculación, aunque el user_id venga ausente: el
        `create_thread` es la señal de intención opuesta.
        """
        r = await self.client.post("/conversations", json={"project": "demo"})
        conv_id = (await r.json())["id"]

        async def bot_ok(**kw):
            self.assertEqual(kw["discord_user_id"], "example-user-id")
            return "dm_channel_9", None

        with patch.dict("relay.config._runtime", {
                "FOURBIS_DEFAULT_DISCORD_USER": "example-user-id"}), \
                patch("relay.server._request_bot_create_thread", bot_ok):
            r = await self.client.post(
                f"/conversations/{conv_id}/set-discord-user",
                json={"create_thread": True})
        self.assertEqual(r.status, 200)
        conv = await (await self.client.get(f"/conversations/{conv_id}")).json()
        self.assertEqual(conv["discord_user_id"], "example-user-id")
        self.assertEqual(conv["discord_thread_id"], "dm_channel_9")

    async def test_seguir_en_el_celu_hereda_el_usuario_de_la_conv(self) -> None:
        """Vínculo sin hilo: repara heredando, sin default y sin modal."""
        r = await self.client.post("/conversations", json={
            "project": "demo", "discord_user_id": "example-user-id",
            "discord_author": "Yo"})
        conv_id = (await r.json())["id"]

        async def bot_ok(**kw):
            self.assertEqual(kw["discord_user_id"], "example-user-id")
            return "dm_channel_h", None

        with patch.dict("relay.config._runtime", {"FOURBIS_DEFAULT_DISCORD_USER": ""}), \
                patch("relay.server._request_bot_create_thread", bot_ok):
            r = await self.client.post(
                f"/conversations/{conv_id}/set-discord-user",
                json={"create_thread": True})
        self.assertEqual(r.status, 200)

    async def test_seguir_en_el_celu_sin_default_ni_vinculo_es_400(self) -> None:
        """Sin nada de dónde sacar el usuario, el error dice qué falta."""
        r = await self.client.post("/conversations", json={"project": "demo"})
        conv_id = (await r.json())["id"]
        with patch.dict("relay.config._runtime", {"FOURBIS_DEFAULT_DISCORD_USER": ""}):
            r = await self.client.post(
                f"/conversations/{conv_id}/set-discord-user",
                json={"create_thread": True})
        self.assertEqual(r.status, 400)
        body = await r.json()
        self.assertTrue(body["needs_user_id"])
        self.assertIn("FOURBIS_DEFAULT_DISCORD_USER", body["error"])

    async def test_set_discord_user_empty_string_is_400(self) -> None:
        """discord_user_id="" NO es señal de clear (es ambiguo: ¿quiso
        escribir un ID y se olvidó?). Devolver 400 para forzar al cliente
        a mandar null explícito si quiere desvincular."""
        r = await self.client.post("/conversations", json={
            "project": "demo",
        })
        self.assertEqual(r.status, 201)
        conv_id = (await r.json())["id"]
        r = await self.client.post(
            f"/conversations/{conv_id}/set-discord-user",
            json={"discord_user_id": ""})
        self.assertEqual(r.status, 400)


class TestDbClear(unittest.IsolatedAsyncioTestCase):
    """Helper set_conversation_discord_user(clear=True) limpia ambos campos."""

    async def test_set_conversation_discord_user_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _tmp_env(Path(tmp))
            try:
                db = Database()
                await db.init_schema()
                conv_id = await db.create_conversation(
                    project_slug="demo",
                    discord_thread_id="thread_keep",
                    discord_user_id="user_a",
                    discord_author="AuthorA")
                # Confirmar vínculo.
                conv = await db.get_conversation(conv_id)
                self.assertEqual(conv["discord_user_id"], "user_a")
                self.assertEqual(conv["discord_author"], "AuthorA")
                # Clear.
                await db.set_conversation_discord_user(
                    conv_id, discord_user_id=None, clear=True)
                conv = await db.get_conversation(conv_id)
                self.assertIsNone(conv["discord_user_id"])
                self.assertIsNone(conv["discord_author"])
                # discord_thread_id intacto (no es parte del clear).
                self.assertEqual(conv["discord_thread_id"], "thread_keep")
            finally:
                _clear_env()


if __name__ == "__main__":
    unittest.main()
