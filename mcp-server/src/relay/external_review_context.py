"""Immutable, bounded GitHub context for a tool-free advisory review."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import PurePosixPath
from urllib.parse import quote

from . import external_pr
from .expert_steps import _redactar

MAX_FILES = 50
MAX_BLOB_BYTES = 100_000
MAX_CONTEXT_CHARS = 200_000
# Si los archivos completos no caben, se envían tramos de este radio alrededor de cada hunk.
EXCERPT_RADIUS_LINES = 40
MAX_GUIDELINE_FILES = 20
MAX_GUIDELINE_BYTES = 32_000
MAX_GUIDELINE_CHARS = 60_000
MAX_GUIDELINE_TREE_ENTRIES = 20_000
MAX_TEAM_GUIDELINE_CHARS = 16_000
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_CREDENTIAL = re.compile(r'''(?ix)(["']?(?:password|passwd|pwd|api[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token|secret|token)["']?\s*[:=]\s*["'])([^"'\r\n]{3,})(["'])''')
_UNQUOTED_CREDENTIAL = re.compile(r'''(?ix)(["']?\b(?:access[_-]?token|auth[_-]?token|token)\b["']?\s*[:=]\s*)(?!["'])(\[REDACTED\][^\s,;\#}\]]*|[^\s,;\#}\]]{3,})''')


def redact_text(value):
    quoted = _CREDENTIAL.sub(r"\1[REDACTED]\3", _redactar(value))
    return _UNQUOTED_CREDENTIAL.sub(r"\1[REDACTED]", quoted)


def normalize_text(value, maximum):
    """Bound rendered content after redaction without reactivating mentions."""
    neutral = re.sub(r"@\u200b*", "@\u200b", redact_text(value))
    if len(neutral) <= maximum:
        return neutral
    return neutral[:maximum - 1].removesuffix("@").rstrip() + "…"


def _sensitive_path(path):
    name = PurePosixPath(path).name.lower()
    return (name.startswith(".env") or name.startswith("appsettings") and name.endswith(".json")
            or name in {"id_rsa", "id_ed25519", "credentials", "credentials.json", "credentials.yml", "credentials.yaml", ".npmrc", ".pypirc", "secrets.json", "secrets.yaml", "secrets.yml"}
            or PurePosixPath(name).suffix in {".pem", ".pfx", ".p12", ".key", ".p8", ".jks", ".keystore"})


class ReviewContextError(ValueError):
    pass


class Superseded(ReviewContextError):
    pass


def team_digest(project):
    content = project.get("system_prompt", "")
    if content is None:
        content = ""
    if (not isinstance(content, str) or len(content) > MAX_TEAM_GUIDELINE_CHARS or "\0" in content
            or redact_text(content) != content):
        raise ReviewContextError("Las directrices del equipo son excesivas o contienen material potencialmente sensible")
    return hashlib.sha256(content.encode()).hexdigest()


def inputs_digest(task, head_sha):
    """Bind only the local review inputs, excluding unrelated receipt timestamps."""
    issue = task.get("issue_link") or {}
    evidence = task.get("evidence", [])
    if not isinstance(issue, dict) or not isinstance(evidence, list) or any(not isinstance(row, dict) for row in evidence):
        raise ReviewContextError("Los datos locales de revisión no son verificables")
    data = {"objective": task.get("title", ""), "risk": task.get("risk"), "head_sha": head_sha,
            "current_head": (task.get("external_pr") or {}).get("head_sha"),
            "current_base": (task.get("external_pr") or {}).get("base_sha"),
            "issue": {key: issue.get(key) for key in ("repo", "repo_id", "number", "node_id", "url")},
            "evidence": [{key: row.get(key) for key in ("request_id", "head_sha", "summary", "at", "actor")}
                         for row in evidence if row.get("head_sha") == head_sha]}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _path(value):
    if (not isinstance(value, str) or not value or len(value) > 500 or "\\" in value
            or any(ord(char) < 32 for char in value)
            or PurePosixPath(value).is_absolute() or any(part in {".", ".."} for part in value.split("/"))):
        raise ReviewContextError("GitHub devolvió una ruta no verificable")
    return value


def changed_lines(patch, additions, deletions):
    """Validate every hunk and return only changed RIGHT lines for inline findings."""
    allowed, old_left, new_left = set(), 0, 0
    right, added, removed, found = 0, 0, 0, False
    for line in patch.splitlines():
        match = _HUNK.match(line)
        if match:
            if old_left or new_left:
                raise ReviewContextError("El diff está incompleto")
            old_left, new_left = int(match[2] or 1), int(match[4] or 1)
            right, found = int(match[3]), True
        elif found and line.startswith("+"):
            allowed.add(right)
            right, new_left, added = right + 1, new_left - 1, added + 1
        elif found and line.startswith("-"):
            old_left, removed = old_left - 1, removed + 1
        elif found and line.startswith(" "):
            old_left, new_left, right = old_left - 1, new_left - 1, right + 1
        elif found and line.startswith("\\"):
            continue
        else:
            raise ReviewContextError("El diff no tiene un formato verificable")
        if old_left < 0 or new_left < 0:
            raise ReviewContextError("El diff excede los rangos declarados")
    if old_left or new_left or added != additions or removed != deletions:
        raise ReviewContextError("No se pudo acreditar el diff completo")
    return sorted(allowed)


async def check_head(job):
    repo = job.get("repo")
    if (not isinstance(repo, str) or not external_pr._SLUG.fullmatch(repo)
            or repo.rsplit("/", 1)[-1] in {".", ".."}
            or any(not isinstance(job.get(key), str) or not external_pr._SHA.fullmatch(job[key])
                   for key in ("head_sha", "base_sha"))
            or type(job.get("pr_number")) is not int or job["pr_number"] <= 0):
        raise ReviewContextError("Identidad del trabajo de revisión inválida")
    pr = await external_pr._api(f"repos/{repo}/pulls/{job['pr_number']}")
    try:
        identity_ok = (type(pr["number"]) is int and pr["number"] == job["pr_number"]
                       and external_pr.same_repository(pr["base"], job["repo_id"], repo)
                       and external_pr.same_repository(pr["head"], job["repo_id"], repo)
                       and pr["html_url"].casefold() == f"https://github.com/{repo}/pull/{job['pr_number']}".casefold())
        current = pr["state"] == "open" and pr["head"]["sha"] == job["head_sha"] and pr["base"]["sha"] == job["base_sha"]
    except (KeyError, TypeError, AttributeError):
        raise ReviewContextError("GitHub no confirmó la identidad de la PR") from None
    if not identity_ok:
        raise ReviewContextError("La PR no corresponde al repositorio del trabajo")
    if not current:
        raise Superseded("La PR cambió de commit/base o dejó de estar abierta; esta revisión es histórica")
    return pr


async def read(job):
    pr = await check_head(job)
    expected = pr.get("changed_files")
    if type(expected) is not int or not 1 <= expected <= MAX_FILES:
        raise ReviewContextError(f"Se requiere revisión manual: máximo {MAX_FILES} archivos y cobertura completa")
    prefix = f"repos/{job['repo']}"
    data = await external_pr._api(f"{prefix}/compare/{job['base_sha']}...{job['head_sha']}?per_page=1")
    if (not isinstance(data, dict) or data.get("base_commit", {}).get("sha") != job["base_sha"]
            or not isinstance(data.get("files"), list) or len(data["files"]) != expected):
        raise ReviewContextError("GitHub no confirmó todos los archivos del diff inmutable")
    files, size, seen = [], 0, set()
    for row in data["files"]:
        if not isinstance(row, dict):
            raise ReviewContextError("Metadatos de archivo inválidos")
        path = _path(row.get("filename"))
        if _sensitive_path(path) or _sensitive_path(row.get("previous_filename") or path):
            raise ReviewContextError("Un archivo potencialmente sensible requiere revisión manual; no se envió al modelo")
        sha, status = row.get("sha"), row.get("status")
        if path in seen or not isinstance(sha, str) or not external_pr._SHA.fullmatch(sha):
            raise ReviewContextError("Archivo repetido o sin identidad verificable")
        seen.add(path)
        if status not in {"added", "modified", "removed", "renamed", "changed", "copied"}:
            raise ReviewContextError("Cambio de archivo no admitido para revisión automática")
        additions, deletions, patch = row.get("additions"), row.get("deletions"), row.get("patch", "")
        if (type(additions) is not int or additions < 0 or type(deletions) is not int or deletions < 0
                or not isinstance(patch, str)):
            raise ReviewContextError("Diff no verificable")
        lines = changed_lines(patch, additions, deletions)
        if redact_text(patch) != patch:
            raise ReviewContextError("Se detectó material potencialmente sensible; no se envió al modelo")
        blob = await external_pr._api(f"{prefix}/git/blobs/{sha}")
        if (not isinstance(blob, dict) or blob.get("sha") != sha or blob.get("encoding") != "base64"
                or type(blob.get("size")) is not int or not 0 <= blob["size"] <= MAX_BLOB_BYTES
                or not isinstance(blob.get("content"), str)):
            raise ReviewContextError("Archivo binario, excesivo o no verificable: requiere revisión manual")
        try:
            raw = base64.b64decode("".join(blob["content"].split()), validate=True)
            content = raw.decode("utf-8")
        except (ValueError, UnicodeError):
            raise ReviewContextError("Archivo no textual: requiere revisión manual") from None
        actual = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if len(raw) != blob["size"] or len(raw) > MAX_BLOB_BYTES or actual != sha or "\0" in content:
            raise ReviewContextError("El contenido no coincide con el blob declarado")
        if redact_text(content) != content:
            raise ReviewContextError("Se detectó material potencialmente sensible; no se envió al modelo")
        files.append({"path": path, "status": status, "blob_sha": sha, "patch": _redactar(patch),
                      "content": _redactar(content), "content_at": "base" if status == "removed" else "head",
                      "changed_lines": lines})
    _fit_context(files)
    await check_head(job)
    return {"repo": job["repo"], "repo_id": job["repo_id"], "pr_number": job["pr_number"],
            "head_sha": job["head_sha"], "base_sha": job["base_sha"], "files": files,
            "coverage": "complete", "tests_executed": False}



def hunk_ranges(patch, side):
    """(start, count) of each hunk on the side whose content was read ('head' = new, 'base' = old)."""
    ranges = []
    for line in patch.splitlines():
        match = _HUNK.match(line)
        if match:
            start, count = (match[3], match[4]) if side == "head" else (match[1], match[2])
            ranges.append((int(start), int(count or 1)))
    return ranges


def excerpt(content, ranges, radius=None):
    """Lines around each hunk, each block headed by its real line numbers; nothing invented."""
    radius = EXCERPT_RADIUS_LINES if radius is None else radius
    lines = content.splitlines()
    spans = []
    for start, count in sorted(ranges):
        low, high = max(1, start - radius), min(len(lines), start + max(count, 1) - 1 + radius)
        if spans and low <= spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], max(spans[-1][1], high))
        elif low <= high:
            spans.append((low, high))
    return "".join(f"@@ líneas {low}-{high} de {len(lines)} @@\n" + "\n".join(lines[low - 1:high]) + "\n"
                   for low, high in spans)


def _fit_context(files):
    """The whole diff always goes; full files shrink to excerpts (largest first) until it fits."""
    for row in files:
        row["content_scope"] = "full"
    def total():
        return sum(len(row["patch"]) + len(row["content"]) for row in files)
    for row in sorted(files, key=lambda item: len(item["content"]), reverse=True):
        if total() <= MAX_CONTEXT_CHARS:
            return
        row["content"] = excerpt(row["content"], hunk_ranges(row["patch"], row["content_at"]))
        row["content_scope"] = "excerpt"
    if total() > MAX_CONTEXT_CHARS:
        raise ReviewContextError("Contexto excesivo: ni el diff con tramos de contexto cabe; requiere revisión manual")

async def assert_guideline_base(job):
    """Only the configured repo's develop or default branch authorizes guidance."""
    repo = await external_pr._api(f"repos/{job['repo']}")
    if (not isinstance(repo, dict) or type(repo.get("id")) is not int or repo["id"] != job["repo_id"]
            or str(repo.get("full_name", "")).casefold() != job["repo"].casefold()
            or not isinstance(repo.get("default_branch"), str) or not 0 < len(repo["default_branch"]) <= 200):
        raise ReviewContextError("GitHub no confirmó la rama autorizada para las directrices")
    pr = await check_head(job)
    branch = pr.get("base", {}).get("ref")
    if not isinstance(branch, str) or branch not in {"develop", repo["default_branch"]}:
        raise ReviewContextError("La revisión automática requiere base develop o la rama predeterminada; revisa esta PR manualmente")
    ref = await external_pr._api(f"repos/{job['repo']}/git/ref/heads/{quote(branch, safe='')}")
    if (not isinstance(ref, dict) or ref.get("ref") != f"refs/heads/{branch}"
            or not isinstance(ref.get("object"), dict) or ref["object"].get("type") != "commit"):
        raise ReviewContextError("GitHub no confirmó la referencia autorizada para las directrices")
    if ref["object"].get("sha") != job["base_sha"]:
        raise Superseded("La rama base autorizada avanzó; actualiza GitHub antes de revisar")
    return {"source": "github_base", "repo": job["repo"], "repo_id": job["repo_id"],
            "base_ref": branch, "base_sha": job["base_sha"]}


async def read_guidelines(job, files):
    """Read applicable instructions from the verified base commit, never PR HEAD.

    A complete Git tree proves that an optional instruction file is absent;
    permission errors and truncated trees cannot silently mean no instructions.
    No filesystem, arbitrary path, symlink, include or URL is followed.
    """
    authority = await assert_guideline_base(job)
    prefix = f"repos/{job['repo']}"
    commit = await external_pr._api(f"{prefix}/git/commits/{job['base_sha']}")
    tree_info = commit.get("tree") if isinstance(commit, dict) else None
    tree_sha = tree_info.get("sha") if isinstance(tree_info, dict) else None
    if (not isinstance(commit, dict) or commit.get("sha") != job["base_sha"]
            or not isinstance(tree_sha, str) or not external_pr._SHA.fullmatch(tree_sha)):
        raise ReviewContextError("GitHub no confirmó el árbol de directrices del commit base")
    tree = await external_pr._api(f"{prefix}/git/trees/{tree_sha}?recursive=1")
    if (not isinstance(tree, dict) or tree.get("sha") != tree_sha or tree.get("truncated") is not False
            or not isinstance(tree.get("tree"), list) or len(tree["tree"]) > MAX_GUIDELINE_TREE_ENTRIES):
        raise ReviewContextError("El árbol base de directrices es incompleto o excesivo; requiere revisión manual")
    paths = sorted({_path(row["path"]) for row in files})
    wanted = {"AGENTS.md", ".github/copilot-instructions.md"}
    for path in paths:
        parent = PurePosixPath(path).parent
        while str(parent) != ".":
            wanted.add(str(parent / "AGENTS.md"))
            parent = parent.parent
    selected = {}
    for row in tree["tree"]:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise ReviewContextError("El árbol base de directrices no es verificable")
        if row["path"] not in wanted:
            continue
        path, sha = row["path"], row.get("sha")
        if (path in selected or row.get("type") != "blob" or row.get("mode") not in {"100644", "100755"}
                or not isinstance(sha, str) or not external_pr._SHA.fullmatch(sha)):
            raise ReviewContextError("Una directriz base no es un archivo regular verificable")
        selected[path] = sha
    if len(selected) > MAX_GUIDELINE_FILES:
        raise ReviewContextError("Demasiadas directrices aplicables; requiere revisión manual sin recortes")
    guidelines, size = [], 0
    for path, sha in sorted(selected.items(), key=lambda row: (row[0].count("/"), row[0])):
        blob = await external_pr._api(f"{prefix}/git/blobs/{sha}")
        if (not isinstance(blob, dict) or blob.get("sha") != sha or blob.get("encoding") != "base64"
                or type(blob.get("size")) is not int or not 0 <= blob["size"] <= MAX_GUIDELINE_BYTES
                or not isinstance(blob.get("content"), str)):
            raise ReviewContextError("Una directriz base es excesiva o no verificable")
        try:
            raw = base64.b64decode("".join(blob["content"].split()), validate=True)
            content = raw.decode("utf-8")
        except (ValueError, UnicodeError):
            raise ReviewContextError("Una directriz base no es texto UTF-8 verificable") from None
        actual = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if (len(raw) != blob["size"] or len(raw) > MAX_GUIDELINE_BYTES or actual != sha or "\0" in content
                or redact_text(content) != content):
            raise ReviewContextError("Una directriz base no coincide con su blob o contiene material potencialmente sensible")
        size += len(content)
        if size > MAX_GUIDELINE_CHARS:
            raise ReviewContextError("Directrices excesivas; requiere revisión manual sin recortes")
        parent = str(PurePosixPath(path).parent)
        applies_to = paths if path in {"AGENTS.md", ".github/copilot-instructions.md"} else [
            changed for changed in paths if changed.startswith(parent + "/")]
        guidelines.append({**authority, "path": path,
                           "blob_sha": sha, "sha256": hashlib.sha256(raw).hexdigest(),
                           "applies_to": applies_to, "content": content})
    return guidelines
