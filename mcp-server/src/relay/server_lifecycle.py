"""Server domain handlers extracted from the composition entrypoint."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from aiohttp import web

from .server_common import (
    BG_TASKS_KEY, CBM_WARMUP_KEY, CBM_WATCHER_KEY, COMMANDS_KEY, DB_KEY,
    EXPORT_RETRY_KEY, GRAFOS_KEY, MCP_HEALTH_PROBE_KEY, MCP_INSTALLER_KEY,
    MCP_POOL_KEY, MCP_REAPER_KEY, NIGHT_KEY, NOTIFY_KEY, PROGRESS_KEY, RUNNING_KEY,
    SESSIONS_KEY, SKILLS_KEY, SKILL_BROWSER_KEY, STATE_DIR_KEY, SWEEPER_KEY,
    SessionRegistry, _bg_tasks, _get_api_key, logger, relay_config,
)
from . import cbm_watcher
from . import experts
from . import finalization
from . import identity
from . import mcp_pool
from . import notify
from .commands import CommandRegistry
from .db import Database
from .mcp_installer import McpInstaller
from .mcp_pool import McpPool
from .notify import NotifyClient, default_bot_url
from .skills import SkillBrowser, SkillCache
from .server_conversation_jobs import _autoclose_sweeper, _export_retry_loop
from . import task_service
# ---------- app factory ----------


async def _reap_zombie_chats(db: Database) -> int:
    """Cierra los chats que quedaron `running` de un proceso anterior.

    Al boot no hay ambigüedad: el registro de runs vivos (`RUNNING_KEY`)
    es de memoria, así que cualquier fila en `running` perdió a su dueño
    cuando el proceso murió. Sin este barrido quedan en "En curso" para
    siempre — había 8 acumulados, el más viejo de 10 días.

    Reusa `list_zombie_chats`, que ya trae el criterio (y su margen de
    60s, que acá sobra pero no molesta: lo que entre en ese margen lo
    levanta el boot siguiente o la grid de zombies del admin).

    Devuelve cuántos cerró. Best-effort: no rompe el arranque.
    """
    try:
        muertos = await db.list_zombie_chats(older_than_s=60)
    except Exception as e:  # noqa: BLE001 — un barrido no tumba el boot
        logger.warning("no pude listar chats zombies: %r", e)
        return 0
    cerrados = 0
    for chat in muertos:
        try:
            await db.finish_chat(
                chat["id"], status="cancelled",
                error="zombie: el relay reinició y el run murió con el "
                      "proceso anterior")
            cerrados += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("no pude cerrar el zombie %s: %r",
                           chat["id"][:8], e)
    return cerrados


def _avisar_globales_apagados() -> None:
    """Avisa si un modelo global apunta a un spec apagado en el catálogo.

    No es cosmético: `enabled` NO gatea `build_model`, así que el spec
    corre igual — pero la pantalla Modelos lo muestra apagado, y
    `admin._validar_flag` RECHAZA asignárselo a un proyecto por no estar
    prendido. O sea, el relay usa por default algo que no te deja elegir
    a vos, y las dos pantallas que deberían explicarlo dicen lo
    contrario. Un WARNING nombra la contradicción en el boot en vez de
    dejarla para el día que alguien se pregunte por qué el documentador
    corre con un modelo que "está apagado".

    Solo avisa: prender el modelo o cambiar el .env es decisión del
    humano, y fallar el arranque por esto sería peor que el problema.
    """
    from . import config as relay_config
    prendidos = {m["spec"] for m in experts.catalog() if m.get("enabled")}
    if not prendidos:
        return          # catálogo vacío: nada que contrastar
    globales = {
        "FOURBIS_MODEL": relay_config.model_spec(),
        "FOURBIS_PLANNER_MODEL": relay_config.planner_model_spec(),
        "FOURBIS_VERIFIER_MODEL": relay_config.verifier_model_spec(),
        "FOURBIS_DOCUMENTER_MODEL": relay_config.documenter_model_spec(),
        "FOURBIS_COMPACTOR_MODEL": relay_config.compactor_model_spec(),
    }
    for var, spec in globales.items():
        if spec and spec != "test" and spec not in prendidos:
            logger.warning(
                "%s=%s no está PRENDIDO en el catálogo de modelos: el relay "
                "lo usa igual, pero la pantalla Modelos lo muestra apagado y "
                "no vas a poder asignárselo a un proyecto. Prendelo en "
                "/admin/ (tab Modelos) o cambiá la variable.", var, spec)


async def _warm_cbm_session(app: web.Application) -> None:
    """Levanta la sesión MCP residente de cbm al boot (Fase 5, 2026-09-02).

    Sin esto, la sesión recién arranca en el primer tool call de un
    experto o de la Admin UI, que paga el spawn de ~1.1s. Acá se paga
    ese costo al boot, en background, contra un proyecto cualquiera ya
    indexado — después de esto queda UN proceso cbm residente que sirve
    a todos los proyectos (`experts._cbm_toolset` es un singleton de
    módulo, no por proyecto).

    Degrada solo: sin binario cbm, sin proyectos indexados, o cualquier
    falla del spawn → warning y el relay sigue con el camino viejo
    (spawn por CLI, ~1.1s por llamada). Igual que `cbm_watcher.run`.
    """
    try:
        if not experts.cbm_binary_path():
            return
        from . import admin
        db = app[DB_KEY]
        projects = [p for p in await db.list_projects(enabled_only=True)
                    if p.get("include_in_index", 1) and p.get("repo_path")
                    and Path(p["repo_path"]).is_dir()]
        if not projects:
            logger.info("cbm warmup: sin proyectos indexados; nada que calentar")
            return
        cbm_proj = admin._cbm_project_name(projects[0]["repo_path"])
        await experts.cbm_call("index_status", {"project": cbm_proj}, timeout=45.0)
        logger.info("cbm warmup: sesión residente lista (proyecto=%s)", cbm_proj)
    except Exception:
        logger.warning(
            "cbm warmup falló; primer spawn real pagará ~1.1s", exc_info=True)


async def _on_startup(app: web.Application) -> None:
    from . import user_accounts
    user_accounts.load_oauth_config()
    from . import config as relay_config
    # Health-check al iniciar (bug fix 2026-07-08): avisar de qué
    # ejecutable está cargando este código. Si NO es el venv del
    # proyecto, warning claro — suele pasar cuando hay un zombie de
    # un start previo sirviendo el puerto y tú levantas otro encima.
    # Mira stop.ps1/start.ps1 mejorados en ese turno.
    import sys as _sys
    from pathlib import Path as _Path
    _server_file = _Path(__file__).resolve()
    _server_marker = _sys.prefix != _sys.base_prefix
    logger.info(
        "boot: python=%s cwd=%s server=%s in_venv=%s",
        _sys.executable,
        os.getcwd(),
        _server_file,
        _server_marker,
    )
    state_dir = Path(os.environ.get("STATE_DIR", "./state"))
    sessions = SessionRegistry()
    notify = NotifyClient(base_url=os.environ.get("BOT_NOTIFY_URL", default_bot_url()))
    skills = SkillCache()
    db = Database()
    await db.init_schema()
    await finalization.retry_pending(db)
    # ADR-037 fase 2: el cache de roles se llena una vez acá. Es un
    # lookup por request y la tabla la edita un humano por SQL, así que
    # una consulta por request no compraría nada.
    identity.load_roles(await db.list_users())
    # Mismo criterio que los roles: el catálogo de modelos es un lookup
    # por run y lo edita un humano. Se refresca solo al escribir por la
    # API (ver admin._refrescar_catalogo).
    experts.load_catalog(await db.list_models())
    # Los avisos resuelven los valores efectivos, incluida system_config.
    relay_config.set_runtime_config(await db.all_config())
    _avisar_globales_apagados()
    # Chats que quedaron en `running` de un relay anterior. Va ANTES de
    # sanar los grafos porque `sanar` repara las TAREAS y esto repara la
    # fila del chat, que nadie tocaba: los chats de un grafo cancelado o
    # fallado no entran en `list_active_graphs` y quedaban en `running`
    # para siempre, contados como vivos por la UI y con el reloj
    # corriendo solo. Visto hoy: dos nodos de un grafo cancelado
    # sobrevivieron al reinicio como zombies.
    try:
        _huerfanos = await db.reap_running_chats(
            "se cortó el proceso del relay mientras este chat corría")
        if _huerfanos:
            logger.warning(
                "boot: cerré %d chat(s) que quedaron en running de una "
                "corrida anterior", _huerfanos)
    except Exception:  # noqa: BLE001 — un chat colgado no impide el boot
        logger.exception("boot: no pude cerrar los chats huérfanos")
    # F4: grafos que quedaron a medias cuando este relay (o el anterior)
    # se cortó. Se SANAN pero NO se relanzan solos: soltar reservas y
    # marcar los nodos huérfanos es reparación, y arrancar trabajo nuevo
    # sin que nadie lo pida es otra cosa. El humano lo retoma con
    # `POST /graphs/{id}/resume` o con el ▶ del panel.
    try:
        from . import orquestador as _orq
        for _gid in await db.list_active_graphs():
            if _sanadas := await _orq.sanar(db, _gid):
                logger.warning(
                    "boot: el grafo %s quedó a medias; sané %d tareas. "
                    "No lo relanzo solo: retomalo desde el panel.",
                    _gid, _sanadas)
    except Exception:  # noqa: BLE001 — un grafo roto no impide el boot
        logger.exception("boot: no pude sanar los grafos a medias")
    # Iter 9.8: auto-seed del proyecto `notes` (workspace de notas/
    # bitácora/decisiones, sin repo). Idempotente — se hace al boot
    # aunque la DB ya tenga datos. La carpeta en disco también se
    # crea acá (la DB no toca filesystem).
    try:
        notes = await db.ensure_notes_project()
        if notes is not None:
            from pathlib import Path as _P
            notes_root = _P(notes["repo_path"]).expanduser()
            notes_root.mkdir(parents=True, exist_ok=True)
            logger.info("notes-workspace listo: %s", notes_root)
    except Exception as e:  # noqa: BLE001
        logger.warning("ensure_notes_project falló: %r (sigo sin)", e)
    # Iter 9.10: la migración consults → chats ya corrió en el boot
    # de iter 9.8 y la tabla consults se dropeó en este pase. Nada
    # que migrar.
    # Iter 9.7: cerrar preguntas abiertas de runs muertos (el
    # orchestrator no pudo cerrarlas porque el proceso cayó).
    # Sin esto, las preguntas quedan abiertas para siempre.
    try:
        n_closed = await db.orphan_open_questions()
        if n_closed:
            logger.info(
                "startup: cerradas %d preguntas huérfanas de runs muertos",
                n_closed)
    except Exception as e:  # noqa: BLE001
        logger.warning("orphan_open_questions falló: %r", e)
    # …y lo mismo con los chats: un `running` al boot es de un proceso
    # que ya no existe (2026-08-17).
    n_zombies = await _reap_zombie_chats(db)
    if n_zombies:
        logger.info("startup: cerrados %d chats zombies de runs muertos",
                    n_zombies)
    registry = CommandRegistry(db)
    n_cmds = await registry.load_from_db()
    app[SESSIONS_KEY] = sessions
    app[NOTIFY_KEY] = notify
    app[SKILLS_KEY] = skills
    app[DB_KEY] = db
    app[COMMANDS_KEY] = registry
    app[RUNNING_KEY] = {}
    # Ver BG_TASKS_KEY: referencias vivas a las tasks de experto, para
    # poder drenarlas al cerrar (RUNNING_KEY se vacía antes de tiempo).
    app[BG_TASKS_KEY] = set()
    app[GRAFOS_KEY] = {}
    # Fase 1: progress store para liveness on-demand + progreso SSE.
    app[PROGRESS_KEY] = {}
    # Iter 5.4: state_dir de night runs (plan mirror). Lo necesitan
    # los endpoints /admin/api/night-runs/* para leer el espejo del plan
    # ledger. Sin esto el detail devuelve plan_tasks=[] aunque el
    # plan.md exista en disco.
    app[STATE_DIR_KEY] = state_dir
    # ADR-028: registry de night runs vivos.
    app[NIGHT_KEY] = {}
    # F1: pool de MCPs on-demand + reaper por idle.
    pool = McpPool()
    # F2: installer de MCPs desde GitHub (jobs en memoria, no reaper).
    app[MCP_INSTALLER_KEY] = McpInstaller()
    # Alta de skills desde GitHub (jobs en memoria; TTL 1h, sweep lazy).
    app[SKILL_BROWSER_KEY] = SkillBrowser()
    app[MCP_POOL_KEY] = pool
    app[MCP_REAPER_KEY] = asyncio.create_task(pool.reaper_loop())
    # Iter 9.11: probe de health al boot para MCPs stdio externos
    # on-demand. Sin esto el campo health queda 'unknown' hasta que un
    # experto los use (el código de update vive dentro de
    # _catalog_toolsets, en experts.py). Solo externos:
    # heurística = command sin path absoluto (los wrappers propios
    # usan sys.executable + ruta .py absoluta y andan sin probe).
    # Bound por MCP_INIT_TIMEOUT_S por probe, no bloquea el boot.
    app[MCP_HEALTH_PROBE_KEY] = asyncio.create_task(
        _probe_external_mcps_health(app))
    # ADR-025: sweeper de auto-close (24h de inactividad → cierra + compacta)
    app[SWEEPER_KEY] = asyncio.create_task(_autoclose_sweeper(app))
    app[EXPORT_RETRY_KEY] = asyncio.create_task(_export_retry_loop(app))
    await task_service.recover(db)
    app[task_service.TASK_RUNNERS_KEY] = {}
    app[task_service.TASK_LOOP_KEY] = asyncio.create_task(task_service.loop(app))
    # Opción A: auto-reindex incremental de cbm por file-watcher.
    # Degrada solo (flag off / sin cbm / sin watchdog → no-op con log).
    from . import cbm_watcher
    app[CBM_WATCHER_KEY] = asyncio.create_task(cbm_watcher.run(app))
    # Fase 5 (2026-09-02): calentar la sesión MCP residente de cbm.
    # Medido: sin proceso cbm vivo, cada spawn paga ~1.1s (295MB de
    # imagen a cargar); con UNO residente, 15-30ms — factor 65x, y
    # abarata TODOS los spawns del relay (index_repository,
    # _cbm_cli_text, futuros), no solo el primero. Fire-and-forget:
    # no puede bloquear el boot ni voltearlo si cbm no está instalado.
    app[CBM_WARMUP_KEY] = asyncio.create_task(_warm_cbm_session(app))
    # Health-check: exponer qué versión cargó realmente el proceso
    # (Fase 1.5 — bug fix 2026-07-08 "no se actualizaba el relay").
    # El admin UI /health ya devuelve relay_version; este log deja
    # rastro en consola para que un `Get-Content logs\relay.log`
    # muestre de un vistazo qué PID carga qué archivos.
    _server_filename = _server_file.name
    _in_venv_hint = "✓ venv" if _server_marker else "⚠ NO venv"
    # 2026-08-28: guard contra la clase entera de bugs
    # "variable no inicializada en run_expert_staged" (T1.2 fue uno).
    # Si la inicialización de `Bitacora` o el call site al verificador
    # cambian, este check lo detecta al boot en vez de esperar al
    # primer chat. Falla rápido y ruidoso antes de aceptar tráfico.
    try:
        from .experts import Bitacora, run_expert_staged  # noqa: F401
        btest = Bitacora()
        btest.marcar_paso(1, "boot-check")
        bj = btest.volcar()
        if not bj or "pasos" not in bj:
            raise RuntimeError(f"Bitacora.volcar() no serializó pasos: {bj!r}")
        logger.info("boot-check Bitacora OK: %s", bj)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "boot-check FALLÓ (Bitacora / run_expert_staged): %s — "
            "el relay NO va a poder ejecutar el verificador. "
            "Revisar el último PR mergeado a develop antes de seguir.",
            exc, exc_info=True)
        raise SystemExit(2) from exc
    logger.info(
        "boot completo: %d commands, modulo cargado=%s [%s]",
        n_cmds, _server_filename, _in_venv_hint)
    logger.info(
        "relay listo (push+expertos): prompts=%s notify=%s api_key=%s db=%s commands=%d",
        state_dir / "prompts", notify.url, "set" if _get_api_key() else "off",
        db.path, n_cmds,
    )


async def _probe_external_mcps_health(app: web.Application) -> None:
    """Probe de health al boot para MCPs stdio externos on-demand.

    Bug fix iter 9.11: el código de update de health vivía dentro de
    `_catalog_toolsets` (experts.py), entonces el campo `health` se
    quedaba en 'unknown' para MCPs que nadie usaba nunca. Ahora probe
    al boot: external stdio on-demand → handshake one-shot → setea
    'ok' o 'handshake_failed'. No bloquea el boot (background task,
    bounded por MCP_INIT_TIMEOUT_S por probe).

    Heurística de "externo": command sin path absoluto. Wrappers
    propios (github_mcp.py, playwright_mcp.py) usan sys.executable +
    ruta .py absoluta y ya tienen health=ok sin probe. Siempre probe
    para cbm / sequential-thinking / cualquier npx / cmd.
    """
    # 2026-08-16: apagable. El probe LEVANTA PROCESOS de verdad (`npx -y
    # …`, `uvx …`) y en la primera corrida además los descarga. Eso está
    # bien en una máquina con red, y está mal en la suite: desde que el
    # catálogo trae MCPs sembrados, cada create_app() de un test spawnea
    # dos subprocesses y espera su timeout. Medido: test_conversations_ui
    # pasó de 2.1s a 19.3s. Mismo criterio que CBM_AUTO_WATCH=0 en el
    # conftest — un test no debería tocar procesos ni red de la máquina.
    if os.environ.get("FOURBIS_MCP_HEALTH_PROBE", "").strip() in ("0", "off"):
        logger.debug("health-probe: apagado por FOURBIS_MCP_HEALTH_PROBE")
        return
    db = app[DB_KEY]
    try:
        rows = await db.list_mcp_servers(enabled_only=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("health-probe: list_mcp_servers falló (%r), skip", e)
        return
    n_ok = n_failed = n_skipped = 0
    n_skipped_config = 0  # env vars faltantes (postgres-mcp sin POSTGRES_MCP_URI)
    for row in rows:
        if row.get("transport") != "stdio":
            continue
        if not row.get("on_demand"):
            continue
        cmd = (row.get("command") or "").strip()
        # Heurística externo: el path no es absoluto O apunta a algo
        # sin extensión .exe/.py local (cmd/npx/node pelados).
        is_external = not Path(cmd).is_absolute() or Path(cmd).name.lower() in {
            "cmd", "npx", "node", "npm",
        }
        if not is_external:
            n_skipped += 1
            continue
        # El probe y la escritura del health viven en mcp_pool para que
        # el botón de re-chequeo de la Admin UI no escriba otra cosa.
        health, error = await mcp_pool.probe_and_store_health(db, row)
        if health == "ok":
            n_ok += 1
            logger.info("health-probe: %r → ok", row["name"])
        elif health == "skipped":
            # 2026-08-28: separado de los wrappers locales. Son MCPs
            # sembrados pero sin config de runtime (env vars faltantes).
            # El log sale en INFO, no WARNING: NO es un fallo.
            n_skipped_config += 1
            logger.info(
                "health-probe: %r → skipped (%s) — setea la env var para "
                "que arranque en el próximo boot", row["name"], error)
        else:
            n_failed += 1
            logger.warning(
                "health-probe: %r → handshake_failed (%s)", row["name"], error)
    logger.info(
        "health-probe completo: %d ok, %d failed, %d skipped "
        "(%d wrappers locales, %d sin env var)",
        n_ok, n_failed, n_skipped + n_skipped_config,
        n_skipped, n_skipped_config)


# Cuánto se espera a los runs de experto vivos al apagar la app. Corto a
# propósito: acota el cierre y alcanza de sobra para los runs con
# TestModel de la suite, que es donde este drenaje importa de verdad.
_DRAIN_RUNNING_TIMEOUT_S = 10.0


async def _drain_running_experts(app: web.Application) -> None:
    """Espera a los runs de experto en vuelo antes de terminar de cerrar.

    Sin esto, `_run_expert_bg` sigue escribiendo en la DB después de que
    el test cerró su TestClient, y cuando el `TemporaryDirectory` borra
    el tmpdir Windows aborta la limpieza con PermissionError /
    NotADirectoryError. Se manifestaba como un ERROR intermitente que
    saltaba de archivo en archivo según el orden de la suite, y que no
    tenía nada que ver con el test al que se le atribuía.

    Va acá, en el cleanup de la app, y no en cada fixture: son 38
    archivos de test con la misma fixture copiada, y arreglarla uno por
    uno deja el mismo bug latente en el número 39.

    Se drena BG_TASKS_KEY y no RUNNING_KEY: `_run_expert_bg` vacía el
    segundo en su `finally` y después todavía calcula sugerencias,
    persiste y notifica. Esperar sobre RUNNING_KEY es esperar sobre un
    dict que ya se vació — que es exactamente por qué la primera versión
    de este drenaje no arregló nada.
    """
    # …y `_bg_tasks`, que es la MISMA lección una colección más allá: las
    # que larga `_spawn_bg` (compactación, spawns del sweeper) vivían en
    # un set de módulo que nadie esperaba. Una compactación seguía
    # escribiendo la base después de que la app cerró; en los tests eso
    # borra el tempdir abajo de la task, y en producción deja la reserva
    # del workspace tomada por una task que ya nadie mira.
    # Y se vuelve a mirar en cada vuelta, porque una foto no alcanza: una
    # tarea que está terminando puede largar otra —un turno que dispara
    # su grafo es el caso normal, no el raro— y esa hija nacía después
    # del `asyncio.wait` y quedaba afuera. El cierre seguía igual y le
    # cerraba los clientes MCP/HTTP en la cara.
    #
    # El plazo es global y no por vuelta: si no, una cadena de tareas que
    # se largan entre sí estira el cierre indefinidamente, cada una con
    # su tope entero.
    limite = time.monotonic() + _DRAIN_RUNNING_TIMEOUT_S
    while True:
        pendientes = [t for t in ((app.get(BG_TASKS_KEY) or set()) | _bg_tasks)
                      if not t.done()]
        if not pendientes:
            return
        resto = limite - time.monotonic()
        if resto <= 0:
            break
        await asyncio.wait(pendientes, timeout=resto)

    for task in pendientes:
        # Se pasó del tope: cancelar es mejor que colgar el cierre.
        task.cancel()
    logger.warning(
        "cierre: %d run(s) de experto no terminaron en %.0fs, cancelados",
        len(pendientes), _DRAIN_RUNNING_TIMEOUT_S)
    # Dejar que el rescate persista antes de cerrar sus clientes MCP/HTTP.
    _, sin_responder = await asyncio.wait(pendientes, timeout=5)
    if sin_responder:
        logger.warning("cierre: %d tareas no respondieron a la cancelación",
                       len(sin_responder))


async def _frenar_productores(app: web.Application) -> None:
    """Corta los loops periódicos que pueden largar trabajo nuevo.

    Va ANTES del drenaje: el sweeper de auto-close larga tareas con
    `_spawn_bg`, así que cancelarlo después significaba drenar mientras
    alguien seguía agregando. Con el productor frenado, la lista de
    pendientes solo puede achicarse.
    """
    for task_key in (task_service.TASK_LOOP_KEY, SWEEPER_KEY, EXPORT_RETRY_KEY, CBM_WATCHER_KEY,
                     CBM_WARMUP_KEY, MCP_HEALTH_PROBE_KEY):
        task = app.get(task_key)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


async def _on_cleanup(app: web.Application) -> None:
    await _frenar_productores(app)
    await _drain_running_experts(app)
    # F1: apagar reaper y matar los subprocesos MCP vivos del pool.
    reaper = app.get(MCP_REAPER_KEY)
    if reaper is not None:
        reaper.cancel()
        try:
            await reaper
        except asyncio.CancelledError:
            pass
    pool = app.get(MCP_POOL_KEY)
    if pool is not None:
        await pool.shutdown()
    # La sesión de cbm no vive en el pool (un proceso sirve a todos los
    # proyectos), así que se cierra aparte o queda huérfana: son 273MB.
    await experts.close_cbm_session()
    # ADR-028: night runs vivos — cancel duro; la fila queda colgada y
    # night_start la cierra como crashed en el próximo arranque.
    for _orch, task in (app.get(NIGHT_KEY) or {}).values():
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    notify: NotifyClient = app[NOTIFY_KEY]
    await notify.aclose()
