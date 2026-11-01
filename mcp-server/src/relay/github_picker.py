"""Read-only, per-actor selectors scoped to the project's verified GitHub repo."""
from __future__ import annotations

import json
import re
from urllib.parse import urlencode

from . import external_pr, github, issue_links
from .user_accounts import AccountError

PAGE_SIZE = 20


class PickerError(RuntimeError):
    def __init__(self, message, code="github_unavailable", status=502, retry_after=None):
        super().__init__(message)
        self.code, self.status, self.retry_after = code, status, retry_after


def parameters(query="", page="1"):
    if not isinstance(query, str) or len(query) > 120 or any(ord(c) < 32 for c in query):
        raise ValueError("La búsqueda debe tener hasta 120 caracteres, sin controles")
    if not isinstance(page, str) or not re.fullmatch(r"[1-9][0-9]?", page) or int(page) > 50:
        raise ValueError("page debe estar entre 1 y 50")
    return query.strip(), int(page)


async def _api(endpoint):
    rc, output = await github._gh("api", "--hostname", "github.com", "--include", endpoint)
    # Include status headers to distinguish rate limits without returning provider text.
    header, separator, body = output.replace("\r\n", "\n").partition("\n\n")
    lines = header.splitlines()
    match = re.fullmatch(r"HTTP/\S+ (\d{3})(?: .*)?", lines[0]) if lines else None
    if not separator or not match:
        raise PickerError("GitHub no confirmó la lectura. Puedes ingresar la URL manualmente.")
    status = int(match[1])
    headers = {key.strip().lower(): value.strip() for line in lines[1:]
               for key, separator, value in [line.partition(":")] if separator}
    if status == 429 or (status == 403 and (headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers)):
        retry = headers.get("retry-after", "")
        retry = min(int(retry), 3600) if len(retry) <= 10 and retry.isascii() and retry.isdigit() else None
        raise PickerError("GitHub limitó las consultas. Reintenta más tarde o ingresa la URL manualmente.",
                          "github_rate_limited", 429, retry)
    if status == 401:
        raise AccountError("Reconecta tu cuenta personal de GitHub para consultar la lista.")
    if status == 404:
        raise PickerError("Tu cuenta no pudo consultar el repositorio o elemento de GitHub.",
                          "github_not_accessible", 404)
    if rc or status != 200:
        raise PickerError("GitHub no confirmó la lectura. Puedes ingresar la URL manualmente.")
    try:
        return json.loads(body)
    except (TypeError, ValueError) as exc:
        raise PickerError("GitHub devolvió una lista inválida. Puedes ingresar la URL manualmente.") from exc


async def _repository(project):
    configured = await github.repo_slug(project.get("repo_path") or "", refresh=True)
    if (not isinstance(configured, str) or not issue_links._REPO.fullmatch(configured)
            or configured.rsplit("/", 1)[-1] in {".", ".."}):
        raise ValueError("El proyecto no tiene un repositorio GitHub autorizado")
    repo = await _api(f"repos/{configured}")
    if (not isinstance(repo, dict) or type(repo.get("id")) is not int or repo["id"] <= 0
            or not isinstance(repo.get("full_name"), str) or not issue_links._REPO.fullmatch(repo["full_name"])
            or repo["full_name"].rsplit("/", 1)[-1] in {".", ".."}):
        raise PickerError("GitHub no confirmó la identidad del repositorio del proyecto.")
    return repo, configured


def _pull(repo, row):
    canonical, number = repo["full_name"], row.get("number")
    url = f"https://github.com/{canonical}/pull/{number}"
    same_repo = (external_pr.same_repository(row.get("base"), repo["id"], canonical)
                 and external_pr.same_repository(row.get("head"), repo["id"], canonical))
    if (type(number) is not int or not 1 <= number <= 2147483647 or not same_repo
            or "repository_url" in row and str(row["repository_url"]).casefold() != f"https://api.github.com/repos/{canonical}".casefold()
            or not isinstance(row.get("html_url"), str) or row["html_url"].casefold() != url.casefold()
            or not isinstance(row.get("title"), str) or row.get("state") != "open"
            or not isinstance(row.get("updated_at"), str) or not row["updated_at"]
            or (row.get("draft") is not None and type(row["draft"]) is not bool)):
        raise PickerError("GitHub no confirmó una PR abierta del repositorio autorizado.")
    return {"repo": canonical, "repo_id": repo["id"], "number": number, "url": url,
            "title": row["title"], "state": "OPEN", "draft": row.get("draft"), "updated_at": row["updated_at"]}


async def options(project, kind, query="", page="1"):
    """No cache: each read uses the actor bound by the existing identity middleware."""
    query, page = parameters(query, page)
    if kind not in {"issues", "pulls"}:
        raise ValueError("Selector desconocido")
    repo, configured = await _repository(project)
    canonical, total, limited = repo["full_name"], 0, False
    numeric = re.fullmatch(r"#?([0-9]+)", query)
    if numeric:
        if len(numeric[1]) > 10 or not 1 <= int(numeric[1]) <= 2147483647:
            raise ValueError("Número fuera de rango")
        rows = []
        if page == 1:
            try:
                row = await _api(f"repos/{canonical}/{kind}/{int(numeric[1])}")
            except PickerError as exc:
                if exc.status != 404:
                    raise
            else:
                if (not isinstance(row, dict) or type(row.get("number")) is not int
                        or row["number"] != int(numeric[1])):
                    raise PickerError("GitHub devolvió un elemento inválido.")
                if row.get("state") == "open" and (kind == "pulls" or "pull_request" not in row):
                    rows = [row]
        total = len(rows)
    else:
        # Quote only literal title words; users cannot supply repository qualifiers.
        terms = [f'"{word}"' for word in re.findall(r"[^\W_]+", query, flags=re.UNICODE)]
        if query and not terms:
            raise ValueError("Busca por número o palabras del título")
        search = " ".join([f"repo:{canonical}", "is:pr" if kind == "pulls" else "is:issue", "is:open", "in:title", *terms])
        data = await _api("search/issues?" + urlencode({"q": search, "sort": "updated", "order": "desc", "per_page": PAGE_SIZE, "page": page}))
        if (not isinstance(data, dict) or data.get("incomplete_results") is not False
                or type(data.get("total_count")) is not int or data["total_count"] < 0
                or not isinstance(data.get("items"), list) or len(data["items"]) > PAGE_SIZE):
            raise PickerError("GitHub no confirmó una lista completa. Reintenta o ingresa la URL manualmente.", "github_incomplete")
        rows, total = data["items"], data["total_count"]
        limited = total > 1000
        if len(rows) > max(0, total - (page - 1) * PAGE_SIZE) or (not rows and total > (page - 1) * PAGE_SIZE):
            raise PickerError("GitHub devolvió una paginación incompleta. Actualiza la lista.", "github_incomplete")
        if any(not isinstance(row, dict) or ("pull_request" in row) != (kind == "pulls") or row.get("state") != "open" for row in rows):
            raise PickerError("GitHub devolvió elementos inesperados para esta lista.")
    try:
        items = []
        for row in rows:
            if kind != "pulls":
                items.append(issue_links._canonical(repo, row, row.get("number")))
                continue
            # Search results omit refs. Verify each bounded candidate through the
            # documented read-only PR endpoint before offering a same-repo choice.
            if "base" not in row or "head" not in row:
                number = row.get("number")
                if (type(number) is not int or not 1 <= number <= 2147483647
                        or row.get("repository_url", "").casefold() != f"https://api.github.com/repos/{canonical}".casefold()):
                    raise PickerError("GitHub no confirmó una PR del repositorio autorizado.")
                row = await _api(f"repos/{canonical}/pulls/{number}")
                if not isinstance(row, dict) or row.get("number") != number:
                    raise PickerError("GitHub no confirmó la identidad de la PR.")
            if not external_pr.same_repository(row.get("base"), repo["id"], canonical):
                raise PickerError("GitHub no confirmó la base autorizada de la PR.")
            if not external_pr.same_repository(row.get("head"), repo["id"], canonical):
                continue  # Forks/deleted head repositories are outside this workflow.
            items.append(_pull(repo, row))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise PickerError("GitHub no confirmó elementos del repositorio autorizado.") from exc
    current = await github.repo_slug(project.get("repo_path") or "", refresh=True)
    if not isinstance(current, str) or current.casefold() != configured.casefold():
        raise PickerError("El repositorio del proyecto cambió. Actualiza la lista antes de seleccionar.", "project_changed", 409)
    return {"repo": canonical, "query": query, kind: items,
            "pagination": {"page": page, "per_page": PAGE_SIZE, "has_next": not numeric and page * PAGE_SIZE < min(total, 1000), "limited": limited}}
