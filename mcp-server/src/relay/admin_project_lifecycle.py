"""Alta, importación y apertura de proyectos y plantillas nocturnas."""
from __future__ import annotations
import asyncio
import json
import logging
import time
from pathlib import Path
from aiohttp import web
from .experts import cbm_binary_path
from .app_state import DB_KEY
from .admin_common import _serialize

from .admin_cbm import _cbm_list_projects
from .admin_workspace import _start_index_job

logger = logging.getLogger("relay.admin")

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


def _slugify(raw: str) -> str:
    """Normaliza a slug seguro: [a-z0-9-], sin dobles guiones."""
    slug = (raw or "").strip().lower().replace(" ", "-")
    slug = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in slug)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "unnamed"
