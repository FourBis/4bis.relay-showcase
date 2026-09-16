"""La bitácora sobrevive entre turnos (2026-08-26).

El agujero: la bitácora existe para ser "inmune a la elisión", pero vivía
solo en memoria del run. Viaja como *instructions*, y el único choke
point de persistencia (`_dump_messages`) hace `_strip_instructions` antes
de guardar. Encima, un corte por `off_plan` recorta el historial y le
saca las tool calls enteras.

Medido sobre el run 05cb8aaf de sample-app: **152 tool calls** y lo que quedó
guardado en la conversación fueron **4 mensajes de texto**. Todo lo que el
experto había averiguado —que el server está en agosto 2026, que hay
contratos MonthlyLease bloqueando franjas, que `available-slots` rechaza
la fecha— no estaba en ninguna parte. El `/continuar` arrancaba a ciegas.

Se testea el contrato de ida y vuelta y los topes, que son lo que evita
que arreglar la amnesia reintroduzca el blowup que la elisión vino a
cortar.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from relay.experts import Bitacora  # noqa: E402


def test_ida_y_vuelta_conserva_hechos_y_comandos():
    b = Bitacora()
    b.anotar("el build compila con dotnet build")
    b.anotar("PostgreSQL responde en 5434")
    b.anotar_comando("dotnet --version", 0)

    vuelta = Bitacora.cargar(b.volcar())
    assert vuelta.hechos == b.hechos
    assert vuelta.comandos == b.comandos
    # Y lo que ve el modelo es lo mismo, que es lo único que importa.
    assert vuelta.render() == b.render()


def test_bitacora_vacia_no_ocupa_lugar():
    """`""` y no `{}`: el caller usa la verdad del string para decidir si
    guarda. Un JSON vacío escribiría en la DB en cada turno sin datos."""
    assert Bitacora().volcar() == ""


def test_cargar_vacio_da_bitacora_limpia():
    for raw in ("", None, "{}"):
        b = Bitacora.cargar(raw)
        assert b.hechos == [] and b.comandos == []


def test_json_roto_no_tira():
    """Una bitácora ilegible es peor contexto, no un run muerto."""
    for basura in ("{no es json", "[]", '{"hechos": "no soy lista"}', "null"):
        b = Bitacora.cargar(basura)
        assert b.hechos == [] and b.comandos == []


def test_los_topes_se_reaplican_al_cargar():
    """Un JSON guardado por una versión con otros límites no puede
    reintroducir el blowup: los topes mandan al cargar, no al guardar."""
    gordo = json.dumps({
        "hechos": [f"hecho {i}" for i in range(Bitacora.MAX + 40)],
        "comandos": [f"$ cmd{i} → exit=0"
                     for i in range(Bitacora.MAX_COMANDOS + 40)],
    })
    b = Bitacora.cargar(gordo)
    assert len(b.hechos) == Bitacora.MAX
    assert len(b.comandos) == Bitacora.MAX_COMANDOS
    # Se conserva lo RECIENTE, que es lo que sirve para cerrar la tarea.
    assert b.hechos[-1] == f"hecho {Bitacora.MAX + 39}"


def test_el_render_respeta_el_techo_de_chars_tras_cargar():
    """El tope real es el que se re-manda por request."""
    b = Bitacora.cargar(json.dumps(
        {"hechos": ["x" * 290 for _ in range(Bitacora.MAX)], "comandos": []}))
    bloque = b.render()
    assert len(bloque) < Bitacora.MAX_CHARS * 2      # techo + encabezados
    assert "omitidos por espacio" in bloque


def test_acumula_entre_turnos_sin_duplicar():
    """El caso real: turno 1 verifica, turno 2 retoma y agrega."""
    t1 = Bitacora()
    t1.anotar("el server corre en agosto 2026")

    t2 = Bitacora.cargar(t1.volcar())
    assert "Ya estaba anotado" in t2.anotar("el server corre en agosto 2026")
    t2.anotar("available-slots rechaza fechas pasadas")

    assert t2.hechos == ["el server corre en agosto 2026",
                         "available-slots rechaza fechas pasadas"]
    assert "agosto 2026" in t2.render()
