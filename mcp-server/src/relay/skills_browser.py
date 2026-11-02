"""Exploración e instalación de skills desde repositorios clonados."""
from __future__ import annotations
import asyncio
import logging
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("relay.skills")

from .skills_index import _parse_skill_md
from .skills_store import (
    estimate_tokens, sanitize_skill_name, set_skill_enabled_sync,
    _plain_entry, _skill_mutation_lock,
)

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

_SCAN_MAX_DEPTH = 6

_SCAN_SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", "dist",
                   "build", ".github"}

SKILL_MAX_BYTES = 5 * 1024 * 1024
SKILL_MAX_ENTRIES = 10_000


def _physical_root(path: Path) -> Path:
    raw = Path(os.path.abspath(path))
    info = raw.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise ValueError("la raíz de la skill es un enlace o reparse point")
    return raw.resolve()


def _checked_path(base: Path, path: Path) -> os.stat_result:
    """No seguir symlinks, junctions ni otros reparse points del clon."""
    rel = path.relative_to(base)
    for candidate in (base, *(base.joinpath(*rel.parts[:i]) for i in range(1, len(rel.parts) + 1))):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("la skill contiene un enlace o reparse point")
        if not candidate.resolve().is_relative_to(base):
            raise ValueError("la skill sale del clon")
    return info


def _read_regular(base: Path, path: Path) -> str:
    before = _checked_path(base, path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError("el archivo de la skill cambió durante la lectura")
        data = stream.read(SKILL_MAX_BYTES + 1)
    _checked_path(base, path)
    if len(data) > SKILL_MAX_BYTES:
        raise ValueError("el archivo de la skill excede el límite de tamaño")
    return data.decode("utf-8")


def _tree_entries(path: Path) -> tuple[list[Path], list[Path], int]:
    """El mismo manifiesto validado limita el tamaño y define qué se copiará."""
    base = _physical_root(path)
    _checked_path(base, base)
    dirs, files, pending = [], [], [base]
    total = entries = 0
    while pending:
        current = pending.pop()
        with os.scandir(current) as children:
            for child in children:
                if child.name == ".git":
                    continue
                entries += 1
                if entries > SKILL_MAX_ENTRIES:
                    raise ValueError("la skill excede el límite de entradas")
                source = Path(child.path)
                info = _checked_path(base, source)
                if stat.S_ISDIR(info.st_mode):
                    dirs.append(source.relative_to(base))
                    pending.append(source)
                elif stat.S_ISREG(info.st_mode):
                    files.append(source.relative_to(base))
                    total += info.st_size
                    if total > SKILL_MAX_BYTES:
                        return dirs, files, total
                else:
                    raise ValueError("la skill contiene un archivo especial")
    return dirs, files, total

def _dir_bytes(path: Path) -> int:
    return _tree_entries(path)[2]

def scan_repo_skills(clone_dir: Path) -> list[dict]:
    """Encuentra los SKILL.md del clon y los devuelve parseados.

    Cada item trae `rel_path` (el directorio de la skill, relativo al
    clon, en POSIX) que es lo que la UI manda de vuelta para instalar.
    """
    try:
        clone_dir = _physical_root(clone_dir)
    except (OSError, ValueError):
        return []
    found: list[dict] = []
    root_depth = len(clone_dir.parts)

    def walk(d: Path) -> None:
        if len(d.parts) - root_depth > _SCAN_MAX_DEPTH:
            return
        try:
            _checked_path(clone_dir, d)
            children = sorted(d.iterdir())
        except (OSError, ValueError):
            return
        skill_md = d / "SKILL.md"
        if skill_md.is_file():
            try:
                _checked_path(clone_dir, skill_md)
                size = _dir_bytes(d)
                text = _read_regular(clone_dir, skill_md)
            except (OSError, ValueError):
                logger.warning("skills: árbol no seguro en %s", d.name)
                return
            parsed = _parse_skill_md(skill_md, d.name, text=text)
            rel = d.relative_to(clone_dir).as_posix()
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
        return _read_regular(src, src / "SKILL.md")
    except (OSError, ValueError):
        return None

def _resolve_in_clone(clone_dir: Path, rel_path: str) -> Optional[Path]:
    """Resuelve rel_path DENTRO del clon. None si se escapa (traversal)."""
    if not rel_path or rel_path.startswith(("/", "\\")):
        return None
    try:
        base = _physical_root(clone_dir)
        target = Path(os.path.abspath(base / rel_path))
        _checked_path(base, target)
        _checked_path(base, target / "SKILL.md")
    except (OSError, ValueError):
        return None
    if target != base and base not in target.parents:
        return None
    if not target.is_dir() or not (target / "SKILL.md").is_file():
        return None
    return target

def _publish_staged_skill(
    base_dir: Path, safe: str, staged: Path, *, overwrite: bool,
) -> None:
    """Publish under the same process lock as draft approval and other writers.

    Keep the previous directory until replacement succeeds. No-overwrite is
    revalidated here, after staging; a losing request cannot remove a winner.
    This lock coordinates Relay threads, not other processes/filesystem tools.
    """
    dest, previous = base_dir / safe, staged.parent / "previous"
    with _skill_mutation_lock(base_dir, safe):
        exists = _plain_entry(dest, directory=True)
        if exists and not overwrite:
            raise FileExistsError(safe)
        if exists:
            # The container is private to this attempt, never an installed dir.
            if os.path.lexists(previous):
                raise FileExistsError("la copia de recuperación ya existe")
            os.replace(dest, previous)
        try:
            os.replace(staged, dest)
        except BaseException:
            try:
                if exists and not os.path.lexists(dest):
                    os.replace(previous, dest)
            finally:
                if os.path.lexists(previous):
                    logger.warning("skills: publicación fallida; copia anterior en %s", previous)
            # If another process intervened or restore failed, keep previous
            # for recovery. Neither its directory nor the new dest is deleted.
            raise
        if exists:
            try:
                _plain_entry(previous, directory=True)
                shutil.rmtree(previous)
            except (OSError, ValueError):
                logger.warning("skills: publicada; se conserva copia de recuperación en %s", previous)


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
    dirs, files, total = _tree_entries(src)
    if total > SKILL_MAX_BYTES:
        raise ValueError(
            f"{rel_path!r} pesa más de {SKILL_MAX_BYTES // 1024 // 1024}MB; "
            "no parece una skill")

    parsed = _parse_skill_md(src / "SKILL.md", src.name, text=_read_regular(src, src / "SKILL.md"))
    safe = sanitize_skill_name(parsed.name if parsed else src.name)
    if not safe:
        raise ValueError(f"nombre de skill inválido: {src.name!r}")

    base_dir = base_dir.resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    container = Path(tempfile.mkdtemp(prefix=".skill-import-", dir=base_dir))
    container_id = container.stat()
    # Readers inspect only immediate children/<SKILL.md>. The container never
    # has that file, so unfinished content and rollback copies stay unindexed.
    staged = container / "content"
    try:
        staged.mkdir()
        for rel in dirs:
            (staged / rel).mkdir(parents=True, exist_ok=True)
        copied = 0
        for rel in files:
            source = src / rel
            before = _checked_path(src, source)
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(source, flags), "rb") as reader:
                opened = os.fstat(reader.fileno())
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError("la skill cambió durante la copia")
                target = staged / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as writer:
                    while chunk := reader.read(min(64 * 1024, SKILL_MAX_BYTES - copied + 1)):
                        copied += len(chunk)
                        if copied > SKILL_MAX_BYTES:
                            raise ValueError("la skill creció y excedió el límite de tamaño")
                        writer.write(chunk)
            _checked_path(src, source)
            shutil.copystat(source, target, follow_symlinks=False)
        if not enabled and not set_skill_enabled_sync(container, "content", False):
            raise ValueError("no se pudo preparar la skill apagada")
        _publish_staged_skill(base_dir, safe, staged, overwrite=overwrite)
    finally:
        # Never delete the last old copy after an unsuccessful restore. Cleanup
        # failures also must not turn an already published install into failure.
        try:
            current = _checked_path(base_dir, container)
            if ((current.st_dev, current.st_ino) == (container_id.st_dev, container_id.st_ino)
                    and not os.path.lexists(container / "previous")):
                shutil.rmtree(container)
        except (OSError, ValueError):
            logger.warning("skills: se conserva staging no eliminable en %s", container)
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
        from .mcp_install_scan import clone_repo  # mismo clone pineado
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
