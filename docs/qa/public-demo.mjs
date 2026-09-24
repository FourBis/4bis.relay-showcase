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
const capabilities = ["models", "context", "execution", "workspace", "team", "commercial", "usage"];
async function checkCapabilities() {
  const tabs = page.locator("#capability-tabs [role=tab]");
  assert.equal(await tabs.count(), capabilities.length, "The capability catalog must expose seven tabs");
  assert.deepEqual(await tabs.evaluateAll(items => items.map(item => item.dataset.capability)), capabilities);

  for (const name of ["models", "context"]) {
    const tab = page.locator(`#capability-tabs [data-capability="${name}"]`);
    const label = tab.locator("span[data-en]");
    assert.match((await label.textContent()).toLowerCase(), name === "models" ? /modelo/ : /contexto/);
    assert.match((await label.getAttribute("data-en") || "").toLowerCase(), name === "models" ? /model/ : /context/);
  }

  await page.locator("#capabilities").scrollIntoViewIfNeeded();
  for (const name of capabilities) {
    const tab = page.locator(`#capability-tabs [data-capability="${name}"]`);
    await tab.click();
    assert.equal(await tab.getAttribute("aria-selected"), "true");
    const panel = page.locator(`#capability-${name}`);
    assert.equal(await panel.isVisible(), true);
    assert.equal(await page.locator("#capabilities [role=tabpanel]:visible").count(), 1, "Only the selected capability panel may be visible");
    const bilingualCopy = await panel.locator("[data-en]").evaluateAll(items => items.some(item => item.dataset.en?.trim()));
    assert(bilingualCopy, `${name} needs English copy alongside its Spanish copy`);

    const triggers = panel.locator(".screenshot-open");
    const triggerCount = await triggers.count();
    assert.equal(triggerCount, name === "models" ? 2 : 1, `${name} screenshot controls changed`);
    for (let index = 0; index < triggerCount; index++) {
      const trigger = triggers.nth(index);
      await trigger.scrollIntoViewIfNeeded();
      await trigger.click();
      const dialog = page.locator("#screenshot-dialog");
      await dialog.waitFor({ state: "visible" });
      const image = page.locator("#screenshot-image");
      await image.evaluate(el => el.scrollIntoView({ block: "center" }));
      await page.waitForFunction(() => {
        const image = document.querySelector("#screenshot-image");
        return image?.complete && image.naturalWidth > 0;
      });
      assert.match(await image.getAttribute("src"), /^assets\/[^/]+\.png$/i);
      const caption = page.locator("#screenshot-caption");
      assert((await caption.textContent()).trim().length > 0, `${name} screenshot needs a caption`);
      assert((await caption.getAttribute("data-es") || "").trim().length > 0, `${name} screenshot caption needs Spanish text`);
      assert((await caption.getAttribute("data-en") || "").trim().length > 0, `${name} screenshot caption needs English text`);
      assert.equal(await page.evaluate(() => document.activeElement?.id), "screenshot-close", "Opening the screenshot should move focus into its dialog");

      if (name === "models" && index === 0) {
        await page.keyboard.press("Escape");
        await dialog.waitFor({ state: "hidden" });
        assert.equal(await trigger.evaluate(el => el === document.activeElement), true, "Escape should return focus to the screenshot trigger");
      } else {
        await page.locator("#screenshot-close").click();
        await dialog.waitFor({ state: "hidden" });
        assert.equal(await trigger.evaluate(el => el === document.activeElement), true, "Closing should return focus to the screenshot trigger");
      }
    }
  }

  await page.locator("#capability-tabs [data-capability='models']").focus();
  await page.keyboard.press("ArrowRight");
  assert.equal(await page.locator("#capability-tabs [data-capability='context']").evaluate(el => el === document.activeElement), true, "ArrowRight should move tab focus");
  assert.equal(await page.locator("#capability-tabs [tabindex='0']").getAttribute("data-capability"), "context", "Roving tabindex should follow keyboard focus");
  await page.keyboard.press("Home");
  assert.equal(await page.locator("#capability-tabs [data-capability='models']").evaluate(el => el === document.activeElement), true, "Home should focus the first tab");
  assert.equal(await page.locator("#capability-tabs [tabindex='0']").getAttribute("data-capability"), "models");
  await page.keyboard.press("End");
  assert.equal(await page.locator("#capability-tabs [data-capability='usage']").evaluate(el => el === document.activeElement), true, "End should focus the last tab");
  assert.equal(await page.locator("#capability-tabs [tabindex='0']").getAttribute("data-capability"), "usage");

  await page.locator("#language").click();
  for (const name of ["models", "context"]) {
    const tab = page.locator(`#capability-tabs [data-capability="${name}"]`);
    assert.match((await tab.locator("span[data-en]").textContent()).toLowerCase(), name === "models" ? /model/ : /context/);
  }
  const modelReadme = page.locator("#capability-models a.text-link").first();
  assert.equal(await modelReadme.getAttribute("href"), await modelReadme.getAttribute("data-en-href"), "English mode should switch documentation links");
  await page.locator("#language").click();
  assert.equal(await modelReadme.getAttribute("href"), await modelReadme.getAttribute("data-es-href"), "Spanish mode should restore documentation links");
  const deliveryTargets = page.locator(".delivery-flow [data-capability-target]");
  const expectedDeliveryTargets = ["commercial", "commercial", "team", "context", "execution", "workspace", "workspace"];
  assert.equal(await deliveryTargets.count(), expectedDeliveryTargets.length);
  assert.deepEqual(await deliveryTargets.evaluateAll(items => items.map(item => item.dataset.capabilityTarget)), expectedDeliveryTargets);
  for (let index = 0; index < expectedDeliveryTargets.length; index++) {
    const name = expectedDeliveryTargets[index];
    await deliveryTargets.nth(index).click();
    assert.equal(await page.locator(`#capability-tab-${name}`).getAttribute("aria-selected"), "true", `Workflow link should select ${name}`);
    assert.equal(await page.locator(`#capability-${name}`).isVisible(), true, `Workflow link should show ${name}`);
  }
  const englishUrl = new URL(base);
  englishUrl.searchParams.set("lang", "en");
  await page.goto(englishUrl.href);
  assert.equal(await page.locator("html").getAttribute("lang"), "en", "Direct ?lang=en should initialize the site in English");
  assert((await page.locator("#headline").textContent()).includes("Your team and AI."));
  assert.equal(await page.locator("#capability-tab-models").locator("span[data-en]").textContent(), "Models");
  await page.goto(base);
  assert.equal(await page.locator("html").getAttribute("lang"), "es", "The default URL should initialize Spanish");
  for (const width of [768, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    await noOverflow();
  }
}
async function captureReviewScreenshots() {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.locator(".hero").screenshot({ path: join(out, "review-hero-desktop.png") });
  for (const name of ["models", "context", "team"]) {
    await page.locator(`#capability-tab-${name}`).click();
    await page.locator(`#capability-${name}`).screenshot({ path: join(out, `review-capability-${name}-desktop.png`) });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator(".hero").screenshot({ path: join(out, "review-hero-mobile.png") });
  await page.locator("#capabilities").screenshot({ path: join(out, "review-capabilities-mobile.png") });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.evaluate(() => scrollTo(0, 0));
}
try {
  assert.equal((await page.goto(base)).status(), 200);
  await page.locator('[data-chat="docs"] textarea').waitFor();
  await noOverflow();
  await checkCapabilities();
  await captureReviewScreenshots();
  await page.evaluate(() => scrollTo(0, 0));
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
  assert((await page.locator("#headline").textContent()).includes("Your team and AI."));
  assert.equal(await page.locator("nav").getAttribute("aria-label"), "Main navigation");
  assert.equal(await page.locator(".hero-evidence img[src*='workflow-graph']").count(), 1, "The hero should show the workflow screenshot");
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
    for (const name of capabilities) {
      const tab = page.locator(`#capability-tab-${name}`);
      await tab.click();
      assert.equal(await page.locator(`#capability-${name}`).isVisible(), true);
      assert.equal(await page.locator("#capabilities [role=tabpanel]:visible").count(), 1);
      await noOverflow();
      if (name === "team") {
        const trigger = page.locator("#capability-team .screenshot-open").first();
        await trigger.scrollIntoViewIfNeeded();
        await trigger.click();
        const dialog = page.locator("#screenshot-dialog");
        await dialog.waitFor({ state: "visible" });
        await page.waitForFunction(() => {
          const image = document.querySelector("#screenshot-image");
          return image?.complete && image.naturalWidth > 0;
        });
        await noOverflow();
        await page.locator("#screenshot-close").click();
        await dialog.waitFor({ state: "hidden" });
        assert.equal(await trigger.evaluate(el => el === document.activeElement), true);
      }
    }
    await page.screenshot({ path: join(out, `mobile-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  const visibleImages = [page.locator(".hero img")];
  for (const image of visibleImages) {
    await image.scrollIntoViewIfNeeded();
    await image.evaluate(el => el.scrollIntoView({ block: "center" }));
    await image.evaluate(el => el.decode());
    assert(await image.evaluate(el => el.complete && el.naturalWidth > 0), "Broken lazy-loaded image");
  }
  for (const name of capabilities) {
    await page.locator(`#capability-tab-${name}`).click();
    const image = page.locator(`#capability-${name} img`);
    await image.scrollIntoViewIfNeeded();
    await image.evaluate(el => el.scrollIntoView({ block: "center" }));
    await image.evaluate(el => el.decode());
    assert(await image.evaluate(el => el.complete && el.naturalWidth > 0), `${name} has a broken lazy-loaded image`);
  }
  assert(await page.locator("img:not(#screenshot-image)").evaluateAll(items => items.every(image => image.complete && image.naturalWidth > 0)), "Broken image");
  assert(requests.every(request => request.method === "GET" && !["fetch", "xhr", "websocket"].includes(request.type)), "Demo used a backend request");
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ result: "PASS", requests: requests.length, errors, screenshots: out, reviewScreenshots: ["review-hero-desktop.png", "review-capability-models-desktop.png", "review-capability-context-desktop.png", "review-capability-team-desktop.png", "review-hero-mobile.png", "review-capabilities-mobile.png"], checked: "7 capability panels and 8 screenshot triggers with captions, image loading and focus restoration; Arrow/Home/End roving tabs; delivery-flow targets; direct ?lang=en; 768/1440 desktop and all panels plus lightbox at 320/390 mobile without overflow; existing chats, split, team, PR and task-continuity demo; same-origin GET only, no backend requests" }, null, 2));
} finally {
  await browser.close();
  if (server.listening) await new Promise(done => server.close(done));
}
