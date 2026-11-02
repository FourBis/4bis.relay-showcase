# Consultas personales mediante MCP

Relay puede ofrecer una interfaz MCP local para consultar repositorios ya
registrados. Está deshabilitada por defecto. No incluye un dominio externo,
cuenta compartida, credenciales ni proyectos habilitados de ejemplo.

El administrador debe habilitar **Consultas MCP personales** en Config y
**Permitir consultas MCP personales de solo lectura** en cada proyecto que
corresponda. Los dos valores parten deshabilitados. La opción global requiere
un nuevo arranque para registrar la ruta; este documento no ejecuta ese paso.
Deshabilitarla vuelve a bloquear las solicitudes y la admisión de trabajo aunque
la ruta ya estuviera registrada.

La configuración no concede acceso a un proyecto. Se requiere una cuenta
personal activa con el rol técnico y la asignación vigentes. Archivar una cuenta,
revocar su acceso, deshabilitar el proyecto o retirar alguna de las opciones
impide admitir consultas nuevas. El actor local genérico `owner` y una clave
de API sin identidad personal no sustituyen una sesión personal.

## Interfaz

La ruta es `POST /mcp/relay` en la instalación que administres. La ruta permanece
ausente si la opción global estaba deshabilitada al crear la aplicación.
Acepta un mensaje JSON-RPC por solicitud, con cuerpo JSON de hasta 128 KiB;
no abre sesiones de streaming ni SSE.

Admite `initialize`, `ping`, `tools/list` y `tools/call`. Las notificaciones
JSON-RPC se acusan sin despachar herramientas ni crear encargos. Versiones
de protocolo admitidas: `2025-03-26`, `2025-06-18` y `2025-11-25`.

| Herramienta | Uso |
|---|---|
| `relay_list_projects` | Lista nombres y slugs de proyectos explícitamente habilitados y autorizados para tu cuenta. No devuelve rutas ni configuración. `offset` y `limit` permiten paginar. |
| `relay_start_consultation` | Recibe `project`, `prompt` y un `request_id` estable. Crea un encargo local durable de consulta y devuelve `chat_id` y `conversation_id`. |
| `relay_get_result` | Recibe el UUID completo del encargo propio y devuelve su estado y el texto disponible. `offset`, `limit` y `next_offset` permiten continuar su lectura sin reiniciar trabajo. |

Reutiliza el mismo `request_id` y los mismos argumentos ante una respuesta
incierta. Cambiar proyecto o consulta con una identidad ya utilizada se rechaza;
consultar un resultado no vuelve a ejecutar el modelo. Un resultado sólo se
entrega a su actor personal original, con acceso actual al proyecto y la
conversación. Las respuestas llevan `Cache-Control: no-store`.

## Límites del encargo

Una consulta mantiene su repositorio en solo lectura, sin publicación,
seguimiento automático, shell nativa ni SQL. No hereda un sandbox abierto,
raíces adicionales o vistas de adjuntos de otros encargos. La política pública
de herramientas también conserva su exclusión de catálogos MCP externos para
ejecuciones de solo lectura. Esta interfaz no habilita generación de audio/vídeo,
correo, notificaciones ni proveedores nuevos.

Las consultas de esta interfaz no adjuntan `read_skill` ni descubren directorios
globales de skills. Tampoco incluyen índice, contenido embebido, resumen compacto
o fallback de skills en las instrucciones. Sólo pueden leer un archivo de su
proyecto mediante `read_file` si los permisos y exclusiones vigentes lo permiten;
ese lector no convierte un directorio global en una raíz autorizada.

Iniciar una consulta sí crea registros locales y puede usar el modelo que el
administrador haya configurado previamente para el proyecto, con sus límites
y consumo habituales. Activar la opción no inicia consultas por sí solo.
Las herramientas describen esa diferencia en sus anotaciones MCP.

La autorización se revisa de nuevo al admitir el trabajo y dentro de la
transacción que verifica el alcance del proyecto. Las opciones no modifican
las asignaciones del equipo ni convierten una lectura compartida en permiso
para ejecutar o modificar archivos.

## Identidad y operación

Puedes usar la sesión personal de la instalación. Si el operador configura
Cloudflare Access, dominio y audiencia se proporcionan en su propia instalación;
no hay valores predefinidos del autor. `CF_ACCESS_MCP_AUD` es una audiencia
opcional adicional para esta ruta y queda vacía por defecto. La interfaz no
crea un túnel ni publica el servidor.

El registro de la ruta corresponde a la composición del servidor; las
comprobaciones de flag y alcance siguen presentes en el handler y la admisión.
Para decidir si registra la ruta, el arranque lee el flag de la base existente
con una conexión nueva de solo lectura; una base ausente, una tabla ausente o
un error dejan la interfaz deshabilitada. Esa consulta no crea la instancia ni
confía en el valor de un caché anterior.
La clave de configuración global es `RELAY_PRIVATE_MCP_ENABLED` y el booleano
por proyecto es `defaults_json.mcp_consultations`. Sólo el valor booleano
`true` habilita el alcance del proyecto; strings como `"true"` no lo habilitan.

Esta entrega se revisó estáticamente. No se ejecutaron consultas, modelos,
proveedores, bases de datos, navegador ni pruebas para activarla o demostrar
una conexión real. El aislamiento de archivos es un control de Relay sobre el
repositorio autorizado, no una separación de procesos del sistema operativo.
