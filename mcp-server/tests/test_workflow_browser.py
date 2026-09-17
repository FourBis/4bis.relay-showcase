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

        browser_options = {"headless": True}
        browser_executable = os.environ.get("RELAY_TEST_BROWSER_EXECUTABLE")
        if browser_executable:
            browser_options["executable_path"] = browser_executable
        else:
            browser_options["channel"] = "chrome"
        browser = await browser_api.chromium.launch(**browser_options)
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

            # The secondary KPIs are rendered in a collapsed details panel.
            secondary_details = page.locator("details.workspace-filters").filter(
                has=page.locator("#metrics-secondary"))
            await pw.expect(secondary_details).to_have_count(1)
            await secondary_details.locator("summary").click()
            await pw.expect(page.locator("#metrics-secondary")).to_be_visible()

            def stat(label):
                return page.locator("#metrics-kpi, #metrics-secondary").locator(".stat").filter(
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

            # Keep the filter lookup after the details panel is open: the
            # state change must exercise the same visible metrics workspace.
            await page.locator("details.workspace-filters").filter(
                has=page.locator("#metrics-state")).locator("summary").click()
            metrics_state = page.locator("#metrics-state")
            await pw.expect(metrics_state).to_be_visible()
            async with page.expect_response(lambda response: "/metrics/summary?" in response.url and "status=split" in response.url) as selected:
                await metrics_state.select_option("split")
            filtered = await (await selected.value).json()
            assert {k: filtered["totals"][k] for k in ("runs", "ok", "errors", "running", "cancelled", "split")} == {
                "runs": 1, "ok": 0, "errors": 0, "running": 0, "cancelled": 0, "split": 1}
            await pw.expect(stat("Runs")).to_have_text("1")
            await pw.expect(stat("Intentos con error")).to_have_text("0")
            await pw.expect(stat("Subdivididos")).to_have_text("1")
            await pw.expect(page.locator("#metrics-status")).to_contain_text("1 filtro activo")
            await pw.expect(page.locator("#metrics-trend")).not_to_contain_text("sin runs en el rango")
            await page.screenshot(path=str(evidence / "workflow-metrics-split.png"), full_page=True)

            await page.locator("#workspace-launcher").click()
            launcher = page.locator("#workspace-launcher-dialog")
            await pw.expect(launcher).to_be_visible()
            await launcher.locator('.tab[data-tab="chat"]').click()
            await page.locator('button.chat-drawer-btn:visible').first.click()
            await page.locator(f'.chat-conv-item[data-id="{conv}"]').click()
            await page.locator("#chat-panel-grafo").click()
            await pw.expect(page.locator("#chat-grafo")).to_be_visible()
            await pw.expect(page.locator("#chat-grafo-estado")).to_have_text("hecho")
            await pw.expect(page.locator("#chat-grafo-conteo")).to_have_text("2/2 hechas · 1 subdividida")

            async def graph_layout():
                return await page.locator("#chat-grafo").evaluate("""grafo => {
                    const rect = el => {
                        const box = el.getBoundingClientRect();
                        return {left: box.left, right: box.right, top: box.top,
                                bottom: box.bottom, width: box.width, height: box.height};
                    };
                    const chat = grafo.parentElement.querySelector('.chat-main');
                    return {chat: rect(chat), grafo: rect(grafo),
                            workspace: rect(grafo.parentElement),
                            overflow: document.documentElement.scrollWidth - innerWidth};
                }""")

            def assert_graph_layout(layout):
                assert layout["chat"]["right"] <= layout["grafo"]["left"] + 1, layout
                assert layout["chat"]["width"] >= 319, layout
                assert layout["overflow"] <= 1, layout

            normal_layout = await graph_layout()
            assert_graph_layout(normal_layout)
            normal_graph_width = normal_layout["grafo"]["width"]
            await page.locator("#chat-grafo-ancho").click()
            await pw.expect(page.locator("#chat-grafo")).to_have_class("chat-grafo ancho")
            await page.wait_for_timeout(220)
            expanded_layout = await graph_layout()
            assert_graph_layout(expanded_layout)
            assert expanded_layout["grafo"]["width"] > normal_graph_width + 5, expanded_layout
            await page.locator("#chat-grafo-ancho").click()
            await pw.expect(page.locator("#chat-grafo")).to_have_class("chat-grafo")
            await page.wait_for_timeout(220)
            restored_layout = await graph_layout()
            assert_graph_layout(restored_layout)
            assert abs(restored_layout["grafo"]["width"] - normal_graph_width) <= 1.5, restored_layout

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

            # Shrink the desktop workspace to exercise the 700px container query.
            window_head = page.locator(".workspace-window[data-module='chat'] .workspace-window-head")
            await pw.expect(window_head).to_have_count(1)
            await window_head.focus()
            for _ in range(11):
                await window_head.press("Shift+ArrowLeft")
            await page.wait_for_timeout(100)
            container_width = await page.locator(".chat-workspace").evaluate(
                "el => el.getBoundingClientRect().width")
            assert container_width <= 700, container_width
            await pw.expect(page.locator(".chat-workspace .chat-main")).to_be_hidden()
            await pw.expect(page.locator("#chat-grafo")).to_be_visible()
            assert (await graph_layout())["overflow"] <= 1

            chat_main = page.locator(".chat-workspace .chat-main")
            await page.locator("#chat-grafo-cerrar").click()
            await pw.expect(page.locator("#chat-grafo")).to_be_hidden()
            await pw.expect(chat_main).to_be_visible()
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

            await page.set_viewport_size({"width": 390, "height": 844})
            await page.wait_for_timeout(100)
            await pw.expect(page.locator("#chat-grafo")).to_be_hidden()
            await pw.expect(chat_main).to_be_visible()
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            graph_open = page.locator("#chat-panel-grafo")
            graph_close = page.locator("#chat-grafo-cerrar")
            await graph_open.click()
            await pw.expect(page.locator("#chat-grafo")).to_be_visible()
            await pw.expect(chat_main).to_be_hidden()
            await pw.expect(graph_close).to_be_focused()
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            await graph_close.press("Enter")
            await pw.expect(page.locator("#chat-grafo")).to_be_hidden()
            await pw.expect(chat_main).to_be_visible()
            await pw.expect(graph_open).to_be_focused()
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            await graph_open.press("Enter")
            await pw.expect(page.locator("#chat-grafo")).to_be_visible()
            await pw.expect(chat_main).to_be_hidden()
            await pw.expect(graph_close).to_be_focused()
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            assert not errors, errors
            assert not console_errors, console_errors
            assert not external, external
        finally:
            await browser.close()
