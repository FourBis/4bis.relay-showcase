"""Imágenes adjuntas: van al modelo, pero no se quedan en el historial.

2026-07-31. El relay ya guardaba las imágenes que mandaba el bot, pero
al experto solo le pasaba "nombre (N bytes) — no puedes ver su
contenido". Falso: el modelo ve. Spike contra MiniMax-M3 con un PNG de
bandas roja/verde → "dos bandas horizontales: una roja arriba y una
verde abajo" (228 tokens de entrada para 64x64).

Las dos mitades del cambio:
  - `_prompt_con_imagenes`: el prompt pasa a [texto, BinaryContent…].
  - `_strip_images` en `_dump_messages`: al PERSISTIR, las imágenes se
    reemplazan por una nota. Dentro del run el modelo las sigue viendo;
    lo que se evita es re-mandar un screenshot de 1MB en cada turno
    futuro de la conversación (el mismo N² que atacan las capas de
    elisión de tool results y thinking).

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_images_prompt.py -q
"""
from __future__ import annotations

import json
import unittest

from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from relay import experts

# 50KB: el orden de magnitud de un screenshot real, que es el caso que
# motivó el strip. Con un PNG de juguete la metadata del JSON domina y
# el test no probaría nada.
_PNG = b"\x89PNG\r\n\x1a\n" + b"\xde\xad\xbe\xef" * 12_800


class TestPromptConImagenes(unittest.TestCase):
    def test_sin_imagenes_devuelve_el_str_pelado(self) -> None:
        """El camino sin adjuntos no cambia ni un byte."""
        self.assertEqual(experts._prompt_con_imagenes("hola", None), "hola")
        self.assertEqual(experts._prompt_con_imagenes("hola", []), "hola")

    def test_con_imagenes_arma_lista_multimodal(self) -> None:
        prompt = experts._prompt_con_imagenes(
            "mirá esto", [(_PNG, "image/png"), (_PNG, "image/jpeg")])
        self.assertIsInstance(prompt, list)
        self.assertEqual(prompt[0], "mirá esto")
        self.assertEqual(len(prompt), 3)
        self.assertTrue(all(isinstance(p, BinaryContent) for p in prompt[1:]))
        self.assertEqual(prompt[1].data, _PNG)
        self.assertEqual(prompt[1].media_type, "image/png")
        self.assertEqual(prompt[2].media_type, "image/jpeg")


class TestImagenesFueraDelHistorial(unittest.TestCase):
    def _historial_con_imagen(self) -> list:
        return [
            ModelRequest(parts=[UserPromptPart(content=[
                "mirá esta captura",
                BinaryContent(data=_PNG, media_type="image/png"),
            ])]),
            ModelResponse(parts=[TextPart(content="veo un botón rojo")]),
        ]

    def test_el_historial_persistido_no_lleva_los_bytes(self) -> None:
        from pydantic_ai.messages import ModelMessagesTypeAdapter
        msgs = self._historial_con_imagen()
        dumped = experts._dump_messages(msgs)
        self.assertNotIn("deadbeef", dumped.lower())
        self.assertNotIn("iVBORw0KGg", dumped)      # el png en base64
        # 50KB de imagen: el historial guardado tiene que quedar en el
        # orden del texto, no en el de la imagen.
        crudo = ModelMessagesTypeAdapter.dump_json(msgs).decode("utf-8")
        self.assertLess(len(dumped) * 10, len(crudo),
                        f"strip={len(dumped)} crudo={len(crudo)}")

    def test_el_texto_del_usuario_sobrevive(self) -> None:
        dumped = experts._dump_messages(self._historial_con_imagen())
        self.assertIn("mirá esta captura", dumped)
        self.assertIn("imagen(es) adjunta(s)", dumped)

    def test_el_historial_sigue_siendo_deserializable(self) -> None:
        """Si esto se rompe, la conversación no se puede continuar."""
        from pydantic_ai.messages import ModelMessagesTypeAdapter
        dumped = experts._dump_messages(self._historial_con_imagen())
        msgs = ModelMessagesTypeAdapter.validate_json(dumped)
        self.assertEqual(len(msgs), 2)
        self.assertIsInstance(msgs[0].parts[0].content, str)

    def test_un_historial_sin_imagenes_no_se_toca(self) -> None:
        msgs = [
            ModelRequest(parts=[UserPromptPart(content="hola")]),
            ModelResponse(parts=[TextPart(content="chau")]),
        ]
        dumped = json.loads(experts._dump_messages(msgs))
        self.assertEqual(dumped[0]["parts"][0]["content"], "hola")

    def test_varias_imagenes_se_cuentan(self) -> None:
        msgs = [ModelRequest(parts=[UserPromptPart(content=[
            "tres capturas",
            BinaryContent(data=_PNG, media_type="image/png"),
            BinaryContent(data=_PNG, media_type="image/png"),
            BinaryContent(data=_PNG, media_type="image/png"),
        ])])]
        self.assertIn("[3 imagen(es)", experts._dump_messages(msgs))


if __name__ == "__main__":
    unittest.main()
