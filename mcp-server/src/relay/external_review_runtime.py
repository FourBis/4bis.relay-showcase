"""One bounded background review worker; independent of code execution queues."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from aiohttp import web

from . import external_pr, external_review, external_review_notify, external_review_publish, identity
from . import external_review_store as jobs
from . import external_review_outbox as effects_store
from . import external_evidence_images
from .app_state import BG_TASKS_KEY, DB_KEY
from .user_accounts import bind_actor

logger = logging.getLogger("relay.external_review")
RUNNER = web.AppKey("external_review_runner", asyncio.Task)
NEXT_POLL = web.AppKey("external_review_next_poll", float)


def _review_summary(job):
    result = job.get("result") or {}
    checks = result.get("checks_read") or []
    observed = result.get("read_at") or "sin fecha acreditada"
    success = sum((c.get("conclusion") or c.get("status") or "").lower() == "success" for c in checks)
    failed = sum((c.get("conclusion") or c.get("status") or "").lower() in {
        "failure", "error", "cancelled", "timed_out", "action_required"} for c in checks)
    return (result.get("summary", "") + "\n\n"
            f"Checks leídos de GitHub para este SHA: {len(checks)}; success: {success}; "
            f"fallo/cancelado: {failed}; pendientes u otros: {len(checks)-success-failed}. "
            f"Observación: {observed}. Relay no ejecutó estas pruebas.\n"
            f"[Consultar checks actuales](https://github.com/{job['repo']}/pull/{job['pr_number']}/checks)")


async def recover(db):
    await jobs.recover(db)
    await external_review_publish.recover(db)
    await external_review_notify.ensure_schema(db)
    await external_evidence_images.cleanup_confirmed(db)


async def authorized_project(db, slug, actor):
    project = await db.get_project(slug)
    user = next((u for u in await db.list_users() if u["email"].lower() == actor.lower()), None)
    if (not project or not project.get("enabled") or (project.get("defaults_json") or {}).get("read_only")
            or not user or not identity.user_can_write_project(user, slug)):
        raise PermissionError("La cuenta ya no tiene permiso para este proyecto")
    return project


async def _observe(db):
    # Monitoring is opt-in. It never upgrades the policy or adopts another actor.
    # Rotation is scheduling state only: denied records must not acquire fake
    # activity or keep the next eligible record outside the bounded batch.
    cursor = getattr(db, "_external_review_observe_cursor", "")
    query = ("SELECT c.id,c.project_slug,c.requested_by FROM conversations c JOIN projects p "
             "ON p.slug=c.project_slug COLLATE NOCASE WHERE c.status='open' AND p.enabled=1 "
             "AND json_extract(c.task_json,'$.mode')='external_pr' "
             "AND json_extract(p.defaults_json,'$.external_pr_review_policy.enabled')=1 "
             "AND c.id>? ORDER BY c.id LIMIT 100")
    for after in (cursor, "") if cursor else ("",):
        rows = await db.run(query, (after,))
        if rows:
            break
    if not rows:
        db._external_review_observe_cursor = ""
    for row in rows:
        # Advance before any preparation await, including failed authorization.
        db._external_review_observe_cursor = row["id"]
        project = await db.get_project(row["project_slug"])
        policy = jobs.policy(project or {})
        if not project or not project.get("enabled") or policy.get("enabled") is not True:
            continue
        actor = row["requested_by"]
        try:
            project = await authorized_project(db, row["project_slug"], actor)
            with bind_actor(db, actor):
                await external_pr.refresh(db, project, row["id"], actor,
                    authorize=lambda: authorized_project(db, row["project_slug"], actor))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Remote failures already persist verification_error. Do not copy
            # provider stderr, code, credentials or arbitrary exceptions to logs.
            logger.warning("External PR observation unavailable; retained previous observation")


async def _mirror_to_issue(db):
    """Replica en el issue vinculado cada informe ya encolado para el PR (una vez por informe)."""
    rows = await db.run("SELECT e.* FROM external_review_effects e WHERE e.kind='review' AND NOT EXISTS("
                        "SELECT 1 FROM external_review_effects i WHERE i.kind='review_issue' "
                        "AND i.conversation_id=e.conversation_id AND i.correlation_id=e.correlation_id "
                        "AND i.head_sha=e.head_sha AND i.actor=e.actor) ORDER BY e.created_at LIMIT 10")
    for row in rows:
        try:
            task = await db.get_conversation_task(row["conversation_id"])
            issue = (task or {}).get("issue_link") or {}
            if type(issue.get("number")) is not int:
                continue
            payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
            comments = [{**c, "body": c["body"].split(": ", 1)[1] if c["body"].startswith(external_review_publish.BOT_MARK) else c["body"]}
                        for c in payload.get("comments", [])]
            await external_review_publish.enqueue_effect(
                db, row["conversation_id"], "review_issue", row["correlation_id"], row["head_sha"],
                {"summary": payload["summary"], "comments": comments, "model": payload["model"],
                 "base_sha": payload.get("base_sha"), "context_revision": payload.get("context_revision", 0),
                 "target": {"kind": "issue", "number": issue["number"]}}, row["actor"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("External review issue mirror rejected; the PR publication is unaffected")


async def _publish(db):
    for job in await external_review.publication_jobs(db, limit=10):
        try:
            result = job.get("result") or {}
            await external_review_publish.enqueue_effect(
                db, job["conversation_id"], "review", jobs.publication_id(job), job["head_sha"],
                {"summary": _review_summary(job), "comments": result.get("comments", []), "model": job["model"],
                 "base_sha": job["base_sha"], "context_revision": job.get("context_revision", 0)}, job["requested_by"])
        except asyncio.CancelledError:
            raise
        except Exception:
            await jobs.block_ready(db, job, "La revisión no produjo una publicación válida; requiere revisión o reintento explícito")
            logger.warning("External review publication intent rejected; other jobs remain eligible")
    await _mirror_to_issue(db)
    # Evidence is enqueued atomically with its declaration, not reconstructed
    # from legacy declarations and not sent once per UI refresh.
    rows = await db.run("SELECT e.conversation_id,c.project_slug,e.actor FROM external_review_effects e "
                        "JOIN conversations c ON c.id=e.conversation_id WHERE "
                        "(e.state='pending' AND e.attempts<3) OR (e.state='blocked' AND e.attempts=0) "
                        "OR (e.state='uncertain' AND e.read_attempts<3) "
                        "GROUP BY e.conversation_id,e.actor ORDER BY MIN(e.updated_at) LIMIT 20")
    for row in rows:
        blocked = False
        try:
            project = await db.get_project(row["project_slug"])
            if not project:
                blocked = True
                continue
            await external_review_publish.deliver_pending(db, project, row["conversation_id"], row["actor"])
        except asyncio.CancelledError:
            raise
        except Exception:
            blocked = True
            logger.warning("External PR publication unavailable; durable state retained")
        finally:
            await effects_store.defer_group(db, row["conversation_id"], row["actor"], blocked=blocked)


async def _notify(db):
    cursor = getattr(db, "_external_review_notify_cursor", "")
    rows = await db.run("SELECT * FROM external_review_jobs WHERE state IN ('reviewed','blocked','failed') "
                        "AND job_id>? ORDER BY job_id LIMIT 50", (cursor,))
    db._external_review_notify_cursor = rows[-1]["job_id"] if rows else ""
    for row in rows:
        job = await jobs.get(db, row["job_id"])
        project = await db.get_project(job["project_slug"])
        if not project or jobs.policy(project).get("enabled") is not True:
            continue
        task = await db.get_conversation_task(job["conversation_id"])
        if (task.get("external_pr") or {}).get("head_sha") != job["head_sha"]:
            continue
        delivery = await external_review_publish.status(db, job["conversation_id"])
        correlation = jobs.publication_id(job)
        effects = [e for e in delivery["effects"] if e["kind"] == "review" and e["head_sha"] == job["head_sha"]
                   and e["correlation_id"] == correlation]
        current = effects[-1] if effects else {}
        neutral = {**job, "job_id": correlation, "review_job_id": job["job_id"],
                   "pr_url": task["external_pr"]["url"],
                   "comments": (job.get("result") or {}).get("comments", [])}
        if job["state"] == "reviewed" and not jobs.completed_result(job):
            neutral["state"] = "blocked"
        elif current.get("state") == "confirmed":
            neutral["review_url"] = (current.get("remote") or {}).get("url", "")
        elif current and current.get("state") not in {"pending", "processing"}:
            neutral["state"] = "blocked"
        receipt = await external_review_notify.publish(db, project, neutral, jobs.policy(project).get("recipients", []))
        await jobs.record_notifications(db, job, receipt["recipients"])


async def run_once(db, *, observe=True):
    await jobs.ensure_schema(db)
    await external_review_publish.ensure_schema(db)
    if observe:
        await _observe(db)
    await external_review.run_next(db)
    await _publish(db)
    await _notify(db)
    await external_evidence_images.cleanup_confirmed(db)


def tick(app):
    previous = app.get(RUNNER)
    if previous is not None and not previous.done():
        return previous
    if time.monotonic() < app.get(NEXT_POLL, 0):
        return None
    app[NEXT_POLL] = time.monotonic() + 60

    async def cycle():
        try:
            await run_once(app[DB_KEY])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("External review worker unavailable; durable jobs remain recoverable")

    task = asyncio.create_task(cycle(), name="relay-external-review")
    app[RUNNER] = task
    pending = app.setdefault(BG_TASKS_KEY, set())
    pending.add(task)
    task.add_done_callback(pending.discard)
    return task


async def stop(app):
    task = app.get(RUNNER)
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
