"""Server domain handlers extracted from the composition entrypoint."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

from aiohttp import web

from .server_common import DB_KEY, GRAFOS_KEY, NIGHT_KEY, NOTIFY_KEY, _require_auth, logger
from . import coordination
from . import dbtool
from . import grafo as grafo_mod
from . import night as night_mod
from . import notify
from . import planificador
from .db import Database
from .notify import NotifyClient
from .server_graph_helpers import _grafo_publico, _largar_grafo
# ---------- modo nocturno (ADR-028: pipeline de dos fases) ----------


@_require_auth
@coordination.guard_workspace(DB_KEY)
async def night_start(request: web.Request) -> web.Response:
    """POST /night-mode/start — arranca un night run para un proyecto.

    Body:
        project:      str  (requerido, slug)
        deadline_iso: str  (opcional; default próximas 7am local)
        directive:    str  (opcional; semilla de la Fase 1)
        error_logs:   str  (opcional; input alternativo de la Fase 1)

    Returns:
        202 -> {run_id, project, started_at, deadline_at}
        400 -> night_mode_enabled=0 / body o deadline malformados
        404 -> proyecto desconocido
        409 -> ya hay un run activo para el proyecto
    """
    from datetime import datetime as _dt

    from . import night as night_mod

    db: Database = request.app[DB_KEY]
    registry: dict = request.app[NIGHT_KEY]
    notify: NotifyClient = request.app[NOTIFY_KEY]

    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)

    slug = body.get("project")
    if not isinstance(slug, str) or not slug.strip():
        return web.json_response({"error": "project requerido (string)"},
                                 status=400)
    project = await db.get_project(slug.strip())
    if project is None or not project["enabled"]:
        return web.json_response(
            {"error": f"proyecto {slug!r} desconocido o deshabilitado"},
            status=404)
    if not project.get("night_mode_enabled"):
        return web.json_response(
            {"error": f"night_mode_enabled=0 en {project['slug']!r}: "
                      "activalo antes (PATCH del proyecto)"},
            status=400)

    # Lock: 1 run activo por proyecto (memoria + DB por si el relay reinició).
    for orch, task in registry.values():
        if (orch.project["slug"].lower() == project["slug"].lower()
                and not task.done()):
            return web.json_response(
                {"error": "ya hay un night run activo para este proyecto",
                 "run_id": orch.run_id}, status=409)
    stale = await db.active_night_run(project["slug"])
    if stale and stale["id"] not in registry:
        # Fila colgada de un run que murió con el proceso: cerrarla para
        # no bloquear runs nuevos para siempre.
        await db.finish_night_run(
            stale["id"], end_reason="crashed",
            error="proceso del relay reiniciado con el run activo")

    # Plantilla (2026-08-27): `template` reemplaza a `directive` cuando
    # esta viene vacia. No al reves — una directiva explicita SIEMPRE
    # gana, porque el caso "arranque desde la plantilla pero con un
    # retoque" se resuelve editando el texto en el composer, y si la
    # plantilla pisara eso el retoque se perderia sin aviso.
    directiva = (body.get("directive") or "").strip()
    tpl_nombre = (body.get("template") or "").strip()
    if tpl_nombre and not directiva:
        tpl = await db.get_night_template(tpl_nombre, project["slug"])
        if tpl is None:
            return web.json_response(
                {"error": f"no hay plantilla {tpl_nombre!r} para "
                          f"{project['slug']!r} ni global"}, status=404)
        directiva = tpl["directiva"]

    deadline_iso = body.get("deadline_iso") or ""
    if deadline_iso:
        try:
            deadline = _dt.fromisoformat(deadline_iso)
            if deadline.tzinfo is None:
                deadline = deadline.astimezone()
        except ValueError:
            return web.json_response(
                {"error": f"deadline_iso inválido: {deadline_iso!r}"},
                status=400)
    else:
        deadline = night_mod.default_deadline()

    state_dir = Path(os.environ.get("STATE_DIR", "./state"))
    orch = night_mod.NightOrchestrator(
        db=db, project=project, deadline=deadline,
        directive=directiva,
        error_logs=body.get("error_logs") or "",
        notify=notify, state_dir=state_dir)
    task = asyncio.create_task(orch.run())
    coordination.hold_current(task)
    registry[orch.run_id] = (orch, task)

    return web.json_response(
        {"run_id": orch.run_id, "project": project["slug"],
         "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "deadline_at": deadline.isoformat(),
         # Iter 9.7: el cliente sabe si el run es interactivo.
         "interactive": bool(project.get("interactive_mode")),
         "questions_endpoint":
             f"/admin/api/night/questions?run_id={orch.run_id}"},
        status=202)


@_require_auth
async def night_stop(request: web.Request) -> web.Response:
    """POST /night-mode/stop {run_id} — para el loop después de la tarea
    actual. Idempotente: parar un run ya parado devuelve 200 igual."""
    registry: dict = request.app[NIGHT_KEY]
    try:
        body: dict = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    run_id = body.get("run_id") or ""
    entry = registry.get(run_id)
    if entry is None:
        return web.json_response(
            {"error": f"run {run_id!r} desconocido (o el relay reinició)"},
            status=404)
    orch, _task = entry
    orch.stop()
    return web.json_response({"run_id": run_id, "status": orch.status})


@_require_auth
async def night_status(request: web.Request) -> web.Response:
    """GET /night-mode/status?run_id=... — snapshot del run (memoria) o
    la fila night_runs si el run ya no está en memoria.
    Sin run_id: {"active": [snapshots de los runs vivos]}."""
    db: Database = request.app[DB_KEY]
    registry: dict = request.app[NIGHT_KEY]
    run_id = request.query.get("run_id", "")
    if not run_id:
        # Sin run_id: snapshot de todos los runs vivos (puede ser []).
        return web.json_response(
            {"active": [orch.snapshot() for orch, _task in registry.values()]})
    entry = registry.get(run_id)
    if entry is not None:
        return web.json_response(entry[0].snapshot())
    row = await db.get_night_run(run_id)
    if row is None:
        return web.json_response({"error": f"run {run_id!r} desconocido"},
                                 status=404)
    return web.json_response(row)


# ---- 2026-08-16: conexiones SQL que el chat puede consultar ----
#
# El DSN se guarda acá y el experto usa un alias, así la contraseña no
# entra al historial del hilo. `GET` nunca devuelve el DSN entero.


@_require_auth
async def db_connections_list(request: web.Request) -> web.Response:
    """GET /admin/api/db-connections — sin credenciales.

    `relay` siempre viene en la lista, tenga fila o no: es la conexión
    que el experto puede usar sin que nadie le registre nada, así que la
    UI necesita mostrarla para poder darle permiso de escritura.
    """
    from . import dbtool
    db = request.app[DB_KEY]
    filas = await db.list_db_connections()
    propia = next((f for f in filas if f["alias"] == "relay"), None)
    reservada = {
        "alias": "relay",
        "dsn": dbtool.redactar(str(getattr(db, "path", ""))),
        "motor": "sqlite",
        "descripcion": (propia or {}).get("descripcion")
                       or "la base del propio relay (chats, tokens, runs)",
        "escribir": bool((propia or {}).get("escribir")),
        "reservada": True,
    }
    return web.json_response({"connections": [reservada] + [
        {"alias": f["alias"], "dsn": dbtool.redactar(f["dsn"]),
         "motor": dbtool.motor(f["dsn"]),
         "descripcion": f.get("descripcion") or "",
         "escribir": bool(f.get("escribir"))}
        for f in filas if f["alias"] != "relay"]})


@_require_auth
async def db_connection_upsert(request: web.Request) -> web.Response:
    """POST /admin/api/db-connections {alias, dsn, descripcion?, escribir?}"""
    from . import dbtool
    db = request.app[DB_KEY]
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "json inválido"}, status=400)
    alias = (body.get("alias") or "").strip().lower()
    dsn = (body.get("dsn") or "").strip()
    if alias == "relay":
        # Reservado a medias, y la mitad importa: la RUTA se pisa con la
        # base real del relay (si el alias se pudiera re-apuntar,
        # `db_query("relay", …)` iría a otra base y el experto creería
        # estar mirando la suya). El PERMISO sí se guarda: habilitar la
        # escritura sobre su propia base es una decisión del humano
        # (2026-08-16, a pedido).
        dsn = str(getattr(db, "path", "")) or dsn
    if alias and not dsn:
        # Sin `dsn` sobre un alias que ya existe = "cambiame solo el
        # permiso". No es comodidad: el GET devuelve el DSN REDACTADO, así
        # que una UI que quisiera reenviarlo guardaría
        # `postgres://…@host/base` como cadena real y rompería la
        # conexión. La única forma segura de editar el permiso es no
        # tocar la cadena.
        previa = await db.get_db_connection(alias)
        if previa:
            dsn = previa["dsn"]
    if not alias or not dsn:
        return web.json_response(
            {"error": "alias y dsn son requeridos"}, status=400)
    fila = await db.upsert_db_connection(
        alias, dsn, descripcion=(body.get("descripcion") or "").strip(),
        escribir=bool(body.get("escribir")))
    return web.json_response({
        "ok": True, "alias": fila["alias"],
        "dsn": dbtool.redactar(fila["dsn"]),
        "motor": dbtool.motor(fila["dsn"]),
        "escribir": bool(fila["escribir"])})


@_require_auth
async def db_connection_delete(request: web.Request) -> web.Response:
    db = request.app[DB_KEY]
    alias = request.match_info["alias"]
    if not await db.delete_db_connection(alias):
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"ok": True, "alias": alias})


@_require_auth
async def db_connection_test(request: web.Request) -> web.Response:
    """POST /admin/api/db-connections/{alias}/test — ¿conecta?

    Existe porque el momento de descubrir que el DSN está mal es cuando
    lo cargás, no tres días después en medio de un run del experto.
    """
    from . import dbtool
    db = request.app[DB_KEY]
    alias = request.match_info["alias"]
    try:
        dsn, _ro = await dbtool.resolver(
            db, alias, relay_db_path=str(getattr(db, "path", "")))
    except dbtool.ConexionDesconocida as e:
        return web.json_response({"error": str(e)}, status=404)
    sonda = ("SELECT 1 AS ok" if dbtool.motor(dsn) == "postgres"
             else "SELECT 1 AS ok")
    out = await dbtool.consultar(dsn, sonda)
    ok = not out.startswith("error")
    return web.json_response({"ok": ok, "alias": alias,
                              "motor": dbtool.motor(dsn), "salida": out[:400]})


# ---- 2026-08-16: preguntas del experto al humano (chat) ----
#
# Contrato, calcado del harness de Claude Code y adaptado a que acá el
# humano puede tardar horas: el experto NO bloquea esperando. Llama a
# `ask_human`, deja la pregunta anotada, termina el turno con lo que
# hizo, y la respuesta del humano entra como el TURNO SIGUIENTE de la
# conversación. El hilo ya sabe pasar contexto entre turnos; no hacía
# falta inventar un canal nuevo.


async def _grafo_estancado(app: Optional[web.Application], db: Database,
                           g: dict) -> bool:
    """¿Este grafo `activo` ya no puede avanzar por su cuenta?

    Un grafo activo le impide al hilo armar otro, y con razón: dos
    planes sobre el mismo repo se pisan los archivos. La excepción es el
    que quedó colgado — nadie lo está corriendo, no tiene ningún nodo
    vivo, y la pregunta que lo dejó esperando ya no está abierta. Ese no
    protege nada: solo deja la conversación sin poder volver a
    planificar, para siempre. Hay uno así en la base desde el 24/8, con
    tres nodos en `esperando_humano` y sus cinco preguntas contestadas.

    Las tres condiciones son AND y el default es "bloquea": ante la
    duda, el error caro es dejar correr dos planes, no hacer esperar
    uno. Un grafo cortado a mitad (nodos en `corriendo` que nadie
    ejecuta) NO entra acá a propósito — de ese se encarga el barrido del
    boot, que lo vuelve a largar y lo sana.
    """
    # `.get` y no `[...]`: los tests pasan un dict pelado como app.
    if g["id"] in ((app or {}).get(GRAFOS_KEY) or {}):
        return False
    if any((t.get("estado") or "") == grafo_mod.CORRIENDO
           for t in (g.get("tasks") or ())):
        return False
    abiertas = await db.list_expert_questions(
        conversation_id=g.get("conversation_id") or "", only_open=True,
        limit=1)
    return not abiertas


async def _grafo_en_vez_de_proponer(
    app: Optional[web.Application], db: Database, project: dict, user: str,
    conv_id: Optional[str], result: dict,
) -> dict:
    """`DEMASIADO_GRANDE` deja de ser una propuesta y pasa a ser un plan
    que corre.

    Hasta hoy, cuando el planificador juzgaba que el pedido no entra en
    un run, el relay devolvía la descomposición en prosa y **no ejecutaba
    nada**: el humano tenía que elegir por dónde empezar y volver a
    pedirlo, una tarea por vez. Ese era el techo del que hablábamos —
    justo el caso para el que se construyó el grafo.

    Ahora ese mismo corte arma el grafo y lo larga. Se engancha acá y no
    en `run_expert_staged` a propósito: la task de fondo tiene que
    quedar registrada en la app para que el apagado la espere y para que
    `/graphs/{id}/cancel` la encuentre, y eso es cosa del servidor.

    Si algo sale mal —el modelo no devuelve un grafo válido, la base
    falla— se vuelve al texto de antes. Degradar a lo que ya funcionaba
    es mejor que un error: la descomposición sigue siendo útil.

    Se apaga con el flag `grafo_automatico` del proyecto.
    """
    from . import planificador

    defaults = project.get("defaults_json") or {}
    if app is None or not defaults.get("grafo_automatico", True):
        return result
    if conv_id and (vivo := await db.active_task_graph(conv_id)) \
            and not await _grafo_estancado(app, db, vivo):
        # Ya hay un grafo vivo en este hilo: dos planes sobre el mismo
        # repo se pisan los archivos y el humano ve dos avances a los
        # tumbos. Se queda la propuesta en texto.
        #
        # Pero se lo decimos. Antes esto devolvía la descomposición tal
        # cual —"todavía no ejecuté nada, dime por cuáles empiezo"—, que
        # es indistinguible de un pedido grande normal: el humano no
        # tenía forma de saber que lo único que faltaba era esperar.
        # Pasó el 30/8 a las 07:25: el planificador gastó 17k tokens en
        # cortar el pedido y la respuesta se leyó como una propuesta más.
        p = _grafo_publico(vivo).get("progreso") or {}
        result["content"] = (result.get("content") or "") + (
            f"\n\n⏸️ **No lo ejecuté todavía**: en este hilo ya hay un plan "
            f"corriendo ({p.get('hechos', 0)} de {p.get('total', 0)} tareas "
            "listas). Dos planes sobre el mismo repo se pisan los archivos. "
            "Cuando termine, vuelve a pedirme esto y lo ejecuto — o cancela "
            "el plan actual desde el panel del plan.")
        return result
    try:
        g = await planificador.armar_grafo(
            project, user, db=db, conversation_id=conv_id or "",
            # La descomposición en prosa que ya se pagó entra como
            # contexto: el razonador no vuelve a decidir el corte, solo
            # lo estructura.
            contexto=result.get("plan") or "")
    except Exception as e:  # noqa: BLE001 — sin grafo queda la propuesta
        logger.warning("no pude armar el grafo del pedido grande (%r): "
                       "devuelvo la descomposición en texto", e)
        return result

    _largar_grafo(app, project, g["id"])
    result["graph_id"] = g["id"]
    result["phase_at_end"] = "graph"
    result["content"] = (
        "📋 Este pedido es grande, así que lo **partí en "
        f"{len(g['tasks'])} tareas** y las estoy ejecutando:\n\n"
        f"{planificador.resumen(g)}\n\n"
        "Van de a dos en paralelo, y ninguna toca un archivo que otra "
        "esté tocando. Si una falla y no se puede reintentar sola, te "
        "pregunto antes de seguir.\n\n"
        f"`{g['id']}` — para pararlo: `POST /graphs/{g['id']}/cancel`"
    )
    return result


# =====================================================================
# Grafos de tareas: el disparador (2026-08-23)
# =====================================================================
#
# Hasta hoy el motor del grafo (F1 + F2) estaba entero y no lo llamaba
# nadie: `correr_grafo` solo aparecía en los tests. Estos endpoints son
# la puerta — arman el grafo desde un pedido, lo largan en background y
# lo dejan consultable, que es lo que va a leer el panel del chat.
#
# El grafo corre en una task de fondo registrada en BG_TASKS_KEY, igual
# que un run de experto: sin eso, el apagado del relay no la espera y
# quedan nodos escribiendo mientras el proceso se cierra.
