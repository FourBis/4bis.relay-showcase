"""Recorte del historial, imágenes y reparación de llamadas sin respuesta."""
from __future__ import annotations
import dataclasses
import logging
import os
import re
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from typing import Any, Optional
from . import expert_context

logger = logging.getLogger("relay.experts")


TOOL_RESULT_CAP = int(os.environ.get("FOURBIS_TOOL_RESULT_CAP", "16000"))
# Tools cuyo resultado es salida de consola: builds, tests, git. Tienen su
# propio cap —más grande— y se les conserva el final (2026-08-16). El
# nombre `run_shell` es el del wrapper 4bis; los otros dos son alias que
# aparecen según el MCP que esté adjunto.
_CONSOLE_TOOLS = frozenset({"run_shell", "shell", "run_command"})
SHELL_RESULT_CAP = int(os.environ.get("FOURBIS_SHELL_RESULT_CAP", "48000"))
SHELL_RESULT_KEEP_TAIL = int(
    os.environ.get("FOURBIS_SHELL_RESULT_KEEP_TAIL", "12000"))
TOOL_KEEP_FULL = int(os.environ.get("FOURBIS_TOOL_KEEP_FULL", "8"))
# Thinking: ventana MUCHO más corta que la de los results. El
# razonamiento de hace 20 tool calls no le sirve al modelo — pero se
# re-manda igual (pydantic-ai lo mapea de vuelta a `reasoning_content`),
# y medido sobre un run real de kimi-k3 era el 50% del gasto: 136
# partes, 217KB, re-enviadas 206 veces.
THINK_KEEP_FULL = int(os.environ.get("FOURBIS_THINK_KEEP_FULL", "2"))
_ELIDE_MIN_CHARS = 600   # results más chicos que esto no vale la pena elidir
_ELIDE_KEEP_HEAD = 200   # chars que sobreviven del result elidido
_ELIDE_KEEP_TAIL = 80    # …más la última línea si entra acá: `(exit=1)`
_ELIDE_MARK = "[…result viejo elidido para ahorrar contexto; "\
    "re-ejecuta la tool si lo necesitas]"
# Args de una tool call YA respondida: el return dice si funcionó, así
# que el payload (write_file manda el archivo entero) es peso muerto.
# Se deja JSON válido: los providers OpenAI-compat parsean `arguments`.
_ELIDED_ARGS = '{"_elided": "args de una call vieja ya respondida"}'


# Rescate de errores del tramo elidido (2026-09-04). Al truncar la salida
# de consola se conservan cabeza y cola; el error de compilación vive en el
# MEDIO y se perdía. Lo sufre sobre todo `_STEP_OUT_MAX` (2400), que es lo
# que queda en `chats.progress_events`: el único rastro post-mortem de lo
# que dijo la terminal. Los topes son duros a propósito —`error` matchea
# ruido tipo `0 Error(s)`— y el bloque se DESCUENTA de la cabeza: el texto
# final mide lo mismo que antes, ningún cap sube.
_ERR_RE = re.compile(r"\b(error|fail(ed|ure)?|exception|FAILED)\b", re.I)
_ERR_KEEP_LINES = 20
_ERR_KEEP_CHARS = 1200
_ERR_HEAD_MIN = 200      # la cabeza nunca baja de acá; si no entra, no hay rescate


def _rescatar_errores(medio: str, budget: int) -> str:
    """Líneas con pinta de error del tramo elidido, deduplicadas.

    El dedupe no es cosmético: MSBuild repite el MISMO `error CSxxxx` una
    vez por target framework, y sin dedupe un build con 300 errores gasta
    el presupuesto entero en la misma línea repetida.
    """
    vistas: set[str] = set()
    lineas: list[str] = []
    usado = 0
    for cruda in medio.splitlines():
        linea = cruda.strip()
        if not linea or linea in vistas or not _ERR_RE.search(linea):
            continue
        vistas.add(linea)
        if len(lineas) >= _ERR_KEEP_LINES or usado + len(linea) + 1 > budget:
            break
        lineas.append(linea)
        usado += len(linea) + 1
    if not lineas:
        return ""
    return (f"\n…[{len(lineas)} líneas con error, rescatadas del medio]…\n"
            + "\n".join(lineas) + "\n")


def _cap_text(s: str, cap: int = TOOL_RESULT_CAP, *, keep_tail: int = 0) -> str:
    """Acota un texto a `cap` chars.

    `keep_tail` (2026-08-16): reserva ese pedazo para el FINAL del texto.
    Nació de `run_shell`: el cap se quedaba con la cabeza, y en la salida
    de un build o una suite de tests la cabeza es el banner de la
    herramienta — el veredicto (`exit=1`, el traceback, "3 failed") vive
    al final. El experto veía 16.000 chars de compilación y ni una línea
    del error, así que respondía "compiló" sobre un build roto o
    reintentaba a ciegas. Es la misma lógica que `_elide_tail` ya aplicaba
    del lado de la elisión.
    """
    if len(s) <= cap:
        return s
    if keep_tail > 0 and keep_tail < cap:
        # El bloque de errores sale del presupuesto de la CABEZA, así que
        # achicarla elide líneas nuevas que también hay que escanear: dos
        # pasadas alcanzan (`len(bloque)` está topeado, no diverge).
        budget = min(_ERR_KEEP_CHARS, cap - keep_tail - _ERR_HEAD_MIN)
        bloque = ""
        if budget > 0:
            for _ in range(2):
                bloque = _rescatar_errores(
                    s[cap - keep_tail - len(bloque):-keep_tail], budget)
        head = cap - keep_tail - len(bloque)
        return (
            s[:head]
            + bloque
            + f"\n…[TRUNCADO: {len(s) - head - keep_tail} chars del medio. "
            f"El resultado "
            f"completo tenía {len(s)} chars; abajo va el FINAL, que es "
            "donde suele estar el veredicto (exit code, error, resumen).]…\n"
            + s[-keep_tail:])
    return (
        s[:cap]
        + f"\n…[TRUNCADO: el resultado completo tenía {len(s)} chars. "
        "Refina la consulta (limit/offset, file_pattern, path más "
        "específico) para ver el resto.]")


def _caps_for(tool_name: str) -> tuple[int, int]:
    """`(cap, keep_tail)` para una tool. Default: el cap global, sin cola.

    Un cap único para todas las tools trataba igual a un `read_file` y a
    un `dotnet test`. Los de salida-de-consola necesitan más presupuesto
    Y que se les respete el final; el resto se paginan solos (el mensaje
    de truncado les dice cómo).
    """
    if tool_name in _CONSOLE_TOOLS:
        return SHELL_RESULT_CAP, SHELL_RESULT_KEEP_TAIL
    return TOOL_RESULT_CAP, 0


def _cap_tool_result(result: Any, *, tool_name: str = "") -> Any:
    """Capa 1: acota un tool result. Defensivo — tipo desconocido pasa igual."""
    cap, keep_tail = _caps_for(tool_name)
    if isinstance(result, str):
        return _cap_text(result, cap, keep_tail=keep_tail)
    if isinstance(result, list):
        # MCP content parts (TextContent y afines con .text). Presupuesto
        # compartido entre items; los que no entran se descartan con nota.
        budget = cap
        out = []
        for item in result:
            text = item if isinstance(item, str) else getattr(item, "text", None)
            if not isinstance(text, str):
                out.append(item)
                continue
            if budget <= 0:
                continue  # todavía pueden venir imágenes después del texto
            if len(text) > budget:
                capped = _cap_text(text, budget, keep_tail=min(
                    keep_tail, max(0, budget - 1)))
                if isinstance(item, str):
                    item = capped
                else:
                    item = item.model_copy(update={"text": capped})
                out.append(item)
                budget = 0
            else:
                out.append(item)
                budget -= len(text)
        return out
    return result


def _elide_old_tool_returns(messages: list) -> None:
    """Capa 2: stub-ea in-place los tool results viejos del run.

    Deja intactos los últimos TOOL_KEEP_FULL (working set del LLM);
    los anteriores quedan con head + marcador. Idempotente: un part ya
    elidido queda corto y el len-check lo saltea. Muta los objetos del
    historial vivo — pydantic-ai re-arma cada request desde estos
    mismos objetos, y el messages_json persistido hereda la elisión
    (win extra: los hilos guardados también adelgazan).
    """
    returns = [
        p
        for m in messages if isinstance(m, ModelRequest)
        for p in m.parts if isinstance(p, ToolReturnPart)
    ]
    for part in returns[:-TOOL_KEEP_FULL or None]:
        content = part.content
        if isinstance(content, str) and len(content) > _ELIDE_MIN_CHARS:
            part.content = (content[:_ELIDE_KEEP_HEAD] + "\n" + _ELIDE_MARK
                            + _elide_tail(content))


def _elide_tail(content: str) -> str:
    """Última línea (si es corta) de un result elidido: el veredicto.

    2026-07-31: el head son los primeros 200 chars — en un `run_shell`
    eso es el comando y el arranque del build, nunca el `(exit=N)` que
    va al final. Releyendo el hilo, el modelo no sabía si ese build
    había pasado. Medidos 579 results del historial en ese estado.
    """
    last = content.rstrip().rsplit("\n", 1)[-1].strip()
    return f"\n{last}" if 0 < len(last) <= _ELIDE_KEEP_TAIL else ""


def _elide_old_response_parts(messages: list) -> None:
    """Capa 2b: adelgaza los ModelResponse viejos del run (2026-07-28).

    Dos cosas que la capa 2 no tocaba y que medidas sobre runs reales
    pesaban 3 de cada 4 tokens re-enviados:

    - **thinking**: se descarta pasadas las últimas THINK_KEEP_FULL
      respuestas. Nunca se vacía un response por completo: si el
      thinking era su única parte, se deja como está (un assistant sin
      contenido lo rechazan varios providers).
    - **args de tool calls ya respondidas**: pasadas las mismas
      TOOL_KEEP_FULL que usan los results, el payload se reemplaza por
      un stub JSON. Una call sin responder NUNCA se toca: el par
      call↔return tiene que seguir cerrado.

    Idempotente y in-place, igual que `_elide_old_tool_returns`.
    """
    responses = [m for m in messages if isinstance(m, ModelResponse)]
    for m in responses[:-THINK_KEEP_FULL or None]:
        parts = [p for p in m.parts if not isinstance(p, ThinkingPart)]
        if parts and len(parts) != len(m.parts):
            m.parts[:] = parts

    answered = {
        p.tool_call_id
        for m in messages if isinstance(m, ModelRequest)
        for p in m.parts if isinstance(p, ToolReturnPart)
    }
    for m in responses[:-TOOL_KEEP_FULL or None]:
        for p in m.parts:
            if (isinstance(p, ToolCallPart)
                    and p.tool_call_id in answered
                    and len(str(p.args)) > _ELIDE_MIN_CHARS):
                p.args = _ELIDED_ARGS


def _close_orphan_tool_calls(messages: list, *, reason: str) -> int:
    """Contesta con un ToolReturnPart sintético cada tool call sin respuesta.

    Bug fix 2026-07-25: si cortamos el run mientras una tool estaba
    corriendo (watchdog de idle, tope global, cancel del usuario), el
    historial rescatado termina en un tool call huérfano. pydantic-ai
    rechaza el turno siguiente con
    `UserError: Cannot provide a new user prompt when the message
    history contains unprocessed tool calls` — o sea, el **continúa**
    que le ofrecemos al humano en el mensaje de corte explotaba SIEMPRE
    (caso anonimizado: un chat de ejemplo sobre un hilo de ejemplo).

    Devuelve cuántos huérfanos cerró. Muta `messages` in-place.
    """
    answered = {
        p.tool_call_id
        for m in messages if isinstance(m, ModelRequest)
        for p in m.parts if isinstance(p, ToolReturnPart)
    }
    # RetryPromptPart también cuenta como respuesta, pero no está
    # importado y llega por el mismo camino: filtramos por atributo.
    for m in messages:
        for p in getattr(m, "parts", []) or []:
            if (type(p).__name__ == "RetryPromptPart"
                    and getattr(p, "tool_call_id", None)):
                answered.add(p.tool_call_id)

    orphans = [
        p
        for m in messages if isinstance(m, ModelResponse)
        for p in getattr(m, "parts", []) or []
        if getattr(p, "tool_call_id", None)
        and getattr(p, "tool_name", None)
        and type(p).__name__ == "ToolCallPart"
        and p.tool_call_id not in answered
    ]
    if not orphans:
        return 0
    messages.append(ModelRequest(parts=[
        ToolReturnPart(
            tool_name=p.tool_name,
            tool_call_id=p.tool_call_id,
            content=f"(sin resultado: {reason})",
        )
        for p in orphans
    ]))
    logger.info(
        "cerré %d tool call(s) huérfano(s) para dejar el hilo reanudable: %s",
        len(orphans), ", ".join(p.tool_name for p in orphans))
    return len(orphans)


def _slim_history(messages: list, *, corte: Optional[int] = None) -> list:
    """Capa 3: adelgaza el historial ANTERIOR al último user prompt.

    Los turnos viejos quedan user/assistant text puro (sin ToolCallPart/
    ToolReturnPart/thinking); el último turno viaja intacto para que
    'continúa' retome con el working set completo. El pairing tool
    call↔return nunca cruza un user prompt, así que cortar ahí es
    seguro para providers OpenAI-compat.

    Bug fix 2026-08-13: de cada turno viejo sobrevive SOLO el último
    response. Los anteriores son la narración de mitad de run ("veo el
    appsettings:", "compilo:") cuya tool call esta misma función acaba de
    borrar — y un historial lleno de "el asistente anuncia una acción y no
    pasa nada" le enseña al modelo a hacer exactamente eso. Medido en el
    un hilo de ejemplo: 115 de 166 textos terminaban en `:` y el experto
    encadenó 14 runs sin ejecutar una sola tool. Ver
    docs/DIAG_HILO_EXAMPLE.md.

    `corte` es el índice desde el cual el historial viaja intacto; por
    default, el último user prompt. Pasar `len(messages)` adelgaza
    TAMBIÉN el último turno — es lo que hace `run_expert` cuando el
    verificador cortó por `off_plan` (ver ahí el porqué).
    """
    last_user = corte if corte is not None else -1
    if corte is None:
        for i, m in enumerate(messages):
            if isinstance(m, ModelRequest) and any(
                    isinstance(p, UserPromptPart) for p in m.parts):
                last_user = i
    if last_user <= 0:
        return messages
    # Último response de cada turno viejo = la respuesta al humano; los
    # anteriores son narración de tool. Se marca de atrás para adelante:
    # los tool results van en ModelRequest sin UserPromptPart, así que
    # "el siguiente mensaje" no alcanza para distinguirlos.
    keep_resp: set[int] = set()
    seen_resp = False
    for i in range(last_user - 1, -1, -1):
        m = messages[i]
        if isinstance(m, ModelResponse):
            if not seen_resp:
                keep_resp.add(i)
                seen_resp = True
        elif isinstance(m, ModelRequest) and any(
                isinstance(p, UserPromptPart) for p in m.parts):
            seen_resp = False
    slim: list = []
    for i, m in enumerate(messages[:last_user]):
        if isinstance(m, ModelRequest):
            parts = [p for p in m.parts if isinstance(p, UserPromptPart)]
        elif isinstance(m, ModelResponse):
            if i not in keep_resp:
                continue
            parts = [p for p in m.parts if isinstance(p, TextPart)]
        else:
            slim.append(m)
            continue
        if parts:
            slim.append(dataclasses.replace(m, parts=parts))
    return slim + messages[last_user:]


def _strip_foreign_thinking(messages: list, current_spec: str) -> int:
    """Saca los `ThinkingPart` del historial si lo escribió OTRO modelo.

    Por qué (2026-08-15): `_slim_history` deja el ÚLTIMO turno intacto —
    thinking incluido— para que "continúa" retome con el working set
    completo. Si entre dos turnos del mismo hilo cambia el modelo del
    ejecutor (override por run desde la UI, o un cambio en
    `defaults_json.model`), ese thinking se le replaya a un proveedor
    distinto del que lo generó. En OpenAI-compat viaja como
    `reasoning_content`: en el mejor caso es ruido que el modelo nuevo no
    puede interpretar, en el peor el endpoint lo rechaza.

    El razonamiento propio de un modelo no es contexto compartible; el
    texto y las tool calls sí. Devuelve cuántos parts sacó (0 = no hubo
    cambio de modelo, o no había thinking). Muta `messages` in-place.
    """
    cur = expert_context._model_key(current_spec)
    if not cur:
        return 0
    # El modelo del historial es el del último response que lo declare.
    prev = ""
    for m in reversed(messages):
        if isinstance(m, ModelResponse) and getattr(m, "model_name", None):
            prev = expert_context._model_key(m.model_name)
            break
    if not prev or not expert_context._distinto_modelo(prev, cur):
        return 0
    removed = 0
    for m in messages:
        if not isinstance(m, ModelResponse):
            continue
        parts = [p for p in m.parts if not isinstance(p, ThinkingPart)]
        # Nunca se vacía un response: un assistant sin contenido lo
        # rechazan varios providers (misma regla que `_elide_old_response_parts`).
        if parts and len(parts) != len(m.parts):
            removed += len(m.parts) - len(parts)
            m.parts[:] = parts
    if removed:
        logger.info(
            "historial escrito por %r y ahora corre %r: saqué %d thinking "
            "parts ajenos", prev, cur, removed)
    return removed


def _strip_instructions(messages: list) -> list:
    """Copia del historial sin las `instructions` de cada ModelRequest.

    pydantic-ai guarda el system prompt COMPLETO en cada ModelRequest,
    pero manda UNA sola copia al provider (el agente re-inyecta las
    suyas en cada request; las históricas solo son fallback cuando el
    agente no tiene). Medido sobre un hilo de ejemplo: 52 requests con
    ~33 KB de instructions c/u = 94.8% del blob de 1.81 MB, contra un
    payload real de 96 KB con un único mensaje `role=system`.

    Separada de sus dos usos a propósito, porque sirve para ambos:
      - estimar contexto sin contar 52 veces lo que viaja una sola;
      - persistir el hilo sin inflarlo ~20x.
    """
    return [
        dataclasses.replace(m, instructions=None)
        if isinstance(m, ModelRequest) and getattr(m, "instructions", None)
        else m
        for m in messages
    ]


def _prompt_con_imagenes(user: str, images: Optional[list] = None):
    """`user` solo, o [texto, BinaryContent…] si hay imágenes adjuntas.

    Devolver el str pelado cuando no hay imágenes no es cosmético: es
    el camino que ya estaba probado, y así los runs sin adjuntos no
    cambian ni un byte de lo que viaja al provider.
    """
    if not images:
        return user
    return [user, *(BinaryContent(data=d, media_type=m) for d, m in images)]


def _strip_images(messages: list) -> list:
    """Reemplaza las imágenes del historial por una nota de texto.

    Se aplica al PERSISTIR, no al correr: dentro del run el modelo sigue
    viendo la imagen en cada vuelta (es parte del prompt del usuario),
    pero el historial guardado no se la lleva a los turnos siguientes.
    Sin esto, un screenshot de 1MB se re-manda en cada turno futuro de
    la conversación para siempre — el mismo problema N² que ya atacan
    las capas de elisión de tool results y thinking.

    ponytail: la imagen NO se puede recuperar del historial. Si algún
    día hace falta "seguí mirando la captura", el upgrade es guardar el
    attach_id en la nota y re-cargarla por id.
    """
    out = []
    for m in messages:
        parts = getattr(m, "parts", None)
        if not parts:
            out.append(m)
            continue
        nuevas, tocado = [], False
        for p in parts:
            c = getattr(p, "content", None)
            if isinstance(c, list) and any(isinstance(x, BinaryContent) for x in c):
                textos = [x for x in c if isinstance(x, str)]
                n = sum(1 for x in c if isinstance(x, BinaryContent))
                nuevas.append(dataclasses.replace(p, content="\n".join(
                    textos + [f"[{n} imagen(es) adjunta(s) en su momento; "
                              "no se re-envían en los turnos siguientes]"])))
                tocado = True
            else:
                nuevas.append(p)
        out.append(dataclasses.replace(m, parts=nuevas) if tocado else m)
    return out


def _dump_messages(messages: list) -> str:
    """Serializa el historial para persistir. Único choke point: si algo
    más hay que sacar antes de guardar, va acá y no en cada call site."""
    return ModelMessagesTypeAdapter.dump_json(
        _strip_images(_strip_instructions(messages))).decode("utf-8")
