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
from . import logctx
from . import mcp_pool
from . import notify
from . import progress
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
    """Etapas del runner → JSON para `chats.stages_json`, o None.

    Devuelve None cuando el run NO pasó por el runner por etapas
    (`run_expert` directo, opt-out del proyecto): así la columna queda
    NULL en vez de con un objeto de campos vacíos, y "no hubo etapas"
    se distingue de "hubo etapas y salieron vacías".

    El plan se recorta: es texto del planificador y no tiene por qué
    entrar entero en una fila de la DB — el valor de auditoría está en
    el veredicto y en qué modelo corrió cada etapa.
    """
    if not result.get("three_stage"):
        return None
    usage = result.get("stage_usage") or {}
    out = {
        "plan": (result.get("plan") or "")[:4000],
        "planner_model": result.get("planner_model") or "",
        "verifier_verdict": result.get("verifier_verdict") or "",
        "verifier_feedback": result.get("verifier_feedback") or "",
        "verifier_model": result.get("verifier_model") or "",
        "documenter_model": result.get("documenter_model") or "",
        "executor_model": result.get("model") or "",
        # Qué pasó con el trabajo. Ver `_resultado_del_trabajo`: no es
        # `chats.status`, que habla de la ejecución.
        "resultado": _resultado_del_trabajo(result),
    }
    # Cuántas pasadas necesitó el turno. El runner lo calcula y hasta hoy
    # lo tiraba: sin esto, "resuelto en una pasada" y "resuelto después
    # de tres rondas de needs_more" quedaban idénticos en la fila, y era
    # imposible medir si un cambio de prompt o de modelo mejoraba algo
    # sin volver a parsear los logs a mano.
    #
    # Plano el conteo (SQL lo alcanza), anidado el detalle (para leer un
    # caso puntual). El feedback se recorta como el resto del texto libre.
    rondas = result.get("verifier_rounds") or []
    if rondas:
        out["rondas"] = len(rondas)
        out["rondas_detalle"] = [
            {"ronda": r.get("ronda"), "verdict": r.get("verdict") or "",
             "phase_at_end": r.get("phase_at_end") or "",
             "tool_calls": r.get("tool_calls"),
             "feedback": (r.get("feedback") or "")[:200]}
            for r in rondas]
    # Veredictos de media corrida, uno por borde de tanda. Un corte por
    # desvío se veía en el mensaje al humano y no quedaba en ningún lado
    # para revisar después POR QUÉ el ejecutor se fue del plan.
    medios = result.get("mid_verdicts") or []
    if medios:
        out["mid_verdicts"] = [
            {"leg": m.get("leg"), "verdict": m.get("verdict") or "",
             "feedback": (m.get("feedback") or "")[:200],
             "error": str(m.get("error") or "")[:200]}
            for m in medios]
    # Tokens por etapa, planos para que `json_extract` los alcance desde
    # SQL sin recorrer objetos anidados. Una etapa que no reportó usage
    # NO escribe sus claves: ausente = "no se midió", que no es cero.
    for stage in ("planner", "verifier", "documenter"):
        u = usage.get(stage) or {}
        if u:
            out[f"{stage}_tokens_in"] = u.get("tokens_in", 0)
            out[f"{stage}_tokens_out"] = u.get("tokens_out", 0)
    # Etapas caídas (2026-08-15). Misma regla que los tokens: la clave
    # solo existe si hubo error, así que `IS NOT NULL` en SQL alcanza para
    # contar degradaciones. Sin esto, un planificador que corta por
    # timeout dejaba al ejecutor corriendo sin plan y el único rastro era
    # un WARNING en el log — el run se veía idéntico a uno planificado.
    for stage, err in (result.get("stage_errors") or {}).items():
        if err:
            out[f"{stage}_error"] = str(err)[:200]
    # Etapa B, P3: pasos que el ejecutor marcó con `plan_step_done`.
    # Se guarda como dict {paso_str: nota}, mismo criterio que el resto
    # de `stages_json`: ausente = nadie marcó nada, presente = al menos
    # uno. Si la key está vacía la omitimos para no ensuciar el JSON.
    ps = result.get("plan_steps_done")
    if isinstance(ps, dict) and ps:
        out["plan_steps_done"] = {str(k): str(v)[:200]
                                  for k, v in ps.items()}
    return json.dumps(out, ensure_ascii=False)


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
) -> None:
    """Ciclo de vida completo de un run de experto en background.

    ADR-024: el HTTP ya respondió 202; acá corremos, persistimos
    (.md + JSONL + finish_chat + historial de la conversación) y
    avisamos al bot por /notify con kind response|error|cancelled y
    la metadata que necesita para postear en el hilo correcto.
    """
    # Correlación (2026-07-25): de acá en más, TODA línea que se loguee
    # en esta task —y en las que cree, watchdog y callbacks incluidos—
    # sale con el chat_id. Sin esto, un run muerto había que
    # reconstruirlo a mano desde la DB. Va primero: si algo revienta en
    # el setup, queremos que ese error también quede atribuido.
    logctx.bind(chat_id, project.get("slug", ""))

    conv_id = conversation["id"] if conversation else None
    thread_id = conversation.get("discord_thread_id") if conversation else None
    history_json = (conversation.get("messages_json") or "") if conversation else ""
    # Lo que el experto verificó en turnos anteriores (2026-08-26). Va
    # aparte del historial porque sobrevive a cosas distintas: un corte
    # por `off_plan` le saca al historial las tool calls enteras, y la
    # bitácora viaja como instructions, que no se persisten.
    bitacora_json = (conversation.get("bitacora_json") or "") if conversation else ""

    # Fase 1: progress store + callback. Si run_expert no acepta
    # on_progress (versión vieja), el callback es noop — best-effort.
    progress_cb = experts.make_progress_callback(
        store=progress, notify=notify,
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
        )
        if result.get("phase_at_end") == "planned":
            result = await _grafo_en_vez_de_proponer(
                app, db, project, user, conv_id, result)
    except experts.ModelUnavailable as e:
        error, status = str(e), "error"
    except asyncio.TimeoutError:
        error, status = "timeout total del experto", "error"
    except asyncio.CancelledError:
        # Cancelar usa el mismo cierre durable que éxito/error; incluso si
        # todavía no había historial, la fila y los artefactos quedan cerrados.
        status = "cancelled"
        result = {**rescue, "content": "run cancelado", "phase_at_end": "cancelled"}
    except Exception as e:
        logger.exception("experto %s falló", target)
        error, status = f"{type(e).__name__}: {e}", "error"
    finally:
        running.pop(chat_id, None)
        # Estado post-mortem para que /status siga respondiendo hasta que el
        # sweeper lo limpie. OJO: `finished` NO se prende acá — se prende
        # abajo, DESPUÉS de persistir el historial (ver el comentario).
        rp = progress.get(chat_id)
        if rp is not None:
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

    # ADR-025: persistir el historial de la conversación. Va ANTES de
    # prender `finished` (bug 2026-07-27): la UI poll-ea /experts/status y
    # en cuanto ve finished=True hace GET .../messages. Con el flag prendido
    # primero, ese GET devolvía el historial VIEJO — las burbujas del turno
    # (prompt + respuesta + tool calls) no aparecían hasta recargar la
    # página, que era justo cuando el historial ya estaba guardado.
    if conv_id and status in ("ok", "cancelled") and result.get("messages_json"):
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
    duration_ms = result.get("duration_ms", int((time.monotonic() - t0) * 1000))
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
    )
    md_path = await finalization.finish(
        db, chat_id, status=status, artifact=artifact,
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
    # Sugerencias de continuación (2026-07-26): 2-3 próximos pasos que
    # la UI y Discord pintan como botones. Van DESPUÉS de finish_chat
    # (el run ya está cerrado y persistido: si el turno extra falla o
    # tarda, no se pierde nada) y ANTES del notify, para que el bot
    # pinte los botones en el MISMO mensaje de la respuesta.
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

    kind = "cancelled" if status == "cancelled" else ("error" if error else "response")
    # Iter 10.0: bridge Discord↔UI. Si la conversación tiene
    # discord_user_id (autor Discord original), se lo pasamos al bot en
    # el notify para que sepa a quién mandarle el reply — sin este campo
    # el bot no puede rutear la respuesta a Discord cuando el chat nació
    # en la UI. discord_thread_id es para hilos existentes (auto-attach);
    # discord_user_id es para chats UI→Discord (este bridge nuevo).
    notify_discord_user_id = (
        conversation.get("discord_user_id") if conversation else None)
    notify_discord_author = (
        conversation.get("discord_author") if conversation else None)
    # Iter 10.1: guard soft. Si el run vino de Discord y el proyecto
    # NO tiene discord_channel_id seteado, marcamos el notify para que
    # el bot sepa que tiene que preguntar al admin qué canal elegir.
    # El run corre normal (no rompemos nada). El flag NO se manda si:
    #   - source != "discord" (UI/CLI no necesitan canal — tienen su
    #     propio panel)
    #   - el proyecto ya tiene canal (comportamiento actual)
    missing_channel = (
        source == "discord"
        and not (project.get("discord_channel_id") or "").strip())
    if missing_channel:
        logger.warning(
            "experts/run: source=discord pero proyecto %r sin "
            "discord_channel_id (chat_id=%s conv_id=%s) — el bot "
            "preguntará al admin qué canal usar",
            target, chat_id, conv_id)
    # El runner agrega los adjuntos recibidos al texto aun si el modelo
    # no cita sus ids. Conservamos también las menciones de uploads manuales.
    run_attachments = [
        aid for aid in dict.fromkeys(_ATT_ID_RE.findall(content or ""))
        if attachments_mod.resolve(aid) is not None
    ]
    if run_attachments:
        logger.info("experts/run: chat=%s adjunta %d archivo(s) generados: %s",
                    chat_id, len(run_attachments), ", ".join(run_attachments))

    await notify.send(
        agent_id=f"chat:{chat_id}",
        kind=kind,
        message=error if error else content,
        metadata={
            "chat_id": chat_id,
            "conversation_id": conv_id,
            "discord_thread_id": thread_id,
            **({"attachments": run_attachments} if run_attachments else {}),
            # Iter 10.0: campos nuevos para bridge bidireccional
            "discord_user_id": notify_discord_user_id,
            "discord_author": notify_discord_author,
            # Canal default del proyecto: si está seteado, el bot postea la
            # respuesta ahí (aunque el run haya nacido en la UI/CLI). Sin
            # esto el bot no sabía el canal vinculado y no podía responder.
            # El bot arbitra la precedencia (thread existente > canal).
            "discord_channel_id": (project.get("discord_channel_id") or None),
            "target": target,
            "source": source,
            "status": status,
            "resultado": _resultado_del_trabajo(result),
            "verifier_verdict": result.get("verifier_verdict") or "",
            "verifier_feedback": result.get("verifier_feedback") or "",
            "verifier_error": (result.get("stage_errors") or {}).get("verifier") or "",
            "model": result.get("model", model_override),
            "tokens_in": result.get("tokens_in"),
            "cache_read_tokens": result.get("cache_read_tokens"),
            "tokens_out": result.get("tokens_out"),
            "tool_calls": result.get("tool_calls"),
            "duration_ms": duration_ms,
            # Medidor de contexto: el aviso ya va en el texto; esto es
            # para que el bot/UI puedan pintarlo aparte si quieren.
            **({"context_tokens": ctx["base_tokens"],
                "context_limit": ctx["limit"],
                "context_pct": ctx["pct"]} if ctx else {}),
            # Iter 10.1: el bot usa esto para decidir si pregunta al
            # admin qué canal elegir (solo si source=discord y el
            # proyecto no tiene canal).
            **({"missing_discord_channel": True} if missing_channel else {}),
            # El bot los pinta como botones; el custom_id lleva el
            # índice y resuelve el texto con GET /chats/{chat_id}.
            **({"suggestions": suggestions} if suggestions else {}),
        },
    )


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
