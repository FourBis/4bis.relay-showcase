"""Imágenes contra modelos que no las ven (2026-08-18).

MiniMax M3 ve imágenes; el nemotron servido por los NIM gratis de NVIDIA
no. Antes de esto la imagen viajaba igual y el humano se comía un error
del provider —o peor, una respuesta segura sobre algo que el modelo
nunca vio— después de esperar el run entero.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

import json

import pytest

from relay import experts


@pytest.fixture(autouse=True)
def catalogo_medido():
    """Los cuatro medidos a mano, como los siembra el schema."""
    experts.load_catalog([
        {"spec": "minimax:MiniMax-M3", "vision": 1, "enabled": 1},
        {"spec": "nvidia:minimaxai/minimax-m3", "vision": 1, "enabled": 1},
        {"spec": "nvidia:z-ai/glm-5.2", "vision": 0, "enabled": 1},
        {"spec": "nvidia:nvidia/nemotron-3-ultra-550b-a55b",
         "vision": 0, "enabled": 1},
    ])
    yield
    experts.load_catalog([])


def test_minimax_ve_imagenes():
    assert experts.has_vision("minimax:MiniMax-M3") is True


def test_nemotron_no_ve_imagenes():
    assert experts.has_vision(
        "nvidia:nvidia/nemotron-3-ultra-550b-a55b") is False


def test_glm_no_ve_imagenes():
    """El peligroso: acepta el payload multimodal sin error y contesta
    igual. Probado el 2026-08-18 — con un PNG de un color liso y la
    consigna de decir el color, contesta NO_VEO."""
    assert experts.has_vision("nvidia:z-ai/glm-5.2") is False


def test_la_vision_es_del_endpoint_no_del_modelo():
    """Los mismos pesos servidos en dos lados no tienen por qué
    comportarse igual — por eso la lista es a mano y no un regex sobre
    el nombre. Acá dan lo mismo, y nemotron (mismo provider) no."""
    assert experts.has_vision("minimax:MiniMax-M3")
    assert experts.has_vision("nvidia:minimaxai/minimax-m3")
    assert not experts.has_vision("nvidia:nvidia/nemotron-3-ultra-550b-a55b")


def test_modelo_desconocido_se_asume_con_vision():
    """Solo cortamos cuando SABEMOS que no ve. Asumir que no ve haría
    que un modelo nuevo se tragara la imagen en silencio."""
    assert experts.has_vision("openai:gpt-5") is True
    assert experts.has_vision("") is True


def test_sin_medir_se_deja_pasar():
    """Importar los 102 de NVIDIA mete 102 filas en vision=NULL. Tratar
    NULL como ciego cortaría runs que hoy andan; el dato honesto es
    "nadie lo probó", y ante la duda no se bloquea."""
    experts.load_catalog([
        {"spec": "nvidia:recien/importado", "vision": None, "enabled": 1}])
    assert experts.has_vision("nvidia:recien/importado") is True


def test_el_seed_trae_los_medidos():
    """El seed es lo único con visión medida a mano; si alguien lo
    rompe, el guard se queda sin nada que ofrecer en el mensaje."""
    from relay import db as db_mod
    por_spec = {m["spec"]: m for m in db_mod._MODELS_SEED}
    assert por_spec["minimax:MiniMax-M3"]["vision"] == 1
    assert por_spec["nvidia:z-ai/glm-5.2"]["vision"] == 0
    assert por_spec[
        "nvidia:nvidia/nemotron-3-ultra-550b-a55b"]["vision"] == 0
    assert any(m["vision"] == 1 for m in db_mod._MODELS_SEED)


def test_seed_bien_formado():
    """Cada fila tiene que poder armar un modelo Y poder medirse: sin
    base_url no hay ni run ni probe."""
    from relay import db as db_mod
    for m in db_mod._MODELS_SEED:
        assert ":" in m["spec"], m["spec"] + " no tiene provider:modelo"
        assert m["vision"] in (0, 1, None)
        assert m["enabled"] in (0, 1)
        assert m["base_url"], m["spec"] + " sin base_url"
        assert m["api_key_env"], m["spec"] + " sin api_key_env"


def test_la_key_nunca_sale_por_la_api():
    """La tabla la guarda porque así se pidió; eso no es motivo para
    mandarla en cada poll del panel."""
    from relay.db import Database
    fila = {"spec": "x:y", "api_key": "test-key-1234", "enabled": 1}
    salida = Database.mask_key(fila)
    assert "api_key" not in salida
    assert fila["api_key"] not in json.dumps(salida)
    assert salida["api_key_set"] is True
    assert salida["api_key_hint"] == "••••1234"


def test_mask_key_sin_key():
    from relay.db import Database
    salida = Database.mask_key({"spec": "x:y", "api_key": None})
    assert salida["api_key_set"] is False
    assert salida["api_key_hint"] == ""


# ---------- la cascada que mira el guard ----------


def test_override_gana_sobre_el_proyecto():
    proj = {"defaults_json": {"model": "minimax:MiniMax-M3"}}
    assert experts.resolve_model_spec("nvidia:x", proj) == "nvidia:x"


def test_sin_override_manda_el_proyecto():
    proj = {"defaults_json": {"model": "nvidia:nvidia/nemotron-3-ultra-550b-a55b"}}
    assert experts.resolve_model_spec("", proj) == (
        "nvidia:nvidia/nemotron-3-ultra-550b-a55b")


def test_sin_nada_cae_al_global(monkeypatch):
    monkeypatch.setenv("FOURBIS_MODEL", "minimax:MiniMax-M3")
    assert experts.resolve_model_spec("", {}) == "minimax:MiniMax-M3"
    assert experts.resolve_model_spec("", None) == "minimax:MiniMax-M3"


def test_el_guard_mira_el_modelo_del_proyecto_no_solo_el_override():
    """El caso que se escapa si el guard solo mira el body: nadie eligió
    modelo en la UI y el proyecto tiene configurado uno ciego."""
    proj = {"defaults_json": {"model": "nvidia:nvidia/nemotron-3-ultra-550b-a55b"}}
    assert not experts.has_vision(experts.resolve_model_spec("", proj))


# ---------- modelo por etapa (2026-08-18) ----------


def _flag(nombre, valor):
    from relay import admin
    return admin._validar_flag(nombre, valor)


def test_asignar_un_modelo_prendido():
    from relay import admin
    assert "planner_model" in admin._PROJECT_FLAGS
    valor, err = _flag("planner_model", "nvidia:z-ai/glm-5.2")
    assert err == "" and valor == "nvidia:z-ai/glm-5.2"


def test_vacio_vuelve_al_global():
    valor, err = _flag("verifier_model", "")
    assert err == "" and valor is None
    valor, err = _flag("verifier_model", None)
    assert err == "" and valor is None


def test_un_typo_se_rechaza_al_guardar():
    """Sin esto el spec malo se guarda y el proyecto revienta recién en
    el próximo run, con un error del provider que no nombra la causa."""
    valor, err = _flag("documenter_model", "nvidia:no-exsite")
    assert valor is None
    assert "no está en el catálogo" in err


def test_un_modelo_apagado_no_se_puede_asignar():
    """Apagado = no aparece en ningún selector. Dejar asignarlo por API
    haría que la pantalla de Modelos mienta sobre qué está en uso."""
    experts.load_catalog([
        {"spec": "nvidia:apagado/x", "vision": None, "enabled": 0}])
    valor, err = _flag("model", "nvidia:apagado/x")
    assert valor is None and "prendidos" in err


def test_las_cuatro_etapas_tienen_su_flag():
    from relay import admin
    for n in ("model", "planner_model", "verifier_model", "documenter_model"):
        assert admin._PROJECT_FLAGS[n][0] == "model"
        assert n in admin._MODEL_FLAG_GLOBAL


def test_el_default_que_muestra_la_ui_es_el_global_resuelto(monkeypatch):
    """Un select vacío no dice con qué corre. La UI muestra "(el global:
    X)" y ese X sale de acá."""
    from relay import admin
    monkeypatch.setenv("FOURBIS_MODEL", "minimax:MiniMax-M3")
    estado = admin._flags_state({})
    assert estado["model"]["default"] == "minimax:MiniMax-M3"
    assert estado["model"]["valor"] == ""
    assert estado["model"]["explicito"] is False
    puesto = admin._flags_state({"planner_model": "nvidia:z-ai/glm-5.2"})
    assert puesto["planner_model"]["valor"] == "nvidia:z-ai/glm-5.2"
    assert puesto["planner_model"]["explicito"] is True


def test_graph_planner_vacio_hereda_el_planner_del_proyecto():
    from relay import admin

    estado = admin._flags_state({"planner_model": "project:planner"})
    assert estado["graph_planner_model"]["default"] == "project:planner"
    assert estado["graph_planner_model"]["valor"] == ""
    assert estado["graph_planner_model"]["explicito"] is False


def test_un_global_apagado_se_avisa_en_el_boot(monkeypatch, caplog):
    """La contradicción que se encontró viva el 2026-08-18.

    `FOURBIS_DOCUMENTER_MODEL` apuntaba a un spec con `enabled=0`. El
    relay lo usaba igual —`enabled` no gatea `build_model`— pero la
    pantalla Modelos lo mostraba apagado y `_validar_flag` rechazaba
    asignárselo a un proyecto. O sea: el default del relay era algo que
    a vos no te dejaba elegir. Ahora el boot lo nombra.
    """
    import logging

    from relay import config, server

    config.set_runtime_config({
        "FOURBIS_MODEL": "minimax:MiniMax-M3",
        "FOURBIS_DOCUMENTER_MODEL": "nvidia:nvidia/apagado",
        "FOURBIS_PLANNER_MODEL": "minimax:MiniMax-M3",
        "FOURBIS_VERIFIER_MODEL": "minimax:MiniMax-M3",
    })

    with caplog.at_level(logging.WARNING, logger="relay.server"):
        server._avisar_globales_apagados()

    avisos = [r.getMessage() for r in caplog.records]
    assert any("FOURBIS_DOCUMENTER_MODEL" in m and "apagado" in m
               for m in avisos), avisos
    # El que SÍ está prendido no genera ruido: un warning por cada boot
    # sobre algo que está bien enseña a ignorar los warnings.
    assert not any("FOURBIS_MODEL=" in m for m in avisos), avisos


def test_el_aviso_incluye_compactor_y_usa_system_config(monkeypatch, caplog):
    import logging

    from relay import config, server

    monkeypatch.setenv("FOURBIS_COMPACTOR_MODEL", "minimax:MiniMax-M3")
    experts.load_catalog([
        {"spec": "minimax:MiniMax-M3", "enabled": 1},
        {"spec": "nvidia:nvidia/apagado", "enabled": 0},
    ])
    config.set_runtime_config({
        "FOURBIS_COMPACTOR_MODEL": "nvidia:nvidia/apagado"})
    try:
        with caplog.at_level(logging.WARNING, logger="relay.server"):
            server._avisar_globales_apagados()
        avisos = [r.getMessage() for r in caplog.records]
        assert any("FOURBIS_COMPACTOR_MODEL" in m and "apagado" in m
                   for m in avisos), avisos
    finally:
        config.set_runtime_config({})


def test_sin_catalogo_no_avisa_nada():
    """Catálogo vacío (tests, o una DB recién creada) no es motivo para
    llenar el log: no hay con qué contrastar."""
    import logging

    from relay import server

    experts.load_catalog([])
    logger = logging.getLogger("relay.server")
    registros = []
    handler = logging.Handler()
    handler.emit = registros.append
    logger.addHandler(handler)
    try:
        server._avisar_globales_apagados()
    finally:
        logger.removeHandler(handler)
    assert registros == []
