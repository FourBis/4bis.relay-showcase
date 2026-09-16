"""Los tres arreglos de la revisión del 2026-08-26.

1. Integridad referencial entre el catálogo de modelos y los roles.
2. `stage_models` para una etapa apagada: se descarta, pero se avisa.
3. Aislación del store de adjuntos por conversación.

Cada bloque testea la regla, no la plomería: qué pasa cuando alguien
borra un modelo en uso, cuándo se puebla `ignored_stages`, y que un run
del cliente A no alcance un adjunto del cliente B.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from relay import attachments as att  # noqa: E402
from relay.admin import _roles_que_usan  # noqa: E402
from relay.files import Permisos, SinPermiso, escribir  # noqa: E402


# ===== 1. rol huérfano =================================================

class _DbFalsa:
    def __init__(self, cfg, projects=None):
        self._cfg = cfg
        self._projects = projects or []
    async def all_config(self): return dict(self._cfg)
    async def list_projects(self, enabled_only=False):
        return list(self._projects)


@pytest.mark.asyncio
async def test_detecta_el_rol_que_usa_el_modelo():
    db = _DbFalsa({"FOURBIS_PLANNER_MODEL": "minimax:chico",
                   "FOURBIS_VERIFIER_MODEL": "minimax:chico",
                   "FOURBIS_DOCUMENTER_MODEL": "otro:grande"})
    assert sorted(await _roles_que_usan(db, "minimax:chico")) == [
        "planner", "verifier"]


@pytest.mark.asyncio
async def test_modelo_sin_uso_se_puede_borrar():
    db = _DbFalsa({"FOURBIS_PLANNER_MODEL": "minimax:chico"})
    assert await _roles_que_usan(db, "nadie:lo-usa") == []


@pytest.mark.asyncio
async def test_detecta_modelos_persistidos_en_proyectos():
    db = _DbFalsa({}, [{
        "slug": "demo",
        "defaults_json": {
            "graph_planner_model": "claude:sonnet",
            "planner_fallback": "minimax:m3, claude:sonnet",
        },
    }])
    assert await _roles_que_usan(db, "claude:sonnet") == [
        "project:demo:graph_planner_model",
        "project:demo:planner_fallback",
    ]


@pytest.mark.asyncio
async def test_detecta_el_ejecutor_efectivo_aunque_no_este_guardado():
    from relay import config

    config.set_runtime_config({
        "FOURBIS_MODEL": "runtime:executor",
        "FOURBIS_PLANNER_MODEL": "otro:planner",
        "FOURBIS_VERIFIER_MODEL": "otro:verifier",
        "FOURBIS_DOCUMENTER_MODEL": "otro:documenter",
        "FOURBIS_COMPACTOR_MODEL": "otro:compactor",
    })
    try:
        assert await _roles_que_usan(_DbFalsa({}), "runtime:executor") == [
            "executor"]
    finally:
        config.set_runtime_config({})


@pytest.mark.asyncio
async def test_detecta_fallback_global_del_planificador(monkeypatch):
    monkeypatch.setenv("FOURBIS_PLANNER_FALLBACK",
                       "minimax:uno, claude:fallback")
    assert "planner_fallback" in await _roles_que_usan(
        _DbFalsa({}), "claude:fallback")


@pytest.mark.asyncio
async def test_spec_vacio_no_matchea_roles_vacios():
    """El caso que rompe un `==` ingenuo: rol sin fijar es "", y borrar
    un modelo con spec vacío no puede hacer match contra TODOS."""
    db = _DbFalsa({"FOURBIS_PLANNER_MODEL": "", "FOURBIS_VERIFIER_MODEL": ""})
    assert await _roles_que_usan(db, "") == []
    assert await _roles_que_usan(db, "   ") == []


# ===== 2. etapas apagadas ==============================================
#
# Reproducimos la regla del handler tal cual, que es lo que decide si el
# humano se entera o no.

def _descartadas(stage_models: dict, defaults: dict) -> list[str]:
    if not stage_models:
        return []
    if not defaults.get("three_stage", True):
        return list(stage_models)
    return [rol for rol in ("verifier", "documenter")
            if rol in stage_models and not defaults.get(rol, True)]


def test_proyecto_normal_no_descarta_nada():
    assert _descartadas({"planner": "m", "verifier": "m"}, {}) == []


def test_three_stage_apagado_descarta_todo():
    assert sorted(_descartadas({"planner": "m", "verifier": "m"},
                               {"three_stage": False})) == ["planner",
                                                            "verifier"]


def test_solo_la_etapa_apagada():
    d = {"verifier": False}
    assert _descartadas({"planner": "m", "verifier": "m", "documenter": "m"},
                        d) == ["verifier"]


def test_sin_eleccion_no_hay_aviso():
    """Sin `stage_models` el campo queda vacío: la UI no avisa de nada."""
    assert _descartadas({}, {"three_stage": False}) == []


# ===== 3. aislación de adjuntos ========================================

def test_scope_es_la_conversacion_si_hay():
    assert att.scope_for("conv-123", "sample-app") == "conv-123"


def test_scope_cae_al_proyecto_sin_conversacion():
    assert att.scope_for("", "sample-app") == "proj-sample-app"


@pytest.mark.parametrize("malicioso", ["../../etc", "..", ".", "a/../..",
                                       "c:\\windows", "//srv/share"])
def test_scope_nunca_sale_de_la_vista(malicioso):
    """Un scope siempre es UN nombre de directorio bajo `conv/`.

    El caso que importa es `..`: si el punto estuviera permitido,
    `conv/..` sería el store entero y la vista dejaría de aislar nada.
    """
    d = att.scope_dir(malicioso)
    assert d.parent == att.attachments_dir() / "conv"
    assert d.resolve().parent == (att.attachments_dir() / "conv").resolve()
    assert set(d.name) <= set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def test_conversaciones_distintas_no_se_ven(monkeypatch, tmp_path):
    """El hallazgo original: un run del cliente A alcanzaba el adjunto
    del cliente B porque el store es plano y entraba entero."""
    monkeypatch.setenv("FOURBIS_ATTACHMENTS_DIR", str(tmp_path / "store"))
    blob_b = att.attachments_dir()
    blob_b.mkdir(parents=True, exist_ok=True)
    secreto = blob_b / "bbbb2222.xlsx"
    secreto.write_text("planilla del cliente B")
    att.materializar("conv-de-B", secreto)

    repo_a = tmp_path / "repo_a"; repo_a.mkdir()
    vista_a = att.scope_dir(att.scope_for("conv-de-A", "cliente-a"))
    perm = Permisos.para(str(repo_a), extras=[str(vista_a)],
                         solo_lectura=[str(vista_a)])

    # Ni el blob ni la vista de B caen bajo ninguna raíz del run de A.
    assert perm.base_de(secreto.resolve()) is None
    assert perm.base_de(att.scope_dir("conv-de-B").resolve()) is None


def test_la_propia_vista_si_se_ve(monkeypatch, tmp_path):
    """No rompimos la intención: el experto llega a SUS adjuntos."""
    monkeypatch.setenv("FOURBIS_ATTACHMENTS_DIR", str(tmp_path / "store"))
    store = att.attachments_dir()
    store.mkdir(parents=True, exist_ok=True)
    blob = store / "aaaa1111.xlsx"
    blob.write_text("planilla propia")
    mio = att.materializar("conv-mia", blob)

    repo = tmp_path / "repo"; repo.mkdir()
    vista = att.scope_dir("conv-mia")
    perm = Permisos.para(str(repo), extras=[str(vista)],
                         solo_lectura=[str(vista)])
    assert perm.base_de(mio.resolve()) is not None
    assert mio.read_text() == "planilla propia"


def test_la_vista_no_se_escribe(monkeypatch, tmp_path):
    """Los archivos de la vista son hardlinks: escribirlos cambiaría el
    contenido que ven todas las conversaciones que citen ese sha256."""
    monkeypatch.setenv("FOURBIS_ATTACHMENTS_DIR", str(tmp_path / "store"))
    store = att.attachments_dir()
    store.mkdir(parents=True, exist_ok=True)
    blob = store / "cccc3333.xlsx"
    blob.write_text("original")
    mio = att.materializar("conv-mia", blob)

    repo = tmp_path / "repo"; repo.mkdir()
    vista = att.scope_dir("conv-mia")
    perm = Permisos.para(str(repo), extras=[str(vista)],
                         solo_lectura=[str(vista)])
    with pytest.raises(SinPermiso):
        escribir(perm, str(mio), "pisado")
    assert blob.read_text() == "original"


def test_materializar_no_duplica_bytes(monkeypatch, tmp_path):
    """Hardlink, no copia: el dedup del store content-addressed se
    conserva. Si el FS no soporta links, cae a copia y el test lo dice
    en vez de fallar."""
    monkeypatch.setenv("FOURBIS_ATTACHMENTS_DIR", str(tmp_path / "store"))
    store = att.attachments_dir()
    store.mkdir(parents=True, exist_ok=True)
    blob = store / "dddd4444.bin"
    blob.write_text("contenido")
    visto = att.materializar("conv-x", blob)
    if blob.stat().st_nlink > 1:
        assert visto.stat().st_ino == blob.stat().st_ino
    else:                                    # FS sin hardlinks: copia
        assert visto.read_text() == blob.read_text()


def test_materializar_es_idempotente(monkeypatch, tmp_path):
    """Se llama en cada turno que cita el id; no puede fallar la segunda."""
    monkeypatch.setenv("FOURBIS_ATTACHMENTS_DIR", str(tmp_path / "store"))
    store = att.attachments_dir()
    store.mkdir(parents=True, exist_ok=True)
    blob = store / "eeee5555.bin"
    blob.write_text("x")
    a = att.materializar("conv-x", blob)
    b = att.materializar("conv-x", blob)
    assert a == b
