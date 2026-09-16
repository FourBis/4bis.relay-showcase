"""Tools MCP para Gmail: gmail_read y gmail_send.

Modos:
- MOCK: GOOGLE_REAL!=1 (default). Devuelve respuestas fijas sin credenciales.
- REAL: GOOGLE_REAL=1. Usa service account en GOOGLE_CREDS_PATH.

Para gmail_send REAL, el service account necesita Domain-Wide Delegation
y GOOGLE_USER_EMAIL con la dirección del buzón que manda.

gmail_read usa la query nativa de Gmail: from:, to:, subject:, is:unread,
newer_than:Nd, etc.

Env vars:
  GOOGLE_REAL: "1" para activar modo real. Default: mock.
  GOOGLE_CREDS_PATH: ruta al client_secret*.json del service account.
  GOOGLE_USER_EMAIL: dirección del buzón que manda (solo para send).

ponytail: separar _mock y _real en vez de un dispatcher central. Es menos
abstracto pero más fácil de seguir cuando algo falla en producción.
"""
from __future__ import annotations

import asyncio
import base64
import email.utils
import logging
import os
from email.message import EmailMessage
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger("relay.tools.gmail")

_GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

_MOCK_MESSAGES = [
    {
        "id": "msg_001",
        "from": "client@example.com",
        "subject": "Propuesta de AuroraDemo",
        "snippet": "Adjuntamos la documentación del proyecto de ejemplo.",
        "date": "2026-07-04T09:00:00Z",
    },
    {
        "id": "msg_002",
        "from": "sender@example.test",
        "subject": "Documentación de AuroraDemo",
        "snippet": "Adjuntamos la documentación del proyecto de ejemplo.",
        "date": "2026-07-03T16:30:00Z",
    },
    {
        "id": "msg_003",
        "from": "no-reply@github.com",
        "subject": "[INVENTORYDEMO] Pull request #142 merged",
        "snippet": "usuario-demo merged 3 commits into main from feature/shipping-label...",
        "date": "2026-07-03T11:15:00Z",
    },
]


def _build_gmail_client(creds_path: str):
    """Construye el cliente de Gmail. Lanza si las creds no sirven."""
    if not creds_path:
        raise FileNotFoundError(
            "GOOGLE_CREDS_PATH no está seteado. Pega la ruta a tu client_secret*.json."
        )
    if not os.path.exists(creds_path):
        raise FileNotFoundError(
            f"GOOGLE_CREDS_PATH no existe: {creds_path}. Pega tu client_secret*.json ahí."
        )
    creds = Credentials.from_service_account_file(creds_path, scopes=_GMAIL_SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except Exception as e:
            raise RuntimeError(
                f"No se pudo refrescar credenciales Google: {e}. "
                "Si usás service account sin Domain-Wide Delegation, "
                "solo lectura. Para enviar necesitas DWD y GOOGLE_USER_EMAIL."
            ) from e
    # cache_discovery=False evita escribir disco (FS readonly en containers)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


class GmailReadTool:
    """Lee correos. Mock por defecto, real con GOOGLE_REAL=1."""

    name = "gmail_read"
    description = (
        "Lee correos de Gmail. Args: query (str, opcional), max_results (int, default 10, max 50). "
        "Devuelve lista de {id, from, subject, snippet, date}. "
        "Query usa la sintaxis nativa de Gmail: from:, to:, subject:, is:unread, newer_than:Nd."
    )

    def __init__(self, creds_path: str | None = None) -> None:
        self._real = os.environ.get("GOOGLE_REAL") == "1"
        self._creds_path = creds_path or os.environ.get("GOOGLE_CREDS_PATH", "")
        self._client = None
        if self._real:
            self._client = _build_gmail_client(self._creds_path)
            logger.info("gmail_read en modo REAL")
        else:
            logger.info("gmail_read en modo MOCK (GOOGLE_REAL!=1)")

    async def call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = (arguments.get("query") or "").strip()
        try:
            max_results = min(int(arguments.get("max_results", 10)), 50)
        except (TypeError, ValueError):
            max_results = 10
        if self._client is None:
            return self._mock_call(query, max_results)
        try:
            return await self._real_call(query, max_results)
        except HttpError as e:
            logger.warning("gmail_read HttpError: %s", e)
            return {"ok": False, "error": f"gmail API: {e.reason}", "messages": []}
        except Exception as e:
            logger.exception("gmail_read falló")
            return {"ok": False, "error": repr(e), "messages": []}

    @staticmethod
    def _mock_call(query: str, max_results: int) -> dict[str, Any]:
        msgs = _MOCK_MESSAGES
        if query:
            q = query.lower()
            msgs = [
                m for m in _MOCK_MESSAGES
                if q in m["from"].lower()
                or q in m["subject"].lower()
                or q in m["snippet"].lower()
            ]
        return {"ok": True, "source": "mock", "messages": msgs[:max_results]}

    async def _real_call(self, query: str, max_results: int) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            assert self._client is not None
            res = (
                self._client.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
            ids = [m["id"] for m in res.get("messages", [])]
            out = []
            for mid in ids:
                msg = (
                    self._client.users()
                    .messages()
                    .get(
                        userId="me",
                        id=mid,
                        format="metadata",
                        metadataHeaders=["From", "Subject", "Date"],
                    )
                    .execute()
                )
                hdrs = {
                    h["name"]: h["value"]
                    for h in msg.get("payload", {}).get("headers", [])
                }
                out.append({
                    "id": mid,
                    "from": hdrs.get("From", ""),
                    "subject": hdrs.get("Subject", ""),
                    "snippet": msg.get("snippet", ""),
                    "date": hdrs.get("Date", ""),
                })
            return {"ok": True, "source": "real", "messages": out}

        return await asyncio.to_thread(_do)


class GmailSendTool:
    """Manda correos. Solo modo REAL (con GOOGLE_USER_EMAIL + DWD)."""

    name = "gmail_send"
    description = (
        "Envía un correo. Args OBLIGATORIOS: to (str), subject (str), body (str). "
        "Args opcionales: html (bool, default false), cc (list[str]), bcc (list[str]). "
        "Requiere service account con Domain-Wide Delegation y GOOGLE_USER_EMAIL seteado."
    )

    def __init__(self, creds_path: str | None = None) -> None:
        self._real = os.environ.get("GOOGLE_REAL") == "1"
        self._creds_path = creds_path or os.environ.get("GOOGLE_CREDS_PATH", "")
        self._user_email = os.environ.get("GOOGLE_USER_EMAIL", "")
        self._client = None
        if self._real:
            if not self._user_email:
                logger.warning(
                    "gmail_send: GOOGLE_USER_EMAIL no seteado. "
                    "Para enviar como un buzón real necesitas Domain-Wide Delegation."
                )
            self._client = _build_gmail_client(self._creds_path)
            logger.info("gmail_send en modo REAL (user=%s)", self._user_email or "?")

    async def call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            return {"ok": False, "error": "gmail_send solo disponible con GOOGLE_REAL=1"}
        if not self._user_email:
            return {
                "ok": False,
                "error": (
                    "gmail_send requiere GOOGLE_USER_EMAIL con un buzón que tenga "
                    "Domain-Wide Delegation al service account. Si quieres mandar "
                    "como tú, configura esa variable."
                ),
            }
        to = (arguments.get("to") or "").strip()
        subject = (arguments.get("subject") or "").strip()
        body = (arguments.get("body") or "").strip()
        if not to or not subject or not body:
            return {"ok": False, "error": "to, subject y body son obligatorios"}
        html = bool(arguments.get("html", False))
        cc = list(arguments.get("cc") or [])
        bcc = list(arguments.get("bcc") or [])

        try:
            msg_id = await self._send(to, subject, body, html, cc, bcc)
            return {"ok": True, "id": msg_id, "source": "real"}
        except HttpError as e:
            logger.warning("gmail_send HttpError: %s", e)
            return {"ok": False, "error": f"gmail API: {e.reason}"}
        except Exception as e:
            logger.exception("gmail_send falló")
            return {"ok": False, "error": repr(e)}

    async def _send(self, to, subject, body, html, cc, bcc) -> str:
        em = EmailMessage()
        em["To"] = to
        em["Subject"] = subject
        em["Date"] = email.utils.formatdate(localtime=True)
        if cc:
            em["Cc"] = ", ".join(cc)
        if html:
            em.set_content("Este mensaje requiere un cliente que soporte HTML.")
            em.add_alternative(body, subtype="html")
        else:
            em.set_content(body)

        raw = base64.urlsafe_b64encode(em.as_bytes()).decode("ascii")

        def _do() -> str:
            assert self._client is not None
            res = (
                self._client.users()
                .messages()
                .send(userId=self._user_email, body={"raw": raw})
                .execute()
            )
            return res.get("id", "")

        return await asyncio.to_thread(_do)


# Schemas PRIMERO (luego el dict que los consume)
_READ_SCHEMA: dict = {
    "name": "gmail_read",
    "description": GmailReadTool.description,
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Filtro estilo Gmail"},
            "max_results": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
        },
    },
}


_SEND_SCHEMA: dict = {
    "name": "gmail_send",
    "description": GmailSendTool.description,
    "inputSchema": {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Destinatario"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "html": {"type": "boolean", "default": False},
            "cc": {"type": "array", "items": {"type": "string"}},
            "bcc": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["to", "subject", "body"],
    },
}


GMAIL_TOOLS: dict[str, tuple[type, dict]] = {
    "gmail_read": (GmailReadTool, _READ_SCHEMA),
    "gmail_send": (GmailSendTool, _SEND_SCHEMA),
}
