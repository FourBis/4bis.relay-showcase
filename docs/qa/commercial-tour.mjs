// Real CRM markup/module with fictional, intercepted API responses.
import assert from 'node:assert/strict';
import { readFile, mkdir } from 'node:fs/promises';
import { createServer } from 'node:http';
import { dirname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const root = process.env.RELAY_UI_ROOT || resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const admin = join(root, 'mcp-server/admin_static');
const out = process.env.CRM_VISUAL_OUT || join(root, 'docs/images');
const index = await readFile(join(admin, 'index.html'), 'utf8');
const section = index.match(/<section\b[^>]*id="tab-crm"[\s\S]*?<\/section>/)?.[0];
assert(section, 'CRM section missing from the real admin index');

const clients = [{
  id: 101,
  ext_id: 'company-aurora-demo',
  name: 'Aurora Demo',
  domain: 'aurora.example.test',
  project_count: 2,
  last_sync_status: 'ok',
  last_activity_at: new Date(Date.now() - 3 * 86400000).toISOString(),
  deals: [
    { id: 'deal-platform', name: 'Plataforma de operaciones', stage: 'CONTRACT_SENT', amount: 18500000 },
    { id: 'deal-analytics', name: 'Analítica comercial', stage: 'CONTRACT_SENT', amount: 9200000 },
  ],
}];
const detail = {
  client: {
    ...clients[0],
    contacts: [{ id: 'contact-1', name: 'Alex Demo', email: 'alex@aurora.example.test', title: 'Contacto ficticio' }],
  },
  projects: [
    {
      slug: 'aurora-platform-demo', name: 'Aurora · Plataforma', repo_path: 'demo/aurora-platform',
      has_git: true, git_remote_url: 'https://github.com/example/aurora-platform-demo',
      deal_id: 'deal-platform', deal_name: 'Plataforma de operaciones', enabled: true,
      github_project: { owner: 'example', number: 12, url: 'https://github.com/orgs/example/projects/12' },
    },
    {
      slug: 'aurora-insights-demo', name: 'Aurora · Insights', repo_path: 'demo/aurora-insights',
      has_git: true, git_remote_url: 'https://github.com/example/aurora-insights-demo',
      deal_id: 'deal-analytics', deal_name: 'Analítica comercial', enabled: true,
      github_project: { owner: 'example', number: 13, url: 'https://github.com/orgs/example/projects/13' },
    },
  ],
};

const fixture = `<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/admin/static/admin.css">
<style>
  body{background:#09090b;color:#f4f4f5;padding:24px}
  main{margin:auto;max-width:1760px}
  #tab-crm{display:block!important}
  #crm-detail.open{animation:none!important}
  .tour-note{color:#a1a1aa;font-size:12px;margin:0 0 14px}
</style>
<main><p class="tour-note">Recorrido visual · datos ficticios · API interceptada</p>${section}</main><div id="toast-root"></div>
<script type="module">
  import {initCrm,loadCrm} from '/admin/static/tab-crm.js';
  initCrm({role:'owner',email:'demo@example.test'});
  await loadCrm();
  window.crmReady=true;
</script></html>`;

const server = createServer(async (req, res) => {
  try {
    if (req.url === '/') {
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end(fixture);
      return;
    }
    const pathname = new URL(req.url, 'http://127.0.0.1').pathname;
    const file = resolve(admin, '.' + pathname.replace(/^\/admin/, ''));
    if (!file.startsWith(admin + sep)) { res.writeHead(403).end(); return; }
    res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css; charset=utf-8' : 'text/javascript; charset=utf-8');
    res.end(await readFile(file));
  } catch {
    res.writeHead(404).end();
  }
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const base = `http://127.0.0.1:${server.address().port}`;
await mkdir(out, { recursive: true });
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1720, height: 420 }, deviceScaleFactor: 1 });
const errors = [], unexpected = [], apiCalls = [];
page.on('pageerror', e => errors.push(e.message));
const assets = new Set([
  '/admin/static/admin.css',
  '/admin/static/tab-crm.js',
  '/admin/static/api.js',
  '/admin/static/ui.js',
  '/admin/static/pollers.js',
]);

const replies = new Map([
  ['GET /admin/api/crm/check', { app_url: 'http://crm.example.test', workspace: 'crm', app_up: true, companies: 1, contacts: 1, deals: 2 }],
  ['GET /admin/api/crm/clients', { clients }],
  ['GET /admin/api/crm/clients/101', detail],
  ['GET /admin/api/projects', { projects: [{ slug: 'aurora-followup-demo', name: 'Aurora · Seguimiento', client_id: null }] }],
  ['GET /admin/api/projects/aurora-platform-demo/github', { configured: true, issues: [{ number: 7 }], pulls: [{ number: 8 }] }],
  ['GET /admin/api/projects/aurora-insights-demo/github', { configured: true, issues: [], pulls: [{ number: 9 }] }],
]);
await page.route('**/*', async route => {
  const request = route.request();
  const url = new URL(request.url());
  if (url.origin !== base) {
    unexpected.push(`${request.method()} ${url.href}`);
    await route.abort();
    return;
  }
    if (url.pathname.startsWith('/admin/api/')) {
    const key = `${request.method()} ${url.pathname}`;
    apiCalls.push(key);
    const reply = replies.get(key);
    if (!reply) {
      unexpected.push(key);
      await route.abort();
      return;
    }
    await route.fulfill({ json: reply });
    return;
    }
  if (url.pathname !== '/' && !assets.has(url.pathname)) {
    unexpected.push(`${request.method()} ${url.pathname}`);
    await route.abort();
    return;
  }
  await route.continue();
});

try {
  await page.goto(base);
  await page.waitForLoadState('networkidle');
  await page.locator('#crm-grid .crm-row').filter({ hasText: 'Aurora Demo' }).click();
  await page.locator('#crm-detail .side-panel-header').getByText('Aurora Demo', { exact: true }).waitFor();
  await page.locator('#crm-detail').getByRole('link', { name: 'Plataforma de operaciones' }).waitFor();
  await page.locator('#crm-detail').getByText('Aurora · Plataforma', { exact: true }).waitFor();
  await page.locator('#crm-detail').getByText('Aurora · Insights', { exact: true }).waitFor();
  await page.locator('#crm-detail').getByText('1 issues', { exact: true }).waitFor();
  await page.locator('#crm-detail').getByText('1 PR', { exact: true }).first().waitFor();

  assert.deepEqual(errors, [], 'page errors');
  assert.deepEqual(unexpected, [], 'unexpected or external requests');
  assert(apiCalls.includes('GET /admin/api/crm/clients/101'));
  assert(apiCalls.includes('GET /admin/api/projects/aurora-platform-demo/github'));
  await page.locator('#crm-detail').screenshot({ path: join(out, 'crm-projects.png') });
  console.log(JSON.stringify({ result: 'PASS', apiCalls, unexpected, screenshot: join(out, 'crm-projects.png') }));
} finally {
  await browser.close();
  await new Promise(done => server.close(done));
}
