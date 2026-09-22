"""Model-backed task execution, autosplit and graph callbacks."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from . import config
from . import grafo as G
from . import finalization

logger = logging.getLogger("relay.orquestador")

async def _intentar_autosplit(
    db, proyecto: dict, grafo_row: dict, tarea: dict, resultado_parcial: str,
) -> list[str]:
    """Fase 2B (2026-09-02): reparte el trabajo RESTANTE de un nodo que
    cortó por `budget_split` (tope de 250 tool calls de la Fase 1, ver
    `config.expert_max_tool_calls`) en subtareas, en vez de dejarlo
    fallado esperando al humano.

    Devuelve los ids agregados, o `[]` si no se subdividió — por
    CUALQUIER motivo: ineligible, ya subdividido, el modelo no contestó,
    devolvió basura, o `add_tasks_to_graph` lo rechazó. `[]` no es un
    error: el caller sigue con el camino de siempre (nodo fallado) como
    si esto no existiera — ver el `try/except` alrededor del call site.

    Los límites que evitan que una subdivisión mala se MULTIPLIQUE en
    vez de fallar (una recursión sin tope sería peor que el problema
    que esto resuelve):
      - **profundidad 1**: un nodo con `parent_id` (ya es una subtarea)
        no se vuelve a subdividir, aunque él mismo corte con
        `budget_split`.
      - **fan-out 2..5**: `parsear_tareas(max_tareas=5)` topea de
        arriba; menos de 2 no es una subdivisión (mismo criterio que
        `planificador.armar_grafo`).
      - **una sola vez por nodo**: se chequea consultando la BASE (¿ya
        hay tareas con `parent_id=<este nodo>`?), no memoria del
        proceso — el relay se reinicia y una bandera en memoria no
        sobrevive.

    Reusa `planificador`: `cascada_planner` + `_pedir_plan` (fallback
    entre modelos) y `parsear_tareas` (auto-repara JSON envuelto, deps
    por título, deps colgadas) son el mismo parser que arma los grafos
    hoy — escribir otro acá sería duplicar el punto que más falla.
    """
    if tarea.get("parent_id"):
        return []
    graph_id = tarea["graph_id"]
    existentes = await db.list_tasks(graph_id)
    if any(t.get("parent_id") == tarea["id"] for t in existentes):
        return []  # ya se subdividió una vez — no se reintenta

    from . import experts, planificador as P

    contexto = (
        "Esta NO es una tarea nueva: es la continuación de una que se "
        "quedó sin presupuesto a mitad de camino (demasiadas tool "
        "calls). Repartí SOLO lo que FALTA en subtareas chicas — NO "
        "repitas lo que ya está hecho.\n\n"
        "### Lo que esta tarea alcanzó a hacer antes de cortar\n"
        f"{(resultado_parcial or '(no reportó nada)').strip()[:3000]}\n"
    )
    objetivo = (
        f"{grafo_row.get('objetivo') or ''}\n\n"
        f"Tarea que hay que terminar de repartir: "
        f"{tarea.get('titulo') or tarea['id']}\n"
        f"{(tarea.get('detalle') or '').strip()}"
    ).strip()
    prompt = P.PROMPT.format(
        objetivo=objetivo, contexto=f"\n## Contexto\n{contexto}\n",
        max_tareas=5)

    specs = P.cascada_planner(proyecto)
    r, fallo, _usado = await P._pedir_plan(specs, 0, prompt, experts)
    if r is None:
        logger.warning(
            "autosplit de %s: el planificador no contestó en ninguno de "
            "%d modelo(s) (%r)", tarea["id"], len(specs), fallo)
        return []

    try:
        subtareas = P.parsear_tareas(getattr(r, "output", "") or "",
                                     max_tareas=5)
    except ValueError as e:
        logger.warning("autosplit de %s: plan ilegible (%s)", tarea["id"], e)
        return []
    if len(subtareas) < 2:
        # Igual que `armar_grafo`: si entra en una sola tarea, no era
        # una subdivisión — hubiera terminado en el mismo run.
        logger.info(
            "autosplit de %s: el modelo devolvió %d tarea(s), no es una "
            "subdivisión", tarea["id"], len(subtareas))
        return []

    # Ids únicos (tasks.id es PK global): mismo criterio que
    # `_ids_unicos`, con un prefijo propio para no chocar ni con el
    # grafo ni con un intento de subdivisión anterior de OTRO nodo.
    #
    # El prefijo arranca en `tarea["id"]` y NO en `graph_id`: los ids de
    # tarea YA vienen prefijados con el grafo (`g_xxx:t4`), así que
    # anteponerlo otra vez lo duplicaba. Visto en la prueba en vivo del
    # 2026-09-03: `un grafo de ejemplo:un grafo de ejemplo:t4:split:analisis-23-26`.
    # Cosmético —nada se rompía— pero esos ids se leen en la UI, en los
    # logs y en `task_deps`.
    subtareas = P._ids_unicos(subtareas, f"{tarea['id']}:split")

    # Si el modelo no declaró archivos, hereda los del nodo original: el
    # scheduler los usa para no correr en paralelo dos que se pisan, y
    # las subtareas tocan el mismo terreno que la tarea que reemplazan.
    archivos_originales = _archivos_de(tarea)
    for t in subtareas:
        if not t.get("archivos"):
            t["archivos"] = list(archivos_originales)

    try:
        agregados = await db.add_tasks_to_graph(
            graph_id, subtareas, reemplaza=tarea["id"])
    except (ValueError, G.GrafoInvalido) as e:
        logger.warning(
            "autosplit de %s: add_tasks_to_graph lo rechazó (%s)",
            tarea["id"], e)
        return []

    logger.info("grafo %s: nodo %s subdividido en %d subtareas: %s",
                graph_id, tarea["id"], len(agregados), ", ".join(agregados))
    return agregados


def _archivos_de(tarea: dict) -> list[str]:
    """`tasks.archivos` es JSON en texto; acá siempre lista."""
    crudo = tarea.get("archivos")
    if not crudo:
        return []
    try:
        lista = json.loads(crudo) if isinstance(crudo, str) else list(crudo)
    except (ValueError, TypeError):
        return []
    return [str(a) for a in lista] if isinstance(lista, list) else []


def ejecutor_minimax(db, proyecto: dict, grafo_row: dict, *,
                     modelo: str = "", progreso_de=None):
    """Devuelve un `ejecutar(tarea)` que corre UN nodo con el modelo barato.

    Tres decisiones que hacen que un nodo sea corto de verdad, que es de
    lo que depende todo lo demás:

    1. **`run_expert` y no `run_expert_staged`.** El plan YA existe — es
       el grafo. Pasar por el runner de etapas pagaría un turno de
       planificación por nodo para re-derivar lo que el coordinador ya
       decidió, más uno de verificación y otro de documentación.
    2. **Sin historial del hilo.** El nodo recibe su objetivo y el
       resultado de sus dependencias, no la conversación entera. Es lo
       que lo hace barato y lo que evita que el contexto crezca turno a
       turno hasta el cap.
    3. **Archivos reservados adentro.** El run puede leer todo pero no
       escribir lo que otra tarea tiene tomado.

    `progreso_de` es una **factory**, no un callback: recibe el `chat_id`
    del nodo y devuelve su `on_progress`. Tiene que ser así porque
    `make_progress_callback` registra el `RunProgress` en el store bajo
    ese id, y el chat del nodo recién existe acá adentro. Antes este
    parámetro era un `on_progress` suelto que NADIE pasaba nunca — con lo
    cual los nodos del grafo no reportaban a ningún lado y el panel no
    tenía forma de decir qué herramienta estaba corriendo. Medido el
    24/8: veinte minutos de un nodo trabajando sin una sola señal.
    """
    from . import experts

    async def ejecutar(tarea: dict) -> dict:
        reservados = await db.files_claimed_by_others(tarea["id"])
        prompt = _prompt_de_tarea(grafo_row, tarea,
                                  await db.list_tasks(tarea["graph_id"]))
        chat_id = ""
        if hasattr(db, "create_chat"):
            chat_id = await db.create_chat(
                project_slug=proyecto.get("slug"), source="grafo",
                author="orquestador", target=proyecto.get("slug"),
                conversation_id=grafo_row.get("conversation_id"),
                # El título de la tarea y no `prompt`: el prompt del nodo
                # lo arma `_prompt_de_tarea` y trae el objetivo entero del
                # grafo, que como título de una fila no dice cuál nodo es.
                user_prompt=tarea.get("titulo") or "")
        # Asocia el chat a la tarea desde el arranque, no al cerrar: la
        # ventana en la que se mira el trabajo en vuelo es justamente
        # esta, y sin `chat_id` la fila no permite saltar al chat.
        # Idempotente: al cerrar, `_terminar_tarea` reescribe el mismo
        # campo sobre la misma fila.
        if chat_id:
            await db.update_task(tarea["id"], chat_id=chat_id)
        # Después de crear el chat: el callback se ata a ese id.
        on_progress = None
        if progreso_de is not None and chat_id:
            try:
                on_progress = progreso_de(chat_id)
            except Exception as e:  # noqa: BLE001 — telemetría, no el trabajo
                logger.warning("no pude armar el progreso del nodo %s (%r)",
                               tarea.get("id"), e)
        rescue: dict = {}
        started = asyncio.get_running_loop().time()
        try:
            res = await experts.run_expert(
                proyecto, prompt, db=db, model_override=modelo,
                chat_id=chat_id,
                conversation_id=grafo_row.get("conversation_id") or "",
                on_progress=on_progress,
                steer=getattr(on_progress, "steer", None),
                rescue=rescue,
                archivos_reservados=reservados)
        except asyncio.CancelledError:
            rescue.setdefault("duration_ms", int(
                (asyncio.get_running_loop().time() - started) * 1000))
            rescue.update(content="run cancelado", phase_at_end="cancelled")
            if chat_id and hasattr(db, "finish_chat"):
                await _cerrar_chat(db, chat_id, "cancelled", "cancelado", rescue)
            raise
        except Exception as e:  # noqa: BLE001 — un nodo roto no voltea el grafo
            if chat_id and hasattr(db, "finish_chat"):
                await _cerrar_chat(db, chat_id, "error", str(e))
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "chat_id": chat_id, "modelo": modelo}

        fase = res.get("phase_at_end") or ""
        # La lista canónica vive en `grafo` (ver `FASES_INCOMPLETAS`): acá
        # faltaban `budget_exceeded`, `provider_error` y `off_plan`, así
        # que un nodo cortado a mitad quedaba `hecho` y el grafo cerraba
        # anunciando un trabajo que no se hizo.
        roto = fase in G.FASES_INCOMPLETAS

        subdivididas: list[str] = []
        if fase == "budget_split":
            from . import config
            if config.grafo_autosplit_habilitado():
                try:
                    subdivididas = await _intentar_autosplit(
                        db, proyecto, grafo_row, tarea,
                        res.get("content") or "")
                except asyncio.CancelledError:
                    await _cerrar_chat(db, chat_id, "cancelled", "cancelado al subdividir",
                                      {**res, "phase_at_end": "cancelled"})
                    raise
                except Exception as e:  # noqa: BLE001 — falla segura
                    logger.exception(
                        "autosplit de %s reventó (%r); sigue como "
                        "budget_split normal", tarea.get("id"), e)

        error = f"el nodo terminó en {fase!r}" if roto else ""
        # Un nodo que abre una pregunta NO está roto, pero tampoco
        # terminó: `ok` se va a False por `question_id` y hasta hoy
        # `error` quedaba "". Resultado medido (barrido del 4/9): de 81
        # chats en `error`, 29 (36 %) tienen la columna `error` NULL o
        # vacía —todos `source='grafo'`, `author='orquestador'`,
        # `phase_at_end='writing'`—, o sea runs que figuran como fallidos
        # sin decir por qué cuando en realidad están esperando al humano.
        if not error and res.get("question_id"):
            error = (f"el nodo abrió una pregunta al humano "
                     f"({res['question_id']}) y quedó en pausa")
        if subdivididas:
            error = (f"subdividido en {len(subdivididas)} subtareas: "
                     + ", ".join(subdivididas))
        salida = {
            "ok": not roto and not res.get("question_id"),
            "resultado": (res.get("content") or "").strip(),
            "error": error,
            "chat_id": chat_id or res.get("chat_id") or "",
            "modelo": res.get("model") or modelo,
            "pregunta": res.get("question_id") or "",
        }
        if chat_id and hasattr(db, "finish_chat"):
            # Status del chat: SOLO depende de `roto` (nodo realmente
            # roto), no de `salida["ok"]`. `ok` mezcla dos preguntas
            # distintas —¿el grafo sigue avanzando? y ¿esto salió bien?—
            # y para la primera un nodo que abrió una pregunta también
            # es `False` (correcto: no está `hecho`, el grafo lo espera).
            # Pero acá abajo esa misma bandera etiquetaba como `error` un
            # chat que solo pausó a preguntarle algo a un humano. Medido
            # sobre relay.db: camino UI 79 preguntas → 79 `ok`; camino
            # grafo 32 preguntas → 32 `error`. Misma tool, misma pausa,
            # etiqueta opuesta según qué código cierra el chat.
            await _cerrar_chat(db, chat_id, "error" if roto else "ok",
                               salida["error"], res)
        return salida

    return ejecutar


async def _cerrar_chat(db, chat_id: str, status: str, error: str = "",
                       res: Optional[dict] = None) -> None:
    """Mismo cierre durable que HTTP: los artefactos se pueden reintentar."""
    from . import finalization
    if status != "ok" and not error:
        error = f"cerrado como {status!r} sin motivo declarado (bug del caller)"
    r = res or {}
    fields = dict(
        status=status, error=error or None,
        tokens_in=r.get("tokens_in"), tokens_out=r.get("tokens_out"),
        cache_read_tokens=r.get("cache_read_tokens"), tool_calls=r.get("tool_calls"),
        phase_at_end=r.get("phase_at_end"), duration_ms=r.get("duration_ms"),
        model=r.get("model"), last_tool=r.get("last_tool"),
        progress_events=json.dumps(r.get("progress_events") or []))
    try:
        row = await db.get_chat(chat_id)
        if row is None:
            await db.finish_chat(chat_id, **fields)
            return
        artifact = dict(
            target=row.get("target") or row.get("project_slug") or "_orphan",
            chat_id=chat_id, user=row.get("user_prompt") or row.get("author") or "?",
            content=r.get("content") or "", source=row.get("source") or "grafo",
            author=row.get("author") or "", model=r.get("model") or "",
            status=status, duration_ms=r.get("duration_ms") or 0,
            error=error or None, events=r.get("progress_events") or [])
        if status == "cancelled" and r.get("messages_json"):
            # Historial del nodo, no del hilo compartido: conservarlo en
            # chat_outputs sin sobrescribir la conversación del grafo.
            artifact["messages_json"] = r["messages_json"]
        await finalization.finish(db, chat_id, artifact=artifact, **fields)
    except Exception:
        logger.exception("no pude cerrar el chat %s del nodo", chat_id)


def _prompt_de_tarea(grafo_row: dict, tarea: dict, todas: list) -> str:
    """Lo único que el nodo necesita saber. Corto a propósito.

    Lleva el resultado de sus dependencias porque son su insumo real: el
    nodo que carga datos necesita saber qué esquema dejó el que migró.
    NO lleva el resto del grafo — un nodo que ve doce tareas ajenas
    empieza a opinar sobre ellas en vez de hacer la suya.
    """
    por_id = {t["id"]: t for t in todas}
    partes = [
        f"Objetivo general del plan: {grafo_row.get('objetivo') or ''}",
        "",
        f"## Tu tarea: {tarea.get('titulo') or tarea['id']}",
    ]
    if (tarea.get("detalle") or "").strip():
        partes += ["", tarea["detalle"].strip()]
    previos = [por_id[d] for d in (tarea.get("deps") or []) if d in por_id]
    hechos = [t for t in previos if (t.get("resultado") or "").strip()]
    if hechos:
        partes += ["", "## Lo que dejaron las tareas de las que dependés"]
        for t in hechos:
            partes.append(f"- **{t.get('titulo') or t['id']}**: "
                          f"{(t['resultado'] or '')[:600]}")
    archivos = tarea.get("archivos")
    if archivos:
        import json as _json
        try:
            lista = _json.loads(archivos) if isinstance(archivos, str) else list(archivos)
        except (ValueError, TypeError):
            lista = []
        if lista:
            partes += ["", "## Archivos que declaraste para esta tarea",
                       ", ".join(f"`{a}`" for a in lista),
                       "",
                       "Otros archivos los podés LEER. Si necesitás escribir "
                       "uno que no está en esta lista y otra tarea lo tiene "
                       "tomado, el relay te lo va a rechazar: anotalo en tu "
                       "resultado en vez de insistir."]
    partes += [
        "",
        "Hacé SOLO esta tarea y terminá. No sigas con las que vienen "
        "después: las corre otro. Cerrá con un resumen de una o dos líneas "
        "de lo que quedó hecho — eso es lo que van a leer las tareas que "
        "dependen de vos.",
    ]
    return "\n".join(partes)


def coordinador_nemotron(proyecto: dict, *, modelo: str = ""):
    """Devuelve un `coordinar(tarea, resultado)` con el modelo que razona.

    Entra SOLO cuando un nodo falla. Lo que se le pide no es que arregle
    la tarea: es que decida **qué hacer con el fallo**, que es donde su
    criterio cambia el resultado y donde la regla mecánica se equivoca.
    Un 429 o un lock del filesystem se reintentan aunque el nodo escriba;
    un test que falla de verdad, no.
    """
    from . import config, experts

    spec = modelo or config.planner_model_spec()

    async def coordinar(tarea: dict, res: dict) -> Optional[str]:
        error = (res.get("error") or "")[:1200]
        prompt = (
            "Una tarea de un plan automático falló. Decidí qué hacer.\n\n"
            f"Tarea: {tarea.get('titulo') or tarea['id']}\n"
            f"¿Es idempotente (repetirla no duplica efectos)?: "
            f"{'sí' if tarea.get('idempotente') else 'no'}\n"
            f"Intento {tarea.get('intentos')} de {tarea.get('max_intentos')}\n"
            f"Error: {error}\n\n"
            "Respondé UNA sola palabra:\n"
            "- REINTENTAR si el error es transitorio (429, timeout de red, "
            "lock, un servicio que no estaba arriba).\n"
            "- FALLAR si el error es real y repetirlo va a dar lo mismo.\n"
            "- PREGUNTAR si hace falta que un humano decida."
        )
        try:
            agente = experts.Agent(experts.build_model(spec), instructions=(
                "Sos el coordinador de un plan. Contestá con UNA palabra."))
            import asyncio as _asyncio
            r = await _asyncio.wait_for(agente.run(prompt), timeout=45)
            texto = (getattr(r, "output", "") or "").strip().upper()
        except Exception as e:  # noqa: BLE001 — sin coordinador manda la regla
            logger.warning("el coordinador no contestó (%r): usa la regla", e)
            return None
        for palabra, decision in (("REINTENTAR", "reintentar"),
                                  ("FALLAR", "fallar"),
                                  ("PREGUNTAR", "preguntar")):
            if palabra in texto:
                # Un nodo NO idempotente no se reintenta ni aunque el
                # coordinador lo pida: el modelo no puede saber si el
                # efecto ya ocurrió, y esa es justo la regla que el
                # humano fijó. El coordinador puede ablandar un FALLAR a
                # PREGUNTAR, no al revés.
                if decision == "reintentar" and not tarea.get("idempotente"):
                    logger.info(
                        "el coordinador pidió reintentar %s pero no es "
                        "idempotente: pregunto", tarea.get("id"))
                    return "preguntar"
                return decision
        return None

    return coordinar


def verificador_del_grafo(project: dict, *, modelo: str = ""):
    """Devuelve el `verificar(...)` de cierre. Reusa `experts._run_verifier`.

    Es el MISMO turno que ya corre en los chats y el mismo vocabulario de
    veredictos (`complete` | `needs_more` | `needs_human` | `off_plan`):
    escribir un segundo verificador sería mantener dos criterios que
    tienen que decir lo mismo. Lo único propio de acá es el mapeo —el
    objetivo del grafo como pedido, los nodos como plan, el resumen de lo
    que produjeron como resultado del ejecutor— y el modelo, que sale de
    la misma cascada que el verificador de los chats.

    Devuelve un dict y no la 5-tupla de `_run_verifier` para que el
    orquestador no tenga que conocer su forma: este archivo es el único
    lugar del módulo que habla con `experts`.

    NO atrapa: si el verificador revienta, lo atrapa
    `_verificar_al_cerrar`, que es quien decide que eso no voltea el
    grafo.
    """
    from . import config, experts

    spec = (modelo or (project.get("defaults_json") or {}).get("verifier_model")
            or config.verifier_model_spec())

    async def verificar(*, user: str, plan: str, executor_result: dict) -> dict:
        ponytail = await experts.read_ponytail()
        verdict, feedback, usage, error, _pasos = await experts._run_verifier(
            user=user, plan=plan, executor_result=executor_result,
            model_spec=spec, ponytail=ponytail)
        return {"verdict": verdict, "feedback": feedback, "usage": usage,
                "error": error, "modelo": spec}

    return verificar
