from relay import expert_models, expert_runner
"""Opt-in: RELAY_TEST_PLAYWRIGHT_MCP=<cli.js instalado>, Chrome local.

Usa el MCP de Node real, perfil aislado, HTTP y archivos temporales. El
FunctionModel observa los bytes que recibe el SDK; no simula inferencia visual.
"""
import copy
import os
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic_ai.messages import BinaryContent, ModelResponse, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from relay import attachments, experts, server
from relay.db import Database


@pytest.mark.skipif(not os.environ.get("RELAY_TEST_PLAYWRIGHT_MCP"),
                    reason="requiere CLI Playwright MCP instalado y Chrome local")
async def test_browser_capture_reaches_model_and_user(tmp_path, monkeypatch):
    cli = Path(os.environ["RELAY_TEST_PLAYWRIGHT_MCP"]).resolve()
    assert cli.is_file()
    monkeypatch.setenv("FOURBIS_ATTACHMENTS_DIR", str(tmp_path / "attachments"))
    db = Database(path=tmp_path / "relay.db")
    await db.init_schema()
    await db.upsert_project(dict(slug="browser-test", name="Browser test", repo_path=str(tmp_path)))
    await db.upsert_mcp_server(dict(name="browser-test", capability="browser", enabled=True,
        command="node", args=[str(cli), "--headless", "--isolated", "--browser", "chrome"],
        install_dir=str(cli.parent), env={"PLAYWRIGHT_MCP_OUTPUT_DIR": str(tmp_path)}))
    app = web.Application()

    async def page(request):
        return web.Response(content_type="text/html", text='''<!doctype html>
          <html lang="es"><title>Prueba aislada</title><label>Nombre <input id="name"></label>
          <button onclick="document.querySelector('output').textContent='Hola '+document.querySelector('input').value">Saludar</button>
          <output aria-live="polite"></output></html>''')

    app.router.add_get("/", page)
    app.router.add_get("/attachments/{attach_id}", server.discord_attachments_download)
    named = tmp_path / "named.png"
    received = []
    model_history = []
    async with TestClient(TestServer(app)) as client:
        steps = [("browser_navigate", {"url": str(client.make_url("/"))}),
                 ("browser_type", {"target": "#name", "text": "Relay"}),
                 ("browser_click", {"target": "button"}),
                 ("browser_snapshot", {}),
                 ("browser_take_screenshot", {"type": "png", "fullPage": True}),
                 ("browser_take_screenshot", {"type": "png", "filename": str(named)}),
                 ("read_image", {"path": str(named)})]

        def model(messages, info):
            parts = [p for m in messages for p in m.parts]
            assert not any(isinstance(p, RetryPromptPart) for p in parts), str(parts)[-2000:]
            returns = [p for p in parts if isinstance(p, ToolReturnPart)]
            images = [x for p in parts if isinstance(p, (UserPromptPart, ToolReturnPart)) and isinstance(p.content, list)
                      for x in p.content if isinstance(x, BinaryContent)]
            if len(returns) == 4:
                assert "Hola Relay" in str(returns[-1].content)
            if len(returns) == 5:
                assert len(images) == 1, "MCP sin filename debe entregar bytes al modelo"
            if len(returns) == 6:
                assert named.read_bytes().startswith(b"\x89PNG")
                assert len(images) == 1, "MCP con filename devuelve texto, no otra imagen"
            if len(returns) < len(steps):
                name, args = steps[len(returns)]
                return ModelResponse(parts=[ToolCallPart(name, args)])
            assert len(images) == 2, "read_image debe entregar los bytes de la captura nombrada"
            received.extend(x.data for x in images)
            model_history.extend(copy.deepcopy(messages))
            return ModelResponse(parts=[TextPart("Flujo comprobado.")])  # sin citar ids

        monkeypatch.setattr(expert_models, "build_model", lambda spec: FunctionModel(model))
        result = await expert_runner.run_expert(await db.get_project("browser-test"), "Prueba aislada",
                                         db=db, model_override="function", mcp_with=["browser-test"])
        assert result["content"].startswith("Flujo comprobado."), result["content"]
        assert result["tool_calls"] == len(steps)
        assert received and result["image_artifacts"]
        # El adaptador usado por los endpoints compatibles envía data URLs,
        # no el repr de BinaryContent. No hace una llamada de red.
        adapter = OpenAIChatModel("test", provider=OpenAIProvider(api_key="test-only"))
        wire = await adapter._map_messages(model_history, ModelRequestParameters())
        wire_images = [p["image_url"]["url"] for m in wire if isinstance(m.get("content"), list)
                       for p in m["content"] if p.get("type") == "image_url"]
        assert len(wire_images) == 2
        assert all(url.startswith("data:image/png;base64,") for url in wire_images)
        for aid in result["image_artifacts"]:
            assert f"/attachments/{aid}" in result["content"], "entrega no depende del texto del LLM"
            response = await client.get(f"/attachments/{aid}")
            assert response.status == 200
            assert response.content_type == "image/png"
            assert await response.read() in received
            assert attachments.resolve(aid).read_bytes() in received
