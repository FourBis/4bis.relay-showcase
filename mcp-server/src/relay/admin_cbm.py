"""Admin UI: módulo cohesivo extraído de admin.py."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from . import config as relay_config
from .experts import cbm_binary_path
from .admin_common import mark_job_done

logger = logging.getLogger("relay.admin")

CBM_INDEX_TIMEOUT_S = int(os.environ.get("CBM_INDEX_TIMEOUT", "600"))

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
    from . import cbm_runtime
    # Sesión ya apagada: ir DERECHO al subprocess. Si no, `cbm_call` cae
    # a `cbm_cli_call`, que corre el mismo `cbm cli` y siempre falla acá
    # (estos 7 tools imprimen tabla, no JSON) — y recién entonces
    # spawnearíamos de nuevo. Serían DOS spawns fríos (2078ms medidos)
    # justo en el escenario degradado, contra 1031ms de uno solo.
    if cbm_runtime._cbm_session_off:
        return await _cbm_cli_text_subprocess(*args)
    try:
        out = await cbm_runtime.cbm_call(tool, json.loads(args_json), timeout=30.0)
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

_CBM_SECTION_RE = re.compile(r"^(\w+):\s*(\d+)\s*\(cols:\s*([^)]+)\)\s*$")

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

_CBM_PROJECTS_TTL_S = 30.0

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

def _mark_job_done(job_id: str) -> None:
    mark_job_done(job_id)
    _invalidate_cbm_projects_cache()

def _norm_path_key(p: str) -> str:
    """Clave canónica para comparar paths Windows: separadores unificados,
    sin trailing slash, case-insensitive. Dos repos con el mismo basename
    en carpetas distintas NO deben colisionar (por eso path completo)."""
    return (p or "").replace("\\", "/").rstrip("/").lower()

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
