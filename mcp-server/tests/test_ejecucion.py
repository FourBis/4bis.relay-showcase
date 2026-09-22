"""Poder de ejecución del experto (2026-08-16).

Tres límites que se pisaban entre sí y hacían fallar tool calls
legítimas — ver docs/EJECUCION.md:

  - el techo de una tool-call estaba atado al watchdog de idle
    (`idle * 0.9` = 162s), así que un build o una suite de tests moría a
    los 162s y el experto reintentaba a ciegas;
  - el watchdog contaba una tool en vuelo como "experto idle" y mataba
    el run entero a los 180s;
  - el cap del resultado se quedaba con la CABEZA, y en la salida de un
    build el veredicto (`exit=1`, el traceback) vive al final.
"""
from __future__ import annotations
from relay import (config, expert_history, expert_iteration, expert_result,
                   expert_runner, expert_stage_prompts, expert_toolsets)

import asyncio
import contextlib
import os
import time
from unittest.mock import patch

import pytest
from pydantic_ai.exceptions import ModelRetry

from relay import config, experts, mcp_pool
from relay.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    await d.init_schema()
    return d


# ---------- 1. cap por tool, con cola ----------


def test_cap_general_sigue_quedandose_con_la_cabeza():
    """El resto de las tools se paginan solas: la cabeza + el aviso basta."""
    texto = "a" * 20_000
    out = expert_history._cap_text(texto, 16_000)
    assert out.startswith("a" * 100)
    assert "TRUNCADO" in out
    assert len(out) < len(texto)


def test_cap_de_consola_conserva_el_final():
    """El caso que motivó el cambio: el exit code vive al final."""
    salida = ("banner del build\n" + "x" * 60_000 + "\nFAILED: 3 tests\n(exit=1)")
    out = expert_history._cap_text(salida, 48_000, keep_tail=12_000)
    assert out.startswith("banner del build")
    assert "(exit=1)" in out
    assert "FAILED: 3 tests" in out
    assert "TRUNCADO" in out


def test_cap_de_consola_no_pasa_el_presupuesto():
    salida = "y" * 200_000
    out = expert_history._cap_text(salida, 48_000, keep_tail=12_000)
    # cap + el texto del marcador; nunca el original entero.
    assert len(out) < 49_000


def test_caps_for_run_shell_es_mas_grande():
    cap_shell, tail_shell = expert_history._caps_for("run_shell")
    cap_otro, tail_otro = expert_history._caps_for("read_file")
    assert cap_shell > cap_otro
    assert tail_shell > 0 and tail_otro == 0


def test_cap_tool_result_usa_el_cap_de_la_tool():
    salida = "z" * 40_000 + "\n(exit=1)"
    capeado = expert_history._cap_tool_result(salida, tool_name="run_shell")
    assert "(exit=1)" in capeado          # cabe entero en el cap de consola
    otro = expert_history._cap_tool_result(salida, tool_name="read_file")
    assert "(exit=1)" not in otro         # el cap general lo corta antes
    assert "TRUNCADO" in otro


def test_cap_tool_result_sin_nombre_usa_el_default():
    """Los callers viejos (y los tests) no pasan tool_name."""
    assert expert_history._cap_tool_result("x" * 100) == "x" * 100
    assert "TRUNCADO" in expert_history._cap_tool_result("x" * 20_000)


def test_cap_de_texto_corto_no_toca_nada():
    for name in ("run_shell", "read_file", ""):
        assert expert_history._cap_tool_result("ok", tool_name=name) == "ok"


# ---------- 2. techo propio de la tool-call ----------


def test_tool_call_timeout_default():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FOURBIS_TOOL_CALL_TIMEOUT", None)
        config._runtime.pop("FOURBIS_TOOL_CALL_TIMEOUT", None)
        assert config.tool_call_timeout_s() == 300.0


def test_tool_call_timeout_por_runtime():
    with patch.dict(config._runtime, {"FOURBIS_TOOL_CALL_TIMEOUT": "900"},
                    clear=False):
        assert config.tool_call_timeout_s() == 900.0


def test_tool_call_timeout_malformado_cae_al_default():
    with patch.dict(os.environ, {"FOURBIS_TOOL_CALL_TIMEOUT": "no-es-un-numero"}):
        config._runtime.pop("FOURBIS_TOOL_CALL_TIMEOUT", None)
        assert config.tool_call_timeout_s() == 300.0


def test_tool_call_timeout_ya_no_depende_del_idle():
    """La regresión que se está previniendo.

    Antes el techo de la tool era `idle_timeout * 0.9`: bajar el watchdog
    de idle recortaba en silencio lo que podía tardar un comando.
    """
    with patch.dict(config._runtime, {"FOURBIS_EXPERT_IDLE_TIMEOUT": "60",
                                      "FOURBIS_TOOL_CALL_TIMEOUT": "300"},
                    clear=False):
        assert config.expert_idle_timeout_s() == 60.0
        assert config.tool_call_timeout_s() == 300.0


def test_read_timeout_del_mcp_va_por_encima_del_nuestro():
    """Si la capa MCP corta primero, subir nuestro techo no sirve de nada."""
    captured = {}

    class _FakeToolset:
        def __init__(self, *a, **kw):
            captured.update(kw)

    with patch.dict(os.environ, {"FOURBIS_TOOL_CALL_TIMEOUT": "300"}), \
         patch.object(mcp_pool, "MCPToolset", _FakeToolset):
        config._runtime.pop("FOURBIS_TOOL_CALL_TIMEOUT", None)
        mcp_pool.make_toolset(
            {"transport": "stdio", "command": "python", "args": []},
            repo_path="/tmp")
    assert captured["read_timeout"] > 300.0


# ---------- 3. el watchdog no cuenta una tool en vuelo ----------


class _StubToolset:
    """Toolset mínimo para ejercitar CappedToolset sin MCP real."""

    def __init__(self, delay=0.0, result="ok"):
        self.delay = delay
        self.result = result
        self.label = "stub"

    async def call_tool(self, name, tool_args, ctx, tool):
        await asyncio.sleep(self.delay)
        return self.result


class _Ctx:
    messages: list = []


async def test_capped_toolset_marca_la_tool_en_vuelo():
    inflight: dict = {}
    visto = {}

    class _Watcher(_StubToolset):
        async def call_tool(self, name, tool_args, ctx, tool):
            # Mientras la tool corre, el watchdog tiene que ver esto.
            visto["tool"], visto["since"] = expert_toolsets.tool_en_vuelo(inflight)
            return "ok"

    capped = expert_toolsets.CappedToolset(
        wrapped=_Watcher(), timeout=5.0, inflight=inflight)
    await capped.call_tool("run_shell", {}, _Ctx(), None)
    assert visto["tool"] == "run_shell"
    assert visto["since"] is not None
    # …y al terminar se limpia: un run sin tools no puede parecer ocupado.
    assert inflight == {}


async def test_capped_toolset_limpia_el_inflight_si_la_tool_revienta():
    inflight: dict = {}

    class _Boom(_StubToolset):
        async def call_tool(self, name, tool_args, ctx, tool):
            raise RuntimeError("la tool explotó")

    capped = expert_toolsets.CappedToolset(
        wrapped=_Boom(), timeout=5.0, inflight=inflight)
    with pytest.raises(RuntimeError):
        await capped.call_tool("run_shell", {}, _Ctx(), None)
    assert inflight == {}


async def test_capped_toolset_corta_por_su_propio_timeout():
    """El corte sigue existiendo: el mensaje tiene que ser accionable."""
    inflight: dict = {}
    capped = expert_toolsets.CappedToolset(
        wrapped=_StubToolset(delay=5.0), timeout=0.05, inflight=inflight)
    with pytest.raises(ModelRetry) as e:
        await capped.call_tool("run_shell", {}, _Ctx(), None)
    assert "no devolvió" in str(e.value)
    assert "background" in str(e.value)     # dice qué hacer
    assert inflight == {}


# ---------- 3b. un MCP que contesta error no puede voltear el run ----------
#
# Caso real (2026-08-26): `mcp-mermaid` devolvió
#   -32603 "Failed to generate mermaid: Parse error on line 83"
# porque el modelo escribió `#32["#32 Filtros..."]` y en Mermaid el `#`
# abre una entidad HTML (`#35;`). Un typo de un carácter.
#
# pydantic-ai traduce a ModelRetry el `ToolError` de fastmcp, pero un
# `McpError` pelado cae en el default de `on_tool_execute_error`, que es
# `raise error`: el run entero moría por ese typo. Estos tests fijan que
# el error llegue al modelo como algo corregible.


def _mcp_error(msg, code=-32603):
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData
    return McpError(ErrorData(code=code, message=msg))


async def test_error_de_mcp_no_mata_el_run_y_llega_como_retry():
    inflight: dict = {}

    class _MalFormado(_StubToolset):
        async def call_tool(self, name, tool_args, ctx, tool):
            raise _mcp_error(
                "Failed to generate mermaid: Parse error on line 83")

    capped = expert_toolsets.CappedToolset(
        wrapped=_MalFormado(), timeout=5.0, inflight=inflight)
    with pytest.raises(ModelRetry) as e:
        await capped.call_tool("generate_mermaid", {}, _Ctx(), None)
    texto = str(e.value)
    # El mensaje del server va entero: el número de línea es lo único que
    # le sirve al modelo para arreglarlo.
    assert "Parse error on line 83" in texto
    assert "generate_mermaid" in texto          # cuál de todas falló
    assert inflight == {}


async def test_error_de_mcp_tambien_sin_timeout_configurado():
    """Las tools nativas van con timeout=None; el guard vale igual."""

    class _MalFormado(_StubToolset):
        async def call_tool(self, name, tool_args, ctx, tool):
            raise _mcp_error("boom")

    capped = expert_toolsets.CappedToolset(
        wrapped=_MalFormado(), timeout=None, inflight={})
    with pytest.raises(ModelRetry):
        await capped.call_tool("x", {}, _Ctx(), None)


async def test_el_guard_no_se_traga_cualquier_excepcion():
    """No es un `except Exception`: un bug nuestro tiene que seguir
    explotando, y una cancelación tiene que seguir cancelando."""
    capped = expert_toolsets.CappedToolset(
        wrapped=_StubToolset(), timeout=5.0, inflight={})

    class _Bug(_StubToolset):
        async def call_tool(self, name, tool_args, ctx, tool):
            raise RuntimeError("bug nuestro")

    capped.wrapped = _Bug()
    with pytest.raises(RuntimeError):
        await capped.call_tool("x", {}, _Ctx(), None)


async def test_el_default_de_pydantic_ai_sigue_siendo_propagar():
    """Este guard existe SOLO porque el default de pydantic-ai es
    `raise error`. Si una versión futura lo cambia a "convertir en
    retry", este wrapper pasa a ser redundante y conviene sacarlo.
    Que se entere el test, no producción."""
    import inspect

    from pydantic_ai.capabilities.abstract import AbstractCapability

    src = inspect.getsource(AbstractCapability.on_tool_execute_error)
    assert "raise error" in src, (
        "pydantic-ai cambió el default de on_tool_execute_error; "
        "revisá si el guard de McpError en CappedToolset sigue haciendo falta")


async def test_capped_toolset_comparte_el_inflight_entre_copias():
    """`for_run`/`for_run_step` hacen dataclasses.replace: el estado se
    comparte por referencia o el watchdog mira una copia muerta."""
    import dataclasses

    inflight: dict = {}
    capped = expert_toolsets.CappedToolset(
        wrapped=_StubToolset(), timeout=5.0, inflight=inflight)
    copia = dataclasses.replace(capped)
    expert_toolsets._anotar_en_vuelo(copia.inflight, "run_shell")
    assert expert_toolsets.tool_en_vuelo(inflight)[0] == "run_shell"


async def test_dos_tools_en_paralelo_no_se_borran_la_marca():
    """Regresion medida el 2026-08-31 en code-hero-rpg.

    El modelo pidio DOS `shell` en el mismo turno —pydantic-ai los corre
    en paralelo— y `inflight` era UN solo par `{tool, since}`:

        shell A: Start-Process 'npm run dev'   (techo default, 300s)
        shell B: npx vitest run                (timeout_s=120)

    B corto a los 120s y su `finally` borro la marca de LOS DOS. Desde
    ahi el watchdog no vio ninguna tool en vuelo, conto a A como "el
    experto no hace nada" y mato el run entero al llegar a los 180s de
    idle. Pasó dos runs seguidos y dejo el plan clavado en las ultimas
    tareas, siempre con el mismo texto: "la tool `shell` no devolvió en
    >180s".

    Lo que se prueba es el veredicto, que es lo que mataba el run: con A
    todavia corriendo el watchdog tiene que decir `tool_wait`.
    """
    inflight: dict = {}
    async with expert_toolsets._tool_en_vuelo(inflight, "shell"):        # A (lenta)
        async with expert_toolsets._tool_en_vuelo(inflight, "shell"):    # B (rapida)
            assert len(inflight) == 2, "cada llamada necesita su propia marca"
        # B termino; A sigue viva y el watchdog TIENE que verla.
        nombre, desde = expert_toolsets.tool_en_vuelo(inflight)
        assert nombre == "shell"
        assert desde is not None
        assert expert_toolsets._watchdog_verdict(
            idle_s=200.0, idle_timeout=180.0,
            tool_s=0.0, tool_timeout=300.0) == "tool_wait"
    assert inflight == {}, "las dos se dieron de baja"


async def test_tool_en_vuelo_devuelve_la_mas_vieja():
    """La que decide si esto avanza o esta trabado es la que arranco
    primero: una hermana rapida no dice nada sobre la que sigue."""
    inflight: dict = {}
    expert_toolsets._anotar_en_vuelo(inflight, "vieja")
    await asyncio.sleep(0.01)
    expert_toolsets._anotar_en_vuelo(inflight, "nueva")
    assert expert_toolsets.tool_en_vuelo(inflight)[0] == "vieja"
    assert expert_toolsets.tool_en_vuelo({}) == (None, None)


async def test_el_shell_nativo_se_anuncia_al_watchdog():
    """El `shell` del relay tiene techo propio (300s) POR ENCIMA del
    watchdog de idle (180s), así que es la única tool que DEBE anunciarse
    o el watchdog la lee como "el experto no hace nada". Hasta el 30/8
    solo `CappedToolset` escribía `inflight`, y `native_shell` (default)
    saca al shell de ese camino: un `dotnet build` de tres minutos mataba
    el run entero. Pasó en inventorydemo, nodo `Corregir bugs de Infrastructure y
    Web`, 159 tool calls tirados a la basura."""
    import inspect

    inflight: dict = {}
    async with expert_toolsets._tool_en_vuelo(inflight, "shell"):
        nombre, desde = expert_toolsets.tool_en_vuelo(inflight)
        assert nombre == "shell"
        # Lo que el watchdog decide con eso puesto: esperar, no matar.
        assert expert_toolsets._watchdog_verdict(
            idle_s=200.0, idle_timeout=180.0,
            tool_s=time.monotonic() - desde,
            tool_timeout=300.0) == "tool_wait"
    assert inflight == {}, "una tool que terminó no puede quedar en vuelo"

    # Y que el shell nativo lo USE. Esto miraba el FUENTE de `run_expert`
    # buscando la llamada literal, y el propio comentario decía por qué:
    # la tool era un closure adentro de esas 2.400 líneas y no había cómo
    # alcanzarla sin levantar un run entero con proveedor. Desde que
    # `shell_tools` es un módulo aparte se arma y se ejecuta de verdad —un
    # grep del fuente pasa igual si alguien deja la línea puesta pero
    # rompe lo que hace, y eso es justo lo que hay que atrapar.
    import contextlib
    from unittest.mock import patch

    from relay import shell_tools as shell_tools_mod

    anunciadas: list = []

    @contextlib.asynccontextmanager
    async def _en_vuelo(nombre):
        anunciadas.append(nombre)
        yield

    class _Bitacora:
        def anotar_comando(self, *a):
            pass

    async def _run_falso(cmd, **kw):
        assert anunciadas == ["shell"], (
            "el comando corrió sin anunciarse: el watchdog lo lee como "
            "idle y mata el run entero")
        return {"out": "hola", "exit": 0}

    tool = shell_tools_mod.shell_tools(
        repo=".", techo_s=300.0, bitacora=_Bitacora(),
        en_vuelo=_en_vuelo)[0]
    with patch.object(shell_tools_mod.shell_mod, "run", _run_falso):
        assert await tool.function(cmd="echo hola") == "hola"
    assert anunciadas == ["shell"]


async def test_el_shell_nativo_se_desanuncia_aunque_falle():
    """Si el comando explota y la entrada queda puesta, el watchdog cree
    que hay una tool corriendo para siempre y deja de ser una red."""
    inflight: dict = {}
    with contextlib.suppress(RuntimeError):
        async with expert_toolsets._tool_en_vuelo(inflight, "shell"):
            raise RuntimeError("el comando reventó")
    assert inflight == {}


# ---------- 4. la decisión del watchdog ----------


def test_watchdog_espera_a_una_tool_en_vuelo():
    """El caso central: 10 minutos de `dotnet test` NO son idle."""
    v = expert_toolsets._watchdog_verdict(
        idle_s=600.0, idle_timeout=180.0, tool_s=600.0, tool_timeout=900.0)
    assert v == "tool_wait"


def test_watchdog_mata_si_no_hay_tool_y_pasa_el_cap():
    v = expert_toolsets._watchdog_verdict(
        idle_s=200.0, idle_timeout=180.0, tool_s=None, tool_timeout=300.0)
    assert v == "kill"


def test_watchdog_mata_si_la_tool_paso_su_propio_techo():
    """Red de seguridad: el corte de la tool falló (subprocess zombie)."""
    v = expert_toolsets._watchdog_verdict(
        idle_s=400.0, idle_timeout=180.0, tool_s=400.0, tool_timeout=300.0)
    assert v == "kill"


def test_watchdog_le_da_gracia_a_la_tool_antes_de_matar():
    """Justo pasado el techo, el corte propio de la tool va primero."""
    v = expert_toolsets._watchdog_verdict(
        idle_s=310.0, idle_timeout=180.0, tool_s=310.0, tool_timeout=300.0)
    assert v == "tool_wait"


def test_watchdog_late_cuando_el_modelo_piensa():
    v = expert_toolsets._watchdog_verdict(
        idle_s=60.0, idle_timeout=180.0, tool_s=None, tool_timeout=300.0)
    assert v == "beat"


def test_watchdog_callado_en_un_run_normal():
    v = expert_toolsets._watchdog_verdict(
        idle_s=5.0, idle_timeout=180.0, tool_s=None, tool_timeout=300.0)
    assert v == "ok"


# ---------- 5. un solo browser: la migración que retira obscura ----------


async def test_retire_obscura_borra_la_fila(db):
    """Idempotente y one-shot: si la volvés a agregar, no resucita el borrado."""
    await db.upsert_mcp_server({"name": "obscura", "capability": "browser",
                                "on_demand": 1, "enabled": 1})
    await db.upsert_mcp_server({"name": "playwright-mcp",
                                "capability": "browser",
                                "on_demand": 1, "enabled": 1})
    # El boot ya corrió en la fixture, así que el flag está puesto: para
    # ejercitar la migración hay que sacarlo (simula una DB vieja).
    await db.run("DELETE FROM system_config WHERE key='obscura_retired'")
    await db.init_schema()

    nombres = [m["name"] for m in await db.list_mcp_servers()]
    assert "obscura" not in nombres
    assert "playwright-mcp" in nombres
    assert await db.get_config("obscura_retired") == "1"


async def test_retire_obscura_no_resucita(db):
    """Si el humano la vuelve a agregar a mano, el boot la respeta."""
    await db.upsert_mcp_server({"name": "obscura", "capability": "browser",
                                "on_demand": 1, "enabled": 1})
    await db.init_schema()          # flag ya puesto por la fixture
    assert "obscura" in [m["name"] for m in await db.list_mcp_servers()]


# ---------- el watchdog y el modelo que razona (2026-08-23) ----------
#
# Diagnóstico del "queda colgado" con prompts largos, confirmado leyendo
# el código: el loop late por NODE (`async for node in agent_run`).
# Mientras el modelo genera no llega ningún node, así que una generación
# larga y un provider colgado se ven EXACTAMENTE IGUAL desde el
# watchdog — y el de 180s mataba trabajo sano.
#
# Con MiniMax no se notaba (contesta en segundos). Con un razonador
# —nemotron, que es justo el que queremos para coordinar— una sola
# generación pasa los 180s.


def test_pensando_no_es_idle_dentro_del_cap_grande():
    """El caso que mataba runs sanos."""
    assert expert_toolsets._watchdog_verdict(
        idle_s=200, idle_timeout=180, tool_s=None, tool_timeout=None,
        pensando=True, think_timeout=600) == "beat"


def test_pensando_igual_se_corta_si_pasa_su_propio_cap():
    """El margen es más grande, no infinito: un provider colgado se
    detecta igual, con el tope global de 600s como segundo anillo."""
    assert expert_toolsets._watchdog_verdict(
        idle_s=700, idle_timeout=180, tool_s=None, tool_timeout=None,
        pensando=True, think_timeout=600) == "kill"


def test_sin_pensar_el_cap_de_siempre_sigue_valiendo():
    """Dos caps y no uno solo más grande: subir el idle general
    retrasaría 10 minutos la detección en CUALQUIER otra fase."""
    assert expert_toolsets._watchdog_verdict(
        idle_s=200, idle_timeout=180, tool_s=None, tool_timeout=None,
        pensando=False, think_timeout=600) == "kill"


def test_pensando_calladito_al_principio_no_spamea():
    assert expert_toolsets._watchdog_verdict(
        idle_s=10, idle_timeout=180, tool_s=None, tool_timeout=None,
        pensando=True, think_timeout=600) == "ok"


def test_una_tool_en_vuelo_le_gana_a_todo():
    """El orden importa: si hay una tool corriendo, su propio techo manda
    aunque la fase diga que está pensando."""
    assert expert_toolsets._watchdog_verdict(
        idle_s=500, idle_timeout=180, tool_s=10, tool_timeout=300,
        pensando=True, think_timeout=600) == "tool_wait"


def test_el_default_del_cap_de_pensar_es_mas_grande_que_el_idle():
    from relay import config
    assert config.expert_think_timeout_s() > config.expert_idle_timeout_s()


def test_un_idle_apretado_a_mano_manda_sobre_el_cap_de_pensar():
    """Un humano que puso `idle_timeout_s: 5` no quiere que el modelo
    tenga 600s por otra puerta. Lo destapó un test que ya existía."""
    assert expert_toolsets._think_cap({"idle_timeout_s": 5}, 5.0) == 5.0


def test_sin_config_del_proyecto_vale_el_cap_grande():
    assert expert_toolsets._think_cap({}, 180.0) == config.expert_think_timeout_s()


def test_el_proyecto_puede_fijar_su_propio_cap_de_pensar():
    """Una tarea de auditoría con un razonador puede querer más aún."""
    assert expert_toolsets._think_cap({"idle_timeout_s": 5, "think_timeout_s": 900},
                              5.0) == 900.0


# ---------- 4. `off_plan` no se vota por quedarse sin presupuesto ----------
#
# Caso real (2026-08-26, sample-app): el ejecutor bajó el build de 98 errores
# a 1 SIGUIENDO el plan, se quedó sin presupuesto, y el verificador votó
# `off_plan`. Su propio feedback decía "agotó el presupuesto (200
# llamadas)" — o sea volumen, no rumbo, y las reglas dicen textual que
# "lo que define off_plan es el RUMBO, no el volumen".
#
# El error importa porque el castigo es asimétrico: `needs_more` sigue,
# `off_plan` corta Y TIRA el working set. Un off_plan mal puesto cuesta
# mucho más que un needs_more mal puesto.
#
# La causa: al verificador se le pasa el estado final del run, pero
# ninguna regla le decía qué hacer con `budget_exceeded`. Caso sin
# cubrir → el modelo cayó en el veredicto punitivo.


def test_el_verificador_tiene_regla_para_budget_exceeded():
    """Buscar el string `budget_exceeded` a secas NO sirve: ya aparecía
    en el preámbulo ("recibes el estado final del run: ok /
    budget_exceeded / ..."). El test tiene que pinchar el BULLET de la
    lista de reglas, que es lo único que le dice al modelo qué votar."""
    import re

    reglas = expert_stage_prompts.VERIFIER_INSTRUCTIONS
    bullet = re.search(
        r"^- `budget_exceeded`.*?(?=^- `|\Z)", reglas, re.M | re.S)
    assert bullet, (
        "falta la regla de budget_exceeded en la lista; sin ella el "
        "verificador vuelve a votar off_plan por volumen")
    texto = bullet.group(0)
    # La regla tiene que empujar al veredicto que NO destruye trabajo…
    assert "needs_more" in texto
    # …y exigir que se nombre el desvío antes de cortar.
    assert "off_plan" in texto


def test_la_regla_nombra_la_fase_que_el_codigo_manda_de_verdad():
    """El acople silencioso: la regla solo aplica si el verificador ve
    ese string exacto. Si alguien renombra la fase, la regla queda
    muerta y el veredicto vuelve a ser off_plan sin que nadie se entere."""
    import inspect

    src = inspect.getsource(expert_iteration)
    assert 'last_phase = "budget_exceeded"' in src, (
        "cambió el nombre de la fase; actualizá VERIFIER_INSTRUCTIONS o la "
        "regla de budget_exceeded deja de matchear")


def test_off_plan_sigue_siendo_el_unico_veredicto_que_poda():
    """Es lo que justifica subirle la barra de prueba a `off_plan`. Si
    algún día `needs_more` también podara, la regla de arriba pierde
    sentido y hay que repensarla."""
    import inspect

    src = inspect.getsource(expert_result)
    assert 'if state.last_phase == "off_plan" and state.messages_json:' in src
    assert 'if last_phase == "needs_more"' not in src
