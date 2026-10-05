"""UI de acceso nativo con Chrome y GitHub simulado; nunca usa OAuth real."""
import json
import os
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer

from relay import server


@pytest.mark.skipif(os.environ.get("RELAY_TEST_UI") != "1", reason="integración UI opt-in con Chrome")
async def test_bootstrap_login_error_retry_and_logout_in_real_browser(tmp_path, monkeypatch):
    pw = pytest.importorskip("playwright.async_api")
    for key, value in {
        "FOURBIS_DB_PATH": tmp_path / "relay.db",
        "STATE_DIR": tmp_path / "state",
        "FOURBIS_ATTACHMENTS_DIR": tmp_path / "attachments",
        "FOURBIS_CHATS_DIR": tmp_path / "chats",
        "FOURBIS_JSONL_DIR": tmp_path / "jsonl",
    }.items():
        monkeypatch.setenv(key, str(value))

    from relay.admin_observability import ADMIN_STATIC_DIR

    session = {"authenticated": False, "configured": False, "setup_required": True}
    setup_requests, start_requests, logout_requests = [], [], []
    evidence = Path(os.environ.get("RELAY_TEST_UI_OUTPUT", tmp_path / "screenshots")).resolve()
    evidence.mkdir(parents=True, exist_ok=True)

    async with TestServer(server.create_app()) as http, pw.async_playwright() as browser_api:
        origin = str(http.make_url("/")).rstrip("/")
        account_url = origin + "/admin/#/account"
        browser = await browser_api.chromium.launch(channel="chrome", headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            page_errors = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))

            async def admin_document(route):
                name = "index.html" if session["authenticated"] else "login.html"
                await route.fulfill(path=str(ADMIN_STATIC_DIR / name), content_type="text/html")

            async def status(route):
                await route.fulfill(json={
                    "setup_required": session["setup_required"],
                    "configured": session["configured"],
                    "authenticated": session["authenticated"],
                    "enabled": session["authenticated"],
                    "local_setup": not session["authenticated"],
                    "callback_url": origin + "/admin/api/account/github/callback",
                })

            async def setup(route):
                body = json.loads(route.request.post_data or "{}")
                setup_requests.append(body)
                if len(setup_requests) == 1:
                    await route.fulfill(status=400, json={
                        "error": "invalid_client",
                        "message": f"client_secret={body.get('client_secret', '')}",
                    })
                    return
                session["configured"] = True
                await route.fulfill(json={"configured": True})

            async def start(route):
                start_requests.append(route.request.method)
                if len(start_requests) == 1:
                    await route.fulfill(status=503, json={"error": "temporal", "message": "GitHub no está disponible ahora."})
                    return
                await route.fulfill(json={
                    "authorization_url": "https://github.com/login/oauth/authorize?client_id=fixture&state=local-test",
                })

            async def logout(route):
                logout_requests.append(route.request.method)
                session.update(authenticated=False, setup_required=False)
                await route.fulfill(json={"logged_out": True})

            async def me(route):
                await route.fulfill(json={"email": "admin@example.test", "role": "owner", "allowed_tabs": None})

            async def connections(route):
                await route.fulfill(json={"email": "admin@example.test", "native_login": True,
                    "local_identity": False, "connections": []})

            async def fake_github(route):
                session.update(authenticated=True, configured=True, setup_required=False)
                destination = json.dumps(account_url)
                await route.fulfill(content_type="text/html", body=
                    "<!doctype html><title>GitHub simulado</title>"
                    f"<script>location.replace({destination})</script>")

            await page.route("**/admin/", admin_document)
            await page.route("**/admin/api/auth/status", status)
            await page.route("**/admin/api/auth/setup", setup)
            await page.route("**/admin/api/auth/github/start", start)
            await page.route("**/admin/api/auth/logout", logout)
            await page.route("**/admin/api/me", me)
            await page.route("**/admin/api/account/connections", connections)
            await page.route("https://github.com/**", fake_github)

            await page.goto(origin + "/admin/")
            await pw.expect(page.locator("#login-setup-form")).to_be_visible()
            await pw.expect(page.locator("#login-error")).to_be_hidden()
            await page.screenshot(path=str(evidence / "login-empty.png"))

            first_secret = "synthetic-secret-ui-check-7429"
            await page.locator("#login-client-id").fill("fixture-client-id-1234")
            await page.locator("#login-client-secret").fill(first_secret)
            await page.locator("#login-setup-submit").click()
            await pw.expect(page.locator("#login-error")).to_contain_text("[oculto]")
            assert first_secret not in await page.locator("body").inner_text()
            assert await page.locator("#login-client-secret").input_value() == ""
            await page.screenshot(path=str(evidence / "login-setup-error.png"))

            await page.locator("#login-client-secret").fill("synthetic-secret-ui-check-7430")
            await page.locator("#login-setup-submit").click()
            await pw.expect(page.locator("#login-error")).to_contain_text("503: GitHub no está disponible ahora.")
            await pw.expect(page.locator("#login-authorize")).to_be_enabled()
            await pw.expect(page.locator("#login-edit-details")).to_be_visible()
            await pw.expect(page.locator("#login-app-instructions")).to_be_hidden()
            await page.screenshot(path=str(evidence / "login-start-error.png"))

            await page.locator("#login-authorize").click()
            await pw.expect(page.locator("#account-logout")).to_be_visible(timeout=15_000)
            await page.screenshot(path=str(evidence / "login-account.png"))
            assert await page.locator("#account-logout").inner_text() == "Cerrar sesión"

            await page.locator("#account-logout").click()
            await pw.expect(page.locator("#login-setup-header")).to_be_hidden()
            await pw.expect(page.locator("#login-app-instructions")).to_be_hidden()
            await pw.expect(page.locator("#login-authorize")).to_have_text("Iniciar sesión con GitHub")
            await pw.expect(page.locator("#login-edit-details")).to_be_hidden()
            await page.screenshot(path=str(evidence / "login-logout.png"))
            assert len(setup_requests) == 2
            assert start_requests == ["POST", "POST"]
            assert logout_requests == ["POST"]
            assert not page_errors, page_errors
        finally:
            await browser.close()
