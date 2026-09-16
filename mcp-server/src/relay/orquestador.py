"""Corre un grafo de tareas: nemotron coordina, MiniMax ejecuta (F2).

La idea que esto habilita, y que un run largo no puede dar: **procesos
largos con avance visible**. Un run de 40 minutos es opaco y frágil —
si se cae, se cae entero, y mientras tanto nadie sabe en qué va. Un
grafo de doce tareas de dos minutos cada una avanza, se ve, y cuando una
falla se pierde esa y no las once.

**El reparto.** Nemotron arma el grafo y lo re-arma cuando algo falla:
ahí es donde sus hilos largos valen. Cada nodo lo ejecuta MiniMax en un
run corto e independiente, que es barato. Efecto lateral que importa
tanto como el costo: **runs cortos hacen que el watchdog de idle deje de
ser el techo** — el problema que estamos investigando aparte.

**Quién dice que un nodo terminó.** El mismo MiniMax que lo ejecutó se
auto-reporta, y nemotron revisa SOLO cuando el nodo falla. Verificar
cada nodo con el modelo caro dobla el costo del grafo para revisar
noventa por ciento de tareas que salieron bien; verificar ninguna deja
pasar el fallo silencioso. Revisar el fallo es donde el criterio del
modelo grande cambia la decisión.

**Dos nodos no tocan el mismo archivo.** Tres capas, y la de arriba
existe porque las otras dos resultaron insuficientes (9/9/2026):

  1. `TOPE_PARALELO = 1`: por defecto corre UN nodo a la vez. Las capas
     de abajo razonan sobre los `archivos` DECLARADOS, y la `shell`
     escribe lo que quiera sin declarar nada — dos `npm run build` en el
     mismo working tree se corrompen aunque los dos nodos digan `[]`.
     `defaults_json.grafo_paralelo` lo sube, y ahí la garantía vuelve a
     depender del planificador.
  2. `grafo.conflictan` serializa a todo nodo que declare `archivos`,
     se pisen o no: la reserva por archivo no alcanza a la shell, así
     que dos escritores nunca comparten el árbol.
  3. `files.Permisos.reservadas` rechaza la escritura de un archivo que
     otra tarea viva tiene tomado. Es lo que protege a las tools de
     archivo, y hace falta porque la declaración la escribe un LLM y
     puede quedarse corta.

El final correcto es un `git worktree` por nodo: ahí el paralelismo
vuelve entero y la garantía deja de depender de lo que declare nadie.

**Una verificación al cerrar, no una por nodo.** Lo de arriba vale para
el planificador y se arrastró al verificador sin que el argumento
aplicara: verificar no es re-derivar el plan, es mirar si lo que salió se
parece a lo que se pidió. Ver `_verificar_al_cerrar`.

Este módulo NO habla con la red ni con pydantic-ai: recibe `ejecutar`,
`coordinar` y `verificar` como funciones. Así el loop —que es donde viven
las decisiones caras— se prueba entero sin un modelo contestando.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from . import config
from . import grafo as G
from . import persist

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


# =====================================================================
# La verificación de cierre: UNA por grafo
# =====================================================================
#
# Los chats pasan por `run_expert_staged` —planificador, ejecutor,
# verificador— y el grafo llama a `run_expert` pelado, un nodo a la vez.
# El motivo de saltear el PLANIFICADOR por nodo sigue en pie: el plan ya
# existe, es el grafo, y volver a derivarlo por nodo sería pagar por una
# decisión ya tomada.
#
# El verificador se había arrastrado en la misma decisión y ahí el
# argumento no aplica: verificar no es re-derivar el plan, es mirar si lo
# que salió se parece a lo que se pidió. La tarea que MÁS se puede
# desviar —la grande, la de decenas de nodos— corría sin nadie mirando:
# seis grafos muertos el 2026-09-06/07 se llevaron el 50 % de los tokens
# del día sin entregar nada.
#
# Por qué UNA y no una por nodo: por nodo es exactamente el costo que la
# decisión original evitaba (doblar el precio del grafo para revisar el
# 90 % de tareas que salieron bien). Una al cerrar cuesta un turno corto
# por grafo entero.
#
# Lo que esta etapa NO hace: relanzar. Aunque el veredicto sea
# `needs_more`, el grafo no se reanuda solo — mismo criterio que los
# grafos a medias en el boot del server: reparar es una cosa y arrancar
# trabajo que nadie pidió es otra. El humano decide, con el veredicto a
# la vista.

# Tope del resumen de nodos que viaja al verificador, en caracteres. Un
# grafo tiene decenas de nodos y este turno tiene que ser barato: las
# transcripciones enteras son cientos de miles de caracteres.
#
# 3000 y no más porque `experts._run_verifier` recorta el contenido a
# head=1200 / tail=1800: pasarse de ahí no compra contexto, solo hace que
# el recorte se coma los nodos del MEDIO en silencio. Recortando acá, los
# que no entran se nombran (id + estado) en vez de desaparecer.
TOPE_RESUMEN_CHARS = 3000
TOPE_POR_NODO_CHARS = 200


def _tareas_ordenadas(g: dict) -> list[dict]:
    return sorted(g.get("tasks") or [], key=lambda x: (x["orden"], x["id"]))


def _plan_del_grafo(g: dict) -> str:
    """Los nodos como plan numerado: el plan del grafo ES el grafo.

    Va con id y dependencias porque eso es lo que deja al verificador
    decir "el nodo que faltaba era el que dependía de t3" en vez de una
    objeción genérica.
    """
    lineas = []
    for i, t in enumerate(_tareas_ordenadas(g), 1):
        deps = ", ".join(t.get("deps") or ())
        lineas.append(
            f"{i}. [{t['id']}] {(t.get('titulo') or '').strip()}"
            + (f" (depende de: {deps})" if deps else ""))
    return "\n".join(lineas)


def _resumen_de_nodos(g: dict) -> str:
    """Qué produjo cada nodo, recortado. Ver `TOPE_RESUMEN_CHARS`.

    Se manda el `error` cuando lo hay y el `resultado` cuando no: el
    error es lo que el verificador necesita para separar "falló y por eso
    falta" de "cerró bien pero no hizo lo que se pidió".
    """
    lineas: list[str] = []
    usado = 0
    sin_detalle: list[str] = []
    sustituidos = G.sustituidos([
        G.Nodo.desde_fila(t, t.get("deps") or ()) for t in g.get("tasks") or []])
    # El cierre más reciente contiene las recuperaciones. `orden` mezcla
    # padres e hijos de autosplit y dejaba fuera justo la comprobación final.
    recientes = sorted(_tareas_ordenadas(g),
                       key=lambda t: t.get("ended_at") or "", reverse=True)
    for t in recientes:
        error = (t.get("error") or "").strip()
        cuerpo = error or (t.get("resultado") or "").strip()
        if len(cuerpo) > TOPE_POR_NODO_CHARS:
            cuerpo = cuerpo[:TOPE_POR_NODO_CHARS] + "…"
        fecha = f" ({t['ended_at']})" if t.get("ended_at") else ""
        estado = (f"subdividido; historial: {t.get('estado')}"
                  if t["id"] in sustituidos else t.get("estado"))
        linea = (f"- [{estado}]{fecha} "
                 f"{(t.get('titulo') or t['id']).strip()[:80]}"
                 + (f" — {'error' if error else 'resultado'}: {cuerpo}"
                    if cuerpo else ""))
        if lineas and usado + len(linea) > TOPE_RESUMEN_CHARS:
            sin_detalle.append(f"{t['id']} [{estado}]")
            continue
        lineas.append(linea)
        usado += len(linea) + 1
    if sin_detalle:
        # Nombrarlos y no omitirlos: un verificador que no sabe que está
        # viendo una muestra juzga "faltó la mitad del plan" sobre un
        # recorte nuestro. Mismo criterio que `_render_tool_calls`.
        lineas.append(
            f"- (tope de {TOPE_RESUMEN_CHARS} caracteres: estos nodos van "
            f"sin detalle) " + "; ".join(sin_detalle[:60]))
    return "\n".join(lineas)


#: El exit code que el harness pega al final de la salida de `shell`.
_RE_EXIT = re.compile(r"^\(exit=(-?\d+)\)\s*$", re.MULTILINE)
_RE_CHECK = re.compile(r"\b(test|pytest|newman|build|check|validate)\b", re.I)
_RE_CHECK_OUTPUT = re.compile(
    r"^.*(?:Passed!|Correctas!|Passed:|Correctas:|Total tests:|"
    r"requests\s*[=:│]|assertions\s*[=:│]|failures\s*[=:]).*$", re.I | re.M)


def _comandos_con_resultado(eventos: list) -> tuple[list[dict], int]:
    """Correlaciona por id; un lote histórico sin ids no se puede atribuir."""
    pendientes: list[dict] = []
    comandos: list[dict] = []
    omitidos = 0
    for ev in eventos:
        if not isinstance(ev, dict):
            continue
        if ev.get("phase") == "tool_call" and ev.get("cmd"):
            actual = {**ev, "ambiguo": False}
            mismos = [p for p in pendientes if p.get("tool") == ev.get("tool")]
            if mismos:
                for p in mismos + [actual]:
                    p["ambiguo"] = True
            pendientes.append(actual)
        elif ev.get("phase") == "tool_result":
            call_id = ev.get("tool_call_id")
            candidatos = [p for p in pendientes
                          if (p.get("tool_call_id") == call_id if call_id
                              else p.get("tool") == ev.get("tool"))]
            if not candidatos:
                continue
            actual = candidatos[0]
            # Consumir TAMBIÉN background/sin exit. Dejarlo pendiente pegaba
            # todos los códigos siguientes al comando anterior (AuroraDemo, fecha de ejemplo).
            pendientes.remove(actual)
            if len(candidatos) != 1 or (not call_id and actual["ambiguo"]):
                omitidos += 1
                for p in candidatos[1:]:
                    p["ambiguo"] = True
                continue
            salida = str(ev.get("output") or "")
            codigos = _RE_EXIT.findall(salida)
            resumen = " ".join(" ".join(
                _RE_CHECK_OUTPUT.findall(salida)).split())[:160]
            comandos.append({
                "cmd": str(actual["cmd"]).splitlines()[0],
                "ts": ev.get("ts") or actual.get("ts") or "",
                "exit": int(codigos[-1]) if codigos else None,
                "resumen": resumen,
            })
    return comandos, omitidos + len(pendientes)


async def _evidencia_de_los_nodos(db, g: dict) -> str:
    """Bitácora sintética con lo que de verdad hicieron los nodos.

    El verificador del grafo recibía `tool_calls_summary: []`, y el
    comentario de al lado lo admitía: "más honesto que inventar un
    resumen agregado que nadie midió". Pero el rastro existe y está
    medido — cada nodo dejó en `chats.progress_events` el comando en su
    `tool_call` y el exit code al final del `tool_result`.

    Reusa `Bitacora`, con muestra por nodo y fechas. El cierre permite
    4000 caracteres de evidencia y señala los recortes; un lote sin ids
    se omite si no permite asociar cada salida a su comando sin adivinar.

    Nunca lanza: sin evidencia se verifica peor, no se rompe el cierre.
    """
    from .experts import Bitacora

    bit = Bitacora()
    por_nodo: list[list[dict]] = []
    total = sin_correlacion = 0
    for t in g.get("tasks") or []:
        chat_id = t.get("chat_id")
        if not chat_id:
            continue
        try:
            fila = await db.get_chat(chat_id)
            eventos = json.loads((fila or {}).get("progress_events") or "[]")
        except Exception:  # noqa: BLE001
            continue
        comandos, omitidos = _comandos_con_resultado(eventos)
        sin_correlacion += omitidos
        total += len(comandos)
        for cmd in comandos:
            cmd["nodo"] = t["id"]
        relevantes = [cmd for cmd in comandos
                      if cmd["resumen"] or _RE_CHECK.search(cmd["cmd"])]
        # ponytail: dos comprobaciones recientes por nodo; evidencia
        # estructurada por alcance reemplaza esta heurística si no alcanza.
        muestra = (relevantes or comandos)[-2:]
        if muestra:
            por_nodo.append(list(reversed(muestra)))
    por_nodo.sort(key=lambda cs: cs[0]["ts"], reverse=True)
    # Una plaza por nodo antes de darle una segunda al mismo: un run largo
    # no desaloja las comprobaciones de todos sus hermanos.
    seleccion = [cs[0] for cs in por_nodo][:Bitacora.MAX_COMANDOS - 1]
    seleccion += [cs[1] for cs in por_nodo if len(cs) > 1][
        :Bitacora.MAX_COMANDOS - 1 - len(seleccion)]
    seleccion.sort(key=lambda cmd: cmd["ts"], reverse=True)
    if total or sin_correlacion:
        bit.comandos.append(
            f"Muestra por nodo, reciente primero: {total - len(seleccion)} "
            f"comandos omitidos; {sin_correlacion} resultados sin correlación "
            "segura. Una omisión no prueba éxito ni fallo.")
    for cmd in seleccion:
        codigo = (f"exit={cmd['exit']}" if cmd["exit"] is not None
                  else "sin código de salida; disponibilidad sin verificar")
        resumen = f" | {cmd['resumen']}" if cmd["resumen"] else ""
        bit.comandos.append(
            f"[{cmd['nodo']} {cmd['ts'] or 'sin fecha'}] "
            f"$ {' '.join(cmd['cmd'].split())[:120]} → {codigo}{resumen}")
    return bit.volcar()


async def _verificar_al_cerrar(db, graph_id: str, g: dict, prog: dict,
                               verificar) -> None:
    """Corre la verificación del grafo y la guarda. NUNCA lanza.

    El grafo ya terminó su trabajo: que esta etapa falle es una falla de
    TELEMETRÍA, no del trabajo. Mismo criterio que el `try/except` que
    envuelve a `sanar` en el boot del server — se registra que falló y el
    grafo cierra como habría cerrado. Por eso el estado ya está fijado
    antes de llegar acá.
    """
    payload: dict = {"at": _ahora(), "estado_grafo": prog.get("estado") or "",
                     "verdict": "", "feedback": "", "modelo": "", "error": ""}
    try:
        res = await verificar(
            user=g.get("objetivo") or "",
            plan=_plan_del_grafo(g),
            executor_result={
                "graph_id": graph_id,
                "phase_at_end": prog.get("estado") or "",
                "content": _resumen_de_nodos(g),
                # El grafo no lleva un registro de tool calls propio: las
                # de cada nodo viven en su chat. `_render_tool_calls`
                # imprime "(sin llamadas a herramientas)" y el prompt lo
                # dice tal cual, que es más honesto que inventar un
                # resumen agregado que nadie midió.
                "tool_calls_summary": [],
                # La evidencia SÍ se puede medir, y desde el 9/9/2026 se
                # mide: los comandos de cada nodo con su exit code real,
                # sacados de `progress_events`. Viaja como bitácora para
                # que el verificador del grafo lea el mismo formato que
                # el del turno. Ver `_evidencia_de_los_nodos`.
                "bitacora_json": await _evidencia_de_los_nodos(db, g),
            })
        res = res if isinstance(res, dict) else {}
        payload["verdict"] = str(res.get("verdict") or "")
        payload["feedback"] = str(res.get("feedback") or "")[:2000]
        payload["modelo"] = str(res.get("modelo") or "")
        payload["error"] = str(res.get("error") or "")[:200]
        # Tokens de la etapa, planos, con la misma regla que
        # `chats.stages_json`: la clave solo existe si se midió, así
        # que ausente = "sin dato" y no cero.
        usage = res.get("usage") or {}
        if usage:
            payload["tokens_in"] = usage.get("tokens_in", 0)
            payload["tokens_out"] = usage.get("tokens_out", 0)
        logger.info("grafo %s: veredicto %s (%s)", graph_id,
                    payload["verdict"] or "(vacío)", payload["modelo"])
    except Exception as e:  # noqa: BLE001 — telemetría, no el trabajo
        logger.exception("grafo %s: la verificación de cierre reventó (%r); "
                         "el grafo cierra igual", graph_id, e)
        payload["error"] = f"{type(e).__name__}: {e}"[:200]
    try:
        await db.set_task_graph_verificacion(
            graph_id, json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — ídem: no puede voltear el cierre
        logger.exception("grafo %s: no pude guardar el veredicto", graph_id)


async def _seguro(fn, *a):
    """Llama a un callback sin que su error se lleve puesto el grafo."""
    try:
        return await fn(*a)
    except Exception:  # noqa: BLE001
        logger.exception("callback del orquestador falló; sigo")
        return None


def _ahora() -> str:
    from .db import now_iso
    return now_iso()


# =====================================================================
# El cableado real: MiniMax ejecuta cada nodo, nemotron mira los fallos
# =====================================================================
#
# Todo lo de arriba recibe `ejecutar` y `coordinar` inyectados para poder
# probarse sin modelo. Acá abajo están las implementaciones de verdad,
# que son lo único de este archivo que habla con pydantic-ai.


async def _intentar_autosplit(
    db, proyecto: dict, grafo_row: dict, tarea: dict, resultado_parcial: str,
) -> list[str]:
    """Fase 2B (2026-09-02): reparte el trabajo RESTANTE de un nodo que
    cortó por `budget_split` (tope de 250 tool calls de la Fase 1, ver
    `config.expert_max_tool_calls`) en subtareas, en vez de dejarlo
    fallado esperando al humano.

    Devuelve los ids agregados, o `[]` si no se subdividió — por
    CUALQUIER motivo: ineligible, ya subdividido, el modelo no contestó,
    devolvió basura, o `add_tasks_to_graph` lo rechazó. `[]` no es un
    error: el caller sigue con el camino de siempre (nodo fallado) como
    si esto no existiera — ver el `try/except` alrededor del call site.

    Los límites que evitan que una subdivisión mala se MULTIPLIQUE en
    vez de fallar (una recursión sin tope sería peor que el problema
    que esto resuelve):
      - **profundidad 1**: un nodo con `parent_id` (ya es una subtarea)
        no se vuelve a subdividir, aunque él mismo corte con
        `budget_split`.
      - **fan-out 2..5**: `parsear_tareas(max_tareas=5)` topea de
        arriba; menos de 2 no es una subdivisión (mismo criterio que
        `planificador.armar_grafo`).
      - **una sola vez por nodo**: se chequea consultando la BASE (¿ya
        hay tareas con `parent_id=<este nodo>`?), no memoria del
        proceso — el relay se reinicia y una bandera en memoria no
        sobrevive.

    Reusa `planificador`: `cascada_planner` + `_pedir_plan` (fallback
    entre modelos) y `parsear_tareas` (auto-repara JSON envuelto, deps
    por título, deps colgadas) son el mismo parser que arma los grafos
    hoy — escribir otro acá sería duplicar el punto que más falla.
    """
    if tarea.get("parent_id"):
        return []
    graph_id = tarea["graph_id"]
    existentes = await db.list_tasks(graph_id)
    if any(t.get("parent_id") == tarea["id"] for t in existentes):
        return []  # ya se subdividió una vez — no se reintenta

    from . import experts, planificador as P

    contexto = (
        "Esta NO es una tarea nueva: es la continuación de una que se "
        "quedó sin presupuesto a mitad de camino (demasiadas tool "
        "calls). Repartí SOLO lo que FALTA en subtareas chicas — NO "
        "repitas lo que ya está hecho.\n\n"
        "### Lo que esta tarea alcanzó a hacer antes de cortar\n"
        f"{(resultado_parcial or '(no reportó nada)').strip()[:3000]}\n"
    )
    objetivo = (
        f"{grafo_row.get('objetivo') or ''}\n\n"
        f"Tarea que hay que terminar de repartir: "
        f"{tarea.get('titulo') or tarea['id']}\n"
        f"{(tarea.get('detalle') or '').strip()}"
    ).strip()
    prompt = P.PROMPT.format(
        objetivo=objetivo, contexto=f"\n## Contexto\n{contexto}\n",
        max_tareas=5)

    specs = P.cascada_planner(proyecto)
    r, fallo, _usado = await P._pedir_plan(specs, 0, prompt, experts)
    if r is None:
        logger.warning(
            "autosplit de %s: el planificador no contestó en ninguno de "
            "%d modelo(s) (%r)", tarea["id"], len(specs), fallo)
        return []

    try:
        subtareas = P.parsear_tareas(getattr(r, "output", "") or "",
                                     max_tareas=5)
    except ValueError as e:
        logger.warning("autosplit de %s: plan ilegible (%s)", tarea["id"], e)
        return []
    if len(subtareas) < 2:
        # Igual que `armar_grafo`: si entra en una sola tarea, no era
        # una subdivisión — hubiera terminado en el mismo run.
        logger.info(
            "autosplit de %s: el modelo devolvió %d tarea(s), no es una "
            "subdivisión", tarea["id"], len(subtareas))
        return []

    # Ids únicos (tasks.id es PK global): mismo criterio que
    # `_ids_unicos`, con un prefijo propio para no chocar ni con el
    # grafo ni con un intento de subdivisión anterior de OTRO nodo.
    #
    # El prefijo arranca en `tarea["id"]` y NO en `graph_id`: los ids de
    # tarea YA vienen prefijados con el grafo (`g_xxx:t4`), así que
    # anteponerlo otra vez lo duplicaba. Visto en la prueba en vivo del
    # 2026-09-03: `un grafo de ejemplo:un grafo de ejemplo:t4:split:analisis-23-26`.
    # Cosmético —nada se rompía— pero esos ids se leen en la UI, en los
    # logs y en `task_deps`.
    subtareas = P._ids_unicos(subtareas, f"{tarea['id']}:split")

    # Si el modelo no declaró archivos, hereda los del nodo original: el
    # scheduler los usa para no correr en paralelo dos que se pisan, y
    # las subtareas tocan el mismo terreno que la tarea que reemplazan.
    archivos_originales = _archivos_de(tarea)
    for t in subtareas:
        if not t.get("archivos"):
            t["archivos"] = list(archivos_originales)

    try:
        agregados = await db.add_tasks_to_graph(
            graph_id, subtareas, reemplaza=tarea["id"])
    except (ValueError, G.GrafoInvalido) as e:
        logger.warning(
            "autosplit de %s: add_tasks_to_graph lo rechazó (%s)",
            tarea["id"], e)
        return []

    logger.info("grafo %s: nodo %s subdividido en %d subtareas: %s",
                graph_id, tarea["id"], len(agregados), ", ".join(agregados))
    return agregados


def _archivos_de(tarea: dict) -> list[str]:
    """`tasks.archivos` es JSON en texto; acá siempre lista."""
    crudo = tarea.get("archivos")
    if not crudo:
        return []
    try:
        lista = json.loads(crudo) if isinstance(crudo, str) else list(crudo)
    except (ValueError, TypeError):
        return []
    return [str(a) for a in lista] if isinstance(lista, list) else []


def ejecutor_minimax(db, proyecto: dict, grafo_row: dict, *,
                     modelo: str = "", progreso_de=None):
    """Devuelve un `ejecutar(tarea)` que corre UN nodo con el modelo barato.

    Tres decisiones que hacen que un nodo sea corto de verdad, que es de
    lo que depende todo lo demás:

    1. **`run_expert` y no `run_expert_staged`.** El plan YA existe — es
       el grafo. Pasar por el runner de etapas pagaría un turno de
       planificación por nodo para re-derivar lo que el coordinador ya
       decidió, más uno de verificación y otro de documentación.
    2. **Sin historial del hilo.** El nodo recibe su objetivo y el
       resultado de sus dependencias, no la conversación entera. Es lo
       que lo hace barato y lo que evita que el contexto crezca turno a
       turno hasta el cap.
    3. **Archivos reservados adentro.** El run puede leer todo pero no
       escribir lo que otra tarea tiene tomado.

    `progreso_de` es una **factory**, no un callback: recibe el `chat_id`
    del nodo y devuelve su `on_progress`. Tiene que ser así porque
    `make_progress_callback` registra el `RunProgress` en el store bajo
    ese id, y el chat del nodo recién existe acá adentro. Antes este
    parámetro era un `on_progress` suelto que NADIE pasaba nunca — con lo
    cual los nodos del grafo no reportaban a ningún lado y el panel no
    tenía forma de decir qué herramienta estaba corriendo. Medido el
    24/8: veinte minutos de un nodo trabajando sin una sola señal.
    """
    from . import experts

    async def ejecutar(tarea: dict) -> dict:
        reservados = await db.files_claimed_by_others(tarea["id"])
        prompt = _prompt_de_tarea(grafo_row, tarea,
                                  await db.list_tasks(tarea["graph_id"]))
        chat_id = ""
        if hasattr(db, "create_chat"):
            chat_id = await db.create_chat(
                project_slug=proyecto.get("slug"), source="grafo",
                author="orquestador", target=proyecto.get("slug"),
                conversation_id=grafo_row.get("conversation_id"),
                # El título de la tarea y no `prompt`: el prompt del nodo
                # lo arma `_prompt_de_tarea` y trae el objetivo entero del
                # grafo, que como título de una fila no dice cuál nodo es.
                user_prompt=tarea.get("titulo") or "")
        # Asocia el chat a la tarea desde el arranque, no al cerrar: la
        # ventana en la que se mira el trabajo en vuelo es justamente
        # esta, y sin `chat_id` la fila no permite saltar al chat.
        # Idempotente: al cerrar, `_terminar_tarea` reescribe el mismo
        # campo sobre la misma fila.
        if chat_id:
            await db.update_task(tarea["id"], chat_id=chat_id)
        # Después de crear el chat: el callback se ata a ese id.
        on_progress = None
        if progreso_de is not None and chat_id:
            try:
                on_progress = progreso_de(chat_id)
            except Exception as e:  # noqa: BLE001 — telemetría, no el trabajo
                logger.warning("no pude armar el progreso del nodo %s (%r)",
                               tarea.get("id"), e)
        rescue: dict = {}
        started = asyncio.get_running_loop().time()
        try:
            res = await experts.run_expert(
                proyecto, prompt, db=db, model_override=modelo,
                chat_id=chat_id,
                conversation_id=grafo_row.get("conversation_id") or "",
                on_progress=on_progress,
                steer=getattr(on_progress, "steer", None),
                rescue=rescue,
                archivos_reservados=reservados)
        except asyncio.CancelledError:
            rescue.setdefault("duration_ms", int(
                (asyncio.get_running_loop().time() - started) * 1000))
            rescue.update(content="run cancelado", phase_at_end="cancelled")
            if chat_id and hasattr(db, "finish_chat"):
                await _cerrar_chat(db, chat_id, "cancelled", "cancelado", rescue)
            raise
        except Exception as e:  # noqa: BLE001 — un nodo roto no voltea el grafo
            if chat_id and hasattr(db, "finish_chat"):
                await _cerrar_chat(db, chat_id, "error", str(e))
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "chat_id": chat_id, "modelo": modelo}

        fase = res.get("phase_at_end") or ""
        # La lista canónica vive en `grafo` (ver `FASES_INCOMPLETAS`): acá
        # faltaban `budget_exceeded`, `provider_error` y `off_plan`, así
        # que un nodo cortado a mitad quedaba `hecho` y el grafo cerraba
        # anunciando un trabajo que no se hizo.
        roto = fase in G.FASES_INCOMPLETAS

        subdivididas: list[str] = []
        if fase == "budget_split":
            from . import config
            if config.grafo_autosplit_habilitado():
                try:
                    subdivididas = await _intentar_autosplit(
                        db, proyecto, grafo_row, tarea,
                        res.get("content") or "")
                except asyncio.CancelledError:
                    await _cerrar_chat(db, chat_id, "cancelled", "cancelado al subdividir",
                                      {**res, "phase_at_end": "cancelled"})
                    raise
                except Exception as e:  # noqa: BLE001 — falla segura
                    logger.exception(
                        "autosplit de %s reventó (%r); sigue como "
                        "budget_split normal", tarea.get("id"), e)

        error = f"el nodo terminó en {fase!r}" if roto else ""
        # Un nodo que abre una pregunta NO está roto, pero tampoco
        # terminó: `ok` se va a False por `question_id` y hasta hoy
        # `error` quedaba "". Resultado medido (barrido del 4/9): de 81
        # chats en `error`, 29 (36 %) tienen la columna `error` NULL o
        # vacía —todos `source='grafo'`, `author='orquestador'`,
        # `phase_at_end='writing'`—, o sea runs que figuran como fallidos
        # sin decir por qué cuando en realidad están esperando al humano.
        if not error and res.get("question_id"):
            error = (f"el nodo abrió una pregunta al humano "
                     f"({res['question_id']}) y quedó en pausa")
        if subdivididas:
            error = (f"subdividido en {len(subdivididas)} subtareas: "
                     + ", ".join(subdivididas))
        salida = {
            "ok": not roto and not res.get("question_id"),
            "resultado": (res.get("content") or "").strip(),
            "error": error,
            "chat_id": chat_id or res.get("chat_id") or "",
            "modelo": res.get("model") or modelo,
            "pregunta": res.get("question_id") or "",
        }
        if chat_id and hasattr(db, "finish_chat"):
            # Status del chat: SOLO depende de `roto` (nodo realmente
            # roto), no de `salida["ok"]`. `ok` mezcla dos preguntas
            # distintas —¿el grafo sigue avanzando? y ¿esto salió bien?—
            # y para la primera un nodo que abrió una pregunta también
            # es `False` (correcto: no está `hecho`, el grafo lo espera).
            # Pero acá abajo esa misma bandera etiquetaba como `error` un
            # chat que solo pausó a preguntarle algo a un humano. Medido
            # sobre relay.db: camino UI 79 preguntas → 79 `ok`; camino
            # grafo 32 preguntas → 32 `error`. Misma tool, misma pausa,
            # etiqueta opuesta según qué código cierra el chat.
            await _cerrar_chat(db, chat_id, "error" if roto else "ok",
                               salida["error"], res)
        return salida

    return ejecutar


async def _cerrar_chat(db, chat_id: str, status: str, error: str = "",
                       res: Optional[dict] = None) -> None:
    """Mismo cierre durable que HTTP: los artefactos se pueden reintentar."""
    from . import finalization
    if status != "ok" and not error:
        error = f"cerrado como {status!r} sin motivo declarado (bug del caller)"
    r = res or {}
    fields = dict(
        status=status, error=error or None,
        tokens_in=r.get("tokens_in"), tokens_out=r.get("tokens_out"),
        cache_read_tokens=r.get("cache_read_tokens"), tool_calls=r.get("tool_calls"),
        phase_at_end=r.get("phase_at_end"), duration_ms=r.get("duration_ms"),
        model=r.get("model"), last_tool=r.get("last_tool"),
        progress_events=json.dumps(r.get("progress_events") or []))
    try:
        row = await db.get_chat(chat_id)
        if row is None:
            await db.finish_chat(chat_id, **fields)
            return
        artifact = dict(
            target=row.get("target") or row.get("project_slug") or "_orphan",
            chat_id=chat_id, user=row.get("user_prompt") or row.get("author") or "?",
            content=r.get("content") or "", source=row.get("source") or "grafo",
            author=row.get("author") or "", model=r.get("model") or "",
            status=status, duration_ms=r.get("duration_ms") or 0,
            error=error or None, events=r.get("progress_events") or [])
        if status == "cancelled" and r.get("messages_json"):
            # Historial del nodo, no del hilo compartido: conservarlo en
            # chat_outputs sin sobrescribir la conversación del grafo.
            artifact["messages_json"] = r["messages_json"]
        await finalization.finish(db, chat_id, artifact=artifact, **fields)
    except Exception:
        logger.exception("no pude cerrar el chat %s del nodo", chat_id)


def _prompt_de_tarea(grafo_row: dict, tarea: dict, todas: list) -> str:
    """Lo único que el nodo necesita saber. Corto a propósito.

    Lleva el resultado de sus dependencias porque son su insumo real: el
    nodo que carga datos necesita saber qué esquema dejó el que migró.
    NO lleva el resto del grafo — un nodo que ve doce tareas ajenas
    empieza a opinar sobre ellas en vez de hacer la suya.
    """
    por_id = {t["id"]: t for t in todas}
    partes = [
        f"Objetivo general del plan: {grafo_row.get('objetivo') or ''}",
        "",
        f"## Tu tarea: {tarea.get('titulo') or tarea['id']}",
    ]
    if (tarea.get("detalle") or "").strip():
        partes += ["", tarea["detalle"].strip()]
    previos = [por_id[d] for d in (tarea.get("deps") or []) if d in por_id]
    hechos = [t for t in previos if (t.get("resultado") or "").strip()]
    if hechos:
        partes += ["", "## Lo que dejaron las tareas de las que dependés"]
        for t in hechos:
            partes.append(f"- **{t.get('titulo') or t['id']}**: "
                          f"{(t['resultado'] or '')[:600]}")
    archivos = tarea.get("archivos")
    if archivos:
        import json as _json
        try:
            lista = _json.loads(archivos) if isinstance(archivos, str) else list(archivos)
        except (ValueError, TypeError):
            lista = []
        if lista:
            partes += ["", "## Archivos que declaraste para esta tarea",
                       ", ".join(f"`{a}`" for a in lista),
                       "",
                       "Otros archivos los podés LEER. Si necesitás escribir "
                       "uno que no está en esta lista y otra tarea lo tiene "
                       "tomado, el relay te lo va a rechazar: anotalo en tu "
                       "resultado en vez de insistir."]
    partes += [
        "",
        "Hacé SOLO esta tarea y terminá. No sigas con las que vienen "
        "después: las corre otro. Cerrá con un resumen de una o dos líneas "
        "de lo que quedó hecho — eso es lo que van a leer las tareas que "
        "dependen de vos.",
    ]
    return "\n".join(partes)


def coordinador_nemotron(proyecto: dict, *, modelo: str = ""):
    """Devuelve un `coordinar(tarea, resultado)` con el modelo que razona.

    Entra SOLO cuando un nodo falla. Lo que se le pide no es que arregle
    la tarea: es que decida **qué hacer con el fallo**, que es donde su
    criterio cambia el resultado y donde la regla mecánica se equivoca.
    Un 429 o un lock del filesystem se reintentan aunque el nodo escriba;
    un test que falla de verdad, no.
    """
    from . import config, experts

    spec = modelo or config.planner_model_spec()

    async def coordinar(tarea: dict, res: dict) -> Optional[str]:
        error = (res.get("error") or "")[:1200]
        prompt = (
            "Una tarea de un plan automático falló. Decidí qué hacer.\n\n"
            f"Tarea: {tarea.get('titulo') or tarea['id']}\n"
            f"¿Es idempotente (repetirla no duplica efectos)?: "
            f"{'sí' if tarea.get('idempotente') else 'no'}\n"
            f"Intento {tarea.get('intentos')} de {tarea.get('max_intentos')}\n"
            f"Error: {error}\n\n"
            "Respondé UNA sola palabra:\n"
            "- REINTENTAR si el error es transitorio (429, timeout de red, "
            "lock, un servicio que no estaba arriba).\n"
            "- FALLAR si el error es real y repetirlo va a dar lo mismo.\n"
            "- PREGUNTAR si hace falta que un humano decida."
        )
        try:
            agente = experts.Agent(experts.build_model(spec), instructions=(
                "Sos el coordinador de un plan. Contestá con UNA palabra."))
            import asyncio as _asyncio
            r = await _asyncio.wait_for(agente.run(prompt), timeout=45)
            texto = (getattr(r, "output", "") or "").strip().upper()
        except Exception as e:  # noqa: BLE001 — sin coordinador manda la regla
            logger.warning("el coordinador no contestó (%r): usa la regla", e)
            return None
        for palabra, decision in (("REINTENTAR", "reintentar"),
                                  ("FALLAR", "fallar"),
                                  ("PREGUNTAR", "preguntar")):
            if palabra in texto:
                # Un nodo NO idempotente no se reintenta ni aunque el
                # coordinador lo pida: el modelo no puede saber si el
                # efecto ya ocurrió, y esa es justo la regla que el
                # humano fijó. El coordinador puede ablandar un FALLAR a
                # PREGUNTAR, no al revés.
                if decision == "reintentar" and not tarea.get("idempotente"):
                    logger.info(
                        "el coordinador pidió reintentar %s pero no es "
                        "idempotente: pregunto", tarea.get("id"))
                    return "preguntar"
                return decision
        return None

    return coordinar


def verificador_del_grafo(project: dict, *, modelo: str = ""):
    """Devuelve el `verificar(...)` de cierre. Reusa `experts._run_verifier`.

    Es el MISMO turno que ya corre en los chats y el mismo vocabulario de
    veredictos (`complete` | `needs_more` | `needs_human` | `off_plan`):
    escribir un segundo verificador sería mantener dos criterios que
    tienen que decir lo mismo. Lo único propio de acá es el mapeo —el
    objetivo del grafo como pedido, los nodos como plan, el resumen de lo
    que produjeron como resultado del ejecutor— y el modelo, que sale de
    la misma cascada que el verificador de los chats.

    Devuelve un dict y no la 5-tupla de `_run_verifier` para que el
    orquestador no tenga que conocer su forma: este archivo es el único
    lugar del módulo que habla con `experts`.

    NO atrapa: si el verificador revienta, lo atrapa
    `_verificar_al_cerrar`, que es quien decide que eso no voltea el
    grafo.
    """
    from . import config, experts

    spec = (modelo or (project.get("defaults_json") or {}).get("verifier_model")
            or config.verifier_model_spec())

    async def verificar(*, user: str, plan: str, executor_result: dict) -> dict:
        ponytail = await experts.read_ponytail()
        verdict, feedback, usage, error, _pasos = await experts._run_verifier(
            user=user, plan=plan, executor_result=executor_result,
            model_spec=spec, ponytail=ponytail)
        return {"verdict": verdict, "feedback": feedback, "usage": usage,
                "error": error, "modelo": spec}

    return verificar


async def lanzar(db, project: dict, graph_id: str, *, tope: int = 0,
                 on_cambio=None, progreso_de=None) -> dict:
    """Corre un grafo ya guardado con el reparto real de modelos.

    Es el atajo que usan el endpoint y el disparador del chat, para que
    el reparto —MiniMax ejecuta, nemotron mira los fallos— viva en un
    solo lugar y no en cada llamador.

    Los modelos salen de los flags del proyecto: `model` para el
    ejecutor, `planner_model` para el coordinador y `verifier_model`
    para la verificación de cierre. Es la misma cascada que el resto del
    relay, así que quien ya configuró su proyecto no tiene que
    configurar nada nuevo para el grafo.
    """
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
