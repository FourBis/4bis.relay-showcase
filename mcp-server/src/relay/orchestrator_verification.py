"""Evidence collection and close verification for task graphs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re

from . import grafo as G

logger = logging.getLogger("relay.orquestador")


def verification_signature(graph: dict) -> str:
    """Bind a verification intent to inputs, never to wall-clock timestamps."""
    try:
        tasks = graph["tasks"]
        if not tasks or any(not isinstance(t.get("id"), str) for t in tasks):
            return ""
        inputs = {k: graph.get(k) for k in ("id", "objetivo", "project_slug", "conversation_id")}
        inputs["tasks"] = sorted(tasks, key=lambda t: t["id"])
        return hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")).hexdigest()
    except (KeyError, TypeError, ValueError, AttributeError):
        return ""


def _verification_record(graph):
    raw = graph.get("verificacion_json")
    if raw is None or raw == "":
        return None
    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else False
    except (TypeError, ValueError):
        return False


def _closed_nodes(graph):
    try:
        nodes = [G.Nodo.desde_fila(t, t.get("deps") or ()) for t in graph["tasks"]]
        replaced = G.sustituidos(nodes)
        operational = [n for n in nodes if n.id not in replaced]
        return operational if operational and all(
            n.estado in {G.HECHO, G.FALLADO, G.BLOQUEADO} for n in operational) else []
    except (KeyError, TypeError, ValueError, AttributeError):
        return []


def _closure_correction_candidate(record):
    """Typed provenance, never an ID-prefix or failure-text inference."""
    if (not isinstance(record, dict) or type(record.get("verification_version")) is not int
            or record.get("verification_version") != 1 or record.get("pending") is not False
            or record.get("verdict") != "needs_more" or record.get("error")):
        return None
    proof = record.get("closure_correction")
    if (not isinstance(proof, dict) or type(proof.get("version")) is not int
            or proof.get("version") != 1
            or proof.get("kind") not in {"blocked_candidate", "executable_candidate"}
            or proof.get("input_signature") != record.get("input_signature")
            or not isinstance(proof.get("input_signature"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", proof["input_signature"])):
        return None
    task, attempts = proof.get("task"), proof.get("input_attempts")
    fields = {"id", "graph_id", "titulo", "detalle", "estado", "idempotente", "intentos",
              "max_intentos", "modelo", "chat_id", "resultado", "error", "orden", "archivos",
              "started_at", "ended_at", "parent_id", "deps"}
    if (not isinstance(task, dict) or set(task) != fields
            or not all(isinstance(task[k], str) and task[k] for k in ("id", "graph_id", "titulo"))
            or not all(isinstance(task[k], str) for k in ("detalle", "error"))
            or task["estado"] != (G.FALLADO if proof["kind"] == "blocked_candidate" else G.PENDIENTE)
            or any(type(task[k]) is not int for k in ("idempotente", "intentos", "max_intentos", "orden"))
            or (task["idempotente"], task["intentos"], task["max_intentos"]) != (0, 0, 1)
            or any(task[k] != "" for k in ("modelo", "resultado"))
            or any(task[k] is not None for k in ("chat_id", "started_at", "ended_at", "parent_id"))
            or task["archivos"] != "[]" or task["deps"] != []
            or not isinstance(attempts, dict) or not attempts or task["id"] in attempts
            or any(not isinstance(k, str) or not k or type(v) is not int or v < 0
                   for k, v in attempts.items())):
        return None
    if ("input_attempts" in record and (
            not _valid_attempt_baseline(record["input_attempts"])
            or record["input_attempts"] != attempts)):
        return None
    return proof


def verification_confirmation_matches(graph: dict, record=None) -> bool:
    """A confirmed input plus its exact nonexecuted close metadata is one pass."""
    record = _verification_record(graph) if record is None else record
    if (not isinstance(record, dict) or type(record.get("verification_version")) is not int
            or record.get("verification_version") != 1 or record.get("pending") is not False):
        return False
    signature = verification_signature(graph)
    if signature and record.get("input_signature") == signature:
        return True
    proof = _closure_correction_candidate(record)
    if (not proof or proof["kind"] != "blocked_candidate"
            or proof["task"]["graph_id"] != graph.get("id")):
        return False
    tasks = graph.get("tasks") or []
    matching = [t for t in tasks if t.get("id") == proof["task"]["id"]]
    if len(matching) != 1 or matching[0] != proof["task"]:
        return False
    original = [t for t in tasks if t.get("id") != proof["task"]["id"]]
    if {t.get("id") for t in original} != set(proof["input_attempts"]):
        return False
    return verification_signature({**graph, "tasks": original}) == record["input_signature"]


def _valid_attempt_baseline(baseline):
    return (isinstance(baseline, dict) and bool(baseline)
            and all(isinstance(k, str) and bool(k) and type(v) is int and v >= 0
                    for k, v in baseline.items()))


def _attempts_advanced(graph, baseline):
    return (_valid_attempt_baseline(baseline)
            and any(type(t.get("intentos")) is int and t["intentos"] > baseline.get(t.get("id"), 0)
                    for t in graph.get("tasks") or []))


def _new_pass_after_closure(graph, record):
    """Metadata changes alone do not authorize another model call."""
    if "verification_version" in record and (
            type(record.get("verification_version")) is not int
            or record["verification_version"] != 1
            or not isinstance(record.get("input_signature"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["input_signature"])):
        return False
    if "input_attempts" in record and (
            record.get("pending") is not False
            or record.get("verification_version") != 1
            or not _valid_attempt_baseline(record["input_attempts"])):
        return False
    if "closure_correction" not in record:
        if record.get("verdict") == "needs_more" and not record.get("error"):
            # Legacy needs_more cannot prove whether a zero-attempt failure
            # was its own synthetic annotation. Do not invent that origin.
            return _attempts_advanced(graph, record.get("input_attempts"))
        return True  # retain other older active-pass contracts.
    proof = _closure_correction_candidate(record)
    if proof is None or proof["task"]["graph_id"] != graph.get("id"):
        return False
    baseline = {**proof["input_attempts"], proof["task"]["id"]: 0}
    return _attempts_advanced(graph, baseline)


def verification_pending(graph: dict, *, verifier_enabled=True) -> bool:
    """Only known pending checks; cancelled/finished legacy absence is ambiguous."""
    if ((verifier_enabled is not True and verifier_enabled is not None)
            or graph.get("estado") not in {"activo", "cancelado", "fallado", "hecho"}):
        return False
    nodes = _closed_nodes(graph)
    if not nodes or not verification_signature(graph):
        return False
    record = _verification_record(graph)
    if record is None:
        # Retain the existing active/no-result recovery contract. A cancelled
        # legacy graph may instead have completed with its verifier disabled.
        return (verifier_enabled is True and graph.get("estado") == "activo"
                and all(n.estado == G.HECHO for n in nodes))
    if isinstance(record, dict) and record.get("pending") is False:
        if verification_confirmation_matches(graph, record):
            return False
        if not _new_pass_after_closure(graph, record):
            return False
        if (("closure_correction" in record
                or _attempts_advanced(graph, record.get("input_attempts"))) and verifier_enabled is True
                and graph.get("estado") == "activo"):
            return True  # a provenance-backed executed failed pass also needs closure.
    if (isinstance(record, dict) and verifier_enabled is True
            and graph.get("estado") == "activo" and all(n.estado == G.HECHO for n in nodes)
            and type(record.get("verification_version")) is int
            and record.get("verification_version") == 1 and record.get("pending") is False
            and isinstance(record.get("input_signature"), str)
            and re.fullmatch(r"[0-9a-f]{64}", record["input_signature"])
            and record["input_signature"] != verification_signature(graph)):
        # A new completed correction pass may be interrupted before intent.
        # Its prior confirmed verdict is not confirmation of these inputs.
        return True
    return (isinstance(record, dict) and type(record.get("verification_version")) is int
            and record.get("verification_version") == 1
            and record.get("pending") is True
            and bool(signature := verification_signature(graph))
            and record.get("input_signature") == signature)


def metadata_close_pending(graph: dict, *, verifier_enabled) -> bool:
    """An active completed plan can finish without a verifier when disabled."""
    nodes = _closed_nodes(graph)
    return (verifier_enabled is False and graph.get("estado") == "activo"
            and bool(nodes) and all(n.estado == G.HECHO for n in nodes))


def _verification_intent(graph, previous):
    intent = {"verification_version": 1, "pending": True,
              "input_signature": verification_signature(graph), "started_at": _ahora()}
    result = previous.get("previous_result") if previous.get("pending") is True else previous
    if isinstance(result, dict) and result and result.get("pending") is not True:
        intent["previous_result"] = {k: v for k, v in result.items() if k != "previous_result"}
    if isinstance(previous.get("recovery"), dict):
        intent["recovery"] = previous["recovery"]
    return intent


def verification_interrupt_intent(graph: dict):
    """For a proven interrupted live worker, fill only its before-intent gap."""
    if not _closed_nodes(graph) or not (signature := verification_signature(graph)):
        return None
    record = _verification_record(graph)
    if record is False:
        return None
    if verification_confirmation_matches(graph, record):
        return None
    if isinstance(record, dict) and not _new_pass_after_closure(graph, record):
        return None
    if (isinstance(record, dict) and type(record.get("verification_version")) is int
            and record.get("verification_version") == 1
            and record.get("input_signature") == signature
            and type(record.get("pending")) is bool):
        return None  # current pending already exists OR current result is confirmed.
    return _verification_intent(graph, record or {})


def verification_close_state(graph: dict):
    """A closure-only needs_more is visible as failed without inventing a task."""
    record = _verification_record(graph)
    if (graph.get("estado") == "fallado" and isinstance(record, dict)
            and type(record.get("verification_version")) is int
            and record.get("verification_version") == 1 and record.get("pending") is False
            and record.get("close_only") is True and record.get("verdict") == "needs_more"
            and bool(signature := verification_signature(graph))
            and record.get("input_signature") == signature):
        return "fallado"

# =====================================================================
# La verificación de cierre: una por pasada del grafo
# =====================================================================
#
# La aceptación de cada nodo no sustituye la comprobación del objetivo
# completo: el cierre revisa la integración y los criterios transversales.
#
# `needs_more` conserva la corrección estructurada: el scheduler puede
# continuar los criterios pendientes dentro de la ejecución autorizada.
# El arranque del servidor sigue sin relanzar grafos por su cuenta.

# Tope del resumen de nodos que viaja al verificador, en caracteres. Un
# grafo tiene decenas de nodos y este turno tiene que ser barato: las
# transcripciones enteras son cientos de miles de caracteres.
#
# 3000 y no más porque `experts._run_verifier` recorta el contenido a
# head=1200 / tail=1800: pasarse de ahí no compra contexto, solo hace que
# el recorte se coma los nodos del MEDIO en silencio. Recortando acá, los
# que no entran se nombran (id + estado) en vez de desaparecer.
TOPE_RESUMEN_CHARS = 3000
TOPE_POR_NODO_CHARS = 200


def _tareas_ordenadas(g: dict) -> list[dict]:
    return sorted(g.get("tasks") or [], key=lambda x: (x["orden"], x["id"]))


def _plan_del_grafo(g: dict) -> str:
    """Los nodos como plan numerado: el plan del grafo ES el grafo.

    Va con id y dependencias porque eso es lo que deja al verificador
    decir "el nodo que faltaba era el que dependía de t3" en vez de una
    objeción genérica.
    """
    lineas = []
    for i, t in enumerate(_tareas_ordenadas(g), 1):
        deps = ", ".join(t.get("deps") or ())
        lineas.append(
            f"{i}. [{t['id']}] {(t.get('titulo') or '').strip()}"
            + (f" (depende de: {deps})" if deps else ""))
    return "\n".join(lineas)


def _resumen_de_nodos(g: dict) -> str:
    """Qué produjo cada nodo, recortado. Ver `TOPE_RESUMEN_CHARS`.

    Se manda el `error` cuando lo hay y el `resultado` cuando no: el
    error es lo que el verificador necesita para separar "falló y por eso
    falta" de "cerró bien pero no hizo lo que se pidió".
    """
    lineas: list[str] = []
    usado = 0
    sin_detalle: list[str] = []
    sustituidos = G.sustituidos([
        G.Nodo.desde_fila(t, t.get("deps") or ()) for t in g.get("tasks") or []])
    # El cierre más reciente contiene las recuperaciones. `orden` mezcla
    # padres e hijos de autosplit y dejaba fuera justo la comprobación final.
    recientes = sorted(_tareas_ordenadas(g),
                       key=lambda t: t.get("ended_at") or "", reverse=True)
    for t in recientes:
        error = (t.get("error") or "").strip()
        cuerpo = error or (t.get("resultado") or "").strip()
        if len(cuerpo) > TOPE_POR_NODO_CHARS:
            cuerpo = cuerpo[:TOPE_POR_NODO_CHARS] + "…"
        fecha = f" ({t['ended_at']})" if t.get("ended_at") else ""
        estado = (f"subdividido; historial: {t.get('estado')}"
                  if t["id"] in sustituidos else t.get("estado"))
        linea = (f"- [{estado}]{fecha} "
                 f"{(t.get('titulo') or t['id']).strip()[:80]}"
                 + (f" — {'error' if error else 'resultado'}: {cuerpo}"
                    if cuerpo else ""))
        if lineas and usado + len(linea) > TOPE_RESUMEN_CHARS:
            sin_detalle.append(f"{t['id']} [{estado}]")
            continue
        lineas.append(linea)
        usado += len(linea) + 1
    if sin_detalle:
        # Nombrarlos y no omitirlos: un verificador que no sabe que está
        # viendo una muestra juzga "faltó la mitad del plan" sobre un
        # recorte nuestro. Mismo criterio que `_render_tool_calls`.
        lineas.append(
            f"- (tope de {TOPE_RESUMEN_CHARS} caracteres: estos nodos van "
            f"sin detalle) " + "; ".join(sin_detalle[:60]))
    return "\n".join(lineas)


#: El exit code que el harness pega al final de la salida de `shell`.
_RE_EXIT = re.compile(r"^\(exit=(-?\d+)\)\s*$", re.MULTILINE)
_RE_CHECK = re.compile(r"\b(test|pytest|newman|build|check|validate)\b", re.I)
_RE_CHECK_OUTPUT = re.compile(
    r"^.*(?:Passed!|Correctas!|Passed:|Correctas:|Total tests:|"
    r"requests\s*[=:│]|assertions\s*[=:│]|failures\s*[=:]).*$", re.I | re.M)


def _comandos_con_resultado(eventos: list) -> tuple[list[dict], int]:
    """Correlaciona por id; un lote histórico sin ids no se puede atribuir."""
    pendientes: list[dict] = []
    comandos: list[dict] = []
    omitidos = 0
    for ev in eventos:
        if not isinstance(ev, dict):
            continue
        if ev.get("phase") == "tool_call" and ev.get("cmd"):
            actual = {**ev, "ambiguo": False}
            mismos = [p for p in pendientes if p.get("tool") == ev.get("tool")]
            if mismos:
                for p in mismos + [actual]:
                    p["ambiguo"] = True
            pendientes.append(actual)
        elif ev.get("phase") == "tool_result":
            call_id = ev.get("tool_call_id")
            candidatos = [p for p in pendientes
                          if (p.get("tool_call_id") == call_id if call_id
                              else p.get("tool") == ev.get("tool"))]
            if not candidatos:
                continue
            actual = candidatos[0]
            # Consumir TAMBIÉN background/sin exit. Dejarlo pendiente pegaba
            # todos los códigos siguientes al comando anterior (IST, 10/9).
            pendientes.remove(actual)
            if len(candidatos) != 1 or (not call_id and actual["ambiguo"]):
                omitidos += 1
                for p in candidatos[1:]:
                    p["ambiguo"] = True
                continue
            salida = str(ev.get("output") or "")
            codigos = _RE_EXIT.findall(salida)
            resumen = " ".join(" ".join(
                _RE_CHECK_OUTPUT.findall(salida)).split())[:160]
            comandos.append({
                "cmd": str(actual["cmd"]).splitlines()[0],
                "ts": ev.get("ts") or actual.get("ts") or "",
                "exit": int(codigos[-1]) if codigos else None,
                "resumen": resumen,
            })
    return comandos, omitidos + len(pendientes)


async def _evidencia_de_los_nodos(db, g: dict) -> str:
    """Bitácora sintética con lo que de verdad hicieron los nodos.

    El verificador del grafo recibía `tool_calls_summary: []`, y el
    comentario de al lado lo admitía: "más honesto que inventar un
    resumen agregado que nadie midió". Pero el rastro existe y está
    medido — cada nodo dejó en `chats.progress_events` el comando en su
    `tool_call` y el exit code al final del `tool_result`.

    Reusa `Bitacora`, con muestra por nodo y fechas. El cierre permite
    4000 caracteres de evidencia y señala los recortes; un lote sin ids
    se omite si no permite asociar cada salida a su comando sin adivinar.

    Nunca lanza: sin evidencia se verifica peor, no se rompe el cierre.
    """
    from .experts import Bitacora

    bit = Bitacora()
    por_nodo: list[list[dict]] = []
    total = sin_correlacion = 0
    for t in g.get("tasks") or []:
        chat_id = t.get("chat_id")
        if not chat_id:
            continue
        try:
            fila = await db.get_chat(chat_id)
            eventos = json.loads((fila or {}).get("progress_events") or "[]")
        except Exception:  # noqa: BLE001
            continue
        comandos, omitidos = _comandos_con_resultado(eventos)
        sin_correlacion += omitidos
        total += len(comandos)
        for cmd in comandos:
            cmd["nodo"] = t["id"]
        relevantes = [cmd for cmd in comandos
                      if cmd["resumen"] or _RE_CHECK.search(cmd["cmd"])]
        # ponytail: dos comprobaciones recientes por nodo; evidencia
        # estructurada por alcance reemplaza esta heurística si no alcanza.
        muestra = (relevantes or comandos)[-2:]
        if muestra:
            por_nodo.append(list(reversed(muestra)))
    por_nodo.sort(key=lambda cs: cs[0]["ts"], reverse=True)
    # Una plaza por nodo antes de darle una segunda al mismo: un run largo
    # no desaloja las comprobaciones de todos sus hermanos.
    seleccion = [cs[0] for cs in por_nodo][:Bitacora.MAX_COMANDOS - 1]
    seleccion += [cs[1] for cs in por_nodo if len(cs) > 1][
        :Bitacora.MAX_COMANDOS - 1 - len(seleccion)]
    seleccion.sort(key=lambda cmd: cmd["ts"], reverse=True)
    if total or sin_correlacion:
        bit.comandos.append(
            f"Muestra por nodo, reciente primero: {total - len(seleccion)} "
            f"comandos omitidos; {sin_correlacion} resultados sin correlación "
            "segura. Una omisión no prueba éxito ni fallo.")
    for cmd in seleccion:
        codigo = (f"exit={cmd['exit']}" if cmd["exit"] is not None
                  else "sin código de salida; disponibilidad sin verificar")
        resumen = f" | {cmd['resumen']}" if cmd["resumen"] else ""
        bit.comandos.append(
            f"[{cmd['nodo']} {cmd['ts'] or 'sin fecha'}] "
            f"$ {' '.join(cmd['cmd'].split())[:120]} → {codigo}{resumen}")
    return bit.volcar()


async def _verificar_al_cerrar(db, graph_id: str, g: dict, prog: dict,
                               verificar, *, allow_continue: bool = True,
                               close_only: bool = False) -> bool:
    """Guarda la verificación con su corrección; indica si puede continuar.

    NUNCA lanza por un fallo del verificador o de persistencia.

    Un error del verificador se registra sin invalidar los nodos terminados.
    Una interrupción antes de guardar el resultado deja el grafo activo para
    que Retomar recupere la verificación sin repetir esos nodos.
    """
    signature = verification_signature(g)
    if not signature:
        return False  # Unknown provenance is not permission to query a model.
    payload: dict = {"at": _ahora(), "estado_grafo": prog.get("estado") or "",
                     "verification_version": 1, "pending": False,
                     "input_signature": signature, "close_only": close_only,
                     "input_attempts": {t["id"]: int(t.get("intentos") or 0) for t in g["tasks"]},
                     "verdict": "", "feedback": "", "modelo": "", "error": ""}
    previous = {}
    try:
        previous = json.loads(g.get("verificacion_json") or "{}")
        if not isinstance(previous, dict):
            return False  # preserve malformed legacy data; do not infer a new call.
        if isinstance(previous.get("recovery"), dict):
            # Conservar el progreso acreditado entre pasadas y reinicios.
            payload["recovery"] = previous["recovery"]
    except (TypeError, ValueError, AttributeError):
        return False
    intent = _verification_intent(g, previous)
    # Persist intent before even collecting evidence or entering the model.
    # A newer cancellation is never reactivated by this metadata write.
    try:
        admitted = await db.run(
            "UPDATE task_graphs SET verificacion_json=? WHERE id=? AND estado<>'cancelado' RETURNING id",
            (json.dumps(intent, ensure_ascii=False), graph_id))
    except Exception:
        logger.exception("grafo %s: no pude registrar intención de verificación", graph_id)
        return False
    if not admitted:
        return False
    try:
        evidence = await _evidencia_de_los_nodos(db, g)
        if close_only:
            from . import task_service, user_accounts
            cid = g.get("conversation_id")
            state = await db.get_conversation_task(cid) if cid else {}
            bound = user_accounts.current_actor.get()
            if bound and cid:
                from . import conversation_access
                if bound[0].db is not db or not (await conversation_access.get_access(db, cid, bound[1]))["can_control"]:
                    return False
            if g.get("project_slug"):
                actual = await db.get_project(g["project_slug"])
                if (not actual or not actual.get("enabled")
                        or not (actual.get("defaults_json") or {}).get("graph_verifier", True)):
                    return False
            if (state.get("state") in task_service.STOPPED
                    or graph_id in getattr(db, "_graph_cancellations", {})
                    or cid in getattr(db, "_task_cancellations", {})):
                return False
        res = await verificar(
            user=g.get("objetivo") or "",
            plan=_plan_del_grafo(g),
            executor_result={
                "graph_id": graph_id,
                "phase_at_end": prog.get("estado") or "",
                "content": _resumen_de_nodos(g),
                # El grafo no lleva un registro de tool calls propio: las
                # de cada nodo viven en su chat. `_render_tool_calls`
                # imprime "(sin llamadas a herramientas)" y el prompt lo
                # dice tal cual, que es más honesto que inventar un
                # resumen agregado que nadie midió.
                "tool_calls_summary": [],
                # La evidencia SÍ se puede medir, y desde el 9/9/2026 se
                # mide: los comandos de cada nodo con su exit code real,
                # sacados de `progress_events`. Viaja como bitácora para
                # que el verificador del grafo lea el mismo formato que
                # el del turno. Ver `_evidencia_de_los_nodos`.
                "bitacora_json": evidence,
            })
        res = res if isinstance(res, dict) else {}
        payload["verdict"] = str(res.get("verdict") or "")
        payload["feedback"] = str(res.get("feedback") or "")[:2000]
        payload["modelo"] = str(res.get("modelo") or "")
        payload["error"] = str(res.get("error") or "")[:200]
        # Tokens de la etapa, planos, con la misma regla que
        # `chats.stages_json`: la clave solo existe si se midió, así
        # que ausente = "sin dato" y no cero.
        usage = res.get("usage") or {}
        if usage.get("plan_correction"):
            payload["plan_correction"] = usage["plan_correction"]
        if usage:
            payload["tokens_in"] = usage.get("tokens_in", 0)
            payload["tokens_out"] = usage.get("tokens_out", 0)
        logger.info("grafo %s: veredicto %s (%s)", graph_id,
                    payload["verdict"] or "(vacío)", payload["modelo"])
    except Exception as e:  # noqa: BLE001 — telemetría, no el trabajo
        logger.exception("grafo %s: la verificación de cierre reventó (%r); "
                         "el grafo cierra igual", graph_id, e)
        payload["error"] = f"{type(e).__name__}: {e}"[:200]
    async def persist_result():
        from .orchestrator_recovery import continue_verification
        continued = None if close_only else await continue_verification(
            db, graph_id, g, payload, allow_continue=allow_continue)
        if continued is not None:
            current = await db.get_task_graph(graph_id)
            if current and current.get("estado") == "cancelado":
                # Guarded correction SQL can skip a newer cancellation and
                # return False without saving. The verifier still returned a
                # known result: confirm it instead of leaving intent pending.
                continued = None
        if continued is None:
            terminal = ("fallado" if close_only and payload["verdict"] == "needs_more"
                        else prog["estado"])
            await db.run(
                "UPDATE task_graphs SET verificacion_json=?, "
                "estado=CASE WHEN estado='cancelado' THEN estado ELSE ? END WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), terminal, graph_id))
        return bool(continued)

    # Once the result is known, cancellation cannot turn it back into a
    # pending call. Settle persistence before propagating, preserving usage.
    closing = asyncio.create_task(persist_result())
    cancelled = False
    while not closing.done():
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    try:
        continued = closing.result()
    except Exception as exc:  # storage unavailable; do not claim confirmation.
        if cancelled:
            raise asyncio.CancelledError from exc
        logger.exception("grafo %s: no pude guardar el veredicto y su corrección", graph_id)
        return False
    if cancelled:
        raise asyncio.CancelledError
    return continued


async def _seguro(fn, *a):
    """Llama a un callback sin que su error se lleve puesto el grafo."""
    try:
        return await fn(*a)
    except Exception:  # noqa: BLE001
        logger.exception("callback del orquestador falló; sigo")
        return None


def _ahora() -> str:
    from .db import now_iso
    return now_iso()


# =====================================================================
# El cableado real: MiniMax ejecuta cada nodo, nemotron mira los fallos
# =====================================================================
#
# Todo lo de arriba recibe `ejecutar` y `coordinar` inyectados para poder
# probarse sin modelo. Acá abajo están las implementaciones de verdad,
# que son lo único de este archivo que habla con pydantic-ai.
