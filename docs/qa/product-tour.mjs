// Reproducible screenshots from real Admin UI markup/modules and fictitious API data.
// All browser traffic stays on this temporary loopback server; API writes are blocked.
import assert from 'node:assert/strict';
import { readFile, mkdir } from 'node:fs/promises';
import { createServer } from 'node:http';
import { dirname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { browse, config, indexedFiles, models, projects, timeouts } from './product-tour-fixtures.mjs';

const root = process.env.RELAY_UI_ROOT || resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const admin = join(root, 'mcp-server/admin_static');
const images = join(root, 'docs/images');
const source = await readFile(join(admin, 'index.html'), 'utf8');

function sectionById(id) {
  const start = source.indexOf(`<section id="${id}"`);
  assert(start >= 0, `Missing real UI section #${id}`);
  const tags = /<\/?section\b[^>]*>/gi;
  tags.lastIndex = start;
  let depth = 0, match;
  while ((match = tags.exec(source))) {
    if (match[0].startsWith('</')) depth--;
    else depth++;
    if (depth === 0) return source.slice(start, tags.lastIndex);
  }
  assert.fail(`Unclosed real UI section #${id}`);
}

function divById(id) {
  const start = source.indexOf(`<div id="${id}"`);
  assert(start >= 0, `Missing real UI modal #${id}`);
  const tags = /<\/?div\b[^>]*>/gi;
  tags.lastIndex = start;
  let depth = 0, match;
  while ((match = tags.exec(source))) {
    if (match[0].startsWith('</')) depth--;
    else depth++;
    if (depth === 0) return source.slice(start, tags.lastIndex);
  }
  assert.fail(`Unclosed real UI modal #${id}`);
}

const sections = {
  models: sectionById('tab-models'),
  config: sectionById('tab-config'),
  index: sectionById('tab-index'),
  projects: sectionById('tab-projects'),
};
const editor = divById('models-editor');

const mounts = {
  models: `import {initModels,loadModels} from '/admin/static/tab-models.js'; initModels(); await loadModels();`,
  config: `import {initConfig,loadConfig,loadExpertTimeout,loadToolTimeout} from '/admin/static/tab-config.js'; initConfig(); await loadConfig(); await loadExpertTimeout(); await loadToolTimeout();`,
  index: `import {initIndex,initFromConfig,loadIndexPanel} from '/admin/static/tab-index.js'; initIndex(); await initFromConfig(); await loadIndexPanel(); document.querySelector('#browse-btn').click();`,
  projects: `import {loadProjects} from '/admin/static/tab-projects.js'; await loadProjects();`,
};

function fixtureHtml(view) {
  const content = view === 'models' ? sections.models + editor : sections[view];
  const css = {
    models: '#tab-models{display:block!important}',
    config: '#tab-config{display:block!important}#tab-config>.form-grid,#tab-config>.form-section:not(.mt-4),#tab-config>.form-section.mt-4~*,#cfg-save,#cfg-msg,#cfg-restart-note{display:none!important}#tab-config>.form-section.mt-4{margin-top:0!important}',
    index: '#tab-index{display:block!important}#bulk-progress{display:none!important}#tab-index>.table-wrap,#tab-index>.toolbar{margin-bottom:12px}',
    projects: '#tab-projects{display:block!important}',
  }[view];
  return `<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Recorrido Relay · datos ficticios</title><link rel="stylesheet" href="/admin/static/admin.css"><style>
    body{background:#09090b;color:#f4f4f5;padding:24px}main{max-width:1480px;margin:auto}#tour-label{font:600 13px system-ui;color:#a1a1aa;margin:0 0 14px}.panel{padding:0!important}#tab-config>.form-section.mt-4{display:block!important}#models-editor:not(.open){display:none!important}#models-editor.open{display:none!important}#browse-path{min-width:280px;flex:1}#tab-index>.toolbar:first-of-type{flex-wrap:wrap}
    ${css}
  </style></head><body><main><p id="tour-label">FourBis Relay · vista del producto · datos ficticios</p>${content}<div id="toast-root"></div></main>
    <script type="module">${mounts[view]} window.productTourReady=true;</script></body></html>`;
}

const unexpected = [], writes = [], pageErrors = [];
const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://127.0.0.1');
    if (url.pathname === '/') {
      const view = url.searchParams.get('view');
      if (!Object.hasOwn(sections, view)) { res.writeHead(400).end('unknown view'); return; }
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end(fixtureHtml(view));
      return;
    }
    if (req.method !== 'GET') { res.writeHead(405).end(); return; }
    const pathname = decodeURIComponent(url.pathname);
    const staticPrefix = '/admin/static/';
    if (!pathname.startsWith(staticPrefix)) { res.writeHead(404).end(); return; }
    const file = resolve(admin, pathname.slice('/admin/'.length));
    if (!file.startsWith(admin + sep)) { res.writeHead(403).end(); return; }
    res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css; charset=utf-8' : 'text/javascript; charset=utf-8');
    res.end(await readFile(file));
  } catch {
    res.writeHead(404).end();
  }
});

await new Promise(done => server.listen(0, '127.0.0.1', done));
const base = `http://127.0.0.1:${server.address().port}`;
await mkdir(images, { recursive: true });
const browser = await chromium.launch({ headless: true });

async function openView(view) {
  const heights = { models: 480, config: 610, index: 900, projects: 440 };
  const page = await browser.newPage({ viewport: { width: 1440, height: heights[view] }, deviceScaleFactor: 1 });
  page.on('pageerror', e => pageErrors.push(`${view}: ${e.message}`));
  page.on('console', m => { if (m.type() === 'error') pageErrors.push(`${view}: console ${m.text()}`); });
  await page.route('**/*', async route => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.origin !== base) {
      unexpected.push(`external ${req.method()} ${url.origin}${url.pathname}`);
      return route.abort();
    }
    if (url.pathname.startsWith('/admin/api/')) {
      if (req.method() !== 'GET') {
        writes.push(`${req.method()} ${url.pathname}`);
        unexpected.push(`blocked API write ${req.method()} ${url.pathname}`);
        return route.abort();
      }
      let data;
      if (url.pathname === '/admin/api/models') data = { models, default: 'demo-compatible:aurora-small' };
      else if (url.pathname === '/admin/api/config') data = config;
      else if (url.pathname === '/admin/api/config/timeouts') data = timeouts;
      else if (url.pathname === '/admin/api/projects') data = { projects, discord_guild_id: '' };
      else if (url.pathname === '/admin/api/fs/browse') data = browse;
      else if (url.pathname.startsWith('/admin/api/projects/') && url.pathname.endsWith('/index/files')) data = indexedFiles;
      else {
        unexpected.push(`unexpected API GET ${url.pathname}`);
        return route.abort();
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) });
    }
    if (req.method() !== 'GET') {
      writes.push(`${req.method()} ${url.pathname}`);
      unexpected.push(`blocked HTTP write ${req.method()} ${url.pathname}`);
      return route.abort();
    }
    return route.continue();
  });
  await page.goto(`${base}/?view=${view}`);
  await page.waitForFunction(() => window.productTourReady === true, null, { timeout: 10000 });
  return page;
}

try {
  const modelsPage = await openView('models');
  await modelsPage.locator('#models-tbody tr[data-spec="demo-compatible:aurora-small"]').waitFor();
  await modelsPage.locator('#models-tbody tr[data-spec="demo-native:orion-review"]').waitFor();
  assert.equal(await modelsPage.locator('#models-tbody tr[data-spec]').count(), 4);
  assert.equal(await modelsPage.locator('#models-count').innerText(), '4 prendidos');
  assert.match(await modelsPage.locator('#models-tbody').innerText(), /Vision Unmeasured/);
  assert.match(await modelsPage.locator('#models-tbody').innerText(), /sin medir/);
  await modelsPage.screenshot({ path: join(images, 'models.png'), fullPage: true });
  await modelsPage.close();

  const rolesPage = await openView('config');
  for (const role of ['executor', 'planner', 'verifier', 'documenter', 'compactor']) {
    await rolesPage.locator(`#cfg-role-${role}`).waitFor();
    await rolesPage.locator(`#cfg-role-${role}-eff`).waitFor();
  }
  assert.match(await rolesPage.locator('#cfg-role-verifier-eff').innerText(), /demo-native:orion-review/);
  assert.equal(await rolesPage.locator('#cfg-role-planner').inputValue(), 'demo-compatible:aurora-small');
  assert.match(await rolesPage.locator('#cfg-model-prices').inputValue(), /"demo-\*"[\s\S]*"in": 0\.25[\s\S]*"out": 0\.75/);
  await rolesPage.screenshot({ path: join(images, 'model-roles.png'), fullPage: true });
  await rolesPage.close();

  const indexPage = await openView('index');
  await indexPage.locator('#browse-table tbody tr').filter({ hasText: 'aurora-dashboard' }).waitFor();
  await indexPage.locator('#files-table tbody tr').filter({ hasText: 'ProjectList.tsx' }).waitFor();
  assert.equal(await indexPage.locator('#browse-table tbody tr').count(), 2);
  assert.equal(await indexPage.locator('#files-table tbody tr').count(), 3);
  assert.match(await indexPage.locator('#browse-summary').innerText(), /2 entradas/);
  await indexPage.locator('#browse-table .pick').first().check();
  assert.equal(await indexPage.locator('#bulk-selected-count').innerText(), '1');
  assert.equal(await indexPage.locator('#bulk-start').isDisabled(), false);
  await indexPage.screenshot({ path: join(images, 'repository-index.png'), fullPage: true });
  await indexPage.close();

  const projectsPage = await openView('projects');
  await projectsPage.locator('#projects-table tbody tr[data-slug="aurora-dashboard"]').waitFor();
  await projectsPage.locator('#projects-table tbody tr[data-slug="inventory-api"]').waitFor();
  assert.equal(await projectsPage.locator('#projects-table tbody tr[data-slug]').count(), 2);
  await projectsPage.screenshot({ path: join(images, 'projects.png'), fullPage: true });
  await projectsPage.close();

  assert.deepEqual(writes, [], 'Product tour must not call a mutation endpoint');
  assert.deepEqual(unexpected, [], 'Product tour must not make unapproved requests');
  assert.deepEqual(pageErrors, [], 'Product tour must render without browser errors');
  console.log(JSON.stringify({ result: 'PASS', screenshots: ['models.png', 'model-roles.png', 'repository-index.png', 'projects.png'], apiWrites: writes.length, unexpectedRequests: unexpected.length, pageErrors: pageErrors.length }));
} finally {
  await browser.close();
  await new Promise(done => server.close(done));
}
