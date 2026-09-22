"""PR y evidencia por commit, sobre Git/gh y el verificador existentes."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import git_flow, git_process, github, night, task_workspace


class TaskPRError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def set_phase(db, cid, phase, **fields):
    """Un paso en vuelo nunca revierte una pausa/cancelación del usuario."""
    return bool(await db.run(
        "UPDATE conversations SET task_json=json_patch(task_json, ?) WHERE id=? "
        "AND json_extract(task_json,'$.state') NOT IN ('paused','cancelled','finished','blocked','cleaned') RETURNING id",
        (json.dumps({**fields, "state": phase}), cid)))


async def git(repo, *args):
    rc, out = await git_process._git(repo, *args)
    if rc:
        raise TaskPRError(f"Git {args[0]}: {out[-800:]}")
    return out.strip()


async def gh_json(repo, *args):
    rc, out = await git_process._exec(repo, "gh", *args)
    if rc:
        raise TaskPRError(f"GitHub no confirmó la lectura: {out[-600:]}")
    try:
        return json.loads(out)
    except (TypeError, ValueError) as exc:
        raise TaskPRError("GitHub devolvió una respuesta inválida") from exc


async def find_pr(repo, branch):
    rows = await gh_json(repo, "pr", "list", "--head", branch, "--base", "develop",
                         "--state", "all", "--limit", "10", "--json",
                         "number,url,state,headRefOid,headRefName,baseRefName,mergedAt")
    rows = [r for r in rows if r.get("headRefName") == branch
            and r.get("baseRefName") == "develop"]
    if len(rows) > 1:
        raise TaskPRError("Hay varias PR para esta rama; reconciliación manual necesaria")
    return rows[0] if rows else None


async def publish(db, project, conv_id):
    """Commit → validar SHA exacto → push → crear o recuperar la misma PR.

    Un timeout de create no autoriza otro create. La intención queda durable
    y sólo una lectura positiva de GitHub permite reconciliarla.
    """
    task = await db.get_conversation_task(conv_id)
    if task.get("mode") != "write" or not task.get("publish_allowed"):
        raise TaskPRError("Esta tarea no tiene autorización para publicar una PR")
    project = await task_workspace.resolved_project(db, project, conv_id)
    await task_workspace.inspect_workspace(db, project, conv_id, fetch=True)
    task = await db.get_conversation_task(conv_id)
    if task.get("state") in {"paused", "cancelled", "finished", "cleaned"}:
        return task
    if task.get("workspace_state") == "blocked" or task.get("state") == "blocked":
        raise TaskPRError(task.get("error") or "Workspace bloqueado")
    repo, branch = project["repo_path"], task["branch"]
    existing = await find_pr(repo, branch)
    if existing and existing["state"] != "OPEN":
        await db.cancel_pending_conversation_events(conv_id)
        return await db.update_conversation_task(conv_id, state="finished", tracking={"enabled": False})
    if task.get("publish_uncertain") and not existing:
        raise TaskPRError("Publicación anterior incierta: no se repetirá create sin reconciliar la PR")
    if existing:
        await db.set_conversation_pr(conv_id, existing["url"])
        await db.update_conversation_task(conv_id, publish_uncertain=False)
    # La base remota debe estar integrada antes de acreditar este commit.
    await git(repo, "fetch", "origin", "--prune")
    rc, _ = await git_process._git(repo, "merge-base", "--is-ancestor", "origin/develop", "HEAD")
    if rc:
        raise TaskPRError("develop cambió: integra origin/develop en la rama de la tarea y vuelve a validar")
    if await git(repo, "status", "--porcelain"):
        committed = await git_flow.commit_paths(
            repo, branch, message=f"task: cambios de {conv_id[:8]}")
        if committed.get("error"):
            raise TaskPRError(committed["error"])
    sha = await git(repo, "rev-parse", "HEAD")
    if await git(repo, "rev-list", "--count", "origin/develop..HEAD") == "0":
        raise TaskPRError("La tarea aún no tiene cambios para una PR")
    if not await set_phase(db, conv_id, "validating", head_sha=sha,
                           validation={"head_sha": sha, "status": "running"}):
        return await db.get_conversation_task(conv_id)
    ok, detail = await night.verify_repo(repo, project)
    stable = sha == await git(repo, "rev-parse", "HEAD") and not await git(repo, "status", "--porcelain")
    validation = {"head_sha": sha, "status": ("ok" if ok else "failed" if ok is False else "unconfigured")
                  if stable else "stale", "detail": detail[-12000:], "at": now()}
    await db.update_conversation_task(conv_id, validation=validation)
    current = await db.get_conversation_task(conv_id)
    if current.get("state") in {"paused", "cancelled", "finished", "cleaned"}:
        return current
    if not stable:
        raise TaskPRError("El commit o sus archivos cambiaron durante la validación; no se publica evidencia anterior")
    if ok is not True:
        raise TaskPRError("Validación fallida o sin configurar: se conserva el commit local y no se publica")
    if not await set_phase(db, conv_id, "publishing", publish_head=sha):
        return await db.get_conversation_task(conv_id)
    # Push no forzado: rechaza cambios remotos concurrentes sin perderlos.
    await git(repo, "push", "-u", "origin", f"HEAD:refs/heads/{branch}")
    await db.update_conversation_task(conv_id, remote_sha=sha)
    current = await db.get_conversation_task(conv_id)
    if current.get("state") in {"paused", "cancelled", "finished", "cleaned"}:
        return current
    if not existing:
        body = (f"Cambios de la tarea `{conv_id}`.\n\n"
                f"Validación local del commit `{sha}`: **{validation['status']}**.\n\n"
                f"{detail}\n\nAbrir esta PR no finaliza la tarea ni acredita CI remoto.")
        # Se registra ANTES del efecto, incluso si el proceso muere dentro de gh.
        await db.update_conversation_task(conv_id, publish_uncertain=True)
        args = ["pr", "create", "--base", "develop", "--head", branch,
                "--title", f"[{project['slug']}] tarea {conv_id[:8]}", "--body", body]
        rc, output = await git_process._exec(repo, "gh", *args)
        # Una respuesta de create no es suficiente: leer identidad y head real.
        existing = await find_pr(repo, branch)
        if not existing:
            raise TaskPRError(f"Publicación incierta; se conserva la intención: {output[-400:]}")
    if existing["headRefOid"] != sha:
        existing = await find_pr(repo, branch)
    if not existing or existing["headRefOid"] != sha:
        raise TaskPRError("La PR no confirma todavía el commit publicado; reconciliar antes de continuar")
    await db.set_conversation_pr(conv_id, existing["url"])
    current = await db.get_conversation_task(conv_id)
    if current.get("state") in {"paused", "cancelled", "finished", "cleaned"}:
        return await db.update_conversation_task(conv_id, pr_url=existing["url"],
                                                 pr_number=existing["number"], publish_uncertain=False)
    await set_phase(
        db, conv_id, "review", pr_url=existing["url"], pr_number=existing["number"],
        head_sha=sha, remote_sha=sha, publish_uncertain=False, error="")
    return await db.get_conversation_task(conv_id)


async def _pages(repo, endpoint):
    rows = []
    # ponytail: máximo 500 eventos por tipo; si se supera se bloquea sin perder
    # un cursor. Upgrade: paginación incremental si aparece una PR de ese tamaño.
    for page in range(1, 6):
        part = await gh_json(repo, "api", f"{endpoint}?per_page=100&page={page}")
        if not isinstance(part, list):
            raise TaskPRError("Lista de eventos GitHub inválida")
        rows.extend(part)
        if len(part) < 100:
            return rows
    raise TaskPRError("Más de 500 eventos en la PR: requiere revisión manual")


async def feedback_snapshot(project, branch):
    """Sólo lectura. Incluye IDs/versiones estables; ninguna llamada al modelo."""
    repo = project["repo_path"]
    pr = await find_pr(repo, branch)
    if not pr or pr["state"] != "OPEN":
        return pr, []
    slug = await github.repo_slug(repo)
    if not slug:
        raise TaskPRError("No se pudo determinar el repositorio GitHub de origin")
    number, sha = pr["number"], pr["headRefOid"]
    events = []
    for kind, endpoint in (
        ("comment", f"repos/{slug}/issues/{number}/comments"),
        ("review_comment", f"repos/{slug}/pulls/{number}/comments"),
        ("review", f"repos/{slug}/pulls/{number}/reviews"),
    ):
        for row in await _pages(repo, endpoint):
            body = (row.get("body") or "").strip()
            if not body or (kind == "review" and row.get("state") != "CHANGES_REQUESTED"):
                continue
            # Reviews sobre un head reemplazado no disparan correcciones viejas.
            if row.get("commit_id") and row["commit_id"] != sha:
                continue
            stamp = row.get("updated_at") or row.get("submitted_at") or row.get("created_at") or ""
            events.append({"key": f"pr:{number}:{kind}:{row['id']}:{stamp}",
                           "user": body[:16000], "head_sha": sha,
                           "url": row.get("html_url", ""), "at": stamp})
    checks = await gh_json(repo, "api", f"repos/{slug}/commits/{sha}/check-runs?per_page=100")
    if checks.get("total_count", 0) > 100:
        raise TaskPRError("Más de 100 checks: requiere revisión manual")
    failures = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
    for row in checks.get("check_runs", []):
        if row.get("head_sha") != sha or row.get("conclusion") not in failures:
            continue
        events.append({"key": f"pr:{number}:ci:{sha}:{row['id']}:{row.get('completed_at')}",
                       "user": f"CI {row['name']}: {row['conclusion']}. "
                               f"{(row.get('output') or {}).get('summary') or ''}"[:16000],
                       "head_sha": sha, "url": row.get("html_url", "")})
    statuses = await gh_json(repo, "api", f"repos/{slug}/commits/{sha}/status?per_page=100")
    if statuses.get("total_count", 0) > 100:
        raise TaskPRError("Más de 100 estados CI: requiere revisión manual")
    for row in statuses.get("statuses", []):
        if row.get("state") in {"error", "failure"}:
            events.append({"key": f"pr:{number}:status:{sha}:{row['id']}",
                           "user": f"CI {row['context']}: {row.get('description') or row['state']}",
                           "head_sha": sha, "url": row.get("target_url", "")})
    return pr, events
