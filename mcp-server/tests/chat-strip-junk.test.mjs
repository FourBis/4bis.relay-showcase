// Test de stripJunk() (admin_static/static/tab-chats.js).
// Node puro, sin deps:  node mcp-server/tests/chat-strip-junk.test.mjs
//
// Mismo truco que poller-registry.test.mjs: se carga el módulo por data:
// URL. Acá además se neutralizan los imports (api.js/ui.js tocan el DOM);
// stripJunk es puro, no los usa.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const src = readFileSync(
  new URL("../admin_static/static/tab-chats.js", import.meta.url), "utf8")
  .replace(/^import .*$/gm, "");

const { stripJunk } = await import(
  "data:text/javascript," + encodeURIComponent(src));

// 1. texto normal: intacto (salvo trim)
assert.equal(stripJunk("  ## Plan\n\ntexto  "), "## Plan\n\ntexto");

// 2. bloque de tool call malformado de minimax: fuera entero
const conBasura = `Lo arreglo.
<tool_call>
]<]minimax[>[<invoke name="cbm_query">]<]minimax[>[
]<]minimax[>[</tool_call>`;
assert.equal(stripJunk(conBasura), "Lo arreglo.");

// 3. bloque SIN cierre (el caso que se ve en la DB: la respuesta corta ahí)
assert.equal(stripJunk("Listo.\n<tool_call>\n]<]minimax[>[<invoke"), "Listo.");

// 4. separadores sueltos sin bloque
assert.equal(stripJunk("a ]<]minimax[>[ b"), "a  b");

// 5. turno que era SOLO basura ⇒ vacío (bubbleHtml lo trata como sin texto)
assert.equal(stripJunk("<tool_call>x</tool_call>"), "");

// 6. nada de HTML legítimo se pierde
assert.equal(stripJunk("usa `<div>` acá"), "usa `<div>` acá");

console.log("ok - stripJunk (6 casos)");

// --- el bloque de pregunta no se muestra dos veces (2026-08-16) --------
//
// El relay pega la pregunta al final de la respuesta para Discord y para
// el .md, donde no hay tarjeta. En este chat la tarjeta interactiva se
// pinta justo abajo, así que el texto la duplicaba: la misma decisión en
// prosa y en botones, una arriba de la otra.

const conPregunta = `Miré el deploy y falta pwsh 7.

❓ **¿Instalo PowerShell 7?**

El deploy usa cmdlets que 5.1 no tiene.

- **Instalalo vos**
- **Lo instalo yo**

_Respondé en el chat con la opción que prefieras._

\`q_e2e0001\``;

const limpio = stripJunk(conPregunta);
assert.ok(limpio.startsWith("Miré el deploy"), "conserva la respuesta");
assert.ok(!limpio.includes("¿Instalo PowerShell 7?"), "saca el título");
assert.ok(!limpio.includes("Instalalo vos"), "saca las opciones");
assert.ok(!limpio.includes("q_e2e0001"), "saca el id");

// Un texto que solo MENCIONA algo parecido no se toca.
const sinBloque = "Te dejé una pregunta abierta sobre ❓ el deploy.";
assert.equal(stripJunk(sinBloque), sinBloque, "no muerde texto normal");

// Con el medidor de contexto pegado después (el orden real del relay).
const conMedidor = conPregunta + "\n\n_🧠 contexto: 12% (24k/200k)_";
const l2 = stripJunk(conMedidor);
assert.ok(!l2.includes("Instalalo vos"), "saca la pregunta del medio");
assert.ok(l2.includes("contexto: 12%"), "conserva el medidor");

console.log("ok - stripJunk saca el bloque de pregunta duplicado");
