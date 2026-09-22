"""Utilidades compartidas por los endpoints de la Admin UI."""
from __future__ import annotations

import os
import re
import time
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Optional

# ---------- jobs de reindex ----------

_active_index_jobs: dict[str, Any] = {}
# job_id -> monotonic del momento en que terminó. La UI pollea el
# resultado un rato; pasado el TTL se purga (sin esto el dict crece
# un slot por reindex/bulk hasta el próximo restart).
_job_done_at: dict[str, float] = {}
JOB_RESULT_TTL_S = int(os.environ.get("CBM_JOB_TTL", "900"))


def _purge_finished_jobs() -> None:
    now = time.monotonic()
    for jid, done_at in list(_job_done_at.items()):
        if now - done_at > JOB_RESULT_TTL_S:
            _active_index_jobs.pop(jid, None)
            _job_done_at.pop(jid, None)


def mark_job_done(job_id: str) -> None:
    _job_done_at[job_id] = time.monotonic()


def _set_job(job_id: str, value: Any) -> None:
    _purge_finished_jobs()
    _active_index_jobs[job_id] = value


def _get_job(job_id: str) -> Any:
    _purge_finished_jobs()
    return _active_index_jobs.get(job_id)


def _serialize(obj: Any) -> Any:
    """Serializa para JSON un objeto de cbm (que puede traer set/list/etc)."""
    if isinstance(obj, (list, tuple, set)):
        return [_serialize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    return obj


_remote_url_cache: dict[str, tuple[float, Optional[str]]] = {}


def _resolve_git_config(repo_path: str) -> Optional[Path]:
    git = Path(repo_path) / ".git"
    if git.is_dir():
        return git / "config"
    if git.is_file():
        try:
            line = git.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if not line.startswith("gitdir:"):
            return None
        gitdir = Path(line.split(":", 1)[1].strip())
        if not gitdir.is_absolute():
            gitdir = (git.parent / gitdir).resolve()
        return gitdir.parent.parent / "config"
    return None


def _git_remote_url(repo_path: str) -> Optional[str]:
    cfg = _resolve_git_config(repo_path)
    if cfg is None:
        return None
    try:
        mtime = cfg.stat().st_mtime
    except OSError:
        return None
    hit = _remote_url_cache.get(repo_path)
    if hit and hit[0] == mtime:
        return hit[1]
    url: Optional[str] = None
    try:
        in_origin = False
        for line in cfg.read_text(encoding="utf-8",
                                  errors="replace").splitlines():
            s = line.strip()
            if s.startswith("["):
                in_origin = s.replace(" ", "") == '[remote"origin"]'
            elif in_origin and s.startswith("url"):
                _, _, v = s.partition("=")
                url = _safe_git_remote_url(v.strip())
                break
    except OSError:
        url = None
    _remote_url_cache[repo_path] = (mtime, url)
    return url


def _has_git(path: Path) -> bool:
    return (path / ".git").exists()


def _safe_git_remote_url(url: str) -> Optional[str]:
    """Remote para mostrar: nunca credenciales, query ni fragmento del config."""
    if not url or any(c.isspace() or ord(c) < 32 for c in url):
        return None
    if "://" not in url:
        # La sintaxis scp usual; las rutas locales no son enlaces públicos.
        scp = re.fullmatch(r"[^/@:\s]+@(\[[^\]]+\]|[^/:\s]+):(.+)", url)
        if not scp:
            return None
        url = f"ssh://{scp[1]}/{scp[2]}"
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"https", "http", "ssh", "git"} or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host += f":{parsed.port}"
        return parsed._replace(netloc=host, params="", query="", fragment="").geturl()
    except ValueError:
        return None
