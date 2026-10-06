# Cuando una tarea crece mientras se ejecuta

Relay mantiene el pedido, el plan y los cambios asociados al trabajo. Puedes
seguir una tarea larga en el grafo, revisar su diff y continuar en su workspace
sin reconstruir el contexto en cada mensaje.

## Un recorrido concreto

Imagina una migración de una interfaz a un nuevo cliente web:

1. **Organizar:** el planificador crea tareas con dependencias. El grafo permite
   ver qué está terminado, qué corre y qué espera.
2. **Avanzar:** el ejecutor trabaja en el repositorio. La conversación conserva
   el historial y la tarea mantiene su rama y worktree.
3. **Subdividir durante la ejecución:** una tarea agota su presupuesto de
   herramientas con trabajo pendiente. Relay usa su salida parcial para
   proponer subtareas y continuar sobre ese avance.
4. **Actualizar dependencias:** las subtareas quedan guardadas con un padre.
   Los nodos que dependían del padre pasan a esperar a sus subtareas.
5. **Revisar y continuar:** la persona consulta cambios y resultados desde la
   conversación. Publicar una PR es una acción autorizada aparte; terminar el
   plan no implica que el cambio esté integrado o desplegado.

El 24 de septiembre de 2026 se observó este mecanismo en un trabajo local:
un grafo inicial de diez tareas incorporó cuatro subtareas para una de ellas;
al observarlo, una subtarea había terminado y otra estaba ejecutándose.
El trabajo completo seguía en curso. Aquí se describe solo ese comportamiento,
sin nombres de clientes, contenido de repositorios ni capturas de esa sesión.

La [demo interactiva](https://fourbis.github.io/4bis.relay-showcase/) recrea el
recorrido con datos ficticios y pasos controlados en el navegador. No ejecuta
ese trabajo real ni llama a un proveedor de IA.

## Qué significa subdividir

El plan inicial admite de 2 a 20 tareas. Cuando un nodo devuelve `budget_split`,
el autosplit, habilitado por defecto, propone entre 2 y 5 subtareas. El contexto
incluye hasta 3.000 caracteres del resultado parcial y pide no repetir lo hecho.
El padre permanece en el historial; su estado fallido no implica por sí solo
que el grafo haya dejado de avanzar.

La subdivisión es acotada: cada padre se divide una sola vez y las subtareas
no vuelven a dividirse. La comprobación usa el estado persistido y sobrevive
a un reinicio. Si no puede subdividir, la tarea queda para atención humana.
`FOURBIS_GRAFO_AUTOSPLIT=0` desactiva este mecanismo. Reintentar un nodo o
revisar el plan de un chat por etapas son operaciones distintas.

La implementación está en `orchestrator_execution.py` y `db_graph.py`; las
pruebas reproducibles están en `mcp-server/tests/test_orquestador.py`:

```powershell
python -m pytest mcp-server/tests/test_orquestador.py -k "split or subdiv or fan_out or flag_apagado or subtarea" -q
```

## Retomar un corte de presupuesto

Tras un corte por `budget_exceeded` o `budget_split`, **Retomar** concede un
nuevo intento explícito y recupera el historial del nodo antes de abrir otro
chat. Los nodos terminados y los padres ya sustituidos se conservan. Si el
historial falta o es inválido, el siguiente intento recibe la instrucción de
inspeccionar los archivos y comprobar el estado antes de actuar. Las preguntas,
los fallos funcionales y un veredicto global `off_plan` mantienen sus propios
controles. El historial orienta la continuación; un efecto externo incierto
requiere revisión antes de repetirlo.

La persistencia, la reanudación HTTP y el orden de ejecución se comprueban en
`mcp-server/tests/test_graph_budget_resume.py` con base temporal y ejecutor
simulado. Estas pruebas no demuestran el resultado de una ejecución real de IA.

## Continuar criterios pendientes de la verificación

Cuando todos los nodos terminan correctamente, la verificación revisa el
objetivo completo. Si devuelve `needs_more` con una corrección estructurada
válida, Relay agrega un nodo con los criterios pendientes y continúa en la
misma ejecución autorizada. Los nodos terminados conservan su resultado y
no vuelven a ejecutarse. La corrección usa el mismo workspace, identidad y
controles de la ejecución original.

**Pausar** impide iniciar nuevos nodos, también si se pulsa mientras responde
el verificador. Los nodos en curso pueden terminar y la corrección queda
pendiente con su progreso guardado. Para ejecutarla después, usa **Continuar**
en la tarea y **Retomar** en el plan. Una tarea cancelada permanece terminal.

Cada nueva corrección debe acreditar un criterio pendiente anterior y no
repetir una lista de pendientes ya intentada. Si falta una corrección válida,
no hay progreso o se alcanza el límite global de 200 vueltas del scheduler,
se registra un nodo fallido con el motivo y se detiene la continuación. El
porcentaje de progreso cuenta solo tareas terminadas correctamente; un fallo
o un bloqueo no equivale a trabajo cumplido.

Las cancelaciones, preguntas pendientes, fallos de ejecución y veredictos
`off_plan` o `needs_human` conservan sus controles. Un error del verificador
o al guardar su respuesta tampoco inicia trabajo adicional. El alta del nodo
y su registro de progreso se guardan juntos; reiniciar el servidor no arranca
los grafos automáticamente ni borra el freno ante criterios repetidos.

Las pruebas de `mcp-server/tests/test_graph_verification_recovery.py` usan una
base temporal y respuestas simuladas. Comprueban el mecanismo de continuación;
no demuestran que un modelo real evalúe correctamente los criterios.

## Equipo, cuentas y escritura

El Admin asigna proyectos a Dev o Subadmin desde **Equipo**. Una asignación
permite editar, compilar y probar ese proyecto. La conexión personal de GitHub
se realiza en **Mi cuenta** y tiene su propio requisito de autorización.

Si una tarea nació en solo lectura, mantiene ese modo después de la asignación.
La persona pulsa **Habilitar escritura** para preparar la rama y el worktree,
y luego continúa explícitamente. Si falta GitHub, el mensaje señala **Mi cuenta**.
Otorgar el proyecto no ejecuta el plan ni autoriza publicación o integración.

Consulta [Equipo](TEAM_ACCESS.md), [cuentas personales](USER_ACCOUNTS.md) y
[tareas persistentes](PERSISTENT_TASKS.md) para los contratos y límites.
