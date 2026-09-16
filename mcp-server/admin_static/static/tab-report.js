// Tab Informe (UI 2026-07-20): gasto de tokens como registro/informe.
//
// Fuente: GET /admin/api/report?days=N[&project=slug] — agregados SQL
// (daily / by_project / by_author). El chart de barras salió de acá y
// ahora vive en ui-chart.js, compartido con el resto de los tabs; este
// archivo solo aporta los datos, el rango y el tooltip. Tabla colapsable
// al lado como vista accesible de los mismos datos.

import { $, api, escape, fmtNum, fmtDuration, _dbg } from "./api.js";
import { barChart, fillDays } from "./ui-chart.js";

let _days = 30;
let _project = "";
let _loaded = false;

export function initReport() {
  $("#report-days").addEventListener("change", (e) => {
    _days = Number(e.target.value);
    loadReport();
  });
  $("#report-project").addEventListener("change", (e) => {
    _project = e.target.value;
    loadReport();
  });
  $("#report-refresh").addEventListener("click", loadReport);
}

export async function loadReport() {
  const grid = $("#report-kpis");
  try {
    const q = `report?days=${_days}`
      + (_project ? `&project=${encodeURIComponent(_project)}` : "");
    const data = await api(q);
    _render(data);
    if (!_loaded) { _loaded = true; _fillProjectFilter(); }
  } catch (e) {
    grid.innerHTML = `<p class="fail text-sm col-span-full">error: ${escape(e.message)}</p>`;
  }
}

// El filtro de proyectos se llena UNA vez desde /admin/api/projects
// (cacheado server-side; no paga spawn de cbm desde el perf pass).
async function _fillProjectFilter() {
  try {
    const r = await api("projects");
    const sel = $("#report-project");
    const cur = sel.value;
    sel.innerHTML = `<option value="">todos los proyectos</option>`
      + (r.projects || []).map((p) =>
        `<option value="${escape(p.slug)}">${escape(p.slug)}</option>`).join("");
    sel.value = cur;
  } catch (e) { _dbg("report: fill projects falló", e.message); }
}

function _render(data) {
  const daily = data.daily || [];
  const tIn = daily.reduce((a, d) => a + (d.tokens_in || 0), 0);
  const tOut = daily.reduce((a, d) => a + (d.tokens_out || 0), 0);
  const runs = daily.reduce((a, d) => a + (d.runs || 0), 0);
  const notOk = daily.reduce((a, d) => a + (d.not_ok || 0), 0);
  const okPct = runs ? Math.round(((runs - notOk) / runs) * 100) : null;

  // 2026-08-31: la parte cacheada de la entrada. "tokens entrada" solo
  // es el bruto y se lee como gasto pleno; medido en MiniMax, el 82,5%
  // son cache reads que se cobran a una fracción.
  const tCache = daily.reduce((a, d) => a + (d.cache_read_tokens || 0), 0);

  $("#report-kpis").innerHTML = [
    _kpi(fmtNum(tIn), "tokens entrada"),
    ...(tCache ? [_kpi(
      fmtNum(tCache),
      `de caché (${Math.round((100 * tCache) / tIn)}% de la entrada)`)] : []),
    _kpi(fmtNum(tOut), "tokens salida"),
    _kpi(String(runs), "runs"),
    _kpi(okPct == null ? "—" : okPct + "%", "éxito",
         okPct != null && okPct < 90 ? "!text-amber-300" : ""),
    _kpi(String(notOk), "con error", notOk > 0 ? "!text-red-300" : ""),
  ].join("");

  _renderChart(daily);
  _renderDailyTable(daily);
  _renderProjects(data.by_project || []);
  _renderAuthors(data.by_author || []);
}

function _kpi(v, k, vCls = "") {
  return `<div class="stat"><div class="stat-v ${vCls}">${v}</div>
    <div class="stat-k">${k}</div></div>`;
}

// ---- chart de barras diario (una serie: tokens_in) ----

function _renderChart(daily) {
  // fillDays: los días sin actividad son barra 0. Saltearlos comprime el
  // eje y miente sobre el ritmo de gasto.
  const dias = fillDays(daily, _days, { runs: 0, tokens_in: 0, tokens_out: 0 });
  barChart("#report-chart", {
    // label corto para el eje X (día del mes); la fecha completa la pone
    // el tooltip, que tiene lugar.
    points: dias.map((d) => ({ label: d.day.slice(8, 10), value: d.tokens_in, ...d })),
    ariaLabel: `tokens de entrada por día, últimos ${_days} días`,
    onEmpty: "sin actividad en el rango",
    tooltip: (d) => `<div class="font-medium text-zinc-100">${escape(d.day)}</div>
      <div>entrada <span class="tabular-nums">${fmtNum(d.tokens_in)}</span></div>
      <div>salida <span class="tabular-nums">${fmtNum(d.tokens_out)}</span></div>
      <div>runs <span class="tabular-nums">${d.runs || 0}</span></div>`,
  });
}

function _renderDailyTable(daily) {
  const tbody = $("#report-daily-table tbody");
  tbody.innerHTML = daily.length ? daily.slice().reverse().map((d) => `<tr>
    <td class="tabular-nums">${escape(d.day)}</td>
    <td class="tabular-nums">${d.runs}</td>
    <td class="tabular-nums">${fmtNum(d.tokens_in)}</td>
    <td class="tabular-nums">${fmtNum(d.tokens_out)}</td>
    <td class="tabular-nums">${d.tool_calls ?? "—"}</td>
    <td class="tabular-nums">${fmtDuration(d.avg_duration_ms)}</td>
    <td class="tabular-nums ${d.not_ok ? "text-red-300" : ""}">${d.not_ok || 0}</td>
  </tr>`).join("")
    : `<tr><td colspan="7" class="empty">sin actividad en el rango</td></tr>`;
}

function _renderProjects(rows) {
  const tbody = $("#report-projects-table tbody");
  tbody.innerHTML = rows.length ? rows.map((p) => `<tr>
    <td><code>${escape(p.project)}</code></td>
    <td class="tabular-nums">${p.runs}</td>
    <td class="tabular-nums">${fmtNum(p.tokens_in)}</td>
    <td class="tabular-nums">${fmtNum(p.tokens_out)}</td>
    <td class="tabular-nums">${p.tool_calls ?? "—"}</td>
    <td class="tabular-nums">${fmtDuration(p.avg_duration_ms)}</td>
    <td class="tabular-nums ${p.not_ok ? "text-red-300" : ""}">${p.not_ok || 0}</td>
    <td class="whitespace-nowrap text-xs text-zinc-400">${escape((p.last_run || "").slice(0, 16).replace("T", " "))}</td>
  </tr>`).join("")
    : `<tr><td colspan="8" class="empty">sin actividad en el rango</td></tr>`;
}

function _renderAuthors(rows) {
  const tbody = $("#report-authors-table tbody");
  tbody.innerHTML = rows.length ? rows.map((a) => `<tr>
    <td>${escape(a.author)}</td>
    <td><span class="badge dim">${escape(a.source)}</span></td>
    <td class="tabular-nums">${a.runs}</td>
    <td class="tabular-nums">${fmtNum(a.tokens_in)}</td>
    <td class="tabular-nums">${fmtNum(a.tokens_out)}</td>
    <td class="tabular-nums">${fmtDuration(a.avg_duration_ms)}</td>
  </tr>`).join("")
    : `<tr><td colspan="6" class="empty">sin actividad en el rango</td></tr>`;
}
