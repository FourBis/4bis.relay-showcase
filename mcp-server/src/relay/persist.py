"""Persistencia de chats de expertos: .md por chat + JSONL por target.

El resultado se confirma primero en SQLite (chat_outputs). Markdown y
JSONL son exportaciones recuperables; los chats anteriores conservan su MD
como fuente. El JSONL por target es el histórico append-only.

Layout:
    ~/.4bis/chats/<target>/<YYYYmmdd-HHMMSS>-<chat8>.md
    ~/.4bis/jsonl/<target>.jsonl
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import re
import time
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-]")


def _safe(name: str) -> str:
    return _SAFE_RE.sub("_", name) or "unknown"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


#: Heading de la sección de bitácora. NO está en `_MD_SECTIONS`, así que
#: `parse_chat_md_turns` lo trata como contenido del experto y no abre
#: un turno nuevo — auditable sin romper el parseo.
BITACORA_HEADING = "## Bitácora de la corrida"

#: Cuántos tool_call recientes volcar al .md antes de empezar a resumir.
#: 50 ocupa ~5-6 KB; por encima de eso el .md deja de ser leíble.
BITACORA_MAX_RECENT = 50

#: Tope de chars por mensaje individual — un tool_call con un read_file
#: de 500 líneas no tiene que inflar el .md. 200 chars alcanza para ver
#: QUÉ archivo miró.
BITACORA_MSG_CAP = 200


def _format_events_section(
    events: list[dict], *,
    max_recent: int = BITACORA_MAX_RECENT,
    msg_cap: int = BITACORA_MSG_CAP,
) -> str:
    """Compacta `events` (lista de dicts tool_call/thinking/say) a markdown.

    Filtra `phase == 'tool_call'`, descarta thinking/say (ruido para
    auditar). Trunca `message` a `msg_cap` chars. Si hay más de
    `max_recent` eventos, resume los más viejos en una línea y vuelca
    los últimos N en orden cronológico.

    Regresión medida 2026-09-06: un nodo de grafo consumió 790.142
    tokens en 61 tool calls y su .md pesaba ~1 KB con pedido + respuesta
    final, mientras `chats.progress_events` guardaba 161 eventos /
    59.701 chars de la corrida — la bitácora existía pero no salía al
    .md, así que auditar cómo llegó al veredicto era imposible.
    """
    if not events:
        return ""
    calls = [e for e in events if isinstance(e, dict) and e.get("phase") == "tool_call"]
    if not calls:
        return ""
    n = len(calls)
    if n > max_recent:
        head_omit = n - max_recent
        recent = calls[-max_recent:]
        lines = [f"…[{head_omit} eventos tool_call anteriores omitidos]…"]
    else:
        recent = calls
        lines = []
    for e in recent:
        ts = str(e.get("ts") or "")
        tool = str(e.get("tool") or "")
        msg = str(e.get("message") or "")
        if len(msg) > msg_cap:
            msg = msg[:msg_cap].rstrip() + "…"
        # formato tipo cronología: timestamp + herramienta + mensaje corto.
        # una sola línea por evento para que sea escaneable.
        prefix = f"- `{ts}` **{tool}**" if tool else f"- `{ts}`"
        lines.append(f"{prefix} — {msg}" if msg else prefix)
    return "\n".join(lines)


async def write_chat_md(
    *, target: str, chat_id: str, user: str, content: str,
    source: str, author: str, model: str, status: str,
    duration_ms: int, error: str | None = None,
    events: list[dict] | None = None,
    filename: str | None = None,
) -> str:
    """Escribe el .md del chat. Devuelve el path como string.

    Si se pasan `events` (lista de dicts con `phase`, `tool`, `message`,
    `ts` — el mismo formato que `chats.progress_events`), se agrega al
    final una sección `## Bitácora de la corrida` con los tool_call
    compactos. Sin esa lista, el .md es idéntico al de antes (este
    parámetro es retrocompatible: los callers viejos que no lo pasan
    siguen produciendo el mismo .md).
    """
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    d = config.chats_dir() / _safe(target)
    path = d / (filename or f"{stamp}-{chat_id[:8]}.md")

    lines = [
        "---",
        f"id: {chat_id}",
        f"target: {target}",
        f"source: {source}",
        f"author: {author}",
        f"model: {model}",
        f"status: {status}",
        f"duration_ms: {duration_ms}",
        f"ts: {now_iso()}",
        "---",
        "",
        "## Usuario",
        "",
        user,
        "",
        "## Respuesta",
        "",
        content if content else "(sin contenido)",
    ]
    if error:
        lines += ["", "## Error", "", error]
    bitacora = _format_events_section(events or [])
    if bitacora:
        lines += ["", BITACORA_HEADING, "", bitacora]
    text = "\n".join(lines) + "\n"

    def _write() -> None:
        d.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    await asyncio.to_thread(_write)
    return str(path)


async def append_jsonl(target: str, role: str, content: str, **extra) -> None:
    """Append de un evento al JSONL del target. Best-effort."""
    d = config.jsonl_dir()
    path = d / f"{_safe(target)}.jsonl"
    record = {"ts": now_iso(), "role": role, "content": content, **extra}
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))

    def _write() -> None:
        d.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    await asyncio.to_thread(_write)


async def jsonl_tiene_respuesta(target: str, chat_id: str) -> bool:
    """¿Ya está la línea `assistant` de este chat en el JSONL del target?

    La usa SOLO el reintento de exportación. El .md se sobrescribe y por
    eso reintentar es inofensivo, pero el JSONL es un append: si la
    exportación escribió los dos archivos y falló recién al confirmar en
    SQLite, el reintento agregaba la respuesta una segunda vez.

    **Mira el rol y no solo el `chat`.** La primera versión buscaba
    cualquier registro con ese chat_id, y el turno HTTP appendea la
    línea `user` ANTES de correr (`server.py`, en `experts_run`): la
    aguja aparecía siempre, así que el reintento se saltaba la respuesta
    en TODOS los casos y encima marcaba `exported=1`. Quedaba el pedido
    sin la respuesta y nadie se enteraba.

    Se parsea cada línea en vez de buscar el texto: `"role":"assistant"`
    puede aparecer dentro del `content` de un chat que justo hable de
    JSON, y ahí el substring vuelve a mentir.

    Escanea el archivo entero, que es O(n) sobre todo el historial del
    proyecto. Por eso no se llama en el camino normal: cerrar un chat no
    puede pagar una lectura completa cada vez, y ahí el append es
    correcto porque es la primera y única escritura.
    """
    path = config.jsonl_dir() / f"{_safe(target)}.jsonl"

    def _buscar() -> bool:
        try:
            with path.open("r", encoding="utf-8") as f:
                for linea in f:
                    linea = linea.strip()
                    if not linea:
                        continue
                    try:
                        rec = json.loads(linea)
                    except json.JSONDecodeError:
                        continue
                    if (isinstance(rec, dict) and rec.get("chat") == chat_id
                            and rec.get("role") == "assistant"):
                        return True
        except OSError:
            return False
        return False

    return await asyncio.to_thread(_buscar)


async def write_chat_artifacts(
    *, target: str, chat_id: str, user: str, content: str,
    source: str, author: str, model: str, status: str,
    duration_ms: int, error: str | None = None,
    events: list[dict] | None = None,
    filename: str | None = None,
    escribir_jsonl: bool = True,
    messages_json: str | None = None,
) -> str:
    """Escribe .md + JSONL del chat. Devuelve el md_path.

    Helper para los dos caminos que cierran un chat (HTTP y nodo de
    grafo): reune las dos escrituras que el ADR-005 pide como artefactos
    canónicos del run. El caller sigue siendo responsable de
    `db.finish_chat` porque los campos de telemetría difieren entre
    caminos (stages/timeline/tool_meter solo los emite el endpoint HTTP).

    `events` (opcional) es la lista de tool_call/thinking/say del run
    (el mismo formato de `chats.progress_events`); si viene, `write_chat_md`
    la vuelca compactada al .md para auditar cómo se llegó al veredicto.
    Si es None o vacía, el .md queda igual que antes.

    `messages_json` puede viajar en el payload durable de un nodo cancelado.
    Permanece en la base; no se publica como texto crudo en .md ni JSONL.

    ponytail: si algún día aparece un tercer camino que cierre chats,
    viene acá; si no, queda en 2 callers.
    """
    md_path = await write_chat_md(
        target=target, chat_id=chat_id, user=user, content=content,
        source=source, author=author, model=model,
        status=status, duration_ms=duration_ms, error=error,
        events=events,
        filename=filename,
    )
    # `escribir_jsonl=False` lo manda el reintento cuando la línea ya
    # está: el .md se puede reescribir, el append no se puede deshacer.
    if content and escribir_jsonl:
        await append_jsonl(target, "assistant", content, chat=chat_id)
    return md_path


async def write_chat_artifacts_from_chat(db, chat_id: str, *, res: dict) -> str | None:
    """Persiste .md + JSONL de un chat a partir de lo que ya está en `chats`.

    Usado por el camino del grafo (`_cerrar_chat`), que no tiene en scope
    `target`/`source`/`author`/`user`. Los levanta con `db.get_chat` y
    reusa `write_chat_artifacts` para no duplicar el formato.

    También levanta `progress_events` desde la columna JSON de `chats`
    y lo pasa al .md — al cerrar el nodo de grafo, esa columna ya tiene
    la timeline completa del run y es lo único que permite auditar
    cómo se llegó al veredicto.

    Devuelve el md_path o None si el chat no está en la tabla o falta el
    contenido del experto (cualquiera de los dos casos es no-op).
    """
    row = await db.get_chat(chat_id)
    content = (res or {}).get("content") or ""
    if not row or not content:
        return None
    # progress_events es JSON en la DB. Si falla el parseo (o está
    # vacío), seguimos sin bitácora — el .md no es peor que antes.
    raw_pe = row.get("progress_events")
    events: list[dict] = []
    if raw_pe:
        try:
            parsed = json.loads(raw_pe)
            if isinstance(parsed, list):
                events = [e for e in parsed if isinstance(e, dict)]
        except (TypeError, ValueError):
            logger.exception("progress_events de chat %s no parsea", chat_id)
    md_path = await write_chat_artifacts(
        target=row.get("target") or "_orphan",
        chat_id=chat_id,
        # La columna es `user_prompt`; `user` no existe en `chats`.
        # Con el nombre viejo esto caia siempre al fallback y la
        # seccion "Usuario" del .md decia "orquestador" en vez de la
        # tarea: la respuesta sin la pregunta, que es la mitad que
        # hace falta para releer un nodo.
        user=row.get("user_prompt") or row.get("author") or "?",
        content=content,
        source=row.get("source") or "grafo",
        author=row.get("author") or "",
        model=(res or {}).get("model") or row.get("model") or "",
        status=(res or {}).get("status") or "ok",
        duration_ms=int((res or {}).get("duration_ms") or 0),
        error=(res or {}).get("error") or "",
        events=events,
    )
    # Anota el md_path en la fila. `finish_chat` usa COALESCE para
    # md_path, así que pisar acá no toca status, error, tokens, etc.
    try:
        await db.finish_chat(chat_id, status=row.get("status") or "ok",
                              md_path=md_path)
    except Exception:  # noqa: BLE001
        # El .md ya quedó en disco; si la fila no se puede actualizar el
        # admin UI lo ve sin .md pero el archivo está. Log y seguimos.
        logger.exception("no pude anotar md_path del chat %s", chat_id)
    return md_path


# Mapea el heading del .md al role del front. Solo aparecen estas 3 secciones.
_MD_SECTION_TO_ROLE = {
    "Usuario": "user",
    "Respuesta": "assistant",
    # "Error" también produce un turno assistant (no se mapea por nombre:
    # se emite después del Respuesta del mismo run).
}

# Headings que ABREN sección. Cualquier otro `## …` del archivo es texto
# del experto (markdown), no un separador — ver parse_chat_md_turns.
_MD_SECTIONS = frozenset({"Usuario", "Respuesta", "Error"})


def parse_chat_md_turns(md_path: str | Path) -> list[dict]:
    """Parsea un .md de chat a la lista de turnos que consume la UI.

    Formato esperado (escrito por write_chat_md):
        ## Usuario
        <texto>
        ## Respuesta
        <texto>
        ## Error       (opcional)
        <texto>

    Devuelve una lista de {"role": str, "content": str} en orden
    cronológico. La sección "## Error" se emite como un turno assistant
    extra al final del turno assistant.

    Robustez: si el archivo falta, no se puede leer o no tiene la
    sección "## Respuesta", devuelve [] (el caller decide qué hacer).
    Stdlib puro (ponytail: si el formato crece, mover a un parser real).
    """
    try:
        text = Path(md_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    sections: list[tuple[str, str]] = []
    current_role: str | None = None
    buf: list[str] = []

    def _flush() -> None:
        nonlocal current_role
        if current_role is not None and buf:
            sections.append((current_role, "\n".join(buf).strip()))
        buf.clear()
        current_role = None

    for line in text.splitlines():
        # Solo los headings de sección cortan. Un `## ` cualquiera es
        # CONTENIDO: el experto escribe markdown y sus títulos entran acá
        # (2026-07-31: "## Repos oficiales" cerraba el turno y el resto de
        # la respuesta se descartaba — la UI mostraba 46 de 2583 chars).
        heading = line[3:].strip() if line.startswith("## ") else None
        if heading in _MD_SECTIONS:
            _flush()
            current_role = heading
        elif current_role is not None:
            buf.append(line)

    _flush()

    turns: list[dict] = []
    for heading, content in sections:
        if not content:
            continue
        if heading == "Usuario":
            turns.append({"role": "user", "content": content})
        elif heading == "Respuesta":
            turns.append({"role": "assistant", "content": content})
        elif heading == "Error":
            turns.append({"role": "assistant", "content": content})
    return turns
