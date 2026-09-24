from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

import httpx
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from relay import user_mail
from relay.app_state import DB_KEY
from relay.db import Database
from relay.user_accounts import AccountError, current_actor


class _Store:
    def __init__(self, db):
        self.db = db
        self.invalidated = []
        self.mail_drafts = {}

    async def invalidate(self, provider, email):
        self.invalidated.append((provider, email))

    async def check_user(self, provider, email):
        return {"email": email}


class _Response:
    def __init__(self, data: dict, status: int = 200):
        self.status_code = status
        self.is_success = 200 <= status < 300
        self._data = data

    def json(self):
        return self._data


class _Client:
    def __init__(self, handler, calls):
        self.handler = handler
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return await self.handler(method, url, **kwargs)


class TestUserMail(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(path=Path(self._tmp.name) / "mail.db")
        with patch.dict(os.environ, {"RELAY_OWNER_EMAIL": ""}):
            await self.db.init_schema()
        await self.db.run(
            "INSERT OR IGNORE INTO users(email, role, display_name, enabled, created_at) "
            "VALUES ('alex@example.test', 'owner', 'Alice', 1, 'test'), "
            "('sam@example.test', 'member', 'Bob', 1, 'test')")
        await self.db.run(
            "CREATE TABLE IF NOT EXISTS account_mail_sends ("
            "email TEXT NOT NULL, request_id TEXT NOT NULL, state TEXT NOT NULL, "
            "payload_hash TEXT NOT NULL, message_id TEXT NOT NULL DEFAULT '', "
            "created_at REAL NOT NULL, PRIMARY KEY(email, request_id))")
        self.store = _Store(self.db)

        @web.middleware
        async def test_actor(request, handler):
            token = current_actor.set((self.store, request.headers.get("X-Test-Actor", "")))
            try:
                return await handler(request)
            finally:
                current_actor.reset(token)

        self.app = web.Application(middlewares=[test_actor])
        self.app[DB_KEY] = self.db
        user_mail.register_routes(self.app)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.accounts = {
            "alex@example.test": {
                "access_token": "token-A", "subject": "google-A",
                "login": "alex", "email": "alex@example.test", "scopes": []},
            "sam@example.test": {
                "access_token": "token-B", "subject": "google-B",
                "login": "sam", "email": "sam@example.test", "scopes": []},
        }
        async def require_account(provider):
            self.assertEqual(provider, "google")
            actor = current_actor.get()
            if actor is None or actor[1] not in self.accounts:
                raise AccountError("disabled")
            return self.accounts[actor[1]]
        self.require_account_patch = patch.object(
            user_mail, "require_account", require_account)
        self.require_account_patch.start()

    async def asyncTearDown(self):
        self.require_account_patch.stop()
        await self.client.close()
        self._tmp.cleanup()

    def _mock_google(self, handler):
        calls = []
        patcher = patch.object(
            user_mail.httpx, "AsyncClient",
            side_effect=lambda **kwargs: _Client(handler, calls))
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def _headers(self, email="alex@example.test"):
        return {"X-Test-Actor": email}

    async def test_read_is_bound_to_actor_and_returns_plain_text(self):
        calls = []
        plain = base64.urlsafe_b64encode("Mensaje de Ana".encode()).decode().rstrip("=")

        async def handler(method, url, headers, params=None, json=None):
            token = headers["Authorization"]
            calls.append(token)
            if url.endswith("/messages"):
                return _Response({"messages": [{"id": token[-1]}]})
            if params.get("format") == "metadata":
                return _Response({
                    "id": token[-1], "snippet": "vista previa",
                    "payload": {"headers": [
                        {"name": "From", "value": f"{token[-1]}@example.test"},
                        {"name": "Subject", "value": "Hola"},
                        {"name": "Date", "value": "ayer"}]}})
            return _Response({"id": token[-1], "payload": {
                "mimeType": "text/plain", "body": {"data": plain}}})

        google_calls = self._mock_google(handler)
        for actor, token in (("alex@example.test", "token-A"),
                             ("sam@example.test", "token-B")):
            listed = await self.client.get(
                "/admin/api/account/gmail/messages?q=from%3Ateam&limit=5",
                headers=self._headers(actor))
            self.assertEqual(listed.status, 200)
            self.assertEqual((await listed.json())["messages"][0]["id"], token[-1])
            self.assertEqual(listed.headers["Cache-Control"], "no-store")
            detail = await self.client.get(
                f"/admin/api/account/gmail/messages/id-{token[-1]}",
                headers=self._headers(actor))
            self.assertEqual((await detail.json())["body"], "Mensaje de Ana")
        self.assertEqual(calls, ["Bearer token-A"] * 3 + ["Bearer token-B"] * 3)
        self.assertEqual(len(google_calls), 6)

    async def test_mcp_can_read_one_full_message_and_reject_bad_ids(self):
        data = base64.urlsafe_b64encode(b"cuerpo completo").decode().rstrip("=")
        async def handler(*args, **kwargs):
            return _Response({"id": "mail-1", "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": "Tema"}],
                "body": {"data": data}}})
        calls = self._mock_google(handler)
        token = current_actor.set((self.store, "alex@example.test"))
        try:
            result = await user_mail.mcp_read_mail({"message_id": "mail-1"})
            invalid = await user_mail.mcp_read_mail({"message_id": "bad/id"})
            invalid_limit = await user_mail.mcp_read_mail({"max_results": None})
        finally:
            current_actor.reset(token)
        self.assertEqual(result["message"]["body"], "cuerpo completo")
        self.assertEqual(result["message"]["subject"], "Tema")
        self.assertFalse(invalid["ok"])
        self.assertFalse(invalid_limit["ok"])
        self.assertEqual(len(calls), 1)

    async def test_send_uses_actor_token_and_from_and_idempotency(self):
        posted = []

        async def handler(method, url, headers, params=None, json=None):
            self.assertEqual(method, "POST")
            posted.append(headers["Authorization"])
            raw = base64.urlsafe_b64decode(json["raw"])
            message = BytesParser(policy=policy.default).parsebytes(raw)
            return _Response({"id": message["From"]})

        calls = self._mock_google(handler)
        request = {"request_id": "05a7fb71-f0ae-420f-9692-88e7d17c1b2c",
                   "to": ["recipient@example.test"], "cc": ["copy@example.test"],
                   "bcc": ["hidden@example.test"], "subject": "Reunión",
                   "body": "Hola"}
        response = await self.client.post(
            "/admin/api/account/gmail/send", json=request,
            headers=self._headers())
        sent = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(sent["state"], "sent")
        self.assertEqual(sent["message_id"], "alex@example.test")
        self.assertFalse(sent["replayed"])

        replay = await self.client.post(
            "/admin/api/account/gmail/send", json=request,
            headers=self._headers())
        self.assertEqual(replay.status, 200)
        self.assertTrue((await replay.json())["replayed"])
        self.assertEqual(posted, ["Bearer token-A"])
        self.assertEqual(len(calls), 1)

        changed = {**request, "body": "otro cuerpo"}
        conflict = await self.client.post(
            "/admin/api/account/gmail/send", json=changed,
            headers=self._headers())
        self.assertEqual(conflict.status, 409)
        self.assertEqual((await conflict.json())["error"], "request_id_conflict")

    async def test_send_marks_timeout_uncertain_and_blocks_retry(self):
        calls = self._mock_google(
            lambda *a, **k: self._timeout())
        request = {"request_id": "c410d345-a87f-47de-953f-d8930912843f",
                   "to": "recipient@example.test", "subject": "Hola", "body": "texto"}
        response = await self.client.post(
            "/admin/api/account/gmail/send", json=request,
            headers=self._headers())
        self.assertEqual(response.status, 504, await response.text())
        self.assertEqual((await response.json())["error"], "delivery_uncertain")
        retry = await self.client.post(
            "/admin/api/account/gmail/send", json=request,
            headers=self._headers())
        self.assertEqual(retry.status, 409)
        self.assertEqual((await retry.json())["error"], "delivery_uncertain")
        self.assertEqual(len(calls), 1)

    async def test_concurrent_duplicate_only_claims_one_send(self):
        calls = []
        async def handler(*args, **kwargs):
            calls.append(1)
            await asyncio.sleep(0.05)
            return _Response({"id": "sent-1"})
        self._mock_google(handler)
        request = {"request_id": "e4f63d75-97ba-44fa-971d-d45281da0340",
                   "to": ["recipient@example.test"], "subject": "Hola", "body": "texto"}
        async def send():
            return await self.client.post(
                "/admin/api/account/gmail/send", json=request,
                headers=self._headers())
        responses = await asyncio.gather(send(), send())
        self.assertEqual(sorted(response.status for response in responses), [200, 409])
        self.assertEqual(len(calls), 1)

    async def _timeout(self):
        raise httpx.TimeoutException("red de prueba")

    async def test_newline_header_injection_rejected_before_google(self):
        calls = self._mock_google(
            lambda *a, **k: self.fail("no se debe llamar a Gmail"))
        response = await self.client.post(
            "/admin/api/account/gmail/send", json={
                "request_id": "e1869e1a-c022-47d4-a83c-946d15ef882d",
                "to": ["recipient@example.test\r\nBcc: attacker@example.test"],
                "subject": "Hola", "body": "texto"},
            headers=self._headers())
        self.assertEqual(response.status, 400)
        self.assertEqual(len(calls), 0)

    async def test_disabled_account_is_rejected_without_google_call(self):
        async def disabled(provider):
            raise AccountError("disabled")
        async def disabled_user(provider, email):
            raise AccountError("disabled")
        with patch.object(user_mail, "require_account", disabled):
            calls = self._mock_google(
                lambda *a, **k: self.fail("no se debe llamar a Gmail"))
            response = await self.client.get(
                "/admin/api/account/gmail/messages", headers=self._headers())
        with patch.object(self.store, "check_user", disabled_user):
            drafts = await self.client.get(
                "/admin/api/account/gmail/drafts", headers=self._headers())
        self.assertEqual(response.status, 409)
        self.assertEqual(drafts.status, 409)
        self.assertEqual(len(calls), 0)

    async def test_mcp_tools_are_actor_bound_and_send_only_creates_private_draft(self):
        from relay.tools.gmail import GmailReadTool, GmailSendTool

        read_result = await GmailReadTool().call({})
        self.assertFalse(read_result["ok"])
        self.assertIn("conecta", read_result["error"].lower())

        async def handler(*args, **kwargs):
            return _Response({"messages": []})
        calls = self._mock_google(handler)
        request = {"to": ["recipient@example.test"], "cc": [], "bcc": [],
                   "subject": "Borrador", "body": "revisar"}
        token = current_actor.set((self.store, "alex@example.test"))
        try:
            result = await GmailSendTool().call(request)
        finally:
            current_actor.reset(token)
        self.assertTrue(result["ok"])
        self.assertEqual(result["draft"]["to"], ["recipient@example.test"])
        self.assertEqual(calls, [])

        for actor in ("alex@example.test", "sam@example.test"):
            response = await self.client.get(
                "/admin/api/account/gmail/drafts", headers=self._headers(actor))
            items = (await response.json())["drafts"]
            self.assertEqual(len(items), 1 if actor.startswith("alex") else 0)


if __name__ == "__main__":
    unittest.main()
