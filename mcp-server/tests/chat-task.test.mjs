import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const src = readFileSync(
  new URL("../admin_static/static/chat-task.js", import.meta.url), "utf8")
  .replace(/^import .*$/gm, "");
const { taskActionLabel } = await import(
  "data:text/javascript," + encodeURIComponent(src));

assert.equal(taskActionLabel("continue"), "Continuar");
assert.equal(taskActionLabel("pause"), "Pausar");
assert.equal(taskActionLabel("cancel"), "Cancelar");
assert.equal(taskActionLabel("publish"), "Publicar PR");
assert.equal(taskActionLabel("unknown"), "unknown");
console.log("ok - chat task action labels");

const calls = [];
let poll = null;
globalThis.apiRoot = async (...args) => { calls.push(args); return {}; };
globalThis.toast = () => {};
globalThis.confirmModal = async () => false;
globalThis.registerPoller = (fn) => { poll = fn; return () => {}; };
const panel = {
  hidden: false,
  innerHTML: "",
  querySelectorAll: () => [],
  querySelector: () => null,
};
globalThis.document = { getElementById: () => panel };
const { mountTaskPanel } = await import(
  "data:text/javascript," + encodeURIComponent(src + "\n//# sourceURL=chat-task-test-runtime.js"));
mountTaskPanel({ convId: "conv-1" });
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(calls.length, 1);
assert.equal(calls[0][0], "/conversations/conv-1/task");
assert.equal(calls[0][1], undefined, "render usa GET por defecto");
await poll();
assert.equal(calls.length, 2);
assert.equal(calls[1][1], undefined, "polling no muta el backend");
console.log("ok - chat task render y polling solo leen");

const { stableTaskRequestId, taskActionPayload, requiresUncertainAcknowledgement } = await import(
  "data:text/javascript," + encodeURIComponent(src + "\n//# sourceURL=chat-task-payload-test.js"));
const first = stableTaskRequestId("conv-2", "continue");
assert.equal(stableTaskRequestId("conv-2", "continue"), first,
  "un retry conserva el request_id");
assert.notEqual(stableTaskRequestId("conv-2", "publish"), first,
  "acciones distintas tienen intentos distintos");
assert.deepEqual(taskActionPayload("conv-2", "continue", { acknowledge_uncertain: true }), {
  action: "continue", request_id: first, acknowledge_uncertain: true,
});
assert.notEqual(taskActionPayload("conv-2", "track", { enabled: true }).request_id,
  taskActionPayload("conv-2", "track", { enabled: false }).request_id,
  "activar y apagar tracking son intentos distintos");
assert.equal(requiresUncertainAcknowledgement({ pending_events: ["evt"] }), false,
  "pendientes informativos no fuerzan acknowledge");
assert.equal(requiresUncertainAcknowledgement({ uncertain_events: ["evt"] }), true,
  "eventos inciertos sí fuerzan acknowledge");
console.log("ok - request_id estable y acknowledge explícito");

const chatsSrc = readFileSync(
  new URL("../admin_static/static/tab-chats.js", import.meta.url), "utf8")
  .replace(/^import .*$/gm, "");
const { steerRequestPayload } = await import(
  "data:text/javascript," + encodeURIComponent(chatsSrc));
assert.deepEqual(steerRequestPayload("corrige el rumbo", "req-7"), {
  message: "corrige el rumbo", request_id: "req-7",
});
console.log("ok - steer request payload conserva mensaje e id");

globalThis.escape = (value) => String(value || "").replaceAll("<", "&lt;");
const { panelHtml } = await import("data:text/javascript," + encodeURIComponent(src));
const review = panelHtml({mode:"write", state:"review", publish_allowed:true,
  pr_url:"https://example.invalid/pr/1", validation:{status:"stale", head_sha:"abc", detail:"antes correcta"}});
assert.match(review, /pendiente de revisión/);
assert.match(review, /Validación: obsoleta/);
assert.doesNotMatch(review, /data-task-track disabled/);
assert.match(panelHtml({mode:"write",state:"implemented",publish_allowed:false}), /Publicar PR/);
assert.match(panelHtml({mode:"write",state:"implemented",publish_allowed:false}),
  /disabled\s+data-task-action="publish"/);
const member = panelHtml({mode:"write",state:"review",can_control:false,
  publish_allowed:true,pr_url:"https://example.invalid/pr/1",tracking:{enabled:true}});
assert.match(member, /pendiente de revisión/);
assert.equal((member.match(/disabled\s+data-task-action/g) || []).length, 4);
assert.match(member, /data-task-track disabled/);
assert.doesNotMatch(panelHtml({mode:"read_only",state:"ready"}), /Publicar PR/);
assert.doesNotMatch(panelHtml({mode:"write",state:"finished",pr_url:'javascript:alert(1)'}), /href=/);
const finished = panelHtml({mode:"write",state:"finished"});
assert.equal((finished.match(/disabled\s+data-task-action/g) || []).length, 4);
console.log("ok - estados reales, publicación explícita y enlaces seguros");

let lateResolve;
const latePanel = {
  hidden: false, innerHTML: "before",
  querySelectorAll: () => [], querySelector: () => null,
};
globalThis.document = { getElementById: () => latePanel };
globalThis.registerPoller = () => () => {};
globalThis.apiRoot = async () => new Promise(resolve => { lateResolve = resolve; });
const lateMount = mountTaskPanel({ convId: "late-conv" });
lateMount.destroy();
lateResolve({ mode: "write", state: "ready" });
await new Promise(resolve => setTimeout(resolve, 0));
assert.equal(latePanel.innerHTML, "");
console.log("ok - GET tardío después de destroy no repinta el panel");
