"""SQLite de configuración — ADR-013.

Guarda lo RELACIONAL y chico: proyectos, comandos dinámicos, índice de
chats y auditoría de comandos. La data conversacional sigue en `.md` +
JSONL (ADR-005): **el `.md` es la verdad, la tabla `chats` es índice**.

Las sesiones VS Code NO se persisten acá: viven en memoria
(`SessionRegistry`, ADR-009) porque la extensión re-handshakea sola.

Ponytail: sqlite3 stdlib, una conexión por operación (cheap en local,
evita dramas de threads con asyncio.to_thread), WAL mode. Sin ORM,
sin aiosqlite: el tráfico de config es ínfimo. Si algún día hay
contención real, se cambia por aiosqlite — upgrade path documentado.
"""
from __future__ import annotations

import asyncio
import calendar
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from . import config

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
    requested_by      TEXT
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
_PROJECT_COLS = (
    "slug", "name", "repo_path", "system_prompt", "mcp_servers",
    "defaults_json", "native_tools", "description", "enabled",
    "include_in_index", "night_mode_enabled", "night_config",
    "discord_channel_id",  # Iter 10.1: canal Discord default por proyecto
)
#: Cuánto del pedido se copia a `chats.user_prompt` (ver `create_chat`).
_CAP_PROMPT_FILA = 300

_COMMAND_COLS = ("name", "description", "handler", "args_schema", "enabled")
_MCP_COLS = (
    "name", "capability", "transport", "command", "args", "url", "env",
    "read_only", "on_demand", "idle_timeout_s", "enabled",
    "source_url", "source_commit", "install_dir",
    "vet_verdict", "vet_report", "health",)


#: Los únicos modelos con `vision` MEDIDA (2026-08-18, probando cada
#: endpoint con un PNG de un color liso y pidiendo el color). Reproducir
#: con `scripts/probe_vision.py`. El resto del catálogo entra por
#: `scripts/import_models.py` con vision=NULL, que es la verdad.
_MINIMAX_URL = "https://api.minimax.io/v1"
_NVIDIA_URL = "https://integrate.api.nvidia.com/v1"

#: Los únicos modelos con `vision` MEDIDA (2026-08-18, mandando a cada
#: endpoint un PNG de un color liso y pidiendo el color, con dos colores
#: distintos para que acertar de casualidad no cuente). Reproducir con
#: `scripts/probe_vision.py`. El resto del catálogo entra por
#: `scripts/import_models.py` con vision=NULL, que es la verdad.
#:
#: Dicts y no tuplas: la primera versión eran tuplas posicionales y
#: agregar `base_url` corrió todos los índices de los tests. Un seed que
#: se rompe al sumar una columna es un seed mal escrito.
_MODELS_SEED = [
    {"spec": "minimax:MiniMax-M3", "label": "MiniMax M3",
     "provider": "minimax", "base_url": _MINIMAX_URL,
     "api_key_env": "MINIMAX_API_KEY", "vision": 1, "enabled": 1,
     "cost_in": 0.3, "cost_out": 1.2, "verified_at": "2026-08-18",
     # `cost_cache_in` queda en NULL A PROPÓSITO: la tarifa de cache read
     # de MiniMax no está verificada acá, y un número inventado en la
     # columna de la plata es peor que no tenerlo (se cobra a `cost_in`
     # hasta que alguien pegue el valor del tarifario).
     "cost_cache_in": None,
     "context_tokens": 200_000, "context_warn_tokens": 92_000,
     "notes": "el default, pago; ve imágenes"},
    {"spec": "nvidia:minimaxai/minimax-m3",
     "label": "MiniMax M3 · NVIDIA (respaldo)",
     "provider": "nvidia", "base_url": _NVIDIA_URL,
     "api_key_env": "NVIDIA_API_KEY", "vision": 1, "enabled": 1,
     "cost_in": 0.0, "cost_out": 0.0, "verified_at": "2026-08-18",
     "cost_cache_in": 0.0,
     # Mismos pesos que el de arriba: misma ventana y mismo punto de
     # degradación medido.
     "context_tokens": 200_000, "context_warn_tokens": 92_000,
     "notes": "respaldo: ve imágenes pero gasta la cuota gratis de NVIDIA "
              "en un modelo que ya pagamos"},
    {"spec": "nvidia:z-ai/glm-5.2", "label": "GLM 5.2 · NVIDIA",
     "provider": "nvidia", "base_url": _NVIDIA_URL,
     "api_key_env": "NVIDIA_API_KEY", "vision": 0, "enabled": 1,
     "cost_in": 0.0, "cost_out": 0.0, "verified_at": "2026-08-18",
     "notes": "gratis; NO ve imágenes y encima no da error: contesta igual"},
    {"spec": "nvidia:nvidia/nemotron-3-ultra-550b-a55b",
     "label": "Nemotron 3 Ultra · NVIDIA",
     "provider": "nvidia", "base_url": _NVIDIA_URL,
     "api_key_env": "NVIDIA_API_KEY", "vision": 0, "enabled": 1,
     "cost_in": 0.0, "cost_out": 0.0, "verified_at": "2026-08-18",
     "notes": "gratis; solo texto (corta con 400 si mandás una imagen)"},
]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _vence_en(segundos: int) -> str:
    """`now_iso()` corrido N segundos. Mismo formato fijo y en UTC, así
    que comparar con `<` como texto es comparar fechas."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() + segundos))


#: Clave de `system_config` con la tarifa por modelo. JSON:
#:   {"minimax:MiniMax-M3": {"in": 0.3, "out": 1.2},
#:    "nvidia:*":           {"in": 0, "out": 0,
#:                           "ref_in": 0.6, "ref_out": 2.4}}
#: `in`/`out` son USD por MILLÓN de tokens y es lo que se paga.
#: `ref_*` es el precio de mercado del mismo modelo en un proveedor
#: pagado, y solo se usa para estimar cuánto ahorra el free tier; sin
#: `ref_*` el ahorro de esa entrada es 0 (no se inventa una tarifa).
MODEL_PRICES_KEY = "MODEL_PRICES"


def match_model_price(prices: dict, spec: str) -> Optional[dict]:
    """Tarifa de `spec`: exacta > patrón `prefijo*` (el más largo) > None.

    None significa "sin tarifa cargada", que NO es lo mismo que gratis:
    el costo de esa fila queda en null y la UI lo muestra como sin dato.
    Un modelo nuevo sin precio no puede aparecer como si saliera 0.
    """
    if not spec or not isinstance(prices, dict):
        return None
    exact = prices.get(spec)
    if isinstance(exact, dict):
        return exact
    best: Optional[dict] = None
    best_len = -1
    for pat, val in prices.items():
        if not isinstance(val, dict) or not pat.endswith("*"):
            continue
        head = pat[:-1]
        if spec.startswith(head) and len(head) > best_len:
            best, best_len = val, len(head)
    return best


def cost_usd(price: Optional[dict], tokens_in, tokens_out,
             *, reference: bool = False, cache_read=None) -> Optional[float]:
    """Costo en USD de un turno, o None si no hay con qué calcularlo.

    `reference=True` usa `ref_in`/`ref_out` (lo que habría costado en un
    proveedor pagado) en vez de la tarifa real; es la base del "ahorro".

    `cache_read` (2026-08-31) son los tokens de `tokens_in` que el
    provider sirvió de su caché. Se cobran a `cache_in` si la tarifa
    está cargada; si NO está, se cobran a `in` como antes — caro de más,
    nunca de menos. No aplica al modo `reference`: el ahorro del free
    tier se estima contra el precio de lista, que no tiene caché.

    Devuelve None cuando falta la tarifa O faltan los tokens: sumar un
    cero silencioso ahí haría que el total parezca completo cuando no lo
    está.
    """
    if not price or tokens_in is None or tokens_out is None:
        return None
    ki, ko = ("ref_in", "ref_out") if reference else ("in", "out")
    if ki not in price or ko not in price:
        return None
    try:
        entrada = float(tokens_in)
        salida = float(tokens_out)
        rate_in = float(price[ki])
        # max(0,…) porque `cache_read` viene del provider: si alguna vez
        # reporta más caché que entrada, el costo no puede irse a negativo.
        cacheados = 0.0
        if not reference and cache_read is not None:
            cacheados = min(max(float(cache_read), 0.0), entrada)
        rate_cache = price.get("cache_in")
        rate_cache = rate_in if rate_cache is None else float(rate_cache)
        return ((entrada - cacheados) * rate_in
                + cacheados * rate_cache
                + salida * float(price[ko])) / 1_000_000
    except (TypeError, ValueError):
        return None


def prices_from_models(rows: list) -> dict:
    """Tarifario con la forma de `MODEL_PRICES` armado desde `models`.

    2026-08-31: la pantalla Modelos guarda `cost_in`/`cost_out` por spec
    y el dashboard leía SOLO la clave `MODEL_PRICES` de system_config —
    que estaba vacía. Resultado: `priced=false` y todos los costos en
    null aunque la tarifa estuviera cargada en la tabla de al lado. Dos
    fuentes de verdad y la UI leía la vacía.

    `MODEL_PRICES` sigue ganando (ver `metrics_summary`): es el lugar
    donde se escriben los patrones `nvidia:*` y los `ref_*`, que la
    tabla no tiene. Una fila sin las DOS tarifas no entra: media tarifa
    no es una tarifa.
    """
    out: dict = {}
    for row in rows or []:
        r = dict(row)
        spec = r.get("spec")
        if not spec or r.get("cost_in") is None or r.get("cost_out") is None:
            continue
        precio = {"in": r["cost_in"], "out": r["cost_out"]}
        if r.get("cost_cache_in") is not None:
            precio["cache_in"] = r["cost_cache_in"]
        out[spec] = precio
    return out


def read_system_config_sync(key: str, default: str = "") -> str:
    """Lectura sync de system_config para ANTES de arrancar el loop.

    `main()` necesita RELAY_HOST para el bind de aiohttp, que ocurre
    antes de que exista la app (y su Database async). Tolera DB o
    tabla inexistentes: primer arranque devuelve el default.
    """
    path = config.db_path()
    try:
        conn = sqlite3.connect(path, timeout=5)
        try:
            cur = conn.execute(
                "SELECT value FROM system_config WHERE key=?", (key,))
            row = cur.fetchone()
            return row[0] if row else default
        finally:
            conn.close()
    except sqlite3.Error:
        return default


class Database:
    """Repos de config sobre SQLite. Métodos async (to_thread)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or config.db_path()
        # Se resuelve en init_schema(); False hasta entonces (ADR-027).
        self._fts_available = False
        # Serializa _upsert. Bug fix 2026-07-18: dos upserts concurrentes
        # del mismo slug pasaban el SELECT (ambos creían "no existe"), uno
        # insertaba OK y el otro se llevaba IntegrityError → 500. El lock
        # evita la doble escritura a nivel de coroutine; no necesita
        # saber de threads porque asyncio.Lock igual serializa el await.
        self._upsert_lock = asyncio.Lock()

    # ---- infra ----

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            # Migración best-effort para DBs existentes (iter 4.5, 2026-07-07):
            # la columna native_tools se agregó después. Si la tabla ya
            # existía, CREATE TABLE IF NOT EXISTS no la agrega — ALTER TABLE
            # la crea si falta. swallow el error si ya existe.
            try:
                conn.execute("ALTER TABLE task_file_claims ADD COLUMN vence_at TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "ALTER TABLE task_file_claims ADD COLUMN "
                    "project_slug TEXT NOT NULL DEFAULT ''")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            for _col, _tipo in (("intentos", "INTEGER NOT NULL DEFAULT 0"),
                                ("ultimo_error", "TEXT"),
                                ("proximo_intento_at", "TEXT")):
                try:
                    conn.execute(
                        f"ALTER TABLE chat_outputs ADD COLUMN {_col} {_tipo}")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass
            try:
                conn.execute("ALTER TABLE projects ADD COLUMN native_tools TEXT NOT NULL DEFAULT '[]'")
                conn.commit()
            except sqlite3.OperationalError:
                # column already exists, ignore
                pass
            # Migración iter 4.8: include_in_index (admin UI toggle).
            try:
                conn.execute("ALTER TABLE projects ADD COLUMN include_in_index INTEGER NOT NULL DEFAULT 1")
                conn.commit()
            except sqlite3.OperationalError:
                # column already exists, ignore
                pass
            # Migración iter 5.1 (ADR-025): chats.conversation_id para
            # auditar qué runs pertenecen a qué conversación.
            try:
                conn.execute("ALTER TABLE chats ADD COLUMN conversation_id TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            # Migración Fase 1 (liveness on-demand + progress): persistir
            # la última fase y última tool al cerrar el run, para
            # diagnóstico post-mortem. Decisión 6.
            try:
                conn.execute("ALTER TABLE chats ADD COLUMN phase_at_end TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE chats ADD COLUMN last_tool TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            # Migración iter 9.8: notes-workspace necesita `model`,
            # `duration_ms` y `tool_calls` en la fila de chats. Best-effort.
            for _col, _def in [
                ("model", "TEXT"),
                ("duration_ms", "INTEGER"),
                ("tool_calls", "INTEGER"),
            ]:
                try:
                    conn.execute(
                        f"ALTER TABLE chats ADD COLUMN {_col} {_def}")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass
            # Migración 2026-07-26: sugerencias de continuación. JSON
            # array de strings (los "próximos pasos" que la UI y Discord
            # pintan como botones). NULL = run sin sugerencias.
            try:
                conn.execute("ALTER TABLE chats ADD COLUMN suggestions TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            # Migración iter 10.0: conversaciones necesitan discord_user_id
            # y discord_author para el bridge Discord↔UI. Nullable porque
            # las conversaciones nacidas en UI no tienen autor Discord.
            for _col in ("discord_user_id", "discord_author"):
                try:
                    conn.execute(
                        f"ALTER TABLE conversations ADD COLUMN {_col} TEXT")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_conv_discord_user "
                    "ON conversations(discord_user_id)")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            # Seguimiento por GitHub (fase 4): la conversación puede nacer
            # de un issue. Guardamos SOLO el número — el título y el cuerpo
            # se leen de GitHub en cada run (cacheados 60s) para que editar
            # el issue se refleje sin resincronizar nada.
            try:
                conn.execute(
                    "ALTER TABLE conversations ADD COLUMN issue_number INTEGER")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            # Migración Iter 5 (ADR-028): opt-in del modo nocturno por
            # proyecto + config JSON opcional (defaults en night.py).
            try:
                conn.execute("ALTER TABLE projects ADD COLUMN night_mode_enabled INTEGER NOT NULL DEFAULT 0")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE projects ADD COLUMN night_config TEXT NOT NULL DEFAULT '{}'")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            # Migración Iter 5.1 (ADR-028 enmienda): rama ÚNICA + PR
            # único al final del run. Idempotente.
            # Migración Iter 9.7: opt-in por proyecto del modo
            # interactivo (checkpoints entre bloques + fallback en
            # fallos). Default 0 = comportamiento actual, opt-in
            # explícito (UPDATE projects SET interactive_mode=1 ...).
            try:
                conn.execute(
                    "ALTER TABLE projects ADD COLUMN interactive_mode "
                    "INTEGER NOT NULL DEFAULT 0")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            # Migración Iter 9.8 (plan DAYMODE_DISCORD): mapea
            # proyecto → canal Discord donde el bot manda los
            # embeds de checkpoint. Si no está, el bot no manda
            # embeds (la pregunta igual existe en DB y se puede
            # responder por admin UI). Idempotente.
            try:
                conn.execute(
                    "ALTER TABLE projects ADD COLUMN discord_channel_id TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            # CRM (read-only): link projects → crm_clients.
            # ON DELETE SET NULL: si se borra un crm_client en el CRM
            # y el próximo sync lo refleja en el relay, no queremos
            # perder el project histórico.
            try:
                conn.execute(
                    "ALTER TABLE projects ADD COLUMN client_id INTEGER "
                    "REFERENCES crm_clients(id) ON DELETE SET NULL")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            # Migración legacy (2026-08-11). La columna se llamó
            # `hs_company_id` mientras el CRM era el de un proveedor
            # externo; la fuente pasó a ser el Postgres de trycompai/crm
            # y ahora guarda el cuid de `company.id`, así que el nombre
            # cambió a `ext_id`.
            #
            # El ALTER se queda aunque el proveedor ya no exista: una
            # base creada antes de esa fecha todavía tiene la columna
            # vieja, y sin esto el arranque revienta al primer query.
            # Idempotente: si ya se renombró, falla con
            # OperationalError y seguimos.
            try:
                conn.execute(
                    "ALTER TABLE crm_clients "
                    "RENAME COLUMN hs_company_id TO ext_id")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            # Migración deal→proyecto: un proyecto es un deal del CRM, y
            # los deals cuelgan de una company. Guarda el cuid de
            # `deal.id`; `client_id` sigue existiendo y se deriva del deal
            # (un proyecto con deal SIEMPRE tiene el cliente del deal).
            # No es FK: los deals viven en deals_json, no en tabla propia.
            try:
                conn.execute("ALTER TABLE projects ADD COLUMN deal_id TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            # Migración digest de silencio: último email/reunión/nota por
            # cliente, para marcar "sin contacto hace N días" sin recorrer
            # deals_json en cada request.
            try:
                conn.execute(
                    "ALTER TABLE crm_clients ADD COLUMN last_activity_at TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            # Migración Iter 5.1 (ADR-028 enmienda): rama ÚNICA + PR
            # único al final del run. Idempotente.
            for col in ("branch", "pr_url"):
                try:
                    conn.execute(f"ALTER TABLE night_runs ADD COLUMN {col} TEXT")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass
            # Migración flujo git en conversaciones (/nuevo → rama, /cerrar
            # → PR a develop): autor (para el nombre de rama), rama activa
            # y URL del PR abierto al cerrar. Idempotente.
            for col in ("author", "branch", "pr_url"):
                try:
                    conn.execute(f"ALTER TABLE conversations ADD COLUMN {col} TEXT")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass
            # Migración supersede de facts (2026-07-14): hechos obsoletos
            # se marcan, no se borran. Idempotente.
            for col in ("superseded_at", "superseded_by"):
                try:
                    conn.execute(f"ALTER TABLE facts ADD COLUMN {col} TEXT")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass
            # Aprobación de facts (2026-08-21). `status` NO puede ser NULL
            # ni quedar vacío: la inyección filtra por él, y una fila sin
            # estado sería un hecho invisible para siempre. Los que ya
            # estaban quedan `approved` — existían desde antes de que la
            # aprobación existiera, y mandarlos a revisar sería un backlog
            # de 532 items nacido de un ALTER TABLE.
            try:
                conn.execute(
                    "ALTER TABLE facts ADD COLUMN status TEXT NOT NULL "
                    "DEFAULT 'approved'")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE facts ADD COLUMN reviewed_at TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            # 2026-07-26: peso de los tool results (JSON por tool). Sirve
            # para elegir los caps con datos: un result se reenvía en cada
            # vuelta posterior, así que lo que importa no es su tamaño sino
            # su tamaño × las vueltas que le quedan al run.
            # `stages_json` (2026-08-14) viaja en la misma lista: mismo
            # tipo, mismo patrón idempotente. Antes de esto el plan, el
            # veredicto del verificador y el modelo de cada etapa solo
            # existían en memoria durante el run — con cada etapa en un
            # proveedor distinto, no se podía auditar quién hizo qué.
            for col in ("progress_events", "tool_bytes", "stages_json"):
                try:
                    conn.execute(f"ALTER TABLE chats ADD COLUMN {col} TEXT")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass
            # Fase 1 identidad (2026-08-17): quién pidió el run / abrió la
            # conversación. NULL en todo lo anterior a esta migración, que
            # es la respuesta honesta: de esas filas no sabemos.
            for _tbl in ("chats", "conversations"):
                try:
                    conn.execute(
                        f"ALTER TABLE {_tbl} ADD COLUMN requested_by TEXT")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass
            for col in ("trimmed", "trimmed_turns"):
                try:
                    conn.execute(
                        f"ALTER TABLE chats ADD COLUMN {col} INTEGER DEFAULT 0")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass
            # 2026-08-30: el pedido del humano, recortado, en la fila del
            # chat. El .md sigue siendo la verdad; esto existe porque el
            # panel del plan necesita un TÍTULO por turno y lo pedía como
            # `user_prompt` a una fila que nunca lo tuvo — todos los nodos
            # del grafo sintético salían con el título vacío. Leerlo del
            # .md serían N lecturas de disco cada 2,5s de polling.
            try:
                conn.execute("ALTER TABLE chats ADD COLUMN user_prompt TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            # ADR-037 fase 2: el owner tiene que existir antes del primer
            # request, si no el relay queda sin nadie que pueda tocar la
            # config. RELAY_OWNER_EMAIL lo pisa; sin valor por defecto no se
            # atribuye una instalación nueva a una persona. INSERT OR IGNORE:
            # si alguien se
            # degradó a mano, el boot no lo vuelve a promover.
            _owner = os.environ.get("RELAY_OWNER_EMAIL", "").strip().lower()
            if _owner:
                conn.execute(
                    "INSERT OR IGNORE INTO users (email, role, created_at) "
                    "VALUES (?, 'owner', ?)", (_owner, now_iso()))
            # api_key llegó después de la tabla (2026-08-18).
            try:
                conn.execute("ALTER TABLE models ADD COLUMN api_key TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            # 2026-08-31: caché y ventana real. Ver los comentarios en
            # SCHEMA. Los runs y los modelos anteriores quedan en NULL,
            # que es la verdad: de esos no se midió.
            for _tbl, _col, _tipo in (
                ("chats", "cache_read_tokens", "INTEGER"),
                ("models", "cost_cache_in", "REAL"),
                ("models", "context_tokens", "INTEGER"),
                ("models", "context_warn_tokens", "INTEGER"),
            ):
                try:
                    conn.execute(
                        f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_tipo}")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass
            # bitacora_json llegó después de la tabla (2026-08-26).
            try:
                conn.execute(
                    "ALTER TABLE conversations ADD COLUMN bitacora_json TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            # Catálogo de modelos: siembra SOLO los cuatro medidos a
            # mano el 2026-08-18. El resto se trae con
            # `scripts/import_models.py` y entra apagado y sin medir.
            # INSERT OR IGNORE: si prendiste, apagaste o le pusiste
            # tarifa a uno, el boot no te lo pisa.
            for _m in _MODELS_SEED:
                _cols = ", ".join(_m)
                _qs = ",".join("?" * len(_m))
                conn.execute(
                    f"INSERT OR IGNORE INTO models ({_cols}) VALUES ({_qs})",
                    tuple(_m.values()))
                # Backfill para las DBs sembradas antes de que el seed
                # trajera base_url: sin URL en la fila, la tabla no
                # alcanza ni para armar el modelo ni para medirlo.
                conn.execute(
                    "UPDATE models SET base_url=? "
                    "WHERE spec=? AND (base_url IS NULL OR base_url='')",
                    (_m["base_url"], _m["spec"]))
                # Mismo backfill para las columnas de 2026-08-31 (ventana
                # real y tarifa de caché): las filas del seed ya existen en
                # cualquier DB viva, así que el INSERT OR IGNORE de arriba
                # nunca les habría puesto estos valores. Solo pisa NULL —
                # si lo editaste desde la pantalla Modelos, queda como está.
                for _col in ("cost_cache_in", "context_tokens",
                             "context_warn_tokens"):
                    if _m.get(_col) is None:
                        continue
                    conn.execute(
                        f"UPDATE models SET {_col}=? "
                        f"WHERE spec=? AND {_col} IS NULL",
                        (_m[_col], _m["spec"]))
            # Sprint 1: seed de configs de context meter.
            # CONTEXT_LIMIT_TOKENS = "0" significa "derivalo de la ventana
            # del modelo" (la función que lo derivaba, _context_trim_limit,
            # fue borrada con el sprint de trim por turnos; el seed queda
            # porque system_config lo expone y un futuro trim puede
            # reintroducir la lectura). El 48000 fijo que había acá era
            # un numero suelto que no se correspondia con ninguna ventana
            # real y hacia trimear hilos que estaban al 11%.
            for key, val in [
                ("CONTEXT_LIMIT_TOKENS", "0"),
                ("CONTEXT_TRIM_STRATEGY", "drop_oldest"),
                ("CONTEXT_WARN_AT", "0.8"),
            ]:
                conn.execute(
                    "INSERT OR IGNORE INTO system_config (key, value) "
                    "VALUES (?, ?)", (key, val))
            # Migración Fase 2A (subdivisión automática, 2026-09-02):
            # `tasks.parent_id` — el nodo que originó esta subtarea
            # (`add_tasks_to_graph(..., reemplaza=X)`). SOLO agrupa
            # visualmente ("cada caja del grafo es un proceso con
            # subtareas adentro"); el scheduler no la lee, la ejecución
            # sigue plana.
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN parent_id TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            # Migracion: `task_graphs.verificacion_json` (2026-09-08). El
            # veredicto de la verificacion de cierre del grafo. Mismo
            # patron que el resto: ALTER TABLE best-effort, si ya existe
            # se ignora.
            try:
                conn.execute(
                    "ALTER TABLE task_graphs ADD COLUMN verificacion_json TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            # Migración F0 (plan MCP_REGISTRY): blob projects.mcp_servers →
            # catálogo mcp_servers. One-shot (flag en system_config): si
            # después borras una fila del catálogo, no resucita en el boot.
            self._migrate_mcp_blob(conn)
            # 2026-08-16: un solo browser. Saca obscura del catálogo.
            self._retire_obscura(conn)
            # 2026-08-16: el wrapper MCP se retira (VS Code ya no se usa
            # y sus tools son nativas del relay).
            self._retire_wrapper(conn)
            # 2026-08-16: catálogo base de MCPs on-demand (context7, fetch).
            # Va DESPUÉS de los retires: si un día uno de estos se retira,
            # el retire tiene que correr sobre el catálogo ya sembrado.
            self._seed_mcps(conn)
            # ADR-027: FTS5 best-effort (puede no estar compilado).
            try:
                conn.executescript(FTS_SCHEMA)
                self._fts_available = True
            except sqlite3.OperationalError as e:
                import logging
                logging.getLogger("relay.db").warning(
                    "FTS5 no disponible (%s): search_memories devolvera []", e)
                self._fts_available = False
        finally:
            conn.close()

    async def init_schema(self) -> None:
        await asyncio.to_thread(self._init_schema_sync)

    @staticmethod
    def _retire_wrapper(conn: sqlite3.Connection) -> None:
        """Saca `4bis-wrapper` del catálogo. One-shot e idempotente.

        Decisión 2026-08-16: el usuario dejó de usar VS Code, así que el
        wrapper perdió su consumidor propio y quedaba solo como una capa
        con sus caps —6.000 chars de salida de shell, 256 KB de lectura,
        60 s de timeout— que ganaban por estar más adentro en la cadena.
        Sus siete tools son ahora nativas del relay (`relay/files.py` y
        `relay/shell.py`). Ver docs/WRAPPER.md.

        Mismo contrato que `_retire_obscura`: el flag en `system_config`
        hace que no se re-ejecute ni resucite. Si alguien vuelve a usar
        VS Code y quiere el wrapper de vuelta, lo agrega desde la Admin UI
        y el boot siguiente lo respeta — pero conviene apagarle las
        nativas al proyecto (`native_files`/`native_shell` en false) o el
        relay las va a esconder igual.
        """
        done = conn.execute(
            "SELECT 1 FROM system_config WHERE key='wrapper_retired'"
        ).fetchone()
        if done:
            return
        cur = conn.execute("DELETE FROM mcp_servers WHERE name='4bis-wrapper'")
        if cur.rowcount:
            import logging
            logging.getLogger("relay.db").info(
                "4bis-wrapper retirado del catálogo (%d fila): sus tools son "
                "nativas del relay", cur.rowcount)
        conn.execute(
            "INSERT INTO system_config (key, value) VALUES "
            "('wrapper_retired', '1')")
        conn.commit()

    # Catálogo base de MCPs on-demand (2026-08-16). Pedido: *"agregá los
    # MCP que encuentres necesarios, son on-demand y la idea es que el
    # relay tenga buenas herramientas para hacer buenos desarrollos"*.
    #
    # El criterio para entrar es el mismo con el que salieron obscura y
    # el wrapper: **un MCP tiene que traer algo que el relay no tenga**.
    # Por eso NO están, aunque existan y funcionen:
    #
    #   - `server-filesystem` y `mcp-server-git`: `files.py` y `shell.py`
    #     ya hacen eso, nativo y sin subprocess. Git por shell además es
    #     más completo que las 13 tools del MCP.
    #   - `server-memory`: sería una segunda memoria al lado de cbm, o
    #     sea dos fuentes de verdad — justo lo que venimos sacando.
    #   - Cualquier segundo browser: uno solo (docs/BROWSER_UNICO.md).
    #
    # Ambos entran `on_demand=1`: duermen hasta que un run los pide con
    # `--con docs` o el experto llama `use_capability`, y el reaper los
    # apaga al pasar el idle. Un MCP dormido no cuesta nada.
    _SEED_MCPS = (
        {
            # Docs de librerías al día, por nombre. Es lo que evita que el
            # modelo escriba la API que recordaba de su corte: para un
            # relay que tiene que producir código que compile, vale más
            # que cualquier otra tool de lectura.
            "name": "context7",
            "capability": "docs",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@upstash/context7-mcp@4.0.2"],
            "read_only": 1,
            "on_demand": 1,
            "enabled": 1,
            "idle_timeout_s": 300,
        },
        {
            # Traer una URL como markdown. Complementa a context7 para lo
            # que no está indexado ahí: un changelog, un RFC, la doc de la
            # API de un cliente. El browser también puede, pero levantar
            # Chromium para leer una página de texto es carísimo al lado
            # de esto.
            "name": "fetch",
            "capability": "web",
            "transport": "stdio",
            "command": "uvx",
            # `mcp<2` no es opcional: 2.x renombró `McpError` → `MCPError`
            # y mcp-server-fetch explota al importar. Sin el pin, la fila
            # se siembra y queda en `handshake_failed` para siempre (pasó:
            # se detectó el 2026-08-16 al registrar postgres-mcp).
            "args": ["--with", "mcp<2", "mcp-server-fetch"],
            "read_only": 1,
            "on_demand": 1,
            "enabled": 1,
            "idle_timeout_s": 180,
        },
        {
            # EXPLAIN, salud del motor y sugerencia de índices sobre un
            # Postgres. NO se pisa con `db_query` (dbtool.py): ejecutar
            # SQL ya lo hacemos nativo; lo que no tenemos es el análisis,
            # que necesita pg_stat_statements y un parser SQL.
            #
            # Capability `postgres` y no `database`: esa es del MCP de
            # sqlite y solo se adjunta UNO por capacidad, así que
            # compartirla lo desplazaría en silencio.
            #
            # Necesita POSTGRES_MCP_URI en el .env — el catálogo guarda la
            # ref, nunca la credencial. Sin la variable el toolset no se
            # arma y el run sigue sin esta capacidad.
            "name": "postgres-mcp",
            "capability": "postgres",
            "transport": "stdio",
            "command": "uvx",
            "args": ["--with", "mcp<2", "postgres-mcp",
                     "--access-mode=restricted"],
            "env": {"DATABASE_URI": "env:POSTGRES_MCP_URI"},
            "read_only": 1,
            "on_demand": 1,
            "enabled": 1,
            "idle_timeout_s": 300,
        },
    )

    @staticmethod
    def _seed_mcps(conn: sqlite3.Connection) -> None:
        """Siembra el catálogo base de MCPs. One-shot e idempotente.

        Same contrato que `_retire_*`: el flag en `system_config` hace que
        no se re-ejecute. Eso importa acá más que en un retire — si esto
        corriera en cada boot, borrar un MCP que no querés lo resucitaría
        al reiniciar, y no habría forma de sacártelo de encima.

        Tampoco pisa lo que ya exista con ese nombre: si alguien lo
        configuró a mano (otra versión, otro `env`), su fila gana.
        """
        done = conn.execute(
            "SELECT 1 FROM system_config WHERE key='mcps_seeded_v1'"
        ).fetchone()
        if done:
            return
        import json as _json
        import logging

        puestos = []
        for m in Database._SEED_MCPS:
            ya = conn.execute("SELECT 1 FROM mcp_servers WHERE name=?",
                              (m["name"],)).fetchone()
            if ya:
                continue
            conn.execute(
                "INSERT INTO mcp_servers (name, capability, transport, "
                "command, args, env, read_only, on_demand, idle_timeout_s, "
                "enabled) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (m["name"], m["capability"], m["transport"], m["command"],
                 _json.dumps(m["args"]), _json.dumps(m.get("env") or {}),
                 m["read_only"], m["on_demand"],
                 m["idle_timeout_s"], m["enabled"]))
            puestos.append(m["name"])
        conn.execute(
            "INSERT INTO system_config (key, value) VALUES "
            "('mcps_seeded_v1', '1')")
        conn.commit()
        if puestos:
            logging.getLogger("relay.db").info(
                "MCPs on-demand sembrados: %s (duermen hasta que un run los "
                "pida; necesitan npx / uvx en el PATH)", ", ".join(puestos))

    @staticmethod
    def _retire_obscura(conn: sqlite3.Connection) -> None:
        """Saca `obscura` del catálogo de MCPs. One-shot e idempotente.

        Decisión 2026-08-16: un solo browser. Obscura prometía bien
        (36 tools, salida en texto) pero falla seguido, y tener dos MCPs
        declarando la capability `browser` obligaba a un desempate que
        además elegía obscura por orden alfabético — así que
        `use_capability("browser")` nunca llegaba a playwright. El browser
        del relay es `playwright-mcp` (`mcp_servers/playwright_mcp.py`).

        Borrar la fila alcanza: `project_mcp_servers` tiene
        `ON DELETE CASCADE`, así que los links se van con ella.

        El flag en `system_config` es lo que hace que esto NO resucite ni
        se vuelva a ejecutar: si mañana querés obscura de vuelta, la
        agregás desde la Admin UI y el boot siguiente la respeta. Mismo
        contrato que `_migrate_mcp_blob`.
        """
        done = conn.execute(
            "SELECT 1 FROM system_config WHERE key='obscura_retired'"
        ).fetchone()
        if done:
            return
        cur = conn.execute("DELETE FROM mcp_servers WHERE name='obscura'")
        if cur.rowcount:
            import logging
            logging.getLogger("relay.db").info(
                "obscura retirado del catálogo (%d fila): el browser del "
                "relay es playwright-mcp", cur.rowcount)
        conn.execute(
            "INSERT INTO system_config (key, value) VALUES "
            "('obscura_retired', '1')")
        conn.commit()

    @staticmethod
    def _migrate_mcp_blob(conn: sqlite3.Connection) -> None:
        """Migra el blob legacy projects.mcp_servers al catálogo (F0).

        Desvío deliberado del plan §3: los 44 blobs eran idénticos
        (wrapper con FOURBIS_WORKSPACE, que build_toolsets ya inyecta
        per-run desde repo_path), así que el wrapper se siembra como
        UNA fila global SIN links (= sirve a todos, incluidos proyectos
        futuros) en vez de 44 links redundantes. Cualquier otra entrada
        del blob (hoy: ninguna) se migra como fila propia + link a su
        proyecto. El blob queda intacto como audit/rollback.
        """
        done = conn.execute(
            "SELECT 1 FROM system_config WHERE key='mcp_blob_migrated'"
        ).fetchone()
        if done:
            return
        # 2026-08-16: el wrapper ya NO se siembra. Sus tools son nativas
        # del relay (files.py / shell.py) y `_retire_wrapper` saca la fila
        # de las bases que ya la tenían. Sembrarlo acá lo resucitaría en
        # una instalación nueva.
        for row in conn.execute("SELECT id, mcp_servers FROM projects"):
            try:
                blob = json.loads(row[1] or "[]")
            except (json.JSONDecodeError, TypeError):
                blob = []
            for cfg in blob:
                name = (cfg.get("name") or "").strip()
                if not name or name == "4bis-wrapper":
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO mcp_servers (name, capability, "
                    "transport, command, args, url, env, enabled, health) "
                    "VALUES (?, 'files', ?, ?, ?, ?, ?, 1, 'unknown')",
                    (name, cfg.get("transport", "stdio"), cfg.get("command"),
                     json.dumps(cfg.get("args", [])), cfg.get("url"),
                     json.dumps(cfg.get("env", {}))))
                conn.execute(
                    "INSERT OR IGNORE INTO project_mcp_servers "
                    "(project_id, mcp_id) SELECT ?, id FROM mcp_servers "
                    "WHERE name=?", (row[0], name))
        conn.execute(
            "INSERT INTO system_config (key, value) VALUES "
            "('mcp_blob_migrated', '1')")
        conn.commit()

    def _run(self, sql: str, params: tuple = ()) -> list[dict]:
        # nota: `with conn` de sqlite3 solo commitea, NO cierra — cerramos
        # explícito para no dejar handles vivos (Windows se ofende).
        conn = self._connect()
        try:
            cur = conn.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
            conn.commit()
            return rows
        finally:
            conn.close()

    async def run(self, sql: str, params: tuple = ()) -> list[dict]:
        return await asyncio.to_thread(self._run, sql, params)

    def _run_tx(self, sentencias: list) -> None:
        """Varias sentencias en UNA transacción: entran todas o ninguna.

        `run()` abre y commitea una conexión POR sentencia, así que una
        escritura de varias filas queda a medias si la tercera falla. Eso
        no es teórico: `create_task_graph` insertaba el grafo y después
        sus tareas en llamadas sueltas, y el 24/8 un choque de ids dejó
        un grafo `activo` con CERO tareas — que además bloquea el hilo,
        porque `active_task_graph` lo devuelve y no deja armar otro.
        """
        conn = self._connect()
        try:
            try:
                for sql, params in sentencias:
                    conn.execute(sql, params)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    async def run_tx(self, sentencias: list) -> None:
        await asyncio.to_thread(self._run_tx, sentencias)

    # ---- projects ----

    async def list_projects(self, enabled_only: bool = True) -> list[dict]:
        where = "WHERE enabled=1" if enabled_only else ""
        rows = await self.run(f"SELECT * FROM projects {where} ORDER BY slug")
        return [self._parse_project(r) for r in rows]

    async def get_project(self, slug: str) -> Optional[dict]:
        # NOCASE: los targets de VS Code llegan como "INVENTORYDEMO" y los slugs
        # se guardan "inventorydemo" — el lookup no debe depender del caso.
        rows = await self.run(
            "SELECT * FROM projects WHERE slug=? COLLATE NOCASE", (slug,))
        return self._parse_project(rows[0]) if rows else None

    @staticmethod
    def _parse_project(row: dict) -> dict:
        row = dict(row)
        # Expandir `~` en repo_path: el seed de `notes` guarda
        # "~/.4bis/notes" y los consumidores (git_flow.is_git_repo,
        # subprocess cwd=, filesystem) NO expanden el tilde → cwd
        # inexistente → OSError 500 en /conversations.
        # Solo tocamos paths con `~`: los repos reales ya son absolutos
        # y se guardan con forward-slash (C:/Users/...). Usamos as_posix()
        # para MANTENER esa convención (str(Path(...)) los pasaría a
        # backslash, cambiando la representación de un path que otros
        # comparan por string). Ver _cbm_project_name (slash-agnóstico).
        rp = row.get("repo_path")
        if rp and rp.startswith("~"):
            row["repo_path"] = Path(rp).expanduser().as_posix()
        for col in ("mcp_servers", "defaults_json", "native_tools"):
            try:
                default = "[]" if col in ("mcp_servers", "native_tools") else "{}"
                row[col] = json.loads(row[col] or default)
            except (json.JSONDecodeError, TypeError):
                row[col] = [] if col in ("mcp_servers", "native_tools") else {}
        row["enabled"] = bool(row["enabled"])
        row["include_in_index"] = bool(row.get("include_in_index", 1))
        # ADR-028: modo nocturno (opt-in) + config JSON con defaults en código.
        row["night_mode_enabled"] = bool(row.get("night_mode_enabled", 0))
        try:
            row["night_config"] = json.loads(row.get("night_config") or "{}")
        except (json.JSONDecodeError, TypeError):
            row["night_config"] = {}
        return row

    async def upsert_project(self, p: dict) -> dict:
        """Crea o actualiza por slug. Solo toma columnas conocidas."""
        data = {k: p[k] for k in _PROJECT_COLS if k in p}
        if "slug" not in data:
            raise ValueError("slug requerido")
        for col in ("mcp_servers", "defaults_json", "native_tools", "night_config"):
            if col in data and not isinstance(data[col], str):
                data[col] = json.dumps(data[col], ensure_ascii=False)
        for col in ("enabled", "include_in_index", "night_mode_enabled"):
            if col in data:
                data[col] = 1 if data[col] else 0
        await self._upsert("projects", "slug", data)
        return (await self.get_project(data["slug"]))  # type: ignore[return-value]

    async def _upsert(self, table: str, key: str, data: dict) -> None:
        """INSERT o UPDATE parcial por clave. No usamos ON CONFLICT
        porque valida el INSERT completo (NOT NULL) antes de resolver
        el conflicto — rompería updates parciales."""
        async with self._upsert_lock:
            existing = await self.run(
                f"SELECT 1 FROM {table} WHERE {key}=?", (data[key],))
            if existing:
                sets = {c: v for c, v in data.items() if c != key}
                if not sets:
                    return
                clause = ", ".join(f"{c}=?" for c in sets)
                await self.run(
                    f"UPDATE {table} SET {clause}, updated_at=datetime('now') "
                    f"WHERE {key}=?", (*sets.values(), data[key]))
            else:
                cols = ", ".join(data)
                marks = ", ".join("?" for _ in data)
                await self.run(
                    f"INSERT INTO {table} ({cols}) VALUES ({marks})",
                    tuple(data.values()))

    async def disable_project(self, slug: str) -> bool:
        """Soft delete: enabled=0, no rompe historial."""
        rows = await self.run(
            "UPDATE projects SET enabled=0, updated_at=datetime('now') "
            "WHERE slug=? RETURNING id", (slug,))
        return bool(rows)

    # ---- notes workspace (iter 9.8 — single-pair con UI chats) ----

    NOTES_SLUG = "notes"   # slug del proyecto auto-creado para notas/bitácora
    NOTES_ROOT_DEFAULT = "~/.4bis/notes"  # carpeta por default

    async def ensure_notes_project(
        self, *, root_dir: Optional[str] = None,
    ) -> Optional[dict]:
        """Auto-seed del proyecto `notes`. Idempotente.

        Si ya existe (incluso disabled) lo devuelve. Si no, lo crea
        con system_prompt dedicado y `enabled=1`. La carpeta en
        disco se crea afuera (DB no toca filesystem por ahora —
        ver server.py:_on_startup).

        Devuelve el dict del proyecto o None si el slug está
        ocupado por OTRO proyecto no-notes (defensiva: no pisar
        proyectos reales del usuario).
        """
        existing = await self.get_project(self.NOTES_SLUG)
        if existing is not None:
            return existing
        # Defensive check: si alguien ya tiene un proyecto en ese
        # slug (no debería pasar), no pisa.
        check = await self.run(
            "SELECT slug FROM projects WHERE slug=?",
            (self.NOTES_SLUG,))
        if check:
            return None
        path = root_dir or self.NOTES_ROOT_DEFAULT
        await self.upsert_project({
            "slug": self.NOTES_SLUG,
            "name": "Notas / bitácora",
            "repo_path": path,
            "system_prompt": (
                "Eres el asistente de notas/bitácora del usuario. "
                "Tu trabajo es ayudar a fijar ideas, decisiones, tareas "
                "sueltas y borradores. NO necesitas aprobar nada ni pedir "
                "permiso: si el usuario tira una idea, la guardas en el "
                "formato que pida. Si pide procesar/expandir/resumir, lo "
                "haces. Si te pide crear archivos en el workspace, usás "
                "las tools de filesystem disponibles. Responde siempre en "
                "español, sin emojis salvo que el usuario "
                "los pida, y trata los archivos del workspace como de él: "
                "leelos cuando haga falta, escribe solo cuando lo pida o "
                "lo implique el contexto."
            ),
            "description": "Workspace default para notas, ideas y decisiones sin repo",
            "mcp_servers": [],
            "defaults_json": {"timeout": 180},  # 3min es suficiente para notas
            "native_tools": [],  # sin cbm en este workspace
            "enabled": 1,
            "include_in_index": 0,
        })
        return await self.get_project(self.NOTES_SLUG)

    # ---- commands ----

    async def list_commands(self, enabled_only: bool = True) -> list[dict]:
        where = "WHERE enabled=1" if enabled_only else ""
        rows = await self.run(f"SELECT * FROM commands {where} ORDER BY name")
        return [self._parse_command(r) for r in rows]

    async def get_command(self, name: str) -> Optional[dict]:
        rows = await self.run("SELECT * FROM commands WHERE name=?", (name,))
        return self._parse_command(rows[0]) if rows else None

    async def delete_command(self, name: str) -> bool:
        """Borra un command por nombre. Devuelve True si existía y borró."""
        existing = await self.get_command(name)
        if not existing:
            return False
        await self.run("DELETE FROM commands WHERE name=?", (name,))
        return True

    async def delete_project(self, slug: str) -> bool:
        """Borra un project por slug. Hard delete (no soft)."""
        existing = await self.get_project(slug)
        if not existing:
            return False
        await self.run("DELETE FROM projects WHERE slug=?", (slug,))
        return True

    # ---- ignored_orphans ----

    async def list_ignored_orphans(self) -> list[str]:
        """Devuelve los cbm_name que el usuario eligió ignorar en /cbm/orphans."""
        rows = await self.run("SELECT cbm_name FROM ignored_orphans")
        return [r["cbm_name"] for r in rows]

    async def ignore_orphan(self, cbm_name: str, reason: str = "") -> bool:
        """Marca un cbm_name como ignorado. Devuelve True si se insertó."""
        try:
            await self.run(
                "INSERT INTO ignored_orphans (cbm_name, reason) VALUES (?, ?)",
                (cbm_name, reason),
            )
            return True
        except Exception:  # noqa: BLE001
            # PRIMARY KEY colisión = ya estaba.
            return False

    async def unignore_orphan(self, cbm_name: str) -> bool:
        existing = await self.run(
            "SELECT 1 FROM ignored_orphans WHERE cbm_name=?", (cbm_name,),
        )
        if not existing:
            return False
        await self.run(
            "DELETE FROM ignored_orphans WHERE cbm_name=?", (cbm_name,),
        )
        return True

    @staticmethod
    def _parse_command(row: dict) -> dict:
        row = dict(row)
        if row.get("args_schema"):
            try:
                row["args_schema"] = json.loads(row["args_schema"])
            except json.JSONDecodeError:
                row["args_schema"] = None
        row["enabled"] = bool(row["enabled"])
        return row

    async def upsert_command(self, c: dict) -> dict:
        data = {k: c[k] for k in _COMMAND_COLS if k in c}
        if "name" not in data:
            raise ValueError("name requerido")
        if "args_schema" in data and data["args_schema"] is not None \
                and not isinstance(data["args_schema"], str):
            data["args_schema"] = json.dumps(data["args_schema"], ensure_ascii=False)
        if "enabled" in data:
            data["enabled"] = 1 if data["enabled"] else 0
        await self._upsert("commands", "name", data)
        return (await self.get_command(data["name"]))  # type: ignore[return-value]

    async def disable_command(self, name: str) -> bool:
        rows = await self.run(
            "UPDATE commands SET enabled=0, updated_at=datetime('now') "
            "WHERE name=? RETURNING id", (name,))
        return bool(rows)

    # ---- mcp_servers (plan MCP_REGISTRY F0: catálogo, espejo de commands) ----

    @staticmethod
    def _parse_mcp(row: dict) -> dict:
        row = dict(row)
        for col, default in (("args", "[]"), ("env", "{}")):
            try:
                row[col] = json.loads(row[col] or default)
            except (json.JSONDecodeError, TypeError):
                row[col] = json.loads(default)
        for col in ("read_only", "on_demand", "enabled"):
            row[col] = bool(row[col])
        return row

    async def list_mcp_servers(self, enabled_only: bool = False) -> list[dict]:
        where = "WHERE enabled=1" if enabled_only else ""
        rows = await self.run(f"SELECT * FROM mcp_servers {where} ORDER BY name")
        return [self._parse_mcp(r) for r in rows]

    async def get_mcp_server(self, name: str) -> Optional[dict]:
        rows = await self.run(
            "SELECT * FROM mcp_servers WHERE name=? COLLATE NOCASE", (name,))
        return self._parse_mcp(rows[0]) if rows else None

    async def upsert_mcp_server(self, m: dict) -> dict:
        data = {k: m[k] for k in _MCP_COLS if k in m}
        if "name" not in data:
            raise ValueError("name requerido")
        for col in ("args", "env"):
            if col in data and not isinstance(data[col], str):
                data[col] = json.dumps(data[col], ensure_ascii=False)
        for col in ("read_only", "on_demand", "enabled"):
            if col in data:
                data[col] = 1 if data[col] else 0
        await self._upsert("mcp_servers", "name", data)
        return (await self.get_mcp_server(data["name"]))  # type: ignore[return-value]

    async def delete_mcp_server(self, name: str) -> bool:
        """Hard delete (los links caen por ON DELETE CASCADE)."""
        existing = await self.get_mcp_server(name)
        if not existing:
            return False
        await self.run("DELETE FROM mcp_servers WHERE id=?", (existing["id"],))
        return True

    async def link_mcp(self, project_id: int, mcp_id: int) -> None:
        await self.run(
            "INSERT OR IGNORE INTO project_mcp_servers (project_id, mcp_id) "
            "VALUES (?, ?)", (project_id, mcp_id))

    async def unlink_mcp(self, project_id: int, mcp_id: int) -> None:
        await self.run(
            "DELETE FROM project_mcp_servers WHERE project_id=? AND mcp_id=?",
            (project_id, mcp_id))

    async def mcp_project_ids(self, mcp_id: int) -> list[int]:
        rows = await self.run(
            "SELECT project_id FROM project_mcp_servers WHERE mcp_id=?",
            (mcp_id,))
        return [r["project_id"] for r in rows]

    async def mcp_servers_for_project(
        self, project_id: int, names: Optional[list[str]] = None,
        capabilities: Optional[list[str]] = None,
        all_visible: bool = False,
    ) -> list[dict]:
        """Selector F1: MCPs habilitados visibles para un proyecto.

        Visible = sin links (global, caso wrapper) o linkeado al proyecto.
        `names` / `capabilities` filtran además por selección explícita
        (`use_capability("postgres-x")` → names; `--con db` → capabilities).
        Los filtros NO afectan a los always-on (on_demand=0): esos entran
        siempre. `all_visible=True` ignora los filtros y devuelve todo lo
        visible (para armar el menú de use_capability).
        """
        sql = (
            "SELECT m.* FROM mcp_servers m WHERE m.enabled=1 "
            "AND (NOT EXISTS (SELECT 1 FROM project_mcp_servers l "
            "                 WHERE l.mcp_id=m.id) "
            "     OR EXISTS (SELECT 1 FROM project_mcp_servers l "
            "                WHERE l.mcp_id=m.id AND l.project_id=?))")
        params: list[Any] = [project_id]
        if not all_visible:
            clauses = ["m.on_demand=0"]
            for col, values in (("name", names), ("capability", capabilities)):
                if values:
                    marks = ", ".join("?" for _ in values)
                    clauses.append(f"m.{col} COLLATE NOCASE IN ({marks})")
                    params.extend(values)
            sql += f" AND ({' OR '.join(clauses)})"
        sql += " ORDER BY m.name"
        rows = await self.run(sql, tuple(params))
        return [self._parse_mcp(r) for r in rows]

    # ---- crm_clients (snapshot read-only del CRM local) ----
    # ponytail: read-only en el sentido "el relay NO escribe contra el
    # CRM"; localmente sí se persiste (snapshot) para que la Admin UI
    # liste sin depender de que el CRM esté arriba. Upsert por
    # ext_id = `company.id` (cuid) del CRM.

    async def upsert_crm_client(
        self, *, ext_id: str, name: str, domain: Optional[str],
        contacts_json: str, deals_json: str,
        last_sync_status: str = "ok",
        last_activity_at: Optional[str] = None,
    ) -> int:
        """Snapshot de una empresa del CRM. Devuelve el id local."""
        now = now_iso()
        async with self._upsert_lock:
            row = await self.run(
                "SELECT id FROM crm_clients WHERE ext_id=?",
                (ext_id,))
            if row:
                cid = row[0]["id"]
                await self.run(
                    "UPDATE crm_clients SET name=?, domain=?, "
                    "contacts_json=?, deals_json=?, last_sync_at=?, "
                    "last_sync_status=?, last_activity_at=? WHERE id=?",
                    (name, domain, contacts_json, deals_json, now,
                     last_sync_status, last_activity_at, cid))
                return cid
            rows = await self.run(
                "INSERT INTO crm_clients (ext_id, name, domain, "
                "contacts_json, deals_json, last_sync_at, last_sync_status, "
                "last_activity_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (ext_id, name, domain, contacts_json, deals_json,
                 now, last_sync_status, last_activity_at))
            return rows[0]["id"]

    async def list_crm_clients(self) -> list[dict]:
        rows = await self.run(
            "SELECT c.*, "
            "       (SELECT COUNT(*) FROM projects p "
            "        WHERE p.client_id=c.id) AS project_count "
            "FROM crm_clients c ORDER BY c.name")
        return [
            {
                "id": r["id"],
                "ext_id": r["ext_id"],
                "name": r["name"],
                "domain": r["domain"],
                "contacts": json.loads(r["contacts_json"] or "[]"),
                "deals": json.loads(r["deals_json"] or "[]"),
                "last_sync_at": r["last_sync_at"],
                "last_sync_status": r["last_sync_status"],
                "last_activity_at": r["last_activity_at"],
                "project_count": r["project_count"],
            }
            for r in rows
        ]

    async def get_crm_client(self, cid: int) -> Optional[dict]:
        rows = await self.run(
            "SELECT * FROM crm_clients WHERE id=?", (cid,))
        if not rows:
            return None
        r = rows[0]
        return {
            "id": r["id"],
            "ext_id": r["ext_id"],
            "name": r["name"],
            "domain": r["domain"],
            "contacts": json.loads(r["contacts_json"] or "[]"),
            "deals": json.loads(r["deals_json"] or "[]"),
            "last_sync_at": r["last_sync_at"],
            "last_sync_status": r["last_sync_status"],
            "last_activity_at": r["last_activity_at"],
        }

    async def mark_crm_sync_error(self, message: str) -> None:
        """Marca TODOS los clientes con last_sync_status='error'.

        Usado cuando el sync completo falla (ej: token expirado) para
        que la UI muestre el badge rojo sin perder los datos viejos."""
        await self.run(
            "UPDATE crm_clients SET last_sync_at=datetime('now'), "
            "last_sync_status='error'")

    async def prune_crm_clients(self, seen_ext_ids: list[str]) -> dict[str, int]:
        """Saca del snapshot lo que ya no está en el CRM.

        Sin esto el sync solo hace upsert y los clientes borrados en el CRM
        quedan para siempre en la tabla del relay (se veían 16 clientes
        contra 6 empresas reales).

        Regla: si nadie lo apuntaba, se borra; si tiene proyectos
        vinculados, NO se borra — la FK es ON DELETE SET NULL y borrarlo
        desvincularía los proyectos en silencio. Ese queda marcado 'gone'
        para que la UI lo muestre y vos decidas.
        """
        if not seen_ext_ids:
            # Un CRM vacío es indistinguible de un sync roto: no tocar nada.
            return {"removed": 0, "gone": 0}

        marks = ",".join("?" * len(seen_ext_ids))
        linked = (
            "EXISTS (SELECT 1 FROM projects p WHERE p.client_id = crm_clients.id)"
        )
        gone = await self.run(
            f"UPDATE crm_clients SET last_sync_status='gone' "
            f"WHERE ext_id NOT IN ({marks}) AND {linked} "
            f"AND last_sync_status <> 'gone' RETURNING id",
            tuple(seen_ext_ids))
        removed = await self.run(
            f"DELETE FROM crm_clients "
            f"WHERE ext_id NOT IN ({marks}) AND NOT {linked} RETURNING id",
            tuple(seen_ext_ids))
        return {"removed": len(removed), "gone": len(gone)}

    async def set_project_client(
        self, *, project_slug: str, client_id: Optional[int],
    ) -> None:
        """Vincula un project a un crm_client (F2: Convertir en proyecto)."""
        await self.run(
            "UPDATE projects SET client_id=?, updated_at=? WHERE slug=?",
            (client_id, now_iso(), project_slug))

    async def find_crm_client_by_deal(self, deal_id: str) -> Optional[dict]:
        """El cliente cuyo `deals_json` contiene ese deal.

        ponytail: escaneo lineal sobre el snapshot (decenas de clientes,
        pocos deals cada uno). Si algún día duele, los deals pasan a tabla
        propia con índice por id — ver la nota del schema de crm_clients.
        """
        for client in await self.list_crm_clients():
            for deal in client.get("deals") or []:
                if str(deal.get("id")) == deal_id:
                    return client
        return None

    async def set_project_deal(
        self, *, project_slug: str, deal_id: Optional[str],
        client_id: Optional[int],
    ) -> None:
        """Vincula un project a un deal del CRM.

        Escribe `client_id` en la misma sentencia porque un deal pertenece
        a una company: dejarlos desincronizados haría que el proyecto
        cuelgue de un cliente que no es el dueño de su deal.
        """
        await self.run(
            "UPDATE projects SET deal_id=?, client_id=?, updated_at=? "
            "WHERE slug=?",
            (deal_id, client_id, now_iso(), project_slug))

    async def list_projects_for_client(self, client_id: int) -> list[dict]:
        """Proyectos vinculados a un cliente CRM (eslabón cliente→proyecto).

        Devuelve el project parseado completo: quien arma la cadena
        necesita `repo_path` (git) y `defaults_json.github_project`
        (kanban) del mismo row, sin una segunda consulta por proyecto.
        """
        rows = await self.run(
            "SELECT * FROM projects WHERE client_id=? ORDER BY slug",
            (client_id,))
        return [self._parse_project(r) for r in rows]

    async def client_name_by_id(self) -> dict[int, str]:
        """`{crm_clients.id: name}` para resolver nombres en lote.

        El listado de proyectos muestra el cliente de cada fila; con 52
        proyectos, resolverlo de a uno serían 52 consultas.
        """
        rows = await self.run("SELECT id, name FROM crm_clients")
        return {r["id"]: r["name"] for r in rows}

    async def last_crm_sync_at(self) -> Optional[str]:
        rows = await self.run(
            "SELECT MAX(last_sync_at) AS s FROM crm_clients")
        return rows[0]["s"] if rows and rows[0]["s"] else None

    # ---- catálogo de modelos (2026-08-18) ----

    async def list_models(self, *, only_enabled: bool = False) -> list[dict]:
        sql = "SELECT * FROM models"
        if only_enabled:
            sql += " WHERE enabled=1"
        # Los prendidos primero y los medidos-que-ven arriba: el selector
        # los muestra en este orden y lo primero tiene que ser lo usable.
        sql += " ORDER BY enabled DESC, vision IS NULL, vision DESC, spec"
        return [dict(r) for r in await self.run(sql)]

    @staticmethod
    def mask_key(row: dict) -> dict:
        """Fila lista para viajar por la API: la key sale enmascarada.

        La tabla la guarda porque así se pidió, pero eso no es motivo
        para mandarla en cada poll del panel. `api_key_set` le dice a la
        UI si hay una cargada sin decir cuál.
        """
        out = dict(row)
        key = (out.pop("api_key", None) or "").strip()
        out["api_key_set"] = bool(key)
        out["api_key_hint"] = f"••••{key[-4:]}" if len(key) >= 4 else ""
        return out

    async def get_model(self, spec: str) -> Optional[dict]:
        rows = await self.run("SELECT * FROM models WHERE spec=?", (spec,))
        return dict(rows[0]) if rows else None

    async def model_context(self, model_name: str) -> tuple:
        """`(ventana, umbral_de_aviso)` del modelo, o `(None, None)`.

        2026-08-31. `model_name` es lo que el provider devuelve en cada
        respuesta (`MiniMax-M3`), sin el prefijo del spec — por eso el
        match es exacto O por sufijo `:modelo`. Devolver None y no un
        default es a propósito: quien llama (`experts.context_usage`)
        decide el fallback, y así el medidor puede decir contra qué
        número está midiendo en vez de mostrar un porcentaje de una
        ventana que no es la del modelo que corrió.
        """
        nombre = (model_name or "").strip()
        if not nombre:
            return (None, None)
        rows = await self.run(
            "SELECT context_tokens, context_warn_tokens FROM models "
            "WHERE spec=? OR spec LIKE ? COLLATE NOCASE LIMIT 1",
            (nombre, f"%:{nombre}"))
        if not rows:
            return (None, None)
        r = dict(rows[0])
        return (r.get("context_tokens"), r.get("context_warn_tokens"))

    async def upsert_model(self, spec: str, **campos) -> None:
        """Alta o edición. Solo toca las columnas que le pasan."""
        permitidas = ("label", "provider", "base_url", "api_key_env",
                      "api_key", "vision", "enabled", "cost_in", "cost_out",
                      "cost_cache_in", "context_tokens",
                      "context_warn_tokens", "notes", "verified_at")
        malas = set(campos) - set(permitidas)
        if malas:
            raise ValueError(f"columnas desconocidas: {sorted(malas)}")
        await self.run(
            "INSERT INTO models (spec, created_at) VALUES (?, ?) "
            "ON CONFLICT(spec) DO NOTHING", (spec, now_iso()))
        if campos:
            sets = ", ".join(f"{k}=?" for k in campos)
            await self.run(f"UPDATE models SET {sets} WHERE spec=?",
                           (*campos.values(), spec))

    async def delete_model(self, spec: str) -> None:
        await self.run("DELETE FROM models WHERE spec=?", (spec,))

    # ---- users / roles (ADR-037 fase 2) ----

    async def list_users(self) -> list[dict]:
        rows = await self.run(
            "SELECT email, role, created_at FROM users ORDER BY email")
        return [dict(r) for r in rows]

    async def set_user_role(self, email: str, role: str) -> None:
        """Alta o cambio de rol (2026-08-21: ya tiene UI y endpoints).

        El email se normaliza a minúsculas acá y no en el caller porque
        `identity.role_of` busca por email en minúsculas: una fila con
        mayúsculas sería un owner que nunca resuelve como owner.
        """
        if role not in ("owner", "member"):
            raise ValueError(f"rol desconocido: {role!r}")
        email = (email or "").strip().lower()
        if not email:
            raise ValueError("email vacío")
        await self.run(
            "INSERT INTO users (email, role, created_at) VALUES (?,?,?) "
            "ON CONFLICT(email) DO UPDATE SET role=excluded.role",
            (email, role, now_iso()))

    async def delete_user(self, email: str) -> bool:
        """Saca la fila. NO es "sacarle el acceso": quien no está en la
        tabla es `member`, y el alta/baja de verdad la hace la policy de
        Access (ver `identity`). Esto solo lo devuelve a member."""
        rows = await self.run(
            "DELETE FROM users WHERE email=? COLLATE NOCASE RETURNING email",
            ((email or "").strip().lower(),))
        return bool(rows)

    # ---- chats (índice; el .md es la verdad) ----

    async def create_chat(
        self, *, project_slug: Optional[str], source: str,
        author: Optional[str], target: Optional[str],
        vscode_session_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        requested_by: Optional[str] = None,
        user_prompt: Optional[str] = None,
    ) -> str:
        """Abre la fila del run. `user_prompt` es el título del turno.

        Se recorta a `_CAP_PROMPT_FILA` porque no es el registro del
        pedido —ese es el .md— sino lo que el panel del plan muestra
        como título del nodo. Guardar el pedido entero engordaría una
        tabla que se lee en cada poll para mostrar 60 caracteres.
        """
        chat_id = str(uuid.uuid4())
        prompt = (user_prompt or "").strip()[:_CAP_PROMPT_FILA] or None
        await self.run(
            "INSERT INTO chats (id, project_slug, vscode_session_id, source, "
            "author, target, started_at, conversation_id, requested_by, "
            "user_prompt) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (chat_id, project_slug, vscode_session_id, source, author,
             target, now_iso(), conversation_id, requested_by, prompt),
        )
        return chat_id

    async def finish_chat(
        self, chat_id: str, *, status: str, md_path: Optional[str] = None,
        tokens_in: Optional[int] = None, tokens_out: Optional[int] = None,
        cache_read_tokens: Optional[int] = None,  # 2026-08-31
        tool_calls: Optional[int] = None, error: Optional[str] = None,
        phase_at_end: Optional[str] = None, last_tool: Optional[str] = None,
        duration_ms: Optional[int] = None,
        model: Optional[str] = None,
        progress_events: Optional[str] = None,  # Sprint 1: JSON array
        trimmed_turns: int = 0,  # Sprint 1
        tool_bytes: Optional[str] = None,  # 2026-07-26: JSON {tool: {...}}
        stages_json: Optional[str] = None,  # 2026-08-14: etapas del runner
        artifact: Optional[dict] = None,
    ) -> None:
        # Iter 9.8: agregamos model + duration_ms. Para que la migración
        # consults → chats y el path legacy consults puedan guardarlos
        # sin re-mapear. Defaults=None para no tocar las llamadas viejas.
        statement = (
            "UPDATE chats SET status=?, finished_at=?, "
            "md_path=COALESCE(?, md_path), "
            "tokens_in=COALESCE(?, tokens_in), "
            "tokens_out=COALESCE(?, tokens_out), "
            "cache_read_tokens=COALESCE(?, cache_read_tokens), "
            "tool_calls=COALESCE(?, tool_calls), "
            "duration_ms=COALESCE(?, duration_ms), "
            "model=COALESCE(?, model), "
            "progress_events=COALESCE(?, progress_events), "
            "tool_bytes=COALESCE(?, tool_bytes), "
            "stages_json=COALESCE(?, stages_json), "
            "error=COALESCE(?, error), "
            "phase_at_end=COALESCE(?, phase_at_end), "
            "last_tool=COALESCE(?, last_tool), "
            "trimmed=?, trimmed_turns=? WHERE id=?",
            (status, now_iso(), md_path, tokens_in, tokens_out,
             cache_read_tokens, tool_calls,
             duration_ms, model, progress_events, tool_bytes, stages_json,
             error, phase_at_end, last_tool,
             1 if trimmed_turns > 0 else 0, trimmed_turns,
             chat_id),
        )
        if artifact is None:
            await self.run(*statement)
        else:
            await self.run_tx([
                statement,
                ("INSERT INTO chat_outputs(chat_id, payload) VALUES (?, ?) "
                 "ON CONFLICT(chat_id) DO UPDATE SET payload=excluded.payload, exported=0",
                 (chat_id, json.dumps(artifact, ensure_ascii=False))),
            ])

    async def reap_running_chats(self, motivo: str) -> int:
        """Cierra los chats que quedaron en `running` de un relay anterior.

        Ningún run sobrevive a un reinicio del proceso, así que una fila
        en `running` al bootear es siempre mentira: la UI la cuenta como
        viva y le corre el reloj sola. `orquestador.sanar` repara las
        TAREAS de los grafos activos pero nunca tocó `chats`, y los
        chats de un grafo cancelado o fallado no los mira nadie.

        Devuelve cuántos cerró. Idempotente.

        Ponytail: cierra TODOS los `running`, sin mirar antigüedad. Es
        correcto porque el relay es un solo proceso (start.ps1 mata al
        anterior). Si algún día conviven dos, esto tiene que filtrar por
        dueño del run.
        """
        rows = await self.run("SELECT id FROM chats WHERE status='running'")
        if not rows:
            return 0
        await self.run(
            "UPDATE chats SET status='cancelled', finished_at=?, "
            "error=COALESCE(error, ?), "
            "phase_at_end=COALESCE(phase_at_end, 'cancelled') "
            "WHERE status='running'",
            (now_iso(), motivo))
        return len(rows)

    async def set_chat_suggestions(self, chat_id: str, suggestions_json: str) -> None:
        """Guarda las sugerencias de continuación del run (JSON array).

        Aparte de finish_chat porque se generan DESPUÉS de cerrar el chat
        (un turno extra de LLM que no debe demorar el cierre ni perderse
        si falla).
        """
        await self.run(
            "UPDATE chats SET suggestions=? WHERE id=?",
            (suggestions_json, chat_id))

    async def get_chat(self, chat_id: str) -> Optional[dict]:
        rows = await self.run("SELECT * FROM chats WHERE id=?", (chat_id,))
        return rows[0] if rows else None

    async def list_zombie_chats(
        self, *, older_than_s: int = 60, limit: int = 200,
    ) -> list[dict]:
        """Chats en status=running con started_at muy viejo (Iter 5.3).

        Un zombie es un chat que figura running pero el relay perdió el
        proceso (reinicio, crash sin DB update, etc.). Por convención
        los consideramos zombies después de `older_than_s` segundos sin
        terminar — un run interactivo normal termina en <60s, así que
        un running de hace 2min es sospechoso.

        Si el relay está corriendo y el chat TIENE proceso vivo, NO
        aparece acá — el relay se va a enterar por sí solo.
        """
        threshold = time.time() - older_than_s
        # started_at es ISO 8601 UTC ("...Z"). strftime '%Y-%m-%dT%H:%M:%SZ'.
        # Lo filtramos por strftime en SQLite — las fechas vienen del
        # mismo formato siempre (now_iso), así que el orden lexicográfico
        # matchea el orden cronológico.
        threshold_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(threshold))
        return await self.run(
            "SELECT * FROM chats WHERE status='running' "
            "AND started_at < ? ORDER BY started_at DESC LIMIT ?",
            (threshold_iso, limit))

    async def delete_chat(self, chat_id: str) -> bool:
        """Hard-delete de un chat zombie (Iter 5.3).

        Borra la fila de `chats` y best-effort el .md y el JSONL
        asociado. Devuelve True si borró, False si no existía.
        El .md/JSONL son best-effort — si no existen o fallan, igual
        la fila queda fuera.
        """
        chat = await self.get_chat(chat_id)
        if chat is None:
            return False
        md_path = chat.get("md_path")
        await self.run("DELETE FROM chats WHERE id=?", (chat_id,))
        # Cascade best-effort de archivos. Si falla (permisos, etc.),
        # la fila de DB ya está fuera — el humano verá el .md suelto
        # en state/ y decidirá qué hacer.
        if md_path:
            try:
                Path(md_path).unlink(missing_ok=True)
            except OSError:
                pass
        # JSONL lives under chats/<target>/...jsonl (relative to the
        # chat dir). Best-effort: skip — los JSONL no pesan, no urge.
        return True

    async def set_chat_stages(self, chat_id: str, stages_json: str) -> None:
        """Escribe `stages_json` en un chat que TODAVÍA está corriendo.

        `finish_chat` ya lo guarda, pero recién al terminar — y el plan
        del planificador existe a los pocos segundos de arrancar. Servía
        de nada para mirar un run de diez minutos: el panel lo mostraba
        justo cuando el run ya no estaba. Esto lo deja disponible apenas
        se conoce; `finish_chat` después lo pisa con el objeto completo.
        """
        await self.run("UPDATE chats SET stages_json=? WHERE id=?",
                       (stages_json, chat_id))

    async def list_chats(
        self, project_slug: Optional[str] = None, limit: int = 20,
        status: Optional[str] = None,
    ) -> list[dict]:
        """Índice de chats. Filtros opcionales combinables."""
        where, params = [], []
        if project_slug:
            where.append("project_slug=?")
            params.append(project_slug)
        if status:
            where.append("status=?")
            params.append(status)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        return await self.run(
            f"SELECT * FROM chats {clause} "
            "ORDER BY started_at DESC LIMIT ?", tuple(params))

    async def list_chats_by_conversation(
        self, conversation_id: str,
    ) -> list[dict]:
        """Lista los chats (runs) de una conversación, en orden cronológico.

        Usado por GET /conversations/{id}/messages para armar la vista
        del humano desde el resultado durable o los .md históricos.

        `tool_calls`/`phase_at_end` van para que la burbuja del experto
        pueda decir cuántas tools corrió: un turno que anunció y no
        ejecutó se veía idéntico a uno que trabajó (2026-08-13)."""
        return await self.run(
            "SELECT c.id, c.md_path, c.started_at, c.progress_events, c.tool_calls, "
            "c.phase_at_end, c.requested_by, c.status, c.stages_json, c.duration_ms, "
            "o.payload AS output_payload, "
            # Estado de la exportación: la UI ya cae al payload cuando
            # falta el .md, pero no podía DECIR que estaba pendiente ni
            # por qué. Sin esto, un chat sin archivo se ve igual que uno
            # que nunca lo iba a tener.
            "o.exported, o.intentos AS export_intentos, "
            "o.ultimo_error AS export_error, "
            "o.proximo_intento_at AS export_proximo "
            "FROM chats c LEFT JOIN chat_outputs o ON o.chat_id=c.id "
            "WHERE c.conversation_id=? AND (c.md_path IS NOT NULL OR o.chat_id IS NOT NULL) "
            "ORDER BY c.started_at ASC",
            (conversation_id,))

    async def list_chats_of_conversation(
        self, conversation_id: str, limit: int = 10,
    ) -> list[dict]:
        """Los últimos runs de un hilo, con lo que el panel del plan pide.

        No reusa `list_chats_by_conversation` porque ese filtra
        `md_path IS NOT NULL` — y el `.md` se escribe al TERMINAR, así
        que no ve el run que está corriendo. Ese filtro es correcto para
        la vista de mensajes (un run sin `.md` no tiene nada que
        mostrar) y equivocado para esto, donde el run en curso es
        justamente el que interesa.

        Del más nuevo al más viejo: quien llama quiere el último que
        haya dejado un plan.

        **Las columnas importan.** Hasta el 30/8 esto traía cinco y
        `_grafo_sintetico` leía nueve: `user_prompt` (no existía),
        `phase_at_end`, `source`, `finished_at`. El resultado no era un
        error sino algo peor — un panel entero de nodos sin título, con
        los turnos que murieron por budget o timeout pintados de verde
        ("hecho") porque sin `phase_at_end` no hay fase terminal que
        mirar. Lo que la fila no trae, el panel lo inventa en silencio.
        """
        return await self.run(
            "SELECT id, status, started_at, finished_at, tool_calls, "
            "stages_json, phase_at_end, source, user_prompt "
            "FROM chats WHERE conversation_id=? "
            "ORDER BY started_at DESC LIMIT ?",
            (conversation_id, limit))

    async def report_usage(self, days: int = 30,
                           project_slug: Optional[str] = None) -> dict:
        """Agregados de `chats` para el tab Informe de la Admin UI.

        2026-07-20: /chats capea en 200 filas — para un informe
        histórico el GROUP BY va en SQL, no en el browser. Read-only.
        """
        since = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
        where = "WHERE started_at>=?"
        params: tuple = (since,)
        if project_slug:
            where += " AND project_slug=? COLLATE NOCASE"
            params = (since, project_slug)
        agg = ("COUNT(*) AS runs, "
               "COALESCE(SUM(tokens_in),0) AS tokens_in, "
               # 2026-08-31: la parte cacheada de `tokens_in`, no un
               # sumando aparte. Sin esto el Informe mostraba el bruto y
               # parecía 3-4x del gasto real (82,5% medido en MiniMax).
               "COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens, "
               "COALESCE(SUM(tokens_out),0) AS tokens_out, "
               "COALESCE(SUM(tool_calls),0) AS tool_calls, "
               "CAST(AVG(duration_ms) AS INTEGER) AS avg_duration_ms, "
               "SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END) AS not_ok")
        daily = await self.run(
            f"SELECT substr(started_at,1,10) AS day, {agg} "
            f"FROM chats {where} GROUP BY day ORDER BY day", params)
        by_project = await self.run(
            f"SELECT COALESCE(project_slug,'—') AS project, {agg}, "
            "MAX(started_at) AS last_run "
            f"FROM chats {where} GROUP BY project "
            "ORDER BY tokens_in DESC", params)
        by_author = await self.run(
            f"SELECT COALESCE(author,'—') AS author, "
            f"COALESCE(source,'—') AS source, {agg} "
            f"FROM chats {where} GROUP BY author, source "
            "ORDER BY tokens_in DESC", params)
        return {"days": days, "since": since, "daily": daily,
                "by_project": by_project, "by_author": by_author}

    # Sprint 1 (Item 3): Dashboard de métricas

    async def metrics_summary(  # noqa: C901
        self, days: int = 7, *, project: str = "", status: str = "",
        provider: str = "", role: str = "",
        from_date: str = "", to_date: str = "",
    ) -> dict:
        """KPIs agregados para el dashboard. Una query por sección.

        Los filtros operan en DOS niveles distintos, y mezclarlos daría
        totales incocherentes:

        * `project` y `status` filtran RUNS. Afectan todo: totales,
          desglose por modelo, por proyecto, horario y errores.
        * `provider` y `role` filtran TURNOS, que es una dimensión del
          desglose y no del run. Solo afectan `by_model`/`by_provider`;
          los totales siguen siendo los del run completo, porque un run
          no "pertenece" a un proveedor —usa varios—.

        Ventana temporal: si vienen `from_date` y `to_date` (YYYY-MM-DD,
        validados por `admin._parse_metrics_window`) se usa ese rango
        absoluto; si no, se cae al comportamiento viejo con `days`
        contando desde ahora. En ambos casos el "período anterior" del
        que sacamos los deltas es la ventana de igual largo
        inmediatamente anterior.

        `filters_applied` en la respuesta deja explícito qué se aplicó,
        para que la UI pueda decirlo en vez de mostrar números filtrados
        que parecen totales.
        """
        since, until, span_days = self._resolve_metrics_window(
            from_date, to_date, days)
        where, run_params = self._build_run_where(
            since, until, project, status)

        # Totales
        tot = await self.run(
            "SELECT COUNT(*) AS runs, "
            "COALESCE(SUM(tokens_in),0) AS tokens_in, "
            "COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens, "
            "COALESCE(SUM(tokens_out),0) AS tokens_out, "
            "COALESCE(SUM(tool_calls),0) AS tool_calls, "
            "CAST(COALESCE(AVG(duration_ms),0) AS INTEGER) AS duration_ms_avg, "
            "SUM(CASE WHEN status='error' AND COALESCE(phase_at_end,'')!='budget_split' THEN 1 ELSE 0 END) AS errors, "
            "SUM(status='ok' AND COALESCE(phase_at_end,'')!='budget_split') AS ok, "
            "SUM(status='running' AND COALESCE(phase_at_end,'')!='budget_split') AS running, "
            "SUM(status='cancelled' AND COALESCE(phase_at_end,'')!='budget_split') AS cancelled, "
            "SUM(COALESCE(phase_at_end,'')='budget_split') AS split "
            f"FROM chats {where}", tuple(run_params))
        totals = dict(tot[0]) if tot else {}

        # Período ANTERIOR de igual largo, para el delta de los KPIs. Un
        # número solo no es una señal: 229 runs no dice si el sistema se
        # está usando más o menos que la semana pasada. El rango previo
        # arranca donde terminaba el actual y tiene la misma amplitud
        # (`span_days`); sin `until` el WHERE es abierto abajo y sin
        # `since` no entramos acá.
        prev_since, prev_until = self._shift_window_back(
            since, until, span_days)
        prev_where, prev_params = self._build_run_where(
            prev_since, prev_until, project, status)
        prev = await self.run(
            "SELECT COUNT(*) AS runs, "
            "COALESCE(SUM(tokens_in),0) AS tokens_in, "
            "COALESCE(SUM(tokens_out),0) AS tokens_out, "
            "COALESCE(SUM(tool_calls),0) AS tool_calls, "
            "SUM(CASE WHEN status='error' AND COALESCE(phase_at_end,'')!='budget_split' THEN 1 ELSE 0 END) AS errors "
            f"FROM chats {prev_where}", tuple(prev_params))
        previous = dict(prev[0]) if prev else {}

        # Por modelo y ROL. Antes esto agrupaba solo por `chats.model`,
        # que guarda el spec del EJECUTOR: desde que las etapas corren en
        # proveedores distintos, eso le atribuía al modelo pagado todo lo
        # que consumieron el planificador, el verificador y el
        # documentador. Ahora cada etapa aporta su propia fila desde
        # `stages_json` (una UNION por etapa, sin tabla nueva).
        #
        # Los runs viejos no tienen `stages_json`: quedan con su fila de
        # ejecutor y ninguna de etapa, que es la verdad — no se midieron.
        _stage_union = " UNION ALL ".join(
            # Sin COALESCE a propósito: si la etapa corrió pero no se
            # midió (runs entre que se agregó stages_json y que se
            # instrumentaron los tokens), json_extract da NULL, SUM lo
            # ignora y la fila queda en NULL = "sin dato". Con COALESCE
            # a 0 se leería como "corrió y no consumió", que es mentira.
            # `cache_read_tokens` va en NULL para las etapas: no se mide
            # (planner/verifier/documenter son ~1% del gasto y corren en
            # otros proveedores). NULL = sin dato, y `cost_usd` entonces
            # les cobra la entrada entera, que es lo correcto acá.
            f"""SELECT json_extract(stages_json,'$.{st}_model') AS model,
                       '{st}' AS role, 1 AS runs,
                       json_extract(stages_json,'$.{st}_tokens_in') AS tokens_in,
                       NULL AS cache_read_tokens,
                       json_extract(stages_json,'$.{st}_tokens_out') AS tokens_out
                FROM chats {where} AND stages_json IS NOT NULL
                  AND json_extract(stages_json,'$.{st}_model') IS NOT NULL
                  AND json_extract(stages_json,'$.{st}_model') != ''"""
            for st in ("planner", "verifier", "documenter"))
        by_model = await self.run(
            "SELECT model, role, SUM(runs) AS runs, "
            "SUM(tokens_in) AS tokens_in, "
            "SUM(cache_read_tokens) AS cache_read_tokens, "
            "SUM(tokens_out) AS tokens_out FROM ("
            "  SELECT COALESCE(model,'?') AS model, 'executor' AS role, "
            "         1 AS runs, COALESCE(tokens_in,0) AS tokens_in, "
            "         cache_read_tokens, "
            "         COALESCE(tokens_out,0) AS tokens_out "
            f"  FROM chats {where}"
            f"  UNION ALL {_stage_union}"
            ") GROUP BY model, role ORDER BY runs DESC",
            tuple(run_params) * 4)

        # Por proveedor (el prefijo antes de ':'). Responde la pregunta
        # que el desglose por modelo no contesta de un vistazo: cuánto
        # corre en el proveedor pagado y cuánto en los endpoints gratis.
        # `tokens_*` arranca en None y solo se vuelve número si alguna
        # fila trajo dato, para no reportar 0 tokens de un proveedor cuyas
        # etapas todavía no estaban instrumentadas.
        # Tarifas (fase 2). Sin `MODEL_PRICES` cargada, todos los costos
        # quedan en None y la UI los muestra como "sin tarifa" — nunca
        # como 0, que se leería como "gratis".
        # 2026-08-31: la tabla `models` es la base y `MODEL_PRICES` la
        # pisa. Antes se leía SOLO la clave de system_config, que estaba
        # vacía: `priced=false` y todos los costos en null con la tarifa
        # cargada en la pantalla Modelos. `MODEL_PRICES` sigue mandando
        # porque es donde viven los patrones `nvidia:*` y los `ref_*`.
        prices = prices_from_models(
            await self.run("SELECT spec, cost_in, cost_out, cost_cache_in "
                           "FROM models"))
        try:
            prices.update(json.loads(
                await self.get_config(MODEL_PRICES_KEY, "") or "{}"))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        # Filtros de TURNO. Se aplican acá y no en el SQL porque `role`
        # y `provider` son dimensiones del desglose: recortarlos en la
        # query dejaría los totales del run descuadrados contra la tabla.
        if role:
            by_model = [r for r in by_model if r["role"] == role]
        if provider:
            by_model = [r for r in by_model
                        if (r["model"] or "?").split(":", 1)[0] == provider]

        by_provider: dict[str, dict] = {}
        cost_total: Optional[float] = None
        saved_total: Optional[float] = None
        for row in by_model:
            spec = row["model"] or "?"
            price = match_model_price(prices, spec)
            row["cost_usd"] = cost_usd(
                price, row["tokens_in"], row["tokens_out"],
                cache_read=row.get("cache_read_tokens"))
            # Ahorro: lo que ESTE turno habría costado a precio de
            # mercado, menos lo que costó de verdad. Solo tiene sentido
            # donde hay `ref_*` cargado (los endpoints gratis).
            ref = cost_usd(price, row["tokens_in"], row["tokens_out"],
                           reference=True)
            row["saved_usd"] = (
                None if ref is None else ref - (row["cost_usd"] or 0.0))

            prov = spec.split(":", 1)[0] or "?"
            acc = by_provider.setdefault(
                prov, {"provider": prov, "runs": 0, "tokens_in": None,
                       "cache_read_tokens": None,
                       "tokens_out": None, "cost_usd": None,
                       "saved_usd": None})
            acc["runs"] += row["runs"] or 0
            for k in ("tokens_in", "cache_read_tokens", "tokens_out",
                      "cost_usd", "saved_usd"):
                if row[k] is not None:
                    acc[k] = (acc[k] or 0) + row[k]
            if row["cost_usd"] is not None:
                cost_total = (cost_total or 0.0) + row["cost_usd"]
            if row["saved_usd"] is not None:
                saved_total = (saved_total or 0.0) + row["saved_usd"]

        totals["cost_usd"] = cost_total
        totals["saved_usd"] = saved_total
        totals["priced"] = bool(prices)

        # Por proyecto (top 10)
        by_project = await self.run(
            "SELECT COALESCE(project_slug,'—') AS slug, COUNT(*) AS runs, "
            "COALESCE(SUM(tokens_in),0) AS tokens_in "
            f"FROM chats {where} GROUP BY project_slug "
            "ORDER BY runs DESC LIMIT 10", tuple(run_params))

        # Distribución horaria
        hourly = await self.run(
            "SELECT CAST(strftime('%H',started_at) AS INTEGER) AS hour, "
            "COUNT(*) AS runs "
            f"FROM chats {where} GROUP BY hour ORDER BY hour", tuple(run_params))

        # Top errores
        errors_breakdown = await self.run(
            "SELECT COALESCE(error,'(unknown)') AS error_type, COUNT(*) AS count "
            f"FROM chats {where} AND status='error' "
            "AND COALESCE(phase_at_end,'')!='budget_split' AND error IS NOT NULL "
            "GROUP BY error_type ORDER BY count DESC LIMIT 10", tuple(run_params))

        return {
            # `span_days` es la amplitud real del rango pedido: cuando el
            # dashboard mandó `from`+`to` puede no coincidir con `days`
            # (que queda en None). La UI ya sabe que 30 es 30 días.
            "period_days": span_days,
            "since": since,
            "until": until,
            "from_date": from_date,
            "to_date": to_date,
            # Solo las claves con valor: la UI pregunta "¿hay filtros?"
            # con un truthiness y no tiene que descartar strings vacíos.
            "filters_applied": {k: v for k, v in (
                ("project", project), ("status", status),
                ("provider", provider), ("role", role)) if v},
            "totals": totals,
            # Crudo, sin porcentajes calculados: el delta contra 0 no es
            # "+100%", es "no hay con qué comparar", y esa distinción la
            # decide quien lo muestra.
            "previous": previous,
            "by_model": by_model,
            "by_provider": sorted(by_provider.values(),
                                  key=lambda r: -r["runs"]),
            "by_project": by_project,
            "hourly_distribution": hourly,
            "error_breakdown": errors_breakdown,
        }

    async def metrics_trends(
        self, days: int = 7, *, project: str = "", status: str = "",
        from_date: str = "", to_date: str = "",
    ) -> list[dict]:
        """Slice diario para gráfica de tendencia.

        Toma los MISMOS filtros de nivel run que `metrics_summary`
        (`project` y `status`, con errores separados de cancelación,
        actividad y subdivisión). Sin esto la serie es global mientras los
        KPIs de arriba están filtrados, y el gráfico contradice a los
        números que tiene al lado.

        Misma ventana que summary: `from_date`+`to_date` absoluto o
        `days` fallback.

        `provider` y `role` no viajan a propósito: filtran TURNOS, no
        runs, así que no recortan ni los totales ni esta serie —igual
        que en `metrics_summary`.
        """
        since, until, _span = self._resolve_metrics_window(
            from_date, to_date, days)
        where, params = self._build_run_where(
            since, until, project, status)
        return await self.run(
            "SELECT substr(started_at,1,10) AS date, "
            "COUNT(*) AS runs, "
            "COALESCE(SUM(tokens_in),0) AS tokens_in, "
            "SUM(CASE WHEN status='error' AND COALESCE(phase_at_end,'')!='budget_split' THEN 1 ELSE 0 END) AS errors, "
            "SUM(status='ok' AND COALESCE(phase_at_end,'')!='budget_split') AS ok, "
            "SUM(status='running' AND COALESCE(phase_at_end,'')!='budget_split') AS running, "
            "SUM(status='cancelled' AND COALESCE(phase_at_end,'')!='budget_split') AS cancelled, "
            "SUM(COALESCE(phase_at_end,'')='budget_split') AS split "
            f"FROM chats {where} "
            "GROUP BY date ORDER BY date",
            tuple(params))

    # ---- helpers internos de métricas (Sprint 1: rango de fechas) ----

    @staticmethod
    def _resolve_metrics_window(
        from_date: str, to_date: str, days: int,
    ) -> tuple[str, str, int]:
        """Resuelve la ventana de tiempo en formato SQL.

        Devuelve `(since, until, span_days)`. `since` siempre se incluye
        en el WHERE; `until` es "" si no hay cota superior (modo
        `days`). `span_days` es la amplitud que va al JSON para que la UI
        sepa cuántos días cubre.

        El caller (admin._parse_metrics_window) ya validó que
        `from_date`/`to_date` parsean y que el rango no supera 366 días;
        acá solo se traduce a ISO-8601 con `T00:00:00Z` para que el
        `WHERE started_at>=?` los pueda comparar contra el
        `started_at` UTC que guarda SQLite.
        """
        if from_date and to_date:
            # 23:59:59 del día final para que un run que arrancó a las
            # 23:55 entre en "to=2026-02-01". Sin esto, to=inicio del
            # día y la query excluiría el mismo día del borde derecho.
            since = f"{from_date}T00:00:00Z"
            until = f"{to_date}T23:59:59Z"
            f = datetime.strptime(from_date, "%Y-%m-%d").date()
            t = datetime.strptime(to_date, "%Y-%m-%d").date()
            span = (t - f).days + 1
            return since, until, span
        # Fallback legacy: `days` contando desde ahora. Arriba, el admin
        # ya clampeó a 1..90; acá solo computamos el timestamp.
        since = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
        return since, "", days

    @staticmethod
    def _shift_window_back(
        since: str, until: str, span_days: int,
    ) -> tuple[str, str]:
        """Desplaza la ventana `span_days` hacia atrás, sin pisarla.

        Si la ventana actual tiene `until` (modo absoluto), la anterior
        va desde `until` (exclusivo) hasta `until - span_days`. Si es
        abierta (modo `days`), la anterior es `[since-span, since)`.
        Devuelve los dos extremos ya en formato ISO con hora fija
        (00:00:00 / 23:59:59) para que los WHERE comparen parejo con
        `started_at` UTC.
        """
        if until:
            # `until` está fijo en 23:59:59 del `to`; para la ventana
            # anterior, la "abajo" tiene que empezar exactamente al día
            # siguiente en 00:00:00. Calcularlo sobre el timestamp crudo
            # (sin hora) evita derivas por zona horaria del servidor.
            since_date = datetime.strptime(since[:10], "%Y-%m-%d").date()
            new_to_date = since_date - timedelta(days=1)
            new_since_date = new_to_date - timedelta(days=span_days - 1)
            return (f"{new_since_date}T00:00:00Z",
                    f"{new_to_date}T23:59:59Z")
        # Modo `days`: ventana abierta abajo. Cortamos en `since` (la
        # nueva va desde `since - span_days` hasta `since`, exclusivo).
        since_ts = calendar.timegm(time.strptime(since, "%Y-%m-%dT%H:%M:%SZ"))
        new_since_ts = since_ts - span_days * 86400
        return (time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(new_since_ts)),
            since)

    @staticmethod
    def _build_run_where(
        since: str, until: str, project: str, status: str,
    ) -> tuple[str, list]:
        """Arma el `WHERE started_at ...` de summary/trends con filtros run.

        Centralizado para que el `prev_where` del summary no tenga que
        reescribir el `replace("WHERE started_at>=?", ...)` original:
        ahora los dos lados (período actual y anterior) salen del mismo
        helper y agregan filtros en el mismo orden.
        """
        where = "WHERE started_at>=?"
        params: list = [since]
        if until:
            where += " AND started_at<?"
            params.append(until)
        if project:
            where += " AND project_slug=?"
            params.append(project)
        if status == "error":
            where += " AND status='error' AND COALESCE(phase_at_end,'')!='budget_split'"
        elif status == "split":
            where += " AND phase_at_end='budget_split'"
        elif status:
            where += " AND status=?"
            params.append(status)
            if status in ("ok", "running", "cancelled"):
                where += " AND COALESCE(phase_at_end,'')!='budget_split'"
        return where, params

    async def global_search(self, q: str, limit: int = 8) -> dict:
        """Búsqueda agrupada para el Cmd+K de la Admin UI (UI 2026-07-20).

        3 queries (projects + chats + conversations), todas LIKE case-insensitive
        y con cap por tipo. Read-only. Más barato que el browser pegue a
        /projects + /chats + /conversations y filtre client-side.
        """
        like = f"%{q}%"
        projects = await self.run(
            "SELECT slug, name, repo_path, enabled FROM projects "
            "WHERE slug LIKE ? COLLATE NOCASE "
            "OR name LIKE ? COLLATE NOCASE "
            "OR repo_path LIKE ? COLLATE NOCASE "
            "ORDER BY slug LIMIT ?",
            (like, like, like, limit))
        chats = await self.run(
            "SELECT id, project_slug, source, status, started_at, "
            "COALESCE(model,'') AS model, "
            "COALESCE(tokens_in,0) AS tokens_in "
            "FROM chats "
            "WHERE id LIKE ? COLLATE NOCASE "
            "OR project_slug LIKE ? COLLATE NOCASE "
            "ORDER BY started_at DESC LIMIT ?",
            (f"{q}%", like, limit))
        conversations = await self.run(
            "SELECT id, project_slug, status, last_activity_at, "
            "discord_thread_id, "
            "substr(COALESCE(summary,''),1,140) AS summary "
            "FROM conversations "
            "WHERE id LIKE ? COLLATE NOCASE "
            "OR project_slug LIKE ? COLLATE NOCASE "
            "ORDER BY last_activity_at DESC LIMIT ?",
            (f"{q}%", like, limit))
        return {"q": q, "projects": projects,
                "chats": chats, "conversations": conversations}

    # ---- conversations (ADR-025: working memory por hilo) ----

    async def create_conversation(
        self, *, project_slug: str, discord_thread_id: Optional[str] = None,
        author: Optional[str] = None, branch: Optional[str] = None,
        discord_user_id: Optional[str] = None,
        discord_author: Optional[str] = None,
        requested_by: Optional[str] = None,
    ) -> str:
        """Crea conversación. Iter 10.0: si viene de Discord, persistir
        discord_user_id + discord_author para que la UI pueda mandar
        replies de vuelta al autor."""
        conv_id = str(uuid.uuid4())
        ts = now_iso()
        await self.run(
            "INSERT INTO conversations (id, project_slug, discord_thread_id, "
            "discord_user_id, discord_author, status, started_at, "
            "last_activity_at, author, branch, requested_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (conv_id, project_slug, discord_thread_id, discord_user_id,
             discord_author, "open", ts, ts, author, branch, requested_by),
        )
        return conv_id

    async def get_open_conversation_for_workspace(self, project: dict) -> Optional[dict]:
        from .coordination import workspace_key
        rows = await self.run(
            "SELECT c.*, p.repo_path FROM conversations c JOIN projects p "
            "ON p.slug=c.project_slug COLLATE NOCASE WHERE c.status='open' "
            "ORDER BY c.started_at DESC")
        key = workspace_key(project)
        return next((r for r in rows if workspace_key(
            {"slug": r["project_slug"], "repo_path": r["repo_path"]}) == key), None)

    async def get_open_conversation_for_project(
        self, project_slug: str,
    ) -> Optional[dict]:
        """Conversación ABIERTA de un proyecto (regla: una por repo).

        Se usa en /nuevo para rechazar una segunda conversación sobre el
        mismo repo (un solo working tree → pelearían por la rama activa)."""
        rows = await self.run(
            "SELECT * FROM conversations WHERE project_slug=? COLLATE NOCASE "
            "AND status='open' ORDER BY started_at DESC LIMIT 1",
            (project_slug,))
        return rows[0] if rows else None

    async def set_conversation_pr(self, conv_id: str, pr_url: str) -> None:
        await self.run(
            "UPDATE conversations SET pr_url=? WHERE id=?", (pr_url, conv_id))

    async def set_conversation_issue(self, conv_id: str, number: int) -> None:
        """Vincula la conversación a un issue de GitHub (fase 4)."""
        await self.run(
            "UPDATE conversations SET issue_number=? WHERE id=?",
            (int(number), conv_id))

    async def set_conversation_discord_user(
        self, conv_id: str, *, discord_user_id: Optional[str],
        discord_author: Optional[str] = None,
        clear: bool = False,
    ) -> None:
        """Iter 10.0: vincula/desvincula una conversación con autor Discord.

        Modos:
          - normal: discord_user_id es string no vacío → setea ambos campos
            (discord_author solo si lo pasan — COALESCE para no pisar).
          - clear: discord_user_id=None y clear=True → NULL en ambos
            campos. Usado por el botón "Desvincular Discord" del chat panel.

        Se usa cuando la UI recibe un chat que NO vino de Discord (sin
        discord_thread_id) pero queremos habilitar el bridge bidireccional.
        El bot C# necesita discord_user_id para saber a quién mandarle el
        reply cuando el experto termina.
        """
        if clear:
            await self.run(
                "UPDATE conversations SET discord_user_id=NULL, "
                "discord_author=NULL WHERE id=?", (conv_id,))
            return
        await self.run(
            "UPDATE conversations SET discord_user_id=?, "
            "discord_author=COALESCE(?, discord_author) WHERE id=?",
            (discord_user_id, discord_author, conv_id))

    # ---- projects.discord_channel_id (Iter 10.1: canal default por proyecto) ----

    async def set_project_discord_channel(
        self, slug: str, *, channel_id: Optional[str],
        clear: bool = False,
    ) -> bool:
        """Iter 10.1: setea/limpia el canal Discord default del proyecto.

        Modos:
          - normal: channel_id es string no vacío → guarda (upsert).
          - clear:  channel_id=None y clear=True → NULL. Devuelve True si
            la fila existía, False si el slug es desconocido.

        El endpoint PATCH valida el slug y el shape; este helper NO
        valida (es low-level, mirror de set_conversation_discord_user).
        """
        if clear:
            row = await self.run(
                "UPDATE projects SET discord_channel_id=NULL, "
                "updated_at=datetime('now') WHERE slug=? COLLATE NOCASE "
                "RETURNING slug", (slug,))
            return bool(row)
        # Upsert tolerante: si el slug no existe, no creamos fila nueva
        # (el endpoint valida slug primero → 404 antes de llegar acá).
        # Si llega con string vacío, lo tratamos como clear.
        if not channel_id:
            row = await self.run(
                "UPDATE projects SET discord_channel_id=NULL, "
                "updated_at=datetime('now') WHERE slug=? COLLATE NOCASE "
                "RETURNING slug", (slug,))
            return bool(row)
        row = await self.run(
            "UPDATE projects SET discord_channel_id=?, "
            "updated_at=datetime('now') WHERE slug=? COLLATE NOCASE "
            "RETURNING slug", (channel_id, slug))
        return bool(row)

    async def get_conversation_messages(
        self, conv_id: str,
    ) -> Optional[str]:
        """Devuelve el messages_json persistido de una conversación.
        None si la conversación no existe. Usado por la UI para
        reconstruir el historial cuando abres un chat.
        """
        rows = await self.run(
            "SELECT messages_json FROM conversations WHERE id=?",
            (conv_id,))
        return rows[0]["messages_json"] if rows else None

    async def get_conversation(self, conv_id: str) -> Optional[dict]:
        rows = await self.run(
            "SELECT * FROM conversations WHERE id=?", (conv_id,))
        return rows[0] if rows else None

    async def conversation_usage(self, conv_id: str) -> dict:
        """Tiempo de USO de una conversacion: lo que duraron sus runs.

        No es el reloj de pared. Los dos numeros no se parecen porque una
        conversacion queda abierta entre pedido y pedido: medido el
        2026-08-31 sobre `un run de ejemplo` (inventorydemo), 16h16 de pared contra 2h49 de
        trabajo real. Para "cuantas horas lleva esto" —el chip del header
        y las horas que van al PR de /cerrar— sirve el segundo.

        `duration_ms` puede venir NULL en un run que el relay no llego a
        cerrar (reinicio a mitad); ese run cuenta en `runs` pero suma 0.
        Preferimos subestimar antes que inventar.
        """
        rows = await self.run(
            "SELECT COUNT(*) AS runs, COALESCE(SUM(duration_ms), 0) AS ms "
            "FROM chats WHERE conversation_id=?", (conv_id,))
        r = rows[0] if rows else {}
        return {"runs": int(r.get("runs") or 0), "ms": int(r.get("ms") or 0)}

    async def get_open_conversation_by_thread(
        self, project_slug: str, discord_thread_id: str,
    ) -> Optional[dict]:
        """Conversación ABIERTA de un hilo Discord (auto-attach, ADR-025).

        Puede haber conversaciones cerradas con el mismo thread_id (hilo
        que siguió tras un /cerrar): se toma la abierta más reciente."""
        rows = await self.run(
            "SELECT * FROM conversations WHERE project_slug=? COLLATE NOCASE "
            "AND discord_thread_id=? AND status='open' "
            "ORDER BY started_at DESC LIMIT 1",
            (project_slug, discord_thread_id))
        return rows[0] if rows else None

    async def list_conversations(
        self, project_slug: Optional[str] = None,
        status: Optional[str] = None, limit: int = 20,
    ) -> list[dict]:
        """Lista SIN messages_json (blob pesado; pedirlo por id).

        Iter 10.3: incluye `branch` y `pr_url` para que la sidebar
        muestre el nombre de la rama local acumulada y el link al PR
        sin pedir `/conversations/{id}` por cada item. Son dos strings
        chicas; pesan nada contra el messages_json que seguimos
        excluyendo.

        Iter 10.6: incluye `messages_len` derivado de `messages_json`
        con `json_array_length`, para que la sidebar pinte el contador
        sin pedir el detalle. COALESCE a 0 cubre las conversaciones
        con `messages_json` NULL (la columna puede ser NULL — la
        lista las devuelve con 0).
        """
        cols = ("id, project_slug, discord_thread_id, status, started_at, "
                "closed_at, last_activity_at, summary, branch, pr_url, "
                "COALESCE(json_array_length(messages_json), 0) AS messages_len")
        where, params = [], []
        if project_slug:
            where.append("project_slug=? COLLATE NOCASE")
            params.append(project_slug)
        if status:
            where.append("status=?")
            params.append(status)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return await self.run(
            f"SELECT {cols} FROM conversations {clause} "
            "ORDER BY last_activity_at DESC LIMIT ?", (*params, limit))

    async def save_conversation_messages(
        self, conv_id: str, messages_json: str, *, expected: Optional[str] = None,
    ) -> None:
        """Persiste sin pisar cambios posteriores al snapshot del caller."""
        if expected is not None:
            rows = await self.run(
                "UPDATE conversations SET messages_json=?, last_activity_at=? "
                "WHERE id=? AND COALESCE(messages_json, '')=? RETURNING id",
                (messages_json, now_iso(), conv_id, expected))
            if not rows:
                raise RuntimeError("el historial cambió durante la ejecución; no se sobrescribió")
            return
        await self.run(
            "UPDATE conversations SET messages_json=?, last_activity_at=? "
            "WHERE id=?", (messages_json, now_iso(), conv_id))

    async def save_conversation_bitacora(
        self, conv_id: str, bitacora_json: str,
    ) -> None:
        """Persiste la bitácora del run. `""` la borra.

        Aparte de `save_conversation_messages` porque los dos se caen en
        momentos distintos: el historial se guarda solo cuando el run
        termina `ok`, y la bitácora tiene que sobrevivir también a los
        cortes —que es justo cuando sirve—.
        """
        await self.run(
            "UPDATE conversations SET bitacora_json=? WHERE id=?",
            (bitacora_json, conv_id))

    async def touch_conversation(self, conv_id: str) -> None:
        """Marca actividad. Se llama al INICIO de cada run: así el
        sweeper de auto-close nunca cierra una conversación con run en
        curso (el run dura ≤ expert_timeout << 24h)."""
        await self.run(
            "UPDATE conversations SET last_activity_at=? WHERE id=?",
            (now_iso(), conv_id))

    async def close_conversation(self, conv_id: str) -> bool:
        """Cierra (idempotente). Devuelve True si estaba abierta."""
        rows = await self.run(
            "UPDATE conversations SET status='closed', closed_at=? "
            "WHERE id=? AND status='open' RETURNING id",
            (now_iso(), conv_id))
        return bool(rows)

    async def set_conversation_summary(self, conv_id: str, summary: str) -> None:
        await self.run(
            "UPDATE conversations SET summary=? WHERE id=?",
            (summary, conv_id))

    async def stale_open_conversations(self, hours: float) -> list[dict]:
        """Conversaciones abiertas sin actividad hace > `hours` (sweeper)."""
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
        return await self.run(
            "SELECT id, project_slug, messages_json FROM conversations "
            "WHERE status='open' AND last_activity_at < ?", (cutoff,))

    # ---- facts (ADR-026: hechos atómicos, append-only + supersede soft) ----

    #: Estados de un fact. `pending` = destilado por el compactador y
    #: todavía sin revisar: cuenta para deduplicar pero NO llega al
    #: experto. Ver la nota de `status` en el schema.
    FACT_STATES = ("pending", "approved", "rejected")

    async def add_facts(
        self, project_slug: str, facts: list[str],
        source_conversation: Optional[str] = None,
        status: str = "pending",
    ) -> int:
        """Agrega hechos. `status='pending'` por default: lo que escribe
        el compactador espera aprobación. Quien llama con `approved` es
        el alta manual desde la UI — ahí lo escribió un humano y no hay
        a quién pedirle permiso."""
        if status not in self.FACT_STATES:
            raise ValueError(f"status inválido: {status!r}")
        n = 0
        for fact in facts:
            fact = (fact or "").strip()
            if not fact:
                continue
            await self.run(
                "INSERT INTO facts (project_slug, fact, source_conversation, "
                "status) VALUES (?,?,?,?)",
                (project_slug, fact, source_conversation, status))
            n += 1
        return n

    async def list_facts(
        self, project_slug: str, limit: int = 100,
        include_superseded: bool = False,
        status: Optional[str] = None,
    ) -> list[dict]:
        """Hechos del proyecto, por default solo los VIGENTES (los
        obsoletos quedan en la tabla para auditoría pero no molestan).

        `status` filtra por estado de aprobación. Sin él vienen TODOS los
        vigentes, que es lo que quieren el panel de la UI y el compactador
        (para no re-emitir un hecho que ya está esperando revisión). Quien
        inyecta al experto pide `status="approved"` explícito.
        """
        where = "" if include_superseded else "AND superseded_at IS NULL "
        params: list[Any] = [project_slug]
        if status is not None:
            where += "AND status=? "
            params.append(status)
        params.append(limit)
        return await self.run(
            f"SELECT * FROM facts WHERE project_slug=? COLLATE NOCASE "
            f"{where}ORDER BY created_at DESC, id DESC LIMIT ?",
            tuple(params))

    async def set_fact_status(self, fact_id: int, status: str) -> bool:
        """Aprueba o rechaza un hecho. Devuelve False si el id no existe."""
        if status not in self.FACT_STATES:
            raise ValueError(f"status inválido: {status!r}")
        rows = await self.run(
            "UPDATE facts SET status=?, reviewed_at=? WHERE id=? RETURNING id",
            (status, now_iso(), fact_id))
        return bool(rows)

    async def supersede_facts(
        self, fact_ids: list[int], project_slug: str,
        superseded_by: Optional[str] = None,
    ) -> int:
        """Marca hechos como obsoletos (soft; el compactador detectó que
        una conversación posterior los contradice). Scopeado al proyecto
        para que un id ajeno no pise hechos de otro repo."""
        n = 0
        for fid in fact_ids:
            rows = await self.run(
                "UPDATE facts SET superseded_at=?, superseded_by=? "
                "WHERE id=? AND project_slug=? COLLATE NOCASE "
                "AND superseded_at IS NULL RETURNING id",
                (now_iso(), superseded_by, fid, project_slug))
            n += len(rows)
        return n

    async def delete_fact(self, fact_id: int) -> bool:
        """Borra un fact por id (curación desde la Admin UI, 2026-07-12).
        La tabla sigue siendo append-only para el compactador; esto es
        el botón de "sacar un fact que quedó viejo o mal destilado"."""
        rows = await self.run(
            "DELETE FROM facts WHERE id=? RETURNING id", (fact_id,))
        return bool(rows)

    # ---- skill_drafts (autoaprendizaje 2026-07-12: aprobación humana) ----

    async def add_skill_draft(
        self, *, name: str, description: str, content: str,
        project_slug: Optional[str] = None,
        source_conversation: Optional[str] = None,
    ) -> int:
        rows = await self.run(
            "INSERT INTO skill_drafts (name, description, content, "
            "project_slug, source_conversation) VALUES (?,?,?,?,?) "
            "RETURNING id",
            (name, description, content, project_slug, source_conversation))
        return rows[0]["id"]

    async def list_skill_drafts(
        self, status: Optional[str] = None, limit: int = 100,
    ) -> list[dict]:
        """Lista SIN content (puede ser largo; pedirlo por id)."""
        cols = ("id, name, description, project_slug, source_conversation, "
                "status, approved_path, created_at, reviewed_at, "
                "length(content) AS content_len")
        where = "WHERE status=?" if status else ""
        params = ([status] if status else []) + [limit]
        return await self.run(
            f"SELECT {cols} FROM skill_drafts {where} "
            "ORDER BY created_at DESC, id DESC LIMIT ?", tuple(params))

    async def get_skill_draft(self, draft_id: int) -> Optional[dict]:
        rows = await self.run(
            "SELECT * FROM skill_drafts WHERE id=?", (draft_id,))
        return rows[0] if rows else None

    async def update_skill_draft(
        self, draft_id: int, *, name: Optional[str] = None,
        description: Optional[str] = None, content: Optional[str] = None,
    ) -> bool:
        """Edición pre-aprobación (solo drafts pending). True si actualizó."""
        sets, params = [], []
        for col, val in (("name", name), ("description", description),
                         ("content", content)):
            if val is not None:
                sets.append(f"{col}=?")
                params.append(val)
        if not sets:
            return False
        rows = await self.run(
            f"UPDATE skill_drafts SET {', '.join(sets)} "
            "WHERE id=? AND status='pending' RETURNING id",
            (*params, draft_id))
        return bool(rows)

    async def set_skill_draft_status(
        self, draft_id: int, status: str,
        approved_path: Optional[str] = None,
    ) -> bool:
        rows = await self.run(
            "UPDATE skill_drafts SET status=?, approved_path=?, "
            "reviewed_at=? WHERE id=? RETURNING id",
            (status, approved_path, now_iso(), draft_id))
        return bool(rows)

    async def delete_skill_draft(self, draft_id: int) -> bool:
        rows = await self.run(
            "DELETE FROM skill_drafts WHERE id=? RETURNING id", (draft_id,))
        return bool(rows)

    async def count_skill_drafts_pending(self) -> int:
        rows = await self.run(
            "SELECT COUNT(*) AS n FROM skill_drafts WHERE status='pending'")
        return rows[0]["n"]

    # ---- night_runs (ADR-028: modo nocturno) ----

    async def create_night_run(
        self, run_id: str, project_slug: str, deadline_at: str,
    ) -> None:
        await self.run(
            "INSERT INTO night_runs (id, project_slug, started_at, "
            "deadline_at) VALUES (?,?,?,?)",
            (run_id, project_slug, now_iso(), deadline_at))

    async def finish_night_run(
        self, run_id: str, *, end_reason: str, prs_opened: int = 0,
        tasks_done: int = 0, tasks_discarded: int = 0,
        report_path: Optional[str] = None, error: Optional[str] = None,
        branch: Optional[str] = None, pr_url: Optional[str] = None,
    ) -> None:
        await self.run(
            "UPDATE night_runs SET ended_at=?, end_reason=?, prs_opened=?, "
            "tasks_done=?, tasks_discarded=?, report_path=?, "
            "branch=?, pr_url=?, error=? WHERE id=?",
            (now_iso(), end_reason, prs_opened, tasks_done, tasks_discarded,
             report_path, branch, pr_url, error, run_id))

    # ---- plantillas de directiva del modo nocturno (2026-08-27) ----

    async def list_night_templates(
        self, project_slug: str = "",
    ) -> list[dict]:
        """Plantillas visibles para un proyecto: las suyas + las globales.

        Sin `project_slug` devuelve TODAS (es lo que necesita el panel de
        administracion, que las edita sin estar parado en un proyecto).
        Las del proyecto van primero: son las mas especificas y es lo que
        el operador busca cuando abre el selector desde un hilo.
        """
        if not project_slug:
            return await self.run(
                "SELECT * FROM night_templates "
                "ORDER BY project_slug, nombre COLLATE NOCASE")
        return await self.run(
            "SELECT * FROM night_templates "
            "WHERE project_slug = ? COLLATE NOCASE OR project_slug = '' "
            "ORDER BY (project_slug = '') , nombre COLLATE NOCASE",
            (project_slug,))

    async def get_night_template(
        self, nombre: str, project_slug: str = "",
    ) -> Optional[dict]:
        """Busca por nombre: primero la del proyecto, si no la global.

        Ese orden es la razon de ser del scope — una plantilla del
        proyecto TAPA a la global del mismo nombre, que es lo que deja
        tener una "release" generica y una "release" propia de sample-app.
        """
        rows = await self.run(
            "SELECT * FROM night_templates "
            "WHERE nombre = ? COLLATE NOCASE "
            "AND (project_slug = ? COLLATE NOCASE OR project_slug = '') "
            "ORDER BY (project_slug = '') LIMIT 1",
            (nombre, project_slug))
        return rows[0] if rows else None

    async def save_night_template(
        self, nombre: str, directiva: str, *,
        project_slug: str = "", notas: str = "",
    ) -> None:
        """Alta o actualizacion por (project_slug, nombre)."""
        ahora = now_iso()
        await self.run(
            "INSERT INTO night_templates "
            "  (project_slug, nombre, directiva, notas, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_slug, nombre) DO UPDATE SET "
            "  directiva = excluded.directiva, "
            "  notas = excluded.notas, "
            "  updated_at = excluded.updated_at",
            (project_slug or "", nombre, directiva, notas, ahora, ahora))

    async def delete_night_template(
        self, nombre: str, project_slug: str = "",
    ) -> bool:
        """True si borro algo. Exacto por scope: borrar la del proyecto
        NO puede llevarse puesta la global del mismo nombre."""
        antes = await self.run(
            "SELECT id FROM night_templates WHERE nombre = ? COLLATE NOCASE "
            "AND project_slug = ? COLLATE NOCASE", (nombre, project_slug or ""))
        if not antes:
            return False
        await self.run(
            "DELETE FROM night_templates WHERE nombre = ? COLLATE NOCASE "
            "AND project_slug = ? COLLATE NOCASE", (nombre, project_slug or ""))
        return True

    async def get_night_run(self, run_id: str) -> Optional[dict]:
        rows = await self.run("SELECT * FROM night_runs WHERE id=?", (run_id,))
        return rows[0] if rows else None

    async def active_night_run(self, project_slug: str) -> Optional[dict]:
        """Run sin terminar del proyecto (lock: 1 run activo por proyecto)."""
        rows = await self.run(
            "SELECT * FROM night_runs WHERE project_slug=? COLLATE NOCASE "
            "AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
            (project_slug,))
        return rows[0] if rows else None

    async def list_night_runs(
        self, project_slug: Optional[str] = None, limit: int = 10,
    ) -> list[dict]:
        where = "WHERE project_slug=? COLLATE NOCASE" if project_slug else ""
        params = ([project_slug] if project_slug else []) + [limit]
        return await self.run(
            f"SELECT * FROM night_runs {where} "
            "ORDER BY started_at DESC LIMIT ?", tuple(params))

    # ---- night_questions (Iter 9.7) ----
    # El orchestrator escribe una pregunta cuando necesita input del
    # humano (fallo de Fase 1 o decisión entre bloques lógicos).
    # Bloquea en ask_and_wait() hasta que get_night_question_answer()
    # devuelva algo distinto de None o hasta que pase el deadline.

    # ---- conexiones SQL para el chat (2026-08-16) ----

    async def list_db_connections(self) -> list[dict]:
        return await self.run(
            "SELECT * FROM db_connections ORDER BY alias")

    async def get_db_connection(self, alias: str) -> Optional[dict]:
        rows = await self.run(
            "SELECT * FROM db_connections WHERE alias=?", (alias,))
        return rows[0] if rows else None

    async def upsert_db_connection(self, alias: str, dsn: str, *,
                                   descripcion: str = "",
                                   escribir: bool = False) -> dict:
        await self.run(
            "INSERT INTO db_connections (alias, dsn, descripcion, escribir) "
            "VALUES (?,?,?,?) ON CONFLICT(alias) DO UPDATE SET "
            "dsn=excluded.dsn, descripcion=excluded.descripcion, "
            "escribir=excluded.escribir",
            (alias, dsn, descripcion, 1 if escribir else 0))
        return await self.get_db_connection(alias)

    async def delete_db_connection(self, alias: str) -> bool:
        rows = await self.run(
            "DELETE FROM db_connections WHERE alias=? RETURNING alias",
            (alias,))
        return bool(rows)

    # ---- preguntas del experto al humano (2026-08-16) ----

    # ---- grafo de tareas (F1, 2026-08-17) ----
    #
    # La lógica del grafo (orden, bloqueo, reintentos) vive en
    # `relay/grafo.py` y NO acá: acá solo entra y sale de SQLite. Esa
    # separación es lo que hace que las decisiones caras se puedan
    # probar sin levantar una base ni un modelo.

    async def create_task_graph(
        self, graph_id: str, objetivo: str, *, tareas: list,
        conversation_id: Optional[str] = None,
        project_slug: Optional[str] = None,
    ) -> dict:
        """Guarda un grafo entero. `tareas` son dicts con `id` y `deps`.

        Valida ANTES de escribir (ciclos, deps colgadas, ids repetidos):
        el grafo lo escribe un LLM y un grafo inválido a medio guardar es
        peor que uno rechazado — el primero se descubre tres nodos
        después, con trabajo ya hecho encima.
        """
        from . import grafo as grafo_mod

        nodos = [grafo_mod.Nodo(
            id=t["id"], titulo=t.get("titulo") or "",
            deps=tuple(t.get("deps") or ()),
            idempotente=bool(t.get("idempotente")),
            detalle=t.get("detalle") or "", orden=int(t.get("orden") or i),
            archivos=tuple(t.get("archivos") or ()),
            max_intentos=int(t.get("max_intentos") or 2))
            for i, t in enumerate(tareas)]
        grafo_mod.validar(nodos)

        ahora = now_iso()
        # TODO en una transacción. Antes eran llamadas sueltas y el
        # INSERT del grafo commiteaba solo: si fallaba una tarea —el 24/8
        # fue un choque de `tasks.id`, que es PK global— quedaba un grafo
        # `activo` con cero tareas, que además traba el hilo porque
        # `active_task_graph` lo devuelve y no deja armar otro.
        sentencias: list = [(
            "INSERT INTO task_graphs (id, conversation_id, project_slug, "
            "objetivo, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (graph_id, conversation_id, project_slug, objetivo, ahora, ahora))]
        for n in nodos:
            sentencias.append((
                "INSERT INTO tasks (id, graph_id, titulo, detalle, "
                "idempotente, max_intentos, orden, archivos) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (n.id, graph_id, n.titulo, n.detalle,
                 1 if n.idempotente else 0, n.max_intentos, n.orden,
                 json.dumps(list(n.archivos)))))
            for d in n.deps:
                sentencias.append((
                    "INSERT INTO task_deps (graph_id, task_id, depende_de) "
                    "VALUES (?,?,?)", (graph_id, n.id, d)))
        await self.run_tx(sentencias)
        return await self.get_task_graph(graph_id)

    async def get_task_graph(self, graph_id: str) -> Optional[dict]:
        filas = await self.run(
            "SELECT * FROM task_graphs WHERE id=?", (graph_id,))
        if not filas:
            return None
        g = dict(filas[0])
        g["tasks"] = await self.list_tasks(graph_id)
        return g

    async def list_tasks(self, graph_id: str) -> list[dict]:
        """Las tareas con sus `deps` resueltas, en orden estable."""
        tareas = await self.run(
            "SELECT * FROM tasks WHERE graph_id=? ORDER BY orden, id",
            (graph_id,))
        aristas = await self.run(
            "SELECT task_id, depende_de FROM task_deps WHERE graph_id=?",
            (graph_id,))
        por_tarea: dict = {}
        for a in aristas:
            por_tarea.setdefault(a["task_id"], []).append(a["depende_de"])
        for t in tareas:
            t["deps"] = sorted(por_tarea.get(t["id"], []))
        return tareas

    async def add_tasks_to_graph(
        self, graph_id: str, tareas: list, *,
        reemplaza: Optional[str] = None,
    ) -> list[str]:
        """Agrega tareas a un grafo QUE YA ESTÁ CORRIENDO (Fase 2A, 2026-09-02).

        `orquestador._vueltas` relee el grafo entero en cada iteración
        (`get_task_graph`), así que las tareas agregadas acá se levantan
        solas en la vuelta siguiente — no hace falta tocar el scheduler.

        `tareas` son dicts con `id` y `deps`, mismo formato que
        `create_task_graph`: quien llama es responsable de que los ids
        ya vengan en su forma final y única (mismo criterio de
        `planificador._ids_unicos` — `tasks.id` es PK GLOBAL, no por
        grafo; acá solo se VERIFICA la colisión, no se resuelve).

        `reemplaza`: el id de una tarea EXISTENTE que se subdivide. Dos
        cosas pasan para que el grafo no quede roto en silencio:
          - los DEPENDIENTES de `reemplaza` (filas de `task_deps` con
            `depende_de=reemplaza`) se re-apuntan a TODAS las tareas
            nuevas — si no, quedarían esperando para siempre a un nodo
            que nunca va a estar `hecho`.
          - las tareas nuevas heredan las deps que tenía `reemplaza`,
            para no arrancar antes de que esas dependencias originales
            cierren.
        `reemplaza` en sí NO se toca (ni se borra ni cambia de estado):
        qué pasa con el nodo padre es otra decisión, no de acá.

        Todo o nada (`run_tx`): si una tarea no se puede insertar, no
        puede quedar el grafo con las demás a medio meter — el mismo
        bug que ya pasó con `create_task_graph` el 24/8.

        Devuelve los ids de las tareas agregadas.
        """
        from . import grafo as grafo_mod

        existentes = await self.list_tasks(graph_id)
        if not existentes:
            raise ValueError(f"grafo sin tareas o inexistente: {graph_id}")
        por_id = {t["id"]: t for t in existentes}

        nuevos_ids = [t["id"] for t in tareas]
        if not nuevos_ids:
            raise ValueError("tareas vacío: nada que agregar")
        if len(set(nuevos_ids)) != len(nuevos_ids):
            raise grafo_mod.GrafoInvalido(
                "ids repetidos entre las tareas nuevas")

        # tasks.id es PK GLOBAL (ver create_task_graph): un id que ya
        # existe en CUALQUIER grafo —este u otro— revienta el INSERT
        # con un IntegrityError feo en vez de un error legible.
        placeholders = ",".join("?" * len(nuevos_ids))
        choques = {f["id"] for f in await self.run(
            f"SELECT id FROM tasks WHERE id IN ({placeholders})",
            tuple(nuevos_ids))}
        if choques:
            raise grafo_mod.GrafoInvalido(
                f"id(s) ya existen en la base: {sorted(choques)}")

        deps_heredadas: tuple = ()
        if reemplaza is not None:
            if reemplaza not in por_id:
                raise ValueError(
                    "reemplaza apunta a una tarea que no está en el "
                    f"grafo: {reemplaza!r}")
            deps_heredadas = tuple(por_id[reemplaza]["deps"])

        base_orden = max(
            (int(t.get("orden") or 0) for t in existentes), default=0) + 1
        nodos_nuevos = []
        for i, t in enumerate(tareas):
            deps = tuple(dict.fromkeys(
                list(t.get("deps") or ()) + list(deps_heredadas)))
            orden = t.get("orden")
            nodos_nuevos.append(grafo_mod.Nodo(
                id=t["id"], titulo=t.get("titulo") or "", deps=deps,
                idempotente=bool(t.get("idempotente")),
                detalle=t.get("detalle") or "",
                orden=int(orden) if orden is not None else base_orden + i,
                archivos=tuple(t.get("archivos") or ()),
                max_intentos=int(t.get("max_intentos") or 2)))

        # Validar el grafo COMPLETO (existentes + nuevos) TAL COMO VA A
        # QUEDAR, no el de antes: si `reemplaza` tiene dependientes, acá
        # abajo se les borra la arista a `reemplaza` y se les crea una
        # por cada tarea nueva — validar contra `t["deps"]` sin ese
        # reapunte es validar un grafo distinto del que se escribe, y un
        # ciclo que el reapunte MISMO crea (dependiente → nueva → … →
        # dependiente) no se ve nunca. Se ajustan los nodos existentes
        # EN MEMORIA con el mismo reapunte antes de llamar a `validar`.
        ids_nuevos = [n.id for n in nodos_nuevos]
        nodos_existentes = []
        for t in existentes:
            deps_t = t["deps"]
            if reemplaza is not None and reemplaza in deps_t:
                deps_t = [d for d in deps_t if d != reemplaza] + ids_nuevos
            nodos_existentes.append(grafo_mod.Nodo.desde_fila(t, deps_t))
        grafo_mod.validar(nodos_existentes + nodos_nuevos)

        sentencias: list = []
        for n in nodos_nuevos:
            sentencias.append((
                "INSERT INTO tasks (id, graph_id, titulo, detalle, "
                "idempotente, max_intentos, orden, archivos, parent_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (n.id, graph_id, n.titulo, n.detalle,
                 1 if n.idempotente else 0, n.max_intentos, n.orden,
                 json.dumps(list(n.archivos)), reemplaza)))
            for d in n.deps:
                sentencias.append((
                    "INSERT INTO task_deps (graph_id, task_id, "
                    "depende_de) VALUES (?,?,?)", (graph_id, n.id, d)))

        if reemplaza is not None:
            dependientes = await self.run(
                "SELECT task_id FROM task_deps WHERE graph_id=? "
                "AND depende_de=?", (graph_id, reemplaza))
            if dependientes:
                sentencias.append((
                    "DELETE FROM task_deps WHERE graph_id=? "
                    "AND depende_de=?", (graph_id, reemplaza)))
                for dep_row in dependientes:
                    for n in nodos_nuevos:
                        sentencias.append((
                            "INSERT INTO task_deps (graph_id, task_id, "
                            "depende_de) VALUES (?,?,?)",
                            (graph_id, dep_row["task_id"], n.id)))

        await self.run_tx(sentencias)
        return [n.id for n in nodos_nuevos]

    async def hay_turnos_humanos_despues(
        self, conversation_id: str, desde: str,
    ) -> bool:
        """¿El hilo siguió trabajando después de `desde` (ISO)?

        La usa el panel para decidir si un grafo sigue siendo el estado
        del hilo o ya es su historia. Va como query y no filtrando en
        Python la lista de turnos porque el panel pregunta esto cada
        2,5s: traer 200 filas con su `stages_json` para mirar una fecha
        costaría ~800 kB por poll.

        `source<>'grafo'`: los nodos del propio plan no cuentan, o todo
        grafo se declararía superado por sí mismo al correr su primero.
        """
        if not desde:
            return False
        filas = await self.run(
            "SELECT 1 FROM chats WHERE conversation_id=? AND started_at>? "
            "AND COALESCE(source,'')<>'grafo' LIMIT 1",
            (conversation_id, desde))
        return bool(filas)

    async def active_task_graph(self, conversation_id: str) -> Optional[dict]:
        """El grafo vivo de un hilo, o None. Uno por conversación."""
        return await self._active_graph_where(
            "conversation_id", conversation_id)

    async def active_task_graph_by_project(
        self, project_slug: str,
    ) -> Optional[dict]:
        """El grafo vivo de un proyecto, o None.

        Gemelo de `active_task_graph` pero filtrando por `project_slug`:
        la guarda por conversación se saltea cuando el POST no trae
        `conversation_id`, y dos grafos sobre el mismo repo se pisan
        los archivos sin que ninguno se entere.
        """
        return await self._active_graph_where(
            "project_slug", project_slug)

    async def _active_graph_where(
        self, column: str, value: str,
    ) -> Optional[dict]:
        # Whitelist: las dos únicas columnas por las que tiene sentido
        # buscar el grafo vivo. No es defensa contra inyección — el `?`
        # parametriza el valor—, sino para que el helper no se vuelva
        # un SQL genérico accidental.
        if column not in ("conversation_id", "project_slug"):
            raise ValueError(f"columna no soportada: {column}")
        filas = await self.run(
            f"SELECT id FROM task_graphs WHERE {column}=? "
            "AND estado='activo' ORDER BY created_at DESC LIMIT 1",
            (value,))
        return await self.get_task_graph(filas[0]["id"]) if filas else None

    async def last_task_graph(self, conversation_id: str) -> Optional[dict]:
        """El grafo más reciente de un hilo, TERMINADO o no.

        Existe para que un grafo no desaparezca de la vista al cerrarse.
        `active_task_graph` filtra `estado='activo'`, así que el panel
        caía a modo lineal en cuanto la última tarea pasaba a `hecho` — y
        con él se iba lo único que decía qué se hizo, en qué orden, qué
        falló y cuántos intentos costó. El grafo terminado es el registro
        más útil que deja un pedido grande: sirve para aprender del run
        siguiente, y estaba entero en la base sin forma de leerlo.
        """
        filas = await self.run(
            "SELECT id FROM task_graphs WHERE conversation_id=? "
            "ORDER BY created_at DESC LIMIT 1", (conversation_id,))
        return await self.get_task_graph(filas[0]["id"]) if filas else None

    async def list_active_graphs(self) -> list[str]:
        """Los grafos que quedaron `activo`. Los mira el barrido del boot.

        Un grafo `activo` al arrancar el relay es, por definición, uno
        que se cortó: nadie lo está corriendo todavía en este proceso.
        """
        filas = await self.run(
            "SELECT id FROM task_graphs WHERE estado='activo' "
            "ORDER BY created_at")
        return [f["id"] for f in filas]

    async def update_task(self, task_id: str, **campos) -> Optional[dict]:
        """Actualiza una tarea. Solo columnas conocidas.

        Whitelist y no `**campos` directo al SQL: quien llama es el
        orquestador con datos que vienen de un modelo.
        """
        permitidas = {"estado", "intentos", "modelo", "chat_id", "resultado",
                      "error", "started_at", "ended_at", "idempotente",
                      "max_intentos", "titulo", "detalle", "archivos"}
        sets, params = [], []
        for k, v in campos.items():
            if k not in permitidas:
                raise ValueError(f"columna no actualizable: {k}")
            sets.append(f"{k}=?")
            params.append(v)
        if not sets:
            return None
        params.append(task_id)
        await self.run(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", tuple(params))
        filas = await self.run("SELECT * FROM tasks WHERE id=?", (task_id,))
        if filas:
            await self.run(
                "UPDATE task_graphs SET updated_at=? WHERE id=?",
                (now_iso(), filas[0]["graph_id"]))
        return filas[0] if filas else None

    def _claim_files_tx(self, task_id: str, graph_id: str,
                        archivos: list) -> list:
        """Cuerpo transaccional de `claim_task_files`.

        `run()` abre y commitea una conexión POR sentencia, y `run_tx()`
        recibe una lista de sentencias armada de antemano — ninguno de
        los dos sirve para "leer, decidir en Python, escribir" sin
        soltar el candado en el medio. Con eso, dos llamadas concurrentes
        leen el mismo estado ANTES de que cualquiera inserte, y las dos
        se creen dueñas: medido con la `Database` real, 9 de 40 intentos
        concurrentes (22%) dejaban dos dueños del mismo archivo.

        La solución es una sola conexión, un solo hilo, `BEGIN IMMEDIATE`
        (toma el candado de escritura YA, no al primer INSERT como el
        `BEGIN` implícito de sqlite3) para que la segunda llamada quede
        bloqueada en el `_connect()`/BEGIN hasta que la primera commitee
        o revierta — ahí sí, serializado de verdad.
        """
        from . import grafo as grafo_mod

        conn = self._connect()
        conn.isolation_level = None  # autocommit off a mano: BEGIN propio
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # El scope del choque es el proyecto del grafo, resuelto
                # ACA adentro (no lo manda el caller: el único caller de
                # producción, `orquestador.correr_grafo`, tiene el
                # `graph_id` pero no el `project`). '' si el grafo no
                # resuelve a ningún proyecto — pasa con grafos de test
                # que no lo declaran, y ahí siguen chocando entre sí
                # como antes de esta columna.
                fila = conn.execute(
                    "SELECT project_slug FROM task_graphs WHERE id=?",
                    (graph_id,)).fetchone()
                slug = (fila["project_slug"] or "") if fila else ""

                # El choque se decide con el MISMO predicado que usa el
                # planificador (`grafo._pisa`), no con un `archivo=?`:
                # una tarea que declara la carpeta `src/` choca con otra
                # que tiene tomado `src/x.py`, y un igual-a-igual no lo
                # ve. Se compara en Python y no con LIKE porque un
                # nombre de archivo puede traer `%` o `_`, que en LIKE
                # son comodines y harían coincidir de más.
                ajenas = [r["archivo"] for r in conn.execute(
                    "SELECT archivo FROM task_file_claims "
                    "WHERE task_id<>? AND project_slug=?",
                    (task_id, slug)).fetchall()]

                ahora = now_iso()
                vence = _vence_en(config.claim_ttl_s())
                rechazados = []
                for a in archivos:
                    norm = grafo_mod.norm_archivo(str(a))
                    if not norm:
                        continue
                    if any(grafo_mod._pisa(norm, otra) for otra in ajenas):
                        rechazados.append(norm)
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO task_file_claims "
                        "(archivo, task_id, graph_id, project_slug, "
                        "tomado_at, vence_at) VALUES (?,?,?,?,?,?)",
                        (norm, task_id, graph_id, slug, ahora, vence))
                conn.commit()
                return rechazados
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    async def claim_task_files(self, task_id: str, graph_id: str,
                               archivos: list) -> list:
        """Toma los archivos para una tarea. Devuelve los que NO pudo.

        No falla si otro los tiene: devuelve la lista y quien llama
        decide. El orquestador no debería llegar acá con conflictos
        (`grafo.elegibles` ya los filtró), pero esta es la capa que de
        verdad garantiza, y una capa que garantiza tiene que poder decir
        que no. Ver `_claim_files_tx` por qué esto no es un `run()` más.
        """
        return await asyncio.to_thread(
            self._claim_files_tx, task_id, graph_id, archivos)

    async def renovar_claims(self, task_ids: list) -> int:
        """Corre el vencimiento de las reservas de tareas que siguen vivas.

        El latido de la lease. Lo llama el loop del orquestador con lo
        que tiene `en_curso`, o sea con las tareas que ese proceso
        está corriendo de verdad —no con las que la tabla *dice* que
        corren, que es justo la diferencia que hace útil todo esto—.

        Sin esto una tarea larga y viva perdería sus archivos al vencer
        el TTL, que es peor que el cuelgue que la lease arregla.
        """
        if not task_ids:
            return 0
        hueco = ",".join("?" for _ in task_ids)
        filas = await self.run(
            f"UPDATE task_file_claims SET vence_at=? "
            f"WHERE task_id IN ({hueco}) RETURNING archivo",
            (_vence_en(config.claim_ttl_s()), *task_ids))
        return len(filas)

    async def release_task_files(self, task_id: str) -> None:
        await self.run("DELETE FROM task_file_claims WHERE task_id=?",
                       (task_id,))

    async def files_claimed_by_others(self, task_id: str) -> set:
        """Archivos tomados por OTRA tarea viva. Va a `Permisos.reservadas`.

        `Permisos.para` ya está anclado a UN repo (el `project` de la
        tarea); si esto devolviera reservas de otros proyectos, el
        arreglo de `claim_task_files` (permitir la misma ruta relativa
        en dos proyectos) se anularía acá: la escritura rebotaría por
        una reserva ajena al repo que se está tocando. Se resuelve el
        `project_slug` de `task_id` vía su grafo; `''` si no resuelve
        (tarea o grafo inexistente, o grafo sin proyecto) — mismo
        default que usa `claim_task_files`.
        """
        filas = await self.run(
            "SELECT archivo FROM task_file_claims WHERE task_id<>? "
            "AND project_slug=COALESCE("
            "  (SELECT tg.project_slug FROM tasks t "
            "   JOIN task_graphs tg ON tg.id=t.graph_id WHERE t.id=?), '')",
            (task_id, task_id))
        return {f["archivo"] for f in filas}

    async def release_dead_claims(self, graph_id: Optional[str] = None) -> int:
        """Suelta las reservas de tareas que ya no están corriendo.

        Sin esto, un run que muere sin soltar (relay reiniciado, proceso
        matado) deja el archivo tomado para siempre y el grafo se traba
        sin que nadie pueda decir por qué.

        **El barrido es global a propósito**, aunque lo llame un grafo en
        particular. La regla es *barrer con al menos el alcance con que
        se bloquea*: si el barrido mirara solo las del grafo que arranca,
        una reserva que quedó de un grafo anterior —o de uno que después
        se borró, que no cascadea porque `task_file_claims` no tiene FK—
        bloquearía para siempre a todos los que vinieran. Barrer con
        MENOS alcance del que bloquea es justo el caso que deja el
        archivo tomado por nadie.

        Ojo, que la premisa cambió el 8/9/2026: `files_claimed_by_others`
        bloqueaba mirando TODAS las reservas vivas y ahora mira solo las
        del mismo proyecto. O sea que este barrido pasó a ser MÁS ancho
        que el bloqueo, no igual. Eso sigue siendo seguro —soltar de más
        no traba a nadie—, pero si alguna vez se achica el barrido, el
        límite ya no es "global": es el `project_slug`.

        Un `task_id` que ya no existe en `tasks` también se barre: es una
        reserva huérfana de un grafo borrado.

        **Y las que vencieron** (2026-09-04). La regla por estado sola no
        alcanzaba: `sanar()` cura las tareas DEL GRAFO QUE ARRANCA, pero
        `files_claimed_by_others` bloquea con las de TODOS. Un grafo que
        murió con nodos en `corriendo` retiene sus archivos para siempre
        —nadie lo va a sanar si no vuelve a correr— y traba a cualquier
        otro. Es la misma asimetría de alcance que arregla el párrafo de
        arriba, un nivel más arriba. No se arregla haciendo `sanar()`
        global: eso mataría las tareas de un grafo que SÍ está corriendo
        en paralelo. Hace falta distinguir vivo de huérfano, y la
        señal es el latido: una tarea viva renueva (`renovar_claims`),
        una huérfana no.

        Una reserva con `vence_at` NULL es anterior a la lease: no vence
        por reloj, solo la alcanza la regla por estado.

        `graph_id` queda solo para el log de quien pidió el barrido.
        """
        filas = await self.run(
            "DELETE FROM task_file_claims WHERE task_id NOT IN ("
            "  SELECT id FROM tasks WHERE estado='corriendo'"
            ") OR (vence_at IS NOT NULL AND vence_at < ?) "
            "RETURNING archivo", (now_iso(),))
        return len(filas)

    async def set_task_graph_state(self, graph_id: str, estado: str) -> None:
        await self.run(
            "UPDATE task_graphs SET estado=?, updated_at=? WHERE id=?",
            (estado, now_iso(), graph_id))

    async def set_task_graph_verificacion(self, graph_id: str,
                                          verificacion_json: str) -> None:
        """Guarda el veredicto de la verificacion de cierre del grafo.

        NO toca `updated_at`: ese campo lo mira
        `hay_turnos_humanos_despues` para decidir si el grafo sigue
        siendo lo que esta pasando en el hilo, y moverlo por escribir
        telemetria haria que un grafo viejo volviera a tapar la
        conversacion.
        """
        await self.run(
            "UPDATE task_graphs SET verificacion_json=? WHERE id=?",
            (verificacion_json, graph_id))

    async def create_expert_question(
        self, q_id: str, chat_id: str, question_json: str, *,
        conversation_id: Optional[str] = None,
        project_slug: Optional[str] = None, kind: str = "choice",
    ) -> int:
        """Anota una pregunta y JUBILA las que quedaron abiertas antes.

        Devuelve cuántas jubiló (2026-08-16).

        Por qué jubilar: una pregunta abierta de un turno anterior es una
        decisión que el hilo ya dejó atrás. Si el humano no la contestó y
        el experto volvió a preguntar, la vieja no solo es ruido —
        contestarla inyecta como turno siguiente una decisión sobre algo
        que ya no está en juego. Se veía como "la decisión de la UI se
        repite": cada turno agregaba una pregunta más a la pila y nadie
        sacaba las de antes.

        `superseded` y no `skipped`: skipped es "el humano la descartó",
        y esto no lo decidió el humano. Además `answer_expert_question`
        exige `status='open'`, así que una jubilada ya no se puede
        contestar de rebote desde una pestaña vieja.
        """
        # Por conversación si hay hilo; si no, por run. Sin esto, dos
        # conversaciones distintas del mismo proyecto se jubilarían entre
        # ellas.
        # …salvo las de un NODO DE GRAFO que sigue esperando (2026-08-31).
        #
        # Jubilar por conversación asume un solo hilo de decisión, que es
        # verdad en el chat: cada turno reemplaza al anterior. En un
        # grafo NO: varios nodos corren en paralelo sobre la MISMA
        # conversación y cada uno pregunta por su propia tarea, así que
        # la pregunta del nodo B mataba la del nodo A. Y como nada saca
        # a esa tarea de `esperando_humano` —y `answer_expert_question`
        # exige `status='open'`— el nodo quedaba inalcanzable: ni
        # contestándolo desde la UI ni con `/graphs/{id}/resume`.
        # Medido: 10 tareas trabadas en 3 grafos, algunas desde hacía 6
        # horas, arrastrando a sus dependientes.
        #
        # Protege a CUALQUIER chat que sea un nodo de grafo, sin mirar el
        # estado de la tarea. La primera versión de esto exigía
        # `estado='esperando_humano'` y llegaba tarde: la tarea recién
        # pasa a ese estado cuando el run TERMINA (`_cerrar_tarea`),
        # mientras que la pregunta nace a mitad del run. En esa ventana
        # el nodo figura `corriendo` y quedaba desprotegido. Se vio en
        # vivo el 1/9: el nodo de EmailSender preguntó 03:28:30 y el de
        # i18n le jubiló la pregunta 03:28:39, nueve segundos después.
        #
        # La tarea que RE-pregunta no se auto-protege: al re-ejecutarse
        # el run nuevo le pisa el `chat_id` a la fila de la tarea, así
        # que el chat viejo deja de pertenecer a ningún nodo y su
        # pregunta vuelve a ser jubilable. No quedan dos abiertas para el
        # mismo nodo.
        if conversation_id:
            filas = await self.run(
                "UPDATE expert_questions SET status='superseded', "
                "answered_at=? WHERE conversation_id=? AND status='open' "
                "AND chat_id NOT IN ("
                "  SELECT chat_id FROM tasks WHERE COALESCE(chat_id,'')!=''"
                ") RETURNING id", (now_iso(), conversation_id))
        else:
            filas = await self.run(
                "UPDATE expert_questions SET status='superseded', "
                "answered_at=? WHERE chat_id=? AND status='open' "
                "RETURNING id", (now_iso(), chat_id))
        await self.run(
            "INSERT INTO expert_questions "
            "(id, chat_id, conversation_id, project_slug, kind, "
            " question_json, asked_at) VALUES (?,?,?,?,?,?,?)",
            (q_id, chat_id, conversation_id, project_slug, kind,
             question_json, now_iso()))
        return len(filas)

    async def get_expert_question(self, q_id: str) -> Optional[dict]:
        rows = await self.run(
            "SELECT * FROM expert_questions WHERE id=?", (q_id,))
        return rows[0] if rows else None

    async def list_expert_questions(
        self, *, conversation_id: Optional[str] = None,
        chat_id: Optional[str] = None, only_open: bool = True,
        limit: int = 20,
    ) -> list[dict]:
        where, params = [], []
        if conversation_id:
            where.append("conversation_id=?"); params.append(conversation_id)
        if chat_id:
            where.append("chat_id=?"); params.append(chat_id)
        if only_open:
            where.append("status='open'")
        sql = "SELECT * FROM expert_questions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY asked_at DESC LIMIT {int(limit)}"
        return await self.run(sql, tuple(params))

    async def answer_expert_question(
        self, q_id: str, answer_json: str,
    ) -> bool:
        """Responde una pregunta ABIERTA. False si ya estaba cerrada.

        El `status='open'` en el WHERE es lo que hace idempotente al
        doble click del humano (y a Discord + Admin UI respondiendo la
        misma pregunta): la segunda respuesta no pisa a la primera.
        """
        rows = await self.run(
            "UPDATE expert_questions SET answer_json=?, answered_at=?, "
            "status='answered' WHERE id=? AND status='open' RETURNING id",
            (answer_json, now_iso(), q_id))
        return bool(rows)

    async def skip_expert_question(self, q_id: str) -> None:
        await self.run(
            "UPDATE expert_questions SET status='skipped', answered_at=? "
            "WHERE id=? AND status='open'", (now_iso(), q_id))

    async def create_night_question(
        self, q_id: str, run_id: str, phase: str,
        question_json: str, notify_target: Optional[str] = None,
        notify_via: str = "both",
    ) -> None:
        await self.run(
            "INSERT INTO night_questions "
            "(id, run_id, phase, question_json, asked_at, "
            " notified_via, notify_target) VALUES (?,?,?,?,?,?,?)",
            (q_id, run_id, phase, question_json, now_iso(),
             notify_via, notify_target))

    async def get_night_question(self, q_id: str) -> Optional[dict]:
        rows = await self.run(
            "SELECT * FROM night_questions WHERE id=?", (q_id,))
        return rows[0] if rows else None

    async def get_night_question_answer(
        self, q_id: str,
    ) -> Optional[dict]:
        """Retorna el answer_json parseado si la pregunta está respondida,
        None si todavía no. Usado por ask_and_wait() en su poll loop."""
        rows = await self.run(
            "SELECT answer_json, answered_at FROM night_questions "
            "WHERE id=? AND answered_at IS NOT NULL",
            (q_id,))
        if not rows:
            return None
        answer_json = rows[0]["answer_json"]
        try:
            return json.loads(answer_json) if answer_json else {}
        except json.JSONDecodeError:
            return {}

    async def answer_night_question(
        self, q_id: str, answer: dict,
    ) -> bool:
        """Guarda la respuesta del humano. Idempotente: si ya estaba
        respondida, retorna False (la API HTTP responde 409 en ese caso).

        Bug fix 2026-07-18: era SELECT-luego-UPDATE sin transacción —
        dos respuestas concurrentes podían leer ambas `answered_at IS
        NULL` y recibir ambas True. La cláusula `WHERE answered_at IS
        NULL` del UPDATE impedía la doble escritura, pero el caller HTTP
        quedaba mintiendo sobre quién ganó. Ahora es atómico:
        `UPDATE ... RETURNING answered_at` y solo retornamos True si
        el RETURNING trajo una fila (osea, la nuestra ganó el race).
        """
        rows = await self.run(
            "UPDATE night_questions SET answered_at=?, answer_json=? "
            "WHERE id=? AND answered_at IS NULL "
            "RETURNING id",
            (now_iso(), json.dumps(answer), q_id))
        return bool(rows)

    async def skip_night_question(self, q_id: str) -> bool:
        """Cierra la pregunta sin respuesta (default). Misma semántica
        que answer_night_question pero con answer={}."""
        return await self.answer_night_question(q_id, {})

    async def list_night_questions(
        self, run_id: Optional[str] = None,
        only_open: bool = False,
    ) -> list[dict]:
        where_parts = []
        params: list[Any] = []
        if run_id:
            where_parts.append("run_id=?")
            params.append(run_id)
        if only_open:
            where_parts.append("answered_at IS NULL")
        where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        return await self.run(
            f"SELECT * FROM night_questions {where} "
            "ORDER BY asked_at DESC", tuple(params))

    async def orphan_open_questions(self) -> int:
        """Marca como skipped todas las preguntas abiertas de runs que
        no están activos en memoria. Llamado al startup del relay
        para limpiar preguntas huérfanas de runs que murieron con el
        proceso. Retorna cuántas cerró."""
        # Heurística: runs con ended_at != NULL cuyas preguntas siguen
        # abiertas. Si el run está vivo (ended_at IS NULL), no toca.
        rows = await self.run(
            "SELECT q.id FROM night_questions q "
            "JOIN night_runs r ON r.id = q.run_id "
            "WHERE q.answered_at IS NULL AND r.ended_at IS NOT NULL")
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        # Answer con {} = "skip". Mantiene el rastro en el morning report.
        for q_id in ids:
            await self.answer_night_question(q_id, {"skipped": True})
        return len(ids)


    # ---- memories (ADR-027: FTS5 sobre resúmenes compactados) ----

    async def add_memory(
        self, conversation_id: str, project_slug: str, summary: str,
    ) -> bool:
        """Indexa (o re-indexa) el resumen de una conversación."""
        if not self._fts_available:
            return False
        # idempotente: recompactar reemplaza la entrada previa
        await self.run(
            "DELETE FROM memories_fts WHERE conversation_id=?",
            (conversation_id,))
        await self.run(
            "INSERT INTO memories_fts (summary, project_slug, conversation_id) "
            "VALUES (?,?,?)", (summary, project_slug, conversation_id))
        return True

    async def delete_memory(self, conversation_id: str) -> bool:
        """Saca una memoria del retrieval (curación desde la Admin UI).

        Borra la fila FTS5 y limpia conversations.summary — las DOS rutas
        de search_memories (MATCH y fallback por recientes) dejan de
        devolverla. El historial (messages_json) queda intacto. True si
        la conversación existía y tenía summary.
        """
        if self._fts_available:
            await self.run(
                "DELETE FROM memories_fts WHERE conversation_id=?",
                (conversation_id,))
        rows = await self.run(
            "UPDATE conversations SET summary=NULL "
            "WHERE id=? AND summary IS NOT NULL RETURNING id",
            (conversation_id,))
        return bool(rows)

    @staticmethod
    def _fts_quote(query: str) -> str:
        """Sanitiza el query del usuario para MATCH: cada término entre
        comillas, así `AND`, `-`, `:` y demás operadores FTS5 no rompen."""
        terms = [t.replace('"', "") for t in query.split()]
        return " ".join(f'"{t}"' for t in terms if t)

    async def search_memories(
        self, project_slug: str, query: str = "", limit: int = 5,
    ) -> list[dict]:
        """Busca resúmenes por FTS5 scopeado a un proyecto (ADR-027).

        Sin query (o sin FTS5): devuelve los resúmenes más recientes del
        proyecto desde `conversations` directamente.
        """
        query = (query or "").strip()
        if query and self._fts_available:
            match = self._fts_quote(query)
            if match:
                try:
                    return await self.run(
                        "SELECT conversation_id, project_slug, summary, "
                        "bm25(memories_fts) AS rank FROM memories_fts "
                        "WHERE memories_fts MATCH ? AND project_slug=? "
                        "ORDER BY rank LIMIT ?",
                        (match, project_slug, limit))
                except sqlite3.OperationalError:
                    pass  # query FTS malformado pese al quoting → fallback
        rows = await self.run(
            "SELECT id AS conversation_id, project_slug, summary "
            "FROM conversations WHERE project_slug=? COLLATE NOCASE "
            "AND summary IS NOT NULL AND summary != '' "
            "ORDER BY closed_at DESC LIMIT ?", (project_slug, limit))
        return rows

    # ---- system_config (red + paths, gestionable desde la Admin UI) ----

    async def get_config(self, key: str, default: Optional[str] = None) -> Optional[str]:
        rows = await self.run(
            "SELECT value FROM system_config WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    async def set_config(self, key: str, value: str) -> None:
        await self.run(
            "INSERT INTO system_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=datetime('now')",
            (key, str(value)))

    async def all_config(self) -> dict[str, str]:
        rows = await self.run(
            "SELECT key, value FROM system_config ORDER BY key")
        return {r["key"]: r["value"] for r in rows}

    # ---- command_logs ----

    async def log_command(
        self, *, name: str, source: str, author: Optional[str],
        args: dict, response: str, duration_ms: int, status: str,
    ) -> None:
        await self.run(
            "INSERT INTO command_logs (command_name, source, author, args_json, "
            "response, duration_ms, status) VALUES (?,?,?,?,?,?,?)",
            (name, source, author, json.dumps(args, ensure_ascii=False),
             response[:4000], duration_ms, status),
        )

    # ---- stats ----

    async def stats(self) -> dict:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        chats = await self.run(
            "SELECT COUNT(*) AS n, COALESCE(SUM(tokens_in),0) AS tin, "
            "COALESCE(SUM(tokens_out),0) AS tout FROM chats "
            "WHERE started_at >= ?", (today,))
        cmds = await self.run(
            "SELECT COUNT(*) AS n FROM command_logs WHERE ts >= ?", (today,))
        return {
            "chats_today": chats[0]["n"],
            "tokens_in_today": chats[0]["tin"],
            "tokens_out_today": chats[0]["tout"],
            "commands_today": cmds[0]["n"],
        }
