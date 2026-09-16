// Contrato del selector global de roles: no pierde un valor guardado si el
// backend omite model_roles.keys, y lo marca apagado aunque se conserve.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const src = readFileSync(
  new URL("../admin_static/static/tab-config.js", import.meta.url), "utf8")
  .replace(/^import\b[\s\S]*?from\s+"[^"]+";\s*$/gm, "");

const { roleModelState } = await import(
  "data:text/javascript," + encodeURIComponent(src));

const catalogo = [{ spec: "minimax:MiniMax-M3", label: "MiniMax M3" }];

// Si el contrato nuevo/partial no manda keys, usa la clave conocida y no
// borra el valor al guardar. Que se conserve no lo vuelve ejecutable.
const fallback = roleModelState({
  config: { FOURBIS_PLANNER_MODEL: "deepseek:deepseek-flash" },
  model_roles: { keys: {}, effective: { planner: "deepseek:deepseek-flash" } },
}, catalogo, "planner");
assert.equal(fallback.key, "FOURBIS_PLANNER_MODEL");
assert.equal(fallback.guardado, "deepseek:deepseek-flash");
assert.ok(fallback.specs.includes("deepseek:deepseek-flash"));
assert.equal(fallback.apagado, true);

// Si el catálogo no respondió, desconocemos si el modelo está apagado.
assert.equal(
  roleModelState({
    config: {}, model_roles: { keys: {}, effective: { planner: "x" } },
  }, [], "planner", false).apagado,
  false,
);

// Una clave entregada por el backend tiene precedencia y un modelo del
// catálogo no se señala como apagado.
const dynamic = roleModelState({
  config: { CUSTOM_PLANNER: "minimax:MiniMax-M3" },
  model_roles: { keys: { planner: "CUSTOM_PLANNER" },
                 effective: { planner: "minimax:MiniMax-M3" } },
}, catalogo, "planner");
assert.equal(dynamic.key, "CUSTOM_PLANNER");
assert.equal(dynamic.apagado, false);

console.log("ok - role model state fallback and catalog warning");
