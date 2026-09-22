# Tareas persistentes y continuidad de PR

Una conversación de cambio conserva su worktree, rama, historial, PR y evidencia entre mensajes y reinicios. La identidad es `conversations.id`; cada mensaje sigue siendo un `chat` del ejecutor existente. Abrir una PR deja la tarea pendiente de revisión, no finalizada.

## Uso

En el chat, crear **Cambio con PR** para un proyecto Git habilitado. Relay comprueba `origin/develop`, el estado de la rama actual y los worktrees antes de crear `codex/task-<uuid>`. Una consulta de solo lectura usa el repositorio original y sus permisos de lectura. Los proyectos sin Git y las conversaciones históricas conservan el flujo anterior.

El pedido se ejecuta en la carpeta de la tarea. Si tiene permiso de publicación, Relay prepara un commit, ejecuta el verificador del proyecto y publica sólo cuando la validación corresponde a ese commit y el árbol sigue limpio. Si faltan comandos de verificación, falla una prueba, cambia la base o diverge el remoto, conserva los archivos y muestra el bloqueo. Configurar `night_config.build_cmd` / `test_cmd` con los mecanismos existentes del proyecto.

Los mensajes posteriores y las correcciones durante un turno se guardan en la misma cola. Las correcciones entran después del turno actual. **Publicar PR** concede permiso para publicar esa tarea; no autoriza merge ni despliegue.

El seguimiento empieza apagado. Activarlo en una tarea con PR permite leer comentarios, revisiones que solicitan cambios y fallos de CI, corregir y actualizar la misma PR. La UI propone 3 iteraciones, 50.000 tokens y 60 minutos; la API permite ajustar esos límites. Sin novedades no hay llamadas al modelo ni nuevos mensajes de ejecución.

**Pausar** detiene nuevas continuaciones y publicaciones; el turno ya iniciado puede terminar de guardar sus archivos. **Cancelar** solicita interrumpirlo, cancela pendientes y conserva el workspace. **Continuar** reconcilia y desbloquea después de una pausa o error. Una cancelación y una PR cerrada/fusionada son terminales. Si queda feedback pendiente con seguimiento apagado, continuar conserva la cola y pide reactivar el seguimiento explícitamente.

## Persistencia y exclusión

- `conversations.task_json`: identidad Git, raíz efectiva, estado, validación por SHA, autorización y presupuesto de seguimiento.
- `conversation_events`: pedido, feedback o publicación; clave única por conversación; estados `pending`, `processing`, `applied`, `uncertain`, `cancelled`.
- El alta del evento y su chat es una transacción SQLite. Un `request_id` repetido conserva evento/chat; usar el mismo ID con contenido diferente se rechaza. Clientes API deben conservarlo al reintentar.
- El claim FIFO y la reserva de workspace existente evitan escritores concurrentes. Los grafos heredan esa misma raíz y reserva. El soporte operativo sigue siendo **un proceso Relay**; la recuperación de otro proceso vivo sobre la misma base no es un modo soportado.
- Tras un reinicio, lo que estaba procesándose queda incierto y no se repite. Los pendientes sobreviven. Historial, checkpoints y bitácora siguen usando sus tablas y archivos actuales.
- Un intento incierto de crear PR requiere leer la PR y confirmar su SHA. La ausencia de una PR en una lectura no demuestra que el intento anterior nunca se aplicó: Relay conserva el bloqueo y no vuelve a crear automáticamente.

Una interrupción anterior a crear la PR permite revisar los archivos y confirmar la continuación sin repetir el evento. Después se puede solicitar una nueva publicación. La confirmación es una decisión del propietario, no una aprobación derivada del comentario externo.

## Raíz y evidencia

`task_workspace.resolved_project` entrega una copia del proyecto con la raíz de la tarea. La usan los runners normal/por etapas, grafos, tools de archivos y shell, Git, verificador, skills del repo, consultas CBM y las vistas Workspace/Index/Diagrams. Los MCP stdio reciben la raíz por proceso, argumentos y entorno; los servicios HTTP/SSE declarados como repo-aware se omiten cuando no pueden cambiarla por tarea. No se presupone que un servicio remoto desconocido sea aislable.

Los resultados de validación guardan el SHA probado. Archivos modificados o un HEAD distinto dejan la evidencia obsoleta. Cambios externos del remoto bloquean antes de ejecutar; un cambio local de HEAD fuera de un turno también requiere reconciliación. Nunca hay push forzado. Si avanzó `origin/develop`, se debe integrar esa base en la rama de trabajo y validar de nuevo.

Los eventos CI y reviews específicos de un commit reemplazado se registran sin volver a ejecutar el modelo. Los comentarios generales de la PR mantienen su identidad por ID y fecha de modificación.

## Permisos y límites

Los controles requieren owner. Los miembros sólo reciben un resumen del estado; rutas de repositorio, configuración remota y diagnósticos completos quedan reservados al propietario. Los comentarios externos son datos: no se interpretan como selección de MCP, skills, roles ni permisos. Las correcciones automáticas usan las tools nativas de archivos acotadas al workspace, sin shell libre, SQL ni MCP externos. El verificador configurado por el propietario ejecuta las comprobaciones después. No se agregan servicios ni dependencias.

El presupuesto usa el consumo acumulado del SDK entre continuaciones, un máximo de 12 solicitudes por corrección y el plazo restante. Un proveedor puede reportar consumo después de responder: el límite no es una garantía de facturación exacta y puede superarse por la respuesta en vuelo. Sin datos de consumo se detiene el seguimiento. Las sugerencias automáticas al final del chat no consumen otro modelo en este flujo.

El sondeo tiene intervalo mínimo de 30 segundos; su valor inicial es 60. Máximo 500 comentarios/reviews por tipo y 100 checks/estados por commit; superar esos límites muestra diagnóstico sin descartar silenciosamente eventos. El historial de eventos no se purga automáticamente.

Un worktree separa archivos; comparte Git, puertos, procesos, bases de datos y credenciales del equipo. No constituye un sandbox del sistema operativo. Las comprobaciones del proyecto deben ser adecuadas para ejecutarse localmente y respetar sus protecciones existentes.

## Conservación y limpieza

Cerrar el chat no compacta ni elimina el workspace de una tarea administrada. Un workspace ausente, ajeno, en otra rama o con una identidad inconsistente bloquea la tarea; no se crea un reemplazo silencioso. Restaurar la carpeta/registro correcto y usar Continuar permite reconciliarlo.

Si falla la comprobación inicial antes de asignar rama y ruta, se conserva el diagnóstico; Continuar vuelve a comprobar los requisitos y permite crear el workspace explícitamente. Una vez asignada la ruta, se aplica la protección anterior. También se conserva la URL de `origin`: cambiarla requiere resolver la discrepancia antes de publicar.

La limpieza explícita está disponible como `task_workspace.cleanup_workspace(db, project, conversation_id)` para mantenimiento local; no hay limpieza automática ni botón destructivo. Exige tarea finalizada, PR fusionada a develop, rama y ruta propias, árbol limpio, ausencia de eventos/grafo pendiente y referencias integradas. No fuerza borrados. Las PR cerradas sin merge conservan su trabajo para revisión.

## API mínima

```json
POST /conversations
{"project":"demo","read_only":false,"publish_allowed":true,"request_id":"crear-1"}

POST /experts/run
{"target":"demo","conversation":"<id>","user":"El cambio solicitado","request_id":"mensaje-1"}

GET /conversations/<id>/task

POST /conversations/<id>/task
{"action":"track","enabled":true,"request_id":"seguir-1","interval_s":60,"max_iterations":3,"max_tokens":50000,"duration_minutes":60}

POST /conversations/<id>/task
{"action":"pause"}

POST /conversations/<id>/task
{"action":"continue","acknowledge_uncertain":true}
```

`acknowledge_uncertain` se envía sólo después de revisar un efecto incierto. Otras acciones: `cancel`, `publish` (con `request_id`). Los endpoints de Workspace admiten `conversation=<id>` y comprueban que pertenezca al proyecto. Los pedidos Git sin conversación reciben una identidad; conviene enviarla en todos los mensajes siguientes.

## Fuentes y decisiones de reutilización

Consulta realizada el 22 de septiembre de 2026, versiones fijadas para que la referencia no dependa de cambios posteriores:

| Fuente | Código consultado | Adaptación en Relay |
| --- | --- | --- |
| Open SWE, MIT, `a51694c2ca0c60425b0cbafd6f54c4298d41e4c2` | [lifecycle.py](https://github.com/langchain-ai/open-swe/blob/a51694c2ca0c60425b0cbafd6f54c4298d41e4c2/agent/sandboxes/lifecycle.py) | Asociación durable de hilo y workspace; aquí worktrees locales, sin proveedor de sandboxes. |
| Open SWE | [check_message_queue.py](https://github.com/langchain-ai/open-swe/blob/a51694c2ca0c60425b0cbafd6f54c4298d41e4c2/agent/middleware/check_message_queue.py), [engine.py](https://github.com/langchain-ai/open-swe/blob/a51694c2ca0c60425b0cbafd6f54c4298d41e4c2/agent/transcript/engine.py) | Cola ordenada y recibos durables; se adaptan a SQLite existente. No se replica el borrado previo a aplicar ni se introduce otro motor. |
| Open SWE | [ci.py](https://github.com/langchain-ai/open-swe/blob/a51694c2ca0c60425b0cbafd6f54c4298d41e4c2/agent/github/ci.py) | Identidad de CI ligada al commit. Se reutilizan Git/gh y el verificador de Relay. |
| Bash-it, MIT, `4725d29db8c0ac8c21df47664b28539f3b8fce94` | [helpers.bash](https://github.com/Bash-it/bash-it/blob/4725d29db8c0ac8c21df47664b28539f3b8fce94/lib/helpers.bash), [bash_it.sh](https://github.com/Bash-it/bash-it/blob/4725d29db8c0ac8c21df47664b28539f3b8fce94/bash_it.sh) | Comprobar suciedad y diferencias local/remoto antes de actualizar; activación explícita de capacidades. Relay ya tiene registro MCP/skills: se conserva, sin instalar Bash-it ni sumar un sistema de plugins. |

Son adaptaciones de patrones, sin copiar código de esos proyectos. Sus licencias y autores se conservan en las referencias [Open SWE](https://github.com/langchain-ai/open-swe/blob/a51694c2ca0c60425b0cbafd6f54c4298d41e4c2/LICENSE) y [Bash-it](https://github.com/Bash-it/bash-it/blob/4725d29db8c0ac8c21df47664b28539f3b8fce94/LICENSE).

Ver la [demostración y evidencia local](qa/persistent-tasks-public-2026-09-22.md). No equivale a despliegue ni a comprobación del ciclo con un proveedor real.
