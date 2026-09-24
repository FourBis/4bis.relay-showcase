"""Inicialización y migraciones idempotentes del esquema Relay."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3

from .db_schema import FTS_SCHEMA, SCHEMA
from .db_support import _MODELS_SEED, now_iso


class DatabaseSchemaMixin:

    def _init_schema_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            # Equipo: agrega las columnas nuevas sin reescribir usuarios previos.
            _user_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(users)")
            }
            if "display_name" not in _user_columns:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN display_name "
                    "TEXT NOT NULL DEFAULT ''")
            if "enabled" not in _user_columns:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN enabled "
                    "INTEGER NOT NULL DEFAULT 1")
            if "project_slugs_json" not in _user_columns:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN project_slugs_json "
                    "TEXT NOT NULL DEFAULT '[]'")
            conn.commit()
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
            try:
                conn.execute("ALTER TABLE conversations ADD COLUMN task_json TEXT NOT NULL DEFAULT '{}'")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            conn.executescript("""
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
            CREATE INDEX IF NOT EXISTS idx_conversation_events_fifo
                ON conversation_events(conversation_id, id);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_events_processing
                ON conversation_events(conversation_id) WHERE state='processing';
            """)
            conn.commit()
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
            # config. RELAY_OWNER_EMAIL lo pisa; el default es el dueño
            # de esta instalación. INSERT OR IGNORE: si alguien se
            # degradó a mano, el boot no lo vuelve a promover.
            _owner = os.environ.get(
                "RELAY_OWNER_EMAIL", "").strip().lower()
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
