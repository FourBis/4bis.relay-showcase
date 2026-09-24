import { mkdir } from "node:fs/promises";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize, sep } from "node:path";
import { tmpdir } from "node:os";
import { chromium } from "playwright";

const root = normalize(join(dirname(fileURLToPath(import.meta.url)), "../.."));
const out = process.env.TASK_PANEL_VISUAL_OUT || join(tmpdir(), "4bis-task-panel-visual");

let base;
const htmlFor = (urlBase) => `<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="${urlBase}/mcp-server/admin_static/static/admin.css">
<style>body{background:#09090b;color:#f4f4f5;padding:24px;font-family:system-ui}.fixture{max-width:760px;margin:auto}</style></head>
<body><main class="fixture"><p class="text-xs text-zinc-500">fixture: verificación simulada</p>
<h1 class="panel-title">Tarea persistente</h1><section id="chat-task-panel" class="chat-task-panel card" hidden aria-live="polite"></section>
<p id="fixture-status" aria-live="polite"></p><div id="toast-root"></div></main>
<script type="module">
  let task = {id:'fixture-task', mode:'write', state:'review', publish_allowed:true, branch:'codex/fixture', head_sha:'abc123',
    pr_url:'https://example.invalid/pr/1', validation:{head_sha:'abc123',status:'ok',detail:'fixture: validación simulada'},
    pending_events:2, tracking:{enabled:false,max_iterations:3,max_tokens:50000}};
  const originalFetch = window.fetch;
  window.__actions = [];
  window.fetch = async (url, opts = {}) => {
    if (!String(url).includes('/conversations/fixture/task')) return originalFetch(url, opts);
    if (opts.method === 'POST') {
      const body = JSON.parse(opts.body || '{}');
      window.__actions.push(body.action);
      task = {...task, state: body.action === 'cancel' ? 'cancelled' : body.action === 'pause' ? 'paused' : body.action === 'continue' ? 'running' : task.state,
        tracking: body.action === 'track' ? {...task.tracking, enabled: !!body.enabled} : task.tracking};
    }
    return new Response(JSON.stringify(task), {status:200, headers:{'Content-Type':'application/json'}});
  };
  const mod = await import('${urlBase}/mcp-server/admin_static/static/chat-task.js');
  window.__task = mod.mountTaskPanel({convId:'fixture'});
  window.__setTask = (next) => { task = next; return window.__task.refresh(); };
</script></body></html>`;

await mkdir(out, {recursive:true});
const server = createServer(async (req, res) => {
  try {
    const path = new URL(req.url, base).pathname;
    if (path === "/fixture.html") {
      res.setHeader("content-type", "text/html; charset=utf-8");
      res.end(htmlFor(base));
      return;
    }
    const relative = decodeURIComponent(path).replace(/^\/+/, "");
    const file = join(root, relative);
    if (!file.startsWith(root + sep)) { res.writeHead(403); res.end(); return; }
    if (file.endsWith('.js')) res.setHeader('content-type', 'text/javascript; charset=utf-8');
    if (file.endsWith('.css')) res.setHeader('content-type', 'text/css; charset=utf-8');
    res.end(await readFile(file));
  } catch (error) { console.error('fixture server:', req.url, error.message); res.writeHead(404); res.end(); }
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
base = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({headless:true});
const pageErrors = [];
try {
  const page = await browser.newPage({viewport:{width:1100,height:720}});
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('pageerror', (error) => console.error('pageerror:', error.message));
  page.on('console', (message) => console.error('browser:', message.text()));
  await page.goto(`${base}/fixture.html`, {waitUntil:'domcontentloaded'});
  await page.waitForFunction(() => !!window.__task, null, {timeout:10000});
  const continueButton = page.getByRole('button', {name:'Continuar'});
  await continueButton.focus();
  await page.keyboard.press('Tab');
  if (await page.evaluate(() => document.activeElement?.dataset.taskAction) !== 'pause') throw new Error('Tab no llegó a Pausar');
  await page.keyboard.press('Shift+Tab');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(100);
  if (await page.evaluate(() => document.activeElement?.dataset.taskAction) !== 'pause') throw new Error('Foco perdido al cambiar el estado');
  await page.getByRole('checkbox', {name:'Activar seguimiento automático'}).check();
  await page.waitForTimeout(200);
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('overflow desktop');
  await page.screenshot({path:`${out}/task-panel-validado.png`, fullPage:true});
  await page.evaluate(() => window.__setTask({id:'fixture-task',mode:'write',state:'blocked',error:'fixture: error simulado',uncertain_events:[],tracking:{enabled:false,max_iterations:3,max_tokens:50000}}));
  await page.screenshot({path:`${out}/task-panel-error.png`, fullPage:true});
  await page.evaluate(() => window.__setTask({id:'fixture-task',mode:'write',state:'ready',
    can_control:false,allowed_actions:['continue','pause','cancel'],tracking:{enabled:false}}));
  const beforeDevClick = await page.evaluate(() => window.__actions.length);
  await page.getByRole('button', {name:'Continuar'}).click();
  await page.waitForFunction((before) => window.__actions.length === before + 1, beforeDevClick);
  if (await page.evaluate(() => window.__actions.at(-1)) !== 'continue') throw new Error('Dev no envió Continue autorizado');
  if (await page.locator('[data-task-action="publish"], [data-task-track]').count()) throw new Error('Dev recibió publicación o seguimiento');
  const mobile = await browser.newPage({viewport:{width:360,height:740}});
  mobile.on('pageerror', error => pageErrors.push(error.message));
  await mobile.goto(`${base}/fixture.html`, {waitUntil:'domcontentloaded'});
  await mobile.waitForFunction(() => !!window.__task, null, {timeout:10000});
  if (await mobile.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('overflow mobile');
  await mobile.screenshot({path:`${out}/task-panel-mobile-360.png`, fullPage:true});
  if (pageErrors.length) throw new Error(pageErrors.join('\n'));
  console.log(JSON.stringify({out, keyboard:'Continuar focused and activated', dev:'redacted task allowed_actions sends Continue POST', toggle:'track enabled', error:'rendered', mobile:'360x740'}));
} finally {
  await browser.close();
  server.close();
}
