// Test de chat-tools.js (paleta de comandos + parser).
// Node puro, sin deps:  node mcp-server/tests/chat-tools.test.mjs
//
// Mismo truco que chat-strip-junk.test.mjs: se carga el módulo por data:
// URL neutralizando los imports (api.js/ui.js tocan el DOM). Lo que se
// prueba acá es lo que NO toca el DOM: el registro de comandos, el
// matcher y la ayuda. El render de la tarjeta de pregunta necesita DOM
// real y se ejercita a mano en la Admin UI.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const src = readFileSync(
  new URL("../admin_static/static/chat-tools.js", import.meta.url), "utf8")
  .replace(/^import .*$/gm, "")
  // `escape` y `toast` vienen de los imports que acabamos de sacar.
  .replace(/^/, "const escape = (s) => String(s); const toast = () => {};\n");

const mod = await import(
  "data:text/javascript;base64," + Buffer.from(src).toString("base64"));

const { chatCommands, matchCommand, helpText } = mod;

// ---- el registro está sano ----

assert.ok(chatCommands.length >= 8, "hay comandos registrados");
for (const c of chatCommands) {
  assert.ok(c.name.startsWith("/"), `${c.name} empieza con /`);
  assert.equal(typeof c.desc, "string", `${c.name} tiene descripción`);
  assert.ok(c.desc.length > 5, `${c.name}: la descripción dice algo`);
  assert.equal(typeof c.run, "function", `${c.name} tiene run()`);
}
const nombres = chatCommands.map((c) => c.name);
assert.equal(new Set(nombres).size, nombres.length, "sin comandos duplicados");

// ---- el matcher ----

assert.equal(matchCommand("hola qué tal"), null, "texto normal no es comando");
assert.equal(matchCommand(""), null, "vacío no es comando");
assert.equal(matchCommand("!proyectos"), null,
  "los !comando del relay NO los intercepta la UI");
assert.equal(matchCommand("/noexiste"), null, "comando desconocido → null");

const compactar = matchCommand("/compactar");
assert.ok(compactar, "/compactar matchea");
assert.equal(compactar.cmd.name, "/compactar");
assert.equal(compactar.args, "");

const explicar = matchCommand("/explicar experts.py:_slim_history");
assert.equal(explicar.cmd.name, "/explicar");
assert.equal(explicar.args, "experts.py:_slim_history",
  "los argumentos llegan enteros");

assert.ok(matchCommand("  /parar  "), "tolera espacios alrededor");
assert.ok(matchCommand("/PARAR"), "el nombre no distingue mayúsculas");

// ---- el contrato de run(): false corta, string se expande ----

let llamado = false;
const ctx = {
  compactar: () => { llamado = true; },
  cerrar: () => {}, parar: () => {}, verDiff: () => {}, mostrarAyuda: () => {},
};

assert.equal(matchCommand("/compactar").cmd.run(ctx), false,
  "un comando de acción devuelve false: no se manda nada al experto");
assert.ok(llamado, "y ejecutó la acción del panel");

const expandido = matchCommand("/continuar").cmd.run(ctx);
assert.equal(typeof expandido, "string", "/continuar se expande a un prompt");
assert.ok(expandido.length > 0);

const conArgs = matchCommand("/explicar el pool de MCPs");
assert.ok(conArgs.cmd.run(ctx, conArgs.args).includes("el pool de MCPs"),
  "el argumento entra en el prompt expandido");

// Sin argumento, /explicar sigue siendo útil (no manda un prompt vacío).
assert.ok(matchCommand("/explicar").cmd.run(ctx, "").length > 20);

// ---- la ayuda lista todo ----

const ayuda = helpText();
for (const c of chatCommands) {
  assert.ok(ayuda.includes(c.name), `la ayuda menciona ${c.name}`);
}
assert.ok(ayuda.includes("!comando"),
  "la ayuda aclara que los !comando del relay siguen andando");

console.log(`ok — ${chatCommands.length} comandos, matcher y ayuda`);

// ---- /noche: el comando y, sobre todo, sus argumentos ----
//
// El bug que esto fija (2026-08-27): `matchCommand` hacia
// `split(/\s+/)` + `join(" ")`, que aplasta los saltos de linea. La
// directiva del modo nocturno se pasa por ahi, y el planificador
// (`_POINT_RE` en night.py) detecta los puntos enumerados SOLO a
// principio de linea. Aplastada, la directiva se queda con un solo
// punto detectable y el chequeo de cobertura —que existe para avisar
// que el LLM se olvido de un punto— deja de servir. En silencio.

const noche = chatCommands.find((c) => c.name === "/noche");
assert.ok(noche, "el comando /noche esta registrado");
assert.ok(noche.args, "/noche declara que toma argumentos");

const DIRECTIVA = [
  "Objetivo: cerrar los huecos que quedan.",
  "",
  "P1. Limpiar el root del repo.",
  "P2. Parametrizar el SQL de EfUnitOfWork.",
  "P3. Actualizar README.md.",
].join("\n");

const hit = matchCommand("/noche " + DIRECTIVA);
assert.ok(hit, "matchea aunque la directiva sea multilinea");
assert.equal(hit.cmd.name, "/noche");
assert.ok(hit.args.includes("\n"),
  "los saltos de linea SOBREVIVEN al parser");
assert.equal(hit.args, DIRECTIVA, "los args llegan tal cual");

// La prueba que importa: los tres puntos siguen siendo detectables con
// la MISMA regla que usa night.py — principio de linea.
const puntos = hit.args.split(/\n/).filter((l) => /^\s*P\d/.test(l));
assert.equal(puntos.length, 3,
  "los 3 puntos arrancan en su propia linea (si esto baja a 1, el " +
  "planificador pierde la cobertura)");

// Un comando de una sola linea se comporta igual que antes.
assert.equal(matchCommand("/explicar files.py").args, "files.py");
assert.equal(matchCommand("/noche").args, "", "sin args, args es vacio");

// `run` no puede devolver una promesa: el dispatcher de tab-chats.js
// hace `hit.cmd.run(...)` SIN await, y `String(promesa)` le mandaria
// "[object Promise]" al experto como si fuera un prompt.
for (const c of chatCommands) {
  const esAsync = c.run.constructor.name === "AsyncFunction";
  assert.ok(!esAsync, `${c.name}: run() no puede ser async (el ` +
    "dispatcher no hace await; devolve false y disparalo aparte)");
}

console.log("chat-tools: /noche ok");

// ---- /noche @plantilla ----
//
// El `@nombre` tiene que exigir la palabra ENTERA. Si alcanzara con
// "empieza con @", una directiva que arranca mencionando a alguien se
// mandaria como nombre de plantilla y el run fallaria con un 404 raro.

const cmds = chatCommands.map((c) => c.name);
assert.ok(cmds.includes("/plantillas"), "/plantillas registrado");

const esPlantilla = (txt) => /^@[\w.-]+$/.test(matchCommand(txt).args);

assert.ok(esPlantilla("/noche @release"), "@release es plantilla");
assert.ok(esPlantilla("/noche @release-sample-app"), "acepta guiones");
assert.ok(!esPlantilla("/noche @release y ademas limpiar el root"),
  "con mas palabras es directiva, no plantilla");
assert.ok(!esPlantilla("/noche P1. limpiar."), "una directiva normal no es plantilla");

console.log("chat-tools: /plantillas ok");
