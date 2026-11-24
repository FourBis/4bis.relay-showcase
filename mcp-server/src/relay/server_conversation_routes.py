"""Server domain handlers extracted from the composition entrypoint."""
from __future__ import annotations

import json
import uuid
import sqlite3
from functools import wraps

from aiohttp import web

from .server_common import DB_KEY, _require_auth, logger
from . import coordination
from . import experts
from . import git_flow
from . import identity
from . import issue_links
from . import task_workspace
from . import conversation_creation
from .db import Database
from .user_accounts import AccountError
from .server_conversation_history import (
    _cap_text, _history_message_id, conversations_get_messages,
)


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
