"""QA Chrome opt-in con HTTP/SQLite temporales y respuestas GitHub simuladas."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestServer

from relay import admin, server, server_common, identity, native_auth


@pytest.mark.skipif(os.environ.get("RELAY_TEST_UI") != "1", reason="integración UI opt-in con Chrome")
async def test_common_issue_link_and_sync_states_in_real_browser(tmp_path, monkeypatch):
    pw = pytest.importorskip("playwright.async_api")
    for key, value in {
        "FOURBIS_DB_PATH": tmp_path / "relay.db", "STATE_DIR": tmp_path / "state",
        "FOURBIS_ATTACHMENTS_DIR": tmp_path / "attachments", "FOURBIS_CHATS_DIR": tmp_path / "chats",
        "FOURBIS_JSONL_DIR": tmp_path / "jsonl", "BOT_NOTIFY_URL": "http://127.0.0.1:9",
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setattr(server_common, "_get_api_key", lambda: "")
    monkeypatch.setattr(admin, "_cbm_list_projects", AsyncMock(return_value={"projects": []}))
    evidence_dir = Path(os.environ.get("RELAY_TEST_UI_OUTPUT", tmp_path / "output" / "playwright"))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    async with TestServer(server.create_app()) as http, pw.async_playwright() as browser_api:
        db = http.app[server.DB_KEY]
        # Sesión sintética del showcase: solo DB temporal, sin OAuth ni cuentas reales.
        session = "synthetic-ui-session"
        native_auth._finish_login(db, 999, "ui-admin@example.test", "UI Admin", True, session)
        for attr in ("_roles", "_disabled", "_names", "_project_grants"):
            monkeypatch.setattr(identity, attr, getattr(identity, attr).copy())
        identity.load_roles(await db.list_users())
        await db.upsert_project(dict(slug="issue-ui", name="Issue UI", repo_path=str(tmp_path)))
        # Solo una conversación vacía en DB temporal: no se aprovisiona un worktree
        # ni se persiste una issue que pudiera recoger un monitor del servidor.
        cid = await db.create_conversation(project_slug="issue-ui")
        issue = {"repo": "AuroraDemo/sample", "repo_id": 701, "number": 17, "node_id": "I_test_17",
                 "url": "https://github.com/AuroraDemo/sample/issues/17", "title": "Cambio compartido", "state": "OPEN"}
        task = {"id": cid, "mode": "write", "state": "ready", "publish_allowed": False,
                "allowed_actions": ["link_issue", "issue_sync"], "resolution_complete": False,
                "tracking": {"enabled": False}}
        sync = {"issue": issue, "enabled": False, "status": "disabled", "observed": {}, "events": [], "error": ""}
        saves, sync_posts = [], []
        list_reads = 0
        retry_started, release_retry = asyncio.Event(), asyncio.Event()
        browser = await browser_api.chromium.launch(channel="chrome", headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            await page.context.add_cookies([{"name": native_auth.SESSION_COOKIE, "value": session,
                "url": str(http.make_url("/")), "httpOnly": True, "sameSite": "Lax"}])
            errors, console_errors, expected_resource_errors = [], [], []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def capture_console(message):
                if message.type != "error":
                    return
                # Chrome informa HTTP 503 aun cuando la aplicación lo maneja.
                # Esos dos fallos de transporte son parte deliberada de esta QA.
                if message.text.startswith("Failed to load resource:") and "503" in message.text:
                    expected_resource_errors.append(message.text)
                else:
                    console_errors.append(message.text)

            page.on("console", capture_console)

            async def issues(route):
                nonlocal list_reads
                assert route.request.method == "GET"
                list_reads += 1
                if list_reads == 1:
                    await route.fulfill(status=503, content_type="application/json",
                                        body='{"error":"Lista temporalmente no disponible"}')
                    return
                await route.fulfill(content_type="application/json", body=json.dumps({"issues": [issue]}))

            async def save_issue(route):
                assert route.request.method == "POST"
                body = route.request.post_data_json
                saves.append(body)
                if len(saves) == 1:
                    await route.fulfill(status=503, content_type="application/json",
                                        body='{"error":"GitHub temporalmente no disponible"}')
                    return
                task["issue_link"] = dict(issue)
                task["resolution_complete"] = body["resolution_complete"]
                if body["resolution_complete"]:
                    task["resolution_actor"] = {"relay_actor": "owner", "github_id": 101, "github_login": "responsable"}
                    task["resolution_at"] = "2026-10-20T17:00:00+00:00"
                await route.fulfill(content_type="application/json", body=json.dumps(task))

            async def sync_state(route):
                if route.request.method == "POST":
                    body = route.request.post_data_json
                    sync_posts.append(body)
                    assert body["action"] in {"refresh", "retry"}
                    if body["action"] == "retry":
                        retry_started.set()
                        await release_retry.wait()
                else:
                    assert route.request.method == "GET"
                await route.fulfill(content_type="application/json", body=json.dumps(sync))

            await page.route("**/projects/issue-ui/issues", issues)
            await page.route(f"**/conversations/{cid}/issue", save_issue)
            await page.route(f"**/conversations/{cid}/issue-sync", sync_state)
            await page.route(f"**/conversations/{cid}/task", lambda route: route.fulfill(
                content_type="application/json", body=json.dumps(task)))
            await page.goto(str(http.make_url(f"/admin/?chat-window={cid}")) + "#/chat")
            panel = page.locator("#chat-task-panel")
            await pw.expect(panel).to_contain_text("Sin issue vinculada")
            await panel.locator('[data-issue-action="link"]').click()
            modal = page.locator("#issue-link-modal")
            input_issue = page.locator("#issue-link-input")
            complete = page.locator("#issue-resolution-complete")
            await pw.expect(complete).not_to_be_checked()
            await pw.expect(modal.locator("[data-issue-picker-status]")).to_contain_text("Lista temporalmente")
            await input_issue.fill("#17")
            await modal.locator("[data-issue-picker-retry]").click()
            await pw.expect(page.locator("#issue-link-input-options option")).to_have_count(1)
            await pw.expect(input_issue).to_have_value("#17")
            await modal.locator('[type="submit"]').click()
            await pw.expect(page.locator("#issue-link-modal-msg")).to_contain_text("Conservamos los datos")
            await pw.expect(input_issue).to_have_value("#17")
            await pw.expect(complete).not_to_be_checked()
            await pw.expect(input_issue).to_be_enabled()
            await page.screenshot(path=str(evidence_dir / "issue-link-503-1440.png"), animations="disabled")
            await modal.locator('[type="submit"]').click()
            await pw.expect(modal).not_to_be_visible()
            assert saves[:2] == [{"issue": 17, "resolution_complete": False}] * 2
            await pw.expect(panel.locator(f'a[href="{issue["url"]}"]')).to_be_visible()
            await pw.expect(panel).to_contain_text("Trabajo parcial")
            await pw.expect(panel.locator("[data-issue-sync] .badge")).to_have_text("No configurada")

            # El valor ofrecido en el datalist es la URL canónica. La declaración
            # se marca solo después de elegirla, y no se transfiere al editarla.
            await panel.locator('[data-issue-action="link"]').click()
            await pw.expect(complete).not_to_be_checked()
            await pw.expect(page.locator("#issue-link-input-options option")).to_have_count(1)
            listed_url = await page.locator("#issue-link-input-options option").get_attribute("value")
            assert listed_url == issue["url"]
            await complete.check()
            await input_issue.fill("17")
            await pw.expect(complete).not_to_be_checked()
            await input_issue.fill(listed_url)
            await complete.check()
            await modal.locator('[type="submit"]').click()
            await pw.expect(modal).not_to_be_visible()
            assert saves[-1] == {"issue": issue["url"], "resolution_complete": True}
            await pw.expect(panel).to_contain_text("Declarada como resolución completa")

            # Desactivar la política no oculta fallos previos ni incertidumbre.
            sync.update(status="error", error="Lectura bloqueada; requiere reintento",
                        events=[{"transition": "review", "state": "blocked", "read_attempts": 3,
                                 "attempts": 0, "error": "GitHub no respondió"}])
            await panel.locator('[data-issue-action="refresh"]').click()
            await pw.expect(panel.locator("[data-issue-sync] .badge")).to_have_text("Error")
            await pw.expect(panel.locator("[data-issue-sync]")).to_contain_text("Política desactivada")
            retry_button = panel.locator('[data-issue-action="retry"]')
            await pw.expect(retry_button).to_be_visible()
            await page.screenshot(path=str(evidence_dir / "issue-sync-disabled-error-1440.png"), animations="disabled")
            retries_before = sum(body["action"] == "retry" for body in sync_posts)
            await retry_button.click()
            await pw.expect(page.locator("#confirm-modal-body")).to_contain_text("puede aplicar el mapeo")
            await page.locator("#confirm-modal-cancel").click()
            await pw.expect(retry_button).to_be_enabled()
            assert sum(body["action"] == "retry" for body in sync_posts) == retries_before

            sync.update(status="uncertain", error="Resultado remoto aún no confirmado",
                        events=[{"transition": "review", "state": "uncertain", "attempts": 1,
                                 "error": "Reconciliar antes de repetir"}])
            await retry_button.click()
            await page.locator("#confirm-modal-ok").click()
            await asyncio.wait_for(retry_started.wait(), timeout=5)
            await pw.expect(retry_button).to_be_disabled()
            await pw.expect(panel.locator('[data-issue-action="refresh"]')).to_be_disabled()
            await retry_button.evaluate("button => button.click()")
            assert sum(body["action"] == "retry" for body in sync_posts) == 1
            async with page.expect_response(lambda response: response.request.method == "POST"
                                            and response.url.endswith(f"/conversations/{cid}/issue-sync")):
                release_retry.set()
            await pw.expect(panel.locator("[data-issue-sync] .badge")).to_have_text("Resultado incierto")
            await pw.expect(panel.locator("[data-issue-sync]")).to_contain_text("Política desactivada")
            await pw.expect(panel.locator('[data-issue-action="retry"]')).to_have_count(0)
            await page.screenshot(path=str(evidence_dir / "issue-sync-disabled-uncertain-1440.png"), animations="disabled")

            sync.update(enabled=True, status="synced", error="", observed={
                "issue": {"state": "OPEN"}, "observed_at": "2026-10-20T17:01:00+00:00",
                "projects": [{"project_id": "PVT_test_project", "field_id": "PVTSSF_test_status",
                              "item_id": "PVTI_test_item", "option_id": "option_review", "value": "In review"}]},
                events=[{"transition": "review", "state": "applied", "created_at": "2026-10-20T17:01:00+00:00"}])
            await panel.locator('[data-issue-action="refresh"]').click()
            await pw.expect(panel.locator("[data-issue-sync] .badge")).to_have_text("Sincronizada")
            await pw.expect(panel.locator("[data-issue-sync]")).to_contain_text("PVT_test_project")
            await pw.expect(panel.locator("[data-issue-sync]")).to_contain_text("In review")
            await pw.expect(panel.locator("[data-issue-sync]")).to_contain_text("Estado observado en GitHub: OPEN")
            await pw.expect(page.locator("#toast-root > *")).to_have_count(0, timeout=15000)
            for width, height in [(1440, 1000), (390, 844)]:
                await page.set_viewport_size({"width": width, "height": height})
                await page.screenshot(path=str(evidence_dir / f"issue-sync-{width}.png"), animations="disabled")
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                assert await panel.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
            await panel.locator('[data-issue-action="link"]').click()
            await pw.expect(complete).to_be_checked()
            await page.screenshot(path=str(evidence_dir / "issue-link-390.png"), animations="disabled")
            assert await modal.locator(".modal-card").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
            await page.keyboard.press("Escape")
            await pw.expect(modal).not_to_be_visible()
            assert not errors, errors
            assert not console_errors, console_errors
            assert len(expected_resource_errors) <= 2, expected_resource_errors
            assert (await db.get_conversation_task(cid)) == {}, "la QA no deja un vínculo real para el monitor"
        finally:
            release_retry.set()
            await browser.close()
