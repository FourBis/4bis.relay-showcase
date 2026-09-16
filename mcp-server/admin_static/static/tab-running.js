// Tab En curso (Sub-ola 2.1): liveness de runs vivos + modal de status.

import { $, $$, apiRoot, escape, formatElapsed, _dbg, openProjectInVSCode, onClick } from "./api.js";
import { statusBadge, modalLoad, isModalOpen, closeModal, confirmModal, toast } from "./ui.js";
import { dtView } from "./ui-table.js";
import { viewChat } from "./tab-chats.js";
import { showChatLogs } from "./tab-logs.js";
import { updateRunningBadge } from "./tab-status.js";

// Estado de orden local. dtView conserva orden y página entre
// refreshes manuales del usuario (no hay poller — este tab se recarga
// on-click). Default: inicio descendente (lo más nuevo arriba).
const _PAGE_SIZE = 25;
const _runState = { sortKey: 0, sortDir: "desc", page: 0 };
const _RUN_COLUMNS = [
  { key: "started_at", label: "inicio", className: "whitespace-nowrap",
    value: (c) => c.started_at || "" },
  { key: "project", label: "proyecto",
    value: (c) => c.project_slug || c.target || "" },
  { key: "author", label: "autor", value: (c) => c.author || "" },
  { key: "source", label: "origen", value: (c) => c.source || "" },
  { key: "elapsed", label: "elapsed", sortable: false,
    value: (c) => Date.parse((c.started_at || "").replace(" ", "T") + "Z") || 0 },
  { key: "phase", label: "fase", sortable: false,
    value: (c) => c.status === "running" ? 1 : 0 },
  { key: "tool", label: "tool", sortable: false,
    value: (c) => c.last_tool || "" },
  { key: "tokens_in", label: "tokens in", className: "tabular-nums",
    value: (c) => c.tokens_in ?? 0 },
  { key: "_actions", label: "", sortable: false, className: "row-actions" },
];

// Refresco on-demand (decisión del usuario: sin auto-polling).
export async function loadRunning() {
  const limit = $("#running-limit").value;
  const includeFinished = $("#running-include-finished").checked;
  _dbg("loadRunning start", { limit, includeFinished });
  // status=running SIEMPRE; si el user pidió incluir los recién
  // terminados, segundo fetch con status=ok filtrado client-side (≤ 30s).
  try {
    const r = await apiRoot(`/chats?status=running&limit=${limit}`);
    _dbg("loadRunning fetch1 ok", "chats count:", r.chats?.length);
    let rows = r.chats || [];
    if (includeFinished) {
      try {
        const r2 = await apiRoot(`/chats?status=ok&limit=20`);
        _dbg("loadRunning fetch2 ok", "chats count:", r2.chats?.length);
        const now = Date.now();
        rows = rows.concat((r2.chats || []).filter((c) => {
          if (!c.finished_at) return false;
          const t = Date.parse(c.finished_at.replace(" ", "T") + "Z");
          return (now - t) <= 30_000;
        }));
      } catch (e) {
        _dbg("loadRunning fetch2 error", e.message);
      }
    }
    rows.sort((a, b) => (b.started_at || "").localeCompare(a.started_at || ""));
    $("#running-summary").textContent = `${rows.length} en curso`;
    const tbody = $("#running-table tbody");
    if (!rows.length) {
      tbody.innerHTML = "";
      $("#running-empty").hidden = false;
      updateRunningBadge(0);
      _dbg("loadRunning done (empty)");
      return;
    }
    $("#running-empty").hidden = true;
    // dtView filter+sort+paginate (sin input de búsqueda: el shell ya
    // existe y no queremos pisar el toolbar del usuario con un .dt-search).
    const { slice } = dtView(_RUN_COLUMNS, {
      rows,
      query: "",
      sortKey: _runState.sortKey,
      sortDir: _runState.sortDir,
      page: _runState.page,
    }, rows.length > _PAGE_SIZE ? _PAGE_SIZE : 0);
    tbody.innerHTML = slice.map((c) => {
      const elapsed = formatElapsed(c.started_at);
      const phase = c.status === "running"
        ? "running" : (c.phase_at_end || c.status);
      const tool = c.last_tool || "—";
      const stCls = c.status === "ok" ? "ok"
                  : c.status === "running" ? "warn" : "err";
      return `<tr data-id="${escape(c.id)}">
        <td class="whitespace-nowrap">${escape(c.started_at || "")}</td>
        <td><code>${escape(c.project_slug || c.target || "—")}</code></td>
        <td>${escape(c.author || "—")}</td>
        <td>${escape(c.source || "—")}</td>
        <td class="tabular-nums">${elapsed}</td>
        <td><span class="badge ${stCls}">${escape(phase)}</span></td>
        <td>${escape(tool)}</td>
        <td class="tabular-nums">${c.tokens_in ?? "—"}</td>
        <td class="row-actions"><span class="action-group">
          <button class="btn btn-xs running-status-btn" data-id="${escape(c.id)}"
                  title="snapshot vivo">Status</button>
          <button class="btn btn-xs running-vscode-btn"
                  data-slug="${escape(c.project_slug || c.target || "")}"
                  title="abrir el repo en VS Code"
                  aria-label="abrir el repo en VS Code"><svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4.5" width="18" height="12" rx="2"/><path d="M8 20h8M12 16.5V20"/></svg></button>
        </span><span class="action-group">
          ${c.status === "running"
            ? `<button class="btn btn-xs danger running-cancel-btn" data-id="${escape(c.id)}"
                  title="matar el run">Cancelar</button>`
            : ""}
        </span></td>
      </tr>`;
    }).join("");
    // aria-sort en el <th> según el estado.
    const ths = document.querySelectorAll("#running-table thead .th");
    ths.forEach((th, i) => {
      const col = _RUN_COLUMNS[i];
      if (!col || col.sortable === false) {
        th.removeAttribute("aria-sort");
        return;
      }
      th.setAttribute("aria-sort",
        i === _runState.sortKey
          ? (_runState.sortDir === "desc" ? "descending" : "ascending")
          : "none");
    });
    _dbg("loadRunning render ok");
    updateRunningBadge(rows.filter((c) => c.status === "running").length);
    _dbg("loadRunning done (rendered)");
  } catch (e) {
    _dbg("loadRunning ERROR", e.message);
    $("#running-summary").textContent = "error: " + e.message;
  }
}

function openStatusModal(chatId) {
  modalLoad({
    id: "status-modal",
    title: `Status · ${chatId.slice(0, 8)}…`,
    loader: () => apiRoot(`/chats/${encodeURIComponent(chatId)}/status`),
    render: (r) => renderStatusDetail(r, chatId),
    after: () => {
      onClick("#status-modal-cancel",
        () => cancelRunning(chatId, $("#status-modal-msg")));
      onClick("#status-modal-md", () => viewChat(chatId));
      // El .md tiene la conversación; los logs tienen el POR QUÉ
      // (watchdog, trim, resolución de timeouts, spawn del pool).
      onClick("#status-modal-logs", () => {
        closeModal("status-modal");
        showChatLogs(chatId);
      });
    },
    watchdogDetail: `El endpoint <code>/chats/{id}/status</code> no debería
      tardar. Si esto sigue acá, el relay está ocupado o colgado.`,
  });
}

function renderStatusDetail(r, chatId) {
  const c = r.chat || {};
  const p = r.progress;
  const cells = [];
  cells.push(["chat_id", `<code>${escape(c.id || "—")}</code>`]);
  cells.push(["status DB", statusBadge(c.status)]);
  cells.push(["proyecto", `<code>${escape(c.project_slug || c.target || "—")}</code>`]);
  cells.push(["autor / origen",
    `${escape(c.author || "—")} / ${escape(c.source || "—")}`]);
  cells.push(["iniciado", escape(c.started_at || "—")]);
  cells.push(["terminado", escape(c.finished_at || "— (vivo)")]);
  cells.push(["tokens in / out",
    `${c.tokens_in ?? "—"} / ${c.tokens_out ?? "—"}`]);
  cells.push(["tool_calls", c.tool_calls ?? "—"]);
  cells.push(["phase_at_end (DB)", escape(c.phase_at_end || "—")]);
  cells.push(["last_tool (DB)", escape(c.last_tool || "—")]);
  if (c.error) cells.push(["error", `<span class="fail">${escape(c.error)}</span>`]);
  // Sprint 1: progress timeline from progress_events JSON
  let timelineHtml = "";
  if (c.progress_events) {
    try {
      const events = typeof c.progress_events === "string"
        ? JSON.parse(c.progress_events) : c.progress_events;
      if (Array.isArray(events) && events.length) {
        timelineHtml = `<h4>Timeline de progreso (${events.length} eventos)</h4>
          <div class="text-sm">${events.map((e) => {
            const icon = e.phase === "thinking" ? "🧠" :
              e.phase === "tool_call" ? "🔧" :
              e.phase === "writing" ? "✍️" :
              e.phase === "heartbeat" ? "💓" :
              e.phase === "say" ? "💭" :          // el por qué del modelo
              e.phase === "steer" ? "🧭" : "•";   // corrección del humano
            const detail = e.message || e.tool || "";
            const ts = (e.ts || "").slice(11, 19); // HH:MM:SS
            return `<div class="flex items-start gap-2 py-0.5">
              <span class="shrink-0">${icon}</span>
              <span class="text-zinc-300">${escape(detail)}</span>
              <span class="ml-auto font-mono text-[10px] text-zinc-400">${escape(ts)}</span>
            </div>`;
          }).join("")}</div>`;
      }
    } catch { /* ignore parse errors */ }
  }
  let html = `<table class="status-detail"><tbody>${
    cells.map(([k, v]) =>
      `<tr><th>${k}</th><td>${v}</td></tr>`).join("")}</tbody></table>${timelineHtml}`;
  if (p && !Array.isArray(p)) {
    // snapshot vivo (un solo match)
    html += `<h4>Snapshot vivo</h4>
      <table class="status-detail"><tbody>
        <tr><th>elapsed_s</th><td>${p.elapsed_s}</td></tr>
        <tr><th>idle_s</th><td>${p.idle_s}</td></tr>
        <tr><th>phase</th><td>${escape(p.phase)}</td></tr>
        <tr><th>last_tool</th><td>${escape(p.last_tool || "—")}</td></tr>
        <tr><th>tool_calls</th><td>${p.tool_calls}</td></tr>
        <tr><th>tokens in / out</th>
            <td>${p.tokens_in ?? "—"} / ${p.tokens_out ?? "—"}</td></tr>
        <tr><th>model</th><td><code>${escape(p.model || "—")}</code></td></tr>
        <tr><th>finished</th><td>${p.finished ? "sí" : "no"}</td></tr>
        ${p.error ? `<tr><th>error vivo</th>
            <td><span class="fail">${escape(p.error)}</span></td></tr>` : ""}
      </tbody></table>`;
  } else if (p && Array.isArray(p)) {
    html += `<p class="muted mt-3">múltiples runs con prefijo ${escape(chatId)};
      ${p.length} snapshots.</p>`;
  } else if (r.is_running) {
    html += `<p class="muted mt-3">El chat figura como <code>running</code> en SQLite
      pero no hay snapshot vivo (relay probablemente reinició
      durante el run). Si quieres datos, mira el log del relay.</p>`;
  } else {
    html += `<p class="muted mt-3">No hay snapshot vivo. El run ya terminó y el
      progress store se liberó.</p>`;
  }
  return html;
}

async function cancelRunning(chatId, msgEl) {
  const target = msgEl || $("#running-summary");
  if (!await confirmModal({
    title: `Cancelar run ${chatId.slice(0, 8)}…`,
    body: "El bot va a recibir kind=cancelled. Esto no se puede deshacer.",
    confirmText: "Sí, cancelar",
    danger: true,
  })) return;
  target.textContent = "cancelando...";
  try {
    await apiRoot(`/experts/cancel/${encodeURIComponent(chatId)}`, {
      method: "POST",
    });
    target.textContent = "cancelado ✓";
    loadRunning();
    // si el modal de status está abierto, refrescar su snapshot
    if (isModalOpen("status-modal")) openStatusModal(chatId);
  } catch (e) {
    target.textContent = "error: " + e.message;
  }
}

export function initRunning() {
  onClick("#running-refresh", loadRunning);
  // Delegación sobre el wrapper: el tbody se reescribe en cada refresh
  // (es load-on-click, no poller, pero igual destruye listeners por fila).
  const el = $("#running-table");
  if (el && !el.dataset.wired) {
    el.dataset.wired = "1";
    el.addEventListener("click", async (e) => {
      const stBtn = e.target.closest(".running-status-btn");
      if (stBtn) { openStatusModal(stBtn.dataset.id); return; }
      const vsBtn = e.target.closest(".running-vscode-btn");
      if (vsBtn) {
        const r = await openProjectInVSCode(vsBtn.dataset.slug);
        if (!r.ok) toast("No se pudo abrir VS Code: " + r.error, "err");
        return;
      }
      const cBtn = e.target.closest(".running-cancel-btn");
      if (cBtn) { cancelRunning(cBtn.dataset.id); return; }
    });
    // Orden por <th>: click y teclado (Enter/Espacio), igual que
    // ui-table.js#_sortFrom — el thead es estático en index.html, así
    // que no migramos a dataTable(), pero el wiring copia el mismo
    // patrón de accesibilidad.
    const thead = el.querySelector("thead");
    if (thead && !thead.dataset.sortWired) {
      thead.dataset.sortWired = "1";
      const sortFrom = (target) => {
        const th = target.closest?.(".th-sort");
        if (!th) return;
        const i = Number(th.dataset.col);
        const col = _RUN_COLUMNS[i];
        if (!col || col.sortable === false) return;
        if (_runState.sortKey === i) {
          _runState.sortDir = _runState.sortDir === "asc" ? "desc" : "asc";
        } else {
          _runState.sortKey = i;
          _runState.sortDir = "asc";
        }
        _runState.page = 0;
        loadRunning();
      };
      thead.addEventListener("click", (e) => sortFrom(e.target));
      thead.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sortFrom(e.target); }
      });
    }
  }
}
