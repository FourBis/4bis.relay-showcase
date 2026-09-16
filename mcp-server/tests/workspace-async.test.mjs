import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

function sourceFunction(file, name) {
  const src = readFileSync(new URL(`../admin_static/static/${file}`, import.meta.url), 'utf8');
  return src.match(new RegExp(`(?:export )?((?:async )?function ${name}\\([^]*?\\n})`))[1];
}

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
