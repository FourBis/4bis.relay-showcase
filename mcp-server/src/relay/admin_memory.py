"""Rutas de facts, memoria y diff de proyectos."""
from __future__ import annotations
import asyncio
import logging
from aiohttp import web
from . import memory
from .app_state import DB_KEY

logger = logging.getLogger("relay.admin")

async def api_facts_list(request: web.Request) -> web.Response:
    """GET /admin/api/conversations/facts?project=&limit= — facts del proyecto.

    Lista los hechos atómicos destilados al cerrar conversaciones
    (ADR-026). Append-only — no hay DELETE desde la UI; si quieres
    sacar algo, lo haces a mano en SQLite.

    Sub-ola 2.4 — para el panel de hechos del proyecto en la Admin UI.
    """
    db = request.app[DB_KEY]
    project_slug = request.query.get("project")
    if not project_slug:
        return web.json_response(
            {"error": "project requerido"}, status=400)
    try:
        limit = min(int(request.query.get("limit", "100")), 500)
    except (TypeError, ValueError):
        limit = 100
    status = (request.query.get("status") or "").strip() or None
    if status is not None and status not in db.FACT_STATES:
        return web.json_response(
            {"error": f"status debe ser uno de {list(db.FACT_STATES)}"},
            status=400)
    facts = await db.list_facts(project_slug, limit=limit, status=status)
    # El contador de pendientes viaja SIEMPRE, mires el filtro que mires:
    # una cola de aprobación que no se ve es una cola que no se atiende
    # (los 13 borradores de skills, el más viejo de hace 25 días).
    pendientes = len(await db.list_facts(
        project_slug, limit=500, status="pending"))
    return web.json_response({
        "project": project_slug, "facts": facts,
        "pendientes": pendientes,
    })

async def api_project_git_diff(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug}/git-diff — diff sin procesar.

    Devuelve el JSON de `_capture_git_diff_sync` con cap defensivo:
        {ok, status, diff, sha, branch, truncated?, full_size?, stderr}

    Cambio 2026-07-08 (bug fix Sub-ola 2.7): antes el response mandaba
    el diff ENTERO (hasta 20KB) al cliente y la UI lo metía en un <pre>.
    En repos con muchos archivos modificados eso colgaba el browser
    y acumulaba memoria por cada apertura del modal. Ahora capamos
    el response del endpoint a un tamaño razonable (default 64KB
    total del JSON, override con `?max_kb=N`) y siempre devolvemos
    `full_size` para que la UI sepa cuánto se cortó.

    - `ok: false, status: "not_git_repo"` → repo no es git
    - `truncated: true` → diff cortado por el cap interno del experto
      (DIFF_MAX_BYTES=20KB) o por el cap del endpoint (?max_kb=N)
    - `full_size`: tamaño real del diff (en chars), para UI info
    - `stderr`: warnings de git separados del diff (no se renderizan
      por default; quedan en la respuesta para diagnóstico si ?debug=1)
    """
    db = request.app[DB_KEY]
    from . import experts as relay_experts
    slug = request.match_info["slug"]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    info = await asyncio.to_thread(
        relay_experts._capture_git_diff_sync, project["repo_path"])
    full_diff = info.get("diff", "") or ""
    full_size = len(full_diff)
    out = {
        "slug": slug,
        "ok": bool(info.get("ok")),
        "status": info.get("status", ""),
        "diff": full_diff,
        "sha": info.get("sha", ""),
        "branch": info.get("branch", ""),
        "full_size": full_size,
    }
    # cap defensivo a nivel de endpoint (default 64KB, ajustable).
    # Evita que un diff enorme cuelgue el browser. El experto ya capa
    # a DIFF_MAX_BYTES internamente; este cap es la segunda barrera.
    try:
        max_kb = int(request.query.get("max_kb", "64"))
    except (TypeError, ValueError):
        max_kb = 64
    max_chars = max_kb * 1024
    if len(out["diff"]) > max_chars:
        # truncar en frontera de línea para no cortar mid-line
        cut = out["diff"][:max_chars]
        last_nl = cut.rfind("\n")
        if last_nl > 0:
            cut = cut[:last_nl]
        out["diff"] = cut
        out["truncated"] = True
    elif full_size > relay_experts.DIFF_MAX_BYTES:
        out["truncated"] = True
    # stderr solo si se pide explícitamente (sirve para debug)
    if request.query.get("debug") == "1":
        out["stderr"] = info.get("stderr", "")
    return web.json_response(out)

async def api_memories_search(request: web.Request) -> web.Response:
    """GET /admin/api/conversations/memories?project=&q=&limit=.

    Búsqueda FTS5 sobre los resúmenes de conversaciones cerradas
    (ADR-027). Scoped a un proyecto. Sin query, devuelve los
    resúmenes más recientes del proyecto.

    Devuelve además `fts_available` para que la UI sepa si mostrar
    el input de búsqueda o un warning de "FTS5 no compilado".

    Sub-ola 2.5 — para la búsqueda en memoria de la Admin UI.
    """
    db = request.app[DB_KEY]
    project_slug = request.query.get("project")
    if not project_slug:
        return web.json_response(
            {"error": "project requerido"}, status=400)
    q = request.query.get("q") or ""
    try:
        limit = min(int(request.query.get("limit", "5")), 50)
    except (TypeError, ValueError):
        limit = 5
    hits = await db.search_memories(project_slug, q, limit=limit)
    return web.json_response({
        "project": project_slug,
        "query": q,
        "hits": hits,
        "fts_available": bool(db._fts_available),
    })

async def api_fact_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/facts/{id} — curación de facts (2026-07-12).

    La tabla sigue append-only para el compactador; esto es el botón
    de "sacar un fact viejo o mal destilado" sin abrir SQLite a mano.
    """
    db = request.app[DB_KEY]
    try:
        fact_id = int(request.match_info["id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "id inválido"}, status=400)
    if not await db.delete_fact(fact_id):
        return web.json_response({"error": "no existe"}, status=404)
    return web.json_response({"deleted": True, "id": fact_id})

async def api_fact_status(request: web.Request) -> web.Response:
    """PATCH /admin/api/facts/{id} — aprobar o rechazar un hecho.

    Aprobación estricta (2026-08-21, pedido del usuario): el compactador
    escribe `pending` y el hecho NO entra al prompt del experto hasta
    que pasa por acá. `rejected` se conserva en la tabla en vez de
    borrarse: sirve para ver qué destila mal el compactador. Para que
    desaparezca del todo está el DELETE.
    """
    db = request.app[DB_KEY]
    try:
        fact_id = int(request.match_info["id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "id inválido"}, status=400)
    body = await request.json()
    status = (body.get("status") or "").strip()
    if status not in db.FACT_STATES:
        return web.json_response(
            {"error": f"status debe ser uno de {list(db.FACT_STATES)}"},
            status=400)
    if not await db.set_fact_status(fact_id, status):
        return web.json_response({"error": "no existe"}, status=404)
    return web.json_response({"id": fact_id, "status": status})

async def api_fact_create(request: web.Request) -> web.Response:
    """POST /admin/api/conversations/facts — alta manual de un hecho.

    Nace `approved`: lo escribió una persona, no hay a quién pedirle
    aprobación. Es la vía para meter un dato que el compactador nunca va
    a destilar solo (una credencial de demo, un puerto, una convención
    del equipo).
    """
    db = request.app[DB_KEY]
    body = await request.json()
    project_slug = (body.get("project") or "").strip()
    fact = (body.get("fact") or "").strip()
    if not project_slug:
        return web.json_response({"error": "project requerido"}, status=400)
    if not fact:
        return web.json_response({"error": "fact vacío"}, status=400)
    if len(fact) > 2000:
        return web.json_response(
            {"error": "fact demasiado largo (máx 2000 chars)"}, status=400)
    if await db.get_project(project_slug) is None:
        return web.json_response(
            {"error": f"proyecto {project_slug!r} no existe"}, status=404)
    await db.add_facts(project_slug, [fact], status="approved")
    return web.json_response({"created": True, "project": project_slug})

async def api_conversation_extract_facts(request: web.Request) -> web.Response:
    """POST /admin/api/conversations/{conv_id}/extract-facts.

    Destila hechos del hilo SIN cerrarlo ni compactarlo. Existe porque
    los dos caminos que ya había hacen de más: `/close` cierra la
    conversación y `/compact` además **recorta el historial**, o sea que
    pedir "sacá los hechos de esto" costaba perder el detalle de los
    turnos viejos. Acá el hilo queda exactamente como estaba.

    Los hechos entran como `pending`, igual que los del compactador: que
    lo haya disparado un humano no significa que el LLM haya destilado
    bien.
    """
    db = request.app[DB_KEY]
    conv_id = request.match_info["conv_id"]
    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response({"error": "no existe"}, status=404)
    messages_json = conv.get("messages_json") or ""
    if not messages_json:
        return web.json_response(
            {"error": "la conversación no tiene turnos todavía"}, status=400)
    # Los vigentes van al compactador para que no re-emita duplicados ni
    # se pise con lo que ya está esperando revisión (por eso sin filtro
    # de status: un `pending` duplicado sigue siendo un duplicado).
    existing = await db.list_facts(conv["project_slug"], limit=100)
    try:
        result = await memory.compact_conversation(
            messages_json, existing_facts=existing)
    except Exception as e:  # noqa: BLE001 — el compactador es un LLM
        logger.warning("extract-facts conv=%s falló (%r)", conv_id[:8], e)
        return web.json_response(
            {"error": f"el compactador falló: {type(e).__name__}"}, status=502)
    if result is None:
        return web.json_response(
            {"error": "no se pudo destilar nada del hilo"}, status=422)
    n = await db.add_facts(
        conv["project_slug"], result.facts, source_conversation=conv_id)
    # El summary se descarta a propósito: esto NO es compactar. Pisar el
    # resumen del hilo desde un botón que dice "extraer hechos" sería
    # hacer algo que nadie pidió.
    return web.json_response({
        "created": n, "pending": n,
        "project": conv["project_slug"],
        "facts": result.facts,
    })

async def api_memory_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/conversations/{conv_id}/memory — saca una
    memoria del retrieval (FTS5 + summary). El historial de la
    conversación queda intacto; solo deja de aparecer en `memoria`
    y en el campo memory de /experts/run.
    """
    db = request.app[DB_KEY]
    conv_id = request.match_info["conv_id"]
    if not await db.delete_memory(conv_id):
        return web.json_response(
            {"error": "no existe o no tiene summary"}, status=404)
    return web.json_response({"deleted": True, "conversation_id": conv_id})
