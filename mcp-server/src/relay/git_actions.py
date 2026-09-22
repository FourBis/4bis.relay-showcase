"""git actions: funciones del flujo git_flow."""
from __future__ import annotations
import json
import logging
from typing import Optional
from . import git_branches, git_conversations, git_diff, git_process

logger = logging.getLogger("relay.git_flow")

def _guard_work_branch(branch: str) -> Optional[str]:
    """Motivo por el que `branch` no es una rama de trabajo, o None."""
    if not (branch or "").strip():
        return "esta conversación no tiene rama de trabajo"
    if branch in git_process.PROTECTED_BRANCHES:
        return (f"{branch} es rama protegida; el flujo es rama de trabajo "
                f"-> PR a {git_process.DEVELOP_BRANCH}")
    return None


def _safe_paths(repo: str, paths) -> tuple[list[str], Optional[str]]:
    """(paths relativos validados, error). Lista vacía = "todo"."""
    if not paths:
        return [], None
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return [], "paths debe ser una lista de strings"
    limpios = []
    for p in paths[:500]:
        rel = git_diff._rel_inside(repo, p)
        if rel is None:
            return [], f"path fuera del repo: {p!r}"
        limpios.append(rel)
    return limpios, None


async def commit_paths(repo: str, branch: str, *, message: str,
                       paths: Optional[list[str]] = None) -> dict:
    """Commitea la rama de la conversación. `paths` vacío = `git add -A`.

    Guard: HEAD tiene que estar EN la rama de la conversación. Commitear
    con HEAD en otra rama mete el trabajo del experto en la rama de
    otro, y eso no se deshace con un botón.

    Returns: {committed, sha, message, files, error}
    """
    out: dict = {"committed": False, "sha": "", "message": message,
                 "files": 0, "error": None}
    err = _guard_work_branch(branch)
    if err:
        out["error"] = err
        return out
    msg = (message or "").strip()
    if not msg:
        out["error"] = "hace falta un mensaje de commit"
        return out
    if not await git_branches.is_git_repo(repo):
        out["error"] = f"{repo} no es un repo git"
        return out
    current = await git_conversations._current_branch(repo)
    if current != branch:
        out["error"] = (f"HEAD está en {current or '?'}, no en {branch}; "
                        f"no commiteo en una rama ajena")
        return out
    rels, perr = _safe_paths(repo, paths)
    if perr:
        out["error"] = perr
        return out
    if rels:
        rc, txt = await git_process._git(repo, "add", "--", *rels)
    else:
        rc, txt = await git_process._git(repo, "add", "-A")
    if rc != 0:
        out["error"] = f"git add falló: {txt[-200:]}"
        return out
    rc, txt, _ = await git_process._git_out(repo, "diff", "--cached", "--name-only")
    staged = [ln for ln in txt.splitlines() if ln.strip()] if rc == 0 else []
    if not staged:
        out["error"] = "no hay nada staged para commitear"
        return out
    out["files"] = len(staged)
    rc, txt = await git_process._git(repo, "commit", "-m", msg)
    if rc != 0:
        out["error"] = f"git commit falló: {txt[-300:]}"
        return out
    rc, sha = await git_process._git(repo, "rev-parse", "--short", "HEAD")
    out["sha"] = sha.strip() if rc == 0 else ""
    out["committed"] = True
    logger.info("git_flow: commit %s en %s (%d archivos)",
                out["sha"], branch, out["files"])
    return out


async def push_branch(repo: str, branch: str) -> dict:
    """`git push -u origin <branch>`. Solo ramas de trabajo.

    Returns: {pushed, branch, output, error}
    """
    out: dict = {"pushed": False, "branch": branch, "output": "", "error": None}
    err = _guard_work_branch(branch)
    if err:
        out["error"] = err
        return out
    if not await git_branches.is_git_repo(repo):
        out["error"] = f"{repo} no es un repo git"
        return out
    if not await git_branches._branch_exists(repo, branch):
        out["error"] = f"la rama local {branch} no existe"
        return out
    rc, txt = await git_process._git(repo, "push", "-u", "origin", branch,
                         timeout=git_process.PUSH_TIMEOUT_S)
    out["output"] = txt.strip()[-500:]
    if rc != 0:
        out["error"] = f"push de {branch} falló: {txt[-300:]}"
        return out
    out["pushed"] = True
    logger.info("git_flow: push de %s a origin", branch)
    return out


async def open_pr(repo: str, branch: str, *, title: str,
                  body: str = "") -> dict:
    """Push + PR a develop, SIN cerrar la conversación.

    El /cerrar ya abría PR, pero cerraba el hilo con él: no había forma
    de publicar lo hecho y seguir trabajando. Este es ese camino. Si el
    PR ya existe devuelve su URL (mismo criterio que /cerrar).

    Returns: {pr_url, created, base, error}
    """
    out: dict = {"pr_url": None, "created": False, "base": git_process.DEVELOP_BRANCH,
                 "error": None}
    err = _guard_work_branch(branch)
    if err:
        out["error"] = err
        return out
    if not await git_branches.is_git_repo(repo):
        out["error"] = f"{repo} no es un repo git"
        return out
    base = await git_branches.detect_base_branch(repo)
    # Contra develop si ya existe; si no, contra el trunk. Al reves (contar
    # siempre contra develop) el `rc != 0` de una rama inexistente se
    # colaba como "hay commits" y el push creaba develop para un PR vacio.
    ref = (git_process.DEVELOP_BRANCH if await git_branches._branch_exists(repo, git_process.DEVELOP_BRANCH)
           else base)
    rc, txt = await git_process._git(repo, "rev-list", "--count", f"{ref}..{branch}")
    if rc != 0 or not txt.strip() or txt.strip() == "0":
        out["error"] = (f"la rama {branch} no tiene commits propios sobre "
                        f"{ref}; commiteá algo antes del PR")
        return out
    err = await git_conversations._ensure_develop(repo, base)
    if err:
        out["error"] = err
        return out
    push = await push_branch(repo, branch)
    if push["error"]:
        out["error"] = push["error"]
        return out
    rc, txt = await git_process._exec(repo, "gh", "pr", "create",
                          "--base", git_process.DEVELOP_BRANCH, "--head", branch,
                          "--title", (title or branch).strip(),
                          "--body", body or "")
    if rc != 0:
        url = (await git_conversations._existing_pr_url(repo, branch)
               if "already exists" in txt else None)
        if url:
            out["pr_url"] = url
            return out
        out["error"] = f"gh pr create falló: {txt[-300:]}"
        return out
    url = txt.strip().splitlines()[-1] if txt.strip() else ""
    out["pr_url"] = url or None
    out["created"] = bool(url)
    logger.info("git_flow: PR abierto (sin cerrar hilo) %s -> %s: %s",
                branch, git_process.DEVELOP_BRANCH, url)
    return out


async def merge_pr(repo: str, branch: str, *, method: str = "squash",
                   delete_branch: bool = True) -> dict:
    """Mergea el PR de la rama, SOLO si va a develop.

    Guard duro: si el PR apunta a main/master se rechaza. El flujo del
    repo es rama -> develop -> (PR a mano) -> main; un botón que mergea
    a main desde el visor de diff es exactamente lo que no queremos.

    Returns: {merged, pr_url, base, state, error}
    """
    out: dict = {"merged": False, "pr_url": None, "base": "", "state": "",
                 "error": None}
    err = _guard_work_branch(branch)
    if err:
        out["error"] = err
        return out
    if method not in ("squash", "merge", "rebase"):
        out["error"] = f"método de merge inválido: {method!r}"
        return out
    rc, txt = await git_process._exec(repo, "gh", "pr", "view", branch, "--json",
                          "url,baseRefName,state,isDraft")
    if rc != 0:
        out["error"] = f"no encontré PR para {branch}: {txt[-200:]}"
        return out
    try:
        info = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        out["error"] = f"no pude leer el PR: {txt[-200:]}"
        return out
    out["pr_url"] = info.get("url")
    out["base"] = base = info.get("baseRefName") or ""
    out["state"] = state = info.get("state") or ""
    if state != "OPEN":
        out["error"] = f"el PR está {state or '?'}, no OPEN"
        return out
    if base != git_process.DEVELOP_BRANCH:
        out["error"] = (f"el PR apunta a {base!r}; desde acá solo se mergea "
                        f"a {git_process.DEVELOP_BRANCH} (el PR a main lo haces vos)")
        return out
    args = ["pr", "merge", branch, f"--{method}"]
    if delete_branch:
        args.append("--delete-branch")
    rc, txt = await git_process._exec(repo, "gh", *args, timeout=git_process.PUSH_TIMEOUT_S)
    if rc != 0:
        out["error"] = f"gh pr merge falló: {txt[-300:]}"
        return out
    out["merged"] = True
    logger.info("git_flow: PR de %s mergeado (%s) a %s", branch, method, base)
    return out


async def sync_base(repo: str) -> dict:
    """`git fetch origin` + fast-forward de la base. No destructivo.

    Returns: {fetched, base, ref, behind, error}
    """
    out: dict = {"fetched": False, "base": "", "ref": "", "behind": 0,
                 "error": None}
    if not await git_branches.is_git_repo(repo):
        out["error"] = f"{repo} no es un repo git"
        return out
    out["fetched"] = await git_branches.fetch_origin_safe(repo)
    out["base"] = base = await git_branches.work_base_branch(repo)
    rc, behind = await git_process._git(repo, "rev-list", "--count",
                            f"{base}..origin/{base}")
    if rc == 0 and behind.strip().isdigit():
        out["behind"] = int(behind.strip())
    out["ref"] = await git_branches.sync_base_with_origin(repo, base)
    if not out["fetched"]:
        out["error"] = "fetch de origin falló (sin red o auth vencida)"
    return out


async def restore_paths(repo: str, branch: str, paths: list[str]) -> dict:
    """Descarta los cambios sin commitear de esos archivos (destructivo).

    `--source=HEAD --staged --worktree`: vuelve el archivo a como está
    en el último commit, staged incluido. Es lo que el botón promete
    ("descartar mis cambios en este archivo"); un `git restore` pelado
    solo pisa el worktree con el índice y dejaría lo staged vivo.

    Los untracked no los borra: eso es `rm`, no `restore`, y borrar un
    archivo que git no conoce no tiene reflog del que volver.

    Returns: {restored: [paths], error}
    """
    out: dict = {"restored": [], "error": None}
    err = _guard_work_branch(branch)
    if err:
        out["error"] = err
        return out
    rels, perr = _safe_paths(repo, paths)
    if perr or not rels:
        out["error"] = perr or "hace falta al menos un path"
        return out
    if not await git_branches.is_git_repo(repo):
        out["error"] = f"{repo} no es un repo git"
        return out
    current = await git_conversations._current_branch(repo)
    if current != branch:
        out["error"] = (f"HEAD está en {current or '?'}, no en {branch}; "
                        f"no descarto cambios de una rama ajena")
        return out
    rc, txt = await git_process._git(repo, "restore", "--source=HEAD", "--staged",
                         "--worktree", "--", *rels)
    if rc != 0:
        out["error"] = f"git restore falló: {txt[-300:]}"
        return out
    out["restored"] = rels
    logger.warning("git_flow: descartados cambios de %d archivo(s) en %s: %s",
                   len(rels), branch, ", ".join(rels[:5]))
    return out
