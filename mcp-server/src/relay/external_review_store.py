"""Small durable queue for advisory external-PR reviews, never code execution."""
from __future__ import annotations

import json
import re
import uuid

from .task_pr import now

_RETRY_STATES = {"blocked", "failed", "superseded"}

_SCHEMA = """CREATE TABLE IF NOT EXISTS external_review_jobs (
    job_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
    project_slug TEXT NOT NULL, requested_by TEXT NOT NULL,
    repo TEXT NOT NULL, repo_id INTEGER NOT NULL, pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL, base_sha TEXT NOT NULL, model TEXT NOT NULL,
    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, run_id TEXT,
    result_json TEXT, error TEXT NOT NULL DEFAULT '',
    context_revision INTEGER NOT NULL DEFAULT 0, context_history_json TEXT NOT NULL DEFAULT '[]',
    notifications_json TEXT NOT NULL DEFAULT '[]',
    retry_required INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
    UNIQUE(repo_id,pr_number,head_sha))"""


async def ensure_schema(db):
    def work(conn):
        conn.execute(_SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(external_review_jobs)")}
        for column, definition in (("context_revision", "INTEGER NOT NULL DEFAULT 0"),
                                   ("context_history_json", "TEXT NOT NULL DEFAULT '[]'"),
                                   ("notifications_json", "TEXT NOT NULL DEFAULT '[]'"),
                                   ("retry_required", "INTEGER NOT NULL DEFAULT 0")):
            if column not in columns:
                conn.execute(f"ALTER TABLE external_review_jobs ADD COLUMN {column} {definition}")
        if "retry_required" not in columns:
            conn.execute("UPDATE external_review_jobs SET retry_required=1 "
                         "WHERE state IN ('blocked','failed','superseded') AND attempts>0")
            rows = conn.execute("SELECT job_id,context_history_json FROM external_review_jobs "
                                "WHERE state IN ('blocked','failed','superseded') AND attempts=0 "
                                "AND json_valid(context_history_json)").fetchall()
            for row in rows:
                history = json.loads(row["context_history_json"])
                if not isinstance(history, list):
                    continue
                for previous in reversed(history):
                    if (not isinstance(previous, dict) or previous.get("state") not in _RETRY_STATES
                            or type(previous.get("attempts")) is not int or previous["attempts"] < 0):
                        break
                    if previous["attempts"] > 0:
                        conn.execute("UPDATE external_review_jobs SET retry_required=1 WHERE job_id=?", (row["job_id"],))
                        break
    await _transaction(db, work)


async def _transaction(db, operation):
    def work():
        conn = db._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = operation(conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    return await db._in_thread(work)


def _decode(row):
    if row is None:
        return None
    job = dict(row)
    job["result"] = json.loads(job.pop("result_json") or "null")
    job["context_history"] = json.loads(job.pop("context_history_json", "[]"))
    job["notifications"] = json.loads(job.pop("notifications_json", "[]")) or (job["result"] or {}).get("notifications", [])
    job.setdefault("context_revision", 0)
    job.setdefault("retry_required", int(job.get("state") in _RETRY_STATES and job.get("attempts", 0) > 0))
    return job


def publication_id(job):
    """Revision zero keeps existing outbox correlations after the additive upgrade."""
    revision = job.get("context_revision", 0)
    return job["job_id"] + (f":r{revision}" if revision else "")


def completed_result(job):
    """Receipts and other metadata can never establish a successful model review."""
    result = job.get("result")
    return (isinstance(result, dict) and job.get("attempts", 0) > 0
            and bool(job.get("started_at")) and bool(job.get("finished_at"))
            and isinstance(result.get("summary"), str) and bool(result["summary"].strip())
            and len(result["summary"]) <= 4000
            and isinstance(result.get("comments"), list) and len(result["comments"]) <= 25
            and all(isinstance(row, dict) and set(row) == {"path", "line", "side", "body"}
                    and isinstance(row["path"], str) and bool(row["path"])
                    and type(row["line"]) is int and row["line"] > 0 and row["side"] == "RIGHT"
                    and isinstance(row["body"], str) and 0 < len(row["body"]) <= 2000
                    for row in result["comments"])
            and len({(row["path"], row["line"], row["side"], row["body"]) for row in result["comments"]}) == len(result["comments"])
            and result.get("coverage") == "complete" and result.get("tests_executed") is False
            and all(result.get(key) == job.get(key) for key in ("model", "head_sha", "base_sha")))


def _public_context(job):
    if not job:
        return None
    result = job.get("result") or {}
    invalid = job.get("state") == "reviewed" and not completed_result(job)
    return {**{key: job.get(key) for key in (
        "job_id", "head_sha", "base_sha", "model", "state", "attempts", "error",
        "created_at", "updated_at", "started_at", "finished_at")},
        "context_revision": job.get("context_revision", 0), "publication_correlation_id": publication_id(job),
        "notifications": job.get("notifications", result.get("notifications", [])),
        "retry_required": bool(job.get("retry_required", False)),
        **({"state": "blocked", "error": "Falta un resultado de modelo válido para este contexto; solicita un reintento explícito"} if invalid else {}),
        "automated": True, "tests_executed": False,
        "summary": result.get("summary", ""), "comments": result.get("comments", []),
        "coverage": result.get("coverage"), "checks_read": result.get("checks_read", []),
        "read_at": result.get("read_at"), "declared_tests": result.get("declared_tests", []),
        "guideline_sources": result.get("guideline_sources", []), "guideline_base": result.get("guideline_base"),
        "review_inputs_digest": result.get("review_inputs_digest"), "issue_source": result.get("issue_source")}


def current_context(job, project, task):
    """A report with old or absent provenance cannot represent current inputs."""
    from . import external_review_context as context
    result = job.get("result") or {}
    sources = result.get("guideline_sources")
    if not isinstance(sources, list) or any(not isinstance(row, dict) for row in sources):
        return False
    team = [row for row in sources if row.get("source") == "project.system_prompt"]
    try:
        return (len(team) == 1 and team[0].get("sha256") == context.team_digest(project)
                and result.get("review_inputs_digest") == context.inputs_digest(task, job["head_sha"]))
    except (ValueError, TypeError, KeyError):
        return False


def public(job):
    if not job:
        return None
    return {**_public_context(job), "context_history": [_public_context(previous)
            for previous in job.get("context_history", [])]}


def policy(project):
    value = (project.get("defaults_json") or {}).get("external_pr_review_policy")
    return value if isinstance(value, dict) else {}


def enqueue(conn, task, project, actor, *, retry=False, observed_issue_digest=None):
    """Caller owns BEGIN IMMEDIATE and persists task's projection in that transaction."""
    if observed_issue_digest is not None and (not retry or not isinstance(observed_issue_digest, str)
                                              or not re.fullmatch(r"[0-9a-f]{64}", observed_issue_digest)):
        raise ValueError("La observación del issue requiere un reintento explícito y una huella verificable")
    pr, stamp = task["external_pr"], now()
    config = policy(project)
    model = config.get("model") if isinstance(config.get("model"), str) else ""
    model = model.strip()
    error = ("La revisión automática no está habilitada para este proyecto" if config.get("enabled") is not True
             else "Configura un modelo explícito para la revisión automática" if not model
             else "La PR debe estar abierta para solicitar revisión automática" if pr.get("state", "OPEN") != "OPEN" else "")
    state = "blocked" if error else "queued"
    job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"relay:external-review:{pr['repo_id']}:{pr['number']}:{pr['head_sha']}"))
    conn.execute("UPDATE external_review_jobs SET state='superseded',run_id=NULL,"
                 "retry_required=CASE WHEN attempts>0 THEN 1 ELSE retry_required END,updated_at=? "
                 "WHERE repo_id=? AND pr_number=? AND head_sha<>? AND state<>'superseded'",
                 (stamp, pr["repo_id"], pr["number"], pr["head_sha"]))
    existing = _decode(conn.execute("SELECT * FROM external_review_jobs WHERE job_id=?", (job_id,)).fetchone())
    previous_issue = (existing.get("result") or {}).get("issue_source") if existing is not None else None
    previous_issue_digest = previous_issue.get("content_sha256") if isinstance(previous_issue, dict) else None
    issue_changed = bool(existing is not None and retry and observed_issue_digest is not None
                         and previous_issue_digest != observed_issue_digest)
    if existing is None:
        conn.execute("INSERT INTO external_review_jobs(job_id,conversation_id,project_slug,requested_by,"
                     "repo,repo_id,pr_number,head_sha,base_sha,model,state,error,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (job_id, task["id"], project["slug"], actor, pr["repo"], pr["repo_id"], pr["number"],
                      pr["head_sha"], pr["base_sha"], model, state, error, stamp, stamp))
    elif (existing["base_sha"] != pr["base_sha"]
          or retry and existing["model"] != model
          or retry and existing["state"] in _RETRY_STATES
          or retry and existing["state"] == "reviewed" and not completed_result(existing)
          or retry and completed_result(existing) and not current_context(existing, project, task)
          or issue_changed
          or existing["state"] == "blocked" and existing["attempts"] == 0 and not existing["retry_required"] and not error):
        if existing["requested_by"] != actor:
            raise PermissionError("El reintento debe conservar el actor que solicitó esta revisión")
        changed = (existing["base_sha"] != pr["base_sha"] or existing["model"] != model
                   or retry and completed_result(existing) and not current_context(existing, project, task)
                   or issue_changed)
        # A replacement report must not inherit an old effect's publication key.
        # Same-code retries still retain their accumulated model attempt count.
        replacement = changed or retry and not completed_result(existing)
        history = existing["context_history"]
        revision = existing["context_revision"]
        if replacement:
            history = [*history, {key: value for key, value in existing.items() if key != "context_history"}]
            revision += 1
        reusable = (not changed and not error and completed_result(existing) and existing["model"] == model
                    and existing["base_sha"] == pr["base_sha"])
        next_state = "reviewed" if reusable else state
        required = not retry and (existing["retry_required"] or
            existing["state"] in _RETRY_STATES and existing["attempts"] > 0)
        if required:
            next_state = existing["state"] if existing["state"] in _RETRY_STATES else "blocked"
            error = existing["error"] or "El contexto cambió: solicita un reintento explícito"
        conn.execute("UPDATE external_review_jobs SET state=?,model=?,attempts=?,error=?,run_id=NULL,result_json=?,"
                     "base_sha=?,context_revision=?,context_history_json=?,notifications_json=?,retry_required=?,updated_at=?,started_at=?,finished_at=? WHERE job_id=?",
                     (next_state, model, 0 if changed else existing["attempts"], error,
                      json.dumps(existing["result"], ensure_ascii=False) if reusable else None,
                      pr["base_sha"], revision, json.dumps(history, ensure_ascii=False),
                      json.dumps([] if replacement else existing["notifications"], ensure_ascii=False), int(required), stamp, existing["started_at"] if reusable else None,
                      existing["finished_at"] if reusable else None, job_id))
    job = _decode(conn.execute("SELECT * FROM external_review_jobs WHERE job_id=?", (job_id,)).fetchone())
    task["external_review"] = public(job)
    return job


async def _exists(db):
    return bool(await db.run("SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_review_jobs'"))


async def get(db, job_id):
    if not await _exists(db):
        return None
    rows = await db.run("SELECT * FROM external_review_jobs WHERE job_id=?", (job_id,))
    return _decode(rows[0]) if rows else None


async def current(db, cid, head_sha, *, base_sha=None):
    if not await _exists(db):
        return None
    rows = await db.run("SELECT * FROM external_review_jobs WHERE conversation_id=? AND head_sha=?",
                        (cid, head_sha))
    job = _decode(rows[0]) if rows else None
    return public(job) if job and (base_sha is None or job["base_sha"] == base_sha) else None


def _mirror(conn, job):
    conn.execute("UPDATE conversations SET task_json=json_set(task_json,'$.external_review',json(?)) "
                 "WHERE id=? AND json_extract(task_json,'$.external_pr.head_sha')=? "
                 "AND json_extract(task_json,'$.external_pr.base_sha')=?",
                 (json.dumps(public(job), ensure_ascii=False), job["conversation_id"], job["head_sha"], job["base_sha"]))


async def claim(db):
    await ensure_schema(db)
    def work(conn):
        row = conn.execute("SELECT j.* FROM external_review_jobs j JOIN conversations c ON c.id=j.conversation_id "
                           "WHERE j.state='queued' AND json_extract(c.task_json,'$.external_pr.head_sha')=j.head_sha "
                           "AND json_extract(c.task_json,'$.external_pr.base_sha')=j.base_sha "
                           "ORDER BY j.created_at,j.job_id LIMIT 1").fetchone()
        if row is None:
            return None
        stamp, run_id = now(), str(uuid.uuid4())
        conn.execute("UPDATE external_review_jobs SET state='running',attempts=attempts+1,run_id=?,started_at=?,"
                     "updated_at=?,error='' WHERE job_id=? AND state='queued'", (run_id, stamp, stamp, row["job_id"]))
        job = _decode(conn.execute("SELECT * FROM external_review_jobs WHERE job_id=?", (row["job_id"],)).fetchone())
        _mirror(conn, job)
        return job
    return await _transaction(db, work)


async def finish(db, job, state, result=None, error=""):
    if state not in {"reviewed", "blocked", "failed", "superseded"}:
        raise ValueError("Estado de revisión inválido")
    def work(conn):
        row = conn.execute("UPDATE external_review_jobs SET state=?,result_json=?,retry_required=?,error=?,finished_at=?,updated_at=? "
                           "WHERE job_id=? AND state='running' AND run_id=? RETURNING *",
                           (state, json.dumps(result, ensure_ascii=False) if result is not None else None, int(state in _RETRY_STATES),
                            error, now(), now(), job["job_id"], job["run_id"])).fetchone()
        if row is None:
            return False
        _mirror(conn, _decode(row))
        return True
    return await _transaction(db, work)


async def recover(db):
    await ensure_schema(db)
    def work(conn):
        rows = conn.execute("UPDATE external_review_jobs SET state='failed',run_id=NULL,retry_required=1,error=?,updated_at=? "
                            "WHERE state='running' RETURNING *",
                            ("Revisión interrumpida: solicita un reintento explícito; no se repitió el modelo", now())).fetchall()
        for row in rows:
            _mirror(conn, _decode(row))
        return len(rows)
    return await _transaction(db, work)


async def block_ready(db, job, error):
    def work(conn):
        row = conn.execute("UPDATE external_review_jobs SET state='blocked',retry_required=1,error=?,updated_at=? "
                           "WHERE job_id=? AND state='reviewed' AND context_revision=? RETURNING *",
                           (error, now(), job["job_id"], job.get("context_revision", 0))).fetchone()
        if row:
            _mirror(conn, _decode(row))
    await _transaction(db, work)


async def record_notifications(db, job, receipts):
    """Notification delivery metadata never manufactures or changes a model result."""
    if not isinstance(receipts, list) or len(receipts) > 50:
        raise ValueError("Recibos de notificación inválidos")
    return bool(await db.run("UPDATE external_review_jobs SET notifications_json=? "
                             "WHERE job_id=? AND context_revision=? RETURNING job_id",
                             (json.dumps(receipts, ensure_ascii=False), job["job_id"], job.get("context_revision", 0))))
