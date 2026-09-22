import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

function sourceFunction(file, name) {
  const src = readFileSync(new URL(`../admin_static/static/${file}`, import.meta.url), 'utf8');
  return src.match(new RegExp(`(?:export )?((?:async )?function ${name}\\([^]*?\\n})`))[1];
}

test('navegar abre ventana en el padre y selecciona dentro del iframe', () => {
  const opened = [], selected = [], drawer = [];
  const context = {
    embedded: false,
    convCache: [{ id: 'a', project_slug: 'demo' }],
    setChatDrawer: open => drawer.push(open),
    openConversationWindow: options => opened.push(options),
    selectConversation: id => selected.push(id),
  };
  context.getEmbeddedConversation = () => context.embedded ? 'actual' : '';
  runInNewContext(`${sourceFunction('tab-chats.js', 'navigateConversation')};
    this.navigate = navigateConversation`, context);

  context.navigate('a');
  context.embedded = true;
  context.navigate('b');

  assert.equal(opened.length, 1);
  assert.equal(opened[0].id, 'a');
  assert.equal(opened[0].project_slug, 'demo');
  assert.deepEqual(drawer, [false]);
  assert.deepEqual(selected, ['b']);
});

test('seleccionar B mientras A carga mantiene B aunque A llegue último', async () => {
  const pending = {}, opened = [], errors = [];
  const context = {
    apiRoot: path => new Promise(resolve => { pending[path] = resolve; }),
    setChatDrawer() {}, openConversation: async meta => opened.push(meta.id),
    toast: error => errors.push(error),
  };
  runInNewContext(`let chatSelectionGeneration = 0; ${sourceFunction('tab-chats.js', 'selectConversation')}; this.select = selectConversation`, context);
  const a = context.select('a');
  const b = context.select('b');
  pending['/conversations/b']({ id: 'b' }); await b;
  pending['/conversations/a']({ id: 'a' }); await a;
  assert.deepEqual(opened, ['b']); assert.deepEqual(errors, []);
});

test('terminar sincronización CRM desregistra el poller una sola vez', () => {
  let stopped = 0;
  const context = { unregister: () => stopped++ };
  runInNewContext(`let _pollHandle = unregister; ${sourceFunction('tab-crm.js', '_stopPolling')}; _stopPolling(); _stopPolling();`, context);
  assert.equal(stopped, 1);
});

test('cambiar de conversación reemplaza el grafo antes de esperar el proyecto', async () => {
  let graph = 'anterior', headerGraph, release;
  const context = {
    $: () => ({}), showMainView() {}, renderConvList() {},
    setTaskWorkspaceContext() {}, mountTaskPanel: () => ({ destroy() {} }), renderTaskMode() {},
    attachGrafo: id => { graph = id; },
    renderHeader: () => { headerGraph = graph; },
    api: () => new Promise(resolve => { release = resolve; }),
    selectionIsCurrent: () => false,
  };
  runInNewContext(`let chatSelectionGeneration = 1, activeChat, taskPanel;
    ${sourceFunction('tab-chats.js', 'openConversation')}; this.open = openConversation`, context);
  const pending = context.open({ id: 'nueva', project_slug: 'demo', status: 'open' }, 1);
  assert.equal(headerGraph, 'nueva', 'el encabezado nuevo nunca debe conservar el grafo anterior');
  release({});
  await pending;
});

test('consulta abierta conserva entrada aunque task_json sea read_only', async () => {
  const context = {
    $: () => ({ hidden: false, textContent: '' }),
    renderTaskMode() {},
    resumeRunningChat: async () => false,
    loadMessages: async () => {},
    activeChat: { convId: 'c-open', busy: false, readOnly: false },
  };
  runInNewContext(`let activeChat = this.activeChat;
    const selectionIsCurrent = () => true;
    ${sourceFunction('tab-chats.js', 'taskSnapshot')};
    ${sourceFunction('tab-chats.js', 'handleTaskUpdate')};
    this.update = handleTaskUpdate`, context);
  await context.update({ id: 'c-open', status: 'open' },
    { mode: 'read_only', state: 'ready', publish_allowed: false }, null, 1);
  assert.equal(context.activeChat.readOnly, false);
  assert.equal(context.activeChat.mode, 'consultation');
});

test('cambio durante busy se recupera al quedar idle con el mismo snapshot', async () => {
  let reconnects = 0;
  const context = {
    $: () => ({ hidden: false, textContent: '' }),
    renderTaskMode() {},
    resumeRunningChat: async () => { reconnects++; return false; },
    loadMessages: async () => {},
    activeChat: { convId: 'c-busy', busy: false, readOnly: false },
  };
  runInNewContext(`let activeChat = this.activeChat;
    const selectionIsCurrent = () => true;
    ${sourceFunction('tab-chats.js', 'taskSnapshot')};
    ${sourceFunction('tab-chats.js', 'handleTaskUpdate')};
    this.update = handleTaskUpdate`, context);
  const oldTask = { mode: 'write', state: 'ready', head_sha: 'a' };
  const nextTask = { mode: 'write', state: 'running', head_sha: 'b',
    last_event: { id: 'evt-1', state: 'processing', chat_id: 'chat-1' } };
  await context.update({ id: 'c-busy', status: 'open' }, oldTask, null, 1);
  context.activeChat.busy = true;
  await context.update({ id: 'c-busy', status: 'open' }, nextTask, oldTask, 1);
  assert.equal(reconnects, 0);
  context.activeChat.busy = false;
  await context.update({ id: 'c-busy', status: 'open' }, nextTask, nextTask, 1);
  assert.equal(reconnects, 1);
});

test('cambio de selección durante await no escribe snapshot en el chat nuevo', async () => {
  let release;
  const context = {
    $: () => ({ hidden: false, textContent: '' }),
    renderTaskMode() {},
    resumeRunningChat: () => new Promise(resolve => { release = resolve; }),
    loadMessages: async () => {},
    activeChat: { convId: 'old', busy: false, readOnly: false },
  };
  runInNewContext(`let activeChat = this.activeChat;
    const selectionIsCurrent = (_g, id) => activeChat.convId === id;
    ${sourceFunction('tab-chats.js', 'taskSnapshot')};
    ${sourceFunction('tab-chats.js', 'handleTaskUpdate')};
    this.update = handleTaskUpdate;
    this.switchChat = next => { activeChat = next; }`, context);
  await context.update({ id: 'old', status: 'open' },
    { mode: 'write', state: 'ready', head_sha: 'a' }, null, 1);
  const pending = context.update({ id: 'old', status: 'open' },
    { mode: 'write', state: 'running', head_sha: 'b' },
    { mode: 'write', state: 'ready', head_sha: 'a' }, 1);
  const fresh = { convId: 'new', busy: false, readOnly: false };
  context.switchChat(fresh);
  release(false);
  await pending;
  assert.equal(fresh.lastTaskSnapshot, undefined);
});
