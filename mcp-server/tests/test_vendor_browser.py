"""RELAY_TEST_UI=1: comprueba los vendors reales, incluido el bundle reconstruido."""
import os
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestServer

from relay import admin, server


@pytest.mark.skipif(os.environ.get("RELAY_TEST_UI") != "1", reason="requiere Chrome")
async def test_diagrams_and_html_sanitizer(tmp_path, monkeypatch):
    pw = pytest.importorskip("playwright.async_api")
    monkeypatch.setenv("FOURBIS_DB_PATH", str(tmp_path / "relay.db"))
    monkeypatch.setattr(admin, "_cbm_list_projects", AsyncMock(return_value={"projects": []}))
    async with TestServer(server.create_app()) as http, pw.async_playwright() as api:
        browser = await api.chromium.launch(channel="chrome", headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(str(http.make_url("/admin/")) + "#/diagrams")
            await pw.expect(page.locator("#mermaid-preview svg")).to_be_visible()
            assert await page.evaluate("mermaid.mermaidAPI.getConfig().securityLevel") == "strict"
            for code in [
                "flowchart TD\n A[Hello] --> B[World]",
                "sequenceDiagram\n Alice->>Bob: Hello",
                "classDiagram\n Animal <|-- Duck",
                "---\ntitle: Example\n---\ngraph TD\n A --> B",
            ]:
                await page.locator("#mermaid-code").fill(code)
                await page.locator("#mermaid-render").click()
                await pw.expect(page.locator("#mermaid-preview svg")).to_be_visible()
            safe = await page.evaluate("""async () => {
                const {default: purify} = await import('/admin/static/vendor-purify-3.4.15.es.js');
                const html = purify.sanitize('<img src=x onerror="window.COMPROMISED=1">'
                  + '<a href="javascript:window.COMPROMISED=1">link</a>');
                const el = document.createElement('div');
                el.innerHTML = html;
                document.body.append(el);
                return !el.querySelector('[onerror], [href^="javascript:"]') && !window.COMPROMISED;
            }""")
            assert safe
            assert not errors, errors
        finally:
            await browser.close()
