"""Rutas de GitHub, flags de proyecto y navegación del filesystem."""
from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
from aiohttp import web
from . import config as relay_config
from . import github as github_mod
from .experts import cbm_binary_path
from .app_state import DB_KEY

from .admin_cbm import _cbm_list_projects

logger = logging.getLogger("relay.admin")

_REPO_MARKERS = (".git", "*.sln", "*.csproj", "*.fsproj", "package.json",
                 "pyproject.toml", "Cargo.toml", "go.mod", "pom.xml",
                 "build.gradle", "build.gradle.kts")

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
    # request (example-client ~2000 con el tope, sample-app ~930) y no todo
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
