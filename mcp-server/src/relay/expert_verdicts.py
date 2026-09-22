"""Interpretación y validación de planes, interrupciones y veredictos."""
from __future__ import annotations
import json
import re
from typing import Any, Optional


_TOOLS_SOLO_LECTURA = frozenset({
    "read_file", "list_dir", "search_files", "rutas_habilitadas",
    "git_diff", "read_skill", "cbm_query", "db_connections",
    "anotar", "plan_step_done",
})


def _solo_reviso(tool_calls_summary) -> bool:
    """¿El run solo miro? `False` si no llamo a nada (no hay revision)."""
    llamadas = list(tool_calls_summary or [])
    if not llamadas:
        return False
    return all((t[0] if isinstance(t, (tuple, list)) else t)
               in _TOOLS_SOLO_LECTURA for t in llamadas)
# Techo del planificador CUANDO tiene el razonador enchufado. Con
# sequential-thinking el turno son varias vueltas de pensamiento antes de
# escribir el plan, y en 60s no entra. Sigue siendo un techo duro: si el
# razonador se va por las ramas, la etapa corta y el ejecutor arranca sin
# plan, que es el mismo degradado de siempre.
_PV_REASONING_TIMEOUT_S = 180.0

# Parsers tolerantes: los modelos a veces meten markdown, prefijos
# extra, o capitalización distinta. Si no matchea, default conservador.
_VERDICT_RE = re.compile(
    r"\b(?P<v>needs_more|off_plan|needs_human)\b", re.IGNORECASE)
_FEEDBACK_RE = re.compile(
    r"FEEDBACK\s*:\s*(?P<f>.+?)(?:\n|$)", re.IGNORECASE | re.DOTALL)
# Línea `VERDICT: x` (o `VEREDICTO:`, o con markdown alrededor). Ancla al
# principio de línea: es el contrato que pide VERIFIER_INSTRUCTIONS, y a
# diferencia de `_VERDICT_RE` no la confunde una mención en prosa.
_VERDICT_LINE_RE = re.compile(
    r"^[\s>*_#`-]*(?:VERDICT|VEREDICTO)\s*[:=]\s*[*_`\"']*"
    r"(?P<v>complete|needs_more|off_plan|needs_human)\b",
    re.IGNORECASE | re.MULTILINE)
# Una línea que es SOLO el veredicto (el modelo respetó el vocabulario
# pero se saltó la etiqueta).
_VERDICT_ALONE_RE = re.compile(
    r"^[\s>*_#`-]*(?P<v>complete|needs_more|off_plan|needs_human)[\s*_`.]*$",
    re.IGNORECASE | re.MULTILINE)
# `PASOS: 1,3,5` o `PASOS: 1 3 5` o `PASOS: ` (lista vacía).
# Lista de ints positivos; tolerante a espacios y separadores.
_PASOS_LINE_RE = re.compile(
    r"^[\s>*_#`-]*PASOS\s*[:=]\s*[*_`\"']*(?P<nums>[0-9,\s]*)\s*$",
    re.IGNORECASE | re.MULTILINE)
_NUM_RE = re.compile(r"\d+")

# ---------- saneamiento de la salida de las etapas (2026-08-15) ----------
#
# Las tres etapas auxiliares corren en otro proveedor que el ejecutor
# (iter 11 + provider nvidia del 2026-08-14). Los contratos de texto
# —`VERDICT:`, `TRIVIAL:`, `DEMASIADO_GRANDE:`— se afinaron contra
# MiniMax, que contesta pelado. Los modelos de razonamiento del catálogo
# NIM anteponen su cadena de pensamiento en el MISMO campo `content`
# cuando el endpoint no la separa en `reasoning_content`, así que el
# contrato se rompe por un prefijo, no por el contenido.
#
# Este bloque es el adaptador: saca el andamiaje y deja el texto que el
# parser espera. Barato, idempotente y sin red — si el modelo ya
# contestaba limpio, no cambia nada.

_THINK_BLOCK_RE = re.compile(
    r"<(think|thinking|reasoning)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
# Bloque de razonamiento que quedó ABIERTO (el modelo cortó por límite de
# tokens o el endpoint truncó): todo lo anterior al cierre que nunca
# llegó es andamiaje. Solo se aplica si NO hay cierre en el texto.
_THINK_OPEN_RE = re.compile(
    r"<(think|thinking|reasoning)\b[^>]*>", re.IGNORECASE)
_FENCE_RE = re.compile(
    r"^\s*```[a-zA-Z0-9_-]*\s*\n(?P<body>.*?)\n\s*```\s*$", re.DOTALL)


def _clean_stage_output(text: str) -> str:
    """Salida cruda de una etapa auxiliar → texto parseable.

    Quita, en este orden:
      1. bloques `<think>…</think>` (y variantes `<thinking>`,
         `<reasoning>`) que algunos endpoints NIM devuelven inline;
      2. un bloque de razonamiento sin cerrar — se descarta todo lo
         anterior a la etiqueta de apertura huérfana;
      3. un fence markdown que envuelva TODA la respuesta.

    No toca nada más: el texto de las etapas es el producto (el plan que
    ve el ejecutor, el registro que ve el humano), así que este helper
    solo saca andamiaje, nunca reescribe contenido.
    """
    s = (text or "").strip()
    if not s:
        return ""
    s = _THINK_BLOCK_RE.sub("", s).strip()
    # Apertura huérfana: el modelo abrió el razonamiento y nunca cerró.
    # Nos quedamos con lo que venga DESPUÉS de la última apertura; si no
    # hay nada después, el texto entero era razonamiento y no hay salida.
    if "</" not in s:
        opens = list(_THINK_OPEN_RE.finditer(s))
        if opens:
            s = s[opens[-1].end():].strip()
    m = _FENCE_RE.match(s)
    if m:
        s = m.group("body").strip()
    return s


def _stage_usage(result: Any) -> dict:
    """Tokens de un turno de etapa → `{"tokens_in": N, "tokens_out": N}`.

    Devuelve `{}` cuando no se pudo leer el usage. El dict vacío NO es lo
    mismo que cero: significa "no se sabe" (la etapa falló, o el provider
    no reportó), y las métricas lo tienen que distinguir de una etapa que
    corrió y consumió poco.

    `usage` es propiedad en pydantic-ai 2.x y era método antes; se
    soportan las dos formas para no atarse a la versión.
    """
    try:
        u = result.usage() if callable(result.usage) else result.usage
        return {"tokens_in": int(u.input_tokens or 0),
                "tokens_out": int(u.output_tokens or 0)}
    except Exception:  # noqa: BLE001 — medir nunca puede romper el run
        return {}


# ---------- corrección dinámica del Verificador → Planificador/Ejecutor (t5) ---
#
# Convención de marker (ver `schemas/verifier_verdict.json`,
# `x-marker-convention.fenced_block`):
#   bloque fenced cuyo fence de apertura lleva el token
#   `VERIFIER_VERDICT`, p.ej.
#       ```VERIFIER_VERDICT
#       {"verdict": "needs_more", "feedback": "...",
#        "steps_completed": [1, 2],
#        "plan_correction": {...}}
#       ```
#
# Solo se emite cuando `verdict != complete` (el path `complete` no
# requiere corrección → el marker queda omitido y el texto plano
# `VERDICT:/FEEDBACK:/PASOS:` sigue siendo el contrato para el
# Documentador y para el chat del humano). Cuando está, el objeto
# quepa en `schemas/verifier_verdict.json`; `schemas/_check.py` valida
# los ejemplos canónicos.
#
# Validación best-effort: este helper NO carga jsonschema para no
# sumar dependencia runtime en el relay. Comprueba a mano las claves
# obligatorias y los enums del schema; cualquier inconsistencia cae
# a `None` y se registra en `usage["plan_correction_error"]` para
# que t6 decida si reintenta. La validación completa vive en
# `schemas/_check.py`, fuera del hot path de la API.

_VERDICT_FENCE_RE = re.compile(
    r"```VERIFIER_VERDICT\s*\n(?P<body>.*?)\n\s*```",
    re.DOTALL | re.IGNORECASE)
# Enum estricto del schema: complete | needs_more | off_plan.
_VERDICT_VALUES = {"complete", "needs_more", "off_plan"}
# Claves top-level obligatorias del schema.
_VERDICT_KEYS = (
    "verdict", "feedback", "steps_completed", "plan_correction")
# Cuando verdict != complete, el schema obliga a plan_correction con
# feedback_to_executor / revised_steps / remove_step_ids / add_step_ids.
_VERDICT_PC_KEYS = (
    "feedback_to_executor", "revised_steps",
    "remove_step_ids", "add_step_ids")
# Status del step en revised_steps.
_STEP_STATUSES = {"pending", "in_progress", "done", "failed"}


def _validate_verifier_verdict_payload(payload: dict) -> Optional[str]:
    """Valida el payload completo de `VERIFIER_VERDICT` → mensaje de error o None.

    Best-effort contra `schemas/verifier_verdict.json`. Devuelve el motivo de
    rechazo (string) o None si todo cuadra.
    """
    for k in _VERDICT_KEYS:
        if k not in payload:
            return f"falta clave top-level: {k!r}"
    if payload["verdict"] not in _VERDICT_VALUES:
        return f"verdict invalido: {payload['verdict']!r}"
    if not isinstance(payload["feedback"], str):
        return "feedback no es string"
    sc = payload["steps_completed"]
    if (not isinstance(sc, list)
            or not all(isinstance(x, (int, str)) for x in sc)):
        return "steps_completed no es lista de enteros o strings"
    pc = payload.get("plan_correction")
    if payload["verdict"] != "complete":
        if not isinstance(pc, dict):
            return "plan_correction obligatorio cuando verdict != complete"
        return _validate_plan_correction(pc)
    return None


def _validate_plan_correction(pc: dict) -> Optional[str]:
    """Valida `plan_correction` → mensaje de error o None."""
    fbe = pc.get("feedback_to_executor")
    if not isinstance(fbe, str) or not fbe.strip():
        return "plan_correction.feedback_to_executor vacio o no es string"
    rs = pc.get("revised_steps")
    if not isinstance(rs, list) or not rs:
        return "plan_correction.revised_steps vacio o no es lista"
    for i, s in enumerate(rs):
        if not isinstance(s, dict):
            return f"plan_correction.revised_steps[{i}] no es objeto"
        if not all(k in s for k in ("id", "description", "expected_output", "status")):
            return (f"plan_correction.revised_steps[{i}] le faltan claves")
        if s["status"] not in _STEP_STATUSES:
            return (f"plan_correction.revised_steps[{i}].status invalido: "
                    f"{s['status']!r}")
    for k in ("remove_step_ids", "add_step_ids"):
        v = pc.get(k)
        if (not isinstance(v, list)
                or not all(isinstance(x, str) for x in v)):
            return f"plan_correction.{k} no es lista de strings"
    return None


def _extract_plan_correction(
        text: str, verdict_hint: Optional[str] = None) -> Optional[dict]:
    """Salida del Verificador → payload `plan_correction` parseado, o None.

    Busca el bloque fenced `\\`\\`\\`VERIFIER_VERDICT ... \\`\\`\\`` con el
    objeto veredicto extendido de `schemas/verifier_verdict.json` y
    devuelve solo el sub-objeto `plan_correction` validado, listo para
    que t6 lo re-inyecte al Ejecutor.

    Devuelve `None` cuando:
      - no hay marker (camino `complete`, texto vacío, o el LLM omitió
        el bloque por error);
      - el JSON no parsea;
      - el payload pasa `_parse_verifier` y `_validate_verifier_verdict_payload`
        pero el `verdict_hint` (si viene) discrepa;
      - cualquier clave obligatoria del schema falta o es del tipo
        incorrecto.

    Es deliberadamente silenciosa: cualquier inconsistencia se loggea
    en `usage["plan_correction_error"]` desde el call-site, no acá —
    el parser no debe tirar excepciones para no romper el lazo del
    runner cuando el LLM emite un bloque malformado.
    """
    if not text:
        return None
    m = _VERDICT_FENCE_RE.search(text)
    if not m:
        return None
    try:
        payload = json.loads(m.group("body").strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    err = _validate_verifier_verdict_payload(payload)
    if err is not None:
        return None
    if verdict_hint is not None and payload["verdict"] != verdict_hint:
        return None
    return payload["plan_correction"]


# ---------- interrupción del Ejecutor → Planificador (Etapa B, t2) ----------
#
# Convención de marker (ver `schemas/executor_interruption.json`):
#   * canónica — bloque fenced cuyo fence de apertura lleva el token
#     EXECUTOR_INTERRUPTION, p.ej.
#         ```EXECUTOR_INTERRUPTION
#         {"reason": "compile_fail", ...}
#         ```
#   * fallback — una sola línea que arranca con el marcador y trae el
#     JSON al lado:
#         <<INTERRUPT:EXECUTOR>>{"reason": "compile_fail", ...}
#
# El Ejecutor está obligado a emitir la canónica (lo pide el prompt).
# El fallback existe para el caso degradado en que el Ejecutor no puede
# cerrar el fence (límite de tokens truncó la salida, log scrapeado, etc.)
# — `schemas/_selfcheck.py` valida que ambos lleguen al mismo payload.

_INTERRUPTION_FENCE_RE = re.compile(
    r"```EXECUTOR_INTERRUPTION\s*\n(?P<body>.*?)\n\s*```",
    re.DOTALL | re.IGNORECASE)
# El fallback del schema es estricto (`^<<INTERRUPT:EXECUTOR>>\\s*({.*})`),
# pero el texto real puede traer ruido antes/después (markdown, prefijos
# del modelo). Se busca el ancla en cualquier posición y se captura el
# primer {...} balanceado que viene después.
_INTERRUPTION_LINE_RE = re.compile(
    r"<<INTERRUPT:EXECUTOR>>\s*(\{.*\})\s*$", re.IGNORECASE)
# Campos requeridos por el schema. La validación es best-effort — un JSON
# que parsea pero le falta una clave cae a None, igual que si no hubiera
# señal, para no romper el lazo del Ejecutor.
_INTERRUPTION_REQUIRED = (
    "reason", "affected_step_id", "error_context",
    "partial_progress", "timestamp")
_VALID_REASONS = {
    "compile_fail", "type_mismatch", "file_read_error",
    "wrong_assumption", "other"}


def _parse_executor_interruption(output: str) -> Optional[dict]:
    """Salida del Ejecutor → señal de interrupción parseada, o None.

    Busca primero el bloque fenced canónico, después el fallback de una
    línea, y devuelve el dict si cumple los 5 campos requeridos con
    tipos básicos razonables. NO valida el schema completo (eso es
    trabajo de `schemas/_selfcheck.py`); este helper es el adaptador
    barato que el lazo del runner necesita para decidir si aborta la
    etapa actual.

    Devuelve `None` cuando:
      - no hay marker,
      - hay marker pero el cuerpo no parsea como JSON,
      - parsea pero le falta un campo requerido,
      - parsea pero `reason` no es uno de los valores del enum.
    """
    if not output:
        return None
    body: Optional[str] = None
    m = _INTERRUPTION_FENCE_RE.search(output)
    if m:
        body = m.group("body").strip()
    else:
        m = _INTERRUPTION_LINE_RE.search(output.strip())
        if m:
            body = m.group(1).strip()
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    for k in _INTERRUPTION_REQUIRED:
        if k not in payload:
            return None
    if payload.get("reason") not in _VALID_REASONS:
        return None
    if not isinstance(payload.get("partial_progress"), dict):
        return None
    return payload


def _verdict_match(text: str):
    # Solo un veredicto explícito puede aprobar; "not complete" no aprueba.
    return (_VERDICT_LINE_RE.search(text) or _VERDICT_ALONE_RE.search(text)
            or _VERDICT_RE.search(text))


def _parse_verifier(text: str) -> tuple[str, str, list[int]]:
    """`(complete|needs_more|off_plan|needs_human, feedback, pasos_cubiertos)`.

    Tolerante con prefijos/markdown. Sin veredicto reconocible devuelve
    `needs_human`, sin acreditar pasos. `_run_verifier` registra el fallo
    de formato para distinguirlo de una verificación que exige intervención.

    Pasos cubiertos: lista de enteros extraída de la línea `PASOS: 1,3,5`
    que pide VERIFIER_INSTRUCTIONS. Sirve de red para los pasos que el
    ejecutor olvidó marcar con `plan_step_done`. Si no hay línea `PASOS:`
    válida, se devuelve lista vacía (la fusión con el ejecutor lo trata
    como "no hay red").

    Bug fix 2026-08-15 (multi-modelo): el veredicto se busca en TRES
    pasadas, de la más específica a la más laxa:

      1. una línea `VERDICT: x` — el contrato que pide el prompt;
      2. una línea que sea SOLO el veredicto;
      3. una mención conservadora: needs_more, off_plan o needs_human.

    La 3 era la única que existía, y con un modelo que razona en voz
    alta antes de contestar ("no es `needs_human`, es `complete`") daba
    el veredicto INVERTIDO. Con MiniMax nunca se notó porque contestaba
    con el formato pedido; desde que las etapas corren en los endpoints
    NIM (2026-08-14) el caso es real. La pasada 3 se conserva como
    último recurso únicamente para pedir revisión, nunca para aprobar.
    """
    text = _clean_stage_output(text)
    if not text:
        return "needs_human", (
            "el verificador no devolvió texto: este run NO fue verificado"), []
    m = _verdict_match(text)
    if not m:
        return "needs_human", ("NO fue verificado: falta un veredicto válido. " + text)[:200], []
    verdict = m.group("v").lower()
    # Cap igual que las otras ramas: sin esto, un modelo que contesta con
    # un ensayo deja el ensayo entero en `chats.stages_json`.
    feedback = text[:200]
    fm = _FEEDBACK_RE.search(text)
    if fm:
        feedback = fm.group("f").strip()[:200]
    elif m:
        # Encontró el verdict pero no la línea FEEDBACK: agarramos lo
        # que viene después del verdict.
        rest = text[m.end():].strip().lstrip(":").strip()
        if rest:
            feedback = rest[:200]
    # Red de pasos cubiertos (P5 de la Etapa B).
    pasos: list[int] = []
    pm = _PASOS_LINE_RE.search(text)
    if pm:
        nums = pm.group("nums") or ""
        # `int("0")` es válido pero un paso "0" no existe; filtramos.
        pasos = [int(x) for x in _NUM_RE.findall(nums) if int(x) > 0]
    return verdict, feedback, pasos
