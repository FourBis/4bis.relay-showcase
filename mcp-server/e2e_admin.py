"""E2E del admin UI.

Asume relay corriendo en 127.0.0.1:8413.
"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8413/admin/"


def get_first_row_with_actions(page):
    rows = page.query_selector_all("#projects-table tbody tr")
    for r in rows:
        btns = r.query_selector_all(".row-actions button")
        if len(btns) >= 2:
            slug = r.get_attribute("data-slug")
            admin_btn = next((b for b in btns
                              if "administrar" in
                              (b.get_attribute("title") or "")), None)
            return slug, admin_btn
    return None, None


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()

        page.on("console", lambda m: print(f"[{m.type}] {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: print(f"[pageerror] {e}"))

        print(">>> navegando a admin")
        page.goto(BASE)
        page.click('.tab[data-tab="projects"]')
        try:
            page.wait_for_selector("#projects-table tbody tr", timeout=10_000)
        except Exception:
            print(f"FAIL: no rows. URL={page.url}")
            page.screenshot(path="e2e-fail.png", full_page=True)
            raise

        print(">>> check 1: fila con row-actions")
        slug, admin_btn = get_first_row_with_actions(page)
        assert slug, "ninguna fila tiene row-actions con 2+ botones"
        first_row = page.query_selector(
            f'#projects-table tbody tr[data-slug="{slug}"]')
        buttons = first_row.query_selector_all(".row-actions button")
        print(f"    slug={slug} | botones={len(buttons)}")
        labels = [b.text_content().strip() for b in buttons]
        print(f"    labels: {labels}")
        expected_icons = {"🖥", "⚙", "🧪"}
        assert expected_icons.issubset(set(labels)), (
            f"faltan botones esperados: {expected_icons - set(labels)}")
        print("    OK: botones esperados presentes")

        # check 2: sidepanel admin con tabs
        print(">>> check 2: admin sidepanel con tabs")
        admin_btn.click()
        page.wait_for_selector("#admin-sidepanel:not(.hidden)", timeout=5000)
        page.wait_for_selector(".admin-tab", timeout=5000)
        tabs = page.query_selector_all(".admin-tab")
        tab_keys = [t.get_attribute("data-tab") for t in tabs]
        print(f"    tabs: {tab_keys}")
        expected = ["general", "prompt", "workspace", "index",
                    "git", "night", "expert", "danger"]
        assert tab_keys == expected, f"tabs no coinciden: {tab_keys}"
        print(f"    OK: {len(tabs)} tabs")

        # check 3: cambio entre tabs renderiza el panel
        print(">>> check 3: tabs renderizan contenido")
        for key in ("workspace", "index", "general", "danger", "prompt"):
            page.click(f'.admin-tab[data-tab="{key}"]')
            page.wait_for_selector('#admin-panel *', timeout=3000)
            html_len = page.eval_on_selector(
                "#admin-panel", "el => el.innerHTML.length")
            print(f"    {key}: {html_len} chars")
            assert html_len > 100, f"tab {key} vacío"

        # check 4: bug fix, 2 guardados seguidos
        print(">>> check 4: bug fix — 2 PATCHes seguidos")
        page.click('.admin-tab[data-tab="general"]')
        page.wait_for_selector("#edit-name", timeout=3000)
        page.wait_for_selector("#edit-desc", timeout=3000)

        ts = int(time.time())
        desc1 = f"e2e save1 {ts}"
        desc2 = f"e2e save2 {ts}"

        page.fill("#edit-desc", desc1)
        page.click("#edit-save-inline")
        page.wait_for_function(
            '() => { const b = document.querySelector("#edit-save-inline");'
            ' return b && !b.disabled; }',
            timeout=5000)

        save_disabled = page.eval_on_selector(
            "#edit-save-inline", "el => el.disabled")
        print(f"    save.disabled entre saves: {save_disabled}")
        assert not save_disabled, (
            "BUG VIVO: save quedó disabled después del primer éxito")

        page.fill("#edit-desc", desc2)
        page.click("#edit-save-inline")
        page.wait_for_function(
            '() => { const b = document.querySelector("#edit-save-inline");'
            ' return b && !b.disabled; }',
            timeout=5000)

        api_desc = page.evaluate(
            f"""async () => {{
                const r = await fetch('/admin/api/projects/{slug}');
                const d = await r.json();
                return d.project.description;
            }}"""
        )
        print(f"    DB tras 2do save: {api_desc!r}")
        assert api_desc == desc2, (
            f"BUG VIVO: DB no refleja desc2 (tiene {api_desc!r})")
        print("    OK: desc2 persistido (ambos guardados funcionaron)")

        nc = page.evaluate(
            f"""async () => {{
                const r = await fetch('/admin/api/projects/{slug}');
                const d = await r.json();
                return d.project.night_config;
            }}"""
        )
        print(f"    night_config en DB: {nc}")
        assert isinstance(nc, dict), f"night_config no es dict: {nc!r}"

        # check 5: tab Workspace
        print(">>> check 5: tab Workspace")
        page.click('.admin-tab[data-tab="workspace"]')
        page.wait_for_selector("#ws-list", timeout=3000)
        page.wait_for_function(
            '() => document.querySelector("#ws-list")'
            ' && !/cargando/i.test(document.querySelector("#ws-list").innerHTML)',
            timeout=5000)
        list_text = page.eval_on_selector("#ws-list", "el => el.innerText")
        print(f"    ws-list (primeros 100): {list_text[:100]!r}")
        assert len(list_text) > 0 or "(vacío)" in list_text, \
            "ws-list sin contenido"
        print("    OK: workspace cargó")

        page.screenshot(path="e2e-admin.png", full_page=True)
        print(">>> screenshot: e2e-admin.png")
        print()
        print("=" * 50)
        print("ALL CHECKS PASSED")
        print("=" * 50)
        browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
