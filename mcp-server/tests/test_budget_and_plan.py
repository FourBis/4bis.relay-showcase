"""Tests de dos mejoras para prompts grandes (caso real: usuario-demo-b pidió
"100% coverage de N controllers" y el run se murió con
`UsageLimitExceeded: request_limit of 50`, perdiendo todo el trabajo).

Cubre:
  Opción 1 — corte por presupuesto gracioso (expert_runner.run_expert):
    - al topar request_limit NO propaga excepción: devuelve dict normal
    - phase_at_end="budget_exceeded", content con mensaje útil
    - messages_json parcial NO vacío (conversación reanudable, ADR-025)

  Opción 4 — descomposición de prompt grande. Desde 2026-08-10 la hace
  el planificador del runner por etapas (experts._run_planner emite
  `DEMASIADO_GRANDE:` y run_expert_staged corta sin ejecutar), no el
  `server._maybe_plan_large_prompt` que se eliminó:
    - señal DEMASIADO_GRANDE → propuesta formateada, ejecutor NO corre
    - follow-up (hay historial) → se ignora la señal y se ejecuta
    - plan normal → el ejecutor corre como siempre

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_budget_and_plan.py -q
"""
from __future__ import annotations
from relay import config, expert_models, expert_planning, expert_runner, expert_selection, expert_staged_runner, expert_stages

import json
import tempfile
import unittest
from unittest.mock import patch

from pydantic_ai import Tool
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.toolsets import FunctionToolset

from relay import experts


# ---------- Opción 1: corte por presupuesto ----------


def _loop_model() -> FunctionModel:
    """FunctionModel que SIEMPRE pide la primera tool → loop infinito
    hasta que pydantic-ai corta por request_limit."""
    def act(messages, info):
        if info.function_tools:
            # `ping` por nombre: [0] puede ser cbm_query si el binario
            # cbm está instalado en la máquina que corre los tests.
            names = [t.name for t in info.function_tools]
            name = "ping" if "ping" in names else names[0]
            return ModelResponse(parts=[ToolCallPart(name, {"x": 1})])
        return ModelResponse(parts=[TextPart("done")])
    return FunctionModel(act)


def _ping_toolset(*_a, **_k) -> list:
    def ping(x: int) -> int:
        """ping"""
        return x
    return [FunctionToolset(tools=[Tool(ping, takes_ctx=False)])]


class TestBudgetExceeded(unittest.IsolatedAsyncioTestCase):
    async def test_budget_cut_is_graceful_and_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = {
                "slug": "demo", "repo_path": tmp,
                # project con id fuerza el path del catálogo F0 (ver
                # audit 2026-07-18: build_toolsets legacy fue removido;
                # los tests ahora patchean _catalog_toolsets).
                "id": 1,
                "system_prompt": "", "mcp_servers": [],
                "defaults_json": {"model": "fake", "timeout": 30},
                "native_tools": [],
            }
            # db falso con el método que _catalog_toolsets necesita.
            class _FakeDb:
                async def mcp_servers_for_project(self, *_a, **_k):
                    return ([{"name": "ping", "capability": "ping",
                              "transport": "http", "url": "x",
                              "on_demand": False, "health": "ok"}], [])
            async def _fake_catalog(*_a, **_k):
                return (_ping_toolset(), [], [])
            with patch.object(expert_models, "build_model", lambda spec: _loop_model()), \
                    patch.object(expert_selection, "_catalog_toolsets", _fake_catalog), \
                    patch.object(config, "expert_request_limit",
                                 lambda: 1):
                # NO debe levantar UsageLimitExceeded.
                r = await expert_runner.run_expert(project, "haz todo", db=_FakeDb())

            self.assertEqual(r["phase_at_end"], "budget_exceeded")
            # Mensaje útil (no stacktrace crudo).
            self.assertIn("presupuesto", r["content"].lower())
            # "contin" y no "continu": el nudge es "continúa" (con tilde).
            self.assertIn("contin", r["content"].lower())
            # Historial parcial rescatado → conversación reanudable.
            self.assertTrue(r["messages_json"])
            msgs = json.loads(r["messages_json"])
            self.assertGreater(len(msgs), 0)


# ---------- 2026-07-20c: auto-continue de presupuesto ----------


def _finishing_model(needed_returns: int) -> FunctionModel:
    """FunctionModel que trabaja de verdad: pide la tool hasta acumular
    `needed_returns` resultados en el historial y recién ahí responde.
    Con request_limit chico, solo termina si el auto-continue re-entra
    con el historial rescatado (símil tarea real de 69 tools)."""
    def act(messages, info):
        seen = sum(
            1 for m in messages for p in getattr(m, "parts", [])
            if isinstance(p, ToolReturnPart))
        if seen >= needed_returns:
            return ModelResponse(parts=[TextPart("listo: tarea completa")])
        # `ping` por nombre: function_tools[0] puede ser cbm_query si el
        # binario cbm está instalado en la máquina que corre los tests.
        names = [t.name for t in info.function_tools]
        name = "ping" if "ping" in names else names[0]
        return ModelResponse(parts=[ToolCallPart(name, {"x": 1})])
    return FunctionModel(act)


class TestBudgetAutoContinue(unittest.IsolatedAsyncioTestCase):
    def _project(self, tmp: str) -> dict:
        return {
            "slug": "demo", "repo_path": tmp, "id": 1,
            "system_prompt": "", "mcp_servers": [],
            # timeout > ROUND_MIN_S (30s): el auto-continue exige ese
            # margen para entrar a otra tanda (has_time).
            "defaults_json": {"model": "fake", "timeout": 300},
            "native_tools": [],
        }

    async def test_working_run_chains_legs_and_finishes(self) -> None:
        """Un run que PROGRESA cruza el request_limit y termina igual
        (antes: moría a los 50 pasos con el ⚠️ de presupuesto)."""
        async def _fake_catalog(*_a, **_k):
            return (_ping_toolset(), [], [])
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(expert_models, "build_model",
                              lambda spec: _finishing_model(5)), \
                    patch.object(expert_selection, "_catalog_toolsets", _fake_catalog), \
                    patch.object(config, "expert_request_limit",
                                 lambda: 2), \
                    patch.object(config, "expert_max_legs",
                                 lambda: 5):
                r = await expert_runner.run_expert(
                    self._project(tmp), "haz todo", db=object())
            self.assertIn("listo", r["content"])
            self.assertNotEqual(r["phase_at_end"], "budget_exceeded")
            self.assertGreater(r["legs"], 1,
                "no encadenó tandas: el auto-continue no corrió")
            # 5 tool calls reales acumuladas entre tandas.
            self.assertEqual(r["tool_calls"], 5)

    async def test_tool_loop_is_not_extended(self) -> None:
        """El gate anti-loop: la misma tool con los mismos args 8+ veces
        seguidas NO gana tandas extra — corta en la primera con el
        mensaje de presupuesto + diagnóstico de loop."""
        async def _fake_catalog(*_a, **_k):
            return (_ping_toolset(), [], [])
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(expert_models, "build_model",
                              lambda spec: _loop_model()), \
                    patch.object(expert_selection, "_catalog_toolsets", _fake_catalog), \
                    patch.object(config, "expert_request_limit",
                                 lambda: 10):
                r = await expert_runner.run_expert(
                    self._project(tmp), "haz todo", db=object())
            self.assertEqual(r["phase_at_end"], "budget_exceeded")
            self.assertEqual(r["legs"], 1,
                "extendió tandas a un run que estaba loopeando")
            self.assertIn("loop", r["content"].lower())


def _finishing_model_variado(needed_returns: int) -> FunctionModel:
    """Como `_finishing_model`, pero con args DISTINTOS en cada llamada.

    Hace falta para los tests del supervisor: `_finishing_model` repite
    `{"x": 1}`, así que en cuanto se encadenan suficientes tandas la
    ventana de `_tool_loop_detected` se llena de llamadas idénticas y el
    run corta por loop antes de que el supervisor pueda decidir nada.

    Y es más fiel al caso que motivó esto: el run de sample-app hizo 172 tool
    calls VARIADAS — por eso el gate anti-loop no disparó y por eso hacía
    falta un supervisor que mirara el rumbo y no la repetición.
    """
    def act(messages, info):
        seen = sum(
            1 for m in messages for p in getattr(m, "parts", [])
            if isinstance(p, ToolReturnPart))
        if seen >= needed_returns:
            return ModelResponse(parts=[TextPart("listo: tarea completa")])
        names = [t.name for t in info.function_tools]
        name = "ping" if "ping" in names else names[0]
        return ModelResponse(parts=[ToolCallPart(name, {"x": seen + 1})])
    return FunctionModel(act)


class TestSupervisorDeMediaCorrida(unittest.IsolatedAsyncioTestCase):
    """`on_leg_boundary`: el verificador mira en cada corte de tanda.

    Motivación (chat 9d0fef5e, sample-app): el planificador pidió UNA llamada
    a `cbm_query`, el ejecutor hizo 172 tool calls, y el verificador —que
    corría una sola vez, al final— lo detectó a los 20 minutos. El desvío
    ya era visible en el primer corte de tanda, al paso 64.
    """

    def _project(self, tmp: str) -> dict:
        return {
            "slug": "demo", "repo_path": tmp, "id": 1,
            "system_prompt": "", "mcp_servers": [],
            "defaults_json": {"model": "fake", "timeout": 300},
            "native_tools": [],
        }

    async def _correr(self, tmp: str, *, supervisor, needed=9, max_legs=2,
                     hard=12):
        async def _fake_catalog(*_a, **_k):
            return (_ping_toolset(), [], [])
        with patch.object(expert_models, "build_model",
                          lambda spec: _finishing_model_variado(needed)), \
                patch.object(expert_selection, "_catalog_toolsets", _fake_catalog), \
                patch.object(config, "expert_request_limit",
                             lambda: 2), \
                patch.object(config, "expert_max_legs",
                             lambda: max_legs), \
                patch.object(config, "expert_max_legs_hard",
                             lambda: hard):
            return await expert_runner.run_expert(
                self._project(tmp), "haz todo", db=object(),
                on_leg_boundary=supervisor)

    async def test_off_plan_corta_en_el_primer_borde(self) -> None:
        """Un veredicto de desvío corta ahí, no 50 pasos después."""
        vistas = []

        async def supervisor(parcial):
            vistas.append(parcial)
            return "el plan pedía leer un archivo, estás levantando servicios"

        with tempfile.TemporaryDirectory() as tmp:
            r = await self._correr(tmp, supervisor=supervisor)
        self.assertEqual(r["phase_at_end"], "off_plan")
        self.assertEqual(r["legs"], 1, "siguió después del corte por desvío")
        # El humano tiene que leer el porqué y saber que no perdió nada.
        self.assertIn("desvié del plan", r["content"])
        self.assertIn("levantando servicios", r["content"])
        self.assertTrue(r["messages_json"], "tiró el trabajo hecho")
        # ...pero el working set del desvío NO se conserva (2026-08-23).
        # Si viajara entero, el turno siguiente arrancaría leyendo el
        # descarrilamiento y se descarrilaría igual — que es el loop que
        # se midió en sample-app. Sobreviven el pedido y el texto final.
        self.assertNotIn("tool-call", r["messages_json"])
        self.assertNotIn("tool-return", r["messages_json"])
        self.assertIn("user-prompt", r["messages_json"])
        # El supervisor recibe con qué juzgar.
        self.assertEqual(len(vistas), 1)
        self.assertEqual(vistas[0]["leg"], 1)
        self.assertGreater(vistas[0]["tool_calls"], 0)
        self.assertTrue(vistas[0]["messages_json"])

    async def test_supervisor_conforme_deja_terminar_la_tarea(self) -> None:
        """Lo que se pidió: que no quede a medias.

        Con `max_legs=2` y una tarea que necesita ~5 tandas, SIN supervisor
        el run se corta incompleto (ver el test de abajo). Con un supervisor
        que no objeta, `max_legs` deja de ser el techo y la tarea termina.
        """
        async def supervisor(_parcial):
            return ""

        with tempfile.TemporaryDirectory() as tmp:
            r = await self._correr(tmp, supervisor=supervisor)
        self.assertIn("listo", r["content"])
        self.assertNotEqual(r["phase_at_end"], "budget_exceeded")
        self.assertGreater(r["legs"], 2,
            "no pasó de max_legs: el supervisor no habilitó las tandas extra")
        self.assertEqual(r["tool_calls"], 9)

    async def test_sin_supervisor_max_legs_sigue_siendo_el_techo(self) -> None:
        """El diferencial del test de arriba: sin supervisor, se trunca."""
        with tempfile.TemporaryDirectory() as tmp:
            r = await self._correr(tmp, supervisor=None)
        self.assertEqual(r["phase_at_end"], "budget_exceeded")
        self.assertEqual(r["legs"], 2)
        self.assertNotIn("listo", r["content"])

    async def test_el_techo_duro_acota_al_supervisor_dormido(self) -> None:
        """Un supervisor que nunca objeta no puede dar tandas infinitas."""
        async def supervisor(_parcial):
            return ""

        with tempfile.TemporaryDirectory() as tmp:
            # La tarea necesita ~13 tandas; el techo duro corta en 3.
            r = await self._correr(tmp, supervisor=supervisor, needed=25,
                                   max_legs=2, hard=3)
        self.assertEqual(r["legs"], 3)
        self.assertEqual(r["phase_at_end"], "budget_exceeded")

    async def test_supervisor_que_explota_deja_terminar_igual(self) -> None:
        """Un verificador caído no puede costar el trabajo hecho.

        Sin veredicto no hay objeción, así que el run sigue Y conserva el
        techo extendido: degradar a `max_legs` truncaría la tarea por una
        falla del proveedor del verificador, que es el motivo más ajeno
        posible a la tarea misma.
        """
        async def supervisor(_parcial):
            raise RuntimeError("el proveedor del verificador se cayó")

        with tempfile.TemporaryDirectory() as tmp:
            r = await self._correr(tmp, supervisor=supervisor)
        self.assertIn("listo", r["content"], "truncó por una caída ajena")
        self.assertGreater(r["legs"], 2)
        self.assertTrue(r["messages_json"])


# ---------- Fase 1: tope de subdivisión automática (2026-09-02) ----------
#
# Medido sobre 238 nodos de grafo (SIN supervisor — `orquestador.py`
# llama a `run_expert` sin `on_leg_boundary`): por `tool_calls`
# acumuladas, 0-200 → 0-4% se cortan; 200-250 → 36%; 250+ → 85%.
#
# El tope aplica SOLO cuando `on_leg_boundary is None`. Corregido dos
# veces el mismo día: un intento anterior lo midió en TANDAS (mal
# inferidas) y otro lo aplicaba siempre, con o sin supervisor — pero
# separando la medición por camino, los CHATS staged (con supervisor)
# terminan bien 60% de las veces incluso pasando 250 tool calls (sample-app
# fue uno: 172 tool calls, con supervisor). La extensión por supervisor
# (`max_legs_hard`, ver `TestSupervisorDeMediaCorrida` arriba) sigue
# intacta y esos 4 tests pasan SIN tocarlos — no hay conflicto porque
# este tope directamente no se evalúa cuando hay supervisor.


class TestBudgetSplit(unittest.IsolatedAsyncioTestCase):
    def _project(self, tmp: str) -> dict:
        return {
            "slug": "demo", "repo_path": tmp, "id": 1,
            "system_prompt": "", "mcp_servers": [],
            "defaults_json": {"model": "fake", "timeout": 300},
            "native_tools": [],
        }

    async def _correr(self, tmp: str, *, needed, request_limit,
                       max_legs=1, max_legs_hard=12, on_leg_boundary=None):
        async def _fake_catalog(*_a, **_k):
            return (_ping_toolset(), [], [])
        with patch.object(expert_models, "build_model",
                          lambda spec: _finishing_model_variado(needed)), \
                patch.object(expert_selection, "_catalog_toolsets", _fake_catalog), \
                patch.object(config, "expert_request_limit",
                             lambda: request_limit), \
                patch.object(config, "expert_max_legs",
                             lambda: max_legs), \
                patch.object(config, "expert_max_legs_hard",
                             lambda: max_legs_hard):
            return await expert_runner.run_expert(
                self._project(tmp), "haz todo", db=object(),
                on_leg_boundary=on_leg_boundary)

    async def test_justo_debajo_del_tope_no_corta(self) -> None:
        """Justo por debajo del tope: corta igual por el mecanismo viejo
        (`max_legs`=1), pero NO con `budget_split`.

        El tope sale de `config.expert_max_tool_calls()` y NO se escribe
        a mano: estaba fijo en 249/250 y al bajar el default a 150 el
        2026-09-07 el test quedo afirmando el umbral viejo. Atado al
        valor real, el par de tests sobrevive al proximo ajuste.
        """
        from relay import config
        tope = config.expert_max_tool_calls()
        with tempfile.TemporaryDirectory() as tmp:
            r = await self._correr(tmp, needed=100_000,
                                   request_limit=tope - 1)
        self.assertEqual(r["tool_calls"], tope - 1)
        self.assertNotEqual(r["phase_at_end"], "budget_split")
        self.assertEqual(r["phase_at_end"], "budget_exceeded")

    async def test_en_el_tope_corta_con_fase_propia(self) -> None:
        """En el tope exacto, sin supervisor: corta con `budget_split`,
        no `budget_exceeded` ni `off_plan`. El tope sale de config."""
        from relay import config
        tope = config.expert_max_tool_calls()
        with tempfile.TemporaryDirectory() as tmp:
            r = await self._correr(tmp, needed=100_000, request_limit=tope)
        self.assertEqual(r["tool_calls"], tope)
        self.assertEqual(r["phase_at_end"], "budget_split")
        self.assertIn("lote", r["content"].lower())

    async def test_con_supervisor_pasar_250_no_corta_decide_el_verificador(
            self) -> None:
        """El caso sample-app, generalizado: con supervisor conforme, un run
        que pasa las 250 tool calls TERMINA en vez de cortarse — el
        tope de subdivisión no se evalúa cuando hay supervisor."""
        async def supervisor_conforme(_parcial):
            return ""

        with tempfile.TemporaryDirectory() as tmp:
            r = await self._correr(
                tmp, needed=260, request_limit=50, max_legs=2,
                max_legs_hard=10, on_leg_boundary=supervisor_conforme)
        self.assertIn("listo", r["content"])
        self.assertEqual(r["tool_calls"], 260)
        self.assertNotEqual(r["phase_at_end"], "budget_split")
        self.assertGreater(r["legs"], 2,
            "no se extendió más allá de max_legs: la extensión por "
            "supervisor dejó de funcionar")

    async def test_el_corte_rescata_el_historial(self) -> None:
        """Reanudable, igual que `budget_exceeded`: el historial parcial
        no se pierde."""
        with tempfile.TemporaryDirectory() as tmp:
            r = await self._correr(tmp, needed=100_000, request_limit=250)
        self.assertEqual(r["phase_at_end"], "budget_split")
        self.assertTrue(r["messages_json"])
        msgs = json.loads(r["messages_json"])
        self.assertGreater(len(msgs), 0)

    def test_budget_split_es_fase_incompleta(self) -> None:
        """El nodo tiene que quedar fallado y reanudable, no `hecho`."""
        from relay import grafo
        self.assertIn("budget_split", grafo.FASES_INCOMPLETAS)


# ---------- Opción 4: descomposición ----------


class TestLargePromptDecomposition(unittest.IsolatedAsyncioTestCase):
    """El planificador por etapas absorbió la Opción 4.

    Ya no hay un segundo planificador con regex + TaskGenerator: la
    señal la emite el mismo turno de planificación que se paga siempre.
    """

    BIG = "DEMASIADO_GRANDE:\n1. Tests a FooController\n2. Tests a BarController"
    PROJ = {
        "slug": "demo", "repo_path": "x",
        "system_prompt": "", "mcp_servers": [], "native_tools": [],
        "defaults_json": {},
    }

    @staticmethod
    def _executor_that_must_not_run():
        async def _fake(proj, user, **kwargs):
            raise AssertionError("el ejecutor no debe correr con DEMASIADO_GRANDE")
        return _fake

    async def test_too_large_proposes_without_executing(self) -> None:
        async def fake_planner(**kwargs):
            return self.BIG, {}, ""

        with patch.object(expert_stages, "_run_planner", fake_planner), \
             patch.object(expert_runner, "run_expert",
                          self._executor_that_must_not_run()):
            r = await expert_staged_runner.run_expert_staged(
                self.PROJ, "coverage 100% de todos los controllers")

        self.assertEqual(r["phase_at_end"], "planned")
        self.assertIn("Tests a FooController", r["content"])
        self.assertIn("todavía no ejecuté nada", r["content"])
        # El turno se guarda en el hilo. Antes viajaba vacío y server.py
        # no persiste lo vacío, así que la descomposición no entraba al
        # historial del modelo — y el mensaje SIGUIENTE a una
        # descomposición es casi siempre "dale" o "realiza el plan", que
        # sin contexto no significan nada. Medido el 24/8: el planificador
        # recibió "Realiza el plan" con el historial en cero, dijo que el
        # pedido estaba vacío, y el ejecutor adivinó 19 min / 1,96M tokens.
        from pydantic_ai.messages import (
            ModelMessagesTypeAdapter, ModelRequest, ModelResponse,
            TextPart, UserPromptPart)
        self.assertTrue(r["messages_json"], "la descomposición no se guardó")
        msgs = ModelMessagesTypeAdapter.validate_json(r["messages_json"])
        pedidos = [p.content for m in msgs if isinstance(m, ModelRequest)
                   for p in m.parts if isinstance(p, UserPromptPart)]
        textos = [p.content for m in msgs if isinstance(m, ModelResponse)
                  for p in m.parts if isinstance(p, TextPart)]
        self.assertEqual(pedidos, ["coverage 100% de todos los controllers"])
        self.assertIn("Tests a FooController", textos[0])
        # No hay verificación ni documentación de algo que no se ejecutó.
        self.assertEqual(r["verifier_verdict"], "")
        self.assertEqual(r["doc"], "")

    async def test_followup_ignores_the_signal(self) -> None:
        """Un pedido que RETOMA no se secuestra: se ejecuta.

        Lo que apaga la señal es que el mensaje sea una continuación
        ("sigue con la 2"), no que el hilo tenga historial — ver
        `test_pedido_nuevo_en_un_hilo_si_arma_grafo`.
        """
        seen = {}

        async def fake_planner(**kwargs):
            seen["is_followup"] = kwargs.get("is_followup")
            seen["es_continuacion"] = kwargs.get("es_continuacion")
            return self.BIG, {}, ""

        async def fake_executor(proj, user, **kwargs):
            seen["executed"] = True
            return {
                "content": "hecho", "model": "test",
                "tokens_in": 1, "tokens_out": 1, "tool_calls": 0,
                "duration_ms": 1, "messages_json": "[]",
                "phase_at_end": "writing", "last_tool": None,
                "legs": 1, "steers": 0, "steer_texts": [],
                "progress_events": [],
            }

        async def fake_verifier(**kwargs):
            return "complete", "ok", {}, ""

        with patch.object(expert_stages, "_run_planner", fake_planner), \
             patch.object(expert_runner, "run_expert", fake_executor), \
             patch.object(expert_stages, "_run_verifier", fake_verifier):
            r = await expert_staged_runner.run_expert_staged(
                self.PROJ, "sigue con la 2",
                message_history_json="HISTORIAL")

        self.assertTrue(seen.get("executed"))
        self.assertTrue(seen.get("is_followup"))
        self.assertTrue(seen.get("es_continuacion"))
        self.assertEqual(r["phase_at_end"], "writing")

    async def test_pedido_nuevo_en_un_hilo_si_arma_grafo(self) -> None:
        """El agujero que tapó el 23/8: `task_graphs` estaba vacía.

        Un pedido nuevo y grande que entra por un hilo abierto es
        follow-up, y con el guard viejo (`not is_followup`) la señal se
        ignoraba SIEMPRE — el grafo no se armó nunca en producción y el
        pedido gigante se ejecutaba hasta morir por `off_plan`.
        """
        seen = {}

        async def fake_planner(**kwargs):
            seen["es_continuacion"] = kwargs.get("es_continuacion")
            return self.BIG, {}, ""

        with patch.object(expert_stages, "_run_planner", fake_planner), \
             patch.object(expert_runner, "run_expert",
                          self._executor_that_must_not_run()):
            r = await expert_staged_runner.run_expert_staged(
                self.PROJ,
                "Genera un manual de usuario de la aplicación en formato "
                "md, con capturas de pantalla, todos los pasos deben estar "
                "documentados.",
                message_history_json="HISTORIAL")

        self.assertFalse(seen.get("es_continuacion"))
        # `planned` es lo que server.py convierte en grafo.
        self.assertEqual(r["phase_at_end"], "planned")

    def test_format_decomposition_strips_prefix(self) -> None:
        out = expert_planning._format_decomposition(self.BIG)
        self.assertNotIn("DEMASIADO_GRANDE", out)
        self.assertIn("1. Tests a FooController", out)
        self.assertIn("night run", out)

    async def test_normal_plan_still_executes(self) -> None:
        seen = {}

        async def fake_planner(**kwargs):
            return "1. leer main.py\n2. editar", {}, ""

        async def fake_executor(proj, user, **kwargs):
            seen["executed"] = True
            return {
                "content": "hecho", "model": "test",
                "tokens_in": 1, "tokens_out": 1, "tool_calls": 0,
                "duration_ms": 1, "messages_json": "[]",
                "phase_at_end": "writing", "last_tool": None,
                "legs": 1, "steers": 0, "steer_texts": [],
                "progress_events": [],
            }

        async def fake_verifier(**kwargs):
            return "complete", "ok", {}, ""

        with patch.object(expert_stages, "_run_planner", fake_planner), \
             patch.object(expert_runner, "run_expert", fake_executor), \
             patch.object(expert_stages, "_run_verifier", fake_verifier):
            r = await expert_staged_runner.run_expert_staged(self.PROJ, "edita main.py")

        self.assertTrue(seen.get("executed"))
        self.assertEqual(r["phase_at_end"], "writing")


class TestPlanBasura(unittest.IsolatedAsyncioTestCase):
    """Un plan que no es un plan no puede cortar el run (2026-08-19).

    El daño no era quedarse sin plan —el ejecutor trabaja sin él— sino
    que el supervisor de media corrida tomaba el ruido como contrato y
    cortaba por `off_plan`. En sample-app frenó runs en los pasos 78, 100,
    106 y 150 mientras el ejecutor hacía lo que el humano pidió.
    """

    # Salida real del planificador, de `chats.stages_json`.
    BASURA = 'cbm_query({"tool": "search_graph", "name_pattern": "docker-compose"})'
    PROJ = {
        "slug": "demo", "repo_path": "x",
        "system_prompt": "", "mcp_servers": [], "native_tools": [],
        "defaults_json": {},
    }

    @staticmethod
    def _executor(seen):
        async def _fake(proj, user, **kwargs):
            seen["system_extra"] = kwargs.get("system_extra") or ""
            seen["on_leg_boundary"] = kwargs.get("on_leg_boundary")
            return {
                "content": "hecho", "model": "test",
                "tokens_in": 1, "tokens_out": 1, "tool_calls": 3,
                "duration_ms": 1, "messages_json": "[]",
                "phase_at_end": "writing", "last_tool": None,
                "legs": 1, "steers": 0, "steer_texts": [],
                "progress_events": [],
            }
        return _fake

    async def _correr(self, plan):
        seen = {}

        async def fake_planner(**kwargs):
            return plan, {}, ""

        async def fake_verifier(**kwargs):
            return "complete", "ok", {}, ""

        with patch.object(expert_stages, "_run_planner", fake_planner), \
             patch.object(expert_runner, "run_expert", self._executor(seen)), \
             patch.object(expert_stages, "_run_verifier", fake_verifier):
            r = await expert_staged_runner.run_expert_staged(self.PROJ, "documenta la app")
        return r, seen

    async def test_no_se_inyecta_ni_se_supervisa(self) -> None:
        r, seen = await self._correr(self.BASURA)
        # 1. El ruido no llega al ejecutor como "Plan a ejecutar".
        self.assertNotIn("cbm_query", seen["system_extra"])
        self.assertNotIn("Plan a ejecutar", seen["system_extra"])
        # 2. Y sobre todo: sin supervisor no hay corte por `off_plan`.
        self.assertIsNone(seen["on_leg_boundary"])
        # 3. Queda auditable en vez de desaparecer en un log.
        self.assertIn("planner", r["stage_errors"])
        self.assertEqual(r["plan"], "")
        # 4. El run termina normal: descartar el plan no rompe nada.
        self.assertEqual(r["phase_at_end"], "writing")

    async def test_un_plan_de_verdad_sigue_supervisado(self) -> None:
        """El guard no puede apagar el supervisor cuando SÍ hay plan."""
        r, seen = await self._correr("1. leer main.py\n2. editar")
        self.assertIn("Plan a ejecutar", seen["system_extra"])
        self.assertIsNotNone(seen["on_leg_boundary"])
        self.assertEqual(r["stage_errors"], {})


if __name__ == "__main__":
    unittest.main()
