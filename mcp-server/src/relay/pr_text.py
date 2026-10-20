"""Referencias de issues sin instrucciones de cierre en cuerpos generados.

No modifica PR existentes, mensajes de commit ni automatizaciones de GitHub.
"""
from __future__ import annotations

import re


_CLOSING_REFERENCE = re.compile(
    r"(?<!\w)(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
    r"(?:\s+:?\s*|:\s*)"
    r"(?P<reference>https?://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+/issues/[0-9]+"
    r"|[A-Za-z0-9-]+/[A-Za-z0-9_.-]+\#[0-9]+|\#[0-9]+)(?!\w)",
    re.IGNORECASE,
)


def neutralize_issue_closing_keywords(body: str) -> str:
    """Conserva cada referencia y sustituye únicamente su keyword de cierre."""
    return _CLOSING_REFERENCE.sub(
        lambda match: f"Relacionado con {match['reference']}", body)


def neutralize_gh_pr_create_body(args: tuple[str, ...]) -> tuple[str, ...]:
    """Normaliza las variantes de --body usadas al crear una PR con gh."""
    result = list(args)
    for index, arg in enumerate(args):
        if arg in {"--body", "-b"} and index + 1 < len(args):
            result[index + 1] = neutralize_issue_closing_keywords(args[index + 1])
        elif arg.startswith("--body="):
            result[index] = "--body=" + neutralize_issue_closing_keywords(arg[7:])
    return tuple(result)
