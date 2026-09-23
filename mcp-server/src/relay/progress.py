"""Progreso en memoria y notificaciones del experto al panel y al bot."""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time
from typing import Any

logger = logging.getLogger("relay.experts")

# ---------- Estado consultable del run y progreso enviado al bot ----------
#
# Diseñados para "pregunta y te digo" (sin heartbeat periódico):
# - server.py mantiene un dict AppKey[PROGRESS_KEY][chat_id] = RunProgress
# - el iter() de run_expert actualiza ese dict vía on_progress
# - GET /experts/status/{chat_id} lo serializa (o 404 si ya terminó)
#
# El dataclass vive en memoria del proceso. Si el relay reinicia, se
# pierde — el caller lo documenta en el response ("lost_on_restart": true).

# Fase 3b (2026-07-20f): tope de pasos guardados en el snapshot. El web
# pollea /experts/status y renderea los nuevos; guardar TODOS inflaría el
# payload en runs de 200 tool calls. Los últimos N alcanzan: el web ya
# renderizó los viejos, y al terminar loadMessages() trae el persistido
# completo. Cada diff ya viene capeado por _format_tool_step (~1.6KB).
STEPS_KEEP = 30

# Tope del texto de narración por paso (el "por qué" que dice el modelo
# antes de cada tool). Un modelo verborrágico puede tirar párrafos; el
# snapshot lo pollea la UI cada 1.5s y no queremos pagar eso.
#
# 700 → 1800 (2026-08-01): con 700 se cortaban las tablas de opciones a
# la mitad. Caso real (website-demo): el modelo ofreció A/B/C/D en una tabla
# de 1086 chars, el usuario vio hasta la fila A, y el mensaje final le
# decía "decime A, B o C de la tabla" — una tabla que nunca vio entera.
# En ese run 5 de 30 narraciones tocaron el tope. Peor caso del snapshot
# con STEPS_KEEP=30: ~54KB, y solo si el modelo escribe párrafos largos.
SAY_MAX_CHARS = 1800

#: Marca del corte. Sin esto el recorte es invisible y parece que el
#: modelo escribe frases truncadas (así se manifestó el bug de arriba).
SAY_CLIP_MARK = "… (recortado)"


def _clip_say(text: str) -> str:
    """Recorta la narración al tope dejando marca visible del corte."""
    if len(text) <= SAY_MAX_CHARS:
        return text
    return text[:SAY_MAX_CHARS] + SAY_CLIP_MARK


@dataclass
class RunProgress:
    chat_id: str
    target: str
    started_at: float            # time.monotonic()
    last_activity_at: float      # time.monotonic(), refrescado por nodo
    phase: str                   # "thinking" | "tool_call" | "writing"
    last_tool: str | None
    tool_calls: int
    tokens_in: int | None
    tokens_out: int | None
    model: str
    error: str | None = None
    graph_id: str = ""           # nodo de grafo, para no contar también al padre
    finished: bool = False       # True cuando el run terminó (para que
                                 # /status siga respondiendo post-mortem)
    # Pasos ricos del run (Fase 3b): mismos datos que van al embed de
    # Discord (línea legible + diff de edit_file). El web los renderea
    # en vivo como las MISMAS tarjetas que quedan al persistir.
    steps: list[dict] = field(default_factory=list)
    # `n` de los steps: contador propio y monótono. Antes era rp.tool_calls,
    # que dejó de servir cuando empezamos a intercalar pasos de narración
    # (dos pasos con el mismo n ⇒ la UI, que deduplica con n > lastStepN,
    # se comía el segundo).
    seq: int = 0
    # Cola de correcciones del humano (2026-07-25). La escribe
    # POST /experts/steer/{chat_id}; la consume run_expert en el próximo
    # borde de nodo. Vive acá porque el store ya está indexado por
    # chat_id y el endpoint ya lo tiene a mano — cero plumbing nuevo.
    steer: list[str] = field(default_factory=list)

    def snapshot(self, *, now: float | None = None) -> dict:
        """Serializa para el endpoint /experts/status."""
        n = now if now is not None else time.monotonic()
        elapsed_s = max(0.0, n - self.started_at)
        idle_s = max(0.0, n - self.last_activity_at)
        return {
            "chat_id": self.chat_id,
            "target": self.target,
            "elapsed_s": round(elapsed_s, 2),
            "idle_s": round(idle_s, 2),
            "phase": self.phase,
            "last_tool": self.last_tool,
            "tool_calls": self.tool_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "model": self.model,
            "error": self.error,
            "finished": self.finished,
            **({"graph_id": self.graph_id} if self.graph_id else {}),
            # Fase 3b: el web appendea los steps con n > lastStepN.
            "steps": self.steps,
        }


def make_progress_callback(
    store: dict[str, "RunProgress"], notify: Any | None,
    chat_id: str, target: str, model: str,
) -> Any:
    """Devuelve un callable async que actualiza store + emite notify.

    server.py lo pasa como `on_progress=` a run_expert. El store es
    el AppKey[PROGRESS_KEY] (dict compartido). El notify es el
    NotifyClient del relay (puede ser None en tests).
    """
    rp = RunProgress(
        chat_id=chat_id, target=target,
        started_at=time.monotonic(),
        last_activity_at=time.monotonic(),
        phase="thinking", last_tool=None, tool_calls=0,
        tokens_in=None, tokens_out=None, model=model,
    )
    store[chat_id] = rp
    # NotifyClient coalesce en segundo plano; los doubles/callers antiguos
    # conservan el contrato send. El panel nunca espera la conexión al bot.
    send_progress = (getattr(notify, "queue_progress", notify.send)
                     if notify is not None else None)

    def _push_step(kind: str, msg: str, *, tool: str | None = None,
                   diff: str | None = None, cmd: str | None = None,
                   tool_call_id: str | None = None) -> None:
        rp.seq += 1
        paso = {
            "n": rp.seq, "kind": kind, "tool": tool,
            "message": msg, "diff": diff or None,
        }
        # `cmd` y `output` solo cuando los hay: esta lista viaja entera en
        # CADA poll de /experts/status (1.5s), así que las claves nulas de
        # los pasos que no son shell son peso puro.
        if cmd:
            paso["cmd"] = cmd
        if tool_call_id:
            paso["tool_call_id"] = tool_call_id
        rp.steps.append(paso)
        if len(rp.steps) > STEPS_KEEP:
            del rp.steps[:-STEPS_KEEP]

    def _attach_output(tool: str | None, output: str,
                       tool_call_id: str | None = None) -> None:
        """Cuelga la salida de la terminal del paso que la disparó.

        Usa el ID de llamada cuando existe. Para eventos legacy sin ID,
        busca el paso más viejo de esa tool que todavía no tiene salida.

        2026-09-04, medido: pydantic-ai entrega los `ToolReturnPart` en
        el ORDEN en que se pidieron las tools. Esto buscaba de atrás
        para adelante —el comentario viejo afirmaba lo contrario, que el
        orden de los returns no era el de las calls— y con dos llamadas
        a la misma tool en un turno colgaba el primer resultado del
        último paso: las salidas salían cruzadas. No se notaba porque el
        turno dejaba un solo paso y los resultados de más se perdían.

        Si no aparece (el paso ya se cayó del tope de STEPS_KEEP), la
        salida se descarta en silencio: es decoración de un paso que la
        UI ya no muestra.

        La UI deduplica por `n > lastStepN`, así que un paso mutado
        DESPUÉS de haberse enviado no se repinta en vivo — se ve al
        recargar el hilo, que es cuando el humano lo va a mirar en
        serio. ponytail: si molesta, hace falta versionar el paso.
        """
        for step in rp.steps:
            if (step.get("kind") == "tool_call"
                    and step.get("tool") == tool
                    and (not tool_call_id or step.get("tool_call_id") == tool_call_id)
                    and not step.get("output")):
                step["output"] = output
                return

    async def _cb(*, phase: str, tool: str | None, tool_calls: int | None = None,
                  message: str | None = None, diff: str | None = None,
                  cmd: str | None = None, output: str | None = None,
                  tool_call_id: str | None = None) -> None:
        if phase == "tool_result":
            # No es una fase del run: es el resultado del paso anterior.
            # No pisa rp.phase ni notifica al bot (que ya recibe el
            # timeline por `message`).
            if output:
                _attach_output(tool, output, tool_call_id)
            return
        if phase in ("say", "steer") and message:
            # El "por qué" del modelo y las correcciones del humano: van al
            # timeline vivo pero NO pisan rp.phase (la fase real la marca la
            # tool o el writing que viene atrás) ni notifican al bot, que
            # solo entiende pasos de tool.
            rp.last_activity_at = time.monotonic()
            _push_step(phase, message)
            return
        if phase == "heartbeat":
            # Latido del watchdog (2026-07-20b): el run sigue vivo pero
            # el modelo no emitió nodes (contexto largo ⇒ respuestas
            # lentas). NO refresca last_activity_at (mentiría el idle_s
            # de /status) ni pisa la fase real; solo avisa al bot/UI
            # para que el hilo de Discord/web no parezca muerto.
            if notify is not None:
                elapsed = round(time.monotonic() - rp.started_at)
                try:
                    await send_progress(
                        agent_id=f"chat:{chat_id}",
                        kind="progress",
                        message=(
                            f"⏳ sigue trabajando ({rp.phase}, "
                            f"{rp.tool_calls} tools, {elapsed}s)"),
                        metadata={
                            "chat_id": chat_id,
                            "target": target,
                            "phase": rp.phase,
                            "heartbeat": True,
                            "tool": rp.last_tool,
                            "tool_calls": rp.tool_calls,
                            "elapsed_s": elapsed,
                        },
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("notify heartbeat falló: %r", e)
            return
        rp.phase = phase
        rp.last_activity_at = time.monotonic()
        if tool is not None:
            rp.last_tool = tool
        if tool_calls is not None:
            rp.tool_calls = tool_calls
        # Notify best-effort al bot (Fase 1 B): un "progress" por tool
        # call. El ModelResponseNode que no pidió tools NO notifica
        # (evita spam — solo el LLM pensando). `message` es la línea
        # legible que arma run_expert (📄 leyó `x`, ✏️ editó `y`…); si
        # no vino, caemos al genérico. `diff` (solo edit_file) va en
        # metadata para que el bot lo postee aparte del timeline.
        # Fase 3b: guardar el paso rico en el snapshot ANTES del notify —
        # el web lo lee por /experts/status aunque el bot de Discord esté
        # caído (notify es best-effort y puede fallar).
        if phase == "tool_call" and tool:
            _push_step("tool_call", message or f"🔧 {tool}", tool=tool,
                       diff=diff, cmd=cmd, tool_call_id=tool_call_id)

        if notify is not None and phase == "tool_call" and tool:
            meta = {
                "chat_id": chat_id,
                "target": target,
                "phase": phase,
                "tool": tool,
                "tool_calls": rp.tool_calls,
                "elapsed_s": round(time.monotonic() - rp.started_at, 2),
            }
            if diff:
                meta["diff"] = diff
            try:
                await send_progress(
                    agent_id=f"chat:{chat_id}",
                    kind="progress",
                    message=message or f"🔧 {tool}",
                    metadata=meta,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("notify progress falló: %r", e)

    # Compartir la cola real: /experts/steer agrega mensajes durante el run.
    _cb.steer = rp.steer
    return _cb
