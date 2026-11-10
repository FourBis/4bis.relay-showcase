"""Server domain handlers extracted from the composition entrypoint."""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import AsyncExitStack, suppress
from typing import Optional

from aiohttp import web
from . import identity

from .server_common import DB_KEY, GRAFOS_KEY, _require_auth, _spawn_bg, logger
from . import coordination
from . import orquestador
from . import planificador
from .server_questions import _question_graph
from .graph_resume_access import _resume_access_denial
from .server_graph_helpers import (
    _PLANIFICANDO, _cancelar_grafo, _grafo_en_curso, _grafo_publico, _grafo_sintetico, _largar_grafo,
    _finish_graph_cancel, _graph_control_lock, _stages_de,
)

from .server_graph_reads import (
    _graph_public_with_policy, _graph_read_response, _graph_verifier_enabled,
    conversation_plan, expert_questions_list, graphs_get, graphs_list,
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
                    read_only=not identity.can_write_project(request, project))
            except (RuntimeError, OSError) as exc:
                return web.json_response({"error": str(exc), "conversation_id": conv_id}, status=422)
    if conv_id:
        conv = await db.get_conversation(conv_id)
        if not conv or conv["project_slug"] != slug:
            return web.json_response({"error": "La conversación no pertenece al proyecto"}, status=400)
        task = await db.get_conversation_task(conv_id)
        if task.get("state") in task_service.STOPPED:
            return web.json_response({"error": "Continúa la tarea desde sus controles"}, status=409)
        if task.get("mode") == "write" and not identity.can_write_project(request, project):
            return web.json_response({"error": "No tienes permiso de escritura en este proyecto. Revisa Equipo."}, status=403)
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
    if conv_id:
        task = await db.get_conversation_task(conv_id)
        if (task.get("state") in task_service.STOPPED
                or conv_id in getattr(db, "_task_cancellations", {})):
            return web.json_response({"error": "La tarea está detenida; revisa sus controles."}, status=409)
    # No ceder el loop entre comprobar la reserva y registrar al planificador.
    if planning_key in _PLANIFICANDO:
        return web.json_response(
            {"error": "este proyecto ya está armando un grafo",
             "message": "Esperá a que el planificador termine y mirá "
                        "el grafo que salga de ahí."},
            status=409)
    _PLANIFICANDO[planning_key] = asyncio.current_task()
    try:
        g = await planificador.armar_grafo(
            project, objetivo, db=db, conversation_id=conv_id,
            contexto=(body.get("contexto") or "").strip())
    except asyncio.CancelledError:
        return web.json_response({"error": "La planificación fue cancelada."}, status=409)
    except RuntimeError as e:
        # El planificador no pudo. Es un 502 y no un 500: el relay
        # funciona, el que no contestó algo usable fue el modelo.
        return web.json_response({"error": str(e)}, status=502)
    finally:
        # Sale sí o sí: si el planificador rompe y no soltamos el slug,
        # el proyecto queda trabado hasta reiniciar el relay.
        _PLANIFICANDO.pop(planning_key, None)

    if conv_id and (await db.get_conversation_task(conv_id)).get("state") in task_service.STOPPED:
        return web.json_response({"error": "La tarea está detenida; revisa sus controles.",
                                  "graph_id": g["id"]}, status=409)
    if body.get("arrancar", True):
        from . import identity
        _largar_grafo(request.app, project, g["id"], identity.requester(request))
    return web.json_response(_grafo_publico(g), status=202)


_ETAPA = {
    "planner": "planificando", "verifier": "verificando",
    "documenter": "documentando", "question": "te está preguntando",
    "thinking": "ejecutando", "writing": "ejecutando",
    "tool_call": "ejecutando", "say": "ejecutando",
    "heartbeat": "ejecutando", "steer": "ejecutando",
}


@_require_auth
async def graphs_resume(request: web.Request) -> web.Response:
    db = request.app[DB_KEY]
    graph = await db.get_task_graph(request.match_info["id"])
    if graph is None:
        return web.json_response({"error": "not found"}, status=404)
    # El guard de workspace puede reparar estado; autorizar antes de entrar.
    if not await identity.can_control_graph(request, db, graph, for_resume=True):
        return web.json_response({"error": "No tienes permiso para controlar esta tarea."}, status=403)
    denial = await _resume_access_denial(request, graph)
    if denial is not None:
        return denial
    # Mismo orden que pausar/cancelar: conversación antes del grafo.
    async with AsyncExitStack() as controls:
        if conv_id := graph.get("conversation_id"):
            from .task_service import control_lock
            lock = control_lock(db, conv_id)
            if lock.locked():
                return web.json_response(
                    {"error": "La tarea está completando otra acción; espera y reintenta.",
                     "graph_id": graph["id"]}, status=409)
            await controls.enter_async_context(lock)
        # Autorización en base de datos ya bajo el control de la conversación.
        denial = await _resume_access_denial(request, graph)
        if denial is not None:
            return denial
        lock = _graph_control_lock(request.app, graph["id"])
        if lock.locked():
            return web.json_response(
                {"error": "El grafo está completando otra acción; espera y reintenta.",
                 "graph_id": graph["id"]}, status=409)
        await controls.enter_async_context(lock)
        # Las inspecciones de workspace del guard interno se validan contra esta reanudación.
        from .graph_resume_inspection import resume_inspection_scope
        with resume_inspection_scope(db, graph, identity.requester(request)):
            return await _resume_graph(request)


async def _resume_graph(request: web.Request) -> web.Response:
    """POST /graphs/{id}/resume — retoma un grafo cortado.

    Sana las ejecuciones interrumpidas y reabre los cortes por presupuesto o desvío
    al recibir esta acción explícita. Un grafo vivo devuelve 409.
    """
    db = request.app[DB_KEY]
    graph_id = request.match_info["id"]
    g = await db.get_task_graph(graph_id)
    if g is None:
        return web.json_response({"error": "not found"}, status=404)
    if _grafo_en_curso(request.app, graph_id):
        return web.json_response(
            {"error": "ese grafo ya está corriendo", "graph_id": graph_id},
            status=409)
    project = await db.get_project(g.get("project_slug") or "")
    if not project:
        return web.json_response(
            {"error": f"el proyecto {g.get('project_slug')!r} ya no existe"},
            status=409)
    from . import task_service

    async def control_denial(graph):
        if not await identity.can_control_graph(request, db, graph, for_resume=True):
            return web.json_response({"error": "No tienes permiso para controlar esta tarea."}, status=403)
        cid = graph.get("conversation_id")
        state = await db.get_conversation_task(cid) if cid else {}
        if state.get("mode") == "external_pr":
            return web.json_response({"error": "external_pr_read_only"}, status=409)
        conversation = await db.get_conversation(cid) if cid else None
        if (conversation and conversation.get("project_slug") and
                conversation["project_slug"].casefold() != (graph.get("project_slug") or "").casefold()):
            return web.json_response({"error": "la conversación pertenece a otro proyecto"}, status=400)
        # Última autorización antes de reparar o relanzar, ya bajo los controles.
        denial = await _resume_access_denial(request, graph)
        if denial is not None:
            return denial
        if (state.get("state") in task_service.STOPPED
                or cid in getattr(db, "_task_cancellations", {})):
            return web.json_response(
                {"error": "La tarea está detenida; continúa desde sus controles",
                 "graph_id": graph_id}, status=409)
        # Cancellation reserves its marker before waiting for this graph lock.
        # Recheck after await-heavy ACL/state/repair work, not only on entry.
        if _grafo_en_curso(request.app, graph_id):
            return web.json_response(
                {"error": "ese grafo ya está corriendo o cerrándose", "graph_id": graph_id}, status=409)

    denial = await control_denial(g)
    if denial is not None:
        return denial
    # A legacy cancelled/failed graph can contain an exhausted pending node.
    # Sanar sólo reconcilia SQLite: no necesita un checkout para preguntar.
    # Los locks de conversación/grafo protegen la reparación; ejecutar sigue
    # exigiendo el guard de workspace, antes de conceder intentos o activar.
    repair = asyncio.create_task(orquestador.sanar(db, graph_id))
    try:
        await asyncio.shield(repair)
    except asyncio.CancelledError:
        # Keep the controls until waiting-state AND question have settled.
        while not repair.done():
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(repair)
        with suppress(asyncio.CancelledError, Exception):
            repair.result()
        raise
    g = await db.get_task_graph(graph_id)
    if g is None:
        return web.json_response({"error": "not found"}, status=404)
    denial = await control_denial(g)
    if denial is not None:
        return denial
    from . import grafo as G
    from .orchestrator_recovery import prepare_graph_resume
    from .orchestrator_verification import verification_pending, metadata_close_pending
    verifier_enabled = _graph_verifier_enabled(project)

    def closing_only(graph):
        return (verification_pending(graph, verifier_enabled=verifier_enabled)
                or metadata_close_pending(graph, verifier_enabled=verifier_enabled))

    verification_only = closing_only(g)

    def idle_response(graph):
        nodes = [G.Nodo.desde_fila(t, t.get("deps") or ()) for t in graph["tasks"]]
        if (not verification_only and not G.listas(nodes)
                and not any(n.estado == G.CORRIENDO for n in nodes)):
            reason = ("Responde la pregunta pendiente antes de retomar."
                      if any(n.estado == G.ESPERANDO for n in nodes)
                      else "Continúa la conversación indicando cómo resolver el fallo."
                      if G.estado_del_grafo(nodes) != "hecho"
                      else "El plan ya terminó.")
            return web.json_response({"error": "No hay tareas que se puedan retomar. " + reason,
                                      "graph_id": graph_id}, status=409)

    # Un fallo puede reabrirse por presupuesto o desvío: requiere workspace.
    if not any(t["estado"] == G.FALLADO for t in g["tasks"]):
        idle = idle_response(g)
        if idle is not None:
            return idle

    @coordination.guard_workspace(DB_KEY, source="graph")
    async def resume_execution(_request):
        g = await db.get_task_graph(graph_id)
        if g is None:
            return web.json_response({"error": "not found"}, status=404)
        current_project = await db.get_project(g.get("project_slug") or "")
        if not current_project or _graph_verifier_enabled(current_project) != verifier_enabled:
            return web.json_response({"error": "El proyecto cambió; revisa el plan antes de retomarlo."}, status=409)
        denial = await control_denial(g)
        if denial is not None:
            return denial
        # El cierre final no concede intentos ni entra al scheduler normal.
        if not verification_only:
            await prepare_graph_resume(db, g)
        g = await db.get_task_graph(graph_id)
        if g is None:
            return web.json_response({"error": "not found"}, status=404)
        if verification_only and not closing_only(g):
            return web.json_response({"error": "El cierre cambió; revisa el plan antes de retomarlo.",
                                      "graph_id": graph_id}, status=409)
        idle = idle_response(g)
        if idle is not None:
            return idle
        denial = await control_denial(g)
        if denial is not None:
            return denial
        if g["estado"] != "activo":
            await db.set_task_graph_state(graph_id, "activo")
            denial = await control_denial(g)
            if denial is not None:
                return denial
        _largar_grafo(request.app, current_project, graph_id, identity.requester(request),
                      **({"verification_only": True} if verification_only else {}))
        return web.json_response(
            _grafo_publico(await db.get_task_graph(graph_id)), status=202)

    return await resume_execution(request)


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
    conv_id = g.get("conversation_id")
    if conv_id:
        allowed = await identity.can_control_graph(request, db, g)
    else:
        # Los grafos históricos pueden no tener conversación ni proyecto.
        # Admin debe poder detenerlos; otros actores necesitan el proyecto asignado.
        project = await db.get_project(g.get("project_slug") or "")
        allowed = identity.role_of(request) == identity.OWNER_ROLE or identity.can_write_project(request, project)
    if not allowed:
        return web.json_response({"error": "No tienes permiso para controlar esta tarea."}, status=403)
    await _cancelar_grafo(request.app, graph_id)
    return web.json_response(
        _grafo_publico(await db.get_task_graph(graph_id)))


def _question_actor_error(request, graph_id):
    active = _grafo_en_curso(request.app, graph_id)
    if active and getattr(active, "relay_actor", None) != identity.requester(request):
        return web.json_response(
            {"error": "El grafo pertenece a otra persona; inicia un nuevo turno cuando termine.",
             "graph_id": graph_id}, status=409)


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
    graph = await _question_graph(db, q)
    if not await identity.can_control_graph(request, db, graph):
        return web.json_response({"error": "No tienes permiso para controlar esta tarea."}, status=403)
    denial = _question_actor_error(request, graph.get("id") or "")
    if denial is not None:
        return denial
    # Leer al cliente antes de reservar los controles de ejecución.
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    if not isinstance(body, dict) or any(
            body.get(key) is not None and not isinstance(body[key], str)
            for key in ("choice", "text")):
        return web.json_response({"error": "choice y text deben ser texto."}, status=400)
    from . import task_service
    locks = []
    if graph.get("id"):
        cid = graph.get("conversation_id") or q.get("conversation_id")
        if cid:
            locks.append(task_service.control_lock(db, cid))
        locks.append(_graph_control_lock(request.app, graph["id"]))
    async def finish_answer():
        # Mismo orden que cancelar una tarea: conversación antes del grafo.
        async with AsyncExitStack() as controls:
            for lock in locks:
                if lock.locked():
                    return web.json_response(
                        {"error": "La tarea está completando otra acción; espera y reintenta."}, status=409)
                await controls.enter_async_context(lock)
            if not await identity.can_control_graph(request, db, graph):
                return web.json_response({"error": "No tienes permiso para controlar esta tarea."}, status=403)
            return await _answer_question(request, q, graph, body)

    # Una desconexión no deja la respuesta guardada sin aplicar al nodo.
    return await asyncio.shield(_spawn_bg(finish_answer(), hold_workspace=False))


async def _answer_question(request, q, graph, body):
    from . import task_service
    db, q_id = request.app[DB_KEY], q["id"]
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

    active_gid = (graph or {}).get("id") or ""
    cid = graph.get("conversation_id") or q.get("conversation_id")
    if active_gid:
        if (active_gid in getattr(db, "_graph_cancellations", {})
                or cid in getattr(db, "_task_cancellations", {})):
            return web.json_response({"error": "La cancelación todavía está en curso."}, status=409)
        state = await db.get_conversation_task(cid) if cid else {}
        worker = request.app[GRAFOS_KEY].get(active_gid)
        paused_live = (state.get("state") == "paused" and worker is not None
                       and not worker.done() and not worker.cancelling())
        stopping = q.get("kind") == "grafo" and choice == "parar"
        if state.get("state") in task_service.STOPPED and not paused_live and not stopping:
            return web.json_response(
                {"error": "La tarea está detenida; revisa sus controles antes de responder."}, status=409)
    denial = _question_actor_error(request, active_gid)
    if denial is not None:
        return denial

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
    if (grafo_info is None and graph.get("id") and (
            q.get("kind") == "grafo" and pregunta.get("graph_id") == graph["id"]
            or q.get("chat_id") and any(t.get("chat_id") == q["chat_id"]
                                       for t in graph.get("tasks", [])))):
        # The answer was saved for this graph. A blocked/failed continuation
        # must not become a generic chat prompt that the UI auto-submits.
        grafo_info = {"graph_id": graph["id"], "decision": choice or "responder",
                      "corriendo": False,
                      "blocked": "Respuesta guardada; revisa el plan antes de retomarlo."}

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
       "Reinténtala igual" no le dice nada al run siguiente.
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
            await _finish_graph_cancel(request.app, gid)
            return {"graph_id": gid, "decision": decision, "corriendo": False}

        g = await db.get_task_graph(gid)
        project = await db.get_project((g or {}).get("project_slug") or "")
        info = {"graph_id": gid, "decision": decision, "corriendo": False}
        if not g or not project:
            return {**info, "blocked": "El plan o su proyecto ya no está disponible."}

        async def continuation_status(graph):
            from . import task_service
            cid = graph.get("conversation_id")
            state = await db.get_conversation_task(cid) if cid else {}
            # Check the central ACL after the other reads, immediately before
            # the synchronous registry checks and eventual launch.
            if not await identity.can_control_graph(request, db, graph, for_resume=True):
                return {**info, "blocked": "Ya no tienes permiso para continuar este plan."}
            if (gid in getattr(db, "_graph_cancellations", {})
                    or cid in getattr(db, "_task_cancellations", {})):
                return {**info, "blocked": "La cancelación todavía está en curso."}
            worker = request.app[GRAFOS_KEY].get(gid)
            live = worker is not None and not worker.done() and not worker.cancelling()
            paused_live = state.get("state") == "paused" and live
            if state.get("state") in task_service.STOPPED and not paused_live:
                return {**info, "blocked": "La tarea está detenida; revisa sus controles."}
            denial = _question_actor_error(request, gid)
            if denial is not None:
                return {**info, "blocked": "El plan está controlado por otra persona."}
            if worker is not None:
                return ({**info, "corriendo": True} if live else
                        {**info, "blocked": "El proceso anterior todavía está cerrándose."})

        status = await continuation_status(g)
        if status is not None:
            return status
        # The explicit answer resumes the graph as well as its node. Leaving
        # a legacy cancelled graph cancelled skips normal final verification
        # and masks completed work in the UI. Stop decisions returned above.
        if g.get("estado") != "activo":
            await db.set_task_graph_state(gid, "activo")
            g = await db.get_task_graph(gid)
            if g is None:
                return {**info, "blocked": "El plan ya no está disponible."}
            # Cancellation reserves its marker before waiting for our lock;
            # never overtake a newer stop while activation was awaiting SQL.
            status = await continuation_status(g)
            if status is not None:
                return status
        _largar_grafo(request.app, project, gid, identity.requester(request))
        return {"graph_id": gid, "decision": decision, "corriendo": True}
    except Exception:  # noqa: BLE001
        logger.exception("no pude retomar el grafo tras la respuesta")
        return None
