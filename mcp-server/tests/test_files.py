"""Tools de archivo nativas del relay (2026-08-16).

Reemplazan a las del wrapper MCP, que se retiró (docs/WRAPPER.md). Lo
que más importa acá es el **sandbox**, portado del wrapper junto con su
matcher de `.gitignore`.

Una corrección sobre lo que este archivo decía antes: acá se afirmaba
que el sandbox es "un límite de seguridad". **No lo es**, y conviene no
mentirse: el mismo run tiene la tool `shell`, que corre cualquier
comando en cualquier directorio. Lo que el sandbox evita es el
*accidente* —un `write_file("../../otro-repo/x")` mal calculado que pisa
algo que nadie estaba mirando—, no a un modelo que quiera salirse.
Contener eso, con `shell` en la mesa, no es posible; y capar `shell` fue
justo lo que sacamos a pedido.

Sigue valiendo la pena testearlo fuerte: los accidentes son el caso
común, y desde el 2026-08-16 el sandbox además tiene rendijas
configurables (`extras`), que es donde una vuelta de más abre una puerta
que nadie pidió.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from relay import file_tools, files


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hola')\nprint('chau')\n")
    (tmp_path / "README.md").write_text("# demo\n")
    (tmp_path / ".gitignore").write_text("out/\n/build\n**/tmp\n*.secreto\n")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "bundle.js").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("x")
    return tmp_path


@pytest.fixture
def perm(repo):
    return files.Permisos.para(str(repo))


# ---------- 1. el sandbox ----------


def test_ruta_relativa_cae_dentro(perm, repo):
    assert perm.resolve("src/main.py") == (repo / "src" / "main.py").resolve()


def test_absoluta_de_adentro_se_acepta(perm, repo):
    """El modelo a veces pega un path completo sacado de un error."""
    assert perm.resolve(str(repo / "README.md")).name == "README.md"


@pytest.mark.parametrize("escape", [
    "../afuera.txt",
    "../../etc/passwd",
    "src/../../afuera.txt",
    "src/../../../tmp/x",
])
def test_escape_con_dotdot_se_corta(perm, escape):
    """`.resolve()` ANTES de comparar es lo que cierra esto.

    Sin el resolve, `repo/../../etc/passwd` pasa el startswith porque el
    string empieza con la raíz.
    """
    with pytest.raises(files.FueraDelRepo):
        perm.resolve(escape)


def test_absoluta_de_afuera_se_corta(perm):
    with pytest.raises(files.FueraDelRepo):
        perm.resolve("/etc/passwd" if os.name != "nt" else r"C:\Windows\win.ini")


def test_la_raiz_misma_es_valida(perm, repo):
    assert perm.resolve("") == repo.resolve()


def test_rutas_vedadas(repo):
    p = files.Permisos.para(str(repo), vedadas=[".env", "secretos/"])
    (repo / ".env").write_text("KEY=1")
    with pytest.raises(files.SinPermiso):
        p.resolve(".env")
    with pytest.raises(files.SinPermiso):
        p.resolve("secretos/prod.json")
    p.resolve("README.md")      # el resto sigue accesible


# ---------- 2. permisos de escritura ----------


def test_read_only_bloquea_escritura(repo):
    p = files.Permisos.para(str(repo), read_only=True)
    with pytest.raises(files.SinPermiso):
        files.escribir(p, "nuevo.txt", "x")
    with pytest.raises(files.SinPermiso):
        files.editar(p, "README.md", "demo", "otro")
    with pytest.raises(files.SinPermiso):
        files.mover(p, "README.md", "LEEME.md")
    # …pero leer sigue andando: read_only no es "no puedo trabajar".
    assert "demo" in files.leer(p, "README.md")


def test_el_mensaje_de_read_only_dice_como_arreglarlo(repo):
    p = files.Permisos.para(str(repo), read_only=True)
    with pytest.raises(files.SinPermiso) as e:
        files.escribir(p, "x.txt", "y")
    assert "Admin UI" in str(e.value)


# ---------- 3. leer ----------


def test_leer_archivo_entero(perm):
    assert files.leer(perm, "src/main.py").startswith("print('hola')")


def test_leer_por_rango(perm):
    out = files.leer(perm, "src/main.py", desde=2, hasta=2)
    assert "chau" in out and "hola" not in out
    assert "líneas 2-2 de 2" in out


def test_archivo_gigante_dice_como_pedir_menos(perm, repo, monkeypatch):
    """El wrapper fallaba con 'excede 256 KB' y el modelo tenía que
    adivinar. Acá el error trae el rango a pedir."""
    monkeypatch.setattr(files, "MAX_READ_BYTES", 100)
    (repo / "grande.txt").write_text("línea\n" * 500)
    out = files.leer(perm, "grande.txt")
    assert out.startswith("error:")
    assert "desde=1" in out and "hasta=" in out


def test_leer_inexistente(perm):
    assert files.leer(perm, "no-existe.py").startswith("error:")


# ---------- 4. escribir / editar / mover ----------


def test_escribir_crea_y_sobrescribe(perm, repo):
    assert "creado" in files.escribir(perm, "sub/dir/x.txt", "uno")
    assert (repo / "sub" / "dir" / "x.txt").read_text() == "uno"
    assert "sobrescrito" in files.escribir(perm, "sub/dir/x.txt", "dos")


def test_editar_reemplaza_una_vez(perm, repo):
    assert "ok" in files.editar(perm, "src/main.py", "hola", "mundo")
    assert "mundo" in (repo / "src" / "main.py").read_text()


def test_editar_falla_si_el_fragmento_es_ambiguo(perm, repo):
    """Reemplazar todas las ocurrencias es la forma más fácil de romper
    un archivo sin darse cuenta."""
    (repo / "dup.py").write_text("x = 1\nx = 1\n")
    out = files.editar(perm, "dup.py", "x = 1", "x = 2")
    assert "2 veces" in out
    assert (repo / "dup.py").read_text() == "x = 1\nx = 1\n"   # intacto


def test_editar_sin_match_lo_dice(perm):
    out = files.editar(perm, "src/main.py", "no está", "x")
    assert "no encontré" in out


def test_mover_y_sus_guardas(perm, repo):
    assert "ok" in files.mover(perm, "README.md", "docs/LEEME.md")
    assert (repo / "docs" / "LEEME.md").is_file()
    assert "no existe" in files.mover(perm, "fantasma.txt", "x.txt")
    files.escribir(perm, "a.txt", "1")
    files.escribir(perm, "b.txt", "2")
    assert "ya existe" in files.mover(perm, "a.txt", "b.txt")


def test_mover_no_saca_del_repo(perm):
    with pytest.raises(files.FueraDelRepo):
        files.mover(perm, "README.md", "../robado.md")


# ---------- 5. el árbol y el .gitignore portado ----------


def test_arbol_saltea_build_dirs_y_gitignore(perm):
    out = files.arbol(perm, max_depth=3)
    assert "main.py" in out
    assert "README.md" in out
    assert "node_modules" not in out    # default duro
    assert "bundle.js" not in out       # `out/` del .gitignore
    assert ".gitignore" in out          # este sí se muestra: es info útil


def test_arbol_respeta_max_depth(perm):
    assert "main.py" not in files.arbol(perm, max_depth=1)


def test_arbol_de_un_subdirectorio(perm):
    assert "main.py" in files.arbol(perm, "src")


def test_arbol_de_algo_que_no_es_dir(perm):
    assert files.arbol(perm, "README.md").startswith("error:")


@pytest.mark.parametrize("pat,rel,name,esperado", [
    # Las tres formas que el fnmatch crudo NO matcheaba (bugfix 2026-07-26).
    ("out/", "out", "out", True),            # barra final
    ("/build", "build", "build", True),      # anclado al root
    ("**/tmp", "src/tmp", "tmp", True),      # en cualquier nivel
    # Y las que sí andaban.
    ("*.secreto", "x.secreto", "x.secreto", True),
    ("/build", "src/build", "build", False),  # anclado: no en subdirs
    ("out/", "src/main.py", "main.py", False),
])
def test_matcher_de_gitignore(pat, rel, name, esperado):
    assert files._pattern_matches(pat, rel, name) is esperado


def test_negacion_del_gitignore_gana(repo):
    (repo / ".gitignore").write_text("*.log\n!importante.log\n")
    (repo / "ruido.log").write_text("x")
    (repo / "importante.log").write_text("x")
    assert files.esta_gitignorado(repo, repo / "ruido.log")
    assert not files.esta_gitignorado(repo, repo / "importante.log")


def test_sin_gitignore_no_explota(tmp_path):
    assert not files.esta_gitignorado(tmp_path, tmp_path / "x.txt")


# ---------- 6. buscar (no existía en el wrapper) ----------


def test_buscar_encuentra_con_archivo_y_linea(perm):
    out = files.buscar(perm, "chau")
    assert "src/main.py:2" in out


def test_buscar_filtra_por_glob(perm, repo):
    (repo / "nota.md").write_text("chau\n")
    assert "nota.md" in files.buscar(perm, "chau", glob="*.md")
    assert "main.py" not in files.buscar(perm, "chau", glob="*.md")


def test_buscar_saltea_lo_ignorado(perm, repo):
    (repo / "out" / "bundle.js").write_text("secreto-en-build\n")
    (repo / "node_modules" / "lib.js").write_text("secreto-en-build\n")
    assert "sin coincidencias" in files.buscar(perm, "secreto-en-build")


def test_buscar_sin_patron(perm):
    assert files.buscar(perm, "  ").startswith("error:")


# ---------- 7. raíces extra (2026-08-16) ----------
#
# Pedido: "el sandbox como que igual puede saltar arena afuera de la caja
# cuando necesitemos hacer otras cosas". La caja se queda; deja de ser el
# único lugar del mundo. Lo que estos tests cuidan es que abrir una
# rendija no abra la puerta: lo habilitado entra, lo demás sigue afuera.


@pytest.fixture
def otro(tmp_path_factory):
    d = tmp_path_factory.mktemp("otro-repo")
    (d / "lib.py").write_text("def x(): pass\n")
    (d / "sub").mkdir()
    (d / "sub" / "hondo.txt").write_text("hola\n")
    return d


def test_sin_extras_todo_afuera_se_corta(repo, otro):
    perm = files.Permisos.para(str(repo))
    with pytest.raises(files.FueraDelRepo):
        perm.resolve(str(otro / "lib.py"))


def test_una_raiz_extra_habilita_lo_suyo(repo, otro):
    perm = files.Permisos.para(str(repo), extras=[str(otro)])
    assert perm.resolve(str(otro / "lib.py")).name == "lib.py"
    assert perm.resolve(str(otro / "sub" / "hondo.txt")).name == "hondo.txt"


def test_habilitar_una_raiz_no_habilita_a_su_hermana(repo, otro, tmp_path_factory):
    """Lo importante: la rendija es del tamaño que se pidió, no del padre."""
    vecina = tmp_path_factory.mktemp("vecina")
    (vecina / "secreto.txt").write_text("x")
    perm = files.Permisos.para(str(repo), extras=[str(otro)])
    with pytest.raises(files.FueraDelRepo):
        perm.resolve(str(vecina / "secreto.txt"))


def test_el_dotdot_sigue_sin_escapar_con_extras(repo, otro):
    perm = files.Permisos.para(str(repo), extras=[str(otro)])
    with pytest.raises(files.FueraDelRepo):
        perm.resolve(str(otro / ".." / ".." / "etc" / "passwd"))


def test_leer_y_escribir_en_una_raiz_extra(repo, otro):
    perm = files.Permisos.para(str(repo), extras=[str(otro)])
    assert "def x()" in files.leer(perm, str(otro / "lib.py"))
    files.escribir(perm, str(otro / "nuevo.txt"), "contenido\n")
    assert (otro / "nuevo.txt").read_text() == "contenido\n"


def test_read_only_tambien_aplica_a_las_extras(repo, otro):
    """El permiso de escritura es del run, no del directorio."""
    perm = files.Permisos.para(str(repo), read_only=True, extras=[str(otro)])
    assert "def x()" in files.leer(perm, str(otro / "lib.py"))
    with pytest.raises(files.SinPermiso):
        files.escribir(perm, str(otro / "nuevo.txt"), "x")


def test_una_extra_adentro_del_repo_se_descarta(repo):
    """No agrega nada y rompería los paths relativos del árbol."""
    perm = files.Permisos.para(str(repo), extras=[str(repo / "src")])
    assert perm.extras == ()


def test_una_extra_ilegible_no_voltea_el_run(repo):
    perm = files.Permisos.para(str(repo), extras=["", "   ", None])
    assert perm.extras == ()


def test_el_arbol_de_una_extra_usa_su_propia_raiz(repo, otro):
    """Sin esto, `relative_to(repo)` explota sobre un path de otra raíz."""
    perm = files.Permisos.para(str(repo), extras=[str(otro)])
    salida = files.arbol(perm, str(otro))
    assert "lib.py" in salida
    assert "sub/" in salida or "sub" in salida


def test_buscar_en_una_extra(repo, otro):
    perm = files.Permisos.para(str(repo), extras=[str(otro)])
    salida = files.buscar(perm, "def x", en=str(otro))
    assert "lib.py:1:" in salida


def test_buscar_sin_en_no_se_va_a_las_extras(repo, otro):
    """El default sigue siendo el repo: una extra se visita si se pide."""
    perm = files.Permisos.para(str(repo), extras=[str(otro)])
    assert "lib.py" not in files.buscar(perm, "def x")


def test_base_de_devuelve_la_raiz_mas_especifica(repo, tmp_path_factory):
    """Con `/padre` y `/padre/hijo` habilitados, gana el hijo.

    Si ganara el padre, los paths se mostrarían con medio prefijo de más
    y el `.gitignore` que se aplica sería el equivocado.
    """
    padre = tmp_path_factory.mktemp("padre")
    hijo = padre / "hijo"
    hijo.mkdir()
    (hijo / "a.txt").write_text("x")
    perm = files.Permisos.para(str(repo), extras=[str(padre), str(hijo)])
    assert perm.base_de((hijo / "a.txt").resolve()) == hijo.resolve()


def test_el_error_lista_las_rutas_habilitadas(repo, otro):
    """Un "no" que no dice dónde SÍ deja al modelo adivinando."""
    perm = files.Permisos.para(str(repo), extras=[str(otro)])
    with pytest.raises(files.FueraDelRepo) as e:
        perm.resolve("/etc/passwd")
    msg = str(e.value)
    assert str(repo) in msg and str(otro) in msg
    assert "ask_human" in msg


def test_las_vedadas_se_evaluan_contra_la_raiz_que_contiene(repo, otro):
    """Una vedada tiene que tapar el path relativo de SU raíz.

    Si se midiera siempre contra el repo, `relative_to` explotaría sobre
    un archivo de una extra y el veto se caería en silencio.
    """
    (otro / "secretos").mkdir()
    (otro / "secretos" / "k.env").write_text("KEY=1")
    perm = files.Permisos.para(str(repo), extras=[str(otro)],
                               vedadas=["secretos"])
    with pytest.raises(files.SinPermiso):
        perm.resolve(str(otro / "secretos" / "k.env"))


# ---------- 8. de dónde salen las raíces extra ----------


def test_rutas_extra_siempre_incluye_el_temp(monkeypatch):
    """Escribir un scratch es la escapada legítima más común.

    Sin un lugar donde hacerlo, el modelo deja basura en el repo.
    """
    import tempfile

    from relay import experts
    monkeypatch.delenv("FOURBIS_EXTRA_ROOTS", raising=False)
    assert tempfile.gettempdir() in file_tools.rutas_extra({})


def test_rutas_extra_toma_las_del_proyecto(monkeypatch):
    from relay import experts
    monkeypatch.delenv("FOURBIS_EXTRA_ROOTS", raising=False)
    salida = file_tools.rutas_extra({"rutas_extra": ["/datos/clientes"]})
    assert "/datos/clientes" in salida


def test_rutas_extra_expande_el_token_repos(monkeypatch):
    """`repos` en vez de la ruta a mano: escribirla en cada proyecto envejece mal."""
    from relay import experts
    monkeypatch.delenv("FOURBIS_EXTRA_ROOTS", raising=False)
    monkeypatch.setattr(experts.config, "repos_root", lambda: "/src/repos")
    assert "/src/repos" in file_tools.rutas_extra({"rutas_extra": ["repos"]})


def test_rutas_extra_toma_la_variable_global(monkeypatch):
    from relay import experts
    monkeypatch.setenv("FOURBIS_EXTRA_ROOTS",
                       os.pathsep.join(["/a", "/b"]))
    salida = file_tools.rutas_extra({})
    assert "/a" in salida and "/b" in salida


def test_rutas_extra_sin_nada_configurado_no_abre_el_home(monkeypatch):
    """Lo único que entra solo es el temp. Ni el home ni la raíz del disco.

    Ahí la diferencia entre "necesito esto" y "me equivoqué de path" deja
    de existir.
    """
    import tempfile

    from relay import experts
    monkeypatch.delenv("FOURBIS_EXTRA_ROOTS", raising=False)
    assert file_tools.rutas_extra({}) == [tempfile.gettempdir()]


# ---------- 9. el sandbox apagado (2026-08-16) ----------
#
# Flag pedido después de la discusión de qué contiene el sandbox: con
# `shell` en la mesa, lo que la caja cierra ya estaba abierto por otro
# lado. Lo que estos tests cuidan es que apagar UNA cosa no apague TRES:
# `read_only` y `vedadas` responden preguntas distintas y siguen valiendo.


def test_apagado_llega_a_cualquier_lado(repo, tmp_path_factory):
    lejos = tmp_path_factory.mktemp("lejos")
    (lejos / "x.txt").write_text("hola\n")
    perm = files.Permisos.para(str(repo), abierto=True)
    assert perm.resolve(str(lejos / "x.txt")).name == "x.txt"
    assert "hola" in files.leer(perm, str(lejos / "x.txt"))


def test_apagado_tambien_escribe_afuera(repo, tmp_path_factory):
    lejos = tmp_path_factory.mktemp("lejos2")
    perm = files.Permisos.para(str(repo), abierto=True)
    files.escribir(perm, str(lejos / "nuevo.txt"), "contenido\n")
    assert (lejos / "nuevo.txt").read_text() == "contenido\n"


def test_apagado_no_apaga_read_only(repo, tmp_path_factory):
    """`dónde` y `si puede escribir` son preguntas distintas."""
    lejos = tmp_path_factory.mktemp("lejos3")
    (lejos / "x.txt").write_text("hola\n")
    perm = files.Permisos.para(str(repo), abierto=True, read_only=True)
    assert "hola" in files.leer(perm, str(lejos / "x.txt"))
    with pytest.raises(files.SinPermiso):
        files.escribir(perm, str(lejos / "y.txt"), "x")


def test_apagado_no_apaga_las_rutas_vedadas(repo):
    """`vedadas` es lo que NUNCA se toca, no una consecuencia de la raíz."""
    (repo / "secretos").mkdir()
    (repo / "secretos" / "k.env").write_text("KEY=1")
    perm = files.Permisos.para(str(repo), abierto=True, vedadas=["secretos"])
    with pytest.raises(files.SinPermiso):
        perm.resolve(str(repo / "secretos" / "k.env"))


def test_una_vedada_tapa_afuera_tambien_cuando_esta_apagado(repo, tmp_path_factory):
    """Sin raíz contra la cual medir, el veto se aplica sobre el path entero.

    Si no, apagar el sandbox convertiría `vedadas` en decorativo justo
    cuando más falta hace.
    """
    lejos = tmp_path_factory.mktemp("lejos4")
    (lejos / "secretos").mkdir()
    (lejos / "secretos" / "k.env").write_text("KEY=1")
    perm = files.Permisos.para(str(repo), abierto=True, vedadas=["secretos"])
    with pytest.raises(files.SinPermiso):
        perm.resolve(str(lejos / "secretos" / "k.env"))


def test_prendido_es_el_default(repo, tmp_path_factory):
    lejos = tmp_path_factory.mktemp("lejos5")
    perm = files.Permisos.para(str(repo))
    assert perm.abierto is False
    with pytest.raises(files.FueraDelRepo):
        perm.resolve(str(lejos / "x.txt"))


def test_el_arbol_de_algo_sin_raiz_no_explota(repo, tmp_path_factory):
    """`relative_to` de una raíz que no lo contiene tira ValueError.

    Con el sandbox apagado ese caso es normal, así que la referencia pasa
    a ser el propio directorio pedido.
    """
    lejos = tmp_path_factory.mktemp("lejos6")
    (lejos / "a.txt").write_text("x")
    (lejos / "sub").mkdir()
    perm = files.Permisos.para(str(repo), abierto=True)
    salida = files.arbol(perm, str(lejos))
    assert "a.txt" in salida


def test_buscar_fuera_de_toda_raiz_con_el_sandbox_apagado(repo, tmp_path_factory):
    lejos = tmp_path_factory.mktemp("lejos7")
    (lejos / "code.py").write_text("def objetivo(): pass\n")
    perm = files.Permisos.para(str(repo), abierto=True)
    assert "code.py:1:" in files.buscar(perm, "def objetivo", en=str(lejos))


# ---------- 10. de dónde sale el flag ----------


def test_flag_del_proyecto_gana(monkeypatch):
    from relay import experts
    monkeypatch.delenv("FOURBIS_SANDBOX", raising=False)
    abierto, por_que = file_tools.sandbox_abierto({"sandbox": False})
    assert abierto is True
    assert "defaults_json" in por_que


def test_el_proyecto_puede_prenderlo_aunque_el_global_lo_apague(monkeypatch):
    """El proyecto manda: si no, no habría forma de re-encerrar uno solo."""
    from relay import experts
    monkeypatch.setenv("FOURBIS_SANDBOX", "0")
    abierto, _ = file_tools.sandbox_abierto({"sandbox": True})
    assert abierto is False


@pytest.mark.parametrize("valor", ["0", "off", "false", "no", "OFF"])
def test_la_variable_global_lo_apaga(monkeypatch, valor):
    from relay import experts
    monkeypatch.setenv("FOURBIS_SANDBOX", valor)
    abierto, por_que = file_tools.sandbox_abierto({})
    assert abierto is True
    assert "FOURBIS_SANDBOX" in por_que


@pytest.mark.parametrize("valor", ["", "1", "on", "true", "cualquier-cosa"])
def test_lo_que_no_es_un_apagado_explicito_deja_el_sandbox(monkeypatch, valor):
    """Ante la duda, la caja se queda: un typo no debería abrir el disco."""
    from relay import experts
    monkeypatch.setenv("FOURBIS_SANDBOX", valor)
    assert file_tools.sandbox_abierto({})[0] is False


def test_por_defecto_esta_prendido(monkeypatch):
    from relay import experts
    monkeypatch.delenv("FOURBIS_SANDBOX", raising=False)
    assert file_tools.sandbox_abierto({}) == (False, "")
