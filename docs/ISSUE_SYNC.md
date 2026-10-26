# Issue común y sincronización con GitHub

[English](ISSUE_SYNC.en.md) · [PR externos](EXTERNAL_PRS.md)

**Alcance revisado el 26 de octubre de 2026:** estas funciones están implementadas en el código público e integradas en `develop` mediante el [PR #38](https://github.com/FourBis/4bis.relay-showcase/pull/38), del 20 de octubre, con una corrección de observaciones concurrentes en el [PR #39](https://github.com/FourBis/4bis.relay-showcase/pull/39). La demo y las pruebas documentadas usan datos sintéticos y GitHub simulado. La disponibilidad depende de la versión instalada; esta documentación no acredita un despliegue conectado. Consulta la [evidencia y los límites de esta revisión](qa/showcase-verified-status-2026-10-26.md).

Una conversación de Relay y un PR externo pueden compartir una issue del repositorio autorizado. El vínculo conserva la identidad canónica verificada: repositorio e ID, número, node ID, URL, título y estado.

## Vincular el trabajo

Al crear una conversación o registrar un PR externo, elige una issue de la lista o introduce su número o URL canónica de GitHub. El servidor verifica repositorio, identidad y acceso antes de guardar. Si falla la lista, conserva lo escrito y ofrece reintento o entrada directa.

En una tarea existente usa **Vincular issue** o **Editar vínculo / resolución**. **Este trabajo resuelve la issue completa** empieza desmarcado; marcarlo registra una declaración humana atribuida. Cambiar la referencia vuelve a desmarcarlo.

Un avance parcial mantiene la issue abierta. Para cerrar se exige declaración humana de resolución completa y merge verificado de todos los PR y tareas vinculados. Cerrar un PR sin merge, terminar un run o un texto del modelo no equivale a resolver la issue. Los vínculos históricos sin identidad comprobable bloquean el cierre hasta verificarse.

Una conversación antigua sin tarea persistente debe importarse antes como tarea; no se convierte silenciosamente al vincular una issue.

## Projects se configura explícitamente

La vista **Gestión** sigue siendo de consulta. La sincronización de una tarea es otra capacidad: Admin revisa una política por proyecto en `projects.defaults_json.issue_sync_policy`. Comienza deshabilitada. Vincular una issue no activa movimientos.

La [API](API.md) ofrece discovery de Projects/campos/opciones y guardado de política. Usa los IDs exactos verificados; no se infieren columnas por su nombre. Solo se admiten campos single-select existentes de Projects V2. La issue debe estar agregada previamente al Project: Relay no crea ese item automáticamente.

| Transición verificada | Issue | Projects, solo con política habilitada |
| --- | --- | --- |
| Trabajo iniciado o PR Draft | Se conserva abierta. | Opción configurada para `started`. |
| PR abierto fuera de Draft | Estar en revisión no acredita aprobación ni resolución. | `review`. |
| Cambios solicitados por revisor independiente para el head actual | Se conserva abierta. | `changes_requested`. |
| Merge | Cierre sujeto a declaración completa y todos los vínculos verificados. | `merged`, según la política. |
| Cancelación o PR cerrado sin merge | No resuelve la issue. | `cancelled`. |

El sincronizador no reabre una issue cerrada manualmente. Los textos nuevos de PR usan referencias neutrales. Las closing keywords presentes en PR externos, commits históricos u otras automatizaciones de GitHub pueden seguir actuando por separado.

## Leer, sincronizar y reintentar

Abrir el panel consulta el estado registrado. **Actualizar sincronización** reevalúa hechos remotos y, con política habilitada, puede aplicar el mapeo autorizado. El estado de sincronización se presenta aparte de la evidencia y aprobación del código.

El monitor consulta tareas elegibles cada 60 segundos; no es un webhook. Los fallos de lectura permiten hasta tres intentos con backoff de 60 segundos. **Revisar y reintentar** requiere revisar y confirmar el estado remoto: puede replantear el mapeo tras un conflicto manual, conserva los límites de escritura y no descarta incertidumbre.

Un timeout o interrupción tras intentar escribir deja un resultado incierto. Antes de repetirlo se relee el destino y se verifican precondición y permisos. Hay como máximo tres intentos de escritura por evento. Una política desactivada permite reconciliar resultados anteriores mediante lectura; no inicia efectos nuevos. Mientras exista un efecto en curso o incierto, no se alteran los vínculos que determinan el cierre.

Los cambios manuales incompatibles no se sobrescriben automáticamente. Un operador autorizado puede reconciliar la incertidumbre de otra persona; se conservan actor original y reconciliador por separado.

La inspección de Projects tiene límites de 100 elementos por conexión consultada; una página adicional o datos incompletos detienen la operación para revisión manual. El lector de PR admite hasta 99 revisiones y hay un máximo de 100 vínculos por issue. Llegar al límite no demuestra ausencia de dependencias.

## Demostración y configuración real

La demo pública simula una issue compartida, evidencia por commit y una política inicialmente apagada. Sus IDs y personas son ficticios y no envía solicitudes remotas. Instalar el servidor tampoco configura una política productiva, credenciales, apps, webhooks, reglas ni tableros.

Las pruebas usan GitHub simulado y bases temporales. Revisa la [seguridad](../SECURITY.md) y las cuentas personales antes de habilitar una política en tu instalación.
