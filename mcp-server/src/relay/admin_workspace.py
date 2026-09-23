"""Rutas de workspace y reindexación de archivos de proyectos."""
from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
from typing import Optional
from aiohttp import web
from . import config as relay_config
from .experts import cbm_binary_path
from pydantic import BaseModel
from pydantic_ai import Agent
from .experts import (
    ModelUnavailable, build_model, structured_output_settings)
from .app_state import DB_KEY
from .admin_common import _get_job, _set_job
from . import coordination
from .task_workspace import TaskWorkspaceError, resolved_project

from .admin_cbm import CBM_INDEX_TIMEOUT_S, _cbm_env, _mark_job_done

logger = logging.getLogger("relay.admin")

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

WORKSPACE_FILE_READ_CAP = 1 * 1024 * 1024

WORKSPACE_FILE_WRITE_CAP = 64 * 1024

WORKSPACE_LIST_CAP = 500


async def _project_for_request(request: web.Request):
    """Proyecto copiado con la raíz de la conversación, si fue indicada."""
    db = request.app[DB_KEY]
    slug = request.match_info["slug"]
    project = await db.get_project(slug)
    if not project:
        return None, web.json_response({"error": "not found"}, status=404)
    conv_id = (request.query.get("conversation")
               or request.query.get("conversation_id") or "").strip()
    if not conv_id:
        return project, None
    conv = await db.get_conversation(conv_id)
    if not conv:
        return None, web.json_response(
            {"error": "conversación desconocida"}, status=404)
    if (conv.get("project_slug") or "").casefold() != slug.casefold():
        return None, web.json_response(
            {"error": "la conversación pertenece a otro proyecto"}, status=400)
    try:
        return await resolved_project(db, project, conv_id), None
    except TaskWorkspaceError as exc:
        return None, web.json_response(
            {"error": "task_workspace_blocked", "message": str(exc)},
            status=409)

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
    slug = request.match_info["slug"]
    project, error = await _project_for_request(request)
    if error is not None:
        return error
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
    project, error = await _project_for_request(request)
    if error is not None:
        return error
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

@coordination.guard_workspace(DB_KEY, source="workspace")
async def api_workspace_file_put(request: web.Request) -> web.Response:
    """PUT /admin/api/projects/{slug}/workspace/file {path, content}

    Crea o sobrescribe un archivo de texto dentro del repo. Crea los
    subdirectorios intermedios. NO permite sobrescribir si el archivo
    existe y el body trae `overwrite=false` (default false, v1 seguro).
    """
    project, error = await _project_for_request(request)
    if error is not None:
        return error
    if (project.get("defaults_json") or {}).get("read_only"):
        return web.json_response({"error": "workspace read_only"}, status=403)
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

@coordination.guard_workspace(DB_KEY, source="workspace")
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
    slug = request.match_info["slug"]
    project, error = await _project_for_request(request)
    if error is not None:
        return error
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    prompt = (body.get("prompt") or "").strip()
    apply_to_disk = bool(body.get("apply", False))
    overwrite = bool(body.get("overwrite", False))
    if apply_to_disk and (project.get("defaults_json") or {}).get("read_only"):
        return web.json_response({"error": "workspace read_only"}, status=403)
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
    project, error = await _project_for_request(request)
    if error is not None:
        return error
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
