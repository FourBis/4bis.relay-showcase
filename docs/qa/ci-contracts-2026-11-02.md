# Contratos de creación, cola y conocimiento

Las comprobaciones de CI deben distinguir consultas independientes, reintentos
del mismo pedido y tareas que todavía ocupan un checkout histórico.

## Creación e identidad del pedido

- Un proyecto que no contiene Git admite consultas independientes, incluso si
  dos slugs apuntan al mismo directorio. Quedan en modo de sólo lectura, sin
  rama ni worktree y sin permiso de publicación.
- La misma combinación de proyecto y `request_id`, con el mismo cuerpo, crea
  una sola conversación. La primera respuesta es 201 y su replay es 200; ambos
  conservan el identificador y el recibo. Reutilizar ese ID con otro cuerpo
  devuelve 409 y no inserta otra conversación.
- Una conversación histórica que usa el checkout Git original conserva la
  exclusión por directorio. Un alias del proyecto tampoco puede abrir otra
  tarea sobre ese checkout ocupado. Las pruebas de esa frontera preparan un
  repositorio Git temporal real y una conversación histórica sin worktree.

## Cola de turnos

Una tarea gestionada admite nuevos pedidos con 202 mientras ejecuta un turno:
los conserva en FIFO y ejecuta uno a la vez. Repetir el mismo `request_id` y
cuerpo devuelve el mismo chat; cambiar su cuerpo devuelve 409. El ensayo de
integridad comprueba el estado `processing` del primero y `pending` del segundo,
la ausencia de ejecución simultánea y el avance a `applied` al terminar.

Cerrar la vista de una conversación gestionada conserva su tarea y sus pedidos;
no cancela la ejecución ni borra la cola. Para detenerla se usan los controles de
la tarea. Esto no modifica la autorización de cuenta o proyecto de cada turno.

## Fuentes de conocimiento y preguntas nocturnas

Las fixtures de facts y memorias crean conversaciones de origen reales y fijan
su creador y el actor de la escritura. Los endpoints de esas pruebas siguen
usando el adaptador local existente; la ausencia de actor u origen no concede
acceso en el código de producto. Se conserva la comprobación de aislamiento
entre proyectos.

La prueba de autoría de preguntas nocturnas consulta una cuenta vigente en la
base temporal de la propia prueba. Un actor distinto o ausente recibe la misma
respuesta 404 que una pregunta no disponible, antes de leer el cuerpo o guardar
la respuesta. La persistencia simulada exige el actor exacto de la solicitud.

## Organización y evidencia

`db_schema_conversations.py` contiene el fragmento de conversaciones, sus eventos
y barreras de creación. `db_schema.py` lo compone en la misma posición. El SQL
resultante y el esquema FTS conservan exactamente su contenido; los ALTER y su
orden permanecen en `db_schema_init.py`.

`server_conversation_history.py` contiene la proyección de historial, la
redacción, sus límites y la autorización final. Los nombres existentes se
reexportan desde `server_conversation_routes.py`. La extracción conserva el AST
de todas las funciones originales.

La preparación local se verificó sólo con AST de Python 3.11, igualdad literal
del SQL, revisión estática y `git diff --check`. No se ejecutaron suites, imports
del producto, aplicación ni base de datos en esta revisión. El siguiente CI debe
confirmar el comportamiento en ejecución.
