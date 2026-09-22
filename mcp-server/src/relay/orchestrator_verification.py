"""Evidence collection and close verification for task graphs."""
from __future__ import annotations

import json
import logging
import re

from . import grafo as G

logger = logging.getLogger("relay.orquestador")

# =====================================================================
# La verificación de cierre: UNA por grafo
# =====================================================================
#
# Los chats pasan por `run_expert_staged` —planificador, ejecutor,
# verificador— y el grafo llama a `run_expert` pelado, un nodo a la vez.
# El motivo de saltear el PLANIFICADOR por nodo sigue en pie: el plan ya
# existe, es el grafo, y volver a derivarlo por nodo sería pagar por una
# decisión ya tomada.
#
# El verificador se había arrastrado en la misma decisión y ahí el
# argumento no aplica: verificar no es re-derivar el plan, es mirar si lo
# que salió se parece a lo que se pidió. La tarea que MÁS se puede
# desviar —la grande, la de decenas de nodos— corría sin nadie mirando:
# seis grafos muertos el 2026-09-06/07 se llevaron el 50 % de los tokens
# del día sin entregar nada.
#
# Por qué UNA y no una por nodo: por nodo es exactamente el costo que la
# decisión original evitaba (doblar el precio del grafo para revisar el
# 90 % de tareas que salieron bien). Una al cerrar cuesta un turno corto
# por grafo entero.
#
# Lo que esta etapa NO hace: relanzar. Aunque el veredicto sea
# `needs_more`, el grafo no se reanuda solo — mismo criterio que los
# grafos a medias en el boot del server: reparar es una cosa y arrancar
# trabajo que nadie pidió es otra. El humano decide, con el veredicto a
# la vista.

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
            # todos los códigos siguientes al comando anterior (AuroraDemo, fecha de ejemplo).
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
                               verificar) -> None:
    """Corre la verificación del grafo y la guarda. NUNCA lanza.

    El grafo ya terminó su trabajo: que esta etapa falle es una falla de
    TELEMETRÍA, no del trabajo. Mismo criterio que el `try/except` que
    envuelve a `sanar` en el boot del server — se registra que falló y el
    grafo cierra como habría cerrado. Por eso el estado ya está fijado
    antes de llegar acá.
    """
    payload: dict = {"at": _ahora(), "estado_grafo": prog.get("estado") or "",
                     "verdict": "", "feedback": "", "modelo": "", "error": ""}
    try:
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
                "bitacora_json": await _evidencia_de_los_nodos(db, g),
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
        if usage:
            payload["tokens_in"] = usage.get("tokens_in", 0)
            payload["tokens_out"] = usage.get("tokens_out", 0)
        logger.info("grafo %s: veredicto %s (%s)", graph_id,
                    payload["verdict"] or "(vacío)", payload["modelo"])
    except Exception as e:  # noqa: BLE001 — telemetría, no el trabajo
        logger.exception("grafo %s: la verificación de cierre reventó (%r); "
                         "el grafo cierra igual", graph_id, e)
        payload["error"] = f"{type(e).__name__}: {e}"[:200]
    try:
        await db.set_task_graph_verificacion(
            graph_id, json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — ídem: no puede voltear el cierre
        logger.exception("grafo %s: no pude guardar el veredicto", graph_id)


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
