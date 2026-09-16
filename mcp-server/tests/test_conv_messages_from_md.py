"""Tests de Sub-ola X.X: la vista de conversacion se arma desde los .md
por chat (fuente del humano), no desde `messages_json` (camino del LLM).

Caso real: la compactacion reescribe `messages_json` a un resumen de 2
turnos. Antes del fix la UI mostraba ese resumen; despues del fix arma
el hilo desde los .md (que la compactacion no toca) ordenados por
`started_at`.

Cubre:
  - 3 secciones (## Usuario / ## Respuesta / ## Error) -> 3 turnos
  - sin ## Error -> 2 turnos, no se rompe
  - .md inexistente o ilegible -> ese run se saltea, los demas se devuelven
  - content_cap trunca y max_turns limita (igual que el camino viejo)

Correr: cd mcp-server && python -m pytest tests/test_conv_messages_from_md.py -q
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


def _write_md(chats_root: Path, *, target: str, chat_id: str,
              user: str, content: str, error: str | None = None) -> str:
    """Escribe un .md con el mismo formato que persist.write_chat_md."""
    import time
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    d = chats_root / target
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{stamp}-{chat_id[:8]}.md"
    lines = [
        "---",
        f"id: {chat_id}",
        f"target: {target}",
        "source: ui",
        "author: tester",
        "model: test-model",
        "status: ok",
        "duration_ms: 100",
        "ts: 2026-01-01T00:00:00+00:00",
        "---",
        "",
        "## Usuario",
        "",
        user,
        "",
        "## Respuesta",
        "",
        content if content else "(sin contenido)",
    ]
    if error:
        lines += ["", "## Error", "", error]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


async def _get(client: TestClient, conv_id: str, **q: str) -> dict:
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    url = f"/conversations/{conv_id}/messages"
    if qs:
        url += "?" + qs
    r = await client.get(url)
    return await r.json()


class TestMessagesFromMd(unittest.IsolatedAsyncioTestCase):
    """La vista se arma desde los .md por chat, no desde messages_json."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)
        self.chats_root = base / "chats"
        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name})
        self.conv_id = await self.db.create_conversation(project_slug="demo")
        # Compactamos: messages_json queda en 2 turnos. La UI NO debe
        # mostrar esto; debe leer los .md.
        self._summary = json.dumps([
            {"kind": "request", "parts": [
                {"part_kind": "user-prompt", "content": "resumen user"}]},
            {"kind": "response", "parts": [
                {"part_kind": "text", "content": "resumen assistant"}]}])
        await self.db.save_conversation_messages(self.conv_id, self._summary)

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    # -------- helpers --------

    async def _register_chat(self, *, target: str, user: str,
                            content: str, error: str | None = None,
                            requested_by: str | None = None
                            ) -> tuple[str, str]:
        chat_id = await self.db.create_chat(
            project_slug="demo", source="ui", author="tester",
            target=target, conversation_id=self.conv_id,
            requested_by=requested_by,
        )
        md_path = _write_md(self.chats_root, target=target, chat_id=chat_id,
                            user=user, content=content, error=error)
        await self.db.finish_chat(chat_id, status="ok", md_path=md_path)
        return chat_id, md_path

    async def _call(self, client: TestClient, **q: str) -> dict:
        return await _get(client, self.conv_id, **q)

    def _app(self):
        from relay.server import create_app
        return create_app()

    # -------- tests --------

    async def test_el_mail_de_access_cuelga_del_turno_del_usuario(self) -> None:
        """ADR-037: quién pidió el turno viaja al humano, no solo a la DB.

        Va en el turno `user` y no en el `assistant` porque es SU prompt:
        en un hilo compartido por el túnel, dos turnos seguidos pueden ser
        de dos personas distintas.
        """
        await self._register_chat(
            target="demo", user="hacelo", content="hecho",
            requested_by="analyst@example.test")
        async with TestClient(TestServer(self._app())) as client:
            body = await self._call(client)
        users = [t for t in body["messages"] if t["role"] == "user"]
        assert users[0]["requested_by"] == "analyst@example.test"
        # El assistant no lo lleva: no lo pidió él.
        assistants = [t for t in body["messages"] if t["role"] == "assistant"]
        assert "requested_by" not in assistants[0]

    async def test_chat_viejo_sin_solicitante_no_inventa_uno(self) -> None:
        """Los ~800 chats previos a la fase 1 tienen NULL. Se omite la clave."""
        await self._register_chat(
            target="demo", user="hacelo", content="hecho")
        async with TestClient(TestServer(self._app())) as client:
            body = await self._call(client)
        users = [t for t in body["messages"] if t["role"] == "user"]
        assert "requested_by" not in users[0]

    async def test_centinela_local_llega_tal_cual_y_lo_traduce_la_ui(self) -> None:
        """`owner` viaja crudo; la UI lo pinta "local" (ver requesterLabel)."""
        await self._register_chat(
            target="demo", user="hacelo", content="hecho",
            requested_by="owner")
        async with TestClient(TestServer(self._app())) as client:
            body = await self._call(client)
        users = [t for t in body["messages"] if t["role"] == "user"]
        assert users[0]["requested_by"] == "owner"

    async def test_three_sections_yield_three_turns(self) -> None:
        """Un .md con Usuario + Respuesta + Error produce 3 turnos."""
        await self._register_chat(
            target="demo", user="pregunta 1", content="respuesta 1")
        await self._register_chat(
            target="demo", user="pregunta 2", content="",
            error="exploto el LLM")
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = self._app()
            async with TestClient(TestServer(app)) as client:
                body = await self._call(client)
        # chat 1: user+assistant -> 2 turnos.
        # chat 2: user+assistant+error -> 3 turnos (el "## Error" emite
        # un turno assistant extra con el texto del error).
        roles = [m["role"] for m in body["messages"]]
        self.assertEqual(
            roles, ["user", "assistant", "user", "assistant", "assistant"])
        self.assertEqual(body["messages"][0]["content"], "pregunta 1")
        self.assertEqual(body["messages"][1]["content"], "respuesta 1")
        self.assertEqual(body["messages"][2]["content"], "pregunta 2")
        self.assertEqual(body["messages"][3]["content"], "(sin contenido)")
        self.assertEqual(body["messages"][4]["content"], "exploto el LLM")
        self.assertFalse(body["truncated"])

    async def test_two_sections_no_error(self) -> None:
        """Un .md sin ## Error produce 2 turnos y no se rompe."""
        await self._register_chat(
            target="demo", user="hola", content="chau")
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = self._app()
            async with TestClient(TestServer(app)) as client:
                body = await self._call(client)
        self.assertEqual(len(body["messages"]), 2)
        self.assertEqual(body["messages"][0]["role"], "user")
        self.assertEqual(body["messages"][1]["role"], "assistant")

    async def test_missing_md_skipped(self) -> None:
        """Un .md borrado del disco se saltea, los demas se devuelven."""
        await self._register_chat(
            target="demo", user="queda", content="queda respuesta")
        # Este segundo chat apunta a un .md que no existe en disco.
        ghost_id = await self.db.create_chat(
            project_slug="demo", source="ui", author="tester",
            target="demo", conversation_id=self.conv_id,
        )
        await self.db.finish_chat(
            ghost_id, status="ok",
            md_path=str(self.chats_root / "demo" / "no-existe.md"),
        )
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = self._app()
            async with TestClient(TestServer(app)) as client:
                body = await self._call(client)
        # Solo el primer chat sobrevivio (2 turnos).
        self.assertEqual(len(body["messages"]), 2)
        self.assertEqual(body["messages"][0]["content"], "queda")

    async def test_garbled_md_skipped(self) -> None:
        """Un .md malformado se saltea, los demas se devuelven."""
        await self._register_chat(
            target="demo", user="bueno", content="buena respuesta")
        # Segundo chat con .md vacio / sin secciones reconocibles.
        bad_id = await self.db.create_chat(
            project_slug="demo", source="ui", author="tester",
            target="demo", conversation_id=self.conv_id,
        )
        bad_path = self.chats_root / "demo" / "basura.md"
        bad_path.write_text("esto no es un .md valido\n\n", encoding="utf-8")
        await self.db.finish_chat(bad_id, status="ok", md_path=str(bad_path))
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = self._app()
            async with TestClient(TestServer(app)) as client:
                body = await self._call(client)
        self.assertEqual(len(body["messages"]), 2)
        self.assertEqual(body["messages"][0]["content"], "bueno")

    async def test_content_cap_truncates(self) -> None:
        """content_cap trunca el turno individual."""
        big = "x" * 5000
        await self._register_chat(
            target="demo", user="hola", content=big)
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = self._app()
            async with TestClient(TestServer(app)) as client:
                body = await self._call(client, content_cap="500")
        self.assertEqual(len(body["messages"]), 2)
        asst = body["messages"][1]
        self.assertTrue(asst["truncated"])
        self.assertLess(len(asst["content"]), 5000)
        self.assertEqual(body["content_cap"], 500)

    async def test_max_turns_limits(self) -> None:
        """max_turns limita la cantidad total de turnos."""
        for i in range(10):
            await self._register_chat(
                target="demo", user=f"pregunta {i}",
                content=f"respuesta {i}")
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = self._app()
            async with TestClient(TestServer(app)) as client:
                body = await self._call(client, max_turns="4")
        self.assertEqual(len(body["messages"]), 4)
        self.assertTrue(body["truncated"])
        # Ordenados por started_at creciente.
        self.assertEqual(body["messages"][0]["content"], "pregunta 0")
        self.assertEqual(body["messages"][2]["content"], "pregunta 1")

    async def test_ignores_compacted_messages_json(self) -> None:
        """El summary del compact NO aparece: la UI ve los .md."""
        await self._register_chat(
            target="demo", user="texto real", content="respuesta real")
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = self._app()
            async with TestClient(TestServer(app)) as client:
                body = await self._call(client)
        # Antes del fix, este endpoint devolvia "resumen user" /
        # "resumen assistant". Despues del fix, devuelve el .md real.
        contents = [m["content"] for m in body["messages"]]
        self.assertIn("texto real", contents)
        self.assertNotIn("resumen user", contents)

    async def test_returns_200_even_if_all_md_broken(self) -> None:
        """Si todos los .md estan rotos, devolvemos 200 con messages=[]."""
        bad_id = await self.db.create_chat(
            project_slug="demo", source="ui", author="tester",
            target="demo", conversation_id=self.conv_id,
        )
        bad_path = self.chats_root / "demo" / "basura.md"
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_path.write_text("nada", encoding="utf-8")
        await self.db.finish_chat(bad_id, status="ok", md_path=str(bad_path))
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            app = self._app()
            async with TestClient(TestServer(app)) as client:
                r = await client.get(
                    f"/conversations/{self.conv_id}/messages")
                body = await r.json()
        self.assertEqual(r.status, 200)
        self.assertEqual(body["messages"], [])


class TestParseChatMdTurns(unittest.TestCase):
    """Unit tests de persist.parse_chat_md_turns (no async)."""

    def test_three_sections(self) -> None:
        from relay.persist import parse_chat_md_turns
        import tempfile
        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(
                "---\n"
                "id: x\ntarget: t\nstatus: ok\n---\n\n"
                "## Usuario\n\nhola\n\n"
                "## Respuesta\n\nchau\n\n"
                "## Error\n\nalgo fallo\n"
            )
            path = f.name
        try:
            turns = parse_chat_md_turns(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(turns), 3)
        self.assertEqual(turns[0], {"role": "user", "content": "hola"})
        self.assertEqual(turns[1], {"role": "assistant", "content": "chau"})
        self.assertEqual(
            turns[2], {"role": "assistant", "content": "algo fallo"})

    def test_no_error_section(self) -> None:
        from relay.persist import parse_chat_md_turns
        import tempfile
        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(
                "## Usuario\n\nhola\n\n"
                "## Respuesta\n\n(sin contenido)\n"
            )
            path = f.name
        try:
            turns = parse_chat_md_turns(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(turns), 2)

    def test_headings_markdown_del_experto_no_cortan_el_turno(self) -> None:
        """Bug 2026-07-31: el experto escribe markdown y sus `## Titulo`
        cerraban la seccion — la UI mostraba 46 de 2583 chars. Solo
        Usuario/Respuesta/Error separan; el resto es contenido."""
        from relay.persist import parse_chat_md_turns
        import tempfile
        respuesta = (
            "Encontre los repos. Aca va la lista:\n\n"
            "## Repos oficiales\n\n"
            "- `awslabs/mcp`\n\n"
            "## Instalacion\n\n"
            "```bash\n## esto es un comentario, no un heading\nuvx algo\n```\n\n"
            "Fin de la respuesta."
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(f"## Usuario\n\nbusca el mcp\n\n## Respuesta\n\n{respuesta}\n")
            path = f.name
        try:
            turns = parse_chat_md_turns(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[1]["role"], "assistant")
        self.assertIn("## Repos oficiales", turns[1]["content"])
        self.assertIn("Fin de la respuesta.", turns[1]["content"])
        # y el turno del usuario sigue separado (no se tragó la respuesta)
        self.assertEqual(turns[0]["content"], "busca el mcp")

    def test_missing_file_returns_empty(self) -> None:
        from relay.persist import parse_chat_md_turns
        turns = parse_chat_md_turns("/no/existe.md")
        self.assertEqual(turns, [])

    def test_garbled_returns_empty(self) -> None:
        from relay.persist import parse_chat_md_turns
        import tempfile
        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write("basura sin secciones\n")
            path = f.name
        try:
            turns = parse_chat_md_turns(path)
        finally:
            os.unlink(path)
        self.assertEqual(turns, [])


class TestStepsFromProgress(unittest.TestCase):
    """`chats.progress_events` -> `steps[]` (el bloque proceso de la UI).

    El .md solo guarda Usuario/Respuesta: sin esto, al recargar el hilo el
    razonamiento del experto y sus tool calls desaparecian.
    """

    def _steps(self, raw):
        from relay.server import _steps_from_progress
        return _steps_from_progress(raw)

    def test_filters_noise_phases(self) -> None:
        """thinking/writing/heartbeat no son razonamiento: se descartan."""
        raw = json.dumps([
            {"ts": "t", "phase": "thinking"},
            {"ts": "t", "phase": "say", "message": "voy a leer el repo"},
            {"ts": "t", "phase": "writing"},
            {"ts": "t", "phase": "heartbeat"},
            {"ts": "t", "phase": "tool_call", "tool": "read_file",
             "message": "leyo `a.py`"},
        ])
        steps = self._steps(raw)
        self.assertEqual([s["kind"] for s in steps], ["say", "tool"])
        self.assertEqual(steps[1]["tool"], "read_file")

    def test_keeps_diff(self) -> None:
        raw = json.dumps([{"phase": "tool_call", "tool": "edit_file",
                           "message": "edito `a.py`", "diff": "+nueva\n-vieja"}])
        self.assertEqual(self._steps(raw)[0]["diff"], "+nueva\n-vieja")

    def test_say_without_message_dropped(self) -> None:
        """Un `say` vacio no es un pensamiento: no ensucia el contador."""
        raw = json.dumps([{"phase": "say"}, {"phase": "say", "message": "   "}])
        self.assertEqual(self._steps(raw), [])

    def test_caps_count_and_message(self) -> None:
        raw = json.dumps(
            [{"phase": "say", "message": "x" * 5000}] * 300)
        steps = self._steps(raw)
        self.assertEqual(len(steps), 120)
        self.assertEqual(len(steps[0]["message"]), 2000)

    def test_garbage_never_raises(self) -> None:
        for raw in (None, "", "no soy json", "{}", '"texto"', "[1, 2, null]"):
            self.assertEqual(self._steps(raw), [], f"fallo con {raw!r}")


class TestMessagesCarrySteps(unittest.IsolatedAsyncioTestCase):
    """Los pasos del run cuelgan del turno assistant de ESE run."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)
        self.chats_root = base / "chats"
        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name})
        self.conv_id = await self.db.create_conversation(project_slug="demo")

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    async def _run(self, user: str, content: str, events: list) -> None:
        chat_id = await self.db.create_chat(
            project_slug="demo", source="ui", author="tester",
            target="demo", conversation_id=self.conv_id)
        md_path = _write_md(self.chats_root, target="demo", chat_id=chat_id,
                            user=user, content=content)
        await self.db.finish_chat(
            chat_id, status="ok", md_path=md_path,
            progress_events=json.dumps(events))

    async def test_steps_attached_to_own_assistant_turn(self) -> None:
        await self._run("pregunta 1", "respuesta 1",
                        [{"phase": "say", "message": "pienso 1"}])
        await self._run("pregunta 2", "respuesta 2",
                        [{"phase": "tool_call", "tool": "read_file",
                          "message": "leyo `b.py`"}])
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            from relay.server import create_app
            async with TestClient(TestServer(create_app())) as client:
                body = await _get(client, self.conv_id)
        msgs = body["messages"]
        self.assertEqual([m["role"] for m in msgs],
                         ["user", "assistant", "user", "assistant"])
        # cada respuesta se lleva SUS pasos, no los del otro run
        self.assertEqual(msgs[1]["steps"], [{"kind": "say", "message": "pienso 1"}])
        self.assertEqual(msgs[3]["steps"][0]["tool"], "read_file")
        # el turno del usuario nunca los lleva
        self.assertNotIn("steps", msgs[0])

    async def test_no_events_no_steps_key(self) -> None:
        """Sin progress_events el response queda igual que antes."""
        await self._run("hola", "chau", [])
        async def fake_send(self_, *a, **kw):
            return True
        with patch.object(NotifyClient, "send", fake_send):
            from relay.server import create_app
            async with TestClient(TestServer(create_app())) as client:
                body = await _get(client, self.conv_id)
        self.assertNotIn("steps", body["messages"][1])


if __name__ == "__main__":
    unittest.main()
