"""Rutas de arquitectura, grafos y diagramas asistidos por CBM/LLM."""
from __future__ import annotations
import json
import logging
import re
import time
from aiohttp import web
from .experts import cbm_binary_path
from .app_state import DB_KEY
from .admin_common import _serialize

from .admin_cbm import (
    _CBM_SCALAR_RE, _cbm_cli_text, _cbm_project_name, _parse_cbm_sections,
)

logger = logging.getLogger("relay.admin")

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

_ARCH_ASPECTS = {"structure", "dependencies", "routes", "hotspots",
                 "boundaries", "layers", "clusters", "file_tree"}

_ARCH_TTL_S = 120.0

_arch_cache: dict[tuple[str, str], tuple[float, dict]] = {}

async def _project_for_request(request: web.Request):
    # Import local para conservar el acoplamiento actual de rutas admin.
    from .admin_workspace import _project_for_request as resolve_project
    return await resolve_project(request)

async def api_project_architecture(request: web.Request) -> web.Response:
    """GET /admin/api/projects/{slug}/architecture?aspects=layers,boundaries

    Devuelve el grafo semántico que cbm ya calcula (capas, boundaries con
    peso de llamadas, clusters por cohesión, hotspots por fan-in, rutas HTTP
    y file_tree real). Es la fuente que el tab Diagramas necesitaba: antes
    dibujaba `ls` del root con fs/browse y lo llamaba "arquitectura".
    """
    slug = request.match_info["slug"]
    project, error = await _project_for_request(request)
    if error is not None:
        return error
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
    project, error = await _project_for_request(request)
    if error is not None:
        return error
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
    project, error = await _project_for_request(request)
    if error is not None:
        return error
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
