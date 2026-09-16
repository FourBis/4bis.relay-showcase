// Test del poller registry (admin_static/static/pollers.js).
// Node puro, sin deps:  node mcp-server/tests/poller-registry.test.mjs
//
// Importa el modulo via data: URL (el repo no tiene package.json con
// type:module y pollers.js debe seguir siendo .js para el browser).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// --- stubs: reloj manual + intervalo manual + document.hidden ---
let now = 0;
const realNow = Date.now;
Date.now = () => now;
let hidden = false;
globalThis.document = { get hidden() { return hidden; } };
let tickFn = null;
globalThis.setInterval = (fn) => { tickFn = fn; return 1; };

const src = readFileSync(
  new URL("../admin_static/static/pollers.js", import.meta.url), "utf8");
const { initPollers, registerPoller } = await import(
  "data:text/javascript," + encodeURIComponent(src));

let active = "status";
initPollers(() => active);
const flush = () => new Promise((r) => setImmediate(r));

// 1. dispara al vencer, no antes; respeta el período
let n1 = 0;
registerPoller(() => n1++, 1000);
tickFn(); assert.equal(n1, 0, "disparó antes del vencimiento");
now += 1000; tickFn(); assert.equal(n1, 1);
now += 500; tickFn(); assert.equal(n1, 1, "no respetó el período");
now += 500; tickFn(); assert.equal(n1, 2);

// 2. pausa total con la pestaña oculta
hidden = true;
now += 2000; tickFn(); assert.equal(n1, 2, "disparó con pestaña oculta");
hidden = false;

// 3. tabId solo dispara con su tab activo
let n3 = 0;
registerPoller(() => n3++, 1000, { tabId: "logs" });
now += 1000; tickFn(); assert.equal(n3, 0, "tab inactivo disparó");
active = "logs";
tickFn(); assert.equal(n3, 1, "tab activo no disparó");

// 4. unregister frena al poller
const un = registerPoller(() => n3++, 1000, { tabId: "logs" });
un();
now += 1000; tickFn(); await flush();
assert.equal(n3, 2, "unregister no frenó");

// 5. throw sync o reject async de un callback no frenan a los demás
let n5 = 0;
registerPoller(() => { throw new Error("boom sync"); }, 1000);
registerPoller(() => Promise.reject(new Error("boom async")), 1000);
registerPoller(() => n5++, 1000);
now += 1000; tickFn(); await flush();
assert.equal(n5, 1, "un poller roto frenó al resto");

// Workspace: refrescar todas las herramientas visibles, no solo la enfocada.
active = new Set(["chat", "logs"]);
let chatTicks = 0;
registerPoller(() => chatTicks++, 1000, { tabId: "chat" });
now += 1000; tickFn();
assert.equal(chatTicks, 1);
const beforeMinimize = n3;
active.delete("logs");
now += 1000; tickFn();
assert.equal(n3, beforeMinimize, "una ventana minimizada siguió refrescándose");
assert.equal(chatTicks, 2);

Date.now = realNow;
console.log("poller-registry: todos los checks pasaron");
