// Paneles arrastrables del chat (2026-08-24).
//
// El pedido fue "los paneles no se pueden mover". Eran de ancho fijo en
// CSS —288px la lista de conversaciones, 360px el panel del plan— así
// que la única palanca era el collapse, que es todo-o-nada: o tienes la
// lista entera o tienes 48px sin nada adentro.
//
// Un tirador por borde. Se arrastra, se suelta, y el ancho queda
// guardado en localStorage por panel. Doble click vuelve al default del
// CSS (borrar el override es más honesto que hardcodear el número acá:
// si mañana cambia el CSS, el "default" sigue siendo el del CSS).
//
// Sin librería: son dos divs y tres listeners. `pointerdown` +
// `setPointerCapture` y no `mousedown`/`mousemove` en document, porque
// la captura hace que el drag sobreviva a pasar por encima de un iframe
// o de un elemento que se coma el evento, y porque cubre touch y mouse
// con el mismo código.
//
// NO toca el layout abajo de 900px: ahí el panel del plan es un overlay
// y la lista es un drawer, así que un ancho fijo guardado los rompería.

const LS = "4bis.chat.panelW";

//: Topes. El mínimo es "todavía se puede usar"; el máximo evita que un
//: panel se coma el hilo entero y no se pueda volver atrás.
const LIMITES = {
  sidebar: { min: 180, max: 560 },
  grafo: { min: 260, max: 900 },
};

//: Abajo de esto el layout es responsive (overlay/drawer) y los anchos
//: guardados no aplican. Mismo número que el `@media` del CSS.
const MIN_ANCHO_VENTANA = 901;

//: Piso del hilo. Los topes de LIMITES son POR PANEL y no saben del
//: ancho de la ventana, así que se podían pedir los dos a la vez: con la
//: lista en su mínimo (180) y el plan en su máximo (900) dentro de una
//: ventana de 950, el hilo quedaba en 0px —medido el 2026-08-31— y como
//: `.btn` es `shrink-0` el header entero se pintaba ENCIMA del plan. Que
//: ceda el panel que estás moviendo, no el hilo.
const MIN_HILO = 320;

const PANELES = {
  sidebar: ".chat-sidebar",
  grafo: "#chat-grafo",
};

function leer() {
  try {
    return JSON.parse(localStorage.getItem(LS) || "{}") || {};
  } catch (_) {
    return {};
  }
}

function guardar(anchos) {
  try {
    localStorage.setItem(LS, JSON.stringify(anchos));
  } catch (_) {
    // Modo privado / cuota llena: el resize sigue andando en esta
    // sesión, solo no se acuerda. No vale romper por esto.
  }
}

function panelDe(cual) {
  return document.querySelector(PANELES[cual] || "");
}

/**
 * El ancho pedido, recortado a los topes del panel. `export` por el
 * test: es lo único del módulo que no toca el DOM y es donde un error
 * pasa desapercibido — un clamp mal hecho no tira excepción, deja un
 * panel de 12px o uno que se comió el hilo, y en los dos casos parece
 * que el drag "anda mal" en vez de que el tope esté mal.
 */
export function anchoValido(cual, px, techo = Infinity) {
  const l = LIMITES[cual];
  if (!l) return 0;
  // `techo` es lo que la VENTANA deja libre (ver `techoDeVentana`).
  // Puede quedar por debajo del mínimo del panel; ahí gana el mínimo:
  // una columna de 40px no es usable, y para eso ya está el collapse.
  const max = Math.max(l.min, Math.min(l.max, techo));
  return Math.round(Math.min(max, Math.max(l.min, px)));
}

/**
 * Cuánto puede medir `cual` sin bajar el hilo de `MIN_HILO`, contando lo
 * que ya ocupa el OTRO panel. `Infinity` si el workspace todavía no está
 * en el DOM (arranque): ahí manda el tope del panel y listo.
 */
function techoDeVentana(cual) {
  const ws = document.querySelector(".chat-workspace");
  const ancho = ws ? ws.getBoundingClientRect().width : 0;
  // Tab oculto (o todavía sin layout): medir da 0 y el techo saldría
  // negativo, o sea que recortaría los dos paneles a su mínimo y el
  // ancho guardado del usuario se perdería en silencio. Sin dato, manda
  // el tope del panel; `observarWorkspace` re-aplica cuando mida.
  if (ancho <= 0) return Infinity;
  const otro = panelDe(cual === "sidebar" ? "grafo" : "sidebar");
  const ocupa = otro && !otro.hidden
    ? otro.getBoundingClientRect().width : 0;
  return ancho - ocupa - MIN_HILO;
}

/**
 * Re-aplica los anchos cuando cambia el espacio disponible. El `resize`
 * de la ventana no alcanza: el workspace también cambia de ancho al
 * entrar al tab (arranca en 0), al colapsar el rail global y al abrir o
 * cerrar el panel del plan. Sin esto, un ancho guardado que ya no entra
 * se queda sin recortar hasta que alguien toque la ventana.
 */
function observarWorkspace() {
  const ws = document.querySelector(".chat-workspace");
  if (!ws || typeof ResizeObserver !== "function") return;
  let ultimo = -1;
  new ResizeObserver(() => {
    // `aplicar` toca a los HIJOS, así que esto no se puede re-disparar
    // solo; el guard es para no hacer trabajo de más en cada animación.
    const w = Math.round(ws.getBoundingClientRect().width);
    if (w === ultimo) return;
    ultimo = w;
    aplicarAnchosGuardados();
  }).observe(ws);
}

/** Aplica un ancho al panel, o lo saca para que mande el CSS. */
function aplicar(cual, px) {
  const el = panelDe(cual);
  if (!el) return;
  // Colapsado manda el CSS (`.collapsed` → 3rem). Un estilo inline le
  // gana a la clase por especificidad, así que sin este guard colapsar
  // con un ancho guardado dejaba una columna de 420px con solo el
  // chevron y el ➕ flotando adentro — que es como se ve rota.
  if (el.classList.contains("collapsed")) px = 0;
  if (!px) {
    el.style.removeProperty("flex");
    el.style.removeProperty("width");
    return;
  }
  const w = anchoValido(cual, px, techoDeVentana(cual));
  el.style.flex = `0 0 ${w}px`;
  el.style.width = `${w}px`;
}

/** Reaplica lo guardado. Se llama al arrancar y al redimensionar. */
export function aplicarAnchosGuardados() {
  const angosto = window.innerWidth < MIN_ANCHO_VENTANA;
  const anchos = leer();
  for (const cual of Object.keys(PANELES)) {
    aplicar(cual, angosto ? 0 : anchos[cual]);
  }
}

function arrancarDrag(tirador, ev) {
  const cual = tirador.dataset.resize;
  const el = panelDe(cual);
  if (!el || window.innerWidth < MIN_ANCHO_VENTANA) return;

  const x0 = ev.clientX;
  const w0 = el.getBoundingClientRect().width;
  // El tirador de la izquierda crece hacia la derecha; el del panel del
  // plan está a SU izquierda, así que arrastrar a la derecha lo achica.
  const signo = cual === "grafo" ? -1 : 1;

  // La captura hace que el drag sobreviva a pasar por encima de otro
  // elemento, pero tira `NotFoundError` si el pointer ya no está activo
  // (soltaste el botón entre el evento y esta línea). Sin captura el
  // drag anda igual mientras no salgas del tirador: no vale abortarlo.
  try {
    tirador.setPointerCapture(ev.pointerId);
  } catch (_) { /* seguimos sin captura */ }
  tirador.classList.add("arrastrando");
  document.body.classList.add("resizeando");

  const mover = (e) => aplicar(cual, w0 + signo * (e.clientX - x0));
  const soltar = () => {
    tirador.removeEventListener("pointermove", mover);
    tirador.removeEventListener("pointerup", soltar);
    tirador.removeEventListener("pointercancel", soltar);
    tirador.classList.remove("arrastrando");
    document.body.classList.remove("resizeando");
    const anchos = leer();
    anchos[cual] = Math.round(el.getBoundingClientRect().width);
    guardar(anchos);
  };
  tirador.addEventListener("pointermove", mover);
  tirador.addEventListener("pointerup", soltar);
  tirador.addEventListener("pointercancel", soltar);
  ev.preventDefault();
}

/**
 * El tirador del panel del plan solo tiene sentido cuando el panel se
 * ve. Lo llama chat-grafo.js al mostrar/ocultar: sin esto queda una
 * línea arrastrable contra el borde de la ventana que no mueve nada.
 */
export function verTiradorGrafo(visible) {
  const t = document.querySelector('.chat-resizer[data-resize="grafo"]');
  if (t) t.hidden = !visible;
}

export function wirePanelResize() {
  for (const tirador of document.querySelectorAll(".chat-resizer")) {
    tirador.addEventListener("pointerdown", (ev) => arrancarDrag(tirador, ev));
    tirador.addEventListener("dblclick", () => {
      const cual = tirador.dataset.resize;
      const anchos = leer();
      delete anchos[cual];
      guardar(anchos);
      aplicar(cual, 0);
    });
    // Teclado: mismo gesto para quien no arrastra. 24px por flecha.
    tirador.addEventListener("keydown", (ev) => {
      const paso = ev.key === "ArrowLeft" ? -24
        : ev.key === "ArrowRight" ? 24 : 0;
      if (!paso) return;
      const cual = tirador.dataset.resize;
      const el = panelDe(cual);
      if (!el) return;
      const signo = cual === "grafo" ? -1 : 1;
      aplicar(cual, el.getBoundingClientRect().width + signo * paso);
      const anchos = leer();
      anchos[cual] = Math.round(el.getBoundingClientRect().width);
      guardar(anchos);
      ev.preventDefault();
    });
  }
  aplicarAnchosGuardados();
  observarWorkspace();
  // Cruzar el breakpoint en los dos sentidos: al achicar hay que soltar
  // los anchos fijos, y al agrandar hay que volver a ponerlos.
  window.addEventListener("resize", aplicarAnchosGuardados);
}
