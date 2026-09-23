"""Loop, recovery and human-response handling for task graphs."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from typing import Any, Awaitable, Callable, Optional

from . import config
from . import grafo as G
from .orchestrator_verification import _ahora, _seguro, _verificar_al_cerrar

logger = logging.getLogger("relay.orquestador")

#: Nodos en vuelo por grafo. Uno, o sea SERIAL, desde el 9/9/2026.
#:
#: Estuvo en 2 (docs/GRAFO_DE_TAREAS.md) mientras se creía que declarar
#: `archivos` alcanzaba para no pisarse. No alcanza: `conflictan` ya
#: serializa a los que declaran, pero un nodo con `archivos=[]` conserva
#: la `shell`, y dos `npm run build` sobre el mismo working tree se
#: corrompen sin que nadie declare nada. Medido sobre los 339 nodos
#: historicos: 111 (33%) no declaran archivos, y 29 pares de esos
#: llegaron a solaparse de verdad.
#:
#: El costo de ir a serial son justo esos 29 pares, porque los pares con
#: un escritor ya estaban serializados. Barato, y correcto por defecto.
TOPE_PARALELO = 1
#: Lo que vale `grafo_paralelo` cuando el proyecto lo prende. Es opt-in
#: porque la garantia pasa a depender de que ningun nodo escriba desde
#: la shell — o sea del planificador y del agente, no del sistema. El
#: arreglo de fondo es worktree por nodo; hasta entonces, esto.
TOPE_PARALELO_OPTIN = 2
MAX_VUELTAS = 200      # cortafuegos: un grafo no debería necesitar tantas


def _nodos(g: dict) -> list:
    return [G.Nodo.desde_fila(t, t["deps"]) for t in g["tasks"]]


async def correr_grafo(
    db: Any, graph_id: str, *,
    ejecutar: Callable[[dict], Awaitable[dict]],
    coordinar: Optional[Callable[[dict, dict], Awaitable[Any]]] = None,
    tope: int = TOPE_PARALELO,
    on_cambio: Optional[Callable[[dict], Awaitable[None]]] = None,
    verificar: Optional[Callable[..., Awaitable[dict]]] = None,
) -> dict:
    """Ejecuta el grafo hasta que no quede nada lanzable.

    `ejecutar(tarea) -> {ok, resultado?, error?, chat_id?, modelo?,
    pregunta?}` corre UN nodo. `coordinar(tarea, resultado)` es el
    modelo grande mirando un fallo; puede devolver `"reintentar"`,
    `"fallar"` o `None` para que decida la regla por default.
    `verificar(user=, plan=, executor_result=) -> dict` es la
    verificación de cierre: UNA por grafo, ver `_verificar_al_cerrar`.

    Devuelve el progreso final. No lanza por un nodo que falla: un fallo
    es un estado del grafo, no una excepción del orquestador.
    """
    # F4: un `corriendo` que quedó de una corrida anterior no está
    # corriendo — el proceso que lo tenía ya no existe. Va ANTES del
    # barrido de reservas a propósito: el barrido no toca las de una
    # tarea `corriendo` (por definición está viva), así que sanar
    # primero es lo que las libera.
    sanadas = await sanar(db, graph_id)
    if sanadas:
        logger.info("grafo %s: sané %d tareas que quedaron a medias",
                    graph_id, sanadas)
    # Reservas huérfanas de una corrida anterior que murió sin soltar. Si
    # no se limpian, el grafo se traba con archivos tomados por tareas
    # que ya no existen, y desde afuera parece un cuelgue.
    soltadas = await db.release_dead_claims(graph_id)
    if soltadas:
        logger.info("grafo %s: solté %d reservas de tareas muertas",
                    graph_id, soltadas)

    en_curso: dict = {}         # task_id -> asyncio.Task
    vueltas = 0

    try:
        vueltas = await _vueltas(db, graph_id, en_curso, ejecutar=ejecutar,
                                 coordinar=coordinar, tope=tope,
                                 on_cambio=on_cambio)
    finally:
        # Salir por excepción, por MAX_VUELTAS o por cancelación no puede
        # dejar nodos corriendo sueltos. Ver `_cerrar_las_que_quedaron`.
        limpieza = asyncio.ensure_future(
            _cerrar_las_que_quedaron(db, graph_id, en_curso, coordinar))
        # `shield`: si a nosotros nos cancelaron, la limpieza igual
        # termina —corre como tarea propia—; lo único que se pierde es la
        # espera. Sin esto, cancelar el grafo dejaría los archivos
        # tomados por tareas que ya no existen.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(limpieza)

    if vueltas >= MAX_VUELTAS:
        logger.error("grafo %s: corté por MAX_VUELTAS", graph_id)

    g = await db.get_task_graph(graph_id)
    prog = G.progreso(_nodos(g))
    terminal = prog["estado"] in ("hecho", "fallado")
    if terminal:
        await db.set_task_graph_state(graph_id, prog["estado"])
    # La verificación va DESPUÉS de fijar el estado y ANTES de avisar:
    # el estado del grafo no depende de que la etapa corra (ver
    # `_verificar_al_cerrar`), y el `on_cambio` es el que le lleva el
    # veredicto al panel — dispararlo antes mostraría el grafo cerrado
    # sin el único dato nuevo que este cierre agrega.
    #
    # `estado != cancelado`: si un humano lo paró, no le cobramos un
    # turno para decirle lo que ya sabe. En el camino normal esto ni se
    # evalúa —cancelar levanta `CancelledError` dentro de `_vueltas` y
    # nunca se llega hasta acá—, pero un grafo marcado `cancelado` desde
    # afuera mientras cerraba sí llegaría.
    if terminal and verificar is not None and g.get("estado") != "cancelado":
        await _verificar_al_cerrar(db, graph_id, g, prog, verificar)
    if on_cambio:
        await _seguro(on_cambio, await db.get_task_graph(graph_id))
    return prog


async def _vueltas(db, graph_id: str, en_curso: dict, *, ejecutar,
                   coordinar, tope: int, on_cambio) -> int:
    """El loop en sí. Devuelve cuántas vueltas dio.

    Vive aparte de `correr_grafo` para que `en_curso` sea de quien
    limpia: el loop lo llena y lo vacía, y el `finally` de afuera lo lee
    para cerrar lo que haya quedado vivo.
    """
    vueltas = 0
    while vueltas < MAX_VUELTAS:
        vueltas += 1
        g = await db.get_task_graph(graph_id)
        if g is None:
            raise ValueError(f"no existe el grafo {graph_id}")
        nodos = _nodos(g)
        corriendo = [n for n in nodos if n.id in en_curso]

        # Lanzar lo que se pueda sin pisar archivos ni pasar el tope.
        for cand in G.elegibles(nodos, tope=tope, en_curso=corriendo):
            fila = next(t for t in g["tasks"] if t["id"] == cand.id)
            # `corriendo` PRIMERO y la reserva después. Al revés queda una
            # ventana en la que la reserva existe pero la tarea todavía
            # figura `pendiente`, y el barrido de reservas muertas
            # —que borra las de toda tarea que no esté `corriendo`— se
            # llevaría puesta una reserva recién tomada.
            await db.update_task(cand.id, estado=G.CORRIENDO,
                                 started_at=_ahora(),
                                 intentos=cand.intentos + 1)
            pisados = await db.claim_task_files(
                cand.id, graph_id, list(cand.archivos))
            if pisados:
                # Defensa en profundidad: `elegibles` ya lo tendría que
                # haber filtrado. Si igual llegamos acá, NO se lanza —
                # perder una vuelta es más barato que dos bots editando
                # el mismo archivo.
                logger.warning(
                    "tarea %s no arranca: %s ya está tomado por otra",
                    cand.id, ", ".join(pisados))
                await db.release_task_files(cand.id)
                # Vuelve como estaba: un lanzamiento que no ocurrió no
                # gasta un intento, o un grafo trabado por reservas
                # agotaría los reintentos sin haber ejecutado nada.
                await db.update_task(cand.id, estado=G.PENDIENTE,
                                     intentos=cand.intentos)
                continue
            en_curso[cand.id] = asyncio.create_task(ejecutar(dict(fila)))
            logger.info("grafo %s: lanzo %s (%s)", graph_id, cand.id,
                        cand.titulo[:60])
            corriendo.append(cand)

        if not en_curso:
            break               # no queda nada corriendo ni lanzable

        if on_cambio:
            await _seguro(on_cambio, await db.get_task_graph(graph_id))

        # Esperar al primero que termine: seguir apenas se libera un
        # lugar, en vez de esperar a que terminen los dos.
        #
        # El `timeout` NO es para dejar de esperar: es el latido que
        # renueva las reservas. Este loop no tiene tick propio —duerme
        # hasta que algo termina—, así que sin él una tarea de 40
        # minutos no lo despierta nunca y su lease vencería estando
        # viva, que es peor que el cuelgue que la lease arregla.
        # Renovar y volver a esperar no gasta una vuelta: no hubo
        # progreso del grafo, y `MAX_VUELTAS` cuenta progreso.
        while True:
            hechas, _ = await asyncio.wait(
                en_curso.values(), return_when=asyncio.FIRST_COMPLETED,
                timeout=config.claim_renovar_s())
            if hechas:
                break
            await db.renovar_claims(list(en_curso))
        for task in hechas:
            tid = next(k for k, v in en_curso.items() if v is task)
            del en_curso[tid]
            await _cerrar_tarea(db, graph_id, tid, task, coordinar)
    return vueltas


async def _cerrar_las_que_quedaron(db, graph_id: str, en_curso: dict,
                                   coordinar) -> None:
    """Cierra los nodos que seguían vivos cuando el loop salió por la mala.

    Tres formas de salir sin haber terminado: una excepción (alguien
    borró el grafo en el medio), el cortafuegos de `MAX_VUELTAS`, y la
    cancelación (el relay apagándose). En las tres, sin esto, los runs
    quedan corriendo sueltos: **siguen escribiendo archivos** que ya
    nadie va a mirar, y sus reservas quedan tomadas por una tarea que
    figura `corriendo` para siempre — el barrido de reservas muertas no
    las toca justamente porque dicen estar vivas.

    Se cierran por la misma puerta que cualquier otro fallo
    (`_cerrar_tarea`), que ya sabe qué hacer con una tarea cancelada:
    suelta los archivos y le aplica la regla. Un nodo idempotente vuelve
    a `pendiente` y se retoma solo; uno que no lo es para y pregunta,
    que es lo que corresponde cuando nadie sabe cuánto alcanzó a hacer
    antes del corte.
    """
    if not en_curso:
        return
    quedaron = list(en_curso.items())
    en_curso.clear()
    for _, task in quedaron:
        task.cancel()
    # Que terminen de cancelarse antes de leerles el resultado.
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait([t for _, t in quedaron])
    for tid, task in quedaron:
        try:
            await _cerrar_tarea(db, graph_id, tid, task, coordinar)
        except Exception:  # noqa: BLE001 — limpiar no puede volver a fallar
            logger.exception("no pude cerrar la tarea %s al salir", tid)


async def sanar(db, graph_id: str) -> int:
    """Arregla las tareas que quedaron `corriendo` sin nadie corriéndolas.

    Es la mitad de F4 que nadie ve y sin la cual la otra no funciona. Si
    el relay se reinicia —o lo matan— con un grafo a medias, sus nodos
    quedan en `corriendo` para siempre. Eso no es solo cosmético:

    - el grafo figura `activo` y no avanza nunca más;
    - **sus reservas de archivo no se sueltan**, porque el barrido de
      reservas muertas deja en paz a las tareas `corriendo` —y estas
      dicen estarlo—, así que traban a cualquier grafo que venga
      después.

    Se les aplica la MISMA regla que a una tarea cortada
    (`_cerrar_las_que_quedaron`), que es la misma que a un fallo: la
    idempotente vuelve a `pendiente` y se rehace sola; la que no lo es
    para y pregunta, porque nadie sabe cuánto alcanzó a hacer antes del
    corte y repetirla podría duplicarlo.

    Devuelve cuántas sanó. Idempotente: correrla dos veces no cambia
    nada la segunda.
    """
    g = await db.get_task_graph(graph_id)
    if g is None:
        return 0
    motivo = "se cortó el proceso mientras esta tarea corría"
    n = 0
    for fila in g["tasks"]:
        if fila["estado"] != G.CORRIENDO:
            continue
        await db.release_task_files(fila["id"])
        nodo = G.Nodo.desde_fila(fila, fila["deps"])
        if nodo.idempotente:
            await db.update_task(fila["id"], estado=G.PENDIENTE,
                                 error=f"{motivo}; se rehace")
        else:
            await db.update_task(fila["id"], estado=G.ESPERANDO, error=motivo)
            await _preguntar_por_la_tarea(db, graph_id, fila, motivo,
                                          chat_id=fila.get("chat_id") or "")
        n += 1
    return n


async def aplicar_respuesta(db, graph_id: str, task_id: str,
                            decision: str, texto: str = "") -> str:
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
        await db.set_task_graph_state(graph_id, "cancelado")
        logger.info("grafo %s: el humano lo paró desde %s", graph_id, task_id)
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


async def responder_a_la_tarea(db, chat_id: str, respuesta: str) -> str:
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


async def _cerrar_tarea(db, graph_id: str, tid: str, task, coordinar) -> None:
    """Pasa una tarea terminada a su estado final y aplica las reglas."""
    try:
        res = task.result()
    except asyncio.CancelledError:
        res = {"ok": False, "error": "cancelada"}
    except Exception as e:  # noqa: BLE001 — un nodo roto no voltea el grafo
        logger.exception("tarea %s reventó", tid)
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Soltar los archivos SIEMPRE, salga bien o mal. Una tarea que falla
    # y se queda con el archivo tomado traba a todas las que lo
    # necesitan, y el grafo se ve colgado sin motivo visible.
    await db.release_task_files(tid)

    comun = {"ended_at": _ahora(), "modelo": res.get("modelo") or ""}
    # Al cancelar no hay resultado, pero el ejecutor ya vinculó el chat.
    if res.get("chat_id"):
        comun["chat_id"] = res["chat_id"]

    if res.get("pregunta"):
        # Paró a preguntar por su cuenta (`ask_human` adentro del run):
        # la pregunta ya existe, solo hay que reflejar el estado.
        await db.update_task(tid, estado=G.ESPERANDO,
                             resultado=res.get("resultado") or "", **comun)
        return

    if res.get("ok"):
        await db.update_task(tid, estado=G.HECHO,
                             resultado=(res.get("resultado") or "")[:4000],
                             error="", **comun)
        return

    # Falló. Acá entra el modelo grande, y SOLO acá.
    # `or {"tasks": []}`: si el grafo se borró mientras corría —que es
    # una de las formas de llegar acá desde la limpieza— igual hay que
    # cerrar la tarea, no reventar encima del error que ya teníamos.
    g = await db.get_task_graph(graph_id) or {"tasks": []}
    fila = next((t for t in g["tasks"] if t["id"] == tid), None)
    nodo = G.Nodo.desde_fila(fila, fila["deps"]) if fila else None
    decision = None
    if coordinar is not None:
        decision = await _seguro(coordinar, fila, res)
    if decision not in ("reintentar", "fallar", "preguntar"):
        decision = G.decidir_tras_fallo(nodo) if nodo else "fallar"

    error = (res.get("error") or "sin detalle")[:2000]
    if decision == "reintentar":
        # Vuelve a `pendiente`: la próxima vuelta del loop lo re-elige.
        await db.update_task(tid, estado=G.PENDIENTE, error=error, **comun)
        logger.info("tarea %s falló y se reintenta (idempotente)", tid)
        return
    if decision == "preguntar":
        await db.update_task(tid, estado=G.ESPERANDO, error=error, **comun)
        # …y preguntar DE VERDAD. Marcar el estado y no dejar la pregunta
        # es peor que fallar: el grafo se queda esperando a un humano que
        # nunca se enteró de que lo esperaban.
        #
        # El resultado manda; si se canceló, la fila conserva el chat
        # asociado al arrancar ese intento.
        await _preguntar_por_la_tarea(db, graph_id, fila, error,
                                      chat_id=comun.get("chat_id") or
                                      (fila or {}).get("chat_id") or "")
        logger.info("tarea %s falló y para a preguntar (no idempotente)", tid)
        return

    await db.update_task(tid, estado=G.FALLADO, error=error, **comun)
    g = await db.get_task_graph(graph_id)
    for otro in G.bloqueados_por(_nodos(g), tid):
        await db.update_task(otro, estado=G.BLOQUEADO,
                             error=f"bloqueada: {tid} falló")
    logger.warning("tarea %s falló definitivamente: %s", tid, error[:120])


async def _preguntar_por_la_tarea(db, graph_id: str, fila: dict,
                                  error: str, *, chat_id: str = "") -> None:
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
        "detail": (f"{error}\n\nNo está declarada como idempotente, así que "
                   "repetirla podría duplicar lo que ya haya hecho. "
                   "¿Cómo seguimos?"),
        # Las keys son el CONTRATO con `aplicar_respuesta`, no un detalle
        # de presentación: si acá dijeran `o0`/`o1`/`o2`, la única forma
        # de saber qué eligió el humano sería comparar la etiqueta —
        # texto en castellano, que cambia el día que alguien lo mejore.
        "options": [
            {"key": "reintentar", "label": "Reintentala igual"},
            {"key": "fallar", "label": "Dala por fallada y seguí con el resto"},
            {"key": "parar", "label": "Pará el plan"},
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
