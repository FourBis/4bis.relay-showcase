"""Persistencia local, estados y presupuesto de tokens de skills."""
from __future__ import annotations
import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("relay.skills")

from .skills_index import (
    CACHE_TTL_S, Skill, _FRONTMATTER_RE, _Index, _build_index,
    _parse_skill_md, render_block, resolve_skills_dir,
)

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

def estimate_tokens(text: str) -> int:
    return (len(text or "") + 3) // 4

def skills_token_budget() -> int:
    try:
        return max(1, int(os.environ.get("FOURBIS_SKILLS_TOKEN_BUDGET", "1200")))
    except ValueError:
        return 1200

def prompt_token_budget() -> int:
    try:
        return max(1, int(os.environ.get("FOURBIS_PROMPT_TOKEN_BUDGET", "5000")))
    except ValueError:
        return 5000
