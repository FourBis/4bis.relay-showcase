"""Preguntas y respuestas humanas del grafo, sin coordinar ejecutores."""
from __future__ import annotations

import logging
from . import grafo as G

logger = logging.getLogger("relay.orquestador")


async def aplicar_respuesta(db, graph_id: str, task_id: str,
                            decision: str, texto: str = "", *, _nodos, _detalle_con_respuesta) -> str:
    """El humano contestó la pregunta de una tarea. Se hace lo que dijo.

    Sin esto, "para y pregunta" es un callejón sin salida: el
    orquestador deja la pregunta, el humano la contesta… y el grafo
    sigue parado porque nadie llevó esa respuesta a la tarea.

    `decision` es la key de la opción que eligió (`parar`, `fallar`,
    `reintentar`); `texto` es lo que escribió a mano, que puede venir
    solo —contestar con palabras es tan válido como elegir una
    opción— o además de la decisión.

    Cuando la tarea se va a volver a correr, ese texto viaja pegado a su
    detalle por el mismo canal que `responder_a_la_tarea`. Sin eso el
    nodo se re-ejecutaba IDÉNTICO, sin enterarse de nada de lo que el
    humano dijo, y el grafo se reanudaba igual: por eso el agujero no se
    veía desde afuera.

    Devuelve `"responder"` (se reintenta con el texto adjunto),
    `"reintentar"`, `"fallar"`, `"parar"`, o `""` si no aplicaba.
    """
    g = await db.get_task_graph(graph_id)
    if g is None:
        return ""
    fila = next((t for t in g["tasks"] if t["id"] == task_id), None)
    if fila is None or fila["estado"] != G.ESPERANDO:
        # Ya se resolvió por otro lado (otra pestaña, Discord). No es un
        # error: es el mismo doble-click que `answer_expert_question` ya
        # trata como idempotente.
        return ""

    if decision == "parar":
        # El caller detiene y espera al worker antes de persistir cancelado.
        logger.info("grafo %s: el humano pidió detenerlo desde %s", graph_id, task_id)
        return "parar"

    if decision == "fallar":
        await db.update_task(task_id, estado=G.FALLADO)
        g = await db.get_task_graph(graph_id)
        for otro in G.bloqueados_por(_nodos(g), task_id):
            await db.update_task(otro, estado=G.BLOQUEADO,
                                 error=f"bloqueada: {task_id} falló")
        return "fallar"

    # Reintentar. Se le da UN intento más: la tarea llegó acá justamente
    # porque la regla no la reintentaba sola, y en varios de esos casos
    # ya gastó todos sus intentos. Sin esta línea, "reintentala igual"
    # volvía a `pendiente` y `decidir_tras_fallo` la mataba de nuevo en
    # el acto, sin haber ejecutado nada — y desde afuera se ve como que
    # el botón no hace nada.
    #
    # Si el humano además escribió algo, eso NO es una decisión sobre la
    # tarea: es el dato que a la tarea le faltaba. Va pegado al detalle,
    # el mismo canal que usa `responder_a_la_tarea`, porque reintentar
    # sin decirle nada la deja repitiendo exactamente lo que ya falló.
    #
    # `parar` y `fallar` se atienden arriba, así que ganan cuando llegan
    # junto con texto: son estados terminales —el grafo se cancela, o la
    # tarea queda fallada y bloquea a las que dependían— y ahí ningún
    # run vuelve a leer el detalle, así que pegárselo sería escribir
    # para nadie. Lo que el humano escribió igual queda guardado en la
    # respuesta de la pregunta, que es donde se lee ese caso.
    extra = ({"detalle": _detalle_con_respuesta(fila.get("detalle") or "", texto)}
             if texto else {})
    await db.update_task(task_id, estado=G.PENDIENTE, error="",
                         max_intentos=int(fila["intentos"] or 0) + 1, **extra)
    return "responder" if texto else "reintentar"


async def responder_a_la_tarea(db, chat_id: str, respuesta: str, *, _detalle_con_respuesta) -> str:
    """Una pregunta hecha DESDE ADENTRO del nodo (`ask_human`).

    Distinta de `aplicar_respuesta`: acá el humano no decidió qué hacer
    con la tarea, contestó algo que la tarea necesitaba saber. La tarea
    vuelve a `pendiente` con la respuesta pegada a su detalle, así el
    run siguiente la lee — es el mismo truco que usa el chat para
    retomar un hilo, sin inventar un canal nuevo.

    Devuelve el `graph_id` si retomó algo, o `""`.
    """
    if not chat_id:
        return ""
    filas = await db.run(
        "SELECT * FROM tasks WHERE chat_id=? AND estado=?",
        (chat_id, G.ESPERANDO))
    if not filas:
        return ""
    fila = filas[0]
    await db.update_task(
        fila["id"], estado=G.PENDIENTE, error="",
        detalle=_detalle_con_respuesta(fila.get("detalle") or "", respuesta),
        max_intentos=int(fila["intentos"] or 0) + 1)
    return fila["graph_id"]


def _detalle_con_respuesta(detalle: str, respuesta: str) -> str:
    """Le pega al detalle de la tarea lo que contestó el humano.

    Uno solo para los dos caminos que traen respuestas del humano
    (`ask_human` y el texto libre de la pregunta del orquestador): si
    cada uno la pegara a su manera, el run siguiente tendría que
    aprender a leer dos formatos.
    """
    detalle = (detalle or "").strip()
    respuesta = (respuesta or "").strip()
    nuevo = (f"{detalle}\n\n## El humano ya te contestó esto\n{respuesta}"
             if detalle else f"El humano ya te contestó esto: {respuesta}")
    return nuevo[:8000]


async def _preguntar_por_la_tarea(db, graph_id: str, fila: dict,
                                  error: str, *, chat_id: str = "", interrupted=False) -> None:
    """Deja la decisión anotada para el humano, con las opciones del caso.

    Reusa `expert_questions` —la misma tabla que `ask_human`— así la
    tarjeta del chat, Discord y el 409 de "esa decisión quedó vieja"
    funcionan igual sin código nuevo.
    """
    import json
    import uuid

    g = await db.get_task_graph(graph_id)
    if not g:
        return
    titulo = (fila.get("titulo") or fila["id"])[:200]
    q = {
        "title": f"Se frenó «{titulo}» y no puedo seguirla sola",
        "detail": (f"{error}\n\n" + (
            "La ejecución se interrumpió. Revisa el resultado conservado antes de decidir si continuar. "
            if interrupted else "No está declarada como idempotente, así que repetirla podría duplicar lo que ya haya hecho. ")
            + "¿Cómo seguimos?"),
        # Las keys son el CONTRATO con `aplicar_respuesta`, no un detalle
        # de presentación: si acá dijeran `o0`/`o1`/`o2`, la única forma
        # de saber qué eligió el humano sería comparar la etiqueta —
        # texto en castellano, que cambia el día que alguien lo mejore.
        "options": [
            {"key": "reintentar", "label": "Reinténtala igual"},
            {"key": "fallar", "label": "Dala por fallada y sigue con el resto"},
            {"key": "parar", "label": "Para el plan"},
        ],
        # A qué tarea corresponde. Sin esto, contestar deja la respuesta
        # anotada y el grafo parado igual: nadie sabría a quién aplicarla.
        "graph_id": graph_id,
        "task_id": fila["id"],
    }
    try:
        await db.create_expert_question(
            f"q_{uuid.uuid4().hex[:8]}",
            chat_id or fila.get("chat_id") or graph_id,
            json.dumps(q, ensure_ascii=False),
            conversation_id=g.get("conversation_id"),
            project_slug=g.get("project_slug"), kind="grafo")
    except Exception:  # noqa: BLE001 — no poder preguntar no voltea el grafo
        logger.exception("no pude dejar la pregunta de la tarea %s",
                         fila.get("id"))
