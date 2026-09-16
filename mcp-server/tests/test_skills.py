"""Tests del parser + cache de skills (ADR-010).

Ponytail: cubrimos solo lo no-trivial (regex de frontmatter, edge
cases del spec, TTL de la cache). El bloque de skills se inyecta
hoy en el system prompt de los expertos (build_instructions +
admin preview). Antes también se inyectaba en el push (POST /prompts,
ya no existe) — el camino experto lo cubre test_experts.py.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
MCP_SRC = HERE.parent / "mcp-server" / "src"
sys.path.insert(0, str(MCP_SRC))

from relay.skills import (  # noqa: E402
    SkillCache,
    _build_index,
    _parse_skill_md,
    render_block,
    render_requested_block,
    Skill,
)


# ---------- _parse_skill_md ----------


def test_parse_skill_md_ok(tmp_path: Path) -> None:
    skill_dir = tmp_path / "tdd"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: tdd\n"
        "description: Implement using Test-Driven Development\n"
        "---\n"
        "\n"
        "# Body que ignoramos\n",
        encoding="utf-8",
    )
    s = _parse_skill_md(skill_dir / "SKILL.md", "tdd")
    assert s == Skill(name="tdd", description="Implement using Test-Driven Development")


def test_parse_skill_md_no_frontmatter(tmp_path: Path) -> None:
    p = tmp_path / "SKILL.md"
    p.write_text("# Solo titulo, sin frontmatter\n", encoding="utf-8")
    assert _parse_skill_md(p, "broken") is None


def test_parse_skill_md_missing_description(tmp_path: Path) -> None:
    skill_dir = tmp_path / "foo"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: foo\n---\n", encoding="utf-8",
    )
    s = _parse_skill_md(skill_dir / "SKILL.md", "foo")
    assert s == Skill(name="foo", description="")


def test_parse_skill_md_name_differs_from_dir(tmp_path: Path) -> None:
    """Si el name del frontmatter no coincide con el dir, usamos el del frontmatter."""
    skill_dir = tmp_path / "tdd_dir"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: tdd\ndescription: ok\n---\n", encoding="utf-8",
    )
    s = _parse_skill_md(skill_dir / "SKILL.md", "tdd_dir")
    assert s is not None and s.name == "tdd"


def test_parse_skill_md_unreadable(tmp_path: Path) -> None:
    """Si read_text() rompe (permisos / encoding), devolvemos None y no raise."""
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: x\ndescription: y\n---\n", encoding="utf-8")

    real_read = Path.read_text

    def boom(self, *a, **kw):
        raise OSError("simulated disk error")

    Path.read_text = boom  # type: ignore[assignment]
    try:
        assert _parse_skill_md(p, "x") is None
    finally:
        Path.read_text = real_read  # type: ignore[assignment]


# ---------- _build_index ----------


def test_build_index_missing_dir(tmp_path: Path) -> None:
    idx = _build_index(tmp_path / "nope")
    assert idx.skills == ()
    assert idx.dir_existed is False


def test_build_index_empty_dir(tmp_path: Path) -> None:
    idx = _build_index(tmp_path)
    assert idx.skills == ()
    assert idx.dir_existed is True


def test_build_index_picks_valid_skips_broken(tmp_path: Path) -> None:
    # skill válida
    d1 = tmp_path / "tdd"
    d1.mkdir()
    (d1 / "SKILL.md").write_text(
        "---\nname: tdd\ndescription: Test-driven\n---\n", encoding="utf-8",
    )
    # skill rota (sin frontmatter)
    d2 = tmp_path / "broken"
    d2.mkdir()
    (d2 / "SKILL.md").write_text("# sin frontmatter\n", encoding="utf-8")
    # directorio sin SKILL.md → se ignora silencioso
    (tmp_path / "ghost").mkdir()

    idx = _build_index(tmp_path)
    names = [s.name for s in idx.skills]
    assert names == ["tdd"]


# ---------- render_block ----------


def test_render_block_empty() -> None:
    from relay.skills import _Index
    idx = _Index(skills=(), fetched_at=0.0, dir_existed=True)
    assert render_block(idx) == ""


def test_render_block_format() -> None:
    from relay.skills import _Index
    idx = _Index(
        skills=(
            Skill(name="brainstorming", description="Spec via dialogue"),
            Skill(name="tdd", description="TDD discipline"),
        ),
        fetched_at=0.0,
        dir_existed=True,
    )
    block = render_block(idx)
    assert block.startswith("## Skills disponibles")
    assert "- **brainstorming**: Spec via dialogue" in block
    assert "- **tdd**: TDD discipline" in block


# ---------- SkillCache (TTL) ----------


async def test_cache_refreshes_after_ttl(tmp_path: Path) -> None:
    cache = SkillCache(skills_dir=tmp_path, ttl_s=0.05)
    # primera vez: vacío (dir no existe)
    assert await cache.get_block() == ""

    # creamos una skill
    d = tmp_path / "tdd"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: tdd\ndescription: TDD\n---\n", encoding="utf-8",
    )

    # dentro del TTL: cache todavía vacía
    assert await cache.get_block() == ""

    # esperamos a que expire
    await asyncio.sleep(0.06)
    block = await cache.get_block()
    assert "**tdd**" in block


async def test_cache_never_raises_on_broken_dir(tmp_path: Path) -> None:
    """Si el FS está roto (permisos / no existe), get_block devuelve '' y no raise."""
    # path que no se puede crear como dir para listar: usamos un archivo
    fake = tmp_path / "fake_file"
    fake.write_text("x", encoding="utf-8")
    # apuntamos el cache al archivo (no es dir, así que _build_index devuelve vacío)
    cache = SkillCache(skills_dir=fake, ttl_s=60.0)
    block = await cache.get_block()
    assert block == ""

# ---------- overwrite_skill_sync (edición desde la Admin UI) ----------


def _write_skill(base: Path, name: str, body: str = "cuerpo") -> Path:
    d = base / name
    d.mkdir(parents=True)
    md = d / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: desc original\n---\n\n{body}\n",
        encoding="utf-8")
    return md


def test_overwrite_skill_deja_bak(tmp_path: Path) -> None:
    from relay.skills import overwrite_skill_sync

    md = _write_skill(tmp_path, "demo")
    nuevo = "---\nname: demo\ndescription: desc recortada\n---\n\ncuerpo nuevo\n"
    assert overwrite_skill_sync(tmp_path, "demo", nuevo) is True
    assert md.read_text(encoding="utf-8") == nuevo
    # El .bak guarda la versión anterior: esto va al system prompt de
    # cada run, un guardado en falso sin copia se paga caro.
    assert "desc original" in md.with_suffix(".md.bak").read_text(encoding="utf-8")


def test_overwrite_skill_inexistente_devuelve_none(tmp_path: Path) -> None:
    from relay.skills import overwrite_skill_sync

    assert overwrite_skill_sync(tmp_path, "no-existe", "x") is None


def test_overwrite_skill_rechaza_path_traversal(tmp_path: Path) -> None:
    from relay.skills import overwrite_skill_sync

    victima = tmp_path.parent / "victima.md"
    victima.write_text("intacto", encoding="utf-8")
    for raro in ("../victima", "..", "sub/dir", "a\b"):
        assert overwrite_skill_sync(tmp_path, raro, "pwned") is None
    assert victima.read_text(encoding="utf-8") == "intacto"


def test_overwrite_skill_frontmatter_roto_se_detecta(tmp_path: Path) -> None:
    """La UI avisa 'no se inyecta' releyendo por el mismo camino que el
    runtime: si el humano rompe el frontmatter, _build_index la descarta."""
    from relay.skills import _build_index, overwrite_skill_sync

    _write_skill(tmp_path, "demo")
    assert len(_build_index(tmp_path).skills) == 1
    assert overwrite_skill_sync(tmp_path, "demo", "sin frontmatter\n") is True
    assert _build_index(tmp_path).skills == ()


# ---------- render_index_compact (iter 10.4) ----------


def test_render_index_compact_vacio_devuelve_vacio(tmp_path: Path) -> None:
    """Sin skills, el bloque compact también es vacío (no imprimimos
    el header solo, quedaría como ruido)."""
    from relay.skills import _build_index, render_index_compact

    assert render_index_compact(_build_index(tmp_path)) == ""


def test_render_index_compact_solo_nombres(tmp_path: Path) -> None:
    """El bloque compact NO lleva descripciones, solo nombres en
    bullets con backticks. Más barato que render_block."""
    from relay.skills import _build_index, render_index_compact

    _write_skill(tmp_path, "alpha", body="alpha body")
    _write_skill(tmp_path, "beta", body="beta body")
    block = render_index_compact(_build_index(tmp_path))

    # Header + 2 bullets. La descripción NO debe aparecer.
    assert "alpha" in block and "beta" in block
    assert "alpha body" not in block and "beta body" not in block
    assert "read_skill" in block  # instrucción al LLM


def test_render_index_compact_mas_chico_que_render_block(tmp_path: Path) -> None:
    """Ponytail check: el modo compact debe pagar menos tokens cuando
    las descripciones son realistas (≥80 chars). Para descriptions
    muy cortas el header del compact puede empatar; eso no es el
    caso de uso real, lo cubrimos igual porque el bloque compact
    NO escala con la longitud de las descripciones."""
    from relay.skills import _build_index, render_block, render_index_compact

    long_desc = "Esta es una descripción realista de unos 80-100 chars " * 2
    for n in ("aa", "bb", "cc", "dd", "ee", "ff", "gg", "hh"):
        d = tmp_path / n
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: {n}\ndescription: {long_desc}\n---\n", encoding="utf-8"
        )
    idx = _build_index(tmp_path)

    full = render_block(idx)
    compact = render_index_compact(idx)
    assert len(compact) < len(full), (
        f"compact ({len(compact)}) debería ser menor que full ({len(full)})"
    )
    # El compact es O(1 * N) en nombres; el full es O(D * N) en
    # descripciones. Con 8 skills y descripciones de ~80 chars, el
    # compact tiene que ser al menos 5x más chico.
    assert len(compact) * 5 < len(full), (
        f"compact ({len(compact)}) debería ser << full ({len(full)}) "
        "con descripciones realistas"
    )


def test_render_index_compact_incluye_manual_pero_no_off(tmp_path: Path) -> None:
    """En compact TODAS son on-demand: `auto` y `manual` van igual.

    Distinguirlas no le diría nada al modelo (ninguna lleva descripción)
    y dejar afuera las manual escondía skills que `read_skill` sí puede
    leer. `off` sigue afuera: es el interruptor del humano.
    """
    from relay.skills import _build_index, render_index_compact

    for name, extra in (("auto1", ""),
                        ("manual1", "when: manual\n"),
                        ("off1", "enabled: false\n")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: descripcion cara\n{extra}---\n",
            encoding="utf-8",
        )
    block = render_index_compact(_build_index(tmp_path))
    assert "auto1" in block
    assert "manual1" in block
    assert "off1" not in block
    assert "descripcion cara" not in block  # compact = solo nombres


# ---------- read_skill_sync (base de la tool nativa read_skill) ----------


def test_read_skill_sync_existente(tmp_path: Path) -> None:
    """La tool nativa read_skill del experto (iter 10.4) usa esta
    función. Verificamos que devuelve el cuerpo de la skill."""
    from relay.skills import read_skill_sync

    _write_skill(tmp_path, "demo", body="cuerpo concreto")
    content = read_skill_sync(tmp_path, "demo")
    assert content is not None
    assert "demo" in content
    assert "cuerpo concreto" in content


def test_read_skill_sync_inexistente_devuelve_none(tmp_path: Path) -> None:
    """El wrapper async convierte None en un hint con la lista de
    disponibles — pero la función sync es fiel: None es None."""
    from relay.skills import read_skill_sync

    assert read_skill_sync(tmp_path, "fantasma") is None


def test_read_skill_sync_rechaza_path_traversal(tmp_path: Path) -> None:
    """Igual que overwrite_skill_sync: el `name` no puede escapar de
    la base. Mismo bug class, mismo fix."""
    from relay.skills import read_skill_sync

    assert read_skill_sync(tmp_path, "..") is None
    assert read_skill_sync(tmp_path, "../etc/passwd") is None
    assert read_skill_sync(tmp_path, "sub/dir") is None


# ---------- --skill: parse_skill_flags + render_requested_block ----------


def _mk_skill(base: Path, name: str, *, when: str = "auto",
              body: str = "cuerpo") -> None:
    d = base / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: desc de {name}\nwhen: {when}\n"
        f"---\n\n{body}\n", encoding="utf-8")


def test_parse_skill_flags() -> None:
    from relay.experts import parse_skill_flags

    assert parse_skill_flags("arregla --skill pdf,docx ya") == (
        "arregla ya", ["pdf", "docx"])
    assert parse_skill_flags("usa --skills=morning") == ("usa", ["morning"])
    assert parse_skill_flags("sin flags") == ("sin flags", [])


def test_render_requested_block_inyecta_manual(tmp_path: Path) -> None:
    """--skill fuerza una skill `when: manual` (que render_block NO
    inyecta). El bloque trae el SKILL.md completo."""
    from relay.skills import render_block, render_requested_block, _build_index

    _mk_skill(tmp_path, "pdf", when="manual", body="PASOS PDF SECRETOS")
    # Sanity: manual NO entra al bloque automático.
    assert "PASOS PDF" not in render_block(_build_index(tmp_path))
    # Pero SÍ entra cuando se pide explícito.
    block = render_requested_block(tmp_path, ["pdf"])
    assert "PASOS PDF SECRETOS" in block
    assert "--skill" in block


def test_render_requested_block_resuelve_por_name_y_case(tmp_path: Path) -> None:
    _mk_skill(tmp_path, "morning", when="manual")
    # Case-insensitive; y sin duplicar si se pide dos veces.
    block = render_requested_block(tmp_path, ["MORNING", "morning"])
    assert block.count("desc de morning") == 1


def test_render_requested_block_nombre_inexistente_se_anota(tmp_path: Path) -> None:
    block = render_requested_block(tmp_path, ["fantasma"])
    assert "fantasma" in block
    assert "no encontré" in block.lower()


def test_render_requested_block_vacio_sin_nombres(tmp_path: Path) -> None:
    assert render_requested_block(tmp_path, []) == ""
