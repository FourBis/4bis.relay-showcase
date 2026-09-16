"""Test nivel 1 (mock) del wrapper Playwright.

NO toca red, NO toca el browser real, NO hace spawn del subprocess.
Solo verifica que las funciones async de Playwright se llamen con
los argumentos correctos.

Manda el output a /tmp o al working dir con un nombre estable.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Hacer que el wrapper sea importable
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "mcp_servers"))
sys.path.insert(0, str(REPO / "src"))

# `playwright` es dependencia del wrapper, no del relay: no está en el
# pyproject y no la tiene toda máquina que corre la suite. Sin este
# skip, el import de abajo revienta la COLECCIÓN del archivo y pytest lo
# reporta como error de la suite, no como "falta una dependencia
# opcional" (2026-08-16).
pytest.importorskip(
    "playwright",
    reason="playwright no instalado: pip install playwright")

from playwright_mcp import (  # noqa: E402
    _ensure_page, _close, navigate, get_title, get_url,
    get_text, get_html, screenshot, close,
    _pw, _browser, _page, _lock,
)


def _reset_state():
    """Reset module-level singletons entre tests."""
    import playwright_mcp
    playwright_mcp._pw = None
    playwright_mcp._browser = None
    playwright_mcp._page = None


class TestNavigateMock(unittest.IsolatedAsyncioTestCase):
    """Verifica que navigate() use los métodos correctos de Playwright."""

    async def asyncSetUp(self) -> None:
        _reset_state()

    async def test_navigate_returns_title_url_status(self) -> None:
        # Mock del page que devuelve Playwright. Las coroutines (title,
        # goto) son AsyncMock; el atributo url es string plano.
        mock_page = MagicMock()
        mock_page.title = AsyncMock(return_value="Test Page")
        mock_page.url = "https://example.com"
        mock_page.goto = AsyncMock(
            return_value=MagicMock(status=200))

        with patch("playwright_mcp._ensure_page",
                   AsyncMock(return_value=mock_page)):
            result = await navigate("https://example.com")
        # Verifica el formato del output.
        self.assertIn("title: 'Test Page'", result)
        self.assertIn("url: https://example.com", result)
        self.assertIn("status: 200", result)
        # Verifica que goto se llamó con timeout.
        mock_page.goto.assert_awaited_once()
        call = mock_page.goto.await_args
        self.assertEqual(call.args[0], "https://example.com")
        self.assertEqual(call.kwargs.get("timeout"), 15000)

    async def test_navigate_handles_exception(self) -> None:
        mock_page = MagicMock()
        mock_page.goto = AsyncMock(
            side_effect=RuntimeError("network down"))
        with patch("playwright_mcp._ensure_page",
                   AsyncMock(return_value=mock_page)):
            result = await navigate("https://x.test")
        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("network down", result)


class TestGetTextMock(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        _reset_state()

    async def test_get_text_default_body(self) -> None:
        mock_locator = MagicMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.text_content = AsyncMock(
            return_value="hello world")
        # El wrapper hace `page.locator(selector).first`. La chain es:
        #   page.locator(selector) -> Locator (no coroutine)
        #   .first -> property que devuelve un Locator
        #   .count() -> coroutine
        #   .text_content() -> coroutine
        # Mockeamos locator para que su `.first` devuelva un objeto
        # con .count() y .text_content() async.
        mock_locator.first = mock_locator
        mock_page = MagicMock()
        mock_page.locator = MagicMock(return_value=mock_locator)
        with patch("playwright_mcp._ensure_page",
                   AsyncMock(return_value=mock_page)):
            result = await get_text()
        self.assertEqual(result, "hello world")
        mock_page.locator.assert_called_once_with("body")

    async def test_get_text_selector_not_found(self) -> None:
        mock_locator = MagicMock()
        mock_locator.count = AsyncMock(return_value=0)
        mock_locator.first = mock_locator
        mock_page = MagicMock()
        mock_page.locator = MagicMock(return_value=mock_locator)
        with patch("playwright_mcp._ensure_page",
                   AsyncMock(return_value=mock_page)):
            result = await get_text(".no-existe")
        self.assertIn("ERROR:", result)
        self.assertIn(".no-existe", result)


class TestGetHtmlMock(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        _reset_state()

    async def test_get_html_truncates(self) -> None:
        long_html = "<p>" + "x" * 25000 + "</p>"
        mock_locator = MagicMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.inner_html = AsyncMock(return_value=long_html)
        mock_locator.first = mock_locator
        mock_page = MagicMock()
        mock_page.locator = MagicMock(return_value=mock_locator)
        with patch("playwright_mcp._ensure_page",
                   AsyncMock(return_value=mock_page)):
            result = await get_html("p", max_chars=100)
        self.assertIn("truncado", result)
        self.assertLessEqual(len(result), 200)

    async def test_get_html_passes_through_when_short(self) -> None:
        short = "<p>ok</p>"
        mock_locator = MagicMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.inner_html = AsyncMock(return_value=short)
        mock_locator.first = mock_locator
        mock_page = MagicMock()
        mock_page.locator = MagicMock(return_value=mock_locator)
        with patch("playwright_mcp._ensure_page",
                   AsyncMock(return_value=mock_page)):
            result = await get_html("p", max_chars=20000)
        self.assertEqual(result, short)


class TestScreenshotMock(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        _reset_state()

    async def test_screenshot_creates_file(self) -> None:
        # unittest no soporta tmp_path de pytest; usamos tempfile
        # manual. El wrapper escribe el archivo de verdad — el test
        # es del wrapper, no de Path.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out.png"
            mock_page = MagicMock()

            async def fake_screenshot(*, path, full_page):
                # Crea el archivo con bytes dummy.
                with open(path, "wb") as f:
                    f.write(b"x" * 12345)
            mock_page.screenshot = fake_screenshot

            with patch("playwright_mcp._ensure_page",
                       AsyncMock(return_value=mock_page)):
                result = await screenshot(str(out))

            self.assertIn("OK:", result)
            self.assertIn("12345 bytes", result)
            self.assertIn(str(out), result)
            self.assertTrue(out.is_file())
            self.assertEqual(out.stat().st_size, 12345)


class TestSingletonLifecycle(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        _reset_state()

    async def test_ensure_page_creates_browser(self) -> None:
        """_ensure_page arranca playwright + browser + page en orden."""
        # Importar dentro del test para tener el módulo fresco.
        import playwright_mcp

        mock_pw_instance = MagicMock()
        mock_browser_instance = MagicMock()
        mock_page_instance = MagicMock()
        mock_pw_instance.chromium.launch = AsyncMock(
            return_value=mock_browser_instance)
        mock_browser_instance.new_page = AsyncMock(
            return_value=mock_page_instance)
        mock_async_pw = MagicMock()
        mock_async_pw.start = AsyncMock(return_value=mock_pw_instance)

        with patch("playwright_mcp.async_playwright",
                   MagicMock(return_value=mock_async_pw)):
            page = await _ensure_page()

        # Verifica que se llamó chromium.launch con headless=True.
        launch_call = mock_pw_instance.chromium.launch.await_args
        self.assertEqual(launch_call.kwargs.get("headless"), True)
        # Verifica que la page devuelta es la esperada.
        self.assertIs(page, mock_page_instance)

    async def test_ensure_page_reuses_singleton(self) -> None:
        """Llamar _ensure_page dos veces no vuelve a crear browser."""
        import playwright_mcp
        playwright_mcp._pw = MagicMock()
        playwright_mcp._browser = MagicMock()
        mock_page = MagicMock()
        playwright_mcp._browser.new_page = AsyncMock(
            return_value=mock_page)
        # _pw.start NO se llama porque ya hay _pw.

        with patch("playwright_mcp.async_playwright") as mock_apw:
            page = await _ensure_page()
        # async_playwright no se invocó.
        mock_apw.assert_not_called()
        self.assertIs(page, mock_page)


class TestCloseAndLocking(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        _reset_state()

    async def test_close_resets_singletons(self) -> None:
        """close() llama _close() y vacía los singletons."""
        import playwright_mcp
        # Setup con mocks
        mock_pw = MagicMock()
        mock_pw.stop = AsyncMock()
        mock_browser = MagicMock()
        mock_browser.close = AsyncMock()
        mock_page = MagicMock()
        mock_page.close = AsyncMock()
        playwright_mcp._pw = mock_pw
        playwright_mcp._browser = mock_browser
        playwright_mcp._page = mock_page

        result = await close()
        self.assertEqual(result, "OK: browser cerrado")
        self.assertIsNone(playwright_mcp._pw)
        self.assertIsNone(playwright_mcp._browser)
        self.assertIsNone(playwright_mcp._page)
        # Verifica que se cerró en orden: page, browser, pw.
        mock_page.close.assert_awaited_once()
        mock_browser.close.assert_awaited_once()
        mock_pw.stop.assert_awaited_once()

    async def test_navigate_uses_lock(self) -> None:
        """El lock se acquire/release alrededor de la operación."""
        import playwright_mcp
        # Mock del lock para ver si se acquire.
        mock_lock = AsyncMock()
        mock_lock.__aenter__ = AsyncMock(return_value=mock_lock)
        mock_lock.__aexit__ = AsyncMock(return_value=None)
        playwright_mcp._lock = mock_lock

        mock_page = MagicMock()
        mock_page.goto = AsyncMock(
            return_value=MagicMock(status=200))
        mock_page.title = AsyncMock(return_value="x")
        with patch("playwright_mcp._ensure_page",
                   AsyncMock(return_value=mock_page)):
            await navigate("https://x.test")

        # El lock se entró y salió una vez.
        mock_lock.__aenter__.assert_awaited_once()
        mock_lock.__aexit__.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
