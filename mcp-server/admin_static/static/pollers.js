// Poller registry (2026-07-28): UN solo intervalo para toda la app.
//
// Antes había 8 setInterval independientes (watcher, status, logs,
// reindex x2, mcp job, skills browse, banner nocturno): todos corrían
// con la pestaña oculta y los de tabs corrían aunque estuvieras en
// otro tab. Acá centralizamos:
//
//   - pausa global mientras document.hidden (pestaña oculta)
//   - con { tabId }, el callback solo dispara si ese tab es el activo
//   - un callback que falla no frena a los demás
//
// Devuelve una función unregister (reemplaza a clearInterval).
//
// ponytail: el tick fijo de 250ms cuantiza los períodos (uno de 800ms
// dispara cada ~800-1050ms reales). Para polling de UI sobra; si algún
// día hace falta precisión, timeouts individuales por entrada.
// Test: mcp-server/tests/poller-registry.test.mjs (node, sin deps).
const TICK_MS = 250;
const _pollers = new Set();
let _timer = null;
let _getActiveTab = () => null;

/** Le dice al registry cómo saber qué tab está activo (lo llama main.js). */
export function initPollers(getActiveTab) {
  _getActiveTab = getActiveTab;
}

/**
 * Registra cb para correr cada `ms`. Con { tabId } solo dispara cuando
 * ese tab es el activo. Devuelve unregister().
 */
export function registerPoller(cb, ms, { tabId = null } = {}) {
  const entry = { cb, ms, tabId, next: Date.now() + ms };
  _pollers.add(entry);
  if (!_timer) _timer = setInterval(_tick, TICK_MS);
  return () => { _pollers.delete(entry); };
}

function _tick() {
  if (document.hidden) return;
  const now = Date.now();
  const active = _getActiveTab();
  // Copia: un callback puede des-registrarse a sí mismo durante el tick.
  for (const p of [..._pollers]) {
    if (now < p.next) continue;
    if (p.tabId && !(active instanceof Set ? active.has(p.tabId) : p.tabId === active)) continue;
    p.next = now + p.ms;
    // Un poller roto (throw sync o reject async) no frena al resto,
    // y cb corre sync como en el setInterval clásico.
    try { Promise.resolve(p.cb()).catch(() => {}); } catch (_) {}
  }
}
