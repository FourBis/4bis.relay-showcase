# Contrato de creación y reconciliación de conversaciones

El código de esta preparación local añade contratos de creación y recuperación. Los ejemplos son ficticios. La presencia del código no demuestra que una instancia instalada lo esté ejecutando; no se ejecutaron migraciones ni pruebas funcionales en esta tanda.

Esta es la referencia del servidor y sus integraciones. El recorrido de la UI y
sus borradores se explica en [Borradores y solicitudes sin confirmación](REQUEST_RECOVERY.md).

## Conserva la identidad del pedido

Un cliente debe conservar el `request_id` y el cuerpo exacto mientras un envío esté pendiente o tenga resultado incierto. El servidor conserva una huella SHA-256 del cuerpo original y el modo efectivo de la conversación. Reutilizar un ID con otro contenido no modifica la solicitud anterior.

Crear una conversación y preparar su workspace son pasos distintos. El recibo comunica si la creación sigue en preparación, si existe un bloqueo y si la issue requerida quedó vinculada por el servidor. Resolver una issue en GitHub no equivale a haberla vinculado de forma durable.

Las conversaciones de escritura Git usan una rama y worktree desde `develop` comprobada. Una consulta explícita de lectura o un proyecto sin Git conserva su raíz permitida sin reservar un checkout de escritura. Los históricos vacíos no se migran automáticamente a ese modo.

En esta preparación la creación nativa de audio/vídeo no está conectada. El campo contractual `media_only` se devuelve como `false` en las creaciones admitidas; conservar ese campo no habilita una generación.

## Resultado incierto y rechazo definitivo

Un timeout, desconexión, excepción o respuesta 500 no demuestra que no se haya creado la conversación. Una consulta que todavía no encuentra el ID tampoco lo demuestra: la petición original podría seguir en vuelo. Conserva el pedido y reconcilia el mismo intento.

Un rechazo definitivo anterior a la creación puede llevar una prueba durable. El servidor verifica el actor y la huella y deja una barrera que impide un INSERT tardío con ese ID lógico, incluidas variantes de mayúsculas del proyecto. Si ya ganó una conversación, conserva esa fila y su control de acceso; no entrega una prueba de ausencia.

La prueba no incluye el cuerpo del pedido ni texto del proveedor. No concede acceso al proyecto, escritura, publicación, una issue o una ejecución de modelo. Un reintento vuelve a validar sus permisos.

La UI permite confirmar el reintento del mismo pedido o enviar una solicitud corregida una vez acreditado el cierre del intento anterior. Sólo entonces crea otra identidad. No hay reenvío automático con un ID nuevo para resolver incertidumbre.

## API de reconciliación

| Recurso | Uso |
|---|---|
| `POST /conversations` | Crea o reconcilia el mismo cuerpo y `request_id`; devuelve el recibo de creación cuando corresponde. |
| `GET /conversations/creation-requests/{request_id}?project=demo` | Consulta la creación o una barrera de rechazo ya acreditada. La ausencia no autoriza otro ID. |
| `POST /conversations/creation-requests/{request_id}` | Asienta explícitamente el cierre del intento original si la base puede demostrarlo, sin ejecutar el pedido. Puede recibir el descriptor compacto de su huella. |
| `GET /conversations/{id}/requests/{request_id}` | Reconcilia el evento propio sin ejecutar ni devolver su payload. Compartir una conversación no permite apropiarse de una solicitud de otra cuenta. |
| `GET /conversations/{id}/shares` | Consulta destinatarios y permisos actuales si puedes compartir el hilo. |
| `PUT /conversations/{id}/shares/{email}` | Concede `read` o `collaborate` a una cuenta elegible. |
| `DELETE /conversations/{id}/shares/{email}` | Revoca la invitación conservando su registro. |

Las respuestas de reconciliación no son permisos para nuevas ejecuciones. El primer mensaje puede aportar `expected_creation`: se compara en la misma transacción de encolado con el modo y vínculo de issue reales, después de reconciliar un evento duplicado existente.

## Cola, preparación y recuperación

La cola conserva mensajes humanos y feedback con identidades distintas. Un feedback suspendido o sin presupuesto no impide atender un nuevo mensaje humano elegible. La reserva de un evento se registra antes de preparar su ejecución; cancelar durante esa reserva espera su asentamiento y conserva el resultado conocido del claim.

El feedback automático revalida su cuenta, permisos, commit, política y plazo en la admisión final del ejecutor. Una omisión conocida antes de admitirlo no consume una iteración. Una cancelación posterior a la admisión permanece incierta o cancelada según su resultado conocido; no se convierte en permiso para repetir efectos.

Los borradores de una página no constituyen almacenamiento durable entre recargas. Los locks y recibos de este proceso no acreditan recuperación tras terminarlo abruptamente, coordinación de varios procesos ni disponibilidad del disco.

Véanse [acceso y conocimiento](CONVERSATION_ACCESS.md) y [continuidad del trabajo](LONG_RUNNING_WORK.md). La generación multimedia y su nueva terminología esperan su revisión específica y no forman parte de estos contratos genéricos.
