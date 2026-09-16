"""Configuración efectiva del relay.

La configuración operativa se administra desde el panel y se mantiene en
``system_config``. ``FOURBIS_DB_PATH`` es la única excepción: selecciona la
instancia SQLite antes de que exista una configuración que consultar.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path


# Metadatos compartidos por runtime y panel. Los valores se guardan como
# texto en system_config; ``type`` define validación y presentación.
PANEL_SETTINGS: dict[str, dict[str, object]] = {
    "RELAY_HOST": {"label": "Host", "group": "Servidor", "type": "text", "default": "127.0.0.1", "options": ["127.0.0.1", "0.0.0.0"], "restart": True},
    "RELAY_PORT": {"label": "Puerto", "group": "Servidor", "type": "number", "default": "8413", "min": 1, "max": 65535, "restart": True},
    "RELAY_API_KEY": {"label": "API key", "group": "Servidor", "type": "secret", "default": ""},
    "STATE_DIR": {"label": "Estado", "group": "Rutas", "type": "path", "default": "./state", "restart": True},
    "FOURBIS_CHATS_DIR": {"label": "Chats", "group": "Rutas", "type": "path", "default": "~/.4bis/chats", "restart": True},
    "FOURBIS_JSONL_DIR": {"label": "JSONL", "group": "Rutas", "type": "path", "default": "~/.4bis/jsonl", "restart": True},
    "FOURBIS_LOG_DIR": {"label": "Logs", "group": "Rutas", "type": "path", "default": "~/.4bis/logs", "restart": True},
    "FOURBIS_ATTACHMENTS_DIR": {"label": "Adjuntos", "group": "Rutas", "type": "path", "default": "~/.4bis/attachments", "restart": True},
    "FOURBIS_SKILLS_DIR": {"label": "Skills", "group": "Rutas", "type": "path", "default": "~/.4bis/skills", "restart": True},
    "FOURBIS_MCP_INSTALLS_DIR": {"label": "Instalaciones MCP", "group": "Rutas", "type": "path", "default": "~/.4bis/mcps", "restart": True},
    "FOURBIS_REPOS_ROOT": {"label": "Raíz de repos", "group": "Rutas", "type": "path", "default": "~/source/repos"},
    "BOT_NOTIFY_URL": {"label": "URL de notificaciones", "group": "Integraciones", "type": "url", "default": "http://127.0.0.1:8297/notify"},
    "FOURBIS_BOT_EXE": {"label": "Ejecutable del bot", "group": "Integraciones", "type": "path", "default": "", "restart": True},
    "FOURBIS_BOT_START_TIMEOUT": {"label": "Timeout de inicio del bot", "group": "Integraciones", "type": "number", "default": "25", "min": 1, "max": 600},
    "DISCORD_GUILD_ID": {"label": "Guild de Discord", "group": "Integraciones", "type": "text", "default": ""},
    "FOURBIS_DEFAULT_DISCORD_USER": {"label": "Usuario Discord predeterminado", "group": "Integraciones", "type": "text", "default": ""},
    "FOURBIS_DEFAULT_DISCORD_AUTHOR": {"label": "Nombre Discord predeterminado", "group": "Integraciones", "type": "text", "default": ""},
    "expert_timeout_s": {"label": "Timeout del experto", "group": "Ejecución", "type": "number", "default": "600", "min": 1, "max": 86400},
    "FOURBIS_EXPERT_REQUEST_TIMEOUT": {"label": "Timeout total del pedido", "group": "Ejecución", "type": "number", "default": "", "min": 1, "max": 172800},
    "FOURBIS_EXPERT_IDLE_TIMEOUT": {"label": "Timeout sin actividad", "group": "Ejecución", "type": "number", "default": "180", "min": 1, "max": 86400},
    "FOURBIS_EXPERT_THINK_TIMEOUT": {"label": "Timeout de razonamiento", "group": "Ejecución", "type": "number", "default": "600", "min": 1, "max": 86400},
    "FOURBIS_MCP_TIMEOUT": {"label": "Timeout de tool MCP", "group": "Ejecución", "type": "number", "default": "60", "min": 1, "max": 86400},
    "FOURBIS_MCP_INIT_TIMEOUT": {"label": "Timeout de inicio MCP", "group": "Ejecución", "type": "number", "default": "20", "min": 1, "max": 600},
    "FOURBIS_TOOL_CALL_TIMEOUT": {"label": "Timeout por tool", "group": "Ejecución", "type": "number", "default": "300", "min": 1, "max": 86400},
    "FOURBIS_EXPERT_REQUEST_LIMIT": {"label": "Pedidos por run", "group": "Ejecución", "type": "number", "default": "50", "min": 1, "max": 1000},
    "FOURBIS_EXPERT_VERIFIER_ROUNDS": {"label": "Rondas de verificación", "group": "Ejecución", "type": "number", "default": "2", "min": 0, "max": 20},
    "FOURBIS_EXPERT_MAX_LEGS": {"label": "Tramos iniciales", "group": "Ejecución", "type": "number", "default": "4", "min": 1, "max": 100},
    "FOURBIS_EXPERT_MAX_LEGS_HARD": {"label": "Tope de tramos", "group": "Ejecución", "type": "number", "default": "12", "min": 1, "max": 200},
    "FOURBIS_EXPERT_MAX_TOOL_CALLS": {"label": "Tope de tools", "group": "Ejecución", "type": "number", "default": "150", "min": 1, "max": 1000},
    "FOURBIS_CLAIM_TTL_S": {"label": "TTL de claim", "group": "Ejecución", "type": "number", "default": "300", "min": 60, "max": 86400},
    "FOURBIS_CLAIM_RENOVAR_S": {"label": "Renovación de claim", "group": "Ejecución", "type": "number", "default": "60", "min": 10, "max": 3600},
    "FOURBIS_GRAFO_AUTOSPLIT": {"label": "Subdividir grafos", "group": "Ejecución", "type": "boolean", "default": "1"},
    "FOURBIS_MODEL_CONTEXT_TOKENS": {"label": "Contexto del modelo", "group": "Contexto", "type": "number", "default": "120000", "min": 1, "max": 10000000},
    "FOURBIS_CONTEXT_WARN_PCT": {"label": "Alerta de contexto", "group": "Contexto", "type": "number", "default": "60", "min": 1, "max": 99},
    "FOURBIS_CONV_AUTOCLOSE_H": {"label": "Autocierre de conversaciones", "group": "Contexto", "type": "number", "default": "24", "min": 0.1, "max": 8760},
    "FOURBIS_COMPACT_TAIL_MAX_CHARS": {"label": "Cola de compactación", "group": "Contexto", "type": "number", "default": "24000", "min": 100, "max": 10000000},
    "FOURBIS_TOOL_RESULT_CAP": {"label": "Tope de resultado de tool", "group": "Contexto", "type": "number", "default": "16000", "min": 100, "max": 10000000},
    "FOURBIS_SHELL_RESULT_CAP": {"label": "Tope de resultado shell", "group": "Contexto", "type": "number", "default": "48000", "min": 100, "max": 10000000},
    "FOURBIS_SHELL_RESULT_KEEP_TAIL": {"label": "Cola de resultado shell", "group": "Contexto", "type": "number", "default": "12000", "min": 0, "max": 10000000},
    "FOURBIS_TOOL_KEEP_FULL": {"label": "Tools completas", "group": "Contexto", "type": "number", "default": "8", "min": 0, "max": 1000},
    "FOURBIS_THINK_KEEP_FULL": {"label": "Razonamientos completos", "group": "Contexto", "type": "number", "default": "2", "min": 0, "max": 1000},
    "FOURBIS_RECAP_MAX_CHARS": {"label": "Tope del recap", "group": "Contexto", "type": "number", "default": "2400", "min": 100, "max": 1000000},
    "FOURBIS_RECAP_MAX_TURNS": {"label": "Turnos del recap", "group": "Contexto", "type": "number", "default": "3", "min": 1, "max": 100},
    "FOURBIS_READ_MAX_BYTES": {"label": "Lectura máxima", "group": "Archivos", "type": "number", "default": "524288", "min": 1, "max": 1000000000},
    "FOURBIS_TREE_MAX_ENTRIES": {"label": "Entradas de árbol", "group": "Archivos", "type": "number", "default": "500", "min": 1, "max": 1000000},
    "FOURBIS_ATTACH_IMAGE_MAX": {"label": "Imagen adjunta máxima", "group": "Archivos", "type": "number", "default": "5242880", "min": 1, "max": 1000000000},
    "FOURBIS_ATTACH_INLINE_MAX": {"label": "Adjunto inline máximo", "group": "Archivos", "type": "number", "default": "200000", "min": 1, "max": 1000000000},
    "ATTACHMENT_MAX_BYTES": {"label": "Adjunto máximo", "group": "Archivos", "type": "number", "default": "10485760", "min": 1, "max": 1000000000},
    "FOURBIS_SHELL_MAX_CAPTURE": {"label": "Captura shell máxima", "group": "Archivos", "type": "number", "default": "200000", "min": 1, "max": 1000000000},
    "FOURBIS_SKILLS_TOKEN_BUDGET": {"label": "Presupuesto de skills", "group": "Contexto", "type": "number", "default": "12000", "min": 1, "max": 10000000},
    "FOURBIS_PROMPT_TOKEN_BUDGET": {"label": "Presupuesto del prompt", "group": "Contexto", "type": "number", "default": "30000", "min": 1, "max": 10000000},
    "VOICE_STT_API_KEY": {"label": "API key de voz", "group": "Voz", "type": "secret", "default": ""},
    "MINIMAX_STT_URL": {"label": "URL de STT", "group": "Voz", "type": "url", "default": "https://api.minimax.io/v1/audio/transcriptions"},
    "MINIMAX_STT_MODEL": {"label": "Modelo STT", "group": "Voz", "type": "text", "default": "minimax-asr"},
    "MINIMAX_STT_LANGUAGE": {"label": "Idioma STT", "group": "Voz", "type": "text", "default": "es-419"},
    "MINIMAX_STT_TIMEOUT": {"label": "Timeout STT", "group": "Voz", "type": "number", "default": "90", "min": 1, "max": 3600},
    "VOICE_MAX_AUDIO_BYTES": {"label": "Audio máximo", "group": "Voz", "type": "number", "default": "52428800", "min": 1, "max": 1000000000},
    "VOICE_AUDIO_DIR": {"label": "Audios", "group": "Voz", "type": "path", "default": "state/audio", "restart": True},
    "VOICE_TRANSCRIPTS_DIR": {"label": "Transcripciones", "group": "Voz", "type": "path", "default": "state/transcripts", "restart": True},
    "FOURBIS_TRACING": {"label": "Trazas", "group": "Observabilidad", "type": "boolean", "default": "0", "restart": True},
    "FOURBIS_TRACING_CONTENT": {"label": "Contenido en trazas", "group": "Observabilidad", "type": "boolean", "default": "0", "restart": True},
    "FOURBIS_ENV": {"label": "Entorno", "group": "Observabilidad", "type": "text", "default": "local", "restart": True},
    "OTEL_EXPORTER_OTLP_ENDPOINT": {"label": "Endpoint OTLP", "group": "Observabilidad", "type": "url", "default": "", "restart": True},
    "LOGFIRE_TOKEN": {"label": "Token Logfire", "group": "Observabilidad", "type": "secret", "default": "", "restart": True},
    "FOURBIS_SQL_MAX_ROWS": {"label": "Filas SQL", "group": "SQL", "type": "number", "default": "200", "min": 1, "max": 100000},
    "FOURBIS_SQL_MAX_CHARS": {"label": "Caracteres SQL", "group": "SQL", "type": "number", "default": "20000", "min": 1, "max": 10000000},
    "FOURBIS_SQL_TIMEOUT": {"label": "Timeout SQL", "group": "SQL", "type": "number", "default": "30", "min": 1, "max": 3600},
    "FOURBIS_SQL_RELAY_WRITE": {"label": "Escritura SQL sobre Relay", "group": "SQL", "type": "boolean", "default": "0"},
    "FOURBIS_SANDBOX": {"label": "Sandbox de archivos", "group": "Seguridad", "type": "boolean", "default": "1", "restart": True},
    "FOURBIS_EXTRA_ROOTS": {"label": "Raíces adicionales", "group": "Seguridad", "type": "text", "default": "", "restart": True},
    "FOURBIS_MCP_ALLOW_FILE_URL": {"label": "Permitir MCP file URL", "group": "Seguridad", "type": "boolean", "default": "0"},
    "FOURBIS_PLANNER_FALLBACK": {"label": "Fallbacks del planificador", "group": "Modelos", "type": "text", "default": ""},
    "MINIMAX_BASE_URL": {"label": "URL de MiniMax", "group": "Modelos", "type": "url", "default": "https://api.minimax.io/v1"},
    "NVIDIA_BASE_URL": {"label": "URL de NVIDIA", "group": "Modelos", "type": "url", "default": "https://integrate.api.nvidia.com/v1"},
    "OLLAMA_BASE_URL": {"label": "URL de Ollama", "group": "Modelos", "type": "url", "default": "http://localhost:11434/v1"},
    "FOURBIS_VET_MODEL": {"label": "Modelo de revisión MCP", "group": "Modelos", "type": "text", "default": ""},
    "FOURBIS_VET_TIMEOUT": {"label": "Timeout de revisión MCP", "group": "Modelos", "type": "number", "default": "120", "min": 1, "max": 3600},
    "GITHUB_BOARD_OWNER": {"label": "Owner de GitHub Project", "group": "GitHub", "type": "text", "default": ""},
    "GITHUB_BOARD_NUMBER": {"label": "Número de GitHub Project", "group": "GitHub", "type": "number", "default": "", "min": 1},
    "MODEL_PRICES": {"label": "Precios de modelos", "group": "Modelos", "type": "text", "default": ""},
}

_runtime: dict[str, str] = {}


def get(key: str, default: str = "") -> str:
    """Valor efectivo: secreto/config guardada y luego default del schema."""
    secret = _runtime.get(f"secret:{key}")
    if secret is not None:
        return secret
    value = _runtime.get(key)
    # Un número vacío significa restablecer el default del panel; las
    # cadenas/secretos vacíos sí son valores efectivos (p. ej. desactivar).
    if value is not None and not (
            value == "" and PANEL_SETTINGS.get(key, {}).get("type") == "number"):
        return value
    schema_default = PANEL_SETTINGS.get(key, {}).get("default", default)
    return str(schema_default if schema_default is not None else default)

def db_path() -> Path:
    """Ruta del SQLite de config. Default: ~/.4bis/relay.db"""
    raw = os.environ.get("FOURBIS_DB_PATH", "")
    p = Path(raw).expanduser() if raw else Path.home() / ".4bis" / "relay.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def chats_dir() -> Path:
    """Directorio de persistencia .md por chat. Default: ~/.4bis/chats"""
    return Path(get("FOURBIS_CHATS_DIR")).expanduser()


def jsonl_dir() -> Path:
    """Directorio de JSONL por target. Default: ~/.4bis/jsonl"""
    return Path(get("FOURBIS_JSONL_DIR")).expanduser()


def log_dir() -> Path:
    """Directorio de logs rotados. Default: ~/.4bis/logs"""
    return Path(get("FOURBIS_LOG_DIR")).expanduser()


def model_spec() -> str:
    """Modelo default de los expertos, formato `provider:modelo`.

    Providers soportados (ver experts.build_model):
        minimax:MiniMax-M3   (default; OpenAI-compatible)
        ollama:<modelo>      (local, OpenAI-compatible)
        openai:<modelo>      / anthropic:<modelo>  (nativos pydantic-ai)
        test                 (TestModel, sin red — para tests/smoke)
    """
    return get("FOURBIS_MODEL", "minimax:MiniMax-M3").strip()


def expert_timeout_s() -> float:
    return float(get("expert_timeout_s", "600"))


def expert_request_timeout_s() -> float:
    """Tope de TODO el pedido, no de una pasada. Default: 1800s.

    `expert_timeout_s` acota UNA llamada a `run_expert`, y el runner por
    etapas la llama de nuevo por cada ronda que pide el verificador — con
    un `t0` nuevo cada vez. O sea que el numero configurado no
    representaba el pedido: con los defaults (600s x 3 pasadas) el peor
    caso real eran 30 minutos de ejecutor, mas el planificador, mas un
    verificador por ronda, mas el documentador.

    El default es justamente ese peor caso ya implicito
    (`expert_timeout_s` x (`expert_verifier_rounds` + 1)), asi que
    encender esto no le recorta el presupuesto a nadie: lo vuelve UNO
    solo, compartido y visible. Bajarlo es una decision aparte.

    Per-project: `defaults_json.request_timeout`.
    """
    crudo = get("FOURBIS_EXPERT_REQUEST_TIMEOUT")
    if crudo:
        try:
            return float(crudo)
        except (TypeError, ValueError):
            pass
    return expert_timeout_s() * (expert_verifier_rounds() + 1)


def expert_idle_timeout_s() -> float:
    """Idle watchdog del experto (per-tool-call concept, real: per-node).

    El cap global (`expert_timeout_s`) sigue siendo el segundo anillo
    (600s default). Este es el PRIMER anillo: si pasan >N segundos sin
    que el agente emita NINGÚN node (CallToolsNode / ModelResponseNode /
    ModelRequestNode), el experto está idle — probablemente provider
    colgado. Cancelamos y devolvemos un mensaje útil al humano.

    Default 180s (3 min). Configurable via FOURBIS_EXPERT_IDLE_TIMEOUT.

    Por proyecto: project.defaults_json.idle_timeout_s gana sobre el
    default. Útil para tareas de auditoría que legítimamente tardan en
    tools largos (cbm con index grande, git push, etc.).
    """
    return float(get("FOURBIS_EXPERT_IDLE_TIMEOUT"))


def expert_think_timeout_s() -> float:
    """Cap de idle MIENTRAS EL MODELO GENERA. Default 600s.

    Existe por un diagnostico del 2026-08-23. El watchdog mide actividad
    como "nodes emitidos por el agente" (`async for node in agent_run`).
    Mientras el modelo genera su respuesta NO llega ningun node, asi que
    una generacion larga y un provider colgado se ven EXACTAMENTE IGUAL:
    el reloj de idle corre en los dos casos.

    Con MiniMax eso no se notaba —contesta en segundos— pero con un
    modelo razonador (nemotron y sus hilos largos, que es justo lo que
    queremos para coordinar planes) una sola generacion puede pasar los
    180s, y el watchdog mataba trabajo sano. Ese es el "queda colgado"
    que el usuario reportaba con los prompts largos.

    Dos caps y no uno solo mas grande: subir el idle general a 600s
    tambien retrasaria 10 minutos la deteccion de un provider realmente
    colgado en cualquier otra fase. Aca se paga el margen SOLO donde la
    ambiguedad existe.

    Sigue habiendo un tope global (`expert_timeout_s`, 600s) como segundo
    anillo, asi que esto no vuelve infinito a nada.
    """
    return float(get("FOURBIS_EXPERT_THINK_TIMEOUT"))


def tool_timeout_s() -> float:
    """FOURBIS_MCP_TIMEOUT efectivo — lo que usa pydantic-ai para cada tool.

    Cascada: system_config (admin UI) > default 60s.
    system_config se chequea en runtime vía `_runtime` (seteado por
    server._on_startup desde db.all_config). Si no hay system_config
    seteado, usa el default. Permite editar el valor sin reiniciar el relay.

    Sub-ola 2.6.
    """
    try:
        return float(get("FOURBIS_MCP_TIMEOUT"))
    except (TypeError, ValueError):
        return 60.0


def tool_call_timeout_s() -> float:
    """Techo de UNA tool-call (segundos). Cascada: system_config > default 300.

    2026-08-16 — antes esto no existía: el techo se derivaba del idle
    watchdog (`expert_idle_timeout_s * 0.9` = 162s con los defaults), así
    que cualquier comando legítimamente largo —`dotnet test`, `npm ci`,
    un build— moría a los 162s. El experto recibía un ModelRetry, lo
    reintentaba, volvía a morir, y el humano veía "el modelo se equivoca
    llamando tools" cuando lo que pasaba es que no lo dejábamos terminar.

    Ahora es un límite propio y el watchdog de idle NO cuenta el tiempo
    de una tool en vuelo (ver `_idle_watchdog` en experts.py), así que
    subir esto no lo pelea con el watchdog.

    Anillos de seguridad que siguen: el techo global del run
    (`expert_timeout_s`) y, para las tools nativas, `tool_timeout_s`.
    Per-project: `defaults_json.tool_call_timeout_s`.
    """
    try:
        return float(get("FOURBIS_TOOL_CALL_TIMEOUT"))
    except (TypeError, ValueError):
        return 300.0


def discord_guild_id() -> str:
    """ID del guild/servidor de Discord (system_config > env). "" si no
    está seteado.

    Lo usa la Admin UI para linkear el canal default de cada proyecto:
    `discord.com/channels/<guild>/<channel>` necesita el guild, y por
    proyecto solo guardamos el channel_id. Cascada igual que
    tool_timeout_s (runtime > env); editable en el tab Config.
    """
    return get("DISCORD_GUILD_ID").strip()


def expert_request_limit() -> int:
    """Presupuesto de requests al modelo por run del experto (pydantic-ai
    UsageLimits.request_limit). Es el número de idas-y-vueltas modelo↔tools
    antes de que pydantic-ai corte con UsageLimitExceeded.

    Default 50 (el mismo default de pydantic-ai). Tuneable por env. El corte
    ya no pierde trabajo: run_expert rescata el historial parcial y deja la
    conversación reanudable (ver experts._run_iter).
    """
    try:
        return int(get("FOURBIS_EXPERT_REQUEST_LIMIT"))
    except (TypeError, ValueError):
        return 50


def expert_verifier_rounds() -> int:
    """Rondas EXTRA de ejecutor que el verificador puede pedir solo.

    `needs_more` significa "falta trabajo y otra pasada lo termina": el
    sistema ya sabe qué hacer, así que devolverle la pelota al humano con
    un "respondé continúa" convertía cada tarea grande en apretar un botón
    cada 20 minutos. Con esto el runner cierra el lazo solo, re-entrando
    con el feedback del verificador como consigna.

    0 = comportamiento anterior (avisar y esperar al humano).
    Default 2, o sea hasta tres pasadas del ejecutor por turno. No es un
    presupuesto: eso es `expert_request_timeout_s`, que acota el pedido
    ENTERO. Hasta el 9/9/2026 esta línea decía que cada pasada seguía
    acotada "por el deadline global" y no era cierto — el deadline se
    rehacía en cada llamada a `run_expert`, así que este número
    multiplicaba el tiempo en vez de acotarlo. Es el tope de "cuántas
    veces acepto que el
    verificador diga que falta algo antes de que lo mire una persona" —
    si a la tercera sigue faltando, el problema no es una pasada más.

    Per-project: `defaults_json.verifier_rounds`.
    """
    try:
        return max(0, int(get("FOURBIS_EXPERT_VERIFIER_ROUNDS")))
    except (TypeError, ValueError):
        return 2


def expert_max_legs_hard() -> int:
    """Techo duro de tandas cuando hay supervisor de media corrida.

    Con `on_leg_boundary` enchufado (lo hace `run_expert_staged`), la
    decisión de seguir la toma el verificador tanda por tanda, así que
    `expert_max_legs` deja de ser un techo y pasa a ser el punto donde se
    empieza a pedir permiso. Un run grande que va bien se TERMINA en vez
    de quedar a medias — que es lo que se pidió: que tarde no importa,
    que quede incompleto sí.

    Esto de acá es solo el backstop por si el verificador nunca objeta y
    el deadline global no llega nunca. El anillo real sigue siendo el
    deadline.

    Default 12 (3× el max_legs default). Per-project:
    `defaults_json.max_legs_hard`.
    """
    try:
        return max(1, int(get("FOURBIS_EXPERT_MAX_LEGS_HARD")))
    except (TypeError, ValueError):
        return 12


def expert_max_legs() -> int:
    """Tandas de presupuesto que un run puede encadenar solo (2026-07-20c).

    Cuando el corte por `expert_request_limit` agarra al experto
    PROGRESANDO (tools variadas, sin loop), run_expert re-entra con el
    historial rescatado + nudge "continúa" — el mismo mecanismo que el
    resume manual — hasta este número de tandas. Presupuesto total
    efectivo = max_legs × request_limit (default 4×50 = 200 pasos).
    1 = comportamiento viejo (cortar en la primera tanda).
    Per-project: defaults_json.max_legs gana sobre esto.
    """
    try:
        return max(1, int(get("FOURBIS_EXPERT_MAX_LEGS")))
    except (TypeError, ValueError):
        return 4


def expert_max_tool_calls() -> int:
    """Tope de tool calls acumuladas antes de cortar por subdivisión
    automática (Fase 1, 2026-09-02) — SOLO aplica sin supervisor de
    media corrida (`on_leg_boundary is None`), ver el `if
    budget_exceeded:` de `run_expert` para el detalle completo.

    Medido sobre 238 nodos de grafo (que corren SIN supervisor —
    `orquestador.py` llama a `run_expert` sin ese hook): por
    `tool_calls` acumuladas, 0-200 → 0-4% se cortan; 200-250 → 36%;
    250+ → 85%. Separado por camino: con 250+ tool calls, los nodos de
    grafo cortan 85% de las veces, pero los chats staged (que SÍ tienen
    supervisor) terminan bien 60% de las veces — la extensión por
    supervisor (`expert_max_legs_hard`) se gana el sueldo justo ahí,
    así que este tope no la reemplaza donde ya funciona.

    No es un presupuesto de progreso normal (eso es
    `expert_request_limit` × `expert_max_legs`): es un backstop propio
    para el camino sin supervisor, donde nada más para el run antes de
    que entre en zona de fallo casi seguro. Default 250.

    **NO es un límite duro**, y conviene saberlo antes de elegir el
    número: se evalúa SOLO en el borde de tanda (dentro del
    `if budget_exceeded:` de `run_expert`), o sea cuando el run agotó su
    `expert_request_limit` y pide otra. Un nodo que TERMINA su trabajo
    dentro de su tanda no pasa nunca por el chequeo, por más tool calls
    que haya usado. Medido el 2026-09-03 con el tope bajado a 15 para
    probar: un nodo llegó a 16 tool calls y cerró en `writing`, sin
    cortarse. Es lo correcto —cortar algo que ya terminó sería un
    error—, pero significa que el tope acota a los que se DESBOCAN, no
    a los que simplemente usan muchas llamadas.

    2026-09-07: bajado de 250 a 150. El 250 salio de la medicion de
    arriba, pero en produccion NO llegaba a dispararse: seis corridas
    de ese dia murieron por `hard_timeout` o `budget_exceeded` con 202,
    210 y 222 tool calls — debajo del umbral. Otro corte ganaba siempre
    la carrera, asi que la subdivision automatica no se ejercitaba nunca
    y esas seis corridas se llevaron el 50% de los tokens del dia
    (24,5M de 48,6M) sin entregar nada. Con 150 el corte por
    subdivision llega ANTES que los otros dos y el nodo se parte en vez
    de morirse. Sigue estando en la banda medida de 0-200, donde solo
    el 0-4% se corta, asi que no deberia partir trabajo sano.
    """
    try:
        return max(1, int(get("FOURBIS_EXPERT_MAX_TOOL_CALLS")))
    except (TypeError, ValueError):
        return 150


def claim_ttl_s() -> int:
    """Cuánto vale una reserva de archivo del grafo sin renovarse.

    La tarea viva renueva sola mientras corre; este número es cuánto
    tarda en soltarse la de una que murió sin avisar. Bajarlo libera
    antes y arriesga pisar a una tarea viva si el latido se atrasa;
    subirlo es al revés. Piso de 60s para que no se pueda configurar
    un TTL más corto que el latido.
    """
    try:
        return max(60, int(get("FOURBIS_CLAIM_TTL_S")))
    except (TypeError, ValueError):
        return 300


def claim_renovar_s() -> int:
    """Cada cuánto late el orquestador para renovar las reservas.

    Tiene que ser bastante menor que `claim_ttl_s()`: con el default
    (60 vs 300) hay que perder cinco latidos seguidos para que a una
    tarea viva se le venza la lease.
    """
    try:
        return max(10, int(get("FOURBIS_CLAIM_RENOVAR_S")))
    except (TypeError, ValueError):
        return 60


def grafo_autosplit_habilitado() -> bool:
    """¿Subdividir automáticamente un nodo de grafo que corta por
    `budget_split` (Fase 2B, 2026-09-02)?

    Default encendido. Existe para poder apagarlo SIN deploy si algo
    sale mal en producción — con esto en `0`, un nodo `budget_split`
    queda `fallado` y espera al humano, que era el comportamiento de
    antes de esta fase.
    """
    return get("FOURBIS_GRAFO_AUTOSPLIT") != "0"


def compactor_model_spec() -> str:
    """Modelo del compactador de conversaciones (ADR-026).

    Prioridad: system_config > modelo de los expertos.
    """
    val = get("FOURBIS_COMPACTOR_MODEL").strip()
    if val:
        return val
    return model_spec()


def _staged_model_spec(env_key: str) -> str:
    """Cascada común de las etapas del runner por etapas (iter 11).

    Orden: system_config > compactador > ejecutor.
    > model_spec() (último recurso). El override por proyecto vive en
    `defaults_json` y lo resuelve `run_expert_staged`, no esta función.

    Si el rol está vacío, usa la cascada compartida.
    """
    # Centinela de tests: si el modelo base es TestModel, las etapas
    # auxiliares también, evitando llamadas facturables desde la suite.
    if model_spec() == "test":
        return "test"
    val = get(env_key).strip()
    if val:
        return val
    return compactor_model_spec()


def planner_model_spec() -> str:
    """Modelo del planificador (iter 11, runner por etapas).

    El planificador arma un plan numerado ANTES de que corra el
    experto. Es un turno breve y económico: no debería usar el mismo
    modelo pesado del ejecutor. Override por proyecto vía
    `defaults_json.planner_model`.
    """
    return _staged_model_spec("FOURBIS_PLANNER_MODEL")


def verifier_model_spec() -> str:
    """Modelo del verificador (iter 11, runner por etapas).

    El verificador revisa la salida del ejecutor contra el plan y
    devuelve un veredicto (complete | needs_more | needs_human). Turno
    breve y preciso. Override por proyecto vía
    `defaults_json.verifier_model`.
    """
    return _staged_model_spec("FOURBIS_VERIFIER_MODEL")


def documenter_model_spec() -> str:
    """Modelo del documentador (iter 11, runner por etapas).

    El documentador redacta el resumen final del run: qué se cambió,
    en qué archivos y qué queda pendiente. Turno breve de redacción.
    Override por proyecto vía `defaults_json.documenter_model`.
    """
    return _staged_model_spec("FOURBIS_DOCUMENTER_MODEL")


def model_context_tokens() -> int:
    """Ventana de contexto del modelo, en tokens (para el medidor del hilo).

    No hay API que la reporte: es un dato del provider. Cascada
    system_config > default para poder corregirlo desde la Admin UI
    sin redeploy si cambias de modelo.

    2026-08-13: el default era 200k (la ventana nominal de MiniMax-M3),
    pero la ventana ÚTIL es bastante menor: medido en un hilo de ejemplo,
    a ~92k el modelo dejó de llamar tools y respondía una línea. Con 200k
    eso daba 46% y el medidor nunca avisó. 120k pone el aviso (60%) en
    ~72k, antes del colapso. Si el modelo real aguanta más, súbelo por
    el panel.
    """
    try:
        val = int(float(get("FOURBIS_MODEL_CONTEXT_TOKENS")))
        if val > 0:
            return val
    except (TypeError, ValueError):
        pass
    return 120_000


def context_warn_pct() -> int:
    """% de la ventana ocupado por el hilo a partir del cual se avisa.

    Se mide sobre el contexto BASE (lo que pesa el historial antes de
    que el experto haga nada): pasado ese punto queda poco margen para
    tool results y el run empieza a arrastrarse hasta el timeout.
    """
    try:
        return max(1, min(99, int(get("FOURBIS_CONTEXT_WARN_PCT"))))
    except (TypeError, ValueError):
        return 60


def conv_autoclose_hours() -> float:
    """Horas de inactividad tras las que el sweeper cierra una
    conversación abierta y dispara la compactación (ADR-025)."""
    return float(get("FOURBIS_CONV_AUTOCLOSE_H"))


def mcp_init_timeout_s() -> float:
    """Timeout del handshake MCP (plan MCP_REGISTRY F1).

    Obligatorio por el historial de stdio colgándose en Windows
    (ADR-017): un MCP que no completa initialize en este tiempo se
    saltea con warning en vez de colgar el run entero.
    """
    return float(get("FOURBIS_MCP_INIT_TIMEOUT"))


# ---- runtime config: snapshot en memoria de la tabla system_config ----
# La tabla es la fuente de verdad (gestionable desde la Admin UI);
# server._on_startup la carga acá y admin.api_config_put la refresca al
# escribir. Los consumidores sync (p.ej. admin._cbm_env) leen de acá.

def set_runtime_config(cfg: dict[str, str]) -> None:
    """Reemplaza el snapshot en memoria de system_config."""
    _runtime.clear()
    _runtime.update({k: str(v) for k, v in cfg.items()})


def runtime_get(key: str, default: str = "") -> str:
    return _runtime.get(key, default)


def _load_runtime_config_sync() -> None:
    """Precarga una DB existente sin crear archivos durante el import."""
    raw = os.environ.get("FOURBIS_DB_PATH", "")
    path = Path(raw).expanduser() if raw else Path.home() / ".4bis" / "relay.db"
    if not path.is_file():
        return
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT key, value FROM system_config").fetchall()
    except sqlite3.Error:
        return
    set_runtime_config(dict(rows))


def default_discord_user() -> tuple[str, str]:
    """`(user_id, display)` del Discord al que avisar por default.

    Vacío = sin vinculación automática (el comportamiento de antes: hay
    que apretar el botón en cada conversación).

    Existe porque el botón de vincular se aprieta ANTES de irse, que es
    justo cuando uno no se acuerda: se lanza un run, se cierra la
    notebook, y la respuesta queda esperando en una pantalla que nadie
    está mirando. Con esto, toda conversación nueva nace avisando.
    """
    uid = get("FOURBIS_DEFAULT_DISCORD_USER").strip()
    nombre = get("FOURBIS_DEFAULT_DISCORD_AUTHOR").strip()
    return uid, nombre


def repos_root() -> str:
    """Raíz de los repos del usuario (file browser, CBM_ALLOWED_ROOT).

    Prioridad: system_config > ~/source/repos.
    """
    return str(Path(get("FOURBIS_REPOS_ROOT")).expanduser())


_load_runtime_config_sync()
