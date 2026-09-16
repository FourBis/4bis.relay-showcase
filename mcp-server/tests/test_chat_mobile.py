"""El tab Chats en móvil: la lista es un drawer, no una columna.

Medido a 375px ANTES del cambio (2026-07-31): `.chat-sidebar` se llevaba
288px fijos y le dejaba 87px al hilo. Ahora la lista sale del flujo como
overlay y el hilo usa el ancho completo.

Esto es verificación ESTÁTICA: que las piezas estén y estén conectadas.
El layout real se probó en el navegador a 375/1280 (sidebar en x=-288
cerrada, 0 abierta; en desktop vuelve a columna estática de 288px). Un
test de layout de verdad pediría un browser headless, que este repo no
tiene en la suite.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_chat_mobile.py -q
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "admin_static"
_HTML = (_STATIC / "index.html").read_text(encoding="utf-8")
_CSS = (_STATIC / "static" / "admin.css").read_text(encoding="utf-8")
_JS = (_STATIC / "static" / "tab-chats.js").read_text(encoding="utf-8")


def _bloques_media_767() -> list[str]:
    """Cuerpos de los `@media (max-width:767px)` del bundle minificado.

    Con llaves balanceadas y no con un regex: el bundle es una sola
    línea y `.*?\\}\\}` corta en la primera regla anidada."""
    out = []
    for m in re.finditer(r"@media\s*\(max-width:\s*767px\)\s*\{", _CSS):
        i, depth = m.end(), 1
        while i < len(_CSS) and depth:
            depth += {"{": 1, "}": -1}.get(_CSS[i], 0)
            i += 1
        out.append(_CSS[m.end():i - 1])
    return out


class TestDrawerMarkup(unittest.TestCase):
    def test_el_html_tiene_las_piezas(self) -> None:
        self.assertIn('id="chat-drawer-backdrop"', _HTML)
        # dos disparadores: el ☰ del header y el del estado vacío (en
        # móvil no hay "elegí una de la izquierda": no hay izquierda)
        botones = re.findall(r'class="[^"]*\bchat-drawer-btn\b[^"]*"', _HTML)
        self.assertEqual(len(botones), 2, botones)
        self.assertIn('id="chat-drawer-btn"', _HTML)
        self.assertIn("chat-drawer-btn-empty", _HTML)

    def test_el_js_wirea_el_drawer(self) -> None:
        self.assertIn("wireChatDrawer", _JS)
        self.assertIn("chat-drawer-backdrop", _JS)
        # se llama en el init, si no los botones no hacen nada
        init = _JS[_JS.index("export function initChats"):]
        self.assertIn("wireChatDrawer()", init)

    def test_elegir_conversacion_cierra_el_drawer(self) -> None:
        """Si no, la lista tapa justo el hilo que acabás de abrir."""
        cuerpo = _JS[_JS.index("export async function selectConversation"):]
        cuerpo = cuerpo[:cuerpo.index("\n}")]
        self.assertIn("setChatDrawer(false)", cuerpo)


class TestAdjuntosComposer(unittest.TestCase):
    """Upload desde la UI (2026-07-31). Antes solo Discord podía adjuntar."""

    def test_el_composer_tiene_el_uploader(self) -> None:
        self.assertIn('id="chat-attach-input"', _HTML)
        self.assertIn('id="chat-attach-btn"', _HTML)
        self.assertIn('id="chat-attach-tray"', _HTML)

    def test_el_js_sube_al_endpoint_generico(self) -> None:
        """`/attachments`, no `/discord/attachments`: la UI no es Discord."""
        self.assertIn('apiRoot("/attachments"', _JS)
        self.assertIn("wireAttachments()", _JS)

    def test_los_adjuntos_viajan_en_el_run(self) -> None:
        cuerpo = _JS[_JS.index("async function sendCurrentMessage"):]
        cuerpo = cuerpo[:cuerpo.index("\n}")]
        self.assertIn("attachments:", cuerpo)
        # y la bandeja se vacía en el envío (si no, se cuelan en el próximo)
        self.assertIn("pendingAttachments = []", cuerpo)

    def test_las_miniaturas_usan_el_id_sin_extension(self) -> None:
        """El endpoint resuelve por id pelado: con `.png` da 404 y la
        burbuja muestra una imagen rota."""
        cuerpo = _JS[_JS.index("function withThumbs"):]
        cuerpo = cuerpo[:cuerpo.index("\n}")]
        self.assertIn('replace(/\\.[a-z0-9]+$/i, "")', cuerpo)

    def test_el_texto_del_usuario_se_sigue_escapando(self) -> None:
        """La miniatura se inyecta DESPUÉS de escape() y solo sobre un id
        [0-9a-f]{16}: si alguien invierte el orden, vuelve el XSS."""
        cuerpo = _JS[_JS.index("function withThumbs"):]
        cuerpo = cuerpo[:cuerpo.index("\n}")]
        self.assertIn("escape(texto).replace(", cuerpo)


class TestDrawerCss(unittest.TestCase):
    """admin.css es output commiteado: si alguien edita el .src.css y no
    corre build-css.ps1, estas reglas no existen en lo que se sirve.
    (test_admin_css_build cubre el sello; esto cubre el contenido.)"""

    def test_el_bundle_tiene_las_reglas_del_drawer(self) -> None:
        self.assertIn("chat-sidebar.mobile-open", _CSS)
        self.assertIn("chat-drawer-backdrop", _CSS)

    def test_las_reglas_estan_dentro_del_media_de_movil(self) -> None:
        bloques = _bloques_media_767()
        movil = "".join(bloques)
        self.assertTrue(movil, "no hay media query de 767px en el bundle")
        self.assertIn("mobile-open", movil)
        self.assertIn("100dvh", movil)   # vh deja el composer bajo el teclado
        # Workspace: la lista es contextual también en escritorio; el
        # overlay está acotado al chat y no ocupa una columna permanente.
        self.assertRegex(_CSS, r"body\.workspace-mode \.chat-sidebar\.mobile-open\{[^}]*position:absolute")

    def test_los_botones_del_drawer_se_ocultan_en_desktop(self) -> None:
        """Con `.chat-drawer-btn` a secas no alcanzaba: `button.chat-icon-btn`
        tiene más especificidad y el ☰ se colaba visible en desktop."""
        self.assertRegex(_CSS, r"button\.chat-drawer-btn\{display:none\}")

    def test_el_header_wrappea_siempre(self) -> None:
        """Regresión reportada: con `nowrap` + scrollbar oculto, las
        acciones del header (🐙, 🔗, diff, compactar, cerrar) quedaban
        fuera del viewport —954px de contenido en 375— sin ninguna pista
        de que seguían ahí. Wrappear cuesta alto; esconderlas, funciones.

        La regla salió del @media el 2026-08-31: el ancho que aprieta al
        header es el del PANEL, no el de la ventana. Con los tiradores en
        los topes (lista 180, plan 900) el hilo queda en 220px dentro de
        una ventana de 1366 —o sea, con el media de 767 sin disparar— y
        las acciones se pintaban ENCIMA de los paneles vecinos (medido:
        "🔒 Cerrar" terminaba en x=1362 en una caja que termina en 461).
        Fuera del media cubre los dos casos; con ancho de sobra
        `flex-wrap` no hace nada."""
        desktop = _CSS
        for b in _bloques_media_767():
            desktop = desktop.replace(b, "")
        self.assertRegex(desktop, r"chat-convo-head\{[^}]*flex-wrap:wrap")
        self.assertNotRegex(_CSS, r"chat-convo-head[^{}]*\{[^}]*nowrap")

    def test_vscode_se_oculta_en_movil(self) -> None:
        """Abre el repo en el escritorio: desde el teléfono no hace nada.
        Es el único del header que sobra — el resto sí se usa."""
        movil = "".join(_bloques_media_767())
        self.assertRegex(movil, r"#chat-panel-vscode\{display:none")
        # …y en desktop sigue estando
        desktop = _CSS
        for b in _bloques_media_767():
            desktop = desktop.replace(b, "")
        self.assertNotRegex(desktop, r"#chat-panel-vscode\{display:none")

    def test_el_ancho_del_drawer_no_depende_de_collapsed(self) -> None:
        """El minificador fusiona declaraciones iguales: cuando el width
        estaba repetido en `.chat-sidebar` y `.chat-sidebar.collapsed`,
        quedó SOLO en `.collapsed` y el drawer se abría en 1px."""
        m = re.search(r"([^{}]*)\{[^}]*width:min\(85vw", _CSS)
        self.assertIsNotNone(m, "no está la regla de ancho del drawer")
        selector = m.group(1)
        self.assertIn(".chat-sidebar,", selector + ",")


if __name__ == "__main__":
    unittest.main()
