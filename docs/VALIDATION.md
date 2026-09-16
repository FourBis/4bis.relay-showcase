# Validación de la versión de portafolio

Revisión local del 16 de septiembre de 2026. Relay se presenta como proyecto
experimental para uso local, con licencia [MIT](../LICENSE). Esta revisión reduce
riesgos concretos de publicación; no certifica ausencia de vulnerabilidades ni
preparación para un servicio público multiusuario.

## Publicación y privacidad

- Copia independiente con historial nuevo, sin heredar el repositorio privado.
- Sin remoto configurado, push ni cambio de visibilidad.
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

Validación de estos cambios: dos grupos con **93 passed** y **91 passed**;
comprobación adicional del default/override de la ruta CRM; formulario de voz
y configuración comprobados en Chrome con datos temporales y sin errores de
consola. Se corrigieron las expectativas de orden al renombrar los fixtures.
No se volvió a ejecutar la suite general completa.

## Instalación y dependencias

Instalación editable con extras de desarrollo en un venv nuevo, Python 3.12 y
Windows: completada. `pip check` sin conflictos, metadatos MIT generados.
La guía actualiza pip antes de instalar dependencias.

Consulta de avisos conocidos al 16 de septiembre de 2026:

- 101 paquetes Python de la instalación nueva, incluido pip: sin avisos activos
  devueltos por la API de PyPI. El paquete local Relay se revisa como código.
- 81 coordenadas npm del JavaScript distribuido y sus inputs de build: sin avisos
  devueltos por OSV. Inventario obtenido de 33 sourcemaps anidados y dos metafiles
  de esbuild; el CSS de Tailwind se documenta por separado.
- El workspace de compilación upstream contiene otros paquetes/herramientas con
  avisos. Ese workspace y sus `node_modules` no se distribuyen con Relay.

Estos resultados describen las versiones resueltas en esa fecha; los rangos de
instalación y las bases de avisos pueden cambiar.

## Pruebas y límites

La revalidación de la revisión MIT y de seguridad, previa a la anonimización,
del subconjunto de CI y los cinco archivos con
fixtures reparados produjo **414 passed, 1 skipped, 6 subtests passed** en
208,68 segundos. Incluye identidad, límites HTTP, configuración, timeouts y
contratos de interfaz. La construcción de un modelo OpenAI con credencial de
catálogo se verifica sin llamar al proveedor.

Los tres recorridos reales de navegador pasaron con los vendors definitivos:
workspace y objetos flotantes; estados del chat y adjuntos; render Mermaid de
flujo, secuencia, clases y frontmatter, más sanitización de HTML malicioso.
También pasaron JavaScript, sintaxis y los sellos del CSS recompilado.

La corrida general se detuvo a los diez fallos: **1288 passed, 4 skipped,
12 subtests passed**. Encontró el reset del timeout, fixtures desactualizados y
un fallo de Git que no se reprodujo al ejecutar sus 29 pruebas aisladamente.
Se corrigieron los casos identificados y se revalidaron por grupos. **No se ha
obtenido una corrida general completa en verde**; no se presenta el subconjunto
de CI como certificación de todo el producto.

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
