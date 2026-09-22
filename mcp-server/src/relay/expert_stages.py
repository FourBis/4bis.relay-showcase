"""Invocaciones acotadas del planificador, verificador y documentador."""
from __future__ import annotations
import asyncio
import logging
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse, TextPart
from typing import Any, Optional
from . import config, expert_models, expert_planning, expert_stage_prompts, expert_verdicts
from .bitacora import Bitacora

logger = logging.getLogger("relay.experts")


async def _run_planner(
    *, user: str, project: dict, model_spec: str,
    ponytail: str, on_progress: Any = None, is_followup: bool = False,
    history_recap: str = "", toolsets: Optional[list] = None,
    es_continuacion: bool = False, deadline: Optional[float] = None,
) -> tuple[str, dict, str]:
    """Un turno breve con el modelo económico: prompt → (plan, usage, error).

    `usage` son los tokens de ESTA etapa (ver `_stage_usage`): desde que
    cada etapa puede correr en un proveedor distinto, atribuirlos al
    ejecutor hacía que el dashboard reportara todo el consumo como si
    fuera del modelo pagado. `error` es "" cuando la etapa corrió: si
    trae algo, el ejecutor va a correr SIN plan y eso tiene que quedar
    auditable (2026-08-15) en vez de vivir solo en un WARNING del log.

    Sin herramientas, sin MCPs, sin cbm_query: el planificador NO toca
    el repositorio. Si devuelve "TRIVIAL: ...", el ejecutor recibe el
    plan como system_extra igual; es solo una señal para la UI ("esta
    tarea no necesitaba planificación").

    `history_recap` (2026-08-15) es el resumen corto de los últimos
    turnos del hilo — ver `_history_recap`. Antes de eso el planificador
    no veía nada de la conversación y en cada follow-up planificaba desde
    cero, contra un ejecutor que sí la recordaba.
    """
    spec = model_spec or config.planner_model_spec()
    pony = (ponytail or "").strip()
    sysp = (project.get("system_prompt") or "").strip()
    instructions = "\n\n".join(p for p in (pony, expert_stage_prompts.PLANNER_INSTRUCTIONS) if p)
    # La regla del razonador entra solo si el razonador está. Ver el
    # comentario de `PLANNER_REASONING_RULE`: pedirle que use una tool que
    # no tiene es lo que hacía que el intento de llamada saliera como
    # texto y el "plan" fuera un tool call serializado.
    if toolsets:
        instructions = f"{instructions}\n{expert_stage_prompts.PLANNER_REASONING_RULE}"
    # Ponemos el system_prompt del proyecto al final: el planner debe
    # conocer la "voz" del experto, pero sus instrucciones mandan.
    if sysp:
        instructions = f"{instructions}\n\n{sysp}"

    if on_progress is not None:
        try:
            await on_progress(phase="planner", tool=None, message=spec)
        except Exception:  # noqa: BLE001 — callback best-effort
            pass

    project_ctx = (
        f"## Contexto del proyecto\n"
        f"Slug: `{project.get('slug', '?')}`\n"
        f"Repo: `{project.get('repo_path', '?')}`\n"
    )
    # Retomar ("sigue con la 2") y pedir algo nuevo dentro de un hilo son
    # cosas distintas: en lo primero, proponer una descomposición
    # secuestra la conversación en vez de avanzarla; en lo segundo es
    # exactamente lo que hay que hacer. Ver `_es_continuacion`.
    if es_continuacion:
        project_ctx += (
            "Este pedido RETOMA algo que ya estaba en curso: planifica\n"
            "el siguiente paso y no uses `DEMASIADO_GRANDE:`.\n"
        )
    elif is_followup:
        project_ctx += (
            "Este pedido llega en un hilo que ya venía, pero abre algo\n"
            "NUEVO: juzga su tamaño por sí mismo, sin darlo por chico\n"
            "porque la conversación ya existía.\n"
        )
    # Memoria del hilo (2026-08-15). Va ANTES del pedido y con el encuadre
    # explícito de que es contexto, no la tarea: sin esa aclaración el
    # planificador tiende a re-planificar el turno viejo que está leyendo.
    recap_block = ""
    if history_recap.strip():
        recap_block = (
            "\n## Turnos previos del hilo (contexto, NO es el pedido)\n"
            "Resumen recortado de la conversación hasta acá. Úsalo para no\n"
            "re-planificar lo ya hecho y para entender a qué se refiere el\n"
            "pedido cuando dice \"eso\", \"continúa\" o \"el paso 3\".\n\n"
            f"{history_recap.strip()}\n"
        )
    prompt = (f"{project_ctx}{recap_block}\n"
              f"## Pedido del usuario\n{user.strip()}")
    # build_model va DENTRO del try: si queda afuera, un fallo al armar
    # el modelo que no sea ModelUnavailable escapa y mata el run entero,
    # justo lo que este best-effort quiere evitar.
    try:
        agent = Agent(
            expert_models.build_model(spec), instructions=instructions,
            tool_timeout=config.tool_timeout_s(),
            toolsets=list(toolsets or []),
        )
        # Con el razonador enchufado el turno deja de ser "una pregunta,
        # una respuesta": son varias vueltas de pensamiento antes de
        # escribir el plan, y 60s no alcanzan.
        limite = expert_verdicts._PV_REASONING_TIMEOUT_S if toolsets else expert_stage_prompts._PV_TIMEOUT_S
        result = await asyncio.wait_for(agent.run(prompt), timeout=expert_planning._stage_timeout(limite, deadline))
        return expert_verdicts._clean_stage_output(result.output or ""), expert_verdicts._stage_usage(result), ""
    except expert_models.ModelUnavailable:
        # El planner Y el ejecutor comparten la misma falta de key
        # cuando el modelo es el mismo: si el planner propaga, el
        # ejecutor también lo haría. Mejor propagar al server una sola
        # vez que terminar con un ejecutor que va a fallar igual, y
        # dejar el chat en un limbo que el test_end_to_end confunde con
        # "ok porque respondió algo" (caso real: el patch de os.environ
        # del test se cierra ANTES de que corra el background task →
        # build_model ve la key real → el LLM responde de verdad).
        raise
    except Exception as e:  # noqa: BLE001 — planner best-effort
        # 2026-08-15: además del WARNING, el error viaja de vuelta. Un
        # planificador caído (429 del free tier, timeout de 60s) dejaba al
        # ejecutor corriendo a ciegas sin que quedara rastro fuera del log:
        # el turno se veía idéntico a uno planificado. Ahora se persiste en
        # `chats.stages_json` y se emite como evento de progreso.
        err = f"{type(e).__name__}: {e}"[:200]
        logger.warning("planner falló (%r) — ejecutor corre sin plan", e)
        if on_progress is not None:
            try:
                await on_progress(phase="planner", tool=None,
                                  message=f"sin plan ({err})")
            except Exception:  # noqa: BLE001 — callback best-effort
                pass
        return "", {}, err


async def _run_verifier(
    *, user: str, plan: str, executor_result: dict,
    model_spec: str, ponytail: str, on_progress: Any = None, deadline: Optional[float] = None,
) -> tuple[str, str, dict, str, list[int]]:
    """Un turno breve con el modelo económico: plan + resultado → veredicto.

    Devuelve `(verdict, feedback, usage, error, pasos_verificados)`;
    `usage` son los tokens de esta etapa (ver `_stage_usage`), `{}` si
    no se pudieron medir, `error` es "" salvo que la etapa se haya
    caído (2026-08-15: queda en `chats.stages_json` para poder separar
    "el trabajo estaba mal" de "el verificador no corrió"), y
    `pasos_verificados` es la lista de pasos del plan que el verificador
    vio como cubiertos (Etapa B de GRAFO_SIEMPRE_PLAN.md).

    Nunca rompe el run: si el verificador falla, devuelve
    `needs_human` con el error como feedback. NO devuelve `complete`
    —eso aprobaría en silencio un run que nadie revisó—, y tampoco
    `needs_more`, que haría reintentar al ejecutor por una caída que
    no tiene nada que ver con la calidad del trabajo.
    """
    spec = model_spec or config.verifier_model_spec()
    pony = (ponytail or "").strip()
    instructions = "\n\n".join(p for p in (pony, expert_stage_prompts.VERIFIER_INSTRUCTIONS) if p)

    if on_progress is not None:
        try:
            await on_progress(phase="verifier", tool=None, message=spec)
        except Exception:  # noqa: BLE001
            pass

    tools_text = expert_planning._render_tool_calls(executor_result)
    # Evidencia dura, del `bitacora_json` que el ejecutor ya devuelve:
    # comandos con su exit code, pasos marcados y hechos anotados. Sin
    # esto el verificador solo veía qué tools se LLAMARON, o sea que no
    # podía separar "corrió las pruebas" de "las pruebas pasaron".
    # Se deriva acá adentro y no se pasa por parámetro para que los dos
    # call sites —el supervisor de media corrida y el de cierre— la
    # tengan sin tocar sus llamadas.
    evidencia = ""
    try:
        raw = executor_result.get("bitacora_json") or ""
        if raw:
            evidencia = Bitacora.cargar(raw).evidencia(
                max_chars=4000 if executor_result.get("graph_id") else 1500)
    except Exception:  # noqa: BLE001 — sin evidencia se verifica peor, no se rompe
        logger.warning("verificador: no pude armar la evidencia", exc_info=True)
    prompt = (
        f"## Pedido del usuario\n{user.strip()[:2000]}\n\n"
        f"## Plan\n{(plan or '(sin plan: tarea trivial o el planificador falló)').strip()[:2000]}\n\n"
        f"## Estado del run\n{executor_result.get('phase_at_end', '?')}\n\n"
        f"## Herramientas ejecutadas\n{tools_text}\n\n"
        f"## Evidencia\n"
        f"{evidencia or '(el run no dejó comandos ni hechos anotados)'}\n\n"
        + ("En este grafo, usa la comprobación más reciente de cada alcance; "
           "un fallo anterior o de un comando auxiliar no invalida una "
           "comprobación posterior. Los recortes y resultados sin correlación "
           "son límites de evidencia. Un exit=0 no demuestra por sí solo que "
           "se cumplió el contrato solicitado.\n\n"
           if executor_result.get("graph_id") else "")
        + f"## Resultado del ejecutor\n"
        f"{expert_planning._head_tail(executor_result.get('content') or '', head=1200, tail=1800)}"
    )

    # build_model va dentro del try por lo mismo que en _run_planner.
    try:
        agent = Agent(
            expert_models.build_model(spec), instructions=instructions,
            tool_timeout=config.tool_timeout_s(),
        )
        result = await asyncio.wait_for(agent.run(prompt), timeout=expert_planning._stage_timeout(expert_stage_prompts._PV_TIMEOUT_S, deadline))
        verifier_text = str(result.output or "")
        verdict, feedback, _pasos_verificados = expert_verdicts._parse_verifier(verifier_text)
        # Corrección dinámica del plan (Etapa B, t5). Solo se publica
        # cuando el veredicto EXIGE re-trabajo (needs_more u off_plan):
        # el camino `complete` no debe contaminar `stages_json` con
        # payloads vacíos, y re-ejecuciones del mismo diff contra el
        # mismo veredicto deben dar el mismo estado de usage.
        # Idempotente: si el LLM no emitió el bloque fenced
        # VERIFIER_VERDICT, el helper devuelve None y usage queda sin
        # la clave (no se mete un `null` que después se confunda con
        # "había corrección y se rompió al validar"). Lo mismo si
        # falla `_validate_verifier_verdict_payload` — en ese caso
        # sí registramos el motivo en `usage["plan_correction_error"]`
        # para que t6 (orquestador) decida si reintenta o descarta.
        usage = expert_verdicts._stage_usage(result)
        if verdict in ("needs_more", "off_plan"):
            pc = expert_verdicts._extract_plan_correction(verifier_text, verdict_hint=verdict)
            if pc is not None:
                usage = {**usage, "plan_correction": pc}
            else:
                # El bloque estaba y falló, o no estaba. Distinguimos
                # los dos casos: si el regex encontró el fence pero
                # la validación tiró, lo decimos; si directamente no
                # había fence, no es error (el LLM puede haber emitido
                # solo el bloque de prosa).
                if expert_verdicts._VERDICT_FENCE_RE.search(verifier_text):
                    usage = {
                        **usage,
                        "plan_correction_error": (
                            "bloque VERIFIER_VERDICT presente pero invalido"),
                    }
        return (
            verdict, feedback, usage,
            "" if expert_verdicts._verdict_match(expert_verdicts._clean_stage_output(verifier_text)) else "veredicto ausente o inválido",
            list(_pasos_verificados or []),
        )
    except expert_models.ModelUnavailable:
        # Mismo razonamiento que el planificador: si el verificador no
        # puede armar su modelo, se propaga. El ejecutor ya corrió bien;
        # el caller (server) prefiere enterarse de un ModelUnavailable
        # único a recibir un content "completo" con un verificador que
        # no pudo verificar.
        raise
    except Exception as e:  # noqa: BLE001 — el verificador no debe romper el run
        # 2026-08-14: antes esto devolvía `complete`. Con MiniMax pagado
        # casi nunca se activaba; desde que las etapas corren en los
        # endpoints GRATIS de NVIDIA (429 / timeout son esperables), un
        # verificador caído aprobaba el run en silencio — justo el caso
        # en que menos se sabe si el trabajo está bien.
        # `needs_human` es el mismo estado que usa `run_expert_staged`
        # cuando el ejecutor termina roto: no rompe el run, pero le
        # antepone el aviso ⚠️ al content para que el humano mire.
        logger.warning("verificador falló (%r): needs_human, run sin verificar", e)
        if on_progress is not None:
            try:
                await on_progress(phase="verifier", tool=None,
                                  message=f"sin verificar ({type(e).__name__})")
            except Exception:  # noqa: BLE001 — callback best-effort
                pass
        return "needs_human", (
            f"el verificador no pudo correr ({type(e).__name__}): este run "
            f"NO fue verificado, revisá el resultado a mano"), {}, (
            f"{type(e).__name__}: {e}"[:200]), []


async def _run_documenter(
    *, user: str, plan: str, executor_result: dict, verdict: str,
    feedback: str, model_spec: str, ponytail: str, on_progress: Any = None, deadline: Optional[float] = None,
) -> tuple[str, dict, str]:
    """Un turno breve de redacción: run → (registro, usage, error).

    Devuelve el bloque markdown que `run_expert_staged` agrega al
    `content`. Best-effort: si falla, devuelve "" y el run sigue con la
    respuesta del ejecutor tal cual — documentar nunca debe romper un
    run que ya terminó bien. `error` queda en `chats.stages_json`
    (2026-08-15) para distinguir "no había nada que documentar" de "el
    documentador se cayó".
    """
    spec = model_spec or config.documenter_model_spec()
    pony = (ponytail or "").strip()
    instructions = "\n\n".join(p for p in (pony, expert_stage_prompts.DOCUMENTER_INSTRUCTIONS) if p)

    if on_progress is not None:
        try:
            await on_progress(phase="documenter", tool=None, message=spec)
        except Exception:  # noqa: BLE001
            pass

    tools_text = expert_planning._render_tool_calls(executor_result)
    prompt = (
        f"## Pedido del usuario\n{user.strip()[:2000]}\n\n"
        f"## Plan\n{(plan or '(sin plan)').strip()[:2000]}\n\n"
        f"## Herramientas ejecutadas\n{tools_text}\n\n"
        f"## Veredicto del verificador\n{verdict}: {feedback}\n\n"
        f"## Respuesta final del ejecutor\n"
        f"{expert_planning._head_tail(executor_result.get('content') or '', head=1500, tail=2500)}"
    )

    # build_model va dentro del try por lo mismo que en _run_planner.
    try:
        agent = Agent(
            expert_models.build_model(spec), instructions=instructions,
            tool_timeout=config.tool_timeout_s(),
        )
        result = await asyncio.wait_for(agent.run(prompt), timeout=expert_planning._stage_timeout(expert_stage_prompts._PV_TIMEOUT_S, deadline))
        return expert_verdicts._clean_stage_output(result.output or ""), expert_verdicts._stage_usage(result), ""
    except expert_models.ModelUnavailable as e:
        # A diferencia del planificador y del verificador, aquí NO se
        # propaga: el ejecutor ya terminó y su respuesta es válida.
        # Perder el registro del cambio no justifica marcar el run como
        # fallido y hacer que el humano lo repita.
        logger.warning("documentador sin modelo disponible: sigo sin registro")
        return "", {}, f"ModelUnavailable: {e}"[:200]
    except Exception as e:  # noqa: BLE001 — documentador best-effort
        logger.warning("documentador falló (%r): sigo sin registro", e)
        return "", {}, f"{type(e).__name__}: {e}"[:200]


# La marca existe porque el registro, anexado crudo al texto del
# asistente, es indistinguible de algo que escribió el experto — así que
# al turno siguiente el modelo lo imita, y encima el documentador le
# anexa uno nuevo. Medido en sample-app el 17/8: los bloques "Qué se hizo /
# Archivos tocados / Cómo verificarlo / Pendiente" crecían de a uno por
# turno dentro de la misma conversación (2 → 3 → 4 → 5) hasta que la
# respuesta eran cinco registros casi idénticos y ninguna respuesta.
#
# Va SOLO en el historial: el `content` que lee el humano se queda
# limpio, que para eso el documentador escribe en prosa.
_DOC_MARCA = "[registro automático del relay — NO lo reproduzcas]"
_DOC_SEP = f"\n\n---\n\n{_DOC_MARCA}\n"


def _merge_doc_into_history(messages_json: str, doc: str) -> str:
    """Mete el registro del documentador en el último texto del historial.

    Por qué (2026-08-15): el bloque del documentador se concatenaba al
    `content` que ve el humano pero NO al `messages_json` que se replaya
    al turno siguiente. Resultado: vos leías "Archivos tocados: X ·
    Pendiente: Y" y al pedir "seguí con el pendiente que anotaste" el
    experto no tenía ese texto. Un desajuste entre lo que ve el humano y
    lo que ve el modelo, en el único artefacto que la capa 3
    (`_slim_history`) deja cruzar el user prompt.

    Se ANEXA al último `TextPart` del último `ModelResponse` en vez de
    agregar un response nuevo, justamente por `_slim_history`: de cada
    turno viejo sobrevive un solo response, así que un mensaje aparte se
    llevaría puesta la respuesta real del ejecutor.

    Best-effort: ante cualquier problema devuelve el historial original.
    """
    if not (doc or "").strip() or not (messages_json or "").strip():
        return messages_json
    try:
        messages = list(ModelMessagesTypeAdapter.validate_json(messages_json))
        for m in reversed(messages):
            if not isinstance(m, ModelResponse):
                continue
            texts = [p for p in m.parts if isinstance(p, TextPart)]
            if not texts:
                # Un response sin texto (solo tool calls) no es el cierre
                # del turno: seguimos buscando hacia atrás.
                continue
            # Si ya hay un registro de un turno anterior en este texto, se
            # REEMPLAZA en vez de apilarse: dos registros en el historial
            # son dos ejemplos del formato a imitar, no el doble de
            # contexto útil. Idempotente por el mismo camino.
            previo = texts[-1].content or ""
            corte = previo.find(_DOC_SEP)
            if corte != -1:
                previo = previo[:corte]
            texts[-1].content = f"{previo.rstrip()}{_DOC_SEP}{doc.strip()}"
            return ModelMessagesTypeAdapter.dump_json(messages).decode("utf-8")
        return messages_json
    except Exception as e:  # noqa: BLE001 — nunca romper por documentar
        logger.warning("no pude anexar el registro al historial (%r)", e)
        return messages_json
