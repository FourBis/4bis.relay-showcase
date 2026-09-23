"""Iteración del modelo con vigilancia, cancelación y rescate del historial."""
from __future__ import annotations
import anyio
import asyncio
import httpx
import json
import logging
import time
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.usage import UsageLimits
from . import (
    expert_context,
    expert_history,
    expert_run_state,
    expert_selection,
    expert_steps,
    expert_toolsets,
)
from .progress import _clip_say

logger = logging.getLogger("relay.experts")


async def run_iteration(agent: Agent, state: expert_run_state.ExpertRunState, _emit_progress) -> None:
    """Cuerpo de iter() — corre FUERA del wait_for para que el
    timeout duro cancele la task entera (no esperamos a un cancel
    cooperativo que pydantic-ai 2.5.1 no garantiza).

    Bug fix 2026-07-20: incluye un watchdog de idle. Si pasan
    >`expert_idle_timeout_s` segundos sin que el agente emita
    NINGÚN node (ni CallToolsNode, ni ModelResponseNode, ni
    ModelRequestNode), el experto está colgado esperando al
    provider (MiniMax M3 a veces deja la conexión abierta sin
    responder). Cancelamos la agent_run y propagamos
    asyncio.TimeoutError con un mensaje accionable — antes esto
    se manifestaba como "tarea muerta a 600s" sin contexto de
    que el problema fue idle, no budget. Cubre el caso del
    usuario donde el experto AUDITABA bien pero un tool call
    largo lo dejaba colgado.
    """
    # Per-project override del cap idle (defaults_json.idle_timeout_s).
    # Si no está seteado en el project, usa el global del config.
    idle_timeout = float(state._idle_to)
    async with agent.iter(
        expert_history._prompt_con_imagenes(state.user, state.images),
        message_history=state.message_history,
        usage_limits=UsageLimits(request_limit=state.request_limit,
                                 total_tokens_limit=state.task_token_limit),
        usage=state.task_usage,
    ) as agent_run:
        # Watchdog: corre en paralelo al async for. Si pasan
        # >idle_timeout sin un solo node nuevo, asumimos provider
        # colgado y cancelamos LA TASK que corre el async for.
        # Bug fix 2026-07-20b: `raise CancelledError` DENTRO del
        # watchdog solo mataba al watchdog mismo — el run seguía
        # vivo hasta el tope global (por eso los chats morían a los
        # 600s exactos como "timeout total" sin mensaje de idle).
        # Además: heartbeat hacia el bot/UI cuando el modelo piensa
        # largo sin emitir nodes (contexto largo ⇒ minutos por
        # respuesta) para que el hilo no parezca muerto.
        last_node_at = time.monotonic()
        idle_cause: list[str] = [None]  # mutable para que el watchdog escriba
        runner_task = asyncio.current_task()

        async def _idle_watchdog() -> None:
            last_beat = time.monotonic()
            while True:
                await asyncio.sleep(min(15.0, idle_timeout / 4))
                now = time.monotonic()
                idle_s = now - last_node_at
                # Tool en vuelo (2026-08-16): el experto NO está
                # idle — está esperando un comando que puede tardar
                # minutos (`dotnet test`, `npm ci`, un build). Antes
                # esto contaba como idle y el watchdog mataba el run
                # entero a los 180s, así que el techo real de
                # cualquier comando era el watchdog. Quien acota la
                # tool es su propio timeout (`_mcp_tool_to`), no
                # esto.
                #
                # El margen: si la tool lleva MÁS que su propio
                # techo + 30s, es que ese corte falló (un
                # subprocess que no muere, un transport trabado) y
                # el watchdog vuelve a ser la red de seguridad.
                running_tool, _desde = expert_toolsets.tool_en_vuelo(state._inflight)
                tool_s = now - (_desde or now)
                # `thinking`/`writing` = el agente esta esperando al
                # modelo. Es la unica ventana donde "generando" y
                # "colgado" son indistinguibles desde el loop, y por
                # eso es la unica que paga el cap grande.
                verdict = expert_toolsets._watchdog_verdict(
                    idle_s=idle_s, idle_timeout=idle_timeout,
                    tool_s=tool_s if running_tool else None,
                    tool_timeout=state._mcp_tool_to,
                    pensando=state.last_phase in ("thinking", "writing"),
                    think_timeout=state._think_to)
                if verdict == "tool_wait":
                    # Latido, NO una fase nueva: `make_progress_callback`
                    # trata cualquier fase desconocida pisando `rp.phase`
                    # y refrescando `last_activity_at` — o sea, mentiría
                    # la fase real (`tool_call`) y el idle_s de /status.
                    # La rama `heartbeat` ya hace lo correcto: avisa al
                    # bot/UI sin tocar el estado. El nombre de la tool
                    # viaja igual, en `rp.last_tool`.
                    if (now - last_beat) >= 60.0:
                        last_beat = now
                        await _emit_progress(
                            phase="heartbeat", tool=None)
                    continue
                if verdict == "kill":
                    idle_cause[0] = (
                        f"experto idle por >{idle_timeout:.0f}s "
                        "(probable provider colgado, no budget ni "
                        "work real)"
                        + (f"; la tool `{running_tool}` pasó su propio "
                           f"techo de {state._mcp_tool_to:.0f}s sin cortar"
                           if running_tool else ""))
                    logger.warning(
                        "run_expert: %s — cancelando runner",
                        idle_cause[0])
                    runner_task.cancel()
                    return
                # >45s sin nodes pero aún bajo el cap: el modelo
                # está pensando (típico con historial largo). Un
                # latido por minuto hacia la UI/Discord.
                if verdict == "beat" and (now - last_beat) >= 60.0:
                    last_beat = now
                    await _emit_progress(phase="heartbeat", tool=None)

        watchdog_task = asyncio.create_task(
            _idle_watchdog(), name=f"expert-idle-{id(agent_run)}")
        try:
            async for node in agent_run:
                last_node_at = time.monotonic()
                # Steer del humano (2026-07-25): cortamos ACÁ, en el
                # borde de nodo, no a mitad de una tool. El historial
                # rescatado + su corrección se re-inyectan en el round
                # loop, así redirigir cuesta un round-trip y no el run
                # entero (antes la única salida era cancelar, que
                # además tiraba el avance).
                if state.steer:
                    _nudge = "\n".join(state.steer).strip()
                    state.steer.clear()
                    try:
                        _msgs = agent_run.all_messages()
                        expert_history._close_orphan_tool_calls(
                            _msgs,
                            reason="el humano corrigió el rumbo mientras "
                                   "esta tool corría")
                        state.messages_json = expert_history._dump_messages(_msgs)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "steer: no pude rescatar historial (%r) — "
                            "sigo sin cortar, se pierde la corrección", e)
                        _nudge = ""
                    if _nudge:
                        _u = getattr(agent_run, "usage", None)
                        state.usage = _u() if callable(_u) else _u
                        state.steer_text = _nudge
                        state.last_phase = "steered"
                        logger.info(
                            "run_expert: steer del humano tras %d tools — "
                            "re-entro con la corrección", state.tool_calls_count)
                        await _emit_progress(
                            phase="steer", tool=None,
                            message=_clip_say(_nudge))
                        # `return`, no `break`: al salir del async for
                        # normalmente el código de abajo lee
                        # agent_run.result, que en un corte a mitad es
                        # None. Mismo patrón que el corte por
                        # presupuesto. El finally mata el watchdog.
                        return
                # ModelRequestNode → el agente está pensando (post-tool)
                # CallToolsNode → el modelo respondió (con tools o con
                #   el texto final; pydantic-ai 2.x NO emite un
                #   ModelResponseNode, la rama que lo esperaba estaba
                #   muerta y por eso la fase quedaba clavada en la tool
                #   vieja mientras el modelo redactaba)
                node_kind = type(node).__name__
                if node_kind == "CallToolsNode":
                    # Tools pedidas en este turno, CON sus args: el
                    # modelo pide varias en un mismo response y cada
                    # una necesita su propio evento (ver abajo).
                    mr = getattr(node, "model_response", None)
                    pedidas, says, _recent = expert_steps._clasificar_partes(mr)
                    state.recent_tool_calls.extend(_recent)
                    # El POR QUÉ (2026-07-25): el mismo response que pide
                    # las tools trae el TextPart donde el modelo dice qué
                    # va a hacer y por qué. Lo tirábamos — la UI mostraba
                    # "📄 leyó x.py" sin motivo, y un run de 20 pasos era
                    # una lista de tools sin hilo narrativo. Va ANTES del
                    # paso de tool: primero dice, después hace.
                    for txt in says:
                        await _emit_progress(
                            phase="say", tool=None,
                            message=_clip_say(txt))
                    if pedidas:
                        state.last_tool_name = pedidas[-1][0]
                        state.last_phase = "tool_call"
                        # Streaming al bot (2026-07-20d): en vez de un
                        # genérico "🔧 tool", armamos una línea legible
                        # por cada tool call (con el path/cmd) y, para
                        # edit_file, el diff old→new (que ya viene en
                        # los args — no tocamos el filesystem). El bot
                        # acumula estas líneas en un timeline vivo.
                        #
                        # UNO POR TOOL, no uno por turno (2026-09-04).
                        # Antes se contaban N y se emitía solo el
                        # último, con los args del PRIMER part de ese
                        # nombre: el timeline perdía el 30,8% de las
                        # tool calls (2.586 de 8.383 medidas sobre 228
                        # chats) y, cuando el turno traía dos shell,
                        # mostraba un comando pegado a la salida del
                        # otro. Se ejecutaban igual — lo que faltaba
                        # era el rastro para el humano.
                        for nombre, args, call_id in pedidas:
                            state.tool_calls_count += 1
                            step_msg, step_diff, step_cmd = (
                                expert_steps._format_tool_step(nombre, args))
                            await _emit_progress(
                                phase="tool_call",
                                tool=nombre,
                                tool_calls=state.tool_calls_count,
                                message=step_msg,
                                diff=step_diff,
                                cmd=step_cmd,
                                tool_call_id=call_id,
                            )
                    elif says:
                        # Response con texto y sin tools = la respuesta
                        # final ya está aterrizando.
                        state.last_phase = "writing"
                        await _emit_progress(phase="writing", tool=None)
                elif node_kind == "ModelRequestNode":
                    # El request que vuelve al modelo trae los
                    # ToolReturnPart de las tools que acaban de correr:
                    # es el único lugar donde se ve lo que realmente
                    # entra al historial (y por lo tanto lo que se
                    # reenvía en cada vuelta desde acá hasta el final).
                    state.meter_turn += 1
                    expert_context._measure_tool_returns(
                        node, state.tool_meter, state.meter_turn)
                    # …y es también el único lugar donde se ve QUÉ
                    # contestó la terminal. Se engancha al paso de la
                    # tool que ya está en el timeline, así la tarjeta
                    # queda "comando + salida" en vez de solo el
                    # comando (2026-08-27).
                    for _tname, _tout, _call_id in expert_steps._console_tool_outputs(node):
                        if state.out_budget <= 0:
                            break
                        state.out_budget -= len(_tout)
                        await _emit_progress(
                            phase="tool_result", tool=_tname,
                            output=_tout, tool_call_id=_call_id)
                    # Volvió a pensar (arranque o post-tool). Sin esto la
                    # UI se quedaba con el "🔧 read_file" de hace 50s en
                    # pantalla mientras el modelo redactaba, y parecía
                    # colgado. El guard evita repetir el evento.
                    if state.last_phase != "thinking":
                        state.last_phase = "thinking"
                        await _emit_progress(phase="thinking", tool=None)
                # UserPromptNode y EndNode no nos dicen nada nuevo.
        except expert_selection._CapabilityRequested:
            state.message_history = list(agent_run.all_messages())
            expert_history._close_orphan_tool_calls(state.message_history, reason="activación de capacidad en curso")
            state.messages_json = expert_history._dump_messages(state.message_history)
            state.usage = agent_run.usage()
            raise
        except asyncio.CancelledError:
            # Cancel de la task del runner. Tres causas: watchdog
            # (idle), wait_for (tope global) o cancel del usuario.
            # En TODAS rescatamos el historial parcial PRIMERO —
            # sin esto el avance se pierde y el próximo turno de la
            # conversación arranca de cero (el "falla a contextos
            # largos": cada retry re-pagaba todo el trabajo).
            if state.rescue is not None:
                state.rescue.update(
                    progress_events=state.progress_events, tool_calls=state.tool_calls_count,
                    duration_ms=int((time.monotonic() - state.t0) * 1000),
                    model=state.spec, last_tool=state.last_tool_name)
            try:
                _msgs = agent_run.all_messages()
                # Bug fix 2026-07-25: si cortamos con una tool en
                # vuelo, el historial queda con un tool call sin
                # respuesta y el **continúa** que ofrecemos abajo
                # muere con UserError. Lo cerramos antes de serializar.
                expert_history._close_orphan_tool_calls(
                    _msgs,
                    reason="el run se cortó mientras esta tool corría")
                state.messages_json = expert_history._dump_messages(_msgs)
                # Bug fix 2026-07-25: el cancel del humano re-lanza y
                # server.py retorna temprano, así que el historial que
                # acabamos de rescatar moría acá — cancelar costaba TODO
                # el avance y el próximo turno arrancaba de cero. `rescue`
                # lo saca sin await (la task ya está cancelada).
                if state.rescue is not None:
                    state.rescue["messages_json"] = state.messages_json
                    state.rescue["tool_calls"] = state.tool_calls_count
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "no pude rescatar historial parcial tras cancel: %r", e)
            # Bug fix 2026-07-25: rescatar el usage igual que hacen
            # las ramas de retries/budget. Sin esto, todo run cortado
            # por idle se guardaba con tokens_in/out NULL — el trabajo
            # se pagó igual y las métricas lo contaban como 0→0.
            state.usage = getattr(agent_run, "usage", None)
            if callable(state.usage):
                try:
                    state.usage = state.usage()
                except Exception:  # noqa: BLE001
                    state.usage = None
            if state.rescue is not None and state.usage is not None:
                state.rescue["tokens_in"] = state.tokens_in_prev + (state.usage.input_tokens or 0)
                state.rescue["tokens_out"] = state.tokens_out_prev + (state.usage.output_tokens or 0)
                state.rescue["cache_read_tokens"] = state.cache_prev + (state.usage.cache_read_tokens or 0)
            if idle_cause[0] is None:
                # No fue el watchdog: tope global o cancel real.
                # Que el round loop / caller clasifique.
                raise
            # Bug fix 2026-07-25: no culpar al provider a ciegas. Si
            # el corte nos agarró en fase tool_call, el sospechoso es
            # la tool que nunca devolvió — y su nombre ya lo tenemos.
            # Culpar al provider mandaba a debuggear MiniMax cuando el
            # cuelgue real era un `npx vite --host` local que dejaba
            # un node vivo reteniendo los pipes (un chat de ejemplo).
            if state.last_phase == "tool_call" and state.last_tool_name:
                culprit = (
                    f"la tool `{state.last_tool_name}` no devolvió en "
                    f">{idle_timeout:.0f}s. Suele ser un comando que "
                    "no termina solo (un server en foreground, un "
                    "prompt interactivo) o un proceso hijo que quedó "
                    "colgado — no el provider.")
            else:
                culprit = (
                    f"el experto quedó idle >{idle_timeout:.0f}s sin "
                    "emitir ningún evento. Probable provider colgado.")
            state.last_phase = "idle_timeout"
            state.output_text = (
                f"⚠️ Corté el run: {culprit} Guardé lo avanzado: "
                "manda **continúa** para retomar desde acá, o sube "
                "`expert_idle_timeout_s` si de verdad esperabas algo "
                "tan largo.")
            logger.warning(
                "run_expert: idle_timeout tras %.0fs (tool=%s, "
                "tool_calls=%d)", idle_timeout, state.last_tool_name,
                state.tool_calls_count)
            raise
        except UnexpectedModelBehavior as umb:
            # Bug fix 2026-07-20: capturar específicamente el caso
            # "Tool 'X' exceeded max retries" en vez de propagar la
            # excepción cruda. El LLM insistió N veces con un tool
            # call que la tool rechazó (path inválido, archivo que
            # no existe, etc.) y pydantic-ai cortó. Con retries=3
            # arriba le dimos más chances, pero si aún así falla,
            # NO matamos el chat: rescatamos el historial parcial
            # y devolvemos un mensaje accionable que liste los
            # archivos problemáticos. El humano puede retomar el
            # chat con la info concreta.
            msg = str(umb.message or "")
            if "exceeded max retries" in msg or "max retries" in msg:
                state.last_phase = "tool_retries_exhausted"
                # Extraer la tool y el último path intentado del
                # historial parcial para que el humano sepa qué
                # archivo fue el problema.
                failing_tool = "?"
                failing_args: dict = {}
                try:
                    for m in reversed(
                            agent_run.all_messages()):
                        for part in getattr(m, "parts", []) or []:
                            tname = getattr(part, "tool_name", None)
                            if tname and getattr(
                                    part, "args", None):
                                failing_tool = tname
                                failing_args = dict(
                                    part.args or {})
                                break
                        if failing_tool != "?":
                            break
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "tool_retries_exhausted: no pude extraer "
                        "args fallidos: %r", e)
                # Guardar el historial para resume. Igual que en el
                # cancel: la tool que agotó los retries puede quedar
                # sin respuesta y romper el **continúa**.
                try:
                    _msgs = agent_run.all_messages()
                    expert_history._close_orphan_tool_calls(
                        _msgs,
                        reason="la tool agotó los reintentos")
                    state.messages_json = expert_history._dump_messages(_msgs)
                except Exception:  # noqa: BLE001
                    pass
                state.usage = getattr(agent_run, "usage", None)
                if callable(state.usage):
                    try:
                        state.usage = state.usage()
                    except Exception:  # noqa: BLE001
                        state.usage = None
                state.output_text = (
                    f"⚠️ La tool `{failing_tool}` rechazó el "
                    "tool call 3 veces seguidas con los mismos "
                    f"args (`{json.dumps(failing_args, ensure_ascii=False)[:300]}`). "
                    "El bot se quedó insistiendo con el mismo path/"
                    "argumento en vez de corregirlo. Guardé lo "
                    "avanzado: manda **continúa** para que siga, o "
                    "indica explícitamente el path correcto del "
                    "archivo que quieres que lea/edite."
                )
                logger.warning(
                    "run_expert: tool_retries_exhausted tool=%s "
                    "args=%s tool_calls=%d", failing_tool,
                    failing_args, state.tool_calls_count)
                return  # salimos con status ok + output_text accionable
            # Cualquier otra UnexpectedModelBehavior: propagamos
            # para que el caller decida (server.py lo convierte en
            # mensaje al humano con el contexto del chat).
            raise
        except UsageLimitExceeded:
            # Corte por presupuesto (request_limit): el modelo loopeó sin
            # converger (típico de tareas mal acotadas — ver ESTADO.md).
            # NO perdemos lo andado: rescatamos el historial parcial
            # (agent_run.all_messages() sirve mid-run) para que la
            # conversación quede REANUDABLE, y devolvemos un mensaje útil
            # en vez de propagar el stacktrace crudo. status sigue "ok";
            # phase_at_end="budget_exceeded" marca el corte para diagnóstico.
            state.budget_exceeded = True
            state.last_phase = "budget_exceeded"
            try:
                state.messages_json = expert_history._dump_messages(agent_run.all_messages())
            except Exception as e:  # noqa: BLE001 — rescate best-effort
                logger.warning(
                    "no pude rescatar historial parcial tras corte por "
                    "presupuesto: %r", e)
            state.usage = getattr(agent_run, "usage", None)
            if callable(state.usage):
                try:
                    state.usage = state.usage()
                except Exception:  # noqa: BLE001
                    state.usage = None
            state.output_text = (
                f"⚠️ Esta tarea superó el presupuesto de {state.request_limit} "
                "pasos del bot antes de terminar (probablemente es muy "
                "amplia para un solo mensaje). Guardé lo avanzado: puedes "
                "escribir **continúa** para que siga desde acá, o acotar "
                "el alcance (menos archivos/objetivos por vez)."
            )
            return
        except (ModelHTTPError, httpx.HTTPError,
                anyio.ClosedResourceError, anyio.BrokenResourceError,
                anyio.EndOfStream) as e:
            # Caída del proveedor a mitad del run (2026-08-26). El
            # endpoint gratis de NVIDIA devolvió 500 a los 18 minutos
            # de ejecutor y la excepción subía CRUDA hasta el
            # `except Exception` de server.py — donde `status="error"`
            # apaga el guardado del historial y el canal `rescue` solo
            # se lee en la rama de cancel. Resultado medido (chat
            # un run de ejemplo / conversación un hilo de ejemplo): content vacío, tokens
            # NULL, tool_calls NULL y la conversación sin un solo
            # turno: 18 minutos de trabajo tirados y el próximo
            # mensaje arrancando de cero.
            #
            # `httpx.HTTPError` cubre el otro lado de la misma
            # falla: pydantic-ai envuelve los status en
            # ModelHTTPError, pero una conexión reseteada, un DNS
            # caído o un read timeout del transporte salen crudos y
            # perdían el run igual. `_que_hacer` ya los contempla —
            # sin `status_code` devuelve "reintentar".
            #
            # ponytail: una tool que haga HTTP por su cuenta (un MCP
            # remoto) también puede tirar httpx.HTTPError y acá se
            # lee como "se cayó el proveedor". El veredicto queda mal
            # etiquetado, pero el resultado es el correcto igual:
            # reintento y corte reanudable en vez de perder el run.
            # Separarlos pide envolver cada toolset, que es bastante
            # más diff; hacerlo cuando aparezca uno real.
            #
            # Los errores de anyio son el MCP local: cuando el stdio
            # de un server se muere a mitad del run, la sesión tira
            # `ClosedResourceError` y subía cruda, con el mismo daño
            # que describe el párrafo de arriba. Aparecieron 13 runs
            # así en 14 días (14 min de trabajo tirado); el último,
            # `un run de ejemplo` en code-hero-rpg, murió a los 101s con la
            # conversación sin un solo turno. El reintento acá vale
            # la pena aunque la sesión siga muerta: el modelo puede
            # terminar sin volver a tocar ESE server, y si vuelve a
            # caer, el corte ya es reanudable — el próximo turno
            # re-adquiere el MCP por el pool, que lo prueba y lo
            # levanta de nuevo.
            #
            # Rescatamos igual que el corte por presupuesto y volvemos
            # SIN excepción: quién reintenta y quién corta lo decide
            # el round loop con `_que_hacer`.
            state.provider_error = e
            state.last_phase = "provider_error"
            try:
                _msgs = agent_run.all_messages()
                # Si el corte agarró una tool en vuelo, el historial
                # queda con un tool call sin respuesta y el
                # **continúa** muere con UserError. Mismo cierre que
                # en el cancel.
                expert_history._close_orphan_tool_calls(
                    _msgs,
                    reason="el proveedor cortó mientras esta tool corría")
                state.messages_json = expert_history._dump_messages(_msgs)
            except Exception as _e:  # noqa: BLE001 — rescate best-effort
                logger.warning(
                    "no pude rescatar historial parcial tras caída del "
                    "proveedor (%s): %r", getattr(e, "status_code", "?"), _e)
            state.usage = getattr(agent_run, "usage", None)
            if callable(state.usage):
                try:
                    state.usage = state.usage()
                except Exception:  # noqa: BLE001
                    state.usage = None
            return
        finally:
            # Pase lo que pase (normal, UsageLimitExceeded, cancel
            # por watchdog), matamos la task de watchdog para que
            # no quede colgada consumiendo CPU/event-loop slots.
            if not watchdog_task.done():
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except (asyncio.CancelledError, Exception):
                    pass

    # Al salir del async for, agent_run.result es el AgentRunResult
    # completo (output, usage, all_messages). Capturamos TODO acá
    # para no perder referencias (el result se libera al salir del
    # async with, así que dump_json tiene que pasar adentro).
    result = agent_run.result
    state.output_text = str(result.output) if result.output is not None else ""
    state.usage = result.usage
    state.messages_json = expert_history._dump_messages(result.all_messages())
