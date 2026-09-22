"""Esquema SQLite de Relay.

Se mantiene separado de la fachada para que las migraciones y repositorios
puedan leerse por dominio sin alterar el SQL ni su orden de ejecución.
"""
from __future__ import annotations

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS projects (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    repo_path     TEXT NOT NULL,
    system_prompt TEXT NOT NULL DEFAULT '',
    mcp_servers   TEXT NOT NULL DEFAULT '[]',
    defaults_json TEXT NOT NULL DEFAULT '{}',
    native_tools  TEXT NOT NULL DEFAULT '[]',
    description   TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    include_in_index INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS commands (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    description   TEXT NOT NULL,
    handler       TEXT NOT NULL,
    args_schema   TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chats (
    id                TEXT PRIMARY KEY,
    project_slug      TEXT,
    vscode_session_id TEXT,
    source            TEXT NOT NULL,
    author            TEXT,
    target            TEXT,
    md_path           TEXT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    tokens_in         INTEGER,
    tokens_out        INTEGER,
    tool_calls        INTEGER,
    status            TEXT NOT NULL DEFAULT 'running',
    error             TEXT,
    -- ALTERs (incluidos en CREATE para que el schema refleje la realidad;
    -- las ALTERs legacy siguen como no-ops en _migrate).
    conversation_id   TEXT,  -- FK lógico a conversations
    phase_at_end      TEXT,  -- fase final del run (para diagnóstico)
    last_tool         TEXT,  -- última tool llamada (post-mortem)
    duration_ms       INTEGER,  -- latencia total del run
    model             TEXT,  -- qué LLM corrió
    suggestions       TEXT,  -- JSON array de botones de continuación
    progress_events   TEXT,  -- Sprint 1: timeline de eventos (JSON array)
    tool_bytes        TEXT,  -- 2026-07-26: pesos de tool results
    trimmed           INTEGER DEFAULT 0,  -- Sprint 1: ¿se truncó historial?
    trimmed_turns     INTEGER DEFAULT 0,  -- Sprint 1: turnos descartados
    -- 2026-08-31: la parte de `tokens_in` que el provider sirvió DESDE SU
    -- CACHÉ y cobra a una fracción. Medido sobre 4.890 requests reales de
    -- MiniMax: 153,2M de 185,7M de entrada (82,5%) eran cache reads. Sin
    -- esta columna el relay facturaba todo a tarifa plena y el total no
    -- podía cuadrar contra la consola del proveedor ni de casualidad.
    -- NULL = el run es anterior a la columna o el provider no lo reporta;
    -- 0 = lo reportó y no hubo caché. La distinción importa: `cost_usd`
    -- cobra a tarifa plena lo que no sabe que estaba cacheado.
    cache_read_tokens INTEGER,
    -- 2026-08-14: plan / veredicto / modelo de cada etapa del runner
    -- (iter 11). Van en un solo JSON y no en 7 columnas porque
    -- `finish_chat` ya tiene 15 parámetros y porque las etapas todavía
    -- se agregan y se sacan. Consultable igual:
    --   SELECT json_extract(stages_json,'$.verifier_verdict') FROM chats
    stages_json       TEXT,
    -- Fase 1 identidad (2026-08-17): mail verificado por Cloudflare
    -- Access, o 'owner' si el pedido no cruzó el túnel (bot, CLI, la
    -- máquina). `author` NO sirve para esto: es el nick de Discord y
    -- lo escribe el bot en el body.
    requested_by      TEXT
);
CREATE INDEX IF NOT EXISTS idx_chats_project ON chats(project_slug);
CREATE INDEX IF NOT EXISTS idx_chats_started ON chats(started_at);

CREATE TABLE IF NOT EXISTS command_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    command_name TEXT NOT NULL,
    source       TEXT,
    author       TEXT,
    args_json    TEXT,
    response     TEXT,
    duration_ms  INTEGER,
    status       TEXT NOT NULL,
    ts           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cmdlogs_ts ON command_logs(ts);

CREATE TABLE IF NOT EXISTS ignored_orphans (
    cbm_name    TEXT PRIMARY KEY,
    reason      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS system_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ADR-025: conversaciones de expertos (working memory por hilo Discord).
-- messages_json = historial pydantic-ai serializado (ModelMessagesTypeAdapter);
-- se reescribe entero cada turno (blob chico, single-user; upgrade path:
-- tabla messages append-only si un hilo llega a cientos de turnos).
CREATE TABLE IF NOT EXISTS conversations (
    id                TEXT PRIMARY KEY,
    project_slug      TEXT NOT NULL,
    discord_thread_id TEXT,
    discord_user_id   TEXT,  -- Iter 10.0: id del autor en Discord
                             -- (para mandar replies a Discord desde
                             -- la UI cuando no hay thread). Nullable
                             -- para conversaciones nacidas en UI.
    discord_author    TEXT,  -- display name del autor (cache para la UI)
    status            TEXT NOT NULL DEFAULT 'open',
    started_at        TEXT NOT NULL,
    closed_at         TEXT,
    last_activity_at  TEXT NOT NULL,
    messages_json     TEXT,
    -- Bitacora del ultimo run (2026-08-26). Vive aparte de
    -- `messages_json` a proposito: ese historial se recorta —en un corte
    -- por `off_plan` se le sacan las tool calls enteras— y ademas la
    -- bitacora viaja como *instructions*, que `_strip_instructions`
    -- borra antes de persistir. O sea que el unico registro de lo que el
    -- experto verifico se perdia justo al cortarse el run, que es cuando
    -- hace falta para retomar.
    bitacora_json     TEXT,
    summary           TEXT,
    -- Fase 1 identidad (2026-08-17): ver el comentario en `chats`.
    requested_by      TEXT,
    task_json         TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS chat_outputs (
    chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
    payload TEXT NOT NULL,
    exported INTEGER NOT NULL DEFAULT 0,
    -- Diagnostico del reintento (2026-09-09). Sin esto, una exportacion
    -- pendiente solo se podia investigar leyendo el log del relay, que
    -- no se persiste: la fila decia "exported=0" y nada mas.
    intentos INTEGER NOT NULL DEFAULT 0,
    ultimo_error TEXT,
    -- Cuando volver a probar. NULL = ya, o sea la primera vez. El
    -- backoff evita que un disco lleno se coma un intento por vuelta
    -- del barrido para siempre.
    proximo_intento_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_conv_status ON conversations(status, last_activity_at);
CREATE INDEX IF NOT EXISTS idx_conv_project ON conversations(project_slug);
CREATE TABLE IF NOT EXISTS conversation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    event_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('run','feedback','publish')),
    payload TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','processing','applied','uncertain','cancelled')),
    chat_id TEXT REFERENCES chats(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL, finished_at TEXT, error TEXT, commit_sha TEXT,
    UNIQUE(conversation_id, event_key)
);
CREATE INDEX IF NOT EXISTS idx_conversation_events_fifo ON conversation_events(conversation_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_events_processing
    ON conversation_events(conversation_id) WHERE state='processing';
-- idx_conv_discord_user se crea después del ALTER de la columna
-- (iter 10.0) — el CREATE INDEX en el SCHEMA falla en DBs preexistentes
-- que aún no tienen la columna.

-- ADR-026: hechos atómicos destilados al cerrar una conversación.
-- Append-only + supersede soft (2026-07-14): un hecho nunca se pisa ni
-- se borra al compactar; si una conversación posterior lo contradice,
-- el compactador lo marca superseded (superseded_at/by) y deja de
-- listarse. Los hechos viejos entorpecen cuando la decisión cambió.
CREATE TABLE IF NOT EXISTS facts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_slug        TEXT NOT NULL,
    fact                TEXT NOT NULL,
    source_conversation TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_at       TEXT,
    superseded_by       TEXT,
    -- Aprobación estricta (2026-08-21): el compactador escribe `pending`
    -- y el hecho NO llega al experto hasta que un humano lo aprueba. Los
    -- que escribe una persona a mano nacen `approved`: no hay a quién
    -- pedirle permiso. El DEFAULT es `approved` para que la migración
    -- de los 532 hechos que ya existían no cree una cola de revisión de
    -- 532 el primer día; `add_facts` pasa el estado explícito.
    status              TEXT NOT NULL DEFAULT 'approved',
    reviewed_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_project ON facts(project_slug);

-- Autoaprendizaje (2026-07-12): borradores de skill que destila el
-- compactador (memory.SkillDraft). status=pending hasta que el usuario
-- los apruebe (se escriben a ~/.copilot/skills) o rechace desde la
-- Admin UI. NUNCA se instalan solos.
CREATE TABLE IF NOT EXISTS skill_drafts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    content             TEXT NOT NULL,
    project_slug        TEXT,
    source_conversation TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
    approved_path       TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    reviewed_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_skill_drafts_status
    ON skill_drafts(status, created_at DESC);

-- ADR-028: Modo Nocturno. Una fila por corrida; el detalle de tareas
-- vive en el plan ledger (.relay/night-plan.md + espejo en state/),
-- NO en DB (tabla night_tasks diferida hasta que la auditoría duela).
CREATE TABLE IF NOT EXISTS night_runs (
    id              TEXT PRIMARY KEY,
    project_slug    TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    deadline_at     TEXT NOT NULL,
    ended_at        TEXT,
    end_reason      TEXT,
    prs_opened      INTEGER DEFAULT 0,
    tasks_done      INTEGER DEFAULT 0,
    tasks_discarded INTEGER DEFAULT 0,
    report_path     TEXT,
    branch          TEXT,                       -- Iter 5.1: rama ÚNICA del run
    pr_url          TEXT,                       -- Iter 5.1: PR único al final
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_night_runs_project
    ON night_runs(project_slug, started_at DESC);

-- MCP registry (plan MCP_REGISTRY, F0): catálogo global de MCP servers,
-- espejo de `commands` (ADR-013). Reemplaza el blob projects.mcp_servers
-- (la columna queda como legacy/audit; F1 deja de leerla).
-- Secretos en `env` por referencia "env:NAME" (se resuelven contra el
-- entorno al armar el toolset), nunca valores directos para credenciales.
CREATE TABLE IF NOT EXISTS mcp_servers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    capability    TEXT NOT NULL,               -- db|browser|docs|github|aws|files
    transport     TEXT NOT NULL DEFAULT 'stdio', -- stdio|http|sse
    command       TEXT,                        -- stdio: ejecutable
    args          TEXT NOT NULL DEFAULT '[]',  -- stdio: JSON array
    url           TEXT,                        -- http/sse: endpoint
    env           TEXT NOT NULL DEFAULT '{}',  -- JSON; refs "env:NAME"
    read_only     INTEGER NOT NULL DEFAULT 1,
    on_demand     INTEGER NOT NULL DEFAULT 1,  -- 0 = always-on (wrapper)
    idle_timeout_s INTEGER NOT NULL DEFAULT 300,
    enabled       INTEGER NOT NULL DEFAULT 0,  -- 0 hasta vetting+handshake
    source_url    TEXT,                        -- alta por GitHub (F2)
    source_commit TEXT,
    install_dir   TEXT,
    vet_verdict   TEXT,                        -- safe|suspect|rejected|unknown
    vet_report    TEXT,
    health        TEXT NOT NULL DEFAULT 'unknown', -- unknown|ok|handshake_failed
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Scoping MCP↔proyecto: un MCP SIN filas acá sirve a TODOS los proyectos
-- (caso wrapper). Con filas, solo a los linkeados.
CREATE TABLE IF NOT EXISTS project_mcp_servers (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    mcp_id     INTEGER NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
    PRIMARY KEY (project_id, mcp_id)
);

-- Iter 9.10: la tabla `consults` ya no se usa. Las 6 filas históricas
-- se migraron a `chats` (target=notes) en iter 9.8. El primitive de
-- consults se removió del backend en este pase. Si tu DB todavía
-- tiene la tabla de iter 9.x, este DROP la limpia idempotentemente.
DROP TABLE IF EXISTS consults;

-- 2026-08-16: conexiones SQL que el experto puede consultar desde el
-- chat. El DSN vive acá y el modelo usa un ALIAS: así la contraseña no
-- entra al `messages_json` del hilo, que se replaya en cada turno y
-- queda en disco. `escribir=0` por default — un DELETE sin WHERE contra
-- la producción de un cliente no tiene deshacer.
CREATE TABLE IF NOT EXISTS db_connections (
    alias       TEXT PRIMARY KEY,
    dsn         TEXT NOT NULL,
    descripcion TEXT NOT NULL DEFAULT '',
    escribir    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 2026-08-16: preguntas del EXPERTO al humano durante un chat.
--
-- Tabla propia y no `night_questions` por dos razones. La técnica:
-- `night_questions.run_id` tiene FK a `night_runs` y los FK están ON
-- (PRAGMA en _connect), así que una pregunta de un chat no entra ahí.
-- La de diseño: el ciclo de vida es distinto. Una pregunta de night
-- BLOQUEA al orchestrator hasta que alguien responde; una de chat
-- TERMINA el turno — el experto deja la pregunta anotada, cierra, y la
-- respuesta del humano entra como el turno siguiente de la conversación,
-- que es como el hilo ya sabe pasarse contexto. Sin hilos bloqueados
-- esperando a alguien que quizás contesta mañana.
CREATE TABLE IF NOT EXISTS expert_questions (
    id              TEXT PRIMARY KEY,      -- q_<uuid8>
    chat_id         TEXT NOT NULL,         -- el run que preguntó
    conversation_id TEXT,                  -- donde entra la respuesta
    project_slug    TEXT,
    kind            TEXT NOT NULL DEFAULT 'choice',  -- choice|install|text
    question_json   TEXT NOT NULL,         -- {title, detail, options[]}
    asked_at        TEXT NOT NULL,
    answered_at     TEXT,
    answer_json     TEXT,                  -- {choice, label, free_text?}
    -- open|answered|skipped|superseded. `superseded` (2026-08-16) es la
    -- que quedó vieja porque el experto preguntó otra cosa después: no la
    -- descartó el humano, y no se puede contestar (answer exige 'open').
    status          TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_expert_q_open
    ON expert_questions(conversation_id) WHERE status='open';
CREATE INDEX IF NOT EXISTS idx_expert_q_chat ON expert_questions(chat_id);

-- Iter 9.7: preguntas interactivas al humano durante un night run.
-- El orchestrator bloquea esperando respuesta cuando necesita input
-- (fallo de Fase 1, decisión entre bloques lógicos, etc.).
-- Respondida via /admin/api/night/questions/<id>/answer o via Discord
-- (iter 9.8).
CREATE TABLE IF NOT EXISTS night_questions (
    id            TEXT PRIMARY KEY,            -- q_<uuid8>
    run_id        TEXT NOT NULL,               -- FK night_runs.id
    phase         TEXT NOT NULL,               -- phase1 | block_done | phase2
    question_json TEXT NOT NULL,               -- JSON: {kind, prompt, options, ...}
    asked_at      TEXT NOT NULL,
    answered_at   TEXT,                        -- NULL si todavía esperando
    answer_json   TEXT,                        -- JSON: {choice, free_text?}
    notified_via  TEXT,                        -- discord | admin | both
    notify_target TEXT,                        -- discord channel_id o admin_url
    FOREIGN KEY (run_id) REFERENCES night_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_night_questions_open
    ON night_questions(run_id) WHERE answered_at IS NULL;

-- Plantillas de directiva para el modo nocturno (2026-08-27).
--
-- Una directiva buena cuesta trabajo: hay que numerar los puntos (el
-- planificador los extrae y chequea cobertura) y nombrar solo paths que
-- resuelvan contra el indice cbm — una ref invalida descarta la tarea
-- entera, en silencio. Reescribir eso cada noche desde cero es donde se
-- pierden los runs.
--
-- `project_slug` '' = plantilla global (sirve para cualquier repo). Con
-- slug = especifica de ese proyecto. La UI muestra las dos.
--
-- '' y no NULL: en SQLite dos NULL son DISTINTOS para un UNIQUE, asi que
-- con NULL se podian crear dos plantillas globales con el mismo nombre y
-- el UNIQUE no decia nada.
CREATE TABLE IF NOT EXISTS night_templates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_slug TEXT NOT NULL DEFAULT '',  -- '' = global
    nombre       TEXT NOT NULL,
    directiva    TEXT NOT NULL,
    notas        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (project_slug, nombre)
);
CREATE INDEX IF NOT EXISTS idx_night_templates_proj
    ON night_templates(project_slug);

-- CRM (trycompai/crm local): espejo read-only de Companies + Contacts +
-- Deals. El relay NO es la fuente de verdad: los datos viven en el
-- Postgres del CRM; esta tabla es un snapshot para que la Admin UI los
-- liste sin depender de que el CRM esté arriba. `client_id` en projects
-- vincula el proyecto relay con la empresa del CRM (se llena cuando el
-- usuario convierte un deal ganado en proyecto, ver crm.py / tab-crm.js).
-- `ext_id` = el cuid de `company.id` en el CRM local.
-- ponytail: contacts_json y deals_json son JSON embebido a propósito.
-- Queries sobre deals individuales (ej: "todos los deals en etapa X")
-- no están en el radar de Fase 1; cuando duela, se migra a tablas
-- crm_contacts / crm_deals con FK. Hoy con un SELECT * y un json.loads
-- alcanza.
CREATE TABLE IF NOT EXISTS crm_clients (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ext_id          TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    domain          TEXT,
    contacts_json   TEXT NOT NULL DEFAULT '[]',
    deals_json      TEXT NOT NULL DEFAULT '[]',
    last_activity_at TEXT,  -- último email/reunión/nota (ActivityStampService del CRM)
    last_sync_at    TEXT,
    last_sync_status TEXT NOT NULL DEFAULT 'pending'  -- pending|ok|error
);
CREATE INDEX IF NOT EXISTS idx_crm_clients_sync
    ON crm_clients(last_sync_at DESC);

-- FK de projects hacia crm_clients. Se llena con ALTER en _init_schema_sync
-- (las DBs preexistentes ya tienen la tabla projects sin la columna).

-- ============================================================
-- F1 (2026-08-17): el plan deja de ser un string
-- ============================================================
--
-- Hasta hoy el plan del planificador se inyectaba como texto en el
-- system prompt del ejecutor y ahi moria: no habia forma de saber en
-- que paso iba, ni de retomarlo, ni de mostrarlo. "Que se vea el avance
-- en la UI" no era un problema de UI — no habia nada que mostrar.
--
-- Un grafo por conversacion. `nodos` con estado + `aristas` con las
-- dependencias explicitas (DAG): permite paralelismo real y, sobre
-- todo, que un nodo que falla BLOQUEE a los que dependian de el en vez
-- de que el resto siga trabajando sobre una base rota.
CREATE TABLE IF NOT EXISTS task_graphs (
    id              TEXT PRIMARY KEY,      -- g_<uuid8>
    conversation_id TEXT,
    project_slug    TEXT,
    objetivo        TEXT NOT NULL,         -- el pedido original del humano
    estado          TEXT NOT NULL DEFAULT 'activo',  -- activo|hecho|fallado|cancelado
    -- Veredicto de la UNICA verificacion del grafo, la que corre al
    -- cerrar (ver `orquestador._verificar_al_cerrar`). JSON con el
    -- mismo vocabulario que los chats —complete|needs_more|needs_human|
    -- off_plan— mas el feedback, el modelo, los tokens de la etapa y su
    -- error si no pudo correr. Una columna y no una tabla aparte: es UN
    -- registro por grafo y se lee siempre junto con el grafo.
    -- NULL = todavia no se verifico (o el grafo se cancelo, que no se
    -- verifica a proposito).
    verificacion_json TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_graphs_conv
    ON task_graphs(conversation_id, estado);

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,          -- t_<uuid8>
    graph_id    TEXT NOT NULL REFERENCES task_graphs(id) ON DELETE CASCADE,
    titulo      TEXT NOT NULL,
    detalle     TEXT NOT NULL DEFAULT '',
    -- pendiente: le faltan dependencias o nadie lo tomo todavia.
    -- corriendo: hay un run vivo. hecho/fallado: termino.
    -- bloqueado: una dependencia fallo — no se intenta, no es su culpa.
    -- esperando_humano: paro a preguntar (ver `ask_human`).
    estado      TEXT NOT NULL DEFAULT 'pendiente',
    -- Declarado por el planificador. Decide si un fallo se reintenta
    -- SOLO o para y pregunta: reintentar algo que ya escribio archivos
    -- puede duplicar trabajo, y eso no lo puede decidir el runtime.
    idempotente INTEGER NOT NULL DEFAULT 0,
    intentos    INTEGER NOT NULL DEFAULT 0,
    max_intentos INTEGER NOT NULL DEFAULT 2,
    modelo      TEXT NOT NULL DEFAULT '',   -- spec con el que se ejecuto
    chat_id     TEXT,                       -- el run que lo ejecuto
    resultado   TEXT NOT NULL DEFAULT '',   -- resumen de lo que hizo
    error       TEXT NOT NULL DEFAULT '',
    orden       INTEGER NOT NULL DEFAULT 0, -- desempate estable para la UI
    archivos    TEXT NOT NULL DEFAULT '[]', -- JSON: los que dice que va a tocar
    started_at  TEXT,
    ended_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_graph ON tasks(graph_id, orden);

-- Aristas: `task_id` depende de `depende_de`. Tabla propia y no una
-- columna con CSV: asi una consulta puede preguntar "quien depende de
-- este" sin parsear strings, que es exactamente lo que hace falta para
-- bloquear en cascada cuando algo falla.
CREATE TABLE IF NOT EXISTS task_deps (
    graph_id    TEXT NOT NULL REFERENCES task_graphs(id) ON DELETE CASCADE,
    task_id     TEXT NOT NULL,
    depende_de  TEXT NOT NULL,
    PRIMARY KEY (task_id, depende_de)
);
CREATE INDEX IF NOT EXISTS idx_task_deps_graph ON task_deps(graph_id);

-- Reservas de archivo (F2): que dos bots en paralelo no toquen lo mismo.
-- La declaracion del planificador (tasks.archivos) evita el choque al
-- PLANIFICAR; esto lo impide al ESCRIBIR, que es donde se puede
-- garantizar. Una fila viva = "esta tarea tiene tomado este archivo".
-- Se borran al terminar la tarea; `release_dead_claims` barre las de
-- runs que murieron sin soltar, y lo hace GLOBAL (no por grafo) porque
-- el bloqueo tambien es global: barrer menos de lo que se bloquea deja
-- archivos tomados por nadie. Sin FK a proposito — el barrido ya cubre
-- las huerfanas, y un FK obligaria a ordenar los borrados.
CREATE TABLE IF NOT EXISTS task_file_claims (
    archivo   TEXT NOT NULL,      -- normalizado: unix, minusculas
    task_id   TEXT NOT NULL,
    graph_id  TEXT NOT NULL,
    -- Scope del choque (2026-09-08). Antes el SELECT de conflictos
    -- barria la tabla ENTERA sin mirar de que proyecto era cada fila:
    -- reservar "src/main.py" en un repo bloqueaba "src/main.py" de
    -- otro repo distinto. Se resuelve por `graph_id` contra
    -- `task_graphs.project_slug` (ver `claim_task_files`), nunca lo
    -- manda el caller. '' = grafo sin proyecto resuelto (incluye los
    -- de test que no lo declaran); esas siguen chocando entre si,
    -- como antes de esta columna.
    project_slug TEXT NOT NULL DEFAULT '',
    tomado_at TEXT NOT NULL,
    -- Vencimiento de la lease (2026-09-04). NULL = reserva vieja, sin
    -- lease: solo la barre la regla por estado. Ver release_dead_claims.
    vence_at  TEXT,
    PRIMARY KEY (archivo, task_id)
);
CREATE INDEX IF NOT EXISTS idx_claims_task ON task_file_claims(task_id);
-- Catálogo de modelos (2026-08-18). Antes vivía como constante en
-- experts.py; pasa a tabla para poder prender/apagar, cargar tarifas y
-- sumar un provider pago sin tocar código.
--
-- `api_key_env` guarda el NOMBRE de la variable de entorno, NUNCA la
-- key. Un secreto en SQLite se filtra en cada backup, cada export y
-- cada SELECT que le pase un experto a un LLM. La key vive en .env y
-- acá solo decimos cómo se llama.
--
-- `vision` es tri-estado a propósito:
--   1    medido: ve imágenes
--   0    medido: NO ve  → el guard de /experts/run corta
--   NULL sin medir      → se deja pasar, como cualquier modelo nuevo
-- Importar los 102 del catálogo de NVIDIA mete 102 filas en NULL, que
-- es la verdad: nadie las probó. Inventar un 1 o un 0 ahí sería peor
-- que no tener el dato.
CREATE TABLE IF NOT EXISTS models (
    spec        TEXT PRIMARY KEY,   -- provider:modelo, como lo pide build_model
    label       TEXT NOT NULL DEFAULT '',
    provider    TEXT NOT NULL DEFAULT '',
    base_url    TEXT,               -- NULL = el default del provider
    api_key_env TEXT,               -- NOMBRE de una env var (.env)
    api_key     TEXT,               -- la key acá mismo; gana sobre api_key_env
                                    -- OJO: los expertos corren con shell en
                                    -- esta máquina y este archivo es legible.
                                    -- Write-only por la API: el GET la enmascara.
    vision      INTEGER,            -- 1 / 0 / NULL (ver arriba)
    enabled     INTEGER NOT NULL DEFAULT 0,
    cost_in     REAL,               -- USD por millón de tokens de entrada
    cost_out    REAL,               -- USD por millón de salida
    -- 2026-08-31. `cost_cache_in`: USD por millón de tokens de entrada que
    -- el provider sirvió de su caché. NULL = no sabemos, y entonces esos
    -- tokens se cobran a `cost_in` (caro de más, pero nunca de menos).
    -- `context_tokens` / `context_warn_tokens`: la ventana REAL del modelo
    -- y el punto MEDIDO donde se degrada. Son dos números distintos y por
    -- eso son dos columnas: el medidor mostraba todo contra un 120000 fijo
    -- que no era la ventana de nadie (ver `experts.context_usage`).
    cost_cache_in      REAL,
    context_tokens     INTEGER,
    context_warn_tokens INTEGER,
    notes       TEXT NOT NULL DEFAULT '',
    verified_at TEXT,               -- cuándo se midió `vision`
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_models_enabled ON models(enabled, provider);

-- ADR-037 fase 2: roles. La tabla NOMBRA A LOS OWNERS — quien cruzó
-- Cloudflare Access y no está acá es `member`. Nadie necesita alta para
-- entrar: la lista de quién puede llegar vive en la policy de Access.
CREATE TABLE IF NOT EXISTS users (
    email      TEXT PRIMARY KEY,   -- siempre en minúsculas
    role       TEXT NOT NULL DEFAULT 'member',  -- owner|member
    created_at TEXT NOT NULL
);
"""

# ADR-027: FTS5 sobre los resúmenes compactados (NO sobre turnos crudos).
# Va aparte del SCHEMA porque FTS5 puede no estar compilado en el sqlite
# del sistema — best-effort: sin FTS5, search_memories devuelve [].
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    summary,
    project_slug UNINDEXED,
    conversation_id UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);
"""
