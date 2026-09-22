"""Catálogo de modelos, capacidades y lectura del prompt personal."""
from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from typing import Any, Optional
from . import config

logger = logging.getLogger("relay.experts")


PONYTAIL_PATH = Path.home() / ".copilot" / "copilot-instructions.md"

# Cache simple del ponytail: (mtime, texto). El archivo casi nunca cambia.
_ponytail_cache: tuple[float, str] | None = None


def _read_ponytail_sync() -> str:
    global _ponytail_cache
    try:
        mtime = PONYTAIL_PATH.stat().st_mtime
        if _ponytail_cache and _ponytail_cache[0] == mtime:
            return _ponytail_cache[1]
        text = PONYTAIL_PATH.read_text(encoding="utf-8").strip()
        _ponytail_cache = (mtime, text)
        return text
    except OSError:
        return ""


async def read_ponytail() -> str:
    """Filosofía base del usuario. Best-effort: sin archivo → ""."""
    return await asyncio.to_thread(_read_ponytail_sync)


def build_model(spec: str) -> Any:
    """`provider:modelo` → objeto modelo pydantic-ai (o string passthrough).

    minimax:*, nvidia:* y ollama:* van como OpenAI-compatible con
    base_url propia. openai:* / anthropic:* se delegan a la inferencia
    nativa de pydantic-ai. `test` → TestModel (sin red).
    """
    if spec == "test":
        from pydantic_ai.models.test import TestModel
        # call_tools=[]: NO llama tools. TestModel por default llama
        # TODAS con args dummy — con write_file/run_shell de un MCP
        # real eso ensuciaría el repo. Para probar tool calls reales:
        # scripts/smoke_expert.py.
        return TestModel(call_tools=[])

    provider_name, _, model_name = spec.partition(":")

    # Provider por tabla (2026-08-18): si la fila del catálogo trae
    # base_url + api_key_env, se arma OpenAI-compatible con eso. Es lo
    # que permite sumar DeepSeek —o cualquier endpoint compatible— con
    # un INSERT en el catálogo, sin tocar este archivo. La key
    # NUNCA sale de la tabla: la tabla dice CÓMO SE LLAMA la env var.
    fila = _catalog.get(spec) or {}
    if fila.get("base_url"):
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        # La key de la fila gana; si no hay, el nombre de env var que
        # diga la fila. Así conviven los dos estilos: los providers
        # viejos pueden referenciar una key y los nuevos se cargan enteros
        # desde la tabla.
        env = (fila.get("api_key_env") or "").strip()
        api_key = (fila.get("api_key") or "").strip() or (
            config.get(env) if env else "")
        if not api_key:
            raise ModelUnavailable(
                f"{spec} no tiene API key: cargala en la fila del modelo "
                + (f"o configurá {env} en el panel del relay." if env
                   else "(campo api_key) o indicá qué env var usar.")
            )
        return OpenAIChatModel(
            model_name or spec,
            provider=OpenAIProvider(
                base_url=fila["base_url"], api_key=api_key),
        )

    if provider_name == "minimax":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        api_key = config.get("MINIMAX_API_KEY")
        if not api_key:
            raise ModelUnavailable(
                "MINIMAX_API_KEY no está configurada. Cargala en el "
                "catálogo del panel o elegí otro modelo."
            )
        base_url = config.get("MINIMAX_BASE_URL")
        return OpenAIChatModel(
            model_name or "MiniMax-M3",
            provider=OpenAIProvider(base_url=base_url, api_key=api_key),
        )
    if provider_name == "nvidia":
        # build.nvidia.com: catálogo NIM con endpoints gratis (40 rpm en
        # la cuenta 4BIS). Los model_name llevan barra —
        # `nvidia:minimaxai/minimax-m3` — y partition(":") corta en el
        # primer ":", así que la barra viaja intacta.
        # OJO licencia: los free endpoints son para desarrollo/testing
        # /evaluación, no producción (NVIDIA API Trial ToS).
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        api_key = config.get("NVIDIA_API_KEY")
        if not api_key:
            raise ModelUnavailable(
                "NVIDIA_API_KEY no está seteada. Generá una en "
                "build.nvidia.com/settings/api-keys y cargala en el "
                "catálogo del panel."
            )
        base_url = config.get("NVIDIA_BASE_URL")
        return OpenAIChatModel(
            model_name,
            provider=OpenAIProvider(base_url=base_url, api_key=api_key),
        )
    if provider_name == "ollama":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        base_url = config.get("OLLAMA_BASE_URL")
        return OpenAIChatModel(
            model_name, provider=OpenAIProvider(base_url=base_url, api_key="ollama"),
        )
    api_key = (fila.get("api_key") or "").strip()
    env = (fila.get("api_key_env") or "").strip()
    api_key = api_key or (config.get(env) if env else "")
    if provider_name == "openai" and api_key:
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        return OpenAIChatModel(model_name, provider=OpenAIProvider(api_key=api_key))
    if provider_name == "anthropic" and api_key:
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider
        return AnthropicModel(model_name, provider=AnthropicProvider(api_key=api_key))
    # Filas nativas sin credencial quedan como error explícito: el SDK no
    # debe volver a buscar secretos ocultos en el entorno del proceso.
    if provider_name in {"openai", "anthropic"}:
        raise ModelUnavailable(
            f"{spec} no tiene API key: cargala en el catálogo del panel.")
    return spec


# ---------- catálogo de modelos (2026-08-18) ----------
#
# Lo que la UI ofrece en el selector del chat. `vision` NO es una
# propiedad del modelo sino DEL ENDPOINT: los pesos de minimax-m3 ven
# imágenes, pero servidos por los NIM gratis de NVIDIA el endpoint no
# acepta partes de imagen. Por eso la lista es a mano — el dato se
# comprueba a mano, no se deduce del nombre.
# Catálogo de modelos. La fuente de verdad es la tabla `models` de
# relay.db; acá vive el CACHE, que se llena en el startup igual que el
# de roles: es un lookup por run y la tabla la edita un humano.
#
# `vision` es tri-estado y eso importa: 0 es "medido, no ve", NULL es
# "nadie lo probó". Solo cortamos con el 0. Importar los 102 modelos de
# NVIDIA mete 102 NULL, y tratarlos como ciegos rompería runs que hoy
# andan.
#
# El caso que justifica medir en vez de deducir es glm-5.2: NO devuelve
# error con una imagen adentro, la acepta y contesta igual. Preguntándole
# derecho dice que no la ve. Un modelo así no se detecta mirando errores
# en producción.
_catalog: dict[str, dict] = {}


def load_catalog(rows) -> None:
    """Refresca el cache del catálogo. `rows` son filas de `models`."""
    global _catalog
    _catalog = {r["spec"]: dict(r) for r in rows}
    logger.info("catálogo de modelos: %d", len(_catalog))


def catalog() -> list[dict]:
    return list(_catalog.values())


def has_vision(spec: str) -> bool:
    """¿Ese modelo acepta imágenes?

    True si está medido que ve, y TAMBIÉN si no lo midió nadie: solo
    cortamos cuando SABEMOS que no ve. Asumir lo contrario haría que un
    modelo recién importado se tragara la imagen en silencio, que es
    exactamente el bug que esto viene a arreglar.
    """
    row = _catalog.get(spec)
    if row is None:
        return True
    return row.get("vision") != 0


def resolve_model_spec(model_override: str, project: Optional[dict] = None) -> str:
    """La misma cascada que usa `run_expert`, en un solo lugar.

    Existe para que el guard de imágenes en `/experts/run` mire
    EXACTAMENTE el modelo que va a correr y no una aproximación.
    """
    defaults = (project or {}).get("defaults_json") or {}
    return model_override or defaults.get("model") or config.model_spec()


def structured_output_settings(spec: str) -> Optional[dict]:
    """model_settings para un Agent con `output_type` (structured output).

    DeepSeek v4 (deepseek-v4-pro / -flash) rechaza con 400 "Thinking mode
    does not support this tool_choice" cuando pydantic-ai fuerza
    `tool_choice` — que es exactamente lo que hace al haber `output_type`.
    La doc de v4 dice deshabilitar thinking por `extra_body`.

    Solo para agentes con output_type: el experto normal usa tool_choice
    auto y con thinking anda bien, no le metemos mano.

    Los otros providers (minimax, anthropic, openai) no tienen el bug →
    None, que es "no toques nada".
    """
    return ({"extra_body": {"thinking": {"type": "disabled"}}}
            if "deepseek-v4" in (spec or "").lower() else None)


class ModelUnavailable(RuntimeError):
    """El modelo pedido no se puede armar (falta key, config, etc.)."""
