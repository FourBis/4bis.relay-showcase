"""Índice de skills de Copilot Chat para inyectar en el system prompt.

Lee `~/.copilot/skills/*/SKILL.md`, parsea el frontmatter YAML y arma
un bloque compacto que el relay agrega al `system` de cada push.

ADR-010 (2026-07-06): la fuente canónica es el directorio del usuario,
mismo que Copilot Chat nativo y `dwaintr.superpowers-vscode` leen.

Diseño (Ponytail, lite):
- Cache TTL 60s, una sola instancia global por proceso.
- Best-effort: cualquier falla (FS, parse) loggeamos y seguimos sin
  inyectar. NUNCA romper un push por una skill rota.
- Regex simple para frontmatter (no PyYAML): solo necesitamos `name`
  y `description` del bloque `---...---`. El resto del archivo
  (descripción completa, instrucciones) queda como upgrade path.

Upgrade path (no implementado):
- Inyección completa del SKILL.md cuando una skill matchea.
- Hot reload por mtime, whitelist/blacklist, dir configurable.
- Hoy: ruta hardcodeada, refresh lazy cada 60s.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
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

# Captura el bloque `--- ... ---` al inicio del archivo.
# DOTALL porque las líneas YAML pueden tener saltos; no nos importa
# lo que haya después del cierre.
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
# Captura `key: value` simple. Sin YAML completo: no listas, no
# comillas anidadas, no multilínea. Suficiente para `name`/`description`.
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
    for d in dirs:
        dir_name = resolve_skill_dir(d, name)
        if dir_name is None:
            continue
        contenido = read_skill_sync(d, dir_name)
        if contenido is not None:
            return contenido
    return None


# Las `when: manual` entran al prompt SOLO por nombre (2026-08-16). Antes
# no entraban de ninguna forma: `read_skill` podía leerlas, pero el
# experto nunca veía el nombre, así que 14 de 18 skills instaladas eran
# invisibles. Un nombre cuesta ~3 tokens contra ~50 del bullet completo,
# que es justo lo que `manual` quería evitar.
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


# Iter 10.4: modo "compact" para reducir el system prompt.
# En vez de inyectar nombre+descripción de CADA skill, inyectamos solo
# los nombres y le decimos al LLM que llame `read_skill(name)` cuando
# necesite el cuerpo. Ahorro típico: ~80% del bloque de skills.
# Ponytail: el contenido completo se sigue leyendo por la tool nativa,
# no por embedding. Si la skill no se necesita, ni se paga.
# Header corto a propósito: el bloque se inyecta en CADA turno, cada
# char cuenta. La descripción detallada de la tool va en el docstring
# de `read_skill` (que el LLM lee cuando la invoca), no acá.
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


# Cap por skill inyectada vía `--skill` (mismo tope que la tool read_skill).
REQUESTED_SKILL_CAP = 20_000


def render_requested_block(skills_dir: Path, names: list[str]) -> str:
    """Bloque para `--skill <name>`: inyecta el SKILL.md completo de cada
    skill pedida explícitamente, aunque tenga `when: manual` (que si no NO
    se auto-inyecta en el system prompt). Paralelo a `--con` para MCPs: el
    usuario fuerza una capacidad al run.

    Resuelve cada nombre contra el dir O el `name` del frontmatter
    (case-insensitive). Los no encontrados quedan anotados para que el
    humano vea el typo. Sync (el caller lo corre en to_thread)."""
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


class SkillCache:
    """Cache global del índice de skills. Una sola instancia por proceso.

    Uso:
        cache = SkillCache()
        block = await cache.get_block()  # "" o markdown listo para concatenar
    """

    def __init__(self, skills_dir: Optional[Path] = None, ttl_s: float = CACHE_TTL_S) -> None:
        self._dir = skills_dir if skills_dir is not None else resolve_skills_dir()
        self._ttl = ttl_s
        self._index: Optional[_Index] = None
        self._notified_missing = False  # para loggear el "no existe" UNA vez
        self._lock = asyncio.Lock()

    @property
    def dir(self) -> Path:
        return self._dir

    def invalidate(self) -> None:
        """Fuerza refresh en el próximo get_block (aprobar/borrar una
        skill desde la Admin UI no debería esperar el TTL de 60s)."""
        self._index = None

    async def get_block(self) -> str:
        """Devuelve el bloque markdown ("" si no hay skills o falló el refresh).

        Refresca si la cache es None o si pasó el TTL. Si el refresh
        falla por excepción, devolvemos lo que haya (incluso vacío) y
        NO propagamos.
        """
        async with self._lock:
            if self._index is None or (time.monotonic() - self._index.fetched_at) > self._ttl:
                await self._refresh()
            return render_block(self._index) if self._index else ""

    async def _refresh(self) -> None:
        """Lee el disco y actualiza self._index. Tolerante a fallos."""
        try:
            new_index = await asyncio.to_thread(_build_index, self._dir)
        except Exception as e:
            # FS roto, permisos, lo que sea. Log y no rompemos.
            logger.warning("skills: refresh falló (%r); sigo con cache previa", e)
            if self._index is None:
                # primera vez y falló: inicializamos vacío para no intentar
                # de nuevo en cada push hasta que pase el TTL.
                self._index = _Index(skills=(), fetched_at=time.monotonic(), dir_existed=False)
            return

        # logging una sola vez por estado (no spam en cada push)
        if not new_index.dir_existed and not self._notified_missing:
            logger.info("skills: %s no existe; no inyecto bloque", self._dir)
            self._notified_missing = True
        elif new_index.dir_existed and self._index is None:
            logger.info("skills: %d skills cargadas desde %s",
                        len(new_index.skills), self._dir)

        self._index = new_index


# ---------- escritura / gestión (autoaprendizaje, 2026-07-12) ----------
# El compactador (memory.py) destila borradores de skill; la Admin UI los
# aprueba y estos helpers los escriben al directorio canónico, donde el
# SkillCache los levanta solo. Todo sync — se llama vía asyncio.to_thread.

_NAME_UNSAFE_RE = re.compile(r"[^a-z0-9-]+")


def sanitize_skill_name(name: str) -> str:
    """Normaliza a kebab-case seguro para nombre de carpeta.

    Es también la barrera anti path-traversal: el resultado solo puede
    tener [a-z0-9-], así que "../x" o "a\\b" no sobreviven.
    """
    name = (name or "").strip().lower().replace(" ", "-").replace("_", "-")
    name = _NAME_UNSAFE_RE.sub("-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name[:64]


def _safe_skill_dir(base_dir: Path, dir_name: str) -> Optional[Path]:
    """Resuelve base_dir/dir_name validando que quede DENTRO de base_dir.

    Para operar sobre skills existentes (ver/borrar), cuyo dir puede no
    ser kebab-case (creadas a mano). None si el nombre es sospechoso.
    """
    if not dir_name or any(c in dir_name for c in ("/", "\\", "..")):
        return None
    target = base_dir / dir_name
    if target.parent != base_dir:
        return None
    return target


def render_skill_md(name: str, description: str, content: str,
                    *, frontmatter_extra: str = "") -> str:
    """Arma el SKILL.md con el frontmatter mínimo que lee _parse_skill_md.

    `frontmatter_extra` (opcional) son líneas extra del frontmatter
    que se agregan después de `description`. Se usan para campos
    adicionales que el caller quiere fijar (`when: manual`, etc.).
    Las líneas tienen que venir YA formateadas con `\n` final cada una;
    si vienen vacías no se agrega nada. Default: "" (no cambia el
    comportamiento previo).
    """
    desc = " ".join((description or "").split())  # una sola línea
    extra = ""
    if frontmatter_extra.strip():
        # asegurar terminación con \n si el caller no lo hizo.
        lines = [ln for ln in frontmatter_extra.splitlines() if ln.strip()]
        extra = "\n" + "\n".join(lines)
    return (
        f"---\nname: {name}\ndescription: {desc}{extra}\n---\n\n"
        f"{(content or '').strip()}\n"
    )


def write_skill_sync(
    name: str, description: str, content: str,
    base_dir: Path, *, overwrite: bool = False,
    frontmatter_extra: str = "",
) -> Path:
    """Escribe <base_dir>/<name-sanitizado>/SKILL.md y devuelve su path.

    Raises:
        ValueError:      nombre vacío tras sanitizar.
        FileExistsError: ya hay una skill con ese nombre y overwrite=False.
    """
    safe = sanitize_skill_name(name)
    if not safe:
        raise ValueError(f"nombre de skill inválido: {name!r}")
    target = base_dir / safe / "SKILL.md"
    if target.exists() and not overwrite:
        raise FileExistsError(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_skill_md(safe, description, content,
                                      frontmatter_extra=frontmatter_extra),
                      encoding="utf-8")
    return target


def list_skills_sync(base_dir: Path) -> list[dict]:
    """Skills instaladas con metadata para la Admin UI (no para el prompt)."""
    if not base_dir.is_dir():
        return []
    out: list[dict] = []
    for child in sorted(base_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if not child.is_dir() or not skill_md.is_file():
            continue
        parsed = _parse_skill_md(skill_md, child.name)
        try:
            st = skill_md.stat()
        except OSError:
            continue
        out.append({
            "dir": child.name,
            "name": parsed.name if parsed else child.name,
            "description": parsed.description if parsed else "",
            "valid": parsed is not None,
            "manual": bool(parsed.manual) if parsed else False,
            "enabled": bool(parsed.enabled) if parsed else False,
            "state": skill_state(parsed) if parsed else "off",
            # Lo que esta skill le cuesta al system prompt: SOLO el bullet
            # (name + description), no el SKILL.md entero. Es la unidad del
            # presupuesto que muestra la UI.
            "bullet_tokens": (
                estimate_tokens(parsed.render_bullet()) if parsed else 0),
            "size": st.st_size,
            "modified_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
        })
    return out


def resolve_skill_dir(base_dir: Path, name: str) -> Optional[str]:
    """Nombre pedido → directorio instalado. None si no matchea ninguno.

    Acepta el nombre del directorio O el `name:` del frontmatter,
    case-insensitive. Hacen falta las dos formas: la Admin UI manda el
    dir, pero el LLM ve el del frontmatter (es lo que rinde el índice
    del system prompt) y son distintos cada vez que alguien importa una
    skill cuya carpeta no coincide con su `name:`.

    El dir gana si hay empate: es el identificador que usa el resto de
    la API (borrar, togglear, editar).
    """
    key = (name or "").strip().lower()
    if not key:
        return None
    installed = list_skills_sync(base_dir)
    for s in installed:
        if s["dir"].lower() == key:
            return s["dir"]
    for s in installed:
        if (s.get("name") or "").lower() == key:
            return s["dir"]
    return None


def _read_skill_md(base_dir: Path, dir_name: str) -> Optional[str]:
    """SKILL.md de un directorio EXACTO. None si no existe / nombre raro."""
    target = _safe_skill_dir(base_dir, dir_name)
    if target is None:
        return None
    try:
        return (target / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return None


def read_skill_sync(base_dir: Path, dir_name: str) -> Optional[str]:
    """Contenido completo del SKILL.md, o None si no existe / nombre raro.

    Acepta dir o `name:` del frontmatter (ver `resolve_skill_dir`). El
    path directo va primero y corta ahí: es el caso normal, y el
    resolver parsea TODOS los SKILL.md del directorio. Solo se paga
    cuando el path directo falla.
    """
    direct = _read_skill_md(base_dir, dir_name)
    if direct is not None:
        return direct
    resolved = resolve_skill_dir(base_dir, dir_name)
    if resolved is None or resolved == dir_name:
        return None  # ya lo intentamos por path directo
    return _read_skill_md(base_dir, resolved)


def overwrite_skill_sync(
    base_dir: Path, dir_name: str, content: str,
) -> Optional[bool]:
    """Pisa el SKILL.md de una skill instalada con `content` crudo.

    A diferencia de `write_skill_sync`, NO re-renderiza el frontmatter:
    el humano edita el archivo entero (incluido `enabled:`/`when:`), que
    es justo lo que hace falta para corregir una descripción gorda o un
    frontmatter roto desde la UI.

    Deja `SKILL.md.bak` con la versión anterior — esto entra al system
    prompt de cada run y un guardado en falso sin copia se paga caro.

    Devuelve True si escribió, None si la skill no existe (o el nombre
    es sospechoso: `_safe_skill_dir` valida el path traversal).
    """
    target = _safe_skill_dir(base_dir, dir_name)
    if target is None or not target.is_dir():
        return None
    skill_md = target / "SKILL.md"
    if not skill_md.is_file():
        return None
    shutil.copy2(skill_md, skill_md.with_suffix(".md.bak"))
    skill_md.write_text(content, encoding="utf-8")
    return True


def delete_skill_sync(base_dir: Path, dir_name: str) -> bool:
    """Borra el directorio entero de la skill. True si existía y borró."""
    target = _safe_skill_dir(base_dir, dir_name)
    if target is None or not target.is_dir():
        return False
    shutil.rmtree(target)
    return True


# ---------- estado de una skill (2026-07-25) ----------
# Tres estados, dos campos del frontmatter:
#   auto   → enabled: true,  when: auto    (entra al system prompt)
#   manual → enabled: true,  when: manual  (on-demand: se dispara a mano)
#   off    → enabled: false                (no entra ni se ofrece)

SKILL_STATES = ("auto", "manual", "off")


def skill_state(s: Skill) -> str:
    """Estado legible de una Skill parseada (el inverso de set_skill_state)."""
    if not s.enabled:
        return "off"
    return "manual" if s.manual else "auto"


def _set_frontmatter_keys(skill_md: Path, keys: dict[str, str]) -> bool:
    """Reescribe `key: value` en el frontmatter. True si aplicó.

    Toca SOLO esas líneas (o las agrega al final del bloque `---`): el
    resto del archivo —contenido, campos anidados, formato— queda
    intacto. False si no se puede leer o no hay frontmatter que editar.
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return False
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return False

    block = m.group(1)
    for key, value in keys.items():
        # `^key:` sin indentar — así no pisamos una key homónima que
        # cuelgue de un bloque anidado (p.ej. `metadata:\n  when: x`).
        line_re = re.compile(rf"^{re.escape(key)}\s*:.*$", re.MULTILINE)
        if line_re.search(block):
            block = line_re.sub(f"{key}: {value}", block, count=1)
        else:
            block = block.rstrip("\n") + f"\n{key}: {value}"
    new_text = text[:m.start(1)] + block + text[m.end(1):]
    try:
        skill_md.write_text(new_text, encoding="utf-8")
    except OSError:
        return False
    return True


def set_skill_state_sync(base_dir: Path, dir_name: str, state: str) -> bool:
    """Pone la skill en `auto` / `manual` / `off`. True si aplicó.

    False si el estado es inválido, el nombre es sospechoso, no existe,
    o su SKILL.md no tiene frontmatter.
    """
    if state not in SKILL_STATES:
        return False
    target = _safe_skill_dir(base_dir, dir_name)
    if target is None:
        return False
    # `off` no toca `when`: si la skill era on-demand y la apagas, al
    # prenderla de nuevo eliges explícitamente en qué estado vuelve.
    keys = {"off": {"enabled": "false"},
            "auto": {"enabled": "true", "when": "auto"},
            "manual": {"enabled": "true", "when": "manual"}}[state]
    return _set_frontmatter_keys(target / "SKILL.md", keys)


def set_skill_enabled_sync(base_dir: Path, dir_name: str,
                           enabled: bool) -> bool:
    """Prende/apaga sin tocar `when`. Atajo sobre set_skill_state_sync."""
    if enabled:
        target = _safe_skill_dir(base_dir, dir_name)
        if target is None:
            return False
        return _set_frontmatter_keys(target / "SKILL.md",
                                     {"enabled": "true"})
    return set_skill_state_sync(base_dir, dir_name, "off")


# ---------- presupuesto de tokens (2026-07-25) ----------

# ponytail: estimador naive chars/4 — no es el tokenizer del modelo, pero
# el error (~10-15%) no cambia ninguna decisión de "recorta skills". Si
# algún día importa el número exacto, cambiar por tiktoken/el del provider.
def estimate_tokens(text: str) -> int:
    return (len(text or "") + 3) // 4


# Techo sugerido del bloque de skills (el índice name+description que va
# en CADA push). No es un límite duro: la UI pinta la barra en ámbar/rojo
# al pasarlo. Env-overridable por si el modelo cambia de contexto.
def skills_token_budget() -> int:
    try:
        return max(1, int(os.environ.get("FOURBIS_SKILLS_TOKEN_BUDGET", "1200")))
    except ValueError:
        return 1200


# Techo del overhead FIJO del system prompt (ponytail + skills + fallback
# de tools). Es lo que se paga en cada llamada antes de escribir una
# palabra útil.
def prompt_token_budget() -> int:
    try:
        return max(1, int(os.environ.get("FOURBIS_PROMPT_TOKEN_BUDGET", "5000")))
    except ValueError:
        return 5000


# ---------- alta desde GitHub (2026-07-25) ----------
# Mismo espíritu que el installer de MCPs (mcp_installer.py) pero mucho
# más liviano: una skill es markdown, no un proceso. No hay handshake ni
# vetting LLM — clonamos, listamos los SKILL.md que encontramos, el humano
# tilda cuáles quiere y copiamos esos directorios. Nada se ejecuta.
#
# El riesgo real no es RCE: es prompt injection (un SKILL.md hostil entra
# al system prompt). Por eso el install es opt-in por skill y la UI te
# deja leer el contenido antes.

# Catálogo curado para el dropdown de la UI. Lista corta a propósito:
# es un atajo, no un registry. Pegar cualquier otra URL sigue andando.
GITHUB_CATALOG = [
    {"url": "https://github.com/anthropics/skills",
     "label": "anthropics/skills — oficiales (docx, pdf, pptx, xlsx, +)"},
    {"url": "https://github.com/github/awesome-copilot",
     "label": "github/awesome-copilot — 200+ skills"},
    {"url": "https://github.com/antfu/skills",
     "label": "antfu/skills — frontend/TS"},
    {"url": "https://github.com/addyosmani/agent-skills",
     "label": "addyosmani/agent-skills — ingeniería"},
    {"url": "https://github.com/wshobson/agents",
     "label": "wshobson/agents — 175 skills en plugins"},
]

# Tope de profundidad del walk. wshobson/agents anida
# plugins/<x>/skills/<y>/SKILL.md = 4 niveles; 6 da aire sin barrer
# node_modules enteros.
_SCAN_MAX_DEPTH = 6
_SCAN_SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", "dist",
                   "build", ".github"}
# Una skill sana pesa KBs. Si trae 20MB es un repo entero mal empaquetado.
SKILL_MAX_BYTES = 5 * 1024 * 1024


def _dir_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def scan_repo_skills(clone_dir: Path) -> list[dict]:
    """Encuentra los SKILL.md del clon y los devuelve parseados.

    Cada item trae `rel_path` (el directorio de la skill, relativo al
    clon, en POSIX) que es lo que la UI manda de vuelta para instalar.
    """
    found: list[dict] = []
    root_depth = len(clone_dir.parts)

    def walk(d: Path) -> None:
        if len(d.parts) - root_depth > _SCAN_MAX_DEPTH:
            return
        try:
            children = sorted(d.iterdir())
        except OSError:
            return
        skill_md = d / "SKILL.md"
        if skill_md.is_file():
            parsed = _parse_skill_md(skill_md, d.name)
            rel = d.relative_to(clone_dir).as_posix()
            size = _dir_bytes(d)
            found.append({
                "rel_path": rel or ".",
                "dir": d.name,
                "name": parsed.name if parsed else d.name,
                "description": parsed.description if parsed else "",
                "valid": parsed is not None,
                "size": size,
                "too_big": size > SKILL_MAX_BYTES,
                "bullet_tokens": (
                    estimate_tokens(parsed.render_bullet()) if parsed else 0),
            })
            return  # no anidamos skills dentro de skills
        for child in children:
            if child.is_dir() and child.name not in _SCAN_SKIP_DIRS:
                walk(child)

    walk(clone_dir)
    found.sort(key=lambda s: s["name"].lower())
    return found


def read_skill_md_from_clone(clone_dir: Path, rel_path: str) -> Optional[str]:
    """SKILL.md de una skill del clon (preview antes de instalar)."""
    src = _resolve_in_clone(clone_dir, rel_path)
    if src is None:
        return None
    try:
        return (src / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return None


def _resolve_in_clone(clone_dir: Path, rel_path: str) -> Optional[Path]:
    """Resuelve rel_path DENTRO del clon. None si se escapa (traversal)."""
    if not rel_path or rel_path.startswith(("/", "\\")):
        return None
    try:
        target = (clone_dir / rel_path).resolve()
        base = clone_dir.resolve()
    except OSError:
        return None
    if target != base and base not in target.parents:
        return None
    if not target.is_dir() or not (target / "SKILL.md").is_file():
        return None
    return target


def install_skill_from_clone(
    clone_dir: Path, rel_path: str, base_dir: Path, *,
    overwrite: bool = False, enabled: bool = True,
) -> str:
    """Copia <clone>/<rel_path> a <base_dir>/<nombre>. Devuelve el nombre.

    El nombre sale del frontmatter (o del dir) y pasa por
    `sanitize_skill_name`, que es la barrera anti-traversal del destino.

    Raises:
        ValueError:      rel_path fuera del clon, nombre inválido o skill
                         demasiado pesada (>SKILL_MAX_BYTES).
        FileExistsError: ya hay una skill con ese nombre y overwrite=False.
    """
    src = _resolve_in_clone(clone_dir, rel_path)
    if src is None:
        raise ValueError(f"ruta inválida en el clon: {rel_path!r}")
    if _dir_bytes(src) > SKILL_MAX_BYTES:
        raise ValueError(
            f"{rel_path!r} pesa más de {SKILL_MAX_BYTES // 1024 // 1024}MB; "
            "no parece una skill")

    parsed = _parse_skill_md(src / "SKILL.md", src.name)
    safe = sanitize_skill_name(parsed.name if parsed else src.name)
    if not safe:
        raise ValueError(f"nombre de skill inválido: {src.name!r}")

    dest = base_dir / safe
    if dest.exists():
        if not overwrite:
            raise FileExistsError(safe)
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # ignore de .git por si la skill es un repo entero (submódulo suelto).
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))
    if not enabled:
        set_skill_enabled_sync(base_dir, safe, False)
    return safe


def skill_installs_root() -> Path:
    """Donde se clonan los repos mientras eliges qué instalar."""
    root = Path(os.environ.get(
        "FOURBIS_SKILL_INSTALLS_DIR",
        str(Path.home() / ".4bis" / "skill-installs")))
    root.mkdir(parents=True, exist_ok=True)
    return root


@dataclass
class BrowseJob:
    id: str
    url: str
    state: str = "pending"     # pending|cloning|scanning|ready|failed
    clone_dir: str = ""
    commit: str = ""
    skills: list[dict] = field(default_factory=list)
    error: str = ""
    created_at: float = field(default_factory=time.time)

    def to_public(self) -> dict:
        return {
            "job_id": self.id, "url": self.url, "state": self.state,
            "commit": self.commit, "skills": self.skills,
            "error": self.error,
        }


# El clon vive mientras eliges. Pasada esta ventana lo barremos: son
# copias de repos públicos, nada que preservar.
_JOB_TTL_S = 3600.0


class SkillBrowser:
    """Clona un repo en background y lista sus SKILL.md para elegir.

    Los jobs viven en memoria (como McpInstaller): si el relay cae, se
    pierden y la UI dice "expiró, prueba de nuevo".
    """

    def __init__(self) -> None:
        self._jobs: dict[str, BrowseJob] = {}
        self._lock = asyncio.Lock()

    def get(self, job_id: str) -> Optional[BrowseJob]:
        return self._jobs.get(job_id)

    async def start(self, url: str) -> BrowseJob:
        url = (url or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")
                or url.startswith("git@")):
            raise ValueError("URL debe ser http(s) o git@")
        self._sweep()
        slug = re.sub(r"[^a-z0-9_\-]", "-",
                      url.rstrip("/").rsplit("/", 1)[-1].lower()).strip("-")
        job_id = uuid.uuid4().hex[:12]
        job = BrowseJob(
            id=job_id, url=url,
            clone_dir=str(skill_installs_root() / f"{slug or 'repo'}-{job_id}"))
        async with self._lock:
            self._jobs[job_id] = job
        asyncio.create_task(self._run(job))
        return job

    def discard(self, job_id: str) -> bool:
        job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        _rmtree_quiet(Path(job.clone_dir))
        return True

    def _sweep(self) -> None:
        """Borra jobs (y clones) vencidos. Barato: corre al crear uno nuevo."""
        now = time.time()
        for jid in [j.id for j in self._jobs.values()
                    if now - j.created_at > _JOB_TTL_S]:
            self.discard(jid)

    async def _run(self, job: BrowseJob) -> None:
        from .mcp_installer import clone_repo  # mismo clone pineado
        try:
            job.state = "cloning"
            job.commit = await clone_repo(job.url, Path(job.clone_dir))
            job.state = "scanning"
            job.skills = await asyncio.to_thread(
                scan_repo_skills, Path(job.clone_dir))
            job.state = "ready"
            logger.info("skills: %s → %d skills encontradas",
                        job.url, len(job.skills))
        except Exception as e:  # noqa: BLE001
            logger.warning("skills: browse de %s falló: %r", job.url, e)
            job.state = "failed"
            job.error = str(e)
            _rmtree_quiet(Path(job.clone_dir))


def _rmtree_quiet(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass