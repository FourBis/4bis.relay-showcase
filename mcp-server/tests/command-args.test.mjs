// Test de ejemploArgs() (admin_static/static/tab-commands.js).
//
// El runner "probar un comando" tenía un input `project (opcional)` que
// el backend leía y tiraba: el campo no hacía nada. Y no se podía
// arreglar pasándolo, porque cada comando pide su proyecto con SU clave
// (`build` → project, `memoria` → target, `cancel` → chat). El input se
// sacó y el placeholder pasó a salir del args_schema del comando
// elegido, que es el dato que sí sabe cuál es la clave.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const src = readFileSync(
  new URL("../admin_static/static/tab-commands.js", import.meta.url), "utf8")
  .replace(/^import\b[\s\S]*?from\s+"[^"]+";\s*$/gm, "");

const { ejemploArgs } = await import(
  "data:text/javascript," + encodeURIComponent(src));

// 1. El caso que motivó todo: la clave es `project` para build...
assert.equal(
  ejemploArgs({ name: "build",
                args_schema: { properties: { project: { type: "string" } },
                               required: ["project"] } }),
  '{"project":"<project>"}');

// 2. ...y `target` para memoria. Un ejemplo fijo mentía para uno de los dos.
assert.match(
  ejemploArgs({ name: "memoria",
                args_schema: { properties: { target: { type: "string" },
                                             query: { type: "string" } },
                               required: ["target"] } }),
  /"target":"<target>"/);

// 3. Lo que no es obligatorio se nombra como opcional en vez de
//    aparecer como si hiciera falta.
assert.match(
  ejemploArgs({ args_schema: { properties: { name: { type: "string" },
                                             limit: { type: "integer" } },
                               required: ["name"] } }),
  /\(opcionales: limit\)/);

// 4. Los números no van como "<limit>": el campo es JSON y un string
//    donde va un entero se rechaza al parsear.
assert.match(
  ejemploArgs({ args_schema: { properties: { limit: { type: "integer" } } } }),
  /"limit":10/);

// 5. Un comando sin args lo dice, en vez de mostrar "{}" y dejar
//    dudando si falta algo.
assert.equal(ejemploArgs({ name: "proyectos", args_schema: {} }), "sin args");
assert.equal(ejemploArgs({ name: "ayuda" }), "sin args");
assert.equal(ejemploArgs(undefined), "sin args", "sin comando elegido");

// 6. Lo que sale tiene que ser JSON parseable: es lo que el humano copia
//    al campo.
const ej = ejemploArgs({ args_schema: { properties: { chat: { type: "string" } },
                                        required: ["chat"] } });
assert.deepEqual(JSON.parse(ej), { chat: "<chat>" });

console.log("ok - ejemploArgs (6 casos)");
