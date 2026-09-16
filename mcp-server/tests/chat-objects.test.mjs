// Check puro de referencias y extracción local de tablas para objetos del chat.
// node --test mcp-server/tests/chat-objects.test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const src = readFileSync(
  new URL("../admin_static/static/chat-objects.js", import.meta.url), "utf8")
  .replace(/^import .*$/gm, "");
const { cleanMarkdown, objectReference, validObjectReference, tableText } = await import(
  "data:text/javascript," + encodeURIComponent(src));

test("la referencia es pequeña y no guarda contenido de la respuesta", () => {
  const ref = objectReference("conv-123", 4, "table", 1);
  assert.deepEqual(ref, {
    conversationId: "conv-123", messageIndex: 4, kind: "table", itemIndex: 1,
  });
  assert.equal(Object.keys(ref).includes("content"), false);
  assert.equal(validObjectReference(ref), true);
});

test("rechaza referencias incompletas o con índices inválidos", () => {
  assert.equal(validObjectReference(null), false);
  assert.equal(validObjectReference({
    conversationId: "conv", messageIndex: -1, kind: "response", itemIndex: 0,
  }), false);
  assert.equal(validObjectReference({
    conversationId: "conv", messageIndex: 0, kind: "html", itemIndex: 0,
  }), false);
});

test("tableText copia encabezados y filas como TSV", () => {
  const table = {
    rows: [
      { cells: [{ textContent: " Nombre " }, { textContent: "Estado" }] },
      { cells: [{ textContent: "  Ana\n" }, { textContent: "ok" }] },
    ],
  };
  assert.equal(tableText(table), "Nombre\tEstado\nAna\tok");
});

test("cleanMarkdown elimina basura de protocolo sin tocar HTML textual", () => {
  assert.equal(cleanMarkdown("Listo.\n<tool_call>ruido</tool_call>"), "Listo.");
  assert.equal(cleanMarkdown("usa `<div>` como ejemplo"), "usa `<div>` como ejemplo");
});

console.log("chat-objects: ok");
