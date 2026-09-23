// Panel del grafo: el plan mientras corre (F3, 2026-08-23).
//
// El pedido era "que se vea si el plan se está siguiendo, y en qué punto
// va — algo gráfico, como un grafo". La parte de "gráfico" no es
// decoración: una lista numerada puede decir *cuántas* tareas faltan,
// pero no puede mostrar **qué es secuencia obligada y qué es trabajo
// simultáneo**. Eso es lo que se pierde y es justo lo que el humano
// necesita para saber si el plan avanza o está trabado.
//
// El dibujo sale de `capas`, que calcula el servidor (`grafo.capas`):
// cada capa es una fila, y **estar en la misma fila ES poder correr en
// paralelo**. Las posiciones se derivan de ahí con dos for anidados. Sin
// librería de grafos: el DAG tiene doce nodos, no mil, y meter una
// dependencia de layout para esto sería pagar 200kB por aritmética.
//
// Vive en su propio módulo y no dentro de tab-chats.js —que ya tiene
// 2200 líneas— por lo mismo que chat-tools.js: lo nuevo entra al costado.
// La diferencia con aquel es que este SÍ trae clases al bundle
// (`.chat-grafo*`, `.gnodo`, `.garista` en admin.src.css), así que tocar
// sus estilos pide un `build-css.ps1`.

import { $, apiRoot, escape } from "./api.js";
import { toast, confirmModal } from "./ui.js";
import { verTiradorGrafo } from "./panel-resize.js";
import { isChatViewVisible } from "./chat-window.js";

const POLL_MS = 2500;

// Geometría del dibujo, en unidades del viewBox. El SVG escala al ancho
// del panel, así que lo que importa NO son los números sueltos sino la
// relación entre el ancho total y el del panel: con tres nodos por fila
// el viewBox mide ~360 y el panel útil ~344, o sea que casi no se
// achica y el texto queda del tamaño que dice acá. Nodos más anchos se
// veían mejor en el diseño y peor en pantalla: el SVG se encogía para
// entrar y la letra terminaba en 7px, ilegible.
const NODO_W = 108, NODO_H = 40, GAP_X = 10, GAP_Y = 24, PAD = 8;
//: Cuántos caracteres entran por línea del nodo, a 11px en 108 unidades.
const CHARS_LINEA = 16;

const ETIQUETA = {
  pendiente: "pendiente", corriendo: "corriendo", hecho: "hecho",
  fallado: "falló", bloqueado: "bloqueada", esperando_humano: "te espera",
};

function etiquetaTarea(t) {
  return t.sustituido ? "subdividida" : t.presupuesto_agotado ? "sin presupuesto"
    : (ETIQUETA[t.estado] || t.estado);
}

//: Escalones de zoom. `0` es "que entre en el panel" (el comportamiento
//: de siempre); el resto es factor sobre el tamaño natural del viewBox.
//: Arranca en `0` porque un grafo chico entra y se lee bien; el que no
//: entra es el ancho, y para ese están los botones.
const ZOOMS = [0, 0.75, 1, 1.5, 2, 3];

let estado = null;   // { convId, graphId, timer, sel, vista, zoom } | null

// =====================================================================
// Ciclo de vida
// =====================================================================

/** Engancha el panel a una conversación. Si no tiene grafo, lo apaga. */
export async function attachGrafo(convId) {
  detachGrafo();
  if (!convId) return;
  // `vista` y `zoom` se resetean por conversación a propósito: son una
  // preferencia sobre ESTE plan, no una configuración del usuario.
  estado = { convId, graphId: null, timer: null, sel: null,
             vista: "grafo", zoom: 0, g: null };
  verVista("grafo");
  await refrescar();
}

/**
 * ¿Hay un plan trabajando en el hilo que se está mirando?
 *
 * Lo pregunta tab-chats.js antes de cerrar la conversación. Existe
 * porque `activeChat.busy` mira los runs del CHAT y un grafo no crea
 * ninguno: sin esto, "Cerrar" mientras el plan corre compactaba la
 * memoria y abría el PR con las tareas a mitad de camino.
 *
 * Devuelve `null` si no hay plan; si lo hay, el mínimo para armar el
 * mensaje ("lleva 20:31, van 5 de 12").
 */
export function planEnCurso() {
  const g = estado?.g;
  // Un grafo sintético (Etapa A) NO es un plan en curso: solo refleja
  // los turnos del hilo. tab-chats.js avisa con esto antes de cerrar
  // la conversación; sin el guard, avisaría en TODOS los hilos con
  // turnos, incluso los viejos.
  if (!g || g.sintetico === true || g.corriendo !== true) return null;
  const p = g.progreso || {};
  return {
    hechos: p.hechos || 0,
    total: p.total || 0,
    reloj: cronometro(arranqueMasViejo(g.tasks || [])),
    tareas: (g.tasks || []).filter((t) => t.estado === "corriendo")
      .map((t) => t.titulo),
  };
}

/** Suelta el polling. Se llama al cambiar de conversación y al cerrar. */
export function detachGrafo() {
  if (estado?.timer) clearTimeout(estado.timer);
  pararReloj();
  const aviso = $("#chat-grafo-aviso");
  if (aviso) aviso.hidden = true;
  estado = null;
  const panel = $("#chat-grafo");
  if (panel) panel.hidden = true;
  const btn = $("#chat-panel-grafo");
  if (btn) btn.hidden = true;
  verTiradorGrafo(false);
}

/**
 * Algo cambió en el hilo: arrancó un run, terminó, o nació un grafo (el
 * disparador corta por pedido grande). En los tres casos el panel tiene
 * algo nuevo que mostrar.
 */
export async function pokeGrafo() {
  if (estado) await refrescar();
}

async function refrescar() {
  const propio = estado;
  if (!propio) return;
  if (!isChatViewVisible()) { programar(propio); return; }
  let r = null;
  try {
    // UN pedido. El servidor decide qué mostrar: grafo si el pedido se
    // partió en tareas, plan lineal si fue un run normal (el
    // planificador arma uno en CADA run), o nada si el hilo no corrió
    // todavía. Encadenar tres requests desde acá dejaba que el
    // navegador decidiera cuál gana.
    r = await apiRoot(
      `/conversations/${encodeURIComponent(propio.convId)}/plan`);
  } catch (_) {
    // Sin red no se vacía lo que YA se mostró de este hilo: un timeout
    // no significa que el plan desapareció. Pero si todavía no
    // mostramos nada de este hilo, tampoco se puede dejar en pantalla
    // lo del hilo anterior — eso es peor que un panel vacío, porque se
    // lee como el plan de la conversación que estás mirando.
    programar(propio);
    return;
  }
  if (estado !== propio) return;      // cambiaste de conversación mientras

  // El endpoint siempre devuelve modo "grafo" desde la Etapa A (P3).
  // Si por algún motivo no trae grafo, dejamos lo que ya estaba en
  // pantalla en vez de romper el panel con un panel vacío.
  if (!r?.grafo) {
    programar(propio);
    return;
  }
  if (!r.grafo.tasks?.length && !r.grafo.corriendo) {
    // Un grafo sintético vacío no es un plan "hecho" ni debe tapar el chat.
    propio.g = r.grafo;
    for (const id of ["#chat-grafo", "#chat-panel-grafo"]) {
      const el = $(id);
      if (el) el.hidden = true;
    }
    verTiradorGrafo(false);
    programar(propio);
    return;
  }
  mostrar();
  pintar(r.grafo);
  // Solo se sigue mirando lo que puede cambiar. Un grafo terminado
  // deja de costar un request cada 2.5s para siempre.
  //
  // `corriendo` va en el OR y no de adorno: lo dice el relay mirando el
  // hilo, no el grafo. Un plan que terminó mientras el hilo sigue
  // trabajando da estado "hecho" y `corriendo: true`, y con solo la
  // primera condición el panel se congelaba justo ahí — el caso que
  // reportó el humano el 30/8: "el proceso siguiente lo hizo bien pero
  // en la UI se refleja mal".
  if (r.grafo.progreso?.estado === "activo" || r.grafo.corriendo === true) {
    programar(propio);
  }
}

function programar(propio) {
  if (propio.timer) clearTimeout(propio.timer);
  propio.timer = setTimeout(() => { if (estado === propio) refrescar(); },
                            POLL_MS);
}

// ---------- el cronómetro ----------
//
// Corre aparte del poll (2.5s) porque son dos cosas distintas: el poll
// trae datos nuevos del servidor, esto solo re-dibuja el tiempo que ya
// se puede calcular en el cliente. Atarlo al poll haría que el número
// salte de a 2-3 segundos, que se lee como una UI trabada — justo lo
// contrario de lo que este contador tiene que transmitir.

let tic = null;

function arrancarReloj() {
  if (tic) return;
  tic = setInterval(refrescarRelojes, 1000);
}

function pararReloj() {
  if (tic) clearInterval(tic);
  tic = null;
}

/** Re-dibuja SOLO los tiempos, con el último grafo que llegó. */
function refrescarRelojes() {
  const g = estado?.g;
  if (!g || g.corriendo !== true) { pararReloj(); return; }
  const conteo = $("#chat-grafo-conteo");
  if (conteo) conteo.textContent = resumenTexto(g.progreso || {}, g.tasks || []);
  // Cada span marcado lleva su propio `desde`: así esto no necesita
  // saber dónde está dibujado ni volver a armar el HTML.
  for (const el of document.querySelectorAll("[data-desde]")) {
    el.textContent = cronometro(el.dataset.desde);
  }
  pintarAviso(g);
}

/**
 * El aviso arriba del composer mientras el plan corre.
 *
 * Existe porque el composer NO sabe del grafo: `activeChat.busy` mira
 * los runs del chat, y un grafo no crea ninguno, así que el placeholder
 * decía "Mensaje…" y mandar algo arrancaba un run NUEVO compitiendo con
 * el plan por los mismos archivos. Con el contador a la vista, un
 * mensaje de más deja de ser lo primero que uno hace cuando no pasa nada
 * en pantalla.
 */
function pintarAviso(g) {
  const aviso = $("#chat-grafo-aviso");
  // `sintetico !== true` por lo mismo que lo pide `planEnCurso`: en un
  // grafo sintético "corriendo" es un run NORMAL del chat, y de ese el
  // composer sí se entera solo (`activeChat.busy` lo deja ocupado).
  // Sin el guard, cualquier run pintaba "el plan está trabajando… lo
  // que escribas arranca un pedido nuevo en paralelo" sobre un
  // composer que justamente no deja escribir.
  const vivo = g && g.corriendo === true && g.sintetico !== true;
  // El botón de cerrar avisa ANTES de clickearlo. Sigue habilitado a
  // propósito: deshabilitado no dispara el click y el humano se queda
  // sin saber por qué no puede: el modal es el que explica.
  const cerrar = $("#chat-panel-close-conv");
  if (cerrar) {
    cerrar.classList.toggle("ocupado", !!vivo);
    cerrar.title = vivo
      ? "El plan está trabajando — no se puede cerrar hasta que termine"
      : "Cerrar la conversación (compacta memoria y abre PR)";
  }
  if (!aviso) return;
  aviso.hidden = !vivo;
  if (!vivo) {
    // Vaciarlo y no solo ocultarlo: adentro queda un `data-desde` con la
    // hora del nodo que ya cerró, y ese atributo es el que busca
    // `refrescarRelojes`. Escondido no molesta, pero deja un reloj
    // muerto en el DOM que el próximo grafo del hilo haría revivir.
    aviso.textContent = "";
    return;
  }
  const p = g.progreso || {};
  const reloj = cronometro(arranqueMasViejo(g.tasks || []));
  const enCurso = (g.tasks || []).filter((t) => t.estado === "corriendo");
  const nombres = enCurso.map((t) => t.titulo).join(" · ");
  aviso.innerHTML =
    `<span class="gav-punto"></span>`
    + `<span class="gav-txt">El plan está trabajando`
    + (reloj ? ` <b data-desde="${escape(arranqueMasViejo(g.tasks || []))}">`
       + `${escape(reloj)}</b>` : "")
    + ` — ${p.hechos || 0}/${p.total || 0} listas`
    + (nombres ? `. Ahora: ${escape(nombres)}` : "")
    + `.<br>Lo que escribas acá arranca un pedido <b>nuevo</b> en paralelo. `
    + `Mandá algo solo si querés corregir el rumbo.</span>`;
}

/**
 * El veredicto de la verificación de cierre del grafo.
 *
 * Existe por el requisito que originó la etapa: verificar sin que el
 * veredicto llegue a una pantalla es gastar un turno para nada. De 68
 * veredictos `off_plan` de los chats, 33 no se vieron nunca — construir
 * otra perspectiva ciega habría sido peor que no construirla.
 *
 * `complete` en verde, `needs_more`/`off_plan` en ámbar, `needs_human`
 * en rojo: los tres estados que cambian lo que el humano hace después.
 * Si la etapa no pudo correr, se dice —"no se verificó" no es lo mismo
 * que "está bien", y es justo la confusión que este panel tiene que
 * evitar.
 */
function pintarVeredicto(g) {
  const caja = $("#chat-grafo-veredicto");
  if (!caja) return;
  const v = (g && g.verificacion) || {};
  const verdict = v.verdict || "";
  if (!verdict && !v.error) {
    caja.hidden = true;
    caja.innerHTML = "";
    return;
  }
  const tono = verdict === "complete" ? "ok"
    : verdict === "needs_human" ? "err"
    : verdict ? "warn" : "dim";
  const etiqueta = verdict || "sin verificar";
  const tokens = (v.tokens_in != null || v.tokens_out != null)
    ? ` · ${(v.tokens_in || 0) + (v.tokens_out || 0)} tokens` : "";
  const nota = v.error
    ? `La verificación no pudo correr: ${v.error}`
    : (v.feedback || "");
  caja.hidden = false;
  caja.className = `chat-grafo-veredicto v${tono}`;
  caja.innerHTML =
    `<span class="badge ${tono}">${escape(etiqueta)}</span>`
    + `<span class="cgv-txt">${escape(nota)}`
    + `<small>${escape(v.modelo || "")}${escape(tokens)}</small></span>`;
}

function mostrar() {
  const btn = $("#chat-panel-grafo");
  if (btn) btn.hidden = false;
  const panel = $("#chat-grafo");
  // `hidden` solo lo maneja el humano (✕ / 🕸). Un refresco no reabre un
  // panel que acabás de cerrar: sería la UI discutiéndote.
  if (panel && panel.dataset.cerrado !== "1") panel.hidden = false;
  verTiradorGrafo(!!panel && !panel.hidden);
}

/** Muestra solo el bloque del grafo. Es el único modo que queda
 * desde la Etapa A: ya no hay lista de "modo lineal". */
function soloUno(_cual = "grafo") {
  // El argumento queda para no romper llamadas viejas, pero siempre
  // termina en el grafo: el botón ✕ y `pintar()` son lo único que
  // hay que coordinar hoy.
  verVista(estado?.vista || "grafo");
}

/** Cambia entre el dibujo y el resumen. Las dos leen el mismo grafo. */
function verVista(v) {
  if (estado) estado.vista = v;
  const lienzo = $("#chat-grafo-lienzo");
  const resumen = $("#chat-grafo-resumen");
  const zoom = $("#chat-grafo-zoom");
  if (lienzo) lienzo.hidden = v !== "grafo";
  if (resumen) resumen.hidden = v !== "resumen";
  if (zoom) zoom.hidden = v !== "grafo";
  for (const t of document.querySelectorAll(".chat-grafo-tab")) {
    const activa = t.dataset.vista === v;
    t.classList.toggle("activa", activa);
    t.setAttribute("aria-selected", activa ? "true" : "false");
  }
}

/**
 * Aplica el zoom al SVG ya pintado. `zoom === 0` es el comportamiento
 * de siempre (entrar en el panel); cualquier otro valor lo agranda por
 * encima del contenedor, y el `overflow: auto` del lienzo se encarga
 * del paneo. El ancho natural sale del viewBox, así que esto no
 * necesita saber nada de la geometría de `svgDelGrafo`.
 */
function aplicarZoom() {
  const svg = $("#chat-grafo-lienzo svg");
  const nivel = $("#chat-grafo-zoom-nivel");
  const z = estado?.zoom ?? 0;
  if (nivel) nivel.textContent = z ? `${Math.round(z * 100)}%` : "entra";
  if (!svg) return;
  const W = Number((svg.getAttribute("viewBox") || "").split(/\s+/)[2]) || 0;
  if (!W || !z) {
    svg.style.width = "100%";
    svg.style.maxWidth = `${W || 0}px`;
    return;
  }
  svg.style.width = `${Math.round(W * z)}px`;
  svg.style.maxWidth = "none";
}

function moverZoom(paso) {
  if (!estado) return;
  const i = ZOOMS.indexOf(estado.zoom);
  const j = Math.min(ZOOMS.length - 1, Math.max(0, (i < 0 ? 0 : i) + paso));
  estado.zoom = ZOOMS[j];
  aplicarZoom();
}

/**
 * El resumen: el plan por capas, en texto y con el estado de cada
 * tarea. Sale de los mismos `capas` que el dibujo — una capa es lo que
 * puede correr a la vez — así que las dos vistas no se pueden
 * contradecir. Clickear una tarea abre el mismo detalle que el nodo.
 */
function pintarResumen(g) {
  const caja = $("#chat-grafo-resumen");
  if (!caja) return;
  const porId = Object.fromEntries((g.tasks || []).map((t) => [t.id, t]));
  // Capas consecutivas de UNA tarea son una cadena, no tandas: un
  // "Después" por cada una repite la palabra sin decir nada nuevo. Mismo
  // criterio que `planificador.resumen`, que arma el mensaje del chat —
  // las dos superficies tienen que contar el plan igual.
  const grupos = [];
  for (const capa of (g.capas || [])) {
    const paralelo = capa.length > 1;
    const ult = grupos[grupos.length - 1];
    if (ult && !paralelo && !ult.paralelo) ult.ids.push(...capa);
    else grupos.push({ paralelo, ids: [...capa] });
  }
  let n = 0;
  const bloques = grupos.map(({ paralelo, ids }, i) => {
    const cab = i === 0
      ? (paralelo ? "Arrancan ahora" : "Arranca ahora")
      : paralelo ? `Después · ${ids.length} en paralelo`
      : ids.length > 1 ? "Después, en orden" : "Después";
    const filas = ids.map((id) => {
      const t = porId[id];
      if (!t) return "";
      n += 1;
      const sel = estado?.sel === id ? " sel" : "";
      const trozos = [];
      if (t.intentos > 1) {
        trozos.push(`<span>intento ${t.intentos}/${t.max_intentos}</span>`);
      }
      // El cronómetro de la tarea viva. `data-desde` lo deja al alcance
      // del tic de 1s (ver `refrescarRelojes`): sin eso el número solo
      // se movería cuando llega el poll.
      if (t.estado === "corriendo" && t.started_at) {
        trozos.push(`⏱ <span data-desde="${escape(t.started_at)}">`
          + `${escape(cronometro(t.started_at))}</span>`);
      }
      const nota = trozos.length
        ? ` <span class="gres-nota">${trozos.join(" · ")}</span>` : "";
      // `g-presupuesto` distingue "se acabó el presupuesto" (subí un
      // número, partí el trabajo) de "fallado" a secas (hay que
      // debuggear) — hoy se veían igual y un grafo de inventorydemo se quedó
      // fallado sin que nada dijera por qué (1/9).
      const presupuesto = t.presupuesto_agotado && !t.sustituido ? " g-presupuesto" : "";
      const etiqueta = etiquetaTarea(t);
      return `<li><button class="gres-item ${escape(t.sustituido ? "pendiente" : t.estado)}${presupuesto}${sel}" `
        + `type="button" data-id="${escape(id)}">`
        + `<span class="gres-n">${n}</span>`
        + `<span class="gres-txt">${escape(t.titulo)}${nota}</span>`
        + `<span class="gres-est">${escape(etiqueta)}</span></button></li>`;
    }).join("");
    return `<section class="gres-capa"><h6>${escape(cab)}</h6>`
      + `<ul>${filas}</ul></section>`;
  }).join("");
  caja.innerHTML = bloques
    || `<p class="chat-plan-vacio">El plan todavía no tiene tareas.</p>`;
  for (const el of caja.querySelectorAll(".gres-item")) {
    el.addEventListener("click", () => elegir(g, el.dataset.id));
  }
}

// =====================================================================
// Pintar
// =====================================================================

function pintar(g) {
  const panel = $("#chat-grafo");
  if (!panel) return;
  soloUno("grafo");

  // Grafos sintéticos (Etapa A): los nodos son turnos encadenados, no
  // un task_graph real en la DB. Los botones "parar"/"seguir" pegan a
  // /graphs/{id}/... y devolverían 404 si quedaran visibles.
  const sintetico = g.sintetico === true;
  if (estado) estado.graphId = sintetico ? null : (g.id || null);

  const p = g.progreso || {};
  // `estado_visible` (server.py `_grafo_publico`) distingue "esperando
  // que conteste un humano" de "activo" de verdad — un grafo puede
  // quedarse en esa espera días (medido 6/9) y el badge no puede seguir
  // diciendo "activo" como si algo estuviera corriendo. Cae a
  // `p.estado` para el grafo sintético, que no trae este campo.
  const evis = g.estado_visible || p.estado;
  const est = $("#chat-grafo-estado");
  if (est) {
    est.textContent = evis === "esperando_humano" ? "esperando humano"
      : (evis || "—");
    est.className = "badge " + (
      evis === "hecho" ? "ok" :
      evis === "fallado" ? "err" :
      (evis === "activo" || evis === "esperando_humano") ? "warn" : "dim");
  }
  // Verde lo que salió bien, rojo lo que no. `p.porcentaje` cuenta las
  // CERRADAS —y una fallada está cerrada—, así que usarlo para una sola
  // barra verde pintaba 75% de avance con una sola tarea hecha.
  const total = p.total || 0;
  const bien = $("#chat-grafo-barra-ok");
  const mal = $("#chat-grafo-barra-mal");
  const pct = (n) => (total ? `${Math.round(100 * n / total)}%` : "0%");
  if (bien) bien.style.width = pct(p.hechos || 0);
  if (mal) mal.style.width = pct((p.fallados || 0) + (p.bloqueados || 0));
  const conteo = $("#chat-grafo-conteo");
  if (conteo) conteo.textContent = resumenTexto(p, g.tasks || []);
  // El último grafo, para que el cronómetro re-dibuje sin re-pedir.
  if (estado) estado.g = g;
  if (g.corriendo === true) arrancarReloj(); else pararReloj();
  pintarAviso(g);
  pintarVeredicto(g);

  const lienzo = $("#chat-grafo-lienzo");
  if (lienzo) {
    lienzo.innerHTML = svgDelGrafo(g);
    for (const el of lienzo.querySelectorAll(".gnodo")) {
      el.addEventListener("click", () => elegir(g, el.dataset.id));
    }
    // El repintado tira el SVG viejo con su ancho puesto: el zoom hay
    // que volver a aplicarlo o cada poll (2.5s) lo resetearía solo.
    aplicarZoom();
  }
  pintarResumen(g);
  // Si había un nodo abierto, se repinta con los datos nuevos: mirar el
  // detalle de una tarea que está corriendo es exactamente cuando querés
  // que se actualice sola.
  if (estado?.sel) elegir(g, estado.sel, true);

  // Parar mientras hay algo que parar; retomar cuando el plan quedó a
  // medias y NADIE lo está corriendo. `g.corriendo` lo dice el relay: un
  // grafo `activo` puede estar tanto trabajando como cortado a mitad, y
  // desde la base los dos se ven igual. En un grafo sintético esos
  // botones no existen: no hay fila en la DB a la que pegarle.
  const vivo = g.corriendo === true;
  const terminado = p.estado === "hecho";
  const parar = $("#chat-grafo-parar");
  if (parar) parar.hidden = sintetico || !vivo;
  const seguir = $("#chat-grafo-seguir");
  if (seguir) seguir.hidden = sintetico || vivo || terminado;
}

/**
 * Cronómetro: cuánto lleva corriendo algo, en `MM:SS` (o `H:MM:SS`).
 * `""` si no arrancó o la fecha no parsea.
 *
 * Formato de cronómetro y no "hace 26m" a propósito: esto se refresca
 * cada segundo y tiene que VERSE moverse. Un texto que dice "26m"
 * durante sesenta segundos seguidos no distingue "está trabajando" de
 * "está colgado", que es exactamente la pregunta que tiene que contestar.
 *
 * Por qué existe: el panel se veía congelado durante los nodos largos —
 * el contador de hechas solo se mueve cuando una tarea CIERRA, así que
 * un nodo de 25 minutos dejaba "3/12 hechas" quieto. Pasó de verdad el
 * 24/8: el nodo de las capturas estaba vivo escribiendo PNGs y lo dimos
 * por muerto, y se mandaron mensajes al hilo mientras tanto.
 *
 * No es una prueba de vida —el nodo no reporta progreso, ver
 * `ejecutor_minimax`— pero dice hace cuánto arrancó, que es el dato con
 * el que uno decide si esperar o intervenir.
 */
export function cronometro(desde, ahora = Date.now()) {
  const t = Date.parse(desde || "");
  if (!Number.isFinite(t)) return "";
  const s = Math.max(0, Math.floor((ahora - t) / 1000));
  const ss = String(s % 60).padStart(2, "0");
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}:${ss}`;
  return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}:${ss}`;
}

/** El `started_at` del nodo vivo que arrancó PRIMERO. `""` si no hay. */
export function arranqueMasViejo(tasks = []) {
  return (tasks || [])
    .filter((t) => t.estado === "corriendo" && t.started_at)
    .map((t) => t.started_at).sort()[0] || "";
}

export function resumenTexto(p, tasks = []) {
  const partes = [`${p.hechos || 0}/${p.total || 0} hechas`];
  if (p.corriendo) {
    // Con dos nodos en paralelo manda el más viejo: es el que dice si
    // esto avanza o está trabado.
    const reloj = cronometro(arranqueMasViejo(tasks));
    partes.push(`${p.corriendo} corriendo${reloj ? ` · ⏱ ${reloj}` : ""}`);
  }
  if (p.esperando_humano) partes.push(`${p.esperando_humano} te espera`);
  if (p.fallados) partes.push(`${p.fallados} fallada${p.fallados > 1 ? "s" : ""}`);
  if (p.bloqueados) partes.push(`${p.bloqueados} bloqueada${p.bloqueados > 1 ? "s" : ""}`);
  if (p.sustituidos) partes.push(`${p.sustituidos} subdividida${p.sustituidos > 1 ? "s" : ""}`);
  return partes.join(" · ");
}

/**
 * El DAG como SVG. `export` por el test (chat-grafo.test.mjs): es la
 * única parte del módulo que no toca el DOM y la única donde un error
 * de geometría pasa desapercibido — un dibujo torcido no tira excepción.
 * Las filas son las capas del servidor; dentro de una
 * fila los nodos se reparten el ancho. Toda arista va de una fila a otra
 * posterior (lo garantiza `grafo.capas`), así que ninguna flecha
 * apunta hacia atrás y el dibujo se lee de arriba abajo.
 */
export function svgDelGrafo(g) {
  const capas = g.capas || [];
  if (!capas.length) return "";
  const porId = Object.fromEntries((g.tasks || []).map((t) => [t.id, t]));
  const anchoMax = Math.max(...capas.map((c) => c.length));
  const W = PAD * 2 + anchoMax * NODO_W + (anchoMax - 1) * GAP_X;
  const H = PAD * 2 + capas.length * NODO_H + (capas.length - 1) * GAP_Y;

  const pos = {};
  capas.forEach((fila, y) => {
    // Centrada: una fila de un nodo queda bajo el medio y no pegada a la
    // izquierda, que es lo que hace que una cadena se lea como cadena.
    const ancho = fila.length * NODO_W + (fila.length - 1) * GAP_X;
    const x0 = (W - ancho) / 2;
    fila.forEach((id, x) => {
      pos[id] = { x: x0 + x * (NODO_W + GAP_X), y: PAD + y * (NODO_H + GAP_Y) };
    });
  });

  const aristas = [];
  for (const t of g.tasks || []) {
    for (const d of t.deps || []) {
      if (!pos[d] || !pos[t.id]) continue;
      const a = pos[d], b = pos[t.id];
      const x1 = a.x + NODO_W / 2, y1 = a.y + NODO_H;
      const x2 = b.x + NODO_W / 2, y2 = b.y;
      const m = (y1 + y2) / 2;
      // Cúbica con los tiradores en la vertical: la curva sale hacia
      // abajo y entra desde arriba, así dos aristas que llegan al mismo
      // nodo no se superponen aunque vengan de costados opuestos.
      const cumplida = porId[d]?.estado === "hecho";
      aristas.push(
        `<path class="garista${cumplida ? " lista" : ""}" `
        + `d="M${x1} ${y1} C${x1} ${m} ${x2} ${m} ${x2} ${y2}"/>`);
    }
  }

  const nodos = (g.tasks || []).map((t) => {
    const p = pos[t.id];
    if (!p) return "";
    const sel = estado?.sel === t.id ? " sel" : "";
    // Dos líneas y no una: en 108 unidades entran ~16 caracteres, y un
    // título de tarea real ("Migrar las tablas de clientes") no entra
    // en 16. Con una sola línea el nodo decía "Migrar las tab…", que
    // obliga a hacer click para saber qué es cada cosa — y entonces el
    // dibujo no sirve para lo único que tiene que servir, que es
    // entender el plan de un vistazo.
    const lineas = envolver(t.titulo, CHARS_LINEA, 2);
    const y0 = p.y + NODO_H / 2 - (lineas.length - 1) * 6 + 4;
    const texto = lineas.map((ln, i) =>
      `<tspan x="${p.x + 8}" y="${y0 + i * 12}">${escape(ln)}</tspan>`).join("");
    const presupuesto = t.presupuesto_agotado && !t.sustituido ? " g-presupuesto" : "";
    const etiqueta = etiquetaTarea(t);
    return `<g class="gnodo ${escape(t.sustituido ? "pendiente" : t.estado)}${presupuesto}${sel}" data-id="${escape(t.id)}">`
      + `<title>${escape(t.titulo)} — ${escape(etiqueta)}</title>`
      + `<rect x="${p.x}" y="${p.y}" width="${NODO_W}" height="${NODO_H}" rx="5"/>`
      + `<text>${texto}</text></g>`;
  }).join("");

  // `width: 100%` + viewBox: el dibujo se adapta al ancho del panel sin
  // que haga falta recalcular nada al redimensionar.
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" `
    + `style="max-width:${W}px" role="img" `
    + `aria-label="Grafo del plan: ${(g.tasks || []).length} tareas">`
    + aristas.join("") + nodos + `</svg>`;
}

/**
 * Parte el título en hasta `max` líneas de `ancho` caracteres, cortando
 * por palabra. La última lleva `…` si quedó texto afuera.
 */
export function envolver(texto, ancho, max) {
  const palabras = String(texto || "").trim().split(/\s+/).filter(Boolean);
  const lineas = [];
  let actual = "";
  for (const p of palabras) {
    const cand = actual ? `${actual} ${p}` : p;
    if (cand.length <= ancho) { actual = cand; continue; }
    if (actual) lineas.push(actual);
    // Una palabra sola más larga que la línea (una ruta, un identificador)
    // se corta igual: dejarla entera desborda el nodo.
    actual = p.length > ancho ? p.slice(0, ancho) : p;
    if (lineas.length === max) break;
  }
  if (actual && lineas.length < max) lineas.push(actual);
  const sobra = palabras.join(" ").length
    > lineas.join(" ").length;
  if (sobra && lineas.length) {
    const u = lineas.length - 1;
    lineas[u] = lineas[u].slice(0, Math.max(1, ancho - 1)) + "…";
  }
  return lineas.length ? lineas : [""];
}

function recortar(s, n) {
  const t = String(s || "");
  return t.length > n ? t.slice(0, n - 1) + "…" : t;
}

function elegir(g, id, silencioso = false) {
  const t = (g.tasks || []).find((x) => x.id === id);
  const caja = $("#chat-grafo-detalle");
  if (!t || !caja) return;
  if (estado) estado.sel = id;
  if (!silencioso) {
    for (const el of document.querySelectorAll("#chat-grafo-lienzo .gnodo")) {
      el.classList.toggle("sel", el.dataset.id === id);
    }
  }
  const vivo = t.estado === "corriendo" && t.started_at;
  const etiqueta = etiquetaTarea(t);
  const filas = [`<h5>${escape(t.titulo)}</h5>`,
                 `<p class="muted">${escape(etiqueta)}`
                 + (vivo ? ` · ⏱ <span data-desde="${escape(t.started_at)}">`
                    + `${escape(cronometro(t.started_at))}</span>` : "")
                 + (t.intentos > 1 ? ` · intento ${t.intentos}/${t.max_intentos}` : "")
                 + `</p>`];
  if (t.detalle) filas.push(`<p>${escape(recortar(t.detalle, 400))}</p>`);
  if (t.sustituido) filas.push('<p class="muted">Tarea conservada como historial; sus subtareas cuentan en el avance.</p>');
  if (t.resultado) filas.push(`<p>${escape(recortar(t.resultado, 400))}</p>`);
  // "Se acabó el presupuesto" no es "hay un bug": el arreglo es subir
  // un número o partir el trabajo, no debuggear. Van en párrafos
  // separados para que la distinción se note sin leer el error entero.
  if (t.presupuesto_agotado && !t.sustituido) {
    filas.push(`<p class="gres-presupuesto-aviso">Cortó por agotar el `
      + `presupuesto (tool calls), no por un error del código. Subí el `
      + `límite o partí la tarea.</p>`);
  }
  // El error del intento anterior sigue en la fila mientras el nodo
  // reintenta: mostrarlo es la diferencia entre "esto ya falló una vez y
  // está reintentando" y "esto está muerto y nadie me avisó".
  if (t.error) {
    filas.push(`<p class="${t.sustituido ? "muted" : "gerror"}">${t.estado === "corriendo"
      ? "Intento anterior: " : ""}${escape(recortar(t.error, 300))}</p>`);
  }
  caja.innerHTML = filas.join("");
  caja.hidden = false;
}

// =====================================================================
// Wiring
// =====================================================================

export function wireGrafoPanel() {
  const panel = $("#chat-grafo");
  $("#chat-grafo-cerrar")?.addEventListener("click", () => {
    if (panel) { panel.hidden = true; panel.dataset.cerrado = "1"; }
    verTiradorGrafo(false);
    $("#chat-panel-grafo")?.focus();
  });
  $("#chat-panel-grafo")?.addEventListener("click", () => {
    if (panel) { panel.hidden = false; panel.dataset.cerrado = "0"; }
    verTiradorGrafo(true);
    $("#chat-grafo-cerrar")?.focus();
  });
  $("#chat-grafo-parar")?.addEventListener("click", pararElPlan);
  $("#chat-grafo-seguir")?.addEventListener("click", retomarElPlan);

  for (const t of document.querySelectorAll(".chat-grafo-tab")) {
    t.addEventListener("click", () => verVista(t.dataset.vista));
  }
  $("#chat-grafo-zoom-mas")?.addEventListener("click", () => moverZoom(+1));
  $("#chat-grafo-zoom-menos")?.addEventListener("click", () => moverZoom(-1));
  $("#chat-grafo-zoom-fit")?.addEventListener("click", () => {
    if (estado) estado.zoom = 0;
    aplicarZoom();
  });
  $("#chat-grafo-ancho")?.addEventListener("click", (ev) => {
    if (!panel) return;
    const ancho = panel.classList.toggle("ancho");
    ev.currentTarget.setAttribute("aria-pressed", ancho ? "true" : "false");
    ev.currentTarget.title = ancho ? "Achicar el panel" : "Ensanchar el panel";
    // El SVG en modo "entra" se mide contra el contenedor, así que al
    // cambiar el ancho hay que recalcularlo.
    aplicarZoom();
  });
}

async function retomarElPlan() {
  const gid = estado?.graphId;
  if (!gid) return;
  try {
    await apiRoot(`/graphs/${encodeURIComponent(gid)}/resume`,
                  { method: "POST" });
    toast("Retomando el plan", "ok");
    await refrescar();
  } catch (e) {
    toast("No pude retomar el plan: " + e.message, "err");
  }
}

async function pararElPlan() {
  const gid = estado?.graphId;
  if (!gid) return;
  const ok = await confirmModal({
    title: "¿Parar el plan?",
    body: "Se cortan las tareas en vuelo y se sueltan los archivos que "
        + "tengan tomados. Lo que ya se hizo queda hecho; lo que estaba "
        + "a medias no se deshace.",
    confirmText: "⏹ Parar el plan",
    danger: true,
  });
  if (!ok) return;
  try {
    await apiRoot(`/graphs/${encodeURIComponent(gid)}/cancel`,
                  { method: "POST" });
    toast("Plan cortado", "ok");
    await refrescar();
  } catch (e) {
    toast("No pude parar el plan: " + e.message, "err");
  }
}
