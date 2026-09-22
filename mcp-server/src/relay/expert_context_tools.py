"""Herramientas de contexto: preguntas, código, skills y bitácora."""
from __future__ import annotations
import asyncio
import json
import logging
import uuid
from pydantic_ai import Tool
from pydantic_ai.exceptions import ModelRetry
from . import cbm_runtime, expert_evidence, expert_git, expert_history

logger = logging.getLogger("relay.experts")


def build_context_tools(*, project, db, chat_id, conversation_id, _is_notes, bitacora, _emit_progress):
    """Herramientas de contexto con sus datos por corrida, sin estado global."""
    tools = []
    _q_state: dict = {}
    if db is not None and chat_id:

        async def ask_human(pregunta: str, evidencia: str, opciones: str = "",
                            detalle: str = "") -> str:
            """Pregunta algo al humano y TERMINA tu turno.

            Usala cuando de verdad no podés seguir sin una decisión suya:
            hay que INSTALAR algo, hay que elegir entre caminos que no son
            equivalentes, o el pedido es ambiguo de una forma que cambia
            el resultado. No la uses para pedir permiso de rutina: tenés
            libertad para leer, escribir y ejecutar.

            Después de llamarla, cerrá con un resumen de lo que hiciste y
            de qué estás esperando. NO sigas trabajando ni inventes la
            respuesta: el turno termina acá y la respuesta del humano
            llega como el mensaje siguiente.

            Args:
                pregunta: la pregunta, en una línea y concreta.
                evidencia: OBLIGATORIO. Qué archivos leíste concretamente
                    y qué encontraste en ellos — no un resumen de tu
                    conclusión. Mal: "el inventario está desactualizado".
                    Bien: "leí Service/Notifications/NotificableAttribute.cs:
                    tiene el namespace anidado; el inventario dice textual
                    'asumo, no leí el resto'". Sin esto, quien responda
                    tiene que reabrir el repo para verificar tu pregunta a
                    mano — y eso es lo que hizo que 11 preguntas quedaran
                    sin contestar semanas enteras.
                opciones: alternativas separadas por `|`. Ej:
                    "instalalo vos|lo instalo yo|seguí sin eso".
                    Vacío = respuesta libre.
                detalle: contexto que el humano necesita para decidir
                    (qué falta, para qué, qué pasa si dice que no).
            """
            motivo = expert_evidence._evidencia_insuficiente(evidencia)
            if motivo:
                raise ModelRetry(
                    f"La `evidencia` {motivo}. Volvé a llamar `ask_human` "
                    "con los archivos concretos que leíste y lo que "
                    "encontraste en ellos — sin eso, quien responda no "
                    "puede verificar la pregunta sin reabrir el repo.")
            opts = [o.strip() for o in (opciones or "").split("|") if o.strip()]
            q = {
                "title": (pregunta or "").strip()[:400],
                "detail": (detalle or "").strip()[:2000],
                "evidencia": evidencia.strip()[:2000],
                "options": [{"key": f"o{i}", "label": o}
                            for i, o in enumerate(opts[:6])],
            }
            q_id = f"q_{uuid.uuid4().hex[:8]}"
            try:
                await db.create_expert_question(
                    q_id, chat_id, json.dumps(q, ensure_ascii=False),
                    conversation_id=conversation_id or None,
                    project_slug=project.get("slug"),
                    kind="install" if expert_evidence._huele_a_instalacion(pregunta, detalle)
                    else ("choice" if opts else "text"))
            except Exception as e:  # noqa: BLE001 — preguntar no rompe el run
                logger.warning("no pude registrar la pregunta (%r)", e)
                return ("No pude registrar la pregunta. Explicá en tu "
                        "respuesta final qué necesitás del humano.")
            _q_state["asked"] = q_id
            logger.info("pregunta al humano %s (chat=%s): %s",
                        q_id, chat_id[:8], q["title"][:80])
            try:
                await _emit_progress(phase="question", tool="ask_human",
                                     message=q["title"][:200])
            except Exception:  # noqa: BLE001
                pass
            return (
                f"Pregunta registrada ({q_id}). El humano la va a ver en el "
                "chat. TERMINÁ TU TURNO AHORA: escribí un resumen corto de "
                "lo que hiciste y de qué estás esperando. No sigas "
                "trabajando ni asumas una respuesta.")

        tools.append(Tool(ask_human, takes_ctx=False))

    # Iter 9.8: cbm_query no se adjunta en el workspace de notas.
    if (cbm_runtime.cbm_binary_path() is not None and not _is_notes
            and not (project.get("defaults_json") or {}).get("task_feedback")):
        # El experto está scopeado a UN proyecto: derivamos el nombre de cbm
        # del repo_path y lo inyectamos en cada query. El LLM NO puede adivinar
        # este nombre (es el path mangleado, ej "C-Users-developer-source-repos-
        # AuroraDemo-SampleApp") — antes lo omitía y cbm respondía "project not found
        # or not indexed", forzando al experto a explorar a mano con read_file/
        # list_dir (19 tool calls en vez de 1). night.py ya usaba _cbm_project_name.
        from .admin_cbm import _cbm_project_name
        cbm_project = _cbm_project_name(project["repo_path"])

        async def cbm_query(tool: str, args_json: str = "{}") -> str:
            """Consulta el grafo de código del proyecto actual (codebase-memory).

            USALA ANTES de explorar a mano con read_file/list_dir: una
            búsqueda bien elegida reemplaza decenas de lecturas. Cada
            llamada tiene ~1s de costo fijo — prefiere pocas llamadas
            precisas sobre muchas exploratorias.

            NO pases `project` en args_json: se inyecta solo.

            Tools disponibles (nombre en `tool`, args en `args_json`):

            - search_graph — buscar funciones/clases/rutas/variables.
              Modos combinables: query="update settings" (full-text BM25),
              name_pattern=".*regex.*", semantic_query=["send","publish"]
              (ARRAY de keywords, búsqueda vectorial). Filtros: label,
              file_pattern, min_degree. Paginación: limit (default 40) +
              offset; el response trae total y has_more.
            - trace_path — callers/callees/impacto de una función. args:
              function_name*, direction (inbound|outbound|both), depth
              (default 3), mode (calls|data_flow|cross_service).
            - get_code_snippet — leer el código de un símbolo. args:
              qualified_name* (obtenelo con search_graph primero).
            - search_code — grep enriquecido con el grafo (dedup por
              función, ranking estructural). args: pattern*, file_pattern
              (glob), path_filter (regex), mode (compact|full|files),
              regex (bool), limit (default 10).
            - get_architecture — vista de alto nivel: paquetes, servicios,
              dependencias, clusters (módulos de facto). args: path (scope
              a un subdirectorio), aspects (["overview"] = compacto).
            - query_graph — Cypher para patrones multi-hop y métricas de
              complejidad por función (complexity, transitive_loop_depth,
              linear_scan_in_loop, alloc_in_loop...). args: query*.
            - detect_changes — cambios vs base_branch (default main) y su
              impacto. args: scope, depth, since (ej "HEAD~5").
            - get_graph_schema — labels y edge types del grafo.
            - index_status / list_projects — estado del índice.
            - manage_adr — leer/actualizar ADRs (mode get|update|sections).

            Args:
                tool: nombre del tool cbm (ver lista de arriba).
                args_json: JSON string con los args (SIN `project`).
                    Vacío = {}.
            """
            try:
                args = json.loads(args_json) if args_json else {}
            except json.JSONDecodeError as e:
                return json.dumps({"error": f"args_json inválido: {e}"})
            if not isinstance(args, dict):
                args = {}
            # Forzamos el project correcto (el LLM lo omite o lo adivina mal).
            if tool != "list_projects":
                args["project"] = cbm_project
            # Cap de tokens: search_graph sin limit devuelve hasta 200
            # nodos — un default más chico obliga a paginar consciente.
            if tool == "search_graph":
                args.setdefault("limit", 40)
            raw = await cbm_runtime.cbm_call(tool, args, timeout=30.0)
            capped = expert_history._cap_text(raw)
            # Bug fix 2026-07-20: si cbm devolvió error, inyectar un
            # `fallback_hint` explícito para que el LLM sepa qué hacer.
            # Sin esto, el modelo recibía `{"error": "..."}` y volvía
            # a llamar cbm_query con los mismos args (loop que terminaba
            # en `Tool exceeded max retries count of 3`). Con el hint
            # en el response, el LLM lee la sugerencia en el mismo turno
            # y cae a list_dir/read_file directo.
            # Ponytail: el hint NO se inyecta cuando cbm anduvo bien.
            try:
                parsed = json.loads(capped)
                if isinstance(parsed, dict) and parsed.get("error"):
                    parsed["fallback_hint"] = (
                        "cbm no respondió con esta query. NO la repitas. "
                        "Caé a `list_dir(path)` para navegar el repo a "
                        "mano, y `read_file(path)` para leer el archivo "
                        "puntual que necesitabas. La próxima tool call "
                        "debería ser filesystem, no otra cbm_query."
                    )
                    return json.dumps(parsed, ensure_ascii=False)
            except (json.JSONDecodeError, ValueError):
                # cbm devolvió JSON inválido o string suelto — no
                # podemos enriquecer, devolvemos lo que hay.
                pass
            return capped

        tools.append(Tool(cbm_query, takes_ctx=False))

    # Iter 10.4: tool nativa `read_skill(name)` para modo compact.
    # Reusa `read_skill_sync` (síncrona, hace FS read) envuelta en
    # asyncio.to_thread para no bloquear el event loop. Cap de 20KB
    # en el output: una SKILL.md razonable entra; un script de 200KB
    # no. Devuelve "" + hint si la skill no existe, así el LLM cae a
    # otra acción en vez de loop.
    # Ponytail: si la skill no está en el índice, NO la inventamos.
    # El LLM tiene que saber que pidió algo que no existe.
    from . import skills as _skills_mod  # lazy: evita ciclo de imports
    # El repo primero: una skill versionada junto al codigo que describe
    # le gana a la global del mismo nombre (ver `skills.skills_dirs`).
    _skills_dirs = _skills_mod.skills_dirs(project.get("repo_path") or "")

    async def read_skill(name: str) -> str:
        """Lee el cuerpo de una skill por nombre (ej: "ponytail", "4bis-shortcuts").

        ÚSALA cuando el system prompt te diga que una skill matchea
        tu tarea. La descripción que viste en el bloque de skills es
        un resumen de 1 línea: el cuerpo tiene las reglas, los
        comandos concretos, y los casos de uso. Inventar el cuerpo
        te sale caro: bug.

        Las skills de la línea "On-demand" del system prompt viajan
        SOLO con el nombre: si el nombre suena a tu tarea, esta tool
        es la única forma de saber qué dicen. No las descartes por no
        tener descripción.

        Args:
            name: nombre de la skill (el del frontmatter, ej
                "ponytail"). Case-insensitive en la búsqueda.
        """
        # to_thread: read_skill_sync hace open() + read(), bloqueante.
        # Para una skill chica no importa, pero la API es pública y
        # no queremos que un LLM lento + skill grande nos frene el
        # loop del relay.
        try:
            content = await asyncio.to_thread(
                _skills_mod.read_skill_multi, _skills_dirs, name,
            )
        except Exception as e:  # noqa: BLE001 — best-effort
            return f"[read_skill] error leyendo {name!r}: {e!r}"
        if content is None:
            # _build_index lee el dir (un glob) y es suficientemente
            # rápido para un tool call ocasional; no comparte cache con
            # el SkillCache app-level.
            # auto + manual: listar solo las auto le decía al modelo que
            # las on-demand no existen —justo las que esta tool está
            # para leer— y lo mandaba a abandonar una skill instalada.
            index = _skills_mod.build_index_multi(_skills_dirs)
            available = ", ".join(
                s.name for s in index.auto_skills() + index.manual_skills()
            ) or "(ninguna)"
            await _emit_progress(phase="tool_call", tool="read_skill",
                                 skill=name, encontrada=False)
            return (
                f"[read_skill] skill {name!r} no encontrada. "
                f"Disponibles: {available}. "
                "Si ninguna matchea, no la invoques de nuevo."
            )
        # Cap defensivo: una SKILL.md puede ser un libro si alguien
        # la importó mal. 20KB ≈ 5K tokens, alcanza para cualquier
        # skill razonable y previene que un bad actor infle el
        # context del LLM.
        if len(content) > 20_000:
            content = content[:20_000] + (
                f"\n\n[read_skill] truncado a 20KB; total "
                f"{len(content):,} chars."
            )
        # Fase 0 (2026-09-08): registrar CUAL skill se leyo. El evento de
        # tool call ya guardaba `tool="read_skill"` pero no el argumento,
        # asi que de 57 chats que la llamaron no se podia saber que
        # leyeron ni si servia. Sin este dato, medir si las skills
        # mejoran algo es imposible: `progress_events` es lo unico que se
        # persiste por turno.
        await _emit_progress(phase="tool_call", tool="read_skill",
                             skill=name, encontrada=True)
        return content

    tools.append(Tool(read_skill, takes_ctx=False))

    # Cambios sin commitear: antes viajaban SIEMPRE en el system prompt
    # (ADR-020). El bloque cambia en cuanto el experto escribe un
    # archivo, así que el system prompt dejaba de ser estable y el
    # provider tiraba la cache del historial entero en cada resume
    # (medido: 0% de hit contra 94% de los runs que no lo tocaban).
    # Como tool el costo es on-demand y el result entra en la elisión.
    if not _is_notes:
        _repo_path = project["repo_path"]

        async def git_diff() -> str:
            """Cambios sin commitear del repo (git status + git diff HEAD).

            Úsala al empezar si necesitas saber qué hay pendiente en el
            working tree. El diff va capeado a 20KB.
            """
            return await asyncio.to_thread(
                expert_git._build_git_diff_block_sync, _repo_path,
            ) or "Working tree clean (o el repo no es git)."

        tools.append(Tool(git_diff, takes_ctx=False))

    def anotar(hecho: str) -> str:
        """Anota un hecho VERIFICADO en la bitácora del run.

        Tu historial se recorta: los resultados de tools viejas se
        reemplazan por un muñón. Lo que anotes aquí sobrevive todo el run
        y lo seguirás viendo al final, cuando escribas la respuesta.

        Anota el resultado, no la intención: "el build pasó en 7.66s",
        no "corrí el build". Los hechos negativos también valen.

        Args:
            hecho: una línea, concreta y comprobada. Ej:
                "npm run lint: 3 errores, 215 warnings (exit=1)".
        """
        return bitacora.anotar(hecho)

    tools.append(Tool(anotar, takes_ctx=False))

    def plan_step_done(paso: int, nota: str = "") -> str:
        """Marca un paso del plan como completado (Etapa B, P1).

        El panel dibuja el plan del run como un grafo, y necesita saber
        en qué paso vas para pintar la columna que está verde. Vos
        sos la única fuente honesta de ese puntero — el verificador lo
        corrobora al final, pero no puede adivinar.

        Args:
            paso: el número del paso (1-based, igual al que dice el
                plan que viste en el system prompt). Si el plan dice
                "1. ... 2. ... 3. ...", pasás 1, 2 o 3.
            nota: una línea corta opcional con qué quedó hecho en
                ese paso. Aparece al hover del nodo.

        Devuelve confirmación corta. Acepta cualquier entero (un número
        mal puesto no puede cortar el run): el orquestador filtra los
        que caen fuera del rango del plan antes de armar el dict que
        sale a la UI.
        """
        bitacora.marcar_paso(paso, nota)
        return f"paso {paso} marcado" + (f" ({nota})" if nota else "")

    tools.append(Tool(plan_step_done, takes_ctx=False))

    return tools, _q_state
