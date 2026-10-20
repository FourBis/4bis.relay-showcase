"""Infraestructura de conexión y transacciones SQLite."""
from __future__ import annotations

import asyncio
import sqlite3
from contextlib import suppress
from pathlib import Path
from typing import Optional


from . import config
from .reporting import local_timestamp

class DatabaseCore:

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
        return await self._in_thread(self._run, sql, params)

    async def _in_thread(self, operation, *args):
        """Espera el cierre del SQLite interno incluso al cancelar el await.

        Cancelar una coroutine no detiene el hilo ni deshace su transacción.
        La cancelación se propaga una vez terminado el commit/rollback y cierre.
        """
        worker = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            while not worker.done():
                with suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(worker)
            with suppress(asyncio.CancelledError, Exception):
                worker.result()
            raise

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
        await self._in_thread(self._run_tx, sentencias)

    # ---- projects ----
