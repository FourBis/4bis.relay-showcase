# Demo estática: alcance y recorrido

`site/` es una simulación con datos ficticios. No se conecta al servidor Relay,
GitHub, modelos, correo ni otros servicios. La política CSP conserva
`connect-src 'none'`; no hay permisos del navegador, notificaciones del sistema,
almacenamiento persistente ni envío de formularios. Todo estado vive en memoria
hasta recargar o pulsar **Reiniciar demo**. Usa únicamente texto ficticio.

La demo y el servidor instalable son artefactos distintos. Esta actualización
representa un **port local de paridad en revisión**: no acredita publicación en
GitHub, integración remota, disponibilidad de una instancia ni funcionamiento de
un proveedor. La versión instalada, sus permisos y su configuración determinan
qué puedes usar. Los escenarios usan `AuroraDemo`, `InventoryDemo`, actores con
etiqueta «demo» y una referencia en texto a `fakeexampleorg`; no identifican
repositorios, personas o cuentas verificadas.

## Manifiesto de capacidades

| Escenario | Qué se puede explorar aquí | Contrato del servidor instalable que representa | Límite de la simulación |
|---|---|---|---|
| Conversaciones personales y compartidas | Mías/Compartidas, vista de propietario/destinatario, acceso explícito de lectura o colaboración y retirada de acceso. | Identidad personal, ACL fresca, destinatario activo y asignado, proyecto habilitado. Colaborar no concede propiedad ni administración; enviar exige también los permisos técnicos actuales del proyecto y la tarea. | ACL simulada con dos actores y AuroraDemo. Colaboración exige habilitar primero el permiso de escritura ficticio; mientras sea consulta sólo admite compartir lectura. No es aislamiento real entre personas. |
| Envío incierto | Resultado confirmado, rechazo anterior a la admisión o respuesta perdida; borrador y número de intento conservados; comprobación del mismo intento. | Identidad inmutable del pedido, misma persona, consulta/reconciliación y evidencia del servidor antes de liberar un intento incierto. | La respuesta perdida usa un envío aceptado del guion. Comprobar no consulta una API ni repite un envío. No simula recuperación después de cerrar/recargar la página. |
| Equipo y selectores | Buscar proyectos sin borrar selecciones, asignar, archivar y restaurar una persona ficticia. | Administración y asignaciones explícitas; archivo conserva datos y asignaciones y retira acceso; restaurar no lo reactiva. Activar acceso es una acción separada. | Sólo una persona editable. Una conexión personal ficticia no es una credencial ni concede acceso a un repositorio. |
| Catálogo y roles | Modelo por planificador, ejecutor, verificador, documentador y compactador; modelo detallado deshabilitado/habilitado. | Catálogo habilitado y modelos efectivos por rol. Configurar o habilitar un modelo no prueba respuesta, capacidades ni coste. | Nombres ficticios y estado en memoria. No hay endpoints, proveedores ni precios. |
| PR externo y evidencia | Registro manual, declaración humana, checks y aprobación independiente ligados al SHA; otro head deja evidencia histórica. | PR del mismo repositorio y evidencia actual. El registro no descubre automáticamente PR externos. | PR/issue/SHA son sintéticos. Aprobar y fusionar son botones del guion, no operaciones de GitHub. La evaluación Relay no bloquea merges en GitHub. |
| Revisión asesora | Opt-in del propietario de la instalación por proyecto, revisor habilitado, cola, hallazgos ficticios, fallo y reintento explícito por SHA. | Revisión acotada de diff/blob, sin tools, vinculada al snapshot. Separa asesoría, declaración humana, checks y aprobación. | El resultado es un texto preparado. No revisa código ni ejecuta pruebas; no publica comentarios o aprobaciones. Un intento conserva su modelo aunque cambies la selección para próximos intentos. |
| Bandeja Relay | Destinatarios inicialmente vacíos, aviso ficticio por resultado asesor, SHA e indicador leído. | Destinatarios explícitos y acceso actual. Avisos dentro de Relay; una integración externa necesita su propio contrato/configuración. | No usa Notification API ni solicita permisos, correo, Slack, Discord o avisos del sistema. La vista separada del destinatario no concede a un admin acceso a bandejas ajenas. |
| Continuidad y grafo | Borradores privados por actor e hilo, sustitución por subtareas, continuación y PR ficticia. | Estado, dependencias, revisión y acciones explícitas según la versión instalada. | No reinicia un proceso, ejecuta comandos, modifica archivos ni conserva estado al recargar. |

Las superficies nuevas representan los módulos `conversation_access`,
`server_conversation_sharing`, `conversation_creation`, las rutas de
conversaciones, `external_review*`, sus rutas y módulos de interfaz, el catálogo
por roles y la administración de equipo. Su conciliación con la implementación
pública queda sometida a la revisión estática del port; esta página no sustituye
ese dictamen. Los recorridos de registro/evidencia que ya estaban en la demo se
mantienen separados de la revisión asesora nueva.

No se añadieron escenarios H3, TTS, generación/edición de imágenes o políticas de
proveedores. Las capturas existentes mantienen su procedencia ficticia; no se
crearon capturas de la instancia privada ni assets nuevos de imagen.

## Recorrido manual propuesto (no ejecutado en esta tarea)

1. En **Conversaciones**, escribe un pedido ficticio. Elige **Respuesta perdida**
   y envía. Comprueba que conserva texto e identificador sin mostrarlos al otro actor, bloquea otro envío y
   **Comprobar el mismo envío** añade el mensaje una sola vez. En **Rechazado**,
   el borrador queda editable y un envío nuevo requiere tu acción explícita.
2. En **Equipo**, edita a Dev demo, filtra proyectos y asigna AuroraDemo. El
   filtro no cambia selecciones ocultas. La conexión, permiso y continuación
   son pasos ficticios separados; habilitar permiso no ejecuta la tarea.
3. En **Conversaciones**, comparte el hilo con lectura y cambia a la vista de la
   persona destinataria: no puede responder ni administrar el acceso. Vuelve a
   propietario, habilita primero el permiso de escritura ficticio en Equipo y
   después concede colaboración. Mientras sea consulta sólo puedes compartir lectura. El destinatario puede responder, conserva su atribución y no puede
   renombrar ni administrar el hilo. Retirar acceso oculta el hilo al destinatario.
4. En **Modelos y bandeja**, elige un revisor habilitado y activa revisión
   asesora. La revisión empieza apagada y sus destinatarios vacíos. Deshabilitar
   un modelo conserva su selección histórica; no permite iniciar otro envío con
   un ejecutor deshabilitado ni completar una revisión con otro modelo por detrás.
5. Registra el PR ficticio. Solicita revisión, simula fallo y reintenta
   explícitamente; después muestra hallazgos. La asesoría no marca la aprobación
   humana ni los checks. Un head nuevo deja el resultado anterior histórico.
6. Configura el destinatario local ficticio y completa otra revisión: aparece
   un aviso en la bandeja. Archiva a Dev demo: pierde acceso y conserva sus
   asignaciones. Restaurar no reactiva el acceso: debes pulsar **Activar acceso
   ficticio** por separado. Esto no inicia ni continúa una tarea.
7. Cambia ES/EN y recorre tabs con flechas/Home/End. Revisa foco, etiquetas,
   paneles ocultos y acciones deshabilitadas. Prueba el diseño en pantalla estrecha.

Este recorrido es una guía de QA posterior, no evidencia de que se haya
realizado. Por instrucción del usuario sólo se comprueban sintaxis y diff:
**sin tests/suites, navegador, app, imports de producto ni proveedores**. La
presentación visual y las interacciones quedan pendientes de QA manual
autorizado; no se declara pass funcional ni CI verde.

## Mantenimiento

Actualiza este manifiesto junto con cambios de `site/` y con cada contrato que
la demo represente. Conserva los requisitos y los estados que bloquean una
acción; una respuesta preparada nunca sustituye evidencia real. La simulación
no convierte una función pendiente de integración en una capacidad disponible
en una instancia desplegada. Mantén licencias, assets públicos existentes y
CSP. Publicar la demo requiere la autorización delimitada correspondiente.
