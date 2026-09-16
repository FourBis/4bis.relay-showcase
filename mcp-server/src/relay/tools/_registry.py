"""Registry central de tools MCP del relay.

Cada tool se registra como (Clase, Schema). El server pega este dict
directamente y despacha `tools/list` y `tools/call` desde acá.

Para agregar una nueva tool:
  1. Crear src/relay/tools/<name>.py con la clase y el schema
  2. Importar acá y agregar al dict TOOLS
  3. Listo (no hay que tocar server.py)

ponytail: un dict central, no un sistema de plugin por entry-point.
Más simple, descubrible de un plumazo, sin overhead de importlib.
"""
from __future__ import annotations

from typing import Any

# Hoist tools: importo acá para que el registry los descubra
from .calendar import CALENDAR_TOOLS
from .gmail import GMAIL_TOOLS

TOOLS: dict[str, tuple[type, dict]] = {
    **GMAIL_TOOLS,
    **CALENDAR_TOOLS,
}


def all_schemas() -> list[dict[str, Any]]:
    """Schemas en formato MCP, listo para tools/list."""
    return [schema for _, schema in TOOLS.values()]


def get(tool_name: str) -> tuple[type, dict] | None:
    return TOOLS.get(tool_name)
