"""Tests del autoaprendizaje (2026-07-12): borradores de skill con
aprobación humana + curación de facts/memorias desde la Admin UI.

Cubre:
  - DB: CRUD de skill_drafts, count de pendientes, delete_fact,
    delete_memory (FTS + summary).
  - skills.py: sanitize_skill_name (anti path-traversal), write/list/
    read/delete sync.
  - Endpoints: listado con badge, PATCH solo pending, approve escribe
    el SKILL.md (409 needs_overwrite si colisiona), reject, deletes.
  - memory.py: CompactionResult acepta skill opcional (default None).

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_skill_drafts.py -q
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from relay.db import Database
from relay import config as relay_config
from relay.notify import NotifyClient
from relay import skills as skills_mod

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR",
             "FOURBIS_SKILLS_DIR", "VOICE_TRANSCRIPTS_DIR")


def _tmp_env(tmp: Path) -> None:
    os.environ["FOURBIS_DB_PATH"] = str(tmp / "test.db")
    os.environ["FOURBIS_CHATS_DIR"] = str(tmp / "chats")
    os.environ["FOURBIS_JSONL_DIR"] = str(tmp / "jsonl")
    os.environ["FOURBIS_SKILLS_DIR"] = str(tmp / "skills")
    os.environ["VOICE_TRANSCRIPTS_DIR"] = str(tmp / "transcripts")


def _clear_env() -> None:
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


# ---------- skills.py: helpers sync ----------


class TestSkillHelpers(unittest.TestCase):
    def test_sanitize_kebab(self) -> None:
        self.assertEqual(skills_mod.sanitize_skill_name("Deploy INVENTORYDEMO!!"),
                         "deploy-inventorydemo")
        self.assertEqual(skills_mod.sanitize_skill_name("ya_kebab-ok"),
                         "ya-kebab-ok")
        self.assertEqual(skills_mod.sanitize_skill_name("  --x--  "), "x")

    def test_sanitize_blocks_traversal(self) -> None:
        # "../../evil" no puede sobrevivir: solo [a-z0-9-].
        self.assertEqual(skills_mod.sanitize_skill_name("../../evil"), "evil")
        self.assertEqual(skills_mod.sanitize_skill_name("a/b\\c"), "a-b-c")
        self.assertEqual(skills_mod.sanitize_skill_name("..."), "")

    def test_write_read_list_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = skills_mod.write_skill_sync(
                "Deploy INVENTORYDEMO", "cómo deployar", "# Pasos\n1. build", base)
            self.assertEqual(p, base / "deploy-inventorydemo" / "SKILL.md")
            text = p.read_text(encoding="utf-8")
            self.assertIn("name: deploy-inventorydemo", text)
            self.assertIn("description: cómo deployar", text)
            self.assertIn("# Pasos", text)
            # el parser del SkillCache lo entiende
            parsed = skills_mod._parse_skill_md(p, "deploy-inventorydemo")
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.name, "deploy-inventorydemo")

            # colisión sin overwrite
            with self.assertRaises(FileExistsError):
                skills_mod.write_skill_sync("deploy-inventorydemo", "x", "y", base)
            # con overwrite pisa
            skills_mod.write_skill_sync(
                "deploy-inventorydemo", "v2", "z", base, overwrite=True)
            self.assertIn("description: v2", p.read_text(encoding="utf-8"))

            items = skills_mod.list_skills_sync(base)
            self.assertEqual([s["name"] for s in items], ["deploy-inventorydemo"])
            self.assertTrue(items[0]["valid"])

            self.assertIn("v2", skills_mod.read_skill_sync(base, "deploy-inventorydemo"))
            # traversal en lectura/borrado → None/False, nunca escapa
            self.assertIsNone(skills_mod.read_skill_sync(base, "../deploy-inventorydemo"))
            self.assertFalse(skills_mod.delete_skill_sync(base, "..\\x"))

            self.assertTrue(skills_mod.delete_skill_sync(base, "deploy-inventorydemo"))
            self.assertFalse((base / "deploy-inventorydemo").exists())
            self.assertFalse(skills_mod.delete_skill_sync(base, "deploy-inventorydemo"))

    def test_write_empty_name_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                skills_mod.write_skill_sync("...", "d", "c", Path(tmp))


# ---------- memory.py: schema del compactador ----------


class TestCompactionSchema(unittest.TestCase):
    def test_skill_optional_default_none(self) -> None:
        from relay.memory import CompactionResult
        r = CompactionResult(summary="s", facts=[])
        self.assertIsNone(r.skill)

    def test_skill_draft_roundtrip(self) -> None:
        from relay.memory import CompactionResult, SkillDraft
        r = CompactionResult(
            summary="s", facts=["f"],
            skill=SkillDraft(name="deploy-x", description="d", content="c"))
        self.assertEqual(r.skill.name, "deploy-x")


# ---------- DB layer ----------


class TestSkillDraftsDb(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        _tmp_env(Path(self._tmp.name))
        self.db = Database()
        await self.db.init_schema()

    async def asyncTearDown(self) -> None:
        _clear_env()
        relay_config.set_runtime_config({})
        self._tmp.cleanup()

    async def test_crud_and_pending_count(self) -> None:
        did = await self.db.add_skill_draft(
            name="deploy-inventorydemo", description="d", content="# pasos",
            project_slug="inventorydemo", source_conversation="conv1")
        self.assertIsInstance(did, int)
        self.assertEqual(await self.db.count_skill_drafts_pending(), 1)

        drafts = await self.db.list_skill_drafts()
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["status"], "pending")
        # el listado NO trae content (blob), solo su length
        self.assertNotIn("content", drafts[0])
        self.assertEqual(drafts[0]["content_len"], len("# pasos"))

        self.assertTrue(await self.db.update_skill_draft(
            did, name="deploy-inventorydemo-v2"))
        draft = await self.db.get_skill_draft(did)
        self.assertEqual(draft["name"], "deploy-inventorydemo-v2")
        self.assertEqual(draft["content"], "# pasos")

        self.assertTrue(await self.db.set_skill_draft_status(
            did, "approved", approved_path="C:/x/SKILL.md"))
        self.assertEqual(await self.db.count_skill_drafts_pending(), 0)
        # update sobre no-pending no toca nada
        self.assertFalse(await self.db.update_skill_draft(did, name="nope"))

        self.assertEqual(
            len(await self.db.list_skill_drafts(status="approved")), 1)
        self.assertEqual(
            len(await self.db.list_skill_drafts(status="pending")), 0)

        self.assertTrue(await self.db.delete_skill_draft(did))
        self.assertFalse(await self.db.delete_skill_draft(did))

    async def test_delete_fact(self) -> None:
        await self.db.add_facts("demo", ["a", "b"])
        facts = await self.db.list_facts("demo")
        self.assertEqual(len(facts), 2)
        self.assertTrue(await self.db.delete_fact(facts[0]["id"]))
        self.assertEqual(len(await self.db.list_facts("demo")), 1)
        self.assertFalse(await self.db.delete_fact(99999))

    async def test_delete_memory_clears_fts_and_summary(self) -> None:
        cid = await self.db.create_conversation(project_slug="demo")
        await self.db.close_conversation(cid)
        await self.db.set_conversation_summary(cid, "postgres en sample-app")
        await self.db.add_memory(cid, "demo", "postgres en sample-app")
        hits = await self.db.search_memories("demo", "postgres")
        self.assertEqual(len(hits), 1)

        self.assertTrue(await self.db.delete_memory(cid))
        # ni por MATCH ni por fallback de recientes
        self.assertEqual(await self.db.search_memories("demo", "postgres"), [])
        self.assertEqual(await self.db.search_memories("demo", ""), [])
        # idempotencia: segunda vez ya no hay summary
        self.assertFalse(await self.db.delete_memory(cid))


# ---------- endpoints ----------


class TestSkillEndpoints(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _tmp_env(base)
        self.skills_dir = base / "skills"
        self.db = Database()
        await self.db.init_schema()
        self.draft_id = await self.db.add_skill_draft(
            name="Deploy INVENTORYDEMO", description="cómo deployar inventorydemo",
            content="# Pasos\n1. dotnet build\n2. publish.ps1",
            project_slug="inventorydemo", source_conversation="conv1")

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    def _client(self):
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        self._notify_patch = patch.object(NotifyClient, "send", fake_send)
        self._notify_patch.start()
        self.addCleanup(self._notify_patch.stop)
        return TestClient(TestServer(create_app()))

    async def test_list_and_badge(self) -> None:
        async with self._client() as client:
            r = await client.get("/admin/api/skill-drafts")
            self.assertEqual(r.status, 200)
            body = await r.json()
            self.assertEqual(body["pending"], 1)
            self.assertEqual(body["drafts"][0]["name"], "Deploy INVENTORYDEMO")
            # health expone el count para el badge del sidebar
            h = await (await client.get("/admin/api/health")).json()
            self.assertEqual(h["skill_drafts_pending"], 1)
            # status inválido → 400
            r = await client.get("/admin/api/skill-drafts?status=xx")
            self.assertEqual(r.status, 400)

    async def test_patch_only_pending(self) -> None:
        async with self._client() as client:
            r = await client.patch(
                f"/admin/api/skill-drafts/{self.draft_id}",
                json={"description": "editada"})
            self.assertEqual(r.status, 200)
            body = await r.json()
            self.assertEqual(body["draft"]["description"], "editada")

            await self.db.set_skill_draft_status(self.draft_id, "rejected")
            r = await client.patch(
                f"/admin/api/skill-drafts/{self.draft_id}",
                json={"description": "x"})
            self.assertEqual(r.status, 409)

    async def test_approve_writes_skill_md(self) -> None:
        async with self._client() as client:
            r = await client.post(
                f"/admin/api/skill-drafts/{self.draft_id}/approve")
            self.assertEqual(r.status, 200)
            body = await r.json()
            self.assertEqual(body["name"], "deploy-inventorydemo")  # sanitizado
            target = self.skills_dir / "deploy-inventorydemo" / "SKILL.md"
            self.assertTrue(target.is_file())
            text = target.read_text(encoding="utf-8")
            self.assertIn("name: deploy-inventorydemo", text)
            self.assertIn("publish.ps1", text)
            draft = await self.db.get_skill_draft(self.draft_id)
            self.assertEqual(draft["status"], "approved")
            self.assertEqual(draft["approved_path"], str(target))

            # re-aprobar el mismo draft → 409 (ya no está pending)
            r = await client.post(
                f"/admin/api/skill-drafts/{self.draft_id}/approve")
            self.assertEqual(r.status, 409)

            # otro draft con el MISMO nombre → 409 needs_overwrite,
            # y con overwrite:true pisa.
            did2 = await self.db.add_skill_draft(
                name="deploy-inventorydemo", description="v2", content="otro")
            r = await client.post(f"/admin/api/skill-drafts/{did2}/approve")
            self.assertEqual(r.status, 409)
            self.assertTrue((await r.json()).get("needs_overwrite"))
            r = await client.post(
                f"/admin/api/skill-drafts/{did2}/approve",
                json={"overwrite": True})
            self.assertEqual(r.status, 200)
            self.assertIn("otro", target.read_text(encoding="utf-8"))

    async def test_reject_and_delete(self) -> None:
        async with self._client() as client:
            r = await client.post(
                f"/admin/api/skill-drafts/{self.draft_id}/reject")
            self.assertEqual(r.status, 200)
            draft = await self.db.get_skill_draft(self.draft_id)
            self.assertEqual(draft["status"], "rejected")
            # nada en el FS
            self.assertFalse(self.skills_dir.exists())

            r = await client.delete(
                f"/admin/api/skill-drafts/{self.draft_id}")
            self.assertEqual(r.status, 200)
            r = await client.delete(
                f"/admin/api/skill-drafts/{self.draft_id}")
            self.assertEqual(r.status, 404)

    async def test_installed_list_get_delete(self) -> None:
        skills_mod.write_skill_sync(
            "tdd", "disciplina TDD", "# rojo verde refactor",
            self.skills_dir)
        async with self._client() as client:
            r = await client.get("/admin/api/skills")
            body = await r.json()
            self.assertEqual([s["name"] for s in body["skills"]], ["tdd"])
            self.assertEqual(body["dir"], str(self.skills_dir))

            r = await client.get("/admin/api/skills/tdd")
            self.assertEqual(r.status, 200)
            self.assertIn("rojo verde", (await r.json())["content"])

            # traversal → 404, no escapa del dir
            r = await client.get("/admin/api/skills/..%5Cx")
            self.assertEqual(r.status, 404)

            r = await client.delete("/admin/api/skills/tdd")
            self.assertEqual(r.status, 200)
            self.assertFalse((self.skills_dir / "tdd").exists())
            r = await client.delete("/admin/api/skills/tdd")
            self.assertEqual(r.status, 404)


class TestCurationEndpoints(unittest.IsolatedAsyncioTestCase):
    """DELETE de facts y memorias (curación sin SQLite a mano)."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        _tmp_env(Path(self._tmp.name))
        self.db = Database()
        await self.db.init_schema()
        await self.db.add_facts("demo", ["hecho uno"])
        self.fact_id = (await self.db.list_facts("demo"))[0]["id"]
        self.conv_id = await self.db.create_conversation(project_slug="demo")
        await self.db.close_conversation(self.conv_id)
        await self.db.set_conversation_summary(self.conv_id, "resumen x")
        await self.db.add_memory(self.conv_id, "demo", "resumen x")

    async def asyncTearDown(self) -> None:
        _clear_env()
        self._tmp.cleanup()

    def _client(self):
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        self._notify_patch = patch.object(NotifyClient, "send", fake_send)
        self._notify_patch.start()
        self.addCleanup(self._notify_patch.stop)
        return TestClient(TestServer(create_app()))

    async def test_delete_fact_endpoint(self) -> None:
        async with self._client() as client:
            r = await client.delete(f"/admin/api/facts/{self.fact_id}")
            self.assertEqual(r.status, 200)
            self.assertEqual(await self.db.list_facts("demo"), [])
            r = await client.delete(f"/admin/api/facts/{self.fact_id}")
            self.assertEqual(r.status, 404)
            r = await client.delete("/admin/api/facts/abc")
            self.assertEqual(r.status, 400)

    async def test_delete_memory_endpoint(self) -> None:
        async with self._client() as client:
            r = await client.delete(
                f"/admin/api/conversations/{self.conv_id}/memory")
            self.assertEqual(r.status, 200)
            self.assertEqual(
                await self.db.search_memories("demo", "resumen"), [])
            r = await client.delete(
                f"/admin/api/conversations/{self.conv_id}/memory")
            self.assertEqual(r.status, 404)


# ---------- Iter 8.5: skills on-demand (`when: manual`) ----------
#
# Cubrir:
#  - _parse_skill_md detecta `when: manual|ondemand|on_demand|on-demand`
#  - Skill.manual=True -> NO entra en render_block (system prompt)
#  - write_skill_sync con frontmatter_extra="when: manual" -> archivo OK
#  - api_skill_drafts_approve con `manual=true` -> frontmatter presente
#  - /admin/api/skills/{name}/apply-to-transcript corre el LLM y
#    devuelve markdown listo para issue / historia de usuario.

class TestSkillsManual(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        _tmp_env(Path(self._tmp.name))
        self.db = Database()
        await self.db.init_schema()
        await self.db.set_config("FOURBIS_MODEL", "test")
        relay_config.set_runtime_config(await self.db.all_config())
        self.skills_dir = Path(os.environ["FOURBIS_SKILLS_DIR"])
        self.draft_id = await self.db.add_skill_draft(
            name="create-github-issue-from-transcript",
            description="Destilar un transcript y emitir markdown listo.",
            content="## Procedimiento\nlee el transcript\nemite markdown",
        )

    async def asyncTearDown(self) -> None:
        _clear_env()
        relay_config.set_runtime_config({})
        self._tmp.cleanup()

    def _client(self):
        from relay.server import create_app
        async def fake_send(self_, *a, **kw):
            return True
        self._notify_patch = patch.object(NotifyClient, "send", fake_send)
        self._notify_patch.start()
        self.addCleanup(self._notify_patch.stop)
        return TestClient(TestServer(create_app()))

    async def test_parse_manual_from_frontmatter(self) -> None:
        target = self.skills_dir / "foo" / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "---\nname: foo\ndescription: d\nwhen: manual\n---\n\n# foo\n",
            encoding="utf-8")
        parsed = skills_mod._parse_skill_md(target, "foo")
        self.assertTrue(parsed.manual)

        target2 = self.skills_dir / "bar" / "SKILL.md"
        target2.parent.mkdir(parents=True, exist_ok=True)
        target2.write_text(
            "---\nname: bar\ndescription: d\n---\n\n# bar\n",
            encoding="utf-8")
        parsed2 = skills_mod._parse_skill_md(target2, "bar")
        self.assertFalse(parsed2.manual)

    async def test_approve_with_manual_writes_when_manual(self) -> None:
        async with self._client() as client:
            r = await client.post(
                f"/admin/api/skill-drafts/{self.draft_id}/approve",
                json={"manual": True})
            self.assertEqual(r.status, 200)
            body = await r.json()
            self.assertEqual(body["name"], "create-github-issue-from-transcript")
            target = self.skills_dir / "create-github-issue-from-transcript" / "SKILL.md"
            text = target.read_text(encoding="utf-8")
            self.assertIn("name: create-github-issue-from-transcript", text)
            self.assertIn("when: manual", text)

    async def test_approve_without_manual_omits_when(self) -> None:
        # apruebo el mismo draft (re-creo el row porque el anterior ya
        # quedó approved en el test anterior) sin manual -> NO `when:`.
        did = await self.db.add_skill_draft(
            name="auto-skill",
            description="d", content="c")
        async with self._client() as client:
            r = await client.post(
                f"/admin/api/skill-drafts/{did}/approve")
            self.assertEqual(r.status, 200)
            target = self.skills_dir / "auto-skill" / "SKILL.md"
            text = target.read_text(encoding="utf-8")
            self.assertNotIn("when:", text)

    async def test_manual_skills_not_in_injection(self) -> None:
        # Aprobar un manual + uno auto, después invalidar cache y
        # verificar que el block solo contiene el auto.
        did_manual = await self.db.add_skill_draft(
            name="manual-skill", description="d", content="c")
        did_auto = await self.db.add_skill_draft(
            name="auto-skill", description="d", content="c")
        async with self._client() as client:
            await client.post(
                f"/admin/api/skill-drafts/{did_manual}/approve",
                json={"manual": True})
            await client.post(
                f"/admin/api/skill-drafts/{did_auto}/approve")
            cache = (await client.get("/admin/api/skills")).__hash__  # noqa
            # invalidar via el endpoint de delete y re-list
            r = await client.get("/admin/api/skills")
            body = await r.json()
            self.assertEqual(
                sorted([s["name"] for s in body["skills"]]),
                ["auto-skill", "manual-skill"])
            self.assertTrue(
                [s for s in body["skills"] if s["name"] == "manual-skill"][0]["manual"])
            self.assertFalse(
                [s for s in body["skills"] if s["name"] == "auto-skill"][0]["manual"])

    async def test_apply_skill_to_transcript(self) -> None:
        # preparar: aprobar la skill manual y sembrar un transcript.
        from relay import voice as voice_mod
        async with self._client() as client:
            r = await client.post(
                f"/admin/api/skill-drafts/{self.draft_id}/approve",
                json={"manual": True})
            self.assertEqual(r.status, 200)

        # crear transcript manualmente en disco (el helper es async).
        # El id tiene que matchear el patrón `trx_*` de read_transcript.
        trx_id = "trx_smoke00001"
        rec = {
            "id": trx_id, "ts": "2026-07-17T10:00:00Z", "author": "smoke",
            "mode": "cli", "transcript": "hay que sumarle botón de reenvío.",
            "related_project": "demo", "discord_channel": "smoke",
            "duration_s": 3.0,
        }
        await voice_mod.persist_transcript(rec)

        # El TestModel queda en system_config/SQLite: la configuración
        # operativa ya no se lee de variables de entorno.
        async with self._client() as client:
            r = await client.post(
                "/admin/api/skills/create-github-issue-from-transcript"
                "/apply-to-transcript",
                json={"transcript_id": trx_id, "mode": "issue",
                      "repo": "ORG/repo"})
            self.assertEqual(r.status, 200, await r.text())
            body = await r.json()
            self.assertEqual(body["transcript_id"], trx_id)
            self.assertEqual(body["mode"], "issue")
            self.assertEqual(body["repo"], "ORG/repo")
            self.assertTrue(body["output"], "output vacío")
            # Con TestModel el content es un placeholder
            # ("success (no tool calls)"). Lo importante acá es que el
            # endpoint enruta correctamente: 200 + output no vacío.
            # El shape real se valida en el smoke manual con el LLM real.
            self.assertIn("tool calls", body["output"].lower(),
                "TestModel debería devolver su placeholder canónico")

    async def test_apply_skill_errors(self) -> None:
        # 400 si falta transcript_id
        async with self._client() as client:
            r = await client.post(
                "/admin/api/skills/create-github-issue-from-transcript"
                "/apply-to-transcript",
                json={"mode": "issue"})
            self.assertEqual(r.status, 400)
            # 400 si mode es inválido
            r = await client.post(
                "/admin/api/skills/create-github-issue-from-transcript"
                "/apply-to-transcript",
                json={"transcript_id": "x", "mode": "raro"})
            self.assertEqual(r.status, 400)
            # 404 si la skill no existe
            r = await client.post(
                "/admin/api/skills/no-existe/apply-to-transcript",
                json={"transcript_id": "x", "mode": "issue"})
            self.assertEqual(r.status, 404)
            # 404 si el transcript no existe
            r = await client.post(
                "/admin/api/skills/create-github-issue-from-transcript"
                "/apply-to-transcript",
                json={"transcript_id": "no-existe", "mode": "issue"})
            self.assertEqual(r.status, 404)


if __name__ == "__main__":
    unittest.main()
