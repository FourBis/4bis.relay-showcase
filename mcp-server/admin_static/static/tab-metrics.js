// Sprint 1: Dashboard de métricas (Item #3).
// KPI cards + distribuciones por modelo, proyecto, hora y errores.

import { $, api, escape, fmtNum, onClick } from "./api.js";
import { barChart } from "./ui-chart.js";
import { dataTable } from "./ui-table.js";

// Filtros del dashboard. `days` viaja también a trends; el resto solo
// a summary, que es lo único que sabe desglosar.
const FILTERS = {
  project: "#metrics-project", status: "#metrics-state",
  provider: "#metrics-provider", role: "#metrics-role",
};

function currentFilters() {
  const out = {};
  for (const [key, sel] of Object.entries(FILTERS)) {
    const v = ($(sel)?.value || "").trim();
    if (v) out[key] = v;
  }
  return out;
}

/** Ventana activa: rango absoluto (from+to) si ambos inputs tienen
 *  fecha, si no el atajo de días. `to` vacío se manda como hoy al
 *  backend, así que "desde" solo ya alcanza.
 *
 *  Devuelve además un flag `hasRange` para que la UI sepa si tiene que
 *  habilitar el botón "limpiar rango".
 */
function currentWindow() {
  const from = ($("#metrics-from")?.value || "").trim();
  const to = ($("#metrics-to")?.value || "").trim();
  const hasRange = !!(from && to);
  return {
    kind: hasRange ? "range" : "days",
    days: parseInt($("#metrics-days")?.value || "7"),
    from, to,
    hasRange,
  };
}

// Querystring del hash tal como llegó al cargar la página. Se toma acá,
// a nivel de módulo, porque showTab() hace replaceState("#/metrics")
// ANTES de invocar al loader del tab: leerlo dentro de initMetrics ya
// lo encontraría borrado.
const _bootQs = (() => {
  const h = location.hash || "";
  return h.includes("?") ? h.slice(h.indexOf("?") + 1) : "";
})();

/** Refleja los filtros en la URL para que la vista sea compartible.
 *
 * El prefijo `#/` no es cosmético: el router de main.js matchea
 * `^#\/([a-z-]+)`, así que un `#metrics?...` sin barra no matchea y la
 * app abre el tab por defecto al recargar.
 */
function syncUrl(win, filters) {
  const p = new URLSearchParams();
  if (win.kind === "range") {
    p.set("from", win.from);
    p.set("to", win.to);
  } else {
    p.set("days", String(win.days));
  }
  for (const [k, v] of Object.entries(filters)) p.set(k, v);
  const window = $("#tab-metrics")?.closest('.workspace-window');
  if (window) {
    window.dataset.route = `#/metrics?${p}`;
    if (!window.classList.contains('is-active')) return;
  }
  history.replaceState(null, "", `${location.pathname}#/metrics?${p}`);
}

/** Repone los filtros que venían en la URL al abrir la página. */
export function restoreFiltersFromUrl() {
  if (!_bootQs) return;
  const p = new URLSearchParams(_bootQs);
  const from = p.get("from"), to = p.get("to");
  if (from) { const f = $("#metrics-from"); if (f) f.value = from; }
  if (to)   { const t = $("#metrics-to");   if (t) t.value = to;   }
  const d = $("#metrics-days");
  if (d && p.get("days")) d.value = p.get("days");
  for (const [key, sel] of Object.entries(FILTERS)) {
    const el = $(sel);
    // El select de proyecto/proveedor puede no tener la opción todavía
    // (se llenan con datos); se agrega para no perder el filtro.
    if (el && p.get(key)) {
      const v = p.get(key);
      if (![...el.options].some((o) => o.value === v)) {
        el.insertAdjacentHTML("beforeend",
          `<option value="${escape(v)}">${escape(v)}</option>`);
      }
      el.value = v;
    }
  }
}

export async function loadMetrics() {
  const win = currentWindow();
  const status = $("#metrics-status");
  const filters = currentFilters();
  if (status) status.textContent = "cargando...";

  try {
    // api() prepende /admin/api/. Los filtros van al backend como query
    // params: filtrar en el cliente un JSON que ya vino entero daría
    // números que no coinciden con lo que el servidor agregó.
    const q = new URLSearchParams();
    if (win.kind === "range") { q.set("from", win.from); q.set("to", win.to); }
    else                       { q.set("days", String(win.days)); }
    for (const [k, v] of Object.entries(filters)) q.set(k, v);
    // trends toma los mismos filtros de nivel run (project/status) que
    // summary. provider/role no viajan: filtran turnos, no runs.
    const qt = new URLSearchParams(q);
    const [summary, trends] = await Promise.all([
      api(`metrics/summary?${q}`),
      api(`metrics/trends?${qt}`),
    ]);
    renderMetrics(summary, trends);
    syncUrl(win, filters);
    const n = Object.keys(filters).length;
    if (status) {
      const wLabel = win.kind === "range"
        ? `rango ${win.from} → ${win.to}`
        : `últimos ${win.days}d`;
      status.textContent = wLabel
        + (n ? ` · ${n} filtro${n > 1 ? "s" : ""} activo${n > 1 ? "s" : ""}` : "");
    }
    const clear = $("#metrics-clear");
    if (clear) clear.hidden = n === 0;
    const rclear = $("#metrics-range-clear");
    if (rclear) rclear.hidden = !win.hasRange;
  } catch (e) {
    if (status) status.textContent = "error: " + e.message;
  }
}

/** Llena proyecto y proveedor con lo que existe en los datos. */
async function fillFilterOptions(summary) {
  const provSel = $("#metrics-provider");
  if (provSel) {
    const keep = provSel.value;
    const provs = (summary.by_provider || []).map((p) => p.provider);
    provSel.innerHTML = '<option value="">todos</option>'
      + provs.map((p) => `<option value="${escape(p)}">${escape(p)}</option>`).join("");
    if (keep && provs.includes(keep)) provSel.value = keep;
  }
  const projSel = $("#metrics-project");
  if (projSel && projSel.options.length <= 1) {
    try {
      const r = await api("projects");
      const keep = projSel.value;
      projSel.innerHTML = '<option value="">todos</option>'
        + (r.projects || []).map((p) =>
          `<option value="${escape(p.slug)}">${escape(p.slug)}</option>`).join("");
      if (keep) projSel.value = keep;
    } catch { /* sin lista de proyectos el filtro queda en "todos" */ }
  }
}

function renderMetrics(s, trends) {
  const t = s.totals || {};
  fillFilterOptions(s);
  // provider/role recortan el desglose pero NO los totales (un run usa
  // varios proveedores). Sin decirlo, la tabla y los KPIs parecen no
  // cuadrar.
  const fa = s.filters_applied || {};
  const turnOnly = ["provider", "role"].filter((k) => fa[k]);
  const noteEl = $("#metrics-filter-note");
  if (noteEl) {
    noteEl.hidden = turnOnly.length === 0;
    noteEl.textContent = turnOnly.length
      ? `Filtro por ${turnOnly.join(" y ")}: recorta el desglose por modelo. `
        + "Los KPIs de arriba siguen siendo del run completo."
      : "";
  }
  const usd = (v) => (v === null || v === undefined)
    ? '<span class="text-zinc-600">—</span>'
    : "$" + v.toFixed(2);

  // Delta contra el período anterior de igual largo. Sin base previa se
  // devuelve "" en vez de "+100%": contra cero no hay con qué comparar.
  //
  // `tone` decide el color, y no es cosmético. Solo "Runs" mejora al
  // subir (más uso del relay). Tokens al alza es MÁS GASTO —pintarlo de
  // verde diría "todo bien" justo cuando el plan de MiniMax se está
  // agotando— y más errores es peor. Tool calls no tiene dirección
  // buena o mala, así que va neutro en vez de inventarle una.
  const prev = s.previous || {};
  const delta = (key, tone = "up-good") => {
    const now = t[key], before = prev[key];
    if (before === undefined || before === null || !before) return "";
    if (now === undefined || now === null) return "";
    const pct = Math.round(((now - before) / before) * 100);
    // Clases del bundle de Tailwind ya compilado: agregar una nueva
    // exigiría correr build-css.ps1 y re-sellar admin.css (hay un test
    // que compara el sha de la fuente). emerald-400/amber-400/zinc-500
    // ya están dentro.
    if (pct === 0) return '<div class="mt-1 text-xs text-zinc-500">sin cambios</div>';
    const up = pct > 0;
    let cls = "text-zinc-500";
    if (tone === "up-good") cls = up ? "text-emerald-400" : "text-amber-400";
    else if (tone === "up-bad") cls = up ? "text-amber-400" : "text-emerald-400";
    return `<div class="mt-1 text-xs ${cls}" title="vs los ${s.period_days}d previos">`
      + `${up ? "↑" : "↓"} ${Math.abs(pct)}%</div>`;
  };

  const kpi = [
    { v: t.runs ?? 0, k: "Runs", d: delta("runs", "up-good") },
    { v: fmtNum(t.tokens_in ?? 0), k: "Tokens in", d: delta("tokens_in", "up-bad") },
    { v: fmtNum(t.tokens_out ?? 0), k: "Tokens out", d: delta("tokens_out", "up-bad") },
    { v: t.tool_calls ?? 0, k: "Tool calls", d: delta("tool_calls", "neutral") },
    { v: t.errors ?? 0, k: "Intentos con error", d: delta("errors", "up-bad") },
    { v: t.running ?? 0, k: "En curso" },
    { v: t.cancelled ?? 0, k: "Cancelados" },
    { v: t.split ?? 0, k: "Subdivididos" },
  ];
  // Caché (2026-08-31): la parte de "Tokens in" que el proveedor sirvió
  // de su caché y cobra a una fracción. Sin este KPI, "Tokens in" se lee
  // como gasto pleno y da 3-4x del costo real (82,5% de la entrada de
  // MiniMax son cache reads). Solo aparece si hay medición: los runs
  // anteriores a la columna tienen 0 y eso no es "no hubo caché".
  if (t.cache_read_tokens) {
    const pctCache = t.tokens_in
      ? Math.round((100 * t.cache_read_tokens) / t.tokens_in) : 0;
    kpi.push({
      v: fmtNum(t.cache_read_tokens),
      k: `De caché (${pctCache}% de la entrada)`,
    });
  }
  // Costo y ahorro solo se muestran si hay tarifas cargadas: dos KPIs
  // en "$0.00" porque nadie cargó precios son peores que no mostrarlos.
  if (t.priced) {
    kpi.push({ v: usd(t.cost_usd), k: "Costo" });
    kpi.push({ v: usd(t.saved_usd), k: "Ahorro free tier" });
  }

  const metric = (x) =>
    `<div class="stat"><div class="stat-v">${x.v}</div>`
    + `<div class="stat-k">${x.k}</div>${x.d || ""}</div>`;

  const kpiEl = $("#metrics-kpi");
  if (kpiEl) kpiEl.innerHTML = kpi.filter((_x, i) => [0, 1, 4].includes(i)).map(metric).join('');
  const secondary = $("#metrics-secondary");
  if (secondary) secondary.innerHTML = kpi.filter((_x, i) => ![0, 1, 4].includes(i)).map(metric).join('');

  _renderTrend(trends, s.period_days || 7);
  _renderHourly(s.hourly_distribution || []);

  // Por proveedor/modelo/proyecto/errores: dataTable() con mount lazy.
  // `t.priced` se congela en el primer mount — si cambia mid-session
  // (cargar tarifas nuevas en Config), hay que recargar la página. Es
  // el mismo comportamiento que tenía el código viejo con `costCol`.
  const priced = !!t.priced;
  _renderProveedor(s.by_provider || [], priced);
  _renderModelo(s.by_model || [], priced);
  _renderProyecto(s.by_project || []);
  _renderErrores(s.error_breakdown || []);
}

// ---- tablas: dataTable() lazy, una instancia por <div> ----

// Por proveedor. Va ARRIBA del desglose por modelo (pregunta: cuánto
// corre en el provider pago vs gratis).
const _tablaProveedor = { ref: null };

function _renderProveedor(rows, priced) {
  const mount = $("#metrics-by-provider");
  if (!mount) return;
  if (!_tablaProveedor.ref) {
    _tablaProveedor.ref = dataTable(mount, {
      searchPlaceholder: "filtrar proveedores…",
      sort: { key: 2, dir: "desc" },  // más tokens in primero
      columns: [
        { key: "provider", label: "proveedor",
          render: (p) => `<code>${escape(p.provider)}</code>`,
          value: (p) => p.provider || "" },
        { key: "runs", label: "turnos", className: "tabular-nums",
          render: (p) => String(p.runs ?? 0), value: (p) => p.runs ?? 0 },
        { key: "tokens_in", label: "tokens in", className: "tabular-nums",
          render: (p) => _tokensCell(p.tokens_in),
          value: (p) => p.tokens_in ?? null },
        { key: "cache_read_tokens", label: "de caché",
          className: "tabular-nums",
          title: "parte de la entrada que vino de la caché del proveedor",
          render: (p) => _tokensCell(p.cache_read_tokens),
          value: (p) => p.cache_read_tokens ?? null },
        { key: "tokens_out", label: "tokens out", className: "tabular-nums",
          render: (p) => _tokensCell(p.tokens_out),
          value: (p) => p.tokens_out ?? null },
        ...(priced ? [{ key: "cost_usd", label: "costo",
          className: "tabular-nums",
          render: (p) => _usd(p.cost_usd),
          value: (p) => p.cost_usd ?? null }] : []),
      ],
      empty: "sin turnos en el rango",
    });
  }
  _tablaProveedor.ref.setRows(rows);
}

// Por modelo + rol. Un mismo modelo puede aparecer como ejecutor y como
// verificador; sin la columna de rol, ver muchos turnos de un nemotron
// no dice si planifica o si ejecuta.
const _ROLE = { executor: "ejecutor", planner: "planificador",
                verifier: "verificador", documenter: "documentador" };
const _tablaModelo = { ref: null };

function _renderModelo(rows, priced) {
  const mount = $("#metrics-by-model");
  if (!mount) return;
  if (!_tablaModelo.ref) {
    _tablaModelo.ref = dataTable(mount, {
      searchPlaceholder: "filtrar modelos…",
      sort: { key: 2, dir: "desc" },
      columns: [
        { key: "model", label: "modelo",
          render: (m) => `<code>${escape(m.model)}</code>`,
          value: (m) => m.model || "" },
        { key: "role", label: "rol",
          className: "text-xs text-zinc-400",
          render: (m) => escape(_ROLE[m.role] || m.role || "—"),
          value: (m) => _ROLE[m.role] || m.role || "" },
        { key: "runs", label: "turnos", className: "tabular-nums",
          render: (m) => String(m.runs ?? 0), value: (m) => m.runs ?? 0 },
        { key: "tokens_in", label: "tokens in", className: "tabular-nums",
          render: (m) => _tokensCell(m.tokens_in),
          value: (m) => m.tokens_in ?? null },
        { key: "cache_read_tokens", label: "de caché",
          className: "tabular-nums",
          title: "parte de la entrada que vino de la caché del proveedor",
          render: (m) => _tokensCell(m.cache_read_tokens),
          value: (m) => m.cache_read_tokens ?? null },
        { key: "tokens_out", label: "tokens out", className: "tabular-nums",
          render: (m) => _tokensCell(m.tokens_out),
          value: (m) => m.tokens_out ?? null },
        ...(priced ? [{ key: "cost_usd", label: "costo",
          className: "tabular-nums",
          render: (m) => _usd(m.cost_usd),
          value: (m) => m.cost_usd ?? null }] : []),
      ],
      empty: { title: "sin turnos en el rango",
               sub: "Un run por etapas produce varios turnos (ejecutor + "
                  + "auxiliares). Los runs anteriores al 14/8/2026 solo "
                  + "tienen fila de ejecutor." },
    });
  }
  _tablaModelo.ref.setRows(rows);
}

const _tablaProyecto = { ref: null };

function _renderProyecto(rows) {
  const mount = $("#metrics-by-project");
  if (!mount) return;
  if (!_tablaProyecto.ref) {
    _tablaProyecto.ref = dataTable(mount, {
      searchPlaceholder: "filtrar proyectos…",
      sort: { key: 1, dir: "desc" },  // más runs primero
      columns: [
        { key: "slug", label: "proyecto",
          render: (p) => `<code>${escape(p.slug)}</code>`,
          value: (p) => p.slug || "" },
        { key: "runs", label: "runs", className: "tabular-nums",
          render: (p) => String(p.runs ?? 0), value: (p) => p.runs ?? 0 },
        { key: "tokens_in", label: "tokens in", className: "tabular-nums",
          render: (p) => fmtNum(p.tokens_in ?? 0),
          value: (p) => p.tokens_in ?? 0 },
      ],
      empty: "sin runs en el rango",
    });
  }
  _tablaProyecto.ref.setRows(rows);
}

const _tablaErrores = { ref: null };

function _renderErrores(rows) {
  const mount = $("#metrics-by-errors");
  if (!mount) return;
  if (!_tablaErrores.ref) {
    _tablaErrores.ref = dataTable(mount, {
      searchPlaceholder: "filtrar errores…",
      sort: { key: 1, dir: "desc" },  // más frecuentes primero
      columns: [
        { key: "error_type", label: "tipo",
          render: (e) => `<span class="fail">${escape(e.error_type)}</span>`,
          value: (e) => e.error_type || "" },
        { key: "count", label: "ocurrencias", className: "tabular-nums",
          render: (e) => String(e.count ?? 0), value: (e) => e.count ?? 0 },
      ],
      empty: { title: "sin errores en el rango", sub: "🎉" },
    });
  }
  _tablaErrores.ref.setRows(rows);
}

// Helper: USD formateado, compartido por las tablas que tienen costo.
function _usd(v) {
  return (v === null || v === undefined)
    ? '<span class="text-zinc-600">—</span>'
    : "$" + Number(v).toFixed(2);
}

// Helper: tokens. null/undefined = etapa corrió pero no se midió → "—".
// 0 también → "0" (no es lo mismo que "no se midió"). fmtNum() agrega
// separadores de miles.
function _tokensCell(v) {
  if (v === null || v === undefined)
    return '<span class="text-zinc-600" title="sin medición de tokens">—</span>';
  return fmtNum(v);
}

// ---- gráficos (primitiva compartida: ui-chart.js) ----

// Colores de las dos series de "Runs por día". No son slots categóricos
// sino ESTADOS, así que el color significa algo y va explícito:
//   ok    #059669 — el hue de marca, el mismo que usa Informe
//   error #d95926 — el slot 2 de la paleta
// Verde vs rojo, que sería lo obvio, es justo el par que un deuteranope
// no distingue: medido con el validador de dataviz da ΔE 6,6 (banda de
// warning). Verde vs naranja da 10,2 y pasa los cinco checks:
//   node scripts/validate_palette.js "#059669,#d95926" --mode dark --surface "#18181b" --pairs all
const OK_HUE = "#059669";
const ERR_HUE = "#d95926";

/** Serie diaria de runs, partida en ok / con error. */
function _renderTrend(trends, days) {
  const wrap = $("#metrics-trend");
  if (!wrap) return;
  // Días sin actividad = barra 0. El backend solo devuelve los días que
  // tuvieron runs, así que sin rellenar el eje se comprime y miente
  // sobre el ritmo. (No se usa fillDays de ui-chart.js porque estas
  // filas traen `date` y no `day`.)
  const byDay = new Map((trends || []).map((d) => [d.date, d]));
  const hoy = new Date();
  const serie = [];
  for (let i = days - 1; i >= 0; i--) {
    const key = new Date(hoy.getTime() - i * 86400_000)
      .toISOString().slice(0, 10);
    const d = byDay.get(key) || {};
    const runs = d.runs || 0;
    const err = d.errors || 0;
    serie.push({ ...d, date: key, runs, errors: err,
      ok: d.ok ?? Math.max(runs - err - (d.running || 0) - (d.cancelled || 0) - (d.split || 0), 0) });
  }
  barChart(wrap, {
    points: serie.map((d) => ({ label: d.date.slice(8, 10),
                                values: [d.ok, d.errors], ...d })),
    series: [{ name: "ok", color: OK_HUE },
             { name: "con error", color: ERR_HUE }],
    ariaLabel: `intentos ok y con error por día, últimos ${days} días; actividad, cancelaciones y subdivisiones en el detalle`,
    onEmpty: "sin intentos ok o con error en el rango",
    tooltip: (d) => `<div class="font-medium text-zinc-100">${escape(d.date)}</div>
      <div>runs <span class="tabular-nums">${d.runs}</span></div>
      <div>con error <span class="tabular-nums">${d.errors}</span></div>
      <div>en curso ${d.running || 0} · cancelados ${d.cancelled || 0} · subdivididos ${d.split || 0}</div>
      <div>tokens in <span class="tabular-nums">${fmtNum(d.tokens_in || 0)}</span></div>`,
  });
}

/** Distribución horaria: 24 columnas, una por hora del día. */
function _renderHourly(rows) {
  const wrap = $("#metrics-hourly");
  if (!wrap) return;
  // Mismo criterio que arriba: las horas sin runs son 0, no se saltean.
  // La versión anterior listaba solo las horas con actividad, así que
  // "de 3 a 7 no corre nada" se leía como si esas horas no existieran.
  const byHour = new Map(rows.map((h) => [h.hour, h.runs || 0]));
  barChart(wrap, {
    points: Array.from({ length: 24 }, (_, h) => ({
      label: String(h), hour: h, value: byHour.get(h) || 0 })),
    labelEvery: 3,
    highlight: null,
    ariaLabel: "runs por hora del día (UTC)",
    onEmpty: "sin runs en el rango",
    tooltip: (d) => `<div class="font-medium text-zinc-100">${d.hour}:00 UTC</div>
      <div>runs <span class="tabular-nums">${d.value}</span></div>`,
  });
}

export function initMetrics() {
  onClick("#metrics-refresh", loadMetrics);
  const sel = $("#metrics-days");
  if (sel) sel.onchange = loadMetrics;
  for (const s of Object.values(FILTERS)) {
    const el = $(s);
    if (el) el.onchange = loadMetrics;
  }
  // Rango absoluto: se aplica con un click para no recargar en cada
  // cambio de día (cambiar "desde" no tiene sentido disparar sin que
  // el usuario termine). Escuchar `change` en ambos inputs le da ese
  // mismo comportamiento: el input type=date dispara change al cerrar
  // el calendario o al confirmar.
  const fromEl = $("#metrics-from"), toEl = $("#metrics-to");
  const onRangeChange = () => {
    const f = fromEl?.value?.trim() || "";
    const t = toEl?.value?.trim() || "";
    if (f && t) loadMetrics();
  };
  if (fromEl) fromEl.onchange = onRangeChange;
  if (toEl)   toEl.onchange   = onRangeChange;
  onClick("#metrics-range-apply", () => {
    const f = fromEl?.value?.trim() || "";
    const t = toEl?.value?.trim() || "";
    if (!f || !t) {
      const status = $("#metrics-status");
      if (status) status.textContent = "rango: completá desde y hasta";
      return;
    }
    loadMetrics();
  });
  const rclear = $("#metrics-range-clear");
  onClick("#metrics-range-clear", () => {
    if (fromEl) fromEl.value = "";
    if (toEl)   toEl.value = "";
    rclear.hidden = true;
    loadMetrics();
  });
  onClick("#metrics-clear", () => {
    for (const s of Object.values(FILTERS)) {
      const el = $(s);
      if (el) el.value = "";
    }
    loadMetrics();
  });
  restoreFiltersFromUrl();
}
