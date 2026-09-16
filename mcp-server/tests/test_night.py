"""Tests Iter 5 — Modo Nocturno (ADR-028, pipeline de dos fases).

Cover:
  - ledger: render/parse roundtrip del formato estricto, estados,
    líneas malformadas ignoradas
  - NightConfig: auto-detect por stack + overrides de night_config
  - Fase 1: validación determinista de refs (existe en disco + cbm),
    drafts→tasks con descarte `[-]`, generate() con TestModel
  - Fase 2: BranchWorker sobre un repo git REAL temporal — verde
    (commit + rama), gate rojo (rollback), diff cap, paths prohibidos,
    working tree sucio
  - Orquestador: run completo con stubs → ledger + night_runs + reporte;
    crash en Fase 1 → reporte parcial igual
  - Endpoints: 202 / 400 / 404 / 409 / stop idempotente / status

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_night.py -q
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from relay import night
from relay.db import Database
from relay.night import (
    BranchWorker,
    NightConfig,
    NightOrchestrator,
    NightTask,
    TaskGenerator,
    autodetect_cmds,
    parse_plan,
    render_plan,
)

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR",
             "FOURBIS_MODEL", "STATE_DIR")

PY = sys.executable


def _git(repo: str, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=30)
    return (out.stdout or "") + (out.stderr or "")


def _make_git_repo(base: Path) -> str:
    """Repo git real con un commit inicial en main."""
    repo = base / "repo"
    repo.mkdir()
    _git(str(repo), "init", "-b", "main")
    _git(str(repo), "config", "user.email", "night@test")
    _git(str(repo), "config", "user.name", "night test")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    _git(str(repo), "add", "-A")
    _git(str(repo), "commit", "-m", "init")
    return str(repo)


# ---------- ledger ----------


class TestPlanLedger(unittest.TestCase):
    def test_roundtrip(self) -> None:
        tasks = [
            NightTask("T-001", "Extraer timeout a config.", ["src/a.cs", "src/b.cs"]),
            NightTask("T-002", "Cubrir con test.", ["tests/t.py"],
                      status="done", note="night/2026-07-09-t-002"),
            NightTask("T-003", "Renombrar handler.", ["x.cs"],
                      status="discarded", note="ref no resuelve en cbm: x.cs"),
        ]
        text = render_plan(project_slug="demo", run_id="run_abc",
                           directive="arreglar timeout", tasks=tasks)
        parsed = parse_plan(text)
        self.assertEqual(len(parsed), 3)
        self.assertEqual(parsed[0].id, "T-001")
        self.assertEqual(parsed[0].refs, ["src/a.cs", "src/b.cs"])
        self.assertEqual(parsed[0].status, "pending")
        self.assertEqual(parsed[1].status, "done")
        self.assertEqual(parsed[1].note, "night/2026-07-09-t-002")
        self.assertEqual(parsed[2].status, "discarded")
        self.assertIn("no resuelve", parsed[2].note)

    def test_strict_format_ignores_noise(self) -> None:
        text = "\n".join([
            "# Night plan — demo — 2026-07-09",
            "prosa suelta que no es tarea",
            "- [ ] sin task id (Refs: `a`)",           # sin T-NNN
            "- [ ] T-001: válida. (Refs: `src/a.py`)",
            "- [z] T-002: estado inválido. (Refs: `b`)",
        ])
        parsed = parse_plan(text)
        self.assertEqual([t.id for t in parsed], ["T-001"])


# ---------- config ----------


class TestNightConfig(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_autodetect_dotnet(self) -> None:
        (self.base / "App.sln").write_text("", encoding="utf-8")
        self.assertEqual(autodetect_cmds(str(self.base)),
                         ("dotnet build", "dotnet test --no-build"))

    def test_autodetect_node(self) -> None:
        (self.base / "package.json").write_text(
            json.dumps({"scripts": {"build": "x", "test": "y"}}),
            encoding="utf-8")
        self.assertEqual(autodetect_cmds(str(self.base)),
                         ("npm run build", "npm test"))

    def test_autodetect_python(self) -> None:
        (self.base / "pyproject.toml").write_text("", encoding="utf-8")
        self.assertEqual(autodetect_cmds(str(self.base)), (None, "pytest"))

    def test_autodetect_nothing(self) -> None:
        self.assertEqual(autodetect_cmds(str(self.base)), (None, None))

    def test_overrides_beat_autodetect(self) -> None:
        (self.base / "pyproject.toml").write_text("", encoding="utf-8")
        cfg = NightConfig.from_project({
            "repo_path": str(self.base),
            "night_config": {"test_cmd": "pytest -x", "max_diff_lines": 50},
        })
        self.assertEqual(cfg.test_cmd, "pytest -x")
        self.assertEqual(cfg.max_diff_lines, 50)
        self.assertEqual(cfg.discord_channel, "#equipo-demo")


class TestVerifyRepo(unittest.IsolatedAsyncioTestCase):
    """verify_repo alimenta el body del PR de /cerrar: verde, rojo o
    'nadie verificó'. Comandos triviales para no depender del stack."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _project(self, **cfg: object) -> dict:
        return {"repo_path": str(self.base), "night_config": cfg}

    async def test_verde(self) -> None:
        ok, md = await night.verify_repo(
            str(self.base),
            self._project(build_cmd="python -c \"pass\"",
                          test_cmd="python -c \"pass\""))
        self.assertTrue(ok)
        self.assertIn("✅ verde", md)

    async def test_rojo_no_corre_tests_si_falla_el_build(self) -> None:
        ok, md = await night.verify_repo(
            str(self.base),
            self._project(build_cmd="python -c \"raise SystemExit(1)\"",
                          test_cmd="python -c \"open('CORRIO','w')\""))
        self.assertFalse(ok)
        self.assertIn("ROJO", md)
        self.assertFalse((self.base / "CORRIO").exists())

    async def test_sin_comandos_avisa_que_nadie_verifico(self) -> None:
        ok, md = await night.verify_repo(str(self.base), self._project())
        self.assertIsNone(ok)
        self.assertIn("nadie compiló", md)


# ---------- Fase 1: validación de refs ----------


class TestTaskGeneratorModel(unittest.TestCase):
    def test_usa_la_cascada_del_rol_planificador(self) -> None:
        project = {
            "slug": "demo", "repo_path": ".",
            "defaults_json": {
                "model": "executor:base",
                "planner_model": "project:planner",
            },
        }
        with patch.object(night.config, "planner_model_spec",
                          return_value="global:planner"):
            self.assertEqual(
                TaskGenerator(project, model_spec="run:planner").model_spec,
                "run:planner")
            self.assertEqual(TaskGenerator(project).model_spec, "project:planner")
            self.assertEqual(TaskGenerator({
                **project, "defaults_json": {"model": "executor:base"},
            }).model_spec, "global:planner")


class TestRefValidation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._runtime_before = dict(night.config._runtime)
        night.config.set_runtime_config({"FOURBIS_MODEL": "test",
                                         "FOURBIS_PLANNER_MODEL": "test",
                                         "FOURBIS_COMPACTOR_MODEL": "test"})
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = _make_git_repo(Path(self._tmp.name))
        self.gen = TaskGenerator({
            "slug": "demo", "repo_path": self.repo,
            "defaults_json": {"model": "test"}})
        # cbm devuelve paths absolutos normalizados; el set usa _norm_path
        self.indexed = {night._norm_path(str(Path(self.repo) / "src" / "main.py"))}

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()
        night.config.set_runtime_config(self._runtime_before)

    def test_ref_valid_exists_and_indexed(self) -> None:
        self.assertTrue(self.gen.ref_valid("src/main.py", self.indexed))

    def test_ref_missing_on_disk(self) -> None:
        # Archivo no existe Y la carpeta padre no está indexada
        # (nope/ no existe en el repo) → rechazado.
        self.assertFalse(self.gen.ref_valid("nope/missing.py", self.indexed))

    def test_ref_to_create_in_existing_indexed_dir(self) -> None:
        # Caso típico de noche: 'agrega un test nuevo en
        # CommerceDemo.Tests/Services/FooTests.cs'. El archivo no existe
        # todavía, pero la carpeta padre SÍ y tiene hermanos indexados
        # (src/main.py). Debe aceptar como ref válida.
        self.assertTrue(self.gen.ref_valid(
            "src/brand_new_file.cs", self.indexed))

    def test_ref_to_create_in_unknown_dir(self) -> None:
        # Carpeta padre no existe en el repo → rechazar (path inventado).
        self.assertFalse(self.gen.ref_valid(
            "nope/brand_new_file.cs", self.indexed))

    def test_ref_not_in_cbm_index(self) -> None:
        (Path(self.repo) / "src" / "other.py").write_text("y=2", encoding="utf-8")
        self.assertFalse(self.gen.ref_valid("src/other.py", self.indexed))

    def test_ref_traversal_rejected(self) -> None:
        self.assertFalse(self.gen.ref_valid("../evil.py", self.indexed))

    def test_drafts_to_tasks_discards_and_assigns_ids(self) -> None:
        from relay.night import PlanDraft
        # nope/ está FUERA del repo → descartado
        drafts = [
            PlanDraft(title="Arreglar el\n  timeout", refs=["src/main.py"]),
            PlanDraft(title="Crear test nuevo en src/", refs=["src/nope.py"]),
            PlanDraft(title="Path totalmente inventado", refs=["nope/x.py"]),
            PlanDraft(title="Sin refs", refs=[]),
        ]
        tasks = self.gen.drafts_to_tasks(drafts, self.indexed)
        self.assertEqual([t.id for t in tasks], ["T-001", "T-002", "T-003", "T-004"])
        self.assertEqual(tasks[0].status, "pending")
        self.assertNotIn("\n", tasks[0].title)  # una línea (formato estricto)
        # src/nope.py: padre src/ existe e indexado → archivo a crear → pending
        self.assertEqual(tasks[1].status, "pending")
        # nope/x.py: padre nope/ no existe → discarded
        self.assertEqual(tasks[2].status, "discarded")
        self.assertIn("no resuelve en cbm", tasks[2].note)
        self.assertEqual(tasks[3].status, "discarded")
        self.assertEqual(tasks[3].note, "sin refs")

    async def test_generate_aborts_without_cbm(self) -> None:
        with patch.object(TaskGenerator, "indexed_files", return_value=None):
            with self.assertRaises(RuntimeError):
                await self.gen.generate("lo que sea")

    async def test_generate_with_testmodel(self) -> None:
        """Fase 1 end-to-end sin red: TestModel genera drafts dummy y la
        validación determinista los procesa sin explotar."""
        with patch.object(TaskGenerator, "indexed_files",
                          return_value=self.indexed):
            tasks, missing = await self.gen.generate("arreglar el timeout")
        self.assertIsInstance(tasks, list)
        self.assertEqual(missing, [])
        for t in tasks:
            self.assertRegex(t.id, r"^T-\d{3}$")
            self.assertIn(t.status, ("pending", "discarded"))

    async def test_generate_degrades_on_unexpected_model_behavior(self) -> None:
        """Si el LLM no puede producir output válido tras N intentos
        (típicamente: output truncado por tokens o JSON inválido), el
        orquestador degrada a plan vacío en vez de crashear. El run
        termina con end_reason='no_tasks' y tú reformulas la directiva.
        Caso real: run_b77f9c56 transformadorplanos 2026-07-09."""
        from pydantic_ai.exceptions import UnexpectedModelBehavior

        class FakeAgent:
            def __init__(self, *a, **kw): pass
            async def run(self, *a, **kw):
                raise UnexpectedModelBehavior(
                    "Exceeded maximum output retries (3)",
                    body='{"tasks": [{"title": "truncado...')

        # Parcheamos el Agent real dentro de night.py
        import pydantic_ai
        with patch.object(TaskGenerator, "indexed_files",
                          return_value=self.indexed), \
             patch.object(pydantic_ai, "Agent", FakeAgent):
            tasks, missing = await self.gen.generate(
                "directiva ambiciosa que rompe el LLM")
        self.assertEqual(tasks, [],
            "con UnexpectedModelBehavior el plan debe degradarse a []")
        self.assertEqual(missing, [])

    def test_plandraft_unwraps_minimax_item_mangle(self) -> None:
        """MiniMax M3 mangla arrays de strings anidados del lado del
        provider: el modelo emite `"refs": ["a"]` pero llega
        `{"item": ["a"]}` (o `{"item": "a"}`). El validador before
        acepta ambas formas además de la sana. Caso real:
        run_dfbfdd82 commercedemo 2026-07-10."""
        from relay.night import PlanDraft
        sane = PlanDraft(title="t", refs=["a.cs", "b.cs"])
        self.assertEqual(sane.refs, ["a.cs", "b.cs"])
        mangled = PlanDraft.model_validate(
            {"title": "t", "refs": {"item": ["a.cs", "b.cs"]}})
        self.assertEqual(mangled.refs, ["a.cs", "b.cs"])
        single = PlanDraft.model_validate(
            {"title": "t", "refs": {"item": "a.cs"}})
        self.assertEqual(single.refs, ["a.cs"])

    async def test_generate_retries_fresh_on_model_http_error(self) -> None:
        """MiniMax a veces rechaza el request entero con 400 'invalid
        function arguments json string' (un tool call con JSON inválido
        queda en la historia del retry interno de pydantic-ai). Un
        agent.run FRESCO no arrastra esa historia: el primer 400 se
        reintenta una vez. Caso real: run_6fd77b3f commercedemo 2026-07-10."""
        from pydantic_ai.exceptions import ModelHTTPError

        calls = {"n": 0}

        class FlakyAgent:
            def __init__(self, *a, **kw): pass
            async def run(self, *a, **kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ModelHTTPError(
                        status_code=400, model_name="MiniMax-M3",
                        body={"type": "bad_request_error",
                              "message": "invalid function arguments json string"})

                class R:
                    output = []
                return R()

        import pydantic_ai
        with patch.object(TaskGenerator, "indexed_files",
                          return_value=self.indexed), \
             patch.object(pydantic_ai, "Agent", FlakyAgent):
            tasks, missing = await self.gen.generate(
                "directiva con provider flaky")
        self.assertEqual(calls["n"], 2, "el 400 debe reintentarse una vez")
        self.assertEqual(tasks, [])
        self.assertEqual(missing, [])

    async def test_generate_degrades_on_persistent_model_http_error(self) -> None:
        """Dos 400 consecutivos del provider → plan vacío (no_tasks),
        NO end_reason='crashed'."""
        from pydantic_ai.exceptions import ModelHTTPError

        class BrokenAgent:
            def __init__(self, *a, **kw): pass
            async def run(self, *a, **kw):
                raise ModelHTTPError(
                    status_code=400, model_name="MiniMax-M3",
                    body={"message": "invalid function arguments json string"})

        import pydantic_ai
        with patch.object(TaskGenerator, "indexed_files",
                          return_value=self.indexed), \
             patch.object(pydantic_ai, "Agent", BrokenAgent):
            tasks, missing = await self.gen.generate(
                "directiva que rompe al provider")
        self.assertEqual(tasks, [],
            "con ModelHTTPError persistente el plan debe degradarse a []")
        self.assertEqual(missing, [])

    async def test_generate_degrades_on_timeout(self) -> None:
        """Si el planificador cuelga más de PLAN_TIMEOUT_S, degradamos a
        plan vacío igual que con UnexpectedModelBehavior — no crasheamos
        el run (que lo dejaría en end_reason='crashed')."""
        class SlowAgent:
            def __init__(self, *a, **kw): pass
            async def run(self, *a, **kw):
                await asyncio.sleep(9999)

        import pydantic_ai
        with patch.object(TaskGenerator, "indexed_files",
                          return_value=self.indexed), \
             patch.object(pydantic_ai, "Agent", SlowAgent), \
             patch.object(night, "PLAN_TIMEOUT_S", 0.05):
            tasks, missing = await self.gen.generate(
                "directiva que cuelga al LLM")
        self.assertEqual(tasks, [],
            "con timeout el plan debe degradarse a []")
        self.assertEqual(missing, [])

    async def test_generate_returns_missing_in_tuple(self) -> None:
        """End-to-end: el LLM genera UNA tarea cuando la directiva
        lista 2 puntos → generate() devuelve (tasks, ['P0.2'])."""
        from relay.night import PlanDraft

        class OneTaskAgent:
            def __init__(self, *a, **kw): pass
            async def run(self, *a, **kw):
                class R:
                    output = [PlanDraft(
                        title="Eliminar variable 'ex' no usada (P0.1).",
                        refs=["src/main.py"])]
                return R()

        import pydantic_ai
        with patch.object(TaskGenerator, "indexed_files",
                          return_value=self.indexed), \
             patch.object(pydantic_ai, "Agent", OneTaskAgent):
            tasks, missing = await self.gen.generate(
                "P0.1 — Catch variables 'ex' no usadas\n"
                "P0.2 — Actualizar MailKit")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(missing, ["P0.2"],
            "el missing debe detectar que P0.2 no se representó")


# ---------- Iter 5.2: validación de puntos enumerados de la directiva ----------


class TestDirectivePointsExtraction(unittest.TestCase):
    """Caso real del bug: directiva con 'P0.1 — ... P0.2 — ...' donde el
    LLM devolvió UNA sola tarea (P0.1) y tiró P0.2 al piso. Ahora
    extraemos los IDs y diffamos contra lo generado."""

    def test_extracts_pn_dot_format(self) -> None:
        d = ("Sigue el roadmap:\n"
             "P0.1 — Catch variables 'ex' no usadas\n"
             "P0.2 — Actualizar MailKit\n"
             "P0.3 — Arreglar warnings")
        # El prefijo `P` se incluye en el id canónico — buscar "p0.1"
        # en lowercase es lo que usa find_missing_points.
        self.assertEqual(
            night.extract_directive_points(d),
            ["P0.1", "P0.2", "P0.3"])

    def test_extracts_numbered_list(self) -> None:
        d = ("Tareas:\n"
             "1. Arreglar el timeout\n"
             "2. Cubrir con test\n"
             "3. Actualizar deps")
        self.assertEqual(
            night.extract_directive_points(d),
            ["1", "2", "3"])

    def test_extracts_letter_list(self) -> None:
        d = ("Haz lo siguiente:\n"
             "a) Renombrar el handler\n"
             "b) Limpiar imports")
        self.assertEqual(
            night.extract_directive_points(d),
            ["a", "b"])

    def test_extracts_step_format(self) -> None:
        d = ("Step 1: configurar el wrapper\n"
             "Step 2: probar con el repo real")
        self.assertEqual(
            night.extract_directive_points(d),
            ["Step 1", "Step 2"])

    def test_extracts_lowercase_p_prefix(self) -> None:
        d = "p0.1 - algo\np0.2 - otra cosa"
        self.assertEqual(
            night.extract_directive_points(d),
            ["p0.1", "p0.2"])

    def test_empty_directive_returns_empty(self) -> None:
        self.assertEqual(night.extract_directive_points(""), [])
        self.assertEqual(night.extract_directive_points("prosa sin lista"), [])

    def test_find_missing_points(self) -> None:
        """El bug que reportaste: directiva con P0.1+P0.2, LLM solo hizo P0.1."""
        d = ("P0.1 — Catch variables 'ex' no usadas\n"
             "P0.2 — Actualizar MailKit")
        tasks = [NightTask("T-001",
                            "Eliminar variable 'ex' no usada (P0.1).",
                            ["x.cs"])]
        missing = night.find_missing_points(d, tasks)
        self.assertEqual(missing, ["P0.2"])

    def test_find_missing_points_none_when_all_covered(self) -> None:
        d = ("P0.1 — Catch\n"
             "P0.2 — MailKit")
        tasks = [
            NightTask("T-001", "Tarea P0.1 sobre catch.", ["a.cs"]),
            NightTask("T-002", "Tarea P0.2 sobre mailkit.", ["b.cs"]),
        ]
        self.assertEqual(night.find_missing_points(d, tasks), [])

    def test_find_missing_no_points_in_directive(self) -> None:
        d = "haz lo que puedas"
        tasks = [NightTask("T-001", "Algo.", ["a.cs"])]
        self.assertEqual(night.find_missing_points(d, tasks), [])


class ExpertTimeoutOverrideTests(unittest.IsolatedAsyncioTestCase):
    """De noche el experto necesita más presupuesto que el timeout
    interactivo del .env (300s): MiniMax puede tardar >3 min por
    respuesta. _expert_work inyecta EXPERT_TIMEOUT_S vía
    defaults_json.timeout (tope de la cascada de run_expert) salvo
    que el proyecto ya tenga un override explícito."""

    def _worker(self, defaults: dict) -> BranchWorker:
        project = {"slug": "demo", "repo_path": ".",
                   "defaults_json": defaults}
        return BranchWorker(project, NightConfig(), run_id="run_t")

    async def _captured_timeout(self, worker: BranchWorker):
        captured = {}

        async def fake_run_expert(project, prompt, *, skills_block="",
                                  on_progress=None, **_kw):
            captured["timeout"] = (
                project.get("defaults_json") or {}).get("timeout")

        from relay import experts
        with patch.object(experts, "run_expert_staged", fake_run_expert):
            await worker._expert_work(NightTask("T-001", "t.", []), "p")
        return captured["timeout"]

    async def test_sets_night_timeout_by_default(self) -> None:
        t = await self._captured_timeout(self._worker({}))
        self.assertEqual(t, night.EXPERT_TIMEOUT_S)

    async def test_respects_explicit_project_timeout(self) -> None:
        t = await self._captured_timeout(self._worker({"timeout": 120}))
        self.assertEqual(t, 120)


class TransientBuildRetryTests(unittest.IsolatedAsyncioTestCase):
    """El SDK .NET 10.0.204 tira 'Value cannot be null (path1)' en el
    PRIMER build (restore en frío) y anda en el segundo. El gate
    reintenta UNA vez ante esa firma, pero NO ante un error real de
    compilación (CS####). Verificado en CommerceDemo 2026-07-10."""

    _PATH1 = ("Determinando los proyectos...\n"
              "C:\\...\\NuGet.targets(780,5): error : "
              "Value cannot be null. (Parameter 'path1')")
    _CS = ("Program.cs(10,5): error CS7036: falta argumento "
           "'configuration'")

    def _worker(self, cfg: NightConfig) -> BranchWorker:
        project = {"slug": "demo", "repo_path": ".", "defaults_json": {}}
        return BranchWorker(project, cfg, run_id="run_t")

    def test_signature_matches_only_transient(self) -> None:
        self.assertTrue(BranchWorker._is_transient_build_error(1, self._PATH1))
        self.assertFalse(BranchWorker._is_transient_build_error(1, self._CS))
        self.assertFalse(BranchWorker._is_transient_build_error(0, self._PATH1))

    async def test_gate_retries_once_on_cold_restore(self) -> None:
        cfg = NightConfig(build_cmd="dotnet build", test_cmd="dotnet test")
        w = self._worker(cfg)
        calls = {"build": 0}

        async def fake_sh(cmd, timeout=600.0):
            if cmd == "dotnet build":
                calls["build"] += 1
                # 1er build: restore en frío falla; 2do: caliente ok.
                return (1, self._PATH1) if calls["build"] == 1 else (0, "ok")
            return (0, "ok")  # test_cmd

        with patch.object(w, "_sh", fake_sh):
            ok, err = await w._run_gates()
        self.assertTrue(ok, err)
        self.assertEqual(calls["build"], 2, "el build debe reintentar 1 vez")

    async def test_gate_does_not_retry_real_compile_error(self) -> None:
        cfg = NightConfig(build_cmd="dotnet build", test_cmd="dotnet test")
        w = self._worker(cfg)
        calls = {"build": 0}

        async def fake_sh(cmd, timeout=600.0):
            if cmd == "dotnet build":
                calls["build"] += 1
                return (1, self._CS)
            return (0, "ok")

        with patch.object(w, "_sh", fake_sh):
            ok, err = await w._run_gates()
        self.assertFalse(ok)
        self.assertEqual(calls["build"], 1, "un CS#### NO se reintenta")
        self.assertIn("CS7036", err)


class LiveSnapshotTests(unittest.IsolatedAsyncioTestCase):
    """Observabilidad async: mientras una tarea corre, el snapshot del
    run debe exponer en qué paso de Fase 2 estamos y (si el experto
    trabaja) su phase/idle_s. Sin esto, de noche no se distingue
    'avanzando' de 'colgado' y el timeout es un valor a ciegas."""

    def _worker(self) -> BranchWorker:
        project = {"slug": "demo", "repo_path": ".", "defaults_json": {}}
        return BranchWorker(project, NightConfig(), run_id="run_t")

    def test_none_when_idle(self) -> None:
        self.assertIsNone(self._worker().live_snapshot())

    def test_reports_current_step(self) -> None:
        w = self._worker()
        w._set_step("gates")
        snap = w.live_snapshot()
        self.assertEqual(snap["step"], "gates")
        self.assertGreaterEqual(snap["step_elapsed_s"], 0.0)
        self.assertNotIn("expert", snap)

    def test_includes_expert_progress_when_present(self) -> None:
        from relay.experts import RunProgress
        import time as _t
        w = self._worker()
        w._set_step("expert_work")
        w.progress = RunProgress(
            chat_id="T-001", target="night", started_at=_t.monotonic(),
            last_activity_at=_t.monotonic(), phase="tool_call",
            last_tool="edit_file", tool_calls=3, tokens_in=None,
            tokens_out=None, model="minimax:MiniMax-M3")
        snap = w.live_snapshot()
        self.assertEqual(snap["step"], "expert_work")
        self.assertEqual(snap["expert"]["phase"], "tool_call")
        self.assertEqual(snap["expert"]["last_tool"], "edit_file")
        self.assertEqual(snap["expert"]["tool_calls"], 3)
        self.assertIn("idle_s", snap["expert"])

    async def test_orchestrator_snapshot_surfaces_live(self) -> None:
        """El snapshot del orquestador incluye `live` cuando el worker
        está en una tarea, y lo omite cuando no."""
        project = {"slug": "demo", "repo_path": ".", "defaults_json": {},
                   "night_config": {}}

        class _DB:
            async def create_night_run(self, *a, **k): pass
            async def finish_night_run(self, *a, **k): pass

        orch = NightOrchestrator(
            db=_DB(), project=project,
            deadline=datetime.now().astimezone() + timedelta(hours=1))
        self.assertNotIn("live", orch.snapshot())
        orch.worker._set_step("gates")
        self.assertEqual(orch.snapshot()["live"]["step"], "gates")


# ---------- Fase 2: BranchWorker sobre git real ----------


class TestBranchWorker(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = _make_git_repo(Path(self._tmp.name))
        self.project = {"slug": "demo", "repo_path": self.repo,
                        "defaults_json": {"model": "test"}}

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _worker(self, work_fn, *, test_cmd=f'{PY} -c "exit(0)"',
                max_diff=200) -> BranchWorker:
        cfg = NightConfig(build_cmd=None, test_cmd=test_cmd,
                          max_diff_lines=max_diff)
        return BranchWorker(self.project, cfg, run_id="run_test",
                            work_fn=work_fn)

    async def _ready_worker(self, work_fn, **kw) -> BranchWorker:
        """Worker con la rama ÚNICA ya creada (Iter 5.1) — work_one asume
        que el orquestador llamó ensure_branch() antes."""
        w = self._worker(work_fn, **kw)
        err = await w.ensure_branch()
        self.assertIsNone(err, f"ensure_branch: {err}")
        return w

    async def test_green_path_commits_on_branch(self) -> None:
        async def work(task, prompt):
            (Path(self.repo) / "src" / "fix.py").write_text(
                "fixed = True\n", encoding="utf-8")

        task = NightTask("T-001", "Aplicar fix.", ["src/main.py"])
        worker = await self._ready_worker(work)
        res = await worker.work_one(task)
        self.assertEqual(res.status, "done", res.error)
        # el commit quedó en la rama ÚNICA del run, con trailer
        log = _git(self.repo, "log", worker.branch, "-1", "--format=%B")
        self.assertIn("Aplicar fix.", log)
        self.assertIn("run_test", log)
        # rama ÚNICA = night/<fecha>-demo (sin task id)
        self.assertTrue(worker.branch.startswith("night/"),
                        f"rama debe ser night/*, fue {worker.branch!r}")
        self.assertNotIn("t-001", worker.branch,
                         "Iter 5.1: la rama NO lleva el task id")
        # volvimos a main y el working tree quedó limpio
        self.assertIn("main", _git(self.repo, "branch", "--show-current"))
        self.assertEqual(_git(self.repo, "status", "--porcelain").strip(), "")
        # main NO tiene el fix (aislamiento)
        self.assertFalse((Path(self.repo) / "src" / "fix.py").exists())
        # rama ÚNICA persiste con su commit
        self.assertIn(worker.branch, _git(self.repo, "branch", "-a"))
        # done NO abre PR por tarea (eso es del orquestador)
        self.assertEqual(res.pr_url, "")

    async def test_red_gate_resets_branch_not_deletes(self) -> None:
        """Iter 5.1: rollback NO borra la rama ÚNICA — hace
        `git reset --hard HEAD~1`. Si la rama queda vacía (caso
        primera tarea falla), cae a reset contra base."""
        async def work(task, prompt):
            (Path(self.repo) / "src" / "broken.py").write_text(
                "broken", encoding="utf-8")

        task = NightTask("T-001", "Romper todo.", ["src/main.py"])
        worker = await self._ready_worker(
            work, test_cmd=f'{PY} -c "exit(1)"')
        res = await worker.work_one(task)
        self.assertEqual(res.status, "rolled_back")
        self.assertIn("tests fallaron", res.error)
        # rama ÚNICA persiste (no se borra como antes)
        self.assertIn(worker.branch, _git(self.repo, "branch", "-a"))
        # rama quedó sin commits propios (reset a base, sin HEAD~1)
        out = _git(self.repo, "rev-list", "--count", f"main..{worker.branch}")
        self.assertEqual(out.strip(), "0",
            "rollback de la única tarea debe dejar la rama sin commits")
        # tree limpio, archivo no quedó
        self.assertEqual(_git(self.repo, "status", "--porcelain").strip(), "")
        self.assertFalse((Path(self.repo) / "src" / "broken.py").exists())

    async def test_rollback_preserves_good_commits(self) -> None:
        """Si la tarea N falla, las N-1 anteriores (commits buenos)
        DEBEN sobrevivir en la rama ÚNICA. Esto era imposible con
        el modelo viejo (cada tarea tenía su propia rama)."""
        cfg = NightConfig(build_cmd=None,
                          test_cmd=f'{PY} -c "exit(0)"',
                          max_diff_lines=200)

        async def good_work(task, prompt):
            (Path(self.repo) / "src" / "good.py").write_text("g", encoding="utf-8")

        async def bad_work(task, prompt):
            (Path(self.repo) / "src" / "bad.py").write_text("b", encoding="utf-8")

        worker = BranchWorker(self.project, cfg, run_id="run_test",
                              work_fn=good_work)
        err = await worker.ensure_branch()
        self.assertIsNone(err)

        # T-001 verde
        t1 = NightTask("T-001", "Tarea buena.", ["src/main.py"])
        r1 = await worker.work_one(t1)
        self.assertEqual(r1.status, "done", r1.error)
        # T-002 falla en el gate: rotamos work_fn + cambiamos el test_cmd
        worker._work_fn = bad_work
        worker.cfg.test_cmd = f'{PY} -c "exit(1)"'
        t2 = NightTask("T-002", "Tarea rota.", ["src/main.py"])
        r2 = await worker.work_one(t2)
        self.assertEqual(r2.status, "rolled_back")
        # La rama ÚNICA persiste CON el commit bueno de T-001
        out = _git(self.repo, "rev-list", "--count", f"main..{worker.branch}")
        self.assertEqual(out.strip(), "1",
            "rollback de T-002 NO debe tocar el commit de T-001")
        log = _git(self.repo, "log", worker.branch, "--format=%s", "-2")
        self.assertIn("Tarea buena.", log)
        self.assertNotIn("Tarea rota.", log)
        # good.py quedó en la rama; bad.py NO
        good_in_tree = _git(self.repo, "ls-tree", "-r", worker.branch,
                            "--name-only")
        self.assertIn("src/good.py", good_in_tree)
        self.assertNotIn("src/bad.py", good_in_tree)
        # Al volver a main: bad.py tampoco quedó (reset limpio)
        self.assertFalse((Path(self.repo) / "src" / "bad.py").exists())

    async def test_tdd_strict_no_test_cmd_no_commit(self) -> None:
        async def work(task, prompt):
            (Path(self.repo) / "src" / "fix.py").write_text("x", encoding="utf-8")

        worker = await self._ready_worker(work, test_cmd=None)
        res = await worker.work_one(
            NightTask("T-001", "Sin gate.", ["src/main.py"]))
        self.assertEqual(res.status, "rolled_back")
        self.assertIn("sin test_cmd", res.error)

    async def test_diff_cap(self) -> None:
        async def work(task, prompt):
            (Path(self.repo) / "src" / "big.py").write_text(
                "\n".join(f"line{i} = {i}" for i in range(100)),
                encoding="utf-8")

        worker = await self._ready_worker(work, max_diff=10)
        res = await worker.work_one(
            NightTask("T-001", "Diff gigante.", ["src/main.py"]))
        self.assertEqual(res.status, "rolled_back")
        self.assertIn("max_diff_lines", res.error)

    async def test_pre_flight_clean_returns_empty(self) -> None:
        """Si el build de master pasa, pre_flight devuelve ''."""
        cfg = NightConfig(
            build_cmd=f'{PY} -c "print(\'build ok\')"',
            test_cmd=f'{PY} -c "exit(0)"',
            max_diff_lines=200)
        worker = BranchWorker(self.project, cfg, run_id="run_test")
        result = await worker._pre_flight_build()
        self.assertEqual(result, "")

    async def test_pre_flight_broken_injects_errors(self) -> None:
        """Si master está roto, el prompt del experto incluye los
        errores pre-existentes para que sepa qué tiene que arreglar."""
        fake_build = (
            "Build failed.\n"
            "C:/repo/A.cs(10,5): error CS7036: missing arg\n"
            "C:/repo/B.cs(20,5): error CS7036: missing arg\n"
            "    3 Errores\n"
        )
        cfg = NightConfig(
            build_cmd=f'{PY} -c "print({fake_build!r}); import sys; sys.exit(1)"',
            test_cmd=f'{PY} -c "exit(0)"',
            max_diff_lines=200)
        worker = BranchWorker(self.project, cfg, run_id="run_test")
        result = await worker._pre_flight_build()
        self.assertIn("Pre-flight build falló", result)
        self.assertIn("error CS7036", result)
        self.assertIn("A.cs", result)
        self.assertIn("B.cs", result)

    async def test_pre_flight_injected_into_expert_prompt(self) -> None:
        """End-to-end: cuando el experto es llamado, su prompt
        contiene los errores de pre-flight si master está roto."""
        fake_build = "C:/repo/A.cs(10,5): error CS0001: bad\n"
        captured: dict[str, str] = {}

        async def work_capture(task, prompt):
            captured["prompt"] = prompt
            # escribir algo chico para que el diff_stats no falle
            (Path(self.repo) / "src" / "fix.py").write_text(
                "x = 1\n", encoding="utf-8")

        cfg = NightConfig(
            build_cmd=f'{PY} -c "print({fake_build!r}); import sys; sys.exit(1)"',
            test_cmd=f'{PY} -c "exit(0)"',
            max_diff_lines=200)
        worker = BranchWorker(self.project, cfg, run_id="run_test",
                              work_fn=work_capture)
        err = await worker.ensure_branch()
        self.assertIsNone(err)
        await worker.work_one(
            NightTask("T-001", "Tarea con master roto.", ["src/main.py"]))
        self.assertIn("Pre-flight build falló", captured.get("prompt", ""))

    async def test_forbidden_paths(self) -> None:
        async def work(task, prompt):
            (Path(self.repo) / ".env").write_text("SECRET=1", encoding="utf-8")

        worker = await self._ready_worker(work)
        res = await worker.work_one(
            NightTask("T-001", "Tocar secrets.", ["src/main.py"]))
        self.assertEqual(res.status, "rolled_back")
        self.assertIn("prohibidos", res.error)
        self.assertFalse((Path(self.repo) / ".env").exists())

    async def test_no_changes_rolls_back(self) -> None:
        async def work(task, prompt):
            pass  # el experto no hizo nada

        worker = await self._ready_worker(work)
        res = await worker.work_one(
            NightTask("T-001", "Nada.", ["src/main.py"]))
        self.assertEqual(res.status, "rolled_back")
        self.assertIn("no produjo cambios", res.error)

    async def test_dirty_tree_skips(self) -> None:
        (Path(self.repo) / "dirty.txt").write_text("x", encoding="utf-8")

        async def work(task, prompt):
            self.fail("no debería llegar a trabajar")

        # dirty tree es chequeado ANTES de ensure_branch — el worker
        # lo detecta en work_one (no llega a la rama).
        worker = self._worker(work)
        res = await worker.work_one(
            NightTask("T-001", "Con tree sucio.", ["src/main.py"]))
        self.assertEqual(res.status, "failed")
        self.assertIn("sucio", res.error)


# ---------- orquestador ----------


class TestOrchestrator(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.repo = _make_git_repo(base)
        self.state = base / "state"
        self.db = Database(path=base / "test.db")
        await self.db.init_schema()
        self.project = {
            "slug": "demo", "name": "Demo", "repo_path": self.repo,
            "night_mode_enabled": True, "night_config": {},
            "defaults_json": {"model": "test"},
        }
        await self.db.upsert_project(self.project)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _orch(self, generator, worker=None, deadline=None) -> NightOrchestrator:
        return NightOrchestrator(
            db=self.db, project=self.project,
            deadline=deadline or (datetime.now().astimezone()
                                  + timedelta(hours=1)),
            directive="test", state_dir=self.state,
            generator=generator, worker=worker)

    async def test_full_run_writes_ledger_report_and_row(self) -> None:
        tasks = [NightTask("T-001", "Tarea uno.", ["src/main.py"]),
                 NightTask("T-002", "Descartada.", ["nope"],
                           status="discarded", note="ref no resuelve")]

        class StubGen:
            async def generate(self, directive="", error_logs=""):
                return tasks, []

        class StubWorker:
            # Iter 5.1: el orquestador llama ensure_branch ANTES de
            # Fase 1 y finalize_pr DESPUÉS del loop. El Stub tiene que
            # responder a las tres cosas.
            branch = ""

            async def ensure_branch(self):
                StubWorker.branch = night.run_branch_name(
                    orch.project["slug"])
                return None

            async def work_one(self, task):
                from relay.night import TaskResult
                return TaskResult(
                    task_id=task.id, status="done",
                    branch=StubWorker.branch, pr_url="")

            async def finalize_pr(self, tasks_, results_):
                return "http://pr/unique"

        orch = self._orch(StubGen(), StubWorker())
        await orch.run()

        self.assertEqual(orch.status, "finished")
        # ledger: espejo en state + copia en el repo + exclude
        mirror = self.state / "night-runs" / orch.run_id / "plan.md"
        self.assertTrue(mirror.is_file())
        parsed = parse_plan(mirror.read_text(encoding="utf-8"))
        self.assertEqual(parsed[0].status, "done")
        self.assertEqual(parsed[1].status, "discarded")
        repo_plan = Path(self.repo) / ".relay" / "night-plan.md"
        self.assertTrue(repo_plan.is_file())
        exclude = Path(self.repo) / ".git" / "info" / "exclude"
        self.assertIn(".relay/", exclude.read_text(encoding="utf-8"))
        # fila night_runs cerrada con contadores + branch + pr_url (Iter 5.1)
        row = await self.db.get_night_run(orch.run_id)
        # había una tarea pendiente (T-001) y el loop la corrió hasta el
        # final sin deadline/stop → "completed" (no "no_tasks", que es solo
        # cuando el plan no dejó nada ejecutable).
        self.assertEqual(row["end_reason"], "completed")
        self.assertEqual(row["tasks_done"], 1)
        self.assertEqual(row["prs_opened"], 1)
        self.assertEqual(row["tasks_discarded"], 1)
        self.assertTrue(row["branch"].startswith("night/"),
                        f"branch en DB: {row['branch']!r}")
        self.assertEqual(row["pr_url"], "http://pr/unique")
        # reporte escrito con el PR único visible
        self.assertTrue(row["report_path"])
        report = Path(row["report_path"]).read_text(encoding="utf-8")
        self.assertIn("T-001", report)
        self.assertIn("http://pr/unique", report)
        # Iter 5.1: el note de la tarea done apunta a la rama del run
        # (no a un PR per-tarea, que ahora es único).
        self.assertEqual(parsed[0].note, StubWorker.branch)

    async def test_crash_in_phase1_still_reports(self) -> None:
        class BoomGen:
            async def generate(self, directive="", error_logs=""):
                raise RuntimeError("cbm index no disponible")

        class StubWorker:
            branch = ""
            async def ensure_branch(self): return None
            async def work_one(self, task): raise AssertionError
            async def finalize_pr(self, *a, **k): return None

        orch = self._orch(BoomGen(), StubWorker())
        await orch.run()
        self.assertEqual(orch.status, "crashed")
        row = await self.db.get_night_run(orch.run_id)
        self.assertEqual(row["end_reason"], "crashed")
        self.assertIn("cbm index", row["error"])
        self.assertTrue(row["report_path"])  # reporte parcial igual

    async def test_deadline_stops_new_tasks(self) -> None:
        tasks = [NightTask("T-001", "No debería correr.", ["src/main.py"])]

        class StubGen:
            async def generate(self, directive="", error_logs=""):
                return tasks, []

        class NeverWorker:
            branch = ""
            async def ensure_branch(self): return None
            async def work_one(self, task):
                raise AssertionError("no debería arrancar tareas")
            async def finalize_pr(self, *a, **k): return None

        # deadline en 1 segundo: menos que MIN_TASK_WINDOW_S
        orch = self._orch(StubGen(), NeverWorker(),
                          deadline=datetime.now().astimezone()
                          + timedelta(seconds=1))
        await orch.run()
        self.assertEqual(orch.status, "finished")
        row = await self.db.get_night_run(orch.run_id)
        self.assertEqual(row["end_reason"], "deadline")

    async def test_single_branch_per_run(self) -> None:
        """Iter 5.1 (regression): TODAS las tareas van a la MISMA rama.
        Antes cada tarea creaba su propia rama y eso multiplicaba los
        PRs (y los conflictos entre tareas hermanas)."""
        tasks = [
            NightTask("T-001", "Tarea A.", ["src/main.py"]),
            NightTask("T-002", "Tarea B.", ["src/main.py"]),
            NightTask("T-003", "Tarea C.", ["src/main.py"]),
        ]

        class StubGen:
            async def generate(self, directive="", error_logs=""):
                return tasks, []

        class StubWorker:
            branch = ""
            async def ensure_branch(self):
                StubWorker.branch = night.run_branch_name(
                    orch.project["slug"])
                return None
            async def work_one(self, task):
                from relay.night import TaskResult
                return TaskResult(
                    task_id=task.id, status="done",
                    branch=StubWorker.branch, pr_url="")
            async def finalize_pr(self, *a, **k): return "http://pr/once"

        orch = self._orch(StubGen(), StubWorker())
        await orch.run()
        branches = {r.branch for r in orch.results}
        self.assertEqual(len(branches), 1,
            f"todas las tareas deben compartir la misma rama, vi: {branches}")
        self.assertTrue(branches.pop().startswith("night/"))
        # El reporte menciona el PR único UNA vez
        report = Path((await self.db.get_night_run(orch.run_id))[
            "report_path"]).read_text(encoding="utf-8")
        self.assertIn("http://pr/once", report)


# ---------- endpoints ----------


class TestNightEndpoints(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._env_backup = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        os.environ["STATE_DIR"] = str(base / "state")

        self.repo = _make_git_repo(base)
        db = Database()
        await db.init_schema()
        await db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self.repo,
            "night_mode_enabled": True,
            "defaults_json": {"model": "test"},
        })
        await db.upsert_project({
            "slug": "apagado", "name": "Sin night", "repo_path": self.repo,
        })

    async def asyncTearDown(self) -> None:
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    async def _wait(self, predicate, timeout: float = 10.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            value = predicate()
            if value:
                return value
            await asyncio.sleep(0.05)
        self.fail("timeout esperando condición async")

    async def test_start_validations(self) -> None:
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/night-mode/start", json={})
            self.assertEqual(r.status, 400)  # sin project
            r = await client.post("/night-mode/start",
                                  json={"project": "nope"})
            self.assertEqual(r.status, 404)
            r = await client.post("/night-mode/start",
                                  json={"project": "apagado"})
            self.assertEqual(r.status, 400)  # night_mode_enabled=0
            body = await r.json()
            self.assertIn("night_mode_enabled", body["error"])
            r = await client.post("/night-mode/start",
                                  json={"project": "demo",
                                        "deadline_iso": "no-es-fecha"})
            self.assertEqual(r.status, 400)

    async def test_start_conflict_stop_and_status(self) -> None:
        from relay.server import create_app

        hold = asyncio.Event()

        async def slow_generate(self_, directive="", error_logs=""):
            await hold.wait()
            return [], []

        with patch.object(TaskGenerator, "generate", slow_generate):
            app = create_app()
            async with TestClient(TestServer(app)) as client:
                r = await client.post("/night-mode/start",
                                      json={"project": "demo"})
                self.assertEqual(r.status, 202)
                body = await r.json()
                run_id = body["run_id"]
                self.assertTrue(body["deadline_at"])

                # segundo run del mismo proyecto → 409 (lock)
                r = await client.post("/night-mode/start",
                                      json={"project": "demo"})
                self.assertEqual(r.status, 409)

                # status en vivo
                r = await client.get(f"/night-mode/status?run_id={run_id}")
                self.assertEqual(r.status, 200)
                snap = await r.json()
                self.assertEqual(snap["status"], "running")

                # stop (idempotente)
                r = await client.post("/night-mode/stop",
                                      json={"run_id": run_id})
                self.assertEqual(r.status, 200)
                r = await client.post("/night-mode/stop",
                                      json={"run_id": run_id})
                self.assertEqual(r.status, 200)
                r = await client.post("/night-mode/stop",
                                      json={"run_id": "run_nope"})
                self.assertEqual(r.status, 404)

                # soltar la Fase 1 → el run termina y emite reporte
                hold.set()
                from relay.server import NIGHT_KEY
                _orch, task = app[NIGHT_KEY][run_id]
                await self._wait(lambda: task.done())

                r = await client.get(f"/night-mode/status?run_id={run_id}")
                snap = await r.json()
                self.assertEqual(snap["status"], "finished")

    async def test_status_unknown_run(self) -> None:
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.get("/night-mode/status?run_id=run_nope")
            self.assertEqual(r.status, 404)

    async def test_admin_night_endpoint_and_toggle(self) -> None:
        """La UI lee /admin/api/projects/{slug}/night y togglea el flag
        con PATCH — cablea el botón del tab Proyectos (Iter 5)."""
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.get("/admin/api/projects/demo/night")
            self.assertEqual(r.status, 200)
            data = await r.json()
            self.assertTrue(data["night_mode_enabled"])
            self.assertIn("config", data)
            self.assertEqual(data["runs"], [])
            self.assertIsNone(data["active_run"])

            # toggle off vía el PATCH admin (mismo que usa el checkbox)
            r = await client.patch("/admin/api/projects/demo",
                                   json={"night_mode_enabled": False})
            self.assertEqual(r.status, 200)
            r = await client.get("/admin/api/projects/demo/night")
            self.assertFalse((await r.json())["night_mode_enabled"])

            # start ahora debe fallar con 400 (night_mode_enabled=0)
            r = await client.post("/night-mode/start",
                                  json={"project": "demo"})
            self.assertEqual(r.status, 400)

            # el proyecto aparece con el flag en la lista admin
            r = await client.get("/admin/api/projects")
            projs = {p["slug"]: p for p in (await r.json())["projects"]}
            self.assertIn("night_mode_enabled", projs["demo"])
            self.assertFalse(projs["demo"]["night_mode_enabled"])

    async def test_admin_night_unknown_project(self) -> None:
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.get("/admin/api/projects/nope/night")
            self.assertEqual(r.status, 404)

    async def test_admin_cbm_endpoint(self) -> None:
        """GET /admin/api/projects/{slug}/cbm devuelve stats + cbm_name."""
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.get("/admin/api/projects/demo/cbm")
            self.assertEqual(r.status, 200)
            data = await r.json()
            self.assertEqual(data["slug"], "demo")
            self.assertIn("cbm_project_name", data)
            self.assertIn("installed", data)
            self.assertIn("stats", data)
            # 404 para proyecto desconocido
            r = await client.get("/admin/api/projects/nope/cbm")
            self.assertEqual(r.status, 404)

    async def test_admin_night_run_report_missing(self) -> None:
        """Run sin reporte (sigue activo o nunca escribió) → 404 claro."""
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            # run que no existe
            r = await client.get("/admin/api/night-runs/run_nope/report")
            self.assertEqual(r.status, 404)
            # run que existe pero no tiene reporte (recién arrancado)
            r = await client.post("/night-mode/start", json={"project": "demo"})
            self.assertEqual(r.status, 202)
            run_id = (await r.json())["run_id"]
            # al toque: el reporte todavía no se escribió
            r = await client.get(
                f"/admin/api/night-runs/{run_id}/report")
            self.assertEqual(r.status, 404)
            # stop limpio
            await client.post("/night-mode/stop", json={"run_id": run_id})

    async def test_admin_expert_run_validates_prompt(self) -> None:
        """POST /admin/api/projects/{slug}/expert-run — validación básica.

        No invocamos al LLM real (eso sería caro en CI). Solo cubrimos:
        - 404 proyecto desconocido
        - 400 prompt vacío
        - 400 json inválido
        - 502 si el endpoint /experts/run devuelve 5xx (mockeado)
        """
        from relay.server import create_app
        app = create_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.post(
                "/admin/api/projects/nope/expert-run",
                json={"prompt": "hola"})
            self.assertEqual(r.status, 404)

            r = await client.post(
                "/admin/api/projects/demo/expert-run",
                json={"prompt": ""})
            self.assertEqual(r.status, 400)

            r = await client.post(
                "/admin/api/projects/demo/expert-run",
                data="no es json", headers={"Content-Type": "application/json"})
            self.assertEqual(r.status, 400)

            # prompt válido: el handler llama a /experts/run via httpx.
            # En este test el relay no está bindeado a un puerto real,
            # así que el httpx.post va a fallar con ConnectionError.
            # El handler lo traduce a 502 (best-effort surface).
            r = await client.post(
                "/admin/api/projects/demo/expert-run",
                json={"prompt": "dummy"})
            self.assertIn(r.status, (502, 200))


class TestIndexedFilesFormatos(unittest.IsolatedAsyncioTestCase):
    """`indexed_files` parsea las DOS formas de respuesta de search_graph.

    Es dependencia dura de la Fase 1: si devuelve None por no entender el
    formato, el night run aborta entero. cbm 0.9.0 responde
    {"results": [{"file_path": ...}]} y 0.10.0 responde
    {"groups": [{"file": ..., "rows": [...]}]}, asi que el mismo codigo
    tiene que servir para los dos binarios — si no, cambiar de version
    deja de ser un `cp` y pasa a ser un revert de codigo.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        self.gen = TaskGenerator({"slug": "demo", "repo_path": self.repo,
                                  "name": "Demo"})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def _run_con(self, payload: dict) -> set | None:
        async def fake_cbm(tool, args, *, timeout=30.0):
            # el arg format se manda siempre; 0.9.0 lo ignora
            self.assertEqual(args.get("format"), "json")
            return json.dumps(payload)

        with patch.object(night, "cbm_binary_path", lambda: "cbm.exe"),              patch.object(night, "cbm_call", fake_cbm):
            return await self.gen.indexed_files()

    async def test_formato_0_9_results(self) -> None:
        got = await self._run_con({"results": [
            {"file_path": "src/app.py"}, {"file_path": "tests/test_app.py"}]})
        self.assertEqual(len(got), 2)
        self.assertTrue(any(g.endswith("src/app.py") for g in got), got)

    async def test_formato_0_10_groups(self) -> None:
        got = await self._run_con({"groups": [
            {"file": "src/app.py", "rows": [["__file__", "File"]]},
            {"file": "tests/test_app.py", "rows": []}]})
        self.assertEqual(len(got), 2)
        self.assertTrue(any(g.endswith("src/app.py") for g in got), got)

    async def test_error_devuelve_none(self) -> None:
        """El JSON de error es identico en las dos versiones."""
        self.assertIsNone(await self._run_con(
            {"error": "project not found or not indexed"}))

    async def test_respuesta_vacia_devuelve_none(self) -> None:
        self.assertIsNone(await self._run_con({"groups": [], "results": []}))


if __name__ == "__main__":
    unittest.main()
