// Uses the real Equipo module and markup with fictional, in-memory API replies.
import assert from 'node:assert/strict';
import { readFile, mkdir } from 'node:fs/promises';
import { createServer } from 'node:http';
import { dirname, join, resolve, sep } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const root = process.env.RELAY_UI_ROOT || resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const admin = join(root, 'mcp-server/admin_static');
const out = process.env.TEAM_VISUAL_OUT || join(tmpdir(), 'relay-team-visual');
const index = await readFile(join(admin, 'index.html'), 'utf8');
const section = index.match(/<section\b[^>]*id="tab-team"[\s\S]*?(?=<!-- Mi cuenta)/)?.[0];
assert(section, 'Equipo section missing');
const users = [
  { email: 'ana@example.test', display_name: 'Ana', role: 'owner', enabled: true, project_slugs: [] },
  { email: 'alex@example.test', display_name: 'Alex', role: 'member', enabled: true, project_slugs: [] },
];
const fixture = `<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/admin/static/admin.css"><style>body{background:#09090b;color:#f4f4f5;padding:16px}main{max-width:740px;margin:auto}#tab-team{display:block!important}</style>
<main><p>Equipo · datos ficticios para validación</p>${section}</main><div id="toast-root"></div>
<script type="module">import {initTeam,loadTeam} from '/admin/static/tab-team.js';
window.showTeam=async(role)=>{initTeam({role,email:'ana@example.test'});await loadTeam();};await showTeam('owner');</script></html>`;
const server = createServer(async (req, res) => {
  try {
    if (req.url === '/') { res.setHeader('Content-Type', 'text/html; charset=utf-8'); res.end(fixture); return; }
    const file = resolve(admin, '.' + new URL(req.url, 'http://localhost').pathname.replace(/^\/admin/, ''));
    if (!file.startsWith(admin + sep)) { res.writeHead(403).end(); return; }
    res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
    res.end(await readFile(file));
  } catch { res.writeHead(404).end(); }
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const base = `http://127.0.0.1:${server.address().port}`;
await mkdir(out, { recursive: true });
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 900, height: 1000 } });
const errors = [], writes = [];
let canAssign = true;
page.on('pageerror', e => errors.push(e.message));
await page.route('**/*', async route => {
  const req = route.request(), url = new URL(req.url());
  if (url.origin !== base) { errors.push('external request'); return route.abort(); }
  if (url.pathname !== '/admin/api/users') return route.continue();
  if (req.method() === 'PUT') {
    const body = req.postDataJSON(); writes.push(body);
    assert(canAssign); Object.assign(users.find(user => user.email === body.email), body);
  }
  return route.fulfill({ json: { users, can_assign_projects: canAssign,
    roles: ['owner', 'subadmin', 'member', 'finance'], role_labels: {owner:'Admin',subadmin:'Subadmin',member:'Dev',finance:'Finanzas'},
    projects: [{ slug: 'aurora-demo', name: 'Aurora Demo' }] } });
});
try {
  await page.goto(base);
  const alex = page.locator('#team-table tbody tr').filter({hasText:'alex@example.test'});
  await alex.getByText('Solo lectura', {exact:true}).waitFor();
  await alex.getByRole('button', {name:'Editar',exact:true}).click();
  await page.locator('input[name="team-project"]').check();
  assert.equal(writes.length, 0, 'Editing must not save before confirmation');
  await page.locator('#team-save').click();
  await alex.getByText('Proyectos asignados', {exact:true}).waitFor();
  assert.deepEqual(writes[0].project_slugs, ['aurora-demo']);
  await alex.getByRole('button', {name:'Editar',exact:true}).click();
  await page.screenshot({path:join(out,'team-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:1100});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Mobile overflow');
  await page.screenshot({path:join(out,'team-mobile.png'),fullPage:true});
  await page.locator('#team-role').selectOption('finance');
  assert(await page.locator('input[name="team-project"]').isDisabled());
  assert.equal(await page.locator('input[name="team-project"]').isChecked(),false);
  await page.locator('#team-cancel').click();
  canAssign = false;
  await page.evaluate(()=>showTeam('subadmin'));
  await alex.getByRole('button', {name:'Editar',exact:true}).click();
  assert.equal(await page.locator('input[name="team-project"]').count(),0,'Subadmin cannot assign');
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({result:'PASS',writes:writes.length,screenshots:out,root}));
} finally {
  await browser.close();
  await new Promise(done=>server.close(done));
}
