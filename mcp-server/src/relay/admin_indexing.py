"""Rutas de archivos indexados y reindexación masiva."""
from __future__ import annotations
import asyncio
import json
import logging
import time
from pathlib import Path
from aiohttp import web
from .experts import cbm_binary_path
from .app_state import DB_KEY
from .admin_common import _serialize, _set_job

from .admin_cbm import (
    CBM_INDEX_TIMEOUT_S, _cbm_cli_text, _cbm_env, _cbm_list_projects,
    _cbm_project_name,
    _mark_job_done,
)
from .admin_diagrams import _parse_cbm_search_graph

logger = logging.getLogger("relay.admin")

async def api_index_files(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug}/index/files?limit=N&sort=path|name."""
    slug = request.match_info["slug"]
    from .admin_workspace import _project_for_request
    project, error = await _project_for_request(request)
    if error is not None:
        return error
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
