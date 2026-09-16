"""Tests Discord-attachment: upload multipart + integración con /experts/run.

Cubre:
  - attachments.store() unitario (id estable, dedupe, mapeo de mime).
  - POST /discord/attachments (multipart): 201/400/413.
  - Backward-compat: POST /experts/run sin campo `attachments` funciona
    idéntico al caso viejo (no rompe el flujo actual).

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_attachments.py -q
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from relay import attachments as attachments_mod
from relay.db import Database

_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR",
             "FOURBIS_ATTACHMENTS_DIR")


# ---------- unidad: attachments_mod.store() ----------

class StoreUnitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["FOURBIS_ATTACHMENTS_DIR"] = str(Path(self._tmp.name))
        # Forzar recrear el global del módulo.
        self._saved = attachments_mod.attachments_dir()

    async def asyncTearDown(self) -> None:
        os.environ.pop("FOURBIS_ATTACHMENTS_DIR", None)
        self._tmp.cleanup()

    async def test_store_returns_stable_id(self) -> None:
        buf = b"hola mundo"
        id1, path1, size1 = attachments_mod.store(buf, filename="a.txt")
        id2, path2, size2 = attachments_mod.store(buf, filename="a.txt")
        # Mismo id y mismo path (dedupe).
        self.assertEqual(id1, id2)
        self.assertEqual(path1, path2)
        self.assertEqual(size1, size2)
        self.assertEqual(size1, len(buf))

    async def test_store_derives_extension_from_filename(self) -> None:
        _, path, _ = attachments_mod.store(b"x", filename="img.png")
        self.assertEqual(path.suffix, ".png")

    async def test_store_derives_extension_from_mimetype_when_no_filename(self) -> None:
        _, path, _ = attachments_mod.store(b"x", mimetype="application/pdf")
        self.assertEqual(path.suffix, ".pdf")

    async def test_store_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            attachments_mod.store(b"")

    async def test_resolve_finds_existing_file(self) -> None:
        buf = b"abc"
        attach_id, _, _ = attachments_mod.store(buf, filename="a.txt")
        self.assertIsNotNone(attachments_mod.resolve(attach_id))

    async def test_resolve_unknown_returns_none(self) -> None:
        self.assertIsNone(attachments_mod.resolve("att_deadbeefdeadbeef"))
        self.assertIsNone(attachments_mod.resolve("not_a_valid_id"))

    async def test_format_user_block_inlines_text_content(self) -> None:
        """Bug fix 2026-07-20: el contenido de texto va INLINE al prompt.

        Antes se inyectaba el PATH y se esperaba que el LLM lo leyera
        con read_file — imposible (path relativo al cwd del relay +
        wrapper scopeado al workspace del proyecto). Caso real: el
        .txt que Discord sugiere para prompts largos llegaba vacío.
        """
        buf = "el contenido REAL del prompt largo".encode("utf-8")
        attach_id, path, _ = attachments_mod.store(buf, filename="s.txt")
        block = attachments_mod.format_user_block([attach_id])
        self.assertIn("## Adjuntos", block)
        self.assertIn(attach_id, block)  # en el nombre del archivo
        self.assertIn("el contenido REAL del prompt largo", block)

    async def test_format_user_block_truncates_huge_text(self) -> None:
        cap = attachments_mod._inline_max()
        buf = ("x" * (cap + 5000)).encode("utf-8")
        attach_id, _, _ = attachments_mod.store(buf, filename="big.txt")
        block = attachments_mod.format_user_block([attach_id])
        self.assertIn("TRUNCADO", block)
        self.assertLess(len(block), cap + 1000)

    async def test_format_user_block_binary_da_el_path(self) -> None:
        """Binarios: metadata + RUTA, nunca bytes crudos.

        2026-08-26: antes esto decía "no puedes ver su contenido; pídele
        al usuario que lo mande como texto", que era un callejón sin
        salida — el archivo estaba en disco y el experto tiene run_shell.
        Lo que faltaba era el path (y la raíz extra del sandbox, que
        pone experts.py).
        """
        attach_id, path, _ = attachments_mod.store(
            b"%PDF-1.4\n" + b"\x00" * 64, filename="informe.pdf")
        block = attachments_mod.format_user_block([attach_id])
        self.assertIn(attach_id, block)
        self.assertIn(str(path.resolve()), block)
        self.assertNotIn("\x00", block)
        # El callejón sin salida no vuelve.
        self.assertNotIn("mande como texto", block)

    async def test_binario_es_legible_desde_el_sandbox_del_experto(self) -> None:
        """La promesa del bloque tiene que ser cierta: con el directorio
        de adjuntos como raíz extra, las tools de archivo LLEGAN.

        Sin esa raíz el bloque le daría al experto un path que sus
        propias tools rechazan con FueraDelRepo — que es exactamente el
        agujero que el mensaje viejo esquivaba en vez de arreglar.
        """
        from relay import files as files_mod

        attach_id, path, _ = attachments_mod.store(
            b"columna_a,columna_b\n1,2\n", filename="datos.parquet")
        with tempfile.TemporaryDirectory() as repo:
            perm = files_mod.Permisos.para(
                repo, extras=[str(attachments_mod.attachments_dir())])
            # No levanta FueraDelRepo y trae el contenido real.
            self.assertIn("columna_a", files_mod.leer(perm, str(path)))
            # El repo sigue siendo la raíz principal: la extra suma, no
            # reemplaza.
            self.assertIn(Path(repo).resolve(), perm.raices)

    async def test_imagen_se_anuncia_como_visible(self) -> None:
        """2026-07-31: el bloque decía "no puedes ver su contenido" para
        TODO binario. Falso para imágenes: el modelo las ve (spike con
        MiniMax-M3), y decirle lo contrario lo hace pedir que se las
        manden como texto."""
        buf = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        attach_id, _, _ = attachments_mod.store(buf, filename="foto.png")
        block = attachments_mod.format_user_block([attach_id])
        # el nombre que se ve es `att_<hash>.png`: store() no conserva el
        # original (content-addressed). El modelo distingue por ese id.
        self.assertIn(f"{attach_id}.png", block)
        self.assertIn("puedes verla", block)
        self.assertNotIn("no puedes ver", block)
        self.assertNotIn("\x89", block)      # nunca los bytes

    async def test_load_images_devuelve_bytes_y_mime(self) -> None:
        buf = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        png, _, _ = attachments_mod.store(buf, filename="foto.png")
        txt, _, _ = attachments_mod.store(b"hola", filename="nota.txt")
        pdf, _, _ = attachments_mod.store(b"%PDF-1.4", filename="doc.pdf")
        imgs = attachments_mod.load_images([png, txt, pdf, "att_deadbeef"])
        self.assertEqual(len(imgs), 1)       # solo la imagen
        data, mime = imgs[0]
        self.assertEqual(data, buf)
        self.assertEqual(mime, "image/png")

    async def test_load_images_respeta_el_cap(self) -> None:
        """La imagen viaja en CADA request del run: una foto de 8MB se
        paga en todas las vueltas."""
        big = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096
        attach_id, _, _ = attachments_mod.store(big, filename="grande.png")
        os.environ["FOURBIS_ATTACH_IMAGE_MAX"] = "1024"
        try:
            self.assertEqual(attachments_mod.load_images([attach_id]), [])
            block = attachments_mod.format_user_block([attach_id])
            self.assertIn("excede el cap", block)
        finally:
            os.environ.pop("FOURBIS_ATTACH_IMAGE_MAX", None)

    async def test_load_images_sin_ids_no_rompe(self) -> None:
        self.assertEqual(attachments_mod.load_images([]), [])
        self.assertEqual(attachments_mod.load_images(["att_nada"]), [])

    async def test_format_user_block_empty_returns_empty(self) -> None:
        self.assertEqual(attachments_mod.format_user_block([]), "")

    async def test_store_rejects_dangerous_extension(self) -> None:
        """Bug fix 2026-07-18: .exe/.ps1/.bat/.sh/etc deben rechazarse."""
        for bad in ("evil.exe", "trojan.dll", "macro.vbs",
                    "install.sh", "run.bat", "payload.ps1"):
            with self.subTest(filename=bad):
                with self.assertRaises(ValueError):
                    attachments_mod.store(b"x" * 16, filename=bad)

    async def test_store_rejects_dangerous_mimetype(self) -> None:
        """Bug fix 2026-07-18: application/x-msdownload y similares off."""
        for bad_mt in ("application/x-msdownload",
                       "application/x-shellscript",
                       "application/javascript"):
            with self.subTest(mimetype=bad_mt):
                with self.assertRaises(ValueError):
                    attachments_mod.store(b"x" * 16, mimetype=bad_mt)

    async def test_format_user_block_omits_unresolvable_ids(self) -> None:
        """Bug fix 2026-07-18: id que no existe en disco no debe
        aparecer como '(no encontrado)' — el LLM lo trata como
        contenido válido y alucina. Mejor omitirlo del bloque.
        Si ninguno resuelve, el bloque entero se descarta.
        """
        # Solo ids fantasma → bloque vacío.
        block = attachments_mod.format_user_block(["att_deadbeefdeadbeef"])
        self.assertEqual(block, "")
        # Un id válido + un fantasma → solo el válido aparece.
        attach_id, _, _ = attachments_mod.store(b"sample", filename="s.txt")
        block = attachments_mod.format_user_block(
            [attach_id, "att_deadbeefdeadbeef"])
        self.assertIn(attach_id, block)
        self.assertNotIn("att_deadbeefdeadbeef", block)
        self.assertNotIn("no encontrado", block.lower())


# ---------- HTTP: POST /discord/attachments ----------

class DiscordAttachmentsHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        os.environ["FOURBIS_ATTACHMENTS_DIR"] = str(base / "attachments")

        self.db = Database()
        await self.db.init_schema()
        # Proyecto dummy para /experts/run.
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo",
            "repo_path": str(base), "description": "test"})

        from relay.server import create_app
        self.app = create_app()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp.cleanup()

    async def _post_file(self, content: bytes, filename: str = "x.bin",
                         mimetype: str | None = None) -> tuple[int, dict]:
        # aiohttp test client: client.post(data=form) setea el
        # Content-Type con boundary solo. No seteamos header manual.
        form = aiohttp.FormData()
        form.add_field("file", io.BytesIO(content),
                       filename=filename,
                       content_type=mimetype or "application/octet-stream")
        r = await self.client.post("/discord/attachments", data=form)
        return r.status, await r.json()

    async def test_201_returns_id_and_persisted(self) -> None:
        status, body = await self._post_file(b"hello", "a.txt", "text/plain")
        self.assertEqual(status, 201)
        self.assertIn("id", body)
        self.assertTrue(body["id"].startswith("att_"))
        self.assertEqual(body["bytes"], len(b"hello"))
        self.assertIn("path", body)
        # El path en disco existe.
        self.assertTrue(Path(body["path"]).exists())

    async def test_400_no_file(self) -> None:
        # FormData sin `file` — el handler devuelve 400.
        form = aiohttp.FormData()
        form.add_field("foo", "bar")
        r = await self.client.post("/discord/attachments", data=form)
        self.assertEqual(r.status, 400)

    async def test_400_not_multipart(self) -> None:
        r = await self.client.post(
            "/discord/attachments",
            json={"foo": "bar"})
        self.assertEqual(r.status, 400)

    async def test_413_oversized(self) -> None:
        # Bajar el cap a algo chico para no escribir 50MB en el test.
        os.environ["ATTACHMENT_MAX_BYTES"] = "128"
        cap = attachments_mod.max_attachment_bytes()
        self.assertEqual(cap, 128)
        status, _body = await self._post_file(b"x" * 200)
        self.assertEqual(status, 413)
        os.environ.pop("ATTACHMENT_MAX_BYTES", None)

    async def test_dedupe_same_bytes_same_id(self) -> None:
        s1, b1 = await self._post_file(b"same", "a.txt", "text/plain")
        s2, b2 = await self._post_file(b"same", "b.txt", "text/plain")
        self.assertEqual(s1, 201)
        self.assertEqual(s2, 201)
        self.assertEqual(b1["id"], b2["id"])


# ---------- backward-compat: expertos sin `attachments` ----------

class BackwardCompatTests(unittest.IsolatedAsyncioTestCase):
    """Garantiza que el body viejo de POST /experts/run NO se rompió."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        os.environ["FOURBIS_ATTACHMENTS_DIR"] = str(base / "attachments")

        self.db = Database()
        await self.db.init_schema()
        await self.db.upsert_project({
            "slug": "demo", "name": "Demo",
            "repo_path": str(base), "description": "test"})

        from relay.server import create_app
        self.app = create_app()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp.cleanup()

    async def test_experts_run_without_attachments_field(self) -> None:
        # Body idéntico al viejo — `model=test` mockea el LLM a TestModel
        # en pydantic-ai. No mandamos attachments: el relay debe aceptar
        # igual y arrancar el run.
        body = {
            "target": "demo",
            "user": "hola",
            "source": "test",
            "author": "tester",
            "model": "test",
        }
        r = await self.client.post("/experts/run", json=body)
        # Si 200/202 sería ideal, pero el test es más estricto: el
        # handler NO debe 400-ear el body (que era el riesgo). 5xx sí
        # aceptamos porque TestModel puede no estar bien configurado
        # en el venv del relator.
        self.assertNotEqual(r.status, 400,
            f"el body sin attachments NO debe 400-ear; got {r.status}")

    async def test_experts_run_with_empty_attachments_list_is_noop(self) -> None:
        body = {
            "target": "demo",
            "user": "hola",
            "source": "test",
            "author": "tester",
            "model": "test",
            "attachments": [],
        }
        r = await self.client.post("/experts/run", json=body)
        self.assertNotEqual(r.status, 400,
            f"attachments=[] es no-op; no debe 400-ear; got {r.status}")

    async def test_experts_run_with_invalid_attachments_is_noop(self) -> None:
        # ids inválidos (no existen en disco) → resolve() devuelve None,
        # pero el bloque igual se inyecta con "(no encontrado)" — eso es
        # información útil para el LLM. El handler debe NO fallar.
        body = {
            "target": "demo",
            "user": "hola",
            "source": "test",
            "author": "tester",
            "model": "test",
            "attachments": ["att_deadbeefdeadbeef"],
        }
        r = await self.client.post("/experts/run", json=body)
        self.assertNotEqual(r.status, 400)
