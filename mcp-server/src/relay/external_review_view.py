"""Local-only projections; refreshing a task never runs a reviewer or POST."""
from __future__ import annotations

from . import external_review, external_review_publish
from . import external_review_store as jobs


async def enrich(db, task, *, can_configure=False):
    cid, sha = task["id"], task["external_pr"]["head_sha"]
    review = await external_review.snapshot(db, cid, sha, base_sha=task["external_pr"].get("base_sha"))
    delivery = await external_review_publish.status(db, cid)
    review = dict(review or task.get("external_review") or {})
    current_base = task["external_pr"].get("base_sha")
    same_context = review.get("head_sha") == sha and (not current_base or review.get("base_sha") == current_base)
    correlation = (review.get("publication_correlation_id") or review.get("job_id")) if same_context else None
    effects = [e for e in delivery["effects"] if e["kind"] == "review" and e["head_sha"] == sha
               and e.get("correlation_id") == correlation]
    history = [e for e in delivery["effects"] if e["kind"] == "review" and e not in effects]
    if history:
        review["publication_history"] = [{"id": e["id"], "head_sha": e["head_sha"],
            "correlation_id": e.get("correlation_id"), "state": e["state"],
            "url": (e.get("remote") or {}).get("url", "")} for e in history]
    if effects:
        effect = effects[-1]
        review["publication"] = {"state": effect["state"], "error": effect.get("error", ""),
                                 "url": (effect.get("remote") or {}).get("url", ""),
                                 "publisher": effect.get("publisher", {}),
                                 "reconcile_required": effect.get("reconcile_required", False)}
    if review.get("job_id"):
        job = await jobs.get(db, review["job_id"])
        if (job and all(job.get(key) == review.get(key) for key in ("head_sha", "base_sha"))
                and job.get("context_revision", 0) == review.get("context_revision", 0)):
            result = job.get("result") or {}
            review["notifications"] = job.get("notifications", result.get("notifications", []))
            for key in ("checks_read", "read_at", "checks_source", "declared_tests"):
                if key in result:
                    review[key] = result[key]
    actions = list(task.get("allowed_actions") or [])
    for action in ("external_review_retry", "external_evidence_image"):
        if action not in actions:
            actions.append(action)
    conv = await db.get_conversation(cid)
    project = await db.get_project(conv["project_slug"])
    extras = {"project_slug": conv["project_slug"]}
    if can_configure:
        actions.append("external_review_configure")
        policy = jobs.policy(project)
        extras["review_configuration"] = {key: policy[key] for key in (
            "enabled", "model", "publication_mode", "publisher_actor", "recipients") if key in policy}
    publications = [{**e, "historical": e["kind"] == "review" and e not in effects} for e in delivery["effects"]]
    return {**task, **extras, "external_review": review, "external_publications": publications, "allowed_actions": actions}
