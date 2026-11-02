# Recuperar ejecuciones y cerrar planes

Una ejecución puede terminar antes de que su resultado se guarde o se revise.
El workspace distingue el trabajo producido, su guardado y la verificación del
plan. Un proceso terminado por sí solo no acredita que el objetivo esté cumplido.

## Qué significa cada situación

| Situación | Qué conserva Relay | Qué hacer |
|---|---|---|
| El ejecutor produjo trabajo y falló una etapa posterior | Respuesta recuperable, historial, bitácora y consumo medido de las tandas terminadas. | Revisa el resultado y el motivo del corte antes de continuar. |
| Se interrumpió el guardado de un resultado conocido | Payload de salida asociado al run; el cierre espera el IO iniciado y consulta si ya se confirmó. | Un reintento de persistencia guarda ese mismo resultado; no vuelve a ejecutar el modelo. |
| El plan tiene nodos terminados y una verificación final pendiente acreditada | Firma de las entradas verificables y estado pendiente de la comprobación. | Usa **Retomar** para completar sólo el cierre. Esa ruta no ejecuta nodos ni concede intentos. |
| Se canceló un nodo antes de registrar su ejecutor | Admisión condicional identificada y sus claims provisionales. | La limpieza revierte sólo esa admisión, si el estado sigue coincidiendo; no consume un intento de modelo inexistente. |
| Un nodo interrumpido agotó sus intentos | Resultados disponibles y una pregunta para decidir cómo seguir. | Responde la pregunta. El límite no aumenta por sanar o por reiniciar. |
| Un feedback automático ya no está permitido al terminar la preparación | Evento omitido y motivo de política. | Revisa los controles de seguimiento; una omisión conocida no se presenta como trabajo ejecutado. |

## Resultado conocido y resultado incierto

El cierre toma una copia del resultado antes de capturas, verificación,
documentación y escrituras que puedan interrumpirse. El payload conserva su
historial y metadata de recuperación en la base; esa metadata no se convierte
en texto crudo de los archivos Markdown o JSONL exportados.

Si se recibe una cancelación mientras un worker de disco o un commit sigue
trabajando, Relay espera su término antes de liberar los controles. Cuando se
pierde la confirmación de una escritura, compara el payload y su origen exactos:
run, proyecto, conversación y nodo. Un resultado distinto o sin procedencia
coincidente exige revisión; no autoriza repetir herramientas o efectos externos.

El resultado completo de un nodo tiene prioridad al recuperar un estado legado.
Si se confirma como terminado, el grafo lo recupera sin ejecutar otro intento.
Si el cierre no puede confirmarse, conserva el resultado disponible y espera
una decisión en lugar de convertir incertidumbre en un reintento automático.

## Verificación final con procedencia

Cada comprobación nueva registra una versión, una firma de las entradas y los
contadores de intentos observados. La intención pendiente se guarda antes de
recoger evidencia o consultar al verificador. Cuando el verificador ya devolvió
un resultado conocido, el cierre protege su persistencia aunque llegue otra
cancelación, preservando también el consumo que se haya medido.

La corrección sintética que anota un cierre bloqueado no constituye otra pasada
del plan. Sólo metadata con versión y campos reconocidos, coincidente con ese
cierre, puede tratarse como tal. Cambiar una etiqueta, una fecha o metadata por
sí solo no autoriza otra consulta al modelo. Los registros antiguos sin una
procedencia suficiente se conservan; no se inventa una historia de ejecución.

La UI oculta un veredicto anterior cuando pertenece a otra firma o la nueva
comprobación sigue pendiente. `verification_pending` describe una comprobación
recuperable acreditada; no debe inferirse sólo porque aparezcan nodos verdes.

**Retomar** comprueba los permisos y el estado actuales. Si existe un cierre
pendiente acreditado, ejecuta exclusivamente esa comprobación. Con el
verificador deshabilitado, sólo puede completar la metadata pendiente que el
contrato permita. Una pausa, cancelación o revocación vigente impide adelantar
ese cierre. La política `graph_verifier` se vuelve a leer antes de la operación.

## Identidad, feedback y límites

Las consultas y ejecuciones usan la cuenta ligada al turno. Tras preparar
herramientas o contexto, las nuevas etapas con conversación vuelven a comprobar
su acceso actual, que el hilo siga abierto y que la tarea permita continuar.
La pertenencia a un proyecto no comparte por sí sola otra conversación.

El feedback automático reserva su admisión en una transacción justo antes del
ejecutor. Comprueba el evento reclamado, su commit, límites, plazo, permisos
actuales y acceso técnico requerido. Una denegación conocida no aumenta el
contador de iteraciones. Si la admisión o el resultado quedan inciertos, el
evento no se reenvía automáticamente. Las decisiones nocturnas conservan además
el actor que solicitó la corrida; una respuesta posterior no sustituye esa
identidad ni sus permisos actuales.

La recuperación conserva límites finitos: tandas, herramientas, rondas de
verificación, intentos de nodos y vueltas del grafo. El progreso por sí solo no
elimina esos topes ni crea una autorización adicional. **Retomar** para un corte
normal puede aplicar el intento explícito que sus controles ya permiten;
**Retomar** exclusivamente un cierre no concede ninguno.

## Límites y evidencia de este candidato

Este comportamiento se comprobó por lectura de fuentes, comparación de contratos,
AST y diff. No se ejecutaron pruebas, aplicación, migraciones, base real,
proveedores ni UI para validar este candidato. No se declara funcionamiento
verificado en la instancia que otra persona esté usando ni CI verde.

Los checkpoints en memoria no sobreviven a terminar el proceso. La recuperación
durable depende de que el resultado y su procedencia hayan llegado al
almacenamiento. No se promete recuperar un resultado que desapareció antes de
ese commit, coordinar procesos distintos ni escribir en un disco inaccesible.
Las copias visibles se sanean, pero esa redacción no repara retroactivamente
archivos o historiales anteriores ni detecta cualquier secreto arbitrario.

Esta documentación describe las rutas genéricas de ejecución y recuperación.
No habilita generación multimedia ni un proveedor, servicio o integración que
no esté configurado y autorizado por separado.
