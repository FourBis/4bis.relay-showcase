// Tab Logs (UI 2026-07-20): cola en vivo del ring buffer del relay.
//
// Fuente: GET /admin/api/logs (deque de 2000 en memoria). Desde el
// 2026-07-25 el relay TAMBIÉN escribe a ~/.4bis/logs/relay.log rotado:
// esto es el tail en vivo, el archivo es lo que sobrevive al reinicio.
//
// Filtros: nivel y chat server-side, texto client-side. Auto-refresh
// opt-in cada 5s SOLO mientras el tab está visible.
//
// El filtro por chat es la razón de ser de todo esto: poder contestar
// "qué pasó en este run" sin reconstruirlo a mano desde la DB.

import { $, api, escape, _dbg } from "./api.js";
import { registerPoller } from "./pollers.js";

let _timer = null;
let _level = "";
let _query = "";
let _chat = "";

export function initLogs() {
  $("#logs-level").addEventListener("change", (e) => {
    _level = e.target.value;
    loadLogs();
  });
  $("#logs-search").addEventListener("input", (e) => {
    _query = e.target.value.toLowerCase();
    loadLogs();
  });
  $("#logs-chat").addEventListener("input", (e) => {
    _chat = e.target.value.trim();
    loadLogs();
  });
  $("#logs-chat-clear").addEventListener("click", () => {
    _chat = "";
    $("#logs-chat").value = "";
    loadLogs();
  });
  $("#logs-refresh").addEventListener("click", loadLogs);
  $("#logs-auto").addEventListener("change", (e) => {
    if (e.target.checked) {
      // tabId: el registry solo lo dispara con el tab logs activo
      // (antes el guard de visibilidad era manual).
      _timer = registerPoller(loadLogs, 5000, { tabId: "logs" });
    } else if (_timer) {
      _timer(); // unregister
      _timer = null;
    }
  });
}

const _LEVEL_CLS = {
  ERROR: "text-red-300", CRITICAL: "text-red-300",
  WARNING: "text-amber-300", INFO: "text-zinc-400",
  DEBUG: "text-zinc-400",
};

/**
 * Abre el tab Logs filtrado por un run. Lo llama el modal de Status y
 * cualquier otro lugar que tenga un chat_id a mano.
 */
export function showChatLogs(chatId) {
  _chat = (chatId || "").trim();
  const input = $("#logs-chat");
  if (input) input.value = _chat;
  document.querySelector('.tab[data-tab="logs"]')?.click();
  loadLogs();
}

export async function loadLogs() {
  const box = $("#logs-box");
  try {
    const q = "logs?limit=2000"
      + (_level ? `&level=${_level}` : "")
      + (_chat ? `&chat=${encodeURIComponent(_chat)}` : "");
    const r = await api(q);
    let logs = r.logs || [];
    if (_query) {
      logs = logs.filter((l) =>
        (l.msg + " " + l.logger).toLowerCase().includes(_query));
    }
    $("#logs-count").textContent = `${logs.length} líneas`
      + (_chat ? ` · chat ${_chat.slice(0, 8)}…` : "");
    if (!logs.length) {
      box.innerHTML = _chat
        ? `<p class="empty">sin líneas para el chat <code>${escape(_chat)}</code>.
           El buffer en memoria se vacía al reiniciar el relay — para runs
           viejos, busca el id en <code>~/.4bis/logs/relay.log</code>.</p>`
        : `<p class="empty">sin logs que matcheen
           (el buffer arranca vacío en cada reinicio del relay)</p>`;
      return;
    }
    const atBottom =
      box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    // El chip del chat sale solo cuando NO estás ya filtrando por uno
    // (si filtraste, es la misma columna repetida en cada línea).
    // Clickeable: ves un error suelto y saltas al run entero.
    box.innerHTML = logs.map((l) => `<div class="log-line">
      <span class="log-ts">${escape(l.ts)}</span>
      <span class="log-level ${_LEVEL_CLS[l.level] || "text-zinc-400"}">${escape(l.level.padEnd(7))}</span>
      <span class="log-logger">${escape(l.logger)}</span>${
        !_chat && l.chat_id
          ? `<span class="log-chat" data-chat="${escape(l.chat_id)}"
                   role="button" tabindex="0"
                   title="ver solo este run: ${escape(l.chat_id)}"
                   aria-label="filtrar logs por este chat (${escape(l.chat_id)})"
             >${escape(l.chat_id.slice(0, 8))}</span>`
          : ""}
      <span class="log-msg">${escape(l.msg)}</span>
    </div>`).join("");
    box.querySelectorAll(".log-chat").forEach((el) => {
      el.addEventListener("click", () => showChatLogs(el.dataset.chat));
      // Mismo handler desde teclado: sin esto el filtro "por chat" era
      // solo mouse. Enter y Space son la convención para role="button".
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          showChatLogs(el.dataset.chat);
        }
      });
    });
    // Sticky-bottom: si estabas al fondo, sigue al fondo (tipo tail -f).
    if (atBottom) box.scrollTop = box.scrollHeight;
  } catch (e) {
    _dbg("logs: load falló", e.message);
    box.innerHTML = `<p class="fail text-sm p-3">error: ${escape(e.message)}</p>`;
  }
}
