# API HTTP

Base local predeterminada: `http://127.0.0.1:8413`.
La UI usa la misma API. Este documento resume los endpoints principales;
las rutas completas están registradas en `server.py` y `admin.py`.

## Alta inicial y sesión GitHub

En una base sin usuarios, la UI permite configurar una OAuth App propia desde
localhost y crear el primer Admin con identidad verificada. No modifica el
acceso de instalaciones existentes. Consulta [Cuentas personales](USER_ACCOUNTS.md).

| Método | Ruta | Contrato |
| --- | --- | --- |
| GET | `/admin/api/auth/status` | Estado del alta, sesión y URL de callback; nunca devuelve secretos |
| POST | `/admin/api/auth/setup` | JSON `client_id`, `client_secret`; solo alta local abierta; 409 si ya hay usuarios |
| POST | `/admin/api/auth/github/start` | JSON vacío; devuelve `authorization_url` y cookie OAuth de un solo flujo |
| GET | `/admin/api/account/github/callback` | Verifica state, PKCE e identidad; crea sesión y redirige al workspace |
| POST | `/admin/api/auth/logout` | JSON vacío; revoca la sesión y borra su cookie |

Después del alta, las solicitudes usan la identidad autenticada y los permisos
de esa cuenta. Un cliente HTTP conserva la cookie `relay-session` recibida al
iniciar sesión y la envía como `Cookie: relay-session=<sesión>` en cada llamada.
`author` solo atribuye el run; no establece identidad. Si `RELAY_API_KEY` está
configurada, también se exige `X-Relay-Key`; esa clave compartida habilita
acceso, pero no identifica a una persona.
El login no guarda tokens de herramientas; estas se conectan desde **Mi cuenta**.
Las solicitudes JSON verifican origen y el alta rechaza proxies. Un host público
sigue requiriendo Cloudflare Access; la sesión nativa no abre el servidor a Internet.

## Consulta y configuración

| Método | Ruta | Uso |
| --- | --- | --- |
| GET | `/health` | Comprobar que el proceso responde |
| GET | `/admin/api/projects` | Consultar proyectos |
| POST | `/admin/api/projects` | Crear un proyecto |
| GET | `/admin/api/projects/{slug}` | Consultar un proyecto |
| PATCH | `/admin/api/projects/{slug}` | Actualizar un proyecto |
| GET | `/admin/api/models` | Consultar catálogo de modelos |
| GET | `/admin/api/me` | Identidad y rol efectivos |
| GET | `/admin/api/config/timeouts` | Consultar tiempos límite |

La configuración de proveedores, claves y proyectos se puede completar desde
el workspace. No hace falta editar SQLite para el uso normal.
La ruta `GET /projects` de versiones antiguas ya no está registrada.

## Conversaciones y ejecuciones

| Método | Ruta | Uso |
| --- | --- | --- |
| POST | `/conversations` | Crear una conversación |
| GET | `/conversations` | Listar conversaciones |
| GET | `/conversations/{id}/messages` | Leer mensajes |
| POST | `/conversations/{id}/close` | Cerrar una conversación |
| POST | `/experts/run` | Iniciar o encolar una ejecución; 202 confirma aceptación, no resultado final |
| GET | `/experts/status/{chat_id}` | Consultar progreso vivo o estado de evento gestionado |
| POST | `/experts/cancel/{chat_id}` | Solicitar cancelación y esperar el cierre durable del worker |
| POST | `/graphs` | Crear un plan de tareas; `arrancar: false` permite preparar sin ejecutar |
| GET | `/graphs/{id}` | Consultar nodos, avance y estado del grafo |
| POST | `/graphs/{id}/resume` | Solicitar reanudación explícita del grafo |
| POST | `/graphs/{id}/cancel` | Detener el grafo; también admite grafos históricos sin conversación |
| GET | `/chats/{id}` | Consultar el registro de ejecución |
| GET | `/chats/{id}/md` | Consultar su exportación Markdown |
| POST | `/attachments` | Subir un archivo multipart, campo `file` |

Con el proyecto `demo` ya registrado y un modelo configurado:

```json
{
  "target": "demo",
  "user": "Explica la estructura del repositorio",
  "source": "api",
  "author": "agente-demo",
  "conversation": "ID_DE_CONVERSACION",
  "request_id": "agent-issue-123"
}
```

Envía ese cuerpo a `POST /experts/run`. Una ejecución aceptada responde con
HTTP 202 y un `id`. `conversation` es opcional; si se envía, debe ser el ID de
una conversación existente del mismo proyecto. `conversation_id` se acepta como
alias; si ambos llegan con valores distintos, el servidor responde `400` sin
iniciar trabajo. En tareas gestionadas, el servidor comprueba el permiso de
control antes de modificar la conversación: quien creó una consulta de solo
lectura puede continuarla y quien tiene permiso de escritura puede continuar
tareas del proyecto. Una tarea detenida responde `409`. Para atribuir la
solicitud a una persona, usa la cookie de sesión; `author` no la sustituye.
Los adjuntos se envían como `"attachments": ["<id>"]` después de subirlos.
`model` permite indicar un modelo del catálogo.

Usa un `request_id` estable de 1 a 128 caracteres para reconocer reintentos.
La deduplicación solo aplica a ejecuciones gestionadas con conversación y a la
creación Git implícita; no vuelve idempotente cualquier POST. Reutiliza el mismo
ID y payload para reintentar; cambiar el payload con el mismo ID devuelve `409`.
Usa un ID nuevo, como un UUID, para cada pedido nuevo.

Un 202 confirma aceptación, no que la ejecución haya finalizado correctamente.
Conserva el `conversation_id` devuelto y consulta
`/experts/status/{chat_id}` mientras el relay conserva el progreso en memoria.
Ese identificador se garantiza al crear o pasar una conversación; un run directo
sin conversación puede no devolverlo.
Si ese estado volátil devuelve `404`, `/chats/{id}` solo aporta metadatos y
estado; recupera el contenido mediante
`GET /conversations/{conversation_id}/messages`. `/chats/{id}/md` puede devolver
`404` mientras `md_path` esté vacío y la exportación Markdown siga pendiente;
no relances el pedido solo por ese `404`.

La respuesta general `truncated` indica que se alcanzó el límite de turnos;
`messages[].truncated` indica que se recortó ese turno. Puedes ajustar
`content_cap` (predeterminado 4000, máximo 100000 caracteres por turno) y
`max_turns` (predeterminado 200, máximo 2000) en la consulta. Los eventos
gestionados conservan su estado en la base de datos. Las respuestas de error
incluyen un campo `error`; revisa el estado y el resultado antes de dar una
acción por completada.

La cancelación espera el cierre del worker y su persistencia. Un `409` no
confirma cancelación: revisa el estado y el resultado antes de reintentar. Si el
ID abreviado es ambiguo, usa el completo.

Cuando `ask_human` guarda una pregunta, el experto termina con
`phase_at_end: "question"`: no consulta otra vez al modelo ni ejecuta las
herramientas posteriores de esa tanda. El historial conserva sus resultados
y marca las llamadas que no se ejecutaron. Una tarea gestionada queda
`blocked`, sin marcarse implementada ni publicarse. Para continuar una tarea
sin grafo, responde la pregunta, usa el control `continue` de la tarea y
envía el `resume_prompt` recibido como siguiente turno; responder por sí solo
no reactiva una tarea bloqueada. Las preguntas vinculadas a nodos mantienen
su flujo de respuesta y reanudación del grafo.

Reanudar un grafo sin nodos listos o interrumpidos responde 409 y conserva
su estado y resultados. El error distingue un plan terminado, una decisión
humana pendiente y un fallo que necesita corrección; no inicia un worker vacío.

Cancelar un grafo existente responde 200 con `estado: cancelado`, incluso si
nadie lo ejecuta tras un reinicio o se repite la petición. No borra sus nodos
ni resultados. Un ID inexistente responde 404. En grafos históricos sin
conversación, Admin puede detenerlos aunque el proyecto ya no exista; Dev y
Subadmin requieren permiso de escritura en el proyecto. Las tareas vinculadas
conservan sus controles de permisos. Cancelar el grafo y cancelar la tarea
persistente de conversación son acciones distintas.

Las cancelaciones simultáneas comparten la limpieza del worker: un reintento
no vuelve a interrumpirlo, y la interrupción de la petición que lo espera
no cancela otra vez esa limpieza.

## Acceso

En instalaciones anteriores sin login nativo habilitado, las peticiones locales
sin cabeceras de Cloudflare Access se consideran del propietario. Con el login
nativo habilitado, la solicitud necesita una sesión válida y no usa ese fallback
de propietario. En hosts públicos, el JWT de Cloudflare Access debe verificarse
con el dominio y la audiencia configurados; el correo sin JWT no basta. Los
permisos se comprueban en el servidor.

Consulta [SECURITY.md](../SECURITY.md) antes de configurar acceso remoto.
