# Borradores y solicitudes sin confirmación

Un error de conexión no demuestra que el servidor haya descartado un pedido.
El chat conserva su identidad para consultar el resultado y evitar que un
reintento cree otra conversación o ejecute otra copia del mismo mensaje.

## Qué conserva la página

Cada proyecto tiene su borrador mientras la página siga abierta. **Nueva
conversación** recupera el texto, la issue y los adjuntos de ese proyecto;
cambiar de proyecto no mueve el pedido al proyecto nuevo. También se conserva
el texto sin enviar al volver a abrir una conversación en ese mismo contexto.

Durante un envío, texto y adjuntos quedan asociados a su conversación y al
pedido original. Si escribes algo más mientras esperas, un error o una
respuesta tardía no reemplaza esa edición. Los adjuntos pendientes de subir
impiden enviar hasta terminar su carga.

Estos borradores viven en memoria del contexto de la página. Recargar,
cerrar la página o abrir otra pestaña puede perder el texto local y la
identidad pendiente. El historial y los recibos que ya tenga el servidor son
persistentes; la UI no promete restaurar un borrador local después de recargar.

## Creación de una conversación

Antes de enviar el primer mensaje, el chat crea la conversación con un
`request_id` y un cuerpo fijo. Comprueba el recibo efectivo del servidor:
proyecto, modo, permiso de publicación y vínculo requerido de issue deben
estar listos antes de enviar. Una política puede reducir la publicación
permitida; un recibo no aumenta por sí solo la autoridad del borrador.

Si la creación no queda confirmada, vuelve a consultar el mismo identificador.
Que una consulta todavía no encuentre el resultado no permite empezar otro
pedido con un ID nuevo. Si cambias texto, proyecto, modo o issue, la edición
se conserva y el intento original sigue separado.

Cuando existe un rechazo durable y verificable, la UI puede ofrecer
**Reintentar el mismo pedido** o **Enviar la solicitud corregida**. Antes de
usar otra identidad comprueba la prueba de rechazo del pedido original y pide
confirmación sobre la intención visible. Un código HTTP por sí solo no basta.
Una issue requerida que aún no esté vinculada se reconcilia sobre la misma
conversación; el primer mensaje espera a que el contrato esté listo.

## Envío de un mensaje

Cada mensaje tiene su propio `request_id`, texto, modelos y adjuntos. El cuerpo
se congela para ese intento. Ante un resultado incierto, el chat consulta el
pedido original; si ya se recibió, retoma ese turno en lugar de enviar otro.
Una respuesta aceptada que llegue después de navegar se conserva para volver
al mismo hilo.

Si el servidor no encuentra todavía el pedido, el reintento conserva el mismo
ID y cuerpo. Cuando hay una edición distinta, la UI explica que primero puede
reintentar únicamente el original; la edición continúa como borrador. Un
comando de resultado incierto requiere revisar su registro antes de volver a
ejecutarlo.

Los errores de conexión o de actualización no se presentan como ejecución
terminada. El estado del turno se sigue hasta un final confirmado y entonces
se vuelve a cargar su historial.

## Si estás probando un fallo de conexión

1. Conserva la página abierta y revisa el aviso del chat.
2. Espera a que vuelva la conexión y usa el recorrido de consulta o reintento
   que ofrece la UI. Revisa el pedido original antes de volver a escribirlo
   como uno nuevo.
3. Si el recibo y el estado visible no coinciden, conserva el borrador y el
   identificador para que un Admin investigue; no asumas que falló todo.
4. Antes de recargar, copia cualquier texto local que quieras conservar.

No hace falta reenviar un mensaje recibido sólo porque el panel no pudo
actualizarse. Las confirmaciones de GitHub, publicación y notificación tienen
sus propios estados y permisos; un recibo del chat no las confirma.

## Contrato para integraciones

| Operación | Ruta |
| --- | --- |
| Crear con identidad estable | `POST /conversations` con `request_id` |
| Consultar la creación | `GET /conversations/creation-requests/{request_id}?project={slug}` |
| Solicitar el cierre durable de un intento rechazado | `POST /conversations/creation-requests/{request_id}` |
| Enviar el turno | `POST /experts/run` con `request_id` y conversación |
| Consultar el turno original | `GET /conversations/{id}/requests/{request_id}` |

El protocolo de creación usa recibos y pruebas de rechazo versión 2. La prueba
de rechazo vincula el identificador con el hash SHA-256 del cuerpo canónico.
No reutilices un ID para otro payload ni interpretes `found:false` como una
revocación del intento original. Esta versión prepara conversaciones de
consulta y de trabajo; el recibo debe indicar `media_only:false`.

Consulta [Acceso a conversaciones](CONVERSATION_ACCESS.md) y
[Recuperación durable](DURABLE_RECOVERY.md) para los límites del servidor.

El [contrato del servidor](CONVERSATION_REQUESTS.md) detalla la barrera de
rechazo, preparación del workspace y admisión de eventos para integraciones.
