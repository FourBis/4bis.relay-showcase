"""Lectura de GitHub para el panel de seguimiento (fase 0 del plan).

Todo acceso remoto pasa por `gh` con la cuenta GitHub del actor actual;
no se usa autenticación global de la máquina ni fallback administrativo.

Por qué NO se reusa `mcp_servers/github_mcp.py`: ese es un subproceso MCP
que se adquiere por run para el EXPERTO, y además no habla Projects v2.
Para una lectura del lado del servidor, `gh` es un comando y ya está.

Casi todo es lectura. Las ÚNICAS escrituras son `create_board` y
`link_board` (2026-08-01, a pedido: un proyecto sin tablero tiene que
poder crearse uno desde la UI). Ambas son explícitas, disparadas por un
click humano, y no tocan items: mover tarjetas se sigue haciendo en
GitHub, con los links que el panel expone.

Degradación: si `gh` no está instalado, no está autenticado o el repo no
tiene remote, las funciones devuelven None. El panel muestra "GitHub no
configurado" y el tab Proyectos sigue funcionando igual.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional

logger = logging.getLogger("relay.github")

GH_TIMEOUT_S = 30.0

#: Caché en memoria: cada llamada a `gh` es un spawn, y en Windows eso
#: cuesta ~200ms (medido 2026-09-01: es cargar la imagen de 40MB de
#: gh.exe, NO antivirus — ver docstring de experts.cbm_cli_call).
#: El panel se abre y se
#: refresca a mano, pero igual: sin caché, dos usuarios mirando el mismo
#: proyecto son dos spawns por bloque.
CACHE_TTL_S = 60.0
_cache: dict[tuple, tuple[float, Any]] = {}

_REMOTE_RE = re.compile(
    r"(?:git@github\.com:|https://github\.com/)([^/]+/[^/\s]+?)(?:\.git)?$")


def _cached(key: tuple) -> tuple[bool, Any]:
    """(hit, value). `hit` distingue "cacheado como None" de "no está"."""
    entry = _cache.get(key)
    if entry is None or (time.monotonic() - entry[0]) > CACHE_TTL_S:
        return False, None
    return True, entry[1]


def clear_cache() -> None:
    """Para los tests y para el botón de refrescar del panel."""
    _cache.clear()


async def _gh(*args: str, cwd: Optional[str] = None) -> tuple[int, str]:
    """`gh <args>`. Nunca lanza: sin `gh` en el PATH devuelve (127, msg).

    Mismo patrón que git_flow._exec (sin shell, cada arg literal), pero
    acá el cwd es opcional: `gh project` y `gh issue --repo` no necesitan
    estar parados en el repo.
    """
    try:
        from .github_credentials import gh_env
        proc = await asyncio.create_subprocess_exec(
            "gh", *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=await gh_env())
    except (OSError, ValueError) as e:
        return 127, f"gh spawn falló: {e}"
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(),
                                          timeout=GH_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"gh {' '.join(args)} superó {GH_TIMEOUT_S}s"
    return proc.returncode or 0, out_b.decode("utf-8", errors="replace")


async def _git(repo_path: str, *args: str,
               timeout: float = 15.0) -> tuple[int, str]:
    """`git <args>` en `repo_path`. Nunca lanza: (127, msg) si el spawn
    falla (cwd inexistente, git fuera del PATH). Mismo patrón que
    git_flow._git; acá abajo del panel de proyectos."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=repo_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except (OSError, ValueError) as e:
        return 127, f"git spawn falló: {e}"
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"git {' '.join(args)} superó {timeout}s"
    return proc.returncode or 0, out_b.decode("utf-8", errors="replace")


async def set_remote(repo_path: str, url: str) -> tuple[bool, str]:
    """Vincula o cambia el remote `origin` del repo local. ESCRITURA.

    `git remote set-url origin` si ya existe, `add` si no. Devuelve
    (ok, motivo_si_falló). Invalida el slug cacheado de este repo para
    que `repo_slug` (y el panel) lean el remote nuevo enseguida.

    Solo se llama desde el botón "vincular/cambiar remoto" del panel de
    proyectos; el caller (admin) ya validó que `url` parsea a owner/repo.
    """
    exists_rc, _ = await _git(repo_path, "remote", "get-url", "origin")
    verb = "set-url" if exists_rc == 0 else "add"
    rc, out = await _git(repo_path, "remote", verb, "origin", url)
    if rc != 0:
        motivo = out.strip().splitlines()[-1][:200] if out.strip() else ""
        return False, motivo
    _cache.pop(("slug", repo_path), None)
    return True, ""


async def _gh_json(key: tuple, *args: str, cwd: Optional[str] = None) -> Any:
    """`gh` que devuelve JSON, cacheado por `key`. None si falló.

    El error se loggea una vez y se cachea igual: si `gh` no está
    autenticado, no queremos reintentar en cada refresh del panel.
    """
    from .github_credentials import actor_cache_key
    actor = await actor_cache_key()
    scoped_key = ("actor", actor, *key)
    hit, value = _cached(scoped_key)
    if hit:
        return value
    rc, out = await _gh(*args, cwd=cwd)
    result: Any = None
    if rc == 0:
        try:
            result = json.loads(out) if out.strip() else None
        except json.JSONDecodeError:
            logger.warning("gh %s: salida no-JSON (%s)", args[0], out[:200])
    else:
        logger.info("gh %s falló (rc=%d): %s", " ".join(args), rc, out[:200])
    _cache[scoped_key] = (time.monotonic(), result)
    return result


def _parse_remote(url: str) -> Optional[str]:
    """"owner/name" desde la URL del remote. None si no es GitHub."""
    m = _REMOTE_RE.search((url or "").strip())
    return m.group(1) if m else None


async def repo_slug(repo_path: str) -> Optional[str]:
    """"owner/name" del remote origin, o None si no hay/no es GitHub.

    Se deriva del remote en vez de guardarse en la DB a propósito: si
    cambia el remote (un repo que se mueve de org, caso real acá), no
    queda una config vieja apuntando a otro lado.
    """
    key = ("slug", repo_path)
    hit, value = _cached(key)
    if hit:
        return value
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "remote", "get-url", "origin", cwd=repo_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        rc = proc.returncode or 0
    except (OSError, ValueError, asyncio.TimeoutError):
        rc, out_b = 127, b""
    slug = None
    if rc == 0:
        slug = _parse_remote(out_b.decode("utf-8", errors="replace"))
    _cache[key] = (time.monotonic(), slug)
    return slug


async def issues(slug: str, limit: int = 20) -> Optional[list[dict]]:
    """Issues abiertos de `owner/repo`."""
    return await _gh_json(
        ("issues", slug, limit),
        "issue", "list", "--repo", slug, "--state", "open",
        "--limit", str(limit),
        "--json", "number,title,labels,assignees,updatedAt,url")


async def pulls(slug: str, limit: int = 20) -> Optional[list[dict]]:
    """PRs abiertos de `owner/repo`."""
    return await _gh_json(
        ("pulls", slug, limit),
        "pr", "list", "--repo", slug, "--state", "open",
        "--limit", str(limit),
        "--json", "number,title,isDraft,author,headRefName,updatedAt,url")


async def issue(slug: str, number: int) -> Optional[dict]:
    """Un issue puntual. None si no existe o `gh` no está disponible."""
    return await _gh_json(
        ("issue", slug, number),
        "issue", "view", str(number), "--repo", slug,
        "--json", "number,title,body,state,url,labels")


def issue_block(data: dict) -> str:
    """Bloque markdown con el issue, para el system del experto.

    Se arma en cada run desde lo que GitHub tiene AHORA (cacheado 60s):
    si alguien edita el issue, el experto ve la versión nueva sin que
    haya que resincronizar nada. Por eso guardamos solo el número.
    """
    body = (data.get("body") or "").strip()
    if len(body) > 4000:                 # un issue con 40KB de log no entra
        body = body[:4000] + "\n…(recortado)"
    labels = ", ".join(
        l.get("name", "") for l in (data.get("labels") or []) if l.get("name"))
    lines = [
        f"## Issue #{data.get('number')} — {data.get('title') or ''}".rstrip(),
        "",
        "Esta conversación resuelve ese issue. El PR que abra `/cerrar` lo "
        "va a cerrar automáticamente.",
    ]
    if labels:
        lines.append(f"Labels: {labels}")
    if body:
        lines += ["", body]
    return "\n".join(lines)


async def boards(owner: str) -> Optional[list[dict]]:
    """Tableros Projects v2 del owner (para el desplegable de mapeo)."""
    data = await _gh_json(
        ("boards", owner), "project", "list", "--owner", owner,
        "--format", "json")
    if isinstance(data, dict):          # `gh project list` envuelve en {projects:[…]}
        return data.get("projects") or []
    return data


async def board_items(owner: str, number: int,
                      limit: int = 100) -> Optional[list[dict]]:
    """Items del tablero, ya aplanados a lo que el panel necesita.

    `gh project item-list` devuelve el item con su `content` anidado
    (el issue/PR real) y los campos custom al mismo nivel. Aplanamos acá
    para que la UI no tenga que saber de esa forma.
    """
    data = await _gh_json(
        ("board_items", owner, number, limit),
        "project", "item-list", str(number), "--owner", owner,
        "--limit", str(limit), "--format", "json")
    raw = data.get("items") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return None
    out = []
    for it in raw:
        content = it.get("content") or {}
        out.append({
            "title": it.get("title") or content.get("title") or "(sin título)",
            "status": it.get("status") or "",
            "assignees": it.get("assignees") or [],
            "repository": content.get("repository") or "",
            "number": content.get("number"),
            "type": content.get("type") or "",
            "url": content.get("url") or "",
        })
    return out


async def create_board(owner: str, title: str) -> Optional[dict]:
    """Crea un tablero Projects v2. Devuelve {number, url, title} o None.

    ESCRITURA. Solo se llama desde el botón "crear tablero" del panel.
    """
    rc, out = await _gh("project", "create", "--owner", owner,
                        "--title", title, "--format", "json")
    if rc != 0:
        logger.warning("gh project create falló (rc=%d): %s", rc, out[:300])
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        logger.warning("gh project create: salida no-JSON: %s", out[:200])
        return None
    for cache_key in list(_cache):
        if cache_key[-2:] == ("boards", owner):
            _cache.pop(cache_key, None)
    return data


async def link_board(owner: str, number: int, repo: str) -> tuple[bool, str]:
    """Linkea el tablero al repo EN GitHub (pestaña Projects del repo).

    Devuelve (ok, motivo_si_falló). ESCRITURA, best-effort: si falla, el
    vínculo del relay igual sirve — solo se pierde que el tablero salga
    en la pestaña del repo.

    Caso real y esperable: GitHub rechaza linkear un tablero de la org
    `AuroraDemo` a un repo de `ExampleOwner` ("has different owner").
    Por eso el motivo se propaga hasta el toast: sin él, el usuario ve
    "creado" sin linkear y no sabe si es un bug o una regla de GitHub.
    """
    rc, out = await _gh("project", "link", str(number), "--owner", owner,
                        "--repo", repo)
    if rc != 0:
        logger.info("gh project link falló (rc=%d): %s", rc, out[:200])
        return False, out.strip().splitlines()[-1][:200] if out.strip() else ""
    return True, ""


def board_url(owner: str, number: int) -> str:
    """URL del tablero. Se usa como fallback cuando no la guardamos."""
    return f"https://github.com/orgs/{owner}/projects/{number}"


def group_by_status(items: list[dict]) -> dict[str, list[dict]]:
    """Agrupa por la columna del tablero, preservando el orden de llegada.

    Los sin Status van a "(sin estado)": esconderlos sería mentir sobre
    lo que hay en el tablero.
    """
    grouped: dict[str, list[dict]] = {}
    for it in items:
        grouped.setdefault(it.get("status") or "(sin estado)", []).append(it)
    return grouped
