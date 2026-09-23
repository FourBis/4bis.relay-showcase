"""Eventos de herramientas, salidas de consola y redacción de secretos."""
from __future__ import annotations
import json
import logging
import re
from pydantic_ai.messages import ToolReturnPart
from typing import Any, Optional
from . import expert_history

logger = logging.getLogger("relay.experts")


_STEP_DIFF_MAX_LINES = 40
_STEP_DIFF_MAX_CHARS = 1600


def _args_de_part(part: Any) -> dict:
    """Args de UN `ToolCallPart`. {} si no se pueden parsear.

    Por part y no por nombre a propósito: buscar por nombre devuelve
    siempre el PRIMER match, así que en un response con dos llamadas a
    la misma tool las dos quedaban con los args de la primera.
    """
    args = getattr(part, "args", None)
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


#: Credenciales que los expertos pasan por línea de comandos y que, sin
#: esto, quedaban guardadas para siempre. Barrido del 2026-09-02 sobre
#: `relay.db` + los 1240 `.md` de chats: 1 token de GitHub (94 copias del
#: mismo), 22 headers `Authorization`, 22 `password=`, 8 connection
#: strings, 7 passwords en URL, 3 `api_key` y 1 AWS access key, en siete
#: proyectos de clientes.
#:
#: Se redacta SOLO el secreto y se conserva la etiqueta: `Password=[…]`
#: sigue diciendo que había un password ahí, que es lo que necesita quien
#: lee el log para entender qué pasó. Un `[REDACTED]` que se come la línea
#: entera vuelve el timeline inútil.
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
    # que el valor no arrancaba nunca— y se filtraban 3 de 17 connection
    # strings del corpus real (medido 2026-09-02).
    (re.compile(r"((?:password|passwd|pwd|api[_-]?key|apikey|secret|"
                r"access[_-]?key|client[_-]?secret)\s*[=:]\s*['\"]?)"
                r"(?!\[REDACTED)([^\s;,'\"]{3,})", re.I),
     r"\1[REDACTED]"),
    # Clave privada PEM: se tapa el cuerpo entero, no solo la cabecera.
    (re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]*?"
                r"(-----END [A-Z ]*PRIVATE KEY-----)"),
     r"\1[REDACTED]\2"),
)


def _redactar(texto: Optional[str]) -> Optional[str]:
    """Tapa credenciales en un string que va a persistirse o mostrarse.

    Se aplica en `_format_tool_step`, que es el productor ÚNICO de las
    cadenas que terminan en `chats.progress_events` (y de ahí en los
    `.md` y en el contexto que se le re-inyecta al LLM). Redactar acá
    cubre los dos `_emit_progress` y los dos callers sin tocarlos.

    Es seguro porque estas cadenas son SOLO display: el comando que se
    ejecuta viaja por los args de la tool, no por acá. Verificado
    2026-09-02: el `cmd` persistido solo se asigna a `turn["cmd"]`,
    `step["cmd"]` y `paso["cmd"]`, nunca se ejecuta.

    Lo que NO hace: el secreto sigue existiendo en memoria mientras el
    comando corre, y en la salida del proceso si la herramienta lo
    imprime. Esto corta la ACUMULACIÓN, que es lo que convertía un uso
    puntual en un archivo permanente.
    """
    if not texto:
        return texto
    for rx, repl in _SECRETOS_RE:
        texto = rx.sub(repl, texto)
    return texto


def _clasificar_partes(mr: Any) -> tuple[list, list[str], list[str]]:
    """Parte un `ModelResponse` en (tools pedidas, textos a narrar, bitácora).

    2026-09-04: `ThinkingPart` estaba en la misma rama que `TextPart` y se
    emitía como `phase="say"`. MiniMax-M3 es de razonamiento —pydantic-ai
    mapea `reasoning_content` → `ThinkingPart`—, así que la UI mostraba el
    chain-of-thought crudo, en inglés, apareado con la narración real
    (mismo timestamp, 6 pares en un run de 6 turnos). Contradecía el
    contrato del consumidor: `server._STEP_PHASES` dice que "pensar" es
    heartbeat (`phase="thinking"`), no contenido — y el resto del módulo
    ya trata `ThinkingPart` como algo a FILTRAR (`_elide_old_response_parts`,
    `_strip_foreign_thinking`).
    Narrar = `TextPart` y nada más.
    """
    pedidas: list[tuple[str, dict, str]] = []
    says: list[str] = []
    recent: list[str] = []
    for part in getattr(mr, "parts", []) or []:
        t = getattr(part, "tool_name", None)
        if t:
            pedidas.append((t, _args_de_part(part),
                            getattr(part, "tool_call_id", "") or ""))
            recent.append(f"{t}:{getattr(part, 'args', None)!r}"[:400])
        elif type(part).__name__ == "TextPart":
            txt = str(getattr(part, "content", "") or "").strip()
            if txt:
                says.append(txt)
    return pedidas, says, recent


def _format_tool_step(
    name: str, args: dict,
) -> tuple[str, str | None, str | None]:
    """(línea legible, diff|None, comando|None) para un tool call.

    Las tres cadenas salen redactadas (ver `_redactar`): son las que se
    persisten en `chats.progress_events`, y los expertos pasan tokens y
    passwords por línea de comandos.
    """
    msg, diff, cmd = _format_tool_step_crudo(name, args)
    return _redactar(msg) or "", _redactar(diff), _redactar(cmd)


def _format_tool_step_crudo(
    name: str, args: dict,
) -> tuple[str, str | None, str | None]:
    """Implementación sin redactar. No llamar directo: usar
    `_format_tool_step`, que es el que tapa credenciales.

    La línea va al timeline del bot y al encabezado de la tarjeta de la
    UI; el diff (solo edit_file) se postea aparte; el `comando` es el
    texto ENTERO de la shell, para que la tarjeta lo muestre sin
    recortar.

    2026-08-27: `shell` —la tool nativa del relay, la que el experto usa
    de verdad desde que `HideToolsToolset` esconde el `run_shell` del
    wrapper— no tenía rama acá y caía al genérico `🔧 {name}`. O sea que
    la UI decía "shell" y nada más: ni qué comando corrió ni con qué
    salió. Ese era el agujero reportado.
    """
    def _clip(s: Any, n: int = 80) -> str:
        s = str(s or "")
        return s if len(s) <= n else s[: n - 1] + "…"

    if name == "read_file":
        return f"📄 leyó `{_clip(args.get('path'))}`", None, None
    if name == "list_dir":
        return f"📂 listó `{_clip(args.get('path') or '.')}`", None, None
    if name == "directory_tree":
        return f"🌳 árbol de `{_clip(args.get('path') or '.')}`", None, None
    if name == "write_file":
        content = args.get("content") or ""
        n_lines = content.count("\n") + 1 if content else 0
        return (f"📝 escribió `{_clip(args.get('path'))}` ({n_lines} líneas)",
                None, None)
    if name == "move_file":
        return (f"🔀 movió `{_clip(args.get('src'))}` → "
                f"`{_clip(args.get('dst'))}`", None, None)
    if name in expert_history._CONSOLE_TOOLS:
        cmd = str(args.get("cmd") or args.get("command") or "")
        bg = " [background]" if args.get("background") else ""
        cwd = str(args.get("cwd") or "")
        # El encabezado lleva la PRIMERA línea: un heredoc o un script
        # de varias líneas no puede empujar el resto de la tarjeta.
        # El comando entero (y el cwd, que cambia lo que significa) va
        # al cuerpo desplegable.
        detalle = cmd + (f"\n# cwd: {cwd}" if cwd else "")
        return (f"⚙️ shell{bg}: `{_clip(cmd.split(chr(10))[0], 100)}`",
                None, detalle or None)
    if name == "edit_file":
        path = _clip(args.get("path"))
        old = str(args.get("old") or "")
        new = str(args.get("new") or "")
        diff = _unified_diff(old, new, path)
        added = sum(
            1 for ln in diff.splitlines()
            if ln.startswith("+") and not ln.startswith("+++")) if diff else 0
        removed = sum(
            1 for ln in diff.splitlines()
            if ln.startswith("-") and not ln.startswith("---")) if diff else 0
        return f"✏️ editó `{path}` (+{added} −{removed})", diff, None
    if name == "cbm_query":
        tool = args.get("tool") or "query"
        return f"🔎 cbm `{_clip(tool, 40)}`", None, None
    return f"🔧 {name}", None, None


def _unified_diff(old: str, new: str, path: str) -> str | None:
    """Diff unificado capeado de old→new. None si son iguales o vacío."""
    if old == new:
        return None
    import difflib
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=path, tofile=path, lineterm="", n=2))
    if not lines:
        return None
    if len(lines) > _STEP_DIFF_MAX_LINES:
        lines = lines[:_STEP_DIFF_MAX_LINES] + [
            f"… (+{len(lines) - _STEP_DIFF_MAX_LINES} líneas más)"]
    text = "\n".join(lines)
    if len(text) > _STEP_DIFF_MAX_CHARS:
        text = text[:_STEP_DIFF_MAX_CHARS] + "\n…(diff truncado)"
    return text


# Salida de consola que se muestra en la tarjeta. Más chica que el cap
# que ve el modelo (`SHELL_RESULT_CAP`, 48k) a propósito: esto viaja en
# CADA poll de /experts/status (cada 1.5s) y se persiste en
# `chats.progress_events`. Se reserva cola porque el veredicto —el
# `exit=N`, el traceback, el "3 failed"— vive al final.
_STEP_OUT_MAX = 2400
_STEP_OUT_TAIL = 900
# …y un techo para el RUN entero: `progress_events` se guarda en la fila
# del chat sin recorte, y un run de 300 tools escribiendo 2.4KB cada una
# le mete 700KB a la DB. Pasado el techo se deja de adjuntar salida (las
# tarjetas siguen mostrando el comando).
_STEP_OUT_BUDGET = 120_000


def _console_tool_outputs(node) -> list[tuple[str, str, str]]:
    """(tool, salida, tool_call_id) de las tools de consola que devolvieron.

    Lo que contesta la terminal era el dato que la UI nunca mostraba:
    se veía QUÉ tool corrió, nunca con qué salió, así que mirar un run
    en vivo no decía si iba bien o mal.

    ponytail: solo consola. Meter el resultado de TODAS las tools
    multiplicaría por diez el payload del poll y el tamaño de
    `progress_events` en la DB, para mostrar el `read_file` de un
    archivo que ya está en el repo. Si hace falta otra tool, se agrega
    su nombre acá.

    Best-effort como `_measure_tool_returns`: si pydantic-ai cambia la
    forma del nodo dejamos de mostrar salida, nunca rompemos el run.
    """
    out: list[tuple[str, str, str]] = []
    try:
        req = getattr(node, "request", None)
        for part in getattr(req, "parts", []) or []:
            if not isinstance(part, ToolReturnPart):
                continue
            name = getattr(part, "tool_name", None) or ""
            if name not in expert_history._CONSOLE_TOOLS:
                continue
            content = getattr(part, "content", "")
            text = content if isinstance(content, str) else repr(content)
            out.append((name, expert_history._cap_text(
                text.strip() or "(sin salida)",
                _STEP_OUT_MAX, keep_tail=_STEP_OUT_TAIL),
                getattr(part, "tool_call_id", "") or ""))
    except Exception as e:  # noqa: BLE001
        logger.debug("no pude leer la salida de consola del turno: %r", e)
    return out


def _tool_loop_detected(recent: "deque[str]") -> bool:
    """Ventana llena de tool calls idénticas (tool + args) = loop.

    Es el gate del auto-continue de presupuesto (2026-07-20c): si las
    últimas N llamadas son exactamente la misma operación, extender el
    presupuesto solo alarga el loop. Tools variadas = trabajo real.
    ponytail: heurística naive — no ve loops alternados (A,B,A,B…);
    subir a detección con período si aparece ese caso real.
    """
    return len(recent) == recent.maxlen and len(set(recent)) == 1
