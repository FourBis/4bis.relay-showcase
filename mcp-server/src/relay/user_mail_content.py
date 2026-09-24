"""Decodificación a texto plano de respuestas MIME de Gmail."""
from __future__ import annotations

import base64
import html
from html.parser import HTMLParser


def _decode_body(data: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        return raw.decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


class _TextOnlyHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("br", "p", "div", "li", "tr") and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        elif tag in ("p", "div", "li", "tr") and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _message_text(payload: dict) -> str:
    plain: list[str] = []
    markup: list[str] = []

    def visit(part: dict) -> None:
        data = (part.get("body") or {}).get("data") or ""
        kind = part.get("mimeType")
        if data and kind == "text/plain":
            plain.append(_decode_body(data))
        elif data and kind == "text/html":
            markup.append(_decode_body(data))
        for child in part.get("parts", []):
            visit(child)

    visit(payload)
    if plain:
        return "\n".join(plain)
    if markup:
        parser = _TextOnlyHTML()
        parser.feed(markup[0])
        return html.unescape("".join(parser.parts)).strip()
    return ""


def message_view(message: dict, fallback_id: str) -> dict:
    headers = {str(item.get("name", "")).lower(): str(item.get("value", ""))
               for item in message.get("payload", {}).get("headers", [])}
    return {"id": message.get("id", fallback_id), "from": headers.get("from", ""),
            "to": headers.get("to", ""), "cc": headers.get("cc", ""),
            "subject": headers.get("subject", ""), "date": headers.get("date", ""),
            "body": _message_text(message.get("payload") or {})}
