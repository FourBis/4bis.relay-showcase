"""Eventos de herramientas, salidas de consola y redacción de secretos."""
from __future__ import annotations
import json
import hashlib
import logging
import re
from collections import deque
from pydantic_ai.messages import ToolReturnPart
from typing import Any, Optional
from . import expert_history
from .secret_redaction import _SECRETOS_RE, redact_text as _redactar

logger = logging.getLogger("relay.experts")


_STEP_DIFF_MAX_LINES = 40
_STEP_DIFF_MAX_CHARS = 1600

_EVIDENCE_TIMESTAMPS_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b")
_EVIDENCE_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/)[^\s:'\"]+")
_EVIDENCE_DURATION_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|msec|s|sec|secs|seconds?)\b", re.I)
_SHELL_EXIT_RE = re.compile(r"\(exit=(-?\d+)\)\s*$", re.I)
_EVIDENCE_FAILURE_RE = re.compile(
    r"^(?:error\b|failed\b|failure\b|traceback\b|exception\b|"
    r"❌|✖\s*error\b|command failed\b|tool error\b)", re.I)
_MUTATING_TOOLS = {"write_file", "edit_file", "move_file", "delete_file"}


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


#: Redacta credenciales de las copias persistidas o mostradas. La etiqueta se
#: conserva para que el diagnóstico siga siendo legible; los archivos originales
#: y los argumentos que ejecuta la herramienta no se reescriben aquí.
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
            retry = type(part).__name__ == "RetryPromptPart"
            if not isinstance(part, ToolReturnPart) and not retry:
                continue
            name = getattr(part, "tool_name", None) or ""
            if name not in expert_history._CONSOLE_TOOLS:
                continue
            content = getattr(part, "content", "")
            text = content if isinstance(content, str) else repr(content)
            out.append((name, expert_history._cap_text(
                _redactar(text.strip()) or "(sin salida)",
                _STEP_OUT_MAX, keep_tail=_STEP_OUT_TAIL),
                getattr(part, "tool_call_id", "") or ""))
    except Exception as e:  # noqa: BLE001
        logger.debug("no pude leer la salida de consola del turno: %r", e)
    return out


def _tool_loop_detected(recent: "deque[str]") -> bool:
    """Detecta una ventana de fallas repetidas, aun con args distintos."""
    return len(recent) == recent.maxlen and all(
        item == "falla-repetida" for item in recent)


def _tool_stalled(recent: "deque[str]") -> bool:
    """Detecta resultados repetidos; las lecturas/polls no cuentan como avance."""
    return len(recent) == recent.maxlen and all(
        item in ("sin-progreso", "falla-repetida") for item in recent)


def _progress_signature(evidence: Any) -> str:
    """Firma evidencia estructurada para comparar dos puntos del run."""
    value = evidence if isinstance(evidence, str) else json.dumps(
        evidence, sort_keys=True, default=str, ensure_ascii=False)
    value = _EVIDENCE_TIMESTAMPS_RE.sub("<timestamp>", value.strip())
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _tool_result_evidence(node: Any, call_args: dict[str, tuple[str, dict]]) -> list[str]:
    """Firmas estables de resultados MCP para decidir si el run avanzó.

    Las fallas se comparan por resultado, no por comando/args, para que
    variar el comando no oculte una repetición del mismo error. Escrituras
    confirmadas incluyen sus argumentos: `write_file` suele responder con
    el mismo texto aunque haya guardado contenido distinto.
    """
    evidence = []
    try:
        req = getattr(node, "request", None)
        for part in getattr(req, "parts", []) or []:
            retry = type(part).__name__ == "RetryPromptPart"
            if not isinstance(part, ToolReturnPart) and not retry:
                continue
            call_id = getattr(part, "tool_call_id", "") or ""
            tool, args = call_args.get(call_id, (
                getattr(part, "tool_name", None) or "?", {}))
            content = getattr(part, "content", "")
            if not isinstance(content, str):
                content = json.dumps(content, sort_keys=True, default=str,
                                     ensure_ascii=False)
            content = _EVIDENCE_TIMESTAMPS_RE.sub("<timestamp>", content.strip())
            outcome = str(getattr(part, "outcome", "") or "").lower()
            raw_content = getattr(part, "content", None)

            def _has_error(value: Any) -> bool:
                if isinstance(value, dict):
                    return bool(value.get("isError") or value.get("is_error")
                                or value.get("error")) or any(
                                    _has_error(v) for v in value.values())
                if isinstance(value, (list, tuple)):
                    return any(_has_error(v) for v in value)
                return bool(getattr(value, "isError", False)
                            or getattr(value, "is_error", False))

            is_error = _has_error(raw_content)
            exit_match = _SHELL_EXIT_RE.search(content)
            shell_failed = bool(exit_match and int(exit_match.group(1)) != 0)
            failed = (retry or outcome not in ("", "success")
                      or is_error or shell_failed
                      or bool(_EVIDENCE_FAILURE_RE.match(content)))
            if failed:
                # Rutas y timestamps variables no convierten el mismo error
                # en evidencia nueva. El mensaje/causa sí se conserva.
                normalized = _EVIDENCE_PATH_RE.sub("<path>", content)
                normalized = _EVIDENCE_DURATION_RE.sub("<duration>", normalized)
                material = f"error:{tool}:{normalized}"
            else:
                material = f"ok:{tool}:{content}"
                if tool in _MUTATING_TOOLS:
                    material += ":" + json.dumps(
                        args, sort_keys=True, default=str, ensure_ascii=False)
            evidence.append(("error:" if failed else "ok:") +
                            _progress_signature(material))
    except Exception as e:  # noqa: BLE001 — best-effort, nunca frena el run
        logger.warning("no pude calcular evidencia de progreso: %r", e)
        return ["error:" + _progress_signature(
            f"evidence-extraction-failed:{type(e).__name__}")]
    return evidence
