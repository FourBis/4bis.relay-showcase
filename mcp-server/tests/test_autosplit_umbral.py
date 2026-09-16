"""El tope de subdivisión tiene que dispararse ANTES que los otros cortes.

**El agujero.** `expert_max_tool_calls()` decide cuándo un nodo de grafo
que se desbocó se PARTE en subtareas en vez de seguir. Su default era
250, elegido sobre una medición de 238 nodos (0-200 tool calls → 0-4%
se cortan; 200-250 → 36%; 250+ → 85%).

El número estaba bien elegido para lo que medía y aun así no servía,
porque nunca llegaba a evaluarse: en producción los nodos morían antes
por otros cortes. Medido el 2026-09-07 sobre las corridas de ese día:

    222 tool calls  ->  hard_timeout
    210 tool calls  ->  hard_timeout
    202 tool calls  ->  budget_exceeded

Las tres debajo de 250. La subdivisión automática existía, estaba
encendida y jamás se ejercitó. Esas seis corridas fallidas se llevaron
el 50% de los tokens del día —24,5M de 48,6M— sin entregar nada.

**Qué fija este test.** Que el umbral quede dentro de la banda medida
como sana (<=200) y no vuelva a subir a la zona donde ya es tarde. No
prueba que la subdivisión funcione —eso lo cubre el camino de
`_intentar_autosplit`—, prueba que llegue a tener la oportunidad.

Cómo correr:
    cd mcp-server
    ./.venv/Scripts/python.exe -m pytest tests/test_autosplit_umbral.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from unittest.mock import patch

from relay import config  # noqa: E402

#: Tope de la banda donde la medición de 238 nodos da 0-4% de cortes.
#: Por encima de esto el nodo ya está en zona de fallo y partirlo llega
#: tarde: en 200-250 se corta el 36%, y de 250 para arriba el 85%.
BANDA_SANA = 200

#: Las tres corridas del 2026-09-07 que murieron por otro corte antes de
#: llegar al umbral. El tope tiene que quedar por debajo de la más chica.
MUERTAS_EL_7_SEP = (202, 210, 222)


def test_el_umbral_se_dispara_antes_que_los_otros_cortes():
    """Con 250 ninguna de las tres se habría partido: todas murieron
    antes. El tope tiene que ganarle a la más temprana."""
    tope = config.expert_max_tool_calls()
    assert tope < min(MUERTAS_EL_7_SEP), (
        f"el tope ({tope}) no le gana a la corrida que murió con "
        f"{min(MUERTAS_EL_7_SEP)} tool calls: la subdivisión no se "
        f"llega a disparar y el nodo se pierde entero")


def test_el_umbral_queda_en_la_banda_medida_como_sana():
    """Que no vuelva a subir a la zona donde partir ya llega tarde."""
    assert config.expert_max_tool_calls() <= BANDA_SANA


def test_el_umbral_es_configurable_sin_deploy():
    """La configuración runtime permite ajustarlo en caliente si
    150 resulta agresivo. Un valor basura cae al default, no revienta."""
    with patch.dict(config._runtime,
                    {"FOURBIS_EXPERT_MAX_TOOL_CALLS": "80"}, clear=False):
        assert config.expert_max_tool_calls() == 80
    with patch.dict(config._runtime,
                    {"FOURBIS_EXPERT_MAX_TOOL_CALLS": "no-es-un-numero"},
                    clear=False):
        assert config.expert_max_tool_calls() == 150
