"""Canonical issue links shared by chat tasks and external PR registrations."""
from __future__ import annotations

import json
import re

from . import github, identity
from .task_pr import TaskPRError, now

_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", re.ASCII)
_URL = re.compile(r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)/?", re.ASCII)


def reference(value):
    """Return optional repository and number, without accepting arbitrary URLs."""
    if type(value) is int and 1 <= value <= 2147483647:
        return None, value
    match = _URL.fullmatch(value) if isinstance(value, str) and len(value) <= 500 else None
    if match and match[2] not in {".", ".."} and len(match[3]) <= 10:
        number = int(match[3])
        if number <= 2147483647:
            return f"{match[1]}/{match[2]}", number
    raise ValueError("issue debe ser un número entero positivo o una URL HTTPS canónica de GitHub issues")


async def _api(endpoint):
    rc, output = await github._gh("api", "--hostname", "github.com", endpoint)
    if rc:
        raise TaskPRError("GitHub no confirmó el issue. Revisa tu conexión personal y vuelve a intentar.")
    try:
        return json.loads(output)
    except (TypeError, ValueError) as exc:
        raise TaskPRError("GitHub devolvió datos de issue inválidos") from exc


async def repository(project):
    configured = await github.repo_slug(project.get("repo_path") or "")
    if not configured or not _REPO.fullmatch(configured) or configured.rsplit("/", 1)[-1] in {".", ".."}:
        raise ValueError("El proyecto no tiene un repositorio GitHub autorizado")
    repo = await _api(f"repos/{configured}")
    if (not isinstance(repo, dict) or type(repo.get("id")) is not int or repo["id"] <= 0
            or not isinstance(repo.get("full_name"), str) or not _REPO.fullmatch(repo["full_name"])
            or repo["full_name"].rsplit("/", 1)[-1] in {".", ".."}):
        raise TaskPRError("GitHub no confirmó la identidad del repositorio")
    return repo


def _canonical(repo, issue, number):
    canonical = repo["full_name"]
    url = f"https://github.com/{canonical}/issues/{number}"
    if (not isinstance(issue, dict) or type(issue.get("number")) is not int
            or issue["number"] != number or "pull_request" in issue
            or not isinstance(issue.get("repository_url"), str)
            or issue["repository_url"].casefold() != f"https://api.github.com/repos/{canonical}".casefold()
            or not isinstance(issue.get("html_url"), str) or issue["html_url"].casefold() != url.casefold()
            or not isinstance(issue.get("node_id"), str) or not issue["node_id"]
            or not isinstance(issue.get("title"), str) or issue.get("state") not in {"open", "closed"}
            or not isinstance(issue.get("updated_at"), str) or not issue["updated_at"]):
        raise ValueError("GitHub no confirmó un issue del repositorio autorizado; una PR no es un issue")
    return {"repo": canonical, "repo_id": repo["id"], "number": number,
            "node_id": issue["node_id"], "url": url, "title": issue["title"],
            "state": issue["state"].upper(), "updated_at": issue["updated_at"]}


async def resolve(project, value):
    requested, number = reference(value)
    repo = await repository(project)
    if requested:
        requested_repo = await _api(f"repos/{requested}")
        if (not isinstance(requested_repo, dict) or type(requested_repo.get("id")) is not int
                or requested_repo["id"] != repo["id"]):
            raise ValueError("El issue no pertenece al repositorio autorizado del proyecto")
    return _canonical(repo, await _api(f"repos/{repo['full_name']}/issues/{number}"), number)


async def list_issues(project):
    repo = await repository(project)
    rows = await _api(f"repos/{repo['full_name']}/issues?state=open&per_page=100")
    if not isinstance(rows, list):
        raise TaskPRError("GitHub no confirmó la lista de issues")
    return [_canonical(repo, row, row.get("number")) for row in rows
            if isinstance(row, dict) and "pull_request" not in row]


def require_writer(project, who):
    if not isinstance(who, str) or not who:
        raise PermissionError("Proyecto no autorizado para tu cuenta")
    role = "owner" if who == identity.OWNER else identity._roles.get(who, "unregistered")
    if (who in identity._disabled
            or not project or not project.get("enabled", True)
            or (project.get("defaults_json") or {}).get("read_only")
            or not identity.user_can_write_project({"role": role,
                "project_slugs": identity._project_grants.get(who, [])}, project.get("slug", ""))):
        raise PermissionError("Proyecto no autorizado para tu cuenta")


async def authorize_project(project, who, authorize):
    require_writer(project, who)
    if authorize is None:
        return project
    current = await authorize()
    require_writer(current, who)
    if (current.get("slug", "").casefold() != project.get("slug", "").casefold()
            or current.get("repo_path") != project.get("repo_path")):
        raise ValueError("El proyecto cambió durante la verificación; vuelve a intentar")
    return current


def guard_change(conn, task, issue, resolution_complete=None):
    """Keep closure dependencies stable while an effect is in flight or uncertain."""
    previous = task.get("issue_link") or {}
    keys = ("repo", "repo_id", "number", "node_id", "url")
    same_link = bool(previous) and all(previous.get(key) == issue.get(key) for key in keys)
    same_completion = resolution_complete is None or task.get("resolution_complete", False) == resolution_complete
    if same_link and same_completion:
        return
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='issue_sync_events'").fetchone():
        return
    active = conn.execute("SELECT 1 FROM issue_sync_events WHERE issue_key=? "
                          "AND state IN ('processing','uncertain') LIMIT 1",
                          (f"{issue['repo_id']}:{issue['number']}",)).fetchone()
    if active:
        raise ValueError("Hay una sincronización en curso o incierta para este issue; reconcilia su resultado antes de cambiar vínculos o resolución")


def apply(task, issue, actor, resolution_complete=None):
    """Apply a verified link to a task inside the caller's transaction."""
    if resolution_complete is not None and type(resolution_complete) is not bool:
        raise ValueError("resolution_complete debe ser booleano")
    previous = task.get("issue_link") or {}
    number = task.get("issue_number")
    if ((number is not None and number != issue["number"])
            or (previous and (previous.get("repo_id"), previous.get("number")) != (issue["repo_id"], issue["number"]))):
        raise ValueError("La tarea ya está vinculada a otro issue; no se reasigna automáticamente")
    task["issue_number"] = issue["number"]
    task["issue_link"] = dict(issue)
    if not previous:
        task["issue_link_actor"], task["issue_linked_at"] = dict(actor), now()
    if "resolution_complete" not in task:
        task["resolution_complete"] = False
    if resolution_complete is not None and task["resolution_complete"] != resolution_complete:
        task["resolution_complete"] = resolution_complete
        task["resolution_actor"], task["resolution_at"] = dict(actor), now()
    return task


async def bind(db, project, cid, value, who, resolution_complete=None, authorize=None, *, _resolved=None):
    """Bind one canonical issue atomically; never write to GitHub."""
    if resolution_complete is not None and type(resolution_complete) is not bool:
        raise ValueError("resolution_complete debe ser booleano")
    project = await authorize_project(project, who, authorize)
    conv = await db.get_conversation(cid)
    if not conv or conv["project_slug"].casefold() != project["slug"].casefold():
        raise ValueError("La tarea no pertenece al proyecto autorizado")
    initial = await db.get_conversation_task(cid)
    if initial.get("mode") not in {"write", "external_pr", "read_only"}:
        raise ValueError("Importa la conversación legacy como tarea antes de asociar issue")
    # The same identity check used by external registrations rejects bot accounts.
    from .external_pr import actor_identity
    actor = await actor_identity(who)
    issue = await resolve(project, value) if _resolved is None else _resolved
    project = await authorize_project(project, who, authorize)

    def work():
        conn = db._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT project_slug,issue_number,task_json FROM conversations WHERE id=?", (cid,)).fetchone()
            if not row or row["project_slug"].casefold() != project["slug"].casefold():
                raise ValueError("La tarea no pertenece al proyecto autorizado")
            if row["issue_number"] is not None and row["issue_number"] != issue["number"]:
                raise ValueError("La tarea ya está vinculada a otro issue")
            task = json.loads(row["task_json"] or "{}")
            if not isinstance(task, dict):
                raise ValueError("Los datos de la tarea no son válidos")
            if task.get("mode") not in {"write", "external_pr", "read_only"}:
                raise ValueError("Importa la conversación legacy como tarea antes de asociar issue")
            guard_change(conn, task, issue, resolution_complete)
            apply(task, issue, actor, resolution_complete)
            conn.execute("UPDATE conversations SET issue_number=?,task_json=? WHERE id=?",
                         (issue["number"], json.dumps(task, ensure_ascii=False), cid))
            conn.commit()
            return task
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return await db._in_thread(work)
