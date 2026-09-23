"""Generación y validación determinista del plan nocturno."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

from . import config
from .experts import build_model, cbm_binary_path, cbm_call, structured_output_settings
from .night_plan import PLAN_INSTRUCTIONS
from .night_types import (
    CBM_INDEX_LIMIT, PLAN_DIRNAME, PLAN_FILENAME, PLAN_PROMPT_FILES_CAP,
    PLAN_TIMEOUT_S, NightTask, PlanDraft,
)

logger = logging.getLogger("relay.night")


def _summarize_block_diff(
    repo_path: str, last_commit_sha: Optional[str],
) -> dict:
    """Genera un resumen del diff del último commit del bloque.

    Devuelve {files: ['path (+N/-M)', ...], commit_message: '...'}
    con cap de 10 archivos para no inflar el embed del checkpoint.
    Best-effort total: si git falla, devuelve {}.
    """
    if not last_commit_sha:
        return {}
    try:
        out = subprocess.run(
            ["git", "show", "--stat", "--format=%s", last_commit_sha],
            cwd=repo_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=10, check=False)
        if out.returncode != 0:
            return {}
        lines = (out.stdout or "").splitlines()
        # Primera línea no-vacía = subject del commit.
        commit_message = ""
        files: list[str] = []
        for ln in lines:
            if not commit_message and ln.strip():
                commit_message = ln.strip()
            elif ln.strip() and "|" in ln:
                # Formato: " path/to/file.cs | 5 ++-..."
                # Tomamos solo el path.
                files.append(ln.strip().split(" | ", 1)[0])
        return {
            "files": files[:10],
            "commit_message": commit_message,
        }
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {}


def _norm_path(p: str) -> str:
    """Normaliza para comparar: separadores posix + casefold (Windows)."""
    return p.replace("\\", "/").strip("/").casefold()


# Regex para extraer puntos enumerados de una directiva (Iter 5.2 — fix
# del caso real: directiva con "P0.1, P0.2" donde el LLM devolvía UNA sola
# tarea). Cubre:
#   - "P0.1", "P0.2"           (prefijo alpha + dot + dot)
#   - "1.", "2.", "3)"         (numeración plana)
#   - "a)", "b.", "c-"         (letra + delimitador)
#   - "Step 1:", "Paso 2 -"    (palabra + número + delimitador)
# Estrategia: el id se extrae como `[Pp]\d+(?:\.\d+)?` (con prefijo si
# existe) O `(\d+(?:\.\d+)?|[a-zA-Z])` pelado. El delimitador sigue
# al id.
_POINT_RE = re.compile(
    r"(?:^|\n)"
    r"\s*"
    r"(?P<id>"
    r"[Pp]\d+(?:\.\d+)?"           # P0.1 / p0.2 (con prefijo)
    r"|[Ss]tep\s+\d+"               # Step 1 / step 2
    r"|[Pp]aso\s+\d+"               # Paso 1 / paso 2
    r"|\d+(?:\.\d+)?"              # 1, 1.2, 3 (pelado)
    r"|[a-zA-Z]"                    # a, b, A (letra pelada)
    r")"
    r"\s*[\.\)\:\-\u2014\u2013]\s+",  # delimitador: .  )  :  -  — (em-dash)  – (en-dash)
    re.MULTILINE,
)


def extract_directive_points(directive: str) -> list[str]:
    """Extrae los IDs de puntos enumerados de la directiva del run.

    Caso real que rompió: directiva con 'P0.1 — ... P0.2 — ...' donde el
    LLM devolvió una sola tarea (P0.1) y tiró P0.2 al piso. Ahora
    extraemos los puntos y los comparamos contra las tareas generadas:
    si falta alguno, log warning + alerta en el reporte (sin abortar).
    """
    if not directive:
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for m in _POINT_RE.finditer(directive):
        pid = m.group("id")
        if pid and pid not in seen:
            ids.append(pid)
            seen.add(pid)
    return ids


def find_missing_points(directive: str, tasks: list[NightTask]) -> list[str]:
    """Diff entre puntos enumerados de la directiva y títulos generados.

    No es un match exacto (los títulos se reformatean), es heurístico:
    busca el id del punto (P0.1, 1, a, ...) como substring en el título
    de cada tarea. Si un id de la directiva NO aparece en ningún título,
    es candidato a 'olvidado por el LLM' y se reporta.
    """
    expected = extract_directive_points(directive)
    if not expected:
        return []
    # Concatenar todos los títulos para búsqueda substring barata.
    titles_blob = " ".join(t.title for t in tasks).lower()
    missing: list[str] = []
    for pid in expected:
        if pid.lower() not in titles_blob:
            missing.append(pid)
    return missing


class TaskGenerator:
    """Fase 1. Genera el plan grounded y valida refs contra cbm."""

    def __init__(self, project: dict, *, model_spec: str = "",
                 run_id: str = "",
                 propagate_failures: bool = False) -> None:
        self.project = project
        self.repo_path = project["repo_path"]
        defaults = project.get("defaults_json") or {}
        # Fase 1 también planifica: usa la misma cascada de rol que el
        # planificador del runner, sin mezclarla con graph_planner_model.
        self.model_spec = (model_spec or defaults.get("planner_model")
                           or config.planner_model_spec())
        self.run_id = run_id
        # Iter 9.7: si True, generate() PROPAGA las excepciones
        # transitorias (UnexpectedModelBehavior, ModelHTTPError,
        # asyncio.TimeoutError) en vez de degradar a plan vacío. El
        # orchestrator interactivo las captura y pregunta al humano.
        # Default False (comportamiento actual: nunca crashear el run).
        self.propagate_failures = propagate_failures

    async def indexed_files(self) -> Optional[set[str]]:
        """Set de paths (normalizados) del grafo cbm, o None si cbm no
        está disponible / el proyecto no está indexado.

        cbm es DEPENDENCIA DURA de la Fase 1 (enmienda ADR-028): sin
        índice no hay validación determinista → el run aborta.
        """
        if cbm_binary_path() is None:
            return None
        from .admin import _cbm_project_name  # lazy: evita ciclo de imports
        cbm_proj = _cbm_project_name(self.repo_path)
        # format="json" es de cbm 0.10.0; 0.9.0 ignora los args que no
        # conoce (verificado), así que pedirlo es seguro en las dos.
        raw = await cbm_call(
            "search_graph",
            {"label": "File", "project": cbm_proj, "limit": CBM_INDEX_LIMIT,
             "format": "json"},
            timeout=30.0,
        )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or data.get("error"):
            return None

        # Dos formas de respuesta, porque el binario puede ser cualquiera
        # de las dos versiones y esto es dependencia DURA de la Fase 1:
        # si acá devolvemos None por no entender el formato, el night run
        # aborta entero. Que el rollback del binario sea un `cp` y no un
        # revert de código.
        #   0.9.0 : {"results": [{"file_path": "src/x.py", ...}, ...]}
        #   0.10.0: {"groups":  [{"file": "src/x.py", "rows": [...]}, ...]}
        # Los errores son JSON idéntico en ambas, así que el guard de
        # arriba no cambia.
        paths: list[str] = []
        for item in data.get("results") or []:
            if isinstance(item, dict) and isinstance(item.get("file_path"), str):
                paths.append(item["file_path"])
        for group in data.get("groups") or []:
            if isinstance(group, dict) and isinstance(group.get("file"), str):
                paths.append(group["file"])

        # Path relativo al repo -> absoluto normalizado, para matchear
        # contra `Path(repo) / ref` en ref_valid(). Mezclar relativos y
        # absolutos acá fue un bug real (iter 5): rompía el match contra
        # el parent del ref.
        found = {_norm_path(str(Path(self.repo_path) / p))
                 for p in paths if p.strip()}
        return found or None

    def ref_valid(self, ref: str, indexed: set[str]) -> bool:
        """Validación determinista de UNA ref (el contrato de la Fase 1):
        (a) el archivo existe en disco dentro del repo, y
        (b) resuelve contra el índice cbm (match exacto o por sufijo —
            cbm guarda paths absolutos, las refs son relativas).

        Caso especial: si el archivo NO existe todavía pero su
        directorio padre SÍ existe dentro del repo (y hay otros
        archivos indexados en él), aceptar como ref válida. Cubre la
        tarea típica de noche: "agrega un test nuevo en
        CommerceDemo.Tests/Services/" — el archivo a crear es válido
        porque vive en una carpeta real e indexada.
        """
        ref = ref.strip()
        if not ref or ".." in ref.replace("\\", "/").split("/"):
            return False
        ref_path = Path(self.repo_path) / ref
        try:
            file_exists = ref_path.is_file()
        except OSError:
            return False
        n = _norm_path(ref)
        matches_index = any(ip == n or ip.endswith("/" + n) for ip in indexed)
        if file_exists:
            return matches_index
        # Archivo a crear: aceptar si el padre existe y tiene hermanos
        # indexados. Esto evita que el LLM invente paths en carpetas
        # inexistentes (ej: "tests/foo.cs" cuando no hay carpeta tests).
        parent = ref_path.parent
        try:
            parent_exists = parent.is_dir()
        except OSError:
            parent_exists = False
        if not parent_exists:
            return False
        # Buscar al menos UN archivo indexado cuyo path empiece con
        # el directorio padre normalizado (case-insensitive). El
        # índice guarda paths absolutos (cbm style), el padre es
        # absoluto también — comparo ambos normalizados.
        pn_abs = _norm_path(str(parent))
        return any(ip == pn_abs or ip.startswith(pn_abs + "/")
                   for ip in indexed)

    def drafts_to_tasks(
        self, drafts: list[PlanDraft], indexed: set[str],
    ) -> list[NightTask]:
        """Asigna IDs y aplica la validación: refs inválidas → `[-]`."""
        tasks: list[NightTask] = []
        for i, d in enumerate(drafts, start=1):
            # Sanitizar para el formato de UNA línea del ledger: sin
            # newlines en el título, sin backticks en las refs.
            title = re.sub(r"\s+", " ", d.title).strip().rstrip(".") + "."
            task = NightTask(
                id=f"T-{i:03d}", title=title,
                refs=[r.replace("\\", "/").replace("`", "").strip()
                      for r in d.refs if r.strip()],
            )
            if not task.refs:
                task.status = "discarded"
                task.note = "sin refs"
            else:
                bad = [r for r in task.refs if not self.ref_valid(r, indexed)]
                if bad:
                    task.status = "discarded"
                    task.note = f"ref no resuelve en cbm: {', '.join(bad[:3])}"
            tasks.append(task)
        return tasks

    def _persist_llm_failure(self, text: str) -> None:
        """Guarda el detalle del fallo LLM en el state dir del run para
        diagnosticar sin capturar stdout del server. Best-effort."""
        try:
            state_run = (
                Path(__file__).parent.parent.parent
                / "state" / "agents" / "night-runs"
                / (self.run_id or "unknown"))
            state_run.mkdir(parents=True, exist_ok=True)
            (state_run / "llm_failure.txt").write_text(
                text, encoding="utf-8")
        except OSError:
            pass

    # Keywords que matchean directivas de "auditoría / status /
    # reconciliación" — el disparador del fallback cuando el LLM
    # devuelve 0 drafts. Cubre español (sin acentos).
    _AUDIT_KEYWORDS = (
        "auditar", "auditoria", "status", "estado",
        "actualiz", "reconcil", "verific", "revisar",
        "stale", "desactualiz", "al dia", "al-día",
        "documentacion", "documentación", "docs", "doc",
    )

    def _audit_fallback_tasks(
        self, directive: str, indexed: set[str],
    ) -> list[NightTask]:
        """Si el LLM devuelve [] y la directiva matchea una keyword de
        auditoría, construir un mini-plan atómico a partir de los
        archivos `.md` indexados en el repo.

        Las refs DEBEN matchear cbm (sino `ref_valid` las descarta);
        la validación determinista de Fase 1 sigue aplicando, no la
        salteamos.

        Caso real: varios runs de ejemplo en
        workshopdemo, 5 corridas seguidas con `no_tasks` por directiva
        "actualiza toda la documentación, dame status". El LLM
        legítimamente rechaza (no tiene tools); este fallback le da
        al usuario UN primer paso verificable.
        """
        d = (directive or "").lower()
        if not any(kw in d for kw in self._AUDIT_KEYWORDS):
            return []
        # Listar los .md indexados relativos al repo (los absolutos
        # están normalizados con _norm_path).
        repo_norm = _norm_path(self.repo_path)
        md_files_rel: list[str] = []
        for ip in indexed:
            if not ip.startswith(repo_norm + "/"):
                continue
            rel = ip[len(repo_norm) + 1:]
            if rel.lower().endswith(".md"):
                md_files_rel.append(rel)
        md_files_rel = sorted(set(md_files_rel))
        # Filtrar `.relay/night-plan.md` (es el ledger del run, no docs).
        md_files_rel = [
            m for m in md_files_rel
            if not m.startswith(".relay/")
        ]
        if not md_files_rel:
            return []
        # Encontrar un nombre razonable para el archivo de status.
        # Si existe "docs/" en el set, lo usamos; si no, raiz.
        status_md_rel = "docs/DOC_STATUS.md"
        # Si `docs/` no es carpeta indexada, pararnos en raíz.
        if not any(m.startswith("docs/") for m in md_files_rel):
            status_md_rel = "DOC_STATUS.md"
        # Construir 2 tareas atómicas: (1) auditar (read_file cada
        # .md) + (2) escribir el status report. Split por mitades si
        # hay >6 .md para no inflar una sola tarea (max_diff_lines=200).
        tasks: list[NightTask] = []
        # Tarea 1: leer .md y compararlos contra código/configs.
        if len(md_files_rel) <= 4:
            chunks = [md_files_rel]
        else:
            mid = (len(md_files_rel) + 1) // 2
            chunks = [md_files_rel[:mid], md_files_rel[mid:]]
        for i, chunk in enumerate(chunks, start=1):
            t = NightTask(
                id=f"T-{i:03d}",
                title=(
                    f"Auditar {len(chunk)} archivos .md ("
                    + ", ".join(Path(m).name for m in chunk)
                    + ") comparándolos contra el código real del repo."
                ),
                refs=chunk,
            )
            tasks.append(t)
        # Tarea final: crear el status report.
        # La ref del status_md_rel puede no existir aún: la lógica
        # `ref_valid` acepta archivos a crear si el padre existe y
        # hay hermanos indexados. Si la validación la descarta, el
        # fallback habrá fracasado y el run dirá no_tasks igual —
        # pero al menos dimos un primer paso concreto.
        tasks.append(NightTask(
            id=f"T-{len(tasks)+1:03d}",
            title=(
                "Escribir el status report consolidando el resultado de "
                "la auditoría: para cada .md, clasificarlo como "
                "al-día / stale / incompleto / no-corresponde-a-código."
            ),
            refs=md_files_rel + [status_md_rel],
        ))
        # Marcar como discarded lo que ref_valid rechace. Si todas
        # quedan pending, devolvemos. Si todas discarded, devolvemos
        # [] (el caller decide qué hacer).
        out: list[NightTask] = []
        for t in tasks:
            bad = [r for r in t.refs if not self.ref_valid(r, indexed)]
            if bad:
                t.status = "discarded"
                t.note = f"ref no resuelve en cbm: {', '.join(bad[:3])}"
            out.append(t)
        return [t for t in out if t.status == "pending"] or out

    async def generate(
        self, directive: str = "", error_logs: str = "",
    ) -> tuple[list[NightTask], list[str]]:
        """Directiva/logs → drafts (LLM) → tareas validadas.

        Devuelve `(tasks, missing_points)`. `missing_points` es la lista
        de IDs de la directiva (P0.1, 1, a, ...) que el LLM no
        representó como tarea — se loggea warning y se reporta en el
        morning report para que el humano sepa qué se quedó en el tintero.

        Levanta RuntimeError si cbm no está disponible (dependencia
        dura — el orquestador lo reporta y aborta el run).
        """
        indexed = await self.indexed_files()
        if indexed is None:
            raise RuntimeError(
                f"cbm index no disponible para {self.project['slug']!r}: "
                "Fase 1 abortada (indexa el repo desde la Admin UI)")

        # Contexto pre-LLM (mismo patrón que ADR-020): el relay aporta
        # hechos, el LLM decide. Git diff best-effort.
        from .experts import _build_git_diff_block_sync
        git_block = await asyncio.to_thread(
            _build_git_diff_block_sync, self.repo_path)

        # El índice guarda paths ABSOLUTOS normalizados; al LLM le
        # prometemos (instrucciones + ejemplo) paths RELATIVOS al repo.
        # Relativizar acá: menos tokens y refs consistentes con el
        # contrato de ref_valid.
        repo_norm = _norm_path(self.repo_path)
        files_sample = sorted(
            ip[len(repo_norm) + 1:] if ip.startswith(repo_norm + "/") else ip
            for ip in indexed)[:PLAN_PROMPT_FILES_CAP]
        prompt_parts = [
            f"Directiva: {directive.strip() or '(sin directiva: usa los logs de error)'}",
        ]
        if error_logs.strip():
            prompt_parts.append(f"Logs de error:\n```\n{error_logs.strip()[:8000]}\n```")
        if git_block:
            prompt_parts.append(git_block)
        prompt_parts.append(
            "Archivos indexados (usa SOLO estos paths en refs):\n"
            + "\n".join(files_sample))

        agent_model = build_model(self.model_spec)
        from pydantic_ai import Agent
        # retries=3 (default 1) → 4 intentos totales. Directivas largas
        # suelen agotar output_tokens en el primero; los retries le dan
        # aire. Si igual falla, degradamos a plan vacío (no crasheamos
        # el run): el reporte dirá no_tasks y tú reformulas.
        # output_type=list[PlanDraft] (no PlanDrafts): con el wrapper
        # `tasks: [...]` el LLM de MiniMax M3 confunde `refs: list[str]`
        # con un objeto {"item": "..."} en el primer intento. Sin wrapper,
        # el schema es plano y emite bien el array de strings.
        # ponytail: si MiniMax M3 cambia, volver a PlanDrafts y revisar.
        agent = Agent(
            agent_model, instructions=PLAN_INSTRUCTIONS,
            output_type=list[PlanDraft], retries=3,
            # deepseek-v4 rechaza el tool_choice que implica output_type
            # si está en thinking mode (400). Ver el helper.
            model_settings=structured_output_settings(self.model_spec))
        try:
            result = None
            for attempt in range(2):
                try:
                    result = await asyncio.wait_for(
                        agent.run("\n\n".join(prompt_parts)),
                        timeout=PLAN_TIMEOUT_S)
                    break
                except ModelHTTPError as e:
                    # MiniMax M3 a veces emite un tool call con JSON
                    # inválido; el retry interno de pydantic-ai reenvía
                    # esa historia envenenada y el provider rechaza el
                    # request ENTERO con 400 'invalid function arguments
                    # json string'. Un agent.run fresco no arrastra la
                    # historia → reintentar UNA vez. Caso real:
                    # un run de ejemplo commercedemo 2026-07-10.
                    logger.warning(
                        "night plan attempt %d/2 falló (ModelHTTPError %s): %s",
                        attempt + 1, e.status_code, str(e)[:300])
                    if attempt == 1:
                        raise
            drafts = result.output
            tasks = self.drafts_to_tasks(drafts, indexed)
            if not tasks:
                # Diferenciar 'el LLM devolvió []' (plan vacío legítimo)
                # de 'el LLM no produjo output válido' (bug). Si el LLM
                # devolvió algo pero no se parseó como tareas, lo logueamos
                # para entender qué pasó.
                logger.warning(
                    "night plan vacío para %s: el LLM devolvió %d drafts "
                    "(verifica que la directiva sea concreta y los paths "
                    "estén indexados en cbm)",
                    self.project['slug'], len(drafts))
                # Bug fix 2026-07-20: si el LLM devuelve [] pero la
                # directiva habla de "auditoría / status / actualizar /
                # reconciliar" y el repo tiene archivos `.md` indexados,
                # construir automáticamente un mini-plan de tareas
                # concretas (leer X + escribir status report). El LLM
                # rechaza legítimamente directivas vagas sin tools;
                # acá le damos un primer paso atómico que SÍ matchea
                # las reglas duras de PLAN_INSTRUCTIONS.
                fallback = self._audit_fallback_tasks(directive, indexed)
                if fallback:
                    logger.info(
                        "night audit-fallback activado para %s: %d tasks "
                        "auto-construidas desde la directiva + paths .md",
                        self.project['slug'], len(fallback))
                    tasks = fallback
            # Validación post-gen: ¿faltaron puntos enumerados? (Iter 5.2)
            missing = find_missing_points(directive, tasks)
            if missing:
                logger.warning(
                    "night plan incompleto para %s: la directiva listaba "
                    "%s pero las tareas generadas no los cubren (IDs "
                    "faltantes: %s). Posible truncado por output_tokens "
                    "del LLM — reformula la directiva o pártela en dos runs.",
                    self.project['slug'],
                    extract_directive_points(directive) or "(?)",
                    ", ".join(missing))
            return tasks, missing
        except UnexpectedModelBehavior as e:
            # El LLM no pudo producir output válido tras N intentos
            # (típicamente: JSON truncado por límite de output_tokens o
            # schema inválido). El body crudo del LLM queda en e.body —
            # lo logueamos con cap a 2KB para no ahogar el log. Además
            # lo persistimos en el state dir del run para diagnosticar
            # sin necesidad de capturar stdout del server.
            body = (e.body or "")[:2000]
            logger.warning(
                "night plan generation falló para %s: %s | body(raw, 2KB): %s",
                self.project['slug'], e.message, body)
            self._persist_llm_failure(
                f"{e.message}\n\n--- body (2KB) ---\n{body}\n")
            if self.propagate_failures:
                # Modo interactivo (Iter 9.7): el orchestrator decide
                # si pregunta al humano o sigue.
                raise
            return [], []
        except ModelHTTPError as e:
            # Segundo 400 consecutivo del provider (ver retry arriba):
            # mismo trato que UnexpectedModelBehavior — plan vacío, el
            # reporte dirá no_tasks y tú reformulas. NO crashear el run.
            logger.warning(
                "night plan generation falló para %s tras retry "
                "(ModelHTTPError %s): %s",
                self.project['slug'], e.status_code, str(e)[:300])
            self._persist_llm_failure(f"ModelHTTPError tras retry: {e}\n")
            if self.propagate_failures:
                raise
            return [], []
        except asyncio.TimeoutError:
            # El planificador colgó más de PLAN_TIMEOUT_S. Mismo trato que
            # arriba: degradamos a plan vacío en vez de crashear el run (el
            # reporte dirá no_tasks y tú reformulas), no lo dejamos explotar
            # al except Exception del orquestador.
            logger.warning(
                "night plan generation timeout (%ss) para %s → plan vacío",
                PLAN_TIMEOUT_S, self.project['slug'])
            if self.propagate_failures:
                raise
            return [], []


# ---------- persistencia del ledger ----------


def write_plan_files(
    repo_path: str, state_dir: Path, run_id: str, text: str,
) -> None:
    """Escribe el ledger en el target repo (.relay/) + espejo en state/.

    El del repo se excluye de git vía .git/info/exclude (NO tocamos el
    .gitignore del proyecto — decisión 11 de la enmienda ADR-028).
    Best-effort en el repo; el espejo en state/ es la fuente de resume.
    """
    mirror = state_dir / "night-runs" / run_id / "plan.md"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    mirror.write_text(text, encoding="utf-8")
    try:
        plan_dir = Path(repo_path) / PLAN_DIRNAME
        plan_dir.mkdir(exist_ok=True)
        (plan_dir / PLAN_FILENAME).write_text(text, encoding="utf-8")
        exclude = Path(repo_path) / ".git" / "info" / "exclude"
        if exclude.parent.is_dir():
            existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
            if f"{PLAN_DIRNAME}/" not in existing:
                exclude.write_text(
                    existing.rstrip("\n") + f"\n{PLAN_DIRNAME}/\n",
                    encoding="utf-8")
    except OSError as e:
        logger.warning("no pude escribir el plan en el repo (%r); "
                       "el espejo en state/ queda como verdad", e)


# ---------- Fase 2: BranchWorker (Ejecutor en frío) ----------

NIGHT_WORK_CONTRACT = """\
## Contrato del modo nocturno (ADR-028)

Estás trabajando de noche, sin supervisión. Tu ÚNICA tarea es la de
abajo. Reglas duras:
- Toca SOLO lo necesario para la tarea (diff pequeño, < {max_diff} líneas).
- NUNCA toques .env, secrets, migraciones de DB ni configs de deploy.
- No hagas git commit/push/checkout — de eso se encarga el orquestador.
- El gate es que build y tests pasen: escribe/ajusta tests si la tarea
  lo amerita (TDD).

## Límite de iteraciones (NO te pases)

Tu ÚNICO objetivo es editar los archivos de la tarea y nada más.
Límite duro: máximo 10 llamadas a tools en total. Si llegaste a 10
calls sin haber editado todavía, deja de leer contexto y edita con
lo que ya tienes. Pasarte de este límite te hace perder el gate (el
timeout global del experto te corta).

Plan típico (no obligatorio):
  1. read_file del archivo a editar (1 call)
  2. read_file del archivo de referencia (1 call)
  3. edit_file o write_file del cambio (1 call)
  4. (opcional) ajustar test adyacente si la tarea lo requiere

No iteres leyendo variantes del mismo archivo — con leerlo UNA vez
alcanza. No uses cbm_query para esta tarea si ya te pasé el path
del archivo a tocar.

## Tarea

{title}

Archivos de referencia (verificados contra el índice cbm):
{refs}

Usa `cbm_query` (search_graph, get_code_snippet, trace_path) y
`read_file` para el contexto exacto antes de editar."""


def _verdict_icon(v: str) -> str:
    """Veredicto → celda de tabla. Vacío = el verificador no corrió (o la
    tarea murió antes), y eso NO es lo mismo que "pasó": se dice."""
    return {
        "complete": "✅ complete",
        "needs_more": "🔸 needs_more",
        "off_plan": "🧭 off_plan",
        "needs_human": "🙋 needs_human",
    }.get((v or "").strip().lower(), "— sin veredicto")
