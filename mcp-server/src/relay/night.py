"""Modo Nocturno — fachada pública del pipeline ADR-028.

La API histórica (`relay.night`) queda estable; la implementación vive en
módulos cohesivos de tipos, planificación, ejecución, reporte y orquestación.
"""
from __future__ import annotations

from . import config
from .experts import (
    build_model, cbm_binary_path, cbm_call, structured_output_settings,
)
from .night_generator import (
    NIGHT_WORK_CONTRACT, TaskGenerator, _norm_path,
    _summarize_block_diff,
    extract_directive_points, find_missing_points, write_plan_files,
    _verdict_icon,
)
from .night_orchestrator import NightOrchestrator
from .night_plan import (
    PLAN_INSTRUCTIONS, NightConfig, _slugify, autodetect_cmds, parse_plan, render_plan,
    resolve_cwd_for_cmd, run_branch_name, run_gate, verify_repo,
    _gate_excerpt,
)
from .night_report import MorningReporter, default_deadline
from .night_types import (
    CBM_INDEX_LIMIT, COMMIT_TRAILER, DEFAULT_DEADLINE_HOUR,
    EXPERT_TIMEOUT_S, FORBIDDEN_PATTERNS, GATE_TIMEOUT_S, GIT_TIMEOUT_S,
    MIN_TASK_WINDOW_S, PLAN_DIRNAME, PLAN_FILENAME, PLAN_PROMPT_FILES_CAP,
    PLAN_TIMEOUT_S, TRANSIENT_BUILD_SIGNATURES,
    BlockDecision, BlockQuestionOption, NightTask, PlanDraft, PlanDrafts,
    TaskResult, _CHAR_TO_STATE, _STATE_TO_CHAR, _TASK_RE,
)
from .night_worker import BranchWorker
