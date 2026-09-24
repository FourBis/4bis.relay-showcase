# Validación de la presentación pública

La web de `site/` es una simulación estática ES/EN. No ejecuta Relay,
proveedores ni herramientas. Sólo este directorio se publica en GitHub Pages.
El artículo y los README describen la aplicación local por separado.

## Reproducir

Con Node y el tooling Playwright usado por las otras comprobaciones visuales:

```powershell
node --check site/demo.js
node --check site/tour.js
node docs/qa/public-demo.mjs
```

Por defecto, el check sirve sólo `site/` en un puerto local temporal y cierra
el servidor al terminar. `PUBLIC_DEMO_URL` permite comprobar el mismo recorrido
en la web publicada; `PUBLIC_DEMO_OUT` cambia la carpeta de capturas.

## Comprobaciones

- Catálogo accesible de siete capacidades, un panel visible a la vez y navegación
  con flechas, Home y End. Se prueban las siete posiciones del recorrido
  cliente → proyecto → equipo → repositorio → ejecución → revisión → documentación
  y el panel que abre cada enlace. El idioma inicial también se comprueba con
  `?lang=en`, incluidos los destinos bilingües de documentación.
- Ocho controles de captura abren PNG locales con pie bilingüe; Escape y el
  botón de cierre devuelven el foco al activador. Las imágenes lazy se desplazan
  a la vista antes de comprobar su carga.
- Dos conversaciones conservan borradores separados al abrir, cerrar y recuperar.
- Renombrar guarda el nombre; Escape cancela; texto escrito se trata como texto.
- Los escenarios conservan la rama, workspace y número de PR ficticios.
- El grafo muestra dependencias antes y después de una subdivisión en cuatro
  subtareas; el dependiente espera a todas ellas y no hay una segunda división.
- Equipo permite editar y guardar proyectos de una persona ficticia. La cuenta
  personal de GitHub, habilitar escritura y continuar son pasos separados.
- La PR de ejemplo se publica desde una escena de Admin y permanece sin integrar.
- Reinicio de demo, ES/EN, etiquetas accesibles y navegación de pestañas por teclado.
- Anchos de 1440, 768, 390 y 320 píxeles, sin desbordamiento horizontal; los
  siete paneles y un diálogo de captura se recorren también a 390 y 320 píxeles.
- Imágenes cargadas, sin errores de consola y sin solicitudes externas, fetch,
  XHR ni WebSocket. La CSP bloquea conexiones y scripts de otros orígenes.

La comprobación local pasó el 24 de septiembre de 2026: 49 solicitudes GET al
mismo origen, cero errores y ninguna llamada a un backend. Pasaron los siete
paneles en escritorio y móvil, los ocho controles de captura y los flujos
anteriores de chats, subdivisión, equipo y PR. Se inspeccionaron las capturas de
componentes en `PUBLIC_DEMO_OUT` (por defecto `%TEMP%\relay-public-demo`): hero
y paneles de Modelos, Contexto y Equipo en escritorio; hero y capacidades en
móvil. También se comprobó el marcado: 17 enlaces a documentación, 73 IDs únicos
y ocho PNG únicos. Estas pruebas validan la presentación pública; no acreditan
ejecución de modelos, aislamiento del sistema ni producción.

Los cambios de esta validación pertenecen solo a la página pública. No cambian
la UI Admin local y no requieren reiniciar Relay.

La demo no conserva los mensajes al recargar. Los enlaces externos abren
GitHub o el sitio público de FourBis sólo al navegar a ellos. No se incorporaron
analítica de visitantes, cookies ni dependencias a la web.

## Equipo y controles en la aplicación instalable

```powershell
node docs/qa/team-access-visual.mjs
node docs/qa/task-panel-visual.mjs
node docs/qa/multi-chat-visual.mjs
```

Equipo usa el HTML, CSS y módulo reales con respuestas de API en memoria.
Comprueba que editar no guarde antes de pulsar el botón, que la asignación se
envíe y aparezca en la tabla, que Finanzas no tenga proyectos con escritura y
que Subadmin no pueda asignarlos. También comprueba móvil a 390 px.

El panel de tarea comprueba que un Dev con un resumen sin datos privados y
`allowed_actions` pueda enviar **Continuar** aunque el indicador antiguo
`can_control` sea falso. Los controles de publicación y seguimiento permanecen
fuera de su alcance. Esta adaptación conserva el contrato propio del showcase.

Se ejecutó además Equipo con `RELAY_UI_ROOT` apuntando al código del Relay local;
pasó el mismo recorrido con datos ficticios. El check se comparte con el repo
local. El nuevo flujo de Pages es una simulación específica del sitio: no
sustituye las pantallas operativas del Relay ni requiere reiniciar su proceso.
