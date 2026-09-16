"""Auto-reindex incremental de cbm vía file-watcher (Opción A, 2026-07-14).

El `auto_watch` interno de cbm NUNCA corre acá (no lo invocamos), así
que archivos nuevos quedaban sin indexar hasta un reindex manual desde
la Admin UI. Este módulo lo suple en el proceso relay:

2026-09-02: el relay SÍ mantiene un proceso cbm residente ahora
(sesión MCP persistente, `experts.py` / `server._warm_cbm_session`;
ADR-017 revertido, ver docs/DECISIONS.md) — pero corre con cwd neutro
y no vigila ningún repo. Este watcher sigue siendo el único que vigila
TODOS los proyectos; no se reemplaza.

- `watchdog` observa los `repo_path` de los proyectos enabled con
  `include_in_index=1` (snapshot al boot; alta/baja de proyectos →
  reiniciar el relay, mismo contrato que el index.html cacheado).
- Debounce por proyecto: ~QUIET_S sin eventos → dispara el MISMO
  `_start_index_job` de admin.py que usa el botón Reindex, SIN force.
  Validado contra el fuente de cbm (pipeline.c
  `try_incremental_or_delete_db`): con DB existente rutea al pipeline
  incremental por hashes — no re-escanea todo.
- Anti-solape: si el job del proyecto sigue corriendo, el pending queda
  y se reintenta en el próximo tick.
- Flag `CBM_AUTO_WATCH=0` lo apaga (default encendido). Sin binario cbm
  o sin watchdog instalado, degrada a no-op con warning.

Patrón de lifecycle calcado del sweeper/reaper: create_task en
_on_startup, cancel en _on_cleanup.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path, PurePosixPath

logger = logging.getLogger("relay.cbm_watcher")

QUIET_S = 5.0   # debounce: N segundos sin eventos en el repo → reindex
TICK_S = 2.0    # frecuencia del loop que evalúa el debounce

# Mismo ruido que bulk_index_repos/admin discover: dirs que no son código.
IGNORE_DIRS = {".git", "node_modules", ".venv", "venv", "obj", "bin",
               "dist", "build", "__pycache__", ".idea", ".vscode",
               "TestResults", ".gradle", "target", ".terraform",
               ".relay", "state"}
# Archivos que cambian solos (logs, DBs, locks): sin esto un log que se
# escribe seguido mantiene el pending eternamente vivo.
IGNORE_SUFFIXES = (".log", ".tmp", ".db", ".db-journal", ".db-wal",
                   ".db-shm", ".lock", ".swp")


def watch_enabled() -> bool:
    return os.environ.get("CBM_AUTO_WATCH", "1") != "0"


def is_noise(path: str) -> bool:
    """¿El evento viene de un dir/archivo que no afecta el índice?

    Los separadores se normalizan a `/` antes de partir la ruta: en un
    host POSIX, `Path(r"C:\repo\node_modules\a.js")` es UN solo
    componente, así que `node_modules` no matcheaba y el filtro dejaba
    pasar todo el ruido. En Windows —donde corre el watcher— funcionaba,
    pero atar el filtro al separador del host es innecesario: los eventos
    pueden venir de cualquier origen (tests, rutas configuradas a mano,
    un relay en Linux) y el que se cuela dispara un reindex de cbm.
    """
    normalizado = str(path).replace("\\", "/")
    p = PurePosixPath(normalizado)
    if p.name.lower().endswith(IGNORE_SUFFIXES):
        return True
    return any(part in IGNORE_DIRS for part in p.parts)


async def run(app) -> None:
    """Task de fondo: observa los repos y dispara reindexes incrementales.

    Sale silenciosamente (con warning) si el feature no puede correr:
    flag apagado, sin watchdog, sin binario cbm o sin proyectos.
    Cualquier excepción se loguea (nadie awaitea este task: sin el
    try/except moriría mudo, como pasó en el primer deploy)."""
    try:
        await _run(app)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "cbm_watcher murió; auto-reindex off hasta el próximo boot")


async def _run(app) -> None:
    if not watch_enabled():
        logger.info("cbm_watcher: apagado por CBM_AUTO_WATCH=0")
        return
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        logger.warning("cbm_watcher: watchdog no instalado; auto-reindex off")
        return
    from . import admin
    from .experts import cbm_binary_path
    if not cbm_binary_path():
        logger.warning("cbm_watcher: binario cbm no encontrado; auto-reindex off")
        return
    # OJO: NO importar DB_KEY de .server — con `python -m relay.server`
    # hay DOS copias del módulo (__main__ y relay.server) con AppKeys
    # distintas, y la del import no matchea el app state. admin.py ya
    # re-bindea las AppKeys de __main__; usamos la suya.
    db = app[admin.DB_KEY]
    projects = [p for p in await db.list_projects(enabled_only=True)
                if p.get("include_in_index", 1) and p.get("repo_path")
                and Path(p["repo_path"]).is_dir()]
    if not projects:
        logger.info("cbm_watcher: sin proyectos indexables; nada que vigilar")
        return

    # slug -> monotonic del último evento relevante. Lo escribe el thread
    # del Observer (asignación atómica por GIL) y lo lee/borra el loop.
    pending: dict[str, float] = {}
    running: dict[str, str] = {}  # slug -> job_id en vuelo

    class _Handler(FileSystemEventHandler):
        def __init__(self, slug: str) -> None:
            self.slug = slug

        def on_any_event(self, event) -> None:  # noqa: ANN001 — watchdog event
            if getattr(event, "is_directory", False):
                return
            if is_noise(getattr(event, "src_path", "") or ""):
                return
            pending[self.slug] = time.monotonic()

    observer = Observer()
    observer.daemon = True
    watched = {}
    for p in projects:
        try:
            observer.schedule(_Handler(p["slug"]), p["repo_path"], recursive=True)
            watched[p["slug"]] = p["repo_path"]
        except OSError as e:
            logger.warning("cbm_watcher: no pude vigilar %s (%s)", p["repo_path"], e)
    if not watched:
        return
    observer.start()
    logger.info("cbm_watcher: vigilando %d repos (debounce %.0fs): %s",
                len(watched), QUIET_S, ", ".join(sorted(watched)))
    try:
        while True:
            await asyncio.sleep(TICK_S)
            now = time.monotonic()
            for slug, last in list(pending.items()):
                if now - last < QUIET_S:
                    continue
                job_id = running.get(slug)
                if job_id is not None:
                    val = admin._get_job(job_id)
                    if isinstance(val, asyncio.Task) and not val.done():
                        continue  # job previo sigue; reintento próximo tick
                    running.pop(slug, None)
                pending.pop(slug, None)
                running[slug] = admin._start_index_job(slug, watched[slug])
                logger.info("cbm_watcher: reindex incremental %s (job %s)",
                            slug, running[slug])
    finally:
        observer.stop()
