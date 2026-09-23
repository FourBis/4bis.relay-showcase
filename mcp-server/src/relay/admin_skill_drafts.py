"""Rutas de borradores de skills y aplicación a transcripciones."""
from __future__ import annotations
import asyncio
import json
import logging
from aiohttp import web
from .app_state import DB_KEY, SKILLS_KEY

logger = logging.getLogger("relay.admin")

async def api_apply_skill_to_transcript(request: web.Request) -> web.Response:
    """POST /admin/api/skills/{name}/apply-to-transcript

    Aplica una skill `when: manual` a un transcript de voz específico.
    Pensado para el botón "Crear issue / historia de usuario" de la
    tab Voz: la skill provee el system prompt, el transcript provee
    el input. El LLM NO toca el repo, NO abre issues en GitHub, NO
    usa tools — devuelve markdown listo para pegar.

    Body:
        transcript_id: str  (requerido; el id del JSONL)
        repo:          str  (opcional; formato owner/name; default "")
        mode:          str  ("issue" | "user_story"; default "issue")

    Returns:
        200 -> {output, model, tokens_in, tokens_out, duration_ms, ...}
        400 -> falta transcript_id / mode inválido
        404 -> skill o transcript desconocidos
        502 -> el LLM falló
        503 -> modelo no disponible
        504 -> timeout

    Decisiones de diseño:
    - Reusamos `run_consult` (no `run_expert`) porque la skill `manual`
      no debe arrastrar el project del transcript. Cero tools, cero
      repo, cero git. Solo la skill como system + el transcript como user.
    - No persistimos la salida como "consult" nueva: el output es un
      artefacto efímero que el usuario decide dónde pegar. Si en el
      futuro aparece el caso de guardar el issue generado, va por
      una tabla aparte (no choca con `consults`, que es por turno).
    - No cacheamos el `content` de la skill en memoria: la leemos
      fresca del FS por si el usuario acaba de aprobarla y el cache
      viejo (TTL 60s) todavía no se invalidó.
    """
    from . import voice as voice_mod
    from . import experts as relay_experts

    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    body = body or {}
    trx_id = (body.get("transcript_id") or "").strip()
    if not trx_id:
        return web.json_response(
            {"error": "transcript_id requerido"}, status=400)
    repo = (body.get("repo") or "").strip()
    mode = (body.get("mode") or "issue").strip()
    if mode not in ("issue", "user_story"):
        return web.json_response(
            {"error": 'mode debe ser "issue" o "user_story"'}, status=400)

    # 1) leer la skill desde disco (cache del lado del FS es barato;
    #    el invalidation lo maneja api_skills_approve / delete).
    from . import skills as skills_mod
    cache = request.app[SKILLS_KEY]
    skill_name = request.match_info["name"]
    skill_md = await asyncio.to_thread(
        skills_mod.read_skill_sync, cache.dir, skill_name)
    if skill_md is None:
        return web.json_response(
            {"error": f"skill {skill_name!r} no existe"}, status=404)

    # 2) leer el transcript
    rec = await voice_mod.read_transcript(trx_id)
    if rec is None:
        return web.json_response(
            {"error": f"transcript {trx_id!r} no existe"}, status=404)
    transcript = (rec.get("transcript") or "").strip()
    if not transcript:
        return web.json_response(
            {"error": "transcript vacío: nada que procesar"}, status=400)

    # 3) armar el user prompt: transcript + repo + mode + metadata útil.
    meta_lines = []
    if rec.get("related_project"):
        meta_lines.append(f"proyecto_relacionado: {rec['related_project']}")
    if rec.get("author"):
        meta_lines.append(f"autor: {rec['author']}")
    if rec.get("ts"):
        meta_lines.append(f"fecha: {rec['ts']}")
    meta = "\n".join(meta_lines)
    user = (
        f"transcript_id: {trx_id}\n"
        f"mode: {mode}\n"
        + (f"repo: {repo}\n" if repo else "repo: (no provisto — si el "
            "transcript menciona un repo, úsalo; si no, pide aclaración)\n")
        + (f"{meta}\n" if meta else "")
        + "\n--- TRANSCRIPT ---\n"
        + transcript
    )

    # 4) correr el LLM sin tools, con la skill como system. system_extra
    #    = content del SKILL.md (frontmatter + cuerpo). El ponytail se
    #    sigue inyectando vía `read_ponytail()` adentro de run_consult.
    try:
        result = await relay_experts.run_consult(
            user=user, system_prompt=skill_md,
            skills_block="",  # la skill YA es system; no duplicar el
                              # índice de skills (ruido inútil).
            model_override="", db=request.app[DB_KEY],
        )
    except relay_experts.ModelUnavailable as e:
        return web.json_response({"error": str(e)}, status=503)
    except asyncio.TimeoutError:
        return web.json_response({"error": "timeout del LLM"}, status=504)
    except Exception as e:  # noqa: BLE001
        logger.exception("apply-skill: %s/%s falló", skill_name, trx_id)
        return web.json_response(
            {"error": f"LLM falló: {type(e).__name__}: {str(e)[:200]}"},
            status=502)

    return web.json_response({
        "transcript_id": trx_id,
        "skill": skill_name,
        "mode": mode,
        "repo": repo,
        "output": result.get("content", ""),
        "model": result.get("model"),
        "tokens_in": result.get("tokens_in"),
        "tokens_out": result.get("tokens_out"),
        "duration_ms": result.get("duration_ms"),
    })

async def api_skill_drafts_list(request: web.Request) -> web.Response:
    """GET /admin/api/skill-drafts?status=&limit= — borradores + count
    de pendientes (para el badge del sidebar)."""
    db = request.app[DB_KEY]
    status = request.query.get("status") or None
    if status and status not in ("pending", "approved", "rejected"):
        return web.json_response(
            {"error": "status debe ser pending|approved|rejected"}, status=400)
    try:
        limit = min(int(request.query.get("limit", "100")), 500)
    except (TypeError, ValueError):
        limit = 100
    drafts = await db.list_skill_drafts(status=status, limit=limit)
    pending = await db.count_skill_drafts_pending()
    return web.json_response({"drafts": drafts, "pending": pending})

async def api_skill_draft_get(request: web.Request) -> web.Response:
    """GET /admin/api/skill-drafts/{id} — detalle con content."""
    db = request.app[DB_KEY]
    try:
        draft_id = int(request.match_info["id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "id inválido"}, status=400)
    draft = await db.get_skill_draft(draft_id)
    if draft is None:
        return web.json_response({"error": "no existe"}, status=404)
    return web.json_response({"draft": draft})

async def api_skill_draft_patch(request: web.Request) -> web.Response:
    """PATCH /admin/api/skill-drafts/{id} — edita name/description/content.

    Solo drafts pending: lo aprobado ya se copió al FS (edita la skill
    instalada) y lo rechazado no tiene sentido editarlo.
    """
    db = request.app[DB_KEY]
    try:
        draft_id = int(request.match_info["id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "id inválido"}, status=400)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    fields = {k: body[k] for k in ("name", "description", "content")
              if isinstance(body.get(k), str)}
    if not fields:
        return web.json_response(
            {"error": "nada para editar (name/description/content)"},
            status=400)
    draft = await db.get_skill_draft(draft_id)
    if draft is None:
        return web.json_response({"error": "no existe"}, status=404)
    if draft["status"] != "pending":
        return web.json_response(
            {"error": f"draft {draft['status']}: solo se editan los pending"},
            status=409)
    await db.update_skill_draft(draft_id, **fields)
    return web.json_response({"draft": await db.get_skill_draft(draft_id)})

async def api_skill_draft_approve(request: web.Request) -> web.Response:
    """POST /admin/api/skill-drafts/{id}/approve — LA aprobación.

    Escribe <skills_dir>/<name>/SKILL.md y marca approved. Si ya existe
    una skill con ese nombre devuelve 409 {needs_overwrite: true}; la UI
    re-postea con {"overwrite": true} tras confirmar.
    """
    from . import skills as skills_mod
    db = request.app[DB_KEY]
    cache = request.app[SKILLS_KEY]
    try:
        draft_id = int(request.match_info["id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "id inválido"}, status=400)
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    overwrite = bool((body or {}).get("overwrite"))
    # `manual=True` aprueba el draft como skill on-demand (frontmatter
    # `when: manual`). NO se inyecta automáticamente en el system prompt;
    # queda disponible vía el botón "Crear issue" en la tab Voz (y
    # futuras integraciones). YAGNI romper este flag por si surge un
    # `when: trigger` más adelante; default False conserva el
    # comportamiento histórico. (2026-07-17, iter 8.5 — refinamiento
    # del draft auto-generado por el compactador.)
    manual = bool((body or {}).get("manual"))
    frontmatter_extra = "when: manual" if manual else ""

    draft = await db.get_skill_draft(draft_id)
    if draft is None:
        return web.json_response({"error": "no existe"}, status=404)
    if draft["status"] != "pending":
        return web.json_response(
            {"error": f"draft ya {draft['status']}"}, status=409)
    try:
        path = await asyncio.to_thread(
            skills_mod.write_skill_sync, draft["name"],
            draft["description"], draft["content"], cache.dir,
            overwrite=overwrite,
            frontmatter_extra=frontmatter_extra)
    except FileExistsError as e:
        return web.json_response(
            {"error": f"ya existe una skill en {e}",
             "needs_overwrite": True}, status=409)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except OSError as e:
        return web.json_response(
            {"error": f"no pude escribir la skill: {e!r}"}, status=500)
    await db.set_skill_draft_status(draft_id, "approved",
                                    approved_path=str(path))
    cache.invalidate()
    logger.info("skill draft #%d aprobado → %s", draft_id, path)
    return web.json_response({
        "approved": True, "id": draft_id, "path": str(path),
        "name": skills_mod.sanitize_skill_name(draft["name"]),
    })

async def api_skill_draft_reject(request: web.Request) -> web.Response:
    """POST /admin/api/skill-drafts/{id}/reject — marca rejected.

    La fila queda (auditoría de qué destiló el compactador); para
    purgarla del todo está el DELETE.
    """
    db = request.app[DB_KEY]
    try:
        draft_id = int(request.match_info["id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "id inválido"}, status=400)
    draft = await db.get_skill_draft(draft_id)
    if draft is None:
        return web.json_response({"error": "no existe"}, status=404)
    if draft["status"] != "pending":
        return web.json_response(
            {"error": f"draft ya {draft['status']}"}, status=409)
    await db.set_skill_draft_status(draft_id, "rejected")
    return web.json_response({"rejected": True, "id": draft_id})

async def api_skill_draft_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/skill-drafts/{id} — purga la fila.

    NO toca el FS: si el draft se aprobó, la skill instalada se borra
    aparte con DELETE /admin/api/skills/{name}.
    """
    db = request.app[DB_KEY]
    try:
        draft_id = int(request.match_info["id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "id inválido"}, status=400)
    if not await db.delete_skill_draft(draft_id):
        return web.json_response({"error": "no existe"}, status=404)
    return web.json_response({"deleted": True, "id": draft_id})
