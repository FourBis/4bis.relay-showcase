# Acceso a las conversaciones

Cada conversación conserva su creador. Tener acceso al proyecto no comparte
por sí solo todos sus hilos: las invitaciones son explícitas y se comprueban
con la identidad actual de la cuenta.

## Encontrar un hilo

En Chat, el selector de conversaciones ofrece **Mis conversaciones** y
**Compartidas conmigo**. El Admin puede consultar además la lista de gestión.
La lista personal sigue disponible desde **Mis conversaciones** al abrir un
hilo en su propia ventana del workspace.

Los filtros de proyecto y búsqueda acotan la lista visible. Una lista vacía
no concede acceso a conversaciones de otras personas; cada lectura y acción
vuelve a comprobar sus permisos en el servidor.

## Compartir y retirar acceso

Abre **Compartir** en el encabezado de un hilo cuando tu cuenta tenga permiso
para administrarlo. El diálogo consulta las personas elegibles y los accesos
actuales antes de habilitar **Guardar acceso**. Puedes buscar por nombre o
correo, seleccionar una persona y elegir:

| Acceso | Permite | Límites |
| --- | --- | --- |
| Solo lectura | Consultar el historial del hilo | No enviar mensajes ni controlar la tarea |
| Colaborar | Consultar y enviar mensajes con los permisos vigentes | No administrar ni volver a compartir la conversación |

La colaboración requiere que el proyecto y la conversación permitan escribir.
La invitación no concede acceso al proyecto, no cambia el rol del integrante
y no reduce las facultades que un Admin ya tenga. Las conversaciones de las
consultas MCP personales no se pueden compartir.

**Retirar acceso** revoca la invitación. Si se elimina la asignación necesaria
del proyecto o se archiva/deshabilita la cuenta de la persona invitada, el
servidor deja de admitir su acceso compartido. Una respuesta ya vista no puede
retirarse de la memoria de una persona.

Si un cambio de acceso no queda confirmado, el diálogo bloquea nuevas
mutaciones hasta consultar otra vez el estado actual. Conserva el contexto de
la conversación y muestra el error; no presenta el cambio como guardado.

## Consultar, colaborar y administrar

El creador conserva el vínculo con su historial. Las consultas propias pueden
estar permitidas sin una asignación de escritura; ejecutar cambios requiere
la autorización vigente del proyecto. Retirar esa autorización no convierte
un trabajo con escritura en una consulta ni vuelve a autorizarlo.

La UI usa los permisos que devuelve el servidor para mostrar el compositor,
Compartir y los controles de cierre, rama, diff y ejecución. El servidor sigue
siendo la autoridad aunque una ventana permanezca abierta con datos antiguos.

La lista de gestión del Admin no lo convierte en participante ni concede esa
capacidad a otro integrante. Los prompts y la configuración administrativa
del proyecto permanecen reservados al Admin.

## Contrato para integraciones

Las respuestas incluyen un objeto `access` con `can_read`, `can_send`,
`can_control`, `can_share`, relación y modo de invitación. No reconstruyas esos
permisos a partir de un proyecto seleccionado o de un correo almacenado.

Los accesos explícitos se consultan con
`GET /conversations/{id}/shares`, se guardan con
`PUT /conversations/{id}/shares/{email}` y cuerpo
`{"permission":"read"}` o `{"permission":"collaborate"}`, y se retiran con
`DELETE` sobre esa misma ruta. Las respuestas no se deben cachear como una
fuente permanente de autorización.

Consulta también [Equipo](TEAM_ACCESS.md), [Interfaz web](ADMIN_UI.md) y
[Recuperación de solicitudes](REQUEST_RECOVERY.md).
