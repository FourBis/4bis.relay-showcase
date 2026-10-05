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

Después del alta, las APIs locales requieren la sesión y el rol correspondiente.
Un cliente HTTP que deba conservar la cuenta personal envía la cookie `relay-session`
recibida al iniciar sesión. El body `author` no establece la identidad. Si
`RELAY_API_KEY` está configurada, `X-Relay-Key` también debe coincidir; esa clave
solo habilita el acceso y no identifica a una persona.
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
| POST | `/experts/run` | Iniciar una ejecución |
| GET | `/experts/status/{chat_id}` | Consultar estado |
| POST | `/experts/cancel/{chat_id}` | Solicitar cancelación |
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
  "source": "ui",
  "author": "local"
}
```

Envía ese cuerpo a `POST /experts/run`. Una ejecución aceptada responde con
HTTP 202 y un `id`; consulta su estado hasta que termine. Para continuar una
conversación existente, añade `"conversation": "<id>"`. Los adjuntos se envían
como `"attachments": ["<id>"]` después de subirlos. `model` permite indicar
un modelo del catálogo.

Un 202 confirma aceptación, no que la ejecución haya finalizado correctamente.
Las respuestas de error incluyen un campo `error`. Revisa el estado final y
el resultado antes de dar una acción por completada.

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

El modo predeterminado es local. Las peticiones locales sin cabeceras de
Cloudflare Access se consideran del propietario. Si se usan cabeceras de
Access, el JWT debe ser verificable con el dominio y audiencia configurados;
el correo sin JWT no basta. Los permisos se comprueban en el servidor.

Consulta [SECURITY.md](../SECURITY.md) antes de configurar acceso remoto.
