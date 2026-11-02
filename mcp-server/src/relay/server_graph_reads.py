"""Lecturas de planes y preguntas con autorización final del grafo público."""
from __future__ import annotations

import json

from aiohttp import web

from . import identity
from .server_common import DB_KEY, _require_auth
from .server_graph_helpers import (
    _grafo_en_curso, _grafo_publico, _grafo_sintetico, _stages_de,
)


def _graph_verifier_enabled(project):
    return bool(project and project.get("enabled", True)
                and (project.get("defaults_json") or {}).get("graph_verifier", True))


async def _graph_public_with_policy(db, graph):
    project = await db.get_project(graph.get("project_slug") or "")
    return _grafo_publico(graph, verifier_enabled=_graph_verifier_enabled(project))


async def _graph_read_response(request, *, graph=None, conversation_id="", payload=None,
                               shape="item", with_running=False, status=200):
    """Authorize the final response and its verifier policy in one snapshot."""
    from . import conversation_access
    db, actor = request.app[DB_KEY], identity.requester(request)
    def snapshot(conn):
        user = conversation_access.user_tx(conn, actor)
        if not user:
            raise PermissionError("Grafo no disponible")
        if conversation_id:
            conversation_access.require_tx(conn, conversation_id, actor, "read")
        if graph is None:
            return None
        current = conn.execute(
            "SELECT conversation_id,project_slug FROM task_graphs WHERE id=?",
            (graph["id"],)).fetchone()
        if (not current
                or (current["conversation_id"] or "") != (graph.get("conversation_id") or "")
                or current["project_slug"] != graph.get("project_slug")):
            raise PermissionError("El origen del grafo cambió")
        cid = current["conversation_id"] or ""
        if conversation_id and cid != conversation_id:
            raise PermissionError("El grafo pertenece a otra conversación")
        if cid:
            conversation_access.require_tx(conn, cid, actor, "read")
        elif user.get("role") != "owner":
            # Historical graphs without a conversation retain administrative
            # access; a graph ID or its project never grants a member access.
            raise PermissionError("Grafo no disponible")
        row = conn.execute("SELECT * FROM projects WHERE slug=? COLLATE NOCASE",
                           (current["project_slug"],)).fetchone()
        project = db._parse_project(dict(row)) if row else None
        return _grafo_publico(graph, verifier_enabled=_graph_verifier_enabled(project))
    try:
        view = await conversation_access.transaction(db, snapshot)
    except PermissionError:
        return web.json_response({"error": "not found"}, status=404,
                                 headers={"Cache-Control": "no-store"})
    # No await follows the final ACL/policy snapshot, including live flags.
    if graph is not None:
        if with_running:
            view["corriendo"] = bool(_grafo_en_curso(request.app, graph["id"]))
        payload = ({"modo": "grafo", "grafo": view} if shape == "plan"
                   else {"graph": view} if shape == "list" else view)
    return web.json_response(payload, status=status, headers={"Cache-Control": "no-store"})


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
        return await _graph_read_response(request, conversation_id=conv_id,
                                          payload={"error": "not found"}, status=404)

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
        vivo = bool(_grafo_en_curso(request.app, g["id"]))
        if vivo or not await db.hay_turnos_humanos_despues(
                conv_id, g.get("updated_at") or g.get("created_at") or ""):
            return await _graph_read_response(request, graph=g, conversation_id=conv_id,
                                              shape="plan", with_running=True)

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
    return await _graph_read_response(request, conversation_id=conv_id,
                                      payload={"modo": "grafo", "grafo": salida})


@_require_auth
async def graphs_get(request: web.Request) -> web.Response:
    """GET /graphs/{id} — el grafo con su progreso. Lo lee el panel."""
    db = request.app[DB_KEY]
    g = await db.get_task_graph(request.match_info["id"])
    if g is None:
        return await _graph_read_response(request, payload={"error": "not found"}, status=404)
    return await _graph_read_response(request, graph=g, with_running=True)


@_require_auth
async def graphs_list(request: web.Request) -> web.Response:
    """GET /graphs?conversation=<id> — el grafo activo de un hilo."""
    db = request.app[DB_KEY]
    conv = (request.query.get("conversation") or "").strip()
    if not conv:
        return web.json_response({"error": "mandá `conversation`"}, status=400)
    g = await db.active_task_graph(conv)
    if g is None:
        return await _graph_read_response(request, conversation_id=conv, payload={"graph": None})
    return await _graph_read_response(request, graph=g, conversation_id=conv, shape="list")


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
    from .private_mcp_access import visible_rows
    rows = await visible_rows(request, rows, kind="question")
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
