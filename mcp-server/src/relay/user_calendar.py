"""Google Calendar personal, autenticado por la cuenta OAuth del actor actual.

El chat puede consultar disponibilidad y preparar eventos; crear el evento
(con invitación a los asistentes) es siempre una acción de la persona en
Mi cuenta, igual que enviar un correo.

Idempotencia: el id del evento se deriva del request_id que genera la UI.
Un reintento tras un timeout choca con 409 en Google en vez de duplicar la
reunión, así que reintentar una creación incierta es seguro.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, time as dtime, timedelta
from email import policy
from email.message import EmailMessage
from time import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from aiohttp import web

from .admin_users import _body
from .user_accounts import GOOGLE_CALENDAR_SCOPES, AccountError, current_actor, require_account

_CAL_API = "https://www.googleapis.com/calendar/v3/calendars/primary"
_DRAFT_TTL = 60 * 60
_DRAFTS_PER_USER = 20
_MAX_EVENT_HOURS = 24
_MAX_RANGE_DAYS = 31
_MAX_EVENTS = 250
_EVENT_FIELDS = {"summary", "start", "end", "time_zone", "attendees",
                 "location", "description", "meet"}
_CREATE_FIELDS = _EVENT_FIELDS | {"request_id"}
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class _Unauthorized(Exception):
    pass


class _Forbidden(Exception):
    pass


class _Conflict(Exception):
    pass


class _Failure(Exception):
    pass


class _Timeout(Exception):
    pass


def _json(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


def has_calendar_scope(account: dict) -> bool:
    return GOOGLE_CALENDAR_SCOPES <= set(account.get("scopes") or [])


# --- Validación -----------------------------------------------------------

def _aware(value, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError(f"invalid_event_{field}")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"invalid_event_{field}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # Sin zona, "11:00" es ambiguo entre la máquina, Google y el invitado.
        raise ValueError(f"invalid_event_{field}_needs_offset")
    return parsed


def _zone(value) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid_event_time_zone")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("invalid_event_time_zone") from None
    return value


def _text(value, field: str, limit: int, *, required=False, single_line=False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"invalid_event_{field}")
    value = value.strip()
    if (required and not value) or (single_line and ("\r" in value or "\n" in value)):
        raise ValueError(f"invalid_event_{field}")
    return value


def _attendees(value) -> list[str]:
    if value in (None, ""):
        return []
    values = [value] if isinstance(value, str) else value
    if (not isinstance(values, list) or len(values) > 50
            or any(not isinstance(item, str) or not item.strip() or len(item) > 320
                   or "\r" in item or "\n" in item for item in values)):
        raise ValueError("invalid_event_attendees")
    out: list[str] = []
    for item in values:
        probe = EmailMessage(policy=policy.SMTP)
        probe["To"] = item.strip()
        parsed = getattr(probe["To"], "addresses", ())
        if len(parsed) != 1 or probe["To"].defects or "@" not in parsed[0].addr_spec:
            raise ValueError("invalid_event_attendees")
        address = parsed[0].addr_spec.lower()
        if address not in out:
            out.append(address)
    return out


def validate_event(arguments: Any, *, allowed=_EVENT_FIELDS) -> dict:
    """Normaliza un evento propuesto. Lanza ValueError('invalid_event_*')."""
    if not isinstance(arguments, dict) or set(arguments) - allowed:
        raise ValueError("invalid_event_fields")
    start = _aware(arguments.get("start"), "start")
    end = _aware(arguments.get("end"), "end")
    if end <= start or end - start > timedelta(hours=_MAX_EVENT_HOURS):
        raise ValueError("invalid_event_range")
    location = _text(arguments.get("location"), "location", 1024, single_line=True)
    meet = arguments.get("meet")
    if meet is None:
        # Acordado con el titular: Meet salvo que la reunión sea presencial.
        meet = not location
    if not isinstance(meet, bool):
        raise ValueError("invalid_event_meet")
    return {
        "summary": _text(arguments.get("summary"), "summary", 1024, required=True, single_line=True),
        "start": start.isoformat(), "end": end.isoformat(),
        "time_zone": _zone(arguments.get("time_zone")),
        "attendees": _attendees(arguments.get("attendees")),
        "location": location,
        "description": _text(arguments.get("description"), "description", 8000),
        "meet": meet,
    }


def event_id_for(request_id: str) -> str:
    """Id de evento válido para Google (solo a-v y 0-9) derivado del request_id."""
    return uuid.UUID(request_id).hex


# --- Google ---------------------------------------------------------------

async def _google(method: str, path: str, account: dict, **kwargs) -> dict:
    token = account.get("access_token")
    if not isinstance(token, str) or not token:
        raise _Unauthorized
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.request(
                method, f"{_CAL_API}/{path.lstrip('/')}" if path else _CAL_API,
                headers={"Authorization": f"Bearer {token}"}, **kwargs)
    except httpx.TimeoutException as exc:
        raise _Timeout from exc
    except httpx.HTTPError as exc:
        raise _Failure from exc
    if response.status_code == 401:
        raise _Unauthorized
    if response.status_code == 403:
        raise _Forbidden
    if response.status_code == 409:
        raise _Conflict
    if not response.is_success:
        raise _Failure
    try:
        data = response.json()
    except ValueError as exc:
        raise _Failure from exc
    if not isinstance(data, dict):
        raise _Failure
    return data


def _when(value: dict | None) -> str:
    value = value or {}
    return str(value.get("dateTime") or value.get("date") or "")


def _event_view(event: dict) -> dict:
    meet = ""
    for entry in (event.get("conferenceData") or {}).get("entryPoints") or []:
        if isinstance(entry, dict) and entry.get("entryPointType") == "video":
            meet = str(entry.get("uri") or "")
            break
    return {"id": str(event.get("id") or ""), "summary": str(event.get("summary") or ""),
            "start": _when(event.get("start")), "end": _when(event.get("end")),
            "location": str(event.get("location") or ""),
            "html_link": str(event.get("htmlLink") or ""), "meet_link": meet}


def _is_busy(event: dict) -> bool:
    if event.get("status") == "cancelled" or event.get("transparency") == "transparent":
        return False
    for attendee in event.get("attendees") or []:
        if isinstance(attendee, dict) and attendee.get("self") and attendee.get("responseStatus") == "declined":
            return False
    return True


def _range(arguments: dict) -> tuple[datetime, datetime]:
    start = _aware(arguments.get("time_min"), "time_min")
    end = _aware(arguments.get("time_max"), "time_max")
    if end <= start or end - start > timedelta(days=_MAX_RANGE_DAYS):
        raise ValueError("invalid_range")
    return start, end


async def _list(account: dict, start: datetime, end: datetime, limit: int) -> tuple[list[dict], str]:
    data = await _google("GET", "events", account, params={
        "timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
        "orderBy": "startTime", "maxResults": limit})
    items = [item for item in data.get("items") or [] if isinstance(item, dict)]
    return items, str(data.get("timeZone") or "")


def _bounds(event: dict, tz: ZoneInfo) -> tuple[datetime, datetime] | None:
    try:
        start, end = event.get("start") or {}, event.get("end") or {}
        if start.get("dateTime"):
            return (datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00")),
                    datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00")))
        if start.get("date"):
            first = datetime.combine(datetime.fromisoformat(start["date"]).date(), dtime(), tz)
            last = datetime.combine(datetime.fromisoformat(end["date"]).date(), dtime(), tz)
            return first, last
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    return None


def free_slots(busy: list[tuple[datetime, datetime]], start: datetime, end: datetime, *,
               tz: ZoneInfo, duration: timedelta, day_start: dtime, day_end: dtime,
               weekends: bool, now: datetime | None = None, limit: int = 20) -> list[dict]:
    """Huecos de al menos `duration` dentro del horario hábil, en la zona del calendario."""
    now = now or datetime.now(tz)
    start = max(start, now)
    merged: list[list[datetime]] = []
    for b_start, b_end in sorted(busy):
        if merged and b_start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b_end)
        else:
            merged.append([b_start, b_end])
    slots: list[dict] = []
    day = start.astimezone(tz).date()
    while day <= end.astimezone(tz).date() and len(slots) < limit:
        if weekends or day.weekday() < 5:
            window_start = max(datetime.combine(day, day_start, tz), start)
            window_end = min(datetime.combine(day, day_end, tz), end)
            cursor = window_start
            for b_start, b_end in merged:
                if b_end <= cursor or b_start >= window_end:
                    continue
                if b_start - cursor >= duration:
                    slots.append({"start": cursor.isoformat(), "end": b_start.isoformat()})
                cursor = max(cursor, b_end)
            if window_end - cursor >= duration:
                slots.append({"start": cursor.isoformat(), "end": window_end.isoformat()})
        day += timedelta(days=1)
    return slots[:limit]


# --- Tools del chat -------------------------------------------------------

async def _actor_account():
    actor = current_actor.get()
    if actor is None:
        raise AccountError("Conecta Google desde Mi cuenta.")
    account = await require_account("google")
    if not has_calendar_scope(account):
        raise AccountError("Tu conexión de Google no autoriza Calendar. Vuelve a conectar Google "
                           "en Mi cuenta y acepta el permiso de Calendar.")
    return actor, account


def _tool_error(exc: Exception) -> dict:
    if isinstance(exc, AccountError):
        return {"ok": False, "error": str(exc)}
    if isinstance(exc, _Forbidden):
        return {"ok": False, "error": "Google rechazó el acceso a Calendar; vuelve a conectar Google en Mi cuenta."}
    return {"ok": False, "error": "Google Calendar no está disponible ahora; intenta de nuevo."}


async def mcp_calendar_events(arguments: dict) -> dict:
    try:
        start, end = _range(arguments)
        limit = int(arguments.get("max_results", 25))
        if not 1 <= limit <= 100:
            raise ValueError("invalid_limit")
    except (TypeError, ValueError):
        return {"ok": False, "error": "Rango inválido: usa ISO 8601 con zona horaria (máx. 31 días)."}
    try:
        (store, email), account = await _actor_account()
        items, zone = await _list(account, start, end, limit)
    except (AccountError, _Unauthorized, _Forbidden, _Failure, _Timeout) as exc:
        if isinstance(exc, _Unauthorized):
            store, email = current_actor.get()
            await store.invalidate("google", email)
        return _tool_error(exc)
    return {"ok": True, "time_zone": zone, "events": [
        {**_event_view(item), "busy": _is_busy(item)} for item in items]}


async def mcp_calendar_free_slots(arguments: dict) -> dict:
    try:
        start, end = _range(arguments)
        duration = timedelta(minutes=int(arguments.get("duration_minutes", 60)))
        if not timedelta(minutes=5) <= duration <= timedelta(hours=_MAX_EVENT_HOURS):
            raise ValueError("invalid_duration")
        day_start = str(arguments.get("day_start") or "09:00")
        day_end = str(arguments.get("day_end") or "19:00")
        if not _HHMM.fullmatch(day_start) or not _HHMM.fullmatch(day_end) or day_end <= day_start:
            raise ValueError("invalid_hours")
        weekends = arguments.get("include_weekends", False)
        if not isinstance(weekends, bool):
            raise ValueError("invalid_weekends")
    except (TypeError, ValueError):
        return {"ok": False, "error": ("Parámetros inválidos: rango ISO 8601 con zona (máx. 31 días), "
                                       "duración 5–1440 min y horario HH:MM.")}
    try:
        (store, email), account = await _actor_account()
        items, zone = await _list(account, start, end, _MAX_EVENTS)
    except (AccountError, _Unauthorized, _Forbidden, _Failure, _Timeout) as exc:
        if isinstance(exc, _Unauthorized):
            store, email = current_actor.get()
            await store.invalidate("google", email)
        return _tool_error(exc)
    try:
        tz = ZoneInfo(zone) if zone else start.tzinfo
    except (ZoneInfoNotFoundError, ValueError):
        tz = start.tzinfo
    busy = [b for item in items if _is_busy(item) and (b := _bounds(item, tz))]
    slots = free_slots(busy, start, end, tz=tz, duration=duration,
                       day_start=dtime.fromisoformat(day_start), day_end=dtime.fromisoformat(day_end),
                       weekends=weekends)
    return {"ok": True, "time_zone": zone, "now": datetime.now(tz).isoformat(),
            "slots": slots, "truncated": len(items) >= _MAX_EVENTS}


def _drafts(store) -> dict[str, dict]:
    if not hasattr(store, "event_drafts"):
        store.event_drafts = {}
    return store.event_drafts


def _public(draft_id: str, draft: dict) -> dict:
    return {"draft_id": draft_id, "event": draft["event"], "created_at": draft["created_at"]}


async def mcp_prepare_event(arguments: dict) -> dict:
    """Guarda un evento propuesto; la persona lo crea desde Mi cuenta."""
    try:
        event = validate_event(arguments)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": f"El evento no es válido ({exc}). Revisa fecha, hora e invitados."}
    try:
        (store, email), _account = await _actor_account()
    except AccountError as exc:
        return {"ok": False, "error": str(exc)}
    now = time()
    drafts = _drafts(store)
    for draft_id, draft in list(drafts.items()):
        if draft["expires_at"] <= now:
            del drafts[draft_id]
    owned = sorted((d["created_at"], k) for k, d in drafts.items() if d["email"] == email)
    while len(owned) >= _DRAFTS_PER_USER:
        drafts.pop(owned.pop(0)[1], None)
    draft_id = uuid.uuid4().hex
    drafts[draft_id] = {"email": email, "event": event, "created_at": now,
                        "expires_at": now + _DRAFT_TTL}
    return {"ok": True, "draft_id": draft_id, "event": event, "open_url": "/admin/#/account"}


# --- API de Mi cuenta -----------------------------------------------------

def _actor():
    actor = current_actor.get()
    if actor is None:
        raise web.HTTPUnauthorized(text="Inicia sesión para usar tu calendario.")
    return actor


async def api_calendar_drafts(request: web.Request) -> web.Response:
    store, email = _actor()
    try:
        await store.check_user("google", email)
    except AccountError:
        return _json({"error": "google_reconnect_required"}, status=409)
    now = time()
    drafts = _drafts(store)
    items = [_public(k, d) for k, d in drafts.items() if d["email"] == email and d["expires_at"] > now]
    items.sort(key=lambda item: item["created_at"], reverse=True)
    return _json({"drafts": items})


def _insert_body(event: dict, request_id: str) -> dict:
    def when(value: str) -> dict:
        out = {"dateTime": value}
        if event["time_zone"]:
            out["timeZone"] = event["time_zone"]
        return out

    body: dict[str, Any] = {"id": event_id_for(request_id), "summary": event["summary"],
                            "start": when(event["start"]), "end": when(event["end"])}
    if event["location"]:
        body["location"] = event["location"]
    if event["description"]:
        body["description"] = event["description"]
    if event["attendees"]:
        body["attendees"] = [{"email": address} for address in event["attendees"]]
    if event["meet"]:
        body["conferenceData"] = {"createRequest": {
            "requestId": request_id, "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
    return body


async def api_calendar_create(request: web.Request) -> web.Response:
    try:
        body = await _body(request)
        if set(body) - _CREATE_FIELDS:
            raise ValueError("invalid_fields")
        request_id = str(uuid.UUID(str(body.get("request_id"))))
        event = validate_event({k: v for k, v in body.items() if k != "request_id"})
    except web.HTTPException:
        raise
    except (TypeError, ValueError, AttributeError) as exc:
        code = str(exc) if str(exc).startswith("invalid_") else "invalid_request_id"
        return _json({"error": code}, status=400)

    store, email = _actor()
    try:
        account = await require_account("google")
    except AccountError:
        return _json({"error": "google_reconnect_required"}, status=409)
    if not has_calendar_scope(account):
        return _json({"error": "calendar_scope_required"}, status=409)

    insert = _insert_body(event, request_id)
    params = {"conferenceDataVersion": 1, "sendUpdates": "all" if event["attendees"] else "none"}
    replayed = False
    try:
        try:
            created = await _google("POST", "events", account, params=params, json=insert)
        except _Conflict:
            # Mismo request_id ya creado (reintento tras un timeout): no duplicar.
            created = await _google("GET", f"events/{insert['id']}", account)
            replayed = True
    except _Unauthorized:
        await store.invalidate("google", email)
        return _json({"error": "google_reconnect_required"}, status=401)
    except _Forbidden:
        return _json({"error": "calendar_scope_required"}, status=409)
    except (_Timeout, _Failure) as exc:
        # El id determinista hace seguro reintentar con el mismo request_id.
        return _json({"error": "creation_uncertain", "retry_safe": True},
                     status=504 if isinstance(exc, _Timeout) else 502)
    if created.get("status") == "cancelled":
        return _json({"error": "event_cancelled"}, status=409)
    return _json({"request_id": request_id, "state": "created", "replayed": replayed,
                  **_event_view(created)})


def register_routes(app: web.Application) -> None:
    app.router.add_get("/admin/api/account/calendar/drafts", api_calendar_drafts)
    app.router.add_post("/admin/api/account/calendar/events", api_calendar_create)
