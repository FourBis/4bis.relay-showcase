"""El store de adjuntos: raíz del sandbox, pero de solo lectura (2026-08-26).

Contexto. El commit `2dea880` sumó el directorio de adjuntos como raíz
extra del sandbox para que el experto pueda abrir un .xlsx o un .zip con
sus propias tools. La intención es correcta y esto no la toca. Lo que sí
toca son dos consecuencias que venían de arrastre:

1. `attachments_dir()` devolvía una ruta RELATIVA, o sea anclada al CWD
   del proceso. Mientras solo era "dónde guardo", daba igual; desde que
   es una raíz del sandbox, es un permiso — y un permiso que se mueve
   con el CWD no se puede razonar.

2. La raíz entraba con permiso de ESCRITURA. El store está indexado por
   sha256 y lo comparten todas las conversaciones y proyectos, así que
   sobrescribir un archivo rompe la correspondencia id↔contenido para
   cualquier chat que lo cite, sin ningún error visible.

Nota de encuadre, para que nadie lea de más: `Permisos` dice de sí mismo
que es una barrera contra el ACCIDENTE y no de seguridad, porque el run
tiene `shell` y con eso llega a todo el disco igual. Esto no cambia ese
techo. Lo que cambia es que el prompt ahora le NOMBRA el directorio al
modelo, así que el accidente pasó de improbable a probable, que es
exactamente lo que esta caja existe para evitar.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from relay import attachments as att  # noqa: E402
from relay.files import Permisos, SinPermiso, escribir, editar, mover  # noqa: E402


# --- 1. la ruta es absoluta -------------------------------------------

def test_attachments_dir_es_absoluta(monkeypatch):
    monkeypatch.delenv("FOURBIS_ATTACHMENTS_DIR", raising=False)
    assert att.attachments_dir().is_absolute()


def test_attachments_dir_no_depende_del_cwd(monkeypatch, tmp_path):
    """El bug original: cambiaba de lugar según desde dónde arrancaras."""
    monkeypatch.delenv("FOURBIS_ATTACHMENTS_DIR", raising=False)
    desde_aca = att.attachments_dir()
    monkeypatch.chdir(tmp_path)
    assert att.attachments_dir() == desde_aca


def test_env_relativo_tambien_queda_absoluto(monkeypatch):
    monkeypatch.setenv("FOURBIS_ATTACHMENTS_DIR", "state/otro")
    assert att.attachments_dir().is_absolute()


# --- 2. se lee pero no se escribe -------------------------------------

@pytest.fixture
def escenario(tmp_path):
    """Un repo de trabajo y un store de adjuntos afuera, como en producción."""
    repo = tmp_path / "repo"; repo.mkdir()
    store = tmp_path / "store"; store.mkdir()
    adjunto = store / "aaaa1111.xlsx"
    adjunto.write_text("planilla de un cliente")
    perm = Permisos.para(str(repo), extras=[str(store)],
                         solo_lectura=[str(store)])
    return perm, repo, store, adjunto


def test_el_adjunto_se_alcanza(escenario):
    """No rompimos la intención del commit: el experto llega al archivo."""
    perm, _repo, _store, adjunto = escenario
    assert perm.base_de(adjunto) is not None
    assert perm.escribir is True          # el run NO es read_only…


def test_no_se_puede_sobrescribir(escenario):
    """…y aun así el store no se toca."""
    perm, _repo, _store, adjunto = escenario
    with pytest.raises(SinPermiso):
        escribir(perm, str(adjunto), "pisado")
    assert adjunto.read_text() == "planilla de un cliente"


def test_no_se_puede_editar(escenario):
    perm, _repo, _store, adjunto = escenario
    with pytest.raises(SinPermiso):
        editar(perm, str(adjunto), "planilla", "otra cosa")


def test_no_se_puede_mover_ni_hacia_adentro_ni_hacia_afuera(escenario):
    """Los dos extremos: sacar el adjunto del store y meter algo adentro."""
    perm, repo, store, adjunto = escenario
    propio = repo / "mio.txt"
    propio.write_text("mio")
    with pytest.raises(SinPermiso):        # sacarlo de ahí
        mover(perm, str(adjunto), str(repo / "robado.xlsx"))
    with pytest.raises(SinPermiso):        # o plantar algo adentro
        mover(perm, str(propio), str(store / "bbbb2222.txt"))


def test_el_repo_propio_sigue_escribiendose(escenario):
    """El guard es por raíz, no un read_only encubierto."""
    perm, repo, _store, _adjunto = escenario
    escribir(perm, str(repo / "nuevo.txt"), "contenido")
    assert (repo / "nuevo.txt").read_text() == "contenido"


def test_sin_solo_lectura_nada_cambia(tmp_path):
    """Back-compat: un Permisos que no declara raíces RO se comporta igual."""
    repo = tmp_path / "r"; repo.mkdir()
    otro = tmp_path / "o"; otro.mkdir()
    perm = Permisos.para(str(repo), extras=[str(otro)])
    assert perm.solo_lectura == ()
    escribir(perm, str(otro / "x.txt"), "va")
    assert (otro / "x.txt").read_text() == "va"
