"""Skills versionadas en el repo, no solo en el directorio global (2026-09-08).

Hasta hoy `SKILLS_DIR` era una constante global (`~/.copilot/skills`)
para los 50 proyectos registrados en el relay. Dos consecuencias medidas:

- `4bis.relay/.claude/skills/relay-ui/SKILL.md` —9.536 chars, escrita a
  medida para el panel de ese repo, la unica skill propia que existe—
  era invisible para el experto del propio relay.
- Al mismo tiempo, cada turno de un repo de Python anunciaba `unocss`,
  `vite`, `tsdown` y `dotnet-best-practices`.

Una skill del repo gana sobre la global del mismo nombre: es mas
especifica por construccion y se versiona junto al codigo que describe.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MCP_SRC = HERE.parent / "src"
if str(MCP_SRC) not in sys.path:
    sys.path.insert(0, str(MCP_SRC))

from relay import skills as S  # noqa: E402


def _skill(base: Path, dir_name: str, name: str, desc: str,
           cuerpo: str = "cuerpo") -> None:
    d = base / dir_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n{cuerpo}\n",
        encoding="utf-8")


def test_sin_repo_solo_esta_la_global(tmp_path, monkeypatch):
    """El comportamiento de antes queda intacto: sin repo, una sola ruta."""
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    monkeypatch.setattr(S, "resolve_skills_dir", lambda: global_dir)

    assert S.skills_dirs("") == [global_dir]
    # Un repo que existe pero no tiene `.claude/skills` tampoco suma.
    repo = tmp_path / "repo_pelado"
    repo.mkdir()
    assert S.skills_dirs(str(repo)) == [global_dir]


def test_el_repo_va_primero(tmp_path, monkeypatch):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    monkeypatch.setattr(S, "resolve_skills_dir", lambda: global_dir)

    repo = tmp_path / "repo"
    propio = repo / ".claude" / "skills"
    propio.mkdir(parents=True)

    assert S.skills_dirs(str(repo)) == [propio, global_dir], (
        "el directorio del repo tiene que tener prioridad sobre el global")


def test_la_skill_del_repo_se_ve_ademas_de_las_globales(tmp_path, monkeypatch):
    """El caso `relay-ui`: existe en el repo y antes era invisible."""
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    _skill(global_dir, "unocss", "unocss", "CSS atomico")
    monkeypatch.setattr(S, "resolve_skills_dir", lambda: global_dir)

    repo = tmp_path / "repo"
    propio = repo / ".claude" / "skills"
    propio.mkdir(parents=True)
    _skill(propio, "relay-ui", "relay-ui", "Design system del panel")

    idx = S.build_index_multi(S.skills_dirs(str(repo)))
    nombres = {s.name for s in idx.auto_skills() + idx.manual_skills()}
    assert "relay-ui" in nombres, (
        "la skill del repo sigue invisible para el experto de ese repo")
    assert "unocss" in nombres, "las globales no se pierden"


def test_ante_el_mismo_nombre_gana_la_del_repo(tmp_path, monkeypatch):
    """Desempate por `name` del frontmatter, que es lo que ve el modelo."""
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    _skill(global_dir, "estilo", "estilo", "la global", cuerpo="GLOBAL")
    monkeypatch.setattr(S, "resolve_skills_dir", lambda: global_dir)

    repo = tmp_path / "repo"
    propio = repo / ".claude" / "skills"
    propio.mkdir(parents=True)
    # Carpeta con otro nombre a proposito: el desempate NO es por carpeta.
    _skill(propio, "estilo_local", "estilo", "la del repo", cuerpo="DEL REPO")

    dirs = S.skills_dirs(str(repo))
    idx = S.build_index_multi(dirs)
    coincidencias = [s for s in idx.auto_skills() + idx.manual_skills()
                     if s.name == "estilo"]
    assert len(coincidencias) == 1, (
        f"quedaron {len(coincidencias)} skills con el mismo `name` visible")
    assert coincidencias[0].description == "la del repo"

    assert "DEL REPO" in (S.read_skill_multi(dirs, "estilo") or ""), (
        "read_skill_multi devolvio el cuerpo de la global")


def test_read_skill_multi_cae_a_la_global(tmp_path, monkeypatch):
    """Lo que el repo no tiene se sigue buscando en la global."""
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    _skill(global_dir, "vite", "vite", "bundler", cuerpo="VITE")
    monkeypatch.setattr(S, "resolve_skills_dir", lambda: global_dir)

    repo = tmp_path / "repo"
    (repo / ".claude" / "skills").mkdir(parents=True)

    dirs = S.skills_dirs(str(repo))
    assert "VITE" in (S.read_skill_multi(dirs, "vite") or "")
    assert S.read_skill_multi(dirs, "no-existe") is None


def test_un_repo_path_invalido_no_rompe(tmp_path, monkeypatch):
    """La fila del proyecto puede traer basura: no puede voltear el run."""
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    monkeypatch.setattr(S, "resolve_skills_dir", lambda: global_dir)

    for basura in ("\x00raro", "   ", "C:/no/existe/en/ningun/lado"):
        assert S.skills_dirs(basura)[-1] == global_dir, (
            f"repo_path {basura!r} dejo al experto sin skills")

# ---------- Fase 2: filtro por proyecto (`defaults_json.skills`) ----------


def _dos_mundos(tmp_path, monkeypatch):
    """Un dir global con tres skills y un repo con la suya."""
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    _skill(global_dir, "unocss", "unocss", "CSS atomico")
    _skill(global_dir, "vite", "vite", "bundler")
    _skill(global_dir, "csharp-async", "csharp-async", "async en C#")
    monkeypatch.setattr(S, "resolve_skills_dir", lambda: global_dir)

    repo = tmp_path / "repo"
    propio = repo / ".claude" / "skills"
    propio.mkdir(parents=True)
    _skill(propio, "relay-ui", "relay-ui", "Design system del panel")
    return repo


def test_sin_lista_declarada_salen_todas(tmp_path, monkeypatch):
    """`defaults_json` sin la clave `skills` = comportamiento de antes."""
    repo = _dos_mundos(tmp_path, monkeypatch)
    bloque = S.bloque_del_proyecto(str(repo), None)
    for nombre in ("unocss", "vite", "csharp-async", "relay-ui"):
        assert nombre in bloque, f"falta {nombre} sin filtro declarado"


def test_la_lista_deja_afuera_las_globales_que_no_aplican(tmp_path,
                                                          monkeypatch):
    """El caso real: un repo de Python no necesita `unocss` ni `csharp`."""
    repo = _dos_mundos(tmp_path, monkeypatch)
    bloque = S.bloque_del_proyecto(str(repo), ["vite"])
    assert "vite" in bloque
    assert "unocss" not in bloque, "se colo una global fuera de la lista"
    assert "csharp-async" not in bloque


def test_la_skill_del_repo_entra_aunque_no_este_en_la_lista(tmp_path,
                                                            monkeypatch):
    """Una skill versionada en el repo ya declaro para que repo es.

    Exigir que ademas se la nombre en `defaults_json` seria burocracia
    que se desincroniza sola: alguien agrega la skill al repo, se olvida
    de la lista, y la skill nunca se usa sin que nadie sepa por que.
    """
    repo = _dos_mundos(tmp_path, monkeypatch)
    bloque = S.bloque_del_proyecto(str(repo), ["vite"])
    assert "relay-ui" in bloque, (
        "la skill del propio repo quedo afuera por no estar en la lista")


def test_lista_vacia_deja_solo_las_del_repo(tmp_path, monkeypatch):
    repo = _dos_mundos(tmp_path, monkeypatch)
    bloque = S.bloque_del_proyecto(str(repo), [])
    assert "relay-ui" in bloque
    for nombre in ("unocss", "vite", "csharp-async"):
        assert nombre not in bloque


def test_un_nombre_que_no_existe_se_anota(tmp_path, monkeypatch):
    """Un typo silencioso es una skill que nunca se usa y nadie sabe por que."""
    repo = _dos_mundos(tmp_path, monkeypatch)
    bloque = S.bloque_del_proyecto(str(repo), ["vite", "no-existe-esta"])
    assert "no-existe-esta" in bloque, (
        "el nombre mal escrito desaparecio sin dejar rastro")
    assert "vite" in bloque, "un typo no puede llevarse puestas a las buenas"


def test_el_filtro_no_distingue_mayusculas(tmp_path, monkeypatch):
    repo = _dos_mundos(tmp_path, monkeypatch)
    bloque = S.bloque_del_proyecto(str(repo), ["  VITE  "])
    assert "vite" in bloque
    assert "no existen" not in bloque, (
        "se anoto como inexistente una skill que si esta")


def test_un_proyecto_sin_skills_propias_solo_filtra_las_globales(tmp_path,
                                                                 monkeypatch):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    _skill(global_dir, "vite", "vite", "bundler")
    _skill(global_dir, "unocss", "unocss", "CSS atomico")
    monkeypatch.setattr(S, "resolve_skills_dir", lambda: global_dir)

    repo = tmp_path / "pelado"
    repo.mkdir()
    bloque = S.bloque_del_proyecto(str(repo), ["vite"])
    assert "vite" in bloque and "unocss" not in bloque

# ---------- Fase 3: las on-demand viajan con su descripcion ----------


def _manual(base, dir_name, name, desc):
    d = base / dir_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\nwhen: manual\n---\n\ncuerpo\n",
        encoding="utf-8")


def test_una_skill_manual_ya_no_viaja_como_nombre_pelado(tmp_path,
                                                         monkeypatch):
    """La descripcion EXISTE en el frontmatter y antes se tiraba.

    El modelo veia `tsdown` suelto y tenia que adivinar si le servia.
    `read_skill` se llamo en 57 de 1.132 chats (5%): ese numero medía que
    el modelo no se entera de que hacen, no que no sirvan.
    """
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    _manual(global_dir, "tsdown", "tsdown", "Empaqueta librerias TypeScript")
    monkeypatch.setattr(S, "resolve_skills_dir", lambda: global_dir)

    bloque = S.bloque_del_proyecto("", None)
    assert "tsdown" in bloque
    assert "Empaqueta librerias TypeScript" in bloque, (
        "la descripcion de una skill on-demand se sigue descartando")


def test_la_seccion_on_demand_sigue_diciendo_como_pedir_el_cuerpo(
        tmp_path, monkeypatch):
    """Con descripcion o sin ella, el cuerpo se pide con `read_skill`.

    Si se pierde esa instruccion, el modelo puede creer que la
    descripcion es todo lo que hay y decidir sin leer las reglas.
    """
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    _manual(global_dir, "vite", "vite", "bundler")
    monkeypatch.setattr(S, "resolve_skills_dir", lambda: global_dir)

    bloque = S.bloque_del_proyecto("", None)
    assert "read_skill" in bloque, (
        "sin esa instruccion el modelo no sabe como llegar al cuerpo")


def test_las_auto_y_las_manual_siguen_separadas(tmp_path, monkeypatch):
    """No es lo mismo "leela" que "leela si aplica": el modelo tiene que
    poder distinguirlas, aunque ahora las dos lleven descripcion."""
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    _skill(global_dir, "auto-una", "auto-una", "se lee siempre")
    _manual(global_dir, "manual-una", "manual-una", "se lee si aplica")
    monkeypatch.setattr(S, "resolve_skills_dir", lambda: global_dir)

    bloque = S.bloque_del_proyecto("", None)
    assert "## Skills disponibles" in bloque
    assert "## Skills on-demand" in bloque
    # La auto va antes que el encabezado de on-demand.
    assert bloque.index("auto-una") < bloque.index("## Skills on-demand")


def test_sin_manual_no_queda_un_encabezado_vacio(tmp_path, monkeypatch):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    _skill(global_dir, "sola", "sola", "la unica")
    monkeypatch.setattr(S, "resolve_skills_dir", lambda: global_dir)

    bloque = S.bloque_del_proyecto("", None)
    assert "on-demand" not in bloque, (
        "encabezado de una seccion que no tiene skills")
