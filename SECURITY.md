# Seguridad y uso local

FourBis Relay es un proyecto experimental para ejecución local. Por defecto escucha en `127.0.0.1`; no se presenta como un servicio público desplegado ni como una instalación multiusuario endurecida.

El proceso se ejecuta con los permisos del usuario que lo inicia. Sus herramientas pueden leer y modificar archivos, ejecutar comandos y consultar bases de datos dentro de las rutas autorizadas por la instalación. El acceso local sin cabeceras de identidad se trata como acceso de propietario. Ejecuta el relay solo sobre proyectos y datos que controles, y revisa las operaciones antes de autorizarlas.

No expongas el puerto directamente a Internet. Las credenciales de proveedores, las conversaciones y los adjuntos pertenecen a la instalación local; no los subas a Git ni los incluyas en issues públicos. Los modelos remotos reciben los mensajes y resultados de herramientas que se incorporen a sus solicitudes.

La integración opcional con controles externos de identidad requiere configuración adicional y no constituye una garantía de aislamiento para código o usuarios no confiables. La versión de portafolio no incluye una auditoría integral ni una promesa de seguridad para despliegues públicos.

El rol `member` recibe únicamente metadatos de los proyectos. La configuración
de herramientas y la vista del prompt efectivo requieren el rol `owner`.
Las URLs Git mostradas por la API omiten credenciales, parámetros y fragmentos;
el archivo local de configuración Git conserva su valor original.

Dev y Subadmin pueden recibir proyectos explícitamente en **Equipo**. Una
asignación habilita edición y ejecución de comandos de compilación/pruebas
con la cuenta del servicio: el worktree no aísla procesos. Asigna escritura
solo a desarrolladores de confianza. La asignación no habilita escritura SQL,
MCP externos ni publicación de PR. Las cuentas externas son personales y
se conectan desde **Mi cuenta**, sin recurrir al token de otra persona.

Las trazas y la captura de contenido están desactivadas por defecto. Activar las
trazas no habilita por sí solo el envío de prompts o respuestas: esa captura
requiere una opción adicional explícita. El envío a Logfire requiere un token
guardado en la configuración de Relay; un token ambiental no lo activa.

La API rechaza orígenes web ajenos y, para peticiones al socket local, los hosts
distintos de `localhost`, `127.0.0.1` o `[::1]` requieren un JWT de Cloudflare
Access verificado. Un proxy debe conservar el `Host` público; reescribirlo a
localhost eliminaría esa distinción. Estos controles no autentican otros procesos
que ya se ejecutan en tu equipo. El bind LAN es opcional y deja disponibles
health, handshake y listado de sesiones: conserva el bind local predeterminado
si no necesitas esa integración.

Si encuentras una vulnerabilidad, evita publicar secretos, datos privados o instrucciones de explotación. Contacta primero al mantenedor con una descripción mínima y reproducible.
