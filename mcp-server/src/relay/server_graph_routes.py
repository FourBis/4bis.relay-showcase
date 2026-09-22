"""Server domain handlers extracted from the composition entrypoint."""
from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from typing import Optional

from aiohttp import web

from .server_common import DB_KEY, GRAFOS_KEY, _require_auth, logger
from . import coordination
from . import orquestador
from . import planificador
from .server_graph_helpers import (
    _PLANIFICANDO, _grafo_publico, _grafo_sintetico, _largar_grafo,
    _stages_de,
)
@_require_auth
@coordination.guard_workspace(DB_KEY)
async def graphs_create(request: web.Request) -> web.Response:
    """POST /graphs  {project, objetivo, conversation_id?, arrancar?}

    Arma el grafo con el razonador y —salvo que pidas `arrancar: false`—
    lo larga. Devuelve 202 con el grafo entero: el humano ve QUÉ se va a
    hacer en la misma respuesta en que arrancó, que es la diferencia
    entre un plan y una caja negra.
    """
    from . import planificador

    db = request.app[DB_KEY]
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)

    slug = (body.get("project") or body.get("target") or "").strip()
    objetivo = (body.get("objetivo") or body.get("user") or "").strip()
    if not slug or not objetivo:
        return web.json_response(
            {"error": "mandá `project` y `objetivo`"}, status=400)
    project = await db.get_project(slug)
    if not project:
        return web.json_response({"error": f"no existe el proyecto {slug}"},
                                 status=404)

    conv_id = (body.get("conversation_id") or "").strip()
    from . import git_flow, identity, task_service, task_workspace
    if not conv_id and await git_flow.is_git_repo(project.get("repo_path") or ""):
        request_id = body.get("request_id")
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
            return web.json_response({"error": "request_id inválido"}, status=400)
        conv_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"relay:graph:{slug}:{request_id}")) if request_id else str(uuid.uuid4())
        if not await db.get_conversation(conv_id):
            await db.create_conversation(project_slug=slug, conversation_id=conv_id,
                                         requested_by=identity.requester(request))
            await db.update_conversation_task(conv_id, role=identity.role_of(request),
                requested_by=identity.requester(request), publish_allowed=False, tracking={"enabled": False})
            try:
                await task_workspace.initialize_task(db, project, conv_id,
                    read_only=identity.role_of(request) != "owner" or bool((project.get("defaults_json") or {}).get("read_only")))
            except (RuntimeError, OSError) as exc:
                return web.json_response({"error": str(exc), "conversation_id": conv_id}, status=422)
    if conv_id:
        conv = await db.get_conversation(conv_id)
        if not conv or conv["project_slug"] != slug:
            return web.json_response({"error": "La conversación no pertenece al proyecto"}, status=400)
        task = await db.get_conversation_task(conv_id)
        if task.get("state") in task_service.STOPPED:
            return web.json_response({"error": "Continúa la tarea desde sus controles"}, status=409)
        if task.get("mode") == "write" and identity.role_of(request) != "owner":
            return web.json_response({"error": "Esta tarea de escritura requiere owner"}, status=403)
        try:
            project = await task_workspace.resolved_project(db, project, conv_id)
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=409)
    # Dos grafos sobre el mismo repo se pisarían los archivos sin que
    # ninguno se entere (las reservas los frenarían de a uno, y el
    # humano vería dos planes avanzando a los tumbos). El chequeo por
    # proyecto se hace SIEMPRE, incluso sin `conversation_id`: dos POST
    # sin conv_id al mismo proyecto pasaban la guarda anterior y hoy
    # arrancaron dos grafos sobre el mismo repositorio.
    # `active_task_graph_by_project` devuelve el grafo ENTERO, no el id.
    if not project.get("_task_id") and (previo := await db.active_task_graph_by_project(slug)):
        return web.json_response(
            {"error": "este proyecto ya tiene un grafo corriendo",
             "graph_id": previo["id"], "message":
                 "Esperá a que termine o cancelalo con "
                 f"POST /graphs/{previo['id']}/cancel."},
            status=409)
    # La guarda por conversación se mantiene ADEMÁS: cubre el caso
    # histórico — un hilo con grafo vivo sigue bloqueado, como antes.
    if conv_id and (previo := await db.active_task_graph(conv_id)):
        return web.json_response(
            {"error": "esta conversación ya tiene un grafo corriendo",
             "graph_id": previo["id"], "message":
                 "Esperá a que termine o cancelalo con "
                 f"POST /graphs/{previo['id']}/cancel."},
            status=409)

    planning_key = conv_id or slug
    if planning_key in _PLANIFICANDO:
        return web.json_response(
            {"error": "este proyecto ya está armando un grafo",
             "message": "Esperá a que el planificador termine y mirá "
                        "el grafo que salga de ahí."},
            status=409)
    _PLANIFICANDO.add(planning_key)
    try:
        g = await planificador.armar_grafo(
            project, objetivo, db=db, conversation_id=conv_id,
            contexto=(body.get("contexto") or "").strip())
    except RuntimeError as e:
        # El planificador no pudo. Es un 502 y no un 500: el relay
        # funciona, el que no contestó algo usable fue el modelo.
        return web.json_response({"error": str(e)}, status=502)
    finally:
        # Sale sí o sí: si el planificador rompe y no soltamos el slug,
        # el proyecto queda trabado hasta reiniciar el relay.
        _PLANIFICANDO.discard(planning_key)

    if body.get("arrancar", True):
        _largar_grafo(request.app, project, g["id"])
    return web.json_response(_grafo_publico(g), status=202)


_ETAPA = {
    "planner": "planificando", "verifier": "verificando",
    "documenter": "documentando", "question": "te está preguntando",
    "thinking": "ejecutando", "writing": "ejecutando",
    "tool_call": "ejecutando", "say": "ejecutando",
    "heartbeat": "ejecutando", "steer": "ejecutando",
}


@_require_auth
async def conversation_plan(request: web.Request) -> web.Response:
    """GET /conversations/{id}/plan — lo que el panel necesita, en un pedido.

    **Un pedido y no tres.** El panel tiene que poder responder "¿en qué
    va esto?" sin encadenar `/graphs` → `/chats` → `/experts/status`, y
    sobre todo sin que el navegador decida cuál de las tres respuestas
    manda. La decisión de qué mostrar es del servidor.

    Desde la Etapa A, este endpoint devuelve **siempre** un grafo:

    - Si la conversación tiene un grafo real (activo o viejo), sale por
      `_grafo_publico` sin la clave `sintetico`.
    - Si no, sale por `_grafo_sintetico`: un nodo por turno, encadenado,
      con `sintetico: true`. El panel no distingue entre los dos:
      `pintar()` lee la misma forma en los dos casos.

    Lo que este endpoint **no** dice, a propósito: en qué PASO del plan
    va el ejecutor. Nadie lleva ese puntero — el plan es prosa y el
    ejecutor no reporta contra él. Inventarlo sería la misma clase de
    mentira que la barra de progreso que contaba las falladas como
    avance. La Etapa B ataca eso con `plan_step_done` y la red del
    verificador.
    """
    db = request.app[DB_KEY]
    conv_id = request.match_info["id"]
    if await db.get_conversation(conv_id) is None:
        return web.json_response({"error": "not found"}, status=404)

    def _grafo(g: dict, corriendo: bool) -> web.Response:
        salida = _grafo_publico(g)
        salida["corriendo"] = corriendo
        return web.json_response({"modo": "grafo", "grafo": salida})

    # El grafo del hilo —el vivo si hay, si no el último— gana **mientras
    # siga siendo lo que está pasando**.
    #
    # Hasta el 30/8 ganaba siempre, y el costo era exactamente lo que el
    # panel existe para evitar: un hilo que terminó su plan y siguió
    # trabajando mostraba el plan viejo al 100% —badge "hecho", barra
    # llena, sin aviso— mientras un run nuevo corría abajo. Nueve
    # conversaciones así en la base, una con diez turnos invisibles.
    # Terminado y con trabajo posterior, el grafo dejó de ser el estado
    # del hilo y pasó a ser su historia; el panel muestra el estado.
    #
    # `vivo` (¿lo está corriendo ESTE proceso?) y no `estado='activo'`:
    # un grafo que quedó `activo` sin nadie ejecutándolo —cortado, o con
    # nodos esperando una respuesta que ya se dio— si no seguiría
    # tapando el hilo para siempre. Hay uno así en la base desde el 24/8.
    g = (await db.active_task_graph(conv_id)
         or await db.last_task_graph(conv_id))
    if g is not None:
        vivo = g["id"] in request.app[GRAFOS_KEY]
        if vivo or not await db.hay_turnos_humanos_despues(
                conv_id, g.get("updated_at") or g.get("created_at") or ""):
            return _grafo(g, vivo)

    # Sin grafo real (o con uno ya superado): un nodo por turno,
    # encadenado (Etapa A, P1). Si hay un run "running" en el hilo,
    # marcamos el grafo entero como `corriendo` para que el panel no se
    # duerma.
    # Pedimos más del cap para que el `_grafo_sintetico` haga su propio
    # cap (12 nodos, no los 10 que viene por default). El cap se hace
    # acá y no en el cliente para que el `objetivo` pueda decir
    # "últimos N de M" cuando recortamos.
    chats = await db.list_chats_of_conversation(conv_id, limit=200)
    # La lista viene DESC (más nuevo primero) para el `corriendo` de arriba;
    # pero el grafo se dibuja de izquierda (inicio) a derecha (fin), así que
    # lo invertimos a ASC antes de armar los nodos. Sin esto, un chat largo
    # muestra el último turno a la izquierda como si fuera el primero y la
    # UI "no se completa" desde la mirada del humano (2026-08-28).
    chats_para_grafo = list(reversed(chats))
    corriendo = any((c.get("status") or "") == "running" for c in chats)
    # Etapa B, P7: si ALGÚN turno del hilo tiene `plan_steps_done`,
    # expandimos a un nodo por paso. Sin esto, un chat largo con muchos
    # pasos marcados por el ejecutor colapsa al modo A (un nodo por
    # turno) y el panel nunca ve el progreso fino. El propio
    # `_grafo_sintetico` desactiva el modo si nadie marcó, así que
    # pasa por default sin costo.
    hay_markers = any(
        _stages_de(c).get("plan_steps_done") for c in chats)
    # `_tiene_pregunta_abierta` resuelve por un campo (`question_id`) que
    # la tabla `chats` no tiene, así que siempre daba False y el estado
    # `esperando_humano` no se dibujaba nunca. Una query al hilo entero
    # lo contesta de verdad y cuesta lo mismo que no contestarlo.
    abiertas = await db.list_expert_questions(
        conversation_id=conv_id, only_open=True, limit=50)
    con_pregunta = {(q.get("chat_id") or "") for q in abiertas}
    salida = _grafo_sintetico(
        chats_para_grafo, corriendo=corriendo, expandir_pasos=hay_markers,
        preguntas_por_chat={(c.get("id") or ""): (c.get("id") or "")
                            in con_pregunta for c in chats_para_grafo})
    return web.json_response({"modo": "grafo", "grafo": salida})



@_require_auth
async def graphs_get(request: web.Request) -> web.Response:
    """GET /graphs/{id} — el grafo con su progreso. Lo lee el panel."""
    db = request.app[DB_KEY]
    g = await db.get_task_graph(request.match_info["id"])
    if g is None:
        return web.json_response({"error": "not found"}, status=404)
    salida = _grafo_publico(g)
    salida["corriendo"] = request.match_info["id"] in request.app[GRAFOS_KEY]
    return web.json_response(salida)


@_require_auth
async def graphs_list(request: web.Request) -> web.Response:
    """GET /graphs?conversation=<id> — el grafo activo de un hilo."""
    db = request.app[DB_KEY]
    conv = (request.query.get("conversation") or "").strip()
    if not conv:
        return web.json_response({"error": "mandá `conversation`"}, status=400)
    g = await db.active_task_graph(conv)
    if g is None:
        return web.json_response({"graph": None})
    return web.json_response({"graph": _grafo_publico(g)})


@_require_auth
@coordination.guard_workspace(DB_KEY, source="graph")
async def graphs_resume(request: web.Request) -> web.Response:
    """POST /graphs/{id}/resume — retoma un grafo cortado.

    `correr_grafo` sana al arrancar (nodos que quedaron `corriendo`,
    reservas huérfanas), así que acá no hay nada especial que hacer más
    que volver a largarlo. Lo que sí hace falta es no largar dos: un
    grafo ya corriendo devuelve 409 en vez de duplicar los nodos.
    """
    db = request.app[DB_KEY]
    graph_id = request.match_info["id"]
    g = await db.get_task_graph(graph_id)
    if g is None:
        return web.json_response({"error": "not found"}, status=404)
    if graph_id in request.app[GRAFOS_KEY]:
        return web.json_response(
            {"error": "ese grafo ya está corriendo", "graph_id": graph_id},
            status=409)
    project = await db.get_project(g.get("project_slug") or "")
    if not project:
        return web.json_response(
            {"error": f"el proyecto {g.get('project_slug')!r} ya no existe"},
            status=409)
    # Un grafo cancelado o fallado vuelve a `activo`: retomarlo es
    # justamente decir "esto sigue". Si no, `estado_del_grafo` lo dejaría
    # como estaba y el panel mostraría un plan muerto avanzando.
    if g["estado"] != "activo":
        await db.set_task_graph_state(graph_id, "activo")
    _largar_grafo(request.app, project, graph_id)
    return web.json_response(
        _grafo_publico(await db.get_task_graph(graph_id)), status=202)


@_require_auth
async def graphs_cancel(request: web.Request) -> web.Response:
    """POST /graphs/{id}/cancel — corta el grafo y sus nodos en vuelo.

    Cancelar la task de fondo alcanza: el `finally` de `correr_grafo`
    corta los nodos vivos, les suelta los archivos y les aplica la regla
    (ver `_cerrar_las_que_quedaron`). Sin eso, cancelar dejaría runs
    escribiendo archivos que ya nadie mira.
    """
    db = request.app[DB_KEY]
    graph_id = request.match_info["id"]
    g = await db.get_task_graph(graph_id)
    if g is None:
        return web.json_response({"error": "not found"}, status=404)
    task = request.app[GRAFOS_KEY].get(graph_id)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    # Igual que en /experts/cancel: si el proceso se reinició y el grafo
    # quedó `activo` en la base sin nadie corriéndolo, marcarlo cancelado
    # lo mismo. El humano no quiere distinguir, quiere que pare.
    await db.set_task_graph_state(graph_id, "cancelado")
    return web.json_response(
        _grafo_publico(await db.get_task_graph(graph_id)))


@_require_auth
async def expert_questions_list(request: web.Request) -> web.Response:
    """GET /questions?conversation=<id>&chat=<id>&only_open=1"""
    db = request.app[DB_KEY]
    only_open = request.query.get("only_open", "1") not in ("0", "false")
    rows = await db.list_expert_questions(
        conversation_id=request.query.get("conversation") or None,
        chat_id=request.query.get("chat") or None,
        only_open=only_open)
    out = []
    for r in rows:
        try:
            pregunta = json.loads(r.get("question_json") or "{}")
        except json.JSONDecodeError:
            pregunta = {"title": r.get("question_json") or ""}
        out.append({
            "id": r["id"], "chat_id": r["chat_id"],
            "conversation_id": r.get("conversation_id"),
            "kind": r.get("kind"), "status": r.get("status"),
            "asked_at": r.get("asked_at"),
            "question": pregunta,
        })
    return web.json_response({"questions": out})


@_require_auth
async def expert_question_answer(request: web.Request) -> web.Response:
    """POST /questions/{q_id}/answer  {choice?, text?}

    Responder es la mitad del trabajo: la otra mitad es que el experto
    RETOME. Por eso el endpoint devuelve `resume_prompt`, el texto listo
    para mandar como turno siguiente. Quien responde (Admin UI o el bot)
    lo postea a /experts/run con la misma conversación y el hilo sigue
    donde quedó.
    """
    db = request.app[DB_KEY]
    q_id = request.match_info["q_id"]
    q = await db.get_expert_question(q_id)
    if q is None:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)

    choice = (body.get("choice") or "").strip()
    texto = (body.get("text") or "").strip()
    if not choice and not texto:
        return web.json_response(
            {"error": "mandá `choice` (la key de una opción) o `text`"},
            status=400)

    try:
        pregunta = json.loads(q.get("question_json") or "{}")
    except json.JSONDecodeError:
        pregunta = {}
    etiqueta = texto
    if choice:
        for opt in pregunta.get("options", []):
            if opt.get("key") == choice:
                etiqueta = opt.get("label", choice)
                break
        else:
            return web.json_response(
                {"error": f"opción {choice!r} no está en la pregunta"},
                status=400)

    ok = await db.answer_expert_question(
        q_id, json.dumps({"choice": choice, "label": etiqueta,
                          "free_text": texto}, ensure_ascii=False))
    if not ok:
        # Ya no está abierta. Dos motivos distintos, y decir cuál importa:
        # "ya respondida" es un doble click (Discord y la UI a la vez);
        # "superseded" es que el experto preguntó otra cosa después, y ahí
        # el humano tiene que mirar la decisión NUEVA, no insistir con
        # esta. Un mensaje único mandaba a la persona a buscar una
        # respuesta que ya existía cuando en realidad cambió la pregunta.
        estado = (await db.get_expert_question(q_id)).get("status")
        detalle = {
            "answered": "esa pregunta ya estaba respondida",
            "skipped": "esa pregunta la habías descartado",
            "superseded": "esa decisión quedó vieja: el experto preguntó "
                          "otra cosa después. Mirá la última.",
        }.get(estado, f"esa pregunta ya no está abierta ({estado})")
        return web.json_response({"error": detalle, "status": estado},
                                 status=409)

    # El texto que retoma el hilo. Lleva la pregunta adentro porque el
    # experto la hizo en un turno anterior y la capa 3 del historial se
    # queda solo con su texto final: sin repetirla, "sí, dale" no tiene
    # referente.
    titulo = (pregunta.get("title") or "").strip()
    resume = (f"Respuesta a tu pregunta «{titulo}»: {etiqueta}"
              if titulo else f"Respuesta: {etiqueta}")
    if texto and choice:
        resume += f"\n\n{texto}"

    # F4: si la pregunta era de una tarea de un grafo, contestarla tiene
    # que MOVER el grafo. Sin esto, "para y pregunta" era un callejón sin
    # salida: el orquestador dejaba la pregunta, el humano la contestaba
    # y el plan seguía parado igual porque nadie llevaba la respuesta a
    # la tarea.
    grafo_info = await _retomar_grafo_tras_respuesta(
        request, q, pregunta, choice, texto, etiqueta)

    return web.json_response({
        "ok": True, "id": q_id, "answer": etiqueta,
        "conversation_id": q.get("conversation_id"),
        "project": q.get("project_slug"),
        # Cuando la respuesta retomó un grafo NO hay que mandar el
        # `resume_prompt` como turno nuevo: el trabajo lo sigue el
        # orquestador, y un turno de chat encima duplicaría el pedido.
        "resume_prompt": "" if grafo_info else resume,
        **({"grafo": grafo_info} if grafo_info else {}),
    })


async def _retomar_grafo_tras_respuesta(
    request: web.Request, q: dict, pregunta: dict, choice: str, texto: str,
    etiqueta: str = "",
) -> Optional[dict]:
    """Lleva la respuesta del humano a la tarea del grafo y lo relanza.

    Dos formas de llegar acá, y son distintas:

    1. **La pregunta la hizo el orquestador** (`kind="grafo"`): el humano
       decidió QUÉ HACER con la tarea — reintentarla, darla por fallada
       o parar el plan. La decisión viaja en la `key` de la opción y no
       en su etiqueta, que es texto en castellano y cambia. Si además
       (o en vez de eso) escribió a mano, ese texto va con la decisión:
       contestar con palabras es tan válido como elegir una opción, y
       antes se descartaba en silencio — la tarea se reintentaba a
       ciegas y el grafo se reanudaba igual, así que no se notaba.
       Va `texto` pelado, NO `etiqueta`: pegarle al detalle de la tarea
       "Reintentala igual" no le dice nada al run siguiente.
    2. **La hizo el nodo desde adentro** (`ask_human`): el humano no
       decidió nada sobre la tarea, contestó algo que la tarea
       necesitaba. Vuelve a `pendiente` con la respuesta pegada al
       detalle, y el run siguiente la lee.

    Devuelve `{graph_id, decision, corriendo}` o None si no era de un
    grafo. Nunca lanza: no poder retomar no puede volver 500 una
    respuesta que YA se guardó.
    """
    from . import orquestador

    db = request.app[DB_KEY]
    try:
        if q.get("kind") == "grafo" and pregunta.get("task_id"):
            gid = pregunta.get("graph_id") or ""
            decision = await orquestador.aplicar_respuesta(
                db, gid, pregunta["task_id"], choice, texto)
        else:
            gid = await orquestador.responder_a_la_tarea(
                db, q.get("chat_id") or "", texto or etiqueta)
            decision = "responder" if gid else ""
        if not gid or not decision:
            return None
        if decision == "parar":
            # Ya quedó `cancelado`; si además estaba corriendo, cortarlo.
            if tarea := request.app[GRAFOS_KEY].get(gid):
                tarea.cancel()
            return {"graph_id": gid, "decision": decision, "corriendo": False}

        g = await db.get_task_graph(gid)
        project = await db.get_project((g or {}).get("project_slug") or "")
        if not g or not project or gid in request.app[GRAFOS_KEY]:
            return {"graph_id": gid, "decision": decision,
                    "corriendo": gid in request.app[GRAFOS_KEY]}
        _largar_grafo(request.app, project, gid)
        return {"graph_id": gid, "decision": decision, "corriendo": True}
    except Exception:  # noqa: BLE001
        logger.exception("no pude retomar el grafo tras la respuesta")
        return None
