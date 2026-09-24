from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from relay.db import Database


class TestTeamUsersDb(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(path=Path(self._tmp.name) / "team.db")
        with patch.dict(os.environ, {"RELAY_OWNER_EMAIL": ""}):
            await self.db.init_schema()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_migration_preserves_users_and_is_idempotent(self) -> None:
        path = Path(self._tmp.name) / "legacy.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE users (email TEXT PRIMARY KEY, role TEXT NOT NULL, "
            "created_at TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO users VALUES (?, ?, ?)",
            [("alex@example.test", "owner", "old-date"),
             ("sam@example.test", "finance", "old-date-2")])
        conn.commit()
        conn.close()

        legacy = Database(path=path)
        with patch.dict(os.environ, {"RELAY_OWNER_EMAIL": ""}):
            await legacy.init_schema()
            await legacy.init_schema()
        users = {user["email"]: user for user in await legacy.list_users()}
        self.assertEqual(users["alex@example.test"]["role"], "owner")
        self.assertEqual(users["sam@example.test"]["role"], "finance")
        self.assertEqual(users["alex@example.test"]["created_at"], "old-date")
        self.assertEqual(users["alex@example.test"]["display_name"], "")
        self.assertEqual(users["alex@example.test"]["enabled"], 1)
        self.assertEqual(users["alex@example.test"]["project_slugs"], [])
        self.assertEqual(len(users), 2)

    async def test_set_preserves_name_and_toggles_enabled(self) -> None:
        await self.db.set_user_role(
            " SAM@EXAMPLE.TEST ", "member", display_name="Equipo")
        await self.db.set_user_role("sam@example.test", "finance", enabled=False)
        user = (await self.db.list_users())[0]
        self.assertEqual(user["email"], "sam@example.test")
        self.assertEqual(user["display_name"], "Equipo")
        self.assertEqual(user["role"], "finance")
        self.assertEqual(user["enabled"], 0)

    async def test_manageable_roles_checked_inside_update(self) -> None:
        await self.db.set_user_role("subadmin@example.test", "subadmin")
        with self.assertRaisesRegex(ValueError, "^forbidden_role$"):
            await self.db.set_user_role(
                "subadmin@example.test", "member", manageable_roles=("member",))
        with self.assertRaisesRegex(ValueError, "^forbidden_role$"):
            await self.db.set_user_role(
                "nueva@example.test", "owner", manageable_roles=("member",))
        await self.db.set_user_role(
            "nueva@example.test", "member", manageable_roles=("member",))

    async def test_only_one_concurrent_owner_demotion_can_succeed(self) -> None:
        await self.db.set_user_role("alex@example.test", "owner")
        await self.db.set_user_role("sam@example.test", "owner")
        outcomes = await asyncio.gather(
            self.db.set_user_role("alex@example.test", "finance"),
            self.db.set_user_role("sam@example.test", "finance"),
            return_exceptions=True)
        self.assertEqual(sum(result is None for result in outcomes), 1)
        failures = [result for result in outcomes if isinstance(result, Exception)]
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ValueError)
        self.assertEqual(str(failures[0]), "last_admin")
        self.assertEqual(
            sum(user["role"] == "owner" and user["enabled"]
                for user in await self.db.list_users()), 1)

    async def test_cannot_disable_or_delete_last_active_owner(self) -> None:
        await self.db.set_user_role("alex@example.test", "owner")
        for operation in (
            self.db.set_user_role("alex@example.test", "owner", enabled=False),
            self.db.delete_user("alex@example.test"),
        ):
            with self.assertRaisesRegex(ValueError, "^last_admin$"):
                await operation


if __name__ == "__main__":
    unittest.main()
