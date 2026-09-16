"""Tools MCP para Google Calendar: calendar_list y calendar_create.

Modos:
- MOCK: GOOGLE_REAL!=1 (default). Devuelve eventos fijos sin credenciales.
- REAL: GOOGLE_REAL=1. Usa service account en GOOGLE_CREDS_PATH.

Env vars:
  GOOGLE_REAL: "1" para activar modo real.
  GOOGLE_CREDS_PATH: ruta al client_secret*.json del service account.
  GOOGLE_USER_EMAIL: dirección del calendario (opcional, default 'me').
  GOOGLE_CALENDAR_ID: default "primary".

Tipos:
- DateTime en ISO 8601 con zona (ej: "2026-07-04T15:00:00-04:00"),
  o "2026-07-04T15:00:00Z" para UTC. La API acepta ambos.

ponytail: separar _mock y _real en vez de un dispatcher central. Es
menos abstracto pero más fácil de seguir cuando algo falla en producción.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger("relay.tools.calendar")

_CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

_DEFAULT_CALENDAR = "primary"

_MOCK_EVENTS = [
    {
        "id": "evt_001",
        "summary": "Reunión con AuroraDemo",
        "start": "2026-07-05T15:00:00-04:00",
        "end": "2026-07-05T16:00:00-04:00",
        "location": "Oficina de AuroraDemo",
        "description": "Revisión del proyecto de ejemplo",
    },
    {
        "id": "evt_002",
        "summary": "Standup equipo demo",
        "start": "2026-07-04T09:00:00-04:00",
        "end": "2026-07-04T09:15:00-04:00",
        "location": "Meet",
        "description": None,
    },
    {
        "id": "evt_003",
        "summary": "INVENTORYDEMO release review",
        "start": "2026-07-07T17:00:00-04:00",
        "end": "2026-07-07T18:30:00-04:00",
        "location": "Oficina",
        "description": "Revisión de release 0.4.2 con equipo",
    },
]


def _build_calendar_client(creds_path: str):
    """Cliente de Calendar. Lanza si GOOGLE_CREDS_PATH no está o no sirve."""
    if not creds_path:
        raise FileNotFoundError(
            "GOOGLE_CREDS_PATH no está seteado. Pega la ruta a client_secret*.json."
        )
    if not os.path.exists(creds_path):
        raise FileNotFoundError(
            f"GOOGLE_CREDS_PATH no existe: {creds_path}."
        )
    creds = Credentials.from_service_account_file(creds_path, scopes=_CALENDAR_SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except Exception as e:
            raise RuntimeError(
                f"No se pudo refrescar credenciales Google: {e}"
            ) from e
    # cache_discovery=False evita escribir disco (problema en containers read-only)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _default_calendar_id() -> str:
    return os.environ.get("GOOGLE_CALENDAR_ID") or _DEFAULT_CALENDAR


class CalendarListTool:
    """Lista eventos en un rango. Mock o REAL."""

    name = "calendar_list"
    description = (
        "Lista eventos del calendario en un rango. "
        "Args: time_min (ISO 8601), time_max (ISO 8601), max_results (int, default 10). "
        "Devuelve lista de {id, summary, start, end, location, description}."
    )

    def __init__(self, creds_path: str | None = None) -> None:
        self._real = os.environ.get("GOOGLE_REAL") == "1"
        self._creds_path = creds_path or os.environ.get("GOOGLE_CREDS_PATH", "")
        self._client = None
        if self._real:
            self._client = _build_calendar_client(self._creds_path)
            logger.info("calendar_list en modo REAL")
        else:
            logger.info("calendar_list en modo MOCK (GOOGLE_REAL!=1)")

    async def call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        time_min = (arguments.get("time_min") or "").strip()
        time_max = (arguments.get("time_max") or "").strip()
        try:
            max_results = min(int(arguments.get("max_results", 10)), 50)
        except (TypeError, ValueError):
            max_results = 10
        if not time_min or not time_max:
            return {"ok": False, "error": "time_min y time_max son obligatorios"}

        if self._client is None:
            return self._mock_call(time_min, time_max, max_results)
        try:
            return await self._real_call(time_min, time_max, max_results)
        except HttpError as e:
            logger.warning("calendar_list HttpError: %s", e)
            return {"ok": False, "error": f"calendar API: {e.reason}", "events": []}
        except Exception as e:
            logger.exception("calendar_list falló")
            return {"ok": False, "error": repr(e), "events": []}

    @staticmethod
    def _mock_call(time_min: str, time_max: str, max_results: int) -> dict[str, Any]:
        # ponytail: comparación lexicográfica funciona solo si ambos son ISO
        # con la misma precisión. La API real usa RFC 3339 estricto.
        evs = [e for e in _MOCK_EVENTS if time_min <= e["start"] <= time_max]
        return {
            "ok": True,
            "source": "mock",
            "events": [
                {k: e[k] for k in ("id", "summary", "start", "end", "location", "description")}
                for e in evs[:max_results]
            ],
        }

    async def _real_call(self, time_min: str, time_max: str, max_results: int) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            assert self._client is not None
            res = (
                self._client.events()
                .list(
                    calendarId=_default_calendar_id(),
                    timeMin=time_min,
                    timeMax=time_max,
                    maxResults=max_results,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            out = []
            for ev in res.get("items", []):
                start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
                end = ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date", "")
                out.append({
                    "id": ev.get("id", ""),
                    "summary": ev.get("summary", ""),
                    "start": start,
                    "end": end,
                    "location": ev.get("location"),
                    "description": ev.get("description"),
                })
            return {"ok": True, "source": "real", "events": out}

        return await asyncio.to_thread(_do)


class CalendarCreateTool:
    """Crea un evento. Solo REAL."""

    name = "calendar_create"
    description = (
        "Crea un evento en Google Calendar. "
        "Args OBLIGATORIOS: summary (str), start (ISO 8601), end (ISO 8601). "
        "Args opcionales: location (str), description (str), attendees (list[str])."
    )

    def __init__(self, creds_path: str | None = None) -> None:
        self._real = os.environ.get("GOOGLE_REAL") == "1"
        self._creds_path = creds_path or os.environ.get("GOOGLE_CREDS_PATH", "")
        self._user_email = os.environ.get("GOOGLE_USER_EMAIL", "")
        self._client = None
        if self._real:
            self._client = _build_calendar_client(self._creds_path)
            logger.info("calendar_create en modo REAL (user=%s)", self._user_email or "?")

    async def call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            return {"ok": False, "error": "calendar_create solo disponible con GOOGLE_REAL=1"}
        summary = (arguments.get("summary") or "").strip()
        start = (arguments.get("start") or "").strip()
        end = (arguments.get("end") or "").strip()
        if not summary or not start or not end:
            return {"ok": False, "error": "summary, start y end son obligatorios"}
        body: dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
        }
        if arguments.get("location"):
            body["location"] = arguments["location"]
        if arguments.get("description"):
            body["description"] = arguments["description"]
        attendees = list(arguments.get("attendees") or [])
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]

        try:
            event_id = await self._create(body)
            return {"ok": True, "id": event_id, "source": "real"}
        except HttpError as e:
            logger.warning("calendar_create HttpError: %s", e)
            return {"ok": False, "error": f"calendar API: {e.reason}"}
        except Exception as e:
            logger.exception("calendar_create falló")
            return {"ok": False, "error": repr(e)}

    async def _create(self, body: dict) -> str:
        def _do() -> str:
            assert self._client is not None
            res = (
                self._client.events()
                .insert(calendarId=_default_calendar_id(), body=body)
                .execute()
            )
            return res.get("id", "")

        return await asyncio.to_thread(_do)


# Schemas PRIMERO (luego el dict que los consume)
_LIST_SCHEMA: dict = {
    "name": "calendar_list",
    "description": CalendarListTool.description,
    "inputSchema": {
        "type": "object",
        "properties": {
            "time_min": {"type": "string", "description": "ISO 8601 (RFC 3339)"},
            "time_max": {"type": "string", "description": "ISO 8601 (RFC 3339)"},
            "max_results": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
        },
        "required": ["time_min", "time_max"],
    },
}


_CREATE_SCHEMA: dict = {
    "name": "calendar_create",
    "description": CalendarCreateTool.description,
    "inputSchema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "start": {"type": "string", "description": "ISO 8601"},
            "end": {"type": "string", "description": "ISO 8601"},
            "location": {"type": "string"},
            "description": {"type": "string"},
            "attendees": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "start", "end"],
    },
}


CALENDAR_TOOLS: dict[str, tuple[type, dict]] = {
    "calendar_list": (CalendarListTool, _LIST_SCHEMA),
    "calendar_create": (CalendarCreateTool, _CREATE_SCHEMA),
}
