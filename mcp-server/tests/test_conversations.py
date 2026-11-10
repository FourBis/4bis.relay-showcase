"""Tests de iter 5.0–5.3: expertos async + conversaciones + memoria.

Cover:
  - db: conversations CRUD, touch/stale (sweeper), facts, memories FTS5
  - memory: render_transcript, compact_conversation (TestModel), bloques
  - experts: run_expert con message_history round-trip (TestModel)
  - server: POST /experts/run → 202 → /notify con metadata (ADR-024);
    flujo conversación completo /conversations → run → close → compactación

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_conversations.py -q
"""
from __future__ import annotations
from relay import expert_runner

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from relay import config, experts, memory
from relay.db import Database
from relay.experts import run_expert
from relay.notify import NotifyClient

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR",
             "FOURBIS_COMPACTOR_MODEL", "FOURBIS_MODEL")


def _fake_history(user: str, reply: str) -> str:
    """Historial pydantic-ai mínimo, shape real de ModelMessages."""
    return json.dumps([
        {"parts": [{"part_kind": "user-prompt", "content": user}],
         "kind": "request"},
        {"parts": [{"part_kind": "text", "content": reply}],
         "kind": "response"},
    ])


class TestConversationsDb(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(path=Path(self._tmp.name) / "test.db")
        await self.db.init_schema()
        for slug in ("demo", "otro"):
            await self.db.upsert_project({
                "slug": slug, "name": slug, "repo_path": self._tmp.name})
        self.actor = "member@example.test"
        await self.db.set_user_role(self.actor, "member", project_slugs=["demo", "otro"])

    async def _source(self, project="demo") -> str:
        return await self.db.create_conversation(project_slug=project, requested_by=self.actor)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_conversation_lifecycle(self) -> None:
        conv_id = await self.db.create_conversation(
            project_slug="demo", discord_thread_id="th123")
        conv = await self.db.get_conversation(conv_id)
        self.assertEqual(conv["status"], "open")
        self.assertEqual(conv["discord_thread_id"], "th123")

        await self.db.save_conversation_messages(conv_id, '[{"x":1}]')
        conv = await self.db.get_conversation(conv_id)
        self.assertEqual(conv["messages_json"], '[{"x":1}]')

        # cerrar es cerrar (idempotente)
        self.assertTrue(await self.db.close_conversation(conv_id))
        self.assertFalse(await self.db.close_conversation(conv_id))
        conv = await self.db.get_conversation(conv_id)
        self.assertEqual(conv["status"], "closed")
        self.assertIsNotNone(conv["closed_at"])

    async def test_list_excludes_messages_blob(self) -> None:
        await self.db.create_conversation(project_slug="demo")
        items = await self.db.list_conversations(project_slug="demo")
        self.assertEqual(len(items), 1)
        self.assertNotIn("messages_json", items[0])

    async def test_list_devuelve_messages_len_con_null_en_blob(self) -> None:
        """La sidebar del panel pinta el contador de mensajes por item
        del listado, no del detalle. Hasta iter 10.5 `list_conversations`
        excluía `messages_json` y nunca derivaba el contador, así que la
        sidebar siempre mostraba 0 (con `?? 0`). Iter 10.6 agrega
        `messages_len` derivado con `json_array_length`, protegido con
        `COALESCE` para las conversaciones con `messages_json` NULL
        (la columna puede estar NULL — sin guarda la consulta revienta).

        Sin el parche este test falla en los dos asserts: no hay
        columna `messages_len`, y no devuelve 0 limpio para los NULL.
        Con el parche pasa con [2, 0, 0] para los tres casos.
        """
        conv_vacia = await self.db.create_conversation(project_slug="demo")
        conv_2 = await self.db.create_conversation(project_slug="demo")
        await self.db.save_conversation_messages(
            conv_2, _fake_history("hola", "chau"))
        conv_null = await self.db.create_conversation(project_slug="demo")
        # forzar messages_json NULL sin pasar por save
        await self.db.run(
            "UPDATE conversations SET messages_json=NULL WHERE id=?",
            (conv_null,))

        items = await self.db.list_conversations(project_slug="demo")
        by_id = {it["id"]: it for it in items}
        self.assertEqual(len(items), 3)
        for it in items:
            # derivado de messages_json sin traer el blob
            self.assertNotIn("messages_json", it)
            self.assertIn("messages_len", it)
            self.assertIsInstance(it["messages_len"], int)

        self.assertEqual(by_id[conv_vacia]["messages_len"], 0)
        self.assertEqual(by_id[conv_2]["messages_len"], 2)
        self.assertEqual(by_id[conv_null]["messages_len"], 0)

    async def test_stale_detection(self) -> None:
        conv_id = await self.db.create_conversation(project_slug="demo")
        # recién creada: no está stale ni con umbral chico
        self.assertEqual(await self.db.stale_open_conversations(0.001), [])
        # forzar last_activity viejo
        await self.db.run(
            "UPDATE conversations SET last_activity_at='2020-01-01T00:00:00Z' "
            "WHERE id=?", (conv_id,))
        stale = await self.db.stale_open_conversations(24)
        self.assertEqual([c["id"] for c in stale], [conv_id])
        # touch la rescata
        await self.db.touch_conversation(conv_id)
        self.assertEqual(await self.db.stale_open_conversations(24), [])
        # cerrada no aparece nunca
        await self.db.run(
            "UPDATE conversations SET last_activity_at='2020-01-01T00:00:00Z' "
            "WHERE id=?", (conv_id,))
        await self.db.close_conversation(conv_id)
        self.assertEqual(await self.db.stale_open_conversations(24), [])

    async def test_facts_append_and_list(self) -> None:
        source = await self._source()
        n = await self.db.add_facts(
            "demo", ["usa postgres", "  ", "deploy con publish.ps1"],
            source_conversation=source, actor=self.actor)
        self.assertEqual(n, 2)  # el item en blanco se saltea
        facts = await self.db.list_facts("demo", actor=self.actor)
        self.assertEqual(len(facts), 2)
        self.assertEqual(await self.db.list_facts("otro", actor=self.actor), [])
        # Un actor ausente no obtiene ni modifica conocimiento de una fuente válida.
        self.assertEqual(await self.db.list_facts("demo"), [])
        with self.assertRaises(PermissionError):
            await self.db.add_facts("demo", ["sin actor"], source_conversation=source)

    async def test_facts_nacen_pendientes_y_no_se_inyectan(self) -> None:
        """Aprobación y audiencia son controles independientes."""
        source = await self._source()
        await self.db.add_facts("demo", ["destilado por el LLM"],
            source_conversation=source, actor=self.actor)
        await self.db.add_facts("demo", ["lo escribí yo"], status="approved",
            source_conversation=source, actor=self.actor)

        # El panel autorizado los ve a los dos; la inyección sólo al aprobado.
        todos = await self.db.list_facts("demo", actor=self.actor)
        self.assertEqual(len(todos), 2)
        aprobados = await self.db.list_facts("demo", status="approved", actor=self.actor)
        self.assertEqual([f["fact"] for f in aprobados], ["lo escribí yo"])

        pendiente = next(f for f in todos if f["status"] == "pending")
        self.assertFalse(await self.db.set_fact_status(pendiente["id"], "approved"))
        self.assertTrue(await self.db.set_fact_status(pendiente["id"], "approved", actor=self.actor))
        self.assertEqual(
            len(await self.db.list_facts("demo", status="approved", actor=self.actor)), 2)

        # Rechazar lo saca de la inyección pero lo deja en la tabla.
        await self.db.set_fact_status(pendiente["id"], "rejected", actor=self.actor)
        self.assertEqual(
            len(await self.db.list_facts("demo", status="approved", actor=self.actor)), 1)
        self.assertEqual(len(await self.db.list_facts("demo", actor=self.actor)), 2)

        self.assertFalse(await self.db.set_fact_status(99999, "approved", actor=self.actor))
        with self.assertRaises(ValueError):
            await self.db.set_fact_status(pendiente["id"], "quizas", actor=self.actor)
        with self.assertRaises(ValueError):
            await self.db.add_facts("demo", ["x"], status="quizas",
                source_conversation=source, actor=self.actor)

    async def test_users_crud_y_normalizacion(self) -> None:
        """El email se normaliza a minúsculas al escribir: `identity`
        busca en minúsculas, así que un owner con mayúsculas sería un
        owner que nunca resuelve como owner."""
        async def rol_de(email):
            # El schema puede arrancar sin owner; se mira la fila propia,
            # no una posición fija en la lista.
            return next((u["role"] for u in await self.db.list_users()
                         if u["email"] == email), None)

        await self.db.set_user_role("Jefe@Example.Test", "owner")
        self.assertEqual(await rol_de("jefe@example.test"), "owner")

        with self.assertRaisesRegex(ValueError, "last_admin"):
            await self.db.set_user_role("jefe@example.test", "member")
        self.assertEqual(await rol_de("jefe@example.test"), "owner")
        await self.db.set_user_role("backup@example.test", "owner")
        await self.db.set_user_role("jefe@example.test", "member")
        self.assertEqual(await rol_de("jefe@example.test"), "member")

        self.assertTrue(await self.db.delete_user("JEFE@example.test"))
        self.assertIsNone(await rol_de("jefe@example.test"))
        self.assertFalse(await self.db.delete_user("nadie@example.test"))

        with self.assertRaises(ValueError):
            await self.db.set_user_role("x@y.cl", "root")
        with self.assertRaises(ValueError):
            await self.db.set_user_role("  ", "owner")

    async def test_facts_supersede_soft(self) -> None:
        source = await self._source()
        sibling = await self._source()
        other = await self._source("otro")
        await self.db.add_facts("demo", ["usa mysql", "deploy manual"],
            source_conversation=source, actor=self.actor)
        await self.db.add_facts("demo", ["otra conversación"],
            source_conversation=sibling, actor=self.actor)
        await self.db.add_facts("otro", ["hecho ajeno"],
            source_conversation=other, actor=self.actor)
        old_id = next(f["id"] for f in await self.db.list_facts("demo", actor=self.actor)
                      if f["fact"] == "usa mysql")
        sibling_id = next(f["id"] for f in await self.db.list_facts("demo", actor=self.actor)
                          if f["source_conversation"] == sibling)
        ajeno_id = (await self.db.list_facts("otro", actor=self.actor))[0]["id"]
        # Ni otro proyecto ni otra conversación son publicables por compactar esta fuente.
        n = await self.db.supersede_facts(
            [old_id, sibling_id, ajeno_id, 99999], "demo", superseded_by=source, actor=self.actor)
        self.assertEqual(n, 1)
        vigentes = await self.db.list_facts("demo", actor=self.actor, source_conversation=source)
        self.assertEqual([f["fact"] for f in vigentes], ["deploy manual"])
        todos = await self.db.list_facts("demo", include_superseded=True,
            actor=self.actor, source_conversation=source)
        self.assertEqual(len(todos), 2)
        muerto = next(f for f in todos if f["fact"] == "usa mysql")
        self.assertEqual(muerto["superseded_by"], source)
        self.assertTrue(muerto["superseded_at"])
        # La fuente válida permite comprobar la idempotencia del UPDATE.
        self.assertEqual(await self.db.supersede_facts(
            [old_id], "demo", superseded_by=source, actor=self.actor), 0)
        self.assertEqual(len(await self.db.list_facts("otro", actor=self.actor)), 1)
        self.assertEqual(len(await self.db.list_facts("demo", actor=self.actor,
            source_conversation=sibling)), 1)
        with self.assertRaises(PermissionError):
            await self.db.supersede_facts([old_id], "demo", superseded_by=source)

    async def test_memories_fts_scoped_by_project(self) -> None:
        if not self.db._fts_available:
            self.skipTest("FTS5 no disponible en este sqlite")
        c1, c2, c3 = await self._source(), await self._source(), await self._source("otro")
        await self.db.add_memory(c1, "demo", "se decidió usar postgres para sample-app", actor=self.actor)
        await self.db.add_memory(c2, "demo", "el build corre con dotnet build", actor=self.actor)
        await self.db.add_memory(c3, "otro", "postgres también acá pero es de OTRO proyecto", actor=self.actor)

        hits = await self.db.search_memories("demo", "postgres", actor=self.actor)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["conversation_id"], c1)
        self.assertNotIn(c3, [h["conversation_id"] for h in hits])
        self.assertEqual(await self.db.search_memories("demo", "postgres"), [])
        with self.assertRaises(PermissionError):
            await self.db.add_memory(c1, "demo", "sin actor")

        # Operadores FTS5 en el query no rompen (quoting).
        hits = await self.db.search_memories("demo", 'postgres AND "x -y:', actor=self.actor)
        self.assertIsInstance(hits, list)

        # Re-indexar la misma conversación no duplica.
        await self.db.add_memory(c1, "demo", "resumen nuevo con postgres", actor=self.actor)
        hits = await self.db.search_memories("demo", "postgres", actor=self.actor)
        self.assertEqual(len(hits), 1)

    async def test_search_memories_without_query_falls_back(self) -> None:
        conv_id = await self._source()
        await self.db.close_conversation(conv_id)
        await self.db.set_conversation_summary(conv_id, "resumen reciente")
        hits = await self.db.search_memories("demo", "", actor=self.actor)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["summary"], "resumen reciente")
        self.assertEqual(await self.db.search_memories("demo", ""), [])


class TestMemoryModule(unittest.IsolatedAsyncioTestCase):
    def test_render_transcript(self) -> None:
        text = memory.render_transcript(_fake_history("hola", "chau"))
        self.assertIn("[usuario] hola", text)
        self.assertIn("[experto] chau", text)

    def test_render_transcript_garbage(self) -> None:
        self.assertEqual(memory.render_transcript("no es json"), "")
        self.assertEqual(memory.render_transcript('{"a":1}'), "")
        self.assertEqual(memory.render_transcript("[]"), "")

    def test_render_transcript_truncates_oldest(self) -> None:
        old = _fake_history("viejo " * 20000, "reciente-final")
        text = memory.render_transcript(old)
        self.assertLessEqual(len(text), memory.TRANSCRIPT_MAX_CHARS + 100)
        self.assertIn("reciente-final", text)
        self.assertIn("truncado", text)

    async def test_compact_with_test_model(self) -> None:
        result = await memory.compact_conversation(
            _fake_history("usamos postgres", "anotado"), model_spec="test")
        self.assertIsNotNone(result)
        self.assertIsInstance(result.summary, str)
        self.assertIsInstance(result.facts, list)

    async def test_compact_falls_back_to_prose(self) -> None:
        """El modelo devuelve vacío en vez del JSON → no se pierde el
        summary: segundo intento sin output_type (prosa plana)."""
        from pydantic_ai.messages import ModelResponse, TextPart
        from pydantic_ai.models.function import FunctionModel

        modes: list[bool] = []

        def reply(messages, info):  # noqa: ANN001
            # el agente estructurado pide output tool; el de prosa no
            modes.append(bool(info.output_tools))
            if info.output_tools:
                return ModelResponse(parts=[TextPart(content="\n\n")])
            return ModelResponse(parts=[TextPart(content="resumen en prosa")])

        with patch.object(memory, "build_model",
                          return_value=FunctionModel(reply)):
            result = await memory.compact_conversation(
                _fake_history("usamos postgres", "anotado"))
        # hubo pasada estructurada (fallida) y pasada en prosa
        self.assertIn(True, modes)
        self.assertIn(False, modes)
        self.assertEqual(result.summary, "resumen en prosa")
        self.assertEqual(result.facts, [])

    async def test_compact_fallback_solo_en_400(self) -> None:
        """400 = el provider rechaza el request estructurado (DeepSeek v4
        + tool_choice) → vale la prosa. 429/500 no: falla igual y cobra."""
        from pydantic_ai.exceptions import ModelHTTPError
        from pydantic_ai.messages import ModelResponse, TextPart
        from pydantic_ai.models.function import FunctionModel

        def reply(status: int):
            def _f(messages, info):  # noqa: ANN001
                if info.output_tools:
                    raise ModelHTTPError(status_code=status, model_name="x")
                return ModelResponse(parts=[TextPart(content="prosa")])
            return _f

        hist = _fake_history("usamos postgres", "anotado")
        with patch.object(memory, "build_model",
                          return_value=FunctionModel(reply(400))):
            result = await memory.compact_conversation(hist)
        self.assertEqual(result.summary, "prosa")

        with patch.object(memory, "build_model",
                          return_value=FunctionModel(reply(429))):
            with self.assertRaises(ModelHTTPError):
                await memory.compact_conversation(hist)

    async def test_compact_empty_returns_none(self) -> None:
        self.assertIsNone(await memory.compact_conversation("[]", model_spec="test"))

    def test_build_memory_block(self) -> None:
        self.assertEqual(memory.build_memory_block([]), "")
        block = memory.build_memory_block([{"summary": "algo"}])
        self.assertIn("## Memoria de conversaciones previas", block)
        self.assertIn("- algo", block)

    async def test_compact_descarta_hechos_autorreferenciales(self) -> None:
        """El compactador parafraseaba los HECHOS VIGENTES que le mostramos.

        Caso de ejemplo: los 4 facts más nuevos de example-client (20/8/2026)
        eran "Un hecho vigente establece que…", o sea hechos sobre la
        lista de hechos. Ocupan lugar y envejecen mal, porque el hecho
        que citan puede haberse marcado obsoleto después.
        """
        from pydantic_ai.messages import ModelResponse, ToolCallPart
        from pydantic_ai.models.function import FunctionModel

        def reply(messages, info):  # noqa: ANN001
            return ModelResponse(parts=[ToolCallPart(
                tool_name=info.output_tools[0].name,
                args={"summary": "s",
                      "facts": ["SampleApp usa PostgreSQL",
                                "Un hecho vigente establece que X",
                                "Los hechos vigentes indican que Y"],
                      "skill": None, "obsolete_fact_ids": []})])

        with patch.object(memory, "build_model",
                          return_value=FunctionModel(reply)):
            result = await memory.compact_conversation(
                _fake_history("usamos postgres", "anotado"))
        self.assertEqual(result.facts, ["SampleApp usa PostgreSQL"])

    def test_es_fact_autorreferencial(self) -> None:
        # Literales de la DB.
        for malo in ("Un hecho vigente establece que el timeout de 30000ms…",
                     "Otro hecho vigente señala que la cadena de timeouts…",
                     "Los hechos vigentes indican que ApiM se comunica…"):
            self.assertTrue(memory.es_fact_autorreferencial(malo), malo[:40])
        # Un hecho legítimo no usa esa frase: solo existe porque la
        # ponemos nosotros en la cabecera del bloque.
        for bueno in ("SampleApp usa PostgreSQL en el puerto 5433",
                      "El deploy de INVENTORYDEMO corre con publish.ps1",
                      "El hecho de que corra en Windows obliga a taskkill"):
            self.assertFalse(memory.es_fact_autorreferencial(bueno), bueno[:40])

    def test_build_facts_block_encuadra_y_recorta(self) -> None:
        self.assertEqual(memory.build_facts_block([]), "")
        block = memory.build_facts_block([{"fact": "SampleApp usa PostgreSQL"}])
        self.assertIn("## Hechos del proyecto", block)
        self.assertIn("- SampleApp usa PostgreSQL", block)
        # El encuadre es lo que evita que se lean como órdenes.
        self.assertIn("gana el repo", block)

        # Recorte: se queda con los primeros (= más recientes) y avisa.
        muchos = [{"fact": f"hecho numero {i} " + "x" * 100}
                  for i in range(100)]
        block = memory.build_facts_block(muchos, max_chars=1000)
        self.assertLessEqual(len(block), 1200)
        self.assertIn("hecho numero 0", block)
        self.assertNotIn("hecho numero 99", block)
        self.assertIn("omitidos por tamaño", block)


class TestRunExpertHistory(unittest.IsolatedAsyncioTestCase):
    async def test_message_history_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = {"slug": "demo", "repo_path": tmp,
                       "system_prompt": "", "mcp_servers": [],
                       "defaults_json": {}, "native_tools": []}
            r1 = await run_expert(project, "primer turno",
                                  model_override="test")
            self.assertTrue(r1["messages_json"])
            # el historial devuelto es deserializable y crece al replayar
            r2 = await run_expert(project, "segundo turno",
                                  model_override="test",
                                  message_history_json=r1["messages_json"])
            self.assertGreater(len(r2["messages_json"]),
                               len(r1["messages_json"]))

    async def test_corrupt_history_does_not_break(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = {"slug": "demo", "repo_path": tmp,
                       "system_prompt": "", "mcp_servers": [],
                       "defaults_json": {}, "native_tools": []}
            r = await run_expert(project, "hola", model_override="test",
                                 message_history_json="{basura")
            self.assertTrue(r["content"])


class TestFactsAlwaysOn(unittest.IsolatedAsyncioTestCase):
    """Los facts aprobados de la fuente autorizada llegan al experto."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(path=Path(self._tmp.name) / "facts.db")
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name})
        self.actor = "member@example.test"
        await self.db.set_user_role(self.actor, "member", project_slugs=["demo"])
        initial_task = {"mode": "read_only", "read_only": True, "no_checkout": True, "state": "ready"}
        self.cid = await self.db.create_conversation(project_slug="demo",
            requested_by=self.actor, initial_task=initial_task)
        sibling = await self.db.create_conversation(project_slug="demo",
            requested_by=self.actor, initial_task=initial_task)
        await self.db.add_facts("demo", ["SampleApp usa PostgreSQL en el 5433"],
            source_conversation=self.cid, status="approved", actor=self.actor)
        await self.db.add_facts("demo", ["hecho pendiente no aprobado"],
            source_conversation=self.cid, actor=self.actor)
        await self.db.add_facts("demo", ["hecho aprobado de otra conversación"],
            source_conversation=sibling, status="approved", actor=self.actor)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _project(self, **defaults):
        return {"slug": "demo", "repo_path": self._tmp.name, "system_prompt": "",
                "mcp_servers": [], "native_tools": [],
                "defaults_json": {"read_only": True, "native_shell": False,
                    "native_files": False, "sql_tools": False, **defaults}}

    async def _instructions(self, project, *, conversation_id=None, actor=None):
        """El runner conserva su admisión real; sólo se observa el Agent."""
        from relay import user_accounts
        # resolved_project relee la configuración vigente del proyecto real.
        await self.db.upsert_project(project)
        sink = {}
        real = expert_runner.Agent

        class _Spy:
            def __init__(self, *a, **kw):
                instr = kw.get("instructions")
                if isinstance(instr, (list, tuple)):
                    instr = "\n\n".join(
                        p() if callable(p) else (p or "") for p in instr)
                sink["instructions"] = instr or ""
                self._inner = real(*a, **kw)

            def __getattr__(self, n):
                return getattr(self._inner, n)

        with patch.object(expert_runner, "Agent", _Spy), user_accounts.bind_actor(self.db, actor):
            await run_expert(project, "hola", model_override="test", db=self.db,
                conversation_id=conversation_id or "")
        return sink["instructions"]

    async def test_con_el_flag_los_hechos_llegan_al_prompt(self) -> None:
        with patch.object(self.db, "list_facts", wraps=self.db.list_facts) as requested:
            instr = await self._instructions(self._project(facts_always_on=True),
                conversation_id=self.cid, actor=self.actor)
        self.assertIn("## Hechos del proyecto", instr)
        self.assertIn("SampleApp usa PostgreSQL en el 5433", instr)
        self.assertNotIn("hecho pendiente no aprobado", instr)
        self.assertNotIn("hecho aprobado de otra conversación", instr)
        requested.assert_awaited_once_with("demo", limit=200, status="approved",
            actor=self.actor, source_conversation=self.cid)

    async def test_sin_el_flag_no_se_pagan_esos_tokens(self) -> None:
        with patch.object(self.db, "list_facts", wraps=self.db.list_facts) as requested:
            instr = await self._instructions(self._project(),
                conversation_id=self.cid, actor=self.actor)
        self.assertNotIn("## Hechos del proyecto", instr)
        requested.assert_not_awaited()

    async def test_sin_conversacion_no_se_consultan_hechos(self) -> None:
        with patch.object(self.db, "list_facts", wraps=self.db.list_facts) as requested:
            instr = await self._instructions(self._project(facts_always_on=True), actor=self.actor)
        self.assertNotIn("## Hechos del proyecto", instr)
        requested.assert_not_awaited()

    async def test_sin_actor_no_admite_el_modelo(self) -> None:
        with self.assertRaises(PermissionError):
            await self._instructions(self._project(facts_always_on=True), conversation_id=self.cid)

    async def test_una_db_rota_no_mata_el_run(self) -> None:
        from pydantic_ai.messages import ModelResponse, TextPart
        from pydantic_ai.models.function import FunctionModel
        from relay import user_accounts
        expected = "Respuesta determinista sin hechos disponibles."

        def reply(messages, info):
            return ModelResponse(parts=[TextPart(content=expected)])

        project = self._project(facts_always_on=True)
        await self.db.upsert_project(project)
        with patch.object(expert_runner.expert_models, "build_model", return_value=FunctionModel(reply)):
            with patch.object(self.db, "list_facts", side_effect=RuntimeError("sqlite se cayó")) as requested:
                with user_accounts.bind_actor(self.db, self.actor):
                    result = await run_expert(project, "hola",
                        model_override="test", db=self.db, conversation_id=self.cid)
        requested.assert_awaited_once_with("demo", limit=200, status="approved",
            actor=self.actor, source_conversation=self.cid)
        self.assertEqual(result["content"], expected)


class TestExpertsAsyncHttp(unittest.IsolatedAsyncioTestCase):
    """Round-trip: 202, cola durable y resultado consultable en Relay."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._env_backup = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        os.environ["FOURBIS_COMPACTOR_MODEL"] = "test"
        config.set_runtime_config({"FOURBIS_MODEL": "test",
                                   "FOURBIS_COMPACTOR_MODEL": "test"})

        self.db = Database()  # usa FOURBIS_DB_PATH
        await self.db.init_schema()
        await self.db.set_config("FOURBIS_MODEL", "test")
        await self.db.set_config("FOURBIS_COMPACTOR_MODEL", "test")
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name,
            "defaults_json": {"model": "test"},
        })

    async def asyncTearDown(self) -> None:
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.set_runtime_config({})
        self._tmp.cleanup()

    async def _wait(self, predicate, timeout: float = 10.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            value = predicate()
            if value:
                return value
            await asyncio.sleep(0.05)
        self.fail("timeout esperando condición async")

    async def _wait_chat_finished(self, chat_id: str, timeout: float = 10.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            chat = await self.db.get_chat(chat_id)
            if chat and chat["status"] not in {"queued", "running"}:
                return chat
            await asyncio.sleep(0.05)
        self.fail("el chat quedó en cola o corriendo")

    async def test_run_returns_202_and_keeps_result_in_relay(self) -> None:
        from relay.server import create_app
        notifications: list[dict] = []

        async def fake_send(self_, agent_id, kind, message, metadata=None):
            notifications.append({"agent_id": agent_id, "kind": kind,
                                  "message": message,
                                  "metadata": metadata or {}})
            return True

        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.post("/experts/run", json={
                    "target": "demo", "user": "hola experto",
                    "source": "test", "author": "pytest",
                })
                self.assertEqual(r.status, 202)
                body = await r.json()
                chat_id = body["id"]
                self.assertIn(body["status"], {"queued", "running"})
                self.assertTrue(body["conversation_id"])

                chat = await self._wait_chat_finished(chat_id)
                self.assertEqual(chat["status"], "ok")
                self.assertEqual(chat["project_slug"], "demo")
                # No verified Discord audience is attached to this run.
                self.assertEqual(notifications, [])

    async def test_conversation_flow_end_to_end(self) -> None:
        from relay.server import create_app
        notifications: list[dict] = []

        async def fake_send(self_, agent_id, kind, message, metadata=None):
            notifications.append({"kind": kind, "metadata": metadata or {}})
            return True

        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                # /nuevo
                r = await client.post("/conversations", json={
                    "project": "demo", "discord_thread_id": "th-42"})
                self.assertEqual(r.status, 201)
                conv_id = (await r.json())["id"]

                # dos turnos en la misma conversación
                for turno in ("turno uno", "turno dos"):
                    r = await client.post("/experts/run", json={
                        "target": "demo", "user": turno,
                        "conversation": conv_id,
                    })
                    self.assertEqual(r.status, 202)
                    event = await r.json()
                    await self._wait_chat_finished(event["id"])

                # el historial creció
                r = await client.get(f"/conversations/{conv_id}")
                detail = await r.json()
                self.assertGreater(detail["messages_len"], 0)
                self.assertEqual(detail["discord_thread_id"], "th-42")
                self.assertEqual(notifications, [])

                # /cerrar → compactación (TestModel via FOURBIS_COMPACTOR_MODEL)
                r = await client.post(f"/conversations/{conv_id}/close")
                self.assertEqual(r.status, 200)
                self.assertEqual((await r.json())["compaction"], "skipped")
                self.assertTrue(await self.db.get_conversation_task(conv_id))

                # La conversación administrada puede reabrirse: cerrar su
                # vista no termina la tarea ni revoca su acceso.
                r = await client.post("/experts/run", json={
                    "target": "demo", "user": "tarde",
                    "conversation": conv_id,
                })
                self.assertEqual(r.status, 202)
                event = await r.json()
                await self._wait_chat_finished(event["id"])

    async def test_auto_attach_by_thread_id(self) -> None:
        """Fix 2026-07-12: /experts/run con discord_thread_id (sin
        `conversation`) resuelve/crea la conversación en el relay. Antes
        el bot tenía que mapear hilo↔conversación y no lo hacía: cada
        mensaje del hilo era un chat amnésico con thread_id null."""
        from relay.server import create_app
        notifications: list[dict] = []

        async def fake_send(self_, agent_id, kind, message, metadata=None):
            notifications.append({"kind": kind, "metadata": metadata or {}})
            return True

        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                # primer mensaje del hilo: crea la conversación sola
                r = await client.post("/experts/run", json={
                    "target": "demo", "user": "primer mensaje",
                    "discord_thread_id": "th-99"})
                self.assertEqual(r.status, 202)
                conv_id = (await r.json())["conversation_id"]
                self.assertIsNotNone(conv_id)
                await self._wait_chat_finished((await r.json())["id"])
                detail = await (await client.get(f"/conversations/{conv_id}")).json()
                self.assertEqual(detail["discord_thread_id"], "th-99")

                # segundo mensaje del mismo hilo: reusa la conversación
                r = await client.post("/experts/run", json={
                    "target": "demo", "user": "segundo mensaje",
                    "discord_thread_id": "th-99"})
                self.assertEqual(r.status, 202)
                event = await r.json()
                self.assertEqual(event["conversation_id"], conv_id)
                await self._wait_chat_finished(event["id"])

                # `conversation` explícita gana sobre el thread_id
                r = await client.post("/experts/run", json={
                    "target": "demo", "user": "x",
                    "conversation": conv_id,
                    "discord_thread_id": "th-otro"})
                self.assertEqual(r.status, 202)
                event = await r.json()
                self.assertEqual(event["conversation_id"], conv_id)
                await self._wait_chat_finished(event["id"])

                # hilo que sigue tras /cerrar → conversación NUEVA
                r = await client.post(f"/conversations/{conv_id}/close")
                self.assertEqual(r.status, 200)
                r = await client.post("/experts/run", json={
                    "target": "demo", "user": "post cierre",
                    "discord_thread_id": "th-99"})
                self.assertEqual(r.status, 202)
                conv2 = (await r.json())["conversation_id"]
                self.assertIsNotNone(conv2)
                self.assertNotEqual(conv2, conv_id)
                await self._wait_chat_finished((await r.json())["id"])
                self.assertEqual(notifications, [])

                # tipo inválido → 400
                r = await client.post("/experts/run", json={
                    "target": "demo", "user": "x",
                    "discord_thread_id": 123})
                self.assertEqual(r.status, 400)

    async def test_conversation_validation(self) -> None:
        from relay.server import create_app

        async def fake_send(self_, agent_id, kind, message, metadata=None):
            return True

        with patch.object(NotifyClient, "send", fake_send):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                # conversación inexistente
                r = await client.post("/experts/run", json={
                    "target": "demo", "user": "x",
                    "conversation": "no-existe"})
                self.assertEqual(r.status, 404)
                # proyecto inexistente en /conversations
                r = await client.post("/conversations", json={
                    "project": "fantasma"})
                self.assertEqual(r.status, 404)

    async def test_bad_bodies_return_400(self) -> None:
        """Regresión 2026-07-09: body no-UTF8 daba 500 (UnicodeDecodeError
        no capturado); y /night-mode/status sin run_id daba 404 confuso."""
        from relay.server import create_app

        app = create_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.post(
                "/experts/run",
                # Con acento a propósito: en latin-1 son bytes que NO son
                # UTF-8 válido, que es justo lo que este test ejercita.
                data='{"target":"demo","user":"análisis"}'.encode("latin-1"),
                headers={"Content-Type": "application/json"})
            self.assertEqual(r.status, 400)

            r = await client.get("/night-mode/status")
            self.assertEqual(r.status, 200)
            self.assertEqual((await r.json())["active"], [])

    async def test_double_close_compacts_once(self) -> None:
        """Regresión 2026-07-09: dos /close casi simultáneos disparaban
        DOS compactaciones (doble gasto LLM). El guard _COMPACTING
        saltea la segunda mientras la primera sigue en vuelo."""
        from relay import server as server_mod
        from relay.server import create_app

        calls = {"n": 0}

        async def slow_compact(messages_json, **kwargs):
            calls["n"] += 1
            await asyncio.sleep(0.5)
            return None

        async def fake_send(self_, agent_id, kind, message, metadata=None):
            return True

        with patch.object(NotifyClient, "send", fake_send), \
                patch.object(server_mod.memory, "compact_conversation",
                             slow_compact):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                # Legacy conversation exercises compaction. Managed tasks
                # intentionally skip compaction when their view is closed.
                conv_id = await self.db.create_conversation(
                    project_slug="demo", requested_by="owner")
                r = await client.post("/experts/run", json={
                    "target": "demo", "user": "hola",
                    "conversation": conv_id})
                self.assertEqual(r.status, 202)
                event = await r.json()
                await self._wait_chat_finished(event["id"])
                # esperar a que el run persista el historial
                async def _has_msgs():
                    resp = await client.get(f"/conversations/{conv_id}")
                    return (await resp.json())["messages_len"] > 0
                t0 = time.monotonic()
                while time.monotonic() - t0 < 10:
                    if await _has_msgs() and not app[server_mod.BG_TASKS_KEY]:
                        break
                    await asyncio.sleep(0.05)
                r1 = await client.post(f"/conversations/{conv_id}/close")
                r2 = await client.post(f"/conversations/{conv_id}/close")
                self.assertEqual(r1.status, 200)
                self.assertEqual(r2.status, 200)
                await asyncio.sleep(0.8)  # dejar terminar la compactación
                self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
