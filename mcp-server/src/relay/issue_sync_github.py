"""Actor-scoped GitHub I/O for explicitly configured issue synchronization.

Projects operations use discovered node/field/option IDs. They never create
projects or items, request scopes, or fall back to a machine account.
See https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from . import github
from .task_pr import TaskPRError
from .user_accounts import AccountError

TRANSITIONS = frozenset({"started", "review", "changes_requested", "merged", "cancelled"})
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", re.ASCII)
_PR = re.compile(r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]{0,9})/?", re.ASCII)
_SHA = re.compile(r"[0-9a-f]{40}", re.ASCII)
_UNSET = object()
_FIELDS = """fields(first:100) { pageInfo { hasNextPage } nodes {
  __typename ... on ProjectV2FieldCommon { id name dataType }
  ... on ProjectV2SingleSelectField { options { id name } }
} }"""
_PROJECT = "id title number url closed " + _FIELDS
_ITEMS = """items(first:100) { pageInfo { hasNextPage } nodes {
  id isArchived content { __typename ... on Issue {
    id number repository { databaseId nameWithOwner }
  } }
  fieldValues(first:100) { pageInfo { hasNextPage } nodes {
    __typename ... on ProjectV2ItemFieldSingleSelectValue {
      optionId field { ... on ProjectV2FieldCommon { id } }
    }
  } }
} }"""


class IssueSyncError(TaskPRError):
    """GitHub did not confirm a complete, authorized remote observation."""


def _identifier(value, name):
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 33 for c in value):
        raise ValueError(f"{name} debe ser un ID explícito válido")
    return value


def _fold(value):
    return value.casefold() if isinstance(value, str) else ""


def _valid_link(link):
    if (not isinstance(link, dict) or type(link.get("number")) is not int
            or not 1 <= link["number"] <= 2147483647 or type(link.get("repo_id")) is not int
            or link["repo_id"] <= 0 or not isinstance(link.get("node_id"), str) or not link["node_id"]):
        raise ValueError("Vínculo canónico de issue inválido")


async def _json(*args):
    try:
        rc, output = await github._gh("api", "--hostname", "github.com", *args)
    except AccountError:
        raise
    except (OSError, ValueError) as exc:
        raise IssueSyncError("GitHub no confirmó la operación; revisa la conexión personal") from exc
    if rc:
        # Provider output is deliberately not returned or persisted: it may contain credentials.
        raise IssueSyncError("GitHub no confirmó la operación; revisa acceso y conexión personal")
    try:
        result = json.loads(output)
    except (TypeError, ValueError) as exc:
        raise IssueSyncError("GitHub devolvió una respuesta inválida") from exc
    if not isinstance(result, (dict, list)):
        raise IssueSyncError("GitHub devolvió una respuesta incompleta")
    return result


async def _graph(query, **variables):
    args = ["graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        args.extend(["-f", f"{name}={value}"])
    result = await _json(*args)
    if not isinstance(result, dict) or result.get("errors") or not isinstance(result.get("data"), dict):
        raise IssueSyncError("GitHub Projects no confirmó todos los datos o permisos")
    return result["data"]


def _nodes(connection, label):
    if (not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list)
            or not isinstance(connection.get("pageInfo"), dict)
            or type(connection["pageInfo"].get("hasNextPage")) is not bool):
        raise IssueSyncError(f"GitHub no confirmó la lista de {label}")
    if connection["pageInfo"]["hasNextPage"] or len(connection["nodes"]) > 100:
        raise IssueSyncError(f"Más de 100 {label}; requiere revisión manual")
    return connection["nodes"]


async def _repo(slug, repo_id=None):
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.split("/")[1] in {".", ".."}:
        raise ValueError("Repositorio GitHub inválido")
    result = await _json(f"repos/{slug}")
    if (not isinstance(result, dict) or type(result.get("id")) is not int or result["id"] <= 0
            or not isinstance(result.get("full_name"), str)
            or result["full_name"].casefold() != slug.casefold()
            or (repo_id is not None and result["id"] != repo_id)):
        raise IssueSyncError("El repositorio GitHub cambió o no es el autorizado")
    return result


def _issue(result, repo, number, node_id=None):
    slug = repo["full_name"]
    if (not isinstance(result, dict) or type(result.get("number")) is not int
            or result["number"] != number or result.get("pull_request")
            or type(result.get("id")) is not int or result["id"] <= 0
            or not isinstance(result.get("node_id"), str) or not result["node_id"]
            or (node_id is not None and result["node_id"] != node_id)
            or _fold(result.get("repository_url")) != f"https://api.github.com/repos/{slug}".casefold()
            or _fold(result.get("html_url")) != f"https://github.com/{slug}/issues/{number}".casefold()
            or result.get("state") not in {"open", "closed"}
            or not isinstance(result.get("updated_at"), str)):
        raise IssueSyncError("GitHub no confirmó identidad, repositorio y estado del issue")
    return {"repo": slug, "repo_id": repo["id"], "number": number,
            "id": result["id"], "node_id": result["node_id"], "url": result["html_url"],
            "title": result.get("title", ""), "state": result["state"],
            "updated_at": result["updated_at"], "observed_at": datetime.now(timezone.utc).isoformat()}


async def read_issue(link):
    _valid_link(link)
    number = link.get("number")
    repo = await _repo(link.get("repo"), link["repo_id"])
    return _issue(await _json(f"repos/{repo['full_name']}/issues/{number}"), repo, number, link.get("node_id"))


def _user(value):
    if (not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] <= 0
            or not isinstance(value.get("login"), str) or not value["login"]
            or value.get("type") not in {"User", "Bot"}):
        raise IssueSyncError("GitHub no confirmó la identidad de una revisión")
    return {key: value[key] for key in ("id", "login", "type")}


async def read_pr(link, pr_url):
    _valid_link(link)
    match = _PR.fullmatch(pr_url) if isinstance(pr_url, str) and len(pr_url) <= 500 else None
    if not match or match[1].casefold() != link.get("repo", "").casefold():
        raise ValueError("La PR debe pertenecer al repositorio del issue")
    repo = await _repo(link["repo"], link.get("repo_id"))
    number, slug = int(match[2]), repo["full_name"]
    result = await _json(f"repos/{slug}/pulls/{number}")
    try:
        valid = (type(result["number"]) is int and result["number"] == number
                 and result["base"]["repo"]["id"] == repo["id"]
                 and result["base"]["repo"]["full_name"].casefold() == slug.casefold()
                 and _SHA.fullmatch(result["head"]["sha"])
                 and result["state"] in {"open", "closed"} and type(result["draft"]) is bool
                 and type(result["merged"]) is bool
                 and ((isinstance(result.get("merged_at"), str) and bool(result["merged_at"]))
                      if result["merged"] else result.get("merged_at") is None)
                 and (not result["merged"] or result["state"] == "closed")
                 and isinstance(result["updated_at"], str)
                 and result["html_url"].casefold() == f"https://github.com/{slug}/pull/{number}".casefold())
    except (KeyError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise IssueSyncError("GitHub no confirmó repositorio, SHA y estado de la PR")
    rows = await _json(f"repos/{slug}/pulls/{number}/reviews?per_page=100")
    if not isinstance(rows, list) or len(rows) >= 100:
        raise IssueSyncError("GitHub no confirmó todas las revisiones de la PR (límite 99)")
    reviews = []
    for row in rows:
        if not isinstance(row, dict) or row.get("state") not in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"}:
            raise IssueSyncError("GitHub devolvió una revisión inválida")
        reviews.append({"id": row.get("id"), "state": row["state"], "user": _user(row.get("user")),
                        "commit_id": row.get("commit_id"), "submitted_at": row.get("submitted_at")})
    # Do not derive transitions from evidence collected across two different commits/states.
    latest = await _json(f"repos/{slug}/pulls/{number}")
    if (not isinstance(latest, dict) or not isinstance(latest.get("head"), dict)
            or latest["head"].get("sha") != result["head"]["sha"]
            or latest.get("state") != result["state"] or latest.get("draft") != result["draft"]
            or latest.get("merged_at") != result.get("merged_at")
            or latest.get("updated_at") != result["updated_at"]):
        raise IssueSyncError("La PR cambió durante la lectura; vuelve a actualizar")
    return {"number": number, "url": result["html_url"], "repo": slug, "repo_id": repo["id"],
            "state": "MERGED" if result["merged"] else result["state"].upper(), "merged": result["merged"],
            "head_sha": result["head"]["sha"], "updated_at": result["updated_at"], "draft": result["draft"],
            "author": _user(result.get("user")), "reviews": reviews,
            "observed_at": datetime.now(timezone.utc).isoformat()}


def _project_data(node):
    if (not isinstance(node, dict) or node.get("__typename") != "ProjectV2"
            or not isinstance(node.get("id"), str) or not node["id"]
            or type(node.get("closed")) is not bool):
        raise IssueSyncError("GitHub no confirmó un Project v2 accesible")
    fields = []
    for field in _nodes(node.get("fields"), "campos"):
        if (not isinstance(field, dict) or not isinstance(field.get("id"), str) or not field["id"]
                or not isinstance(field.get("name"), str) or not field["name"]):
            raise IssueSyncError("GitHub devolvió un campo inaccesible")
        options = field.get("options", [])
        if (not isinstance(options, list) or len(options) > 100
                or any(not isinstance(o, dict) or not isinstance(o.get("id"), str) or not o["id"]
                       or not isinstance(o.get("name"), str) or not o["name"] for o in options)):
            raise IssueSyncError("GitHub no confirmó todas las opciones del campo")
        if len({o["id"] for o in options}) != len(options):
            raise IssueSyncError("Opciones duplicadas en GitHub Projects")
        fields.append({"id": field["id"], "name": field["name"], "data_type": field.get("dataType"), "options": options})
    if len({f["id"] for f in fields}) != len(fields):
        raise IssueSyncError("Campos duplicados en GitHub Projects")
    return {"id": node["id"], "title": node.get("title", ""), "number": node.get("number"),
            "url": node.get("url", ""), "closed": node["closed"], "fields": fields}


async def _project(project_id, *, items=False):
    _identifier(project_id, "project_id")
    query = "query($id:ID!) { node(id:$id) { __typename ... on ProjectV2 { " + _PROJECT + (" " + _ITEMS if items else "") + " } } }"
    node = (await _graph(query, id=project_id)).get("node")
    parsed = _project_data(node)
    if parsed["id"] != project_id:
        raise IssueSyncError("GitHub devolvió otro Project")
    return parsed, node


async def discover(project, project_id=None):
    slug = await github.repo_slug(project.get("repo_path") or "")
    repo = await _repo(slug)
    if project_id is not None:
        parsed, _ = await _project(project_id)
        return {"projects": [parsed]}
    owner = repo.get("owner")
    if not isinstance(owner, dict) or owner.get("type") not in {"User", "Organization"} or not owner.get("node_id"):
        raise IssueSyncError("GitHub no confirmó el propietario del repositorio")
    selection = "projectsV2(first:100) { pageInfo { hasNextPage } nodes { __typename " + _PROJECT + " } }"
    query = "query($id:ID!) { node(id:$id) { __typename ... on User { " + selection + " } ... on Organization { " + selection + " } } }"
    node = (await _graph(query, id=owner["node_id"])).get("node")
    if not isinstance(node, dict) or node.get("__typename") != owner["type"]:
        raise IssueSyncError("GitHub no confirmó los Projects del propietario")
    return {"projects": [_project_data(item) for item in _nodes(node.get("projectsV2"), "Projects")]}


def normalize_policy(policy):
    if policy is None:
        return {"enabled": False, "issue_states": {}, "projects": []}
    if not isinstance(policy, dict) or set(policy) - {"enabled", "issue_states", "projects"}:
        raise ValueError("Política de sincronización inválida")
    enabled = policy.get("enabled", False)
    states, projects = policy.get("issue_states", {}), policy.get("projects", [])
    if type(enabled) is not bool or not isinstance(states, dict) or set(states) - TRANSITIONS:
        raise ValueError("enabled o transiciones de issue inválidos")
    if any(not isinstance(value, str) or value not in {"open", "closed"}
           or (value == "closed" and key != "merged") for key, value in states.items()):
        raise ValueError("Solo merge verificado puede cerrar un issue")
    if not isinstance(projects, list) or len(projects) > 10:
        raise ValueError("Configura como máximo 10 campos de Projects")
    normalized, seen = [], set()
    for entry in projects:
        if not isinstance(entry, dict) or set(entry) != {"project_id", "field_id", "options"}:
            raise ValueError("Mapeo Projects inválido")
        project_id = _identifier(entry["project_id"], "project_id")
        field_id = _identifier(entry["field_id"], "field_id")
        options = entry["options"]
        if not isinstance(options, dict) or not options or set(options) - TRANSITIONS:
            raise ValueError("Mapea transiciones explícitas a IDs de opciones")
        if (project_id, field_id) in seen:
            raise ValueError("Campo Projects duplicado")
        seen.add((project_id, field_id))
        normalized.append({"project_id": project_id, "field_id": field_id,
                           "options": {key: _identifier(value, "option_id") for key, value in options.items()}})
    if enabled and not states and not normalized:
        raise ValueError("Una política activa requiere al menos una transición configurada")
    return {"enabled": enabled, "issue_states": dict(states), "projects": normalized}


def _mapping_field(project, mapping):
    if project["closed"]:
        raise IssueSyncError("El Project está cerrado; requiere revisión manual")
    fields = [field for field in project["fields"] if field["id"] == mapping["field_id"]]
    if len(fields) != 1 or fields[0]["data_type"] != "SINGLE_SELECT":
        raise ValueError("El field_id no identifica un campo single-select del Project")
    known = {item["id"] for item in fields[0]["options"]}
    if not set(mapping["options"].values()) <= known:
        raise ValueError("Una option_id no pertenece al campo configurado")
    return known


async def validate_policy(project, policy):
    normalized = normalize_policy(policy)
    loaded = {}
    for mapping in normalized["projects"]:
        project_id = mapping["project_id"]
        if project_id not in loaded:
            loaded[project_id] = (await discover(project, project_id))["projects"][0]
        _mapping_field(loaded[project_id], mapping)
    return normalized


async def read_project_value(link, mapping):
    await read_issue(link)
    parsed, node = await _project(mapping["project_id"], items=True)
    known = _mapping_field(parsed, mapping)
    matches = []
    for item in _nodes(node.get("items"), "items"):
        if not isinstance(item, dict):
            raise IssueSyncError("GitHub devolvió un item inválido")
        content = item.get("content") or {}
        if not isinstance(content, dict):
            raise IssueSyncError("GitHub devolvió contenido de item inválido")
        if content.get("__typename") == "Issue" and content.get("id") == link["node_id"]:
            repo = content.get("repository") or {}
            if (not isinstance(repo, dict) or content.get("number") != link["number"]
                    or repo.get("databaseId") != link["repo_id"]
                    or _fold(repo.get("nameWithOwner")) != link["repo"].casefold()
                    or not isinstance(item.get("id"), str) or not item["id"] or item.get("isArchived") is not False):
                raise IssueSyncError("El item Projects no confirma el issue o está archivado")
            matches.append(item)
    if len(matches) != 1:
        raise IssueSyncError("El issue debe existir una sola vez como item activo del Project; no se agregó automáticamente")
    value = None
    selected = []
    for field in _nodes(matches[0].get("fieldValues"), "valores de campos"):
        if not isinstance(field, dict):
            raise IssueSyncError("Valor Projects inválido")
        field_identity = field.get("field") or {}
        if not isinstance(field_identity, dict):
            raise IssueSyncError("GitHub devolvió una identidad de campo inválida")
        if field.get("__typename") == "ProjectV2ItemFieldSingleSelectValue" and field_identity.get("id") == mapping["field_id"]:
            selected.append(field.get("optionId"))
    if len(selected) > 1 or (selected and selected[0] not in known):
        raise IssueSyncError("GitHub no confirmó el valor actual de Projects")
    if selected:
        value = selected[0]
    configured_field = next(field for field in parsed["fields"] if field["id"] == mapping["field_id"])
    option_name = next((option["name"] for option in configured_field["options"] if option["id"] == value), "Sin opción")
    return {"item_id": matches[0]["id"], "value": value,
            "project_title": parsed["title"], "field_name": configured_field["name"], "option_name": option_name}


async def write_issue(link, target, *, expected_state=_UNSET):
    if target not in {"open", "closed"}:
        raise ValueError("Estado de issue inválido")
    current = await read_issue(link)
    if current["state"] == target:
        return current
    if current["state"] == "closed":
        raise IssueSyncError("Un issue cerrado manualmente no se reabre automáticamente")
    if expected_state is not _UNSET and current["state"] != expected_state:
        raise IssueSyncError("El estado del issue cambió antes de la escritura")
    args = [f"repos/{link['repo']}/issues/{link['number']}", "--method", "PATCH", "-f", f"state={target}"]
    if target == "closed":
        args.extend(["-f", "state_reason=completed"])
    repo = {"full_name": current["repo"], "id": current["repo_id"]}
    result = _issue(await _json(*args), repo, link["number"], link["node_id"])
    if result["state"] != target:
        raise IssueSyncError("GitHub no confirmó el estado escrito; reconciliar antes de reintentar")
    return result


async def write_project(link, mapping, item_id, target_option, *, expected_value=_UNSET):
    if target_option not in mapping["options"].values():
        raise ValueError("Opción Projects no autorizada en la política")
    current = await read_project_value(link, mapping)
    if current["item_id"] != item_id:
        raise IssueSyncError("El item Projects cambió antes de la escritura")
    if current["value"] == target_option:
        return current
    if expected_value is not _UNSET and current["value"] != expected_value:
        raise IssueSyncError("El campo Projects cambió manualmente; requiere reconciliación")
    query = """mutation($project:ID!,$item:ID!,$field:ID!,$option:String!) {
      updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,fieldId:$field,
        value:{singleSelectOptionId:$option}}) { projectV2Item { id } }
    }"""
    result = await _graph(query, project=mapping["project_id"], item=item_id, field=mapping["field_id"], option=target_option)
    value = result.get("updateProjectV2ItemFieldValue")
    if (not isinstance(value, dict) or not isinstance(value.get("projectV2Item"), dict)
            or value["projectV2Item"].get("id") != item_id):
        raise IssueSyncError("GitHub no confirmó el movimiento Projects; reconciliar antes de reintentar")
    return {"item_id": item_id, "value": target_option}
