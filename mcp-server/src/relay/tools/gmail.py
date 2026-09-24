"""Tools de Gmail vinculadas al actor del chat, nunca a credenciales globales."""
from __future__ import annotations

from typing import Any


class GmailReadTool:
    name = "gmail_read"
    description = (
        "Lee correos de la cuenta Google conectada por la persona actual, "
        "solo cuando lo solicita explícitamente. Usa message_id para leer un "
        "correo completo. Args: query, max_results (1-50), message_id (opcional). "
        "El contenido aparecerá en el chat compartido."
    )

    async def call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from ..user_mail import mcp_read_mail
        return await mcp_read_mail(arguments)


class GmailSendTool:
    name = "gmail_send"
    description = (
        "Prepara un borrador de Gmail personal; nunca envía directamente. "
        "Args: to (string o lista), cc/bcc (listas opcionales), subject y body. "
        "La persona debe abrir Correo personal y pulsar Enviar."
    )

    async def call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from ..user_mail import mcp_create_mail_draft
        return await mcp_create_mail_draft(arguments)


_READ_SCHEMA: dict = {
    "name": "gmail_read",
    "description": GmailReadTool.description,
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Filtro estilo Gmail"},
            "max_results": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            "message_id": {"type": "string", "description": "ID Gmail para abrir un correo completo"},
        },
    },
}

_SEND_SCHEMA: dict = {
    "name": "gmail_send",
    "description": GmailSendTool.description,
    "inputSchema": {
        "type": "object",
        "properties": {
            "to": {"oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ], "description": "Destinatario(s)"},
            "cc": {"type": "array", "items": {"type": "string"}},
            "bcc": {"type": "array", "items": {"type": "string"}},
            "subject": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["to", "subject", "body"],
    },
}

GMAIL_TOOLS: dict[str, tuple[type, dict]] = {
    "gmail_read": (GmailReadTool, _READ_SCHEMA),
    "gmail_send": (GmailSendTool, _SEND_SCHEMA),
}
