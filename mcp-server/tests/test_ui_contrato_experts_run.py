"""Contrato UI<->API para `POST /experts/run` y `POST /attachments` (2026-09-07).

Bug real que motiva este archivo: `sendCurrentMessage()` en
`admin_static/static/tab-chats.js` acepta a proposito un mensaje que sea
SOLO un adjunto sin texto (comentario explicito en el JS: "Un adjunto
sin texto es un mensaje valido"), pero posteaba `user: text` con el
texto vacio. `POST /experts/run` exige `user` no vacio y devolvia 400
"user requerido (string)": mandar una captura sin escribir nada moria
ahi y el adjunto nunca llegaba. Se arreglo en el cliente (commit
cf14a12): el POST manda un texto de relleno ("Mira el adjunto.") cuando
el campo esta vacio; en pantalla se sigue viendo solo el adjunto.

Nada detectaba esto: el panel tiene 22.535 lineas de JS sin runner, y
los tests de Python existentes (`test_attachments.py`) cubren donde se
guarda el archivo y el sandbox, no el body EXACTO que la UI postea.

Estos tests reproducen el shape LITERAL de cada llamada a `apiRoot(...)`
en `sendCurrentMessage()` y `uploadAttachment()` contra los handlers
reales (sin red, sin LLM: `FOURBIS_MODEL=test` -> TestModel de
pydantic-ai), para que una deriva futura entre panel y servidor la
agarre la suite y no un humano en produccion.

Como correr:
    cd mcp-server
    .venv/Scripts/python.exe -m pytest tests/test_ui_contrato_experts_run.py -q
"""
from __future__ import annotations

import asyncio
import io
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from relay.db import Database
from relay.server import create_app


@pytest.fixture
async def env():
    """Mismo patron que test_experts.py::env: DB + create_app() aislados,
    FOURBIS_MODEL=test para correr sin red ni tokens reales."""
    with tempfile.TemporaryDirectory() as tmp:
        env_vars = {
            "FOURBIS_DB_PATH": str(Path(tmp) / "relay.db"),
            "FOURBIS_CHATS_DIR": str(Path(tmp) / "chats"),
            "FOURBIS_JSONL_DIR": str(Path(tmp) / "jsonl"),
            "FOURBIS_ATTACHMENTS_DIR": str(Path(tmp) / "attachments"),
            "FOURBIS_MODEL": "test",
            "LOG_LEVEL": "WARNING",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            db = Database(path=Path(tmp) / "relay.db")
            await db.init_schema()
            await db.upsert_project({
                "slug": "demo", "name": "Demo", "repo_path": str(Path(tmp)),
                "system_prompt": "sos el experto demo", "mcp_servers": [],
            })
            app = create_app()
            cli = TestClient(TestServer(app))
            await cli.start_server()
            # Operational settings are now read from runtime/SQLite, not env.
            from relay import config
            config.set_runtime_config({"FOURBIS_MODEL": "test"})
            try:
                yield cli, db
            finally:
                await cli.close()


async def _espera_terminal(db: Database, chat_id: str) -> dict:
    """Poll de /chats hasta status terminal (TestModel corre en ms)."""
    for _ in range(100):
        chat = await db.get_chat(chat_id)
        if chat["status"] in ("ok", "error"):
            return chat
        await asyncio.sleep(0.05)
    pytest.fail(f"el run no termino a tiempo, status={chat['status']}")


async def _sube_adjunto(cli: TestClient, content: bytes, filename: str,
                         mimetype: str) -> dict:
    """Replica EXACTA de `uploadAttachment(file)` en tab-chats.js:
    FormData con un unico campo `file`, POST a `/attachments` (el nombre
    canonico que usa la UI desde 2026-07-31 — no el alias
    `/discord/attachments`)."""
    form = aiohttp.FormData()
    form.add_field("file", io.BytesIO(content), filename=filename,
                    content_type=mimetype)
    r = await cli.post("/attachments", data=form, timeout=60)
    assert r.status == 201, f"upload debia dar 201, dio {r.status}"
    return await r.json()


# ---------- POST /attachments: contrato de la respuesta que consume la UI ----------


async def test_upload_attachment_devuelve_las_claves_que_lee_la_ui(env):
    """`uploadAttachment()` lee r.id, r.bytes, r.viewable, r.inline para
    armar la bandeja (`pendingAttachments.push({...})`). Si el handler
    deja de mandar alguna, la bandeja se llena con `undefined` en
    silencio (no hay 4xx que lo delate)."""
    cli, _db = env
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    body = await _sube_adjunto(cli, png, "captura.png", "image/png")
    for clave in ("id", "bytes", "viewable", "inline"):
        assert clave in body, f"falta '{clave}' en la respuesta de /attachments"
    assert body["id"].startswith("att_")
    assert body["viewable"] is True   # es una imagen: la UI pinta "🖼 la ve"


# ---------- POST /experts/run: variantes EXACTAS del body que arma sendCurrentMessage() ----------


async def test_payload_minimo_como_lo_manda_la_ui(env):
    """Envio de texto sin adjuntos ni modelo elegido: las claves fijas
    (`target`, `user`, `conversation`, `source`, `author`) sin ninguna
    de las condicionales."""
    cli, db = env
    conv_id = await db.create_conversation(project_slug="demo")
    r = await cli.post("/experts/run", json={
        "target": "demo", "user": "hola, arregla el bug",
        "conversation": conv_id, "source": "ui", "author": "ui",
    })
    assert r.status == 202, await r.text()
    chat = await _espera_terminal(db, (await r.json())["id"])
    assert chat["status"] == "ok"


async def test_payload_con_model_elegido_en_el_popover(env, monkeypatch):
    """Popover de modelo -> `...(chosenModel ? { model: chosenModel } : {})`.

    Se espia `run_expert_staged` para confirmar que `model` LLEGA como
    `model_override` sin perderse — un assert que solo mirara el status
    code no distingue "se aplico" de "se ignoro y corrio con el default
    del proyecto", que es justo el tipo de deriva silenciosa que
    preocupa acá.
    """
    from relay import experts

    capturado = {}

    async def _spy(project, user, **kwargs):
        capturado["model_override"] = kwargs.get("model_override")
        return {"messages_json": "", "tokens_in": 0, "tokens_out": 0,
                "phase_at_end": "done"}

    monkeypatch.setattr(experts, "run_expert_staged", _spy)

    cli, db = env
    conv_id = await db.create_conversation(project_slug="demo")
    r = await cli.post("/experts/run", json={
        "target": "demo", "user": "hola", "conversation": conv_id,
        "source": "ui", "author": "ui", "model": "un-modelo-cualquiera",
    })
    assert r.status == 202, await r.text()
    chat = await _espera_terminal(db, (await r.json())["id"])
    assert chat["status"] == "ok"
    assert capturado["model_override"] == "un-modelo-cualquiera"


async def test_payload_con_stage_models_elegidos_en_el_popover(env, monkeypatch):
    """Selector por etapa -> `stage_models: {...chosenStageModels}` con
    claves de `ROLES_ETAPA` (planner/verifier/documenter).

    Se espia `experts.run_expert_staged` para verificar que las TRES
    claves lleguen intactas hasta ahi (no solo que el endpoint no
    rebote): con etapas prendidas por default, un handler que solo
    reenvie una de las tres (p.ej. solo `planner`) sigue dando 202 y
    `ignored_stages: []` — un assert que mirara solo el status code no
    hubiera detectado ese drop silencioso.
    """
    from relay import experts

    capturado: dict = {}

    async def _spy(project, user, **kwargs):
        capturado.update(kwargs.get("stage_models") or {})
        return {"messages_json": "", "tokens_in": 0, "tokens_out": 0,
                "phase_at_end": "done"}

    monkeypatch.setattr(experts, "run_expert_staged", _spy)

    cli, db = env
    conv_id = await db.create_conversation(project_slug="demo")
    enviado = {"planner": "test", "verifier": "test", "documenter": "test"}
    r = await cli.post("/experts/run", json={
        "target": "demo", "user": "hola", "conversation": conv_id,
        "source": "ui", "author": "ui", "stage_models": enviado,
    })
    assert r.status == 202, await r.text()
    body = await r.json()
    # Proyecto con three_stage default (True) y las etapas prendidas:
    # nada deberia descartarse en silencio.
    assert body["ignored_stages"] == []
    chat = await _espera_terminal(db, body["id"])
    assert chat["status"] == "ok"
    assert capturado == enviado, (
        f"stage_models llego incompleto/alterado a run_expert_staged: "
        f"mande {enviado}, llego {capturado}")


async def test_payload_con_adjunto_y_texto(env):
    """Camino feliz de un adjunto CON texto: `attachments: [ids]` +
    `user` no vacio."""
    cli, db = env
    conv_id = await db.create_conversation(project_slug="demo")
    subido = await _sube_adjunto(
        cli, b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "captura.png", "image/png")
    r = await cli.post("/experts/run", json={
        "target": "demo", "user": "mira esta captura",
        "conversation": conv_id, "source": "ui", "author": "ui",
        "attachments": [subido["id"]],
    })
    assert r.status == 202, await r.text()
    chat = await _espera_terminal(db, (await r.json())["id"])
    assert chat["status"] == "ok"


async def test_payload_adjunto_sin_texto_usa_el_relleno_de_la_ui(env):
    """EL CASO QUE SE ROMPIO. `sendCurrentMessage()` deja mandar un
    adjunto solo (texto vacio en el composer) pero el POST viaja con
    `user: text || "Mira el adjunto."` (commit cf14a12) — nunca con
    string vacio. Replicamos exactamente esa expresion.

    Prueba de que el test sirve: si se revierte el fix (se vuelve a
    mandar `user: text` crudo, o sea `user: ""` acá) el siguiente test
    (`test_user_vacio_de_verdad_sigue_dando_400`) documenta que el
    servidor rebota con 400 — este test en cambio prueba que CON el
    relleno puesto, el 400 no aparece más.
    """
    cli, db = env
    conv_id = await db.create_conversation(project_slug="demo")
    subido = await _sube_adjunto(
        cli, b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "captura.png", "image/png")
    text = ""  # el composer quedo vacio: el usuario solo pego una captura
    r = await cli.post("/experts/run", json={
        "target": "demo",
        "user": text or "Mira el adjunto.",   # ver tab-chats.js:1659
        "conversation": conv_id, "source": "ui", "author": "ui",
        "attachments": [subido["id"]],
    })
    assert r.status == 202, (
        f"un adjunto sin texto (con el relleno de la UI) NO debe 400ear; "
        f"obtuve {r.status}: {await r.text()}")
    chat = await _espera_terminal(db, (await r.json())["id"])
    assert chat["status"] == "ok"


async def test_user_vacio_de_verdad_sigue_dando_400(env):
    """La causa raiz del bug, pineada del lado servidor: `user=""` (lo
    que la UI mandaba ANTES del fix) sigue dando 400 hoy. Es la razon
    de ser del relleno `|| "Mira el adjunto."` en el cliente — si este
    test alguna vez empieza a dar otra cosa que 400, esa justificacion
    desaparecio y vale la pena revisar si el relleno del lado UI sigue
    haciendo falta.
    """
    cli, db = env
    conv_id = await db.create_conversation(project_slug="demo")
    r = await cli.post("/experts/run", json={
        "target": "demo", "user": "",
        "conversation": conv_id, "source": "ui", "author": "ui",
    })
    assert r.status == 400
    body = await r.json()
    assert "user" in body["error"]
