// Buscador global de la Admin UI (UI 2026-07-20).
//
// Overlay con Ctrl+K. Busca en projects + chats + conversations en una sola
// request a /admin/api/search (3 queries chiquitas en SQL). El resultado
// se renderiza agrupado por tipo con keyboard nav (↑↓ Enter Esc).
//
// Click o Enter en un resultado:
//   - proyecto → tab Proyectos, abre el admin modal
//   - chat     → tab Chats, abre el panel SPA (si tiene conversation) o
//                el .md viewer legacy
//   - conv     → tab Conversaciones, abre "continuar" (panel SPA)
//
// Ponytail: cero dependencias, sin fuzzy matching (substring basta para
// el volumen esperado), un solo handler global de keydown (no se monta
// en cada keyup).

import { $, $$, api, escape, _dbg } from "./api.js";
import { toast } from "./ui.js";
import { selectConversation, viewChat } from "./tab-chats.js";

const MIN_CHARS = 2;
const DEBOUNCE_MS = 180;
let _lastQ = "";
let _results = null;          // {projects, chats, conversations} | null
let _flat = [];               // [{kind, id, label, sub, action}]
let _activeIdx = -1;
let _debounceTimer = null;
let _abortController = null;

function _showTab(name) {
  const btn = document.querySelector(`.tab[data-tab="${name}"]`);
  if (btn) btn.click();
}

function _flatten(r) {
  const out = [];
  for (const p of (r.projects || [])) {
    out.push({
      kind: "project",
      id: p.slug,
      label: p.slug,
      sub: p.repo_path || "",
      action: () => { _showTab("projects"); _openAdmin(p.slug); },
    });
  }
  for (const c of (r.chats || [])) {
    out.push({
      kind: "chat",
      id: c.id,
      label: c.project_slug || c.id.slice(0, 8),
      sub: `${c.id.slice(0, 8)} · ${c.status} · ${c.started_at || "—"}`,
      action: () => { _showTab("chat"); _openChat(c.id, c.conversation_id); },
    });
  }
  for (const v of (r.conversations || [])) {
    out.push({
      kind: "conversation",
      id: v.id,
      label: v.project_slug || v.id.slice(0, 8),
      sub: `${v.id.slice(0, 8)} · ${v.status} · ${(v.summary || "—").slice(0, 80)}`,
      action: () => { _showTab("chat"); _openConv(v.id); },
    });
  }
  return out;
}

async function _openAdmin(slug) {
  // El tab Proyectos carga async (loadProjects); esperar activamente a
  // que el row con el admin-btn aparezca (reintenta cada 100ms hasta 5s).
  const sel = `.admin-btn[data-slug="${CSS.escape(slug)}"]`;
  for (let i = 0; i < 50; i++) {
    const btn = document.querySelector(sel);
    if (btn) { btn.click(); return; }
    await new Promise((r) => setTimeout(r, 100));
  }
  _dbg("search: admin-btn no apareció para", slug);
  toast(`No se pudo abrir el admin de "${slug}" — refresca Proyectos.`, "err");
}

async function _openChat(chatId, conversationId) {
  // Workspace unificado (2026-07-20e): si el chat tiene conversación, la
  // abrimos en el panel; si no, caemos al .md viewer. selectConversation
  // no depende de la lista del sidebar (fetch directo por id), así que
  // corre apenas cambia el tab.
  await new Promise((r) => setTimeout(r, 60));
  if (conversationId) selectConversation(conversationId);
  else viewChat(chatId);
}

async function _openConv(convId) {
  await new Promise((r) => setTimeout(r, 60));
  selectConversation(convId);
}

function _render() {
  const list = $("#search-list");
  if (!_results) {
    list.innerHTML = `<div class="px-4 py-6 text-center text-sm text-zinc-400">
      escribe al menos ${MIN_CHARS} chars para buscar</div>`;
    return;
  }
  const counts = {
    project: (_results.projects || []).length,
    chat: (_results.chats || []).length,
    conversation: (_results.conversations || []).length,
  };
  if (counts.project + counts.chat + counts.conversation === 0) {
    list.innerHTML = `<div class="px-4 py-6 text-center text-sm text-zinc-400">
      sin resultados para "${escape(_lastQ)}"</div>`;
    return;
  }
  const sections = [];
  if (counts.project) {
    sections.push(`<div class="search-section-title">Proyectos (${counts.project})</div>`);
    sections.push(_flat.filter((x) => x.kind === "project").map(_renderRow).join(""));
  }
  if (counts.chat) {
    sections.push(`<div class="search-section-title">Chats (${counts.chat})</div>`);
    sections.push(_flat.filter((x) => x.kind === "chat").map(_renderRow).join(""));
  }
  if (counts.conversation) {
    sections.push(`<div class="search-section-title">Conversaciones (${counts.conversation})</div>`);
    sections.push(_flat.filter((x) => x.kind === "conversation").map(_renderRow).join(""));
  }
  list.innerHTML = sections.join("");
  // Sincronizar índice activo con el primer resultado.
  if (_activeIdx < 0 || _activeIdx >= _flat.length) _activeIdx = 0;
  _highlightActive();
}

// Click delegation en #search-list. Los rows se re-renderizan en cada
// query (innerHTML), pero el listener queda pegado al container padre
// y lee data-idx del target. Sin esto, los botones quedaban huérfanos:
// keyboard Enter funcionaba pero click del mouse no.
//
// UX 2026-07-20: usamos `mousedown` en lugar de `click` porque el handler
// de cierre del backdrop (más abajo) también escucha `click` y burbujea,
// y en algunos browsers el orden de eventos mousedown→click puede hacer
// que el modal se cierre antes de que el row-click handler dispare.
// `mousedown` se procesa antes y nunca es interrumpido por el backdrop.
function _onRowMouseDown(e) {
  const row = e.target.closest(".search-row");
  if (!row) return;
  const idx = Number(row.dataset.idx);
  if (Number.isNaN(idx)) return;
  e.preventDefault();  // evita que se seleccione texto
  _activate(idx);
}

function _renderRow(r, i) {
  const idx = _flat.indexOf(r);
  const icon = r.kind === "project" ? "📦"
             : r.kind === "chat" ? "💬" : "🧵";
  return `<button type="button" class="search-row${idx === _activeIdx ? " active" : ""}"
    data-idx="${idx}">
    <span class="search-row-icon">${icon}</span>
    <span class="search-row-text">
      <span class="search-row-label">${escape(r.label)}</span>
      <span class="search-row-sub">${escape(r.sub)}</span>
    </span>
    <span class="search-row-kind">${escape(r.kind)}</span>
  </button>`;
}

function _highlightActive() {
  $$(".search-row").forEach((el, i) => {
    el.classList.toggle("active", i === _activeIdx);
  });
  const active = $(".search-row.active");
  if (active) active.scrollIntoView({ block: "nearest" });
}

function _activate(idx) {
  const r = _flat[idx];
  if (!r) return;
  close();
  // Defer un poco para que el modal cierre antes de que cambie el tab.
  setTimeout(() => r.action(), 30);
}

async function _search(q) {
  if (_abortController) _abortController.abort();
  _abortController = new AbortController();
  try {
    const r = await api(`search?q=${encodeURIComponent(q)}`, {}, 8_000);
    _results = r;
    _flat = _flatten(r);
    _activeIdx = _flat.length ? 0 : -1;
    _render();
  } catch (e) {
    if (e.name === "AbortError") return;
    $("#search-list").innerHTML = `<div class="px-4 py-6 text-center text-sm text-zinc-400">
      error: ${escape(e.message)}</div>`;
  }
}

function _onInput(e) {
  const q = e.target.value.trim();
  _lastQ = q;
  clearTimeout(_debounceTimer);
  if (q.length < MIN_CHARS) {
    _results = null;
    _flat = [];
    _activeIdx = -1;
    _render();
    return;
  }
  _debounceTimer = setTimeout(() => _search(q), DEBOUNCE_MS);
}

function _onKey(e) {
  if (e.key === "Escape") { close(); return; }
  if (e.key === "ArrowDown") {
    e.preventDefault();
    if (_flat.length) { _activeIdx = (_activeIdx + 1) % _flat.length; _highlightActive(); }
    return;
  }
  if (e.key === "ArrowUp") {
    e.preventDefault();
    if (_flat.length) {
      _activeIdx = (_activeIdx - 1 + _flat.length) % _flat.length;
      _highlightActive();
    }
    return;
  }
  if (e.key === "Enter") {
    e.preventDefault();
    if (_activeIdx >= 0) _activate(_activeIdx);
  }
}

export function open() {
  document.getElementById('workspace-launcher-dialog')?.close();
  const m = $("#search-modal");
  if (!m) return;
  m.classList.add("open");
  const inp = $("#search-input");
  inp.value = "";
  $("#search-list").innerHTML = `<div class="px-4 py-6 text-center text-sm text-zinc-400">
    escribe al menos ${MIN_CHARS} chars para buscar</div>`;
  _lastQ = "";
  _results = null;
  _flat = [];
  _activeIdx = -1;
  setTimeout(() => inp.focus(), 30);
}

export function close() {
  const m = $("#search-modal");
  if (!m) return;
  m.classList.remove("open");
  if (_abortController) _abortController.abort();
}

export function initSearch() {
  const m = $("#search-modal");
  if (!m) {
    _dbg("search: #search-modal no existe, init no-op");
    return;
  }
  $("#search-input").addEventListener("input", _onInput);
  $("#search-input").addEventListener("keydown", _onKey);
  $("#search-close")?.addEventListener("click", close);
  // Click delegation: una sola vez al boot, no se vuelve a wirear.
  // Mousedown sobre un row activa el resultado; mousedown sobre el
  // backdrop del modal cierra el modal. Capturing phase para ganar
  // sobre cualquier handler que se monte después en un row.
  $("#search-list").addEventListener("mousedown", _onRowMouseDown, true);
  m.addEventListener("mousedown", (e) => {
    if (e.target === m) close();
  });
  // Atajo global Ctrl+K (y Cmd+K en mac).
  document.addEventListener("keydown", (e) => {
    const isFind = (e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === "k";
    if (isFind) {
      e.preventDefault();
      if (m.classList.contains("open")) close(); else open();
      return;
    }
    // Esc SOLO cuando el modal está abierto y el foco está adentro.
    if (e.key === "Escape" && m.classList.contains("open")
        && m.contains(document.activeElement)) {
      close();
    }
  });
  // Click en el botón de la topbar (si existe) abre el modal.
  const trigger = $("#search-trigger");
  if (trigger) trigger.addEventListener("click", () => open());
  _dbg("search: listo (Ctrl+K)");
}
