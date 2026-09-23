"""Facade pública para coordinación y ejecución de grafos de tareas.

La implementación vive en módulos por responsabilidad; este módulo conserva
las entradas históricas que usan la API HTTP, el disparador y los tests.
"""
from __future__ import annotations

import logging

from . import config
from . import grafo as G
from . import persist

from .orchestrator_core import (
    MAX_VUELTAS,
    TOPE_PARALELO,
    TOPE_PARALELO_OPTIN,
    _cerrar_las_que_quedaron,
    _cerrar_tarea,
    _detalle_con_respuesta,
    _nodos,
    _preguntar_por_la_tarea,
    _vueltas,
    aplicar_respuesta,
    correr_grafo,
    responder_a_la_tarea,
    sanar,
)
from .orchestrator_execution import (
    _archivos_de,
    _cerrar_chat,
    _intentar_autosplit,
    _prompt_de_tarea,
    coordinador_nemotron,
    ejecutor_minimax,
    verificador_del_grafo,
)
from .orchestrator_verification import (
    TOPE_POR_NODO_CHARS,
    TOPE_RESUMEN_CHARS,
    _RE_CHECK,
    _RE_CHECK_OUTPUT,
    _RE_EXIT,
    _ahora,
    _comandos_con_resultado,
    _evidencia_de_los_nodos,
    _plan_del_grafo,
    _resumen_de_nodos,
    _seguro,
    _tareas_ordenadas,
    _verificar_al_cerrar,
)

logger = logging.getLogger("relay.orquestador")


async def lanzar(db, project: dict, graph_id: str, *, tope: int = 0,
                 on_cambio=None, progreso_de=None) -> dict:
    """Corre un grafo ya guardado con el reparto real de modelos."""
    defaults = project.get("defaults_json") or {}
    # `tope=0` = decidilo vos. Serial salvo que el proyecto se haga
    # cargo: con `grafo_paralelo` la exclusion pasa a depender de que
    # ningun nodo escriba desde la shell, que no lo garantiza nadie.
    if not tope:
        tope = (TOPE_PARALELO_OPTIN if defaults.get("grafo_paralelo")
                else TOPE_PARALELO)
    g = await db.get_task_graph(graph_id)
    if g is None:
        raise ValueError(f"no existe el grafo {graph_id}")
    # PRENDIDO por default. Un interruptor que arranca apagado es una
    # feature que nadie corre: `skills_mode: "compact"` lleva meses en el
    # código y ningún proyecto lo tiene seteado. Quien no quiera pagar el
    # turno pone `graph_verifier: false` en su `defaults_json`.
    verificar = (verificador_del_grafo(
        project, modelo=defaults.get("verifier_model") or "")
        if defaults.get("graph_verifier", True) else None)
    return await correr_grafo(
        db, graph_id, tope=tope, on_cambio=on_cambio, verificar=verificar,
        ejecutar=ejecutor_minimax(db, project, g,
                                  modelo=defaults.get("model") or "",
                                  progreso_de=progreso_de),
        coordinar=coordinador_nemotron(
            project, modelo=defaults.get("planner_model") or ""))
