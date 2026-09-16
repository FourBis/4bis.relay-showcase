"""Medidor de peso de tool results (2026-07-26).

Lo que se está midiendo NO es el tamaño del result sino su costo real:
un result se reenvía en cada vuelta posterior del run, así que cuesta
`size × (vueltas restantes)`. El acumulador es O(1) por tool y el carry
sale al final con `Σ size×(N−k) = N×Σsize − Σ(size×k)`; estos tests
fijan esa identidad contra el cálculo directo.
"""
from dataclasses import dataclass

from pydantic_ai.messages import ToolReturnPart

from relay.experts import _measure_tool_returns, summarize_tool_meter


@dataclass
class _Req:
    parts: list


@dataclass
class _Node:
    request: _Req


def _turn(*returns):
    return _Node(request=_Req(parts=[
        ToolReturnPart(tool_name=n, content=c, tool_call_id=f"c{i}")
        for i, (n, c) in enumerate(returns)
    ]))


def test_carry_coincide_con_el_calculo_directo():
    # Tres results: uno grande temprano, dos chicos tarde. El grande
    # tiene que dominar aunque el total de chars sea parecido.
    meter, N = {}, 4
    _measure_tool_returns(_turn(("run_shell", "x" * 1000)), meter, 1)
    _measure_tool_returns(_turn(("read_file", "y" * 500)), meter, 3)
    _measure_tool_returns(_turn(("read_file", "z" * 500)), meter, 4)

    out = summarize_tool_meter(meter, N)
    # Directo: size × (N − k)
    esperado_shell = 1000 * (4 - 1)
    esperado_read = 500 * (4 - 3) + 500 * (4 - 4)
    assert out["tools"]["run_shell"]["carry_chars"] == esperado_shell
    assert out["tools"]["read_file"]["carry_chars"] == esperado_read
    # Mismos chars totales (1000 vs 1000) pero 3000 vs 500 de costo real:
    # esta es toda la razón de ser del medidor.
    assert out["tools"]["run_shell"]["chars"] == out["tools"]["read_file"]["chars"]
    assert out["tools"]["run_shell"]["carry_chars"] > \
        out["tools"]["read_file"]["carry_chars"] * 5


def test_ordena_por_costo_real_no_por_tamano():
    meter = {}
    _measure_tool_returns(_turn(("temprana", "a" * 100)), meter, 1)
    _measure_tool_returns(_turn(("tardia", "b" * 400)), meter, 10)
    out = summarize_tool_meter(meter, 10)
    # `tardia` es 4x más grande pero llega al final: cuesta menos.
    assert list(out["tools"]) == ["temprana", "tardia"]


def test_agrega_varias_llamadas_de_la_misma_tool():
    meter = {}
    _measure_tool_returns(
        _turn(("run_shell", "a" * 10), ("run_shell", "b" * 30)), meter, 1)
    _measure_tool_returns(_turn(("run_shell", "c" * 20)), meter, 2)
    t = summarize_tool_meter(meter, 3)["tools"]["run_shell"]
    assert t["n"] == 3
    assert t["chars"] == 60
    assert t["max"] == 30
    assert t["avg"] == 20


def test_no_rompe_si_el_nodo_no_tiene_la_forma_esperada():
    # Si pydantic-ai cambia el nodo dejamos de medir, no reventamos el run.
    meter = {}
    for basura in (None, object(), _Node(request=None)):
        _measure_tool_returns(basura, meter, 1)
    assert meter == {}
    vacio = summarize_tool_meter({}, 0)
    assert vacio["total_chars"] == 0 and vacio["total_carry"] == 0


def test_cap_saves_es_exacto_no_una_regla_de_tres():
    # Cola larga: un result de 100K y nueve de 200 chars. El promedio
    # (10K) mentiría feo; el ahorro real de un cap tiene que salir del
    # excedente de CADA result.
    meter, N = {}, 12
    _measure_tool_returns(_turn(("run_shell", "x" * 100_000)), meter, 1)
    for k in range(2, 11):
        _measure_tool_returns(_turn(("run_shell", "y" * 200)), meter, k)

    out = summarize_tool_meter(meter, N)["tools"]["run_shell"]
    # Solo el grande excede 8K; los chicos no aportan nada al ahorro.
    esperado = (100_000 - 8_000) * (N - 1)
    assert out["cap_saves"]["8000"] == esperado
    # Un cap por encima del pico no ahorra nada.
    assert out["cap_saves"]["64000"] == (100_000 - 64_000) * (N - 1)
    # Monótono: bajar el cap nunca puede ahorrar menos.
    caps = sorted(int(c) for c in out["cap_saves"])
    saves = [out["cap_saves"][str(c)] for c in caps]
    assert saves == sorted(saves, reverse=True)
    # Y el ahorro jamás puede superar al carry que existía.
    assert max(saves) <= out["carry_chars"]


def test_content_no_string_igual_se_mide():
    # Algunas tools MCP devuelven dicts/listas; medimos su repr para no
    # dejar un agujero silencioso en el reporte.
    meter = {}
    _measure_tool_returns(_turn(("browser_snapshot", {"a": [1, 2, 3]})), meter, 1)
    assert meter["browser_snapshot"]["chars"] > 0
