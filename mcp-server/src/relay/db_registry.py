"""Migraciones puntuales y catálogo de MCPs."""
from __future__ import annotations

import json
import logging
import sqlite3


class DatabaseRegistryMixin:

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
        for m in DatabaseRegistryMixin._SEED_MCPS:
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
