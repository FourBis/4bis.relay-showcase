// Test de opcionesModelo() (admin_static/static/tab-projects.js).
// Node puro, sin deps:  node --test mcp-server/tests/model-options.test.mjs
//
// Mismo truco que chat-strip-junk.test.mjs: se carga el módulo por data:
// URL neutralizando los imports. Acá el regex tiene que ser multilínea —
// tab-projects.js importa de ui.js en dos renglones y un `^import .*$`
// por línea le dejaría huérfano el resto del bloque.
//
// `escape` viene de api.js, que toca el DOM: se stubea. OJO que NO
// alcanza con dejarlo sin definir, porque `escape` también es una global
// legacy de JS que percent-encodea y las aserciones saldrían raras.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const src = readFileSync(
  new URL("../admin_static/static/tab-projects.js", import.meta.url), "utf8")
  .replace(/^import\b[\s\S]*?from\s+"[^"]+";\s*$/gm, "");

const stub = `const escape = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
            "'": "&#39;" }[c]));\n`;

const { opcionesModelo } = await import(
  "data:text/javascript," + encodeURIComponent(stub + src));

const PRENDIDOS = [
  { spec: "minimax:MiniMax-M3", label: "MiniMax M3", vision: 1 },
  { spec: "nvidia:z-ai/glm-5.2", label: "GLM 5.2", vision: 0 },
];

// 1. Sin valor: queda heredado y nombra cuál es.
const global = opcionesModelo({ valor: "", default: "minimax:MiniMax-M3" },
                              PRENDIDOS);
assert.equal(global.length, 3);
assert.match(global[0], /value=""\s+selected/);
assert.match(global[0], /heredado \(minimax:MiniMax-M3\)/);

// 2. Con un spec prendido: queda seleccionado ese y NO el heredado.
const fijo = opcionesModelo({ valor: "nvidia:z-ai/glm-5.2", default: "x" },
                            PRENDIDOS);
assert.equal(fijo.length, 3);
assert.ok(!/selected/.test(fijo[0]), "el heredado no queda seleccionado");
assert.match(fijo[2], /value="nvidia:z-ai\/glm-5.2"\s+selected/);

// 3. EL CASO: el proyecto tiene un spec que ya no está prendido. Sin una
//    option propia ninguna quedaría selected, el browser caería en la
//    primera y la pantalla diría "heredado" mientras el run usa el spec
//    viejo.
const apagado = opcionesModelo(
  { valor: "deepseek:deepseek-chat", default: "minimax:MiniMax-M3" },
  PRENDIDOS);
assert.equal(apagado.length, 4, "se agrega la option del spec apagado");
assert.ok(!/selected/.test(apagado[0]), "el heredado NO queda seleccionado");
assert.match(apagado[3], /value="deepseek:deepseek-chat"\s+selected/);
assert.match(apagado[3], /apagado en el catálogo/);

// 4. El catálogo vacío (la API de modelos falló) no pierde el valor fijo.
const sinCatalogo = opcionesModelo(
  { valor: "minimax:MiniMax-M3", default: "" }, []);
assert.equal(sinCatalogo.length, 2);
assert.match(sinCatalogo[1], /value="minimax:MiniMax-M3"\s+selected/);
assert.match(sinCatalogo[0], /heredado \(sin definir\)/);

// 5. El label vacío cae al spec: una option sin texto es invisible.
const sinLabel = opcionesModelo({ valor: "", default: "x" },
                                [{ spec: "openai:gpt-5", label: "", vision: null }]);
assert.match(sinLabel[1], />openai:gpt-5</);

console.log("ok - opcionesModelo (5 casos)");
