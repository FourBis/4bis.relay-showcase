"""Register and review external PRs; no checkout, model or GitHub writes."""
from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import datetime, timezone

from . import github, issue_links
from .task_pr import TaskPRError, now
from .user_accounts import AccountError

_URL = re.compile(r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?", re.ASCII)
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", re.ASCII)
_SHA = re.compile(r"[0-9a-f]{40}")


class ObservationConflict(ValueError):
    """A newer observation won the local optimistic lease; re-read first."""


def parse_url(url):
    match = _URL.fullmatch(url) if isinstance(url, str) and len(url) <= 500 else None
    if not match or match[2] in {".", ".."} or len(match[3]) > 10:
        raise ValueError("Usa una URL HTTPS de PR github.com/owner/repo/pull/numero, sin query ni fragmento")
    return f"{match[1]}/{match[2]}", int(match[3])


async def _api(endpoint):
    rc, output = await github._gh("api", "--hostname", "github.com", endpoint)
    if rc:
        # Do not persist provider output: it can contain account or credential details.
        raise TaskPRError("GitHub no confirmó la lectura. Revisa tu conexión personal y vuelve a intentar.")
    try:
        return json.loads(output)
    except (TypeError, ValueError) as exc:
        raise TaskPRError("GitHub devolvió una respuesta inválida") from exc


def _user(user):
    if (not isinstance(user, dict) or type(user.get("id")) is not int
            or user["id"] <= 0 or not user.get("login") or user.get("type") not in {"User", "Bot"}):
        raise TaskPRError("GitHub no confirmó una identidad verificable")
    return {key: user[key] for key in ("id", "login", "type")}


async def actor_identity(who):
    user = _user(await _api("user"))
    if user["type"] != "User":
        raise TaskPRError("El responsable debe tener una cuenta GitHub humana conectada")
    return {"relay_actor": who, "github_id": user["id"], "github_login": user["login"]}


async def _pages(endpoint):
    rows = []
    for page in range(1, 6):
        result = await _api(f"{endpoint}?per_page=100&page={page}")
        if not isinstance(result, list):
            raise TaskPRError("GitHub devolvió una lista inválida")
        rows.extend(result)
        if len(result) < 100:
            return rows
    raise TaskPRError("La PR supera 500 revisiones; requiere revisión manual")


async def _read_snapshot(project, url, issue_number=None):
    requested, number = parse_url(url)
    configured = await github.repo_slug(project.get("repo_path") or "")
    if not configured or not _SLUG.fullmatch(configured):
        raise ValueError("El proyecto no tiene un repositorio GitHub autorizado")
    repo = await _api(f"repos/{configured}")
    canonical = repo.get("full_name", "") if isinstance(repo, dict) else ""
    if (not _SLUG.fullmatch(canonical) or requested.casefold() != canonical.casefold()
            or type(repo.get("id")) is not int):
        raise ValueError("La PR no pertenece al repositorio autorizado del proyecto")
    prefix = f"repos/{canonical}"
    pr = await _api(f"{prefix}/pulls/{number}")
    try:
        base, head = pr["base"], pr["head"]
        valid = (pr["number"] == number and base["repo"]["id"] == repo["id"]
                 and base["repo"]["full_name"].casefold() == canonical.casefold()
                 and pr["html_url"].casefold() == f"https://github.com/{canonical}/pull/{number}".casefold()
                 and _SHA.fullmatch(base["sha"]) and _SHA.fullmatch(head["sha"])
                 and pr["state"] in {"open", "closed"} and type(pr["draft"]) is bool)
    except (TypeError, KeyError):
        valid = False
    if not valid:
        raise TaskPRError("GitHub no confirmó repositorio, número y commits de la PR")
    issue_link = await issue_links.resolve(project, issue_number) if issue_number is not None else None
    if issue_link and issue_link["repo_id"] != repo["id"]:
        raise ValueError("El repositorio cambió durante la verificación del issue")
    sha = head["sha"]
    checks_data = await _api(f"{prefix}/commits/{sha}/check-runs?per_page=100&filter=latest")
    statuses = await _api(f"{prefix}/commits/{sha}/status?per_page=100")
    if (not isinstance(checks_data, dict) or not isinstance(statuses, dict)
            or checks_data.get("total_count", 101) > 100 or statuses.get("total_count", 101) > 100
            or statuses.get("sha") != sha):
        raise TaskPRError("GitHub no confirmó todos los checks del commit; requiere revisión manual")
    checks = [{key: row.get(key) for key in ("id", "name", "head_sha", "status", "conclusion", "html_url")}
              for row in checks_data.get("check_runs", []) if row.get("head_sha") == sha]
    # Combined status is newest first. Keep only the latest result per context.
    seen = set()
    for row in statuses.get("statuses", []):
        name = row.get("context")
        if name in seen:
            continue
        seen.add(name)
        checks.append({"id": row.get("id"), "name": name, "head_sha": sha,
                       "status": "completed" if row.get("state") != "pending" else "in_progress",
                       "conclusion": "success" if row.get("state") == "success" else row.get("state"),
                       "html_url": row.get("target_url")})
    reviews = []
    for row in await _pages(f"{prefix}/pulls/{number}/reviews"):
        reviews.append({**{key: row.get(key) for key in ("id", "state", "commit_id", "submitted_at", "html_url")},
                        "user": _user(row.get("user"))})
    # Verify again after reading evidence: do not attach a mixed snapshot to an old head.
    latest = await _api(f"{prefix}/pulls/{number}")
    if (not isinstance(latest, dict) or latest.get("head", {}).get("sha") != sha or latest.get("base", {}).get("sha") != base["sha"]
            or latest.get("state") != pr["state"] or latest.get("draft") != pr["draft"]):
        raise TaskPRError("La PR cambió durante la lectura. Vuelve a actualizar.")
    return {"repo": canonical, "repo_id": repo["id"], "number": number,
            "url": f"https://github.com/{canonical}/pull/{number}", "title": pr.get("title", ""),
            "state": "MERGED" if pr.get("merged_at") else pr["state"].upper(), "draft": pr["draft"],
            "head_sha": sha, "base_sha": base["sha"], "head_ref": head["ref"], "base_ref": base["ref"],
            "author": _user(pr.get("user")), "checks": checks, "reviews": reviews, "observed_at": now(),
            **({"issue_link": issue_link} if issue_link else {})}


async def read_snapshot(project, url, issue_number=None):
    try:
        return await _read_snapshot(project, url, issue_number)
    except (KeyError, TypeError, AttributeError) as exc:
        raise TaskPRError("GitHub devolvió datos incompletos o inválidos; vuelve a actualizar") from exc


def assessment(task):
    pr = task["external_pr"]
    sha, reasons = pr["head_sha"], []
    policy = task.get("policy")
    if task.get("verification_error"):
        reasons.append(task["verification_error"])
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(pr["observed_at"])).total_seconds()
        if age < -60 or age > 300:
            reasons.append("La verificación tiene más de cinco minutos; actualiza GitHub")
    except (KeyError, TypeError, ValueError):
        reasons.append("Falta fecha de verificación GitHub")
    if pr["state"] != "OPEN" or pr["draft"]:
        reasons.append("La PR debe estar abierta y fuera de borrador")
    if (not isinstance(policy, dict) or type(policy.get("require_declaration")) is not bool
            or not isinstance(policy.get("required_checks"), list)
            or not policy["required_checks"] or not all(isinstance(n, str) and n.strip() for n in policy["required_checks"])):
        reasons.append("Falta una política explícita de evidencia para este riesgo (checks por nombre y declaración)")
    else:
        if policy["require_declaration"] and not any(e["head_sha"] == sha for e in task.get("evidence", [])):
            reasons.append("Falta la declaración de pruebas del commit actual")
        for name in policy["required_checks"]:
            matching = [c for c in pr["checks"] if c.get("name") == name and c.get("head_sha") == sha]
            # Duplicate names are ambiguous; never choose the green one from several runs/apps.
            if len(matching) != 1 or matching[0].get("status") != "completed" or matching[0].get("conclusion") != "success":
                reasons.append(f"Falta check verificado satisfactorio e inequívoco: {name}")
    if reasons:
        return {"state": "missing_evidence", "reasons": reasons, "policy": policy}
    latest = {}
    for review in sorted(pr["reviews"], key=lambda r: (r.get("submitted_at") or "", r.get("id") or 0)):
        if review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[review["user"]["id"]] = review
    excluded = {pr["author"]["id"], task["responsible"]["github_id"]}
    independent = [r for uid, r in latest.items() if uid not in excluded and r["user"]["type"] == "User"]
    if any(r["state"] == "CHANGES_REQUESTED" for r in independent):
        reasons.append("Hay cambios solicitados por un revisor independiente")
    if not any(r["state"] == "APPROVED" and r.get("commit_id") == sha for r in independent):
        reasons.append("Falta aprobación humana independiente del commit actual")
    return {"state": "review_pending" if reasons else "ready", "reasons": reasons, "policy": policy}


def snapshot(task, project=None):
    if project is not None:
        task = {**task, "policy": _policy(project, task["risk"])}
    return {**task, "head_sha": task["external_pr"]["head_sha"], "assessment": assessment(task),
            "allowed_actions": ["external_refresh", "external_evidence", "link_issue", "issue_sync"]}


async def _transaction(db, operation):
    def work():
        conn = db._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = operation(conn)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return await db._in_thread(work)


def _load(conn, cid):
    row = conn.execute("SELECT task_json FROM conversations WHERE id=?", (cid,)).fetchone()
    task = json.loads(row[0]) if row else {}
    if task.get("mode") != "external_pr":
        raise ValueError("La tarea no es un registro de PR externo")
    return task


def _save(conn, task, project=None):
    pr = task["external_pr"]
    closed = pr["state"] in {"CLOSED", "MERGED"}
    stamp = now()
    conn.execute("UPDATE conversations SET task_json=?,pr_url=?,last_activity_at=?,status=?,"
                 "closed_at=CASE WHEN ? THEN COALESCE(closed_at,?) ELSE NULL END WHERE id=?",
                 (json.dumps(task, ensure_ascii=False), pr["url"], stamp,
                  "closed" if closed else "open", closed, stamp, task["id"]))
    return snapshot(task, project)


def _policy(project, risk):
    policies = (project.get("defaults_json") or {}).get("external_pr_policy") or {}
    return copy.deepcopy(policies.get(risk)) if isinstance(policies, dict) else None


def _apply_remote(task, pr, project, actor, conn):
    previous = {k: v for k, v in task["external_pr"].items() if k != "observed_at"}
    current = {k: v for k, v in pr.items() if k != "observed_at"}
    task.update(external_pr=pr, policy=_policy(project, task["risk"]), verification_error="")
    if pr.get("issue_link"):
        issue_links.guard_change(conn, task, pr["issue_link"])
        issue_links.apply(task, pr["issue_link"], actor)
    if previous != current:
        task["history"].append({"kind": "github_snapshot", "at": now(), "actor": actor, "snapshot": pr})


def _text(body, key, limit, required=False):
    value = body.get(key, "")
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"{key} inválido (máximo {limit} caracteres)")
    return value.strip()


async def _authorize(project, authorize):
    if authorize is None:
        return project
    current = await authorize()
    if current.get("repo_path") != project.get("repo_path"):
        raise ValueError("El repositorio del proyecto cambió durante la verificación; vuelve a intentar")
    return current


async def _failed_read(db, initial, actor):
    def fail(conn):
        task = _load(conn, initial["id"])
        if task["external_pr"].get("observed_at") != initial["external_pr"].get("observed_at"):
            return  # A newer successful observation supersedes this failed read.
        if not task.get("verification_error"):
            task["history"].append({"kind": "verification_failed", "at": now(), "actor": actor})
        task["verification_error"] = "No se pudo verificar GitHub; actualiza antes de considerar apto"
        _save(conn, task)
    await _transaction(db, fail)


async def register(db, project, body, who, *, authorize=None):
    url = body.get("url")
    requested_repo, requested_number = parse_url(url)
    title, label = _text(body, "title", 300, True), _text(body, "actor_label", 200)
    risk, issue = body.get("risk"), body.get("issue", body.get("issue_number"))
    if risk not in {"low", "medium", "high"}:
        raise ValueError("risk debe ser low, medium o high")
    issue_number = issue_links.reference(issue)[1] if issue is not None else None
    if body.get("issue") is not None and body.get("issue_number") is not None:
        if issue_links.reference(body["issue_number"])[1] != issue_number:
            raise ValueError("issue e issue_number identifican issues diferentes")
    completion = body.get("resolution_complete")
    if completion is not None and type(completion) is not bool:
        raise ValueError("resolution_complete debe ser booleano")
    if completion is not None and issue is None:
        raise ValueError("resolution_complete requiere un issue")
    known = await db.run("SELECT task_json FROM conversations WHERE project_slug=? COLLATE NOCASE "
                         "AND json_extract(task_json,'$.mode')='external_pr' "
                         "AND lower(json_extract(task_json,'$.external_pr.repo'))=lower(?) "
                         "AND json_extract(task_json,'$.external_pr.number')=?",
                         (project["slug"], requested_repo, requested_number))
    actor = {"relay_actor": who}
    try:
        actor = await actor_identity(who)
        pr = await read_snapshot(project, url, issue)
        linked_issue = pr.get("issue_link")
        if issue is not None and linked_issue is None:
            linked_issue = await issue_links.resolve(project, issue)
    except (TaskPRError, AccountError, ValueError, OSError):
        await _authorize(project, authorize)
        if known:
            await _failed_read(db, json.loads(known[0]["task_json"]), actor)
        raise
    project = await _authorize(project, authorize)
    cid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"relay:external-pr:{pr['repo_id']}:{pr['number']}"))
    def work(conn):
        linked = conn.execute("SELECT id FROM conversations WHERE lower(rtrim(pr_url,'/'))=lower(?) AND id<>? LIMIT 1",
                              (pr["url"], cid)).fetchone()
        if linked:
            raise ValueError(f"Esta PR ya está vinculada a la tarea {linked[0]}; consulta su registro existente")
        existing = conn.execute("SELECT project_slug,task_json FROM conversations WHERE id=?", (cid,)).fetchone()
        if existing:
            task = json.loads(existing["task_json"])
            if (existing["project_slug"].casefold() != project["slug"].casefold()
                    or task["requested_by"] != who or task["title"] != title
                    or task["risk"] != risk or task.get("issue_number") != issue_number or task.get("actor_label", "") != label
                    or task["responsible"]["github_id"] != actor["github_id"]):
                raise ValueError("Esta PR ya está registrada con otra tarea, responsable o alcance")
            if linked_issue:
                issue_links.guard_change(conn, task, linked_issue, completion)
            if pr.get("observed_at", "") >= task["external_pr"].get("observed_at", ""):
                _apply_remote(task, pr, project, actor, conn)
            if linked_issue:
                issue_links.apply(task, linked_issue, actor, completion)
            return _save(conn, task, project)
        stamp = now()
        task = {"id": cid, "mode": "external_pr", "state": "registered", "title": title,
                "risk": risk, "issue_number": issue_number, "actor_label": label, "requested_by": who,
                "responsible": actor, "external_pr": pr, "policy": _policy(project, risk),
                "evidence": [], "history": [{"kind": "github_snapshot", "at": stamp, "actor": actor, "snapshot": pr}]}
        if linked_issue:
            issue_links.guard_change(conn, task, linked_issue, completion)
            issue_links.apply(task, linked_issue, actor, completion)
        closed = pr["state"] in {"CLOSED", "MERGED"}
        conn.execute("INSERT INTO conversations (id,project_slug,status,started_at,last_activity_at,author,requested_by,pr_url,issue_number,task_json,summary,closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (cid, project["slug"], "closed" if closed else "open", stamp, stamp,
                      actor["github_login"], who, pr["url"], issue_number, json.dumps(task), title,
                      stamp if closed else None))
        return snapshot(task)
    return await _transaction(db, work)


async def refresh(db, project, cid, who, *, authorize=None):
    initial = await db.get_conversation_task(cid)
    if initial.get("mode") != "external_pr":
        raise ValueError("La tarea no es un registro de PR externo")
    actor = {"relay_actor": who}
    try:
        actor = await actor_identity(who)
        pr = await read_snapshot(project, initial["external_pr"]["url"], initial.get("issue_number"))
    except (TaskPRError, AccountError, ValueError, OSError):
        await _authorize(project, authorize)
        await _failed_read(db, initial, actor)
        raise
    project = await _authorize(project, authorize)
    def work(conn):
        task = _load(conn, cid)
        # Optimistic lease across remote I/O: a slower refresh cannot overwrite a newer one.
        if task["external_pr"].get("observed_at") != initial["external_pr"].get("observed_at"):
            raise ObservationConflict("Otra actualización terminó primero; vuelve a actualizar")
        if pr["repo_id"] != task["external_pr"]["repo_id"] or pr["number"] != task["external_pr"]["number"]:
            raise ValueError("La identidad remota de la PR cambió")
        _apply_remote(task, pr, project, actor, conn)
        return _save(conn, task, project)
    return await _transaction(db, work)


async def add_evidence(db, project, cid, body, who, *, authorize=None):
    key, summary = _text(body, "request_id", 128, True), _text(body, "summary", 12000, True)
    sha = body.get("head_sha", "")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise ValueError("head_sha debe ser un commit completo")
    # Observe the remote head before accepting a new declaration. An idempotent
    # retry of an existing declaration returns its original record, even if stale.
    initial = await db.get_conversation_task(cid)
    previous = next((e for e in initial.get("evidence", []) if e["request_id"] == key and e["actor"]["relay_actor"] == who), None)
    if previous is None:
        # Retry only a lost observation lease; head/identity failures remain fatal.
        for attempt in range(3):
            try:
                await refresh(db, project, cid, who, authorize=authorize)
                break
            except ObservationConflict:
                if attempt == 2:
                    raise
    actor = await actor_identity(who)
    project = await _authorize(project, authorize)
    def work(conn):
        task = _load(conn, cid)
        previous = next((e for e in task["evidence"] if e["request_id"] == key and e["actor"]["relay_actor"] == who), None)
        if previous:
            if previous["summary"] != summary or previous["head_sha"] != sha or previous["actor"]["github_id"] != actor["github_id"]:
                raise ValueError("request_id ya fue usado con otra evidencia o identidad")
            return snapshot(task, project)
        if sha != task["external_pr"]["head_sha"]:
            raise ValueError("El commit cambió; actualiza y declara evidencia para el head vigente")
        entry = {"request_id": key, "summary": summary, "head_sha": sha, "actor": actor, "at": now(), "kind": "declared"}
        task["evidence"].append(entry)
        task["history"].append({"kind": "declared_evidence", "at": entry["at"], "actor": actor, "evidence": entry})
        return _save(conn, task, project)
    return await _transaction(db, work)
