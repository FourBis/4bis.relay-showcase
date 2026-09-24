# Validación de la versión de portafolio

Revisiones locales del 16, 17, 22 y 24 de septiembre de 2026. Relay se presenta como proyecto
experimental para uso local, con licencia [MIT](../LICENSE). Esta revisión reduce
riesgos concretos de publicación; no certifica ausencia de vulnerabilidades ni
preparación para un servicio público multiusuario.

## Publicación y privacidad

- Copia independiente con historial nuevo, sin heredar el repositorio privado.
- Copia alojada en el repositorio público `FourBis/4bis.relay-showcase`,
  con `develop` como rama predeterminada.
- Configuraciones locales, respaldos, bases de datos, conversaciones, cachés,
  claves y apuntes internos excluidos. Ejemplos y captura usan datos ficticios.
- Atribución pública intencional: FourBis y Jeremías Badilla. Los commits usan
  una dirección `users.noreply.github.com`.
- Gitleaks 8.30.1 revisa tanto el árbol exportado como todo el historial público.
  Las únicas excepciones son tres fixtures sintéticos de pruebas, en sus líneas
  exactas; no se excluyen carpetas de tests ni los vendors del escaneo.

La publicación debe salir de esta copia independiente. No traslades el historial
privado ni publiques una rama de preparación que todavía lo conserve.

## Correcciones de esta revisión

- Licencia MIT completa y metadatos SPDX; documentación coherente con esa elección.
- Avisos completos de 82 componentes npm, incluyendo dependencias anidadas del
  parser Mermaid y Tailwind. URLs, integridad y licencias en
  [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
- Mermaid 11.17.2 recompilado con DOMPurify 3.4.15, js-yaml 4.3.2 y lodash-es
  4.18.1. Se conservaron el parche de dependencias/lockfile y la procedencia.
  Render de diagramas en modo `strict`.
- Protección compartida de Host/Origin contra DNS rebinding y peticiones web
  cruzadas. Los hosts públicos requieren identidad Access verificada; abrir la
  pantalla principal mediante un enlace sigue permitido.
- La comprobación del peer local cubre cualquier bind e IPv4/IPv6. El acceso
  local sigue siendo de propietario, con las limitaciones de [SECURITY.md](../SECURITY.md).
- Restablecer una configuración numérica vacía vuelve al default del esquema.
  Corregido el HTTP 500 al resetear el timeout; se rechazan cuerpos inválidos y
  números no finitos sin modificar el valor anterior.
- Fixtures anteriores adaptados a la configuración runtime/SQLite. No se
  reintrodujo lectura de secretos desde variables de entorno para satisfacerlos.
- El listado y detalle de proyectos entregan solo metadatos al rol `member`.
  La configuración MCP, sus variables, rutas y defaults quedan reservados al
  propietario; la vista del prompt efectivo también requiere `owner`.
- Las URLs Git de la API omiten userinfo, query y fragmento, conservando el
  archivo Git original. Se comprobaron también los enlaces HTTPS/SSH del chat.
- Logfire recibe una decisión explícita de exportación y el token del panel.
  Un token del entorno no activa el envío al cloud. La captura de contenido
  requiere opt-in para cualquier destino; las pruebas unitarias no exportan.

## Anonimización para la publicación

Los nombres de empresas, proyectos internos, usuarios de ejemplo y referencias
personales detectados se sustituyeron por entidades ficticias, como AuroraDemo,
InventoryDemo, CommerceDemo y WorkshopDemo. Incluye placeholders, prompts,
respuestas simuladas de Gmail/Calendar, rutas, comentarios y fixtures de pruebas.
La captura del README también se revisó: contiene únicamente datos de muestra.
La atribución de FourBis/Jeremías, los proveedores técnicos y los avisos legales
conservan sus nombres correctos.

La copia pública se consolida en un historial inicial nuevo tras esta limpieza;
los commits anteriores con ejemplos internos no forman parte de sus ramas.
Los defaults de ejemplo ahora usan `#equipo-demo`, `RelayDemoBot` y
`<repos_root>/crm`; las configuraciones explícitas siguen teniendo prioridad.

Se revisaron nuevamente ejemplos, captura, defaults, configuraciones y avisos
de terceros con subagentes independientes. Las pruebas de Gmail usan el
remitente ficticio actual y fuerzan el modo simulado para evitar datos reales.

## Instalación y dependencias

Instalación editable con extras de desarrollo en un venv nuevo, Python 3.12 y
Windows: completada. `pip check` sin conflictos, metadatos MIT generados.
La guía actualiza pip antes de instalar dependencias.

Consulta de avisos conocidos al 16 de septiembre de 2026:

- 101 paquetes Python de la instalación nueva, incluido pip: sin avisos activos
  devueltos por la API de PyPI. El paquete local Relay se revisa como código.
- 82 coordenadas npm (81 componentes JavaScript más Tailwind CSS): sin avisos
  devueltos por OSV. Inventario obtenido de 33 sourcemaps anidados y dos metafiles
  de esbuild, además de la procedencia del CSS de Tailwind.
- El workspace de compilación upstream contiene otros paquetes/herramientas con
  avisos. Ese workspace y sus `node_modules` no se distribuyen con Relay.

Estos resultados describen las versiones resueltas en esa fecha; los rangos de
instalación y las bases de avisos pueden cambiar.

## Pruebas y límites

Las correcciones de esta revisión se validaron con grupos de **171 passed,
1 skipped**, **69 passed** y **57 passed**. Se preservaron las assertions de
las pruebas: configuración operativa en SQLite/runtime, modelos simulados y
servidores HTTP locales para evitar proveedores reales.

Los tres recorridos reales de navegador volvieron a pasar con los vendors definitivos
en Chrome (**3 passed**, 27,65 segundos):
workspace y objetos flotantes; estados del chat y adjuntos; render Mermaid de
flujo, secuencia, clases y frontmatter, más sanitización de HTML malicioso.
También pasaron JavaScript, sintaxis y los sellos del CSS recompilado.

### Revisión de la UI del workspace — 17 de septiembre de 2026

Se recorrieron en navegador los 20 módulos del workspace con pruebas aisladas y
datos ficticios locales, sin proveedores reales. El recorrido inicial registró
**66 passed y 1 fallo**: el fallo corresponde a un selector obsoleto del test;
los indicadores secundarios ahora están en un desplegable y la navegación se
abre desde el catálogo. Una copia de trabajo adaptada a esa interfaz conservó
las assertions y completó el flujo de workflow con **1
passed en 7,78 segundos**. El archivo público se corrigió después en la revisión
de distribución del chat y el grafo descrita a continuación.

Otro grupo aislado registró **94 passed y 6 subtests**. Se comprobaron edición
y persistencia de un proyecto ficticio, tablas desprendibles y filtro de filas,
mosaico, métricas y chat móvil a 390 px sin desbordamiento horizontal.

Se corrigieron el solapamiento entre chat y grafo y el botón de ensanchar el
grafo. Ambos paneles comparten el espacio; al ampliar el plan se conservan al
menos 320 px para la conversación. En ventanas de hasta 700 px se muestra uno
a la vez, y cerrar el grafo devuelve el chat. Las regresiones de JavaScript,
sintaxis, CSS, chat móvil y workflow registraron **78 passed en 18,30 segundos**.
El recorrido de workflow comprueba geometría normal, ampliada y restaurada,
ventana estrecha, móvil a 390 px, cierre/reapertura con devolución de foco,
ausencia de desbordamiento
horizontal y de errores JavaScript o peticiones externas del navegador.

Quedan dos detalles menores observados en la revisión inicial: “Cerrados
recientemente” ordena las ventanas por apertura, no por cierre; aparece un
aviso de consola al no existir el botón de proyectos ocultos. No forman parte
de la corrección de distribución del grafo.

Las capturas del README se generaron desde la aplicación con proyectos,
conversaciones, estados y consumo sintéticos en una base temporal. La captura
del grafo se actualizó después de la corrección visual. La captura automatizada
comprobó ausencia de errores JavaScript y bloqueó toda solicitud del navegador
fuera del servidor local (**1 passed**, 9,08 segundos).

La primera corrida completa del clon limpio produjo **2224 passed, 26 failed,
14 skipped, 18 subtests passed**. Identificó fixtures anteriores a la configuración
persistida, un remitente ficticio desactualizado y pruebas unitarias dependientes
de Logfire opcional. Se corrigieron y revalidaron sin habilitar llamadas reales.
La repetición completa del commit `722b6da`, desde el clon y venv nuevos,
terminó con **2269 passed, 14 skipped, 18 subtests passed**, sin fallos, en
791,90 segundos. Las omisiones corresponden a integraciones opt-in, herramientas
opcionales, comandos POSIX y servicios no iniciados. Las tres pruebas de Chrome
omitidas en esa corrida se ejecutaron por separado y pasaron.

El [CI de Windows del commit 722b6da](https://github.com/FourBis/4bis.relay-showcase/actions/runs/35175834672)
terminó correctamente: **252 passed, 1 skipped, 6 subtests passed**, en 174,61
segundos. Incluye las nuevas regresiones de privacidad y trazas. Este subconjunto
no se presenta como certificación de toda la aplicación.

El CI ejecuta los checks explícitos de
[checks.yml](../.github/workflows/checks.yml). Los recorridos de navegador son
opt-in y requieren Chrome y Playwright instalados:

```powershell
$env:RELAY_TEST_UI = '1'
.\mcp-server\.venv\Scripts\python.exe -m pytest `
  mcp-server/tests/test_workspace_browser.py `
  mcp-server/tests/test_admin_browser_real.py `
  mcp-server/tests/test_workflow_browser.py `
  mcp-server/tests/test_vendor_browser.py -q
```

Para la suite general:

```powershell
.\mcp-server\.venv\Scripts\python.exe -m pytest mcp-server/tests/ -q
```

Las validaciones de esta revisión se ejecutaron con HOME, USERPROFILE,
LOCALAPPDATA, SQLite y estado temporales. No prueban credenciales reales,
servicios externos, despliegue remoto ni plataformas distintas de Windows.

### Tareas persistentes y ventanas de chat — 22 de septiembre de 2026

Se incorporaron módulos Python separados por responsabilidad, tareas con
worktree persistente, cola durable y seguimiento opcional de PR, además de varias
ventanas de chat con nombre editable. Las adaptaciones conservan las protecciones
del showcase: límites del navegador, permisos de miembros, configuración vacía y
ocultación de credenciales en remotos. Los miembros reciben sólo el estado de la
tarea; rutas, configuración y diagnósticos completos requieren owner.

La transferencia contiene código genérico, documentación, pruebas y una captura
sintética. No importa historia Git, bases de datos, conversaciones, configuración
de proyectos, credenciales ni registros de instalaciones reales. La captura nueva
se revisó visualmente y no contiene metadatos de texto ni EXIF.

Comprobaciones locales de esta revisión:

- El mismo conjunto de pruebas de `checks.yml`: **319 passed y 6 subtests passed**.
- Suite general: **2343 passed, 4 failed, 10 skipped y 18 subtests passed** en
  747,06 segundos. Los cuatro fallos pertenecían a pruebas: dos usaban `main`
  en un fixture que ahora crea `develop`, uno tenía una variable renombrada dos
  veces y otro simulaba el runner desde un módulo que ya no lo invoca. Se
  corrigieron sin cambiar producción ni reducir assertions. La repetición
  `pytest mcp-server/tests --lf -q` terminó con **4 passed** en 5,51 segundos.
  No se repitió la suite completa tras esas cuatro correcciones de pruebas.
- Cuatro recorridos de navegador opt-in aprobados: workspace, administración,
  workflow y sanitización de contenido. Comprueban los chats dentro de su iframe,
  distribución del grafo, recuperación de ventanas, teclado, foco y móvil.
- Los dos recorridos sintéticos de `docs/qa` pasaron. Se añadió una comprobación
  de una sola consulta inicial del plan por conversación.
- El paquete Python se construyó y se comprobó que incluye los **142 módulos**.
- Revisión de referencias internas y escaneo del árbol exportable con Gitleaks
  8.30.1: **407 archivos**, sin hallazgos. No se agregaron exclusiones al escaneo.

Las pruebas usan repositorios Git temporales, datos ficticios y proveedores
simulados. Los tests históricos se adaptaron a los módulos responsables y a los
nuevos workspaces; el cierre de conversaciones históricas conserva su prueba de
error de PR, y el cierre de tareas administradas comprueba que no borre su rama
ni sus archivos. Los resultados no acreditan un despliegue ni un ciclo con
proveedores reales. Detalles y reproducción en la
[evidencia pública](qa/persistent-tasks-public-2026-09-22.md).

### Equipo, cuentas y trabajo largo — 24 de septiembre de 2026

Esta transferencia incorpora Equipo, los roles Admin/Subadmin/Dev/Finanzas,
asignaciones por proyecto y GitHub/Google por actor. Las tareas persistentes,
múltiples chats y el visor de diff ya formaban parte del repositorio público;
se actualizan los controles de escritura y su presentación.

Se conservan el historial Git público independiente, los filtros de metadatos
y estado para no administradores, las protecciones de Host/Origin y las
dependencias del renderer público. La nueva respuesta `allowed_actions` se
combina con un resumen sin rutas privadas. Un error de cuenta puede orientar
a **Mi cuenta**; otros errores de ejecución no revelan rutas a miembros.

No se trasladaron el roster operativo, registros OAuth, identificadores de
clientes, configuraciones, conversaciones o bases locales. Los ejemplos y las
capturas nuevas usan identidades ficticias. El código instalado requiere los
clientes OAuth y las autorizaciones personales de su propia instalación.

La demo ES/EN ahora representa una subdivisión acotada durante la ejecución,
dependencias actualizadas, Equipo editable, cuenta personal, escritura explícita
y revisión de diff. La publicación de PR se muestra como paso separado de la
integración. [Recorridos reproducibles](qa/public-launch.md).

El check visual de Equipo se compartió con el repo local y pasó contra sus
fuentes con API simulada. La adaptación de `can_control` al nuevo contrato
es específica del resumen restringido que ya existía en el showcase; no se
trasladó ese contrato público al Relay privado. No se reinició su proceso.

Las pruebas no ejecutan una migración real ni conectan cuentas. El caso de uso
documenta por separado una observación local de trabajo aún en curso, sin
presentarla como evidencia de finalización o de ahorro medido.

Resultados locales de esta transferencia:

- Conjunto completo de `checks.yml`: **437 passed y 6 subtests passed**,
  en 183,89 segundos. No se ejecutó toda la suite general del repositorio.
- Subdivisión acotada: **10 passed, 98 deselected**.
- Tras las correcciones finales se repitieron permisos de proyecto (**14 passed**),
  controles JS de tarea/workspace (**16 passed**) y CSS (**4 passed**).
  Son comprobaciones parcialmente superpuestas, no totales acumulables.
- Los recorridos de navegador de demo pública, Equipo, panel de tarea y múltiples
  chats pasaron. Incluyen recuperación tras F5 y el POST autorizado de un Dev.
- Equipo pasó también contra las fuentes del Relay local con API simulada.
- Referencias locales de Markdown y `git diff --check` correctos. Escaneo de
  identidades operativas sin coincidencias; Gitleaks 8.30.1 sin hallazgos en
  el árbol exportable y el historial público, conservando las excepciones
  precisas de fixtures ya existentes.

La validación de código y navegador es local. Integración del PR y publicación
de Pages se comprueban por separado; los borradores sociales no se enviaron.
