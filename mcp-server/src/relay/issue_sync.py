"""Opt-in, observed issue/Projects transitions; never interpret model prose."""
from __future__ import annotations

import asyncio
import copy

from . import github, issue_sync_github as remote, issue_sync_store as store
from .user_accounts import AccountError, bind_actor


IssueSyncError = remote.IssueSyncError
MANUAL_PROJECT_CONFLICT = "El estado de Projects cambió fuera de Relay; requiere reconciliación manual"


class ConflictError(IssueSyncError):
    """A deliberate precondition conflict; never automatically retry it."""


def _identity(link):
    return {key: link.get(key) for key in ("repo", "repo_id", "number", "node_id", "url")}


def _issue_key(link):
    return f"{link['repo_id']}:{link['number']}"


def _project_observation(mapping, existing):
    return {"project_id": mapping["project_id"], "field_id": mapping["field_id"],
            "item_id": existing["item_id"], "option_id": existing["value"], "value": existing["value"],
            "title": existing.get("project_title"), "field_name": existing.get("field_name"),
            "option_name": existing.get("option_name")}


def _configured(project):
    defaults = project.get("defaults_json") or {}
    value = defaults.get("issue_sync_policy") if isinstance(defaults, dict) else None
    return value if isinstance(value, dict) else {}


def _enabled(project):
    return _configured(project).get("enabled") is True


def _transition(task, pr):
    if pr and pr.get("merged") is True:
        return "merged"
    if task.get("state") == "cancelled" or (pr and pr.get("state", "").upper() == "CLOSED"):
        return "cancelled"
    if not pr or pr.get("draft"):
        return "started"
    latest = {}
    responsible = ((task.get("responsible") or {}).get("github_id")
                   or (task.get("issue_link_actor") or {}).get("github_id"))
    for review in pr.get("reviews") or []:
        user = review.get("user") or {}
        if (user.get("type") != "User" or user.get("id") in {
                (pr.get("author") or {}).get("id"), responsible}
                or review.get("commit_id") != pr.get("head_sha")):
            continue
        if review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[user.get("id")] = review["state"]
    return "changes_requested" if "CHANGES_REQUESTED" in latest.values() else "review"


def _completion_declared(task):
    actor = task.get("resolution_actor") or {}
    return (task.get("resolution_complete") is True and isinstance(actor, dict)
            and type(actor.get("github_id")) is int and actor["github_id"] > 0
            and bool(actor.get("github_login")) and bool(actor.get("relay_actor")))


async def _related(db, link):
    rows = await db.run("SELECT id,project_slug,pr_url,issue_number,task_json FROM conversations "
                        "WHERE issue_number=?", (link["number"],))
    related = []
    for row in rows:
        task = await db.get_conversation_task(row["id"])
        other = task.get("issue_link") or {}
        if other.get("repo_id") == link["repo_id"] and other.get("number") == link["number"]:
            related.append((row, task, other))
        elif not other or not other.get("repo_id"):
            project = await db.get_project(row["project_slug"])
            path = (project or {}).get("repo_path")
            slug = await github.repo_slug(path) if path else None
            if not slug or slug.casefold() == link["repo"].casefold():
                # Historical numeric links are still dependencies. Require
                # canonical verification before they can authorize closure.
                # An unavailable identity cannot prove an unrelated repo.
                related.append((row, task, {}))
        if len(related) > 100:
            raise ConflictError("Más de 100 vínculos del issue; requiere revisión manual")
    return related


def _members(related):
    return sorted((row["id"], row.get("pr_url") or task.get("pr_url") or "", store.digest(_identity(link)))
                  for row, task, link in related)


async def _resolution(db, link, task):
    dependencies = []
    if not _completion_declared(task):
        return False, dependencies, "Falta declarar explícitamente resolución completa con identidad humana"
    related = await _related(db, link)
    if not related:
        return False, dependencies, "No hay vínculos verificables para resolver el issue"
    for row, other_task, other_link in related:
        url = row.get("pr_url") or other_task.get("pr_url")
        if not url or not other_link:
            dependencies.append({"conversation_id": row["id"], "merged": False})
            continue
        pr = await remote.read_pr(other_link, url)
        dependencies.append({"conversation_id": row["id"], "number": pr["number"],
                             "head_sha": pr["head_sha"], "merged": pr.get("merged") is True})
    if _members(related) != _members(await _related(db, link)):
        return False, dependencies, "Cambió la relación de tareas/PR durante la verificación"
    complete = all(item["merged"] for item in dependencies)
    return complete, dependencies, "" if complete else "Hay tareas o PR vinculados aún sin merge verificado"


async def snapshot(db, cid):
    conv = await db.get_conversation(cid)
    if not conv:
        raise ValueError("No existe la tarea")
    task = await db.get_conversation_task(cid)
    project = await db.get_project(conv["project_slug"])
    observed, error = await store.observation(db, cid)
    intent_fingerprint, _ = await store.intent(db, cid)
    error = task.get("issue_sync_error") or error
    events = await store.list_events(db, cid)
    enabled = bool(project and _enabled(project))
    active = [store.event(row) for row in await db.run(
        "SELECT * FROM issue_sync_events WHERE conversation_id=? AND state NOT IN ('applied','superseded') "
        "ORDER BY CASE WHEN state IN ('uncertain','processing') THEN 0 ELSE 1 END,id LIMIT 100", (cid,))]
    if any(event["state"] in {"uncertain", "processing"} for event in active):
        status = "uncertain"
    elif error or any(event["state"] == "blocked" for event in active):
        status = "error"
    elif not task.get("issue_link"):
        status = "unlinked"
    elif not enabled:
        status = "disabled"
    elif active or intent_fingerprint == "":
        status = "pending"
    else:
        status = "synced" if observed else "unobserved"
    # Only selected fields, never provider output, authorization or payloads.
    public_events = [{key: event[key] for key in (
        "id", "sequence", "transition", "actor", "reconciled_by", "state", "attempts", "read_attempts", "created_at", "updated_at", "error")}
        | {"target": event["payload"]["target"]} for event in events]
    return {"issue": task.get("issue_link"), "enabled": enabled, "policy_enabled": enabled, "status": status,
            "observed": observed, "events": public_events,
            "resolution_complete": _completion_declared(task),
            "error": error or next((event["error"] for event in active if event["error"]), "")}


async def recover(db):
    """At startup, lost in-flight calls require a read before another effect."""
    await store.recover(db)


async def retry(db, cid, who, authorize=None):
    """Resume a user's blocked/reconciliation reads; never reset effect caps."""
    await _authorized(db, cid, who, authorize)
    await store.ensure_schema(db)
    await store.retry(db, cid, who, reset_error=MANUAL_PROJECT_CONFLICT)
    return await snapshot(db, cid)


async def _authorized(db, cid, who, authorize):
    # The caller must provide a fresh project/actor authorization check. A
    # background timer with no actor context must never gain write authority.
    if authorize is None or not who:
        raise ConflictError("Sin autorización vigente: no se ejecutó sincronización")
    await authorize()
    conv = await db.get_conversation(cid)
    project = await db.get_project(conv["project_slug"]) if conv else None
    if (not project or not project.get("enabled", True)
            or (project.get("defaults_json") or {}).get("read_only")):
        raise ConflictError("El proyecto ya no permite escritura")
    task = await db.get_conversation_task(cid)
    if task.get("mode") == "read_only":
        raise ConflictError("Una consulta de solo lectura no sincroniza estados")
    link = task.get("issue_link") or {}
    configured = await github.repo_slug(project.get("repo_path") or "")
    if not configured or configured.casefold() != (link.get("repo") or "").casefold():
        raise ConflictError("El repositorio del proyecto cambió; revisar el vínculo")
    return project, task, link


async def _actions(db, link, task, policy, issue, pr):
    transition = _transition(task, pr)
    complete, dependencies, blocked = False, [], ""
    if transition == "merged":
        complete, dependencies, blocked = await _resolution(db, link, task)
    observation = {"at": store.now(), "issue": issue,
                   "pr": {key: pr.get(key) for key in ("number", "url", "state", "merged", "head_sha", "draft")} if pr else None,
                   "transition": transition, "resolution_complete": complete, "dependencies": dependencies,
                   "projects": []}
    if transition == "merged" and not complete:
        return observation, [], blocked
    if issue["state"].lower() == "closed" and transition != "merged":
        return observation, [], "Issue cerrado externamente: no se reabre ni se sobrescribe su tablero automáticamente"
    actions = []
    issue_state = policy["issue_states"].get(transition)
    if issue_state:
        actions.append({"target": "issue", "value": issue_state, "expected": issue["state"].lower(),
                        "link": _identity(link), "head_sha": (pr or {}).get("head_sha"), "pr_url": (pr or {}).get("url")})
    for mapping in policy["projects"]:
        value = mapping["options"].get(transition)
        if value is None:
            continue
        existing = await remote.read_project_value(link, mapping)
        observation["projects"].append(_project_observation(mapping, existing))
        actions.append({"target": f"project:{mapping['project_id']}:{mapping['field_id']}",
                        "value": value, "expected": existing["value"], "item_id": existing["item_id"],
                        "mapping": mapping, "link": _identity(link), "head_sha": (pr or {}).get("head_sha"),
                        "pr_url": (pr or {}).get("url")})
    return observation, actions, ""


async def _current(action):
    if action["target"] == "issue":
        return (await remote.read_issue(action["link"]))["state"].lower()
    existing = await remote.read_project_value(action["link"], action["mapping"])
    if existing["item_id"] != action["item_id"]:
        raise ConflictError("El item de Projects cambió; revisar antes de escribir")
    return existing["value"]


async def _process(db, cid, who, authorize):
    # A bounded batch; the next observed refresh resumes remaining targets.
    for _ in range(20):
        event = await store.claim(db, cid, who)
        if not event:
            return
        action, attempted = event["payload"], False
        try:
            if event["actor"] != who:
                await _authorized(db, cid, who, authorize)
            current = await _current(action)
            # Reconcile uncertain outcomes before checking whether an old
            # intent is still authorized. This branch performs no mutation.
            if current == action["value"]:
                await store.finish(db, event["id"], "applied")
                continue
            project, task, link = await _authorized(db, cid, who, authorize)
            policy = await remote.validate_policy(project, _configured(project))
            observed, _ = await store.observation(db, cid)
            if (not policy["enabled"] or store.digest(policy) != event["policy_digest"]
                    or _identity(link) != action["link"]
                    or observed.get("transition") != event["transition"]):
                await store.finish(db, event["id"], "superseded", "Cambió política, vínculo o transición; no se repitió el efecto")
                continue
            current_pr = await remote.read_pr(link, action["pr_url"]) if action.get("pr_url") else None
            if ((current_pr or {}).get("head_sha") != action.get("head_sha")
                    or _transition(task, current_pr) != event["transition"]):
                await store.finish(db, event["id"], "superseded", "Cambió el commit o estado remoto")
                continue
            gate_members = None
            if event["transition"] == "merged":
                complete, _, why = await _resolution(db, link, task)
                if not complete:
                    raise ConflictError(why)
                gate_members = _members(await _related(db, link))
            if current != action["expected"]:
                raise ConflictError("El estado remoto cambió fuera de Relay; requiere reconciliación manual")
            if action["target"] == "issue" and current == "closed":
                raise ConflictError("Un issue cerrado manualmente no se reabre automáticamente")
            # Refresh authorization after all remote reads and persist intent
            # before the side effect. Cancellation from here is uncertain.
            fresh_project, fresh_task, fresh_link = await _authorized(db, cid, who, authorize)
            if (store.digest(_configured(fresh_project)) != store.digest(_configured(project))
                    or _identity(fresh_link) != action["link"]
                    or _completion_declared(fresh_task) != _completion_declared(task)):
                raise ConflictError("Cambió la autorización o configuración durante la lectura")
            if gate_members is not None and gate_members != _members(await _related(db, link)):
                raise ConflictError("Cambió la relación de tareas/PR antes de la escritura")
            attempt = await store.attempt(db, event["id"])
            if attempt is None:
                await store.finish(db, event["id"], "blocked", "Límite de tres intentos; revisar resultado remoto antes de continuar")
                continue
            attempted = True
            if action["target"] == "issue":
                await remote.write_issue(link, action["value"], expected_state=current)
            else:
                await remote.write_project(link, action["mapping"], action["item_id"], action["value"], expected_value=current)
            if await _current(action) != action["value"]:
                raise IssueSyncError("GitHub no confirmó todavía el efecto solicitado")
            await store.finish(db, event["id"], "applied")
        except asyncio.CancelledError:
            await asyncio.shield(store.finish(db, event["id"], "uncertain", "Sin resultado confirmado: reconciliar antes de repetir"))
            raise
        except Exception as exc:
            if isinstance(exc, AccountError):
                uncertain = attempted or event["previous_state"] == "uncertain"
                await store.finish(db, event["id"], "uncertain" if uncertain else "blocked", str(exc),
                                   delay=60 if uncertain else 0)
                raise
            if not attempted and isinstance(exc, IssueSyncError) and not isinstance(exc, ConflictError):
                await store.read_failure(db, event["id"], str(exc), uncertain=event["previous_state"] == "uncertain")
                return
            if attempted or event["previous_state"] == "uncertain":
                # No blind replay after a timeout or cancellation. The next
                # refresh must read the target, compare its precondition, and
                # obtain authorization again; at most three writes total.
                await store.finish(db, event["id"], "uncertain", str(exc), delay=60)
                return
            await store.finish(db, event["id"], "blocked", str(exc))


async def _reconcile_disabled(db, cid, who, authorize):
    """Revoking a policy forbids effects, not observation of lost responses."""
    for _ in range(20):
        event = await store.claim(db, cid, who, reconcile_only=True)
        if not event:
            return
        try:
            if event["actor"] != who:
                await _authorized(db, cid, who, authorize)
            if await _current(event["payload"]) == event["payload"]["value"]:
                await store.finish(db, event["id"], "applied")
                continue
            await store.finish(db, event["id"], "uncertain",
                               "Política desactivada: el efecto no está confirmado; no se repite", delay=60)
            return
        except asyncio.CancelledError:
            await asyncio.shield(store.finish(db, event["id"], "uncertain", "Reconciliación interrumpida"))
            raise
        except Exception as exc:
            await store.read_failure(db, event["id"], str(exc), uncertain=True)
            if isinstance(exc, AccountError):
                raise
            return


async def refresh(db, project, cid, who, authorize=None):
    """Observe remote truth and, only with an enabled policy, reconcile effects.

    Does not create issues, PRs, Projects items, credentials, or webhooks.
    Observations never derive an instruction from a bot's message.
    """
    conv = await db.get_conversation(cid)
    if not conv or conv["project_slug"] != project["slug"]:
        raise ValueError("Tarea y proyecto no coinciden")
    task = await db.get_conversation_task(cid)
    link = copy.deepcopy(task.get("issue_link") or {})
    if not link:
        return await snapshot(db, cid)
    try:
        with bind_actor(db, who):
            issue = await remote.read_issue(link)
            url = conv.get("pr_url") or task.get("pr_url")
            pr = await remote.read_pr(link, url) if url else None
            raw_policy = _configured(project)
            policy = await remote.validate_policy(project, raw_policy)
            if policy["enabled"] and task.get("mode") != "read_only":
                observed, actions, error = await _actions(db, link, task, policy, issue, pr)
                previous, previous_error = await store.observation(db, cid)
                previous_intent, _ = await store.intent(db, cid)
                old_values = {(item["project_id"], item["field_id"]): item.get("option_id", item.get("value"))
                              for item in previous.get("projects", [])} if previous_intent != "" else {}
                for action in actions:
                    mapping = action.get("mapping")
                    key = (mapping["project_id"], mapping["field_id"]) if mapping else None
                    if (key in old_values and old_values[key] != action["expected"]
                            and action["expected"] != action["value"]):
                        error = MANUAL_PROJECT_CONFLICT
                        actions = []
                        break
                # Recording a new observation must not silently adopt a
                # manually changed value as the baseline on the next poll.
                # A new transition or explicitly authorized retry can replan.
                if (previous_intent != "" and previous_error == MANUAL_PROJECT_CONFLICT
                        and previous.get("transition") == observed["transition"]):
                    error, actions = MANUAL_PROJECT_CONFLICT, []
            else:
                projects = []
                for mapping in policy["projects"]:
                    existing = await remote.read_project_value(link, mapping)
                    projects.append(_project_observation(mapping, existing))
                observed = {"at": store.now(), "issue": issue, "pr": pr, "projects": projects,
                            "transition": _transition(task, pr), "resolution_complete": False}
                actions, error = [], ""
            stable = {key: value for key, value in observed.items() if key != "at"}
            # Include policy and human declaration, not our issue updated_at:
            # writing a state must not enqueue an echo of that same effect.
            stable["issue"] = _identity(link)
            stable.pop("projects", None)
            fingerprint = store.digest([stable, policy, _completion_declared(task)])
            await store.record(db, cid, _issue_key(link), fingerprint, observed, actions, who, store.digest(policy), error)
            if policy["enabled"] and task.get("mode") != "read_only":
                await _process(db, cid, who, authorize)
                # Report confirmed remote truth after effects. Preserve the
                # intent fingerprint so our own writes never enqueue echoes.
                observed["issue"] = await remote.read_issue(link)
                projects = []
                for mapping in policy["projects"]:
                    existing = await remote.read_project_value(link, mapping)
                    projects.append(_project_observation(mapping, existing))
                observed["projects"], observed["at"] = projects, store.now()
                await db.run("UPDATE issue_sync_streams SET observation=?,updated_at=? WHERE conversation_id=?",
                             (store.encoded(observed), store.now(), cid))
            else:
                await _reconcile_disabled(db, cid, who, authorize)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await store.ensure_schema(db)
        await db.run("UPDATE issue_sync_streams SET error=?,updated_at=? WHERE conversation_id=?", (str(exc), store.now(), cid))
        raise
    await db.update_conversation_task(cid, issue_sync_error=None)
    return await snapshot(db, cid)
