"""Exploración e instalación de skills desde repositorios clonados."""
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

from .skills_index import _parse_skill_md
from .skills_store import (
    estimate_tokens, sanitize_skill_name, set_skill_enabled_sync,
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
