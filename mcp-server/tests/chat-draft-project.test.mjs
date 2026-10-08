import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const source = readFileSync(new URL('../admin_static/static/tab-chats.js', import.meta.url), 'utf8');
const openDraft = source.match(/async function openNewChatDraft\([^]*?\n}/)[0];
const sendMessage = source.match(/async function sendCurrentMessage\([^]*?\n}/)[0];
const createDraft = source.match(/async function createDraftConversation\([^]*?\n}/)[0];

function harness(responses, filter = '', apiRoot = async () => ({})) {
  let options = [], selected = '', calls = 0, opened = 0;
  const ui = new Map();
  const node = (id) => {
    if (!ui.has(id)) ui.set(id, { hidden: false, textContent: '', className: '', innerHTML: '',
      options: [], focus() {}, querySelectorAll: () => [], addEventListener() {} });
    return ui.get(id);
  };
  const select = node('#chat-draft-project');
  Object.defineProperty(select, 'options', { get: () => options });
  Object.defineProperty(select, 'innerHTML', { set: (html) => {
    options = [...html.matchAll(/<option value="([^"]*)">/g)].map((m) => ({ value: m[1] }));
    selected = '';
  } });
  Object.defineProperty(select, 'value', { get: () => selected, set: (value) => {
    selected = options.some((o) => o.value === value) ? value : '';
  } });
  const context = {
    $: (id) => id === '#chat-project-filter' ? { value: filter } : node(id),
    api: async () => { const value = responses[calls++]; if (value instanceof Error) throw value; return value; },
    escape: (s) => s, getEmbeddedConversation: () => '', toast: (message) => node('#toast').textContent = message,
    apiRoot,
    setTaskWorkspaceContext() {}, showMainView: () => { opened++; }, renderConvList() {}, detachGrafo() {},
    renderDraftMode() {}, setBusy() {}, matchCommand: () => null, renderSuggestions() {},
    renderAttachTray() {}, appendOptimisticUser() {}, pokeGrafo: async () => {}, pollChat: async () => {},
    renderHeader() {}, loadChats() {}, newRequestId: () => 'fake-request-id',
  };
  runInNewContext(`let chatSelectionGeneration = 0, chatDraftGeneration = 0, activeChat, taskPanel;
    let pendingAttachments = [], chosenModel = '', chosenStageModels = {};
    ${openDraft}; ${createDraft}; ${sendMessage};
    this.open = openNewChatDraft; this.send = sendCurrentMessage;
    this.state = () => activeChat; this.generation = () => chatSelectionGeneration;
    this.setBusyState = (busy) => { if (activeChat) activeChat.busy = busy; }`, context);
  context.setBusy = (busy) => context.setBusyState(busy);
  return { context, select, get calls() { return calls; }, get opened() { return opened; }, node };
}

function deferred() {
  let resolve;
  const promise = new Promise((yes) => { resolve = yes; });
  return { promise, resolve };
}

const catalog = (slug, enabled = true) => ({ projects: [{ slug, enabled }] });

test('refresca catálogo en cada apertura y selecciona el proyecto actual', async () => {
  const h = harness([catalog('alfa'), catalog('beta')]);
  await h.context.open(); await h.context.open();
  assert.equal(h.calls, 2); assert.equal(h.select.value, 'beta');
});

test('filtro parcial o inexistente conserva el primer proyecto habilitado', async () => {
  for (const filter of ['alf', 'inexistente']) {
    const h = harness([catalog('alfa')], filter);
    await h.context.open();
    assert.equal(h.select.value, 'alfa');
    assert.equal(h.node('#toast').textContent, '');
  }
});

test('conserva filtro que coincide con proyecto habilitado', async () => {
  const h = harness([{ projects: [{ slug: 'alfa', enabled: true }, { slug: 'beta', enabled: true }] }], ' beta ');
  await h.context.open(); assert.equal(h.select.value, 'beta');
});

test('error de catálogo muestra aviso sin abrir borrador ni rechazar', async () => {
  const h = harness([new Error('sin conexión')]);
  await assert.doesNotReject(h.context.open());
  assert.match(h.node('#toast').textContent, /sin conexión/);
  assert.equal(h.opened, 0); assert.equal(h.context.state(), undefined);
});

test('fallar al consultar catálogo conserva la generación de selección actual', async () => {
  const h = harness([new Error('sin conexión')]);
  const generation = h.context.generation();
  await h.context.open();
  assert.equal(h.context.generation(), generation);
});

test('el envío invalida la apertura beta pendiente y termina en la conversación alfa', async () => {
  const creation = deferred(), catalogLoad = deferred(), runBodies = [];
  const h = harness([catalog('alfa'), catalogLoad.promise], '', async (path, options) => {
    if (path === '/conversations' && options.method === 'POST') {
      return creation.promise;
    }
    if (path === '/experts/run') { runBodies.push(JSON.parse(options.body)); return { id: 'fake-run' }; }
    return {};
  });
  await h.context.open();
  const opening = h.context.open();
  h.node('#chat-panel-input').value = 'mensaje del borrador alfa';
  const sending = h.context.send();
  await Promise.resolve();
  catalogLoad.resolve(catalog('beta'));
  await opening;
  assert.equal(h.context.state().projectSlug, 'alfa');
  assert.equal(h.context.state().draft, true);
  creation.resolve({ id: 'fake-conversation' });
  await sending;
  assert.equal(h.context.state().projectSlug, 'alfa');
  assert.equal(h.context.state().convId, 'fake-conversation');
  assert.equal(h.context.state().draft, false);
  assert.equal(h.context.state().pendingUserText, 'mensaje del borrador alfa');
  assert.equal(runBodies[0].target, 'alfa');
  assert.equal(runBodies[0].conversation, 'fake-conversation');
  assert.equal(runBodies[0].user, 'mensaje del borrador alfa');
});

test('Nuevo durante el POST de creación espera y conserva el borrador enviado', async () => {
  const creation = deferred();
  const h = harness([catalog('alfa')], '', async (path, options) => {
    if (path === '/conversations' && options.method === 'POST') return creation.promise;
    if (path === '/experts/run') return { id: 'fake-run' };
    return {};
  });
  await h.context.open();
  h.node('#chat-panel-input').value = 'mensaje alfa';
  const sending = h.context.send();
  await Promise.resolve();
  await h.context.open();
  assert.equal(h.calls, 1);
  assert.equal(h.context.state().projectSlug, 'alfa');
  creation.resolve({ id: 'fake-conversation' });
  await sending;
  assert.equal(h.context.state().projectSlug, 'alfa');
  assert.equal(h.context.state().convId, 'fake-conversation');
  assert.equal(h.context.state().pendingUserText, 'mensaje alfa');
});

for (const fails of [false, true]) test(`una respuesta anterior${fails ? ' fallida' : ''} no pisa el borrador nuevo`, async () => {
  let finish, fail;
  const pending = new Promise((resolve, reject) => { finish = resolve; fail = reject; });
  const h = harness([pending, catalog('beta')]);
  const old = h.context.open();
  await h.context.open();
  if (fails) fail(new Error('fallo anterior')); else finish(catalog('alfa'));
  await assert.doesNotReject(old);
  assert.equal(h.select.value, 'beta');
  assert.equal(h.context.state().projectSlug, 'beta');
  assert.equal(h.node('#toast').textContent, '');
  assert.equal(h.opened, 1);
});

test('catálogo sin proyectos habilitados avisa y no abre borrador', async () => {
  const h = harness([{ projects: [{ slug: 'alfa', enabled: false }] }]);
  await h.context.open();
  assert.equal(h.node('#toast').textContent, 'No hay proyectos habilitados');
  assert.equal(h.opened, 0); assert.equal(h.context.state(), undefined);
});
