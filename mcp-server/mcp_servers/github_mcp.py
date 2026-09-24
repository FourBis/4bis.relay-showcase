"""GitHub MCP legado. Relay usa account_tools con OAuth por llamada.
Este proceso solo admite un token y actor explícitos; nunca usa gh auth.
No se comparte en el pool del Relay multiusuario."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                    stream=sys.stderr)
log = logging.getLogger("github-mcp")

mcp = FastMCP("github-mcp")

GITHUB_API = "https://api.github.com"
_client: Optional[httpx.AsyncClient] = None
_lock = asyncio.Lock()


def _resolver_token() -> str:
    """Sin actor explícito, nunca usar una credencial global de la máquina."""
    if not os.environ.get("RELAY_GITHUB_ACTOR", "").strip():
        return ""
    return os.environ.get("GITHUB_TOKEN", "").strip()

def _auth_headers() -> dict:
    """Credencial explícita del actor; las llamadas sin ella se rechazan."""
    token = _resolver_token()
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "4bis-relay-github-mcp",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def _get(path: str, params: Optional[dict] = None) -> tuple[int, dict | str]:
    """Wrapper sobre httpx con manejo de errores legible.
    Devuelve (status, body_dict_o_texto)."""
    if not _resolver_token():
        return 403, 'Conecta tu cuenta GitHub en Relay; la credencial de máquina no está permitida.'
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=GITHUB_API,
            headers=_auth_headers(),
            timeout=20.0,
        )
    try:
        r = await _client.get(path, params=params or {})
        if r.status_code == 200:
            return r.status_code, r.json()
        # 404 → repo no existe; 403 con rate limit → texto específico.
        if r.status_code == 403 and "rate limit" in r.text.lower():
            return r.status_code, f"RATE LIMIT: {r.json().get('message', r.text)}"
        return r.status_code, f"HTTP {r.status_code}: {r.text[:200]}"
    except httpx.HTTPError as e:
        return 0, f"ERROR: {type(e).__name__}: {e}"


async def _post(path: str, payload: dict) -> tuple[int, dict | str]:
    """POST con el mismo manejo de errores que `_get`.

    2026-08-26. Hasta hoy este MCP era solo-lectura: el docstring del
    modulo decia que "la API ya es read-only si solo usamos estos
    endpoints", y era cierto. Sin una tool de escritura, un experto que
    necesite crear un issue en `AuroraDemo/auth-demo` solo puede
    reportar el bloqueo.

    OJO: esto ya no es un MCP de lectura. Escribe en repos de clientes.
    """
    if not _resolver_token():
        return 403, 'Conecta tu cuenta GitHub en Relay; la credencial de máquina no está permitida.'
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=GITHUB_API, headers=_auth_headers(), timeout=30.0)
    try:
        r = await _client.post(path, json=payload)
        if r.status_code in (200, 201):
            return r.status_code, r.json()
        if r.status_code == 403 and "rate limit" in r.text.lower():
            return r.status_code, f"RATE LIMIT: {r.json().get('message', r.text)}"
        # 404 en un POST casi nunca es "no existe": con token sin scope de
        # escritura GitHub devuelve 404 igual que con repo invisible. Se
        # nombra la causa probable para que el modelo no salga a probar
        # variantes del nombre, que es lo que hizo la ultima vez.
        if r.status_code == 404:
            return r.status_code, (
                "HTTP 404: el repo no existe, o el token no tiene permiso de "
                "escritura sobre el. Si `get_repo` del mismo repo funciona, "
                "es lo segundo: falta scope `repo` o acceso a la organizacion.")
        return r.status_code, f"HTTP {r.status_code}: {r.text[:300]}"
    except httpx.HTTPError as e:
        return 0, f"ERROR: {type(e).__name__}: {e}"


# ---- tools ----

@mcp.tool()
async def get_repo(owner: str, repo: str) -> str:
    """Metadata del repo: stars, default_branch, descripción, visibility."""
    async with _lock:
        status, body = await _get(f"/repos/{owner}/{repo}")
        if status != 200:
            return f"ERROR ({status}): {body}"
        d = body
        return (f"OK {owner}/{repo}\n"
                f"  full_name:    {d.get('full_name')}\n"
                f"  description:  {d.get('description', '(none)')}\n"
                f"  default_branch: {d.get('default_branch')}\n"
                f"  stars:        {d.get('stargazers_count')}\n"
                f"  forks:        {d.get('forks_count')}\n"
                f"  open_issues:  {d.get('open_issues_count')}\n"
                f"  visibility:   {d.get('visibility', 'public')}\n"
                f"  language:     {d.get('language', '?')}\n"
                f"  updated_at:   {d.get('updated_at')}")


@mcp.tool()
async def list_issues(owner: str, repo: str,
                      state: str = "open", limit: int = 20) -> str:
    """Lista issues (sin body) — `state`: open/closed/all."""
    async with _lock:
        status, body = await _get(
            f"/repos/{owner}/{repo}/issues",
            params={"state": state, "per_page": min(limit, 100)})
        if status != 200:
            return f"ERROR ({status}): {body}"
        # La API mezcla PRs en /issues; filtrar las que tienen pull_request.
        issues = [i for i in body if "pull_request" not in i]
        if not issues:
            return f"OK: 0 issues en estado {state!r}"
        lines = [f"OK: {len(issues)} issues en estado {state!r}:"]
        for i in issues[:limit]:
            labels = ",".join(l["name"] for l in i.get("labels", []))
            lines.append(
                f"  #{i['number']:>5}  {i['title'][:70]:<70}  "
                f"@{i['user']['login']:<20}  [{labels}]")
        return "\n".join(lines)


@mcp.tool()
async def get_issue(owner: str, repo: str, number: int) -> str:
    """Issue completo: body + comments."""
    async with _lock:
        status, issue = await _get(f"/repos/{owner}/{repo}/issues/{number}")
        if status != 200:
            return f"ERROR ({status}): {issue}"
        if "pull_request" in issue:
            return f"#{number} es un PR, no un issue. Usa get_pull()."
        body_text = (issue.get("body") or "(sin descripción)").strip()
        # Truncar el body para no saturar el contexto.
        if len(body_text) > 4000:
            body_text = body_text[:4000] + "\n... (truncado)"
        # Traer comments
        status_c, comments = await _get(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            params={"per_page": 10})
        comment_lines = []
        if status_c == 200 and comments:
            for c in comments[:10]:
                author = c.get("user", {}).get("login", "?")
                cb = (c.get("body") or "").strip()
                if len(cb) > 500:
                    cb = cb[:500] + "..."
                comment_lines.append(f"\n  --- comment @{author} ---\n  {cb}")
        labels = ",".join(l["name"] for l in issue.get("labels", []))
        return (f"OK #{issue['number']}  {issue['title']}\n"
                f"  state:    {issue['state']}\n"
                f"  author:   @{issue['user']['login']}\n"
                f"  labels:   [{labels}]\n"
                f"  created:  {issue['created_at']}\n"
                f"  updated:  {issue['updated_at']}\n"
                f"  url:      {issue['html_url']}\n"
                f"\n--- body ---\n{body_text}"
                + "".join(comment_lines))


@mcp.tool()
async def list_pulls(owner: str, repo: str,
                     state: str = "open", limit: int = 20) -> str:
    """Lista PRs (sin diff) — `state`: open/closed/all."""
    async with _lock:
        status, body = await _get(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": state, "per_page": min(limit, 100)})
        if status != 200:
            return f"ERROR ({status}): {body}"
        if not body:
            return f"OK: 0 PRs en estado {state!r}"
        lines = [f"OK: {len(body)} PRs en estado {state!r}:"]
        for p in body[:limit]:
            lines.append(
                f"  #{p['number']:>5}  {p['title'][:70]:<70}  "
                f"@{p['user']['login']:<20}  "
                f"({p['head']['ref']} ← {p['base']['ref']})")
        return "\n".join(lines)


@mcp.tool()
async def get_pull(owner: str, repo: str, number: int) -> str:
    """PR completo: body + metadata + lista de archivos cambiados."""
    async with _lock:
        status, pr = await _get(f"/repos/{owner}/{repo}/pulls/{number}")
        if status != 200:
            return f"ERROR ({status}): {pr}"
        body_text = (pr.get("body") or "(sin descripción)").strip()
        if len(body_text) > 2000:
            body_text = body_text[:2000] + "\n... (truncado)"
        # Archivos cambiados
        status_f, files = await _get(
            f"/repos/{owner}/{repo}/pulls/{number}/files",
            params={"per_page": 30})
        file_lines = []
        if status_f == 200 and files:
            for f in files[:30]:
                file_lines.append(
                    f"    {f['status']:>10}  "
                    f"+{f.get('additions', 0):>4}/-{f.get('deletions', 0):>4}  "
                    f"{f['filename']}")
        return (f"OK #{pr['number']}  {pr['title']}\n"
                f"  state:    {pr['state']} ({pr.get('merged') and 'merged' or 'not merged'})\n"
                f"  author:   @{pr['user']['login']}\n"
                f"  branch:   {pr['head']['ref']} → {pr['base']['ref']}\n"
                f"  created:  {pr['created_at']}\n"
                f"  updated:  {pr['updated_at']}\n"
                f"  url:      {pr['html_url']}\n"
                f"  +{pr.get('additions', 0)} -{pr.get('deletions', 0)} "
                f"en {pr.get('changed_files', 0)} archivos\n"
                f"\n--- body ---\n{body_text}"
                + ("\n\n--- files ---\n" + "\n".join(file_lines)
                   if file_lines else ""))


@mcp.tool()
async def list_commits(owner: str, repo: str,
                       branch: Optional[str] = None,
                       limit: int = 20) -> str:
    """Commits (sha corto + msg + author + fecha). `branch` opcional."""
    async with _lock:
        params = {"per_page": min(limit, 100)}
        if branch:
            params["sha"] = branch
        status, body = await _get(
            f"/repos/{owner}/{repo}/commits", params=params)
        if status != 200:
            return f"ERROR ({status}): {body}"
        if not body:
            return f"OK: 0 commits"
        lines = [f"OK: {len(body)} commits" + (f" en {branch!r}" if branch else "")]
        for c in body[:limit]:
            sha = c["sha"][:7]
            msg = c["commit"]["message"].splitlines()[0][:60]
            author = c["commit"]["author"]["name"]
            date = c["commit"]["author"]["date"][:10]
            lines.append(f"  {sha}  {date}  {author:<20}  {msg}")
        return "\n".join(lines)


@mcp.tool()
async def search_issues(owner: str, repo: str,
                        query: str, limit: int = 20) -> str:
    """Search en issues del repo: ej `query='is:issue is:open label:bug'`."""
    async with _lock:
        # GitHub search API usa el formato "repo:owner/repo is:issue ...".
        full_q = f"repo:{owner}/{repo} {query}"
        status, body = await _get(
            "/search/issues",
            params={"q": full_q, "per_page": min(limit, 100)})
        if status != 200:
            return f"ERROR ({status}): {body}"
        items = body.get("items", [])
        total = body.get("total_count", 0)
        if not items:
            return f"OK: 0 resultados para {full_q!r} (total_count={total})"
        lines = [f"OK: {len(items)} hits (total_count={total}) para {full_q!r}:"]
        for i in items:
            labels = ",".join(l["name"] for l in i.get("labels", []))
            lines.append(
                f"  #{i['number']:>5}  {i['title'][:70]:<70}  "
                f"@{i['user']['login']:<20}  [{labels}]")
        return "\n".join(lines)


async def _close() -> None:
    if not _resolver_token():
        return 403, 'Conecta tu cuenta GitHub en Relay; la credencial de máquina no está permitida.'
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:  # noqa: BLE001
            pass
        _client = None


@mcp.tool()
async def create_issue(owner: str, repo: str, title: str, body: str = "",
                       assignees: str = "", labels: str = "") -> str:
    """Crea un issue. ESCRIBE en GitHub.

    Args:
        owner: duenio del repo (org o usuario).
        repo: nombre del repo.
        title: titulo del issue. Obligatorio.
        body: cuerpo en markdown. Opcional.
        assignees: logins separados por coma. OJO: son logins de GitHub,
            no mails — `collaborator@example.test` no es un login valido y
            GitHub lo ignora en silencio. Si solo tenes el mail, buscá el
            login primero o dejalo vacio y avisá.
        labels: etiquetas separadas por coma. Deben existir en el repo.
    """
    if not title.strip():
        return "ERROR: `title` es obligatorio."
    payload: dict = {"title": title.strip()}
    if body.strip():
        payload["body"] = body
    lista = lambda s: [x.strip() for x in s.split(",") if x.strip()]  # noqa: E731
    if assignees.strip():
        payload["assignees"] = lista(assignees)
    if labels.strip():
        payload["labels"] = lista(labels)
    async with _lock:
        status, d = await _post(f"/repos/{owner}/{repo}/issues", payload)
    if status not in (200, 201):
        return f"ERROR ({status}): {d}"
    # Se reporta a quien QUEDO asignado, no a quien se pidio: GitHub
    # descarta en silencio los assignees que no son colaboradores, y sin
    # esto el experto reporta "asignado" sobre una lista vacia.
    quedaron = [a.get("login") for a in (d.get("assignees") or [])]
    pedidos = lista(assignees)
    aviso = ""
    if pedidos and set(pedidos) != set(quedaron):
        faltan = [x for x in pedidos if x not in quedaron]
        aviso = (f"\n  AVISO: GitHub NO asigno a {faltan} — no son "
                 "colaboradores del repo, o no son logins validos.")
    return (f"OK creado #{d.get('number')}\n"
            f"  url:       {d.get('html_url')}\n"
            f"  title:     {d.get('title')}\n"
            f"  assignees: {quedaron or '(ninguno)'}\n"
            f"  labels:    {[l.get('name') for l in (d.get('labels') or [])] or '(ninguna)'}"
            f"{aviso}")


@mcp.tool()
async def comment_issue(owner: str, repo: str, number: int, body: str) -> str:
    """Comenta un issue o PR existente. ESCRIBE en GitHub."""
    if not body.strip():
        return "ERROR: `body` es obligatorio."
    async with _lock:
        status, d = await _post(
            f"/repos/{owner}/{repo}/issues/{number}/comments", {"body": body})
    if status not in (200, 201):
        return f"ERROR ({status}): {d}"
    return (f"OK comentado en #{number}\n"
            f"  url: {d.get('html_url')}")


if __name__ == "__main__":
    try:
        mcp.run()
    finally:
        try:
            asyncio.run(_close())
        except Exception:  # noqa: BLE001
            pass
