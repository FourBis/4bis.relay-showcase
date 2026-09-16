---
name: relay-ui
description: Design system del panel admin del 4bis.relay (mcp-server/admin_static). Usalo SIEMPRE que toques algo visual de la admin UI — un tab nuevo, una tabla, un formulario, un gráfico, un estado vacío, un modal, un badge, un icono, o cualquier cambio en index.html, static/*.js o admin.src.css. También aplica cuando alguien pide "que se vea mejor", "modernizar", "arreglar el diseño" o "agregar una pantalla" en el admin, aunque no nombre el design system. Trae el inventario de componentes que YA existen (para no reescribirlos), las reglas de tablas/formularios/charts/estados, y el paso de build sin el cual el CSS nuevo no hace nada.
---

# Admin UI del 4bis.relay

Stack: Tailwind CLI v3 compilado (no CDN, no JIT en runtime) + ES modules
planos, sin framework y sin red externa. Un tab = un archivo
`static/tab-<nombre>.js` que escribe HTML en su `<section id="tab-...">`
de `index.html`. Referencia funcional de cada tab: `docs/ADMIN_UI.md`.

La vara de calidad es el tab **Chat**: workspace de tres paneles, estados
vacíos reales, densidad pensada. Si lo que estás por escribir se ve peor
que eso, todavía no está.

## 1. Antes de escribir: mirá si ya existe

Casi todo lo que se necesita ya está. Escribir la versión propia de algo
que existe es la forma en que esta UI se volvió inconsistente: cada tab
tenía su empty state, su forma de poner labels y su chart.

| Necesitás | Usá | De |
|---|---|---|
| Tabla con orden, filtro y paginación | `dataTable(mount, opts)` | `ui-table.js` |
| Gráfico de barras (1 o N series) | `barChart(mount, opts)` | `ui-chart.js` |
| Serie de días sin huecos | `fillDays(rows, days, empty)` | `ui-chart.js` |
| Panel de estado vacío | `emptyState({title, sub, action})` | `ui.js` |
| Vacío como fila de tabla | `emptyRow(cols, opts)` | `ui.js` |
| Filas fantasma mientras carga | `skeletonRows(cols, n)` | `ui.js` |
| Campo de formulario | `field({label, control, hint})` | `ui.js` |
| Grupo de campos en card | `formSection({title, sub, body})` | `ui.js` |
| Confirmar / avisar | `confirmModal()` / `alertModal()` | `ui.js` |
| Modal con carga async | `modalLoad({id, loader, render})` | `ui.js` |
| Panel lateral | `openSidePanel()` / `setSidePanelBody()` | `ui.js` |
| Feedback no bloqueante | `toast(msg, kind)` | `ui.js` |
| Badge de estado | `statusBadge(s)` / `.badge.ok\|warn\|err\|dim` | `ui.js` |
| Tarjeta de métrica | `statCell(v, k)` | `ui.js` |
| Fetch al relay | `api(path)` / `apiRoot(path)` | `api.js` |
| Escapar, formatear | `escape`, `fmtNum`, `fmtDuration`, `formatElapsed` | `api.js` |
| Refresco periódico | `registerPoller(fn, ms, {tabId})` | `pollers.js` |

`window.alert` / `confirm` / `prompt` no se usan: hay modales propios que
comparten el manejo de foco, Esc y el watchdog de 8s.

Para ver cómo se ven todos estos componentes con datos reales, con el
relay corriendo: `http://127.0.0.1:8413/admin/static/_kitchen-sink.html`
(fuente en `admin_static/static/_kitchen-sink.html`). Si agregás un
componente a la capa compartida, agregalo también ahí — es el único lugar
donde se ven todos juntos y donde se nota si dos no combinan.

## 2. Tokens

Superficie `zinc-950`, cards `zinc-900`, bordes `zinc-800`, texto
`zinc-100` / `zinc-400` (secundario) / `zinc-500` (terciario). Acento
`emerald` — es el color de "esto está activo o es la acción principal",
no decoración. Rojo solo para destructivo y error; ámbar para "atención,
no roto".

Clases de la capa de componentes (todas en `admin.src.css`, sección
`@layer components`): `.card` `.panel-title` `.panel-sub`
`.section-title` `.btn` `.btn-primary` `.btn.danger` `.icon-btn` `.input`
`.select` `.label` `.toolbar` `.table-wrap` `.th` `.badge` `.stat`
`.empty` `.empty-state` `.skeleton` `.form-section` `.field` `.chart-*`
`.dt-*` `.modal` `.side-panel`.

Preferí una clase de la capa antes que una ristra de utilidades sueltas:
las utilidades sueltas son cómo se pierde la consistencia entre tabs.

## 3. Tablas

Cualquier tabla que pueda pasar de ~15 filas va con `dataTable`. Filas
planas sin ordenar ni buscar obligan al usuario a Ctrl+F.

- La tabla se pinta en dos tiempos (shell una vez, `<tbody>` en cada
  refresh) para que un poller no le robe el foco al buscador ni borre lo
  tipeado. Si escribís una tabla a mano, respetá eso.
- Un dato booleano es un indicador, no la palabra "sí" repetida en cuatro
  columnas.
- Las acciones de fila van en `.cell-actions` / `.action-group`, alineadas
  a la derecha y agrupadas por riesgo. La destructiva se ve destructiva
  **en reposo** (`.btn.danger`), no solo en hover: en un diálogo donde los
  dos botones dicen casi lo mismo, el hover llega tarde.

## 4. Formularios

Agrupá en `formSection`; cada control va en un `field`, siempre en el
orden label → control → hint/error. La pantalla de Config existía como
una pila vertical de filas sueltas con tres variantes de label distintas
y eso es exactamente lo que hay que no repetir.

Un formulario que guarda tiene que decir que guardó (`toast(..., "ok")`)
y qué falló, en el campo (`field({error})`), no en un alert genérico.

## 5. Gráficos

Usá `barChart`. Las reglas vienen del skill `dataviz` y no son estéticas,
son de lectura:

- **Eje de tiempo honesto**: los días sin actividad son barra 0. Saltearlos
  comprime el eje y miente sobre el ritmo. Para eso está `fillDays`, y la
  serie la arma el caller, que es quien conoce el rango.
- **Un solo eje.** Dos medidas de escalas distintas son dos gráficos.
- **Legend con 2+ series, nunca con una** (con una, el título ya dice qué
  se grafica).
- **Label directo solo en el extremo**, jamás en cada barra.
- **El texto nunca se pinta del color de la serie**: el color lo lleva el
  swatch al lado.
- **La paleta no se inventa.** Los 8 slots categóricos están definidos en
  `.chart` y van en orden fijo — el orden ES el mecanismo de seguridad
  para daltonismo, no decoración. Si agregás, cambiás o reordenás un slot,
  volvé a correr el validador del skill `dataviz` contra la superficie
  real (`--mode dark --surface "#18181b"`, que es la card, no el fondo de
  página). El 9º slot no se genera: se pliega en "otros".
- Toda tabla de datos que se grafica queda disponible como tabla en un
  `<details>` al lado: es el equivalente accesible.

## 6. Estados

Toda vista que carga datos tiene tres estados además del normal, y los
tres son código, no suerte:

- **cargando** → `skeletonRows` (una tabla muda no distingue "cargando" de
  "vacío" de "el server se colgó");
- **vacío** → `emptyState` / `emptyRow`, con la causa y una salida. Un
  vacío sin acción deja al usuario sin saber si está roto o si le falta
  hacer algo;
- **error** → `errorHtml(e)` o un `toast(msg, "err")` con el mensaje real
  del server, no "algo salió mal".

"Filtré y no hay resultados" no es lo mismo que "no hay datos": el texto
tiene que distinguirlos.

## 7. Iconos y accesibilidad

Los iconos son `<svg>` inline con `stroke="currentColor"` (mirá los de la
nav en `index.html`). **Emoji no es un icono de acción**: no hereda color,
no tiene estado disabled, cambia de forma por plataforma y no se puede
etiquetar. Todo botón icon-only lleva `aria-label`.

Lo que ya está bien y no se rompe: `skip-link`, `role="tab"` +
`aria-selected` en la nav, `aria-sort` en las columnas ordenables,
`focus-visible:ring` en todo lo interactivo, foco atrapado en los modales,
Esc para cerrar, y `prefers-reduced-motion` respetado en las animaciones.

## 8. El build (sin esto tu CSS no existe)

`admin.css` es output commiteado. Después de tocar `admin.src.css` **o**
de usar una clase de Tailwind nueva en HTML/JS:

```powershell
pwsh mcp-server/admin_static/build-css.ps1
```

Hay dos sellos que atrapan dos olvidos distintos —`src-sha256` (editaste
la fuente y no rebuildeaste) y `classes-sha256` (usaste una clase nueva y
el bundle no la tiene, así que no hace nada, en silencio). Los verifica
`tests/test_admin_css_build.py`. Detalle en `docs/ADMIN_UI.md` § "El CSS:
dos sellos, dos olvidos distintos".

**Trampa de la capa de componentes:** Tailwind purga de `@layer
components` toda clase que no aparezca en el contenido escaneado, que es
solo `index.html` y `static/**/*.js`. Definir una clase en
`admin.src.css` no alcanza para que exista: si la escribe únicamente un
caller que todavía no existe, la regla no sale en el bundle, y si el
único caller se va, desaparece de nuevo. Por eso una clase de layout que
pertenece a un componente la emite el helper (`formSection` pone
`.form-grid`), no el caller. Para verificar que una clase nueva llegó:

```bash
grep -c "\.mi-clase" mcp-server/admin_static/static/admin.css
```

HTML, JS y CSS son **live**: el relay los relee por mtime, no hace falta
reiniciarlo. Solo los cambios en `src/relay/*.py` piden reinicio.

## 9. Checks

La lógica no trivial de la UI se prueba con Node puro, sin deps, en
`mcp-server/tests/*.test.mjs` — se importa el módulo por `data:` URL
neutralizando los imports con stubs. Corren dentro de pytest vía
`test_js_suite.py`, así que un test roto se ve en la suite y no cuando
alguien se acuerda. Mirá `ui-table.test.mjs` o `ui-chart.test.mjs` como
molde: prueban la lógica pura (filtrar/ordenar/paginar, geometría del
chart) sin emular medio navegador.

Después de un cambio visible, verificalo en el navegador contra el relay
corriendo (`preview_start` con la config `relay` de `.Codex/launch.json`)
y mirá la consola: cero errores es parte del entregable.
