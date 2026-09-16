# API HTTP

Base local predeterminada: `http://127.0.0.1:8413`.
La UI usa la misma API. Este documento resume los endpoints principales;
las rutas completas están registradas en `server.py` y `admin.py`.

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

## Acceso

El modo predeterminado es local. Las peticiones locales sin cabeceras de
Cloudflare Access se consideran del propietario. Si se usan cabeceras de
Access, el JWT debe ser verificable con el dominio y audiencia configurados;
el correo sin JWT no basta. Los permisos se comprueban en el servidor.

Consulta [SECURITY.md](../SECURITY.md) antes de configurar acceso remoto.
