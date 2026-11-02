"""Workspace Git persistente y aislado por conversación.

La base conserva la intención antes de ``git worktree add``. Desde ese
momento una ruta ausente es un incidente recuperable: nunca se recrea a
espaldas de posibles cambios que el usuario haya movido.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any


class TaskWorkspaceError(RuntimeError):
    """El workspace no puede usarse sin intervención explícita."""


def _norm(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _run_git(repo: str | Path, *args: str, check: bool = True,
             env: dict[str, str] | None = None) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        # El objeto de TimeoutExpired puede incluir comando y salida; no se
        # propagan porque una URL remota podría contener credenciales.
        raise TaskWorkspaceError(
            "la operación Git excedió el límite de 60 segundos") from exc
    out = (proc.stdout or "").strip()
    if check and proc.returncode:
        detail = (proc.stderr or out or "git falló").strip()[-500:]
        raise TaskWorkspaceError(f"git {' '.join(args[:2])}: {detail}")
    return out if proc.returncode == 0 else ""


async def _git(repo: str | Path, *args: str, check: bool = True) -> str:
    env = None
    if args and args[0] == "fetch":
        from .github_credentials import git_env
        env = await git_env()
    return await asyncio.to_thread(_run_git, repo, *args, check=check, env=env)


def _run_git_bytes(
    repo: str | Path, *args: str, input_data: bytes | None = None,
    check: bool = True,
) -> bytes | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], input=input_data,
            capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise TaskWorkspaceError(
            "la operación Git excedió el límite de 60 segundos") from exc
    if check and proc.returncode:
        detail = (proc.stderr or proc.stdout or b"git fallo").decode(
            "utf-8", errors="replace").strip()[-500:]
        raise TaskWorkspaceError(f"git {' '.join(args[:2])}: {detail}")
    return proc.stdout if proc.returncode == 0 else None


async def _git_bytes(
    repo: str | Path, *args: str, input_data: bytes | None = None,
    check: bool = True,
) -> bytes | None:
    return await asyncio.to_thread(
        _run_git_bytes, repo, *args, input_data=input_data, check=check)


async def _persist(db: Any, conv_id: str, **fields: Any) -> dict:
    return await db.update_conversation_task(conv_id, **fields)


def _task_uuid(conv_id: str) -> str:
    try:
        return str(uuid.UUID(conv_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise TaskWorkspaceError("conversation_id no es un UUID válido") from exc


def _workspace_path(db: Any, task_id: str) -> Path:
    return Path(db.path).resolve().parent / "task-workspaces" / task_id


async def _ref(repo: str | Path, name: str) -> str:
    return await _git(repo, "rev-parse", "--verify", name, check=False)


async def _status_clean(repo: str | Path) -> bool:
    return not bool(await _git(repo, "status", "--porcelain", "--untracked-files=all"))


async def _worktrees(repo: str | Path) -> list[dict[str, str]]:
    raw = await _git(repo, "worktree", "list", "--porcelain")
    rows: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in raw.splitlines() + [""]:
        if not line:
            if current:
                rows.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return rows


async def _strict_develop_guard(
    source: Path, *, allow_source_changes: bool = False,
    source_snapshot: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Fetch + validación estricta y fast-forward de develop."""
    root = await _git(source, "rev-parse", "--show-toplevel", check=False)
    if not root or _norm(root) != _norm(source):
        raise TaskWorkspaceError("repo_path no es la raíz exacta de un repositorio Git")
    if allow_source_changes:
        if not source_snapshot:
            raise TaskWorkspaceError("faltan cambios locales capturados para importar")
    try:
        await _git(source, "fetch", "origin")
    except TaskWorkspaceError as exc:
        raise TaskWorkspaceError(f"fetch origin falló: {exc}") from exc

    local = await _ref(source, "refs/heads/develop")
    remote = await _ref(source, "refs/remotes/origin/develop")
    if not local or not remote:
        raise TaskWorkspaceError("faltan develop local u origin/develop")

    if allow_source_changes:
        current_snapshot = await _local_changes_snapshot(source)
        if source_snapshot != current_snapshot:
            raise TaskWorkspaceError(
                "los cambios locales cambiaron durante fetch; vuelve a revisar antes de importar")
    elif not await _status_clean(source):
        raise TaskWorkspaceError("el checkout actual tiene cambios staged, tracked o untracked")

    develop_tree: Path | None = None
    for row in await _worktrees(source):
        if row.get("branch") != "refs/heads/develop":
            continue
        path = Path(row["worktree"])
        if not await _status_clean(path):
            raise TaskWorkspaceError(
                f"el worktree de develop tiene cambios pendientes: {path}")
        develop_tree = path

    counts = await _git(source, "rev-list", "--left-right", "--count",
                        "refs/heads/develop...refs/remotes/origin/develop")
    try:
        ahead, behind = (int(x) for x in counts.replace("\t", " ").split())
    except (ValueError, TypeError) as exc:
        raise TaskWorkspaceError("no pude comparar develop con origin/develop") from exc
    if ahead and behind:
        raise TaskWorkspaceError("develop local diverge de origin/develop")
    if ahead:
        raise TaskWorkspaceError("develop local tiene commits sin push")

    if behind:
        if develop_tree is not None:
            await _git(develop_tree, "merge", "--ff-only", "origin/develop")
        else:
            # update-ref incluye el SHA viejo: si otro proceso movió la rama,
            # falla en lugar de pisarlo.
            await _git(source, "update-ref", "refs/heads/develop", remote, local)
        local = await _ref(source, "refs/heads/develop")
    if local != remote:
        raise TaskWorkspaceError("develop no quedó idéntica a origin/develop")
    return local, remote


async def _local_changes_snapshot(source: Path) -> dict[str, Any]:
    branch = await _git(source, "symbolic-ref", "--quiet", "--short", "HEAD",
                        check=False)
    if not branch:
        raise TaskWorkspaceError("no se importan cambios desde HEAD detached")
    if branch.casefold() in {"develop", "master"}:
        raise TaskWorkspaceError("no se importan cambios desde ramas develop o master")
    untracked = await _git_bytes(
        source, "ls-files", "--others", "--exclude-standard", "-z")
    if untracked:
        raise TaskWorkspaceError("hay archivos untracked; no se importan cambios locales")
    conflicts = await _git_bytes(source, "ls-files", "--unmerged", "-z")
    if conflicts:
        raise TaskWorkspaceError("hay conflictos Git sin resolver; no se importan cambios locales")
    head = await _git(source, "rev-parse", "HEAD")
    patch = await _git_bytes(source, "diff", "--binary", "--full-index", "HEAD")
    if not patch:
        raise TaskWorkspaceError("no hay cambios tracked que importar")
    raw_files = await _git_bytes(source, "diff", "--name-only", "-z", "HEAD")
    try:
        files = sorted(item.decode("utf-8") for item in raw_files.split(b"\0") if item)
    except UnicodeDecodeError as exc:
        raise TaskWorkspaceError("un nombre de archivo no es UTF-8 válido") from exc
    return {
        "source_branch": branch,
        "source_head": head,
        "files": files,
        "patch": patch,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
    }


async def initialize_task(
    db: Any, project: dict, conv_id: str, *, read_only: bool = False,
    promote: bool = False, import_local_changes: bool = False,
) -> dict:
    """Serializa el único aprovisionamiento permitido por conversación."""
    locks = getattr(db, "_task_workspace_init_locks", None)
    if locks is None:
        # ponytail: un lock por conversación vive hasta reiniciar el proceso;
        # purgar sólo si el número de conversaciones llega a ser significativo.
        locks = db._task_workspace_init_locks = {}
    lock = locks.setdefault(conv_id, asyncio.Lock())
    async with lock:
        try:
            return await _initialize_task(
                db, project, conv_id, read_only=read_only, promote=promote,
                import_local_changes=import_local_changes)
        except Exception as exc:
            task = await db.get_conversation_task(conv_id)
            # Sólo un fallo anterior a la intención Git es reintentable. Si
            # ya hay mode/root/path, recrear podría perder un worktree movido.
            if not (task.get("mode") or task.get("source_repo")
                    or task.get("workspace_path")):
                await _persist(
                    db, conv_id, creation_failed=True,
                    requested_read_only=bool(read_only), read_only=bool(read_only),
                    state="blocked", workspace_state="blocked",
                    error=str(exc), workspace_error=str(exc))
            raise


async def initialize_no_checkout(db: Any, project: dict, conv_id: str) -> dict:
    """Persist an explicit new read-only chat without reserving a Git tree."""
    current = await db.get_project(project["slug"])
    if not current or not current.get("enabled"):
        raise TaskWorkspaceError("el proyecto fue deshabilitado o eliminado")
    repo = current.get("repo_path") or ""
    if not repo or not Path(repo).is_dir():
        raise TaskWorkspaceError("el proyecto no tiene un directorio existente")
    task = await db.get_conversation_task(conv_id)
    if (task.get("no_checkout") is True and task.get("mode") == "read_only"
            and task.get("read_only") is True):
        return task
    if task.get("mode") or task.get("source_repo") or task.get("workspace_path"):
        raise TaskWorkspaceError("la conversación ya tiene una identidad de workspace")
    return await _persist(db, conv_id, mode="read_only", no_checkout=True,
        read_only=True, requested_read_only=True, publish_allowed=False,
        tracking={"enabled": False}, state="ready", workspace_state="not_applicable", workspace_error="")


async def _initialize_task(
    db: Any, project: dict, conv_id: str, *, read_only: bool = False,
    promote: bool = False, import_local_changes: bool = False,
) -> dict:
    """Crea una sola vez el workspace de ``conv_id`` y persiste su estado."""
    existing = await db.get_conversation_task(conv_id)
    if existing.get("mode") == "external_pr":
        raise TaskWorkspaceError("Un PR externo solo registra evidencia y revisión; no crea un workspace.")
    upgrading = promote and existing.get("mode") == "read_only" and not read_only
    if upgrading:
        _safe_task_paths(db, project, conv_id, existing)
    if import_local_changes and (
            read_only or promote or not existing.get("creation_failed")
            or existing.get("mode") or existing.get("source_repo")
            or existing.get("workspace_path")):
        raise TaskWorkspaceError(
            "la importación sólo aplica a una creación fallida sin workspace")
    # task_json también guarda autorización/rol antes de provisionar. Sólo
    # mode+raíces significan que ya hubo una intención de workspace.
    if not upgrading and (existing.get("mode") or existing.get("source_repo")
                          or existing.get("workspace_path")):
        inspected = await inspect_workspace(db, project, conv_id)
        if (existing.get("state") == "provisioning"
                and inspected.get("workspace_state") == "ok"):
            return await _persist(db, conv_id, state="ready")
        return inspected

    if not project.get("repo_path"):
        raise TaskWorkspaceError("el proyecto no tiene un repo_path existente")
    source = Path(project["repo_path"]).resolve()
    if not source.is_dir():
        raise TaskWorkspaceError("el proyecto no tiene un repo_path existente")
    from . import git_flow
    if not await git_flow.is_git_repo(str(source)):
        # This is an explicit initialization, not an inferred migration of an
        # old empty task_json encountered by a reader or a background poller.
        return await initialize_no_checkout(db, project, conv_id)
    task_id = _task_uuid(conv_id)
    if read_only:
        head = await _ref(source, "HEAD")
        return await _persist(
            db, conv_id, mode="read_only", state="ready",
            workspace_state="ok", workspace_error="",
            source_repo=str(source), workspace_path=str(source), branch="",
            head_sha=head, remote_sha="", error="", creation_failed=False,
            requested_read_only=True, read_only=True,
        )

    source_snapshot = (await _local_changes_snapshot(source)
                       if import_local_changes else None)
    head, remote = await _strict_develop_guard(
        source, allow_source_changes=import_local_changes,
        source_snapshot=source_snapshot)
    origin_url = await _git(source, "remote", "get-url", "origin")
    branch = f"codex/task-{task_id}"
    workspace = _workspace_path(db, task_id)
    if workspace.exists():
        raise TaskWorkspaceError(f"la ruta de workspace ya existe: {workspace}")
    if await _ref(source, f"refs/heads/{branch}") or await _ref(
            source, f"refs/remotes/origin/{branch}"):
        raise TaskWorkspaceError(f"la rama de tarea ya existe: {branch}")

    # La intención va primero: si el proceso cae durante worktree add, el
    # reinicio bloquea y conserva evidencia en vez de recrear.
    local_changes = ({key: value for key, value in source_snapshot.items()
                      if key != "patch"}
                     | {"preserved": True, "already_integrated": False}
                     if source_snapshot else None)
    await _persist(
        db, conv_id, mode="write", state="provisioning",
        workspace_state="provisioning", workspace_error="",
        source_repo=str(source), workspace_path=str(workspace), branch=branch,
        head_sha=head, remote_sha=remote, origin_url=origin_url, error="",
        creation_failed=False,
        requested_read_only=False, read_only=False,
        **({"local_changes": local_changes} if local_changes else {}),
    )
    workspace.parent.mkdir(parents=True, exist_ok=True)
    try:
        await _git(source, "worktree", "add", "-b", branch,
                   str(workspace), "develop")
        await _validate_identity(source, workspace, branch)
        if source_snapshot:
            patch = source_snapshot["patch"]
            reverse = await _git_bytes(
                workspace, "apply", "--reverse", "--check", "-",
                input_data=patch, check=False)
            if reverse is not None:
                local_changes["already_integrated"] = True
                await _persist(db, conv_id, local_changes=local_changes)
            else:
                await _git_bytes(workspace, "apply", "--check", "-",
                                 input_data=patch)
                await _git_bytes(workspace, "apply", "-", input_data=patch)
    except Exception as exc:
        await _persist(db, conv_id, state="blocked", error=str(exc),
                       workspace_state="blocked", workspace_error=str(exc))
        raise TaskWorkspaceError(str(exc)) from exc
    task = await _persist(
        db, conv_id, state="ready", head_sha=head, remote_sha=remote,
        error="", workspace_state="ok", workspace_error="",
        creation_failed=False, requested_read_only=False, read_only=False)
    setter = getattr(db, "set_conversation_branch", None)
    if setter is not None:
        await setter(conv_id, branch)
    elif hasattr(db, "run"):
        await db.run("UPDATE conversations SET branch=? WHERE id=?",
                     (branch, conv_id))
    return task


async def _common_dir(repo: Path) -> str:
    raw = await _git(repo, "rev-parse", "--git-common-dir", check=False)
    if not raw:
        return ""
    path = Path(raw)
    return _norm(path if path.is_absolute() else repo / path)


async def _validate_identity(source: Path, workspace: Path, branch: str) -> None:
    root = await _git(workspace, "rev-parse", "--show-toplevel", check=False)
    current = await _git(workspace, "branch", "--show-current", check=False)
    if not root or _norm(root) != _norm(workspace):
        raise TaskWorkspaceError("workspace_path no coincide con la raíz Git")
    if current != branch:
        raise TaskWorkspaceError(
            f"workspace en rama {current or '(detached)'}, se esperaba {branch}")
    if not await _common_dir(source) or await _common_dir(source) != await _common_dir(workspace):
        raise TaskWorkspaceError("workspace y source_repo no comparten el Git común")


def _safe_task_paths(db: Any, project: dict, conv_id: str, task: dict) -> tuple[Path, Path]:
    source = Path(task.get("source_repo") or "").resolve()
    expected_source = Path(project.get("_task_source_repo")
                           or project.get("repo_path") or "").resolve()
    if _norm(source) != _norm(expected_source):
        raise TaskWorkspaceError("source_repo no coincide con el proyecto")
    workspace = Path(task.get("workspace_path") or "").resolve()
    if task.get("mode") == "write":
        expected = _workspace_path(db, _task_uuid(conv_id))
        if _norm(workspace) != _norm(expected):
            raise TaskWorkspaceError("workspace_path no pertenece a esta conversación")
        expected_branch = f"codex/task-{_task_uuid(conv_id)}"
        if task.get("branch") != expected_branch:
            raise TaskWorkspaceError("branch no pertenece a esta conversación")
    elif task.get("mode") == "read_only":
        if _norm(workspace) != _norm(source):
            raise TaskWorkspaceError("workspace read_only debe ser source_repo")
    else:
        raise TaskWorkspaceError("mode de tarea inválido")
    return source, workspace


async def inspect_workspace(
    db: Any, project: dict, conv_id: str, *, fetch: bool = False,
) -> dict:
    """Reconcilia identidad, HEAD y remoto sin tocar cambios del usuario."""
    task = await db.get_conversation_task(conv_id)
    if not task:
        return {"state": "missing", "error": "conversación sin task_json"}
    if task.get("no_checkout") is True:
        if task.get("mode") != "read_only" or task.get("read_only") is not True:
            raise TaskWorkspaceError("identidad sin checkout inválida")
        return task
    if task.get("mode") == "external_pr":
        return task
    if task.get("creation_failed") and not task.get("mode"):
        # Falló antes de reservar rama/path: el diagnóstico se conserva,
        # pero `initialize_task` puede reintentar con una política explícita.
        return task
    if task.get("state") == "cleaned":
        if task.get("workspace_state") != "cleaned":
            task = await _persist(db, conv_id, workspace_state="cleaned",
                                  workspace_error="")
        return task
    try:
        source, workspace = _safe_task_paths(db, project, conv_id, task)
        if not workspace.is_dir():
            raise TaskWorkspaceError(
                "workspace ausente; recuperación manual requerida (no se recreó)")
        if task.get("mode") == "read_only":
            return await _persist(db, conv_id, workspace_state="ok",
                                  workspace_error="")

        await _validate_identity(source, workspace, task["branch"])
        if task.get("origin_url"):
            current_origin = await _git(
                source, "remote", "get-url", "origin", check=False)
            if current_origin != task["origin_url"]:
                raise TaskWorkspaceError(
                    "origin del repositorio cambió desde el aprovisionamiento")
        if fetch:
            await _git(source, "fetch", "origin")
        head = await _ref(workspace, "HEAD")
        remote_ref = f"refs/remotes/origin/{task['branch']}"
        remote = await _ref(workspace, remote_ref)
        if remote:
            counts = await _git(workspace, "rev-list", "--left-right", "--count",
                                f"HEAD...{remote_ref}")
            local_ahead, remote_ahead = (
                int(x) for x in counts.replace("\t", " ").split())
            if remote_ahead:
                detail = "diverge" if local_ahead else "está detrás del remoto"
                raise TaskWorkspaceError(
                    f"la rama local {detail}; resuélvelo antes de continuar")
        fields: dict[str, Any] = {
            "workspace_state": "ok", "workspace_error": "",
            "head_sha": head, "remote_sha": remote,
        }
        dirty = not await _status_clean(workspace)
        if (task.get("head_sha") and task["head_sha"] != head
                and task.get("state") not in {"running", "validating", "publishing", "cancelled", "finished", "cleaned"}):
            fields.update(state="blocked", error="HEAD cambió fuera de la ejecución; revisa el commit y usa Continuar para reconciliar")
        if task.get("validation") and (task.get("head_sha") != head or dirty):
            # Conserva SHA/detalle probado; sólo deja explícito que ya no
            # acredita el árbol actual.
            fields["validation"] = {"status": "stale"}
        return await _persist(db, conv_id, **fields)
    except Exception as exc:
        fields = {"workspace_state": "blocked", "workspace_error": str(exc)}
        if task.get("state") not in {"cancelled", "finished", "cleaned"}:
            fields["state"] = "blocked"
        return await _persist(db, conv_id, **fields)


async def resolved_project(db: Any, project: dict, conv_id: str) -> dict:
    """Copia ``project`` apuntando al root propio de la conversación.

    Conversaciones anteriores a ``task_json`` siguen intactas. Una tarea ya
    versionada, en cambio, debe reconciliar como ``ready`` o el run se corta.
    """
    task = await db.get_conversation_task(conv_id)
    if not task:
        return dict(project)
    if task.get("no_checkout") is True:
        if task.get("mode") != "read_only" or task.get("read_only") is not True:
            raise TaskWorkspaceError("identidad sin checkout inválida")
        current = await db.get_project(project["slug"])
        if not current or not current.get("enabled"):
            raise TaskWorkspaceError("el proyecto fue deshabilitado o eliminado")
        repo = current.get("repo_path") or ""
        if not repo or not Path(repo).is_dir():
            raise TaskWorkspaceError("el directorio del proyecto ya no está disponible")
        return {**current, "_task_id": conv_id, "_task_mode": "read_only",
                "defaults_json": {**(current.get("defaults_json") or {}), "read_only": True}}
    if task.get("mode") == "external_pr":
        raise TaskWorkspaceError("Un PR externo solo registra evidencia y revisión; no ejecuta código ni Git.")
    task = await inspect_workspace(db, project, conv_id)
    if task.get("workspace_state") != "ok" or task.get("workspace_error"):
        raise TaskWorkspaceError(
            task.get("workspace_error") or "workspace no disponible")
    resolved = dict(project)
    resolved["repo_path"] = task["workspace_path"]
    resolved["_task_id"] = conv_id
    resolved["_task_mode"] = task.get("mode")
    resolved["_task_source_repo"] = task["source_repo"]
    defaults = dict(resolved.get("defaults_json") or {})
    if task.get("mode") == "read_only":
        defaults["read_only"] = True
    if task.get("mcp_private"):
        defaults.update(mcp_private=True, read_only=True, sql_tools=False, native_shell=False)
    resolved["defaults_json"] = defaults
    return resolved


async def cleanup_workspace(db: Any, project: dict, conv_id: str) -> dict:
    """Borra explícitamente un worktree terminado, nunca automáticamente."""
    task = await db.get_conversation_task(conv_id)
    if not task or task.get("mode") != "write":
        raise TaskWorkspaceError("la conversación no tiene workspace de escritura")
    if task.get("state") == "cleaned":
        return task
    if task.get("state") != "finished":
        raise TaskWorkspaceError("la tarea debe estar finished antes de limpiar")
    source, workspace = _safe_task_paths(db, project, conv_id, task)
    await _validate_identity(source, workspace, task["branch"])
    if not await _status_clean(workspace):
        raise TaskWorkspaceError("workspace con cambios pendientes; no se borra")
    pending = await db.list_conversation_events(
        conv_id, states=["pending", "processing", "uncertain"], limit=1)
    if pending:
        raise TaskWorkspaceError("la tarea todavía tiene eventos pendientes")
    if await db.active_task_graph(conv_id):
        raise TaskWorkspaceError("la conversación todavía tiene un grafo activo")
    for other in await db.list_managed_conversations():
        if other.get("id") == conv_id:
            continue
        owned = other.get("task_json") or {}
        if (owned.get("workspace_path") == task.get("workspace_path")
                or owned.get("branch") == task.get("branch")):
            raise TaskWorkspaceError(
                "otro task_json reclama el mismo workspace o branch")
    conv = await db.get_conversation(conv_id)
    pr_url = (conv or {}).get("pr_url") or task.get("pr_url")
    if not pr_url:
        raise TaskWorkspaceError("la tarea no tiene PR")
    from .github_credentials import gh_env
    gh = await asyncio.to_thread(
        subprocess.run,
        ["gh", "pr", "view", pr_url, "--json",
         "state,mergedAt,baseRefName,headRefName"],
        cwd=str(workspace), capture_output=True, text=True, timeout=30,
        env=await gh_env(),
    )
    try:
        pr = json.loads(gh.stdout or "{}") if gh.returncode == 0 else {}
    except json.JSONDecodeError:
        pr = {}
    if (pr.get("state") != "MERGED" or not pr.get("mergedAt")
            or pr.get("baseRefName") != "develop"
            or pr.get("headRefName") != task["branch"]):
        raise TaskWorkspaceError("GitHub no confirma una PR terminada y mergeada a develop")
    await _git(source, "fetch", "origin")
    # merge-base no produce stdout: corroborar por return code aparte.
    proc = await asyncio.to_thread(subprocess.run, ["git", "-C", str(source),
        "merge-base", "--is-ancestor", task["branch"], "origin/develop"],
        capture_output=True, text=True, timeout=30)
    if proc.returncode:
        raise TaskWorkspaceError("la rama aún no está mergeada en origin/develop")
    await _git(source, "worktree", "remove", str(workspace))
    await _git(source, "branch", "-d", task["branch"])
    await _persist(db, conv_id, state="cleaned", workspace_state="cleaned",
                   workspace_error="", workspace_path=str(workspace), error="")
    return {"state": "cleaned", "branch": task["branch"],
            "workspace_path": str(workspace), "pr_url": pr_url}
