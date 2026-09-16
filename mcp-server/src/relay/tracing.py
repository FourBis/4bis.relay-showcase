"""Trazas OTel del relay. Apagado por default; se prende con FOURBIS_TRACING=1.

2026-08-26. pydantic-ai YA emite spans OpenTelemetry de cada run: el modelo,
cada tool call con sus argumentos, tokens in/out, reintentos, y el prompt
exacto que vio el LLM. Lo que faltaba no era instrumentación sino un
*exporter*: `opentelemetry-api` viene de arrastre con pydantic-ai, pero sin
SDK configurado esos spans son no-ops y se tiran al vacío.

O sea que esto no agrega telemetría nueva. Solo enchufa la que ya estaba
saliendo por un caño sin conectar.

Por qué importa acá: hoy, cuando un chat sale raro, el post-mortem se arma a
mano leyendo `relay.log` y reconstruyendo lo que el LLM vio con el endpoint
de debug de la Admin UI (`admin.py`, "Reproduce EXACTAMENTE lo que el LLM vio
en su `instructions=`"). Un trace da eso mismo, ordenado en el tiempo y con
timing, sin escribir nada.

`Agent.instrument_all()` lo prende para TODOS los agents del proceso. Hoy hay
cinco sitios que construyen uno (`_build_agent`, planner, verificador,
documentador, grafo); el switch global evita tocar los cinco y, sobre todo,
evita que el sexto nazca sin instrumentar.

Destino, por precedencia:
  1. OTEL_EXPORTER_OTLP_ENDPOINT → tu collector (Jaeger / Tempo / Grafana)
  2. LOGFIRE_TOKEN               → Logfire cloud
  3. ninguno                     → consola (sirve para ver que anda)

OJO con el endpoint: va la BASE, sin el path de la señal. El SDK le pega
`/v1/traces` y `/v1/metrics` solo. O sea `http://127.0.0.1:4318`, NO
`http://127.0.0.1:4318/v1/traces` — con eso último exporta contra
`/v1/traces/v1/traces` y no entra nada, sin más síntoma que un
`Failed to export span batch` en el log.

Si el collector está caído, el exporter reintenta en background y se
rinde solo: el run NO se ve afectado (verificado contra un puerto muerto).

CONTENIDO — leer esto antes de prender el cloud. Los spans pueden llevar el
prompt y la respuesta COMPLETOS. Esa es justo la parte útil para el
post-mortem, y también es el código del repo del cliente. Con destino local
van siempre; a Logfire cloud NO van salvo que pidas
`FOURBIS_TRACING_CONTENT=1` explícito. No mandamos código de terceros afuera
por default.

Best-effort, como `skills.py`: cualquier falla acá loggea y sigue. Un
exporter roto no puede tumbar el relay.

Instalar: pip install logfire      (o: pip install -e "mcp-server[tracing]")
"""
from __future__ import annotations

import logging

logger = logging.getLogger("relay.tracing")

_TRUTHY = frozenset({"1", "true", "yes", "on", "si", "sí"})

# Nombre del servicio en el trace. Fijo: si algún día hay más de un relay,
# se distinguen por `environment`, no por service_name.
SERVICE_NAME = "4bis-relay"


def _flag(name: str, default: bool) -> bool:
    """Lee un booleano de Config. Vacío / ausente → `default`."""
    from . import config
    raw = config.get(name).strip().lower()
    return raw in _TRUTHY if raw else default


def enabled() -> bool:
    """¿Están pedidas las trazas? (`FOURBIS_TRACING=1`)"""
    return _flag("FOURBIS_TRACING", False)


def setup_tracing() -> bool:
    """Enchufa el exporter OTel e instrumenta todos los agents.

    Devuelve True si quedó andando. No tira NUNCA: cualquier problema
    (logfire no instalado, endpoint mal, red caída) sale por el log y el
    relay arranca igual, sin trazas.
    """
    if not enabled():
        return False

    try:
        import logfire
    except ImportError:
        logger.warning(
            "FOURBIS_TRACING=1 pero `logfire` no está instalado: arranco sin "
            "trazas. Instalá con `pip install logfire`.")
        return False

    from . import config
    token = config.get("LOGFIRE_TOKEN").strip()
    otlp = config.get("OTEL_EXPORTER_OTLP_ENDPOINT").strip()
    # Destino remoto = Logfire cloud SIN collector propio. Si hay OTLP, el
    # destino lo elegiste vos y el contenido no sale de donde lo mandes.
    remoto = bool(token) and not otlp
    incluir_contenido = _flag("FOURBIS_TRACING_CONTENT", default=not remoto)

    try:
        logfire.configure(
            service_name=SERVICE_NAME,
            environment=config.get("FOURBIS_ENV"),
            # 'if-token-present' es literal de logfire: manda al cloud solo
            # si hay LOGFIRE_TOKEN. Sin token no sale nada de la máquina.
            send_to_logfire="if-token-present",
            # Consola solo cuando no hay a dónde exportar; si no, cada span
            # duplicaría el stdout que ya llena `relay.log`.
            console=False if (token or otlp) else None,
        )
        # El SDK ya está configurado; ahora sí los spans de pydantic-ai
        # dejan de ser no-ops.
        from pydantic_ai import Agent
        from pydantic_ai.agent import InstrumentationSettings
        Agent.instrument_all(
            InstrumentationSettings(include_content=incluir_contenido))
    except Exception as e:  # noqa: BLE001 — best-effort, ver docstring
        logger.warning(
            "no pude configurar las trazas (%r): sigo sin ellas", e)
        return False

    destino = otlp or ("logfire cloud" if token else "consola")
    logger.info(
        "trazas OTel ON → %s (contenido de prompts: %s)",
        destino, "sí" if incluir_contenido else "no")
    return True
