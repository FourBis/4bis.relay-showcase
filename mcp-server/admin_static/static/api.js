// 4bis.relay admin UI — helpers base: DOM, fetch con timeout, escape.
// ES module plano (sin build). Los demás módulos importan de acá.

export const $ = (s) => document.querySelector(s);
export const $$ = (s) => Array.from(document.querySelectorAll(s));

// Wire de un handler tolerante a que el elemento no exista. `$(sel).onclick =`
// tira TypeError cuando el id no está en el HTML y ABORTA el init entero:
// todos los botones wireados DESPUÉS quedan muertos, sin error visible.
// Pasó dos veces con `#chat-panel-steer` (JS mergeado antes que su markup),
// la segunda se llevó puestos VS Code, Compactar y Cerrar. El warn deja el
// rastro en consola en vez de matar el resto del tab.
export function on(sel, ev, fn) {
  const el = $(sel);
  if (!el) { console.warn(`[admin-ui] no existe ${sel}; handler no wireado`); return null; }
  el.addEventListener(ev, fn);
  return el;
}

// Mismo problema que `on()`, pero para el botón estático de un modal
// (footer definido una vez en index.html, no el body que `modalLoad`
// reemplaza en cada apertura). Esos botones se re-wirean cada vez que el
// modal abre — `wireExpertForm`, `openStatusModal`, etc. corren de nuevo
// por cada apertura — así que `addEventListener` ahí ACUMULA: abrir el
// modal 3 veces deja 3 handlers y el click dispara la acción 3 veces
// (2026-09-04: era exactamente el riesgo de migrar `#expert-run`,
// `#new-save`, `#cbm-reindex`, `#github-refresh`, `#status-modal-*` y
// `#skill-modal-*` a `on()` sin más — el último además lo comparten
// `openDraftModal` y `openInstalledModal`, dos modales distintos sobre
// el mismo botón). `.onclick =` REEMPLAZA en vez de acumular, que es el
// comportamiento correcto para un botón de un solo uso — `onClick` es
// exactamente eso con la guarda de `on()`.
//
// Regla para elegir: un click en un botón que vive en el HTML estático
// (el footer de un modal, un botón de la sidebar) → `onClick`. Un
// `input`/`change`/`keydown`, o cualquier lugar donde de verdad haga
// falta más de un listener a la vez → `on()`. Un botón de una fila de
// tabla reconstruida por `.forEach` no es ninguno de los dos casos: el
// elemento sale del propio render, nunca es null, así que no hace falta
// ninguna guarda ahí.
export function onClick(sel, fn) {
  const el = $(sel);
  if (!el) { console.warn(`[admin-ui] no existe ${sel}; handler no wireado`); return null; }
  el.onclick = fn;
  return el;
}

// Debug flag: con ?debug=1 en la URL loggea a consola cada paso.
// Útil para diagnosticar modales/fetches sin abrir DevTools a ciegas.
export const DEBUG = /[?&]debug=1/.test(location.href);

// Timeout default para todos los fetches. Sin esto, si el server cuelga
// (subprocess git, base lock, etc.), la UI queda esperando para siempre.
export const FETCH_TIMEOUT_MS = 15_000;

export function _dbg(...args) { if (DEBUG) console.log("[admin-ui]", ...args); }

// Fetch JSON con timeout vía AbortController.
async function _fetch_json(url, opts = {}, timeoutMs = FETCH_TIMEOUT_MS) {
  const controller = new AbortController();
  const tid = setTimeout(() => controller.abort(), timeoutMs);
  const t0 = performance.now();
  // 2026-07-23: si el caller pasa `body` como un object, fetch()
  // manda "[object Object]" porque no serializa solo. Antes lo
  // arreglaba cada callsite con JSON.stringify + headers, pero
  // un callsite se olvidó y mandó body={slug}
  // tal cual → handler devolvía 400 "slug requerido" porque
  // request.json() levantaba JSONDecodeError o leía un body vacío.
  // Fix: si body es object y no hay Content-Type, lo serializamos
  // nosotros y agregamos el header. Body como string (ej: FormData
  // o ya-stringified JSON) pasa tal cual. Es el patrón estándar de
  // cualquier helper fetch minimalista (igual a jQuery, axios, etc.).
  let finalOpts = opts;
  if (opts.body && typeof opts.body === "object"
      && !(opts.body instanceof FormData)
      && !(opts.body instanceof ArrayBuffer)
      && !(opts.body instanceof Blob)
      && !(opts.body instanceof URLSearchParams)) {
    finalOpts = {
      ...opts,
      body: JSON.stringify(opts.body),
      headers: {
        "Content-Type": "application/json",
        ...(opts.headers || {}),
      },
    };
  }
  try {
    _dbg("fetch", url, { timeoutMs });
    const r = await fetch(url, { ...finalOpts, signal: controller.signal });
    if (!r.ok) {
      const txt = await r.text().catch(() => "");
      // El relay devuelve {"error": "..."} en JSON. Extraemos ese mensaje
      // para no mostrarle al usuario un blob crudo '422: {"error":"..."}'.
      // Adjuntamos .status y .body para que el caller pueda ramificar
      // (p.ej. distinguir 409 conversación-abierta de 422 tree-sucio).
      let detail = txt;
      try {
        const j = JSON.parse(txt);
        // `message` primero: la convención del relay es `error` =
        // código corto y `message` = la frase accionable. Mostrar
        // "forbidden" cuando hay un texto que dice qué hacer es
        // tirar a la basura la mitad útil de la respuesta.
        if (j && typeof j.message === "string") detail = j.message;
        else if (j && typeof j.error === "string") detail = j.error;
      } catch { /* body no-JSON: usamos el texto crudo */ }
      const err = new Error(
        `${r.status}${detail ? ": " + detail.slice(0, 300) : ""}`);
      err.status = r.status;
      err.body = txt;
      throw err;
    }
    const data = await r.json();
    _dbg("fetch ok", url, "en", Math.round(performance.now() - t0), "ms");
    return data;
  } catch (e) {
    if (e.name === "AbortError") {
      _dbg("fetch ABORT", url, "tras", Math.round(performance.now() - t0), "ms");
      throw new Error(`timeout después de ${timeoutMs / 1000}s — `
        + `el server no respondió. Refresca la página o revisa el relay.`);
    }
    _dbg("fetch error", url, e.message);
    throw e;
  } finally {
    clearTimeout(tid);
  }
}

// Endpoints bajo /admin/api/*. El tercer parámetro permite subir el
// timeout para endpoints lentos (git diff en repos grandes, etc.).
export async function api(path, opts, timeoutMs) {
  return _fetch_json("/admin/api/" + path, opts || {}, timeoutMs);
}

// Endpoints del relay fuera de /admin/api (mismo host/puerto).
export async function apiRoot(path, opts, timeoutMs) {
  return _fetch_json(path, opts || {}, timeoutMs);
}

export function escape(s) {
  return String(s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

export function formatElapsed(startedAt) {
  if (!startedAt) return "—";
  const t = Date.parse(startedAt.replace(" ", "T") + "Z");
  if (Number.isNaN(t)) return escape(startedAt);
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  const h = Math.floor(s / 3600);
  return `${h}h ${Math.floor((s % 3600) / 60)}m`;
}

// Helper compartido: abre VS Code con el repo del proyecto.
// Iter 10.0: usado por tab-projects.js (botón por fila) Y por tab-chats.js
// (botón en el header del chat cuando target tiene repo_path).
// Devuelve {ok, error?}. No tira excepción (el toast lo maneja el caller).
export async function openProjectInVSCode(slug) {
  try {
    const r = await api(`projects/${slug}/open-vscode`, { method: "POST" });
    if (r.error) return { ok: false, error: r.error };
    return { ok: true };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

// ---- helpers UI 2026-07-20 (refresh) ----

// Números compactos para tokens/counts: 1234 → "1,2K", 8500000 → "8,5M".
export function fmtNum(n) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  n = Number(n);
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1).replace(".", ",") + "M";
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1).replace(".", ",") + "K";
  return String(n);
}

// git remote (https o ssh) → URL web navegable. null si no se puede.
//   git@github.com:Org/repo.git  → https://github.com/Org/repo
//   https://github.com/Org/repo.git → https://github.com/Org/repo
export function gitWebUrl(remote) {
  if (!remote) return null;
  const raw = String(remote).trim();
  if (!raw || /[\u0000-\u001f\u007f\s]/.test(raw)) return null;

  let u;
  if (!raw.includes("://")) {
    if (/^[a-z][a-z\d+.-]*:/i.test(raw)) return null;
    const scp = raw.match(/^(?:[^@/:]+@)?([^/:]+):(.+)$/);
    if (!scp) return null;
    try { u = new URL(`ssh://${scp[1]}/${scp[2]}`); } catch { return null; }
  } else {
    try { u = new URL(raw); } catch { return null; }
  }
  if (!new Set(["http:", "https:", "ssh:"]).has(u.protocol)
      || u.password || u.search || u.hash || !u.hostname
      || (u.protocol === "ssh:" && u.port)) {
    return null;
  }
  const path = u.pathname.replace(/\.git$/, "");
  if (!path || path === "/") return null;
  const scheme = u.protocol === "ssh:" ? "https:" : u.protocol;
  const host = u.protocol === "ssh:" ? u.hostname : u.host;
  return `${scheme}//${host}${path}`;
}

// Duración en ms → "1,2s" / "45s" / "3m 10s" / "2h 49m".
export function fmtDuration(ms) {
  if (ms == null) return "—";
  const s = ms / 1000;
  if (s < 10) return s.toFixed(1).replace(".", ",") + "s";
  if (s < 60) return Math.round(s) + "s";
  const m = Math.floor(s / 60);
  // Pasada la hora los segundos son ruido: el tiempo de uso de una
  // conversación son horas y "169m 24s" no se lee (2026-08-31).
  if (m >= 60) return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
  return `${m}m ${Math.round(s % 60)}s`;
}
