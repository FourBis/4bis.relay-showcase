// 4bis.relay admin UI — componentes compartidos: badges, stat cells,
// toasts y el modal manager.
//
// MODAL MANAGER — diseño post-bug 2026-07 (modales pegados en "cargando"):
//   1. Display controlado SOLO por la clase .open (nunca [hidden], que una
//      regla CSS puede pisar sin que nadie se entere).
//   2. Generation counter por modal: cada open/close incrementa la gen.
//      Un fetch que vuelve tarde (zombie) compara su gen y se descarta.
//   3. Watchdog: si a los 8s el body sigue en "cargando…", se muestra un
//      mensaje accionable en vez de un cuelgue mudo.

import { $, $$, _dbg, escape } from "./api.js";

// ---------- badges / celdas ----------

export function statusBadge(s) {
  if (!s) return "—";
  const cls = s === "ok" ? "ok" : s === "running" ? "warn" : "err";
  return `<span class="badge ${cls}">${escape(s)}</span>`;
}

// Celda de stat para los grids (Estado, Config→Efectivo ahora).
// vCls: utilidades extra para el valor (ej: "!text-red-400").
export function statCell(v, k, vCls = "") {
  return `<div class="stat">
    <div class="stat-v ${vCls}">${v}</div>
    <div class="stat-k">${k}</div>
  </div>`;
}

// ---------- estados: vacio y carga ----------
//
// Antes de esto cada tab escribia su propio `<td colspan class="empty">`:
// 21 copias con 21 textos distintos, ninguna con salida, y 11 de los 20
// modulos directamente sin estado vacio. Y el unico skeleton de carga
// vivia en tab-projects, asi que en el resto una tabla muda podia
// significar "cargando", "no hay nada" o "el server se colgo".

/**
 * Panel de estado vacio. `sub` explica la CAUSA y `action` (HTML de un
 * boton) da la salida: un vacio sin salida deja al usuario sin saber si
 * esta roto o si le falta hacer algo.
 * `icon` es HTML (un <svg>), va oculto al lector de pantalla.
 */
export function emptyState({ title, sub = "", icon = "", action = "" }) {
  return `<div class="empty-state">
    ${icon ? `<div class="empty-state-icon" aria-hidden="true">${icon}</div>` : ""}
    <p class="empty-state-title">${escape(title)}</p>
    ${sub ? `<p class="empty-state-sub">${escape(sub)}</p>` : ""}
    ${action ? `<div class="empty-state-action">${action}</div>` : ""}
  </div>`;
}

/**
 * El mismo vacio pero como fila de tabla, que es el caso de los 21
 * `<td colspan>`. Con un string arma la version de una linea (`.empty`,
 * lo que ya habia); con un objeto arma el panel completo.
 */
export function emptyRow(cols, opts) {
  const body = typeof opts === "string"
    ? `<div class="empty">${escape(opts)}</div>`
    : emptyState(opts);
  return `<tr><td colspan="${cols}" class="!p-0">${body}</td></tr>`;
}

/**
 * Filas fantasma mientras carga. Anchos variados a proposito: cuatro
 * barras identicas se leen como una grilla vacia, no como carga.
 */
export function skeletonRows(cols, rows = 4) {
  const widths = ["w-1/3", "w-1/2", "w-2/5", "w-1/4"];
  return Array.from({ length: rows }, (_, i) =>
    `<tr class="skeleton-row"><td colspan="${cols}" class="px-3 py-3">
      <span class="skeleton ${widths[i % widths.length]}"></span>
    </td></tr>`).join("");
}

// ---------- campos de formulario ----------

/**
 * Campo: label -> control -> hint/error, SIEMPRE en ese orden (Config
 * mezclaba tres variantes distintas en la misma pantalla).
 * `control` y `hint` son HTML crudo (el hint suele llevar chips <code>
 * con el nombre de la env var); el resto se escapa.
 * Pasar `error` marca el campo invalido y tapa el hint — el error es
 * mas urgente que la ayuda.
 */
export function field({ label, control, hint = "", error = "",
                        id = "", required = false }) {
  const forAttr = id ? ` for="${escape(id)}"` : "";
  const tail = error ? `<p class="field-error">${escape(error)}</p>`
             : hint ? `<p class="field-hint">${hint}</p>` : "";
  return `<div class="field${error ? " invalid" : ""}">
    <label class="field-label"${forAttr}>${escape(label)}${
      required ? `<span class="req" title="requerido">*</span>` : ""}</label>
    ${control}
    ${tail}
  </div>`;
}

/**
 * Agrupador de campos: una card con titulo y bajada.
 * Por default mete el cuerpo en `.form-grid` (dos columnas desde sm).
 * `grid: false` para un cuerpo que se arma solo.
 *
 * La grilla la pone ESTE helper y no el caller a proposito: Tailwind
 * purga de `@layer components` toda clase que no aparezca en un archivo
 * escaneado, asi que una clase que solo escriben los callers no existe
 * en el bundle hasta que alguien la usa — y desaparece de nuevo cuando
 * ese caller se va. Viviendo aca, viaja con el componente.
 */
export function formSection({ title, sub = "", body, actions = "", grid = true }) {
  return `<section class="form-section">
    <header class="form-section-head">
      <h3 class="form-section-title">${escape(title)}</h3>
      ${sub ? `<p class="form-section-sub">${sub}</p>` : ""}
    </header>
    ${grid ? `<div class="form-grid">${body}</div>` : body}
    ${actions ? `<div class="field-actions">${actions}</div>` : ""}
  </section>`;
}

// ---------- toasts (feedback no bloqueante; reemplazan alert()) ----------

const TOAST_STYLES = {
  info: "border-zinc-700 bg-zinc-800 text-zinc-100",
  ok: "border-emerald-600/50 bg-emerald-950 text-emerald-200",
  warn: "border-amber-600/50 bg-amber-950 text-amber-200",
  err: "border-red-600/50 bg-red-950 text-red-200",
};

export function toast(msg, kind = "info", ms = 4500) {
  _dbg("toast", kind, msg);
  const root = $("#toast-root");
  if (!root) { _alertFallback(msg); return; }  // fallback si el DOM no está
  const el = document.createElement("div");
  el.className = "pointer-events-auto rounded-lg border px-4 py-2.5 text-sm "
    + "shadow-lg whitespace-pre-wrap break-words "
    + (TOAST_STYLES[kind] || TOAST_STYLES.info);
  el.textContent = msg;
  el.title = "click para cerrar";
  el.addEventListener("click", () => el.remove());
  root.appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .3s";
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 300);
  }, ms);
}

// Fallback del toast (no debería pasar en producción, pero si el JS
// falló y arrancó sin #toast-root, mejor un alert nativo que nada).
function _alertFallback(msg) {
  if (typeof console !== "undefined") console.warn("[toast-root missing]", msg);
  alert(msg);
}

// ---------- alert/confirm como modal (reemplazo de window.alert/confirm) ----------
//
// Por qué no usar SweetAlert: ya tenemos un modal manager (mismo .open
// class, generación counter, watchdog 8s, ESC/overlay click para cerrar,
// etc.). Sumar una librería más para dos pantallas es over-kill.
// El estilo encaja con el resto de la UI (mismo zinc-900, mismo border,
// mismo focus ring emerald) sin meter CSS extra.

const _promiseResolvers = new Map();

/**
 * `await confirmModal({ title, body, confirmText?, cancelText?, danger? })`
 * → Promise<boolean>. Devuelve true si confirma, false si cancela.
 *
 * `danger: true` → el botón de confirmar es rojo (para borrar/etc).
 * `body` puede ser HTML (raw) o un string plano.
 */
export function confirmModal(opts) {
  const {
    title = "¿Confirmar?",
    body = "",
    confirmText = "Confirmar",
    cancelText = "Cancelar",
    danger = false,
  } = opts || {};
  return new Promise((resolve) => {
    const id = "confirm-modal";
    const titleEl = $("#" + id + "-title");
    const bodyEl = $("#" + id + "-body");
    const okBtn = $("#" + id + "-ok");
    const cancelBtn = $("#" + id + "-cancel");
    if (!titleEl || !bodyEl || !okBtn || !cancelBtn) {
      // Fallback nativo si el DOM no está (no debería pasar).
      resolve(window.confirm(typeof body === "string" ? body : title));
      return;
    }
    titleEl.textContent = title;
    bodyEl.innerHTML = typeof body === "string" ? escape(body) : body;
    okBtn.textContent = confirmText;
    cancelBtn.textContent = cancelText;
    okBtn.className = danger ? "btn danger" : "btn btn-primary";
    _promiseResolvers.set(id, resolve);
    openModal(id);
    // Foco en el botón cancel (no en OK) — la convención es que OK
    // necesita Enter explícito; cancelar es el default seguro.
    setTimeout(() => cancelBtn.focus(), 50);
  });
}

export function alertModal(opts) {
  const {
    title = "Aviso",
    body = "",
    okText = "OK",
  } = opts || {};
  return new Promise((resolve) => {
    const id = "alert-modal";
    const titleEl = $("#" + id + "-title");
    const bodyEl = $("#" + id + "-body");
    const okBtn = $("#" + id + "-ok");
    if (!titleEl || !bodyEl || !okBtn) {
      alert(typeof body === "string" ? body : title);
      resolve();
      return;
    }
    titleEl.textContent = title;
    bodyEl.innerHTML = typeof body === "string" ? escape(body) : body;
    okBtn.textContent = okText;
    _promiseResolvers.set(id, resolve);
    openModal(id);
    setTimeout(() => okBtn.focus(), 50);
  });
}

// ---------- modal manager ----------

const _gen = Object.create(null);

export function openModal(id) {
  _gen[id] = (_gen[id] || 0) + 1;
  $("#" + id).classList.add("open");
  _dbg("modal open", id, "gen", _gen[id]);
  return _gen[id];
}

export function closeModal(id) {
  _gen[id] = (_gen[id] || 0) + 1;  // invalida cualquier fetch en vuelo
  $("#" + id).classList.remove("open");
  _dbg("modal close", id);
}

export function isModalOpen(id) {
  return $("#" + id).classList.contains("open");
}

function isModalCurrent(id, gen) {
  return isModalOpen(id) && _gen[id] === gen;
}

export const LOADING_HTML = `<p class="muted modal-loading">cargando…</p>`;

export function errorHtml(e, extra = "") {
  return `<p class="fail text-sm">error: ${escape(e.message || e)}</p>${extra}`;
}

// ---- Sidepanel lateral (UI 2026-07-20) ----
// Variante del modal genérico que mantiene el contexto del tab detrás.
// Usado por tab-projects.openAdminModal: el admin de un proyecto es
// lectura + edición liviana, no necesita tapar la pantalla entera.
let _sidepanelGen = 0;

export function openSidePanel(title) {
  _sidepanelGen++;
  const sp = $("#admin-sidepanel");
  const bd = $("#admin-sidepanel-backdrop");
  if (!sp || !bd) return _sidepanelGen;
  $("#admin-sidepanel-title").textContent = title || "Administrar";
  $("#admin-sidepanel-body").innerHTML = LOADING_HTML;
  sp.classList.remove("hidden");
  bd.classList.remove("hidden");
  // anim: aparece desde la derecha si reduced-motion lo permite
  if (!matchMedia("(prefers-reduced-motion: reduce)").matches) {
    sp.style.transform = "translateX(100%)";
    requestAnimationFrame(() => {
      sp.style.transition = "transform .18s ease-out";
      sp.style.transform = "translateX(0)";
    });
  }
  _dbg("sidepanel open gen", _sidepanelGen);
  return _sidepanelGen;
}

export function closeSidePanel() {
  _sidepanelGen++;
  $("#admin-sidepanel")?.classList.add("hidden");
  $("#admin-sidepanel-backdrop")?.classList.add("hidden");
  $("#admin-sidepanel").style.transform = "";
  $("#admin-sidepanel").style.transition = "";
  _dbg("sidepanel close");
}

export function setSidePanelBody(html) {
  $("#admin-sidepanel-body").innerHTML = html;
}

export function sidepanelGen() { return _sidepanelGen; }

export function isSidePanelOpen() {
  return !!$("#admin-sidepanel") && !$("#admin-sidepanel").classList.contains("hidden");
}

export function isSidePanelCurrent(gen) {
  return isSidePanelOpen() && _sidepanelGen === gen;
}

// Wire del backdrop + botón ✕ — se monta una sola vez al boot.
export function wireSidePanel() {
  document.querySelectorAll("[data-sidepanel-close]").forEach((el) =>
    el.addEventListener("click", closeSidePanel));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isSidePanelOpen()) closeSidePanel();
  });
}

// Ciclo completo de un modal con carga async:
//   modalLoad({ id, title, loader, render, after, watchdogDetail })
// - loader(): promesa con los datos (fetch)
// - render(data): HTML del body
// - after(data): opcional, para wirear botones del footer post-render
// Convención de ids en el HTML: #{id}, #{id}-body, #{id}-title, #{id}-msg.
export async function modalLoad({ id, title, loader, render, after,
                                  watchdogMs = 8000, watchdogDetail = "" }) {
  const body = $("#" + id + "-body");
  const titleEl = $("#" + id + "-title");
  const msgEl = $("#" + id + "-msg");
  if (titleEl && title != null) titleEl.textContent = title;
  if (msgEl) msgEl.textContent = "";
  const gen = openModal(id);
  body.innerHTML = LOADING_HTML;
  const wd = setTimeout(() => {
    if (isModalCurrent(id, gen) && body.querySelector(".modal-loading")) {
      _dbg("modal WATCHDOG disparado", id);
      body.innerHTML =
        `<p class="text-sm text-amber-400">⏱ el server tarda más de lo normal…</p>
         <p class="muted mt-2">${watchdogDetail || `La request sigue corriendo
         (timeout duro a los 15–30s). Puedes cerrar con ✕ o Esc y reintentar.`}</p>`;
    }
  }, watchdogMs);
  try {
    const data = await loader();
    clearTimeout(wd);
    if (!isModalCurrent(id, gen)) { _dbg("modal stale, descarto response", id); return; }
    body.innerHTML = render(data);
    if (after) after(data);
    _dbg("modal render ok", id);
  } catch (e) {
    clearTimeout(wd);
    if (!isModalCurrent(id, gen)) { _dbg("modal stale (error), descarto", id); return; }
    _dbg("modal error", id, e.message);
    body.innerHTML = errorHtml(e,
      `<p class="muted mt-2">Si el server está colgado, cierra con ✕ o Esc,
       haz hard refresh (Ctrl+Shift+R) y reintenta.</p>`);
  }
}

// Wiring genérico: overlay click, botones [data-close] y Esc global.
// Se llama una vez desde main.js.
export function wireModals() {
  $$(".modal").forEach((m) => {
    // 2026-08-01: el click en el overlay YA NO cierra. Un misclick al
    // costado del card tiraba el modal entero — y con él un form a medio
    // llenar o un panel que tardó 3s en cargar. Cerrar es explícito: ✕
    // o Esc. Los confirm/alert siguen resolviendo como "cancelado" por
    // esos dos caminos, así que nada queda colgado esperando.
    m.querySelectorAll("[data-close]").forEach((b) =>
      b.addEventListener("click", () => {
        closeModal(m.id);
        _resolvePending(m.id, false);
      }));
  });
  // confirm/alert modales: botones específicos
  const okC = $("#confirm-modal-ok");
  if (okC) okC.addEventListener("click", () => {
    const id = "confirm-modal";
    closeModal(id);
    _resolvePending(id, true);
  });
  const okA = $("#alert-modal-ok");
  if (okA) okA.addEventListener("click", () => {
    const id = "alert-modal";
    closeModal(id);
    _resolvePending(id, true);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      $$(".modal.open").forEach((m) => {
        _dbg("modal Esc", m.id);
        closeModal(m.id);
        _resolvePending(m.id, false);
      });
    }
  });

  // Focus trap (#10): Tab / Shift+Tab ciclan dentro del modal abierto
  // (el último en el DOM si hay varios); antes el foco se escapaba a
  // la página de fondo.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Tab") return;
    const open = $$(".modal.open");
    if (!open.length) return;
    const m = open[open.length - 1];
    const focusables = [...m.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), '
      + 'textarea:not([disabled]), select:not([disabled]), '
      + '[tabindex]:not([tabindex="-1"])')]
      .filter((el) => el.offsetParent !== null);
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault(); first.focus();
    } else if (!m.contains(document.activeElement)) {
      e.preventDefault(); first.focus();
    }
  });
}

function _resolvePending(id, value) {
  const r = _promiseResolvers.get(id);
  if (r) {
    _promiseResolvers.delete(id);
    r(value);
  }
}
