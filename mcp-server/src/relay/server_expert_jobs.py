"""Server domain handlers extracted from the composition entrypoint."""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

import httpx
from aiohttp import web

from .server_common import _bg_tasks, logger
from . import attachments as attachments_mod
from . import coordination
from . import experts
from . import finalization
from . import server_expert_stage_report as _stage_report
from . import logctx
from . import mcp_pool
from . import notify
from . import progress
from .secret_redaction import redact_data, redact_text
from .db import Database
from .mcp_pool import McpPool
from .notify import NotifyClient
from .server_questions import _ATT_ID_RE
from .server_night import _grafo_en_vez_de_proponer
# ---------- expertos (ADR-012 + ADR-024: async, el resultado va por /notify) ----------

# Tasks fire-and-forget (compactación, sweeper spawns): guardamos la
# referencia para que el GC no las mate a mitad de camino.
_bg_tasks: set[asyncio.Task] = set()


def _spawn_bg(coro, *, hold_workspace: bool = True) -> asyncio.Task:
    task = asyncio.create_task(coro)
    if hold_workspace:
        coordination.hold_current(task)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


# Nota (2026-08-10): acá vivía `_maybe_plan_large_prompt` (Opción 4), un
# segundo planificador que detectaba prompts grandes con un regex y
# pagaba una corrida entera de night.TaskGenerator para proponer una
# descomposición. Quedó absorbido por el planificador del runner por
# etapas (experts._run_planner + la señal `DEMASIADO_GRANDE:`), que ya
# corre en cada request: el mismo comportamiento sin el LLM extra ni el
# acoplamiento del chat con la maquinaria del modo nocturno.


async def _request_bot_create_thread(
    *, discord_user_id: str, conversation_id: str, project_slug: str,
) -> tuple[Optional[str], Optional[str]]:
    """Iter 10.4: pide al bot C# crear un DM thread para la conversación.

    Llama POST /threads en el bot. Devuelve (thread_id, None) si ok,
    o (None, mensaje_error) si falla — nunca crashea el handler caller.
    """
    bot_notify_url = os.environ.get(
        "BOT_NOTIFY_URL", "http://127.0.0.1:8297/notify")
    base = bot_notify_url.rstrip("/")
    if base.endswith("/notify"):
        base = base[:-len("/notify")]
    url = f"{base}/threads"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, json={
                "discord_user_id": discord_user_id,
                "conversation_id": conversation_id,
                "project_slug": project_slug,
            })
            r.raise_for_status()
            data = r.json()
            tid = data.get("discord_thread_id") or data.get("channel_id") or ""
            if not tid:
                return None, f"bot respondió sin discord_thread_id: {data}"
            return str(tid), None
    except httpx.HTTPError as e:
        logger.warning(
            "bot create-thread falló conv=%s discord_user=%s err=%r",
            conversation_id, discord_user_id, e)
        return None, f"no se pudo crear thread: {e}"
    except Exception as e:
        logger.warning(
            "bot create-thread excepción inesperada conv=%s err=%r",
            conversation_id, e)
        return None, f"error inesperado al crear thread: {e}"


def _render_pregunta(row: dict) -> str:
    """Fila de `expert_questions` → bloque markdown para el humano.

    Se pinta como una pregunta con opciones numeradas para que se pueda
    contestar de la forma más barata posible: escribiendo en el chat. El
    `id` va visible porque es lo que necesita quien quiera responder por
    API en vez de por texto.
    """
    try:
        q = json.loads(row.get("question_json") or "{}")
    except json.JSONDecodeError:
        q = {}
    titulo = (q.get("title") or "").strip() or "El experto necesita una decisión"
    icono = "📦" if row.get("kind") == "install" else "❓"
    lineas = [f"{icono} **{titulo}**"]
    detalle = (q.get("detail") or "").strip()
    if detalle:
        lineas += ["", detalle]
    opciones = q.get("options") or []
    if opciones:
        lineas += [""] + [f"- **{o.get('label', '')}**" for o in opciones]
        lineas += ["", "_Respondé en el chat con la opción que prefieras._"]
    else:
        lineas += ["", "_Respondé en el chat y el experto retoma desde ahí._"]
    lineas += ["", f"`{row.get('id', '')}`"]
    return "\n".join(lineas)


#: Qué pasó con el TRABAJO, que no es lo mismo que qué pasó con la
#: ejecución. `chats.status` ya dice si el run terminó o reventó; esto
#: dice si lo que salió sirve. Contar "éxito" como "runs sin error"
#: mezclaba las dos cosas: un run que termina prolijo y que el
#: verificador marcó `needs_more` contaba igual que uno aprobado.
#:
#: `sin_verificar` es su propia categoría y no un `aprobado` optimista:
#: separar "el trabajo estaba mal" de "el verificador no corrió" es
#: justo lo que permite saber si una degradación del supervisor está
#: inflando las métricas.
_RESULTADO_POR_VERDICT = {
    "complete": "aprobado",
    "needs_more": "pendiente",
    "needs_human": "intervencion",
    "off_plan": "desviado",
}


def _resultado_del_trabajo(result: dict) -> str:
    """Veredicto del verificador → vocabulario de resultado."""
    if (result.get("stage_errors") or {}).get("verifier"):
        return "sin_verificar"
    verdict = (result.get("verifier_verdict") or "").strip()
    if not verdict:
        # Sin veredicto y sin error: la etapa no corrió (opt-out del
        # proyecto, o cortó antes de llegar).
        return "sin_verificar"
    return _RESULTADO_POR_VERDICT.get(verdict, "sin_verificar")


def _stages_json(result: dict) -> Optional[str]:
    """Serializa etapas conservando el vocabulario público del resultado."""
    return _stage_report.stages_json(result, outcome=_resultado_del_trabajo)


async def _bind_generated_images(db, chat_id, conv_id, result):
    """Link only captures produced by this server run, never IDs quoted in text."""
    from .user_accounts import current_actor
    context = current_actor.get()
    images = result.get("image_artifacts")
    if not conv_id or not isinstance(images, dict) or not images:
        return []
    chat = await db.get_chat(chat_id)
    conv = await db.get_conversation(conv_id)
    if (not context or context[0].db is not db or not context[1] or not chat or not conv
            or chat.get("conversation_id") != conv_id or chat.get("project_slug") != conv.get("project_slug")
            or str(chat.get("requested_by") or "").lower() != context[1]):
        logger.warning("Capturas sin vínculo verificado con el run %s", chat_id)
        return []
    scope = attachments_mod.scope_dir(conv_id).resolve()
    storage = attachments_mod.attachments_dir().resolve()
    linked = []
    for aid, name in images.items():
        blob = attachments_mod.resolve(aid) if isinstance(aid, str) else None
        if (blob is None or not isinstance(name, str) or name != blob.name or blob.is_symlink()
                or blob.resolve().parent != storage):
            continue
        try:
            worker = asyncio.create_task(asyncio.to_thread(
                attachments_mod.materializar, conv_id, blob))
            try:
                path = await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Cancelar el await no detiene el IO. Cerrar después de que
                # termine su worker, sin soltar una escritura todavía viva.
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not worker.cancelled():
                    worker.exception()  # recoger fallo; se conserva la cancelación original.
                raise
            if path.is_file() and not path.is_symlink() and path.resolve().parent == scope:
                linked.append(aid)
            else:
                logger.warning("Captura del run %s sin archivo en su conversación", chat_id)
        except OSError:
            logger.warning("No se pudo vincular una captura del run %s", chat_id)
    return linked


def _redact_result_logs(result):
    """Sanitize new display/log fields, preserving inherited durable history.

    New console tool returns are redacted at their producer before they enter
    messages_json. Historical model context/bitacora are not a retroactive
    repair target; authorized readers receive sanitized copies separately.
    """
    retained = {key: result[key] for key in ("messages_json", "bitacora_json") if key in result}
    return {**redact_data({key: value for key, value in result.items() if key not in retained}), **retained}


def _preserve_completed_result(checkpoint, *, result, chat_id, conv_id, history,
                               user, target, source, author, model, status, error, started):
    """Publicar sin await los datos de cierre antes de cualquier post-proceso."""
    if checkpoint is None or not result:
        return
    # The rescue checkpoint is itself a future persistence source. Keep its
    # log/history copy sanitized even when later post-processing is cancelled.
    result = _redact_result_logs(result)
    error = redact_text(error)
    raw_images = result.get("image_artifacts")
    images = {aid: name for aid, name in (raw_images if isinstance(raw_images, dict) else {}).items()
              if isinstance(aid, str) and _ATT_ID_RE.fullmatch(aid)
              and isinstance(name, str) and "/" not in name and "\\" not in name}
    content = result.get("content") or ""
    if images and not all(f"/attachments/{aid}" in content for aid in images):
        content += "\n\n" + attachments_mod.generated_markdown(images)
    duration = result.get("duration_ms")
    if duration is None:
        duration = int((time.monotonic() - started) * 1000)
    history_json = result.get("messages_json")
    bitacora_json = result.get("bitacora_json")
    requests = result.get("requests")
    phase = result.get("phase_at_end")
    metadata = {
        "image_artifacts": images,
        "bitacora_json": bitacora_json if isinstance(bitacora_json, str) else "",
        "requests": requests if type(requests) is int and requests >= 0 else None,
        "executor_phase_at_end": phase if isinstance(phase, str) else None,
    }
    for steer in result.get("steer_texts") or []:
        if isinstance(steer, str):
            user += f"\n\nCorrección en vivo: {steer}"
    artifact = dict(target=target, chat_id=chat_id, user=user, content=content,
        source=source, author=author, model=result.get("model") or model,
        status=status, duration_ms=duration, error=error,
        events=result.get("progress_events"),
        messages_json=history_json if isinstance(history_json, str) else "",
        result_metadata=metadata)
    try:
        events_json = json.dumps(result.get("progress_events") or [])
        tools_json = json.dumps(result["tool_meter"]) if result.get("tool_meter") else None
        stages_json = _stages_json(result)
    except (TypeError, ValueError):
        events_json, tools_json, stages_json = "[]", None, None
    artifact["events"] = json.loads(events_json)
    trimmed = result.get("trimmed_turns")
    fields = dict(tokens_in=result.get("tokens_in"), tokens_out=result.get("tokens_out"),
        cache_read_tokens=result.get("cache_read_tokens"), tool_calls=result.get("tool_calls"),
        error=error, phase_at_end=result.get("phase_at_end"), last_tool=result.get("last_tool"),
        duration_ms=duration, model=result.get("model") or model or None,
        progress_events=events_json,
        trimmed_turns=trimmed if type(trimmed) is int and trimmed >= 0 else 0,
        tool_bytes=tools_json, stages_json=stages_json)
    checkpoint.update(artifact=artifact, fields=fields, conversation_id=conv_id,
                      history_before=history)


@finalization.supervise
async def _run_expert_bg(
    *, db: Database, notify: NotifyClient, running: dict,
    progress: dict,
    chat_id: str, project: dict, user: str, skills_block: str,
    system_extra: str, model_override: str, target: str,
    source: str, author: str, conversation: Optional[dict],
    stage_models: Optional[dict] = None,
    mcp_with: Optional[list] = None, mcp_pool: Optional[McpPool] = None,
    images: Optional[list] = None,
    app: Optional[web.Application] = None,
    _close_checkpoint: Optional[dict] = None,
    before_executor=None, deadline_pedido=0.0,
) -> None:
    """Ciclo de vida completo de un run de experto en background.

    ADR-024: el HTTP ya respondió 202; acá corremos, persistimos
    (.md + JSONL + finish_chat + historial de la conversación).
    El resultado se consulta en Relay; no se publica a una audiencia
    externa no verificable a partir de ids del caller o del proyecto.
    """
    # Correlación (2026-07-25): de acá en más, TODA línea que se loguee
    # en esta task —y en las que cree, watchdog y callbacks incluidos—
    # sale con el chat_id. Sin esto, un run muerto había que
    # reconstruirlo a mano desde la DB. Va primero: si algo revienta en
    # el setup, queremos que ese error también quede atribuido.
    logctx.bind(chat_id, project.get("slug", ""))
    if isinstance(source, str) and source.strip().lower() == "discord":
        # Defensa para callers directos/antiguos además del ingreso y cola.
        # supervise cierra la fila durable sin iniciar modelo o consumo.
        raise RuntimeError("Puente Discord sin audiencia verificable; continúa desde tu cuenta en Relay.")

    conv_id = conversation["id"] if conversation else None
    private = bool((project.get("defaults_json") or {}).get("mcp_private"))
    history_json = (conversation.get("messages_json") or "") if conversation else ""
    # Lo que el experto verificó en turnos anteriores (2026-08-26). Va
    # aparte del historial porque sobrevive a cosas distintas: un corte
    # por `off_plan` le saca al historial las tool calls enteras, y la
    # bitácora viaja como instructions, que no se persisten.
    bitacora_json = (conversation.get("bitacora_json") or "") if conversation else ""

    # Fase 1: progress store + callback. Si run_expert no acepta
    # on_progress (versión vieja), el callback es noop — best-effort.
    progress_cb = experts.make_progress_callback(
        # Ni source='discord' ni ids del payload prueban la audiencia del
        # resultado, que puede incluir memoria privada. Pasos sólo en Relay.
        store=progress, notify=None,
        chat_id=chat_id, target=target,
        model=model_override or (project.get("defaults_json") or {}).get("model") or "",
    )
    # Canales vivos con el run (2026-07-25). `steer` es la MISMA lista que
    # muta POST /experts/steer (make_progress_callback ya dejó el
    # RunProgress en el store bajo este chat_id). `rescue` es por dónde
    # sale el historial cuando el run no puede devolver un dict — o sea,
    # cuando el humano cancela.
    steer_queue: list[str] = progress[chat_id].steer
    rescue: dict = {}

    error: str | None = None
    result: dict = {}
    status = "ok"
    t0 = time.monotonic()

    try:
        # Iter 11 (por etapas): el experto corre con planificador +
        # ejecutor + verificador + documentador. El opt-out por proyecto
        # es defaults_json.three_stage=false (nombre heredado). Si el
        # planificador juzga que el pedido es demasiado grande, el
        # wrapper corta ahí y devuelve la descomposición con
        # phase_at_end="planned", sin ejecutar nada. Los tests
        # existentes siguen llamando run_expert directo: esta ruta es
        # solo la del chat.
        result = await experts.run_expert_staged(
            project, user, skills_block=skills_block,
            system_extra=system_extra, model_override=model_override,
            stage_models=stage_models or {},
            db=db, message_history_json=history_json,
            bitacora_json=bitacora_json,
            on_progress=progress_cb,
            steer=steer_queue, rescue=rescue,
            mcp_with=mcp_with, mcp_pool=mcp_pool,
            images=images,
            # 2026-08-16: `ask_human` necesita saber a qué chat y a qué
            # hilo pertenece la pregunta que registra.
            chat_id=chat_id, conversation_id=conv_id or "",
            **({"before_executor": before_executor, "deadline_pedido": deadline_pedido,
                "three_stage": False} if before_executor is not None else {}),
        )
        if before_executor is not None and result.get("feedback_skipped") is True:
            # The trusted gate already cancelled the exact event/chat in SQL.
            # No model result exists to export, reconcile or classify uncertain.
            status, error = "cancelled", result.get("feedback_skip_reason") or "Feedback omitido antes del modelo"
            return False
        _preserve_completed_result(_close_checkpoint, result=result, chat_id=chat_id,
            conv_id=conv_id, history=history_json, user=user, target=target,
            source=source, author=author, model=model_override, status=status,
            error=error, started=t0)
        if result.get("phase_at_end") == "planned" and not private:
            result = await _grafo_en_vez_de_proponer(
                app, db, project, user, conv_id, result)
    except experts.ModelUnavailable as e:
        result = {**result, **rescue, "phase_at_end": "error"}
        error, status = str(e), "error"
    except asyncio.TimeoutError:
        result = {**result, **rescue, "phase_at_end": "timeout"}
        error, status = "timeout total del experto", "error"
    except asyncio.CancelledError:
        admission = getattr(before_executor, "admission", {}) if before_executor is not None else {}
        if (before_executor is not None and not getattr(before_executor, "started", False)
                and not admission.get("started") and not admission.get("uncertain")):
            if await before_executor.cancel_before_start():
                # Only preparation ran: do not merge rescue/history or export
                # a synthetic executor response. SQL cancellation is durable
                # before supervise marks progress finished.
                status, error = "cancelled", "Feedback cancelado antes de su admisión al ejecutor"
                return {"feedback_preparation_cancelled": True}
        # Cancelar usa el mismo cierre durable que éxito/error; incluso si
        # todavía no había historial, la fila y los artefactos quedan cerrados.
        status = "cancelled"
        result = {**result, **rescue, "content": rescue.get("content") or result.get("content") or "run cancelado", "phase_at_end": "cancelled"}
    except Exception as e:
        logger.exception("experto %s falló", target)
        result = {**result, **rescue, "phase_at_end": "error"}
        error, status = f"{type(e).__name__}: {e}", "error"
    finally:
        # Estado post-mortem para que /status siga respondiendo hasta que el
        # sweeper lo limpie. OJO: `finished` NO se prende acá — se prende
        # abajo, DESPUÉS de persistir el historial (ver el comentario).
        rp = progress.get(chat_id)
        if rp is not None:
            rp.closing = True
            if status == "ok":
                rp.phase = result.get("phase_at_end", "done")
            else:
                rp.phase = status  # "error" o "timeout"
                rp.error = error
            lt = result.get("last_tool")
            if lt is not None:
                rp.last_tool = lt
            ti, to = result.get("tokens_in"), result.get("tokens_out")
            if ti is not None:
                rp.tokens_in = ti
            if to is not None:
                rp.tokens_out = to

    # Redact before history/artifact/event writes. Preserve the original
    # history_json CAS token: it identifies the version we started from.
    result = _redact_result_logs(result)
    error = redact_text(error)
    admission = getattr(before_executor, "admission", {}) if before_executor is not None else {}
    if (before_executor is not None and not getattr(before_executor, "started", False)
            and not admission.get("started") and not admission.get("uncertain")
            and status == "error"):
        # Preparation itself can reject a freshly revoked account before the
        # model gate. Recheck without spending an iteration; preserve genuine
        # model/preparation errors when policy is still eligible.
        admission = await before_executor(check_only=True)
        if not admission["execute"]:
            rp = progress.get(chat_id)
            if rp is not None:
                rp.phase, rp.error = "cancelled", admission.get("reason", "")
            return False
    _preserve_completed_result(_close_checkpoint, result=result, chat_id=chat_id,
        conv_id=conv_id, history=history_json, user=user, target=target,
        source=source, author=author, model=model_override, status=status,
        error=error, started=t0)
    await _bind_generated_images(db, chat_id, conv_id, result)

    # ADR-025: persistir el historial de la conversación. Va ANTES de
    # prender `finished` (bug 2026-07-27): la UI poll-ea /experts/status y
    # en cuanto ve finished=True hace GET .../messages. Con el flag prendido
    # primero, ese GET devolvía el historial VIEJO — las burbujas del turno
    # (prompt + respuesta + tool calls) no aparecían hasta recargar la
    # página, que era justo cuando el historial ya estaba guardado.
    if conv_id and result.get("messages_json"):
        try:
            await db.save_conversation_messages(
                conv_id, result["messages_json"], expected=history_json)
        except Exception as e:
            status, error = "error", f"No se pudo guardar el historial: {e}"
            logger.exception("no pude persistir el historial de conv=%s",
                             conv_id[:8])
    # La bitácora se guarda SIN el gate de `status == "ok"`, a propósito:
    # el turno que más la necesita es el que se cortó, y ese no llega acá
    # como "ok". Es lo único que le queda al **continuá** cuando el
    # historial vino recortado.
    if conv_id and result.get("bitacora_json"):
        try:
            await db.save_conversation_bitacora(
                conv_id, result["bitacora_json"])
        except Exception:
            logger.exception("no pude persistir la bitácora de conv=%s",
                             conv_id[:8])
    duration_ms = result.get("duration_ms")
    if duration_ms is None:
        duration_ms = int((time.monotonic() - t0) * 1000)
    content = result.get("content", "")
    generated = result.get("image_artifacts") or {}
    if generated and not all(f"/attachments/{aid}" in content for aid in generated):
        content += "\n\n" + attachments_mod.generated_markdown(generated)
    # 2026-08-16: si el experto dejó una pregunta abierta, va al final de
    # la respuesta. Que se vea en el MISMO mensaje es lo que la hace
    # accionable sin UI nueva: el humano lee la pregunta y contesta en el
    # chat como contestaría cualquier otra cosa. Los endpoints
    # /questions/{id}/answer existen para que la UI ponga botones encima,
    # pero el flujo funciona sin ellos.
    preguntas = []
    if status == "ok":
        try:
            preguntas = await db.list_expert_questions(
                chat_id=chat_id, only_open=True)
        except Exception as e:  # noqa: BLE001 — preguntar no rompe el run
            logger.warning("no pude leer las preguntas de %s: %r", chat_id, e)
    if preguntas:
        content = f"{content.rstrip()}\n\n{_render_pregunta(preguntas[0])}"
    # Medidor de contexto (2026-07-22): se calcula del historial que
    # acabamos de producir y se ANEXA al texto de la respuesta (no al
    # historial: el LLM no tiene que leer esto). Va antes de persistir
    # para que el .md y Discord vean lo mismo.
    ctx = await experts.context_usage_db(
        db, result.get("messages_json") or "")
    if ctx and content:
        content += experts.format_context_note(ctx)
    # Las correcciones en vivo (steer) son parte del pedido: sin esto el
    # .md archivado queda incoherente — el prompt dice "lee los 3
    # primeros archivos" y la respuesta contesta otra cosa, que es lo que
    # el humano pidió a mitad del run.
    for _s in result.get("steer_texts") or []:
        user += f"\n\n🧭 corrección en vivo: {_s}"
    artifact = dict(
        target=target, chat_id=chat_id, user=user, content=content,
        source=source, author=author, model=result.get("model", model_override),
        status=status, duration_ms=duration_ms, error=error,
        # Bitácora compacta al .md: `result["progress_events"]` ya es la
        # lista (la misma que se persiste a chats.progress_events). Sin
        # esto, el .md solo tiene pedido + respuesta — ver el bug de los
        # 790.142 tokens / 61 tool calls / .md de 1 KB.
        events=result.get("progress_events"),
        messages_json=result.get("messages_json") or "",
        result_metadata=(_close_checkpoint or {}).get("artifact", {}).get("result_metadata"),
    )
    md_path = await finalization.finish(
        db, chat_id, status=status, artifact=artifact, checkpoint=_close_checkpoint,
        tokens_in=result.get("tokens_in"), tokens_out=result.get("tokens_out"),
        cache_read_tokens=result.get("cache_read_tokens"),
        tool_calls=result.get("tool_calls"), error=error,
        phase_at_end=result.get("phase_at_end"),
        last_tool=result.get("last_tool"),
        # Bug fix 2026-07-20: duration_ms/model se calculaban acá arriba
        # y se escribían al .md pero NO a la fila de chats (quedaba NULL).
        duration_ms=duration_ms,
        model=result.get("model") or model_override or None,
        # Sprint 1: timeline de eventos + context trim
        progress_events=json.dumps(result.get("progress_events") or []),
        trimmed_turns=result.get("trimmed_turns", 0),
        # 2026-07-26: peso de los tool results, para calibrar los caps.
        tool_bytes=(json.dumps(result["tool_meter"])
                    if result.get("tool_meter") else None),
        # 2026-08-14: etapas del runner (iter 11). Antes esto vivía solo
        # en el dict en memoria: al terminar el run se perdía el plan, el
        # veredicto y qué modelo corrió cada etapa. Con las etapas
        # repartidas entre proveedores (ejecutor pagado, auxiliares en
        # los endpoints gratis de NVIDIA) eso dejó de ser cosmético: sin
        # esto no hay forma de saber si un run se aprobó porque estaba
        # bien o porque el verificador se cayó.
        stages_json=_stages_json(result),
    )
    if rp is not None:
        rp.finished = True
        if error:
            rp.error, rp.phase = error, "error"
    if private:
        return  # El cliente consulta SQLite; no se publica el resultado privado en Discord.
    # Sugerencias de continuación (2026-07-26): 2-3 próximos pasos que
    # la UI pinta como botones. Van DESPUÉS de finish_chat
    # (el run ya está cerrado y persistido: si el turno extra falla o
    # tarda, no se pierde nada).
    suggestions = await _suggest_followups(
        db, user=user, answer=content,
        run_model=result.get("model") or model_override or "",
    ) if status == "ok" and not (project.get("defaults_json") or {}).get("task_feedback") else []
    if suggestions:
        try:
            await db.set_chat_suggestions(
                chat_id, json.dumps(suggestions, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            logger.warning("no pude guardar sugerencias de %s: %r", chat_id, e)

    # No verified Discord audience is associated with this run. Keep results,
    # usage and artifacts in Relay, including stateless runs that may retrieve
    # private knowledge. Body IDs and project channels never authorize delivery.


# Tope del turno extra que propone los próximos pasos. Corto a
# propósito: es un turno de una línea sobre texto ya escrito, y el
# humano está esperando la respuesta que ya terminó de generarse.
SUGGEST_TIMEOUT_S = 45.0


async def _suggest_followups(
    db: Database, *, user: str, answer: str, run_model: str = "",
) -> list[str]:
    """Sugerencias de continuación del run. Best-effort: [] si algo falla.

    Modelo: `suggestions_model` (system_config) > el modelo con el que
    corrió ESTE run > default global. Caer al modelo del run importa:
    un proyecto pineado a otro provider no tiene por qué tener key del
    default (y si no la tiene, el turno extra se cuelga hasta el timeout).

    Se apaga entero con `suggestions_enabled=0`.
    """
    model = run_model
    try:
        enabled = await db.get_config("suggestions_enabled", "1")
        if (enabled or "").strip().lower() in ("0", "false", "no", "off"):
            return []
        model = (await db.get_config("suggestions_model", "")) or run_model
    except Exception as e:  # noqa: BLE001
        logger.warning("sugerencias: no pude leer config (%r), sigo con default", e)
    try:
        return await asyncio.wait_for(
            experts.suggest_followups(user=user, answer=answer, model_spec=model),
            timeout=SUGGEST_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        # Sin botones se sigue pudiendo escribir: nunca rompemos el run.
        logger.info("sugerencias: la respuesta sale sin botones (%r)", e)
        return []
