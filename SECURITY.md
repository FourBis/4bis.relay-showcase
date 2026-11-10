# Seguridad y uso local

FourBis Relay es un proyecto experimental para ejecución local. Por defecto escucha en `127.0.0.1`; no se presenta como un servicio público desplegado ni como una instalación multiusuario endurecida.

El proceso se ejecuta con los permisos del usuario que lo inicia. Sus herramientas pueden leer y modificar archivos, ejecutar comandos y consultar bases de datos dentro de las rutas autorizadas por la instalación. Las instalaciones anteriores mantienen el acceso local de propietario. Cuando se crea el primer Admin con GitHub, las peticiones locales requieren una sesión válida y los permisos de ese usuario. Ejecuta el relay solo sobre proyectos y datos que controles, y revisa las operaciones antes de autorizarlas.

El alta inicial con GitHub solo está disponible sin usuarios y desde loopback,
sin cabeceras de proxy. Usa state de un solo uso, PKCE y cookie ligada al navegador;
crea el Admin, su identidad y su sesión en una transacción. Los secretos de la
OAuth App quedan en el archivo local `.oauth.json` junto a la base. Este login
local no sustituye Cloudflare Access para exponer una instalación de forma remota.
Consulta [Cuentas personales](docs/USER_ACCOUNTS.md) para los límites del flujo.

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

## Protecciones del candidato de mantenimiento

Los adjuntos HTML, SVG, PDF y formatos desconocidos se descargan como bytes;
sólo las imágenes pasivas admitidas pueden mostrarse inline. Se sirven con
`nosniff`, política CSP de documento aislado y caché privada desactivada. Esto
no valida el contenido ni cambia los permisos de acceso existentes.

Las salidas de consola se redactan al preparar sus copias para historial y
pantallas; los handlers de estado también sanean sus copias. Los comandos ejecutables y los archivos originales se
conservan. La redacción por patrones no identifica secretos arbitrarios ni
limpia retroactivamente los logs en disco; tampoco concede permiso de lectura.

El movimiento de directorios revisa descendientes protegidos. Los escáneres
de repositorios y skills omiten enlaces; las búsquedas de archivos revalidan
cada ruta y acotan entradas y bytes. Estos controles y los worktrees
no aíslan otros procesos con los mismos permisos del sistema operativo.

El [recibo del candidato](docs/qa/maintenance-2026-11-01.md) conserva límites
pendientes y distingue revisión estática de validación operativa.

Los resultados MCP pasan por redacción antes de incorporarse al historial o
aplicar sus límites de presentación. Los diagnósticos que el SDK convierte en
reintentos (`ModelRetry`) también se sanean antes de llegar al modelo, sin
encadenar la excepción original. Las lecturas de logs de fondo usan una
copia contrastada con procedencia local; una lectura mixta sensible que no
puede atribuirse se retiene con un error explícito para solicitar lecturas
individuales. No se transforma una fuente ordinaria censurada en archivo apto
para editar. Estos controles no autentican productores desconocidos ni
identifican todos los secretos posibles.

## Fronteras del candidato de paridad

Las conversaciones usan invitaciones explícitas y permisos actuales. Los listados,
proyecciones de tarea, memoria e historial no deben convertir la asignación de
un proyecto en lectura de todos sus hilos. Las lecturas compuestas usan una
instantánea coherente de la cuenta y del acceso; las acciones revalidan su
admisión. Compartir no concede escritura ni permiso para publicar.

La revisión automática de PR externos parte deshabilitada. Antes de reservar
durablemente un intento de publicación, se vuelven a comprobar cuenta, conexión
personal, proyecto, política, commit y efecto esperado en una transacción. Una
revocación posterior no puede retirar un POST ya admitido. Una respuesta incierta
no autoriza repetirlo a ciegas. Los borradores PNG requieren consentimiento para la copia temporal. La persona
publica manualmente la imagen en GitHub y Relay verifica después el comentario;
este recorrido no genera imágenes ni las sube automáticamente.

Las consultas MCP personales están apagadas globalmente y por proyecto. Sólo
admiten su actor personal, encargo y repositorio autorizados. No adjuntan
`read_skill`, skills globales ni inyección automática de skills; no heredan
raíces adicionales, shell nativa, SQL o catálogos MCP externos de escritura.
Los archivos del proyecto siguen sujetos al lector y filtros permitidos.
Su aislamiento es un control de aplicación, no de procesos del host.

El candidato conserva los vendors públicos de Mermaid estricto y DOMPurify,
el whitelist de metadatos de miembros, los controles de loopback, las URLs Git
saneadas y los presupuestos finitos. La identidad opcional del runtime empieza
apagada y sólo la ve un Admin actual; no confirma automáticamente otro PID ni
atestigua criptográficamente la máquina.

La demo ampliada usa datos sintéticos, no almacena cuentas y no hace solicitudes
de red. Las funciones multimedia posteriores y sus cambios de UI quedan fuera.
El [recibo](docs/qa/parity-2026-11-01.md) declara la revisión estática y los límites
de ejecución, CI y confidencialidad; no certifica una instancia desplegada.
