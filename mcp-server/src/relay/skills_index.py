"""Índice y render de skills para el prompt del experto."""
from __future__ import annotations
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("relay.skills")

CACHE_TTL_S = 60.0

SKILLS_DIR = Path.home() / ".copilot" / "skills"

def resolve_skills_dir() -> Path:
    """Directorio canónico de skills. FOURBIS_SKILLS_DIR lo pisa
    (tests y setups no estándar); default ~/.copilot/skills (ADR-010)."""
    raw = os.environ.get("FOURBIS_SKILLS_DIR", "").strip()
    return Path(raw).expanduser() if raw else SKILLS_DIR

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)

_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$", re.MULTILINE)

def _unquote(v: str) -> str:
    """Saca las comillas de un escalar YAML citado (`name: "x"` → `x`).

    Sin esto el valor viajaba con comillas al system prompt: una skill
    con `name: "prompt-optimizer"` se inyectaba como
    `- **"prompt-optimizer"**`. Pasa seguido en las skills públicas
    (YAML obliga a citar cuando el valor tiene `:` o arranca con un
    caracter especial). No es un parser YAML: solo el caso citado
    simple, que es el que aparece.
    """
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1].strip()
    return v

@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    # Si True, NO se inyecta automáticamente en el bloque de skills del
    # system prompt (convención `when: manual` en el frontmatter). Queda
    # disponible vía `list_skills_sync()` para que el caller la ofrezca
    # a demanda (botón en la UI, comando del bot, etc.). Útil para
    # skills procedimentales que se ejecutan contra un input específico
    # y no deben sesgar respuestas conversacionales generales.
    manual: bool = False
    # Interruptor del humano (`enabled: false` en el frontmatter). A
    # diferencia de `manual`, esto es "apagada": no se inyecta y tampoco
    # se ofrece on-demand. El toggle de la Admin UI escribe este campo.
    # Existe para bajar el peso del bloque de skills sin borrar el
    # SKILL.md (ver el presupuesto de tokens en la tab Skills).
    enabled: bool = True

    def render_bullet(self) -> str:
        return f"- **{self.name}**: {self.description}"

@dataclass
class _Index:
    skills: tuple[Skill, ...]
    fetched_at: float
    dir_existed: bool  # para loggear una sola vez si no existe

    def auto_skills(self) -> tuple[Skill, ...]:
        """Skills que SÍ se inyectan automáticamente en el system prompt."""
        return tuple(s for s in self.skills if s.enabled and not s.manual)

    def manual_skills(self) -> tuple[Skill, ...]:
        """On-demand: `when: manual` y prendidas.

        Al system prompt van SOLO por nombre (ver `render_block`); el
        cuerpo se pide con `read_skill`. `enabled` se filtra acá: una
        skill apagada no se ofrece ni siquiera on-demand.
        """
        return tuple(s for s in self.skills if s.enabled and s.manual)

def _parse_skill_md(path: Path, dir_name: str) -> Optional[Skill]:
    """Lee un SKILL.md y devuelve Skill, o None si está roto.

    Saltea con warning si:
    - no se puede leer (permisos / encoding)
    - no tiene frontmatter
    - el frontmatter está vacío y no podemos derivar nada
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("skills: no pude leer %s: %r (salteando)", path, e)
        return None

    m = _FRONTMATTER_RE.match(text)
    if not m:
        logger.warning("skills: %s sin frontmatter (salteando)", path)
        return None

    kv: dict[str, str] = {}
    for line in m.group(1).splitlines():
        km = _KV_RE.match(line)
        if km:
            kv[km.group(1).lower()] = _unquote(km.group(2).strip())

    # name: frontmatter > dir. Si difieren, warning pero usamos frontmatter.
    name = (kv.get("name") or dir_name).strip()
    if name != dir_name and kv.get("name"):
        logger.warning(
            "skills: %s tiene name=%r (distinto del dir %r); uso el del frontmatter",
            path, name, dir_name,
        )

    description = kv.get("description", "").strip()
    if not description:
        logger.info("skills: %s sin description (uso string vacío)", path)

    # Convención on-demand: frontmatter `when: manual` (o `when: ondemand`).
    # La skill se lista pero NO se inyecta automáticamente en el system
    # prompt — el caller debe pedirla explícitamente (botón UI, comando,
    # dispatch desde un endpoint). Ver `apply_skill_to_voice_transcript`
    # en admin.py para el caso de uso actual.
    when = kv.get("when", "auto").strip().lower()
    manual = when in ("manual", "ondemand", "on_demand", "on-demand")

    # `enabled: false` apaga la skill sin borrarla (toggle de la Admin UI).
    # Default true: los SKILL.md que no lo declaran siguen inyectándose.
    enabled = kv.get("enabled", "true").strip().lower() not in (
        "false", "0", "no", "off")

    return Skill(name=name, description=description, manual=manual,
                 enabled=enabled)

def _build_index(skills_dir: Path) -> _Index:
    """Lee el directorio y devuelve un _Index nuevo. Sync (se llama via to_thread)."""
    if not skills_dir.exists():
        return _Index(skills=(), fetched_at=time.monotonic(), dir_existed=False)

    skills: list[Skill] = []
    try:
        children = sorted(skills_dir.iterdir())
    except OSError as e:
        logger.warning("skills: no pude listar %s: %r", skills_dir, e)
        return _Index(skills=(), fetched_at=time.monotonic(), dir_existed=True)

    for child in children:
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.exists():
            continue
        s = _parse_skill_md(skill_md, child.name)
        if s is not None:
            skills.append(s)

    return _Index(skills=tuple(skills), fetched_at=time.monotonic(), dir_existed=True)

def skills_dirs(repo_path: str = "") -> list[Path]:
    """Directorios de skills en orden de PRIORIDAD: el del repo primero.

    Hasta 2026-09-08 esto era una sola constante global
    (`~/.copilot/skills`) para los 50 proyectos registrados. Consecuencia
    medida: `4bis.relay/.claude/skills/relay-ui/SKILL.md` —9.536 chars,
    escrita a medida para el panel de este repo, la unica skill nuestra
    que existe— era invisible para el experto del propio relay. Al mismo
    tiempo, cada turno de un repo de Python anunciaba `unocss`, `vite`,
    `tsdown` y `dotnet-best-practices`.

    Una skill del repo gana sobre la global con el mismo nombre: es mas
    especifica por construccion, y es la que el equipo puede versionar
    junto al codigo que describe.

    Sin `repo_path`, o si el repo no tiene el directorio, devuelve solo
    la global: el comportamiento de antes, intacto.
    """
    dirs: list[Path] = []
    if repo_path:
        try:
            propio = Path(repo_path).expanduser() / ".claude" / "skills"
            if propio.is_dir():
                dirs.append(propio)
        except (OSError, ValueError) as e:  # path raro en la fila
            logger.warning("skills: repo_path invalido %r (%r)", repo_path, e)
    dirs.append(resolve_skills_dir())
    return dirs

def build_index_multi(dirs: list[Path]) -> _Index:
    """Un indice con varios directorios, el PRIMERO gana por nombre.

    El desempate es por `name` del frontmatter y no por nombre de
    carpeta: es el identificador que el modelo ve y el que `read_skill`
    resuelve, asi que empatar por carpeta dejaria dos skills con el
    mismo `name` visible.
    """
    vistos: set[str] = set()
    juntas: list[Skill] = []
    existio = False
    for d in dirs:
        idx = _build_index(d)
        existio = existio or idx.dir_existed
        for sk in idx.skills:
            clave = (sk.name or "").strip().lower()
            if clave and clave in vistos:
                continue
            vistos.add(clave)
            juntas.append(sk)
    return _Index(skills=tuple(juntas), fetched_at=time.monotonic(),
                  dir_existed=existio)

def read_skill_multi(dirs: list[Path], name: str) -> Optional[str]:
    """Lee el cuerpo de una skill probando cada directorio en orden.

    Mismo criterio de prioridad que `build_index_multi`: si el repo
    tiene una skill con ese nombre, gana sobre la global.
    """
    from .skills_store import read_skill_sync, resolve_skill_dir

    for d in dirs:
        dir_name = resolve_skill_dir(d, name)
        if dir_name is None:
            continue
        contenido = read_skill_sync(d, dir_name)
        if contenido is not None:
            return contenido
    return None

ONDEMAND_PREFIX = (
    "On-demand (solo el nombre; si una aplica, pide el cuerpo con "
    "`read_skill(name)` antes de responder): "
)

def render_ondemand_line(manual: tuple[Skill, ...]) -> str:
    return ONDEMAND_PREFIX + ", ".join(f"`{s.name}`" for s in manual)

ONDEMAND_HEADER = (
    "## Skills on-demand (si una aplica, pide el cuerpo con "
    "`read_skill(name)` antes de responder)"
)

def render_ondemand_block(manual: tuple) -> str:
    """Las `when: manual` con su descripcion, no solo el nombre.

    Hasta 2026-09-08 viajaban como una linea de nombres sueltos
    (`render_ondemand_line`, que queda para el modo minimal). La
    descripcion EXISTE en el frontmatter de las 15 y se descartaba: el
    modelo veia `antfu`, `tsdown`, `unocss` y tenia que adivinar si
    alguna le servia. Medido: `read_skill` se llamo en 57 de 1.132 chats
    (5%), y ese numero no dice que las skills no sirvan — dice que el
    modelo no se entera de que hacen.

    El costo NO es despreciable (+851 tokens por turno con las 17
    globales) y por eso esto llega DESPUES del filtro por proyecto (ver
    `bloque_del_proyecto`): pagar la descripcion de `unocss` en un repo
    de Python es comprar ruido caro.
    """
    if not manual:
        return ""
    return "\n".join([ONDEMAND_HEADER, ""]
                      + [sk.render_bullet() for sk in manual])

def render_block(index: _Index) -> str:
    """Compone el bloque markdown a inyectar en el `system`. Vacío si no hay skills.

    Dos secciones:
    - `auto`: bullet completo (name + description). El experto decide
      con la descripción.
    - `manual`: bullet completo tambien (2026-09-08), con la instruccion
      de pedir el cuerpo con `read_skill` si aplica. Antes iban como
      nombres sueltos y el experto tenia que adivinar: la descripcion
      estaba en el frontmatter y se tiraba. Cuesta ~851 tokens por turno
      con las 17 globales, y por eso conviene declarar
      `defaults_json.skills` (ver `bloque_del_proyecto`).
    """
    auto = index.auto_skills() if index else ()
    manual = index.manual_skills() if index else ()
    if not auto and not manual:
        return ""
    lines: list[str] = []
    if auto:
        lines += [
            "## Skills disponibles (lee la que aplique antes de responder)",
            "",
        ]
        lines.extend(s.render_bullet() for s in auto)
    if manual:
        if lines:
            lines.append("")
        lines.append(render_ondemand_block(manual))
    return "\n".join(lines)

def bloque_del_proyecto(repo_path: str,
                        permitidas: Optional[list] = None) -> str:
    """El bloque de skills de UN proyecto: las del repo + las globales
    que ese proyecto declare.

    Existe porque el bloque que llega por parametro a
    `build_instructions` sale de un `SkillCache` construido con el
    directorio GLOBAL y compartido por los 50 proyectos: no puede ver
    las skills del repo ni respetar un filtro por proyecto.

    `permitidas` es `defaults_json.skills`:
      - `None` (la clave no esta) -> todas las globales, como siempre.
      - lista de nombres -> solo esas globales.
      - lista vacia -> ninguna global. Para apagar todo esta
        `inject_skills: false`, que es mas explicito.

    **Las skills del repo entran SIEMPRE**, esten o no en la lista: una
    skill versionada junto al codigo que describe ya declaro para que
    repo es. Pedir que ademas se la nombre seria burocracia que se
    desincroniza sola.

    El desempate y el orden los pone `build_index_multi` (el repo gana
    por `name`). Los nombres declarados que no existen se anotan en el
    bloque, mismo criterio que `render_requested_block`: un typo que
    nadie ve es una skill que nunca se usa y nadie sabe por que.
    """
    dirs = skills_dirs(repo_path)
    del_repo: set[str] = set()
    if len(dirs) > 1:
        del_repo = {(sk.name or "").strip().lower()
                    for sk in _build_index(dirs[0]).skills}
    index = build_index_multi(dirs)
    if permitidas is None:
        return render_block(index)

    pedidas = {str(n).strip().lower() for n in permitidas if str(n).strip()}
    quedan = tuple(
        sk for sk in index.skills
        if (sk.name or "").strip().lower() in pedidas
        or (sk.name or "").strip().lower() in del_repo
    )
    filtrado = _Index(skills=quedan, fetched_at=index.fetched_at,
                      dir_existed=index.dir_existed)
    bloque = render_block(filtrado)

    existentes = {(sk.name or "").strip().lower() for sk in index.skills}
    faltan = [n for n in pedidas if n not in existentes]
    if faltan:
        nota = ("Nota para el usuario: este proyecto declara skills que no "
                "existen: " + ", ".join(f"`{n}`" for n in sorted(faltan))
                + " (revisa `defaults_json.skills`).")
        bloque = f"{bloque}\n\n{nota}" if bloque else nota
    return bloque

SKILL_INDEX_HEADER_COMPACT = (
    "## Skills (on-demand)\n"
    "Para el cuerpo de una: `read_skill(name)`. NO adivines el contenido."
)

def render_index_compact(index: _Index) -> str:
    """Bloque minimal: lista de nombres + instrucción para `read_skill`.

    Usar cuando `defaults_json.skills_mode == "compact"`. Cada nombre
    en su línea (1 token c/u); sin descripción. Espera que el LLM
    invoque `read_skill` para el cuerpo cuando una skill matchea.

    `auto` y `manual` van juntas y sin distinguir: en compact TODAS son
    on-demand (nadie lleva descripción), así que separarlas no le diría
    nada al modelo.
    """
    names = ((index.auto_skills() + index.manual_skills())
             if index else ())
    if not names:
        return ""
    lines = [SKILL_INDEX_HEADER_COMPACT, ""]
    lines.extend(f"- `{s.name}`" for s in names)
    return "\n".join(lines)

REQUESTED_SKILL_CAP = 20_000

def render_requested_block(skills_dir: Path, names: list[str]) -> str:
    """Bloque para `--skill <name>`: inyecta el SKILL.md completo de cada
    skill pedida explícitamente, aunque tenga `when: manual` (que si no NO
    se auto-inyecta en el system prompt). Paralelo a `--con` para MCPs: el
    usuario fuerza una capacidad al run.

    Resuelve cada nombre contra el dir O el `name` del frontmatter
    (case-insensitive). Los no encontrados quedan anotados para que el
    humano vea el typo. Sync (el caller lo corre en to_thread)."""
    from .skills_store import read_skill_sync, resolve_skill_dir

    if not names:
        return ""
    chunks: list[str] = []
    missing: list[str] = []
    seen: set[str] = set()
    for raw in names:
        dir_name = resolve_skill_dir(skills_dir, raw)
        if dir_name is None:
            missing.append(raw)
            continue
        if dir_name in seen:
            continue  # pedida dos veces (ej. por dir y por name)
        seen.add(dir_name)
        content = read_skill_sync(skills_dir, dir_name)
        if content is None:
            missing.append(raw)
            continue
        content = content.strip()
        if len(content) > REQUESTED_SKILL_CAP:
            content = content[:REQUESTED_SKILL_CAP] + "\n\n[…truncado]"
        chunks.append(content)
    lines: list[str] = []
    if chunks:
        lines += [
            "## Skill(s) pedida(s) explícitamente por el usuario (`--skill`)",
            "",
            "Aplicá esta(s) skill(s) en tu respuesta, tengan o no "
            "`when: manual`:",
            "",
            "\n\n---\n\n".join(chunks),
        ]
    if missing:
        if lines:
            lines.append("")
        lines.append(
            "Nota para el usuario: no encontré la(s) skill(s) "
            + ", ".join(f"`{m}`" for m in missing)
            + " (revisá el nombre con `!ayuda`).")
    return "\n".join(lines)
