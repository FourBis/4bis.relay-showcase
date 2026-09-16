// Watcher global de procesos (UI 2026-07-20).
//
// Un solo poller para toda la app (10s): alimenta los vitales del
// topbar (runs corriendo, tokens de hoy) y emite toasts de ciclo de
// vida de runs: ▶ arrancó / ✔ terminó / ✖ error. Los endpoints que
// pega son sqlite-only (<10ms desde el perf pass del 19/7) — el poll
// es gratis.
//
// Diseño anti-ruido:
//   - El primer poll siembra el baseline SIN toasts (si abres la UI
//     con 3 runs vivos, no te comes 3 toasts).
//   - Un run que desaparece del set "running" se resuelve con UN
//     fetch a /chats/{id} para saber cómo terminó.
//   - Click en el toast navega al tab pertinente.

import { $, apiRoot, fmtNum, _dbg } from "./api.js";
import { toast } from "./ui.js";
import { registerPoller } from "./pollers.js";

const POLL_MS = 10_000;
let _known = null;          // Map<chat_id, {project, source}> | null = sin baseline
let _timer = null;

function _showTab(name) {
  if (name === "chats") name = "chat";
  const btn = document.querySelector(`.tab[data-tab="${name}"]`);
  if (btn) btn.click();
}

function _toastClickable(msg, kind, tab) {
  // toast() devuelve void; agregamos click-to-navigate armando el
  // texto con una indicación. El click cierra el toast (ui.js) — acá
  // colgamos la navegación en el último toast insertado.
  toast(msg, kind, 7000);
  const root = $("#toast-root");
  const el = root && root.lastElementChild;
  if (el && tab) {
    el.addEventListener("click", () => _showTab(tab), { once: true });
  }
}

async function _resolveFinished(id, info) {
  try {
    const chat = await apiRoot(`/chats/${id}`);
    const dur = chat.duration_ms != null
      ? ` · ${Math.round(chat.duration_ms / 1000)}s` : "";
    const tok = chat.tokens_in != null
      ? ` · ${fmtNum(chat.tokens_in)} tok` : "";
    if (chat.status === "ok") {
      _toastClickable(
        `✔ ${info.project} terminó${dur}${tok}`, "ok", "chats");
    } else if (chat.status === "cancelled") {
      _toastClickable(`⊘ ${info.project} cancelado`, "warn", "chats");
    } else {
      _toastClickable(
        `✖ ${info.project} falló: ${(chat.error || "?").slice(0, 80)}`,
        "err", "chats");
    }
  } catch (e) {
    _dbg("watcher: no pude resolver el final de", id, e.message);
  }
}

async function _tick() {
  let running = [];
  try {
    const r = await apiRoot("/chats?status=running&limit=50");
    running = r.chats || [];
  } catch (e) {
    _dbg("watcher: poll running falló", e.message);
    _setVitals(null);
    return;
  }

  const current = new Map(running.map(
    (c) => [c.id, { project: c.project_slug || "?", source: c.source || "" }]));

  if (_known !== null) {
    for (const [id, info] of current) {
      if (!_known.has(id)) {
        const src = info.source ? ` · ${info.source}` : "";
        _toastClickable(`▶ ${info.project}${src} corriendo…`, "info", "running");
      }
    }
    for (const [id, info] of _known) {
      if (!current.has(id)) _resolveFinished(id, info);
    }
  }
  _known = current;

  try {
    const s = await apiRoot("/stats");
    _setVitals({ running: s.experts_running ?? current.size,
                 tokens: s.tokens_in_today ?? 0,
                 chats: s.chats_today ?? 0 });
  } catch {
    _setVitals({ running: current.size, tokens: null, chats: null });
  }
}

function _setVitals(v) {
  const dot = $("#topbar-dot");
  const runsEl = $("#topbar-runs");
  const tokEl = $("#topbar-tokens");
  const chatsEl = $("#topbar-chats");
  if (!runsEl) return;
  if (v === null) {
    dot.className = "topbar-dot down";
    runsEl.textContent = "relay caído";
    if (tokEl) tokEl.textContent = "—";
    if (chatsEl) chatsEl.textContent = "—";
    return;
  }
  dot.className = "topbar-dot" + (v.running > 0 ? " busy" : "");
  runsEl.textContent = v.running > 0
    ? `${v.running} corriendo` : "sin runs activos";
  if (tokEl) tokEl.textContent = v.tokens == null ? "—" : fmtNum(v.tokens);
  if (chatsEl) chatsEl.textContent = v.chats == null ? "—" : String(v.chats);
}

export function initWatcher() {
  const bar = $("#topbar");
  if (bar) {
    bar.addEventListener("click", (e) => {
      if (e.target.closest("#topbar-status")) _showTab("running");
      if (e.target.closest("#topbar-tokens-wrap")) _showTab("report");
      if (e.target.closest("#topbar-chats-wrap")) _showTab("chats");
    });
  }
  _tick();
  // Via pollers.js: un solo intervalo compartido en toda la app, con
  // pausa automática cuando la pestaña queda oculta.
  _timer = registerPoller(_tick, POLL_MS);
}
