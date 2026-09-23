"""Persistencia del modo nocturno y conexiones SQL configuradas."""
from __future__ import annotations

from typing import Optional

from .db_support import now_iso

class DatabaseNightMixin:

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
