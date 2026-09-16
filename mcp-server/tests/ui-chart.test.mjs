// Test de la primitiva de gráficos (admin_static/static/ui-chart.js).
// Node puro, sin deps:  node --test mcp-server/tests/ui-chart.test.mjs
//
// barChart escribe HTML en un elemento; acá se le pasa un elemento
// falso y se afirma sobre el string. No hace falta un DOM: lo que se
// prueba es la GEOMETRÍA (alturas en %, qué barra lleva label, cada
// cuánto va la etiqueta del eje) y las reglas de dataviz que son fáciles
// de romper sin darse cuenta — legend con 2+ series y nunca con una,
// slots de color en orden fijo, y que un valor chico pero != 0 no se
// dibuje como cero.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const src = readFileSync(
  new URL("../admin_static/static/ui-chart.js", import.meta.url), "utf8")
  .replace(/^import\b[\s\S]*?from\s+"[^"]+";\s*$/gm, "");

const stub = `const escape = (s) => String(s ?? "");
const fmtNum = (n) => String(n);\n`;

const { barChart, fillDays } = await import(
  "data:text/javascript," + encodeURIComponent(stub + src));

// Elemento falso: querySelector devuelve null, así que el wire del
// tooltip corta solo y no hace falta emular eventos.
function render(opts) {
  const el = { innerHTML: "", classList: { add() {} }, querySelector: () => null };
  barChart(el, opts);
  return el.innerHTML;
}

const pts = (...vals) => vals.map((v, i) => ({ label: String(i), value: v }));

test("sin datos (o todo en cero) muestra el vacío, no un chart en blanco", () => {
  assert.match(render({ points: [] }), /chart-none/);
  assert.match(render({ points: pts(0, 0, 0) }), /chart-none/);
});

test("la barra más alta es 100% y las demás proporcionales", () => {
  const html = render({ points: pts(50, 100, 25) });
  const alturas = [...html.matchAll(/class="chart-bar[^"]*"\s*\n?\s*style="height:(\d+)%"/g)]
    .map((m) => Number(m[1]));
  assert.deepEqual(alturas, [50, 100, 25]);
});

test("un valor chico pero distinto de cero se ve; el cero no", () => {
  // 1 sobre 1000 redondea a 0%: sin el piso, una barra que SÍ tuvo
  // actividad se dibujaría igual que un día muerto.
  const html = render({ points: pts(1000, 1, 0) });
  const alturas = [...html.matchAll(/style="height:(\d+)%"/g)].map((m) => Number(m[1]));
  assert.deepEqual(alturas, [100, 2, 0]);
});

test("label directo solo en el extremo, nunca en cada barra", () => {
  const html = render({ points: pts(5, 90, 12) });
  const labels = [...html.matchAll(/chart-toplabel">([^<]+)</g)].map((m) => m[1]);
  assert.deepEqual(labels, ["90"]);
  assert.equal([...html.matchAll(/chart-toplabel/g)].length, 1);
  assert.equal(render({ points: pts(5, 90, 12), highlight: null })
    .includes("chart-toplabel"), false);
});

test("la última columna se distingue (.last)", () => {
  const html = render({ points: pts(5, 9, 12) });
  assert.equal([...html.matchAll(/chart-bar last/g)].length, 1);
  assert.match(html.slice(html.lastIndexOf("chart-col")), /chart-bar last/);
});

test("una sola serie NO lleva legend; dos o más SÍ", () => {
  const uno = render({ points: pts(1, 2) });
  assert.equal(uno.includes("chart-legend"), false);

  const dos = render({
    points: [{ label: "a", values: [3, 1] }, { label: "b", values: [2, 2] }],
    series: [{ name: "entrada" }, { name: "salida" }],
  });
  assert.match(dos, /chart-legend/);
  assert.equal([...dos.matchAll(/chart-legend-item/g)].length, 2);
  assert.match(dos, /entrada/);
  assert.match(dos, /salida/);
});

test("los slots de color van en orden fijo y se cortan en 8", () => {
  const n = 10;
  const html = render({
    points: [{ label: "a", values: Array(n).fill(1) }],
    series: Array.from({ length: n }, (_, i) => ({ name: "s" + i })),
  });
  const slots = [...html.matchAll(/var\(--series-(\d+)\)/g)].map((m) => Number(m[1]));
  const enSegmentos = slots.slice(0, 8);
  // Los segmentos se pintan de arriba hacia abajo, así que el orden en
  // el HTML es el inverso del de la legend: 8..1 y después 1..8.
  assert.deepEqual(enSegmentos, [8, 7, 6, 5, 4, 3, 2, 1]);
  assert.deepEqual(slots.slice(8), [1, 2, 3, 4, 5, 6, 7, 8]);
  assert.equal(slots.includes(9), false, "generó un 9no slot");
  assert.equal([...html.matchAll(/chart-legend-item/g)].length, 8);
});

test("con muchas columnas las etiquetas del eje se saltean, no se recortan", () => {
  const muchos = Array.from({ length: 30 }, (_, i) => ({ label: "d" + i, value: i + 1 }));
  const html = render({ points: muchos });
  const xs = [...html.matchAll(/chart-x">([^<]+)</g)].map((m) => m[1]);
  assert.equal(xs.length, 30);
  const visibles = xs.filter((x) => x !== "&nbsp;");
  assert.deepEqual(visibles, ["d0", "d5", "d10", "d15", "d20", "d25"]);
});

test("fillDays no deja huecos: los días sin actividad son cero", () => {
  const hoy = new Date().toISOString().slice(0, 10);
  const serie = fillDays([{ day: hoy, runs: 7 }], 5, { runs: 0, tokens_in: 0 });
  assert.equal(serie.length, 5);
  assert.equal(serie[4].day, hoy);
  assert.equal(serie[4].runs, 7);
  assert.equal(serie[0].runs, 0, "el día sin dato no quedó en cero");
  assert.equal(serie[0].tokens_in, 0);
  // días consecutivos y ordenados
  const dias = serie.map((d) => d.day);
  assert.deepEqual(dias, [...dias].sort());
  assert.equal(new Set(dias).size, 5);
});

test("una serie puede traer su propio color (estados) sin romper los slots", () => {
  const html = render({
    points: [{ label: "a", values: [3, 1] }],
    series: [{ name: "ok", color: "#059669" }, { name: "con error" }],
  });
  // la que trae color usa el suyo; la que no, su slot categórico
  assert.match(html, /background:#059669/);
  assert.match(html, /var\(--series-2\)/);
  assert.equal(html.includes("var(--series-1)"), false, "pisó el slot 1 igual");
  // y la legend usa el mismo color que el segmento, no otro
  const legend = html.slice(html.indexOf("chart-legend"));
  assert.match(legend, /chart-swatch" style="background:#059669/);
});
