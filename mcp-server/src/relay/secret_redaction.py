"""Secret redaction for persisted/display copies, without product imports.

Pattern-based protection is not a guarantee for arbitrary unlabelled secrets.
Never use the redacted copy as an executable command or a provider credential.
"""
from __future__ import annotations

import json
import re
from typing import Optional

_CONSOLE_ASSIGNMENT_RE = re.compile(
    r"((?:password|passwd|pwd|api[_-]?key|apikey|secret|"
    r"access[_-]?key|client[_-]?secret)['\"]?\s*[=:]\s*['\"]?)"
    r"(?!\[REDACTED)([^\s;,'\"]{3,})", re.I)

_SECRETOS_RE: tuple[tuple[re.Pattern, str], ...] = (
    # Header Authorization: se guarda el esquema, se tapa el valor.
    (re.compile(r"(Authorization\s*:\s*(?:Bearer|token|Basic)\s+)\S+", re.I),
     r"\1[REDACTED]"),
    # Tokens con prefijo reconocible: el prefijo dice de qué era.
    (re.compile(r"\b(gh[pousr]_)[A-Za-z0-9]{20,}"), r"\1[REDACTED]"),
    (re.compile(r"\b(github_pat_)[A-Za-z0-9_]{30,}"), r"\1[REDACTED]"),
    (re.compile(r"\b(sk-(?:ant-)?)[A-Za-z0-9_\-]{20,}"), r"\1[REDACTED]"),
    (re.compile(r"\b(xox[baprs]-)[A-Za-z0-9\-]{10,}"), r"\1[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED-AWS-KEY]"),
    # Credenciales en URL: `postgres://user:pass@host` → tapa solo `pass`.
    (re.compile(r"([a-z][a-z0-9+.\-]*://[^\s:/@]+:)([^\s@]{3,})(@)"),
     r"\1[REDACTED]\3"),
    # `password=`, `pwd=`, `api_key=`, `secret=` y compañía. La comilla
    # de apertura va DENTRO del grupo que se conserva: sin eso,
    # `PASSWORD='secreto'` no matcheaba —la clase excluye comillas, así
    # que el valor no arrancaba nunca—.
    (_CONSOLE_ASSIGNMENT_RE, r"\1[REDACTED]"),
    # Clave privada PEM: se tapa el cuerpo entero, no solo la cabecera.
    (re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]*?"
                r"(-----END [A-Z ]*PRIVATE KEY-----|\Z)"),
     r"\1[REDACTED]\2"),
)


_SECRET_FIELD_RE = re.compile(
    r"^(?:password|passwd|pwd|api[_-]?key|apikey|secret|access[_-]?key|"
    r"client[_-]?secret|authorization)$", re.I)

_JSON_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
_READ_REFERENCE_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|%[A-Za-z_][A-Za-z0-9_]*%")
# Read copies do not inherit console's free-form-value patterns. These
# signatures require actual token bytes or an aligned PEM body, and leave
# quoted/concatenated strings and environment references untouched.
_READ_AUTH_RE = re.compile(
    r"(^[ \t]*Authorization[ \t]*:[ \t]*(?:Bearer|token|Basic)[ \t]+)"
    r"([A-Za-z0-9_./~+\-][A-Za-z0-9_./~+=\-]*)(?=$|[\s\"',;])", re.I | re.M)
_READ_URL_RE = re.compile(
    r"([a-z][a-z0-9+.\-]*://(?:[A-Za-z0-9_.~\-]|%[0-9a-f]{2})+:)"
    r"((?:[A-Za-z0-9_.~!+\-]|%[0-9a-f]{2}){3,})(@)", re.I)
_READ_PEM_RE = re.compile(
    r"(?P<header>-----BEGIN (?P<kind>[A-Z ]*PRIVATE KEY)-----)[ \t]*(?P<eol>\r?\n)"
    r"(?P<body>[A-Za-z0-9+/]{16,}={0,2}[ \t]*(?:\r?\n|$)"
    r"(?:[A-Za-z0-9+/]{4,}={0,2}[ \t]*(?:\r?\n|$))*)"
    r"(?P<end>-----END (?P=kind)-----)?")


def _read_reference(value):
    return isinstance(value, str) and _READ_REFERENCE_RE.search(value) is not None


def _json_text_copy(text, string_filter):
    """Redact JSON value spans while preserving every unaffected byte.

    JSONDecoder validates values and their endpoints. This is only a scan of
    string/key spans in already-valid JSON, not another JSON parser.
    """
    if not text.lstrip().startswith(("{", "[", '"')):
        return None
    try:
        json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return None
    decoder = json.JSONDecoder()
    pieces, copied_to = [], 0
    for match in _JSON_STRING_RE.finditer(text):
        if match.start() < copied_to:
            continue  # A containing sensitive value was already replaced.
        value = json.loads(match.group())
        after = match.end()
        while after < len(text) and text[after].isspace():
            after += 1
        if after < len(text) and text[after] == ":":
            if not _SECRET_FIELD_RE.fullmatch(value):
                continue
            start = after + 1
            while start < len(text) and text[start].isspace():
                start += 1
            original, end = decoder.raw_decode(text, start)
            if original is None or original == "" or original == "[REDACTED]":
                continue
            # Explicit environment placeholders in configuration are references,
            # not credentials. This does not infer arbitrary expression syntax.
            if string_filter is redact_read_text and _read_reference(original):
                continue
            replacement = '"[REDACTED]"'
        else:
            safe = string_filter(value)
            if safe == value:
                continue
            start, end = match.span()
            replacement = json.dumps(safe, ensure_ascii=False)
        pieces.extend((text[copied_to:start], replacement))
        copied_to = end
    if not pieces:
        return text
    pieces.append(text[copied_to:])
    return "".join(pieces)


def redact_text(text: Optional[str]) -> Optional[str]:
    """Apply the existing credential rules before truncation or persistence."""
    if not text:
        return text
    safe_json = _json_text_copy(text, redact_text)
    if safe_json is not None:
        return safe_json
    for pattern, replacement in _SECRETOS_RE:
        text = pattern.sub(replacement, text)
    return text


def redact_read_text(text: Optional[str]) -> Optional[str]:
    """Sane a file/opaque return copy without treating source code as stdout.

    Prefix tokens, aligned PEM material, literal authenticated URLs/headers and
    sensitive JSON values remain protected. Source assignments and header/PEM
    constants are not credentials merely because of their labels. Assignment
    and partial-console rules require known console/log provenance.
    """
    if not text:
        return text
    safe_json = _json_text_copy(text, redact_read_text)
    if safe_json is not None:
        return safe_json
    # Keep known prefix-token signatures; the header/URL/PEM rules below are
    # deliberately separate from the stronger free-form console patterns.
    for pattern, replacement in _SECRETOS_RE[1:6]:
        text = pattern.sub(replacement, text)
    text = _READ_AUTH_RE.sub(r"\1[REDACTED]", text)
    text = _READ_URL_RE.sub(r"\1[REDACTED]\3", text)
    def pem(match):
        eol = match["eol"]
        trailing = eol if match["body"].endswith("\n") else ""
        return match["header"] + eol + "[REDACTED]" + trailing + (match["end"] or "")
    text = _READ_PEM_RE.sub(pem, text)
    return text


def redact_background_read_text(text: Optional[str], snapshots: tuple[str, ...]) -> Optional[str]:
    """Sanitize an ambiguous log copy without inventing an MCP filesystem.

    Innocuous source/JSON keeps the read contract. Stronger console rules
    require that the returned text (or a JSON-encoded string) is an actual
    contiguous copy of a bounded Relay log snapshot, allowing CRLF folding.
    Otherwise withhold this ambiguous fragment instead of exposing it or
    silently rewriting unrelated source. No snapshot bytes are disclosed.
    """
    ordinary = redact_read_text(text)
    console = redact_text(text)
    if console == ordinary:
        return ordinary
    candidates = [text]
    try:
        decoded = json.loads(text)
        if isinstance(decoded, str):
            candidates.append(decoded)
    except (TypeError, ValueError, RecursionError):
        pass
    for snapshot in snapshots:
        normalized = snapshot.replace("\r\n", "\n")
        if any(candidate and candidate.replace("\r\n", "\n") in normalized for candidate in candidates):
            return console
    return ("error: lectura retenida: este fragmento puede ser de un log de Relay "
            "y el MCP no acredita su origen por archivo. Lee los archivos por "
            "separado con read_file nativo o una herramienta individual ya "
            "autorizada, usando la ruta exacta y los permisos existentes. "
            "No uses este mensaje como contenido para editar un archivo.")


def redact_read_data(value, *, text_filter=redact_read_text):
    """A copy of structured tool data, using the source-preserving text rule."""
    if isinstance(value, str):
        return text_filter(value)
    if isinstance(value, list):
        return [redact_read_data(item, text_filter=text_filter) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_read_data(item, text_filter=text_filter) for item in value)
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if isinstance(key, str) and _SECRET_FIELD_RE.fullmatch(key)
                      and item is not None and item != ""
                      and not _read_reference(item)
                      else redact_read_data(item, text_filter=text_filter)) for key, item in value.items()}
    return value


def redact_json(raw: str) -> str:
    """Sanitize a serialized copy without invalidating JSON escape sequences."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return redact_text(raw) or ""
    safe = redact_data(value)
    return raw if safe == value else (redact_text(raw) or "")


def redact_data(value):
    """Return a redacted copy of JSON-shaped data; preserve keys and scalars."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item) for item in value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_FIELD_RE.fullmatch(key) and item is not None and item != "":
                out[key] = "[REDACTED]"
            elif isinstance(key, str) and key.endswith("_json") and isinstance(item, str):
                out[key] = redact_json(item)
            else:
                out[key] = redact_data(item)
        return out
    return value


# Compatibility for callers that historically imported the display redactor.
_redactar = redact_text
