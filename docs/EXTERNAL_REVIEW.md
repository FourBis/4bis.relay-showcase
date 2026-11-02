# Revisar una PR externa con Relay

Relay puede preparar una revisión informativa del código de una PR registrada,
conservar evidencia ligada a su commit y mostrar avisos dentro del workspace.
La revisión del modelo no sustituye a la persona que decide integrar el cambio.

La funcionalidad comienza desactivada: sin modelo seleccionado, sin publicación
GitHub y sin destinatarios. Abrir una tarea o consultar su estado no ejecuta al
revisor ni publica comentarios. Las cuentas, modelos y permisos se configuran
en cada instalación; el repositorio no incluye conexiones activas.

## Registrar el trabajo

En **Proyectos → PR**, abre el registro del proyecto y selecciona una PR abierta
o ingresa su URL. Los selectores consultan sólo el repositorio vinculado al
proyecto y tu conexión personal de GitHub. Puedes buscar por título o número,
recorrer páginas o introducir la URL manualmente si GitHub no responde.

Declara el objetivo, el riesgo y, cuando corresponda, el issue vinculado. Ambas
ramas de la PR deben pertenecer al repositorio del proyecto; los forks quedan
fuera de este recorrido. El registro conserva repositorio, número, responsable,
SHA de head, SHA de base, checks observados y revisión humana por separado.

**Actualizar GitHub** comprueba de nuevo la identidad y el estado. Una respuesta
antigua no reemplaza una observación más reciente. Registrar una PR no concede
acceso a otras conversaciones, herramientas ni repositorios del proyecto.

## Configurar la revisión

Un Admin activo configura **Revisión automática** del proyecto. Debe elegir un
modelo existente y habilitado antes de activar el recorrido. La configuración
se guarda en `external_pr_review_policy`; su estado inicial equivale a:

```json
{
  "enabled": false,
  "model": "",
  "publication_mode": "",
  "publisher_actor": "",
  "recipients": []
}
```

Activar la revisión permite observar las PR registradas abiertas de ese proyecto
y procesar los nuevos commits con el modelo seleccionado. Puede consumir el
servicio que hayas configurado. Cambiar el modelo o los permisos no autoriza a
repetir un intento fallido: usa **Reintentar revisión** después de revisar el
estado. Para detener nuevas revisiones, desactiva la opción del proyecto.

Los modos de publicación y destinatarios son decisiones separadas. Dejar
`publication_mode` vacío conserva el informe en Relay. Los valores `personal`
y `bot` requieren una cuenta conectada que acredite exactamente el tipo de
identidad elegido; no se cambia automáticamente a otra cuenta. Configurar
destinatarios crea avisos en sus bandejas de Relay, sin enviar correo ni mensajes
a otras aplicaciones. Sólo se admiten cuentas activas con acceso al proyecto.

## Entender el estado y el resultado

| Estado | Qué significa | Qué puedes hacer |
|---|---|---|
| Bloqueada | Falta configuración, permiso, modelo o contexto verificable. | Lee el motivo, corrige la causa y solicita un reintento. |
| En cola | El pedido quedó registrado para el contexto indicado. | Espera o consulta su estado. |
| En revisión | Se está leyendo el contexto y consultando al modelo configurado. | Mantén la referencia al SHA; el código de la PR no se ejecuta. |
| Con hallazgos / lista para revisión humana | Existe un resultado válido para ese head, base y contexto. | Contrasta sus hallazgos y límites antes de decidir. |
| Histórica | Cambió el commit, la base o el contexto de revisión. | Actualiza GitHub y solicita una revisión del contexto vigente. |
| Fallida o interrumpida | No se confirmó un resultado válido. | Reintenta explícitamente; no se repite el modelo por abrir la vista. |

El informe distingue checks leídos de GitHub y pruebas declaradas por una
persona. Relay no ejecuta esas pruebas durante esta revisión. Cada hallazgo
automático se vincula a una ruta y línea modificada verificada; no aprueba,
fusiona ni despliega la PR.

El contexto se obtiene de blobs verificados y de un diff completo acotado a 50
archivos. Las directrices del repositorio se leen del commit base autorizado,
nunca de instrucciones añadidas por la propia PR. Si hay material potencialmente
sensible, archivos no textuales, límites excedidos o evidencia incompleta, la
revisión se bloquea en vez de presentar un recorte como cobertura completa.

El objetivo, issue, evidencia y directrices quedan ligados al informe mediante
huellas de contenido. Los resultados y publicaciones de otro contexto se
conservan como historia. Una notificación no se convierte en un informe del
modelo ni en aprobación humana.

## Publicar comentarios y declarar evidencia

**Declarar evidencia** registra una explicación humana para un SHA y un destino
explícito: la PR o su issue vinculado. La intención de publicación se conserva
en una cola separada, condicionada al modo de publicación configurado. Revisa
el texto y su destino antes de confirmar la acción.

La publicación comprueba otra vez cuenta, permisos, repositorio, commit y
contexto. Una respuesta perdida se muestra como **sin confirmar**: Relay busca
el comentario existente y no repite automáticamente el POST. Un rechazo HTTP
definitivo admite un reintento explícito con nuevos controles. El informe del
modelo se publica como comentario informativo, nunca como aprobación de GitHub.

En este candidato local, la reserva del intento vuelve a comprobar permisos,
política completa, PR, contexto, informe y conexión de GitHub en una sola
transacción de escritura. Una revocación o desconexión confirmada antes de esa
reserva bloquea el envío. La reserva es el punto de autorización: un cambio
posterior no permite retirar una petición que ya haya sido admitida. Si se
interrumpe la espera después de confirmar la reserva, el intento se conserva
como sin confirmar y sólo admite reconciliación por lectura.

La sincronización del issue y GitHub Projects conserva su propia política y
su control de observaciones concurrentes. Consulta [ISSUE_SYNC.md](ISSUE_SYNC.md);
un comentario o imagen no autoriza cerrar un issue.

## Aportar una captura a la PR o al issue

La evidencia visual de este módulo usa una copia temporal PNG que debes revisar
antes de preparar. Admite hasta 5 MB, 4096 píxeles por lado y 4 megapíxeles, con
validación de estructura y contenido. No se envía la imagen al modelo.

1. Selecciona el destino y confirma que revisaste la captura.
2. Descarga la copia preparada desde Relay.
3. Sube esa imagen mediante la interfaz de GitHub y publica el comentario con el
   marcador que proporciona Relay. GitHub realiza esa subida en cuanto la haces.
4. Pega el enlace permanente del comentario en **Confirmar en GitHub**.

Relay comprueba autor, destino, marcador y huella de los bytes antes de marcar
la imagen como verificada. Sólo entonces puede limpiar su copia temporal propia.
Si no puede acreditar el adjunto, conserva el temporal para recuperación. No
borra assets de GitHub, no sube imágenes automáticamente y no usa la captura
como sustituto de pruebas o revisión humana.

## API y permisos

| Ruta | Operación |
|---|---|
| `GET /projects/{slug}/pulls` y `GET /projects/{slug}/issues?q=&page=` | Selectores paginados de lectura con conexión personal. |
| `GET /projects/{slug}/external-pr` | Registro local paginado; no consulta al modelo. |
| `POST /projects/{slug}/external-pr` | Registrar una PR con objetivo, riesgo y alcance. |
| `POST /conversations/{id}/external-pr` | Actualizar, declarar evidencia o reintentar revisión. |
| `GET/POST /conversations/{id}/external-pr/images` | Listar o preparar imágenes propias del registro. |
| `GET /conversations/{id}/external-pr/images/{draft_id}` | Descargar una copia propia verificada. |
| `POST /conversations/{id}/external-pr/images/{draft_id}/confirm` | Verificar el comentario y el adjunto publicados por la persona. |
| `GET /admin/api/external-review/notifications` | Leer tu bandeja de avisos autorizados. |
| `POST /admin/api/external-review/notifications/{notification_id}/read` | Marcar como leído un aviso propio. |

El registro de trabajo requiere permiso actual de escritura en el proyecto,
incluso para quien creó la tarea. Esa autorización sólo sirve a estas acciones;
no concede lectura del historial privado del chat. Las imágenes temporales se
restringen además a su autor. Las revocaciones se vuelven a comprobar antes de
exponer registros o actuar con servicios externos.

El observador recorre lotes de hasta 100 registros con rotación por identidad.
Un registro cuyo actor perdió permisos no bloquea los posteriores ni recibe
actividad o evidencia ficticia para moverlo de lugar. La posición del recorrido
vive en memoria del proceso y se reinicia con él; no concede permisos ni
garantiza continuidad del recorrido ante reinicios permanentes.

La documentación describe el contrato del código preparado localmente. En esta
tanda sólo se realizaron revisiones estáticas y comprobaciones de sintaxis/diff;
no se ejecutaron modelos, proveedores, publicaciones, aplicación, DB ni pruebas.
Consulta [VALIDATION.md](VALIDATION.md) para distinguir la evidencia de cada versión.
