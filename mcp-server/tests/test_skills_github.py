"""Alta de skills desde GitHub + toggle enabled (2026-07-25).

Cubre lo que puede romper en silencio: el guard anti-traversal del clon,
que el toggle no destroce el SKILL.md, y que apagar una skill la saque
del bloque que va al system prompt.
"""
from pathlib import Path

import pytest

from relay import skills as sk


def _write_skill(root: Path, rel: str, name: str, desc: str = "hace algo",
                 extra: str = "") -> Path:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n{extra}---\n\n# {name}\n\ncuerpo\n",
        encoding="utf-8")
    return d


# ---------- toggle enabled ----------

def test_toggle_saca_la_skill_del_bloque(tmp_path):
    _write_skill(tmp_path, "alpha", "alpha")
    _write_skill(tmp_path, "beta", "beta")

    block = sk.render_block(sk._build_index(tmp_path))
    assert "alpha" in block and "beta" in block

    assert sk.set_skill_enabled_sync(tmp_path, "alpha", False) is True
    block = sk.render_block(sk._build_index(tmp_path))
    assert "alpha" not in block
    assert "beta" in block

    # Y vuelve a entrar al prenderla (sin duplicar la línea).
    assert sk.set_skill_enabled_sync(tmp_path, "alpha", True) is True
    text = (tmp_path / "alpha" / "SKILL.md").read_text(encoding="utf-8")
    assert text.count("enabled:") == 1
    assert "alpha" in sk.render_block(sk._build_index(tmp_path))


def test_toggle_preserva_el_cuerpo_y_los_otros_campos(tmp_path):
    _write_skill(tmp_path, "gamma", "gamma", extra="when: manual\n")
    sk.set_skill_enabled_sync(tmp_path, "gamma", False)
    text = (tmp_path / "gamma" / "SKILL.md").read_text(encoding="utf-8")
    assert "when: manual" in text
    assert "# gamma" in text and "cuerpo" in text
    assert "enabled: false" in text


def test_frontmatter_citado_no_arrastra_las_comillas(tmp_path):
    # Caso real: prompt-optimizer trae `name: "prompt-optimizer"` y se
    # inyectaba como `- **"prompt-optimizer"**` en el system prompt.
    d = tmp_path / "prompt-optimizer"
    d.mkdir()
    (d / "SKILL.md").write_text(
        '---\nname: "prompt-optimizer"\ndescription: \'hace cosas\'\n---\n\nx\n',
        encoding="utf-8")
    row = sk.list_skills_sync(tmp_path)[0]
    assert row["name"] == "prompt-optimizer"
    assert row["description"] == "hace cosas"
    assert '"' not in sk.render_block(sk._build_index(tmp_path))


def test_estado_manual_viaja_con_su_descripcion(tmp_path):
    """`manual` = nombre Y descripción entran al prompt. El cuerpo no.

    Este test fijaba lo contrario hasta el 8/9/2026 ("el nombre sí, la
    descripción no") y se invirtió a propósito: la descripción está en
    el frontmatter y se descartaba, así que el experto veía `epsilon`
    suelto y tenía que adivinar si le servía. Lo único que sigue sin
    viajar es el CUERPO, que es justamente lo que se pide con
    `read_skill`.
    """
    _write_skill(tmp_path, "epsilon", "epsilon", desc="descripcion cara")
    assert sk.set_skill_state_sync(tmp_path, "epsilon", "manual") is True

    idx = sk._build_index(tmp_path)
    block = sk.render_block(idx)
    assert "**epsilon**" in block                # bullet, igual que las auto
    assert "descripcion cara" in block           # la descripción ahora sí
    assert "read_skill" in block                 # y cómo pedir el cuerpo
    assert [s.name for s in idx.manual_skills()] == ["epsilon"]
    assert sk.list_skills_sync(tmp_path)[0]["state"] == "manual"


def test_estado_off_no_se_ofrece_ni_on_demand(tmp_path):
    """`off` es distinto de `manual`: no entra ni como nombre."""
    _write_skill(tmp_path, "epsilon", "epsilon")
    assert sk.set_skill_state_sync(tmp_path, "epsilon", "manual") is True
    assert sk.set_skill_state_sync(tmp_path, "epsilon", "off") is True

    idx = sk._build_index(tmp_path)
    assert "epsilon" not in sk.render_block(idx)
    assert idx.manual_skills() == ()


def test_read_skill_resuelve_por_name_del_frontmatter(tmp_path):
    """El LLM ve el `name:`, la Admin UI el dir. Los dos tienen que leer.

    Con dir != name, `read_skill(<name>)` devolvía None y el experto
    concluía que la skill no existe.
    """
    _write_skill(tmp_path, "some-repo-dir", "zeta-real")

    assert sk.resolve_skill_dir(tmp_path, "some-repo-dir") == "some-repo-dir"
    assert sk.resolve_skill_dir(tmp_path, "zeta-real") == "some-repo-dir"
    assert sk.resolve_skill_dir(tmp_path, "ZETA-Real") == "some-repo-dir"
    assert sk.resolve_skill_dir(tmp_path, "no-existe") is None

    assert "cuerpo" in (sk.read_skill_sync(tmp_path, "zeta-real") or "")
    assert sk.read_skill_sync(tmp_path, "no-existe") is None
    # El guard anti-traversal sigue en pie.
    assert sk.read_skill_sync(tmp_path, "../secretos") is None


def test_ciclo_completo_de_estados(tmp_path):
    _write_skill(tmp_path, "zeta", "zeta")
    for state in ("manual", "off", "auto", "off", "manual", "auto"):
        assert sk.set_skill_state_sync(tmp_path, "zeta", state) is True
        parsed = sk.list_skills_sync(tmp_path)[0]
        assert parsed["state"] == state, f"esperaba {state}"
    text = (tmp_path / "zeta" / "SKILL.md").read_text(encoding="utf-8")
    # Sin líneas duplicadas por más vueltas que dé.
    assert text.count("enabled:") == 1 and text.count("when:") == 1
    assert "cuerpo" in text


def test_estado_invalido_no_toca_el_archivo(tmp_path):
    _write_skill(tmp_path, "eta", "eta")
    before = (tmp_path / "eta" / "SKILL.md").read_text(encoding="utf-8")
    assert sk.set_skill_state_sync(tmp_path, "eta", "banana") is False
    assert (tmp_path / "eta" / "SKILL.md").read_text(encoding="utf-8") == before


def test_no_pisa_una_key_homonima_anidada(tmp_path):
    # Las skills de antfu traen `metadata:` con hijos indentados; el
    # writer solo debe tocar las keys de primer nivel.
    d = tmp_path / "theta"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: theta\ndescription: x\nmetadata:\n  when: siempre\n"
        "  author: alguien\n---\n\ncuerpo\n", encoding="utf-8")
    assert sk.set_skill_state_sync(tmp_path, "theta", "manual") is True
    text = (d / "SKILL.md").read_text(encoding="utf-8")
    assert "  when: siempre" in text        # el anidado intacto
    assert "\nwhen: manual" in text         # el nuevo, al top level
    assert sk.list_skills_sync(tmp_path)[0]["state"] == "manual"


def test_toggle_rechaza_nombres_con_traversal(tmp_path):
    _write_skill(tmp_path, "delta", "delta")
    assert sk.set_skill_enabled_sync(tmp_path, "../delta", False) is False
    assert sk.set_skill_enabled_sync(tmp_path, "no-existe", False) is False


# ---------- scan del clon ----------

def test_scan_encuentra_skills_anidadas(tmp_path):
    _write_skill(tmp_path, "skills/uno", "uno")
    _write_skill(tmp_path, "plugins/x/skills/dos", "dos")
    (tmp_path / ".git").mkdir()
    _write_skill(tmp_path, ".git/trampa", "trampa")

    found = sk.scan_repo_skills(tmp_path)
    names = {s["name"] for s in found}
    assert names == {"uno", "dos"}          # .git se saltea
    rels = {s["rel_path"] for s in found}
    assert "plugins/x/skills/dos" in rels   # POSIX, relativo al clon


# ---------- install ----------

def test_install_copia_el_directorio_entero(tmp_path):
    clone, dest = tmp_path / "clone", tmp_path / "dest"
    d = _write_skill(clone, "skills/uno", "uno")
    (d / "scripts").mkdir()
    (d / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")
    dest.mkdir()

    name = sk.install_skill_from_clone(clone, "skills/uno", dest)
    assert name == "uno"
    assert (dest / "uno" / "SKILL.md").is_file()
    assert (dest / "uno" / "scripts" / "run.py").is_file()


def test_install_respeta_overwrite(tmp_path):
    clone, dest = tmp_path / "clone", tmp_path / "dest"
    _write_skill(clone, "uno", "uno")
    dest.mkdir()
    sk.install_skill_from_clone(clone, "uno", dest)
    with pytest.raises(FileExistsError):
        sk.install_skill_from_clone(clone, "uno", dest)
    sk.install_skill_from_clone(clone, "uno", dest, overwrite=True)


def test_install_puede_entrar_apagada(tmp_path):
    clone, dest = tmp_path / "clone", tmp_path / "dest"
    _write_skill(clone, "uno", "uno")
    dest.mkdir()
    sk.install_skill_from_clone(clone, "uno", dest, enabled=False)
    assert "uno" not in sk.render_block(sk._build_index(dest))


@pytest.mark.parametrize("evil", [
    "../fuera", "../../etc", "/abs", "skills/../../fuera", "",
])
def test_install_rechaza_rutas_fuera_del_clon(tmp_path, evil):
    clone, dest = tmp_path / "clone", tmp_path / "dest"
    clone.mkdir()
    dest.mkdir()
    # Objetivo real fuera del clon, para que el único freno sea el guard.
    _write_skill(tmp_path, "fuera", "fuera")
    with pytest.raises(ValueError):
        sk.install_skill_from_clone(clone, evil, dest)
    assert not any(dest.iterdir())


def test_install_rechaza_skills_gigantes(tmp_path, monkeypatch):
    clone, dest = tmp_path / "clone", tmp_path / "dest"
    d = _write_skill(clone, "gorda", "gorda")
    (d / "blob.bin").write_bytes(b"x" * 2048)
    dest.mkdir()
    monkeypatch.setattr(sk, "SKILL_MAX_BYTES", 1024)
    with pytest.raises(ValueError, match="MB"):
        sk.install_skill_from_clone(clone, "gorda", dest)


# ---------- presupuesto ----------

def test_estimate_tokens_y_bullet_en_el_listado(tmp_path):
    assert sk.estimate_tokens("") == 0
    assert sk.estimate_tokens("abcd") == 1
    _write_skill(tmp_path, "uno", "uno", desc="d" * 100)
    row = sk.list_skills_sync(tmp_path)[0]
    assert row["enabled"] is True
    assert row["bullet_tokens"] > 20
