"""Explicit-identity GitHub publication, independent from review/model execution."""
from __future__ import annotations

import asyncio
import copy
import json
import re

import httpx

from . import external_pr, external_pr_access, external_review_context as context, external_review_outbox as store, github, identity, issue_links, user_accounts
from . import external_review_store as reviews
from .issue_sync_store import digest

_SHA = re.compile(r"[0-9a-f]{40}", re.ASCII)
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", re.ASCII)


class PublicationError(RuntimeError):
    pass


class Rejected(PublicationError):
    """A complete, definite HTTP rejection, eligible only for explicit retry."""


class StaleContext(PublicationError):
    """A pending review belongs to an older base or model context."""


async def _account(db, actor, expected_subject=None):
    with user_accounts.bind_actor(db, actor):
        account = await user_accounts.require_account("github")
    if expected_subject is not None and str(account.get("subject")) != str(expected_subject):
        raise PublicationError("Cambió la identidad de publicación; no se usará otra cuenta")
    return account


async def _api(db, actor, method, endpoint, payload=None, *, expected_subject=None, account=None):
    account = account or await _account(db, actor, expected_subject)
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            response = await client.request(method, f"https://api.github.com/{endpoint}",
                headers={"Authorization": f"Bearer {account['access_token']}",
                         "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                json=payload if method == "POST" else None)
        if not 200 <= response.status_code < 300:
            if method == "POST" and response.status_code in {400, 401, 403, 404, 409, 422, 429}:
                raise Rejected(f"GitHub rechazó la publicación (HTTP {response.status_code})")
            raise PublicationError("GitHub no confirmó el resultado remoto")
        result = response.json()
        if not isinstance(result, (dict, list)):
            raise ValueError("incomplete")
        return result
    except (httpx.HTTPError, ValueError) as exc:
        raise PublicationError("GitHub no confirmó una respuesta válida; no repetir sin reconciliar") from exc


def _text(value, label, maximum, *, content=False):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{label} inválido")
    clean = value.strip()
    return context.normalize_text(clean, maximum) if content else clean


# "review" va al PR (con comentarios inline); "review_issue" replica el informe en el issue vinculado.
REVIEW_KINDS = {"review", "review_issue"}
BOT_MARK = "🤖 Relay"
INLINE_PREFIX = f"{BOT_MARK} · revisión automática: "


def without_inline_prefix(body):
    """Quita solo el prefijo exacto que agrega el servidor; el resto del texto queda intacto."""
    return body[len(INLINE_PREFIX):] if isinstance(body, str) and body.startswith(INLINE_PREFIX) else body


def _target(task, payload, kind):
    pr = task["external_pr"]
    target = payload.get("target")
    if kind == "review":
        if target is not None and target != {"kind": "pr", "number": pr["number"]}:
            raise ValueError("La revisión sólo puede dirigirse a la PR registrada")
        return {"kind": "pr", "number": pr["number"]}
    if not isinstance(target, dict) or set(target) != {"kind", "number"} or type(target.get("number")) is not int:
        raise ValueError("Selecciona un único destino explícito: PR o issue vinculado")
    link = pr if target["kind"] == "pr" else task.get("issue_link", {}) if target["kind"] == "issue" else {}
    if (target["number"] != link.get("number") or link.get("repo_id") != pr["repo_id"]
            or str(link.get("repo", "")).casefold() != pr["repo"].casefold()):
        raise ValueError("El destino no es la PR ni el issue canónico vinculado")
    return copy.deepcopy(target)


def _comments(value, *, marked=False):
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError("La revisión admite como máximo 50 comentarios inline")
    result = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"path", "line", "side", "body"}:
            raise ValueError("Comentario inline inválido")
        path = _text(row["path"], "path", 1000)
        if (path.startswith(("/", "\\")) or "\\" in path or ":" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or type(row["line"]) is not int or not 1 <= row["line"] <= 2147483647
                or row["side"] not in {"LEFT", "RIGHT"}):
            raise ValueError("Ubicación inline inválida")
        body = _text(row["body"], "comentario", 4000, content=True)
        if marked and not body.startswith(INLINE_PREFIX):
            # Cada comentario inline se ve suelto en el diff: debe decir que lo escribió Relay.
            body = INLINE_PREFIX + body
        result.append({"path": path, "line": row["line"], "side": row["side"], "body": body})
    if len({digest(row) for row in result}) != len(result):
        raise ValueError("Comentarios inline duplicados")
    return result


async def _authorized(db, cid, actor, *, expected=None, sha=None, additional_actors=()):
    conv, task = await db.get_conversation(cid), await db.get_conversation_task(cid)
    project = await db.get_project(conv["project_slug"]) if conv else None
    users = {user["email"]: user for user in await db.list_users()}
    for who in {actor, *additional_actors}:
        issue_links.require_writer(project, who)
        user = users.get(who)
        if not user or not user.get("enabled", True) or not identity.user_can_write_project(user, project["slug"]):
            raise PermissionError("El solicitante ya no tiene acceso al proyecto en la base de datos")
    if task.get("mode") != "external_pr":
        raise ValueError("La publicación requiere un registro de PR externo")
    pr = task["external_pr"]
    if (not _REPO.fullmatch(pr.get("repo", "")) or pr["repo"].split("/")[1] in {".", ".."}
            or type(pr.get("repo_id")) is not int or pr["repo_id"] <= 0
            or type(pr.get("number")) is not int or pr["number"] <= 0):
        raise ValueError("Identidad de PR inválida")
    if expected and any(pr.get(k) != expected.get(k) for k in ("repo", "repo_id", "number")):
        raise PublicationError("Cambió la identidad del repositorio o PR")
    if sha and pr.get("head_sha") != sha:
        raise PublicationError("El SHA ya no es el head registrado; no se publica una revisión obsoleta")
    return project, task


def enqueue_in_transaction(conn, cid, kind, correlation_id, head_sha, payload, actor):
    """No network/commit: safe inside add_evidence's existing transaction."""
    if kind not in REVIEW_KINDS | {"evidence"} or not isinstance(payload, dict):
        raise ValueError("Tipo de publicación inválido")
    correlation_id = _text(correlation_id, "correlation_id", 128)
    if not isinstance(head_sha, str) or not _SHA.fullmatch(head_sha):
        raise ValueError("head_sha debe ser un commit completo")
    conv = conn.execute("SELECT project_slug,task_json FROM conversations WHERE id=?", (cid,)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE slug=? COLLATE NOCASE", (conv["project_slug"],)).fetchone() if conv else None
    if project:
        project = dict(project)
        project["defaults_json"] = json.loads(project["defaults_json"])
    issue_links.require_writer(project, actor)
    task = json.loads(conv["task_json"])
    if task.get("mode") != "external_pr":
        raise ValueError("La publicación requiere un registro de PR externo")
    clean = {"summary": _text(payload.get("summary"), "summary", 12000, content=True),
             "target": _target(task, payload, kind),
             "pr": {key: task["external_pr"][key] for key in ("repo", "repo_id", "number", "url")},
             "comments": _comments(payload.get("comments", []), marked=kind == "review") if kind in REVIEW_KINDS else [],
             "model": _text(payload.get("model"), "model", 200, content=True) if kind in REVIEW_KINDS else ""}
    if kind == "review_issue" and clean["target"].get("kind") != "issue":
        raise ValueError("La réplica del informe sólo se publica en el issue vinculado")
    if kind in REVIEW_KINDS:
        base = payload.get("base_sha", task["external_pr"].get("base_sha"))
        revision = payload.get("context_revision", (task.get("external_review") or {}).get("context_revision", 0))
        if not isinstance(base, str) or not _SHA.fullmatch(base) or type(revision) is not int or revision < 0:
            raise ValueError("La revisión requiere base y contexto verificables")
        if base != task["external_pr"].get("base_sha"):
            raise ValueError("La base cambió antes de encolar la publicación")
        clean.update(base_sha=base, context_revision=revision)
        projection = task.get("external_review") or {}
        job_id = projection.get("job_id", "")
        if job_id and (correlation_id == job_id or correlation_id.startswith(job_id + ":")):
            # Provenance is copied from the durable server-owned projection,
            # never accepted from a client-provided report body.
            clean.update(guideline_sources=projection.get("guideline_sources", []),
                         review_inputs_digest=projection.get("review_inputs_digest"))
    return store.enqueue_in_transaction(conn, cid, kind, correlation_id, head_sha, clean, actor)


async def enqueue_effect(db, cid, kind, correlation_id, head_sha, payload, actor):
    return await store.transaction(db, lambda conn: enqueue_in_transaction(
        conn, cid, kind, correlation_id, head_sha, payload, actor))


def _policy(project, actor, kind):
    value = (project.get("defaults_json") or {}).get("external_pr_review_policy")
    if not isinstance(value, dict) or value.get("publication_mode") not in {"personal", "bot"}:
        raise PublicationError("Publicación bloqueada: configura modo explícito e identidad de publicación")
    if kind in REVIEW_KINDS and value.get("enabled") is not True:
        raise PublicationError("La revisión automática está desactivada; no se publica el resultado pendiente")
    expected = value.get("publisher_actor") or actor
    if not isinstance(expected, str) or expected.strip().lower() != actor:
        raise PublicationError("La identidad configurada no es la persona que solicitó la publicación")
    return {"publication_mode": value["publication_mode"], "publisher_actor": actor}


async def _publisher(db, project, policy):
    actor = policy["publisher_actor"]
    issue_links.require_writer(project, actor)
    with user_accounts.bind_actor(db, actor):
        account = await user_accounts.require_account("github")
    user = await _api(db, actor, "GET", "user")
    if (not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0
            or str(user["id"]) != str(account.get("subject")) or user.get("login") != account.get("login")
            or user.get("type") != ("Bot" if policy["publication_mode"] == "bot" else "User")):
        raise PublicationError("La credencial no confirma la identidad y tipo configurados; no hay fallback personal")
    return {"actor": actor, "id": user["id"], "login": user["login"], "type": user["type"],
            "mode": policy["publication_mode"]}


def _body(event):
    payload, sha = event["payload"], event["head_sha"]
    login = (event.get("publisher") or {}).get("login")
    account = (f"Publicado automáticamente por Relay con la cuenta de @{login}; no lo escribió esa persona.\n"
               if login and (event.get("publisher") or {}).get("mode") == "personal" else "")
    intro = (f"## {BOT_MARK} · revisión automática\n{account}Modelo `{payload['model']}`. "
             "Análisis estático: Relay no ejecutó pruebas ni aprueba merge o despliegue.\n"
             "Comentario informativo: no constituye aprobación humana independiente."
             if event["kind"] in REVIEW_KINDS else "Evidencia declarada en Relay; no constituye una aprobación humana.")
    base = f"\nBase: `{payload['base_sha']}`" if payload.get("base_sha") else ""
    findings = ""
    if event["kind"] == "review_issue":
        rows = [f"- `{row['path']}:{row['line']}` — {row['body']}" for row in payload.get("comments", [])]
        findings = ("\n\n### Hallazgos en el PR\n" + "\n".join(rows)) if rows else "\n\nSin hallazgos en el diff revisado."
    return (f"{intro}\n\nPR: {payload['pr']['url']}\nSHA: `{sha}`{base}\n\n{payload['summary']}{findings}\n\n"
            f"<!-- relay-external-effect:{event['id']} -->")


def _review_context(task, event):
    if event["kind"] not in REVIEW_KINDS:
        return
    projection = task.get("external_review") or {}
    current_base = task["external_pr"].get("base_sha")
    expected_base = event["payload"].get("base_sha") or projection.get("base_sha")
    if not expected_base or current_base != expected_base:
        raise StaleContext("La base cambió; la revisión anterior se conserva como histórica sin publicarla")
    job_id = projection.get("job_id", "")
    related = bool(job_id) and (event["correlation_id"] == job_id or event["correlation_id"].startswith(job_id + ":"))
    if related and (event["correlation_id"] != projection.get("publication_correlation_id", job_id)
                    or event["payload"].get("context_revision", 0) != projection.get("context_revision", 0)
                    or projection.get("base_sha") != current_base):
        raise StaleContext("Cambió el contexto de revisión; no se publica el informe anterior")


def _review_inputs(project, task, event):
    if event["kind"] not in REVIEW_KINDS:
        return
    projection = task.get("external_review") or {}
    job_id = projection.get("job_id", "")
    related = bool(job_id) and (event["correlation_id"] == job_id or event["correlation_id"].startswith(job_id + ":"))
    if related and not reviews.current_context({"head_sha": event["head_sha"], "result": event["payload"]}, project, task):
        raise StaleContext("Las directrices, el issue o la evidencia cambiaron; solicita una revisión actualizada")


async def _review_job(db, event):
    if event["kind"] not in REVIEW_KINDS or not await reviews._exists(db):
        return
    rows = await db.run("SELECT * FROM external_review_jobs WHERE conversation_id=? AND head_sha=? AND requested_by=?",
                        (event["conversation_id"], event["head_sha"], event["actor"]))
    _review_job_rows(rows, event)


def _review_job_rows(rows, event):
    for row in rows:
        job = reviews._decode(row)
        related = event["correlation_id"] == job["job_id"] or event["correlation_id"].startswith(job["job_id"] + ":")
        if related and job.get("state") != "reviewed":
            raise PublicationError("La revisión requiere revalidación o reintento explícito antes de publicar")
        if related and not reviews.completed_result(job):
            raise PublicationError("Falta un análisis completo verificable; no se publica un estado de notificación como revisión")


def _admission_tx(db, conn, event, *, actor, project, publisher, policy, account):
    """The local authorization point for this POST; no awaits or provider calls."""
    current = task = None
    for who in {actor, event["actor"], publisher["actor"]}:
        checked, checked_task, user = external_pr_access._record_tx(db, conn, event["conversation_id"], who)
        if user["email"] == identity.OWNER:
            raise PermissionError("La publicación requiere una cuenta personal habilitada")
        current, task = checked, checked_task
    if (current["slug"] != project["slug"] or current.get("repo_path") != project.get("repo_path")
            or digest((current.get("defaults_json") or {}).get("external_pr_review_policy"))
               != digest((project.get("defaults_json") or {}).get("external_pr_review_policy"))
            or digest(_policy(current, event["actor"], event["kind"])) != digest(policy)):
        raise PublicationError("Cambió la autorización o configuración antes de reservar la publicación")
    pr, expected = task["external_pr"], event["payload"]["pr"]
    if (any(pr.get(key) != expected.get(key) for key in ("repo", "repo_id", "number"))
            or pr.get("head_sha") != event["head_sha"] or task.get("read_only")):
        raise PublicationError("Cambió la PR o su permiso de escritura antes de publicar")
    _target(task, event["payload"], event["kind"])
    _review_context(task, event)
    _review_inputs(current, task, event)
    if event["kind"] in REVIEW_KINDS and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_review_jobs'").fetchone():
        rows = conn.execute("SELECT * FROM external_review_jobs WHERE conversation_id=? AND head_sha=? AND requested_by=?",
                            (event["conversation_id"], event["head_sha"], event["actor"])).fetchall()
        _review_job_rows(rows, event)
    connected = conn.execute("SELECT subject,login,status,updated_at,token_blob FROM user_accounts "
                             "WHERE email=? COLLATE NOCASE AND provider='github'", (publisher["actor"],)).fetchone()
    if (not connected or connected["status"] != "connected" or not connected["token_blob"]
            or str(connected["subject"]) != str(publisher["id"])
            or connected["login"] != publisher["login"] or account.get("login") != publisher["login"]
            or not account.get("_connection_revision")
            or user_accounts._connection_revision(connected) != account["_connection_revision"]):
        raise PublicationError("Cambió la cuenta conectada antes de reservar la publicación")


async def _pages(db, actor, endpoint):
    rows = []
    for page in range(1, 6):
        part = await _api(db, actor, "GET", f"{endpoint}?per_page=100&page={page}")
        if not isinstance(part, list) or any(not isinstance(row, dict) for row in part):
            raise PublicationError("GitHub no confirmó una lista completa")
        rows.extend(part)
        if len(part) < 100:
            return rows
    raise PublicationError("Más de 500 elementos: requiere reconciliación manual, sin publicar otra vez")


def _endpoint(event):
    payload = event["payload"]
    suffix = f"pulls/{payload['pr']['number']}/reviews" if event["kind"] == "review" else f"issues/{payload['target']['number']}/comments"
    return f"repos/{payload['pr']['repo']}/{suffix}"


async def _find(db, event):
    publisher, body = event["publisher"], _body(event)
    candidates = []
    for row in await _pages(db, event["actor"], _endpoint(event)):
        user = row.get("user") or {}
        if (not isinstance(user, dict) or type(user.get("id")) is not int or row.get("body") != body
                or user.get("id") != publisher.get("id") or user.get("type") != publisher.get("type")):
            continue
        if event["kind"] == "review":
            if row.get("commit_id") != event["head_sha"] or row.get("state") != "COMMENTED":
                continue
            if event["payload"]["comments"]:
                comments = await _pages(db, event["actor"], f"{_endpoint(event)}/{row['id']}/comments")
                normalized = [{"path": c.get("path"), "line": c.get("original_line", c.get("line")),
                               "side": c.get("side"), "body": c.get("body")} for c in comments]
                if sorted(map(digest, normalized)) != sorted(map(digest, event["payload"]["comments"])):
                    continue
        if type(row.get("id")) is not int or row["id"] <= 0:
            raise PublicationError("GitHub no confirmó la identidad del comentario")
        candidates.append({"id": row["id"], "url": row.get("html_url", ""), "head_sha": event["head_sha"]})
    if len(candidates) > 1:
        raise PublicationError("Hay varias publicaciones coincidentes; reconciliación manual necesaria")
    return candidates[0] if candidates else None


async def _guard_remote(db, project, task, event):
    pr, actor, sha = event["payload"]["pr"], event["actor"], event["head_sha"]
    with user_accounts.bind_actor(db, actor):
        configured = await github.repo_slug(project.get("repo_path") or "", refresh=True)
        same = await external_pr.resolves_to(configured, pr.get("repo_id"), pr["repo"])
    if not same:
        raise PublicationError("Cambió el repositorio autorizado del proyecto")
    repo = await _api(db, actor, "GET", f"repos/{pr['repo']}")
    if not isinstance(repo, dict) or repo.get("id") != pr["repo_id"] or str(repo.get("full_name", "")).casefold() != pr["repo"].casefold():
        raise PublicationError("GitHub no confirmó el repositorio autorizado")
    remote = await _api(db, actor, "GET", f"repos/{pr['repo']}/pulls/{pr['number']}")
    if (not isinstance(remote, dict) or remote.get("number") != pr["number"]
            or not external_pr.same_repository(remote.get("base"), pr["repo_id"], pr["repo"])
            or not external_pr.same_repository(remote.get("head"), pr["repo_id"], pr["repo"])
            or remote.get("head", {}).get("sha") != sha or remote.get("state") != "open"):
        raise PublicationError("La PR cambió o está cerrada; no se publica para otro head")
    _review_context(task, event)
    _review_inputs(project, task, event)
    if event["kind"] in REVIEW_KINDS:
        expected_base = event["payload"].get("base_sha") or (task.get("external_review") or {}).get("base_sha")
        if remote.get("base", {}).get("sha") != expected_base:
            raise StaleContext("GitHub cambió la base; no se publica la revisión del contexto anterior")
    _target(task, event["payload"], event["kind"])
    target = event["payload"]["target"]
    if target["kind"] == "issue":
        issue = await _api(db, actor, "GET", f"repos/{pr['repo']}/issues/{target['number']}")
        if (not isinstance(issue, dict) or issue.get("number") != target["number"] or "pull_request" in issue
                or issue.get("node_id") != task.get("issue_link", {}).get("node_id")):
            raise PublicationError("El issue de destino cambió")


async def _inline_guard(db, event):
    wanted = event["payload"]["comments"]
    if not wanted:
        return
    pr = event["payload"]["pr"]
    allowed = set()
    for file in await _pages(db, event["actor"], f"repos/{pr['repo']}/pulls/{pr['number']}/files"):
        old = new = None
        for line in (file.get("patch") or "").splitlines():
            hunk = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if hunk:
                old, new = int(hunk[1]), int(hunk[2])
            elif old is not None and line[:1] in {" ", "+", "-"}:
                if not line.startswith("+"):
                    allowed.add((file.get("filename"), old, "LEFT"))
                    old += 1
                if not line.startswith("-"):
                    allowed.add((file.get("filename"), new, "RIGHT"))
                    new += 1
    if any((row["path"], row["line"], row["side"]) not in allowed for row in wanted):
        raise PublicationError("El comentario inline no pertenece al diff verificado; no se publicó")


async def deliver_pending(db, project, cid, actor):
    await _authorized(db, cid, actor)
    event = await store.claim(db, cid)
    if event is None:
        return await status(db, cid)
    async def finish(state, **kwargs):
        await store.finish(db, event["id"], state, claim_token=event["claim_token"], **kwargs)
    attempted = event["previous_state"] == "uncertain"
    admission = {"committed": False}
    try:
        current, task = await _authorized(db, cid, event["actor"], expected=event["payload"]["pr"])
        if attempted:
            found = await _find(db, event)
            if found:
                await finish("confirmed", remote=found)
            else:
                await store.observation_failed(db, event["id"], event["claim_token"])
            return await status(db, cid)
        if task["external_pr"]["head_sha"] != event["head_sha"]:
            await finish("stale", error="El SHA registrado cambió; se conserva la intención histórica sin publicarla")
            return await status(db, cid)
        _review_context(task, event)
        await _review_job(db, event)
        policy = _policy(current, event["actor"], event["kind"])
        publisher = await _publisher(db, current, policy)
        if event["attempts"] and any(event["publisher"].get(key) != publisher[key] for key in ("id", "type", "actor")):
            raise PublicationError("Cambió la cuenta que publicó el intento anterior; no se usará otra identidad")
        event["publisher"] = publisher
        await store.bind_publisher(db, event["id"], publisher, event["claim_token"])
        await _guard_remote(db, current, task, event)
        await _inline_guard(db, event)
        found = await _find(db, event)
        if found:
            await finish("confirmed", remote=found)
            return await status(db, cid)
        fresh, fresh_task = await _authorized(db, cid, actor, expected=event["payload"]["pr"], sha=event["head_sha"])
        issue_links.require_writer(fresh, event["actor"])
        issue_links.require_writer(fresh, publisher["actor"])
        if digest(_policy(fresh, event["actor"], event["kind"])) != digest(policy) or fresh.get("repo_path") != current.get("repo_path"):
            raise PublicationError("Cambió la política o proyecto durante la lectura")
        _target(fresh_task, event["payload"], event["kind"])
        await _guard_remote(db, fresh, fresh_task, event)
        # Recheck local grants after the last external await.
        final, _ = await _authorized(db, cid, actor, expected=event["payload"]["pr"], sha=event["head_sha"])
        issue_links.require_writer(final, event["actor"])
        issue_links.require_writer(final, publisher["actor"])
        if digest(_policy(final, event["actor"], event["kind"])) != digest(policy) or final.get("repo_path") != fresh.get("repo_path"):
            raise PublicationError("Cambió la autorización antes de publicar")
        account = await _account(db, publisher["actor"], publisher["id"])
        await _review_job(db, event)
        latest, latest_task = await _authorized(db, cid, actor, expected=event["payload"]["pr"], sha=event["head_sha"],
                                              additional_actors=(event["actor"], publisher["actor"]))
        if digest(_policy(latest, event["actor"], event["kind"])) != digest(policy) or latest.get("repo_path") != final.get("repo_path"):
            raise PublicationError("Cambió la autorización o configuración mientras se obtenía la cuenta")
        _target(latest_task, event["payload"], event["kind"])
        _review_context(latest_task, event)
        _review_inputs(latest, latest_task, event)
        if not await store.admit_attempt(db, event, publisher,
                lambda conn, reserved: _admission_tx(db, conn, reserved, actor=actor, project=current,
                                                     publisher=publisher, policy=policy, account=account),
                admission=admission):
            raise PublicationError("Se agotó el límite de publicación")
        attempted = True
        payload = {"body": _body(event)}
        if event["kind"] == "review":
            payload.update(event="COMMENT", commit_id=event["head_sha"], comments=event["payload"]["comments"])
        await _api(db, publisher["actor"], "POST", _endpoint(event), payload, expected_subject=publisher["id"], account=account)
        found = await _find(db, event)
        if found is None:
            raise PublicationError("El comentario todavía no está confirmado en GitHub")
        await finish("confirmed", remote=found)
    except asyncio.CancelledError:
        await asyncio.shield(finish("uncertain" if attempted or admission["committed"] else "pending", error="Publicación interrumpida; reconciliar antes de continuar"))
        raise
    except Rejected as exc:
        await finish("rejected", error=str(exc))
    except StaleContext as exc:
        await finish("stale", error=str(exc))
    except Exception as exc:
        if attempted or admission["committed"]:
            if event["previous_state"] == "uncertain":
                await store.observation_failed(db, event["id"], event["claim_token"])
            else:
                await finish("uncertain", error="Resultado remoto sin confirmar; no se volverá a publicar automáticamente")
        else:
            error = str(exc) if isinstance(exc, (PublicationError, PermissionError, ValueError, user_accounts.AccountError)) else "Publicación bloqueada: verificación incompleta"
            await finish("blocked", error=error)
    return await status(db, cid)


async def status(db, cid):
    rows = await store.rows(db, cid)
    effects = [{**{key: row[key] for key in ("id", "kind", "correlation_id", "head_sha", "state", "attempts", "publisher", "remote", "error")},
                "target": row["payload"]["target"], "reconcile_required": row["state"] == "uncertain" and row["read_attempts"] >= 3} for row in rows]
    return {"effects": effects, "counts": {state: sum(row["state"] == state for row in rows) for state in {row["state"] for row in rows}}}


async def retry(db, cid, actor):
    await _authorized(db, cid, actor)
    await store.retry(db, cid)
    return await status(db, cid)


recover = store.recover
ensure_schema = store.ensure_schema
