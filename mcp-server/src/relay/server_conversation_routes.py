"""Server domain handlers extracted from the composition entrypoint."""
from __future__ import annotations

import asyncio
import json
import uuid
import sqlite3
import hashlib
from functools import wraps

from aiohttp import web

from .server_common import DB_KEY, _require_auth, logger
from . import coordination
from . import experts
from . import finalization
from . import git_flow
from . import identity
from . import issue_links
from . import persist
from . import task_workspace
from . import conversation_creation
from .db import Database
from .user_accounts import AccountError
from .server_conversation_helpers import _cap_text as _raw_cap_text, _steps_from_progress
from .secret_redaction import redact_data, redact_text


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


def _creation_rejected(proof, error=None):
    return web.json_response({"error": error or "La solicitud fue rechazada antes de crear la conversación; puedes confirmar un reintento del borrador",
        "code": "creation_rejected_before_create", "creation_rejection": proof}, status=422)


async def _creation_attempt(db, body, *, sealed_hash=None):
    """Identify a frozen request, before business validation or workspace effects.

    Unidentifiable JSON/IDs cannot claim a different request. A known scope is
    canonicalized; the durable logical barrier also covers case variants.
    """
    if not isinstance(body, dict):
        return None
    slug, request_id = body.get("project"), body.get("request_id")
    if (not isinstance(slug, str) or not slug.strip()
            or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
        return None
    project = await db.get_project(slug.strip())
    scope = project["slug"] if project else slug.strip()
    try:
        contract = (conversation_creation.contract(body) if sealed_hash is None else
                    {"version": 2, "request_id": request_id, "payload_sha256": sealed_hash, "required_issue": None})
    except (TypeError, ValueError, UnicodeError):
        return None
    return {"project_slug": scope, "request_id": request_id, "creation_contract": contract,
            "conversation_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"relay:{scope}:{request_id}"))}


async def _creation_lookup(db, attempt, actor):
    from . import conversation_access
    from .db_conversations import _LOGICAL_CREATION_ROW
    def lookup(conn):
        rows = conn.execute(
            f"SELECT * FROM conversations WHERE id=? OR ({_LOGICAL_CREATION_ROW}) "
            "ORDER BY (id=?) DESC LIMIT 2",
            (attempt["conversation_id"], attempt["project_slug"], attempt["request_id"], attempt["conversation_id"])).fetchall()
        if not rows:
            return {"found": False}
        if len(rows) != 1:
            raise ValueError("La identidad de creación requiere reconciliación administrativa; no se creará otra copia")
        row = dict(rows[0])
        conversation_access.require_tx(conn, row["id"], actor, "control")
        return {"found": True, "id": row["id"], "project_slug": row["project_slug"],
                "creation_receipt": conversation_creation.receipt(row)}
    return await conversation_access.transaction(db, lookup)


def _creation_rejection_boundary(handler):
    """Certify identifiable, settled 4xx results, never absence/500/cancellation.

    Authentication is outside this wrapper. SQLite checks current actor/scope
    and excludes every late INSERT before any rejection proof can be returned.
    A row committed before a later validation/permission error always wins.
    """
    @wraps(handler)
    async def wrapped(request):
        db = request.app[DB_KEY]
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None
        attempt = await _creation_attempt(db, body)
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        except PermissionError:
            response = web.json_response({"error": "Creación no autorizada"}, status=403)
        except AccountError as exc:
            response = web.json_response({"error": str(exc), "connect_url": "/admin/#/account"}, status=409,
                                         headers={"Cache-Control": "no-store"})
        if response.status not in {400, 403, 404, 409, 422} or attempt is None:
            return response
        try:
            proof = await db.reject_creation(project_slug=attempt["project_slug"],
                conversation_id=attempt["conversation_id"], creation_contract=attempt["creation_contract"],
                requested_by=identity.requester(request))
        except (ValueError, PermissionError):
            return response
        if proof is None:
            return response
        try:
            detail = json.loads(response.text or "{}")
        except (ValueError, TypeError):
            detail = {"error": response.reason}
        if not isinstance(detail, dict):
            detail = {"error": response.reason}
        if detail.get("code") and detail["code"] != "creation_rejected_before_create":
            detail["rejection_reason_code"] = detail["code"]
        detail.update(code="creation_rejected_before_create", creation_rejection=proof)
        # Headers with body-specific length/type cannot describe the new JSON.
        headers = {key: value for key, value in response.headers.items()
                   if key.lower() not in {"content-type", "content-length"}}
        headers["Cache-Control"] = "no-store"
        return web.json_response(detail, status=response.status, headers=headers)
    return wrapped


async def _replay_creation(request, db, project, body, previous, contract, authorize_issue):
    from . import conversation_access
    cid = previous["id"]
    try:
        await conversation_access.require(db, cid, identity.requester(request), "control")
    except PermissionError:
        raise web.HTTPNotFound(text="Conversación no disponible.") from None
    previous = await db.get_conversation(cid)
    try:
        current = conversation_creation.require_replay(previous, contract)
    except ValueError as exc:
        return web.json_response({"error": str(exc), "code": "creation_contract_conflict",
                                  "conversation_id": cid}, status=409)
    try:
        required = current.get("required_issue")
        if required is not None and not current["issue_bound"]:
            # Explicit reconciliation of the SAME creation, without repeating
            # workspace provisioning. Resolve again but never substitute a
            # different canonical issue after the original row was inserted.
            resolved = await issue_links.resolve(project, body.get("issue"))
            if conversation_creation.issue_requirement(resolved) != required:
                raise ValueError("GitHub no confirmó la identidad original de la issue requerida")
            await issue_links.bind(db, project, cid, body["issue"], identity.requester(request),
                resolution_complete=body.get("resolution_complete"), authorize=authorize_issue, _resolved=resolved)
        previous = await db.get_conversation(cid)
        current = conversation_creation.require_replay(previous, contract)
    except AccountError:
        raise
    except (ValueError, RuntimeError, OSError) as exc:
        return web.json_response({"error": str(exc), "conversation_id": cid}, status=422)
    return web.json_response({"id": cid, "project_slug": project["slug"],
        "status": previous["status"], "branch": previous.get("branch"),
        "creation_receipt": current}, status=200)


@_require_auth
@_creation_rejection_boundary
@coordination.guard_workspace(DB_KEY, checkout_only=True)
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
        422 -> no se pudo preparar el workspace o validar su remote de GitHub
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
    if not identity.can_write_project(request, project):
        read_only = True
    if identity.role_of(request) != "owner":
        publish_allowed = False
    if project["slug"].lower() == "notes":
        read_only, publish_allowed = True, False
    issue_value = body.get("issue")
    completion = body.get("resolution_complete")
    async def authorize_issue():
        current = await db.get_project(project["slug"])
        if not identity.can_write_project(request, current):
            raise web.HTTPForbidden(text='{"error":"Proyecto no autorizado para tu cuenta"}', content_type="application/json")
        return current
    if completion is not None and (type(completion) is not bool or issue_value is None):
        return web.json_response({"error": "resolution_complete requiere issue y debe ser booleano"}, status=400)
    if issue_value is not None and not identity.can_write_project(request, project):
        return web.json_response({"error": "Proyecto no autorizado para tu cuenta"}, status=403)
    if issue_value is not None:
        try:
            issue_links.reference(issue_value)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
    from pathlib import Path
    if not project.get("repo_path") or not Path(project["repo_path"]).is_dir():
        return web.json_response({"error": "El proyecto no tiene un directorio existente"}, status=422)
    is_git = await git_flow.is_git_repo(project.get("repo_path") or "")
    if not is_git:
        read_only, publish_allowed = True, False
    request_id = body.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
        return web.json_response({"error": "request_id inválido"}, status=400)
    creation_contract = conversation_creation.contract(body) if request_id else None
    wanted_id = (str(uuid.uuid5(uuid.NAMESPACE_URL, f"relay:{project['slug']}:{request_id}"))
                 if request_id else None)
    async def complete_creation(project=project):
        if wanted_id:
            try:
                rejection = await db.get_creation_rejection(wanted_id,
                    requested_by=identity.requester(request), project_slug=project["slug"],
                    creation_contract=creation_contract)
            except ValueError as exc:
                return web.json_response({"error": str(exc), "code": "creation_contract_conflict"}, status=409)
            if rejection:
                return _creation_rejected(rejection)
        if wanted_id:
            existing_creation = await _creation_lookup(db, {
                "conversation_id": wanted_id, "project_slug": project["slug"], "request_id": request_id}, identity.requester(request))
            if existing_creation["found"]:
                previous = await db.get_conversation(existing_creation["id"])
                return await _replay_creation(request, db, project, body, previous, creation_contract, authorize_issue)
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

        # Las tareas aisladas no ocupan el checkout compartido histórico.
        existing = await db.get_open_conversation_for_workspace(project) if is_git else None
        if existing is not None:
            from . import conversation_access
            if not (await conversation_access.get_access(db, existing["id"], identity.requester(request)))["can_read"]:
                return web.json_response({"error": "El workspace del proyecto está ocupado", "code": "workspace_occupied"}, status=409)
            return web.json_response(
                {"error": "ya hay una conversación abierta para este proyecto "
                          "(cerrala con /cerrar antes de abrir otra)",
                 "conversation_id": existing["id"],
                 "code": "workspace_occupied",
                 "branch": existing.get("branch")},
                status=409)

        # Verificar la identidad del issue antes de crear conversación o workspace.
        # El vínculo por sí solo no autoriza su cierre ni cambios de Projects.
        issue_number, resolved_issue = None, None
        if issue_value is not None:
            try:
                from .external_pr import actor_identity
                await actor_identity(identity.requester(request))
                resolved_issue = await issue_links.resolve(project, issue_value)
                project = await issue_links.authorize_project(project, identity.requester(request), authorize_issue)
                issue_number = resolved_issue["number"]
            except AccountError:
                raise
            except (ValueError, RuntimeError, OSError) as exc:
                return web.json_response({"error": str(exc)}, status=422)

        if creation_contract:
            creation_contract["required_issue"] = conversation_creation.issue_requirement(resolved_issue)
        initial_task = dict(role=identity.role_of(request), requested_by=identity.requester(request),
            publish_allowed=publish_allowed if is_git else False, tracking={"enabled": False},
            read_only=read_only, requested_read_only=read_only, state="provisioning" if is_git else "ready")
        if creation_contract:
            initial_task["creation_request"] = creation_contract
        if not is_git:
            initial_task.update(mode="read_only", no_checkout=True, workspace_state="not_applicable", workspace_error="")
        try:
            conv_id = await db.create_conversation(
                project_slug=project["slug"], discord_thread_id=thread_id,
                author=author, conversation_id=wanted_id,
                discord_user_id=discord_user_id, discord_author=discord_author,
                requested_by=identity.requester(request), initial_task=initial_task)
        except conversation_creation.CreationRejected as exc:
            return _creation_rejected(exc.proof)
        except sqlite3.IntegrityError:
            # The SQLite barrier also excludes racing case-alias producers.
            if not wanted_id:
                raise
            existing_creation = await _creation_lookup(db, {
                "conversation_id": wanted_id, "project_slug": project["slug"], "request_id": request_id}, identity.requester(request))
            if not existing_creation["found"]:
                raise
            previous = await db.get_conversation(existing_creation["id"])
            return await _replay_creation(request, db, project, body, previous, creation_contract, authorize_issue)
        task = initial_task
        if is_git:
            try:
                task = await task_workspace.initialize_task(db, project, conv_id, read_only=read_only)
            except (RuntimeError, OSError) as exc:
                return web.json_response({"error": str(exc), "conversation_id": conv_id}, status=422)
        else:
            try:
                task = await task_workspace.initialize_no_checkout(db, project, conv_id)
            except (RuntimeError, OSError) as exc:
                return web.json_response({"error": str(exc), "conversation_id": conv_id}, status=422)
        branch, base_branch = task.get("branch"), "develop" if not read_only else None
        if resolved_issue is not None:
            try:
                await issue_links.bind(db, project, conv_id, issue_value, identity.requester(request),
                                       resolution_complete=completion, authorize=authorize_issue,
                                       _resolved=resolved_issue)
            except AccountError:
                raise
            except (ValueError, RuntimeError, OSError) as exc:
                return web.json_response({"error": str(exc), "conversation_id": conv_id}, status=422)
        logger.info("conversación creada id=%s project=%s thread=%s branch=%s "
                    "discord_user=%s issue=%s",
                    conv_id[:8], project["slug"], thread_id, branch,
                    discord_user_id, issue_number)
        return web.json_response(
            {"id": conv_id, "project_slug": project["slug"], "status": "open",
             "branch": branch, "base_branch": base_branch,
             "discord_user_id": discord_user_id,
             "discord_author": discord_author,
             "issue_number": issue_number,
             "creation_receipt": conversation_creation.receipt(await db.get_conversation(conv_id))},
            status=201)

    if wanted_id:
        from . import task_service
        async with task_service.control_lock(db, wanted_id):
            return await complete_creation()
    return await complete_creation()


@_require_auth
async def conversations_lookup_creation(request: web.Request) -> web.Response:
    """Read-only reconciliation; absence never authorizes a new creation ID."""
    request_id = request.match_info["request_id"]
    slug = request.query.get("project", "").strip()
    if not slug or not 1 <= len(request_id) <= 128:
        return web.json_response({"error": "project y request_id requeridos"}, status=400)
    db = request.app[DB_KEY]
    attempt = await _creation_attempt(db, {"project": slug, "request_id": request_id})
    try:
        result = await _creation_lookup(db, attempt, identity.requester(request))
        if not result["found"]:
            proof = await db.get_creation_rejection(attempt["conversation_id"],
                requested_by=identity.requester(request), project_slug=attempt["project_slug"], request_id=request_id)
            if proof:
                result["creation_rejection"] = proof
    except (PermissionError, ValueError):
        return web.json_response({"error": "Creación no disponible"}, status=404)
    return web.json_response(result, headers={"Cache-Control": "no-store"})


@_require_auth
async def conversations_reject_creation(request: web.Request) -> web.Response:
    """Explicitly seal the frozen attempt; no permission to create or consume.

    This operation does not trust an earlier HTTP status. It either returns an
    actor-controlled existing row or durably bars all later INSERTs of this ID.
    A compact descriptor can carry the original hash without retransmitting a
    body rejected by the HTTP size limit. The hash asserts no execution right.
    Loss of this response leaves the same identity for read-only reconciliation.
    """
    db = request.app[DB_KEY]
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    sealed_hash = None
    if isinstance(body, dict) and ("version" in body or "payload_sha256" in body):
        if set(body) != {"version", "project", "request_id", "payload_sha256"}:
            return web.json_response({"error": "El descriptor debe contener sólo versión, proyecto, request_id y huella"}, status=400)
        candidate = body["payload_sha256"]
        if (type(body["version"]) is not int or body["version"] != 2 or not isinstance(candidate, str)
                or len(candidate) != 64 or any(char not in "0123456789abcdef" for char in candidate)):
            return web.json_response({"error": "La huella original debe ser SHA-256 verificable"}, status=400)
        sealed_hash = candidate
    attempt = await _creation_attempt(db, body, sealed_hash=sealed_hash)
    if attempt is None or body["request_id"] != request.match_info["request_id"]:
        return web.json_response({"error": "Se requiere el cuerpo original identificable y su request_id"}, status=400)
    from . import task_service
    async with task_service.control_lock(db, attempt["conversation_id"]):
        try:
            existing = await _creation_lookup(db, attempt, identity.requester(request))
            if existing["found"]:
                return web.json_response(existing, headers={"Cache-Control": "no-store"})
            proof = await db.reject_creation(project_slug=attempt["project_slug"],
                conversation_id=attempt["conversation_id"], creation_contract=attempt["creation_contract"],
                requested_by=identity.requester(request))
            if proof is not None:
                return web.json_response({"found": False, "creation_rejection": proof}, headers={"Cache-Control": "no-store"})
            # Another process inserted before the barrier. Return its actual CID
            # only after a fresh ACL read; never turn that race into a rejection.
            existing = await _creation_lookup(db, attempt, identity.requester(request))
            if existing["found"]:
                return web.json_response(existing, headers={"Cache-Control": "no-store"})
        except PermissionError:
            return web.json_response({"error": "Creación no disponible para tu cuenta"}, status=403)
        except ValueError as exc:
            return web.json_response({"error": str(exc), "code": "creation_contract_conflict"}, status=409)
    return web.json_response({"error": "No se acreditó el cierre del intento; conserva su identificador"}, status=409)


@_require_auth
async def conversations_list(request: web.Request) -> web.Response:
    """GET /conversations?project=&status=&limit= — sin messages_json."""
    db: Database = request.app[DB_KEY]
    from . import conversation_access
    scope = request.query.get("scope", "mine")
    if scope not in {"mine", "shared"}:
        return web.json_response({"error": "Usa mine o shared; la gestión administrativa tiene su propia vista"}, status=422)
    try:
        limit = max(1, min(int(request.query.get("limit", "100")), 200))
        items = await conversation_access.listing(db, identity.requester(request), scope=scope,
            project=request.query.get("project"), status=request.query.get("status"), limit=limit)
    except PermissionError:
        raise web.HTTPForbidden() from None
    except ValueError:
        return web.json_response({"error": "Límite inválido"}, status=422)
    return web.json_response({"conversations": items, "scope": scope}, headers={"Cache-Control": "no-store"})


@_require_auth
async def conversations_get(request: web.Request) -> web.Response:
    """GET /conversations/{id} — detalle (messages_json como longitud)."""
    db: Database = request.app[DB_KEY]
    conv = await db.get_conversation(request.match_info["id"])
    if conv is None:
        return web.json_response({"error": "no existe"}, status=404)
    conv = dict(conv)
    from . import conversation_access
    try:
        conv["access"] = await conversation_access.require(db, conv["id"], identity.requester(request))
    except PermissionError:
        raise web.HTTPNotFound(text="Conversación no disponible.") from None
    raw = conv.pop("messages_json") or ""
    # Cantidad de elementos del array, no largo del string serializado
    # (bug 2026-09-06: `len(raw)` sobre el JSON como cadena devolvía
    # 39037 para 8 mensajes en c645fea5). Debe coincidir con lo que
    # proyecta `db.list_conversations` vía `json_array_length`, o la
    # sidebar mezcla números en el mismo listado.
    conv["messages_len"] = len(json.loads(raw)) if raw else 0
    conv["context"] = await experts.context_usage_db(db, raw)
    # Tiempo de uso (2026-08-31): la suma de los runs, no el reloj de
    # pared. Lo pinta el chip ⏱ del header y es la misma cuenta que va
    # a las horas del PR en /cerrar.
    usage_time = await db.conversation_usage(conv["id"])
    context = conv["context"]
    actor = identity.requester(request)
    def snapshot(conn):
        row = conn.execute("SELECT * FROM conversations WHERE id=?", (conv["id"],)).fetchone()
        if row is None:
            raise PermissionError("Conversación no disponible")
        view = conversation_access.project_conversation_tx(conn, row, actor)
        history = view.pop("messages_json") or ""
        view["messages_len"] = len(json.loads(history)) if history else 0
        view["context"], view["usage_time"] = context, usage_time
        return view
    try:
        view = await conversation_access.transaction(db, snapshot)
    except PermissionError:
        raise web.HTTPNotFound(text="Conversación no disponible.") from None
    return web.json_response(view, headers={"Cache-Control": "no-store"})


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


@_require_auth
async def conversations_get_request(request: web.Request) -> web.Response:
    """Reconcile an uncertain send without executing or exposing its payload.

    An absent event is only a snapshot: an original POST may still be in
    flight. Callers may replay the identical request ID/payload, never infer
    permission to send an edited request with a new identity from absence.
    """
    from . import conversation_access

    db: Database = request.app[DB_KEY]
    cid = request.match_info["id"]
    request_id = request.match_info["request_id"]
    if not 1 <= len(request_id) <= 128:
        return web.json_response({"error": "request_id inválido"}, status=400)
    actor = identity.requester(request)

    def lookup(conn):
        conversation_access.require_tx(conn, cid, actor, "send")
        row = conn.execute(
            "SELECT e.chat_id,e.state,c.status,e.payload FROM conversation_events e "
            "LEFT JOIN chats c ON c.id=e.chat_id WHERE e.conversation_id=? "
            "AND e.event_key=? AND e.kind='run'", (cid, "request:" + request_id),
        ).fetchone()
        if row is None:
            return {"found": False}
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            raise PermissionError("Solicitud no disponible") from None
        # Shared conversation access does not grant another actor's request
        # identity. Even an administrator reconciles only their own send.
        if not isinstance(payload, dict) or str(payload.get("requested_by") or "").lower() != str(actor).lower():
            raise PermissionError("Solicitud no disponible")
        return {"found": True, "id": row["chat_id"],
                "status": row["status"] or row["state"]}

    try:
        result = await conversation_access.transaction(db, lookup)
    except PermissionError:
        return web.json_response({"error": "Solicitud no disponible"}, status=404)
    return web.json_response(result, headers={"Cache-Control": "no-store"})
