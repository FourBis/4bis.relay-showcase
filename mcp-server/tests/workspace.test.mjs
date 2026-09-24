// node --test mcp-server/tests/workspace.test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
const source = readFileSync(new URL('../admin_static/static/workspace.js', import.meta.url), 'utf8')
  .replace(/^import .*$/gm, '');
const { fitRect, tileRects, readLayout, recentlyClosed } = await import('data:text/javascript,' + encodeURIComponent(source));

test('mover y redimensionar nunca deja los controles fuera del workspace', () => {
  for (const bounds of [{ width: 1440, height: 800 }, { width: 390, height: 500 }]) {
    for (const rect of [{ x: -999, y: 9999, w: Infinity, h: -20 }, { x: 30, y: 40, w: 4000, h: 900 }]) {
      const r = fitRect(rect, bounds);
      assert(r.x >= 0 && r.y >= 0);
      assert(r.x + r.w <= bounds.width && r.y + r.h <= bounds.height);
      assert(Object.values(r).every(Number.isFinite));
    }
  }
});

test('dos ventanas lado a lado aprovechan el espacio sin solaparse', () => {
  const [a, b] = tileRects(2, { width: 1440, height: 800 });
  assert(a.x + a.w < b.x);
  assert.equal(a.h, 800);
  assert.equal(b.x + b.w, 1440);
  assert.deepEqual(tileRects(0, { width: 1440, height: 800 }), []);
});

test('layout corrupto, módulos desconocidos y duplicados no se restauran', () => {
  assert.deepEqual(readLayout('{broken', ['chat']), []);
  const raw = JSON.stringify({ version: 1, windows: [
    { id: 'chat', module: 'chat', rect: { x: 30, y: 40, w: 800, h: 600 } },
    { id: 'chat', module: 'chat' },
    { id: 'config', module: 'not-a-module' },
    { id: 'unknown' },
  ] });
  const saved = readLayout(raw, ['chat']);
  assert.equal(saved.length, 1);
  assert.deepEqual(saved[0].rect, { x: 30, y: 40, w: 800, h: 600 });
});

test('conserva referencias a resultados y estado de ventanas, nunca su contenido', () => {
  const saved = readLayout(JSON.stringify({ version: 1, windows: [{ id: 'object:1', title: 'Tabla',
    restore: { conversationId: 'conv-1', messageIndex: 2, kind: 'table', itemIndex: 0 },
    content: 'No guardar esta respuesta', closed: true, maximized: true,
  }] }), ['chat']);
  assert.equal(saved.length, 1);
  assert.equal(saved[0].closed, true);
  assert.equal(saved[0].maximized, true);
  assert(!JSON.stringify(saved).includes('No guardar esta respuesta'));
});

test('los chats separados se restauran solo con identificadores válidos y consistentes', () => {
  const saved = readLayout(JSON.stringify({ version: 1, windows: [
    { id: 'object:conversation:conv-1', restore: { kind: 'conversation', conversationId: 'conv-1' } },
    { id: 'object:conversation:conv-2', restore: { kind: 'conversation', conversationId: 'conv-1' } },
    { id: 'object:conversation:../bad', restore: { kind: 'conversation', conversationId: '../bad' } },
    { id: 'object:conversation:', restore: { kind: 'conversation', conversationId: '' } },
  ] }), ['chat']);
  assert.deepEqual(saved.map(w => w.id), ['object:conversation:conv-1']);
});

test('restaurar oculta IDs antiguos y conserva nombres editados sin cambiar la conversación', () => {
  const entry = { id: 'object:conversation:12345678-abcd', title: 'proyecto · 12345678',
    restore: { kind: 'conversation', conversationId: '12345678-abcd' } };
  const restore = customTitle => readLayout(JSON.stringify({ version: 1,
    windows: [{ ...entry, customTitle }] }), ['chat'])[0];
  assert.equal(restore().title, 'proyecto');
  const renamed = restore('  Entrega · 12345678  ');
  assert.equal(renamed.title, 'Entrega · 12345678');
  assert.equal(renamed.customTitle, 'Entrega · 12345678');
  assert.equal(renamed.id, entry.id);
  assert.equal(renamed.restore.conversationId, entry.restore.conversationId);
});

test('Cerrados recientemente usa el último cierre, incluso si una ventana se reabrió', () => {
  const sourceOrder = [
    { id: 'abierta-primero', closed: true, closedAt: 1 },
    { id: 'reabierta', closed: true, closedAt: 3 },
    { id: 'cerrada-despues', closed: true, closedAt: 2 },
  ];
  const recent = recentlyClosed(sourceOrder);
  assert.deepEqual(recent.map(w => w.id), ['reabierta', 'cerrada-despues', 'abierta-primero']);
});

test('readLayout conserva la secuencia de cierre para persistencia', () => {
  const saved = readLayout(JSON.stringify({ version: 1, windows: [{ id: 'object:1',
    restore: { conversationId: 'conv-1', messageIndex: 2, kind: 'table', itemIndex: 0 },
    closed: true, closedAt: 9,
  }] }), ['chat']);
  assert.equal(saved[0].closedAt, 9);
});
