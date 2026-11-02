"""Ejecución por tarea, gates y commits del modo nocturno."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

from . import config
from .experts import cbm_binary_path, cbm_call
from .night_plan import GateCleanupError, NightConfig, resolve_cwd_for_cmd, run_branch_name, run_gate
from .night_generator import NIGHT_WORK_CONTRACT
from .night_types import (
    COMMIT_TRAILER, EXPERT_TIMEOUT_S, FORBIDDEN_PATTERNS, GATE_TIMEOUT_S,
    GIT_TIMEOUT_S, TRANSIENT_BUILD_SIGNATURES, NightTask, TaskResult,
)

logger = logging.getLogger("relay.night")


class NightRecoveryError(RuntimeError):
    """El worktree aislado de la tarea se conserva para recuperación manual."""


class BranchWorker:
    """Fase 2: una tarea → una rama → trabajo → gates → commit o rollback.

    `work_fn` inyectable (tests): async (task, prompt) -> None. Default:
    el experto pydantic-ai del proyecto (reuso de experts.run_expert).
    """

    def __init__(
        self, project: dict, cfg: NightConfig, *,
        run_id: str, skills_block: str = "",
        work_fn: Optional[Callable[..., Any]] = None,
        branch: str = "",
    ) -> None:
        self.project = project
        self.repo = project["repo_path"]
        self.source_repo = self.repo
        self.cfg = cfg
        self.run_id = run_id
        # Rama ÚNICA del run (Iter 5.1). Si no viene del orquestador,
        # la computamos acá (modo interactivo / tests legacy).
        self.branch = branch or run_branch_name(project["slug"])
        self.skills_block = skills_block
        self._work_fn = work_fn or self._expert_work
        # db para run_expert (F0: catálogo, no el blob). El orquestador
        # lo setea en su __init__ si no vino en el constructor. Sin esto,
        # _expert_work cae al path sin catálogo y corre sin tools. Bug
        # fix 2026-07-18.
        self.db: Any = None
        # Estado vivo para observabilidad async (snapshot del orquestador):
        # en qué paso de Fase 2 estamos y el progreso del experto. De noche
        # el timeout puede variar, así que lo que importa NO es su valor
        # sino poder ver, mientras corre, si el experto avanza (idle_s se
        # resetea) o está colgado (idle_s crece). Reuso RunProgress del
        # chat interactivo (experts.py).
        self.current_step: str = ""
        self.step_started_at: float = 0.0
        # Veredicto de la tarea EN CURSO. Se resetea en work_one, no acá:
        # con `work_fn` inyectado (tests) o con una tarea que muere antes
        # del experto, el valor de la tarea anterior se quedaba pegado y
        # el reporte le colgaba a la tarea 3 el juicio de la tarea 2.
        self._last_verdict: str = ""
        self._last_verdict_feedback: str = ""
        self.progress: Any = None    # experts.RunProgress | None
        # SHA del último commit del worker (lo lee _maybe_ask_block_decision
        # vía getattr(worker, "last_commit_sha", None) para mostrar el diff
        # del bloque al LLM secundario). Se setea en work_one() después del
        # commit verde. Bug fix 2026-07-18: getattr devolvía siempre None
        # porque el atributo no existía → _summarize_block_diff quedaba
        # vacío y los checkpoints interactivos iban sin contexto de diff.
        self.last_commit_sha: Optional[str] = None
        self._workspace_ready = False
        self._workspace_path: Optional[Path] = None
        self._manual_recovery = ""

    def _set_step(self, step: str) -> None:
        self.current_step = step
        self.step_started_at = time.monotonic()

    def live_snapshot(self) -> Optional[dict]:
        """Estado vivo de la tarea en curso (o None si no hay ninguna).
        Lo consume NightOrchestrator.snapshot() → GET /night-mode/status."""
        if not self.current_step:
            return None
        now = time.monotonic()
        snap: dict = {
            "step": self.current_step,
            "step_elapsed_s": round(now - self.step_started_at, 1),
        }
        if self.progress is not None:
            snap["expert"] = self.progress.snapshot(now=now)
        return snap

    # -- helpers subprocess --

    async def _git(self, *args: str, timeout: float = GIT_TIMEOUT_S) -> tuple[int, str]:
        from .git_flow import _git
        return await _git(self.repo, *args, timeout=timeout)

    async def _sh(self, cmd: str, timeout: float = GATE_TIMEOUT_S) -> tuple[int, str]:
        # ponytail: ÚNICA ruta con shell en el repo. Las otras dos opciones
        # de ejecutar procesos (gh en git_flow, dotnet build/test en
        # commands) usan _exec / create_subprocess_exec sin shell — los
        # args van literales y no hay que escapar comillas, %, &, ^ ni
        # newlines. Acá seguimos con shell porque `build_cmd` y `test_cmd`
        # son strings libres editables desde la Admin UI (night_config)
        # — el humano QUIERE escribir `dotnet test --filter Category!=Slow`
        # como una línea y que funcione. Si en algún momento quieres
        # validación estricta, parsear con shlex y armar args[] en el
        # caller. Hoy: trade-off explícito, documentado.
        # Bug fix 2026-07-20: si el comando es `dotnet build`/`dotnet test`
        # y no hay sln/csproj en la raíz pero sí en un subdirectorio,
        # correr desde el subdirectorio. Caso anonimizado: WorkshopDemo tiene
        # `backend/*.slnx` y antes el gate fallaba con "Especifique un
        # archivo de proyecto o de solución".
        return await run_gate(self.repo, cmd, timeout)

    def _resolve_cwd_for_cmd(self, cmd: str) -> str:
        return resolve_cwd_for_cmd(self.repo, cmd)

    async def _exec(self, program: str, *args: str,
                    timeout: float = GATE_TIMEOUT_S) -> tuple[int, str]:
        """Como _sh pero sin shell: cada arg va literal (create_subprocess_exec).
        Para `gh`, donde el título/body vienen del LLM: sin cmd.exe no hay
        que escapar comillas, %, &, ^ ni newlines (evita inyección/roturas)."""
        if program == "gh" and args[:2] == ("pr", "create"):
            from .pr_text import neutralize_gh_pr_create_body
            args = neutralize_gh_pr_create_body(args)
        proc = await asyncio.create_subprocess_exec(
            program, *args, cwd=self.repo,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, f"{program} {' '.join(args)} superó {timeout}s"
        return proc.returncode or 0, out_b.decode("utf-8", errors="replace")

    # -- trabajo default: el experto del proyecto --

    async def _expert_work(self, task: NightTask, prompt: str) -> None:
        from .experts import run_expert_staged, make_progress_callback
        # MiniMax M3 a veces devuelve finish_reason='error' en
        # prompts largos, o 400 'invalid function arguments json
        # string' cuando un tool call con JSON inválido queda en la
        # historia (probable bug del provider). Reintentar una vez
        # antes de marcar la tarea como fallida — cuesta 30s pero
        # ahorra un rollback completo. Si el segundo intento también
        # falla, propagamos.
        # De noche nadie espera: el timeout interactivo (300s vía
        # FOURBIS_EXPERT_TIMEOUT en .env) mata al experto a mitad del
        # ciclo read→edit cuando MiniMax tarda >3 min por respuesta
        # (caso real: un run de ejemplo, T-001/T-002 muertas a ~280s).
        # Presupuesto nocturno propio vía defaults_json.timeout — el
        # tope de la cascada de run_expert — sin tocar los chats.
        # Un override explícito del proyecto sigue ganando.
        project = self.project
        defaults = dict(project.get("defaults_json") or {})
        if not defaults.get("timeout"):
            defaults["timeout"] = EXPERT_TIMEOUT_S
            project = {**project, "defaults_json": defaults}
        model = defaults.get("model") or config.model_spec()
        for attempt in range(2):
            # Progress store propio (no el del chat): notify=None → sin
            # spam de Discord por tool call. La RunProgress vive en
            # self.progress y la lee live_snapshot() para GET
            # /night-mode/status. Fresco por intento (resetea idle/tool
            # count tras un retry).
            # db=self.db: el orchestrator ya tiene la DB inyectada; sin
            # esto run_expert caía al blob legacy project["mcp_servers"]
            # y los proyectos nuevos (post-F0) corrían sin wrapper.
            # Bug fix 2026-07-18.
            store: dict = {}
            cb = make_progress_callback(
                store=store, notify=None, chat_id=task.id,
                target=f"night:{self.run_id}:{task.id}", model=model)
            self.progress = store[task.id]
            try:
                # Iter 11: por etapas (planificador + ejecutor +
                # verificador + documentador). El verificador puede
                # pedir needs_human; aquí se registra en el log y se
                # sigue con la próxima tarea. La decisión de abortar el
                # run nocturno es del orquestador, no del wrapper.
                staged = await run_expert_staged(
                    project, prompt, skills_block=self.skills_block,
                    on_progress=cb, db=self.db)
                # Sin esto las etapas auxiliares serían costo puro en
                # modo nocturno: nadie más lee el dict que devuelven.
                if staged:
                    # Se guarda en el worker y no solo en el log: `work_one`
                    # lo copia al TaskResult y de ahi sale al reporte.
                    self._last_verdict = staged.get("verifier_verdict") or ""
                    self._last_verdict_feedback = (
                        staged.get("verifier_feedback") or "")[:300]
                    logger.info(
                        "night task %s: veredicto=%s (%s)", task.id,
                        self._last_verdict or "?",
                        self._last_verdict_feedback[:200])
                if self.progress is not None:
                    self.progress.finished = True
                return
            except (UnexpectedModelBehavior, ModelHTTPError) as e:
                if self.progress is not None:
                    self.progress.finished = True
                    self.progress.error = type(e).__name__
                logger.warning(
                    "_expert_work attempt %d/2 falló (%s): %s",
                    attempt + 1, type(e).__name__, str(e)[:200])
                if attempt == 1:
                    raise

    async def _cbm_context(self, task: NightTask) -> str:
        """Contexto semántico best-effort para la tarea (search_graph
        sobre el título). El experto tiene cbm_query para el detalle."""
        if cbm_binary_path() is None:
            return ""
        from .admin import _cbm_project_name
        raw = await cbm_call(
            "search_graph",
            {"query": task.title, "project": _cbm_project_name(self.source_repo),
             "limit": 10},
            timeout=15.0)
        return raw[:4000] if raw and not raw.startswith('{"error"') else ""

    # -- gates --

    @staticmethod
    def _is_transient_build_error(rc: int, out: str) -> bool:
        """El build falló por el restore en frío flaky del SDK, no por el
        código (ver TRANSIENT_BUILD_SIGNATURES). Ambas firmas presentes
        para no confundir con un error de NuGet real (paquete faltante)."""
        if rc == 0:
            return False
        return all(sig in out for sig in TRANSIENT_BUILD_SIGNATURES)

    async def _build_sh(self, cmd: str) -> tuple[int, str]:
        """Corre un build y reintenta UNA vez si falló por el restore en
        frío flaky (el segundo build encuentra el restore caliente). Un
        error real de compilación (CS####) no matchea → no se reintenta."""
        rc, out = await self._sh(cmd)
        if self._is_transient_build_error(rc, out):
            logger.warning(
                "build falló por restore en frío flaky (NuGet path1); "
                "reintento con restore caliente")
            rc, out = await self._sh(cmd)
        return rc, out

    async def _run_gates(self) -> tuple[bool, str]:
        """TDD estricto: build (si hay) + test (obligatorio) con exit 0."""
        if self.cfg.build_cmd:
            rc, out = await self._build_sh(self.cfg.build_cmd)
            if rc != 0:
                return False, f"build falló (exit {rc}):\n{out[-2000:]}"
        if not self.cfg.test_cmd:
            return False, ("sin test_cmd configurado ni auto-detectado: "
                           "TDD estricto → no se commitea sin gate de tests")
        rc, out = await self._sh(self.cfg.test_cmd)
        if rc != 0:
            return False, f"tests fallaron (exit {rc}):\n{out[-2000:]}"
        return True, ""

    async def _pre_flight_build(self) -> str:
        """Corre el build en master para detectar errores pre-existentes.

        Si falla, devuelve un bloque de texto con los errores para
        inyectar en el prompt del experto. Si pasa (o no hay
        build_cmd), devuelve string vacío.

        Esto cubre el caso típico: el repo master tiene tests rotos
        que NO son de la tarea actual. Sin pre-flight, el experto
        termina su trabajo y el gate rojo le bloquea el commit por
        algo que no le compete. Con pre-flight, ve los errores y
        puede arreglarlos como parte de su tarea.

        ponytail: timeout cap 180s (el build puede ser lento). Si
        supera, devolvemos string vacío y seguimos — el gate al
        final dirá si está roto.
        """
        if not self.cfg.build_cmd:
            return ""
        try:
            rc, out = await self._sh(self.cfg.build_cmd, timeout=180.0)
            if self._is_transient_build_error(rc, out):
                # Warmup: el pre-flight se come el restore en frío flaky
                # para que el gate final arranque caliente.
                rc, out = await self._sh(self.cfg.build_cmd, timeout=180.0)
        except GateCleanupError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("pre_flight_build explotó: %r", e)
            return ""
        if rc == 0:
            return ""
        # Filtrar errores de C# (cs####) — son los más comunes y los
        # más accionables. Si hay errores de otro tipo, también los
        # incluimos (último fallback).
        err_lines: list[str] = []
        for ln in out.splitlines():
            s = ln.strip()
            if not s:
                continue
            if " error CS" in s or "error MSB" in s:
                err_lines.append(s)
            elif err_lines and len(err_lines) < 30 and not s.startswith(" "):
                # líneas de resumen tipo "5 Errores" / "Tiempo..."
                err_lines.append(s)
        if not err_lines:
            return (f"\n\n⚠️ Pre-flight build falló (exit {rc}). "
                    f"Output (últimos 1500 chars):\n```\n{out[-1500:]}\n```\n")
        body = "\n".join(err_lines[:30])
        return (f"\n\n⚠️ Pre-flight build falló en master (exit {rc}). "
                f"La rama actual hereda estos errores pre-existentes. "
                f"Para que tu tarea pueda mergear, arreglálos primero "
                f"(o como parte de tu tarea) — el gate al final exigirá "
                f"que build y tests pasen.\n```\n{body}\n```\n")

    async def _diff_stats(self, *, tree: str = "", checkpoint: str = "HEAD") -> tuple[int, list[str]]:
        """Count the complete candidate, including staged changes.

        Without ``tree`` this inspects tracked working files against HEAD
        plus untracked files. With ``tree`` it inspects only the immutable
        staged snapshot that the caller will compare to the resulting commit.
        NUL-delimited names keep tabs/newlines and literal pathspecs intact.
        """
        args = ("diff", "--numstat", "--no-renames", "-z", checkpoint)
        rc, out = await self._git(*args, *((tree,) if tree else ()))
        if rc != 0:
            raise NightRecoveryError("no se pudo verificar el diff completo de la tarea")
        untracked = ""
        if not tree:
            rc2, untracked = await self._git("ls-files", "--others", "--exclude-standard", "-z")
            if rc2 != 0:
                raise NightRecoveryError("no se pudieron verificar los archivos nuevos de la tarea")
        lines = 0
        files: list[str] = []
        for entry in out.split("\0"):
            if not entry:
                continue
            parts = entry.split("\t", 2)
            if len(parts) == 3:
                add, rem, path = parts
                lines += (int(add) if add.isdigit() else 0)
                lines += (int(rem) if rem.isdigit() else 0)
                files.append(path)
            else:
                raise NightRecoveryError("el diff de la tarea no tiene un formato verificable")
        for path in untracked.split("\0"):
            if path:
                files.append(path)
                try:
                    lines += len((Path(self.repo) / path)
                                 .read_text(encoding="utf-8", errors="ignore")
                                 .splitlines())
                except OSError:
                    lines += 1
        return lines, files

    @staticmethod
    def _forbidden_touched(files: list[str]) -> list[str]:
        out = []
        for f in files:
            fl = f.replace("\\", "/").casefold()
            if any(pat in fl for pat in FORBIDDEN_PATTERNS):
                out.append(f)
        return out

    # -- ciclo completo de una tarea --

    async def work_one(self, task: NightTask) -> TaskResult:
        t0 = time.monotonic()
        branch = self.branch
        res = TaskResult(task_id=task.id, status="failed", branch=branch)
        self._last_verdict = ""
        self._last_verdict_feedback = ""
        self._set_step("precheck")
        if (not self._workspace_ready or self._manual_recovery
                or self._workspace_path != Path(self.repo).resolve()
                or Path(self.repo).is_symlink() or getattr(Path(self.repo), "is_junction", lambda: False)()):
            res.error = self._manual_recovery or "falta preparar el worktree privado del run"
            self._manual_recovery = res.error
            res.duration_s = time.monotonic() - t0
            return res

        # Precondiciones: working tree limpio. La rama ÚNICA del run la
        # creó el orquestador al arrancar (en `ensure_branch`) — si no
        # existe, falla acá con mensaje claro (mejor que inventar
        # nombres por tarea, que era el bug Iter 5.0).
        rc, out = await self._git("status", "--porcelain")
        if rc != 0 or out.strip():
            res.error = "working tree sucio o no-git: tarea salteada"
            self._manual_recovery = res.error
            res.duration_s = time.monotonic() - t0
            return res
        rc, _ = await self._git("rev-parse", "--verify", "--quiet", branch)
        if rc != 0:
            res.error = (f"rama del run {branch!r} no existe: el "
                         "orquestador debe crearla antes de work_one")
            self._manual_recovery = res.error
            res.duration_s = time.monotonic() - t0
            return res

        rc, out = await self._git("rev-parse", "HEAD")
        checkpoint = out.strip()
        ref_rc, ref_out = await self._git("rev-parse", f"refs/heads/{branch}")
        detached_rc, _ = await self._git("symbolic-ref", "--quiet", "HEAD")
        if (rc != 0 or ref_rc != 0 or detached_rc != 1
                or not re.fullmatch(r"[0-9a-f]{40,64}", checkpoint) or ref_out.strip() != checkpoint):
            res.error = "el HEAD privado y la rama del run no coinciden; no se modifica el checkout"
            self._manual_recovery = res.error
            res.duration_s = time.monotonic() - t0
            return res

        try:
            self._set_step("pre_flight_build")
            preflight = await self._pre_flight_build()
            self._set_step("cbm_context")
            cbm_ctx = await self._cbm_context(task)
            prompt = NIGHT_WORK_CONTRACT.format(
                max_diff=self.cfg.max_diff_lines,
                title=task.title,
                refs="\n".join(f"- `{r}`" for r in task.refs),
            )
            if preflight:
                prompt += preflight
            if cbm_ctx:
                prompt += f"\n\nContexto cbm (search_graph sobre la tarea):\n```json\n{cbm_ctx}\n```"

            self._set_step("expert_work")
            await self._work_fn(task, prompt)
            # Una sola copia, acá: todos los `return res` de abajo lo
            # arrastran, y los de ARRIBA (que salen antes de que el
            # experto corra) lo dejan vacío, que es lo correcto — no hubo
            # veredicto porque no hubo trabajo que juzgar.
            res.verdict = self._last_verdict
            res.verdict_feedback = self._last_verdict_feedback

            # Gates (lista cerrada, ADR-028 punto 5).
            self._set_step("diff_stats")
            lines, files = await self._diff_stats()
            forbidden = self._forbidden_touched(files)
            if forbidden:
                return await self._rollback_result(res, checkpoint, f"tocó paths prohibidos: {', '.join(forbidden[:5])}")
            if lines == 0:
                return await self._rollback_result(res, checkpoint, "el experto no produjo cambios")
            if lines > self.cfg.max_diff_lines:
                return await self._rollback_result(res, checkpoint, f"diff {lines} líneas > max_diff_lines={self.cfg.max_diff_lines}")

            self._set_step("gates")
            ok, gate_err = await self._run_gates()
            if not ok:
                return await self._rollback_result(res, checkpoint, gate_err)

            # A build/test can stage or generate additional changes. Validate
            # the final staged tree, not the earlier expert-only working diff.
            self._set_step("final_diff")
            rc, out = await self._git("add", "-A")
            if rc != 0:
                return await self._rollback_result(res, checkpoint, f"git add falló: {out[-500:]}")
            tree_rc, tree_out = await self._git("write-tree")
            candidate_tree = tree_out.strip()
            if tree_rc != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", candidate_tree):
                raise NightRecoveryError("no se confirmó el árbol final staged; se conserva el worktree")
            lines, files = await self._diff_stats(tree=candidate_tree, checkpoint=checkpoint)
            forbidden = self._forbidden_touched(files)
            if forbidden:
                return await self._rollback_result(res, checkpoint, f"los gates tocaron paths prohibidos: {', '.join(forbidden[:5])}")
            if lines == 0:
                return await self._rollback_result(res, checkpoint, "no hay cambios finales para commitear")
            if lines > self.cfg.max_diff_lines:
                return await self._rollback_result(res, checkpoint, f"diff final {lines} líneas > max_diff_lines={self.cfg.max_diff_lines}")

            # Keep normal Git commit hooks/signing. If a hook or an unexpected
            # writer changes the index, retain the private commit for recovery
            # instead of publishing a tree that did not pass the final gate.
            self._set_step("commit")
            msg = f"{task.title}\n\n{COMMIT_TRAILER.format(run_id=self.run_id)}"
            rc, out = await self._git("commit", "-m", msg)
            if rc != 0:
                return await self._rollback_result(res, checkpoint, f"commit falló: {out[-500:]}")
            # Capturar SHA del commit fresco para que el próximo
            # _maybe_ask_block_decision pueda resumir el diff. Bug fix
            # 2026-07-18: este atributo no existía y getattr devolvía
            # siempre None → _summarize_block_diff vacío → checkpoints
            # interactivos sin contexto.
            sha_rc, sha_out = await self._git("rev-parse", "HEAD")
            sha = sha_out.strip()
            if sha_rc != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
                raise NightRecoveryError("commit creado pero no se confirmó su SHA; se conserva el worktree")
            committed_tree_rc, committed_tree = await self._git("rev-parse", "HEAD^{tree}")
            if committed_tree_rc != 0 or committed_tree.strip() != candidate_tree:
                raise NightRecoveryError(f"commit {sha} conservado; su árbol difiere del validado y no se publica")
            parent_rc, parent = await self._git("rev-parse", "HEAD^")
            clean_rc, pending = await self._git("status", "--porcelain", "--untracked-files=all")
            if parent_rc != 0 or parent.strip() != checkpoint or clean_rc != 0 or pending.strip():
                raise NightRecoveryError(f"commit {sha} conservado; parentesco o limpieza posterior no verificables")
            # Detached private HEAD: advance only our run ref, with compare-and-swap.
            attached_rc, attached = await self._git("worktree", "list", "--porcelain")
            if attached_rc != 0 or f"branch refs/heads/{branch}" in attached.splitlines():
                raise NightRecoveryError(f"commit {sha} conservado; rama del run ocupada o no verificable")
            publish_rc, _ = await self._git("update-ref", f"refs/heads/{branch}", sha, checkpoint)
            if publish_rc != 0:
                raise NightRecoveryError(f"commit {sha} conservado; la rama cambió y no se publicó el resultado")
            self.last_commit_sha = sha

            res.status = "done"
            return res
        except (NightRecoveryError, GateCleanupError) as exc:
            self._manual_recovery = f"Recuperación manual requerida: {exc}"
            res.error = self._manual_recovery
            return res
        except asyncio.CancelledError:
            # Cancellation may originate in an expert/Git subprocess as well
            # as a gate. Retain all data rather than race unknown writers.
            self._manual_recovery = "Tarea cancelada; worktree retenido sin restauración automática"
            raise
        except Exception as e:  # noqa: BLE001 — una tarea rota no voltea el run
            logger.exception("night task %s explotó", task.id)
            # Persistir el traceback completo en state/ para diagnóstico
            # sin tener que capturar stdout del server.
            import traceback as _tb
            tb_text = _tb.format_exc()
            try:
                state_run = (
                    Path(__file__).parent.parent.parent
                    / "state" / "agents" / "night-runs" / self.run_id)
                state_run.mkdir(parents=True, exist_ok=True)
                (state_run / f"task_{task.id}_error.txt").write_text(
                    f"{type(e).__name__}: {e}\n\n--- traceback ---\n{tb_text}",
                    encoding="utf-8")
            except OSError:
                pass
            # str(e) puede ser '' en asyncio.TimeoutError → el reporte
            # queda "TimeoutError: ". Agregamos el contexto: en qué paso
            # de la Fase 2 estábamos (work / gates / commit / push).
            msg = str(e) or f"(sin mensaje, ver state/agents/night-runs/{self.run_id}/task_{task.id}_error.txt)"
            return await self._rollback_result(res, checkpoint, f"{type(e).__name__} (en {self.current_step}): {msg}")
        finally:
            # Keep the run's detached private workspace. Never switch the
            # user's checkout or carry a dirty index onto another branch.
            res.duration_s = time.monotonic() - t0
            # Estado vivo consumido: la tarea terminó. Limpiar para que
            # live_snapshot() devuelva None entre tareas.
            self.current_step = ""
            self.progress = None

    async def _rollback_result(self, result: TaskResult, checkpoint: str, reason: str) -> TaskResult:
        try:
            await self._rollback(checkpoint)
        except NightRecoveryError as exc:
            self._manual_recovery = f"{reason}; recuperación manual requerida: {exc}"
            result.status, result.error = "failed", self._manual_recovery
        else:
            result.status, result.error = "rolled_back", reason
        return result

    async def _rollback(self, checkpoint: str) -> None:
        """Restore only the owned private worktree to this task's clean HEAD.

        No reset of history, no clean and no checkout of the user's base.
        Untracked files survive and stop the run for explicit recovery.
        """
        if (not self._workspace_ready or self._workspace_path != Path(self.repo).resolve()
                or Path(self.repo).resolve() == Path(self.source_repo).resolve()
                or Path(self.repo).is_symlink() or getattr(Path(self.repo), "is_junction", lambda: False)()):
            raise NightRecoveryError("el checkout no pertenece al worker; no se restaura")
        rc, head = await self._git("rev-parse", "HEAD")
        symbolic_rc, _ = await self._git("symbolic-ref", "--quiet", "HEAD")
        if rc != 0 or head.strip() != checkpoint or symbolic_rc != 1:
            raise NightRecoveryError("HEAD cambió o no está detached; se conserva el trabajo")
        rc, _ = await self._git("restore", f"--source={checkpoint}", "--staged", "--worktree", "--", ".")
        if rc != 0:
            raise NightRecoveryError("falló la restauración del índice/worktree; no se cambia de rama")
        rc, status = await self._git("status", "--porcelain", "--untracked-files=all")
        if rc != 0 or status.strip():
            raise NightRecoveryError("quedan cambios o archivos nuevos preservados; no se cambia de rama")

    async def ensure_branch(self) -> Optional[str]:
        """Prepare a retained private detached worktree, never a shared checkout.

        Record intent before worktree creation. An interrupted/moved workspace
        requires recovery; it is not silently recreated or automatically removed.
        """
        if self._workspace_ready:
            return self._manual_recovery or None
        base = self.cfg.base_branch
        branch = self.branch
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.run_id):
            return "run_id inválido para el workspace privado"
        rc, status = await self._git("status", "--porcelain")
        if rc != 0 or status.strip():
            return "checkout de origen sucio o no verificable; no se prepara el run"
        # Best-effort fetch. Si falla (red/auth), sigo con local stale.
        try:
            from .git_flow import fetch_origin_safe
            await fetch_origin_safe(self.repo)
        except Exception as e:  # noqa: BLE001
            logger.warning("ensure_branch: fetch best-effort explotó: %r", e)
        rc, out = await self._git("status", "--porcelain")
        if rc != 0:
            return f"git status falló (rc={rc}): {out[-200:]}"
        if out.strip():
            return f"working tree sucio al arrancar: {out.strip()[:200]}"
        # Select the fetched ref without fast-forwarding a user's checked-out
        # base or moving any existing local branch.
        start_ref = base
        if not base.startswith("origin/"):
            remote_rc, _ = await self._git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base}")
            if remote_rc == 0:
                start_ref = f"refs/remotes/origin/{base}"
        rc, _ = await self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        if rc != 0:
            rc, out = await self._git("branch", branch, start_ref)
            if rc != 0:
                return f"no se pudo crear la rama del run: {out[-200:]}"
        rc, worktrees = await self._git("worktree", "list", "--porcelain")
        if rc != 0 or f"branch refs/heads/{branch}" in worktrees.splitlines():
            return "la rama del run está ocupada por otro checkout; no se mueve"
        workspace_root = config.db_path().parent / "night-workspaces"
        workspace = workspace_root / self.run_id
        manifest = workspace_root / f"{self.run_id}.json"
        identity = {"run_id": self.run_id, "branch": branch,
                    "source_repo": str(Path(self.source_repo).resolve()),
                    "workspace": str(workspace.resolve())}
        try:
            workspace_root.mkdir(parents=True, exist_ok=True)
            if manifest.exists():
                if json.loads(manifest.read_text(encoding="utf-8")) != identity or not workspace.is_dir():
                    return "workspace previo ausente o identidad distinta; recuperación manual requerida"
            else:
                if workspace.exists() or workspace.is_symlink():
                    return "la ruta privada ya está ocupada; no se reutiliza ni se borra"
                with manifest.open("x", encoding="utf-8") as stream:
                    json.dump(identity, stream)
                rc, out = await self._git("worktree", "add", "--detach", str(workspace), f"refs/heads/{branch}")
                if rc != 0:
                    return f"worktree no confirmado; se conserva intención para recuperación: {out[-200:]}"
            from .git_flow import _git
            rc, top = await _git(str(workspace), "rev-parse", "--show-toplevel")
            common_rc, common = await _git(str(workspace), "rev-parse", "--path-format=absolute", "--git-common-dir")
            source_rc, source_common = await self._git("rev-parse", "--path-format=absolute", "--git-common-dir")
            detached_rc, _ = await _git(str(workspace), "symbolic-ref", "--quiet", "HEAD")
            if (workspace.is_symlink() or getattr(workspace, "is_junction", lambda: False)()
                    or rc != 0 or Path(top.strip()).resolve() != workspace.resolve()
                    or common_rc != 0 or source_rc != 0
                    or Path(common.strip()).resolve() != Path(source_common.strip()).resolve()
                    or detached_rc != 1):
                return "identidad del worktree privado no verificable; no se utiliza"
        except (OSError, ValueError) as exc:
            return f"workspace privado no preparado: {type(exc).__name__}"
        self.repo = str(workspace)
        self.project = {**self.project, "repo_path": self.repo}
        self._workspace_path = workspace.resolve()
        self._workspace_ready = True
        return None

    async def finalize_pr(self, tasks: list[NightTask],
                          results: list[TaskResult]) -> Optional[str]:
        """Empuja la rama ÚNICA y abre UN PR draft con el resumen del run.

        Llamado por el orquestador al cerrar el run. Best-effort:
        devuelve la URL del PR o None si falló (push, gh, sin commits
        propios, etc.). El reporte ya escrito refleja el resultado.
        """
        base = self.cfg.base_branch
        branch = self.branch
        if self._manual_recovery:
            logger.warning("finalize_pr: workspace retained for manual recovery; no push")
            return None
        done = [r for r in results if r.status == "done"]
        if not done:
            logger.info(
                "finalize_pr: sin tareas done para %s, no abro PR",
                self.run_id)
            return None
        # ¿Hay commits propios en la rama? Si no, nada que pushear.
        rc, out = await self._git("rev-list", "--count", f"{base}..{branch}")
        if rc != 0 or not out.strip() or int(out.strip()) == 0:
            logger.info(
                "finalize_pr: rama %s sin commits propios, no hay nada "
                "que pushear", branch)
            return None
        # Push.
        rc, out = await self._git("push", "-u", "origin", branch,
                                  timeout=120.0)
        if rc != 0:
            logger.warning("finalize_pr: push %s falló: %s",
                           branch, out[-300:])
            return None
        # PR draft con body resumen del run (no del LLM — el body
        # viene del orquestador para que no dependa de generación
        # LLM extra).
        title = f"Night run {self.run_id} — {self.project['slug']}"
        lines = [
            f"Night run `{self.run_id}` (modo nocturno 4bis.relay)",
            "",
            f"- Proyecto: `{self.project['slug']}`",
            f"- Tareas completadas: {len(done)}",
            f"- Rollbacks: {len(results) - len(done)}",
            "",
            "## Tareas",
            "",
        ]
        by_id = {t.id: t for t in tasks}
        for r in done:
            t = by_id.get(r.task_id)
            title_t = t.title if t else "?"
            refs = ", ".join(f"`{x}`" for x in (t.refs if t else []))
            lines.append(f"- **{r.task_id}**: {title_t}  \n  Refs: {refs}")
        lines += ["", f"_{COMMIT_TRAILER.format(run_id=self.run_id)}_"]
        body = "\n".join(lines)
        rc_pr, out_pr = await self._exec(
            "gh", "pr", "create", "--draft",
            "--title", title, "--body", body,
            "--base", base, "--head", branch, timeout=60.0)
        if rc_pr != 0:
            logger.warning("finalize_pr: gh pr create falló: %s",
                           out_pr[-300:])
            return None
        url = out_pr.strip().splitlines()[-1] if out_pr.strip() else ""
        return url or None


# ---------- MorningReporter ----------
