"""Durable Relay inbox receipts; never sends to Discord, email or a provider."""
from __future__ import annotations

import json
import re
import uuid
from urllib.parse import urlsplit

from . import external_review_store, identity
from .external_pr import parse_url
from .issue_sync_store import now, transaction


async def ensure_schema(db):
    await db.run("""CREATE TABLE IF NOT EXISTS external_review_notifications (
        id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
        recipient_email TEXT NOT NULL REFERENCES users(email) ON DELETE CASCADE,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        project_slug TEXT NOT NULL, head_sha TEXT NOT NULL,
        summary TEXT NOT NULL CHECK(length(summary)<=500),
        review_url TEXT NOT NULL DEFAULT '', pr_url TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('created','read')),
        created_at TEXT NOT NULL, read_at TEXT,
        UNIQUE(job_id,recipient_email))""")
    await db.run("CREATE INDEX IF NOT EXISTS external_review_inbox_recipient "
                 "ON external_review_notifications(recipient_email,created_at)")


def _email(value):
    if not isinstance(value, str) or len(value) > 254:
        raise ValueError("El destinatario debe ser un correo explícito")
    value = value.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        raise ValueError("El destinatario debe ser un correo explícito")
    return value


def _project(conn, slug):
    row = conn.execute("SELECT slug,enabled,defaults_json FROM projects WHERE slug=? COLLATE NOCASE", (slug,)).fetchone()
    if row is None:
        return None
    project = dict(row)
    try:
        project["defaults_json"] = json.loads(project["defaults_json"] or "{}")
    except (ValueError, TypeError):
        return None
    return project if isinstance(project["defaults_json"], dict) else None


def _authorized(conn, email, project):
    if (not project or not project["enabled"]
            or project["defaults_json"].get("read_only")):
        return False
    row = conn.execute("SELECT role,enabled,project_slugs_json FROM users WHERE email=?", (email,)).fetchone()
    if row is None:
        return False
    user = dict(row)
    try:
        user["project_slugs"] = json.loads(user.pop("project_slugs_json") or "[]")
    except (ValueError, TypeError):
        return False
    if not isinstance(user["project_slugs"], list):
        return False
    return identity.user_can_write_project(user, project["slug"])


def _policy(project):
    return project["defaults_json"].get("external_pr_review_policy", {}) if project else {}


def _configured(project):
    policy = _policy(project)
    values = policy.get("recipients", []) if isinstance(policy, dict) else []
    if not isinstance(values, list):
        return set()
    try:
        return {_email(value) for value in values}
    except ValueError:
        return set()


def _payload(project, job):
    if not isinstance(project, dict) or not isinstance(job, dict):
        raise ValueError("La revisión debe identificar su proyecto y tarea")
    key = job.get("job_id") or job.get("id")
    if job.get("job_id") and job.get("id") and job["job_id"] != job["id"]:
        raise ValueError("El identificador de revisión no coincide")
    cid, slug, sha = job.get("conversation_id"), project.get("slug"), job.get("head_sha")
    job_slug = job.get("project_slug")
    if (not isinstance(key, str) or not key or len(key) > 200
            or not isinstance(cid, str) or not cid or len(cid) > 200
            or not isinstance(slug, str) or not isinstance(job_slug, str) or job_slug.casefold() != slug.casefold()
            or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)):
        raise ValueError("La revisión no identifica su tarea, proyecto y SHA")
    pr_url = job.get("pr_url")
    if not pr_url:
        pr_url = f"https://github.com/{job.get('repo', '')}/pull/{job.get('pr_number', '')}"
    parse_url(pr_url)
    pr_url = pr_url.rstrip("/")
    review_url = job.get("review_url") or job.get("report_url") or ""
    if not isinstance(review_url, str) or len(review_url) > 1000:
        raise ValueError("El enlace de revisión no es válido")
    if review_url:
        url, pr = urlsplit(review_url), urlsplit(pr_url)
        if (url.scheme != "https" or url.netloc != "github.com" or url.query
                or url.path.rstrip("/") not in {pr.path, pr.path + "/files"}):
            raise ValueError("El enlace de revisión debe pertenecer al mismo PR de GitHub")
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    if job.get("state") in {"blocked", "failed", "error", "cancelled"}:
        summary = "Revisión automática bloqueada; requiere revisión humana."
    elif result.get("comments"):
        summary = "Revisión automática con hallazgos; requiere revisión humana."
    else:
        summary = "Revisión automática lista para revisión humana."
    return {"job_id": key, "conversation_id": cid, "project_slug": slug,
            "head_sha": sha, "summary": summary, "pr_url": pr_url, "review_url": review_url}


def _context_changed(conn, job, payload):
    stable_id = job.get("review_job_id")
    if not isinstance(stable_id, str) or not stable_id:
        return False
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_review_jobs'").fetchone():
        return False
    current = conn.execute("SELECT * FROM external_review_jobs WHERE job_id=?", (stable_id,)).fetchone()
    if current is None:
        return False
    current = dict(current)
    return (current.get("conversation_id") != payload["conversation_id"]
            or current.get("head_sha") != payload["head_sha"]
            or current.get("base_sha") != job.get("base_sha")
            or external_review_store.publication_id(current) != payload["job_id"])


async def publish(db, project, job, recipients):
    """Create local receipts keyed by the server-owned publication context ID.

    The caller supplies publication_id(job) as job_id: revision zero preserves
    existing receipts, and a new immutable context gets a separate unread row.
    """
    if not isinstance(recipients, list) or len(recipients) > 50:
        raise ValueError("recipients debe ser una lista explícita de hasta 50 correos")
    recipients = list(dict.fromkeys(_email(value) for value in recipients))
    payload = _payload(project, job)
    await ensure_schema(db)
    def work(conn):
        current = _project(conn, payload["project_slug"])
        conv = conn.execute("SELECT project_slug,task_json FROM conversations WHERE id=?", (payload["conversation_id"],)).fetchone()
        try:
            external = bool(conv) and json.loads(conv["task_json"] or "{}").get("mode") == "external_pr"
        except (ValueError, TypeError, AttributeError):
            external = False
        if not external or conv["project_slug"].casefold() != payload["project_slug"].casefold():
            raise ValueError("La revisión no pertenece al registro externo indicado")
        policy = _policy(current)
        enabled = isinstance(policy, dict) and policy.get("enabled") is True
        configured, receipts = _configured(current), []
        context_changed = _context_changed(conn, job, payload)
        for email in recipients:
            receipt = {"email": email, "status": "blocked", "reason": "recipient_not_authorized"}
            if not enabled:
                receipt["reason"] = "review_policy_disabled"
            elif context_changed:
                receipt["reason"] = "review_context_changed"
            elif email not in configured:
                receipt["reason"] = "recipient_not_configured"
            elif _authorized(conn, email, current):
                previous = conn.execute("SELECT * FROM external_review_notifications WHERE job_id=? AND recipient_email=?",
                                        (payload["job_id"], email)).fetchone()
                immutable = ("job_id", "conversation_id", "project_slug", "head_sha", "pr_url")
                if previous and any(previous[key] != payload[key] for key in immutable):
                    raise ValueError("La revisión ya tiene otro contenido o destino en la bandeja")
                if previous:
                    changed = any(previous[key] != payload[key] for key in ("summary", "review_url"))
                    if changed:
                        conn.execute("UPDATE external_review_notifications SET summary=?,review_url=?,state='created',read_at=NULL WHERE id=?",
                                     (payload["summary"], payload["review_url"], previous["id"]))
                    receipt = {"email": email, "status": "existing", "notification_id": previous["id"], "changed": changed}
                else:
                    key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"relay:external-review-inbox:{payload['job_id']}:{email}"))
                    conn.execute("INSERT INTO external_review_notifications "
                                 "(id,job_id,recipient_email,conversation_id,project_slug,head_sha,summary,review_url,pr_url,state,created_at) "
                                 "VALUES (?,?,?,?,?,?,?,?,?,'created',?)",
                                 (key, payload["job_id"], email, payload["conversation_id"], payload["project_slug"],
                                  payload["head_sha"], payload["summary"], payload["review_url"], payload["pr_url"], now()))
                    receipt = {"email": email, "status": "created", "notification_id": key}
            receipts.append(receipt)
        return {"recipients": receipts}
    return await transaction(db, work)


def _projection(conn, row):
    result = dict(row)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_review_jobs'").fetchone():
        return result
    columns = {column["name"] for column in conn.execute("PRAGMA table_info(external_review_jobs)")}
    # Old inboxes may exist without durable context metadata. Do not guess
    # staleness or mutate the legacy receipt while the schema is unavailable.
    if not {"job_id", "conversation_id", "head_sha", "base_sha", "context_revision"}.issubset(columns):
        return result
    conv = conn.execute("SELECT task_json FROM conversations WHERE id=?", (row["conversation_id"],)).fetchone()
    try:
        pr = json.loads(conv["task_json"] or "{}").get("external_pr", {}) if conv else {}
    except (ValueError, TypeError, AttributeError):
        return result
    if not isinstance(pr, dict) or any(not isinstance(pr.get(key), str) or not re.fullmatch(r"[0-9a-f]{40}", pr[key])
                                       for key in ("head_sha", "base_sha")):
        return result
    current = conn.execute("SELECT job_id,context_revision,base_sha FROM external_review_jobs "
                           "WHERE conversation_id=? AND head_sha=? AND base_sha=?",
                           (row["conversation_id"], pr.get("head_sha"), pr.get("base_sha"))).fetchone()
    if current is None:
        # Legacy 0.3 may have observed base B without replacing its base-A job.
        # That durable mismatch proves history before the next refresh creates B.
        current = conn.execute("SELECT job_id,context_revision,base_sha FROM external_review_jobs "
                               "WHERE conversation_id=? AND head_sha=?",
                               (row["conversation_id"], pr["head_sha"])).fetchone()
    if current is None or type(current["context_revision"]) is not int or current["context_revision"] < 0:
        return result
    if not isinstance(current["base_sha"], str) or not re.fullmatch(r"[0-9a-f]{40}", current["base_sha"]):
        return result
    correlation = external_review_store.publication_id(dict(current))
    historical = current["base_sha"] != pr["base_sha"] or row["job_id"] != correlation
    result.update(historical=historical, publication_correlation_id=row["job_id"])
    if historical:
        result.update(state="superseded", receipt_state=row["state"])
    else:
        result["review_job_id"] = current["job_id"]
    return result


async def list_for(db, email):
    email = _email(email)
    await ensure_schema(db)
    def work(conn):
        rows = conn.execute("SELECT * FROM external_review_notifications WHERE recipient_email=? "
                            "ORDER BY created_at DESC,id DESC", (email,)).fetchall()
        return [_projection(conn, row) for row in rows if _authorized(conn, email, _project(conn, row["project_slug"]))]
    return await transaction(db, work)


async def mark_read(db, notification_id, email):
    email = _email(email)
    await ensure_schema(db)
    def work(conn):
        row = conn.execute("SELECT * FROM external_review_notifications WHERE id=? AND recipient_email=?",
                           (notification_id, email)).fetchone()
        if row is None or not _authorized(conn, email, _project(conn, row["project_slug"])):
            return None
        conn.execute("UPDATE external_review_notifications SET state='read',read_at=COALESCE(read_at,?) WHERE id=?",
                     (now(), notification_id))
        return _projection(conn, conn.execute("SELECT * FROM external_review_notifications WHERE id=?", (notification_id,)).fetchone())
    return await transaction(db, work)
