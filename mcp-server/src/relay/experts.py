"""Entradas públicas del ejecutor; implementación organizada por responsabilidad."""
from pydantic_ai import Agent
from .bitacora import Bitacora, BITACORA_MAX_PASOS
from .progress import RunProgress, make_progress_callback
from .expert_models import (
    ModelUnavailable,
    PONYTAIL_PATH,
    _catalog,
    _ponytail_cache,
    _read_ponytail_sync,
    build_model,
    catalog,
    has_vision,
    load_catalog,
    read_ponytail,
    resolve_model_spec,
    structured_output_settings,
)
from .expert_selection import (
    _CapabilityRequested,
    _MCP_FLAG_RE,
    _SCOPE_FLAG_RE,
    _SKILL_FLAG_RE,
    _catalog_toolsets,
    _one_per_capability,
    parse_mcp_flags,
    parse_scope_flags,
    parse_skill_flags,
    scope_block,
)
from .cbm_runtime import (
    _cbm_session_call,
    _cbm_session_off,
    _cbm_toolset,
    _cbm_toolset_lock,
    _cbm_transport,
    cbm_binary_path,
    cbm_call,
    cbm_cli_call,
    close_cbm_session,
)
from .expert_history import (
    SHELL_RESULT_CAP,
    SHELL_RESULT_KEEP_TAIL,
    THINK_KEEP_FULL,
    TOOL_KEEP_FULL,
    TOOL_RESULT_CAP,
    _CONSOLE_TOOLS,
    _ELIDED_ARGS,
    _ELIDE_KEEP_HEAD,
    _ELIDE_KEEP_TAIL,
    _ELIDE_MARK,
    _ELIDE_MIN_CHARS,
    _ERR_HEAD_MIN,
    _ERR_KEEP_CHARS,
    _ERR_KEEP_LINES,
    _ERR_RE,
    _cap_text,
    _cap_tool_result,
    _caps_for,
    _close_orphan_tool_calls,
    _dump_messages,
    _elide_old_response_parts,
    _elide_old_tool_returns,
    _elide_tail,
    _prompt_con_imagenes,
    _rescatar_errores,
    _slim_history,
    _strip_foreign_thinking,
    _strip_images,
    _strip_instructions,
)
from .expert_toolsets import (
    CappedToolset,
    HideToolsToolset,
    OptionalToolset,
    _BEAT_AFTER_S,
    _TOOL_OVERRUN_GRACE_S,
    _anotar_en_vuelo,
    _think_cap,
    _tool_en_vuelo,
    _watchdog_verdict,
    tool_en_vuelo,
)
from .expert_evidence import (
    _FILE_REF_RE,
    _HEDGE_HINTS,
    _INSTALL_HINTS,
    _MIN_EVIDENCIA_CHARS,
    _evidencia_insuficiente,
    _huele_a_instalacion,
)
from .expert_context import (
    CAP_LADDER,
    _CUT_PHASES,
    _PROVIDER_BACKOFF_S,
    _PROVIDER_RETRIES,
    _distinto_modelo,
    _measure_tool_returns,
    _model_key,
    _synthesize_no_final_text,
    context_usage,
    context_usage_db,
    format_context_note,
    summarize_tool_meter,
)
from .expert_steps import (
    _SECRETOS_RE,
    _STEP_DIFF_MAX_CHARS,
    _STEP_DIFF_MAX_LINES,
    _STEP_OUT_BUDGET,
    _STEP_OUT_MAX,
    _STEP_OUT_TAIL,
    _args_de_part,
    _clasificar_partes,
    _console_tool_outputs,
    _format_tool_step,
    _format_tool_step_crudo,
    _redactar,
    _tool_loop_detected,
    _unified_diff,
)
from .expert_instructions import (
    BATCH_ARTIFACTS_BLOCK,
    BITACORA_BLOCK,
    EVIDENCE_BLOCK,
    TOOL_FALLBACK_BLOCK,
    build_instructions,
)
from .expert_git import (
    DIFF_MAX_BYTES,
    GIT_CAPTURE_TIMEOUT_S,
    _build_git_diff_block_sync,
    _capture_git_diff_sync,
    _git_unavailable_logged,
)
from .expert_runner import (
    run_expert,
)
from .expert_stage_prompts import (
    DOCUMENTER_INSTRUCTIONS,
    PLANNER_INSTRUCTIONS,
    PLANNER_REASONING_RULE,
    RESERVA_CIERRE_S,
    VERIFIER_INSTRUCTIONS,
    _PV_TIMEOUT_S,
)
from .expert_verdicts import (
    _FEEDBACK_RE,
    _FENCE_RE,
    _INTERRUPTION_FENCE_RE,
    _INTERRUPTION_LINE_RE,
    _INTERRUPTION_REQUIRED,
    _NUM_RE,
    _PASOS_LINE_RE,
    _PV_REASONING_TIMEOUT_S,
    _STEP_STATUSES,
    _THINK_BLOCK_RE,
    _THINK_OPEN_RE,
    _TOOLS_SOLO_LECTURA,
    _VALID_REASONS,
    _VERDICT_ALONE_RE,
    _VERDICT_FENCE_RE,
    _VERDICT_KEYS,
    _VERDICT_LINE_RE,
    _VERDICT_PC_KEYS,
    _VERDICT_RE,
    _VERDICT_VALUES,
    _clean_stage_output,
    _extract_plan_correction,
    _parse_executor_interruption,
    _parse_verifier,
    _solo_reviso,
    _stage_usage,
    _validate_plan_correction,
    _validate_verifier_verdict_payload,
    _verdict_match,
)
from .expert_planning import (
    RECAP_MAX_CHARS,
    RECAP_MAX_TURNS,
    _CONTINUACION_MAX_CHARS,
    _CONTINUACION_RE,
    _NUDGES_DEL_HARNESS,
    _PASO_RE,
    _RECAP_PART_CAP,
    _SIGNAL_RE,
    _SIGNAL_SCAN_LINES,
    _TOO_LARGE_PREFIX,
    _TRIVIAL_PREFIX,
    _es_continuacion,
    _format_decomposition,
    _head_tail,
    _history_recap,
    _plan_signal,
    _plan_utilizable,
    _reasoning_toolset,
    _render_tool_calls,
    _stage_timeout,
    pasos_del_plan,
)
from .expert_stages import (
    _DOC_MARCA,
    _DOC_SEP,
    _merge_doc_into_history,
    _run_documenter,
    _run_planner,
    _run_verifier,
)
from .expert_staged_runner import (
    run_expert_staged,
)
from .expert_consult import (
    SUGGESTIONS_MAX,
    SUGGESTIONS_SYSTEM,
    SUGGESTION_CONTEXT_CHARS,
    SUGGESTION_MAX_CHARS,
    _SUGGESTION_DECOR_RE,
    _summarize_tool_calls_from_messages,
    parse_suggestions,
    run_consult,
    suggest_followups,
)
from .expert_iteration import (
    run_iteration,
)
from .expert_run_state import (
    ExpertRunState,
)
from .expert_context_tools import (
    build_context_tools,
)

run_expert_3stage = run_expert_staged
