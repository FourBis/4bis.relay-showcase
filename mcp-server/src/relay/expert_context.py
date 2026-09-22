"""Medición del contexto, consumo y recuperación de respuestas vacías."""
from __future__ import annotations
import json
import logging
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
)
from typing import Any, Optional
from . import config

logger = logging.getLogger("relay.experts")


def _model_key(spec_or_name: str) -> str:
    """`minimax:MiniMax-M3` / `MiniMax-M3` → clave comparable.

    Los `ModelResponse` guardan el `model_name` que reportó el proveedor,
    que casi nunca trae el prefijo `provider:` con el que lo pedimos. Se
    compara por el tramo del modelo, en minúsculas.
    """
    s = (spec_or_name or "").strip().lower()
    if ":" in s:
        s = s.partition(":")[2] or s
    return s


def _distinto_modelo(prev: str, cur: str) -> bool:
    """¿Dos claves de modelo son de modelos REALMENTE distintos?

    Un prefijo compartido cuenta como el mismo modelo. Los proveedores no
    siempre devuelven el id que pediste: OpenAI responde
    `gpt-4o-2024-08-06` cuando pediste `gpt-4o`, y varios OpenAI-compat
    agregan sufijo de versión. Comparar por igualdad estricta daba un
    FALSO POSITIVO en cada turno de un hilo que nunca cambió de modelo, y
    el precio de ese falso positivo es borrar el razonamiento del último
    turno — o sea, pérdida de contexto silenciosa, justo lo que este
    módulo está tratando de evitar.

    Ante la duda NO se toca el historial: solo se limpia cuando los dos
    nombres son inequívocamente de modelos distintos.
    """
    if not prev or not cur:
        return False
    return not (prev.startswith(cur) or cur.startswith(prev))


def context_usage(messages_json: str, *, limit: Optional[int] = None,
                  warn_tokens: Optional[int] = None) -> Optional[dict]:
    """Ocupación de la ventana de contexto según el último run del hilo.

    None si no hay usage que leer (hilo vacío, historial recién
    compactado o provider que no reporta tokens).

    2026-08-31: `limit` y `warn_tokens` entran por parámetro (los saca
    de la tabla `models` el wrapper async `context_usage_db`). Sin
    ellos cae al default global, que es un número fijo para TODOS los
    modelos — el que hacía que un request real de 136.111 tokens
    apareciera como "113% de la ventana". `model_name` va en la salida
    para que la UI pueda decir contra qué ventana está midiendo.
    """
    try:
        messages = json.loads(messages_json or "")
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(messages, list):
        return None
    last_run: list[int] = []
    last_id = object()
    model_name = ""
    for m in messages:
        if not isinstance(m, dict) or m.get("kind") != "response":
            continue
        tokens = (m.get("usage") or {}).get("input_tokens")
        if not isinstance(tokens, int) or tokens <= 0:
            continue
        run_id = m.get("run_id")
        # run_id None (historiales viejos) ⇒ todo cuenta como un run.
        if run_id is not None and run_id != last_id:
            last_run = []
        last_id = run_id
        last_run.append(tokens)
        model_name = m.get("model_name") or model_name
    if not last_run:
        return None
    medida = limit is not None and limit > 0
    limit = limit if medida else config.model_context_tokens()
    warn = config.context_warn_pct()
    base, peak = last_run[0], max(last_run)
    peak_pct = round(100 * peak / limit)
    # El umbral por modelo es en TOKENS y no en %: el punto donde el
    # modelo se degrada es un dato medido del modelo (92k en MiniMax
    # M3), no una fracción de su ventana nominal. Sin dato por modelo,
    # el % global de siempre.
    umbral = (warn_tokens if warn_tokens and warn_tokens > 0
              else round(limit * warn / 100))
    return {
        "base_tokens": base,
        "peak_tokens": peak,
        "limit": limit,
        "pct": round(100 * base / limit),
        "peak_pct": peak_pct,
        "warn_pct": round(100 * umbral / limit),
        "warn_tokens": umbral,
        "model_name": model_name,
        # `False` cuando el límite salió del default global y no de la
        # ficha del modelo: la UI lo dice en vez de vender un porcentaje
        # contra una ventana inventada.
        "limit_medido": bool(medida),
        # El disparador es el PICO, no la base: es lo cerca que el run
        # llegó de la pared. La base dice cuánto bajarías compactando.
        "hot": peak >= umbral,
    }


async def context_usage_db(db: Any, messages_json: str) -> Optional[dict]:
    """`context_usage` con la ventana REAL del modelo que corrió.

    Wrapper async porque la ventana vive en la tabla `models` y los
    cuatro callers ya tienen el `db` a mano. Si el modelo no está en el
    catálogo (o la fila no tiene ventana cargada), cae al default
    global y `limit_medido` queda en False.
    """
    ctx = context_usage(messages_json)
    if ctx is None or not ctx.get("model_name"):
        return ctx
    try:
        ventana, umbral = await db.model_context(ctx["model_name"])
    except Exception as e:  # noqa: BLE001 — el medidor nunca voltea un run
        logger.warning("no pude leer la ventana de %r: %r",
                       ctx.get("model_name"), e)
        return ctx
    if not ventana:
        return ctx
    # Se recalcula sobre el dict y NO se llama de nuevo a context_usage:
    # el blob del historial llega al MB y este endpoint lo pollea la UI
    # cada 2,5s. Todo lo que cambia es aritmética sobre base/peak.
    base, peak = ctx["base_tokens"], ctx["peak_tokens"]
    if not umbral or umbral <= 0:
        umbral = round(ventana * config.context_warn_pct() / 100)
    ctx.update(
        limit=ventana, limit_medido=True, warn_tokens=umbral,
        pct=round(100 * base / ventana),
        peak_pct=round(100 * peak / ventana),
        warn_pct=round(100 * umbral / ventana),
        hot=peak >= umbral)
    return ctx


_CUT_PHASES = frozenset({
    "idle_timeout", "hard_timeout", "budget_exceeded",
    "tool_retries_exhausted", "cancelled", "steered",
    # 2026-08-17: el corte por veredicto off_plan del verificador de
    # media corrida ya escribe su propio mensaje con el feedback.
    "off_plan",
    # 2026-08-26: el corte por caída del proveedor ya escribe su propio
    # mensaje con el status y el porqué no se pudo retomar.
    "provider_error",
    # 2026-09-02: el tope de subdivisión (Fase 1) ya escribe su propio
    # mensaje con el consejo de partir en lotes.
    "budget_split",
})

#: Reintentos del EJECUTOR ante una caída del proveedor a mitad del run
#: (2026-08-26). Uno solo, y no una cascada como la del planificador: el
#: ejecutor lleva el historial y las tools del proyecto encima, así que
#: bajarlo a otro modelo a mitad de tarea cambia quién la termina. Si el
#: segundo intento también se cae, cortamos con el trabajo guardado y el
#: humano retoma con **continúa** cuando el endpoint vuelva.
_PROVIDER_RETRIES = 1
_PROVIDER_BACKOFF_S = 5.0


def _synthesize_no_final_text(
    messages_json: str, *, current_phase: str,
) -> tuple[str, str]:
    """Genera un fallback accionable cuando el LLM terminó sin texto.

    Bug 2026-08-10: un LLM real (p.ej. minimax M3) puede terminar el
    run con un ModelResponse que solo trae ToolCallPart(s) y ningún
    TextPart, o con un response vacío. `result.output` queda None o "",
    `output_text` se queda como "", y el chat se persiste como
    status="ok" con content="". El usuario en Discord/UI ve un "✓ done"
    sin ninguna respuesta textual — la interacción parece vacía.

    Detección: el ÚLTIMO `ModelResponse` no tiene `TextPart`, pero en
    el historial hay al menos UN `ToolReturnPart` (la tool respondió).
    Eso descarta el caso "el LLM no quiso responder desde el arranque"
    y enfoca el caso real: ejecutó tools y se quedó mudo.

    Devuelve (output_text, last_phase). Si NO detecta el patrón,
    devuelve ("", current_phase) — el caller no se entera. Si el run
    ya cortó por algo accionable (idle/timeout/budget/retries), no
    pisa ese mensaje.

    Idempotente y best-effort: historial corrupto o inesperado → no
    hace nada, no rompe el run.
    """
    if current_phase in _CUT_PHASES:
        return "", current_phase
    if not messages_json:
        return "", current_phase
    try:
        messages = ModelMessagesTypeAdapter.validate_json(messages_json)
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.debug(
            "_synthesize_no_final_text: historial inválido (%r)", e)
        return "", current_phase
    if not messages:
        return "", current_phase
    # ¿Hubo AL MENOS una tool call respondida?
    answered_tools = {
        p.tool_name
        for m in messages if isinstance(m, ModelRequest)
        for p in m.parts if isinstance(p, ToolReturnPart)
    }
    if not answered_tools:
        return "", current_phase
    # ¿El ÚLTIMO response del assistant está mudo?
    last_response = next(
        (m for m in reversed(messages) if isinstance(m, ModelResponse)),
        None)
    if last_response is None:
        return "", current_phase
    if any(isinstance(p, TextPart) for p in getattr(last_response, "parts", [])):
        return "", current_phase
    # Patrón confirmado: tools ejecutadas, respuesta final sin texto.
    tools_list = ", ".join(f"`{n}`" for n in sorted(answered_tools))
    fallback = (
        f"⚠️ El bot ejecutó {tools_list} pero no escribió una respuesta "
        "final. Mandá **continúa** para que redacte el resumen sobre lo "
        "que ya hizo, o pedile explícitamente que cierre con un párrafo "
        "sobre el resultado.")
    return fallback, "no_final_text"

# ---------- medidor de peso de tool results (2026-07-26) ----------
# Instrumentación pura: no cambia ni un byte de lo que viaja al modelo.
# Existe para elegir los caps con datos en vez de a ojo — hoy `run_shell`
# devuelve stdout sin truncar y no sabemos cuánto pesa de verdad.

# Escalera de caps candidatos (chars). Para cada uno acumulamos lo que
# se habría recortado, así el reporte responde "¿dónde pongo el cap?"
# con el ahorro EXACTO en vez de estimarlo desde el promedio — que
# miente feo cuando la distribución tiene cola larga (un `dotnet test`
# de 140KB entre veinte `git status` de 200 chars).
CAP_LADDER = (2_000, 4_000, 8_000, 16_000, 32_000, 64_000)


def _measure_tool_returns(
    node, meter: dict[str, dict], turn: int,
) -> None:
    """Suma los ToolReturnPart del request al acumulador `meter`.

    Best-effort y sin excepciones: si pydantic-ai cambia la forma del
    nodo, dejamos de medir — nunca rompemos un run por el medidor.
    """
    try:
        req = getattr(node, "request", None)
        for part in getattr(req, "parts", []) or []:
            if not isinstance(part, ToolReturnPart):
                continue
            name = getattr(part, "tool_name", None) or "?"
            content = getattr(part, "content", "")
            size = len(content if isinstance(content, str) else repr(content))
            slot = meter.get(name)
            if slot is None:
                slot = meter[name] = {
                    "n": 0, "chars": 0, "max": 0, "weighted": 0,
                    # cap → [chars por encima, esos chars × su turno]
                    "over": {c: [0, 0] for c in CAP_LADDER},
                }
            slot["n"] += 1
            slot["chars"] += size
            slot["max"] = max(slot["max"], size)
            slot["weighted"] += size * turn
            for cap, acc in slot["over"].items():
                excess = size - cap
                if excess > 0:
                    acc[0] += excess
                    acc[1] += excess * turn
    except Exception as e:  # noqa: BLE001
        logger.debug("tool meter: no pude medir el turno %d: %r", turn, e)


def summarize_tool_meter(
    meter: dict[str, dict], turns: int,
) -> dict:
    """Cierra el acumulador: agrega el carry y ordena por costo real.

    `carry_chars` es lo que ese tool le costó al run entero contando el
    reenvío: Σ size×(N−k). Es el número con el que se eligen los caps —
    `chars` a secas subestima a las tools que disparan temprano.

    `cap_saves` responde la otra mitad: para cada cap candidato, cuánto
    carry se habría ahorrado. Misma identidad aplicada al excedente.
    """
    tools = {}
    for name, s in meter.items():
        carry = max(0, turns * s["chars"] - s["weighted"])
        tools[name] = {
            "n": s["n"], "chars": s["chars"], "max": s["max"],
            "avg": s["chars"] // s["n"] if s["n"] else 0,
            "carry_chars": carry,
            "cap_saves": {
                str(cap): max(0, turns * over[0] - over[1])
                for cap, over in (s.get("over") or {}).items()
            },
        }
    return {
        "turns": turns,
        "total_chars": sum(t["chars"] for t in tools.values()),
        "total_carry": sum(t["carry_chars"] for t in tools.values()),
        "tools": dict(sorted(tools.items(),
                             key=lambda kv: -kv[1]["carry_chars"])),
    }


def format_context_note(ctx: Optional[dict]) -> str:
    """Aviso para el humano cuando el hilo se está llenando. "" si hay
    margen de sobra (o si no se pudo medir): no gastamos línea en eso."""
    if not ctx or not ctx["hot"]:
        return ""
    k = lambda n: f"{n / 1000:.0f}k"  # noqa: E731
    return (
        f"\n\n---\n🧠 **Contexto: el run llegó a {k(ctx['peak_tokens'])}/"
        f"{k(ctx['limit'])} ({ctx['peak_pct']}%)** de la ventana; el hilo ya "
        f"arranca en {k(ctx['base_tokens'])} ({ctx['pct']}%) antes de "
        "trabajar. Queda poco margen para tool results, y ahí es donde "
        "aparecen los timeouts.\n"
        "→ `compactar` deja el hilo, la rama y el Discord como están y baja "
        "el contexto a un resumen; si además cambiaste de tema, `cerrar` + "
        "`nuevo`.")
