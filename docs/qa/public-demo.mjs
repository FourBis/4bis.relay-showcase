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
const noOverflow = async () => assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "Page overflows horizontally");
try {
  assert.equal((await page.goto(base)).status(), 200);
  await page.locator('[data-chat="docs"] textarea').waitFor();
  await noOverflow();
  await page.screenshot({ path: join(out, "desktop.png"), fullPage: true });
  await chat("docs").locator("textarea").fill("borrador A");
  await page.locator('[data-open="access"]').click();
  assert.equal(await page.locator(".chat-window:visible").count(), 2);
  await chat("access").locator("textarea").fill("borrador B");
  await page.locator('[data-open="docs"]').click();
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
  await page.locator('[data-open="docs"]').click();
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
  await page.locator("#tab-workspace").focus();
  await page.keyboard.press("End");
  await page.locator("#feedback").click();
  assert(await page.locator("#feedback").isDisabled());
  assert((await page.locator("#review-copy").textContent()).includes("#17"));
  await page.locator("#language").click();
  assert.equal(await page.locator("html").getAttribute("lang"), "en");
  assert((await page.locator("#headline").textContent()).includes("Conversations"));
  assert.equal(await page.locator("nav").getAttribute("aria-label"), "Main navigation");
  assert((await page.locator(".hero-evidence img").getAttribute("alt")).startsWith("Two example"));
  await page.locator("#reset").click();
  assert.equal(await page.locator(".chat-window").count(), 1);
  assert.equal(await chat("docs").locator("textarea").inputValue(), "");
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await noOverflow();
    await page.locator('[data-open="access"]').click();
    assert.equal(await page.locator(".chat-window:visible").count(), 1);
    await chat("access").locator("textarea").fill("mobile draft");
    await page.locator('[data-open="docs"]').click();
    await page.locator('[data-open="access"]').click();
    assert.equal(await chat("access").locator("textarea").inputValue(), "mobile draft");
    for (const scene of ["workspace", "pr", "chats"]) {
      await page.locator(`#tab-${scene}`).click();
      await noOverflow();
    }
    await page.screenshot({ path: join(out, `mobile-${width}.png`), fullPage: true });
  }
  assert(await page.locator("img").evaluateAll(images => images.every(image => image.complete && image.naturalWidth > 0)), "Broken image");
  assert(requests.every(request => request.method === "GET" && !["fetch", "xhr", "websocket"].includes(request.type)), "Demo used a backend request");
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ result: "PASS", requests: requests.length, errors, screenshots: out, checked: "independent drafts, rename/cancel, close/reopen, literal input, task/PR continuity, keyboard, ES/EN, 320/390 mobile, no external/backend requests" }, null, 2));
} finally {
  await browser.close();
  if (server.listening) await new Promise(done => server.close(done));
}
