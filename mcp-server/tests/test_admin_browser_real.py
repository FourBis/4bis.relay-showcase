"""RELAY_TEST_UI=1: admin real + SQLite temporal + Chrome, sin proveedores externos."""
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestServer

from relay import admin, attachments, server, server_common


@pytest.mark.skipif(os.environ.get("RELAY_TEST_UI") != "1", reason="integración UI opt-in con Chrome")
async def test_chat_states_and_images_in_real_browser(tmp_path, monkeypatch):
    pw = pytest.importorskip("playwright.async_api")
    for key, value in {"FOURBIS_DB_PATH": tmp_path / "relay.db", "STATE_DIR": tmp_path / "state",
                       "FOURBIS_ATTACHMENTS_DIR": tmp_path / "attachments",
                       "FOURBIS_CHATS_DIR": tmp_path / "chats", "FOURBIS_JSONL_DIR": tmp_path / "jsonl",
                       "BOT_NOTIFY_URL": "http://127.0.0.1:9"}.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setattr(server_common, "_get_api_key", lambda: "")
    monkeypatch.setattr(admin, "_cbm_list_projects", AsyncMock(return_value={"projects": []}))
    evidence = Path(os.environ.get("RELAY_TEST_UI_OUTPUT", tmp_path / "screenshots"))
    evidence.mkdir(parents=True, exist_ok=True)
    async with TestServer(server.create_app()) as http, pw.async_playwright() as browser_api:
        db = http.app[server.DB_KEY]
        await db.upsert_project(dict(slug="ui-test", name="UI test", repo_path=str(tmp_path)))
        browser = await browser_api.chromium.launch(channel="chrome", headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors, console_errors = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            await page.goto(str(http.make_url("/admin")) + "#/chat")
            await page.wait_for_load_state("networkidle")
            await pw.expect(page.locator("#chat-conv-list")).to_contain_text("sin conversaciones todavía")
            conv = await db.create_conversation(project_slug="ui-test")
            await page.locator("button.chat-drawer-btn:visible").first.click()
            await page.locator("#chat-refresh").click()
            await page.locator(f'.chat-conv-item[data-id="{conv}"]').click()
            conversation_window = page.locator(
                f'article.workspace-window[data-window-id="object:conversation:{conv}"]')
            await pw.expect(conversation_window).to_be_visible(timeout=10_000)
            conversation = conversation_window.frame_locator("iframe")
            await pw.expect(conversation.locator("#chat-panel-messages")).to_contain_text("Sin mensajes todavía")
            await pw.expect(conversation.locator("#chat-grafo")).to_be_hidden()
            await page.screenshot(path=str(evidence / "chat-empty.png"))

            # Bloqueo determinista de la respuesta, sin dormir para simular carga.
            release = asyncio.Event()
            async def hold(route):
                await release.wait()
                await route.continue_()
            await page.route("**/messages?**", hold)
            await conversation_window.locator("iframe").evaluate(
                "el => el.contentWindow.location.reload()")
            await pw.expect(conversation.locator('#chat-panel-messages [role="status"]')).to_have_text(
                "Cargando mensajes…")
            release.set()
            await pw.expect(conversation.locator("#chat-panel-messages")).to_contain_text("Sin mensajes todavía")
            await page.unroute("**/messages?**", hold)

            # Falla HTTP deliberada y acción de recuperación en el mismo panel.
            async def fail(route):
                await route.fulfill(status=503, content_type="application/json", body='{"error":"prueba de recuperación"}')
            await page.route("**/messages?**", fail)
            await conversation_window.locator("iframe").evaluate(
                "el => el.contentWindow.location.reload()")
            await pw.expect(conversation.locator('#chat-panel-messages [role="alert"]')).to_contain_text(
                "No se pudo leer")
            await page.screenshot(path=str(evidence / "chat-error.png"))
            await page.unroute("**/messages?**", fail)

            screenshot = await page.screenshot()
            aid, _, _ = attachments.store(screenshot, mimetype="image/png")
            for i, (status, verdict, outcome) in enumerate([
                ("ok", "complete", "aprobado"), ("ok", "needs_more", "pendiente"),
                ("cancelled", "", "sin_verificar")]):
                cid = await db.create_chat(project_slug="ui-test", source="test", author="", target="ui-test", conversation_id=conv)
                await db.finish_chat(cid, status=status, duration_ms=1200,
                    stages_json=json.dumps(dict(resultado=outcome, verifier_verdict=verdict)),
                    artifact=dict(user=f"Caso {i}", content=("Resultado guardado. " + f"![Captura de prueba](/attachments/{aid})") if i == 0 else "Avance parcial."))
            await conversation.get_by_role("button", name="Reintentar", exact=True).click()
            await pw.expect(conversation.locator("#chat-panel-messages")).to_contain_text("Tarea cumplida")
            for label in ("Ejecución terminada", "Trabajo pendiente", "Sin verificar", "Ejecución cancelada", "Exportación pendiente"):
                await pw.expect(conversation.locator("#chat-panel-messages")).to_contain_text(label)
            img = conversation.locator(f'#chat-panel-messages img[src="/attachments/{aid}"]')
            await pw.expect(img).to_be_visible()
            assert await img.evaluate("el => el.complete && el.naturalWidth > 0")
            for width, height in [(1440, 1000), (390, 844)]:
                await page.set_viewport_size({"width": width, "height": height})
                await page.evaluate("Promise.all(document.getAnimations().filter(a => a.effect.getTiming().iterations !== Infinity).map(a => a.finished.catch(() => {})))")
                await pw.expect(conversation.locator("#chat-grafo")).to_be_hidden()
                await conversation.locator('[aria-label="Estado del resultado"]').first.scroll_into_view_if_needed()
                await page.screenshot(path=str(evidence / f"chat-{width}.png"))
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), await page.evaluate("[...document.querySelectorAll('body *')].filter(e => e.getBoundingClientRect().right > innerWidth + 1).map(e => [e.id, e.className]).slice(0,20)")
                assert await conversation.locator('[aria-label="Estado del resultado"]').count() == 3
                assert await conversation.locator("body").evaluate(
                    "() => document.documentElement.scrollWidth <= innerWidth + 1")
            await conversation.locator("#chat-panel-input").focus()
            await pw.expect(conversation.locator("#chat-panel-input")).to_be_focused()
            assert not errors, errors
            assert all("503" in error for error in console_errors), console_errors
        finally:
            await browser.close()
