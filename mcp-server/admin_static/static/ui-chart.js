// 4bis.relay admin UI — primitiva de graficos (barras).
//
// Sin libreria: divs con height en %. La alternativa era meter Chart.js
// (~200KB) para lo que son 40 lineas de HTML, y el admin es explicito en
// no depender de la red (todo el vendor esta vendorizado).
//
// Nacio como `_renderChart` dentro de tab-report.js — una serie, tokens
// por dia. Se generalizo a N series apiladas para que Metricas y Night
// Runs no vuelvan a escribir su propio chart (que era el estado: un
// "Dashboard de metricas" con cero graficos y dos tablas).
//
// REGLAS QUE NO SON OPCIONALES (skill dataviz; ver el bloque .chart-* de
// admin.src.css para la paleta y su validacion):
//   - Eje de tiempo honesto: los huecos son barra 0, no se saltean. Eso
//     lo arma el CALLER, que es quien sabe el rango.
//   - Un solo eje. Dos medidas de escalas distintas = dos charts.
//   - Legend obligatoria con 2+ series; con una sola NO va.
//   - Label directo solo en el extremo, nunca en cada barra.
//   - El texto nunca se pinta del color de la serie.
//   - Hover con tooltip por defecto, y el target es la COLUMNA entera
//     (mas grande que la marca).
//   - Vista de tabla como equivalente accesible: la pone el caller con
//     un <details> al lado, y por eso el chart es aria-hidden="true"
//     salvo por su aria-label.

import { escape, fmtNum } from "./api.js";

// Orden FIJO de slots (ver admin.src.css). El 9no no se genera.
const SLOTS = 8;

/**
 * barChart(mount, opts) -> void
 *
 * mount   elemento o selector del contenedor (se le agrega .chart).
 * points  [{ label, values: number[] }]  — un valor por serie.
 *         Atajo: [{ label, value }] equivale a values:[value].
 * series  [{ name, color? }] — opcional; default una serie sin nombre.
 *         `color` pisa el slot categórico y es SOLO para series que son
 *         un estado (ok / con error): ahí el color significa algo y el
 *         slot no. Para series comunes no lo uses — el orden fijo de
 *         slots es el que está validado para daltonismo.
 * ariaLabel  descripcion del grafico para el lector de pantalla.
 * format  formateador de numeros (default fmtNum).
 * height  clase de alto (default la del CSS, h-44).
 * labelEvery  cada cuantas columnas se escribe la etiqueta X.
 *         Default automatico segun cuantas entran.
 * highlight  "max" | "last" | null — que columna lleva label directo.
 *         Con varias series apiladas se mide sobre el TOTAL.
 * tooltip  (point, idx) => HTML. Default: label + una linea por serie.
 * onEmpty  texto cuando no hay ni un valor > 0.
 */
export function barChart(mount, {
  points = [],
  series = [{ name: "" }],
  ariaLabel = "",
  format = fmtNum,
  height = "",
  labelEvery = 0,
  highlight = "max",
  tooltip = null,
  onEmpty = "sin datos en el rango",
} = {}) {
  const wrap = typeof mount === "string" ? document.querySelector(mount) : mount;
  if (!wrap) return;
  wrap.classList.add("chart");

  // Normaliza el atajo {label, value} y rellena series faltantes con 0,
  // para que el resto del codigo no tenga que preguntar dos veces.
  const rows = points.map((p) => ({
    label: p.label,
    values: (p.values ?? [p.value ?? 0]).map((v) => Number(v) || 0),
    raw: p,
  }));
  const nSeries = Math.min(series.length, SLOTS);
  const solo = nSeries === 1;
  // Color de cada serie: el slot categórico salvo que la serie traiga
  // el suyo (ver la doc de `series`).
  const hue = (i) => series[i].color || `var(--series-${i + 1})`;

  const totals = rows.map((r) => r.values.reduce((a, b) => a + b, 0));
  const max = Math.max(...totals, 0);
  if (!rows.length || max <= 0) {
    wrap.innerHTML = `<div class="chart-none">${escape(onEmpty)}</div>`;
    return;
  }

  const hiIdx = highlight === "last" ? rows.length - 1
    : highlight === "max" ? totals.reduce((bi, t, i) => (t > totals[bi] ? i : bi), 0)
    : -1;
  // Las etiquetas del eje X no se recortan: si no entran, se escriben
  // salteadas. Recortarlas seria peor que no ponerlas.
  const every = labelEvery
    || (rows.length > 35 ? 7 : rows.length > 14 ? 5 : 1);

  const colHtml = (r, i) => {
    const total = totals[i];
    const isHi = i === hiIdx && total > 0;
    const isLast = i === rows.length - 1;
    // Alto minimo 2% para que un valor chico pero != 0 se vea: una barra
    // de 0px miente y dice "no hubo nada".
    const pct = (v) => (v > 0 ? Math.max(Math.round((v / max) * 100), 2) : 0);
    const body = solo
      ? `<div class="chart-bar${isLast ? " last" : ""}"
              style="height:${pct(total)}%"></div>`
      // Apilada: se pinta de arriba hacia abajo (la ultima serie arriba),
      // asi el orden visual de abajo hacia arriba coincide con el de la
      // legend.
      : `<div class="chart-stack" style="height:${pct(total)}%">
          ${r.values.slice(0, nSeries).map((v, s) => v > 0
            ? `<div class="chart-seg" style="flex:${v} 0 0;background:${hue(s)}"></div>`
            : "").reverse().join("")}
        </div>`;
    return `<div class="chart-col" data-i="${i}">
      ${isHi ? `<div class="chart-toplabel">${escape(format(total))}</div>` : ""}
      ${body}
      <div class="chart-x">${i % every === 0 ? escape(r.label) : "&nbsp;"}</div>
    </div>`;
  };

  const legend = solo ? "" : `<div class="chart-legend">
    ${series.slice(0, nSeries).map((s, i) =>
      `<span class="chart-legend-item">
        <span class="chart-swatch" style="background:${hue(i)}"></span>
        ${escape(s.name)}
      </span>`).join("")}
  </div>`;

  wrap.innerHTML = `<div class="chart-bars ${height}" role="img"
      aria-label="${escape(ariaLabel)}">
    ${rows.map(colHtml).join("")}
  </div>
  ${legend}
  <div class="chart-tooltip" hidden></div>`;

  _wireTooltip(wrap, rows, series.slice(0, nSeries), format, tooltip);
}

// Tooltip por columna. El listener va en el contenedor y no uno por
// barra: con 90 dias son 90 listeners que despues hay que limpiar, y
// este innerHTML se reescribe en cada refresh del poller.
function _wireTooltip(wrap, rows, series, format, custom) {
  const tip = wrap.querySelector(".chart-tooltip");
  const bars = wrap.querySelector(".chart-bars");
  if (!tip || !bars) return;

  bars.addEventListener("mousemove", (e) => {
    const col = e.target.closest(".chart-col");
    if (!col) { tip.hidden = true; return; }
    const i = Number(col.dataset.i);
    const r = rows[i];
    tip.innerHTML = custom ? custom(r.raw, i) : `
      <div class="font-medium text-zinc-100">${escape(r.label)}</div>
      ${series.map((s, k) => `<div>${escape(s.name) || "valor"}
        <span class="tabular-nums">${escape(format(r.values[k] || 0))}</span></div>`).join("")}`;
    tip.hidden = false;
    // Se ancla a la columna y se frena contra el borde derecho, para que
    // el tooltip de la ultima barra no se corte fuera de la card.
    const cr = col.getBoundingClientRect();
    const wr = wrap.getBoundingClientRect();
    tip.style.left = Math.max(0, Math.min(cr.left - wr.left, wr.width - tip.offsetWidth - 4)) + "px";
    tip.style.top = "0px";
  });
  bars.addEventListener("mouseleave", () => { tip.hidden = true; });
}

/**
 * Serie de dias sin huecos: los dias sin actividad son barra 0, no se
 * saltean. Un eje de tiempo con huecos comprimidos miente sobre el ritmo.
 * `rows` trae objetos con `.day` = "YYYY-MM-DD".
 */
export function fillDays(rows, days, empty = {}) {
  const byDay = new Map(rows.map((d) => [d.day, d]));
  const out = [];
  const today = new Date();
  for (let i = days - 1; i >= 0; i--) {
    const key = new Date(today.getTime() - i * 86400_000)
      .toISOString().slice(0, 10);
    out.push({ day: key, ...empty, ...(byDay.get(key) || {}) });
  }
  return out;
}
