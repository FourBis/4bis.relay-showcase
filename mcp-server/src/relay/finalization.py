"""Resultado durable primero; exportación recuperable después, sin repetir el LLM."""
import asyncio
import json
import logging
import time
from functools import wraps

from . import persist

logger = logging.getLogger("relay.finalization")

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
        try:
            return await handler(*args, **kwargs)
        except (asyncio.CancelledError, Exception) as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            status = "cancelled" if cancelled else "error"
            error = "cancelado durante el cierre" if cancelled else f"falló el cierre: {exc}"
            logger.warning("chat %s: %s", chat_id, error, exc_info=not cancelled)
            try:
                row = await db.get_chat(chat_id)
                if row and row.get("status") == "running":
                    await db.finish_chat(chat_id, status=status, error=error)
                elif row:
                    status, error = row["status"], row.get("error")
            except Exception:
                logger.exception("chat %s: base no disponible para cerrar", chat_id)
            progress = kwargs["progress"].get(chat_id)
            if progress is not None:
                progress.phase, progress.error = status, error
        finally:
            kwargs["running"].pop(chat_id, None)
            progress = kwargs["progress"].get(chat_id)
            if progress is not None:
                progress.finished = True
    return guarded


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


async def finish(db, chat_id: str, *, artifact: dict, **fields):
    # Una transacción conserva el resultado y el estado terminal. Si el
    # disco de exportación falla, el chat sigue terminado y se puede leer.
    await db.finish_chat(chat_id, artifact=artifact, **fields)
    return await export(db, chat_id, artifact)


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
