"""El truncado no puede comerse el error de compilación.

`_cap_text(..., keep_tail=N)` conserva cabeza y cola, y elide el medio.
Pero en un build el banner es la cabeza, el `Compilación con errores` es
la cola… y el `error CS1002` que dice QUÉ rompió vive en el medio. El
que más lo sufre es `_STEP_OUT_MAX` (2400 chars), que alimenta la tarjeta
de progreso y `chats.progress_events`: el único rastro post-mortem de lo
que dijo la terminal cuando alguien audita el run después.

Restricción del fix: el bloque rescatado se DESCUENTA de la cabeza. El
texto final mide lo mismo que antes; no sube ningún cap.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_cap_text_rescata_errores.py -q
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from relay.experts import _cap_text  # noqa: E402


def _cap_text_viejo(s: str, cap: int, *, keep_tail: int = 0) -> str:
    """La versión anterior al fix, para comparar presupuesto."""
    if len(s) <= cap:
        return s
    if keep_tail > 0 and keep_tail < cap:
        head = cap - keep_tail
        return (
            s[:head]
            + f"\n…[TRUNCADO: {len(s) - cap} chars del medio. El resultado "
            f"completo tenía {len(s)} chars; abajo va el FINAL, que es "
            "donde suele estar el veredicto (exit code, error, resumen).]…\n"
            + s[-keep_tail:])
    return (
        s[:cap]
        + f"\n…[TRUNCADO: el resultado completo tenía {len(s)} chars. "
        "Refina la consulta (limit/offset, file_pattern, path más "
        "específico) para ver el resto.]")


class TestRescateDeErrores(unittest.TestCase):

    def test_el_error_del_medio_sobrevive(self) -> None:
        s = ("banner\n" * 500 + "error CS1002: ; esperado\n"
             + "relleno\n" * 500 + "Compilación con errores.\n")
        out = _cap_text(s, 2400, keep_tail=900)
        self.assertIn("CS1002", out)

    def test_el_presupuesto_no_explota(self) -> None:
        s = ("banner\n" * 500 + "error CS1002: ; esperado\n"
             + "relleno\n" * 500 + "Compilación con errores.\n")
        out = _cap_text(s, 2400, keep_tail=900)
        viejo = _cap_text_viejo(s, 2400, keep_tail=900)
        self.assertEqual(len(out), len(viejo))

    def test_msbuild_repite_el_error_por_framework(self) -> None:
        """Sin dedupe, 300 copias de la misma línea se comen la cabeza."""
        err = "error CS0246: no se encontró el tipo 'Foo'\n"
        s = "banner\n" * 300 + err * 300 + "relleno\n" * 300 + "FAILED\n"
        out = _cap_text(s, 2400, keep_tail=900)
        cuerpo = out.split("rescatadas del medio]…\n", 1)[1]
        cuerpo = cuerpo.split("\n…[TRUNCADO", 1)[0]
        self.assertEqual(cuerpo.count("CS0246"), 1)
        self.assertTrue(out.startswith("banner\n"))
        # la cabeza sigue existiendo, no la devoró el bloque
        self.assertGreaterEqual(out.count("banner"), 20)
        self.assertEqual(len(out), len(_cap_text_viejo(s, 2400, keep_tail=900)))

    def test_sin_errores_sale_byte_a_byte_igual_que_antes(self) -> None:
        s = "todo bien\n" * 2000
        for cap, tail in ((2400, 900), (48000, 12000), (16000, 0)):
            with self.subTest(cap=cap, tail=tail):
                self.assertEqual(
                    _cap_text(s, cap, keep_tail=tail),
                    _cap_text_viejo(s, cap, keep_tail=tail))

    def test_texto_corto_no_se_toca(self) -> None:
        s = "error CS1002\n"
        self.assertEqual(_cap_text(s, 2400, keep_tail=900), s)

    def test_cap_chico_no_deja_la_cabeza_en_cero(self) -> None:
        """Si no entra el bloque, no hay rescate y el resultado es el viejo."""
        s = "error CS1002: roto\n" * 500
        out = _cap_text(s, 1000, keep_tail=900)
        self.assertEqual(out, _cap_text_viejo(s, 1000, keep_tail=900))

    def test_el_marcador_declara_bien_lo_elidido(self) -> None:
        s = ("banner\n" * 500 + "error CS1002: ; esperado\n"
             + "relleno\n" * 500 + "fin\n")
        out = _cap_text(s, 2400, keep_tail=900)
        import re
        m = re.search(r"TRUNCADO: (\d+) chars del medio", out)
        assert m is not None
        head_len = out.index("\n…[")
        self.assertEqual(int(m.group(1)), len(s) - head_len - 900)


if __name__ == "__main__":
    unittest.main()
