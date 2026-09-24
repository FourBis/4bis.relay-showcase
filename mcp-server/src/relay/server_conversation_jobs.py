"""Server domain handlers extracted from the composition entrypoint."""
from __future__ import annotations

import asyncio
from typing import Optional

from aiohttp import web

from .server_common import DB_KEY, _spawn_bg, logger, relay_config
from . import coordination
from . import finalization
from . import git_flow
from . import memory
from .db import Database
# ---------- conversaciones (ADR-025) + compactación (ADR-026) ----------

SWEEP_INTERVAL_S = 1800.0  # cada 30 min el sweeper revisa auto-close
#: El reintento de exportaciones tiene cadencia propia (2026-09-09).
#: Viajaba de colada en el sweeper de auto-close, o sea que una
#: exportación pendiente esperaba hasta media hora por una cadencia que
#: no tiene nada que ver con ella. Son dos trabajos distintos.
EXPORT_RETRY_INTERVAL_S = 60.0


_COMPACTING: set[str] = set()

# Estado del PR en background por conversación (ADR-025 / fix 2026-07-21).
# En proceso, no en la DB: es efímero (dura lo que el job) y el resultado
# durable ya queda en conversations.pr_url. Si el relay reinicia a mitad,
# el poll de la UI cae en "unknown" y el usuario ve el PR en GitHub.
_PR_JOBS: dict[str, dict] = {}


def _horas_legibles(ms: int) -> str:
    """`ms` -> "2h 49m" / "13m" / "40s". Para leer, no para parsear."""
    s = round((ms or 0) / 1000)
    if s < 60:
        return f"{s}s"
    m = s // 60
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


async def _finalize_pr_bg_bound(db: Database, conv_id: str, project_slug: str,
                          project: dict, branch: str, summary: str,
                          issue_number: Optional[int] = None) -> None:
    """Verify + PR a develop, fuera del request de /close.

    Corre build/test del proyecto (hasta 420s) y le pide al LLM la
    descripción del diff antes de abrir el PR; eso no entra en el
    timeout de un fetch. Publica progreso en `_PR_JOBS[conv_id]` para
    GET /conversations/{id}/pr. Best-effort: nunca lanza.
    """
    from . import night
    job = _PR_JOBS[conv_id] = {"state": "verifying", "pr_url": None,
                               "error": None, "draft": False,
                               "committed": False}
    title = f"[{project_slug}] {branch}"
    # Horas del PR (2026-08-31): el tiempo de USO —la suma de lo que
    # duraron los runs—, no el reloj de pared de la conversación. Es el
    # mismo número que muestra el chip ⏱ del header, y el único con
    # sentido para "cuánto llevó esto": medido sobre `un run de ejemplo`, la
    # pared decía 16h16 y el trabajo real fueron 2h49.
    #
    # En su propio try porque acá todavía no entramos al `try` grande:
    # una excepción dejaría el job clavado en "verifying" y el PR sin
    # abrir, que es justo lo que el docstring promete que no pasa. Sin
    # las horas el PR sale igual.
    try:
        uso = await db.conversation_usage(conv_id)
    except Exception:  # noqa: BLE001
        logger.exception("no pude calcular el tiempo de uso de conv=%s",
                         conv_id[:8])
        uso = {"runs": 0, "ms": 0}
    trabajo = (f"{_horas_legibles(uso['ms'])} de trabajo en {uso['runs']} "
               f"run{'' if uso['runs'] == 1 else 's'} — ") if uso["runs"] else ""
    body = ((summary or "").strip() or "Cambios de la conversación de 4bis.relay.") + (
        f"\n\n_Conversación `{conv_id[:8]}` — {trabajo}4bis.relay_")
    # Seguimiento por GitHub (fase 4): la keyword la interpreta GitHub al
    # mergear — cierra el issue y el tablero lo mueve a Done sin que nadie
    # actualice estado a mano. Va en el body base, no en el `body_builder`,
    # porque ese es best-effort y puede no correr.
    if issue_number:
        body += f"\n\nCloses #{issue_number}"

    async def _build_body(stat: str, diff: str) -> tuple[str, bool]:
        """Verify (build+test) + descripción del diff → body del PR.

        El verify NO bloquea el PR: si viene rojo el PR se abre igual
        pero como draft y con el ❌ arriba de todo. Bloquear dejaría el
        trabajo del experto varado en una rama que nadie mira.
        """
        ok, verify_md = await night.verify_repo(project["repo_path"], project)
        job["verify"] = {True: "ok", False: "failed", None: "none"}[ok]
        job["state"] = "describing"
        described = await memory.describe_changes(diff, stat=stat)
        job["state"] = "opening"
        return "\n\n".join([verify_md] + ([described] if described else [])), ok is False

    try:
        outcome = await git_flow.finalize_conversation_pr(
            project["repo_path"], branch, title=title, body=body,
            body_builder=_build_body)
    except Exception as e:  # noqa: BLE001 — best-effort; el /close ya respondió
        logger.exception("PR en background de conv=%s rompió", conv_id[:8])
        job.update(state="error", error=f"{type(e).__name__}: {e}")
        return
    job.update(state="done" if outcome.get("pr_url") else "error",
               pr_url=outcome.get("pr_url"), error=outcome.get("error"),
               draft=outcome.get("draft", False),
               committed=outcome.get("committed", False))
    if outcome.get("pr_url"):
        await db.set_conversation_pr(conv_id, outcome["pr_url"])
        # 2026-07-27: la rama local ya está pusheada y con PR, así que no
        # aporta nada — antes quedaba una por conversación esperando que
        # alguien la borrara a mano desde la Admin UI. El repo vuelve a
        # develop. git_flow no toca ni el remoto ni ramas protegidas.
        cleanup = await git_flow.cleanup_conversation_branch(
            project["repo_path"], branch)
        job["branch_deleted"] = cleanup["deleted"]
        job["branch"] = branch
        if cleanup.get("error"):
            logger.warning("no pude limpiar la rama %s: %s",
                           branch, cleanup["error"])
    logger.info("PR background conv=%s → %s", conv_id[:8],
                outcome.get("pr_url") or outcome.get("error"))


async def _finalize_pr_bg(db: Database, conv_id: str, project_slug: str,
                          project: dict, branch: str, summary: str,
                          issue_number: Optional[int] = None,
                          requested_by: Optional[str] = None) -> None:
    """Run the close PR job with the conversation's durable actor."""
    from . import user_accounts
    with user_accounts.bind_actor(db, requested_by):
        await _finalize_pr_bg_bound(db, conv_id, project_slug, project, branch,
                                    summary, issue_number=issue_number)


async def _compact_and_store(db: Database, conv_id: str,
                             project_slug: str, messages_json: str) -> None:
    """Compacta una conversación cerrada → summary + facts + FTS5.

    Best-effort total (ADR-026): si el compactador falla, la
    conversación queda closed sin summary y se loggea. Reintentable:
    volver a llamar POST /conversations/{id}/close re-dispara.
    Guard anti-doble-gasto: dos /close casi simultáneos (o /close +
    sweeper) compactarían dos veces — el segundo se saltea.
    """
    if conv_id in _COMPACTING:
        logger.info("compactación conv=%s ya en curso, salteo", conv_id[:8])
        return
    _COMPACTING.add(conv_id)
    try:
        # Hechos vigentes → el compactador detecta obsoletos y no duplica.
        existing_facts = await db.list_facts(project_slug, limit=100)
        result = await memory.compact_conversation(
            messages_json, existing_facts=existing_facts)
        if result is None:
            logger.info("compactación conv=%s: transcript vacío, salteo",
                        conv_id[:8])
            return
        if result.summary.strip():
            await db.set_conversation_summary(conv_id, result.summary)
            await db.add_memory(conv_id, project_slug, result.summary)
        n_facts = await db.add_facts(
            project_slug, result.facts, source_conversation=conv_id)
        # Supersede soft: solo ids que realmente le mostramos (el LLM no
        # puede invalidar hechos fuera de su vista).
        shown_ids = {f["id"] for f in existing_facts}
        obsolete = [i for i in result.obsolete_fact_ids if i in shown_ids]
        n_superseded = 0
        if obsolete:
            n_superseded = await db.supersede_facts(
                obsolete, project_slug, superseded_by=conv_id)
        # Autoaprendizaje (2026-07-12): si el compactador destiló un
        # procedimiento reusable, queda como BORRADOR pendiente de
        # aprobación en la Admin UI (tab Skills). Nunca se instala solo.
        draft_id = None
        if result.skill is not None and result.skill.content.strip():
            draft_id = await db.add_skill_draft(
                name=result.skill.name,
                description=result.skill.description,
                content=result.skill.content,
                project_slug=project_slug,
                source_conversation=conv_id)
        logger.info(
            "compactación conv=%s ok: summary=%d chars, facts=%d, "
            "superseded=%d%s",
            conv_id[:8], len(result.summary), n_facts, n_superseded,
            f", skill draft #{draft_id} pendiente" if draft_id else "")
    except Exception:
        logger.exception("compactación conv=%s falló (queda sin summary)",
                         conv_id[:8])
    finally:
        _COMPACTING.discard(conv_id)


async def _export_retry_loop(app: web.Application) -> None:
    """Reintenta las exportaciones pendientes, con su propia cadencia.

    El backoff de `finalization` decide CUÁNDO le toca a cada una; esto
    solo pregunta seguido. Un loop aparte y no un pedazo del sweeper de
    auto-close porque son dos trabajos con dos relojes: media hora es
    razonable para cerrar conversaciones viejas y es una eternidad para
    recuperar el .md de un chat que el humano está mirando.
    """
    db: Database = app[DB_KEY]
    while True:
        await asyncio.sleep(EXPORT_RETRY_INTERVAL_S)
        try:
            await finalization.retry_pending(db)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — un barrido caído no baja el relay
            logger.exception("reintento de exportaciones: falló la vuelta")


async def _autoclose_sweeper(app: web.Application) -> None:
    """Cierra conversaciones abiertas sin actividad > N horas y las
    compacta (ADR-025). N = FOURBIS_CONV_AUTOCLOSE_H (default 24).

    No hace falta chequear runs vivos: touch_conversation se llama al
    INICIO de cada run, y un run dura <= expert_timeout << 24h.
    """
    from . import config as relay_config
    db: Database = app[DB_KEY]
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_S)
        try:
            hours = relay_config.conv_autoclose_hours()
            for conv in await db.stale_open_conversations(hours):
                project = await db.get_project(conv["project_slug"])
                if project and coordination.busy(db, project):
                    continue
                if await db.close_conversation(conv["id"]):
                    logger.info("auto-close conv=%s (> %sh sin actividad)",
                                conv["id"][:8], hours)
                    if conv.get("messages_json"):
                        _spawn_bg(_compact_and_store(
                            db, conv["id"], conv["project_slug"],
                            conv["messages_json"]))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sweeper de auto-close rompió (sigo)")
