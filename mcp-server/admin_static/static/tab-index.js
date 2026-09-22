// Tab Indexación: file browser + bulk indexado + tabla de archivos.

import { $, $$, api, escape, onClick } from "./api.js";
import { toast, emptyRow } from "./ui.js";
import { registerPoller } from "./pollers.js";
import { refreshStatus } from "./tab-status.js";
import { taskWorkspaceQuery } from "./workspace.js";

let _currentSlug = null;
let _pollHandle = null;
let _bulkPoll = null;
let _browseEntries = [];  // entries del último browse

export async function initFromConfig() {
  // Boot: repos_root efectivo como default del file browser.
  try {
    const c = await api("config");
    if (!$("#browse-path").value) {
      $("#browse-path").value = c.effective.repos_root || "";
    }
  } catch (_) { /* primer arranque sin DB: el user tipea el path */ }
}

async function doBrowse() {
  const path = $("#browse-path").value.trim();
  const depth = $("#browse-depth").value;
  if (!path) {
    toast("Pon un path.", "warn");
    return;
  }
  const tbody = $("#browse-table tbody");
  tbody.innerHTML = `<tr><td colspan="5" class="empty">buscando...</td></tr>`;
  $("#browse-summary").textContent = "";
  try {
    const r = await api(`fs/browse?path=${encodeURIComponent(path)}&depth=${depth}`);
    _browseEntries = r.entries || [];
    renderBrowseTable();
    $("#browse-summary").textContent =
      `${r.count} entradas en ${r.root} (depth=${r.depth})`;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty fail">${escape(e.message)}</td></tr>`;
    $("#browse-summary").textContent = "error: " + e.message;
  }
}

function renderBrowseTable() {
  const tbody = $("#browse-table tbody");
  if (!_browseEntries.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty">sin resultados</td></tr>`;
    updateSelectedCount();
    return;
  }
  tbody.innerHTML = _browseEntries.map((e) => {
    const expert = e.has_expert
      ? `<span class="badge ok">${escape(e.expert_slug || "sí")}${e.expert_enabled ? "" : " (off)"}</span>`
      : `<span class="badge dim">no</span>`;
    const cbm = e.cbm_indexed
      ? `<span class="badge ok">indexado</span>`
      : `<span class="badge warn">no</span>`;
    const stats = e.cbm_indexed
      ? `${e.cbm_nodes.toLocaleString()} / ${e.cbm_edges.toLocaleString()}`
      : "-";
    const pad = Math.max(0, e.depth - 1);
    return `<tr class="${e.is_repo ? "" : "non-repo"}">
      <td><input type="checkbox" class="pick" data-path="${escape(e.path)}"></td>
      <td class="path" style="padding-left:${12 + pad * 16}px">${escape(e.path)}</td>
      <td>${expert}</td>
      <td>${cbm}</td>
      <td class="tabular-nums">${stats}</td>
    </tr>`;
  }).join("");
  updateSelectedCount();
}

function setAllChecked(value) {
  $$(".pick").forEach((c) => { c.checked = value; });
  updateSelectedCount();
}

function setFilteredChecked(predicate) {
  $$(".pick").forEach((c) => {
    const entry = _browseEntries.find((e) => e.path === c.dataset.path);
    c.checked = !!(entry && predicate(entry));
  });
  updateSelectedCount();
}

function updateSelectedCount() {
  const n = $$(".pick:checked").length;
  $("#bulk-selected-count").textContent = String(n);
  $("#bulk-start").disabled = n === 0;
}

async function startBulk() {
  const picked = $$(".pick:checked").map((c) => c.dataset.path);
  if (!picked.length) {
    toast("Selecciona al menos un repo.", "warn");
    return;
  }
  const body = {
    paths: picked,
    concurrency: parseInt($("#bulk-conc").value, 10) || 2,
    force: $("#bulk-force").checked,
  };

  const btn = $("#bulk-start");
  btn.disabled = true;
  $("#bulk-progress").hidden = false;
  $("#bulk-bar-fill").style.width = "0%";
  $("#bulk-summary").textContent = "disparando...";
  $("#bulk-table").hidden = true;
  $("#bulk-table tbody").innerHTML = "";

  try {
    const r = await api("projects/index/bulk", {
      method: "POST",
      body: JSON.stringify(body),
    });
    $("#bulk-summary").textContent =
      `job ${r.job_id} • queued=${r.queued} • conc=${r.concurrency} • force=${r.force}`;
    pollBulk(r.job_id);
  } catch (e) {
    $("#bulk-summary").textContent = "error: " + e.message;
    btn.disabled = false;
  }
}

function pollBulk(jobId) {
  if (_bulkPoll) _bulkPoll();
  _bulkPoll = registerPoller(async () => {
    try {
      const r = await api(`reindex/${jobId}`);
      const done = r.done || 0;
      const total = r.total || 1;
      const pct = total > 0 ? Math.round(100 * done / total) : 0;
      $("#bulk-bar-fill").style.width = pct + "%";
      const eta = r.eta_s ? ` • ETA ~${r.eta_s}s` : "";
      const elapsed = r.elapsed_total_s ? ` • elapsed ${r.elapsed_total_s}s` : "";
      $("#bulk-summary").textContent =
        `${r.status} • ${done}/${total} • ok=${r.ok || 0} fail=${r.fail || 0}` +
        eta + elapsed;

      if (r.per_repo && Object.keys(r.per_repo).length > 0) {
        $("#bulk-table").hidden = false;
        const rows = Object.entries(r.per_repo).map(([p, info]) => {
          const cls = info.status === "OK" ? "ok"
                    : info.status === "FAIL" ? "fail"
                    : info.status === "TIMEOUT" ? "fail"
                    : "";
          return `<tr class="${cls}">
            <td class="path">${escape(p)}</td>
            <td>${escape(info.status)}</td>
            <td class="tabular-nums">${info.nodes ?? "-"}</td>
            <td class="tabular-nums">${info.edges ?? "-"}</td>
            <td class="tabular-nums">${info.elapsed_s != null ? info.elapsed_s.toFixed(1) : "-"}</td>
            <td>${escape((info.error || "")).slice(0, 80)}</td>
          </tr>`;
        }).join("");
        $("#bulk-table tbody").innerHTML = rows;
      }

      if (r.status && r.status !== "running") {
        _bulkPoll(); _bulkPoll = null;
        $("#bulk-start").disabled = false;
        $("#bulk-bar-fill").style.width = "100%";
        if (r.status === "done") {
          // Refrescar el browse para que se vean los nuevos indexados.
          doBrowse();
          refreshStatus();
        }
        if (r.status === "partial") {
          $("#bulk-summary").textContent += " • ⚠ hubo fallos";
        }
      }
    } catch (_) { /* ignore transient */ }
  }, 2000, { tabId: "index" });
}

export async function loadIndexPanel() {
  const { projects } = await api("projects");
  const sel = $("#index-projects");
  sel.innerHTML = projects.map(
    (p) => `<option value="${p.slug}">${escape(p.slug)}</option>`
  ).join("");
  if (!_currentSlug) _currentSlug = projects[0]?.slug;
  sel.value = _currentSlug || "";
  sel.onchange = () => { _currentSlug = sel.value; loadFiles(); };
  if (_currentSlug) loadFiles();
}

async function reindexCurrent() {
  if (!_currentSlug) return;
  const btn = $("#reindex-btn");
  btn.disabled = true;
  $("#reindex-status").textContent = "disparando...";
  try {
    const r = await api(`projects/${_currentSlug}/reindex${taskWorkspaceQuery(_currentSlug)}`,
      { method: "POST" });
    $("#reindex-status").textContent = `job ${r.job_id} corriendo...`;
    pollJob(r.job_id);
  } catch (e) {
    $("#reindex-status").textContent = "error: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

function pollJob(jobId) {
  if (_pollHandle) _pollHandle();
  _pollHandle = registerPoller(async () => {
    try {
      const r = await api(`reindex/${jobId}`);
      if (r.status && r.status !== "running") {
        _pollHandle(); _pollHandle = null;
        $("#reindex-status").textContent =
          r.status === "error" ? "error: " + r.error : `${r.status} ✓`;
        if (r.status === "indexed" || r.status === "done") {
          loadFiles();
          refreshStatus();
        }
      }
    } catch (_) { /* ignore */ }
  }, 2000, { tabId: "index" });
}

async function loadFiles() {
  if (!_currentSlug) return;
  try {
    const limit = 200;
    const r = await api(`projects/${_currentSlug}/index/files`
      + taskWorkspaceQuery(_currentSlug, { limit, sort: "path" }));
    const tbody = $("#files-table tbody");
    tbody.innerHTML = r.files.length
      ? r.files.map((f) => `<tr>
          <td class="path">${escape(f.path)}</td>
          <td>${escape(f.name)}</td>
          <td class="tabular-nums">${f.in_degree}</td>
          <td class="tabular-nums">${f.out_degree}</td>
        </tr>`).join("")
      : emptyRow(4, {
          title: "El índice de este proyecto está vacío",
          sub: "cbm no encontró archivos, o el repo todavía no se indexó. "
             + "Corré la indexación desde el panel de arriba.",
        });
  } catch (e) {
    // Silenciosa hasta el 1/9/2026.
    $("#files-table tbody").innerHTML = emptyRow(4, {
      title: "No se pudo leer el índice",
      sub: e.message,
    });
  }
}

export function initIndex() {
  // El default de #browse-path lo trae /admin/api/config en el boot
  // (repos_root efectivo) — nada hardcodeado acá.
  onClick("#browse-btn", doBrowse);
  $("#browse-path").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doBrowse();
  });
  onClick("#browse-select-all", () => setAllChecked(true));
  onClick("#browse-select-none", () => setAllChecked(false));
  onClick("#browse-select-unindexed", () =>
    setFilteredChecked((e) => !e.cbm_indexed));
  onClick("#browse-select-no-expert", () =>
    setFilteredChecked((e) => !e.has_expert));
  onClick("#bulk-start", startBulk);
  // El checkbox de cada fila dispara el contador.
  $("#browse-table").addEventListener("change", updateSelectedCount);
  onClick("#reindex-btn", reindexCurrent);
  onClick("#refresh-files-btn", loadFiles);
}
