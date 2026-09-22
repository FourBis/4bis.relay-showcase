"""Tests F1 del plan MCP_REGISTRY: on-demand.

Cover:
  - parse_mcp_flags (--con / --with)
  - resolve_env_refs (secretos por referencia, decisión 4)
  - McpPool: cache, probe con timeout (stdio colgado), reaper por idle,
    rebuild al cambiar la config
  - run_expert: adjunta solo lo pedido (selección explícita) y el
    meta-tool use_capability re-corre con el toolset activado
    (FunctionModel scripteado, sin red ni subprocesos)

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_mcp_ondemand.py -q
"""
from __future__ import annotations
from relay import expert_runner, expert_selection

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from relay import mcp_pool
from relay.db import Database
from relay.experts import parse_mcp_flags, run_expert


class TestParseMcpFlags(unittest.TestCase):
    def test_no_flags(self):
        self.assertEqual(parse_mcp_flags("hola experto"),
                         ("hola experto", []))

    def test_con(self):
        clean, sel = parse_mcp_flags("--con db cuántas órdenes hay?")
        self.assertEqual(clean, "cuántas órdenes hay?")
        self.assertEqual(sel, ["db"])

    def test_with_lista_y_acumulacion(self):
        clean, sel = parse_mcp_flags(
            "--with db,docs mira esto --con browser")
        self.assertEqual(clean, "mira esto")
        self.assertEqual(sel, ["db", "docs", "browser"])

    def test_igual_y_case(self):
        clean, sel = parse_mcp_flags("dale --CON=postgres-demo")
        self.assertEqual(clean, "dale")
        self.assertEqual(sel, ["postgres-demo"])

    def test_no_confunde_otros_flags(self):
        clean, sel = parse_mcp_flags("corre pytest --confirm -v")
        # --confirm NO es --con (el \\w+ del valor no arranca con 'firm '…
        # ojo: --confirm matchea --con + 'firm'? el regex exige separador)
        self.assertEqual(sel, [])
        self.assertEqual(clean, "corre pytest --confirm -v")


class TestResolveEnvRefs(unittest.TestCase):
    def test_resuelve_y_passthrough(self):
        with patch.dict("relay.config._runtime", {"MI_DSN": "postgres://x"}):
            out = mcp_pool.resolve_env_refs(
                {"DSN": "env:MI_DSN", "PLAIN": "v", "N": 1})
        self.assertEqual(out, {"DSN": "postgres://x", "PLAIN": "v", "N": 1})

    def test_falta_variable_levanta(self):
        os.environ.pop("NO_EXISTE_XYZ", None)
        with self.assertRaises(ValueError):
            mcp_pool.resolve_env_refs({"DSN": "env:NO_EXISTE_XYZ"})


class TestToolchainEnv(unittest.TestCase):
    """El SDK de MCP no hereda el entorno: completa lo que le pasamos con
    un whitelist sin ProgramFiles*. Sin esas vars NuGet resuelve el config
    machine-wide a null y todo `dotnet` del experto muere con
    "Value cannot be null. (Parameter 'path1')"."""

    def _env_de(self, cfg_env):
        _, transport = mcp_pool.make_toolset(
            {"transport": "stdio", "command": "python",
             "args": ["-m", "mcp_wrapper"], "env": cfg_env},
            "/repo")
        return transport.env

    def test_pasa_programfiles_y_apaga_node_reuse(self):
        with patch.dict(os.environ, {"ProgramFiles(x86)": r"C:\PF86"}):
            env = self._env_de({})
        self.assertEqual(env["ProgramFiles(x86)"], r"C:\PF86")
        self.assertEqual(env["MSBUILDDISABLENODEREUSE"], "1")

    def test_no_filtra_el_entorno_entero(self):
        # el relay spawnea MCPs de terceros: nada de API keys
        with patch.dict(os.environ, {"MINIMAX_API_KEY": "secreto"}):
            env = self._env_de({})
        self.assertNotIn("MINIMAX_API_KEY", env)

    def test_el_catalogo_gana(self):
        with patch.dict(os.environ, {"ProgramFiles": r"C:\PF"}):
            env = self._env_de({"ProgramFiles": r"D:\otro"})
        self.assertEqual(env["ProgramFiles"], r"D:\otro")


class _FakeToolset:
    def __init__(self, hang: bool = False):
        self.hang = hang
        self.entered = 0
        self.is_running = False

    async def __aenter__(self):
        if self.hang:
            await asyncio.sleep(60)
        self.entered += 1
        return self

    async def __aexit__(self, *a):
        return None


class _FakeTransport:
    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1


def _row(name="pg", updated_at="t1", idle=300):
    return {"name": name, "transport": "stdio", "on_demand": True,
            "command": "x", "args": [], "env": {},
            "idle_timeout_s": idle, "updated_at": updated_at}


class TestMcpPool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.made: list[tuple[_FakeToolset, _FakeTransport]] = []

        def fake_make(cfg, repo_path, *, keep_alive=False):
            ts, tr = _FakeToolset(), _FakeTransport()
            self.made.append((ts, tr))
            return ts, tr

        self._patch = patch.object(mcp_pool, "make_toolset", fake_make)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.pool = mcp_pool.McpPool()

    async def test_acquire_cachea_y_probea(self):
        t1 = await self.pool.acquire(_row(), "/repo")
        t2 = await self.pool.acquire(_row(), "/repo")
        self.assertIs(t1, t2)
        self.assertEqual(len(self.made), 1)
        self.assertEqual(t1.entered, 2)  # probe por acquire

    async def test_config_cambiada_rebuildea(self):
        t1 = await self.pool.acquire(_row(updated_at="t1"), "/repo")
        t2 = await self.pool.acquire(_row(updated_at="t2"), "/repo")
        self.assertIsNot(t1, t2)
        self.assertEqual(self.made[0][1].closed, 1)  # el viejo se mató

    async def test_handshake_colgado_saltea(self):
        with patch.object(mcp_pool, "make_toolset",
                          lambda *a, **k: (_FakeToolset(hang=True),
                                           _FakeTransport())):
            with patch.dict(os.environ, {"FOURBIS_MCP_INIT_TIMEOUT": "0.05"}):
                t = await self.pool.acquire(_row(), "/repo")
        self.assertIsNone(t)
        self.assertEqual(self.pool._entries, {})  # no queda basura

    async def test_reaper_por_idle(self):
        await self.pool.acquire(_row(idle=300), "/repo")
        # recién usado → no reapea
        self.assertEqual(await self.pool.reap_idle(), [])
        # pasado el idle → mata el proceso
        reaped = await self.pool.reap_idle(now=time.monotonic() + 301)
        self.assertEqual(reaped, ["pg"])
        self.assertEqual(self.made[0][1].closed, 1)
        # próximo acquire lo levanta de nuevo
        await self.pool.acquire(_row(), "/repo")
        self.assertEqual(len(self.made), 2)

    async def test_reaper_no_mata_run_en_curso(self):
        ts = await self.pool.acquire(_row(idle=300), "/repo")
        ts.is_running = True  # agente adentro
        reaped = await self.pool.reap_idle(now=time.monotonic() + 9999)
        self.assertEqual(reaped, [])

    async def test_shutdown_cierra_todo(self):
        await self.pool.acquire(_row("a"), "/repo")
        await self.pool.acquire(_row("b"), "/repo")
        await self.pool.shutdown()
        self.assertEqual([tr.closed for _, tr in self.made], [1, 1])


class TestRunExpertCatalog(unittest.IsolatedAsyncioTestCase):
    """run_expert + catálogo: solo adjunta lo pedido; use_capability
    re-corre con el toolset activado. Sin subprocesos: make_toolset
    parcheado a FunctionToolsets."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(path=Path(self._tmp.name) / "test.db")
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo", "repo_path": self._tmp.name})
        self.project = await self.db.get_project("demo")
        await self.db.upsert_mcp_server({
            "name": "postgres-demo", "capability": "db",
            "transport": "stdio", "command": "npx", "enabled": True})
        # Un always-on de referencia. Hasta el 2026-08-16 este rol lo
        # cubría el `4bis-wrapper` que sembraba el boot; ahora que el
        # catálogo arranca vacío (docs/WRAPPER.md), el test lo crea.
        await self.db.upsert_mcp_server({
            "name": "files-global", "capability": "files",
            "transport": "stdio", "command": "python",
            "on_demand": 0, "enabled": True})
        self.built: list[str] = []

        from pydantic_ai.toolsets import FunctionToolset

        def fake_make(cfg, repo_path, *, keep_alive=False):
            self.built.append(cfg["name"])
            ts = FunctionToolset()
            if cfg["name"] == "postgres-demo":
                def pg_query(sql: str) -> str:
                    """Corre SQL."""
                    return "3 órdenes"
                ts.add_function(pg_query, takes_ctx=False)
            return ts, None

        from relay import experts as experts_mod
        self._patch = patch.object(
            expert_selection.mcp_pool_mod, "make_toolset", fake_make)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def _function_model(self):
        """Scripteado: si ve pg_query responde texto; si no, pide la
        capability db vía use_capability."""
        from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
        from pydantic_ai.models.function import AgentInfo, FunctionModel

        def f(messages, info: AgentInfo) -> ModelResponse:
            names = [t.name for t in info.function_tools]
            if "pg_query" in names:
                return ModelResponse(parts=[TextPart("listo con db")])
            if "use_capability" in names:
                return ModelResponse(parts=[
                    ToolCallPart(tool_name="use_capability",
                                 args={"name": "db"})])
            return ModelResponse(parts=[TextPart("sin tools")])

        return FunctionModel(f)

    async def test_sin_seleccion_solo_always_on(self):
        from pydantic_ai.messages import ModelResponse, TextPart
        from pydantic_ai.models.function import FunctionModel
        texto = FunctionModel(
            lambda messages, info: ModelResponse(parts=[TextPart("ok")]))
        with patch("relay.expert_models.build_model", return_value=texto):
            await run_expert(self.project, "hola", db=self.db,
                             model_override="fm")
        self.assertNotIn("postgres-demo", self.built)
        self.assertIn("files-global", self.built)

    async def test_seleccion_explicita_adjunta(self):
        with patch("relay.expert_models.build_model",
                   return_value=self._function_model()):
            result = await run_expert(
                self.project, "cuántas órdenes?", db=self.db,
                model_override="fm", mcp_with=["db"])
        self.assertIn("postgres-demo", self.built)
        self.assertEqual(result["content"], "listo con db")

    async def test_use_capability_re_corre(self):
        """Round 1: el modelo pide use_capability('db') → round 2 con
        postgres-demo adjunto responde."""
        with patch("relay.expert_models.build_model",
                   return_value=self._function_model()):
            result = await run_expert(
                self.project, "cuántas órdenes?", db=self.db,
                model_override="fm")
        self.assertEqual(result["content"], "listo con db")
        self.assertIn("postgres-demo", self.built)

    async def test_spec_test_no_adjunta_mcps(self):
        result = await run_expert(
            self.project, "hola", db=self.db, model_override="test")
        self.assertEqual(self.built, [])
        self.assertIn("content", result)


class TestOptionalToolset(unittest.IsolatedAsyncioTestCase):
    """Guard 2026-07-26: un MCP que no abre no puede matar el run."""

    async def test_aenter_que_tira_degrada_a_cero_tools(self):
        class _Boom:
            label = "boom"

            async def __aenter__(self):
                raise RuntimeError("Failed to initialize server session")

            async def __aexit__(self, *a):
                raise AssertionError("no se sale de lo que no entró")

            async def get_tools(self, ctx):
                raise AssertionError("sin sesión no se piden tools")

        from relay.experts import OptionalToolset
        async with OptionalToolset(wrapped=_Boom()) as live:
            self.assertEqual(await live.get_tools(None), {})
            self.assertIsNone(await live.get_instructions(None))

    async def test_toolset_sano_pasa_derecho(self):
        class _Ok:
            label = "ok"
            exited = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                _Ok.exited = True
                return None

            async def get_tools(self, ctx):
                return {"ping": "tool"}

        from relay.experts import OptionalToolset
        async with OptionalToolset(wrapped=_Ok()) as live:
            self.assertIn("ping", await live.get_tools(None))
        self.assertTrue(_Ok.exited)  # el sano sí cierra su sesión


class TestCatalogPooleaAlwaysOn(unittest.IsolatedAsyncioTestCase):
    """Regresión 2026-07-26: con on_demand=0 el stdio NO pasaba por el
    pool, así que nunca había probe y el handshake fallido reventaba el
    run entero desde el __aenter__ del agente."""

    async def test_stdio_always_on_va_por_el_pool(self):
        from relay.experts import _catalog_toolsets

        row = {"name": "npx-lento", "transport": "stdio", "on_demand": 0,
               "health": "ok"}

        class _Db:
            async def mcp_servers_for_project(self, _pid, **kw):
                return [row]

            async def upsert_mcp_server(self, _row):
                return None

        class _Pool:
            def __init__(self):
                self.calls = []

            async def acquire(self, r, repo_path):
                self.calls.append(r["name"])
                return None  # no levantó → se saltea, sin explotar

        pool = _Pool()
        toolsets, attached, _ = await _catalog_toolsets(
            _Db(), {"id": 1, "repo_path": "/repo"}, set(), pool)
        self.assertEqual(pool.calls, ["npx-lento"])
        self.assertEqual((toolsets, attached), ([], []))


class TestUnoPorCapacidad(unittest.IsolatedAsyncioTestCase):
    """Regresión 2026-08-01: `--con browser` resolvía a DOS MCPs y los
    adjuntaba a los dos. Como declaraban las mismas tools, pydantic-ai
    mataba el run con `UserError: ... conflicts with existing tool ...`.

    Desde el 2026-08-16 el browser es uno solo (`playwright-mcp`), pero
    el desempate sigue vivo para cualquier capacidad que vuelva a tener
    dos implementaciones — por eso los casos usan un segundo browser
    hipotético en vez de borrarse."""

    def test_capacidad_con_dos_mcps_adjunta_uno(self):
        from relay.experts import _one_per_capability

        rows = [{"name": "chrome-devtools-mcp", "capability": "browser",
                 "on_demand": 1},
                {"name": "playwright-mcp", "capability": "browser",
                 "on_demand": 1}]
        self.assertEqual([r["name"] for r in _one_per_capability(rows)],
                         ["chrome-devtools-mcp"])

    def test_always_on_le_gana_al_on_demand(self):
        from relay.experts import _one_per_capability

        # Un always-on no lo puede desplazar un on-demand que entró
        # primero alfabéticamente: el sistema lo da por adjunto.
        rows = [{"name": "aaa-files", "capability": "files", "on_demand": 1},
                {"name": "zzz-files", "capability": "files",
                 "on_demand": 0}]
        self.assertEqual([r["name"] for r in _one_per_capability(rows)],
                         ["zzz-files"])

    def test_el_pedido_por_nombre_le_gana_al_alfabetico(self):
        from relay.experts import _one_per_capability

        # Caso del re-run de use_capability: el humano pidió
        # playwright-mcp por nombre y el modelo sumó la capacidad
        # browser. No le cambiamos el MCP a mitad del run.
        rows = [{"name": "chrome-devtools-mcp", "capability": "browser",
                 "on_demand": 1},
                {"name": "playwright-mcp", "capability": "browser",
                 "on_demand": 1}]
        self.assertEqual(
            [r["name"] for r in
             _one_per_capability(rows, {"playwright-mcp", "browser"})],
            ["playwright-mcp"])

    def test_capacidades_distintas_entran_todas(self):
        from relay.experts import _one_per_capability

        rows = [{"name": "chrome-devtools-mcp", "capability": "browser",
                 "on_demand": 1},
                {"name": "github-mcp", "capability": "github",
                 "on_demand": 1}]
        self.assertEqual(
            sorted(r["name"] for r in _one_per_capability(rows)),
            ["chrome-devtools-mcp", "github-mcp"])


if __name__ == "__main__":
    unittest.main()
