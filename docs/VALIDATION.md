# Validación de la versión de portafolio

Revisión local del 16 y 17 de septiembre de 2026. Relay se presenta como proyecto
experimental para uso local, con licencia [MIT](../LICENSE). Esta revisión reduce
riesgos concretos de publicación; no certifica ausencia de vulnerabilidades ni
preparación para un servicio público multiusuario.

## Publicación y privacidad

- Copia independiente con historial nuevo, sin heredar el repositorio privado.
- Copia alojada en el repositorio público `FourBis/4bis.relay-showcase`,
  verificado con `develop` como rama predeterminada y `79a26513` como HEAD de esta
  revisión.
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
datos ficticios locales, sin proveedores reales. El recorrido principal registró
**66 passed y 1 fallo**: el fallo corresponde a un selector obsoleto del test;
los indicadores secundarios ahora están en un desplegable y la navegación se
abre desde el catálogo. Una copia de trabajo adaptada a esa interfaz conservó
las assertions y completó el flujo de workflow con **1
passed en 7,78 segundos**; esto no implica que el archivo público del test haya
sido corregido.

Otro grupo aislado registró **94 passed y 6 subtests**. Se comprobaron edición
y persistencia de un proyecto ficticio, tablas desprendibles y filtro de filas,
mosaico, métricas y chat móvil a 390 px sin desbordamiento horizontal.

Quedan dos detalles menores de UI: “Cerrados recientemente” ordena las ventanas
por apertura, no por cierre; el botón de ensanchar el grafo cambia de estado
pero el CSS del workspace mantiene su ancho. El grafo abre, muestra las
dependencias y permite inspeccionar tareas. Ninguno bloqueó los recorridos
comprobados. También aparece un aviso de consola al no existir el botón de
proyectos ocultos; no interrumpe el módulo Gestión.

Las nuevas capturas del README se generaron desde la aplicación sin modificar,
con proyectos, conversaciones, estados y consumo sintéticos en una base temporal.
La captura automatizada comprobó ausencia de errores JavaScript y bloqueó toda
solicitud del navegador fuera del servidor local (**1 passed**, 8,37 segundos).

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
  mcp-server/tests/test_vendor_browser.py -q
```

Para la suite general:

```powershell
.\mcp-server\.venv\Scripts\python.exe -m pytest mcp-server/tests/ -q
```

Las validaciones de esta revisión se ejecutaron con HOME, USERPROFILE,
LOCALAPPDATA, SQLite y estado temporales. No prueban credenciales reales,
servicios externos, despliegue remoto ni plataformas distintas de Windows.
