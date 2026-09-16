// Test de la tarjeta de tool call de tab-chats.js.
// Node puro, sin deps:  node mcp-server/tests/chat-toolcard.test.mjs
//
// Mismo truco que chat-tools.test.mjs / chat-strip-junk.test.mjs: el
// módulo se carga por data: URL con los imports neutralizados (api.js y
// ui.js tocan el DOM) y con un export agregado al final para llegar a
// las funciones internas. Solo hace falta stubear `escape`: el resto de
// lo importado vive dentro de handlers que acá no corren.
//
// Qué cubre y por qué: el pedido del 2026-08-27 fue *"que ahí escriba el
// comando encerrado y colapsable para ver qué comandos va usando, para
// saber si va bien o mal, y qué está respondiendo la terminal"*. Son
// tres cosas y se verifican las tres — el comando entero en el cuerpo,
// la salida de la terminal, y el veredicto (exit) visible SIN abrir.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// La inserción va con función de reemplazo y no con string: en un string
// de reemplazo `$$` significa un `$` literal, y este stub tiene `$$`.
const STUB = `
const escape = (s) => String(s ?? "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const $ = () => null, $$ = () => [], on = () => {};
`;

const src = readFileSync(
  new URL("../admin_static/static/tab-chats.js", import.meta.url), "utf8")
  .replace(/^import .*$/gm, "")
  .replace(/^/, () => STUB)
  + "\nexport { toolCardHtml, exitBadge, runOutcomeHtml };\n";

const mod = await import(
  "data:text/javascript;base64," + Buffer.from(src).toString("base64"));
const { toolCardHtml, exitBadge, runOutcomeHtml } = mod;

assert.match(exitBadge("(background activo; disponibilidad sin verificar)"), /disponibilidad sin verificar/);
assert.equal(runOutcomeHtml({}), "");
assert.match(runOutcomeHtml({run_status: "ok"}), /Cumplimiento sin confirmar/);
assert.match(runOutcomeHtml({run_status: "ok"}), /Sin verificar/);
assert.doesNotMatch(runOutcomeHtml({run_status: "error", stages: {resultado: "aprobado"}}), /Tarea cumplida/);
assert.match(runOutcomeHtml({run_status: "ok", stages: {resultado: "aprobado", verifier_verdict: "complete"}}), /Tarea cumplida/);
const pending = runOutcomeHtml({run_status: "ok", export_pending: true, export_error: '" onmouseover="oops',
  stages: {resultado: "pendiente", verifier_verdict: "needs_more"}});
assert.match(pending, /Trabajo pendiente/);
assert.match(pending, /Exportación pendiente/);
assert.doesNotMatch(pending, /title="" onmouseover=/);

// ---- el veredicto se ve sin abrir la tarjeta ----

assert.ok(exitBadge("todo bien\n(exit=0)").includes("✓"), "exit=0 → ✓");
assert.ok(exitBadge("boom\n(exit=1)").includes("exit=1"), "exit=1 se nombra");
assert.match(exitBadge("boom\n(exit=1)"), /#f87171/, "exit≠0 va en rojo");
assert.match(exitBadge("ok\n(exit=0)"), /#4ade80/, "exit=0 va en verde");
assert.equal(exitBadge("sin veredicto"), "", "sin exit no inventa badge");
// El ÚLTIMO exit manda: la salida de un script encadenado trae varios.
assert.match(exitBadge("(exit=0)\nsigo\n(exit=2)"), /exit=2/, "gana el último");

// ---- el comando entero y la salida están en el cuerpo ----

const card = toolCardHtml({
  tool: "shell",
  summary: "⚙️ shell: `dotnet build`",
  cmd: "dotnet build -c Release\n# cwd: src",
  output: "error CS0103: 'Foo'\n(exit=1)",
});
assert.match(card, /^<details/, "con cuerpo → colapsable");
assert.ok(card.includes("dotnet build -c Release"), "el comando entero está");
assert.ok(card.includes("# cwd: src"), "el cwd viaja con el comando");
assert.ok(card.includes("error CS0103"), "la salida de la terminal está");
assert.ok(card.includes("exit=1"), "el veredicto está en el encabezado");

// ---- higiene: nada de lo que viene del modelo se interpola crudo ----

const xss = toolCardHtml({
  tool: "shell", summary: "⚙️ shell",
  cmd: "<img src=x onerror=alert(1)>",
  output: "</pre><script>alert(2)</script>",
});
assert.ok(!xss.includes("<img"), "el comando va escapado");
assert.ok(!xss.includes("<script>"), "la salida va escapada");

// ---- una tool sin cuerpo sigue siendo tarjeta plana ----

const plana = toolCardHtml({ tool: "read_file", summary: "📄 leyó `a.py`" });
assert.ok(!plana.startsWith("<details"),
  "sin cuerpo no invita a un click vacío");
assert.ok(plana.includes("ctc-flat"), "usa la tarjeta plana de siempre");

// ---- el diff de edit_file no se perdió por el camino ----

const conDiff = toolCardHtml({
  tool: "edit_file", summary: "✏️ editó `a.py` (+1 −1)",
  diff: "-viejo\n+nuevo",
});
assert.ok(conDiff.includes("ctc-diff"), "el diff sigue coloreado");
assert.ok(conDiff.includes("diff-add"), "las líneas + siguen en verde");

console.log("chat-toolcard: ok");
