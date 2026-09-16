"""QA opt-in de métricas y subdivisiones con Chrome y datos ficticios."""
import os
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from aiohttp.test_utils import TestServer

from relay import admin, cbm_watcher, config, server


@pytest.mark.skipif(os.environ.get("RELAY_TEST_UI") != "1", reason="integración UI opt-in con Chrome")
async def test_workflow_metrics_and_replaced_parent_in_real_browser(tmp_path, monkeypatch):
    pw = pytest.importorskip("playwright.async_api")
    for key, value in {
        "FOURBIS_DB_PATH": tmp_path / "relay.db", "STATE_DIR": tmp_path / "state",
        "FOURBIS_CHATS_DIR": tmp_path / "chats", "FOURBIS_JSONL_DIR": tmp_path / "jsonl",
        "FOURBIS_ATTACHMENTS_DIR": tmp_path / "attachments",
        "FOURBIS_LOG_DIR": tmp_path / "logs", "BOT_NOTIFY_URL": "http://127.0.0.1:9",
        "CBM_AUTO_WATCH": "0", "FOURBIS_MCP_HEALTH_PROBE": "0",
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setattr(server, "_get_api_key", lambda: "")
    monkeypatch.setattr(admin, "_cbm_list_projects", AsyncMock(return_value={"projects": []}))
    for name in ("_autoclose_sweeper", "_export_retry_loop", "_probe_external_mcps_health", "_warm_cbm_session"):
        monkeypatch.setattr(server, name, AsyncMock())
    monkeypatch.setattr(cbm_watcher, "run", AsyncMock())
    evidence = Path(os.environ.get("RELAY_TEST_UI_OUTPUT", tmp_path / "screenshots"))
    evidence.mkdir(parents=True, exist_ok=True)

    async with TestServer(server.create_app()) as http, pw.async_playwright() as browser_api:
        db = http.app[server.DB_KEY]
        assert db.path == tmp_path / "relay.db"
        await db.upsert_project(dict(slug="workflow-qa", name="Workflow QA", repo_path=str(tmp_path)))
        split_id = None
        for status, phase in (("ok", "writing"), ("error", "error"), ("running", None),
                              ("cancelled", "cancelled"), ("error", "budget_split")):
            cid = await db.create_chat(project_slug="workflow-qa", source="test", author="QA", target="workflow-qa")
            if status != "running":
                await db.finish_chat(cid, status=status, phase_at_end=phase,
                                     error="fallo ficticio" if phase == "error" else None)
            if phase == "budget_split":
                split_id = cid
        conv = await db.create_conversation(project_slug="workflow-qa")
        await db.create_task_graph("qa-split", "Auditoría con subdivisión", conversation_id=conv,
                                   project_slug="workflow-qa", tareas=[{"id": "qa-parent", "titulo": "Tarea original"}])
        await db.update_task("qa-parent", estado="fallado", chat_id=split_id,
                             error="subdividido en 2 subtareas: qa-child1, qa-child2")
        await db.add_tasks_to_graph("qa-split", [
            {"id": "qa-child1", "titulo": "Primera parte"},
            {"id": "qa-child2", "titulo": "Segunda parte", "deps": ["qa-child1"]},
        ], reemplaza="qa-parent")
        for tid in ("qa-child1", "qa-child2"):
            await db.update_task(tid, estado="hecho", resultado="Comprobado con datos ficticios")
        await db.set_task_graph_state("qa-split", "fallado")  # cierre histórico previo al arreglo

        browser = await browser_api.chromium.launch(channel="chrome", headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors, console_errors, external = [], [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            origin = urlsplit(str(http.make_url("/"))).netloc

            async def only_fixture(route):
                if urlsplit(route.request.url).netloc != origin:
                    external.append(route.request.url)
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", only_fixture)
            await page.goto(str(http.make_url("/admin/")) + "#/metrics")
            await page.wait_for_load_state("networkidle")

            def stat(label):
                return page.locator("#metrics-kpi .stat").filter(
                    has=page.locator(".stat-k", has_text=label)).locator(".stat-v")

            for label, count in (("Runs", "5"), ("Intentos con error", "1"),
                                 ("En curso", "1"), ("Cancelados", "1"), ("Subdivididos", "1")):
                await pw.expect(stat(label)).to_have_text(count)
            # Sólo ok + error alimentan las dos barras: vivo/cancelado/split no son ok.
            segments = page.locator("#metrics-trend .chart-seg")
            await pw.expect(segments).to_have_count(2)
            assert await segments.evaluate_all("els => els.map(e => e.style.flexGrow)") == ["1", "1"]
            await page.locator("#metrics-trend .chart-col").last.hover()
            await pw.expect(page.locator("#metrics-trend .chart-tooltip")).to_contain_text(
                "en curso 1 · cancelados 1 · subdivididos 1")
            await page.screenshot(path=str(evidence / "workflow-metrics-all.png"), full_page=True)

            async with page.expect_response(lambda response: "/metrics/summary?" in response.url and "status=split" in response.url) as selected:
                await page.locator("#metrics-state").select_option("split")
            filtered = await (await selected.value).json()
            assert {k: filtered["totals"][k] for k in ("runs", "ok", "errors", "running", "cancelled", "split")} == {
                "runs": 1, "ok": 0, "errors": 0, "running": 0, "cancelled": 0, "split": 1}
            await pw.expect(stat("Runs")).to_have_text("1")
            await pw.expect(stat("Intentos con error")).to_have_text("0")
            await pw.expect(stat("Subdivididos")).to_have_text("1")
            await pw.expect(page.locator("#metrics-status")).to_contain_text("1 filtro activo")
            await pw.expect(page.locator("#metrics-trend")).not_to_contain_text("sin runs en el rango")
            await page.screenshot(path=str(evidence / "workflow-metrics-split.png"), full_page=True)

            await page.locator('.tab[data-tab="chat"]').click()
            await page.locator(f'.chat-conv-item[data-id="{conv}"]').click()
            await pw.expect(page.locator("#chat-grafo")).to_be_visible()
            await pw.expect(page.locator("#chat-grafo-estado")).to_have_text("hecho")
            await pw.expect(page.locator("#chat-grafo-conteo")).to_have_text("2/2 hechas · 1 subdividida")
            parent = page.locator('.gnodo[data-id="qa-parent"]')
            await pw.expect(parent).to_contain_text("subdividida")
            assert "fallado" not in (await parent.get_attribute("class")).split()
            assert await page.locator("#chat-grafo-barra-mal").evaluate("el => el.style.width") == "0%"
            await parent.click()
            await pw.expect(page.locator("#chat-grafo-detalle")).to_contain_text("Tarea conservada como historial")
            await pw.expect(page.locator("#chat-grafo-detalle")).to_contain_text("subdividido en 2 subtareas")
            await pw.expect(page.locator("#chat-grafo-detalle .gerror")).to_have_count(0)
            await page.screenshot(path=str(evidence / "workflow-graph.png"))
            await page.locator("#chat-grafo-tab-resumen").click()
            row = page.locator('.gres-item[data-id="qa-parent"]')
            await pw.expect(row).to_contain_text("subdividida")
            assert "fallado" not in (await row.get_attribute("class")).split()
            await page.screenshot(path=str(evidence / "workflow-summary.png"))
            assert not errors, errors
            assert not console_errors, console_errors
            assert not external, external
        finally:
            await browser.close()
