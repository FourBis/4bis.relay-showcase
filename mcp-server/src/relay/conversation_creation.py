"""Immutable creation identity and effective-mode assertions; no permission grant."""
from __future__ import annotations

import hashlib
import json
import re


class CreationRejected(ValueError):
    """This request ID is durably barred from creating a conversation."""

    def __init__(self, proof):
        super().__init__("La solicitud original fue rechazada antes de crear una conversación")
        self.proof = proof


ISSUE_KEYS = ("repo", "repo_id", "number", "node_id", "url")


def contract(body):
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"version": 2, "request_id": body["request_id"],
            "payload_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "required_issue": None}


def issue_requirement(issue):
    return {key: issue[key] for key in ISSUE_KEYS} if issue is not None else None


def issue_bound(conversation, task, origin):
    """Only the link committed by issue_links.bind proves preparation.

    A resolved API response or the requested issue number is not a binding.
    Version 1 has no provenance for an omitted issue and cannot certify ready.
    Completion can evolve after creation; it is not an eternal mode fence.
    """
    if origin.get("version") != 2 or "required_issue" not in origin:
        return False
    required = origin["required_issue"]
    if required is None:
        return True
    link = task.get("issue_link")
    return (isinstance(required, dict) and set(required) == set(ISSUE_KEYS)
            and isinstance(link, dict)
            and type(required.get("repo_id")) is int and required["repo_id"] > 0
            and type(required.get("number")) is int and required["number"] > 0
            and all(required.get(key) is not None and link.get(key) == required[key] for key in ISSUE_KEYS)
            and conversation.get("issue_number") == required["number"]
            and task.get("issue_number") == required["number"])


def receipt(conversation):
    if not isinstance(conversation, dict):
        return None
    try:
        task = json.loads(conversation.get("task_json") or "{}")
    except (TypeError, ValueError):
        return None
    origin = task.get("creation_request") if isinstance(task, dict) else None
    if (not isinstance(origin, dict) or origin.get("version") not in {1, 2}
            or not isinstance(origin.get("request_id"), str)
            or not isinstance(origin.get("payload_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", origin["payload_sha256"])
            or type(task.get("read_only")) is not bool
            or type(task.get("publish_allowed")) is not bool):
        return None
    mode, media = task.get("mode") or "", task.get("media_only") is True
    coherent = not media and mode == ("read_only" if task["read_only"] else "write")
    bound = issue_bound(conversation, task, origin)
    ready = (conversation.get("status") == "open" and coherent and bound
             and task.get("state") not in {"provisioning", "paused", "cancelled", "blocked", "finished", "cleaned"}
             and not task.get("creation_failed") and not task.get("workspace_error"))
    return {**origin, "project_slug": conversation["project_slug"],
            "read_only": task["read_only"], "publish_allowed": task["publish_allowed"],
            "media_only": media, "mode": mode, "issue_bound": bool(bound), "ready": bool(ready)}


def require_replay(conversation, requested_contract):
    current = receipt(conversation)
    if current is None:
        raise ValueError("La creación anterior no tiene contrato verificable; revisa la conversación existente")
    if any(current[key] != requested_contract[key] for key in ("version", "request_id", "payload_sha256")):
        raise ValueError("El identificador de creación ya pertenece a otro pedido; no se cambió la conversación")
    return current


def require_enqueue_tx(conn, conversation_id, expected=None):
    """Assert current mode in the enqueue transaction, after existing-event dedupe.

    The caller also holds task_service.control_lock, serializing this boundary
    with the complete enable_write check/promote operation. Stored assertions
    cannot create permission, change a task, or authorize an extra execution.
    """
    row = conn.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
    current = receipt(dict(row)) if row else None
    # The required-issue invariant also applies to clients that omit a receipt.
    # Do not impose creation-time state on later turns or migrate legacy rows.
    if row:
        task = json.loads(row["task_json"] or "{}")
        origin = task.get("creation_request") if isinstance(task, dict) else None
        if isinstance(origin, dict) and origin.get("version") == 2 and not issue_bound(dict(row), task, origin):
            raise ValueError("La issue requerida aún no está vinculada; no se encoló el mensaje")
    if expected is None:
        return
    keys = ("version", "request_id", "payload_sha256", "project_slug",
            "read_only", "publish_allowed", "media_only", "mode", "ready", "required_issue", "issue_bound")
    if (not isinstance(expected, dict) or set(expected) != set(keys) or current is None or current["version"] != 2
            or any(type(expected[key]) is not type(current[key]) or expected[key] != current[key] for key in keys)
            or not current["ready"]):
        raise ValueError("El contrato o modo efectivo de creación cambió; no se encoló el primer mensaje")
