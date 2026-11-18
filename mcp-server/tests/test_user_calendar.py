"""Calendar personal y respuestas en hilo: el flujo de solicitudes de reunión."""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, time as dtime, timedelta
from email import policy
from email.parser import BytesParser
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from relay import user_calendar, user_mail
from relay.app_state import DB_KEY
from relay.db import Database
from relay.user_accounts import GOOGLE_CALENDAR_SCOPES, GOOGLE_SCOPES, AccountError, current_actor

SANTIAGO = ZoneInfo("America/Santiago")
CAL = sorted(GOOGLE_SCOPES | GOOGLE_CALENDAR_SCOPES)
REQUEST_ID = "05a7fb71-f0ae-420f-9692-88e7d17c1b2c"


class _Store:
    def __init__(self, db):
        self.db = db
        self.invalidated = []
        self.mail_drafts = {}
        self.event_drafts = {}

    async def invalidate(self, provider, email):
        self.invalidated.append((provider, email))

    async def check_user(self, provider, email):
        return {"email": email}


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db = Database(path=tmp_path / "cal.db")
    with patch.dict(os.environ, {"RELAY_OWNER_EMAIL": ""}):
        await db.init_schema()
    await db.run("INSERT OR IGNORE INTO users(email, role, display_name, enabled, created_at) "
                 "VALUES ('alice@example.com', 'owner', 'Alice', 1, 'test')")
    store = _Store(db)
    accounts = {"alice@example.com": {"access_token": "token-A", "subject": "g-A", "login": "alice",
                                      "email": "alice@gmail.com", "scopes": CAL}}

    async def require_account(provider):
        actor = current_actor.get()
        if actor is None or actor[1] not in accounts:
            raise AccountError("disabled")
        return accounts[actor[1]]

    monkeypatch.setattr(user_mail, "require_account", require_account)
    monkeypatch.setattr(user_calendar, "require_account", require_account)

    @web.middleware
    async def actor(request, handler):
        token = current_actor.set((store, request.headers.get("X-Test-Actor", "")))
        try:
            return await handler(request)
        finally:
            current_actor.reset(token)

    app = web.Application(middlewares=[actor])
    app[DB_KEY] = db
    user_mail.register_routes(app)
    user_calendar.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    seen: list[httpx.Request] = []
    state = {"handler": None}
    original = httpx.AsyncClient

    def transport(request):
        seen.append(request)
        return state["handler"](request)

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(transport)))
    yield {"client": client, "store": store, "accounts": accounts, "seen": seen, "state": state}
    await client.close()


H = {"X-Test-Actor": "alice@example.com"}


def _as_actor(env):
    return current_actor.set((env["store"], "alice@example.com"))


# --- Validación y huecos -----------------------------------------------------

def test_event_needs_explicit_offset_and_defaults_meet_unless_in_person():
    with pytest.raises(ValueError, match="needs_offset"):
        user_calendar.validate_event({"summary": "x", "start": "2026-11-23T11:00:00",
                                      "end": "2026-11-23T12:00:00"})
    online = user_calendar.validate_event({
        "summary": "Demo", "start": "2026-11-23T11:00:00-03:00", "end": "2026-11-23T12:00:00-03:00",
        "attendees": ["Ana <ANA@example.test>", "ana@example.test"]})
    assert online["meet"] is True and online["attendees"] == ["ana@example.test"]
    office = user_calendar.validate_event({
        "summary": "Demo", "start": "2026-11-23T11:00:00-03:00", "end": "2026-11-23T12:00:00-03:00",
        "location": "Oficina central"})
    assert office["meet"] is False
    with pytest.raises(ValueError, match="invalid_event_range"):
        user_calendar.validate_event({"summary": "x", "start": "2026-11-23T12:00:00-03:00",
                                      "end": "2026-11-23T11:00:00-03:00"})
    with pytest.raises(ValueError, match="invalid_event_summary"):
        user_calendar.validate_event({"summary": "a\nBcc: x", "start": "2026-11-23T11:00:00-03:00",
                                      "end": "2026-11-23T12:00:00-03:00"})


def test_free_slots_respect_busy_blocks_hours_and_weekends():
    day = datetime(2026, 11, 23, tzinfo=SANTIAGO)  # lunes
    busy = [(day.replace(hour=10), day.replace(hour=11, minute=30)),
            (day.replace(hour=11), day.replace(hour=12)),  # solapado
            (day.replace(hour=15), day.replace(hour=15, minute=30))]
    slots = user_calendar.free_slots(
        busy, day, day + timedelta(days=6), tz=SANTIAGO, duration=timedelta(hours=1),
        day_start=dtime(9), day_end=dtime(19), weekends=False, now=day)
    first_day = [s for s in slots if s["start"].startswith("2026-11-23")]
    assert first_day == [
        {"start": "2026-11-23T09:00:00-03:00", "end": "2026-11-23T10:00:00-03:00"},
        {"start": "2026-11-23T12:00:00-03:00", "end": "2026-11-23T15:00:00-03:00"},
        {"start": "2026-11-23T15:30:00-03:00", "end": "2026-11-23T19:00:00-03:00"}]
    assert not any(s["start"].startswith(("2026-11-28", "2026-11-29")) for s in slots)


def test_free_slots_never_offer_the_past():
    now = datetime(2026, 11, 23, 16, 0, tzinfo=SANTIAGO)
    slots = user_calendar.free_slots(
        [], now.replace(hour=0), now.replace(hour=23), tz=SANTIAGO, duration=timedelta(hours=1),
        day_start=dtime(9), day_end=dtime(19), weekends=False, now=now)
    assert slots == [{"start": "2026-11-23T16:00:00-03:00", "end": "2026-11-23T19:00:00-03:00"}]


# --- Tools del chat ----------------------------------------------------------

async def test_free_slots_tool_reads_actor_calendar_and_ignores_free_or_declined(env):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer token-A"
        assert request.url.path == "/calendar/v3/calendars/primary/events"
        return httpx.Response(200, json={"timeZone": "America/Santiago", "items": [
            {"start": {"dateTime": "2099-11-23T11:00:00-03:00"}, "end": {"dateTime": "2099-11-23T12:00:00-03:00"}},
            {"start": {"dateTime": "2099-11-23T14:00:00-03:00"}, "end": {"dateTime": "2099-11-23T15:00:00-03:00"},
             "transparency": "transparent"},
            {"start": {"dateTime": "2099-11-23T16:00:00-03:00"}, "end": {"dateTime": "2099-11-23T17:00:00-03:00"},
             "attendees": [{"self": True, "responseStatus": "declined"}]}]})
    env["state"]["handler"] = handler
    token = _as_actor(env)
    try:
        # Rango futuro fijo: "ahora" no recorta nada.
        result = await user_calendar.mcp_calendar_free_slots({
            "time_min": "2099-11-23T00:00:00-03:00", "time_max": "2099-11-23T23:59:00-03:00",
            "duration_minutes": 60})
    finally:
        current_actor.reset(token)
    assert result["ok"], result
    assert result["slots"] == [
        {"start": "2099-11-23T09:00:00-03:00", "end": "2099-11-23T11:00:00-03:00"},
        {"start": "2099-11-23T12:00:00-03:00", "end": "2099-11-23T19:00:00-03:00"}]


async def test_calendar_tools_ask_reconnect_without_scope(env):
    env["accounts"]["alice@example.com"]["scopes"] = sorted(GOOGLE_SCOPES)
    env["state"]["handler"] = lambda request: pytest.fail("no debe llamar a Google")
    token = _as_actor(env)
    try:
        listed = await user_calendar.mcp_calendar_events({
            "time_min": "2026-11-23T00:00:00-03:00", "time_max": "2026-11-24T00:00:00-03:00"})
        draft = await user_mail.mcp_create_mail_draft({
            "to": ["ana@example.test"], "subject": "Re: Reunión", "body": "ok",
            "event": {"summary": "x", "start": "2026-11-23T11:00:00-03:00", "end": "2026-11-23T12:00:00-03:00"}})
    finally:
        current_actor.reset(token)
    assert not listed["ok"] and "Calendar" in listed["error"]
    assert not draft["ok"] and "Calendar" in draft["error"]
    assert env["store"].mail_drafts == {}


async def test_meeting_prepare_builds_threaded_draft_with_event_and_never_sends(env):
    from relay.account_tools import account_tools
    from relay.user_accounts import bind_actor

    def handler(request):
        assert request.method == "GET", "preparar no envía ni crea"
        return httpx.Response(200, json={"id": "m1", "threadId": "t1", "payload": {"headers": [
            {"name": "Message-ID", "value": "<abc@example.test>"}, {"name": "Subject", "value": "Reunión"}]}})
    env["state"]["handler"] = handler
    # bind_actor usa store_for(db); el test inyecta nuestro store en su lugar.
    env["store"].db._account_store = env["store"]
    with bind_actor(env["store"].db, "alice@example.com"):
        tools = {tool.name: tool.function for tool in account_tools({})}
        result = json.loads(await tools["meeting_prepare"](
            "m1", ["ana@example.test"], "Re: Reunión", "Ana, agendemos el lunes 23 a las 11:00.",
            "Demo con Ana", "2026-11-23T11:00:00-03:00", "2026-11-23T12:00:00-03:00"))
    assert result["ok"], result
    draft = result["draft"]
    assert draft["reply_to"] == "m1"
    assert draft["event"]["attendees"] == ["ana@example.test"] and draft["event"]["meet"] is True
    assert all(r.method == "GET" for r in env["seen"])
    listed = await env["client"].get("/admin/api/account/gmail/drafts", headers=H)
    assert (await listed.json())["drafts"][0]["event"]["summary"] == "Demo con Ana"


async def test_private_consultations_get_no_personal_calendar(env):
    from relay.account_tools import account_tools
    from relay.user_accounts import bind_actor
    env["store"].db._account_store = env["store"]
    with bind_actor(env["store"].db, "alice@example.com"):
        names = {t.name for t in account_tools({"defaults_json": {"mcp_private": True}})}
        public = {t.name for t in account_tools({})}
    assert not {"calendar_events", "calendar_free_slots", "meeting_prepare",
                "calendar_prepare_event", "gmail_prepare"} & names
    assert {"calendar_events", "calendar_free_slots", "meeting_prepare", "calendar_prepare_event"} <= public


def test_meeting_guidance_carries_current_date():
    from relay.account_tools import meeting_guidance
    text = meeting_guidance(datetime(2026, 11, 17, 13, 56))
    assert "Tuesday 2026-11-17" in text and "ya pasó" in text


# --- Respuesta en hilo --------------------------------------------------------

async def test_send_reply_goes_into_original_thread(env):
    posted = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"id": "m1", "threadId": "t1", "payload": {"headers": [
                {"name": "Message-ID", "value": "<abc@example.test>"},
                {"name": "References", "value": "<root@example.test> bogus"}]}})
        posted.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "sent-1"})
    env["state"]["handler"] = handler
    response = await env["client"].post("/admin/api/account/gmail/send", headers=H, json={
        "request_id": REQUEST_ID, "to": ["ana@example.test"], "subject": "Re: Reunión",
        "body": "Confirmado", "reply_to": "m1"})
    assert response.status == 200, await response.text()
    assert posted["threadId"] == "t1"
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(posted["raw"]))
    assert message["In-Reply-To"] == "<abc@example.test>"
    assert message["References"] == "<root@example.test> <abc@example.test>"


async def test_reply_lookup_failure_sends_nothing_and_leaves_no_claim(env):
    env["state"]["handler"] = lambda request: httpx.Response(404, json={})
    body = {"request_id": REQUEST_ID, "to": ["ana@example.test"], "subject": "Re: x", "body": "y",
            "reply_to": "m404"}
    response = await env["client"].post("/admin/api/account/gmail/send", headers=H, json=body)
    assert response.status == 502 and (await response.json())["error"] == "reply_unavailable"
    assert [r.method for r in env["seen"]] == ["GET"]
    rows = await env["store"].db.run("SELECT * FROM account_mail_sends")
    assert rows == []


# --- Crear el evento ----------------------------------------------------------

EVENT = {"request_id": REQUEST_ID, "summary": "Demo", "start": "2026-11-23T11:00:00-03:00",
         "end": "2026-11-23T12:00:00-03:00", "attendees": ["ana@example.test"], "meet": True}


async def test_create_event_invites_with_meet_and_deterministic_id(env):
    def handler(request):
        assert request.method == "POST"
        assert request.url.params["sendUpdates"] == "all"
        assert request.url.params["conferenceDataVersion"] == "1"
        body = json.loads(request.content)
        assert body["id"] == "05a7fb71f0ae420f969288e7d17c1b2c"
        assert body["conferenceData"]["createRequest"]["requestId"] == REQUEST_ID
        assert body["attendees"] == [{"email": "ana@example.test"}]
        return httpx.Response(200, json={**body, "htmlLink": "https://calendar.google.com/e",
                                         "conferenceData": {"entryPoints": [
                                             {"entryPointType": "video", "uri": "https://meet.google.com/x"}]}})
    env["state"]["handler"] = handler
    response = await env["client"].post("/admin/api/account/calendar/events", headers=H, json=EVENT)
    data = await response.json()
    assert response.status == 200, data
    assert data["state"] == "created" and not data["replayed"]
    assert data["meet_link"] == "https://meet.google.com/x"


async def test_retry_after_uncertain_creation_replays_instead_of_duplicating(env):
    calls = []

    def handler(request):
        calls.append(request.method)
        if len(calls) == 1:
            raise httpx.ReadTimeout("lento", request=request)
        if request.method == "POST":
            return httpx.Response(409, json={})
        assert request.url.path.endswith("/events/05a7fb71f0ae420f969288e7d17c1b2c")
        return httpx.Response(200, json={"id": "05a7fb71f0ae420f969288e7d17c1b2c", "summary": "Demo"})
    env["state"]["handler"] = handler
    first = await env["client"].post("/admin/api/account/calendar/events", headers=H, json=EVENT)
    assert first.status == 504
    assert (await first.json()) == {"error": "creation_uncertain", "retry_safe": True}
    retry = await env["client"].post("/admin/api/account/calendar/events", headers=H, json=EVENT)
    data = await retry.json()
    assert retry.status == 200 and data["replayed"] is True
    assert calls == ["POST", "POST", "GET"]


async def test_create_event_rejects_missing_scope_and_bad_input_without_google(env):
    env["state"]["handler"] = lambda request: pytest.fail("no debe llamar a Google")
    bad = await env["client"].post("/admin/api/account/calendar/events", headers=H,
                                   json={**EVENT, "start": "2026-11-23T11:00:00"})
    assert bad.status == 400 and (await bad.json())["error"].startswith("invalid_event_start")
    extra = await env["client"].post("/admin/api/account/calendar/events", headers=H,
                                     json={**EVENT, "calendarId": "otro"})
    assert extra.status == 400
    env["accounts"]["alice@example.com"]["scopes"] = sorted(GOOGLE_SCOPES)
    scope = await env["client"].post("/admin/api/account/calendar/events", headers=H, json=EVENT)
    assert scope.status == 409 and (await scope.json())["error"] == "calendar_scope_required"


async def test_event_only_drafts_are_private_to_their_owner(env):
    token = _as_actor(env)
    try:
        result = await user_calendar.mcp_prepare_event({
            "summary": "Dentista", "start": "2026-11-24T15:00:00-03:00", "end": "2026-11-24T16:00:00-03:00",
            "location": "Clínica"})
    finally:
        current_actor.reset(token)
    assert result["ok"] and result["event"]["meet"] is False
    mine = await env["client"].get("/admin/api/account/calendar/drafts", headers=H)
    other = await env["client"].get("/admin/api/account/calendar/drafts", headers={"X-Test-Actor": "bob@example.com"})
    assert len((await mine.json())["drafts"]) == 1
    assert (await other.json())["drafts"] == []
