"""RELAY_TEST_UI=1: workspace visual con Chrome y SQLite temporal.

El test es opt-in porque requiere un Chrome instalado. Todos los datos se
siembran en el TestServer y el objeto del chat solo guarda una referencia en
localStorage, nunca el texto de la respuesta.
"""
import json
import os
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer
from unittest.mock import AsyncMock

from relay import admin, server


@pytest.mark.skipif(
    os.environ.get("RELAY_TEST_UI") != "1",
    reason="integración UI opt-in con Chrome",
)
async def test_workspace_modules_windows_objects_and_responsive(tmp_path, monkeypatch):
    pw = pytest.importorskip("playwright.async_api")
    for key, value in {
        "FOURBIS_DB_PATH": tmp_path / "relay.db",
        "STATE_DIR": tmp_path / "state",
        "FOURBIS_ATTACHMENTS_DIR": tmp_path / "attachments",
        "FOURBIS_CHATS_DIR": tmp_path / "chats",
        "FOURBIS_JSONL_DIR": tmp_path / "jsonl",
        "BOT_NOTIFY_URL": "http://127.0.0.1:9",
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setattr(server, "_get_api_key", lambda: "")
    monkeypatch.setattr(
        admin, "_cbm_list_projects", AsyncMock(return_value={"projects": []}))

    async with TestServer(server.create_app()) as http, pw.async_playwright() as browser_api:
        db = http.app[server.DB_KEY]
        await db.upsert_project({
            "slug": "workspace-test", "name": "Workspace test",
            "repo_path": str(tmp_path),
        })
        conv = await db.create_conversation(project_slug="workspace-test")
        secret = "SENSITIVE_RESPONSE_MUST_NOT_REACH_STORAGE"
        answer = (
            "Resultado verificable.\n\n"
            "| Nombre | Estado |\n"
            "| --- | --- |\n"
            "| Ana | Activa |\n"
            "| Beto | Pausado |\n\n"
            f"{secret}"
        )
        chat_id = await db.create_chat(
            project_slug="workspace-test", source="test", author="tester",
            target="workspace-test", conversation_id=conv,
        )
        md_path = Path(tmp_path) / "chats" / "workspace-test" / f"{chat_id}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(
            "---\n"
            f"id: {chat_id}\n"
            "target: workspace-test\n"
            "status: ok\n"
            "---\n\n"
            "## Usuario\n\nmuéstrame el resumen\n\n"
            f"## Respuesta\n\n{answer}\n",
            encoding="utf-8",
        )
        await db.finish_chat(chat_id, status="ok", md_path=str(md_path))

        browser = await browser_api.chromium.launch(channel="chrome", headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            page_errors, console_errors = [], []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text)
                if msg.type == "error" else None,
            )
            base = str(http.make_url("/admin")) + "#/chat"
            await page.goto(base)
            await page.wait_for_load_state("networkidle")
            expect = pw.expect
            chat_window = page.locator('article.workspace-window[data-module="chat"]')
            await expect(chat_window).to_be_visible(timeout=10_000)
            await expect(page.locator("#chat-conv-list")).to_contain_text(
                "workspace-test", timeout=10_000)
            await page.locator("button.chat-drawer-btn:visible").first.click()
            await expect(page.locator(".chat-sidebar.mobile-open")).to_be_visible()
            await page.locator(f'.chat-conv-item[data-id="{conv}"]').click()
            await expect(page.locator("#chat-panel-messages")).to_contain_text(
                "Resultado verificable", timeout=10_000)

            async def open_tool(name):
                await page.locator("#workspace-launcher").click()
                dialog = page.locator("#workspace-launcher-dialog")
                await expect(dialog).to_be_visible(timeout=5_000)
                await dialog.locator(f'button.tab[data-tab="{name}"]').click()
                window = page.locator(
                    f'article.workspace-window[data-module="{name}"]')
                await expect(window).to_be_visible(timeout=10_000)
                await expect(chat_window).to_be_visible(timeout=5_000)
                return window

            # El launcher abre módulos sin desmontar el chat en desktop.
            module_windows = {}
            for name in ("projects", "running", "config", "mcp"):
                module_windows[name] = await open_tool(name)
            assert await page.locator(
                'article.workspace-window[data-module="chat"]:visible').count() == 1

            # Minimizar, restaurar desde dock, cerrar y reabrir desde launcher.
            running = module_windows["running"]
            await page.locator(
                '#workspace-items button[data-window-id="running"]').click()
            await running.locator('[data-window-action="minimize"]').click()
            await expect(running).to_be_hidden()
            await page.locator(
                '#workspace-items button[data-window-id="running"]').click()
            await expect(running).to_be_visible()
            mcp = module_windows["mcp"]
            await page.locator(
                '#workspace-items button[data-window-id="mcp"]').click()
            await mcp.locator('[data-window-action="close"]').click()
            await expect(mcp).to_be_hidden()
            module_windows["mcp"] = await open_tool("mcp")

            # Arrastre de cabecera y resize de esquina con mouse.
            await page.locator(
                '#workspace-items button[data-window-id="chat"]').click()
            header = chat_window.locator(".workspace-window-head")
            before = await chat_window.bounding_box()
            hb = await header.bounding_box()
            assert before and hb
            await page.mouse.move(hb["x"] + 180, hb["y"] + 14)
            await page.mouse.down()
            await page.mouse.move(hb["x"] + 230, hb["y"] + 44, steps=3)
            await page.mouse.up()
            moved = await chat_window.bounding_box()
            assert moved and (moved["x"] != before["x"] or moved["y"] != before["y"])

            resize_window = module_windows["mcp"]
            await page.locator(
                '#workspace-items button[data-window-id="mcp"]').click()
            resize_grip = resize_window.locator(".workspace-resize")
            resized_before = await resize_window.bounding_box()
            gb = await resize_grip.bounding_box()
            assert resized_before and gb
            await page.mouse.move(
                gb["x"] + gb["width"] / 2,
                gb["y"] + gb["height"] / 2,
            )
            await page.mouse.down()
            await page.mouse.move(
                gb["x"] + gb["width"] / 2 + 80,
                gb["y"] + gb["height"] / 2 + 55,
                steps=3,
            )
            await page.mouse.up()
            resized = await resize_window.bounding_box()
            assert resized and resized["width"] > resized_before["width"]

            # Movimiento y resize accesibles por teclado.
            await header.focus()
            keyboard_before = await chat_window.bounding_box()
            await page.keyboard.press("ArrowRight")
            await page.keyboard.press("Shift+ArrowRight")
            keyboard_after = await chat_window.bounding_box()
            assert keyboard_before and keyboard_after
            assert keyboard_after["x"] >= keyboard_before["x"] + 20
            assert keyboard_after["width"] >= keyboard_before["width"] + 20

            # Persistencia de geometría después de una recarga.
            saved_rect = await chat_window.bounding_box()
            await page.wait_for_timeout(250)
            storage_before = await page.evaluate(
                "() => localStorage.getItem('4bis.workspace.v1')")
            assert storage_before
            await page.reload()
            await page.wait_for_load_state("networkidle")
            chat_window = page.locator('article.workspace-window[data-module="chat"]')
            await expect(chat_window).to_be_visible(timeout=10_000)
            restored_rect = await chat_window.bounding_box()
            assert saved_rect and restored_rect
            for key in ("x", "y", "width", "height"):
                assert abs(restored_rect[key] - saved_rect[key]) <= 3, (key, saved_rect, restored_rect)
            # La recarga conserva ventanas y geometría, pero el hilo activo
            # se vuelve a elegir desde el drawer para cargar sus mensajes.
            await page.locator("button.chat-drawer-btn:visible").first.click()
            await page.locator(f'.chat-conv-item[data-id="{conv}"]').click()
            await expect(page.locator("#chat-panel-messages")).to_contain_text(
                "Resultado verificable", timeout=10_000)

            # Mosaico: todas las ventanas abiertas ocupan rectángulos distintos.
            await page.locator("#workspace-arrange").click()
            await page.wait_for_timeout(150)
            boxes = await page.locator(
                "#workspace-windows > article.workspace-window:not([hidden])"
            ).evaluate_all(
                "els => els.map(e => { const r=e.getBoundingClientRect(); "
                "return {x:r.x,y:r.y,w:r.width,h:r.height}; })"
            )
            assert len(boxes) >= 5
            for i, left in enumerate(boxes):
                for right in boxes[i + 1:]:
                    overlap = (
                        left["x"] < right["x"] + right["w"]
                        and left["x"] + left["w"] > right["x"]
                        and left["y"] < right["y"] + right["h"]
                        and left["y"] + left["h"] > right["y"]
                    )
                    assert not overlap, (left, right)

            # Abrir la respuesta completa, separar la tabla, filtrarla y
            # volver al chat sin destruir ninguno de los dos objetos.
            await page.locator(
                '#workspace-items button[data-window-id="chat"]').click()
            grafo = page.locator("#chat-grafo")
            if await grafo.is_visible():
                await grafo.locator("#chat-grafo-cerrar").click()
            await page.locator('button.chat-open-object').click()
            response_window = page.locator(
                'article.workspace-window[data-window-id*="response:0"]')
            await expect(response_window).to_be_visible(timeout=10_000)
            await expect(response_window).to_contain_text("Resultado verificable")
            await response_window.get_by_role(
                "button", name="Separar tabla 1", exact=True).click()
            table_window = page.locator(
                'article.workspace-window[data-window-id*="table:0"]')
            await expect(table_window).to_be_visible(timeout=10_000)
            await expect(table_window.locator("table")).to_be_visible()
            filter_input = table_window.locator('input[aria-label="Filtrar filas"]')
            await filter_input.fill("Ana")
            rows = table_window.locator("tbody tr")
            await expect(rows.nth(0)).to_be_visible()
            await expect(rows.nth(1)).to_be_hidden()
            await expect(table_window.get_by_role("button", name="Copiar tabla")).to_be_visible()
            source = await table_window.locator(".workspace-window-kind").text_content()
            assert "Conversación" in (source or "")
            await page.wait_for_timeout(250)
            storage = await page.evaluate(
                "() => localStorage.getItem('4bis.workspace.v1')")
            assert storage and secret not in storage
            saved = json.loads(storage)
            objects = [w for w in saved["windows"] if not w.get("module")]
            assert objects and objects[-1]["restore"]["conversationId"] == conv
            assert secret not in json.dumps(objects)

            # La recarga rehidrata ambos objetos sólo con su referencia segura.
            await page.reload()
            await page.wait_for_load_state("networkidle")
            response_window = page.locator(
                'article.workspace-window[data-window-id*="response:0"]')
            table_window = page.locator(
                'article.workspace-window[data-window-id*="table:0"]')
            await expect(response_window).to_be_visible(timeout=10_000)
            await expect(response_window).to_contain_text("Resultado verificable")
            await expect(table_window).to_be_visible(timeout=10_000)
            await expect(table_window.locator("table")).to_be_visible()
            restored_kind = await table_window.locator(
                ".workspace-window-kind").text_content()
            assert "Conversación" in (restored_kind or "")

            # El retorno explícito activa Chat y conserva la tabla abierta.
            await table_window.get_by_role(
                "button", name="Ir a la conversación", exact=True).click()
            await expect(chat_window).to_be_visible(timeout=10_000)
            await expect(page.locator("#chat-panel-messages")).to_contain_text(
                "Resultado verificable")
            await expect(table_window).to_be_visible()
            # Cerrar el objeto no cambia el módulo activo ni destruye el chat.
            table_id = await table_window.get_attribute("data-window-id")
            await page.locator(
                f'#workspace-items button[data-window-id="{table_id}"]').click()
            await table_window.locator('[data-window-action="close"]').click()
            await expect(table_window).to_be_hidden()
            response_id = await response_window.get_attribute("data-window-id")
            await page.locator(
                f'#workspace-items button[data-window-id="{response_id}"]').click()
            await response_window.locator('[data-window-action="close"]').click()
            await expect(response_window).to_be_hidden()

            desktop_before_mobile = await chat_window.bounding_box()
            assert desktop_before_mobile

            # El atajo abre un único launcher aunque se pulse dos veces; Ctrl+K
            # no debe abrir una segunda pila de diálogos.
            await page.keyboard.press("Control+Shift+K")
            await expect(page.locator("#workspace-launcher-dialog")).to_be_visible()
            await page.keyboard.press("Control+Shift+K")
            assert await page.locator("#workspace-launcher-dialog").count() == 1
            await page.locator("#workspace-launcher-close").click()
            await page.keyboard.press("Control+K")
            await expect(page.locator("#workspace-launcher-dialog")).to_be_hidden()
            await expect(page.locator("#search-modal")).to_be_visible()
            await page.locator('#search-close').click()
            await expect(page.locator("#search-modal")).to_be_hidden()

            # Launcher y ventanas no deben crear overflow horizontal en móvil.
            for width in (390, 600):
                await page.set_viewport_size({"width": width, "height": 844})
                await page.locator("#workspace-launcher").click()
                await expect(page.locator("#workspace-launcher-dialog")).to_be_visible()
                overflow = await page.evaluate(
                    "() => ({scrollWidth: document.documentElement.scrollWidth, "
                    "innerWidth, offenders: [...document.querySelectorAll('body *')]"
                    ".map(e => [e, e.getBoundingClientRect()])"
                    ".filter(([, r]) => r.right > innerWidth + 1 || r.left < -1)"
                    ".slice(0, 12).map(([e, r]) => [e.id, e.className, r.left, r.right])})"
                )
                assert overflow["scrollWidth"] <= width + 1, (width, overflow)
                await page.locator("#workspace-launcher-close").click()
                await page.wait_for_timeout(80)
            await page.set_viewport_size({"width": 1440, "height": 1000})
            restored_desktop = await chat_window.bounding_box()
            assert restored_desktop
            for key in ("x", "y", "width", "height"):
                assert abs(restored_desktop[key] - desktop_before_mobile[key]) <= 3, (
                    key, desktop_before_mobile, restored_desktop)

            assert not page_errors, page_errors
            assert not console_errors, console_errors
        finally:
            await browser.close()
