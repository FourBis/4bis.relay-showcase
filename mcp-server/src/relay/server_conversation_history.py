"""Historial legible de conversaciones con redacción y ACL final."""
from __future__ import annotations

import asyncio
import hashlib
import json

from aiohttp import web

from . import experts, finalization, identity, persist
from .db import Database
from .secret_redaction import redact_data, redact_text
from .server_common import DB_KEY, _require_auth, logger
from .server_conversation_helpers import _cap_text as _raw_cap_text, _steps_from_progress


def _cap_text(text, cap):
    # Redact before capping: truncating a credential first can remove the
    # syntax/prefix needed to recognize it on a historical display copy.
    return _raw_cap_text(redact_text(text), cap)

def _history_message_id(origin, position, value):
    # Bound to the uncapped, redacted source, so a sliding recent window or
    # a larger content_cap cannot silently select a different response.
    encoded = json.dumps([origin, position, redact_data(value)],
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

@_require_auth
async def conversations_get_messages(request: web.Request) -> web.Response:
    """GET /conversations/{id}/messages — messages_json parseado a turnos.

    Devuelve una lista de turnos legibles para la UI:
        [{"role": "user"|"assistant"|"tool", "content": str, "tool_name"?: str}]

    Best-effort: si el JSON está corrupto, devuelve 200 con `messages: []`
    y un warning en `error`. Nunca 500 por basura del cliente.

    Caps defensivos (bug fix 2026-07-08 — Sub-ola 2.4):
      - `?max_turns=N` (default 200): ventana de los últimos turnos,
        conservando su orden cronológico; `truncated` indica historia anterior.
      - `?content_cap=N` (default 4000): tope de chars por turno
        individual. Turnos más largos se truncan con sufijo
        `… [truncado, N chars totales]`.
      - Sin estos caps, una conversación con 50+ turnos y tool calls
        grandes (cbm_query sobre INVENTORYDEMO = 30KB de JSON por tool return)
        podía devolver varios MB al cliente, lo que colgaba el modal
        de la UI. Ahora el response es bounded.
    """
    db: Database = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    async def history_response(payload, *, status=200):
        # Reading old MD/outputs can await slow IO. Revalidate after all of it,
        # including empty/error returns, and do not yield after this decision.
        from . import conversation_access
        try:
            await conversation_access.require(db, conv_id, identity.requester(request), "read")
        except PermissionError:
            return web.json_response({"error": "Conversación no disponible"}, status=404,
                                     headers={"Cache-Control": "no-store"})
        return web.json_response(payload, status=status, headers={"Cache-Control": "no-store"})
    # parsear params con fallback a defaults
    try:
        max_turns = int(request.query.get("max_turns", "200"))
    except (TypeError, ValueError):
        max_turns = 200
    try:
        content_cap = int(request.query.get("content_cap", "4000"))
    except (TypeError, ValueError):
        content_cap = 4000
    # acotar a límites razonables (anti-DoS del cliente)
    max_turns = max(1, min(max_turns, 2000))
    content_cap = max(100, min(content_cap, 100_000))

    conv = await db.get_conversation(conv_id)
    if conv is None:
        return await history_response({"error": "no existe"}, status=404)

    # Resultado durable primero: un MD viejo, inaccesible o pendiente no
    # debe ocultar una respuesta confirmada. MD para los chats históricos;
    # messages_json queda como último fallback, porque puede estar compactado.
    md_turns: list[dict] = []
    chats = await db.list_chats_by_conversation(conv_id)
    for c in chats:
        md_path = c.get("md_path")
        turns = []
        if c.get("output_payload"):
            try:
                turns = finalization.turns(c["output_payload"])
            except (ValueError, TypeError, AttributeError, KeyError):
                logger.warning("chat output inválido: %s", c.get("id"))
        try:
            if not turns and md_path:
                turns = await asyncio.to_thread(persist.parse_chat_md_turns, md_path)
        except Exception as e:  # noqa: BLE001 — best-effort por run
            logger.warning(
                "skipping chat md conv=%s chat=%s err=%r",
                conv_id[:8], c.get("id", "?")[:8], e,
            )
            continue
        # Los pasos del run cuelgan de SU turno assistant: en la UI son el
        # bloque "proceso" colapsado que va arriba de esa respuesta.
        # Con ellos van el contador de tools y la fase final: sin eso, un
        # turno que anunció y no ejecutó (0 tools) se leía igual que uno
        # que trabajó — el agujero del hilo a292ca08 (2026-08-13).
        steps = _steps_from_progress(c.get("progress_events"))
        for t in turns:
            if t.get("role") == "assistant":
                if steps:
                    t["steps"] = steps
                t["tool_calls"] = c.get("tool_calls")
                t["phase_at_end"] = c.get("phase_at_end")
                t["run_status"] = c.get("status")
                t["duration_ms"] = c.get("duration_ms")
                try:
                    stages = json.loads(c.get("stages_json") or "{}")
                    t["stages"] = stages if isinstance(stages, dict) else {}
                except (ValueError, TypeError):
                    t["stages"] = {}
                t["export_pending"] = c.get("exported") == 0
                t["export_error"] = c.get("export_error") or ""
                break
        # Quién pidió ESTE turno (ADR-037). Cuelga del turno `user`
        # porque es su prompt: en un hilo compartido por el túnel, dos
        # turnos seguidos pueden ser de dos personas distintas, y sin
        # esto la UI los pinta a los dos como "tú".
        for t in turns:
            if t.get("role") == "user":
                t["requested_by"] = c.get("requested_by")
                break
        for index, turn in enumerate(turns):
            turn["message_id"] = _history_message_id(c["id"], index, {
                key: turn.get(key) for key in ("role", "content", "tool_name")})
        md_turns.extend(turns)

    if md_turns:
        # Aplicar caps (max_turns / content_cap) igual que el camino viejo.
        out: list[dict] = []
        for t in md_turns:
            role = t.get("role", "")
            content = t.get("content", "") or ""
            capped = _cap_text(content, content_cap)
            turn = {
                "message_id": t["message_id"],
                "role": role,
                "content": capped["text"],
                "truncated": capped["truncated"],
                "total_chars": capped["total_chars"],
            }
            if "tool_name" in t:
                turn["tool_name"] = t["tool_name"]
            if t.get("steps"):
                turn["steps"] = t["steps"]
            if t.get("tool_calls") is not None:
                turn["tool_calls"] = t["tool_calls"]
            if t.get("phase_at_end"):
                turn["phase_at_end"] = t["phase_at_end"]
            if t.get("requested_by"):
                turn["requested_by"] = t["requested_by"]
            for key in ("run_status", "duration_ms", "stages", "export_pending", "export_error"):
                if key in t:
                    turn[key] = t[key]
            if capped["text"] or role == "tool" or "tool_name" in t:
                out.append(turn)
            elif role == "assistant":
                # assistant vacío (modelo de razonamiento): placeholder
                # accionable, igual que el camino viejo. Los pasos van
                # igual — es justo el caso donde el proceso es TODO lo
                # que quedó del run.
                out.append({**turn, "content": "", "truncated": False, "total_chars": 0})
        # Keep the newest bounded window in chronological order. Taking the
        # first N entries made every later reply disappear on reopening a
        # long conversation, despite its durable output being present.
        truncated_turns = max(0, len(out) - max_turns)
        out = redact_data(out[-max_turns:])
        return await history_response({
            "conversation_id": conv_id,
            "messages": out,
            "turn_count": len(out),
            "truncated": truncated_turns > 0,
            "content_cap": content_cap,
            "max_turns": max_turns,
        })

    # Hay chats pero todos los .md estan rotos/ilegibles: NO caemos al
    # JSON (puede ser el resumen de la compactacion). Mejor [] honesto.
    if chats:
        return await history_response({
            "conversation_id": conv_id,
            "messages": [],
            "turn_count": 0,
            "truncated": False,
            "content_cap": content_cap,
            "max_turns": max_turns,
        })

    raw = conv.get("messages_json") or ""
    if not raw.strip():
        return await history_response({"conversation_id": conv_id, "messages": [],
                                 "turn_count": 0, "truncated": False})
    try:
        msgs = json.loads(raw)
    except json.JSONDecodeError as e:
        return await history_response(
            {"conversation_id": conv_id, "messages": [],
             "error": f"JSON corrupto: {e}", "truncated": False})
    out: list[dict] = []
    for source_index, m in enumerate(msgs):
        previous_count = len(out)
        kind = m.get("kind", "")
        parts = m.get("parts") or []
        if kind == "request":
            # juntar texto de UserPromptPart
            text_parts = []
            for p in parts:
                if p.get("part_kind") == "user-prompt":
                    c = p.get("content", "")
                    text_parts.append(
                        c if isinstance(c, str) else json.dumps(c))
            content = _cap_text("\n".join(text_parts).strip(), content_cap)
            if content["text"]:
                out.append({
                    "role": "user",
                    "content": content["text"],
                    "truncated": content["truncated"],
                    "total_chars": content["total_chars"],
                })
            # tool returns también vienen como "request" en algunos flujos
            for p in parts:
                if p.get("part_kind") == "tool-return":
                    tc = p.get("content", "")
                    capped = _cap_text(
                        tc if isinstance(tc, str) else json.dumps(tc),
                        content_cap)
                    out.append({"role": "tool", "tool_name": "tool",
                                "content": capped["text"],
                                "truncated": capped["truncated"],
                                "total_chars": capped["total_chars"]})
        elif kind == "response":
            text_parts, tool_parts = [], []
            for p in parts:
                pk = p.get("part_kind", "")
                if pk == "text":
                    text_parts.append(p.get("content", ""))
                elif pk == "tool-call":
                    # Guardamos el part entero (no solo el nombre): los args
                    # sirven para reconstruir el diff de edit_file al releer
                    # el hilo (Fase 2 UI, 2026-07-20e).
                    tool_parts.append(p)
            if text_parts:
                content = _cap_text("".join(text_parts).strip(), content_cap)
                if content["text"]:
                    out.append({
                        "role": "assistant",
                        "content": content["text"],
                        "truncated": content["truncated"],
                        "total_chars": content["total_chars"],
                    })
                elif not tool_parts:
                    # Respuesta final con texto vacío: modelos de
                    # razonamiento (MiniMax-M3) a veces vuelcan todo en el
                    # `thinking` y devuelven texto en blanco. Antes este
                    # turno se dropeaba y la UI mostraba "1 turno" sin la
                    # respuesta → parecía que el experto no contestó.
                    # Emitimos el turno vacío para que la UI muestre un
                    # placeholder accionable. (Si hubo tool-calls NO va:
                    # ese turno ya se representa con los chips de tool.)
                    out.append({
                        "role": "assistant", "content": "",
                        "truncated": False, "total_chars": 0,
                    })
            for tp in tool_parts:
                tn = tp.get("tool_name", "?")
                # args puede venir dict o JSON string (pydantic-ai).
                args = tp.get("args")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, ValueError):
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                # línea legible (📄 leyó `x` · ✏️ editó `y` +N−M) + diff
                # (solo edit_file). Mismo formato que el streaming en vivo.
                summary, diff, cmd = experts._format_tool_step(tn, args)
                turn = {"role": "tool", "tool_name": tn,
                        "summary": summary,
                        "content": f"(llamó {tn})",
                        "truncated": False,
                        "total_chars": len(f"(llamó {tn})")}
                if diff:
                    turn["diff"] = diff
                # El comando entero. Acá no hay salida: este camino lee
                # `messages_json`, donde el tool return es otro turno.
                if cmd:
                    turn["cmd"] = cmd
                out.append(turn)
        for part_index, turn in enumerate(out[previous_count:]):
            turn["message_id"] = _history_message_id(
                ["legacy", source_index], part_index, m)
    truncated_turns = max(0, len(out) - max_turns)
    out = redact_data(out[-max_turns:])
    return await history_response({
        "conversation_id": conv_id,
        "messages": out,
        "turn_count": len(out),
        "truncated": truncated_turns > 0,
        "content_cap": content_cap,
    })
