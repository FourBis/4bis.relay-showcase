"""Server domain handlers extracted from the composition entrypoint."""
from __future__ import annotations

import json
import uuid

from aiohttp import web

from .server_common import DB_KEY, _require_auth, _spawn_bg, logger
from . import coordination
from . import git_flow
from . import identity
from . import task_service, task_workspace
from .db import Database
from .server_conversation_helpers import compact_live_conversation
from .server_conversation_jobs import (
    _PR_JOBS, _compact_and_store, _finalize_pr_bg,
)


async def _conversation_project(db, conv, *, read_only=False):
    project = await db.get_project(conv["project_slug"])
    if not project:
        return project
    try:
        if read_only:
            task = await db.get_conversation_task(conv["id"])
            if not task:
                return project
            _, workspace = task_workspace._safe_task_paths(db, project, conv["id"], task)
            return {**project, "repo_path": str(workspace)}
        return await task_workspace.resolved_project(db, project, conv["id"])
    except (RuntimeError, OSError) as exc:
        raise web.HTTPUnprocessableEntity(text=json.dumps({"error": str(exc)}),
                                          content_type="application/json") from exc

@_require_auth
@coordination.guard_workspace(DB_KEY, source="conversation")
async def conversations_compact(request: web.Request) -> web.Response:
    """POST /conversations/{id}/compact — baja el contexto sin cerrar.

    Corre inline (el compactador tarda decenas de segundos y el caller
    quiere el número): si el cliente corta antes, la compactación
    termina igual del lado del relay.

    Returns:
        200 -> {id, status, context_before, chars_before, chars_after, summary}
        404 -> no existe
        409 -> cerrada, sin historial, o con un run en curso
        502 -> el compactador falló (el hilo queda intacto)
    """
    db: Database = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response({"error": "no existe"}, status=404)
    if conv.get("status") != "open":
        return web.json_response(
            {"error": "la conversación está cerrada"}, status=409)
    if not (conv.get("messages_json") or "").strip():
        return web.json_response(
            {"error": "el hilo todavía no tiene historial que compactar"},
            status=409)
    # Un run vivo reescribe messages_json al terminar: compactar ahora
    # sería trabajo tirado (y el humano vería el contexto igual de alto).
    runs = await db.list_chats(status="running", limit=50)
    if any(c.get("conversation_id") == conv_id for c in runs):
        return web.json_response(
            {"error": "hay un run en curso en este hilo; espera a que "
                      "termine (o cancelalo) y compacta después"},
            status=409)
    out = await compact_live_conversation(db, conv, actor=identity.requester(request))
    if not out["ok"]:
        return web.json_response({"error": out["error"]}, status=502)
    return web.json_response({
        "id": conv_id, "status": "open",
        "context_before": out["before"],
        "chars_before": out["chars_before"],
        "chars_after": out["chars_after"],
        "facts": out["facts"],
        "summary": out["summary"],
    })


@_require_auth
@coordination.guard_workspace(DB_KEY, source="conversation")
async def conversations_close(request: web.Request) -> web.Response:
    """POST /conversations/{id}/close — cierra + compacta (comando /cerrar).

    Idempotente: cerrar una cerrada re-dispara la compactación si no
    tiene summary todavía (reintento manual del compactador).

    El PR a develop se abre EN BACKGROUND (`pr: "running"`): incluye
    verify (build+test, hasta 420s) y la redacción del body con el LLM,
    y eso no entra en un request HTTP — la UI cortaba a los 30s y
    mostraba "no pude cerrar" mientras el PR se abría igual (bug
    2026-07-21). El estado se consulta con GET /conversations/{id}/pr.
    El PR develop→main lo hace el usuario a mano.

    Returns:
        200 -> {id, status: "closed", compaction, pr, pr_url?}
        404 -> no existe
    """
    db: Database = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response({"error": "no existe"}, status=404)
    was_open = conv.get("status") == "open"
    await db.close_conversation(conv_id)
    if await db.get_conversation_task(conv_id):
        # Cerrar la vista no finaliza la tarea ni borra rama, archivos o historial.
        return web.json_response({"id": conv_id, "status": "closed", "compaction": "skipped",
                                  "pr": "skipped", "pr_url": conv.get("pr_url")})

    # PR a develop (solo la primera vez que se cierra y si hubo rama).
    pr_url = conv.get("pr_url")
    project = await _conversation_project(db, conv)
    branch = conv.get("branch")
    pr_state = "skipped"
    if (identity.role_of(request) == "owner" and was_open and branch
            and project and project.get("repo_path") and not pr_url):
        _spawn_bg(_finalize_pr_bg(db, conv_id, conv["project_slug"],
                                  project, branch, conv.get("summary") or "",
                                  issue_number=conv.get("issue_number"),
                                  requested_by=identity.requester(request)))
        pr_state = "running"

    compaction = "skipped"
    if conv.get("messages_json") and not (conv.get("summary") or "").strip():
        _spawn_bg(_compact_and_store(
            db, conv_id, conv["project_slug"], conv["messages_json"], actor=identity.requester(request)),
            hold_workspace=False)
        compaction = "running"
    logger.info("conversación cerrada id=%s compaction=%s branch=%s pr=%s",
                conv_id[:8], compaction, branch, pr_url or pr_state)
    resp: dict = {"id": conv_id, "status": "closed", "compaction": compaction,
                  "pr": pr_state}
    if pr_url:  # ya tenía PR de un /cerrar anterior
        resp["pr_url"] = pr_url
    return web.json_response(resp)


@_require_auth
async def conversation_pr_status(request: web.Request) -> web.Response:
    """GET /conversations/{id}/pr — estado del PR lanzado por /close.

    La UI poll-ea esto mientras corre el verify. `state`:
    verifying|describing|opening|done|error, o `unknown` si el job no
    está en memoria (relay reiniciado, o /close viejo): en ese caso
    igual devolvemos el `pr_url` persistido si ya existe.

    Returns:
        200 -> {state, pr_url?, error?, draft, committed, verify?}
        404 -> la conversación no existe
    """
    db: Database = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    job = _PR_JOBS.get(conv_id)
    if job is not None:
        return web.json_response({"id": conv_id, **job})
    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response({"error": "no existe"}, status=404)
    pr_url = conv.get("pr_url")
    return web.json_response({
        "id": conv_id, "state": "done" if pr_url else "unknown",
        "pr_url": pr_url, "error": None, "draft": False, "committed": False,
    })


@_require_auth
async def conversation_branch_status(request: web.Request) -> web.Response:
    """GET /conversations/{id}/branch — estado de la rama local de la conv.

    La UI del panel de chat usa esto para decidir si el botón "borrar
    rama local" es seguro (merged=True, solo se ve merged en develop) o
    destructivo (merged=False, hay commits sin mergear). Devolver
    `ahead`/`behind`/`is_current` le permite al confirm del modal
    decirle al humano exactamente qué se va a perder si insiste.

    Si la conversación no tiene rama (no era un repo git en el /nuevo,
    o el /nuevo falló antes de crear la rama), devuelve 200 con
    `{branch: null, exists: false, ...}`: la UI muestra el botón
    deshabilitado o lo esconde.

    Returns:
        200 -> {id, branch, base, exists, merged, ahead, behind,
                is_current, current_branch, error?}
        404 -> conv desconocida
    """
    db: Database = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response(
            {"error": f"conversación {conv_id!r} desconocida"}, status=404)
    branch = conv.get("branch")
    if not branch:
        # Conv sin rama (no era repo git al crearse, o el relay no creó
        # una). Devolvemos un payload neutral para que la UI oculte el botón.
        return web.json_response({
            "id": conv_id, "branch": None, "base": None,
            "exists": False, "merged": False, "ahead": 0, "behind": 0,
            "is_current": False, "current_branch": "", "error": None,
        })
    project = await _conversation_project(db, conv, read_only=True)
    repo_path = (project or {}).get("repo_path") or ""
    if not repo_path:
        return web.json_response({
            "id": conv_id, "branch": branch, "base": None,
            "exists": False, "merged": False, "ahead": 0, "behind": 0,
            "is_current": False, "current_branch": "",
            "error": "proyecto sin repo_path",
        })
    # Base = develop si existe local/remoto, si no el trunk. Misma
    # lógica que el resto del flujo de git.
    base = await git_flow.work_base_branch(repo_path)
    status = await git_flow.branch_status(repo_path, branch, base=base)
    return web.json_response({"id": conv_id, **status})


@_require_auth
@coordination.guard_workspace(DB_KEY, source="conversation")
async def conversation_branch_delete(request: web.Request) -> web.Response:
    """DELETE /conversations/{id}/branch — borra la rama LOCAL de la conv.

    El endpoint existe porque cada /cerrar deja la rama
    `<autor>-<YYYY-MM-DD>` en el disco del repo, y eso se va
    acumulando. La rama remota la maneja el humano por GitHub (merge
    de develop→main, delete branch on merge, etc.); acá solo
    limpiamos la copia local.

    Reglas:
      1. La conv tiene que estar `closed`. Una conv abierta = trabajo
         en curso, no se toca.
      2. Si la rama es la actualmente checked-out, rechaza: borrar
         bajo tus pies te deja en detached HEAD. Devuelve 409 con el
         nombre de la rama actual para que el humano haga checkout.
      3. `?force=true` permite borrar aunque no esté mergeada a base
         (destructivo). Sin force usa `git branch -d` que falla si
         hay commits sin mergear — eso es el "safe path".
      4. NO toca el remoto. Ni `git push --delete`, ni `gh api`.

    Body: ninguno (todo en query string).

    Returns:
        200 -> {id, branch, deleted, was_current, error}
        404 -> conv desconocida
        409 -> conv abierta, o la rama es la actual del checkout
        422 -> git falló (`error` trae el detalle)
    """
    db: Database = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response(
            {"error": f"conversación {conv_id!r} desconocida"}, status=404)
    if conv.get("status") == "open":
        return web.json_response(
            {"error": "conversación abierta; cerrala antes de borrar la rama",
             "status": "open"},
            status=409)
    branch = conv.get("branch")
    if not branch:
        return web.json_response(
            {"error": "esta conversación no tiene rama"}, status=422)
    if await db.get_conversation_task(conv_id):
        return web.json_response({"error": "La rama pertenece a un workspace persistente. "
            "Se conserva para revisión; no se elimina con el borrado de ramas histórico."}, status=409)
    project = await _conversation_project(db, conv)
    repo_path = (project or {}).get("repo_path") or ""
    if not repo_path:
        return web.json_response(
            {"error": "proyecto sin repo_path"}, status=422)
    force = request.query.get("force", "").lower() in ("1", "true", "yes")
    result = await git_flow.delete_local_branch(repo_path, branch, force=force)
    if result.get("was_current"):
        return web.json_response(
            {"error": result["error"], "branch": branch,
             "current_branch": result.get("current_branch", branch)},
            status=409)
    if not result.get("deleted"):
        return web.json_response(
            {"error": result.get("error") or "git no pudo borrar la rama",
             "branch": branch, "repo_path": repo_path},
            status=422)
    logger.info("rama local borrada conv=%s branch=%s force=%s",
                conv_id[:8], branch, force)
    return web.json_response({
        "id": conv_id, "branch": branch, "deleted": True,
        "was_current": False, "error": None,
    })


@_require_auth
async def conversation_diff(request: web.Request) -> web.Response:
    """GET /conversations/{id}/diff — qué cambió en la rama de la conv.

    Git puro, sin LLM: `git diff base...branch` (los commits que van al
    PR) + lo que quedó sin commitear en el working tree. El `git-diff`
    por proyecto que ya existía solo mira el tree, así que en cuanto el
    experto commitea deja de mostrar nada — este es el que responde
    "¿qué me cambió el experto en esta conversación?".

    Tres vistas sobre lo mismo (2026-08-24, visor navegable):

      - sin query params: el diff ENTERO en dos bloques (compat: es lo
        que consume el `/diff` del bot y los tests viejos).
      - `?view=files`: LISTA de archivos con contadores, sin texto. Es
        lo primero que pide el visor — `--numstat` no lo corta el cap,
        así que la lista está completa aunque el diff pese megas.
      - `?path=<archivo>`: el diff de ESE archivo. Uno por vez, así el
        browser no pinta 5MB de una.

    Query:
        cap:     chars máximos por diff (default 200k, tope 1M).
        view:    `files` para la lista de archivos.
        path:    archivo puntual (relativo al repo).
        mode:    rango — `all` (default, merge-base→working tree, lo que
                 va a quedar en el PR), `committed`, `pending`.
        context: líneas de contexto del diff de un archivo (default 3).

    Returns:
        200 -> {id, branch, base, exists, is_current, commits, stat, diff,
                full_size, truncated, pending_*, untracked, error?}
        200 (view=files) -> {id, pr_url, files, totals, remote_ahead, …}
        200 (path=…) -> {id, path, diff, truncated, binary, untracked, …}
        404 -> conv desconocida
        422 -> conv sin rama, o proyecto sin repo_path
    """
    db: Database = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response(
            {"error": f"conversación {conv_id!r} desconocida"}, status=404)
    branch = conv.get("branch")
    if not branch:
        return web.json_response(
            {"error": "esta conversación no tiene rama de trabajo"}, status=422)
    project = await _conversation_project(db, conv, read_only=True)
    repo_path = (project or {}).get("repo_path") or ""
    if not repo_path:
        return web.json_response(
            {"error": "proyecto sin repo_path"}, status=422)
    try:
        cap = min(max(int(request.query.get("cap", git_flow.DIFF_CAP)), 1_000),
                  1_000_000)
    except ValueError:
        cap = git_flow.DIFF_CAP
    mode = (request.query.get("mode") or "all").strip().lower()
    path = (request.query.get("path") or "").strip()
    if path:
        try:
            context = int(request.query.get("context", 3))
        except ValueError:
            context = 3
        out = await git_flow.diff_file(
            repo_path, branch, path, mode=mode, cap=cap, context=context,
            old_path=(request.query.get("old") or "").strip())
        return web.json_response({"id": conv_id, "branch": branch, **out})
    if (request.query.get("view") or "").strip().lower() == "files":
        out = await git_flow.diff_file_list(repo_path, branch, mode=mode)
        return web.json_response({"id": conv_id, "pr_url": conv.get("pr_url"),
                                  **out})
    out = await git_flow.conversation_diff(repo_path, branch, cap=cap)
    return web.json_response({"id": conv_id, "pr_url": conv.get("pr_url"),
                              **out})


@_require_auth
@coordination.guard_workspace(DB_KEY, source="conversation")
async def conversation_git_action(request: web.Request) -> web.Response:
    """POST /conversations/{id}/git/{action} — git de la rama del hilo.

    Las acciones que un dev hace después de mirar el diff. Las corre el
    RELAY, no el experto (mismo criterio que /nuevo y /cerrar: el git es
    determinista o no es), y con los guards del flujo de ramas puestos en
    `git_flow`, no acá — así valen igual si mañana los llama el bot.

      commit     {message, paths?}  — paths vacío = todo (`git add -A`)
      push       {}                 — `git push -u origin <rama>`
      pr         {title?, body?}    — push + PR a develop SIN cerrar el hilo
      merge      {method?}          — `gh pr merge` del PR, solo si va a develop
      sync-base  {}                 — fetch + fast-forward de la base
      restore    {paths}            — descarta cambios sin commitear (destructivo)

    `merge` y `restore` son las dos destructivas y la UI las pone detrás
    de un confirm; el guard del server es el que importa igual:
    `merge_pr` rechaza cualquier PR que no apunte a develop (main/master
    no se mergean desde un botón) y `restore_paths` exige que HEAD esté
    en la rama del hilo.

    Returns:
        200 -> payload de la acción (`error: null`)
        400 -> acción desconocida o body inválido
        404 -> conv desconocida
        422 -> conv sin rama / sin repo_path, o git falló (`error` explica)
    """
    db: Database = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    accion = request.match_info["action"]
    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response(
            {"error": f"conversación {conv_id!r} desconocida"}, status=404)
    branch = conv.get("branch")
    if not branch:
        return web.json_response(
            {"error": "esta conversación no tiene rama de trabajo"}, status=422)
    project = await _conversation_project(db, conv)
    repo_path = (project or {}).get("repo_path") or ""
    if not repo_path:
        return web.json_response({"error": "proyecto sin repo_path"}, status=422)
    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    if not isinstance(body, dict):
        return web.json_response({"error": "body debe ser un objeto"}, status=400)

    managed = await db.get_conversation_task(conv_id)
    if managed and accion == "pr":
        key = body.get("request_id") or str(uuid.uuid4())
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            return web.json_response({"error": "request_id inválido"}, status=400)
        if await db.run("SELECT id FROM conversation_events WHERE conversation_id=? AND event_key=?",
                        (conv_id, "publish:" + key)):
            return web.json_response(await task_service.snapshot(db, conv_id))
        if managed.get("state") in task_service.STOPPED:
            return web.json_response({"error": "Continúa o reconcilia la tarea antes de publicar"}, status=409)
        await db.update_conversation_task(conv_id, publish_allowed=True, state="ready")
        await db.enqueue_conversation_event(
            conv_id, "publish:" + key, "publish",
            {"role": "owner", "requested_by": identity.requester(request)})
        task_service.start_pending(request.app, conv_id)
        return web.json_response({"id": conv_id, "branch": branch, "state": "queued", "pr_url": conv.get("pr_url")}, status=202)

    if accion == "commit":
        out = await git_flow.commit_paths(
            repo_path, branch, message=str(body.get("message") or ""),
            paths=body.get("paths"))
    elif accion == "push":
        out = await git_flow.push_branch(repo_path, branch)
    elif accion == "pr":
        titulo = str(body.get("title") or "").strip() or f"{branch}: cambios del hilo"
        out = await git_flow.open_pr(repo_path, branch, title=titulo,
                                     body=str(body.get("body") or ""))
        if out.get("pr_url"):
            await db.set_conversation_pr(conv_id, out["pr_url"])
    elif accion == "merge":
        out = await git_flow.merge_pr(
            repo_path, branch, method=str(body.get("method") or "squash"),
            delete_branch=False if managed else bool(body.get("delete_branch", True)))
    elif accion == "sync-base":
        if managed:
            try:
                from pathlib import Path
                local, remote = await task_workspace._strict_develop_guard(Path(managed["source_repo"]))
                out = {"base": "develop", "ref": local, "remote": remote, "fetched": True}
            except (RuntimeError, OSError) as exc:
                out = {"error": str(exc)}
        else:
            out = await git_flow.sync_base(repo_path)
    elif accion == "restore":
        out = await git_flow.restore_paths(repo_path, branch,
                                           body.get("paths") or [])
    else:
        return web.json_response(
            {"error": f"acción git desconocida: {accion!r}",
             "acciones": ["commit", "push", "pr", "merge", "sync-base",
                          "restore"]}, status=400)
    payload = {"id": conv_id, "branch": branch, "action": accion, **out}
    if out.get("error"):
        logger.info("git action %s falló conv=%s: %s", accion, conv_id[:8],
                    out["error"])
        return web.json_response(payload, status=422)
    if managed and accion == "merge":
        await db.update_conversation_task(conv_id, state="finished", tracking={"enabled": False})
        await db.cancel_pending_conversation_events(conv_id)
    logger.info("git action %s ok conv=%s branch=%s", accion, conv_id[:8], branch)
    return web.json_response(payload)


@_require_auth
async def conversation_set_discord_user(request: web.Request) -> web.Response:
    """Keep explicit unlink available; audience-unverified links are closed."""
    db: Database = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response({"error": "no existe"}, status=404)
    from . import conversation_access
    try:
        await conversation_access.require(db, conv_id, identity.requester(request), "control")
    except PermissionError:
        return web.json_response({"error": "Conversación no disponible"}, status=404)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "el body debe ser un objeto"}, status=400)
    # No supplied source/thread/user ID proves Discord membership or opt-in.
    # Reject before creating a bot thread or recording a misleading new link.
    if (body.get("create_thread") or body.get("discord_user_id") is not None
            or body.get("discord_author")):
        return web.json_response({"error": "No se puede vincular Discord: la audiencia de esta conversación no está verificada.",
            "code": "discord_audience_unverified", "linked": False}, status=409)
    if "discord_user_id" not in body:
        return web.json_response({"error": "discord_user_id requerido; null permite desvincular"}, status=400)
    await db.set_conversation_discord_user(conv_id, discord_user_id=None, clear=True)
    await db.run("UPDATE conversations SET discord_thread_id=NULL WHERE id=?", (conv_id,))
    return web.json_response({"id": conv_id, "discord_user_id": None,
        "discord_author": None, "discord_thread_id": None, "cleared": True})
