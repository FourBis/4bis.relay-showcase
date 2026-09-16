"""Tests de expertos (ADR-012): armado de prompt/modelo/toolsets y
el endpoint POST /experts/run end-to-end con TestModel (sin red)."""
from __future__ import annotations

import re
import os
import sys
import json
import tempfile
import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "mcp-server" / "src"))

from aiohttp.test_utils import TestClient, TestServer

from relay import config, experts  # noqa: E402
from relay.db import Database  # noqa: E402
from relay.server import create_app  # noqa: E402


# ---------- unidades ----------


def test_build_model_test():
    from pydantic_ai.models.test import TestModel
    assert isinstance(experts.build_model("test"), TestModel)


def test_build_model_minimax_sin_key():
    with patch.dict(os.environ, {"MINIMAX_API_KEY": ""}):
        with pytest.raises(experts.ModelUnavailable):
            experts.build_model("minimax:MiniMax-M3")


def test_build_model_minimax_con_key():
    with patch.dict(config._runtime, {"MINIMAX_API_KEY": "test-key",
                                      "MINIMAX_BASE_URL": "https://api.minimax.io/v1"}):
        model = experts.build_model("minimax:MiniMax-M3")
        assert model.model_name == "MiniMax-M3"


def test_build_model_passthrough():
    # Un provider desconocido conserva el spec para el SDK consumidor.
    assert experts.build_model("custom:model") == "custom:model"


def test_build_model_openai_uses_configured_credentials():
    with patch.dict(config._runtime, {"OPENAI_API_KEY": "test-key",
                                      "secret:OPENAI_API_KEY": "test-key"}), patch.dict(
        experts._catalog, {"openai:gpt-5": {"api_key_env": "OPENAI_API_KEY"}}
    ):
        assert experts.build_model("openai:gpt-5").model_name == "gpt-5"


def test_structured_output_settings_deepseek_v4():
    """Regresión 2026-07-26: sin esto, todo Agent con `output_type` que
    corriera sobre deepseek-v4 moría con 400 'Thinking mode does not
    support this tool_choice' (el night plan no se generaba nunca)."""
    for spec in ("deepseek-v4-pro", "openai:deepseek-v4-flash", "DeepSeek-V4-Pro"):
        assert experts.structured_output_settings(spec) == {
            "extra_body": {"thinking": {"type": "disabled"}}}, spec
    # El resto de los providers no tiene el bug: no les tocamos nada.
    for spec in ("minimax:MiniMax-M3", "anthropic:claude-opus-5", "test", ""):
        assert experts.structured_output_settings(spec) is None, spec


def test_build_instructions_orden():
    project = {"slug": "inventorydemo", "repo_path": "C:/x/INVENTORYDEMO",
               "system_prompt": "SOS EXPERTO INVENTORYDEMO"}
    out = experts.build_instructions(project, "PONYTAIL", "SKILLS")
    # orden: ponytail → proyecto → skills → workspace
    assert out.index("PONYTAIL") < out.index("SOS EXPERTO INVENTORYDEMO") \
        < out.index("SKILLS") < out.index("## Workspace abierto")
    assert "C:/x/INVENTORYDEMO" in out


def test_build_instructions_partes_vacias():
    project = {"slug": "x", "repo_path": "C:/x", "system_prompt": ""}
    out = experts.build_instructions(project, "", "")
    assert out.startswith("## Workspace abierto")  # sin dobles \n\n huérfanos


def test_build_instructions_trae_la_regla_de_lotes_siempre():
    """La regla de verificar lotes NO puede depender del browser.

    Caso real (chat 9d0fef5e, sample-app): el experto corrió su propio script
    de Playwright, salieron 40 PNG de las cuales 31 eran la misma pantalla
    de /accept-terms, y siguió como si hubiera avanzado. `EVIDENCE_BLOCK`
    tiene la regla equivalente pero se inyecta SOLO con un browser MCP
    adjunto, y ese run no usó el browser. Este bloque va siempre.
    """
    project = {"slug": "x", "repo_path": "C:/x", "system_prompt": ""}
    out = experts.build_instructions(project, "", "")
    assert "## Lotes de artefactos" in out
    # Lo que hace la regla accionable: comparar, no mirar.
    assert "sha256sum" in out or "Get-FileHash" in out
    assert "genera DOS" in out


def test_evidence_block_nombra_tools_que_existen():
    """El prompt del browser tiene que nombrar las tools del browser REAL.

    Ya falló dos veces y siempre igual de caro, porque el síntoma no
    apunta a la causa: el experto llama una tool inexistente, reintenta,
    y el run se lee como "el modelo se equivoca".

      - 2026-08-16: el bloque nombraba las de obscura, ya retirada.
      - 2026-08-19: el "arreglo" de esa vez lo reescribió para
        `mcp_servers/playwright_mcp.py`, que NO es lo que se enchufa —
        la fila del catálogo apunta a `@playwright/mcp` de Microsoft
        (tools `browser_*`) — y encima afirmaba que no había click ni
        form fill, cuando los dos existen. El experto leyó eso y ni
        siquiera pidió el browser: se fue una hora escribiendo
        Playwright a mano por `shell`.

    El guard es doble: los nombres del módulo huérfano no pueden volver,
    y —si el clone está instalado en esta máquina— cada `browser_*` que
    el bloque menciona tiene que existir de verdad en su README.
    """
    bloque = experts.EVIDENCE_BLOCK

    # 1. Los nombres del módulo huérfano no vuelven. Son genéricos a
    #    propósito (`navigate`, `get_text`): con backtick y paréntesis
    #    para no matchear la prosa.
    for muerta in ("`navigate(", "`get_text(", "`get_html(",
                   "`get_title(", "`get_url(", "`screenshot(path)`"):
        assert muerta not in bloque, f"{muerta} no existe en el browser real"

    mencionadas = set(re.findall(r"browser_[a-z_]+", bloque))
    assert mencionadas, "el bloque no nombra ninguna tool del browser"

    # 2. Contraste contra el clone instalado, si está. Se saltea en una
    #    máquina sin el MCP instalado (CI) en vez de fallar: lo que se
    #    verifica acá es la coherencia con ESTA instalación.
    readmes = sorted(
        Path.home().glob(".4bis/mcp-installs/playwright-mcp-*/README.md"))
    if not readmes:
        pytest.skip("playwright-mcp no instalado en esta máquina")
    reales = set(re.findall(r"browser_[a-z_]+", readmes[0].read_text(
        encoding="utf-8", errors="replace")))
    assert mencionadas <= reales, (
        f"el prompt nombra tools que no existen: {sorted(mencionadas - reales)}")


def test_make_toolset_stdio_y_desconocido():
    from relay.mcp_pool import make_toolset

    toolset, transport = make_toolset(
        {"name": "w", "transport": "stdio", "command": "python",
         "args": ["-m", "mcp_wrapper"], "env": {}},
        repo_path="C:/x",
    )
    assert toolset is not None
    assert transport is not None

    with pytest.raises(ValueError, match="carrier-pigeon"):
        make_toolset({"name": "raro", "transport": "carrier-pigeon"},
                     repo_path="C:/x")


# ---------- endpoint /experts/run ----------


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmp:
        env_vars = {
            "STATE_DIR": str(Path(tmp) / "state"),
            "FOURBIS_DB_PATH": str(Path(tmp) / "relay.db"),
            "FOURBIS_CHATS_DIR": str(Path(tmp) / "chats"),
            "FOURBIS_JSONL_DIR": str(Path(tmp) / "jsonl"),
            "FOURBIS_MODEL": "test",
            "LOG_LEVEL": "WARNING",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            db = Database(path=Path(tmp) / "relay.db")
            await db.init_schema()
            config.set_runtime_config({"FOURBIS_MODEL": "test",
                                       "FOURBIS_CHATS_DIR": str(Path(tmp) / "chats"),
                                       "FOURBIS_JSONL_DIR": str(Path(tmp) / "jsonl"),
                                       "FOURBIS_COMPACTOR_MODEL": "test"})
            await db.set_config("FOURBIS_MODEL", "test")
            await db.set_config("FOURBIS_COMPACTOR_MODEL", "test")
            await db.set_config("FOURBIS_CHATS_DIR", str(Path(tmp) / "chats"))
            await db.set_config("FOURBIS_JSONL_DIR", str(Path(tmp) / "jsonl"))
            await db.upsert_project({
                "slug": "demo", "name": "Demo", "repo_path": "C:/x/Demo",
                "system_prompt": "eres el experto demo", "mcp_servers": [],
            })
            app = create_app()
            cli = TestClient(TestServer(app))
            await cli.start_server()
            try:
                yield cli, db
            finally:
                await cli.close()
                config.set_runtime_config({})


async def test_experts_run_ok(env):
    """POST /experts/run ahora es ASYNC (ADR-024): 202 + resultado por /notify.

    Polleamos /chats/{id}/status hasta que termine y validamos el
    resultado persistido en la tabla `chats` + el .md + el jsonl.
    """
    cli, db = env
    r = await cli.post("/experts/run", json={
        "target": "demo", "user": "hola", "source": "test", "author": "usuario-demo-a",
    })
    assert r.status == 202
    body = await r.json()
    chat_id = body["id"]
    assert body["status"] == "running"

    # Poll del chat hasta terminal (TestModel corre en ms).
    for _ in range(100):
        chat = await db.get_chat(chat_id)
        # También se espera el `md_path`: desde c00408f el estado terminal
        # se guarda ANTES de exportar el .md, para que un disco lleno no
        # deje el run sin cerrar. Cortar solo por `status` deja este test
        # leyendo la fila dentro de esa ventana, y `md_path` viene NULL.
        if chat["status"] in ("ok", "error") and chat["md_path"]:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail(f"el run no terminó en 5s, status={chat['status']}")

    assert chat["status"] == "ok"
    assert chat["project_slug"] == "demo"
    # TestModel devuelve un output fijo ("test")
    assert chat["tokens_in"] is not None and chat["tokens_out"] is not None
    # 2026-08-31: la caché tiene que llegar hasta la fila. 0 y no NULL:
    # TestModel no cachea, pero el dato SE MIDIÓ. La distinción es la que
    # decide si `cost_usd` cobra esa entrada a tarifa plena o de caché.
    assert chat["cache_read_tokens"] == 0

    # .md escrito (la verdad está en disco)
    md = Path(chat["md_path"])
    assert md.exists()
    text = md.read_text(encoding="utf-8")
    assert "hola" in text

    # jsonl del target con user + assistant
    jsonl = Path(os.environ["FOURBIS_JSONL_DIR"]) / "demo.jsonl"
    assert jsonl.read_text(encoding="utf-8").count("\n") == 2


async def test_experts_run_proyecto_desconocido(env):
    cli, _ = env
    r = await cli.post("/experts/run", json={"target": "nope", "user": "x"})
    assert r.status == 404
    body = await r.json()
    assert sorted(body["available_projects"]) == sorted(["demo", "notes"])


async def test_experts_run_validacion(env):
    cli, _ = env
    assert (await cli.post("/experts/run", json={"user": "x"})).status == 400
    assert (await cli.post("/experts/run", json={"target": "demo"})).status == 400


async def test_experts_run_model_unavailable(env):
    """Sin MINIMAX_API_KEY el run se kickea igual (202) pero termina en error.

    Polleamos el chat hasta que aparezca `status="error"` con un mensaje
    que mencione la API key faltante. ANTES (pre-ADR-024) el handler
    devolvía 503 inmediato — ya no, ahora el background task es el que
    detecta el ModelUnavailable al armar el modelo.

    Importante: el patch de os.environ tiene que cubrir TODO el ciclo
    del background task (kick + planner + executor + persist). Si el
    `with patch.dict` se cierra antes de que la task corra, el LLM ve
    la key real cargada del .env al import y "responde OK", tapando el
    error que queríamos verificar. Con 3-stage el primer turno (planner)
    ya no es instantáneo como TestModel del legacy, así que el polling
    queda dentro del `with` sin problema.
    """
    cli, db = env
    with patch.dict(os.environ, {"MINIMAX_API_KEY": ""}):
        r = await cli.post("/experts/run", json={
            "target": "demo", "user": "x", "model": "minimax:MiniMax-M3",
        })
        assert r.status == 202
        body = await r.json()
        chat_id = body["id"]

        for _ in range(100):
            chat = await db.get_chat(chat_id)
            if chat["status"] in ("ok", "error"):
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail(f"el run no terminó en 5s, status={chat['status']}")

    assert chat["status"] == "error"
    assert "MINIMAX_API_KEY" in (chat.get("error") or "")


# ---------- capa 3: _slim_history (bug 2026-08-13) ----------


def _hist_dos_turnos():
    """user → [narración+call, narración+call, respuesta] → user → …

    Réplica del hilo a292ca08: en un run agéntico el modelo escribe un
    texto ANTES de cada tool call. Al borrar la call (capa 3) esos textos
    quedan como "anuncio sin ejecución".
    """
    from pydantic_ai.messages import (
        ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart,
        UserPromptPart,
    )
    return [
        ModelRequest(parts=[UserPromptPart(content="arreglá el timeout")]),
        ModelResponse(parts=[TextPart(content="Veo el appsettings:"),
                             ToolCallPart("read_file", {"p": "a.json"},
                                          tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart("read_file", "{...}",
                                           tool_call_id="c1")]),
        ModelResponse(parts=[TextPart(content="Compilo:"),
                             ToolCallPart("run_shell", {"cmd": "build"},
                                          tool_call_id="c2")]),
        ModelRequest(parts=[ToolReturnPart("run_shell", "ok",
                                           tool_call_id="c2")]),
        ModelResponse(parts=[TextPart(content="Listo: era el CTS linkeado.")]),
        ModelRequest(parts=[UserPromptPart(content="y ahora el front")]),
    ]


def test_slim_history_deja_una_respuesta_por_turno():
    from pydantic_ai.messages import (
        ModelResponse, TextPart, ToolCallPart, ToolReturnPart,
    )
    slim = experts._slim_history(_hist_dos_turnos())

    responses = [m for m in slim if isinstance(m, ModelResponse)]
    assert len(responses) == 1, "sobrevivió narración de mitad de run"
    textos = [p.content for m in responses for p in m.parts
              if isinstance(p, TextPart)]
    assert textos == ["Listo: era el CTS linkeado."]
    # y sigue sin viajar el tool spam viejo
    partes = [p for m in slim for p in m.parts]
    assert not any(isinstance(p, (ToolCallPart, ToolReturnPart))
                   for p in partes)


def test_slim_history_no_toca_el_ultimo_turno():
    """El turno vivo viaja intacto: 'continúa' necesita su working set."""
    from pydantic_ai.messages import (
        ModelResponse, TextPart, ToolCallPart,
    )
    hist = _hist_dos_turnos() + [
        ModelResponse(parts=[TextPart(content="Busco axios:"),
                             ToolCallPart("list_dir", {"p": "web"},
                                          tool_call_id="c3")]),
    ]
    slim = experts._slim_history(hist)
    partes = [p for m in slim for p in m.parts]
    assert any(isinstance(p, ToolCallPart) and p.tool_call_id == "c3"
               for p in partes)


def test_slim_history_con_corte_tambien_poda_el_ultimo_turno():
    """El camino de `off_plan`: el working set descarrilado NO se guarda.

    Conservar el último turno vale cuando fue trabajo bueno. `off_plan`
    es el sistema diciendo lo contrario, y guardarlo entero hacía que el
    turno siguiente arrancara leyendo el descarrilamiento (sample-app
    ff494129: 127 KB de `read_file` sobre archivos ajenos al pedido).
    """
    from pydantic_ai.messages import (
        ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart,
        UserPromptPart,
    )
    hist = _hist_dos_turnos() + [
        ModelResponse(parts=[TextPart(content="Busco axios:"),
                             ToolCallPart("list_dir", {"p": "web"},
                                          tool_call_id="c3")]),
        ModelRequest(parts=[ToolReturnPart("list_dir", "web/",
                                           tool_call_id="c3")]),
        ModelResponse(parts=[TextPart(content="Qué se hizo: nada útil.")]),
    ]
    slim = experts._slim_history(hist, corte=len(hist))

    partes = [p for m in slim for p in m.parts]
    assert not any(isinstance(p, (ToolCallPart, ToolReturnPart))
                   for p in partes), "sobrevivió el working set descarrilado"
    # Lo que sí tiene que quedar: el pedido del humano y el cierre de
    # cada turno —uno por turno, igual que siempre—, que es de donde el
    # turno siguiente saca qué pasó. Lo que se cae es la narración de
    # mitad de run ("Veo el appsettings:", "Busco axios:").
    textos = [p.content for p in partes if isinstance(p, TextPart)]
    assert textos == ["Listo: era el CTS linkeado.",
                      "Qué se hizo: nada útil."]
    pedidos = [p.content for p in partes if isinstance(p, UserPromptPart)]
    assert pedidos == ["arreglá el timeout", "y ahora el front"]


# ---------- plan_step_done (Etapa B, P8) ----------
#
# `plan_step_done` vive como closure dentro de `run_expert_staged`:
# no se puede importar como nombre de módulo (igual que `anotar` y
# las otras tools anidadas, que no se testean por import).
# La lógica testeable de P1 vive en `Bitacora.marcar_paso` y ya está
# cubierta por `TestPlanStepDone` en test_bitacora.py (paso fuera de
# rango no tira, paso válido persiste en volcar/cargar). Acá cubrimos
# el contrato end-to-end de P3: lo que el ejecutor va dejando en
# `bitacora.pasos` se serializa a `stages_json["plan_steps_done"]`
# con claves string (json no acepta claves int) y respeta MAX_PASOS.

def test_plan_steps_se_serializan_con_claves_string_y_capean_a_12():
    """P2/P3: Bitacora.volcar() expone `pasos` como dict {str(k): v}.
    El cap retiene los ÚLTIMOS (los recientes son los que importan),
    y se documenta como `BITACORA_MAX_PASOS` para que tests/imports
    no tengan que entrar a la clase.
    """
    from relay.experts import Bitacora, BITACORA_MAX_PASOS

    assert BITACORA_MAX_PASOS == 12  # ponytail: cap se documenta acá.

    bit = Bitacora()
    for i in range(1, 21):  # 20 pasos marcados
        bit.marcar_paso(i, f"hecho {i}")

    raw = bit.volcar()
    dump = json.loads(raw)

    # json-friendly: claves string (json no acepta int)
    pasos = dump["pasos"]
    assert all(isinstance(k, str) for k in pasos)
    assert all(isinstance(v, str) for v in pasos.values())

    # cap: 12 de los 20, reteniendo los últimos (los recientes)
    assert len(pasos) == BITACORA_MAX_PASOS
    assert "9" in pasos and "20" in pasos          # últimos presentes
    assert "1" not in pasos and "8" not in pasos   # primeros se cayeron


def test_plan_steps_se_recuperan_de_bitacora_json():
    """P3: lo que el ejecutor marcó en un turno tiene que sobrevivir
    a un 'continuá' en el turno siguiente — `bitacora_json` se carga
    en cada `run_expert` vía `Bitacora.cargar()`. Verifico round-trip.
    """
    from relay.experts import Bitacora

    bit = Bitacora()
    bit.marcar_paso(2, "modulo 2 ok")
    bit.marcar_paso(4, "modulo 4 ok")

    raw = bit.volcar()
    bit2 = Bitacora.cargar(raw)

    assert bit2.pasos == {2: "modulo 2 ok", 4: "modulo 4 ok"}


# ---------- retomar vs. pedir algo nuevo (2026-08-23) ----------

RETOMA = [
    "continúa", "continua", "Continúa.", "continuá con eso", "sigue",
    "seguí", "segui", "dale, continuá", "ok continua", "sí, seguí",
    "**continúa**", "- sigue con la 2", "retomá donde quedaste",
    # Los que escribe el propio harness al reintentar: son largos, así
    # que no los salva el tope de longitud sino el prefijo.
    "Continúa la tarea. El verificador revisó lo que hiciste y dice que "
    "falta esto: el ejecutor solo completó tres de los cinco módulos y "
    "no generó ninguna captura de pantalla del flujo de reservas.",
    "continúa con la tarea desde donde quedaste; si ya está completa, "
    "responde con el resumen final",
]

ABRE_ALGO_NUEVO = [
    # El pedido que motivó todo esto.
    "Genera un manual de usuario de la aplicación en formato md, con "
    "capturas de pantalla, todos los pasos deben estar documentados.",
    "Ejecuta capturas y documenta cada módulo con pasos y screenshots",
    "listo el docker",
    "¿Puedes decirme en qué punto vamos?",
    "Preparar el entorno local y una cuenta capturable por cada rol",
    # Arranca con un verbo parecido pero es un pedido entero, no un
    # "seguí": lo separa el tope de longitud.
    "Sigue el flujo de checkout de punta a punta y documentá cada "
    "pantalla, cada validación y cada error posible del formulario.",
    "",
]


@pytest.mark.parametrize("texto", RETOMA)
def test_es_continuacion_reconoce_lo_que_retoma(texto):
    assert experts._es_continuacion(texto), texto


@pytest.mark.parametrize("texto", ABRE_ALGO_NUEVO)
def test_es_continuacion_no_se_come_un_pedido_nuevo(texto):
    """El falso positivo es el caro: apaga el grafo y el pedido grande
    se ejecuta igual hasta morir por presupuesto."""
    assert not experts._es_continuacion(texto), texto
