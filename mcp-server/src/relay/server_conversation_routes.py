"""Server domain handlers extracted from the composition entrypoint."""
from __future__ import annotations

import asyncio
import json
import uuid

from aiohttp import web

from .server_common import DB_KEY, _require_auth, logger, relay_config
from . import coordination
from . import experts
from . import finalization
from . import git_flow
from . import github as github_mod
from . import identity
from . import persist
from . import task_service, task_workspace
from .db import Database
from .server_conversation_helpers import _cap_text, _steps_from_progress
@_require_auth
@coordination.guard_workspace(DB_KEY)
async def conversations_create(request: web.Request) -> web.Response:
    """POST /conversations — abre una conversación (comando /nuevo).

    Body:
        project:           str  (requerido, slug)
        discord_thread_id: str  (opcional; el bot lo manda al crear el hilo)
        author:            str  (opcional; username Discord → nombre de rama)

    Las tareas Git de escritura reciben rama y worktree propios desde
    develop comprobada. Las consultas conservan una raíz de sólo lectura.
    Una conversación histórica que ocupa el checkout original sigue
    requiriendo cierre explícito antes de crear tareas nuevas.

    Returns:
        201 -> {id, project_slug, status: "open", branch, base_branch}
        404 -> proyecto desconocido
        409 -> ya hay una conversación abierta para ese proyecto
        422 -> el repo no está en un estado git limpio (no se pudo ramificar)
    """
    db: Database = request.app[DB_KEY]
    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    slug = body.get("project")
    if not isinstance(slug, str) or not slug.strip():
        return web.json_response({"error": "project requerido (string)"}, status=400)
    project = await db.get_project(slug.strip())
    if project is None or not project["enabled"]:
        available = [p["slug"] for p in await db.list_projects()]
        return web.json_response(
            {"error": f"proyecto {slug!r} desconocido o deshabilitado",
             "available_projects": available},
            status=404,
        )
    thread_id = body.get("discord_thread_id")
    if thread_id is not None and not isinstance(thread_id, str):
        return web.json_response(
            {"error": "discord_thread_id debe ser string"}, status=400)
    author = body.get("author")
    if author is not None and not isinstance(author, str):
        return web.json_response({"error": "author debe ser string"}, status=400)
    read_only = body.get("read_only", bool((project.get("defaults_json") or {}).get("read_only")))
    publish_allowed = body.get("publish_allowed", False)
    if not isinstance(read_only, bool) or not isinstance(publish_allowed, bool):
        return web.json_response({"error": "read_only y publish_allowed deben ser booleanos"}, status=400)
    if identity.role_of(request) != "owner":
        read_only, publish_allowed = True, False
    if project["slug"].lower() == "notes":
        read_only, publish_allowed = True, False
    is_git = await git_flow.is_git_repo(project.get("repo_path") or "")
    if not is_git:
        read_only, publish_allowed = True, False
    request_id = body.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
        return web.json_response({"error": "request_id inválido"}, status=400)
    wanted_id = (str(uuid.uuid5(uuid.NAMESPACE_URL, f"relay:{project['slug']}:{request_id}"))
                 if request_id else None)
    if wanted_id and (previous := await db.get_conversation(wanted_id)):
        return web.json_response({"id": wanted_id, "project_slug": project["slug"],
                                  "status": previous["status"], "branch": previous.get("branch")}, status=200)
    # Iter 10.0: bridge Discord↔UI. Si el bot C# crea una conversación
    # trayendo el user_id del autor, lo guardamos para que la UI pueda
    # mandar replies de vuelta a Discord después.
    discord_user_id = body.get("discord_user_id")
    if discord_user_id is not None and not isinstance(discord_user_id, str):
        return web.json_response(
            {"error": "discord_user_id debe ser string"}, status=400)
    discord_author = body.get("discord_author")
    if discord_author is not None and not isinstance(discord_author, str):
        return web.json_response(
            {"error": "discord_author debe ser string"}, status=400)

    # Vinculación automática (2026-08-17). Si hay un Discord por default
    # configurado y el caller no trajo uno, la conversación NACE avisando.
    # Sin esto había que acordarse de apretar el botón antes de irse —
    # justo el momento en el que uno no se acuerda. Lo explícito gana:
    # una conversación que ya trae su user_id (las que abre el bot) no se
    # toca.
    if not (discord_user_id or "").strip():
        auto_id, auto_author = relay_config.default_discord_user()
        if auto_id:
            discord_user_id = auto_id
            discord_author = (discord_author or "").strip() or auto_author or None

    # Las tareas aisladas no ocupan el checkout compartido histórico.
    existing = await db.get_open_conversation_for_workspace(project)
    if existing is not None:
        return web.json_response(
            {"error": "ya hay una conversación abierta para este proyecto "
                      "(cerrala con /cerrar antes de abrir otra)",
             "conversation_id": existing["id"],
             "branch": existing.get("branch")},
            status=409)

    # Seguimiento por GitHub (fase 4): `issue: N` ata la conversación a un
    # issue. Se valida ACÁ contra GitHub — una conversación atada a un
    # issue inexistente después abre un PR con un `Closes #N` que no
    # cierra nada, y eso se descubre tarde y a mano.
    issue_number = body.get("issue")
    if issue_number is not None:
        if isinstance(issue_number, bool) or not isinstance(issue_number, int):
            return web.json_response(
                {"error": "issue debe ser un entero"}, status=400)
        slug_gh = await github_mod.repo_slug(project["repo_path"] or "")
        if not slug_gh:
            return web.json_response(
                {"error": "el repo no tiene remote de GitHub: no puedo atar "
                          "la conversación a un issue"}, status=422)
        if await github_mod.issue(slug_gh, issue_number) is None:
            return web.json_response(
                {"error": f"issue #{issue_number} no existe en {slug_gh} "
                          f"(o `gh` no está autenticado)"}, status=404)

    conv_id = await db.create_conversation(
        project_slug=project["slug"], discord_thread_id=thread_id,
        author=author, conversation_id=wanted_id,
        discord_user_id=discord_user_id, discord_author=discord_author,
        requested_by=identity.requester(request))
    task = {}
    if is_git:
        await db.update_conversation_task(conv_id, role=identity.role_of(request),
                                          requested_by=identity.requester(request), publish_allowed=publish_allowed,
                                          tracking={"enabled": False})
        try:
            task = await task_workspace.initialize_task(db, project, conv_id, read_only=read_only)
        except (RuntimeError, OSError) as exc:
            return web.json_response({"error": str(exc), "conversation_id": conv_id}, status=422)
    branch, base_branch = task.get("branch"), "develop" if not read_only else None
    if issue_number is not None:
        await db.set_conversation_issue(conv_id, issue_number)
    logger.info("conversación creada id=%s project=%s thread=%s branch=%s "
                "discord_user=%s issue=%s",
                conv_id[:8], project["slug"], thread_id, branch,
                discord_user_id, issue_number)
    return web.json_response(
        {"id": conv_id, "project_slug": project["slug"], "status": "open",
         "branch": branch, "base_branch": base_branch,
         "discord_user_id": discord_user_id,
         "discord_author": discord_author,
         "issue_number": issue_number},
        status=201)


@_require_auth
async def conversations_list(request: web.Request) -> web.Response:
    """GET /conversations?project=&status=&limit= — sin messages_json."""
    db: Database = request.app[DB_KEY]
    limit = min(int(request.query.get("limit", "20")), 200)
    items = await db.list_conversations(
        project_slug=request.query.get("project") or None,
        status=request.query.get("status") or None,
        limit=limit)
    if identity.role_of(request) != identity.OWNER_ROLE:
        items = [{**item, "task_json": json.dumps(task_service.visible_task(item.get("task_json")))}
                 for item in items]
    return web.json_response({"conversations": items})


@_require_auth
async def conversations_get(request: web.Request) -> web.Response:
    """GET /conversations/{id} — detalle (messages_json como longitud)."""
    db: Database = request.app[DB_KEY]
    conv = await db.get_conversation(request.match_info["id"])
    if conv is None:
        return web.json_response({"error": "no existe"}, status=404)
    conv = dict(conv)
    if identity.role_of(request) != identity.OWNER_ROLE:
        conv["task_json"] = json.dumps(task_service.visible_task(conv.get("task_json")))
    raw = conv.pop("messages_json") or ""
    # Cantidad de elementos del array, no largo del string serializado
    # (bug 2026-09-06: `len(raw)` sobre el JSON como cadena devolvía
    # 39037 para 8 mensajes en un run de ejemplo). Debe coincidir con lo que
    # proyecta `db.list_conversations` vía `json_array_length`, o la
    # sidebar mezcla números en el mismo listado.
    conv["messages_len"] = len(json.loads(raw)) if raw else 0
    conv["context"] = await experts.context_usage_db(db, raw)
    # Tiempo de uso (2026-08-31): la suma de los runs, no el reloj de
    # pared. Lo pinta el chip ⏱ del header y es la misma cuenta que va
    # a las horas del PR en /cerrar.
    conv["usage_time"] = await db.conversation_usage(conv["id"])
    return web.json_response(conv)


@_require_auth
async def conversations_get_messages(request: web.Request) -> web.Response:
    """GET /conversations/{id}/messages — messages_json parseado a turnos.

    Devuelve una lista de turnos legibles para la UI:
        [{"role": "user"|"assistant"|"tool", "content": str, "tool_name"?: str}]

    Best-effort: si el JSON está corrupto, devuelve 200 con `messages: []`
    y un warning en `error`. Nunca 500 por basura del cliente.

    Caps defensivos (bug fix 2026-07-08 — Sub-ola 2.4):
      - `?max_turns=N` (default 200): tope de turnos en el response
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
        return web.json_response({"error": "no existe"}, status=404)

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
        # que trabajó — el agujero de un hilo de ejemplo (2026-08-13).
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
        md_turns.extend(turns)

    if md_turns:
        # Aplicar caps (max_turns / content_cap) igual que el camino viejo.
        out: list[dict] = []
        truncated_turns = 0
        for t in md_turns:
            role = t.get("role", "")
            content = t.get("content", "") or ""
            capped = _cap_text(content, content_cap)
            turn = {
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
            if len(out) >= max_turns:
                truncated_turns += 1
                break
        return web.json_response({
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
        return web.json_response({
            "conversation_id": conv_id,
            "messages": [],
            "turn_count": 0,
            "truncated": False,
            "content_cap": content_cap,
            "max_turns": max_turns,
        })

    raw = conv.get("messages_json") or ""
    if not raw.strip():
        return web.json_response({"conversation_id": conv_id, "messages": [],
                                 "turn_count": 0, "truncated": False})
    try:
        msgs = json.loads(raw)
    except json.JSONDecodeError as e:
        return web.json_response(
            {"conversation_id": conv_id, "messages": [],
             "error": f"JSON corrupto: {e}", "truncated": False})
    out: list[dict] = []
    truncated_turns = 0
    for m in msgs:
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
        # cortar si pasamos max_turns (respetar orden natural del historial)
        if len(out) >= max_turns:
            truncated_turns += 1
            break
    return web.json_response({
        "conversation_id": conv_id,
        "messages": out,
        "turn_count": len(out),
        "truncated": truncated_turns > 0,
        "content_cap": content_cap,
    })
