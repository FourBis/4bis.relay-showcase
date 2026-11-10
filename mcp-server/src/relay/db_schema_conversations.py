"""Tablas, eventos y barreras de creación de conversaciones.

Este fragmento conserva su posición dentro del esquema compuesto; las
migraciones de instalaciones existentes permanecen en db_schema_init.
"""

CONVERSATION_SCHEMA = """-- ADR-025: conversaciones de expertos (working memory por hilo Discord).
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
-- Definitive validation rejection before a conversation row exists. No raw input
-- or provider/error text; these IDs remain fenced against late concurrent POSTs.
CREATE TABLE IF NOT EXISTS creation_rejections (
    conversation_id TEXT PRIMARY KEY,
    project_slug TEXT NOT NULL,
    requested_by TEXT NOT NULL COLLATE NOCASE,
    contract_version INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    rejected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_creation_rejections_request
    ON creation_rejections(project_slug COLLATE NOCASE,request_id);
CREATE TRIGGER IF NOT EXISTS reject_tombstoned_conversation
BEFORE INSERT ON conversations
WHEN EXISTS (SELECT 1 FROM creation_rejections WHERE conversation_id=NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'El identificador de creación ya fue rechazado');
END;
-- Additive trigger also protects an installation with the earlier CID-only
-- trigger. No unique index is imposed on pre-existing case-alias rejections.
CREATE TRIGGER IF NOT EXISTS reject_logical_tombstoned_conversation
BEFORE INSERT ON conversations
WHEN CASE WHEN json_valid(NEW.task_json) THEN
    json_type(NEW.task_json,'$.creation_request.version')='integer'
    AND json_extract(NEW.task_json,'$.creation_request.version')=2
    AND json_type(NEW.task_json,'$.creation_request.request_id')='text'
    AND EXISTS (SELECT 1 FROM creation_rejections
                WHERE project_slug=NEW.project_slug COLLATE NOCASE
                AND request_id=json_extract(NEW.task_json,'$.creation_request.request_id'))
    ELSE 0 END
BEGIN
    SELECT RAISE(ABORT, 'El identificador de creación ya fue rechazado');
END;
CREATE TABLE IF NOT EXISTS conversation_shares (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    email TEXT NOT NULL COLLATE NOCASE REFERENCES users(email),
    permission TEXT NOT NULL CHECK(permission IN ('read','collaborate')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT,
    PRIMARY KEY(conversation_id,email)
);
CREATE INDEX IF NOT EXISTS idx_conversation_shares_email ON conversation_shares(email,revoked_at);
CREATE TABLE IF NOT EXISTS attachment_uploaders (
    attachment_id TEXT NOT NULL,
    actor TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY(attachment_id,actor)
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
CREATE INDEX IF NOT EXISTS idx_conv_listing ON conversations(last_activity_at DESC, id);
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

"""
