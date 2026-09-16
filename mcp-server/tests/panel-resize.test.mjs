// Test de los paneles arrastrables. Node puro:
//   node mcp-server/tests/panel-resize.test.mjs
//
// Mismo truco que chat-grafo.test.mjs: se carga el módulo por data: URL
// neutralizando los imports. Casi todo panel-resize.js toca el DOM; lo
// que se prueba acá es lo único que no, y lo único donde un error pasa
// desapercibido — el clamp. Un tope mal puesto no tira excepción: deja
// un panel de 12px o uno que se comió el hilo, y las dos veces parece
// que "el drag anda mal" en vez de que el límite esté mal.
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const src = readFileSync(
  new URL("../admin_static/static/panel-resize.js", import.meta.url), "utf8")
  .replace(/^import .*$/gm, "");

const mod = await import(
  "data:text/javascript;base64," + Buffer.from(src).toString("base64"));
const { anchoValido } = mod;

test("respeta el ancho pedido cuando está dentro de los topes", () => {
  assert.equal(anchoValido("sidebar", 300), 300);
  assert.equal(anchoValido("grafo", 500), 500);
});

test("no deja un panel más chico que usable", () => {
  // Arrastrar hasta el borde izquierdo no puede dejar una columna de
  // 12px: para eso está el collapse, que al menos avisa qué pasó.
  assert.equal(anchoValido("sidebar", 0), 180);
  assert.equal(anchoValido("sidebar", -900), 180);
  assert.equal(anchoValido("grafo", 10), 260);
});

test("no deja que un panel se coma el hilo entero", () => {
  // Sin tope, un arrastre largo deja el hilo en cero y el tirador fuera
  // de la ventana: no se puede volver atrás con el mouse.
  assert.equal(anchoValido("sidebar", 5000), 560);
  assert.equal(anchoValido("grafo", 5000), 900);
});

test("el ancho que deja la ventana le gana al tope del panel", () => {
  // El bug del 2026-08-31: los topes son POR PANEL, así que la lista en
  // su mínimo (180) y el plan en su máximo (900) se podían pedir juntos
  // dentro de una ventana de 950 — y el hilo quedaba en 0px, con el
  // header pintado encima del plan. Con la lista en 180 al plan le
  // quedan 402, no 900.
  assert.equal(anchoValido("grafo", 900, 402), 402);
  assert.equal(anchoValido("sidebar", 560, 300), 300);
  // Con lugar de sobra el techo no cambia nada (es el caso normal).
  assert.equal(anchoValido("grafo", 500, 4000), 500);
});

test("un techo por debajo del mínimo no deja un panel inusable", () => {
  // Ventana tan chica que ni el mínimo entra: gana el mínimo. Un panel
  // de 40px no se usa, y para achicar de verdad ya está el collapse.
  assert.equal(anchoValido("grafo", 900, 40), 260);
  assert.equal(anchoValido("grafo", 900, -500), 260);
  assert.equal(anchoValido("sidebar", 400, 0), 180);
});

test("devuelve enteros: un flex-basis fraccionario tiembla al arrastrar", () => {
  assert.equal(anchoValido("sidebar", 300.6), 301);
  assert.equal(Number.isInteger(anchoValido("grafo", 444.4)), true);
});

test("un panel que no existe no rompe: devuelve 0 (= usar el CSS)", () => {
  assert.equal(anchoValido("no-existe", 300), 0);
});
