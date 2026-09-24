"""Server domain handlers extracted from the composition entrypoint."""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Optional

from aiohttp import web

from .server_common import BG_TASKS_KEY, DB_KEY, GRAFOS_KEY, NOTIFY_KEY, PROGRESS_KEY, logger
from . import coordination
from . import experts
from . import grafo as grafo_mod
from . import logctx
from . import orquestador
# Estados terminales de un chat cuando el run cortó por timeout, presupuesto
# o porque el experto se fue por las ramas. Sin este set, un `phase_at_end`
# raro caía al default "pendiente" y el nodo se quedaba amarillo para
# siempre. Mantenido chico a propósito: agregar un valor acá debería ser
# decisión de producto, no de implementación.
# Una sola definición, compartida con el orquestador (`grafo`): tener
# dos listas fue el bug — el panel pintaba de rojo un turno que el grafo
# daba por `hecho`. Sumar `provider_error` acá además arregla lo suyo:
# un turno que murió en un 429 tiene `status='ok'`, así que sin la fase
# salía verde. Pasaron tres seguidos en code-hero-rpg el 31/8.
_PHASE_FALLIDO = grafo_mod.FASES_INCOMPLETAS
_STATUS_FALLIDO = frozenset({"error", "cancelled"})

# Cuántos nodos como máximo dibujamos en el grafo sintético. Una cadena
# de 40 turnos son 40 capas y el SVG se vuelve ilegible (medido el día
# que pusimos 25 en pantalla: el nodo final salía del viewport y nadie
# se enteraba de que el run había terminado). 12 es lo que entra en
# pantalla sin scroll y deja el primero arriba. Subirlo requiere decisión.
_CAP_NODOS_SINTETICOS = 12


def _estado_del_turno(fila: dict, tiene_pregunta_abierta: bool) -> str:
    """El estado real de un turno, sin heurísticas (Etapa A, P2).

    La función es pura para poder testearla: misma entrada → misma
    salida, y nada de "status" inferido a partir del campo contiguo.
    El orden de los chequeos ES el orden de prioridad:
    1. corriendo: el run está activo y todavía no cortó.
    2. esperando_humano: el run cortó con una pregunta abierta.
    3. fallado: el run cortó por un motivo terminal (timeout, budget,
       off-plan, error, cancelado). Aunque el status diga "ok", una
       fase terminal lo invalida — un run que cerró por budget_exceeded
       igual tiene que mostrarse en rojo.
    4. hecho: terminó ok en una fase normal.
    5. pendiente: cualquier otro caso (ej. status desconocido).
    """
    status = (fila.get("status") or "").strip()
    phase = fila.get("phase_at_end") or ""
    # La pregunta abierta gana sobre "running": el ejecutor cortó a
    # esperar, el status todavía no se actualizó y mentir con "corriendo"
    # confundiría al humano que la contestó.
    if tiene_pregunta_abierta:
        return "esperando_humano"
    if status == "running":
        return "corriendo"
    if status in _STATUS_FALLIDO or phase in _PHASE_FALLIDO:
        return "fallado"
    if status == "ok":
        return "hecho"
    return "pendiente"


def _estado_del_paso(
    idx_paso: int,
    *,
    marcados: set,
    turno_corriendo: bool,
    turno_pregunta_abierta: bool,
    ultimo_paso_corriendo: Optional[int],
) -> str:
    """Estado de un paso individual (Etapa B, P7).

    Pura, testeable:
    - marcado por el ejecutor o por el verificador → "hecho"
    - el primer paso sin marcar de un turno que sigue corriendo →
      "corriendo" (un solo nodo corriendo a la vez; coherente con
      cómo el humano lee la cadena)
    - los posteriores → "pendiente"
    - si el turno terminó y quedaron pasos sin marcar → "pendiente"
      (NUNCA "fallado": que no se haya marcado no prueba que no se
      haya hecho, y pintarlo en rojo sería la mentira que este diseño
      evita).
    """
    if idx_paso in marcados:
        return "hecho"
    if turno_pregunta_abierta:
        # El ejecutor paró a esperar; los pasos sin marcar no
        # pueden contar como "hecho" ni como "corriendo".
        return "pendiente"
    if turno_corriendo and idx_paso == ultimo_paso_corriendo:
        return "corriendo"
    return "pendiente"


def _stages_de(fila: dict) -> dict:
    """`chats.stages_json` parseado a dict (Etapa B). Vacío si está mal."""
    import json
    raw = fila.get("stages_json") or ""
    try:
        etapas = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        etapas = {}
    if not isinstance(etapas, dict):
        return {}
    return etapas


def _detalle_del_turno(fila: dict) -> str:
    """El texto que se ve al hacer hover sobre el nodo de un turno.

    Sale de los datos que ya tenemos guardados en `chats.stages_json`:
    si el planificador corrió, los pasos numerados; si no, la respuesta
    del experto. El veredicto del verificador se agrega al final para
    que se lea de una sola mirada.
    """
    import json
    etapas = _stages_de(fila)
    pasos = experts.pasos_del_plan(etapas.get("plan") or "")
    veredicto = (etapas.get("verifier_verdict") or "").strip()
    if pasos:
        # Numeramos para que el LLM y el humano vean la misma referencia
        # que `pasos_del_plan` extrajo del system prompt.
        cuerpo = "\n".join(f"{i+1}. {p}" for i, p in enumerate(pasos))
    else:
        # `last_response` no es una columna de `chats` (la respuesta vive
        # en el .md): sin pasos, lo más cercano que tenemos al contenido
        # del turno es el pedido que lo abrió.
        cuerpo = (fila.get("last_response")
                  or fila.get("user_prompt") or "").strip()
        if len(cuerpo) > 400:
            cuerpo = cuerpo[:400].rstrip() + "…"
    return f"{cuerpo}\n\nVerificador: {veredicto or '—'}" if veredicto \
        else cuerpo


def _titulo_de_respaldo(fila: dict) -> str:
    """Cómo llamar a un turno del que no guardamos el pedido.

    Son las filas anteriores a la migración del 30/8 (`user_prompt`).
    La hora no dice de qué se trata, pero un nodo con etiqueta se puede
    señalar y abrir; uno en blanco no se distingue del de al lado, que
    es como se veía el panel entero hasta hoy.
    """
    t = (fila.get("started_at") or "").strip()
    return f"turno de {t[11:16]}" if len(t) >= 16 else "turno"


def _costo_en_nodos(fila: dict, expandir_pasos: bool) -> int:
    """Cuántos nodos va a rendir este turno. Espeja la rama de abajo.

    Se calcula aparte del armado porque el cap tiene que decidir a qué
    turnos entra ANTES de construirlos. Si las dos condiciones se
    separan, el cap cuenta una cosa y el dibujo hace otra — y el panel
    vuelve a pasarse de largo sin que nadie lo note.
    """
    if not expandir_pasos:
        return 1
    etapas = _stages_de(fila)
    pasos = experts.pasos_del_plan(etapas.get("plan") or "")
    marcados = {k for k in (etapas.get("plan_steps_done") or {}).keys()
                if str(k).isdigit() and int(k) >= 1}
    return len(pasos) if (pasos and marcados) else 1


def _grafo_sintetico(
    chats: list[dict],
    *,
    corriendo: bool = False,
    preguntas_por_chat: Optional[dict[str, bool]] = None,
    expandir_pasos: bool = False,
) -> dict:
    """Un nodo por turno de la conversación, encadenado (Etapa A, P1).

    Existe porque el panel del plan tiene que poder mostrar ALGO en
    cualquier conversación, no solo en las que el disparador partió en
    tareas (Etapa A). Sale de los datos que ya están en `chats`, sin
    escribir en la DB ni inventar tablas.

    Modos:
      - Default (`expandir_pasos=False`): un nodo por turno con el
        detalle de los pasos numerado adentro (Etapa A). Es el modo
        que el panel dibuja hoy.
      - `expandir_pasos=True`: un nodo por paso del plan, encadenado,
        con el estado que sale de `plan_steps_done` (Etapa B, P6).
        Requiere que el ejecutor haya usado `plan_step_done` para que
        `plan_steps_done` no venga vacío — si nadie marcó, este modo
        colapsa al mismo resultado que el default (no expande pasos
        sin marcadores, para no mostrar "pendiente" en todo).

    El cap lo aplicamos acá y no en la UI: si lo hiciéramos del lado del
    JS, el server mandaría el set entero y gastaríamos ancho de banda
    por turnos que ya no se dibujan.

    `preguntas_por_chat` se puede pasar para testear la función pura sin
    DB: si no se pasa, se calcula por chat con `_tiene_pregunta_abierta`.
    """
    from . import grafo as graf
    # Los nodos de un grafo real corren como chats del MISMO hilo
    # (`source='grafo'`). Encadenarlos acá los mostraría como turnos del
    # humano, en fila y en un orden que no tuvieron —el grafo los corrió
    # en paralelo— y de paso empujarían fuera del cap a los turnos que sí
    # escribió una persona. El grafo ya tiene su propia vista.
    chats = [c for c in chats if (c.get("source") or "") != "grafo"]
    total_turnos = len(chats)
    # El cap es sobre NODOS y no sobre turnos. Con `expandir_pasos` un
    # turno rinde tantos nodos como pasos tenga su plan, así que 12
    # turnos daban 43 nodos en un panel lateral (medido el 30/8 en
    # `transformadorplanos`): ilegible, y el turno más viejo se dibujaba
    # con el mismo peso que el que está corriendo. Recortamos por la
    # cola —los turnos nuevos son los que importan— y nunca a mitad de
    # un turno: los pasos se leen en orden dentro del turno y cortar
    # entre el 2 y el 3 deja algo que no refleja nada.
    elegidos: list[dict] = []
    presupuesto = _CAP_NODOS_SINTETICOS
    for c in reversed(chats):                    # del más nuevo al más viejo
        costo = _costo_en_nodos(c, expandir_pasos)
        if elegidos and costo > presupuesto:
            break
        presupuesto -= costo
        elegidos.append(c)
    elegidos.reverse()
    if len(elegidos) < total_turnos:
        objetivo = f"últimos {len(elegidos)} de {total_turnos} turnos"
    else:
        # La tabla `chats` no tiene columna `objetivo` (la tiene el
        # grafo real); usamos el prompt del último turno como título.
        objetivo = (elegidos[-1].get("user_prompt") or "").strip() \
            if elegidos else ""
        if not objetivo:
            # Sin prompt guardado (filas anteriores a la migración del
            # 30/8) decir "sin turnos todavía" sobre un hilo con turnos
            # es la mentira que el panel no puede darse el lujo de
            # contar: preferimos contarlos.
            objetivo = (f"{total_turnos} turno(s) en este hilo"
                        if total_turnos else "sin turnos todavía")
        if len(objetivo) > 80:
            objetivo = objetivo[:80].rstrip() + "…"
    chats = elegidos

    # `chat_id` sirve de id de nodo: es único, es lo que la UI ya conoce,
    # y nos ahorra inventar una capa de mapping.
    # La pregunta abierta la consultamos una sola vez por turno para no
    # pegarle a la DB N veces; `_estado_del_turno` la recibe como bool.
    # (ponytail: lookup O(N) sobre `expert_questions`; si el cuello se
    # vuelve visible, mover a una sola query agregada por turno.)
    if preguntas_por_chat is None:
        preguntas_abiertas: dict[str, bool] = {}
        for c in chats:
            cid = c.get("id") or ""
            if not cid:
                continue
            preguntas_abiertas[cid] = _tiene_pregunta_abierta(c)
    else:
        preguntas_abiertas = preguntas_por_chat

    nodos = []
    ids: list[str] = []
    id_anterior: Optional[str] = None
    for c in chats:
        cid = c.get("id") or ""
        if not cid:
            continue
        prompt = (c.get("user_prompt") or c.get("prompt") or "").strip()
        pregunta_abierta = preguntas_abiertas.get(cid, False)
        etapas = _stages_de(c)
        pasos = experts.pasos_del_plan(etapas.get("plan") or "")
        pasos_marcados = {
            int(k) for k in (etapas.get("plan_steps_done") or {}).keys()
            if str(k).isdigit() and int(k) >= 1
        }
        estado_turno = _estado_del_turno(c, pregunta_abierta)
        turno_corriendo = estado_turno == graf.CORRIENDO
        ultimo_paso_corriendo: Optional[int] = None
        if turno_corriendo and pasos:
            # El primer paso sin marcar es el "corriendo". Si todos
            # están marcados, ninguno está corriendo.
            for idx in range(1, len(pasos) + 1):
                if idx not in pasos_marcados:
                    ultimo_paso_corriendo = idx
                    break
        if pasos and expandir_pasos and pasos_marcados:
            # Un nodo por paso: el id es `<chat_id>#<paso>` para que
            # la UI los distinga del nodo-de-turno viejo y los pueda
            # mergear con los grafos reales que vienen de la DB.
            #
            # (ponytail: entrar solo si `pasos_marcados` no es vacío
            # evita el caso "todos pendientes" cuando nadie usó la
            # tool — sería el peor resultado posible para el panel,
            # peor que el modo A. El switch está acá y no en la UI
            # porque el costo de la decisión es uno por turno.)
            for idx, paso in enumerate(pasos, start=1):
                pid = f"{cid}.{idx}"
                titulo = f"Paso {idx}: {paso[:55]}"
                if len(paso) > 55:
                    titulo = f"Paso {idx}: {paso[:54].rstrip()}…"
                estado = _estado_del_paso(
                    idx_paso=idx,
                    marcados=pasos_marcados,
                    turno_corriendo=turno_corriendo,
                    turno_pregunta_abierta=pregunta_abierta,
                    ultimo_paso_corriendo=ultimo_paso_corriendo,
                )
                nodos.append({
                    "id": pid,
                    "titulo": titulo,
                    "detalle": paso,
                    "estado": estado,
                    "deps": [id_anterior] if id_anterior else [],
                    "idempotente": True,
                    "intentos": 0,
                    "max_intentos": 1,
                    "orden": len(nodos),
                    "chat_id": cid,
                    "resultado": "",
                    "error": "",
                    "started_at": c.get("started_at"),
                    "ended_at": c.get("finished_at") or c.get("ended_at"),
                })
                ids.append(pid)
                id_anterior = pid
        else:
            # Turno sin pasos parseables: un nodo por turno (modo viejo
            # de la Etapa A). Sigue siendo útil para hilos donde el
            # planificador no se disparó (chico o trivial).
            titulo = (prompt[:60] + ("…" if len(prompt) > 60 else "")
                      or _titulo_de_respaldo(c))
            nodos.append({
                "id": cid,
                "titulo": titulo,
                "detalle": _detalle_del_turno(c),
                "estado": estado_turno,
                "deps": [id_anterior] if id_anterior else [],
                "idempotente": True,
                "intentos": 0,
                "max_intentos": 1,
                "orden": len(nodos),
                "chat_id": cid,
                "resultado": "",
                "error": "",
                "started_at": c.get("started_at"),
                "ended_at": c.get("finished_at") or c.get("ended_at"),
            })
            ids.append(cid)
            id_anterior = cid

    # `capas()` y `progreso()` esperan `list[Nodo]` (con atributos), no
    # dicts: convertimos acá para reusar exactamente la misma lógica que
    # `_grafo_publico`. Si la lista queda vacía, `capas` tira
    # `GrafoInvalido` — y lo cazamos porque la UI tiene que poder
    # mostrar un grafo vacío sin romperse.
    nodos_grafo = [graf.Nodo.desde_fila(t, t["deps"]) for t in nodos]
    if nodos_grafo:
        # `capas()` tira `GrafoInvalido` con lista vacía: por eso el
        # `if`. Dentro del try van las dos llamadas para no repetir el
        # guard con `progreso()` — esa no tira.
        try:
            capas = graf.capas(nodos_grafo)
        except graf.GrafoInvalido:
            capas = []
        progreso = graf.progreso(nodos_grafo)
    else:
        capas = []
        progreso = {
            "total": 0, "hechos": 0, "corriendo": 0, "pendientes": 0,
            "bloqueados": 0, "fallados": 0, "esperando_humano": 0,
            "porcentaje": 0, "estado": "hecho",
        }
    corriendo = (any(n["estado"] == graf.CORRIENDO for n in nodos)
                 or corriendo)
    return {
        "id": "",
        "objetivo": objetivo,
        "estado": "activo" if corriendo else "hecho",
        "corriendo": corriendo,
        "progreso": progreso,
        "orden": ids,
        "capas": capas,
        "tasks": nodos,
        "sintetico": True,
    }


def _tiene_pregunta_abierta(chat: dict) -> bool:
    """¿Este chat tiene una expert_question `open`? (Etapa A, P1).

    **No lo uses desde el endpoint**: `chats` no tiene columna
    `question_id`, así que esto devuelve siempre False y el estado
    `esperando_humano` no se dibujaba nunca (2026-08-30). Quien sabe la
    respuesta es `expert_questions`, y `conversation_plan` la consulta
    de una y pasa el resultado por `preguntas_por_chat`. Esto queda como
    default para los llamadores que arman la fila a mano (los tests).
    """
    qid = (chat.get("question_id") or "").strip()
    return bool(qid)


def _verificacion_publica(g: dict) -> dict:
    """`task_graphs.verificacion_json` parseado, o `{}`.

    El veredicto de la verificación de cierre del grafo (ver
    `orquestador._verificar_al_cerrar`). Sale en el MISMO payload que el
    panel ya consume para pintar el grafo y no en un endpoint aparte,
    porque una perspectiva que habla y no llega a ninguna pantalla es
    peor que no tenerla: de 68 veredictos `off_plan` de los chats, 33 no
    se vieron nunca. `{}` = todavía no se verificó (o el grafo se
    canceló, que no se verifica a propósito).
    """
    raw = g.get("verificacion_json") or ""
    try:
        v = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        v = {}
    return v if isinstance(v, dict) else {}


def _grafo_publico(g: dict) -> dict:
    """El grafo como lo quiere la UI: nodos + progreso ya calculado.

    Suma dos campos de PRESENTACIÓN además de lo que ya había (6/9/26,
    ver `grafo.estado_visible` y `grafo.es_error_de_presupuesto` para el
    porqué): `estado_visible` en la raíz distingue "esperando a un
    humano" de "activo" de verdad sin tocar `estado` (que sigue siendo
    el valor crudo de `task_graphs.estado` — de eso depende el
    relanzamiento) y `presupuesto_agotado` en cada tarea distingue un
    corte por presupuesto de un fallo común. Único lugar donde se
    calculan: el endpoint y el panel leen esto, no reinventan el
    criterio cada uno por su lado.
    """
    nodos = [grafo_mod.Nodo.desde_fila(t, t["deps"]) for t in g["tasks"]]
    sustituidos = grafo_mod.sustituidos(nodos)
    # Un grafo sin tareas no se puede ordenar ni dibujar, y `capas`
    # levanta `GrafoInvalido` — que salía como 500 y dejaba el panel del
    # chat sin poder mostrar NADA del hilo. No debería existir (ver el
    # guard de `create_task_graph`), pero los que quedaron de antes del
    # fix del 24/8 siguen en la base: se devuelven vacíos y visibles, que
    # es lo que deja verlos para poder cancelarlos.
    if not nodos:
        return {
            "id": g["id"], "objetivo": g["objetivo"], "estado": g["estado"],
            "estado_visible": g["estado"],
            "conversation_id": g.get("conversation_id"),
            "project_slug": g.get("project_slug"),
            "created_at": g.get("created_at"), "updated_at": g.get("updated_at"),
            "progreso": {"total": 0, "hechos": 0, "corriendo": 0,
                         "pendientes": 0, "bloqueados": 0, "fallados": 0,
                         "esperando_humano": 0, "porcentaje": 0,
                         "estado": g["estado"]},
            "verificacion": _verificacion_publica(g),
            "orden": [], "capas": [], "tasks": [],
        }
    return {
        "id": g["id"], "objetivo": g["objetivo"], "estado": g["estado"],
        "estado_visible": grafo_mod.estado_visible(nodos, g["estado"]),
        "verificacion": _verificacion_publica(g),
        "conversation_id": g.get("conversation_id"),
        "project_slug": g.get("project_slug"),
        "created_at": g.get("created_at"), "updated_at": g.get("updated_at"),
        "progreso": grafo_mod.progreso(nodos),
        "orden": grafo_mod.orden_topologico(nodos),
        # El grafo en filas: cada capa es lo que puede correr a la vez.
        # Va calculado desde acá y no en el JS por lo mismo que el orden
        # topológico — es lógica de grafo, y se prueba en Python.
        "capas": grafo_mod.capas(nodos),
        "tasks": [{
            "id": t["id"], "titulo": t["titulo"], "detalle": t["detalle"],
            "estado": t["estado"], "deps": t["deps"],
            "parent_id": t.get("parent_id"), "sustituido": t["id"] in sustituidos,
            "idempotente": bool(t["idempotente"]),
            "intentos": t["intentos"], "max_intentos": t["max_intentos"],
            "orden": t["orden"], "chat_id": t.get("chat_id") or "",
            "resultado": t.get("resultado") or "", "error": t.get("error") or "",
            "presupuesto_agotado": grafo_mod.es_error_de_presupuesto(
                t.get("error") or ""),
            "started_at": t.get("started_at"), "ended_at": t.get("ended_at"),
        } for t in sorted(g["tasks"], key=lambda x: (x["orden"], x["id"]))],
    }


async def _correr_grafo_bg(app: web.Application, project: dict,
                           graph_id: str) -> None:
    """Corre el grafo entero fuera del request. Nunca lanza."""
    from . import orquestador

    db = app[DB_KEY]
    logctx.bind(graph_id, project.get("slug") or "")
    # Cada nodo reporta como un run del chat (2026-08-24). Sin esto los
    # nodos del grafo no reportaban a NADIE: `/experts/status/{chat_id}`
    # contestaba "no hay run con ese id" y el panel no podía decir en qué
    # herramienta estaba. Medido el 24/8: veinte minutos de un nodo
    # trabajando —escribiendo PNGs— sin una sola señal en pantalla, y lo
    # dimos por colgado.
    #
    # Es una factory porque `make_progress_callback` registra el
    # RunProgress bajo un `chat_id` y el chat de cada nodo se crea dentro
    # del ejecutor. Ver `orquestador.ejecutor_minimax`.
    experts.make_progress_callback(
        store=app[PROGRESS_KEY], notify=None, chat_id=graph_id,
        target=project.get("slug") or "", model="")
    graph_progress = app[PROGRESS_KEY][graph_id]
    graph_progress.phase = "graph"
    graph_progress.graph_id = graph_id

    def progreso_de(chat_id: str):
        # Esta factory corre dentro de la task del nodo, no del padre.
        logctx.bind(chat_id, project.get("slug") or "")
        callback = experts.make_progress_callback(
            store=app[PROGRESS_KEY], notify=app[NOTIFY_KEY],
            chat_id=chat_id, target=project.get("slug") or "",
            model=(project.get("defaults_json") or {}).get("model") or "")
        rp = app[PROGRESS_KEY][chat_id]
        rp.graph_id = graph_id
        task = asyncio.current_task()
        if task is not None:
            def terminado(_task):
                rp.finished = True
                graph_progress.last_activity_at = time.monotonic()

            task.add_done_callback(terminado)
        return callback

    try:
        prog = await orquestador.lanzar(db, project, graph_id,
                                        progreso_de=progreso_de)
        logger.info("grafo %s terminó: %s", graph_id, prog.get("estado"))
    except asyncio.CancelledError:
        logger.info("grafo %s cancelado", graph_id)
        raise
    except Exception:  # noqa: BLE001 — un grafo roto no voltea el relay
        logger.exception("el grafo %s se cayó", graph_id)
        with contextlib.suppress(Exception):
            await db.set_task_graph_state(graph_id, "fallado")
    finally:
        graph_progress.finished = True


def _largar_grafo(app: web.Application, project: dict, graph_id: str,
                  actor_email: str | None = None) -> None:
    from . import user_accounts
    grafos: dict = app[GRAFOS_KEY]
    current = user_accounts.current_actor.get()
    actor = actor_email or (current[1] if current else None)
    async def in_workspace():
        from . import task_service, task_workspace
        db = app[DB_KEY]
        graph = await db.get_task_graph(graph_id)
        effective = project
        if graph and graph.get("conversation_id"):
            task_state = await db.get_conversation_task(graph["conversation_id"])
            if task_state.get("state") in task_service.STOPPED:
                raise RuntimeError("La tarea está detenida; continúa desde sus controles")
            effective = await task_workspace.resolved_project(db, project, graph["conversation_id"])
        with user_accounts.bind_actor(db, actor):
            return await coordination.spawn_workspace(
                db, effective, _correr_grafo_bg(app, effective, graph_id))
    task = asyncio.create_task(in_workspace())
    task.relay_actor = actor
    coordination.hold_current(task)
    grafos[graph_id] = task
    bg_tasks: set = app[BG_TASKS_KEY]
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)
    task.add_done_callback(lambda _t: grafos.pop(graph_id, None))


# Proyectos con el planificador corriendo ahora mismo. La guarda de
# `graphs_create` mira `task_graphs`, y esa fila recién existe cuando el
# planificador vuelve (1-2 min): durante todo ese rato el proyecto queda
# sin reservar y un segundo POST pasa limpio. Dos pedidos sobre el
# mismo proyecto y archivo pueden solaparse si un cliente corta por
# timeout y reintentó. En memoria alcanza porque el relay es UN proceso;
# si algún día son varios, esto tiene que ser una fila con TTL como las
# reservas de archivo.
# ponytail: set en memoria, no reserva persistida. Upgrade cuando el
# relay corra en más de un proceso.
_PLANIFICANDO: set[str] = set()
