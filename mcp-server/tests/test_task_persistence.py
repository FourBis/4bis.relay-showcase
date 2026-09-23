from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path

from relay.db import Database


class TestTaskPersistence(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "relay.db"
        self.db = Database(path=self.path)
        await self.db.init_schema()
        await self.db.upsert_project({"slug": "demo", "name": "Demo", "repo_path": self.tmp.name})
        self.conv = await self.db.create_conversation(project_slug="demo", conversation_id="conv-1")

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_task_patch_and_event_idempotency(self) -> None:
        self.assertEqual(await self.db.update_conversation_task(
            self.conv, mode="write", limits={"max_files": 4, "max_bytes": 100}),
            {"mode": "write", "limits": {"max_files": 4, "max_bytes": 100}})
        self.assertEqual(await self.db.update_conversation_task(
            self.conv, tracking={"enabled": False}),
            {"mode": "write", "limits": {"max_files": 4, "max_bytes": 100},
             "tracking": {"enabled": False}})
        first = await self.db.enqueue_conversation_event(
            self.conv, "r1", "run", {"user": "hola", "source": "task"})
        again = await self.db.enqueue_conversation_event(
            self.conv, "r1", "run", {"source": "task", "user": "hola"})
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(first["chat_id"], again["chat_id"])
        with self.assertRaises(ValueError):
            await self.db.enqueue_conversation_event(self.conv, "r1", "run", {"user": "otro"})

    async def test_concurrent_enqueue_has_one_chat_and_rollback_is_atomic(self) -> None:
        other = Database(path=self.path)
        results = await asyncio.gather(*[
            db.enqueue_conversation_event(self.conv, "same", "run", {"user": "hola"})
            for db in (self.db, other)
        ])
        self.assertEqual({item["id"] for item in results}, {results[0]["id"]})
        self.assertEqual(len(await self.db.run(
            "SELECT id FROM chats WHERE conversation_id=?", (self.conv,))), 1)

        chat_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.conv}/broken"))
        await self.db.run(
            "INSERT INTO chats (id, source, started_at, status, conversation_id) VALUES (?, 'test', ?, 'queued', ?)",
            (chat_id, "2026-01-01T00:00:00Z", self.conv))
        with self.assertRaises(Exception):
            await self.db.enqueue_conversation_event(self.conv, "broken", "run", {"user": "falla"})
        self.assertEqual(await self.db.list_conversation_events(self.conv, states=("pending",)),
                         [results[0]])

    async def test_fifo_single_claim_and_restart_recovery(self) -> None:
        await self.db.enqueue_conversation_event(self.conv, "r1", "run", {"user": "uno"})
        await self.db.enqueue_conversation_event(self.conv, "f1", "feedback", {"user": "dos"})
        claims = await asyncio.gather(
            self.db.claim_conversation_event(self.conv),
            Database(path=self.path).claim_conversation_event(self.conv),
        )
        self.assertEqual(sum(item is not None for item in claims), 1)
        await self.db.recover_conversation_events()
        rows = await self.db.list_conversation_events(self.conv)
        self.assertEqual(rows[0]["state"], "uncertain")
        second = await self.db.claim_conversation_event(self.conv)
        self.assertIsNone(second)
        await self.db.cancel_pending_conversation_events(self.conv)
        rows = await self.db.list_conversation_events(self.conv)
        self.assertEqual([row["state"] for row in rows], ["uncertain", "cancelled"])

    async def test_feedback_during_processing_survives_restart_and_finish_respects_terminal_chat(self) -> None:
        run = await self.db.enqueue_conversation_event(self.conv, "run", "run", {"user": "uno"})
        claimed = await self.db.claim_conversation_event(self.conv)
        self.assertEqual(claimed["id"], run["id"])
        feedback = await self.db.enqueue_conversation_event(
            self.conv, "feedback", "feedback", {"user": "corrige"})
        self.assertIsNone(await self.db.claim_conversation_event(self.conv))
        self.assertEqual(await self.db.recover_conversation_events(), [self.conv])
        self.assertEqual(
            [(row["event_key"], row["state"]) for row in await self.db.list_conversation_events(self.conv)],
            [("run", "uncertain"), ("feedback", "pending")])
        await self.db.run("UPDATE chats SET status='error' WHERE id=?", (run["chat_id"],))
        await self.db.finish_conversation_event(run["id"], state="applied")
        self.assertEqual((await self.db.get_chat(run["chat_id"]))["status"], "error")
        await self.db.finish_conversation_event(feedback["id"], state="cancelled")
        chat = await self.db.get_chat(feedback["chat_id"])
        self.assertEqual(chat["status"], "cancelled")

    async def test_migration_is_repeatable_and_managed_listing(self) -> None:
        await self.db.init_schema()
        await self.db.update_conversation_task(self.conv, workspace_path="C:/repo")
        managed = await self.db.list_managed_conversations()
        self.assertEqual(managed[0]["task_json"]["workspace_path"], "C:/repo")
        self.assertEqual(await self.db.stale_open_conversations(0), [])


if __name__ == "__main__":
    unittest.main()
