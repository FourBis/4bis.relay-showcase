"""Regresión: la salida de un shell de Windows no se decodifica en utf-8.

Entre el 2 y el 5 de septiembre de 2026 se arreglaron seis bugs del
harness (commits `fc972e8` y `5ff3b02`), todos con el mismo patrón: el
modelo hacía todo bien y el harness le devolvía un error o un resultado
corrompido igual. Este archivo fija UNO de esos arreglos, el único que
había quedado sin cobertura de función pura.

`_decodificar` era `buf.decode("utf-8", errors="replace")` a secas. Los
shells de Windows no escriben utf-8 al pipe: escriben en el codepage de
consola, medido en 850 en esta máquina. Con utf-8 cada tilde llegaba
mutilada —en proyectos que son todos en español— y encima tapaba el
error nativo que el modelo necesitaba leer.

`locale.getpreferredencoding()` NO sirve como reemplazo: da cp1252, que
decodifica sin lanzar excepción y devuelve texto MAL. Por eso
`_codepage_consola` consulta `GetConsoleOutputCP()` y no el locale, y
por eso hay un test que lo verifica.

Los otros dos arreglos de la tanda —`_rutas_para_bash` y la contención
de `_resolver_cwd`— ya están cubiertos en `tests/test_shell_y_preguntas.py`
(`test_la_ruta_windows_sobrevive_al_ruteo_a_bash`,
`test_cwd_relativo_no_puede_salirse_del_repo` y vecinos), y ahí la
cobertura es más fuerte que acá: prueban `build_argv` de punta a punta,
o sea la decisión de ruteo Y la conversión, no la función aislada.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_shell_decodificacion.py -q
"""
from __future__ import annotations

import codecs
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from relay import shell  # noqa: E402


# ---------------------------------------------------------------------------
# 2. Decodificación de la salida — el agujero real: sin esto, la única
# cobertura era vía subprocess real y dependía del codepage de la máquina.
# ---------------------------------------------------------------------------

#: Los bytes EXACTOS del docstring de `_codepage_consola`, medidos el
#: 2026-09-04 en una máquina con `GetConsoleOutputCP() == 850`.
_BYTES_MEDIDOS = b"l\xa1nea t\x82rmino"


def test_decodifica_los_bytes_medidos_con_cp850(monkeypatch):
    """Reproduce el ejemplo exacto del docstring de `_codepage_consola`.

    Antes del fix, `_decodificar` era `buf.decode("utf-8",
    errors="replace")` a secas. Esos bytes no son utf-8 válido, así que
    el resultado real era "l�nea t�rmino" — cada tilde mutilada, en
    proyectos que son todos en español, tapando además el error nativo
    que el modelo necesitaba leer.

    Se mockea `_codepage_consola` (en vez de confiar en el codepage real
    de la máquina que corre el test) para que el test sea determinístico
    en cualquier entorno, incluido CI sin consola adjunta.
    """
    monkeypatch.setattr(shell, "_codepage_consola", lambda: "cp850")
    assert shell._decodificar(_BYTES_MEDIDOS) == "línea término"


def test_decodifica_prioriza_utf8_sobre_el_codepage(monkeypatch):
    """utf-8 tiene que ganar cuando los bytes SÍ son utf-8 válido.

    Casi todo lo que corre en el harness (git, node, python, dotnet)
    emite utf-8; el codepage de consola es el plan B. Si una regresión
    invirtiera el orden (probar el codepage primero), un byte que cae
    dentro del rango válido de cp850 pero forma parte de una secuencia
    utf-8 legítima se leería mal. Se mockea el codepage a `cp1252` a
    propósito: es de un byte y DECODIFICA cualquier cosa sin lanzar
    error, solo que distinto ("l�\xadnea término" en vez de "línea
    término") — si `_decodificar` alguna vez probara el codepage antes
    que utf-8, este mock no fallaría con una excepción que se salte al
    plan B: produciría en silencio el texto mutilado, y ESO es lo que
    el assert atrapa. Con `ascii` (que sí lanza ante cualquier byte no
    ascii) el test pasaría igual sin importar el orden, y no serviría.
    """
    texto = "línea término"
    monkeypatch.setattr(shell, "_codepage_consola", lambda: "cp1252")
    assert shell._decodificar(texto.encode("utf-8")) == texto


def test_decodifica_no_explota_si_nada_entiende_los_bytes(monkeypatch):
    """Ni utf-8 ni el codepage decodifican: no tiene que lanzar.

    `_decodificar` tiene un tercer nivel (`errors="replace"`) para el
    caso en que ni el plan A ni el plan B alcanzan. Sin esa red, unos
    bytes verdaderamente ilegibles tirarían una excepción que se lleva
    puesto el comando entero en vez de devolver una salida (aunque sea
    parcialmente mutilada) que el modelo pueda leer.
    """
    monkeypatch.setattr(shell, "_codepage_consola", lambda: "ascii")
    # 0xFF es inválido tanto en utf-8 como en ascii.
    resultado = shell._decodificar(b"antes \xff despues")
    assert "antes" in resultado and "despues" in resultado


@pytest.mark.skipif(not shell.IS_WINDOWS, reason="API de consola de Windows")
def test_codepage_consola_lee_getconsoleoutputcp_no_el_locale(monkeypatch):
    """El bug de fondo: `locale.getpreferredencoding()` da cp1252 en esta
    máquina y decodifica MAL (ver el ejemplo del propio docstring). El
    fix tiene que consultar la API de consola de Windows
    (`GetConsoleOutputCP`), no el locale de Python. Se mockea esa API al
    valor medido (850) y se limpia la cache de `lru_cache` para que el
    mock realmente se ejercite.
    """
    import ctypes

    shell._codepage_consola.cache_clear()
    monkeypatch.setattr(
        ctypes.windll.kernel32, "GetConsoleOutputCP", lambda: 850)
    try:
        assert shell._codepage_consola() == "cp850"
    finally:
        shell._codepage_consola.cache_clear()


@pytest.mark.skipif(not shell.IS_WINDOWS, reason="API de consola de Windows")
def test_codepage_consola_usa_oemcp_sin_consola_adjunta(monkeypatch):
    """Sin consola adjunta (servicio, proceso sin ventana)
    `GetConsoleOutputCP` devuelve 0 — el docstring lo dice explícito.
    Ahí tiene que caer al codepage OEM del sistema y no quedarse con un
    "cp0" inválido que `codecs.lookup` no reconoce.
    """
    import ctypes

    shell._codepage_consola.cache_clear()
    monkeypatch.setattr(
        ctypes.windll.kernel32, "GetConsoleOutputCP", lambda: 0)
    monkeypatch.setattr(
        ctypes.windll.kernel32, "GetOEMCP", lambda: 850)
    try:
        cp = shell._codepage_consola()
        assert cp == "cp850"
        codecs.lookup(cp)  # no tiene que lanzar LookupError
    finally:
        shell._codepage_consola.cache_clear()
