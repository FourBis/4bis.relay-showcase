"""Loop, recovery and human-response handling for task graphs."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from . import config
from . import orchestrator_questions as _questions
from . import grafo as G
from .orchestrator_verification import (
    _ahora, _seguro, _verificar_al_cerrar, metadata_close_pending,
    verification_close_state, verification_pending,
)

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
    verification_only: bool = False,
) -> dict:
    """Ejecuta el grafo hasta que no quede nada lanzable.

    `ejecutar(tarea) -> {ok, resultado?, error?, chat_id?, modelo?,
    pregunta?}` corre UN nodo. `coordinar(tarea, resultado)` es el
    modelo grande mirando un fallo; puede devolver `"reintentar"`,
    `"fallar"` o `None` para que decida la regla por default.
    `verificar(user=, plan=, executor_result=) -> dict` es la
    verificación de cierre por pasada, ver `_verificar_al_cerrar`.

    Devuelve el progreso final. No lanza por un nodo que falla: un fallo
    es un estado del grafo, no una excepción del orquestador.
    """
    if verification_only:
        from . import conversation_access, user_accounts
        from .task_service import STOPPED, control_lock

        def close_progress(graph, *, blocked=False):
            progress = G.progreso(_nodos(graph))
            if graph.get("estado") == "cancelado":
                progress["estado"] = "cancelado"
            elif close_state := verification_close_state(graph):
                progress["estado"] = close_state
            elif blocked:
                progress["estado"] = graph.get("estado") or progress["estado"]
            if blocked:
                progress["verification_blocked"] = True
            return progress

        # This server-owned continuation closes the existing task snapshot.
        # It must never heal, reserve, execute or create another node, even
        # if that snapshot changed after the HTTP eligibility check.
        g = await db.get_task_graph(graph_id)
        if g is None:
            raise ValueError(f"no existe el grafo {graph_id}")
        cid = g.get("conversation_id")
        metadata_closed = None
        # Match node admission's existing conversation control, but release
        # it before a model call so Pause/Cancel can still stop verification.
        async with control_lock(db, cid) if cid else contextlib.nullcontext():
            g = await db.get_task_graph(graph_id)
            if g is None:
                raise ValueError(f"no existe el grafo {graph_id}")
            if g.get("conversation_id") != cid:
                return close_progress(g, blocked=True)
            state = await db.get_conversation_task(cid) if cid else {}
            bound = user_accounts.current_actor.get()
            if bound is not None:
                if (bound[0].db is not db or not bound[1]
                        or cid and not (await conversation_access.get_access(db, cid, bound[1]))["can_control"]):
                    return close_progress(g, blocked=True)
            if (g.get("estado") != "activo" or state.get("state") in STOPPED
                    or graph_id in getattr(db, "_graph_cancellations", {})
                    or cid in getattr(db, "_task_cancellations", {})):
                return close_progress(g, blocked=True)
            if metadata_close_pending(g, verifier_enabled=verificar is not None):
                # A disabled verifier has no model stage to recover. Complete
                # only existing metadata, never overwrite a newer cancellation.
                await db.run(
                    "UPDATE task_graphs SET estado='hecho',updated_at=? "
                    "WHERE id=? AND estado='activo'",
                    (_ahora(), graph_id))
                closed = await db.get_task_graph(graph_id)
                if closed is None:
                    raise ValueError(f"no existe el grafo {graph_id}")
                metadata_closed = closed
            elif not verification_pending(g, verifier_enabled=verificar is not None):
                return close_progress(g, blocked=True)
        if metadata_closed is not None:
            if on_cambio:
                await _seguro(on_cambio, metadata_closed)
            return close_progress(metadata_closed)
        await _verificar_al_cerrar(
            db, graph_id, g, G.progreso(_nodos(g)), verificar,
            allow_continue=False, close_only=True)
        g = await db.get_task_graph(graph_id)
        if g is None:
            raise ValueError(f"no existe el grafo {graph_id}")
        prog = close_progress(g)
        if on_cambio:
            await _seguro(on_cambio, g)
        return prog

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
        while True:
            vueltas += await _vueltas(db, graph_id, en_curso, ejecutar=ejecutar,
                                     coordinar=coordinar, tope=tope,
                                     on_cambio=on_cambio, max_vueltas=MAX_VUELTAS - vueltas)
            g = await db.get_task_graph(graph_id)
            prog = G.progreso(_nodos(g))
            terminal = prog["estado"] in ("hecho", "fallado")
            if terminal and g.get("estado") != "cancelado":
                if verificar is not None:
                    # Veredicto y corrección se guardan juntos antes de continuar.
                    if await _verificar_al_cerrar(db, graph_id, g, prog, verificar,
                                                  allow_continue=vueltas < MAX_VUELTAS):
                        if on_cambio:
                            await _seguro(on_cambio, await db.get_task_graph(graph_id))
                        continue
                else:
                    await db.set_task_graph_state(graph_id, prog["estado"])
            break
    finally:
        # Salir por excepción, por MAX_VUELTAS o por cancelación no puede
        # dejar nodos corriendo sueltos. Ver `_cerrar_las_que_quedaron`.
        limpieza = asyncio.ensure_future(
            _cerrar_las_que_quedaron(db, graph_id, en_curso, coordinar))
        # Una segunda cancelación tampoco puede soltar el grafo antes
        # de que terminen los nodos y se liberen sus archivos.
        try:
            await asyncio.shield(limpieza)
        except asyncio.CancelledError:
            while not limpieza.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(limpieza)
            limpieza.result()
            raise

    if vueltas >= MAX_VUELTAS:
        logger.error("grafo %s: corté por MAX_VUELTAS", graph_id)

    g = await db.get_task_graph(graph_id)
    prog = G.progreso(_nodos(g))
    if on_cambio:
        await _seguro(on_cambio, await db.get_task_graph(graph_id))
    return prog


async def _vueltas(db, graph_id: str, en_curso: dict, *, ejecutar,
                   coordinar, tope: int, on_cambio, max_vueltas: int = MAX_VUELTAS) -> int:
    """El loop en sí. Devuelve cuántas vueltas dio.

    Vive aparte de `correr_grafo` para que `en_curso` sea de quien
    limpia: el loop lo llena y lo vacía, y el `finally` de afuera lo lee
    para cerrar lo que haya quedado vivo.
    """
    vueltas = 0
    while vueltas < max_vueltas:
        vueltas += 1
        g = await db.get_task_graph(graph_id)
        if g is None:
            raise ValueError(f"no existe el grafo {graph_id}")
        nodos = _nodos(g)
        corriendo = [n for n in nodos if n.id in en_curso]

        from .task_service import STOPPED, control_lock

        cid = g.get("conversation_id")
        # Serializar sólo el inicio con Pausar/Cancelar; los nodos ya lanzados
        # terminan fuera del lock. La corrección pendiente queda para Retomar.
        candidatos = G.elegibles(nodos, tope=tope, en_curso=corriendo)
        for cand in candidatos:
            async with control_lock(db, cid) if cid else contextlib.nullcontext():
                if cid and (await db.get_conversation_task(cid)).get("state") in STOPPED:
                    break
                fila = next(t for t in g["tasks"] if t["id"] == cand.id)
                # `corriendo` PRIMERO y la reserva después. Al revés queda una
                # ventana en la que la reserva existe pero la tarea todavía
                # figura `pendiente`, y el barrido de reservas muertas
                # —que borra las de toda tarea que no esté `corriendo`— se
                # llevaría puesta una reserva recién tomada.
                # A unique timestamp identifies this provisional admission.
                # DB awaits settle their worker before propagating cancellation;
                # cleanup below can therefore refund a committed admission even
                # when cancellation arrived before we registered its executor.
                started = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
                admitted = False
                try:
                    changed = await db.run(
                        "UPDATE tasks SET estado='corriendo',started_at=?,intentos=intentos+1 "
                        "WHERE id=? AND graph_id=? AND estado='pendiente' "
                        "AND intentos=? AND intentos<max_intentos RETURNING id",
                        (started, cand.id, graph_id, cand.intentos))
                    if not changed:
                        continue
                    await db.run("UPDATE task_graphs SET updated_at=? WHERE id=?",
                                 (_ahora(), graph_id))
                    pisados = await db.claim_task_files(
                        cand.id, graph_id, list(cand.archivos))
                    if pisados:
                        logger.warning(
                            "tarea %s no arranca: %s ya está tomado por otra",
                            cand.id, ", ".join(pisados))
                        await _rollback_admission(db, graph_id, fila, started)
                        continue
                    en_curso[cand.id] = asyncio.create_task(ejecutar(dict(fila)))
                    admitted = True
                except BaseException:
                    if not admitted:
                        await _rollback_admission(db, graph_id, fila, started)
                    raise
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
            await _cerrar_tarea_protegida(db, graph_id, tid, task, coordinar)
            del en_curso[tid]
    return vueltas


async def _rollback_admission(db, graph_id, before, started):
    """Finish the conditional pre-executor refund before releasing controls."""
    cleanup = asyncio.create_task(db.rollback_task_admission(
        before["id"], graph_id, started_at=started, before=before))
    try:
        return await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        while not cleanup.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(cleanup)
        cleanup.result()
        raise


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
    suelta los archivos y conserva el resultado conocido. Un cierre
    interrumpido para y pregunta antes de repetirlo: no concede intentos.
    """
    if not en_curso:
        return
    quedaron = list(en_curso.items())
    en_curso.clear()
    for _, task in quedaron:
        task._relay_node_shutdown = True
        task.cancel()
    # Que terminen de cancelarse antes de leerles el resultado.
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait([t for _, t in quedaron])
    for tid, task in quedaron:
        try:
            await _cerrar_tarea_protegida(db, graph_id, tid, task, None, shutting_down=True)
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

    Sin resultado durable se consulta la regla común de reintentos. Sólo
    una tarea idempotente con intentos disponibles vuelve a `pendiente`.
    Las demás esperan una decisión explícita; sanar nunca aumenta el
    límite. También repara pendientes agotadas que una versión anterior
    pudo dejar lanzables. Un resultado durable completo tiene prioridad:
    se recupera sin otro intento aunque ya se haya agotado el límite.

    Devuelve cuántas sanó. Idempotente: correrla dos veces no cambia
    nada la segunda.
    """
    g = await db.get_task_graph(graph_id)
    if g is None:
        return 0
    motivo = "se cortó el proceso mientras esta tarea corría"
    n = 0
    for fila in g["tasks"]:
        nodo = G.Nodo.desde_fila(fila, fila["deps"])
        agotada = nodo.intentos >= nodo.max_intentos
        if fila["estado"] != G.CORRIENDO and not (
                fila["estado"] == G.PENDIENTE and agotada):
            continue
        await db.release_task_files(fila["id"])
        durable = await _durable_node_outcome(db, graph_id, fila, g)
        if durable is not None:
            await db.update_task(fila["id"], estado=G.HECHO if durable["ok"] else G.ESPERANDO,
                resultado=durable["resultado"][:4000], modelo=durable["modelo"],
                error="" if durable["ok"] else motivo + "; resultado conservado, revisión pendiente",
                ended_at=_ahora())
            if not durable["ok"]:
                await _preguntar_por_la_tarea(db, graph_id, fila, motivo,
                    chat_id=fila.get("chat_id") or "", interrupted=True)
            n += 1
            continue
        if G.decidir_tras_fallo(nodo) == "reintentar":
            await db.update_task(fila["id"], estado=G.PENDIENTE,
                                 error=f"{motivo}; se rehace")
        else:
            error = (f"límite de intentos agotado ({nodo.intentos}/{nodo.max_intentos}); "
                     "requiere una decisión explícita antes de volver a ejecutar"
                     if agotada else motivo)
            # An interrupted attempt with no durable result is uncertain,
            # not evidence of functional failure. Wait for the existing human
            # response path, which can explicitly grant one further attempt.
            await db.update_task(fila["id"], estado=G.ESPERANDO, error=error)
            await _preguntar_por_la_tarea(db, graph_id, fila, error,
                chat_id=fila.get("chat_id") or "", interrupted=agotada)
        n += 1
    return n


async def _durable_node_outcome(db, graph_id, row, graph):
    """Recover only this node's committed output, never infer completion by text."""
    if not row.get("chat_id") or not hasattr(db, "run"):
        return None
    rows = await db.run("SELECT c.status,c.source,c.project_slug,c.conversation_id,o.payload FROM chats c "
        "JOIN chat_outputs o ON o.chat_id=c.id WHERE c.id=?", (row["chat_id"],))
    if not rows:
        return None
    rejected = dict(ok=False,
        resultado=row.get("resultado") if isinstance(row.get("resultado"), str) else "",
        modelo=row.get("modelo") if isinstance(row.get("modelo"), str) else "")
    if rows[0].get("source") != "grafo":
        return rejected
    if (graph.get("project_slug") and rows[0].get("project_slug") != graph["project_slug"]
            or graph.get("conversation_id") and rows[0].get("conversation_id") != graph["conversation_id"]):
        return rejected
    try:
        artifact = json.loads(rows[0]["payload"])
        if not isinstance(artifact, dict) or not isinstance(artifact.get("content", ""), str) \
                or not isinstance(artifact.get("model", ""), str):
            return rejected
        result_metadata = artifact.get("result_metadata") or {}
        if not isinstance(result_metadata, dict):
            return rejected
        metadata = result_metadata.get("graph_node") or {}
        if not isinstance(metadata, dict):
            return rejected
        if artifact.get("chat_id") != row["chat_id"] or metadata and (
                metadata.get("graph_id") != graph_id or metadata.get("task_id") != row["id"]):
            return rejected
        complete = (rows[0]["status"] == "ok" and metadata.get("complete") is True
                    and metadata.get("graph_id") == graph_id and metadata.get("task_id") == row["id"])
        return dict(ok=complete, resultado=artifact.get("content") or "", modelo=artifact.get("model") or "")
    except (ValueError, TypeError, AttributeError):
        return rejected


async def aplicar_respuesta(db, graph_id: str, task_id: str,
                            decision: str, texto: str = "") -> str:
    """Aplica una decisión humana conservando los callbacks de la fachada."""
    return await _questions.aplicar_respuesta(
        db, graph_id, task_id, decision, texto,
        _nodos=_nodos, _detalle_con_respuesta=_detalle_con_respuesta)


async def responder_a_la_tarea(db, chat_id: str, respuesta: str) -> str:
    """Adjunta la respuesta humana al nodo que la solicitó."""
    return await _questions.responder_a_la_tarea(
        db, chat_id, respuesta, _detalle_con_respuesta=_detalle_con_respuesta)


from .orchestrator_questions import _detalle_con_respuesta


async def _cerrar_tarea_protegida(db, graph_id, tid, task, coordinar, *, shutting_down=False):
    """One closing future per node; cancellation never repeats a coordinator."""
    if shutting_down:
        task._relay_node_shutdown = True
    closure = getattr(task, "_relay_node_closing", None)
    if (closure is not None and closure.done() and (closure.cancelled() or closure.exception() is not None)
            and not getattr(task, "_relay_node_close_retried", False)):
        # A bounded persistence-only recovery; never call the coordinator again.
        task._relay_node_close_retried = True
        task._relay_node_shutdown = True
        shutting_down, coordinar, closure = True, None, None
    if closure is None:
        closure = asyncio.create_task(_cerrar_tarea(db, graph_id, tid, task, coordinar,
                                                   shutting_down=shutting_down))
        task._relay_node_closing = closure
    cancelled = False
    while not closure.done():
        try:
            await asyncio.shield(closure)
        except asyncio.CancelledError:
            cancelled = True
            task._relay_node_shutdown = True
        except Exception:
            break
    try:
        closure.result()
    except Exception as exc:
        if cancelled:
            raise asyncio.CancelledError from exc
        raise
    if cancelled:
        raise asyncio.CancelledError


async def _cerrar_tarea(db, graph_id: str, tid: str, task, coordinar, *, shutting_down=False) -> None:
    """Pasa una tarea terminada a su estado final y aplica las reglas."""
    try:
        res = task.result()
    except asyncio.CancelledError as exc:
        saved = getattr(task, "_relay_node_result", {})
        saved = saved if isinstance(saved, dict) else {}
        checkpoint = getattr(task, "_relay_node_checkpoint", {})
        durable = isinstance(checkpoint, dict) and checkpoint.get("durable") is True
        res = {**saved, "ok": saved.get("ok") is True and durable, "_interrupted": True}
        if not res["ok"]:
            res["error"] = saved.get("error") or "cancelada; revisa el resultado conservado antes de continuar"
            if exc.__cause__ is not None and not durable:
                res["error"] += f"; cierre sin confirmar ({type(exc.__cause__).__name__})"
    except Exception as e:  # noqa: BLE001 — un nodo roto no voltea el grafo
        logger.exception("tarea %s reventó", tid)
        saved = getattr(task, "_relay_node_result", {})
        saved = saved if isinstance(saved, dict) else {}
        checkpoint = getattr(task, "_relay_node_checkpoint", {})
        durable = isinstance(checkpoint, dict) and checkpoint.get("durable") is True
        res = {**saved, "ok": saved.get("ok") is True and durable,
               "error": f"{type(e).__name__}: {e}", "_interrupted": True}

    checkpoint = getattr(task, "_relay_node_checkpoint", {})
    if (isinstance(checkpoint, dict) and checkpoint.get("artifact") and not checkpoint.get("durable")
            and not getattr(task, "_relay_node_persist_retried", False)):
        from . import finalization
        task._relay_node_persist_retried = True
        try:
            await finalization.resume_checkpoint(db, checkpoint["artifact"]["chat_id"], checkpoint)
        except Exception as exc:
            finalization.retain_node_checkpoint(db, checkpoint["artifact"]["chat_id"], checkpoint)
            res.update(ok=False, _interrupted=True,
                error=f"No se confirmó el resultado del nodo ({type(exc).__name__}); requiere revisión")
            logger.exception("cierre durable del nodo %s pendiente", tid)
        else:
            saved = getattr(task, "_relay_node_result", {})
            if isinstance(saved, dict) and saved.get("ok") is True:
                res.update(saved, ok=True)

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

    if res.get("resultado"):
        comun["resultado"] = res["resultado"][:4000]

    # Falló. Acá entra el modelo grande, y SOLO acá.
    # `or {"tasks": []}`: si el grafo se borró mientras corría —que es
    # una de las formas de llegar acá desde la limpieza— igual hay que
    # cerrar la tarea, no reventar encima del error que ya teníamos.
    g = await db.get_task_graph(graph_id) or {"tasks": []}
    fila = next((t for t in g["tasks"] if t["id"] == tid), None)
    nodo = G.Nodo.desde_fila(fila, fila["deps"]) if fila else None
    decision = None
    interrupted = shutting_down or getattr(task, "_relay_node_shutdown", False) is True or res.get("_interrupted") is True
    if interrupted:
        # Observing shutdown/cancel is never authority to buy another model or
        # replay an incomplete result, even when the task was idempotent.
        decision = "preguntar" if nodo is not None else "fallar"
    elif nodo is not None and nodo.intentos >= nodo.max_intentos:
        decision = "fallar"
    elif coordinar is not None and nodo is not None:
        decision = await _seguro(coordinar, fila, res)
    if getattr(task, "_relay_node_shutdown", False) is True:
        interrupted = True
        decision = "preguntar" if nodo is not None else "fallar"
    if decision not in ("reintentar", "fallar", "preguntar"):
        decision = G.decidir_tras_fallo(nodo) if nodo else "fallar"
    if decision == "reintentar" and (nodo is None or G.decidir_tras_fallo(nodo) != "reintentar"):
        # The coordinator recommends; durable attempt/idempotence limits decide.
        decision = G.decidir_tras_fallo(nodo) if nodo else "fallar"

    error = (res.get("error") or "sin detalle")[:2000]
    if decision == "reintentar":
        # Vuelve a `pendiente`: la próxima vuelta del loop lo re-elige.
        await db.update_task(tid, estado=G.PENDIENTE, error=error, **comun)
        if getattr(task, "_relay_node_shutdown", False) is True:
            await db.update_task(tid, estado=G.ESPERANDO, error=error, **comun)
            await _preguntar_por_la_tarea(db, graph_id, fila, error,
                chat_id=comun.get("chat_id") or (fila or {}).get("chat_id") or "", interrupted=True)
            return
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
                                      (fila or {}).get("chat_id") or "", interrupted=interrupted)
        logger.info("tarea %s falló y para a preguntar (no idempotente)", tid)
        return

    await db.update_task(tid, estado=G.FALLADO, error=error, **comun)
    g = await db.get_task_graph(graph_id)
    for otro in G.bloqueados_por(_nodos(g), tid):
        await db.update_task(otro, estado=G.BLOQUEADO,
                             error=f"bloqueada: {tid} falló")
    logger.warning("tarea %s falló definitivamente: %s", tid, error[:120])


from .orchestrator_questions import _preguntar_por_la_tarea
