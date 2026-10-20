"""UI real aislada: registro externo, interrupciones y evidencia por SHA sin GitHub real."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestServer

from relay import admin, admin_projects, server, server_common, identity, native_auth


@pytest.mark.skipif(os.environ.get("RELAY_TEST_UI") != "1", reason="integración UI opt-in con Chrome")
async def test_external_pr_registration_and_evidence_recover_in_real_browser(tmp_path, monkeypatch):
    pw = pytest.importorskip("playwright.async_api")
    for key, value in {"FOURBIS_DB_PATH": tmp_path / "relay.db", "STATE_DIR": tmp_path / "state",
                       "FOURBIS_ATTACHMENTS_DIR": tmp_path / "attachments",
                       "FOURBIS_CHATS_DIR": tmp_path / "chats", "FOURBIS_JSONL_DIR": tmp_path / "jsonl",
                       "BOT_NOTIFY_URL": "http://127.0.0.1:9"}.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setattr(server_common, "_get_api_key", lambda: "")
    monkeypatch.setattr(admin, "_cbm_list_projects", AsyncMock(return_value={"projects": []}))
    monkeypatch.setattr(admin_projects, "_has_git", lambda path: Path(path) == tmp_path)
    monkeypatch.setattr(admin_projects, "_git_remote_url", lambda path: "https://github.com/AuroraDemo/sample.git"
                        if Path(path) == tmp_path else None)
    evidence_dir = Path(os.environ.get("RELAY_TEST_UI_OUTPUT", tmp_path / "screenshots"))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    async with TestServer(server.create_app()) as http, pw.async_playwright() as browser_api:
        db = http.app[server.DB_KEY]
        # Sesión sintética del showcase: solo DB temporal, sin OAuth ni cuentas reales.
        session = "synthetic-ui-session"
        native_auth._finish_login(db, 999, "ui-admin@example.test", "UI Admin", True, session)
        for attr in ("_roles", "_disabled", "_names", "_project_grants"):
            monkeypatch.setattr(identity, attr, getattr(identity, attr).copy())
        identity.load_roles(await db.list_users())
        await db.upsert_project(dict(slug="external-ui", name="External UI", repo_path=str(tmp_path)))
        head, new_head, base = "a" * 40, "b" * 40, "c" * 40
        actor = {"relay_actor": "owner", "github_id": 101, "github_login": "responsable"}
        task = {"id": "", "mode": "external_pr", "state": "registered", "risk": "low",
                "actor_label": "Cualquier bot", "responsible": actor, "evidence": [], "history": [],
                "allowed_actions": ["external_refresh", "external_evidence"],
                "external_pr": {"repo": "AuroraDemo/sample", "number": 123,
                    "url": "https://github.com/AuroraDemo/sample/pull/123", "title": "Cambio externo",
                    "state": "OPEN", "draft": False, "head_sha": head, "base_sha": base,
                    "observed_at": "2026-10-20T15:00:00+00:00",
                    "author": {"id": 102, "login": "autor", "type": "User"},
                    "checks": [{"name": "tests", "head_sha": head, "status": "completed",
                                "conclusion": "success", "html_url": "https://github.com/AuroraDemo/sample/actions/runs/12"}],
                    "reviews": []},
                "assessment": {"state": "missing_evidence", "reasons": ["Falta evidencia para este SHA"],
                    "policy": {"required_checks": ["tests"], "require_declaration": True}}}
        browser = await browser_api.chromium.launch(channel="chrome", headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            await page.context.add_cookies([{"name": native_auth.SESSION_COOKIE, "value": session,
                "url": str(http.make_url("/")), "httpOnly": True, "sameSite": "Lax"}])
            errors, console_errors, registrations, evidence_posts = [], [], [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            registration_started, release_registration = asyncio.Event(), asyncio.Event()

            async def registration(route):
                registrations.append(route.request.post_data_json)
                if len(registrations) == 1:
                    await route.fulfill(status=503, content_type="application/json",
                                        body='{"error":"GitHub temporalmente no disponible"}')
                    return
                if len(registrations) == 2:
                    registration_started.set()
                    await release_registration.wait()
                if not task["id"]:
                    task["id"] = await db.create_conversation(project_slug="external-ui")
                    await db.update_conversation_task(task["id"], **{k: v for k, v in task.items() if k != "id"})
                    await db.set_conversation_pr(task["id"], task["external_pr"]["url"])
                await route.fulfill(content_type="application/json", body=json.dumps(task))

            await page.route("**/projects/external-ui/external-pr", registration)
            await page.route("**/projects/external-ui/issues", lambda route: route.fulfill(
                content_type="application/json", body='{"issues":[]}'))
            await page.route("**/conversations/*/task", lambda route: route.fulfill(
                content_type="application/json", body=json.dumps(task)))
            refresh_count = 0

            async def mutate(route):
                nonlocal refresh_count
                body = route.request.post_data_json
                if body["action"] == "evidence":
                    evidence_posts.append(body)
                    if len(evidence_posts) == 1:
                        await route.fulfill(status=503, content_type="application/json",
                                            body='{"error":"prueba de reintento"}')
                        return
                    task["evidence"].append({**body, "actor": actor, "at": "2026-10-20T15:01:00+00:00"})
                    task["assessment"]["state"] = "review_pending"
                    task["assessment"]["reasons"] = ["Falta aprobación humana independiente"]
                else:
                    refresh_count += 1
                    if refresh_count == 1:
                        await route.fulfill(status=503, content_type="application/json",
                                            body='{"error":"GitHub no responde"}')
                        return
                    task["history"].append({"kind": "github_snapshot", "at": "2026-10-20T15:02:00+00:00",
                                            "actor": actor, "snapshot": json.loads(json.dumps(task["external_pr"]))})
                    task["external_pr"]["head_sha"] = new_head
                    task["external_pr"]["checks"][0]["head_sha"] = new_head
                    task["assessment"]["state"] = "missing_evidence"
                    task["assessment"]["reasons"] = ["Falta evidencia para el head nuevo"]
                await route.fulfill(content_type="application/json", body=json.dumps(task))

            await page.route("**/conversations/*/external-pr", mutate)
            await page.goto(str(http.make_url("/admin")) + "#/chat")
            await page.wait_for_load_state("networkidle")
            if not await page.locator("#chat-external-pr").is_visible():
                await page.locator("button.chat-drawer-btn:visible").first.click()
            await page.locator("#chat-external-pr").click()
            await page.locator("#external-url").fill(task["external_pr"]["url"])
            await page.locator("#external-title").fill("Cambio externo")
            await page.locator("#external-risk").select_option("low")
            await page.locator("#external-actor").fill("Cualquier bot")
            await page.locator('#external-register-form [type="submit"]').click()
            await pw.expect(page.locator("#external-pr-modal-msg")).to_contain_text("Conservamos los datos")
            await pw.expect(page.locator("#external-url")).to_have_value(task["external_pr"]["url"])
            await pw.expect(page.locator("#external-title")).to_have_value("Cambio externo")
            await page.screenshot(path=str(evidence_dir / "external-pr-registration-error.png"))

            # El resultado de una operación pendiente no reabre ni navega después de Esc.
            await page.locator('#external-register-form [type="submit"]').click()
            await registration_started.wait()
            await page.keyboard.press("Escape")
            await pw.expect(page.locator("#external-pr-modal")).not_to_have_class("modal open")
            async with page.expect_response(lambda response: response.request.method == "POST"
                                            and "/projects/external-ui/external-pr" in response.url):
                release_registration.set()
            await pw.expect(page.locator("iframe.workspace-conversation-frame")).to_have_count(0)
            await page.locator("#chat-external-pr").click()
            await page.locator("#external-url").fill(task["external_pr"]["url"])
            await page.locator("#external-title").fill("Cambio externo")
            await page.locator("#external-risk").select_option("low")
            await page.locator('#external-register-form [type="submit"]').click()
            await pw.expect(page.locator("iframe.workspace-conversation-frame")).to_have_count(1)
            assert registrations[0] == registrations[1]
            assert len(await db.list_conversations(project_slug="external-ui")) == 1

            # Misma vista que el iframe del workspace, abierta directamente para QA móvil.
            await page.goto(str(http.make_url(f"/admin/?chat-window={task['id']}")) + "#/chat")
            panel = page.locator("#chat-task-panel")
            await pw.expect(panel).to_contain_text("Falta evidencia")
            await pw.expect(page.locator("#chat-input-bar")).to_be_hidden()
            await pw.expect(page.locator("#chat-panel-grafo")).to_be_hidden()
            await pw.expect(panel.locator("[data-task-action]")).to_have_count(0)
            await panel.locator('[data-external-action="evidence"]').click()
            await page.locator("#external-summary").fill("Suite local: 12 pruebas correctas.")
            await page.locator('#external-evidence-form [type="submit"]').click()
            await pw.expect(page.locator("#external-pr-modal-msg")).to_contain_text("texto se conserva")
            await pw.expect(page.locator("#external-summary")).to_have_value("Suite local: 12 pruebas correctas.")
            await page.locator('#external-evidence-form [type="submit"]').click()
            await pw.expect(panel).to_contain_text("Revisión pendiente")
            assert evidence_posts[0]["request_id"] == evidence_posts[1]["request_id"]
            assert evidence_posts[1]["head_sha"] == head
            evidence_section = panel.locator('details[data-external-section="evidence"]')
            if not await evidence_section.evaluate("el => el.open"):
                await evidence_section.locator("summary").first.click()
            await pw.expect(evidence_section.get_by_text("Declarada · SHA actual", exact=True)).to_be_visible()
            await panel.locator('[data-external-action="refresh"]').click()
            await pw.expect(page.locator("#toast-root")).to_contain_text("No se pudo verificar")
            await panel.locator('[data-external-action="refresh"]').click()
            await pw.expect(panel).to_contain_text("Falta evidencia para el head nuevo")
            if not await evidence_section.evaluate("el => el.open"):
                await evidence_section.locator("summary").first.click()
            await pw.expect(evidence_section.get_by_text("Histórica · otro SHA", exact=True)).to_be_visible()
            history_section = panel.locator('details[data-external-section="history"]')
            if not await history_section.evaluate("el => el.open"):
                await history_section.locator("summary").first.click()
            await pw.expect(history_section).to_contain_text("Lectura GitHub")
            await pw.expect(page.locator("#toast-root > *")).to_have_count(0, timeout=15000)

            for width, height in [(1440, 1000), (390, 844)]:
                await page.set_viewport_size({"width": width, "height": height})
                await page.screenshot(path=str(evidence_dir / f"external-pr-{width}.png"))
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                assert await panel.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
            assert not errors, errors
            assert all("503" in error for error in console_errors), console_errors
        finally:
            release_registration.set()
            await browser.close()
