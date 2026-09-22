"""Ejecución por tarea, gates y commits del modo nocturno."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

from . import config
from .experts import cbm_binary_path, cbm_call
from .night_plan import NightConfig, resolve_cwd_for_cmd, run_branch_name, run_gate
from .night_generator import NIGHT_WORK_CONTRACT
from .night_types import (
    COMMIT_TRAILER, EXPERT_TIMEOUT_S, FORBIDDEN_PATTERNS, GATE_TIMEOUT_S,
    GIT_TIMEOUT_S, TRANSIENT_BUILD_SIGNATURES, NightTask, TaskResult,
)

logger = logging.getLogger("relay.night")

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
            {"query": task.title, "project": _cbm_project_name(self.repo),
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

    async def _diff_stats(self) -> tuple[int, list[str]]:
        """(líneas cambiadas, archivos tocados) vs working tree."""
        rc, out = await self._git("diff", "--numstat")
        rc2, untracked = await self._git("ls-files", "--others", "--exclude-standard")
        lines = 0
        files: list[str] = []
        for ln in out.splitlines():
            parts = ln.split("\t")
            if len(parts) == 3:
                add, rem, path = parts
                lines += (int(add) if add.isdigit() else 0)
                lines += (int(rem) if rem.isdigit() else 0)
                files.append(path)
        for path in untracked.splitlines():
            if path.strip():
                files.append(path.strip())
                try:
                    lines += len((Path(self.repo) / path.strip())
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
        base = self.cfg.base_branch
        branch = self.branch
        res = TaskResult(task_id=task.id, status="failed", branch=branch)
        self._last_verdict = ""
        self._last_verdict_feedback = ""
        self._set_step("precheck")

        # Precondiciones: working tree limpio. La rama ÚNICA del run la
        # creó el orquestador al arrancar (en `ensure_branch`) — si no
        # existe, falla acá con mensaje claro (mejor que inventar
        # nombres por tarea, que era el bug Iter 5.0).
        rc, out = await self._git("status", "--porcelain")
        if rc != 0 or out.strip():
            res.error = "working tree sucio o no-git: tarea salteada"
            res.duration_s = time.monotonic() - t0
            return res
        rc, _ = await self._git("rev-parse", "--verify", "--quiet", branch)
        if rc != 0:
            res.error = (f"rama del run {branch!r} no existe: el "
                         "orquestador debe crearla antes de work_one")
            res.duration_s = time.monotonic() - t0
            return res

        # Asegurar que estamos EN la rama (el orquestador hace checkout
        # al crearla, pero si el work_one se llama interactivamente /
        # por tests, podemos estar en base).
        rc, out = await self._git("checkout", branch)
        if rc != 0:
            res.error = f"checkout {branch} falló: {out[-500:]}"
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
                await self._rollback(branch)
                res.status = "rolled_back"
                res.error = f"tocó paths prohibidos: {', '.join(forbidden[:5])}"
                return res
            if lines == 0:
                await self._rollback(branch)
                res.status = "rolled_back"
                res.error = "el experto no produjo cambios"
                return res
            if lines > self.cfg.max_diff_lines:
                await self._rollback(branch)
                res.status = "rolled_back"
                res.error = f"diff {lines} líneas > max_diff_lines={self.cfg.max_diff_lines}"
                return res

            self._set_step("gates")
            ok, gate_err = await self._run_gates()
            if not ok:
                await self._rollback(branch)
                res.status = "rolled_back"
                res.error = gate_err
                return res

            # Verde: commit con trailer en la rama ÚNICA del run. El
            # push + PR se hace UNA vez al final del run
            # (NightOrchestrator.finalize_pr) — no por tarea.
            self._set_step("commit")
            await self._git("add", "-A")
            msg = f"{task.title}\n\n{COMMIT_TRAILER.format(run_id=self.run_id)}"
            rc, out = await self._git("commit", "-m", msg)
            if rc != 0:
                await self._rollback(branch)
                res.status = "rolled_back"
                res.error = f"commit falló: {out[-500:]}"
                return res
            # Capturar SHA del commit fresco para que el próximo
            # _maybe_ask_block_decision pueda resumir el diff. Bug fix
            # 2026-07-18: este atributo no existía y getattr devolvía
            # siempre None → _summarize_block_diff vacío → checkpoints
            # interactivos sin contexto.
            sha_rc, sha_out = await self._git("rev-parse", "HEAD")
            if sha_rc == 0 and sha_out.strip():
                self.last_commit_sha = sha_out.strip().splitlines()[0]

            res.status = "done"
            return res
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
            await self._rollback(branch)
            res.status = "rolled_back"
            # str(e) puede ser '' en asyncio.TimeoutError → el reporte
            # queda "TimeoutError: ". Agregamos el contexto: en qué paso
            # de la Fase 2 estábamos (work / gates / commit / push).
            msg = str(e) or f"(sin mensaje, ver state/agents/night-runs/{self.run_id}/task_{task.id}_error.txt)"
            res.error = f"{type(e).__name__} (en {self.current_step}): {msg}"
            return res
        finally:
            # SIEMPRE volver a base (la próxima tarea parte de base y
            # hace checkout a la rama del run; la rama ÚNICA persiste
            # con todos sus commits buenos).
            await self._git("checkout", base)
            res.duration_s = time.monotonic() - t0
            # Estado vivo consumido: la tarea terminó. Limpiar para que
            # live_snapshot() devuelva None entre tareas.
            self.current_step = ""
            self.progress = None

    async def _rollback(self, branch: str) -> None:
        """Rollback NO destructivo (Iter 5.1).

        Antes: `git branch -D` borraba la rama entera. Con el modelo
        de rama única, los commits buenos de las tareas previas TIENEN
        que sobrevivir — descartamos solo el trabajo de ESTA tarea.

        Cuándo se llama: desde `work_one` cuando el experto no
        produjo cambios, gates rojos, paths prohibidos, TDD sin
        test_cmd, o el commit mismo falló. **Antes del commit** = el
        commit de ESTA tarea todavía NO existe en la rama; los de
        tareas anteriores sí.

        Estrategia: `git checkout -- .` + `git clean -fd` (descarta
        working tree), después `git checkout base` (HEAD fuera de la
        rama). NO tocamos la historia — el último commit (de la tarea
        anterior, si lo hubo) sigue intacto en la rama.

        Caso especial post-commit: si la tarea alcanzó a commitear y
        algo posterior falló (push, pr create, etc.), el orquestador
        llama `git reset --hard HEAD~1` explícitamente antes del
        rollback — `_rollback` no lo hace porque puede NO saber si
        hay un commit propio que descartar.
        """
        await self._git("checkout", "--", ".")
        await self._git("clean", "-fd")
        # Salir de la rama (a base). La rama queda intacta con los
        # commits de tareas previas. work_one.finally vuelve a base
        # explícitamente — esto es defensa en profundidad.
        base = self.cfg.base_branch
        await self._git("checkout", base)

    async def ensure_branch(self) -> Optional[str]:
        """Crea la rama ÚNICA del run si no existe, desde base limpio.

        Llamado por el orquestador UNA vez al arrancar el run. Idempotente:
        si la rama ya existe (caso 'resume' post-crash), la reutiliza.
        Devuelve mensaje de error o None si todo OK.

        Bug fix 2026-07-20: hace `git fetch origin` best-effort antes de
        detectar/crear la rama. Sin esto, si el local tiene días de stale
        data, el bot opera sobre refs viejas. Caso anonimizado: WorkshopDemo
        tenía develop local atrasado del origin por varios commits, el
        night run basó la rama sobre develop viejo y el experto propuso
        cambios ya mergeados. Mismo problema que un chat que
        gatilló este fix: el LLM responde coherente con lo que ve, pero
        lo que ve no es lo que está en el remoto.
        """
        base = self.cfg.base_branch
        branch = self.branch
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
        # El fetch de arriba solo movía `origin/*`: la rama del run seguía
        # saliendo del base LOCAL stale (bug 2026-07-27, mismo síntoma que
        # el del 20/7). Solo fast-forward; si divergió no toca nada y el
        # run sale del local (sus commits importan).
        try:
            from .git_flow import sync_base_with_origin
            await sync_base_with_origin(self.repo, base)
        except Exception as e:  # noqa: BLE001
            logger.warning("ensure_branch: ff del base explotó: %r", e)
        rc, _ = await self._git("rev-parse", "--verify", "--quiet", branch)
        if rc == 0:
            # Ya existe: asumimos resume (post-crash). Checkout a la
            # rama para que la primera tarea opere sobre ella.
            rc, out = await self._git("checkout", branch)
            return None if rc == 0 else f"checkout rama existente falló: {out[-200:]}"
        rc, out = await self._git("checkout", "-b", branch, base)
        if rc != 0:
            return f"checkout -b {branch} falló: {out[-200:]}"
        # Volvemos a base — las tareas harán checkout a la rama en
        # cada work_one.
        await self._git("checkout", base)
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
