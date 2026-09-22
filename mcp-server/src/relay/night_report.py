"""Reporte matutino y escritura del resultado del run nocturno."""
from __future__ import annotations

import logging
import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .night_generator import _verdict_icon
from .night_types import DEFAULT_DEADLINE_HOUR, NightTask, TaskResult

logger = logging.getLogger("relay.night")


class MorningReporter:
    """Compila el reporte de ingeniería (ÚNICO entregable core, decisión
    12 de la enmienda ADR-028) + push Discord best-effort."""

    def __init__(self, state_dir: Path) -> None:
        self.reports_dir = state_dir / "morning-reports"

    def build_md(
        self, *, run_id: str, project_slug: str, started_at: str,
        deadline_at: str, end_reason: str, tasks: list[NightTask],
        results: list[TaskResult], error: Optional[str] = None,
        branch: str = "", pr_url: Optional[str] = None,
        missing_points: Optional[list[str]] = None,
    ) -> str:
        done = [r for r in results if r.status == "done"]
        rolled = [r for r in results if r.status != "done"]
        discarded = [t for t in tasks if t.status == "discarded"]
        missing_points = missing_points or []
        lines = [
            f"# Night run — {project_slug} — {time.strftime('%Y-%m-%d')}",
            "",
            f"**Run ID**: `{run_id}`",
            f"**Started**: {started_at}",
            f"**Deadline**: {deadline_at}",
            f"**End reason**: {end_reason}",
            f"**Branch**: `{branch or '(sin rama)'}`",
            f"**PR**: {pr_url or '(no abierto — sin tareas done o push/gh falló)'}",
            "",
            "## TL;DR",
            "",
            f"{len(done)} tareas completadas (PR único: "
            f"{pr_url or 'no abierto'}), "
            f"{len(rolled)} rollbacks, {len(discarded)} descartadas en Fase 1."
            + (f" ERROR: {error}" if error else ""),
            "",
        ]
        if missing_points:
            lines += [
                "## ⚠️ Puntos de la directiva NO representados como tareas",
                "",
                "El LLM no devolvió tareas para estos IDs de la directiva. "
                "Típico: output_tokens agotado en directivas largas. "
                "Reformula la directiva (más corta) o pártela en dos runs:",
                "",
            ]
            lines += [f"- **{pid}**" for pid in missing_points]
            lines.append("")
        if done:
            lines += ["## Tareas completadas", "",
                      "| Task | Veredicto | Título | Branch | PR |",
                      "|---|---|---|---|---|"]
            by_id = {t.id: t for t in tasks}
            for r in done:
                title = by_id.get(r.task_id, NightTask(r.task_id, "?", [])).title
                pr = r.pr_url or (pr_url or "local")
                lines.append(
                    f"| {r.task_id} | {_verdict_icon(r.verdict)} | {title} "
                    f"| `{r.branch}` | {pr} |")
            lines.append("")
            # Lo que hace útil al veredicto es la lista corta de los que NO
            # dijeron `complete`: son los PR que hay que mirar primero a las
            # 7am. Commitearon —los gates pasaron— pero el verificador dudó.
            dudosos = [r for r in done
                       if r.verdict and r.verdict != "complete"]
            if dudosos:
                lines += [
                    "### ⚠️ Pasaron los gates pero el verificador dudó", "",
                    "El build y los tests pasaron, así que commitearon. "
                    "Esto es el juicio, no la máquina: mirá estos PR primero.",
                    "",
                ]
                for r in dudosos:
                    fb = r.verdict_feedback or "(sin detalle)"
                    lines.append(f"- **{r.task_id}** — `{r.verdict}`: {fb}")
                lines.append("")
        if rolled:
            lines += ["## Rollbacks / fallos", ""]
            for r in rolled:
                lines.append(f"- **{r.task_id}** (`{r.branch}`): {r.error}")
            lines.append("")
        if discarded:
            lines += ["## Descartadas en Fase 1 (refs inválidas / sin refs)", ""]
            for t in discarded:
                lines.append(f"- **{t.id}**: {t.title} — {t.note}")
            lines.append("")
        return "\n".join(lines)

    async def emit(
        self, md: str, *, project_slug: str, run_id: str,
        notify: Any = None, discord_channel: str = "",
        summary: str = "",
    ) -> Path:
        """Escribe el archivo (fuente de verdad) + push Discord best-effort."""
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        # <fecha>-<slug>.md: dos proyectos la misma noche no colisionan
        # (drift documentado vs el `<fecha>.md` de la spec original).
        path = self.reports_dir / f"{time.strftime('%Y-%m-%d')}-{project_slug}.md"
        await asyncio.to_thread(path.write_text, md, encoding="utf-8")
        if notify is not None:
            try:
                await notify.send(
                    agent_id=f"night:{run_id}", kind="done",
                    message=summary or f"[4bis / {project_slug}] Night run finished.",
                    metadata={"run_id": run_id, "project": project_slug,
                              "report_path": str(path),
                              "discord_channel": discord_channel},
                )
            except Exception as e:  # noqa: BLE001 — Discord es best-effort
                logger.warning("push del reporte a Discord falló: %r", e)
        return path


# ---------- NightOrchestrator ----------


def default_deadline() -> datetime:
    """Próximas 7am hora local (aware)."""
    now = datetime.now().astimezone()
    deadline = now.replace(hour=DEFAULT_DEADLINE_HOUR, minute=0,
                           second=0, microsecond=0)
    if deadline <= now:
        deadline += timedelta(days=1)
    return deadline
