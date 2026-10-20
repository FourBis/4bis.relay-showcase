import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";

const source = readFileSync(
  new URL("../admin_static/static/tab-chats.js", import.meta.url), "utf8");
const pollStart = source.indexOf("async function pollChat");
const pollEnd = source.indexOf("\n\nfunction appendOptimisticUser", pollStart);
const pollSource = source.slice(pollStart, pollEnd);
const helpersStart = source.indexOf("function selectionIsCurrent");
const helpersEnd = source.indexOf("// =====================================================================\n// SIDEBAR", helpersStart);
const selectionSource = source.slice(helpersStart, helpersEnd);
const terminalStart = source.indexOf('const TERMINAL_CHAT_STATUSES');
const terminalEnd = source.indexOf('function showPollReconnect', terminalStart);
const terminalSource = source.slice(terminalStart, terminalEnd);

function error(message, status) {
  return Object.assign(new Error(message), status === undefined ? {} : { status });
}

function harness(respond) {
  const calls = [];
  const waits = [];
  const busyButton = { hidden: false, textContent: "⏳ pensando…" };
  const busyChanges = [];
  const loaded = [];
  const toasts = [];
  const chimes = [];
  const context = {
    apiRoot: async (path) => {
      calls.push(path);
      return respond(path, calls.length);
    },
    $: (selector) => selector === "#chat-panel-busy" ? busyButton : null,
    isChatViewVisible: () => true,
    _dbg: () => {},
    updateBusyStatus: () => {},
    showPollReconnect: () => { busyButton.textContent = "↻ reconectando…"; },
    setBusy: (busy) => busyChanges.push(busy),
    loadMessages: async (convId) => loaded.push(convId),
    refreshContextBadge: () => {},
    loadChats: () => {},
    loadSuggestions: () => {},
    refreshChatQuestions: async () => 0,
    pokeGrafo: async () => {},
    toast: (...args) => toasts.push(args),
    showContinueBanner: () => {},
    chime: (...args) => chimes.push(args),
    setTimeout: (resolve, ms) => { waits.push(ms); resolve(); return 1; },
  };
  runInNewContext(`${selectionSource}\n    ${terminalSource}
    ${pollSource}
    this.pollChat = pollChat;`, context);
  return { context, calls, waits, busyButton, busyChanges, loaded, toasts, chimes };
}

// El snapshot vivo real no trae `status`; `finished=true` es su confirmación.
const terminal = { finished: true };

// Un cambio de conversación durante el primer request invalida la respuesta
// tardía antes de que toque el estado o el DOM de la conversación nueva.
{
  let release;
  const first = new Promise((resolve) => { release = resolve; });
  const h = harness(() => first);
  h.context.activeChat = { convId: "conv-old", currentChatId: "run-old" };
  h.context.chatSelectionGeneration = 1;
  const polling = h.context.pollChat("run-old", "conv-old", 1);
  await Promise.resolve();
  h.context.activeChat = { convId: "conv-new", currentChatId: "run-new" };
  h.context.chatSelectionGeneration = 2;
  release(terminal);
  await polling;
  assert.deepEqual(h.busyChanges, [], "respuesta vieja cambió el busy nuevo");
  assert.deepEqual(h.loaded, [], "respuesta vieja cargó mensajes nuevos");
}

// El snapshot vivo real no tiene status; si un backend agrega `queued`,
// finished=true tampoco debe cerrar antes de recibir un estado terminal.
{
  const responses = [{ status: "queued", finished: true }, terminal];
  const h = harness(() => responses.shift());
  h.context.activeChat = { convId: "conv-queued", currentChatId: "run-queued" };
  h.context.chatSelectionGeneration = 1;
  await h.context.pollChat("run-queued", "conv-queued", 1);
  assert.deepEqual(h.busyChanges, [false], "queued con finished cerró el chat");
  assert.deepEqual(h.loaded, ["conv-queued"]);
  assert.deepEqual(h.waits, [1500]);
}

// También se invalida una respuesta tardía del fallback persistido.
{
  let release;
  const persisted = new Promise((resolve) => { release = resolve; });
  const h = harness((path) => {
    if (path.startsWith("/experts/status/")) throw error("run ausente", 404);
    return persisted;
  });
  h.context.activeChat = { convId: "conv-old-fallback", currentChatId: "run-old-fallback" };
  h.context.chatSelectionGeneration = 1;
  const polling = h.context.pollChat("run-old-fallback", "conv-old-fallback", 1);
  await Promise.resolve();
  h.context.activeChat = { convId: "conv-new-fallback", currentChatId: "run-new-fallback" };
  h.context.chatSelectionGeneration = 2;
  release({ chat: { status: "error" }, progress: null });
  await polling;
  assert.deepEqual(h.busyChanges, [], "fallback viejo cambió el busy nuevo");
  assert.deepEqual(h.loaded, [], "fallback viejo cargó mensajes nuevos");
}

// Timeout y 503 sólo muestran reconexión; el cierre ocurre después del estado
// terminal confirmado y carga el historial una sola vez.
{
  const responses = [error("timeout"), error("503", 503), terminal];
  const h = harness(() => {
    const response = responses.shift();
    if (response instanceof Error) throw response;
    return response;
  });
  h.context.activeChat = { convId: "conv-1", currentChatId: "run-1" };
  h.context.chatSelectionGeneration = 1;
  await h.context.pollChat("run-1", "conv-1", 1);
  assert.deepEqual(h.busyChanges, [false], "un error transitorio finalizó el chat");
  assert.deepEqual(h.loaded, ["conv-1"]);
  assert.equal(h.toasts.length, 0, "los retries mostraron toast por tick");
  assert.match(h.busyButton.textContent, /reconectando/);
  assert.deepEqual(h.waits, [1500, 3000], "el retry no respetó el backoff acotado");
}

// Si se pierde el snapshot en memoria, la fila persistida confirma el cierre.
{
  const h = harness((path) => {
    if (path.startsWith("/experts/status/")) throw error("run ausente", 404);
    return { chat: { status: "error", error: "fallo persistido" }, progress: null,
      has_progress: false, is_running: false };
  });
  h.context.activeChat = { convId: "conv-2", currentChatId: "run-2" };
  h.context.chatSelectionGeneration = 1;
  await h.context.pollChat("run-2", "conv-2", 1);
  assert.deepEqual(h.calls, ["/experts/status/run-2", "/chats/run-2/status"]);
  assert.deepEqual(h.busyChanges, [false]);
  assert.deepEqual(h.loaded, ["conv-2"]);
  assert.equal(h.chimes[0][0], "error");
}

// Reemplazar el run en el mismo hilo también invalida la petición anterior.
{
  let release;
  const h = harness(() => new Promise((resolve) => { release = resolve; }));
  h.context.activeChat = { convId: "same", currentChatId: "old" };
  h.context.chatSelectionGeneration = 1;
  const polling = h.context.pollChat("old", "same", 1);
  await Promise.resolve();
  h.context.activeChat.currentChatId = "new";
  release(terminal);
  await polling;
  assert.deepEqual(h.busyChanges, []);
  assert.deepEqual(h.loaded, []);
}

// La espera crece hasta el tope y un estado cancelado confirmado termina.
{
  let remaining = 7;
  const h = harness(() => {
    if (remaining-- > 0) throw error("sin conexión");
    return { finished: true, status: "cancelled" };
  });
  h.context.activeChat = { convId: "retry", currentChatId: "retry-run" };
  h.context.chatSelectionGeneration = 1;
  await h.context.pollChat("retry-run", "retry", 1);
  assert.deepEqual(h.waits, [1500, 3000, 6000, 10000, 10000, 10000, 10000]);
  assert.deepEqual(h.busyChanges, [false]);
  assert.deepEqual(h.loaded, ["retry"]);
}

console.log("chat-poll: estados transitorios y persistidos pasan");
