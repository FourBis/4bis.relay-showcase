"""Server domain handlers extracted from the composition entrypoint."""
from __future__ import annotations

import asyncio
import json
import base64
import uuid
import sqlite3
from typing import Optional

from aiohttp import web

from .server_common import (
    BG_TASKS_KEY, COMMANDS_KEY, DB_KEY, GRAFOS_KEY, MCP_POOL_KEY, NOTIFY_KEY, PROGRESS_KEY,
    RUNNING_KEY, SESSIONS_KEY, SKILLS_KEY, _require_auth, _spawn_bg, logger,
)
from . import attachments as attachments_mod
from . import coordination
from . import experts
from . import github as github_mod
from . import identity
from . import memory
from . import notify
from . import persist
from . import progress
from . import task_service, task_workspace
from .commands import CommandContext, CommandRegistry
from .db import Database
from .notify import NotifyClient
from .skills import SkillCache
from .server_expert_jobs import _run_expert_bg
from .secret_redaction import redact_data
from . import server_expert_controls as _controls
from .server_expert_controls import _graph_chat_cancel_conflict


async def _require_conversation_send(request, db, cid):
    from . import conversation_access
    try:
        return await conversation_access.require(db, cid, identity.requester(request), "send")
    except PermissionError:
        raise web.HTTPNotFound(text="Conversación no disponible.") from None




@_require_auth
@coordination.guard_workspace(DB_KEY, enqueue=True, checkout_only=True)
async def experts_run(request: web.Request) -> web.Response:
    """POST /experts/run — corre el experto pydantic-ai de un proyecto.

    ADR-024: ASYNC. Responde 202 inmediato; el resultado (o error)
    llega al bot por POST /notify con agent_id "chat:<id>" y metadata
    {chat_id, conversation_id, discord_thread_id, tokens, ...}.

    Body:
        target:       str  (requerido, slug del proyecto)
        user:         str  (requerido)
        system:       str  (opcional, se concatena a las instructions)
        source:       str  (opcional, ej "discord")
        author:       str  (opcional)
        model:        str  (opcional, override "provider:modelo")
        conversation: str  (opcional, id de conversación abierta — ADR-025;
                            replaya el historial y lo persiste al terminar)
        discord_thread_id: str  (opcional; si no viene `conversation`, el
                            relay busca la conversación abierta de ese hilo
                            o crea una nueva — el bot queda sin estado)
        memory:       str  (opcional, query FTS5 — ADR-027; inyecta el
                            bloque "Memoria de conversaciones previas")

    Returns:
        202 -> {id, status: "running", conversation_id}
        400 -> body mal formado / conversación de otro proyecto
        404 -> proyecto o conversación desconocidos
        409 -> conversación cerrada (cerrar es cerrar, ADR-025)
    """
    db: Database = request.app[DB_KEY]
    skills: SkillCache = request.app[SKILLS_KEY]
    running: dict = request.app[RUNNING_KEY]
    progress: dict = request.app[PROGRESS_KEY]
    notify: NotifyClient = request.app[NOTIFY_KEY]

    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)

    target = body.get("target")
    user = body.get("user")
    if not isinstance(target, str) or not target.strip():
        return web.json_response({"error": "target requerido (string)"}, status=400)
    if not isinstance(user, str) or not user.strip():
        return web.json_response({"error": "user requerido (string)"}, status=400)
    target = target.strip()
    source = body.get("source") or "api"
    if isinstance(source, str) and source.strip().casefold() == "discord":
        return web.json_response({"error": "bridge_audience_unverified",
            "message": "Usa Relay con tu cuenta; no se puede verificar la audiencia de Discord."}, status=409)
    author = body.get("author") or ""
    system_extra = body.get("system") or ""
    model_override = body.get("model") or ""
    # Modelo por rol, SOLO para este run (2026-08-26). `model` sigue
    # siendo el del ejecutor; esto cubre las otras tres etapas, que
    # hasta ahora solo se podían cambiar por .env (global) o por
    # `defaults_json` del proyecto (permanente). El popover del chat
    # manda esto; el default sigue siendo no mandar nada.
    #
    # No se valida contra el catálogo a propósito: el desplegable que lo
    # manda ya se llena con los modelos prendidos, y un spec inventado
    # muere con un ModelUnavailable que nombra el modelo. Lo que sí se
    # valida es la forma, que es lo que hace de esto un borde de
    # confianza: claves conocidas, valores string, nada más.
    raw_stage = body.get("stage_models")
    stage_models: dict[str, str] = {}
    _stage_roles = {"planner", "verifier", "documenter"}
    if raw_stage is not None and not isinstance(raw_stage, dict):
        return web.json_response(
            {"error": "stage_models debe ser un objeto"}, status=400)
    if isinstance(raw_stage, dict):
        if _unknown := sorted(set(raw_stage) - _stage_roles):
            return web.json_response(
                {"error": "stage_models tiene roles desconocidos: "
                          + ", ".join(_unknown)}, status=400)
        if _invalid := sorted(k for k, v in raw_stage.items()
                              if not isinstance(v, str)):
            return web.json_response(
                {"error": "stage_models requiere valores string: "
                          + ", ".join(_invalid)}, status=400)
        for _rol in ("planner", "verifier", "documenter"):
            _v = raw_stage.get(_rol)
            if isinstance(_v, str) and _v.strip():
                stage_models[_rol] = _v.strip()
    # F1 (plan MCP_REGISTRY): selección explícita de MCPs on-demand.
    # `--con db` / `--with db,docs` en el texto, o body "with": [...].
    user, mcp_with = experts.parse_mcp_flags(user)
    # Working set scope (2026-07-20d): `--solo src/auth/*` acota la
    # atención del experto. Se inyecta como system_extra (mismo canal
    # que ya fluye a run_expert), no toca los tools — es una directiva.
    user, scope_paths = experts.parse_scope_flags(user)
    if scope_paths:
        _sb = experts.scope_block(scope_paths)
        system_extra = f"{system_extra}\n\n{_sb}" if system_extra else _sb
    # Selección explícita de skills: `--skill pdf` / body "skills": [...].
    # Fuerza al run una skill `manual` (no auto-inyectada), mismo canal
    # system_extra que --solo. La lista de disponibles sale en !ayuda.
    user, skill_names = experts.parse_skill_flags(user)
    body_skills = body.get("skills")
    if isinstance(body_skills, list):
        skill_names.extend(str(s) for s in body_skills if s)
    if skill_names:
        _skb = await asyncio.to_thread(
            skills.render_requested_block, skills.dir, skill_names)
        if _skb:
            system_extra = f"{system_extra}\n\n{_skb}" if system_extra else _skb
    if not user:
        return web.json_response(
            {"error": "user vacío (solo flags --con/--with/--solo/--skill)"},
            status=400)
    body_with = body.get("with")
    if isinstance(body_with, list):
        mcp_with.extend(str(s) for s in body_with if s)

    # `!comando` escrito en el chat (2026-08-01). El prefijo `!` lo
    # entendía SOLO el bot de Discord; en el chat web se iba al LLM como
    # prompt cualquiera — medido: dos "!ayuda" quemaron dos runs de
    # experto en website-demo. El guard va ACÁ y no en la UI porque este
    # endpoint es el camino compartido de todos los callers (web, bot,
    # extensión): uno solo cubre a los tres.
    #
    # Estrictamente aditivo: si el primer token no es un comando cuyo
    # handler resolvió, sigue de largo al experto como hasta hoy (un
    # "!ojo con esto" no se secuestra). El target del chat entra como
    # `project`/`target` para los handlers que lo piden (build, fact);
    # si al comando le falta un arg, el ValueError del registry se
    # devuelve tal cual, que ya es un mensaje entendible.
    if user.startswith("!"):
        registry: CommandRegistry = request.app[COMMANDS_KEY]
        cmd_name = user[1:].split()[0].lower() if len(user) > 1 else ""
        if cmd_name and cmd_name in registry.names():
            ctx = CommandContext(
                db=db, sessions=request.app[SESSIONS_KEY], running=running,
                source=source, author=author)
            ok = True
            try:
                text = await registry.dispatch(
                    cmd_name, {"project": target, "target": target}, ctx)
            except (ValueError, RuntimeError) as e:
                text = f"`!{cmd_name}` falló: {e}"
                ok = False
            return web.json_response({"command": cmd_name, "text": text, "ok": ok})

    project = await db.get_project(target)
    if project is None or not project["enabled"]:
        available = [p["slug"] for p in await db.list_projects()]
        return web.json_response(
            {"error": f"proyecto {target!r} desconocido o deshabilitado",
             "available_projects": available},
            status=404,
        )


    # Etapas apagadas en el proyecto (2026-08-26). El popover del chat
    # ofrece los tres roles sin saber la config del proyecto, así que
    # `stage_models` podía traer un verificador para un proyecto que
    # tiene el verificador apagado: `run_expert_staged` lo descartaba en
    # silencio y el humano se quedaba creyendo que había elegido algo.
    # Se descarta igual —no es un error que justifique tirar el run—
    # pero se dice, y la UI lo muestra.
    ignored_stages: list[str] = []
    if stage_models:
        _d = project.get("defaults_json") or {}
        if not _d.get("three_stage", True):
            ignored_stages = list(stage_models)      # no corre ninguna
        else:
            ignored_stages = [
                rol for rol in ("verifier", "documenter")
                if rol in stage_models and not _d.get(rol, True)]
        for _rol in ignored_stages:
            stage_models.pop(_rol, None)
        if ignored_stages:
            logger.info(
                "experts_run: %s pidió modelo para etapa(s) apagada(s) en "
                "%s: %s", author or source, target, ", ".join(ignored_stages))

    # ADR-025: conversación (opcional). Validar ANTES de crear el chat.
    conversation: Optional[dict] = None
    conv_id = body.get("conversation") or body.get("conversation_id") or ""
    if "expected_creation" in body and (not conv_id or not isinstance(body["expected_creation"], dict)):
        return web.json_response({"error": "expected_creation requiere conversación y contrato"}, status=400)
    if conv_id:
        if not isinstance(conv_id, str):
            return web.json_response(
                {"error": "conversation debe ser string"}, status=400)
        conversation = await db.get_conversation(conv_id)
        if conversation is None:
            return web.json_response(
                {"error": f"conversación {conv_id!r} desconocida"}, status=404)
        await _require_conversation_send(request, db, conv_id)
        managed = await db.get_conversation_task(conv_id)
        if "expected_creation" in body and not managed:
            return web.json_response({"error": "La creación no tiene un contrato de tarea verificable"}, status=409)
        if conversation["status"] != "open" and not managed:
            return web.json_response(
                {"error": "conversación cerrada (cerrar es cerrar; abre una "
                          "nueva con /nuevo)", "conversation_id": conv_id},
                status=409)
        if conversation["project_slug"].lower() != project["slug"].lower():
            return web.json_response(
                {"error": f"la conversación pertenece a "
                          f"{conversation['project_slug']!r}, no a "
                          f"{project['slug']!r}"},
                status=400)
        if managed and not identity.can_control_task(request, project, managed, conversation):
            return web.json_response({"error": "No tienes permiso para continuar esta tarea."}, status=403)
        if managed.get("state") in task_service.STOPPED:
            return web.json_response({"error": managed.get("error") or "Continúa la tarea desde sus controles",
                                      "conversation_id": conv_id}, status=409)
        # touch al INICIO: el sweeper nunca ve stale una conv con run vivo
        await db.touch_conversation(conv_id)

    # Auto-attach por hilo Discord (fix 2026-07-12): si el caller no
    # trae `conversation` pero sí `discord_thread_id`, el relay resuelve
    # la conversación solo — sin esto, cada mensaje del hilo era un chat
    # amnésico y el /notify salía con discord_thread_id null.
    thread_id = body.get("discord_thread_id")
    discord_user_id = body.get("discord_user_id")  # Iter 10.0
    discord_author = body.get("discord_author")    # Iter 10.0
    if conversation is None and thread_id:
        if not isinstance(thread_id, str):
            return web.json_response(
                {"error": "discord_thread_id debe ser string"}, status=400)
        conversation = await db.get_open_conversation_by_thread(
            project["slug"], thread_id)
        if conversation is not None:
            await _require_conversation_send(request, db, conversation["id"])
        if conversation is None:
            from . import git_flow
            from pathlib import Path
            repo = project.get("repo_path") or ""
            if not repo or not Path(repo).is_dir():
                return web.json_response({"error": "El proyecto no tiene un directorio existente"}, status=422)
            thread_is_git = await git_flow.is_git_repo(repo)
            existing = await db.get_open_conversation_for_workspace(project) if thread_is_git else None
            if existing is not None:
                from . import conversation_access
                if not (await conversation_access.get_access(db, existing["id"], identity.requester(request)))["can_read"]:
                    return web.json_response({"error": "El workspace del proyecto está ocupado", "code": "workspace_occupied"}, status=409)
                return web.json_response(
                    {"error": "ya hay una conversación abierta para este repositorio",
                     "code": "workspace_occupied",
                     "conversation_id": existing["id"]}, status=409)
            initial_task = {"role": identity.role_of(request), "requested_by": identity.requester(request),
                "publish_allowed": False, "tracking": {"enabled": False}, "state": "provisioning",
                "read_only": not identity.can_write_project(request, project) if thread_is_git else True}
            if not thread_is_git:
                initial_task.update(mode="read_only", no_checkout=True, state="ready", workspace_state="not_applicable")
            new_id = await db.create_conversation(
                project_slug=project["slug"], discord_thread_id=thread_id,
                discord_user_id=discord_user_id,
                discord_author=discord_author,
                requested_by=identity.requester(request), initial_task=initial_task)
            read_only = not identity.can_write_project(request, project)
            try:
                await task_workspace.initialize_task(db, project, new_id, read_only=read_only)
            except (RuntimeError, OSError) as exc:
                return web.json_response({"error": str(exc), "conversation_id": new_id}, status=422)
            conversation = await db.get_conversation(new_id)
        else:
            managed = await db.get_conversation_task(conversation["id"])
            if managed and not identity.can_control_task(request, project, managed, conversation):
                return web.json_response({"error": "No tienes permiso para continuar esta tarea."}, status=403)
            await db.touch_conversation(conversation["id"])
            # Si la conversación ya existía pero le llegan estos campos
            # por primera vez (caso: thread viejo sin discord_user_id),
            # los guardamos — útil para que el bridge funcione con
            # hilos que ya estaban abiertos cuando se creó el campo.
            if discord_user_id and not conversation.get("discord_user_id"):
                await db.set_conversation_discord_user(
                    conversation["id"],
                    discord_user_id=discord_user_id,
                    discord_author=discord_author)
                conversation = await db.get_conversation(conversation["id"])

    # Un pedido sin hilo sobre un repo Git también necesita identidad y raíz propia.
    if conversation is None:
        from . import git_flow
        from pathlib import Path
        repo = project.get("repo_path") or ""
        if not repo or not Path(repo).is_dir():
            return web.json_response({"error": "El proyecto no tiene un directorio existente"}, status=422)
        if repo:
            request_id = body.get("request_id")
            if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
                return web.json_response({"error": "request_id inválido"}, status=400)
            cid = (str(uuid.uuid5(uuid.NAMESPACE_URL, f"relay:implicit:{target}:{request_id}")) if request_id else None)
            conversation = await db.get_conversation(cid) if cid else None
            if conversation:
                await _require_conversation_send(request, db, cid)
                if not (await db.get_conversation_task(cid)).get("mode"):
                    return web.json_response({"error": "La conversación aún no tiene un workspace preparado; revisa sus controles antes de continuar.",
                        "code": "conversation_initializing", "conversation_id": cid}, status=409)
            if not conversation:
                readonly = body.get("read_only", bool((project.get("defaults_json") or {}).get("read_only")))
                if not isinstance(readonly, bool):
                    return web.json_response({"error": "read_only debe ser booleano"}, status=400)
                readonly = readonly or not identity.can_write_project(request, project)
                try:
                    cid = await db.create_conversation(project_slug=target, author=author,
                        requested_by=identity.requester(request), conversation_id=cid,
                        initial_task={"role": identity.role_of(request), "requested_by": identity.requester(request),
                            "publish_allowed": False, "tracking": {"enabled": False}, "read_only": readonly, "state": "provisioning"})
                except sqlite3.IntegrityError:
                    if not cid or not await db.get_conversation(cid):
                        raise
                    await _require_conversation_send(request, db, cid)
                    # Another identical ingress owns initialization. Do not run
                    # through the legacy/stateless fallback while it is pending.
                    if not (await db.get_conversation_task(cid)).get("mode"):
                        return web.json_response({"error": "La conversación se está preparando; vuelve a enviar el mismo pedido.",
                            "code": "conversation_initializing", "conversation_id": cid}, status=409)
                try:
                    await task_workspace.initialize_task(db, project, cid, read_only=readonly)
                except (RuntimeError, OSError) as exc:
                    return web.json_response({"error": str(exc), "conversation_id": cid}, status=422)
                conversation = await db.get_conversation(cid)

    if conversation is not None:
        await _require_conversation_send(request, db, conversation["id"])
    managed = await db.get_conversation_task(conversation["id"]) if conversation else {}
    if not managed:
        # Historical empty tasks remain untouched. A directory without Git
        # still cannot inherit code-write/shell permissions from project defaults.
        from . import git_flow
        from pathlib import Path
        repo = project.get("repo_path") or ""
        if not repo or not Path(repo).is_dir():
            return web.json_response({"error": "El proyecto no tiene un directorio existente"}, status=422)
        if not await git_flow.is_git_repo(repo):
            project = {**project, "defaults_json": {**(project.get("defaults_json") or {}), "read_only": True}}
    if managed and not identity.can_control_task(request, project, managed, conversation):
        return web.json_response({"error": "No tienes permiso para continuar esta tarea."}, status=403)
    # Seguimiento por GitHub (fase 4): si la conversación nació de un
    # issue, el experto arranca sabiendo qué tiene que resolver. Se lee
    # de GitHub en cada run (caché de 60s) en vez de copiarse a la DB:
    # si alguien edita el issue, el run siguiente ve la versión nueva.
    # Best-effort: sin `gh` el run sigue igual, solo sin el bloque.
    if conversation and conversation.get("issue_number"):
        gh_slug = await github_mod.repo_slug(project["repo_path"] or "")
        data = (await github_mod.issue(gh_slug, conversation["issue_number"])
                if gh_slug else None)
        if data:
            blk = github_mod.issue_block(data)
            system_extra = f"{system_extra}\n\n{blk}" if system_extra else blk

    # ADR-027: retrieval manual de memoria (si el caller lo pide).
    memory_query = body.get("memory") or ""
    if not isinstance(memory_query, str):
        return web.json_response({"error": "memory debe ser texto"}, status=400)
    if memory_query and not managed and conversation:
        hits = await db.search_memories(project["slug"], memory_query, limit=3,
            actor=identity.requester(request), source_conversation=conversation["id"])
        block = memory.build_memory_block(hits)
        if block:
            system_extra = f"{system_extra}\n\n{block}" if system_extra else block

    # Iter Discord-attachment: si el bot mandó una lista de `attachments`
    # (ids que ya subió via POST /discord/attachments), armamos un bloque
    # estándar y lo concatenamos al `user` que se le pasa al LLM. Si el
    # campo está ausente o vacío, no tocamos nada (back-compat al 100%).
    # Scope de la vista de adjuntos (2026-08-26): la conversación cuando
    # hay una, si no el proyecto. Los dos aíslan entre clientes, que es
    # lo que importa; la conversación además hace que un id nombrado tres
    # mensajes atrás siga resolviendo, porque su hardlink ya quedó en la
    # vista desde el turno en que se citó.
    attach_scope = attachments_mod.scope_for(
        conversation["id"] if conversation else "", project["slug"])
    raw_attachments = body.get("attachments")
    valid_ids = []
    images: list[tuple[bytes, str]] = []
    if isinstance(raw_attachments, list) and raw_attachments:
        # Filtramos strings vacíos y casteamos a str (defensivo).
        valid_ids = [str(a).strip() for a in raw_attachments
                     if isinstance(a, (str,)) and a.strip()]
        from . import conversation_access
        for attach_id in valid_ids:
            if not await conversation_access.can_read_attachment(db, identity.requester(request), attach_id):
                return web.json_response({"error": "Adjunto no disponible para esta cuenta"}, status=404)
        block = attachments_mod.format_user_block(valid_ids, attach_scope)
        if block:
            user = f"{user.rstrip()}\n{block}"
        # Las imágenes van aparte, como partes binarias del prompt
        # (2026-07-31): el `user` sigue siendo str para el jsonl, el .md
        # y el historial. El bloque de arriba las nombra; acá viajan.
        images = attachments_mod.load_images(valid_ids)
        if images:
            # 2026-08-18: no todos los endpoints aceptan imágenes. Se
            # corta ACÁ y no adentro del run: un modelo ciego igual
            # contesta —con seguridad y sobre lo que no vio— y eso es
            # peor que un error. Cortar cuesta 0 tokens y el selector de
            # la UI está a un click. El guard va en el endpoint y no en
            # la UI por el mismo motivo que los roles: esconder no es
            # impedir.
            spec_efectivo = experts.resolve_model_spec(model_override, project)
            if not experts.has_vision(spec_efectivo):
                con_vista = ", ".join(
                    m["spec"] for m in experts.catalog()
                    if m.get("vision") == 1 and m.get("enabled")) or "ninguno cargado"
                return web.json_response(
                    {"error": "modelo sin visión",
                     "model": spec_efectivo,
                     "message": (
                         f"{spec_efectivo} no ve imágenes y adjuntaste "
                         f"{len(images)}. Elegí un modelo con visión "
                         f"({con_vista}) en el selector del chat, o sacá "
                         f"el adjunto.")},
                    status=400)
            logger.info("adjuntos: %d imagen(es) van al modelo en chat de %s",
                        len(images), target)

    try:
        skills_block = await skills.get_block()
    except Exception as e:
        logger.warning("skills: get_block rompio: %r (sigo sin skills)", e)
        skills_block = ""

    if conversation and managed:
        managed = await db.get_conversation_task(conversation["id"])
        if not identity.can_control_task(request, project, managed, conversation):
            return web.json_response({"error": "No tienes permiso para continuar esta tarea."}, status=403)
        if managed.get("state") in task_service.STOPPED:
            return web.json_response({"error": "Continúa la tarea desde sus controles"}, status=409)
        request_id = body.get("request_id") or str(uuid.uuid4())
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            return web.json_response({"error": "request_id inválido"}, status=400)
        payload = {"user": user, "system_extra": system_extra, "model_override": model_override,
                   "memory_query": memory_query,
                   "knowledge_scope_version": 1,
                   "stage_models": stage_models, "mcp_with": mcp_with, "source": source,
                   "author": author, "target": target, "role": identity.role_of(request),
                   "requested_by": identity.requester(request),
                   "images": [(base64.b64encode(raw).decode("ascii"), mime) for raw, mime in images]}
        if "expected_creation" in body:
            payload["expected_creation"] = body["expected_creation"]
        try:
            # enable_write holds this same lock across its empty-queue check
            # and promotion. The SQL assertion then shares the event commit.
            async with task_service.control_lock(db, conversation["id"]):
                event = await db.enqueue_conversation_event(conversation["id"], "request:" + request_id, "run", payload,
                                                           authorized_actor=identity.requester(request))
                await db.run("UPDATE conversations SET status='open', closed_at=NULL WHERE id=?", (conversation["id"],))
                task_service.start_pending(request.app, conversation["id"])
        except PermissionError:
            return web.json_response({"error": "Conversación no disponible"}, status=404)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        return web.json_response({"id": event["chat_id"], "status": "queued" if event["state"] == "pending" else "running",
                                  "conversation_id": conversation["id"], "event_id": event["id"],
                                  "ignored_stages": ignored_stages}, status=202)

    chat_id = await db.create_chat(
        project_slug=target, source=source, author=author, target=target,
        conversation_id=conversation["id"] if conversation else None,
        requested_by=identity.requester(request),
        user_prompt=user,
    )
    await persist.append_jsonl(target, "user", user, author=author, chat=chat_id)

    task = asyncio.create_task(_run_expert_bg(
        db=db, notify=notify, running=running, progress=progress,
        chat_id=chat_id,
        project=project, user=user, skills_block=skills_block,
        system_extra=system_extra, model_override=model_override,
        stage_models=stage_models,
        target=target, source=source, author=author,
        conversation=conversation,
        mcp_with=mcp_with, mcp_pool=request.app.get(MCP_POOL_KEY),
        images=images,
        # Para el disparador del grafo: la task de fondo se registra en
        # la app, así el apagado la espera y /graphs/{id}/cancel la ubica.
        app=request.app,
    ))
    running[chat_id] = task
    coordination.hold_current(task)
    bg_tasks: set = request.app[BG_TASKS_KEY]
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)

    return web.json_response(
        {"id": chat_id, "status": "running",
         "conversation_id": conversation["id"] if conversation else None,
         # Vacío en el caso normal; la UI solo avisa si trae algo.
         "ignored_stages": ignored_stages},
        status=202,
    )


@_require_auth
async def experts_cancel(request: web.Request) -> web.Response:
    """Cancela una ejecución identificada y espera su cierre durable."""
    db, running = request.app[DB_KEY], request.app[RUNNING_KEY]
    chat_id = request.match_info["chat_id"]
    matches = [cid for cid in running if cid.startswith(chat_id)]
    for cid in matches or [chat_id]:
        if not await identity.can_control_chat(request, db, await db.get_chat(cid)):
            return web.json_response({"error": "No tienes permiso para controlar este run."}, status=403)
    if len(matches) > 1:
        return web.json_response({"error": "Prefijo ambiguo; indica el ID completo."}, status=409)
    chat_id = matches[0] if matches else chat_id
    event = await db.conversation_event_for_chat(chat_id)

    async def finish_cancel():
        if event:
            async with task_service.control_lock(db, event["conversation_id"]):
                return await _cancel_expert(request, chat_id)
        return await _cancel_expert(request, chat_id)

    return await asyncio.shield(_spawn_bg(finish_cancel(), hold_workspace=False))




async def _cancel_expert(request, chat_id):
    # Resolve the facade callback at call time for existing monkeypatch callers.
    return await _controls.cancel_expert(
        request, chat_id, graph_conflict=_graph_chat_cancel_conflict)


@_require_auth
async def experts_steer(request: web.Request) -> web.Response:
    return await _controls.steer_expert(request)


@_require_auth
async def experts_status(request: web.Request) -> web.Response:
    """GET /experts/status/{chat_id} — liveness on-demand (Fase 1 A).

    Devuelve un snapshot del RunProgress: elapsed_s, idle_s, phase,
    last_tool, tool_calls, tokens, model, finished, error.

    Si el chat ya terminó, devolvemos el último estado conocido
    (finished=true). Si nunca existió, 404. El store es en memoria
    del proceso — si el relay reinició, los runs viejos se pierden
    y devolvemos 404 aunque el chat exista en SQLite (no podemos
    reconstruir el progreso, solo el .md final).
    """
    progress: dict = request.app[PROGRESS_KEY]
    chat_id = request.match_info["chat_id"]
    matches = [cid for cid in progress if cid.startswith(chat_id)]
    from .private_mcp_access import visible_rows
    def unavailable():
        return web.json_response(
            {"error": "no hay run con ese id (o el relay reinició)",
             "lost_on_restart_possible": True}, status=404,
            headers={"Cache-Control": "no-store"})
    if not matches:
        db = request.app[DB_KEY]
        event = await db.conversation_event_for_chat(chat_id)
        if event:
            chat = await db.get_chat(chat_id)
            if not chat:
                return unavailable()
            payload = {"chat_id": chat_id, "target": chat.get("project_slug"),
                "phase": "queued" if event["state"] == "pending" else chat.get("status"),
                "finished": event["state"] in {"applied", "uncertain", "cancelled"},
                "error": event.get("error"), "elapsed_s": 0, "tool_calls": chat.get("tool_calls") or 0,
                "tokens_in": chat.get("tokens_in"), "tokens_out": chat.get("tokens_out"), "steps": []}
            if not await visible_rows(request, [{"chat_id": chat_id,
                                                  "conversation_id": chat.get("conversation_id")}]):
                return unavailable()
            return web.json_response(redact_data(payload), headers={"Cache-Control": "no-store"})
        # Distinguir "no existe" de "existe pero reinicié": el caller
        # puede ir a /chats/{id} para ver el resultado final.
        return unavailable()
    snapshots = await visible_rows(request, [redact_data(progress[cid].snapshot()) for cid in matches])
    if not snapshots:
        return unavailable()
    if len(snapshots) > 1:
        return web.json_response(
            {"matches": [row["chat_id"] for row in snapshots], "snapshots": snapshots},
            headers={"Cache-Control": "no-store"})
    return web.json_response(snapshots[0], headers={"Cache-Control": "no-store"})
