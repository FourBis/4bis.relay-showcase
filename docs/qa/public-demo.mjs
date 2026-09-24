// Run: node docs/qa/public-demo.mjs (requires the existing Playwright tooling).
// Set PUBLIC_DEMO_URL to verify the published site instead of the local fixture.
import assert from "node:assert/strict";
import { mkdir, readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, extname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const site = resolve(dirname(fileURLToPath(import.meta.url)), "../../site");
const out = process.env.PUBLIC_DEMO_OUT || join(tmpdir(), "relay-public-demo");
const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml" };
const server = createServer(async (req, res) => {
  try {
    const path = decodeURIComponent(new URL(req.url, "http://localhost").pathname);
    const file = resolve(site, `.${path === "/" ? "/index.html" : path}`);
    if (!file.startsWith(site + sep)) { res.writeHead(403).end(); return; }
    res.setHeader("Content-Type", `${mime[extname(file)] || "application/octet-stream"}; charset=utf-8`);
    res.end(await readFile(file));
  } catch { res.writeHead(404).end(); }
});
let base = process.env.PUBLIC_DEMO_URL;
if (!base) {
  await new Promise(done => server.listen(0, "127.0.0.1", done));
  base = `http://127.0.0.1:${server.address().port}/`;
}
await mkdir(out, { recursive: true });
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
const page = await context.newPage();
const errors = [], requests = [];
page.on("pageerror", error => errors.push(error.message));
page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
page.on("request", request => requests.push({ url: request.url(), method: request.method(), type: request.resourceType() }));
await context.route("**/*", route => {
  if (new URL(route.request().url()).origin !== new URL(base).origin) {
    errors.push(`Unexpected external request: ${route.request().url()}`);
    return route.abort();
  }
  return route.continue();
});
const chat = id => page.locator(`[data-chat="${id}"]`);
const openChat = id => page.locator("#scene-chats").locator(`[data-open="${id}"]`);
const noOverflow = async () => assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "Page overflows horizontally");
try {
  assert.equal((await page.goto(base)).status(), 200);
  await page.locator('[data-chat="docs"] textarea').waitFor();
  await noOverflow();
  await page.screenshot({ path: join(out, "desktop.png"), fullPage: true });
  await chat("docs").locator("textarea").fill("borrador A");
  await openChat("access").click();
  assert.equal(await page.locator(".chat-window:visible").count(), 2);
  await chat("access").locator("textarea").fill("borrador B");
  await openChat("docs").click();
  assert.equal(await chat("docs").locator("textarea").inputValue(), "borrador A");
  assert.equal(await chat("access").locator("textarea").inputValue(), "borrador B");
  await chat("docs").getByRole("button", { name: /Renombrar/ }).click();
  await page.locator("#window-name").fill("Mi guía");
  await page.getByRole("button", { name: "Guardar nombre" }).click();
  await page.locator("#rename-dialog").waitFor({ state: "hidden" });
  await page.waitForFunction(() => document.querySelector('[data-chat="docs"] h3').textContent === "Mi guía");
  assert.equal(await chat("docs").locator("h3").textContent(), "Mi guía");
  await chat("docs").getByRole("button", { name: /Renombrar/ }).click();
  await page.locator("#window-name").fill("Descartado");
  await page.keyboard.press("Escape");
  assert.equal(await chat("docs").locator("h3").textContent(), "Mi guía");
  await chat("docs").getByRole("button", { name: /Cerrar/ }).click();
  await openChat("docs").click();
  assert.equal(await chat("docs").locator("textarea").inputValue(), "borrador A");
  const literal = '<img src=x onerror="alert(1)">';
  await chat("docs").locator("textarea").fill(literal);
  await chat("docs").getByRole("button", { name: "Probar" }).click();
  assert((await chat("docs").locator(".message.user").textContent()).includes(literal));
  assert.equal(await chat("docs").locator("img").count(), 0);
  assert.equal(await chat("access").locator("textarea").inputValue(), "borrador B");
  await page.locator(".workbench").screenshot({ path: join(out, "conversations.png") });
  await page.locator("#tab-chats").focus();
  await page.keyboard.press("ArrowRight");
  assert.equal(await page.locator("#tab-workspace").getAttribute("aria-selected"), "true");
  const record = await page.locator(".record code").allTextContents();
  await page.locator("#resume").click();
  assert.equal(await page.locator("#turn-count").textContent(), "2");
  assert.deepEqual(await page.locator(".record code").allTextContents(), record);
  assert((await page.locator(".graph-flow").first().textContent()).includes("Espera la migración"));
  await page.screenshot({ path: join(out, "long-task-before.png"), fullPage: true });
  await page.locator("#split-task").click();
  assert.equal(await page.locator("#task-children li").count(), 4);
  assert.equal(await page.locator("#task-children li ol").count(), 0, "Subtasks must not subdivide again");
  assert.equal(await page.locator("#task-children li").filter({ hasText: "Completada" }).count(), 1);
  assert.equal(await page.locator("#task-children li").filter({ hasText: "En curso" }).count(), 1);
  assert((await page.locator("#graph-after").textContent()).includes("espera a que terminen"));
  assert(await page.locator("#split-task").isDisabled());
  assert.equal(await page.locator("#task-children").locator("li").count(), 4);
  await page.screenshot({ path: join(out, "long-task-after.png"), fullPage: true });
  await page.locator("#tab-workspace").click();
  await page.locator("#tab-team").click();
  assert(await page.locator("#allow-write").isDisabled());
  assert(await page.locator("#continue-task").isDisabled());
  assert(await page.locator("#publish-task").isDisabled());
  assert.equal(await page.locator(".team-table tbody tr").count(), 2);
  assert((await page.locator(".team-table").textContent()).includes("Alex"));
  await page.locator("#edit-alex").click();
  await page.locator("#project-picker").selectOption(["web", "shared"]);
  await page.locator("#save-projects").click();
  assert((await page.locator("#alex-projects").textContent()).includes("Migración del cliente web"));
  assert(await page.locator("#allow-write").isDisabled(), "Account link is a separate prerequisite");
  await page.locator("#connect-github").click();
  assert(await page.locator("#allow-write").isEnabled());
  assert(await page.locator("#continue-task").isDisabled(), "Account and project access do not continue the task");
  await page.locator("#allow-write").click();
  assert(await page.locator("#continue-task").isEnabled());
  assert((await page.locator("#execution-state").textContent()).includes("En pausa"));
  await page.locator(".workbench").screenshot({ path: join(out, "team-access.png") });
  assert.equal(await page.locator("#continue-task").isEnabled(), true);
  await page.locator("#continue-task").click();
  assert((await page.locator("#execution-state").textContent()).includes("Continuada"));
  assert(await page.locator("#publish-task").isEnabled());
  await page.locator("#tab-pr").click();
  assert(await page.locator("#publish-task").isEnabled());
  await page.locator("#publish-task").click();
  assert((await page.locator("#publish-result").textContent()).includes("sin merge ni integración"));
  assert((await page.locator("#review-copy").textContent()).includes("no se hizo merge"));
  assert(await page.locator("#feedback").isEnabled());
  await page.locator("#feedback").click();
  assert((await page.locator("#feedback-result").textContent()).includes("no se creó ni modificó ninguna PR real"));
  await page.screenshot({ path: join(out, "example-pr.png"), fullPage: true });
  await page.locator("#tab-workspace").click();
  await page.locator(".diff summary").click();
  assert((await page.locator(".diff").textContent()).includes("No se modificaron archivos reales"));
  await page.locator(".workbench").screenshot({ path: join(out, "long-task.png") });
  await page.locator("#tab-workspace").click();
  await page.locator("#resume").click();
  assert.equal(await page.locator("#task-children li").count(), 4, "Restart must retain the fictional split task");
  await page.locator("#tab-pr").focus();
  await page.keyboard.press("End");
  assert.equal(await page.locator("#tab-pr").getAttribute("aria-selected"), "true");
  assert(await page.locator("#feedback").isDisabled());
  assert((await page.locator("#review-copy").textContent()).includes("no se hizo merge"));
  await page.locator("#language").click();
  assert.equal(await page.locator("html").getAttribute("lang"), "en");
  assert((await page.locator("#headline").textContent()).includes("Long tasks"));
  assert.equal(await page.locator("nav").getAttribute("aria-label"), "Main navigation");
  assert((await page.locator(".hero-task").textContent()).includes("FICTIONAL TASK"));
  assert((await page.locator('meta[http-equiv="Content-Security-Policy"]').getAttribute("content")).includes("connect-src 'none'"));
  await page.locator("#tab-workspace").click();
  assert((await page.locator("#split-result").textContent()).includes("Budget exhausted"));
  assert((await page.locator("#task-children").textContent()).includes("Completed"));
  await page.locator("#tab-team").click();
  assert((await page.locator("#execution-state").textContent()).includes("continued"));
  assert((await page.locator("#alex-projects").textContent()).includes("Web client migration"));
  assert((await page.locator("#repo-state").textContent()).includes("Personal account linked"));
  await page.locator("#tab-pr").click();
  assert((await page.locator("#review-copy").textContent()).includes("no merge or integration"));
  await page.locator("#language").click();
  await page.locator("#reset").click();
  assert.equal(await page.locator(".chat-window").count(), 1);
  assert.equal(await chat("docs").locator("textarea").inputValue(), "");
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await noOverflow();
    await openChat("access").click();
    assert.equal(await page.locator(".chat-window:visible").count(), 1);
    await chat("access").locator("textarea").fill("mobile draft");
    await openChat("docs").click();
    await openChat("access").click();
    assert.equal(await chat("access").locator("textarea").inputValue(), "mobile draft");
    for (const scene of ["workspace", "team", "pr", "chats"]) {
      await page.locator(`#tab-${scene}`).click();
      await noOverflow();
    }
    await page.screenshot({ path: join(out, `mobile-${width}.png`), fullPage: true });
  }
  assert(await page.locator("img").evaluateAll(images => images.every(image => image.complete && image.naturalWidth > 0)), "Broken image");
  assert(requests.every(request => request.method === "GET" && !["fetch", "xhr", "websocket"].includes(request.type)), "Demo used a backend request");
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ result: "PASS", requests: requests.length, errors, screenshots: out, checked: "dependency graph before/after one-level split, retained parent and waiting dependent, personal account and project assignment, explicit write/continue, separate PR publication without merge, task/diff continuity, keyboard, ES/EN, 320/390 mobile, same-origin GET only, no backend requests" }, null, 2));
} finally {
  await browser.close();
  if (server.listening) await new Promise(done => server.close(done));
}
