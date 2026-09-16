// Test de dtView (admin_static/static/ui-table.js): filtrar -> ordenar
// -> paginar, que es la única lógica no trivial de la tabla compartida.
// Node puro, sin deps:  node --test mcp-server/tests/ui-table.test.mjs
//
// Se importa por data: URL neutralizando los imports (api.js/ui.js tocan
// el DOM). dtView no usa nada de ellos, así que no hace falta stub.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const src = readFileSync(
  new URL("../admin_static/static/ui-table.js", import.meta.url), "utf8")
  .replace(/^import\b[\s\S]*?from\s+"[^"]+";\s*$/gm, "");

const { dtView } = await import(
  "data:text/javascript," + encodeURIComponent(src));

const COLS = [
  { key: "slug" },
  { key: "runs" },
  { key: "ultimo", value: (r) => r.ultimo || null },
];
const ROWS = [
  { slug: "sample-app", runs: 74, ultimo: "2026-08-30" },
  { slug: "InventoryDemo", runs: 9, ultimo: "" },
  { slug: "4bis-relay", runs: 120, ultimo: "2026-08-31" },
  { slug: "inventory-alpha", runs: 9, ultimo: null },
];
const st = (o) => ({ rows: ROWS, query: "", sortKey: null, sortDir: "asc",
                     page: 0, ...o });

test("sin filtro ni orden devuelve todo en el orden original", () => {
  const v = dtView(COLS, st(), 0);
  assert.equal(v.all.length, 4);
  assert.equal(v.pages, 1);
  assert.equal(v.slice[0].slug, "sample-app");
});

test("el filtro es case-insensitive y mira todas las columnas", () => {
  assert.equal(dtView(COLS, st({ query: "INVENTORYDEMO" }), 0).all.length, 1);
  // 9 aparece en la columna numérica de dos filas
  assert.equal(dtView(COLS, st({ query: "9" }), 0).all.length, 2);
  assert.equal(dtView(COLS, st({ query: "nada" }), 0).all.length, 0);
});

test("ordena números como números, no como texto", () => {
  const asc = dtView(COLS, st({ sortKey: 1 }), 0).all.map((r) => r.runs);
  assert.deepEqual(asc, [9, 9, 74, 120]);
  const desc = dtView(COLS, st({ sortKey: 1, sortDir: "desc" }), 0)
    .all.map((r) => r.runs);
  assert.deepEqual(desc, [120, 74, 9, 9]);
});

test("ordena texto ignorando mayúsculas (InventoryDemo no se va al final)", () => {
  const asc = dtView(COLS, st({ sortKey: 0 }), 0).all.map((r) => r.slug);
  assert.deepEqual(asc, ["4bis-relay", "inventory-alpha", "InventoryDemo", "sample-app"]);
});

test("los vacíos van al final en LAS DOS direcciones", () => {
  for (const sortDir of ["asc", "desc"]) {
    const got = dtView(COLS, st({ sortKey: 2, sortDir }), 0).all;
    const vacios = got.slice(2).map((r) => r.ultimo);
    assert.deepEqual(vacios.map((v) => !v), [true, true],
      `dir=${sortDir}: los vacíos no quedaron al final`);
  }
});

test("no muta el array del caller (los pollers reusan el mismo)", () => {
  const antes = ROWS.map((r) => r.slug);
  dtView(COLS, st({ sortKey: 1, sortDir: "desc" }), 0);
  assert.deepEqual(ROWS.map((r) => r.slug), antes);
});

test("pagina y corrige la página que se quedó sin filas al filtrar", () => {
  const v1 = dtView(COLS, st({ page: 1 }), 2);
  assert.equal(v1.pages, 2);
  assert.equal(v1.slice.length, 2);
  assert.equal(v1.page, 1);
  // filtrar a 1 fila deja la página 1 inexistente -> vuelve a la 0 con
  // contenido, no a una tabla vacía
  const v2 = dtView(COLS, st({ page: 1, query: "inventorydemo" }), 2);
  assert.equal(v2.pages, 1);
  assert.equal(v2.page, 0);
  assert.equal(v2.slice.length, 1);
});

test("pageSize 0 = sin paginar", () => {
  const v = dtView(COLS, st({ page: 3 }), 0);
  assert.equal(v.slice.length, 4);
  assert.equal(v.page, 0);
});

test("sin filas devuelve 1 página vacía, no 0 páginas", () => {
  const v = dtView(COLS, st({ rows: [] }), 25);
  assert.equal(v.pages, 1);
  assert.equal(v.page, 0);
  assert.deepEqual(v.slice, []);
});

test("sortKey 0 es un índice válido, no 'sin orden'", () => {
  // Regresión: con `sort?.key || null` la columna 0 se caía a null y la
  // primera columna nunca podía ser el orden inicial.
  const v = dtView(COLS, st({ sortKey: 0 }), 0);
  assert.equal(v.all[0].slug, "4bis-relay");
});

