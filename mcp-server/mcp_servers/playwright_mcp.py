"""MCP server stdio propio: browser automation con Playwright (Async API).

4bis.relay — el relé lo registra como `playwright-mcp` (capability=browser).
Honesto: solo lectura + screenshot. Sin form fill, sin click-and-type.

Tools (read-mostly):
  navigate(url)             → carga URL, devuelve title + url final + status
  get_title()               → title de la página actual
  get_url()                 → url actual del browser
  get_text(selector="body") → textContent del primer match del selector
  get_html(selector="body",
           max_chars=20000) → innerHTML del selector (cap a max_chars)
  screenshot(path)          → escribe PNG en path, devuelve path + bytes
  close()                   → cierra browser (libera memoria)

Diseño:
  - Async API de Playwright (la sync no se puede usar dentro del event
    loop de FastMCP, da error de "It looks like you are using Playwright
    Sync API inside the asyncio loop").
  - Singleton async con lock: una sola instancia de browser compartida
    entre tool calls. Los MCP stdio son lineales (1 req → 1 resp) así
    que el lock raramente se contiende, pero está.
  - headless=True siempre. Sin sandbox. Documentado.
  - Errores → texto en el resultado, no excepción. El LLM prefiere ver
    el error a quedarse mudo.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright, Browser, Page, Playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                    stream=sys.stderr)
log = logging.getLogger("playwright-mcp")

mcp = FastMCP("playwright-mcp")

# ---- browser state (singleton async) ----
_pw: Optional[Playwright] = None
_browser: Optional[Browser] = None
_page: Optional[Page] = None
_lock = asyncio.Lock()


async def _ensure_page() -> Page:
    global _pw, _browser, _page
    if _pw is None:
        _pw = await async_playwright().start()
        log.info("playwright: started")
    if _browser is None:
        _browser = await _pw.chromium.launch(headless=True)
        log.info("playwright: chromium launched")
    if _page is None:
        _page = await _browser.new_page()
        log.info("playwright: new page")
    return _page


async def _close() -> None:
    global _pw, _browser, _page
    try:
        if _page is not None:
            await _page.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        if _browser is not None:
            await _browser.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        if _pw is not None:
            await _pw.stop()
    except Exception:  # noqa: BLE001
        pass
    _pw = _browser = _page = None


# ---- tools (async) ----

@mcp.tool()
async def navigate(url: str) -> str:
    """Navega a una URL. Devuelve title + URL final + status_code."""
    async with _lock:
        try:
            page = await _ensure_page()
            resp = await page.goto(url, timeout=15000)
            return (f"OK\ntitle: {await page.title()!r}\n"
                    f"url: {page.url}\n"
                    f"status: {resp.status if resp else 'n/a'}")
        except Exception as e:  # noqa: BLE001
            return f"ERROR: {type(e).__name__}: {e}"


@mcp.tool()
async def get_title() -> str:
    """Devuelve el title de la página actual."""
    async with _lock:
        try:
            page = await _ensure_page()
            return await page.title() or "(vacío)"
        except Exception as e:  # noqa: BLE001
            return f"ERROR: {type(e).__name__}: {e}"


@mcp.tool()
async def get_url() -> str:
    """Devuelve la URL actual del browser."""
    async with _lock:
        try:
            page = await _ensure_page()
            return page.url
        except Exception as e:  # noqa: BLE001
            return f"ERROR: {type(e).__name__}: {e}"


@mcp.tool()
async def get_text(selector: str = "body") -> str:
    """Devuelve el textContent del primer match del selector CSS."""
    async with _lock:
        try:
            page = await _ensure_page()
            loc = page.locator(selector).first
            if await loc.count() == 0:
                return f"ERROR: selector {selector!r} no matchea nada"
            return await loc.text_content() or "(vacío)"
        except Exception as e:  # noqa: BLE001
            return f"ERROR: {type(e).__name__}: {e}"


@mcp.tool()
async def get_html(selector: str = "body", max_chars: int = 20000) -> str:
    """Devuelve innerHTML del primer match del selector. Cap a max_chars."""
    async with _lock:
        try:
            page = await _ensure_page()
            loc = page.locator(selector).first
            if await loc.count() == 0:
                return f"ERROR: selector {selector!r} no matchea nada"
            html = await loc.inner_html()
            if len(html) > max_chars:
                return html[:max_chars] + f"\n... (truncado, {len(html)} chars total)"
            return html
        except Exception as e:  # noqa: BLE001
            return f"ERROR: {type(e).__name__}: {e}"


@mcp.tool()
async def screenshot(path: str) -> str:
    """Escribe un screenshot PNG en `path`. Devuelve el path absoluto."""
    async with _lock:
        try:
            page = await _ensure_page()
            p = Path(path).expanduser().resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(p), full_page=True)
            return f"OK: {p} ({p.stat().st_size} bytes)"
        except Exception as e:  # noqa: BLE001
            return f"ERROR: {type(e).__name__}: {e}"


@mcp.tool()
async def close() -> str:
    """Cierra el browser. Útil para liberar memoria entre runs largos."""
    async with _lock:
        await _close()
        return "OK: browser cerrado"


if __name__ == "__main__":
    try:
        mcp.run()
    finally:
        try:
            asyncio.run(_close())
        except Exception:  # noqa: BLE001
            pass
