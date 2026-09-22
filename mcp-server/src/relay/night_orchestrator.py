"""Orquestación del pipeline nocturno y checkpoints interactivos."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

from .experts import build_model, structured_output_settings
from .night_generator import TaskGenerator, _summarize_block_diff, write_plan_files
from .night_plan import NightConfig, render_plan
from .night_report import MorningReporter
from .night_types import BlockDecision, MIN_TASK_WINDOW_S, NightTask, TaskResult, PLAN_TIMEOUT_S
from .night_worker import BranchWorker

logger = logging.getLogger("relay.night")


class NightOrchestrator:
    """Loop principal: Fase 1 una vez → Fase 2 por tarea hasta deadline.

    Siempre emite reporte (deadline, stop manual o crash) y cierra la
    fila night_runs. Un orquestador = un run = un proyecto.
    """

    def __init__(
        self, *, db: Any, project: dict, deadline: datetime,
        directive: str = "", error_logs: str = "",
        notify: Any = None, state_dir: Optional[Path] = None,
        generator: Optional[TaskGenerator] = None,
        worker: Optional[BranchWorker] = None,
    ) -> None:
        self.db = db
        self.project = project
        self.deadline = deadline
        self.directive = directive
        self.error_logs = error_logs
        self.notify = notify
        self.state_dir = state_dir or Path("./state")
        self.run_id = f"run_{uuid.uuid4().hex[:8]}"
        self.cfg = NightConfig.from_project(project)
        # Iter 9.7: si el proyecto está en interactive_mode, el generator
        # PROPAGA las excepciones transitorias de Fase 1 en vez de
        # degradar — el orchestrator las captura y pregunta al humano.
        if generator is None:
            self.generator = TaskGenerator(
                project, run_id=self.run_id,
                propagate_failures=bool(project.get("interactive_mode")))
        else:
            self.generator = generator
        self.worker = worker or BranchWorker(
            project, self.cfg, run_id=self.run_id)
        # Inyectar db al worker para que _expert_work → run_expert
        # vaya por el catálogo F0 (no el blob legacy). Si el caller
        # inyectó un worker custom, también le pasamos el db — son los
        # tests los que se benefician.
        self.worker.db = self.db
        self.reporter = MorningReporter(self.state_dir)
        # Iter 9.7: opt-in del modo interactivo. Default 0 (comportamiento
        # actual). Si está prendido: el orchestrator hace checkpoint entre
        # bloques lógicos y pregunta al humano cuando Fase 1 falla.
        self.interactive_mode = bool(project.get("interactive_mode"))

        self.status = "running"        # running | stopping | finished | crashed
        self.started_at = ""
        self.current_task: Optional[str] = None
        self.tasks: list[NightTask] = []
        self.results: list[TaskResult] = []
        # Iter 5.2: P0.x olvidados por el LLM. Inicializado acá para
        # que el `finally` de run() pueda leerlo incluso si generate()
        # explotó antes de la asignación.
        self.missing_points: list[str] = []
        self._stop = asyncio.Event()

    def stop(self) -> None:
        """Para el loop después de la tarea actual. Idempotente."""
        self._stop.set()
        if self.status == "running":
            self.status = "stopping"

    # ---- Iter 9.7: checkpoints interactivos ----
    # ask_and_wait: el primitive. Crea una night_questions row, notifica
    # al bot (best-effort), y polea la DB cada poll_every hasta que el
    # humano responda o pase el deadline. asyncio.sleep NO bloquea el
    # event loop del relay — otros jobs siguen corriendo.
    # Devuelve el answer_json parseado, o None si pasó el deadline.

    async def _ask_and_wait(
        self, question: dict, phase: str,
    ) -> Optional[dict]:
        if not self.interactive_mode:
            # Modo no-interactivo: no pregunta, devuelve None
            # (comportamiento actual, default).
            return None
        q_id = f"q_{uuid.uuid4().hex[:8]}"
        question["deadline_iso"] = self.deadline.isoformat()
        # Preferir el canal mapeado en projects.discord_channel_id
        # (iter 9.8); fallback al del night_config.
        notify_target = (
            self.project.get("discord_channel_id")
            or self.cfg.discord_channel)
        try:
            await self.db.create_night_question(
                q_id, self.run_id, phase,
                json.dumps(question),
                notify_target=notify_target,
                notify_via="both",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("create_night_question falló: %r", e)
            return None
        # Notificación al bot (best-effort: si falla, el humano
        # igual puede responder por admin UI).
        if self.notify is not None:
            try:
                await self.notify.send(
                    agent_id=f"night:{self.run_id}", kind="question",
                    message=f"[{self.project['slug']}] {question.get('title', '')}",
                    metadata={
                        "run_id": self.run_id,
                        "project": self.project["slug"],
                        "q_id": q_id,
                        "question": question,
                        "discord_channel": notify_target,
                    },
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("notify.send(question) falló: %r", e)
        # Poll loop. 5s entre checks; cada check es 1 query barata.
        poll_every = 5.0
        while datetime.now().astimezone() < self.deadline:
            if self._stop.is_set():
                # Stop manual: cerrar la pregunta sin respuesta.
                await self.db.skip_night_question(q_id)
                return None
            ans = await self.db.get_night_question_answer(q_id)
            if ans is not None:
                return ans
            await asyncio.sleep(poll_every)
        # Deadline pasado: skip (respuesta vacía). El orchestrator
        # decide qué hacer con esto (default en question["default"]).
        await self.db.skip_night_question(q_id)
        return None

    def _apply_default_answer(
        self, question: dict, answer: Optional[dict],
    ) -> dict:
        """Si el humano no respondió, devuelve la opción default."""
        if answer:
            return answer
        default_key = question.get("default", "")
        for opt in question.get("options", []):
            if opt.get("key") == default_key:
                return {"choice": default_key, "label": opt.get("label", "")}
        return {"choice": default_key, "label": "(default)"}

    async def _handle_phase1_failure(
        self, exc: Exception,
    ) -> Optional[tuple[list[NightTask], list[str]]]:
        """Si interactive_mode=1 y Fase 1 falló con error transitorio,
        pregunta al humano qué hacer. Si responde con algo accionable,
        retorna el (tasks, missing) resultado de la nueva corrida.
        Si no responde, retorna None → el caller cierra el run."""
        if not self.interactive_mode:
            return None
        # Construimos la pregunta según el tipo de error.
        err_msg = str(exc)[:200]
        kind = "phase1_failed"
        prompt = (
            f"El planificador falló: {err_msg}. ¿Qué hago?")
        options = [
            {"key": "A", "label": "Reintentar con directive ajustada (responde con texto en Discord)"},
            {"key": "B", "label": "Probar con modelo MiniMax-M2.5"},
            {"key": "C", "label": "Cerrar el run, lo veo después"},
        ]
        question = {
            "kind": kind,
            "title": f"Fase 1 falló para {self.project['slug']}",
            "prompt": prompt,
            "options": options,
            "default": "C",
            "context": {"last_error": err_msg},
        }
        answer = await self._ask_and_wait(question, phase="phase1")
        if answer is None:
            return None
        effective = self._apply_default_answer(question, answer)
        choice = (effective.get("choice") or "").upper()
        if choice == "B":
            # Re-correr generate() con MiniMax-M2.5 forzado.
            logger.info("phase1 retry con M2.5 (decisión humana)")
            try:
                self.generator.model_spec = (
                    "minimax:MiniMax-M2.5")
                return await self.generator.generate(
                    self.directive, self.error_logs)
            except Exception as e:  # noqa: BLE001
                logger.warning("retry con M2.5 también falló: %r", e)
                return None
        if choice == "A":
            # Free-text del humano — todavía no tenemos el canal de
            # free-text desde Discord, así que lo loggeamos y cerramos.
            logger.info(
                "humano pidió ajustar directive pero free-text "
                "todavía no está implementado: cerrando")
            return None
        # C (default): cerrar run.
        return None

    async def _maybe_ask_block_decision(
        self, task: NightTask, task_result: Any,
    ) -> None:
        """Si interactive_mode=1 y la tarea terminó OK, preguntamos al
        LLM si necesita input humano para seguir. Si dice que sí,
        pregunta al humano y loggea la decisión (no aplica cambios
        al plan en esta versión — la decisión queda en el reporte).

        Best-effort total: cualquier falla del LLM secundario se ignora.
        Costo: 1 round LLM extra por bloque completado."""
        if not self.interactive_mode:
            return
        if task_result.status != "done":
            return  # solo preguntamos entre bloques que salieron bien
        # Diff summary del último commit del bloque.
        diff = _summarize_block_diff(
            self.project["repo_path"],
            getattr(self.worker, "last_commit_sha", None))
        summary_text = (
            f"Tarea {task.id} ({task.title}) completada. "
            f"Commit: {diff.get('commit_message', '(sin mensaje)')}. "
            f"Archivos tocados: {', '.join(diff.get('files', [])[:5])}"
        )
        # LLM secundario: ¿necesitamos preguntar?
        prompt = (
            f"Contexto: {summary_text}\n\n"
            "¿Necesito input humano antes de seguir con la próxima "
            "tarea? Si la decisión es reversible y chica, sigue solo. "
            "Si es irreversible o arquitectural, devuelve una pregunta "
            "con opciones concretas (mín 2). Si no, devuelve un objeto "
            "vacío {{}}.")
        try:
            from .experts import build_model
            from pydantic_ai import Agent
            agent = Agent(
                build_model(self.generator.model_spec),
                output_type=Optional[BlockDecision],
                model_settings=structured_output_settings(
                    self.generator.model_spec))
            result = await asyncio.wait_for(
                agent.run(prompt), timeout=PLAN_TIMEOUT_S)
            decision = result.output
        except Exception as e:  # noqa: BLE001
            logger.warning("_maybe_ask_block_decision LLM falló: %r", e)
            return
        if decision is None:
            return  # el LLM dijo: sigue solo
        # El LLM quiere preguntar. Convertir a dict y notificar.
        question = {
            "kind": "block_decision",
            "title": decision.title,
            "prompt": decision.prompt,
            "options": [opt.model_dump() for opt in decision.options],
            "default": decision.default,
            "context": {"block_id": task.id, "diff_summary": diff},
        }
        answer = await self._ask_and_wait(question, phase="block_done")
        # La decisión queda en el morning report (no la aplicamos al
        # plan en esta versión — el orchestrator sigue con la próxima
        # tarea igual). Documentado en el doc de diseño.
        effective = self._apply_default_answer(question, answer)
        logger.info(
            "block_decision %s → %s (answer=%s)",
            task.id, effective.get("choice"),
            "default" if answer is None else "human")

    def snapshot(self) -> dict:
        now = datetime.now().astimezone()
        pending = sum(1 for t in self.tasks if t.status == "pending")
        snap = {
            "run_id": self.run_id,
            "project": self.project["slug"],
            "status": self.status,
            "current_task": self.current_task,
            "tasks_done": sum(1 for t in self.tasks if t.status == "done"),
            "tasks_pending": pending,
            "tasks_discarded": sum(1 for t in self.tasks if t.status == "discarded"),
            "prs_opened": sum(1 for r in self.results if r.pr_url),
            "seconds_to_deadline": max(
                0, int((self.deadline - now).total_seconds())),
        }
        # Estado vivo de la tarea en curso: en qué paso de Fase 2 estamos
        # (pre_flight_build / expert_work / gates / commit / ...) y, si el
        # experto está corriendo, su phase/last_tool/tool_calls/idle_s.
        # `idle_s` es el detector de cuelgue: crece = el experto no produce
        # nodos; se resetea = avanza. Con esto el valor del timeout deja de
        # ser crítico — se ve si está vivo mientras corre async.
        live_fn = getattr(self.worker, "live_snapshot", None)
        live = live_fn() if callable(live_fn) else None
        if live is not None:
            snap["live"] = live
        return snap

    def _write_ledger(self) -> None:
        text = render_plan(
            project_slug=self.project["slug"], run_id=self.run_id,
            directive=self.directive, tasks=self.tasks)
        write_plan_files(
            self.project["repo_path"], self.state_dir, self.run_id, text)

    async def run(self) -> None:
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await self.db.create_night_run(
            self.run_id, self.project["slug"], self.deadline.isoformat())
        end_reason = "no_tasks"
        error: Optional[str] = None
        pr_url: Optional[str] = None
        try:
            # ---- Rama ÚNICA del run (Iter 5.1) ----
            # La crea el orquestador UNA vez antes de Fase 1; el worker
            # hace checkout a la rama en cada work_one. Sin esto, las
            # tareas no saben sobre qué rama escribir.
            branch_err = await self.worker.ensure_branch()
            if branch_err:
                raise RuntimeError(
                    f"no pude preparar la rama del run: {branch_err}")

            # ---- Fase 1: plan grounded + validado ----
            # Iter 9.7: en interactive_mode, generate() propaga las
            # excepciones transitorias y el orchestrator pregunta al
            # humano qué hacer antes de cerrar.
            try:
                self.tasks, self.missing_points = (
                    await self.generator.generate(
                        self.directive, self.error_logs))
            except (UnexpectedModelBehavior, ModelHTTPError,
                    asyncio.TimeoutError) as phase1_err:
                retry_result = await self._handle_phase1_failure(phase1_err)
                if retry_result is None:
                    # Humano no respondió o eligió cerrar.
                    logger.info(
                        "phase1 sin recuperación → run termina con no_tasks")
                    self.tasks, self.missing_points = [], []
                else:
                    self.tasks, self.missing_points = retry_result
            await asyncio.to_thread(self._write_ledger)

            # Si el plan trajo tareas ejecutables, el fin natural del loop es
            # "completed"; deadline/manual_stop lo pisan vía break. Sin tareas
            # pendientes (todas descartadas o plan vacío) queda "no_tasks".
            if any(t.status == "pending" for t in self.tasks):
                end_reason = "completed"

            # ---- Fase 2: una tarea por vez, en frío ----
            for task in self.tasks:
                if task.status != "pending":
                    continue
                if self._stop.is_set():
                    end_reason = "manual_stop"
                    break
                now = datetime.now().astimezone()
                left = (self.deadline - now).total_seconds()
                if left < MIN_TASK_WINDOW_S:
                    end_reason = "deadline"
                    break
                self.current_task = f"{task.id}: {task.title}"
                result = await self.worker.work_one(task)
                self.results.append(result)
                if result.status == "done":
                    task.status = "done"
                    # En el modelo de rama única, `pr_url` lo asigna
                    # el orquestador al cerrar (PR único). Mientras
                    # tanto, dejamos el nombre de rama como nota.
                    task.note = result.branch
                else:
                    task.status = "discarded"
                    task.note = result.error[:200]
                await asyncio.to_thread(self._write_ledger)
                # Iter 9.7: checkpoint entre bloques (solo interactive).
                # El LLM evalúa si necesita input humano para seguir.
                await self._maybe_ask_block_decision(task, result)
            else:
                if self._stop.is_set():
                    end_reason = "manual_stop"
            self.current_task = None

            # ---- PR único al final (Iter 5.1) ----
            # Una sola URL para todo el run; sobreescribe los pr_url
            # vacíos de cada resultado con la URL del PR consolidado.
            try:
                pr_url = await self.worker.finalize_pr(
                    self.tasks, self.results)
                if pr_url:
                    for r in self.results:
                        if r.status == "done":
                            r.pr_url = pr_url
                    await asyncio.to_thread(self._write_ledger)
            except Exception as e:  # noqa: BLE001
                logger.warning("finalize_pr falló: %r", e)

            self.status = "finished"
        except Exception as e:  # noqa: BLE001 — crash → reporte parcial igual
            logger.exception("night run %s crasheó", self.run_id)
            error = f"{type(e).__name__}: {e}"
            end_reason = "crashed"
            self.status = "crashed"
        finally:
            md = self.reporter.build_md(
                run_id=self.run_id, project_slug=self.project["slug"],
                started_at=self.started_at,
                deadline_at=self.deadline.isoformat(),
                end_reason=end_reason, tasks=self.tasks,
                results=self.results, error=error,
                branch=self.worker.branch, pr_url=pr_url,
                missing_points=self.missing_points)
            snap = self.snapshot()
            report_path: Optional[Path] = None
            try:
                report_path = await self.reporter.emit(
                    md, project_slug=self.project["slug"],
                    run_id=self.run_id, notify=self.notify,
                    discord_channel=self.cfg.discord_channel,
                    summary=(
                        f"[4bis / {self.project['slug']}] Night run finished — "
                        f"{snap['prs_opened']} PRs, {snap['tasks_done']} done, "
                        f"{end_reason}."))
            except Exception as e:  # noqa: BLE001
                logger.exception("reporter falló: %r", e)
            await self.db.finish_night_run(
                self.run_id, end_reason=end_reason,
                prs_opened=snap["prs_opened"],
                tasks_done=snap["tasks_done"],
                tasks_discarded=snap["tasks_discarded"],
                report_path=str(report_path) if report_path else None,
                error=error,
                branch=self.worker.branch,
                pr_url=pr_url)
