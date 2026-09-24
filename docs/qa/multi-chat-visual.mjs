import { mkdir, readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, extname, join, normalize, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const root = normalize(join(dirname(fileURLToPath(import.meta.url)), "../.."));
const admin = join(root, "mcp-server", "admin_static");
const out = process.env.MULTI_CHAT_VISUAL_OUT
  || join(tmpdir(), "4bis-relay-multi-chat");
const conversations = {
  "conv-a": { id: "conv-a", project_slug: "demo", title: "Chat A", status: "open",
    branch: "codex/chat-a", messages_len: 0, last_activity_at: "2026-09-22 12:00:00" },
  "conv-b": { id: "conv-b", project_slug: "demo", title: "Chat B", status: "open",
    branch: "codex/chat-b", messages_len: 0, last_activity_at: "2026-09-22 12:01:00" },
};
const messages = { "conv-a": [], "conv-b": [] };
const requests = [];
const planRequests = [];
const destructive = [];
let base;

const mime = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png" };
const server = createServer(async (req, res) => {
  try {
    const pathname = new URL(req.url, base).pathname;
    let file;
    if (pathname === "/admin" || pathname === "/admin/") file = join(admin, "index.html");
    else if (pathname.startsWith("/admin/static/")) {
      file = join(admin, "static", decodeURIComponent(pathname.slice("/admin/static/".length)));
    } else { res.writeHead(404); res.end(); return; }
    const safe = normalize(file);
    if (!safe.startsWith(admin + sep)) { res.writeHead(403); res.end(); return; }
    res.setHeader("content-type", mime[extname(safe)] || "application/octet-stream");
    res.end(await readFile(safe));
  } catch (error) {
    console.error("fixture server:", req.url, error.message);
    res.writeHead(404); res.end();
  }
});

function json(route, value, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
}

async function mockRelay(route) {
  const req = route.request();
  const url = new URL(req.url());
  if (url.origin !== base || url.pathname === "/admin/"
      || url.pathname.startsWith("/admin/static/")) return route.continue();
  const path = url.pathname;
  const method = req.method();
  if (method === "POST" && (path.includes("/cancel") || path.endsWith("/close"))) {
    destructive.push({ method, path });
  }
  if (path === "/experts/run" && method === "POST") {
    const body = req.postDataJSON();
    requests.push(body);
    messages[body.conversation]?.push({ role: "user", content: body.user, requested_by: "owner" });
    return json(route, { id: `run-${body.conversation}`, status: "running",
      conversation_id: body.conversation });
  }
  if (path.startsWith("/experts/status/")) return json(route, {
    status: "ok", finished: true, phase: "done", tool_calls: 0,
  });
  const messageMatch = path.match(/^\/conversations\/([^/]+)\/messages$/);
  if (messageMatch) return json(route, { messages: messages[messageMatch[1]] || [] });
  const taskMatch = path.match(/^\/conversations\/([^/]+)\/task$/);
  if (taskMatch) return json(route, { id: taskMatch[1], mode: "write", state: "ready",
    workspace_state: "ok", publish_allowed: false, tracking: { enabled: false } });
  const conversationMatch = path.match(/^\/conversations\/([^/]+)$/);
  if (conversationMatch) {
    const item = conversations[conversationMatch[1]];
    return json(route, item || { error: "not found" }, item ? 200 : 404);
  }
  if (path === "/conversations") return json(route, { conversations: Object.values(conversations) });
  if (path === "/admin/api/projects") return json(route, { projects: [
    { slug: "demo", name: "Demo", repo_path: join(root, "fixture", "demo"), enabled: true },
  ] });
  if (path === "/admin/api/projects/demo") return json(route, { project: {
    slug: "demo", name: "Demo", repo_path: join(root, "fixture", "demo"), defaults_json: {},
  } });
  if (path === "/admin/api/models") return json(route, { models: [] });
  if (path === "/questions") return json(route, { questions: [] });
  if (path === "/chats") return json(route, { chats: [] });
  if (path.endsWith("/plan")) {
    planRequests.push({ path, at: Date.now() });
    return json(route, { modo: "sin_grafo", grafo: null });
  }
  if (path.includes("/suggestions")) return json(route, { suggestions: [] });
  if (path.includes("/context")) return json(route, { used: 0, limit: 1, percent: 0 });
  if (path === "/stats") return json(route, { running: 0, chats: 0 });
  if (path === "/system/active") return json(route, { active: [] });
  if (path === "/admin/api/health") return json(route, { status: "ok" });
  if (path === "/admin/api/me") return json(route, {
    role: "owner", email: "fixture", allowed_tabs: null,
  });
  return json(route, {});
}

function assert(condition, message) { if (!condition) throw new Error(message); }
const frameFor = (page, id) => page.frames().find(frame =>
  new URL(frame.url()).searchParams.get("chat-window") === id);

await mkdir(out, { recursive: true });
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
base = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1500, height: 900 } });
await context.route("**/*", mockRelay);
const pageErrors = [], consoleErrors = [];
try {
  const page = await context.newPage();
  page.on("pageerror", error => pageErrors.push(error.message));
  page.on("console", message => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  await page.goto(`${base}/admin/#/workspace`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#workspace-modules .workspace-module-group", { state: "attached" });
  await page.evaluate(async () => (await import("/admin/static/workspace.js"))
    .openWorkspaceModule("chat"));
  await page.locator("#workspace-chat-new").click();
  await page.locator("#chat-panel-title").filter({ hasText: "nueva conversación" }).waitFor();
  await page.locator("#chat-panel-input").fill("borrador principal");
  await page.locator("#chat-drawer-btn").click();
  await page.locator(".chat-conv-item[data-id='conv-a']").click();
  await page.waitForFunction(() => document.querySelectorAll(
    "article.workspace-window[data-window-id='object:conversation:conv-a']").length === 1);
  let frameA = frameFor(page, "conv-a");
  await frameA.locator("#chat-panel-input").waitFor();
  await frameA.locator("#chat-panel-input").fill("mensaje A");
  assert(requests.length === 0, "navegar a Chat A envió una solicitud al modelo");

  await page.locator(".workspace-dock-item[data-window-id='chat']").click();
  assert(await page.locator("#chat-panel-input").inputValue() === "borrador principal",
    "navegar a Chat A perdió el borrador principal");
  await page.locator("#chat-drawer-btn").click();
  await page.locator(".chat-conv-item[data-id='conv-b']").click();
  await page.waitForFunction(() => document.querySelectorAll(
    "article.workspace-window[data-window-id='object:conversation:conv-b']").length === 1);
  let frameB = frameFor(page, "conv-b");
  await frameB.locator("#chat-panel-input").waitFor();
  assert(requests.length === 0, "navegar a Chat B envió una solicitud al modelo");

  await page.locator(".workspace-dock-item[data-window-id='chat']").click();
  await page.locator("#chat-drawer-btn").click();
  await page.locator(".chat-conv-item[data-id='conv-a']").click();
  assert(await page.locator(
    "article.workspace-window[data-window-id='object:conversation:conv-a']").count() === 1,
  "reabrir Chat A creó una ventana duplicada");
  assert(await frameA.locator("#chat-panel-input").inputValue() === "mensaje A",
    "reabrir Chat A perdió su borrador");
  assert(requests.length === 0, "reabrir Chat A envió una solicitud al modelo");
  const conversationDockLabels = await page.locator(
    ".workspace-dock-item[data-window-id^='object:conversation:']").allTextContents();
  assert(conversationDockLabels.every(label => label.trim() === "demo"),
    `el dock expuso ID o título inesperado: ${conversationDockLabels.join(" | ")}`);

  const customTitleA = "Investigación de permisos y continuidad del workspace";
  const windowA = page.locator(
    "article.workspace-window[data-window-id='object:conversation:conv-a']");
  await windowA.locator("iframe").evaluate(node => { node.dataset.fixtureIdentity = "conv-a"; });
  await windowA.locator("[data-window-action='rename']").click();
  await page.locator("#confirm-modal-body input[aria-label='Nombre de la ventana']")
    .fill(customTitleA);
  await page.locator("#confirm-modal-ok").click();
  await windowA.locator(".workspace-window-title").filter({ hasText: customTitleA }).waitFor();
  assert(await windowA.locator("iframe").getAttribute("data-fixture-identity") === "conv-a",
    "renombrar reemplazó el iframe de Chat A");
  assert(await frameA.locator("#chat-panel-input").inputValue() === "mensaje A",
    "renombrar perdió el borrador de Chat A");
  const dockA = page.locator(
    ".workspace-dock-item[data-window-id='object:conversation:conv-a']");
  assert((await dockA.textContent())?.trim() === customTitleA,
    "el dock no reflejó el nombre personalizado");
  const dockBox = await dockA.evaluate(node => {
    const box = node.getBoundingClientRect(), parent = node.parentElement.getBoundingClientRect();
    return { width: box.width, right: box.right, parentRight: parent.right,
      maxWidth: parseFloat(getComputedStyle(node).maxWidth) };
  });
  assert(dockBox.width <= dockBox.maxWidth + 1 && dockBox.right <= dockBox.parentRight + 1,
    "el botón renombrado desbordó el dock");
  await page.waitForTimeout(150);

  await page.locator(".workspace-dock-item[data-window-id='chat']").click();
  await page.locator("article.workspace-window[data-window-id='chat']")
    .getByRole("button", { name: /^Minimizar/ }).click();
  await page.locator("#workspace-arrange").click();
  await page.waitForFunction(() => [...document.querySelectorAll("iframe")]
    .filter(frame => frame.contentDocument?.querySelector("#chat-panel-input")).length === 2);
  frameA = frameFor(page, "conv-a"); frameB = frameFor(page, "conv-b");
  assert(frameA && frameB, "no se abrieron ambos iframes de chat");
  await frameA.locator("#chat-panel-input").waitFor();
  await frameB.locator("#chat-panel-input").waitFor();

  const rects = await page.locator("article.workspace-window[data-window-id^='object:conversation:']:not([hidden])")
    .evaluateAll(nodes => nodes.map(node => { const r = node.getBoundingClientRect();
      return { x: r.x, y: r.y, right: r.right, bottom: r.bottom }; }));
  assert(rects.length === 2, `esperaba dos ventanas visibles, hay ${rects.length}`);
  assert(rects[0].right <= rects[1].x || rects[1].right <= rects[0].x,
    "las ventanas no quedaron lado a lado");

  await frameB.locator("#chat-panel-input").fill("mensaje B");
  const minimizeA = windowA.getByRole("button", { name: /^Minimizar/ });
  await minimizeA.click();
  assert(await page.locator("article.workspace-window[data-window-id='object:conversation:conv-a']").isHidden(),
    "minimizar no ocultó Chat A");
  await page.locator(".workspace-dock-item[data-window-id='object:conversation:conv-a']").click();
  assert(await frameA.locator("#chat-panel-input").inputValue() === "mensaje A",
    "restaurar perdió el borrador A");

  await frameA.locator("#chat-panel-send").click();
  await frameB.locator("#chat-panel-send").click();
  for (let i = 0; i < 50 && requests.length < 2; i++) await page.waitForTimeout(50);
  assert(requests.length === 2, `esperaba dos POST /experts/run, hubo ${requests.length}`);
  const byConversation = Object.fromEntries(requests.map(body => [body.conversation, body]));
  assert(byConversation["conv-a"]?.user === "mensaje A", "Chat A envió otra conversación/texto");
  assert(byConversation["conv-b"]?.user === "mensaje B", "Chat B envió otra conversación/texto");
  assert(byConversation["conv-a"].request_id !== byConversation["conv-b"].request_id,
    "los request_id de ambos chats se duplicaron");
  await frameA.locator("#chat-panel-send").waitFor({ state: "visible" });
  await frameB.locator("#chat-panel-send").waitFor({ state: "visible" });
  const bubbleA = frameA.locator("#chat-panel-messages div.whitespace-pre-wrap")
    .filter({ hasText: "mensaje A" });
  const bubbleB = frameB.locator("#chat-panel-messages div.whitespace-pre-wrap")
    .filter({ hasText: "mensaje B" });
  await bubbleA.first().waitFor();
  await bubbleB.first().waitFor();
  const countA = await bubbleA.count(), countB = await bubbleB.count();
  assert(countA === 1, `Chat A renderizó ${countA} copias del turno`);
  assert(countB === 1, `Chat B renderizó ${countB} copias del turno`);

  const requestsBeforeOpen = requests.length;
  await frameA.locator("#chat-panel-input").fill("/abrir projects");
  await frameA.locator("#chat-panel-send").click();
  const projectsWindow = page.locator("article.workspace-window[data-module='projects']:not([hidden])");
  await projectsWindow.waitFor();
  assert(requests.length === requestsBeforeOpen, "/abrir projects envió una solicitud al modelo");
  await projectsWindow.getByRole("button", { name: /^Cerrar/ }).click();
  await frameA.locator("#chat-panel-input").focus();
  await page.keyboard.press("Control+Shift+K");
  const launcher = page.locator("#workspace-launcher-dialog[open]");
  await launcher.waitFor();
  await page.keyboard.press("Escape");
  await launcher.waitFor({ state: "hidden" });

  const closeB = page.locator("article.workspace-window[data-window-id='object:conversation:conv-b']")
    .getByRole("button", { name: /^Cerrar/ });
  await closeB.focus();
  assert(await closeB.evaluate(node => document.activeElement === node),
    "el control cerrar no recibió foco de teclado");
  await page.keyboard.press("Enter");
  assert(destructive.length === 0, "cerrar UI llamó cancel/close del backend");
  await page.locator(".workspace-dock-item[data-window-id='chat']").click();
  await page.locator("#chat-drawer-btn").click();
  await page.locator(".chat-conv-item[data-id='conv-b']").click();
  await page.waitForFunction(() => !document.querySelector(
    "article.workspace-window[data-window-id='object:conversation:conv-b']")?.hidden);
  await page.locator(".workspace-dock-item[data-window-id='chat']").click();
  await page.locator("article.workspace-window[data-window-id='chat']")
    .getByRole("button", { name: /^Minimizar/ }).click();
  await page.locator("#workspace-arrange").click();
  frameA = frameFor(page, "conv-a"); frameB = frameFor(page, "conv-b");
  await frameA.locator("#chat-panel-input").fill("borrador efímero A");
  await frameB.locator("#chat-panel-input").fill("borrador efímero B");
  await page.waitForTimeout(250);
  await page.screenshot({ path: `${out}/multi-chat-desktop.png`, fullPage: true, animations: "disabled" });

  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.querySelectorAll(
    "article.workspace-window[data-window-id='object:conversation:conv-a'],article.workspace-window[data-window-id='object:conversation:conv-b']").length === 2);
  await page.waitForFunction(() => [...document.querySelectorAll("iframe")]
    .filter(frame => frame.contentDocument?.querySelector("#chat-panel-input")).length === 2);
  frameA = frameFor(page, "conv-a"); frameB = frameFor(page, "conv-b");
  assert((await page.locator(
    "article.workspace-window[data-window-id='object:conversation:conv-a'] .workspace-window-title")
    .textContent())?.trim() === customTitleA, "F5 perdió el nombre personalizado de Chat A");
  assert((await page.locator(
    ".workspace-dock-item[data-window-id='object:conversation:conv-a']").textContent())?.trim()
    === customTitleA, "F5 perdió el nombre personalizado en el dock");
  assert(await frameA.locator("#chat-panel-input").inputValue() === "", "F5 persistió texto A");
  assert(await frameB.locator("#chat-panel-input").inputValue() === "", "F5 persistió texto B");

  const mobile = await context.newPage();
  await mobile.setViewportSize({ width: 390, height: 844 });
  mobile.on("pageerror", error => pageErrors.push(error.message));
  mobile.on("console", message => { if (message.type() === "error") consoleErrors.push(message.text()); });
  await mobile.goto(`${base}/admin/#/workspace`, { waitUntil: "domcontentloaded" });
  await mobile.waitForFunction(() => document.querySelectorAll(
    "article.workspace-window[data-window-id='object:conversation:conv-a'],article.workspace-window[data-window-id='object:conversation:conv-b']").length === 2);
  const mobileVisible = await mobile.locator(
    "article.workspace-window[data-window-id^='object:conversation:']:not([hidden])").count();
  assert(mobileVisible === 1, `móvil mostró ${mobileVisible} chats simultáneos`);
  const mobileFrame = frameFor(mobile, "conv-b");
  assert(mobileFrame, "móvil no restauró el iframe activo");
  await mobileFrame.locator("#chat-panel-send").waitFor({ state: "visible" });
  await mobileFrame.locator("#chat-panel-input").waitFor({ state: "visible" });
  assert(await mobileFrame.locator("#chat-panel-input").isEnabled(),
    "móvil capturó el compositor todavía cargando");
  assert(!await mobile.evaluate(() => document.documentElement.scrollWidth > innerWidth),
    "workspace móvil tiene overflow horizontal");
  await mobile.screenshot({ path: `${out}/multi-chat-mobile-390.png`, fullPage: true, animations: "disabled" });

  assert(pageErrors.length === 0, `pageerror: ${pageErrors.join(" | ")}`);
  assert(consoleErrors.length === 0, `console.error: ${consoleErrors.join(" | ")}`);
  for (const id of ["conv-a", "conv-b"]) {
    const calls = planRequests.filter(call => call.path === `/conversations/${id}/plan`);
    assert(calls.length > 0, `${id} no consultó el plan`);
    assert(calls.filter(call => call.at - calls[0].at < 1000).length === 1,
      `${id} consultó el plan dos veces al abrir la conversación`);
  }
  console.log(JSON.stringify({ out, windows: ["conv-a", "conv-b"], requests: requests.map(r => ({
    conversation: r.conversation, request_id: r.request_id })), desktop: "side-by-side",
  mobile: "one-window-no-overflow", destructiveCalls: destructive.length, pageErrors: 0 }));
} finally {
  await context.close();
  await browser.close();
  server.close();
}
