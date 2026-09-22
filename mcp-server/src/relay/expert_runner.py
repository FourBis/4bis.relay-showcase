"""Preparación y continuación de una corrida del experto."""
from __future__ import annotations
import asyncio
import logging
import time
from collections import deque
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.toolsets import FunctionToolset
from typing import Any, Iterable, Optional
from . import (
    config,
    expert_context,
    expert_context_tools,
    expert_history,
    expert_instructions,
    expert_iteration,
    expert_models,
    expert_result,
    expert_run_state,
    expert_selection,
    expert_steps,
    expert_toolsets,
    file_tools as file_tools_mod,
    shell_tools as shell_tools_mod,
)
from .bitacora import Bitacora
from .planificador import _que_hacer as _que_hacer_con_el_proveedor
from .task_workspace import resolved_project

logger = logging.getLogger("relay.experts")


async def run_expert(
    project: dict, user: str, *, skills_block: str = "",
    system_extra: str = "", model_override: str = "",
    db: Any = None, message_history_json: str = "",
    on_progress: Any = None,
    steer: Optional[list[str]] = None, rescue: Optional[dict] = None,
    mcp_with: Optional[list[str]] = None, mcp_pool: Any = None,
    images: Optional[list[tuple[bytes, str]]] = None,
    chat_id: str = "", conversation_id: str = "",
    archivos_reservados: Optional[Iterable[str]] = None,
    on_leg_boundary: Any = None,
    bitacora_json: str = "",
    deadline_pedido: float = 0.0,
    image_artifacts: Optional[dict] = None,
) -> dict:
    """Ejecuta un turno con herramientas, historial y recuperación de cortes.

    ``steer`` y ``rescue`` son canales mutables compartidos con el caller.
    ``on_leg_boundary`` supervisa cada tanda; el deadline limita la corrida.
    Devuelve contenido, uso, historial y evidencia para persistir el resultado.
    """
    if db is not None and conversation_id and not project.get("_task_id"):
        project = await resolved_project(db, project, conversation_id)
    state = expert_run_state.ExpertRunState(user=user, images=images, steer=steer, rescue=rescue)
    state.t0 = time.monotonic()
    defaults = project.get("defaults_json") or {}
    from .execution_policy import ExecutionPolicy
    policy = ExecutionPolicy.for_run(defaults, archivos_reservados)
    image_artifacts = image_artifacts if image_artifacts is not None else {}
    if state.rescue is not None:
        state.rescue["image_artifacts"] = image_artifacts
    state.spec = expert_models.resolve_model_spec(model_override, project)
    model = expert_models.build_model(state.spec)
    _global_to = None
    if db is not None:
        try:
            _global_to = await db.get_config("expert_timeout_s")
        except Exception:
            _global_to = None
    timeout = float(
        defaults.get("timeout")
        or (_global_to if _global_to else None)
        or config.expert_timeout_s()
    )
    tool_timeout = config.tool_timeout_s()
    state._idle_to = defaults.get("idle_timeout_s") or config.expert_idle_timeout_s()
    state._think_to = expert_toolsets._think_cap(defaults, state._idle_to)
    state._mcp_tool_to = float(
        defaults.get("tool_call_timeout_s") or config.tool_call_timeout_s())

    selection: set[str] = {
        s.strip().lower() for s in (mcp_with or []) if s.strip()}
    use_catalog = (
        db is not None and project.get("id") is not None and state.spec != "test")
    visible_mcps: list[dict] = []
    attached_mcps: list[dict] = []
    state._inflight: dict = {}
    # El proyecto de notas no necesita herramientas del repositorio.
    _is_notes = (project.get("slug") or "").lower() == "notes"
    if _is_notes or state.spec == "test" or not policy.unrestricted_tools:
        toolsets = []
        attached_mcps = []
        visible_mcps = []
    elif use_catalog:
        _hide: set[str] = set()
        if defaults.get("native_shell", True):
            _hide.add("run_shell")
        if defaults.get("native_files", True):
            _hide |= {"read_file", "write_file", "edit_file", "move_file",
                      "list_dir", "directory_tree"}
        _hide = frozenset(_hide)
        toolsets, attached_mcps, visible_mcps = await expert_selection._catalog_toolsets(
            db, project, selection, mcp_pool, state._mcp_tool_to, state._inflight, _hide,
            image_artifacts, expert_models.has_vision(state.spec))
    else:
        toolsets = []
        attached_mcps = []
        visible_mcps = []

    ponytail = await expert_models.read_ponytail()
    instructions = expert_instructions.build_instructions(project, ponytail, skills_block)
    if not policy.unrestricted_tools:
        instructions += (
            "\n\nEste run tiene permisos restringidos. Shell y MCP externos no están "
            "disponibles: usa las herramientas nativas de archivos y SQL. "
            "No intentes eludir restricciones cambiando de herramienta.")
    if _is_notes:
        pony = ponytail or ""
        sysp = project.get("system_prompt") or ""
        instructions = "\n\n".join(p for p in (pony, sysp) if p)
    if db is not None and not _is_notes and defaults.get("facts_always_on"):
        try:
            from . import memory as memory_mod
            _facts = await db.list_facts(
                project["slug"], limit=200, status="approved")
            _fb = memory_mod.build_facts_block(_facts)
            if _fb:
                instructions = f"{instructions}\n\n{_fb}"
        except Exception as e:  # noqa: BLE001 — la memoria nunca mata un run
            logger.warning("facts always-on: no pude leerlos (%r)", e)

    if system_extra:
        instructions = f"{instructions}\n\n{system_extra}" if instructions else system_extra

    _attached_terms = set()
    for m in attached_mcps:
        _attached_terms.add(m["name"].lower())
        _attached_terms.add(m["capability"].lower())
    _pending = [
        m for m in visible_mcps
        if m["on_demand"] and m["name"].lower() not in _attached_terms
    ]
    if _pending:
        _menu = ", ".join(sorted(
            f"{m['name']} ({m['capability']})" for m in _pending))
        instructions = (
            f"{instructions}\n\n"
            f"## Capacidades externas disponibles (on-demand)\n"
            f"Si el pedido del usuario requiere una capacidad que no esta "
            f"cubierta por tus tools (browser, DB, github, etc.), invoca "
            f"`use_capability(name)` y el run se reinicia con esa toolset "
            f"adjunta. Disponibles ahora: {_menu}.")
    elif use_catalog and visible_mcps and all(
            m["name"].lower() in _attached_terms for m in visible_mcps):
        _menu = ", ".join(sorted(
            f"{m['name']} ({m['capability']})" for m in visible_mcps))
        instructions = (
            f"{instructions}\n\n"
            f"## Capacidades externas (adjuntas en este run)\n"
            f"Ya tienes estas capabilities activas: {_menu}.")

    if "browser" in _attached_terms:
        instructions = f"{instructions}\n\n{expert_instructions.EVIDENCE_BLOCK}"

    tools: list[Any] = []

    bitacora = Bitacora.cargar(bitacora_json) if bitacora_json else Bitacora()
    if bitacora.hechos or bitacora.comandos:
        logger.info("bitácora retomada: %d hechos, %d comandos",
                    len(bitacora.hechos), len(bitacora.comandos))

    _native_shell = (defaults.get("native_shell", True) and not _is_notes
                     and policy.unrestricted_tools)
    if _native_shell:
        tools += shell_tools_mod.shell_tools(
            repo=project.get("repo_path") or "",
            techo_s=float(state._mcp_tool_to), bitacora=bitacora,
            en_vuelo=lambda n: expert_toolsets._tool_en_vuelo(state._inflight, n))

    _native_files = defaults.get("native_files", True) and not _is_notes
    _perm = None
    if project.get("repo_path") and not _is_notes:
        _perm, _abierto, _por_que = file_tools_mod.permisos_del_run(
            project, defaults, conversation_id=conversation_id,
            reservadas=archivos_reservados or ())
        if _abierto:
            logger.warning(
                "sandbox de archivos APAGADO para %s (%s): las tools de "
                "archivo llegan a todo el disco. read_only y rutas_vedadas "
                "siguen aplicando.", project.get("slug") or "?", _por_que)
        if _native_files:
            tools += file_tools_mod.file_tools(_perm)

    if db is not None and defaults.get("sql_tools", True) and not _is_notes:
        from .sql_tools import sql_tools
        tools.extend(sql_tools(db, policy, perm=_perm))

    state.progress_events: list[dict[str, Any]] = []
    progress_sink = on_progress

    async def _emit_progress(**fields) -> None:
        event = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **{k: v for k, v in fields.items() if v is not None},
        }
        state.progress_events.append(event)
        if progress_sink is None:
            return
        try:
            await progress_sink(**fields)
        except Exception as e:  # noqa: BLE001 — el callback es best-effort
            logger.warning("on_progress callback falló: %r", e)

    context_tools, _q_state = expert_context_tools.build_context_tools(
        project=project, db=db, chat_id=chat_id, conversation_id=conversation_id,
        _is_notes=_is_notes, bitacora=bitacora, _emit_progress=_emit_progress)
    tools.extend(context_tools)

    def _build_agent(extra_tools: list[Any]) -> Agent:
        all_tools = tools + extra_tools
        native = [
            expert_toolsets.CappedToolset(wrapped=FunctionToolset(
                all_tools, max_retries=3,
                timeout=state._mcp_tool_to + expert_toolsets._TOOL_OVERRUN_GRACE_S),
                image_artifacts=image_artifacts, vision=expert_models.has_vision(state.spec))
        ] if all_tools else []
        # ponytail: tres reintentos; recalibrar si cambia el proveedor.
        return Agent(
            model,
            instructions=[instructions, bitacora.render],
            toolsets=native + toolsets,
            tool_timeout=tool_timeout,
            retries=3,
        )


    state.message_history = None
    if message_history_json:
        try:
            state.message_history = ModelMessagesTypeAdapter.validate_json(
                message_history_json)
            n_before = len(state.message_history)
            state.message_history = expert_history._slim_history(list(state.message_history))
            if len(state.message_history) != n_before:
                logger.info(
                    "historial adelgazado: %d → %d mensajes",
                    n_before, len(state.message_history))
            expert_history._strip_foreign_thinking(state.message_history, state.spec)
            expert_history._close_orphan_tool_calls(
                state.message_history,
                reason="el turno anterior se cortó con esta tool en vuelo")
        except Exception as e:  # noqa: BLE001 — pydantic ValidationError y afines
            logger.warning(
                "message_history corrupto (%r): corro sin historial", e)

    await _emit_progress(phase="thinking", tool=None)

    state.output_text = ""
    state.usage = None
    state.tool_calls_count = 0
    state.out_budget = expert_steps._STEP_OUT_BUDGET
    state.last_phase = "thinking"
    state.last_tool_name: str | None = None
    state.messages_json = ""
    state.budget_exceeded = False
    state.provider_error: BaseException | None = None
    provider_retries = 0
    state.request_limit = min(config.expert_request_limit(), int(defaults.get("task_request_limit") or config.expert_request_limit()))
    if defaults.get("task_token_limit"):
        from pydantic_ai.usage import RunUsage
        state.task_token_limit = int(defaults["task_token_limit"])
        state.task_usage = RunUsage()
    state.recent_tool_calls: deque[str] = deque(maxlen=8)
    state.tool_meter: dict[str, dict[str, int]] = {}
    state.meter_turn = 0
    state.steer_text = ""
    steers = 0
    steer_texts: list[str] = []

    deadline = state.t0 + timeout
    if deadline_pedido:
        deadline = min(deadline, deadline_pedido)
    max_rounds = 3
    ROUND_MIN_S = 30.0
    max_legs = int(defaults.get("max_legs") or config.expert_max_legs())
    max_legs_hard = int(
        defaults.get("max_legs_hard") or config.expert_max_legs_hard())
    if max_legs_hard < max_legs:
        max_legs_hard = max_legs
    max_tool_calls = config.expert_max_tool_calls()
    legs = 1
    cap_rounds = 0
    state.tokens_in_prev = 0   # usage acumulado de tandas anteriores
    state.tokens_out_prev = 0
    state.cache_prev = 0       # 2026-08-31: cache reads de tandas anteriores
    while True:
        remaining = deadline - time.monotonic()
        if (cap_rounds or legs > 1 or steers) and remaining < ROUND_MIN_S:
            logger.warning(
                "run_expert: abortando, %.1fs restantes < %.0fs piso",
                remaining, ROUND_MIN_S)
            break
        extra: list[Any] = []
        if use_catalog and cap_rounds < max_rounds - 1:
            uc = expert_selection.make_use_capability(attached_mcps, visible_mcps)
            if uc is not None:
                extra.append(uc)
        agent = _build_agent(extra)
        try:
            await asyncio.wait_for(
                expert_iteration.run_iteration(agent, state, _emit_progress),
                timeout=max(1.0, deadline - time.monotonic()))
        except expert_selection._CapabilityRequested as e:
            logger.info("use_capability(%r): re-run con el toolset adjunto",
                        e.name)
            cap_rounds += 1
            selection.add(e.name)
            if state.usage:
                state.tokens_in_prev += state.usage.input_tokens or 0
                state.tokens_out_prev += state.usage.output_tokens or 0
                state.cache_prev += state.usage.cache_read_tokens or 0
                state.usage = None
            toolsets, attached_mcps, visible_mcps = await expert_selection._catalog_toolsets(
                db, project, selection, mcp_pool, state._mcp_tool_to, state._inflight, _hide,
                image_artifacts, expert_models.has_vision(state.spec))
            if any(m["capability"] == "browser" for m in attached_mcps) \
                    and expert_instructions.EVIDENCE_BLOCK not in instructions:
                instructions += f"\n\n{expert_instructions.EVIDENCE_BLOCK}"
            state.user = ("Continúa desde las herramientas ya ejecutadas. Revisa las "
                    "capacidades disponibles ahora; no repitas efectos ya realizados.")
            state.images = None  # ya están en message_history
            await _emit_progress(phase="thinking", tool=None)
            continue
        except asyncio.CancelledError:
            if state.last_phase == "idle_timeout":
                _t = asyncio.current_task()
                if _t is not None:
                    _t.uncancel()
                break
            raise  # cancel real (usuario /experts/cancel o shutdown)
        except asyncio.TimeoutError:
            state.last_phase = "hard_timeout"
            state.output_text = (
                f"⚠️ La tarea superó el tope global de {timeout:.0f}s de "
                "trabajo continuo (venía progresando, no colgada). Guardé "
                "lo avanzado: manda **continúa** para que siga desde acá, "
                "o sube `expert_timeout_s` para tareas largas.")
            logger.warning(
                "run_expert: hard_timeout tras %.0fs (tool=%s, "
                "tool_calls=%d, historial rescatado=%s)",
                timeout, state.last_tool_name, state.tool_calls_count,
                bool(state.messages_json))
            break
        if state.last_phase == "steered" and state.steer_text:
            if not state.messages_json:
                logger.warning("steer: sin historial rescatado — corto acá")
                break
            try:
                state.message_history = ModelMessagesTypeAdapter.validate_json(
                    state.messages_json)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "steer: historial rescatado inválido (%r) — corto acá", e)
                break
            if state.usage:
                state.tokens_in_prev += state.usage.input_tokens or 0
                state.tokens_out_prev += state.usage.output_tokens or 0
                state.cache_prev += state.usage.cache_read_tokens or 0
            state.user = state.steer_text
            steer_texts.append(state.steer_text)
            state.steer_text = ""
            steers += 1
            state.last_phase = "thinking"
            continue
        if state.provider_error is not None:
            _err, state.provider_error = state.provider_error, None
            _sig = getattr(_err, "status_code", None) or type(_err).__name__
            if (_que_hacer_con_el_proveedor(_err) == "reintentar"
                    and provider_retries < expert_context._PROVIDER_RETRIES
                    and (deadline - time.monotonic()) >= ROUND_MIN_S):
                provider_retries += 1
                if state.usage:
                    state.tokens_in_prev += state.usage.input_tokens or 0
                    state.tokens_out_prev += state.usage.output_tokens or 0
                    state.cache_prev += state.usage.cache_read_tokens or 0
                if state.tool_calls_count and state.messages_json:
                    try:
                        state.message_history = (
                            ModelMessagesTypeAdapter.validate_json(
                                state.messages_json))
                        state.user = (
                            "continúa con la tarea desde donde quedaste; si "
                            "ya está completa, responde con el resumen final")
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "provider_error: historial rescatado inválido "
                            "(%r) — reintento sin él", e)
                state.last_phase = "thinking"
                state.progress_events.append(
                    {"phase": "provider_retry", "attempt": provider_retries})
                logger.warning(
                    "run_expert: el proveedor cortó (%s) tras %d tools — "
                    "reintento %d/%d sobre el historial rescatado",
                    _sig, state.tool_calls_count, provider_retries,
                    expert_context._PROVIDER_RETRIES)
                await _emit_progress(phase="heartbeat", tool=None)
                await asyncio.sleep(expert_context._PROVIDER_BACKOFF_S)
                continue
            state.last_phase = "provider_error"
            state.output_text = (
                f"⚠️ El proveedor del modelo cortó el run ({_sig}) y no se "
                "pudo retomar. Guardé lo avanzado: manda **continúa** para "
                "que siga desde acá, o cambia de modelo si el endpoint "
                "sigue caído."
            )
            logger.warning(
                "run_expert: corte por caída del proveedor (%s) tras %d "
                "tools y %d reintento(s); historial rescatado=%s",
                _sig, state.tool_calls_count, provider_retries, bool(state.messages_json))
            break
        if state.budget_exceeded:
            loop_hit = expert_steps._tool_loop_detected(state.recent_tool_calls)
            has_time = (deadline - time.monotonic()) >= ROUND_MIN_S
            off_plan_feedback = ""
            if (on_leg_boundary is not None and state.messages_json
                    and not loop_hit and has_time):
                try:
                    off_plan_feedback = await on_leg_boundary({
                        "content": state.output_text,
                        "phase_at_end": state.last_phase,
                        "messages_json": state.messages_json,
                        "tool_calls": state.tool_calls_count,
                        "leg": legs,
                    }) or ""
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "run_expert: on_leg_boundary rompió (%r) — sigo sin "
                        "veredicto", e)
                    off_plan_feedback = ""
            if off_plan_feedback:
                state.last_phase = "off_plan"
                logger.warning(
                    "run_expert: corte por off_plan en la tanda %d "
                    "(tools=%d): %s", legs, state.tool_calls_count,
                    off_plan_feedback[:160])
                state.output_text += (
                    f"\n\n🧭 **Me desvié del plan y me detuve en el paso "
                    f"{state.tool_calls_count}.** El verificador dice: "
                    f"_{off_plan_feedback}_\n\nLo hecho está guardado. "
                    "Corrígeme el rumbo en un mensaje (o escribe "
                    "**continúa** si querías que siguiera por acá).")
                break
            if on_leg_boundary is None and state.tool_calls_count >= max_tool_calls:
                state.last_phase = "budget_split"
                logger.warning(
                    "run_expert: corte por budget_split (tools=%d >= "
                    "%d) en la tanda %d — tope de subdivisión "
                    "alcanzado", state.tool_calls_count, max_tool_calls, legs)
                state.output_text += (
                    f"\n\n✂️ **Paré en el paso {state.tool_calls_count} "
                    f"(tanda {legs}).** A esta altura el 85% de los "
                    "nodos no cierran — esta tarea es más grande de lo "
                    "que entra en un nodo. Lo hecho está guardado. "
                    "Convendría partirla en lotes (ej: \"lote 1\", "
                    "\"lote 2\"), que es como sí funcionaron los nodos "
                    "hermanos.")
                break
            techo = max_legs_hard if on_leg_boundary is not None else max_legs
            if legs < techo and state.messages_json and not loop_hit and has_time:
                try:
                    state.message_history = ModelMessagesTypeAdapter.validate_json(
                        state.messages_json)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "auto-continue: historial rescatado inválido (%r) — "
                        "corto acá", e)
                    break
                if state.usage:
                    state.tokens_in_prev += state.usage.input_tokens or 0
                    state.tokens_out_prev += state.usage.output_tokens or 0
                    state.cache_prev += state.usage.cache_read_tokens or 0
                legs += 1
                state.progress_events.append(
                    {"phase": "auto_continue", "leg": legs})
                state.budget_exceeded = False
                state.user = (
                    "continúa con la tarea desde donde quedaste; si ya "
                    "está completa, responde con el resumen final")
                logger.info(
                    "run_expert: budget auto-continue → tanda %d/%d "
                    "(tools acumuladas=%d, supervisada=%s)", legs, techo,
                    state.tool_calls_count, on_leg_boundary is not None)
                await _emit_progress(phase="heartbeat", tool=None)
                continue
            if legs > 1:
                state.output_text += (
                    f" (Ya se auto-extendió {legs} tandas: "
                    f"{legs * state.request_limit} pasos totales.)")
            if loop_hit:
                state.output_text += (
                    " Corté porque venía repitiendo exactamente la misma "
                    "tool con los mismos args (loop, no progreso).")
        break
    return expert_result.finish_run(
        state, image_artifacts=image_artifacts, bitacora=bitacora, legs=legs,
        cap_rounds=cap_rounds, provider_retries=provider_retries, steers=steers,
        steer_texts=steer_texts, _q_state=_q_state)
