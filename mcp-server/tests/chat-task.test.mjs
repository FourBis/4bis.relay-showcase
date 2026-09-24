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
assert.equal(taskActionLabel("enable_write"), "Habilitar escritura");
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
const devReadOnly = panelHtml({mode:"read_only",state:"ready",
  allowed_actions:["continue","pause","cancel","enable_write"]});
assert.match(devReadOnly, /data-task-action="enable_write"/);
assert.doesNotMatch(devReadOnly, /Publicar PR|data-task-track/);
assert.doesNotMatch(panelHtml({mode:"read_only",state:"running",
  allowed_actions:["enable_write"]}), /data-task-action="enable_write"/);
assert.doesNotMatch(panelHtml({mode:"read_only",state:"finished",
  allowed_actions:["enable_write"]}), /data-task-action="enable_write"/);
const projectDev = panelHtml({mode:"write",state:"ready",publish_allowed:true,
  pr_url:"https://example.invalid/pr/2",allowed_actions:["continue","pause","cancel"]});
assert.doesNotMatch(projectDev, /Publicar PR|data-task-track/);
assert.match(projectDev, /data-task-action="continue"/);
const projectDevRedacted = panelHtml({mode:"write",state:"ready",can_control:false,
  allowed_actions:["continue","pause","cancel"]});
assert.match(projectDevRedacted, /data-task-action="continue"/);
assert.doesNotMatch(projectDevRedacted, /disabled\s+data-task-action="continue"|Publicar PR|data-task-track/);
assert.doesNotMatch(panelHtml({mode:"write",state:"finished",pr_url:'javascript:alert(1)'}), /href=/);
const finished = panelHtml({mode:"write",state:"finished"});
assert.equal((finished.match(/disabled\s+data-task-action/g) || []).length, 4);
console.log("ok - estados reales, publicación explícita y enlaces seguros");

const devPosts = [];
const devPanel = {
  hidden: false,
  _html: "",
  _buttons: [],
  set innerHTML(value) {
    this._html = value;
    this._buttons = [...value.matchAll(/<button([^>]*)data-task-action="([^"]+)"[^>]*>/g)]
      .map((match) => {
        const listeners = {};
        return {
          dataset: { taskAction: match[2] },
          disabled: /\bdisabled\b/.test(match[1]),
          addEventListener(type, fn) { listeners[type] = fn; },
          async click() { await listeners.click?.(); },
        };
      });
  },
  get innerHTML() { return this._html; },
  querySelectorAll(selector) {
    return selector === "[data-task-action]" ? this._buttons : [];
  },
  querySelector() { return null; },
};
globalThis.document = { getElementById: () => devPanel, activeElement: null };
globalThis.registerPoller = () => () => {};
globalThis.confirmModal = async () => true;
globalThis.toast = () => {};
globalThis.apiRoot = async (_path, options) => {
  if (!options) return { id: "dev-task", mode: "read_only", state: "ready",
    can_control: false,
    allowed_actions: ["continue", "enable_write"] };
  devPosts.push(options.body.action);
  return { id: "dev-task", mode: "read_only", state: "ready", can_control: false,
    allowed_actions: ["continue", "enable_write"] };
};
const devPanelModule = await import(
  "data:text/javascript," + encodeURIComponent(src + "\n//# sourceURL=chat-task-dev-actions.js"));
devPanelModule.mountTaskPanel({ convId: "dev-conversation" });
await new Promise((resolve) => setTimeout(resolve, 0));
const devButtons = devPanel.querySelectorAll("[data-task-action]");
assert.equal(devButtons.find(button => button.dataset.taskAction === "continue").disabled, false);
assert.equal(devButtons.find(button => button.dataset.taskAction === "enable_write").disabled, false);
assert.doesNotMatch(devPanel.innerHTML, /Publicar PR|data-task-track/);
await devButtons.find(button => button.dataset.taskAction === "continue").click();
await devButtons.find(button => button.dataset.taskAction === "enable_write").click();
assert.deepEqual(devPosts, ["continue", "enable_write"],
  "acciones del contrato autorizado envían POST aunque can_control sea false");
console.log("ok - acciones Dev redacted respetan allowed_actions y envían POST");

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
