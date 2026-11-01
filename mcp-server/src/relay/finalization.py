"""Resultado durable primero; exportación recuperable después, sin repetir el LLM."""
import asyncio
from copy import deepcopy
import json
import logging
import time
from functools import wraps

from . import persist

logger = logging.getLogger("relay.finalization")


def retain_node_checkpoint(db, chat_id, checkpoint):
    """Keep an unconfirmed full result alive for manual persistence recovery."""
    if not hasattr(db, "_retained_node_results"):
        db._retained_node_results = {}
    db._retained_node_results[chat_id] = checkpoint


def _release_retained_checkpoint(db, chat_id, checkpoint):
    retained = getattr(db, "_retained_node_results", {})
    if retained.get(chat_id) is checkpoint:
        retained.pop(chat_id)

#: Un candado por event loop, no por chat. El cierre normal y el barrido
#: de pendientes exportan el mismo chat sin excluirse: `finish` deja la
#: fila con `exported=0` y recién después escribe los archivos, así que
#: `retry_pending` puede levantarla en esa ventana y appendear la
#: respuesta una segunda vez.
#:
#: Uno global y no uno por chat_id a propósito: exportar es escribir un
#: .md y una línea, o sea milisegundos, y un candado por clave pide un
#: ciclo de vida (cuándo se borra la entrada sin dejar afuera a un
#: waiter) que es justo donde se cuelan las carreras que esto viene a
#: cerrar. Con los grafos en serie, dos exportaciones simultáneas ya son
#: raras.
#:
#: Va por loop y no a nivel de módulo porque los primitivos de asyncio se
#: atan al loop en el primer uso: uno solo reventaría en la suite, donde
#: cada test corre en el suyo.
#:
#: Esto fue un `WeakKeyDictionary` y no servía para nada: un `asyncio.
#: Lock` guarda una referencia FUERTE a su loop desde el primer uso, así
#: que el valor mantenía viva a su propia clave y la entrada no se
#: recolectaba nunca. Parecía resolver la retención y no la resolvía —el
#: peor tipo de mecanismo—. Se barren los loops cerrados a mano, que es
#: chequeable: un loop cerrado no puede estar corriendo, así que no hay
#: carrera al sacarlo.
_CANDADOS: dict = {}


def _candado() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    for muerto in [l for l in _CANDADOS if l.is_closed()]:
        _CANDADOS.pop(muerto, None)
    lock = _CANDADOS.get(loop)
    if lock is None:
        lock = _CANDADOS[loop] = asyncio.Lock()
    return lock


#: Espera antes del siguiente intento, por número de intento. El PRIMERO
#: es 0 a propósito: el loop de reintentos ya corre cada 60s, y una falla
#: transitoria —un lock, un antivirus abriendo el archivo— se arregla
#: sola ahí. Demorar también el primer intento solo retrasa el caso
#: fácil. El backoff empieza cuando la falla se repite, que es cuando
#: deja de ser transitoria.
#:
#: El último valor se repite: media hora es suficiente para que un
#: humano arregle un disco lleno o un permiso mal puesto, y más que eso
#: convierte una falla transitoria en una exportación olvidada.
_BACKOFF_EXPORT_S = (0, 120, 600, 1800)


def _proximo_intento(intentos: int):
    """Cuándo reintentar. `None` = en la próxima vuelta, sin esperar."""
    espera = _BACKOFF_EXPORT_S[min(intentos, len(_BACKOFF_EXPORT_S)) - 1]
    if espera <= 0:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() + espera))


async def _intentos_de(db, chat_id: str) -> int:
    filas = await db.run(
        "SELECT intentos FROM chat_outputs WHERE chat_id=?", (chat_id,))
    return int(filas[0]["intentos"] or 0) if filas else 0


async def _ya_exportado(db, chat_id: str) -> bool:
    filas = await db.run(
        "SELECT exported FROM chat_outputs WHERE chat_id=?", (chat_id,))
    return bool(filas and filas[0]["exported"])


async def _md_guardado(db, chat_id: str):
    """El `md_path` que dejó quien exportó primero.

    Devolverlo y no `None` importa: el caller escribe lo que reciba en la
    fila del chat, y un `None` ahí borraría el puntero al .md que sí
    existe.
    """
    fila = await db.get_chat(chat_id)
    return (fila or {}).get("md_path")


def supervise(handler):
    """Una excepción al finalizar tampoco deja el registro en ejecución."""
    @wraps(handler)
    async def guarded(*args, **kwargs):
        chat_id, db = kwargs["chat_id"], kwargs["db"]
        checkpoint = {}
        kwargs["_close_checkpoint"] = checkpoint
        try:
            return await handler(*args, **kwargs)
        except (asyncio.CancelledError, Exception) as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            status = "cancelled" if cancelled else "error"
            error = "cancelado durante el cierre" if cancelled else f"falló el cierre: {exc}"
            logger.warning("chat %s: %s", chat_id, error, exc_info=not cancelled)
            try:
                async def reconcile():
                    nonlocal status, error
                    if checkpoint.get("artifact") and not checkpoint.get("durable"):
                        status, error = await _recover_result(db, chat_id, checkpoint, status=status, error=error)
                    else:
                        row = await db.get_chat(chat_id)
                        if row and row.get("status") == "running":
                            await db.finish_chat(chat_id, status=status, error=error)
                        elif row:
                            status, error = row["status"], row.get("error")
                recovery = asyncio.create_task(reconcile())
                while not recovery.done():
                    try:
                        await asyncio.shield(recovery)
                    except asyncio.CancelledError:
                        continue  # includes early metadata-only runs, not just results.
                recovery.result()
            except Exception:
                if checkpoint.get("artifact") and not checkpoint.get("durable"):
                    # Retener también el resultado HTTP si el estado SQL no
                    # puede confirmarse. No depender del lifetime del handler.
                    retain_node_checkpoint(db, chat_id, checkpoint)
                logger.exception("chat %s: base no disponible para cerrar", chat_id)
            progress = kwargs["progress"].get(chat_id)
            if progress is not None:
                progress.phase, progress.error = status, error
        finally:
            kwargs["running"].pop(chat_id, None)
            progress = kwargs["progress"].get(chat_id)
            if progress is not None:
                progress.finished = True
                progress.closing = False
    return guarded


async def _recover_result(db, chat_id, checkpoint, *, status, error):
    """Cerrar con el resultado ya producido, sin vincular archivos ni ejecutar modelos."""
    confirmed = await _confirm_existing_checkpoint(db, chat_id, checkpoint)
    if confirmed is not None:
        return confirmed
    artifact = {**checkpoint["artifact"], "status": status, "error": error}
    fields = {**checkpoint["fields"], "error": error, "phase_at_end": status}
    cid = checkpoint.get("conversation_id")
    if cid and artifact.get("messages_json"):
        try:
            await db.save_conversation_messages(cid, artifact["messages_json"],
                                                expected=checkpoint.get("history_before"))
        except Exception:
            # El payload también conserva ese historial si cambió el hilo o
            # falló el CAS. No sobrescribir otro turno para recuperar éste.
            logger.warning("chat %s: historial conservado en resultado durable", chat_id)
    bitacora = (artifact.get("result_metadata") or {}).get("bitacora_json")
    if cid and bitacora:
        try:
            await db.save_conversation_bitacora(cid, bitacora)
        except Exception:
            logger.warning("chat %s: bitácora conservada en resultado durable", chat_id)
    await finish(db, chat_id, artifact=artifact, status=status,
                 checkpoint=checkpoint, **fields)
    return status, error


async def export(db, chat_id: str, artifact: dict | str, *, reintento: bool = False):
    """Escribe los artefactos y marca el chat como exportado.

    `reintento` existe porque las dos escrituras no son igual de
    reversibles: el .md se sobrescribe, el JSONL es un append. Si la
    primera vuelta escribió los dos y falló recién al confirmar en
    SQLite, repetir el append duplicaba la respuesta en el historial del
    proyecto. Se consulta el archivo solo en ese caso — en el camino
    normal el append es la primera y única escritura, y escanear el
    JSONL entero en cada cierre de chat sería O(n) sobre todo el
    historial.
    """
    async with _candado():
        try:
            # Dentro del candado, y no antes: el resultado queda en
            # `chat_outputs` con `exported=0` mientras se escriben los
            # archivos, asi que el barrido puede levantarlo justo
            # entonces y exportar en paralelo con el cierre normal. Sin
            # este re-chequeo quedaba `user, assistant, assistant`.
            if await _ya_exportado(db, chat_id):
                return await _md_guardado(db, chat_id)
            # Parsear dentro del cierre protegido: una fila corrupta
            # conserva su diagnóstico y no impide exportar las siguientes.
            if isinstance(artifact, str):
                artifact = json.loads(artifact)
            escribir_jsonl = True
            if reintento:
                escribir_jsonl = not await persist.jsonl_tiene_respuesta(
                    artifact.get("target") or "", chat_id)
            path = await persist.write_chat_artifacts(
                **artifact, filename=f"{chat_id}.md",
                escribir_jsonl=escribir_jsonl)
            await db.run_tx([
                ("UPDATE chats SET md_path=? WHERE id=?", (path, chat_id)),
                ("UPDATE chat_outputs SET exported=1 WHERE chat_id=?",
                 (chat_id,)),
            ])
            return path
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "chat %s: resultado guardado; exportación pendiente", chat_id)
            # Por qué falló y cuándo reintentar. Antes esto solo vivía en
            # el log del relay, que no se persiste: la fila decía
            # `exported=0` y para diagnosticarla había que haber estado
            # mirando. El backoff evita que un disco lleno se coma un
            # intento por vuelta del barrido, para siempre.
            try:
                await db.run(
                    "UPDATE chat_outputs SET intentos=intentos+1, "
                    "ultimo_error=?, proximo_intento_at=? WHERE chat_id=?",
                    (f"{type(e).__name__}: {e}"[:300],
                     _proximo_intento(await _intentos_de(db, chat_id) + 1),
                     chat_id))
            except Exception:  # noqa: BLE001 — anotar el fallo no puede fallar el run
                logger.exception("chat %s: no pude anotar el reintento", chat_id)
            return None


async def finish(db, chat_id: str, *, artifact: dict, checkpoint=None, **fields):
    # Una transacción conserva el resultado y el estado terminal. Si el
    # disco de exportación falla, el chat sigue terminado y se puede leer.
    if checkpoint is None:
        checkpoint = {}
    artifact, fields = deepcopy(artifact), deepcopy(fields)
    # El snapshot de confirmación debe ser exactamente lo enviado a SQLite,
    # también cuando cambió error/fase durante el postprocesado HTTP.
    checkpoint.update(artifact=artifact, fields={key: value for key, value in fields.items() if key != "status"})
    writer = asyncio.create_task(db.finish_chat(chat_id, artifact=artifact, **fields))
    cancelled = False
    while not writer.done():
        try:
            await asyncio.shield(writer)
        except asyncio.CancelledError:
            # La cancelación del caller no cancela el commit SQLite. Conocer
            # su resultado antes de decidir si hace falta recuperar evita
            # reescribir/exportar otra vez una fila ya confirmada.
            cancelled = True
        except Exception:
            break
    try:
        writer.result()
    except Exception as exc:
        if cancelled:
            raise asyncio.CancelledError from exc
        raise
    checkpoint["durable"] = True
    _release_retained_checkpoint(db, chat_id, checkpoint)
    if cancelled:
        raise asyncio.CancelledError
    return await export(db, chat_id, artifact)


async def _confirm_existing_checkpoint(db, chat_id, checkpoint):
    """None means confirmed absence, never a failed lookup or different result."""
    artifact = checkpoint["artifact"]
    rows = await db.run("SELECT c.source,c.target,c.project_slug,c.conversation_id,c.status,c.error,"
        "o.payload,o.exported FROM chats c LEFT JOIN chat_outputs o ON o.chat_id=c.id WHERE c.id=?", (chat_id,))
    node = (artifact.get("result_metadata") or {}).get("graph_node") or {}
    expected_cid = node.get("conversation_id") if node else checkpoint.get("conversation_id")
    if (artifact.get("chat_id") != chat_id or not rows
            or rows[0].get("source") != artifact.get("source")
            or (rows[0].get("target") or rows[0].get("project_slug") or "_orphan") != artifact.get("target")
            or rows[0].get("conversation_id") != expected_cid):
        raise RuntimeError("No se confirmó la procedencia del resultado de cierre")
    row = rows[0]
    if row.get("payload") is None:
        return None
    if (json.loads(row["payload"]) != artifact or row.get("status") not in {"ok", "error", "cancelled"}):
        raise RuntimeError("El resultado durable cambió; el cierre requiere revisión")
    checkpoint["durable"] = True
    _release_retained_checkpoint(db, chat_id, checkpoint)
    if not row.get("exported"):
        await export(db, chat_id, artifact, reintento=True)
    return row["status"], row.get("error")


async def resume_checkpoint(db, chat_id, checkpoint):
    """Retry only a node's known result; never reset an already confirmed export."""
    if checkpoint.get("durable"):
        _release_retained_checkpoint(db, chat_id, checkpoint)
        return
    artifact = checkpoint["artifact"]
    node = (artifact.get("result_metadata") or {}).get("graph_node") or {}
    row = await db.get_chat(chat_id)
    if (artifact.get("chat_id") != chat_id or not node.get("task_id") or not node.get("graph_id")
            or not row or row.get("source") != "grafo"
            or row.get("project_slug") != node.get("project_slug")
            or row.get("conversation_id") != node.get("conversation_id")):
        raise RuntimeError("No se confirmó la procedencia del resultado del nodo")
    if await _confirm_existing_checkpoint(db, chat_id, checkpoint) is not None:
        return
    await finish(db, chat_id, artifact=artifact, checkpoint=checkpoint,
                 status=artifact["status"], **checkpoint["fields"])


async def retry_pending(db):
    """Reintenta las exportaciones que quedaron pendientes y ya tocan.

    Ordenado por `proximo_intento_at` y respetando el backoff: sin eso,
    las 50 primeras filas se llevaban todos los intentos y una
    exportación vieja que fallaba siempre podía tapar a una nueva que
    habría andado. `NULL` primero es la que nunca se intentó.
    """
    ahora = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for row in await db.run(
            "SELECT chat_id, payload FROM chat_outputs WHERE exported=0 "
            "AND (proximo_intento_at IS NULL OR proximo_intento_at<=?) "
            "ORDER BY proximo_intento_at IS NOT NULL, proximo_intento_at "
            "LIMIT 50", (ahora,)):
        await export(db, row["chat_id"], row["payload"],
                     reintento=True)


def turns(payload: str) -> list[dict]:
    """La UI conserva la conversación incluso si su .md no pudo escribirse."""
    data = json.loads(payload)
    result = [{"role": "user", "content": data["user"]},
              {"role": "assistant", "content": data["content"] or "(sin contenido)"}]
    if data.get("error"):
        result.append({"role": "assistant", "content": data["error"]})
    return result
