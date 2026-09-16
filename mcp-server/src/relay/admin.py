"""Endpoints REST de la Admin UI (ADR-014).

El grueso de `/admin/api/*`. La UI que los consume vive en
`admin_static/` y se documenta en docs/ADMIN_UI.md (20 tabs); el
contrato HTTP, en docs/API.md.

Qué cubre, a grandes rasgos:
  - vitales del relay, logs, informes y métricas
  - CRUD de projects + sus flags tipados (`_PROJECT_FLAGS`), workspace,
    system prompt, diff, diagramas y seguimiento de GitHub
  - indexación cbm por repo (kick + status + tabla de archivos)
  - catálogo de MCPs (incluido el instalador desde GitHub), skills y
    sus borradores, comandos de Discord, y el catálogo de `models`
  - CRM local: clientes, sync, digest y la cadena cliente → proyecto

Lo que NO hace: correr expertos (eso es `/experts/*` en server.py) ni
resolver identidad — el rol lo aplica el middleware `require_role` de
identity.py, y casi todo esto es owner-only.

> El `docs/ADMIN_UI_SPEC.md` que citaba este docstring era el plan
> pre-ejecución; se movió a docs/legacy/ en el compactado v1.0.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import math
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from aiohttp import web

from . import __version__ as RELAY_VERSION
from . import bot_control
from . import config as relay_config
from . import crm as crm_mod
from . import github as github_mod
from . import identity
from . import logctx
from . import mcp_pool
from . import memory
from .experts import cbm_binary_path
from .mcp_installer import McpInstaller
from .notify import NotifyClient
# Iter 4.7: workspace scaffold usa Agent + pydantic + build_model.
# Import a top-level (no lazy dentro de la función) para que los tests
# puedan `patch("relay.admin.Agent")` sin sorpresas.
from pydantic import BaseModel
from pydantic_ai import Agent
from .experts import (
    ModelUnavailable, build_model, structured_output_settings)
# SKILLS_KEY está declarada en server.py (no se puede redefinir un
# web.AppKey con el mismo nombre — aiohttp tira). La importamos acá
# para usar el mismo AppKey en este módulo.
from .server import DB_KEY, NOTIFY_KEY, SESSIONS_KEY, SKILLS_KEY  # noqa: F401

# FIX: cuando el relay corre como `python -m relay.server`, el módulo se carga
# como `__main__` y sus AppKeys se identifican por `__module__ == "__main__"`.
# Si hacemos `from .server import DB_KEY` desde otro módulo (admin.py),
# la AppKey importada lleva `__module__ == "relay.server"` — y aiohttp la
# considera DISTINTA a la guardada en el state (que es la de __main__).
# Truco: importar los AppKeys desde `sys.modules["__main__"]`, que apunta
# al mismo objeto que `relay.server` cuando se ejecuta con `-m`.
import sys
_main = sys.modules.get("__main__")
if _main is not None and getattr(_main, "DB_KEY", None) is not None:
    DB_KEY = _main.DB_KEY
    SESSIONS_KEY = _main.SESSIONS_KEY
    SKILLS_KEY = _main.SKILLS_KEY
    COMMANDS_KEY = _main.COMMANDS_KEY
    BIND_HOST_KEY = _main.BIND_HOST_KEY
    MCP_POOL_KEY = _main.MCP_POOL_KEY
    MCP_INSTALLER_KEY = _main.MCP_INSTALLER_KEY
    SKILL_BROWSER_KEY = _main.SKILL_BROWSER_KEY
    NIGHT_KEY = _main.NIGHT_KEY
    NOTIFY_KEY = _main.NOTIFY_KEY
else:
    # Fallback para tests / ejecución como submódulo.
    from .server import (  # type: ignore
        BIND_HOST_KEY, COMMANDS_KEY, DB_KEY, MCP_INSTALLER_KEY, MCP_POOL_KEY,
        NIGHT_KEY, NOTIFY_KEY, SESSIONS_KEY, SKILL_BROWSER_KEY,
    )

logger = logging.getLogger("relay.admin")

CBM_INDEX_TIMEOUT_S = int(os.environ.get("CBM_INDEX_TIMEOUT", "600"))
ADMIN_STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "admin_static"


# ---------- helpers comunes ----------

def _cbm_env() -> dict:
    """Env vars que el relay le pasa al binario cbm para sus spawns.

    Sin paths de usuario hardcodeados: la cache deriva del home y el
    allowed-root sale de system_config / FOURBIS_REPOS_ROOT
    (config.repos_root). CBM_* por env siguen ganando si están.
    """
    return {
        **os.environ,
        "CBM_CACHE_DIR": os.environ.get(
            "CBM_CACHE_DIR", str(Path.home() / ".4bis" / "cbm-cache")),
        "CBM_ALLOWED_ROOT": os.environ.get(
            "CBM_ALLOWED_ROOT", relay_config.repos_root()),
    }


async def _cbm_cli_text(*args: str) -> tuple[str, str]:
    """Llama `cbm cli <args...>` y devuelve (texto_plano, error).

    2026-07-25: `cbm cli` imprime tablas legibles por humanos, NO JSON —
    solo emite JSON cuando falla (el dict {"error":...,"available_projects":
    [...]}). La antigua `_cbm_cli` (borrada 2026-09-02 por no tener
    callers) buscaba "la última línea que arranca con { o [", así que
    contra search_graph/get_architecture devolvía {} SIEMPRE y los callers
    leían .get("results", []) → lista vacía. Eso es lo que dejaba el panel
    de archivos del tab Índice y el autogen "Top files" de Diagramas
    permanentemente en blanco (no era, como decía el comentario viejo, que
    cbm filtrara `File` como noise: hay 633 nodos File en SampleApp).

    2026-09-02: `args[0]` ("cli") ya no se usa acá — se mantiene en la
    firma para no tocar los 7 callers. Camino rápido: la sesión MCP
    persistente de `experts.cbm_call` (0-16ms) devuelve el mismo tool
    directo, no por `cbm cli`, así que la forma de la respuesta cambia:
      - {"text": "<tabla>"}                     → tabla de siempre
      - {"error": "..."}                        → error de dominio
      - cualquier otra cosa top-level (p.ej.
        {"status":"ambiguous", ...})            → el JSON crudo COMO
        TEXTO, sin tocar: los callers de get(kind="sequence") lo
        parsean ellos mismos para mostrar candidatos.
    Cae al subprocess `cbm cli` de siempre — único camino que sabe
    imprimir tablas — cuando la sesión+CLI de cbm_call no devolvieron
    JSON útil (sesión apagada Y el CLI JSON-only tampoco entendió el
    tool) o cuando la respuesta no parsea como JSON en absoluto.
    """
    tool, args_json = args[1], args[2]
    from . import experts
    # Sesión ya apagada: ir DERECHO al subprocess. Si no, `cbm_call` cae
    # a `cbm_cli_call`, que corre el mismo `cbm cli` y siempre falla acá
    # (estos 7 tools imprimen tabla, no JSON) — y recién entonces
    # spawnearíamos de nuevo. Serían DOS spawns fríos (2078ms medidos)
    # justo en el escenario degradado, contra 1031ms de uno solo.
    if experts._cbm_session_off:
        return await _cbm_cli_text_subprocess(*args)
    try:
        out = await experts.cbm_call(tool, json.loads(args_json), timeout=30.0)
        j = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return await _cbm_cli_text_subprocess(*args)
    if isinstance(j, dict) and "text" in j:
        return j["text"], ""
    if isinstance(j, dict) and j.get("error"):
        err = str(j["error"])
        if err.startswith("cbm no devolvió JSON"):
            return await _cbm_cli_text_subprocess(*args)
        return "", err
    return out, ""


async def _cbm_cli_text_subprocess(*args: str) -> tuple[str, str]:
    """Camino viejo: spawnea `cbm cli <args...>` (~1.13s por llamada).

    Único fallback que sabe imprimir tablas legibles cuando la sesión
    MCP persistente y el CLI JSON-only de `experts.cbm_call` fallan
    los dos — ver `_cbm_cli_text`.
    """
    bin_path = cbm_binary_path()
    if not bin_path:
        return "", "codebase-memory-mcp no instalado"
    proc = await asyncio.create_subprocess_exec(
        bin_path, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=_cbm_env(),
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return "", stderr.decode("utf-8", errors="replace")[:500]
    text = stdout.decode("utf-8", errors="replace").strip()
    # El error viaja como JSON en stdout aun con returncode 0.
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("{"):
            try:
                j = json.loads(s)
            except json.JSONDecodeError:
                continue
            if isinstance(j, dict) and j.get("error"):
                return "", str(j["error"])
    return text, ""


def _split_row(line: str, ncols: int) -> list[str]:
    """Parte una fila de tabla cbm respetando comillas dobles.

    ponytail: split por espacios + merge de segmentos entrecomillados. Techo:
    un campo con espacios y SIN comillas en medio de la fila desalinea las
    columnas siguientes (pasa en file_tree con nombres con espacios). Cuando
    el conteo no cuadra devolvemos lo que haya y el caller usa .get() con
    default. Upgrade si molesta: pedirle a cbm salida TSV/--json estructurado.
    """
    parts: list[str] = []
    buf: list[str] = []
    in_q = False
    for tok in line.split(" "):
        if not tok and not in_q:
            continue
        if in_q:
            buf.append(tok)
            if tok.endswith('"'):
                parts.append(" ".join(buf).strip('"'))
                buf, in_q = [], False
        elif tok.startswith('"') and not (tok.endswith('"') and len(tok) > 1):
            buf, in_q = [tok], True
        else:
            parts.append(tok.strip('"'))
    if buf:
        parts.append(" ".join(buf).strip('"'))
    return parts[:ncols] if ncols and len(parts) > ncols else parts


#: `name: <count>  (cols: a b c)` — cabecera de sección tabular de cbm.
_CBM_SECTION_RE = re.compile(r"^(\w+):\s*(\d+)\s*\(cols:\s*([^)]+)\)\s*$")
#: `key: value` — escalares sueltos (project, total_nodes, ...).
_CBM_SCALAR_RE = re.compile(r"^(\w+):\s*(.*)$")


def _parse_cbm_sections(text: str) -> dict:
    """Texto de `cbm cli get_architecture` → dict estructurado.

    Formato de entrada:
        project: NAME
        total_nodes: 11430
        boundaries: 10  (cols: from to calls)
          Services Repositories 376
        layers: 11  (cols: name layer reason)
          Helpers core "high fan-in (111 in, 0 out)"

    Salida: {"project": "NAME", "total_nodes": 11430,
             "boundaries": [{"from": "Services", "to": "Repositories",
                             "calls": "376"}, ...], ...}
    """
    out: dict = {}
    cur: list[dict] | None = None
    cols: list[str] = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        if raw_line[0].isspace():  # fila de la sección abierta
            if cur is None:
                continue
            vals = _split_row(raw_line.strip(), len(cols))
            cur.append({c: (vals[i] if i < len(vals) else "")
                        for i, c in enumerate(cols)})
            continue
        m = _CBM_SECTION_RE.match(raw_line)
        if m:
            cols = m.group(3).split()
            cur = []
            out[m.group(1)] = cur
            continue
        cur = None
        m = _CBM_SCALAR_RE.match(raw_line)
        if m:
            v = m.group(2).strip()
            out[m.group(1)] = int(v) if v.isdigit() else v
    return out


# Cache TTL de `cbm cli list_projects` (perf 2026-07-19): cada spawn
# del exe cbm paga ~1.1s cargando su imagen de 295MB antes de main()
# (NO es Defender — ver docstring de experts.cbm_cli_call),
# y la UI pollea health cada 15s — sin cache, cada poll y cada carga
# de tab pagaba ese spawn. Invalidación: al terminar cualquier job de
# reindex (_mark_job_done); los reindex del watcher externo quedan
# cubiertos por el TTL (stats a lo sumo 30s viejas, cosmético). Sin
# lock a propósito: dos requests concurrentes con cache frío hacen 2
# spawns una vez por ventana TTL (benigno), y un asyncio.Lock
# module-level se pinnea al primer loop (rompería la suite, que crea
# un loop por test).
_CBM_PROJECTS_TTL_S = 30.0
# Techo para el spawn de cbm cuando lo pide un endpoint de UI (el front
# corta a los 15s; 5s deja margen para el resto del handler).
_CBM_PROJECTS_TIMEOUT_S = 5.0
_cbm_projects_cache: dict | None = None
_cbm_projects_at = 0.0


async def _cbm_list_projects() -> dict:
    """Lista proyectos indexados por cbm leyendo dbs directo del cache.

    Medido 2026-07-23: `cbm cli list_projects` tarda 28-32s constante
    cuando hay 50+ dbs (la operación agrega trabajo de validación
    interna que escala con el catálogo). En cambio leer los sqlite
    directo del cache sale <200ms para 56 dbs y devuelve el mismo
    shape JSON. El binario cbm se sigue usando para operaciones
    puntuales (search_graph, get_architecture, etc.) — esto es solo
    para listar.

    Cache TTL 30s como antes, pero ahora el hit es <1ms y el cold
    path es ~150ms, no 30s. La UI pollea health cada 15s y abre
    drawers que disparan este endpoint: el cache es defensa contra
    storms, el path real es la lectura directa.
    """
    global _cbm_projects_cache, _cbm_projects_at
    if (_cbm_projects_cache is not None
            and time.monotonic() - _cbm_projects_at < _CBM_PROJECTS_TTL_S):
        return _cbm_projects_cache
    data = await asyncio.to_thread(_read_cbm_cache_index)
    if not data.get("error"):
        _cbm_projects_cache = data
        _cbm_projects_at = time.monotonic()
    return data


def _read_cbm_cache_index() -> dict:
    """Lee `~/.4bis/cbm-cache/*.db` y devuelve el mismo shape que
    `cbm cli list_projects`: `{"projects": [{"name", "root_path",
    "nodes", "edges", "size_bytes"}]}`.

    Schema cbm 0.10.0-fourbis-rebase verificado 2026-07-23 en
    C-Users-demo-source-repos-FourBis-4bis.relay.db:
      - projects(name PK, indexed_at, root_path)
      - nodes(id, project, ...)  → COUNT WHERE project = ?
      - edges(id, project, ...)  → COUNT WHERE project = ?
    Una db por proyecto; el nombre del archivo (sin .db) es el
    `cbm name` que devuelven las tools. Si cbm cambia el schema,
    caemos a `{"error": "schema changed"}` y los handlers sirven
    el cache anterior (defensa contra regresiones upstream).
    """
    import sqlite3 as _sqlite3
    cache_dir = Path(os.environ.get(
        "CBM_CACHE_DIR", str(Path.home() / ".4bis" / "cbm-cache")))
    if not cache_dir.is_dir():
        return {"projects": []}
    projects: list[dict] = []
    for db_path in cache_dir.glob("*.db"):
        if db_path.name.startswith("_"):
            continue
        name = db_path.stem  # cbm project name
        try:
            size_bytes = db_path.stat().st_size
            con = _sqlite3.connect(str(db_path), timeout=1.0)
            try:
                # root_path + indexed_at
                row = con.execute(
                    "SELECT root_path, indexed_at FROM projects "
                    "WHERE name = ? LIMIT 1", (name,)).fetchone()
                if not row:
                    # cbm a veces crea la db antes de tener fila en projects;
                    # saltamos y seguimos. La próxima index la va a llenar.
                    continue
                root_path, indexed_at = row
                nodes = con.execute(
                    "SELECT COUNT(*) FROM nodes WHERE project = ?",
                    (name,)).fetchone()[0]
                edges = con.execute(
                    "SELECT COUNT(*) FROM edges WHERE project = ?",
                    (name,)).fetchone()[0]
            finally:
                con.close()
            projects.append({
                "name": name,
                "root_path": root_path,
                "indexed_at": indexed_at,
                "nodes": nodes,
                "edges": edges,
                "size_bytes": size_bytes,
            })
        except _sqlite3.Error as e:
            # db corrupta o lock transitorio: la salteamos, no rompemos
            # el listado entero por una db jodida. Se loguea porque si
            # no, un repo que desaparece del listado no deja rastro y
            # no hay por dónde empezar a mirar.
            logger.debug("cbm: salteo %s (%r)", name, e)
            continue
        except OSError as e:
            logger.debug("cbm: salteo %s (%r)", name, e)
            continue
    projects.sort(key=lambda p: p["name"])
    return {"projects": projects}


def _cbm_indexed_count() -> int:
    """Cuántos repos tiene cbm indexados, SIN spawnear el binario.

    cbm guarda un `.db` por proyecto en CBM_CACHE_DIR, así que contar
    archivos da el mismo número que `list_projects` (verificado
    2026-07-21: 90 y 90) por una fracción del costo: **1.28ms contra
    20.668s** de la llamada al binario en esa misma medición.

    Eso era un bug real, no una micro-optimización: `api_health` llamaba
    a `_cbm_list_projects()` y la UI pollea health cada 15s con un cache
    de 30s, o sea un spawn de 273MB cada 30 segundos para un contador —
    y cuando el spawn se iba a 20s (lock del store contra un reindex del
    watcher, que vigila 49 repos) el endpoint se colgaba con él. Se veía
    como flakiness de `test_health_endpoint_responds_2xx`.

    `_config.db` es de cbm, no un proyecto: por eso el filtro de `_`.
    """
    cache_dir = Path(os.environ.get(
        "CBM_CACHE_DIR", str(Path.home() / ".4bis" / "cbm-cache")))
    try:
        return sum(1 for p in cache_dir.iterdir()
                   if p.suffix == ".db" and not p.name.startswith("_"))
    except OSError:
        return 0


def _invalidate_cbm_projects_cache() -> None:
    global _cbm_projects_cache
    _cbm_projects_cache = None


def _serialize(obj: Any) -> Any:
    """Serializa para JSON un objeto de cbm (que puede traer set/list/etc)."""
    if isinstance(obj, (list, tuple, set)):
        return [_serialize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    return obj


def _norm_path_key(p: str) -> str:
    """Clave canónica para comparar paths Windows: separadores unificados,
    sin trailing slash, case-insensitive. Dos repos con el mismo basename
    en carpetas distintas NO deben colisionar (por eso path completo)."""
    return (p or "").replace("\\", "/").rstrip("/").lower()


# UI 2026-07-20: URL del remote origin para el botón GitHub por proyecto.
# El browser no puede leer .git/config — este es el único camino. Cache
# por mtime del config (cambia casi nunca); parse a mano para no
# spawnear git por proyecto en cada GET /admin/api/projects.
_remote_url_cache: dict[str, tuple[float, Optional[str]]] = {}


def _resolve_git_config(repo_path: str) -> Optional[Path]:
    """Path al `.git/config` efectivo. Maneja worktrees: ahí `.git` es un
    archivo `gitdir: <path>` y el remote vive en el config COMPARTIDO del
    repo principal, no en uno propio del worktree. Sin esto un proyecto que
    es worktree mostraba 'sin remoto' aunque tuviera origin."""
    git = Path(repo_path) / ".git"
    if git.is_dir():
        return git / "config"
    if git.is_file():
        try:
            line = git.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if not line.startswith("gitdir:"):
            return None
        gitdir = Path(line.split(":", 1)[1].strip())
        if not gitdir.is_absolute():
            gitdir = (git.parent / gitdir).resolve()
        # gitdir = <common>/worktrees/<name>; el config compartido está 2 arriba.
        return gitdir.parent.parent / "config"
    return None


def _git_remote_url(repo_path: str) -> Optional[str]:
    cfg = _resolve_git_config(repo_path)
    if cfg is None:
        return None
    try:
        mtime = cfg.stat().st_mtime
    except OSError:
        return None
    hit = _remote_url_cache.get(repo_path)
    if hit and hit[0] == mtime:
        return hit[1]
    url: Optional[str] = None
    try:
        in_origin = False
        for line in cfg.read_text(encoding="utf-8",
                                  errors="replace").splitlines():
            s = line.strip()
            if s.startswith("["):
                in_origin = s.replace(" ", "") == '[remote"origin"]'
            elif in_origin and s.startswith("url"):
                _, _, v = s.partition("=")
                url = v.strip() or None
                break
    except OSError:
        url = None
    _remote_url_cache[repo_path] = (mtime, url)
    return url


# Cache en memoria: repo_path absoluto -> cbm project_name.
# cbm 0.8.1 nombra proyectos normalizando la ruta así:
#   C:/Users/demo/source/repos/INVENTORYDEMO -> C-Users-demo-source-repos-INVENTORYDEMO
_cbm_project_cache: dict[str, str] = {}


def _cbm_project_name(repo_path: str) -> str:
    """Convierte 'C:/Users/demo/source/repos/INVENTORYDEMO' al nombre interno de cbm.

    Regla de cbm 0.8.1: reemplaza separadores de path por '-' (sin normalizar
    mayúsculas/minúsculas, así que respetamos el caso del repo en disco).
    """
    if repo_path in _cbm_project_cache:
        return _cbm_project_cache[repo_path]
    # Normalizar separadores: backslash -> guion.
    name = repo_path.replace("\\", "-").replace("/", "-")
    # Quitar el ':' de la unidad (C:/ -> C-).
    if len(name) >= 2 and name[1] == ":":
        name = name[0] + "-" + name[2:]
    # Quitar guiones repetidos que puedan quedar de dobles separadores.
    while "--" in name:
        name = name.replace("--", "-")
    _cbm_project_cache[repo_path] = name
    return name


# AppKeys importados top-level arriba (DB_KEY, SESSIONS_KEY).
# No usamos helper — referencias directas a los AppKey compartidos.


# ---------- assets estáticos ----------

# (mtime, html). Cachear por mtime y no "para siempre": el .js y el .css
# se sirven del disco en cada request, así que un cambio en el index.html
# obligaba a reiniciar el relay y era imposible de adivinar — editabas la
# UI, recargabas, y veías la versión vieja solo de este archivo.
ADMIN_INDEX: tuple[float, str] | None = None


async def admin_index(request: web.Request) -> web.Response:
    """Sirve el index.html del UI admin (single page app vanilla)."""
    global ADMIN_INDEX
    path = ADMIN_STATIC_DIR / "index.html"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if mtime is None:
        return web.Response(
            text="<h1>Admin UI no compilada</h1><p>Falta admin_static/index.html</p>",
            content_type="text/html",
            status=500,
        )
    if ADMIN_INDEX is None or ADMIN_INDEX[0] != mtime:
        ADMIN_INDEX = (mtime, path.read_text(encoding="utf-8"))
    # Mismo cache-control que admin_static: la UI cambia seguido,
    # no queremos servir HTML viejo si el user mantiene la tab abierta.
    resp = web.Response(text=ADMIN_INDEX[1], content_type="text/html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


async def admin_static(request: web.Request) -> web.Response:
    """Sirve JS/CSS del UI admin bajo /admin/static/*."""
    rel = request.match_info["filename"]
    if "/" in rel or "\\" in rel or ".." in rel:
        return web.Response(status=404)
    path = ADMIN_STATIC_DIR / "static" / rel
    if not path.is_file():
        return web.Response(status=404)
    ext = path.suffix.lower()
    ctype = {
        ".js": "application/javascript",
        ".css": "text/css",
        ".html": "text/html",
        ".ico": "image/x-icon",
    }.get(ext, "application/octet-stream")
    # Vendored libs (vendor-<lib>-<version>.js) no cambian con la UI:
    # cache largo para no re-bajarlas en cada F5. El cache-bust es el
    # nombre — un upgrade cambia la versión y con eso la URL, así que
    # el archivo viejo nunca se sirve stale. NO vendorizar sin versión
    # en el nombre: quedaría cacheado 24h sin forma de invalidarlo.
    # El resto va no-store abajo.
    if rel.startswith("vendor-"):
        resp = web.Response(body=path.read_bytes(), content_type=ctype)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
    # Cache-control agresivo: la UI cambia seguido (sub-olas 2.x) y un
    # browser con cache stale puede mostrar modales rotos / versiones
    # viejas del JS sin que el usuario sepa. Forzamos always-revalidate.
    resp = web.Response(body=path.read_bytes(), content_type=ctype)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ---------- endpoints API ----------

async def api_config_timeouts(request: web.Request) -> web.Response:
    """GET /admin/api/config/timeouts — timeouts efectivos (expert + tool).

    Muestra el timeout efectivo del expert (resuelto en cascada),
    los defaults de cbm MCP y los envs relevantes. Solo lectura;
    para cambiarlos usar /admin/api/config/expert-timeout (PUT).
    Renombrado desde `api_config` (2026-07-09): se separó del
    `api_config_get` (whitelist editable: RELAY_HOST, FOURBIS_REPOS_ROOT)
    para no pisar la ruta /admin/api/config — antes convivían en el
    mismo path y ganaba este por orden de registro, lo que rompía
    `test_system_config.py::test_config_get_defaults`.
    """
    db = request.app[DB_KEY]
    overrides = {}
    try:
        all_cfg = await db.all_config()
        overrides = {k: v for k, v in (all_cfg or {}).items()
                     if not k.startswith(_SECRET_PREFIX)
                     and relay_config.PANEL_SETTINGS.get(k, {}).get("type")
                     != "secret"}
    except Exception:  # noqa: BLE001
        pass

    # Lee el valor resuelto (no el default suelto), pasando el override
    # que ya tengamos en system_config.
    eff_to = relay_config.expert_timeout_s()
    eff_tool_to = relay_config.tool_timeout_s()

    return web.json_response({
        "relay_version": RELAY_VERSION,
        "expert_timeout_s": eff_to,
        "expert_timeout_default_s": float(relay_config.PANEL_SETTINGS["expert_timeout_s"]["default"]),
        "tool_timeout_s": eff_tool_to,
        "tool_timeout_default_s": 60.0,
        "overrides": overrides,
    })


async def api_config_expert_timeout(request: web.Request) -> web.Response:
    """PUT /admin/api/config/expert-timeout — setea el timeout global.

    Body: {"value": 600} (segundos, float).
    Persiste en system_config (sobrevive reinicios). Permite null
    para volver al default.
    """
    db = request.app[DB_KEY]
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body debe ser un objeto JSON"}, status=400)
    raw = body.get("value", None)
    if raw is None or raw == "":
        await db.set_config("expert_timeout_s", "")
    else:
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return web.json_response(
                {"error": "value debe ser número o null"}, status=400,
            )
        if not math.isfinite(v) or v < 30 or v > 3600:
            return web.json_response(
                {"error": "rango permitido: 30..3600 segundos"}, status=400,
            )
        await db.set_config("expert_timeout_s", str(int(v)))
    await _refresh_runtime_config(db)
    eff = float((await db.get_config("expert_timeout_s"))
                or relay_config.expert_timeout_s())
    return web.json_response({"expert_timeout_s": eff})


async def api_config_tool_timeout(request: web.Request) -> web.Response:
    """PUT /admin/api/config/tool-timeout — setea el FOURBIS_MCP_TIMEOUT.

    Body: {"value": 60} (segundos, float).
    Persiste en system_config (sobrevive reinicios). Permite null
    para volver al env (o default 60s).

    Cascada efectiva (al usar el valor):
      1. system_config.FOURBIS_MCP_TIMEOUT (este endpoint)
      2. env var FOURBIS_MCP_TIMEOUT
      3. default 60s

    Rango: 5..600s. Sub-ola 2.6.
    """
    db = request.app[DB_KEY]
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body debe ser un objeto JSON"}, status=400)
    raw = body.get("value", None)
    if raw is None or raw == "":
        await db.set_config("FOURBIS_MCP_TIMEOUT", "")
    else:
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return web.json_response(
                {"error": "value debe ser número o null"}, status=400,
            )
        if not math.isfinite(v) or v < 5 or v > 600:
            return web.json_response(
                {"error": "rango permitido: 5..600 segundos"}, status=400,
            )
        await db.set_config("FOURBIS_MCP_TIMEOUT", str(int(v)))
    # refresca el snapshot en memoria para que el siguiente run lo vea
    await _refresh_runtime_config(db)
    eff = relay_config.tool_timeout_s()
    return web.json_response({"tool_timeout_s": eff})


async def _refresh_runtime_config(db) -> None:
    """Refresca config._runtime desde db.all_config() (sin reiniciar).

    Se llama después de set_config() para que el siguiente run del
    experto vea el valor nuevo sin esperar al próximo arranque.
    """
    try:
        all_cfg = await db.all_config()
        relay_config.set_runtime_config(all_cfg or {})
    except Exception as e:  # noqa: BLE001 — best-effort, no rompemos el set
        logger.warning("refresh runtime config falló: %r", e)


async def api_bot_status(request: web.Request) -> web.Response:
    """GET /admin/api/bot/status — estado real del bot de Discord.

    Tres estados, no uno: proceso y gateway se caen por separado y se
    arreglan distinto. Ver el docstring de `bot_control`.
    """
    return web.json_response(await bot_control.probe())


async def api_bot_start(request: web.Request) -> web.Response:
    """POST /admin/api/bot/start — deja el bot conectado a Discord.

    Bloquea hasta que el gateway conecte o se acabe el timeout, así el
    botón de la UI no miente: cuando responde, el estado que devuelve es
    el que hay. 200 igual si falla — el body trae `ok:false` y el
    `detail` accionable, que es lo que la UI pinta.
    """
    return web.json_response(await bot_control.start())


async def api_health(request: web.Request) -> web.Response:
    """GET /admin/api/health — estado del relay."""
    db = request.app[DB_KEY]
    sessions = request.app[SESSIONS_KEY]
    try:
        sessions_list = await sessions.list_sessions()
        sessions_count = len(sessions_list)
    except Exception:
        sessions_count = -1

    projects = await db.list_projects(enabled_only=True)
    # health tiene que ser barato: contamos los .db del cache en vez de
    # spawnear cbm (ver _cbm_indexed_count). El detalle por proyecto
    # —nodes/edges/size— lo sigue trayendo /admin/api/projects, que se
    # pide cuando abres el tab, no cada 15s.
    indexed_count = _cbm_indexed_count()

    try:
        override_to = await db.get_config("expert_timeout_s")
    except Exception:
        override_to = None
    eff_to = float(override_to or relay_config.expert_timeout_s())

    # Autoaprendizaje: count de borradores pendientes para el badge del
    # sidebar (la UI pollea health cada 15s). Best-effort.
    try:
        drafts_pending = await db.count_skill_drafts_pending()
    except Exception:  # noqa: BLE001
        drafts_pending = 0

    return web.json_response({
        "ok": True,
        "relay_version": RELAY_VERSION,
        "projects_total": len(projects),
        "projects_indexed": indexed_count,
        "vscode_sessions_live": sessions_count,
        "cbm_binary": cbm_binary_path() is not None,
        "expert_timeout_s": eff_to,
        "skill_drafts_pending": drafts_pending,
    })


async def api_report(request: web.Request) -> web.Response:
    """GET /admin/api/report?days=N&project=slug — informe de uso.

    UI 2026-07-20: agregados de tokens/runs por día, proyecto y autor.
    El GROUP BY vive en SQL (ver Database.report_usage) porque /chats
    capea en 200 filas. Read-only.
    """
    db = request.app[DB_KEY]
    try:
        days = min(max(int(request.query.get("days", "30")), 1), 365)
    except ValueError:
        days = 30
    project = request.query.get("project") or None
    data = await db.report_usage(days, project_slug=project)
    return web.json_response(data)


# ---------- Sprint 1: Dashboard de métricas ----------

#: Estados y roles aceptados por el filtro de métricas. Lista blanca:
#: `status` viaj
#: a un `=?` parametrizado y `role` compara contra un
#: literal, pero acotarlos acá evita que un valor cualquiera devuelva
#: silenciosamente cero filas y parezca "no hubo actividad".
_METRICS_STATUSES = ("ok", "error", "running", "cancelled", "split")
_METRICS_ROLES = ("executor", "planner", "verifier", "documenter")

#: Tope de amplitud cuando el dashboard manda fechas absolutas en vez de
#: `days`. Sin esto, un rango de 5 años dispara un SELECT sobre todas
#: las filas de `chats` y se nota. 366 días cubre el peor caso honesto
#: (año bisiesto) y mantiene coherencia con el cap de 90 que ya tenía
#: `days`.
_METRICS_RANGE_MAX_DAYS = 366


def _parse_metrics_window(query: Mapping[str, str]) -> dict:
    """Resuelve la ventana de tiempo de los endpoints de métricas.

    Acepta `from`+`to` (YYYY-MM-DD) o, como fallback, `days=N`. Si vienen
    los tres o `days` mezclado con uno de los dos, gana el par
    `from`/`to` (es lo más explícito que tiene el dashboard). Devuelve
    siempre algo usable por `Database.metrics_summary/_trends`:

        {"from_date": "YYYY-MM-DD", "to_date": "YYYY-MM-DD", "days": None}
        o {"from_date": "", "to_date": "", "days": 7}

    Si el rango está invertido o la fecha no parsea, devuelve
    `{"error": web.Response(400, ...)}` listo para que el handler lo
    devuelva. No se valida acá si `to` es pasado reciente: el backend
    no tiene reloj de negocio, y "sin datos en ese día" ya es la
    respuesta correcta.
    """
    f = (query.get("from") or "").strip()
    t = (query.get("to") or "").strip()
    if f or t:
        # Si solo viene uno de los dos, el usuario a medio completar
        # inputs: mejor 400 que devolver un rango silencioso.
        if not f or not t:
            return {"error": web.json_response(
                {"ok": False, "error": "from y to deben venir juntos"},
                status=400)}
        try:
            f_date = datetime.strptime(f, "%Y-%m-%d").date()
            t_date = datetime.strptime(t, "%Y-%m-%d").date()
        except ValueError:
            return {"error": web.json_response(
                {"ok": False,
                 "error": "from/to deben ser YYYY-MM-DD"},
                status=400)}
        if t_date < f_date:
            return {"error": web.json_response(
                {"ok": False,
                 "error": "to no puede ser anterior a from"},
                status=400)}
        # Amplitud inclusiva: from=01/01 to=01/01 es 1 día, no 0. El
        # cap en días se hace comparando la diferencia de fechas; el
        # límite de 366 ya cubre el peor caso de año bisiesto.
        span = (t_date - f_date).days + 1
        if span > _METRICS_RANGE_MAX_DAYS:
            return {"error": web.json_response(
                {"ok": False,
                 "error": f"rango máximo {_METRICS_RANGE_MAX_DAYS} días"},
                status=400)}
        return {"from_date": f, "to_date": t, "days": None}
    # Fallback a `days`. Misma lista blanca que ya tenía summary: 1..90,
    # y un valor fuera de rango se ignora silenciosamente (dashboard,
    # no API de escritura). `days=0` o negativo cae al default.
    raw = (query.get("days") or "").strip()
    try:
        d = int(raw) if raw else 7
    except ValueError:
        d = 7
    d = min(max(d, 1), 90)
    return {"from_date": "", "to_date": "", "days": d}


async def api_metrics_summary(request: web.Request) -> web.Response:
    """GET /admin/api/metrics/summary — KPIs y distribuciones.

    Query: `from`/`to` (YYYY-MM-DD, rango absoluto, máximo 366 días) o
    `days` (1-90, fallback); más `project`, `status`, `provider`,
    `role`. `project`/`status` filtran runs; `provider`/`role`
    filtran turnos (ver `Database.metrics_summary`). Un valor fuera
    de la lista blanca se ignora en vez de devolver 400: es un
    dashboard, no una API de escritura, y un filtro mal tipeado no
    debería romper la pantalla.
    """
    db = request.app[DB_KEY]
    q = request.query
    win = _parse_metrics_window(q)
    if "error" in win:
        return win["error"]
    status = q.get("status", "").strip()
    role = q.get("role", "").strip()
    return web.json_response(await db.metrics_summary(
        win["days"],
        from_date=win["from_date"],
        to_date=win["to_date"],
        project=q.get("project", "").strip(),
        status=status if status in _METRICS_STATUSES else "",
        provider=q.get("provider", "").strip(),
        role=role if role in _METRICS_ROLES else "",
    ))


async def api_metrics_trends(request: web.Request) -> web.Response:
    """GET /admin/api/metrics/trends — slice diario para gráfica.

    Misma ventana que summary (`from`/`to` o `days`). Acepta los
    mismos `project`/`status` que summary, con la misma lista blanca:
    el gráfico va al lado de los KPIs y tiene que estar filtrado
    igual. `provider`/`role` no aplican (filtran turnos, no runs) y
    se ignoran.
    """
    db = request.app[DB_KEY]
    q = request.query
    win = _parse_metrics_window(q)
    if "error" in win:
        return win["error"]
    status = q.get("status", "").strip()
    return web.json_response(await db.metrics_trends(
        win["days"],
        from_date=win["from_date"],
        to_date=win["to_date"],
        project=q.get("project", "").strip(),
        status=status if status in _METRICS_STATUSES else "",
    ))


async def api_search(request: web.Request) -> web.Response:
    """GET /admin/api/search?q=foo — buscador global de la topbar.

    UI 2026-07-20: para el overlay de Cmd+K. Devuelve projects + chats +
    conversations en una sola respuesta (3 queries chiquitas en SQL).
    Cap por tipo para no explotar el payload si el usuario tipea
    poco (1-2 chars matchean miles).
    """
    q = (request.query.get("q") or "").strip()
    if len(q) < 2:
        return web.json_response({"q": q, "projects": [],
                                  "chats": [], "conversations": []})
    try:
        limit = min(max(int(request.query.get("limit", "8")), 1), 25)
    except ValueError:
        limit = 8
    db = request.app[DB_KEY]
    data = await db.global_search(q, limit=limit)
    return web.json_response(data)


# ---------- logs en vivo (UI 2026-07-20) ----------
# Ring buffer en memoria sobre el root logger: cola instantánea para la
# UI, cero disco. Desde 2026-07-25 ya NO es la única fuente — el relay
# escribe a ~/.4bis/logs/relay.log rotado (ver create_app), que es lo
# que sobrevive a un reinicio. Este buffer es solo el tail en vivo.
#
# maxlen subido de 500 a 2000: 500 líneas es menos de un run ocupado
# (el chat de 248 tool calls lo desbordaba entero y la UI mostraba solo
# el final), y 2000 entradas son ~400 KB.

_LOG_BUFFER: collections.deque = collections.deque(maxlen=2000)
_log_handler_installed = False


class _RingLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _LOG_BUFFER.append({
                "ts": time.strftime(
                    "%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage()[:500],
                # Puesto por logctx.ChatContextFilter. El getattr cubre
                # los records que entran antes de que create_app instale
                # el filter (arranque) y los de los tests.
                "chat_id": getattr(record, "chat_id", ""),
                "project": getattr(record, "project", ""),
            })
        except Exception:  # noqa: BLE001 — un log NUNCA rompe nada
            pass


def _install_ring_handler() -> None:
    global _log_handler_installed
    if _log_handler_installed:
        return
    h = _RingLogHandler(level=logging.INFO)
    # Este handler se instala DESPUÉS del bootstrap de create_app, así
    # que no lo alcanzó el loop que filtra los handlers de ahí: se lo
    # ponemos acá o el ring buffer queda sin chat_id.
    h.addFilter(logctx.ChatContextFilter())
    logging.getLogger().addHandler(h)
    _log_handler_installed = True


async def api_logs(request: web.Request) -> web.Response:
    """GET /admin/api/logs?limit=200&level=WARNING&chat=<id> — ring buffer.

    `chat` es la razón de ser de todo esto: filtrar por un run concreto
    para poder contestar "qué pasó en este chat" sin reconstruirlo a
    mano desde la DB. Acepta el id completo o el prefijo corto que
    muestra la UI.
    """
    try:
        limit = min(max(int(request.query.get("limit", "200")), 1), 2000)
    except ValueError:
        limit = 200
    level = (request.query.get("level") or "").upper()
    chat = (request.query.get("chat") or "").strip().lower()
    logs = list(_LOG_BUFFER)
    if level in ("WARNING", "ERROR"):
        keep = {"WARNING", "ERROR", "CRITICAL"} if level == "WARNING" \
            else {"ERROR", "CRITICAL"}
        logs = [l for l in logs if l["level"] in keep]
    if chat:
        logs = [l for l in logs
                if (l.get("chat_id") or "").lower().startswith(chat)]
    return web.json_response({"logs": logs[-limit:]})


async def api_projects(request: web.Request) -> web.Response:
    """GET /admin/api/projects — lista con estado de index."""
    db = request.app[DB_KEY]
    projects = await db.list_projects(enabled_only=False)
    indexed: dict[str, dict] = {}
    try:
        # Bug 2026-07-22: el listado NO puede colgarse por las stats de
        # index. El spawn de cbm se va a 20s+ cuando el watcher tiene el
        # store lockeado (ver _cbm_indexed_count) y el front corta a los
        # 15s → la sidebar de Chats quedaba en "el server no respondió"
        # y la lista de conversaciones ni se pedía. Degradamos a lo
        # último cacheado y seguimos: el resto del payload es sqlite.
        try:
            cbm_resp = await asyncio.wait_for(
                _cbm_list_projects(), timeout=_CBM_PROJECTS_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(
                "admin/projects: cbm tardó >%ss (store lockeado?), sirvo "
                "sin stats de index", _CBM_PROJECTS_TIMEOUT_S)
            cbm_resp = _cbm_projects_cache or {}
        for c in cbm_resp.get("projects", []):
            # Match por root_path completo normalizado, NO por basename:
            # dos repos llamados igual en carpetas distintas colisionan.
            root = _norm_path_key(c.get("root_path", ""))
            if root:
                indexed[root] = {
                    "nodes": c.get("nodes"),
                    "edges": c.get("edges"),
                    "size_bytes": c.get("size_bytes"),
                }
    except Exception as e:  # noqa: BLE001
        logger.warning("admin/projects: cbm fail: %r", e)

    # Cliente CRM de cada fila. `_parse_project` ya trae `client_id`
    # (hace SELECT *), pero este listado lo descartaba al armar la
    # respuesta, así que la grid no tenía forma de mostrar el dueño del
    # proyecto. Los nombres se resuelven en un solo SELECT y no de a uno.
    client_names = await db.client_name_by_id()

    out = []
    for p in projects:
        rp = p.get("repo_path", "")
        idx = indexed.get(_norm_path_key(rp), {})
        cid = p.get("client_id")
        out.append({
            "slug": p["slug"],
            "name": p["name"],
            "client_id": cid,
            "client_name": client_names.get(cid) if cid else None,
            "repo_path": rp,
            "enabled": bool(p.get("enabled", 1)),
            "include_in_index": bool(p.get("include_in_index", 1)),
            "description": p.get("description"),
            "indexed": bool(idx),
            "index_stats": idx or None,
            "native_tools": p.get("native_tools", []),
            "has_git": _has_git(Path(rp)) if rp else False,
            "git_remote_url": _git_remote_url(rp) if rp else None,
            "night_mode_enabled": bool(p.get("night_mode_enabled", 0)),
            # El listado lo omitía → la grid mostraba "sin canal" aunque
            # estuviera seteado (el detalle sí lo devolvía). La columna 📡
            # lo lee de acá.
            "discord_channel_id": p.get("discord_channel_id"),
            # Tablero vinculado (ADR-036). Va en el listado y no en una
            # request aparte: con 51 proyectos, saber cuáles tienen
            # tablero no puede costar 51 fetches. Sale de defaults_json,
            # que ya está en memoria acá.
            "github_project": (p.get("defaults_json") or {}).get(
                "github_project"),
            # Marcado como "nunca va a tener tablero": sale de la lista
            # de pendientes del tab Gestión sin desaparecer del sistema.
            "github_project_skip": bool(
                (p.get("defaults_json") or {}).get("github_project_skip")),
        })
    # discord_guild_id (system_config): el front lo usa para linkear el
    # canal de cada fila. Va una vez a nivel top, no por proyecto.
    return web.json_response({
        "projects": _serialize(out),
        "discord_guild_id": relay_config.discord_guild_id(),
    })


async def api_projects_slug(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug} — detalle."""
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    data = dict(project)
    rp = project.get("repo_path", "")
    data["git_remote_url"] = _git_remote_url(rp)
    # has_git: el detalle lo omitía (solo lo mandaba el listado), así que
    # el panel de admin no sabía si el repo era git → la sección de remoto
    # caía en "no es repo git" aunque lo fuera.
    data["has_git"] = _has_git(Path(rp)) if rp else False
    return web.json_response({"project": _serialize(data)})


async def api_project_git_remote(request: web.Request) -> web.Response:
    """PUT /admin/api/projects/{slug}/git-remote {url} — vincula o cambia
    el remote `origin` del repo local (git remote set-url/add).

    Solo GitHub: validamos que la URL parsee a owner/repo (https o ssh)
    porque el panel de seguimiento deriva de ahí y un typo dejaría el
    origin roto sin aviso. La escritura vive en github.set_remote.
    """
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    url = (body.get("url") or "").strip()
    if not url:
        return web.json_response({"error": "url requerida"}, status=400)
    repo = github_mod._parse_remote(url)
    if repo is None:
        return web.json_response(
            {"error": "la URL no parece un repo de GitHub "
             "(https://github.com/owner/repo o git@github.com:owner/repo)"},
            status=400)
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    repo_path = project.get("repo_path") or ""
    if not _has_git(Path(repo_path)):
        return web.json_response(
            {"error": "el proyecto no es un repo git (no tiene .git); "
             "inicializá git primero"}, status=409)
    ok, motivo = await github_mod.set_remote(repo_path, url)
    if not ok:
        return web.json_response(
            {"error": f"git remote falló: {motivo}" if motivo
             else "git remote falló"}, status=500)
    # Invalidar el cache del read-path (mtime del .git/config puede no
    # cambiar de forma perceptible entre lecturas rápidas).
    _remote_url_cache.pop(repo_path, None)
    return web.json_response({"ok": True, "url": url, "repo": repo})


async def api_project_system_prompt(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug}/system-prompt — system_prompt efectivo.

    Reproduce EXACTAMENTE lo que el LLM vio en su `instructions=`:
    1. Ponytail (filosofía del usuario, best-effort)
    2. projects.system_prompt (lo específico del repo)
    3. Índice de skills (ADR-010, vía SkillCache)
    4. Bloque workspace (ADR-011, desde repo_path)
    5. Bloque git diff (ADR-020, si repo_path es git) — capturado en
       to_thread para no bloquear el loop

    Devuelve los bloques por separado + el texto ensamblado, para que
    la UI pueda colapsar/copiar cada parte individualmente. El git diff
    puede ser pesado (hasta 20KB); mandarlo separado permite que el front
    muestre un preview + "ver completo" sin tragar el body entero.

    Sub-ola 2.3 — server-side replica la lógica de experts.run_expert.
    """
    from . import experts, sessions
    from .skills import SkillCache

    db = request.app[DB_KEY]
    skills: SkillCache = request.app[SKILLS_KEY]
    slug = request.match_info["slug"]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)

    # Reusamos exactamente las mismas primitivas que run_expert.
    # Si cambian allá, este endpoint queda desfasado — pero el bloque
    # ya está testeado (test_experts) y el contrato está documentado
    # en ADR-020 + comments de build_instructions.
    ponytail = await experts.read_ponytail()
    defaults = project.get("defaults_json") or {}
    skills_mode = defaults.get("skills_mode", "embed")
    skills_block = ""
    try:
        if skills_mode == "compact":
            from .skills import _build_index, render_index_compact, resolve_skills_dir
            dir_ = resolve_skills_dir()
            idx = _build_index(dir_)
            # render_index_compact ya devuelve "" si no hay nada; el
            # guard extra por auto_skills() escondía el preview cuando
            # todas las skills eran on-demand (`when: manual`).
            skills_block = render_index_compact(idx)
        else:
            skills_block = await skills.get_block()
    except Exception as e:  # noqa: BLE001 — best-effort, mismo fallback que _run_expert_bg
        logger.warning("system-prompt: skills.get_block rompio: %r", e)
    workspace_block = sessions.extract_workspace_block(
        [{"path": project["repo_path"], "name": project["slug"]}]
    )

    # 2026-07-28: el bloque de git diff YA NO va en el system prompt
    # (rompía la cache del provider en cada resume — ver
    # `build_instructions`). El experto lo pide con la tool `git_diff()`,
    # así que este preview no lo muestra: mostraría un bloque que el
    # modelo no recibe.
    blocks = {
        "ponytail": ponytail or "",
        "system_prompt": project.get("system_prompt", "") or "",
        "skills": skills_block or "",
        "workspace": workspace_block or "",
        # 2026-07-20: build_instructions cierra con el bloque de
        # fallback de tools; sin él este replica queda desfasado.
        "tool_fallback": experts.TOOL_FALLBACK_BLOCK,
        # 2026-08-17: idem con la regla de lotes. El test de
        # replicabilidad de test_system_prompt.py es el que avisa cuando
        # este dict se queda atrás de build_instructions.
        "batch_artifacts": experts.BATCH_ARTIFACTS_BLOCK,
        # 2026-08-17: la bitácora. Acá va solo el bloque de
        # instrucciones (el que se paga siempre); los hechos que el run
        # va anotando viajan aparte, en las instructions dinámicas.
        "bitacora": experts.BITACORA_BLOCK,
    }
    assembled = "\n\n".join(v for v in blocks.values() if v)
    return web.json_response({
        "slug": slug,
        "blocks": blocks,
        "assembled": assembled,
        "stats": {k: len(v) for k, v in blocks.items()} | {"total": len(assembled)},
    })


async def api_project_night(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug}/night — estado del modo nocturno.

    Devuelve el flag + config + últimas corridas (tabla night_runs) para
    que el tab Proyectos muestre el estado sin arrancar nada. El
    start/stop/status en vivo van por los endpoints raíz /night-mode/*
    (ADR-028) que UI y CLI comparten.
    """
    from . import night as night_mod

    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    cfg = night_mod.NightConfig.from_project(project)
    runs = await db.list_night_runs(project["slug"], limit=5)
    active = next((r for r in runs if not r.get("ended_at")), None)
    # Enriquecer el run activo con el snapshot VIVO del orquestador en
    # memoria (current_task + bloque `live`: step/idle_s/tool del experto).
    # La fila de la DB sólo tiene id/deadline; la observabilidad async
    # vive en el registry NIGHT_KEY. Sin esto el modal 🌙 no muestra en
    # qué anda el run mientras corre (que es cuando se mira de noche).
    if active is not None:
        registry = request.app.get(NIGHT_KEY) or {}
        entry = registry.get(active["id"])
        if entry is not None:
            try:
                active = {**active, **entry[0].snapshot()}
            except Exception:  # noqa: BLE001 — el modal no debe romperse
                pass
    return web.json_response({
        "slug": project["slug"],
        "night_mode_enabled": bool(project.get("night_mode_enabled")),
        "config": {
            "build_cmd": cfg.build_cmd,
            "test_cmd": cfg.test_cmd,
            "max_diff_lines": cfg.max_diff_lines,
            "discord_channel": cfg.discord_channel,
            "base_branch": cfg.base_branch,
        },
        "active_run": active,
        "runs": runs,
    })


async def api_project_cbm(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug}/cbm — drilldown del índice cbm.

    Devuelve las stats del proyecto en cbm (nodes/edges/size_bytes)
    más un sample de tipos de nodos y el nombre canónico que cbm le
    da al proyecto (útil para debuggear el mapeo repo_path → cbm name).
    La UI usa esto para mostrar el modal "📊 cbm" con drilldown +
    botón "reindex" que dispara POST /admin/api/projects/{slug}/reindex.
    """
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    rp = project["repo_path"]
    cbm_name = _cbm_project_name(rp) if rp else ""
    installed = cbm_binary_path() is not None

    stats: dict = {}
    node_types: list[dict] = []
    note = ""
    if installed and rp:
        try:
            data = await _cbm_list_projects()
            norm = _norm_path_key(rp)
            for c in data.get("projects", []):
                if _norm_path_key(c.get("root_path", "")) == norm:
                    stats = {
                        "nodes": c.get("nodes"),
                        "edges": c.get("edges"),
                        "size_bytes": c.get("size_bytes"),
                    }
                    break
        except Exception as e:  # noqa: BLE001
            note = f"cbm cli error: {e!r}"
    return web.json_response({
        "slug": slug,
        "repo_path": rp,
        "cbm_project_name": cbm_name,
        "installed": installed,
        "stats": stats,
        "node_types": node_types,
        "note": note,
    })


async def api_facts_list(request: web.Request) -> web.Response:
    """GET /admin/api/conversations/facts?project=&limit= — facts del proyecto.

    Lista los hechos atómicos destilados al cerrar conversaciones
    (ADR-026). Append-only — no hay DELETE desde la UI; si quieres
    sacar algo, lo haces a mano en SQLite.

    Sub-ola 2.4 — para el panel de hechos del proyecto en la Admin UI.
    """
    db = request.app[DB_KEY]
    project_slug = request.query.get("project")
    if not project_slug:
        return web.json_response(
            {"error": "project requerido"}, status=400)
    try:
        limit = min(int(request.query.get("limit", "100")), 500)
    except (TypeError, ValueError):
        limit = 100
    status = (request.query.get("status") or "").strip() or None
    if status is not None and status not in db.FACT_STATES:
        return web.json_response(
            {"error": f"status debe ser uno de {list(db.FACT_STATES)}"},
            status=400)
    facts = await db.list_facts(project_slug, limit=limit, status=status)
    # El contador de pendientes viaja SIEMPRE, mires el filtro que mires:
    # una cola de aprobación que no se ve es una cola que no se atiende
    # (los 13 borradores de skills, el más viejo de hace 25 días).
    pendientes = len(await db.list_facts(
        project_slug, limit=500, status="pending"))
    return web.json_response({
        "project": project_slug, "facts": facts,
        "pendientes": pendientes,
    })


async def api_project_git_diff(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug}/git-diff — diff sin procesar.

    Devuelve el JSON de `_capture_git_diff_sync` con cap defensivo:
        {ok, status, diff, sha, branch, truncated?, full_size?, stderr}

    Cambio 2026-07-08 (bug fix Sub-ola 2.7): antes el response mandaba
    el diff ENTERO (hasta 20KB) al cliente y la UI lo metía en un <pre>.
    En repos con muchos archivos modificados eso colgaba el browser
    y acumulaba memoria por cada apertura del modal. Ahora capamos
    el response del endpoint a un tamaño razonable (default 64KB
    total del JSON, override con `?max_kb=N`) y siempre devolvemos
    `full_size` para que la UI sepa cuánto se cortó.

    - `ok: false, status: "not_git_repo"` → repo no es git
    - `truncated: true` → diff cortado por el cap interno del experto
      (DIFF_MAX_BYTES=20KB) o por el cap del endpoint (?max_kb=N)
    - `full_size`: tamaño real del diff (en chars), para UI info
    - `stderr`: warnings de git separados del diff (no se renderizan
      por default; quedan en la respuesta para diagnóstico si ?debug=1)
    """
    db = request.app[DB_KEY]
    from . import experts as relay_experts
    slug = request.match_info["slug"]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    info = await asyncio.to_thread(
        relay_experts._capture_git_diff_sync, project["repo_path"])
    full_diff = info.get("diff", "") or ""
    full_size = len(full_diff)
    out = {
        "slug": slug,
        "ok": bool(info.get("ok")),
        "status": info.get("status", ""),
        "diff": full_diff,
        "sha": info.get("sha", ""),
        "branch": info.get("branch", ""),
        "full_size": full_size,
    }
    # cap defensivo a nivel de endpoint (default 64KB, ajustable).
    # Evita que un diff enorme cuelgue el browser. El experto ya capa
    # a DIFF_MAX_BYTES internamente; este cap es la segunda barrera.
    try:
        max_kb = int(request.query.get("max_kb", "64"))
    except (TypeError, ValueError):
        max_kb = 64
    max_chars = max_kb * 1024
    if len(out["diff"]) > max_chars:
        # truncar en frontera de línea para no cortar mid-line
        cut = out["diff"][:max_chars]
        last_nl = cut.rfind("\n")
        if last_nl > 0:
            cut = cut[:last_nl]
        out["diff"] = cut
        out["truncated"] = True
    elif full_size > relay_experts.DIFF_MAX_BYTES:
        out["truncated"] = True
    # stderr solo si se pide explícitamente (sirve para debug)
    if request.query.get("debug") == "1":
        out["stderr"] = info.get("stderr", "")
    return web.json_response(out)


async def api_memories_search(request: web.Request) -> web.Response:
    """GET /admin/api/conversations/memories?project=&q=&limit=.

    Búsqueda FTS5 sobre los resúmenes de conversaciones cerradas
    (ADR-027). Scoped a un proyecto. Sin query, devuelve los
    resúmenes más recientes del proyecto.

    Devuelve además `fts_available` para que la UI sepa si mostrar
    el input de búsqueda o un warning de "FTS5 no compilado".

    Sub-ola 2.5 — para la búsqueda en memoria de la Admin UI.
    """
    db = request.app[DB_KEY]
    project_slug = request.query.get("project")
    if not project_slug:
        return web.json_response(
            {"error": "project requerido"}, status=400)
    q = request.query.get("q") or ""
    try:
        limit = min(int(request.query.get("limit", "5")), 50)
    except (TypeError, ValueError):
        limit = 5
    hits = await db.search_memories(project_slug, q, limit=limit)
    return web.json_response({
        "project": project_slug,
        "query": q,
        "hits": hits,
        "fts_available": bool(db._fts_available),
    })


async def api_fact_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/facts/{id} — curación de facts (2026-07-12).

    La tabla sigue append-only para el compactador; esto es el botón
    de "sacar un fact viejo o mal destilado" sin abrir SQLite a mano.
    """
    db = request.app[DB_KEY]
    try:
        fact_id = int(request.match_info["id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "id inválido"}, status=400)
    if not await db.delete_fact(fact_id):
        return web.json_response({"error": "no existe"}, status=404)
    return web.json_response({"deleted": True, "id": fact_id})


async def api_fact_status(request: web.Request) -> web.Response:
    """PATCH /admin/api/facts/{id} — aprobar o rechazar un hecho.

    Aprobación estricta (2026-08-21, pedido del usuario): el compactador
    escribe `pending` y el hecho NO entra al prompt del experto hasta
    que pasa por acá. `rejected` se conserva en la tabla en vez de
    borrarse: sirve para ver qué destila mal el compactador. Para que
    desaparezca del todo está el DELETE.
    """
    db = request.app[DB_KEY]
    try:
        fact_id = int(request.match_info["id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "id inválido"}, status=400)
    body = await request.json()
    status = (body.get("status") or "").strip()
    if status not in db.FACT_STATES:
        return web.json_response(
            {"error": f"status debe ser uno de {list(db.FACT_STATES)}"},
            status=400)
    if not await db.set_fact_status(fact_id, status):
        return web.json_response({"error": "no existe"}, status=404)
    return web.json_response({"id": fact_id, "status": status})


async def api_fact_create(request: web.Request) -> web.Response:
    """POST /admin/api/conversations/facts — alta manual de un hecho.

    Nace `approved`: lo escribió una persona, no hay a quién pedirle
    aprobación. Es la vía para meter un dato que el compactador nunca va
    a destilar solo (una credencial de demo, un puerto, una convención
    del equipo).
    """
    db = request.app[DB_KEY]
    body = await request.json()
    project_slug = (body.get("project") or "").strip()
    fact = (body.get("fact") or "").strip()
    if not project_slug:
        return web.json_response({"error": "project requerido"}, status=400)
    if not fact:
        return web.json_response({"error": "fact vacío"}, status=400)
    if len(fact) > 2000:
        return web.json_response(
            {"error": "fact demasiado largo (máx 2000 chars)"}, status=400)
    if await db.get_project(project_slug) is None:
        return web.json_response(
            {"error": f"proyecto {project_slug!r} no existe"}, status=404)
    await db.add_facts(project_slug, [fact], status="approved")
    return web.json_response({"created": True, "project": project_slug})


async def api_conversation_extract_facts(request: web.Request) -> web.Response:
    """POST /admin/api/conversations/{conv_id}/extract-facts.

    Destila hechos del hilo SIN cerrarlo ni compactarlo. Existe porque
    los dos caminos que ya había hacen de más: `/close` cierra la
    conversación y `/compact` además **recorta el historial**, o sea que
    pedir "sacá los hechos de esto" costaba perder el detalle de los
    turnos viejos. Acá el hilo queda exactamente como estaba.

    Los hechos entran como `pending`, igual que los del compactador: que
    lo haya disparado un humano no significa que el LLM haya destilado
    bien.
    """
    db = request.app[DB_KEY]
    conv_id = request.match_info["conv_id"]
    conv = await db.get_conversation(conv_id)
    if conv is None:
        return web.json_response({"error": "no existe"}, status=404)
    messages_json = conv.get("messages_json") or ""
    if not messages_json:
        return web.json_response(
            {"error": "la conversación no tiene turnos todavía"}, status=400)
    # Los vigentes van al compactador para que no re-emita duplicados ni
    # se pise con lo que ya está esperando revisión (por eso sin filtro
    # de status: un `pending` duplicado sigue siendo un duplicado).
    existing = await db.list_facts(conv["project_slug"], limit=100)
    try:
        result = await memory.compact_conversation(
            messages_json, existing_facts=existing)
    except Exception as e:  # noqa: BLE001 — el compactador es un LLM
        logger.warning("extract-facts conv=%s falló (%r)", conv_id[:8], e)
        return web.json_response(
            {"error": f"el compactador falló: {type(e).__name__}"}, status=502)
    if result is None:
        return web.json_response(
            {"error": "no se pudo destilar nada del hilo"}, status=422)
    n = await db.add_facts(
        conv["project_slug"], result.facts, source_conversation=conv_id)
    # El summary se descarta a propósito: esto NO es compactar. Pisar el
    # resumen del hilo desde un botón que dice "extraer hechos" sería
    # hacer algo que nadie pidió.
    return web.json_response({
        "created": n, "pending": n,
        "project": conv["project_slug"],
        "facts": result.facts,
    })


async def api_memory_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/conversations/{conv_id}/memory — saca una
    memoria del retrieval (FTS5 + summary). El historial de la
    conversación queda intacto; solo deja de aparecer en `memoria`
    y en el campo memory de /experts/run.
    """
    db = request.app[DB_KEY]
    conv_id = request.match_info["conv_id"]
    if not await db.delete_memory(conv_id):
        return web.json_response(
            {"error": "no existe o no tiene summary"}, status=404)
    return web.json_response({"deleted": True, "conversation_id": conv_id})


# ---------- skills (autoaprendizaje 2026-07-12) ----------
# Instaladas = ~/.copilot/skills (fuente canónica, ADR-010), vía helpers
# sync de skills.py en to_thread. Borradores = tabla skill_drafts que
# llena el compactador; aprobar escribe el SKILL.md y el SkillCache lo
# inyecta en el próximo push (invalidate() salta el TTL de 60s).


async def api_skills_list(request: web.Request) -> web.Response:
    """GET /admin/api/skills — skills instaladas con metadata."""
    from . import skills as skills_mod
    cache = request.app[SKILLS_KEY]
    items = await asyncio.to_thread(skills_mod.list_skills_sync, cache.dir)
    return web.json_response({"dir": str(cache.dir), "skills": items})


async def api_skills_get(request: web.Request) -> web.Response:
    """GET /admin/api/skills/{name} — SKILL.md completo de una instalada."""
    from . import skills as skills_mod
    cache = request.app[SKILLS_KEY]
    name = request.match_info["name"]
    content = await asyncio.to_thread(
        skills_mod.read_skill_sync, cache.dir, name)
    if content is None:
        return web.json_response({"error": "no existe"}, status=404)
    return web.json_response({"name": name, "content": content})


async def api_skills_put(request: web.Request) -> web.Response:
    """PUT /admin/api/skills/{name} {content} — reescribe el SKILL.md.

    El humano edita el archivo ENTERO (frontmatter incluido) desde la
    UI: era lo único que faltaba para poder recortar una descripción
    gorda —que es lo que el propio medidor de presupuesto te pide— sin
    abrir el disco a mano.

    Deja `SKILL.md.bak`. Devuelve `valid` para que la UI avise si el
    frontmatter quedó roto (una skill inválida NO se inyecta).
    """
    from . import skills as skills_mod
    cache = request.app[SKILLS_KEY]
    name = request.match_info["name"]
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    content = body.get("content") if isinstance(body, dict) else None
    if not isinstance(content, str) or not content.strip():
        return web.json_response({"error": "falta `content` (string no vacío)"},
                                 status=400)
    ok = await asyncio.to_thread(
        skills_mod.overwrite_skill_sync, cache.dir, name, content)
    if ok is None:
        return web.json_response({"error": "no existe"}, status=404)
    cache.invalidate()
    # Releer por el mismo camino que usa el runtime: si el frontmatter
    # quedó roto, la skill desaparece del listado y hay que decirlo.
    items = await asyncio.to_thread(skills_mod.list_skills_sync, cache.dir)
    row = next((s for s in items if s.get("dir") == name), None)
    return web.json_response({
        "name": name, "saved": True,
        "valid": bool(row and row.get("valid")),
        "state": (row or {}).get("state"),
        "tokens": skills_mod.estimate_tokens(content),
        "bullet_tokens": (row or {}).get("bullet_tokens"),
    })


async def api_skills_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/skills/{name} — borra el directorio de la skill.

    Hard delete del FS (no hay papelera). La UI pide confirm() antes.
    """
    from . import skills as skills_mod
    cache = request.app[SKILLS_KEY]
    name = request.match_info["name"]
    ok = await asyncio.to_thread(
        skills_mod.delete_skill_sync, cache.dir, name)
    if not ok:
        return web.json_response({"error": "no existe"}, status=404)
    cache.invalidate()
    return web.json_response({"deleted": True, "name": name})


async def api_skills_patch(request: web.Request) -> web.Response:
    """PATCH /admin/api/skills/{name} {state} — cambia el estado.

    `state` ∈ auto | manual | off. Escribe `enabled:`/`when:` en el
    frontmatter del SKILL.md:
      auto   → entra al bloque de skills del system prompt
      manual → on-demand: se lista pero se dispara a mano
      off    → ni se inyecta ni se ofrece (el archivo queda)

    Acepta también `{enabled: bool}` como atajo (no toca `when`).
    """
    from . import skills as skills_mod
    cache = request.app[SKILLS_KEY]
    name = request.match_info["name"]
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body debe ser un objeto"},
                                 status=400)

    if "state" in body:
        state = str(body["state"])
        if state not in skills_mod.SKILL_STATES:
            return web.json_response(
                {"error": "`state` debe ser "
                          + " | ".join(skills_mod.SKILL_STATES)},
                status=400)
        ok = await asyncio.to_thread(
            skills_mod.set_skill_state_sync, cache.dir, name, state)
    elif "enabled" in body:
        state = "auto" if body["enabled"] else "off"
        ok = await asyncio.to_thread(
            skills_mod.set_skill_enabled_sync, cache.dir, name,
            bool(body["enabled"]))
    else:
        return web.json_response(
            {"error": "falta `state` (auto|manual|off) o `enabled` (bool)"},
            status=400)

    if not ok:
        return web.json_response(
            {"error": "no existe o su SKILL.md no tiene frontmatter"},
            status=404)
    cache.invalidate()
    return web.json_response({"name": name, "state": state})


async def api_skills_budget(request: web.Request) -> web.Response:
    """GET /admin/api/skills/budget[?project=slug] — cuánto pesa el prompt fijo.

    Mide lo que REALMENTE se manda en cada push, no una estimación
    teórica: el ponytail (copilot-instructions.md), el bloque de skills
    que arma render_block(), y el bloque de fallback de tools. Devuelve
    tokens estimados + los umbrales para que la UI pinte la barra.

    `project` (2026-09-08) es lo que mantiene cierta esa primera frase.
    Desde que el bloque de skills se arma por proyecto —las del repo
    entran siempre, y `defaults_json.skills` filtra las globales— un
    único número global dejó de describir lo que recibe cualquier
    proyecto en particular. Sin el parámetro se mide el bloque global,
    como antes; `scope` en la respuesta dice cuál de los dos se midió,
    para que la UI nunca muestre un número sin decir de qué es.
    """
    from . import skills as skills_mod
    from .experts import (
        BATCH_ARTIFACTS_BLOCK, BITACORA_BLOCK, TOOL_FALLBACK_BLOCK,
        read_ponytail)
    cache = request.app[SKILLS_KEY]

    try:
        ponytail = await read_ponytail()
    except Exception as e:  # noqa: BLE001
        logger.warning("budget: read_ponytail rompió: %r", e)
        ponytail = ""
    slug = (request.query.get("project") or "").strip()
    proyecto = None
    if slug:
        proyecto = await request.app[DB_KEY].get_project(slug)
        if proyecto is None:
            return web.json_response(
                {"error": f"no existe el proyecto {slug}"}, status=404)

    scope = "global"
    dirs_medidos = [cache.dir]
    if proyecto is not None:
        repo = proyecto.get("repo_path") or ""
        filtro = (proyecto.get("defaults_json") or {}).get("skills")
        try:
            skills_block = await asyncio.to_thread(
                skills_mod.bloque_del_proyecto, repo, filtro)
            dirs_medidos = await asyncio.to_thread(
                skills_mod.skills_dirs, repo)
            scope = proyecto.get("slug") or slug
        except Exception as e:  # noqa: BLE001 — medir no puede romper el panel
            logger.warning("budget: bloque de %s rompió: %r", slug, e)
            proyecto = None
    if proyecto is None:
        try:
            skills_block = await cache.get_block()
        except Exception as e:  # noqa: BLE001
            logger.warning("budget: get_block rompió: %r", e)
            skills_block = ""

    # El ranking tiene que salir de los MISMOS directorios que el bloque
    # medido: con el global, recortar la que "más pesa" podía apuntar a
    # una skill que ese proyecto ni siquiera recibe.
    items: list = []
    vistos: set = set()
    for d in dirs_medidos:
        for it in await asyncio.to_thread(skills_mod.list_skills_sync, d):
            clave = (it.get("name") or "").strip().lower()
            if clave and clave in vistos:
                continue
            vistos.add(clave)
            items.append(it)
    on = [s for s in items if s["enabled"] and not s["manual"]]
    est = skills_mod.estimate_tokens
    parts = {
        "ponytail": est(ponytail),
        "skills": est(skills_block),
        "tool_fallback": est(TOOL_FALLBACK_BLOCK),
        # El medidor de presupuesto tiene que contar TODO lo que viaja en
        # cada turno, o subestima justo lo que se paga siempre.
        "batch_artifacts": est(BATCH_ARTIFACTS_BLOCK),
        "bitacora": est(BITACORA_BLOCK),
    }
    return web.json_response({
        "parts": parts,
        "total": sum(parts.values()),
        "skills_budget": skills_mod.skills_token_budget(),
        "prompt_budget": skills_mod.prompt_token_budget(),
        # `off` (apagadas a mano) y `manual` (on-demand) se cuentan por
        # separado: `off` no entra al prompt de ninguna forma; `manual`
        # entra solo como nombre (~3 tokens, ver render_ondemand_line),
        # sin la descripción que cuenta `bullet_tokens`.
        "counts": {
            "installed": len(items), "injected": len(on),
            "off": sum(1 for s in items if not s["enabled"]),
            "manual": sum(1 for s in items if s["enabled"] and s["manual"]),
        },
        # Ranking para saber a quién recortar primero.
        "top": sorted(
            ({"name": s["name"], "dir": s["dir"],
              "tokens": s["bullet_tokens"]} for s in on),
            key=lambda s: s["tokens"], reverse=True)[:10],
        "estimator": "chars/4 (aproximado)",
        # Qué se midió. La UI lo muestra: un número de presupuesto sin
        # decir de qué proyecto es, es un número que no se puede usar.
        "scope": scope,
    })


# ---------- copilot-instructions.md (el "ponytail" que se inyecta) ----------

async def api_instructions_get(request: web.Request) -> web.Response:
    """GET /admin/api/instructions — el archivo que lee read_ponytail()."""
    from . import skills as skills_mod
    from .experts import PONYTAIL_PATH
    try:
        content = await asyncio.to_thread(
            PONYTAIL_PATH.read_text, "utf-8")
    except OSError as e:
        return web.json_response(
            {"path": str(PONYTAIL_PATH), "content": "", "exists": False,
             "error": str(e), "tokens": 0})
    return web.json_response({
        "path": str(PONYTAIL_PATH), "content": content, "exists": True,
        "tokens": skills_mod.estimate_tokens(content),
    })


async def api_instructions_put(request: web.Request) -> web.Response:
    """PUT /admin/api/instructions {content} — reescribe el ponytail.

    Deja un `.bak` con la versión anterior antes de pisar (este archivo
    va en el system prompt de CADA push; un guardado en falso sin copia
    se paga caro). El cache de read_ponytail() es por mtime, así que el
    cambio entra solo en el próximo run.
    """
    from . import skills as skills_mod
    from .experts import PONYTAIL_PATH
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    content = body.get("content") if isinstance(body, dict) else None
    if not isinstance(content, str):
        return web.json_response({"error": "falta `content` (string)"},
                                 status=400)

    def _write() -> None:
        PONYTAIL_PATH.parent.mkdir(parents=True, exist_ok=True)
        if PONYTAIL_PATH.is_file():
            shutil.copy2(PONYTAIL_PATH,
                         PONYTAIL_PATH.with_suffix(".md.bak"))
        PONYTAIL_PATH.write_text(content, encoding="utf-8")

    try:
        await asyncio.to_thread(_write)
    except OSError as e:
        return web.json_response({"error": f"no pude escribir: {e}"},
                                 status=500)
    return web.json_response({
        "path": str(PONYTAIL_PATH), "saved": True,
        "backup": str(PONYTAIL_PATH.with_suffix(".md.bak")),
        "tokens": skills_mod.estimate_tokens(content),
    })


# ---------- alta de skills desde GitHub ----------
# Espejo del pipeline de MCPs (POST /mcp/install → poll → confirm) pero
# sin ejecutar nada: clonar → listar SKILL.md → tildar → copiar.

async def api_skills_catalog(request: web.Request) -> web.Response:
    """GET /admin/api/skills/catalog — repos sugeridos para el dropdown."""
    from . import skills as skills_mod
    return web.json_response({"catalog": skills_mod.GITHUB_CATALOG})


async def api_skills_browse_start(request: web.Request) -> web.Response:
    """POST /admin/api/skills/browse {url} — clona y escanea en background."""
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    url = (body.get("url") or "").strip() if isinstance(body, dict) else ""
    if not url:
        return web.json_response(
            {"error": "url requerida (https://github.com/owner/repo)"},
            status=400)
    browser = request.app[SKILL_BROWSER_KEY]
    try:
        job = await browser.start(url)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response(job.to_public(), status=202)


async def api_skills_browse_status(request: web.Request) -> web.Response:
    """GET /admin/api/skills/browse/{job_id} — estado + skills encontradas."""
    browser = request.app[SKILL_BROWSER_KEY]
    job = browser.get(request.match_info["job_id"])
    if job is None:
        return web.json_response(
            {"error": "job no encontrado (¿expiró? vuelve a clonar)"},
            status=404)
    return web.json_response(job.to_public())


async def api_skills_browse_preview(request: web.Request) -> web.Response:
    """GET /admin/api/skills/browse/{job_id}/preview?path=… — SKILL.md crudo.

    Leer antes de instalar es la única defensa real contra un SKILL.md
    hostil: no ejecuta nada, pero entra al system prompt.
    """
    from . import skills as skills_mod
    browser = request.app[SKILL_BROWSER_KEY]
    job = browser.get(request.match_info["job_id"])
    if job is None:
        return web.json_response({"error": "job no encontrado"}, status=404)
    rel = request.query.get("path", "")
    content = await asyncio.to_thread(
        skills_mod.read_skill_md_from_clone, Path(job.clone_dir), rel)
    if content is None:
        return web.json_response({"error": "ruta inválida"}, status=404)
    return web.json_response({"path": rel, "content": content})


async def api_skills_browse_install(request: web.Request) -> web.Response:
    """POST /admin/api/skills/browse/{job_id}/install {paths[], overwrite}

    Copia las skills tildadas a ~/.copilot/skills. Best-effort por item:
    una que falle no aborta las demás, se reporta en `errors`.
    """
    from . import skills as skills_mod
    browser = request.app[SKILL_BROWSER_KEY]
    cache = request.app[SKILLS_KEY]
    job = browser.get(request.match_info["job_id"])
    if job is None:
        return web.json_response(
            {"error": "job no encontrado (¿expiró? vuelve a clonar)"},
            status=404)
    if job.state != "ready":
        return web.json_response(
            {"error": f"job en estado {job.state!r}; espera al scan"},
            status=409)
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    paths = body.get("paths") if isinstance(body, dict) else None
    if not isinstance(paths, list) or not paths:
        return web.json_response({"error": "falta `paths` (lista)"},
                                 status=400)
    overwrite = bool(body.get("overwrite"))
    # Instalar apagadas es la opción segura para tandas grandes: entran
    # al disco pero no al prompt hasta que las prendas una por una.
    enabled = bool(body.get("enabled", True))

    installed: list[str] = []
    errors: list[dict] = []

    def _install_all() -> None:
        for rel in paths:
            if not isinstance(rel, str):
                continue
            try:
                name = skills_mod.install_skill_from_clone(
                    Path(job.clone_dir), rel, cache.dir,
                    overwrite=overwrite, enabled=enabled)
                installed.append(name)
            except FileExistsError as e:
                errors.append({"path": rel,
                               "error": f"ya existe una skill '{e}' "
                                        "(marca sobrescribir)"})
            except (ValueError, OSError) as e:
                errors.append({"path": rel, "error": str(e)})

    await asyncio.to_thread(_install_all)
    if installed:
        cache.invalidate()
    return web.json_response({"installed": installed, "errors": errors,
                              "enabled": enabled})


async def api_skills_browse_discard(request: web.Request) -> web.Response:
    """DELETE /admin/api/skills/browse/{job_id} — tira el clon."""
    browser = request.app[SKILL_BROWSER_KEY]
    ok = browser.discard(request.match_info["job_id"])
    return web.json_response({"discarded": ok})


async def api_apply_skill_to_transcript(request: web.Request) -> web.Response:
    """POST /admin/api/skills/{name}/apply-to-transcript

    Aplica una skill `when: manual` a un transcript de voz específico.
    Pensado para el botón "Crear issue / historia de usuario" de la
    tab Voz: la skill provee el system prompt, el transcript provee
    el input. El LLM NO toca el repo, NO abre issues en GitHub, NO
    usa tools — devuelve markdown listo para pegar.

    Body:
        transcript_id: str  (requerido; el id del JSONL)
        repo:          str  (opcional; formato owner/name; default "")
        mode:          str  ("issue" | "user_story"; default "issue")

    Returns:
        200 -> {output, model, tokens_in, tokens_out, duration_ms, ...}
        400 -> falta transcript_id / mode inválido
        404 -> skill o transcript desconocidos
        502 -> el LLM falló
        503 -> modelo no disponible
        504 -> timeout

    Decisiones de diseño:
    - Reusamos `run_consult` (no `run_expert`) porque la skill `manual`
      no debe arrastrar el project del transcript. Cero tools, cero
      repo, cero git. Solo la skill como system + el transcript como user.
    - No persistimos la salida como "consult" nueva: el output es un
      artefacto efímero que el usuario decide dónde pegar. Si en el
      futuro aparece el caso de guardar el issue generado, va por
      una tabla aparte (no choca con `consults`, que es por turno).
    - No cacheamos el `content` de la skill en memoria: la leemos
      fresca del FS por si el usuario acaba de aprobarla y el cache
      viejo (TTL 60s) todavía no se invalidó.
    """
    from . import voice as voice_mod
    from . import experts as relay_experts

    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    body = body or {}
    trx_id = (body.get("transcript_id") or "").strip()
    if not trx_id:
        return web.json_response(
            {"error": "transcript_id requerido"}, status=400)
    repo = (body.get("repo") or "").strip()
    mode = (body.get("mode") or "issue").strip()
    if mode not in ("issue", "user_story"):
        return web.json_response(
            {"error": 'mode debe ser "issue" o "user_story"'}, status=400)

    # 1) leer la skill desde disco (cache del lado del FS es barato;
    #    el invalidation lo maneja api_skills_approve / delete).
    from . import skills as skills_mod
    cache = request.app[SKILLS_KEY]
    skill_name = request.match_info["name"]
    skill_md = await asyncio.to_thread(
        skills_mod.read_skill_sync, cache.dir, skill_name)
    if skill_md is None:
        return web.json_response(
            {"error": f"skill {skill_name!r} no existe"}, status=404)

    # 2) leer el transcript
    rec = await voice_mod.read_transcript(trx_id)
    if rec is None:
        return web.json_response(
            {"error": f"transcript {trx_id!r} no existe"}, status=404)
    transcript = (rec.get("transcript") or "").strip()
    if not transcript:
        return web.json_response(
            {"error": "transcript vacío: nada que procesar"}, status=400)

    # 3) armar el user prompt: transcript + repo + mode + metadata útil.
    meta_lines = []
    if rec.get("related_project"):
        meta_lines.append(f"proyecto_relacionado: {rec['related_project']}")
    if rec.get("author"):
        meta_lines.append(f"autor: {rec['author']}")
    if rec.get("ts"):
        meta_lines.append(f"fecha: {rec['ts']}")
    meta = "\n".join(meta_lines)
    user = (
        f"transcript_id: {trx_id}\n"
        f"mode: {mode}\n"
        + (f"repo: {repo}\n" if repo else "repo: (no provisto — si el "
            "transcript menciona un repo, úsalo; si no, pide aclaración)\n")
        + (f"{meta}\n" if meta else "")
        + "\n--- TRANSCRIPT ---\n"
        + transcript
    )

    # 4) correr el LLM sin tools, con la skill como system. system_extra
    #    = content del SKILL.md (frontmatter + cuerpo). El ponytail se
    #    sigue inyectando vía `read_ponytail()` adentro de run_consult.
    try:
        result = await relay_experts.run_consult(
            user=user, system_prompt=skill_md,
            skills_block="",  # la skill YA es system; no duplicar el
                              # índice de skills (ruido inútil).
            model_override="", db=request.app[DB_KEY],
        )
    except relay_experts.ModelUnavailable as e:
        return web.json_response({"error": str(e)}, status=503)
    except asyncio.TimeoutError:
        return web.json_response({"error": "timeout del LLM"}, status=504)
    except Exception as e:  # noqa: BLE001
        logger.exception("apply-skill: %s/%s falló", skill_name, trx_id)
        return web.json_response(
            {"error": f"LLM falló: {type(e).__name__}: {str(e)[:200]}"},
            status=502)

    return web.json_response({
        "transcript_id": trx_id,
        "skill": skill_name,
        "mode": mode,
        "repo": repo,
        "output": result.get("content", ""),
        "model": result.get("model"),
        "tokens_in": result.get("tokens_in"),
        "tokens_out": result.get("tokens_out"),
        "duration_ms": result.get("duration_ms"),
    })


async def api_skill_drafts_list(request: web.Request) -> web.Response:
    """GET /admin/api/skill-drafts?status=&limit= — borradores + count
    de pendientes (para el badge del sidebar)."""
    db = request.app[DB_KEY]
    status = request.query.get("status") or None
    if status and status not in ("pending", "approved", "rejected"):
        return web.json_response(
            {"error": "status debe ser pending|approved|rejected"}, status=400)
    try:
        limit = min(int(request.query.get("limit", "100")), 500)
    except (TypeError, ValueError):
        limit = 100
    drafts = await db.list_skill_drafts(status=status, limit=limit)
    pending = await db.count_skill_drafts_pending()
    return web.json_response({"drafts": drafts, "pending": pending})


async def api_skill_draft_get(request: web.Request) -> web.Response:
    """GET /admin/api/skill-drafts/{id} — detalle con content."""
    db = request.app[DB_KEY]
    try:
        draft_id = int(request.match_info["id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "id inválido"}, status=400)
    draft = await db.get_skill_draft(draft_id)
    if draft is None:
        return web.json_response({"error": "no existe"}, status=404)
    return web.json_response({"draft": draft})


async def api_skill_draft_patch(request: web.Request) -> web.Response:
    """PATCH /admin/api/skill-drafts/{id} — edita name/description/content.

    Solo drafts pending: lo aprobado ya se copió al FS (edita la skill
    instalada) y lo rechazado no tiene sentido editarlo.
    """
    db = request.app[DB_KEY]
    try:
        draft_id = int(request.match_info["id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "id inválido"}, status=400)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    fields = {k: body[k] for k in ("name", "description", "content")
              if isinstance(body.get(k), str)}
    if not fields:
        return web.json_response(
            {"error": "nada para editar (name/description/content)"},
            status=400)
    draft = await db.get_skill_draft(draft_id)
    if draft is None:
        return web.json_response({"error": "no existe"}, status=404)
    if draft["status"] != "pending":
        return web.json_response(
            {"error": f"draft {draft['status']}: solo se editan los pending"},
            status=409)
    await db.update_skill_draft(draft_id, **fields)
    return web.json_response({"draft": await db.get_skill_draft(draft_id)})


async def api_skill_draft_approve(request: web.Request) -> web.Response:
    """POST /admin/api/skill-drafts/{id}/approve — LA aprobación.

    Escribe <skills_dir>/<name>/SKILL.md y marca approved. Si ya existe
    una skill con ese nombre devuelve 409 {needs_overwrite: true}; la UI
    re-postea con {"overwrite": true} tras confirmar.
    """
    from . import skills as skills_mod
    db = request.app[DB_KEY]
    cache = request.app[SKILLS_KEY]
    try:
        draft_id = int(request.match_info["id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "id inválido"}, status=400)
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    overwrite = bool((body or {}).get("overwrite"))
    # `manual=True` aprueba el draft como skill on-demand (frontmatter
    # `when: manual`). NO se inyecta automáticamente en el system prompt;
    # queda disponible vía el botón "Crear issue" en la tab Voz (y
    # futuras integraciones). YAGNI romper este flag por si surge un
    # `when: trigger` más adelante; default False conserva el
    # comportamiento histórico. (2026-07-17, iter 8.5 — refinamiento
    # del draft auto-generado por el compactador.)
    manual = bool((body or {}).get("manual"))
    frontmatter_extra = "when: manual" if manual else ""

    draft = await db.get_skill_draft(draft_id)
    if draft is None:
        return web.json_response({"error": "no existe"}, status=404)
    if draft["status"] != "pending":
        return web.json_response(
            {"error": f"draft ya {draft['status']}"}, status=409)
    try:
        path = await asyncio.to_thread(
            skills_mod.write_skill_sync, draft["name"],
            draft["description"], draft["content"], cache.dir,
            overwrite=overwrite,
            frontmatter_extra=frontmatter_extra)
    except FileExistsError as e:
        return web.json_response(
            {"error": f"ya existe una skill en {e}",
             "needs_overwrite": True}, status=409)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except OSError as e:
        return web.json_response(
            {"error": f"no pude escribir la skill: {e!r}"}, status=500)
    await db.set_skill_draft_status(draft_id, "approved",
                                    approved_path=str(path))
    cache.invalidate()
    logger.info("skill draft #%d aprobado → %s", draft_id, path)
    return web.json_response({
        "approved": True, "id": draft_id, "path": str(path),
        "name": skills_mod.sanitize_skill_name(draft["name"]),
    })


async def api_skill_draft_reject(request: web.Request) -> web.Response:
    """POST /admin/api/skill-drafts/{id}/reject — marca rejected.

    La fila queda (auditoría de qué destiló el compactador); para
    purgarla del todo está el DELETE.
    """
    db = request.app[DB_KEY]
    try:
        draft_id = int(request.match_info["id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "id inválido"}, status=400)
    draft = await db.get_skill_draft(draft_id)
    if draft is None:
        return web.json_response({"error": "no existe"}, status=404)
    if draft["status"] != "pending":
        return web.json_response(
            {"error": f"draft ya {draft['status']}"}, status=409)
    await db.set_skill_draft_status(draft_id, "rejected")
    return web.json_response({"rejected": True, "id": draft_id})


async def api_skill_draft_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/skill-drafts/{id} — purga la fila.

    NO toca el FS: si el draft se aprobó, la skill instalada se borra
    aparte con DELETE /admin/api/skills/{name}.
    """
    db = request.app[DB_KEY]
    try:
        draft_id = int(request.match_info["id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "id inválido"}, status=400)
    if not await db.delete_skill_draft(draft_id):
        return web.json_response({"error": "no existe"}, status=404)
    return web.json_response({"deleted": True, "id": draft_id})


async def api_projects_patch(request: web.Request) -> web.Response:
    """PATCH /admin/api/projects/{slug} — edita nombre/desc/ruta."""
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    editable = {"name", "description", "repo_path", "system_prompt",
                "enabled", "include_in_index", "night_mode_enabled",
                "night_config", "discord_channel_id"}  # Iter 10.1
    for field in editable:
        if field in body:
            if field in ("name", "description", "repo_path", "system_prompt"):
                if not isinstance(body[field], str):
                    continue
            elif field in ("enabled", "include_in_index", "night_mode_enabled"):
                if not isinstance(body[field], bool):
                    continue
            elif field == "night_config":
                if not isinstance(body[field], dict):
                    continue
            elif field == "discord_channel_id":
                # Iter 10.1: null explícito limpia; string vacío se
                # ignora (la UI manda el campo siempre — si quedó vacío,
                # NO queremos pisar el valor existente con NULL por
                # accidente; usamos endpoint dedicado para clear).
                if body[field] is None:
                    project[field] = None
                elif isinstance(body[field], str) and body[field].strip():
                    project[field] = body[field].strip()
                continue
            project[field] = body[field]
    await db.upsert_project(project)
    return web.json_response({"project": _serialize(dict(project))})


async def api_projects_set_discord_channel(request: web.Request) -> web.Response:
    """PATCH /admin/api/projects/{slug}/discord-channel — set/clear del
    canal Discord default por proyecto (Iter 10.1).

    Body:
        discord_channel_id: str | null
            - string no vacío: upsert
            - null: clear (campo a NULL)
            - string vacío: 400

    Esta ruta la usa principalmente el dropdown del DM que el bot
    manda cuando detecta missing_discord_channel en metadata. El form
    general PATCH /admin/api/projects/{slug} también acepta el campo
    (ver `api_projects_patch` arriba).
    """
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    raw = body.get("discord_channel_id")
    if raw is None:
        existing = await db.get_project(slug)
        if existing is None:
            return web.json_response(
                {"error": f"proyecto {slug!r} desconocido"}, status=404)
        ok = await db.set_project_discord_channel(slug, channel_id=None, clear=True)
        if not ok:
            return web.json_response(
                {"error": f"proyecto {slug!r} desconocido"}, status=404)
        return web.json_response({
            "slug": slug,
            "discord_channel_id": None,
            "cleared": True,
        })
    if not isinstance(raw, str) or not raw.strip():
        return web.json_response(
            {"error": "discord_channel_id string vacío o tipo inválido "
                      "(manda null explícito para limpiar)"},
            status=400)
    existing = await db.get_project(slug)
    if existing is None:
        return web.json_response(
            {"error": f"proyecto {slug!r} desconocido"}, status=404)
    ok = await db.set_project_discord_channel(slug, channel_id=raw.strip())
    if not ok:
        return web.json_response(
            {"error": f"proyecto {slug!r} desconocido"}, status=404)
    return web.json_response({"slug": slug, "discord_channel_id": raw})


async def api_projects_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/projects/{slug} — purga hard de la fila.

    NO es soft. La fila se borra de la DB (incluyendo system_prompt,
    mcp_servers, etc). Si el repo sigue indexado en cbm, vuelve a
    aparecer como huérfano en /cbm/orphans y se puede re-importar
    con /projects/from-cbm.

    El soft-delete alternativo es PATCH {enabled: false}.
    """
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    existing = await db.get_project(slug)
    if not existing:
        return web.json_response({"error": "no existe"}, status=404)
    await db.delete_project(slug)
    return web.json_response({"deleted": True, "slug": slug})


# ---------- workspace (docs del proyecto en repo_path) ----------
#
# Filosofía (Ponytail): los docs viven como archivos reales dentro del
# repo, no como notas en la DB. Así cbm los indexa y el experto los lee
# como cualquier otro file. El form de edición de proyecto siempre
# guardó en la DB (name/desc/system_prompt/night_config); este es un
# canal paralelo para archivos de texto en repo_path.
#
# Endpoints:
#   GET  .../workspace/files?subdir=         lista (cap 500, filtra bin/.git/etc)
#   GET  .../workspace/file?path=docs/X.md   lee texto (cap 1MB)
#   PUT  .../workspace/file {path, content}  escribe texto (cap 64KB)
#   POST .../workspace/scaffold {prompt,...} genera files via LLM one-shot
#
# Guard anti path-traversal: TODO path se resuelve contra repo_path
# (Path.resolve() + check parent). Si la ruta normalizada sale del
# repo, 403. Mismo patrón que _safe_skill_dir en skills.py.

WORKSPACE_TEXT_EXTS = frozenset({
    ".md", ".txt", ".rst", ".adoc",  # docs
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",  # código
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".html", ".htm", ".css", ".scss", ".less",
    ".sh", ".ps1", ".bat", ".cmd",
    ".sql", ".csv", ".tsv",
    ".gitignore", ".gitattributes", ".editorconfig",
    ".cs", ".csproj", ".props", ".targets", ".sln",
    ".fs", ".fsproj",  # F#
    ".go", ".rs", ".rb", ".php", ".java", ".kt", ".swift", ".scala",
    ".dockerfile",  # mayúscula también, lo cubrimos por basename
    ".xml", ".proto",
    ".svg",  # Iter 10.4: el tab Diagramas guarda el SVG renderizado
              # por mermaid. Es XML/texto, no binario.
})
WORKSPACE_TEXT_BASENAMES = frozenset({
    "dockerfile", "makefile", "rakefile", "gemfile",
    "license", "copying", "readme", "contributing", "changelog",
    "authors", "notice",
})
WORKSPACE_IGNORED_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "bin", "obj", "dist", "build", "target", ".next", ".nuxt",
    ".cache", ".idea", ".vscode", "out", "coverage", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", "htmlcov",
    ".gradle", ".terraform",
})
WORKSPACE_FILE_READ_CAP = 1 * 1024 * 1024       # 1 MB por archivo leído
WORKSPACE_FILE_WRITE_CAP = 64 * 1024            # 64 KB por archivo escrito
WORKSPACE_LIST_CAP = 500                        # entradas por listado


def _is_text_file(path: Path) -> bool:
    """Decide si un archivo es 'editable como texto' (sin leerlo).

    Mira ext + basename (Dockerfile/Makefile sin ext). Si la extensión
    no está en la whitelist pero tampoco es obviously-binary, devuelve
    False (conservador: mejor decir 'no' y obligar al user a renombrar).
    """
    if path.name.lower() in WORKSPACE_TEXT_BASENAMES:
        return True
    return path.suffix.lower() in WORKSPACE_TEXT_EXTS


def _safe_repo_path(repo_path: str, rel: str) -> Optional[Path]:
    r"""Resuelve rel contra repo_path garantizando que el resultado
    queda DENTRO de repo_path.

    None si:
      - repo_path no existe o no es dir
      - rel es absoluto (defensa en profundidad — Windows resuelve
        /etc/passwd dentro del repo si el repo está en C:\, pero
        en Linux escaparíamos: filter antes de resolver)
      - rel tiene traversal (aceptamos subdirs normales, no `..`)
      - la ruta resuelta se va de repo_path (incluido escape por
        symlink — resolvemos symlinks)

    Resuelve symlinks (Path.resolve) para que un symlink `evil -> ../../etc`
    no escapee. El caller tiene que validar el resultado con is_relative_to.
    """
    base = Path(repo_path).resolve()
    if not base.is_dir():
        return None
    # Normalizar separadores y rechazar absolutos + traversal explícito.
    rel_norm = (rel or "").replace("\\", "/")
    # Chequear ABSOLUTO antes de strip: si el rel empieza con '/' es
    # absoluto POSIX; 'C:/'/'D:/' es absoluto Windows. Path.is_absolute
    # no sirve en este caso (en Windows devuelve False para '/etc/passwd'
    # porque no tiene drive letter).
    if rel_norm.startswith("/") or (
        len(rel_norm) >= 2 and rel_norm[1] == ":"):
        return None
    rel_clean = rel_norm.strip("/")
    if not rel_clean:
        return base
    if ".." in Path(rel_clean).parts:
        return None
    target = (base / rel_clean).resolve()
    if not target.is_relative_to(base):
        return None
    return target


async def api_workspace_files(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug}/workspace/files?subdir=docs

    Lista archivos de texto dentro de repo_path (subdir opcional,
    default raíz). Filtra binarios y directorios ignorados. Cap de
    WORKSPACE_LIST_CAP entradas con mensaje de truncado.
    """
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    subdir = request.query.get("subdir", "")
    base = _safe_repo_path(project["repo_path"], subdir)
    if base is None:
        return web.json_response(
            {"error": "subdir inválido o repo_path no existe"}, status=400)
    # Paths SIEMPRE relativos al repo root (no al subdir), así el cliente
    # puede usarlos directo en GET/PUT ?path=X sin tener que reconstruir.
    # Si los devolviera relativos al subdir, un "ARCHITECTURE.md" en la
    # raíz colisionaría con "docs/ARCHITECTURE.md".
    repo_root = Path(project["repo_path"]).resolve()
    if not base.exists():
        return web.json_response(
            {"error": f"{subdir or '.'} no existe en el repo"}, status=404)
    if not base.is_dir():
        return web.json_response(
            {"error": f"{subdir} no es un directorio"}, status=400)

    entries: list[dict] = []
    truncated = False
    try:
        for child in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name in WORKSPACE_IGNORED_DIRS:
                continue
            if child.is_dir():
                entries.append({
                    # as_posix: el contrato es paths con "/" SIEMPRE (los
                    # consume la UI y el LLM); str() en Windows da "\".
                    "path": child.relative_to(repo_root).as_posix(),
                    "name": child.name,
                    "is_dir": True,
                })
            else:
                if not _is_text_file(child):
                    continue
                try:
                    st = child.stat()
                except OSError:
                    continue
                entries.append({
                    "path": child.relative_to(repo_root).as_posix(),
                    "name": child.name,
                    "is_dir": False,
                    "size": st.st_size,
                    "modified_at": int(st.st_mtime),
                })
            if len(entries) > WORKSPACE_LIST_CAP:
                truncated = True
                entries = entries[:WORKSPACE_LIST_CAP]
                break
    except OSError as e:
        return web.json_response({"error": f"FS error: {e}"}, status=500)

    return web.json_response({
        "slug": slug,
        "subdir": subdir,
        "entries": entries,
        "truncated": truncated,
    })


async def api_workspace_file_get(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug}/workspace/file?path=docs/X.md"""
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    rel = request.query.get("path", "")
    target = _safe_repo_path(project["repo_path"], rel)
    if target is None:
        return web.json_response({"error": "path inválido"}, status=400)
    if not target.exists():
        return web.json_response({"error": "no existe"}, status=404)
    if target.is_dir():
        return web.json_response({"error": "es un directorio"}, status=400)
    if not _is_text_file(target):
        return web.json_response({"error": "binario o no editable"}, status=415)
    try:
        size = target.stat().st_size
    except OSError as e:
        return web.json_response({"error": f"stat: {e}"}, status=500)
    if size > WORKSPACE_FILE_READ_CAP:
        return web.json_response(
            {"error": f"archivo excede cap de {WORKSPACE_FILE_READ_CAP // 1024}KB"},
            status=413)
    try:
        content = await asyncio.to_thread(target.read_text, encoding="utf-8")
    except UnicodeDecodeError:
        return web.json_response({"error": "no es UTF-8 válido"}, status=415)
    except OSError as e:
        return web.json_response({"error": f"read: {e}"}, status=500)
    return web.json_response({
        "path": rel,
        "content": content,
        "size": size,
    })


async def api_workspace_file_put(request: web.Request) -> web.Response:
    """PUT /admin/api/projects/{slug}/workspace/file {path, content}

    Crea o sobrescribe un archivo de texto dentro del repo. Crea los
    subdirectorios intermedios. NO permite sobrescribir si el archivo
    existe y el body trae `overwrite=false` (default false, v1 seguro).
    """
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    rel = (body.get("path") or "").strip()
    content = body.get("content", "")
    overwrite = bool(body.get("overwrite", False))
    if not rel:
        return web.json_response({"error": "path requerido"}, status=400)
    if not isinstance(content, str):
        return web.json_response({"error": "content debe ser string"}, status=400)
    if len(content.encode("utf-8")) > WORKSPACE_FILE_WRITE_CAP:
        return web.json_response(
            {"error": f"content excede cap de {WORKSPACE_FILE_WRITE_CAP // 1024}KB"},
            status=413)
    target = _safe_repo_path(project["repo_path"], rel)
    if target is None:
        return web.json_response({"error": "path inválido"}, status=400)
    # Bloquear path que ya existe como directorio (no pisar dirs).
    if target.exists() and target.is_dir():
        return web.json_response(
            {"error": f"{rel} ya existe como directorio"}, status=409)
    if target.exists() and not overwrite:
        return web.json_response(
            {"error": f"{rel} ya existe (manda overwrite=true para pisar)"},
            status=409)
    # Validar "extensión de texto" en el destino, no en el source.
    if not _is_text_file(target):
        return web.json_response(
            {"error": "extensión no soportada para edición (¿binario?)"},
            status=415)
    # Crear padres si no existen.
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, content, encoding="utf-8")
    except OSError as e:
        return web.json_response({"error": f"write: {e}"}, status=500)
    size = target.stat().st_size
    return web.json_response({
        "path": rel,
        "size": size,
        "created": not target.exists() or overwrite,  # best-effort
    })


async def api_workspace_scaffold(request: web.Request) -> web.Response:
    """POST /admin/api/projects/{slug}/workspace/scaffold {prompt, overwrite}

    Genera un set inicial de archivos (README + docs/) usando el LLM
    one-shot. Devuelve la lista de archivos generados; si `apply=true`
    los escribe al repo (mismo path que PUT, con overwrite=true). El
    default es `apply=false` (preview + confirmación manual) — si el
    LLM alucina paths raros quieres verlos antes.

    Estructura mínima que pedimos al LLM:
      - README.md (descripción + tabla de stack + estructura)
      - docs/ARCHITECTURE.md
      - docs/PLAN.md
      - docs/NOTES.md (el "lugar para ir agregando textos")
    El LLM puede agregar más archivos si el prompt lo justifica.

    Errores:
      - 400 si no hay prompt
      - 502 si el modelo no se puede armar (sin API key)
      - 502 si el LLM devuelve algo que no parsea
      - 500 si escribir falla
    """
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    prompt = (body.get("prompt") or "").strip()
    apply_to_disk = bool(body.get("apply", False))
    overwrite = bool(body.get("overwrite", False))
    if not prompt:
        return web.json_response({"error": "prompt requerido"}, status=400)

    class _ScaffoldFile(BaseModel):
        path: str
        content: str

    class _ScaffoldOutput(BaseModel):
        files: list[_ScaffoldFile]
        rationale: str = ""

    # Prompt de sistema: cortito y directivo. El LLM no necesita más.
    instructions = (
        "Eres un asistente que genera documentación inicial de un proyecto.\n"
        "Devuelve EXCLUSIVAMENTE un objeto JSON con la forma pedida:\n"
        "  files: lista de {path, content} (paths RELATIVOS al repo, sin .., sin absolutos)\n"
        "  rationale: 1-2 frases explicando qué generaste\n"
        "Reglas:\n"
        "  - Paths SIEMPRE relativos (ej: 'README.md', 'docs/PLAN.md')\n"
        "  - Sin ../, sin absolutos (/etc, C:\\, etc)\n"
        "  - Lenguaje: español, seco, directo, sin adornos\n"
        "  - README.md con: descripción de 1-2 líneas, tabla de stack (| área | tech |), "
        "estructura de carpetas (árbol simple)\n"
        "  - docs/ARCHITECTURE.md: cómo está armado (capas, entrypoints, dependencias clave)\n"
        "  - docs/PLAN.md: próximos pasos en bullets accionables\n"
        "  - docs/NOTES.md: secciones vacías para que el usuario vaya agregando\n"
        "  - NO inventes dependencias ni comandos; si no sabes, ponlo como TBD\n"
    )

    # Modelo: mismo que el resto del relay. Cae a "test" en tests
    # unitarios que mockean el Agent (no se llama a la API real).
    spec = relay_config.model_spec()
    try:
        model = build_model(spec)
    except ModelUnavailable as e:
        return web.json_response(
            {"error": f"modelo no disponible: {e}"}, status=502)

    agent = Agent(model, output_type=_ScaffoldOutput,
                  instructions=instructions,
                  model_settings=structured_output_settings(spec))

    user_msg = (
        f"Proyecto: {project['name']} (slug={slug})\n"
        f"Descripción existente: {project.get('description') or '(vacía)'}\n"
        f"Repo: {project['repo_path']}\n\n"
        f"Pedido del usuario:\n{prompt}\n\n"
        "Genera los archivos de scaffolding."
    )

    try:
        result = await agent.run(user_msg)
    except Exception as e:  # noqa: BLE001
        logger.warning("workspace_scaffold: agent.run fallo: %r", e)
        return web.json_response(
            {"error": f"LLM call fallo: {type(e).__name__}: {e}"},
            status=502)

    output: _ScaffoldOutput = result.output

    # Validar paths: todos relativos, sin traversal, ext de texto OK.
    validated: list[dict] = []
    rejected: list[str] = []
    for f in output.files:
        try:
            target = _safe_repo_path(project["repo_path"], f.path)
        except Exception:
            target = None
        if target is None or not _is_text_file(target):
            rejected.append(f.path)
            continue
        size = len(f.content.encode("utf-8"))
        if size > WORKSPACE_FILE_WRITE_CAP:
            rejected.append(f"{f.path} (> {WORKSPACE_FILE_WRITE_CAP // 1024}KB)")
            continue
        validated.append({
            "path": f.path,
            "content": f.content,
            "size": size,
        })

    if not validated:
        return web.json_response({
            "error": "el LLM no generó archivos válidos",
            "rejected": rejected,
        }, status=502)

    written: list[dict] = []
    if apply_to_disk:
        for f in validated:
            t = _safe_repo_path(project["repo_path"], f["path"])
            if t is None:
                rejected.append(f["path"])
                continue
            if t.exists() and not overwrite:
                rejected.append(f"{f['path']} (ya existe)")
                continue
            try:
                t.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(t.write_text, f["content"], encoding="utf-8")
                written.append({"path": f["path"], "size": f["size"]})
            except OSError as e:
                rejected.append(f"{f['path']} (write error: {e})")

    return web.json_response({
        "slug": slug,
        "files": validated,
        "rationale": output.rationale,
        "rejected": rejected,
        "written": written,
        "applied": apply_to_disk,
    })


# ---------- jobs de reindex ----------

_active_index_jobs: dict[str, Any] = {}
# job_id -> monotonic del momento en que terminó. La UI pollea el
# resultado un rato; pasado el TTL se purga (sin esto el dict crece
# un slot por reindex/bulk hasta el próximo restart).
_job_done_at: dict[str, float] = {}
JOB_RESULT_TTL_S = int(os.environ.get("CBM_JOB_TTL", "900"))


def _purge_finished_jobs() -> None:
    now = time.monotonic()
    for jid, done_at in list(_job_done_at.items()):
        if now - done_at > JOB_RESULT_TTL_S:
            _active_index_jobs.pop(jid, None)
            _job_done_at.pop(jid, None)


def _mark_job_done(job_id: str) -> None:
    _job_done_at[job_id] = time.monotonic()
    # Un reindex cambia lo que list_projects devuelve — cache afuera.
    _invalidate_cbm_projects_cache()


def _set_job(job_id: str, value: Any) -> None:
    _purge_finished_jobs()
    _active_index_jobs[job_id] = value


def _get_job(job_id: str) -> Any:
    _purge_finished_jobs()
    return _active_index_jobs.get(job_id)


def _start_index_job(slug: str, repo_path: str) -> str:
    """Arranca `cbm index_repository` en background y devuelve el job_id
    (pollear GET /admin/api/reindex/{job_id}). El caller ya validó que
    cbm_binary_path() existe."""
    job_id = f"job_{slug}_{int(asyncio.get_event_loop().time() * 1000) % 100000}"

    async def _do_index() -> None:
        bin_path = cbm_binary_path()
        proc = await asyncio.create_subprocess_exec(
            bin_path, "cli", "index_repository",
            json.dumps({"repo_path": repo_path}),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_cbm_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=CBM_INDEX_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            proc.kill()
            _set_job(job_id, {"status": "error",
                               "error": f"timeout {CBM_INDEX_TIMEOUT_S}s"})
            return
        last_json = ""
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("{"):
                last_json = s
        if not last_json:
            _set_job(job_id, {"status": "error",
                               "error": stderr.decode("utf-8", errors="replace")[:500]})
            return
        try:
            data = json.loads(last_json)
        except json.JSONDecodeError:
            _set_job(job_id, {"status": "error", "error": "JSON inválido de cbm"})
            return
        _set_job(job_id, data)

    task = asyncio.create_task(_do_index())
    # Guardamos el task mientras corre; _do_index lo reemplaza al terminar.
    # El done_callback arma el TTL del resultado (purga a los JOB_RESULT_TTL_S).
    task.add_done_callback(lambda _t: _mark_job_done(job_id))
    _set_job(job_id, task)
    return job_id


async def api_reindex_post(request: web.Request) -> web.Response:
    """POST /admin/api/projects/{slug}/reindex."""
    slug = request.match_info["slug"]
    db = request.app[DB_KEY]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    if not cbm_binary_path():
        return web.json_response({"error": "cbm no instalado"}, status=503)

    job_id = _start_index_job(slug, project["repo_path"])
    return web.json_response(
        {"job_id": job_id, "slug": slug, "status": "running"},
        status=202,
    )


async def api_reindex_status(request: web.Request) -> web.Response:
    """GET /admin/api/reindex/{job_id} — sirve single y bulk."""
    job_id = request.match_info["job_id"]
    val = _get_job(job_id)
    if val is None:
        return web.json_response({"error": "job desconocido"}, status=404)
    if isinstance(val, asyncio.Task):
        # Reindex single: todavía no reemplazó el slot.
        if not val.done():
            return web.json_response({"job_id": job_id, "status": "running"})
        return web.json_response(
            {"job_id": job_id, "status": "unknown",
             "error": "job terminó sin payload"}
        )
    if isinstance(val, dict):
        # Bulk (y single ya terminado): devolvemos el dict, pero
        # sacamos el handle del task para no serializarlo.
        out = {k: v for k, v in val.items() if k != "_task"}
        return web.json_response(out)
    return web.json_response({"job_id": job_id, "status": "running"})


# ---------- file browser (UI bulk pick) ----------

# Marcadores que dicen "esto parece un repo". Misma heurística que
# bulk_index_repos.py para mantener consistencia.
_REPO_MARKERS = (".git", "*.sln", "*.csproj", "*.fsproj", "package.json",
                 "pyproject.toml", "Cargo.toml", "go.mod", "pom.xml",
                 "build.gradle", "build.gradle.kts")

# Subpaths que NO son repo aunque tengan archivos de repo adentro
# (son package caches, deps, output). Cubre Unity, npm, dotnet, Unity
# Library, JetBrains, etc.
_NON_REPO_PATH_PARTS = (
    "node_modules", "bower_components",
    "Library/PackageCache", "Library/ScriptAssemblies",
    "Library/Bee", "Library/PlayerDataCache",
    "obj", "bin", "dist", "build", "out",
    "__pycache__", ".gradle", "target",
    ".terraform", ".venv", "venv", "env",
    "TestResults", ".idea",
    "DerivedData", "Intermediate", "Saved",
    ".cargo", ".rustup",
)


def _looks_like_repo(d: Path) -> bool:
    # Descartar por path: si CUALQUIER parte del path matchea un
    # non-repo marker, no es repo.
    parts = {p.lower() for p in d.parts}
    for marker in _NON_REPO_PATH_PARTS:
        if marker.lower() in parts:
            return False
    for m in _REPO_MARKERS:
        if m.startswith("*."):
            if any(d.glob(m[1:])):
                return True
        elif (d / m).exists():
            return True
    return False


async def api_project_github(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug}/github — seguimiento del proyecto.

    Issues y PRs abiertos del repo (derivado del remote, no de una
    columna: si el repo se muda de organización, no queda config vieja
    apuntando a otro lado), más las columnas del tablero Projects v2 si
    el proyecto tiene uno mapeado en `defaults_json.github_project`.

    Read-only y a prueba de entorno: sin `gh`, sin auth o sin remote
    devuelve 200 con `configured:false`. Este panel NUNCA puede impedir
    que se abra el tab Proyectos.
    """
    slug = request.match_info["slug"]
    db = request.app[DB_KEY]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)

    repo = await github_mod.repo_slug(project["repo_path"] or "")
    out: dict = {"repo": repo, "configured": bool(repo),
                 "repo_url": f"https://github.com/{repo}" if repo else None,
                 "issues": [], "pulls": [], "board": None, "mapping": None}
    if not repo:
        return web.json_response(out)

    # Las dos lecturas del repo van juntas: son dos spawns de `gh` que no
    # dependen entre sí y el panel las muestra al mismo tiempo.
    issues, pulls = await asyncio.gather(
        github_mod.issues(repo), github_mod.pulls(repo))
    out["issues"] = issues or []
    out["pulls"] = pulls or []
    # `gh` respondió pero sin auth: lo decimos en vez de mostrar un panel
    # vacío que parece "no hay trabajo pendiente".
    out["configured"] = issues is not None or pulls is not None

    # El mapping va SIEMPRE, aunque `gh` no pueda leer el tablero: la UI
    # necesita saber si el proyecto ya tiene uno (para ofrecer vincular o
    # crear) y poder linkear a GitHub incluso si la lectura falló.
    mapped = (project.get("defaults_json") or {}).get("github_project") or {}
    owner, number = mapped.get("owner"), mapped.get("number")
    if owner and isinstance(number, int):
        mapped.setdefault("url", github_mod.board_url(owner, number))
        out["mapping"] = mapped
        items = await github_mod.board_items(owner, number)
        if items is not None:
            out["board"] = {
                "owner": owner, "number": number,
                "url": mapped["url"],
                "columns": github_mod.group_by_status(items),
            }
    return web.json_response(out)


async def api_github_board(request: web.Request) -> web.Response:
    """GET /admin/api/github/board — el tablero de empresa (fase 5).

    Qué se está haciendo en TODA la empresa, agrupado por columna del
    tablero y con el repo de cada item (los tableros cruzan repos y
    organizaciones: el de "Gestión Empresarial" trae items de otra org).

    El tablero sale de system_config (`GITHUB_BOARD_OWNER` /
    `GITHUB_BOARD_NUMBER`, editables desde el tab Config) o de los query
    params, para poder mirar otro tablero sin cambiar la config.
    """
    db = request.app[DB_KEY]
    cfg = await db.all_config()
    owner = (request.query.get("owner")
             or cfg.get("GITHUB_BOARD_OWNER", "")).strip()
    raw_number = request.query.get("number") or cfg.get("GITHUB_BOARD_NUMBER", "")
    try:
        number = int(str(raw_number).strip())
    except (TypeError, ValueError):
        number = 0
    if not owner or number <= 0:
        return web.json_response(
            {"configured": False, "owner": owner, "number": number or None,
             "columns": {}, "boards": await github_mod.boards(owner) or []
             if owner else []})

    items = await github_mod.board_items(owner, number)
    if items is None:
        return web.json_response(
            {"configured": False, "owner": owner, "number": number,
             "columns": {}, "boards": [],
             "error": "gh no pudo leer el tablero (¿instalado y autenticado?)"})
    # Título del tablero: `item-list` no lo trae, sale del listado (que
    # ya está cacheado 60s). Si `gh project list` falla, el header cae al
    # número — no vale un 500 por un string.
    title = ""
    for b in (await github_mod.boards(owner)) or []:
        if b.get("number") == number:
            title = b.get("title") or ""
            break
    return web.json_response({
        "configured": True, "owner": owner, "number": number,
        "title": title, "url": github_mod.board_url(owner, number),
        "total": len(items),
        "columns": github_mod.group_by_status(items),
        "repos": sorted({i["repository"] for i in items if i["repository"]}),
    })


async def _merge_defaults(db, project: dict, patch: dict) -> None:
    """Merge de claves sueltas en defaults_json (None borra la clave).

    Merge y no replace: el PATCH de proyectos pisa el dict entero, así
    que escribir `defaults_json` completo desde acá se llevaría puestos
    el model y el timeout del proyecto.
    """
    defaults = dict(project.get("defaults_json") or {})
    for k, v in patch.items():
        if v is None:
            defaults.pop(k, None)
        else:
            defaults[k] = v
    await db.upsert_project({"slug": project["slug"], "defaults_json": defaults})


# ---- flags del proyecto (2026-08-16) ----
#
# Hasta ahora `read_only`, `native_files`, `sandbox` y compañía se
# editaban escribiendo JSON a mano contra la API. Cada uno cambia lo que
# el experto PUEDE hacer, así que vivir solo en un blob que nadie ve es
# la peor forma de guardarlos: la gente no configura lo que no encuentra.
#
# Whitelist tipada y no el blob entero a propósito. Si la UI mandara
# `defaults_json` completo, un save concurrente o una UI vieja se
# llevaría puesto el `model`, el `timeout` o el `github_project` del
# proyecto — que es exactamente el bug que `_merge_defaults` existe para
# evitar (y el mismo que ya mordió con `night_config`).

# nombre → (tipo, default efectivo, descripción corta para la UI)
_PROJECT_FLAGS: dict = {
    "native_files": ("bool", True,
                     "Tools de archivo nativas (read/write/edit/list/search)"),
    "native_shell": ("bool", True, "Tool `shell` nativa"),
    "sql_tools": ("bool", True, "Tools SQL (`db_query` / `db_connections`)"),
    # 9/9/2026: los grafos pasaron a serial por defecto. Las reservas por
    # archivo no alcanzan a la `shell`, así que dos nodos que declaran
    # `[]` igual pueden pisarse un build. Prenderlo es hacerse cargo.
    "grafo_paralelo": ("bool", False,
                       "Correr 2 nodos del grafo a la vez. Ojo: la shell "
                       "escribe sin declarar archivos"),
    "read_only": ("bool", False,
                  "Solo lectura: el experto no escribe archivos"),
    "sandbox": ("bool", True,
                "Sandbox de rutas. Apagado = todo el disco (docs/SANDBOX.md)"),
    "three_stage": ("bool", True,
                    "Runner por etapas (planner → executor → verifier)"),
    "verifier": ("bool", True, "Etapa de verificación"),
    "documenter": ("bool", True, "Etapa de documentación"),
    "inject_skills": ("bool", True, "Inyectar skills en el prompt"),
    # 2026-08-20. Apagado por default porque cuesta tokens en CADA
    # request (example-client ~2000 con el tope, demo-project ~930) y no todo
    # proyecto tiene hechos que valgan eso. Va en esta lista y no solo
    # en `defaults_json` por lo que dice el comentario de arriba: un
    # flag que no se ve es un flag que nadie prende — y ese fue
    # exactamente el destino de la vía manual que este flag reemplaza.
    "facts_always_on": ("bool", False,
                        "Inyectar los hechos del proyecto en cada run. Si "
                        "no, solo se ven con `/fact` o en este panel"),
    # 2026-08-23. Prendido por default porque lo que reemplaza es NO
    # HACER NADA: hasta hoy, un pedido que el planificador juzgaba
    # demasiado grande devolvía la descomposición en prosa y ahí moría —
    # el humano tenía que elegir por dónde empezar y volver a pedirlo,
    # una tarea por vez. El grafo no está reemplazando una ejecución
    # cuidadosa; está reemplazando un mensaje. Apagalo si preferís que
    # te proponga y espere.
    "grafo_automatico": ("bool", True,
                         "Un pedido demasiado grande se parte en un grafo de "
                         "tareas y se ejecuta, en vez de solo proponerlo"),
    "skills_mode": ("enum:embed,compact", "embed",
                    "`embed` mete las skills enteras; `compact` solo los "
                    "nombres + `read_skill` on-demand"),
    "rutas_extra": ("list", [],
                    "Raíces extra habilitadas además del repo. El token "
                    "`repos` se expande al FOURBIS_REPOS_ROOT"),
    "rutas_vedadas": ("list", [],
                      "Rutas que NUNCA se leen, ni con el sandbox apagado"),
    # Modelo por etapa (2026-08-18). Ya existían en `defaults_json` pero
    # solo se podían tocar por SQL, así que nadie los usaba: el
    # planificador y el documentador venían corriendo con el mismo
    # modelo pesado del ejecutor, que es justo lo que estas etapas
    # querían evitar. "" = el global del relay.
    "model": ("model", "", "Modelo del ejecutor (el que hace el trabajo)"),
    "planner_model": ("model", "",
                      "Modelo del planificador. Turno corto: conviene uno "
                      "barato"),
    # 8/9/2026: cortar el grafo y planificar un turno son trabajos
    # distintos. El grafo corre 3 veces por día contra 21 del por-turno,
    # así que un modelo caro acá sale ~5 USD/mes y allá ~46 — y es el
    # que lo justifica, porque un mal corte manda cinco bots a hacer lo
    # equivocado. Vacío = usa `planner_model`.
    "graph_planner_model": ("model", "",
                            "Modelo que corta el grafo de tareas. Vacío "
                            "usa el del planificador"),
    "verifier_model": ("model", "",
                       "Modelo del verificador. Turno corto y preciso"),
    "documenter_model": ("model", "",
                         "Modelo del documentador. Solo redacta el resumen "
                         "final"),
}

#: Qué global resuelve cada flag de tipo `model` cuando el proyecto no
#: fija ninguno. La UI lo muestra como "(el global: X)" — sin esto, un
#: select vacío no dice con qué va a correr en realidad.
_MODEL_FLAG_GLOBAL = {
    "model": lambda: relay_config.model_spec(),
    "planner_model": lambda: relay_config.planner_model_spec(),
    "graph_planner_model": lambda: relay_config.planner_model_spec(),
    "verifier_model": lambda: relay_config.verifier_model_spec(),
    "documenter_model": lambda: relay_config.documenter_model_spec(),
}


def _flags_state(defaults: dict) -> dict:
    """El estado de cada flag: valor efectivo + si está explícito.

    `explicito` importa para la UI: un flag en su default y un flag
    puesto a mano en su mismo valor se ven igual, pero significan cosas
    distintas cuando alguien cambie el default más adelante.
    """
    salida = {}
    for nombre, (tipo, default, desc) in _PROJECT_FLAGS.items():
        if tipo == "model":
            # El default no es "" sino lo que resuelva la cascada del
            # relay; decirlo es la mitad de la información.
            try:
                default = _MODEL_FLAG_GLOBAL[nombre]()
                if nombre == "graph_planner_model":
                    default = defaults.get("planner_model") or default
            except Exception:  # noqa: BLE001 — la UI no se cae por esto
                default = ""
        salida[nombre] = {
            "valor": defaults.get(nombre, "" if tipo == "model" else default),
            "default": default,
            "explicito": nombre in defaults,
            "tipo": tipo,
            "descripcion": desc,
        }
    return salida


def _validar_flag(nombre: str, valor):
    """`valor` normalizado, o `(None, error)`. `None` = volver al default."""
    tipo = _PROJECT_FLAGS[nombre][0]
    if valor is None:
        return None, ""
    if tipo == "bool":
        if not isinstance(valor, bool):
            return None, f"`{nombre}` tiene que ser true o false"
        return valor, ""
    if tipo.startswith("enum:"):
        opciones = tipo.split(":", 1)[1].split(",")
        if valor not in opciones:
            return None, f"`{nombre}` tiene que ser uno de: {', '.join(opciones)}"
        return valor, ""
    if tipo == "list":
        if not isinstance(valor, list):
            return None, f"`{nombre}` tiene que ser una lista"
        limpio = [str(v).strip() for v in valor if str(v or "").strip()]
        return limpio, ""
    if tipo == "model":
        spec = str(valor or "").strip()
        if not spec:
            return None, ""          # "" = volver al global
        # Se valida contra el catálogo PRENDIDO: un spec con un typo se
        # guardaría igual y el proyecto reventaría recién en el próximo
        # run, con un error del provider que no nombra la causa.
        from . import experts as experts_mod
        prendidos = {m["spec"] for m in experts_mod.catalog() if m.get("enabled")}
        if spec not in prendidos:
            return None, (f"`{spec}` no está en el catálogo de modelos "
                          f"prendidos. Prendelo en la pantalla Modelos "
                          f"antes de asignarlo.")
        return spec, ""
    return None, f"tipo desconocido para `{nombre}`"


async def api_project_flags_get(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug}/flags"""
    db = request.app[DB_KEY]
    project = await db.get_project(request.match_info["slug"])
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(
        {"flags": _flags_state(project.get("defaults_json") or {})})


async def api_project_flags_patch(request: web.Request) -> web.Response:
    """PATCH /admin/api/projects/{slug}/flags — merge, `null` vuelve al default.

    Merge y no replace: el body trae solo lo que cambió, así que dos
    pestañas abiertas no se pisan los flags que la otra tocó, y las
    claves que no son flags (`model`, `timeout`, `github_project`) ni se
    enteran.
    """
    slug = request.match_info["slug"]
    db = request.app[DB_KEY]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body tiene que ser un objeto"},
                                 status=400)

    desconocidos = sorted(k for k in body if k not in _PROJECT_FLAGS)
    if desconocidos:
        return web.json_response(
            {"error": f"flags desconocidos: {', '.join(desconocidos)}",
             "flags": sorted(_PROJECT_FLAGS)}, status=400)

    # Validar TODO antes de escribir: un body con un flag malo no deja el
    # proyecto a medio aplicar.
    patch = {}
    for nombre, crudo in body.items():
        valor, err = _validar_flag(nombre, crudo)
        if err:
            return web.json_response({"error": err}, status=400)
        patch[nombre] = valor

    await _merge_defaults(db, project, patch)
    fresco = await db.get_project(slug)
    defaults = fresco.get("defaults_json") or {}
    if not defaults.get("sandbox", True):
        logger.warning(
            "sandbox APAGADO para %s desde la Admin UI: las tools de archivo "
            "llegan a todo el disco (docs/SANDBOX.md)", slug)
    return web.json_response({"ok": True, "flags": _flags_state(defaults)})


async def api_project_github_link(request: web.Request) -> web.Response:
    """PUT /admin/api/projects/{slug}/github-project — vincula un tablero.

    Body: `{owner, number, title?, url?}` para vincular; `{}` o
    `{number: null}` para desvincular; `{skip: true}` para sacarlo de la
    lista de "proyectos sin tablero" (hay repos que nunca van a tener
    uno y no tienen por qué ensuciar la vista de gestión), `{skip: false}`
    para devolverlo. No valida contra GitHub: si el número está mal, el
    panel lo muestra vacío y se corrige ahí mismo.
    """
    slug = request.match_info["slug"]
    db = request.app[DB_KEY]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    body = body or {}
    if "skip" in body:
        skip = bool(body["skip"])
        await _merge_defaults(db, project,
                              {"github_project_skip": True if skip else None})
        return web.json_response({"skipped": skip})
    number = body.get("number")
    if number in (None, "", 0):
        await _merge_defaults(db, project, {"github_project": None})
        return web.json_response({"linked": None})
    try:
        number = int(number)
    except (TypeError, ValueError):
        return web.json_response({"error": "number debe ser entero"}, status=400)
    owner = (body.get("owner") or "").strip()
    if not owner:
        return web.json_response({"error": "owner requerido"}, status=400)
    mapping = {"owner": owner, "number": number,
               "title": (body.get("title") or "").strip(),
               "url": (body.get("url") or "").strip()
                      or github_mod.board_url(owner, number)}
    await _merge_defaults(db, project, {"github_project": mapping})
    return web.json_response({"linked": mapping})


async def api_project_client_link(request: web.Request) -> web.Response:
    """PUT /admin/api/projects/{slug}/client — vincula un cliente CRM.

    Body: `{client_id: N}` para vincular, `{client_id: null}` (o `{}`)
    para desvincular.

    Este endpoint faltaba y por eso la cadena cliente→proyecto no
    funcionaba: la columna `projects.client_id`, su FK y el método
    `db.set_project_client()` existían desde F1, pero nada los
    alcanzaba — ni HTTP ni UI. El comentario del schema afirmaba que lo
    llenaba la UI al "convertir un deal ganado en proyecto"; esa
    conversión nunca se implementó.

    A diferencia de `github-project`, acá SÍ se valida que el cliente
    exista: es una FK real, y un id inventado dejaría el proyecto
    apuntando a la nada con ON DELETE SET NULL sin avisar.
    """
    slug = request.match_info["slug"]
    db = request.app[DB_KEY]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    body = body or {}
    raw = body.get("client_id")
    if raw in (None, "", 0):
        await db.set_project_client(project_slug=project["slug"],
                                    client_id=None)
        return web.json_response({"client_id": None, "client_name": None})
    try:
        client_id = int(raw)
    except (TypeError, ValueError):
        return web.json_response(
            {"error": "client_id debe ser entero o null"}, status=400)
    client = await db.get_crm_client(client_id)
    if not client:
        return web.json_response(
            {"error": f"no existe el cliente {client_id}"}, status=404)
    await db.set_project_client(project_slug=project["slug"],
                                client_id=client_id)
    return web.json_response(
        {"client_id": client_id, "client_name": client.get("name")})


async def api_project_deal_link(request: web.Request) -> web.Response:
    """PUT /admin/api/projects/{slug}/deal — vincula un deal del CRM.

    Body: `{deal_id: "cuid"}` para vincular, `{deal_id: null}` (o `{}`)
    para desvincular.

    Un proyecto es un deal, y un deal cuelga de una company: por eso el
    `client_id` NO se pide, se deduce del deal. Pedirlo aparte dejaría
    armar la combinación imposible (proyecto de un cliente con el deal de
    otro).

    Desvincular el deal deja el cliente como estaba: se puede saber de
    quién es un proyecto sin haber cerrado la venta todavía.
    """
    slug = request.match_info["slug"]
    db = request.app[DB_KEY]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    raw = (body or {}).get("deal_id")
    if raw in (None, ""):
        await db.set_project_deal(project_slug=project["slug"], deal_id=None,
                                  client_id=project.get("client_id"))
        return web.json_response({"deal_id": None})
    deal_id = str(raw)
    client = await db.find_crm_client_by_deal(deal_id)
    if not client:
        return web.json_response(
            {"error": f"no existe el deal {deal_id} en el snapshot; "
                      f"sincronizá el CRM"}, status=404)
    deal = next(d for d in client["deals"] if str(d.get("id")) == deal_id)
    await db.set_project_deal(project_slug=project["slug"], deal_id=deal_id,
                              client_id=client["id"])
    return web.json_response({
        "deal_id": deal_id,
        "deal_name": deal.get("name"),
        "client_id": client["id"],
        "client_name": client.get("name"),
    })


async def api_crm_client_detail(request: web.Request) -> web.Response:
    """GET /admin/api/crm/clients/{cid} — el cliente y su cadena.

    Devuelve el cliente (contactos + deals del espejo del CRM) y sus
    proyectos, cada uno con lo necesario para seguir bajando:
    `repo_path` + `has_git` (git) y `github_project` (kanban, que a su
    vez es la entrada a issues y PRs vía
    `/admin/api/projects/{slug}/github`).

    Va en una sola llamada a propósito: la cadena
    cliente → proyecto → git → kanban se navega de arriba hacia abajo, y
    pedir un fetch por eslabón la vuelve inusable con varios proyectos.
    """
    db = request.app[DB_KEY]
    try:
        cid = int(request.match_info["cid"])
    except (TypeError, ValueError):
        return web.json_response({"error": "cid inválido"}, status=400)
    client = await db.get_crm_client(cid)
    if not client:
        return web.json_response({"error": "not found"}, status=404)
    projects = await db.list_projects_for_client(cid)
    # Deals del cliente por id, para poder mostrar el nombre del deal de
    # cada proyecto sin que la UI tenga que cruzarlos a mano.
    deals_by_id = {str(d.get("id")): d for d in (client.get("deals") or [])}
    out_projects = []
    for p in projects:
        rp = p.get("repo_path", "")
        defaults = p.get("defaults_json") or {}
        deal = deals_by_id.get(str(p.get("deal_id") or ""))
        out_projects.append({
            "slug": p["slug"],
            "name": p["name"],
            "repo_path": rp,
            "has_git": _has_git(Path(rp)) if rp else False,
            "git_remote_url": _git_remote_url(rp) if rp else None,
            "github_project": defaults.get("github_project"),
            "enabled": bool(p.get("enabled", 1)),
            "deal_id": p.get("deal_id"),
            "deal_name": deal.get("name") if deal else None,
            "deal_stage": deal.get("stage") if deal else None,
        })
    return web.json_response(_serialize({
        "client": client,
        "projects": out_projects,
    }))


async def api_project_github_create(request: web.Request) -> web.Response:
    """POST /admin/api/projects/{slug}/github-project — crea el tablero.

    Body: `{owner, title?}`. Crea el Projects v2, lo linkea al repo en
    GitHub (pestaña Projects del repo, best-effort) y lo deja vinculado
    al proyecto del relay. ESCRITURA: solo por click humano.
    """
    slug = request.match_info["slug"]
    db = request.app[DB_KEY]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    body = body or {}
    owner = (body.get("owner") or "").strip()
    if not owner:
        return web.json_response({"error": "owner requerido"}, status=400)
    title = (body.get("title") or "").strip() or project.get("name") or slug

    data = await github_mod.create_board(owner, title)
    if not data or not data.get("number"):
        return web.json_response(
            {"error": "gh no pudo crear el tablero (¿scope `project` en el "
                      "token? `gh auth status`)"}, status=502)
    number = int(data["number"])
    # Linkearlo al repo hace que aparezca en la pestaña Projects del repo
    # en GitHub. Best-effort: si falla, el vínculo del relay igual queda.
    repo = await github_mod.repo_slug(project["repo_path"] or "")
    linked_repo, link_error = (
        await github_mod.link_board(owner, number, repo) if repo
        else (False, "el repo no tiene remote de GitHub"))
    mapping = {"owner": owner, "number": number, "title": title,
               "url": data.get("url") or github_mod.board_url(owner, number)}
    await _merge_defaults(db, project, {"github_project": mapping})
    return web.json_response({"created": mapping, "linked_to_repo": linked_repo,
                              "link_error": link_error}, status=201)


async def api_github_boards(request: web.Request) -> web.Response:
    """GET /admin/api/github/boards?owner=X — tableros del owner.

    Alimenta el desplegable de configuración (y la fase 3, cuando toque
    mapear tablero por proyecto).
    """
    owner = (request.query.get("owner") or "").strip()
    if not owner:
        return web.json_response({"error": "owner requerido"}, status=400)
    data = await github_mod.boards(owner)
    if data is None:
        return web.json_response(
            {"boards": [], "error": "gh no pudo listar los tableros"})
    return web.json_response({"boards": data})


async def api_fs_browse(request: web.Request) -> web.Response:
    """GET /admin/api/fs/browse?path=C:\\Users\\...&depth=2.

    Lista subdirs de primer nivel (o más si `depth>1`) y para cada uno
    reporta:
      - is_repo:        ¿tiene marker de repo? (.git, *.sln, etc.)
      - has_expert:     ¿hay un project en la DB con ese repo_path?
      - expert_slug:    nombre del project si lo hay
      - cbm_indexed:    ¿está indexado en cbm? (lookup en list_projects)
      - cbm_nodes/edges si está indexado

    Sirve para alimentar el file-browser del tab Indexación sin hacer
    que el cliente recorra paths a mano.
    """
    raw = request.query.get("path", "").strip()
    if not raw:
        return web.json_response({"error": "path requerido"}, status=400)
    root = Path(raw)
    if not root.is_dir():
        return web.json_response({"error": f"no es directorio: {root}"},
                                 status=400)
    try:
        depth = max(1, min(int(request.query.get("depth", "1")), 4))
    except ValueError:
        depth = 1

    db = request.app[DB_KEY]

    # Mapa repo_path -> project (case-insensitive en Windows).
    all_projects = await db.list_projects(enabled_only=False)
    by_path: dict[str, dict] = {}
    for p in all_projects:
        rp = (p.get("repo_path") or "").replace("\\", "/").rstrip("/").lower()
        if rp:
            by_path[rp] = p

    # Mapa cbm root_path -> stats.
    cbm_indexed: dict[str, dict] = {}
    if cbm_binary_path():
        try:
            data = await _cbm_list_projects()
            for c in data.get("projects", []):
                rp = (c.get("root_path") or "").replace("\\", "/").rstrip("/").lower()
                if rp:
                    cbm_indexed[rp] = {
                        "nodes": c.get("nodes", 0),
                        "edges": c.get("edges", 0),
                        "name": c.get("name", ""),
                    }
        except Exception:
            pass

    def _key(d: Path) -> str:
        return str(d).replace("\\", "/").rstrip("/").lower()

    def _walk(d: Path, level: int, out: list[dict]) -> None:
        try:
            entries = sorted(
                [e for e in d.iterdir() if e.is_dir()],
                key=lambda p: p.name.lower(),
            )
        except (PermissionError, OSError):
            return
        for child in entries:
            key = _key(child)
            expert = by_path.get(key)
            cbm = cbm_indexed.get(key)
            is_repo = _looks_like_repo(child)
            if is_repo or expert:
                out.append({
                    "path": str(child),
                    "name": child.name,
                    "depth": level,
                    "is_repo": is_repo,
                    "has_expert": bool(expert),
                    "expert_slug": expert["slug"] if expert else None,
                    "expert_enabled": expert["enabled"] if expert else False,
                    "cbm_indexed": bool(cbm),
                    "cbm_nodes": (cbm or {}).get("nodes", 0),
                    "cbm_edges": (cbm or {}).get("edges", 0),
                })
                # Si parece repo, NO bajamos adentro (sería ruido).
                continue
            if level < depth:
                _walk(child, level + 1, out)

    found: list[dict] = []
    _walk(root, 1, found)

    return web.json_response({
        "root": str(root),
        "depth": depth,
        "count": len(found),
        "entries": found,
    })


async def api_index_status(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug}/index/status."""
    slug = request.match_info["slug"]
    db = request.app[DB_KEY]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    if not cbm_binary_path():
        return web.json_response({"slug": slug, "indexed": False,
                                 "reason": "cbm no instalado"})
    try:
        data = await _cbm_list_projects()
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": str(e)}, status=500)

    # Match determinista por el nombre interno de cbm (derivado del path
    # absoluto), NO por endswith(basename): "INVENTORYDEMO" y "Forks/INVENTORYDEMO" son
    # proyectos distintos y el sufijo los confundía.
    cbm_proj = _cbm_project_name(project["repo_path"])
    for c in data.get("projects", []):
        if c.get("name") == cbm_proj:
            return web.json_response({
                "slug": slug,
                "indexed": True,
                "name": c.get("name"),
                "nodes": c.get("nodes"),
                "edges": c.get("edges"),
                "size_bytes": c.get("size_bytes"),
            })
    return web.json_response({"slug": slug, "indexed": False})


#: `QN (ruta/al/archivo.cs):` — cabecera de grupo en search_graph.
_CBM_GROUP_RE = re.compile(r"^(\S.*?)\s+\((.+)\):$")


def _parse_cbm_search_graph(text: str) -> tuple[list[dict], int, bool]:
    """Texto de `cbm cli search_graph` → (files, total, has_more).

    Formato:
        total: 633
        results: 3  (rows: name label lines in out; ...)
        C-...-SampleApp.Web.react-app (Web/react-app/.env):
          __file__ File  0 0
        has_more: true

    El path real vive en la CABECERA de grupo, no en la fila: las filas de
    label=File son todas `__file__ File  0 0`. Por eso el ranking "top files
    by degree" que intentaba el tab Diagramas no podía existir — los nodos
    File tienen grado 0. Para "qué archivo importa" está get_architecture
    (aspects=hotspots), que sí trae fan_in por símbolo.
    """
    files: list[dict] = []
    total = 0
    has_more = False
    cur: dict | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0].isspace():
            if cur is None:
                continue
            vals = line.split()
            # name label lines in out  → in/out son los dos últimos.
            if len(vals) >= 2 and vals[-1].isdigit() and vals[-2].isdigit():
                cur["in_degree"] = int(vals[-2])
                cur["out_degree"] = int(vals[-1])
            continue
        m = _CBM_SCALAR_RE.match(line)
        if m and m.group(1) == "total":
            total = int(m.group(2)) if m.group(2).strip().isdigit() else 0
            cur = None
            continue
        if m and m.group(1) == "has_more":
            has_more = m.group(2).strip().lower() == "true"
            cur = None
            continue
        if m and m.group(1) == "results":
            cur = None
            continue
        g = _CBM_GROUP_RE.match(line)
        if g:
            fp = g.group(2).replace("\\", "/")
            cur = {
                "path": fp,
                "name": fp.rsplit("/", 1)[-1],
                "qualified_name": g.group(1),
                "extension": ("." + fp.rsplit(".", 1)[-1]) if "." in fp else "",
                "in_degree": 0,
                "out_degree": 0,
            }
            files.append(cur)
    return files, total, has_more


#: Aspects de get_architecture que la UI sabe consumir.
_ARCH_ASPECTS = {"structure", "dependencies", "routes", "hotspots",
                 "boundaries", "layers", "clusters", "file_tree"}
#: Cache por (cbm_project, aspects) — get_architecture paga el spawn de cbm
#: (~1.5s, ver _CBM_PROJECTS_TTL_S) y el tab Diagramas re-pide lo mismo cada
#: vez que tocas un autogen. TTL corto: el índice cambia solo al reindexar.
_ARCH_TTL_S = 120.0
_arch_cache: dict[tuple[str, str], tuple[float, dict]] = {}


async def api_project_architecture(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug}/architecture?aspects=layers,boundaries

    Devuelve el grafo semántico que cbm ya calcula (capas, boundaries con
    peso de llamadas, clusters por cohesión, hotspots por fan-in, rutas HTTP
    y file_tree real). Es la fuente que el tab Diagramas necesitaba: antes
    dibujaba `ls` del root con fs/browse y lo llamaba "arquitectura".
    """
    slug = request.match_info["slug"]
    db = request.app[DB_KEY]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    if not cbm_binary_path():
        return web.json_response({"error": "cbm no instalado"}, status=503)

    req = request.query.get("aspects", "layers,boundaries")
    aspects = sorted({a for a in (s.strip() for s in req.split(","))
                      if a in _ARCH_ASPECTS})
    if not aspects:
        return web.json_response(
            {"error": f"aspects inválidos; usa: {sorted(_ARCH_ASPECTS)}"},
            status=400)

    cbm_proj = _cbm_project_name(project["repo_path"])
    key = (cbm_proj, ",".join(aspects))
    hit = _arch_cache.get(key)
    if hit and (time.time() - hit[0]) < _ARCH_TTL_S:
        return web.json_response({**hit[1], "cached": True})

    try:
        text, err = await _cbm_cli_text(
            "cli", "get_architecture",
            json.dumps({"project": cbm_proj, "aspects": aspects}),
        )
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": str(e)}, status=500)
    if err:
        return web.json_response({"error": err, "cbm_project": cbm_proj},
                                 status=502)

    data = _parse_cbm_sections(text)
    payload = {"slug": slug, "cbm_project": cbm_proj,
               "aspects": aspects, **_serialize(data)}
    _arch_cache[key] = (time.time(), payload)
    return web.json_response(payload)


def _parse_cbm_grouped(text: str, section: str) -> list[dict]:
    """Filas agrupadas de trace_call_path: `QN:` + filas `name hop`.

    Mismo formato que search_graph pero con cabecera `callers: N (rows: …)`
    en vez de `(cols: …)`, así que _parse_cbm_sections no lo toma.
    """
    out: list[dict] = []
    group = ""
    active = False
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0].isspace():
            if not active:
                continue
            parts = line.split()
            if parts:
                out.append({"group": group, "name": parts[0],
                            "hop": int(parts[-1]) if parts[-1].isdigit() else 1})
            continue
        m = re.match(r"^(\w+):\s*(\d+)\s*\(rows:", line)
        if m:
            active = m.group(1) == section
            continue
        if _CBM_SCALAR_RE.match(line) and ":" in line and not line.endswith(":"):
            active = active and False if line.split(":")[0].isidentifier() else active
            continue
        if line.endswith(":"):
            group = line[:-1].strip()
    return out


#: Consultas Cypher que la UI puede pedir. Las escribe el SERVER: el browser
#: manda un `kind` de esta lista, nunca Cypher — no hace falta abrir un
#: intérprete de queries arbitrarias contra el índice para dibujar 3 diagramas.
_GRAPH_KINDS: dict[str, dict[str, str]] = {
    "classes": {
        "methods": "MATCH (c:Class)-[:DEFINES_METHOD]->(m:Method) "
                   "RETURN c.name, m.name LIMIT 400",
        "inherits": "MATCH (a)-[:INHERITS]->(b) RETURN a.name, b.name LIMIT 120",
        "implements": "MATCH (a)-[:IMPLEMENTS]->(b) RETURN a.name, b.name LIMIT 120",
    },
    "states": {
        # cbm NO guarda los miembros de un enum, solo el nodo y sus usos: por
        # eso esto es un mapa de estados-y-consumidores, no una máquina de
        # estados con transiciones. Ver el toast del tab.
        "enums": "MATCH (e:Enum) RETURN e.name, e.qualified_name LIMIT 60",
        "usage": "MATCH (x)-[:USAGE]->(e:Enum) RETURN e.name, x.name LIMIT 300",
    },
}


async def api_project_graph(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug}/graph/{kind}

    kind ∈ classes | states | sequence. Devuelve el grafo semántico que
    cbm ya tiene indexado, listo para dibujar. `sequence` acepta
    ?function=NOMBRE (default: el hotspot de mayor fan-in).
    """
    slug = request.match_info["slug"]
    kind = request.match_info["kind"]
    db = request.app[DB_KEY]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    if not cbm_binary_path():
        return web.json_response({"error": "cbm no instalado"}, status=503)
    cbm_proj = _cbm_project_name(project["repo_path"])

    if kind == "sequence":
        fn = request.query.get("function", "").strip()
        if not fn:
            return web.json_response(
                {"error": "falta ?function="}, status=400)
        try:
            text, err = await _cbm_cli_text(
                "cli", "trace_call_path",
                json.dumps({"project": cbm_proj, "function_name": fn}))
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": str(e)}, status=500)
        if err:
            return web.json_response({"error": err}, status=502)
        # Nombre repetido (21 `CreateAsync` en SampleApp): cbm devuelve JSON con
        # status=ambiguous + candidatos. No es un fallo — es una lista para
        # elegir, así que la pasamos a la UI en vez de romper.
        if text.lstrip().startswith("{"):
            try:
                j = json.loads(text.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                j = {}
            if j.get("status") == "ambiguous":
                return web.json_response({
                    "slug": slug, "kind": kind, "function": fn,
                    "ambiguous": True,
                    "message": j.get("message", ""),
                    "suggestions": _serialize(j.get("suggestions", [])[:40]),
                })
        return web.json_response({
            "slug": slug, "kind": kind, "function": fn,
            "callers": _serialize(_parse_cbm_grouped(text, "callers")),
            "callees": _serialize(_parse_cbm_grouped(text, "callees")),
        })

    queries = _GRAPH_KINDS.get(kind)
    if not queries:
        return web.json_response(
            {"error": f"kind inválido; usa: {sorted(_GRAPH_KINDS)} o sequence"},
            status=400)

    out: dict = {"slug": slug, "kind": kind, "cbm_project": cbm_proj}
    for name, q in queries.items():
        try:
            text, err = await _cbm_cli_text(
                "cli", "query_graph",
                json.dumps({"project": cbm_proj, "query": q}))
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": str(e)}, status=500)
        if err:
            return web.json_response({"error": err, "at": name}, status=502)
        # query_graph emite `rows: N  (cols: a b)` → lo toma el parser genérico.
        out[name] = _serialize(_parse_cbm_sections(text).get("rows", []))
    return web.json_response(out)


# ── Iter 10.4: Diagramas generados por LLM ──
# El pipeline mecánico (cbm raw → mapeo JS → mermaid) produce ovillos
# ilegibles en proyectos legacy con 300+ símbolos. Este endpoint pasa
# los mismos datos crudos por un LLM (MiniMax M3 via run_consult) que
# interpreta, agrupa por dominio, filtra ruido y anota cada componente.
# Mismo principio que el modo compact de skills: pagar 1 turno de LLM
# (~2K tokens) por diagrama a cambio de un output que sirve para entender
# el sistema, no solo para una demo.

#: Tipos de diagrama que el LLM puede generar.
_DIAGRAM_TYPES: dict[str, dict] = {
    "architecture": {
        "label": "Arquitectura",
        "aspects": ["layers", "boundaries", "clusters"],
        "prompt_tail": (
            "El diagrama debe mostrar las capas lógicas del sistema, los "
            "límites entre módulos y el peso de las llamadas entre ellos. "
            "Agrupa por dominio o bounded context real (NO por paquete de "
            "código). Cada subgraph debe tener un título que describa QUÉ "
            "hace ese grupo, no cómo se llama el namespace. Las aristas "
            "deben mostrar el conteo de llamadas. Usa flowchart LR."
        ),
    },
    "hotspots": {
        "label": "Hotspots",
        "aspects": ["hotspots"],
        "prompt_tail": (
            "Muestra los símbolos más referenciados del sistema, agrupados "
            "por dominio real (no por paquete). Cada nodo debe decir qué "
            "hace (no solo el nombre de la clase). Resalta los 3 más "
            "críticos con estilo. Usa flowchart LR. Si hay más de 15 "
            "símbolos, muestra solo los más significativos y anota cuántos "
            "quedaron fuera."
        ),
    },
    "modules": {
        "label": "Módulos",
        "aspects": ["clusters"],
        "prompt_tail": (
            "Muestra los módulos que el grafo detecta por cohesión de "
            "llamadas. Para cada módulo, explica qué responsabilidad "
            "tiene (no solo su nombre). Muestra los 2-3 archivos o "
            "símbolos más representativos de cada módulo. Usa flowchart TD."
        ),
    },
    "routes": {
        "label": "Rutas",
        "aspects": ["routes"],
        "prompt_tail": (
            "Muestra la superficie HTTP del sistema agrupada por recurso "
            "(no por controlador). Cada grupo debe decir qué dominio "
            "expone. Los métodos HTTP deben ser explícitos (GET, POST, "
            "PUT, DELETE). Si hay más de 25 rutas, agrúpalas y muestra "
            "solo las más representativas con un conteo de las omitidas. "
            "Usa flowchart LR."
        ),
    },
    "classes": {
        "label": "Clases",
        "special": "classes",  # usa _GRAPH_KINDS en vez de aspects
        "prompt_tail": (
            "Genera un classDiagram de Mermaid con las clases más "
            "importantes del sistema (máximo 12). Para cada clase, "
            "muestra sus métodos más representativos (máximo 5). Muestra "
            "las relaciones de herencia e implementación ENTRE las clases "
            "dibujadas. Agrupa las clases por dominio con comentarios "
            "`%% dominio: xxx`. NO dibujes clases de test."
        ),
    },
    "states": {
        "label": "Estados",
        "special": "states",
        "prompt_tail": (
            "Muestra los enums o tipos de estado del sistema y qué "
            "componentes los consumen. Agrupa por dominio. Para cada "
            "enum, indica cuántos consumidores tiene (no los nombres "
            "de todos, solo los 2-3 más relevantes). Usa flowchart LR. "
            "NO inventes transiciones entre estados: solo muestra "
            "quién usa cada enum."
        ),
    },
    "sequence": {
        "label": "Secuencia",
        "special": "sequence",
        "prompt_tail": (
            "Genera un sequenceDiagram de Mermaid que muestre el camino "
            "de llamadas real de la función. Incluye los callers (quién "
            "la llama) y los callees (a quién llama), ordenados por "
            "profundidad (hop). Cada participante debe tener un alias "
            "corto y legible. Usa autonumber."
        ),
    },
}


def _build_diagram_system_prompt(
    data: dict, diagram_type: str, user_prompt: str,
) -> str:
    """Arma el system prompt que le pasamos al LLM para generar el diagrama.

    Estructura:
      1. Rol: sos un arquitecto de software generando documentación.
      2. Datos crudos: el JSON/texto que cbm devolvió.
      3. Instrucciones: tipo de diagrama + reglas de formato + instructions tail.
      4. User prompt (opcional): foco adicional ("solo el flujo de pagos").
    """
    meta = _DIAGRAM_TYPES.get(diagram_type, {})
    label = meta.get("label", diagram_type)
    prompt_tail = meta.get("prompt_tail", "")

    data_str = json.dumps(data, ensure_ascii=False, indent=2)
    # Cap defensivo: un grafo de 3000 símbolos no cabe en el context.
    if len(data_str) > 24_000:
        data_str = data_str[:24_000] + (
            f"\n\n... [truncado a 24KB; total original {len(data_str):,} chars]")

    user_line = ""
    if user_prompt.strip():
        user_line = (
            f"\n\nEl usuario pide esto específicamente: «{user_prompt.strip()}». "
            "Enfócate en eso. Si el diagrama resultante no cubre otras áreas, "
            "está bien: el usuario quiere precisión, no cobertura."
        )

    return (
        f"Eres un arquitecto de software senior generando un diagrama "
        f"de {label} para documentación técnica. Tu output va a ser leído "
        f"por desarrolladores que necesitan entender este sistema rápido.\n\n"
        f"## Datos del grafo de código\n\n"
        f"Estos datos vienen del índice semántico (codebase-memory) del "
        f"proyecto. Úsalos como fuente de verdad. No inventes nombres, "
        f"relaciones ni métricas que no estén aquí.\n\n"
        f"```json\n{data_str}\n```\n\n"
        f"## Instrucciones\n\n"
        f"1. Genera SOLO el diagrama Mermaid, dentro de un fence "
        f"```mermaid ... ```.\n"
        f"2. {prompt_tail}\n"
        f"3. Usa nombres legibles: si un qualified_name es "
        f"'Com.Proyecto.Modulo.Clase', muéstralo como 'Clase' y "
        f"anota el namespace si aporta contexto.\n"
        f"4. Filtra ruido: builtins (str, list, len, console, Object), "
        f"clases de test, DTOs vacíos, interfaces sin implementaciones, "
        f"y archivos de configuración no aportan al diagrama.\n"
        f"5. Después del fence ```, escribe 2-4 bullets explicando "
        f"QUÉ muestra el diagrama y qué decisiones de filtrado tomaste. "
        f"Sé honesto: si el grafo no tenía suficiente información para "
        f"un aspecto, dilo ('no se detectaron boundaries claros entre "
        f"estos módulos').\n"
        f"6. IMPORTANTE: solo quiero el fence mermaid y los bullets. "
        f"Nada de '¡Por supuesto!' ni introducciones. Directo al diagrama."
        f"{user_line}"
    )


def _extract_mermaid(text: str) -> tuple[str, str]:
    """Saca el bloque ```mermaid y la explicación (bullets después del fence).

    Devuelve (mermaid_code, explanation). Si no hay fence, intenta usar
    todo el texto como mermaid (el LLM a veces omite el fence).
    """
    m = re.search(r"```mermaid\s*\n(.*?)```", text, re.DOTALL)
    if m:
        code = m.group(1).strip()
        explanation = text[m.end():].strip()
        return code, explanation
    # Fallback: si no hay fence, asumimos que todo es mermaid.
    return text.strip(), ""


async def api_project_diagrams_llm(request: web.Request) -> web.Response:
    """POST /admin/api/projects/{slug}/diagrams/llm

    Body JSON: {type, limit?, prompt?, function?}

    Genera un diagrama Mermaid interpretado por LLM a partir de los
    datos crudos del grafo de cbm. A diferencia del pipeline mecánico
    (que vuelca los datos sin interpretar), el LLM agrupa por dominio,
    filtra ruido, anota componentes y responde a prompts del usuario.
    """
    slug = request.match_info["slug"]
    db = request.app[DB_KEY]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    # La validación del PEDIDO va ANTES que la del SERVICIO (2026-08-16).
    # Estaba al revés: sin cbm instalado, un body malformado se contestaba
    # con 503 "cbm no instalado" — un error que le echa la culpa al
    # servidor por algo que el cliente mandó mal, y que además cambia
    # según la máquina. 400 y 503 responden preguntas distintas: "¿está
    # bien lo que mandaste?" y "¿puedo atenderte ahora?". Parsear el body
    # es barato y no tiene efectos, así que se responde primero.
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)

    diagram_type = (body.get("type") or "").strip()
    if diagram_type not in _DIAGRAM_TYPES:
        return web.json_response(
            {"error": f"type inválido; usa: {sorted(_DIAGRAM_TYPES)}"},
            status=400)

    if not cbm_binary_path():
        return web.json_response({"error": "cbm no instalado"}, status=503)

    user_prompt = (body.get("prompt") or "").strip()
    fn_name = (body.get("function") or "").strip()
    meta = _DIAGRAM_TYPES[diagram_type]
    cbm_proj = _cbm_project_name(project["repo_path"])

    # ── Paso 1: obtener datos crudos de cbm ──
    special = meta.get("special")

    if special == "sequence":
        if not fn_name:
            return web.json_response(
                {"error": "sequence requiere ?function="}, status=400)
        try:
            text, err = await _cbm_cli_text(
                "cli", "trace_call_path",
                json.dumps({"project": cbm_proj, "function_name": fn_name}))
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
        if err:
            return web.json_response({"error": err}, status=502)
        raw_data: dict = {
            "function": fn_name,
            "raw_text": text[:12_000],  # cap: el texto de cbm puede ser largo
        }
        # Si es ambiguo, pasamos las sugerencias para que el LLM explique.
        if text.lstrip().startswith("{"):
            try:
                j = json.loads(text.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                j = {}
            if j.get("status") == "ambiguous":
                raw_data["ambiguous"] = True
                raw_data["suggestions"] = j.get("suggestions", [])[:20]

    elif special in ("classes", "states"):
        queries = _GRAPH_KINDS.get(special, {})
        raw_data = {"kind": special}
        for name, q in queries.items():
            try:
                text, err = await _cbm_cli_text(
                    "cli", "query_graph",
                    json.dumps({"project": cbm_proj, "query": q}))
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)
            if err:
                return web.json_response({"error": err, "at": name}, status=502)
            raw_data[name] = _serialize(
                _parse_cbm_sections(text).get("rows", []))
    else:
        aspects = meta.get("aspects", [])
        try:
            text, err = await _cbm_cli_text(
                "cli", "get_architecture",
                json.dumps({"project": cbm_proj, "aspects": aspects}))
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
        if err:
            return web.json_response({"error": err}, status=502)
        raw_data = _serialize(_parse_cbm_sections(text))
        raw_data["aspects"] = aspects

    # ── Paso 2: system prompt + LLM ──
    from . import experts

    system_prompt = _build_diagram_system_prompt(
        raw_data, diagram_type, user_prompt)

    try:
        result = await experts.run_consult(
            user=f"diagram:{diagram_type}",
            system_prompt=system_prompt,
            db=db,
        )
    except experts.ModelUnavailable as e:
        return web.json_response(
            {"error": f"modelo no disponible: {e}"}, status=503)
    except Exception as e:
        logger.exception("diagrams/llm: run_consult falló")
        return web.json_response({"error": str(e)}, status=500)

    content = result.get("content", "")
    mermaid_code, explanation = _extract_mermaid(content)

    # `run_consult` devuelve tokens_in/tokens_out planos, NO un dict
    # `usage` (bug 2026-07-26: leerlo como usage["input_tokens"] daba
    # None siempre y la UI nunca mostraba el costo del diagrama).
    return web.json_response({
        "slug": slug,
        "type": diagram_type,
        "mermaid": mermaid_code,
        "explanation": explanation,
        "tokens_in": result.get("tokens_in"),
        "tokens_out": result.get("tokens_out"),
        "model": result.get("model", ""),
    })


async def api_index_files(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug}/index/files?limit=N&sort=path|name."""
    slug = request.match_info["slug"]
    db = request.app[DB_KEY]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    if not cbm_binary_path():
        return web.json_response({"error": "cbm no instalado"}, status=503)

    limit = int(request.query.get("limit", "200"))
    sort = request.query.get("sort", "path")

    # cbm 0.8.1 nombra proyectos normalizando el path absoluto a slug.
    cbm_proj = _cbm_project_name(project["repo_path"])

    try:
        text, err = await _cbm_cli_text(
            "cli", "search_graph",
            json.dumps({"label": "File", "project": cbm_proj,
                        "limit": min(limit, 500)}),
        )
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": str(e)}, status=500)

    if err:
        return web.json_response({"error": err, "cbm_project": cbm_proj},
                                 status=502)

    files, total, has_more = _parse_cbm_search_graph(text)

    if sort == "name":
        files.sort(key=lambda x: x["name"].lower())
    else:
        files.sort(key=lambda x: x["path"].lower())

    return web.json_response({
        "files": _serialize(files),
        "total_shown": len(files),
        "limit": limit,
        "total_indexed": total or len(files),
        "has_more": has_more,
        "cbm_project": cbm_proj,
    })


async def api_index_bulk(request: web.Request) -> web.Response:
    """POST /admin/api/projects/index/bulk — kick async para N repos.

    Body JSON (cualquiera de las dos formas):
      {"slugs": ["inventorydemo","sample-app","commercedemo"]}
      {"root_path": "C:/Users/demo/source/repos", "recurse": 2,
       "skip": ["AuroraDemo"]}
      {"force": false, "concurrency": 2, ...}    # cualquiera combinado

    Devuelve 202 con {"job_id", "status":"running", "queued": N}.
    El progreso se consulta en GET /admin/api/reindex/{job_id} (mismo endpoint
    que reindex single — payload con campos extra: total, done, ok, fail,
    per_repo, eta_s).
    """
    db = request.app[DB_KEY]
    if not cbm_binary_path():
        return web.json_response({"error": "cbm no instalado"}, status=503)

    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    body = body or {}

    force = bool(body.get("force", False))
    concurrency = max(1, min(int(body.get("concurrency", 2)), 6))

    # Resolver la lista de paths a indexar.
    slugs: list[str] = list(body.get("slugs") or [])
    explicit_paths: list[str] = list(body.get("paths") or [])
    skip_set = {s.lower() for s in (body.get("skip") or [])}

    paths: list[str] = []
    if explicit_paths:
        # Modo UI: el usuario pickeó desde el file browser.
        # Validamos que sean dirs reales (404 si alguno no existe).
        bad = [p for p in explicit_paths if not Path(p).is_dir()]
        if bad:
            return web.json_response(
                {"error": "paths inexistentes", "bad": bad}, status=400,
            )
        paths = [str(Path(p)) for p in explicit_paths]
    elif slugs:
        for slug in slugs:
            project = await db.get_project(slug)
            if not project:
                return web.json_response(
                    {"error": f"slug desconocido: {slug}"}, status=404,
                )
            paths.append(project["repo_path"])
    elif "root_path" in body:
        root = Path(str(body["root_path"]))
        recurse = max(1, min(int(body.get("recurse", 1)), 4))
        # Reusamos el patrón de discover_repos (duplicado chico, evita
        # acoplar admin.py al script CLI).
        repo_markers = (".git", "*.sln", "*.csproj", "*.fsproj",
                        "package.json", "pyproject.toml", "Cargo.toml",
                        "go.mod", "pom.xml", "build.gradle",
                        "build.gradle.kts")
        no_enter = {".git", "node_modules", ".venv", "venv", "obj", "bin",
                    "dist", "build", "__pycache__", ".idea", ".vscode",
                    "TestResults", ".gradle", "target", ".terraform"}

        def _walk(d: Path, depth: int) -> None:
            try:
                entries = sorted(d.iterdir())
            except (PermissionError, OSError):
                return
            for child in entries:
                if not child.is_dir() or child.name.lower() in skip_set:
                    continue
                is_repo = False
                for marker in repo_markers:
                    if marker.startswith("*."):
                        if any(child.glob(marker[1:])):
                            is_repo = True
                            break
                    elif (child / marker).exists():
                        is_repo = True
                        break
                if is_repo:
                    paths.append(str(child))
                    continue
                if depth < recurse and child.name not in no_enter:
                    _walk(child, depth + 1)

        _walk(root, 1)
    else:
        # Default: indexar TODOS los projects de la DB que estén enabled
        # Y con include_in_index=1 (toggle del admin UI).
        all_projects = await db.list_projects(enabled_only=True)
        paths = [p["repo_path"] for p in all_projects
                 if p.get("include_in_index", 1)]

    if not paths:
        return web.json_response({"error": "no hay paths para indexar"}, status=400)

    job_id = f"bulk_{int(asyncio.get_event_loop().time() * 1000) % 100000}"
    initial = {
        "job_id": job_id,
        "status": "running",
        "kind": "bulk",
        "total": len(paths),
        "done": 0,
        "ok": 0,
        "fail": 0,
        "skip": 0,
        "concurrency": concurrency,
        "force": force,
        "started_at": time.time(),
        "per_repo": {},  # path -> {status, nodes, edges, elapsed_s, error}
        "_task": None,    # se setea abajo, ignorado en el JSON de salida
    }
    _set_job(job_id, initial)

    sem = asyncio.Semaphore(concurrency)

    async def _one(path: str) -> None:
        async with sem:
            t0 = time.monotonic()
            try:
                proc = await asyncio.create_subprocess_exec(
                    cbm_binary_path(), "cli", "index_repository",
                    json.dumps({"repo_path": path, "force": force}),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=_cbm_env(),
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=CBM_INDEX_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                initial["per_repo"][path] = {
                    "status": "TIMEOUT",
                    "elapsed_s": time.monotonic() - t0,
                    "error": f">{CBM_INDEX_TIMEOUT_S}s",
                }
                initial["fail"] += 1
                initial["done"] += 1
                return
            elapsed = time.monotonic() - t0
            last_json = ""
            for line in (stdout or b"").decode("utf-8", errors="replace").splitlines():
                s = line.strip()
                if s.startswith("{"):
                    last_json = s
            if proc.returncode != 0 or not last_json:
                initial["per_repo"][path] = {
                    "status": "FAIL",
                    "elapsed_s": elapsed,
                    "error": ((stderr or b"").decode("utf-8", errors="replace") or
                              last_json or "no JSON")[:300],
                }
                initial["fail"] += 1
                initial["done"] += 1
                return
            try:
                data = json.loads(last_json)
            except json.JSONDecodeError:
                initial["per_repo"][path] = {
                    "status": "FAIL", "elapsed_s": elapsed,
                    "error": "json inválido de cbm",
                }
                initial["fail"] += 1
                initial["done"] += 1
                return
            if data.get("error"):
                initial["per_repo"][path] = {
                    "status": "FAIL", "elapsed_s": elapsed,
                    "error": str(data["error"])[:300],
                }
                initial["fail"] += 1
            else:
                initial["per_repo"][path] = {
                    "status": "OK",
                    "elapsed_s": elapsed,
                    "nodes": data.get("nodes", 0),
                    "edges": data.get("edges", 0),
                }
                initial["ok"] += 1
            initial["done"] += 1
            # Estimación naive: promedio móvil sobre los ya corridos.
            done = initial["done"]
            if done > 0:
                avg = sum(
                    pr["elapsed_s"] for pr in initial["per_repo"].values()
                    if "elapsed_s" in pr
                ) / max(done, 1)
                initial["eta_s"] = round(avg * (initial["total"] - done), 1)

    async def _run_all() -> None:
        await asyncio.gather(*[_one(p) for p in paths])
        initial["status"] = "done" if initial["fail"] == 0 else "partial"
        initial["finished_at"] = time.time()
        initial["elapsed_total_s"] = round(
            initial["finished_at"] - initial["started_at"], 1,
        )
        # Liberamos la referencia al task en el dict para que el JSON de salida
        # no lo incluya.
        initial["_task"] = None

    # Guardamos el task DENTRO del dict. api_reindex_status lo lee vía
    # `_task is not None and not done` para saber si sigue corriendo. Al
    # terminar, el dict queda con status final listo para devolver y el
    # done_callback arma el TTL de purga del resultado.
    bulk_task = asyncio.create_task(_run_all())
    bulk_task.add_done_callback(lambda _t: _mark_job_done(job_id))
    initial["_task"] = bulk_task

    return web.json_response(
        {"job_id": job_id, "status": "running", "queued": len(paths),
         "concurrency": concurrency, "force": force},
        status=202,
    )


# ---------- system_config (red + paths, editable desde la UI) ----------

# Whitelist estricta: solo estas claves se tocan desde la UI.
#: Rol del runner por etapas → clave de system_config que fija su
#: modelo. Vacío = cae por la cascada de `config._staged_model_spec`
#: (system_config → .env → compactor → ejecutor), que es el
#: comportamiento que había antes de que esto fuera editable.
#:
#: Existía solo por .env, y un .env se edita a mano y pide reiniciar el
#: relay; system_config lo pisa y aplica en el próximo run.
_MODEL_ROLE_KEYS = {
    "executor": "FOURBIS_MODEL",
    "planner": "FOURBIS_PLANNER_MODEL",
    "verifier": "FOURBIS_VERIFIER_MODEL",
    "documenter": "FOURBIS_DOCUMENTER_MODEL",
    "compactor": "FOURBIS_COMPACTOR_MODEL",
}

_EDITABLE_CONFIG_KEYS = (*relay_config.PANEL_SETTINGS,
                         *_MODEL_ROLE_KEYS.values())
_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SECRET_PREFIX = "secret:"


def _public_panel_settings(stored: dict[str, str]) -> tuple[dict, dict]:
    """Schema + state for Config, without serializing secret values."""
    schema: dict[str, dict] = {}
    states: dict[str, dict] = {}
    for key, raw_meta in relay_config.PANEL_SETTINGS.items():
        meta = dict(raw_meta)
        kind = meta.get("type")
        # A default is useful to the form, except where it would invite a
        # caller to mistake a secret's state for a readable value.
        schema[key] = meta
        if kind == "secret":
            states[key] = {"configured": bool(stored.get(_SECRET_PREFIX + key))}
        else:
            states[key] = {"value": stored.get(key, str(meta.get("default", "")))}

    # Dynamic MCP credentials use the same public shape.  The name is not
    # sensitive; the stored value never leaves this function.
    for key, value in stored.items():
        if key.startswith(_SECRET_PREFIX):
            name = key[len(_SECRET_PREFIX):]
            states.setdefault(name, {"configured": bool(value)})
    return schema, states


def _number_is_integral(meta: Mapping[str, object]) -> bool:
    """Counters and ports reject fractions; explicitly fractional bounds don't."""
    return all(not isinstance(meta.get(k), float) for k in ("default", "min", "max"))


def _validate_panel_value(key: str, raw: object, meta: Mapping[str, object]) -> tuple[str, str]:
    """Normalize one public Config value without leaking its input in errors."""
    kind = meta.get("type")
    if kind == "boolean":
        if raw is True or raw in (1, "1", "true", "on"):
            return "1", ""
        if raw is False or raw in (0, "0", "false", "off"):
            return "0", ""
        return "", f"{key}: booleano inválido"
    if kind == "number":
        if isinstance(raw, bool):
            return "", f"{key}: número inválido"
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return "", f"{key}: número inválido"
        if not math.isfinite(number):
            return "", f"{key}: número finito requerido"
        if _number_is_integral(meta) and not number.is_integer():
            return "", f"{key}: entero requerido"
        minimum, maximum = meta.get("min"), meta.get("max")
        if minimum is not None and number < float(minimum):
            return "", f"{key}: mínimo {minimum}"
        if maximum is not None and number > float(maximum):
            return "", f"{key}: máximo {maximum}"
        return (str(int(number)) if _number_is_integral(meta) else str(number)), ""
    if not isinstance(raw, str):
        return "", f"{key}: texto requerido"
    value = raw.strip()
    if kind == "url" and value:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return "", f"{key}: URL http/https requerida"
    if kind == "path" and key == "FOURBIS_REPOS_ROOT" and value:
        if not Path(value).expanduser().is_dir():
            return "", f"{key}: directorio inexistente"
    if key == "DISCORD_GUILD_ID" and value and not value.isdigit():
        return "", "DISCORD_GUILD_ID: número requerido"
    return value, ""


def _validate_model_prices(raw: str) -> tuple[str, str]:
    """Valida el JSON de tarifas → `(normalizado, error)`.

    Entrada de la Admin UI: se valida forma y rango acá, no al leerlo.
    Un JSON roto guardado sin chequear haría que las métricas dejen de
    calcular costo en silencio y sin decir por qué.

    Vacío es válido: significa "sin tarifas", y deja los costos en null.
    """
    txt = (raw or "").strip()
    if not txt:
        return "", ""
    try:
        data = json.loads(txt)
    except (json.JSONDecodeError, TypeError) as e:
        return "", f"JSON inválido: {e}"
    if not isinstance(data, dict):
        return "", "se esperaba un objeto {modelo: {in, out}}"
    for spec, val in data.items():
        if not isinstance(spec, str) or not spec.strip():
            return "", "hay una clave de modelo vacía"
        if not isinstance(val, dict):
            return "", f"{spec}: se esperaba {{in, out}}"
        for k in ("in", "out"):
            if k not in val:
                return "", f"{spec}: falta '{k}'"
        for k, v in val.items():
            if k not in ("in", "out", "ref_in", "ref_out"):
                return "", f"{spec}: clave desconocida {k!r}"
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                return "", f"{spec}.{k}: se esperaba un número"
            if v < 0:
                return "", f"{spec}.{k}: no puede ser negativo"
    return json.dumps(data, ensure_ascii=False), ""


async def _roles_que_usan(db, spec: str) -> list[str]:
    """Referencias globales o de proyecto que apuntan a `spec`.

    La contracara de `_validate_model_role` (2026-08-26). Aquella valida
    al ESCRIBIR el rol; esta valida al borrar o apagar el modelo, que es
    el otro lado por donde se rompe la misma invariante.

    Sin esto el rol queda huérfano y el síntoma sale lejísimos de la
    causa: `build_model` no falla —para un provider con base_url fija
    arma el objeto igual— así que el error recién aparece cuando el
    proveedor rechaza un nombre de modelo que no existe, y se reporta
    como `provider_error`. O sea que borrar un modelo un martes se
    manifiesta el jueves como "se cayó minimax".
    """
    txt = (spec or "").strip()
    if not txt:
        return []
    cfg = await db.all_config()
    usados = {rol for rol, key in _MODEL_ROLE_KEYS.items()
              if (cfg.get(key) or "").strip() == txt}
    efectivos = {
        "executor": relay_config.model_spec,
        "planner": relay_config.planner_model_spec,
        "verifier": relay_config.verifier_model_spec,
        "documenter": relay_config.documenter_model_spec,
        "compactor": relay_config.compactor_model_spec,
    }
    for rol, getter in efectivos.items():
        if getter() == txt:
            usados.add(rol)
    if txt in (v.strip() for v in
               os.environ.get("FOURBIS_PLANNER_FALLBACK", "").split(",")):
        usados.add("planner_fallback")
    for project in await db.list_projects(enabled_only=False):
        defaults = project.get("defaults_json") or {}
        slug = project.get("slug") or "?"
        for rol in ("model", "planner_model", "graph_planner_model",
                    "verifier_model", "documenter_model"):
            if str(defaults.get(rol) or "").strip() == txt:
                usados.add(f"project:{slug}:{rol}")
        fallback = defaults.get("planner_fallback") or []
        if isinstance(fallback, str):
            fallback = fallback.split(",")
        elif not isinstance(fallback, (list, tuple)):
            fallback = []
        if txt in (str(v).strip() for v in fallback):
            usados.add(f"project:{slug}:planner_fallback")
    return sorted(usados)


async def _validate_model_role(db, spec: str) -> str:
    """`""` si el spec sirve para un rol, si no el motivo del rechazo.

    Vacío es válido: significa "cae por la cascada". Lo que NO puede
    pasar es guardar un spec inexistente o apagado — el rol quedaría
    tirando `ModelUnavailable` en cada run y el síntoma aparecería
    lejos de este form.
    """
    txt = (spec or "").strip()
    if not txt:
        return ""
    fila = await db.get_model(txt)
    if fila is None:
        return f"{txt}: no está en el catálogo de modelos"
    if not fila.get("enabled"):
        return f"{txt}: está apagado en el catálogo"
    return ""


#: Solo las que NO son "" cuando no hay fila en system_config.
_CONFIG_DEFAULTS = {"RELAY_HOST": "127.0.0.1"}
_VALID_RELAY_HOSTS = ("127.0.0.1", "0.0.0.0")


async def api_config_get(request: web.Request) -> web.Response:
    """GET /admin/api/config — system_config + valores efectivos.

    `config` son las filas de la tabla (con defaults aplicados);
    `effective` es lo que el proceso está usando AHORA: el bind real
    (RELAY_HOST recién aplica al próximo arranque) y el repos_root
    resuelto por config.repos_root().
    """
    db = request.app[DB_KEY]
    stored = await db.all_config()
    bind_host = request.app.get(BIND_HOST_KEY) or "127.0.0.1"
    return web.json_response({
        # Una clave por entrada de la whitelist: si se agrega una editable
        # y no se devuelve acá, el form la guarda pero nunca la muestra de
        # vuelta (pasó con GITHUB_BOARD_* el 2026-08-01).
        "config": {k: stored.get(k, _CONFIG_DEFAULTS.get(k, ""))
                   for k in _EDITABLE_CONFIG_KEYS},
        "effective": {
            "bind_host": bind_host,
            "localhost_guard_active": True,
            "repos_root": relay_config.repos_root(),
            "version": RELAY_VERSION,
        },
        "editable_keys": list(_EDITABLE_CONFIG_KEYS),
        "restart_required_keys": ["RELAY_HOST"],
        # Qué modelo resuelve cada rol AHORA. Un rol vacío en el form no
        # dice nada por sí solo: hay que saberse la cascada de memoria
        # (system_config → .env → compactor → ejecutor) para entender con
        # qué va a correr. Acá se ve el resultado.
        "model_roles": {
            "keys": dict(_MODEL_ROLE_KEYS),
            "effective": {
                "executor": relay_config.model_spec(),
                "planner": relay_config.planner_model_spec(),
                "verifier": relay_config.verifier_model_spec(),
                "documenter": relay_config.documenter_model_spec(),
                "compactor": relay_config.compactor_model_spec(),
            },
        },
    })


async def api_config_put(request: web.Request) -> web.Response:
    """PUT /admin/api/config — body {clave: valor}, whitelist estricta.

    RELAY_HOST toma efecto en el próximo arranque (el bind de aiohttp
    no se puede cambiar en vivo); FOURBIS_REPOS_ROOT aplica al instante
    (refresca el snapshot runtime).
    """
    db = request.app[DB_KEY]
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    if not isinstance(body, dict) or not body:
        return web.json_response({"error": "body vacío"}, status=400)

    unknown = sorted(k for k in body if k not in _EDITABLE_CONFIG_KEYS)
    if unknown:
        return web.json_response(
            {"error": f"claves no editables: {', '.join(unknown)}",
             "editable_keys": list(_EDITABLE_CONFIG_KEYS)},
            status=400,
        )

    changed: dict[str, str] = {}
    if "RELAY_HOST" in body:
        host = str(body["RELAY_HOST"]).strip()
        if host not in _VALID_RELAY_HOSTS:
            return web.json_response(
                {"error": f"RELAY_HOST inválido: {host!r}",
                 "valid": list(_VALID_RELAY_HOSTS)},
                status=400,
            )
        changed["RELAY_HOST"] = host
    if "FOURBIS_REPOS_ROOT" in body:
        root = str(body["FOURBIS_REPOS_ROOT"]).strip()
        if root and not Path(root).is_dir():
            return web.json_response(
                {"error": f"no es un directorio: {root}"}, status=400)
        changed["FOURBIS_REPOS_ROOT"] = root
    # Tablero de empresa (fase 5). El owner no se valida contra GitHub
    # acá: guardar config no debería depender de que `gh` conteste. Si
    # está mal, el panel lo dice cuando lo abrís.
    if "MODEL_PRICES" in body:
        norm, err = _validate_model_prices(str(body["MODEL_PRICES"] or ""))
        if err:
            return web.json_response({"error": f"MODEL_PRICES: {err}"},
                                     status=400)
        changed["MODEL_PRICES"] = norm
    if "GITHUB_BOARD_OWNER" in body:
        changed["GITHUB_BOARD_OWNER"] = str(body["GITHUB_BOARD_OWNER"]).strip()
    if "GITHUB_BOARD_NUMBER" in body:
        raw = str(body["GITHUB_BOARD_NUMBER"]).strip()
        if raw and not raw.isdigit():
            return web.json_response(
                {"error": f"GITHUB_BOARD_NUMBER debe ser un número: {raw!r}"},
                status=400)
        changed["GITHUB_BOARD_NUMBER"] = raw
    # Guild de Discord (snowflake numérico). Vacío = limpiar. Habilita el
    # link al canal en el tab Proyectos (discord.com/channels/<guild>/<ch>).
    if "DISCORD_GUILD_ID" in body:
        raw = str(body["DISCORD_GUILD_ID"]).strip()
        if raw and not raw.isdigit():
            return web.json_response(
                {"error": f"DISCORD_GUILD_ID debe ser un número: {raw!r}"},
                status=400)
        changed["DISCORD_GUILD_ID"] = raw
    # Modelo por rol del runner por etapas. Vacío = cascada.
    for _rol, _key in _MODEL_ROLE_KEYS.items():
        if _key not in body:
            continue
        _spec = str(body[_key] or "").strip()
        _err = await _validate_model_role(db, _spec)
        if _err:
            return web.json_response({"error": f"{_rol}: {_err}"}, status=400)
        changed[_key] = _spec

    for k, v in changed.items():
        await db.set_config(k, v)
    # Refresca el snapshot en memoria: repos_root aplica al instante.
    relay_config.set_runtime_config(await db.all_config())

    bind_host = request.app.get(BIND_HOST_KEY) or "127.0.0.1"
    restart_required = ("RELAY_HOST" in changed
                        and changed["RELAY_HOST"] != bind_host)
    return web.json_response({
        "saved": changed,
        "restart_required": restart_required,
    })


# ---------- orphans: cbm projects sin fila en projects ----------

def _has_git(path: Path) -> bool:
    return (path / ".git").exists()


# ---------- commands: CRUD sobre tabla `commands` ----------

async def api_commands_list(request: web.Request) -> web.Response:
    """GET /admin/api/commands — lista todos los comandos dinámicos."""
    db = request.app[DB_KEY]
    cmds = await db.list_commands(enabled_only=False)
    # Si la DB está vacía pero el registry en memoria tiene comandos built-in,
    # los devolvemos igual (los built-in se cargan en _on_startup).
    if not cmds:
        from .commands import CommandRegistry
        reg: CommandRegistry | None = request.app.get(COMMANDS_KEY)
        if reg is not None:
            # El registry tiene dispatch() pero no un dump. Listamos por nombre.
            for n in reg.names():
                cmds.append({
                    "name": n, "description": "(built-in)",
                    "handler": "(built-in)", "args_schema": None,
                    "enabled": True,
                })
    return web.json_response({"commands": _serialize(cmds)})


async def api_commands_upsert(request: web.Request) -> web.Response:
    """POST /admin/api/commands — crea o actualiza un comando.

    Body: {name, description, handler, args_schema, enabled?}.

    `handler` es un dotted path Python del estilo `relay.handlers.foo`
    o `relay.commands.builtin_build`. El server NO verifica que
    importe — eso lo hace `CommandRegistry.load_from_db` al recibir
    una conexión nueva. Si el import falla, el comando queda en la
    DB pero no se ejecuta; desde esta UI vas a ver el error.
    """
    db = request.app[DB_KEY]
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    body = body or {}
    name = (body.get("name") or "").strip()
    if not name:
        return web.json_response({"error": "name requerido"}, status=400)
    handler = (body.get("handler") or "").strip()
    if not handler:
        return web.json_response({"error": "handler requerido"}, status=400)
    cmd = {
        "name": name,
        "description": (body.get("description") or "").strip(),
        "handler": handler,
        "args_schema": body.get("args_schema"),
        "enabled": bool(body.get("enabled", True)),
    }
    await db.upsert_command(cmd)
    return web.json_response({"command": _serialize(cmd)}, status=201)


async def api_commands_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/commands/{name} — borra un comando por nombre."""
    db = request.app[DB_KEY]
    name = request.match_info["name"]
    deleted = await db.delete_command(name)
    if not deleted:
        return web.json_response({"error": "no encontrado"}, status=404)
    return web.json_response({"deleted": True, "name": name})


async def api_commands_run(request: web.Request) -> web.Response:
    """POST /admin/api/commands/{name}/run — corre el comando en el relay.

    Body: {args: {...}}. Útil para probar un comando desde la UI sin
    pasar por Discord. Devuelve {output: str, duration_ms: int}.
    """
    from .commands import CommandContext
    db = request.app[DB_KEY]
    reg: CommandRegistry | None = request.app.get(COMMANDS_KEY)
    if reg is None:
        return web.json_response({"error": "registry no disponible"}, status=503)
    name = request.match_info["name"]
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    body = body or {}
    args = body.get("args") or {}
    # No hay `project` acá a propósito: la UI mandaba uno y este handler
    # lo leía en una variable que no usaba nadie, así que el campo del
    # form no hacía nada. No se puede arreglar pasándolo al contexto —
    # cada comando pide su proyecto con SU clave (`build` usa `project`,
    # `memoria` usa `target`, `cancel` usa `chat`), así que el proyecto
    # viaja dentro de `args` y el placeholder de la UI lo dice.
    t0 = time.monotonic()
    try:
        out = await reg.dispatch(name, args, CommandContext(
            db=db, sessions=request.app.get(SESSIONS_KEY),
            source="admin-ui",
        ))
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": str(e)}, status=500)
    dur = int((time.monotonic() - t0) * 1000)
    return web.json_response({"output": out, "duration_ms": dur})


# ---------- MCPs (plan MCP_REGISTRY F3: CRUD del catálogo) ----------

# Campos editables desde la UI. health/vet_* los setea el sistema
# (pool F1 / pipeline de install F2), no el usuario.
_MCP_EDITABLE = ("capability", "transport", "command", "args", "url", "env",
                 "read_only", "on_demand", "idle_timeout_s", "enabled")


async def _mcp_with_links(db) -> list[dict]:
    rows = await db.list_mcp_servers()
    links = await db.run(
        "SELECT l.mcp_id, p.slug FROM project_mcp_servers l "
        "JOIN projects p ON p.id = l.project_id ORDER BY p.slug")
    by_mcp: dict[int, list[str]] = {}
    for ln in links:
        by_mcp.setdefault(ln["mcp_id"], []).append(ln["slug"])
    for r in rows:
        # sin links = global (sirve a todos los proyectos)
        r["project_slugs"] = by_mcp.get(r["id"], [])
    return rows


async def _mcp_set_links(db, mcp_id: int, slugs: list) -> str | None:
    """Reemplaza los links de un MCP. Devuelve mensaje de error o None."""
    ids = []
    for slug in slugs:
        p = await db.get_project(str(slug).strip())
        if p is None:
            return f"proyecto desconocido: {slug!r}"
        ids.append(p["id"])
    await db.run("DELETE FROM project_mcp_servers WHERE mcp_id=?", (mcp_id,))
    for pid in ids:
        await db.link_mcp(pid, mcp_id)
    return None


async def api_mcp_list(request: web.Request) -> web.Response:
    """GET /admin/api/mcp — catálogo completo (con project_slugs)."""
    db = request.app[DB_KEY]
    rows = await _mcp_with_links(db)
    return web.json_response({"mcp_servers": _serialize(rows)})


async def api_mcp_upsert(request: web.Request) -> web.Response:
    """POST /admin/api/mcp — alta/edición manual de un MCP.

    Body: {name, capability, transport?, command?, args?, url?, env?,
           read_only?, on_demand?, idle_timeout_s?, enabled?,
           project_slugs?: [..]}  (project_slugs vacío/omitido = global)

    Para el alta vía GitHub con vetting está POST /admin/api/mcp/install
    (F2, aún no implementada).
    """
    db = request.app[DB_KEY]
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    body = body or {}
    name = (body.get("name") or "").strip()
    if not name:
        return web.json_response({"error": "name requerido"}, status=400)
    existing = await db.get_mcp_server(name)
    if existing is None and not (body.get("capability") or "").strip():
        return web.json_response({"error": "capability requerida"}, status=400)
    data = {"name": name}
    for col in _MCP_EDITABLE:
        if col in body:
            data[col] = body[col]
    if "env" in data and not isinstance(data["env"], dict):
        return web.json_response(
            {"error": "env debe ser objeto JSON (refs \"env:NAME\" para "
                      "credenciales)"}, status=400)
    if "args" in data and not isinstance(data["args"], list):
        return web.json_response({"error": "args debe ser lista"}, status=400)
    row = await db.upsert_mcp_server(data)
    if isinstance(body.get("project_slugs"), list):
        err = await _mcp_set_links(db, row["id"], body["project_slugs"])
        if err:
            return web.json_response({"error": err}, status=400)
    rows = await _mcp_with_links(db)
    row = next(r for r in rows if r["name"] == row["name"])
    return web.json_response({"mcp": _serialize(row)},
                             status=200 if existing else 201)


async def api_mcp_patch(request: web.Request) -> web.Response:
    """PATCH /admin/api/mcp/{name} — toggles / campos parciales / links."""
    db = request.app[DB_KEY]
    name = request.match_info["name"]
    existing = await db.get_mcp_server(name)
    if existing is None:
        return web.json_response({"error": "no encontrado"}, status=404)
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    body = body or {}
    data = {"name": existing["name"]}
    for col in _MCP_EDITABLE:
        if col in body:
            data[col] = body[col]
    if len(data) > 1:
        await db.upsert_mcp_server(data)
    if isinstance(body.get("project_slugs"), list):
        err = await _mcp_set_links(db, existing["id"], body["project_slugs"])
        if err:
            return web.json_response({"error": err}, status=400)
    rows = await _mcp_with_links(db)
    row = next(r for r in rows if r["id"] == existing["id"])
    return web.json_response({"mcp": _serialize(row)})


async def api_mcp_health(request: web.Request) -> web.Response:
    """POST /admin/api/mcp/{name}/health — re-chequea el handshake AHORA.

    El `health` de la tabla se escribía solo en el probe del boot, así
    que arreglar un MCP roto (cambiarle los args, cargar la credencial
    que faltaba) y confirmarlo obligaba a reiniciar el relay entero
    mientras la UI seguía mostrando `handshake ✗` sobre algo que ya
    andaba.

    Levanta el proceso de verdad — es el punto: un handshake que no
    spawnea no prueba nada. Devuelve el error cuando falla, que es lo
    que hace falta para arreglarlo.
    """
    db = request.app[DB_KEY]
    name = request.match_info["name"]
    row = await db.get_mcp_server(name)
    if row is None:
        return web.json_response({"error": "no encontrado"}, status=404)
    health, error = await mcp_pool.probe_and_store_health(db, row)
    return web.json_response({"name": row["name"], "health": health,
                              "error": error})


async def api_mcp_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/mcp/{name} — baja (links caen por cascade)."""
    db = request.app[DB_KEY]
    name = request.match_info["name"]
    deleted = await db.delete_mcp_server(name)
    if not deleted:
        return web.json_response({"error": "no encontrado"}, status=404)
    return web.json_response({"deleted": True, "name": name})


# ---------- MCPs F2: install pipeline desde GitHub ----------

async def api_mcp_install_start(request: web.Request) -> web.Response:
    """POST /admin/api/mcp/install {url} — arranca el pipeline F2.

    Crea un job transitorio y dispara clone → scan → vetting en
    background. Responde inmediato con `{job_id, state: "pending"}`
    y la UI hace polling a `GET /admin/api/mcp/install/{job_id}`.
    """
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    url = (body.get("url") or "").strip()
    if not url:
        return web.json_response(
            {"error": "url requerida (https://github.com/owner/repo)"},
            status=400)
    if url.startswith("file://"):
        if not os.environ.get("FOURBIS_MCP_ALLOW_FILE_URL"):
            return web.json_response(
                {"error": "url debe ser http(s) o git@ "
                          "(file:// solo con FOURBIS_MCP_ALLOW_FILE_URL=1, "
                          "para E2E/tests)"},
                status=400)
    elif not (url.startswith("http://") or url.startswith("https://")
              or url.startswith("git@")):
        return web.json_response(
            {"error": "url debe ser http(s) o git@"}, status=400)
    installer: McpInstaller = request.app[MCP_INSTALLER_KEY]
    try:
        job = await installer.start(url)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"job_id": job.id, **job.to_public()},
                             status=202)


async def api_mcp_install_status(request: web.Request) -> web.Response:
    """GET /admin/api/mcp/install/{job_id} — estado actual del job."""
    installer: McpInstaller = request.app[MCP_INSTALLER_KEY]
    job_id = request.match_info["job_id"]
    job = installer.get(job_id)
    if job is None:
        return web.json_response(
            {"error": "job no encontrado (¿expiró? reinstala)"},
            status=404)
    return web.json_response(job.to_public())


async def api_mcp_install_confirm(request: web.Request) -> web.Response:
    """POST /admin/api/mcp/install/{job_id}/confirm — corre install + handshake.

    Body opcional: `{command?, args?, env?, capability?, name?}` — si
    el detector automático no acertó (proposal con `needs_manual=true`)
    o quieres cambiarle el nombre/capability, pasalo acá. El override
    reemplaza la propuesta antes del install.
    """
    installer: McpInstaller = request.app[MCP_INSTALLER_KEY]
    db = request.app[DB_KEY]
    job_id = request.match_info["job_id"]
    override: dict = {}
    try:
        body = await request.json() if request.body_exists else {}
        if isinstance(body, dict):
            override = body
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)

    # Filtro override: solo campos válidos (los mismos que acepta
    # api_mcp_upsert para proposal más capability/name).
    allowed = {"command", "args", "env", "capability", "name"}
    override = {k: v for k, v in override.items() if k in allowed}

    job = installer.get(job_id)
    if job is None:
        return web.json_response(
            {"error": "job no encontrado (¿expiró? reinstala)"},
            status=404)
    if job.state.value == "awaiting_confirm" \
            and job.proposal.get("needs_manual") \
            and "command" not in override:
        return web.json_response(
            {"error": "el detector no pudo inferir comando; pasa "
                      "{command, args} en el body"}, status=400)

    try:
        job = await installer.confirm(job_id, db, override=override or None)
    except KeyError as e:
        return web.json_response({"error": str(e)}, status=404)
    except RuntimeError as e:
        return web.json_response(
            {"error": str(e),
             "job": job.to_public(),
             "hint": "ajusta override y reintenta"}, status=409)

    return web.json_response(job.to_public())


async def api_cbm_orphans(request: web.Request) -> web.Response:
    """GET /admin/api/cbm/orphans — lista cbm projects sin fila en projects.

    Devuelve una lista con root_path, cbm_name, nodes, edges, has_git,
    suggested_slug (derivado del basename). El cliente decide qué crear.
    """
    db = request.app[DB_KEY]
    if not cbm_binary_path():
        return web.json_response({"error": "cbm no instalado"}, status=503)

    try:
        data = await _cbm_list_projects()
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": str(e)}, status=500)

    all_projects = await db.list_projects(enabled_only=False)
    by_path: dict[str, dict] = {}
    for p in all_projects:
        rp = (p.get("repo_path") or "").replace("\\", "/").rstrip("/").lower()
        if rp:
            by_path[rp] = p

    orphans: list[dict] = []
    for c in data.get("projects", []):
        rp_raw = c.get("root_path", "")
        rp_key = rp_raw.replace("\\", "/").rstrip("/").lower()
        if not rp_key:
            continue
        if rp_key in by_path:
            continue  # ya tiene experto
        path = Path(rp_raw)
        base = path.name
        # Slug sugerido: minúsculas, espacios y puntos → guion.
        slug = base.lower().replace(" ", "-")
        slug = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in slug)
        while "--" in slug:
            slug = slug.replace("--", "-")
        slug = slug.strip("-") or "unnamed"
        orphans.append({
            "root_path": rp_raw,
            "cbm_name": c.get("name", ""),
            "nodes": c.get("nodes", 0),
            "edges": c.get("edges", 0),
            "size_bytes": c.get("size_bytes", 0),
            "has_git": _has_git(path),
            "suggested_slug": slug,
            "exists_in_db": False,
        })

    # Filtrar los que el usuario marcó como ignorados.
    ignored = set(await db.list_ignored_orphans())
    if ignored:
        orphans = [o for o in orphans if o["cbm_name"] not in ignored]

    # Ordenar por nodes desc — los más grandes primero son los más útiles.
    orphans.sort(key=lambda o: -o["nodes"])
    return web.json_response({
        "count": len(orphans),
        "orphans": orphans,
        "indexed_total": len(data.get("projects", [])),
        "with_expert": len(by_path),
        "ignored_count": len(ignored),
    })


async def api_orphan_ignore(request: web.Request) -> web.Response:
    """POST /admin/api/cbm/orphans/ignore — marca un cbm_name como ignorado.

    Body: {"cbm_name": "C-Users-..."}
    """
    db = request.app[DB_KEY]
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    body = body or {}
    cbm_name = (body.get("cbm_name") or "").strip()
    if not cbm_name:
        return web.json_response({"error": "cbm_name requerido"}, status=400)
    reason = (body.get("reason") or "").strip()
    ok = await db.ignore_orphan(cbm_name, reason)
    if not ok:
        return web.json_response(
            {"error": "ya estaba ignorado", "cbm_name": cbm_name},
            status=409,
        )
    return web.json_response({"ignored": True, "cbm_name": cbm_name})


async def api_orphan_unignore(request: web.Request) -> web.Response:
    """DELETE /admin/api/cbm/orphans/ignore?cbm_name=X — des-ignora."""
    db = request.app[DB_KEY]
    cbm_name = (request.query.get("cbm_name") or "").strip()
    if not cbm_name:
        return web.json_response({"error": "cbm_name requerido"}, status=400)
    ok = await db.unignore_orphan(cbm_name)
    if not ok:
        return web.json_response(
            {"error": "no estaba ignorado", "cbm_name": cbm_name},
            status=404,
        )
    return web.json_response({"unignored": True, "cbm_name": cbm_name})


async def api_orphan_ignored_list(request: web.Request) -> web.Response:
    """GET /admin/api/cbm/orphans/ignored — lista los ignorados."""
    db = request.app[DB_KEY]
    rows = await db.run(
        "SELECT cbm_name, reason, created_at FROM ignored_orphans "
        "ORDER BY created_at DESC"
    )
    return web.json_response({"ignored": _serialize(rows), "count": len(rows)})


def _slugify(raw: str) -> str:
    """Normaliza a slug seguro: [a-z0-9-], sin dobles guiones."""
    slug = (raw or "").strip().lower().replace(" ", "-")
    slug = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in slug)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "unnamed"


def _new_project_row(slug: str, name: str, repo_path: str,
                     description: str = "") -> dict:
    """Fila default para las altas de projects (directa y from-cbm):
    system_prompt template minimal + native cbm; `mcp_servers` vacío
    (el wrapper 4bis se retiró del catálogo 2026-08-16 — docs/WRAPPER.md;
    los MCPs se agregan a mano desde el tab).
    El usuario refina el prompt después (✏️ en la Admin UI)."""
    sys_prompt = (
        f"Eres el experto {name}. Stack: leer del repo para responder.\n\n"
        "Reglas:\n"
        "- Si no sabes, dilo. No inventes.\n"
        "- Usa las tools disponibles para leer/escribir el código real.\n\n"
        "TOOLS:\n"
        "- `cbm_query(tool, args_json)`: knowledge graph del repo. ÚSALA PRIMERO "
        "para preguntas sobre estructura con `tool='search_graph'` o "
        "`tool='get_architecture'`. Devuelve JSON <10ms.\n"
        # Mismo orden que la migración de 2026-09-02 que corrigió los 54
        # system_prompt ya guardados: así un proyecto nuevo y uno migrado
        # producen texto idéntico y un diff entre ambos no muestra ruido.
        "- `read_file`, `write_file`, `edit_file`, `list_dir`, "
        "`move_file`, `search_files`, `shell`: filesystem + shell sobre "
        "el repo."
    )
    return {
        "slug": slug,
        "name": name,
        "repo_path": repo_path,
        "description": description,
        "system_prompt": sys_prompt,
        "enabled": True,
        # Iter 11: runner por etapas (planificador + ejecutor +
        # verificador + documentador) activo por default. Se escribe
        # explícito aquí para que la fila lo declare, pero OJO: el
        # wrapper usa `defaults.get("three_stage", True)`, así que los
        # proyectos anteriores, sin la clave, también quedan activos.
        # El opt-out por proyecto es `three_stage=false` en
        # defaults_json desde la Admin UI, y `documenter=false` apaga
        # solo la etapa de documentación. El ejecutor sigue siendo el
        # mismo run_expert; las etapas son turnos breves alrededor.
        "defaults_json": {"three_stage": True},
        "native_tools": ["cbm"],
        # 2026-08-16: el blob arranca vacío. Antes cada proyecto nuevo
        # nacía con el `4bis-wrapper` adentro, así que el tab MCP lo
        # seguía mostrando aunque la migración lo hubiera sacado del
        # catálogo — filesystem y shell son nativos del relay ahora
        # (docs/WRAPPER.md). Los MCPs de verdad se agregan desde el tab.
        "mcp_servers": [],
    }


async def api_project_create(request: web.Request) -> web.Response:
    """POST /admin/api/projects — alta directa de un proyecto/experto.

    Body: {
      "repo_path":   requerido, path absoluto,
      "slug":        opcional (default: basename slugificado),
      "name":        opcional (default: basename),
      "description": opcional,
      "create_dir":  crear la carpeta (+ README.md seed si queda vacía),
      "git_init":    `git init` si falta .git (best-effort),
      "index_now":   kick de `cbm index_repository` si cbm está instalado
    }

    A diferencia de POST /projects/from-cbm NO exige que el repo ya esté
    indexado en cbm: sirve para partir un proyecto de cero
    (docs/NUEVO_PROYECTO.md). 409 si el slug ya existe. Si arrancó
    indexación devuelve `index_job_id` (pollear GET /admin/api/reindex/{job_id}).
    """
    db = request.app[DB_KEY]
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    body = body or {}

    raw_path = (body.get("repo_path") or "").strip()
    if not raw_path:
        return web.json_response({"error": "repo_path requerido"}, status=400)
    repo = Path(raw_path).expanduser()
    if not repo.is_absolute():
        return web.json_response(
            {"error": f"repo_path debe ser absoluto: {repo}"}, status=400)
    if repo.exists() and not repo.is_dir():
        return web.json_response(
            {"error": f"repo_path no es un directorio: {repo}"}, status=400)

    notes: list[str] = []
    create_dir = bool(body.get("create_dir"))
    if not repo.is_dir():
        if not create_dir:
            return web.json_response(
                {"error": f"el directorio no existe: {repo} "
                          "(manda create_dir=true para crearlo)"},
                status=400)
        repo.mkdir(parents=True, exist_ok=True)
        notes.append("directorio creado")

    name = (body.get("name") or "").strip() or repo.name
    slug = _slugify((body.get("slug") or "").strip() or repo.name)
    description = (body.get("description") or "").strip()

    existing = await db.get_project(slug)
    if existing:
        return web.json_response(
            {"error": f"slug ya existe: {slug}",
             "existing_slug": existing["slug"]},
            status=409)

    # README seed: solo al partir de cero (create_dir y carpeta vacía).
    # Le da a cbm algo que indexar y contexto inicial al experto.
    if create_dir and not any(repo.iterdir()):
        (repo / "README.md").write_text(
            f"# {name}\n\n{description or '(sin descripción todavía)'}\n\n"
            f"Proyecto creado desde 4bis.relay el "
            f"{time.strftime('%Y-%m-%d')}.\n",
            encoding="utf-8")
        notes.append("README.md seed escrito")

    if bool(body.get("git_init")) and not (repo / ".git").exists():
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "init", cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0:
                notes.append("git init OK")
            else:
                notes.append("git init falló: "
                             + stderr.decode("utf-8", errors="replace")[:200])
        except (OSError, asyncio.TimeoutError) as e:
            notes.append(f"git init falló: {e!r}")

    project = _new_project_row(slug, name, str(repo), description)
    await db.upsert_project(project)

    index_job_id = None
    if bool(body.get("index_now")):
        if cbm_binary_path():
            index_job_id = _start_index_job(slug, str(repo))
            notes.append(f"indexación cbm arrancada ({index_job_id})")
        else:
            notes.append("cbm no instalado: sin indexación")

    return web.json_response(
        {"project": _serialize(dict(project)),
         "index_job_id": index_job_id, "notes": notes},
        status=201)


async def api_project_from_cbm(request: web.Request) -> web.Response:
    """POST /admin/api/projects/from-cbm.

    Body: {"cbm_name": "C-Users-...", "slug": "opcional", "name": "opcional"}.

    Crea una fila en `projects` con:
      - slug (auto del basename si no se pasa, validado único)
      - name (auto del basename Title-Case si no se pasa)
      - repo_path (del cbm lookup)
      - system_prompt template minimal (sabe usar cbm_query)
      - native_tools = ["cbm"]
      - mcp_servers = [] (vacío; el wrapper 4bis se retiró del catálogo
        2026-08-16 — docs/WRAPPER.md. Los MCPs se agregan desde el tab)

    Si el slug ya existe, devuelve 409.
    """
    db = request.app[DB_KEY]
    if not cbm_binary_path():
        return web.json_response({"error": "cbm no instalado"}, status=503)

    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    body = body or {}

    cbm_name = (body.get("cbm_name") or "").strip()
    if not cbm_name:
        return web.json_response({"error": "cbm_name requerido"}, status=400)

    # Lookup del cbm_name en list_projects.
    try:
        data = await _cbm_list_projects()
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": str(e)}, status=500)

    match = None
    for c in data.get("projects", []):
        if c.get("name") == cbm_name:
            match = c
            break
    if not match:
        return web.json_response(
            {"error": f"cbm_name no encontrado: {cbm_name}"}, status=404,
        )

    repo_path = match["root_path"]
    base = Path(repo_path).name
    slug = _slugify((body.get("slug") or "").strip() or base)
    name = (body.get("name") or "").strip() or base

    # Validar slug único.
    existing = await db.get_project(slug)
    if existing:
        return web.json_response(
            {"error": f"slug ya existe: {slug}",
             "existing_slug": existing["slug"]},
            status=409,
        )

    project = _new_project_row(
        slug, name, repo_path,
        description=(body.get("description") or "").strip())
    await db.upsert_project(project)
    return web.json_response({"project": _serialize(dict(project))}, status=201)


async def api_project_open_vscode(request: web.Request) -> web.Response:
    """POST /admin/api/projects/{slug}/open-vscode — abre VS Code con el repo.

    Usa `code <repo_path>` (asume que `code` está en PATH, lo instala VS Code).
    Si no está, devuelve 503 con instrucción.
    """
    slug = request.match_info["slug"]
    db = request.app[DB_KEY]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    rp = project.get("repo_path", "")
    if not rp or not Path(rp).is_dir():
        return web.json_response({"error": "repo_path inválido"}, status=400)

    # Buscar `code` (VS Code) en PATH. Filtramos cbm que también se llama
    # `code` y rompe. Preferimos .cmd (es batch válido en Windows) sobre
    # el `code` sin extensión (que a veces no es PE válido).
    from shutil import which
    candidates = [which("code.cmd"), which("code.exe"), which("code")]
    code_bin = None
    for c in candidates:
        if not c:
            continue
        cl = c.lower()
        if "codebase-memory" in cl or "codebase_memory" in cl:
            continue
        # VS Code instala `code` (sin extensión) en ...\Microsoft VS Code\bin\
        # que NO es un binario PE válido en algunas instalaciones.
        # Preferimos siempre code.cmd o code.exe.
        if cl.endswith(".cmd") or cl.endswith(".exe"):
            code_bin = c
            break
    if not code_bin:
        return web.json_response(
            {"error": "`code` (VS Code) no está en PATH. "
                      "Desde VS Code: Ctrl+Shift+P → 'Shell Command: Install "
                      "`code` command in PATH'."},
            status=503,
        )

    try:
        # Sin asignar: es fire-and-forget a propósito (ver abajo).
        await asyncio.create_subprocess_exec(
            code_bin, rp,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # No esperamos — `code` abre y vuelve.
        return web.json_response({"ok": True, "spawned": True,
                                  "cmd": code_bin, "path": rp})
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": str(e)}, status=500)


async def api_zombies_list(request: web.Request) -> web.Response:
    """GET /admin/api/chats/zombies — chats en status=running viejos.

    Iter 5.3: un chat zombie figura running en DB pero el proceso del
    relay ya no lo tiene vivo (reinicio, crash sin cleanup, etc.).
    Por convención, > `older_than_s` segundos en running = sospechoso.
    La UI muestra esta lista en un tab aparte con botón "Eliminar de BD".
    """
    db = request.app[DB_KEY]
    older_than_s = int(request.query.get("older_than_s", "60"))
    limit = int(request.query.get("limit", "200"))
    rows = await db.list_zombie_chats(
        older_than_s=older_than_s, limit=limit)
    return web.json_response({
        "zombies": [dict(r) for r in rows],
        "older_than_s": older_than_s,
        "count": len(rows),
    })


async def api_chat_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/chats/{chat_id} — hard delete de un zombie.

    Iter 5.3: la grid de zombies tiene un botón "Eliminar de BD" que
    dispara este endpoint. Best-effort: borra la fila + .md asociado.
    El JSONL no se toca (best-effort, no urge, pesan poco).
    """
    db = request.app[DB_KEY]
    chat_id = request.match_info["chat_id"]
    ok = await db.delete_chat(chat_id)
    if not ok:
        return web.json_response(
            {"error": f"chat {chat_id!r} no existe"}, status=404)
    return web.json_response({"deleted": chat_id})


# ---------- Notes workspace (iter 9.8) ----------
#
# ---------- Notes workspace (iter 9.8) ----------
#
# Es la única entrada para el workspace sin-repo. Iter 9.10 removió
# el legacy /admin/api/consults — los clientes que lo usaban ahora
# usan /admin/api/notes directamente.

async def api_notes_create(request: web.Request) -> web.Response:
    """POST /admin/api/notes — nuevo turno del workspace notes.

    Body: {user, system_prompt?, conversation_id?, new_conversation?, model?}
    Equivalente al POST que la UI de Chats usa para cualquier proyecto,
    pero solo válido para project_slug='notes'. Devuelve 202 con id
    (mismo shape que experts/run) o 503 si el notes-workspace no existe.
    """
    db = request.app[DB_KEY]
    notes = await db.get_project("notes")
    if notes is None:
        return web.json_response(
            {"error": "notes-workspace no existe (boot no completó el seed)"},
            status=503)
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    body = body or {}
    user = (body.get("user") or "").strip()
    if not user:
        return web.json_response({"error": "user requerido"}, status=400)
    sys_extra = body.get("system_prompt") or ""
    model = body.get("model") or ""
    is_async = bool(body.get("async"))

    conv_id = (body.get("conversation") or "").strip()
    if not conv_id and body.get("new_conversation"):
        conv_id = await db.create_conversation(
            project_slug="notes", requested_by=identity.requester(request))

    payload = {"target": "notes", "user": user, "system_extra": sys_extra}
    if model:
        payload["model"] = model
    if conv_id:
        payload["conversation"] = conv_id

    relay_url = os.environ.get("RELAY_URL", "http://127.0.0.1:8413")
    try:
        import httpx
        r = await asyncio.to_thread(lambda: httpx.post(
            f"{relay_url}/experts/run",
            json=payload,
            timeout=10.0))
        if r.status_code >= 400:
            return web.json_response(
                {"error": f"experts/run {r.status_code}: {r.text[:200]}"},
                status=502)
        data = r.json()
        chat_id = data.get("id") or ""
        data["chat_id"] = chat_id
        if conv_id:
            data["conversation_id"] = conv_id
        return web.json_response(data, status=(202 if is_async else 200))
    except Exception as e:  # noqa: BLE001
        return web.json_response(
            {"error": f"no pude llamar /experts/run: {e!r}"}, status=502)


async def api_notes_list(request: web.Request) -> web.Response:
    """GET /admin/api/notes?limit=&offset= — lista de chats del notes."""
    db = request.app[DB_KEY]
    try:
        limit = min(int(request.query.get("limit", "50")), 200)
        offset = max(int(request.query.get("offset", "0")), 0)
    except (TypeError, ValueError):
        limit, offset = 50, 0
    rows = await db.run(
        "SELECT id, target, status, model, started_at, finished_at, "
        "tokens_in, tokens_out, duration_ms, error "
        "FROM chats WHERE target=? ORDER BY started_at DESC LIMIT ? OFFSET ?",
        ("notes", limit, offset))
    return web.json_response({
        "notes": [dict(r) for r in rows],
        "count": len(rows),
    })


async def api_night_runs_list(request: web.Request) -> web.Response:
    """GET /admin/api/night-runs — lista paginada de TODAS las corridas.

    Iter 5.4: el tab dedicado de Night Runs llama este endpoint.
    Filtros: ?project=slug, ?limit=N (default 50), ?since=ISO8601.
    Orden: started_at DESC.
    """
    db = request.app[DB_KEY]
    project = request.query.get("project") or None
    limit = int(request.query.get("limit", "50"))
    rows = await db.list_night_runs(project_slug=project, limit=limit)
    return web.json_response({
        "runs": [dict(r) for r in rows],
        "count": len(rows),
    })


async def api_night_run_detail(request: web.Request) -> web.Response:
    """GET /admin/api/night-runs/{run_id} — detalle completo de un run.

    Iter 5.4: incluye tasks + results + plan ledger (parseado desde
    el espejo en state/) + report_path + branch + pr_url.
    """
    db = request.app[DB_KEY]
    run_id = request.match_info["run_id"]
    row = await db.get_night_run(run_id)
    if row is None:
        return web.json_response(
            {"error": f"run {run_id!r} desconocido"}, status=404)
    detail = {"run": dict(row), "tasks": [], "results": [],
               "plan_tasks": [], "plan_mirror": None}
    # Plan ledger: lo leemos del espejo state/. Si no existe todavía
    # (run muy fresco), devolvemos [] y la UI muestra "todavía no
    # hay ledger" en lugar de explotar.
    from . import night as night_mod
    # aiohttp web.AppKey se serializa al nombre "state_dir" en
    # app["state_dir"]. El fallback a "./state" sirve si el setup no
    # inyectó la key (tests, etc.) — pero el cwd del server DEBE ser
    # el del repo (no mcp-server), o el path falla.
    state_dir = request.app.get("state_dir") or Path("./state")
    if not state_dir.is_absolute():
        state_dir = state_dir.resolve()
    # Importante: el espejo del plan vive en state/agents/night-runs/
    # (la subcarpeta `agents/` es donde van los state de agentes por
    # convención del repo). Iter 5.4 ponía el path incorrecto.
    mirror = Path(state_dir) / "agents" / "night-runs" / run_id / "plan.md"
    # Iter 5.4 — debug: devolvemos el path resuelto + si existe para
    # que la UI pueda mostrar "se buscó en X pero no hay plan ahí".
    detail["plan_mirror"] = str(mirror)
    detail["plan_mirror_exists"] = mirror.is_file()
    if mirror.is_file():
        try:
            text = mirror.read_text(encoding="utf-8")
            detail["plan_tasks"] = [
                {"id": t.id, "title": t.title, "refs": t.refs,
                 "status": t.status, "note": t.note}
                for t in night_mod.parse_plan(text)
            ]
        except OSError:
            detail["plan_tasks"] = []
    # Snapshot vivo si el orquestador está corriendo este run.
    night_registry = request.app.get(NIGHT_KEY) or {}
    if run_id in night_registry:
        orch, _task = night_registry[run_id]
        try:
            detail["snapshot"] = orch.snapshot()
        except Exception:  # noqa: BLE001 — best-effort
            pass
    return web.json_response(detail)


async def api_night_run_report(request: web.Request) -> web.Response:
    """GET /admin/api/night-runs/{run_id}/report — devuelve el .md del reporte.

    Lee el path guardado en `night_runs.report_path`. Si el run sigue
    activo y todavía no escribió reporte, devuelve 404 con un mensaje
    claro (la UI muestra "todavía no hay reporte, refresca más tarde").
    El reporte puede ser pesado (cientos de KB en noches largas);
    capeamos a 256 KB en el read por defensa.
    """
    from pathlib import Path as _P

    db = request.app[DB_KEY]
    run_id = request.match_info["run_id"]
    row = await db.get_night_run(run_id)
    if row is None:
        return web.json_response({"error": "run desconocido"}, status=404)
    rp = row.get("report_path")
    if not rp:
        return web.json_response(
            {"error": "el run no tiene reporte todavía "
                      "(¿sigue corriendo?)", "run": dict(row)},
            status=404)
    path = _P(rp)
    if not path.is_file():
        return web.json_response(
            {"error": f"el reporte apunta a {rp} pero no existe en disco",
             "run": dict(row)},
            status=410)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return web.json_response(
            {"error": f"no pude leer el reporte: {e}"}, status=500)
    if len(text) > 256 * 1024:
        text = text[: 256 * 1024] + \
            f"\n\n…(truncado a 256 KB; el reporte completo está en {rp})"
    return web.json_response({
        "run_id": run_id,
        "project_slug": row.get("project_slug"),
        "end_reason": row.get("end_reason"),
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "report_path": rp,
        "report_md": text,
        "size_bytes": path.stat().st_size,
    })


async def api_project_expert_run(request: web.Request) -> web.Response:
    """POST /admin/api/projects/{slug}/expert-run — dispara /experts/run.

    Body: {"prompt": "...", "async": false}
    Si async=true devuelve 202 con chat_id; si false espera y devuelve
    el output completo (cap a 32 KB). El modal "🧪 experto" de la Admin
    UI usa esto para no tener que abrir la extensión VS Code.
    """
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return web.json_response(
            {"error": "prompt requerido (no vacío)"}, status=400)
    is_async = bool(body.get("async"))

    # Conversación (2026-07-12): el modal 🧪 puede mantener contexto
    # entre corridas. `new_conversation: true` crea una acá y la
    # devuelve; `conversation: <id>` replaya una existente (la valida
    # /experts/run: 404 desconocida / 409 cerrada).
    conv_id = (body.get("conversation") or "").strip()
    if not conv_id and body.get("new_conversation"):
        conv_id = await db.create_conversation(
            project_slug=project["slug"],
            requested_by=identity.requester(request))

    payload = {"target": slug, "user": prompt}
    if conv_id:
        payload["conversation"] = conv_id

    relay_url = os.environ.get("RELAY_URL", "http://127.0.0.1:8413")
    try:
        import httpx
        r = await asyncio.to_thread(lambda: httpx.post(
            f"{relay_url}/experts/run",
            json=payload,
            timeout=10.0))
        if r.status_code >= 400:
            return web.json_response(
                {"error": f"experts/run {r.status_code}: "
                          f"{r.text[:200]}"},
                status=502)
        data = r.json()
        chat_id = data.get("id") or ""
        data["chat_id"] = chat_id  # alias para la UI
        if conv_id:
            data["conversation_id"] = conv_id
        if is_async:
            return web.json_response(data, status=202)
    except Exception as e:  # noqa: BLE001
        return web.json_response(
            {"error": f"no pude llamar /experts/run: {e!r}"}, status=502)

    # sync: /experts/run es async desde ADR-024 (202 + notify), así que
    # acá esperamos nosotros: poll a la fila de chats hasta finished
    # (max ~5min) y devolvemos el .md completo como output.
    deadline = time.monotonic() + 320.0
    chat = None
    while time.monotonic() < deadline:
        await asyncio.sleep(2.0)
        chat = await db.get_chat(chat_id)
        if chat and chat.get("finished_at"):
            break
    if not chat or not chat.get("finished_at"):
        return web.json_response(
            {"id": chat_id, "chat_id": chat_id, "status": "running",
             "output": "(sigue corriendo — mira el tab Estado o espera "
                       "el notify)"},
            status=202)
    out = ""
    if chat.get("md_path"):
        try:
            out = await asyncio.to_thread(
                Path(chat["md_path"]).read_text, encoding="utf-8")
        except OSError:
            out = "(no pude leer el .md del chat)"
    if len(out) > 32 * 1024:
        out = out[:32 * 1024] + "\n…(truncado a 32 KB)"
    return web.json_response({
        "id": chat_id, "chat_id": chat_id, "status": chat["status"],
        "error": chat.get("error"), "output": out,
        "tokens_in": chat.get("tokens_in"),
        "tokens_out": chat.get("tokens_out"),
        "conversation_id": conv_id or None,
    })


# ---- CRM local (trycompai/crm): read-only + sync job ----
# No hay credencial que pasar: el CRM corre al lado y el relay le lee el
# Postgres (DSN en CRM_DATABASE_URL). _set_job/_get_job ya están definidos
# arriba para los jobs del reindex; los reusamos para que el polling desde
# la UI sea idéntico.


async def api_crm_clients_list(request: web.Request) -> web.Response:
    """GET /admin/api/crm/clients — snapshot de la última sync."""
    db = request.app[DB_KEY]
    clients = await db.list_crm_clients()
    # _parse_crm_client ya inyectó `deals` y `contacts` como objetos (no JSON).
    out = [{
        "id": c["id"],
        "ext_id": c["ext_id"],
        "name": c["name"],
        "domain": c.get("domain") or "",
        "contacts": c.get("contacts") or [],
        "deals": c.get("deals") or [],
        "project_count": c.get("project_count", 0),
        "last_sync_at": c.get("last_sync_at"),
        "last_sync_status": c.get("last_sync_status"),
        "last_activity_at": c.get("last_activity_at"),
    } for c in clients]
    return web.json_response({"clients": out})


async def api_crm_sync_post(request: web.Request) -> web.Response:
    """POST /admin/api/crm/sync — dispara sync en background."""
    db = request.app[DB_KEY]
    # Chequear la conexión ANTES de arrancar el job: si el CRM está
    # apagado devolvemos 503 inmediato en vez de dejar un job que falla
    # tres segundos después, y el feedback en la UI queda claro.
    try:
        await crm_mod.check()
    except crm_mod.CrmError as e:
        try:
            await db.mark_crm_sync_error(e.message)
        except Exception:  # noqa: BLE001
            pass
        return web.json_response(
            {"error": e.message, "status_code": e.status_code},
            status=e.status_code or 503)
    job_id = f"crm_{int(asyncio.get_event_loop().time() * 1000) % 100000}"

    async def _do_sync() -> None:
        try:
            _set_job(job_id, {"status": "running",
                               "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                          time.gmtime())})
            stats = await crm_mod.sync_once(db)
            _set_job(job_id, {"status": "ok", **stats,
                               "finished_at": time.strftime(
                                   "%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        except crm_mod.CrmError as e:
            try:
                await db.mark_crm_sync_error(e.message)
            except Exception:  # noqa: BLE001
                pass  # si falla el mark, igual devolvemos el error abajo
            _set_job(job_id, {"status": "error", "error": e.message})
        except Exception as e:  # noqa: BLE001
            _set_job(job_id, {"status": "error",
                               "error": f"{type(e).__name__}: {e}"})

    asyncio.create_task(_do_sync())  # fire-and-forget; _do_sync muta el slot del job
    return web.json_response({"job_id": job_id})


async def api_crm_sync_status(request: web.Request) -> web.Response:
    """GET /admin/api/crm/sync/status?job_id=... — patrón del reindex."""
    job_id = request.query.get("job_id", "")
    if not job_id:
        return web.json_response({"error": "falta job_id"}, status=400)
    val = _get_job(job_id)
    if val is None:
        return web.json_response({"error": "job desconocido"}, status=404)
    if isinstance(val, asyncio.Task):
        if not val.done():
            return web.json_response({"job_id": job_id, "status": "running"})
        return web.json_response({"job_id": job_id, "status": "unknown",
                                  "error": "job terminó sin payload"})
    if isinstance(val, dict):
        out = {k: v for k, v in val.items() if k != "_task"}
        return web.json_response(out)
    return web.json_response({"job_id": job_id})


async def api_crm_check(request: web.Request) -> web.Response:
    """GET /admin/api/crm/check — ¿está arriba el CRM y migrado?

    Devuelve 200 con los conteos del CRM, o 503 si el Postgres no
    responde / el schema no está. La UI lo usa para el semáforo del tab
    antes de dejar sincronizar."""
    try:
        r = await crm_mod.check()
    except crm_mod.CrmError as e:
        return web.json_response(
            {"ok": False, "error": e.message, "dsn": _crm_dsn_public(),
             "app_url": crm_mod.crm_app_url(),
             "app_up": await crm_mod.app_up(),
             "workspace": crm_mod.CRM_WORKSPACE},
            status=e.status_code or 503)
    except Exception as e:  # noqa: BLE001
        return web.json_response(
            {"ok": False, "error": f"{type(e).__name__}: {e}"},
            status=500)
    # `app_up` es aparte de `ok`: el Postgres del CRM vuelve solo con
    # Docker, el dev server no. Es lo que decide si mostrar "Levantar CRM".
    return web.json_response({**r, "dsn": _crm_dsn_public(),
                              "app_url": crm_mod.crm_app_url(),
                              "app_up": await crm_mod.app_up(),
                              "workspace": crm_mod.CRM_WORKSPACE})


async def api_crm_start(request: web.Request) -> web.Response:
    """POST /admin/api/crm/start — levanta el stack del CRM.

    El CRM no es parte del relay: hay que arrancarlo a mano después de cada
    reinicio (docs/CRM_LOCAL.md). Devuelve enseguida; el tab poll-ea
    /crm/check hasta que `app_up` da true.
    """
    if await crm_mod.app_up():
        return web.json_response({"started": False, "already_up": True,
                                  "notes": ["el CRM ya estaba respondiendo"]})
    try:
        return web.json_response(await crm_mod.start_stack())
    except crm_mod.CrmError as e:
        return web.json_response({"error": e.message, "log": str(crm_mod.crm_dev_log())},
                                 status=e.status_code or 500)


def _crm_dsn_public() -> str:
    """DSN sin la contraseña — se muestra en la UI para diagnosticar."""
    dsn = crm_mod.crm_dsn()
    if "@" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    return f"{scheme}://{rest.split('@', 1)[1]}"


async def _crm_health_rows(db: Any) -> list[dict]:
    """Una fila por cliente CRM con proyectos vinculados: silencio +
    resumen de GitHub. Base de `/crm/health` y `/crm/digest`.

    Solo clientes con `project_count > 0`: el sync trae cualquier
    empresa que apareció en un email o una reunión (decenas, la mayoría
    gente con la que se habló una vez), y de esas solo importan acá las
    que son clientes de verdad — es decir, tienen un proyecto vinculado.
    """
    clients = [c for c in await db.list_crm_clients() if c["project_count"] > 0]
    rows: list[dict] = []
    for c in clients:
        projects = await db.list_projects_for_client(c["id"])
        repos = []
        for p in projects:
            slug = await github_mod.repo_slug(p.get("repo_path") or "")
            if slug:
                repos.append(slug)
        # Un cliente puede tener varios proyectos → varios repos; se piden
        # todos en paralelo (mismo criterio que api_project_github: dos
        # spawns de `gh` por repo que no dependen entre sí).
        results = await asyncio.gather(
            *(github_mod.issues(r) for r in repos),
            *(github_mod.pulls(r) for r in repos),
        ) if repos else []
        issues_lists = results[:len(repos)]
        pulls_lists = results[len(repos):]
        # None si NINGÚN repo pudo leerse (sin `gh`/auth); si al menos uno
        # respondió, se suma lo que haya — parcial es mejor que ocultar todo.
        got_any = any(x is not None for x in issues_lists + pulls_lists)
        open_issues = (sum(len(x or []) for x in issues_lists)
                      if got_any else None)
        open_prs = sum(len(x or []) for x in pulls_lists) if got_any else None

        rows.append({
            "client_id": c["id"],
            "name": c["name"],
            "domain": c["domain"],
            "days_silent": crm_mod.days_since(c.get("last_activity_at")),
            "last_activity_at": c.get("last_activity_at"),
            "project_count": c["project_count"],
            "deals": [{"name": d.get("name"), "stage": d.get("stage")}
                     for d in c.get("deals") or []],
            "open_issues": open_issues,
            "open_prs": open_prs,
        })
    return rows


async def api_crm_health(request: web.Request) -> web.Response:
    """GET /admin/api/crm/health?stale_days=N — salud por cliente.

    Silencio (`days_since(last_activity_at)`) + issues/PRs abiertos por
    cliente, solo para los que tienen proyecto vinculado. Es la data
    detrás del digest; separado del endpoint que lo envía a Discord para
    poder pedirla sin disparar una notificación (ver docs/CRM_DIGEST.md).
    """
    db = request.app[DB_KEY]
    try:
        stale_days = int(request.query.get("stale_days",
                                           crm_mod.DEFAULT_STALE_DAYS))
    except ValueError:
        return web.json_response({"error": "stale_days debe ser entero"},
                                 status=400)
    rows = await _crm_health_rows(db)
    stale = sum(1 for r in rows
               if r["days_silent"] is None or r["days_silent"] >= stale_days)
    return web.json_response({
        "stale_days": stale_days, "stale_count": stale, "clients": rows,
    })


async def api_crm_digest(request: web.Request) -> web.Response:
    """POST /admin/api/crm/digest — arma el digest y (salvo dry_run) lo
    manda a Discord vía el bot C#.

    Body opcional: `{"stale_days": 14, "channel": "#equipo-demo",
    "dry_run": false}`. Con `dry_run` devuelve el texto sin enviarlo —
    para probar el mensaje antes de spamear el canal.

    Ver docs/CRM_DIGEST.md: es la acción que se le pide al sistema para
    un reporte on-demand además del uso desde el botón de la Admin UI.
    """
    db = request.app[DB_KEY]
    try:
        body = await request.json() if request.body_exists else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "JSON inválido"}, status=400)
    body = body or {}
    stale_days = int(body.get("stale_days") or crm_mod.DEFAULT_STALE_DAYS)
    channel = body.get("channel") or crm_mod.DEFAULT_DIGEST_CHANNEL
    dry_run = bool(body.get("dry_run"))

    rows = await _crm_health_rows(db)
    text = crm_mod.render_digest(rows, stale_days=stale_days)
    stale = sum(1 for r in rows
               if r["days_silent"] is None or r["days_silent"] >= stale_days)

    sent = False
    if not dry_run:
        notify: NotifyClient = request.app[NOTIFY_KEY]
        today = time.strftime("%Y-%m-%d", time.gmtime())
        sent = await notify.send(
            agent_id=f"crm-digest:{today}", kind="progress", message=text,
            metadata={"discord_channel": channel, "stale_count": stale})

    return web.json_response({
        "sent": sent, "dry_run": dry_run, "channel": channel,
        "stale_days": stale_days, "stale_count": stale,
        "client_count": len(rows), "text": text,
    })


async def api_admin_restart(request: web.Request) -> web.Response:
    """POST /admin/api/restart — escribe un marker file en state/ y
    devuelve 200. NO mata el proceso (eso lo hace el script PowerShell
    o el supervisor externo leyendo el marker).

    Patron: la UI dispara, el handler escribe state/restart-requested-at.txt
    con timestamp + pid del relay, devuelve. El script externo
    (restart-4bis-relay.ps1) o un supervisor mira ese archivo y
    decide qué hacer. Si no hay supervisor, el usuario corre el
    .ps1 manualmente.

    Razon: os.execv mata el proceso en seco y si el proceso no
    arranca de nuevo (supervisor ausente), el relay queda MUERTO
    y la UI no se puede reconectar nunca sin reiniciar la maquina.
    Con marker file + script externo el peor caso es: usuario ve
    banner en UI "restart solicitado", corre .ps1 cuando puede.
    """
    state_dir = Path("state")
    state_dir.mkdir(exist_ok=True)
    marker = state_dir / "restart-requested-at.txt"
    marker.write_text(
        f"{time.time()}\nPID={os.getpid()}\nrequested_by={request.remote}\n",
        encoding="utf-8")
    return web.json_response({
        "status": "marker_written",
        "marker_path": str(marker),
        "pid": os.getpid(),
        "next_step": "ejecutar restart-4bis-relay.ps1 o relanzar manualmente",
    })


async def api_me(request: web.Request) -> web.Response:
    """GET /admin/api/me — quién está mirando.

    Hoy no hay forma de saber en qué sesión estás. Esto lo resuelve y de
    paso es la verificación end-to-end del middleware de identidad: por
    el túnel devuelve tu mail, desde localhost devuelve 'owner'.

    El `role` que devuelve es informativo — sirve para que la UI esconda
    lo que no corresponde. Quien mande es `require_role`, del lado del
    server: mentir acá no habilita nada.
    """
    return web.json_response({
        "email": identity.requester(request),
        "role": identity.role_of(request),
    })


# ---------- usuarios / roles (2026-08-21) ----------
#
# Hasta acá la tabla `users` se editaba por SQL y el cambio recién valía
# al reiniciar, porque `identity._roles` se carga UNA vez en el startup.
# Las dos mitades importan: sin endpoints no hay gestión, y sin la
# recarga en caliente la gestión miente (guardás, no pasa nada, y no hay
# nada en pantalla que lo explique).
#
# Qué NO es esto: dar o sacar acceso. Quien llega hasta el relay ya pasó
# la policy de Cloudflare Access; esta tabla solo dice quién de los que
# entran es `owner`. Sacar a alguien de acá lo baja a `member`, no lo
# echa. Ver la nota larga en `identity`.


async def _recargar_roles(db) -> None:
    """Refresca `identity._roles` desde la tabla. Se llama después de
    CADA escritura: el cache es por proceso, así que un cambio sin esto
    no aplica hasta el próximo reinicio."""
    identity.load_roles(await db.list_users())


async def api_users_list(request: web.Request) -> web.Response:
    """GET /admin/api/users — quiénes son owners."""
    db = request.app[DB_KEY]
    return web.json_response({
        "users": await db.list_users(),
        # Para que la UI pueda avisar "este sos vos" y no dejarte
        # quitarte el owner a vos mismo sin querer.
        "me": identity.requester(request),
        "roles": ["owner", "member"],
    })


async def api_user_upsert(request: web.Request) -> web.Response:
    """PUT /admin/api/users — alta o cambio de rol. Body: {email, role}."""
    db = request.app[DB_KEY]
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    role = (body.get("role") or "").strip()
    if "@" not in email or len(email) < 3:
        return web.json_response({"error": "email inválido"}, status=400)
    if role not in ("owner", "member"):
        return web.json_response(
            {"error": "role debe ser owner o member"}, status=400)
    # Bajarse a sí mismo a member deja el relay sin quien lo administre
    # si es el último owner. El check es sobre la TABLA, no sobre quién
    # pide: desde localhost `requester` es "owner" (sin mail) y ahí este
    # guard no aplica ni hace falta — esa sesión manda igual.
    if role == "member":
        owners = [u for u in await db.list_users() if u["role"] == "owner"]
        if len(owners) == 1 and owners[0]["email"] == email:
            return web.json_response(
                {"error": "es el único owner: dejarías el relay sin "
                          "administrador. Agregá otro owner primero."},
                status=409)
    await db.set_user_role(email, role)
    await _recargar_roles(db)
    return web.json_response({"email": email, "role": role})


async def api_user_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/users/{email} — lo devuelve a `member`."""
    db = request.app[DB_KEY]
    email = (request.match_info["email"] or "").strip().lower()
    owners = [u for u in await db.list_users() if u["role"] == "owner"]
    if len(owners) == 1 and owners[0]["email"] == email:
        return web.json_response(
            {"error": "es el único owner: dejarías el relay sin "
                      "administrador. Agregá otro owner primero."},
            status=409)
    if not await db.delete_user(email):
        return web.json_response({"error": "no existe"}, status=404)
    await _recargar_roles(db)
    return web.json_response({"deleted": True, "email": email})


async def api_models(request: web.Request) -> web.Response:
    """GET /admin/api/models[?all=1] — catálogo de modelos.

    Sin `all`, solo los prendidos: es lo que come el selector del chat.
    Con `all=1`, todo (la pantalla de administración), que puede ser
    >100 filas si se importó el catálogo de NVIDIA.

    `vision` viaja para que la UI avise ANTES de mandar en vez de dejar
    que el humano se coma el 400. El 400 sigue existiendo: el selector
    es comodidad, no control.
    """
    db: Database = request.app[DB_KEY]
    todos = request.query.get("all") in ("1", "true", "yes")
    filas = await db.list_models(only_enabled=not todos)
    return web.json_response({
        "models": [db.mask_key(f) for f in filas],
        "default": relay_config.model_spec(),
    })


async def api_model_upsert(request: web.Request) -> web.Response:
    """PUT /admin/api/models/{spec} — alta o edición de un modelo.

    Owner-only por el middleware de roles (no está en MEMBER_ALLOWED).
    Un `api_key` vacío NO borra la que hay: para borrarla hay que
    mandar null explícito, si no cualquier edición de la tarifa desde
    un form que no la muestra te la dejaría sin key.
    """
    db: Database = request.app[DB_KEY]
    spec = request.match_info["spec"]
    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    campos = {k: v for k, v in body.items() if k != "spec"}
    if campos.get("api_key", "sin tocar") == "":
        campos.pop("api_key")
    if "vision" in campos and campos["vision"] is not None:
        campos["vision"] = 1 if campos["vision"] else 0
    if "enabled" in campos:
        campos["enabled"] = 1 if campos["enabled"] else 0
        # Apagar un modelo que un rol está usando lo deja huérfano igual
        # que borrarlo: el rol sigue apuntando al spec y `_staged_model_spec`
        # lo devuelve sin mirar el catálogo. Se corta acá, que es donde
        # está el humano que lo apagó.
        if not campos["enabled"]:
            usados = await _roles_que_usan(db, spec)
            if usados:
                return web.json_response(
                    {"error": (f"{spec} está en uso por: {', '.join(usados)}. "
                               "Cambiá esas referencias antes de apagarlo."),
                     "roles": usados}, status=409)
    try:
        await db.upsert_model(spec, **campos)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    await _refrescar_catalogo(request.app)
    fila = await db.get_model(spec)
    return web.json_response(db.mask_key(fila) if fila else {})


#: Cap del turno de prueba. Ocho tokens alcanzan para un "ok" y hacen
#: que probar el catálogo entero cueste menos que un run.
_TEST_MAX_TOKENS = 8
#: Un proveedor sano contesta un turno de 8 tokens en segundos. Si tarda
#: más, el dato útil ya es "no contesta", no la respuesta.
_TEST_TIMEOUT_S = 45.0


# ── Plantillas de directiva del modo nocturno (2026-08-27) ──
#
# Escribir una directiva buena no es escribir un pedido: hay que numerar
# los puntos (el planificador los extrae y despues chequea cobertura) y
# nombrar solo paths que resuelvan contra el indice cbm — una ref
# invalida descarta la tarea ENTERA y en silencio. Eso se aprende
# perdiendo un run, y sin donde guardarlo se vuelve a perder.


def _tpl_payload(row: dict) -> dict:
    return {
        "nombre": row.get("nombre") or "",
        "directiva": row.get("directiva") or "",
        "notas": row.get("notas") or "",
        "project_slug": row.get("project_slug") or "",
        "global": not (row.get("project_slug") or ""),
        "updated_at": row.get("updated_at") or "",
    }


async def api_night_templates(request: web.Request) -> web.Response:
    """GET /admin/api/night/templates?project=<slug>

    Sin `project` devuelve todas (panel de administracion). Con project,
    las del proyecto + las globales, las especificas primero.
    """
    db = request.app[DB_KEY]
    slug = (request.query.get("project") or "").strip()
    filas = await db.list_night_templates(slug)
    return web.json_response({"templates": [_tpl_payload(r) for r in filas]})


async def api_night_template_save(request: web.Request) -> web.Response:
    """PUT /admin/api/night/templates — alta o edicion.

    Body: {nombre, directiva, project_slug?, notas?}
    """
    db = request.app[DB_KEY]
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    nombre = (body.get("nombre") or "").strip()
    directiva = (body.get("directiva") or "").strip()
    if not nombre:
        return web.json_response({"error": "nombre requerido"}, status=400)
    # Se valida ACA y no en la UI: el endpoint tambien lo usa el chat.
    if not directiva:
        return web.json_response(
            {"error": "directiva vacía: una plantilla sin texto no sirve "
                      "para nada y tapa a la global del mismo nombre"},
            status=400)
    slug = (body.get("project_slug") or "").strip()
    if slug and not await db.get_project(slug):
        return web.json_response(
            {"error": f"proyecto {slug!r} desconocido"}, status=404)
    await db.save_night_template(
        nombre, directiva, project_slug=slug,
        notas=(body.get("notas") or "").strip())
    fila = await db.get_night_template(nombre, slug)
    return web.json_response(_tpl_payload(fila or {}))


async def api_night_template_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/night/templates/{nombre}?project=<slug>"""
    db = request.app[DB_KEY]
    nombre = request.match_info["nombre"]
    slug = (request.query.get("project") or "").strip()
    if not await db.delete_night_template(nombre, slug):
        return web.json_response(
            {"error": f"no hay plantilla {nombre!r} en ese scope"}, status=404)
    return web.json_response({"ok": True})


async def api_model_test(request: web.Request) -> web.Response:
    """POST /admin/api/models/{spec}/test — ¿este modelo contesta?

    Owner-only por el middleware de roles (no está en MEMBER_ALLOWED).

    Existe porque hasta hoy cargar un modelo era a ciegas: se guardaba la
    fila y recién se sabía si servía cuando un run fallaba, con el error
    del proveedor envuelto como `provider_error` — el síntoma que menos
    dice. El caso real (2026-08-26) fue una key de xAI perfectamente
    válida contra una cuenta sin créditos: el proveedor lo explicaba en
    una línea y esa línea no llegaba a ninguna pantalla.

    Por eso lo que se devuelve es el error CRUDO, con su status HTTP.
    Traducirlo a "no se pudo conectar" sería volver al problema.

    Hace un turno real y mínimo (8 tokens) en vez de pinchar un endpoint
    de catálogo: lo que interesa es si ESTE spec responde, y un
    `/v1/models` sano convive con un nombre de modelo que no existe.
    """
    db: Database = request.app[DB_KEY]
    spec = request.match_info["spec"]
    if await db.get_model(spec) is None:
        return web.json_response(
            {"error": f"{spec} no está en el catálogo"}, status=404)
    # El cache es lo que lee `build_model`; refrescarlo acá evita el
    # "lo edité y sigue probando lo viejo" cuando la fila se tocó por
    # fuera del upsert.
    await _refrescar_catalogo(request.app)

    from pydantic_ai import Agent
    from pydantic_ai.exceptions import ModelHTTPError

    t0 = time.monotonic()
    try:
        modelo = build_model(spec)
    except ModelUnavailable as e:
        # Falta la key: no es un fallo del proveedor, es config nuestra.
        return web.json_response(
            {"ok": False, "kind": "sin_key", "error": str(e)})
    except Exception as e:  # noqa: BLE001 — spec roto, provider raro
        return web.json_response(
            {"ok": False, "kind": "spec", "error": f"{type(e).__name__}: {e}"})

    try:
        agent = Agent(modelo)
        r = await asyncio.wait_for(
            agent.run("Responde exactamente: ok",
                      model_settings={"max_tokens": _TEST_MAX_TOKENS}),
            timeout=_TEST_TIMEOUT_S)
    except asyncio.TimeoutError:
        return web.json_response({
            "ok": False, "kind": "timeout",
            "error": f"no contestó en {_TEST_TIMEOUT_S:.0f}s"})
    except ModelHTTPError as e:
        # El caso que motiva todo esto. `body` trae el JSON del proveedor
        # (código, mensaje, a veces hasta el link para arreglarlo).
        return web.json_response({
            "ok": False, "kind": "http", "status": e.status_code,
            "error": str(getattr(e, "body", "") or e)[:600]})
    except Exception as e:  # noqa: BLE001 — red, TLS, provider exótico
        return web.json_response({
            "ok": False, "kind": type(e).__name__, "error": str(e)[:600]})

    # `usage` es PROPIEDAD en pydantic-ai 2.x, no método: con paréntesis
    # tira `'RunUsage' object is not callable` y el botón reportaría
    # error justo en el caso en que el modelo anduvo.
    u = r.usage
    return web.json_response({
        "ok": True,
        "ms": int((time.monotonic() - t0) * 1000),
        "reply": str(r.output)[:120],
        "tokens_in": getattr(u, "input_tokens", None),
        "tokens_out": getattr(u, "output_tokens", None),
    })


async def api_model_delete(request: web.Request) -> web.Response:
    """DELETE /admin/api/models/{spec}"""
    db: Database = request.app[DB_KEY]
    spec = request.match_info["spec"]
    usados = await _roles_que_usan(db, spec)
    if usados:
        return web.json_response(
            {"error": (f"{spec} está en uso por: {', '.join(usados)}. "
                       "Cambiá esas referencias antes de borrarlo."),
             "roles": usados}, status=409)
    await db.delete_model(spec)
    await _refrescar_catalogo(request.app)
    return web.json_response({"deleted": request.match_info["spec"]})


async def _refrescar_catalogo(app: web.Application) -> None:
    """El cache de experts se llena en el startup; editar por la UI
    tiene que verse sin reiniciar, si no la pantalla miente."""
    from . import experts as experts_mod
    db: Database = app[DB_KEY]
    experts_mod.load_catalog(await db.list_models())


def register_admin_routes(app: web.Application) -> None:
    """Enchufe las rutas /admin/* en la app aiohttp."""
    _install_ring_handler()  # logs en vivo para la UI (idempotente)
    app.router.add_get("/admin/api/me", api_me)
    # Usuarios / roles (2026-08-21). Owner-only sin decir nada: la
    # allowlist de `identity.MEMBER_ALLOWED` no los tiene, y lo que no
    # está ahí nace owner-only.
    app.router.add_get("/admin/api/users", api_users_list)
    app.router.add_put("/admin/api/users", api_user_upsert)
    app.router.add_delete("/admin/api/users/{email}", api_user_delete)
    app.router.add_get("/admin/api/models", api_models)
    app.router.add_put("/admin/api/models/{spec}", api_model_upsert)
    app.router.add_delete("/admin/api/models/{spec}", api_model_delete)
    # El `/test` no colisiona con `{spec}` porque el front manda el spec
    # con `encodeURIComponent`: la barra de `nvidia:meta/llama-…` viaja
    # como %2F y aiohttp la matchea dentro del segmento, decodificándola
    # recién en `match_info`. Verificado con los dos formatos de spec.
    app.router.add_post("/admin/api/models/{spec}/test", api_model_test)
    app.router.add_get("/admin/api/night/templates", api_night_templates)
    app.router.add_put("/admin/api/night/templates", api_night_template_save)
    app.router.add_delete("/admin/api/night/templates/{nombre}",
                          api_night_template_delete)
    app.router.add_get("/admin/", admin_index)
    app.router.add_get("/admin", admin_index)
    app.router.add_get("/admin/static/{filename}", admin_static)
    app.router.add_get("/admin/api/health", api_health)
    app.router.add_get("/admin/api/bot/status", api_bot_status)
    app.router.add_post("/admin/api/bot/start", api_bot_start)
    app.router.add_get("/admin/api/report", api_report)
    app.router.add_get("/admin/api/metrics/summary", api_metrics_summary)
    app.router.add_get("/admin/api/metrics/trends", api_metrics_trends)
    app.router.add_get("/admin/api/logs", api_logs)
    app.router.add_get("/admin/api/search", api_search)
    # Timeouts efectivos: ruta separada del /admin/api/config editable
    # para no colisionar con api_config_get (whitelist RELAY_HOST/REPOS_ROOT).
    app.router.add_get("/admin/api/config/timeouts", api_config_timeouts)
    app.router.add_put("/admin/api/config/expert-timeout",
                        api_config_expert_timeout)
    app.router.add_put("/admin/api/config/tool-timeout",
                        api_config_tool_timeout)
    app.router.add_get("/admin/api/projects", api_projects)
    app.router.add_post("/admin/api/projects", api_project_create)
    app.router.add_get("/admin/api/projects/{slug}", api_projects_slug)
    app.router.add_patch("/admin/api/projects/{slug}", api_projects_patch)
    app.router.add_delete("/admin/api/projects/{slug}", api_projects_delete)
    app.router.add_put("/admin/api/projects/{slug}/git-remote",
                       api_project_git_remote)
    # Iter 10.1: setear/limpiar discord_channel_id dedicado (UX: el
    # bot lo usa cuando pregunta al admin qué canal elegir).
    app.router.add_patch(
        "/admin/api/projects/{slug}/discord-channel",
        api_projects_set_discord_channel)
    # Iter 4.7: workspace — list/read/write de archivos de texto en repo_path
    # + scaffold LLM one-shot. Ver api_workspace_* arriba.
    app.router.add_get("/admin/api/projects/{slug}/workspace/files",
                       api_workspace_files)
    app.router.add_get("/admin/api/projects/{slug}/workspace/file",
                       api_workspace_file_get)
    app.router.add_put("/admin/api/projects/{slug}/workspace/file",
                       api_workspace_file_put)
    app.router.add_post("/admin/api/projects/{slug}/workspace/scaffold",
                        api_workspace_scaffold)
    app.router.add_get("/admin/api/projects/{slug}/night", api_project_night)
    app.router.add_get("/admin/api/projects/{slug}/cbm", api_project_cbm)
    app.router.add_get("/admin/api/projects/{slug}/system-prompt",
                       api_project_system_prompt)
    app.router.add_get("/admin/api/projects/{slug}/git-diff",
                       api_project_git_diff)
    app.router.add_post("/admin/api/projects/{slug}/expert-run",
                        api_project_expert_run)
    app.router.add_get("/admin/api/night-runs/{run_id}/report",
                       api_night_run_report)
    # Iter 5.3: zombies (chats running viejos sin proceso vivo).
    app.router.add_get("/admin/api/chats/zombies", api_zombies_list)
    app.router.add_delete("/admin/api/chats/{chat_id}", api_chat_delete)
    # Iter 9.8: notes-workspace (single-pair con UI chats).
    app.router.add_get("/admin/api/notes", api_notes_list)
    app.router.add_post("/admin/api/notes", api_notes_create)
    # Iter 5.4: listado + detalle de night runs para el tab dedicado.
    app.router.add_get("/admin/api/night-runs", api_night_runs_list)
    app.router.add_get("/admin/api/night-runs/{run_id}", api_night_run_detail)
    app.router.add_get("/admin/api/conversations/facts", api_facts_list)
    app.router.add_get("/admin/api/conversations/memories",
                       api_memories_search)
    # Aprobación de facts (2026-08-21). El POST va a `.../conversations/
    # facts` y no a `/facts` para quedar al lado del GET que ya lista.
    app.router.add_post("/admin/api/conversations/facts", api_fact_create)
    app.router.add_patch("/admin/api/facts/{id}", api_fact_status)
    app.router.add_post("/admin/api/conversations/{conv_id}/extract-facts",
                        api_conversation_extract_facts)
    # Curación de memoria (2026-07-12): sacar facts/memorias sin SQLite a mano.
    app.router.add_delete("/admin/api/facts/{id}", api_fact_delete)
    app.router.add_delete("/admin/api/conversations/{conv_id}/memory",
                          api_memory_delete)
    # Autoaprendizaje (2026-07-12): skills instaladas + borradores del
    # compactador con aprobación humana.
    app.router.add_get("/admin/api/skills", api_skills_list)
    # OJO con el orden: aiohttp matchea en orden de registro y
    # `/skills/{name}` se come cualquier literal que venga después.
    # Todo lo estático de /skills/* va ANTES del {name}.
    app.router.add_get("/admin/api/skills/budget", api_skills_budget)
    app.router.add_get("/admin/api/skills/catalog", api_skills_catalog)
    app.router.add_post("/admin/api/skills/browse", api_skills_browse_start)
    app.router.add_get("/admin/api/skills/browse/{job_id}",
                       api_skills_browse_status)
    app.router.add_get("/admin/api/skills/browse/{job_id}/preview",
                       api_skills_browse_preview)
    app.router.add_post("/admin/api/skills/browse/{job_id}/install",
                        api_skills_browse_install)
    app.router.add_delete("/admin/api/skills/browse/{job_id}",
                          api_skills_browse_discard)
    app.router.add_get("/admin/api/instructions", api_instructions_get)
    app.router.add_put("/admin/api/instructions", api_instructions_put)
    app.router.add_get("/admin/api/skills/{name}", api_skills_get)
    app.router.add_put("/admin/api/skills/{name}", api_skills_put)
    app.router.add_patch("/admin/api/skills/{name}", api_skills_patch)
    app.router.add_delete("/admin/api/skills/{name}", api_skills_delete)
    # Iter 8.5: aplicar skill `when: manual` a un transcript de voz.
    # Pensado para el botón "Crear issue / historia de usuario" de la
    # tab Voz (reusa run_consult: cero tools, system = skill content).
    app.router.add_post("/admin/api/skills/{name}/apply-to-transcript",
                        api_apply_skill_to_transcript)
    app.router.add_get("/admin/api/skill-drafts", api_skill_drafts_list)
    app.router.add_get("/admin/api/skill-drafts/{id}", api_skill_draft_get)
    app.router.add_patch("/admin/api/skill-drafts/{id}", api_skill_draft_patch)
    app.router.add_post("/admin/api/skill-drafts/{id}/approve",
                        api_skill_draft_approve)
    app.router.add_post("/admin/api/skill-drafts/{id}/reject",
                        api_skill_draft_reject)
    app.router.add_delete("/admin/api/skill-drafts/{id}",
                          api_skill_draft_delete)
    app.router.add_post("/admin/api/projects/{slug}/reindex", api_reindex_post)
    app.router.add_post("/admin/api/projects/{slug}/open-vscode",
                        api_project_open_vscode)
    app.router.add_post("/admin/api/projects/from-cbm", api_project_from_cbm)
    app.router.add_post("/admin/api/projects/index/bulk", api_index_bulk)
    app.router.add_get("/admin/api/projects/{slug}/index/status", api_index_status)
    app.router.add_get("/admin/api/projects/{slug}/index/files", api_index_files)
    app.router.add_get("/admin/api/github/board", api_github_board)
    app.router.add_get("/admin/api/github/boards", api_github_boards)
    app.router.add_get("/admin/api/projects/{slug}/github", api_project_github)
    app.router.add_put("/admin/api/projects/{slug}/github-project",
                       api_project_github_link)
    app.router.add_post("/admin/api/projects/{slug}/github-project",
                        api_project_github_create)
    app.router.add_get("/admin/api/projects/{slug}/flags",
                       api_project_flags_get)
    app.router.add_patch("/admin/api/projects/{slug}/flags",
                         api_project_flags_patch)
    app.router.add_get("/admin/api/projects/{slug}/architecture",
                       api_project_architecture)
    app.router.add_get("/admin/api/projects/{slug}/graph/{kind}",
                       api_project_graph)
    app.router.add_post("/admin/api/projects/{slug}/diagrams/llm",
                        api_project_diagrams_llm)
    app.router.add_get("/admin/api/reindex/{job_id}", api_reindex_status)
    app.router.add_get("/admin/api/fs/browse", api_fs_browse)
    app.router.add_get("/admin/api/config", api_config_get)
    app.router.add_put("/admin/api/config", api_config_put)
    app.router.add_get("/admin/api/cbm/orphans", api_cbm_orphans)
    app.router.add_get("/admin/api/cbm/orphans/ignored", api_orphan_ignored_list)
    app.router.add_post("/admin/api/cbm/orphans/ignore", api_orphan_ignore)
    app.router.add_delete("/admin/api/cbm/orphans/ignore", api_orphan_unignore)
    # commands (CRUD + run de prueba)
    app.router.add_get("/admin/api/commands", api_commands_list)
    app.router.add_post("/admin/api/commands", api_commands_upsert)
    app.router.add_post("/admin/api/commands/{name}/run", api_commands_run)
    app.router.add_delete("/admin/api/commands/{name}", api_commands_delete)
    # MCPs (plan MCP_REGISTRY F2+F3). /install es el pipeline real ahora.
    app.router.add_get("/admin/api/mcp", api_mcp_list)
    app.router.add_post("/admin/api/mcp", api_mcp_upsert)
    app.router.add_post("/admin/api/mcp/install", api_mcp_install_start)
    app.router.add_get("/admin/api/mcp/install/{job_id}",
                       api_mcp_install_status)
    app.router.add_post("/admin/api/mcp/install/{job_id}/confirm",
                        api_mcp_install_confirm)
    app.router.add_post("/admin/api/mcp/{name}/health", api_mcp_health)
    app.router.add_patch("/admin/api/mcp/{name}", api_mcp_patch)
    app.router.add_delete("/admin/api/mcp/{name}", api_mcp_delete)
    # CRM local (read-only + sync job + check de conexión).
    app.router.add_get("/admin/api/crm/clients", api_crm_clients_list)
    # La cadena: cliente → sus proyectos → git/kanban.
    app.router.add_get("/admin/api/crm/clients/{cid}", api_crm_client_detail)
    app.router.add_put("/admin/api/projects/{slug}/client",
                       api_project_client_link)
    app.router.add_put("/admin/api/projects/{slug}/deal",
                       api_project_deal_link)
    app.router.add_post("/admin/api/crm/sync", api_crm_sync_post)
    app.router.add_get("/admin/api/crm/sync/status", api_crm_sync_status)
    app.router.add_get("/admin/api/crm/check", api_crm_check)
    app.router.add_post("/admin/api/crm/start", api_crm_start)
    app.router.add_get("/admin/api/crm/health", api_crm_health)
    app.router.add_post("/admin/api/crm/digest", api_crm_digest)
    app.router.add_post("/admin/api/restart", api_admin_restart)
